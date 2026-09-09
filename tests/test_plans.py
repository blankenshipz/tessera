from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.cli import main
from tessera_control.migrations import MIGRATIONS
from tessera_control.store import (
    LOCAL_ORG_ID,
    Forbidden,
    Store,
    _token_hash,
    canonical_digest,
    initialize_state,
    migrate_state,
)


def _owner_headers(state_dir: Path) -> dict[str, str]:
    _, value = (state_dir / "owner.header").read_text().strip().split(": ", 1)
    return {"Authorization": value}


def _headers(state_dir: Path, key: str) -> dict[str, str]:
    return _owner_headers(state_dir) | {"Idempotency-Key": key}


def _add_principal(
    state_dir: Path,
    principal_id: str,
    org_id: str,
    role: str,
    token: str,
    project_id: str | None = None,
) -> dict[str, str]:
    connection = sqlite3.connect(state_dir / "tessera.sqlite3")
    try:
        credential_created_at = datetime.now(UTC)
        if org_id != LOCAL_ORG_ID:
            connection.execute(
                "INSERT INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
                (org_id, "Other", "2026-01-01T00:00:00Z"),
            )
        connection.execute(
            """INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)
            VALUES (?, ?, ?, ?, NULL, '2026-01-01T00:00:00Z')""",
            (principal_id, org_id, role, _token_hash(token)),
        )
        if role == "human":
            credential = ("local_owner", "control", None, None, None)
        else:
            if project_id is None:
                raise ValueError("Synthetic agents require a project scope.")
            parent = connection.execute(
                """SELECT c.id FROM credentials c JOIN principals p ON p.id = c.principal_id
                WHERE p.org_id = ? AND c.kind = 'local_owner'""",
                (org_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("Synthetic agents require a local owner parent.")
            credential = (
                "agent",
                "control",
                project_id,
                parent[0],
                (credential_created_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            )
        connection.execute(
            """INSERT INTO credentials
            (id, principal_id, kind, audience, project_id, parent_id, expires_at,
             revoked_at, created_at, created_by_principal_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                f"credential_{principal_id}",
                principal_id,
                *credential,
                credential_created_at.isoformat().replace("+00:00", "Z"),
                principal_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    initialize_state(state)
    return state


@pytest.fixture
def client(state_dir: Path) -> TestClient:
    return TestClient(create_app(state_dir))


def _project(client: TestClient, state_dir: Path) -> dict[str, object]:
    response = client.post(
        "/projects", headers=_headers(state_dir, "project"), json={"name": "Plan"}
    )
    assert response.status_code == 201
    return response.json()


def _task(client: TestClient, state_dir: Path, project_id: str) -> dict[str, object]:
    response = client.post(
        f"/projects/{project_id}/tasks",
        headers=_headers(state_dir, "task"),
        json={
            "title": "Record",
            "purpose": "Record only",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    assert response.status_code == 201
    return response.json()


def _spec() -> dict[str, object]:
    return {
        "argv": ["python", "analysis.py"],
        "input_snapshot_digest": "a" * 64,
        "pinned_skill_digests": ["b" * 64],
        "output_paths": ["results/summary.json"],
        "profile": "local-cpu",
        "max_output_bytes": 4096,
    }


def _plan_body(expected_revision: int, *, title: str = "Executable plan") -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "title": title,
        "purpose": "Run a bounded local analysis.",
        "budget_cents": 123,
        "max_duration_seconds": 120,
        "execution_spec": _spec(),
    }


def test_immutable_plan_revisions_replay_and_approval_invalidation(
    client: TestClient, state_dir: Path
) -> None:
    project = _project(client, state_dir)
    record = _task(client, state_dir, str(project["id"]))
    body = _plan_body(1)
    first = client.put(
        f"/tasks/{record['id']}/plan", headers=_headers(state_dir, "plan"), json=body
    )
    replay = client.put(
        f"/tasks/{record['id']}/plan", headers=_headers(state_dir, "plan"), json=body
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    revised = first.json()
    assert revised["status"] == "draft"
    assert revised["revision"] == 2
    assert revised["execution_granted"] is False
    assert (
        client.put(
            f"/tasks/{record['id']}/plan",
            headers=_headers(state_dir, "plan"),
            json=_plan_body(1, title="Different"),
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/tasks/{record['id']}/plan", headers=_headers(state_dir, "stale"), json=_plan_body(1)
        ).status_code
        == 409
    )

    revisions = client.get(
        f"/tasks/{record['id']}/revisions?limit=1", headers=_owner_headers(state_dir)
    )
    assert revisions.status_code == 200
    assert revisions.json()["revisions"][0]["execution_spec"] is None
    next_page = client.get(
        f"/tasks/{record['id']}/revisions?offset=1&limit=1", headers=_owner_headers(state_dir)
    ).json()["revisions"]
    assert next_page[0]["plan_digest"] == revised["current_plan_digest"]
    assert next_page[0]["execution_spec"]["network_enabled"] is False

    approval = client.post(
        f"/tasks/{record['id']}/approve",
        headers=_headers(state_dir, "approve"),
        json={"expected_revision": 2, "expected_plan_digest": revised["current_plan_digest"]},
    )
    assert approval.status_code == 200
    assert approval.json()["revision"] == 3
    assert approval.json()["status"] == "approved"
    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        row = connection.execute(
            "SELECT plan_revision, plan_digest, approving_principal_id FROM task_approvals"
        ).fetchone()
    assert row[0:2] == (2, revised["current_plan_digest"])
    assert row[2].startswith("principal_")
    # Approval actor is the live owner principal and approval remains historical after a new plan.
    changed = client.put(
        f"/tasks/{record['id']}/plan",
        headers=_headers(state_dir, "changed"),
        json=_plan_body(3, title="Changed"),
    )
    assert changed.status_code == 200
    assert changed.json()["status"] == "draft"
    assert (
        client.post(
            f"/tasks/{record['id']}/approve",
            headers=_headers(state_dir, "bad-basis"),
            json={"expected_revision": 4, "expected_plan_digest": revised["current_plan_digest"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/tasks/{record['id']}/approve",
            headers=_headers(state_dir, "approve-changed"),
            json={
                "expected_revision": 4,
                "expected_plan_digest": changed.json()["current_plan_digest"],
            },
        ).json()["revision"]
        == 5
    )


def test_plan_revisions_enforce_tenant_human_and_live_actor(
    state_dir: Path, client: TestClient
) -> None:
    project = _project(client, state_dir)
    record = _task(client, state_dir, str(project["id"]))
    agent = _add_principal(
        state_dir,
        "agent",
        LOCAL_ORG_ID,
        "agent",
        "agent-token",
        str(project["id"]),
    )
    outsider = _add_principal(state_dir, "other", "other-org", "human", "other-token")
    assert (
        client.put(
            f"/tasks/{record['id']}/plan",
            headers=agent | {"Idempotency-Key": "agent"},
            json=_plan_body(1),
        ).status_code
        == 403
    )
    assert client.get(f"/tasks/{record['id']}/revisions", headers=outsider).status_code == 404
    assert (
        client.put(
            f"/tasks/{record['id']}/plan",
            headers=outsider | {"Idempotency-Key": "other"},
            json=_plan_body(1),
        ).status_code
        == 404
    )

    stale_agent = {"id": "agent", "org_id": LOCAL_ORG_ID, "role": "human"}
    with pytest.raises(Forbidden, match="Human plan revision"):
        Store(state_dir).revise_task_plan(
            stale_agent,
            str(record["id"]),
            "stale-role",
            canonical_digest(_plan_body(1)),
            _plan_body(1),
        )
    assert stale_agent == {"id": "agent", "org_id": LOCAL_ORG_ID, "role": "human"}
    with pytest.raises(Forbidden, match="Project-scoped"):
        Store(state_dir).create_project(
            stale_agent,
            "replay-after-revoke",
            canonical_digest({"name": "Stale actor"}),
            {"name": "Stale actor"},
        )

    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE principals SET revoked_at = '2026-01-01T00:00:00Z' WHERE id = 'agent'"
        )
        connection.commit()
    with pytest.raises(Forbidden):
        Store(state_dir).create_project(
            stale_agent,
            "replay-after-revoke",
            canonical_digest({"name": "Stale actor"}),
            {"name": "Stale actor"},
        )


@pytest.mark.parametrize(
    "argv, output_paths, network_enabled",
    [
        ([" "], ["results/out"], False),
        (["python", "analysis.py"], ["."], False),
        (["python", "analysis.py"], ["a//b"], False),
        (["python", "analysis.py"], ["a\\b"], False),
        (["python", "analysis.py"], ["C:/output"], False),
        (["python", "analysis.py"], ["a/../b"], False),
        (["python\x00", "analysis.py"], ["results/out"], False),
        (["python", "analysis.py"], ["results/out"], True),
    ],
)
def test_execution_spec_rejects_nonportable_paths_and_unsupported_network(
    client: TestClient,
    state_dir: Path,
    argv: list[str],
    output_paths: list[str],
    network_enabled: bool,
) -> None:
    project = _project(client, state_dir)
    record = _task(client, state_dir, str(project["id"]))
    body = _plan_body(1)
    body["execution_spec"] = _spec() | {
        "argv": argv,
        "output_paths": output_paths,
        "network_enabled": network_enabled,
    }
    assert (
        client.put(
            f"/tasks/{record['id']}/plan", headers=_headers(state_dir, "invalid"), json=body
        ).status_code
        == 422
    )


def test_execution_spec_preserves_raw_noncommand_arguments(
    client: TestClient, state_dir: Path
) -> None:
    project = _project(client, state_dir)
    record = _task(client, state_dir, str(project["id"]))
    body = _plan_body(1)
    body["execution_spec"] = _spec() | {"argv": [" python ", "", " leading space"]}
    response = client.put(
        f"/tasks/{record['id']}/plan", headers=_headers(state_dir, "preserve-argv"), json=body
    )
    assert response.status_code == 200
    revision = client.get(
        f"/tasks/{record['id']}/revisions?offset=1", headers=_owner_headers(state_dir)
    ).json()["revisions"][0]
    assert revision["execution_spec"]["argv"] == [" python ", "", " leading space"]


def _legacy_state(tmp_path: Path) -> Path:
    state = tmp_path / "legacy"
    state.mkdir(mode=0o700)
    database = state / "tessera.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL
            );
            INSERT INTO schema_meta VALUES (1, 1);
            CREATE TABLE organizations (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE principals (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
                role TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, revoked_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
                name TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL, purpose TEXT NOT NULL, budget_cents INTEGER NOT NULL,
                max_duration_seconds INTEGER NOT NULL, status TEXT NOT NULL,
                revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
                project_id TEXT NOT NULL, task_id TEXT, event_type TEXT NOT NULL,
                resource_revision INTEGER NOT NULL, actor_principal_id TEXT NOT NULL,
                created_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE idempotency (
                principal_id TEXT NOT NULL, org_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                method TEXT NOT NULL, path TEXT NOT NULL, request_digest TEXT NOT NULL,
                response_status INTEGER NOT NULL, response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (principal_id, org_id, idempotency_key, method, path)
            );
            """
        )
        connection.execute(
            "INSERT INTO organizations VALUES (?, ?, ?)",
            (LOCAL_ORG_ID, "Legacy", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO principals VALUES (?, ?, ?, ?, NULL, ?)",
            ("owner", LOCAL_ORG_ID, "human", _token_hash("legacy-token"), "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?)",
            ("project", LOCAL_ORG_ID, "P", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task",
                "project",
                "Legacy",
                "Old plan",
                5,
                60,
                "approved",
                2,
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
            ),
        )
        for event_type, revision, stamp in (
            ("task.created", 1, "2026-01-01T00:00:00Z"),
            ("task.approved", 2, "2026-01-02T00:00:00Z"),
        ):
            connection.execute(
                """INSERT INTO events
                (org_id, project_id, task_id, event_type, resource_revision,
                 actor_principal_id, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (LOCAL_ORG_ID, "project", "task", event_type, revision, "owner", stamp, "{}"),
            )
        old_response = {
            "id": "task",
            "status": "approved",
            "revision": 2,
            "execution_granted": False,
        }
        connection.execute(
            "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "owner",
                LOCAL_ORG_ID,
                "old-approval",
                "POST",
                "/tasks/task/approve",
                canonical_digest({"expected_revision": 1}),
                200,
                json.dumps(old_response),
                "2026-01-02T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    database.chmod(0o600)
    (state / "owner.header").write_text("Authorization: Bearer legacy-token\n")
    (state / "owner.header").chmod(0o600)
    return state


def _v2_state(tmp_path: Path, *, with_agent: bool = False, with_second_human: bool = False) -> Path:
    state = _legacy_state(tmp_path)
    connection = sqlite3.connect(state / "tessera.sqlite3")
    try:
        connection.execute("BEGIN")
        MIGRATIONS[1](connection)
        if with_agent:
            connection.execute(
                """INSERT INTO principals VALUES (?, ?, 'agent', ?, NULL, ?)""",
                (
                    "legacy_agent",
                    LOCAL_ORG_ID,
                    _token_hash("legacy-agent-token"),
                    "2026-01-01T00:00:00Z",
                ),
            )
        if with_second_human:
            connection.execute(
                """INSERT INTO principals VALUES (?, ?, 'human', ?, NULL, ?)""",
                (
                    "second_human",
                    LOCAL_ORG_ID,
                    _token_hash("second-human-token"),
                    "2026-01-03T00:00:00Z",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return state


def test_explicit_v1_migration_preserves_records_approval_event_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _legacy_state(tmp_path)
    with pytest.raises(ValueError, match="explicit"):
        create_app(state)
    monkeypatch.setattr("sys.argv", ["tessera", "migrate", "--state-dir", str(state)])
    assert main() == 0
    assert capsys.readouterr().out.strip() == "23"
    client = TestClient(create_app(state))
    assert client.get("/tasks/task", headers=_owner_headers(state)).json()["status"] == "approved"
    revisions = client.get("/tasks/task/revisions", headers=_owner_headers(state)).json()[
        "revisions"
    ]
    assert revisions[0]["revision"] == 1
    assert revisions[0]["execution_spec"] is None
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM task_approvals").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 1
        credential = connection.execute(
            """SELECT kind, audience, project_id, expires_at FROM credentials
            WHERE principal_id = 'owner'"""
        ).fetchone()
    assert credential == ("local_owner", "control", None, None)
    replay = client.post(
        "/tasks/task/approve",
        headers=_headers(state, "old-approval"),
        json={"expected_revision": 1},
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "approved"
    assert migrate_state(state) == 23


def test_fresh_initialization_uses_latest_schema(state_dir: Path) -> None:
    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 23


def test_v2_migration_retains_only_bootstrap_owner_and_disables_legacy_agents(
    tmp_path: Path,
) -> None:
    state = _v2_state(tmp_path, with_agent=True)
    assert migrate_state(state) == 23
    client = TestClient(create_app(state))
    assert client.get("/tasks/task", headers=_owner_headers(state)).status_code == 200
    agent = {"Authorization": "Bearer legacy-agent-token", "Idempotency-Key": "agent-create"}
    assert client.get("/projects", headers=agent).status_code == 401
    assert client.post("/projects", headers=agent, json={"name": "Denied"}).status_code == 401
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        owner = connection.execute(
            "SELECT kind, audience FROM credentials WHERE principal_id = 'owner'"
        ).fetchone()
        agent_credential = connection.execute(
            "SELECT id FROM credentials WHERE principal_id = 'legacy_agent'"
        ).fetchone()
        agent_revoked = connection.execute(
            "SELECT revoked_at FROM principals WHERE id = 'legacy_agent'"
        ).fetchone()
    assert owner == ("local_owner", "control")
    assert agent_credential is None
    assert agent_revoked[0] is not None


def test_v2_migration_rejects_ambiguous_local_humans(tmp_path: Path) -> None:
    state = _v2_state(tmp_path, with_second_human=True)
    with pytest.raises(ValueError, match="unambiguous"):
        migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'credentials'"
            ).fetchone()[0]
            == 0
        )


def test_migration_rollback_and_future_version_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _legacy_state(tmp_path)

    def fail_mid_migration(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE tasks ADD COLUMN partial TEXT")
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(MIGRATIONS, 1, fail_mid_migration)
    with pytest.raises(RuntimeError, match="injected"):
        migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 1
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    assert "partial" not in columns

    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE schema_meta SET schema_version = 99")
        connection.commit()
    with pytest.raises(ValueError, match="newer"):
        migrate_state(state)

    unsafe_state = tmp_path / "unsafe"
    unsafe_state.mkdir()
    (unsafe_state / "tessera.sqlite3").symlink_to(state / "tessera.sqlite3")
    with pytest.raises(ValueError, match="regular"):
        migrate_state(unsafe_state)
