"""A delegated approval refuses a ``not_before`` the approval or the delegation cannot outlive.

Parity with a person's approval, which refuses a start time at or after its own expiry: the
delegated path escalates (409 ``outside_delegation``) with ``not_before_after_approval_expiry``
and/or ``not_before_after_delegation_expiry``, records no approval, and widens nothing.

Synthetic fixtures only: the real stores and runner; no provider call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_campaign_not_before_spec import _v2
from test_deferred_launch_preclear import Deferred, _stamp

from tessera_control import campaign_preapproval
from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_hold import not_before_reaches
from tessera_control.campaign_pending_approval import pending_human_approval
from tessera_control.delegated_decisions import DelegationRefused
from tessera_control.store import Conflict, _connect, canonical_digest

APPROVAL_REASON = "not_before_after_approval_expiry"
DELEGATION_REASON = "not_before_after_delegation_expiry"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class World(Deferred):
    """The deferred-launch world with a chosen ``not_before`` and ``deadline_seconds``."""

    def __init__(self, tmp_path, *, not_before: datetime | None, deadline: int = 86400) -> None:
        super().__init__(tmp_path, deferred=False)
        if not_before is not None:
            self.start_at = not_before
            self.spec["not_before"] = _stamp(not_before)
        self.spec["deadline_seconds"] = deadline

    def short_delegation(self, minutes: int) -> dict:
        return self.delegate(expires_at=_stamp(_now() + timedelta(minutes=minutes)))

    def attempt(self, delegation: dict) -> tuple[dict, list[str]]:
        submitted = self.submit()
        body = {
            "delegation_id": delegation["id"],
            "expected_revision": submitted["revision"],
            "expected_spec_digest": submitted["spec_digest"],
        }
        with pytest.raises(DelegationRefused) as caught:
            self.decider.delegated_approve(
                self.human, submitted["id"], self.key("dapprove"), canonical_digest(body), body
            )
        return submitted, sorted(caught.value.reasons)

    def assert_nothing_approved(self, submitted: dict, reasons: list[str]) -> None:
        campaign_id = submitted["id"]
        assert self.row("SELECT status FROM campaigns WHERE id=?", campaign_id)["status"] == (
            "submitted"
        )
        assert self.row("SELECT 1 FROM campaign_approvals WHERE campaign_id=?", campaign_id) is None
        with _connect(self.env.state / "tessera.sqlite3") as connection:
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            kinds = [i["kind"] for i in pending_human_approval(connection, campaign)]
        assert kinds == ["campaign_approval"]
        listed = self.decider.list_delegation_escalations(self.human, self.env.project["id"])
        (escalation,) = listed["escalations"]
        assert escalation["campaign_id"] == campaign_id
        assert sorted(escalation["reasons"]) == reasons
        rows = self.env.query("SELECT outcome FROM delegated_decisions")
        assert [r["outcome"] for r in rows] == ["escalated"]


# ---- (1) inside both bounds ------------------------------------------------------------------


def test_a_start_inside_both_expiries_is_approved_and_waits(tmp_path):
    world = World(tmp_path, not_before=_now() + timedelta(minutes=10), deadline=3600)
    delegation = world.short_delegation(60)
    approved = world.approve_under(delegation)
    assert approved["status"] == "running"
    reports = [world.runner.tick() for _ in range(3)]
    assert any(f"waiting:{world.campaign_id}:not_before" in r.actions for r in reports)


# ---- (2) (3) (4) the refusals ----------------------------------------------------------------


def test_a_start_after_the_delegation_expiry_is_escalated(tmp_path):
    # The approval (a day) would outlast the start; the delegation (5 minutes) would not.
    world = World(tmp_path, not_before=_now() + timedelta(minutes=30))
    submitted, reasons = world.attempt(world.short_delegation(5))
    assert reasons == [DELEGATION_REASON]
    world.assert_nothing_approved(submitted, [DELEGATION_REASON])


def test_a_start_after_the_approval_expiry_is_escalated(tmp_path):
    # The delegation (two days) outlasts the start; the approval (5 minutes) does not.
    world = World(tmp_path, not_before=_now() + timedelta(minutes=30), deadline=300)
    submitted, reasons = world.attempt(world.delegate())
    assert reasons == [APPROVAL_REASON]
    world.assert_nothing_approved(submitted, [APPROVAL_REASON])


def test_both_bounds_are_reported_together(tmp_path):
    world = World(tmp_path, not_before=_now() + timedelta(minutes=30), deadline=300)
    submitted, reasons = world.attempt(world.short_delegation(5))
    assert reasons == [APPROVAL_REASON, DELEGATION_REASON]
    world.assert_nothing_approved(submitted, [APPROVAL_REASON, DELEGATION_REASON])


def test_a_start_exactly_at_the_delegation_expiry_is_refused(tmp_path):
    expiry = _now() + timedelta(minutes=20)
    world = World(tmp_path, not_before=expiry)
    delegation = world.delegate(expires_at=_stamp(expiry))
    assert delegation["expires_at"] == _stamp(expiry)
    submitted, reasons = world.attempt(delegation)
    assert reasons == [DELEGATION_REASON]
    world.assert_nothing_approved(submitted, [DELEGATION_REASON])


def test_the_comparison_is_at_or_after_the_expiry():
    class Spec:
        not_before = "2030-01-02T03:04:05Z"

    edge = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert not_before_reaches(Spec, edge) is not None  # exactly equal
    assert not_before_reaches(Spec, edge + timedelta(seconds=1)) is None
    assert not_before_reaches(Spec, edge - timedelta(seconds=1)) is not None


def test_a_refusal_widens_nothing(tmp_path):
    world = World(tmp_path, not_before=_now() + timedelta(minutes=30))
    delegation = world.short_delegation(5)
    before = world.row("SELECT expires_at, revision, status FROM approval_delegations")
    world.attempt(delegation)
    after = world.row("SELECT expires_at, revision, status FROM approval_delegations")
    assert dict(before) == dict(after)


# ---- (5) no not_before: as before -------------------------------------------------------------


def test_a_spec_without_not_before_is_unaffected(tmp_path):
    world = World(tmp_path, not_before=None, deadline=300)
    approved = world.approve_under(world.short_delegation(5))
    assert approved["status"] == "running"
    assert world.row("SELECT 1 FROM delegated_decisions WHERE outcome='escalated'") is None


# ---- (6) the human path ------------------------------------------------------------------------


@pytest.fixture
def human_env(tmp_path):
    env = Env(tmp_path)
    worker.register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    env.trust()
    yield env
    worker.register_from_config(None)


def test_a_persons_approval_is_unchanged(human_env):
    env = human_env
    late = env.submit(spec=_v2(env, not_before="2999-01-01T00:00:00Z"))
    with pytest.raises(Conflict, match="not_before is not before this approval's expiry"):
        env.approve(late)
    soon = _stamp(_now() + timedelta(minutes=10))
    approved = env.approve(env.submit(spec=_v2(env, not_before=soon)))
    assert approved["status"] == "running"


# ---- (7) at the stop, an expired delegation still leaves the review with a person --------------


def test_an_expired_delegation_leaves_the_review_stop_in_place(tmp_path, monkeypatch):
    world = World(tmp_path, not_before=_now() + timedelta(minutes=3), deadline=3600)
    world.approve_under(world.short_delegation(5))
    # Past the delegation's expiry (5 minutes) by the time the deferred start reaches its stop.
    later = _stamp(_now() + timedelta(minutes=6))
    monkeypatch.setattr(campaign_preapproval, "utc_now", lambda: later)
    world.start()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.row("SELECT 1 FROM campaign_review_preapproval_uses") is None
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "unverified"
    assert world.declined() == ["delegation_expired"]
    # (The pending view derives its decline from the wall clock, which this test does not move, so
    # the recorded decline event above is what pins the visible reason here.)
