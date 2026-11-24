"""Another worker's execution grant is answered exactly as a grant that does not exist.

Input delivery, artifact uploads and the worker credential-access route reach the grant
through a record the calling worker holds (its execution lease, or its credential access
lease). When that grant now belongs to another worker, the answer is the same 404 status
and body as a grant that does not exist, and nothing at all is written: no event, receipt,
idempotency record or denial audit row.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from test_credential_access_leases import _create
from test_execution import _worker
from test_input_delivery import _delivery

from tessera_control.api import create_app

MISSING = {"detail": "Execution grant was not found."}
WORKER = {"Authorization": "Bearer worker-token"}


def _database(state: Path) -> Path:
    return state / "tessera.sqlite3"


def _dump(state: Path) -> list[str]:
    """Every row of every table, so any write by a refused request shows up."""
    with sqlite3.connect(_database(state)) as connection:
        return list(connection.iterdump())


def _raw(state: Path, sql: str, params: tuple[object, ...]) -> None:
    # A plain connection (no foreign-key enforcement) can point a record at a grant
    # that does not exist, which no API can do.
    connection = sqlite3.connect(_database(state))
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _reassign(state: Path, owner: dict[str, object], grant: dict[str, object]) -> None:
    other = _worker(state, owner, grant["project_id"], "other-worker")
    _raw(
        state,
        "UPDATE execution_grants SET worker_principal_id=? WHERE id=?",
        (other["id"], grant["id"]),
    )


def _refused_without_writes(state: Path, call) -> tuple[int, dict[str, object]]:
    before = _dump(state)
    response = call()
    assert _dump(state) == before, "a refused request wrote state"
    return response.status_code, response.json()


def _upload_body(lease: dict[str, object], content: bytes = b"result") -> dict[str, object]:
    return {
        "expected_epoch": lease["epoch"],
        "operation_id": hashlib.sha256(f"tessera-cpu-v1:{lease['id']}".encode()).hexdigest()[:32],
        "relative_path": "result.txt",
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }


def _begin_body(lease: dict[str, object], content: bytes = b"result") -> dict[str, object]:
    body = _upload_body(lease, content)
    body.pop("content_base64")
    return body | {"size": len(content)}


def _lease_route_parity(tmp_path: Path, post) -> None:
    """``post(client, lease, key)`` calls a lease-scoped worker route."""
    state, _, owner, _, grant, lease = _delivery(tmp_path)
    client = TestClient(create_app(state))
    own = post(client, lease, "own-grant")
    assert own.status_code in {200, 201}, own.text

    # The worker's own lease names a grant that does not exist.
    _raw(state, "UPDATE execution_leases SET grant_id=? WHERE id=?", ("grant_missing", lease["id"]))
    missing = _refused_without_writes(state, lambda: post(client, lease, "missing-grant"))
    _raw(state, "UPDATE execution_leases SET grant_id=? WHERE id=?", (grant["id"], lease["id"]))

    # The same lease, whose grant now belongs to another worker.
    _reassign(state, owner, grant)
    foreign = _refused_without_writes(state, lambda: post(client, lease, "foreign-grant"))
    assert missing == (404, MISSING)
    assert foreign == missing


def test_input_delivery_answers_another_workers_grant_as_missing(tmp_path: Path) -> None:
    def post(client, lease, key):
        return client.post(
            f"/worker/leases/{lease['id']}/inputs",
            json={"expected_epoch": lease["epoch"]},
            headers=WORKER | {"Idempotency-Key": key},
        )

    _lease_route_parity(tmp_path, post)


def test_input_chunk_answers_another_workers_grant_as_missing(tmp_path: Path) -> None:
    state, _, owner, _, grant, lease = _delivery(tmp_path)
    client = TestClient(create_app(state))

    def post():
        return client.post(
            f"/worker/leases/{lease['id']}/inputs/chunks/0",
            json={"expected_epoch": lease["epoch"]},
            headers=WORKER,
        )

    _raw(state, "UPDATE execution_leases SET grant_id=? WHERE id=?", ("grant_missing", lease["id"]))
    missing = _refused_without_writes(state, post)
    _raw(state, "UPDATE execution_leases SET grant_id=? WHERE id=?", (grant["id"], lease["id"]))
    _reassign(state, owner, grant)
    assert missing == (404, MISSING)
    assert _refused_without_writes(state, post) == missing


def test_artifact_upload_answers_another_workers_grant_as_missing(tmp_path: Path) -> None:
    def post(client, lease, key):
        return client.post(
            f"/worker/leases/{lease['id']}/artifacts",
            json=_upload_body(lease, key.encode()),
            headers=WORKER | {"Idempotency-Key": key},
        )

    _lease_route_parity(tmp_path, post)


def test_artifact_begin_upload_answers_another_workers_grant_as_missing(tmp_path: Path) -> None:
    def post(client, lease, key):
        return client.post(
            f"/worker/leases/{lease['id']}/artifact-uploads",
            json=_begin_body(lease, key.encode()),
            headers=WORKER | {"Idempotency-Key": key},
        )

    _lease_route_parity(tmp_path, post)


def test_credential_access_answers_another_workers_grant_as_missing(tmp_path: Path) -> None:
    state, _, owner, _, grant, lease, access, _ = _create(tmp_path)
    app = create_app(state)
    backend = app.state.fake_secret_backend
    client = TestClient(app)
    path = f"/worker/credential-access-leases/{access['id']}/access"

    def post():
        return client.post(path, headers=WORKER)

    def point_at(grant_id: str) -> None:
        # The credential access lease and its parent lease name the grant together.
        _raw(
            state,
            "UPDATE credential_access_leases SET grant_id=? WHERE id=?",
            (grant_id, access["id"]),
        )
        _raw(state, "UPDATE execution_leases SET grant_id=? WHERE id=?", (grant_id, lease["id"]))

    own = post()
    assert own.status_code == 200, own.text
    issued = list(backend.access_log)

    point_at("grant_missing")
    missing = _refused_without_writes(state, post)
    point_at(grant["id"])

    _reassign(state, owner, grant)
    foreign = _refused_without_writes(state, post)
    assert missing == (404, MISSING)
    assert foreign == missing
    # No handle was issued (or denied) for either refusal.
    assert backend.access_log == issued
