# ruff: noqa: E501
"""Context-only message delivery control and worker fences; no live harness or provider."""

# fmt: off

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_execution import _admitted_package

from tessera_control.api import create_app
from tessera_control.interaction import InteractionStore
from tessera_control.store import Conflict, Forbidden, Store, _connect


def _owner(state: Path) -> dict[str, object]:
    token = (state / "owner.header").read_text().strip().split(": ", 1)[1].removeprefix("Bearer ")
    return Store(state).authenticate(token)


def _delivery_fixture(tmp_path: Path):
    response, worker, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    owner, interactions = _owner(state), InteractionStore(Store(state))
    project_id, task_id = package.package["project_id"], package.package["task_id"]
    _, thread = interactions.create_thread(owner, "thread", project_id, "Harness", task_id)
    with _connect(state / "tessera.sqlite3") as connection:
        attempt = connection.execute(
            "SELECT revision FROM research_experiment_admitted_attempts WHERE id=?",
            (response["admitted_attempt_id"],),
        ).fetchone()
    assert attempt is not None
    payload = {
        "thread_id": thread["id"], "admitted_attempt_id": response["admitted_attempt_id"],
        "expected_attempt_revision": attempt["revision"], "context_revision": 1,
        "expected_epoch": package.binding.epoch, "worker_principal_id": worker["id"],
        "harness_instance_id": "harness-1", "upstream_session_id": "session-1",
        "intent": "direction", "body": "Use this exact context only.",
    }
    return state, interactions, owner, worker, payload


def test_human_enqueue_worker_claim_ack_history_and_legacy_chat(tmp_path: Path) -> None:
    _state, interactions, owner, worker, payload = _delivery_fixture(tmp_path)
    _, legacy = interactions.send_message(owner, "legacy", payload["thread_id"], "question", "Legacy chat")
    status, created = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    assert status == 201 and created["delivery"]["state"] == "queued"
    delivery = created["delivery"]
    with pytest.raises(Forbidden):
        interactions.enqueue_harness_delivery(worker, "worker-enqueue", **payload)
    status, claim = interactions.claim_harness_delivery(
        worker, "claim", delivery["delivery_id"], expected_epoch=payload["expected_epoch"]
    )
    assert status == 200 and claim["delivery"]["state"] == "delivered"
    assert interactions.claim_harness_delivery(
        worker, "claim", delivery["delivery_id"], expected_epoch=payload["expected_epoch"]
    ) == (status, claim)
    with pytest.raises(Conflict, match="context digest"):
        interactions.acknowledge_harness_delivery(
            worker, "bad-digest", delivery["delivery_id"], expected_revision=2,
            context_digest="f" * 64, upstream_message_id="message-1", upstream_readback_digest="a" * 64,
        )
    _, acknowledged = interactions.acknowledge_harness_delivery(
        worker, "ack", delivery["delivery_id"], expected_revision=2,
        context_digest=delivery["context_digest"], upstream_message_id="message-1",
        upstream_readback_digest="b" * 64,
    )
    assert acknowledged["state"] == "acknowledged" and acknowledged["upstream_message_id"] == "message-1"
    messages = interactions.list_messages(owner, payload["thread_id"])
    assert next(item for item in messages if item["id"] == legacy["id"])["delivery_legacy"] is True
    delivered = next(item for item in messages if item["id"] == created["id"])["delivery"]
    assert delivered["state"] == "acknowledged" and delivered["upstream_session_id"] == "session-1"


def test_attempt_epoch_fences_and_control_only_cancellation(tmp_path: Path) -> None:
    state, interactions, owner, worker, payload = _delivery_fixture(tmp_path)
    _, created = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    delivery = created["delivery"]
    with pytest.raises(Conflict, match="claimable"):
        interactions.claim_harness_delivery(
            worker, "wrong-epoch", delivery["delivery_id"], expected_epoch=payload["expected_epoch"] + 1
        )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE research_experiment_admitted_attempts SET revision=revision+1 WHERE id=?",
            (payload["admitted_attempt_id"],),
        )
    with pytest.raises(Conflict, match="authority"):
        interactions.claim_harness_delivery(
            worker, "stale-attempt", delivery["delivery_id"], expected_epoch=payload["expected_epoch"]
        )
    cancelled = interactions.cancel_harness_delivery(
        owner, "cancel", delivery["delivery_id"], expected_revision=1
    )
    assert cancelled[1]["state"] == "cancelled"
    with pytest.raises(Conflict, match="claimable"):
        interactions.claim_harness_delivery(
            worker, "claim-after-cancel", delivery["delivery_id"], expected_epoch=payload["expected_epoch"]
        )
    with _connect(state / "tessera.sqlite3") as connection:
        states = connection.execute(
            "SELECT state FROM interaction_message_delivery_states WHERE delivery_id=? ORDER BY sequence",
            (delivery["delivery_id"],),
        ).fetchall()
    assert [row["state"] for row in states] == ["queued", "cancelled"]


def test_worker_routes_require_worker_audience_and_preserve_context_digest(tmp_path: Path) -> None:
    state, _interactions, _owner_actor, _worker, payload = _delivery_fixture(tmp_path)
    owner_header = (state / "owner.header").read_text().strip().split(": ", 1)[1]
    client = TestClient(create_app(state))
    created = client.post(
        "/interaction/deliveries", json=payload,
        headers={"Authorization": owner_header, "Idempotency-Key": "api-enqueue"},
    )
    assert created.status_code == 201
    delivery = created.json()["delivery"]
    denied = client.post(
        f"/worker/interaction-deliveries/{delivery['delivery_id']}/claim",
        json={"expected_epoch": payload["expected_epoch"]},
        headers={"Authorization": owner_header, "Idempotency-Key": "human-claim"},
    )
    assert denied.status_code == 401
    claimed = client.post(
        f"/worker/interaction-deliveries/{delivery['delivery_id']}/claim",
        json={"expected_epoch": payload["expected_epoch"]},
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "worker-claim"},
    )
    assert claimed.status_code == 200 and claimed.json()["delivery"]["context_digest"] == delivery["context_digest"]
