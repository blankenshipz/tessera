# Contributing to Tessera

Tessera is a source-only developer alpha. No license has been selected: do not
assume redistribution rights, submit third-party material, or solicit outside
contributions until the maintainer chooses release terms and reopens the policy.
The maintainer has paused PRs; unsolicited contributions may be declined even
when a technical change is useful.

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

For native development, install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and Python 3.11 or newer:

```sh
uv sync --locked
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

`pyproject.toml` declares dependencies and `uv.lock` pins the resolved environment.
Use `uv lock` after intentional dependency changes and rerun all checks. Docker
uses `uv sync --locked`, following the [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/).
The ignored `.venv/` and `.tessera/` directories are for local development;
credentials and runtime data must stay out of Git and image build contexts.

## Initial implementation workflow

For an opt-in live CPU isolation/cancellation fixture, first fetch the exact
development Python image, then run the script against your local Docker daemon:

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

After installing the locked desktop dependencies, run
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
Use logical, tested commits on `main`; PRs can resume when requested.

1. Start from an up-to-date `main` and preserve unrelated local work.
2. Keep one reviewable behavior change per commit. Reference the relevant design
   section and add a decision note if changing a boundary or supported behavior.
3. Add tests for the behavior and its failure cases. Use temporary directories
   and synthetic data, not real tokens, research inputs, or external paid calls.
4. Run the checks above. Update the README if runnable behavior or setup changes.
5. Commit explicit paths and push completed changes to `main`.
   State validation performed, limitations, and any migration or security impact.
6. Check CI results and fix failures. Do not bypass required checks.

If a PR is requested, use a feature branch and the repository PR template; merging
still requires the maintainer's instruction. Repository rules/branch protections
depend on the hosting plan and are not assumed configured.

## Engineering boundaries

Read DESIGN.md for requirements; do not implement the entire roadmap at once.
Use explicit schemas and errors, bounded queries, tenant-scoped lookups, and
transactional state/event writes. A valid file or successful agent message is not
proof of a completed experiment. Test stale revisions, duplicate commands,
unauthorized access, and restart behavior when they touch your change.

Avoid speculative adapters and abstractions. New infrastructure or billable
providers need a separate proposal. Research runtime skills must never edit the
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
