# Implemented capabilities and evidence boundaries

This detailed capability inventory complements the concise [Tessera overview](../README.md).
Historical observations are bounded evidence, not guarantees for a fresh installation.

New to the local macOS alpha? Start with the [quickstart](quickstart.md) for
the supported service → desktop → project/design → review/export path and its
explicit execution and acceptance boundaries.

The [administrative workspace](admin-setup.md) adds Setup, Targets, Secrets,
Delegations, Readiness and Audit panels with typed project-scoped metadata commands.
Secret lifecycle includes [protected native macOS Keychain enrollment](protected-secret-enrollment.md)
for a same-host native control database, plus explicit synthetic fixtures. Values
never enter the renderer or HTTP API. Remote/Windows/Linux enrollment remains
unavailable; delegation governs metadata, not execution or secret reads.
Organizational invitations and live provider enablement remain unavailable.
Configuration and successful enrollment are not readiness.

[Remote agent execution routing](remote-agent-execution.md) adds typed
Kubernetes/Salad proposals, human review, durable offline dispatch and desktop
control/status. Desktop/local-process targets are rejected by this route. Default
targets and live effects remain disabled; fake receipts are not live execution,
custody, cost settlement or scientific acceptance evidence.

The [Salad server proxy boundary](salad-proxy.md) adds proxy-only credential
interfaces, strict admission and fake transport tests. Its live entry point remains
blocked before secret resolution/network. The opt-in local validator is
`.venv/bin/python -m tessera_control.salad_proxy_cli`; it performs no network and
does not establish provider isolation or an autonomous sandbox.

Tessera is a workspace for turning a research goal into a bounded experiment
design, remote execution, linked evidence, and a human decision. A researcher
works with a desktop agent to plan an investigation, run experiments on approved
infrastructure, and review findings in a persistent labbook. Skills and external
tracking or compute systems support that workflow; they are not the product's
authority.

A tessera is a piece of a mosaic. The name reflects how observations, experiments,
and different perspectives come together to form a research finding.

The desktop, control plane, and execution workers run independently. Here,
“remote” means outside Electron: all three can run on one machine or across
separate machines and clusters.

The control plane and laptop-independent execution are first-class. Closing the
desktop must not erase the authoritative record or stop eligible remote work.
Local CPU is the primary development target; one identified remote CPU target is
being pursued in parallel. MLflow, Databricks, Spark, and similar systems are
optional integrations, never the authority for Tessera plans, attempts, evidence,
or human conclusions.

**Status: usable bounded local research bench; developer alpha.** This repository is for local developer
evaluation, not a production release or hosted service. The local API supports scoped credentials, versioned task plans,
human approval records, durable conversations and decisions, and a research
ledger with complete study exports and source corrections. Approval records intent
only; they do not grant execution. A desktop shell, safe file inventory, structured
transport, and CPU supervisor have component tests. Native desktop runtime and
CPU isolation fixtures pass. Live experiment dispatch and the full pilot remain
incomplete. See
[implementation progress](implementation-progress.md) for acceptance limits.

For the bounded supported-workflow matrix, enforced HTTP/output limits, and
remaining production authentication, spending, storage and release prerequisites,
see [launch readiness](launch-readiness.md). Local safeguards do not enable a
provider or establish production support.

[Bounded autonomy](bounded-autonomy.md) adds durable agent-authored preparation
plans, exact human review, checkpoints, session leases and synthetic broker receipt
projections. Public auto-approval is explicit and capped within an existing approved
capability envelope. Real computer-use/download/install effects remain blocked;
session approval is neither execution authority nor scientific acceptance.

The opt-in [execution-plane monitor](execution-monitor.md) runs beside the
execution target while the control plane runs anywhere, without host namespace or
Docker socket access. Signed assignments, independent process deadlines, durable
monitor receipts and exact-owned stop/reconciliation have offline conformance tests.
The closed Docker adapter requires explicit host privileges and native observations;
Kubernetes/Salad monitor adapters and real autonomy activation remain disabled.

The [MixFM Outcome 0/1 interaction slice](mixfm-interactions.md) adds typed,
evidence-fenced program-plan, source-use and go/no-go reviews to the existing
decision inbox. Agents can propose; only humans resolve. Structured authoring and
review history are API-first; the desktop explains and resolves pending reviews.
This is research bookkeeping, not generated scientific advice, model training,
Salad support, or permission to execute.

[MixFM evaluation and the future Salad contract](mixfm-evaluation-and-salad-contract.md)
add caller-reported metrics, immutable declared thresholds, deterministic
pass/fail/inconclusive reports and human go/no-go gating. An opt-in validated
Salad GPU target contract offers local discovery and rejecting preflight only;
it performs no credential lookup or network calls and cannot execute jobs.

The [local API comparison workflow](local-api-workflow.md) integrates two
registered baseline/intervention plans, explicit approval and admission, normal
worker dispatch, retained evidence, and human review in default fake-effect
tests. Normal terminal Job/owned-Pod reports now reach the admitted-attempt
ledger; success, failure, confirmed cancellation and unknown outcomes remain
distinct. A bounded, opt-in Docker Desktop fixture has additionally recorded a
real two-Job baseline/intervention run (see below); that narrow fixture result is
not general Kubernetes support or a completed scientific experiment.

**Dated live evidence (2026-10-03):** the opt-in
[`check_kubernetes_lab_two_variant_e2e.py`](../scripts/check_kubernetes_lab_two_variant_e2e.py)
Local record IDs below identify retained local history; they are not links to
downloadable artifacts or independent public proof.
The retained local evidence record `LOCAL-TWO-VARIANT-2026-10-03` records two
succeeded Jobs on the owned Docker Desktop Kubernetes cluster. Distinct retained,
non-late, hash-linked outputs yielded API-derived `sum_squares` values 385
(baseline) and 2870 (intervention); grants reconciled with zero settlement. Both
artifacts remain quarantined and scientific review is pending; validity is
`not_assessed`. This deterministic operational fixture does not establish
scientific validity, provider execution, laptop-off operation, portability to a
second environment, agent acceptance, production readiness, or researcher review.
The earlier unknown and late-upload attempts remain distinct in that local record and
are not erased by this run. See [the implementation roadmap](../IMPLEMENTATION_TASKS.md)
for the evidence ledger and remaining gates. The current verification checkpoint
reports repo-wide Ruff lint/format success and 3,043 passing tests across four
disjoint pytest shards after schema 49; a monolithic full-suite run timed out
previously, so the sharded pass is the recorded suite evidence, not a claim that
the monolithic invocation completed.

An [opt-in comparison compute pilot](comparison-pilot.md) now connects those
ordinary admission/dispatch contracts to one explicitly configured remote
Kubernetes target for two constant CPU probes. It retains private audit journals
and observed stdout artifacts, leaves human review pending, and keeps model
providers disabled. Offline tests only: no live pilot or selected-target cost,
credential, isolation, or deployment acceptance is claimed.

**Ordinary-service evidence (2026-10-04):** the installed control/worker on the
owned Docker Desktop bench now collect output without harness uploads. The
[ordinary worker runbook](ordinary-worker-collection.md) records generation-2
baseline 385 / 36 bytes and intervention 2870 / 37 bytes, preserved Job UID on
stop/restart, generation-3 late 29-byte quarantine, and generation-4
terminal-report acknowledgements plus separate human grant reconciliation. Native
review and read-only identity reconciliation confirm the same 29-byte late
artifact including its trailing newline. The earlier 28-byte summary omitted
that newline and is preserved as a correction, not erased history.
Earlier missing-UID proof and Job-list 403 histories remain unresolved rather
than erased. Scientific outcome remains null. This is laptop-local selected-target
evidence, with a supervised expiring route rather than permanent external ingress.
Separate native and two-SDK deployed-MCP client receipts pass their bounded
journeys; actual human OS-picker selection and independent-host acceptance remain
unverified. A separate fresh native Kubernetes plan/admit/dispatch-to-new-Job
journey now passed: exact human grant and worker package, an ordinary new Job,
automatic timely 385 / 36-byte output, native `retain_quarantined` review followed
by separate custody release/save, reproduction export and reconnect without
redispatch. Scientific outcome remains null; OS dialogs used a labelled fallback,
so actual human selection is still unverified. Follow [the native/operator handoff](../desktop/EXECUTION.md).
Normal client connectivity now has an installed operator entry point,
[`tessera connection-tunnel`](connection-tunnel.md), rather than requiring
the historical private route launcher. Installed default-selection authenticated
connect/reconnect passed against the owned gateway, separately from unit/help
checks. See the runbook and [actionable handoff](research-bench-handoff.md)
for start/restart commands and limits.

The pilot requires a [dedicated project workload namespace and admission
boundary](pilot-workload-isolation.md). Offline manifest rendering and opt-in
local authorization/admission dry runs are available; they neither apply resources
nor attest NetworkPolicy, filesystem quota, or live pilot success.

## Run the foundation

The [agent capability policy](agent-capability-policy.md) supports exact,
human-approved model/data/package declarations, lease/epoch/revocation fencing,
and transactional requested/approved/actual-effect audit records (schema 49).
Agents cannot grant themselves capabilities. Existing runtime adapters still
return **execution disabled** for these effects: the trusted download/install
broker now has an [offline runtime and durable journal](runtime-capability-broker.md),
but local/Kubernetes real-effect activation is blocked pending isolation enforcement.
Host/control-PVC/ambient-credential access remains denied. Verification is offline;
no live download/install support or trusted sandbox is claimed.

Strict activation-candidate, pinned-HTTPS and watchdog-protocol primitives now have
[offline tests and a blocked-activation runbook](capability-sandbox-activation.md).
An independent [local networkless Docker fixture](local-networkless-docker-fixture.md)
adds opt-in, closed harmless conformance with a separate supervisor and quarantined
fixture evidence. Default invocation has zero effects; it does not enable capability
downloads/installs or Kubernetes. Docker start now passes; live conformance remains
blocked by the pre-workload runtime-limit probe.
The opt-in [independent sandbox supervision gate](independent-sandbox-supervision.md)
adds a reciprocal external guardian and a one-case kill conformance command. Its
single live attempt still blocked at the network witness, before workload release;
exact cleanup was confirmed, but autonomy activation remains disabled.
The [namespace-bound proof](local-docker-namespace-proof.md) now blocks before
create when independent daemon-host procfs observations are unavailable. Its one
follow-up invocation on macOS/LinuxKit launched zero cases; activation stays blocked.
The [capability Kubernetes runner and installer](capability-kubernetes-runner.md)
add an exact profile, raw-observation inspection, fake-client lifecycle and bounded
hook-free wheel materialization. These are offline components, not a validated
live activation path; real effects remain blocked.

An [authenticated Kubernetes evidence collector](capability-kubernetes-collector.md)
adds explicit target/credential binding, bounded API readback and dry runs, signed
raw custody and one-use activation inspection. Its opt-in API-only CLI remains
blocked without independent runtime witnesses; collection is not live enforcement.
The [runtime witness protocol and runbook](runtime-witnesses.md) add four
mandatory authenticated CNI/quota/watchdog/admission gates, daemon-side probe
programs and an opt-in `check_capability_runtime_witnesses.py` command. Default
tests use explicit fakes. The [target-specific adapter layer](runtime-witness-targets.md)
adds Docker Desktop preflight, native mTLS/HMAC channels, Linux/watchdog primitives
and real API admission with `--live --target`. Complete native drivers, a reviewed
fixture image and defaulting compatibility are still required; live conformance
has not been demonstrated and real execution remains blocked.

The [direct Agent Sandbox adapter](agent-sandbox.md) adds authenticated
one-shot suspended/gated lifecycle commands, exact Pod-UID release fences and
artifact-placement-gated cleanup over upstream v1.0.6. It preserves batch Jobs.
Default tests are isolated; live activation still requires a verified operator
backend, strong runtime/CNI/admission witnesses and the pinned upstream CRD/controller.
Normal `serve` supports digest-approved operator configuration; explicit stopped-PVC
delivery reuses the existing read-only collector and private artifact-transfer paths.
Local byte delivery is tested with synthetic Kubernetes transport; live Sandbox PVC
collection is unverified. Input/reasoning credential delivery remains unsupported.

Docker Compose is the development/test workflow; uv manages the Python lockfile.

```sh
docker compose up --build -d --wait control
docker compose run --build --rm test
curl http://127.0.0.1:8765/health
```

For an authenticated request without printing the owner credential, copy its
header to a private temporary directory, use it, and delete it when finished:

```sh
credential_dir=$(mktemp -d)
docker compose cp control:/state/control/owner.header "$credential_dir/owner.header"
chmod 600 "$credential_dir/owner.header"
curl -H @"$credential_dir/owner.header" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: first-project' \
  -d '{"name":"First investigation"}' http://127.0.0.1:8765/projects
rm "$credential_dir/owner.header"
rmdir "$credential_dir"
```

Stop with `docker compose down`; the named volume retains the database and owner
credential. See [CONTRIBUTING.md](../CONTRIBUTING.md) for native uv setup and the development
workflow. Compose is not required for deployment; see
[installed service operations](installation.md). Tessera is MIT licensed.

The API exposes projects, task plans, revision history, approval, and labbook
reads. `PUT /tasks/{id}/plan` creates an immutable plan revision and invalidates
prior approval. `POST /tasks/{id}/approve` checks the expected revision and optional
plan digest. Mutations require an `Idempotency-Key`; reusing one with different
input returns a conflict. [Record APIs](records-api.md) describe studies,
evidence, conversations, and human decisions.
Project/task lists accept `limit` (1–200, default 100) and `offset` (default 0);
increase the offset by the number returned until the page is shorter than the limit.
Task inputs are `title`, `purpose`, `budget_cents`, and `max_duration_seconds`.
Separate execution grants reserve against project and cumulative task budgets.
Provider spending enforcement and dispatch integration remain incomplete.
Browser origins are rejected; the owner credential is for trusted local human
clients. It is not production organization authentication.

Provider activation and shared-organization collaboration are explicit future
gates, not enabled alpha features. The default remains provider-disabled with
unknown usage/billing, no API-key or billing fallback, and one local owner; see
[provider activation and collaboration gates](provider-activation-and-collaboration.md).

The operator-only `tessera astra-namespace` command can inspect/use an explicit
existing namespace with zero writes, or create one separately approved dedicated
namespace and stop at `namespace_created_control_plane_not_installed`. It never
installs a control plane or creates workloads, credentials, policies, RBAC, or
storage; see [Astra namespace lifecycle](astra-namespace-lifecycle.md).

The [desktop shell](../desktop/README.md) connects to this API and keeps bearer
credentials in its main process. It supports projects, draft plans, approval
records, labbook reading, attributed conversation threads, and a pending decision
inbox with explicit revision-bound human approval/rejection. Read-only study views
show complete variant, source, finding, correction and ledger histories, including
failed and excluded variants. Explicit authoring supports studies, accounted
variant outcomes, source assessments/corrections, finding acceptance, reasoned
reaffirmation, terminal retraction, and linked narrowing/supersession. These record human judgment and preserve
history; browsing never starts work or accepts findings.

Existing databases require an explicit `tessera migrate --state-dir PATH` before
startup; see the backup and migration instructions in CONTRIBUTING.md. Schema v49
includes credential metadata, immutable plans, research/interaction tables and
retained local input snapshots, execution admission records, artifact quarantine
and resumable upload sessions with complete output manifests. Human manifest
dispositions append digest-bound review history while preserving quarantine;
they do not release data, accept scientific results, or settle execution costs.
An additional human custody release can authorize private project-scoped artifact
manifest/byte retrieval and bounded reproduction-bundle export without changing
quarantine, input-release, result-acceptance, or publication state.
Migration retains an unambiguous local bootstrap owner and revokes other legacy
principals, which must be enrolled again as scoped, expiring credentials.

Schema v14 also records owner-approved worker registration metadata and bounded
lifecycle/heartbeat admission. It does not attest builds or capabilities, broker
credentials, dispatch providers, accept results, or fence restores; see
[worker lifecycle](worker-lifecycle.md).

Schema v47 adds local-owner-only `/operator/health` and `/operator/audit`
projections. They expose secret-free credential-admin audit records, command
lineage where a command event exists, stale-worker/reconciliation/backup alerts,
and configured project resource budgets; they do not account provider usage or
create recovery authority. See the [single-active lab operations runbook](single-active-lab-operations.md).

Schema v48 adds metadata-only dataset provenance and ingestion reviews. Upgrading
active v47 state invalidates the old backup-freshness claim; take and verify a new
backup after recording the new data. Follow the
[historical schema-50 held-restore gate](recovery.md#historical-schema-50-post-migration-verification-gate-does-not-cover-schema-55),
including for already-migrated v48 labs. A fresh backup timestamp is not proof of
a verified restore, and the drill cannot promote recovered history to writable state.

Schema v27 freezes goal and success criterion into each new immutable experiment
design revision and attaches every new conclusion/review to one complete design
snapshot. Migration preserves only the current legacy context that was actually
recorded; earlier context remains unknown instead of being reconstructed.

Schema v28 binds each new experiment attempt either to one immutable variant and
matching admitted operation/package with an attributed terminal observation, or
to an explicitly typed human-reported observation. It records cancellation desire,
observed execution, and termination confirmation separately from the human outcome;
none establishes scientific success. Earlier attempt links migrate as explicitly
`legacy_operation_unverified`, with no invented plan/package or termination proof.

Schema v29 adds a human-only append-only correction relationship for a reviewed
conclusion. It records `supersedes` or `retracts`, a reason, actor, and expected
conclusion/design revisions while retaining the original statement, evidence, and
review attribution. It neither authorizes execution nor accepts a scientific claim.

Schema v33 stores a human admitted-attempt outcome in a separate immutable record.
Late worker/provider observations remain operational history and cannot erase a
human `unknown`, failed, cancelled, or succeeded label. A succeeded or cancelled
execution label requires the bound package/operation and a confirmed attributed
exit; an unconfirmed exit remains inconclusive. A routine package refresh is not a
restart, and revoked authority accepts only matching confirmed-termination
reconciliation—not starts, renewals, replacement packages, or result acceptance.

Schema v34 retains high-frequency worker/provider samples separately from bounded
admitted-attempt semantic transition history. Detail/export show every semantic
transition and the latest observed state, opaque cursor, and timestamp, so routine
polling does not consume the 1,000-record history bound or hide uncertainty.

Schema v35 reconciles current worker and fake-provider receipt observations by
observed time. A newer or same-time contradictory `unknown`/`running` observation
blocks operational success and exposure release while preserving any human outcome.

Schema v36 adds a human-approved, provider-disabled Kubernetes dispatch/outbox
ledger. Schemas v37-v39 add context-only OpenCode delivery and immutable
operation-to-session binding records; none activates a Kubernetes client,
OpenCode provider, model, credential delivery, or artifact collection.
An exact reviewed Kubernetes remote route now queues that existing outbox in the
same transaction as its start command. A separately started fake worker can claim
and reconcile it durably; route state alone does not report worker completion.
This is offline composition, not remote deployment or provider enablement.

Schemas v40-v41 add provider-disabled Astra conversation, proposal, and
human proposal-to-design registration records. Registration creates no approval,
grant, dispatch, or execution authority. Schema v42 adds one deterministic
Explain-status path: a scoped agent may mediate exactly one bound
`read_experiment_status` call whose intent, result digest, observed time, and
answer are durable. It has no generic tool, skill, shell, provider, or model path.

Schema v43 adds empty operation-bound remote-workflow lifecycle records for new
packages: worker heartbeats, opaque checkpoints, and reconnect/outage observations.
They are operational evidence fenced to the original worker, lease, and operation;
migration does not invent runs for existing operations, authorize replacement
submission, make held history writable, or prove provider termination or
laptop-independent execution.

Schema v15 adds a human-only, immutable synthetic result checkpoint for a
reconciled, worker-confirmed operation and a complete exact quarantined manifest.
It rechecks retained bytes and declared metadata, preserves quarantine and labels
the validation/receipt limitations. It does not attest provider success, release
bytes, accept scientific findings, or activate collection/dispatch.

Schema v16 adds immutable metadata-only project skill snapshots and human-only,
worker-targeted expiring releases. Pinned plans require matching live releases at
admission, but local CPU packages and runtime reject pins until materialization
and verification are implemented. See [skill lifecycle](skill-lifecycle.md).

Schema v17 also records a bounded [confirmed workspace handoff](workspace-handoff.md):
human stop request, matching worker-confirmed exit, captured human diff, and a fresh
handback context epoch. It accepts no arbitrary filesystem path or bytes, dispatches
nothing, and does not yet mediate arbitrary writers or provide full desktop adoption.

Schema v18 adds a development-only [external-operation journal](external-operation-journal.md)
before an executable CPU package is returned. It records one deterministic
operation identity and manual-reconciliation intent, binds observations to it,
and records revocation as desired cancellation. It does not dispatch Docker from
control, accept results, release artifacts, or prove provider termination.

Schema v19 adds a separate fake-provider receipt record after an injected,
deterministic development-worker dispatch. Lost replies reconcile by stable
lookup key and retain uncertainty; receipt evidence is not real provider
dispatch, accounting, Docker execution proof, artifact release, or pilot/result
acceptance.

Schema v20 adds a human-only, append-only result-bookkeeping ledger over exact
reconciled synthetic checkpoints, manifests, retained bytes, operation receipts,
grant revisions, and epochs. It records only Tessera ownership/bookkeeping:
scientific validity remains `not_assessed`, provider success `not_established`,
and artifact release `not_performed`. It neither releases/downloads/publishes
bytes, settles cost, creates findings, nor establishes scientific truth.

Schema v21 adds human-only, metadata-only credential-access leases distinct from
Tessera authentication credentials. Each binds an opaque secret reference and
provider-neutral audience/purpose/scope to one current grant, task, worker
execution lease, and epoch. The worker access route returns only a short-lived
fake-backend handle and rechecks live authority; values never enter SQLite, API
responses, events, receipts, or diagnostics. The in-memory fake backend is test
only: production secret backends and real provider credential integration remain
unimplemented.

Schema v22 separates development worker output-slot custody from durable private
project artifact storage. Quarantine remains intake; validated development bytes
are stage/commit/reconciled to default project persistence before workspace cleanup.
Placement is not release, classification, finding/result acceptance, cost settlement,
or provider/scientific success. Legacy SQLite BLOBs receive no invented placement.
S3/IAM/KMS, public URLs, external export/move, deletion, and pilot activation remain
unimplemented; see [project artifact storage](project-artifact-storage.md).

Local [workspace adoption](workspace-adoption.md) now preserves reviewed input
bytes and their manifest, verifies them after restart, and records adoption in the
labbook. Original snapshots retain their unclassified, imported/unverified labels;
a separate human decision can release public inputs to one worker. Lease-scoped [input delivery](input-delivery.md) and verified local worker
staging are implemented. [CPU runtime integration](cpu-runtime.md) connects
packages, verified read-only input mounts and the supervisor in synthetic tests
and native Docker fixtures.

[Execution admission](execution-admission.md) now records human-reviewed input
release, project limits, grants, short-lived worker leases and reconciliation.
Unknown termination retains exposure; stale lease retries cannot restore revoked
authority. A separate lease-scoped route delivers released inputs. These routes do not
dispatch jobs or accept results.

[Artifact quarantine](artifact-quarantine.md) retains bounded, attributable
worker outputs, including late results. Chunked uploads resume after interrupted
transfers and share the approved output budget with direct uploads. Uploads remain
unclassified and cannot declare execution success, accept findings or release
reserved exposure.

Synthetic live fixtures now verify Docker isolation, read-only released inputs,
control-plane revocation, duration stops, and the native Electron renderer/bridge.
Complete artifact manifests verify the declared output set and retained bytes;
they remain quarantined. Automatic container output collection still requires
verified persistent storage limits, and full research E2E acceptance remains pending.

An explicit `tessera-worker development-run` native one-shot fixture can consume
an already issued grant with a private worker credential file, heartbeat before
lease acquisition, and use the development-only output mount. Confirmed declared
outputs transfer only into quarantine before local cleanup. It cannot issue or
reconcile human authority, release artifacts, accept results, or claim success;
see [CPU runtime](cpu-runtime.md).

A read-only worker probe can inspect an already-provisioned dedicated Linux
output mount and return the existing storage contract only when its path,
custody, mount restrictions, capacity, and UID 65532 access observations pass.
It is not wired to runtime admission or collection and rejects macOS Docker
Desktop/non-Linux host facts; decisive Linux Docker-host exhaustion and restart
evidence remains required.

## The first workflow (target)

1. Connect to an authoritative control plane, state a goal, and adopt inputs as
   needed without inventing missing provenance.
2. Register a simple CPU baseline and one bounded intervention, with inputs,
   method, expected measurements, cost, stop conditions, and outcome criteria.
3. Approve execution on an identified local or remote target. The remote path
   continues under bounded policies if the desktop disconnects.
4. Link operation provenance and retained artifacts/measurements to each attempt;
   record succeeded, failed, cancelled, or inconclusive outcomes honestly.
5. Have a human review the evidence and record a versioned conclusion. A rerun,
   new intervention, or conclusion change is separate work.

Sessions, experiments, attempts, external operations, artifacts/measurements, and
conclusions have separate lifetimes. A session ending does not end an attempt; an
operation receipt does not validate a measurement; and evidence does not become a
conclusion without human review.

## What the workspace includes

- Chat, project history, selected files, artifact previews, and change review.
- Skills authored in chat, the UI, or a repository using `SKILL.md`; optional
  scripts and resources. Runs record the exact skill version used.
- A labbook linking conclusions to evidence and the wider investigation,
  including failed, excluded, and abandoned work.
- A decision inbox for direction, cost, access, methodology, and publication.
- Configurable agent harnesses and compute targets, with supported capabilities
  and limitations shown before dispatch.
- Named control-plane connections showing the signed-in organization, project,
  data owner, and execution destinations.

The scientific model covers literature, theory, observation, analysis, simulation,
and experiments. Domain-specific methods and validation belong in research
packages. The first pilot uses one existing computational workflow; it does not
attempt to support every domain or infrastructure combination.

## System boundaries

| Part | Responsibility | Runs where |
|---|---|---|
| Desktop client | Chat, skills, labbook, review and approvals | Researcher's machine |
| Control plane | Identity/access, tasks, budgets, research records and credential brokering | Local processes/cluster, organization infrastructure, or Tessera hosting |
| Execution plane | Agent supervisors, harnesses, tools and experiment jobs | Same machine, other hosts, clusters or cloud compute |

The design describes three future deployment modes; only the local developer
workflow is currently supported:

- **Personal:** local developer processes are the only current scope. A local
  cluster such as Minikube is not a supported execution or deployment claim.
- **Organization-managed:** planned; no production remote control-plane claim.
- **Tessera-hosted:** planned; no hosted offering exists.

An opt-in [Kubernetes coordinator and dispatch sample](../deploy/kubernetes/README.md)
adds a provider-disabled coordinator process and a separately opt-in dispatch
service. It is an operator-provisioned composition, not a self-contained lab
deployment: it requires a pinned image, scoped credentials, target binding, an
operator-provided TLS gateway, and real allowed/denied network probes. It does
not prove laptop-independent operation, provider execution, or deployment across
diverse environments. Research corrections are recorded atomically; when a
variant result is recorded, the finding is marked `needs_review` for human review.
That ledger behavior is not scientific validation or proof of a result.

Each project has one authoritative control plane. Organization isolation covers
data, credentials, and operations; a Kubernetes namespace alone is not sufficient.
Switching desktop connections does not move projects or transfer credentials.

OpenCode is an optional, pinned harness behind Tessera's harness contract; its
sessions and jobs remain independently managed. Tessera—not OpenCode—authorizes
plans, approvals, budgets, leases/epochs, credentials/access, artifacts, provider
operations, result acceptance, and labbook records. OpenCode permission prompts,
tools, MCP, skills, session events, provider auth, and UI are not Tessera
authority boundaries. Node setup/enrollment is separate from research control.
Synthetic adapter POC and local HTTP/SSE seam fixtures are implemented, but no
real OpenCode compatibility result or provider activation exists; the pilot is
not complete. See the [compatibility matrix](compatibility-matrix.md).

### Governed remote agents: contract versus available proof

The [design contract](../DESIGN.md#53-governed-agent-identity-reasoning-and-peer-transports)
defines distinct agent identity, human-issued bounded grants, subset-only
delegation, correlated notebook provenance, supervised real reasoning, session
stdio MCP, and independent A2A peers. The installed Python distribution exposes
`tessera-mcp` with the official SDK pinned to 1.26.0; see the
[remote-agent install/runbook](remote-agent-platform-install.md) for the
actual local stdio-to-control-service arrangement and prerequisites.

These interfaces do not by themselves mean the integrated platform is accepted.
The latest full integrated remote-agent journey receipt reports schema 52 on a fresh owned Docker
Desktop namespace/project: all 16 configured workflow assertions passed, an
on-time ordinary Job reached supported control `succeeded`, its 36-byte output
was proven and joined in the notebook, and two installed reference peers each
drained four records. Public journey details are in the
[installed runbook](remote-agent-platform-install.md)
(evidence identifier `REMOTE-AGENT-PLATFORM-2026-10-04`).
The output remains human-quarantined with custody released; scientific validity
was not assessed. This is local installed evidence, not real critic reasoning,
independent-host/laptop-off, Linux/systemd, or native-picker proof. The receipt
keeps `reasoning_verified`, `independent_host_verified`, `laptop_off_verified`,
and `integrated_acceptance_verified` false. Historical cancelled/quarantined and
quota-denied/unknown attempts remain preserved; the successful fresh project does
not reconcile or erase them. The earlier 3,454-pass/1-skip baseline belongs to an
older digest; the later candidate's exact source gate is summarized in the
[release evidence note](release-evidence-intent9.md). A deterministic/
provider-disabled coordinator is not reasoning evidence. See the
[implementation journal](remote-agent-implementation-journal.md) and
[installed journey record](remote-agent-platform-journey.md) for check-by-check
status and remaining acceptance.

Subsequent schema-53/54 source checkpoints add the typed generated-public-output
registry/capture family and a bounded ordinary-Docker worker HTTP/service/CLI
adapter; see the [generated-output contract](generated-public-output-interface.md),
[public-capture boundary](public-capture-interface.md), and
[ordinary-Docker HTTP binding](ordinary-docker-http-binding.md). Those source
tests did not establish a schema-54 installed image, generated model reasoning, or
live garage-host deployment. The later schema-55 `0.1.0rc2` candidate passed its
own exact-source gate, as summarized below; the current changed tree still needs a
fresh freeze and full-source gate. The ordinary Docker worker is not a host-shell or
owner-token proxy. The journal records the remaining integration gaps.

The schema-55 `0.1.0rc2` candidate has a fresh installed-wheel and local native
slice. Its exact source/archive/wheel hashes, source-gate results, synthetic-data
limits, and unverified acceptance boundaries are in the
[sanitized candidate evidence note](release-evidence-intent9.md). This supersedes
the schema-52 installed receipt above only for the bounded checks described there;
it does not establish production enrollment, real model reasoning, scientific
validity, or off-host/device acceptance. Ongoing CLI/factory and documentation work
requires a fresh candidate inventory/build and affected gates. See the
[release checklist](release-checklist.md) and
[runtime-admission contract](runtime-admission-v2.md). Operator bootstrap and
upgrade limits are in [runtime operator setup](runtime-operator-setup.md); the
bounded tabular workload is described in [tabular starter](tabular-starter.md).

Closing Electron does not stop the separately running control plane. If its
machine sleeps or becomes unreachable, workers follow their disconnect and
credential-expiry policies. Stopping an agent session does not necessarily stop
its jobs; the UI distinguishes requested stops from confirmed termination and
reports unresolved cost exposure.

Skills are instructions, not permissions. Authorization applies to files, tools,
models, conversations, jobs, secrets, and publication. Credential storage is
provider-neutral: the control plane can broker access to an existing secrets
service without storing its values. Secrets do not belong in prompts, skills,
or evidence bundles. Local deployments also require authentication.

GitHub is an optional publication destination, not the job queue or labbook.
A separate publisher may open research-scoped PRs; research agents cannot modify
platform code or policies.

## Build scope

The next thin vertical slice is one public/simple CPU workflow: baseline plus one
intervention, remote execution that survives desktop disconnection, linked
evidence, succeeded/failed/cancelled/inconclusive outcomes, and human review.
It will compare total researcher effort against the baseline; a proposed 25%
effort reduction is a hypothesis to measure, not current evidence or a product
claim. Freeze broad provisioning/cloud, skills, and desktop expansion until this
slice has accepted evidence. Security and evidence checks apply from the first
real-data run.

**Approved direction, with one narrow local bootstrap:** local CPU remains the first target
while Kubernetes/Minikube preparation proceeds separately. Kubernetes target
attachment requires explicit target identity rather than an ambient kube context;
Minikube bootstrap and control-plane provisioning are distinct operator workflows.
Humans approve immutable provisioning and execution plans. Planned composable
environment/execution adapters and credential store/delivery adapters may support
an authorized local environment source and explicitly authorized Kubernetes Secret
materialization; no skill can approve those actions or use raw `kubectl` or cloud
credentials. The explicitly human-approved `tessera-dev` local bootstrap is
documented in [Minikube provisioning](minikube-provisioning.md); it does
not attach a Kubernetes target or authorize execution. Automatic destructive
teardown is not authorized. See the
[compatibility matrix](compatibility-matrix.md) for the current evidence.

The separate [Astra operator setup](astra-operator-setup.md) prepares one
explicit Docker Desktop Kubernetes profile from a digest-bound human plan. It
retains only allowlisted resources and does not attach a target, create research
authority, enroll a worker, start a workload, create a Secret, or invoke a
provider. Its documentation also defines an explicit, standalone in-cluster
NetworkPolicy/PVC conformance probe; the probe is opt-in, cleanup-bounded, and
does not establish a supported execution target or storage quota enforcement.

Cluster packaging, organization login/administration, paid hosting, additional
runtimes, GPUs, schedules, and automatic memory follow the pilot. Compare it with
the researcher's current workflow, including setup, review, verification, and
cost—not just agent execution speed.

An explicitly owner-authorized, remote Kubernetes OpenAI ChatGPT OAuth reconnect
harness is available as a default-disabled operational fixture. It requires a
pre-existing remote runner deployment, an opaque owner-bound runtime reference,
preflight, and a digest-bound one-call confirmation; it remains outside pytest/CI
and does not relabel provider-disabled records or claim a supported provider
integration. See [remote Kubernetes OAuth reconnect E2E](remote-kubernetes-oauth-reconnect-e2e.md).

See [DESIGN.md](../DESIGN.md) for the architecture, implementation sequence,
failure handling, and release tests.

## Developer-alpha boundaries

Start with the [local alpha quickstart](quickstart.md). Current source uses schema 56;
the historical installed candidate c77 used schema 55. The documented component and
synthetic evidence does not make a production workflow. There is no supported
production remote-control offering, real model-provider integration or OpenCode
compatibility result. The selected ordinary Kubernetes bench above is not general
Kubernetes/production support. The tested developer environments are locked Python
3.11+ and Docker Compose/native fixtures; no production OS support matrix is
declared. Local object-bundle backups copy committed project bytes and restore
them into a permanently read-only held history. Backup/restore do not provide
retention automation, owner/account recovery, operational failover, or fencing of
the original service/workers/jobs. See [privacy](privacy.md),
[support](support.md), [release checklist](release-checklist.md), and
[SECURITY.md](../SECURITY.md). See [LICENSE](../LICENSE) for MIT terms; outside
contributions are welcome under [CONTRIBUTING.md](../CONTRIBUTING.md).
