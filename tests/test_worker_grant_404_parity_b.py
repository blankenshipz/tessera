"""Another worker's grant is answered exactly like a missing grant on the shared worker routes.

The harness session-binding routes and the Kubernetes dispatch claim read their grant
id from a record the caller already holds (an operation, binding or dispatch), not
from the request. A grant can only belong to someone else there if those records
and the grant disagree, which is simulated by rewriting the grant row directly.
Either way the caller must get the same 404 and body as for a grant that does not
exist, and the refusal must write nothing: no event, audit row, claim or
idempotency record (so the same key still works once authority is restored).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from test_execution import _owner, _worker
from test_kubernetes_dispatch import _dispatch
from test_kubernetes_execution import _admitted_package, _cluster
from test_opencode_message_delivery import _delivery_fixture

from tessera_control.api import create_app
from tessera_worker.kubernetes_contracts import cluster_identity_digest

MISSING = {"detail": "Execution grant was not found."}
WORKER = "Bearer worker-token"


def _database(state: Path) -> Path:
    return state / "tessera.sqlite3"


def _dump(state: Path) -> dict[str, list[tuple]]:
    """Every row of every table, so any write by a refused request is visible."""
    with sqlite3.connect(_database(state)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
            for table in tables
        }


@contextmanager
def _grant_owned_by_other_worker(state: Path, grant_id: str, other_id: str) -> Iterator[None]:
    with sqlite3.connect(_database(state)) as connection:
        (original,) = connection.execute(
            "SELECT worker_principal_id FROM execution_grants WHERE id=?", (grant_id,)
        ).fetchone()
        connection.execute(
            "UPDATE execution_grants SET worker_principal_id=? WHERE id=?", (other_id, grant_id)
        )
    try:
        yield
    finally:
        with sqlite3.connect(_database(state)) as connection:
            connection.execute(
                "UPDATE execution_grants SET worker_principal_id=? WHERE id=?",
                (original, grant_id),
            )


@contextmanager
def _grant_missing(state: Path, grant_id: str) -> Iterator[None]:
    # Foreign keys are not enforced on this plain connection: the records that name
    # the grant keep pointing at an id that no longer exists.
    with sqlite3.connect(_database(state)) as connection:
        connection.execute(
            "UPDATE execution_grants SET id=? WHERE id=?", (grant_id + "-moved", grant_id)
        )
    try:
        yield
    finally:
        with sqlite3.connect(_database(state)) as connection:
            connection.execute(
                "UPDATE execution_grants SET id=? WHERE id=?", (grant_id, grant_id + "-moved")
            )


def _other_worker(state: Path, grant_id: str) -> str:
    with sqlite3.connect(_database(state)) as connection:
        (project_id,) = connection.execute(
            "SELECT project_id FROM execution_grants WHERE id=?", (grant_id,)
        ).fetchone()
    return str(_worker(state, _owner(state), project_id, "other-worker-token")["id"])


def _refusals(state: Path, grant_id: str, other_id: str, post) -> None:
    """The foreign and the missing grant answer identically and write nothing."""
    before = _dump(state)
    with _grant_owned_by_other_worker(state, grant_id, other_id):
        rewritten = _dump(state)
        foreign = post("foreign")
        assert _dump(state) == rewritten
    with _grant_missing(state, grant_id):
        rewritten = _dump(state)
        missing = post("missing")
        assert _dump(state) == rewritten
    assert _dump(state) == before
    assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json())
    assert (foreign.status_code, foreign.json()) == (404, MISSING)


def test_harness_session_binding_create_answers_foreign_grant_as_missing(tmp_path: Path) -> None:
    _response, _caller, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    grant_id = package.package["grant_id"]
    other_id = _other_worker(state, grant_id)
    client = TestClient(create_app(state))
    body = {
        "operation_id": package.binding.operation_id,
        "harness_instance_id": "harness-1",
        "upstream_session_id": "session-1",
        "adapter_profile": "opencode-v1.18.30-context-only",
        "provider_disabled": True,
    }

    def post(key: str):
        return client.post(
            "/worker/operation-harness-session-bindings",
            json=body,
            headers={"Authorization": WORKER, "Idempotency-Key": key},
        )

    _refusals(state, grant_id, other_id, post)
    # Nothing replayable was stored: the refused key binds once authority is current.
    created = post("foreign")
    assert created.status_code == 201, created.text
    assert created.json()["grant_id"] == grant_id


def test_harness_delivery_claim_and_authorize_answer_foreign_grant_as_missing(
    tmp_path: Path,
) -> None:
    state, interactions, owner, _caller, payload = _delivery_fixture(tmp_path)
    _, queued = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    delivery = queued["delivery"]
    grant_id = delivery["grant_id"]
    other_id = _other_worker(state, grant_id)
    client = TestClient(create_app(state))
    claim_path = f"/worker/interaction-deliveries/{delivery['delivery_id']}/claim"

    def claim(key: str):
        return client.post(
            claim_path,
            json={"expected_epoch": delivery["epoch"]},
            headers={"Authorization": WORKER, "Idempotency-Key": key},
        )

    _refusals(state, grant_id, other_id, claim)
    claimed = claim("foreign")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["delivery"]["state"] == "delivered"

    def authorize(key: str):
        return client.post(
            f"/worker/interaction-deliveries/{delivery['delivery_id']}/authorize",
            json={
                "expected_epoch": delivery["epoch"],
                "expected_revision": 2,
                "context_digest": delivery["context_digest"],
            },
            headers={"Authorization": WORKER, "Idempotency-Key": key},
        )

    _refusals(state, grant_id, other_id, authorize)
    # A claim replay rechecks authority: refused the same way, still writing nothing.
    _refusals(state, grant_id, other_id, lambda _key: claim("foreign"))
    authorized = authorize("foreign")
    assert (authorized.status_code, authorized.json()) == (200, {"authorized": True})


def test_kubernetes_dispatch_claim_answers_foreign_grant_as_missing(tmp_path: Path) -> None:
    response, _caller, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client = TestClient(create_app(state))
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    grant_id = package.package["grant_id"]
    other_id = _other_worker(state, grant_id)
    body = {"target_digest": cluster_identity_digest(cluster), "profile_version": cluster.profile}

    def claim(path: str):
        def post(key: str):
            return client.post(
                path, json=body, headers={"Authorization": WORKER, "Idempotency-Key": key}
            )

        return post

    by_id = claim(f"/worker/kubernetes-dispatches/{dispatch['id']}/claim")
    _refusals(state, grant_id, other_id, by_id)
    _refusals(state, grant_id, other_id, claim("/worker/kubernetes-dispatches/next/claim"))

    # The refused key stored no replayable response and claimed nothing.
    claimed = by_id("foreign")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["dispatch"]["state"] == "claimed"
    # A replay of the stored claim rechecks the same guard and is refused identically.
    _refusals(state, grant_id, other_id, lambda _key: by_id("foreign"))
    assert by_id("foreign").json() == claimed.json()
