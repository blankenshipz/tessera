# Contributing to Tessera

Tessera is licensed under the [MIT license](LICENSE). Outside contributions are
welcome: discuss substantial design changes first, preserve explicit authority
boundaries, and include focused tests. Only submit material you have the right to
license under MIT. Acceptance is subject to maintainer review; this does not
authorize an agent to open, merge, commit, or push without user instruction.

## Setup

Docker Compose is the default development and test environment. Install Docker
with Compose, then run from the repository root:

```sh
docker compose up --build -d --wait control
docker compose run --build --rm test
docker compose down
```

The control service listens at `http://127.0.0.1:8765`. Its named volume retains
state across container recreation; `docker compose down` preserves it. Rebuild
after source changes. The test service has no network or control-state volume;
it uses temporary state and runs lint, formatting checks, and pytest.
Compose is only a developer workflow, not a deployment requirement.
CI runs style checks separately and four disjoint file-based test shards to avoid
the previously observed monolithic-suite timeout. Reproduce all shards locally:

```sh
for shard in 0 1 2 3; do
  .venv/bin/python dev/pytest_shard.py --index "$shard" --count 4 -q || exit
done
```

Every `tests/**/test_*.py` file belongs to exactly one shard; no test is deselected.
This is a full-suite alternative, not permission to omit a failing shard.

For native development, install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and Python 3.11 or newer:

Control-plane SQLite must expose built-in `json_extract` with both
`SQLITE_DETERMINISTIC` and `SQLITE_INNOCUOUS`. Python version alone does not establish
this capability. Initialization, storage connections and standalone recovery fail
closed before state writes on incompatible builds, with an actionable error.
The pinned control-plane image uses Python 3.11 Trixie; its Linux amd64 base was
probed with SQLite 3.46.1 and `trusted_schema=OFF`. The image pin is a multiarch
index; other architectures are available upstream but have not been tested here.
This is a capability requirement, not a claim about the first compatible SQLite
release. Native tests additionally require an existing C compiler, `patch`, and
`/bin/ps` (provided by `procps` on Debian) for exact owned-process identity;
the development-only Docker target supplies those tools and the locked setuptools
build backend. Hardened lab/remote runtime targets do not include these test tools.

```sh
uv sync --locked --extra mcp
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
uv run --locked tessera init --state-dir .tessera
uv run --locked tessera serve --state-dir .tessera --port 8765
```

Initialize once; initialization refuses existing directories. Native serving
binds only to loopback. Keep the owner header private and never pass it to an
agent: this first slice identifies a human by possession of that bearer token.
It does not yet provide separate human-session verification or token rotation.
For installing the Python distribution outside a checkout and operating separate
services, see [installation and operations](docs/installation.md). Desktop remains
a separate package; Python wheel installation does not install Electron.
For the governed remote-agent identity, MCP stdio, and still-open A2A/reasoning
acceptance boundaries, see the [design contract](DESIGN.md#53-governed-agent-identity-reasoning-and-peer-transports)
and [remote-agent install/runbook](docs/remote-agent-platform-install.md). The
runbook labels unavailable entrypoints and does not treat an installed client or
synthetic fixture as a completed remote-agent workflow.

`pyproject.toml` declares dependencies and `uv.lock` pins the resolved environment.
Use `uv lock` after intentional dependency changes and rerun all checks. Docker
uses `uv sync --locked`, following the [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/).
The ignored `.venv/` and `.tessera/` directories are for local development;
credentials and runtime data must stay out of Git and image build contexts.

## Initial implementation workflow

The [transferable execution-plane monitor](docs/execution-monitor.md) is separately
opt-in: `.venv/bin/python -m tessera_monitor` and
`.venv/bin/python scripts/check_execution_monitor.py` default to zero effects.
`--check-prerequisites` on the latter is read-only. Never run its explicit single
Docker case unless daemon-host native observation and the local pinned image are
available; missing host privileges are a fail-closed prerequisite, not permission
to install a universal privileged agent. Default tests use fake adapters/private
sockets and do not run Docker, Kubernetes, Salad or paid workloads.

The newer [closed networkless Docker Desktop fixture](docs/local-networkless-docker-fixture.md)
runs with `.venv/bin/python scripts/check_local_docker_fixture.py` (zero effects by
default). Its explicit `--run`, already-local digest image and fresh evidence path
are separate from the older CPU fixtures below. It never pulls/builds images or
installs dependencies; unknown enforcement or cleanup blocks conformance. Read
its runbook for the independent supervisor, recovery and current live blocker.

For an opt-in live CPU isolation/cancellation fixture, first fetch the exact
worker fixture Python image, then run the script against your local Docker daemon.
This Bookworm worker/stdlib-guard pin is deliberately unchanged: those images do
not host control-plane SQLite recovery. Root lab/dev and remote control-plane
images instead share the compatible Trixie pin; changing a control-plane base does
not reapprove a worker artifact or imply a new live fixture run:

```sh
docker pull python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
.venv/bin/python scripts/check_cpu_runtime.py
.venv/bin/python scripts/check_control_runtime.py
.venv/bin/python scripts/check_development_e2e.py
TESSERA_RUN_SYNTHETIC_COMBINED_E2E=1 .venv/bin/python scripts/check_combined_synthetic_e2e.py
```

The scripts run synthetic probes in networkless, non-root containers and clean
up only their own labeled containers. They require Docker socket access and run
outside the isolated Compose test service. The second starts a temporary loopback
control service, creates fixture-only authority through storage commands, transfers
released input bytes over HTTP, and records actual Docker observations. They do
not exercise automatic artifact collection or a research investigation. Unknown
cleanup is reported with the fixture container name for reconciliation.

`check_development_e2e.py` additionally runs the explicitly opt-in native
development worker against temporary loopback control state. It uses a fixture
worker registration and grant created directly by the fixture, writes a declared
synthetic output, and verifies quarantine-only transfer before workspace cleanup.
It is not a pilot storage, provider, or result-acceptance check.

`check_combined_synthetic_e2e.py` is an additional explicit opt-in. It runs the
existing in-process OpenCode harness/HTTP-SSE fixtures followed by the native
development-worker fake-provider Docker fixture. It has no real OpenCode server,
provider/model call, or provider credential path; see the compatibility matrix
for its exact scope and activation gates.

On a provisioned Linux output-slot mount, the read-only inspection command is
`.venv/bin/python scripts/check_persistent_output_storage.py PATH`. It does not
mount, write, or invoke Docker; a passing host inspection is not the required
Docker byte/inode exhaustion or restart evidence for automatic collection.

For the opt-in native desktop fixture, after installing the locked dependencies, run
`.venv/bin/python scripts/check_desktop_control.py` on a native graphical host
for the synthetic desktop-to-control fixture. It starts a temporary loopback
service and opens the sandboxed Electron application. A test-only file-picker
stub selects the fixture credential; actual renderer, IPC, authentication and
decision routes record an explicit synthetic approval. It verifies that no
execution grant or lease was created. It also browses the complete 80-variant
synthetic study and its correction history without changing any research record.
A second pass restores the backup, browses the same ledger with a fresh credential,
and verifies that write controls are disabled. Both passes save the complete
study JSON through a native dialog and compare it with the control ledger. The
active pass also creates a study, registers two variants, records negative
outcomes, closes the accounted study, and exports its full ledger. These manual
records have no execution receipt or provider work. It also exercises source
assessment, candidate selection, explicit finding acceptance, correction and
downstream review through the UI. No real project or credential is used.

The maintainer has paused PRs while the initial implementation takes shape.
When commits are explicitly requested, use logical, tested commits on `main`;
PRs can resume when requested. Commit and push each require explicit user
authorization, subject to higher-priority policy; neither authorizes the other.

1. Inspect the branch and existing edits; preserve unrelated local work. When
   integrating on `main`, verify its baseline without silently switching or resetting.
2. Keep one reviewable behavior change per task (and per authorized commit).
   Reference the relevant design section and add a decision note if changing a
   boundary or supported behavior.
3. Add tests for the behavior and its failure cases. Default suites use temporary
   directories and synthetic data, without real tokens, research inputs or paid calls.
   Explicitly requested live/provider tests are separate opt-in harnesses: implement
   them when asked and execute when requested, with bounded calls/tokens/time/retries.
   Use existing authorized credentials via runtime references; never commit or print
   secrets. Missing live prerequisites do not prevent independent harness work.
4. Use the verification gates below. Update the README if runnable behavior or
   setup changes.
5. Leave changes uncommitted unless explicitly authorized to commit; then stage
   only intended paths. Push only with separate explicit authorization. State
   validation performed, limitations, and any migration or security impact.
6. For an authorized push/PR, check CI results and fix in-scope failures. Report
   unrelated failures to their owner; do not bypass required checks.

If a PR is requested, use a feature branch and the repository PR template; merging
still requires the maintainer's instruction. Repository rules/branch protections
depend on the hosting plan and are not assumed configured.

### Verification gates

- **Task acceptance:** derive targeted tests and relevant lint/format checks from
  the requested behavior; clarify only material ambiguity. Stop when those acceptance conditions pass; report broader
  verification separately. For docs/instruction-only changes, check the scoped
  diff, references, consistency and skill frontmatter/discovery where affected;
  runtime tests are not required unless behavior or an explicit assignment warrants them.
- **Integration acceptance:** the integrating implementer owns the full gate. For
  multi-worker work, the parent owns it or explicitly assigns an integrator. Before
  declaring integration ready, run `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, and `.venv/bin/pytest` (or the isolated Compose
  test service). Record failures/unrun checks; targeted success is not full-suite
  success. Before a full gate, check for concurrent work affecting the tested tree;
  coordinate when possible and report observed changes as a validation limitation.
  Reproduce unrelated failures boundedly and hand them to their owner,
  rather than silently expanding the task.
- **Opt-in fixtures:** Docker runtime, native graphical desktop, Kubernetes and
  combined E2E fixtures are separate gates requiring explicit scope, prerequisites
  and permission for their effects. Neither gate above implicitly authorizes them.
  Existing user authorization satisfies this scope requirement; do not repeatedly
  request it. Ask only for missing prerequisites or additional effects outside the
  authorized scope. Keep synthetic, live operational and scientific evidence distinct.

## Engineering boundaries

Read DESIGN.md for requirements; do not implement the entire roadmap at once.
Use explicit schemas and errors, bounded queries, tenant-scoped lookups, and
transactional state/event writes. A valid file or successful agent message is not
proof of a completed experiment. Test stale revisions, duplicate commands,
unauthorized access, and restart behavior when they touch your change.

Avoid speculative adapters and abstractions. New infrastructure or billable
providers outside the requested scope need a separate proposal. An explicit request
to implement a live/provider test authorizes that implementation; a request to run
it authorizes a bounded run within the stated scope, not unrelated provisioning,
credit purchases, or top-up. Research runtime skills must never edit the
platform, policy, or CI; repository-development skills have a different purpose.
Kubernetes/Minikube bootstrap, target attachment, control-plane provisioning, and
credential delivery are separate planned workflows: do not rely on the current
kube context, raw `kubectl`/cloud credentials, or automatic teardown. Do not turn
a human plan approval into authorization to provision or materialize a Secret.

## Security reports

Do not put credentials or sensitive research data in issues or PRs. Follow
[SECURITY.md](SECURITY.md): maintainers must configure a private reporting channel
before accepting sensitive reports. Automated tests must not target systems outside
the assigned repository. A public security-reporting channel and license are
release prerequisites, not implemented promises.

## Local schema upgrades

Stop the control service and back up its private state directory before an upgrade.
Startup refuses older schemas rather than migrating them implicitly. For native
state, run `uv run --locked tessera migrate --state-dir .tessera`, then restart.
The command is transactional and safe to repeat. Future schema versions are
rejected; do not run an older binary against newer state.

For the development Compose volume, stop `control`, rebuild, then run
`docker compose run --rm control tessera migrate --state-dir /state/control`
before `docker compose up -d --wait control`. This preserves the original owner
credential and labbook. Do not use `down -v` as an upgrade procedure.

## Local backup and held recovery

Schema 11 provides `tessera backup --state-dir SOURCE --output NEW_BACKUP` and
`tessera restore --backup BACKUP --state-dir NEW_STATE`. Backup uses SQLite's
online consistent-copy API and includes retained database BLOBs; restore creates a
new permanently read-only held history copy with a fresh owner header. Read [the
recovery runbook](docs/recovery.md) before using either command. It explains the
1 GiB/30-second cooperative limits and why restore does not stop the original
control service, workers, or provider jobs.

Schema 12 adds terminal finding retraction. Run `tessera migrate --state-dir STATE`
explicitly after taking a backup; startup refuses older schemas. The migration
rebuilds findings and their citation tables in one transaction, retaining IDs,
revisions, attribution, and links. Backups retain their schema version; restore
requires a backup matching the running control schema. Restore older backups
using the matching Tessera release before explicitly migrating the held state.

Schema 13 adds linked finding versions for narrowing/supersession. Its explicit
migration preserves existing finding columns, supplies null version metadata for
older findings, and retains citation links. New replacement candidates link to
historical findings; acceptance and scientific review are separate actions.

Schema 14 adds the worker registry. Its explicit migration preserves v13 records
and adds no inferred registrations; existing workers must be registered by the
local owner before they can acquire or renew leases.

Schema 15 adds immutable synthetic result checkpoints. Its explicit migration
preserves v14 records and adds no inferred checkpoints; retained manifests remain
quarantined and unclassified.

Schema 16 adds metadata-only skill snapshots and worker-targeted release records.
It adds no inferred releases; local CPU skill materialization remains unavailable.

Schema 17 adds tenant-scoped confirmed workspace handoff records. It adds no
inferred handoffs, filesystem writes, provider dispatch, or result acceptance.

Schema 18 adds durable external-operation intent/lifecycle records for returned
development-local CPU packages. The migration creates no inferred operations or
provider receipts for v17 leases/observations; read the external-operation journal
note before relying on recovery or reconciliation behavior.

Schema 19 adds empty-by-default fake-provider receipt records. The development
E2E injects its deterministic fake provider before the existing Docker adapter;
its receipt is operational test evidence, not Docker/provider accounting or
pilot acceptance. Migration never backfills v18 operations with receipts.

Schema 20 adds an empty human result-bookkeeping ledger. It never infers an
acceptance from v19 checkpoints, manifests, receipts, or reconciled grants.

Schema 21 adds empty metadata-only credential-access leases. It does not infer a
lease from existing Tessera authentication credentials, workers, grants, or v20
operations, and it never migrates or stores a secret value.

Schema 22 adds empty project-storage bindings, placement, and storage-operation
metadata. Existing SQLite artifact BLOBs remain legacy with no invented location.
Back up before migration; current local filesystem backups use a versioned object
bundle for committed objects and preserve SQLite-only backups for legacy BLOBs.

Schema 23 records project-object intent and reconciliation separately from SQLite
authority commits. It preserves v22 placements and does not infer locations for
legacy BLOBs.

Schema 24 adds project-object retention and recovery metadata. It preserves v23
placements, defaults every migrated object to active project retention, and does
not schedule deletion. Explicit tombstone/delete intent remains fail-closed for
held, referenced, checkpointed, or accepted object custody.

Schema 25 adds an empty, tenant-scoped goal-to-decision ledger: versioned baseline
and intervention designs, attempts linked to existing operation/artifact evidence,
manual measurements, and human-reviewed conclusions. It does not backfill prior
studies, operations, artifacts, measurements, outcomes, or scientific validity.

Schema 26 indexes existing attempt-artifact and measurement-artifact links as
tenant-scoped retention references. New placed evidence links require active
project-object retention; tombstoned, delete-intent, degraded, and deleted objects
are rejected. Legacy SQLite-only artifacts remain explicitly `legacy_unplaced`
evidence and are not assigned invented project-storage custody.

Schema 27 stores goal and success criterion in each newly registered immutable
experiment design revision. It binds each new conclusion and its review to one
design snapshot. Migration copies the current projection only to its matching
current design; earlier goal/criterion context that was never recorded remains
explicitly unknown rather than inferred.

Schema 28 adds typed experiment-attempt provenance. New operation-backed attempts
must match an immutable variant's recorded task plan and an attributed terminal
operation observation; human-reported observations are explicit and operationless.
Migration preserves v27 attempt links as `legacy_operation_unverified` rather than
inventing a package, terminal observation, cancellation outcome, or scientific success.

Schema 29 adds an empty append-only reviewed-conclusion correction lineage. A human
can record one explicit `supersedes` or `retracts` relationship with reason,
attribution, and reviewed design snapshot fencing; it preserves the original
reviewed conclusion and creates no execution authority or scientific acceptance.

Schema 30 adds empty human-admitted experiment execution bindings. A binding is
created before a worker package can carry its immutable experiment/design/variant
identity into one external operation; it does not backfill old operations, turn a
package into dispatch, or infer a result or scientific validity.

Schema 31 repairs the v30 fresh-bootstrap omission of the immutable
experiment-operation binding index. It preserves all ledger data and makes fresh
and migrated v30 states eligible for the same fail-closed backup and held-restore
validation.

Schema 32 adds empty evidence/measurement custody tables keyed by existing
admitted-attempt IDs. It does not convert historical operation-backed or explicit
human-reported attempts, infer evidence, or resolve an uncertain execution.

Schema 33 separates immutable human admitted-attempt outcomes from mutable worker
and provider observations. It preserves every legacy outcome label, records no
invented operational detail for migrated rows, and allows post-revocation
reconciliation only for the original worker/lease/operation's confirmed exit.

Schema 34 keeps high-frequency worker/provider samples in their operational
journal. Admitted-attempt detail and export retain every semantic transition plus
the latest observed state, opaque cursor, and timestamp without consuming their
1,000-record semantic-history bound during routine polling.

Schema 35 adds receipt-local observation sequences and reconciles current worker
and receipt observations by observed time. It does not rewrite historical
receipts; existing receipt rows begin at sequence one. At an equal observed time,
contradictory nonterminal evidence remains unresolved rather than releasing held
exposure or resolving an execution success.

Schema 36 adds an empty human-approved Kubernetes dispatch/outbox ledger with its
cancellation fence trigger and worker lookup index. It preserves all v35 records and
does not infer a dispatch, target, worker claim, provider action, or receipt. Backups
and held restores validate the exact trusted trigger rather than accepting arbitrary
executable schema.

Schema 37 adds an empty context-only OpenCode message-delivery journal. It preserves
legacy conversation messages without inventing a harness session, delivery, upstream
readback, or OpenCode compatibility result; see `docs/opencode-message-delivery.md`.

Schema 38 fences new OpenCode deliveries to their immutable operation, grant, and
lease tuple. Existing v37 queued/delivered rows remain unclaimable rather than having
that authority association inferred during migration.

Schema 39 adds empty immutable operation-to-harness session bindings. A worker records
one provider-disabled context-only destination for its admitted operation; migration
does not infer bindings for legacy deliveries, which remain readable and cancellable
but cannot be newly claimed or authorized.

Schema 40 adds empty provider-disabled Astra conversation and coordinator records.
Migration does not infer conversations, turns, selected context, proposals, approvals,
grants, dispatches, or provider activity from v39 data.

Schema 41 adds human proposal-to-design registrations. Migration adds no
registration, experiment, approval, grant, dispatch, or execution authority to
existing Astra proposals.

Schema 42 adds empty provider-disabled Astra Explain-status discussions, scoped
bindings, one-tool invocation records, and attributed answers. Migration does not
reinterpret existing turns or create a tool call, answer, operation binding,
execution lease, provider/model call, or authority.

Schema 43 adds empty operation-bound remote workflow lifecycle records for new
packages only: heartbeat, opaque checkpoint, and reconnect/outage metadata remain
operational evidence fenced to the original worker/lease/operation. It does not
infer a run for existing operations, make held restores writable, authorize a
replacement submit, or establish provider termination or laptop independence.

Schema 44 adds empty operation-bound recovery reconciliation records. It does
not infer worker identity loss, a provider lookup, cancellation delivery, or a
terminal result for legacy operations; independent reconciler evidence remains
separate from the original worker's identity and cannot create replacement work.

Schema 45 adds empty Astra coordinator consumer leases and availability records.
Schema 46 adds empty human artifact-read custody and immutable reproduction-bundle
records without releasing historical quarantined artifacts. Schema 47 adds
secret-free local-owner operator audit/health metadata and optional event lineage.
Schema 48 adds empty metadata-only MixFM dataset snapshots, source/license/
provenance declarations, and review proposals. It does not download or validate
external data, infer a license/provenance record, or approve prior datasets.
None of these migrations infers credentials, provider activity, artifact release,
recovery authority, or scientific acceptance from older records.

Schema 49 adds empty human-approved capability policies and unique action-intent/
effect-report records. Migration infers no network/install authority and enables
no execution adapter. It invalidates pre-upgrade backup freshness in active state;
held restores remain immutable. See the [capability policy](docs/agent-capability-policy.md)
for declaration limits and offline acceptance checks.
