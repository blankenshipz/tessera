from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import _worker
from test_input_delivery import _delivery
from test_supervisor import FakeAdapter

from tessera_control.api import create_app
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import Conflict, NotFound, _connect, canonical_digest
from tessera_worker.runtime import CpuRuntime


def _persistent_storage(**changes):
    capability = {
        "contract_version": 1,
        "allocation_id": "slot-fixture-1",
        "filesystem_id": "filesystem-fixture-1",
        "mount_id": "mount-fixture-1",
        "capacity_bytes": 16 * 1024 * 1024,
        "capacity_inodes": 128,
        "noexec": True,
        "nosuid": True,
        "nodev": True,
        "write_uid": 65532,
        "write_access_verified": True,
        "supervisor_custody_verified": True,
        "no_nested_mounts_verified": True,
    }
    return capability | changes


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
    assert "persistent_output_storage" not in first["package"]
    assert first["execution_dispatched"] is False
    operation = first["operation"]
    assert operation["operation_id"] == first["package"]["operation_id"]
    assert operation["lease_id"] == lease["id"]
    assert operation["package_digest"] == first["package_digest"]
    assert operation["retry_class"] == "manual_reconciliation"
    assert operation["desired_state"] == "admitted"
    assert operation["observed_state"] == "unknown"
    assert operation["docker_lookup_key"] == f"tessera-op-{operation['operation_id']}"
    assert store.execution_package(*request)[1] == first
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='execution.package_prepared'"
            ).fetchone()[0]
            == 1
        )
    renewed = execution.renew_execution_lease(
        worker, lease["id"], "renew", "renew", lease["epoch"]
    )[1]
    after = store.execution_package(*request)[1]
    assert after["package"] == first["package"]
    assert after["lease"]["expires_at"] == renewed["expires_at"]
    execution.revoke_execution_grant(owner, grant["id"], "revoke", "revoke", 1)
    with pytest.raises(Conflict):
        store.execution_package(*request)


def test_journaled_observations_fence_operation_bindings_and_provider_identity(tmp_path):
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    package = RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "package",
        canonical_digest({"expected_epoch": lease["epoch"]}),
        lease["epoch"],
    )[1]
    operation = package["operation"]
    report = {
        "expected_epoch": lease["epoch"],
        "operation_id": operation["operation_id"],
        "observed_state": "running",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "container-one",
        "termination_confirmed": False,
    }
    with pytest.raises(Conflict, match="external operation"):
        execution.report_execution_observation(
            worker,
            lease["id"],
            "wrong-operation",
            canonical_digest(report | {"operation_id": "a" * 32}),
            report | {"operation_id": "a" * 32},
        )
    with pytest.raises(Conflict, match="epoch"):
        execution.report_execution_observation(
            worker,
            lease["id"],
            "wrong-epoch",
            canonical_digest(report | {"expected_epoch": lease["epoch"] + 1}),
            report | {"expected_epoch": lease["epoch"] + 1},
        )
    wrong_worker = _worker(state, owner, grant["project_id"], "journal-wrong-worker")
    with pytest.raises(NotFound):
        execution.report_execution_observation(
            wrong_worker,
            lease["id"],
            "wrong-worker",
            canonical_digest(report),
            report,
        )
    _, observed = execution.report_execution_observation(
        worker, lease["id"], "running", canonical_digest(report), report
    )
    assert observed["operation_id"] == operation["operation_id"]
    with pytest.raises(Conflict, match="external operation"):
        execution.report_execution_observation(
            worker,
            lease["id"],
            "missing-operation",
            canonical_digest(report | {"operation_id": None}),
            report | {"operation_id": None},
        )
    with pytest.raises(Conflict, match="Provider identity"):
        execution.report_execution_observation(
            worker,
            lease["id"],
            "changed-provider",
            canonical_digest(report | {"provider_id": "container-two"}),
            report | {"provider_id": "container-two"},
        )
    execution.revoke_execution_grant(owner, package["package"]["grant_id"], "revoke", "revoke", 1)
    with _connect(state / "tessera.sqlite3") as connection:
        row = connection.execute("SELECT * FROM external_operations").fetchone()
    assert row["desired_state"] == "cancel_requested"
    assert row["observed_state"] == "running"
    assert row["provider_id"] == "container-one"


def test_corrupt_plan_does_not_produce_a_package(tmp_path):
    state, _, _, worker, grant, lease = _delivery(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE task_revisions SET purpose='tampered' WHERE task_id=?", (grant["task_id"],)
        )
    with pytest.raises(Conflict, match="checksum"):
        RuntimePackageStore(state).execution_package(worker, lease["id"], "p", "p", lease["epoch"])


@pytest.mark.parametrize(
    ("capability", "message"),
    [
        (None, "capability is required"),
        (_persistent_storage(noexec=False), "requires noexec"),
        (_persistent_storage(no_nested_mounts_verified=False), "no_nested_mounts_verified"),
    ],
)
def test_persistent_output_storage_admission_fails_closed(tmp_path, capability, message):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    with pytest.raises(Conflict, match=message):
        RuntimePackageStore(state).execution_package(
            worker, lease["id"], "persistent", "persistent", lease["epoch"], True, capability
        )


def test_persistent_output_storage_is_frozen_and_changed_capacity_rejects_replay(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    store = RuntimePackageStore(state)
    capability = _persistent_storage()
    response = store.execution_package(
        worker, lease["id"], "persistent", "persistent", lease["epoch"], True, capability
    )[1]
    assert response["package"]["persistent_output_storage"] == capability
    assert response["package_digest"] == canonical_digest(response["package"])
    with pytest.raises(Conflict, match="package digest conflicts"):
        store.execution_package(
            worker,
            lease["id"],
            "persistent",
            "persistent",
            lease["epoch"],
            True,
            _persistent_storage(capacity_bytes=32 * 1024 * 1024),
        )


def test_persistent_output_storage_failure_precedes_supervisor_effects(tmp_path):
    state, _, _, worker, _, lease = _delivery(tmp_path)
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    adapter = FakeAdapter()
    with CpuRuntime(
        tmp_path / "runtime",
        root,
        ApiChannel(state),
        adapter,
        worker_id=worker["id"],
        persistent_output_collection=True,
    ) as runtime:
        with pytest.raises(ConnectionError, match="409"):
            runtime.start(lease["id"], lease["epoch"])
    assert adapter.calls == []


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
    read_entered, release = threading.Event(), threading.Event()

    class Response:
        def __init__(self, response, *, block_read=False):
            self.response = response
            self.headers = response.headers
            self.block_read = block_read

        def getcode(self):
            return self.response.status_code

        def read(self, size):
            if self.block_read:
                read_entered.set()
                release.wait(2)
            return self.response.content[:size]

        def close(self):
            self.response.close()

    def open_request(request, timeout, *, block_read=False):
        return Response(
            client.post(
                urlsplit(request.full_url).path,
                content=request.data,
                headers=dict(request.header_items()),
            ),
            block_read=block_read,
        )

    startup_channel = HttpControlChannel(
        "http://127.0.0.1:8765", "worker-token", timeout_seconds=5, opener=open_request
    )
    stalled_read_channel = HttpControlChannel(
        "http://127.0.0.1:8765",
        "worker-token",
        timeout_seconds=0.2,
        opener=lambda request, timeout: open_request(request, timeout, block_read=True),
    )
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    adapter = FakeAdapter()
    try:
        with CpuRuntime(
            tmp_path / "runtime", root, startup_channel, adapter, worker_id=worker["id"]
        ) as runtime:
            assert runtime.start(lease["id"], lease["epoch"]).phase == "running"
            runtime.channel = stalled_read_channel
            result = []
            tick = threading.Thread(target=lambda: result.append(runtime.tick()))
            start = time.monotonic()
            tick.start()
            assert read_entered.wait(1)
            tick.join(1)
            assert time.monotonic() - start < 1
            assert not tick.is_alive()
            assert len(result) == 1
            stopped = result[0]
            assert stopped.phase == "terminal"
            assert stopped.reason == "cancelled_lease_denied"
            assert adapter.calls.count("cancel") == 1
    finally:
        release.set()
        if stalled_read_channel._inflight is not None:
            stalled_read_channel._inflight.result(timeout=2)
        client.close()
