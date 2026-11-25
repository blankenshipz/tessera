"""The delegated decision routes: statuses, the 409 body, authorization and tenant scoping."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from test_delegated_decisions import World, _terms

from tessera_control.api import create_app
from tessera_control.approval_trust import AUTOMATION_HEADER, DELEGATION_HEADER


@pytest.fixture
def api(tmp_path):
    world = World(tmp_path)
    world.env.human()  # a human session exists (it signs the delegations)
    client = TestClient(create_app(world.env.state))
    return world, client


def agent_headers(world, name="decider", scopes=("campaign:decide_delegated", "campaign:read")):
    world.agent(scopes, name=name)
    return {
        "Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30,
        "X-Agent-Destination": "planner.local",
    }


def approve(client, headers, campaign, delegation, key="k1", **over):
    body = {
        "delegation_id": delegation["id"],
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
        **over,
    }
    return client.post(
        f"/campaigns/{campaign['id']}/delegated-approval",
        json=body,
        headers={**headers, "Idempotency-Key": key},
    )


def test_a_met_rule_is_a_200_and_a_second_try_is_the_409_body_naming_reasons(api):
    world, client = api
    delegation = world.create()
    campaign = world.campaign()
    headers = agent_headers(world)
    ok = approve(client, headers, campaign, delegation)
    assert ok.status_code == 200, ok.text
    assert ok.json()["approval"]["decision_class"] == "owner_delegated"
    again = approve(
        client, headers, {**campaign, "revision": ok.json()["revision"]}, delegation, "k2"
    )
    assert again.status_code == 409
    body = again.json()
    assert body["outside_delegation"] is True and body["escalated_to"] == "owner"
    assert "campaign_not_submitted" in body["reasons"]
    assert "owner has been asked" in body["detail"]


def test_without_the_scope_or_a_credential_nothing_is_decided(api):
    world, client = api
    delegation = world.create()
    campaign = world.campaign()
    reader = agent_headers(world, "reader", ("campaign:read",))
    assert approve(client, reader, campaign, delegation).status_code == 403
    assert client.post(
        f"/campaigns/{campaign['id']}/delegated-approval",
        json={"delegation_id": delegation["id"], "expected_revision": 1,
              "expected_spec_digest": campaign["spec_digest"]},
        headers={"Idempotency-Key": "anon"},
    ).status_code == 401  # fmt: skip
    assert world.decisions(delegation["id"]) == []


def test_unknown_fields_are_refused_and_another_tenant_sees_nothing(api):
    world, client = api
    delegation = world.create()
    campaign = world.campaign()
    headers = agent_headers(world)
    extra = approve(client, headers, campaign, delegation, unlock=[{"campaign_id": "x"}])
    assert extra.status_code == 422
    outsider = world.other_tenant()
    assert approve(client, outsider, campaign, delegation).status_code in (403, 404)
    asked = client.get(
        f"/projects/{world.project}/delegation-escalations", headers=agent_headers(world)
    )
    assert asked.status_code == 200 and asked.json() == {"escalations": []}


def test_an_escalation_is_listed_for_the_owner(api):
    world, client = api
    delegation = world.create(terms=_terms(per_run=5))
    campaign = world.campaign(cap=10)
    assert approve(client, agent_headers(world), campaign, delegation).status_code == 409
    listed = client.get(
        f"/projects/{world.project}/delegation-escalations", headers=agent_headers(world)
    ).json()["escalations"]
    assert [(e["campaign_id"], e["kind"]) for e in listed] == [(campaign["id"], "approval")]
    assert "per_run_cap_exceeded" in listed[0]["reasons"]


def test_a_header_delegation_reference_never_becomes_an_owner_delegation(api):
    """The X-Tessera-Delegation label (owner-run helpers) and the server-verified owner delegation
    (ADR 0122) stay separate: a request naming a real delegation id in headers, on the plain
    approve route, records only the label; no owner_delegated class and no delegation_id."""
    world, client = api
    delegation = world.create()
    campaign = world.campaign()
    headers = {
        "Authorization": "Bearer synthetic-human-token-" + "x" * 30,
        "Idempotency-Key": "header-delegation",
        AUTOMATION_HEADER: "owner-helper",
        DELEGATION_HEADER: delegation["id"],
        # Header names that resemble the in-process marker are ignored outright.
        "X-Tessera-Owner-Delegation": delegation["id"],
        "Owner-Delegation": delegation["id"],
        "Delegation-Id": delegation["id"],
    }
    response = client.post(
        f"/campaigns/{campaign['id']}/approve",
        json={
            "expected_revision": campaign["revision"],
            "expected_spec_digest": campaign["spec_digest"],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    approval = response.json()["approval"]
    assert approval["decision_class"] != "owner_delegated"
    assert approval.get("delegation_id") is None
    payload = world.env.events("campaign.approved")[-1]["payload"]
    assert payload["decision_class"] == "human_session"
    assert payload["delegation_ref"] == delegation["id"]  # recorded only as the label
    assert "delegation_id" not in payload
    stored = world.env.query(
        "SELECT decision_class, delegation_id FROM campaign_approvals WHERE campaign_id=?",
        campaign["id"],
    )
    assert [tuple(row) for row in stored] == [("human_session", None)]
    assert world.decisions(delegation["id"]) == []
    assert not world.env.events("campaign.delegated_approval")
    for event in world.env.events("campaign.approved"):
        assert "delegation_id" not in event["payload"]


APPROVER_KEY = re.compile(r"approv.*principal", re.IGNORECASE)


def _approver_keys(obj, path="$"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if APPROVER_KEY.search(str(key)):
                yield f"{path}.{key}"
            yield from _approver_keys(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _approver_keys(value, f"{path}[{index}]")


def test_the_agent_response_hides_who_approved_but_keeps_the_summary(api):
    """The route projects for an enrolled agent after the command: the stored row and the
    idempotency record keep the approver (the receipt and runner need it)."""
    world, client = api
    delegation = world.create()
    campaign = world.campaign()
    headers = agent_headers(world)
    ok = approve(client, headers, campaign, delegation)
    assert ok.status_code == 200, ok.text
    approval = ok.json()["approval"]
    assert list(_approver_keys(ok.json())) == []
    assert {
        "id", "spec_digest", "decision_class", "approved_at", "expires_at", "delegation_id",
        "trust_policy_digest", "status",
    } <= set(approval)  # fmt: skip
    assert approval["delegation_id"] == delegation["id"]
    # Non-vacuous: the same call is stored with the approver, and a replay is projected too.
    creator = world.row(delegation["id"])["created_by_principal_id"]
    (stored,) = world.env.query(
        "SELECT approving_principal_id FROM campaign_approvals WHERE campaign_id=?",
        campaign["id"],
    )
    assert stored[0] == creator
    replay = approve(client, headers, campaign, delegation)
    assert replay.status_code == 200 and list(_approver_keys(replay.json())) == []


def test_a_person_gets_the_full_approval_row(api):
    world, client = api
    delegation = world.create()
    campaign = world.campaign()
    human = {"Authorization": "Bearer synthetic-human-token-" + "x" * 30}
    ok = approve(client, human, campaign, delegation)
    assert ok.status_code == 200, ok.text
    creator = world.row(delegation["id"])["created_by_principal_id"]
    assert ok.json()["approval"]["approving_principal_id"] == creator
    assert list(_approver_keys(ok.json())) == ["$.approval.approving_principal_id"]
