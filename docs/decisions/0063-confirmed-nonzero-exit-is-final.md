# 0063 — A confirmed nonzero exit is a final failure with its exit code

## Status

Implemented (no schema change). Narrows
[ADR 0053](0053-resolve-attempts-on-confirmed-exit.md) and
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) section 4.

## Context

A container that exits with code 1 and leaves none of its declared outputs never gets a run
receipt: the worker cannot seal output that is not there, so nothing after the confirmed exit
posts. Control then knew the exit was confirmed but not the code, and the resolution of
ADR 0053 recorded the attempt as a transient failure (`exit_confirmed_unresolved`, "the run
receipt or outputs never completed"). A task failure looked like infrastructure trouble, the
exit code was missing from the receipt, and a stage with retries left was run again.

## Decision

1. **The code travels with the confirmed exit.** The worker's observation report carries
   `exit_code` when, and only when, it reports an exited, termination-confirmed container
   (control refuses it otherwise). It is reported before outputs are collected, so it exists
   even when collection fails. Control keeps it on the existing `execution.observation_reported`
   event, so there is no schema change; an observation without it (an older worker, or a
   non-exit) is replayed with the same digest as before.
2. **Classification.** Resolution on a confirmed exit uses the run receipt's code and, when
   there is no receipt, the observed code, and classifies it as the normal observation does: a
   code the spec declares transient is `failed_transient` (`t2_declared_exit_code`); any other
   nonzero code is `failed_permanent` (`undeclared_exit_code`), whether or not outputs exist.
   The attempt records the code, and the campaign's stop reason names it
   (`stage_failed:<stage>:undeclared_exit_code:exit_code=<n>`).
3. **Infrastructure exits keep their retry path.** An exit code that means the platform ended
   the container, not the task (137: the engine or the out-of-memory killer), with no receipt
   stays `failed_transient` / `exit_confirmed_unresolved`, now with the code in the detail and
   on the attempt, handled by the stage's retry policy as before. The set is
   `INFRA_EXIT_CODES` in `campaign_exited_resolve.py`; the spec-declared infrastructure codes
   of [ADR 0058](0058-infrastructure-retry-of-stages.md) extend this when that lands.
4. **Unchanged.** Exit 0 is never a pass without the normal collect-and-verify path. Every
   other guard of ADR 0053 (a live lease or upload in progress, the grace period, a recorded
   cost never above the hold, no contradicting observation) still applies, so a worker still
   collecting is not overtaken.


## Composition with infrastructure retries (ADR 0058, batch integration)

Both ADRs classify a confirmed exit of 137, so they are tied together here:

- With the spec's `retry.infra_retries` **off** (the default), a 137 with no run receipt keeps
  the historical retried path (`exit_confirmed_unresolved`, one scientific retry), as this ADR
  describes.
- With it **on**, that 137 is an ordinary final nonzero exit at resolution
  (`undeclared_exit_code`, carrying the code), and the runner then reclassifies it as
  `infra_container_killed` under the spec's own bound, never spending a scientific retry; when
  the bound is spent it stays final and the exhaustion is recorded.
- The codes that count are the spec's `retry.infra_exit_codes` (default 137), not a fixed
  list: a spec that names other codes gets those, and 137 is then an ordinary failure.
- Every other nonzero code stays final here and is never an infrastructure retry.

Tests: `tests/test_exit_nonzero_final.py` (composition cases) and
`tests/test_campaign_infra_retry.py`.
