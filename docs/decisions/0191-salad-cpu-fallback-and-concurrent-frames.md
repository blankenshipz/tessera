# 0191 — An operator CPU fallback chain for Salad profiles, and an operator setting for concurrent billable frames

## Status

Accepted. Amends [ADR 0060](0060-capacity-aware-stage-placement.md) (class fallback addendum) and
[ADR 0062](0062-salad-cpu-only-profiles.md) (CPU profiles), narrows [ADR 0169](0169-salad-profile-fallback-policy.md)
by one source of next profile (the strict pre-start rule is unchanged), and replaces the "the limit
stays at 1" decision of [ADR 0041](0041-execution-frames.md) (section 7.2, resolved question 4b) with an operator
setting. No schema change and no migration.

## Context

A research stage on a small Salad CPU profile (one country, two vCPU) sat for more than twenty minutes without
an allocation, and a second stage queued behind it. Two limits caused that. A profile is one country and one
shape, and the only way to try another was a new approved spec (`alternate_profiles`, ADR 0151), which needs a
new approval. And the Salad safety service allowed exactly one open billable frame, the Salad executor drove one paid
route at a time, so nothing else could run while the first waited.

## Decision

### 1. Operator CPU fallback chain

1. **Where it lives.** A CPU profile in `tessera.salad.campaign-profiles.v1` may carry an optional
   `fallback_profiles` list: one to three distinct operator profile names, never itself, CPU only. Absent keeps every
   existing profile document and every approved spec digest byte-identical: `SaladStageSpec` is untouched.
2. **Load-time checks (fail closed).** Every name must exist in the file, be a CPU profile and share the listing
   profile's organization, project, worker principal, Salad organization, capability environment, artifact and
   checkpoint destinations, monitor and watchdog profiles, custody host and stage adapter section, so a move
   cannot change who runs the stage or what runs. Cycles are refused. Country, shape, rates, ceilings and route window may
   differ (that is the point).
3. **Order.** The approved `profile` and `alternate_profiles` first, unchanged; then each of those profiles' operator
   chain, depth first. A profile the spec already lists keeps its approved place.
4. **Trigger: the strict pre-start rule, unchanged.** An operator fallback is tried only where ADR 0169 already allows a
   move: the provider proves the attempt never started (no start effect on its route, never reallocated, a pre-start
   receipt class or a create rejection, which includes a capacity refusal at create) and the attempt settled at
   exactly zero. After any possible start or spend there is no move (ADR 0169 point 3 stays absolute).
5. **Cost bound (fail closed).** A candidate is used only if all hold, checked when the move is made: its hold is within its
   own `max_run_cents`; its CPU rate times the stage's `max_duration_seconds` is at most the stage's approved
   `max_exposure_cents`; every allowed attempt's hold is within its `max_task_cents`. It also passes every check an
   approved profile passes (tenant, worker, kind, credential, input trust and public-artifact guards, image allowlist,
   capability declaration). A candidate that fails is skipped and named on the move; if none passes the stage is held with
   `profile_not_approved` carrying the first refusal's code (`fallback_over_hold`, `over_run_cap`, `fallback_refused`,
   `capability_mismatch`, `preapproval_not_covering`). GPU profiles cannot list operator fallbacks: there is no static
   rate to bound the cost.
6. **Counted in the approved worst case.** Every operator move counts against the stage's approved
   `retry.max_class_fallbacks`, so `max_attempts + infra_retries + max_class_fallbacks` is never exceeded. A stage
   approved with `max_class_fallbacks: 0` (the default) never uses an operator fallback, and an operator `fallback_profiles` list changes
   nothing about how such a stage is handled, including after a possible start: the post-start hold of ADR 0169 point 3
   (`post_start_switch_refused`) applies to a single-profile stage only when its own `max_class_fallbacks` is at least 1
   (or it lists spec alternates, as before). This is the one place a spec-level
   decision cannot be avoided: using a fallback spends money the approver did not budget, so the approver opts in per stage
   with `max_class_fallbacks`; the operator then chooses which profiles are the fallbacks without a new approval.
7. **Review pre-approvals.** An operator fallback is covered by a review pre-approval only when the stage is on its own
   provider, the entry has no per-profile bindings, the hold and CPU worst case are within the entry's own ceilings and
   the plan digest the runner will propose on the fallback equals the entry's bound digest; otherwise the stage is held
   (`preapproval_not_covering`) and a person decides.
8. **Receipts.** Each move is one `campaign.salad_profile_switched` event with a new `source` (`spec_alternate` or
   `operator_fallback`; older events read as `spec_alternate`), and operator moves add `operator_order` and `skipped`. The
   receipt's `salad_profiles.switches`, each attempt's `salad_profile` and the stage-status read show the source, and the
   paired-comparison class check reads the same events so a result is never labelled with the wrong class.

9. **A route that is created and started but never allocated (amends ADR 0169 point 3).** The stalled stage in the Context did not fail
   at create: it passed create and the start request, then no instance arrived. The guardian's allocation deadline stops such a group
   (stop cause `allocation_timeout`; a group-level `running` status or a machine-less allocating count is not an allocation) and control
   records the attempt `failed_transient` / `infra_allocation_timeout`. ADR 0169 point 3 forbids a move after a start effect, so this
   narrow exception is added: a CPU Salad stage on its own provider may move to its next profile (spec alternates first, then the operator
   chain) after such an attempt, ONLY when control's own records prove all of: the guardian's final carries `allocation_timeout`, status
   `stopped`, no orphan, uncertain allocation, pending escalation, budget fence or unknown rate, no billed cost, and an estimate of exactly
   zero; the attempt settled under `guardian_bounded_hold`, which the settlement path records only after control's own guardian read said
   `stopped`, and its frame carries that verdict digest; the route was never reallocated and the live receipt did not pass. The move uses
   the same selector, coverage checks and `max_class_fallbacks` bound as a pre-start move, and that bound now counts pre-start and
   allocation-timeout moves together. It is one `campaign.salad_profile_switched` event with the closed reason `allocation_timeout`
   (`started: true`, `spent: false`, the settled amount and basis, `estimated_exposure_cents: "0"`, closed evidence) committed in the same
   transaction as the attempt's unchanged `failed_transient` transition, so a restart cannot record it twice. GPU stages, stages with
   alternate or reroute candidates, and any missing, malformed or ambiguous record keep today's infrastructure retry path; an uncovered
   next profile also keeps that path rather than holding the stage. **Settlement is unchanged:** per ADR 0041 section 7.3 point 2 a created
   frame settles at its hold under `guardian_bounded_hold` (non-final) until a human charge or reconcile; no zero settlement or new
   basis is introduced (that would need a migration and its own review). The timed-out attempt still counts as an infrastructure attempt and
   the next attempt reserves its own hold, so the approved worst case `max_attempts + infra_retries + max_class_fallbacks` stays an upper
   bound and the first hold stays reserved. The guardian's rule that `allocation_timeout` fires only when no instance, allocation or running
   time was ever seen is relied on, not re-proven here. Residuals: an uncovered fallback leaves no trace on the receipt (it keeps the
   retry path silently); read views show a moved attempt as one infrastructure retry used even with `infra_retries` 0; the move is exercised
   in tests with operator-chain CPU stages (spec alternates share the selector but have no end-to-end CPU test).

### 2. Concurrent billable frames and routes

1. **Guardian.** `max_open_billable_frames` is an operator setting: an integer 1 to 4, default 1; any other value (0, 5, a bool,
   a string, a float) refuses startup. With more than one frame every per-frame rule still holds per frame (its own rate,
   accrual ledger, deadlines, heartbeat, reallocation handling and stop); a cap breach or fence on one frame stops that
   frame only. The total cap is enforced in aggregate: closed frames' frozen accrual plus `max(accrued, hold)` of every
   other frame, read in the same `BEGIN IMMEDIATE` transaction as the open-frame count and the insert; after every loop
   pass the guardian recomputes that total and, if it is over the cap, fences and stops every frame that may still bill. An
   ambiguous create counts until proven absent or stopped, and a stopped or settled frame frees exactly one slot. Two open
   frames may never name the same provider group nor be bound to the same group id (`provider_group_in_use`,
   `provider_identity_in_use`).
2. **The unseen-run gap grows with the setting.** One loop pass reads every open frame, so the gap charged at admission and
   used to backdate accrual is 45 s plus 16 s per extra frame (45, 61, 77, 93 s). At 1 it is exactly 45 s.
3. **Per-frame isolation.** One loop pass runs each open frame in its own guarded step. An exception in one frame (arm, accrual,
   cancel, poll) is recorded for that frame (event and frame note `loop_frame_failed`, the exception type name only), fails that frame
   closed through the ordinary fence and stop, and never aborts the pass for the other frames, skips the aggregate cap check or takes down
   the service. `KeyboardInterrupt` and `SystemExit` are never swallowed.
4. **A lost-create frame keeps its hold reserved while it may reopen.** A frame closed as a lost create (automatic or operator
   resolution) still counts `max(accrued, hold)` toward the total cap for the 24 h late-resource watch, so a late group that reopens it can never
   push the total past `total_cap_cents`; its open-frame slot is still freed at once. This is an exception to the "frozen accrual of closed frames"
   rule of ADR 0041 section 7.3. A create proven absent after Salad's own clean rejection frees its hold at once, as before. Residuals: a clean-rejected frame
   that a late group reopens can exceed the total cap by up to its hold until the next pass's aggregate check fences every frame; a late group after
   the 24 h watch is not seen; and a watch-voided reopen can take the open count above the limit by one.
5. **What concurrency costs.** It trades runs, never spend: a longer pass can stop a healthy frame as a missed poll and one
   frame's failing reads can use up the pass's shared retry window; the reserve-time count does not bound open frames
   afterwards (a late group seen under a lost-create name reopens that frame), so the limit governs admission.
6. **Executor.** The executor runs paid routes one at a time; its new `max_concurrent_paid_routes` (integer 1 to 4, default 1)
   keeps up to that many live-check children in flight. At 1 the old code path is unchanged. Above 1 each child is waited on by its own
   thread that runs only the child; all checks, `mark_executing`, import, upload, termination confirmation, the guardian's
   final status and outcome stay on the pass thread. A further route starts only while the executor's cap, the guardian's own
   `max_open_billable_frames` (read from an in-flight route's guardian status) and a no-receiptless-end hold admit it;
   otherwise the attempt waits untouched (`route_deferred`), which is neither a failure nor a retry. A guardian refusal
   inside a started child still ends that attempt without a receipt (a route directory is never re-run) and blocks further
   concurrent starts until everything in flight has finished. The stage watcher follows one in-flight route.
7. **Operator guidance.** To allow two paid routes set the guardian's `max_open_billable_frames` and the executor's
   `max_concurrent_paid_routes` to 2 (guardian first), size `total_cap_cents` for two holds, and expect longer polls.

## Consequences

- A profile with no capacity in one country can fall back to another without a new approval, never spends more than the approved hold and
  never after a possible start, except after an allocation timeout proven to have had no instance (section 1, item 9), where the
  first hold stays reserved.
- Two paid routes can run at once, at the price of a longer unseen-run gap and runs that stop on a missed poll; the spend
  caps hold per frame and in aggregate.
- Defaults reproduce today's behaviour: no `fallback_profiles`, `max_concurrent_paid_routes` 1, `max_open_billable_frames` 1.
