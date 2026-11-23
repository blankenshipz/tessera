"""What an enrolled agent sees of a Salad review resolved under a pre-approval (ADR 0044, G-11).

The real runner drives a Salad campaign with an exact-digest review pre-approval, so it resolves
the plan and source-use decision requests itself (``campaign_delegated``). An agent credential
with every campaign read scope then reads each campaign surface over the real HTTP routes and the
MCP tools: no review is ever listed as pending, and no pre-approval id or digest, resolved
decision-request id, requester/reviewer/resolver principal, approving principal or credential id
reaches the agent. A sibling campaign without a pre-approval shows the agent its pending reviews.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control.api import create_app
from tessera_control.campaign_models import normalize, parse_spec_json
from tessera_control.campaign_runner import _Ctx
from tessera_control.store import canonical_digest

DEST = {"X-Agent-Destination": "planner.local"}
READS = ["campaign:read", "snapshot:read", "artifact:read", "campaign:logs", "budget:read"]
CLEAN = dict(
    status="stopped",
    reason="deadline",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.5",
    actual_billed_cost_cents=None,
    observed_at="2026-11-19T00:00:00Z",
)


def _short(env):
    return [salad_stage(env, max_duration_seconds=600)]


class PreWorld(SaladWorld):
    def start_pre(self) -> None:
        submitted = self.env.submit(spec=self.spec)
        row = self.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0]
        campaign = dict(row)
        spec = normalize(parse_spec_json(campaign["spec_json"]))
        entry = {
            **self.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), "g1"),
            "max_attempt_cents": 50,
            "max_total_cents": 50,
        }
        body = {
            "expected_revision": submitted["revision"],
            "expected_spec_digest": submitted["spec_digest"],
            "preapprovals": [entry],
        }
        status, result = self.env.store.approve_campaign(
            self.human, submitted["id"], self.key("approve"), canonical_digest(body), body
        )
        assert status == 200
        self.campaign = result


def _token(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


def _paths(world) -> dict[str, str]:
    cid, project = world.campaign_id, world.env.project["id"]
    return {
        "campaign": f"/campaigns/{cid}",
        "report": f"/campaigns/{cid}/report",
        "report_detail": f"/campaigns/{cid}/report?detail=true",
        "receipt": f"/campaigns/{cid}/receipt",
        "outputs": f"/campaigns/{cid}/outputs",
        "logs": f"/campaigns/{cid}/logs",
        "grants": f"/campaigns/{cid}/grants",
        "retries": f"/campaigns/{cid}/retries",
        "budget": f"/campaigns/{cid}/budget",
        "holdout": f"/campaigns/{cid}/holdout",
        "stage_watch": f"/campaigns/{cid}/stage-watch?offset=0&limit=100",
        "gate_decisions": f"/campaigns/{cid}/gate-decisions",
        "standing": f"/projects/{project}/agent-standing-status",
        "spend": f"/projects/{project}/agent-spend",
        "holdout_claims": f"/projects/{project}/holdout-claims",
    }


def _read_all(world, client, headers, *, terminal: bool) -> dict[str, dict]:
    bodies = {}
    for name, path in _paths(world).items():
        response = client.get(path, headers=headers)
        if name == "receipt" and not terminal:
            assert response.status_code == 404, (name, response.text)
            continue
        assert response.status_code == 200, (name, response.status_code, response.text)
        bodies[name] = response.json()
    return bodies


def _mcp_all(world, tmp_path) -> dict[str, dict]:
    """Every agent read tool, through create_server and the real app over ASGI."""
    pytest.importorskip("mcp")
    from tessera_mcp.client import ControlClient
    from tessera_mcp.config import Config
    from tessera_mcp.server import create_server

    reference = tmp_path / "agent.token"
    reference.write_text("synthetic-agent-token-" + "x" * 30)
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, world.env.project["id"], "planner.local"),
        httpx.ASGITransport(app=create_app(world.env.state)),
    )
    server = create_server(client, campaign_tools=True, campaign_actions=frozenset(READS))
    cid = world.campaign_id
    calls = {
        "tessera_get_campaign": {"campaign_id": cid},
        "tessera_campaign_report": {"campaign_id": cid, "detail": True},
        "tessera_get_campaign_receipt": {"campaign_id": cid},
        "tessera_campaign_outputs": {"campaign_id": cid},
        "tessera_campaign_logs": {"campaign_id": cid},
        "tessera_campaign_grants": {"campaign_id": cid},
        "tessera_campaign_retries": {"campaign_id": cid},
        "tessera_campaign_budget": {"campaign_id": cid},
        "tessera_campaign_holdout": {"campaign_id": cid},
        "tessera_campaign_stage_watch": {"campaign_id": cid},
        "tessera_campaign_gate_decisions": {"campaign_id": cid},
        "tessera_campaign_standing_status": {},
        "tessera_project_spend": {},
        "tessera_campaign_holdout_claims": {},
    }
    results = {}
    for name, arguments in calls.items():
        result = asyncio.run(server.call_tool(name, arguments))
        results[name] = result[1] if isinstance(result, tuple) else result
    return results


def _secrets(world) -> dict[str, str]:
    """Every pre-approval, review-decision and approver identifier recorded for the campaign."""
    found: dict[str, str] = {}
    cid = world.campaign_id
    for row in world.env.query(
        "SELECT * FROM campaign_review_preapprovals WHERE campaign_id=?", cid
    ):
        for column in ("id", "source_digest", "plan_digest", "source_use_digest"):
            found[f"preapproval.{column}"] = row[column]
    for row in world.env.query(
        "SELECT * FROM campaign_review_preapproval_uses WHERE campaign_id=?", cid
    ):
        found[f"use.{row['kind']}.id"] = row["id"]
        found[f"use.{row['kind']}.review_digest"] = row["review_digest"]
    salad = world.salad()
    for column in ("plan_decision_id", "source_decision_id"):
        decision = world.row("SELECT * FROM decision_requests WHERE id=?", salad[column])
        found[f"{column}"] = decision["id"]
        found[f"{column}.scope_digest"] = decision["scope_digest"]
        for who in ("reviewer_principal_id", "requester_principal_id", "resolved_by_principal_id"):
            if decision[who]:
                found[f"{column}.{who}"] = decision[who]
        if decision["rationale"] and len(decision["rationale"]) >= 12:
            found[f"{column}.rationale"] = decision["rationale"]
    for row in world.env.query("SELECT * FROM campaign_approvals WHERE campaign_id=?", cid):
        found["approval.approving_principal_id"] = row["approving_principal_id"]
    found["human.principal_id"] = world.human["id"]
    found["human.credential_id"] = world.human["credential_id"]
    return found


def _leaks(body, secrets: dict[str, str]) -> list[str]:
    text = json.dumps(body, sort_keys=True)
    return sorted(name for name, value in secrets.items() if value and value in text)


def _preapproved_world(tmp_path) -> PreWorld:
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre()
    world.env.agent(READS, "agent")
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")
    salad = world.salad()
    for column in ("plan_decision_id", "source_decision_id"):
        row = world.row("SELECT status FROM decision_requests WHERE id=?", salad[column])
        assert row["status"] == "approved"
    resolved = world.events("decision_resolved")
    assert resolved and all(e["decision_class"] == "campaign_delegated" for e in resolved)
    return world


def test_baseline_human_gated_review_is_pending_for_the_agent_with_only_its_own_fields(tmp_path):
    world = SaladWorld(tmp_path)
    world.env.agent(READS, "agent")
    world.to_awaiting_review()
    client = TestClient(create_app(world.env.state))
    agent = client.get(f"/campaigns/{world.campaign_id}", headers={**_token("agent"), **DEST})
    human = client.get(f"/campaigns/{world.campaign_id}", headers=_token("human"))
    assert agent.status_code == human.status_code == 200
    salad = world.salad()
    reviews = [i for i in agent.json()["pending_human_approval"] if i["kind"] == "salad_review"]
    assert reviews == [
        {
            "kind": "salad_review",
            "stage_key": "g1",
            "attempt": 1,
            "review": review,
            "awaiting": f"a person's decision on the {review.replace('_', ' ')} review",
            "decision_request_id": salad[column],
        }
        for review, column in (("plan", "plan_decision_id"), ("source_use", "source_decision_id"))
    ]
    assert human.json()["pending_human_approval"] == agent.json()["pending_human_approval"]
    # The pending item names the request by id and nothing else about it.
    secrets = {
        k: v
        for k, v in _secrets(world).items()
        if k not in {"plan_decision_id", "source_decision_id"}
    }
    pending = agent.json()["pending_human_approval"]
    assert _leaks(pending, secrets) == []


def _by_design(name: str, body: dict) -> tuple[dict, dict]:
    """Split off the fields every campaign:read caller is given by contract.

    - the live campaign view lists the spec approval rows with their scope, class, expiry and
      delegation but, for an agent, WITHOUT ``approving_principal_id`` (the person who approved,
      and so who created the pre-approval): the agent projection hides approver identity, as
      the gate decisions do;
    - the campaign receipt carries ``approval.approving_principal_id`` and, by ADR 0044 section
      7, the ``preapprovals`` section (id, the three digests, caps and each delegated
      resolution). The receipt is digest-bound and has no per-caller projection.
    Everything else must carry none of the secrets."""
    body = json.loads(json.dumps(body))
    taken: dict = {}
    if name in {"campaign", "tessera_get_campaign"}:
        assert "approving_principal_id" not in body["approval"]
        assert all("approving_principal_id" not in a for a in body["approvals"])
    if name in {"receipt", "tessera_get_campaign_receipt"}:
        receipt = body["receipt"]
        taken["approval"] = receipt["approval"].pop("approving_principal_id")
        taken["preapprovals"] = receipt.pop("preapprovals")
    return body, taken


def _check_by_design(world, taken: dict, secrets: dict[str, str]) -> None:
    if "preapprovals" in taken:
        assert taken["approval"] == world.human["id"]
        [entry] = taken["preapprovals"]
        assert entry["id"] == secrets["preapproval.id"]
        assert entry["plan_digest"] == secrets["preapproval.plan_digest"]
        kinds = sorted(r["kind"] for r in entry["resolutions"])
        assert kinds == ["plan", "source_assessment", "source_use"]
        # The receipt names who approved, never a credential.
        assert secrets["human.credential_id"] not in json.dumps(taken)


def _agent_leaks(world, bodies: dict[str, dict], secrets: dict[str, str]) -> dict[str, list]:
    leaks = {}
    for name, body in bodies.items():
        rest, taken = _by_design(name, body)
        _check_by_design(world, taken, secrets)
        found = _leaks(rest, secrets)
        if found:
            leaks[name] = found
    return leaks


def test_a_preapproved_review_is_never_pending_and_reaches_the_agent_only_by_contract(tmp_path):
    world = _preapproved_world(tmp_path)
    client = TestClient(create_app(world.env.state))
    agent_headers = {**_token("agent"), **DEST}
    secrets = _secrets(world)
    assert {"preapproval.id", "plan_decision_id", "source_decision_id"} <= set(secrets)

    def check(terminal: bool) -> tuple[dict[str, dict], dict[str, dict]]:
        agent = _read_all(world, client, agent_headers, terminal=terminal)
        human = _read_all(world, client, _token("human"), terminal=terminal)
        assert agent["campaign"]["pending_human_approval"] == []
        assert human["campaign"]["pending_human_approval"] == []
        assert _agent_leaks(world, agent, secrets) == {}
        # The agent's live view hides who approved; a person still reads the full rows.
        assert [a["approving_principal_id"] for a in human["campaign"]["approvals"]] == [
            world.human["id"]
        ]
        assert human["campaign"]["approval"]["approving_principal_id"] == world.human["id"]
        assert agent["campaign"]["approvals"] == [
            {k: v for k, v in a.items() if k != "approving_principal_id"}
            for a in human["campaign"]["approvals"]
        ]
        # What was approved (digest, class, expiry, delegation) stays visible to the agent.
        kept = agent["campaign"]["approval"]
        assert {"id", "spec_digest", "decision_class", "approved_at", "expires_at"} <= set(kept)
        return agent, human

    check(terminal=False)
    world.run_to_outcome(guardian_final=CLEAN)
    world.tick_until(lambda: world.status() == "succeeded")
    agent, human = check(terminal=True)
    assert agent["receipt"] == human["receipt"]
    # The pending list was never the place a delegated review shows up, for anyone.
    for body in (agent["campaign"], human["campaign"]):
        assert _leaks(body["pending_human_approval"], secrets) == []

    tools = _mcp_all(world, tmp_path)
    assert tools["tessera_get_campaign"]["pending_human_approval"] == []
    assert _agent_leaks(world, tools, secrets) == {}
    # The tools pass the control payload through unchanged.
    assert tools["tessera_get_campaign_receipt"] == agent["receipt"]
    assert tools["tessera_campaign_gate_decisions"] == agent["gate_decisions"]
    assert tools["tessera_campaign_standing_status"] == agent["standing"]


def test_an_agent_never_sees_who_approved_a_campaign_but_a_person_does(tmp_path):
    world = _preapproved_world(tmp_path)
    client = TestClient(create_app(world.env.state))
    cid = world.campaign["id"]
    agent = client.get(f"/campaigns/{cid}", headers={**_token("agent"), **DEST}).json()
    human = client.get(f"/campaigns/{cid}", headers=_token("human")).json()
    assert human["approval"]["approving_principal_id"] == world.human["id"]
    assert "approving_principal_id" not in agent["approval"]
    assert all("approving_principal_id" not in row for row in agent["approvals"])
    assert world.human["id"] not in json.dumps(agent["approvals"])
    # The summary of the approval itself is intact for the agent (scope, cap, expiry, class).
    assert agent["approval"]["spec_digest"] == human["approval"]["spec_digest"]
    assert agent["approval"]["expires_at"] == human["approval"]["expires_at"]
    assert agent["approval"]["decision_class"] == human["approval"]["decision_class"]
