# ADR 0153: a stuck campaign stops holding capacity it provably is not using

Status: Implemented (no schema change). Builds on ADR 0046 (not-started reconciliation),
ADR 0048 (grant lifetime and slots), ADR 0060 (capacity-aware stage placement) and the
idle-campaign release rule in `docs/campaigns.md`.

## Context

A campaign that is blocked (an `unknown` attempt, a late overrun) or paused
(`paused_delegation_lapsed`: its approver's authority lapsed) issues no new grant and drives
no attempt forward until a person acts. Three counters decide whether another campaign may
start, and each could stay taken by such a campaign:

1. The project concurrency limit (`campaign_placement.project_limits_allow`, enforced again by
   the store when a grant is issued) counts every `active` or `cancel_requested` grant of the
   project, expired or not. A stuck campaign's grant that expired before any worker took it
   stayed `active` forever: a blocked campaign's runner only observes `unknown` attempts, and
   a paused one observes nothing. With the default `max_concurrency` of 1 every new campaign
   waited at `reserved` (`campaign.stage_waiting_limits`) until a person cancelled or
   reconciled the stuck one.
2. The placement queue (`campaign_placement.placed_not_granted`), used only for stages with
   candidates (`alternate_providers`, or a due `retry.reroute_providers` reroute), counts
   attempts placed on a provider but not yet granted, whatever their campaign's status.
3. The provider's own slot count (`campaign_provider_worker.free_slots`) counts the worker's
   unresolved, unexpired grants. An expired grant already frees it (ADR 0048).

## Decision

1. **Expired, never-claimed grants are reconciled at zero.** For a campaign in `blocked` or
   `paused_delegation_lapsed`, the runner reconciles each grant of a `granted` or `admitted`,
   unsettled attempt when the grant is unresolved and expired, no worker ever took a lease on
   it, and no external operation was recorded for it. That is the evidence the
   expired-unattempted reconcile route and a running campaign's own `reconcile_unattempted`
   step already use. A lease is the only way a worker gets authority to run, and the store
   refuses a lease on an expired grant, so such a grant can never run. The accounting is the
   shared `reconcile_grant_row` (hold moved to settled at zero), the attempt ends
   `failed_transient`/`t1_grant_expired_unleased` settled at zero (as on a running
   campaign), and an `execution.expired_unattempted_grant_reconciled` event is written with
   `automatic: true`. All of it happens in one transaction, under the runner lease. It needs no
   live approval: it only reduces authority.
   Fail closed: an attempt whose grant is no longer unresolved (for example one a person
   already reconciled while the attempt still reads `admitted`) matches nothing, so this rule
   does not touch the attempt; it keeps its status and is left for a person or the evidence
   paths below. The same applies when the reservation ledger cannot cover the zero settlement.
2. **Not-started evidence also applies to a paused campaign.** The ADR 0046 auto-reconcile of
   an `unknown` attempt shown to have never started already ran for a blocked campaign; it now
   runs for `paused_delegation_lapsed` too, unchanged.
3. **A claimed grant is driven through the evidence paths only.** A grant that was leased (or
   has an operation) may have run. While its grant is live it is not touched: no observation
   is classified, nothing is resolved early by this rule, and it keeps its slot. Once the
   grant has expired, an `admitted` or `running` attempt on a worker-grant provider is
   classified from control-plane records exactly as the running flow's observation
   (`_do_observe`) would classify it on an expired grant: `unknown` (`observed_unknown`,
   `missing_receipt` or `missing_manifest`) when no complete confirmed exit was recorded, left
   as it is while the latest observation is `running`, while outputs are still arriving
   (ADR 0064) or when a run receipt shows a complete exit (the campaign collects it on resume).
   The same tick then applies the confirmed-exit resolution (ADR 0053) and the not-started
   auto-reconcile (ADR 0046) to the campaign's `unknown` attempts, with their own accounting
   (`release_grant`/`reconcile_grant_row`, the attempt settled, their events). Nothing is
   started, cancelled or re-run; no provider is called and no lease is issued (the store
   refuses a lease for a campaign that is not running). A route provider (Salad), an
   unregistered provider and a `granted` attempt are never classified; they are only reported
   (rule 7). The classification is written in one transaction under the runner lease, after
   re-reading the observation and the attempt's revision in it.
4. **The placement queue skips a stuck campaign's unrequested attempt.** An attempt stops
   counting while its campaign is `blocked`, `paused_delegation_lapsed` or terminal (the
   idle set of the release rule), the attempt is `reserved` with no grant, and no
   `issue_grant` intent exists for it. It is a computed rule (`FREED_SQL`), not a state
   change. `running`, `stopping` and `cancel_requested` campaigns keep counting: they are
   driven every tick and may issue the grant at any moment (a cancelling campaign cancels the
   attempt itself). An attempt with an `issue_grant` intent keeps counting, because a replay
   after a crash issues or completes that grant without a slot check.
5. **Resume never double-runs.** A resumed campaign's queued attempt counts again and its grant
   goes through the normal fresh `issue_grant` path, which checks the project limits and the
   provider's slot against live grants. An attempt closed by rule 1 is retried as a new
   attempt by the normal retry rules, through the same checks. The runner is a single lease
   holder and checks one attempt at a time, so a resumed attempt and a newer one cannot both
   pass on a full worker: the second waits (`campaign.stage_waiting_limits` or
   `campaign.stage_waiting_slot`).
6. **Every freed counter is recorded.** A `campaign.slot_freed` event carries `campaign_id`,
   `stage_key`, `attempt`, `attempt_id`, `provider_id`, `reason`, `campaign_status`,
   `trigger`, `released_by: system`, `automatic: true` and, for a grant, `grant_id`. Reasons:
   `expired_unclaimed_grant`, `not_started_evidence` and `confirmed_exit` (trigger
   `stuck_campaign_reconcile`, written in the reconcile's transaction, once, since the grant is
   then reconciled), and
   `placed_not_granted` (written in the transaction of the block, pause or terminal
   transition, or by the runner's bounded reconcile sweep, trigger `reconcile_sweep`, for a
   campaign that went idle earlier). The queue event is written once per attempt per spell
   out of `running`; a spell ends at the campaign's last `campaign.unblocked`, or
   `campaign.approved` / `campaign.cancel_requested` whose resulting status is not idle.
   Events carry the campaign's organization and project like every runner event.
7. **A claimed grant no evidence settles is held and reported.** An expired, unresolved grant
   of a `granted`, `admitted`, `running` or `unknown` unsettled attempt that was leased or has
   an operation, and that is still unresolved after rule 3, keeps its project slot, hold and
   exposure. It gets one `campaign.slot_held` event per attempt per spell (the same spell as
   rule 6): `stage_key`, `attempt`, `attempt_id`, `attempt_status`, `provider_id`, `grant_id`,
   `campaign_status`, `trigger: stuck_campaign_reconcile`, `automatic: true`, `reason`
   (`leased_grant_unconfirmed`, or `exited_awaiting_collection` when a confirmed exit has a
   run receipt), `evidence` (grant expiry, lease and live-lease counts, operation ids, the
   latest observed state, termination confirmation, contradiction state, whether a run
   receipt exists: ids, states and times only) and `action`, the step a person takes. All
   events of one campaign's pass are written in one transaction under the runner lease. A
   live grant gets no event: it is not stuck.

### What a stuck campaign keeps

- A live (unexpired) grant: its worker slot and its project slot. The run may still be going.
- A claimed, expired grant that no evidence settles (rule 3): its project slot, hold and open
  exposure (`exposure_open`), until a person reconciles it; it is reported once per spell
  (rule 7).
- Its held and settled cents, apart from the zero-settlement of a grant that never ran.
- Its state: status, stop reason, stages and attempts are not changed by rule 4; rule 1 ends
  only the never-run attempt, and rule 3 changes an attempt only as a running campaign's
  observation and evidence routes would.
- Its snapshot release while an attempt still needs it (the release rule in
  `docs/campaigns.md`); a second campaign on the same snapshot and worker proceeds once that
  release has expired or been freed.

## Consequences

- A blocked or paused campaign no longer needs a person to cancel it before a new campaign can
  use the project or the worker, when its grant provably never ran or it never asked for one.
- A resumed campaign may wait for capacity a newer campaign took meanwhile; it is never
  refused for that and never runs beside it past the limits.
- An attempt closed by rule 1 uses the stage's retry rules on resume, exactly as the same
  expiry does on a running campaign.

## Limitations

- A claimed grant whose expired run left no evidence (no confirmed exit, no launch error) still
  needs a person: control cannot tell an unreported run from no run. The `slot_held` event
  says so; nothing frees it automatically. Narrowed by ADR 0160: the worker now reports the
  exit, or the absence of the exact container it recorded, after it stopped driving the grant,
  and the Salad executor reports the guardian's confirmed exact-group stop.
- Rule 3 runs only while the campaign has an active approval, like the other evidence routes;
  a stuck campaign without one is reported by neither rule 3 nor rule 7.
- The not-started auto-reconcile now also runs for a campaign blocked by a late overrun
  (`budget_overrun`); it only settles at zero a zero-hold grant that provably never started,
  so a billable route's attempt never qualifies.
- Resolved: the reconcile sweep pages with a resumable cursor. `idle_holders` and
  `idle_slot_holders` return an id-ordered window of at most `SWEEP_LIMIT` campaigns after the
  cursor plus the next cursor (none once a window comes up short, so the sweep wraps around);
  the runner visits ids up to the nearer end of the two windows and advances the in-memory
  cursor only after the sweep's transaction commits. Idle campaigns that sort first and still
  need their release no longer keep later holders out: every holder is visited within about
  ceil(holders / SWEEP_LIMIT) + 1 ticks, with bounded work per tick. A restarted runner starts
  again from the first id.
- A resumed campaign whose attempt needs a new snapshot release while a newer campaign holds
  an unexpired release of the same snapshot on the same worker is refused (`step_refused`)
  by the release rule rather than queued; resume it once the newer campaign is done.
- The queue sweep and the per-spell check read the events table by project; cost grows with
  the project's event count.
