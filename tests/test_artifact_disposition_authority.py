from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_artifact_dispositions import _manifest, _request
from test_credentials import headers, issue, owner_headers, token

from tessera_control.api import create_app
from tessera_control.store import Store, _token_hash


def _client(state: Path) -> TestClient:
    return TestClient(create_app(state))


def _path(manifest: dict[str, object]) -> str:
    return f"/artifact-manifests/{manifest['id']}/dispositions"


def _other_tenant_headers(state: Path) -> dict[str, str]:
    value = token("other-tenant-human")
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "INSERT INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
            ("org_other", "Other tenant", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            """INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)
            VALUES (?, ?, 'human', ?, NULL, ?)""",
            ("other_human", "org_other", _token_hash(value), "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            """INSERT INTO credentials
            (id, principal_id, kind, audience, project_id, parent_id, expires_at,
             revoked_at, created_at, created_by_principal_id)
            VALUES (?, ?, 'local_owner', 'control', NULL, NULL, NULL, NULL, ?, ?)""",
            (
                "credential_other_human",
                "other_human",
                "2026-01-01T00:00:00Z",
                "other_human",
            ),
        )
        connection.commit()
    return headers(value)


def _protected_snapshot(state: Path) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        "artifacts",
        "artifact_uploads",
        "artifact_manifests",
        "execution_grants",
        "execution_leases",
        "execution_observations",
        "execution_project_limits",
        "execution_task_epochs",
        "source_snapshots",
        "snapshot_files",
        "snapshot_releases",
        "tasks",
        "task_revisions",
        "task_approvals",
        "research_studies",
        "research_variants",
        "research_sources",
        "research_findings",
        "research_finding_sources",
        "research_finding_variants",
        "research_corrections",
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        available = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert set(tables) <= available
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def test_disposition_api_is_human_only_and_scopes_manifest_reads_and_writes(tmp_path: Path) -> None:
    state, worker, _owner, manifest = _manifest(tmp_path)
    client = _client(state)
    path = _path(manifest)
    body = _request(manifest)

    project_id = str(manifest["project_id"])
    agent_value = token("live-control-agent")
    issue(
        client,
        state,
        value=agent_value,
        kind="agent",
        audience="control",
        project_id=project_id,
        key="issue-live-control-agent",
    )
    agent = headers(agent_value)
    assert (
        client.post(path, headers=agent | {"Idempotency-Key": "agent-write"}, json=body).status_code
        == 403
    )
    assert client.get(path, headers=agent).status_code == 200

    other_project = client.post(
        "/projects",
        headers=owner_headers(state) | {"Idempotency-Key": "other-project"},
        json={"name": "Other disposition project"},
    ).json()
    scoped_value = token("other-project-human")
    issue(
        client,
        state,
        value=scoped_value,
        kind="human_session",
        audience="control",
        project_id=str(other_project["id"]),
        key="issue-other-project-human",
    )
    scoped = headers(scoped_value)
    assert client.get(path, headers=scoped).status_code == 404
    assert (
        client.post(
            path, headers=scoped | {"Idempotency-Key": "wrong-project"}, json=body
        ).status_code
        == 404
    )

    outsider = _other_tenant_headers(state)
    assert client.get(path, headers=outsider).status_code == 404
    assert (
        client.post(
            path, headers=outsider | {"Idempotency-Key": "wrong-tenant"}, json=body
        ).status_code
        == 404
    )

    worker_value = token("known-worker-audience")
    worker_credential, _ = issue(
        client,
        state,
        value=worker_value,
        kind="worker",
        audience="worker",
        project_id=project_id,
        key="issue-known-worker-audience",
    )
    live_worker = Store(state).authenticate_worker(worker_value)
    assert live_worker["id"] == worker_credential["principal_id"]
    assert live_worker["kind"] == "worker" and live_worker["project_id"] == project_id
    worker_headers = headers(worker_value)
    assert client.get(path, headers=worker_headers).status_code == 401
    assert (
        client.post(
            path,
            headers=worker_headers | {"Idempotency-Key": "worker-disposition"},
            json=body,
        ).status_code
        == 401
    )
    assert worker["kind"] == "worker"


def test_disposition_api_replay_is_live_after_credential_and_parent_revocation(
    tmp_path: Path,
) -> None:
    state, _worker, _owner, manifest = _manifest(tmp_path)
    client = _client(state)
    path = _path(manifest)
    session_value = token("review-session")
    credential, _ = issue(
        client,
        state,
        value=session_value,
        kind="human_session",
        audience="control",
        project_id=str(manifest["project_id"]),
        key="issue-review-session",
    )
    session = headers(session_value)
    body = _request(manifest)
    created = client.post(path, headers=session | {"Idempotency-Key": "review"}, json=body)
    assert created.status_code == 201
    assert (
        client.post(path, headers=session | {"Idempotency-Key": "review"}, json=body).json()
        == created.json()
    )
    assert (
        client.post(
            f"/credentials/{credential['credential_id']}/revoke",
            headers=owner_headers(state) | {"Idempotency-Key": "revoke-review-session"},
        ).status_code
        == 200
    )
    assert (
        client.post(path, headers=session | {"Idempotency-Key": "review"}, json=body).status_code
        == 401
    )

    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_state, _worker, _owner, parent_manifest = _manifest(parent_root)
    parent_client = _client(parent_state)
    parent_path = _path(parent_manifest)
    child_value = token("parent-revoked-session")
    _, _ = issue(
        parent_client,
        parent_state,
        value=child_value,
        kind="human_session",
        audience="control",
        project_id=str(parent_manifest["project_id"]),
        key="issue-parent-revoked-session",
    )
    child = headers(child_value)
    parent_body = _request(parent_manifest)
    assert (
        parent_client.post(
            parent_path, headers=child | {"Idempotency-Key": "parent-review"}, json=parent_body
        ).status_code
        == 201
    )
    root_credential = parent_client.get("/me", headers=owner_headers(parent_state)).json()[
        "credential_id"
    ]
    assert (
        parent_client.post(
            f"/credentials/{root_credential}/revoke",
            headers=owner_headers(parent_state) | {"Idempotency-Key": "revoke-parent"},
        ).status_code
        == 200
    )
    assert (
        parent_client.post(
            parent_path, headers=child | {"Idempotency-Key": "parent-review"}, json=parent_body
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    ("changes", "status"),
    (
        ({"expected_revision": True}, 422),
        ({"expected_revision": 1.0}, 422),
        ({"unexpected": "field"}, 422),
        ({"rationale": "   "}, 400),
        ({"expected_manifest_digest": "A" * 64}, 422),
    ),
)
def test_disposition_api_rejects_strict_or_blank_request_fields(
    tmp_path: Path, changes: dict[str, object], status: int
) -> None:
    state, _worker, _owner, manifest = _manifest(tmp_path)
    response = _client(state).post(
        _path(manifest),
        headers=owner_headers(state) | {"Idempotency-Key": "malformed"},
        json=_request(manifest, **changes),
    )
    assert response.status_code == status


def test_disposition_changed_body_conflicts_and_preserves_execution_research_history(
    tmp_path: Path,
) -> None:
    state, worker, _owner, manifest = _manifest(tmp_path)
    client = _client(state)
    path = _path(manifest)
    body = _request(manifest)
    before = _protected_snapshot(state)
    created = client.post(
        path, headers=owner_headers(state) | {"Idempotency-Key": "history"}, json=body
    )
    assert created.status_code == 201
    changed = body | {"rationale": "a changed replay body"}
    assert (
        client.post(
            path, headers=owner_headers(state) | {"Idempotency-Key": "history"}, json=changed
        ).status_code
        == 409
    )
    assert _protected_snapshot(state) == before

    # Late/revoked execution state fences new worker writes, but cannot erase human review history.
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE execution_grants SET status = 'cancel_requested'")
        connection.execute(
            "UPDATE credentials SET revoked_at = ? WHERE principal_id = ?",
            ("2026-01-01T00:00:00Z", worker["id"]),
        )
        connection.commit()
    late_before = _protected_snapshot(state)
    second = _request(manifest, expected_revision=1, rationale="late history review")
    assert (
        client.post(
            path,
            headers=owner_headers(state) | {"Idempotency-Key": "late-history"},
            json=second,
        ).status_code
        == 201
    )
    assert _protected_snapshot(state) == late_before
    listed = client.get(path, headers=owner_headers(state))
    assert listed.status_code == 200
    assert [row["revision"] for row in listed.json()["dispositions"]] == [1, 2]
    page = client.get(f"{path}?offset=1&limit=1", headers=owner_headers(state))
    assert page.status_code == 200 and [row["revision"] for row in page.json()["dispositions"]] == [
        2
    ]
    assert client.get(f"{path}?offset=-1", headers=owner_headers(state)).status_code == 422
    assert client.get(f"{path}?limit=0", headers=owner_headers(state)).status_code == 422
    assert _protected_snapshot(state) == late_before
