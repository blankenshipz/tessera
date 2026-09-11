from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers
from test_artifact_manifests import _body, _completed_outputs, _operation

from tessera_control.api import create_app
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.execution import ExecutionStore
from tessera_control.migrations import MIGRATIONS
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.result_checkpoints import ResultCheckpointStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _connect,
    canonical_digest,
    migrate_state,
)


def _owner(state: Path) -> dict[str, object]:
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    return Store(state).authenticate(token)


def _checkpoint_request(
    grant: dict[str, object], lease: dict[str, object], manifest: dict[str, object]
):
    return {
        "expected_grant_revision": grant["revision"],
        "expected_epoch": lease["epoch"],
        "lease_id": lease["id"],
        "operation_id": _operation(lease),
        "manifest_id": manifest["id"],
        "expected_manifest_digest": manifest["manifest_digest"],
        "validation_contract": "synthetic-declared-output-v1",
        "expected_artifacts": manifest["artifacts"],
        "expected_total_bytes": manifest["total_bytes"],
    }


def _reconciled_manifest(tmp_path: Path):
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    owner = _owner(state)
    manifest_body = _body(lease, first, second)
    _, manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(manifest_body), manifest_body
    )
    execution = ExecutionStore(state)
    observation = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "synthetic-provider-operation",
        "termination_confirmed": True,
    }
    execution.report_execution_observation(
        worker, lease["id"], "exited", canonical_digest(observation), observation
    )
    grant = execution.get_execution_grant(owner, first["grant_id"])
    reconcile = {
        "expected_revision": grant["revision"],
        "settlement_cents": 0,
        "provider_terminated": True,
    }
    _, grant = execution.reconcile_execution_grant(
        owner, grant["id"], "reconcile", canonical_digest(reconcile), reconcile
    )
    return state, owner, worker, grant, lease, manifest, first


def test_result_checkpoint_commits_reconciled_complete_manifest_and_replays_after_restart(
    tmp_path: Path,
) -> None:
    state, owner, _worker, grant, lease, manifest, _first = _reconciled_manifest(tmp_path)
    body = _checkpoint_request(grant, lease, manifest)
    store = ResultCheckpointStore(state)
    status, checkpoint = store.commit(
        owner, grant["id"], "checkpoint", canonical_digest(body), body
    )
    assert status == 201
    assert checkpoint["state"] == "committed_quarantined"
    assert checkpoint["synthetic_validation"] is True
    assert checkpoint["scientific_validity"] == "not_assessed"
    assert checkpoint["execution_receipt"] == "trusted_worker_termination_observation_only"
    assert checkpoint["manifest_digest"] == manifest["manifest_digest"]
    restarted = ResultCheckpointStore(state)
    assert restarted.commit(owner, grant["id"], "checkpoint", canonical_digest(body), body) == (
        201,
        checkpoint,
    )
    assert restarted.commit(owner, grant["id"], "another-key", canonical_digest(body), body) == (
        200,
        checkpoint,
    )
    assert restarted.list_checkpoints(owner, manifest["project_id"], 0, 200) == [checkpoint]
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        event = connection.execute(
            """SELECT payload_json FROM events
            WHERE event_type='execution.result_checkpoint_committed'"""
        ).fetchone()[0]
        assert b"direct" not in event.encode() and "synthetic_validation" in event
        manifest_row = connection.execute(
            "SELECT status, classification FROM artifact_manifests WHERE id=?", (manifest["id"],)
        ).fetchone()
        assert tuple(manifest_row) == ("quarantined", "unclassified")


def test_result_checkpoint_api_requires_control_human_and_lists_project_scope(
    tmp_path: Path,
) -> None:
    state, _owner, _worker, grant, lease, manifest, _first = _reconciled_manifest(tmp_path)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    body = _checkpoint_request(grant, lease, manifest)
    client = TestClient(create_app(state))
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "checkpoint-api"}
    response = client.post(
        f"/execution-grants/{grant['id']}/result-checkpoint", headers=headers, json=body
    )
    assert response.status_code == 201
    assert (
        client.post(
            f"/execution-grants/{grant['id']}/result-checkpoint",
            headers=_other_tenant_headers(state) | {"Idempotency-Key": "other-tenant"},
            json=body,
        ).status_code
        == 404
    )
    listed = client.get(
        f"/projects/{manifest['project_id']}/result-checkpoints",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200
    assert listed.json()["result_checkpoints"] == [response.json()]


@pytest.mark.parametrize(
    "change",
    ("stale_epoch", "wrong_operation", "revision_race", "invalid_validation", "wrong_manifest"),
)
def test_result_checkpoint_rejects_stale_or_mismatched_command_identity(
    tmp_path: Path, change: str
) -> None:
    state, owner, _worker, grant, lease, manifest, _first = _reconciled_manifest(tmp_path)
    body = _checkpoint_request(grant, lease, manifest)
    if change == "stale_epoch":
        body["expected_epoch"] = int(lease["epoch"]) + 1
    elif change == "wrong_operation":
        body["operation_id"] = "0" * 32
    elif change == "revision_race":
        body["expected_grant_revision"] = int(grant["revision"]) - 1
    elif change == "invalid_validation":
        body["expected_artifacts"] = body["expected_artifacts"][:-1]
    else:
        body["manifest_id"] = "artifact_manifest_wrong"
    with pytest.raises((Conflict, NotFound)):
        ResultCheckpointStore(state).commit(
            owner, grant["id"], change, canonical_digest(body), body
        )
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        count = connection.execute("SELECT count(*) FROM execution_result_checkpoints").fetchone()[
            0
        ]
        assert count == 0


@pytest.mark.parametrize("change", ("unconfirmed", "corrupt", "late", "manifest_digest"))
def test_result_checkpoint_rechecks_termination_and_quarantined_bytes(
    tmp_path: Path, change: str
) -> None:
    state, owner, _worker, grant, lease, manifest, first = _reconciled_manifest(tmp_path)
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        if change == "unconfirmed":
            connection.execute(
                """INSERT INTO execution_observations
                (id, grant_id, lease_id, worker_principal_id, epoch, desired_state, observed_state,
                observed_at, provider_id, termination_confirmed, recorded_at)
                VALUES (?, ?, ?, ?, ?, 'admitted', 'unknown', ?, NULL, 0, ?)""",
                (
                    "later_unknown",
                    grant["id"],
                    lease["id"],
                    lease["worker_principal_id"],
                    lease["epoch"],
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        elif change == "corrupt":
            connection.execute(
                "UPDATE artifacts SET content=? WHERE id=?", (b"corrupt", first["id"])
            )
        elif change == "late":
            connection.execute("UPDATE artifact_manifests SET late=1 WHERE id=?", (manifest["id"],))
    body = _checkpoint_request(grant, lease, manifest)
    if change == "manifest_digest":
        with closing(_connect(state / "tessera.sqlite3")) as connection:
            connection.execute(
                "UPDATE artifact_manifests SET artifacts_json='[]' WHERE id=?", (manifest["id"],)
            )
        body["expected_artifacts"] = []
        body["expected_total_bytes"] = 0
    with pytest.raises(Conflict):
        ResultCheckpointStore(state).commit(
            owner, grant["id"], change, canonical_digest(body), body
        )


def test_result_checkpoint_rechecks_corrupt_committed_project_object(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, manifest, first = _reconciled_manifest(tmp_path)
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": _operation(lease),
        "artifacts": [{"id": first["id"], "sha256": first["sha256"], "size": first["size"]}],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "placement", canonical_digest(placement), placement
    )
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        row = connection.execute(
            "SELECT tenant_namespace,project_namespace,object_key FROM project_artifact_placements"
        ).fetchone()
    (state / "project-artifacts" / row[0] / row[1] / row[2]).write_bytes(b"corrupt")
    body = _checkpoint_request(grant, lease, manifest)
    with pytest.raises(Conflict, match="project storage integrity"):
        ResultCheckpointStore(state).commit(
            owner, grant["id"], "checkpoint", canonical_digest(body), body
        )


def test_result_checkpoint_conflicts_on_changed_replay_and_rolls_back_event_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, owner, worker, grant, lease, manifest, _first = _reconciled_manifest(tmp_path)
    body = _checkpoint_request(grant, lease, manifest)
    store = ResultCheckpointStore(state)
    original_event = store._event

    def fail(*_args):
        raise sqlite3.OperationalError("event failure")

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(sqlite3.OperationalError):
        store.commit(owner, grant["id"], "atomic", canonical_digest(body), body)
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        count = connection.execute("SELECT count(*) FROM execution_result_checkpoints").fetchone()[
            0
        ]
        idempotency = connection.execute(
            "SELECT count(*) FROM idempotency WHERE idempotency_key='atomic'"
        ).fetchone()[0]
        assert count == 0
        assert idempotency == 0
    monkeypatch.setattr(store, "_event", original_event)
    _, checkpoint = store.commit(owner, grant["id"], "atomic", canonical_digest(body), body)
    changed = body | {"expected_total_bytes": int(body["expected_total_bytes"]) + 1}
    with pytest.raises(Conflict):
        store.commit(owner, grant["id"], "atomic", canonical_digest(changed), changed)
    with pytest.raises(Conflict):
        store.commit(owner, grant["id"], "new-key", canonical_digest(changed), changed)
    with pytest.raises(Forbidden):
        store.commit(worker, grant["id"], "worker", canonical_digest(body), body)
    assert ResultCheckpointStore(state).list_checkpoints(owner, manifest["project_id"], 0, 200) == [
        checkpoint
    ]


def test_v14_to_v15_checkpoint_migration_preserves_v14_evidence_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _owner, _worker, _grant, _lease, manifest, first = _reconciled_manifest(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE execution_result_checkpoints")
        from schema_fixtures import drop_empty_experiment_schema

        drop_empty_experiment_schema(connection)
        connection.execute("UPDATE schema_meta SET schema_version=14")

    def fail(connection):
        from tessera_control.result_checkpoints import create_result_checkpoint_schema

        create_result_checkpoint_schema(connection)
        raise RuntimeError("checkpoint migration failure")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 14, fail)
        with pytest.raises(RuntimeError, match="checkpoint migration"):
            migrate_state(state)
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 14
        assert connection.execute("SELECT id FROM artifacts WHERE id=?", (first["id"],)).fetchone()
        assert connection.execute(
            "SELECT id FROM artifact_manifests WHERE id=?", (manifest["id"],)
        ).fetchone()
    assert migrate_state(state) == 35
    assert migrate_state(state) == 35
