"""Audit O-05, the narrowing paths: what a second agent is NOT told about the first agent's work.

Two agents (alice submitted, bob did not) read the same project. Every test also asserts that the
submitter still sees its own data, so a filter that returns nothing for everyone cannot pass.
Covers re-run linkage and the tree's withheld marker, withheld submission approvals, standing
status starts and triggers, delegation escalations and decisions, and the MCP read tools.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from mcp_required import require_mcp
from test_campaign_submissions import SUBMIT, Sub
from test_delegated_decisions import World, _terms

from tessera_control import campaign_rerun, program_budget
from tessera_control.api import create_app
from tessera_control.delegated_decisions import DECIDE_SCOPE, DelegationRefused

require_mcp()

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

DEST = {"X-Agent-Destination": "planner.local"}
ACTIONS = [
    "campaign:submit",
    "campaign:read",
    "campaign:cancel",
    "campaign:logs",
    "artifact:read",
    "budget:read",
]
MISSING = "campaign_" + "0" * 32


def _token(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


def _get(client, path, name):
    headers = _token(name) if name == "human" else {**_token(name), **DEST}
    return client.get(path, headers=headers)


def _link(env, child, parent, actor):
    """Record the re-run link a server-side re-run writes (campaign.rerun_submitted)."""
    org = env.query("SELECT org_id FROM projects WHERE id=?", env.project["id"])[0][0]
    env.execute(
        "INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision, "
        "actor_principal_id, created_at, payload_json) VALUES (?,?,NULL,?,1,?,?,?)",
        org,
        env.project["id"],
        campaign_rerun.RERUN_EVENT,
        actor["id"],
        "2026-01-01T00:00:00Z",
        json.dumps(
            {
                "campaign_id": child["id"],
                "rerun_of": parent["id"],
                "mode": "identical",
                "retried_stages": [],
                "request_digest": "d" * 64,
            }
        ),
    )


# ---- (a) (b) re-run linkage and the tree's withheld marker -----------------------------------


@pytest.fixture
def reruns(tmp_path):
    env = Env(tmp_path)
    alice = env.agent(ACTIONS, "alice")
    bob = env.agent(ACTIONS, "bob")
    env.human("human")
    root = env.submit(alice)  # alice's first campaign
    own_child = env.submit(alice)  # alice re-ran it
    other_child = env.submit(bob)  # bob re-ran alice's campaign (a link only a reader of both sees)
    _link(env, own_child, root, alice)
    _link(env, other_child, root, bob)
    solo_root = env.submit(alice)  # a chain made only of alice's campaigns
    solo_child = env.submit(alice)
    _link(env, solo_child, solo_root, alice)
    client = TestClient(create_app(env.state))
    return (
        env,
        client,
        {
            "root": root,
            "own_child": own_child,
            "other_child": other_child,
            "solo_root": solo_root,
            "solo_child": solo_child,
        },
    )


def _campaign(client, campaign, name):
    return _get(client, f"/campaigns/{campaign['id']}", name)


def test_reruns_listing_holds_only_the_readers_own_links(reruns):
    _, client, c = reruns
    alice = _campaign(client, c["root"], "alice").json()
    assert [r["campaign_id"] for r in alice["reruns"]] == [c["own_child"]["id"]]
    assert c["other_child"]["id"] not in json.dumps(alice)
    human = _campaign(client, c["root"], "human").json()
    assert {r["campaign_id"] for r in human["reruns"]} == {
        c["own_child"]["id"],
        c["other_child"]["id"],
    }


def test_rerun_of_is_dropped_when_the_source_belongs_to_another_agent(reruns):
    _, client, c = reruns
    # bob reads his own re-run of alice's campaign: alice's campaign is never named to him.
    bob = _campaign(client, c["other_child"], "bob").json()
    assert "rerun" not in bob and "reruns" not in bob
    for hidden in (c["root"], c["own_child"]):
        assert hidden["id"] not in json.dumps(bob)
    # alice reads her own re-run: the link to her source stays.
    alice = _campaign(client, c["own_child"], "alice").json()
    assert alice["rerun"]["rerun_of"] == c["root"]["id"]
    assert _campaign(client, c["own_child"], "human").json()["rerun"]["rerun_of"] == c["root"]["id"]
    # bob cannot read alice's campaigns at all, and gets the answer a missing one gets.
    refused = _campaign(client, c["root"], "bob")
    assert refused.status_code == 404
    assert refused.json() == _get(client, f"/campaigns/{MISSING}", "bob").json()


def test_rerun_tree_is_withheld_when_it_holds_a_campaign_the_reader_may_not_read(reruns):
    _, client, c = reruns
    alice = _campaign(client, c["root"], "alice").json()
    assert alice["rerun_tree"] == {
        "withheld": "the re-run tree includes campaigns another principal submitted"
    }
    bob = _campaign(client, c["other_child"], "bob").json()
    assert set(bob["rerun_tree"]) == {"withheld"}
    # The marker names no campaign, root, member or amount of the unreadable ones.
    unreadable = (c["root"], c["own_child"])
    for campaign in unreadable:
        assert campaign["id"] not in json.dumps(bob["rerun_tree"])
    human = _campaign(client, c["root"], "human").json()["rerun_tree"]
    assert human["root_campaign_id"] == c["root"]["id"]
    assert {m["campaign_id"] for m in human["members"]} == {
        c["root"]["id"],
        c["own_child"]["id"],
        c["other_child"]["id"],
    }


def test_rerun_tree_is_shown_in_full_when_every_member_is_the_readers_own(reruns):
    _, client, c = reruns
    for campaign in (c["solo_root"], c["solo_child"]):
        tree = _campaign(client, campaign, "alice").json()["rerun_tree"]
        assert tree["root_campaign_id"] == c["solo_root"]["id"]
        assert {m["campaign_id"] for m in tree["members"]} == {
            c["solo_root"]["id"],
            c["solo_child"]["id"],
        }
        assert tree["truncated"] is False


def test_a_truncated_tree_is_withheld_even_when_every_member_read_is_own(reruns, monkeypatch):
    _, client, c = reruns
    monkeypatch.setattr(campaign_rerun, "MAX_TREE", 1)
    alice = _campaign(client, c["solo_root"], "alice").json()
    assert set(alice["rerun_tree"]) == {"withheld"}  # unseen members could be another agent's
    assert c["solo_child"]["id"] not in json.dumps(alice["rerun_tree"])
    human = _campaign(client, c["solo_root"], "human").json()["rerun_tree"]
    assert human["truncated"] is True and len(human["members"]) == 1
    # Alice still sees her own link, only the totals are withheld.
    assert [r["campaign_id"] for r in alice["reruns"]] == [c["solo_child"]["id"]]


# ---- (c) campaign_withheld on submission approvals --------------------------------------------


@pytest.fixture
def submissions(tmp_path):
    program_budget.reset()
    sub = Sub(tmp_path)
    actions = ["campaign:read", SUBMIT, "campaign:submit"]
    alice = sub.agent(actions, "alice")
    bob = sub.agent(actions, "bob")
    sub.env.human("human")
    record_a = sub.create()
    record_b = sub.create()
    pending = sub.create()
    status_a, started_a = sub.submit(record_a, actor=alice)
    status_b, started_b = sub.submit(record_b, actor=bob)
    assert status_a == status_b == 201
    client = TestClient(create_app(sub.env.state))
    yield sub, client, record_a, record_b, pending, started_a, started_b
    program_budget.reset()


def _campaign_ids(sub, *records):
    return [
        sub.env.query("SELECT campaign_id FROM campaign_submission_approvals WHERE id=?", r["id"])[
            0
        ][0]
        for r in records
    ]


def test_submission_approval_list_withholds_the_campaign_another_agent_started(submissions):
    sub, client, record_a, record_b, pending, _, _ = submissions
    camp_a, camp_b = _campaign_ids(sub, record_a, record_b)
    assert camp_a and camp_b and camp_a != camp_b
    path = f"/projects/{sub.env.project['id']}/submission-approvals?limit=200"
    for reader, own, other, own_record, other_record in (
        ("alice", camp_a, camp_b, record_a, record_b),
        ("bob", camp_b, camp_a, record_b, record_a),
    ):
        response = _get(client, path, reader)
        assert response.status_code == 200
        rows = {r["id"]: r for r in response.json()["submission_approvals"]}
        assert set(rows) == {record_a["id"], record_b["id"], pending["id"]}
        assert rows[own_record["id"]]["campaign_id"] == own  # own data still there
        assert "campaign_withheld" not in rows[own_record["id"]]
        hidden = rows[other_record["id"]]
        assert hidden["campaign_id"] is None and hidden["approval_id"] is None
        assert hidden["campaign_withheld"] is True
        assert other not in response.text
        assert "campaign_withheld" not in rows[pending["id"]]
    human = _get(client, path, "human").json()["submission_approvals"]
    assert {r["campaign_id"] for r in human if r["id"] != pending["id"]} == {camp_a, camp_b}


def test_one_submission_approval_withholds_the_submitted_decision_actor_and_details(submissions):
    sub, client, record_a, _, _, _, _ = submissions
    (camp_a,) = _campaign_ids(sub, record_a)
    path = f"/submission-approvals/{record_a['id']}"
    mine = _get(client, path, "alice").json()
    assert mine["campaign_id"] == camp_a and "campaign_withheld" not in mine
    submitted = [d for d in mine["decisions"] if d["decision"] == "submitted"]
    assert len(submitted) == 1
    assert submitted[0]["actor_principal_id"] and submitted[0]["details"]
    theirs = _get(client, path, "bob")
    body = theirs.json()
    assert body["campaign_withheld"] is True and body["campaign_id"] is None
    (hidden,) = [d for d in body["decisions"] if d["decision"] == "submitted"]
    assert hidden["details"] == {} and hidden["actor_principal_id"] is None
    assert camp_a not in theirs.text
    assert submitted[0]["actor_principal_id"] not in theirs.text


# ---- (d) standing status starts and triggers --------------------------------------------------


@pytest.fixture
def standing(tmp_path):
    env = Env(tmp_path)
    alice = env.agent(ACTIONS, "alice")
    bob = env.agent(ACTIONS, "bob")
    human = env.human("human")
    org = env.query("SELECT org_id FROM projects WHERE id=?", env.project["id"])[0][0]
    spec = env.submit()  # only for a spec to hold in the template column
    spec_json = env.query("SELECT spec_json, spec_digest FROM campaigns WHERE id=?", spec["id"])[0]
    campaigns = {
        "alice": env.approve(env.submit(alice)),
        "bob": env.approve(env.submit(bob)),
        "human": env.approve(env.submit()),
    }
    env.execute(
        "INSERT INTO campaign_standing_approvals (id, org_id, project_id, spec_json, spec_digest, "
        "created_by_principal_id, decision_class, trust_policy_digest, "
        'per_start_budget_cap_cents, max_starts, max_total_budget_cents, "trigger", '
        "not_after, status, revision, created_at, updated_at) "
        "VALUES ('standing_1',?,?,?,?,?,'human_session',?,5,10,50,'schedule',?, 'active',1,?,?)",
        org,
        env.project["id"],
        spec_json[0],
        spec_json[1],
        human["id"],
        "t" * 64,
        "2999-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
    )
    for index, campaign in enumerate(campaigns.values(), start=1):
        approval = env.query(
            "SELECT id FROM campaign_approvals WHERE campaign_id=?", campaign["id"]
        )
        env.execute(
            "INSERT INTO campaign_standing_starts (standing_approval_id, start_index, campaign_id, "
            "approval_id, kind, trigger_campaign_id, retry_number, budget_cap_cents, created_at) "
            "VALUES ('standing_1',?,?,?,'scheduled',NULL,0,5,'2026-01-01T00:00:00Z')",
            index,
            campaign["id"],
            approval[0][0],
        )
        env.execute(
            "INSERT INTO campaign_standing_triggers (trigger_campaign_id, kind, "
            "target_standing_approval_id, retry_number, status, created_at) "
            "VALUES (?,'follow_on','standing_1',0,'pending','2026-01-01T00:00:00Z')",
            campaign["id"],
        )
    return env, TestClient(create_app(env.state)), campaigns


def test_standing_status_starts_and_triggers_are_filtered_to_the_readers_campaigns(standing):
    env, client, campaigns = standing
    path = f"/projects/{env.project['id']}/agent-standing-status"
    for reader in ("alice", "bob"):
        response = _get(client, path, reader)
        assert response.status_code == 200
        (item,) = response.json()["standing_approvals"]
        own = campaigns[reader]["id"]
        assert [s["campaign_id"] for s in item["starts"]] == [own]
        assert [t["trigger_campaign_id"] for t in item["triggers"]] == [own]
        for other in campaigns:
            if other != reader:
                assert campaigns[other]["id"] not in response.text
    # A human reads every start and trigger of the schedule.
    (item,) = _get(client, path, "human").json()["standing_approvals"]
    assert {s["campaign_id"] for s in item["starts"]} == {c["id"] for c in campaigns.values()}
    assert {t["trigger_campaign_id"] for t in item["triggers"]} == {
        c["id"] for c in campaigns.values()
    }


# ---- (e) delegation escalations and decisions ------------------------------------------------


@pytest.fixture
def delegation(tmp_path):
    world = World(tmp_path)
    world.env.human("human")
    scopes = (DECIDE_SCOPE, "campaign:read", "campaign:submit")
    alice = world.agent(scopes, name="alice")
    bob = world.agent(scopes, name="bob")
    record = world.create(terms=_terms(per_run=5))
    own_a = world.env.submit(alice, world.env.spec(budget_cap_cents=10, name="alice one"))
    own_b = world.env.submit(bob, world.env.spec(budget_cap_cents=10, name="bob one"))
    cross = world.env.submit(alice, world.env.spec(budget_cap_cents=10, name="alice two"))
    for campaign, actor in ((own_a, alice), (own_b, bob), (cross, bob)):
        with pytest.raises(DelegationRefused):
            world.approve(campaign, record, actor=actor)  # over the per-run cap: escalated
    client = TestClient(create_app(world.env.state))
    return world, client, record, {"a": own_a, "b": own_b, "cross": cross}


def test_delegation_escalations_show_a_second_agent_only_its_own(delegation):
    world, client, _, c = delegation
    path = f"/projects/{world.project}/delegation-escalations"
    alice = _get(client, path, "alice")
    bob = _get(client, path, "bob")
    human = _get(client, path, "human")
    assert {e["campaign_id"] for e in alice.json()["escalations"]} == {
        c["a"]["id"],
        c["cross"]["id"],
    }
    # bob sees his own campaign and the attempt he made himself, never alice's other campaign.
    assert {e["campaign_id"] for e in bob.json()["escalations"]} == {c["b"]["id"], c["cross"]["id"]}
    assert c["a"]["id"] not in bob.text and c["b"]["id"] not in alice.text
    assert {e["campaign_id"] for e in human.json()["escalations"]} == {
        c["a"]["id"],
        c["b"]["id"],
        c["cross"]["id"],
    }


def test_delegation_decisions_listing_and_detail_show_a_second_agent_only_its_own(delegation):
    world, client, record, c = delegation
    path = f"/approval-delegations/{record['id']}/decisions"
    alice = _get(client, path, "alice")
    bob = _get(client, path, "bob")
    assert {d["campaign_id"] for d in alice.json()["decisions"]} == {c["a"]["id"], c["cross"]["id"]}
    assert {d["campaign_id"] for d in bob.json()["decisions"]} == {c["b"]["id"], c["cross"]["id"]}
    assert c["a"]["id"] not in bob.text
    assert len(_get(client, path, "human").json()["decisions"]) == 3
    detail = f"/approval-delegations/{record['id']}"
    for reader, expected in (("alice", {c["a"]["id"], c["cross"]["id"]}),):
        body = _get(client, detail, reader).json()
        assert {d["campaign_id"] for d in body["decisions"]} == expected
    bob_detail = _get(client, detail, "bob")
    assert c["a"]["id"] not in bob_detail.text
    assert {d["campaign_id"] for d in bob_detail.json()["decisions"]} == {
        c["b"]["id"],
        c["cross"]["id"],
    }
    # The usage totals sum every campaign the delegation decided: a person reads them.
    for reader in ("alice", "bob"):
        assert set(_get(client, detail, reader).json()["usage"]) == {"withheld"}
    assert "committed_cents" in _get(client, detail, "human").json()["usage"]


# ---- (f) MCP read tools ----------------------------------------------------------------------

AGENT = ACTIONS
BY_CAMPAIGN = [
    "tessera_get_campaign",
    "tessera_get_campaign_receipt",
    "tessera_campaign_report",
    "tessera_campaign_outputs",
    "tessera_campaign_logs",
    "tessera_campaign_stage_watch",
    "tessera_campaign_stage_status",
    "tessera_campaign_budget",
    "tessera_campaign_retries",
    "tessera_campaign_gate_decisions",
    "tessera_campaign_holdout",
    "tessera_campaign_grants",
]


@pytest.fixture
def mcp(tmp_path):
    env = Env(tmp_path)
    alice = env.agent(AGENT, "alice")
    env.agent(AGENT, "bob")
    env.human("human")
    mine = env.approve(env.submit(alice))
    app = create_app(env.state)

    def caller(name):
        reference = tmp_path / f"{name}.token"
        reference.write_text(f"synthetic-{name}-token-" + "x" * 30)
        reference.chmod(0o600)
        client = ControlClient(
            Config("http://127.0.0.1:8765", reference, env.project["id"], "planner.local"),
            httpx.ASGITransport(app=app),
        )
        server = create_server(client, campaign_tools=True, campaign_actions=frozenset(AGENT))

        def call(tool, **arguments):
            result = asyncio.run(server.call_tool(tool, arguments))
            return result[1] if isinstance(result, tuple) else result

        return call

    return env, mine, caller("alice"), caller("bob")


@pytest.mark.parametrize("tool", BY_CAMPAIGN)
def test_mcp_read_tool_answers_a_second_agent_as_if_the_campaign_did_not_exist(mcp, tool):
    _, mine, alice, bob = mcp
    # The submitter reaches the campaign (a receipt exists only once it finishes).
    try:
        found = alice(tool, campaign_id=mine["id"])
    except Exception as error:  # noqa: BLE001
        assert tool == "tessera_get_campaign_receipt" and "404" in str(error)
    else:
        assert found
    for campaign_id in (mine["id"], MISSING):
        with pytest.raises(Exception) as caught:  # noqa: B017
            bob(tool, campaign_id=campaign_id)
        assert "404" in str(caught.value)
        assert mine["id"] not in str(caught.value)
    with pytest.raises(Exception) as hidden:  # noqa: B017
        bob(tool, campaign_id=mine["id"])
    with pytest.raises(Exception) as missing:  # noqa: B017
        bob(tool, campaign_id=MISSING)
    assert str(hidden.value).replace(mine["id"], "X") == str(missing.value).replace(MISSING, "X")


def test_mcp_wave_receipts_report_another_agents_campaign_as_unreadable_and_name_nothing(mcp):
    _, mine, alice, bob = mcp
    own = alice("tessera_campaign_wave_receipts", campaign_ids=[mine["id"]])["waves"]
    assert own[0]["status"] == "running" and "error" not in own[0]
    waves = bob("tessera_campaign_wave_receipts", campaign_ids=[mine["id"], MISSING])["waves"]
    hidden, missing = waves
    assert hidden["error"] == missing["error"] and "404" in hidden["error"]
    for entry in waves:
        assert set(entry) == {"campaign_id", "error"}  # no status, receipt or gate decisions


def test_mcp_list_tools_hold_only_the_callers_campaigns(mcp):
    env, mine, alice, bob = mcp
    bobs = env.submit(env.agent(AGENT, "bob"))
    listed = alice("tessera_list_campaigns")["campaigns"]
    assert [c["id"] for c in listed] == [mine["id"]]
    seen = bob("tessera_list_campaigns")
    assert [c["id"] for c in seen["campaigns"]] == [bobs["id"]]
    assert mine["id"] not in json.dumps(seen)
    assert bob("tessera_campaign_holdout_claims")["claims"] == []
    assert mine["id"] not in json.dumps(bob("tessera_campaign_standing_status"))
