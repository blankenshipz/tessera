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
composed snapshot. A `kind: stage` input is bound when the dependent stage starts
([ADR 0140](decisions/0140-late-bound-stage-output-inputs.md)): the runner resolves each
referenced output to the sha256 and size the upstream stage's succeeded, settled attempt
recorded, checks it against the custody manifest, re-checks the provider trust setting
against the resolved data class and inherited licence labels, and pins those digests for
composition. A refusal is a closed code (`upstream_missing`, `upstream_not_succeeded`,
`upstream_not_settled`, `output_missing`, `digest_mismatch`, `data_class_refused`,
`license_refused`, `campaign_not_found`) recorded as `campaign.stage_input_bind_refused`; the
stage fails as `late_bind_refused:<code>`. The binding is the event
`campaign.stage_inputs_bound` and the receipt's `input.late_bound_inputs` (per input: source stage,
output, digest, bytes, data class, labels, and `bound_at`, the binding event's timestamp). A
refused binding shows on the dependent stage's receipt as one `input_not_pinned` entry
(`status`, `code` = the refusal code, `input`, `source_stage`; never the free-text reason),
appended after any entries the stage bound before the refusal was recorded; a refused Salad alternate records the same event, so it shows too. Providers read it through
`RunnerServices.bound_inputs` and receive resolved digests only. Submitting grants nothing and spends nothing. A v2 stage may also pin
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

A v2 spec may also seal data for a role inside the campaign (`seals`,
[ADR 0164](decisions/0164-sealed-role-inputs.md)). Each seal has a `name`, a `role` and either a
`digest` (any `snapshot` or `artifact` input pinned to it, in any stage) or a `stage` and `output`
of the spec. A stage that mounts sealed data, or an output derived from it at any depth, must list
the role in its `roles`, or the spec is refused when parsed (submit, validate, approval and every
runner read); the producing stage of a sealed output needs no role. Each stage receipt entry lists
what it read under a seal (`sealed_access`: seal, role, input, `direct` or `lineage` with
`from_stage`). A re-run plan keeps the seals of the outputs it pins. Seals are a per-spec rule;
combine them with a `holdout` claim when a score must also be one-time.

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
changes nothing and is listed for the owner (`GET /projects/{id}/delegation-escalations`). An agent
that makes a delegated approval gets the approval summary (id, spec digest, class, times, delegation,
status) without `approving_principal_id`, as in the live campaign view; a person gets the full row. Creating and
revoking a delegation is a person's act; the MCP tools only read delegations and use them
(`tessera_list_approval_delegations`, `tessera_get_approval_delegation`, `tessera_delegation_escalations`,
`tessera_delegated_approve_campaign`, `tessera_delegated_gate_decision`). Enroll an agent for it with
`--allow-delegated-decisions`. Contract and limits:
An agent reading delegations or their decisions through these tools never sees who signed or revoked a delegation (`created_by_principal_id`, `created_credential_id`, `revoked_by_principal_id`) or which person acted (`actor_principal_id`, shown only when it is the reading agent itself); a person reading them gets the full rows. [ADR 0122](decisions/0122-owner-signed-approval-delegations.md).

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
and the object carries `cache_requested: true` only when a source asked for it. A cache entry is a copy
(never a link) kept in the cache directory outside the work tree, so a pinned file with `cache: true` may
occupy up to twice its size on the provider's disk (the verified file and its cache copy); the Salad
adapter's `disk_mb` check counts the pinned bytes once, so leave room for that.

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

The agent view of a campaign (`tessera_get_campaign`) shows the approvals but omits
`approving_principal_id`; only the owner view carries it.

A re-approval of a `cancel_requested` or `blocked` campaign installs a live delegation for
reduce-only cleanup only and never resumes forward work, so a lapsed approver cannot strand a cancel.

Agents with the external scopes `campaign:submit|read|cancel` may submit, read and cancel
(`tessera_submit_campaign`, `tessera_get_campaign`, `tessera_cancel_campaign`, enabled with
`tessera-mcp`: registered by default only when the credential's live scopes include
`campaign:*`, forced with `--campaign-tools` or `--no-campaign-tools`). They cannot approve.
`campaign:read` covers only the campaigns the agent submitted (audit O-05; the recorded
submitter is the caller, or the caller consumed the owner's approved submission that started
it): another agent's or a person's campaign answers the same 404 as a missing id, and
project-wide lists, counts and pages hold only the caller's rows (details in
[mcp-campaign-client-path.md](mcp-campaign-client-path.md#scope-semantics)). A person still reads
every campaign of their organization. For an agent, cancel stays submitter-only, and so do the steering tools (budget reduction, release revoke,
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
  A placement skip for a licence label or data class also carries additive `cause` fields on the
  `campaign.stage_placed` / waiting `skipped` entry, the receipt `placement` block and the guard
  `detail`: `cause: license_label` with `labels`, or `cause: data_class` with `data_class`. The reason
  code and message are unchanged. A third-party provider (Salad) receives an explicitly labelled
  input (or derived data that inherits a label, within a campaign or through a recorded output of an
  earlier one) only when its trust entry's `allowed_license_labels` names that label; its entry
  without the key admits unlabelled data only, so a spec's `forbid_providers` list is no longer the
  only thing keeping such data off it (operator-owned providers listed without the key are unchanged) ([licence labels](license-labels.md), ADR 0113 amendment 2026-11-23).
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
  (`infra_retry_policy` on submit and every read) states it per stage, together with
  `max_reallocation_retries` (ADR 0179, shown even at its default of 2, which the stored spec
  omits) and a `worst_case_attempts` of `max_attempts + infra_retries + max_reallocation_retries` (plus `max_class_fallbacks` when set),
  and it applies on the
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
- **Exited without a report (ADR 0158, proposed).** When the executor's exit observation never
  arrives, an `admitted` or `running` attempt on a non-billable worker-grant provider is closed
  from the stage watcher's `failed` report only if that report carries the closed `exit` field (a
  definitive container exit with an exit code; never an unknown, absent or deadline failure), for
  the operation's own lease and epoch. The close also needs every lease of the grant expired, no
  confirmed exit, no run receipt, no later observation and no upload still arriving. It waits
  `exit_report_grace_seconds` (default 300 s, at most 900 s) after control recorded the report. The
  attempt becomes `failed_transient` / `exited_without_report` with an exit class (`killed`, `oom`,
  `error`), settled at zero, with its grant and slot released, and the stage's retry policy decides
  what follows. The watcher's code never makes a failure permanent. The receipt's `exit_resolved`
  names the closed reason, the exit class and the watch event.
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
- **Attempts with no claimed provider create (ADR 0157).** An unknown attempt whose provider
  proves from control's own records that it never claimed a create or start is reconciled by
  the runner itself. The proof comes from the provider's optional `create_claim_state` hook
  (Salad: exactly one packaged operation, no `salad_proxy.claim` create or start for it, no
  route that wants to run or records a create or start effect, and a Salad record with no
  provider id, receipt, evidence or charge); a provider without the hook is never reconciled
  this way. The attempt must also show no exit, receipt or manifest, and its grant no provider
  id, running, exited or terminated observation, receipt or dispatch. The runner revokes the
  grant through its reduce-only step if it is still active, then in one transaction reconciles
  it at zero with the operator route's accounting (its hold released), settles the attempt
  `failed_transient`/`not_started` at zero (`unclaimed_zero`) and re-queues the stage under its
  own caps (an attempt number, no scientific retry). It records
  `execution.grant_reconciled_unclaimed` (`automatic: true`) and the receipt event
  `campaign.attempt_unclaimed_reconciled` (attempt id, proof code, evidence digest). Any failed
  guard keeps the human path and records `campaign.unclaimed_autoreconcile_refused` with a
  closed reason and a readable sentence. At most `--campaign-unclaimed-autoreconcile-limit`
  (default 2, 0 disables) such reconciles per campaign; past it the attempt stays `unknown`
  with `campaign.autoreconcile_stopped` (`kind: unclaimed`). The operator route
  `reconcile-unclaimed-salad` is unchanged and still human-only.
- **Owner-requested unclaimed recovery (ADR 0205 amendment).** An attempt that is `unknown`
  past that limit, or one left `unknown` by an earlier build, keeps its campaign `blocked`.
  The owner recovers it with
  `tessera campaign reconcile-unclaimed-attempts --campaign-id ID [--campaign-id ID2]
  [--dry-run] [--reason TEXT] --owner-header FILE` (the owner's human control credential; an
  automation label is allowed; agents and workers are refused). Over HTTP it is
  `GET /campaigns/{id}/unclaimed-attempts` for the dry run and
  `POST /campaigns/{id}/reconcile-unclaimed-attempts` with `{"reason": "...", "dry_run": false}`
  and an `Idempotency-Key`. Run `--dry-run` first. For each `unknown` attempt it lists the
  runner's own verdict: `reconcilable` (with its proof and evidence digest), `requested`, or
  `refused` with the closed reason (for example `create_claimed`). A create refused at the trusted
  server's use-time recheck is `reconcilable` even when an earlier build (v0.102.0) wrote no
  `salad_proxy.claim_refused`, provided its receipt is the bare `admission_denied` refusal and
  nothing records a group identity, allocation or other sign of a provider call (ADR 0205
  amendment, point 6). The real request writes one
  `campaign.unclaimed_reconcile_requested` per reconcilable attempt, bound to that evidence
  digest, and settles nothing itself. On its next pass the runner applies the unwind above to
  exactly those attempts, regardless of the per-campaign limit, and re-reads every guard. The grant
  is reconciled at 0 with its hold released, the attempt becomes `failed_transient` /
  `not_started` (`unclaimed_zero`), the stage is re-queued without a scientific retry, and a
  `blocked` campaign with no other unknown attempt returns to `running`. The events carry
  `automatic: false` and `requested_by_principal_id`. If the evidence changes, the request no
  longer applies. A request that finds nothing reconcilable refuses with each attempt's reason
  and writes nothing, and a repeat does nothing. Do not use `reconcile-unclaimed-salad` on its
  own for such an attempt: it releases the hold, but the runner's blocked pass then settles the
  attempt `failed_permanent`, which ends the stage. Never record an invented termination or a
  token charge to unblock it.
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
- **Completion-cancel recovery (ADR 0075 amendment).** The Salad live check's own stop of a
  verified pass (`cancel_cause.class` exactly `execution_complete`) no longer blocks the
  attempt's success. An attempt an earlier build failed for it (`failed_permanent` /
  `step_refused`, "Execution success cannot override a requested cancellation.") is recovered
  with `tessera campaign reconcile-completion-cancels --campaign-id ID [--campaign-id ID2]
  [--dry-run] --owner-header FILE` (the owner's control credential; an automation label is
  allowed). It re-checks the evidence, re-hashes each output from custody, registers the
  official output rows, moves the attempt and stage (and the campaign, once every stage has
  succeeded) to `succeeded`, and appends one sealed `campaign.completion_cancel_recovered`
  event bound to the immutable terminal receipt, which an `artifact` input then resolves
  through. It never charges, never changes a settlement, refuses anything else (a real
  cancel, a missing or mismatched digest, a receipt that is not a verified pass) and is a
  no-op on a second run.
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
Every read of a stored receipt (this endpoint and the MCP receipt tool, the campaign and window
reports, `artifact`/`receipt_output`/`image_from` resolution and measurement gates) recomputes
`receipt_digest` over the stored body and refuses on a mismatch with the stable code
`receipt_digest_mismatch` (409; a measurement gate refuses with `measurement_receipt_digest_mismatch`),
returning nothing from the stored body.
`pending_human_approval` there means a stage is bound to a program or gate, so the scientific
verdict is left to a person. What a campaign is waiting on *now* is shown live in the campaign view
(`GET /campaigns/{id}`, `tessera_get_campaign`; the human-approval surface of audit G-11, not the receipt) as `pending_human_approval`, a list derived from
recorded state: `campaign_approval` (submitted, awaiting a person's approval of its spec digest),
`campaign_reapproval` (its approval lapsed), and `salad_review` (a Salad attempt's pending plan or
source-use review, with the stage, attempt and decision request). Each item names what is awaited,
and it disappears the moment the decision is made, refused or expired, or the campaign ends; an
empty list means nothing awaits a person.

The stored receipt is the owner's audit record and a person reads it whole. An external-scope
agent credential (the receipt route, `tessera_get_campaign_receipt`,
`tessera_campaign_wave_receipts`) reads a marked projection instead (`"view": "agent_redacted"`
on the response and the receipt body, and `redacted`, the field paths removed), the same
approver-hiding rule as the live campaign view and the gate decisions: it drops
`approval.approving_principal_id` and `approval.smoke_grant_id`, each pre-approval's `id`,
`source_digest`, `plan_digest` and `source_use_digest`, each delegated resolution's `subject_id`
(the decision request or source) and `review_digest`, and the submission section's
`approved_by_principal_id` and per-decision `actor_principal_id`. It keeps the spec digest, the
approval's id, class and expiry, the delegation, and each pre-approval's stage, caps, expiry and
resolved kinds, attempts and cents. Its `receipt_digest` is the stored receipt's digest, naming
the record it is a view of; the projection itself is not digest-verifiable. Chaining
(`receipt_output`), gate and unlock evaluation and the plain-words report read the stored receipt.
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
`split_lock_differs`, `spec_unavailable`, `spec_digest_mismatch` (the stored spec no longer hashes
to the digest the receipt names), `metric_not_recorded` or `metric_not_numeric`. See
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

### Runtime image provenance

When an attempt is granted, the runner writes one `campaign.image_provenance` event (in the grant's
transaction) and each attempt's receipt entry gains an `image_provenance` block with a closed set of
fields: `image_ref` (the image name without its digest), `image_digest` (`sha256:...`, the immutable
reference the stage was pinned to), `base_images` (a list of `{ref, digest}` taken from an image-build
stage's approved plan; empty for any other stage), `compressed_size_bytes` (the total compressed layer
size read from the registry manifest) and `size_reason`. When the size cannot be read,
`compressed_size_bytes` is `null` and `size_reason` is one of `adapter_cannot_read_manifest` (the
provider has no manifest reader) or `size_unavailable` (the reader returned nothing usable or failed);
it is `null` when a size is recorded. Reasons are a closed list, never free text, and a reference with a
scheme, user information or whitespace is recorded as `null`: a reference containing `@` is accepted
only when exactly one `@` is followed by a well-formed `sha256:` digest, so `user:pw@host/img` records
neither a name nor a digest. The check is
syntactic: a bare `user:pw@sha256:<hex>` has the same shape as `img:tag@sha256:<hex>`, so it is recorded as the
name `user:pw` (read as a tag) and cannot be told apart from an image tag; never put a secret in an image reference. Digest pinning is unchanged: a tag-only
reference is still refused wherever the grant or spec requires a digest. A provider may offer an optional
`image_pull_size_bytes(stage)` hook; the Salad provider takes an operator-supplied manifest reader and
reports no size without one. The runner waits at most 10 seconds for that read, outside the database
transaction; a read still running then is abandoned and recorded as `size_unavailable`. Attempts dispatched before this record existed have no block.

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
tick (so at the first tick after a restart); it handles at most 50 campaigns per tick and
resumes where it left off, wrapping around, so every idle
holder is eventually reached. While the holder still has a live attempt the
refusal stays, and names the holding campaign and the release expiry to the same
organization only (another organization learns only that the snapshot is held and until
when). A person's own release, which no campaign attempt used, is never revoked by the runner. A steering-paused campaign is treated the same way (ADR 0167); when it resumes it waits for the snapshot in first-come order rather than failing.
A release whose `expires_at` has passed is never treated as active, whoever holds it: it
does not refuse a new campaign, and the new release revokes it in the same transaction with a
`workspace.snapshot_release_expired` event (`release_id`, `expired_at`, `replaced_by`).

## Capacity held by stuck campaigns (ADR 0153)

A `blocked` or `paused_delegation_lapsed` campaign issues nothing until a person acts, so it
stops holding capacity it provably is not using:

- An expired grant of a `granted` or `admitted` attempt that no worker ever leased and that has
  no operation can never run (the store refuses a lease on an expired grant). The runner
  reconciles it at zero, as the expired-unattempted reconcile route and a running campaign's
  own step do: the grant is reconciled, the attempt ends `failed_transient`
  (`t1_grant_expired_unleased`) settled at zero, and the project concurrency slot it held is
  free. The not-started evidence of an `unknown` attempt (ADR 0046) is applied to a paused
  campaign as to a blocked one.
- For a stage with placement candidates, an attempt placed on a provider but never granted
  (no `issue_grant` intent) stops counting against that provider's free slots while its
  campaign is blocked, paused or terminal. It keeps its state and counts again on resume.

- A grant a worker leased (or that has an operation) is driven only through the evidence
  paths a running campaign uses, read from control-plane records: once the grant has expired,
  an `admitted` or `running` attempt on a worker-grant provider is classified as the running
  flow's observation would classify it (`unknown` for an expired grant with no confirmed
  exit, a missing receipt or a missing manifest); a confirmed exit is then resolved (ADR 0053)
  and not-started evidence applied (ADR 0046), each with its own accounting. Nothing is
  started, cancelled or re-run, and no lease is issued.

A live (unexpired) grant keeps its slot and is not touched: the run may still be going. An
expired leased grant that no evidence settles keeps its slot, hold and exposure until a person
reconciles it, and gets one `campaign.slot_held` event per attempt per spell (`stage_key`,
`attempt`, `attempt_id`, `attempt_status`, `provider_id`, `grant_id`, `campaign_status`,
`trigger`, `reason`: `leased_grant_unconfirmed`, or `exited_awaiting_collection` for an exit
with a run receipt that the campaign collects on resume, an `evidence` summary of leases,
operations, the latest observation and the receipt, and the `action` a person takes). A
resumed campaign gets new capacity only through the normal `issue_grant` checks, so it waits
rather than running beside a newer campaign. Each freed counter writes a `campaign.slot_freed`
event (`stage_key`, `attempt`, `attempt_id`, `provider_id`, `reason`:
`expired_unclaimed_grant`, `not_started_evidence`, `confirmed_exit` or `placed_not_granted`,
`campaign_status`, `trigger`, and `grant_id` for a grant); the queue event is written once per
spell out of `running`, at the transition or by the runner's reconcile sweep. No spend beyond
the zero settlement of a grant that never ran or exited on a free worker.

Termination evidence (ADR 0160): when the development poller has stopped driving a grant
without a confirmed exit, it keeps looking at the exact container and reports, as the worker
that held the lease, an exit with its code, or `termination_evidence: container_absent` once the
container id control recorded for that lease is gone and the lease has ended. The Salad executor
reports the guardian's confirmed stop of the route's exact group
(`termination_evidence: provider_group_stopped`). The paths above then settle the grant; any
doubt (no recorded identity, another container, a container that may still run, Docker not
answering) holds it, with the reason in the grant's local `termination-evidence.json`.

A paused campaign (steering pause or delegation lapsed) also gives back a grant no worker ever
took, even a live one (ADR 0167): no lease row, no operation, no recorded attempt operation, a
registered worker-grant provider and no holdout. The attempt is settled at zero as
`not_started` and consumes no scientific retry. Resuming places a new attempt that waits for a
project slot in first-come order and records `campaign.slot_reacquired` when it gets one. The
line counts any earlier waiter for the project, including one that is itself stuck on a
provider slot, so such a waiter can delay later attempts for other providers. A pause between
grant issue and the worker's lease voids that attempt (amends ADR 0061: only a leased attempt
is in flight).

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

**Alternate profiles ([ADR 0151](decisions/0151-salad-alternate-execution-profiles.md)).** The
`salad` block may add `alternate_profiles`: one to four further operator profile names, distinct
and not the stage's own `profile`, in the order the execution plane may move to them, only before
any possible start ([ADR 0169](decisions/0169-salad-profile-fallback-policy.md): an attempt proven
never started that settled at zero, at most `retry.max_class_fallbacks` times, which must be at
least 1 when alternates are listed; never after a run that may have started or been billed). Absent
(the default) leaves the spec, and its digest, exactly as before. Submit and approval check every
listed profile exactly as the stage's own: registered and of this organization and project, the
stage's worker, the same GPU or CPU kind as `salad.gpu`, the image digest in its allowlist, the
stage's unchanged `max_exposure_cents` within its `max_run_cents` (and the CPU worst case within
it), and the worst-case hold within its `max_task_cents`; a refusal (409) names the alternate.
The approved spec digest binds the list, so approving the spec approves every listed profile up
front and a profile not listed is never used. A review pre-approval of such a stage must cover
every listed profile: its `plan_digest` is the stage's own profile's, and `alternate_profiles:
[{"profile": NAME, "plan_digest": ...}]` names each alternate's (the plan's compute window
depends on the profile; the source-assessment and source-use digests do not). An entry that omits
a listed profile, or names one the spec does not list, is refused at approval with the profile
named; an attempt on a profile the entry does not cover waits for a person (`wrong_profile`).
`tessera campaign preapproval-digests` prints the per-profile digests with the others.

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
window makes the attempt `unknown` and the stage `blocked_unknown` (the route window counts from the
campaign's latest unblock or approval if that is later, and a never-sent route is released
instead; see the executor section below). Success needs a
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

`max_concurrent_paid_routes` (an integer from 1 to 4, default 1; anything else, including a
boolean, a string or null, is refused when the config loads) bounds how many live-check
children the executor drives at once. At 1 the executor is serial: each started route's child
runs to completion inside the pass that started it. Above 1, a pass starts a child on its own
thread and returns; later passes finish each child that returned with the same import,
upload, exit report, guardian final status and outcome, in attempt order. All executor state
stays on the pass's thread: a route's thread only waits for its child, and the child renews
its own lease as it always has. A further route starts only when every bound holds: fewer
routes in flight (plus attempts an earlier process left `executing`) than the configured
value, no route has ended without a receipt since the in-flight set was last empty, and the
guardian's own `max_open_billable_frames`, read from an in-flight route's guardian status,
admits one more frame. Otherwise the route waits (`route_deferred`, with the reason): it stays
`route_started` with its lease renewed, is not marked executing and has no route directory,
so waiting is not a failure and nothing is retried. An unreadable guardian status, or one
without the limit (a route the guardian has not reserved yet), waits too. The guardian still
decides at reserve: a `single_operation_orphan_exposure` refusal inside a started child (a
frame the executor cannot see, such as another worker's, still open) ends that attempt
without a receipt, as before, and holds further concurrent starts until the in-flight routes
finish. The stage watcher follows one in-flight route at a time; the others run unwatched.

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
When the live check exits on its own with no receipt, the executor reports only that
(`live_check_no_receipt`; never after a timeout or a restart). If control then holds no
provider create or start claim and the route records no create or start effect, the runner
ends the attempt through the existing unclaimed unwind: it cancels the route, revokes the
grant and reconciles it at 0 with the store's own no-claim proof, so the attempt becomes
`failed_transient`/`t1_salad_unclaimed` with detail `live_check_no_receipt`, settled at 0
with its hold released. Any claim or effect keeps the wait, and the silent-executor rule
above still applies.

**Blocked campaigns, silent executors and restarts (v0.104.0; ADR 0038, 0160 and 0205
amendments of 2026-11-26).**

- *A block stops new spend, not the record of a run.* While a campaign is `blocked` or
  `paused_delegation_lapsed`, the executor keeps its `executing` attempts (to resume, finish and
  keep alive) and its `route_started` ones (keepalive only) in the work list, which now carries
  `campaign_status`. It never launches a route of a campaign that is not running. Control accepts
  the outcome, no-receipt and custody evidence of those attempts and refuses `mark_executing` and
  packaging. The runner verifies a recorded outcome while blocked and settles it through the
  ordinary charge path. A runner pass that blocks a campaign ends there, so later reserved
  stages keep their reservations and holds and continue after the unblock. If control stops
  listing an attempt it refused, the executor journals `work_withdrawn_after_refusal`.
- *Silent-executor clock.* The window plus 600 s now counts from the later of the attempt's
  last move and the campaign's latest `campaign.unblocked` or `campaign.approved`, so blocked or
  paused time is never counted and an unblock does not call a healthy route silent. A route
  whose provider receipts already confirm termination waits for the import
  (`campaign.salad_import_awaited`) instead of going `unknown`.
- *Late import of a silent-unknown attempt.* An attempt that is `unknown` with detail exactly
  `executor silent past the route window`, unsettled, and whose grant is still `active` or
  `cancel_requested` stays in the executor's work list. If its receipt is still in the
  executor's route directory, the running executor (or the next one after a restart) records the
  outcome as a late import (`late_import: true`). The runner then re-runs the unchanged
  verification and returns the stage to `running` (`campaign.salad_late_import_verified`), and
  the attempt settles from its charge like any other run. Operator action: keep the route
  directory and let the executor run. Do **not** reconcile the grant first (neither
  `POST /execution-grants/{id}/reconcile` nor `reconcile-unclaimed-salad`): a reconciled grant or a
  settled attempt is never late-imported. A late outcome whose route shows no provider-confirmed
  termination is refused, and the attempt stays `unknown` for the human path.
- *Never-sent route: released, not unknown.* A `route_started` route queued behind the
  executor's limits with no provider id, no sent create or start effect, no create or start
  claim, no manifest and no passing receipt is no longer made `unknown` when its window passes.
  The runner cancels the route, revokes the grant and reconciles it at 0 on the store's own proof.
  The attempt becomes `failed_transient` / `not_started` (`executor_silent_never_sent`) with its
  hold released, and the stage is requeued without a scientific retry. An attempt already
  `unknown` for the silent reason in that shape, which `reconcile-unclaimed-attempts --dry-run`
  refuses only as `route_wants_run`, gets the reduce-only route cancel on the next runner pass,
  even while blocked, and is then reconciled the same way. A route whose lease the executor keeps
  renewing while it waits is not silent. Anything with a claim or sent effect still waits.
- *Do not restart the executor while a route runs.* `tessera deploy-upgrade` refuses while any
  Salad attempt is `executing` ([deploy upgrade](deploy-upgrade.md#salad-routes-in-flight)).
  Restarts while stages are only reserved, preparing or queued are safe.

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
