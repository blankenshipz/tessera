"""Baseline-versus-intervention comparison of recorded measurements (README workflow step 5).

A read-only result section: for each design revision and measurement name recorded on both
arms it reports the arithmetic of the numbers Tessera already holds, with the sample sizes,
the difference of means and, when each arm has at least two succeeded attempts, a Welch 95%
interval, so a reviewer (or an agent critiquing the run) can see the size of the effect and
its uncertainty without reconstructing it by hand.

It never recommends a decision and never judges validity: attempts that did not succeed are
counted separately (a failed run is not a negative result), measurements are not verified,
and the interval assumes independent attempts and roughly normal values. Only a measurement
whose value is a finite number (a bare number or ``{"number": x}``) is used; anything else
is counted as skipped. The sample is the attempt, never the measurement: several measurements
of one name on one attempt are averaged into that attempt's single value, so repeated entries
cannot manufacture an interval.
"""

from __future__ import annotations

import json
import math
from typing import Any

from . import gate_evidence_stats as stats

COMPARISON_METHOD = (
    "difference of means (intervention minus baseline) over succeeded attempts, one value per "
    "attempt (the mean of that attempt's measurements of the name); Welch 95% interval when "
    "both arms have at least two attempts and some spread; unpaired, assumes independent "
    "attempts"
)
_MAX_ROWS = 400


def _number(raw: str) -> float | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if isinstance(value, dict):
        value = value.get("number")
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def welch_interval(
    baseline: list[float], intervention: list[float]
) -> tuple[float, float | None, float | None] | None:
    """(difference, ci_low, ci_high) of intervention minus baseline.

    None below two values per arm. When there is no spread at all the interval is undefined
    and ci_low/ci_high are None (a zero-width interval would read as certainty).
    """
    na, nb = len(intervention), len(baseline)
    if na < 2 or nb < 2:
        return None
    mean_a, mean_b = sum(intervention) / na, sum(baseline) / nb
    var_a = sum((x - mean_a) ** 2 for x in intervention) / (na - 1)
    var_b = sum((x - mean_b) ** 2 for x in baseline) / (nb - 1)
    se2 = var_a / na + var_b / nb
    difference = mean_a - mean_b
    if se2 == 0:
        return difference, None, None
    df = se2**2 / ((var_a / na) ** 2 / (na - 1) + (var_b / nb) ** 2 / (nb - 1))
    half = stats.t_critical_975(max(1, math.floor(df))) * math.sqrt(se2)
    return difference, difference - half, difference + half


def comparison_section(connection: Any, actor: dict, experiment: dict) -> dict[str, Any]:
    """Result section loader: an open read transaction, the actor, the experiment row."""
    variants = {
        row["id"]: (row["kind"], row["design_revision"])
        for row in connection.execute(
            "SELECT id,kind,design_revision FROM research_experiment_variants "
            "WHERE experiment_id=?",
            (experiment["id"],),
        )
    }
    # (revision, name, arm) -> attempt key -> that attempt's numbers; the attempt is the sample.
    per_attempt: dict[tuple[int, str, str], dict[tuple[str, str], list[float]]] = {}
    sources: dict[tuple[int, str, str], set[str]] = {}
    skipped = 0
    counted: set[tuple[str, str]] = set()
    not_counted: dict[str, set[tuple[str, str]]] = {}

    for table, attempts, label in (
        (
            "research_admitted_attempt_measurements",
            "research_experiment_admitted_attempts",
            "admitted",
        ),
        ("research_attempt_measurements", "research_experiment_attempts", "recorded"),
    ):
        for row in connection.execute(
            f"SELECT a.id AS attempt_id,a.variant_id,a.outcome,m.name,m.value_json,m.source "
            f"FROM {table} m JOIN {attempts} a ON a.id=m.attempt_id "
            "JOIN research_experiment_variants v ON v.id=a.variant_id "
            "WHERE v.experiment_id=? ORDER BY m.created_at,m.id",
            (experiment["id"],),
        ):
            if row["variant_id"] not in variants:
                continue
            attempt = (label, row["attempt_id"])
            if row["outcome"] != "succeeded":
                not_counted.setdefault(row["outcome"] or "unresolved", set()).add(attempt)
                continue
            number = _number(row["value_json"])
            if number is None:
                skipped += 1
                continue
            counted.add(attempt)
            kind, revision = variants[row["variant_id"]]
            key = (revision, row["name"], kind)
            per_attempt.setdefault(key, {}).setdefault(attempt, []).append(number)
            sources.setdefault(key, set()).add(f"{label}:{row['source']}")

    def arm_view(key: tuple[int, str, str]) -> tuple[list[float], dict[str, Any]]:
        attempts_of = per_attempt.get(key, {})
        values = [sum(v) / len(v) for v in attempts_of.values()]
        return values, {
            "n": len(values),
            "mean": sum(values) / len(values) if values else None,
            "attempts_admitted": sum(1 for a in attempts_of if a[0] == "admitted"),
            "attempts_recorded": sum(1 for a in attempts_of if a[0] == "recorded"),
            "attempts_with_repeated_measurements": sum(
                1 for v in attempts_of.values() if len(v) > 1
            ),
            "measurement_sources": sorted(sources.get(key, ())),
        }

    comparisons = []
    one_arm_only = []
    for revision, name in sorted({(r, n) for r, n, _ in per_attempt}):
        base_values, base = arm_view((revision, name, "baseline"))
        int_values, intervention = arm_view((revision, name, "intervention"))
        if not base_values or not int_values:
            one_arm_only.append(
                {
                    "design_revision": revision,
                    "name": name,
                    "arm": "baseline" if base_values else "intervention",
                }
            )
            continue
        interval = welch_interval(base_values, int_values)
        if interval is None:
            uncertainty = "not_estimable_needs_two_succeeded_attempts_per_arm"
        elif interval[1] is None:
            uncertainty = "degenerate_no_spread"
        else:
            uncertainty = "welch_95"
        arms = (base, intervention)
        comparisons.append(
            {
                "design_revision": revision,
                "name": name,
                "baseline": base,
                "intervention": intervention,
                "difference": intervention["mean"] - base["mean"],
                "ci95": None
                if interval is None or interval[1] is None
                else {"low": interval[1], "high": interval[2]},
                "uncertainty": uncertainty,
                # Machine-derived and human-entered numbers, or admitted and recorded
                # attempts, may share one arm: say so rather than pooling silently.
                "mixed_provenance": len({s for arm in arms for s in arm["measurement_sources"]})
                > 1,
            }
        )
    return {
        "method": COMPARISON_METHOD,
        "comparisons": comparisons[:_MAX_ROWS],
        "one_arm_only": one_arm_only[:_MAX_ROWS],
        "comparisons_total": len(comparisons),
        "one_arm_only_total": len(one_arm_only),
        "truncated": len(comparisons) > _MAX_ROWS or len(one_arm_only) > _MAX_ROWS,
        "attempts_counted": {
            "admitted": sum(1 for a in counted if a[0] == "admitted"),
            "recorded": sum(1 for a in counted if a[0] == "recorded"),
        },
        "attempts_not_counted": {k: len(v) for k, v in sorted(not_counted.items())},
        "measurements_skipped_non_numeric": skipped,
        "decision": "none",
        "note": (
            "Arithmetic on recorded numbers only. It does not say whether the method is "
            "sound or what to do next; failed, cancelled and unresolved attempts are not "
            "counted as results."
        ),
    }
