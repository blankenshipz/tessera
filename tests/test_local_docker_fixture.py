"""Offline/fake local fixture checks. No Docker effects in default pytest."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import multiprocessing
import os
import tarfile
from pathlib import Path

import pytest

from tessera_worker.capability_effects import BackendBlocked, GatedRealCapabilityEffects
from tessera_worker.capability_journal import JournalConflict
from tessera_worker.local_docker_contract import (
    CAPTURE,
    MEMORY,
    OUTPUT,
    PIDS,
    FixturePlan,
    LocalFixtureConfig,
    authenticate,
    capture_archive,
    log_records,
    message,
)
from tessera_worker.local_docker_engine import (
    DockerCLI,
    DockerReplyUnknown,
    LocalDockerEngine,
    create_arguments,
    safe_engine_error,
    tmpfs,
)
from tessera_worker.local_docker_fixture import (
    FixtureJournal,
    LocalFixtureAdapter,
    recover,
    supervise,
)
from tessera_worker.local_docker_namespace import NamespaceUnknown

IMAGE = "python@sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
CID = "c" * 64
KEY = b"k" * 32


def plan(**changes):
    return FixturePlan(
        **(dict(operation_id="1" * 64, nonce="2" * 64, image=IMAGE, mode="success") | changes)
    )


def archive(name="output/result.txt", content=OUTPUT, kind=tarfile.REGTYPE, size=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.size = len(content) if size is None else size
        if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
            entry.linkname = "/etc/passwd"
            entry.size = 0
        tar.addfile(entry, io.BytesIO(content))
    return stream.getvalue()


def logs(p, stages=("ready", "done")):
    values = []
    for stage in stages:
        value = dict(stage=stage, nonce=p.nonce, package=p.digest)
        if stage == "ready":
            value.update(filesystem="tmpfs-byte-inode-exhaustion", network="none")
        if stage == "done":
            value["exit_code"] = 0 if p.mode == "success" else 7
        if stage == "limits_unknown":
            value.update(check="memory_swap_max", failure="missing")
        values.append(json.dumps(value))
    return "\n".join(values).encode()


class FakeEngine:
    def __init__(self, p, fault=None):
        self.plan, self.fault = p, fault
        self.calls = []
        self.running = True
        self.paused = False
        self.released = False

    def preflight(self, p):
        self.calls.append("preflight")
        if self.fault == "preflight":
            raise BackendBlocked("SECRET_CANARY")
        return IMAGE_ID

    def create(self, p, image_id):
        self.calls.append("create")
        if self.fault == "create":
            raise DockerReplyUnknown("SECRET_CANARY")
        return CID

    def namespace_preflight(self):
        self.calls.append("namespace_preflight")
        if self.fault == "namespace_preflight":
            raise NamespaceUnknown("daemon_host_namespace_observer_unavailable")
        return dict(state="available", source="fake-test-only")

    def verify_network(self, *args):
        self.calls.append("verify_network")
        if self.fault == "network":
            raise NamespaceUnknown("namespace_observation_unavailable_or_malformed")
        return dict(state="verified", source="fake-test-only")

    def inspect(self, *args):
        self.calls.append("inspect")
        return dict(
            Id=CID,
            State=dict(
                Running=self.running,
                Paused=self.paused,
                Status="running" if self.running else "exited",
                ExitCode=0 if self.plan.mode == "success" else 7,
            ),
        )

    def run(self, args, **kwargs):
        self.calls.append(args[0])
        if args[0] == self.fault:
            raise DockerReplyUnknown("SECRET_CANARY")
        if args[0] == "logs":
            stages = ("ready", "done") if self.released and self.plan.mode != "wait" else ("ready",)
            if self.fault == "limits":
                stages = ("limits_unknown",)
            return logs(self.plan, stages)
        if args[0] == "cp":
            return archive(content=b"SECRET_CANARY") if self.fault == "output" else archive()
        if args[0] == "pause":
            self.paused = True
        if args[0] == "unpause":
            self.paused = False
        return b""

    def signal(self, cid, marker):
        self.calls.append(marker)
        if marker == "go":
            self.released = True
        else:
            self.running = False

    def stop(self, *args):
        self.calls.append("kill")
        if self.fault == "kill":
            raise DockerReplyUnknown("SECRET_CANARY")
        self.running = False
        return self.inspect()

    def cleanup(self, *args):
        self.calls.append("cleanup")
        if self.fault == "cleanup":
            raise DockerReplyUnknown("SECRET_CANARY")


def execute(tmp_path, *, mode="success", fault=None, control=None):
    p = plan(mode=mode)
    engine = FakeEngine(p, fault)
    journal = FixtureJournal(tmp_path / "journal")
    clock = [0.0]

    def sleep(value):
        clock[0] += value

    try:
        result = supervise(
            p,
            journal,
            engine,
            control or (lambda n: "heartbeat"),
            clock=lambda: clock[0],
            sleep=sleep,
        )
        return result, engine, p
    finally:
        journal.close()


@pytest.mark.parametrize(
    "image",
    [
        "python",
        "python:latest",
        "python:3@sha256:" + "a" * 64,
        "sha256:" + "a" * 64,
        "python@sha256:abc",
        "user:password@host/image@sha256:" + "a" * 64,
        "python@sha256:" + "A" * 64,
        IMAGE + "\n",
    ],
)
def test_mutable_or_unsafe_image_rejected(image):
    with pytest.raises(ValueError):
        plan(image=image)


@pytest.mark.parametrize(
    "change",
    [
        dict(runtime_seconds=0),
        dict(runtime_seconds=16),
        dict(runtime_seconds=True),
        dict(nonce="short"),
        dict(mode="shell"),
        dict(org_id="another-tenant"),
        dict(project_id="actual-project"),
        dict(command="pip install"),
    ],
)
def test_closed_plan_bounds(change):
    with pytest.raises(ValueError):
        plan(**change)


def test_default_adapter_zero_effects(tmp_path):
    adapter = LocalFixtureAdapter()
    with pytest.raises(BackendBlocked):
        adapter.launch(plan(), tmp_path / "absent")
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(BackendBlocked):
        recover(tmp_path / "absent", LocalFixtureConfig())
    assert list(tmp_path.iterdir()) == []


def test_default_cli_zero_effects(monkeypatch, capsys):
    path = Path(__file__).parents[1] / "scripts/check_local_docker_fixture.py"
    spec = importlib.util.spec_from_file_location("local_fixture_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(LocalFixtureAdapter, "launch", lambda *a: pytest.fail("effect"))
    assert module.main([]) == 0
    assert json.loads(capsys.readouterr().out)["effects"] == 0


def test_exact_security_arguments():
    args = create_arguments(plan(), IMAGE_ID)
    for flag, value in [
        ("--network", "none"),
        ("--user", "65532:65532"),
        ("--memory", str(MEMORY)),
        ("--memory-swap", str(MEMORY)),
        ("--pids-limit", str(PIDS)),
        ("--cpus", "0.25"),
        ("--cap-drop", "ALL"),
        ("--pull", "never"),
        ("--security-opt", "no-new-privileges=true"),
        ("--ipc", "none"),
    ]:
        assert args[args.index(flag) + 1] == value
    assert "--read-only" in args
    assert not set(args) & {"--mount", "--volume", "-v", "--env", "-e", "--privileged"}
    assert args.count("--tmpfs") == 2
    assert all("nr_inodes=32" in value and "size=1048576" in value for value in tmpfs().values())
    assert args[args.index("--log-driver") + 1] == "local"
    assert [args[i + 1] for i, arg in enumerate(args) if arg == "--log-opt"] == [
        "max-size=32k",
        "max-file=1",
        "compress=false",
    ]


def security_value():
    return dict(
        HostConfig=dict(
            NetworkMode="none",
            ReadonlyRootfs=True,
            Privileged=False,
            NanoCpus=250000000,
            Memory=MEMORY,
            MemorySwap=MEMORY,
            PidsLimit=PIDS,
            IpcMode="none",
            PidMode="",
            UTSMode="",
            CapDrop=["ALL"],
            RestartPolicy=dict(Name="no", MaximumRetryCount=0),
            Tmpfs=tmpfs(),
            ShmSize=65536,
            LogConfig=dict(
                Type="local",
                Config={"max-size": "32k", "max-file": "1", "compress": "false"},
            ),
            SecurityOpt=["no-new-privileges=true"],
        ),
        Config=dict(User="65532:65532", Entrypoint=["/usr/local/bin/python"]),
        Mounts=[],
        NetworkSettings=dict(Networks={"none": {}}),
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("NetworkMode", "host"),
        ("ReadonlyRootfs", False),
        ("Privileged", True),
        ("Memory", 0),
        ("MemorySwap", -1),
        ("NanoCpus", 0),
        ("PidsLimit", -1),
        ("IpcMode", "host"),
        ("PidMode", "host"),
        ("UTSMode", "host"),
        ("CapAdd", ["SYS_ADMIN"]),
        ("Binds", ["/var/run/docker.sock:/var/run/docker.sock"]),
        ("Devices", ["/dev/disk"]),
        ("VolumesFrom", ["control"]),
        ("SecurityOpt", ["seccomp=unconfined"]),
        ("Tmpfs", {"/output": "size=1m"}),
        ("RestartPolicy", dict(Name="always")),
        ("LogConfig", dict(Type="local", Config={"max-size": "32k", "max-file": "1"})),
        (
            "LogConfig",
            dict(Type="local", Config={"max-size": "32k", "max-file": "1", "compress": "true"}),
        ),
        (
            "LogConfig",
            dict(Type="local", Config={"max-size": "32k", "max-file": "2", "compress": "false"}),
        ),
    ],
)
def test_readback_security_mutation_blocks(key, value):
    engine = LocalDockerEngine(None)
    original = security_value()
    engine.verify_security(original)
    changed = copy.deepcopy(original)
    changed["HostConfig"][key] = value
    with pytest.raises(BackendBlocked):
        engine.verify_security(changed)


def test_host_mount_or_root_user_readback_blocks():
    engine = LocalDockerEngine(None)
    for change in [
        dict(Mounts=[dict(Type="bind", Destination="/inputs")]),
        dict(Config=dict(User="0", Entrypoint=["/usr/local/bin/python"])),
    ]:
        with pytest.raises(BackendBlocked):
            engine.verify_security(security_value() | change)


@pytest.mark.parametrize(
    "name",
    [
        "../result.txt",
        "/output/result.txt",
        "output/../result.txt",
        "output/sub/file",
        "output/secret",
        "output\\result.txt",
    ],
)
def test_output_path_rejection(name):
    with pytest.raises(BackendBlocked):
        capture_archive(archive(name=name))


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE]
)
def test_output_links_special_files_rejected(kind):
    with pytest.raises(BackendBlocked):
        capture_archive(archive(kind=kind))


@pytest.mark.parametrize(
    "raw",
    [
        b"x" * (CAPTURE + 1),
        archive(content=b"SECRET_CANARY"),
        archive(content=OUTPUT * 2),
        b"not tar",
    ],
)
def test_oversized_or_undeclared_output_rejected_without_leak(raw):
    with pytest.raises(BackendBlocked) as error:
        capture_archive(raw)
    assert "SECRET_CANARY" not in str(error.value)


def test_exact_capture_and_log_secret_rejection():
    assert capture_archive(archive()) == OUTPUT
    assert len(log_records(logs(plan()), plan())) == 2
    with pytest.raises(BackendBlocked, match="discarded") as error:
        log_records(b"SECRET_CANARY", plan())
    assert "SECRET_CANARY" not in str(error.value)


@pytest.mark.parametrize("change", ["key", "sequence", "nonce", "action", "extra", "oversized"])
def test_authenticated_operation_messages(change):
    p = plan()
    raw = message(KEY, p, 1, "heartbeat")
    assert authenticate(raw, KEY, p, 1) == "heartbeat"
    key, seq = KEY, 1
    if change == "key":
        key = b"x" * 32
    elif change == "sequence":
        seq = 2
    elif change == "nonce":
        p = plan(nonce="3" * 64)
    elif change == "action":
        raw = message(KEY, p, 1, "start")
    elif change == "extra":
        raw += b" "
    else:
        raw = b"x" * 1025
    with pytest.raises(BackendBlocked):
        authenticate(raw, key, p, seq)


@pytest.mark.parametrize("mode,expected", [("success", "completed"), ("failure", "failed")])
def test_success_failure_quarantine_and_no_replay(tmp_path, mode, expected):
    report, engine, p = execute(tmp_path, mode=mode)
    assert report["state"] == expected
    assert report["termination_confirmed"] and report["cleanup_confirmed"]
    assert engine.calls.count("create") == engine.calls.count("start") == 1
    assert "custody" in report["records"]
    journal = FixtureJournal(tmp_path / "journal")
    try:
        with pytest.raises(JournalConflict, match="recovery only"):
            journal.claim(p)
        with journal.connect() as db:
            row = db.execute("SELECT * FROM local_fixture_output").fetchone()
        assert row["content"] == OUTPUT
        assert json.loads(row["manifest"])["classification"] == "quarantined_unreleased"
    finally:
        journal.close()
    assert recover(tmp_path / "journal", LocalFixtureConfig(enabled=True), engine=engine) == report


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("preflight", "blocked"),
        ("limits", "blocked"),
        ("create", "unknown"),
        ("start", "unknown"),
        ("cp", "unknown"),
        ("output", "unknown"),
        ("logs", "unknown"),
        ("pause", "unknown"),
        ("cleanup", "completed"),
    ],
)
def test_faults_no_retry_and_no_secret_receipts(tmp_path, fault, expected):
    report, engine, _ = execute(tmp_path, fault=fault)
    assert report["state"] == expected
    assert "SECRET_CANARY" not in json.dumps(report)
    assert engine.calls.count("create") <= 1 and engine.calls.count("start") <= 1
    if fault == "limits":
        assert "go" not in engine.calls and report["cleanup_confirmed"]
        assert "custody" not in report["records"] and "cp" not in engine.calls
        assert report["records"]["limits"]["observation"] == dict(
            stage="limits_unknown",
            nonce=engine.plan.nonce,
            package=engine.plan.digest,
            check="memory_swap_max",
            failure="missing",
        )
    if fault == "preflight":
        assert engine.calls == ["preflight"]
    if fault == "cleanup":
        assert not report["cleanup_confirmed"]


@pytest.mark.parametrize(
    "control,expected,reason",
    [
        (lambda n: "cancel", "cancelled", "cancel"),
        (lambda n: "lost", "unknown", "broker_loss"),
        (lambda n: None, "unknown", "heartbeat_loss"),
        (lambda n: "heartbeat", "unknown", "deadline"),
    ],
)
def test_cancel_loss_heartbeat_deadline_independent_kill(tmp_path, control, expected, reason):
    report, engine, _ = execute(tmp_path, mode="wait", control=control)
    assert report["state"] == expected
    assert report["records"]["terminal"]["observation"]["reason"] == reason
    assert report["termination_confirmed"] and report["cleanup_confirmed"]
    assert engine.calls.count("kill") == 1
    assert report["records"]["kill_pending"]["observation"]["state"] == "unknown"


def test_lost_kill_reply_unknown_and_explicit_recovery_never_restarts(tmp_path):
    report, engine, _ = execute(tmp_path, mode="wait", fault="kill", control=lambda n: "cancel")
    assert report["state"] == "unknown"
    assert not report["termination_confirmed"] and not report["cleanup_confirmed"]
    assert "cleanup" not in engine.calls
    engine.fault = None
    recovered = recover(tmp_path / "journal", LocalFixtureConfig(enabled=True), engine=engine)
    assert recovered["state"] == "unknown" and recovered["termination_confirmed"]
    assert recovered["cleanup_confirmed"]
    assert engine.calls.count("create") == engine.calls.count("start") == 1


def test_restart_with_only_create_intent_kills_exact_identity(tmp_path):
    p = plan()
    journal = FixtureJournal(tmp_path / "journal")
    journal.claim(p)
    journal.event("image", dict(image_id=IMAGE_ID))
    journal.event("create_intent", dict(container_name=p.name, image_id=IMAGE_ID))
    journal.close()
    engine = FakeEngine(p)
    result = recover(tmp_path / "journal", LocalFixtureConfig(enabled=True), engine=engine)
    assert result["state"] == "unknown" and result["cleanup_confirmed"]
    assert "create" not in engine.calls and "start" not in engine.calls


def test_receipt_idempotency_no_overwrite_and_owner_lock(tmp_path):
    journal = FixtureJournal(tmp_path / "journal")
    try:
        journal.claim(plan())
        with pytest.raises(JournalConflict):
            FixtureJournal(journal.root)
        first = journal.event("custody", {"ok": True}, content=OUTPUT)
        assert journal.event("custody", {"ok": True}, content=OUTPUT) == first
        with pytest.raises(JournalConflict):
            journal.event("custody", {"ok": False})
        with pytest.raises(JournalConflict):
            journal.event("custody", {"ok": True}, content=b"changed")
        assert len(journal.records()) == 1
        assert first["operation_id"] == plan().operation_id
        assert first["org_id"] == plan().org_id and first["project_id"] == plan().project_id
    finally:
        journal.close()


def test_unknown_cannot_activate_existing_effects(tmp_path):
    report, _, _ = execute(tmp_path, mode="wait", control=lambda n: "lost")
    assert report["activation"] == "blocked"
    with pytest.raises(BackendBlocked):
        LocalFixtureAdapter(LocalFixtureConfig(enabled=True)).preflight(report)
    with pytest.raises(BackendBlocked):
        GatedRealCapabilityEffects(None).preflight(report)


def test_cleanup_ownership_fences_destructive_commands():
    p = plan()
    calls = []
    value = dict(
        Id=CID,
        Name="/" + p.name,
        Image=IMAGE_ID,
        Config=dict(Labels=p.labels | {"tessera.nonce": "foreign"}),
        State=dict(Running=True),
    )

    def run(args):
        calls.append(args)
        return json.dumps([value]).encode()

    engine = LocalDockerEngine(run)
    with pytest.raises(BackendBlocked):
        engine.stop(p, IMAGE_ID, CID)
    with pytest.raises(BackendBlocked):
        engine.cleanup(p, IMAGE_ID, CID)
    assert all(c[0:2] == ["container", "inspect"] for c in calls)


def _independent_fake(root, connection, p, ready=None):
    journal = FixtureJournal(Path(root))
    try:

        def receive(seq):
            if connection.poll():
                try:
                    return authenticate(connection.recv_bytes(1024), KEY, p, seq)
                except Exception:
                    return "lost"
            return None

        engine = FakeEngine(p)
        if ready is not None:
            original = engine.signal

            def signal(cid, marker):
                original(cid, marker)
                if marker == "go":
                    ready.set()

            engine.signal = signal
        supervise(p, journal, engine, receive)
    finally:
        journal.close()


def test_real_independent_process_clock_kills_without_broker_ticks(tmp_path):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    root = tmp_path / "journal"
    process = context.Process(target=_independent_fake, args=(str(root), child, plan(mode="wait")))
    process.start()
    child.close()
    try:
        # No tick or heartbeat from this process. The separate supervisor expires.
        process.join(timeout=8)
        assert not process.is_alive() and process.exitcode == 0
        journal = FixtureJournal(root)
        try:
            result = journal.report()
        finally:
            journal.close()
        assert result["records"]["armed"]["observation"]["supervisor_pid"] != os.getpid()
        assert result["records"]["terminal"]["observation"]["reason"] == "heartbeat_loss"
        assert result["termination_confirmed"] and result["cleanup_confirmed"]
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


def test_preflight_local_image_identity_and_no_pull():
    p = plan()
    calls = []
    image = dict(Id=IMAGE_ID, RepoDigests=[IMAGE], Os="linux", Config=dict(Env=["PATH=/bin"]))

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "info":
            return json.dumps(
                dict(OperatingSystem="Docker Desktop", OSType="linux", CgroupVersion="2")
            ).encode()
        return json.dumps([image]).encode()

    engine = LocalDockerEngine(run)
    assert engine.preflight(p) == IMAGE_ID
    assert [c[:2] for c in calls] == [["info", "--format"], ["image", "inspect"]]
    for change in [
        dict(Id="mutable"),
        dict(RepoDigests=[]),
        dict(Os="windows"),
        dict(Config=dict(Volumes={"/home": {}})),
        dict(Config=dict(OnBuild=["RUN download"])),
        dict(Config=dict(Env=["SECRET_CANARY=not-permitted"])),
    ]:
        original = image.copy()
        image.update(change)
        with pytest.raises(BackendBlocked):
            engine.preflight(p)
        image.clear()
        image.update(original)
    assert not any("pull" in c or "login" in c for c in calls)


@pytest.mark.parametrize("kind", ["id", "name", "image", "operation", "nonce", "scope", "package"])
def test_every_container_ownership_binding_checked(kind):
    p = plan()
    value = dict(Id=CID, Name="/" + p.name, Image=IMAGE_ID, Config=dict(Labels=p.labels.copy()))
    if kind in {"id", "name", "image"}:
        value[{"id": "Id", "name": "Name", "image": "Image"}[kind]] = "foreign"
    else:
        value["Config"]["Labels"]["tessera." + kind] = "foreign"
    engine = LocalDockerEngine(lambda _: json.dumps([value]).encode())
    with pytest.raises(BackendBlocked):
        engine.inspect(p, IMAGE_ID, CID)


def test_cleanup_does_not_treat_missing_reply_as_absence():
    p = plan()
    value = dict(
        Id=CID,
        Name="/" + p.name,
        Image=IMAGE_ID,
        Config=dict(Labels=p.labels),
        State=dict(Running=False),
    )
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["container", "inspect"]:
            return json.dumps([value]).encode()
        if args[0] == "rm":
            return b""
        raise DockerReplyUnknown()

    with pytest.raises(DockerReplyUnknown):
        LocalDockerEngine(run).cleanup(p, IMAGE_ID, CID)
    assert ["rm", CID] in calls
    assert not any("--force" in c or "prune" in c for c in calls)


def test_safe_engine_error_has_only_digest_and_finite_categories():
    value = safe_engine_error("SECRET_CANARY: invalid argument mounting tmpfs")
    assert value["categories"] == ["invalid argument", "tmpfs", "mount"]
    assert "SECRET_CANARY" not in json.dumps(value)


def test_cli_process_has_no_ambient_credentials_and_stream_bound(monkeypatch, tmp_path):
    import subprocess
    import sys

    real_popen = subprocess.Popen
    seen = []

    def popen(command, **kwargs):
        seen.append((command, kwargs))
        # A real harmless local child exercises streaming cutoff without Docker.
        return real_popen([sys.executable, "-I", "-c", "print('x' * 8192)"], **kwargs)

    monkeypatch.setattr("tessera_worker.local_docker_engine.shutil.which", lambda _: "/docker")
    monkeypatch.setattr("tessera_worker.local_docker_engine.subprocess.Popen", popen)
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid")
    monkeypatch.setenv("SECRET_CANARY", "must-not-propagate")
    cli = DockerCLI(LocalFixtureConfig(enabled=True), tmp_path)
    with pytest.raises(DockerReplyUnknown):
        cli(["info"], limit=1024)
    assert seen[0][0][:5] == [
        "/docker",
        "--config",
        str(tmp_path),
        "--host",
        "unix:///var/run/docker.sock",
    ]
    assert set(seen[0][1]["env"]) == {"PATH", "HOME"}
    assert seen[0][1]["stdin"] == subprocess.DEVNULL


def test_cli_process_timeout_is_bounded(monkeypatch, tmp_path):
    import subprocess
    import sys
    import time

    real_popen = subprocess.Popen
    children = []

    def popen(command, **kwargs):
        child = real_popen([sys.executable, "-I", "-c", "import time; time.sleep(10)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("tessera_worker.local_docker_engine.shutil.which", lambda _: "/docker")
    monkeypatch.setattr("tessera_worker.local_docker_engine.subprocess.Popen", popen)
    started = time.monotonic()
    with pytest.raises(DockerReplyUnknown):
        DockerCLI(LocalFixtureConfig(enabled=True), tmp_path)(["info"], timeout=0.1)
    assert time.monotonic() - started < 2
    assert children[0].poll() is not None


def test_symlinked_evidence_root_and_relaunch_rejected(tmp_path):
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    adapter = LocalFixtureAdapter(LocalFixtureConfig(enabled=True))
    for path in [root, link, link / "new"]:
        with pytest.raises(BackendBlocked):
            adapter.launch(plan(), path)
    with pytest.raises(JournalConflict):
        FixtureJournal(link)


def test_real_supervisor_process_loss_recovery_has_no_resubmit(tmp_path):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    ready = context.Event()
    root = tmp_path / "crashed"
    p = plan(mode="wait")
    process = context.Process(target=_independent_fake, args=(str(root), child, p, ready))
    process.start()
    child.close()
    try:
        assert ready.wait(5)
        process.terminate()
        process.join(timeout=3)
        assert not process.is_alive()
        engine = FakeEngine(p)
        result = recover(root, LocalFixtureConfig(enabled=True), engine=engine)
        assert "created" in result["records"] and "start_intent" in result["records"]
        assert result["state"] == "unknown" and result["termination_confirmed"]
        assert result["records"]["terminal"]["observation"]["reason"] == "supervisor_restart"
        assert engine.calls == ["kill", "inspect", "cleanup"]
        assert result["cleanup_confirmed"]
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


@pytest.mark.parametrize("failed_key", ["outcome_intent", "kill_pending", "stop_intent"])
def test_journal_failure_cannot_disable_independent_kill(tmp_path, failed_key):
    import sqlite3

    journal = FixtureJournal(tmp_path / "journal")
    p = plan(mode="wait")
    engine = FakeEngine(p)
    original = journal.event

    def event(key, *args, **kwargs):
        if key == failed_key:
            raise sqlite3.OperationalError("SECRET_CANARY")
        return original(key, *args, **kwargs)

    journal.event = event
    try:
        if failed_key == "stop_intent":
            supervise(p, journal, engine, lambda n: "cancel")
        else:
            with pytest.raises(sqlite3.OperationalError):
                supervise(p, journal, engine, lambda n: "cancel")
        assert engine.calls.count("kill") == 1
        assert not engine.running
        assert "SECRET_CANARY" not in json.dumps(journal.records())
    finally:
        journal.close()
