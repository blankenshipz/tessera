# 0040 — Multi-stage, multi-vendor campaigns

## Status

Accepted (2026-11-19). Goal: on-platform agents design experiments with as many
stages across as many vendors as they want, and stage inputs and outputs persist and
plug into each other. Implemented by the ADR 0040 changes (provider interface, trust setting, campaign spec v2).
Amends [ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) (spec, runner,
receipts) and [ADR 0039](0039-data-class-guard.md) (the third-party data rule).
Schema migration v64 → v65 (v62 and v63 are ADR 0039's, v64 is ADR 0038's campaign
migration). Example operator configuration lives in
[provider trust](../provider-trust.md), not here.

## Context

**Backends are hard-coded.** Spec v1 allows 1–32 stages, each naming one of two
backends. The runner branches on `SALAD_BACKEND` in about fifteen places, so adding a
vendor means editing the runner core. **The runner never executes.** It only makes
delegated store calls (ADR 0038 §8). `tessera-worker development-poll` runs local Docker
CPU grants, and `campaign_salad_executor` runs Salad routes. **I/O is
content-addressed.** Stage inputs are snapshots. Outputs are quarantined manifests with
a sha256 per file, custody-released and linked by lineage. Outputs reach later stages
only through composition, which records them `private_derived`. **Salad cannot take
derived data.** ADR 0039 admits only public data and human-approved code to third-party
compute, so a GPU stage fed by earlier outputs cannot run on Salad. **Bounds.** Outputs
are capped at 16 MiB per stage (`MAX_ARTIFACT_BYTES`, and Salad's
`MAX_TOTAL_OUTPUT_BYTES`). Chunked snapshots go up to 2 GiB and 10,000 files, with 8 GiB
per project, all stored in the control SQLite database (ADR 0037). *Amended by
[ADR 0050](0050-large-artifact-custody.md): outputs above 16 MiB use chunked custody and
these bounds follow the operator limits (default 8 GiB per artifact, 32 GiB per project);
Salad stage outputs stay at 16 MiB.* **Self-hosted servers.** A
deployment may run the control service, the Salad guardian and the Salad executor on an
self-hosted server that runs no experiment containers. The ordinary-docker seam built
for such hosts is frozen (ADR 0033).

## Decision

### 1. Campaign spec v2

`CampaignSpec` accepts `version: 1` unchanged, so existing digests and approvals stay
valid, or `version: 2`. A pure, tested `normalize(spec)` turns either one into an
in-memory v2 form that only the runner reads. Approval binds `canonical_digest` of the
spec exactly as submitted; the normal form is never digested or stored. v1 mapping:
`docker-local-cpu-v1` becomes provider `local-docker` with runtime `docker-cpu-v1`;
`salad-gpu-v1` becomes provider `salad` with runtime `salad-workload-v1`, and its
`salad` block becomes `provider_config`.

A v2 stage has `key`; `provider` (a registered id); `runtime` (a contract that
provider declares); `provider_config` (validated by the provider's strict model, part
of the digest, never secrets); v1's `image`, `argv`, `env`, `egress`,
`max_duration_seconds`, `max_exposure_cents`, `retry`, `metrics` and `stop_when`;
`inputs`; `outputs`, 1–128 entries of `{name, path, kind, media_type, max_bytes}` with
`kind` one of `dataset|model|cache|metrics|code|log|other`, whose `max_bytes` sum must
fit the provider's output limit; and an optional `send_classes` (§4).

The spec-level `worker_principal_id` is gone; the provider instance's operator
configuration supplies the worker.

A stage has 1–8 inputs, each mounted at `/inputs/<name>/`. An input is one of: `snapshot
{name, snapshot_id, digest, data_class}`, as in v1; `stage {name, stage,
outputs:[names]}`, named outputs of an upstream stage; `artifact {name, campaign_id,
stage, output, sha256}`, an output of an earlier campaign, pinned by digest. It resolves
only to a `campaign_stage_io` row (§3) that is in the same org and project, comes from a
succeeded attempt listed in that campaign's terminal receipt, and is backed by a
custody-released, `retain_quarantined` file with that sha256. Anything else is a 409.

**Bound.** At most 64 stages, 8 inputs per stage, 512 edges, and 1 MiB of canonical
spec JSON. A human reviews the whole DAG in one approval. Each attempt makes about 25
intents, there are up to 5 attempts per stage, the runner is bounded per tick, and the
5000-cent and 7-day caps are unchanged. "As many stages as they want" comes from
chaining campaigns through `artifact` inputs, which has no bound. Other validation
(acyclic, unique, resolved, metrics on declared outputs, holds under the cap) is
unchanged.

### 2. Compute provider plugins

`tessera_control.campaign_providers` defines one `ComputeProvider` protocol and a
registry keyed by provider id (`register_provider`). A duplicate id is refused, and an
unknown id is refused at submit. The runner core calls only the protocol: it never
imports a vendor module or compares provider ids.

A provider has a descriptor: `provider_id`, `kind`, `runtimes`, `target` (an
`execution_targets` name, or none), `billable`, `third_party`, `delegable_steps`, output
limits. A stage with several inputs is composed into one snapshot first, so a provider
receives one input. Its operations: **validate**:
pure, at submit and approve. **check**: against the database (profiles, image
allowlists, worker). **submit**: `prepare` (scaffolding steps), `grant_request` (worker
and expiry clamp) and `after_admit`. **poll**: `observe` returns `{outcome,
failure_class, evidence_kind, evidence_digest, manifest_id}`, where outcome is one of
`running`, `succeeded`, `failed_transient`, `failed_permanent` or `unknown`. **cancel**:
a reduce-only unwind. **cost/settle**: `settle` returns `Settlement{cents, basis, final,
estimate_cents}` or raises `Waiting`; `exposure_open` and `after_tick` cover late
charges. **staging and custody**: declared by the provider and performed by its
executor. Bytes never pass through the runner.

| Operation | `local-docker`, `self-hosted` (`worker_grant`) | `salad` (`salad_route`) |
| --- | --- | --- |
| submit | `issue_grant`; `development-poll` leases and runs it | `SaladStageDriver.scaffold`, `issue_grant`, `admit_attempt`, route propose/approve/start; executor runs `salad_live_check` |
| poll | runner `_observe` / `_do_observe` | `SaladStageDriver.advance` / `verify_outcome` |
| cancel | `revoke_grant`, `reconcile_grant` / `reconcile_unattempted` | `SaladStageDriver.unwind` (`cancel_route`, `reconcile_unclaimed_salad`) |
| settle | `local_zero` | `settlement_for` (`human_charge`, `human_reconcile`, `unclaimed_zero`, `guardian_bounded_hold`), `late_charges` |
| input staging | `release_snapshot`, `POST /worker/leases/{id}/inputs` (chunked) | `ProviderInputStore.issue_ticket`, `TrustedSaladServer.deliver_inputs` |
| output custody | worker artifact/manifest upload, quarantine, disposition, custody release | `TrustedSaladServer.import_retained`, executor upload via worker artifact routes, same manifest path |

The three providers: `local-docker` is built in. `self-hosted` is a second instance of
the same `worker_grant` code: the unchanged development worker and grant poller
(`docker-cpu-v1`) on a self-hosted server, under its own worker principal. It is not
the frozen ordinary-docker seam, and it claims no GPU. `salad` wraps `SaladStageDriver`,
with the settle and poll contracts of ADR 0038's amendments.

Instances come from a closed, secret-free operator file (`tessera serve
--campaign-providers FILE`), which also absorbs the Salad campaign profiles. Without
the file, `local-docker` and the `salad` provider with no profiles exist (a Salad stage is
refused until profiles are supplied). A new vendor is a plugin module, a `TargetSpec`
if it routes, and an executor; the runner, the spec model and the delegation hook do
not change.

**Delegation.** `STEP_TABLE` stays one closed, reviewed table. Each plugin declares
`delegable_steps ⊆ STEP_TABLE`. Every intent records its stage's provider, and `admit`
refuses a step outside that provider's set. Plugins cannot register steps.

**Reviewed core changes, not plugin changes.** `ProviderKind`, the evidence kinds an
`Observation` may carry and `FAILURE_CLASSES` are closed core enums too (today they name
the worker-grant and route kinds and the evidence and failure classes of the two shipped
providers). A new route-based vendor whose evidence or failure classes do not fit them needs
a reviewed core change, consistent with the step and basis rule below. Two further things a
vendor may need are deliberately not pluggable: a new delegable step kind (a `STEP_TABLE` row with its
authority checks) and a new `settlement_basis` value (a closed core enum, stored in a
column, mapped in receipts and in ledger reconciliation). Each changes what the runner may
do without a human or what a receipt claims about money, so each is a security-reviewed
change to the core, with its own ADR amendment and tests. A provider whose effects fit the
existing steps and bases is a plugin-only change; one that needs another step or basis is
not, and its plugin cannot load until that core change lands (registration refuses unknown
steps; `settle` returning an unknown basis is a runner error that blocks the attempt). These
stay generic in the core:
intent-first writes, completed in the effect's transaction and replayed on resume;
authority: live approver, campaign state, resource ownership, and the tenant check in
`_mutate`; every budget hold, retry classification and `settlement_basis`; the receipt.

Plugin rows such as `campaign_salad_attempts` are written only through the runner
connection while the runner lease is held.

Settlement note (ADR 0041, "Execution frames"): the campaign settlement routine
(`campaign_settlement.settle_attempt`, idempotent by request digest) is the path ADR 0041
stage 3 turns into `settle_frame`. A provider's `on_settled` hook runs inside that routine's
transaction, and a provider never writes the campaign ledger itself. The worker-attested
guardian final (ADR 0038 amendment A) is superseded by ADR 0041's control-read guardian
verdict once stage 3 lands.

### 3. Persistent stage I/O

The new table `campaign_stage_io` indexes every resolved input and output of each
attempt. Each row holds the org, project, campaign, stage, attempt, direction, name,
kind, media type, sha256, size, `snapshot_id` or (`manifest_id`, path), data class,
lineage node and time. The row is written in the runner transaction that marks the
input ready or the attempt succeeded, and triggers refuse update and delete. The bytes
stay in snapshots and retained manifests; the index only names them.

**Resume** works as in ADR 0038 §8. **Re-run or swap**: a new spec (so a new approval)
that pins the same inputs by digest and changes the stage or its provider. **Reuse**: an
`artifact` input.

**No relabelling.** Outputs are always `private_derived`. Composition and `artifact`
resolution record `derived=True` and copy no code approval. No class is ever upgraded,
and the index holds the platform's recorded class, never the spec's claim. Lineage adds
`derived_from` edges from outputs to inputs and to the node that produced each
`artifact` input.

**Storage, honestly.** A stage's outputs are capped at the smaller of its `max_bytes`
sum and the provider limit, which is 16 MiB for all three providers. A composed input
above 16 MiB is a chunked snapshot: at most 2 GiB, 8 GiB per project, in SQLite. Larger
artifacts need chunked output custody, which is not done. *(Done for local Docker and
self-hosted stages by [ADR 0050](0050-large-artifact-custody.md), with operator-configurable
bounds.)*

### 4. Provider data-class trust policy (amends ADR 0039)

The hard rule that only public data and approved code reach third-party compute is
replaced by an **operator setting of the deployment**: which of `public`,
`private_code_approved` and `private_derived` each provider may receive.
`private_derived` covers every non-public record (`derived`, `default`, `migrated`,
unrecorded).

**Setting.** The file is passed as `tessera serve --provider-trust FILE`, schema
`tessera.provider-trust.v1`. It maps each provider id to its `data_classes` and its
`worker_principal_ids`: the worker principals bound to that provider instance (for a
`worker_grant` provider, the workers that run its grants; for a route provider, the
executor principals that hold its grants). A principal may appear under at most one
provider; a duplicate refuses the file.

**Trust is keyed on the provider instance.** A class reaches a worker only when the
grant's provider id and its worker principal are bound together in the setting:

- the stage's (or target's) provider id is listed, with that class in `data_classes`;
- the grant's worker principal is listed under that same provider.

A principal listed nowhere, or listed under a different provider than the stage or
target names, receives `public` only. An unknown or unlisted provider id receives `public`
only. No grant inherits trust from a plan profile, a host or a previous release: the
ADR 0038 §5 rule "`private_derived` for any `local-cpu` plan without egress" is replaced
by this binding.
The `--campaign-providers` file (§2) says which principal an instance uses; it grants
no trust. Only the trust setting binds a principal to classes, so an instance whose
principal is not bound there runs public inputs only.

**File checks.** A symlink, a file not owned by the service user, a group- or
world-writable file, or a malformed file stops startup with exit 2. **No write API.**
Agents and specs cannot change the setting; host file permissions are the boundary.
**Loading.** At start, and on the human-only `POST /admin/provider-trust/reload`, which
re-reads the configured path. Each load is canonicalized into `provider_trust_policies`,
an immutable table keyed by digest, and an append-only activation log names the newest one.
**Effective policy.** Every admission, ticket, lease and receipt check reads the newest
activated row from the database (cached by digest), so the live executor, the provider-input
server and a reload in the serving process all enforce the same setting; no row means the
built-in default, and a recorded document that no longer parses fails closed to it.
**Audit.** A new digest appends
`provider_trust.policy_changed`, recording the old and new digest, the per-provider
difference and the actor (`deployment_start` or the human). A failed load keeps the last
good setting and appends `provider_trust.reload_refused`. **Reads.** `GET
/admin/provider-trust` (a human, or the `provider_trust:read` scope), `tessera admin
provider-trust show`, and the MCP tool `tessera_get_provider_trust` (registered only
with that scope). Each returns the setting, its digest and its load time, so agents can
design within it.

**Default.** Built in, recorded the same way, and fail-closed: every provider, including
`local-docker`, receives `public` only, because the default binds no worker principal.
Tessera has no verifiable notion of "the control host's own worker": a worker principal
is an enrolled credential that can run on any host, so the built-in `local-docker` entry
cannot safely name one. Private classes therefore need an operator file, with no
exception for `local-docker`, `self-hosted` or the `kubernetes` target.

**Upgrade impact (deliberate).** On upgrade without a trust file:

- local CPU grants on a `private_derived` release, including campaign stages fed by
  upstream outputs or composed inputs, are refused until the operator lists the local
  worker principal under `local-docker` with `private_derived`;
- public-input local CPU campaigns are unaffected, but a snapshot carrying a code
  approval now needs `private_code_approved` for its provider, `local-docker` included;
- approved code no longer reaches Salad (a change from ADR 0039 amendment 1); it needs
  `private_code_approved` for `salad` and the executor principal bound.

Such a request is refused at its next input-ready, grant or ticket check (a 409 naming
the provider and principal; the runner blocks the stage). Nothing already released is
reclassified. The refusal is a configuration error for the operator, not a data-class
change; [provider trust](../provider-trust.md) gives a minimal file that restores local
behaviour.

**Specs only narrow.** A stage's `send_classes` and a spec-level `forbid_providers`
narrow the setting and never widen it. A `send_classes` entry outside the current
setting is a 409 at submit and again at approve; it is never silently dropped.
The approval records the setting digest it was checked against. Every enforcement
point uses the intersection of the current setting, the approval-time setting and
`send_classes`, so a later narrowing fails closed and a later widening adds nothing to
an approved campaign. Precisely: the runner's admission gate (submit, approve, reserve
and each attempt's re-check) applies current ∩ approval-time ∩ `send_classes`;
the downstream points (ticket issue, workload admission, grant and lease checks) re-check
the current setting only, so a narrowing cuts off a live run at those points too.

**Check.** `provider_admits(connection, provider_id, worker_principal_id, snapshot_id,
narrowed)` returns `{code_paths, input_data_class, input_code, basis, policy_digest}`,
where `basis` is `public_only` or `provider_trust_policy`. Let *allowed* be the
instance's classes (above) after narrowing. In order, each step refusing on failure:

1. An invalid code approval or an unreadable manifest is refused under any setting.
2. **Code approval fails closed.** If the snapshot carries any owner code approval, the
   provider must allow `private_code_approved`, whatever the snapshot's recorded data
   class. Otherwise it is refused. Approved files never fall back to counting as data:
   a snapshot staged with approved code is recorded `public`, so that fallback would pass private code
   as public.
3. Every file outside the approved code paths counts as the snapshot's recorded class
   (`public`, or `private_derived` for every non-public record), which must be in
   *allowed*.
4. A snapshot without an approval and with recorded class `public` is admitted with
   basis `public_only`; every other admission has basis `provider_trust_policy`.

**Route literal.** A route's `data_policy` is `public_released_only` only when the
admission basis is `public_only`; `check_workload_admission` refuses any other basis
unless the literal is `provider_trust_policy_v1`. The campaign path derives the literal
from the admitted input (a snapshot with a code approval is non-public), and a stage
with such an input cannot declare workload fetch or redirect hosts.

**Changed ADR 0039 checks:**
1. `require_public_input` becomes `require_provider_input`; the old name stays as a
   wrapper. It maps the target through a new `TargetSpec.provider_id` (both Salad
   targets map to `salad`). Route-less and unregistered targets stay `public` only.
2. `require_public_provider_input` (ticket issue, and every manifest and chunk pull)
   checks the provider of the route's target. Pulls re-check, so a narrowed setting
   cuts off live tickets.
3. `check_workload_admission` takes the first staging of the digest that the setting
   admits, preferring public, then code-only, then private. Each candidate staging goes
   through the full check above; an approval-bearing staging is never admitted as data.
   The entry-must-be-code rule applies only when admission relied on a code approval and
   `private_derived` is not allowed.
4. In `ExecutionStore._authorize_snapshot`, `allow_private_derived` means "the grant's
   provider instance (provider id plus bound worker principal) admits `private_derived`"
   instead of "local-cpu plan". A `private_derived` release is then valid for Salad grants
   only when the executor principal is bound to `salad`.
5. The Salad-only checks (`check_salad_inputs`, the runner's Salad-public check, the
   model's refusal of a Salad stage source) become one `provider_admits` check at
   submit, approve, reservation and input-ready. A Salad stage may then take `stage`
   and `artifact` inputs, composed into one snapshot.
6. The route `data_policy` literal gains `provider_trust_policy_v1`, which the runner
   sets when the input is not public. A route never claims `public_released_only` for
   private bytes.

**Unchanged:** public declarations with license and source evidence; immutable class
and approval rows; anti-laundering; `OUTPUT_DATA_CLASS`; composition recorded as
derived; no reclassification endpoint; `refuse_local_transfer`; the hook's refusal of a
`public` release for a non-public record; and ADR 0034's rule that private inputs get
no egress. That rule now covers workload egress too: a stage with any non-public input
declares no fetch or redirect hosts.

**Receipts.** These record `provider_id`, the bound `worker_principal_id`,
`trust_policy_digest`, `admission_basis`,
`input_data_class`, `input_code` and `input_code_files`: the worker run receipt and its
event; the ticket-issue event; route admission evidence; every campaign receipt attempt
(receipt v2).

The frozen Salad live-check receipt keeps its existing class fields, which remain
accurate; the campaign receipt binds it to the policy digest.

### 5. Migration, frozen planes and limits

**Migration.** `migrate_v64_to_v65` makes these changes, and the same DDL goes into
`_create_schema`: adds the `provider_trust_policies` and `campaign_stage_io` tables,
with triggers; adds `provider_id` to `campaign_stages`; adds `provider_id`,
`trust_policy_digest` and `admission_basis` to `campaign_stage_attempts`; adds
`trust_policy_digest` to `campaign_approvals`; backfills `provider_id` from v1 backends;
marks the last backup stale.

There is no CHECK rebuild and no reclassification. Live tickets stay valid because
every pull re-checks.

**ADR 0033.** No guardian, custody, proxy, IMDS, broker, Kubernetes, ordinary-docker or
harness code changes. Three narrow exceptions:
1. The two `data_policy` literals (in `remote_execution_models` and `admin_models`)
   each gain one value, so route records stay truthful.
2. `provider_inputs` and `salad_workload_admission` change only as listed in §4; both
   already changed under ADR 0039.
3. `remote_execution._admit_workload` and `salad_server.preflight_authority` pass the
   operation's `worker_principal_id` into workload admission, which instance-keyed trust
   needs, and route approval stores `state["input_admission"]`.

`self-hosted` uses the unfrozen development worker. ADR 0033's `adapter_kind` registry
over `ExecutionAdapter` is not built; campaign providers sit above it.

**Not done:** stage memoization; chunked output custody above 16 MiB for Salad stage outputs
(other providers: see [ADR 0050](0050-large-artifact-custody.md)); GPU runtimes on
self-hosted servers; `provider_billed` settlement; pre-approval of per-attempt Salad decisions;
multi-host runners; dynamic fan-out; object storage; reuse across projects.

**What remains for an unattended multi-stage campaign** (for example fetch →
preprocess → large intermediate artifact → GPU stage → evaluation → gate): **Fetch and
preprocess** already run as CPU stages; only public sources get egress. **Intermediate
artifacts and model outputs** above 16 MiB need chunked output custody. **A GPU stage on
derived inputs** needs this record, an operator setting that binds the Salad executor,
and a Salad workload without egress. **Per-attempt Salad plan and source-use
decisions** still need a human until a pre-approval amendment exists. Settlement is
already automatic (`guardian_bounded_hold`). **The gate** is an ADR 0032 bound gate, and
its go stays with a human. **Supervision.** `development-poll` and the Salad executor
need always-on supervision under ADR 0038's loop contract.

## Consequences

Vendors are plugins. Derived data reaches a GPU vendor only through an audited operator
setting that agents cannot widen, and every control-side receipt names its digest. ADR 0039's
invariant changes from "data reaching third-party compute is recorded public" to "it is
within the recorded provider setting for the bound provider instance at transfer time".
Approved code is admitted only as `private_code_approved`, never as data.

## Revisit when

A campaign needs more than 64 stages; outputs must exceed 2 GiB or SQLite becomes the
binding limit; a provider exposes billed usage; or the owner decides on Salad decision
pre-approval.
