"""What a campaign waits for a person to decide is shown live and clears when decided (G-11)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
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


def _salad_review(env, campaign, status="pending", expires_in=timedelta(hours=1), expires_at=None):
    """A Salad attempt waiting at its plan review, with a decision request in ``status``."""
    if expires_at is None:
        expires_at = (datetime.now(UTC) + expires_in).isoformat().replace("+00:00", "Z")
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
            "'r', 'q', ?, 1, ?, 1, 'now')",
            (status, expires_at),
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
    for decided in ("approved", "rejected"):
        env.execute("UPDATE decision_requests SET status=? WHERE id='dec1'", decided)
        assert _view(env, campaign) == [], decided


@pytest.mark.parametrize(
    ("expires_at", "listed"),
    [
        ("2000-01-01T00:00:00Z", 0),  # past: cleared
        ("2999-01-01T00:00:00Z", 1),  # future: listed
        ("not a time", 1),  # unparsable: fail closed to listing
    ],
)
def test_a_pending_review_past_its_expiry_is_cleared(tmp_path, expires_at, listed):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    _salad_review(env, campaign, expires_at=expires_at)
    assert len(_view(env, campaign)) == listed


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


def test_a_pending_request_with_a_future_expiry_is_listed(tmp_path):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    _salad_review(env, campaign, expires_in=timedelta(minutes=5))
    assert [i["decision_request_id"] for i in _view(env, campaign)] == ["dec1"]


def test_view_only_a_pending_request_past_its_expiry_is_not_listed(tmp_path):
    # View-only check on a hand-built row (no real project/org). The real
    # InteractionStore.expire_decisions sweep is exercised in
    # test_campaign_pending_approval_runner.py against rows the runner wrote.
    env = Env(tmp_path)
    campaign, _ = env.approved()
    _salad_review(env, campaign, expires_in=timedelta(minutes=-1))
    assert env.query("SELECT status FROM decision_requests WHERE id='dec1'")[0]["status"] == (
        "pending"
    )
    assert _view(env, campaign) == []


def test_an_unreadable_expiry_stays_listed(tmp_path):
    # Fail closed: an expiry that cannot be read keeps the request in front of a person.
    env = Env(tmp_path)
    campaign, _ = env.approved()
    _salad_review(env, campaign)
    env.execute("UPDATE decision_requests SET expires_at='later' WHERE id='dec1'")
    assert [i["decision_request_id"] for i in _view(env, campaign)] == ["dec1"]
