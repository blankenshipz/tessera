"""A stage output above the inline bound flows through chunked custody into a later stage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import artifact_limits, snapshot_chunks, snapshot_composition
from tessera_control.store import Conflict
from tessera_worker import chunked_inputs

PAYLOAD = json.dumps({"weights": list(range(120))})  # a few hundred bytes
CHUNK = 64


def test_large_output_becomes_a_chunked_stage_input(tmp_path, monkeypatch):
    # "Large" and "chunk" are shrunk so the real code path runs on a small file.
    monkeypatch.setattr(artifact_limits, "LEGACY_INLINE_BYTES", 100)
    monkeypatch.setattr(artifact_limits, "CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(snapshot_chunks, "CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(chunked_inputs, "CHUNK_BYTES", CHUNK)
    assert len(PAYLOAD) > 100

    world = World(tmp_path)
    world.default_scenario = (0, {"data.json": PAYLOAD})
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "produce", max_output_bytes=4096),
        stage_spec(
            world.env,
            "consume",
            inputs=[{"kind": "stage", "name": "up", "stage": "produce", "paths": ["data.json"]}],
        ),
    ]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])

    digest = hashlib.sha256(PAYLOAD.encode()).hexdigest()
    (output,) = world.env.query(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='output' "
        "AND stage_key='produce'",
        campaign["id"],
    )
    assert (output["sha256"], output["size_bytes"]) == (digest, len(PAYLOAD))
    assert output["data_class"] == "private_derived"

    # Every output is chunk rows under its artifact; the artifact row holds no bytes.
    artifacts = world.env.query("SELECT id, size, length(content) AS n FROM artifacts")
    assert len(artifacts) == 2
    for artifact in artifacts:
        assert (artifact["n"], artifact["size"]) == (0, len(PAYLOAD))
        chunks = world.env.query(
            "SELECT size FROM snapshot_chunks WHERE owner_id=? ORDER BY seq", artifact["id"]
        )
        assert len(chunks) == -(-len(PAYLOAD) // CHUNK)
        assert max(c["size"] for c in chunks) <= CHUNK

    # The consuming stage's input is a composed, chunked, private_derived snapshot.
    (attempt,) = [a for a in world.attempts(campaign["id"]) if a["stage_key"] == "consume"]
    assert attempt["status"] == "succeeded", dump(world, campaign["id"])
    composed = world.env.query(
        "SELECT id FROM snapshot_chunk_uploads WHERE status='completed' AND id LIKE 'snapshot_%'"
    )
    assert composed
    copied = b"".join(
        row["content"]
        for upload in composed
        for row in world.env.query(
            "SELECT content FROM snapshot_chunks WHERE owner_id=? ORDER BY seq", upload["id"]
        )
        if upload
    )
    assert PAYLOAD.encode() in copied

    # The campaign receipt names the artifact by its whole-file sha256 and size.
    row = world.row(
        "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign["id"]
    )
    receipt = json.loads(row["receipt_json"])
    produced = [
        io
        for stage in receipt["stages"]
        if stage["stage_key"] == "produce"
        for a in stage["attempts"]
        for io in a["io"]
        if io["direction"] == "output"
    ]
    assert [(io["sha256"], io["size_bytes"]) for io in produced] == [(digest, len(PAYLOAD))]
    assert Path(tmp_path).exists()


def _two_stage(world):
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "produce", max_output_bytes=4096),
        stage_spec(
            world.env,
            "consume",
            inputs=[{"kind": "stage", "name": "up", "stage": "produce", "paths": ["data.json"]}],
        ),
    ]
    return world.campaign(spec)


@pytest.fixture
def tiny(monkeypatch):
    monkeypatch.setattr(artifact_limits, "LEGACY_INLINE_BYTES", 100)
    monkeypatch.setattr(artifact_limits, "CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(snapshot_chunks, "CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(chunked_inputs, "CHUNK_BYTES", CHUNK)


def test_composition_copies_chunks_outside_the_write_lock(tmp_path, tiny, monkeypatch):
    world = World(tmp_path)
    world.default_scenario = (0, {"data.json": PAYLOAD})
    probes = []
    real = snapshot_composition.copy_file_chunks_batched

    def probe(database_path, *args, **kwargs):
        # While chunks are being copied, another writer must be able to take the write lock.
        other = sqlite3.connect(database_path, timeout=0.2)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            probes.append("free")
        except sqlite3.OperationalError:
            probes.append("locked")
        finally:
            other.close()
        return real(database_path, *args, **kwargs)

    monkeypatch.setattr(snapshot_composition, "copy_file_chunks_batched", probe)
    campaign = _two_stage(world)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert probes and set(probes) == {"free"}
    # Nothing is left staged: every composed upload completed.
    assert not world.env.query(
        "SELECT id FROM snapshot_chunk_uploads WHERE status='uploading' AND id LIKE 'snapshot_%'"
    )


def test_a_failed_staged_copy_is_removed_at_once(tmp_path, tiny, monkeypatch):
    world = World(tmp_path)
    world.default_scenario = (0, {"data.json": PAYLOAD})
    real = snapshot_composition.copy_file_chunks_batched
    failures = []

    def fail_once(database_path, *args, **kwargs):
        if not failures:
            failures.append(1)
            real(database_path, *args, **kwargs)  # copy everything, then fail the check
            raise Conflict("Released artifact bytes no longer match the manifest.")
        return real(database_path, *args, **kwargs)

    monkeypatch.setattr(snapshot_composition, "copy_file_chunks_batched", fail_once)
    campaign = _two_stage(world)
    for _ in range(40):
        world.runner.tick()
        world.poller.run(max_cycles=1)
        if failures:
            break
    assert failures
    leftovers = world.env.query(
        "SELECT id FROM snapshot_chunk_uploads WHERE status='uploading' AND id LIKE 'snapshot_%'"
    )
    assert not leftovers
    orphan = world.env.query(
        "SELECT DISTINCT owner_id FROM snapshot_chunks WHERE owner_id LIKE 'snapshot_%' "
        "AND owner_id NOT IN (SELECT id FROM snapshot_chunk_uploads)"
    )
    assert not orphan, campaign


def test_owner_automation_approval_composes_a_chunk_stored_stage_output(tmp_path, tiny):
    """The delegated compose step keeps the approval's automation label while staging chunks."""
    world = World(tmp_path)
    world.default_scenario = (0, {"data.json": PAYLOAD})
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "produce", max_output_bytes=4096),
        stage_spec(
            world.env,
            "consume",
            inputs=[{"kind": "stage", "name": "up", "stage": "produce", "paths": ["data.json"]}],
        ),
    ]
    campaign = world.campaign(spec, world.env.owner)
    assert campaign["approval"]["decision_class"] == "owner_automation_labelled"
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert world.env.query(
        "SELECT id FROM snapshot_chunk_uploads WHERE status='completed' AND id LIKE 'snapshot_%'"
    )
    assert not world.env.query(
        "SELECT id FROM snapshot_chunk_uploads WHERE status='uploading' AND id LIKE 'snapshot_%'"
    )
