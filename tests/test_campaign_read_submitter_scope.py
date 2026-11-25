"""Audit O-05: an agent credential reads only the campaigns it submitted; a human reads the org's.

Another agent's campaign answers exactly like a campaign that does not exist, and project-wide
lists, counts and pages hold only the caller's own rows.
"""

from __future__ import annotations

import json

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers

from tessera_control.api import create_app

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
    headers = _token(name) if name in {"human", "owner"} else {**_token(name), **DEST}
    return client.get(path, headers=headers)


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    alice = env.agent(ACTIONS, "alice")
    bob = env.agent(ACTIONS, "bob")
    env.human("human")
    mine = env.approve(env.submit(alice))
    theirs = env.approve(env.submit(bob))
    by_human = env.submit()
    org = env.query("SELECT org_id FROM projects WHERE id=?", env.project["id"])[0][0]
    client = TestClient(create_app(env.state))
    return env, client, org, mine, theirs, by_human


def test_agents_are_distinct_principals(world):
    env, _, _, mine, theirs, _ = world
    rows = dict(env.query("SELECT id, submitted_by_principal_id FROM campaigns"))
    assert rows[mine["id"]] != rows[theirs["id"]]


PER_CAMPAIGN = [
    "/campaigns/{id}",
    "/campaigns/{id}/receipt",
    "/campaigns/{id}/report",
    "/campaigns/{id}/gate-decisions",
    "/campaigns/{id}/retries",
    "/campaigns/{id}/holdout",
    "/campaigns/{id}/outputs",
    "/campaigns/{id}/logs",
    "/campaigns/{id}/stage-status",
    "/campaigns/{id}/grants",
    "/campaigns/{id}/budget",
    "/campaigns/{id}/stage-watch",
]


@pytest.mark.parametrize("template", PER_CAMPAIGN)
def test_other_agents_campaign_is_indistinguishable_from_missing(world, template):
    _, client, _, _, theirs, by_human = world
    missing = _get(client, template.format(id=MISSING), "alice")
    assert missing.status_code == 404
    for hidden in (theirs["id"], by_human["id"]):
        response = _get(client, template.format(id=hidden), "alice")
        assert response.status_code == 404, template
        assert response.json() == missing.json()
        assert hidden not in response.text


@pytest.mark.parametrize("template", PER_CAMPAIGN)
def test_agent_reads_its_own_campaign(world, template):
    _, client, _, mine, _, _ = world
    response = _get(client, template.format(id=mine["id"]), "alice")
    # The receipt exists only once a campaign finishes; its 404 names the receipt, not the
    # campaign, so the campaign itself was found.
    if template.endswith("/receipt"):
        assert response.status_code == 404
        assert "receipt" in response.json()["detail"].lower()
    else:
        assert response.status_code == 200, (template, response.text)


@pytest.mark.parametrize("template", ["/campaigns/{id}", "/campaigns/{id}/report"])
def test_human_reads_every_campaign_in_the_org(world, template):
    _, client, _, mine, theirs, by_human = world
    for campaign in (mine, theirs, by_human):
        assert _get(client, template.format(id=campaign["id"]), "human").status_code == 200


def test_other_org_human_sees_none(world):
    env, client, _, mine, theirs, by_human = world
    outsider = _other_tenant_headers(env.state)
    for campaign in (mine, theirs, by_human):
        assert client.get(f"/campaigns/{campaign['id']}", headers=outsider).status_code == 404
    listed = client.get(f"/projects/{env.project['id']}/campaigns", headers=outsider)
    assert listed.status_code == 404


def test_list_holds_only_own_rows_and_pages_do_not_leak(world):
    env, client, _, mine, theirs, by_human = world
    for _ in range(3):
        env.submit(env.agent(ACTIONS, "bob"))
    extra = env.submit(env.agent(ACTIONS, "alice"))
    base = f"/projects/{env.project['id']}/campaigns"
    own = [c["id"] for c in _get(client, base + "?limit=200", "alice").json()["campaigns"]]
    assert own == [mine["id"], extra["id"]]
    paged = []
    for offset in range(0, 6):
        page = _get(client, f"{base}?offset={offset}&limit=1", "alice").json()["campaigns"]
        paged.extend(c["id"] for c in page)
    assert paged == own
    assert _get(client, f"{base}?offset=2&limit=50", "alice").json()["campaigns"] == []
    every = [c["id"] for c in _get(client, base + "?limit=200", "human").json()["campaigns"]]
    assert {mine["id"], theirs["id"], by_human["id"], extra["id"]} <= set(every)
    assert len(every) == 7


def test_window_report_counts_only_own_campaigns(world):
    env, client, _, mine, _, _ = world
    path = (
        f"/projects/{env.project['id']}/campaign-report"
        "?since=2000-01-01T00:00:00Z&until=2999-01-01T00:00:00Z&detail=true"
    )
    agent = _get(client, path, "alice").json()
    assert agent["totals"]["campaigns_matching"] == 1
    assert [e["ids"]["campaign_id"] for e in agent["campaigns"]] == [mine["id"]]
    assert _get(client, path, "human").json()["totals"]["campaigns_matching"] == 3


def _claim(env, org, campaign, partition):
    approval = env.query("SELECT id FROM campaign_approvals WHERE campaign_id=?", campaign["id"])[
        0
    ][0]
    env.execute(
        "INSERT INTO campaign_holdout_claims (id, org_id, project_id, partition_digest, "
        "claim_scope, campaign_id, stage_key, input_name, approval_id, spec_digest, status, "
        "opened_at) VALUES (?,?,?,?, 'scope', ?, 's1', 'code', ?, ?, 'opened', ?)",
        "claim_" + campaign["id"][-8:],
        org,
        env.project["id"],
        partition,
        campaign["id"],
        approval,
        campaign["spec_digest"],
        "2026-01-01T00:00:00Z",
    )


def test_project_holdout_claims_hold_only_own(world):
    env, client, org, mine, theirs, _ = world
    _claim(env, org, mine, "a" * 64)
    _claim(env, org, theirs, "b" * 64)
    base = f"/projects/{env.project['id']}/holdout-claims"
    agent = _get(client, base, "alice").json()["claims"]
    assert [c["campaign_id"] for c in agent] == [mine["id"]]
    assert _get(client, base + "?partition_digest=" + "b" * 64, "alice").json()["claims"] == []
    assert len(_get(client, base, "human").json()["claims"]) == 2


def _finding(env, org, campaign, fid):
    env.execute(
        "INSERT INTO campaign_stuck_findings (id, org_id, project_id, campaign_id, code, "
        "subject, suggested_action, status, occurrences, first_seen_at, last_seen_at, "
        "evidence_json, receipt_sha256) VALUES (?,?,?,?, 'no_progress', 'x', 'look', 'open', 1, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{}', ?)",
        fid,
        org,
        env.project["id"],
        campaign["id"],
        "f" * 64,
    )


def test_stuck_findings_list_and_acknowledge_are_own_only(world):
    env, client, org, mine, theirs, _ = world
    _finding(env, org, mine, "finding_mine")
    _finding(env, org, theirs, "finding_theirs")
    base = f"/projects/{env.project['id']}/campaign-stuck"
    agent = _get(client, base + "?status=all", "alice").json()["findings"]
    assert [f["id"] for f in agent] == ["finding_mine"]
    human = _get(client, base + "?status=all", "human").json()["findings"]
    assert {f["id"] for f in human} == {"finding_mine", "finding_theirs"}
    headers = {**_token("alice"), **DEST, "Idempotency-Key": "ack-theirs"}
    refused = client.post(f"{base}/finding_theirs/acknowledge", json={}, headers=headers)
    missing = client.post(
        f"{base}/finding_nope/acknowledge",
        json={},
        headers={**headers, "Idempotency-Key": "ack-nope"},
    )
    assert refused.status_code == missing.status_code == 404
    assert refused.json() == missing.json()
    own = client.post(
        f"{base}/finding_mine/acknowledge",
        json={},
        headers={**headers, "Idempotency-Key": "ack-mine"},
    )
    assert own.status_code == 200


def test_stuck_scan_reports_only_own_findings(world):
    env, client, org, mine, theirs, _ = world
    _finding(env, org, mine, "finding_mine")
    _finding(env, org, theirs, "finding_theirs")
    headers = {**_token("alice"), **DEST, "Idempotency-Key": "scan-1"}
    body = client.post(f"/projects/{env.project['id']}/campaign-stuck/scan", headers=headers).json()
    visible = {"finding_mine"} | {f["id"] for f in body["opened"]}
    rows = dict(env.query("SELECT id, campaign_id FROM campaign_stuck_findings"))
    assert all(rows[fid] == mine["id"] for fid in visible if fid in rows)
    assert all(rows[fid] == mine["id"] for fid in body["resolved"])
    own_open = env.query(
        "SELECT COUNT(*) FROM campaign_stuck_findings WHERE campaign_id=? "
        "AND status IN ('open','acknowledged')",
        mine["id"],
    )[0][0]
    assert body["open_count"] == own_open


def test_project_spend_totals_cover_own_campaigns(world):
    env, client, _, mine, _, _ = world
    path = f"/projects/{env.project['id']}/agent-spend"
    agent = _get(client, path, "alice").json()
    assert sum(row["campaigns"] for row in agent["campaigns_by_status"]) == 1
    human = _get(client, path, "human").json()
    assert sum(row["campaigns"] for row in human["campaigns_by_status"]) == 3
    assert mine["id"] not in json.dumps(agent)


def test_rerun_of_another_agents_campaign_is_not_found(world):
    _, client, _, _, theirs, _ = world
    headers = {**_token("alice"), **DEST, "Idempotency-Key": "rerun-theirs"}
    response = client.post(
        f"/campaigns/{theirs['id']}/rerun", json={"mode": "identical"}, headers=headers
    )
    missing = client.post(
        f"/campaigns/{MISSING}/rerun",
        json={"mode": "identical"},
        headers={**headers, "Idempotency-Key": "rerun-missing"},
    )
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json()


def test_unenrolled_agent_credential_is_also_submitter_scoped(world):
    env, client, _, mine, _, _ = world
    plain = env.agent(None, "plain")
    assert plain["role"] == "agent"
    response = client.get(f"/campaigns/{mine['id']}", headers=_token("plain"))
    assert response.status_code == 404
    listed = client.get(f"/projects/{env.project['id']}/campaigns", headers=_token("plain"))
    assert listed.status_code == 200 and listed.json()["campaigns"] == []
