"""Chunked input snapshots (ADR 0037 s1-2); synthetic bytes, tiny chunks."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_artifact_custody import _released_manifest
from test_dataset_staging import _body as _staging_body
from test_dataset_staging import _stage
from test_execution import _grant_payload, _owner, _task, _worker
from test_snapshots import _agent

from tessera_control import dataset_staging, snapshot_chunks
from tessera_control.api import create_app
from tessera_control.dataset_staging import DatasetStagingStore
from tessera_control.execution import ExecutionStore
from tessera_control.input_delivery import InputDeliveryStore
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.snapshot_chunks import ChunkedSnapshotStore, chunk_plan
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _connect,
    canonical_digest,
    initialize_state,
    migrate_state,
)
from tessera_control.workspace import (
    InventoryEntry,
    WorkspaceInventory,
    inventory_digest,
)

FILES = {"a.bin": bytes(range(20)), "empty.txt": b"", "z.txt": b"12345678"}


@pytest.fixture(autouse=True)
def tiny_chunks(monkeypatch):
    monkeypatch.setattr(snapshot_chunks, "CHUNK_BYTES", 8)


def _inventory(files):
    entries = tuple(
        InventoryEntry(path, len(data), hashlib.sha256(data).hexdigest())
        for path, data in sorted(files.items())
    )
    inventory = WorkspaceInventory("synthetic", entries, (), sum(len(d) for d in files.values()))
    manifest = json.loads(json.dumps(__import__("dataclasses").asdict(inventory)))
    return manifest, inventory_digest(inventory)


def _chunk_body(data):
    return {
        "content_base64": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _bytes_of(files, planned):
    data = files[planned["relative_path"]]
    return data[planned["offset"] : planned["offset"] + planned["size"]]


def _begin(store, owner, project_id, files, key="begin"):
    manifest, digest = _inventory(files)
    return store.begin_upload(
        owner, project_id, key, {"expected_digest": digest, "inventory": manifest}
    )


def _upload(store, owner, project_id, files, key="up"):
    status, begun = _begin(store, owner, project_id, files, key + "-begin")
    assert status == 201
    for planned in begun["chunk_plan"]:
        store.put_chunk(
            owner,
            project_id,
            begun["upload_id"],
            planned["seq"],
            _chunk_body(_bytes_of(files, planned)),
        )
    return store.complete_upload(owner, project_id, begun["upload_id"], key + "-done")[1], begun


def _state(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    owner = _owner(state)
    _, project = Store(state).create_project(
        owner, "project", canonical_digest({"name": "Pilot"}), {"name": "Pilot"}
    )
    return state, owner, project


def _release(state, owner, project_id, worker_id, snapshot_id, digest):
    payload = {
        "expected_digest": digest,
        "worker_principal_id": worker_id,
        "classification": "public",
        "permits_worker_transfer": True,
        "rationale": "Chunked pilot input release.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    return SnapshotReleaseStore(state).release_snapshot(
        owner, snapshot_id, "release", canonical_digest(payload), payload
    )[1]


def _lease(tmp_path, files=FILES):
    state, owner, project = _state(tmp_path)
    worker = _worker(state, owner, project["id"], "worker-token")
    snapshot, _ = _upload(ChunkedSnapshotStore(state), owner, project["id"], files)
    _release(state, owner, project["id"], worker["id"], snapshot["id"], snapshot["digest"])
    task = _task(state, owner, project["id"], snapshot["digest"], "First")
    execution = ExecutionStore(state)
    limits = {
        "expected_revision": 0,
        "currency": "USD",
        "monetary_limit_cents": 100,
        "max_concurrency": 1,
    }
    execution.configure_project_execution(
        owner, project["id"], "limits", canonical_digest(limits), limits
    )
    payload = _grant_payload(task, worker, {"id": snapshot["id"]})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    return state, owner, project, worker, snapshot, grant, lease


def test_chunk_plan_is_deterministic_and_skips_empty_files():
    entries = [{"relative_path": p, "size": len(d)} for p, d in sorted(FILES.items())]
    plan = chunk_plan(entries)
    assert plan == chunk_plan(entries)
    assert [(c["seq"], c["relative_path"], c["offset"], c["size"]) for c in plan] == [
        (0, "a.bin", 0, 8),
        (1, "a.bin", 8, 8),
        (2, "a.bin", 16, 4),
        (3, "z.txt", 0, 8),
    ]
    assert chunk_plan([]) == []


def test_upload_happy_path_event_once_and_replay(tmp_path):
    state, owner, project = _state(tmp_path)
    store = ChunkedSnapshotStore(state)
    snapshot, begun = _upload(store, owner, project["id"], FILES)
    assert snapshot["chunked"] and snapshot["digest"] == _inventory(FILES)[1]
    assert snapshot["id"] == begun["upload_id"] and snapshot["chunk_count"] == 4
    again = store.complete_upload(owner, project["id"], begun["upload_id"], "up-done")
    assert again == (201, snapshot)
    fetched = store.get_snapshot(owner, snapshot["id"])
    assert fetched["digest"] == snapshot["digest"] and len(fetched["manifest"]["entries"]) == 3
    assert store.verify_snapshot(owner, snapshot["id"])["integrity"] == "verified"
    with _connect(state / "tessera.sqlite3") as db:
        rows = db.execute(
            "SELECT payload_json FROM events WHERE event_type='workspace.snapshot_adopted'"
        ).fetchall()
        assert len(rows) == 1 and json.loads(rows[0][0])["chunked"] is True
        assert db.execute("SELECT count(*) FROM snapshot_files").fetchone()[0] == 0
        assert db.execute("SELECT status FROM snapshot_chunk_uploads").fetchone()[0] == "completed"
        assert (
            db.execute("SELECT max(length(response_json)) FROM idempotency").fetchone()[0] < 20000
        )
        assert (
            db.execute(
                "SELECT count(*) FROM idempotency WHERE response_json LIKE '%content%'"
            ).fetchone()[0]
            == 0
        )


def test_upload_requires_human(tmp_path):
    state, owner, project = _state(tmp_path)
    agent = _agent(state, owner, project["id"])
    store = ChunkedSnapshotStore(state)
    with pytest.raises(Forbidden):
        _begin(store, agent, project["id"], FILES, "agent")
    _, begun = _begin(store, owner, project["id"], FILES)
    with pytest.raises(Forbidden):
        store.put_chunk(agent, project["id"], begun["upload_id"], 0, _chunk_body(b"x" * 8))
    with pytest.raises(Forbidden):
        store.complete_upload(agent, project["id"], begun["upload_id"], "agent-done")
    client = TestClient(create_app(state))
    manifest, digest = _inventory(FILES)
    path = f"/projects/{project['id']}/chunked-snapshots"
    body = {"expected_digest": digest, "inventory": manifest}
    assert client.post(path, json=body, headers={"Idempotency-Key": "n"}).status_code == 401
    worker = {"Authorization": "Bearer worker-token", "Idempotency-Key": "w"}
    assert client.post(path, json=body, headers=worker).status_code == 401
    human = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    made = client.post(path, json=body, headers=human | {"Idempotency-Key": "h"})
    assert made.status_code == 201, made.text
    plan = made.json()["chunk_plan"]
    uid = made.json()["upload_id"]
    for planned in plan:
        put = client.put(
            f"{path}/{uid}/chunks/{planned['seq']}",
            json=_chunk_body(_bytes_of(FILES, planned)),
            headers=human,
        )
        assert put.status_code == 201, put.text
    done = client.post(f"{path}/{uid}/complete", headers=human | {"Idempotency-Key": "c"})
    assert done.status_code == 201 and done.json()["id"] == uid


def test_wrong_digest_or_inventory_is_409(tmp_path):
    state, owner, project = _state(tmp_path)
    store = ChunkedSnapshotStore(state)
    manifest, digest = _inventory(FILES)
    with pytest.raises(Conflict):
        store.begin_upload(
            owner, project["id"], "k1", {"expected_digest": "0" * 64, "inventory": manifest}
        )
    tampered = json.loads(json.dumps(manifest))
    tampered["entries"][0]["size"] += 1
    with pytest.raises(Conflict):
        store.begin_upload(
            owner, project["id"], "k2", {"expected_digest": digest, "inventory": tampered}
        )
    with _connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM snapshot_chunk_uploads").fetchone()[0] == 0


def test_chunk_validation_reput_and_incomplete_complete(tmp_path):
    state, owner, project = _state(tmp_path)
    store = ChunkedSnapshotStore(state)
    _, begun = _begin(store, owner, project["id"], FILES)
    uid, pid = begun["upload_id"], project["id"]
    good = _bytes_of(FILES, begun["chunk_plan"][0])
    with pytest.raises(Conflict):  # wrong size
        store.put_chunk(owner, pid, uid, 0, _chunk_body(good[:-1]))
    with pytest.raises(Conflict):  # sha mismatch
        store.put_chunk(owner, pid, uid, 0, _chunk_body(good) | {"sha256": "0" * 64})
    assert store.put_chunk(owner, pid, uid, 0, _chunk_body(good))[0] == 201
    assert store.put_chunk(owner, pid, uid, 0, _chunk_body(good))[0] == 200
    with pytest.raises(Conflict):  # different bytes
        store.put_chunk(owner, pid, uid, 0, _chunk_body(b"\xff" * 8))
    with pytest.raises(NotFound):
        store.put_chunk(owner, pid, uid, 99, _chunk_body(good))
    with pytest.raises(Conflict, match="missing"):
        store.complete_upload(owner, pid, uid, "early")
    with _connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 0


def test_complete_rejects_chunks_that_do_not_hash_to_the_file(tmp_path):
    state, owner, project = _state(tmp_path)
    store = ChunkedSnapshotStore(state)
    _, begun = _begin(store, owner, project["id"], FILES)
    for planned in begun["chunk_plan"]:
        store.put_chunk(
            owner,
            project["id"],
            begun["upload_id"],
            planned["seq"],
            _chunk_body(_bytes_of(FILES, planned)),
        )
    with _connect(state / "tessera.sqlite3") as db:
        db.execute(  # same length, consistent chunk hash, wrong file bytes
            "UPDATE snapshot_chunks SET content=?, sha256=? WHERE seq=1",
            (b"\x00" * 8, hashlib.sha256(b"\x00" * 8).hexdigest()),
        )
    with pytest.raises(Conflict):
        store.complete_upload(owner, project["id"], begun["upload_id"], "bad")
    with _connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 0


def test_delivery_index_chunk_route_and_fences(tmp_path):
    state, owner, project, worker, snapshot, grant, lease = _lease(tmp_path)
    app = TestClient(create_app(state))
    auth = {"Authorization": "Bearer worker-token"}
    base = f"/worker/leases/{lease['id']}/inputs"
    first = app.post(
        base, headers=auth | {"Idempotency-Key": "in"}, json={"expected_epoch": lease["epoch"]}
    )
    assert first.status_code == 201, first.text
    replay = app.post(
        base, headers=auth | {"Idempotency-Key": "in"}, json={"expected_epoch": lease["epoch"]}
    )
    assert replay.json() == first.json()
    snap = first.json()["snapshot"]
    assert snap["delivery"] == "chunked" and "files" not in snap and snap["id"] == snapshot["id"]
    assert [c["seq"] for c in snap["chunks"]] == [0, 1, 2, 3]
    received = b""
    for item in snap["chunks"]:
        got = app.post(
            f"{base}/chunks/{item['seq']}", headers=auth, json={"expected_epoch": lease["epoch"]}
        )
        assert got.status_code == 200, got.text
        payload = got.json()
        data = base64.b64decode(payload["content_base64"])
        assert hashlib.sha256(data).hexdigest() == item["sha256"] == payload["sha256"]
        assert payload["relative_path"] == item["relative_path"] and payload["size"] == len(data)
        if item["relative_path"] == "a.bin":
            received += data
    assert received == FILES["a.bin"]
    owner_token = (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
    denied = app.post(
        f"{base}/chunks/0",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"expected_epoch": lease["epoch"]},
    )
    assert denied.status_code == 401
    assert (
        app.post(
            f"{base}/chunks/99", headers=auth, json={"expected_epoch": lease["epoch"]}
        ).status_code
        == 404
    )
    with _connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute("SELECT max(length(response_json)) FROM idempotency").fetchone()[0] < 20000
        )
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE payload_json LIKE '%content_base64%'"
            ).fetchone()[0]
            == 0
        )
        events = db.execute("SELECT count(*) FROM events").fetchone()[0]
    app.post(f"{base}/chunks/0", headers=auth, json={"expected_epoch": lease["epoch"]})
    with _connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == events  # reads emit none


def test_chunk_route_fences(tmp_path):
    state, owner, project, worker, snapshot, grant, lease = _lease(tmp_path)
    store = InputDeliveryStore(state)
    epoch = lease["epoch"]
    assert store.read_input_chunk(worker, lease["id"], 0, epoch)["seq"] == 0
    other = _worker(state, owner, project["id"], "other-worker")
    with pytest.raises(NotFound):
        store.read_input_chunk(other, lease["id"], 0, epoch)
    with pytest.raises(Conflict):
        store.read_input_chunk(worker, lease["id"], 0, epoch + 1)
    with _connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (lease["id"],),
        )
    with pytest.raises(Conflict):
        store.read_input_chunk(worker, lease["id"], 0, epoch)
    with _connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE execution_leases SET expires_at=? WHERE id=?",
            (lease["expires_at"], lease["id"]),
        )
        assert store.read_input_chunk(worker, lease["id"], 1, epoch)["seq"] == 1
        db.execute(
            "UPDATE snapshot_releases SET status='revoked' WHERE id=?",
            (grant["snapshot_release_id"],),
        )
    with pytest.raises(Forbidden):
        store.read_input_chunk(worker, lease["id"], 0, epoch)


def test_tampered_chunk_row_passes_metadata_but_delivery_refuses(tmp_path):
    state, owner, project, worker, snapshot, grant, lease = _lease(tmp_path)
    with _connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE snapshot_chunks SET content=? WHERE seq=0", (b"\xaa" * 8,))
        # Authorization metadata check (release/grant/lease paths) does not re-hash bytes.
        SnapshotStore._verify_retained(db, worker, snapshot["id"])
    store = InputDeliveryStore(state)
    with pytest.raises(Conflict):
        store.read_input_chunk(worker, lease["id"], 0, lease["epoch"])
    assert store.read_input_chunk(worker, lease["id"], 1, lease["epoch"])["seq"] == 1
    with pytest.raises(Conflict):
        SnapshotStore(state).verify_snapshot(owner, snapshot["id"])
    with _connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE snapshot_chunks SET content=x'aa' WHERE seq=0")  # wrong length
        with pytest.raises(Conflict):
            SnapshotStore._verify_retained(db, worker, snapshot["id"])


def test_legacy_snapshot_delivery_is_unchanged(tmp_path):
    from test_input_delivery import _delivery

    state, _, _, worker, _, lease = _delivery(tmp_path)
    status, body = InputDeliveryStore(state).deliver_inputs(
        worker,
        lease["id"],
        "inputs",
        canonical_digest({"expected_epoch": lease["epoch"]}),
        lease["epoch"],
    )
    assert status == 201 and "delivery" not in body["snapshot"] and body["snapshot"]["files"]
    with pytest.raises(NotFound):
        InputDeliveryStore(state).read_input_chunk(worker, lease["id"], 0, lease["epoch"])


def test_staging_above_threshold_yields_chunked_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset_staging, "CHUNKED_STAGING_BYTES", 5)
    state, owner, _w, manifest, _f, _s, _c = _released_manifest(tmp_path)
    store = DatasetStagingStore(state)
    body = _staging_body(state, manifest)
    status, snap = _stage(store, owner, manifest, body)
    assert status == 201 and snap["chunked"] and snap["staging"]["snapshot_id"] == snap["id"]
    assert _stage(store, owner, manifest, body) == (status, snap)
    assert store.get_snapshot(owner, snap["id"])["digest"] == body["expected_snapshot_digest"]
    assert store.verify_snapshot(owner, snap["id"])["integrity"] == "verified"
    with _connect(state / "tessera.sqlite3") as db:
        types = [
            r[0]
            for r in db.execute(
                "SELECT event_type FROM events WHERE json_extract(payload_json,'$.snapshot_id')=?",
                (snap["id"],),
            )
        ]
        assert sorted(types) == ["snapshot.staged_from_artifacts", "workspace.snapshot_adopted"]
        assert (
            db.execute(
                "SELECT count(*) FROM snapshot_files WHERE snapshot_id=?", (snap["id"],)
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM snapshot_chunks").fetchone()[0] >= 2


def test_migration_v59_to_v60_and_fresh_init_have_the_tables(tmp_path):
    names = ("snapshot_chunk_uploads", "snapshot_chunks", "provider_input_tickets")
    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        for name in names:
            assert db.execute("SELECT count(*) FROM " + name).fetchone()[0] == 0
        db.execute("DROP INDEX provider_input_tickets_by_operation")
        for name in names:
            db.execute("DROP TABLE " + name)
        db.execute("UPDATE schema_meta SET schema_version=59")
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        for name in names:
            assert db.execute("SELECT count(*) FROM " + name).fetchone()[0] == 0


def _expire(state, upload_id):
    with _connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE snapshot_chunk_uploads SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (upload_id,),
        )


def test_expired_upload_refused_and_garbage_collected(tmp_path):
    state, owner, project = _state(tmp_path)
    store = ChunkedSnapshotStore(state)
    pid = project["id"]
    done, _ = _upload(store, owner, pid, FILES, "keep")
    _, stale = _begin(store, owner, pid, {"x.bin": bytes(range(9))}, "stale")
    store.put_chunk(owner, pid, stale["upload_id"], 0, _chunk_body(bytes(range(8))))
    _, fresh = _begin(store, owner, pid, {"y.bin": b"abcdefgh"}, "fresh")
    with _connect(state / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT expires_at > created_at FROM snapshot_chunk_uploads WHERE id=?",
            (fresh["upload_id"],),
        ).fetchone()[0]
    _expire(state, stale["upload_id"])
    with pytest.raises(Conflict, match="expired"):
        store.put_chunk(owner, pid, stale["upload_id"], 1, _chunk_body(b"\x08"))
    with pytest.raises(Conflict, match="expired"):
        store.complete_upload(owner, pid, stale["upload_id"], "late")
    agent = _agent(state, owner, pid)
    with pytest.raises(Forbidden):
        store.collect_garbage(agent, pid, "gc-agent")
    status, report = store.collect_garbage(owner, pid, "gc")
    assert status == 200 and report == {"expired_uploads_deleted": 1, "chunks_deleted": 1}
    assert store.collect_garbage(owner, pid, "gc") == (200, report)
    with _connect(state / "tessera.sqlite3") as db:
        ids = {r[0] for r in db.execute("SELECT id FROM snapshot_chunk_uploads")}
        assert ids == {done["id"], fresh["upload_id"]}
        assert (
            db.execute(
                "SELECT count(*) FROM snapshot_chunks WHERE owner_id=?", (done["id"],)
            ).fetchone()[0]
            == 4
        )
        assert (
            db.execute(
                "SELECT count(*) FROM snapshot_chunks WHERE owner_id=?", (stale["upload_id"],)
            ).fetchone()[0]
            == 0
        )
    # Opportunistic GC inside begin, and the completed snapshot is never collected.
    _expire(state, fresh["upload_id"])
    _begin(store, owner, pid, {"z.bin": b"abcdefgh"}, "next")
    with _connect(state / "tessera.sqlite3") as db:
        ids = {r[0] for r in db.execute("SELECT id FROM snapshot_chunk_uploads")}
        assert fresh["upload_id"] not in ids and done["id"] in ids
    client = TestClient(create_app(state))
    human = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    path = f"/projects/{pid}/chunked-snapshots/gc"
    assert client.post(path, headers=human | {"Idempotency-Key": "g"}).status_code == 200
    assert client.post(path, headers={"Idempotency-Key": "g"}).status_code == 401


def test_per_project_cap_counts_completed_and_unexpired_uploads(tmp_path, monkeypatch):
    state, owner, project = _state(tmp_path)
    store = ChunkedSnapshotStore(state)
    pid = project["id"]
    monkeypatch.setattr(snapshot_chunks, "MAX_PROJECT_CHUNK_BYTES", 40)
    _upload(store, owner, pid, FILES, "first")  # 28 bytes completed
    with pytest.raises(Conflict, match="cap"):
        _begin(store, owner, pid, {"b.bin": bytes(13)}, "too-big")
    _, ok = _begin(store, owner, pid, {"b.bin": bytes(12)}, "fits")  # 28 + 12 == cap
    with pytest.raises(Conflict, match="cap"):
        _begin(store, owner, pid, {"c.bin": b"x"}, "over")
    _expire(state, ok["upload_id"])  # expired uploads stop counting (GC'd on begin)
    assert _begin(store, owner, pid, {"c.bin": b"x"}, "after-gc")[0] == 201


def test_staging_enforces_the_per_project_chunk_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset_staging, "CHUNKED_STAGING_BYTES", 5)
    state, owner, _w, manifest, _f, _s, _c = _released_manifest(tmp_path)
    store = DatasetStagingStore(state)
    body = _staging_body(state, manifest)
    monkeypatch.setattr(snapshot_chunks, "MAX_PROJECT_CHUNK_BYTES", 5)
    with pytest.raises(Conflict, match="cap"):
        _stage(store, owner, manifest, body)
    with _connect(state / "tessera.sqlite3") as db:
        # Refused in one transaction: no snapshot, chunk or upload row survives.
        assert db.execute("SELECT count(*) FROM snapshot_chunks").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM snapshot_chunk_uploads").fetchone()[0] == 0
    monkeypatch.setattr(snapshot_chunks, "MAX_PROJECT_CHUNK_BYTES", 8 * 1024**3)
    assert _stage(store, owner, manifest, body)[0] == 201
