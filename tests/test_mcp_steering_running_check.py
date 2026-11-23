"""The running-campaign steering check (tessera_mcp.steering_running_check): dry run by default,
refusals, and a full run against an in-process control plane with a fake runner and worker: an
agent steers a RUNNING two-stage campaign over MCP tools only (submit, report, pause while a stage
runs, resume, a gate decision) and every step leaves a record. No network, no Docker."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
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
