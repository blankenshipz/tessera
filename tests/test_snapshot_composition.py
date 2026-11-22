"""Composed input snapshots (ADR 0038): named, prefixed sources in one immutable snapshot."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from test_artifact_custody import _released_manifest

import tessera_control.snapshot_composition as composition
from tessera_control.api import create_app
from tessera_control.approval_trust import AUTOMATION_KEY, CAMPAIGN_DELEGATION_KEY
from tessera_control.campaign_delegation import assert_compose_sources_owned
from tessera_control.snapshot_chunks import chunk_index, read_chunk
from tessera_control.snapshot_composition import SnapshotCompositionStore, composed_inventory
from tessera_control.snapshot_composition_api import router
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Conflict, Forbidden, NotFound, StoreError, canonical_digest
from tessera_control.workspace import capture_workspace, inventory_digest


def _adopt(state: Path, owner: dict, project_id: str, files: dict[str, bytes], name: str) -> dict:
    root = state.parent / f"adopt-{name}"
    root.mkdir()
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    capture = capture_workspace(root.absolute())
    digest = inventory_digest(capture.inventory)
    _, snapshot = SnapshotStore(state).adopt_snapshot(owner, project_id, name, digest, capture)
    return snapshot


def _expected(parts: list[tuple[str, dict[str, bytes]]]) -> str:
    import hashlib

    entries = [
        (f"{name}/{path}", hashlib.sha256(content).hexdigest(), len(content), "imported_unverified")
        for name, files in parts
        for path, content in files.items()
    ]
    return inventory_digest(composed_inventory(entries))


def _db(state: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(state / "tessera.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def test_compose_snapshots_and_a_released_manifest(tmp_path):
    state, owner, worker, manifest, first, second, _custody = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    code_files = {"a.txt": b"alpha", "sub/b.txt": b"beta"}
    code = _adopt(state, owner, project_id, code_files, "code")
    with _db(state) as db:
        listed = json.loads(
            db.execute(
                "SELECT artifacts_json FROM artifact_manifests WHERE id=?", (manifest["id"],)
            ).fetchone()[0]
        )
    second_entry = next(e for e in listed if e["relative_path"] == "second.txt")
    import hashlib

    entries = [
        (f"code/{p}", hashlib.sha256(c).hexdigest(), len(c), "imported_unverified")
        for p, c in code_files.items()
    ] + [("up/second.txt", second_entry["sha256"], second_entry["size"], "imported_unverified")]
    expected = inventory_digest(composed_inventory(entries))
    body = {
        "sources": [
            {
                "name": "code",
                "kind": "snapshot",
                "snapshot_id": code["id"],
                "expected_digest": code["digest"],
            },
            {
                "name": "up",
                "kind": "manifest",
                "manifest_id": manifest["id"],
                "expected_manifest_digest": manifest["manifest_digest"],
                "relative_paths": ["second.txt"],
            },
        ],
        "expected_snapshot_digest": expected,
    }
    store = SnapshotCompositionStore(state)
    digest = canonical_digest(body)
    status, snapshot = store.compose(owner, project_id, "compose", digest, body)
    assert status == 201 and snapshot["digest"] == expected
    assert snapshot["transfer_authorized"] is False
    assert store.compose(owner, project_id, "compose", digest, body) == (status, snapshot)
    with _db(state) as db:
        paths = {
            row["relative_path"]: bytes(row["content"])
            for row in db.execute(
                "SELECT relative_path, content FROM snapshot_files WHERE snapshot_id=?",
                (snapshot["id"],),
            )
        }
        assert set(paths) == {"code/a.txt", "code/sub/b.txt", "up/second.txt"}
        assert paths["code/a.txt"] == b"alpha"
        event = json.loads(
            db.execute(
                "SELECT payload_json FROM events WHERE event_type='snapshot.composed'"
            ).fetchone()[0]
        )
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE event_type='snapshot.composed'"
            ).fetchone()[0]
            == 1
        )
    assert event["snapshot_id"] == snapshot["id"] and event["digest"] == expected
    assert [s["name"] for s in event["sources"]] == ["code", "up"]
    assert {f["path"] for f in event["files"]} == set(paths)
    # The result is an ordinary retained snapshot.
    assert SnapshotStore(state).verify_snapshot(owner, snapshot["id"])["integrity"] == "verified"


def test_compose_refusals(tmp_path):
    state, owner, worker, manifest, _first, _second, _custody = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    code = _adopt(state, owner, project_id, {"a.txt": b"alpha"}, "code")
    expected = _expected([("code", {"a.txt": b"alpha"})])
    source = {
        "name": "code",
        "kind": "snapshot",
        "snapshot_id": code["id"],
        "expected_digest": code["digest"],
    }
    body = {"sources": [source], "expected_snapshot_digest": expected}
    store = SnapshotCompositionStore(state)

    def compose(actor, payload, key):
        return store.compose(actor, project_id, key, canonical_digest(payload), payload)

    with pytest.raises(Conflict):  # wrong composed digest
        compose(owner, {**body, "expected_snapshot_digest": "0" * 64}, "k1")
    with pytest.raises(Conflict):  # wrong source digest
        compose(owner, {**body, "sources": [{**source, "expected_digest": "1" * 64}]}, "k2")
    with pytest.raises(NotFound):  # unknown source snapshot
        compose(owner, {**body, "sources": [{**source, "snapshot_id": "snapshot_nope"}]}, "k3")
    with pytest.raises(Conflict):  # wrong manifest digest
        compose(
            owner,
            {
                "sources": [
                    {
                        "name": "up",
                        "kind": "manifest",
                        "manifest_id": manifest["id"],
                        "expected_manifest_digest": "2" * 64,
                    }
                ],
                "expected_snapshot_digest": expected,
            },
            "k4",
        )
    with pytest.raises(Forbidden):  # a worker credential is not a human
        compose(worker, body, "k5")
    for bad in (
        {**body, "sources": []},
        {**body, "sources": [source, {**source}]},  # duplicate names
        {**body, "sources": [{**source, "kind": "manifest"}]},
        {**body, "sources": [{**source, "name": "Bad/Name"}]},
        {**body, "extra": 1},
    ):
        with pytest.raises(StoreError):
            compose(owner, bad, "bad" + str(id(bad)))
    with _db(state) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM snapshot_files WHERE relative_path LIKE 'code/%'"
            ).fetchone()[0]
            == 0
        )


def test_composition_above_the_legacy_bounds_uses_chunks_and_chains(tmp_path, monkeypatch):
    state, owner, _worker, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    files = {"one.txt": b"1" * 10, "two.txt": b"2" * 20}
    base = _adopt(state, owner, project_id, files, "base")
    monkeypatch.setattr(composition, "MAX_SNAPSHOT_FILES", 1)
    store = SnapshotCompositionStore(state)
    source = {
        "name": "base",
        "kind": "snapshot",
        "snapshot_id": base["id"],
        "expected_digest": base["digest"],
    }
    body = {"sources": [source], "expected_snapshot_digest": _expected([("base", files)])}
    status, chunked = store.compose(owner, project_id, "c1", canonical_digest(body), body)
    assert status == 201
    with _db(state) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM snapshot_files WHERE snapshot_id=?", (chunked["id"],)
            ).fetchone()[0]
            == 0
        )
        index = chunk_index(db, chunked["id"])
        assert {c["relative_path"] for c in index} == {"base/one.txt", "base/two.txt"}
        assert read_chunk(db, chunked["id"], index[0]["seq"])[1] in {b"1" * 10, b"2" * 20}
    # A chunked snapshot is itself a valid source for a further composition.
    nested = {
        "sources": [
            {
                "name": "again",
                "kind": "snapshot",
                "snapshot_id": chunked["id"],
                "expected_digest": chunked["digest"],
            }
        ],
        "expected_snapshot_digest": _expected(
            [("again", {"base/one.txt": files["one.txt"], "base/two.txt": files["two.txt"]})]
        ),
    }
    status, second = store.compose(owner, project_id, "c2", canonical_digest(nested), nested)
    assert status == 201
    with _db(state) as db:
        paths = {c["relative_path"] for c in chunk_index(db, second["id"])}
    assert paths == {"again/base/one.txt", "again/base/two.txt"}


def test_composed_inventory_is_order_independent():
    a = ("x/a.txt", "a" * 64, 1, "imported_unverified")
    b = ("x/b.txt", "b" * 64, 2, "imported_unverified")
    assert inventory_digest(composed_inventory([a, b])) == inventory_digest(
        composed_inventory([b, a])
    )
    assert composed_inventory([a, b]).total_bytes == 3


def test_http_route_is_human_only_and_replays(tmp_path):
    env = Env(tmp_path)
    body = {
        "sources": [
            {
                "name": "code",
                "kind": "snapshot",
                "snapshot_id": env.snapshot_id,
                "expected_digest": env.snapshot_digest,
            }
        ],
        "expected_snapshot_digest": "0" * 64,
    }
    app = create_app(env.state)
    app.state.snapshot_composition = SnapshotCompositionStore(env.state)
    app.include_router(router)
    client = TestClient(app)
    owner_token = (env.state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "http",
        "X-Tessera-Automation": "test",
    }
    path = f"/projects/{env.project['id']}/composed-snapshots"
    assert client.post(path, headers=headers, json=body).status_code == 409  # digest fence
    worker = client.post(
        path,
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "w"},
        json=body,
    )
    assert worker.status_code == 401
    assert client.post(path, headers=headers, json={**body, "x": 1}).status_code == 422


def test_delegated_composition_is_limited_to_campaign_sources(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    other = _adopt(env.state, env.owner, env.project["id"], {"z.txt": b"zzz"}, "other")
    store = SnapshotCompositionStore(env.state)
    files = {"input.txt": b"immutable input"}
    mine = {
        "name": "code",
        "kind": "snapshot",
        "snapshot_id": env.snapshot_id,
        "expected_digest": env.snapshot_digest,
    }
    body = {"sources": [mine], "expected_snapshot_digest": _expected([("code", files)])}
    principal, _, key, digest = env.intent(
        campaign,
        approval,
        "compose_snapshot",
        {"project_id": env.project["id"]},
        body,
        stage_key="s2",
    )
    status, snapshot = store.compose(principal, env.project["id"], key, digest, body)
    assert status == 201
    done = env.events("workspace.snapshot_adopted")[-1]
    assert done["payload"]["snapshot_id"] == snapshot["id"]
    assert env.events("snapshot.composed")[-1]["payload"]["sources"][0]["id"] == env.snapshot_id
    # A project snapshot the spec does not name is refused, even though the body is valid.
    foreign = {
        "sources": [
            {
                "name": "code",
                "kind": "snapshot",
                "snapshot_id": other["id"],
                "expected_digest": other["digest"],
            }
        ],
        "expected_snapshot_digest": _expected([("code", {"z.txt": b"zzz"})]),
    }
    principal, _, key, digest = env.intent(
        campaign,
        approval,
        "compose_snapshot",
        {"project_id": env.project["id"]},
        foreign,
        stage_key="s2",
        ordinal=1,
    )
    before = env.query("SELECT count(*) AS n FROM source_snapshots")[0]["n"]
    with pytest.raises(Forbidden):
        store.compose(principal, env.project["id"], key, digest, foreign)
    assert env.query("SELECT count(*) AS n FROM source_snapshots")[0]["n"] == before


def test_manifest_sources_must_be_campaign_attempt_outputs(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    now = "2026-01-01T00:00:00Z"
    env.execute(
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, manifest_id, revision, created_at, updated_at) "
        "VALUES ('att1', ?, 's1', 1, 'collected', 0, 'manifest_mine', 1, ?, ?)",
        campaign["id"],
        now,
        now,
    )
    live = {"campaign_delegated": {"campaign_id": campaign["id"]}}
    with env.store.runner_connection() as connection:
        assert_compose_sources_owned(connection, live, [("manifest", "manifest_mine")])
        with pytest.raises(Forbidden):
            assert_compose_sources_owned(connection, live, [("manifest", "manifest_theirs")])
        with pytest.raises(Forbidden):
            assert_compose_sources_owned(connection, live, [("snapshot", "snapshot_unrelated")])
        # Not delegated: no restriction here (the command's own human checks apply).
        assert_compose_sources_owned(connection, {}, [("manifest", "manifest_theirs")])


def test_staging_does_not_widen_who_may_compose_chunk_stored_sources(tmp_path, monkeypatch):
    """Pre-staging a chunk-stored source keeps the decision-actor rule for undelegated actors."""
    state, owner, _worker, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    files = {"one.txt": b"1" * 10, "two.txt": b"2" * 20}
    base = _adopt(state, owner, project_id, files, "base")
    monkeypatch.setattr(composition, "MAX_SNAPSHOT_FILES", 1)
    store = SnapshotCompositionStore(state)

    def body_for(snapshot: dict) -> dict:
        return {
            "sources": [
                {
                    "name": "base",
                    "kind": "snapshot",
                    "snapshot_id": snapshot["id"],
                    "expected_digest": snapshot["digest"],
                }
            ],
            "expected_snapshot_digest": _expected([("base", files)]),
        }

    body = body_for(base)
    status, chunked = store.compose(owner, project_id, "c1", canonical_digest(body), body)
    assert status == 201
    again = {
        "sources": [
            {
                "name": "again",
                "kind": "snapshot",
                "snapshot_id": chunked["id"],
                "expected_digest": chunked["digest"],
            }
        ],
        "expected_snapshot_digest": _expected(
            [("again", {"base/one.txt": files["one.txt"], "base/two.txt": files["two.txt"]})]
        ),
    }
    digest = canonical_digest(again)

    def uploads() -> int:
        with _db(state) as db:
            return db.execute("SELECT count(*) FROM snapshot_chunk_uploads").fetchone()[0]

    before = uploads()
    unlabelled = {k: v for k, v in owner.items() if k != AUTOMATION_KEY}
    with pytest.raises(Forbidden, match="human_session"):
        store.compose(unlabelled, project_id, "c2", digest, again)
    # A claimed delegation that its approval and intent do not back is refused before any copy.
    forged = dict(owner) | {CAMPAIGN_DELEGATION_KEY: {"campaign_id": "x", "intent_id": "y"}}
    with pytest.raises(Forbidden):
        store.compose(forged, project_id, "c3", digest, again)
    assert uploads() == before


def test_another_campaigns_delegation_cannot_stage_this_campaigns_chunk_source(
    tmp_path, monkeypatch
):
    """A real, active delegation of campaign B is refused for a chunk-stored source that only
    campaign A owns, before anything is copied."""
    env = Env(tmp_path)
    files = {"one.txt": b"1" * 10, "two.txt": b"2" * 20}
    base = _adopt(env.state, env.owner, env.project["id"], files, "base")
    monkeypatch.setattr(composition, "MAX_SNAPSHOT_FILES", 1)
    store = SnapshotCompositionStore(env.state)
    first = {
        "sources": [
            {
                "name": "base",
                "kind": "snapshot",
                "snapshot_id": base["id"],
                "expected_digest": base["digest"],
            }
        ],
        "expected_snapshot_digest": _expected([("base", files)]),
    }
    status, chunked = store.compose(
        env.owner, env.project["id"], "c1", canonical_digest(first), first
    )
    assert status == 201
    campaign_a, approval_a = env.approved()
    campaign_b, approval_b = env.approved()
    assert campaign_a["id"] != campaign_b["id"]
    # Only campaign A owns the chunk-stored snapshot (recorded as its stage input).
    env.execute(
        "UPDATE campaign_stages SET input_snapshot_id=? WHERE campaign_id=?",
        chunked["id"],
        campaign_a["id"],
    )
    body = {
        "sources": [
            {
                "name": "again",
                "kind": "snapshot",
                "snapshot_id": chunked["id"],
                "expected_digest": chunked["digest"],
            }
        ],
        "expected_snapshot_digest": _expected(
            [("again", {"base/one.txt": files["one.txt"], "base/two.txt": files["two.txt"]})]
        ),
    }

    def uploads() -> int:
        return env.query("SELECT count(*) AS n FROM snapshot_chunk_uploads")[0]["n"]

    resources = {"project_id": env.project["id"]}
    before = uploads()
    principal, _, key, digest = env.intent(
        campaign_b, approval_b, "compose_snapshot", resources, body, stage_key="s2"
    )
    with pytest.raises(Forbidden):
        store.compose(principal, env.project["id"], key, digest, body)
    assert uploads() == before
    # Positive control: campaign A's own delegation stages and composes the same source.
    principal, _, key, digest = env.intent(
        campaign_a, approval_a, "compose_snapshot", resources, body, stage_key="s2"
    )
    status, composed = store.compose(principal, env.project["id"], key, digest, body)
    assert status == 201 and uploads() == before + 1
