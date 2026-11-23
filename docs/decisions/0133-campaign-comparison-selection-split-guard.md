# ADR 0133: a campaign comparison can select an arm on validation data only

Status: Implemented (no schema change). Builds on ADR 0131 (campaign result baseline vs
intervention comparison). Read-only. No schema change.

## Context

Choosing training settings by looking at the held-out test data leaks the test set into the
choice. An arm picked because it scored best on the test split has been tuned against that split,
so its test number no longer estimates how it does on unseen data. ADR 0131 compares arms on the
recorded numbers but does not know which evaluation split a number came from, so a reader could
rank arms on test values without any signal that they had done so.

## Decision

1. `GET /campaigns/{campaign_id}/comparison` (ADR 0131) gains an optional selection mode. It is
   on when `select_by` is given; without it the route behaves exactly as ADR 0131 describes.
   New query parameters:
   - `validation_metric`: one or more measurement names (repeatable) read from validation data;
   - `test_metric`: one or more measurement names (repeatable) read from held-out test data;
   - `validation_input`: one or more input names (repeatable) that are validation data;
   - `test_input`: one or more input names (repeatable) that are held-out test data;
   - `select_by`: one validation metric name, the metric the arm is chosen by;
   - `select_direction`: `max` or `min`.
2. In selection mode every metric is given a split role. The plain `metric` parameter is not used;
   a `metric` given in selection mode is refused. Each role list needs at least one metric, and
   each role needs at least one named input.
3. Candidates are the intervention arms. The baseline is the reference and is never a candidate.
   The selected arm is the intervention with the best `select_by` value in `select_direction`.
   Ranking uses validation metrics only.
4. Test metrics are reported only for the baseline and the selected arm. The other interventions'
   test values are not returned at all, not even hidden or masked. Validation metrics are reported
   for every arm.
5. Refusals are 409 with `detail` `comparison_refused:<code>: <message>`, one stable reason code
   each. The new codes are:
   - `split_role_missing`: selection mode with a plain `metric`, no validation metric, `select_by`
     named in neither role list, or a validation or test input not named;
   - `split_role_conflict`: a metric name or an input name given in both roles;
   - `selection_reads_test_metric`: `select_by` names a test metric (held-out test metrics are never
     used to choose an arm);
   - `selection_direction_missing`: `select_by` given without `select_direction` of `max` or `min`;
   - `validation_equals_test`: a validation input and a test input carry the same recorded identity
     within an arm, so the validation data would be the held-out data;
   - `selection_tie`: two or more intervention arms share the best validation value. The comparison
     refuses rather than picking one arbitrarily.
   The ADR 0131 codes still apply to the same checks (`evaluation_inputs_differ`,
   `evaluation_input_missing`, `metric_not_recorded`, `metric_not_numeric`, and the rest). Validation
   inputs must be identical across all arms, and so must test inputs.
   The checks ADR 0131 derives from the approved spec apply in selection mode too: every arm must
   declare the same `gate_evidence.split_lock_id` (`split_lock_differs`), and a held-out input the
   spec declares (`evaluation.holdout.input`) is always a test-role input. It is identity-checked
   across all arms even when the caller does not name it, and naming it as a `validation_input`
   is refused as `validation_equals_test`.
6. A successful selection mode result adds a `selection` object:
   `{"by": metric, "direction", "split_role": "validation", "candidates": [stage keys],
   "selected": stage key, "values": {stage: value}}`. Each metric item carries `split_role`
   (`validation` or `test`). The result keeps `scientific_validity: "not_assessed"` and carries
   **no** decision, verdict or recommendation.

## Consequences

Refusal is a 409 on the read route, and nothing is recorded. The route writes no event, no row and
no receipt. A selection is a computation over an immutable campaign receipt, not a gate: it
does not stop a campaign, change a stage, or mark an arm as chosen anywhere. A caller that wants
to act on a selection does so through its own process.

Choosing an arm on validation data protects the test number only if the validation data really is
separate from the test data. The `validation_equals_test` check catches the case where the recorded
identities match, and nothing more.

Not covered:

- choosing arms across campaigns (compare the campaigns' receipts or use the research ledger);
- the statistical significance of the choice, or any uncertainty on the selected value (ADR 0131
  has no interval, and a difference is not evidence of an effect);
- sealed holdouts (ADR 0068), which are the platform's one-shot test claim. A selection in this
  comparison is a read of the receipt and does not open or use a sealed partition, and it does not
  count as the one test claim that ADR 0068 allows.

## Tests

To be listed with the implementation: each refusal code above, a `metric` given in selection mode,
a `select_by` that names a test metric, a tie between two interventions, a validation input whose
identity matches a test input, and a result where the non-selected intervention's test values are
absent from the response.

Implementation notes: role parameters or `select_direction` without `select_by`, a plain `metric` or `evaluation_input` in selection mode, and a missing validation metric, validation input or test input are all refused as `split_role_missing`; test inputs are required in selection mode even with no test metric; a missing test metric on an unselected arm is not read and not refused. A missing required query list is a 422 from request validation.
