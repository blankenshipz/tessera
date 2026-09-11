"""Human result bookkeeping remains bounded to reconciled synthetic evidence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers
from test_result_checkpoints import _checkpoint_request, _reconciled_manifest

from tessera_control.api import create_app
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.result_acceptances import ResultAcceptanceStore
from tessera_control.result_checkpoints import ResultCheckpointStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    _connect,
    canonical_digest,
    migrate_state,
    utc_now,
)


def _checkpoint_with_receipt(tmp_path: Path):
    state, owner, worker, grant, lease, manifest, first = _reconciled_manifest(tmp_path)
    checkpoint_body = _checkpoint_request(grant, lease, manifest)
    _, checkpoint = ResultCheckpointStore(state).commit(
        owner, grant["id"], "checkpoint", canonical_digest(checkpoint_body), checkpoint_body
    )
    receipt_payload = {"development": "synthetic", "operation": checkpoint["operation_id"]}
    receipt_digest = canonical_digest(receipt_payload)
    now = utc_now()
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        connection.execute(
            """INSERT INTO external_operations
            (operation_id, org_id, project_id, task_id, grant_id, lease_id,
            worker_principal_id, epoch,
            package_digest, adapter_version, profile_version, docker_lookup_key, request_digest,
            retry_class, desired_state, observed_state, provider_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'development-local-cpu-v1',
            'local-cpu-v1', ?, ?, 'manual_reconciliation', 'admitted', 'exited', ?, ?, ?)""",
            (
                checkpoint["operation_id"],
                owner["org_id"],
                grant["project_id"],
                grant["task_id"],
                grant["id"],
                lease["id"],
                lease["worker_principal_id"],
                lease["epoch"],
                "a" * 64,
                f"lookup-{checkpoint['operation_id']}",
                "b" * 64,
                f"fake-{checkpoint['operation_id']}",
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO external_operation_receipts
            (operation_id, provider_kind, provider_version, provider_id, receipt_payload_json,
            receipt_digest, observed_state, observed_at, termination_confirmed, created_at,
            updated_at)
            VALUES (?, 'fake', 'test-v1', ?, ?, ?, 'exited', ?, 1, ?, ?)""",
            (
                checkpoint["operation_id"],
                f"fake-{checkpoint['operation_id']}",
                json.dumps(receipt_payload, separators=(",", ":"), sort_keys=True),
                receipt_digest,
                now,
                now,
                now,
            ),
        )
    return state, owner, worker, grant, lease, manifest, first, checkpoint, receipt_digest


def _request(
    checkpoint: dict[str, object], receipt_digest: str, *, action: str = "accept", revision: int = 0
):
    return {
        "expected_revision": revision,
        "action": action,
        "rationale": "Synthetic development bookkeeping review.",
        "expected_checkpoint_digest": checkpoint["commit_digest"],
        "expected_manifest_digest": checkpoint["manifest_digest"],
        "expected_validation_digest": checkpoint["validation_digest"],
        "expected_receipt_digest": receipt_digest,
        "expected_grant_revision": checkpoint["grant_revision"],
        "lease_id": checkpoint["lease_id"],
        "operation_id": checkpoint["operation_id"],
        "expected_epoch": checkpoint["epoch"],
    }


@pytest.mark.parametrize("action", ("accept", "reject"))
def test_human_acceptance_is_append_only_bookkeeping_and_preserves_evidence(
    tmp_path: Path, action: str
) -> None:
    state, owner, _, grant, _, manifest, first, checkpoint, receipt_digest = (
        _checkpoint_with_receipt(tmp_path)
    )
    body = _request(checkpoint, receipt_digest, action=action)
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        before = {
            "grant": tuple(
                connection.execute(
                    "SELECT status, revision FROM execution_grants WHERE id=?", (grant["id"],)
                ).fetchone()
            ),
            "manifest": tuple(
                connection.execute(
                    "SELECT status, classification, late FROM artifact_manifests WHERE id=?",
                    (manifest["id"],),
                ).fetchone()
            ),
            "artifact": tuple(
                connection.execute(
                    "SELECT status, classification, content FROM artifacts WHERE id=?",
                    (first["id"],),
                ).fetchone()
            ),
            "findings": connection.execute("SELECT count(*) FROM research_findings").fetchone()[0],
        }
    store = ResultAcceptanceStore(state)
    status, record = store.record(owner, checkpoint["id"], "accept", canonical_digest(body), body)
    assert status == 201
    assert record["action"] == action
    assert record["scientific_validity"] == "not_assessed"
    assert record["provider_success"] == "not_established"
    assert record["artifact_release"] == "not_performed"
    assert record["evidence_label"] == "development_synthetic_only"
    assert record["receipt_evidence"] == "fake_provider_development_only"
    assert record["authoritative_scope"] == "tessera_result_bookkeeping_and_ownership_only"
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT status, revision FROM execution_grants WHERE id=?", (grant["id"],)
                ).fetchone()
            )
            == before["grant"]
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT status, classification, late FROM artifact_manifests WHERE id=?",
                    (manifest["id"],),
                ).fetchone()
            )
            == before["manifest"]
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT status, classification, content FROM artifacts WHERE id=?",
                    (first["id"],),
                ).fetchone()
            )
            == before["artifact"]
        )
        assert (
            connection.execute("SELECT count(*) FROM research_findings").fetchone()[0]
            == before["findings"]
        )
        assert (
            connection.execute("SELECT count(*) FROM execution_result_acceptances").fetchone()[0]
            == 1
        )


def test_result_acceptance_requires_human_and_scoped_checkpoint(tmp_path: Path) -> None:
    state, owner, worker, _, _, _, _, checkpoint, receipt_digest = _checkpoint_with_receipt(
        tmp_path
    )
    body = _request(checkpoint, receipt_digest)
    store = ResultAcceptanceStore(state)
    with pytest.raises(Forbidden):
        store.record(worker, checkpoint["id"], "worker", canonical_digest(body), body)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    client = TestClient(create_app(state))
    response = client.post(
        f"/result-checkpoints/{checkpoint['id']}/acceptances",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "owner"},
        json=body,
    )
    assert response.status_code == 201
    listed = client.get(
        f"/result-checkpoints/{checkpoint['id']}/acceptances",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.json()["result_acceptances"] == [response.json()]
    response = client.post(
        f"/result-checkpoints/{checkpoint['id']}/acceptances",
        headers=_other_tenant_headers(state) | {"Idempotency-Key": "other"},
        json=body,
    )
    assert response.status_code == 404
    assert token


@pytest.mark.parametrize(
    "change", ("checkpoint", "manifest", "validation", "receipt", "grant", "epoch")
)
def test_result_acceptance_fences_stale_digests_grant_and_epoch(
    tmp_path: Path, change: str
) -> None:
    state, owner, _, _, _, _, _, checkpoint, receipt_digest = _checkpoint_with_receipt(tmp_path)
    body = _request(checkpoint, receipt_digest)
    field = {
        "checkpoint": "expected_checkpoint_digest",
        "manifest": "expected_manifest_digest",
        "validation": "expected_validation_digest",
        "receipt": "expected_receipt_digest",
        "grant": "expected_grant_revision",
        "epoch": "expected_epoch",
    }[change]
    body[field] = "0" * 64 if "digest" in field else int(body[field]) + 1
    with pytest.raises(Conflict):
        ResultAcceptanceStore(state).record(
            owner, checkpoint["id"], change, canonical_digest(body), body
        )


@pytest.mark.parametrize("change", ("late", "incomplete", "mutated", "released"))
def test_result_acceptance_rechecks_quarantine_manifest_and_retained_bytes(
    tmp_path: Path, change: str
) -> None:
    state, owner, _, _, _, manifest, first, checkpoint, receipt_digest = _checkpoint_with_receipt(
        tmp_path
    )
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        if change == "late":
            connection.execute("UPDATE artifact_manifests SET late=1 WHERE id=?", (manifest["id"],))
        elif change == "incomplete":
            connection.execute(
                "UPDATE artifact_manifests SET artifacts_json='[]' WHERE id=?", (manifest["id"],)
            )
        elif change == "mutated":
            connection.execute(
                "UPDATE artifacts SET content=? WHERE id=?", (b"mutated", first["id"])
            )
        else:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE artifact_manifests SET status='released' WHERE id=?", (manifest["id"],)
            )
    body = _request(checkpoint, receipt_digest)
    with pytest.raises(Conflict):
        ResultAcceptanceStore(state).record(
            owner, checkpoint["id"], change, canonical_digest(body), body
        )


def test_result_acceptance_rechecks_corrupt_committed_project_object(tmp_path: Path) -> None:
    state, owner, worker, _grant, lease, _manifest, first, checkpoint, receipt_digest = (
        _checkpoint_with_receipt(tmp_path)
    )
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": checkpoint["operation_id"],
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
    with pytest.raises(Conflict, match="project storage integrity"):
        ResultAcceptanceStore(state).record(
            owner,
            checkpoint["id"],
            "accept",
            canonical_digest(_request(checkpoint, receipt_digest)),
            _request(checkpoint, receipt_digest),
        )


def test_result_acceptance_can_be_retracted_after_project_object_corruption(tmp_path: Path) -> None:
    state, owner, worker, _grant, lease, _manifest, first, checkpoint, receipt_digest = (
        _checkpoint_with_receipt(tmp_path)
    )
    store = ResultAcceptanceStore(state)
    accepted = _request(checkpoint, receipt_digest)
    store.record(owner, checkpoint["id"], "accept", canonical_digest(accepted), accepted)
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": checkpoint["operation_id"],
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

    retract = _request(checkpoint, receipt_digest, action="retract", revision=1)
    _, retracted = store.record(
        owner, checkpoint["id"], "retract", canonical_digest(retract), retract
    )
    assert retracted["revision"] == 2
    assert retracted["action"] == "retract"


def test_result_acceptance_replays_conflicts_and_retracts_once(tmp_path: Path) -> None:
    state, owner, _, _, _, _, _, checkpoint, receipt_digest = _checkpoint_with_receipt(tmp_path)
    store = ResultAcceptanceStore(state)
    body = _request(checkpoint, receipt_digest)
    assert store.record(owner, checkpoint["id"], "accept", canonical_digest(body), body)[0] == 201
    assert store.record(owner, checkpoint["id"], "accept", canonical_digest(body), body)[0] == 201
    changed = body | {"rationale": "Changed."}
    with pytest.raises(Conflict):
        store.record(owner, checkpoint["id"], "accept", canonical_digest(changed), changed)
    retract = _request(checkpoint, receipt_digest, action="retract", revision=1)
    _, retracted = store.record(
        owner, checkpoint["id"], "retract", canonical_digest(retract), retract
    )
    assert retracted["revision"] == 2 and retracted["action"] == "retract"
    with pytest.raises(Conflict):
        store.record(owner, checkpoint["id"], "again", canonical_digest(retract), retract)
    with pytest.raises(Conflict):
        store.record(
            owner,
            checkpoint["id"],
            "new-accept",
            canonical_digest(_request(checkpoint, receipt_digest, revision=2)),
            _request(checkpoint, receipt_digest, revision=2),
        )


def test_result_acceptance_rolls_back_record_event_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, owner, _, _, _, _, _, checkpoint, receipt_digest = _checkpoint_with_receipt(tmp_path)
    store = ResultAcceptanceStore(state)
    body = _request(checkpoint, receipt_digest)
    monkeypatch.setattr(
        store, "_event", lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("event"))
    )
    with pytest.raises(sqlite3.OperationalError, match="event"):
        store.record(owner, checkpoint["id"], "atomic", canonical_digest(body), body)
    with closing(_connect(state / "tessera.sqlite3")) as connection:
        assert (
            connection.execute("SELECT count(*) FROM execution_result_acceptances").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE idempotency_key='atomic'"
            ).fetchone()[0]
            == 0
        )


def test_v19_to_v20_result_acceptance_migration_is_empty_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tessera_control.migrations import MIGRATIONS

    state, _owner, _worker, _grant, _lease, manifest, first, checkpoint, _receipt_digest = (
        _checkpoint_with_receipt(tmp_path)
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE execution_result_acceptances")
        connection.execute("UPDATE schema_meta SET schema_version=19")
    assert migrate_state(state) == 31
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM execution_result_acceptances").fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT id FROM execution_result_checkpoints WHERE id=?", (checkpoint["id"],)
        ).fetchone()
        assert connection.execute(
            "SELECT id FROM artifact_manifests WHERE id=?", (manifest["id"],)
        ).fetchone()
        assert connection.execute("SELECT id FROM artifacts WHERE id=?", (first["id"],)).fetchone()
        connection.execute("DROP TABLE execution_result_acceptances")
        connection.execute("UPDATE schema_meta SET schema_version=19")

    def fail(connection):
        from tessera_control.result_acceptances import create_result_acceptance_schema

        create_result_acceptance_schema(connection)
        raise RuntimeError("result acceptance migration fault")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 19, fail)
        with pytest.raises(RuntimeError, match="result acceptance"):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 19
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='execution_result_acceptances'"
            ).fetchone()
            is None
        )
