"""Development worker orchestration tests; human authority is fixture setup only."""

from __future__ import annotations

import json
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
from tessera_worker.supervisor import SupervisorError


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

    result = service.run()
    # A running observation that could not be delivered no longer stops the container (ADR
    # 0054): the run went on, and it was the renewal that then failed over the same lost
    # connection (authority is proven by renewals), which cancels. The cancel's confirmed exit
    # is queued in the durable outbox for the poller to redeliver, and it supersedes the stale
    # undelivered running observation: control must never see an old running after the exit.
    # (Before, the exit was dropped and the stale running stayed pending, so control could not
    # resolve the attempt.)
    assert result["reason"] == "heartbeat_or_renewal_failure"
    assert adapter.calls.count("cancel") == 1
    with _connect(tmp_path / "worker" / "runtime" / "runtime-observations.sqlite3") as connection:
        rows = [
            tuple(row)
            for row in connection.execute(
                "SELECT observed_state, completed, abandoned FROM reports ORDER BY id"
            )
        ]
    assert rows == [("running", 0, 1), ("exited", 0, 0)]
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

    result = service.run()
    assert (result["state"], result["reason"]) == (
        "report_pending",
        "observation_delivery_pending",
    )
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
    assert result["reason"] == "cancel_lease_denied_unconfirmed"
    assert provider.cancel_calls
    # Revocation triggers the single supervisor cancellation, never another start.
    assert adapter.calls.count("cancel") == 1
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
        "state": "report_pending",
        "quarantined": False,
        "reason": "observation_delivery_pending",
        "delivery_pending": True,
        "retry_after_seconds": 2.0,
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
        assert result["state"] == "report_pending"
        assert result["reason"] == "observation_delivery_pending"
    assert adapter.calls.count("cancel") >= 1
    if failure == "disconnect":
        assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


@pytest.mark.parametrize("with_path", [False, True])
def test_collect_supervisor_error_reports_bounded_pathless_detail(tmp_path, monkeypatch, with_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    leaked = str(tmp_path / "outputs" / "secret-dir" / "out.bin")
    message = f"cannot seal {leaked}\nsecond line " + "x" * 400 if with_path else "unsafe output"

    def failing_collect(self, result, **kwargs):
        raise SupervisorError(message)

    monkeypatch.setattr(DevelopmentCpuRuntime, "collect", failing_collect)
    adapter = FakeAdapter()

    def exit_provider(_):
        adapter.state = "exited"

    service = _service(tmp_path, state, grant, adapter=adapter, max_ticks=5, sleep=exit_provider)
    result = service.run()
    assert result["reason"] == "quarantine_failed", result
    assert result["state"] == "unknown"
    assert result["quarantined"] is False
    detail = result["detail"]
    assert detail.startswith("SupervisorError: ")
    assert len(detail) <= 200
    assert "\n" not in detail
    assert str(tmp_path) not in detail
    assert "secret-dir" not in detail
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_cli_surfaces_supervisor_error_as_bounded_diagnostic(tmp_path, monkeypatch, capsys):
    from tessera_worker import cli

    class Boom:
        def __init__(self, config):
            pass

        def run(self):
            raise SupervisorError(f"failed at {tmp_path}/x\nmore")

    monkeypatch.setattr(cli, "DevelopmentWorkerService", Boom)
    monkeypatch.setattr(
        "sys.argv",
        [
            "tessera-worker", "development-run", "--control-url", "http://127.0.0.1:1",
            "--grant-id", "g", "--credential-file", str(tmp_path / "c"),
            "--workspace-root", str(tmp_path / "w"),
            "--development-output-root", str(tmp_path / "o"), "--allowed-image", "img",
        ],
    )  # fmt: skip
    assert cli.main() == 1
    err = capsys.readouterr().err
    assert err.startswith("development worker failed: SupervisorError: ")
    assert str(tmp_path) not in err
    assert "Traceback" not in err


class _InspectingAdapter(FakeAdapter):
    """FakeAdapter plus the docker-inspect seam the run receipt reads."""

    def __init__(self):
        super().__init__()
        self.operation = None

    def observe(self, operation):
        self.operation = operation
        return super().observe(operation)

    def start(self, operation):
        Path(operation.development_output.directory, "result.txt").write_text("synthetic output")
        return super().start(operation)

    def run(self, arguments):
        from datetime import UTC, datetime, timedelta

        self.now = getattr(self, "now", None) or datetime.now(UTC)  # stable, like Docker
        now = self.now
        stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"  # noqa: E731
        info = [
            {
                "Id": "container",
                "Image": "sha256:" + "e" * 64,
                "Config": {
                    "Labels": {
                        "tessera.operation": self.operation.operation_id,
                        "tessera.plan": self.operation.plan_digest,
                    }
                },
                "State": {
                    "Status": "exited",
                    "Running": False,
                    "ExitCode": 0,
                    "StartedAt": stamp(now - timedelta(milliseconds=2)),
                    "FinishedAt": stamp(now - timedelta(milliseconds=1)),
                },
            }
        ]
        return SimpleNamespace(returncode=0, stdout=__import__("json").dumps(info), stderr="")


class _ReceiptChannel(ApiChannel):
    def __init__(self, state, fail=None):
        super().__init__(state)
        self.fail = fail
        self.receipts: list[tuple[str, dict, str]] = []

    def post(self, path, body, key):
        if path.endswith("/run-receipt"):
            self.receipts.append((path, body, key))
            if self.fail is not None:
                raise self.fail
        return super().post(path, body, key)


def _exit_after_start(adapter):
    def provider(_):
        adapter.state = "exited"

    return provider


def test_confirmed_exit_posts_one_observed_receipt_that_control_accepts(tmp_path):
    from tessera_control.execution_run_receipts import ReportRunReceipt
    from tessera_worker.run_receipt import canonical_json

    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter, channel = _InspectingAdapter(), None
    channel = _ReceiptChannel(state)
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=5,
        sleep=_exit_after_start(adapter),
    )
    result = service.run()
    assert result["state"] == "exited" and result["quarantined"] is True
    assert result["run_receipt"]["status"] == "recorded"
    assert len(channel.receipts) == 1
    path, body, key = channel.receipts[0]
    assert path.endswith("/run-receipt") and path.startswith("/worker/external-operations/")
    ReportRunReceipt(**body)
    import hashlib

    canonical = canonical_json(body["receipt_payload"])
    assert hashlib.sha256(canonical.encode()).hexdigest() == body["receipt_digest"]
    assert str(tmp_path) not in canonical
    assert body["provider_id"] == "container" and body["exit_code"] == 0
    # Control verified this receipt against its own records (the post was a 201 and
    # the run reported `recorded`); the cited output digest is control's manifest digest,
    # not the worker's local seal digest.
    import sqlite3 as _sqlite3

    with _sqlite3.connect(state / "tessera.sqlite3") as connection:
        (control_digest,) = connection.execute(
            "SELECT manifest_digest FROM artifact_manifests"
        ).fetchone()
        (stored,) = connection.execute(
            "SELECT output_digest FROM execution_run_receipts"
        ).fetchone()
    assert body["output_digest"] == control_digest == stored
    assert body["receipt_payload"]["sealed_manifest_digest"] != control_digest
    assert "output_manifest_digest" not in body["receipt_payload"]
    # The receipt is posted after the exit observation and the sealed upload.
    assert channel.paths.index("/worker/observations") < channel.paths.index(path)
    assert channel.paths[-1] == path


def test_receipt_key_is_stable_across_a_restart_after_a_lost_reply(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = _InspectingAdapter()
    first_channel = _ReceiptChannel(state, fail=ConnectionError("lost reply"))
    first = _service(
        tmp_path,
        state,
        grant,
        channel=first_channel,
        adapter=adapter,
        max_ticks=5,
        sleep=_exit_after_start(adapter),
    )
    assert first.run()["reason"] == "run_receipt_failed"
    second_channel = _ReceiptChannel(state)
    second = _service(tmp_path, state, grant, channel=second_channel, adapter=adapter)
    result = second.run()
    assert result["state"] == "exited" and result["run_receipt"]["status"] == "recorded"
    # A lost reply is transient: the worker retries a bounded number of times with the SAME
    # key and body, and the restart reuses that key, so control sees one logical request.
    assert len(first_channel.receipts) == 3
    assert {item[2] for item in first_channel.receipts} == {second_channel.receipts[0][2]}
    assert {json.dumps(item[1], sort_keys=True) for item in first_channel.receipts} == {
        json.dumps(second_channel.receipts[0][1], sort_keys=True)
    }
    assert adapter.calls.count("start") == 1


@pytest.mark.parametrize("status", [409, 422, None])
def test_control_refusal_keeps_custody_and_claims_nothing(tmp_path, status):
    from tessera_worker.control_channel import ControlChannelError

    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = _InspectingAdapter()
    failure = (
        ControlChannelError("control rejected " + str(tmp_path) + "x" * 400, status)
        if status
        else RuntimeError(f"boom {tmp_path}")
    )
    channel = _ReceiptChannel(state, fail=failure)
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=5,
        sleep=_exit_after_start(adapter),
    )
    result = service.run()
    assert result["state"] == "unknown" and result["quarantined"] is False
    # A permanent control refusal (4xx) is terminal evidence; any other failure here is a
    # non-retryable local fault that keeps the generic reason. Neither retries.
    if status:
        assert result["reason"] == "run_receipt_refused" and result["terminal"] is True
    else:
        assert result["reason"] == "run_receipt_failed" and "terminal" not in result
    assert len(result["detail"]) <= 200 and str(tmp_path) not in result["detail"]
    assert "receipt" not in result and "run_receipt" not in result
    assert len(channel.receipts) == 1  # one attempt, no retry loop
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_no_receipt_when_collection_fails_or_exit_is_unconfirmed(tmp_path, monkeypatch):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = _InspectingAdapter()
    channel = _ReceiptChannel(state)
    # Unconfirmed: the container never exits within the tick budget.
    service = _service(tmp_path, state, grant, channel=channel, adapter=adapter, max_ticks=1)
    assert service.run()["state"] == "unknown"
    assert channel.receipts == []

    def failing_collect(self, result, **kwargs):
        raise SupervisorError("unsafe output")

    monkeypatch.setattr(DevelopmentCpuRuntime, "collect", failing_collect)
    (tmp_path / "second").mkdir()
    state2, _, _, _, grant2 = _unleased_delivery(tmp_path / "second")
    adapter2 = _InspectingAdapter()
    channel2 = _ReceiptChannel(state2)
    service2 = _service(
        tmp_path / "second",
        state2,
        grant2,
        channel=channel2,
        adapter=adapter2,
        max_ticks=5,
        sleep=_exit_after_start(adapter2),
    )
    assert service2.run()["reason"] == "quarantine_failed"
    assert channel2.receipts == []


def test_adapter_without_an_inspect_seam_posts_no_receipt(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class OutputOnly(FakeAdapter):
        def start(self, operation):
            Path(operation.development_output.directory, "result.txt").write_text("x")
            return super().start(operation)

    adapter = OutputOnly()
    channel = _ReceiptChannel(state)
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=5,
        sleep=_exit_after_start(adapter),
    )
    result = service.run()
    assert result["state"] == "exited" and "run_receipt" not in result
    assert channel.receipts == []


def test_stale_sealed_digest_from_another_operation_is_never_posted(tmp_path, monkeypatch):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = _InspectingAdapter()
    channel = _ReceiptChannel(state)
    real_collect = DevelopmentCpuRuntime.collect

    def collect_then_go_stale(self, result, **kwargs):
        receipt = real_collect(self, result, **kwargs)
        # A reused runtime would still hold the previous operation's digest.
        self._sealed_operation_id = "operation_from_a_previous_run"
        return receipt

    monkeypatch.setattr(DevelopmentCpuRuntime, "collect", collect_then_go_stale)
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=5,
        sleep=_exit_after_start(adapter),
    )
    result = service.run()
    assert result["state"] == "unknown" and result["reason"] == "run_receipt_failed"
    assert result["quarantined"] is False
    assert channel.receipts == []


class RenewalTimes(ApiChannel):
    """Records the fake-clock time of every renewal request."""

    def __init__(self, state, now):
        super().__init__(state)
        self.now = now
        self.renewals: list[float] = []

    def post(self, path, body, key):
        if path == "/worker/leases/renew":
            self.renewals.append(self.now[0])
        return super().post(path, body, key)


def _timed_service(tmp_path, state, grant, *, poll_seconds, max_ticks=12):
    import dataclasses

    now = [0.0]
    sleeps: list[float] = []
    channel = RenewalTimes(state, now)

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds + 0.5  # the requested sleep plus a control round trip

    service = _service(tmp_path, state, grant, channel=channel, max_ticks=max_ticks, sleep=sleep)
    service.config = dataclasses.replace(
        service.config, renew_every_ticks=15, poll_seconds=poll_seconds
    )
    service.clock = lambda: now[0]
    return service, channel, sleeps


@pytest.mark.parametrize("poll_seconds", [2.0, 25.0])
def test_running_operation_renews_on_elapsed_time_inside_the_lease(tmp_path, poll_seconds):
    """Neither a 2 s poll with round trips nor a 25 s poll may let the 30 s lease lapse."""
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    service, channel, sleeps = _timed_service(tmp_path, state, grant, poll_seconds=poll_seconds)
    assert service.run()["reason"] == "tick_limit_cancellation_requested"
    times = channel.renewals
    assert times[0] == 0.0 and len(times) >= 2
    # Each renewal follows the previous one by at most the interval plus one round trip,
    # far inside the 30 s lease; counting 15 ticks alone took 37.5 s at a 2 s poll.
    assert max(b - a for a, b in zip(times, times[1:], strict=False)) <= 10.5
    assert max(sleeps) <= 10.0


def test_recovered_lease_is_renewed_before_the_first_sleep(tmp_path):
    """recover() resumes a lease that may have seconds left; it is renewed at once."""
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    _crashed_running_service(tmp_path, state, grant, adapter)
    now = [0.0]
    channel = RenewalTimes(state, now)
    renewals_seen_at_sleep: list[int] = []
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=2,
        sleep=lambda _seconds: renewals_seen_at_sleep.append(len(channel.renewals)),
    )
    service.clock = lambda: now[0]
    service.run()
    assert "/worker/leases" not in channel.paths  # recovered, never re-leased
    assert renewals_seen_at_sleep and renewals_seen_at_sleep[0] >= 1


def test_renewal_interval_must_stay_inside_the_lease(tmp_path):
    import dataclasses

    from tessera_worker.service import DevelopmentWorkerError

    state, _, _, _, grant = _unleased_delivery(tmp_path)
    service = _service(tmp_path, state, grant)

    for bad in (0, 25):
        with pytest.raises((DevelopmentWorkerError, ValueError)):
            dataclasses.replace(service.config, renew_after_seconds=bad)


@pytest.mark.parametrize(
    ("configured", "default", "expected"),
    [(None, "1001:1002", "1001:1002"), (None, None, None), ("2000:2001", "1001:1002", "2000:2001")],
)
def test_service_builds_the_adapter_with_the_operator_or_platform_container_user(
    tmp_path, monkeypatch, configured, default, expected
):
    import dataclasses

    from tessera_worker import service as service_module

    state, _, _, _, grant, _ = _delivery(tmp_path)
    built = {}

    class Capture:
        def __init__(self, *args, **kwargs):
            built.update(kwargs)
            raise RuntimeError("stop after adapter construction")

    monkeypatch.setattr(service_module, "DockerCpuAdapter", Capture)
    monkeypatch.setattr(service_module, "default_container_user", lambda: default)
    service = _service(tmp_path, state, grant)
    service.config = dataclasses.replace(service.config, container_user=configured)
    service.adapter = None
    with pytest.raises(RuntimeError, match="stop after"):
        service.run()
    assert built["container_user"] == expected
    assert built["allow_root_container_user"] is False


def test_a_fresh_grant_workspace_heartbeats_with_new_keys_and_refreshes_liveness(tmp_path):
    """The poller gives each grant a new workspace under one credential: no key replays."""
    import shutil

    state, _, _, _, grant = _unleased_delivery(tmp_path)
    channel = ApiChannel(state)
    first = _service(tmp_path, state, grant, channel=channel)
    first._heartbeat()
    shutil.rmtree(tmp_path / "worker")  # the next grant starts in a fresh workspace
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE worker_registry SET last_seen_at='2000-01-01T00:00:00Z'")

    second = _service(tmp_path, state, grant, channel=channel)
    second._heartbeat()

    heartbeat_keys = [key for path, key in channel.keys if path == "/worker/heartbeat"]
    assert len(heartbeat_keys) == 2 and heartbeat_keys[0] != heartbeat_keys[1]
    with _connect(state / "tessera.sqlite3") as connection:
        seen = connection.execute("SELECT last_seen_at FROM worker_registry").fetchone()[0]
    assert seen > "2026"  # a real beat, not a replay of the earlier stored response


def test_same_workspace_replays_a_pending_heartbeat_key_across_journal_instances(tmp_path):
    from tessera_worker.service import HeartbeatIntentJournal

    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    first = HeartbeatIntentJournal(root).key()
    assert HeartbeatIntentJournal(root).key() == first  # restart before complete(): replay
    HeartbeatIntentJournal(root).complete()
    assert HeartbeatIntentJournal(root).key() != first  # after complete(): a new beat


def test_a_missing_journal_id_row_is_recreated_without_reusing_old_keys(tmp_path):
    import sqlite3

    from tessera_worker.service import HeartbeatIntentJournal

    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    journal = HeartbeatIntentJournal(root)
    first = journal.key()
    journal.complete()
    with sqlite3.connect(root / "heartbeats.sqlite3") as connection:
        connection.execute("DELETE FROM heartbeat_journal")
    reopened = HeartbeatIntentJournal(root)
    assert reopened.journal_id and reopened.journal_id != journal.journal_id
    assert reopened.key() != first
