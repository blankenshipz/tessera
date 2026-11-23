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
