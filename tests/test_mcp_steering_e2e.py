"""An agent steers a whole multi-stage campaign over tessera_mcp only (ADR 0055, ADR 0051).

Real control app (in process, temp state), the real campaign runner and a fake worker: no network
and no Docker. Every agent step goes through the MCP tool functions and the control API over an
ASGI transport. The only human acts are the ones the design keeps human and the test marks
``HUMAN``: approving a submitted digest, creating the standing approval that carries the gate,
and recording the gate's go.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

pytest.importorskip("mcp")

from campaign_runner_harness import IMAGE, dump  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from test_campaign_gates import decide  # noqa: E402
from test_campaign_standing import Sched  # noqa: E402

from tessera_control import campaign_gates  # noqa: E402
from tessera_control import campaign_provider_worker as worker  # noqa: E402
from tessera_control import campaign_providers as cp  # noqa: E402
from tessera_control.api import create_app  # noqa: E402
from tessera_control.store import canonical_digest  # noqa: E402
from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import CAMPAIGN_ACTIONS, create_server  # noqa: E402

AGENT_ACTIONS = sorted(CAMPAIGN_ACTIONS | {"budget:read", "snapshot:read"})
METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
OUTPUT = [{"name": "result", "path": "data.json", "max_bytes": 1024}]


@pytest.fixture(autouse=True)
def _reset():
    yield
    worker.register_from_config(None)
    campaign_gates.configure_go_authority(accept_automation_labelled=False)


def _stage(env, key, upstream=None, cap=2):
    inputs = [
        {
            "kind": "snapshot",
            "name": "code",
            "snapshot_id": env.snapshot_id,
            "digest": env.snapshot_digest,
            "data_class": "public",
        }
    ]
    if upstream:
        inputs.append({"kind": "stage", "name": "up", "stage": upstream, "outputs": ["result"]})
    return {
        "key": key,
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": OUTPUT,
        "max_duration_seconds": 60,
        "max_exposure_cents": cap,
        "inputs": inputs,
        "metrics": METRIC,
    }


def _spec(name, *stages, cap=10):
    return {
        "version": 2,
        "name": name,
        "budget_cap_cents": cap,
        "deadline_seconds": 3600,
        "stages": list(stages),
    }


class Steer:
    """One enrolled agent: every call is an MCP tool call over the in-process control app."""

    def __init__(self, sched: Sched, tmp_path) -> None:
        self.sched = sched
        env = sched.env
        env.agent(AGENT_ACTIONS, "steerer")
        reference = tmp_path / "agent.token"
        reference.write_text("synthetic-steerer-token-" + "x" * 30)
        reference.chmod(0o600)
        self.app = create_app(env.state)
        self.client = ControlClient(
            Config("http://127.0.0.1:8765", reference, env.project["id"], "planner.local"),
            httpx.ASGITransport(app=self.app),
        )
        self.server = create_server(
            self.client, campaign_tools=True, campaign_actions=frozenset(AGENT_ACTIONS)
        )
        self.calls: list[str] = []

    def __call__(self, name, **arguments):
        self.calls.append(name)
        result = asyncio.run(self.server.call_tool(name, arguments))
        return result[1] if isinstance(result, tuple) else result

    def revision(self, campaign_id) -> int:
        return self(  # noqa: PLW2901
            "tessera_get_campaign", campaign_id=campaign_id
        )["revision"]


def _events(env, prefix) -> list[dict]:
    rows = env.query("SELECT event_type, payload_json FROM events WHERE event_type LIKE ?", prefix)
    return [{"type": r["event_type"], **json.loads(r["payload_json"])} for r in rows]


def _standing(sched, spec, unlock) -> dict:
    """HUMAN: create a standing approval for a v2 spec (Sched.body only digests v1 specs)."""
    from datetime import UTC, datetime, timedelta

    from tessera_control.campaign_models import parse_spec

    body = {
        "spec": spec,
        "expected_spec_digest": canonical_digest(parse_spec(spec).model_dump(mode="json")),
        "per_start_budget_cap_cents": 5,
        "max_starts": 1,
        "max_total_budget_cents": 5,
        "not_after": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        "unlock": unlock,
    }
    code, result = sched.env.store.create_standing_approval(
        sched.env.human(), sched.env.project["id"], "standing-b", canonical_digest(body), body
    )
    assert code == 201
    return result


def _human_approve(env, campaign):
    """HUMAN: a person approves the submitted digest; no MCP tool can."""
    return env.approve({"id": campaign["id"], **_read(env, campaign["id"])})


def _read(env, campaign_id):
    row = env.query("SELECT revision, spec_digest FROM campaigns WHERE id=?", campaign_id)[0]
    return {"revision": row["revision"], "spec_digest": row["spec_digest"]}


def test_an_agent_steers_a_three_stage_campaign_with_a_gate_over_mcp_only(tmp_path):
    from provider_trust_fixture import DERIVED, install_trust

    sched = Sched(tmp_path)
    world, env = sched.world, sched.env
    cp.configure_providers({"local_docker": {"worker_principal_id": env.worker["id"]}})
    install_trust({"local-docker": (DERIVED, [env.worker["id"]])})
    steer = Steer(sched, tmp_path)

    # -- 1. validate and submit stages 1 and 2 (campaign A) -------------------------------------
    spec_a = _spec("chem-stages-1-2", _stage(env, "s1"), _stage(env, "s2", upstream="s1"))
    checked = steer("tessera_validate_campaign_spec", spec=spec_a)
    assert checked and not env.query("SELECT 1 FROM campaigns")  # a dry run stores nothing
    submitted = steer("tessera_submit_campaign", spec=spec_a, idempotency_key="submit-a-1")
    a = submitted["id"]
    again = steer("tessera_submit_campaign", spec=spec_a, idempotency_key="submit-a-1")
    assert again["id"] == a and len(env.query("SELECT 1 FROM campaigns")) == 1  # idempotent
    assert _events(env, "campaign.submitted")  # journal evidence of the submit

    # -- 2. the gated third stage (campaign B): a standing approval whose unlock reads A --------
    # HUMAN (once): the operator fixes stage 3, its cap and the unlock condition. The condition
    # is a measurement on A's terminal receipt, narrowing a gate go.
    unlock = [
        {
            "campaign_id": a,
            "accept_human_go": True,
            "measurement": {"stage": "s2", "name": "score", "op": "ge", "value": 2},
        }
    ]
    spec_b = _spec("chem-stage-3", _stage(env, "s3", cap=3), cap=5)
    standing = _standing(sched, spec_b, unlock)

    # -- 3. HUMAN approves A's digest; from here Tessera's own loop advances stages 1 and 2 -----
    approvals_before = len(env.query("SELECT 1 FROM campaign_approvals"))
    assert _human_approve(env, submitted)
    human_after_approval = len(env.query("SELECT 1 FROM campaign_approvals"))
    assert human_after_approval == approvals_before + 1
    world.tick_until(lambda: world.stages(a).get("s1") == "succeeded")
    assert world.stages(a)["s2"] != "succeeded"  # stage 2 waits for stage 1's output
    world.tick_all(a)
    assert world.stages(a) == {"s1": "succeeded", "s2": "succeeded"}, dump(world, a)
    assert len(env.query("SELECT 1 FROM campaign_approvals")) == human_after_approval  # no operator
    intents = env.intents(a)  # the runner's own journal: every step of both stages, delegated
    assert {"issue_grant", "release_snapshot", "derive_measurement", "reconcile_grant"} <= {
        r["step"] for r in intents
    }
    assert all(r["status"] == "done" for r in intents), [dict(r) for r in intents]

    # -- 4. the agent reads grants and the receipt (journal evidence of what ran) ---------------
    grants = steer("tessera_campaign_grants", campaign_id=a)
    assert {g["stage_key"] for g in grants["grants"]} == {"s1", "s2"}
    assert all(g["grant_status"] == "reconciled" for g in grants["grants"])
    receipt = steer("tessera_get_campaign_receipt", campaign_id=a)
    measured = {
        (s["stage_key"], m["name"]): m["value"]
        for s in receipt["receipt"]["stages"]
        for att in s["attempts"]
        for m in att["measurements"]
    }
    assert measured[("s2", "score")] == 3
    outputs = steer("tessera_campaign_outputs", campaign_id=a)["outputs"]
    assert {o["stage_key"] for o in outputs} == {"s1", "s2"}

    # -- 5. the gate: the receipt measurement holds, a go is still required ---------------------
    sched.tick()
    status = steer("tessera_campaign_standing_status")
    assert not sched.starts(standing["id"])
    assert any("no_human_go" in r for r in sched.refusals(standing["id"])), status
    # HUMAN (operator opt-in): the owner script's go counts only where the operator started the
    # service with --accept-automation-gate-go. No MCP tool records a go.
    campaign_gates.configure_go_authority(accept_automation_labelled=True)
    decide(sched, a, actor={**env.owner, "automation_label": "ops-script"})
    gate = steer("tessera_campaign_gate_decisions", campaign_id=a)
    assert gate["gate_decisions"][0]["decision_class"] == "owner_automation_labelled"
    sched.tick()
    (start,) = sched.starts(standing["id"])
    b = start["campaign_id"]
    assert steer("tessera_get_campaign", campaign_id=b)["status"] in {"running", "approved"}
    evaluations = env.query("SELECT * FROM campaign_unlock_evaluations WHERE campaign_id=?", b)
    assert evaluations  # the unlock evaluation is recorded evidence

    # -- 6. an injected failure in stage 3; the agent prepares a retry --------------------------
    world.scenarios.append((137, {"data.json": json.dumps({"score": 0})}))
    world.tick_all(b)
    assert world.stages(b)["s3"] == "failed", dump(world, b)
    logs = steer("tessera_campaign_logs", campaign_id=b)
    assert any(att.get("failure_class") for att in logs["attempts"])
    plan = steer("tessera_prepare_stage_retry", campaign_id=b)
    assert plan["retried_stages"] == ["s3"] and "nothing was submitted" in plan["note"]
    assert len(env.query("SELECT 1 FROM campaigns")) == 2  # A and B; the plan stored nothing
    retry_spec = plan["spec"]
    assert steer("tessera_validate_campaign_spec", spec=retry_spec)
    retry = steer("tessera_submit_campaign", spec=retry_spec, idempotency_key="submit-retry-1")
    r = retry["id"]
    assert _human_approve(env, retry)  # HUMAN: the retry spec is a new digest

    # -- 7. reduce the cap while it runs, then cancel and revoke at the end ---------------------
    world.tick_until(lambda: any(x["release_id"] for x in world.attempts(r)), worker=False)
    budget = steer("tessera_campaign_budget", campaign_id=r)
    held = budget["held_cents"]
    assert held > 0 and budget["budget_cap_cents"] > held
    with pytest.raises(ToolError):  # below held + settled is refused, and nothing changes
        steer(
            "tessera_reduce_campaign_budget",
            campaign_id=r,
            expected_revision=steer.revision(r),
            budget_cap_cents=held - 1,
            reason="too low",
            idempotency_key="reduce-low",
        )
    assert not _events(env, "campaign.budget_reduced")
    reduced = steer(
        "tessera_reduce_campaign_budget",
        campaign_id=r,
        expected_revision=steer.revision(r),
        budget_cap_cents=held,
        reason="agent lowers the cap to what is held",
        idempotency_key="reduce-1",
    )
    assert reduced["budget_cap_cents"] == held
    assert env.query("SELECT budget_cap_cents FROM campaigns WHERE id=?", r)[0][0] == held
    assert _events(env, "campaign.budget_reduced")
    cancelled = steer(
        "tessera_cancel_campaign",
        campaign_id=r,
        expected_revision=steer.revision(r),
        reason="agent stops the retry",
        idempotency_key="cancel-1",
    )
    assert cancelled["status"] in {"cancel_requested", "cancelled"}
    release = env.query("SELECT id, revision FROM snapshot_releases WHERE status='active'")
    assert release  # no runner tick since the cancel: the agent revokes the release itself
    revoked = steer(
        "tessera_revoke_campaign_release",
        campaign_id=r,
        release_id=release[0]["id"],
        expected_revision=release[0]["revision"],
        reason="agent revokes the stale release",
        idempotency_key="revoke-1",
    )
    assert revoked["status"] == "revoked"
    for _ in range(40):
        if world.status(r) == "cancelled":
            break
        world.runner.tick()
    assert world.status(r) == "cancelled", dump(world, r)
    assert not env.query("SELECT 1 FROM snapshot_releases WHERE status='active'")
    # Journal evidence for every state-changing step the agent took.
    mine = {e["campaign_id"] for e in _events(env, "campaign.submitted")}
    assert mine == {a, b, r}  # B's came from the standing start, not from an agent call
    assert steer.calls.count("tessera_submit_campaign") == 3  # A (twice, one key) and the retry
    assert [e["campaign_id"] for e in _events(env, "campaign.budget_reduced")] == [r]
    assert [e["campaign_id"] for e in _events(env, "campaign.cancel_requested")] == [r]
    assert release[0]["id"] in {
        e["release_id"] for e in _events(env, "workspace.snapshot_release_revoked")
    }
    assert {a, b} <= {row[0] for row in env.query("SELECT campaign_id FROM campaign_receipts")}

    # -- 8. nothing above needed a human session beyond the marked acts -------------------------
    assert {
        "tessera_validate_campaign_spec",
        "tessera_campaign_grants",
        "tessera_prepare_stage_retry",
        "tessera_reduce_campaign_budget",
        "tessera_cancel_campaign",
    } <= set(steer.calls)


def test_an_agent_pauses_limits_and_cancels_a_stage_over_mcp_only(tmp_path):
    from provider_trust_fixture import DERIVED, install_trust

    sched = Sched(tmp_path)
    world, env = sched.world, sched.env
    cp.configure_providers({"local_docker": {"worker_principal_id": env.worker["id"]}})
    install_trust({"local-docker": (DERIVED, [env.worker["id"]])})
    steer = Steer(sched, tmp_path)
    spec = _spec(
        "steered", _stage(env, "s1"), _stage(env, "s2", upstream="s1"), _stage(env, "s3"), cap=10
    )
    submitted = steer("tessera_submit_campaign", spec=spec, idempotency_key="submit-steered")
    c = submitted["id"]
    assert _human_approve(env, submitted)  # HUMAN: the digest

    def attempts():
        return world.attempts(c)

    # Lower-only concurrency: one stage at a time; raising it is refused.
    steer(
        "tessera_limit_campaign_concurrency",
        campaign_id=c,
        expected_revision=steer.revision(c),
        max_concurrent_stages=1,
        reason="one at a time",
        idempotency_key="limit-1",
    )
    with pytest.raises(ToolError):
        steer(
            "tessera_limit_campaign_concurrency",
            campaign_id=c,
            expected_revision=steer.revision(c),
            max_concurrent_stages=2,
            reason="raise",
            idempotency_key="limit-2",
        )

    # Pause before anything runs: the loop reserves nothing and says why.
    steer(
        "tessera_pause_campaign",
        campaign_id=c,
        expected_revision=steer.revision(c),
        reason="hold",
        idempotency_key="pause-1",
    )
    for _ in range(8):
        world.runner.tick()
    assert attempts() == [] and world.status(c) == "running"
    assert {e["stage_key"] for e in _events(env, "campaign.stage_waiting_paused")} >= {"s1", "s3"}

    steer(
        "tessera_resume_campaign",
        campaign_id=c,
        expected_revision=steer.revision(c),
        reason="go",
        idempotency_key="resume-1",
    )
    # HUMAN: an operator pause. The agent cannot lift it; only a person can.
    human = env.human()
    body = {"expected_revision": steer.revision(c), "reason": "operator hold"}
    assert env.store.pause_campaign(human, c, "h-pause", canonical_digest(body), body)[0] == 200
    with pytest.raises(ToolError):
        steer(
            "tessera_resume_campaign",
            campaign_id=c,
            expected_revision=steer.revision(c),
            reason="agent tries",
            idempotency_key="resume-2",
        )
    body = {"expected_revision": steer.revision(c), "reason": "operator done"}
    assert env.store.resume_campaign(human, c, "h-resume", canonical_digest(body), body)[0] == 200

    # Cancel the dependent stage while it is still pending; the rest finishes on its own.
    steer(
        "tessera_cancel_campaign_stage",
        campaign_id=c,
        stage_key="s2",
        expected_revision=steer.revision(c),
        reason="not needed",
        idempotency_key="cancel-s2",
    )
    world.tick_all(c)
    assert world.stages(c) == {"s1": "succeeded", "s2": "cancelled", "s3": "succeeded"}
    assert world.status(c) == "stopped", dump(world, c)
    assert {a["stage_key"] for a in attempts()} == {"s1", "s3"}  # s2 never ran
    receipt = steer("tessera_get_campaign_receipt", campaign_id=c)["receipt"]
    assert receipt["steering"]["max_concurrent_stages"] == 1
    assert receipt["steering"]["pause_count"] == 2  # the agent's and the operator's
    assert [x["stage_key"] for x in receipt["steering"]["stage_cancels"]] == ["s2"]
    for event in (
        "campaign.concurrency_limited",
        "campaign.paused",
        "campaign.resumed",
        "campaign.stage_cancel_requested",
        "campaign.stage_cancelled",
    ):
        assert {e["campaign_id"] for e in _events(env, event)} == {c}, event
