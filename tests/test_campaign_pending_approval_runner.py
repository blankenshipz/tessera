"""The pending-approval view over rows the real Salad runner wrote (G-11, ADR 0044).

The unit tests in test_campaign_pending_approval.py insert rows by hand; these drive the real
runner to ``awaiting_review`` so the listed ids are the decision requests ``_await_review`` created.
"""

from __future__ import annotations

from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control.campaign_models import normalize, parse_spec_json
from tessera_control.campaign_pending_approval import pending_human_approval
from tessera_control.campaign_runner import _Ctx
from tessera_control.interaction import InteractionStore
from tessera_control.store import canonical_digest


def _items(world: SaladWorld) -> list[dict]:
    with world.env.store.runner_connection() as connection:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        direct = pending_human_approval(connection, campaign)
    viewed = world.env.store.get_campaign(world.env.owner, world.campaign_id)
    assert viewed["pending_human_approval"] == direct  # the view and the function agree
    return direct


def _reviews(world: SaladWorld) -> list[dict]:
    return [i for i in _items(world) if i["kind"] == "salad_review"]


def _expected(world: SaladWorld, *reviews: str) -> list[dict]:
    row = world.salad()
    ids = {"plan": row["plan_decision_id"], "source_use": row["source_decision_id"]}
    return [
        {
            "kind": "salad_review",
            "stage_key": "g1",
            "attempt": 1,
            "review": review,
            "awaiting": f"a person's decision on the {review.replace('_', ' ')} review",
            "decision_request_id": ids[review],
        }
        for review in reviews
    ]


def test_real_review_requests_are_listed_and_clear_when_approved(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    assert _reviews(world) == _expected(world, "plan", "source_use")
    world.resolve_decisions()
    assert _reviews(world) == []


def test_real_review_requests_clear_when_rejected(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    assert _reviews(world) == _expected(world, "plan", "source_use")
    world.resolve_decisions("reject")
    assert _reviews(world) == []


def test_one_decided_review_clears_alone(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    plan_id = world.salad()["plan_decision_id"]
    world.env.execute("UPDATE decision_requests SET status='rejected' WHERE id=?", plan_id)
    assert _reviews(world) == _expected(world, "source_use")


def test_a_review_past_its_expiry_is_not_listed_and_the_real_sweep_expires_only_it(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    plan_id = world.salad()["plan_decision_id"]
    source_id = world.salad()["source_decision_id"]
    world.env.execute(
        "UPDATE decision_requests SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", plan_id
    )
    status = "SELECT status FROM decision_requests WHERE id=?"
    # Past expiry but nothing swept it yet: still 'pending', yet no longer listed.
    assert world.row(status, plan_id)["status"] == "pending"
    assert _reviews(world) == _expected(world, "source_use")

    project = world.row("SELECT project_id, org_id FROM campaigns WHERE id=?", world.campaign_id)
    code, body = InteractionStore(world.env.plain).expire_decisions(
        world.human, world.key("expire"), project["project_id"]
    )
    assert code == 200
    assert body == {"expired": [plan_id]}  # the future-expiry review is left pending
    assert world.row(status, plan_id)["status"] == "expired"
    assert world.row(status, source_id)["status"] == "pending"
    assert _reviews(world) == _expected(world, "source_use")  # _items also checks the campaign view


# -- review pre-approval (ADR 0044) -----------------------------------------------------


def _short(env):
    return [salad_stage(env, max_duration_seconds=600)]


class PreWorld(SaladWorld):
    def start_pre(self, mutate=None) -> None:
        submitted = self.env.submit(spec=self.spec)
        row = self.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0]
        campaign = dict(row)
        spec = normalize(parse_spec_json(campaign["spec_json"]))
        entry = {
            **self.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), "g1"),
            "max_attempt_cents": 50,
            "max_total_cents": 50,
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


def test_a_review_resolved_under_a_preapproval_is_not_listed(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre()
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")
    # The runner approved both requests itself (decision_class campaign_delegated). The view
    # does not consult the pre-approval: it shows nothing because each request's own status is
    # no longer 'pending'.
    salad = world.salad()
    for decision_id in (salad["plan_decision_id"], salad["source_decision_id"]):
        row = world.row("SELECT status FROM decision_requests WHERE id=?", decision_id)
        assert row["status"] == "approved"
    assert _reviews(world) == []


def test_a_declined_preapproval_leaves_only_that_review_listed(tmp_path):
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=True)
    world.start_pre(mutate=lambda e: e.update(plan_digest="1" * 64))
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert world.phase() == "awaiting_review"
    # The plan digest did not match so the plan stays with a person; source use was delegated.
    assert _reviews(world) == _expected(world, "plan")


def test_a_preapproval_the_profile_ignores_still_waits_for_a_person(tmp_path):
    # The operator profile narrows: preapprove_reviews false ignores the entry, so the human stop
    # stands and both requests stay pending and listed.
    world = PreWorld(tmp_path, stages=_short, preapprove_reviews=False)
    world.start_pre()
    world.tick_until(
        lambda: (
            world.row("SELECT source_id FROM campaign_salad_stages WHERE source_id IS NOT NULL")
            is not None
        )
    )
    world.assess_source()  # the source assessment is a person's too
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    assert _reviews(world) == _expected(world, "plan", "source_use")
