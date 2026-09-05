from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.store import Forbidden, Store, _token_hash, initialize_state


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
    return {name: value}


def headers(token: str, key: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def bearer(headers_: dict[str, str]) -> str:
    return headers_["Authorization"].removeprefix("Bearer ")


def token(label: str) -> str:
    return f"{label}_{'x' * 48}"


def expires_in(hours: int = 1) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def create_project(client: TestClient, state_dir: Path, name: str, key: str) -> dict[str, object]:
    response = client.post(
        "/projects",
        headers=owner_headers(state_dir) | {"Idempotency-Key": key},
        json={"name": name},
    )
    assert response.status_code == 201
    return response.json()


def create_task(
    client: TestClient, project_id: str, credential: dict[str, str], key: str
) -> dict[str, object]:
    response = client.post(
        f"/projects/{project_id}/tasks",
        headers=credential | {"Idempotency-Key": key},
        json={
            "title": "Scoped task",
            "purpose": "Validate scoped credential reads and writes.",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    assert response.status_code == 201
    return response.json()


def issue(
    client: TestClient,
    state_dir: Path,
    *,
    value: str,
    kind: str,
    audience: str,
    project_id: str | None,
    key: str,
    expires_at: str | None = None,
) -> tuple[dict[str, object], dict[str, str]]:
    body: dict[str, object] = {
        "token": value,
        "kind": kind,
        "audience": audience,
        "project_id": project_id,
        "expires_at": expires_at or expires_in(),
    }
    response = client.post(
        "/credentials", headers=owner_headers(state_dir) | {"Idempotency-Key": key}, json=body
    )
    assert response.status_code == 201, response.text
    return response.json(), body


def test_credential_issuance_is_secret_safe_and_idempotent(
    client: TestClient, state_dir: Path
) -> None:
    value = token("session")
    response, body = issue(
        client,
        state_dir,
        value=value,
        kind="human_session",
        audience="control",
        project_id=None,
        key="issue-session",
    )
    assert {"token", "token_hash"}.isdisjoint(response)
    assert response["kind"] == "human_session"
    assert response["audience"] == "control"
    assert response["project_id"] is None
    listed = client.get("/credentials?limit=1", headers=owner_headers(state_dir))
    assert listed.status_code == 200
    assert len(listed.json()["credentials"]) == 1
    replay = client.post(
        "/credentials",
        headers=owner_headers(state_dir) | {"Idempotency-Key": "issue-session"},
        json=body,
    )
    assert replay.status_code == 201
    assert replay.json() == response
    different = body | {"token": token("different")}
    assert (
        client.post(
            "/credentials",
            headers=owner_headers(state_dir) | {"Idempotency-Key": "issue-session"},
            json=different,
        ).status_code
        == 409
    )
    database = state_dir / "tessera.sqlite3"
    with sqlite3.connect(database) as connection:
        replay_json = connection.execute(
            "SELECT response_json FROM idempotency WHERE path = '/credentials'"
        ).fetchone()[0]
        stored_hash = connection.execute(
            "SELECT token_hash FROM principals WHERE id = ?", (response["principal_id"],)
        ).fetchone()[0]
    assert value not in replay_json
    assert value not in database.read_bytes().decode("latin1")
    assert stored_hash != value
    first_revoke = client.post(
        f"/credentials/{response['credential_id']}/revoke",
        headers=owner_headers(state_dir) | {"Idempotency-Key": "revoke-child"},
    )
    repeated_revoke = client.post(
        f"/credentials/{response['credential_id']}/revoke",
        headers=owner_headers(state_dir) | {"Idempotency-Key": "revoke-child-again"},
    )
    assert first_revoke.status_code == repeated_revoke.status_code == 200
    assert first_revoke.json() == repeated_revoke.json()


def test_metadata_less_principal_fails_closed(client: TestClient, state_dir: Path) -> None:
    value = token("metadata-less")
    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)
            VALUES ('principal_without_credential', 'org_local', 'agent', ?, NULL,
            '2026-01-01T00:00:00Z')""",
            (_token_hash(value),),
        )
        connection.commit()
    assert client.get("/projects", headers=headers(value)).status_code == 401
    assert (
        client.post(
            "/projects",
            headers=headers(value, "missing-credential"),
            json={"name": "Denied"},
        ).status_code
        == 401
    )


def test_malformed_credential_metadata_fails_closed(client: TestClient, state_dir: Path) -> None:
    project = create_project(client, state_dir, "Metadata", "metadata-project")
    mutations = (
        ("scope", "UPDATE credentials SET project_id = NULL WHERE id = ?"),
        ("expiry", "UPDATE credentials SET expires_at = NULL WHERE id = ?"),
        ("created", "UPDATE credentials SET created_at = 'invalid' WHERE id = ?"),
        ("cycle", "UPDATE credentials SET parent_id = id WHERE id = ?"),
        ("role", "UPDATE principals SET role = 'human' WHERE id = ?"),
    )
    for label, statement in mutations:
        value = token(f"malformed-{label}")
        metadata, _ = issue(
            client,
            state_dir,
            value=value,
            kind="agent",
            audience="control",
            project_id=str(project["id"]),
            key=f"malformed-{label}",
        )
        target = metadata["principal_id"] if label == "role" else metadata["credential_id"]
        with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
            connection.execute(statement, (target,))
            connection.commit()
        assert client.get("/projects", headers=headers(value)).status_code == 401


def test_credential_contract_and_project_scope_on_all_record_routes(
    client: TestClient, state_dir: Path
) -> None:
    first = create_project(client, state_dir, "First", "first")
    second = create_project(client, state_dir, "Second", "second")
    owner = owner_headers(state_dir)
    first_task = create_task(client, str(first["id"]), owner, "first-task")
    second_task = create_task(client, str(second["id"]), owner, "second-task")
    metadata, _ = issue(
        client,
        state_dir,
        value=token("scoped-human"),
        kind="human_session",
        audience="control",
        project_id=str(first["id"]),
        key="scoped-human",
    )
    scoped = headers(token("scoped-human"))
    me = client.get("/me", headers=scoped)
    assert me.status_code == 200
    assert me.json()["credential_id"] == metadata["credential_id"]
    assert me.json()["project_id"] == first["id"]
    assert "token_hash" not in me.json()
    assert client.get("/projects", headers=scoped).json() == {"projects": [first]}
    assert client.get(f"/projects/{first['id']}", headers=scoped).status_code == 200
    assert client.get(f"/projects/{second['id']}", headers=scoped).status_code == 404
    assert client.get(f"/projects/{first['id']}/tasks", headers=scoped).status_code == 200
    assert client.get(f"/projects/{second['id']}/tasks", headers=scoped).status_code == 404
    assert client.get(f"/projects/{first['id']}/labbook", headers=scoped).status_code == 200
    assert client.get(f"/projects/{second['id']}/labbook", headers=scoped).status_code == 404
    assert client.get(f"/tasks/{first_task['id']}", headers=scoped).status_code == 200
    assert client.get(f"/tasks/{first_task['id']}/revisions", headers=scoped).status_code == 200
    assert client.get(f"/tasks/{second_task['id']}", headers=scoped).status_code == 404
    assert client.get(f"/tasks/{second_task['id']}/revisions", headers=scoped).status_code == 404
    assert (
        client.post(
            "/projects",
            headers=scoped | {"Idempotency-Key": "scope-create"},
            json={"name": "Denied"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/projects/{first['id']}/tasks",
            headers=scoped | {"Idempotency-Key": "scoped-task"},
            json={
                "title": "Permitted",
                "purpose": "A human session can author its scoped project.",
                "budget_cents": 0,
                "max_duration_seconds": 60,
            },
        ).status_code
        == 201
    )
    assert client.get("/credentials", headers=scoped).status_code == 403
    revoked = client.post(
        f"/credentials/{metadata['credential_id']}/revoke",
        headers=scoped | {"Idempotency-Key": "self-revoke"},
    )
    assert revoked.status_code == 200
    assert client.get("/me", headers=scoped).status_code == 401


def test_agent_and_worker_audience_and_authority_boundaries(
    client: TestClient, state_dir: Path
) -> None:
    project = create_project(client, state_dir, "Agent project", "agent-project")
    task = create_task(client, str(project["id"]), owner_headers(state_dir), "agent-task")
    agent_token = token("agent")
    issue(
        client,
        state_dir,
        value=agent_token,
        kind="agent",
        audience="control",
        project_id=str(project["id"]),
        key="agent",
    )
    agent = headers(agent_token)
    assert (
        client.post(
            f"/tasks/{task['id']}/approve",
            headers=agent | {"Idempotency-Key": "agent-approve"},
            json={"expected_revision": 1},
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/tasks/{task['id']}/plan",
            headers=agent | {"Idempotency-Key": "agent-revise"},
            json={
                "expected_revision": 1,
                "title": "Denied",
                "purpose": "Agents cannot revise plans.",
                "budget_cents": 0,
                "max_duration_seconds": 60,
                "execution_spec": {
                    "argv": ["python"],
                    "input_snapshot_digest": "a" * 64,
                    "pinned_skill_digests": [],
                    "output_paths": ["result.json"],
                    "profile": "local-cpu",
                    "max_output_bytes": 1,
                },
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/credentials",
            headers=agent | {"Idempotency-Key": "agent-credential"},
            json={
                "token": token("forbidden"),
                "kind": "agent",
                "audience": "control",
                "project_id": str(project["id"]),
                "expires_at": expires_in(),
            },
        ).status_code
        == 403
    )
    worker_token = token("worker")
    issue(
        client,
        state_dir,
        value=worker_token,
        kind="worker",
        audience="worker",
        project_id=str(project["id"]),
        key="worker",
    )
    worker_headers = headers(worker_token)
    for path in (
        "/me",
        "/projects",
        f"/projects/{project['id']}",
        f"/projects/{project['id']}/tasks",
        f"/projects/{project['id']}/labbook",
        f"/tasks/{task['id']}",
        f"/tasks/{task['id']}/revisions",
    ):
        assert client.get(path, headers=worker_headers).status_code == 401
    assert (
        client.post(
            "/projects",
            headers=worker_headers | {"Idempotency-Key": "worker-project"},
            json={"name": "Denied"},
        ).status_code
        == 401
    )
    worker = Store(state_dir).authenticate_worker(worker_token)
    assert worker["kind"] == "worker"
    assert worker["project_id"] == project["id"]
    with pytest.raises(Forbidden):
        Store(state_dir).authenticate(worker_token)


def test_expiry_revocation_parent_chain_and_replay_are_live(
    client: TestClient, state_dir: Path
) -> None:
    project = create_project(client, state_dir, "Revocation project", "revoke-project")
    child_token = token("child")
    _, body = issue(
        client,
        state_dir,
        value=child_token,
        kind="human_session",
        audience="control",
        project_id=str(project["id"]),
        key="child",
    )
    child = headers(child_token)
    created = client.post(
        f"/projects/{project['id']}/tasks",
        headers=child | {"Idempotency-Key": "child-write"},
        json={
            "title": "Before revocation",
            "purpose": "Create a replay record before revoking its parent.",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    assert created.status_code == 201
    expired_token = token("expired-live")
    expired_metadata, _ = issue(
        client,
        state_dir,
        value=expired_token,
        kind="human_session",
        audience="control",
        project_id=str(project["id"]),
        key="expires-live",
    )
    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE credentials SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00Z", expired_metadata["credential_id"]),
        )
        connection.commit()
    expired_headers = headers(expired_token)
    for path in (
        "/me",
        "/projects",
        f"/projects/{project['id']}",
        f"/projects/{project['id']}/tasks",
        f"/projects/{project['id']}/labbook",
        f"/tasks/{created.json()['id']}",
        f"/tasks/{created.json()['id']}/revisions",
    ):
        assert client.get(path, headers=expired_headers).status_code == 401
    assert (
        client.post(
            "/credentials",
            headers=owner_headers(state_dir) | {"Idempotency-Key": "expired"},
            json=body | {"token": token("expired"), "expires_at": expires_in(25)},
        ).status_code
        == 409
    )
    owner_me = client.get("/me", headers=owner_headers(state_dir)).json()
    root_credential_id = owner_me["credential_id"]
    revoked = client.post(
        f"/credentials/{root_credential_id}/revoke",
        headers=owner_headers(state_dir) | {"Idempotency-Key": "revoke-root"},
    )
    assert revoked.status_code == 200
    assert client.get("/me", headers=child).status_code == 401
    assert (
        client.post(
            f"/projects/{project['id']}/tasks",
            headers=child | {"Idempotency-Key": "child-write"},
            json={
                "title": "Before revocation",
                "purpose": "Create a replay record before revoking its parent.",
                "budget_cents": 0,
                "max_duration_seconds": 60,
            },
        ).status_code
        == 401
    )


def test_credential_request_rejects_invalid_scope_audience_expiry_and_token(
    client: TestClient, state_dir: Path
) -> None:
    base = {
        "token": token("base"),
        "kind": "agent",
        "audience": "control",
        "project_id": None,
        "expires_at": expires_in(),
    }
    assert (
        client.post(
            "/credentials",
            headers=owner_headers(state_dir) | {"Idempotency-Key": "missing-scope"},
            json=base,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/credentials",
            headers=owner_headers(state_dir) | {"Idempotency-Key": "wrong-audience"},
            json=base | {"kind": "worker", "audience": "control", "project_id": "project_x"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/credentials",
            headers=owner_headers(state_dir) | {"Idempotency-Key": "short-token"},
            json=base | {"token": "short", "project_id": "project_x"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/credentials",
            headers=owner_headers(state_dir) | {"Idempotency-Key": "unicode-token"},
            json=base | {"token": "é" * 43, "project_id": "project_x"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/credentials",
            headers=owner_headers(state_dir) | {"Idempotency-Key": "naive-expiry"},
            json=base | {"project_id": "project_x", "expires_at": "2030-01-01T00:00:00"},
        ).status_code
        == 422
    )
