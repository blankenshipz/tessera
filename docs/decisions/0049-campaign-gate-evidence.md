# 0049 — Campaign stages record gate metrics under the campaign approval

## Status

Implemented (no schema change). Narrows the "bound outcome is not fully unattended"
limitation of [ADR 0045](0045-campaign-gates-program-budget.md).

## Context

A conditional unlock (ADR 0045) evaluates a paired delta over `gate_metric_records`. Those
records were written only by a human (`record_metric`), and the split binding that makes a
measurement a typed per-seed metric was also human-only. A chain of campaigns whose later
start depends on an earlier result therefore still needed a person to bind and re-type
numbers.

## Decision

A v2 stage may carry an optional `gate_evidence` block: `split_lock_id`, `seed` and a list
of `{name, partition, unit}` naming stage `metrics`. It is part of the spec digest, so the
human approving the campaign approves the pre-registration. Absent, every existing spec
digest is unchanged.

The runner then performs two more delegated steps, both under the approval's authority and
the usual intent-first transaction:

1. `bind_split` right after `admit_attempt` and before dispatch, with the spec's lock, seed
   and metrics (the attempt must still be `admitted`).
2. `record_gate_metric` for each declared metric after `derive_measurement`, from the
   measurement the runner just derived from the stage's own output.

Delegation enforces the approval twice. When the intent is written, `bind_split` must declare
its `split_lock_id` as a resource, that lock must be one the approved spec declares, and only
a stage that declares `gate_evidence` may bind or record. In the store command, a
campaign-delegated bind compares the lock id, seed and metrics it actually executes with that
stage's approved block and is refused on any difference. Existing store
rules still hold: the lock must belong to the attempt's project, the partition must exist in
the lock, the measurement name must be pre-registered, and a seed binds once per variant and
lock. A refused bind fails the attempt (the grant is unwound as for any failed step). A
refused metric record leaves no record, so an unlock that needs it stays blocked (fail
closed); the receipt's measurement entry carries the error class.

**No retries.** Every attempt of a stage admits the same variant, and a seed binds once per
variant and lock (this is what prevents seed shopping), so a second attempt could never bind.
A stage that declares `gate_evidence` must therefore have `retry.max_attempts` of 1; the spec
is refused otherwise. Retry semantics for pre-registered evidence (for example one seed per
attempt) would be an explicit contract change, not a loosening of the one-seed rule.

`bind_split` and `record_gate_metric` are delegable to worker-grant providers and are not
delegated to Salad stages, which keep their own bound program gate.

## Consequences

- Manual `record_metric` and `bind_attempt_split` stay human-only.
- The split lock is still created by a human before submission.
- A recorded metric is evidence of the measured number, not scientific validation
  (`not_assessed`).
