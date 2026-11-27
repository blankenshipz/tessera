# 0206 — Salad provisioning never spends a stage's run time, and a run that never ran is infrastructure

## Status

Accepted. Narrows the Salad guardian's allocation deadline (ADR 0193), the route window
(provisioning allowance), the bounded-hold settlement (ADR 0038), the pre-custody and post-custody
reallocation retries (ADR 0179, ADR 0197) and the lost-create release (ADR 0205). No schema change
and no migration (the guardian's private database gains one column the usual additive way).

## Context

Live campaigns failed on Salad placement, not on their workloads:

- CPU capacity dried up. Two routes were created and started but never got a machine. The
  allocation deadline never started its clock, because no read both said the group started (no
  pending change) and counted zero instances. Each route ran to the guardian's outer deadline,
  which sat at its lease's hard expiry: lease issue plus the stage's `max_duration_seconds`. The
  live check then failed importing a manifest nothing had written, so the attempts were recorded
  `salad_not_passed` (permanent, which killed the campaign), and their whole holds were settled
  for zero running seconds.
- A short availability check got a machine, but the image pull took about eight minutes. Its
  first running instant landed on the same start-counted deadline, and the attempt ended
  `route_deadline_reached` after 0 s running (permanent).
- A provider reallocation during a short run (running latch cleared by `reallocated`, the
  provider's stop confirmed) did not match either guardian custody-evidence rule, and ended
  `salad_not_passed`.
- A create that was admitted (`ready`) but refused at the trusted server's own use-time recheck
  never reached the provider. Its reservation still held an open-frame slot until the route
  deadline plus the 900 s margin, and other stages were refused `single_operation_orphan_exposure`.

## Decision

1. **The lease covers provisioning.** The hard expiry of a lease for a Salad campaign attempt is
   lease issue plus the task's `max_duration_seconds` plus the route window's provisioning
   allowance (1500 s). Grant and worker-credential expiry still bound it. The admitted route
   window was already declared run plus that allowance. The workload's own bound stays the
   guardian's running cap, counted from first running.
2. **A provisioning bound that needs no start read.** The guardian stops a group as
   `allocation_timeout` at `provisioning_deadline_seconds` after arming, when all of these hold:
   no placed machine was ever latched, no allocation is bound, nothing was seen running, and the
   adapter says no custody of any kind began. The default is the provisioning allowance, never
   below the allocation deadline. An adapter that cannot say whether custody began, or raises,
   never qualifies. A `deadline` stop in that same state carries the same evidence. Control
   records it as `infra_allocation_timeout` (retryable, with its class fallback).
3. **A route limit with no run is infrastructure.** The guardian's route-limit record says
   `custody_work_begun: false` only when the adapter positively says no custody work began. A
   route limit (not the running cap) with at most 60 s running and that word is recorded as the
   transient `infra_salad_provisioning_overrun`. It is retried within `retry.infra_retries`, then
   the class/profile fallback decides. A run longer than that, unknown custody, or the running
   cap keeps today's classes.
4. **A run that never ran settles at the guardian's estimate.** For either record above, the
   attempt settles at the guardian's own accrued estimate, rounded up (zero when nothing was seen
   running), not the whole hold. The basis stays the provisional `guardian_bounded_hold`, so a
   recorded charge raises or confirms it. Only the guardian's recorded evidence selects this,
   never a receipt reason.
5. **A provider-confirmed reallocation is infrastructure.** A route-flagged reallocation with the
   provider's stop and an exited worker confirmed, but without the guardian's custody word, is
   the transient `infra_salad_reallocated`. Its outputs are never accepted. It is retried within
   `retry.infra_retries`, then the class/profile fallback decides. It settles like any failed
   attempt. The unflagged path keeps ADR 0179 and ADR 0197 unchanged, including "unknown custody
   is never retryable".
6. **An unsent create after `ready` is reported.** A failure between a successful create
   `ready` and the one provider call is reported to the guardian through `create_rejected`, with
   the transport's `request_not_sent` summary. The create-rejected release then applies: 30 s,
   and only on the guardian's own exact-name 404 and a complete project list. A failure once the
   send was called is never reported this way. A create that may have been sent keeps the 900 s
   margin, because Salad can materialise a create our client abandoned.

## Consequences

Starved capacity or a slow pull costs one infrastructure retry and about zero cents, not a
campaign. Every retry is a new attempt with its own claim and hold under the campaign and project
caps. No stop, cap or authority check is loosened: the new paths only stop earlier, or
reclassify an attempt whose guardian evidence shows it never ran. A lease for a Salad route lasts
up to 1500 s longer, within its grant and credential.
