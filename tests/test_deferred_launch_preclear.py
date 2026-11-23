"""A deferred Salad campaign approved under an owner-signed delegation with a review rule clears
its per-run review stops at start without a person (ADR 0150), and fails closed otherwise.

Synthetic fixtures only: the real stores and runner, a test-driven executor; no provider call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import record_snapshot_private
from salad_campaign_fixture import (  # noqa: F401
    HOLD,
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
    salad_stage_v2,
)

from tessera_control import campaign_preapproval
from tessera_control.approval_delegations import ApprovalDelegationCommands
from tessera_control.campaign_models import normalize, parse_spec_json
from tessera_control.campaign_pending_approval import pending_human_approval
from tessera_control.delegated_decisions import DelegatedDecisionStore, DelegationRefused
from tessera_control.delegation_rules import evaluate_review, terms_digest
from tessera_control.delegation_terms import DelegationTerms
from tessera_control.store import _connect, canonical_digest, utc_now

RULE = {
    "kinds": ["source_assessment", "plan", "source_use"],
    "max_attempt_cents": 40,
    "max_duration_seconds": 600,
    "provider_classes": ["salad_gpu"],
    "source_use_intents": ["gpu_stage_run"],
}


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _terms(review_rule=RULE, **over) -> dict:
    body = {
        "budget_ceiling_cents": 100,
        "per_run_cap_cents": 50,
        "allowed_providers": ["salad"],
        **({"review_rule": review_rule} if review_rule is not None else {}),
    }
    body.update(over)
    return body


class Deferred(SaladWorld):
    """A v2 Salad campaign held until ``not_before``; the runner's clock is the test's."""

    def __init__(self, tmp_path, *, deferred: bool = True, **stage_over) -> None:
        super().__init__(tmp_path, preapprove_reviews=True)
        self.start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
        stage = salad_stage_v2(self.env, "g1", **{"max_duration_seconds": 600, **stage_over})
        over = {"not_before": _stamp(self.start_at)} if deferred else {}
        self.spec = salad_spec_v2(self.env, [stage], **over)
        self.clock = [datetime.now(UTC)]
        self.runner.now = lambda: self.clock[0]
        self.delegations = ApprovalDelegationCommands(self.env.state)
        self.decider = DelegatedDecisionStore(self.env.state)

    def delegate(self, *, expires_at: str | None = None, **terms_over) -> dict:
        body = {
            "terms": _terms(**terms_over),
            "expires_at": expires_at
            or (datetime.now(UTC) + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        }
        status, result = self.delegations.create_approval_delegation(
            self.human, self.env.project["id"], self.key("deleg"), canonical_digest(body), body
        )
        assert status == 201
        return result

    def submit(self) -> dict:
        return self.env.submit(spec=self.spec)

    def approve_under(self, delegation: dict, submitted: dict | None = None) -> dict:
        submitted = submitted or self.submit()
        body = {
            "delegation_id": delegation["id"],
            "expected_revision": submitted["revision"],
            "expected_spec_digest": submitted["spec_digest"],
        }
        status, result = self.decider.delegated_approve(
            self.human, submitted["id"], self.key("dapprove"), canonical_digest(body), body
        )
        assert status == 200
        self.campaign = result
        return result

    def refused(self, delegation: dict, after_submit=None) -> list[str]:
        submitted = self.submit()
        if after_submit is not None:
            after_submit()
        body = {
            "delegation_id": delegation["id"],
            "expected_revision": submitted["revision"],
            "expected_spec_digest": submitted["spec_digest"],
        }
        with pytest.raises(DelegationRefused) as caught:
            self.decider.delegated_approve(
                self.human, submitted["id"], self.key("dapprove"), canonical_digest(body), body
            )
        status = self.row("SELECT status FROM campaigns WHERE id=?", submitted["id"])["status"]
        assert status == "submitted"  # nothing was approved
        return caught.value.reasons

    def start(self) -> None:
        """Past ``not_before``."""
        self.clock[0] = self.start_at + timedelta(minutes=1)

    def pending(self) -> list[dict]:
        with _connect(self.env.state / "tessera.sqlite3") as connection:
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (self.campaign_id,)
            ).fetchone()
            return pending_human_approval(connection, campaign)

    def declined(self) -> list[str]:
        return [e["reason"] for e in self.events("campaign.preapproval_declined")]


@pytest.fixture
def world(tmp_path):
    return Deferred(tmp_path)


# ---- the covered case ------------------------------------------------------------------------


def test_covered_deferred_campaign_clears_every_review_at_start_without_a_person(world):
    delegation = world.delegate()
    world.approve_under(delegation)
    # Held until not_before: nothing starts and nothing is pre-approved yet.
    reports = [world.runner.tick() for _ in range(3)]
    assert any(f"waiting:{world.campaign_id}:not_before" in r.actions for r in reports)
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.salad() is None
    world.start()
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")
    assert world.events("campaign.stage_awaiting_human") == []
    assert world.declined() == []
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "supported"
    salad = world.salad()
    for decision_id in (salad["plan_decision_id"], salad["source_decision_id"]):
        assert world.row("SELECT status FROM decision_requests WHERE id=?", decision_id)[
            "status"
        ] == ("approved")
    assert world.pending() == []
    uses = [dict(r) for r in world.env.query("SELECT * FROM campaign_review_preapproval_uses")]
    assert sorted(u["kind"] for u in uses) == ["plan", "source_assessment", "source_use"]
    # Never widened: the attempt cap is the stage's own hold, not the rule's 40 or the
    # delegation's 50; the total is the smaller of the campaign cap and the per-run cap.
    pre = world.row("SELECT * FROM campaign_review_preapprovals")
    assert (pre["max_attempt_cents"], pre["max_total_cents"]) == (HOLD, 50)
    assert pre["expires_at"] <= world.row("SELECT expires_at FROM campaign_approvals")["expires_at"]
    (event,) = world.events("campaign.review_preapproval_delegated")
    assert event["delegation_id"] == delegation["id"]
    assert event["terms_digest"] == delegation["terms_digest"]
    assert event["kinds"] == ["source_assessment", "plan", "source_use"]


def test_receipt_names_the_delegation_not_the_person(world):
    from tessera_control.campaign_receipts import build_receipt

    delegation = world.delegate()
    world.approve_under(delegation)
    world.start()
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")
    with world.env.store.runner_connection() as connection:
        campaign = connection.execute("SELECT * FROM campaigns").fetchone()
        receipt, _ = build_receipt(connection, campaign, "running")
    (entry,) = receipt["preapprovals"]
    assert entry["delegation"] == {
        "id": delegation["id"],
        "terms_digest": delegation["terms_digest"],
    }
    assert world.human["id"] not in json.dumps(entry)
    assert {r["kind"] for r in entry["resolutions"]} == {"source_assessment", "plan", "source_use"}
    assert receipt["delegation"]["delegations"] == [delegation["id"]]


# ---- the delegated approval: out of scope is refused, never widened ---------------------------


@pytest.mark.parametrize(
    ("stage_over", "rule_over", "reason"),
    [
        ({}, {"max_attempt_cents": HOLD - 1}, "review_cap_exceeded"),
        ({"max_duration_seconds": 900}, {}, "review_time_limit_exceeded"),
        ({}, {"provider_classes": ["salad_cpu"]}, "provider_class_not_allowed"),
        ({}, {"source_use_intents": ["another_outcome"]}, "source_use_intent_not_covered"),
    ],
)
def test_each_out_of_scope_case_is_refused_with_its_reason(tmp_path, stage_over, rule_over, reason):
    world = Deferred(tmp_path, **stage_over)
    delegation = world.delegate(review_rule={**RULE, **rule_over})
    assert world.refused(delegation) == [reason]
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    escalated = world.env.query("SELECT reason FROM delegated_decisions WHERE outcome='escalated'")
    assert [r["reason"] for r in escalated] == [reason]


def test_a_rule_cap_above_the_delegation_cap_cannot_be_signed():
    with pytest.raises(ValueError, match="review_rule.max_attempt_cents"):
        DelegationTerms.model_validate(_terms(review_rule={**RULE, "max_attempt_cents": 51}))


def test_without_a_review_rule_a_salad_spec_still_needs_a_person(world):
    delegation = world.delegate(review_rule=None)
    assert world.refused(delegation) == ["spec_needs_human"]


def test_a_delegation_without_a_review_rule_keeps_its_digest():
    plain = _terms(review_rule=None)
    terms = DelegationTerms.model_validate(plain)
    assert "review_rule" not in terms.model_dump(mode="json")
    # The canonical form is exactly the pre-ADR 0150 field set, so the digest is unchanged.
    before = {
        "version": 1,
        "program_id": None,
        "max_approvals": None,
        "gate_rules": [],
        **plain,
    }
    assert terms_digest(terms) == canonical_digest(before)
    with_rule = DelegationTerms.model_validate(_terms())
    assert terms_digest(with_rule) != terms_digest(terms)


def test_restricted_data_routed_to_salad_still_refuses(world):
    """ADR 0040: a review rule never admits data the provider trust setting does not."""
    delegation = world.delegate()
    reasons = world.refused(
        delegation,
        after_submit=lambda: record_snapshot_private(world.env.state, world.env.snapshot_id),
    )
    assert reasons == ["spec_references_invalid"]
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None


# ---- at the stop: fail closed, a visible reason -------------------------------------------------


def _to_review_stop(world: Deferred) -> None:
    world.start()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])


def test_a_kind_the_rule_does_not_cover_stays_with_a_person(world):
    delegation = world.delegate(review_rule={**RULE, "kinds": ["source_assessment", "source_use"]})
    world.approve_under(delegation)
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
    assert status == {"plan_decision_id": "pending", "source_decision_id": "approved"}
    pre = world.row("SELECT * FROM campaign_review_preapprovals")
    assert pre["plan_digest"] == campaign_preapproval.UNCOVERED_DIGEST
    (item,) = world.pending()
    assert item["review"] == "plan"
    assert item["delegation_declined"] == ["review_kind_not_covered"]
    # A person can still decide it, and the run continues.
    from tessera_control.interaction import InteractionStore

    plan = world.row("SELECT revision FROM decision_requests WHERE id=?", salad["plan_decision_id"])
    InteractionStore(world.env.plain).resolve_decision(
        world.human,
        world.key("resolve"),
        salad["plan_decision_id"],
        plan["revision"],
        "approve",
        "Owner review of the bounded stage run",
    )
    world.tick_until_phase("route_started")


def test_revoked_before_start_stays_stopped(world):
    delegation = world.delegate()
    world.approve_under(delegation)
    world.tick()
    body = {"expected_revision": delegation["revision"]}
    status, _ = world.delegations.revoke_approval_delegation(
        world.human, delegation["id"], world.key("revoke"), canonical_digest(body), body
    )
    assert status == 200
    _to_review_stop(world)
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "unverified"
    assert world.declined() == ["delegation_revoked"]


def test_expired_at_start_stays_stopped(world, monkeypatch):
    # It must outlive ``not_before`` to approve at all (a delegated approval refuses a start the
    # delegation could not reach); it lapses 30 seconds after the deferred start.
    delegation = world.delegate(expires_at=_stamp(world.start_at + timedelta(seconds=30)))
    world.approve_under(delegation)
    later = _stamp(world.start_at + timedelta(minutes=1))
    monkeypatch.setattr(campaign_preapproval, "utc_now", lambda: later)
    _to_review_stop(world)
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.declined() == ["delegation_expired"]


def test_signer_no_longer_live_stays_stopped(world):
    delegation = world.delegate()
    world.approve_under(delegation)
    world.env.execute(
        "UPDATE credentials SET revoked_at=? WHERE id=?", utc_now(), world.human["credential_id"]
    )
    world.start()
    world.tick(3)
    # The runner acts only while the signer is live: the campaign pauses before any review.
    assert world.status() == "paused_delegation_lapsed"
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.row("SELECT 1 FROM campaign_review_preapproval_uses") is None


def test_a_pre_approval_already_recorded_is_rechecked_at_every_use(world):
    """Revoked after the stage's pre-approval was recorded: the next review is declined."""
    delegation = world.delegate()
    world.approve_under(delegation)
    world.start()
    world.tick_until_phase("awaiting_lease")
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is not None
    body = {"expected_revision": delegation["revision"]}
    world.delegations.revoke_approval_delegation(
        world.human, delegation["id"], world.key("revoke"), canonical_digest(body), body
    )
    world.lease_and_package()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert set(world.declined()) == {"delegation_revoked"}
    assert {i["delegation_declined"][0] for i in world.pending()} == {"delegation_revoked"}
    kinds = {
        r["kind"] for r in world.env.query("SELECT kind FROM campaign_review_preapproval_uses")
    }
    assert kinds == {"source_assessment"}


# ---- the at-stop rule, directly --------------------------------------------------------------


def _review(world: Deferred, delegation: dict, kind: str = "plan", now=None):
    with _connect(world.env.state / "tessera.sqlite3") as connection:
        row = connection.execute(
            "SELECT * FROM approval_delegations WHERE id=?", (delegation["id"],)
        ).fetchone()
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        stage = normalize(parse_spec_json(campaign["spec_json"])).stage("g1")
        return evaluate_review(
            connection, row, campaign, stage, kind, now=now or datetime.now(UTC)
        ).reasons


def test_the_stop_rule_rechecks_every_bound(world):
    delegation = world.delegate()
    world.approve_under(delegation)
    assert _review(world, delegation) == ()
    assert _review(world, delegation, now=datetime.now(UTC) + timedelta(days=3)) == (
        "delegation_expired",
    )
    narrow = world.delegate(review_rule={**RULE, "kinds": ["plan"]})
    assert _review(world, narrow, kind="source_use") == ("review_kind_not_covered",)
    small = world.delegate(review_rule={**RULE, "max_attempt_cents": HOLD - 1})
    assert _review(world, small) == ("review_cap_exceeded",)
    cpu = world.delegate(review_rule={**RULE, "provider_classes": ["salad_cpu"]})
    assert _review(world, cpu) == ("provider_class_not_allowed",)
    short = world.delegate(review_rule={**RULE, "max_duration_seconds": 599})
    assert _review(world, short) == ("review_time_limit_exceeded",)
    other = world.delegate(review_rule={**RULE, "source_use_intents": ["another_outcome"]})
    assert _review(world, other) == ("source_use_intent_not_covered",)
    plain = world.delegate(review_rule=None)
    assert _review(world, plain) == ("no_review_rule",)
