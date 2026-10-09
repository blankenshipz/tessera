# MixFM Outcome 0/1 interaction slice

For “start a Salad project”, use the additive [remote routing workflow](remote-agent-execution.md):
exact target proposal, current program/source reviews, independent execution
authority and human-reviewed remote package. Program approval alone cannot
dispatch. Kubernetes/Salad routing is offline-tested; live Salad remains disabled.

## Requirements provenance and gap plan

The requested 46-page MixFM PDF text was not present in the conversation or
workspace inspected for this change. The only workspace PDF is the Tessera
architecture review. This matrix uses the user's explicit Outcome 0/1 requirements;
it is **not** a page-by-page PDF conformance claim. Reconcile against the actual
program text before scientific use.

| Requirement | Existing capability | Priority / bounded addition |
|---|---|---|
| Program objective, hypotheses, success/failure/stop gates, domain, compute/budget, provenance | Projects, studies, task limits; mostly prose | P0: typed program brief in immutable decision scope; project is program container, study is outcome ledger |
| Agent inspects context, recommends a reviewable plan and reports uncertainty | Scoped reads, conversations, provider-disabled coordinator; no scientific generation | P0: scoped program context and typed proposals through existing decision inbox; no new provider or agent authority |
| Human versions/approves/rejects/supersedes | Immutable task/design revisions and revision-bound decision resolution | P0: append-only proposal lineage with current-head and evidence-digest fencing; never edit old scope |
| Outcome 0 variants | Arbitrary study variants; two-arm execution experiments | P0: explicit selected/deferred accounting for all requested distillation/evaluation conditions |
| Outcome 1 variants | Same general primitives | P0: mixture schema/representation/baseline/transfer/holdout/calibration/leakage checklist plus immutable local snapshot declarations |
| Data/source approval | Source support assessments and separate snapshot releases | P0: typed source-use review, explicitly distinct from support assessment and byte release |
| Go/no-go, reversals, findings | Accepted/narrowed/retracted findings, conclusion amendments | P0: evidence-bound direction decisions and explicit replacement history; go checks accounting/support/review and linked execution state |
| Execute, pause/cancel, observations, artifacts/metrics | Approved task, separate grants/admission/dispatch, stop/reconciliation, immutable packages and evidence custody | Reuse unchanged; pause is hold/revoke/cancel and later fresh authority, not process suspension |
| Desktop coherence | Complete ledger UI and generic human decision inbox | P0: meaningful research review labels, rationale and visible non-execution scope; API-first structured authoring |
| Patent/reasoner/active learning/physical loop | General studies, sources, findings, decisions | P1: later versioned profiles with stage-specific controls; no inferred physical/publication authority |
| Salad GPU | Existing provider-neutral execution interface | Opt-in validated target contract and offline rejecting adapter; no live adapter, credential access, dispatch or live test |

The program-review slice reserves a typed `research_program` decision scope. A
separate schema-v48 local dataset-ingestion layer records immutable caller-declared
MixFM Outcome 0/1 snapshot digests, schema/column metadata, units, missingness,
split/leakage declarations, validation summaries, and source/license/provenance
references. It reuses the existing study and source contracts and adds a human
reviewable proposal; it stores neither dataset bytes nor an external-validation
claim. Existing study variants remain the accounting authority; experiments remain
execution authority. No billing enforcement, scientific outputs, synthetic results,
download, provider, or source-network work is introduced.

## Implemented API workflow

All writes use authenticated ordinary APIs and `Idempotency-Key`. A replay returns
the original historical receipt, not fresh authority. Read current context before
acting again. Source assessment, source-use intent, byte release, plan approval,
execution admission, results and scientific judgment are separate actions.

1. Human creates `POST /projects` and `POST /projects/{id}/studies`. A project is
   the program container; use separate studies for outcomes/domains. There is no
   new top-level program table or migration.
2. Register sources and study variants using existing routes. Sources preserve
   URI, passage locator, quoted passage, content digest and human support verdict.
   Variants preserve method/input digests and immutable metadata. Record exact
   representations, datasets, concentration units/normalization, context fields,
   model configurations, train sizes/seeds, split definitions and metric protocols
   in metadata; the platform does not infer these scientific choices. A variant
    may link an existing same-project two-arm execution experiment through
    `metadata.experiment_id`. Use multiple experiments for a multi-condition study;
    there is no combinatorial fan-out or automatic run registration.
   Register a caller-declared local dataset snapshot with
   `POST /studies/{study_id}/dataset-snapshots`. The request must reference current
   same-study source revisions/digests and declares its recognized license label,
   provenance status, columns/units/missingness, validation summary, and split/
   leakage policy. Tessera calculates the schema-record digest but does not fetch
   bytes or check the declared snapshot digest, schema, units, license, provenance,
   row count, validation result, or split. Create an immutable review record with
   `POST /studies/{study_id}/dataset-ingestion-proposals`; only a human can approve
   it, and approval fails closed if the source has changed/is unsupported or the
   recorded license/provenance status is unsupported. The study export includes all
   registered snapshots and proposals in its bounded accounting.
3. Human or scoped agent reads
   `GET /interaction/studies/{study_id}/program-context`. It returns the current
   `basis_digest`, study records, explicitly linked experiment designs/admissions/
   conclusions, complete bounded review history, proposal heads, evidence freshness,
   and the Outcome 0/1 condition catalog. Limits fail closed, never truncate a review.
   Each proposal also retains its exact review-time basis in the transactional
   decision-request event; history returns this as `basis_snapshot`. New proposal
   snapshots are capped at 32KB: split larger programs into bounded outcome studies
   rather than silently dropping evidence. This deliberate first-slice limit is
   narrower than the existing general study-export limit.
4. Human or agent proposes through `POST /interaction/decisions`. Use the existing
   project ID, a live human reviewer, expiry within 24 hours and this reserved scope:

   ```json
   {
     "type": "research_program",
     "version": 1,
     "study_id": "<study ID>",
     "outcome": "outcome_0",
     "action": "plan",
     "basis_digest": "<digest from current context>",
     "supersedes_id": null,
     "reason": "Why this proposal or revision is needed",
     "brief": {
       "objective": "Scientific objective",
       "hypothesis": "Falsifiable outcome hypothesis",
       "success_gate": "Prespecified success criterion",
       "failure_gate": "Failure/inconclusive criterion",
       "stop_gate": "Safety/resource/scientific stop rule",
       "target_domain": "Target domain and applicability boundary",
       "provenance_policy": "Rights, source support, raw records and code/environment policy",
       "data_source_ids": ["<registered source ID>"],
       "compute": {
         "budget_cents": 0,
         "max_wall_seconds": 60,
         "max_gpu_hours": 0.0,
         "target": "local_cpu",
         "resources": "Declared compute requirement, not a reservation"
       },
       "dataset_recommendation": "Dataset and suitability rationale",
       "representation_recommendation": "Representation and ablation rationale",
       "split_recommendation": "Isolation/holdout and leakage protocol",
       "metric_recommendation": "Metrics, calibration, uncertainty and retention protocol",
       "uncertainty": "What is not known and why",
       "missing_prerequisites": []
     },
     "conditions": {
       "<every key returned for the outcome>": {
         "disposition": "selected",
         "rationale": "Reviewable design or deferral reason",
         "variant_ids": ["<registered variant ID>"]
       }
     }
   }
   ```

   The example is a **shape**, not a runnable scientific plan: replace the
   placeholders and include every catalog key. Selected conditions require
   registered variants; deferred conditions require a reason and no variant IDs.
   Every registered study variant must be accounted for. A variant can represent
   multiple conditions; this does not assert that it actually implements them.
   `kind=direction` is required for plan/go-no-go, `kind=access` for source use.
   `tests/test_program_interactions.py` supplies executable isolated examples.
5. Human resolves `/interaction/decisions/resolve` with the displayed decision
   revision, `approve` or `reject`, and a required rationale. Evidence and proposal
   head are rechecked in the same state/event/idempotency transaction. Agents
   cannot resolve. A plan with disclosed missing prerequisites can be reviewed,
   but that is not execution readiness. The coordinator remains provider-disabled;
   recorded recommendations are supplied by the caller, not generated here.
6. Edit/version by submitting a complete replacement scope with `supersedes_id`
   equal to the latest proposal in that study/outcome/action lane. Old scopes are
   retained. New suggestions do not reverse a human approval; only approval of a
   replacement does. Rejection rejects the proposed replacement, not a prior
   approved decision. Use an approved `no_go` to reverse a `go`.
7. For `action=source_use`, omit brief/conditions; provide `source_ids` and
   `intended_use`. Approval requires currently supported, unsuperseded sources.
   This records human permission intent for the named use. It neither verifies
   licensing independently nor releases dataset bytes. Use existing snapshot
   release/custody commands for actual transfer.
8. Register the actual baseline/intervention task plans and execution experiment
   **before** dispatch. Follow [the local API workflow](local-api-workflow.md) for
   immutable plan/input identity, bounded grants, package/code/environment identity,
   human admission/dispatch, durable worker observations and artifact/measurement
   custody. Plan approval alone never grants execution. To pause, stop new work
   and revoke/cancel applicable grants; unknown termination stays unresolved.
   Resumption needs reconciled prior authority and fresh admission, not a claimed
   process-suspend capability. A program no-go does **not** automatically cancel.
9. Record every variant outcome, including failed/excluded/abandoned variants
   with reasons. Review results via existing experiments and study findings. Human
   acceptance, narrowing, supersession, reaffirmation and retraction keep their
   existing source-support, revision and correction checks. Manual observations
   remain explicitly manual; no execution receipt is invented.
10. For `action=go_no_go`, provide `recommendation=go|no_go` and `finding_ids`.
    Go requires a human-reviewed plan with resolved prerequisites, complete
    registered variant accounting, current human source-use review, accepted
    supported findings, all current linked experiment variants admitted, and
    terminal human outcomes plus observed confirmed termination for every linked
    admission. Failure/null results are not automatically scientific rejection.
    Unknown execution blocks go. No-go may be approved before completion to record
    an early stop direction. Both are evidence-bound human judgments, not facts
    established by an agent. A historical reviewed plan can supply the protocol;
    source-use and go review must bind the new result-time evidence digest.

The basis digest changes when the recorded study evidence or linked execution
projection changes, including source corrections, retractions and later operational
observations. Prior approvals remain historical and are marked non-current; they
are never silently rewritten. Program review history is returned by program-context;
the existing study and experiment exports remain separate and should be retained
together. Program-context is not a reproduction bundle or complete artifact export.

## Condition coverage and extensibility

- **Outcome 0:** direct property language; uncertainty/distribution; pairwise
  ranking; counterfactual intervention; attribution-assisted transfer; representation
  ablations; observable scratchpad conditions; scaffold-OOD; capability retention.
  Observable scratchpad means declared, retained experimental input/output—not
  access to a provider's private hidden reasoning. Record scratchpad/no-scratchpad
  arms, teacher/student/version identities and capability-retention controls in
  the variants' metadata and execution inputs.
- **Outcome 1:** unified mixture schema; ingredient/concentration/context;
  permutation-aware model; baseline versus pretrained mixture model; few-shot
  curves; ingredient, family and domain holdouts; calibration; leakage controls.
  Recording these choices does not implement permutation invariance, enforce a
  protected split, train MixFM, or validate concentrations/units automatically.
- Future profiles can reuse the brief, decision lineage and evidence basis without
  changing authority. Add a new validated profile/version for patent rights and
  publication review, reasoner/tool evaluations, adaptive selection accounting,
  and physical sample/operator/instrument/calibration/custody/safety records.
  Version 1 rejects unknown outcome/profile fields rather than pretending these
  later scientific/physical capabilities are supported.

## Deterministic evaluation extension

Plans now optionally declare `evaluation_protocol`; `action=evaluation` records
caller-reported metrics against immutable plan/data/code/environment identities.
The service retains deterministic threshold reports, explicit missing reasons and
pass/fail/inconclusive status. Protocol-backed go decisions require a passing current
human-reviewed evaluation via `evaluation_id`, in addition to the existing checks.
See [the full schema, rules and limitations](mixfm-evaluation-and-salad-contract.md).
No measurements, protected splits, model outputs or scientific truth are inferred.

## Salad / future remote requirement

`brief.compute.target=salad_gpu_future` remains a **planning-only selection**.
The API reports `salad_adapter.supported=false`; it does not widen execution-spec
profiles or select a runnable adapter. Use the existing provider-neutral
`src/tessera_worker/execution_contracts.py` interface: `TargetBinding`,
`ExecutionEnvelope`, preflight, submit, observe, cancel, reconcile, collect and
accounting. Do not add a second authority or job ledger.

The [opt-in Salad contract](mixfm-evaluation-and-salad-contract.md) now validates
endpoint/auth-reference/image/resources/timeouts/lease/labels and exposes neutral
capability/health discovery. Only local rejecting preflight is implemented; even
opted-in configurations cannot perform live effects or credential/network access.

Before implementing/enabling a live Salad adapter, require a separately authorized,
explicit GPU target/profile and operator-owned credential reference; pinned
code/container/CUDA/environment/data identity; target capabilities and dataset
rights/destination checks; durable idempotent provider operation identity and lost
submission reconciliation; bounded deadlines/resources/retries; checkpoint/output
collection with hashes and provenance; confirmed stop/unknown termination and
exposure reporting; and independent control/worker persistence when Electron
disconnects. Budget/compute declarations here are not billing enforcement or proof
that a target can enforce them. No Salad SDK, credential, client, provider job or
network call is included in this pass.

## Remaining product gaps

- Missing original PDF prevents a page-level requirements reconciliation.
- Typed authoring, replacement history and context browsing are API-first. The
  desktop pending inbox displays the full scope, outcome/action/recommendation,
  immutable basis and lineage, and requires a human rationale; a dedicated program
  editor/history browser and automatic proposal-to-task materialization remain.
- Program reviews are intent records, **not a mandatory admission policy for every
  generic task**. Human task approval, source-byte release, grants and dispatch
  remain separate. Deterministic comparisons of declared metrics are implemented;
  scientific metric computation is not. No training, distillation,
  data ingestion/mixture normalization, provider advice or model output is added.
- Scientific split isolation, calibrated uncertainty, retention and leakage
  evaluations require real implementations and separately authorized execution.
  Dataset declarations do not download data or validate external datasets, source
  licenses/provenance, row counts, schemas, units, missingness, or split behavior.
  Completeness covers registered study records and explicit experiment links,
  never omitted/off-platform work. Existing manual evidence paths remain labeled.
- Native GUI, full repository integration and live scientific/provider/Salad
  acceptance are not established by isolated tests. No dependencies were installed,
  services deployed, runtime/Kubernetes state changed, or commits/pushes made.

## Verification (dirty working tree, isolated effects only)

```sh
# 108 passed; two existing Starlette/httpx and anyio deprecation warnings
.venv/bin/pytest -q tests/test_program_interactions.py tests/test_interaction.py tests/test_records_api.py tests/test_research.py tests/test_finding_retraction.py tests/test_finding_versions.py tests/test_plans.py tests/test_goal_to_decision.py tests/test_local_api_workflow.py tests/test_execution_contracts.py
# 131 passed, 0 failed/skipped; actual renderer in a fake DOM, no native GUI
node --test --test-reporter=spec desktop/test/*.test.mjs
# Passed; six files already formatted
.venv/bin/ruff check src/tessera_control/program_models.py src/tessera_control/programs.py src/tessera_control/interaction.py src/tessera_control/interaction_api.py src/tessera_control/interaction_models.py tests/test_program_interactions.py
.venv/bin/ruff format --check src/tessera_control/program_models.py src/tessera_control/programs.py src/tessera_control/interaction.py src/tessera_control/interaction_api.py src/tessera_control/interaction_models.py tests/test_program_interactions.py
git diff --check
```

The eleven new API cases cover both condition profiles, agent proposal/human
resolution, source-use denial, stale evidence/revisions/lineage, incomplete
condition and variant accounting, cross-project denial, prerequisite/rationale
requirements, idempotency, reopen, decision reversal/retraction, and failed versus
unknown linked execution through the ordinary fake-effect worker journey. The
new renderer test exercises the displayed review boundary and mandatory rationale.
Initial scoped lint found import order/long-line formatting issues; those were
fixed before the passing commands above. Full-repository Python/lint/format and
native/live integration gates were not run; this is task acceptance only.
