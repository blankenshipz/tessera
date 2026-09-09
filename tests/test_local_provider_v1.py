"""Control-to-receipt integration tests for the injected local HTTP provider."""

from __future__ import annotations

import pytest
from local_provider_fixture import LocalProviderFixture
from test_fake_provider_dispatch import _runtime
from test_input_delivery import _delivery
from test_supervisor import FakeAdapter

from tessera_control.store import _connect
from tessera_worker.local_provider_v1 import (
    MAX_RESPONSE_BYTES,
    LocalProviderHttpError,
    LocalProviderV1,
)


def _receipt(state, operation_id):
    with _connect(state / "tessera.sqlite3") as connection:
        return connection.execute(
            "SELECT * FROM external_operation_receipts WHERE operation_id=?", (operation_id,)
        ).fetchone()


def test_local_provider_v1_submit_recovers_lost_reply_to_one_control_receipt(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    with LocalProviderFixture() as fixture:
        provider = LocalProviderV1(fixture.endpoint, fixture.fixture_token)
        fixture.lose_submit_reply = True
        with _runtime(tmp_path, state, worker, provider, FakeAdapter()) as runtime:
            assert runtime.start(lease["id"], lease["epoch"]).phase == "uncertain"
            operation_id = runtime.operation.operation_id
        fixture.lose_submit_reply = False
        with _runtime(tmp_path, state, worker, provider, FakeAdapter()) as runtime:
            runtime.start(lease["id"], lease["epoch"])
        receipt = _receipt(state, operation_id)
    assert fixture.submit_calls == [f"tessera-op-{operation_id}"]
    assert receipt["provider_kind"] == "fake"
    assert receipt["provider_version"] == "local-provider-v1"
    assert receipt["provider_id"].startswith("local-")
    assert receipt["observed_state"] == "running"


def test_local_provider_v1_lost_observe_and_cancel_reconcile_without_new_submit(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    with LocalProviderFixture() as fixture:
        provider = LocalProviderV1(fixture.endpoint, fixture.fixture_token)
        with _runtime(tmp_path, state, worker, provider, FakeAdapter()) as runtime:
            assert runtime.start(lease["id"], lease["epoch"]).phase == "running"
            operation = runtime.operation
            fixture.lose_observe_reply = True
            with pytest.raises(LocalProviderHttpError, match="transport unavailable"):
                runtime.provider_dispatch.reconcile(operation, runtime.lease)
            fixture.lose_observe_reply = False
            assert runtime.provider_dispatch.reconcile(operation, runtime.lease).state == "running"
            fixture.lose_cancel_reply = True
            with pytest.raises(LocalProviderHttpError, match="transport unavailable"):
                runtime.provider_dispatch.cancel(operation, runtime.lease)
            fixture.lose_cancel_reply = False
            observation = runtime.provider_dispatch.reconcile(operation, runtime.lease)
            assert observation.state == "exited"
            assert observation.termination_confirmed is True
            accounting = runtime.provider_dispatch.accounting_stable(operation)
    receipt = _receipt(state, operation.operation_id)
    assert fixture.submit_calls == [f"tessera-op-{operation.operation_id}"]
    assert fixture.cancel_calls == [f"tessera-op-{operation.operation_id}"]
    assert receipt["observed_state"] == "exited"
    assert receipt["termination_confirmed"] == 1
    assert accounting.exposure_state == "unknown"
    assert accounting.usage is None


def test_local_provider_v1_rejects_changed_receipt_identity_and_preserves_control_record(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    with LocalProviderFixture() as fixture:
        provider = LocalProviderV1(fixture.endpoint, fixture.fixture_token)
        with _runtime(tmp_path, state, worker, provider, FakeAdapter()) as runtime:
            assert runtime.start(lease["id"], lease["epoch"]).phase == "running"
            operation = runtime.operation
            assert operation is not None
            original = fixture.operations[operation.container_name]["provider_id"]
            fixture.operations[operation.container_name]["receipt_payload"]["provider_id"] = (
                "changed"
            )
            with pytest.raises(LocalProviderHttpError, match="receipt identity"):
                runtime.provider_dispatch.reconcile(operation, runtime.lease)
    receipt = _receipt(state, operation.operation_id)
    assert receipt["provider_id"] == original
    assert receipt["observed_state"] == "running"


def test_local_provider_v1_rejects_stale_epoch_before_provider_submit(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    with LocalProviderFixture() as fixture:
        provider = LocalProviderV1(fixture.endpoint, fixture.fixture_token)
        with _runtime(tmp_path, state, worker, provider, FakeAdapter()) as runtime:
            with pytest.raises(ConnectionError, match="409"):
                runtime.start(lease["id"], lease["epoch"] + 1)
    assert fixture.submit_calls == []


def test_local_provider_v1_rejects_bounded_oversized_http_response():
    class OversizedResponse:
        headers = {"Content-Type": "application/json"}

        def getcode(self):
            return 200

        def read(self, amount=-1):
            return b"x" * (MAX_RESPONSE_BYTES + 1)

        def close(self):
            return None

    provider = LocalProviderV1(
        "http://127.0.0.1:1",
        "fixture-token",
        opener=lambda request, timeout: OversizedResponse(),
    )
    with pytest.raises(LocalProviderHttpError, match="response exceeds its bound"):
        provider.observe("lookup")


def test_local_provider_v1_refuses_non_loopback_or_redirected_endpoints():
    with pytest.raises(ValueError, match="literal loopback"):
        LocalProviderV1("https://provider.example", "fixture-token")
    with pytest.raises(ValueError, match="literal loopback"):
        LocalProviderV1("http://localhost:1", "fixture-token")
    with pytest.raises(ValueError, match="fixture token"):
        LocalProviderV1("http://127.0.0.1:1", "not a token")
