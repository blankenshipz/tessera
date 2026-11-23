"""A campaign's total wall-clock bound is fixed at its first approval and recorded on the receipt
(audit G-13): re-approval cannot extend it silently."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env

from tessera_control import campaign_receipts
from tessera_control.store import Conflict

DEADLINE = 3600


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _age_first_approval(env, approval, seconds):
    """Make the campaign's first approval ``seconds`` old."""
    moved = (_utc(approval["approved_at"]) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    env.execute("UPDATE campaign_approvals SET approved_at=? WHERE id=?", moved, approval["id"])
    return moved


def _lapse(env, campaign):
    env.execute("UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", campaign["id"])
    return env.store.get_campaign(env.owner, campaign["id"])


def test_reapproval_gets_only_the_time_that_is_left(tmp_path):
    env = Env(tmp_path)
    campaign, first = env.approved()
    first_at = _age_first_approval(env, first, DEADLINE - 600)  # 600 seconds left
    second = env.approve(_lapse(env, campaign), env.human("second"))["approval"]
    bound = _utc(first_at) + timedelta(seconds=DEADLINE)
    assert _utc(second["expires_at"]) == bound  # not now + 3600
    assert _utc(second["expires_at"]) - datetime.now(UTC) < timedelta(seconds=700)


def test_reapproval_after_the_bound_is_refused_and_changes_nothing(tmp_path):
    env = Env(tmp_path)
    campaign, first = env.approved()
    _age_first_approval(env, first, DEADLINE + 5)
    current = _lapse(env, campaign)
    with pytest.raises(Conflict, match="deadline has passed"):
        env.approve(current, env.human("second"))
    rows = env.query("SELECT status FROM campaign_approvals")
    assert len(rows) == 1  # nothing was superseded or added


def test_repeated_reapprovals_never_move_the_bound(tmp_path):
    env = Env(tmp_path)
    campaign, first = env.approved()
    first_at = _age_first_approval(env, first, 1200)
    bound = _utc(first_at) + timedelta(seconds=DEADLINE)
    for name in ("second", "third", "fourth"):
        approval = env.approve(_lapse(env, campaign), env.human(name))["approval"]
        assert _utc(approval["expires_at"]) == bound


def test_the_receipt_records_the_bound_and_that_reapproval_did_not_extend_it(tmp_path):
    env = Env(tmp_path)
    campaign, first = env.approved()
    first_at = _age_first_approval(env, first, 1200)
    env.approve(_lapse(env, campaign), env.human("second"))
    with env.store.runner_connection() as connection:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()
        receipt, _ = campaign_receipts.build_receipt(connection, row, "cancelled")
    clock = receipt["wall_clock"]
    assert clock == {
        "deadline_seconds": DEADLINE,
        "first_approved_at": first_at,
        "bound_at": (_utc(first_at) + timedelta(seconds=DEADLINE)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approvals": 2,
        "extended_by_reapproval": False,
    }
    assert receipt["approval"]["expires_at"] == clock["bound_at"]  # the active approval agrees


def test_a_campaign_that_was_never_approved_has_no_wall_clock_section(tmp_path):
    env = Env(tmp_path)
    submitted = env.submit()
    with env.store.runner_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (submitted["id"],)
        ).fetchone()
        receipt, _ = campaign_receipts.build_receipt(connection, row, "cancelled")
    assert "wall_clock" not in receipt and receipt["approval"] is None
