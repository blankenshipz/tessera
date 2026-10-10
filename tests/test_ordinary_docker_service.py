"""Real private SQLite/service/adapter, synthetic backend and Docker effects only."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_ordinary_docker_execution import setup as adapter_setup_fixture  # noqa: F401

from tessera_worker.ordinary_docker_contract import digest
from tessera_worker.ordinary_docker_execution import OrdinaryDockerExecutionAdapter
from tessera_worker.ordinary_docker_messages import (
    DockerClaim,
    DockerControlReadback,
    DockerDeliveryAck,
    NativeDockerManifest,
    NativeDockerReport,
)
from tessera_worker.ordinary_docker_service import (
    DockerServiceError,
    DockerWorkerConfig,
    OrdinaryDockerWorkerService,
)
from tessera_worker.ordinary_docker_worker import main, request_stop


class FakeBackend:
    def __init__(self, s):
        self.s = s
        self.claim_value = DockerClaim(dispatch_id="dispatch", package=s.package, lease=s.lease[0])
        self.queue = [self.claim_value]
        self.claims, self.beats, self.renewals, self.deliveries = [], [], [], []
        self.claim_replies, self.beat_replies, self.acks = {}, {}, {}
        self.lose_claim, self.lose_beat, self.lose_report, self.lose_output = (
            False,
            False,
            False,
            False,
        )
        self.read_acks = True
        self.desired = "run"
        self.bad_ack = None

    def _timeout(self, value):
        assert 0 < value <= 10

    def heartbeat(self, worker_id, key, *, timeout_seconds):
        self._timeout(timeout_seconds)
        assert worker_id == "worker"
        self.beats.append(key)
        self.beat_replies.setdefault(key, self.s.now[0])
        if self.lose_beat:
            raise ConnectionError("synthetic lost heartbeat")
        return self.beat_replies[key]

    def claim(self, worker_id, target, key, *, timeout_seconds):
        self._timeout(timeout_seconds)
        assert worker_id == "worker" and target == self.s.target
        self.claims.append(key)
        if key not in self.claim_replies:
            self.claim_replies[key] = self.queue.pop(0) if self.queue else None
        if self.lose_claim:
            raise ConnectionError("synthetic lost claim")
        return self.claim_replies[key]

    def current_authority(self, claim, *, timeout_seconds):
        self._timeout(timeout_seconds)
        assert claim.package.package_digest == self.claim_value.package.package_digest
        if self.s.revoked[0] or self.s.lease[0].expires_at <= self.s.now[0]:
            raise PermissionError("synthetic authority denial")
        return self.s.lease[0]

    def renew(self, claim, key, *, timeout_seconds):
        self._timeout(timeout_seconds)
        self.renewals.append((key, claim.package.package_digest))
        self.s.lease[0] = self.s.lease[0].model_copy(
            update={
                "expires_at": min(
                    self.s.now[0] + timedelta(seconds=30), claim.package.hard_expires_at
                )
            }
        )
        return self.s.lease[0]

    def _deliver(self, kind, claim, value, key):
        self.deliveries.append((kind, key, value.model_dump_json()))
        payload_digest = digest(value.model_dump(mode="json"))
        ack = DockerDeliveryAck(
            operation_id=claim.package.operation_id,
            key=key,
            payload_digest=payload_digest,
            manifest_id="manifest_real_protocol_fixture" if kind == "output" else None,
            custody="quarantined" if kind == "output" else None,
        )
        self.acks[key] = ack
        if self.bad_ack:
            return ack.model_copy(update=self.bad_ack)
        if kind == "output" and self.lose_output or kind != "output" and self.lose_report:
            raise ConnectionError("synthetic lost delivery acknowledgement")
        return ack

    def report(self, claim, value, key, *, timeout_seconds):
        self._timeout(timeout_seconds)
        return self._deliver("report", claim, value, key)

    def cancel(self, claim, value, key, *, timeout_seconds):
        self._timeout(timeout_seconds)
        assert value.stop_requested
        return self._deliver("cancel", claim, value, key)

    def output(self, claim, value, outputs, key, *, timeout_seconds):
        self._timeout(timeout_seconds)
        assert outputs[0].content == b"result385\n"
        assert value.outputs[0].sha256 == hashlib.sha256(outputs[0].content).hexdigest()
        assert value.provider == "docker" and value.container_id == "c" * 64
        return self._deliver("output", claim, value, key)

    def read(self, claim, *, timeout_seconds):
        self._timeout(timeout_seconds)
        return DockerControlReadback(
            operation_id=claim.package.operation_id,
            desired=self.desired,
            acknowledgements=tuple(self.acks.values()) if self.read_acks else (),
        )


@pytest.fixture
def service_setup(request):
    setup = request.getfixturevalue("adapter_setup_fixture")
    backend = FakeBackend(setup)
    workspace = setup.root / "service"
    config = DockerWorkerConfig(
        "worker",
        "https://control.example:8443",
        Path("/private/runtime/worker.header"),
        Path("/private/runtime/control-ca.pem"),
        workspace,
        setup.target,
    )
    opened = []

    def factory(claim, journal, authority):
        assert claim.package.worker_principal_id == "worker"
        return OrdinaryDockerExecutionAdapter(
            journal,
            setup.target,
            setup.transport,
            current_authority=authority,
            now=lambda: setup.now[0],
            monotonic=lambda: 0,
        )

    def create(*, sleep=lambda _: None, configuration=config):
        service = OrdinaryDockerWorkerService(
            configuration, backend, adapter_factory=factory, now=lambda: setup.now[0], sleep=sleep
        )
        opened.append(service)
        return service

    yield setup, backend, config, create
    for service in opened:
        service.close()


def test_normal_service_native_report_output_and_acknowledged_custody(service_setup):
    s, backend, config, create = service_setup
    service = create()
    assert service.step().status == "running"
    s.transport.ready = True
    result = service.step()
    assert (
        result.status == "completed"
        and result.termination_confirmed
        and result.control_acknowledged
    )
    assert s.transport.creates == s.transport.starts == 1
    assert any(kind == "output" for kind, _, _ in backend.deliveries)
    for _, _, payload in backend.deliveries:
        value = json.loads(payload)
        assert "job_uid" not in value and "pod_uids" not in value
        assert value["container_id"] == "c" * 64 and value["provider"] == "docker"
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT active FROM operations").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM outbox WHERE ack IS NULL").fetchone() == (0,)
    assert service.step().status == "idle"


def test_claim_intent_before_lost_reply_replays_same_key_without_launch(service_setup):
    s, backend, _, create = service_setup
    service = create()
    backend.lose_claim = True
    assert service.step().status == "recovery_required"
    assert s.transport.creates == s.transport.starts == 0
    service.close()
    backend.lose_claim = False
    restarted = create()
    assert restarted.step().status == "running"
    assert backend.claims[0] == backend.claims[1]
    assert s.transport.creates == s.transport.starts == 1


def test_lost_report_reply_restarts_with_exact_original_payload_and_key(service_setup):
    s, backend, _, create = service_setup
    backend.lose_report = True
    backend.read_acks = False
    service = create()
    service.step()
    delivery = backend.deliveries[-1]
    service.close()
    s.now[0] += timedelta(seconds=1)
    backend.lose_report = False
    restarted = create()
    assert restarted.step().status == "running"
    assert backend.deliveries[-1] == delivery
    assert s.transport.creates == s.transport.starts == 1


def test_query_recovers_exact_ack_without_repeating_committed_delivery(service_setup):
    s, backend, _, create = service_setup
    backend.lose_report = True
    service = create()
    service.step()
    assert len(backend.deliveries) == 1
    backend.lose_report = False
    assert service.step().status == "running"
    assert len(backend.deliveries) == 1
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE ack IS NULL").fetchone() == (0,)


def test_lost_output_ack_retains_custody_and_recovers_without_relaunch(service_setup):
    s, backend, _, create = service_setup
    service = create()
    service.step()
    s.transport.ready = backend.lose_output = True
    assert service.step().status == "report_draining"
    original = next(item for item in backend.deliveries if item[0] == "output")
    backend.read_acks = False
    backend.lose_output = False
    service.close()
    restarted = create()
    assert restarted.step().status == "completed"
    outputs = [item for item in backend.deliveries if item[0] == "output"]
    assert outputs == [original, original]
    assert s.transport.creates == s.transport.starts == 1


@pytest.mark.parametrize(
    "failure", ["revoke", "lease_expiry", "hard_expiry", "control_cancel", "local_stop"]
)
def test_active_service_stops_original_container_on_fences(service_setup, failure):
    s, backend, _, create = service_setup
    service = create()
    service.step()
    if failure == "revoke":
        s.revoked[0] = True
    elif failure == "lease_expiry":
        s.now[0] = s.lease[0].expires_at
    elif failure == "hard_expiry":
        s.now[0] = s.package.hard_expires_at
    elif failure == "control_cancel":
        backend.desired = "cancel_requested"
    else:
        service.request_stop()
    result = service.step()
    assert result.status == "stopped" and result.termination_confirmed
    assert s.transport.signals[-1] == ("c" * 64, "SIGKILL")
    assert s.transport.creates == s.transport.starts == 1
    assert backend.renewals == []
    assert any(kind == "cancel" for kind, _, _ in backend.deliveries)


def test_revoked_restart_still_stops_owned_container_without_replacement(service_setup):
    s, backend, _, create = service_setup
    service = create()
    service.step()
    service.close()
    s.revoked[0] = True
    result = create().step()
    assert result.status == "stopped" and result.termination_confirmed
    assert s.transport.creates == s.transport.starts == 1


def test_uncertain_stop_is_not_termination_and_local_intent_survives_restart(service_setup):
    s, backend, _, create = service_setup
    service = create()
    service.step()
    service.request_stop()
    s.transport.lose_stop = True
    result = service.step()
    assert result.status == "recovery_required" and not result.termination_confirmed
    service.close()
    s.transport.lose_stop = False
    result = create().step()
    assert result.status == "stopped" and result.termination_confirmed
    assert s.transport.creates == s.transport.starts == 1


def test_service_renewal_uses_adapter_durable_key_and_keeps_package_sealed(service_setup):
    s, backend, _, create = service_setup
    service = create()
    service.step()
    original = s.package.package_digest
    s.now[0] += timedelta(seconds=10)
    assert service.step().status == "running"
    assert len(backend.renewals) == 1 and backend.renewals[0][1] == original
    assert backend.renewals[0][0].startswith("ordinary-docker-renew-")
    assert s.lease[0].expires_at > s.package.hard_expires_at - timedelta(seconds=100)
    assert s.package.package_digest == original


def test_service_started_intent_without_adapter_row_never_launches_on_recovery(service_setup):
    s, backend, _, create = service_setup
    service = create()
    service._claim()
    with sqlite3.connect(service.path) as db:
        db.execute("UPDATE operations SET started=1")
    service.close()
    result = create().step()
    assert result.status == "running" or result.status == "recovery_required"
    assert s.transport.creates == s.transport.starts == 0
    assert not result.termination_confirmed


def test_ambiguous_start_recovers_actual_id_by_observation_without_second_start(service_setup):
    s, backend, _, create = service_setup
    s.transport.lose_start = True
    service = create()
    service.step()
    service.close()
    result = create().step()
    assert result.container_id == "c" * 64 and result.status == "running"
    assert s.transport.creates == s.transport.starts == 1


def test_worker_workspace_lock_and_binding_are_exclusive(service_setup):
    s, backend, config, create = service_setup
    service = create()
    with pytest.raises(DockerServiceError, match="already supervised"):
        create()
    service.close()
    with pytest.raises(DockerServiceError, match="identity/target/endpoint"):
        create(configuration=replace(config, worker_principal_id="another"))


@pytest.mark.parametrize(
    "bad_ack",
    [
        {"operation_id": "d" * 32},
        {"payload_digest": "e" * 64},
        {"key": "another-key"},
    ],
)
def test_wrong_ack_cannot_clear_pending_delivery(service_setup, bad_ack):
    s, backend, _, create = service_setup
    backend.bad_ack = bad_ack
    backend.read_acks = False
    service = create()
    service.step()
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE ack IS NULL").fetchone() == (1,)


def test_readback_changed_container_fences_original_container(service_setup):
    s, backend, _, create = service_setup
    service = create()
    service.step()
    original = backend.read

    def changed(*args, **kwargs):
        return original(*args, **kwargs).model_copy(update={"container_id": "d" * 64})

    backend.read = changed
    result = service.step()
    assert result.status == "stopped"
    assert s.transport.signals[-1][0] == "c" * 64


def test_long_lived_driver_accepts_work_after_idle_and_then_continues(service_setup):
    s, backend, _, create = service_setup
    backend.queue = []
    sleeps = [0]

    def sleep(_):
        sleeps[0] += 1
        if sleeps[0] == 1:
            backend.queue.append(backend.claim_value)
        else:
            s.transport.ready = True

    service = create(sleep=sleep)
    assert service.run(max_cycles=4).status == "idle"
    assert sleeps[0] == 3 and s.transport.creates == s.transport.starts == 1
    assert len(set(backend.claims)) == len(backend.claims)


def test_local_stop_command_records_only_intent_and_does_not_clear_it(service_setup):
    s, backend, config, create = service_setup
    service = create()
    assert request_stop(config.workspace) == {
        "status": "stop_requested",
        "termination_confirmed": False,
    }
    assert service.step().status == "stopped"
    assert backend.claims == [] and s.transport.creates == 0
    service.close()
    assert create().step().status == "stopped"


def cli_args(action):
    return [
        action,
        "--workspace",
        "/private/worker",
        "--worker-principal-id",
        "worker",
        "--control-url",
        "https://control.example",
        "--credential-file",
        "/private/credential",
        "--control-ca-file",
        "/private/control-ca",
        "--docker-endpoint",
        "unix:///private/engine.sock",
        "--docker-daemon-id",
        "fixed-daemon",
        "--image",
        "experiment@sha256:" + "a" * 64,
    ]


@pytest.mark.parametrize("action,expected", [("check", 0), ("run", 2)])
def test_cli_references_only_and_run_fails_closed_without_binding(
    monkeypatch, capsys, action, expected
):
    def denied(*args, **kwargs):
        raise AssertionError("credential/filesystem read forbidden in CLI validation")

    monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(Path, "read_text", denied)
    if action == "run":
        import tessera_worker.ordinary_docker_worker as worker_module

        monkeypatch.setattr(
            worker_module,
            "run_worker",
            lambda *_: ({"status": "stopped", "execution_started": False}, 2),
        )
    assert main(cli_args(action)) == expected
    value = json.loads(capsys.readouterr().out)
    assert value["execution_started"] is False
    if action == "run":
        assert value["status"] == "stopped"
    assert "/private/credential" not in json.dumps(value)


def test_cli_refuses_unbounded_shell_options_and_bad_endpoint(capsys):
    with pytest.raises(SystemExit):
        main(cli_args("run") + ["--command", "arbitrary"])
    args = cli_args("check")
    args[args.index("--control-url") + 1] = "http://remote.example"
    assert main(args) == 2
    assert "remote.example" not in capsys.readouterr().out


def test_strict_native_report_and_output_reject_fake_or_unattributed_evidence(service_setup):
    s, backend, _, create = service_setup
    service = create()
    service.step()
    payload = json.loads(backend.deliveries[-1][2])
    with pytest.raises(ValidationError):
        NativeDockerReport.model_validate_json(json.dumps(payload | {"job_uid": "fake"}))
    with pytest.raises(ValidationError):
        NativeDockerReport.model_validate_json(json.dumps(payload | {"state": "succeeded"}))
    s.transport.ready = True
    service.step()
    metadata = json.loads(next(item[2] for item in backend.deliveries if item[0] == "output"))
    with pytest.raises(ValidationError):
        NativeDockerManifest.model_validate_json(json.dumps(metadata | {"model_prompt": "raw"}))


def test_claim_other_worker_and_invalid_typed_lease_never_launch(service_setup):
    s, backend, _, create = service_setup
    package = backend.claim_value.package.model_copy(update={"worker_principal_id": "other"})
    lease = backend.claim_value.lease.model_copy(update={"worker_principal_id": "other"})
    backend.queue = [DockerClaim(dispatch_id="dispatch", package=package, lease=lease)]
    assert create().step().status == "recovery_required"
    assert s.transport.creates == s.transport.starts == 0


def test_guard_image_source_is_minimal_and_keeps_separate_worker_pin():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Dockerfile.guard").read_text()
    control_base = next(
        line.split()[1]
        for line in (root / "Dockerfile").read_text().splitlines()
        if line.startswith("FROM python:")
    )
    worker_base = (
        "python:3.11-slim-bookworm@sha256:"
        "528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84"
    )
    assert "FROM " + worker_base in text
    assert control_base != worker_base
    assert "USER 65532:65532" in text
    assert "ordinary_docker_guard.py" in text and "tessera_worker.ordinary_docker_guard" in text
    assert "pip install" not in text and "docker.sock" not in text and "VOLUME" not in text
    assert "tessera_agent" not in text and "COPY src ./src" not in text


def test_lost_heartbeat_ack_restarts_with_original_durable_key(service_setup):
    s, backend, _, create = service_setup
    service = create()
    backend.lose_beat = True
    assert service.step().status == "recovery_required"
    assert s.transport.creates == 0
    service.close()
    backend.lose_beat = False
    assert create().step().status == "running"
    assert backend.beats[0] == backend.beats[1]


def test_corrupt_pending_payload_is_not_delivered_under_its_old_digest(service_setup):
    s, backend, _, create = service_setup
    service = create()
    backend.lose_report = True
    backend.read_acks = False
    service.step()
    count = len(backend.deliveries)
    with sqlite3.connect(service.path) as db:
        db.execute("UPDATE outbox SET payload='{}'")
    backend.lose_report = False
    service.step()
    assert len(backend.deliveries) == count


def test_driver_catches_sensitive_exception_without_stdout_or_raw_trace(service_setup, capsys):
    s, backend, _, create = service_setup
    service = create()

    def fail():
        raise ValueError("sensitive backend payload must never reach logs")

    service.step = fail
    assert service.run(max_cycles=1).status == "recovery_required"
    assert capsys.readouterr().out == ""
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT value FROM meta WHERE key='stop_requested'").fetchone() == (
            "true",
        )
