# 0212 — A provider-reconciled, output-less Salad attempt is retried as infrastructure

## Status

Accepted. A sibling of [ADR 0179](0179-precustody-reallocation-retry.md) and
[ADR 0187](0187-guardian-observed-precustody-reallocation.md); builds on the provider-confirmed
close settlement of ADR 0207. No schema change and no migration.

## Context

A claimed Salad create can start custody (inputs served, an output requested) and then lose its
allocation. The guardian cannot confirm that a node-local writer died after a move, so it never
seals custody. Instead, the safety service closes the frame with its own provider-confirmed
reconcile (`provider_confirmed_exact_group_stopped`): a fresh exact read of the group, stopped
with zero instances. Its final reads `reconciled` with `allocation_uncertain` set.

If the route's own provider receipts never confirmed termination, control's verify step left the
attempt `unknown` / `observed_unknown` ("route termination is not confirmed by provider
receipts"). That blocked the whole campaign. The only way out was the human grant reconcile,
which needs a person to choose a settlement and then fails the attempt permanently, so the stage
gets no retry. Nothing of the run could ever be accepted, and its spend had ended by the
provider's own read. Only the bookkeeping was left, and a person had to do it.

## Decision

1. **Classification.** In the verify step, an attempt whose route never confirmed termination
   ends `failed` with the new class `infra_salad_reconciled_no_output` (receipt reason
   `provider_reconciled_no_output`) only when **all** of these hold. Otherwise it stays
   `unknown` exactly as before (fail closed):
   - the executor's recorded guardian final is `reconciled` by the safety service's
     provider-confirmed close, never an operator attestation, with `allocation_uncertain` set;
   - that final is clean: no orphan, no pending escalation, no budget fence, no unknown rate and
     no billed cost recorded;
   - it carries a finite cost estimate, and that estimate rounded up (at least 1 cent) fits the
     attempt's hold;
   - no output was retained, sealed or collected: there is no retained-before-move marker, no
     manifest or uploaded artifact under the grant, and the live receipt neither passed nor names
     an output or a retained output at cancel;
   - the route's latest provider readback at or after the final's observation does not list a
     live or starting instance or an allocation. Older readbacks are superseded by the later
     provider-confirmed close, and a later clean readback supersedes an earlier live one. A
     readback whose time cannot be read counts as current and is never superseded.

   The evidence digest binds the route's evidence and that guardian final. Outputs are never
   accepted.
2. **Retry.** The class is infrastructure. It spends one `retry.infra_retries`, like the other
   transient Salad placement classes. With none left, the attempt fails permanently under the
   same class (after the class/profile fallback has had its say). It never uses a scientific
   retry.
3. **Settlement.** The attempt settles at the guardian's recorded estimate, rounded up to the
   next cent, never 0 and never supplied by a caller. That is the same provider-confirmed close
   path as ADR 0207: control's own read of the guardian record must confirm the close, and the
   settlement is the provisional `guardian_bounded_hold` (`settlement_final` false on the
   receipt), so a later recorded charge confirms or raises it. It counts against the campaign
   cap and the project holds like any settlement.
4. **Grant reconcile.** If a worker reported a confirmed exit, the ordinary `reconcile_grant`
   step settles the grant. Otherwise the grant route's exit-observation check cannot pass, so
   the Salad driver reconciles the grant itself. It uses the shared reconcile accounting
   (ADR 0048) and does the following in one transaction: it re-checks the rule above, marks the
   attempt `reconciled`, records its settlement, and records one
   `execution.reconciled_no_output_grant_reconciled` event. That event carries
   `termination_evidence: guardian_provider_confirmed_close` and `settlement_is_estimate: true`.
   If the rule no longer holds at that point, the attempt goes back to `unknown` and nothing is
   reconciled. Its waiting reason stays the one the blocked re-read keys on (item 5), so the
   refusal is re-checked on the next blocked pass.
5. **Already-blocked campaigns.** The blocked campaign's evidence pass re-reads each attempt left
   `unknown` for this reason. When the rule now holds, the attempt leaves `unknown` with the
   failed verdict in one transaction: its stage leaves `blocked_unknown` and one
   `campaign.salad_reconciled_no_output` event is recorded. The blocked drive then unblocks the
   campaign as it does for any campaign with no unknown attempt left, so no operator action is
   needed after deploy. A human-reconciled grant or a settled attempt is never touched. The human
   grant reconcile (`POST /execution-grants/{id}/reconcile`) is unchanged for every attempt this
   rule leaves `unknown`.
6. **Re-read, never locked out.** The waiting reason the re-read keys on ("route termination is
   not confirmed by provider receipts") is never rewritten. Each blocked pass re-reads the
   attempt, so a temporary refusal (`provider_lists_live_instance`, `route_state_missing`, an
   unreadable guardian) resolves once its evidence clears. A permanent-shaped one (an output
   retained, sealed or collected) is re-checked too, and stays held. Why the attempt is held is
   a closed code on `campaign.salad_reconciled_no_output_held`, recorded only when that code
   changes, never once per pass. A refusal at the reconcile step is recorded the same way,
   prefixed `reconcile_`. A blocked campaign has no runner breaker, so an unexpected error in
   this re-read is caught, recorded once per error class
   (`campaign.salad_reconciled_no_output_error`), and the attempt stays held. Control flow, a
   refused step and database contention still propagate.
7. **Refreshing a stale guardian final.** The executor records the guardian final once, when it
   records the outcome, and the guardian may close the frame seconds later. While the stored
   copy is missing or not terminal (not `stopped` or `reconciled`), the blocked re-read reads the
   guardian's status itself. It uses control's own authenticated, read-only guardian client
   (`read_final`, the same `status` call as the frame-record read, projected exactly as the
   executor projects it), at most once a minute per attempt. Only a terminal fresh final replaces
   the stored copy, with one `campaign.salad_guardian_final_refreshed` event in the same
   transaction. A stored terminal final is never overwritten. An unreachable or unprojectable
   guardian changes nothing and holds (`guardian_final_unreadable`); a control plane without
   that reader holds as `guardian_final_unrefreshable`.

## Consequences

### Limits (still human-only)

- A route-flagged reallocation whose provider never confirmed the stop (ADR 0149's
  `termination_unconfirmed`) is not covered. It still waits for the provider's own read or a
  person.
- Other `unknown` verify outcomes are not re-read here: a route that confirmed termination only
  later, a missing confirmed exit observation on a confirmed route, a manifest that does not
  match, and an executor that went silent mid-route without reporting the route lost (the
  addendum below covers the reported case).
- The guardian final's observation time stands in for the reconcile time when ordering route
  readbacks. A readback taken between the reconcile and the executor's record of the final, and
  listing a live instance, is ignored.

## Addendum: a lost route resolves without a person; executor refusals and breaker

### Context

The executor finishes a route only from the live check's receipt on its disk. After a crash, an
out-of-memory kill, a reboot or a timeout of the executor mid-route, no receipt is ever written.
The runner waited one route window plus 600 s (up to two windows plus about 6,900 s), then left the
attempt `unknown` for the silent reason (`executor silent past the route window`) and blocked the
campaign. Rule 1 could not help: there is no live receipt and usually no guardian final. Meanwhile
the guardian keeps running and stops the group by itself, so its own final says the spend ended.

Separately, the executor only wrote a note when it refused an attempt (its stage names a profile it
does not serve, or its plan budget is above the profile's cap). Control then waited out the grant,
twice, and blocked. One handler caught every per-attempt error with no bound, and a failed
`control.work()` was swallowed, so the health file never showed errors.

### Decision

8. **Lost-route report.** When the executor resumes an `executing` attempt (no child of its own runs
   it) and finds no receipt in the route's directory, it reads the guardian's status for the route
   (read-only, the same projection as the outcome's guardian final). It reports only a terminal
   final (`stopped`, or `reconciled`) that reads terminal on two reads at least 120 s apart, with no
   receipt on disk at the second read, so a live check still writing its receipt is never reported
   lost. The report goes once per route: a private marker in the route directory fixes the final, so
   a retry or a restart replays the same request. Failed reports are bounded per process. A
   late-importable silent attempt with no receipt is reported the same way. No route directory means
   the executor never launched the route, and it says nothing.
   `record_salad_route_lost` accepts the report only for an `executing` attempt with no recorded
   live receipt, or for the late-importable silent `unknown`. It stores the final, never over a stored
   terminal one, and records one `campaign.salad_route_lost_no_receipt` event. Nothing about effects
   or spend is asserted.
9. **The rule for a lost route.** The attempt ends `failed` with the new class
   `infra_salad_executor_lost` (receipt reason `live_check_lost_no_receipt`) only when **all** of
   these hold. Otherwise today's path stands (fail closed): the wait, then `unknown` for a person.
   - the executor reported the route lost, and no live receipt was ever recorded;
   - the recorded guardian final is `stopped`, or `reconciled` by the safety service's own
     provider-confirmed close. An operator attestation never counts;
   - that final is clean: no orphan, escalation, budget fence or unknown rate, and no billed cost.
     `allocation_uncertain` is allowed only where the settlement rule already tolerates it (a
     provider-confirmed close, or a stop on an allocation change);
   - it carries a finite estimate whose next whole cent (at least 1) fits the hold;
   - no output was retained, sealed or collected: no retained-before-move marker, and no manifest or
     uploaded artifact under the grant;
   - no provider readback recorded on the route since that final lists a live instance (rule 1's
     ordering).

   The runner applies the rule on the first pass after the report, without waiting out a window. It
   also applies it on each blocked pass for an attempt already left `unknown` as silent. In one
   transaction the attempt leaves its phase with the failed verdict, a blocked stage leaves
   `blocked_unknown`, and one `campaign.salad_route_lost_resolved` event is recorded. Why an attempt
   is still held is a closed code on `campaign.salad_route_lost_held`, recorded only when that code
   changes. The guardian's records of the run's own outcome still decide the class first: an
   allocation timeout (`infra_allocation_timeout`), a start failure (`start_failure_suspected`, or
   the pre-custody reallocation when no exit was read), a workload non-zero exit
   (`salad_container_exited_nonzero`) and a provisioning overrun (`infra_salad_provisioning_overrun`)
   keep their own classes. A route-deadline stop that is not a provisioning overrun does not: with
   the executor dead nothing collected the run, so the guardian's deadline stop is the likely shape
   of that death, and it ends as the retryable `infra_salad_executor_lost` (its detail names the
   deadline), never `failed_permanent / route_deadline_reached`. `infra_salad_executor_lost` covers
   that and every other final. The `failure_class` on `campaign.salad_route_lost_resolved` is read
   from the same provider classification the runner records, against the same guardian final (the
   resolve refuses if the final changed in between), so the event always names the class the
   attempt ends with. Outputs are never accepted: with no receipt, nothing of the run can be
   imported.
10. **Retry and settlement.** As in rules 2 to 4: one `retry.infra_retries` per attempt; settlement
   at the guardian's estimate rounded up, at least 1 cent and within the hold (provisional
   `guardian_bounded_hold`), which control's own guardian record read must confirm. Without a
   worker-confirmed exit, the driver reconciles the grant under this rule in the same transaction
   (`termination_evidence: guardian_confirmed_stop`). If the rule no longer holds there, the attempt
   goes back to `unknown` for the silent reason, so the blocked re-read checks it again.
11. **Typed refusals.** The executor reports a refusal with `record_salad_refusal` and a closed code
   (`different_profile`, `plan_budget_above_profile`, `step_breaker_open`), once per attempt and code
   per process. Control accepts it only before any route has started. The waiting reason becomes
   `executor_refused:<code>`, and the runner unwinds the attempt on its next pass:
   - for an unleased attempt, the grant is given back at zero, with the same evidence as a paused
     campaign's release (no lease, no operation);
   - for a leased one, the packaged unwind runs (cancel the route, revoke, the no-claim proof).

   The attempt ends `failed_transient` / `t1_salad_unclaimed` and the stage's own retry budget
   decides. Nothing waits out the grant. This assumes one executor per worker principal, which the
   executor's `profiles` list already provides.
12. **Step breaker.** Each attempt's consecutive per-step errors are persisted in the work directory
   (`executor-breaker.json`), so a crash loop keeps counting. After 5 consecutive failures of a
   pre-launch step (grant poll, lease, package), the executor stops trying that attempt and reports
   `step_breaker_open`. A success, or the attempt leaving control's list, ends the streak. Errors
   after launch (renewal, finish) are counted but never refused: the lease must stay held for the
   runner's own release.
13. **Health.** The service health file gains a `service` object with the executor's counters:
   `work()` failures (total, consecutive, last class), per-item errors (total, last pass, last
   class), refusals reported and failed, lost routes reported, attempts with an open breaker and
   the longest failure streak.

### Limits

- A lost route whose guardian never reaches a terminal final, or whose final fails the rule, still
  ends `unknown` for a person after the wait.
- A route directory removed by the operator makes the executor silent for that route.
- With two executors sharing one worker principal and serving different profiles, a
  `different_profile` refusal would unwind the other executor's attempt. That deployment is not
  supported.

