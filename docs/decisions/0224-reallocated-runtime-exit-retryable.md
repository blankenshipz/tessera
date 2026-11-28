# 0224 — A non-zero exit caused by a provider move is retried as infrastructure

## Status

Accepted. Amends [ADR 0212](0212-reconciled-uncertain-no-output-retry.md) (and the non-zero-exit
classification of the ADR 0196 and ADR 0197 amendments). No schema change and no migration.

## Context

A non-zero container exit was always `failed_permanent` / `salad_container_exited_nonzero`:
a replacement machine would fail the same way if the stage's own code failed.

After a provider move that is not always true. The safety service puts the moved frame into
recovery and refuses the workload's input reads, its claim and its failure report. The GPU
runtime's custody or input call then fails, its failure report is refused, and it exits 1. The
stage's own code never ran (an entry failure carries the runtime reason `entry`). Control still
read the exit as permanent, so one such stage blocked its whole campaign and needed a
hand-submitted rerun.

## Decision

1. **Classification.** Such an exit is `failed_transient` / `infra_salad_reallocated`, with the
   detail `infra_salad_reallocated: signal=runtime_exit; ...`. It counts against
   `retry.infra_retries` and is settled like every provider-moved attempt: the guardian's
   estimate rounded up, at least 1 cent, never above the hold, as the provisional
   `guardian_bounded_hold`; otherwise the hold stands.
2. **Conditions.** Both must hold, or the exit stays permanent:
   - the guardian's own final shows the move: `allocation_uncertain` with reason
     `allocation_changed`, or the safety service's provider-confirmed close (`reconciled` /
     `provider_confirmed_exact_group_stopped`); and
   - either the runtime's last failed status line in the guardian's log tail names `custody`,
     `input_fetch`, `input_unavailable` or `output_plan`, or the tail holds no runtime status
     line and the receipt's `custody_work_begun` is exactly False.
3. **Fail closed.**
   - A status line naming `entry` or `entry_timeout` keeps the exit permanent.
   - A status line with any other reason (argv, manifest, fetch, install, output, internal and
     so on) stays permanent, even before custody began: a crash loop is what makes Salad move
     a group.
   - No status line, with custody begun or unknown, stays permanent.
   - Exit codes are never consulted. `infra_exit_codes` (default 137) is unchanged.
4. **Untrusted log.** The log tail is untrusted, because the stage writes to the same stream.
   It only narrows what the guardian's record already shows; it never widens it. The guardian's
   advisory `last_workload_refusal` is not used.
5. **Status line.** The GPU runtime adds a closed `step` token (`claim`, `manifest`, `chunk`,
   `output_plan`, `upload`) to its failure status line. The guardian's log summary keeps a
   runtime status object that Salad returns parsed (`json_log`) as one closed JSON line with
   `schema`, `status`, `reason`, `step` and an integer `exit_code`. Previously its keys and the
   integer were dropped.

## Consequences

- The runtime change takes effect only after the GPU runtime image is rebuilt and re-pinned. It
  ships in its own commit. Control's classification works with today's image, because `step` is
  optional.
- The log-summary change runs in the guardian (the safety service) and applies when the
  guardian is upgraded. Until then a parsed `json_log` status line is not readable, and such
  exits fall back to the custody-never-began rule or stay permanent.
- Residual risk: a stage that forges a custody status line and suppresses the runtime's own
  line could get at most `infra_retries` extra attempts, each within its own hold, and only on a
  frame the provider really moved.
