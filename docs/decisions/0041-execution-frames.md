# 0041 — Execution frames

## Status

Partially implemented (2026-11-20). The resolved questions below are decided. Implemented
for campaign stage attempts: the frame state machine and its guard triggers (schema v66,
`execution_frames`, `execution_frame_events`, `execution_frame_settlements`,
`experiment_budget_releases`), `settle_frame` as the single framed settlement path with the
`provider_absent_zero` basis and the zero-final experiment release (§3, Q2, Q3), and one frame
per runner attempt that closes in the settlement transaction, so a finished or errored job
releases its hold at once. A settlement whose provider is not registered now fails closed and
is recorded. Not implemented yet: the provider create/lookup/stop/observe surface and
adoption of a found create (§4, Q1), crash-resume driving of open frames (§6), the guardian
per-frame record, verdict read and `max_open_billable_frames` fence (§7, Q4), parent
campaign frame rows (§5), and frames for standalone routes, local Docker and Kubernetes.
Follow-up 6 is done: a billable frame that is `lost`, `failed` or outcome-ambiguous is never
settled on a verdict-based basis (`guardian_bounded_hold`, `provider_billed`) until control has
recorded a guardian verdict digest on the frame (`settle_frame` refuses with `VerdictRequired`;
the Salad driver records it from the guardian final it already reads). Human charge and
reconcile bases are unchanged. The per-frame guardian record and fence are still open. The original record follows. It generalizes the
create-rejection change (commit 0f05f46, since merged) and amends the settlement
contract of ADR 0038 (`0038-campaign-specs-and-platform-campaign-runner.md`, on the campaign runner track) and the
provider interface of ADR 0040 (`0040-multi-stage-multi-vendor-campaigns.md`, on the campaign runner track). The guardian
changes are spend-safety fixes under the exception in
[ADR 0033](0033-freeze-over-built-planes.md). Numbered 0041 because the campaign runner
track uses 0038 and 0040.

## Context

A budget reservation is released today when a wall-clock rule allows it, not when Tessera
knows the outcome. The Salad safety service (`salad_safety_service.py`) keeps one
`reservations` row per route. A new `reserve` is refused with
`single_operation_orphan_exposure` while any other row's status is not `stopped` or
`reconciled`. A row with no `provider_id` (the create never bound) becomes terminal only on
the lost-create path: route deadline + `LOST_CREATE_MARGIN_SECONDS` (900 s), a definite 404
on the exact reserved name, and a complete project group list without it. That margin is
measured from the route deadline because the guardian does not record when a create could
last have been sent.

- **Known outcomes wait anyway.** A finished run releases quickly only on the output path:
  `guardian.complete` stops the group, and the exact stopped readback marks it `stopped`. A
  workload that errors before retaining output is stopped only by a heartbeat lapse,
  executor cancel or deadline. The ADR 0038 campaign settlement (`guardian_bounded_hold`)
  then relies on a final guardian status that the executor reads and passes on as
  worker-attested input.
- **A clean rejection blocked all work.** Live, a create with non-whole-GiB storage (30000
  MiB) was refused with HTTP 400 in about 1.3 s. Nothing was created, but the row had no
  `provider_id`, so the orphan fence blocked every run for deadline + 900 s. This happened
  three times. Commit 0f05f46 classifies a fully read 400/422 ProblemDetails reply as
  `provider_rejected_request`, fences the reservation at once, and releases it after 15 s
  (the dispatch-chain bound) on the guardian's own absence reads, with an audited event.
  It also checks the create shape before any reservation exists.
- **A timeout is not a failure.** Live, Salad accepted a valid create more slowly than the
  2.0 s client deadline. The client classified it as `unknown`, the group was created
  anyway, and the guardian saw it later by its reserved name. Any rule that read "create
  timed out" as "nothing exists" would have released a live resource.
- **Each layer keeps its own ledger.** Today, the guardian's reservations, the remote route
  effects (`remote_execution.py`, intent-first `begin`/`record`), the experiment ledger
  (`experiment_budgets.py`, holds that only human charges reduce), worker leases with run
  receipts (`execution_run_receipts.py`), and the campaign runner's
  `campaign_stage_attempts` with `_settle` (on the campaign track) each record part of one
  attempt's life. No single record says what state that attempt is in.
- **Other providers.** Local Docker (`local_docker_*`) and Kubernetes
  (`deterministic_job_name`) already use deterministic names. They have no budget hold,
  but they need the same crash resume and the same outcome classification.

## Decision

Bind a reservation's life to an explicit, durable, provider-agnostic **execution frame**,
one per stage attempt. The frame owns the budget reservation, the provider handles and one
state machine. A known outcome releases at once. Only an ambiguous one holds, and it is
resolved by looking it up actively. Timeouts remain the last-resort backstop and never the
normal release path.

### 1. Frame model

States: `requested → validated → reserved → creating → running → {succeeded | failed |
rejected | cancelled | lost} → settling → closed`. `ambiguous` is not a state: it is a flag,
with a reason, on a `creating`, `running` or `lost` frame that marks the frame as still
holding.

| From | To | Trigger | Evidence recorded |
| --- | --- | --- | --- |
| requested | validated | `validate_shape` and the authority/data-class checks pass | shape digest |
| requested | rejected | validation fails | refusal code; nothing reserved |
| validated | reserved | control hold and guardian reserve, all caps pass | hold, guardian reservation id |
| reserved | creating | create intent committed (before the provider call) | intent id, send-not-before |
| reserved | cancelled | cancel, or expiry before any create intent | none; release at once |
| creating | running | provider handle bound by create reply or lookup | handle, identity match |
| creating | rejected | definitive provider refusal (clean 4xx) | bounded rejection summary |
| creating | lost | no answer, then lookup proves absence (see rules) | absence reads |
| running | succeeded / failed | provider-observed terminal, stop confirmed | terminal readback, receipt |
| running | cancelled | human/runner cancel, stop confirmed | stop readback |
| running | lost | handle vanished or allocation uncertain, not provably stopped | readbacks; stays ambiguous |
| terminal | settling | outcome known and the provider can no longer bill | guardian verdict (billable) |
| settling | closed | settlement committed | settlement id, frame receipt |

**Known-terminal outcomes** go to `settling` without any timer:
- `succeeded` or `failed`, once the provider shows the exact resource stopped (Salad: the
  guardian's `exact_terminal_and_sealed`; Docker and Kubernetes: a confirmed exited or
  complete readback and the resource removed). A workload failure report or an observed
  exit triggers an immediate stop. It no longer waits for a heartbeat lapse.
- `rejected`, by validation (nothing reserved) or by a definitive create refusal.
- `cancelled`, once the stop is confirmed or no create intent was ever committed.

**Ambiguous outcomes** keep their hold. Each provider capability has its own resolution:
- *Create sent, no definitive reply* (timeout, transport error, 5xx, 409, 429): look up by
  idempotency key. If found with an exact identity match, bind it and continue (`running`)
  or, if authority or deadline has lapsed, stop it and settle as `cancelled`. If absent,
  repeat the lookup until `create_admitted_at + dispatch_bound + materialization_window`
  (both declared by the provider). Absence after that closes the frame as `lost`. A timeout
  is never classified as `failed` or `rejected`.
- *Clean 4xx refusal:* wait the provider's `dispatch_bound` (Salad: 15 s) after the
  report, so any create admitted just before it has landed. Then confirm absence by lookup
  and settle at once. If the lookup cannot confirm absence, treat it as no reply.
- *Provider cannot answer* (lookup unavailable, list incomplete, credential refused): only
  then does the backstop apply: deadline + margin (Salad: 900 s) and an absence proof, as in
  today's lost-create rule. Without an absence proof the frame never closes on its own, and
  an operator reconcile is required.
- *Running and unobservable:* stop by handle, retrying. The frame stays `running`
  (ambiguous) until a stop readback succeeds, or goes to `lost` when the allocation is
  uncertain. A `lost` frame with a handle settles on an exact stopped readback by the
  provider (the Salad service closes it automatically as
  `provider_confirmed_exact_group_stopped`; custody stays unsealed) or by the existing
  operator reconcile, which requires the same readback.

### 2. Data model

**Control owns the frame. The guardian keeps its own fence.** The authoritative frame
lives in the control database. Settlement must commit in the same transaction as the
campaign ledger, the experiment ledger, events and idempotency keys, with the tenant check
at data access (AGENTS.md). The guardian stays an independent safety service: its
`reservations` table becomes a per-frame record keyed by the same `frame_id`. It keeps its
own stop authority, accrual, total cap and late-resource watch. It never reads control's
frame state to decide its fence. For a billable provider, control may not move a frame from
a terminal state to `settling` without a **guardian verdict**, which control reads itself
over the authenticated status channel. This replaces the worker-attested final status in
ADR 0038 amendment A.

`execution_frames` (control):
- Identity, write-once (triggers refuse update): `frame_id`, `parent_frame_id`, `kind`
  (`campaign`, `stage`, `late_resource`), `org_id`, `project_id`, `provider_id`, provider
  instance, `idempotency_key`, `shape_digest`, `hold_cents`, the budget references (campaign
  attempt, admitted attempt, grant, `operation_id`, guardian reservation) and `created_at`.
- Set once when first known, then immutable: `provider_handle`, `create_admitted_at`,
  `outcome`, `outcome_evidence_digest`, `guardian_verdict_digest`, `settlement_id` and
  `closed_at`.
- Mutable by compare-and-swap on `revision` only: `state`, `ambiguous_reason`,
  `next_resolve_at` and `attempts`. A trigger checks every `(from, to)` pair against the
  table above, so a `closed` row is final.

`frame_id` is deterministic: `canonical_digest([parent_frame_id, stage_key, attempt])` for
campaign stages, and the existing reservation digest (`[route_id, route_digest]`) for a
standalone route. The `idempotency_key` is derived from it: the Salad group name, the
Docker container name and labels, or the Kubernetes Job name. Where an approved spec fixes
the name (the Salad `group` today), the frame binds that name, and both control and
guardian refuse a second non-closed frame with the same (provider project, name).

`execution_frame_events` is append-only: sequence, frame, from, to, trigger, evidence
digest, actor (`runner`, `guardian_verdict`, `human:<principal>`) and time. It also writes
the ordinary `events` row (`frame.state_changed`, `frame.ambiguous`,
`frame.resolved_found`, `frame.resolved_absent`, `frame.settled`, `frame.closed`,
`frame.late_resource_observed`). Closing writes an immutable **frame receipt**: identity,
parent, provider, key, handle, outcome, evidence digests (rejection summary, absence reads,
terminal readback, run or live receipt, guardian verdict), hold, settled cents, basis,
`final`, and the guardian estimate labelled `estimate_not_invoice`. Its canonical digest is
stored on the settlement.

### 3. One settlement path

There is one function, `settle_frame(connection, frame_id, settlement)`. It runs in the
caller's transaction and is the only code that moves money for an attempt:
1. Under the runner lease, it requires the frame to be in `settling` and the settlement to
   name a basis from the closed core enum (ADR 0040 §2).
2. It inserts `execution_frame_settlements` (primary key `frame_id`; cents, basis, `final`,
   request digest, receipt digest).
3. It moves the frame's hold to settled in its parent's ledger: `campaign_stage_attempts`
   and `campaigns` for a stage frame (the body of today's `CampaignRunner._settle`), or the
   guardian-facing route record for a standalone run.
4. It adjusts the experiment reservation for the admitted attempt (below).
5. It sets the frame `closed` and writes the receipt and events.

**Idempotent double-settle.** A second call with the same request digest returns the
stored settlement and writes nothing. A different digest raises `Conflict`, as `record_charge`
does for a reused `delivery_id`. The campaign CAS on `settled_cents IS NULL` stays as a
second guard. A runner, executor or recovery pass may therefore call it any number of times.

**Bases.** The existing bases (`local_zero`, `unclaimed_zero`, `human_charge`,
`human_reconcile`, `guardian_bounded_hold`; `provider_billed` reserved) are kept. One is
added as a reviewed core change under ADR 0040 §2: `provider_absent_zero`, for a frame
proven to have created nothing billable (`rejected` after absence, `lost` after absence,
`cancelled` before any create intent). It settles 0 and is `final`.

**Experiment ledger.** For a zero `final` basis with evidence, the experiment
reservation's `held_cents` goes to 0 through an immutable
`experiment_budget_releases` row (one per admitted attempt, carrying the basis and evidence
digest). It is not a charge. Every other basis keeps today's rule: the hold stays until a
human charge, and charges above the hold are late charges. Late charges attach to the
closed frame's settlement and never reopen the frame.

### 4. Provider interface

ADR 0040's `ComputeProvider` gains a frame-level execution surface, implemented by each
provider's executor (bytes and credentials never pass through the runner):

- `validate_shape(spec) -> shape_digest`: pure. Runs in `validated`, before any hold
  (Salad: the `salad_resources` limits, including storage in whole GiB; quota where it can
  be read).
- `create(frame, idempotency_key) -> Created(handle) | Rejected(summary) | Unknown(reason)`:
  only `Rejected` is definitive.
- `lookup(idempotency_key) -> Found(handle, identity) | Absent(proof) | Unanswerable`.
- `stop(handle) -> Stopped | Pending | Unknown`.
- `observe(handle) -> Running | Terminal(outcome, evidence) | Unknown`.
- A capability descriptor: `billable`, `lookup` (`strong`, `eventual` or `none`),
  `create_inert` (a created resource does not run or bill until a separate start),
  `dispatch_bound_seconds`, `materialization_window_seconds` and `backstop_margin_seconds`.

| Capability | Salad | Local Docker | Kubernetes |
| --- | --- | --- | --- |
| key | approved group name | `tessera-local-<nonce>` plus labels | `deterministic_job_name(operation_id)` |
| create | `create_stopped`; 400/422 ProblemDetails is `Rejected` | `docker create`; daemon error classified | Job POST; 422 `Invalid` is `Rejected` |
| lookup | exact-name GET plus complete project list (`eventual`) | inspect by name and labels (`strong`) | GET Job by name (`strong`) |
| inert create | yes (`autostart_policy: false`; start needs guardian `ready`) | yes (create, then start) | no (create starts) |
| stop / terminal | guardian stop; exact stopped readback | stop, inspect exited, remove | delete with propagation; Job conditions |
| windows | dispatch 15 s, materialization 900 s, backstop deadline + 900 s | dispatch ≈ call bound, materialization 0 | API request timeout |
| settle | `guardian_bounded_hold`, `provider_absent_zero`, human | `local_zero` | `local_zero` |

Kubernetes stays an optional lab profile under ADR 0033. It adopts frames only when that
plane is unfrozen or as a bug fix.

### 5. Nesting

A campaign is a `campaign` frame whose reservation is the campaign's `budget_cap_cents`.
Each stage attempt is a child `stage` frame. A child reserves from its parent within the
same transaction, under the ADR 0038 §3 rule (`held + settled + hold <= cap`). When a child
closes, its hold moves to settled in the parent at once, and the remainder is free for the
next stage. A parent cannot reach `settling` while any child is not `closed`. Cancelling a
parent cancels every non-terminal child. An ambiguous child keeps the parent open but blocks
only its own hold, not its siblings', subject to the guardian fence (§7). The `stage →
attempt → frame` mapping is one-to-one with `campaign_stage_attempts`, so campaign retries
(T1/T2) create new child frames and never reuse a closed one.

### 6. Crash recovery and resume

Frame state is durable before every external effect, as with intent-first writes. On start,
the frame driver (inside the campaign runner tick, and in the Salad executor and
development worker loops) selects every frame that is not `closed` and drives it:
- `requested` or `validated`: re-validate, or go to `cancelled` (nothing held).
- `reserved` with no create intent: go to `cancelled` with `provider_absent_zero`, and
  release.
- `creating`: the create intent may or may not have reached the provider, so the frame is
  ambiguous. Look up by key and apply §1. Create is never re-sent: `remote_execution.begin`
  already refuses a second create.
- `running`: observe, and stop if authority, deadline or heartbeat has lapsed.
- Terminal: re-request the guardian verdict and continue.
- `settling`: call `settle_frame` again, which is idempotent.

The guardian keeps its own restart rule. On restart it sets recovery on every non-closed
reservation, stops bound resources and resolves unbound ones by name. Neither side needs the
other to be up to make progress toward a safe state. Control only waits for the verdict
before it releases money.

### 7. Safety invariants

1. **Never release while the provider might still bill.** A billable frame settles only
   with a guardian verdict of `stopped`, `reconciled` or proven absent (a resource found
   stopped and never started counts as stopped). A Salad create with no reply is never
   absent until its materialization window has passed.
2. **The fence becomes per-frame.** `single_operation_orphan_exposure` is replaced by two
   checks: the total cap, and `max_open_billable_frames`, a guardian setting (default 1,
   which keeps today's one-GPU behaviour). A frame counts toward that limit only while it
   might bill. Known-terminal, rejected-absent and closed frames never count, so a clean
   rejection unblocks the next run in about 15 s instead of deadline + 900 s.
3. **Total-cap accounting is unchanged in substance.** Committed = the frozen accrual of
   closed frames + `max(accrued, hold)` of every other frame, checked with the insert in one
   `BEGIN IMMEDIATE` transaction.
4. **Lost-create rules.** The margin is measured from the guardian-recorded
   `create_admitted_at` (the last `ready` admission for that frame) and no longer from the
   route deadline. The two absence reads (exact-name 404 and complete list) are unchanged.
   The 24 h late-resource watch is kept. A late sighting never reopens a closed frame:
   it opens a `late_resource` child frame, which holds `max(accrued, hold)` against the
   total cap, stops the resource and settles under its own basis.
5. Create and start are never retried by a frame. Stop-only authority survives lease and
   credential expiry, as it does today.

### 8. Migration plan

Each stage is a separate reviewable change with focused tests. The full suite runs once
before merge.

1. **Create rejection and validation.** Land the create-rejection change (400/422
   classification, audited early release after 15 s on proven absence, and shape
   pre-validation) and name its states with the frame vocabulary in events and docs. Add
   the `execution_frames` and `execution_frame_events` tables in shadow mode: they are
   written alongside the Salad route effects and guardian reservations and are read by
   nothing that releases money. Add the timeout-is-ambiguous test.
2. **Guardian per-frame record.** Record `create_admitted_at`; measure the lost-create
   margin from it; stop at once on a workload failure report; add a frame verdict to
   `status`; replace the orphan fence with the per-frame checks (§7.2).
3. **One settlement path.** This stage starts after the campaign track's schema (v65) is on
   `main`. Move the body of `CampaignRunner._settle` into `settle_frame`, and make `_settle`
   a thin caller, in one change reviewed with the campaign track's owners. Add
   `provider_absent_zero` (an ADR 0040 core-enum amendment) and `experiment_budget_releases`.
   Make control read the guardian verdict itself. Until this stage, `_settle` remains the
   only campaign settlement, and frames do not move money.
4. **Nesting.** Add campaign frames as parents of stage frames, and release each child's
   hold on close.
5. **Local Docker** frames (`local_zero`) in the development worker. Kubernetes follows
   when ADR 0033 allows.

To coordinate with the campaign track, it keeps ownership of `campaign_*` tables, provider
plugins and the settlement bases. This track adds no second settle routine, and stage 3 is
the only point where the two touch the same code.

### 9. Test plan

All default tests use fakes and synthetic clocks. Live checks are separate opt-in harnesses.
- **Finished:** output retained, stop and exact readback lead to `succeeded`, verdict,
  settlement and `closed`. The next reserve succeeds with no time advance.
- **Errored:** a workload failure report leads to an immediate stop and to `failed`, then
  `closed`. No heartbeat or deadline wait.
- **4xx reject:** a fake 400/422 ProblemDetails reply leads to `rejected`, absence reads at
  +15 s and `provider_absent_zero`. The fence reopens. With a failed or incomplete list read
  the frame stays held, and the deadline + 900 s backstop applies. A non-ProblemDetails 400
  is `Unknown`, never `Rejected`.
- **Timeout-ambiguous:** a create that times out while the fake materializes the group 3 s
  later is looked up, found and bound, never failed. A create that times out and never
  materializes is held until the materialization window, then closes as `lost`. An
  unanswerable lookup falls back to the backstop.
- **Crash mid-create:** kill between the committed create intent and the reply, then
  restart both sides. The create is not re-sent, the frame resolves by lookup, and there
  are no duplicate events or settlements.
- **Double-settle:** the same settlement twice is a no-op. A different digest raises
  `Conflict`. Campaign and experiment ledgers change once.
- Also: transition-trigger refusals; nested release; a late resource after close opens a
  child frame; total-cap races under `BEGIN IMMEDIATE`.

## Consequences

Known outcomes release in seconds, with an audited receipt. Only real ambiguity holds money,
and it is resolved by lookup. Campaigns, standalone routes and local runs share one state
machine and one settlement routine. The guardian keeps an independent fence and becomes the
source of the "cannot bill" verdict that control reads itself. Costs: a new control table
and migration, a guardian change under the ADR 0033 exception, and one new settlement basis.

## Resolved questions (2026-11-19)

1. **A create that timed out and is later found is adopted only inside the original
   deadline.** The lookup result stands in for the create reply: the frame binds the handle
   and, if authority and the original deadline still hold, makes its first and only start
   through the guardian `ready` admission. Identity match means the exact provider project,
   the exact name and read-back shape fields that match `shape_digest`. If authority or the
   deadline has lapsed, the frame stops the resource and settles it (see 2). If identity does
   not match, the frame never starts the resource; it stops it and stays `lost` (ambiguous)
   until an exact stopped provider readback or an operator reconcile settles it. The deadline is never extended. A retry is a new attempt with
   a new child frame. This is not a retry under §7.5: the create is not re-sent and the start
   was never sent. Kubernetes, where create also starts, observes a found Job inside the
   deadline and deletes it otherwise.

2. **A frame found stopped and never started does not settle at `provider_absent_zero`.**
   That basis needs proven absence: absence reads, or `cancelled` with no create intent. A
   billable frame whose resource existed but never started settles under
   `guardian_bounded_hold` at its hold, as a non-final settlement. The guardian accrual is
   recorded on the receipt as `estimate_not_invoice`. Under §7.1 the frame is stopped, so it
   stops counting toward the fence at once. Only the money stays held until a human charge or
   reconcile. `settle_frame` refuses `provider_absent_zero` without the required evidence.

3. **The experiment hold is released only on zero `final` bases with evidence.** The bases
   are `provider_absent_zero`, `local_zero` and `unclaimed_zero`. The release is one
   `experiment_budget_releases` row per admitted attempt. It is written only by
   `settle_frame`, in the settlement transaction, and is write-once (a trigger refuses update
   and delete). It is not a charge and does not edit the hold. Every other basis keeps the
   existing rule: the hold stays until a human charge. If a resource is seen after a release,
   the `late_resource` child frame (§7.4) opens its own hold and its charges are late
   charges. The release row is never reversed.

4. **Ambiguous creates always count, and the limit stays at 1.**
   a. A frame counts toward `max_open_billable_frames` until its guardian verdict is
      `stopped`, `reconciled` or proven absent, whether or not the provider's create is
      inert. An inert create in an unknown state still counts. The inert-create exposure
      release is withdrawn.
   b. `max_open_billable_frames` defaults to 1, and the guardian refuses any other value at
      startup. Concurrent billable frames need a later record that covers total-cap races,
      partial stops and accrual interleaving.

Effect on §8: stage 6 is removed. Effect on §9: add tests for adoption inside the deadline
(started exactly once), adoption after the deadline (stopped, never started, settled at the
hold), an identity mismatch (never started, held), found-stopped-never-started (settled at
the hold, fence reopened with no time advance), a single experiment release under a
double-settle, no release on non-zero or non-final bases, a late resource after a release
(new hold), an ambiguous create blocking the next reserve until resolved, and a startup
refusal for a limit other than 1.
