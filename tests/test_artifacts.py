from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import _grant_payload, _setup, _worker
from test_input_delivery import _delivery

from tessera_control.api import create_app
from tessera_control.artifacts import MAX_BASE64_CHARS, ArtifactStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    canonical_digest,
)


def _payload(lease: dict[str, object], content: bytes = b"result") -> dict[str, object]:
    return {
        "expected_epoch": lease["epoch"],
        "operation_id": hashlib.sha256(f"tessera-cpu-v1:{lease['id']}".encode()).hexdigest()[:32],
        "relative_path": "result.txt",
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }


def test_metadata_operations_never_read_quarantined_blob(tmp_path, monkeypatch):
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    payload = _payload(lease)
    store.upload_artifact(worker, lease["id"], "first", canonical_digest(payload), payload)

    def metadata_connection(path):
        connection = _connect(path)
        connection.set_authorizer(
            lambda action, table, column, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "artifacts" and column == "content"
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    monkeypatch.setattr("tessera_control.store._connect", metadata_connection)
    monkeypatch.setattr("tessera_control.artifacts._connect", metadata_connection)
    with pytest.raises(Conflict, match="immutable"):
        store.upload_artifact(worker, lease["id"], "duplicate", canonical_digest(payload), payload)
    listed = store.list_artifacts(owner, grant["project_id"], 0, 10)
    assert len(listed) == 1 and "content" not in listed[0]


def _two_path_delivery(tmp_path: Path, max_output_bytes: int = 1024):
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    spec = {
        "argv": ["python", "-c", "print('pilot')"],
        "input_snapshot_digest": task["current_plan_digest"],
        "pinned_skill_digests": [],
        "output_paths": ["first.txt", "second.txt"],
        "profile": "local-cpu",
        "network_enabled": False,
        "max_output_bytes": max_output_bytes,
    }
    # Use the original input snapshot digest from the execution task revision.
    with _connect(execution.database_path) as connection:
        spec["input_snapshot_digest"] = connection.execute(
            "SELECT digest FROM source_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()[0]
    revision = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": task["budget_cents"],
        "max_duration_seconds": task["max_duration_seconds"],
        "execution_spec": spec,
    }
    _, revised = Store(state).revise_task_plan(
        owner, task["id"], "two-path-revise", canonical_digest(revision), revision
    )
    _, approved = Store(state).approve_task(
        owner,
        task["id"],
        "two-path-approve",
        canonical_digest({"expected_revision": revised["revision"]}),
        revised["revision"],
        revised["current_plan_digest"],
    )
    payload = _grant_payload(approved, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "two-path-grant", canonical_digest(payload), payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "two-path-lease", "lease")
    return state, worker, lease


def test_upload_is_quarantined_and_replay_is_metadata_only(tmp_path: Path) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    payload = _payload(lease)
    first = store.upload_artifact(
        worker, lease["id"], "artifact", canonical_digest(payload), payload
    )
    replay = store.upload_artifact(
        worker, lease["id"], "artifact", canonical_digest(payload), payload
    )
    assert first == replay and "content" not in first[1]
    with _connect(store.database_path) as connection:
        row = connection.execute("SELECT status, classification, content FROM artifacts").fetchone()
        assert tuple(row)[:2] == ("quarantined", "unclassified")
        assert row[2] == b"result"
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='worker.artifact_quarantined'"
            ).fetchone()[0]
            == 1
        )
        assert (
            "content_base64"
            not in connection.execute(
                "SELECT response_json FROM idempotency WHERE path LIKE '%/artifacts'"
            ).fetchone()[0]
        )


def test_genuinely_legacy_cpu_lease_accepts_only_its_derived_operation(tmp_path: Path) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    payload = _payload(lease)

    status, artifact = store.upload_artifact(
        worker, lease["id"], "legacy-cpu", canonical_digest(payload), payload
    )

    assert status == 201
    assert artifact["operation_id"] == payload["operation_id"]
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM external_operations WHERE lease_id=?", (lease["id"],)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "change",
    [
        {"relative_path": "../result.txt"},
        {"operation_id": "0" * 32},
        {"expected_epoch": 999},
    ],
)
def test_upload_rejects_invalid_scope_fields(tmp_path: Path, change: dict[str, object]) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    payload = _payload(lease)
    payload.update(change)
    with pytest.raises((Conflict, StoreError)):
        store.upload_artifact(worker, lease["id"], "invalid", canonical_digest(payload), payload)


def test_event_failure_rolls_back_artifact_and_idempotency(tmp_path: Path, monkeypatch) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    payload = _payload(lease)

    def fail(*args):
        raise sqlite3.OperationalError("synthetic event failure")

    monkeypatch.setattr(Store, "_event", staticmethod(fail))
    with pytest.raises(sqlite3.OperationalError):
        store.upload_artifact(worker, lease["id"], "event", canonical_digest(payload), payload)
    with _connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%/artifacts'"
            ).fetchone()[0]
            == 0
        )


def test_path_is_immutable_and_worker_revocation_fences_replay(tmp_path: Path) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    payload = _payload(lease)
    store.upload_artifact(worker, lease["id"], "once", canonical_digest(payload), payload)
    changed = _payload(lease, b"changed")
    with pytest.raises(Conflict):
        store.upload_artifact(worker, lease["id"], "different", canonical_digest(changed), changed)
    with _connect(store.database_path) as connection:
        connection.execute(
            "UPDATE credentials SET revoked_at = '2026-01-01T00:00:00Z' WHERE principal_id = ?",
            (worker["id"],),
        )
    with pytest.raises(Forbidden):
        store.upload_artifact(worker, lease["id"], "once", canonical_digest(payload), payload)


def test_cumulative_budget_applies_across_declared_paths(tmp_path: Path) -> None:
    state, worker, lease = _two_path_delivery(tmp_path)
    store = ArtifactStore(state)
    first = _payload(lease, b"a" * 600)
    first["relative_path"] = "first.txt"
    store.upload_artifact(worker, lease["id"], "budget-one", canonical_digest(first), first)
    second = _payload(lease, b"b" * 600)
    second["relative_path"] = "second.txt"
    with pytest.raises(Conflict):
        store.upload_artifact(worker, lease["id"], "budget-two", canonical_digest(second), second)


def test_wrong_worker_and_cross_project_reads_are_denied(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    other = _worker(state, owner, grant["project_id"], "other-worker")
    payload = _payload(lease)
    with pytest.raises(NotFound):
        store.upload_artifact(other, lease["id"], "wrong", canonical_digest(payload), payload)
    with pytest.raises(NotFound):
        store.list_artifacts(owner, "project-does-not-exist", 0, 10)


def test_hash_and_oversize_are_rejected_before_storage(tmp_path: Path, monkeypatch) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    bad = _payload(lease)
    bad["sha256"] = "0" * 64
    with pytest.raises(Conflict):
        store.upload_artifact(worker, lease["id"], "bad-hash", canonical_digest(bad), bad)
    oversized = _payload(lease)
    oversized["content_base64"] = "A" * (MAX_BASE64_CHARS + 1)
    with pytest.raises(StoreError):
        store.upload_artifact(
            worker, lease["id"], "oversize", canonical_digest(oversized), oversized
        )


def test_expired_lease_is_retained_as_late(tmp_path: Path) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    with _connect(store.database_path) as connection:
        connection.execute(
            "UPDATE execution_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (lease["id"],),
        )
    payload = _payload(lease)
    store.upload_artifact(worker, lease["id"], "expired", canonical_digest(payload), payload)
    with _connect(store.database_path) as connection:
        assert connection.execute("SELECT late FROM artifacts").fetchone()[0] == 1


def test_revoked_grant_is_retained_as_late(tmp_path: Path) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    with _connect(store.database_path) as connection:
        connection.execute(
            "UPDATE execution_grants SET status = 'cancel_requested' WHERE id = "
            "(SELECT grant_id FROM execution_leases WHERE id = ?)",
            (lease["id"],),
        )
    payload = _payload(lease)
    store.upload_artifact(worker, lease["id"], "revoked", canonical_digest(payload), payload)
    with _connect(store.database_path) as connection:
        assert connection.execute("SELECT late FROM artifacts").fetchone()[0] == 1


def test_api_audiences_are_isolated(tmp_path: Path) -> None:
    state, _, owner, worker, _, lease = _delivery(tmp_path)
    app = TestClient(create_app(state))
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    payload = _payload(lease)
    assert (
        app.post(
            f"/worker/leases/{lease['id']}/artifacts",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "owner"},
            json=payload,
        ).status_code
        == 401
    )
    assert (
        app.get(
            f"/projects/{lease['grant_id']}/artifacts",
            headers={"Authorization": "Bearer worker-token"},
        ).status_code
        == 401
    )


def _begin_payload(lease: dict[str, object], content: bytes, path: str = "result.txt"):
    return {
        "expected_epoch": lease["epoch"],
        "operation_id": hashlib.sha256(f"tessera-cpu-v1:{lease['id']}".encode()).hexdigest()[:32],
        "relative_path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def test_resumable_api_refreshes_status_and_clears_staged_bytes(tmp_path: Path) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    app = TestClient(create_app(state))
    headers = {"Authorization": "Bearer worker-token", "Idempotency-Key": "begin"}
    content = b"resumable-result"
    begin = _begin_payload(lease, content)
    response = app.post(
        f"/worker/leases/{lease['id']}/artifact-uploads", headers=headers, json=begin
    )
    assert response.status_code == 201
    upload = response.json()
    upload_id = upload["id"]
    chunk = {"offset": 0, "content_base64": base64.b64encode(content[:4]).decode()}
    response = app.post(
        f"/worker/leases/{lease['id']}/artifact-uploads/{upload_id}/chunks",
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "chunk"},
        json=chunk,
    )
    assert response.status_code == 200 and response.json()["received_bytes"] == 4
    status_headers = {"Authorization": "Bearer worker-token", "Idempotency-Key": "status"}
    assert (
        app.post(
            f"/worker/leases/{lease['id']}/artifact-uploads/{upload_id}/status",
            headers=status_headers,
            json={},
        ).json()["received_bytes"]
        == 4
    )
    # A replay with the same status key re-reads current metadata rather than stale cache.
    remainder = {"offset": 4, "content_base64": base64.b64encode(content[4:]).decode()}
    assert (
        app.post(
            f"/worker/leases/{lease['id']}/artifact-uploads/{upload_id}/chunks",
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "remainder"},
            json=remainder,
        ).status_code
        == 200
    )
    assert app.post(
        f"/worker/leases/{lease['id']}/artifact-uploads/{upload_id}/status",
        headers=status_headers,
        json={},
    ).json()["received_bytes"] == len(content)
    assert (
        app.post(
            f"/worker/leases/{lease['id']}/artifact-uploads/{upload_id}/complete",
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "complete"},
            json={},
        ).json()["status"]
        == "quarantined"
    )
    with _connect(state / "tessera.sqlite3") as connection:
        row = connection.execute(
            "SELECT status, received_bytes, staged_content FROM artifact_uploads WHERE id=?",
            (upload_id,),
        ).fetchone()
        assert tuple(row) == ("completed", len(content), None)
        cached = connection.execute(
            "SELECT response_json FROM idempotency WHERE idempotency_key='chunk'"
        ).fetchone()[0]
        assert "content_base64" not in cached and content.decode() not in cached
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='worker.artifact_quarantined'"
            ).fetchone()[0]
            == 1
        )


def test_resumable_api_accepts_more_than_one_megabyte_in_bounded_chunks(tmp_path: Path) -> None:
    state, worker, lease = _two_path_delivery(tmp_path, max_output_bytes=2 * 1024 * 1024)
    app = TestClient(create_app(state))
    content = b"x" * (1024 * 1024 + 1)
    begin = _begin_payload(lease, content, "first.txt")
    response = app.post(
        f"/worker/leases/{lease['id']}/artifact-uploads",
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "large-begin"},
        json=begin,
    )
    assert response.status_code == 201
    upload_id = response.json()["id"]
    for offset in range(0, len(content), 256 * 1024):
        piece = content[offset : offset + 256 * 1024]
        response = app.post(
            f"/worker/leases/{lease['id']}/artifact-uploads/{upload_id}/chunks",
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": f"large-{offset}"},
            json={"offset": offset, "content_base64": base64.b64encode(piece).decode()},
        )
        assert response.status_code == 200
    complete = app.post(
        f"/worker/leases/{lease['id']}/artifact-uploads/{upload_id}/complete",
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "large-complete"},
        json={},
    )
    assert complete.status_code == 201 and complete.json()["size"] == len(content)
    assert worker["id"] == complete.json()["worker_principal_id"]


def test_pending_upload_reservation_blocks_direct_upload_and_bad_complete_keeps_quarantine_empty(
    tmp_path: Path,
) -> None:
    state, worker, lease = _two_path_delivery(tmp_path)
    store = ArtifactStore(state)
    pending = b"a" * 600
    begin = _begin_payload(lease, pending, "first.txt")
    begin["sha256"] = "0" * 64
    _, upload = store.begin_upload(worker, lease["id"], "reserve", canonical_digest(begin), begin)
    direct = _payload(lease, b"b" * 500)
    direct["relative_path"] = "second.txt"
    with pytest.raises(Conflict, match="budget"):
        store.upload_artifact(worker, lease["id"], "direct", canonical_digest(direct), direct)

    chunk = {"offset": 0, "content_base64": base64.b64encode(pending).decode()}
    store.append_chunk(
        worker, lease["id"], upload["id"], "pending-bytes", canonical_digest(chunk), chunk
    )
    with pytest.raises(Conflict, match="size or checksum"):
        store.complete_upload(
            worker, lease["id"], upload["id"], "bad-complete", canonical_digest({})
        )
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert tuple(
            connection.execute(
                "SELECT status, received_bytes FROM artifact_uploads WHERE id=?", (upload["id"],)
            ).fetchone()
        ) == ("uploading", len(pending))


def test_upload_reservations_allow_exact_limit_in_either_order(tmp_path: Path) -> None:
    state, worker, lease = _two_path_delivery(tmp_path)
    store = ArtifactStore(state)
    staged = _begin_payload(lease, b"a" * 600, "first.txt")
    store.begin_upload(worker, lease["id"], "staged-first", canonical_digest(staged), staged)
    direct = _payload(lease, b"b" * 424)
    direct["relative_path"] = "second.txt"
    assert (
        store.upload_artifact(
            worker, lease["id"], "direct-second", canonical_digest(direct), direct
        )[0]
        == 201
    )

    reverse = tmp_path / "reverse"
    reverse.mkdir()
    state, worker, lease = _two_path_delivery(reverse)
    store = ArtifactStore(state)
    direct = _payload(lease, b"b" * 600)
    direct["relative_path"] = "second.txt"
    assert (
        store.upload_artifact(
            worker, lease["id"], "direct-first", canonical_digest(direct), direct
        )[0]
        == 201
    )
    staged = _begin_payload(lease, b"a" * 424, "first.txt")
    assert (
        store.begin_upload(worker, lease["id"], "staged-second", canonical_digest(staged), staged)[
            0
        ]
        == 201
    )


def test_resumable_commands_fence_foreign_worker_and_revoked_replays(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    content = b"test"
    begin = _begin_payload(lease, content)
    _, upload = store.begin_upload(worker, lease["id"], "begin", canonical_digest(begin), begin)
    other = _worker(state, owner, grant["project_id"], "other-upload-worker")
    chunk = {"offset": 0, "content_base64": base64.b64encode(content).decode()}
    with pytest.raises(NotFound):
        store.append_chunk(
            other, lease["id"], upload["id"], "foreign", canonical_digest(chunk), chunk
        )
    with pytest.raises(NotFound):
        store.upload_status(
            other, lease["id"], upload["id"], "foreign-status", canonical_digest({})
        )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE credentials SET revoked_at='2026-01-01T00:00:00Z' WHERE principal_id=?",
            (worker["id"],),
        )
    with pytest.raises(Forbidden):
        store.begin_upload(worker, lease["id"], "begin", canonical_digest(begin), begin)
    with pytest.raises(Forbidden):
        store.upload_status(worker, lease["id"], upload["id"], "status", canonical_digest({}))


def test_resumable_metadata_commands_do_not_read_staged_bytes(tmp_path: Path, monkeypatch) -> None:
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    content = b"metadata"
    begin = _begin_payload(lease, content)
    _, upload = store.begin_upload(worker, lease["id"], "begin", canonical_digest(begin), begin)

    def metadata_connection(path):
        connection = _connect(path)
        connection.set_authorizer(
            lambda action, table, column, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table == "artifact_uploads"
                and column == "staged_content"
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    monkeypatch.setattr("tessera_control.store._connect", metadata_connection)
    monkeypatch.setattr("tessera_control.artifacts._connect", metadata_connection)
    assert (
        store.begin_upload(worker, lease["id"], "new-key", canonical_digest(begin), begin)[0] == 200
    )
    assert (
        store.upload_status(worker, lease["id"], upload["id"], "status", canonical_digest({}))[0]
        == 200
    )
