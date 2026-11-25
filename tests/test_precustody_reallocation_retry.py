"""A reallocation before any custody work is retried on a FRESH attempt (ADR 0179).

Fakes only. The classification is proven where it is made (the live check's receipt, the Salad
provider's observation); the bounded retry, the per-attempt holds and the budget cap are proven
through the real campaign runner and its local-provider harness, with the runner's attempt
classification standing in for a reallocated Salad attempt."""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, dump, stage_spec
from test_salad_live_check import LocalSession, drive

from tessera_control.campaign_infra_retry import REALLOCATED_PRECUSTODY, reallocation_attempts
from tessera_control.campaign_models import FAILURE_CLASSES, RetryPolicy
from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.campaign_runner import CampaignRunner

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
OK = {"data.json": json.dumps({"score": 3})}
KILLED = 137


def test_the_class_is_a_known_failure_class():
    assert REALLOCATED_PRECUSTODY in FAILURE_CLASSES


# -- classification -------------------------------------------------------------------------


class Reallocated(LocalSession):
    """A run whose guardian stopped on an allocation change, with the provider's stop confirmed
    but custody unsealed."""

    def __init__(self, custody_work_begun):
        super().__init__()
        self.custody = custody_work_begun
        self.proof.update(
            stop_confirmed=False,
            provider_stop_confirmed=True,
            stop_cause="allocation_changed",
            custody_work_begun=custody_work_begun,
        )


@pytest.mark.parametrize(
    ("custody", "reason"),
    [
        (False, REALLOCATED_PRECUSTODY),  # nothing produced or received: retryable
        (True, "provider_stopped_custody_unsealed"),  # custody began: today's fail-closed
        (None, "provider_stopped_custody_unsealed"),  # unknown is never retryable
    ],
)
def test_the_receipt_reason_follows_the_guardians_word_on_custody(custody, reason):
    receipt = drive(Reallocated(custody), max_polls=4)
    assert receipt["reason"] == reason, receipt["reason"]
    assert receipt["status"] == "inconclusive"
    assert receipt["custody_work_begun"] is custody


def test_another_stop_cause_is_never_classified_as_a_reallocation():
    session = Reallocated(False)
    session.proof["stop_cause"] = "deadline"
    assert drive(session, max_polls=4)["reason"] == "provider_stopped_custody_unsealed"


def _attempt(detail):
    return {
        "salad_verdict": "failed",
        "salad_evidence_digest": "e" * 64,
        "failure_detail": detail,
        "manifest_id": None,
    }


def test_the_provider_reports_a_retryable_interruption_only_for_the_classified_detail():
    provider = SaladRouteProvider.__new__(SaladRouteProvider)
    moved = provider.observe(
        None, _attempt(f"{REALLOCATED_PRECUSTODY}: allocation changed; termination confirmed")
    )
    assert moved.outcome == "failed_transient" and moved.failure_class == REALLOCATED_PRECUSTODY
    after_custody = provider.observe(
        None, _attempt("allocation changed; termination confirmed by provider read")
    )
    assert after_custody.outcome == "failed_permanent"
    assert after_custody.failure_class == "salad_not_passed"
    other = provider.observe(None, _attempt("live receipt not passed: journey_incomplete"))
    assert other.outcome == "failed_permanent"


# -- the spec -------------------------------------------------------------------------------


def test_the_bound_defaults_to_two_and_a_default_spec_keeps_its_digest():
    policy = RetryPolicy()
    assert policy.max_reallocation_retries == 2
    assert "max_reallocation_retries" not in policy.model_dump(mode="json")
    changed = RetryPolicy(max_reallocation_retries=1).model_dump(mode="json")
    assert changed["max_reallocation_retries"] == 1
    for bad in (-1, 4, True, "2"):
        with pytest.raises(ValueError):
            RetryPolicy(max_reallocation_retries=bad)


# -- the runner -----------------------------------------------------------------------------


@pytest.fixture
def reallocate(monkeypatch):
    """Classify the first N killed attempts as reallocated before custody."""
    state = {"left": 99}
    original = CampaignRunner._infra_failure

    def classify(self, ctx, key, spec_stage, attempt, exit_code, status, failure_class, *a, **kw):
        if exit_code == KILLED and state["left"] > 0:
            state["left"] -= 1
            return "failed_transient", REALLOCATED_PRECUSTODY, f"{REALLOCATED_PRECUSTODY}: moved"
        return original(
            self, ctx, key, spec_stage, attempt, exit_code, status, failure_class, *a, **kw
        )

    monkeypatch.setattr(CampaignRunner, "_infra_failure", classify)
    return state


def _world(tmp_path, retry=None, **spec_args):
    world = World(tmp_path)
    spec = world.env.spec(**spec_args)
    spec["stages"] = [
        stage_spec(world.env, "s1", metrics=METRIC, retry={"max_attempts": 1, **(retry or {})})
    ]
    return world, world.campaign(spec)["id"]


def _classes(world, cid):
    return [(a["status"], a["failure_class"]) for a in world.attempts(cid)]


def test_a_reallocation_before_custody_is_retried_on_a_fresh_attempt_and_succeeds(
    tmp_path, reallocate
):
    reallocate["left"] = 1
    world, cid = _world(tmp_path)
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert _classes(world, cid) == [
        ("failed_transient", REALLOCATED_PRECUSTODY),
        ("succeeded", None),
    ]
    one, two = world.attempts(cid)
    assert one["grant_id"] != two["grant_id"]  # a fresh attempt, never the old one re-bound
    assert world.row("SELECT held_cents FROM campaigns WHERE id=?", cid)["held_cents"] == 0
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    entries = {a["attempt"]: a for stage in receipt["stages"] for a in stage["attempts"]}
    assert entries[1]["failure_class"] == REALLOCATED_PRECUSTODY  # every attempt has a receipt
    assert entries[1]["run_receipt_digest"] and entries[2]["run_receipt_digest"]


def test_retries_exhausted_fails_the_stage_with_every_attempt_on_the_record(tmp_path, reallocate):
    world, cid = _world(tmp_path)  # the default bound: two retries
    world.scenarios = [(KILLED, OK)] * 5
    world.tick_all(cid)
    assert world.status(cid) in {"failed", "stopped"}, dump(world, cid)
    assert _classes(world, cid) == [("failed_transient", REALLOCATED_PRECUSTODY)] * 3
    assert reallocation_attempts(world.attempts(cid)) == 3
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == "stage_failed:s1:provider_reallocated_before_custody_exhausted"
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    attempts = [a for stage in receipt["stages"] for a in stage["attempts"]]
    assert [a["attempt"] for a in attempts] == [1, 2, 3]


def test_a_stage_may_turn_the_retries_off_or_lower_them(tmp_path, reallocate):
    for name in ("off", "one"):
        (tmp_path / name).mkdir(mode=0o700)
    world, cid = _world(tmp_path / "off", retry={"max_reallocation_retries": 0})
    world.scenarios = [(KILLED, OK)] * 3
    world.tick_all(cid)
    assert len(world.attempts(cid)) == 1, dump(world, cid)
    world, cid = _world(tmp_path / "one", retry={"max_reallocation_retries": 1})
    world.scenarios = [(KILLED, OK)] * 3
    world.tick_all(cid)
    assert len(world.attempts(cid)) == 2, dump(world, cid)


def test_a_reallocation_does_not_use_the_scientific_attempt_bound(tmp_path, reallocate):
    reallocate["left"] = 2
    world, cid = _world(tmp_path)  # max_attempts 1, and two reallocations before the real run
    world.scenarios = [(KILLED, OK), (KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert len(world.attempts(cid)) == 3


def test_the_budget_cap_stops_the_retries(tmp_path, reallocate):
    # Each attempt holds 6 cents; a 7-cent cap leaves no room for a second hold while the first
    # is unsettled, and the refusal is recorded instead of exceeding the cap.
    world = World(tmp_path)
    spec = world.env.spec(budget_cap_cents=7)
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, max_exposure_cents=6)]
    cid = world.campaign(spec)["id"]
    world.scenarios = [(KILLED, OK)] * 5
    world.tick_all(cid)
    row = world.row(
        "SELECT budget_cap_cents, held_cents, settled_cents FROM campaigns WHERE id=?", cid
    )
    assert row["held_cents"] + row["settled_cents"] <= row["budget_cap_cents"]
    assert len(world.attempts(cid)) < 4, dump(world, cid)
