# 0148 — Allocation instability per GPU class, and the next attempt's fallback

## Status

Implemented. Operator opt-in, off by default. No schema change and no spec change. Extends the
candidate placement of [ADR 0060](0060-capacity-aware-stage-placement.md) and the operator
placement preferences of ADR 0070 and [ADR 0088](0088-default-placement-by-stage-class.md). The
trust and data-class rules of [ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) and
[ADR 0047](0047-agent-routed-data-destinations.md), the pre-approval coverage of
[ADR 0066](0066-preapproval-covers-salad-alternates.md) and the heavy-stage guard of
[ADR 0107](0107-heavy-stage-placement-guard.md) are unchanged.

**Scope in one sentence:** the fallback is to another approved candidate of the stage (usually a
CPU host), NOT to another Salad GPU class; a Salad-only stage only records
`allocation_unstable_no_alternate`. An unstable run of a stage that lists `alternate_profiles`
does not move it to another profile: under the strict rule of
[ADR 0169](0169-salad-profile-fallback-policy.md) the stage is held for a decision
(`post_start_switch_refused`, [ADR 0151](0151-salad-alternate-execution-profiles.md)).

**Amendment (ADR 0169).** The reorder of item 4 applies only while no earlier attempt of the stage
may have started (none, or only attempts recorded `not_started` or `prestart_no_spend`), so a
stage never changes class after a possible start or spend. When the reorder puts a heavy stage's
(ADR 0107) local candidate ahead of an unstable remote one, the heavy-stage guard records the
explicit reason `allocation_unstable` with that remote candidate, so the local placement carries
the recorded warning (or is skipped in `refuse` mode) and is never silent or unexplained.

## Context

A rented GPU provider can move a running container to another node shortly after it started. The
guardian then stops the attempt (the allocation changed after work began), and a retry placed the
same way lands on the same GPU class and often fails the same way. The platform already records
the evidence: the control plane's route state (`remote_execution.state`) sets `reallocated` when
a provider readback shows a different allocation, keeps each readback with its time, and names the
route's `gpu_class`; the guardian's final status records `allocation_uncertain`. Nothing read that
evidence back, so placement kept choosing a class that had just failed.

## Decision

1. **Operator setting.** `placement_preferences.allocation_instability` in the operator's provider
   file, a closed object: `window_seconds` (60 to 21600, default 900), `threshold` (1 to 10,
   default 2) and `lookback_seconds` (3600 to 604800, default 86400). Unknown keys, out-of-range
   values and non-integers are refused when the file loads and install nothing. Its presence is
   the opt-in; without it nothing is read, reordered or recorded, so every receipt and placement
   record stays as before.
2. **What counts.** A route is unstable when its allocation changed within `window_seconds` of
   first running: the first readback in the `running` state (or, if none, the first `create` or
   `start` effect) to the first readback with a different allocation or the proxy's
   `reallocation_uncertain` reason. When only the guardian saw the change, its final status time is
   an upper bound and counts only if that bound is inside the window. A change whose timing cannot
   be read is recorded and not counted. Preemption is covered only as far as the provider reports
   it as a reallocation.
3. **Per class, per tenant.** Unstable routes are counted per GPU class over the campaign's own
   organization and project within `lookback_seconds` (each route once, latest state). A
   candidate's class comes from its provider through an optional `allocation_class(stage)` report
   (the Salad provider returns its operator profile's `gpu_class`); placement names no vendor.
4. **Next attempt only.** When a new attempt number is placed and the first candidate in the
   order (after the operator rules, the default policy and the ADR 0058/0060 retry rules) is on a
   class whose count reached `threshold`, the eligible candidates move to the front in their
   current order. Eligible means: an approved placement candidate of the stage (not a reroute-only
   target), not excluded or unpinned by steering, not itself on an unstable class, a hold within
   the stage's per-run cap, and, for a third-party candidate, covered by the stage's own review
   pre-approval naming it. Every ADR 0060 check still runs on every candidate in the new order,
   including trust, the ADR 0040 data-class guard and licence labels, so a fallback the guard
   refuses is skipped with its reason as before. A placement already recorded for an attempt
   number stays binding, so an attempt in flight is never moved and nothing is re-bound once
   custody has begun.
5. **No stable alternate.** When no candidate is eligible, or every eligible one fails a check,
   the order is unchanged and the attempt is placed as before. Nothing is skipped by this rule, so
   it cannot make a stage wait or loop.
6. **Record.** The `campaign.stage_placed` payload and the receipt attempt's
   `placement.allocation_instability` carry `outcome` (`allocation_unstable_fallback` or
   `allocation_unstable_no_alternate`, or null when a class was counted but the first candidate
   was below the threshold), `from_provider`, `from_class`, `chosen`, `to_class`, `threshold`,
   `window_seconds`, `counts` (per class), `classes` (per candidate), `eligible`, `refused`
   (`[{provider_id, reason}]` with reasons `same_class_unstable`, `needs_approval`,
   `over_run_cap`, `not_a_placement_candidate`, `excluded`) and `order`. A receipt attempt whose
   own route changed allocation carries `allocation_instability` (`route_id`, `gpu_class`,
   `source`, `elapsed_seconds`, `window_seconds`, `unstable`, `timing`).

## What it does not do

- It does not add a destination, raise a cap, widen trust or approve anything. It only reorders
  candidates the approved spec lists.
- It cannot switch a stage to a different GPU class of the *same* provider. A stage names one
  Salad operator profile, and candidate provider ids are distinct, so an approved spec lists at most
  one Salad GPU class per stage. Choosing among several Salad profiles needs an approved spec field
  for alternate profiles, re-scaffolding the Salad stage for the new profile and pre-approval
  entries per profile. That is a separate contract change. Until then a Salad-only stage at the
  threshold records `allocation_unstable_no_alternate` and runs on its approved class.
- It does not stop, cancel or settle anything, and does not change the guardian or how an
  unconfirmed termination is held.
- Counts do not cross projects or organizations. Pre-start re-binds (allocation changes before
  anything ran) are not counted.

## Consequences

- With the setting on, a stage that lists another approved candidate stops retrying straight onto
  a GPU class that keeps losing its allocation, and the receipt shows the counts, the move and why
  each other candidate did or did not qualify.
- Default off: existing deployments, receipts and tests are unchanged until an operator opts in.
- Tests use fakes only (`tests/test_allocation_instability.py`): route states are test doubles,
  and the trust refusal and the cap are forced as in the ADR 0088 tests. No provider was called.
