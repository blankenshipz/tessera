from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import _grant_payload, _setup, _worker

from tessera_control.api import create_app
from tessera_control.input_delivery import InputDeliveryStore
from tessera_control.store import Conflict, Forbidden, NotFound, Store, _connect, canonical_digest
from tessera_worker.inputs import materialize_snapshot


def _delivery(tmp_path: Path):
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    return state, execution, owner, worker, grant, lease


def test_delivery_replay_is_metadata_only_and_revocation_fences(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    store = InputDeliveryStore(state)
    body = {"expected_epoch": lease["epoch"]}
    first = store.deliver_inputs(
        worker, lease["id"], "inputs", canonical_digest(body), lease["epoch"]
    )
    replay = store.deliver_inputs(
        worker, lease["id"], "inputs", canonical_digest(body), lease["epoch"]
    )
    assert first == replay and first[1]["snapshot"]["files"]
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='worker.inputs_release_authorized'"
            ).fetchone()[0]
            == 1
        )
        assert (
            "content_base64"
            not in connection.execute(
                "SELECT payload_json FROM events ORDER BY cursor DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert (
            "content_base64"
            not in connection.execute(
                "SELECT response_json FROM idempotency WHERE path LIKE '%/inputs'"
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE execution_grants SET status='cancel_requested', revision=revision+1 WHERE id=?",
            (grant["id"],),
        )
    with pytest.raises(Conflict):
        store.deliver_inputs(worker, lease["id"], "inputs", canonical_digest(body), lease["epoch"])


def test_delivery_denies_expiry_and_corruption(tmp_path: Path) -> None:
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    store = InputDeliveryStore(state)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (lease["id"],),
        )
    with pytest.raises(Conflict):
        store.deliver_inputs(worker, lease["id"], "expired", "expired", lease["epoch"])
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_leases SET expires_at=? WHERE id=?",
            (lease["expires_at"], lease["id"]),
        )
        connection.execute(
            "UPDATE snapshot_files SET content=x'00' WHERE snapshot_id="
            "(SELECT snapshot_id FROM execution_grants WHERE id=?)",
            (grant["id"],),
        )
    with pytest.raises(Conflict):
        store.deliver_inputs(worker, lease["id"], "corrupt", "corrupt", lease["epoch"])


def test_delivery_http_requires_worker_audience_and_returns_bundle(tmp_path: Path) -> None:
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    app = TestClient(create_app(state))
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    response = app.post(
        f"/worker/leases/{lease['id']}/inputs",
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "bad"},
        json={"expected_epoch": lease["epoch"]},
    )
    assert response.status_code == 401
    response = app.post(
        f"/worker/leases/{lease['id']}/inputs",
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "ok"},
        json={"expected_epoch": lease["epoch"]},
    )
    assert response.status_code == 201 and response.json()["snapshot"]["files"]
    bundle = response.json()
    inputs = materialize_snapshot(
        bundle,
        tmp_path,
        expected_snapshot_digest=grant["snapshot_digest"],
        expected_lease_id=lease["id"],
        expected_epoch=lease["epoch"],
        expected_worker_id=worker["id"],
    )
    assert (inputs.directory / "input.txt").read_bytes() == b"immutable input"
    for invalid in ({"expected_epoch": True}, {"expected_epoch": 1, "unexpected": 1}):
        response = app.post(
            f"/worker/leases/{lease['id']}/inputs",
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "invalid"},
            json=invalid,
        )
        assert response.status_code == 422


def test_delivery_event_failure_rolls_back_receipt(tmp_path, monkeypatch):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = InputDeliveryStore(state)

    def fail(*args):
        raise sqlite3.OperationalError("synthetic event failure")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail))
        with pytest.raises(sqlite3.OperationalError, match="event failure"):
            store.deliver_inputs(worker, lease["id"], "event", "event", lease["epoch"])
    with _connect(store.database_path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%/inputs'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='worker.inputs_release_authorized'"
            ).fetchone()[0]
            == 0
        )
    assert store.deliver_inputs(worker, lease["id"], "event", "event", lease["epoch"])[0] == 201


@pytest.mark.parametrize("revoked", ["release", "worker"])
def test_revoked_authority_denies_previously_successful_replay(tmp_path, revoked):
    state, _, _, worker, grant, lease = _delivery(tmp_path)
    store = InputDeliveryStore(state)
    store.deliver_inputs(worker, lease["id"], "once", "once", lease["epoch"])
    with _connect(store.database_path) as connection:
        if revoked == "release":
            connection.execute(
                "UPDATE snapshot_releases SET status='revoked' WHERE id=?",
                (grant["snapshot_release_id"],),
            )
        else:
            connection.execute(
                "UPDATE credentials SET revoked_at='2026-01-01' WHERE principal_id=?",
                (worker["id"],),
            )
    with pytest.raises(Forbidden):
        store.deliver_inputs(worker, lease["id"], "once", "once", lease["epoch"])


def test_other_worker_and_stale_epoch_cannot_receive_bytes(tmp_path):
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    store = InputDeliveryStore(state)
    other = _worker(state, owner, grant["project_id"], "other-worker")
    with pytest.raises(NotFound):
        store.deliver_inputs(other, lease["id"], "other", "other", lease["epoch"])
    with pytest.raises(Conflict):
        store.deliver_inputs(worker, lease["id"], "stale", "stale", lease["epoch"] + 1)
