"""Read-only baseline-versus-intervention comparison of one terminal campaign receipt.

Pure and arithmetic only: it reads recorded measurements from the campaign's immutable
receipt and subtracts. Each arm must have exactly one succeeded attempt (a failed attempt
is not a result), every arm must have consumed the identical evaluation input(s), and every
requested metric must be one finite recorded number per arm. The evaluation inputs are the
ones the caller names plus the ones the approved spec itself declares for each arm (its
``evaluation.holdout.input``), and every arm must declare the same
``gate_evidence.split_lock_id``, so a caller cannot make two arms look comparable by naming only
an input that matches. Anything else is refused rather than dropped. It never decides,
recommends or assesses scientific validity.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .store import Conflict

MAX_INTERVENTIONS = 8
MAX_METRICS = 16
MAX_EVALUATION_INPUTS = 8
_IDENTITY_FIELDS = ("sha256", "snapshot_id", "digest")

METHOD = (
    "delta = intervention value minus baseline value, computed from recorded numbers only. "
    "Each arm has exactly one succeeded attempt, so there is no uncertainty interval. All "
    "arms consumed the identical named evaluation input. This is arithmetic on recorded "
    "measurements, not a decision or a statement about scientific validity."
)
SELECTION_METHOD = (
    " The selected arm is the intervention with the best recorded validation value of the "
    "named metric; an exact tie is refused. Held-out test metrics are never used to choose "
    "an arm and are reported only for the baseline and the selected arm."
)


class ComparisonRefused(Conflict):
    """The comparison cannot be made honestly; ``code`` is a stable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"comparison_refused:{code}: {message}")
        self.code = code


def _identity(item: dict[str, Any]) -> tuple[str, str] | None:
    for field in _IDENTITY_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value:
            return field, value
    return None


def _arm_attempt(stages: list[dict[str, Any]], stage_key: str) -> dict[str, Any]:
    stage = next((s for s in stages if s.get("stage_key") == stage_key), None)
    if stage is None:
        raise ComparisonRefused("stage_missing", f"Stage {stage_key} is not in the receipt.")
    succeeded = [a for a in stage.get("attempts") or [] if a.get("status") == "succeeded"]
    if len(succeeded) != 1:
        raise ComparisonRefused(
            "arm_attempt_not_unique",
            f"Stage {stage_key} must have exactly one succeeded attempt.",
        )
    return succeeded[0]


def _metric_value(attempt: dict[str, Any], stage_key: str, name: str) -> float | int:
    values = [
        m.get("value")
        for m in attempt.get("measurements") or []
        if isinstance(m, dict) and m.get("name") == name
    ]
    if len(values) != 1:
        raise ComparisonRefused(
            "metric_not_recorded",
            f"Stage {stage_key} must record exactly one measurement named {name}.",
        )
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ComparisonRefused(
            "metric_not_numeric", f"Measurement {name} of stage {stage_key} is not a finite number."
        )
    return value


def _arm_summary(stage_key: str, attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage_key": stage_key,
        "attempt": attempt.get("attempt"),
        "evidence_kind": attempt.get("evidence_kind"),
        "evidence_digest": attempt.get("evidence_digest"),
        "run_receipt_digest": attempt.get("run_receipt_digest"),
    }


def _declared(stage_spec: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """``(split_lock_id, holdout input name)`` the approved spec declares for a stage."""
    if not isinstance(stage_spec, Mapping):
        return None, None
    evidence = stage_spec.get("gate_evidence")
    lock = evidence.get("split_lock_id") if isinstance(evidence, Mapping) else None
    evaluation = stage_spec.get("evaluation")
    holdout = evaluation.get("holdout") if isinstance(evaluation, Mapping) else None
    name = holdout.get("input") if isinstance(holdout, Mapping) else None
    return (lock if isinstance(lock, str) else None, name if isinstance(name, str) else None)


def _eval_identity(attempt: dict[str, Any], stage_key: str, name: str) -> tuple[str, str]:
    items = [
        i
        for i in attempt.get("io") or []
        if isinstance(i, dict) and i.get("direction") == "input" and i.get("name") == name
    ]
    identity = _identity(items[0]) if len(items) == 1 else None
    if identity is None:
        raise ComparisonRefused(
            "evaluation_input_missing",
            f"Stage {stage_key} has no single recorded input named {name} with an identity.",
        )
    return identity


def _check_request(
    baseline: str,
    interventions: list[str],
    metrics: list[str],
    inputs: list[str],
    *,
    metric_required: bool = True,
) -> None:
    if not interventions:
        raise ComparisonRefused("no_intervention", "Name at least one intervention stage.")
    if metric_required and not metrics:
        raise ComparisonRefused("no_metric", "Name at least one metric.")
    if len(interventions) > MAX_INTERVENTIONS:
        raise ComparisonRefused("too_many_arms", f"At most {MAX_INTERVENTIONS} interventions.")
    if len(metrics) > MAX_METRICS or len(inputs) > MAX_EVALUATION_INPUTS:
        raise ComparisonRefused("too_many_metrics", f"At most {MAX_METRICS} metrics.")
    if baseline in interventions or len(set(interventions)) != len(interventions):
        raise ComparisonRefused(
            "arms_not_distinct", "The baseline and interventions must be distinct stages."
        )
    if len(set(metrics)) != len(metrics):
        raise ComparisonRefused("too_many_metrics", "Metric names must not repeat.")
    if not inputs or len(set(inputs)) != len(inputs):
        raise ComparisonRefused(
            "evaluation_inputs_unnamed",
            "Name the evaluation input(s) every arm must have consumed (distinct names).",
        )


def _shared_inputs(
    names: list[str],
    attempts: dict[str, dict[str, Any]],
    baseline: str,
    interventions: list[str],
    role: str | None = None,
) -> dict[str, Any]:
    keys = [baseline, *interventions]
    inputs: dict[str, Any] = {}
    for name in names:
        base = _eval_identity(attempts[baseline], baseline, name)
        for key in interventions:
            if _eval_identity(attempts[key], key, name) != base:
                raise ComparisonRefused(
                    "evaluation_inputs_differ",
                    f"Evaluation input {name} differs between {baseline} and {key}.",
                )
        inputs[name] = {"sha256": base[1], "identity_field": base[0], "arms": keys}
        if role:
            inputs[name]["split_role"] = role
    return inputs


def _metric_rows(
    names: list[str],
    attempts: dict[str, dict[str, Any]],
    baseline: str,
    arm_keys: list[str],
    role: str | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for name in names:
        base_value = _metric_value(attempts[baseline], baseline, name)
        arms = []
        for key in arm_keys:
            value = _metric_value(attempts[key], key, name)
            arms.append(
                {
                    "stage_key": key,
                    "attempt": attempts[key].get("attempt"),
                    "value": value,
                    "delta": value - base_value,
                    "run_receipt_digest": attempts[key].get("run_receipt_digest"),
                }
            )
        row: dict[str, Any] = {"name": name}
        if role:
            row["split_role"] = role
        row["baseline"] = {
            "value": base_value,
            "stage_key": baseline,
            "attempt": attempts[baseline].get("attempt"),
            "run_receipt_digest": attempts[baseline].get("run_receipt_digest"),
        }
        row["interventions"] = arms
        rows.append(row)
    return rows


def _check_roles(
    metrics: list[str],
    evaluation_inputs: list[str],
    validation_metrics: list[str],
    test_metrics: list[str],
    validation_inputs: list[str],
    test_inputs: list[str],
    select_by: str,
    select_direction: str | None,
) -> None:
    if metrics or evaluation_inputs:
        raise ComparisonRefused(
            "split_role_missing",
            "In selection mode name metrics and inputs by role (validation_*/test_*), "
            "not as plain metric or evaluation_input.",
        )
    if not validation_metrics or not validation_inputs or not test_inputs:
        raise ComparisonRefused(
            "split_role_missing",
            "Selection needs at least one validation metric, validation input and test input.",
        )
    if select_by not in validation_metrics and select_by not in test_metrics:
        raise ComparisonRefused(
            "split_role_missing", f"select_by {select_by} is not a named validation metric."
        )
    if set(validation_metrics) & set(test_metrics) or set(validation_inputs) & set(test_inputs):
        raise ComparisonRefused(
            "split_role_conflict", "A metric or input name is in both the validation and test role."
        )
    if select_direction not in ("max", "min"):
        raise ComparisonRefused(
            "selection_direction_missing", "select_by needs select_direction max or min."
        )
    if select_by in test_metrics:
        raise ComparisonRefused(
            "selection_reads_test_metric",
            f"{select_by} is a test metric: held-out test metrics are never used to choose an arm.",
        )


def _select(
    attempts: dict[str, dict[str, Any]],
    interventions: list[str],
    select_by: str,
    select_direction: str,
) -> tuple[str, dict[str, float | int]]:
    values = {key: _metric_value(attempts[key], key, select_by) for key in interventions}
    best = (max if select_direction == "max" else min)(values.values())
    winners = [key for key, value in values.items() if value == best]
    if len(winners) > 1:
        raise ComparisonRefused(
            "selection_tie",
            f"Interventions {', '.join(winners)} share the best {select_by} validation value.",
        )
    return winners[0], values


def compare_arms(
    receipt_doc: dict[str, Any],
    *,
    baseline: str,
    interventions: list[str],
    metrics: list[str] = (),  # type: ignore[assignment]
    evaluation_inputs: list[str] = (),  # type: ignore[assignment]
    validation_metrics: list[str] = (),  # type: ignore[assignment]
    test_metrics: list[str] = (),  # type: ignore[assignment]
    validation_inputs: list[str] = (),  # type: ignore[assignment]
    test_inputs: list[str] = (),  # type: ignore[assignment]
    select_by: str | None = None,
    select_direction: str | None = None,
    declared: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare the arms of one terminal receipt.

    ``declared`` maps stage key to that stage's entry of the approved spec (the one whose digest
    the receipt carries): what each arm declared as its split lock and held-out input. It is
    required, never inferred from the caller, so the comparison is derived from the record.
    In selection mode a declared held-out input is always a test-role input: it is checked
    like a named ``test_input`` and can never serve as validation data."""
    selecting = select_by is not None
    if selecting:
        _check_roles(
            metrics,
            evaluation_inputs,
            validation_metrics,
            test_metrics,
            validation_inputs,
            test_inputs,
            select_by,
            select_direction,
        )
    elif (
        validation_metrics
        or test_metrics
        or validation_inputs
        or test_inputs
        or select_direction is not None
    ):
        raise ComparisonRefused(
            "split_role_missing", "Role metrics, role inputs and select_direction need select_by."
        )
    if selecting:
        _check_request(baseline, interventions, validation_metrics, validation_inputs)
        _check_request(baseline, interventions, test_metrics, test_inputs, metric_required=False)
    else:
        _check_request(baseline, interventions, metrics, evaluation_inputs)
    receipt = receipt_doc.get("receipt")
    stages = receipt.get("stages") if isinstance(receipt, dict) else None
    if not isinstance(stages, list):
        raise ComparisonRefused("stage_missing", "The receipt records no stages.")

    keys = [baseline, *interventions]
    attempts = {key: _arm_attempt(stages, key) for key in keys}

    locks = {key: _declared(declared.get(key))[0] for key in keys}
    for key in interventions:
        if locks[key] != locks[baseline]:
            raise ComparisonRefused(
                "split_lock_differs",
                f"Stages {baseline} and {key} do not declare the same split lock.",
            )
    # Every held-out input any arm declares: all arms must have consumed each of them with
    # the same recorded identity, whether or not the caller named it.
    held_out: list[str] = []
    for key in keys:
        name = _declared(declared.get(key))[1]
        if name is not None and name not in held_out:
            held_out.append(name)
    result: dict[str, Any] = {
        "campaign_id": receipt_doc.get("campaign_id"),
        "receipt_digest": receipt_doc.get("receipt_digest"),
        "method": METHOD,
        "baseline": _arm_summary(baseline, attempts[baseline]),
        "interventions": [_arm_summary(key, attempts[key]) for key in interventions],
    }
    if not selecting:
        names = list(evaluation_inputs)
        names += [name for name in held_out if name not in names]
        result["evaluation_inputs"] = _shared_inputs(names, attempts, baseline, interventions)
        result["metrics"] = _metric_rows(metrics, attempts, baseline, interventions)
        result["scientific_validity"] = "not_assessed"
        return result

    # A declared held-out input is test data: added to the test role (if the caller named it
    # as a validation input, the validation_equals_test check below refuses).
    test_inputs = list(test_inputs) + [name for name in held_out if name not in test_inputs]
    inputs = _shared_inputs(validation_inputs, attempts, baseline, interventions, "validation")
    test_identities = _shared_inputs(test_inputs, attempts, baseline, interventions, "test")
    for key in keys:
        for v_name in validation_inputs:
            for t_name in test_inputs:
                if _eval_identity(attempts[key], key, v_name) == _eval_identity(
                    attempts[key], key, t_name
                ):
                    raise ComparisonRefused(
                        "validation_equals_test",
                        f"Validation input {v_name} and test input {t_name} of {key} are the "
                        "same recorded data: the validation data must not be the held-out data.",
                    )
    inputs.update(test_identities)
    selected, values = _select(attempts, interventions, select_by, select_direction)
    result["method"] = METHOD + SELECTION_METHOD
    result["evaluation_inputs"] = inputs
    result["selection"] = {
        "by": select_by,
        "direction": select_direction,
        "split_role": "validation",
        "candidates": list(interventions),
        "selected": selected,
        "values": values,
    }
    result["metrics"] = _metric_rows(
        validation_metrics, attempts, baseline, interventions, "validation"
    ) + _metric_rows(test_metrics, attempts, baseline, [selected], "test")
    result["scientific_validity"] = "not_assessed"
    return result
