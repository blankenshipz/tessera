# Tessera

Tessera is a desktop workspace for directing local and remote research agents.
A researcher works with a desktop agent to plan an investigation, author reusable
skills, run experiments on approved infrastructure, and review findings in a
persistent labbook.

A tessera is a piece of a mosaic. The name reflects how observations, experiments,
and different perspectives come together to form a research finding.

The desktop, control plane, and execution workers run independently. Here,
“remote” means outside Electron: all three can run on one machine or across
separate machines and clusters.

**Status:** The local API supports scoped credentials, versioned task plans,
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
startup; see the backup and migration instructions in CONTRIBUTING.md. Schema v21
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

1. Connect to a personal, organization-managed, or Tessera-hosted control plane.
   Create a project or adopt an existing repository, notebook, or results folder;
   imported work keeps its provenance gaps.
2. Ask the desktop agent to investigate a question or help define the next
   activity, including inputs, method, expected outputs, cost, and stop conditions.
3. Author or select a skill, review the plan and access requested, and approve
   bounded execution on one local or remote target.
4. Ask the remote agent questions through the desktop agent. Inspect progress,
   revise work, resolve decisions, or stop execution and take over editing.
5. Follow the labbook: attempts, sources, findings, alternatives, decisions,
   costs, and corrections. Revisit past conclusions without rerunning anything.
6. Review and export the result, including negative or inconclusive outcomes.
   A rerun or new branch is a separate authorized activity.

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

Three deployment modes use the same authenticated API:

- **Personal:** a separately running local control plane, initially as processes;
  a local cluster such as Minikube is another deployment option.
- **Organization-managed:** the organization operates the control plane and
  connects its identity, secrets, storage, and compute services.
- **Tessera-hosted:** a planned paid service operates the control plane;
  customers can retain their own workers, compute accounts, and secrets backend.

Each project has one authoritative control plane. Organization isolation covers
data, credentials, and operations; a Kubernetes namespace alone is not sufficient.
Switching desktop connections does not move projects or transfer credentials.

OpenCode is an optional, pinned harness behind Tessera's harness contract; its
sessions and jobs remain independently managed. Tessera—not OpenCode—authorizes
plans, approvals, budgets, leases/epochs, credentials/access, artifacts, provider
operations, result acceptance, and labbook records. OpenCode permission prompts,
tools, MCP, skills, session events, provider auth, and UI are not Tessera
authority boundaries. Node setup/enrollment is separate from research control.
The adapter POC and compatibility testing remain unimplemented; the pilot is not
complete.

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

Start with one user, a separately runnable process-based control plane, one
supported harness, one worker supervisor, one CPU target, and one credential
integration. The desktop connects over an authenticated API even on the same
machine. Prove adoption, skill authoring, bounded execution,
human intervention, labbook review, and portable export. Security and evidence
checks apply from the first real-data run.

Cluster packaging, organization login/administration, paid hosting, additional
runtimes, GPUs, schedules, and automatic memory follow the pilot. Compare it with
the researcher's current workflow, including setup, review, verification, and
cost—not just agent execution speed.

See [DESIGN.md](./DESIGN.md) for the architecture, implementation sequence,
failure handling, and release tests.
