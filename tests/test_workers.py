"""Bounded worker registration/lifecycle tests; no provider job is submitted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_execution import _grant_payload, _setup, _worker

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
