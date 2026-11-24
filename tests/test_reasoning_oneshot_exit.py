"""One-shot exit codes of the standalone reasoning supervisor and its unit's restart policy.

Synthetic only: the scripted fake loop, fake bridge and fake control plane of the tool-runtime
tests stand in for the Messages client, the MCP server and the control plane. No network, no API
key, no model call.
"""

import argparse
import configparser
import fcntl
import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path

import pytest
from test_anthropic_tool_runtime import (
    AGENT_TOKEN,
    PROMPT,
    SECRETS,
    Collector,
    FakeBridge,
    FakeLoop,
    creds,  # noqa: F401 - fixture
    end_turn,
    grant,
    make_profile,
    private,
)
from test_anthropic_tool_runtime import Control as SettlingControl
from test_reasoning_install import do_apply, env, real  # noqa: F401 - env is a fixture

import tessera_agent.reasoning_install as ri
import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.reasoning_install_cli import _options, add_parser
from tessera_agent.remote_reasoning_store import ReasoningStore

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "deploy/remote-agent-platform/tessera-reasoning.service"


class World:
    """Counts every control-plane call, harness build and model send across starts."""

    def __init__(self, grant_reply=None):
        self.control_calls = []
        self.built = []
        self.grant_reply = grant_reply


def start(
    tmp_path,
    creds,  # noqa: F811
    monkeypatch,
    world,
    *,
    profile=None,
    script=(),
    argv_extra=(),
    prompt=PROMPT,
    command="run",
    token=True,
    manager=True,
):
    profile = (profile or make_profile(tools=(), max_steps=1)) | {
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "control_url": "http://127.0.0.1:1",
        "agent_token_env": "T_AGENT",
        "project_id": "project1",
    }
    path = private(tmp_path / "profile.json", json.dumps(profile))
    prompt_path = private(tmp_path / "prompt.txt", prompt)
    if token:
        monkeypatch.setenv("T_AGENT", AGENT_TOKEN)
    else:
        monkeypatch.delenv("T_AGENT", raising=False)
    if manager:
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    else:
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    class Control(SettlingControl):
        def call(self, method, path, body=None, key=None):
            world.control_calls.append(path)
            if world.grant_reply is not None and path.startswith("/agent-authority-grants/"):
                return world.grant_reply
            return super().call(method, path, body, key)

    def adapter(profile, *, credentials_dir, **options):
        harness = FakeLoop(profile, credentials_dir=credentials_dir, **options)
        harness.script = list(script)
        world.built.append(harness)
        return harness

    FakeBridge.instances.clear()
    monkeypatch.setattr(rr, "JsonEndpoint", Control)
    monkeypatch.setattr(rr, "ProvenanceClient", Collector)
    monkeypatch.setattr(rr, "AnthropicMessagesReasoning", adapter)
    monkeypatch.setattr(rr, "McpToolBridge", lambda **kwargs: FakeBridge(**kwargs))
    monkeypatch.setattr(rr.time, "sleep", lambda _: None)
    argv = [command, "--profile", str(path), "--state-dir", str(tmp_path / "state")]
    if command == "run":
        argv += ["--prompt-file", str(prompt_path)]
    return rr.main([*argv, *argv_extra])


def journal(tmp_path, run_id="run1"):
    store = ReasoningStore(tmp_path / "state")
    try:
        return store.model_calls(run_id, every=True), store.spend_rows(run_id)
    finally:
        store.close()


def no_leaks(text):
    for secret in SECRETS:
        assert secret not in text
    assert "Traceback" not in text


# -- completed ---------------------------------------------------------------------------------


def test_completed_one_shot_run_exits_0(tmp_path, creds, monkeypatch, capsys):  # noqa: F811
    world = World()
    assert start(tmp_path, creds, monkeypatch, world, script=[end_turn()]) == rr.EXIT_COMPLETED
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["state"] == "completed" and len(receipt["model_calls"]) == 1
    assert world.built[0].sent == 1


def test_restart_of_a_completed_run_exits_0_with_no_call_or_reservation(
    tmp_path,
    creds,  # noqa: F811
    monkeypatch,
    capsys,
):
    assert start(tmp_path, creds, monkeypatch, World(), script=[end_turn()]) == 0
    capsys.readouterr()
    before = journal(tmp_path)
    world = World()
    code = start(tmp_path, creds, monkeypatch, world, script=[end_turn()])
    captured = capsys.readouterr()
    assert code == rr.EXIT_COMPLETED
    # Nothing ran: no harness, bridge, control plane, grant, ledger or model call.
    assert world.built == [] and FakeBridge.instances == []
    assert world.control_calls == []
    assert journal(tmp_path) == before
    assert "run run1 already closed (state completed); not rerun, no model call" in captured.err
    assert json.loads(captured.out)["state"] == "completed"
    no_leaks(captured.out + captured.err)


# -- blocked: each reason keeps a non-zero exit, and a restart never reruns it ------------------


@pytest.mark.parametrize(
    "grant_reply,reason",
    [
        ({**grant(remaining=0), "project_id": "project1"}, "budget_exhausted"),
        (
            {
                **grant(),
                "project_id": "project1",
                "status": "revoked",
                "effective_status": "revoked",
            },
            "grant_revoked",
        ),
        ({**grant(), "project_id": "project1", "revision": 2}, "grant_revision_changed"),
    ],
)
def test_blocked_run_exits_2_and_its_restart_stays_closed(
    tmp_path,
    creds,  # noqa: F811
    monkeypatch,
    capsys,
    grant_reply,
    reason,
):
    world = World(grant_reply)
    code = start(tmp_path, creds, monkeypatch, world, script=[end_turn()])
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert code == rr.EXIT_BLOCKED
    assert receipt["state"] in rr.CLOSED_STATES - {"completed"}
    assert receipt["reason"] == reason and receipt["model_calls"] == []
    assert f"({reason})" in captured.err
    before = journal(tmp_path)
    again = World()
    assert start(tmp_path, creds, monkeypatch, again, script=[end_turn()]) == rr.EXIT_BLOCKED
    captured = capsys.readouterr()
    assert again.built == [] and again.control_calls == [] and journal(tmp_path) == before
    assert "already closed" in captured.err
    no_leaks(captured.out + captured.err)


def test_tool_catalog_block_exits_2(tmp_path, creds, monkeypatch, capsys):  # noqa: F811
    world = World()
    monkeypatch.setattr(FakeBridge, "start", lambda self: (_ for _ in ()).throw(
        rr.ToolBridgeBlocked("tool_server_unavailable")
    ))  # fmt: skip
    code = start(tmp_path, creds, monkeypatch, world, profile=make_profile(), script=[end_turn()])
    assert code == rr.EXIT_BLOCKED
    assert json.loads(capsys.readouterr().out)["reason"] == "tool_server_unavailable"


# -- refused (77) ------------------------------------------------------------------------------


def test_a_held_supervisor_lock_is_refused_77(tmp_path, creds, monkeypatch, capsys):  # noqa: F811
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    holder = os.open(state / "supervisor.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        world = World()
        code = start(tmp_path, creds, monkeypatch, world, script=[end_turn()])
    finally:
        os.close(holder)
    captured = capsys.readouterr()
    assert code == rr.EXIT_REFUSED and world.built == [] and world.control_calls == []
    assert "refused (supervisor_already_running)" in captured.err
    no_leaks(captured.out + captured.err)


def test_rebinding_an_open_run_identity_is_refused_77(tmp_path, creds, monkeypatch, capsys):  # noqa: F811
    store = ReasoningStore(tmp_path / "state")
    store.bind("run1", {"other": "binding"}, 1.0)  # open (ready), bound to another profile
    store.close()
    world = World()
    code = start(tmp_path, creds, monkeypatch, world, script=[end_turn()])
    captured = capsys.readouterr()
    assert code == rr.EXIT_REFUSED and world.built == []
    assert "refused (run_identity_rebind_refused)" in captured.err
    assert journal(tmp_path) == ([], [])


# -- configuration errors (78) -----------------------------------------------------------------


@pytest.mark.parametrize(
    "case,reason",
    [
        ("profile", "profile_invalid"),
        ("credentials", "missing_manager_credential_directory"),
        ("prompt", "prompt_invalid"),
        ("token", "agent_token_missing"),
        ("run_id", "run_id_invalid"),
    ],
)
def test_configuration_errors_exit_78_before_any_call(
    tmp_path,
    creds,  # noqa: F811
    monkeypatch,
    capsys,
    case,
    reason,
):
    world = World()
    kwargs = {}
    if case == "profile":
        kwargs["profile"] = make_profile(tools=(), max_steps=99)
    if case == "prompt":
        kwargs["prompt"] = "   "
    if case == "run_id":
        kwargs["argv_extra"] = ["--run-id", "../escape"]
    if case == "credentials":
        kwargs["manager"] = False
    if case == "token":
        kwargs["token"] = False
    code = start(tmp_path, creds, monkeypatch, world, script=[end_turn()], **kwargs)
    captured = capsys.readouterr()
    assert code == rr.EXIT_CONFIG
    assert f"configuration error ({reason})" in captured.err
    assert world.built == [] and world.control_calls == []
    no_leaks(captured.out + captured.err)


def test_a_crash_is_not_mapped_and_exits_1(tmp_path, creds, monkeypatch):  # noqa: F811
    def boom(*args, **kwargs):
        raise RuntimeError("supervisor failure")

    monkeypatch.setattr(rr, "ReasoningSupervisor", boom)
    with pytest.raises(RuntimeError):  # The interpreter exits 1: the unit may restart it.
        start(tmp_path, creds, monkeypatch, World(), script=[end_turn()])
    assert rr.EXIT_CRASH == 1
    codes = {rr.EXIT_COMPLETED, rr.EXIT_CRASH, rr.EXIT_BLOCKED, rr.EXIT_REFUSED, rr.EXIT_CONFIG}
    assert len(codes) == 5


# -- a fresh run identity from the request ------------------------------------------------------


def test_run_id_argument_starts_a_fresh_run_once(tmp_path, creds, monkeypatch, capsys):  # noqa: F811
    assert start(tmp_path, creds, monkeypatch, World(), script=[end_turn()]) == 0
    capsys.readouterr()
    world = World()
    code = start(
        tmp_path, creds, monkeypatch, world, script=[end_turn()], argv_extra=["--run-id", "run2"]
    )
    receipt = json.loads(capsys.readouterr().out)
    assert code == 0 and receipt["id"] == "run2" and world.built[0].sent == 1
    assert len(journal(tmp_path, "run2")[0]) == 1 and len(journal(tmp_path, "run1")[0]) == 1
    again = World()
    code = start(
        tmp_path, creds, monkeypatch, again, script=[end_turn()], argv_extra=["--run-id", "run2"]
    )
    assert code == 0 and again.built == [] and again.control_calls == []
    assert "run run2 already closed" in capsys.readouterr().err
    code = start(
        tmp_path, creds, monkeypatch, World(), command="status", argv_extra=["--run-id", "run2"]
    )
    assert code == 0 and json.loads(capsys.readouterr().out)["id"] == "run2"


def test_install_new_run_writes_a_fresh_run_id_bound_to_the_prompt(env):  # noqa: F811
    options, src = env
    _, accounts, _ = do_apply(options)
    path = real(options, options.profile_path)
    first = json.loads(path.read_text())
    request = replace(options, profile_template=None, prompt_source=None, new_run=True)
    steps = {s.name: s for s in ri.plan(request, accounts=accounts)}
    assert steps["profile"].state == "differs" and steps["profile"].detail["new_run"] is True
    assert steps["profile"].detail["caps"] == {}
    assert "run_id" in steps["profile"].action
    do_apply(request, accounts=accounts)
    second = json.loads(path.read_text())
    assert re.fullmatch(r"run-[0-9a-f]{16}", second["run_id"])
    assert second["run_id"] != first["run_id"]
    prompt = real(options, options.prompt_path).read_bytes()
    assert second["prompt_sha256"] == hashlib.sha256(prompt).hexdigest()
    assert {k: v for k, v in second.items() if k != "run_id"} == {
        k: v for k, v in first.items() if k != "run_id"
    }
    # The generated value is never printed in the plan.
    plan_text = json.dumps([s.__dict__ for s in ri.plan(request, accounts=accounts)])
    assert second["run_id"] not in plan_text
    # Without the flag nothing changes.
    unchanged = path.read_bytes()
    do_apply(replace(request, new_run=False), accounts=accounts)
    assert path.read_bytes() == unchanged


def _cli(*argv):
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    return _options(parser.parse_args(["reasoning", "install", *argv]))


def test_install_cli_maps_new_run():
    assert _cli("--new-run").new_run is True
    assert _cli().new_run is False


# -- unit restart policy -----------------------------------------------------------------------


def _unit(text):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(text)
    return parser


@pytest.mark.parametrize("source", ["shipped", "rendered"])
def test_unit_restarts_only_a_crash_of_a_manual_one_shot(source):
    text = UNIT.read_text() if source == "shipped" else ri.render_unit(ri.Options())
    unit = _unit(text)
    service = unit["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartPreventExitStatus"].split() == ["2", "77", "78"]
    # Exit 0 is systemd's only success; nothing else is reclassified as success.
    assert "SuccessExitStatus" not in service
    assert unit["Unit"]["StartLimitBurst"] == "3"
    assert unit["Unit"]["StartLimitIntervalSec"] == "120"
    assert not unit.has_section("Install")
