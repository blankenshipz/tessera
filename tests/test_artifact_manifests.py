from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_artifacts import _payload, _two_path_delivery
from test_execution import _grant_payload, _setup, _worker

import tessera_control.artifact_manifests as manifests_module
from tessera_control.api import create_app
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.artifacts import ArtifactStore
from tessera_control.migrations import MIGRATIONS
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _connect,
    canonical_digest,
    migrate_state,
)


def _operation(lease: dict[str, object]) -> str:
    return hashlib.sha256(f"tessera-cpu-v1:{lease['id']}".encode()).hexdigest()[:32]


def _completed_outputs(tmp_path: Path):
    state, worker, lease = _two_path_delivery(tmp_path)
    artifacts = ArtifactStore(state)
    direct = _payload(lease, b"direct")
    direct["relative_path"] = "first.txt"
    _, first = artifacts.upload_artifact(
        worker, lease["id"], "first", canonical_digest(direct), direct
    )
    content = b"chunked"
    begin = {
        "expected_epoch": lease["epoch"],
        "operation_id": _operation(lease),
        "relative_path": "second.txt",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
    _, upload = artifacts.begin_upload(worker, lease["id"], "begin", canonical_digest(begin), begin)
    chunk = {"offset": 0, "content_base64": base64.b64encode(content).decode()}
    artifacts.append_chunk(
        worker, lease["id"], upload["id"], "chunk", canonical_digest(chunk), chunk
    )
    _, second = artifacts.complete_upload(
        worker, lease["id"], upload["id"], "complete", canonical_digest({})
    )
    return state, worker, lease, first, second


def _body(lease: dict[str, object], *artifacts: dict[str, object]) -> dict[str, object]:
    return {
        "expected_epoch": lease["epoch"],
        "operation_id": _operation(lease),
        "artifacts": [
            {key: artifact[key] for key in ("id", "relative_path", "sha256", "size")}
            for artifact in artifacts
        ],
    }


def test_manifest_is_canonical_quarantined_and_replays_immutably(tmp_path: Path) -> None:
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    store = ArtifactManifestStore(state)
    body = _body(lease, second, first)
    status, manifest = store.create_manifest(
        worker, lease["id"], "manifest", canonical_digest(body), body
    )
    assert status == 201
    assert manifest["status"] == "quarantined"
    assert manifest["classification"] == "unclassified"
    assert [entry["relative_path"] for entry in manifest["artifacts"]] == [
        "first.txt",
        "second.txt",
    ]
    owner = Store(state).authenticate(
        (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    )
    assert ArtifactManifestStore(state).list_manifests(owner, first["project_id"], 0, 200) == [
        manifest
    ]
    assert store.create_manifest(worker, lease["id"], "manifest", canonical_digest(body), body) == (
        201,
        manifest,
    )
    assert store.create_manifest(worker, lease["id"], "new-key", canonical_digest(body), body) == (
        200,
        manifest,
    )
    changed = _body(lease, first, second)
    changed["artifacts"] = changed["artifacts"][:-1]
    with pytest.raises(Conflict):
        store.create_manifest(worker, lease["id"], "changed", canonical_digest(changed), changed)
    with _connect(state / "tessera.sqlite3") as connection:
        event = connection.execute(
            """SELECT payload_json FROM events
            WHERE event_type='worker.artifact_manifest_quarantined'"""
        ).fetchone()[0]
        assert "content" not in event and "direct" not in event and "chunked" not in event


@pytest.mark.parametrize("change", ["missing", "duplicate", "foreign", "corrupt"])
def test_manifest_rejects_incomplete_mismatched_or_corrupt_artifacts(
    tmp_path: Path, change: str
) -> None:
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    body = _body(lease, first, second)
    if change == "missing":
        body["artifacts"] = body["artifacts"][:-1]
    elif change == "duplicate":
        body["artifacts"] = [body["artifacts"][0], body["artifacts"][0]]
    elif change == "foreign":
        body["artifacts"][0]["id"] = "artifact_not_owned"
    else:
        with _connect(state / "tessera.sqlite3") as connection:
            connection.execute(
                "UPDATE artifacts SET content=? WHERE id=?", (b"corrupt", first["id"])
            )
    with pytest.raises((Conflict, NotFound)):
        ArtifactManifestStore(state).create_manifest(
            worker, lease["id"], change, canonical_digest(body), body
        )
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0] == 0


def test_manifest_fences_workers_epochs_and_keeps_revoked_grant_late(tmp_path: Path) -> None:
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    store = ArtifactManifestStore(state)
    body = _body(lease, first, second)
    stale = dict(body, expected_epoch=lease["epoch"] + 1)
    with pytest.raises(Conflict, match="epoch"):
        store.create_manifest(worker, lease["id"], "stale", canonical_digest(stale), stale)
    owner = Store(state).authenticate(
        (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    )
    other = _worker(state, owner, first["project_id"], "manifest-other")
    with pytest.raises(NotFound):
        store.create_manifest(other, lease["id"], "foreign", canonical_digest(body), body)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_grants SET status='cancel_requested' WHERE id=?", (first["grant_id"],)
        )
    _, manifest = store.create_manifest(worker, lease["id"], "late", canonical_digest(body), body)
    assert manifest["late"] is True and manifest["status"] == "quarantined"
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE credentials SET revoked_at='2026-01-01T00:00:00Z' WHERE principal_id=?",
            (worker["id"],),
        )
    with pytest.raises(Forbidden):
        store.create_manifest(worker, lease["id"], "late", canonical_digest(body), body)


def test_manifest_api_requires_worker_audience_and_returns_quarantine_metadata(
    tmp_path: Path,
) -> None:
    state, _, lease, first, second = _completed_outputs(tmp_path)
    body = _body(lease, first, second)
    app = TestClient(create_app(state))
    response = app.post(
        f"/worker/leases/{lease['id']}/artifact-manifest",
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "manifest"},
        json=body,
    )
    assert response.status_code == 201 and response.json()["status"] == "quarantined"
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    assert (
        app.get(
            f"/projects/{first['project_id']}/artifact-manifests",
            headers={"Authorization": f"Bearer {token}"},
        ).json()["artifact_manifests"][0]["id"]
        == response.json()["id"]
    )


def test_manifest_accepts_exact_empty_approved_output_set(tmp_path: Path) -> None:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
        digest = connection.execute(
            "SELECT digest FROM source_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()[0]
    spec = {
        "argv": ["python", "-c", "print('pilot')"],
        "input_snapshot_digest": digest,
        "pinned_skill_digests": [],
        "output_paths": [],
        "profile": "local-cpu",
        "network_enabled": False,
        "max_output_bytes": 1,
    }
    revision = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": task["budget_cents"],
        "max_duration_seconds": task["max_duration_seconds"],
        "execution_spec": spec,
    }
    _, revised = Store(state).revise_task_plan(
        owner, task["id"], "empty-revise", canonical_digest(revision), revision
    )
    _, approved = Store(state).approve_task(
        owner,
        task["id"],
        "empty-approve",
        canonical_digest({"expected_revision": revised["revision"]}),
        revised["revision"],
        revised["current_plan_digest"],
    )
    grant_payload = _grant_payload(approved, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "empty-grant", canonical_digest(grant_payload), grant_payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "empty-lease", "empty")
    body = {"expected_epoch": lease["epoch"], "operation_id": _operation(lease), "artifacts": []}
    _, manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "empty-manifest", canonical_digest(body), body
    )
    assert manifest["artifacts"] == [] and manifest["total_bytes"] == 0


def test_manifest_event_failure_rolls_back_and_list_is_metadata_only(
    tmp_path: Path, monkeypatch
) -> None:
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    body = _body(lease, first, second)

    def fail(*args):
        raise sqlite3.OperationalError("event failure")

    monkeypatch.setattr(Store, "_event", staticmethod(fail))
    with pytest.raises(sqlite3.OperationalError):
        ArtifactManifestStore(state).create_manifest(
            worker, lease["id"], "event", canonical_digest(body), body
        )
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%artifact-manifest'"
            ).fetchone()[0]
            == 0
        )


def test_manifest_rejects_oversized_corrupt_blob_before_fetching_it(
    tmp_path: Path, monkeypatch
) -> None:
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    body = _body(lease, first, second)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE artifacts SET content=zeroblob(?) WHERE id=?",
            (16 * 1024 * 1024 + 1, first["id"]),
        )
    statements: list[str] = []
    original_connect = _connect

    def traced_connection(path):
        connection = original_connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(manifests_module, "_connect", traced_connection)
    with pytest.raises(Conflict):
        ArtifactManifestStore(state).create_manifest(
            worker, lease["id"], "oversized", canonical_digest(body), body
        )
    assert not any(
        statement.lstrip().startswith("SELECT content FROM artifacts") for statement in statements
    )


def test_schema8_manifest_migration_preserves_artifacts_and_uploads(
    tmp_path: Path, monkeypatch
) -> None:
    state, _, _, first, _ = _completed_outputs(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE artifact_manifests")
        connection.execute("DROP TABLE artifact_dispositions")
        connection.execute("DROP TABLE execution_result_checkpoints")
        from schema_fixtures import drop_empty_experiment_schema

        drop_empty_experiment_schema(connection)
        connection.execute("UPDATE schema_meta SET schema_version=8")

    def fail(connection):
        from tessera_control.artifact_manifests import create_artifact_manifest_schema

        create_artifact_manifest_schema(connection)
        raise RuntimeError("manifest migration fault")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 8, fail)
        with pytest.raises(RuntimeError, match="manifest migration"):
            migrate_state(state)
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 8
        assert first["id"] in {row[0] for row in connection.execute("SELECT id FROM artifacts")}
        assert connection.execute("SELECT count(*) FROM artifact_uploads").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='artifact_manifests'"
            ).fetchone()
            is None
        )
    assert migrate_state(state) == 33
    assert migrate_state(state) == 33
