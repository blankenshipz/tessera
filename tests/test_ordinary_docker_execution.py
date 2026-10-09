"""Synthetic Docker effects, controlled authority/clock, real temporary SQLite custody."""

from __future__ import annotations

import copy
import io
import json
import sqlite3
import tarfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tessera_worker.execution_contracts import ExecutionAdapter, PreflightRequest
from tessera_worker.ordinary_docker_contract import DockerLease, DockerPackage, DockerTarget
from tessera_worker.ordinary_docker_execution import (
    DockerExecutionError,
    OrdinaryDockerExecutionAdapter,
    archive_files,
)


def tar(entries, *, kind=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in entries:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            if kind:
                item.type, item.linkname = kind, "outside"
                item.size = 0
            archive.addfile(item, io.BytesIO(content))
    return output.getvalue()


class FakeDocker:
    def __init__(self, target):
        self.target = target
        self.endpoint_digest = target.endpoint_digest
        self.item = None
        self.creates, self.starts, self.signals = 0, 0, []
        self.lose_create, self.lose_start, self.lose_stop = False, False, False
        self.ready = False
        self.output = tar([("outputs/result.txt", b"result385\n")])
        self.inspect_mutation = None

    def info(self, **_):
        return {
            "ID": self.target.daemon_id,
            "OSType": "linux",
            "Architecture": "x86_64",
            "MemoryLimit": True,
            "SwapLimit": True,
            "CpuCfsQuota": True,
            "PidsLimit": True,
            "SecurityOptions": ["name=seccomp,profile=builtin"],
        }

    def image(self, reference, **_):
        return {"RepoDigests": [reference], "Os": "linux", "Architecture": "amd64", "Config": {}}

    def create(self, name, config, **_):
        self.creates += 1
        self.item = {
            "Id": "c" * 64,
            "Name": "/" + name,
            "Config": copy.deepcopy(config),
            "HostConfig": copy.deepcopy(config["HostConfig"]),
            "Mounts": [],
            "State": {"Running": False, "Status": "created", "ExitCode": 0},
        }
        if self.lose_create:
            raise ConnectionError("synthetic unknown create")
        return {"Id": self.item["Id"]}

    def inspect(self, _identity, **_):
        item = copy.deepcopy(self.item)
        if item is not None and self.inspect_mutation:
            self.inspect_mutation(item)
        return item

    def start(self, _identity, **_):
        self.starts += 1
        self.item["State"] = {"Running": True, "Status": "running", "ExitCode": 0}
        if self.lose_start:
            raise ConnectionError("synthetic unknown start")

    def signal(self, identity, signal, **_):
        self.signals.append((identity, signal))
        if self.lose_stop:
            raise ConnectionError("synthetic unconfirmed stop")
        self.item["State"] = {
            "Running": False,
            "Status": "exited",
            "ExitCode": 0 if signal == "SIGTERM" and self.ready else 137,
        }

    def archive(self, _identity, path, **_):
        if path == "/outputs":
            return self.output
        if not self.ready:
            return None
        operation = self.item["Config"]["Labels"]["tessera.operation"]
        return tar(
            [
                (
                    "tessera-result.json",
                    json.dumps({"version": 1, "operation_id": operation, "exit_code": 0}).encode(),
                )
            ]
        )


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    target = DockerTarget(
        daemon_id="daemon", endpoint_digest="e" * 64, image="tessera-experiment@sha256:" + "f" * 64
    )
    now = [datetime(2026, 10, 5, 12, tzinfo=UTC)]
    package = DockerPackage(
        operation_id="a" * 32,
        org_id="org",
        project_id="project",
        task_id="task",
        admitted_attempt_id="attempt",
        agent_principal_id="agent",
        worker_principal_id="worker",
        grant_id="grant",
        lease_id="lease",
        epoch=2,
        target_digest=target.binding.identity_digest,
        plan_digest="b" * 64,
        hard_expires_at=now[0] + timedelta(seconds=120),
        image=target.image,
        argv=("python", "-c", "print(385)"),
        cpu_millicores=250,
        memory_bytes=128 * 1024**2,
        pids_limit=32,
        scratch_bytes=1024**2,
        max_output_bytes=1024,
        output_paths=("result.txt",),
    )
    lease = [
        DockerLease(
            id="lease",
            grant_id="grant",
            worker_principal_id="worker",
            epoch=2,
            expires_at=now[0] + timedelta(seconds=30),
            hard_expires_at=package.hard_expires_at,
        )
    ]
    revoked = [False]

    def authority(_):
        if revoked[0]:
            raise PermissionError("synthetic grant revoked")
        return lease[0]

    transport = FakeDocker(target)

    def adapter():
        return OrdinaryDockerExecutionAdapter(
            root / "docker.sqlite3",
            target,
            transport,
            current_authority=authority,
            now=lambda: now[0],
            monotonic=lambda: 0,
        )

    return SimpleNamespace(
        root=root,
        target=target,
        package=package,
        envelope=package.envelope(target),
        now=now,
        lease=lease,
        revoked=revoked,
        authority=authority,
        transport=transport,
        adapter=adapter,
    )


def test_protocol_and_bounded_config_are_distinct_from_kubernetes(setup):
    adapter = setup.adapter()
    assert isinstance(adapter, ExecutionAdapter)
    assert adapter.preflight(PreflightRequest(setup.target.binding, setup.envelope)).admissible
    config = setup.package.container_config()
    assert config["User"] == "65532:65532"
    assert config["Env"] == [] and config["HostConfig"]["NetworkMode"] == "none"
    assert config["HostConfig"]["ReadonlyRootfs"] is True
    assert config["HostConfig"]["Binds"] == [] and config["HostConfig"]["Devices"] == []
    assert config["HostConfig"]["CapDrop"] == ["ALL"]
    assert config["HostConfig"]["RestartPolicy"] == {"Name": "no"}
    assert set(config["HostConfig"]["Tmpfs"]) == {"/scratch", "/outputs"}
    assert config["HostConfig"]["Init"] is False
    assert config["Labels"]["tessera.agent"] == "agent"
    assert config["Labels"]["tessera.attempt"] == "attempt"


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "mutable:latest"),
        ("argv", ("/bin/sh", "-c", "anything")),
        ("argv", ("ssh", "host")),
        ("argv", ("python", "x" * 4097)),
        ("argv", tuple(["python"] + ["x" * 4096] * 5)),
        ("cpu_millicores", 3000),
        ("memory_bytes", 1024),
        ("pids_limit", 1000),
        ("network", "host"),
        ("output_paths", ("../secret",)),
        ("output_paths", ("a" * 101,)),
        ("max_output_bytes", 17 * 1024**2),
        ("hard_expires_at", datetime(2026, 10, 5)),
        ("host_mount", "/"),
    ],
)
def test_closed_package_rejects_unsafe_or_unbounded_work(setup, field, value):
    body = setup.package.model_dump()
    body[field] = value
    with pytest.raises(ValidationError):
        DockerPackage.model_validate(body)


def test_create_start_are_one_shot_and_restart_is_observation_owned(setup):
    assert setup.adapter().submit(setup.envelope).state == "running"
    assert setup.adapter().submit(setup.envelope).state == "running"
    assert setup.transport.creates == setup.transport.starts == 1
    assert setup.adapter().account(setup.envelope).exposure_state == "unknown"


@pytest.mark.parametrize("stage", ["create", "start"])
def test_ambiguous_effect_reconciles_exact_id_without_replacement(setup, stage):
    setattr(setup.transport, "lose_" + stage, True)
    assert setup.adapter().submit(setup.envelope).state == "unknown"
    result = setup.adapter().submit(setup.envelope)
    assert result.handle.provider_id == "c" * 64
    assert setup.transport.creates == 1
    assert setup.transport.starts == 1


def test_unknown_create_with_absent_readback_never_recreates(setup):
    setup.transport.lose_create = True
    assert setup.adapter().submit(setup.envelope).state == "unknown"
    setup.transport.item = None
    assert setup.adapter().submit(setup.envelope).state == "unknown"
    assert setup.transport.creates == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(Id="d" * 64),
        lambda item: item["Config"]["Labels"].update({"tessera.attempt": "other"}),
        lambda item: item["HostConfig"].update(Privileged=True),
        lambda item: item["HostConfig"].update(NetworkMode="host"),
        lambda item: item["HostConfig"].update(Binds=["/var/run/docker.sock:/socket"]),
        lambda item: item.update(Mounts=[{"Source": "/host"}]),
        lambda item: item["HostConfig"].update(Memory=1024),
    ],
)
def test_changed_container_or_privilege_readback_is_unknown_never_touched(setup, mutation):
    adapter = setup.adapter()
    adapter.submit(setup.envelope)
    setup.transport.inspect_mutation = mutation
    assert adapter.observe(setup.envelope).state == "unknown"
    stopped = adapter.cancel(setup.envelope)
    assert stopped.reconciliation_required and not stopped.termination_confirmed
    assert setup.transport.signals == []


@pytest.mark.parametrize("failure", ["daemon", "limits", "image", "volumes"])
def test_preflight_fails_before_create_on_missing_target_guarantees(setup, failure):
    if failure == "daemon":
        setup.transport.info = lambda **_: {"ID": "different"}
    elif failure == "limits":
        info = setup.transport.info()
        info["PidsLimit"] = False
        setup.transport.info = lambda **_: info
    else:
        image = setup.transport.image(setup.target.image)
        if failure == "image":
            image["RepoDigests"] = []
        else:
            image["Config"]["Volumes"] = {"/host": {}}
        setup.transport.image = lambda *_, **__: image
    result = setup.adapter().preflight(PreflightRequest(setup.target.binding, setup.envelope))
    assert not result.admissible
    assert setup.transport.creates == 0


def test_stop_is_persistent_and_acknowledgement_is_not_termination(setup):
    adapter = setup.adapter()
    adapter.submit(setup.envelope)
    setup.transport.lose_stop = True
    result = adapter.cancel(setup.envelope)
    assert result.requested and result.reconciliation_required
    with pytest.raises(DockerExecutionError, match="fenced"):
        setup.adapter().submit(setup.envelope)
    setup.transport.lose_stop = False
    assert setup.adapter().cancel(setup.envelope).termination_confirmed
    assert setup.transport.creates == 1


class RenewChannel:
    def __init__(self, setup):
        self.s = setup
        self.calls = []
        self.lose = False
        self.responses = {}

    def post(self, path, body, key):
        self.calls.append((path, body, key))
        if key not in self.responses:
            if path == "/worker/heartbeat":
                self.responses[key] = {"heartbeat_at": self.s.now[0].isoformat()}
            else:
                self.s.lease[0] = self.s.lease[0].model_copy(
                    update={
                        "expires_at": min(
                            self.s.now[0] + timedelta(seconds=30), self.s.package.hard_expires_at
                        )
                    }
                )
                self.responses[key] = self.s.lease[0].model_dump(mode="json")
        if self.lose:
            raise ConnectionError("synthetic lost acknowledgement")
        return self.responses[key]


def test_renewal_persists_key_before_call_and_restart_replays_committed_reply(setup):
    adapter, channel = setup.adapter(), RenewChannel(setup)
    adapter.submit(setup.envelope)
    setup.now[0] += timedelta(seconds=10)
    channel.lose = True
    with pytest.raises(ConnectionError):
        adapter.renew(setup.envelope, channel)
    with sqlite3.connect(adapter.journal) as db:
        assert db.execute("SELECT completed FROM docker_renewals").fetchone() == (0,)
    setup.now[0] += timedelta(seconds=21)  # initial cached deadline elapsed, control query current
    channel.lose = False
    receipt = setup.adapter().renew(setup.envelope, channel)
    assert channel.calls[0] == channel.calls[1]
    assert receipt.epoch == 2 and receipt.hard_expires_at == setup.package.hard_expires_at
    assert setup.package.package_digest == setup.envelope.package_digest


@pytest.mark.parametrize("failure", ["expiry", "hard_expiry", "revoked", "epoch", "stop", "clock"])
def test_no_renewal_revives_or_widens_invalid_authority(setup, failure):
    adapter, channel = setup.adapter(), RenewChannel(setup)
    adapter.submit(setup.envelope)
    if failure == "expiry":
        setup.now[0] += timedelta(seconds=30)
    elif failure == "hard_expiry":
        setup.now[0] = setup.package.hard_expires_at
    elif failure == "revoked":
        setup.revoked[0] = True
    elif failure == "epoch":
        setup.lease[0] = setup.lease[0].model_copy(update={"epoch": 3})
    elif failure == "clock":
        setup.now[0] -= timedelta(seconds=1)
    else:
        adapter.cancel(setup.envelope)
    with pytest.raises((DockerExecutionError, PermissionError)):
        adapter.renew(setup.envelope, channel)
    assert channel.calls == []


def test_renewal_stop_race_does_not_commit_ack_as_authority(setup):
    adapter, channel = setup.adapter(), RenewChannel(setup)
    adapter.submit(setup.envelope)
    original = channel.post

    def raced(*args):
        result = original(*args)
        adapter.cancel(setup.envelope)
        return result

    channel.post = raced
    with pytest.raises(DockerExecutionError):
        adapter.renew(setup.envelope, channel)
    with sqlite3.connect(adapter.journal) as db:
        assert db.execute("SELECT completed FROM docker_renewals").fetchone() == (0,)


def test_output_capture_is_durable_hashed_and_no_fake_kubernetes_identity(setup):
    adapter = setup.adapter()
    adapter.submit(setup.envelope)
    setup.transport.ready = True
    outputs = adapter.collect_outputs(setup.envelope)
    assert outputs[0].content == b"result385\n"
    manifest = adapter.output_manifest(setup.envelope)
    assert manifest["provider"] == "docker" and manifest["container_id"] == "c" * 64
    assert (
        manifest["admitted_attempt_id"] == "attempt" and manifest["agent_principal_id"] == "agent"
    )
    assert "job_uid" not in manifest and "pod_uids" not in manifest
    assert setup.adapter().collect_outputs(setup.envelope) == outputs
    assert setup.adapter().collect(setup.envelope).output_paths == ("result.txt",)
    assert adapter.observe(setup.envelope).termination_confirmed


def test_lost_capture_stop_reply_keeps_bytes_for_exact_restart_recovery(setup):
    adapter = setup.adapter()
    adapter.submit(setup.envelope)
    setup.transport.ready = setup.transport.lose_stop = True
    with pytest.raises(ConnectionError):
        adapter.collect_outputs(setup.envelope)
    setup.transport.lose_stop = False
    # tmpfs need not be read again: already-committed custody snapshot is authoritative.
    setup.transport.output = None
    assert setup.adapter().collect_outputs(setup.envelope)[0].content == b"result385\n"
    assert setup.transport.creates == setup.transport.starts == 1


@pytest.mark.parametrize(
    "entries,kind,message",
    [
        ([("../host", b"x")], None, "unsafe"),
        ([("outputs/result.txt", b"x")], tarfile.SYMTYPE, "type"),
        ([("outputs/result.txt", b"x")], tarfile.LNKTYPE, "type"),
        ([("outputs/result.txt", b"x")], tarfile.FIFOTYPE, "type"),
        ([("outputs/result.txt", b"x"), ("outputs/result.txt", b"x")], None, "replay"),
        ([("outputs/result.txt", b"x" * 1025)], None, "byte"),
    ],
)
def test_archive_negative_cases_are_rejected_without_extraction(entries, kind, message):
    with pytest.raises(DockerExecutionError, match=message):
        archive_files(tar(entries, kind=kind), max_bytes=1024)


def test_undeclared_outputs_never_become_manifest(setup):
    adapter = setup.adapter()
    adapter.submit(setup.envelope)
    setup.transport.ready = True
    setup.transport.output = tar([("outputs/undeclared", b"x")])
    with pytest.raises(DockerExecutionError, match="declared"):
        adapter.collect_outputs(setup.envelope)
    assert setup.transport.signals == []


def test_watch_renews_without_desktop_and_stops_on_revocation(setup):
    adapter, channel = setup.adapter(), RenewChannel(setup)
    count = [0]

    def sleep(_):
        setup.now[0] += timedelta(seconds=5)
        count[0] += 1
        if count[0] == 3:
            setup.revoked[0] = True

    result = adapter.watch(setup.envelope, channel, sleep=sleep)
    assert result.termination_confirmed and result.state == "failed"
    assert sum(path == "/worker/leases/renew" for path, _, _ in channel.calls) == 1
    assert setup.transport.creates == setup.transport.starts == 1


def test_watch_completes_capture_and_preserves_hashes(setup):
    adapter, channel = setup.adapter(), RenewChannel(setup)

    def sleep(_):
        setup.transport.ready = True
        setup.now[0] += timedelta(seconds=1)

    assert adapter.watch(setup.envelope, channel, sleep=sleep).state == "succeeded"
    assert adapter.output_manifest(setup.envelope)["outputs"][0]["size"] == 10


def test_changed_package_same_operation_and_private_journal_requirements(setup, tmp_path):
    adapter = setup.adapter()
    adapter.submit(setup.envelope)
    altered = setup.package.model_copy(update={"argv": ("python", "other")})
    with pytest.raises(DockerExecutionError, match="fenced"):
        adapter.submit(altered.envelope(setup.target))
    with pytest.raises(DockerExecutionError, match="immutable"):
        adapter.submit(replace(setup.envelope, epoch=3))
    tmp_path.chmod(0o755)
    with pytest.raises(DockerExecutionError, match="private"):
        OrdinaryDockerExecutionAdapter(
            tmp_path / "journal", setup.target, setup.transport, current_authority=setup.authority
        )


def test_create_start_race_with_revocation_stops_original_container(setup):
    original = setup.transport.start

    def raced(*args, **kwargs):
        original(*args, **kwargs)
        setup.revoked[0] = True

    setup.transport.start = raced
    observed = setup.adapter().submit(setup.envelope)
    assert observed.state == "failed" and observed.termination_confirmed
    assert setup.transport.signals == [("c" * 64, "SIGKILL")]
    assert setup.transport.creates == setup.transport.starts == 1


@pytest.mark.parametrize("failure", ["disconnect", "hard_limit"])
def test_watch_disconnect_or_hard_limit_stops_without_new_key_or_container(setup, failure):
    adapter, channel = setup.adapter(), RenewChannel(setup)

    def sleep(_):
        if failure == "disconnect":
            setup.now[0] += timedelta(seconds=10)
            channel.lose = True
        else:
            setup.now[0] = setup.package.hard_expires_at

    observed = adapter.watch(setup.envelope, channel, sleep=sleep)
    assert observed.state == "failed" and observed.termination_confirmed
    assert setup.transport.creates == setup.transport.starts == 1
    with sqlite3.connect(adapter.journal) as db:
        pending = db.execute("SELECT key FROM docker_renewals WHERE completed=0").fetchall()
    assert len(pending) == (1 if failure == "disconnect" else 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "other"),
        ("epoch", 3),
        ("expires_at", "2026-10-05T12:01:00+00:00"),
        ("hard_expires_at", "2026-10-05T13:00:00+00:00"),
    ],
)
def test_invalid_renewal_readback_stays_pending_and_does_not_change_package(setup, field, value):
    adapter, channel = setup.adapter(), RenewChannel(setup)
    adapter.submit(setup.envelope)
    original = channel.post

    def invalid(*args):
        return original(*args) | {field: value}

    channel.post = invalid
    with pytest.raises((DockerExecutionError, ValidationError)):
        adapter.renew(setup.envelope, channel)
    with sqlite3.connect(adapter.journal) as db:
        assert db.execute("SELECT completed FROM docker_renewals").fetchone() == (0,)
    assert setup.envelope.package_digest == setup.package.package_digest


def test_heartbeat_reply_loss_replays_same_durable_key_after_restart(setup):
    adapter, channel = setup.adapter(), RenewChannel(setup)
    adapter.submit(setup.envelope)
    channel.lose = True
    with pytest.raises(ConnectionError):
        adapter.heartbeat(setup.envelope, channel)
    channel.lose = False
    setup.adapter().heartbeat(setup.envelope, channel)
    assert channel.calls[0] == channel.calls[1]


def test_capture_stop_ack_waits_for_actual_termination_readback(setup):
    adapter = setup.adapter()
    adapter.submit(setup.envelope)
    setup.transport.ready = True
    original, reads = setup.transport.inspect, [0]

    def delayed(*args, **kwargs):
        item = original(*args, **kwargs)
        if setup.transport.signals and reads[0] < 1:
            reads[0] += 1
            item["State"] = {"Running": True, "Status": "running", "ExitCode": 0}
        return item

    setup.transport.inspect = delayed
    assert adapter.collect_outputs(setup.envelope)[0].content == b"result385\n"
    assert reads[0] == 1


def test_retained_output_tampering_is_not_a_valid_manifest(setup):
    adapter = setup.adapter()
    adapter.submit(setup.envelope)
    setup.transport.ready = True
    adapter.collect_outputs(setup.envelope)
    with sqlite3.connect(adapter.journal) as db:
        db.execute("UPDATE docker_output_bytes SET content=?", (b"altered",))
    with pytest.raises(DockerExecutionError, match="custody changed"):
        adapter.output_manifest(setup.envelope)


def test_revoked_restart_stops_owned_container_without_heartbeat_or_new_create(setup):
    setup.adapter().submit(setup.envelope)
    setup.revoked[0] = True
    channel = RenewChannel(setup)
    restarted = setup.adapter()
    observed = restarted.watch(setup.envelope, channel)
    assert observed.state == "failed" and observed.termination_confirmed
    assert channel.calls == []
    assert setup.transport.creates == setup.transport.starts == 1
    proof = restarted.stop_status(setup.envelope)
    assert proof.requested and proof.termination_confirmed and not proof.reconciliation_required


def test_initial_denial_has_no_owned_container_to_stop_or_replace(setup):
    setup.revoked[0] = True
    channel = RenewChannel(setup)
    observed = setup.adapter().watch(setup.envelope, channel)
    assert observed.state == "unknown" and not observed.termination_confirmed
    assert setup.transport.creates == setup.transport.starts == 0
    assert setup.transport.signals == [] and channel.calls == []
