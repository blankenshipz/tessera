"""Isolated generated transport ledger; fixture receipt is not inference evidence."""

from copy import deepcopy

import pytest

from tessera_control.a2a_store import A2AStore, PeerConflict, PeerNotFound, validate_generated_pins


def pins():
    return {
        "record_id": "public-output-record",
        "collector_sequence": 5,
        "artifact_id": "critic-artifact",
        "manifest_id": "manifest",
        "operation_id": "operation",
        "task_id": "central-task",
        "run_id": "critic-run",
        "session_id": "critic-session",
        "harness_instance_id": "harness-instance",
        "context_id": "public-disclosure",
        "recipient_context_id": "planner-artifact-disclosure",
        "critic_credential_id": "critic-credential",
        "model_id": "approved-critic-model",
        "context_digest": "a" * 64,
        "skill_digest": "b" * 64,
        "config_digest": "c" * 64,
        "content_digest": "sha256:" + "d" * 64,
        "manifest_digest": "e" * 64,
        "source_event_id": "output-event",
        "source_sequence": 1,
    }


ORIGIN = {
    "receiver_peer_id": "planner",
    "credential_id": "planner-credential",
    "project_id": "project",
    "destination": "oauth:included",
}


def envelope(operation="submit"):
    return {
        "operation": operation,
        "reference": "critic-artifact",
        "generated": pins(),
        "authority": {
            "grant_id": "transport-delegation",
            "revision": 1,
            "epoch": 1,
            "binding": {"task_id": "central-task", "variant_id": "variant"},
        },
    }


def delivered(store):
    task = store.send(
        "tenant", "critic", "submit", envelope(), provenance_origin=ORIGIN, generated_receipt=pins()
    )
    return store.send(
        "tenant",
        "critic",
        "result",
        envelope("result"),
        task["id"],
        provenance_origin=ORIGIN,
        generated_receipt=pins(),
    )


def test_registered_result_requires_explicit_planner_ack_restart_replay_atomic_outbox(tmp_path):
    store = A2AStore(tmp_path / "peer.sqlite")
    task = delivered(store)
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task["metadata"]["generated_delivery_state"] == "awaiting_planner_consumption"
    data = task["artifacts"][0]["parts"][0]["data"]
    assert data["registered_public_output"] == pins() and data["verified"] is False
    restarted = A2AStore(store.path)
    assert (
        restarted.send(
            "tenant",
            "critic",
            "result",
            envelope("result"),
            task["id"],
            provenance_origin=ORIGIN,
            generated_receipt=pins(),
        )
        == task
    )
    assert restarted.consumption_task("tenant", "planner", task["id"]) == task
    completed = restarted.consume(
        "tenant", "planner", "ack", task["id"], envelope("consume"), generated_receipt=pins()
    )
    assert completed["status"]["state"] == "TASK_STATE_COMPLETED"
    assert completed["metadata"]["planner_consumption"]["planner_id"] == "planner"
    assert (
        restarted.consume(
            "tenant", "planner", "ack", task["id"], envelope("consume"), generated_receipt=pins()
        )
        == completed
    )
    outbox = restarted.provenance_outbox("tenant", "critic", task["id"])
    assert len(outbox) == 5
    ack = outbox[-1]["envelope"]["record"]
    assert ack["tool_id"] == "a2a.reference.consume" and ack["remote"]["peer_id"] == "planner"
    assert (
        ack["causation_id"] == "result"
        and ack["references"]["provenance_record_id"] == pins()["record_id"]
    )
    assert all(item["envelope"]["record"]["model_id"] is None for item in outbox)
    with pytest.raises(PeerConflict):
        restarted.consume(
            "tenant",
            "planner",
            "another-ack",
            task["id"],
            envelope("consume"),
            generated_receipt=pins(),
        )


@pytest.mark.parametrize("field", sorted(pins()))
def test_all_generated_pins_are_immutable(tmp_path, field):
    store = A2AStore(tmp_path / "peer.sqlite")
    task = store.send(
        "tenant", "critic", "submit", envelope(), provenance_origin=ORIGIN, generated_receipt=pins()
    )
    changed = envelope("result")
    changed["generated"][field] = (
        pins()[field] + 1
        if isinstance(pins()[field], int)
        else "other"
        if not field.endswith("digest")
        else "f" * 64
    )
    if field == "content_digest":
        changed["generated"][field] = "sha256:" + "f" * 64
    with pytest.raises(PeerConflict):
        store.send(
            "tenant",
            "critic",
            "result",
            changed,
            task["id"],
            provenance_origin=ORIGIN,
            generated_receipt=changed["generated"],
        )
    assert store.get("tenant", "critic", task["id"]) == task


def test_no_receipt_no_generated_claim_and_cancel_blocks_consumption(tmp_path):
    store = A2AStore(tmp_path / "peer.sqlite")
    with pytest.raises(PeerConflict, match="verification required"):
        store.send("tenant", "critic", "submit", envelope(), provenance_origin=ORIGIN)
    task = delivered(store)
    for tenant, actor in (("other", "planner"), ("tenant", "critic"), ("tenant", "stranger")):
        with pytest.raises(PeerNotFound):
            store.consume(
                tenant, actor, "ack", task["id"], envelope("consume"), generated_receipt=pins()
            )
    store.cancel("tenant", "critic", task["id"], provenance_origin=ORIGIN)
    with pytest.raises(PeerConflict):
        store.consume(
            "tenant", "planner", "ack", task["id"], envelope("consume"), generated_receipt=pins()
        )
    assert len(store.provenance_outbox("tenant", "critic", task["id"])) == 5


def test_ack_outbox_failure_rolls_back_ack_event_and_idempotency(tmp_path, monkeypatch):
    store = A2AStore(tmp_path / "peer.sqlite")
    task = delivered(store)
    with store.connect() as db:
        count = db.execute("SELECT count(*) FROM peer_events").fetchone()[0]

    def fail(*args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(store, "_peer_record", fail)
    with pytest.raises(RuntimeError):
        store.consume(
            "tenant", "planner", "ack", task["id"], envelope("consume"), generated_receipt=pins()
        )
    assert store.get("tenant", "critic", task["id"]) == task
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM peer_events").fetchone()[0] == count
        assert (
            db.execute("SELECT count(*) FROM peer_messages WHERE agent='planner'").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "change",
    [
        {"content": "unsafe"},
        {"source_sequence": True},
        {"source_sequence": 2**53},
        {"artifact_id": "../escape"},
    ],
)
def test_closed_bounded_pins_reject_payloads_and_unsafe_sequences(change):
    with pytest.raises(PeerConflict):
        validate_generated_pins({**deepcopy(pins()), **change})
