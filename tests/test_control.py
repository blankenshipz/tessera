from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.cli import main
from tessera_control.store import LOCAL_ORG_ID, Store, _token_hash, initialize_state


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    initialize_state(state)
    return state


@pytest.fixture
def client(state_dir: Path) -> TestClient:
    return TestClient(create_app(state_dir))


def owner_headers(state_dir: Path) -> dict[str, str]:
    name, value = (state_dir / "owner.header").read_text().strip().split(": ", 1)
    assert name == "Authorization"
    return {name: value}


def mutation_headers(state_dir: Path, key: str) -> dict[str, str]:
    return owner_headers(state_dir) | {"Idempotency-Key": key}


def project(client: TestClient, state_dir: Path, key: str = "project-key") -> dict[str, object]:
    result = client.post(
        "/projects", headers=mutation_headers(state_dir, key), json={"name": "Research"}
    )
    assert result.status_code == 201
    return result.json()


def task(
    client: TestClient, state_dir: Path, project_id: str, key: str = "task-key"
) -> dict[str, object]:
    result = client.post(
        f"/projects/{project_id}/tasks",
        headers=mutation_headers(state_dir, key),
        json={
            "title": "Evaluate baseline",
            "purpose": "Measure the baseline before designing the next study.",
            "budget_cents": 0,
            "max_duration_seconds": 120,
        },
    )
    assert result.status_code == 201
    return result.json()


def add_principal(
    state_dir: Path,
    *,
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
                "INSERT OR IGNORE INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
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


def test_health_docs_origins_hosts_and_auth(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/projects", headers={"Origin": "http://localhost"}).status_code == 403
    assert client.get("/projects", headers={"Host": "example.test"}).status_code == 400


def test_revoked_and_cross_tenant_principals_cannot_access_resources(
    client: TestClient, state_dir: Path
) -> None:
    record = project(client, state_dir)
    outsider = add_principal(
        state_dir,
        principal_id="human_other",
        org_id="org_other",
        role="human",
        token="other-token",
    )
    assert client.get("/projects", headers=outsider).json() == {"projects": []}
    assert client.get(f"/projects/{record['id']}", headers=outsider).status_code == 404
    connection = sqlite3.connect(state_dir / "tessera.sqlite3")
    try:
        connection.execute("UPDATE principals SET revoked_at = '2026-01-01T00:00:00Z'")
        connection.commit()
    finally:
        connection.close()
    assert client.get("/projects", headers=owner_headers(state_dir)).status_code == 401


def test_project_task_and_labbook_contract(client: TestClient, state_dir: Path) -> None:
    record = project(client, state_dir)
    draft = task(client, state_dir, str(record["id"]))
    assert client.get("/projects", headers=owner_headers(state_dir)).json()["projects"] == [record]
    assert (
        client.get(f"/projects/{record['id']}", headers=owner_headers(state_dir)).json() == record
    )
    assert client.get(f"/projects/{record['id']}/tasks", headers=owner_headers(state_dir)).json()[
        "tasks"
    ] == [draft]
    assert client.get(f"/tasks/{draft['id']}", headers=owner_headers(state_dir)).json() == draft
    events = client.get(
        f"/projects/{record['id']}/labbook", headers=owner_headers(state_dir)
    ).json()["events"]
    assert [event["event_type"] for event in events] == ["project.created", "task.created"]
    assert all(event["created_at"].endswith("Z") for event in events)
    assert (
        client.get(
            f"/projects/{record['id']}/labbook?after={2**63}", headers=owner_headers(state_dir)
        ).status_code
        == 422
    )


def test_event_failure_rolls_back_state_and_replay(
    client: TestClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_event(*args: object) -> None:
        raise sqlite3.OperationalError("synthetic event write failure")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail_event))
        with pytest.raises(sqlite3.OperationalError, match="synthetic event"):
            project(client, state_dir)
    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        for table in ("projects", "events", "idempotency"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert project(client, state_dir)["name"] == "Research"


def test_cross_tenant_task_reads_and_mutations_are_denied(
    client: TestClient, state_dir: Path
) -> None:
    record = project(client, state_dir)
    draft = task(client, state_dir, str(record["id"]))
    outsider = add_principal(
        state_dir, principal_id="outsider", org_id="other-org", role="human", token="outsider-token"
    )
    for path in (
        f"/projects/{record['id']}/tasks",
        f"/projects/{record['id']}/labbook",
        f"/tasks/{draft['id']}",
    ):
        assert client.get(path, headers=outsider).status_code == 404
    assert (
        client.post(
            f"/tasks/{draft['id']}/approve",
            headers=outsider | {"Idempotency-Key": "approve"},
            json={"expected_revision": 1},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/projects/{record['id']}/tasks",
            headers=outsider | {"Idempotency-Key": "create"},
            json={"title": "Test", "purpose": "Test", "budget_cents": 0, "max_duration_seconds": 1},
        ).status_code
        == 404
    )
    assert (
        client.get(f"/tasks/{draft['id']}", headers=owner_headers(state_dir)).json()["status"]
        == "draft"
    )


def test_human_approval_is_revisioned_plan_only_and_agent_is_forbidden(
    client: TestClient, state_dir: Path
) -> None:
    record = project(client, state_dir)
    draft = task(client, state_dir, str(record["id"]))
    agent = add_principal(
        state_dir,
        principal_id="agent_1",
        org_id=LOCAL_ORG_ID,
        role="agent",
        token="agent-token",
        project_id=str(record["id"]),
    )
    denied = client.post(
        f"/tasks/{draft['id']}/approve",
        headers=agent | {"Idempotency-Key": "agent-approve"},
        json={"expected_revision": 1},
    )
    assert denied.status_code == 403
    approved = client.post(
        f"/tasks/{draft['id']}/approve",
        headers=mutation_headers(state_dir, "approve"),
        json={"expected_revision": 1},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["revision"] == 2
    assert approved.json()["execution_granted"] is False
    assert (
        client.post(
            f"/tasks/{draft['id']}/approve",
            headers=mutation_headers(state_dir, "stale"),
            json={"expected_revision": 1},
        ).status_code
        == 409
    )
    events = client.get(
        f"/projects/{record['id']}/labbook", headers=owner_headers(state_dir)
    ).json()["events"]
    assert [event["event_type"] for event in events].count("task.approved") == 1


def test_idempotency_replay_conflict_restart_and_concurrency(
    client: TestClient, state_dir: Path
) -> None:
    headers = mutation_headers(state_dir, "same-key")
    first = client.post("/projects", headers=headers, json={"name": "One"})
    replay = client.post("/projects", headers=headers, json={"name": "One"})
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert client.post("/projects", headers=headers, json={"name": "Different"}).status_code == 409
    restarted = TestClient(create_app(state_dir))
    assert restarted.post("/projects", headers=headers, json={"name": "One"}).json() == first.json()

    concurrent_headers = mutation_headers(state_dir, "concurrent")

    def create_once(_: int) -> tuple[int, dict[str, object]]:
        with TestClient(create_app(state_dir)) as retry_client:
            response = retry_client.post(
                "/projects", headers=concurrent_headers, json={"name": "Concurrent"}
            )
            return response.status_code, response.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(create_once, range(6)))
    assert {status for status, _ in results} == {201}
    assert len({body["id"] for _, body in results}) == 1
    assert len(client.get("/projects", headers=owner_headers(state_dir)).json()["projects"]) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"name": "   "},
        {"name": "x" * 201},
        {"name": "valid", "extra": True},
    ],
)
def test_project_validation_is_strict(
    client: TestClient, state_dir: Path, body: dict[str, object]
) -> None:
    response = client.post("/projects", headers=mutation_headers(state_dir, "bad"), json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request."}


@pytest.mark.parametrize(
    "body",
    [
        {"title": " ", "purpose": "valid", "budget_cents": 0, "max_duration_seconds": 1},
        {"title": "valid", "purpose": " ", "budget_cents": 0, "max_duration_seconds": 1},
        {"title": "valid", "purpose": "valid", "budget_cents": True, "max_duration_seconds": 1},
        {"title": "valid", "purpose": "valid", "budget_cents": -1, "max_duration_seconds": 1},
        {"title": "valid", "purpose": "valid", "budget_cents": 2**63, "max_duration_seconds": 1},
        {"title": "valid", "purpose": "valid", "budget_cents": 0, "max_duration_seconds": 0},
        {"title": "valid", "purpose": "valid", "budget_cents": 0, "max_duration_seconds": 86401},
        {
            "title": "valid",
            "purpose": "valid",
            "budget_cents": 0,
            "max_duration_seconds": 1,
            "extra": 1,
        },
    ],
)
def test_task_validation_is_strict(
    client: TestClient, state_dir: Path, body: dict[str, object]
) -> None:
    record = project(client, state_dir)
    response = client.post(
        f"/projects/{record['id']}/tasks",
        headers=mutation_headers(state_dir, "bad-task"),
        json=body,
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request."}


def test_cursor_bounds_and_failed_command_does_not_consume_key(
    client: TestClient, state_dir: Path
) -> None:
    failed = client.post(
        "/projects/missing/tasks",
        headers=mutation_headers(state_dir, "reusable"),
        json={"title": "valid", "purpose": "valid", "budget_cents": 0, "max_duration_seconds": 1},
    )
    assert failed.status_code == 404
    record = project(client, state_dir, "reusable")
    draft = task(client, state_dir, str(record["id"]))
    assert (
        client.post(
            f"/tasks/{draft['id']}/approve",
            headers=mutation_headers(state_dir, "approval"),
            json={"expected_revision": 1},
        ).status_code
        == 200
    )
    first = client.get(
        f"/projects/{record['id']}/labbook?limit=1", headers=owner_headers(state_dir)
    ).json()["events"]
    next_page = client.get(
        f"/projects/{record['id']}/labbook?after={first[0]['cursor']}&limit=200",
        headers=owner_headers(state_dir),
    ).json()["events"]
    cursors = [event["cursor"] for event in first + next_page]
    assert cursors == sorted(cursors)
    assert len(cursors) == 3
    assert (
        client.get(
            f"/projects/{record['id']}/labbook?limit=201", headers=owner_headers(state_dir)
        ).status_code
        == 422
    )


def test_project_and_task_lists_are_bounded_and_page_with_offsets(
    client: TestClient, state_dir: Path
) -> None:
    projects = [project(client, state_dir, f"project-{number}") for number in range(3)]
    first_projects = client.get("/projects?limit=2", headers=owner_headers(state_dir)).json()[
        "projects"
    ]
    second_projects = client.get(
        "/projects?offset=2&limit=2", headers=owner_headers(state_dir)
    ).json()["projects"]
    assert [record["id"] for record in first_projects + second_projects] == [
        record["id"] for record in projects
    ]

    tasks = [
        task(client, state_dir, str(projects[0]["id"]), f"task-{number}") for number in range(3)
    ]
    first_tasks = client.get(
        f"/projects/{projects[0]['id']}/tasks?limit=2", headers=owner_headers(state_dir)
    ).json()["tasks"]
    second_tasks = client.get(
        f"/projects/{projects[0]['id']}/tasks?offset=2&limit=2", headers=owner_headers(state_dir)
    ).json()["tasks"]
    assert [record["id"] for record in first_tasks + second_tasks] == [
        record["id"] for record in tasks
    ]
    assert client.get("/projects?limit=201", headers=owner_headers(state_dir)).status_code == 422
    assert client.get("/projects?offset=-1", headers=owner_headers(state_dir)).status_code == 422
    assert (
        client.get(
            f"/projects/{projects[0]['id']}/tasks?limit=201", headers=owner_headers(state_dir)
        ).status_code
        == 422
    )


def test_init_modes_refusal_and_token_is_not_stored_plaintext(tmp_path: Path) -> None:
    state = tmp_path / "state"
    database, header = initialize_state(state)
    token = header.read_text().removeprefix("Authorization: Bearer ").strip()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(header.stat().st_mode) == 0o600
    assert token not in database.read_bytes().decode("latin1")
    with pytest.raises(ValueError):
        initialize_state(state)
    link = tmp_path / "link"
    os.symlink(state, link)
    with pytest.raises(ValueError):
        initialize_state(link)


def test_cli_prints_paths_not_token_and_cannot_select_public_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "cli-state"
    monkeypatch.setattr("sys.argv", ["tessera", "init", "--state-dir", str(state)])
    assert main() == 0
    output = capsys.readouterr().out
    assert str(state / "tessera.sqlite3") in output
    assert str(state / "owner.header") in output
    assert "Bearer " not in output

    called: dict[str, object] = {}
    monkeypatch.setattr(
        "tessera_control.cli.uvicorn.run", lambda app, **kwargs: called.update(kwargs)
    )
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "serve", "--state-dir", str(state), "--port", "8765"],
    )
    assert main() == 0
    assert called == {"host": "127.0.0.1", "port": 8765}
