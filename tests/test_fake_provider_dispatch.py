"""Fake-provider receipts exercise only bounded operational evidence."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from test_input_delivery import _delivery
from test_runtime import ApiChannel
from test_supervisor import FakeAdapter

from tessera_control.execution import ExecutionStore
from tessera_control.store import Conflict, _connect, canonical_digest
from tessera_worker.provider import FakeProvider
from tessera_worker.runtime import CpuRuntime


def _runtime(tmp_path, state, worker, provider, adapter):
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700, exist_ok=True)
    return CpuRuntime(
        tmp_path / "runtime",
        inputs,
        ApiChannel(state),
        adapter,
        worker_id=worker["id"],
        provider=provider,
    )


def test_fake_submit_follows_start_intent_and_persists_one_receipt(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    provider, adapter = FakeProvider(), FakeAdapter()
    with _runtime(tmp_path, state, worker, provider, adapter) as runtime:
        result = runtime.start(lease["id"], lease["epoch"])
        assert result.phase == "running"
        operation_id = runtime.operation.operation_id
    assert provider.submit_calls == [operation_id]
    with sqlite3.connect(tmp_path / "runtime" / "journal" / "supervisor.sqlite3") as connection:
        events = [row[0] for row in connection.execute("SELECT event FROM events ORDER BY id")]
    assert events.index("start_intent") < events.index("started")
    with _connect(state / "tessera.sqlite3") as connection:
        receipt = connection.execute("SELECT * FROM external_operation_receipts").fetchone()
    assert receipt["operation_id"] == operation_id
    assert receipt["provider_kind"] == "fake"
    assert receipt["provider_id"] == f"fake-{operation_id}"
    assert receipt["observed_state"] == "running"


def test_lost_submit_reply_restarts_by_lookup_without_a_second_submit(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    provider, adapter = FakeProvider(), FakeAdapter()
    provider.lose_submit_reply = True
    with _runtime(tmp_path, state, worker, provider, adapter) as runtime:
        assert runtime.start(lease["id"], lease["epoch"]).phase == "uncertain"
        operation_id = runtime.operation.operation_id
    provider.lose_submit_reply = False
    with _runtime(tmp_path, state, worker, provider, adapter) as runtime:
        assert runtime.start(lease["id"], lease["epoch"]).phase == "uncertain"
    assert provider.submit_calls == [operation_id]
    assert provider.observe_calls
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM external_operation_receipts").fetchone()[0]
            == 1
        )


def test_lost_receipt_report_replays_the_same_durable_idempotency_request(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    provider, adapter = FakeProvider(), FakeAdapter()

    class LostReplyChannel(ApiChannel):
        def __init__(self, source):
            super().__init__(source)
            self.lost = True
            self.receipt_keys: list[str] = []

        def post(self, path, body, key):
            if "external-operations" in path:
                self.receipt_keys.append(key)
                response = super().post(path, body, key)
                if self.lost:
                    self.lost = False
                    raise ConnectionError("lost receipt response")
                return response
            return super().post(path, body, key)

    channel = LostReplyChannel(state)
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700)
    with CpuRuntime(
        tmp_path / "runtime", inputs, channel, adapter, worker_id=worker["id"], provider=provider
    ) as runtime:
        assert runtime.start(lease["id"], lease["epoch"]).phase == "uncertain"
    with CpuRuntime(
        tmp_path / "runtime", inputs, channel, adapter, worker_id=worker["id"], provider=provider
    ) as runtime:
        assert runtime.start(lease["id"], lease["epoch"]).phase == "uncertain"
    assert len(channel.receipt_keys) == 2
    assert channel.receipt_keys[0] == channel.receipt_keys[1]
    assert len(provider.submit_calls) == 1


def test_v18_to_v19_receipt_migration_does_not_invent_receipts(tmp_path):
    from tessera_control.store import initialize_state, migrate_state

    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE external_operation_receipts")
        connection.execute("UPDATE schema_meta SET schema_version=18")
    assert migrate_state(state) == 19
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM external_operation_receipts").fetchone()[0]
            == 0
        )


def test_v19_receipt_migration_rolls_back_without_a_partial_table(tmp_path, monkeypatch):
    from tessera_control.migrations import MIGRATIONS
    from tessera_control.store import initialize_state, migrate_state

    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE external_operation_receipts")
        connection.execute("UPDATE schema_meta SET schema_version=18")

    def fail(connection):
        from tessera_control.external_operations import create_external_operation_receipt_schema

        create_external_operation_receipt_schema(connection)
        raise RuntimeError("injected receipt migration fault")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 18, fail)
        with pytest.raises(RuntimeError, match="injected receipt"):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 18
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='external_operation_receipts'"
            ).fetchone()
            is None
        )


def test_provider_identity_conflicts_and_delayed_cancel_stay_visible(tmp_path):
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    provider, adapter = FakeProvider(), FakeAdapter()
    with _runtime(tmp_path, state, worker, provider, adapter) as runtime:
        assert runtime.start(lease["id"], lease["epoch"]).phase == "running"
        operation = runtime.operation
    fake_receipt = provider.operations[operation.container_name]
    payload = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "provider_kind": "fake",
        "provider_version": fake_receipt.provider_version,
        "provider_id": "conflicting-provider-id",
        "receipt_payload": fake_receipt.payload,
        "receipt_digest": canonical_digest(fake_receipt.payload),
        "observed_state": "running",
        "observed_at": datetime.now(UTC).isoformat(),
        "termination_confirmed": False,
    }
    with pytest.raises(Conflict, match="immutable identity"):
        ExecutionStore(state).report_external_operation_receipt(
            worker,
            operation.operation_id,
            "conflicting-receipt",
            canonical_digest(payload),
            payload,
        )
    execution.revoke_execution_grant(owner, grant["id"], "revoke", "revoke", 1)
    provider.cancel_delayed = True
    assert provider.cancel(operation.container_name, fake_receipt.provider_id).state == "running"
    assert provider.observe("unknown-lookup").state == "unknown"
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT desired_state FROM external_operations").fetchone()[
            0
        ] == ("cancel_requested")
