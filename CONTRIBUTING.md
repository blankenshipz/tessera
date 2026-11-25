# Contributing to Tessera

Tessera is [MIT licensed](LICENSE); outside contributions are welcome. Discuss
substantial design changes first, preserve authority boundaries and include
focused tests. Submit only material you have the right to license under MIT.
Read [AGENTS.md](AGENTS.md) and the affected [design contract](DESIGN.md).

## Setup

Docker Compose is the default development/test environment:

```sh
docker compose up --build -d --wait control
docker compose run --build --rm test
docker compose down
```

The service listens at `http://127.0.0.1:8765`. Its named volume retains state;
`down` preserves it. Rebuild after source changes. The isolated test service
has no network or control-state volume. Compose is not a deployment requirement.

For native development install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and Python >=3.11. SQLite must expose built-in deterministic and innocuous
`json_extract` with `trusted_schema=OFF`; incompatible builds fail before state
writes. The pinned Trixie control image was probed on Linux amd64, not all index
architectures. Native tests also require an existing C compiler, `patch` and
`/bin/ps` (`procps` on Debian); development images supply them, hardened runtimes
do not. These are capability requirements, not a universal OS/version claim.

```sh
uv sync --locked --extra mcp
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
uv run --locked tessera init --state-dir .tessera
uv run --locked tessera serve --state-dir .tessera --port 8765
```

The MCP tests need the `mcp` extra from the first line: without it they fail with the fix
instead of skipping (a plain `pytest` would otherwise look green while covering none of
them). A run that knowingly has no extra sets `TESSERA_ALLOW_MISSING_MCP=1` to skip them.

Initialize once in an absent directory. Keep `owner.header` private and never
give it to an agent. Local human authority is bearer possession, not verified
human sessions or owner rotation. Desktop is installed separately; see the
[quickstart](docs/quickstart.md) and [service installation](docs/installation.md).
`pyproject.toml` declares dependencies and `uv.lock` pins them. Run `uv lock` only
for intentional dependency changes, then rerun affected checks. Runtime state,
credentials and `.venv/` must stay out of Git and image contexts.

## Change workflow

1. Inspect branch and existing edits; preserve unrelated work. Do not silently
   reset/switch the baseline. Keep one reviewable behavior change per task.
2. Read the affected design boundary; add a decision note for changed contracts.
3. Test failure cases, stale revisions, duplicate commands, tenant isolation and
   restart behavior when affected. Default tests use temporary state/synthetic
   effects, not real credentials, research inputs or paid calls.
4. Update current guidance when setup or runnable behavior changes. Run the
   acceptance gate below and report exact results and unrun limits.
5. Leave changes uncommitted unless explicitly authorized. When requested, use
   logical tested commits on `main`, staging only intended paths. Push requires
   separate permission; authorization for one does not authorize the other.
   PRs remain paused unless requested; merge/deploy requires separate instruction.
   Commit messages are public and permanent: no links to private tool or agent
   sessions. Install `dev/hooks/commit-msg` (`ln -s ../../dev/hooks/commit-msg
   .git/hooks/commit-msg`), which rejects them.
6. For an authorized push/PR inspect CI and repair in-scope failures; report
   unrelated failures without bypassing required gates. If a PR is requested,
   use a feature branch and the repository template. Hosting protections are not
   assumed to exist.

## Verification gates

- **Task acceptance:** derive targeted tests/lint from the requested behavior. While
  working, run only the tests of the modules you changed and their direct dependents,
  stopping at the first failure: `tessera dev focused-tests` (or
  `.venv/bin/python dev/focused_tests.py`) selects them from your changes, or from a range
  (`A..B`), plus a fixed smoke set, under caps (it lists the set with reasons by default;
  `--run` executes it under the `--jobs` cap and stops (exit 2) when a changed file has no test
  unless `--allow-unmapped`, `--explain` shows why each test is in;
  [ADR 0117](docs/decisions/0117-focused-test-selection.md)), or run specific files with
  `pytest -x`. It never runs the full suite unless `--full` is passed with `--run`. Run the whole suite once per branch, right before
  merge, in the background.
  Docs-only changes require scoped diff, references/anchors, source consistency
  and affected package checks, not unrelated runtime traces. Run
  `.venv/bin/python dev/check_docs.py` for local Markdown links/assets/anchors.
- **Integration acceptance:** before declaring integration-ready, run Ruff lint,
  Ruff format and full pytest (or isolated Compose tests) on a coordinated tree.
  Targeted success is not full-suite success. CI uses four disjoint file shards;
  this is an equivalent full-suite alternative, not permission to omit failures:

  ```sh
  for shard in 0 1 2 3; do
    .venv/bin/python dev/pytest_shard.py --index "$shard" --count 4 -q || exit
  done
  ```

  `make test-parallel` (or `.venv/bin/python dev/pytest_parallel.py --jobs N`) runs the
  same file shards concurrently with separate basetemps and logs, without pytest-xdist.
  The full suite stays a once-per-branch pre-merge run (`make test-parallel`); while working,
  run only the focused set or the affected files: `make test TESTS="tests/test_a.py"` (`-x`).

  Every `tests/**/test_*.py` file belongs to exactly one shard. Report concurrent
  changes, failures, skipped and unrun checks explicitly.

  Test scratch: a focused or parallel run puts its pytest base directories under
  `.pytest-tmp/` (`focused-<run id>`, `parallel<N>-<run id>`), removes exactly those when every
  process is green, and keeps them, printing their paths, when anything fails. A second run never
  shares or removes another run's directory. `--keep-basetemp` (or `TESSERA_KEEP_BASETEMP=1`)
  keeps them even when green; a cleanup problem prints one line and never changes the exit code.
- **Live fixtures:** Docker/native desktop/Kubernetes/provider checks are separate
  opt-ins outside default pytest/CI. Implement requested live harnesses and run
  them when execution is authorized, with bounded calls/tokens/time/retries and
  existing secure credential references. Do not ask again for authorized effects.
  Missing live prerequisites block the run, not independent implementation.
  Synthetic tests, installed packages and physical-device behavior are distinct.

## Opt-in development fixtures

Do not run a fixture just because it is listed here. Inspect ownership and use
only explicitly authorized targets; never kill another project's service or
change a shared context. All cleanup must stay within exact owned identities.

| Fixture | Guide / invocation | Evidence boundary |
|---|---|---|
| Salad single-container GPU | [Runbook](docs/salad-proxy.md); `.venv/bin/python -m tessera_control.salad_live_check` | Zero reads/network/effects by default; explicit `--run --server-config ABSOLUTE_PATH --receipt-directory ABSOLUTE_PATH` needs existing exact human authority, independent service and approved financial scope; unknown rate/unresolved exposure blocks new work; estimates are not bills/hard caps and local tests are not GPU/live proof |
| Salad CPU-only profile | [Provider page](docs/campaign-providers.md#checking-a-cpu-profile-against-salad-opt-in-bounded); `scripts/salad_cpu_live_check.py` | Dry run by default (no network, no credential); `--run` needs an exact `--confirm spend-up-to-Nc`, a private credential file and a receipt directory; at most 9 cents, always stops and deletes the group; billing is a manual comparison with Salad's usage page |
| Execution monitor | [Monitor](docs/execution-monitor.md); `scripts/check_execution_monitor.py` | Zero effects by default; Docker case needs native host observation and already-local pinned image |
| Closed networkless Docker | [Runbook](docs/local-networkless-docker-fixture.md); `scripts/check_local_docker_fixture.py` | Separate explicit `--run`; no image pull/build/install; unknown enforcement/cleanup fails closed |
| Development CPU/control/worker | `scripts/check_cpu_runtime.py`, `scripts/check_control_runtime.py`, `scripts/check_development_e2e.py` | Networkless non-root containers, temporary state/fake provider, quarantine-only synthetic output; not scientific acceptance |
| Combined synthetic harness | `TESSERA_RUN_SYNTHETIC_COMBINED_E2E=1 .venv/bin/python scripts/check_combined_synthetic_e2e.py` | In-process OpenCode-shaped HTTP/SSE then CPU fake-provider fixture, not a real model/harness workflow |
| Persistent output inspection | `.venv/bin/python scripts/check_persistent_output_storage.py PATH` | Read-only; no mount/write/Docker; not byte/inode-exhaustion or restart proof |
| Desktop/control | `scripts/check_desktop_control.py` on a graphical host | Temporary backend and test-only picker stub; any native save-dialog steps need actual observation |
| Remote reconnect simulation | `.venv/bin/python scripts/check_remote_reconnect_synthetic_e2e.py` | Temporary loopback service/worker, checkpoints/outage/cancel fence; reports `provider-unverified`, not laptop independence |
| Agent integration | [Bounded harness](docs/remote-agent-platform-journey.md) | MCP/A2A/ordinary execution assertions; genuine reasoning/off-host/native acceptance separate |

The CPU/stdlib-guard fixture uses this exact image; acquisition is an independent
authorized step. It is not the Trixie control-plane recovery image:

```sh
docker pull python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
```

Default tests cover transactional state/event/idempotency rollback, bounded
polling, worker lifecycle and context/reconnect fences with fake effects. Harness
SSE is an upstream boundary; the labbook is bounded polling, not SSE. Quiet
status, a heartbeat or exited observation does not prove provider termination.

## Engineering boundaries

The API admits commands; storage owns tenant checks and atomic state/event/
idempotency writes; supervisors own external execution. Research runtime skills
must not edit platform/policy/CI. Node administration, target attachment,
provisioning, credential delivery and research authorization are separate:
no ambient kube context, human-token reuse or automatic teardown. Approving a
plan does not authorize Secret materialization or provisioning.

No new billable infrastructure/provider is implied by a routine feature request.
Explicitly authorized application-provider tests are separate from coding-agent
inference limits; never expose secrets or enable financial fallback. A valid
artifact/message/approval is not proof that an experiment ran.

## Upgrade, backup and security

Stop/fence the old control writer, verify a private backup and retain the old
binary before upgrading. Startup refuses older schemas; run
`uv run --locked tessera migrate --state-dir .tessera` explicitly. Migration is
transactional/repeatable, rejects future versions and never infers credentials,
provider activity, artifact release or scientific acceptance from older records.
For Compose, stop/rebuild `control`, run
`docker compose run --rm control tessera migrate --state-dir /state/control`,
then restart with `docker compose up -d --wait control`. Never use `down -v` as
an upgrade procedure. Detailed migration code/tests are authoritative; see
[compatibility](docs/compatibility-matrix.md).

Take a new post-migration backup and verify held restore. Restore requires the
matching schema, revokes old credentials and creates permanently read-only
history, not active failover. It does not stop the original writer/workers/jobs.
Read [recovery](docs/recovery.md) for object custody, schema allowlisting, bounds
and older-backup handling. Sensitive vulnerability reports go through the enabled
GitHub private reporting channel in [SECURITY.md](SECURITY.md), never public issues.
