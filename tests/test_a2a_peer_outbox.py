"""Ordinary peer transactional provenance; no model/provider or network effects."""

import json

import pytest

from tessera_control.a2a_store import A2AStore, PeerConflict, PeerNotFound


def envelope():
    return {
        "operation": "submit",
        "reference": "untrusted_context_reference",
        "authority": {
            "grant_id": "delegated_grant",
            "revision": 1,
            "epoch": 1,
            "binding": {"task_id": "control_task", "variant_id": "control_variant"},
        },
    }


ORIGIN = {
    "receiver_peer_id": "recipient_principal",
    "credential_id": "recipient_credential",
    "project_id": "project",
    "destination": "a2a:references",
}


def test_reference_outbox_is_causal_redacted_durable_and_replay_stable(tmp_path):
    store = A2AStore(tmp_path / "peer.sqlite")
    task = store.send("tenant", "sender", "submit_message", envelope(), provenance_origin=ORIGIN)
    result = {**envelope(), "operation": "result", "reference": "untrusted_result_reference"}
    task = store.send(
        "tenant", "sender", "result_message", result, task["id"], provenance_origin=ORIGIN
    )
    items = store.provenance_outbox("tenant", "sender", task["id"])
    assert len(items) == 4
    assert all(item["delivery_state"] == "pending" for item in items)
    assert all(item["envelope"]["record"]["model_id"] is None for item in items)
    messages = [
        item["envelope"]["record"]
        for item in items
        if item["envelope"]["record"]["kind"] == "message"
    ]
    assert messages[-1]["causation_id"] == "submit_message"
    assert messages[-1]["remote"]["message_id"] == "result_message"
    assert items[-1]["envelope"]["record"]["correlation_id"] == task["artifacts"][0]["artifactId"]
    assert task["artifacts"][0]["parts"][0]["data"]["verified"] is False
    assert "untrusted_result_reference" not in json.dumps(items)
    restarted = A2AStore(store.path)
    assert (
        restarted.send(
            "tenant", "sender", "result_message", result, task["id"], provenance_origin=ORIGIN
        )
        == task
    )
    assert restarted.provenance_outbox("tenant", "sender", task["id"]) == items
    with pytest.raises(PeerNotFound):
        restarted.provenance_outbox("other-tenant", "sender", task["id"])
    with pytest.raises(PeerConflict):
        restarted.send(
            "tenant",
            "sender",
            "result_message",
            result,
            task["id"],
            provenance_origin={**ORIGIN, "credential_id": "replacement"},
        )


def test_cancel_proof_frozen_once_and_no_model_or_compute_claim(tmp_path):
    store = A2AStore(tmp_path / "peer.sqlite")
    task = store.send("tenant", "sender", "submit_message", envelope(), provenance_origin=ORIGIN)
    canceled = store.cancel("tenant", "sender", task["id"], provenance_origin=ORIGIN)
    assert store.cancel("tenant", "sender", task["id"], provenance_origin=ORIGIN) == canceled
    items = store.provenance_outbox("tenant", "sender", task["id"])
    assert len(items) == 3
    record = items[-1]["envelope"]["record"]
    assert record["outcome"] == "canceled" and record["kind"] == "permission"
    assert record["causation_id"] == "submit_message"
    assert items[-1]["envelope"]["grant_proof"]["expected_epoch"] == 1
    assert items[-1]["envelope"]["links"]["reasoning_exercised"] is False


def test_outbox_failure_rolls_back_task_event_and_idempotency(tmp_path, monkeypatch):
    store = A2AStore(tmp_path / "peer.sqlite")

    def fail(*args):
        raise ValueError("synthetic outbox failure")

    monkeypatch.setattr(store, "_peer_record", fail)
    with pytest.raises(ValueError):
        store.send("tenant", "sender", "submit_message", envelope(), provenance_origin=ORIGIN)
    with store.connect() as db:
        for table in ("peer_tasks", "peer_messages", "peer_events", "peer_provenance_outbox"):
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
