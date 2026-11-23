"""The $0 MCP steering smoke (tessera_mcp.steering_smoke): dry run by default, refusals, and a
full run against an in-process control plane with a fake worker. No network, no Docker."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("mcp")

from campaign_runner_harness import IMAGE  # noqa: E402
from test_campaign_standing import Sched  # noqa: E402

from tessera_control import campaign_provider_worker as worker  # noqa: E402
from tessera_control import campaign_providers as cp  # noqa: E402
from tessera_control.api import create_app  # noqa: E402
from tessera_mcp import steering_smoke as smoke  # noqa: E402
from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import CAMPAIGN_ACTIONS, create_server  # noqa: E402

DIGEST_IMAGE = IMAGE if "@sha256:" in IMAGE else IMAGE + "@sha256:" + "a" * 64
AGENT_ACTIONS = sorted(CAMPAIGN_ACTIONS | {"budget:read", "snapshot:read"})
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mcp_steering_smoke.py"


@pytest.fixture(autouse=True)
def _reset():
    yield
    worker.register_from_config(None)


def _caller(app, tmp_path, name, **flags):
    reference = tmp_path / f"{name}.token"
    reference.write_text(f"synthetic-{name}-token-" + "x" * 30)
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, PROJECT[0], "planner.local"),
        httpx.ASGITransport(app=app),
    )
    server = create_server(client, **flags)

    def call(tool, **arguments):
        result = asyncio.run(server.call_tool(tool, arguments))
        return result[1] if isinstance(result, tuple) else result

    return call


PROJECT = [""]


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
    agent = _caller(app, tmp_path, "steerer", campaign_tools=True,
                    campaign_actions=frozenset(AGENT_ACTIONS))  # fmt: skip
    agent_approve = _caller(app, tmp_path, "steerer", approval_tool=True)
    person = _caller(app, tmp_path, "human", approval_tool=True)
    return sched, agent, person, agent_approve


def test_dry_run_is_the_default_and_touches_nothing(tmp_path, capsys):
    # No network and no credential file: a dry run only prints the plan and the spec.
    assert smoke.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "dry-run" and plan["provider"] == "local-docker"
    assert [s["step"] for s in plan["steps"]] == [name for name, _ in smoke.STEPS]
    assert plan["spec"]["stages"][0]["provider"] == "local-docker"
    done = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0 and json.loads(done.stdout)["mode"] == "dry-run"


@pytest.mark.parametrize(
    "extra",
    [
        ["--execute"],  # no confirm phrase
        ["--execute", "--confirm", "yes"],  # wrong phrase
        ["--execute", "--confirm", smoke.CONFIRM_PHRASE],  # no connection options
    ],
)
def test_execute_needs_the_exact_confirmation_and_the_options(extra, capsys):
    assert smoke.main(extra) == 2
    assert "refused" in capsys.readouterr().err


def test_execute_refuses_an_unpinned_image_and_loose_credential_files(tmp_path, capsys):
    token = tmp_path / "t"
    token.write_text("x")
    base = [
        "--execute", "--confirm", smoke.CONFIRM_PHRASE, "--control-url", "https://control.example",
        "--project-id", "p", "--destination", "d", "--agent-token-file", str(token),
        "--person-token-file", str(token),
    ]  # fmt: skip
    assert smoke.main([*base, "--image", "registry.example/img:latest"]) == 2
    assert "digest-pinned" in capsys.readouterr().err
    token.chmod(0o644)
    with pytest.raises(SystemExit):
        smoke.main([*base, "--image", "registry.example/img@sha256:" + "a" * 64])


def test_smoke_steers_one_campaign_and_every_step_leaves_a_record(tmp_path):
    sched, agent, person, agent_approve = _rig(tmp_path)
    world = sched.world
    document = smoke.run_smoke(
        agent,
        person,
        agent_approve,
        image=DIGEST_IMAGE,
        snapshot_id=sched.env.snapshot_id,
        grant_id="g-1",
        sleep=lambda _seconds: world.runner.tick(),
        poll_seconds=0,
    )
    steps = {s["step"]: s for s in document["steps"]}
    assert list(steps) == [name for name, _ in smoke.STEPS]
    assert all(s["ok"] for s in document["steps"])
    assert document["smoke_grant_id"] == "g-1"
    assert steps["agent_cannot_approve"]["evidence"]["refused_with"]
    assert steps["terminal_receipt"]["evidence"]["status"] in smoke.TERMINAL
    assert steps["terminal_receipt"]["evidence"]["settled_cents"] == 0  # $0
    assert steps["terminal_receipt"]["evidence"]["pause_count"] >= 1
    # Control's own records agree with the report: one campaign, one approval (a person's),
    # a terminal receipt row, and nothing settled.
    env = sched.env
    assert len(env.query("SELECT 1 FROM campaigns")) == 1
    assert len(env.query("SELECT 1 FROM campaign_approvals")) == 1
    assert len(env.query("SELECT 1 FROM campaign_receipts")) == 1
    types = {r["event_type"] for r in env.query("SELECT event_type FROM events")}
    assert {"campaign.submitted", "campaign.paused", "campaign.resumed"} <= types
    assert any(t.startswith("campaign.cancel") for t in types)


def test_smoke_fails_loudly_when_the_campaign_never_finishes(tmp_path):
    sched, agent, person, agent_approve = _rig(tmp_path)
    ticks = []
    with pytest.raises(smoke.SmokeFailure, match="did not reach a terminal state"):
        smoke.run_smoke(
            agent,
            person,
            agent_approve,
            image=DIGEST_IMAGE,
            snapshot_id=sched.env.snapshot_id,
            wait_seconds=0.0,
            sleep=ticks.append,
            poll_seconds=0,
        )


def _grant_dir(tmp_path, **changes):
    from datetime import UTC, datetime, timedelta

    root = tmp_path / "grant"
    root.mkdir(mode=0o700)
    grant = {
        "schema": "tessera.smoke-credentials.v1",
        "grant_id": "grant-1",
        "project_id": "proj-1",
        "destination": "dest-1",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        "agent_credential_id": "a1",
        "approver_credential_id": "p1",
        "providers": ["local-docker"],
        "max_budget_cents": 0,
        "control_url": None,
        **changes,
    }
    for name, text in (
        ("smoke-grant.json", json.dumps(grant)),
        ("agent.token", "synthetic-agent"),
        ("approver.token", "synthetic-approver"),
    ):
        (root / name).write_text(text)
        (root / name).chmod(0o600)
    return root


def _grant_args(root, *extra):
    return [
        "--execute", "--confirm", smoke.CONFIRM_PHRASE, "--control-url", "https://control.example",
        "--smoke-credentials-dir", str(root), "--image", "r.example/i@sha256:" + "a" * 64, *extra,
    ]  # fmt: skip


def test_grant_directory_fills_the_options(tmp_path):
    root = _grant_dir(tmp_path)
    grant = smoke.load_grant(str(root))
    assert (grant.grant_id, grant.project_id, grant.destination) == ("grant-1", "proj-1", "dest-1")
    assert grant.agent_token_file == root / "agent.token"
    assert grant.approver_token_file == root / "approver.token"


def test_grant_snapshot_ids_are_loaded_and_optional(tmp_path):
    assert smoke.load_grant(str(_grant_dir(tmp_path))).snapshot_id is None
    (tmp_path / "x").mkdir()
    root = _grant_dir(tmp_path / "x", snapshot_id="snapshot_1", snapshot_digest="d" * 64)
    grant = smoke.load_grant(str(root))
    assert (grant.snapshot_id, grant.snapshot_digest) == ("snapshot_1", "d" * 64)


def test_grant_with_half_a_snapshot_is_refused(tmp_path, capsys):
    root = _grant_dir(tmp_path, snapshot_id="snapshot_1")
    assert smoke.main(_grant_args(root)) == 2
    assert "refused" in capsys.readouterr().err


def test_smoke_uses_the_grants_snapshot_unless_one_is_given(tmp_path, monkeypatch):
    root = _grant_dir(tmp_path, snapshot_id="snapshot_g", snapshot_digest="d" * 64)
    seen = []
    monkeypatch.setattr(
        smoke, "run_smoke", lambda *a, **kw: seen.append(kw) or {"ok": True, "steps": []}
    )
    monkeypatch.setattr(smoke, "_build_caller", lambda *a, **kw: None)
    assert smoke.main(_grant_args(root)) == 0
    assert (seen[0]["snapshot_id"], seen[0]["snapshot_digest"]) == ("snapshot_g", "d" * 64)
    assert smoke.main(_grant_args(root, "--snapshot-id", "snapshot_x")) == 0
    assert (seen[1]["snapshot_id"], seen[1]["snapshot_digest"]) == ("snapshot_x", None)


def _listing(data_class, digest="d" * 64):
    return {"snapshots": [{"id": "snapshot_g", "digest": digest, "data_class": data_class}]}


@pytest.mark.parametrize(
    ("data_class", "digest", "fails"),
    [
        ("public", "d" * 64, False),
        ({"data_class": "public"}, "d" * 64, False),
        ("private_derived", "d" * 64, True),
        ("public", "e" * 64, True),
    ],
)
def test_smoke_refuses_a_catalog_that_does_not_show_the_grants_snapshot_public(
    data_class, digest, fails
):
    calls = []

    def agent(name, **_kwargs):
        calls.append(name)
        if name == "tessera_list_snapshots":
            return _listing(data_class, digest)
        return {"actions": []}

    kwargs = {"image": "r.example/i@sha256:" + "a" * 64, "snapshot_id": "snapshot_g"}
    if fails:
        with pytest.raises(smoke.SmokeFailure, match="grant's snapshot"):
            smoke.run_smoke(agent, agent, agent, snapshot_digest="d" * 64, **kwargs)
        assert "tessera_validate_campaign_spec" not in calls
    else:
        with pytest.raises(Exception) as caught:  # passes the check, then the stub stops it
            smoke.run_smoke(agent, agent, agent, snapshot_digest="d" * 64, **kwargs)
        assert "grant's snapshot" not in str(caught.value)


def test_grant_dry_run_prints_id_and_expiry_but_no_token(tmp_path, capsys):
    root = _grant_dir(tmp_path)
    assert smoke.main(["--smoke-credentials-dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["smoke_grant"]["grant_id"] == "grant-1"
    assert "expires_at" in json.loads(out)["smoke_grant"]
    assert "synthetic-agent" not in out and "synthetic-approver" not in out


def test_grant_fills_execute_options_and_explicit_ones_win(tmp_path, monkeypatch):
    root = _grant_dir(tmp_path)
    seen = {}

    def fake_run(agent, person, agent_approve, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "smoke_grant_id": kwargs["grant_id"], "steps": []}

    built = []
    monkeypatch.setattr(smoke, "run_smoke", fake_run)
    monkeypatch.setattr(
        smoke, "_build_caller", lambda a, f, **_: built.append((a.project_id, a.destination, f))
    )
    assert smoke.main(_grant_args(root, "--destination", "other")) == 0
    assert seen["grant_id"] == "grant-1"
    assert built[0] == ("proj-1", "other", root / "agent.token")
    assert built[2][2] == root / "approver.token"


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": "2000-01-01T00:00:00+00:00"},
        {"providers": ["local-docker", "salad"]},
        {"providers": []},
        {"max_budget_cents": 1},
        {"schema": "other.v1"},
        {"grant_id": ""},
    ],
)
def test_bad_grants_are_refused(tmp_path, capsys, changes):
    root = _grant_dir(tmp_path, **changes)
    assert smoke.main(_grant_args(root)) == 2
    assert "refused" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["smoke-grant.json", "agent.token", ".", "approver.token"])
def test_permissive_modes_are_refused(tmp_path, capsys, target):
    root = _grant_dir(tmp_path)
    (root / target).chmod(0o755 if target == "." else 0o644)
    assert smoke.main(_grant_args(root)) == 2
    assert "refused" in capsys.readouterr().err


def test_smoke_spec_is_zero_dollars():
    spec = smoke.smoke_spec("r.example/i@sha256:" + "a" * 64, {"id": "s", "digest": "d"})
    assert spec["budget_cap_cents"] == 0
    assert all(
        s["max_exposure_cents"] == 0 and s["provider"] == "local-docker" for s in spec["stages"]
    )


def test_report_carries_the_grant_id():
    assert smoke.Report(grant_id="g-9").document()["smoke_grant_id"] == "g-9"
    assert smoke.Report().document()["smoke_grant_id"] is None


# ---- the agent_cannot_approve step asserts a SPECIFIC refusal, not "any exception" ----------

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from tessera_mcp.client import ControlError  # noqa: E402


@pytest.mark.parametrize("status,label", [(403, "http_403"), (401, None), (404, None), (500, None)])
def test_only_a_control_403_denial_is_labelled_a_refusal(status, label):
    text = f"Control API denied or rejected request (HTTP {status})"
    assert smoke.agent_approval_refusal(ControlError(text)) == label
    assert smoke.agent_approval_refusal(ToolError(text)) == label
    wrapped = ToolError(f"Error executing tool tessera_approve_campaign: {text}")
    wrapped.__cause__ = ControlError(text)
    assert smoke.agent_approval_refusal(wrapped) == label


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("x"),
        TimeoutError("x"),
        ControlError("Refused (HTTP 409): stale"),
        ValueError("Control API denied or rejected request (HTTP 403)"),
        KeyError("k"),
    ],
)
def test_other_failures_are_not_refusals(error):
    assert smoke.agent_approval_refusal(error) is None


def test_smoke_records_the_403_label_and_class(tmp_path):
    sched, agent, person, agent_approve = _rig(tmp_path)
    document = smoke.run_smoke(
        agent, person, agent_approve, image=DIGEST_IMAGE, snapshot_id=sched.env.snapshot_id,
        sleep=lambda _s: sched.world.runner.tick(), poll_seconds=0,
    )  # fmt: skip
    evidence = {s["step"]: s for s in document["steps"]}["agent_cannot_approve"]["evidence"]
    assert evidence["refused_with"] == "http_403" and evidence["error_class"] == "ToolError"


@pytest.mark.parametrize("error", [ConnectionError("boom"), TimeoutError("t"), ValueError("v")])
def test_smoke_fails_when_the_approval_attempt_fails_for_another_reason(tmp_path, error):
    sched, agent, person, _ = _rig(tmp_path)

    def broken(*_a, **_k):
        raise error

    with pytest.raises(smoke.SmokeFailure, match="other than a refusal") as caught:
        smoke.run_smoke(
            agent, person, broken, image=DIGEST_IMAGE, snapshot_id=sched.env.snapshot_id,
            sleep=lambda _s: None, poll_seconds=0,
        )  # fmt: skip
    assert "boom" not in str(caught.value)


def test_smoke_still_fails_when_the_agent_is_allowed_to_approve(tmp_path):
    sched, agent, person, _ = _rig(tmp_path)
    with pytest.raises(smoke.SmokeFailure, match="was allowed to approve"):
        smoke.run_smoke(
            agent, person, lambda *_a, **_k: {"ok": True}, image=DIGEST_IMAGE,
            snapshot_id=sched.env.snapshot_id, sleep=lambda _s: None, poll_seconds=0,
        )  # fmt: skip


# ---- main(): the live --execute path with in-process callers, no network --------------------


def _live_args(tmp_path, *extra, confirm=smoke.CONFIRM_PHRASE, mode=0o600):
    files = {}
    for name in ("agent", "person"):
        path = tmp_path / f"live-{name}.token"
        path.write_text(f"secret-live-{name}-token-value")
        path.chmod(mode)
        files[name] = path
    return [
        "--execute", "--confirm", confirm, "--control-url", "https://control.example",
        "--project-id", "p", "--destination", "d", "--image", DIGEST_IMAGE,
        "--agent-token-file", str(files["agent"]), "--person-token-file", str(files["person"]),
        *extra,
    ]  # fmt: skip


def _fake_live(monkeypatch, tmp_path, *, approve=None):
    sched, agent, person, agent_approve = _rig(tmp_path)
    built = []

    def build(arguments, token_file, **flags):
        built.append(token_file.name)
        if flags.get("campaign_tools"):
            return agent
        return person if token_file.name == "live-person.token" else (approve or agent_approve)

    real = smoke.run_smoke

    def run(*a, **kw):
        return real(*a, **{**kw, "sleep": lambda _s: sched.world.runner.tick(), "poll_seconds": 0})

    monkeypatch.setattr(smoke, "_build_caller", build)
    monkeypatch.setattr(smoke, "run_smoke", run)
    monkeypatch.setattr(
        smoke, "pick_snapshot", lambda listing, wanted: real_pick(listing, sched.env.snapshot_id)
    )
    return built


real_pick = smoke.pick_snapshot


def _ran_nothing(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("must not build a caller or run")

    monkeypatch.setattr(smoke, "_build_caller", boom)
    monkeypatch.setattr(smoke, "run_smoke", boom)


@pytest.mark.parametrize("confirm", [None, "yes"])
def test_main_refuses_a_missing_or_wrong_confirmation_and_runs_nothing(
    tmp_path, capsys, monkeypatch, confirm
):
    _ran_nothing(monkeypatch)
    args = _live_args(tmp_path, confirm=confirm or "")
    if confirm is None:
        i = args.index("--confirm")
        del args[i : i + 2]
    assert smoke.main(args) == 2
    captured = capsys.readouterr()
    assert "refused: --execute needs --confirm" in captured.err and captured.out == ""


def test_main_refuses_loose_token_files_and_relative_paths(tmp_path, monkeypatch):
    _ran_nothing(monkeypatch)
    with pytest.raises(SystemExit, match="owner-only"):
        smoke.main(_live_args(tmp_path, mode=0o640))
    args = _live_args(tmp_path)
    args[args.index("--person-token-file") + 1] = "relative.token"
    with pytest.raises(SystemExit, match="absolute path"):
        smoke.main(args)


def test_main_runs_prints_the_report_and_writes_the_receipt_0600(tmp_path, capsys, monkeypatch):
    built = _fake_live(monkeypatch, tmp_path)
    receipt = tmp_path / "receipt.json"
    assert smoke.main(_live_args(tmp_path, "--receipt-file", str(receipt))) == 0
    out = capsys.readouterr().out
    document = json.loads(out)
    assert document["schema"] == "tessera.mcp-steering-smoke.v1" and document["ok"] is True
    assert [s["step"] for s in document["steps"]] == [name for name, _ in smoke.STEPS]
    assert "secret-live" not in out and str(tmp_path) not in out and ".token" not in out
    assert built == ["live-agent.token", "live-agent.token", "live-person.token"]
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text()) == document
    with pytest.raises(FileExistsError):
        smoke.main(_live_args(tmp_path, "--receipt-file", str(receipt)))


def test_main_fails_when_the_approval_attempt_dies_and_reports_no_secret(
    tmp_path, capsys, monkeypatch
):
    def dropped(*_a, **_k):
        raise ConnectionError("secret-live-agent-token-value dropped")

    _fake_live(monkeypatch, tmp_path, approve=dropped)
    receipt = tmp_path / "receipt.json"
    assert smoke.main(_live_args(tmp_path, "--receipt-file", str(receipt))) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and not receipt.exists()
    failure = json.loads(captured.err)
    assert failure["ok"] is False and "other than a refusal" in failure["failed"]
    assert "secret-live" not in captured.err and str(tmp_path) not in captured.err
