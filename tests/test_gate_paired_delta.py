"""Paired-delta replicate groups and the bound-outcome unlock rule (ADR 0045).

Synthetic fixtures only: attempts are inserted directly, so these exercise binding and
arithmetic, not dispatch or science.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from gate_evidence_fixture import GateWorld
from test_gate_evidence_metrics import bind, make_lock, metric_body

from tessera_control import gate_evidence_stats as stats
from tessera_control.campaign_gate_models import BoundOutcomeRule, UnlockRequirement
from tessera_control.campaign_gates import evaluate_unlock
from tessera_control.gate_evidence_paired import (
    PairedRefused,
    compute_paired_delta,
    paired_outcome,
)
from tessera_control.store import utc_now


@pytest.fixture
def world(tmp_path: Path) -> GateWorld:
    return GateWorld(tmp_path)


def add_seed(world, lock, variant, seed, number, *, outcome="succeeded", record=True):
    attempt = world.admitted_attempt(variant)
    bind(world, attempt, lock, seed)
    measurement = world.human_measurement(attempt, number)
    world.set_outcome(attempt, outcome, None if outcome == "succeeded" else "synthetic failure")
    if record and outcome == "succeeded":
        world.post(
            f"/experiment-attempts/{attempt}/metric-records",
            metric_body(measurement["id"], seed),
        )
    return attempt


def arms(world, lock, a_values, b_values):
    attempts = []
    for seed, (a, b) in enumerate(zip(a_values, b_values, strict=True)):
        attempts.append(add_seed(world, lock, world.intervention, seed, a))
        attempts.append(add_seed(world, lock, world.baseline, seed, b))
    return attempts


def compute(world, lock, **kw):
    with world.db() as c:
        return compute_paired_delta(
            c,
            world.project["id"],
            arm_a_variant_id=world.intervention["id"],
            arm_b_variant_id=world.baseline["id"],
            lock_id=lock,
            metric="rmse",
            partition="test",
            **kw,
        )


def body(world, lock, **kw):
    return {
        "arm_a_variant_id": world.intervention["id"],
        "arm_b_variant_id": world.baseline["id"],
        "metric": "rmse",
        "split_lock_id": lock,
        "partition": "test",
        **kw,
    }


def path(world):
    return f"/projects/{world.project['id']}/paired-replicate-groups"


def test_paired_delta_pairs_by_seed_and_matches_hand_computation(world):
    lock = make_lock(world)
    arms(world, lock, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])  # deltas 2, 3, 4
    group = world.post(path(world), body(world, lock))
    expected = stats.aggregate([2.0, 3.0, 4.0], "t_interval_95", seed_material="x")
    assert group["n"] == 3 and group["mean"] == pytest.approx(3.0)
    assert group["ci_low"] == pytest.approx(expected["ci_low"])
    assert [p["delta"] for p in group["pairs"]] == [2.0, 3.0, 4.0]
    assert group["scientific_validity"] == "not_assessed"
    fetched = world.get(f"/paired-replicate-groups/{group['id']}")
    assert fetched["result_digest"] == group["result_digest"] and fetched["n"] == 3
    # Deterministic: recomputing from the records gives the same digest.
    assert compute(world, lock)["result_digest"] == group["result_digest"]


def test_paired_group_lookup_and_rows_are_append_only(world):
    lock = make_lock(world)
    arms(world, lock, [3.0, 5.0], [1.0, 2.0])
    world.get("/paired-replicate-groups/gate_paired_missing", status=404)
    group = world.post(path(world), body(world, lock))
    world.post(path(world), body(world, lock, ci_method="percentile_bootstrap_95"))
    # Rows are append-only.
    with world.db() as c, pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE gate_paired_groups SET n=9 WHERE id=?", (group["id"],))


def test_unpaired_seeds_are_refused_not_subsetted(world):
    lock = make_lock(world)
    arms(world, lock, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])
    # A fourth seed exists only in arm A.
    add_seed(world, lock, world.intervention, 3, 9.0)
    with pytest.raises(PairedRefused) as error:
        compute(world, lock)
    assert error.value.code == "seeds_unpaired"


def test_excluded_attempt_in_either_arm_refuses(world):
    lock = make_lock(world)
    arms(world, lock, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])
    # The same bad seed failed in BOTH arms: seed sets stay identical, but the gate
    # must not be computed on what remained.
    add_seed(world, lock, world.intervention, 3, 0.0, outcome="failed")
    add_seed(world, lock, world.baseline, 3, 0.0, outcome="failed")
    with pytest.raises(PairedRefused) as error:
        compute(world, lock)
    assert error.value.code == "attempt_excluded"


def test_unresolved_and_unrecorded_attempts_refuse(world):
    lock = make_lock(world)
    arms(world, lock, [3.0, 5.0], [1.0, 2.0])
    pending = world.admitted_attempt(world.intervention)
    bind(world, pending, lock, 2)
    with pytest.raises(PairedRefused) as error:
        compute(world, lock)
    assert error.value.code == "attempt_unresolved"
    world.set_outcome(pending, "succeeded")
    with pytest.raises(PairedRefused) as error:
        compute(world, lock)
    assert error.value.code == "metric_record_missing"


def test_too_few_pairs_and_same_arm(world):
    lock = make_lock(world)
    arms(world, lock, [3.0], [1.0])
    with pytest.raises(PairedRefused) as error:
        compute(world, lock)
    assert error.value.code == "too_few_pairs"
    with world.db() as c, pytest.raises(PairedRefused) as same:
        compute_paired_delta(
            c,
            world.project["id"],
            arm_a_variant_id=world.baseline["id"],
            arm_b_variant_id=world.baseline["id"],
            lock_id=lock,
            metric="rmse",
            partition="test",
        )
    assert same.value.code == "same_arm"


def test_paired_outcome_directions_and_sources():
    result = {"n": 5, "mean": 2.0, "ci_low": 0.5, "ci_high": 3.5, "min_sample_count": 120}
    result["sources"] = ["derived"]

    def outcome(**kw):
        base = {
            "statistic": "ci_low",
            "comparison": "gt",
            "threshold": 0.0,
            "minimum_pairs": 3,
            "minimum_samples": 100,
            "allowed_sources": ["derived"],
        }
        return paired_outcome(result, **{**base, **kw})

    assert outcome() == ("pass", None)
    assert outcome(threshold=1.0) == ("fail", None)
    assert outcome(statistic="ci_high", comparison="lt", threshold=4.0) == ("pass", None)
    assert outcome(minimum_pairs=6) == (None, "insufficient_pairs")
    assert outcome(minimum_samples=500) == (None, "insufficient_samples")
    assert outcome(allowed_sources=["human_entered"]) == (None, "disallowed_measurement_source")


# ---- bound-outcome unlock against a named campaign ----


def make_campaign(world, attempts, status="succeeded", held=0) -> str:
    campaign_id = "campaign_" + uuid.uuid4().hex
    now = utc_now()
    with world.db() as c:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute(
            """INSERT INTO campaigns (id,org_id,project_id,spec_json,spec_digest,status,
            budget_cap_cents,held_cents,settled_cents,revision,submitted_by_principal_id,
            created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,0,1,?,?,?)""",
            (
                campaign_id,
                world.owner["org_id"],
                world.project["id"],
                "{}",
                "a" * 64,
                status,
                10,
                held,
                world.owner["id"],
                now,
                now,
            ),
        )
        for index, attempt in enumerate(attempts, start=1):
            c.execute(
                """INSERT INTO campaign_stage_attempts (id,campaign_id,stage_key,attempt,status,
                hold_cents,admitted_attempt_id,revision,created_at,updated_at)
                VALUES (?,?,?,?,'succeeded',0,?,1,?,?)""",
                (
                    f"cattempt_{uuid.uuid4().hex}",
                    campaign_id,
                    f"s{index}",
                    1,
                    attempt,
                    now,
                    now,
                ),
            )
    return campaign_id


def rule(world, lock, **kw):
    base = {
        "arm_a_variant_id": world.intervention["id"],
        "arm_b_variant_id": world.baseline["id"],
        "metric": "rmse",
        "split_lock_id": lock,
        "partition": "test",
        "statistic": "mean",
        "comparison": "gt",
        "threshold": 1.0,
        "minimum_pairs": 3,
        "allowed_sources": ["human_entered", "derived"],
        "allowed_outcomes": ["pass"],
    }
    return BoundOutcomeRule.model_validate({**base, **kw})


def unlock(world, campaign_id, **rule_kw):
    lock = rule_kw.pop("lock")
    return [UnlockRequirement(campaign_id=campaign_id, bound_outcome=rule(world, lock, **rule_kw))]


def evaluate(world, requirements):
    with world.db() as c:
        return evaluate_unlock(c, world.owner["org_id"], world.project["id"], requirements)


def test_bound_outcome_unlocks_only_inside_the_allowed_set(world):
    lock = make_lock(world)
    attempts = arms(world, lock, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])
    campaign = make_campaign(world, attempts)
    reason, evaluations = evaluate(world, unlock(world, campaign, lock=lock))
    assert reason is None
    (item,) = evaluations
    assert item["basis_kind"] == "bound_outcome" and item["outcome"] == "pass"
    assert item["result_digest"] == compute(world, lock)["result_digest"]
    # The same data fails a stricter pre-registered threshold: refused, fail closed.
    reason, _ = evaluate(world, unlock(world, campaign, lock=lock, threshold=10.0))
    assert reason == "unlock_not_satisfied:0:outcome_fail_not_allowed"
    # ...unless a human pre-registered that a failing outcome is the one that unlocks.
    reason, evaluations = evaluate(
        world, unlock(world, campaign, lock=lock, threshold=10.0, allowed_outcomes=["fail"])
    )
    assert reason is None and evaluations[0]["outcome"] == "fail"


def test_bound_outcome_refuses_attempts_from_another_campaign_and_open_campaigns(world):
    lock = make_lock(world)
    attempts = arms(world, lock, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])
    partial = make_campaign(world, attempts[:-1])  # one series attempt is not its own
    reason, _ = evaluate(world, unlock(world, partial, lock=lock))
    assert reason == "unlock_not_satisfied:0:unevaluable_attempts_not_from_campaign"
    running = make_campaign(world, attempts, status="running")
    reason, _ = evaluate(world, unlock(world, running, lock=lock))
    assert reason == "unlock_not_satisfied:0:campaign_not_finished"
    held = make_campaign(world, attempts, held=3)
    reason, _ = evaluate(world, unlock(world, held, lock=lock))
    assert reason == "unlock_not_satisfied:0:exposure_unresolved"
    reason, _ = evaluate(world, unlock(world, "campaign_missing", lock=lock))
    assert reason == "unlock_not_satisfied:0:campaign_not_found"


def test_bound_outcome_recomputes_and_never_trusts_a_stored_group(world):
    lock = make_lock(world)
    attempts = arms(world, lock, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])
    campaign = make_campaign(world, attempts)
    world.post(path(world), body(world, lock))  # a stored group that says "pass"
    assert evaluate(world, unlock(world, campaign, lock=lock))[0] is None
    # A new unresolved seed appears: the stored group is now stale and must not count.
    late = world.admitted_attempt(world.baseline)
    bind(world, late, lock, 9)
    reason, _ = evaluate(world, unlock(world, campaign, lock=lock))
    assert reason == "unlock_not_satisfied:0:unevaluable_attempt_unresolved"


def test_other_project_variants_are_not_found(world):
    lock = make_lock(world)
    attempts = arms(world, lock, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])
    campaign = make_campaign(world, attempts)
    with world.db() as c:
        reason, _ = evaluate_unlock(
            c,
            world.owner["org_id"],
            "project_other",
            unlock(world, campaign, lock=lock),
        )
    assert reason == "unlock_not_satisfied:0:campaign_not_found"


def test_requirement_models_are_strict():
    with pytest.raises(ValueError, match="accept_human_go"):
        UnlockRequirement(campaign_id="c")
    with pytest.raises(ValueError):
        BoundOutcomeRule.model_validate(
            {
                "arm_a_variant_id": "x",
                "arm_b_variant_id": "x",
                "metric": "m",
                "split_lock_id": "l",
                "partition": "p",
                "statistic": "mean",
                "comparison": "gt",
                "threshold": 0,
                "minimum_pairs": 2,
                "allowed_outcomes": ["pass"],
            }
        )
