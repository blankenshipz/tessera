"""Bounded worker registration/lifecycle tests; no provider job is submitted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_execution import _grant_payload, _setup, _worker

from tessera_control.api import create_app
from tessera_control.store import Conflict, Forbidden, NotFound, Store, _connect, canonical_digest
from tessera_control.workers import WorkerRegistryStore


def _registration(worker: dict[str, object]) -> dict[str, object]:
    return {
        "worker_principal_id": worker["id"],
        "approved_build_identifier": "synthetic-supervisor-v1",
        "approved_build_digest": "b" * 64,
        "trust_class": "test",
        "region": "local",
    }


def _grant(state, execution, owner, worker, project, task):
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    return execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )[1]


def test_registration_owner_scope_idempotency_and_non_disclosure(tmp_path) -> None:
    state, _, owner, worker, project, _ = _setup(tmp_path)
    # The fixture helper registers its worker so the legacy execution tests retain their setup.
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "DELETE FROM worker_registry WHERE worker_principal_id=?", (worker["id"],)
        )
    workers = WorkerRegistryStore(state)
    payload = _registration(worker)
    created = workers.register_worker(
        owner, project["id"], "register", canonical_digest(payload), payload
    )
    assert created[0] == 201
    assert (
        workers.register_worker(
            owner, project["id"], "register", canonical_digest(payload), payload
        )
        == created
    )
    with pytest.raises(Conflict, match="different request"):
        workers.register_worker(owner, project["id"], "register", "different", payload)
    _, other = Store(state).create_project(owner, "other", "other", {"name": "Other"})
    with pytest.raises(Forbidden, match="bound"):
        workers.register_worker(
            owner, other["id"], "wrong-project", canonical_digest(payload), payload
        )
    with pytest.raises(Forbidden):
        workers.register_worker(
            worker, project["id"], "non-owner", canonical_digest(payload), payload
        )
    foreign_worker = _worker(state, owner, other["id"], "foreign")
    with pytest.raises(NotFound):
        workers.lifecycle_worker(
            owner, "not-a-worker", "drain", "hidden", "hidden", {"expected_revision": 1}
        )
    assert foreign_worker["project_id"] == other["id"]


def test_ready_lifecycle_and_retirement_revoke_lease_authority(tmp_path) -> None:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    workers = WorkerRegistryStore(state)
    grant = _grant(state, execution, owner, worker, project, task)
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    drained = workers.lifecycle_worker(
        owner, "registered-worker-token", "drain", "drain", "drain", {"expected_revision": 1}
    )[1]
    assert drained["lifecycle_state"] == "draining"
    with pytest.raises(Conflict, match="not ready"):
        execution.renew_execution_lease(worker, lease["id"], "renew", "renew", lease["epoch"])
    # Existing observations are still attributable while the current credential is valid.
    observation = {
        "expected_epoch": lease["epoch"],
        "observed_state": "unknown",
        "observed_at": datetime.now(UTC).isoformat(),
        "termination_confirmed": False,
    }
    assert (
        execution.report_execution_observation(
            worker, lease["id"], "observation", "obs", observation
        )[0]
        == 201
    )
    quarantined = workers.lifecycle_worker(
        owner,
        drained["id"],
        "quarantine",
        "quarantine",
        "quarantine",
        {"expected_revision": drained["revision"]},
    )[1]
    assert quarantined["lifecycle_state"] == "quarantined"
    retired = workers.lifecycle_worker(
        owner,
        quarantined["id"],
        "retire",
        "retire",
        "retire",
        {"expected_revision": quarantined["revision"]},
    )[1]
    assert retired["lifecycle_state"] == "retired"
    with pytest.raises(Forbidden):
        Store(state).authenticate_worker("worker-token")
    with _connect(state / "tessera.sqlite3") as connection:
        status = connection.execute(
            "SELECT status FROM execution_grants WHERE id=?", (grant["id"],)
        ).fetchone()[0]
    assert status == "active"


def test_unregistered_stale_and_status_heartbeat_are_fenced(tmp_path) -> None:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    workers = WorkerRegistryStore(state)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "DELETE FROM worker_registry WHERE worker_principal_id=?", (worker["id"],)
        )
    grant = _grant(state, execution, owner, worker, project, task)
    with pytest.raises(Forbidden, match="not registered"):
        execution.acquire_execution_lease(worker, grant["id"], "unregistered", "unregistered")
    payload = _registration(worker)
    _, registered = workers.register_worker(
        owner, project["id"], "register", canonical_digest(payload), payload
    )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE worker_registry SET last_seen_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(), registered["id"]),
        )
    with pytest.raises(Conflict, match="disconnected"):
        execution.acquire_execution_lease(worker, grant["id"], "stale", "stale")
    assert workers.heartbeat(worker, "heartbeat", "heartbeat", {})[0] == 200
    assert workers.worker_status(worker)["lifecycle_state"] == "ready"
    assert execution.acquire_execution_lease(worker, grant["id"], "ready", "ready")[0] == 201


def test_project_worker_discovery_is_safe_metadata_only_and_reports_fences(tmp_path) -> None:
    state, _, owner, worker, project, _ = _setup(tmp_path)
    client = TestClient(create_app(state))
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    headers = {"Authorization": f"Bearer {owner_token}"}

    response = client.get(f"/projects/{project['id']}/workers", headers=headers)
    assert response.status_code == 200
    discovery = response.json()["workers"]
    assert len(discovery) == 1
    record = discovery[0]
    assert record["id"] == "registered-worker-token"
    assert record["project_id"] == project["id"]
    assert record["approved_metadata"] == {
        "build_identifier": "synthetic-test-worker",
        "build_digest": "a" * 64,
        "region": "local",
    }
    assert record["freshness"]["status"] == "fresh"
    assert record["lifecycle"]["state"] == "ready"
    assert record["capabilities"] == {"status": "not_probed", "probed": False}
    assert record["trust_zone"] == {"status": "declared_unverified", "trust_class": "test"}
    assert record["lease_admission"] == {
        "registration_is_not_scheduling": True,
        "status": "requires_separate_active_grant",
        "fence_reason": "registration_only",
    }
    serialized = str(record).lower()
    for forbidden in ("credential_id", "worker_principal_id", "kubeconfig", "endpoint", "secret"):
        assert forbidden not in serialized

    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE worker_registry SET last_seen_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(), record["id"]),
        )
    stale = client.get(f"/projects/{project['id']}/workers", headers=headers).json()["workers"][0]
    assert stale["freshness"]["status"] == "stale"
    assert stale["lease_admission"] == {
        "registration_is_not_scheduling": True,
        "status": "fenced",
        "fence_reason": "heartbeat_stale",
    }

    _, other = Store(state).create_project(owner, "other", "other", {"name": "Other"})
    assert client.get(f"/projects/{other['id']}/workers", headers=headers).json() == {"workers": []}

    retired = WorkerRegistryStore(state).lifecycle_worker(
        owner,
        record["id"],
        "retire",
        "retire-discovery-worker",
        "retire-discovery-worker",
        {"expected_revision": 1},
    )[1]
    assert retired["lifecycle_state"] == "retired"
    revoked = client.get(f"/projects/{project['id']}/workers", headers=headers).json()["workers"][0]
    assert revoked["lifecycle"]["credential_status"] == "revoked"
    assert revoked["lease_admission"]["status"] == "fenced"
    assert revoked["lease_admission"]["fence_reason"] == "credential_revoked"


def test_worker_discovery_fences_expired_credentials(tmp_path) -> None:
    state, _, _, worker, project, _ = _setup(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE credentials SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), worker["credential_id"]),
        )
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    response = TestClient(create_app(state)).get(
        f"/projects/{project['id']}/workers", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert response.status_code == 200
    record = response.json()["workers"][0]
    assert record["lifecycle"]["credential_status"] == "expired"
    assert record["lease_admission"] == {
        "registration_is_not_scheduling": True,
        "status": "fenced",
        "fence_reason": "credential_expired",
    }


def test_registration_is_metadata_not_scheduling_and_worker_package_reads_are_isolated(
    tmp_path,
) -> None:
    """Enrollment neither schedules work nor lets another worker read its package."""
    state, execution, owner, worker, project, task = _setup(tmp_path)
    workers = WorkerRegistryStore(state)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "DELETE FROM worker_registry WHERE worker_principal_id=?", (worker["id"],)
        )
    registered = workers.register_worker(
        owner,
        project["id"],
        "register-isolated-worker",
        canonical_digest(_registration(worker)),
        _registration(worker),
    )[1]
    assert registered["approved_metadata_only"] is True
    assert registered["build_attestation_verified"] is False
    assert registered["capabilities_probed"] is False
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM execution_leases").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 0

    grant = _grant(state, execution, owner, worker, project, task)
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "owner-lease", "owner-lease")
    _, other_project = Store(state).create_project(
        owner, "other-project", "other-project", {"name": "Other"}
    )
    _worker(state, owner, other_project["id"], "foreign-worker-token")
    client = TestClient(create_app(state))
    foreign = client.post(
        f"/worker/leases/{lease['id']}/package",
        json={"expected_epoch": lease["epoch"]},
        headers={"Authorization": "Bearer foreign-worker-token", "Idempotency-Key": "foreign-read"},
    )
    assert foreign.status_code == 404

    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE worker_registry SET last_seen_at=? WHERE worker_principal_id=?",
            ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(), worker["id"]),
        )
    stale = client.post(
        "/worker/leases/renew",
        json={"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "stale-renew"},
    )
    assert stale.status_code == 409

    retired = workers.lifecycle_worker(
        owner,
        registered["id"],
        "retire",
        "retire-isolated-worker",
        "retire-isolated-worker",
        {"expected_revision": registered["revision"]},
    )[1]
    assert retired["lifecycle_state"] == "retired"
    revoked = client.post(
        f"/worker/leases/{lease['id']}/package",
        json={"expected_epoch": lease["epoch"]},
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "revoked-read"},
    )
    assert revoked.status_code == 401
