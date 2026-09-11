from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest
from test_artifact_manifests import _body, _completed_outputs

from tessera_control.artifact_dispositions import ArtifactDispositionStore
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.migrations import MIGRATIONS
from tessera_control.store import (
    Conflict,
    Forbidden,
    Store,
    StoreError,
    _connect,
    canonical_digest,
    migrate_state,
)


def _manifest(tmp_path: Path):
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    body = _body(lease, first, second)
    _, manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(body), body
    )
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    return state, worker, Store(state).authenticate(token), manifest


def _request(manifest: dict, **changes):
    body = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 0,
        "disposition": "reject",
        "rationale": "reviewed output",
    }
    body.update(changes)
    return body


def _retained_records(state: Path):
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in {"schema_meta", "artifact_dispositions"}
        ]
        return {
            name: sorted(
                (
                    tuple(row)
                    for row in connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
                ),
                key=repr,
            )
            for name in tables
        }


def test_disposition_is_human_only_cas_append_only_and_replayable(tmp_path: Path):
    state, worker, owner, manifest = _manifest(tmp_path)
    store = ArtifactDispositionStore(state)
    first = _request(manifest)
    status, original = store.record_disposition(
        owner, manifest["id"], "first", canonical_digest(first), first
    )
    assert status == 201 and original["revision"] == 1
    newer = _request(
        manifest, expected_revision=1, disposition="retain_quarantined", rationale="retained"
    )
    store.record_disposition(owner, manifest["id"], "second", canonical_digest(newer), newer)
    assert (
        store.record_disposition(owner, manifest["id"], "first", canonical_digest(first), first)[1]
        == original
    )
    assert [row["revision"] for row in store.list_dispositions(owner, manifest["id"], 0, 200)] == [
        1,
        2,
    ]
    with pytest.raises(Conflict):
        store.record_disposition(owner, manifest["id"], "stale", canonical_digest(first), first)
    with pytest.raises(Forbidden):
        store.record_disposition(worker, manifest["id"], "worker", canonical_digest(first), first)
    with _connect(state / "tessera.sqlite3") as connection:
        row = connection.execute("SELECT status, classification FROM artifact_manifests").fetchone()
        assert (row["status"], row["classification"]) == ("quarantined", "unclassified")


def test_disposition_rejects_wrong_digest_and_blank_rationale(tmp_path: Path):
    state, _, owner, manifest = _manifest(tmp_path)
    store = ArtifactDispositionStore(state)
    wrong = _request(manifest, expected_manifest_digest="0" * 64)
    with pytest.raises(Conflict):
        store.record_disposition(owner, manifest["id"], "wrong", canonical_digest(wrong), wrong)
    blank = _request(manifest, rationale="   ")
    with pytest.raises(StoreError):
        store.record_disposition(owner, manifest["id"], "blank", canonical_digest(blank), blank)


def test_v9_to_v10_migration_is_transactional_and_preserves_manifest(tmp_path: Path, monkeypatch):
    state, _, _, manifest = _manifest(tmp_path)
    retained = _retained_records(state)
    header = (state / "owner.header").read_bytes()
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE artifact_dispositions")
        connection.execute("DROP TABLE execution_result_checkpoints")
        from schema_fixtures import drop_empty_experiment_schema

        drop_empty_experiment_schema(connection)
        connection.execute("UPDATE schema_meta SET schema_version=9")

    def fail(connection):
        from tessera_control.artifact_dispositions import create_artifact_disposition_schema

        create_artifact_disposition_schema(connection)
        raise RuntimeError("disposition migration fault")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 9, fail)
        with pytest.raises(RuntimeError, match="disposition migration"):
            migrate_state(state)
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 9
        assert (
            connection.execute("SELECT id FROM artifact_manifests").fetchone()[0] == manifest["id"]
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='artifact_dispositions'"
            ).fetchone()
            is None
        )
    assert migrate_state(state) == 34
    assert migrate_state(state) == 34
    assert _retained_records(state) == retained
    assert (state / "owner.header").read_bytes() == header


def test_disposition_event_failure_rolls_back_and_pages_metadata(tmp_path: Path, monkeypatch):
    state, _, owner, manifest = _manifest(tmp_path)
    store = ArtifactDispositionStore(state)
    body = _request(manifest)

    def fail(*_args):
        raise sqlite3.OperationalError("event failure")

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(sqlite3.OperationalError):
        store.record_disposition(owner, manifest["id"], "failed", canonical_digest(body), body)
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifact_dispositions").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE idempotency_key='failed'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type LIKE '%disposition%'"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.undo()
    store.record_disposition(owner, manifest["id"], "failed", canonical_digest(body), body)
    body2 = _request(manifest, expected_revision=1, rationale="second")
    store.record_disposition(owner, manifest["id"], "two", canonical_digest(body2), body2)
    page = store.list_dispositions(owner, manifest["id"], 1, 1)
    assert list(page[0]) == [
        "id",
        "manifest_id",
        "manifest_digest",
        "revision",
        "disposition",
        "rationale",
        "actor_principal_id",
        "created_at",
    ]


def test_concurrent_opposite_dispositions_have_one_cas_winner(tmp_path: Path):
    state, _, owner, manifest = _manifest(tmp_path)
    body_a = _request(manifest, disposition="reject", rationale="a")
    body_b = _request(manifest, disposition="retain_quarantined", rationale="b")
    ready = Barrier(2)

    def submit(key, body):
        ready.wait(timeout=5)
        try:
            return ArtifactDispositionStore(state).record_disposition(
                owner, manifest["id"], key, canonical_digest(body), body
            )[0]
        except Conflict:
            return 409

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, ("a", "b"), (body_a, body_b)))
    assert sorted(outcomes) == [201, 409]
    assert (
        len(ArtifactDispositionStore(state).list_dispositions(owner, manifest["id"], 0, 200)) == 1
    )
