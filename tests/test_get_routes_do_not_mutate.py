"""Reading is side-effect free: no GET route changes state (README: returning to history reruns
no work).

Every GET route of the control API is called on a seeded state, as the owner, as an enrolled
agent and as the fixture's registered worker, with ids taken from the seeded rows (a plausible
missing id where the shared fixture seeds nothing).
A single connection watches ``PRAGMA data_version``: it moves only when another connection
commits, so an unchanged value proves the request wrote nothing at all. When it moves, the
per-table content digests say which tables changed, and any table outside
``ALLOWED_READ_WRITES`` fails the test. A GET route that appears later and that this test can
neither exercise nor explicitly skip fails it too.

Coverage limit, stated plainly: the shared campaign fixture seeds a project, snapshot, release,
task, campaign and approval, so about half of the routes see a real row; the rest (experiments,
studies, grants, receipts and so on) are called with a missing id and prove only that the not-found
path writes nothing. Seed more rows here to widen the proof.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control.api import create_app

DEST = {"X-Agent-Destination": "planner.local"}
ALL_SCOPES = ["campaign:read", "snapshot:read", "artifact:read", "campaign:logs", "budget:read"]

# Tables a read may legitimately write, with the reason. Anything else changing on a GET is a bug.
ALLOWED_READ_WRITES: dict[str, str] = {}

# GET routes this test cannot exercise, each with the reason (streaming or protocol routes).
SKIPPED: dict[str, str] = {}

# GET handlers that really write today: path -> evidence. They xfail so the list cannot grow
# silently; an entry that stops writing fails the run (strict) and must be removed.
KNOWN_WRITERS: dict[str, str] = {}

PARAMETER = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")

# path parameter name -> SQL returning one seeded id (first column). Where the shared fixture
# seeds no such row the route is called with a plausible missing id instead.
SEEDS = {
    "approval_id": "SELECT id FROM campaign_approvals ORDER BY id LIMIT 1",
    "artifact_id": "SELECT id FROM artifacts ORDER BY id LIMIT 1",
    "attempt_id": "SELECT id FROM research_experiment_admitted_attempts ORDER BY id LIMIT 1",
    "bundle_id": "SELECT id FROM reproduction_bundles ORDER BY id LIMIT 1",
    "call_id": "SELECT id FROM model_spend_calls ORDER BY id LIMIT 1",
    "campaign_id": "SELECT id FROM campaigns ORDER BY id LIMIT 1",
    "checkpoint_id": "SELECT id FROM execution_result_checkpoints ORDER BY id LIMIT 1",
    "context_id": "SELECT id FROM external_agent_contexts ORDER BY id LIMIT 1",
    "delegation_id": "SELECT id FROM approval_delegations ORDER BY id LIMIT 1",
    "experiment_id": "SELECT id FROM research_experiments ORDER BY id LIMIT 1",
    "finding_id": "SELECT id FROM research_findings ORDER BY id LIMIT 1",
    "grant_id": "SELECT id FROM execution_grants ORDER BY id LIMIT 1",
    "group_id": "SELECT id FROM gate_replicate_groups ORDER BY id LIMIT 1",
    "job_id": "SELECT id FROM review_jobs ORDER BY id LIMIT 1",
    "lock_id": "SELECT id FROM gate_split_locks ORDER BY id LIMIT 1",
    "manifest_id": "SELECT id FROM artifact_manifests ORDER BY id LIMIT 1",
    "node_id": "SELECT id FROM artifact_lineage_nodes ORDER BY id LIMIT 1",
    "operation_id": "SELECT operation_id FROM external_operations ORDER BY operation_id LIMIT 1",
    "output_id": "SELECT id FROM generated_public_outputs ORDER BY id LIMIT 1",
    "plan_digest": "SELECT current_plan_digest FROM tasks ORDER BY id LIMIT 1",
    "policy_id": "SELECT id FROM runtime_policy_profiles ORDER BY id LIMIT 1",
    "program_id": "SELECT id FROM gate_research_programs ORDER BY id LIMIT 1",
    "project_id": "SELECT id FROM projects ORDER BY id LIMIT 1",
    "proposal_id": "SELECT id FROM external_agent_proposals ORDER BY id LIMIT 1",
    "seq": "SELECT 1",
    "skill_id": "SELECT id FROM skill_releases ORDER BY id LIMIT 1",
    "snapshot_id": "SELECT id FROM source_snapshots ORDER BY id LIMIT 1",
    "standing_id": "SELECT id FROM campaign_standing_approvals ORDER BY id LIMIT 1",
    "study_id": "SELECT id FROM research_studies ORDER BY id LIMIT 1",
    "task_id": "SELECT id FROM tasks ORDER BY id LIMIT 1",
    "thread_id": "SELECT id FROM interaction_threads ORDER BY id LIMIT 1",
    "turn_id": "SELECT id FROM astra_coordinator_turns ORDER BY id LIMIT 1",
}


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]


def _digests(connection: sqlite3.Connection) -> dict[str, str]:
    result = {}
    for table in _tables(connection):
        digest = hashlib.sha256()
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            digest.update(repr(tuple(row)).encode())
        result[table] = digest.hexdigest()
    return result


def _seed(connection: sqlite3.Connection, name: str) -> str:
    query = SEEDS.get(name)
    if query is not None:
        try:
            row = connection.execute(query).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            return str(row[0])
    return f"missing_{name}"


def _walk(routes, prefix=""):
    """Every concrete route, descending into included routers and mounts (this FastAPI keeps
    included routers as lazy wrappers, so ``app.routes`` alone shows only a fraction of them)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from _walk(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
        elif hasattr(route, "routes") and hasattr(route, "path"):
            yield from _walk(route.routes, prefix + route.path)
        else:
            yield prefix + getattr(route, "path", ""), route


def _get_routes(app) -> dict[str, object]:
    found: dict[str, object] = {}
    for path, route in _walk(app.routes):
        if "GET" in (getattr(route, "methods", None) or set()):
            found.setdefault(path, route)
    return found


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    env.approve(campaign)
    env.agent(ALL_SCOPES, "full")
    app = create_app(env.state)
    return env, campaign, TestClient(app, raise_server_exceptions=False), app


def _credentials(env):
    owner = (env.state / "owner.header").read_text().strip().split(": ", 1)[1]
    return {
        "owner": {"Authorization": owner},
        "agent": {"Authorization": "Bearer synthetic-full-token-" + "x" * 30, **DEST},
        # The shared fixture's registered worker (a worker-audience credential): the only way to
        # reach the /worker routes, where a "claiming" read would be most tempting to write.
        "worker": {"Authorization": "Bearer worker-token"},
    }


def _exercise(world):
    """Call every GET route as each credential.

    Returns (routes, exercised paths, writes by (path, credential) -> tables, status codes)."""
    env, _campaign, client, app = world
    routes = _get_routes(app)
    watcher = sqlite3.connect(env.state / "tessera.sqlite3")
    seeding = sqlite3.connect(env.state / "tessera.sqlite3")
    current = _digests(watcher)
    exercised: set[str] = set()
    writes: dict[tuple[str, str], set[str]] = {}
    statuses: dict[tuple[str, str], int] = {}
    for path in sorted(routes):
        if path in SKIPPED:
            continue
        url = PARAMETER.sub(lambda m: _seed(seeding, m.group(1)), path)
        for who, headers in _credentials(env).items():
            before = watcher.execute("PRAGMA data_version").fetchone()[0]
            response = client.get(url, headers=headers)
            after = watcher.execute("PRAGMA data_version").fetchone()[0]
            exercised.add(path)
            statuses[(path, who)] = response.status_code
            if after != before:
                latest = _digests(watcher)
                changed = {t for t in latest if latest[t] != current.get(t)}
                changed |= set(current) - set(latest)
                current = latest
                writes.setdefault((path, who), set()).update(changed or {"(unknown)"})
    watcher.close()
    seeding.close()
    return routes, exercised, writes, statuses


def _offenders(writes) -> dict[str, list[str]]:
    return {
        f"{who} GET {path}": sorted(tables - set(ALLOWED_READ_WRITES))
        for (path, who), tables in writes.items()
        if tables - set(ALLOWED_READ_WRITES) and path not in KNOWN_WRITERS
    }


def test_every_get_route_is_exercised_or_explicitly_skipped(world):
    routes, exercised, _writes, _statuses = _exercise(world)
    assert set(routes) == exercised | set(SKIPPED), (
        "a GET route is neither exercised nor skipped here: "
        f"{sorted(set(routes) - exercised - set(SKIPPED))}"
    )
    assert set(SKIPPED) <= set(routes), "SKIPPED names a route that no longer exists"
    assert len(routes) >= 100, "route enumeration found far fewer GET routes than the API has"


def test_the_requests_reach_real_handlers_not_only_refusals(world):
    """A test that only collected 401s and 404s would pass for the wrong reason."""
    _routes, _exercised, _writes, statuses = _exercise(world)
    owner_ok = {path for (path, who), code in statuses.items() if who == "owner" and code < 300}
    assert len(owner_ok) >= 25, f"only {len(owner_ok)} GET routes answered 2xx for the owner"
    assert not {k: c for k, c in statuses.items() if c >= 500}, "a GET route failed with a 5xx"


def test_no_get_route_writes_outside_the_allowed_tables(world):
    env, campaign, _client, _app = world
    before = _campaign_row(env, campaign)
    _routes, _exercised, writes, _statuses = _exercise(world)
    assert not _offenders(writes), f"GET routes changed state: {_offenders(writes)}"
    assert _campaign_row(env, campaign) == before, "a GET advanced the campaign's state"


def _campaign_row(env, campaign):
    return env.query(
        "SELECT status, revision, updated_at FROM campaigns WHERE id=?", campaign["id"]
    )[0][:]


def test_the_detector_sees_a_get_route_that_writes(world):
    """Self-check: a deliberately writing GET route is found, so a clean run means something."""
    env, _campaign, _client, app = world

    def write_on_read():
        with sqlite3.connect(env.state / "tessera.sqlite3") as connection:
            connection.execute(
                "INSERT INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
                ("org_written_by_a_get", "x", "2026-01-01T00:00:00Z"),
            )
        return {"ok": True}

    app.add_api_route("/_probe/write-on-read", write_on_read, methods=["GET"])
    routes, exercised, writes, _statuses = _exercise(world)
    assert "/_probe/write-on-read" in routes and "/_probe/write-on-read" in exercised
    assert _offenders(writes).get("owner GET /_probe/write-on-read") == ["organizations"]
