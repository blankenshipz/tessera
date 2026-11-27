# 0038 — Campaign specs and the platform campaign runner

## Status

Accepted (2026-11-19). Implemented by the campaign runner change with default
tests (fakes, no live provider): the campaign runner, the Salad stage driver and its
operator profiles. Data classes follow [ADR 0039](0039-data-class-guard.md), which is
the one source of truth. Schema migration v63 → v64 (v62 is ADR 0039's, v63 its
amendment 1's code approvals; [ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) adds
v64 → v65).

## Context

Today a client session drives every experiment step. One local CPU run takes
about fourteen HTTP calls: task, plan, approve, limits, experiment, grant,
admission, a `tessera-worker development-run --grant-id` process, disposition,
custody release, staging, release, resolve, measurements and reconcile. Plan
approval, grant issue, snapshot release, staging and custody are human-only.
Program-specific drivers kept outside this repository hand off between stages through local
directories, and nothing records the stages as one unit.

Verified facts this design relies on:

- `Store._mutate` is the only write path. Each command is one
  `BEGIN IMMEDIATE` transaction that writes state, events and the idempotency row.
  Every human check is `actor["role"] == "human"` plus `require_decision_actor`,
  applied to the live principal inside that transaction.
- No server-side dispatcher exists. The worker is not a poller: it runs once for
  one `--grant-id`. `serve` only runs uvicorn.
- `ExecutionSpec` has one `input_snapshot_digest` and no environment. Release
  classification is only `public`, enforced by `ExecutionStore._authorize_snapshot`
  at grant, lease, input delivery and provider-ticket issue.
- Agent authority grants (`agent_authority.py`) bind an execution grant, task and
  plan digest that already exist. A campaign's later plans depend on outputs that
  do not exist yet, so those grants cannot express a campaign.

ADR 0033 holds. No frozen plane is extended. The design uses only the public
store interfaces of execution, experiments, custody, staging, releases and
lineage, plus the per-grant worker.

## Decision

### 1. Campaign spec: a typed, digest-pinned DAG of ordinary runs

`CampaignSpec` v1 is a strict pydantic model. A human approves
`spec_digest = canonical_digest(spec)`. The spec has `name`, `budget_cap_cents`
(0–5000, the experiment-budget bound), `currency` (`"USD"`), `deadline_seconds`
(at most 7 days), `worker_principal_id`, and 1–32 `stages`. Each stage is one
ordinary Tessera run: `key` (`^[a-z][a-z0-9_-]{0,31}$`); `backend`
(`docker-local-cpu-v1` or `salad-gpu-v1`); `image` pinned `name@sha256:<64 hex>`;
`argv`, `output_paths`, `max_output_bytes`, `max_duration_seconds` and optional
`egress` with the `ExecutionSpec` bounds; `max_exposure_cents` (the stage hold);
`grant_ttl_seconds` (60–43200, default 900; at least `max_duration_seconds` plus a 300 s margin, [ADR 0048](0048-grant-lifetime-and-slot-release.md)); `retry` (`max_attempts` 1–5,
`transient_exit_codes` from 1–255); `metrics`, `stop_when` and
`output_data_class` (§5, §7); and `inputs`, 1–8 named sources each mounted at
`/inputs/<name>/`.

A source is either a project snapshot (`snapshot_id`, `digest`, declared
`data_class`) or an earlier stage's outputs (`stage` plus `paths`, a subset of that
stage's `output_paths`). A stage whose only input is a single snapshot uses it
unchanged, mounted at `/inputs/`. `env` is refused for `docker-local-cpu-v1`,
whose contract has no environment.

Validation runs at submit and again at approval. It rejects duplicate stage keys
or input names, unresolved references, a cyclic stage graph, an input path its
source stage does not declare, a derived `ExecutionSpec` that fails, a stage hold
above `budget_cap_cents`, `egress` on a non-local backend or with a
private-derived input, and a metric or stop rule naming an undeclared output or
metric.

The sum of holds may exceed the cap. The cap is enforced at run time (§3).
`salad-gpu-v1` is specified but refused at submit with a 409 ("not enabled")
until its driver exists.

### 2. Authority: one human approval becomes a recorded, narrow delegation

- **Submit.** A human or an agent submits a spec (`campaign.submitted`). This
  grants nothing.
- **Approve.** A human (`role == "human"` and `require_decision_actor`) approves
  the exact digest. A `campaign_approvals` row records the approver, decision
  class, automation label and expiry. Expiry is at most approval time plus
  `deadline_seconds`.
- **Runner identity.** The runner acts *as the approving principal*. Each step
  calls an existing store method through `_mutate`, carrying a delegation marker
  that exists only in-process. `principal()` never sets it, so no HTTP request can
  carry it.
- **Intent first.** Before each call the runner writes a `campaign_step_intents`
  row in its own transaction. The row records the step kind, method, path,
  request digest, idempotency key, approval id and the resource ids it touches.
- **Check and complete in one transaction.** Inside `_mutate`'s transaction, a
  delegation hook requires an exactly matching intent (`pending`, or `done` for a
  replay); an approval whose approver is the live principal (live credential,
  unrevoked principal); a campaign status and approval state that allow the step
  (forward steps only while `running` under an unexpired approval; reduce-only
  revoke and reconcile steps also while `stopping` or `cancel_requested`, or after
  expiry); path resource ids owned by this campaign; and a step kind in the
  closed table. It narrows the actor's `project_id` to the
  campaign project and marks the intent `done`, with the response, in the **same
  transaction** as the effect, its events and its idempotency row. A `pending`
  intent therefore never has a committed effect.
- **Closed step table.** `create_task`, `revise_plan` (first plan only),
  `approve_task`, `compose_snapshot`, `release_snapshot`, `revoke_release` (only
  releases the campaign created), `create_experiment`, `authorize_budget`,
  `issue_grant`, `admit_attempt`, `revoke_grant`, `reconcile_grant`,
  `reconcile_unattempted`, `record_disposition` (`retain_quarantined` only),
  `custody_release`, `resolve_attempt`, `link_artifacts`, `derive_measurement`,
  `register_lineage_node`, `record_lineage_edge`. Nothing else
  is delegated, including project limits, worker credentials and registration,
  budget increases, exports, conclusions, program go, classification upgrades and
  route starts. Stage outputs reach later stages only through composition (§6).
- **Attribution.** `decision_class` gains `campaign_delegated`, which takes
  precedence; `require_decision_actor` accepts only a hook-verified delegated
  actor in addition to today's cases. Decision events carry `campaign_id`, `approval_id`, `intent_id`
  and the approval's original class. Each completed intent appends
  `campaign.step_completed`.
- **Retries.** A retry writes new intents for a new attempt number, within
  `max_attempts` and under the same approval, through the same hook and every
  existing fence. No step creates or widens authority.
- **Lapse.** If the approver's credential expires or is revoked, the campaign
  becomes `paused_delegation_lapsed`. A human re-approval of the same digest
  supersedes the old approval, and pending intents are re-issued. This is safe
  because a pending intent committed nothing.
- **Narrowed review.** The approver accepts in advance that declared outputs are
  retained and staged into the named downstream stages. This replaces today's
  per-manifest review, bounded by declared paths and byte caps, with no export
  and no declassification (§5).

### 3. Campaign budget with per-stage holds

`campaigns.held_cents` and `campaigns.settled_cents` form the campaign ledger.
Before any authority exists for a stage attempt, one runner transaction inserts
the attempt with `hold_cents = max_exposure_cents`. It increments `held_cents`
only if `held_cents + settled_cents + hold <= budget_cap_cents`.

Otherwise the attempt is recorded as `refused_budget`, with a
`campaign.stage_refused` event, and nothing is dispatched. The campaign then
starts no new stages. Ready stages reserve in topological order, then by key.

Existing fences stay underneath: the task budget (`budget_cents = hold ×
max_attempts`), project execution limits and concurrency at grant issue, and,
for a hold above 0, a per-stage experiment budget so `reserve_attempt` runs in the
admission transaction (Salad requires this).

Reconcile moves the hold to settled at the grant's `settlement_cents`. Local CPU
settles at 0, because no provider bills it. An exposure that `budget_status`
reports as unknown blocks new reservations.

### 4. Retry for transient failures only

An attempt is transient only in one of two cases:

- **T1 (safe repeat, DESIGN §6.1).** The grant expired with zero leases and zero
  operations, and `reconcile_expired_unattempted_grant` succeeded.
- **T2.** The worker recorded an `exited` observation with
  `termination_confirmed`, a run receipt has
  `exit_code ∈ transient_exit_codes`, and `reconcile_execution_grant` succeeded.

Everything else is permanent: observed `unknown` or no termination confirmation;
`quarantine_failed`, or no receipt or manifest after an exit; a grant stuck in
`cancel_requested`; any `Conflict`, `Forbidden` or `StoreError` from a delegated
step; an undeclared exit code; a budget refusal or unknown exposure.

An unknown effect makes the stage `blocked_unknown` and the campaign `blocked`
until a human reconciles it through the existing evidence routes. The stage is
not retried after that. A retry reuses the stage task and approved plan with a
fresh grant. A scientific replicate is a separate stage, never a retry.

### 5. Data class per input, enforced

`ReleaseSnapshot.classification` becomes `"public" | "private_derived"`, and v64
rebuilds the table's CHECK. `_authorize_snapshot` accepts `private_derived` only
for a `local-cpu` plan without `egress`. Provider input tickets keep requiring
`public`, and Salad's `data_policy: public_released_only` is unchanged, so
private-derived bytes never reach a third-party GPU. (Superseded, once implemented, by
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) §4: the `local-cpu` plan rule and
the public-only ticket rule become an operator trust setting keyed on the provider
instance, whose default admits nothing private.)

The class of a snapshot is the one recorded by ADR 0039 (`snapshot_data_classes`,
read with `data_class.snapshot_data_class`); a campaign defines no class of its own.
`SnapshotSource.data_class` is only the spec's claim: `public` is refused (409) at
submit and approve unless the record says `public`, and `private_derived` is always
allowed. A stage output is always `private_derived` and so is a composed snapshot
(`record_snapshot_data_class(..., derived=True)` in the composing transaction). A
stage's `output_data_class` accepts only `private_derived`; `public` is refused.
The runner releases each input with the more restrictive of the spec's class and the
recorded class, and a `public` release of a derived snapshot is refused by
`snapshot_releases` and by the delegation hook. Nothing upgrades `private_derived`
to `public`; there is no reclassification endpoint.

### 6. Snapshot composition (new, generic)

`SnapshotCompositionStore.compose` (`POST /projects/{p}/composed-snapshots`) is
human-only and delegable. It builds one snapshot from named sources, each under
its own prefix. A source is a retained project snapshot, or a manifest that is
custody-released and `retain_quarantined` (the staging rule). It is adopted
through `SnapshotStore.adopt_snapshot`, or the chunked path above 16 MiB, so the
existing bounds apply. A `snapshot.composed` event names every source and each
`(path, sha256)`. No table is added.

### 7. Stop criteria, cancel and the gate verdict

- **Metrics.** Each metric is `{name, path, json_pointer}`. The platform extracts
  it from retained bytes with `derive_output_measurement`.
- **Stop rules.** Each `stop_when` rule is `{metric, op, value}`, with `op` one of
  `lt`, `le`, `gt`, `ge`, `eq`. Rules are evaluated after their stage succeeds.
  A true rule moves the campaign to `stopping`: no new stages start, outstanding
  attempts finish, and the campaign ends `stopped` with reason
  `stop_rule:<stage>.<metric>`.
- **Cancel.** A human, or the submitting agent with `campaign:cancel`, moves the
  campaign to `cancel_requested`. The runner revokes active grants
  (`revoke_execution_grant`), waits for the worker to confirm termination,
  reconciles (`reconcile_execution_grant`, or `reconcile_expired_unattempted_grant`
  after expiry) and revokes its releases. The campaign ends `cancelled`, or
  `blocked` if any effect is unknown.
- **Verdict.** The campaign's own verdict (`continue` or `stop`) is operational.
  The scientific verdict stays with ADR 0032. The receipt records
  `scientific_verdict: "pending_human_approval"`, or `"not_assessed"` when no
  program is named. A campaign never records a go.

### 8. Runner, resume and the execution boundary

- **Runner.** `CampaignRunner.tick()` is synchronous and idempotent, and tests can
  call it directly. `tessera serve` runs it on a background thread every 2 s by
  default (`--campaign-tick-seconds`; `--no-campaign-runner` disables it).
  Default-on is safe because nothing runs without an approved campaign.
- **Each tick.** One runner per database (`campaign_runner_lease`) re-derives
  every stage's next action from database rows: *intent* (CAS the stage revision,
  insert the intent), *effect* (the delegated store call, which completes the
  intent), *advance* (CAS, update the stage or attempt, write the event).
- **Resume.** After a restart the runner replays a `pending` intent with the same
  key, applies a `done` intent's response, observes in-flight grants, leases,
  observations and receipts read-only, and never issues a second grant while the
  attempt's grant is unreconciled.
- **Execution boundary.** The runner never starts containers or calls a provider.
  A new generic `tessera-worker development-poll` lists the worker's own active,
  unleased, unexpired grants (`GET /worker/execution-grants`, worker audience). It
  runs the unchanged `DevelopmentWorkerService` for each in a per-grant workspace,
  recovering existing runtime descriptors first. `development-run` is unchanged.

### 9. Receipts and lineage

Every succeeded attempt carries its worker-posted run receipt digest
(`execution_run_receipts`), its manifest id and digest, and its grant,
admitted-attempt and operation ids. A stage cannot succeed without a run receipt;
only the local Docker adapter maps to one today. Lineage goes through
`ArtifactLineageStore`: an `external_import` dataset node per spec-named snapshot,
a `tessera_attempt` node per output artifact (reusing an existing node with the
same sha256), and `derived_from` edges from outputs to input nodes.

The terminal transition writes an immutable `campaign_receipts` row in the same
transaction. It holds the spec digest, the approval and its class, the budget
ledger, the attempts, run receipt digests, measurements, stop-rule results,
lineage ids, the verdicts and `evidence_class: "operational"`, plus
`receipt_digest`.

### 10. Surfaces and migration

API: `POST|GET /projects/{p}/campaigns`, `GET /campaigns/{id}`,
`POST /campaigns/{id}/approve`, `POST /campaigns/{id}/cancel`,
`GET /campaigns/{id}/receipt`, and the worker route. CLI:
`tessera campaign submit|approve|get|list|cancel|receipt`. MCP:
`tessera_submit_campaign`, `tessera_get_campaign` and `tessera_cancel_campaign`,
under new external-agent scopes `campaign:submit|read|cancel`; approval is never
exposed to agents.

Migration `migrate_v63_to_v64` adds `campaigns`, `campaign_approvals`,
`campaign_stages`, `campaign_stage_attempts`, `campaign_step_intents`,
`campaign_receipts` and `campaign_runner_lease`, and rebuilds `snapshot_releases`
with `CHECK(classification IN ('public','private_derived'))`, copying rows
unchanged. The same DDL goes in `_create_schema`, so backup shape validation
matches.

## Consequences

One approval replaces a hand-driven session. The approval is wide but closed:
only steps the spec names, each checked inside the transaction that commits it
and attributed to the approver and the approval. The `_mutate` delegation hook is
the one new security-critical path; its tests must show that it refuses anything
without a matching intent and that HTTP cannot reach it. DESIGN §2.3's child-task
contract (dependencies, output contracts, reserved parent budget) gets a bounded
form, and opaque fan-out stays disabled. Campaigns progress only while a worker
poller runs; without one, grants expire, T1 retries are spent and the stage fails.

**Not done:** Salad stages (proposing, approving and starting a remote route under
delegation, settled from billing evidence), which need the live session's trusted
server and guardian and may need a narrow 0033 exception; automated v2 bound-gate
records, split binding or program go; multi-host runners and cross-campaign
scheduling beyond project concurrency. Program-specific specs live outside this repository.

## Revisit when

A campaign needs more than 32 stages, dynamic
fan-out or approval beyond 7 days; or `private_derived` data must reach a third
party, which needs a new provider data policy.

## Amendment (2026-11-19): Salad GPU stage driver

Implemented with default tests and fakes only (no live Salad call has
been made by it). It replaces the submit refusal of `salad-gpu-v1` and narrows §2's "route starts" exclusion for Salad stages.

**Facts.** Today `scripts/salad_live_setup.py` (owner) and `salad_live_check`
(trusted server) drive a Salad run: a grant, admitted attempt and `kubernetes-job`
package, plus a route whose `approve`/`start` re-run `RemoteExecutionStore._current`
(lease/epoch, operator `TargetPolicy` allowlist, current human plan and source-use
reviews whose basis includes the packaged admission, live approver,
`salad_budget_binding`). No actual cost is recorded anywhere (`usage_status:
unknown`, guardian `actual_billed_cost_cents: null`), and there is no exit code or
run-receipt mapping.

**Split of duties.** The runner still never calls a provider (§8). A new
operator-run executor (`python -m tessera_control.campaign_salad_executor`) on the
trusted Linux server holds the trusted-server worker credential. It leases and
packages admitted Salad grants, keeps the lease alive, runs each started route
through the unchanged `salad_live_check --run` (subprocess), imports custody with
`TrustedSaladServer.import_retained`, uploads verified files through the existing
worker artifact/manifest routes, posts the `exited`, termination-confirmed
observation from the provider-provenance readback and records its outcome. It
never approves, starts or settles.

**Operator profile.** `tessera serve --salad-campaign-profiles <file>` loads a
closed, secret-free profile set (account, project, country, GPU class, resources,
worker principal, credential *reference*, custody/watchdog/monitor ids, per-run and
per-task cent ceilings, route window). Without it Salad stages stay refused at
submit (409); the provider-disabled path is unchanged. Runner and executor each
derive the exact `ExecutionSelection` from (profile, stage, operation, snapshot
digest) and the route checks they agree. The spec never supplies policy: the profile
carries a required, non-empty allowlist of image digests (`allowed_image_digests`), a
stage whose image digest is not on it is refused at submit/approve and again by the
driver, and a Salad stage takes exactly one snapshot input (several inputs compose a
private_derived snapshot, which can never reach Salad).

**Spec.** A `salad-gpu-v1` stage adds `salad`: profile name, worker principal
(= the profile's), a `WorkloadContractV1`, a program template (outcome, brief,
source, one gate) and `grant_ttl_seconds` (3600–43200). Submit and approve also
require a public spec-level input class; `env`/`egress` unset (the contract
declares provider-wide egress); no `transient_exit_codes`; argv, outputs and
byte cap equal to the contract's; `1 ≤ max_exposure_cents ≤` the per-run ceiling and
`hold × max_attempts ≤` the per-task ceiling. The run's `max_cost_cents` = the hold.

**Data class (ADR 0039).** A Salad input must be recorded public by the platform
(`target_is_third_party` / `require_public_input` on the `salad_container` target), never
the spec's declaration. Only a human staging declaration with license and source evidence
makes a snapshot public, so a Salad stage may consume only `SnapshotSource` inputs
recorded public: any `StageSource` input (stage outputs and composed snapshots are always
`private_derived`) or any snapshot not recorded public is refused. It is enforced at
submit and approve (409 naming the stage and input), at reservation (the stage fails
`salad_input_not_public` before any hold), after composition/input prep, by the delegation
hook on `release_snapshot`, and by ADR 0039's admission and ticket checks. The earlier
event-derived `recorded_snapshot_class` and the `output_data_class: public` option are
removed. Consequence: a GPU stage fed by earlier stages' outputs (the multi-stage program style
flow) cannot run on Salad until an audited reclassification endpoint exists.

**Steps.** New delegable rows: `create_study`, `create_study_source`,
`register_study_variant`, `create_split_lock`, `propose_decision` (plan or
source-use, reviewer = approver), `request_capability`, `approve_capability`,
`propose_route`, and `approve_route`/`start_route`/`cancel_route` (one path, told
apart by a `command` resource; cancel is reduce-only). A new reduce-only
`reconcile_unclaimed_salad` settles 0 only when the grant is `cancel_requested` and
control holds no `salad_proxy.claim` for create or start on any route of the
operation, so no provider mutation can have happened. Human-only, with the runner
pausing (`campaign.stage_awaiting_human`, naming the record and the reason): the
source assessment verdict; resolving the plan and source-use decisions (ADR 0032
decisions with v2 gates, needed per attempt because the review basis includes the
admission); recording the compute charge; project limits, worker/credential
enrollment, guardian/custody deployment and its orphan reconcile; human grant
reconcile of a blocked attempt; go/no-go, conclusions and exports.

**Budget and settlement.** Every existing fence applies: campaign hold, task budget,
project monetary limit and concurrency, the experiment-budget reservation at
admission (ceiling `hold × max_attempts`), `salad_budget_binding`, `max_cost_cents ≤`
plan budget, the guardian's projected-exposure check and total cap.
`CampaignRunner._settlement_for(stage, attempt)` settles a Salad attempt at the
sum of the human-recorded compute charges for its admitted attempt (or a
human-reconciled grant's `settlement_cents`), and at 0 only after
`reconcile_unclaimed_salad`. Until then the attempt is `awaiting_charge`: its
hold stays held and it blocks every new reservation with hold > 0. A later charge is
added to `settled_cents` as a late charge. The guardian estimate is recorded for
display and never settles.

**Retry and evidence.** Retry only after `reconcile_unclaimed_salad`. Any claim
followed by unconfirmed stop, reallocation, orphan, uncertain allocation or an
executor silent past the route window is `blocked_unknown`. Success needs a
`verified_journey` live receipt, a terminal route with confirmed termination and
provider-provenance receipts, and a quarantined control manifest matching the
imported sha256s, recorded as `salad_evidence_digest` in place of a run-receipt
digest. A revoked approver, revoked grant or cancel stops the run through the
existing heartbeat fence.

**ADR 0033.** No guardian, custody, proxy, route-overlay or harness code changes,
except one narrow change under ADR 0037's input-delivery exception: `_drive` calls
`TrustedSaladServer.deliver_inputs` once, after create and before start, for
`workload_v1` profiles only.

**MCP.** `tessera-mcp` registers campaign tools by default only when the
credential's live scopes include `campaign:*` (one status read at startup; fails
closed to today's catalog); `--campaign-tools`/`--no-campaign-tools` force it.
OpenCode harness credentials carry no campaign scopes, so the pinned catalog in
`harness_binding.py` is unchanged.

**Limits.** Each GPU attempt needs human decisions and settlement waits for a human
charge. A stage output is never public (ADR 0039), and there is still no human
declassification path.

## Amendment (2026-11-19): automatic Salad settlement and unattended GPU campaigns

Contract for the next change; default tests use fakes only. No live run is implied.

**Cost evidence today.** No path records a billed cost. The guardian accrues
`estimated_exposure_cents` = published high-priority GPU hourly rate
(`published_gpu_hourly_rate_not_actual_cost`, re-read every 5 s, the higher kept) ×
running seconds from a backdated first-running instant to the observed stop. Storage is
not modelled; every status reports `actual_billed_cost_cents: null` and
`financial_hard_cap: false`. The live receipt's `financial` is that projection,
`usage_status` is `unknown`, `salad_billing` holds declarations only, and the Salad
client has no billing or usage read. So no `provider_billed` source exists, and a rate ×
runtime figure is an estimate, not a proven upper bound. None is invented here.

**A. Settle a claimed attempt at its hold, gated by guardian evidence.** The guardian
already enforces the hold as its per-run cap (`max_cost_cents` = hold; it fences and
stops when accrued plus worst-case remaining exceeds it). A claimed Salad attempt settles
without a human at exactly its hold, `settlement_basis: guardian_bounded_hold`, only when:
- `verify_outcome` is `ok` or `failed`, never `unknown` (provider-confirmed terminal route,
  no reallocation, confirmed `exited` observation);
- a final guardian status, read by the executor after the live check through the existing
  `SaladSafetyHTTPSClient.status` and recorded with its outcome, shows `stopped` (not
  `reconciled`), a published rate, `actual_billed_cost_cents` null, and `orphan`,
  `allocation_uncertain`, `escalation_pending`, `budget_fenced` and `rate_unknown` false;
- `ceil(estimated_exposure_cents) <= hold`.

**Trust boundary.** The final guardian status is worker-attested input: the executor reads
it and passes it with its outcome, and the control plane checks only its shape. A
compromised or buggy executor could therefore claim a clean final and release campaign
progress (the next positive-hold reservation) without a human charge. Each attempt stays
bounded by its hold, and the experiment reservation keeps the hold held, so the exposure
is the sum of holds, not unbounded; but this is a new input that releases progress. Having
the control plane read the guardian status itself is a follow-up. The executor re-reads the
status a bounded number of times, a few seconds apart, while it is missing or not yet
`stopped`, and logs `guardian_final_unavailable` (error class only) when it gives up, in
which case the attempt waits for a human charge.

The runner then reconciles the grant through the delegated `reconcile_grant`
(`settlement_cents` = hold, `provider_terminated`) and moves the campaign hold to settled.
It writes no experiment-ledger entry: `record_charge` entries are immutable, additive
charges that can never be lowered, so an estimate there would read as a charge. The ledger
stays the single source because the attempt's reservation keeps `held_cents` = hold, which
`budget_status` counts until a human charge. Campaign, task, project, experiment and
guardian caps all carry the full hold; nothing is freed.

**The hold is not an upper bound on spend.** The guardian's estimate is rate × runtime
and models no storage, so the invoice can exceed the hold, and with it the campaign, task,
project, experiment and guardian caps. `guardian_bounded_hold` means "settled at the hold
the guardian enforced against its estimate", not "spend proven at most the hold". Excess
spend is recorded when the human charges the invoice (Reconciliation, below): once, as
`campaign.late_charge`, and a running campaign whose settled spend then exceeds its
`budget_cap_cents` is blocked (`budget_overrun`). Anything missing or unreadable
leaves `awaiting_charge` as today. An estimate above the hold makes the attempt `unknown`
and the campaign `blocked` (`estimated_overrun`). Zero still needs `reconcile_unclaimed_salad`.

`campaign_stage_attempts.settlement_basis` is one of `local_zero`, `unclaimed_zero`,
`human_charge`, `human_reconcile`, `guardian_bounded_hold` or `provider_billed`, which is
reserved: nothing produces it until a provider billing read exists, a new frozen-plane
feature needing its own record. The receipt shows per attempt the basis, `settlement_final`
(false for `guardian_bounded_hold`) and the guardian estimate labelled `estimate_not_invoice`.

**Reconciliation.** A human still records the invoice charge X at
`POST /experiments/{id}/budget/charges`; an invoice may be split into several entries.
The settlement covers the hold, so the total of the recorded entries is compared with the
hold, never entry by entry. For a `guardian_bounded_hold` attempt: the first entry emits
`campaign.settlement_confirmed` once and sets the basis to `human_charge`; while the total
is at most the hold nothing else changes (amounts unchanged, and the covered amount stays
at the hold; the experiment ledger never frees residual hold, so the campaign does not
either). Once the total exceeds the hold, the excess is added, each part once, as
`campaign.late_charge`, and an overrun blocks a running campaign. After a terminal
campaign the attempt and campaign totals still follow the charges and an overrun is the
event `campaign.late_overrun` (no status change), while the stored receipt stays
immutable; `GET /campaigns/{id}` shows the current basis. Lowering a settled hold to X is a ledger
policy change outside this amendment.

**ADR 0033.** No frozen file changes: the executor calls an existing read-only status
method. If one became necessary it would be a spend-safety exception recorded here.

**B. One-stage GPU campaign on public data plus approved private code.** No new input
mechanism. Per ADR 0039 amendment 1 the code and the data reach Salad as one staged
snapshot (data files recorded public, code files under a human code approval), which the
existing one-`SnapshotSource` Salad rule and `require_public_input` already admit; the
snapshot digest pins the approved bytes, because the approval row is immutable. The only
change is that the campaign receipt reports `input_code` and `input_code_files` for each
input. No second input slot and no approval-preserving composition are added: ADR 0040's
owner-configured per-provider data-class trust policy will replace this data-class
routing for Salad (amending ADR 0039), and the settlement and executor contracts below
become the Salad provider's settle and poll operations there.

**Unattended loops.** One loop contract that an always-on service supervisor can wrap
(no unit file ships); `campaign_salad_executor` adopts it now, and `tessera-worker
development-poll` with ADR 0040's provider poll interface:
`--config FILE` (credential file paths only, never secrets in argv, env, logs or health);
run forever by default (`--once`, `--max-cycles` for tests); SIGTERM or SIGINT stops new
work, finishes the current step and exits 0. Restart is idempotent (per-grant and
per-route directories, intents). A killed live check stops heartbeating and the guardian
stops the group within its 30 s bound; the attempt then ends `unknown` unless a receipt
exists, so a planned stop should allow the route window. `--health-file PATH` is replaced
atomically (0600) after each loop with pid, start, last loop, loop count, consecutive
errors, last error class and state; no port is opened. Logs are bounded JSON lines (error
classes only) and in-memory reports stay bounded. Exit 2 means configuration or
credential refused (do not restart).

**Still human after A and B.** Campaign approval; staging the input (public declaration
and code approval); the source assessment; per-attempt plan and source-use decisions; the
eventual invoice charge (no longer blocking); limits, enrollment, guardian and custody
deployment, orphan and blocked reconciles; go, conclusions and exports.

**Pre-approval (recommended, not decided).** A Salad stage's source assessment and its
plan and source-use review scopes are fixed by the approved spec and profile; only the
admitted attempt in the basis changes. They could be pre-approved once by binding their
exact scope digests into the approval, as a separate opt-in in the approve body (never the
spec, so a submitting agent cannot pre-fill it), resolved by the runner only on an exact
match and attributed `campaign_delegated`. ADR 0032's bound gates and human go stay
intact, but a human would no longer look at each retry, so it needs its own amendment and
owner decision. Go, conclusions, exports and classification are never pre-approvable.

**Note (ADR 0041, "Execution frames").** The one settlement routine is
`campaign_settlement.settle_attempt`; no other module writes `settled_cents`,
`held_cents` or `settlement_basis`, and late charges and confirmations go through the same
module without reopening a settlement. ADR 0041 stage 3 turns it into `settle_frame`. Once
that stage lands, the worker-attested guardian final status in amendment A (automatic
Salad settlement) is superseded by ADR 0041's guardian verdict, which control reads itself.

## Note (2026-11-25): per-run Salad reviews on the owner's behalf

The Salad driver's source assessment and per-attempt plan and source-use reviews (amendment
2026-11-19) can be resolved by the runner under the campaign delegation when the approval binds an
exact-digest pre-approval: one in the approve body (ADR 0044), one recorded under an owner-signed
delegation the campaign was approved under (ADR 0150), or one recorded under a delegation the owner
named in a direct approval (`review_delegation_id`, ADR 0150 amendment 2026-11-25). Each is
`campaign_delegated`, re-checked at every use and cited on the receipt; anything outside it stays with
a person.

## Amendment (2026-11-26): a block stops new spend, never the record of spend already made (q-2705, v0.104.0)

**Context.** On 2026-11-26 one loop damaged a live Salad campaign: the campaign went `blocked`;
control then dropped its attempts from the executor's work list and refused the finished route's
outcome (`Conflict`); leases and heartbeats went stale while the runner's silent-executor clock
kept running; when the block cleared, the first running pass called every route `unknown`,
re-blocked the campaign mid-pass, and kept issuing `issue_grant` for later reserved stages. The
delegation correctly refused those forward steps, but the runner recorded each refusal as a sticky
failed intent and settled 20 stages `failed_permanent` / `step_refused`.

**Decision.**

1. **A pass that blocks a campaign ends there.** The runner re-reads the campaign's status before
   each stage of a running pass, after each stage loop iteration and before each forward step, and
   ends the pass for that campaign once it is no longer `running` or `stopping` (`stopping` keeps
   forward steps only for the in-flight attempt, as before). Nothing is written for the step it did
   not attempt.
2. **A state-caused refusal is transient.** In `StepExecutor.run`, a `Forbidden` on a step that is
   not reduce-only, while the campaign is not forward-legal (`blocked`, `cancel_requested`), raises
   `StepTransient`: no failed intent, the intent stays `pending`, and the attempt keeps its
   reservation and its one hold. After the unblock the same idempotency key completes. Reduce-only
   steps keep the wider authority set. The delegation's refusal of every forward step outside
   `running` is unchanged, and a contract refusal (undeclared step, wrong resource, digest
   mismatch, expired approval, a recorded sticky failure) still fails closed as `step_refused`.
3. **Evidence is accepted while blocked or paused.** The Salad worker gate is split by direction:
   forward commands (`record_salad_package`, `mark_salad_executing`) keep requiring `running`,
   `stopping` or `cancel_requested`; evidence commands (`record_salad_outcome`,
   `record_salad_no_receipt`, `record_salad_custody_evidence`) are also accepted in `blocked` and
   `paused_delegation_lapsed`. The tenant and worker checks stay at the same data access.
   `list_salad_work` keeps such a campaign's `executing` attempts (resume, finish, keepalive) and
   `route_started` attempts (keepalive only) and returns `campaign_status`; the executor never
   launches a route unless the campaign is launchable. Nothing new is launched while blocked or
   paused: no `mark_executing`, no grant, no lease.
4. **An evidence pass before any silent-clock check.** Each provider may expose an
   `evidence_pass` hook (verification of recorded outcomes and reduce-only steps only, never a
   forward step). The runner calls it for a `blocked` campaign in its stuck-campaign pass, and at
   the start of every running pass before the stage loop. The Salad hook verifies an
   `outcome_recorded` attempt with the unchanged `verify_outcome` and settles it through the
   ordinary collect, resolve and charge path; it also covers the late import of ADR 0160's
   amendment and the reduce-only cancel of ADR 0205's never-sent amendment. A refused step inside
   the hook (for example a sticky failed `cancel_route` or `revoke_grant` intent in an unwind)
   fails that attempt through the stage loop's attempt-failure path and the hook goes on to the
   next attempt; a refusal that still escapes a hook ends that hook only, never the pass, and is
   recorded once per (campaign, provider, step, class) as `campaign.evidence_pass_failed`. One
   transition follows from this: a late-imported `unknown` attempt whose verified receipt shows the
   create was never sent is unwound (`_abandon`, `CREATE_NOT_SENT`); if a reduce-only step of that
   unwind is a sticky refusal, the attempt goes from `unknown` to `failed_permanent` /
   `step_refused`. Nothing was sent, and the grant keeps the hold until it is reconciled.
5. **Unblocking does not manufacture unknowns.** `_drive_blocked` records `campaign.unblocked`
   with the runner's clock (`at`); the Salad silent-executor clock restarts at the latest
   `campaign.unblocked` or `campaign.approved` (ADR 0160 amendment). With points 1 and 4, the first
   pass after an unblock neither calls a route that is still inside its window silent nor, if
   something does block the campaign again, takes another forward step in that pass.

**Consequences.** A mid-pass block leaves reserved stages reserved with their holds. A run that
finished while its campaign was blocked is recorded and settled the ordinary way. Stages already
burned by the earlier behaviour are not un-failed by this change; a sticky failed intent of an
attempt number still replays (`campaign_steps._slot`), so those need a sanctioned new attempt.
Any other `Conflict` from a delegated step (for example a stale `expected_revision` race) is still a
sticky failure; that is a separate, deferred change.
