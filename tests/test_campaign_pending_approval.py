"""What a campaign waits for a person to decide is shown live and clears when decided (G-11)."""

from __future__ import annotations

import sqlite3

from campaign_fixture import Env

from tessera_control.campaign_pending_approval import pending_human_approval


def _view(env, campaign):
    return env.store.get_campaign(env.owner, campaign["id"])["pending_human_approval"]


def test_a_submitted_campaign_awaits_approval_and_it_clears_once_approved(tmp_path):
    env = Env(tmp_path)
    submitted = env.submit()
    (item,) = _view(env, submitted)
    assert item["kind"] == "campaign_approval"
    assert item["spec_digest"] == submitted["spec_digest"] and "approval" in item["awaiting"]
    env.approve(submitted)
    assert _view(env, submitted) == []


def test_a_cancelled_unapproved_campaign_awaits_nothing(tmp_path):
    env = Env(tmp_path)
    submitted = env.submit()
    env.execute("UPDATE campaigns SET status='cancelled' WHERE id=?", submitted["id"])
    assert _view(env, submitted) == []


def test_a_lapsed_approval_awaits_a_reapproval_and_it_clears_after_one(tmp_path):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    env.execute("UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", campaign["id"])
    current = env.store.get_campaign(env.owner, campaign["id"])
    assert [i["kind"] for i in current["pending_human_approval"]] == ["campaign_reapproval"]
    env.approve(current, env.human("second"))
    assert _view(env, campaign) == []


def _salad_review(env, campaign, status="pending"):
    """A Salad attempt waiting at its plan review, with a decision request in ``status``."""
    db = sqlite3.connect(env.state / "tessera.sqlite3")  # foreign keys off: rows only
    try:
        db.execute(
            "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
            "hold_cents, revision, created_at, updated_at) "
            "VALUES ('att1', ?, 's1', 1, 'reserved', 0, 1, 'now', 'now')",
            (campaign["id"],),
        )
        db.execute(
            "INSERT INTO campaign_salad_attempts (attempt_id, phase, plan_decision_id, revision, "
            "updated_at) VALUES ('att1', 'awaiting_review', 'dec1', 1, 'now')"
        )
        db.execute(
            "INSERT INTO decision_requests (id, org_id, project_id, kind, scope_json, "
            "scope_digest, reviewer_principal_id, requester_principal_id, status, revision, "
            "expires_at, priority, created_at) VALUES ('dec1', 'o', 'p', 'spending', '{}', 'd', "
            "'r', 'q', ?, 1, 'later', 1, 'now')",
            (status,),
        )
        db.commit()
    finally:
        db.close()


def test_a_pending_salad_review_is_listed_and_clears_when_decided_or_refused(tmp_path):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    _salad_review(env, campaign)
    (item,) = _view(env, campaign)
    assert item == {
        "kind": "salad_review",
        "stage_key": "s1",
        "attempt": 1,
        "review": "plan",
        "awaiting": "a person's decision on the plan review",
        "decision_request_id": "dec1",
    }
    for decided in ("approved", "rejected", "expired"):
        env.execute("UPDATE decision_requests SET status=? WHERE id='dec1'", decided)
        assert _view(env, campaign) == [], decided


def test_the_derivation_reads_state_only_and_stores_nothing(tmp_path):
    env = Env(tmp_path)
    submitted = env.submit()
    before = env.query("SELECT count(*) AS n FROM sqlite_master")[0]["n"]
    with env.store.runner_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (submitted["id"],)
        ).fetchone()
        first = pending_human_approval(connection, row)
        assert first == pending_human_approval(connection, row)  # stable, no side effects
    assert env.query("SELECT count(*) AS n FROM sqlite_master")[0]["n"] == before
