# 0187 — A guardian-observed pre-custody reallocation is retried like a route-flagged one

Addendum to ADR 0179 (which stands unchanged).

## Context

ADR 0179 made a reallocation before custody retryable, but the campaign driver only reached that
classification when the route itself carried `reallocated=true`. A live run was seen where the
guardian observed `allocation_changed` before any custody work and the live receipt said
`provider_reallocated_before_custody`, while the route's flag stayed false. The attempt took the
ordinary not-passed path and ended `salad_not_passed` (permanent), so no retry ran.

## Decision

1. When the route is not flagged, a not-passed live receipt is classified
   `provider_reallocated_before_custody` (failed, retryable) only if the receipt itself says
   `reason == provider_reallocated_before_custody`, `guardian_stop_cause == allocation_changed`,
   `custody_work_begun is False` (literally; `None` is unknown) and
   `provider_stop_confirmed is True`. The ADR 0149 checks (provider-confirmed termination, a
   confirmed exited worker observation) run first and are unchanged.
2. The attempt's failure detail records the triggering signal as a closed value:
   `signal=route_flag` or `signal=guardian_evidence`.
3. The retry uses the same failure class, so the same `reallocation_attempts` counter,
   `max_reallocation_retries` bound and budget cap apply. Post-custody, unknown or other-cause
   stops are unchanged.
