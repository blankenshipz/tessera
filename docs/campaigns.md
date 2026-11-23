# Campaigns and the platform campaign runner

A campaign is a typed, digest-pinned DAG of ordinary Tessera runs that a session
submits once and a human approves once (or, inside an owner-signed delegation whose written rule is
met, the platform records the approval on the owner's behalf: see
[delegated decisions](#delegated-decisions-adr-0122)). The platform runner then drives every step
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

Instead of copying a campaign id and sha256 by hand, a v2 stage may take a value recorded on an
earlier campaign's terminal receipt with `kind: receipt_output` (`name`, `stage`, `output`, and
exactly one of `campaign_id` or `predecessor`). The submit body may add `predecessors:
{name: campaign_id}` (at most 16). At submit, in the same transaction, the source must be in the
same org and project, `succeeded`, with a terminal receipt listing a succeeded attempt output of
that name (the last succeeded attempt wins). The reference becomes a pinned `artifact` input with
`resolved_from {predecessor?, source_receipt_sha256}` before the spec is digested, so approval
binds the exact bytes and lineage, and the usual artifact checks still run at submit and at stage
start. Refusals are 409 and name the stage, input and reason. The consuming campaign's receipt
gains `chained_inputs`. A predecessor must already have succeeded at submit. `resolved_from` is
set by submit only; a submitted one is refused. Validate resolves references read-only; a standing
approval refuses `receipt_output` (pin an `artifact` input). See
[ADR 0080](decisions/0080-campaign-inputs-from-a-predecessor-receipt.md).

A v2 stage may also declare `gate_evidence` (`{split_lock_id, seed, metrics: [{name, partition,
unit}]}`, each name a stage metric). It is part of the digest; the runner binds that
pre-registered split before dispatch and records each declared metric as a gate metric record
under the campaign's approval, so a conditional unlock can run unattended
([ADR 0049](decisions/0049-campaign-gate-evidence.md)). The split lock must already exist; Salad
stages do not take it. A stage with `gate_evidence` must have `retry.max_attempts` of 1: a
retry would re-admit the same variant and a seed binds once per variant and lock.

A v2 stage may be an evaluation stage (`evaluation: {final, holdout}`,
[ADR 0068](decisions/0068-sealed-holdout-evaluation-stages.md)): it runs the spec's scorer image
and must declare the `metrics` it scores. `final: true` means no stage may consume its outputs.
A final stage may declare `holdout: {input, partition_digest, claim_scope}`: `input` is one of
its `snapshot` or `artifact` inputs (not `public`) whose pinned digest is `partition_digest`, no
other stage of the spec may read that digest, and retries are off (`retry.max_attempts` 1, no
`infra_retries`). The runner opens one claim per project, partition digest and claim scope just
before the stage's input is released, after its dependencies succeeded; a later claim is refused
at submit, approval or the claim itself (`holdout_already_claimed`). Once the claim is open a
failed attempt (even one reconciled as not started) fails the stage instead of being requeued.
A partition is sealed from the first approval of a spec naming it as a holdout (and once
claimed): a stage that is not a holdout stage for it cannot receive it, and no `artifact` input
may come from a holdout stage. Both are refused at submit and approval and again before each
input release (`sealed_partition`). A holdout stage's outputs cannot be staged as an input
snapshot either. The claim ends `scored` (with the metric
values) or `spent` and is on the receipt (`holdout_claims`); read it with
`tessera_campaign_holdout` or `tessera_campaign_holdout_claims`.

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

## Delegated decisions (ADR 0122)

A person can sign a delegation for a project once: a program, a budget ceiling, a per-run cap, the
providers a stage may use and optional gate rules, valid for at most 30 days. Under it a human, or an
agent enrolled with `campaign:decide_delegated`, may approve a submitted campaign
(`POST /campaigns/{id}/delegated-approval`) or record a gate go or no-go
(`POST /campaigns/{id}/delegated-gate-decision`), but only when the platform finds the written rule met,
computed from recorded data at that moment. The approval is on a receipt that cites the delegation
(class `owner_delegated`); a request outside it returns 409 `outside_delegation` with reason codes,
changes nothing and is listed for the owner (`GET /projects/{id}/delegation-escalations`). Creating and
revoking a delegation is a person's act; the MCP tools only read delegations and use them
(`tessera_list_approval_delegations`, `tessera_get_approval_delegation`, `tessera_delegation_escalations`,
`tessera_delegated_approve_campaign`, `tessera_delegated_gate_decision`). Enroll an agent for it with
`--allow-delegated-decisions`. Contract and limits:
[ADR 0122](decisions/0122-owner-signed-approval-delegations.md).

**Salad review stops under a delegation (ADR 0150).** A delegation may carry an optional
`review_rule`: the review `kinds` it covers (`source_assessment`, `plan`, `source_use`),
`max_attempt_cents` (at most the delegation's per-run cap), `max_duration_seconds`,
`provider_classes` (`salad_gpu`, `salad_cpu`) and `source_use_intents` (program outcomes). Without one,
a spec with a Salad stage is refused `spec_needs_human` as before; with one, every Salad candidate must
sit inside it or the delegated approval is refused with the reason (`review_cap_exceeded`,
`review_time_limit_exceeded`, `provider_class_not_allowed`, `source_use_intent_not_covered`). Nothing
is pre-approved at approval: when the runner first reaches the stage's review stop (after `not_before`
for a deferred campaign) it re-checks the delegation (active, unexpired, signer still live, stage
inside the rule) and records an ordinary review pre-approval bound to the digests it derives from the
approved spec, at the stage's own hold, expiring with the approval or the delegation
(`campaign.review_preapproval_delegated` names the delegation and its terms digest). Each review is
re-checked when it is resolved. Anything outside the delegation, a kind it does not cover, a revoked or
expired delegation, or a signer no longer live leaves the review with a person, with the reason codes
on `campaign.preapproval_declined` and on the campaign view's `salad_review` item
(`delegation_declined`). Provider trust, data-class and licence-label checks still refuse what they
refuse. The receipt's `preapprovals` entry names the delegation id and terms digest. A stage placed on
a Salad alternate is not covered.

**Provider-side cache.** A `public_artifact` source with `cache: true` lets the executing provider keep
the verified bytes by digest: the local worker in its private cache directory, and the Salad runtime only
when the provider supplies persistent storage ([ADR 0116](decisions/0116-provider-fetch-cache.md)). Every
reuse is re-hashed and a bad entry is refetched; cached bytes never go to the control plane. The receipt's
`public_fetch` per-file entry records `cache` (`hit`, `miss`, `unavailable` or `off`) and the byte count,
and the object carries `cache_requested: true` only when a source asked for it.

## The one human approval

A human approves the exact digest and revision
(`tessera campaign approve --campaign-id C --expected-revision N --expected-spec-digest D
--automation-label L`; the owner credential needs the automation label, as for any
decision). Approving by id is enough for a batch client: leave out `--expected-revision`
and the CLI reads the campaign's current revision, and leave out `--idempotency-key` and it
generates a fresh key for each call (printed on stderr); the digest is always stated, so the
human still approves exactly what they reviewed. **One key per approval:** an idempotency key
belongs to one campaign and one request. Replaying it for the same request returns the
earlier answer, and reusing it with a different body or for a different campaign is refused
with HTTP 409 and applies nothing, so a scripted loop that forgets to vary its key fails
loudly instead of leaving campaigns unapproved. An agent's MCP session can never approve: it is
an external-agent credential, which is not a human decision (ADR 0038, ADR 0055). An operator
may run a separate `tessera-mcp --approval-tool` server with a person's own session credential
([ADR 0065](decisions/0065-operator-enabled-mcp-approval-tool.md)). Its
`tessera_approve_campaign` reads the current revision, requires the digest to be named, uses a
fresh idempotency key per call (or the caller's, with `expected_revision`, to replay) and goes
through the same route, so every admission check, the program cap and the one-key-per-approval
rule apply unchanged. Unlock conditions and pre-approvals stay on the CLI.
Approval records the approver, decision class and expiry (at most
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
for an agent, cancel stays submitter-only, and so do the steering tools (budget reduction, release revoke,
pause, resume, concurrency limit, stage cancel, placement steering): for an agent caller they act only on a
campaign that agent itself submitted, while a person (human principal) may cancel or steer any campaign in
their tenant. The v2 spec, receipt, list, snapshot, output, log and budget
tools and their scopes are in [mcp-campaign-client-path.md](mcp-campaign-client-path.md).
Never give campaign scopes to an OpenCode harness credential: the pinned tool catalog would
then block with `catalog_inventory_changed`.

## Approved submissions for the agent loop

A person can record an *approved submission*: a spec digest they reviewed, or a failed campaign to
rerun, with a per-run budget cap and an expiry
([ADR 0143](decisions/0143-agent-loop-submits-owner-approved-campaigns.md)). The agent loop, or a
session with the scope `campaign:submit_cleared`, then turns a pending record into a campaign.
The approver on that campaign is the person who made the record; the agent never approves, widens a
cap or changes the digest. The feature is off by default: until an operator enables it every submit is
refused `feature_disabled`. While a kill-switch marker file exists the runner pauses new submissions. Each
submit, refusal and cancel is recorded and appears in the campaign receipt. `tessera_list_pending_submissions`,
`tessera_read_submission` and `tessera_cancel_submission` show and stop what the loop will do next.

### Running the submit runner

`tessera-submit-runner` calls the submit action on a schedule. It starts only on a host whose operator admitted it
(then with `tessera.target`), and control must also run with `tessera serve --agent-submissions` (otherwise every submit is refused
`feature_disabled`). It runs no model: it only consumes records a person approved.

```
export TESSERA_CONTROL_URL=http://localhost:8080
export TESSERA_AGENT_TOKEN_FILE=/etc/tessera/credentials/submit-runner-agent-token
export TESSERA_PROJECT_ID=example-project
export TESSERA_AGENT_DESTINATION=example-destination
tessera-submit-runner --journal /var/lib/tessera/submit-runner/journal.jsonl \
  --kill-switch /etc/tessera/submit-runner.kill --interval-seconds 60
```

`--journal` and `--kill-switch` are required. `--max-submissions-per-tick`, `--max-attempts-per-tick`
and `--max-ticks` bound the work, and `--once` runs a single tick. The runner refuses to start if
`ANTHROPIC_API_KEY` is set. Exit codes: 0 stopped, 2 configuration refused, 3 halted. The journal is
a private, append-only JSONL log of receipts. While the kill-switch file exists (or its state is ambiguous, e.g. a missing parent directory)
the runner pauses without exiting, calls neither the action nor the control plane, and resumes by
itself when the file is removed; exit 0 comes only from a stop request, and exit 3 after repeated
failed ticks. Campaigns already submitted are untouched. An example unit (starts with the target only where the admission marker exists, token
passed as a credential) is in [`deploy/systemd`](../deploy/systemd/README.md).

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

- `max_exposure_cents` x (`retry.max_attempts` + `retry.infra_retries`) may not exceed 5000
  (the experiment budget ceiling) for any stage.
- v1: at most 32 stages, 256 input edges and 8 inputs per stage. v2: 64 stages, 512 edges,
  8 data inputs per stage plus the stage's `code_input` when it names one (ADR 0121; the code
  input is not counted as a data input or an edge), 1 MiB of canonical JSON, 1..128 typed outputs per stage, and at most
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
- **Placement tags (ADR 0070).** A v2 stage may set `placement_tags` (1 to 8 distinct lowercase
  labels, for example `["heavy"]`). They are part of the approved spec (absent unless set, so
  existing digests are unchanged) and are read only by the operator's placement preference
  ([campaign providers](campaign-providers.md#placement-preference-by-resource-class)), which may
  reorder a stage's approved `alternate_providers` candidates; they name no destination.
- **Heavy-stage placement guard (ADR 0107).** When a heavy stage (an ADR 0088 signal, a
  `pretraining` or gpu tag, large pinned inputs, or large declared output limits; placement ordering
  uses the same signals) is placed on a local or
  self-hosted candidate while a Salad profile is registered for the project, the receipt attempt
  and the `campaign.stage_placed` event carry a `placement.heavy_guard` warning with signals and
  reasons, a `campaign.heavy_stage_local_fallback` event is recorded, and `tessera_get_campaign`
  shows a per-stage `heavy_guard`. The operator may set `placement_preferences.heavy_local_fallback`
  to `refuse`, so the stage waits (`heavy_local_refused`) instead of running locally; the default
  `warn` changes nothing else
  ([campaign providers](campaign-providers.md#heavy-stage-placement-guard)). A local placement
  needs an explicit fallback reason (Salad unavailable, skipped by a check, over a cap, refused by
  trust or licence, an operator decision, or the ADR 0141 record saying why no Salad candidate was
  added); without one the stage fails with `heavy_local_unexplained` and a
  `campaign.heavy_stage_local_refused` event unless the operator set
  `placement_preferences.heavy_unexplained_local: warn` (amendment 2026-11-23b). A stage the guard
  refused before any attempt (that failure, or a refuse-mode wait that ended the stage failed or
  cancelled) carries the refusal on its receipt stage entry as `placement.heavy_guard` (`code`,
  `reasons`, `mode`, `detail`), rebuilt from the recorded event; a Salad candidate refused at submit
  names its structured `cause` (`license_label` with `labels`, `data_class` with `data_class`) on
  the ADR 0141 record and in the guard `detail` (amendment 2026-11-23c). Without a Salad
  profile a heavy local placement records only `no_remote_profile` (as a warning, also in refuse mode); light stages are
  unaffected.
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
- **Infrastructure retries (ADR 0058).** A confirmed exit with a code the spec names as
  infrastructure (`retry.infra_exit_codes`, default 137: the container was killed) is
  retried by the runner itself, up to `retry.infra_retries` (off by default; 0 to 3) extra
  attempts that use no scientific retry; enabling it puts it in the approved digest and in the
  stage's worst-case exposure (and a Salad stage's per-task ceiling), the approval view
  (`infra_retry_policy` on submit and every read) states it per stage, and it applies on the
  confirmed-exit path (ADR 0053) too. Each is a new attempt with its own hold (against
  the campaign cap and project limits), grant, receipt and recorded reason; a task failure,
  a refused step, a policy or budget refusal and any unknown effect are never retried this
  way. `retry.reroute_providers` may name other registered, unbilled worker-grant providers
  of the same runtime for the retry to move to; they are part of the approved spec and
  admitted like the stage's own provider at submit, approval and again at reservation (a
  rerouted attempt drops the stage's `provider_config`). `tessera_campaign_retries` shows the state and
  `tessera_cancel_campaign` stops it.
- **Resource-limit stops (ADR 0067).** A stage that stopped on a resource limit is an
  infrastructure failure: an attempt that succeeded but whose stop metric (listed in
  `retry.resource_limit_metrics`, a metric of the stage) is above zero, a kill at the time limit,
  or an out-of-memory kill. With `retry.infra_retries` enabled the runner retries it on the
  next approved placement candidate (or reroute provider). A stop on the stage's own time
  budget is never repeated on the provider where it happened (the approved spec has no raised
  budget). A kill of a stage with no other approved place is retried in place exactly as ADR 0058
  did; where other places exist it returns to the same provider only when the worker's fresh
  report shows free memory above the limit the stage hit. Otherwise the stage ends as
  `resource_limit_no_provider`. A kill at the time limit is exit 137 and repeats in place within
  `infra_retries`, as in ADR 0058. With the bound spent, a stop metric keeps the recorded result
  and only the `stage_infra_retries_exhausted` event names it. A `resource_limit_metrics` metric
  may not also be in `stop_when`. The memory gate covers every `infra_exit_codes` code, and the
  heartbeat figures are the worker's unverified claim. Every attempt and its reason are on the receipt.
- **Plain-words report (ADR 0115).** `tessera campaign report --campaign-id ID` (or `--project-id P
  --since T --until T` for a window) prints what a campaign did in sentences: per stage what ran,
  where, what it cost, how long it took, the hosts it reached, the data that went in and out and
  how it ended, with no internal ids unless `--detail`. `GET /campaigns/{id}/report`,
  `GET /projects/{id}/campaign-report` and the MCP tool `tessera_campaign_report` give the same
  as data plus `text`. Figures that are not recorded yet are said so, and model-call spend is the
  project's for the period, not attributed to the campaign.
- **Confirmed exits (ADR 0053).** An `admitted`, `running` or `unknown` attempt whose worker
  confirmed the exit, with no contradicting observation, is resolved by the runner on its next tick:
  the grant and its slot are released and the cost recorded (never above the hold), the attempt becomes
  `failed_transient` / `exit_confirmed_unresolved` (or the declared or undeclared exit-code class when a
  run receipt records one) and the stage's retry policy decides what follows. Only a non-billable
  worker-grant provider is resolved this way. An `admitted` or `running` attempt waits while a run
  receipt appears, its lease is live or a large-artifact upload is in progress (until the grant
  expires), and 120 s after control recorded the exit, so a late receipt is not overtaken.
  Whatever its status, an attempt is also held while its outputs are still arriving, even after
  its grant has expired ([ADR 0064](decisions/0064-hold-an-exited-attempt-while-outputs-upload.md)):
  an open large upload that gained bytes within the last 10 minutes, or an upload or artifact that
  finished within 120 s. It is closed once those finish, stall or expire, and in any case at most
  2 hours after the exit was recorded once its grant has expired, and never past the approval window
  (a trickle of bytes cannot hold a slot for the upload's own 24 hours).
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

A v2 spec may carry `not_before` (UTC, `YYYY-MM-DDTHH:MM:SSZ`): nothing runs earlier. After
approval the campaign stays `running` and reads as `waiting` with reason `not_before` (the
`waiting` key of the campaign read, the plain report and the MCP reads, with `seconds_remaining`);
it is not failed or stuck. The approval's expiry (`deadline_seconds` from the first approval)
still bounds the wait, so `deadline_seconds` must cover the wait plus the run, and approve is
refused when `not_before` is not before that expiry. A delegated approval makes the same comparison and also
compares `not_before` with the delegation's own expiry: past either, it is not recorded (409 `outside_delegation`
with `not_before_after_approval_expiry` and/or `not_before_after_delegation_expiry`), the campaign stays
at its normal approval stop and the owner is asked. Recurring templates are a follow-up.

## Window requirement

A stage is refused before any grant or hold (failure class `deadline_too_short`) when the
time left is shorter than its `max_duration_seconds` plus a 300 s settle margin. The time
left is the earliest of the campaign approval's expiry and the expiry of the approving
human's and the stage worker's credentials. A stage of the maximum 3600 s therefore needs
at least 65 minutes (3900 s) of approval and credential life when it is dispatched; a
one-hour credential can never start it. Each retry attempt is checked again when it is
dispatched. Re-approval does not extend the campaign's total wall-clock bound. The receipt records
the bound (`wall_clock`: `deadline_seconds`, `first_approved_at`, `bound_at`, the number of
`approvals`, and `extended_by_reapproval: false`); it is absent for a campaign that was never
approved.

## Receipt

`GET /campaigns/{id}/receipt` (404 until terminal) returns an immutable receipt with
`campaign_id`, `spec_digest`, the approval, status and stop reason, the budget ledger
(cap, held, settled), per-stage attempts (grant, admitted attempt, operation, run receipt
digest, exit code, manifest id and digest, holds and settlements), inputs with data
class, measurements derived by the platform from retained bytes, stop-rule results,
lineage node and edge ids, `verdict {operational, scientific_verdict}` and
`receipt_digest`, plus `chained_inputs` when an input was resolved from a predecessor's receipt
(ADR 0080). `scientific_verdict` is `pending_human_approval` or `not_assessed`.
`pending_human_approval` there means a stage is bound to a program or gate, so the scientific
verdict is left to a person. What a campaign is waiting on *now* is shown live in the campaign view
(`GET /campaigns/{id}`, `tessera_get_campaign`) as `pending_human_approval`, a list derived from
recorded state: `campaign_approval` (submitted, awaiting a person's approval of its spec digest),
`campaign_reapproval` (its approval lapsed), and `salad_review` (a Salad attempt's pending plan or
source-use review, with the stage, attempt and decision request). Each item names what is awaited,
and it disappears the moment the decision is made, refused or expired, or the campaign ends; an
empty list means nothing awaits a person.
Do not report a campaign as run without reading its receipt.

**Comparing arms (ADR 0131).** `GET /campaigns/{id}/comparison` (404 until terminal) compares
stages of the receipt as a baseline and one or more interventions. Query parameters:
`baseline` (a stage key), `intervention` (a stage key, repeatable, at most 8), `metric` (a
measurement name, repeatable, at most 16) and `evaluation_input` (an input name, repeatable, at
least one). Each arm must have exactly one succeeded attempt, and each named evaluation input must
have the same identity (sha256, or the snapshot identity) in every arm. The check is not limited to
what the caller names: it also reads the approved spec the receipt's digest names, and the arms must
declare the same `gate_evidence.split_lock_id` and consume the same held-out input
(`evaluation.holdout.input`) with the same identity. The result gives each
metric's baseline value and each intervention's value and delta (intervention minus baseline),
with the receipt digests, and no decision or verdict (`scientific_validity` is `not_assessed`).
Refusals are 409 (`comparison_refused:<code>`), one stable reason code each (missing or over-long query lists are a 422): `arms_not_distinct`, `too_many_arms`,
`too_many_metrics`, `no_intervention`, `no_metric`, `stage_missing`, `arm_attempt_not_unique`,
`evaluation_inputs_unnamed`, `evaluation_input_missing`, `evaluation_inputs_differ`,
`split_lock_differs`, `spec_unavailable`, `metric_not_recorded` or `metric_not_numeric`. See
[ADR 0131](decisions/0131-campaign-result-baseline-vs-intervention-comparison.md).

**Selecting an arm on validation data (ADR 0133).** The same route has a selection mode, on when
`select_by` is given. Metrics are split by role: `validation_metric` and `validation_input` for
validation data, and `test_metric` and `test_input` for held-out data. `select_by` names one
validation metric and `select_direction` is `max` or `min`. The best intervention by that metric
is the selected arm; the baseline is the reference and is never a candidate. Validation metrics are
reported for every arm, test metrics only for the baseline and the selected arm, and each metric
item carries its `split_role`. The plain `metric` parameter is refused in this mode. The ADR 0131
checks still apply: every arm must declare the same split lock, and a held-out input the approved
spec declares (`evaluation.holdout.input`) is always a test-role input, checked across arms even
when unnamed and refused (`validation_equals_test`) if named as validation data. Refusals are
409 with a reason code: `split_role_missing`, `split_role_conflict`, `selection_reads_test_metric`,
`selection_direction_missing`, `validation_equals_test` and `selection_tie` (two interventions tie
for the best value). Selection is read-only and records nothing; it is not a decision or a gate.
See [ADR 0133](decisions/0133-campaign-comparison-selection-split-guard.md).

## Code as an input

A stage's own code is a content-addressed input, not part of an image
([ADR 0095](decisions/0095-stage-code-as-input-artifacts.md)). A v2 stage may name one of its
`snapshot` inputs as `code_input`: the snapshot holding the stage's code, pinned by digest and mounted by
the usual input path (`/inputs`, or `/inputs/<name>/`) into a generic runtime image that holds only
dependencies. Submit and approval refuse a `code_input` that is not a snapshot input of the stage, is
empty, or carries a code approval that no longer matches its files. The receipt's stage entry then has a
`code` block (input name, snapshot id and digest, `code_digest`, the file count (an integer; file
paths never appear on receipts), whether a valid human code approval covers it, and the image
digest) and each attempt a `code_transfer` block (provider, worker, code digest, image digest,
trust policy digest, admission basis, `granted`, and the approval state recorded at grant time).
The runner writes one `campaign.code_granted` event per attempt, in the same transaction as the
grant, with ids and digests only; a grant does not prove the worker fetched the code. This does not approve code (a
human still does, and a composed snapshot loses an approval), it builds no image, and a stage without
`code_input` is unchanged.

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
  admission transaction. A forward re-approval of a lapsed (`paused_delegation_lapsed`)
  campaign re-evaluates the first approval's requirements: a human re-approval restates the
  first `unlock` exactly (matched by requirement digest; a missing, changed, added or
  stricter set is refused with 409), a standing-started campaign reuses its standing
  approval's stored `unlock`, and every re-evaluation is recorded on the receipt. An
  `unlock` on a re-approval of a campaign first approved without one, or on a cleanup
  re-approval, is refused (409), never ignored. Refusals read
  `unlock_not_satisfied:<index>:<code>`.
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
  it. A forward re-approval of a lapsed campaign re-checks it without counting the campaign
  twice (its cap is already committed, so the program must still admit the existing
  commitment) and is refused when it no longer fits. Cleanup re-approvals are not checked.
  It never replaces the guardian or campaign caps.
- A receipt gains an `unlock` section when admission was conditional, with one entry per
  requirement per admission (first approval and each forward re-approval), oldest first.

## Input releases of idle campaigns

A campaign's runner holds one active release per input snapshot and worker for its
attempts, and while it is active a second campaign on the same snapshot is refused with
`step_refused`. When a campaign becomes terminal (`succeeded`, `stopped`, `failed`,
`cancelled`), blocked or paused, every release it used is revoked in the same transaction as
that state change, provided nothing still needs it: no attempt of any campaign that uses it is
in flight, `unknown` or unsettled, and no execution grant issued against it is unresolved.
Each revocation writes a `campaign.release_freed` event (`released_by: system`, `trigger`,
`reason` = the campaign status, `campaign_id`, `snapshot_id`, `snapshot_digest`,
`release_id`). A blocked campaign whose last live attempt settles later, and a campaign that
went idle before this rule existed, are freed by a bounded reconcile sweep on every runner
tick (so at the first tick after a restart). While the holder still has a live attempt the
refusal stays, and names the holding campaign and the release expiry to the same
organization only (another organization learns only that the snapshot is held and until
when). A person's own release, which no campaign attempt used, is never revoked by the runner.
A release whose `expires_at` has passed is never treated as active, whoever holds it: it
does not refuse a new campaign, and the new release revokes it in the same transaction with a
`workspace.snapshot_release_expired` event (`release_id`, `expired_at`, `replaced_by`).

## Not implemented

Bound gate outcomes without human-entered metric records (a human
records every metric), a program budget recorded on events or re-read after startup;
multi-host runners; dynamic fan-out; more than 32
stages in a v1 spec (v2 allows 64) or approvals past 7 days. Unexpired human releases of the same snapshot to the same worker
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

(A local-docker CPU stage can also run on a Salad CPU profile without a Salad-form copy of the
stage: see [Adapting a local-docker stage](campaign-providers.md#adapting-a-local-docker-cpu-stage-to-salad).)

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

**CPU-only profiles ([ADR 0062](decisions/0062-salad-cpu-only-profiles.md)).** A profile with
no `gpu_class` and a `cpu_rate` (`vcpu_hour_usd`, `memory_gb_hour_usd`: the operator's rates in
USD per hour) places a stage on a Salad container with no GPU, for a simple baseline that does
not need one. A stage declares `salad.gpu: false` to use one; the default (`true`) is a GPU
stage, and a stage and profile of different kinds are refused with 409 at submit and approval,
so a CPU stage never takes a GPU and a GPU stage never lands on a CPU container. The route
renders no GPU field. Its cost is estimated from the two rates, which are part of the reviewed
route (an estimate input, never invoice proof, labelled `operator_cpu_rate_not_actual_cost`).
The operator's per-run cap is enforced at admission: the profile's `max_run_cents` bounds the
stage's `max_exposure_cents`, and the worst case of the whole `max_duration_seconds` at the
rates must fit that hold or the stage is refused (409); the guardian then enforces the same
cap while the run is billed. Salad publishes no CPU availability read, so a CPU preflight
reports `available: true` as a **constant**: it is not evidence that Salad has CPU capacity, and
must not be read as one. The preflight checks the replica quota only; allocation is left to Salad
(a stalled one ends at the route deadline). The provider's CPU-only group shape is untested live.

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
  `guardian_final_unavailable` when it cannot read it; the final status is worker-attested (ADR
  0038 amendment, trust boundary), and it supplies only the cost estimate: the verdict for a
  bounded-hold settlement is control's own read of the guardian's per-frame record
  (`--salad-guardian-read`, ADR 0041). Without that read no Salad stage settles on the bounded
  hold; it waits for a human charge. The receipt shows
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
and profile, the worker credential file (which also authorizes the executor's reduce-only cancel of
its own routes, [ADR 0075](decisions/0075-executor-worker-credential-cancels-own-route.md); no human
session credential is needed), the credential directory, guardian socket
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

When the control service refuses a worker step, it records one `campaign.stage_rejection` event on
the attempt's stage. This is provider-generic: the worker-facing control routes that every
provider's worker uses (lease acquire, lease renewal, lease package and observation report) record
through one shared helper, `stage_rejections.recording`, so a new provider or worker gets it without
a change to the runner core. The Salad executor's steps are covered the same way (grant poll, lease,
lease package, record package, renewal, execute, mark executing), as is `observe` (observation
report). The event holds only a short stable code, the step, the attempt number and the time: no
message text, host, address, token or payload. The same attempt, step and code is not recorded again
within 10 minutes, and a stage keeps at most 20 such events. Recording only records: the response and
status are unchanged, and a recording failure never alters the refusal. A grant that belongs to no
campaign attempt, or a worker that does not own the grant, records nothing. A worker that names
another worker's grant (lease acquire) or lease (renewal, package, observation report) gets HTTP 404,
the same response as for an id that does not exist, so the refusal does not reveal that it exists.
Only those worker steps record: a refusal of any other worker route (an external-operation
receipt, an output upload, a heartbeat) is not recorded.
`tessera_campaign_stage_status` shows it per stage as `rejections` (`latest`: attempt, step, code,
`at`, `age_seconds`; and `counts` by code). Codes: `campaign_not_running`, `lease_already_held`,
`worker_authority_expiring`, `stale_heartbeat`, `stale_epoch`, `lease_expired`,
`capability_required`, `worker_unauthorized`, `refused_conflict`, `refused_forbidden`,
`refused_not_found`, `refused`, and `unexpected_error` (anything that is not a store refusal; only
the fact is recorded, never the text).

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

## Owner-run helpers carry their automation identity

Every `tessera` command that acts with the owner credential (campaign submit, approve, cancel and
standing approvals, provider-trust changes) sends `X-Tessera-Automation` itself: the
`--automation-label` flag, else `TESSERA_AUTOMATION_LABEL`, else `tessera-cli:<command>`. A helper
that acts under an owner delegation also names it with `--delegation-ref` or
`TESSERA_DELEGATION_REF`, sent as `X-Tessera-Delegation`; the reference is never invented, and the
server refuses it without an automation label. Both are labels only and grant nothing. The decision
event and the campaign receipt's `approval` record them (`automation_label`, `delegation_ref`).
Scripts that call the API directly can build the headers with
`tessera_control.owner_identity.owner_request_headers`. The dataset snapshot upload route fails
closed: a bare owner credential with no label is refused (a `human_session` needs none), and the
adoption event records credential kind, decision class, label and delegation reference.
