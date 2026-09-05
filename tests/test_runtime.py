from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_input_delivery import _delivery
from test_supervisor import FakeAdapter

from tessera_control.api import create_app
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import Conflict, _connect, canonical_digest
from tessera_worker.runtime import CpuRuntime


class ApiChannel:
    def __init__(self, state):
        self.client = TestClient(create_app(state))
        self.disconnected = False

    def post(self, path, body, key):
        if self.disconnected:
            raise ConnectionError("synthetic control disconnect")
        response = self.client.post(
            path,
            json=body,
            headers={
                "Authorization": "Bearer worker-token",
                "Idempotency-Key": key,
            },
        )
        if response.status_code >= 400:
            raise ConnectionError(f"control rejected command: {response.status_code}")
        return response.json()


def test_package_identity_is_stable_and_replay_checks_current_authority(tmp_path):
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    store = RuntimePackageStore(state)
    payload = {"expected_epoch": lease["epoch"]}
    request = (worker, lease["id"], "package", canonical_digest(payload), lease["epoch"])
    first = store.execution_package(*request)[1]
    assert first["package"]["plan_digest"] == grant["plan_digest"]
    assert first["package"]["pinned_image"] == grant["pinned_image"]
    assert first["execution_dispatched"] is False
    assert store.execution_package(*request)[1] == first
    renewed = execution.renew_execution_lease(
        worker, lease["id"], "renew", "renew", lease["epoch"]
    )[1]
    after = store.execution_package(*request)[1]
    assert after["package"] == first["package"]
    assert after["lease"]["expires_at"] == renewed["expires_at"]
    execution.revoke_execution_grant(owner, grant["id"], "revoke", "revoke", 1)
    with pytest.raises(Conflict):
        store.execution_package(*request)


def test_corrupt_plan_does_not_produce_a_package(tmp_path):
    state, _, _, worker, grant, lease = _delivery(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE task_revisions SET purpose='tampered' WHERE task_id=?", (grant["task_id"],)
        )
    with pytest.raises(Conflict, match="checksum"):
        RuntimePackageStore(state).execution_package(worker, lease["id"], "p", "p", lease["epoch"])


@pytest.mark.parametrize("failure", ["revoke", "disconnect"])
def test_http_package_to_supervisor_stops_on_authority_failure(tmp_path, failure):
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    channel = ApiChannel(state)
    adapter = FakeAdapter()
    root = tmp_path / "private-inputs"
    root.mkdir(mode=0o700)
    runtime_path = tmp_path / "runtime"
    with CpuRuntime(runtime_path, root, channel, adapter, worker_id=worker["id"]) as runtime:
        started = runtime.start(lease["id"], lease["epoch"])
        assert started.phase == "running"
        assert (
            Path(runtime.operation.input_directory) / "input.txt"
        ).read_bytes() == b"immutable input"
        assert (
            runtime.operation.operation_id
            == runtime.descriptor["control"]["package"]["operation_id"]
        )
        runtime.report(started)
        if failure == "revoke":
            execution.revoke_execution_grant(owner, grant["id"], "stop", "stop", 1)
        else:
            channel.disconnected = True
        stopped = runtime.tick()
        assert stopped.phase == "terminal"
        assert stopped.observation.termination_confirmed
        if failure == "revoke":
            runtime.report(stopped)
    assert adapter.calls.count("start") == 1
    assert adapter.calls.count("cancel") == 1


def test_runtime_restart_reuses_input_tree_and_never_resubmits_lost_start(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    channel = ApiChannel(state)
    root = tmp_path / "private-inputs"
    root.mkdir(mode=0o700)
    adapter = FakeAdapter()
    adapter.lose_start_reply = True
    directory = tmp_path / "runtime"
    with CpuRuntime(directory, root, channel, adapter, worker_id=worker["id"]) as runtime:
        assert runtime.start(lease["id"], lease["epoch"]).phase == "uncertain"
        original_path = runtime.operation.input_directory
    adapter.lose_start_reply = False
    with CpuRuntime(directory, root, channel, adapter, worker_id=worker["id"]) as runtime:
        assert runtime.start(lease["id"], lease["epoch"]).phase == "terminal"
        assert runtime.operation.input_directory == original_path
    assert len(list(root.glob("tessera-input-*"))) == 1
    assert adapter.calls.count("start") == 1


def test_same_lease_cannot_start_from_a_second_journal(tmp_path):
    from tessera_worker.supervisor import SupervisorError

    state, _, _, worker, _, lease = _delivery(tmp_path)
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    channel, adapter = ApiChannel(state), FakeAdapter()
    with CpuRuntime(tmp_path / "first", root, channel, adapter, worker_id=worker["id"]) as first:
        assert first.start(lease["id"], lease["epoch"]).phase == "running"
        with CpuRuntime(
            tmp_path / "second", root, channel, adapter, worker_id=worker["id"]
        ) as second:
            with pytest.raises(SupervisorError, match="another runtime journal"):
                second.start(lease["id"], lease["epoch"])
    assert adapter.calls.count("start") == 1


def test_rejected_epoch_does_not_poison_the_valid_runtime_binding(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    with CpuRuntime(
        tmp_path / "runtime", root, ApiChannel(state), FakeAdapter(), worker_id=worker["id"]
    ) as runtime:
        with pytest.raises(ConnectionError, match="409"):
            runtime.start(lease["id"], lease["epoch"] + 1)
        assert list(root.iterdir()) == []
        assert runtime.start(lease["id"], lease["epoch"]).phase == "running"


def test_stalled_http_response_cannot_hold_supervisor_cancellation(tmp_path):
    import threading
    import time
    from urllib.parse import urlsplit

    from tessera_worker.control_channel import HttpControlChannel

    state, _, _, worker, _, lease = _delivery(tmp_path)
    client = TestClient(create_app(state))
    stall, release = threading.Event(), threading.Event()

    class Response:
        def __init__(self, response):
            self.response = response
            self.headers = response.headers

        def getcode(self):
            return self.response.status_code

        def read(self, size):
            if stall.is_set():
                release.wait(2)
            return self.response.content[:size]

        def close(self):
            self.response.close()

    def open_request(request, timeout):
        return Response(
            client.post(
                urlsplit(request.full_url).path,
                content=request.data,
                headers=dict(request.header_items()),
            )
        )

    channel = HttpControlChannel(
        "http://127.0.0.1:8765", "worker-token", timeout_seconds=0.2, opener=open_request
    )
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    adapter = FakeAdapter()
    try:
        with CpuRuntime(
            tmp_path / "runtime", root, channel, adapter, worker_id=worker["id"]
        ) as runtime:
            assert runtime.start(lease["id"], lease["epoch"]).phase == "running"
            stall.set()
            start = time.monotonic()
            stopped = runtime.tick()
            assert time.monotonic() - start < 1
            assert stopped.phase == "terminal"
            assert stopped.reason == "cancelled_lease_denied"
            assert adapter.calls.count("cancel") == 1
    finally:
        release.set()
        if channel._inflight is not None:
            channel._inflight.result(timeout=2)
        client.close()
