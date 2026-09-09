from __future__ import annotations

import base64

import pytest
from test_artifacts import _begin_payload, _payload
from test_input_delivery import _delivery

from tessera_control.artifacts import ArtifactStore
from tessera_control.store import Conflict, Forbidden, _connect, canonical_digest, migrate_state


def test_chunk_and_completion_event_failures_roll_back_staging_and_quarantine(
    tmp_path, monkeypatch
):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    content = b"retained\x00\xff"
    body = _begin_payload(lease, content)
    _, upload = store.begin_upload(worker, lease["id"], "begin", canonical_digest(body), body)
    chunk = {"offset": 0, "content_base64": base64.b64encode(content).decode()}

    def append():
        return store.append_chunk(
            worker, lease["id"], upload["id"], "chunk", canonical_digest(chunk), chunk
        )

    def complete():
        return store.complete_upload(
            worker, lease["id"], upload["id"], "complete", canonical_digest({})
        )

    def fail(*args):
        raise RuntimeError("injected event failure")

    for action, received in [(append, 0), (complete, len(content))]:
        with monkeypatch.context() as patch:
            patch.setattr(store, "_event", fail)
            with pytest.raises(RuntimeError, match="injected"):
                action()
        with _connect(store.database_path) as connection:
            row = connection.execute(
                "SELECT status, received_bytes, staged_content FROM artifact_uploads"
            ).fetchone()
            assert tuple(row) == ("uploading", received, content[:received])
            assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        action()
    with _connect(store.database_path) as connection:
        assert connection.execute("SELECT content FROM artifacts").fetchone()[0] == content
        assert (
            connection.execute("SELECT staged_content FROM artifact_uploads").fetchone()[0] is None
        )


def test_stale_chunk_offsets_do_not_duplicate_bytes_and_revoked_replays_all_fail(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    body = _begin_payload(lease, b"result")

    def begin():
        return store.begin_upload(worker, lease["id"], "begin", canonical_digest(body), body)

    _, upload = begin()
    chunk = {"offset": 0, "content_base64": base64.b64encode(b"result").decode()}

    def append():
        return store.append_chunk(
            worker, lease["id"], upload["id"], "chunk", canonical_digest(chunk), chunk
        )

    def status():
        return store.upload_status(
            worker, lease["id"], upload["id"], "status", canonical_digest({})
        )

    def complete():
        return store.complete_upload(
            worker, lease["id"], upload["id"], "complete", canonical_digest({})
        )

    first = append()
    assert append() == first
    with pytest.raises(Conflict, match="offset"):
        store.append_chunk(worker, lease["id"], upload["id"], "new", canonical_digest(chunk), chunk)
    assert status()[1]["received_bytes"] == 6
    complete()
    with _connect(store.database_path) as connection:
        connection.execute(
            "UPDATE credentials SET revoked_at='2026-01-01T00:00:00Z' WHERE principal_id=?",
            (worker["id"],),
        )
    for action in [begin, append, status, complete]:
        with pytest.raises(Forbidden):
            action()


def test_grant_revocation_during_transfer_retains_late_quarantine(tmp_path):
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    body = _begin_payload(lease, b"late")
    _, upload = store.begin_upload(worker, lease["id"], "begin", canonical_digest(body), body)
    execution.revoke_execution_grant(owner, grant["id"], "stop", "stop", 1)
    chunk = {"offset": 0, "content_base64": base64.b64encode(b"late").decode()}
    store.append_chunk(worker, lease["id"], upload["id"], "chunk", canonical_digest(chunk), chunk)
    result = store.complete_upload(
        worker, lease["id"], upload["id"], "complete", canonical_digest({})
    )[1]
    assert result["late"] is True and result["status"] == "quarantined"
    with _connect(store.database_path) as connection:
        assert (
            connection.execute("SELECT status FROM execution_grants").fetchone()[0]
            == "cancel_requested"
        )


def test_schema7_migration_preserves_existing_artifact_bytes_and_replay(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = ArtifactStore(state)
    body = _payload(lease, b"legacy\x00\xff")
    args = (worker, lease["id"], "artifact", canonical_digest(body), body)
    original = store.upload_artifact(*args)
    with _connect(store.database_path) as connection:
        connection.execute("DROP TABLE artifact_dispositions")
        connection.execute("DROP TABLE artifact_manifests")
        connection.execute("DROP TABLE artifact_uploads")
        connection.execute("DROP TABLE execution_result_checkpoints")
        connection.execute("UPDATE schema_meta SET schema_version=7")
    assert migrate_state(state) == 24
    assert ArtifactStore(state).upload_artifact(*args) == original
    with _connect(store.database_path) as connection:
        assert (
            connection.execute("SELECT content FROM artifacts").fetchone()[0] == b"legacy\x00\xff"
        )
