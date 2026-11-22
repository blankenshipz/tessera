# ruff: noqa: F811
"""Salad review pre-approval covers a placed Salad alternate (ADR 0066). Fakes only: the CPU
host's capacity, the Salad executor, guardian and human are test doubles; no Salad call."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import (  # noqa: F401
    HOLD,
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
)
from test_campaign_placement_salad import NEAR, _Capacity, _stage, near  # noqa: F401

from tessera_control import campaign_preapproval as pre
from tessera_control.campaign_models import normalize, parse_spec_json
from tessera_control.campaign_providers import unregister_provider
from tessera_control.campaign_runner import _Ctx
from tessera_control.store import Conflict, canonical_digest


class AltWorld(SaladWorld):
    def preview(self, submitted: dict, provider: str | None = "salad") -> dict:
        campaign = dict(self.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0])
        spec = normalize(parse_spec_json(campaign["spec_json"]))
        return self.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), "g1", provider)

    def approve(self, mutate=None, **over) -> dict:
        submitted = self.env.submit(spec=self.spec)
        entry = {**self.preview(submitted), "max_attempt_cents": 50, "max_total_cents": 50, **over}
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


def _world(tmp_path, near, slots):
    world = AltWorld(tmp_path, preapprove_reviews=True)
    near(world, slots)
    world.spec = salad_spec_v2(world.env, [_stage(world.env)])
    return world


def _to_route_started(world: AltWorld) -> None:
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")


def test_a_busy_cpu_host_places_the_stage_on_salad_and_the_preapproval_covers_it(tmp_path, near):
    world = _world(tmp_path, near, slots=0)
    world.approve()
    _to_route_started(world)
    (placed,) = world.events("campaign.stage_placed")
    assert placed["provider_id"] == "salad"
    # No person approved anything: the owner's digest-bound pre-approval resolved every review.
    assert world.events("campaign.stage_awaiting_human") == []
    assert world.events("campaign.preapproval_declined") == []
    uses = world.uses()
    assert sorted(u["kind"] for u in uses) == ["plan", "source_assessment", "source_use"]
    assert all(u["decision_class"] == "campaign_delegated" for u in uses)
    assert sum(u["cents"] for u in uses) == HOLD  # the Salad candidate's hold, once
    assert (
        world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)["held_cents"]
        <= 50
    )
    row = world.row("SELECT * FROM campaign_review_preapprovals")
    assert row["max_total_cents"] <= 50  # never beyond the approved campaign cap


def test_an_idle_cpu_host_never_touches_the_preapproval(tmp_path, near):
    world = _world(tmp_path, near, slots=1)
    world.approve()
    world.tick_until(lambda: world.attempt() is not None)
    world.tick(3)
    assert world.attempt()["provider_id"] == NEAR
    assert world.uses() == [] and world.row("SELECT 1 FROM campaign_salad_stages") is None


def test_the_preapproval_binds_the_digests_of_the_alternate(tmp_path, near):
    world = _world(tmp_path, near, slots=0)

    def wrong(entry):
        entry["plan_digest"] = "0" * 64

    world.approve(mutate=wrong)
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick(8)
    assert [e["reason"] for e in world.events("campaign.preapproval_declined")] == [
        pre.DIGEST_MISMATCH
    ]
    assert world.uses() and all(u["kind"] != "plan" for u in world.uses())  # the human path


def test_approval_refuses_what_the_alternate_cannot_cover(tmp_path, near):
    world = _world(tmp_path, near, slots=0)
    submitted = world.env.submit(spec=world.spec)
    base = {**world.preview(submitted), "max_attempt_cents": 50, "max_total_cents": 50}

    def refused(match: str, **over) -> None:
        entry = {**base, **over}
        body = {
            "expected_revision": submitted["revision"],
            "expected_spec_digest": submitted["spec_digest"],
            "preapprovals": [entry],
        }
        with pytest.raises(Conflict, match=match):
            world.env.store.approve_campaign(
                world.human, submitted["id"], world.key("a"), canonical_digest(body), body
            )

    refused("not a candidate", provider="local-docker")  # not named by the stage
    refused("Salad stages only", provider=NEAR)  # a candidate, but not Salad
    refused("below the stage's max_exposure_cents", max_attempt_cents=HOLD - 1)
    refused("exceeds the campaign's approved budget cap", max_total_cents=51)
    assert not world.env.query("SELECT 1 FROM campaign_review_preapprovals")


def test_the_preview_lists_each_salad_candidate_with_its_provider(tmp_path, near):
    from tessera_control.campaign_preapproval import preview_for_campaign

    world = _world(tmp_path, near, slots=0)
    submitted = world.env.submit(spec=world.spec)
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps(world.profiles.model_dump(mode="json", by_alias=True)))
    profiles.chmod(0o600)
    found = preview_for_campaign(world.env.state, profiles, submitted["id"])
    assert [(e["stage_key"], e["provider"]) for e in found] == [("g1", "salad")]
    assert found[0]["max_attempt_cents"] == HOLD
    assert found[0]["plan_digest"] == world.preview(submitted)["plan_digest"]


def _pending_plan(world: AltWorld) -> dict:
    """Run the Salad attempt to its reviews, then put the plan review back to pending so the
    resolution check can be read directly (the runner has already resolved it)."""
    _to_route_started(world)
    salad = world.salad()
    world.env.execute(
        "UPDATE decision_requests SET status='pending' WHERE id=?", salad["plan_decision_id"]
    )
    return salad


def _verdict(world: AltWorld, subject: str) -> pre.Verdict:
    row = world.row("SELECT * FROM campaign_review_preapprovals")
    with world.runner.store.runner_connection() as connection:
        return pre.evaluate(
            connection,
            campaign_id=world.campaign_id,
            approval_id=row["approval_id"],
            stage_key="g1",
            kind=pre.KIND_PLAN,
            subject_id=subject,
        )


def test_an_alternate_unregistered_after_approval_is_declined_at_resolution(tmp_path, near):
    world = _world(tmp_path, near, slots=0)
    world.approve()
    salad = _pending_plan(world)
    assert _verdict(world, salad["plan_decision_id"]).reason is None  # covered while registered
    unregister_provider("salad")
    assert _verdict(world, salad["plan_decision_id"]).reason == pre.ATTEMPT_CAP  # no cents, no pass


def test_a_different_candidate_than_the_one_named_is_declined(tmp_path, near):
    world = _world(tmp_path, near, slots=0)
    world.approve()
    salad = _pending_plan(world)
    assert _verdict(world, salad["plan_decision_id"]).reason is None
    # The attempt now sits on another candidate than the entry names: it never covers it.
    world.env.execute("UPDATE campaign_stage_attempts SET provider_id=?", NEAR)
    assert _verdict(world, salad["plan_decision_id"]).reason == pre.WRONG_PROVIDER


def test_the_covered_provider_is_recorded_on_the_approve_event(tmp_path, near):
    world = _world(tmp_path, near, slots=0)
    world.approve()
    (approved,) = world.events("campaign.approved")
    (entry,) = approved["preapprovals"]
    assert entry["provider_id"] == "salad"
    row = world.row("SELECT * FROM campaign_review_preapprovals")
    with world.runner.store.runner_connection() as connection:
        assert pre.covered_provider(connection, dict(row), NEAR) == "salad"
        # An entry with no recorded provider (approved before alternates) covers the own one.
        assert (
            pre.covered_provider(connection, {**dict(row), "id": "preapproval_none"}, NEAR) == NEAR
        )


def test_the_stores_not_registered_branch_refuses_an_unregistered_alternate(tmp_path, near):
    """Reached directly: a full approve re-validates the spec (and fails earlier), but the
    entry check must hold on its own."""
    from tessera_control.campaign_models import StagePreapproval
    from tessera_control.campaign_store import CampaignStore

    world = _world(tmp_path, near, slots=0)
    submitted = world.env.submit(spec=world.spec)
    campaign = dict(world.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0])
    spec = parse_spec_json(campaign["spec_json"])
    entry = StagePreapproval(
        **{**world.preview(submitted), "max_attempt_cents": 50, "max_total_cents": 50}
    )
    approval = {
        "id": "a",
        "expires_at": "2099-01-01T00:00:00Z",
        "approved_at": "2026-01-01T00:00:00Z",
    }
    unregister_provider("salad")
    with world.runner.store.runner_connection() as connection:
        with pytest.raises(Conflict, match="^Pre-approval provider is not registered"):
            CampaignStore._insert_preapprovals(connection, campaign, spec, approval, [entry], True)


def test_the_stages_own_provider_gets_the_same_registration_and_route_check(tmp_path, near):
    world = _world(tmp_path, near, slots=0)
    submitted = world.env.submit(spec=world.spec)
    campaign = dict(world.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0])
    with world.runner.store.runner_connection() as connection:
        # The own provider (a CPU worker host here) is not a Salad route: no cents.
        assert pre._stage_exposure(connection, campaign, "g1", NEAR) is None
        assert pre._stage_exposure(connection, campaign, "g1", None) is None
        assert pre._stage_exposure(connection, campaign, "g1", "salad") == HOLD
