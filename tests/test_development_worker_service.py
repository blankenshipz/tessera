"""Development worker orchestration tests; human authority is fixture setup only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_input_delivery import _delivery
from test_supervisor import FakeAdapter

from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.store import _connect, canonical_digest
from tessera_worker.development_runtime import DevelopmentCpuRuntime, DevelopmentOutputProfile
from tessera_worker.provider import FakeProvider
from tessera_worker.service import (
    DevelopmentWorkerConfig,
    DevelopmentWorkerService,
    read_private_credential,
)


class ApiChannel:
    def __init__(self, state):
        self.client = TestClient(create_app(state))
        self.paths: list[str] = []
        self.keys: list[tuple[str, str]] = []
        self.disconnected = False

    def post(self, path, body, key):
        self.paths.append(path)
        self.keys.append((path, key))
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


def _service(
    tmp_path, state, grant, *, channel=None, adapter=None, provider=None, max_ticks=0, sleep=None
):
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
        provider=provider,
        sleep=sleep or (lambda _: None),
    )


def _unleased_delivery(tmp_path):
    state, execution, owner, worker, grant, _ = _delivery(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DELETE FROM execution_leases")
    return state, execution, owner, worker, grant


def _admit_service_attempt(state, owner, grant):
    records = ExperimentStore(state)
    design = {
        "goal": "Keep repeated worker polling readable.",
        "success_criterion": "A human reviews the bounded transition history.",
        "protocol": "Run one bounded synthetic baseline.",
        "variants": [
            {
                "kind": "baseline",
                "name": "baseline",
                "method_digest": "a" * 64,
                "input_digest": "b" * 64,
                "metadata": {"synthetic": True},
                "execution_task_id": grant["task_id"],
                "execution_plan_digest": grant["plan_digest"],
            },
            {
                "kind": "intervention",
                "name": "intervention",
                "method_digest": "c" * 64,
                "input_digest": "d" * 64,
                "metadata": {"synthetic": True},
                "execution_task_id": grant["task_id"],
                "execution_plan_digest": grant["plan_digest"],
            },
        ],
    }
    _, experiment = records.create_experiment(
        owner, grant["project_id"], "service-admitted-experiment", canonical_digest(design), design
    )
    baseline = experiment["design"]["variants"][0]
    admission = {
        "expected_variant_revision": baseline["revision"],
        "expected_design_revision": experiment["current_design_revision"],
        "grant_id": grant["id"],
    }
    records.admit_attempt(
        owner,
        baseline["id"],
        "service-admitted-attempt",
        canonical_digest(admission),
        admission,
    )
    return records, experiment


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
    service.adapter.cancel_unknown = True

    result = service.run()
    assert result["state"] == "unknown"
    assert result["reason"] == "tick_limit_cancellation_requested"
    assert service.adapter.calls.count("cancel") == 1
    assert channel.paths[:2] == ["/worker/heartbeat", "/worker/leases"]
    assert all(path.startswith("/worker/") for path in channel.paths)
    assert "/projects/" not in channel.paths


def test_worker_service_repeated_running_polls_keep_admitted_detail_semantic(tmp_path):
    state, _, owner, _, grant = _unleased_delivery(tmp_path)
    records, experiment = _admit_service_attempt(state, owner, grant)
    service = _service(tmp_path, state, grant, max_ticks=8)

    assert service.run()["reason"] == "tick_limit_cancellation_requested"

    attempt = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    assert [entry["state"] for entry in attempt["state_history"]] == [
        "admitted",
        "preparing",
        "running",
        "interrupted",
    ]
    assert attempt["operational_observed_state"] == "exited"
    assert attempt["operational_observed_at"] is not None
    assert attempt["operational_observation_cursor"].startswith("observation:")


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


def test_restart_heartbeats_with_a_distinct_key_before_a_stale_worker_can_lease(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    channel = ApiChannel(state)
    first = _service(tmp_path, state, grant, channel=channel)
    first._heartbeat()
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE worker_registry SET heartbeat_at='2000-01-01T00:00:00+00:00'")

    result = _service(tmp_path, state, grant, channel=channel).run()

    heartbeat_keys = [key for path, key in channel.keys if path == "/worker/heartbeat"]
    assert result["reason"] == "tick_limit_cancellation_requested"
    assert len(heartbeat_keys) == 2
    assert heartbeat_keys[0] != heartbeat_keys[1]


def test_lost_heartbeat_reply_replays_its_durable_key_before_the_next_beat(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class LostReplyChannel(ApiChannel):
        lost = True

        def post(self, path, body, key):
            response = super().post(path, body, key)
            if path == "/worker/heartbeat" and self.lost:
                self.lost = False
                raise ConnectionError("lost heartbeat reply")
            return response

    channel = LostReplyChannel(state)
    service = _service(tmp_path, state, grant, channel=channel)
    with pytest.raises(ConnectionError, match="lost heartbeat"):
        service._heartbeat()
    service._heartbeat()
    service._heartbeat()

    heartbeat_keys = [key for path, key in channel.keys if path == "/worker/heartbeat"]
    assert heartbeat_keys[0] == heartbeat_keys[1]
    assert heartbeat_keys[2] != heartbeat_keys[1]


def test_disconnect_while_reporting_attempts_cancellation_and_retains_the_report(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class ReportingDisconnectChannel(ApiChannel):
        def post(self, path, body, key):
            if path == "/worker/observations":
                self.disconnected = True
            return super().post(path, body, key)

    adapter = FakeAdapter()
    service = _service(
        tmp_path,
        state,
        grant,
        channel=ReportingDisconnectChannel(state),
        adapter=adapter,
        max_ticks=1,
    )

    assert service.run()["reason"] == "observation_report_failure"
    assert adapter.calls.count("cancel") == 1
    with _connect(tmp_path / "worker" / "runtime" / "runtime-observations.sqlite3") as connection:
        assert connection.execute("SELECT completed FROM reports").fetchone()[0] == 0
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_terminal_report_failure_still_attempts_cancellation_before_collection(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class TerminalReportFailureChannel(ApiChannel):
        reports = 0

        def post(self, path, body, key):
            if path == "/worker/observations":
                self.reports += 1
                if self.reports == 2:
                    raise ConnectionError("synthetic terminal report disconnect")
            return super().post(path, body, key)

    adapter = FakeAdapter()

    def exit_operation(_):
        adapter.state = "exited"

    service = _service(
        tmp_path,
        state,
        grant,
        channel=TerminalReportFailureChannel(state),
        adapter=adapter,
        max_ticks=2,
        sleep=exit_operation,
    )

    assert service.run()["reason"] == "observation_report_failure"
    assert adapter.calls.count("cancel") == 1
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_renewal_disconnect_attempts_cancellation_and_replays_the_renewal_intent(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class RenewalDisconnectChannel(ApiChannel):
        def post(self, path, body, key):
            if path == "/worker/leases/renew":
                self.disconnected = True
            return super().post(path, body, key)

    adapter = FakeAdapter()
    service = _service(
        tmp_path,
        state,
        grant,
        channel=RenewalDisconnectChannel(state),
        adapter=adapter,
        max_ticks=2,
    )

    assert service.run()["reason"] == "heartbeat_or_renewal_failure"
    assert adapter.calls.count("cancel") == 1
    with _connect(tmp_path / "worker" / "renewals.sqlite3") as connection:
        assert connection.execute("SELECT completed FROM renewal_intents").fetchone()[0] == 0
    heartbeat_keys = [key for path, key in service.channel.keys if path == "/worker/heartbeat"]
    assert len(heartbeat_keys) == 2
    assert heartbeat_keys[0] != heartbeat_keys[1]


def test_revocation_with_receipt_report_failure_cancels_provider_and_retains_workspace(tmp_path):
    state, execution, owner, _, grant = _unleased_delivery(tmp_path)

    class ReceiptFailureChannel(ApiChannel):
        fail_receipts = False

        def post(self, path, body, key):
            if self.fail_receipts and "external-operations" in path:
                raise ConnectionError("synthetic receipt report disconnect")
            return super().post(path, body, key)

    channel, provider, adapter = ReceiptFailureChannel(state), FakeProvider(), FakeAdapter()

    def revoke(_):
        execution.revoke_execution_grant(owner, grant["id"], "stop", "stop", 1)
        channel.fail_receipts = True

    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        provider=provider,
        adapter=adapter,
        max_ticks=2,
        sleep=revoke,
    )

    result = service.run()
    assert result["state"] == "unknown"
    assert result["reason"] == "observation_report_failure"
    assert provider.cancel_calls
    # Revocation triggers cancellation; the rejected nonterminal observation then
    # triggers the existing bounded safety cancellation, never another start.
    assert adapter.calls.count("cancel") == 2
    assert adapter.calls.count("start") == 1
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_restart_after_renewal_recovers_the_already_running_output_reservation(tmp_path):
    state, execution, _, worker, _, lease = _delivery(tmp_path)
    inputs, outputs, directory = tmp_path / "inputs", tmp_path / "outputs", tmp_path / "runtime"
    inputs.mkdir(mode=0o700)
    outputs.mkdir(mode=0o700)
    adapter = FakeAdapter()
    adapter.development_output_root = outputs / "development-only-cpu-outputs" / "workspaces"
    profile = DevelopmentOutputProfile(outputs, 1024, 10)
    with DevelopmentCpuRuntime(
        directory,
        inputs,
        ApiChannel(state),
        adapter,
        worker_id=worker["id"],
        development_profile=profile,
    ) as first:
        assert first.start(lease["id"], lease["epoch"]).phase == "running"
    execution.renew_execution_lease(worker, lease["id"], "renew", "renew", lease["epoch"])
    with DevelopmentCpuRuntime(
        directory,
        inputs,
        ApiChannel(state),
        adapter,
        worker_id=worker["id"],
        development_profile=profile,
    ) as recovered:
        assert recovered.start(lease["id"], lease["epoch"]).phase == "running"
    assert adapter.calls.count("start") == 1


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


class _SyntheticWorkerCrash(Exception):
    pass


def _crashed_running_service(tmp_path, state, grant, adapter):
    service = _service(
        tmp_path,
        state,
        grant,
        adapter=adapter,
        max_ticks=2,
        sleep=lambda _: (_ for _ in ()).throw(_SyntheticWorkerCrash()),
    )
    with pytest.raises(_SyntheticWorkerCrash):
        service.run()


def test_restart_recovers_before_renewing_and_never_reacquires_or_restarts(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    _crashed_running_service(tmp_path, state, grant, adapter)

    channel = ApiChannel(state)
    result = _service(tmp_path, state, grant, channel=channel, adapter=adapter, max_ticks=2).run()

    assert result["state"] == "unknown"
    assert "/worker/leases/renew" in channel.paths
    assert "/worker/leases" not in channel.paths
    assert adapter.calls.count("start") == 1


@pytest.mark.parametrize("authority", ["expired", "revoked"])
def test_restart_reconciles_expired_or_revoked_grant_without_a_new_start(tmp_path, authority):
    state, execution, owner, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    _crashed_running_service(tmp_path, state, grant, adapter)
    if authority == "expired":
        with _connect(state / "tessera.sqlite3") as connection:
            connection.execute(
                "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (grant["id"],),
            )
    else:
        execution.revoke_execution_grant(owner, grant["id"], "restart-revoke", "restart-revoke", 1)
    adapter.cancel_unknown = True
    channel = ApiChannel(state)

    result = _service(tmp_path, state, grant, channel=channel, adapter=adapter).run()

    assert result["state"] == "unknown"
    assert result["quarantined"] is False
    assert "/worker/leases" not in channel.paths
    assert adapter.calls.count("start") == 1
    assert adapter.calls.count("cancel") >= 1
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_restart_control_outage_cancels_boundedly_without_new_authority(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    _crashed_running_service(tmp_path, state, grant, adapter)
    channel = ApiChannel(state)
    channel.disconnected = True

    result = _service(tmp_path, state, grant, channel=channel, adapter=adapter).run()

    assert result == {
        "state": "unknown",
        "quarantined": False,
        "reason": "observation_report_failure",
    }
    assert "/worker/leases" not in channel.paths
    assert adapter.calls.count("start") == 1
    assert adapter.calls.count("cancel") >= 1
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


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
    result = service.run()
    if failure == "revocation":
        # The synthetic adapter writes no declared output; confirmed cancellation
        # is retained, while the undeclared/missing output stays unreclaimed.
        assert result["state"] == "unknown"
    else:
        assert result == {
            "state": "unknown",
            "quarantined": False,
            "reason": "observation_report_failure",
        }
    assert adapter.calls.count("cancel") >= 1
    if failure == "disconnect":
        assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))
