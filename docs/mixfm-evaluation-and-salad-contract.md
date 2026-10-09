# MixFM deterministic evaluation and future Salad contract

Follow-up: [remote routing](remote-agent-execution.md) adds `salad_container`
proposal/approval/fake dispatch and a disabled trusted proxy seam. The legacy
`salad_gpu_future` adapter below remains rejecting. Its endpoint now permits only
the historical invalid-domain fixture or fixed management origin. The
[server proxy boundary](salad-proxy.md) adds synthetic custody/admission tests, not
a live provider. Neither profile
enables live Salad or replaces evaluation/scientific review.

This is local bookkeeping and validation, **not a MixFM evaluator, training job,
provider integration, or scientific acceptance claim**. It extends the ordinary
[program-review API](mixfm-interactions.md) without a schema migration, new job
ledger, credential lookup, network client, or execution profile. Default execution
and provider-disabled fences are unchanged.

## Declare the protocol in a plan

`research_program` version 1 plan scopes accept `evaluation_protocol`. Existing
prose-only plans remain readable and retain their old review rules; they have no
automatic scientific gate assessment. To use deterministic gating, submit a new
plan with a protocol and obtain explicit human approval. Merely declaring a
protocol does not prove it was preregistered before off-platform work.

The protocol has:

- `version: 1`;
- `identity`: SHA-256 `data_digest`, `code_digest`, `environment_digest` of explicit
  caller-owned manifests. Include all datasets/splits, model/teacher/student
  versions, code locks and container/CUDA/environment locks as applicable;
- `gates`: 1–100 uniquely identified metric declarations. Each specifies `id`,
  `category`, registered same-study `variant_id`, `metric`, `unit`, `split_digest`,
  `procedure`, `comparison` (`ge`, `gt`, `le`, `lt`, `eq`), finite numeric `threshold`
  and positive `minimum_samples`. Optional `training_examples` is mandatory for
  `few_shot_curve`. The procedure must explain aggregation, seeds/replicates,
  uncertainty, baseline and direction/units where relevant; none are inferred.

Supported categories are `teacher_imitation`, `experimental_ground_truth`,
`scaffold_ood`, `counterfactual_direction`, `calibration`, `capability_retention`,
`few_shot_curve`, `ingredient_holdout`, `family_holdout`, `domain_holdout` and
`leakage_check`. Teacher imitation and experimental ground truth are separate
categories, not interchangeable evidence. Represent each few-shot training size,
variant, split or replicate by a distinct gate ID. There is no curve interpolation,
inferred holdout performance, or implicit leakage pass. Protocol authors choose
the applicable metrics; support for a category does not mean it was evaluated.

Thresholds are declared in an immutable plan scope. Changing a threshold, split,
variant or identity requires a replacement plan and new evaluation record. A plan
with no gates is invalid when `evaluation_protocol` is present.

## Record and review observations

Use the same authenticated `POST /interaction/decisions` route,
`Idempotency-Key`, live human reviewer and expiry as other program proposals.
Set `kind=direction`, `action=evaluation`, the current study `basis_digest`, and
the evaluation lane's current `supersedes_id`. The `evaluation` field contains:

```text
version: 1
plan_decision_id: approved plan decision ID
plan_scope_digest: exact scope_digest returned in that plan's review history
protocol_digest: canonical_digest(plan_scope.evaluation_protocol)
identity: identical data/code/environment manifest hashes to the approved plan
evidence_kind: caller_reported
observations: [ ... ]
```

Use `tessera_control.store.canonical_digest` on the **normalized protocol returned
in the stored plan scope**, including optional/default fields. Equivalently use
`EvaluationProtocol.model_validate(payload).model_dump(mode="json")` before
hashing. Hashing an abbreviated request without its defaults is not equivalent.

Each measured observation requires `gate_id`, finite `value`, positive
`sample_count`, SHA-256 `evidence_digest` and explicit `provenance`. The digest
identifies the caller's raw/derived evidence manifest; it is not an artifact
release, custody claim or execution receipt. For unavailable observations supply
only `gate_id` and nonblank `missing_reason`. Omitting a declared gate is also
recorded explicitly as missing. Duplicate or undeclared gate IDs, NaN/infinity,
boolean/string numbers and conflicting missing/measured fields are rejected.
An empty observations list is allowed and is **inconclusive**, never a pass.

The service checks same-study/outcome plan identity and human approval, exact
plan/protocol hashes and matching data/code/environment hashes before admitting
the record. It deterministically computes and retains `evaluation_report` in the
decision-request event in the same state/event/idempotency transaction. The report
is returned with the record in `GET /interaction/studies/{id}/program-context`.
It contains each declaration/observation, status/reason, plan and manifest hashes,
record hash and report hash. No caller-supplied report/status is accepted.

Version-1 evaluation rules:

| Condition | Gate result / reason |
|---|---|
| Gate omitted | `inconclusive` / `metric_not_recorded` |
| Explicitly unavailable | `inconclusive` / `metric_missing: <caller reason>` |
| Below declared sample minimum | `inconclusive` / `insufficient_samples` |
| Exact comparison false | `fail` / `threshold_not_met` |
| Exact comparison true | `pass` / no failure reason |

Overall status is `fail` if any gate fails, otherwise `inconclusive` if anything
is missing/insufficient, otherwise `pass`. Individual missing reasons remain
visible even when another gate fails. Comparisons are exact finite Python numeric
comparisons, with no tolerance, rounding, statistical estimation or aggregation.
Choose explicit derived measurements/procedures when those operations are needed.

Agents may propose but cannot resolve. A human can approve the *record review* of
a failed/inconclusive evaluation; that does not change its gate status. Use the
existing resolution route with expected revision and rationale. Corrections append
a new evaluation proposal; old measurements/reports remain immutable and attributed.
Existing decision and snapshot bounds apply (32KB scope and 32KB combined basis/
report snapshot); oversized studies fail closed rather than truncate evidence.

For a plan with a protocol, a `go_no_go` proposal recommending `go` must specify
`evaluation_id` for the latest evaluation proposal, which must be human-approved,
bind the current study evidence basis and current approved plan proposal head,
and pass all declared gates. Approval rechecks those dependencies transactionally.
A newer evaluation/plan proposal blocks using old evaluations for go; context
marks dependent go reviews ineffective. Historical approvals remain in history.
The old source-use, findings, variant-accounting and observed-termination checks
still apply. A no-go may be reviewed early, with or without an evaluation link.
Neither outcome grants authority, cancels a job, or accepts a scientific finding.

## Opt-in Salad target configuration

`tessera_worker.salad_contract.SaladTargetConfig.model_validate(payload)` validates
the explicit JSON shape below. All values here are **contract-only placeholders**;
no endpoint, image or GPU support is asserted, and there is no usable credential.

```json
{
  "version": 1,
  "adapter_kind": "salad_gpu_future",
  "endpoint": "https://salad.example.invalid/api",
  "auth_reference": "credential-ref:operator-owned-salad",
  "image": "registry.example.invalid/research/mixfm@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "resources": {
    "cpu_cores": 4,
    "memory_mb": 16384,
    "gpu_class": "operator-selected",
    "gpu_count": 1,
    "disk_mb": 32768
  },
  "timeouts": {
    "connect_seconds": 5,
    "request_seconds": 20,
    "startup_seconds": 300,
    "run_seconds": 600,
    "stop_seconds": 60
  },
  "lease": {"duration_seconds": 120, "renew_every_seconds": 30},
  "labels": {"purpose": "contract-only", "owner": "operator"}
}
```

Unknown fields and coercions are rejected. Endpoints are now restricted to the
example above or `https://api.salad.com/api/public`, and must be HTTPS without user
information, query, fragment, whitespace or control characters; registry images
must be digest-pinned. Auth is an opaque reference, not a secret, env lookup or
path. Resources/timeouts/leases are positive and bounded. Connect timeout must
fit request timeout; request timeout must be shorter than the lease; renewal
interval must be less than half the lease. Labels are bounded nonempty identifiers.
These are local syntactic bounds, **not Salad quotas, GPU discovery, enforcement
guarantees or validated provider API resource ranges**.

`config.binding()` produces the existing provider-neutral `TargetBinding`, hashing
every config field with sorted canonical JSON. `SaladContractAdapter(config,
opt_in=True)` snapshots the validated binding. Supplying config without opt-in,
or opt-in without config, fails. It does not register a control-plane execution
target. The program context continues reporting `supported=false`.

The neutral `TargetDiscovery` interface exposes `capabilities(target)` and
`health(target)`; the existing `ExecutionAdapter` interface remains compatible.
`SaladContractAdapter()` defaults to disabled and has no target. A configured
adapter reports health `unavailable` with explicit *not queried* explanation.
Only local rejecting preflight is implemented; all effect capabilities are false.
Preflight is never admissible. Submit, observe, cancel, reconcile, collect and
account raise `SaladUnavailable` without provider effects. In particular, it
never invents termination, zero cost, artifacts or provider receipts. Mismatched
target digests fail closed. There is no SDK, transport injection, secret resolver,
environment credential read, retry loop or network request, even with opt-in.

## Unresolved live requirements

Actual MixFM evaluation still needs authorized data and raw evidence, implemented
metric procedures, split/holdout isolation, leakage analysis, model training and
scientific review. Hashes are recorded identities, not proof of content, provenance,
independence, rights or execution. Retain referenced bytes through existing artifact
custody APIs; these metadata hashes alone do not pin storage or verify bytes.

A future live Salad implementation needs a selected endpoint/API version and GPU
mapping; an operator-authorized credential broker; verified images/environments;
target isolation and data rights; explicit admission/grants; persisted idempotent
operation mapping and lost-submit reconciliation; worker lease/epoch fencing;
bounded startup/run/stop and retries; verified cancellation; output/checkpoint
hashing/quarantine; real usage/cost and unknown-exposure handling; independent
control/worker persistence; and separately authorized bounded live acceptance.
None is activated here. Configuration validity is not live readiness.

## Default verification

`tests/test_program_evaluations.py` and `tests/test_salad_contract.py` use only
temporary state, synthetic caller-reported numbers and invalid-domain config.
They cover all categories/comparators, explicit missing reasons, identity/review
fences, atomic rejection, restart/idempotency, disabled discovery and denied effects.
Network/credential entry points are patched to fail in the adapter contract test.
Run these alongside `test_program_interactions.py`, `test_interaction.py` and
`test_execution_contracts.py`. They establish local task acceptance only, not live
Salad, full-repository integration, GUI or scientific acceptance.

Task acceptance on the existing dirty working tree (2026-09-21):

```sh
# 72 passed, 2 existing Starlette/httpx and anyio deprecation warnings, 94.34s
.venv/bin/pytest -q tests/test_program_evaluations.py tests/test_salad_contract.py tests/test_program_interactions.py tests/test_execution_contracts.py tests/test_interaction.py
# All checks passed; 8 files already formatted
.venv/bin/ruff check src/tessera_control/evaluation_models.py src/tessera_control/program_models.py src/tessera_control/programs.py src/tessera_control/interaction.py src/tessera_worker/execution_contracts.py src/tessera_worker/salad_contract.py tests/test_program_evaluations.py tests/test_salad_contract.py
.venv/bin/ruff format --check src/tessera_control/evaluation_models.py src/tessera_control/program_models.py src/tessera_control/programs.py src/tessera_control/interaction.py src/tessera_worker/execution_contracts.py src/tessera_worker/salad_contract.py tests/test_program_evaluations.py tests/test_salad_contract.py
# Passed
git diff --check
```

Initial validation found test-fixture expiry/parent-directory errors and lint/format
issues, all corrected; an initial combined run also reached its 120-second bound.
The final bounded rerun above completed. Unrelated dataset-ingestion files appeared
concurrently and were preserved. Full-tree Python/lint/format and native/live
fixtures were not run. No installs, live Salad effects, commits or deployment occurred.
