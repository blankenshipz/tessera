"""Synthetic remote-operation lifecycle, reconnect, and held-recovery coverage."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_dispatch import _owner_headers
from test_kubernetes_execution import _admitted_package

from tessera_control.api import create_app
from tessera_control.migrations import MIGRATIONS, migrate_v42_to_v43
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import canonical_digest, migrate_state


def _worker_headers(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer worker-token", "Idempotency-Key": key}


def _v42_operation_state(tmp_path: Path) -> tuple[Path, dict[str, object], object]:
    response, _worker, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    database = state / "tessera.sqlite3"
    with sqlite3.connect(database) as connection:
        authority = connection.execute(
            """SELECT operation_id,grant_id,lease_id,worker_principal_id,epoch,
            experiment_attempt_id FROM external_operations WHERE operation_id=?""",
            (package.operation_id,),
        ).fetchone()
        assert authority is not None
        connection.execute("DROP TABLE remote_workflow_checkpoints")
        connection.execute("DROP TABLE remote_workflow_runs")
        connection.execute("UPDATE schema_meta SET schema_version=42")
    return state, response, package


def test_remote_workflow_lifecycle_reuses_operation_identity_and_attempt_binding(
    tmp_path: Path,
) -> None:
    response, _worker, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    operation_id = package.operation_id
    lease_id = response["package"]["lease_id"]
    epoch = response["package"]["epoch"]
    client = TestClient(create_app(state))

    listed = client.get(
        f"/projects/{response['package']['project_id']}/remote-workflows",
        headers=_owner_headers(state),
    )
    assert listed.status_code == 200
    workflow = listed.json()["remote_workflows"]
    assert len(workflow) == 1
    assert workflow[0]["operation_id"] == operation_id
    assert workflow[0]["experiment_attempt_id"] == response["admitted_attempt_id"]
    assert workflow[0]["state"] == "prepared"
    assert workflow[0]["operation_desired_state"] == "admitted"

    heartbeat = {
        "lease_id": lease_id,
        "expected_epoch": epoch,
        "observed_state": "running",
    }
    first = client.post(
        f"/worker/remote-workflows/{operation_id}/heartbeat",
        json=heartbeat,
        headers=_worker_headers("remote-heartbeat"),
    )
    replay = client.post(
        f"/worker/remote-workflows/{operation_id}/heartbeat",
        json=heartbeat,
        headers=_worker_headers("remote-heartbeat"),
    )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()

    checkpoint = {
        "lease_id": lease_id,
        "expected_epoch": epoch,
        "sequence": 1,
        "checkpoint_cursor": "synthetic-checkpoint-1",
        "checkpoint_digest": "a" * 64,
    }
    assert (
        client.post(
            f"/worker/remote-workflows/{operation_id}/checkpoints",
            json=checkpoint,
            headers=_worker_headers("remote-checkpoint"),
        ).status_code
        == 201
    )
    stale = client.post(
        f"/worker/remote-workflows/{operation_id}/checkpoints",
        json=checkpoint | {"sequence": 3, "checkpoint_cursor": "synthetic-checkpoint-3"},
        headers=_worker_headers("remote-stale-checkpoint"),
    )
    assert stale.status_code == 409

    reconnected = client.post(
        f"/worker/remote-workflows/{operation_id}/reconnect",
        json={
            "lease_id": lease_id,
            "expected_epoch": epoch,
            "last_checkpoint_sequence": 1,
            "control_outage": True,
        },
        headers=_worker_headers("remote-reconnect"),
    )
    assert reconnected.status_code == 200
    assert reconnected.json()["state"] == "unknown"
    assert reconnected.json()["reconciliation_required"] is True

    observed = client.post(
        "/worker/observations",
        json={
            "lease_id": lease_id,
            "expected_epoch": epoch,
            "operation_id": operation_id,
            "observed_state": "unknown",
            "observed_at": response["operation"]["created_at"],
            "termination_confirmed": False,
        },
        headers=_worker_headers("remote-unknown-observation"),
    )
    assert observed.status_code == 201
    detail = client.get(f"/remote-workflows/{operation_id}", headers=_owner_headers(state))
    assert detail.status_code == 200
    assert detail.json()["last_checkpoint_sequence"] == 1
    assert detail.json()["state"] == "unknown"
    assert detail.json()["reconciliation_required"] is True


def test_remote_projection_uses_observed_time_not_cross_source_arrival_order(
    tmp_path: Path,
) -> None:
    response, _worker, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    client = TestClient(create_app(state))
    operation_id = package.operation_id
    lease_id = response["package"]["lease_id"]
    epoch = response["package"]["epoch"]
    base = datetime.fromisoformat(response["operation"]["created_at"])
    receipt_payload = {"synthetic": "remote-projection-order"}
    later_receipt = {
        "lease_id": lease_id,
        "expected_epoch": epoch,
        "provider_kind": "fake",
        "provider_version": "1",
        "provider_id": "remote-projection-provider",
        "receipt_payload": receipt_payload,
        "receipt_digest": canonical_digest(receipt_payload),
        "observed_state": "running",
        "observed_at": (base + timedelta(milliseconds=2)).isoformat(),
        "termination_confirmed": False,
    }
    assert (
        client.post(
            f"/worker/external-operations/{operation_id}/receipt",
            json=later_receipt,
            headers=_worker_headers("remote-later-receipt"),
        ).status_code
        == 201
    )
    delayed_worker = {
        "lease_id": lease_id,
        "expected_epoch": epoch,
        "operation_id": operation_id,
        "observed_state": "unknown",
        "observed_at": (base + timedelta(milliseconds=1)).isoformat(),
        "provider_id": later_receipt["provider_id"],
        "termination_confirmed": False,
    }
    assert (
        client.post(
            "/worker/observations",
            json=delayed_worker,
            headers=_worker_headers("remote-delayed-worker"),
        ).status_code
        == 201
    )

    projected = client.get(
        f"/remote-workflows/{operation_id}", headers=_owner_headers(state)
    ).json()
    assert projected["state"] == projected["last_observed_state"] == "running"
    assert projected["last_observed_at"] == later_receipt["observed_at"].replace("+00:00", "Z")
    assert projected["reconciliation"]["cursor"].startswith("receipt:")
    heartbeat = client.post(
        f"/worker/remote-workflows/{operation_id}/heartbeat",
        json={
            "lease_id": lease_id,
            "expected_epoch": epoch,
            "observed_state": "unknown",
        },
        headers=_worker_headers("remote-raw-heartbeat"),
    )
    assert heartbeat.status_code == 201
    assert heartbeat.json()["state"] == "running"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT observed_state,observed_at FROM execution_observations WHERE operation_id=?",
            (operation_id,),
        ).fetchall() == [("unknown", delayed_worker["observed_at"].replace("+00:00", "Z"))]
        event = connection.execute(
            "SELECT payload_json FROM events "
            "WHERE event_type='remote_workflow.heartbeat_recorded' ORDER BY cursor DESC LIMIT 1"
        ).fetchone()
        assert event is not None
        assert json.loads(event[0])["observed_state"] == "unknown"


def test_remote_projection_equal_time_contradiction_fails_closed_and_retains_samples(
    tmp_path: Path,
) -> None:
    response, _worker, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    client = TestClient(create_app(state))
    operation_id = package.operation_id
    lease_id = response["package"]["lease_id"]
    epoch = response["package"]["epoch"]
    observed_at = response["operation"]["created_at"]
    receipt_payload = {"synthetic": "remote-projection-contradiction"}
    receipt = {
        "lease_id": lease_id,
        "expected_epoch": epoch,
        "provider_kind": "fake",
        "provider_version": "1",
        "provider_id": "remote-contradiction-provider",
        "receipt_payload": receipt_payload,
        "receipt_digest": canonical_digest(receipt_payload),
        "observed_state": "exited",
        "observed_at": observed_at,
        "termination_confirmed": True,
    }
    assert (
        client.post(
            f"/worker/external-operations/{operation_id}/receipt",
            json=receipt,
            headers=_worker_headers("remote-confirmed-receipt"),
        ).status_code
        == 201
    )
    contradiction = {
        "lease_id": lease_id,
        "expected_epoch": epoch,
        "operation_id": operation_id,
        "observed_state": "unknown",
        "observed_at": observed_at,
        "provider_id": receipt["provider_id"],
        "termination_confirmed": False,
    }
    assert (
        client.post(
            "/worker/observations",
            json=contradiction,
            headers=_worker_headers("remote-equal-time-contradiction"),
        ).status_code
        == 201
    )

    projected = client.get(
        f"/remote-workflows/{operation_id}", headers=_owner_headers(state)
    ).json()
    assert projected["state"] == projected["last_observed_state"] == "unknown"
    assert projected["reconciliation_required"] is True
    assert projected["reconciliation"]["contradiction_state"] == "same_time_contradiction"
    assert len(projected["reconciliation"]["sources"]) == 2
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM execution_observations WHERE operation_id=?", (operation_id,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT observation_sequence FROM external_operation_receipts WHERE operation_id=?",
            (operation_id,),
        ).fetchone() == (1,)


def test_v42_to_v43_preserves_operation_evidence_without_inventing_remote_runs(
    tmp_path: Path,
) -> None:
    state, response, package = _v42_operation_state(tmp_path)
    database = state / "tessera.sqlite3"
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            """SELECT operation_id,grant_id,lease_id,worker_principal_id,epoch,
            experiment_attempt_id FROM external_operations WHERE operation_id=?""",
            (package.operation_id,),
        ).fetchone()
        assert before is not None

    assert migrate_state(state) == 43
    assert migrate_state(state) == 43
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            """SELECT operation_id,grant_id,lease_id,worker_principal_id,epoch,
            experiment_attempt_id FROM external_operations WHERE operation_id=?""",
            (package.operation_id,),
        ).fetchone()
        assert after == before
        assert connection.execute("SELECT count(*) FROM remote_workflow_runs").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM remote_workflow_checkpoints"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM events WHERE event_type LIKE 'remote_workflow.%'"
        ).fetchone() == (0,)

    client = TestClient(create_app(state))
    missing = client.get(f"/remote-workflows/{package.operation_id}", headers=_owner_headers(state))
    assert missing.status_code == 404
    assert response["package"]["lease_id"] == before[2]


def test_v42_to_v43_rollback_leaves_no_schema_or_remote_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _response, package = _v42_operation_state(tmp_path)
    database = state / "tessera.sqlite3"
    with sqlite3.connect(database) as connection:
        before = list(connection.iterdump())

    def fail_after_remote_workflow_schema(connection: sqlite3.Connection) -> None:
        migrate_v42_to_v43(connection)
        raise RuntimeError("injected remote workflow migration failure")

    monkeypatch.setitem(MIGRATIONS, 42, fail_after_remote_workflow_schema)
    with pytest.raises(RuntimeError, match="injected remote workflow"):
        migrate_state(state)
    with sqlite3.connect(database) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone() == (42,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name='remote_workflow_runs'"
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT operation_id FROM external_operations WHERE operation_id=?",
            (package.operation_id,),
        ).fetchone() == (package.operation_id,)


def test_remote_workflow_held_restore_preserves_records_and_blocks_new_evidence(
    tmp_path: Path,
) -> None:
    response, _worker, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    client = TestClient(create_app(state))
    operation_id = package.operation_id
    heartbeat = {
        "lease_id": response["package"]["lease_id"],
        "expected_epoch": response["package"]["epoch"],
        "observed_state": "running",
    }
    assert (
        client.post(
            f"/worker/remote-workflows/{operation_id}/heartbeat",
            json=heartbeat,
            headers=_worker_headers("held-remote-heartbeat"),
        ).status_code
        == 201
    )
    checkpoint = {
        "lease_id": response["package"]["lease_id"],
        "expected_epoch": response["package"]["epoch"],
        "sequence": 1,
        "checkpoint_cursor": "held-checkpoint-1",
        "checkpoint_digest": "b" * 64,
    }
    assert (
        client.post(
            f"/worker/remote-workflows/{operation_id}/checkpoints",
            json=checkpoint,
            headers=_worker_headers("held-remote-checkpoint"),
        ).status_code
        == 201
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        before_runs = connection.execute("SELECT * FROM remote_workflow_runs").fetchall()
        before_checkpoints = connection.execute(
            "SELECT * FROM remote_workflow_checkpoints"
        ).fetchall()

    backup = tmp_path / "backup"
    assert backup_state(state, backup)["schema_version"] == 43
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    with sqlite3.connect(restored / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT * FROM remote_workflow_runs").fetchall() == before_runs
        assert (
            connection.execute("SELECT * FROM remote_workflow_checkpoints").fetchall()
            == before_checkpoints
        )

    held = TestClient(create_app(restored))
    denied = held.post(
        f"/worker/remote-workflows/{operation_id}/heartbeat",
        json=heartbeat,
        headers=_worker_headers("held-remote-workflow"),
    )
    assert denied.status_code == 401
    detail = held.get(f"/remote-workflows/{operation_id}", headers=_owner_headers(restored))
    assert detail.status_code == 200
    assert detail.json()["last_checkpoint_cursor"] == checkpoint["checkpoint_cursor"]
    assert detail.json()["last_checkpoint_digest"] == checkpoint["checkpoint_digest"]
    with sqlite3.connect(restored / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT * FROM remote_workflow_runs").fetchall() == before_runs
        assert (
            connection.execute("SELECT * FROM remote_workflow_checkpoints").fetchall()
            == before_checkpoints
        )
