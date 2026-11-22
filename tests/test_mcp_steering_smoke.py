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
        sleep=lambda _seconds: world.runner.tick(),
        poll_seconds=0,
    )
    steps = {s["step"]: s for s in document["steps"]}
    assert list(steps) == [name for name, _ in smoke.STEPS]
    assert all(s["ok"] for s in document["steps"])
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
