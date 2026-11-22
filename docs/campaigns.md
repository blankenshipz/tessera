# Campaigns and the platform campaign runner

A campaign is a typed, digest-pinned DAG of ordinary Tessera runs that a session
submits once and a human approves once. The platform runner then drives every step
(task, plan, approval, snapshot release, grant, admission, collection, custody release,
measurement, reconcile, lineage) through the same store methods a human would call, so
no session has to stay open between stages. Contract: [ADR 0038](decisions/0038-campaign-specs-and-platform-campaign-runner.md).
A campaign receipt is operational evidence (`evidence_class: "operational"`); it is not
a scientific verdict and never records a program go.

## What a session submits

A spec (`version: 1`) with `name`, `budget_cap_cents` (0..5000, USD), `deadline_seconds`,
`worker_principal_id` and 1..32 `stages`. Each stage names its `image` (a pinned
`name@sha256:` reference), `argv`, `output_paths`, `max_output_bytes`,
`max_duration_seconds`, a per-attempt hold `max_exposure_cents`, `grant_ttl_seconds`,
`retry {max_attempts, transient_exit_codes}`, optional `metrics`
(`{name, path, json_pointer}`) and `stop_when` rules (`{metric, op, value}`), and its
`inputs`: a retained project snapshot (`kind: snapshot`, with digest and `data_class`) or
the declared outputs of earlier stages (`kind: stage`). A stage with one snapshot input
sees it at `/inputs/`; any other stage sees each input at `/inputs/<name>/`, built by a
composed snapshot. Submitting grants nothing and spends nothing. A v2 stage may also pin
another campaign's output (`kind: artifact`); that resolves through the source campaign's
v2 receipt and its stage I/O index, so a campaign that finished before schema v65 cannot be
an artifact source.

A v2 stage may also declare `gate_evidence` (`{split_lock_id, seed, metrics: [{name, partition,
unit}]}`, each name a stage metric). It is part of the digest; the runner binds that
pre-registered split before dispatch and records each declared metric as a gate metric record
under the campaign's approval, so a conditional unlock can run unattended
([ADR 0049](decisions/0049-campaign-gate-evidence.md)). The split lock must already exist; Salad
stages do not take it. A stage with `gate_evidence` must have `retry.max_attempts` of 1: a
retry would re-admit the same variant and a seed binds once per variant and lock.

```
tessera campaign submit --owner-header FILE --project-id P --spec spec.json --idempotency-key K
```

Submit and approval re-validate the spec: duplicate keys, unresolved or cyclic
references, undeclared output paths, a hold above the cap, `env` on the local backend,
egress with a private-derived input, and unknown metric or stop-rule names are refused.
A `salad-gpu-v1` stage is refused with 409 unless the server was started with
`--salad-campaign-profiles` and the stage fits that operator profile (see Salad GPU stages).

## Fetching public content for a later no-egress stage

A stage whose inputs are all `public` may declare `egress` (local CPU backends only) with an
allowlist of public hosts and, optionally, a license and source per host
([sandbox egress](sandbox-egress.md)). Its declared outputs are content-addressed run
outputs (sha256 and size recorded) and are always `private_derived`. A later stage takes
them with a `kind: stage` input and runs with no network: it cannot declare `egress`
because its input is `private_derived`. The campaign receipt records, per attempt of the
fetch stage, the approved egress policy (hosts, ports, declared license and source), the
worker's journal (journal sha256 and per-host connection and byte counts, `null` until
the worker records it), the output sha256 and size, and the downstream stage's input data
class. `tests/test_campaign_public_fetch_stage.py` proves this chain on the local
backend with a fake Docker adapter.

Limits: an output above 16 MiB uses chunked [large-artifact custody](large-artifacts.md)
(default ceiling 8 GiB per artifact, operator-configurable). The declared license is not
verified and the fetched bytes are not public: nothing upgrades their class. A downstream
Salad stage on these outputs needs the operator trust binding of ADR 0040; no Salad run is
covered by that test.

## The one human approval

A human approves the exact digest and revision
(`tessera campaign approve --campaign-id C --expected-revision N --expected-spec-digest D
--automation-label L`; the owner credential needs the automation label, as for any
decision). Approval records the approver, decision class and expiry (at most
`deadline_seconds`, never past 7 days). The runner then acts as the approver: each step is
written as an intent row first, then executed inside the store's own transaction by a
check that requires an exactly matching intent. The marker is in-process only; no HTTP
header or body can set it. Events carry the `campaign_delegated` class with the campaign,
approval and intent ids. If the approver's credential expires or is revoked the campaign
pauses (`paused_delegation_lapsed`); a human re-approval of the same digest resumes it. A
forward re-approval never extends the total bound: its expiry is capped at the first
approval plus `deadline_seconds`, and one after that point is refused with 409. When an
approval lapses the runner pauses the campaign with `approval_expired`.
A re-approval of a `cancel_requested` or `blocked` campaign installs a live delegation for
reduce-only cleanup only and never resumes forward work, so a lapsed approver cannot strand a cancel.

Agents with the external scopes `campaign:submit|read|cancel` may submit, read and cancel
(`tessera_submit_campaign`, `tessera_get_campaign`, `tessera_cancel_campaign`, enabled with
`tessera-mcp`: registered by default only when the credential's live scopes include
`campaign:*`, forced with `--campaign-tools` or `--no-campaign-tools`). They cannot approve.
`campaign:read` covers every campaign in the credential's project, not only the caller's own;
cancel stays submitter-only. The v2 spec, receipt, list, snapshot, output, log and budget
tools and their scopes are in [mcp-campaign-client-path.md](mcp-campaign-client-path.md).
Never give campaign scopes to an OpenCode harness credential: the pinned tool catalog would
then block with `catalog_inventory_changed`.

## What the runner does

`tessera serve` ticks the runner every 2 s (`--campaign-tick-seconds`, 0.2 to 60;
`--no-campaign-runner` turns it off). One runner per database holds a lease. Per stage,
in topological order, it prepares the task and input, reserves a hold, releases the input
snapshot, issues a grant, admits an attempt, and observes the worker. It never starts a
container: a worker must run `tessera-worker development-poll`, which runs the unchanged
development worker for each active unleased grant addressed to it. Without a poller grants
expire and attempts are retried or fail. After a run the runner retains outputs in
quarantine under the approver's pre-approval, releases custody of the declared paths,
resolves the attempt, derives measurements, reconciles the grant, and records lineage.

## Limits and refusals worth knowing

- `max_exposure_cents` x `retry.max_attempts` may not exceed 5000 (the experiment budget
  ceiling) for any stage.
- v1: at most 32 stages, 256 input edges and 8 inputs per stage. v2: 64 stages, 512 edges,
  8 inputs per stage, 1 MiB of canonical JSON, 1..128 typed outputs per stage, and at most
  16 MiB per declared output.
- If an experiment budget's exposure is unknown the stage is `refused_budget` with reason
  `exposure_unknown` (not `budget_refused`); nothing is dispatched.
- A stored spec whose digest no longer matches its approval fails the campaign with
  `spec_digest_mismatch`.

## Budget, retry, data class, cancel and restart

- **Budget.** A stage attempt reserves `max_exposure_cents` against
  `budget_cap_cents` in one transaction. If `held + settled + hold` would exceed the cap,
  the attempt is `refused_budget`, a `campaign.stage_refused` event is recorded, nothing
  is dispatched, and the campaign starts no further stages. Settlement is chosen
  by the stage's provider: local Docker settles at 0 (`local_zero`), a self-hosted
  provider settles `local_zero` or `human_reconcile`, and Salad uses its billing source.
  A provider with no settlement source refuses to settle (`settlement_unavailable`).
  Unknown exposure on the stage's experiment budget refuses new reservations. If project
  limits keep a stage from getting a grant, one `campaign.stage_waiting_limits` event is recorded.
  Project limits, concurrency and the task budget still apply underneath.
- **Scratch (ADR 0059).** A CPU stage may set `scratch_bytes` (1 MiB to 64 GiB) to ask for
  a larger `/work` scratch than the operator's default. It is part of the approved plan and
  is granted only within the provider's operator ceiling (`max_scratch_bytes`, 64 MiB unless
  the operator raised it, declared in the provider file and set on the poller): a larger
  request is refused at submit and approval with `scratch_exceeds_operator_ceiling`, never
  mid-run with `ENOSPC`. The scratch is memory-backed, so the worker also needs a memory
  limit that holds it. The receipt records `scratch: {requested_bytes, granted_bytes}` per
  attempt; Salad and other providers that do not grant scratch refuse the field.
- **Grant lifetime and slots (ADR 0048).** A stage whose `grant_ttl_seconds` is shorter than
  `max_duration_seconds` plus 300 s is refused at submit and approval
  (`grant_shorter_than_stage_duration`). The runner issues a grant, and so starts its
  lifetime, only when the project limits and a worker slot (`worker_slots`, default 1) are free
  (`campaign.stage_waiting_slot` records the wait), and an expired grant whose operation has
  a confirmed exit and a recorded cost is reconciled automatically by the runner tick (campaign grants only, credited to the campaign actor)
  (`execution.expired_terminal_grant_reconciled`), freeing its slot. Anything unconfirmed,
  running, unknown or without a recorded cost is left to a person.
- **Retry.** Only two cases are transient: a grant that expired with no lease and no
  operation (reconciled as unattempted), and a confirmed exit whose code is in
  `transient_exit_codes`. Everything else (an undeclared exit, any refused step) is permanent. An unknown
  observation, a missing receipt or manifest is not retried: it marks the attempt
  `unknown`, which makes the stage
  `blocked_unknown` and the campaign `blocked` until a human reconciles it. A retry uses
  a fresh grant under the same approval. A scientific replicate is a separate stage.
- **Confirmed exits (ADR 0053).** An `admitted`, `running` or `unknown` attempt whose worker
  confirmed the exit, with no contradicting observation, is resolved by the runner on its next tick:
  the grant and its slot are released and the cost recorded (never above the hold), the attempt becomes
  `failed_transient` / `exit_confirmed_unresolved` (or the declared or undeclared exit-code class when a
  run receipt records one) and the stage's retry policy decides what follows. Only a non-billable
  worker-grant provider is resolved this way. An `admitted` or `running` attempt waits while a run
  receipt appears, its lease is live or a large-artifact upload is in progress (until the grant
  expires), and 120 s after control recorded the exit, so a late receipt is not overtaken.
  An attempt with no such evidence stays `unknown` for a person. It is shown on the receipt as
  `exit_resolved`, never as a pass or a measurement.
- **Refused launches (ADR 0046).** An unknown attempt whose worker reported a refused
  launch is reconciled by the runner itself, with no person: the report is a closed
  `launch_error` class on an unknown observation with no provider identity (the development
  worker sends it only when Docker definitely refused a network or container create and no
  container exists), and the attempt holds `0` cents with an expired zero-exposure grant and
  no container id, provider resource, receipt or manifest. The attempt becomes
  `failed_transient`/`not_started` and settles zero (`unclaimed_zero`), the grant is
  reconciled at zero, and the stage is re-queued without using a scientific retry. It is
  recorded as `campaign.attempt_not_started` and `execution.not_started_grant_reconciled`
  events, on the frame settlement and on the campaign receipt, and is never evidence for any
  gate, stop rule or metric. After two identical refusals of a stage the next one stays
  `unknown` and the campaign `blocked` with the repeat named
  (`campaign.autoreconcile_stopped`). A host reboot, a lost report or any attempt that held
  money or shows a container keeps the human path: control cannot tell an unreported start
  from no start.
- **Data class (ADR 0039).** Inputs are `public` or `private_derived`; the class of a
  snapshot is the one the control plane recorded when it was staged
  (`snapshot_data_classes`), never the spec's claim. A spec naming a snapshot `public` is
  refused (409) unless it is recorded `public` (a human staging declaration with license
  and source evidence); naming it `private_derived` is always allowed. Outputs and composed
  snapshots are always `private_derived`, so `output_data_class` accepts only
  `private_derived`. A public release of a snapshot built from run outputs is refused. A
  private-derived release is accepted only for a local CPU run without egress, so a stage
  fed by an earlier stage cannot use egress. Provider input tickets still require `public`.
  Nothing upgrades a class; there is no reclassification endpoint yet.
- **Cancel.** `tessera campaign cancel` (a human, or the submitting agent) is
  reduce-only: the cancel transaction fences never-leased grants, workers are not listed or
  leased grants of a campaign that is not `running`, and the runner revokes active grants, waits for confirmed termination,
  reconciles, revokes its own releases and ends the campaign `cancelled` (or `blocked`).
- **Restart.** State lives in SQLite. After a restart the runner replays a `pending`
  intent with the same key, applies a `done` one, and never issues a second grant while
  an attempt's grant is unreconciled.

## Window requirement

A stage is refused before any grant or hold (failure class `deadline_too_short`) when the
time left is shorter than its `max_duration_seconds` plus a 300 s settle margin. The time
left is the earliest of the campaign approval's expiry and the expiry of the approving
human's and the stage worker's credentials. A stage of the maximum 3600 s therefore needs
at least 65 minutes (3900 s) of approval and credential life when it is dispatched; a
one-hour credential can never start it. Each retry attempt is checked again when it is
dispatched. Re-approval does not extend the campaign's total wall-clock bound.

## Receipt

`GET /campaigns/{id}/receipt` (404 until terminal) returns an immutable receipt with
`campaign_id`, `spec_digest`, the approval, status and stop reason, the budget ledger
(cap, held, settled), per-stage attempts (grant, admitted attempt, operation, run receipt
digest, exit code, manifest id and digest, holds and settlements), inputs with data
class, measurements derived by the platform from retained bytes, stop-rule results,
lineage node and edge ids, `verdict {operational, scientific_verdict}` and
`receipt_digest`. `scientific_verdict` is `pending_human_approval` or `not_assessed`.
Do not report a campaign as run without reading its receipt.

## Standing approvals and scheduled starts

A human can create a standing approval (ADR 0042): it binds one spec digest to explicit
caps (per-start budget, `max_starts`, `max_total_budget_cents`, `min_interval_seconds`,
`max_concurrent`, a `not_after` window of at most 30 days, optional retries and a
follow-on template). The runner tick then starts ordinary campaigns from it with no new
approval for each start. Each start is an ordinary campaign plus an approval row with
`decision_class = standing_approval`, `automation_label = standing_approval:<id>`, the
creator as approving principal, `expires_at = start + deadline_seconds` and the trust
digest recorded at creation, so a later trust change can narrow a start but never widen
it. A start is refused (and nothing is created) when the creator is no longer a live
human project member, exposure is unresolved, project spend headroom is short, or the
spec, provider or data-class checks fail.

Routes are human-only (`/projects/{id}/standing-approvals`, list, show, revoke). CLI:
`tessera campaign standing create|list|show|revoke`. Revoke stops new starts and abandons
pending retries and follow-ons; running campaigns keep their own approval unless
`--cancel-running` sends an ordinary cancel for each. `tessera serve
--no-campaign-scheduler` keeps the runner but stops scheduled starts. A recorded start is
not evidence that a run happened; read the campaign receipt. Not covered: parameterized
templates, data handoff between chained campaigns, cron expressions, editable caps, and a
desktop UI.

A standing approval never carries a review pre-approval. A scheduled start inherits no
Salad review decision: source assessment and the per-attempt plan and source-use reviews
stay human stops (see Salad GPU stages), exactly as for a hand-approved campaign. If the
project later supports an explicit, opt-in review pre-approval in a human approve body, a
standing approval still does not carry it. A start without a `min_interval_seconds` can
follow the previous one immediately, limited only by `max_concurrent`, `max_starts` and the
budget caps; set an interval for a paced schedule. A creator whose creating credential
expires, is replaced or is revoked stops all starts.

## Gates, unlock and the program budget (ADR 0045)

- `POST /campaigns/{id}/gate-decision` (`expected_revision`, `decision` `go|no_go`, `note`)
  records a human gate decision on a finished campaign with resolved spend. Human only: an
  agent, worker or external-scope credential gets 403; there is no MCP tool. The latest
  decision wins, so `no_go` retracts a `go` and vetoes any computed outcome. Only a go
  recorded by a person's session (`decision_class` `human_session`) unlocks: a decision from
  the owner credential with an automation label is recorded but never counts as a go,
  unless the operator started `serve` with `--accept-automation-gate-go`, which counts
  that class (never the unverified `desktop-owner` label) and keeps the class on the
  record and receipt (ADR 0051).
  `GET /campaigns/{id}/gate-decisions` lists them.
- `unlock` on a campaign approval body or on a standing approval lists up to four
  requirements: an earlier campaign plus `accept_human_go` and/or a `bound_outcome` rule
  (two arms, metric, split lock, partition, statistic, comparison, threshold, minimum
  pairs, allowed sources, allowed outcomes `pass`/`fail`). It is evaluated at the first
  approval and at every standing start (retries and follow-ons included), inside the
  admission transaction. An `unlock` on an approval of a campaign that is not `submitted` is
  refused (409), never ignored. Refusals read `unlock_not_satisfied:<index>:<code>`.
- A requirement may add `measurement` (`{stage, name, op, value}`): a fail-closed condition
  on one number in the named campaign's terminal receipt (the stage needs exactly one
  succeeded attempt with that measurement). It narrows the go or outcome and never replaces
  it; refusals are `measurement_*` codes (ADR 0051).
- A v2 stage output of `kind: evidence_rows` with an `evidence` block (`split_lock_id`,
  `variants`, `seeds`, `metrics` {name: unit}, `partitions`; at most 5000 cells) holds
  `{"rows": [{variant, seed, metric, value, partition, sample_count}]}`. After the stage
  succeeds the platform ingests it itself, only if the rows fill the approved grid exactly
  and match the split lock (`sample_count` = the partition's member count); otherwise nothing
  is ingested and a paired unlock over it stays closed. Ingested rows feed a bound-outcome
  unlock as derived records; a stored paired group refuses them (ADR 0052).
- `POST /projects/{id}/paired-replicate-groups` (human) pairs two variants' per-seed metric
  records by seed and aggregates the delta with a CI; it refuses unpaired, unresolved,
  failed or cancelled seeds instead of dropping them. The unlock check recomputes and never
  trusts a stored group.
- `tessera serve --program-budget FILE` (`tessera.program-budget.v1`, owner-only) caps the
  committed money across campaigns; every first approval and standing start counts against
  it. It never replaces the guardian or campaign caps.
- A receipt gains an `unlock` section when admission was conditional.

## Not implemented

Bound gate outcomes without human-entered metric records (a human
records every metric), a program budget recorded on events or re-read after startup,
re-checking an unlock when a lapsed campaign is re-approved; multi-host runners; dynamic fan-out; more than 32
stages in a v1 spec (v2 allows 64) or approvals past 7 days. Human releases of the same snapshot to the same worker
block the runner (it records `step_refused` and never revokes the human release).
An admitted attempt left unresolved after cancel or a T1 expiry keeps its experiment-level
budget reservation pending: resolving the attempt does not free it, only a recorded charge does.

## Local check

`scripts/platform_validation/run.py --only campaign` is opt-in. It needs Docker and a local
`python@sha256:` image (never pulled), starts its own `tessera serve` and poller, and runs
synthetic campaigns covering staged inputs, transient retry, budget refusal, restart,
receipt, cancel, the data-class refusal and a version-2 spec with a stop rule and a later campaign pinning its output (`kind: artifact`). The default
`run.py` run skips it; `--include-opt-in` adds it. See [platform validation](platform-validation.md).

## Salad GPU stages

Contract: the Salad GPU stage driver amendment in ADR 0038. Every Salad GPU stage still
needs people at a few fixed points; the runner pauses there and says why. Nothing below
runs a provider from the runner or the API.

A stage with `backend: salad-gpu-v1` adds a `salad` block (profile name, worker, a
`tessera.salad.workload.v1` contract, a program template of outcome, brief, one source and
one gate, and a `grant_ttl_seconds` of 3600 to 43200). Provider account, project, GPU class,
resources, credential reference, cent ceilings, route window and the allowlist of image
digests (`allowed_image_digests`, required) come only from the
operator profile file (`tessera.salad.campaign-profiles.v1`, owner-only, no secrets) passed
as `tessera serve --salad-campaign-profiles FILE` (or under `salad_profiles` in the
`--campaign-providers` file, see [campaign providers](campaign-providers.md)). Without it, or when the stage exceeds
the profile (per-run or per-task cents, worker, image digest not allowlisted), or an input is not recorded public, submit
and approval return 409. `env`, `egress` and `transient_exit_codes` are refused, argv and
outputs must equal the contract, `max_duration_seconds` is at most 3600 (the Kubernetes-job
package limit), and every snapshot input must be recorded public unless the provider trust
setting says otherwise. A stage with several inputs (snapshot, `stage` and `artifact`) is
composed into one snapshot before staging, so the executor still receives one input.

**Data class.** By default a Salad stage may consume only snapshot sources the control
plane records public (ADR 0039: a human declaration with license and source evidence at
staging), and anything else is refused with 409 at submit and approve and again at
reservation (the stage fails `salad_input_not_public` before any release or hold). Under
ADR 0040 the operator's [provider trust](provider-trust.md) setting decides what `salad`
may receive: with `private_derived` approved for the Salad executor's principal, a stage may
also take `stage` and `artifact` inputs, which the core composes into one snapshot, and the
route's `data_policy` becomes `provider_trust_policy_v1`. A stage with any non-public input
may not declare workload fetch or redirect hosts. `output_data_class: public` no longer
exists: it cannot make a public snapshot, and the spec refuses it. See
[campaign providers](campaign-providers.md) for the provider file and the loop contract.

**Automated by the runner (delegated by the one approval):** study, study source, study
variant and split lock for the stage; the plan and source-use decision proposals; the
capability request and approval (valid up to 23 hours, once per stage plan); the route
proposal, approval and start (allowlisted from the profile, never from the spec); cancel
and revoke; the zero-settlement reconcile of an unclaimed attempt; collection, resolution,
measurements and lineage from the verified evidence.

**Pauses for a human, with `campaign.stage_awaiting_human` naming the record:**

- Source assessment: assess the study source as `supported`. Nothing is reserved before it.
- Per attempt, resolve the plan and source-use decisions. They are bound to this admitted
  attempt (ADR 0032), so each attempt needs fresh ones, and they must be resolved before the
  lease ends (`max_duration_seconds` after the executor leases; the grant expiry is
  separate). Unresolved, rejected or stale reviews end the attempt through the unclaimed
  path: grant revoked, route cancelled, `reconcile_unclaimed_salad` settles 0 only when no
  `salad_proxy.claim` for create or start exists, and the stage retries within
  `retry.max_attempts`.
- A grant someone else reconciled does not settle billed Salad work at 0: with a create or
  start claim, only a recorded settlement or compute charge settles the attempt; a NULL
  settlement waits (`awaiting_charge`). Cancel and unwind route every Salad attempt through
  the Salad driver, never the local CPU path.
- Settlement of a claimed Salad attempt has a recorded basis
  (`campaign_stage_attempts.settlement_basis`): `local_zero`, `unclaimed_zero`,
  `human_charge`, `human_reconcile`, `guardian_bounded_hold` or `provider_billed`
  (reserved: no provider billing read exists). Order: unclaimed proof, a human-reconciled
  grant's `settlement_cents`, recorded compute charges
  (`POST /experiments/{id}/budget/charges`, the sum), then the guardian-bounded hold.
- The bounded hold settles the attempt at exactly its hold, without a human, only when the
  verdict is `ok` or `failed` (never `unknown`) and the executor-recorded final guardian
  status shows `stopped`, a published rate, `actual_billed_cost_cents` null, none of
  `orphan`, `allocation_uncertain`, `escalation_pending`, `budget_fenced`, `rate_unknown`,
  and `ceil(estimated_exposure_cents) <= hold`. It writes no experiment-ledger entry, so
  the reservation keeps the full hold; nothing is freed. An estimate above the hold makes
  the attempt `unknown` and blocks the campaign (`estimated_overrun`). Anything missing
  keeps the attempt in `awaiting_charge` with its hold held, and new reservations with a
  positive hold wait (`campaign.stage_waiting_exposure`) until it settles.
- Reconciliation: a human still records the invoice charge, possibly as several entries.
  For a bounded-hold attempt the first entry emits `campaign.settlement_confirmed` once
  and sets `settlement_confirmed_at`; the basis stays `guardian_bounded_hold` while the
  recorded total is below the settled amount (the hold) and becomes `human_charge` only
  when the total equals the settled amount or exceeds it; the total of the entries is compared with the hold
  (never entry by entry), so totals at or below the hold change no amount, and only the
  part above the hold is added, once, as `campaign.late_charge`. A cap overrun blocks a
  running campaign; on a terminal campaign the attempt and campaign `settled_cents` still
  follow the charges and an overrun is the event `campaign.late_overrun` (no status
  change), while the stored receipt is immutable. The executor re-reads the guardian
  status a few times (`guardian_retries`, `guardian_retry_seconds`) and logs
  `guardian_final_unavailable` when it cannot read it; the status is worker-attested (ADR
  0038 amendment, trust boundary). The receipt shows
  each attempt's basis, `settlement_final` (false for the bounded hold) and the guardian
  estimate labelled `estimate_not_invoice`; each input also reports `input_code` and
  `input_code_files` (ADR 0039 approved private code in the staged snapshot).
- Not delegated at all: project execution limits, worker and credential enrollment,
  guardian and custody deployment and orphan reconcile, reconcile of a blocked attempt,
  go/no-go, conclusions and exports.

Implementation: one routine, `campaign_settlement.settle_attempt`, moves a hold to settled in
the caller's transaction and is the only code that does (late charges and the confirmation
relabel are in the same module). It is idempotent by a stored request digest
(`settlement_request_digest`): the same digest replays the stored settlement and writes
nothing, a different one is a `Conflict`; the `settled_cents IS NULL` compare-and-swap stays
as a second guard. ADR 0041 stage 3 renamed it `settle_frame`.

A claim followed by unconfirmed termination, reallocation, synthetic-only receipts, a
manifest that does not match the imported hashes, or an executor silent past the route
window makes the attempt `unknown` and the stage `blocked_unknown`. Success needs a
`verified_journey` live receipt, a terminal provider-confirmed route and a matching
quarantined manifest; the digest is recorded as `salad_evidence_digest`.

Limitations: each GPU attempt needs human reviews and settlement waits for a human charge
unless the guardian evidence above bounds it;
a stage output is never public (ADR 0039); a capability approved for a
stage plan cannot be re-approved, so a stage cannot outlive its capability's 23 hours.

## Salad GPU stage executor

Contract: the Salad GPU stage driver amendment in ADR 0038. The runner never calls a
provider. On the trusted Linux server an operator runs
`python -m tessera_control.campaign_salad_executor --config FILE [--once] [--health-file PATH]` with the
trusted-server worker credential: the worker token of the Salad executor enrollment whose
principal is listed under `salad` in the provider trust file (see
[provider trust: stable executor](provider-trust.md#stable-executor-route-setups-that-mint-principals)). The closed, owner-only config
(`tessera.salad.campaign-executor.v1`) names the state directory, the operator profile file
and profile, the worker and cancel credential files, the credential directory, guardian socket
and uid, custody, a private work directory, the source revision and a 2 to 30 s poll.
Per pass it leases and packages admitted Salad grants (refusing a plan budget above the
profile's per-task ceiling), keeps leases alive (a renewal refusal is recorded and never
retried), and for each started route re-derives the exact selection from the profile and
refuses on any difference. It marks the attempt executing, runs the unchanged
`salad_live_check --run` in a fresh interpreter with a private per-route config (removed
afterwards), imports sealed custody, uploads the verified files through the worker artifact
and manifest stores, reports the provider-confirmed exit only when the route shows confirmed
termination without reallocation, and records its outcome. A route directory under the work
directory marks a route as attempted: it is never run twice. The executor never approves,
starts a route, resolves a decision, records a charge or reconciles a grant.

After the live check and the exit report the executor reads the final guardian status
through the existing read-only `SaladSafetyHTTPSClient.status`, projects only the recorded
keys and passes them with the outcome; an unreadable status records `null` and the runner
waits for a human charge. The loop runs forever by default; `--once` runs one pass.
SIGTERM or SIGINT stops new work, finishes the current step and exits 0 (a planned stop
should allow the route window: a killed live check stops heartbeating and the guardian
stops the group, leaving the attempt `unknown` unless a receipt exists). `--health-file`
is replaced atomically (0600) after each loop with pid, start, last loop, loop count,
consecutive errors, last error class and state (written before the first step and after
each loop, never during a step, so a long route leaves it stale: alert only beyond the
route window plus one poll); no port is opened. Logs are bounded JSON
lines with error classes only. Exit 2 means configuration or credentials were refused: do
not restart. Restart is idempotent (per-route directories, intents).
