"""Isolated durable reference-ledger tests; no network/provider effects."""

import concurrent.futures

import pytest

from tessera_control.a2a_store import A2AStore, PeerConflict, PeerNotFound


def envelope(operation="submit", reference="context-1"):
    return {
        "operation": operation,
        "reference": reference,
        "authority": {"grant_id": "grant-1", "revision": 1, "epoch": 1, "binding": {}},
    }


def test_restart_replay_and_conflict(tmp_path):
    path = tmp_path / "peer.db"
    store = A2AStore(path)
    task = store.send("tenant", "agent", "message", envelope())
    restarted = A2AStore(path)
    assert restarted.send("tenant", "agent", "message", envelope()) == task
    with pytest.raises(PeerConflict):
        restarted.send("tenant", "agent", "message", envelope(reference="other"))
    with pytest.raises(PeerNotFound):
        restarted.get("other-tenant", "agent", task["id"])
    with pytest.raises(PeerNotFound):
        restarted.get("tenant", "other-agent", task["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM peer_events").fetchone()[0] == 1


def test_concurrent_replay(tmp_path):
    store = A2AStore(tmp_path / "peer.db")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        tasks = list(
            pool.map(lambda _: store.send("tenant", "agent", "message", envelope()), range(8))
        )
    assert len({task["id"] for task in tasks}) == 1


def test_cancel_fences_late_result_and_persists(tmp_path):
    store = A2AStore(tmp_path / "peer.db")
    task = store.send("tenant", "agent", "message", envelope())
    canceled = store.cancel("tenant", "agent", task["id"])
    assert store.cancel("tenant", "agent", task["id"]) == canceled
    with pytest.raises(PeerConflict):
        store.send("tenant", "agent", "result", envelope("result"), task["id"])
    assert (
        A2AStore(store.path).get("tenant", "agent", task["id"])["status"]["state"]
        == "TASK_STATE_CANCELED"
    )


def test_result_reference_and_terminal_replay(tmp_path):
    store = A2AStore(tmp_path / "peer.db")
    task = store.send("tenant", "agent", "message", envelope())
    changed = envelope("result")
    changed["authority"]["epoch"] = 2
    with pytest.raises(PeerConflict):
        store.send("tenant", "agent", "bad-result", changed, task["id"])
    result = store.send("tenant", "agent", "result", envelope("result", "artifact-1"), task["id"])
    assert result["status"]["state"] == "TASK_STATE_COMPLETED"
    assert result["artifacts"][0]["parts"][0]["data"]["verified"] is False
    assert (
        store.send("tenant", "agent", "result", envelope("result", "artifact-1"), task["id"])
        == result
    )
    with pytest.raises(PeerConflict):
        store.cancel("tenant", "agent", task["id"])
