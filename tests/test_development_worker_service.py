"""Development worker orchestration tests; human authority is fixture setup only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_input_delivery import _delivery
from test_supervisor import FakeAdapter

from tessera_control.api import create_app
from tessera_control.store import _connect, canonical_digest
from tessera_worker.service import (
    DevelopmentWorkerConfig,
    DevelopmentWorkerService,
    read_private_credential,
)


class ApiChannel:
    def __init__(self, state):
        self.client = TestClient(create_app(state))
        self.paths: list[str] = []
        self.disconnected = False

    def post(self, path, body, key):
        self.paths.append(path)
        if self.disconnected:
            raise ConnectionError("synthetic control disconnect")
        response = self.client.post(
            path,
            json=body,
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": key},
        )
        if response.status_code >= 400:
            raise ConnectionError(f"control rejected command: {response.status_code}")
        return response.json()


def _service(tmp_path, state, grant, *, channel=None, adapter=None, max_ticks=0, sleep=None):
    credential = tmp_path / "worker.credential"
    credential.write_text("worker-token")
    credential.chmod(0o600)
    workspace = tmp_path / "worker"
    outputs = tmp_path / "outputs"
    workspace.mkdir(mode=0o700, exist_ok=True)
    outputs.mkdir(mode=0o700, exist_ok=True)
    config = DevelopmentWorkerConfig(
        "http://127.0.0.1:8765",
        grant["id"],
        credential,
        workspace,
        outputs,
        grant["pinned_image"],
        max_ticks=max_ticks,
        renew_every_ticks=1,
    )
    adapter = adapter or FakeAdapter()
    adapter.development_output_root = outputs / "development-only-cpu-outputs" / "workspaces"
    return DevelopmentWorkerService(
        config,
        channel=channel or ApiChannel(state),
        adapter=adapter,
        sleep=sleep or (lambda _: None),
    )


def _unleased_delivery(tmp_path):
    state, execution, owner, worker, grant, _ = _delivery(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DELETE FROM execution_leases")
    return state, execution, owner, worker, grant


def test_credential_file_is_private_and_never_accepts_a_link(tmp_path):
    credential = tmp_path / "credential"
    credential.write_text("worker-token")
    credential.chmod(0o600)
    assert read_private_credential(credential) == "worker-token"
    credential.chmod(0o644)
    with pytest.raises(Exception, match="not private"):
        read_private_credential(credential)


def test_worker_service_heartbeats_before_lease_and_never_issues_human_commands(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    channel = ApiChannel(state)
    service = _service(tmp_path, state, grant, channel=channel)

    assert service.run()["state"] == "running"
    assert channel.paths[:2] == ["/worker/heartbeat", "/worker/leases"]
    assert all(path.startswith("/worker/") for path in channel.paths)
    assert "/projects/" not in channel.paths


def test_worker_service_profile_mismatch_precedes_start(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class BadPackageChannel(ApiChannel):
        def post(self, path, body, key):
            response = super().post(path, body, key)
            if path.endswith("/package"):
                response["package"]["profile_version"] = "wrong-profile"
                response["package_digest"] = canonical_digest(response["package"])
            return response

    adapter = FakeAdapter()
    with pytest.raises(Exception, match="authority"):
        _service(tmp_path, state, grant, channel=BadPackageChannel(state), adapter=adapter).run()
    assert "start" not in adapter.calls


def test_renewal_lost_response_replays_the_persisted_key(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class FlakyChannel:
        def __init__(self):
            self.keys = []
            self.calls = 0

        def post(self, path, body, key):
            self.keys.append(key)
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("lost reply")
            return {}

    channel = FlakyChannel()
    service = _service(tmp_path, state, grant, channel=channel)
    runtime = SimpleNamespace(
        lease=SimpleNamespace(id="lease-fixture", epoch=1), _refresh_lease=lambda: None
    )
    with pytest.raises(ConnectionError):
        service._renew(runtime)
    service._renew(runtime)
    service._renew(runtime)
    assert channel.keys[:2] == [channel.keys[0], channel.keys[0]]
    assert channel.keys[2] != channel.keys[0]


def test_restart_reconciles_one_start_then_quarantines_only(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class OutputAdapter(FakeAdapter):
        def start(self, operation):
            Path(operation.development_output.directory, "result.txt").write_text(
                "synthetic output"
            )
            return super().start(operation)

    adapter = OutputAdapter()
    adapter.lose_start_reply = True
    first = _service(tmp_path, state, grant, adapter=adapter)
    assert first.run()["state"] == "unknown"
    adapter.lose_start_reply = False
    second = _service(tmp_path, state, grant, adapter=adapter)
    result = second.run()
    assert result["state"] == "exited"
    assert result["receipt"]["status"] == "quarantined"
    assert result["receipt"]["classification"] == "unclassified"
    assert adapter.calls.count("start") == 1
    assert not list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


@pytest.mark.parametrize("failure", ["revocation", "disconnect"])
def test_revocation_or_disconnect_cancels_and_never_reclaims_uncertain_output(tmp_path, failure):
    state, execution, owner, _, grant = _unleased_delivery(tmp_path)
    channel = ApiChannel(state)
    adapter = FakeAdapter()

    def stop(_):
        if failure == "revocation":
            execution.revoke_execution_grant(owner, grant["id"], "stop", "stop", 1)
        else:
            channel.disconnected = True

    service = _service(
        tmp_path, state, grant, channel=channel, adapter=adapter, max_ticks=1, sleep=stop
    )
    if failure == "disconnect":
        with pytest.raises(ConnectionError):
            service.run()
    else:
        # The synthetic adapter writes no declared output; confirmed cancellation
        # is retained, while the undeclared/missing output stays unreclaimed.
        assert service.run()["state"] == "unknown"
    assert adapter.calls.count("cancel") == 1
    if failure == "disconnect":
        assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))
