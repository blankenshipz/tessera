"""Chunked upload of large artifacts into quarantine (control side, tiny thresholds)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_artifact_manifests import _body, _operation
from test_artifacts import _payload, _two_path_delivery

from tessera_control import artifact_limits as limits_module
from tessera_control import snapshot_chunks
from tessera_control.artifact_dispositions import ArtifactDispositionStore
from tessera_control.artifact_limits import ArtifactLimitError, ArtifactLimits, artifact_limits
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.artifacts import ArtifactStore
from tessera_control.large_artifacts import LargeArtifactStore, is_large_artifact
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Conflict, Forbidden, NotFound, _connect, canonical_digest

CHUNK = 16
DATA = bytes(range(256)) * 2  # 512 bytes: 32 chunks of 16


@pytest.fixture(autouse=True)
def _fresh_operator_limits():
    # Declared first, so it is torn down after monkeypatch restores the environment: the
    # cache is then recomputed from the real environment, never from a patched one.
    limits_module.operator_limits.cache_clear()
    yield
    limits_module.operator_limits.cache_clear()


@pytest.fixture(autouse=True)
def tiny_thresholds(monkeypatch):
    # "Large" starts above 100 bytes and chunks are 16 bytes, so the real path runs on tiny data.
    monkeypatch.setattr(snapshot_chunks, "CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(limits_module, "LEGACY_INLINE_BYTES", 100)
    yield


def begin_body(lease, data=DATA, path="first.txt", **over):
    return {
        "expected_epoch": lease["epoch"],
        "operation_id": _operation(lease),
        "relative_path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    } | over


def put_all(store, worker, lease_id, upload_id, data=DATA, start=0, stop=None):
    chunks = [data[i : i + CHUNK] for i in range(0, len(data), CHUNK)]
    for seq, part in list(enumerate(chunks))[start:stop]:
        body = {
            "content_base64": base64.b64encode(part).decode(),
            "sha256": hashlib.sha256(part).hexdigest(),
        }
        store.put_chunk(worker, lease_id, upload_id, seq, body)


def start(tmp_path: Path, **over):
    state, worker, lease = _two_path_delivery(tmp_path, 4096)
    store = LargeArtifactStore(state)
    body = begin_body(lease, **over)
    status, upload = store.begin_upload(worker, lease["id"], "begin", canonical_digest(body), body)
    return state, worker, lease, store, status, upload


def test_upload_streams_chunks_verifies_whole_sha_and_quarantines(tmp_path):
    state, worker, lease, store, status, upload = start(tmp_path)
    assert status == 201
    assert (upload["chunk_count"], upload["chunk_bytes"], upload["next_seq"]) == (32, CHUNK, 0)
    put_all(store, worker, lease["id"], upload["id"], stop=10)
    _, resumed = store.upload_status(
        worker, lease["id"], upload["id"], "status", canonical_digest({})
    )
    assert (resumed["received_chunks"], resumed["next_seq"]) == (10, 10)
    put_all(store, worker, lease["id"], upload["id"], start=5)  # replays are idempotent
    status, artifact = store.complete_upload(
        worker, lease["id"], upload["id"], "complete", canonical_digest({})
    )
    assert status == 201
    assert artifact["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert artifact["size"] == len(DATA)
    assert (artifact["status"], artifact["classification"]) == ("quarantined", "unclassified")
    same_key = store.complete_upload(
        worker, lease["id"], upload["id"], "complete", canonical_digest({})
    )
    assert same_key == (201, artifact)
    assert store.complete_upload(
        worker, lease["id"], upload["id"], "new-key", canonical_digest({})
    ) == (200, artifact)
    with _connect(state / "tessera.sqlite3") as connection:
        row = connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact["id"],)).fetchone()
        assert is_large_artifact(row) and row["content"] == b""
        stored = b"".join(
            r[0]
            for r in connection.execute(
                "SELECT content FROM snapshot_chunks WHERE owner_id=? ORDER BY seq",
                (artifact["id"],),
            )
        )
        assert stored == DATA


def test_begin_is_idempotent_and_immutable(tmp_path):
    _, worker, lease, store, _, upload = start(tmp_path)
    body = begin_body(lease)
    assert store.begin_upload(worker, lease["id"], "again", canonical_digest(body), body)[0] == 200
    other = begin_body(lease, DATA[:-1])
    with pytest.raises(Conflict):
        store.begin_upload(worker, lease["id"], "other", canonical_digest(other), other)


@pytest.mark.parametrize("fault", ["missing", "wrong_sha", "bad_chunk", "corrupt_stored"])
def test_complete_refuses_incomplete_or_mismatched_bytes(tmp_path, fault):
    state, worker, lease, store, _, upload = start(tmp_path)
    data = DATA
    if fault == "wrong_sha":
        # Chunks hash fine individually, but the whole-artifact sha256 differs.
        body = begin_body(lease, path="second.txt", sha256="0" * 64)
        _, upload = store.begin_upload(worker, lease["id"], "b2", canonical_digest(body), body)
    if fault == "bad_chunk":
        part = DATA[:CHUNK]
        with pytest.raises(Conflict):
            store.put_chunk(
                worker,
                lease["id"],
                upload["id"],
                0,
                {"content_base64": base64.b64encode(part).decode(), "sha256": "1" * 64},
            )
        return
    put_all(store, worker, lease["id"], upload["id"], data, stop=31 if fault == "missing" else None)
    if fault == "corrupt_stored":
        with _connect(state / "tessera.sqlite3") as connection:
            connection.execute(
                "UPDATE snapshot_chunks SET content=? WHERE owner_id=? AND seq=3",
                (b"x" * CHUNK, upload["id"]),
            )
    with pytest.raises(Conflict):
        store.complete_upload(worker, lease["id"], upload["id"], "c", canonical_digest({}))
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_declaration_is_fenced_by_plan_path_and_operator_limit(tmp_path, monkeypatch):
    state, worker, lease = _two_path_delivery(tmp_path, 4096)
    store = LargeArtifactStore(state)
    undeclared = begin_body(lease, path="not-declared.txt")
    with pytest.raises(Conflict):
        store.begin_upload(worker, lease["id"], "u", canonical_digest(undeclared), undeclared)
    too_big = begin_body(lease, data=b"x" * 5000)
    with pytest.raises(Conflict):
        store.begin_upload(worker, lease["id"], "t", canonical_digest(too_big), too_big)
    # An operator limit lower than the plan's own bound wins.
    monkeypatch.setenv(limits_module.ENV_MAX_ARTIFACT, "100")
    monkeypatch.setenv(limits_module.ENV_MAX_PROJECT, "1000")
    limits_module.operator_limits.cache_clear()
    over = begin_body(lease)
    with pytest.raises(Conflict):
        store.begin_upload(worker, lease["id"], "o", canonical_digest(over), over)


def test_other_worker_and_human_snapshot_routes_cannot_touch_the_upload(tmp_path):
    state, worker, lease, store, _, upload = start(tmp_path)
    stranger = dict(worker, id="principal_someone_else")
    part = DATA[:CHUNK]
    chunk = {
        "content_base64": base64.b64encode(part).decode(),
        "sha256": hashlib.sha256(part).hexdigest(),
    }
    with pytest.raises((NotFound, Exception)):
        store.put_chunk(stranger, lease["id"], upload["id"], 0, chunk)
    owner = SnapshotStore(state).authenticate(
        (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    )
    with _connect(state / "tessera.sqlite3") as connection:
        project_id = connection.execute(
            "SELECT project_id FROM snapshot_chunk_uploads WHERE id=?", (upload["id"],)
        ).fetchone()[0]
    human = snapshot_chunks.ChunkedSnapshotStore(state)
    with pytest.raises(NotFound):
        human.put_chunk(owner, project_id, upload["id"], 0, chunk)


def test_manifest_accepts_large_and_small_artifacts_together(tmp_path):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"])
    _, large = store.complete_upload(
        worker, lease["id"], upload["id"], "complete", canonical_digest({})
    )
    small = _payload(lease, b"small")
    small["relative_path"] = "second.txt"
    _, small_receipt = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "small", canonical_digest(small), small
    )
    body = _body(lease, large, small_receipt)
    status, manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(body), body
    )
    assert status == 201
    assert {a["relative_path"]: a["size"] for a in manifest["artifacts"]} == {
        "first.txt": len(DATA),
        "second.txt": 5,
    }
    assert json.dumps(manifest).count(base64.b64encode(DATA[:8]).decode()) == 0


def test_limits_defaults_and_validation(monkeypatch):
    monkeypatch.setattr(limits_module, "LEGACY_INLINE_BYTES", 16 * 1024 * 1024)
    defaults = artifact_limits({})
    assert defaults.max_artifact_bytes == 8 * 1024**3
    assert defaults.max_project_bytes == 32 * 1024**3
    assert artifact_limits({"TESSERA_MAX_ARTIFACT_BYTES": str(2 * 1024**3)}).max_artifact_bytes == (
        2 * 1024**3
    )
    for env in (
        {"TESSERA_MAX_ARTIFACT_BYTES": "lots"},
        {"TESSERA_MAX_ARTIFACT_BYTES": "1024"},
        {"TESSERA_MAX_ARTIFACT_BYTES": str(2 * 1024**4)},
        {"TESSERA_MAX_ARTIFACT_BYTES": str(4 * 1024**3), "TESSERA_MAX_PROJECT_ARTIFACT_BYTES": "1"},
    ):
        with pytest.raises(ArtifactLimitError):
            artifact_limits(env)
    with pytest.raises(ArtifactLimitError):
        ArtifactLimits(True, 10 * 1024**3)


def test_small_files_must_use_the_inline_path(tmp_path):
    state, worker, lease = _two_path_delivery(tmp_path, 4096)
    store = LargeArtifactStore(state)
    small = begin_body(lease, b"x" * 100)  # at the inline bound, not above it
    with pytest.raises(Conflict, match="inline"):
        store.begin_upload(worker, lease["id"], "s", canonical_digest(small), small)
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM snapshot_chunk_uploads").fetchone()[0] == 0


def test_an_expired_upload_is_restartable_and_releases_its_budget(tmp_path):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"], stop=3)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE snapshot_chunk_uploads SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (upload["id"],),
        )
        assert store._reserved_bytes(connection, lease["id"]) == 0  # expired: no reservation
    body = begin_body(lease)
    status, fresh = store.begin_upload(worker, lease["id"], "again", canonical_digest(body), body)
    assert (status, fresh["status"], fresh["received_chunks"], fresh["next_seq"]) == (
        201,
        "uploading",
        0,
        0,
    )
    assert fresh["expires_at"] > "2001"
    put_all(store, worker, lease["id"], upload["id"])
    assert (
        store.complete_upload(worker, lease["id"], upload["id"], "done", canonical_digest({}))[0]
        == 201
    )


def test_a_refenced_lease_discards_bytes_sent_under_the_old_epoch(tmp_path):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"], stop=4)
    with _connect(state / "tessera.sqlite3") as connection:
        manifest = json.loads(
            connection.execute(
                "SELECT manifest_json FROM snapshot_chunk_uploads WHERE id=?", (upload["id"],)
            ).fetchone()[0]
        )
        manifest["epoch"] = lease["epoch"] + 1  # as if begun under another epoch
        connection.execute(
            "UPDATE snapshot_chunk_uploads SET manifest_json=? WHERE id=?",
            (json.dumps(manifest), upload["id"]),
        )
    body = begin_body(lease)
    status, fresh = store.begin_upload(worker, lease["id"], "epoch", canonical_digest(body), body)
    assert (status, fresh["epoch"], fresh["received_chunks"]) == (201, lease["epoch"], 0)


def test_chunks_may_arrive_out_of_order_and_resume_from_the_first_gap(tmp_path):
    _, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"], start=5, stop=8)
    _, status = store.upload_status(worker, lease["id"], upload["id"], "s", canonical_digest({}))
    assert (status["received_chunks"], status["next_seq"]) == (3, 0)
    put_all(store, worker, lease["id"], upload["id"], stop=5)
    _, status = store.upload_status(worker, lease["id"], upload["id"], "s2", canonical_digest({}))
    assert (status["received_chunks"], status["next_seq"]) == (8, 8)
    put_all(store, worker, lease["id"], upload["id"])
    assert (
        store.complete_upload(worker, lease["id"], upload["id"], "c", canonical_digest({}))[0]
        == 201
    )


def test_rejecting_a_manifest_reclaims_large_chunks_and_blocks_a_later_retain(tmp_path):
    state, worker, lease, store, _, upload = start(tmp_path)
    put_all(store, worker, lease["id"], upload["id"])
    _, large = store.complete_upload(
        worker, lease["id"], upload["id"], "complete", canonical_digest({})
    )
    small = _payload(lease, b"small")
    small["relative_path"] = "second.txt"
    _, small_receipt = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "small", canonical_digest(small), small
    )
    body = _body(lease, large, small_receipt)
    _, manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(body), body
    )
    owner = SnapshotStore(state).authenticate(
        (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    )
    dispositions = ArtifactDispositionStore(state)

    def decide(key, revision, disposition):
        request = {
            "expected_manifest_digest": manifest["manifest_digest"],
            "expected_revision": revision,
            "disposition": disposition,
            "rationale": "review",
        }
        return dispositions.record_disposition(
            owner, manifest["id"], key, canonical_digest(request), request
        )

    assert decide("reject", 0, "reject")[0] == 201
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM snapshot_chunks").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM snapshot_chunk_uploads").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 2  # rows stay
    with pytest.raises(Conflict, match="reclaimed"):
        decide("retain", 1, "retain_quarantined")


def test_the_operator_limit_applies_to_new_plans_not_to_stored_specs():
    from tessera_control.models import ExecutionSpec, within_operator_limit

    spec = ExecutionSpec.model_validate(
        {
            "argv": ["python", "x.py"],
            "input_snapshot_digest": "a" * 64,
            "pinned_skill_digests": [],
            "output_paths": ["out.bin"],
            "profile": "local-cpu",
            "max_output_bytes": 9 * 1024**3,  # above the 8 GiB default
        }
    )
    with pytest.raises(ValueError):
        within_operator_limit(spec)  # plan intake refuses it...
    assert spec.max_output_bytes == 9 * 1024**3  # ...but a stored spec still validates


def _completed(tmp_path, path="first.txt", data=DATA):
    state, worker, lease = _two_path_delivery(tmp_path, 4096)
    store = LargeArtifactStore(state)
    body = begin_body(lease, data, path)
    _, upload = store.begin_upload(worker, lease["id"], "b-" + path, canonical_digest(body), body)
    put_all(store, worker, lease["id"], upload["id"], data)
    _, artifact = store.complete_upload(
        worker, lease["id"], upload["id"], "c-" + path, canonical_digest({})
    )
    return state, worker, lease, store, artifact


def _counts(state, owner):
    with _connect(state / "tessera.sqlite3") as connection:
        return (
            connection.execute(
                "SELECT count(*) FROM snapshot_chunks WHERE owner_id=?", (owner,)
            ).fetchone()[0],
            connection.execute(
                "SELECT count(*) FROM snapshot_chunk_uploads WHERE id=?", (owner,)
            ).fetchone()[0],
        )


def _age(state, artifact_id, created_at="2000-01-01T00:00:00Z"):
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE artifacts SET created_at=? WHERE id=?", (created_at, artifact_id)
        )


def _events(state):
    with _connect(state / "tessera.sqlite3") as connection:
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='control.large_artifact_reclaimed'"
            )
        ]


def _expire_lease(state, lease_id):
    """The execution is over: lease past its hard expiry and its grant reconciled."""
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_leases SET hard_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (lease_id,),
        )
        connection.execute(
            "UPDATE execution_grants SET status='reconciled', expires_at='2000-01-01T00:00:00Z' "
            "WHERE id=(SELECT grant_id FROM execution_leases WHERE id=?)",
            (lease_id,),
        )


def _manifest_for(state, worker, lease, store, artifact):
    small = _payload(lease, b"small")
    small["relative_path"] = "second.txt"
    _, receipt = ArtifactStore(state).upload_artifact(
        worker, lease["id"], "small", canonical_digest(small), small
    )
    body = _body(lease, artifact, receipt)
    return ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(body), body
    )[1]


def test_an_unmanifested_artifact_of_a_live_lease_is_never_swept(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    _age(state, artifact["id"])  # old, but its lease is still running (it can post a manifest)
    assert store.reclaim_orphans(worker, artifact["project_id"]) == []
    assert _counts(state, artifact["id"]) == (32, 1)
    assert _events(state) == []
    # The same bytes are still usable: the running worker's manifest commits and verifies.
    _manifest_for(state, worker, lease, store, artifact)


def test_an_unmanifested_artifact_is_reclaimed_once_its_lease_is_over_and_it_is_old(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    assert store.reclaim_orphans(worker, artifact["project_id"]) == []  # young: kept
    _age(state, artifact["id"])
    other = begin_body(lease, DATA, "second.txt")  # a begin runs the bounded sweep
    store.begin_upload(worker, lease["id"], "b2", canonical_digest(other), other)
    assert _counts(state, artifact["id"]) == (0, 0)
    (event,) = _events(state)
    assert (event["artifact_id"], event["reason"], event["trigger"]) == (
        artifact["id"],
        "unmanifested",
        "worker_begin",
    )
    with _connect(state / "tessera.sqlite3") as connection:  # the quarantine row is evidence
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1


def test_a_manifest_committed_between_scan_and_delete_wins_the_race(tmp_path, monkeypatch):
    from tessera_control import large_artifacts

    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    real = large_artifacts.reclaimable

    def scan_then_commit_a_manifest(*args, **kwargs):
        found = real(*args, **kwargs)
        assert [item["reason"] for item in found] == ["unmanifested"]  # the scan saw an orphan
        _manifest_for(state, worker, lease, store, artifact)  # ...then a manifest commits
        return found

    monkeypatch.setattr(large_artifacts, "reclaimable", scan_then_commit_a_manifest)
    assert store.reclaim_orphans(worker, artifact["project_id"]) == []  # re-checked under lock
    assert _counts(state, artifact["id"]) == (32, 1)
    assert _events(state) == []


def _owner(state):
    return SnapshotStore(state).authenticate(
        (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    )


def _decide(state, manifest, key, revision, disposition):
    request = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": revision,
        "disposition": disposition,
        "rationale": "review",
    }
    return ArtifactDispositionStore(state).record_disposition(
        _owner(state), manifest["id"], key, canonical_digest(request), request
    )


def _commit_reject_without_reclaim(state, manifest):
    """A committed reject whose reclaim never ran (the process stopped right after)."""
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO artifact_dispositions (id, manifest_id, manifest_digest, revision,
            disposition, rationale, actor_principal_id, created_at)
            SELECT 'd1', ?, ?, 1, 'reject', 'review', id, '2026-01-01T00:00:00Z'
            FROM principals LIMIT 1""",
            (manifest["id"], manifest["manifest_digest"]),
        )


def test_a_decision_committed_between_scan_and_delete_also_wins(tmp_path, monkeypatch):
    from tessera_control import large_artifacts

    state, worker, lease, store, artifact = _completed(tmp_path)
    manifest = _manifest_for(state, worker, lease, store, artifact)
    _commit_reject_without_reclaim(state, manifest)
    real = large_artifacts.reclaimable

    def scan_then_retain(*args, **kwargs):
        found = real(*args, **kwargs)
        assert [item["reason"] for item in found] == ["rejected"]  # the scan saw a reject...
        _decide(state, manifest, "retain", 1, "retain_quarantined")  # ...then a human retains
        return found

    monkeypatch.setattr(large_artifacts, "reclaimable", scan_then_retain)
    assert store.reclaim_orphans(worker, artifact["project_id"]) == []
    assert _counts(state, artifact["id"]) == (32, 1)
    assert _events(state) == []


def test_a_rejection_whose_reclaim_never_ran_is_finished_by_the_sweep(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    manifest = _manifest_for(state, worker, lease, store, artifact)
    _commit_reject_without_reclaim(state, manifest)
    assert _counts(state, artifact["id"]) == (32, 1)
    freed = store.reclaim_orphans(worker, artifact["project_id"])
    assert [(item["id"], item["reason"]) for item in freed] == [(artifact["id"], "rejected")]
    assert _counts(state, artifact["id"]) == (0, 0)


def test_a_manifested_artifact_awaiting_a_decision_is_never_swept(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    _manifest_for(state, worker, lease, store, artifact)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    assert store.reclaim_orphans(worker, artifact["project_id"]) == []
    assert _counts(state, artifact["id"]) == (32, 1)


def test_a_replayed_reject_records_one_event(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    manifest = _manifest_for(state, worker, lease, store, artifact)
    for _ in range(3):  # the original request and two idempotent replays
        assert _decide(state, manifest, "reject-key", 0, "reject")[1]["disposition"] == "reject"
    assert len(_events(state)) == 1
    assert store.reclaim_orphans(worker, artifact["project_id"]) == []  # nothing left to record
    assert len(_events(state)) == 1


def _scan(store, worker, state, artifact):
    from tessera_control.large_artifacts import reclaimable

    with _connect(state / "tessera.sqlite3") as connection:
        scanned = reclaimable(connection, artifact["project_id"], datetime.now(UTC), 3600)
        live = store._live_principal(connection, worker, audience="worker")
    assert [item["reason"] for item in scanned] == ["unmanifested"]
    return live, scanned[0]


def _reclaim(store, live, candidate, **kwargs):
    from tessera_control.large_artifacts import reclaim_artifact_bytes

    return reclaim_artifact_bytes(
        store,
        live,
        candidate,
        "unmanifested",
        trigger="worker_begin",
        orphan_seconds=3600,
        **kwargs,
    )


def test_two_sweeps_interleaved_across_batches_record_exactly_one_event(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    live, candidate = _scan(store, worker, state, artifact)
    results = {}

    def second_sweep_runs_to_completion():
        # Sweep A has committed one batch; sweep B (scanned at the same moment) now runs whole.
        if "b" not in results:
            results["b"] = _reclaim(store, live, candidate, batch=1)

    results["a"] = _reclaim(
        store, live, candidate, batch=1, after_batch=second_sweep_runs_to_completion
    )
    assert results == {"b": True, "a": False}  # one of the two claimed it, the other did not
    assert len(_events(state)) == 1
    assert _counts(state, artifact["id"]) == (0, 0)


def test_the_event_goes_to_the_call_that_deletes_the_upload_record_in_either_order(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    live, candidate = _scan(store, worker, state, artifact)
    order = [_reclaim(store, live, candidate, batch=7) for _ in range(3)]
    assert order == [True, False, False]
    assert len(_events(state)) == 1


def test_the_sweep_is_bounded_skipped_on_replay_and_never_blocks_a_begin(tmp_path, monkeypatch):
    from tessera_control import large_artifacts

    state, worker, lease, store, artifact = _completed(tmp_path)
    calls = []
    real = large_artifacts.reclaimable

    def counting(*args, **kwargs):
        calls.append(kwargs.get("limit", args[4] if len(args) > 4 else None))
        return real(*args, **kwargs)

    monkeypatch.setattr(large_artifacts, "reclaimable", counting)
    body = begin_body(lease, DATA, "second.txt")
    store.begin_upload(worker, lease["id"], "b-new", canonical_digest(body), body)
    store.begin_upload(worker, lease["id"], "b-new", canonical_digest(body), body)  # replay
    assert calls == [large_artifacts.MAX_SWEEP_PER_BEGIN]  # bounded, and not run on the replay

    def broken(*args, **kwargs):
        raise ValueError("synthetic housekeeping failure")

    monkeypatch.setattr(large_artifacts, "reclaimable", broken)
    other = begin_body(lease, DATA[:-1], "second.txt")
    with pytest.raises(Conflict):  # the begin's own refusal (path already begun), not the sweep's
        store.begin_upload(worker, lease["id"], "b-other", canonical_digest(other), other)
    assert _counts(state, artifact["id"]) == (32, 1)


def _hold(state, value="held_restore"):
    (state / ".restore-complete").write_text("published")  # a published restore, as on disk
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE control_recovery SET state=? WHERE singleton=1", (value,))


def test_nothing_is_reclaimed_while_control_is_held_after_a_restore(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    _hold(state)
    with pytest.raises(Forbidden, match="held"):
        store.reclaim_for_human(_owner(state), artifact["project_id"], "k", canonical_digest({}))
    assert _counts(state, artifact["id"]) == (32, 1)  # bytes and upload record untouched
    assert _events(state) == []
    # The begin-time sweep is a no-op under a hold (the begin itself is refused separately).
    body = begin_body(lease, DATA, "second.txt")
    with pytest.raises(Forbidden):
        store.begin_upload(worker, lease["id"], "b", canonical_digest(body), body)
    assert _counts(state, artifact["id"]) == (32, 1)
    _hold(state, "active")
    assert [i["reason"] for i in store.reclaim_orphans(worker, artifact["project_id"])] == [
        "unmanifested"
    ]


def test_a_hold_that_starts_mid_reclaim_stops_it_at_the_next_batch(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    live, candidate = _scan(store, worker, state, artifact)
    with pytest.raises(Forbidden):
        _reclaim(store, live, candidate, batch=4, after_batch=lambda: _hold(state))
    assert _counts(state, artifact["id"])[0] == 28  # exactly one batch of 4 went
    assert _events(state) == []
    _hold(state, "active")
    assert _reclaim(store, live, candidate, batch=4) is True  # resumed and recorded once
    assert len(_events(state)) == 1


def test_a_human_replay_returns_the_first_answer_without_sweeping_again(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    owner = _owner(state)
    project = artifact["project_id"]
    first = store.reclaim_for_human(owner, project, "k1", canonical_digest({}))
    assert first == (200, {"reclaimed": []})  # nothing eligible yet (the lease is live)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])  # now eligible
    again = store.reclaim_for_human(owner, project, "k1", canonical_digest({}))
    assert again == first  # the first answer...
    assert _counts(state, artifact["id"]) == (32, 1)  # ...and no sweep behind its back
    assert _events(state) == []
    fresh = store.reclaim_for_human(owner, project, "k2", canonical_digest({}))
    assert [i["reason"] for i in fresh[1]["reclaimed"]] == ["unmanifested"]
    assert store.reclaim_for_human(owner, project, "k2", canonical_digest({})) == fresh
    assert len(_events(state)) == 1


def test_concurrent_human_calls_with_one_key_reclaim_once_and_agree(tmp_path):
    import threading

    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    owner, project = _owner(state), artifact["project_id"]
    answers, gate = [], threading.Barrier(2)

    def call():
        gate.wait()
        answers.append(store.reclaim_for_human(owner, project, "same", canonical_digest({})))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(answers) == 2 and answers[0] == answers[1]
    assert [i["reason"] for i in answers[0][1]["reclaimed"]] == ["unmanifested"]
    assert len(_events(state)) == 1


def test_the_human_route_works_over_http_and_requires_a_key_and_a_human(tmp_path):
    from fastapi.testclient import TestClient

    from tessera_control.api import create_app

    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    with pytest.raises(Forbidden):
        store.reclaim_orphans(worker, artifact["project_id"], audience="control")
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    url = f"/projects/{artifact['project_id']}/large-artifacts/reclaim"
    with TestClient(create_app(state)) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post(url, json={}, headers=headers).status_code in {400, 422}  # no key
        first = client.post(url, json={}, headers=headers | {"Idempotency-Key": "k1"})
        assert first.status_code == 200
        assert [item["reason"] for item in first.json()["reclaimed"]] == ["unmanifested"]
        assert client.post(url, json={}, headers=headers | {"Idempotency-Key": "k1"}).json() == (
            first.json()
        )
        fresh = client.post(url, json={}, headers=headers | {"Idempotency-Key": "k2"})
        assert fresh.json() == {"reclaimed": []}
    assert len(_events(state)) == 1
    assert _events(state)[0]["trigger"] == "human"


def test_the_begin_scan_is_bounded_oldest_first(tmp_path):
    from tessera_control.large_artifacts import reclaimable

    state, worker, lease, store, first = _completed(tmp_path)
    body = begin_body(lease, DATA, "second.txt")
    _, upload = store.begin_upload(worker, lease["id"], "b2", canonical_digest(body), body)
    put_all(store, worker, lease["id"], upload["id"])
    _, second = store.complete_upload(worker, lease["id"], upload["id"], "c2", canonical_digest({}))
    _expire_lease(state, lease["id"])
    _age(state, first["id"], "2000-01-01T00:00:00Z")
    _age(state, second["id"], "2001-01-01T00:00:00Z")
    with _connect(state / "tessera.sqlite3") as connection:
        now = datetime.now(UTC)
        both = reclaimable(connection, first["project_id"], now, 3600)
        assert [item["id"] for item in both] == [first["id"], second["id"]]  # oldest first
        only_oldest = reclaimable(connection, first["project_id"], now, 3600, None, 1)
        assert [item["id"] for item in only_oldest] == [first["id"]]  # examined at most 1


def test_the_orphan_age_is_operator_configurable_and_validated():
    assert artifact_limits({}).orphan_artifact_seconds == 7 * 24 * 3600
    assert artifact_limits(
        {"TESSERA_ORPHAN_ARTIFACT_SECONDS": "86400"}
    ).orphan_artifact_seconds == (86400)
    for bad in ("soon", "60", str(400 * 24 * 3600)):
        with pytest.raises(ArtifactLimitError):
            artifact_limits({"TESSERA_ORPHAN_ARTIFACT_SECONDS": bad})


def _clone_orphan(state, artifact, new_id, created_at):
    """A second large artifact (same bytes, same finished lease) that no manifest lists."""
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO artifacts SELECT ?, project_id, task_id, grant_id, lease_id,
            worker_principal_id, epoch, operation_id, ?, sha256, size, content, ?, status,
            classification, late FROM artifacts WHERE id=?""",
            (new_id, f"{new_id}.bin", created_at, artifact["id"]),
        )
        connection.execute(
            """INSERT INTO snapshot_chunks SELECT ?, seq, relative_path, offset, size, sha256,
            content FROM snapshot_chunks WHERE owner_id=?""",
            (new_id, artifact["id"]),
        )
        connection.execute(
            """INSERT INTO snapshot_chunk_uploads SELECT ?, project_id, expected_digest,
            manifest_json, status, actor_principal_id, created_at, completed_at, expires_at
            FROM snapshot_chunk_uploads WHERE id=?""",
            (new_id, artifact["id"]),
        )


def test_artifacts_that_cannot_be_eligible_never_starve_a_bounded_scan(tmp_path):
    from tessera_control.large_artifacts import reclaimable

    state, worker, lease, store, retained = _completed(tmp_path)
    _manifest_for(state, worker, lease, store, retained)  # old, listed, awaiting a decision
    _expire_lease(state, lease["id"])
    _age(state, retained["id"], "2000-01-01T00:00:00Z")
    _clone_orphan(state, retained, "artifact_orphan_one", "2001-01-01T00:00:00Z")
    with _connect(state / "tessera.sqlite3") as connection:
        now = datetime.now(UTC)
        # Oldest first, the retained artifact would fill a one-candidate window and hide the
        # orphan behind it; the prefilter leaves only plausible candidates in the window.
        found = reclaimable(connection, retained["project_id"], now, 3600, None, 1)
        assert [(f["id"], f["reason"]) for f in found] == [("artifact_orphan_one", "unmanifested")]
    freed = store.reclaim_orphans(worker, retained["project_id"], scan=1)
    assert [item["id"] for item in freed] == ["artifact_orphan_one"]
    assert _counts(state, retained["id"]) == (32, 1)  # the retained artifact is untouched


def test_a_live_lease_artifact_does_not_starve_the_scan_either(tmp_path):
    from tessera_control.large_artifacts import reclaimable

    state, worker, lease, store, live = _completed(tmp_path)
    _age(state, live["id"], "2000-01-01T00:00:00Z")  # old and unmanifested, but its lease is live
    with _connect(state / "tessera.sqlite3") as connection:
        assert reclaimable(connection, live["project_id"], datetime.now(UTC), 3600, None, 1) == []


def test_a_hold_that_starts_after_a_reject_commits_leaves_the_reclaim_to_the_sweep(
    tmp_path, monkeypatch
):
    state, worker, lease, store, artifact = _completed(tmp_path)
    manifest = _manifest_for(state, worker, lease, store, artifact)
    real = ArtifactDispositionStore._reclaim_large

    def hold_then_reclaim(self, actor, manifest_id):
        _hold(state)  # the decision has committed; control goes on hold before the reclaim
        return real(self, actor, manifest_id)

    monkeypatch.setattr(ArtifactDispositionStore, "_reclaim_large", hold_then_reclaim)
    assert _decide(state, manifest, "reject", 0, "reject")[0] == 201  # the decision stands
    assert _counts(state, artifact["id"]) == (32, 1)
    assert _events(state) == []
    _hold(state, "active")
    freed = store.reclaim_orphans(worker, artifact["project_id"])
    assert [(i["id"], i["reason"]) for i in freed] == [(artifact["id"], "rejected")]
    assert len(_events(state)) == 1


def test_the_human_route_refuses_under_a_hold_and_a_changed_request_under_a_used_key(tmp_path):
    from fastapi.testclient import TestClient

    from tessera_control.api import create_app

    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    owner, project = _owner(state), artifact["project_id"]
    store.reclaim_for_human(owner, project, "used", canonical_digest({}))
    with pytest.raises(Conflict):  # the same key with a different request is refused
        store.reclaim_for_human(owner, project, "used", canonical_digest({"changed": True}))
    second = tmp_path / "second"
    second.mkdir(mode=0o700)
    state2, worker2, lease2, store2, artifact2 = _completed(second)
    _expire_lease(state2, lease2["id"])
    _age(state2, artifact2["id"])
    _hold(state2)
    token = (state2 / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    url = f"/projects/{artifact2['project_id']}/large-artifacts/reclaim"
    with TestClient(create_app(state2)) as client:
        denied = client.post(
            url, json={}, headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "h"}
        )
    assert denied.status_code == 403
    assert _counts(state2, artifact2["id"]) == (32, 1)  # nothing was deleted


def test_human_reclaims_with_different_keys_do_not_wait_on_each_other(tmp_path):
    from tessera_control import large_artifacts

    state, worker, lease, store, artifact = _completed(tmp_path)
    owner = _owner(state)
    principal = owner["id"]
    keys = [f"key-{i}" for i in range(200)]
    held = keys[0]
    busy = large_artifacts._stripe(principal, held)
    other = next(k for k in keys[1:] if large_artifacts._stripe(principal, k) is not busy)
    with busy:  # a reclaim is busy under one key...
        done = []

        import threading

        thread = threading.Thread(
            target=lambda: done.append(
                store.reclaim_for_human(owner, artifact["project_id"], other, canonical_digest({}))
            )
        )
        thread.start()
        thread.join(timeout=10)
        assert done == [(200, {"reclaimed": []})]  # ...and an unrelated key is not blocked
