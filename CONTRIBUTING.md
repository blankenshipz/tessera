# Contributing to Tessera

Tessera is in early development. The repository is intended for future open-source
release, but no license has been selected yet. Do not assume redistribution rights
or solicit outside contributions until the maintainer chooses release terms.

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
```

The scripts run synthetic probes in networkless, non-root containers and clean
up only their own labeled containers. They require Docker socket access and run
outside the isolated Compose test service. The second starts a temporary loopback
control service, creates fixture-only authority through storage commands, transfers
released input bytes over HTTP, and records actual Docker observations. They do
not exercise automatic artifact collection or a research investigation. Unknown
cleanup is reported with the fixture container name for reconciliation.

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

## Security reports

Do not put credentials or sensitive research data in issues or PRs. Contact the
repository owner privately on GitHub to arrange a secure report. Automated tests
must not target systems outside the assigned repository. A public security-reporting
channel and license are release prerequisites, not implemented promises.

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
