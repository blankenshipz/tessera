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

## Pull requests

1. Branch from an up-to-date `main`, e.g. `feat/local-control-auth`.
2. Keep one reviewable behavior change per PR. Reference the relevant design
   section and add a decision note if changing a boundary or supported behavior.
3. Add tests for the behavior and its failure cases. Use temporary directories
   and synthetic data, not real tokens, research inputs, or external paid calls.
4. Run the checks above. Update the README if runnable behavior or setup changes.
5. Commit explicit paths, push the feature branch, and open a PR with `gh pr create`.
   State validation performed, limitations, and any migration or security impact.
6. Wait for CI and maintainer review. Do not self-merge or bypass required checks.

The initial commit seeds the design on `main`; subsequent work uses PRs. Repository
rules/branch protections depend on the hosting plan and are not assumed configured.

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
