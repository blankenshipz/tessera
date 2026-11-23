"""The synthetic public snapshot `smoke-credentials issue` makes sure the project holds.

Fixed synthetic bytes, owner-run, declared public through the ordinary adoption path with a
recorded decision class and automation label. No provider, no network, no money."""

from __future__ import annotations

import hashlib
import json
import stat

import pytest
from campaign_fixture import Env

from tessera_control import smoke_credentials, smoke_snapshot
from tessera_control.data_class import snapshot_data_class
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Conflict, Forbidden, _connect

DEST = "smoke.local"


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _snapshots(env, digest):
    return env.query("SELECT id, project_id FROM source_snapshots WHERE digest=?", digest)


def test_digest_is_stable_across_locations_and_runs(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "deeper" / "two"
    first.mkdir()
    second.parent.mkdir()
    second.mkdir()
    a, digest_a = smoke_snapshot.capture_synthetic(first)
    b, digest_b = smoke_snapshot.capture_synthetic(second)
    _c, digest_c = smoke_snapshot.capture_synthetic()
    assert digest_a == digest_b == digest_c
    assert a.inventory.root == smoke_snapshot.LOGICAL_ROOT == b.inventory.root
    assert len(digest_a) == 64


def test_generated_bytes_are_the_checked_in_constants(tmp_path):
    directory = tmp_path / "gen"
    directory.mkdir()
    smoke_snapshot.generate(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    names = sorted(path.name for path in directory.iterdir())
    assert names == sorted(smoke_snapshot.SMOKE_FILES) == list(smoke_snapshot.SMOKE_FILES)
    for name, payload in smoke_snapshot.SMOKE_FILES.items():
        assert (directory / name).read_bytes() == payload
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
        payload.decode("ascii")
        assert b"synthetic" in smoke_snapshot.SMOKE_FILES["README.txt"].lower()
    assert sum(len(p) for p in smoke_snapshot.SMOKE_FILES.values()) < 1024
    capture, _digest = smoke_snapshot.capture_synthetic()
    assert dict(capture.files) == smoke_snapshot.SMOKE_FILES
    for entry in capture.inventory.entries:
        assert (
            entry.sha256
            == hashlib.sha256(smoke_snapshot.SMOKE_FILES[entry.relative_path]).hexdigest()
        )


def test_issue_creates_one_public_snapshot_then_reuses_it(env):
    first = smoke_credentials.issue(env.state, env.project["id"], DEST)
    digest = first["snapshot_digest"]
    assert digest == smoke_snapshot.capture_synthetic()[1]
    (row,) = _snapshots(env, digest)
    assert row["id"] == first["snapshot_id"] and row["project_id"] == env.project["id"]
    second = smoke_credentials.issue(env.state, env.project["id"], DEST)
    assert second["snapshot_id"] == first["snapshot_id"] and second["snapshot_digest"] == digest
    assert len(_snapshots(env, digest)) == 1
    assert second["grant_id"] != first["grant_id"]
    adopted = env.query(
        "SELECT payload_json FROM events WHERE event_type='workspace.snapshot_adopted'"
        " AND payload_json LIKE ?",
        f"%{digest}%",
    )
    assert len(adopted) == 1
    issued = env.query(
        "SELECT payload_json FROM operator_audit_events WHERE event_type=?",
        "smoke_credentials.issued",
    )
    assert {json.loads(r["payload_json"])["snapshot_id"] for r in issued} == {row["id"]}


def test_the_data_class_record_is_public_cc0_and_labelled(env):
    grant = smoke_credentials.issue(env.state, env.project["id"], DEST)
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        record = snapshot_data_class(connection, grant["snapshot_id"])
        stored = connection.execute(
            "SELECT * FROM snapshot_data_classes WHERE snapshot_id=?", (grant["snapshot_id"],)
        ).fetchone()
    finally:
        connection.close()
    assert record["data_class"] == "public" and record["license"] == "CC0-1.0"
    assert record["source"] == "https://tessera.invalid/smoke-synthetic-v1"
    assert record["decision_class"] == "owner_automation_labelled"
    provenance = json.loads(stored["provenance_json"])
    assert provenance["automation_label"] == "smoke-credentials"
    assert provenance["decision_class"] == "owner_automation_labelled"
    assert stored["origin"] == "human_declared"


def test_adoption_failure_leaves_no_grant_and_no_files(env, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise Conflict("adoption refused")

    monkeypatch.setattr(SnapshotStore, "adopt_snapshot", refuse)
    before = env.query("SELECT COUNT(*) AS n FROM credentials")[0]["n"]
    with pytest.raises(Conflict):
        smoke_credentials.issue(env.state, env.project["id"], DEST)
    assert env.query("SELECT 1 FROM smoke_credential_grants") == []
    assert env.query("SELECT COUNT(*) AS n FROM credentials")[0]["n"] == before
    digest = smoke_snapshot.capture_synthetic()[1]
    assert _snapshots(env, digest) == []


def test_a_non_public_row_with_the_same_digest_is_refused(env):
    capture, digest = smoke_snapshot.capture_synthetic()
    owner = dict(env.owner, automation_label="test")
    # Same bytes adopted without a public declaration: private_derived, never correctable.
    SnapshotStore(env.state).adopt_snapshot(
        owner, env.project["id"], "private-copy", digest, capture
    )
    before = env.query("SELECT COUNT(*) AS n FROM credentials")[0]["n"]
    with pytest.raises(Conflict, match=smoke_snapshot.REFUSAL):
        smoke_credentials.issue(env.state, env.project["id"], DEST)
    assert len(_snapshots(env, digest)) == 1
    assert env.query("SELECT 1 FROM smoke_credential_grants") == []
    assert env.query("SELECT COUNT(*) AS n FROM credentials")[0]["n"] == before


def test_an_agent_credential_still_cannot_adopt_or_declare_public(env):
    grant = smoke_credentials.issue(env.state, env.project["id"], DEST)
    agent = env.plain.authenticate(grant["agent_token"])
    capture, digest = smoke_snapshot.capture_synthetic()
    with pytest.raises(Forbidden):
        SnapshotStore(env.state).adopt_snapshot(
            agent,
            env.project["id"],
            "agent-try",
            digest,
            capture,
            data_class=dict(smoke_snapshot.DATA_CLASS),
        )
