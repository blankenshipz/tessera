"""An external-scope agent reads delegations and their decisions without the signer's identity."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from test_delegated_decisions import World, _terms
from test_delegated_decisions_api import agent_headers, approve

from tessera_control.api import create_app

PERSON = re.compile(r"(created|revoked)_(by_principal|credential)_id|approv.*principal")
HUMAN = {"Authorization": "Bearer synthetic-human-token-" + "x" * 30}


def keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            keys(v, found)
    elif isinstance(value, list):
        for v in value:
            keys(v, found)
    return found


def person_keys(value):
    return {k for k in keys(value) if PERSON.search(k)}


@pytest.fixture
def api(tmp_path):
    world = World(tmp_path)
    world.env.human()
    return world, TestClient(create_app(world.env.state))


def seeded(world, client):
    """One approved decision, one escalation and one revoked delegation."""
    delegation = world.create(terms=_terms(per_run=5))
    campaign = world.campaign(cap=10)
    decider = agent_headers(world)
    assert approve(client, decider, campaign, delegation).status_code == 409  # escalated
    revoked = world.create()
    status, _ = world.revoke(revoked)
    assert status == 200
    return delegation, revoked, decider


def get(client, headers, path):
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_reads_hide_the_signer_from_agents_and_keep_what_they_act_on(api):
    world, client = api
    delegation, revoked, decider = seeded(world, client)
    reader = agent_headers(world, "reader", ("campaign:read",))
    before_d = world.decisions(delegation["id"])
    assert before_d
    # Agents read decisions only on campaigns they submitted or decisions they acted on
    # (campaign_read_scope): record the reader as the decided campaign's submitter (test
    # setup only), so it reads the decisions without being their actor.
    for campaign_id in {d["campaign_id"] for d in before_d}:
        world.env.execute(
            "UPDATE campaigns SET submitted_by_principal_id=? WHERE id=?",
            world.agent(("campaign:read",), name="reader")["id"],
            campaign_id,
        )
    outsider = agent_headers(world, "outsider", ("campaign:read",))
    before = [dict(r) for r in world.env.query("SELECT * FROM approval_delegations")]

    list_path = f"/projects/{world.project}/approval-delegations"
    one = f"/approval-delegations/{delegation['id']}"
    decisions = one + "/decisions"
    esc = f"/projects/{world.project}/delegation-escalations"

    # Humans still get full rows.
    human_list = get(client, HUMAN, list_path)
    assert {"created_by_principal_id", "created_credential_id"} <= keys(human_list)
    assert "revoked_by_principal_id" in keys(human_list)
    assert {"created_by_principal_id", "created_credential_id"} <= keys(get(client, HUMAN, one))
    assert "actor_principal_id" in keys(get(client, HUMAN, decisions))

    for headers in (reader, decider):
        agent_list = get(client, headers, list_path)["approval_delegations"]
        assert {d["id"] for d in agent_list} == {delegation["id"], revoked["id"]}
        for item in agent_list:
            assert not person_keys(item)
            assert {
                "id", "terms", "terms_digest", "decision_class", "automation_label",
                "expires_at", "status", "state", "revision", "created_at", "revoked_at",
            } <= set(item)  # fmt: skip
        by_id = {d["id"]: d for d in agent_list}
        assert by_id[revoked["id"]]["revoked_at"] and by_id[revoked["id"]]["status"] == "revoked"
        detail = get(client, headers, one)
        assert not person_keys(detail)
        assert detail["usage"] and detail["terms"] and detail["terms_digest"]
        assert detail["decisions"]
        for row in detail["decisions"] + get(client, headers, decisions)["decisions"]:
            assert not person_keys(row)
            assert {
                "id", "outcome", "reason", "rule_name", "evaluation", "campaign_id",
                "campaign_spec_digest", "approval_id", "gate_decision_id", "created_at",
                "kind", "actor_kind",
            } <= set(row)  # fmt: skip
        assert not person_keys(get(client, headers, esc))
        assert get(client, headers, esc)["escalations"]

    # The acting agent sees its own id; another agent does not see the actor.
    assert all("actor_principal_id" in r for r in get(client, decider, decisions)["decisions"])
    assert all("actor_principal_id" not in r for r in get(client, reader, decisions)["decisions"])
    assert all(
        r["actor_principal_id"] == before_d[0]["actor_principal_id"]
        for r in get(client, HUMAN, decisions)["decisions"]
    )

    # A different agent that neither submitted the campaign nor acted sees no decisions.
    assert get(client, outsider, one)["decisions"] == []
    assert get(client, outsider, decisions)["decisions"] == []

    # Reads change nothing.
    assert [dict(r) for r in world.env.query("SELECT * FROM approval_delegations")] == before
    assert world.decisions(delegation["id"]) == before_d
