# Tessera

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

**Status: source-only developer alpha.** This repository is for local developer
evaluation, not a production release or hosted service. The local API supports scoped credentials, versioned task plans,
human approval records, durable conversations and decisions, and a research
ledger with complete study exports and source corrections. Approval records intent
only; they do not grant execution. A desktop shell, safe file inventory, structured
transport, and CPU supervisor have component tests. Native desktop runtime and
CPU isolation fixtures pass. Live experiment dispatch and the full pilot remain
incomplete. See
[implementation progress](docs/implementation-progress.md) for acceptance limits.

## Run the foundation

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
credential. See [CONTRIBUTING.md](./CONTRIBUTING.md) for native uv setup and the development
workflow. Compose is not required for deployment. No license has been selected.

The API exposes projects, task plans, revision history, approval, and labbook
reads. `PUT /tasks/{id}/plan` creates an immutable plan revision and invalidates
prior approval. `POST /tasks/{id}/approve` checks the expected revision and optional
plan digest. Mutations require an `Idempotency-Key`; reusing one with different
input returns a conflict. [Record APIs](docs/records-api.md) describe studies,
evidence, conversations, and human decisions.
Project/task lists accept `limit` (1–200, default 100) and `offset` (default 0);
increase the offset by the number returned until the page is shorter than the limit.
Task inputs are `title`, `purpose`, `budget_cents`, and `max_duration_seconds`.
Separate execution grants reserve against project and cumulative task budgets.
Provider spending enforcement and dispatch integration remain incomplete.
Browser origins are rejected; the owner credential is for trusted local human
clients. It is not production organization authentication.

The [desktop shell](desktop/README.md) connects to this API and keeps bearer
credentials in its main process. It supports projects, draft plans, approval
records, labbook reading, attributed conversation threads, and a pending decision
inbox with explicit revision-bound human approval/rejection. Read-only study views
show complete variant, source, finding, correction and ledger histories, including
failed and excluded variants. Explicit authoring supports studies, accounted
variant outcomes, source assessments/corrections, finding acceptance, reasoned
reaffirmation, terminal retraction, and linked narrowing/supersession. These record human judgment and preserve
history; browsing never starts work or accepts findings.

Existing databases require an explicit `tessera migrate --state-dir PATH` before
startup; see the backup and migration instructions in CONTRIBUTING.md. Schema v34
includes credential metadata, immutable plans, research/interaction tables and
retained local input snapshots, execution admission records, artifact quarantine
and resumable upload sessions with complete output manifests. Human manifest
dispositions append digest-bound review history while preserving quarantine;
they do not release data, accept scientific results, or settle execution costs.
Migration retains an unambiguous local bootstrap owner and revokes other legacy
principals, which must be enrolled again as scoped, expiring credentials.

Schema v14 also records owner-approved worker registration metadata and bounded
lifecycle/heartbeat admission. It does not attest builds or capabilities, broker
credentials, dispatch providers, accept results, or fence restores; see
[worker lifecycle](docs/worker-lifecycle.md).

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

Schema v15 adds a human-only, immutable synthetic result checkpoint for a
reconciled, worker-confirmed operation and a complete exact quarantined manifest.
It rechecks retained bytes and declared metadata, preserves quarantine and labels
the validation/receipt limitations. It does not attest provider success, release
bytes, accept scientific findings, or activate collection/dispatch.

Schema v16 adds immutable metadata-only project skill snapshots and human-only,
worker-targeted expiring releases. Pinned plans require matching live releases at
admission, but local CPU packages and runtime reject pins until materialization
and verification are implemented. See [skill lifecycle](docs/skill-lifecycle.md).

Schema v17 also records a bounded [confirmed workspace handoff](docs/workspace-handoff.md):
human stop request, matching worker-confirmed exit, captured human diff, and a fresh
handback context epoch. It accepts no arbitrary filesystem path or bytes, dispatches
nothing, and does not yet mediate arbitrary writers or provide full desktop adoption.

Schema v18 adds a development-only [external-operation journal](docs/external-operation-journal.md)
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
unimplemented; see [project artifact storage](docs/project-artifact-storage.md).

Local [workspace adoption](docs/workspace-adoption.md) now preserves reviewed input
bytes and their manifest, verifies them after restart, and records adoption in the
labbook. Original snapshots retain their unclassified, imported/unverified labels;
a separate human decision can release public inputs to one worker. Lease-scoped [input delivery](docs/input-delivery.md) and verified local worker
staging are implemented. [CPU runtime integration](docs/cpu-runtime.md) connects
packages, verified read-only input mounts and the supervisor in synthetic tests
and native Docker fixtures.

[Execution admission](docs/execution-admission.md) now records human-reviewed input
release, project limits, grants, short-lived worker leases and reconciliation.
Unknown termination retains exposure; stale lease retries cannot restore revoked
authority. A separate lease-scoped route delivers released inputs. These routes do not
dispatch jobs or accept results.

[Artifact quarantine](docs/artifact-quarantine.md) retains bounded, attributable
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
see [CPU runtime](docs/cpu-runtime.md).

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
not complete. See the [compatibility matrix](docs/compatibility-matrix.md).

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
documented in [Minikube provisioning](docs/minikube-provisioning.md); it does
not attach a Kubernetes target or authorize execution. Automatic destructive
teardown is not authorized. See the
[compatibility matrix](docs/compatibility-matrix.md) for the current evidence.

Cluster packaging, organization login/administration, paid hosting, additional
runtimes, GPUs, schedules, and automatic memory follow the pilot. Compare it with
the researcher's current workflow, including setup, review, verification, and
cost—not just agent execution speed.

See [DESIGN.md](./DESIGN.md) for the architecture, implementation sequence,
failure handling, and release tests.

## Developer-alpha boundaries

Start with the [local ledger quickstart](docs/quickstart.md) and its synthetic
example. Schema v34 is the current local schema, but the documented component and
synthetic evidence does not make a production workflow. There is no supported
remote control plane, real provider integration, OpenCode compatibility result,
or Kubernetes execution path. The tested developer environments are locked Python
3.11+ and Docker Compose/native fixtures; no production OS support matrix is
declared. Backups and held restore are limited local recovery tools and do not
provide retention automation, owner/account recovery, object-bundle recovery, or
fencing of the original service/workers/jobs. See [privacy](docs/privacy.md),
[support](docs/support.md), [release checklist](docs/release-checklist.md), and
[SECURITY.md](SECURITY.md). No license has been selected.
