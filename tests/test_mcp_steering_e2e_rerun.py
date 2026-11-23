"""One local-docker multi-stage campaign driven only through tessera_mcp tools, end to end:
submit, inspect, approve, adjust, stop, re-run the failed stages under the re-run tree's spend
bound (ADR 0074) and retrieve the outcome and receipts. Fakes only: no network, no Docker.

Every action the agent takes is a tool call (``Steer.__call__``, recorded in ``steer.calls``).
Approvals are ``tessera_approve_campaign`` calls on a separate approve-only server holding a
person's credential (``Approver``, recorded in ``CALLS``). The test also drives the fake
execution plane (``world.tick_*``) and reads the database to assert evidence; that stands in for
the runner and worker processes and is not an agent action.

Dependencies: it needs ``tessera_rerun_campaign`` (ADR 0069) and ``rerun_tree`` (ADR 0074), so it
cannot sit on a base without them.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

pytest.importorskip("mcp")

from campaign_runner_harness import (  # noqa: E402
    POOLS,
    admit,
    dump,
    lapse,
    latest,
    refuse_launches,
    started_but_unreported,
)
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from test_campaign_standing import Sched  # noqa: E402
from test_mcp_steering_e2e import (  # noqa: E402, F401
    AGENT_ACTIONS,
    Steer,
    _events,
    _reset,
    _spec,
    _stage,
)

# Imported for its fixture side effect: ``_reset`` (below) unregisters the provider worker.
from tessera_control import campaign_provider_worker as worker  # noqa: E402, F401
from tessera_control import campaign_providers as cp  # noqa: E402
from tessera_control.store import Conflict, canonical_digest  # noqa: E402
from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

CALLS: list[str] = []  # every agent and approver tool call of the test, in order


class Approver:
    """HUMAN: a person's own session credential on an operator-enabled approve-only server."""

    def __init__(self, sched, tmp_path, steer) -> None:
        env = sched.env
        env.human()
        reference = tmp_path / "human.token"
        reference.write_text("synthetic-human-token-" + "x" * 30)
        reference.chmod(0o600)
        client = ControlClient(
            Config("http://127.0.0.1:8765", reference, env.project["id"], "planner.local"),
            httpx.ASGITransport(app=steer.app),
        )
        self.server = create_server(client, approval_tool=True)

    def __call__(self, campaign_id, digest):
        CALLS.append("tessera_approve_campaign")
        result = asyncio.run(
            self.server.call_tool(
                "tessera_approve_campaign",
                {"campaign_id": campaign_id, "expected_spec_digest": digest},
            )
        )
        return result[1] if isinstance(result, tuple) else result


def test_one_campaign_is_steered_start_to_finish_over_mcp_tools(tmp_path):
    CALLS.clear()
    from provider_trust_fixture import DERIVED, install_trust

    sched = Sched(tmp_path)
    world, env = sched.world, sched.env
    cp.configure_providers({"local_docker": {"worker_principal_id": env.worker["id"]}})
    install_trust({"local-docker": (DERIVED, [env.worker["id"]])})
    steer = Steer(sched, tmp_path)
    approve = Approver(sched, tmp_path, steer)

    # -- submit: validate, then a three-stage campaign (s2 reads s1; s3 is independent) ---------
    spec = _spec(
        "e2e-rerun", _stage(env, "s1"), _stage(env, "s2", upstream="s1"), _stage(env, "s3"), cap=10
    )
    assert steer("tessera_validate_campaign_spec", spec=spec)
    first = steer("tessera_submit_campaign", spec=spec, idempotency_key="e2e-submit")
    a = first["id"]

    # -- inspect: the campaign, project limits and spend, placement, holdout claims ------------
    view = steer("tessera_get_campaign", campaign_id=a)
    assert view["status"] == "submitted" and {s["stage_key"] for s in view["stages"]} == {
        "s1",
        "s2",
        "s3",
    }
    assert "rerun_tree" not in view  # not part of a re-run tree yet
    spend = steer("tessera_project_spend")
    assert spend["unfinished"]["campaigns"] == 1 and spend["unfinished"]["cap_cents"] == 10
    assert steer("tessera_campaign_holdout", campaign_id=a)  # read-only; nothing claimed
    assert steer("tessera_list_campaigns")["campaigns"][0]["id"] == a

    # -- approve: an agent credential cannot; a person's credential can, over MCP, by digest ---
    agent_with_approval = create_server(steer.client, approval_tool=True)
    with pytest.raises(ToolError):
        asyncio.run(
            agent_with_approval.call_tool(
                "tessera_approve_campaign",
                {"campaign_id": a, "expected_spec_digest": view["spec_digest"]},
            )
        )
    assert not env.query("SELECT 1 FROM campaign_approvals")  # the refusal changed nothing
    with pytest.raises(ToolError):  # a digest that is not the stored one is refused client-side
        approve(a, "0" * 64)
    approve(a, view["spec_digest"])
    assert len(env.query("SELECT 1 FROM campaign_approvals")) == 1

    # -- adjust: lower the cap and the concurrency; the cap cannot go up or below what is held --
    steer(
        "tessera_reduce_campaign_budget",
        campaign_id=a,
        expected_revision=steer.revision(a),
        budget_cap_cents=8,
        reason="agent trims the cap",
        idempotency_key="e2e-reduce",
    )
    assert steer("tessera_campaign_budget", campaign_id=a)["budget_cap_cents"] == 8
    with pytest.raises(ToolError):
        steer(
            "tessera_reduce_campaign_budget",
            campaign_id=a,
            expected_revision=steer.revision(a),
            budget_cap_cents=9,
            reason="raise",
            idempotency_key="e2e-raise",
        )
    steer(
        "tessera_limit_campaign_concurrency",
        campaign_id=a,
        expected_revision=steer.revision(a),
        max_concurrent_stages=1,
        reason="one at a time",
        idempotency_key="e2e-limit",
    )

    # -- run: s1 succeeds; s3 fails (scripted exit code); s2 is never reached -----------------
    world.scenarios = [
        (0, {"data.json": json.dumps({"score": 3})}),
        (137, {"data.json": json.dumps({"score": 0})}),
    ]
    # The scheduler picks the ready stages in spec order (s1, then s3; s2 waits for s1's output
    # and the limit is one), so the scripted scenarios are consumed s1 first, s3 second.
    world.tick_all(a)  # one at a time: s1, then s3 (fails), so s2 never runs
    assert [x["stage_key"] for x in world.attempts(a)] == ["s1", "s3"]
    assert world.stages(a)["s3"] == "failed", dump(world, a)
    assert world.status(a) == "failed"
    logs = steer("tessera_campaign_logs", campaign_id=a)
    assert any(att.get("failure_class") for att in logs["attempts"])
    first_receipt = steer("tessera_get_campaign_receipt", campaign_id=a)["receipt"]
    assert first_receipt["stages"]
    settled_a = steer("tessera_campaign_budget", campaign_id=a)["settled_cents"]

    # -- rerun failed stages, under the re-run tree's spend bound ------------------------------
    plan = steer("tessera_prepare_stage_retry", campaign_id=a)  # the offline plan still works
    assert sorted(plan["retried_stages"]) == ["s2", "s3"]
    # Nothing settled, so the first refusal is the plain rule: never above the original cap (10).
    over_cap = 10 + 1
    with pytest.raises(ToolError, match="HTTP 409"):
        steer("tessera_rerun_campaign", campaign_id=a, idempotency_key="e2e-over",
              budget_cap_cents=over_cap)  # fmt: skip
    # The MCP client deliberately never relays a denial body, so the tool shows only the status.
    # The same request at the store says why.
    body = {"budget_cap_cents": over_cap}
    with pytest.raises(Conflict, match="original campaign's cap"):
        env.store.rerun_campaign(
            env.agent(AGENT_ACTIONS, "steerer"), a, "e2e-over-store", canonical_digest(body), body
        )
    assert len(env.query("SELECT 1 FROM campaigns")) == 1  # a refusal writes nothing
    b_cap = 4
    second = steer(
        "tessera_rerun_campaign", campaign_id=a, idempotency_key="e2e-rerun-1",
        budget_cap_cents=b_cap,
    )  # fmt: skip
    b = second["id"]
    assert second["status"] == "submitted" and second["rerun"]["rerun_of"] == a
    assert sorted(second["rerun"]["retried_stages"]) == ["s2", "s3"]
    # The unfinished re-run commits its cap, so a sibling cannot take the rest of the first cap.
    tree = steer("tessera_get_campaign", campaign_id=b)["rerun_tree"]
    assert tree["root_campaign_id"] == a and tree["root_cap_cents"] == 10
    left = tree["remaining_cents"]
    assert left == 10 - settled_a - b_cap
    with pytest.raises(ToolError, match="HTTP 409"):
        steer("tessera_rerun_campaign", campaign_id=a, idempotency_key="e2e-sibling",
              budget_cap_cents=left + 1)  # fmt: skip
    # The store gives the reason: this one IS the tree's spend bound (left + 1 is under cap 10).
    body = {"budget_cap_cents": left + 1}
    assert left + 1 <= 10
    with pytest.raises(Conflict, match=f"{left} cents left"):
        env.store.rerun_campaign(
            env.agent(AGENT_ACTIONS, "steerer"), a, "e2e-sib-store", canonical_digest(body), body
        )

    # -- approve the new digest, let it reserve a slot, then stop it ---------------------------
    approve(b, steer("tessera_get_campaign", campaign_id=b)["spec_digest"])
    world.tick_until(lambda: any(x["release_id"] for x in world.attempts(b)), worker=False)
    steer(
        "tessera_cancel_campaign",
        campaign_id=b,
        expected_revision=steer.revision(b),
        reason="agent stops the re-run",
        idempotency_key="e2e-cancel",
    )
    release = env.query("SELECT id, revision FROM snapshot_releases WHERE status='active'")
    assert release  # no runner tick since the cancel: the agent revokes the release itself
    steer(
        "tessera_revoke_campaign_release",
        campaign_id=b,
        release_id=release[0]["id"],
        expected_revision=release[0]["revision"],
        reason="agent revokes the stale release",
        idempotency_key="e2e-revoke",
    )
    for _ in range(40):
        if world.status(b) == "cancelled":
            break
        world.runner.tick()
    assert world.status(b) == "cancelled", dump(world, b)
    grants = steer("tessera_campaign_grants", campaign_id=b)["grants"]
    assert all(g["grant_status"] != "held" for g in grants)

    # -- a stopped re-run commits only what it settled, so another re-run fits again -----------
    tree = steer("tessera_get_campaign", campaign_id=a)["rerun_tree"]
    members = {m["campaign_id"]: m for m in tree["members"]}
    assert members[b]["status"] == "cancelled"
    assert members[b]["committed_cents"] == members[b]["settled_cents"]
    left = tree["remaining_cents"]
    assert left >= 1
    world.scenarios = [(0, {"data.json": json.dumps({"score": 3})})] * 2
    third = steer(
        "tessera_rerun_campaign", campaign_id=a, idempotency_key="e2e-rerun-2",
        budget_cap_cents=left,
    )  # fmt: skip
    c = third["id"]
    approve(c, steer("tessera_get_campaign", campaign_id=c)["spec_digest"])
    world.tick_all(c)
    assert world.status(c) == "succeeded", dump(world, c)

    # -- retrieve the outcome and receipts ------------------------------------------------------
    receipt = steer("tessera_get_campaign_receipt", campaign_id=c)["receipt"]
    assert receipt["rerun"]["rerun_of"] == a and sorted(receipt["rerun"]["retried_stages"]) == [
        "s2",
        "s3",
    ]
    assert "request_digest" not in receipt["rerun"]
    outputs = steer("tessera_campaign_outputs", campaign_id=c)["outputs"]
    assert {o["stage_key"] for o in outputs} == {"s2", "s3"} and outputs[0]["sha256"]
    final_tree = steer("tessera_get_campaign", campaign_id=c)["rerun_tree"]
    assert final_tree["committed_cents"] <= final_tree["root_cap_cents"]
    assert {r["campaign_id"] for r in steer("tessera_get_campaign", campaign_id=a)["reruns"]} == {
        b,
        c,
    }
    assert steer("tessera_project_spend")["unfinished"]["campaigns"] == 0

    # -- journal evidence, and the tool calls the agent and the approver made ---------------
    assert [e["campaign_id"] for e in _events(env, "campaign.rerun_submitted")] == [b, c]
    assert [e["campaign_id"] for e in _events(env, "campaign.budget_reduced")] == [a]
    assert (
        len(env.query("SELECT 1 FROM campaign_approvals")) == 3
    )  # a, b, c: each a person's; depends on spec-order scheduling
    agent_only = {
        "tessera_submit_campaign",
        "tessera_get_campaign",
        "tessera_project_spend",
        "tessera_reduce_campaign_budget",
        "tessera_limit_campaign_concurrency",
        "tessera_rerun_campaign",
        "tessera_cancel_campaign",
        "tessera_get_campaign_receipt",
        "tessera_campaign_outputs",
        "tessera_validate_campaign_spec",
        "tessera_prepare_stage_retry",
        "tessera_campaign_logs",
        "tessera_campaign_grants",
        "tessera_campaign_budget",
        "tessera_revoke_campaign_release",
    }
    assert agent_only <= set(steer.calls)  # the agent alone made every non-approval call
    assert set(CALLS) == {"tessera_approve_campaign"}  # the approver made only approvals
    assert "tessera_approve_campaign" not in steer.calls  # the agent never approved
    # Depends on spec-order scheduling: refused-digest attempt, then a, b, c.
    assert CALLS.count("tessera_approve_campaign") == 4


def _one_stage_campaign(tmp_path, v1=False):
    from provider_trust_fixture import DERIVED, install_trust

    sched = Sched(tmp_path)
    env = sched.env
    cp.configure_providers({"local_docker": {"worker_principal_id": env.worker["id"]}})
    install_trust({"local-docker": (DERIVED, [env.worker["id"]])})
    steer = Steer(sched, tmp_path)
    approve = Approver(sched, tmp_path, steer)
    if v1:
        from campaign_runner_harness import stage_spec

        spec = env.spec()
        spec["stages"] = [
            stage_spec(
                env,
                "s1",
                metrics=[{"name": "score", "path": "data.json", "json_pointer": "/score"}],
            )
        ]
    else:
        spec = _spec("lost-lease", _stage(env, "s1"), cap=6)
    submitted = steer(
        "tessera_submit_campaign",
        spec=spec,
        idempotency_key="lost-submit",
    )
    approve(
        submitted["id"], steer("tessera_get_campaign", campaign_id=submitted["id"])["spec_digest"]
    )
    return sched, steer, submitted["id"]


def test_a_lost_worker_lease_is_visible_over_mcp_and_stays_with_a_person(tmp_path):
    """Fault: the worker starts the container and every report is lost, then the lease lapses.
    Control cannot tell this from a launch that never happened, so the attempt is `unknown`, the
    campaign `blocked` and the grant unresolved. The agent can see it and stop the campaign; it
    cannot reconcile the grant (no tool), and nothing reconciles it on its behalf."""
    sched, steer, c = _one_stage_campaign(tmp_path)
    world = sched.world
    starts: list[str] = []
    started_but_unreported(world, c, starts)
    assert len(starts) == 1
    for _ in range(4):
        world.runner.tick()
    assert world.status(c) == "blocked", dump(world, c)
    assert latest(world, c)["status"] == "unknown"

    # The agent sees the fault through tools only.
    view = steer("tessera_get_campaign", campaign_id=c)
    assert view["status"] == "blocked"
    assert [a["status"] for a in view["attempts"]] == ["unknown"]
    grants = steer("tessera_campaign_grants", campaign_id=c)["grants"]
    assert [g["grant_status"] for g in grants] == ["active"]  # unresolved: a person's evidence
    assert steer("tessera_campaign_budget", campaign_id=c)["held_cents"] > 0
    retries = steer("tessera_campaign_retries", campaign_id=c)
    assert retries["campaign_id"] == c
    # It cannot resolve it: the server registers no reconcile tool and no unconditional approve
    # tool (the one "approve" name acts only under a person's signed delegation, ADR 0122, and
    # needs its own scope), and a stop does not release the grant.
    registered = [tool.name for tool in asyncio.run(steer.server.list_tools())]
    assert registered
    delegated = {"tessera_delegated_approve_campaign"}
    assert not [
        n for n in registered if n not in delegated and ("reconcile" in n or "approve" in n)
    ], registered
    assert "reconcile" not in " ".join(registered)
    steer(
        "tessera_cancel_campaign",
        campaign_id=c,
        expected_revision=steer.revision(c),
        reason="worker lost",
        idempotency_key="lost-cancel",
    )
    for _ in range(6):
        world.runner.tick()
    assert world.row("SELECT status FROM execution_grants")["status"] == "active"
    assert world.status(c) not in {"succeeded", "failed"}  # never reported as an outcome
    # No receipt exists until a campaign is terminal, so the outcome cannot be read yet.
    with pytest.raises(ToolError, match="404"):
        steer("tessera_get_campaign_receipt", campaign_id=c)
    # The stop is recorded, but a blocked campaign with an unresolved grant stays blocked until
    # a person reconciles it with evidence: the agent's cancel does not clear it.
    assert [e["campaign_id"] for e in _events(sched.env, "campaign.cancel_requested")] == [c]
    assert world.status(c) == "blocked"


def test_a_refused_launch_is_reconciled_by_the_platform_and_the_agent_sees_it(
    tmp_path, monkeypatch
):
    """Fault: the worker leases the grant but Docker refuses the launch, which it reports. The
    platform reconciles that provable non-start by itself (ADR 0046) and the campaign finishes;
    the agent reads the not-started attempt and then the outcome, with no human step."""
    sched, steer, c = _one_stage_campaign(tmp_path, v1=True)
    world = sched.world
    refuse_launches(monkeypatch)
    admit(world, c)
    world.poller.run(max_cycles=1)
    first = latest(world, c)
    lapse(world, first["grant_id"])
    monkeypatch.undo()
    world.tick_all(c)
    assert world.status(c) == "succeeded", dump(world, c)
    logs = steer("tessera_campaign_logs", campaign_id=c)
    classes = [a.get("failure_class") for a in logs["attempts"]]
    assert "not_started" in classes, logs
    assert any(POOLS in str(a.get("failure_detail")) for a in logs["attempts"])
    grants = steer("tessera_campaign_grants", campaign_id=c)["grants"]
    assert {g["grant_status"] for g in grants} == {"reconciled"}
    assert steer("tessera_campaign_budget", campaign_id=c)["settled_cents"] == 0
    # The agent made no approval: the only approval row is the person's, not the agent's.
    approvals = sched.env.query("SELECT approving_principal_id FROM campaign_approvals")
    assert [r["approving_principal_id"] for r in approvals] == [sched.env.human()["id"]]


def test_a_refused_launch_on_a_v2_stage_stays_blocked_for_a_person(tmp_path, monkeypatch):
    """Observation pinned (not a design guarantee): the same fault as above on a v2
    ``local-docker`` stage is NOT auto-reconciled. Cause, from ``campaign_autoreconcile.assess``:
    it requires a zero-exposure grant and a zero hold on the attempt. A v2 stage carries
    ``max_exposure_cents`` (its cap), so the stage's attempt holds money and the grant's
    exposure is above zero; the guard returns ``None`` before it ever looks at the worker's
    launch error. So the attempt stays ``unknown``, the campaign ``blocked`` and the grant
    unresolved. If this changes, update docs/mcp-steering.md (gap row 8)."""
    sched, steer, c = _one_stage_campaign(tmp_path, v1=False)
    world = sched.world
    refuse_launches(monkeypatch)
    admit(world, c)
    world.poller.run(max_cycles=1)
    first = latest(world, c)
    lapse(world, first["grant_id"])
    monkeypatch.undo()
    for _ in range(8):
        world.runner.tick()
    assert world.status(c) == "blocked", dump(world, c)
    attempt = latest(world, c)
    assert attempt["status"] == "unknown", dump(world, c)
    assert attempt["hold_cents"] > 0  # the cause: a held amount, which assess() requires be 0
    assert world.row("SELECT max_exposure_cents FROM execution_grants")["max_exposure_cents"] > 0
    assert world.row("SELECT status FROM execution_grants")["status"] == "active"  # not reconciled
    assert not _events(sched.env, "campaign.attempt_not_started")
    view = steer("tessera_get_campaign", campaign_id=c)
    assert view["status"] == "blocked"
