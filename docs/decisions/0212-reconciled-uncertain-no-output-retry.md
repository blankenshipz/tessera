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
  match, and an executor that went silent mid-route.
- The guardian final's observation time stands in for the reconcile time when ordering route
  readbacks. A readback taken between the reconcile and the executor's record of the final, and
  listing a live instance, is ignored.
