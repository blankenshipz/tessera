# Bounded autonomous preparation

Tessera now has a bounded **planning and synthetic preparation state machine**,
not a general computer-use agent or an activated computer sandbox. No model is
called to generate plans. A scoped agent can submit them using the API; a human
reviews them. Existing project context access remains the only inspection access.
See [decision 0020](decisions/0020-bounded-computer-autonomy.md).

## Workflow

1. Read authorized project/task context. Create/revise the ordinary task plan and
   request an exact [capability declaration](agent-capability-policy.md). Include
   pinned environment/image, sources, package locks, hashes and all resource caps.
   Do not paste credentials, host paths, raw logs or secret-bearing URLs in plans.
2. `POST /tasks/{task_id}/autonomy-sessions` with a typed autonomy plan. Link the
   policy ID/digest and task basis; explain environment, acquisition, uncertainty,
   success and stop criteria. Acquisition entries reference declared action IDs.
   Unsupported credentials/private/paid/persistent/external/scientific/publication
   requests are recorded as escalation, never executed by these routes.
3. A human approves the ordinary task and capability policy separately, then
   reviews the exact session revision/digest using
   `POST /autonomy-sessions/{id}/commands` with `command=approve`. The default
   `observation-only` profile starts no effects. `public-prep`/`experiment-ready`
   still require existing grants, worker leases and signed broker packages.
   `public_auto=true` is an explicit human-reviewed configuration allowing only
   exact anonymous capped preparation actions; default false requires a checkpoint
   and human resume for each action. Neither option activates a real backend.
4. Agents can `checkpoint` with a focused question and optional declared action ID,
   or `suspend`, `revise`, `cancel`, `complete`, or report `unknown`. All commands
   require `expected_revision`; revise supplies a complete new plan. Human `resume`
   uses the current revision and preserves the original absolute deadline.
   Resume is logical continuation, not process resurrection or unknown-effect retry.
5. Trusted workers bind an already admitted/issued operation with
   `POST /worker/autonomy-sessions/{id}/actions` (operation/package identity and
   expected revision). The control creates the nonce and a five-second lease.
   `/heartbeat` renews only current running authority; `/receipt` links already
   reconciled broker evidence. No action route accepts arbitrary commands, URLs,
   credentials, fixture bytes, or caller-asserted success. Use the existing broker
   through a trusted supervisor; never give a workload control files/signing keys.
6. `GET /autonomy-sessions/{id}` or `GET /tasks/{task_id}/autonomy-sessions`
   shows durable state, review/blocked reasons, checkpoint and receipts. The desktop
   task cards expose the same read projection. API-first authoring and review are
   intentional; no full GUI plan editor or interactive browser/terminal is added.

All writes require authenticated credentials and `Idempotency-Key`. Exact replay
returns historical evidence, not renewed authority. Read current state after an
uncertain response; never submit the effect with a fresh operation or journal.

## Stops, recovery and evidence

Cancellation fences new broker steps. In-flight cancellation, crash, lost reply
or expired watchdog lease projects unknown, not confirmed termination. Retain the
original broker journal and reconcile its evidence. Unknown sessions cannot resume.
Synthetic session leases do not kill processes; live work remains blocked pending
independent watchdog/isolation conformance. Suspend is supported only between
actions. Maximum duration is 900 seconds, single-flight, at most 8 actions, zero
authorized paid spend, no retries. Billing remains unknown, not zero accounting.

Receipts distinguish synthetic completion, quarantined artifacts, observed stop,
missing stdout/stderr capture and scientific validity `not_assessed`. Source/license
review, dataset metadata validation, MixFM evaluation and scientific acceptance
remain separate. Session approval never performs those decisions automatically.

Control backups include session event history without a schema migration. Preserve
worker journals and broker project-object custody separately; session metadata is
not a self-contained reproduction/byte backup. Held restored history cannot approve/resume/replay;
see [recovery](recovery.md). Never delete uncertain journals to unblock work.

## Opt-in demonstration and next gate

`scripts/check_autonomy_fake.py` is disabled without `--run`. Its opt-in demonstration
uses temporary synthetic control state and the existing fake capability broker;
no Docker, network, package installation, provider/model or GPU effects. It is
outside default pytest/CI. Test fixtures are required in this source checkout.

Real computer interaction, arbitrary commands, browser tools and actual network
acquisition remain unsupported. The next separately authorized live gate is exact
target/image isolation and external watchdog conformance, including resolving the
[local fixture network witness](local-networkless-docker-fixture.md) or supplying
the complete [Kubernetes witness deployment](runtime-witnesses.md). A passing fake
demo cannot activate either backend. Salad remains deferred.

The [next sandbox gate](independent-sandbox-supervision.md) now has candidate
research, a local external guardian, an opt-in one-case command and failure tests.
Its one live attempt remained blocked at the runtime network witness, with exact
cleanup confirmed. It did not activate this synthetic session path.

## Recorded task verification (2026-09-23)

On the pre-existing dirty `main` tree, these isolated commands passed:

```sh
.venv/bin/pytest -q tests/test_autonomy.py
# 39 passed, 2 dependency deprecation warnings, 25.97s

.venv/bin/pytest -q tests/test_runtime_capability_broker.py \
  tests/test_runtime_capability_journal.py tests/test_capabilities.py \
  tests/test_capability_fencing.py tests/test_capability_contract.py \
  tests/test_capability_activation.py tests/test_capability_watchdog.py \
  tests/test_local_docker_fixture.py tests/test_local_docker_limits.py
# 449 passed, 2 dependency deprecation warnings, 77.87s

.venv/bin/pytest -q tests/test_capability_kubernetes_runner.py \
  tests/test_capability_collector.py tests/test_capability_runtime_witnesses.py \
  tests/test_program_interactions.py tests/test_dataset_ingestion.py \
  tests/test_program_evaluations.py tests/test_recovery.py tests/test_recovery_safety.py
# 692 passed, 2 dependency deprecation warnings, 244.11s

npm test --prefix desktop
# 143 passed, 0 failed; dependency-free Node tests, not a GUI launch

.venv/bin/python scripts/check_autonomy_fake.py
# disabled, effects=0, activation=blocked
TMPDIR=/tmp/opencode .venv/bin/python scripts/check_autonomy_fake.py --run
# one synthetic action, 23 retained fixture bytes, completed broker receipt,
# session ready, external_effects=0, activation=blocked, billing=null
```

Scoped Ruff lint and format checks pass for `autonomy.py`, `autonomy_models.py`,
`autonomy_api.py`, `capabilities.py`, `api.py`, `test_autonomy.py` and the demo.
Tracked and new-file diff whitespace checks pass. Initial formatting/import lint,
a desktop fixture missing authenticated connection state, and the demo's macOS
symlinked temporary parent were repaired. No workspace guard was weakened.

These are targeted acceptance results, not the full Python integration suite,
repository-wide lint/format, visible Electron validation, live Docker/Kubernetes
conformance or scientific evidence. No real downloads, installs, provider/model,
GPU or Salad effects ran. No commits or pushes were made.
