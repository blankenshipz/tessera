"""A person approving a campaign directly can bind one of their own owner-signed delegations
(ADR 0150 amendment) so the per-run Salad reviews are resolved by the platform under it, and
every refusal is fail closed.

Synthetic fixtures only: the real stores and runner, a test-driven executor; no provider call.
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import HOLD, salad_provider_reset  # noqa: F401
from test_deferred_launch_preclear import RULE, Deferred

from tessera_control.campaign_models import ApproveCampaign, approve_payload
from tessera_control.campaign_receipts import build_receipt
from tessera_control.store import Conflict, NotFound, canonical_digest, utc_now


class Direct(Deferred):
    """The same deferred Salad campaign, approved directly by a person."""

    def approve_direct(self, submitted: dict | None = None, **extra) -> dict:
        submitted = submitted or self.submit()
        status, result = self.try_approve(submitted, **extra)
        assert status == 200
        self.campaign = result
        return result

    def try_approve(self, submitted: dict, actor: dict | None = None, **extra):
        body = {
            "expected_revision": submitted["revision"],
            "expected_spec_digest": submitted["spec_digest"],
            **extra,
        }
        return self.env.store.approve_campaign(
            actor or self.human,
            submitted["id"],
            self.key("approve"),
            canonical_digest(body),
            body,
        )

    def revoke(self, delegation: dict) -> None:
        body = {"expected_revision": delegation["revision"]}
        status, _ = self.delegations.revoke_approval_delegation(
            self.human, delegation["id"], self.key("revoke"), canonical_digest(body), body
        )
        assert status == 200

    def to_review_stop(self) -> None:
        self.start()
        self.tick_until_phase("awaiting_lease")
        self.lease_and_package()
        self.tick_until_phase("route_started")

    def counts(self, campaign_id: str) -> tuple[int, int, int]:
        return (
            len(
                self.env.query("SELECT 1 FROM campaign_approvals WHERE campaign_id=?", campaign_id)
            ),
            len(self.events("campaign.review_delegation_bound")),
            len(self.events("campaign.approved")),
        )


@pytest.fixture
def world(tmp_path):
    return Direct(tmp_path)


def _refused(world: Direct, reason: str, exc=Conflict, **extra) -> None:
    submitted = world.submit()
    before = world.counts(submitted["id"])
    with pytest.raises(exc) as caught:
        world.try_approve(submitted, **extra)
    if exc is Conflict:
        assert str(caught.value).startswith("review_delegation_refused:")
        assert reason in str(caught.value)
    assert world.counts(submitted["id"]) == before == (0, 0, 0)
    assert world.row("SELECT status FROM campaigns WHERE id=?", submitted["id"])["status"] == (
        "submitted"
    )


# ---- the covered case ---------------------------------------------------------------------


def test_direct_approval_with_own_delegation_resolves_reviews_without_a_person(world):
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    (bound,) = world.events("campaign.review_delegation_bound")
    assert bound["delegation_id"] == delegation["id"]
    assert bound["terms_digest"] == delegation["terms_digest"]
    assert bound["campaign_id"] == world.campaign_id
    assert bound["approval_id"] == world.row("SELECT id FROM campaign_approvals")["id"]
    assert sorted(bound["kinds"]) == sorted(RULE["kinds"])
    (approved,) = world.events("campaign.approved")
    assert approved["review_delegation_id"] == delegation["id"]
    assert world.row("SELECT delegation_id FROM campaign_approvals")["delegation_id"] is None

    world.to_review_stop()
    assert world.events("campaign.stage_awaiting_human") == []
    assert world.declined() == []
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "supported"
    salad = world.salad()
    for column in ("plan_decision_id", "source_decision_id"):
        row = world.row("SELECT status FROM decision_requests WHERE id=?", salad[column])
        assert row["status"] == "approved"
    assert world.pending() == []
    uses = [dict(r) for r in world.env.query("SELECT * FROM campaign_review_preapproval_uses")]
    assert sorted(u["kind"] for u in uses) == ["plan", "source_assessment", "source_use"]
    assert {u["decision_class"] for u in uses} == {"campaign_delegated"}
    (event,) = world.events("campaign.review_preapproval_delegated")
    assert event["delegation_id"] == delegation["id"]
    assert event["terms_digest"] == delegation["terms_digest"]

    with world.env.store.runner_connection() as connection:
        campaign = connection.execute("SELECT * FROM campaigns").fetchone()
        receipt, _ = build_receipt(connection, campaign, "running")
    assert receipt["approval"]["review_delegation_id"] == delegation["id"]
    (entry,) = receipt["preapprovals"]
    assert entry["delegation"] == {
        "id": delegation["id"],
        "terms_digest": delegation["terms_digest"],
    }
    assert {r["kind"] for r in entry["resolutions"]} == set(RULE["kinds"])


# ---- refused at approve: nothing is written ------------------------------------------------


def test_another_humans_delegation_is_refused(world):
    other = world.env.human("second-owner")
    body = {
        "terms": {
            "budget_ceiling_cents": 100,
            "per_run_cap_cents": 50,
            "allowed_providers": ["salad"],
            "review_rule": RULE,
        },
        "expires_at": "2099-01-01T00:00:00Z",
    }
    from datetime import UTC, datetime, timedelta

    body["expires_at"] = (datetime.now(UTC) + timedelta(days=2)).isoformat().replace("+00:00", "Z")
    status, theirs = world.delegations.create_approval_delegation(
        other, world.env.project["id"], world.key("deleg"), canonical_digest(body), body
    )
    assert status == 201
    _refused(world, "not_the_delegating_owner", review_delegation_id=theirs["id"])


def test_revoked_delegation_is_refused(world):
    delegation = world.delegate()
    world.revoke(delegation)
    _refused(world, "delegation_revoked", review_delegation_id=delegation["id"])


def test_expired_delegation_is_refused(world, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from tessera_control import campaign_store

    delegation = world.delegate(
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    later = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    submitted = world.submit()
    monkeypatch.setattr(campaign_store, "utc_now", lambda: later)
    with pytest.raises(Conflict, match="review_delegation_refused:.*delegation_expired"):
        world.try_approve(submitted, review_delegation_id=delegation["id"])
    assert world.counts(submitted["id"]) == (0, 0, 0)


def test_delegation_without_a_review_rule_is_refused(world):
    delegation = world.delegate(review_rule=None)
    _refused(world, "no_review_rule", review_delegation_id=delegation["id"])


def test_another_projects_delegation_is_not_found(world):
    from datetime import UTC, datetime, timedelta

    world.env.execute(
        "INSERT INTO projects (id, org_id, name, created_at) VALUES (?, ?, 'elsewhere', ?)",
        "project_elsewhere",
        world.env.project["org_id"],
        utc_now(),
    )
    body = {
        "terms": {
            "budget_ceiling_cents": 100,
            "per_run_cap_cents": 50,
            "allowed_providers": ["salad"],
            "review_rule": RULE,
        },
        "expires_at": (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    status, elsewhere = world.delegations.create_approval_delegation(
        world.human, "project_elsewhere", world.key("deleg"), canonical_digest(body), body
    )
    assert status == 201
    _refused(world, "", exc=NotFound, review_delegation_id=elsewhere["id"])


def test_combined_with_explicit_preapprovals_is_refused(world):
    delegation = world.delegate()
    _refused(
        world,
        "preapprovals",
        review_delegation_id=delegation["id"],
        preapprovals=[
            {
                "stage_key": "g1",
                "source_assessment_digest": "a" * 64,
                "plan_digest": "b" * 64,
                "source_use_digest": "c" * 64,
                "max_attempt_cents": HOLD,
                "max_total_cents": HOLD,
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ],
    )


def test_cleanup_reapproval_cannot_bind_one(world):
    delegation = world.delegate()
    submitted = world.submit()
    world.env.execute("UPDATE campaigns SET status='cancel_requested' WHERE id=?", submitted["id"])
    before = world.counts(submitted["id"])
    with pytest.raises(Conflict, match="review_delegation_refused"):
        world.try_approve(submitted, review_delegation_id=delegation["id"])
    assert world.counts(submitted["id"]) == before


# ---- revoked after approval ----------------------------------------------------------------


def test_revoked_after_approval_leaves_the_reviews_with_a_person(world):
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    world.tick()
    world.revoke(delegation)
    world.start()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.row("SELECT 1 FROM campaign_review_preapproval_uses") is None
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "unverified"
    assert world.events("campaign.review_preapproval_delegated") == []
    assert "delegation_revoked" in world.declined()
    # A person assesses the source; the stage's own reviews then wait, with the reason shown.
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until(lambda: world.phase() == "awaiting_review" and bool(world.pending()))
    items = world.pending()
    assert {i["review"] for i in items} == {"plan", "source_use"}
    assert all(i["delegation_declined"] == ["delegation_revoked"] for i in items)
    assert world.row("SELECT 1 FROM campaign_review_preapproval_uses") is None


# ---- the direct path is unchanged ----------------------------------------------------------


def test_direct_approval_without_a_delegation_still_stops_for_a_person(world):
    world.approve_direct()
    assert world.events("campaign.review_delegation_bound") == []
    assert "review_delegation_id" not in world.events("campaign.approved")[0]
    world.start()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert world.row("SELECT 1 FROM campaign_review_preapproval_uses") is None
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "unverified"
    with world.env.store.runner_connection() as connection:
        campaign = connection.execute("SELECT * FROM campaigns").fetchone()
        receipt, _ = build_receipt(connection, campaign, "running")
    assert "review_delegation_id" not in receipt["approval"]


def test_approve_payload_digest_omits_the_field_when_absent():
    base = {"expected_revision": 1, "expected_spec_digest": "a" * 64}
    assert approve_payload(ApproveCampaign.model_validate(base)) == base
    named = approve_payload(
        ApproveCampaign.model_validate({**base, "review_delegation_id": "deleg_x"})
    )
    assert named == {**base, "review_delegation_id": "deleg_x"}
    assert canonical_digest(approve_payload(ApproveCampaign.model_validate(base))) == (
        canonical_digest(base)
    )


# ---- a kind the rule does not cover ---------------------------------------------------------


def test_a_kind_the_rule_does_not_cover_stays_with_a_person(world):
    delegation = world.delegate(review_rule={**RULE, "kinds": ["source_assessment"]})
    world.approve_direct(review_delegation_id=delegation["id"])
    world.start()
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until(lambda: world.phase() == "awaiting_review" and bool(world.pending()))
    world.tick(2)
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "supported"
    salad = world.salad()
    status = {
        column: world.row("SELECT status FROM decision_requests WHERE id=?", salad[column])[
            "status"
        ]
        for column in ("plan_decision_id", "source_decision_id")
    }
    assert status == {"plan_decision_id": "pending", "source_decision_id": "pending"}
    kinds = {
        r["kind"] for r in world.env.query("SELECT kind FROM campaign_review_preapproval_uses")
    }
    assert kinds == {"source_assessment"}
    assert world.pending()
