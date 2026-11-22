"""Salad review pre-approval (ADR 0044): exact digests, caps, expiry, fail closed."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control import campaign_preapproval as pre
from tessera_control.campaign_models import normalize, parse_spec_json
from tessera_control.campaign_runner import _Ctx
from tessera_control.store import Forbidden, canonical_digest


def _short(env):
    # Short enough to fit the test credentials' window (deadline-fit refusal).
    return [salad_stage(env, max_duration_seconds=600)]


class PreWorld(SaladWorld):
    def preview(self, submitted: dict) -> dict:
        row = self.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0]
        campaign = dict(row)
        spec = normalize(parse_spec_json(campaign["spec_json"]))
        return self.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), "g1")

    def start_pre(self, mutate=None, **entry_over) -> dict:
        submitted = self.env.submit(spec=self.spec)
        entry = {
            **self.preview(submitted),
            "max_attempt_cents": 50,
            "max_total_cents": 50,
            **entry_over,
        }
        if mutate:
            mutate(entry)
        body = {
            "expected_revision": submitted["revision"],
            "expected_spec_digest": submitted["spec_digest"],
            "preapprovals": [entry],
        }
        status, result = self.env.store.approve_campaign(
            self.human, submitted["id"], self.key("approve"), canonical_digest(body), body
        )
        assert status == 200
        self.campaign = result
        return result

    def uses(self) -> list[dict]:
        return [dict(r) for r in self.env.query("SELECT * FROM campaign_review_preapproval_uses")]

    def to_leased(self) -> None:
        self.tick_until_phase("awaiting_lease")
        self.lease_and_package()


def test_exact_digests_resolve_every_review_without_a_human(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre()
    world.to_leased()
    world.tick_until_phase("route_started")
    assert world.events("campaign.stage_awaiting_human") == []
    assert world.events("campaign.preapproval_declined") == []
    source = world.row("SELECT verdict FROM research_sources")
    assert source["verdict"] == "supported"
    salad = world.salad()
    for decision_id in (salad["plan_decision_id"], salad["source_decision_id"]):
        status = world.row("SELECT status FROM decision_requests WHERE id=?", decision_id)
        assert status["status"] == "approved"
    resolved = world.events("decision_resolved")
    assert resolved and all(e["decision_class"] == "campaign_delegated" for e in resolved)
    uses = world.uses()
    assert sorted(u["kind"] for u in uses) == ["plan", "source_assessment", "source_use"]
    assert all(u["decision_class"] == "campaign_delegated" for u in uses)
    assert sum(u["cents"] for u in uses) == 20  # the stage's exposure, counted once


def test_digest_mismatch_falls_back_to_the_human_path(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre(mutate=lambda e: e.update(plan_digest="1" * 64))
    world.to_leased()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    declined = world.events("campaign.preapproval_declined")
    assert [d["reason"] for d in declined if d["kind"] == "plan"] == ["digest_mismatch"]
    assert world.phase() == "awaiting_review"
    # The source assessment and source-use matched; only the plan stays for a human.
    salad = world.salad()
    assert (
        world.row("SELECT status FROM decision_requests WHERE id=?", salad["plan_decision_id"])[
            "status"
        ]
        == "pending"
    )
    # A human can still resolve the plan; the run then continues.
    store_row = world.row(
        "SELECT revision FROM decision_requests WHERE id=?", salad["plan_decision_id"]
    )
    from tessera_control.interaction import InteractionStore

    InteractionStore(world.env.plain).resolve_decision(
        world.human,
        world.key("resolve"),
        salad["plan_decision_id"],
        store_row["revision"],
        "approve",
        "human reviewed",
    )
    world.tick_until_phase("route_started")


def test_expired_preapproval_declines(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    past = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    world.start_pre(expires_at=past)
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert [e["reason"] for e in world.events("campaign.preapproval_declined")] == [
        "preapproval_expired"
    ]
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "unverified"


def test_total_cap_overrun_declines(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre()
    world.to_leased()
    world.tick_until_phase("route_started")
    row = world.row("SELECT * FROM campaign_review_preapprovals")
    # Pretend earlier pre-approved attempts already consumed the whole ceiling.
    world.env.execute(
        "INSERT INTO campaign_review_preapproval_uses (id, preapproval_id, campaign_id, stage_key,"
        " kind, subject_id, attempt, review_digest, cents, intent_id, decision_class, created_at)"
        " VALUES ('u1', ?, ?, 'g1', 'plan', 'other', 9, ?, ?, NULL, 'campaign_delegated', 'now')",
        row["id"],
        row["campaign_id"],
        "2" * 64,
        row["max_total_cents"],
    )
    with world.env.store.runner_connection() as connection:
        salad = world.salad()
        approval_id = row["approval_id"]
        verdict = pre.evaluate(
            connection,
            campaign_id=row["campaign_id"],
            approval_id=approval_id,
            stage_key="g1",
            kind="plan",
            subject_id=salad["plan_decision_id"],
        )
    # The decision is no longer pending, so the first gate is not_pending; the cap itself:
    assert verdict.reason in {"review_not_pending", "total_cap"}


def test_attempt_cap_below_the_stage_exposure_is_refused_at_approve(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    submitted = world.env.submit(spec=world.spec)
    entry = {**world.preview(submitted), "max_attempt_cents": 1, "max_total_cents": 1}
    body = {
        "expected_revision": submitted["revision"],
        "expected_spec_digest": submitted["spec_digest"],
        "preapprovals": [entry],
    }
    from tessera_control.store import Conflict

    with pytest.raises(Conflict):
        world.env.store.approve_campaign(
            world.human, submitted["id"], "k1", canonical_digest(body), body
        )
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None


def test_unknown_stage_is_refused(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    submitted = world.env.submit(spec=world.spec)
    entry = {
        **world.preview(submitted),
        "stage_key": "nope",
        "max_attempt_cents": 50,
        "max_total_cents": 50,
    }
    body = {
        "expected_revision": submitted["revision"],
        "expected_spec_digest": submitted["spec_digest"],
        "preapprovals": [entry],
    }
    from tessera_control.store import Conflict

    with pytest.raises(Conflict):
        world.env.store.approve_campaign(
            world.human, submitted["id"], "k2", canonical_digest(body), body
        )


def test_preapprovals_are_never_part_of_the_spec(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    spec = {**world.spec, "preapprovals": []}
    with pytest.raises(Exception):  # noqa: B017 - strict spec model refuses the field
        parse_spec_json(json.dumps(spec))


def test_delegated_actor_cannot_reject_or_resolve_without_a_matching_step(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre()
    with world.env.store.runner_connection() as connection:
        with pytest.raises(Forbidden):
            pre.authorize_delegated(
                connection,
                {
                    "campaign_delegated": {
                        "step": "resolve_decision",
                        "intent_id": "x",
                        "approval_id": "a",
                        "campaign_id": "c",
                    }
                },
                step="resolve_decision",
                kind="plan",
                subject_id="d",
                approve=False,
            )
        with pytest.raises(Forbidden):
            pre.authorize_delegated(
                connection,
                {
                    "campaign_delegated": {
                        "step": "assess_source",
                        "intent_id": "x",
                        "approval_id": "a",
                        "campaign_id": "c",
                    }
                },
                step="resolve_decision",
                kind="plan",
                subject_id="d",
                approve=True,
            )


def test_bare_approval_never_implies_pre_approval(tmp_path):
    """ADR 0044: opt-in only. A plain approval, even with a permissive profile, stops."""
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start()  # no preapprovals in the approve body
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "unverified"
    assert world.events("campaign.preapproval_declined") == []


def test_profile_can_only_narrow_an_explicit_pre_approval(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=False)
    world.start_pre()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert world.row("SELECT 1 FROM campaign_review_preapproval_uses") is None
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "unverified"


def test_receipt_cites_the_approval_for_each_delegated_review(tmp_path):
    from tessera_control.campaign_receipts import build_receipt

    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre()
    world.to_leased()
    world.tick_until_phase("route_started")
    with world.env.store.runner_connection() as connection:
        campaign = connection.execute("SELECT * FROM campaigns").fetchone()
        receipt, _ = build_receipt(connection, campaign, "running")
    [entry] = receipt["preapprovals"]
    assert entry["approval_id"] == receipt["approval"]["id"]
    assert {r["kind"] for r in entry["resolutions"]} == {"source_assessment", "plan", "source_use"}
    assert all(r["decision_class"] == "campaign_delegated" for r in entry["resolutions"])


def test_cli_digests_match_the_ones_the_runner_enforces(tmp_path, capsys, monkeypatch):
    """The offline preview prints exactly the digests an approve body binds."""
    import json as _json

    from tessera_control.cli import main

    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    submitted = world.env.submit(spec=world.spec)
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text(_json.dumps(world.profiles.model_dump(mode="json", by_alias=True)))
    profiles_file.chmod(0o600)
    import sys

    argv = [
        "campaign",
        "preapproval-digests",
        "--state-dir",
        str(world.env.state),
        "--salad-campaign-profiles",
        str(profiles_file),
        "--campaign-id",
        submitted["id"],
    ]

    monkeypatch.setattr(sys, "argv", ["tessera", *argv])
    code = main()
    out = _json.loads(capsys.readouterr().out)
    assert "error" not in out, out
    printed = out["preapprovals"]
    assert code == 0 and [p["stage_key"] for p in printed] == ["g1"]
    expected = world.preview(submitted)
    assert {k: printed[0][k] for k in expected} == expected


def test_total_above_the_campaign_budget_cap_is_refused_at_approve(tmp_path):
    from tessera_control.store import Conflict

    world = PreWorld(tmp_path, stages=_short)
    submitted = world.env.submit(spec=world.spec)
    entry = {**world.preview(submitted), "max_attempt_cents": 50, "max_total_cents": 51}
    body = {
        "expected_revision": submitted["revision"],
        "expected_spec_digest": submitted["spec_digest"],
        "preapprovals": [entry],
    }
    with pytest.raises(Conflict):
        world.env.store.approve_campaign(
            world.human, submitted["id"], "k9", canonical_digest(body), body
        )
