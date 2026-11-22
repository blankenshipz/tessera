"""Staging released collected outputs as an input snapshot (ADR 0034 s4); synthetic only."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_artifact_custody import _released_manifest
from test_execution import _grant_payload, _task

from tessera_control import agent_research_results as results
from tessera_control.api import create_app
from tessera_control.dataset_staging import (
    DatasetStagingStore,
    staged_inventory,
    staging_for_snapshot,
)
from tessera_control.execution import ExecutionStore
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.store import Conflict, canonical_digest
from tessera_control.workspace import inventory_digest
from tessera_worker.inputs import materialize_snapshot

FILES = {"first.txt": b"direct", "second.txt": b"chunked"}


def _entries(manifest, paths=None):
    import json

    with sqlite3.connect(Path(manifest["_state"]) / "tessera.sqlite3") as db:
        raw = db.execute(
            "SELECT artifacts_json FROM artifact_manifests WHERE id=?", (manifest["id"],)
        ).fetchone()[0]
    return [e for e in json.loads(raw) if paths is None or e["relative_path"] in paths]


def _body(state, manifest, paths=None):
    entries = _entries(dict(manifest, _state=str(state)), paths)
    digest = inventory_digest(staged_inventory(manifest["id"], entries))
    return {
        "manifest_id": manifest["id"],
        "expected_manifest_digest": manifest["manifest_digest"],
        "relative_paths": paths,
        "expected_snapshot_digest": digest,
    }


def _stage(store, actor, manifest, body, key="stage"):
    return store.stage(actor, manifest["project_id"], key, body)


def test_stage_subset_replay_event_and_read(tmp_path: Path) -> None:
    state, owner, _w, manifest, first, _s, _c = _released_manifest(tmp_path)
    store = DatasetStagingStore(state)
    body = _body(state, manifest)
    status, snap = _stage(store, owner, manifest, body)
    assert status == 201 and snap["digest"] == body["expected_snapshot_digest"]
    assert snap["transfer_authorized"] is False
    assert _stage(store, owner, manifest, body) == (status, snap)
    staging = snap["staging"]
    assert [f["relative_path"] for f in staging["files"]] == ["first.txt", "second.txt"]
    assert staging["manifest_digest"] == manifest["manifest_digest"]
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE event_type='snapshot.staged_from_artifacts'"
            ).fetchone()[0]
            == 1
        )
        assert staging_for_snapshot(db, snap["id"])["snapshot_id"] == snap["id"]
        assert staging_for_snapshot(db, "nope") is None
    assert store.get_staging(owner, snap["id"])["files"] == staging["files"]
    # subset
    sub = _body(state, manifest, ["second.txt"])
    _, one = _stage(store, owner, manifest, sub, "subset")
    assert [f["relative_path"] for f in one["staging"]["files"]] == ["second.txt"]
    with pytest.raises(Conflict):  # wrong fence for a different subset
        _stage(
            store,
            owner,
            manifest,
            dict(sub, expected_snapshot_digest=body["expected_snapshot_digest"]),
            "bad",
        )


def test_stage_denials(tmp_path: Path) -> None:
    state, owner, worker, manifest, first, _s, custody = _released_manifest(tmp_path)
    store = DatasetStagingStore(state)
    body = _body(state, manifest)
    with pytest.raises(Exception) as denied:
        _stage(store, worker, manifest, body, "w")
    assert denied.type.__name__ in {"Forbidden", "Unauthorized", "NotFound"}
    with pytest.raises(Conflict):
        _stage(store, owner, manifest, dict(body, expected_manifest_digest="0" * 64), "m")
    with pytest.raises(Conflict):
        _stage(store, owner, manifest, dict(body, expected_snapshot_digest="0" * 64), "s")
    # tampered stored bytes
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        key = db.execute(
            "SELECT object_key FROM project_artifact_placements WHERE artifact_id=?",
            (first["id"],),
        ).fetchone()[0]
    objects = [p for p in (state / "project-artifacts").rglob("*") if p.is_file() and key in str(p)]
    assert objects
    original = objects[0].read_bytes()
    objects[0].write_bytes(b"tampered")
    with pytest.raises(Conflict):
        _stage(store, owner, manifest, body, "t")
    objects[0].write_bytes(original)
    # revoke custody
    revoke = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 1,
        "action": "revoke",
        "rationale": "end access",
    }
    custody.record_release(owner, manifest["id"], "revoke", canonical_digest(revoke), revoke)
    with pytest.raises(Conflict):
        _stage(store, owner, manifest, body, "r")


def test_api_roles_and_staging_read(tmp_path: Path) -> None:
    state, owner, _w, manifest, _f, _s, _c = _released_manifest(tmp_path)
    app = create_app(state)
    if getattr(app.state, "dataset_staging", None) is None:
        from tessera_control.dataset_staging_api import router

        app.state.dataset_staging = DatasetStagingStore(state)
        app.include_router(router)
    client = TestClient(app)
    human = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    worker = {"Authorization": "Bearer worker-token"}
    body = _body(state, manifest)
    path = f"/projects/{manifest['project_id']}/artifact-snapshots"
    assert client.post(path, json=body, headers=worker | {"Idempotency-Key": "w"}).status_code in {
        401,
        403,
    }
    ok = client.post(path, json=body, headers=human | {"Idempotency-Key": "h"})
    assert ok.status_code == 201, ok.text
    again = client.post(path, json=body, headers=human | {"Idempotency-Key": "h"})
    assert again.json() == ok.json()
    got = client.get(f"/snapshots/{ok.json()['id']}/staging", headers=human)
    assert got.status_code == 200 and got.json()["manifest_id"] == manifest["id"]
    other = client.get("/snapshots/missing/staging", headers=human)
    assert other.status_code == 404
    snap = client.get(f"/snapshots/{ok.json()['id']}", headers=human).json()
    assert snap["digest"] == body["expected_snapshot_digest"]


def test_staged_snapshot_reaches_worker_and_result_summary(tmp_path: Path, trust_policy) -> None:
    state, owner, worker, manifest, _f, _s, _c = _released_manifest(tmp_path)
    # ADR 0040: private_derived reaches a local grant only for a worker principal the
    # operator bound to a provider with that class (the builtin default binds none).
    trust_policy({"local-docker": (["public", "private_derived"], [worker["id"]])})
    project_id = manifest["project_id"]
    store = DatasetStagingStore(state)
    body = _body(state, manifest)
    _, snap = _stage(store, owner, manifest, body)
    execution = ExecutionStore(state)
    limits = {
        "expected_revision": 1,
        "currency": "USD",
        "monetary_limit_cents": 500,
        "max_concurrency": 3,
    }
    execution.configure_project_execution(
        owner, project_id, "limits2", canonical_digest(limits), limits
    )
    release = {
        "expected_digest": snap["digest"],
        "worker_principal_id": worker["id"],
        # Staged outputs are recorded private_derived (ADR 0039); a local CPU grant
        # without egress accepts that class for a bound worker (ADR 0040), a public
        # release is refused.
        "classification": "private_derived",
        "permits_worker_transfer": True,
        "rationale": "Stage-and-run test release.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    SnapshotReleaseStore(state).release_snapshot(
        owner, snap["id"], "rel", canonical_digest(release), release
    )
    task = _task(state, owner, project_id, snap["digest"], "Staged")
    payload = _grant_payload(task, worker, {"id": snap["id"]})
    _, grant = execution.issue_execution_grant(
        owner, project_id, "g-staged", canonical_digest(payload), payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "l-staged", "l-staged")
    app = TestClient(create_app(state))
    response = app.post(
        f"/worker/leases/{lease['id']}/inputs",
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "in"},
        json={"expected_epoch": lease["epoch"]},
    )
    assert response.status_code == 201, response.text
    parent = tmp_path / "mat"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    inputs = materialize_snapshot(
        response.json(),
        parent,
        expected_snapshot_digest=snap["digest"],
        expected_lease_id=lease["id"],
        expected_epoch=lease["epoch"],
        expected_worker_id=worker["id"],
    )
    for name, content in FILES.items():
        assert (inputs.directory / name).read_bytes() == content
        assert hashlib.sha256(content).hexdigest()

    # result-summary `inputs` section loader against this seeded connection
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        experiment_id = "exp_seed"
        db.execute("PRAGMA foreign_keys=OFF")
        _seed_attempt(db, experiment_id, grant["id"], task["id"], task["current_plan_digest"])
        section = results._inputs(db, {}, {"id": experiment_id})
        attempts = results._attempts(db, {}, {"id": experiment_id})["admitted"]
    assert section[0]["snapshot_id"] == snap["id"]
    assert section[0]["snapshot_digest"] == snap["digest"]
    assert section[0]["staged_from_artifacts"] == {
        "manifest_id": manifest["id"],
        "manifest_digest": manifest["manifest_digest"],
        "operation_id": manifest["operation_id"],
        "files_count": 2,
    }
    assert attempts[0]["network_profile"] == "none"
    assert attempts[0]["egress_journal_sha256"] is None


def _seed_attempt(db, experiment_id, grant_id, task_id, plan_digest):
    now = "2026-01-01T00:00:00Z"
    cols = [r[1] for r in db.execute("PRAGMA table_info(research_experiment_admitted_attempts)")]
    row = {
        "id": "att_seed", "experiment_id": experiment_id, "design_revision": 1,
        "variant_id": "var_seed", "task_id": task_id, "plan_digest": plan_digest,
        "grant_id": grant_id, "operation_id": None, "package_digest": None,
        "state": "admitted", "outcome": None, "reason": None, "revision": 1,
        "created_at": now, "updated_at": now, "created_by_principal_id": "p",
        "resolved_by_principal_id": None,
    }  # fmt: skip
    use = {k: v for k, v in row.items() if k in cols}
    db.execute(
        f"INSERT INTO research_experiment_admitted_attempts ({','.join(use)}) "
        f"VALUES ({','.join('?' * len(use))})",
        tuple(use.values()),
    )


def test_result_summary_reads_network_profile_from_the_approved_plan(tmp_path: Path) -> None:
    state, owner, worker, manifest, _f, _s, _c = _released_manifest(tmp_path)
    task = _task(state, owner, manifest["project_id"], "1" * 64, "Egress")
    egress = '{"profile":"allowlist-proxy-v1","destinations":[{"host":"example.org","port":443}]}'
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=OFF")
        triggers = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='task_revisions'"
            )
        ]
        for name in triggers:  # test-only seeding of an egress plan revision
            db.execute(f"DROP TRIGGER {name}")
        db.execute(
            "UPDATE task_revisions SET execution_spec_json="
            "json_set(execution_spec_json,'$.egress',json(?)) WHERE task_id=?",
            (egress, task["id"]),
        )
        _seed_attempt(db, "exp_egress", "grant_x", task["id"], task["current_plan_digest"])
        attempts = results._attempts(db, {}, {"id": "exp_egress"})["admitted"]
    assert attempts[0]["network_profile"] == "allowlist-proxy-v1"
