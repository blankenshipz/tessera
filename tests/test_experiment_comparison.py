"""Baseline-versus-intervention comparison section (README workflow step 5). Synthetic data."""

from __future__ import annotations

import math

import pytest
from gate_evidence_fixture import GateWorld

from tessera_control import agent_research_results as results
from tessera_control.experiment_comparison import comparison_section, welch_interval


def _arm(world, variant, numbers, name="rmse", outcome="succeeded"):
    for number in numbers:
        attempt = world.admitted_attempt(variant)
        world.human_measurement(attempt, number, name)
        world.set_outcome(attempt, outcome, None if outcome == "succeeded" else "synthetic")


def _section(world):
    with world.db() as connection:
        experiment = {"id": world.experiment["id"]}
        return comparison_section(connection, world.owner, experiment)


def test_welch_interval_matches_a_hand_computation():
    baseline = [2.0, 2.2, 2.4, 2.6]
    intervention = [1.0, 1.2, 1.1, 1.3]
    difference, low, high = welch_interval(baseline, intervention)
    assert difference == pytest.approx(1.15 - 2.3)
    # Welch df = 4.41, so the conservative tabulated t(4) = 2.776 applies.
    se = math.sqrt(0.05 / 3 / 4 + 0.2 / 3 / 4)
    assert (high - low) / 2 == pytest.approx(2.776 * se, rel=1e-3)
    assert welch_interval([1.0], [2.0, 3.0]) is None


def test_the_section_compares_arms_with_uncertainty_and_never_decides(tmp_path):
    world = GateWorld(tmp_path)
    _arm(world, world.baseline, [2.0, 2.2, 2.4, 2.6])
    _arm(world, world.intervention, [1.0, 1.2, 1.1, 1.3])
    section = _section(world)
    (row,) = section["comparisons"]
    assert row["name"] == "rmse" and row["uncertainty"] == "welch_95"
    assert row["baseline"]["n"] == row["intervention"]["n"] == 4
    assert row["difference"] == pytest.approx(-1.15)
    assert row["ci95"]["high"] < 0 and not row["mixed_provenance"]
    assert row["baseline"]["measurement_sources"] == ["admitted:human_entered"]
    assert row["baseline"]["attempts_admitted"] == 4 and row["baseline"]["attempts_recorded"] == 0
    assert section["decision"] == "none" and "Welch" in section["method"]
    assert section["attempts_counted"] == {"admitted": 8, "recorded": 0}


def test_one_attempt_per_arm_reports_the_difference_but_no_interval(tmp_path):
    world = GateWorld(tmp_path)
    _arm(world, world.baseline, [2.0])
    _arm(world, world.intervention, [1.5])
    (row,) = _section(world)["comparisons"]
    assert row["difference"] == pytest.approx(-0.5) and row["ci95"] is None
    assert row["uncertainty"].startswith("not_estimable")


def test_failed_attempts_are_not_results_and_one_arm_names_are_listed(tmp_path):
    world = GateWorld(tmp_path)
    _arm(world, world.baseline, [2.0, 2.1])
    _arm(world, world.intervention, [9.0, 9.0], outcome="failed")
    _arm(world, world.intervention, [1.0, 1.1])
    _arm(world, world.baseline, [5.0], name="only_baseline")
    section = _section(world)
    (row,) = section["comparisons"]
    assert row["intervention"]["n"] == 2 and row["intervention"]["mean"] == pytest.approx(1.05)
    assert section["attempts_not_counted"] == {"failed": 2}
    assert section["one_arm_only"] == [
        {"design_revision": 1, "name": "only_baseline", "arm": "baseline"}
    ]


def test_non_numeric_values_are_skipped_and_counted(tmp_path):
    world = GateWorld(tmp_path)
    attempt = world.admitted_attempt(world.baseline)
    payload = {"expected_revision": world.revision(attempt), "name": "note", "value": {"v": "x"}}
    import uuid

    from tessera_control.store import canonical_digest

    world.experiments.record_measurement(
        world.owner, attempt, uuid.uuid4().hex, canonical_digest(payload), payload
    )
    world.set_outcome(attempt, "succeeded")
    section = _section(world)
    assert section["comparisons"] == [] and section["measurements_skipped_non_numeric"] == 1


def test_the_section_is_registered_for_results_readers_only():
    sections = [s for s in results._REGISTRY if s.name == "comparison"]
    assert len(sections) == 1
    assert (sections[0].subject, sections[0].scope) == ("experiment", "results:read")


def _extra(world, attempt, number, name="rmse"):
    import uuid

    from tessera_control.store import canonical_digest

    payload = {
        "expected_revision": world.revision(attempt),
        "name": name,
        "value": {"number": number},
    }
    world.experiments.record_measurement(
        world.owner, attempt, uuid.uuid4().hex, canonical_digest(payload), payload
    )


def test_the_sample_is_the_attempt_not_the_measurement(tmp_path):
    world = GateWorld(tmp_path)
    for variant, pair in ((world.baseline, (2.0, 4.0)), (world.intervention, (1.0, 1.4))):
        attempt = world.admitted_attempt(variant)
        world.human_measurement(attempt, pair[0])
        _extra(world, attempt, pair[1])
        world.set_outcome(attempt, "succeeded")
    section = _section(world)
    (row,) = section["comparisons"]
    # One attempt per arm: two repeated numbers do not make a sample of two.
    assert row["baseline"]["n"] == row["intervention"]["n"] == 1
    assert row["ci95"] is None and row["uncertainty"].startswith("not_estimable")
    assert row["baseline"]["attempts_with_repeated_measurements"] == 1
    assert row["baseline"]["mean"] == pytest.approx(3.0)
    assert row["difference"] == pytest.approx(1.2 - 3.0)
    assert section["attempts_counted"] == {"admitted": 2, "recorded": 0}


def test_repeated_measurements_average_into_one_value_per_attempt(tmp_path):
    world = GateWorld(tmp_path)
    for variant, pairs in (
        (world.baseline, [(2.0, 4.0), (3.0, 5.0)]),
        (world.intervention, [(1.0, 1.2), (1.5, 1.7)]),
    ):
        for first, second in pairs:
            attempt = world.admitted_attempt(variant)
            world.human_measurement(attempt, first)
            _extra(world, attempt, second)
            world.set_outcome(attempt, "succeeded")
    (row,) = _section(world)["comparisons"]
    assert row["baseline"]["n"] == row["intervention"]["n"] == 2
    assert row["baseline"]["mean"] == pytest.approx(3.5)  # attempt means 3 and 4
    assert row["uncertainty"] == "welch_95"


def test_zero_spread_is_degenerate_not_a_zero_width_interval(tmp_path):
    assert welch_interval([2.0, 2.0], [1.0, 1.0]) == (-1.0, None, None)
    world = GateWorld(tmp_path)
    _arm(world, world.baseline, [2.0, 2.0])
    _arm(world, world.intervention, [1.0, 1.0])
    (row,) = _section(world)["comparisons"]
    assert row["ci95"] is None and row["uncertainty"] == "degenerate_no_spread"
    assert row["difference"] == pytest.approx(-1.0)


def test_truncation_is_flagged_with_total_counts(tmp_path, monkeypatch):
    from tessera_control import experiment_comparison as module

    world = GateWorld(tmp_path)
    for name in ("a", "b", "c"):
        _arm(world, world.baseline, [1.0, 2.0], name=name)
        _arm(world, world.intervention, [2.0, 3.0], name=name)
    assert _section(world)["truncated"] is False
    monkeypatch.setattr(module, "_MAX_ROWS", 2)
    section = _section(world)
    assert section["truncated"] is True
    assert section["comparisons_total"] == 3 and len(section["comparisons"]) == 2


def test_admitted_and_recorded_attempts_are_flagged_as_mixed(tmp_path):
    world = GateWorld(tmp_path)
    _arm(world, world.baseline, [2.0, 2.2])
    _arm(world, world.intervention, [1.0, 1.2])
    # A recorded (human-reported) attempt joins the baseline arm.
    recorded = world.post(
        f"/experiment-variants/{world.baseline['id']}/attempts",
        {
            "expected_variant_revision": world.baseline["revision"],
            "provenance_kind": "human_reported_observation",
            "human_observation": "ran by hand",
            "outcome": "succeeded",
        },
    )
    world.post(
        f"/experiment-attempts/{recorded['id']}/measurements",
        {"expected_revision": recorded["revision"], "name": "rmse", "value": {"number": 2.4}},
    )
    section = _section(world)
    (row,) = section["comparisons"]
    assert row["baseline"]["n"] == 3
    assert (row["baseline"]["attempts_admitted"], row["baseline"]["attempts_recorded"]) == (2, 1)
    assert row["mixed_provenance"] is True
    assert section["attempts_counted"] == {"admitted": 4, "recorded": 1}


def test_each_design_revision_is_compared_on_its_own(tmp_path):
    from test_goal_to_decision import _command, _design

    world = GateWorld(tmp_path)
    _arm(world, world.baseline, [2.0, 2.2])
    _arm(world, world.intervention, [1.0, 1.2])
    revised = _command(
        world.experiments.revise_design,
        world.owner,
        world.experiment["id"],
        payload=_design(world.experiment["current_design_revision"]),
    )
    new = {v["kind"]: v for v in revised["design"]["variants"]}
    _arm(world, new["baseline"], [5.0, 5.2])
    _arm(world, new["intervention"], [4.0, 4.4])
    rows = {r["design_revision"]: r for r in _section(world)["comparisons"]}
    assert sorted(rows) == [1, 2]
    assert rows[1]["difference"] == pytest.approx(-1.0)
    assert rows[2]["difference"] == pytest.approx(-0.9)
