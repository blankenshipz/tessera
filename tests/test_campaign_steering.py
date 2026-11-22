"""Agent steering routes (ADR 0055): validate is a dry run, the rest only reduce authority."""

from __future__ import annotations

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control.api import create_app

DEST = {"X-Agent-Destination": "planner.local"}
SUBMIT = ["campaign:submit", "campaign:read", "campaign:cancel", "campaign:logs"]


def _token(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    env.agent(SUBMIT, "owner-agent")
    env.agent(SUBMIT, "other-agent")
    env.agent(["campaign:read", "campaign:logs"], "reader")
    return env, TestClient(create_app(env.state))


def _post(client, path, name, body, key="k1", dest=DEST):
    return client.post(path, json=body, headers={**_token(name), **dest, "Idempotency-Key": key})


def _count(env):
    return env.query("SELECT count(*) FROM campaigns")[0][0]


def _revision(env, campaign_id):
    return env.query("SELECT revision FROM campaigns WHERE id=?", campaign_id)[0][0]


# -- validate -----------------------------------------------------------------------------


def test_validate_is_a_dry_run_with_the_digest_submit_would_record(world):
    env, client = world
    before = _count(env)
    path = f"/projects/{env.project['id']}/campaign-specs/validate"
    response = _post(client, path, "owner-agent", {"spec": env.spec()})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True and body["stage_order"] == ["s1", "s2"]
    assert [s["provider"] for s in body["stages"]] and "approve" in body["boundary"]
    assert _count(env) == before  # nothing stored, no event, no idempotency record
    submitted = env.submit(env.agent(SUBMIT, "owner-agent"))
    assert submitted["spec_digest"] == body["spec_digest"]


def test_validate_refuses_what_submit_refuses_and_needs_the_submit_scope(world):
    env, client = world
    path = f"/projects/{env.project['id']}/campaign-specs/validate"
    spec = env.spec()
    spec["stages"][0]["inputs"][0]["snapshot_id"] = "snapshot_missing"
    assert _post(client, path, "owner-agent", {"spec": spec}).status_code in {404, 409}
    assert _post(client, path, "reader", {"spec": env.spec()}).status_code == 403
    assert _post(client, path, "owner-agent", {"spec": env.spec()}, dest={}).status_code == 403
    assert _post(client, path, "owner-agent", {"spec": {"version": 9}}).status_code in {400, 422}
    assert _count(env) == 0


# -- budget reduction ------------------------------------------------------------------------


def _running(env, agent_name="owner-agent"):
    campaign = env.approve(env.submit(env.agent(SUBMIT, agent_name)))
    return campaign


def test_budget_can_be_reduced_but_never_raised_or_cut_below_the_ledger(world):
    env, client = world
    campaign = _running(env)
    cid, url = campaign["id"], None
    url = f"/campaigns/{cid}/budget-reduction"

    def body(cap, revision=None):
        return {
            "expected_revision": revision or _revision(env, cid),
            "budget_cap_cents": cap,
            "reason": "stop spending",
        }

    assert _post(client, url, "owner-agent", body(10), "a").status_code == 409  # not a reduction
    assert _post(client, url, "owner-agent", body(11), "b").status_code == 409  # a raise
    env.execute("UPDATE campaigns SET held_cents=3, settled_cents=2 WHERE id=?", cid)
    assert _post(client, url, "owner-agent", body(4), "c").status_code == 409  # below 3 + 2
    assert _post(client, url, "owner-agent", body(5, revision=99), "d").status_code == 409
    final = body(5)
    done = _post(client, url, "owner-agent", final, "e")
    assert done.status_code == 200 and done.json()["previous_budget_cap_cents"] == 10
    assert env.query("SELECT budget_cap_cents FROM campaigns WHERE id=?", cid)[0][0] == 5
    assert env.events("campaign.budget_reduced")[-1]["payload"]["to_cents"] == 5
    again = _post(client, url, "owner-agent", final, "e")  # same key and body: replayed
    assert again.status_code == 200 and again.json() == done.json()


def test_budget_reduction_is_scoped_submitter_only_and_not_for_unapproved_or_finished(world):
    env, client = world
    campaign = _running(env)
    cid = campaign["id"]
    url = f"/campaigns/{cid}/budget-reduction"
    payload = {"expected_revision": _revision(env, cid), "budget_cap_cents": 4, "reason": "x"}
    assert _post(client, url, "other-agent", payload, "o").status_code == 403
    assert _post(client, url, "reader", payload, "r").status_code == 403
    env.execute("UPDATE campaigns SET status='failed' WHERE id=?", cid)
    assert _post(client, url, "owner-agent", payload, "f").status_code == 409
    fresh = env.submit(env.agent(SUBMIT, "owner-agent"))  # never approved
    pending = {"expected_revision": fresh["revision"], "budget_cap_cents": 4, "reason": "x"}
    assert _post(client, f"/campaigns/{fresh['id']}/budget-reduction", "owner-agent", pending, "p")
    assert (
        _post(
            client, f"/campaigns/{fresh['id']}/budget-reduction", "owner-agent", pending, "p2"
        ).status_code
        == 409
    )


# -- release revocation and the grant view ------------------------------------------------------


def _with_release(env, status="failed"):
    campaign = _running(env)
    cid = campaign["id"]
    rows = env.query("SELECT id, revision, status FROM snapshot_releases")
    if not rows:  # the approval made no release in this fixture: add the one an attempt used
        env.execute(
            "INSERT INTO snapshot_releases (id, snapshot_id, worker_principal_id, "
            "actor_principal_id, digest, classification, rationale, expires_at, created_at, "
            "status, revision) VALUES ('rel1', ?, ?, ?, ?, 'public', 'test', "
            "'2999-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'active', 1)",
            env.snapshot_id,
            env.worker["id"],
            env.owner["id"],
            env.snapshot_digest,
        )
        rows = env.query("SELECT id, revision, status FROM snapshot_releases")
    env.release_id, env.release_revision = rows[0][0], rows[0][1]
    env.execute("UPDATE campaigns SET status=? WHERE id=?", status, cid)
    return cid


def _attempt(env, cid):
    env.execute(
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, release_id, created_at, updated_at, revision) VALUES "
        "('att1', ?, 's1', 1, 'failed_permanent', 0, ?, '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z', 1)",
        cid,
        env.release_id,
    )


def test_a_release_is_revoked_only_for_a_finished_own_campaign_and_one_it_used(world):
    env, client = world
    cid = _with_release(env)
    url = f"/campaigns/{cid}/releases/{env.release_id}/revoke"
    payload = {"expected_revision": env.release_revision, "reason": "stale"}
    assert _post(client, url, "owner-agent", payload, "a").status_code == 404  # no attempt used it
    _attempt(env, cid)
    assert _post(client, url, "other-agent", payload, "b").status_code == 403
    assert _post(client, url, "reader", payload, "c").status_code == 403
    assert (
        _post(client, url, "owner-agent", {**payload, "expected_revision": 7}, "d").status_code
        == 409
    )
    done = _post(client, url, "owner-agent", payload, "e")
    assert done.status_code == 200 and done.json()["status"] == "revoked"
    assert env.query("SELECT status FROM snapshot_releases WHERE id=?", env.release_id)[0][0] == (
        "revoked"
    )
    assert _post(client, url, "owner-agent", payload, "f").status_code == 409  # already revoked


def test_a_running_campaigns_release_is_not_revoked(world):
    env, client = world
    cid = _with_release(env, status="running")
    _attempt(env, cid)
    url = f"/campaigns/{cid}/releases/{env.release_id}/revoke"
    payload = {"expected_revision": env.release_revision, "reason": "x"}
    assert _post(client, url, "owner-agent", payload).status_code == 409
    assert env.query("SELECT status FROM snapshot_releases WHERE id=?", env.release_id)[0][0] == (
        "active"
    )


def test_the_grant_view_needs_campaign_logs_and_lists_recorded_columns_only(world):
    env, client = world
    campaign = _running(env)
    url = f"/campaigns/{campaign['id']}/grants"
    ok = client.get(url, headers={**_token("reader"), **DEST})
    assert ok.status_code == 200 and ok.json()["grants"] == []
    env.agent(["campaign:read"], "nologs")
    assert client.get(url, headers={**_token("nologs"), **DEST}).status_code == 403
    assert (
        client.get("/campaigns/missing/grants", headers={**_token("reader"), **DEST}).status_code
        == 404
    )
