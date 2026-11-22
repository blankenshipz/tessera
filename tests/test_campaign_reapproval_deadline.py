"""A forward re-approval never extends the campaign's total wall-clock bound."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env

from tessera_control.store import Conflict


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _lapse(env: Env, campaign_id: str, first_age: timedelta) -> None:
    """Age the first approval and pause the campaign as a lapsed delegation would."""
    env.execute(
        "UPDATE campaign_approvals SET approved_at=? WHERE campaign_id=?",
        _stamp(datetime.now(UTC) - first_age),
        campaign_id,
    )
    env.execute("UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", campaign_id)


def test_reapproval_expiry_is_capped_at_first_approval_plus_deadline(tmp_path):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    deadline = env.store.get_campaign(env.owner, campaign["id"])["spec"]["deadline_seconds"]
    _lapse(env, campaign["id"], timedelta(seconds=deadline // 2))
    second = env.approve(env.store.get_campaign(env.owner, campaign["id"]), env.human("second"))
    first_at = env.query("SELECT MIN(approved_at) AS at FROM campaign_approvals")[0]["at"]
    expires = _parse(second["approval"]["expires_at"])
    assert expires == _parse(first_at) + timedelta(seconds=deadline)
    assert expires < datetime.now(UTC) + timedelta(seconds=deadline)


def test_reapproval_after_the_total_deadline_is_refused(tmp_path):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    deadline = env.store.get_campaign(env.owner, campaign["id"])["spec"]["deadline_seconds"]
    _lapse(env, campaign["id"], timedelta(seconds=deadline + 60))
    current = env.store.get_campaign(env.owner, campaign["id"])
    with pytest.raises(Conflict, match="deadline has passed"):
        env.approve(current, env.human("second"))


def test_a_cleanup_reapproval_after_the_total_deadline_still_gets_a_window(tmp_path):
    """A cancel must never be stranded by the deadline cap."""
    env = Env(tmp_path)
    campaign, _ = env.approved()
    deadline = env.store.get_campaign(env.owner, campaign["id"])["spec"]["deadline_seconds"]
    _lapse(env, campaign["id"], timedelta(seconds=deadline + 60))
    env.execute("UPDATE campaigns SET status='cancel_requested' WHERE id=?", campaign["id"])
    current = env.store.get_campaign(env.owner, campaign["id"])
    second = env.approve(current, env.human("second"))
    assert _parse(second["approval"]["expires_at"]) > datetime.now(UTC)
