"""The running-campaign steering check (tessera_mcp.steering_running_check): dry run by default,
refusals, and a full run against an in-process control plane with a fake runner and worker: an
agent steers a RUNNING two-stage campaign over MCP tools only (submit, report, pause while a stage
runs, resume, a gate decision) and every step leaves a record. No network, no Docker."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("mcp")

from test_campaign_gates import decide  # noqa: E402
from test_campaign_standing import Sched  # noqa: E402
from test_mcp_steering_e2e import _events, _spec, _stage, _standing  # noqa: E402

from tessera_control import campaign_gates  # noqa: E402
from tessera_control import campaign_provider_worker as worker  # noqa: E402
from tessera_control import campaign_providers as cp  # noqa: E402
from tessera_control.api import create_app  # noqa: E402
from tessera_mcp import steering_running_check as check  # noqa: E402
from tessera_mcp import steering_smoke as smoke  # noqa: E402
from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import CAMPAIGN_ACTIONS, create_server  # noqa: E402

IMAGE = "registry.example/smoke@sha256:" + "a" * 64
AGENT_ACTIONS = sorted(CAMPAIGN_ACTIONS | {"budget:read", "snapshot:read"})
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mcp_steering_running_check.py"
PROJECT = [""]


@pytest.fixture(autouse=True)
def _reset():
    yield
    worker.register_from_config(None)
    campaign_gates.configure_go_authority(accept_automation_labelled=False)


def _caller(app, tmp_path, name, **flags):
    reference = tmp_path / f"{name}.token"
    reference.write_text(f"synthetic-{name}-token-" + "x" * 30)
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, PROJECT[0], "planner.local"),
        httpx.ASGITransport(app=app),
    )
    server = create_server(client, **flags)
    calls: list[str] = []

    def call(tool, **arguments):
        calls.append(tool)
        result = asyncio.run(server.call_tool(tool, arguments))
        return result[1] if isinstance(result, tuple) else result

    call.calls = calls  # type: ignore[attr-defined]
    return call


def _rig(tmp_path):
    from provider_trust_fixture import DERIVED, install_trust

    sched = Sched(tmp_path)
    env = sched.env
    cp.configure_providers({"local_docker": {"worker_principal_id": env.worker["id"]}})
    install_trust({"local-docker": (DERIVED, [env.worker["id"]])})
    env.agent(AGENT_ACTIONS, "steerer")
    env.human()
    PROJECT[0] = env.project["id"]
    app = create_app(env.state)
    agent = _caller(
        app, tmp_path, "steerer", campaign_tools=True, campaign_actions=frozenset(AGENT_ACTIONS)
    )
    agent_approve = _caller(app, tmp_path, "steerer", approval_tool=True)
    person = _caller(app, tmp_path, "human", approval_tool=True)
    return sched, agent, person, agent_approve


def _advance(world):
    def advance(phase):
        world.runner.tick()
        if phase != "running":  # the worker holds back until the check has paused the campaign
            world.poller.run(max_cycles=1)

    return advance


def test_dry_run_is_the_default_and_touches_nothing(capsys):
    assert check.main([]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["mode"] == "dry-run" and document["provider"] == "local-docker"
    assert [s["step"] for s in document["steps"]] == [name for name, _ in check.STEPS]
    assert [s["key"] for s in document["spec"]["stages"]] == ["one", "two"]
    assert document["spec"]["budget_cap_cents"] == 0
    done = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0 and json.loads(done.stdout)["mode"] == "dry-run"


@pytest.mark.parametrize(
    "extra",
    [
        ["--execute"],
        ["--execute", "--confirm", "yes"],
        ["--execute", "--confirm", check.CONFIRM_PHRASE],
    ],
)
def test_execute_needs_the_exact_confirmation_and_the_options(extra, capsys):
    assert check.main(extra) == 2
    assert "refused" in capsys.readouterr().err


def test_the_spec_is_zero_dollar_local_docker_only():
    spec = check.running_spec(IMAGE, {"id": "s", "digest": "d" * 64, "data_class": "public"})
    assert spec["budget_cap_cents"] == 0
    assert {s["provider"] for s in spec["stages"]} == {"local-docker"}
    assert all(s["max_exposure_cents"] == 0 for s in spec["stages"])


def test_an_agent_steers_a_running_campaign_with_a_gate_over_mcp_only(tmp_path):
    sched, agent, person, agent_approve = _rig(tmp_path)
    world, env = sched.world, sched.env
    spec_a = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))
    state: dict[str, str] = {}

    def gate(campaign_a: str) -> str:
        # HUMAN (once): the operator fixes the follow-on stage and its unlock condition, a person
        # (or the owner's labelled automation) records the go. No MCP tool does either.
        unlock = [
            {
                "campaign_id": campaign_a,
                "accept_human_go": True,
                "measurement": {"stage": "two", "name": "score", "op": "ge", "value": 2},
            }
        ]
        standing = _standing(sched, _spec("follow-on", _stage(env, "three", cap=3), cap=5), unlock)
        sched.tick()
        assert not sched.starts(standing["id"])  # the measurement holds; the go is still missing
        campaign_gates.configure_go_authority(accept_automation_labelled=True)
        decide(sched, campaign_a, actor={**env.owner, "automation_label": "ops-script"})
        sched.tick()
        (start,) = sched.starts(standing["id"])
        state["b"] = start["campaign_id"]
        return start["campaign_id"]

    document = check.run_running_check(
        agent,
        person,
        agent_approve,
        image=IMAGE,
        snapshot_id=env.snapshot_id,
        spec=spec_a,
        gate=gate,
        advance=_advance(world),
        held_polls=3,
        wait_seconds=3600.0,  # real time: a slow shared box must not turn into a failure
    )
    steps = {s["step"]: s for s in document["steps"]}
    assert list(steps) == [name for name, _ in check.STEPS] and document["ok"] is True
    assert document["schema"] == check.REPORT_SCHEMA
    assert steps["agent_cannot_approve"]["evidence"]["refused_with"]
    assert steps["running"]["evidence"]["attempt_status"] in check.LIVE_ATTEMPT
    assert steps["report_while_running"]["evidence"]["text_length"] > 0
    assert steps["held"]["evidence"]["first_stage_attempts"] == ["succeeded"]
    assert steps["held"]["evidence"]["second_stage_attempts"] == 0
    assert steps["resume_and_finish"]["evidence"]["status"] == "succeeded"
    assert steps["gate_decision"]["evidence"]["decision_class"] == "owner_automation_labelled"
    assert steps["gate_decision"]["evidence"]["follow_on_started"] is True
    assert steps["terminal_receipt"]["evidence"]["pause_count"] >= 1
    assert steps["terminal_receipt"]["evidence"]["settled_cents"] == 0

    # Receipts, from control's own records: the pause, the approval (a person's), the terminal
    # receipt, and the gate's unlock evaluation for the follow-on campaign.
    paused = {e["campaign_id"] for e in _events(env, "campaign.paused")}
    assert len(paused) == 1
    assert {e["stage_key"] for e in _events(env, "campaign.stage_waiting_paused")} >= {"two"}
    assert len(_events(env, "campaign.resumed")) == 1
    assert len(env.query("SELECT 1 FROM campaign_approvals")) >= 1
    assert env.query("SELECT 1 FROM campaign_receipts WHERE campaign_id=?", next(iter(paused)))
    assert env.query("SELECT * FROM campaign_unlock_evaluations WHERE campaign_id=?", state["b"])
    # Every agent act was a tool call, and the agent has no tool that records a gate go.
    assert {
        "tessera_submit_campaign",
        "tessera_pause_campaign",
        "tessera_resume_campaign",
        "tessera_campaign_report",
        "tessera_campaign_gate_decisions",
        "tessera_get_campaign_receipt",
    } <= set(agent.calls)
    assert not [name for name in agent.calls if "gate_go" in name or "record_gate" in name]


def test_without_a_gate_the_step_reads_and_records_none(tmp_path):
    sched, agent, person, agent_approve = _rig(tmp_path)
    world, env = sched.world, sched.env
    spec = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))
    document = check.run_running_check(
        agent,
        person,
        agent_approve,
        image=IMAGE,
        snapshot_id=env.snapshot_id,
        spec=spec,
        advance=_advance(world),
        held_polls=2,
        wait_seconds=3600.0,
    )
    gate = {s["step"]: s for s in document["steps"]}["gate_decision"]["evidence"]
    assert gate == {"decisions": 0, "gate_configured": False}


def test_a_stage_that_finishes_before_the_pause_fails_loudly(tmp_path):
    sched, agent, person, agent_approve = _rig(tmp_path)
    world, env = sched.world, sched.env
    spec = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))

    def fast(phase):  # a very fast stage: it is done before the check can look at it
        for _ in range(60):
            world.runner.tick()
            world.poller.run(max_cycles=1)

    with pytest.raises(smoke.SmokeFailure, match="finished before it could be paused"):
        check.run_running_check(
            agent,
            person,
            agent_approve,
            image=IMAGE,
            snapshot_id=env.snapshot_id,
            spec=spec,
            advance=fast,
        )


def test_a_campaign_that_never_starts_fails_loudly(tmp_path):
    sched, agent, person, agent_approve = _rig(tmp_path)
    env = sched.env
    spec = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))
    with pytest.raises(smoke.SmokeFailure, match="never became live"):
        check.run_running_check(
            agent,
            person,
            agent_approve,
            image=IMAGE,
            snapshot_id=env.snapshot_id,
            spec=spec,
            advance=lambda phase: None,  # nothing advances: no runner
            wait_seconds=0.0,
        )


# ---- the agent_cannot_approve step asserts a SPECIFIC refusal, not "any exception" ----------

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from tessera_mcp.client import ControlError  # noqa: E402

DENIED = "Control API denied or rejected request (HTTP {})"


def _wrapped(text):
    error = ToolError(f"Error executing tool tessera_approve_campaign: {text}")
    error.__cause__ = ControlError(text)
    return error


@pytest.mark.parametrize(
    "error",
    [ControlError(DENIED.format(403)), ToolError(DENIED.format(403)), _wrapped(DENIED.format(403))],
)
def test_a_control_403_denial_is_labelled_http_403(error):
    assert smoke.agent_approval_refusal(error) == "http_403"


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("connection reset"),
        TimeoutError("timed out"),
        ControlError(DENIED.format(401)),
        _wrapped(DENIED.format(404)),
        ControlError("Refused (HTTP 409): stale"),
        ControlError(DENIED.format(500)),
        ValueError(DENIED.format(403)),  # the right words from the wrong kind of error
        ValueError("bad"),
        KeyError("spec_digest"),
    ],
)
def test_anything_other_than_a_403_denial_is_not_a_refusal(error):
    assert smoke.agent_approval_refusal(error) is None


def test_the_step_records_the_label_and_class_only(tmp_path):
    sched, agent, person, agent_approve = _rig(tmp_path)
    env = sched.env
    spec = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))
    document = check.run_running_check(
        agent, person, agent_approve, image=IMAGE, snapshot_id=env.snapshot_id, spec=spec,
        advance=_advance(sched.world), held_polls=2, wait_seconds=3600.0,
    )  # fmt: skip
    evidence = {s["step"]: s for s in document["steps"]}["agent_cannot_approve"]["evidence"]
    assert evidence["refused_with"] == "http_403" and evidence["error_class"] == "ToolError"


@pytest.mark.parametrize("error", [ConnectionError("boom"), TimeoutError("t"), ValueError("v")])
def test_a_failed_approval_attempt_is_not_counted_as_a_refusal(tmp_path, error):
    sched, agent, person, _ = _rig(tmp_path)
    env = sched.env
    spec = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))

    def broken(*_a, **_k):
        raise error

    with pytest.raises(smoke.SmokeFailure, match="other than a refusal") as caught:
        check.run_running_check(
            agent, person, broken, image=IMAGE, snapshot_id=env.snapshot_id, spec=spec,
            advance=lambda phase: None,
        )  # fmt: skip
    assert "boom" not in str(caught.value)


def test_an_approval_that_succeeds_for_the_agent_still_fails(tmp_path):
    sched, agent, person, _ = _rig(tmp_path)
    env = sched.env
    spec = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))
    with pytest.raises(smoke.SmokeFailure, match="was allowed to approve"):
        check.run_running_check(
            agent, person, lambda *_a, **_k: {"ok": True}, image=IMAGE,
            snapshot_id=env.snapshot_id, spec=spec, advance=lambda phase: None,
        )  # fmt: skip


# ---- main(): the live --execute path, with no network and no control plane -----------------


def _live_args(tmp_path, *extra, confirm=check.CONFIRM_PHRASE, mode=0o600):
    files = {}
    for name in ("agent", "person"):
        path = tmp_path / f"live-{name}.token"
        path.write_text(f"secret-live-{name}-token-value")
        path.chmod(mode)
        files[name] = path
    return [
        "--execute", "--confirm", confirm, "--control-url", "https://control.example",
        "--project-id", "p", "--destination", "d", "--image", IMAGE,
        "--agent-token-file", str(files["agent"]), "--person-token-file", str(files["person"]),
        "--held-polls", "2", "--wait-seconds", "3600", *extra,
    ]  # fmt: skip


def _fake_live(monkeypatch, tmp_path, *, approve=None):
    """Wire main() to in-process callers: _build_caller hands out the rig's fakes by flags."""
    sched, agent, person, agent_approve = _rig(tmp_path)
    env = sched.env
    built = []

    def build(arguments, token_file, **flags):
        built.append((token_file.name, sorted(flags)))
        if flags.get("campaign_tools"):
            return agent
        return person if token_file.name == "live-person.token" else (approve or agent_approve)

    spec = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))
    advance = _advance(sched.world)
    monkeypatch.setattr(smoke, "_build_caller", build)
    monkeypatch.setattr(check, "running_spec", lambda *a, **k: spec)
    monkeypatch.setattr(check.time, "sleep", lambda _s: advance("running_or_later"))
    return built, env


def _ran_nothing(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("must not build a caller or run")

    monkeypatch.setattr(smoke, "_build_caller", boom)
    monkeypatch.setattr(check, "run_running_check", boom)


@pytest.mark.parametrize("confirm", [None, "yes"])
def test_main_refuses_a_missing_or_wrong_confirmation_and_runs_nothing(
    tmp_path, capsys, monkeypatch, confirm
):
    _ran_nothing(monkeypatch)
    args = _live_args(tmp_path, confirm=confirm or "")
    if confirm is None:
        i = args.index("--confirm")
        del args[i : i + 2]
    assert check.main(args) == 2
    captured = capsys.readouterr()
    assert "refused: --execute needs --confirm" in captured.err and captured.out == ""


def test_main_refuses_group_or_world_readable_token_files_and_relative_paths(tmp_path, monkeypatch):
    _ran_nothing(monkeypatch)
    with pytest.raises(SystemExit, match="owner-only"):
        check.main(_live_args(tmp_path, mode=0o640))
    with pytest.raises(SystemExit, match="owner-only"):
        check.main(_live_args(tmp_path, mode=0o604))
    args = _live_args(tmp_path)
    args[args.index("--agent-token-file") + 1] = "relative.token"
    with pytest.raises(SystemExit, match="absolute path"):
        check.main(args)


def test_main_runs_and_prints_the_report_without_secrets(tmp_path, capsys, monkeypatch):
    built, _env = _fake_live(monkeypatch, tmp_path)
    assert check.main(_live_args(tmp_path)) == 0
    out = capsys.readouterr().out
    document = json.loads(out)
    assert document["schema"] == check.REPORT_SCHEMA and document["ok"] is True
    assert [s["step"] for s in document["steps"]] == [name for name, _ in check.STEPS]
    assert all(s["ok"] for s in document["steps"])
    assert "secret-live" not in out and str(tmp_path) not in out and ".token" not in out
    # three callers: the agent, the agent's approve-only server, and the person's.
    assert [name for name, _ in built] == [
        "live-agent.token", "live-agent.token", "live-person.token"
    ]  # fmt: skip


def test_main_writes_the_receipt_0600_and_never_overwrites(tmp_path, capsys, monkeypatch):
    _fake_live(monkeypatch, tmp_path)
    receipt = tmp_path / "receipt.json"
    assert check.main(_live_args(tmp_path, "--receipt-file", str(receipt))) == 0
    assert receipt.stat().st_mode & 0o777 == 0o600
    written = json.loads(receipt.read_text())
    assert written == json.loads(capsys.readouterr().out)
    assert "secret-live" not in receipt.read_text() and str(tmp_path) not in receipt.read_text()
    before = receipt.read_text()
    with pytest.raises(FileExistsError):
        check.main(_live_args(tmp_path, "--receipt-file", str(receipt)))
    assert receipt.read_text() == before


def test_main_fails_when_the_approval_attempt_dies_and_reports_no_secret(
    tmp_path, capsys, monkeypatch
):
    def dropped(*_a, **_k):
        raise ConnectionError("secret-live-agent-token-value dropped")

    _fake_live(monkeypatch, tmp_path, approve=dropped)
    receipt = tmp_path / "receipt.json"
    assert check.main(_live_args(tmp_path, "--receipt-file", str(receipt))) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and not receipt.exists()
    failure = json.loads(captured.err)
    assert failure["ok"] is False and "other than a refusal" in failure["failed"]
    assert "secret-live" not in captured.err and str(tmp_path) not in captured.err
    assert "ConnectionError" in failure["failed"]


def test_dry_run_builds_no_caller_and_reads_no_credential(tmp_path, capsys, monkeypatch):
    _ran_nothing(monkeypatch)
    missing = str(tmp_path / "does-not-exist.token")
    assert check.main(["--agent-token-file", missing, "--person-token-file", missing]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"


def _execute_argv(tmp_path, env, *extra):
    tokens = []
    for name in ("agent", "person"):
        path = tmp_path / f"{name}.token"
        path.write_text("synthetic-token-" + "x" * 30)
        path.chmod(0o600)
        tokens.append(str(path))
    return [
        "--execute",
        "--confirm",
        check.CONFIRM_PHRASE,
        "--control-url",
        "http://127.0.0.1:8765",
        "--project-id",
        env.project["id"],
        "--destination",
        "planner.local",
        "--agent-token-file",
        tokens[0],
        "--person-token-file",
        tokens[1],
        "--image",
        IMAGE,
        "--snapshot-id",
        env.snapshot_id,
        "--held-polls",
        "2",
        *extra,
    ]


def _wire_main(monkeypatch, rig, *, expire_when_held=False):
    """Point main()'s MCP callers at the in-process rig; nothing live is contacted."""
    sched, agent, person, agent_approve = rig
    env = sched.env
    spec = _spec("running-check", _stage(env, "one"), _stage(env, "two", upstream="one"))
    callers = iter([agent, agent_approve, person])
    monkeypatch.setattr(smoke, "_build_caller", lambda *a, **k: next(callers))
    advance = _advance(sched.world)
    expired = [False]

    def fast_forward(phase):
        advance(phase)
        expired[0] = expired[0] or (expire_when_held and phase == "held")

    real = check.run_running_check

    def run(*args, **kwargs):
        kwargs.update(
            spec=spec,
            advance=fast_forward,
            wait_seconds=3600.0,
            clock=lambda: time.monotonic() + (1e7 if expired[0] else 0.0),
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(check, "run_running_check", run)


def test_main_execute_runs_the_check_and_prints_the_report(tmp_path, monkeypatch, capsys):
    rig = _rig(tmp_path)
    _wire_main(monkeypatch, rig)
    receipt = tmp_path / "report.json"
    assert check.main(_execute_argv(tmp_path, rig[0].env, "--receipt-file", str(receipt))) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True and document["schema"] == check.REPORT_SCHEMA
    assert [s["step"] for s in document["steps"]] == [name for name, _ in check.STEPS]
    assert json.loads(receipt.read_text()) == document


def test_a_failed_run_names_its_campaign_and_the_steps_completed(tmp_path, monkeypatch, capsys):
    rig = _rig(tmp_path)
    _wire_main(monkeypatch, rig, expire_when_held=True)  # times out after the pause step
    assert check.main(_execute_argv(tmp_path, rig[0].env)) == 1
    failed = json.loads(capsys.readouterr().err)
    assert failed["ok"] is False and "did not finish while paused" in failed["failed"]
    assert failed["campaign_id"]
    steps = [name for name, _ in check.STEPS]
    assert failed["completed_steps"] == steps[: steps.index("pause_while_running") + 1]
