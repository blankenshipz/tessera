"""Docker portability and exact identity tests using only a fake CLI transport."""

import copy
import json

import pytest
from test_execution_monitor import assignment
from test_local_docker_fixture import CID, IMAGE_ID, plan, security_value

from tessera_monitor.docker import DockerAdapter
from tessera_monitor.protocol import Denied, Limits, Workload, digest
from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.local_docker_contract import PROGRAM
from tessera_worker.local_docker_engine import LocalDockerEngine


@pytest.fixture
def docker(monkeypatch):
    p = plan(mode="wait")
    value = security_value()
    value.update(
        Id=CID,
        Name="/" + p.name,
        Image=IMAGE_ID,
        Created="immutable-created",
        RestartCount=0,
        State=dict(
            Running=True,
            Pid=42,
            StartedAt="immutable-start",
            Paused=False,
            Status="running",
            ExitCode=0,
        ),
    )
    value["Config"].update(
        Labels=p.labels, Cmd=["-I", "-B", "-c", PROGRAM, p.mode, p.nonce, p.digest]
    )
    calls = []
    removed = False

    def run(args, **kwargs):
        nonlocal removed
        calls.append(args)
        if args[:2] == ["container", "inspect"]:
            return json.dumps([value]).encode()
        if args[:2] == ["container", "ls"]:
            return b"" if removed else CID.encode() + b"\n"
        if args[0] == "kill":
            value["State"].update(Running=False, Status="exited", ExitCode=137)
            return b""
        if args[0] == "rm":
            assert not value["State"]["Running"]
            removed = True
            return b""
        pytest.fail("unexpected Docker command")

    engine = LocalDockerEngine(run)
    monkeypatch.setattr(engine, "namespace_preflight", lambda: {"state": "available"})
    adapter = DockerAdapter(engine, p, allow_host_socket=True)
    monkeypatch.setattr(adapter, "target", lambda: "7" * 64)
    process = dict(
        start_ticks=123,
        namespaces={k: dict(inode=i + 1) for i, k in enumerate(("net", "mnt", "pid"))},
    )
    monkeypatch.setattr(
        "tessera_monitor.docker.process_identity", lambda pid: copy.deepcopy(process)
    )
    a = assignment(
        tenant=p.org_id,
        project=p.project_id,
        plan_digest=p.digest,
        package_digest=p.digest,
        operation=p.operation_id,
        nonce=p.nonce,
        adapter="docker-local-v1",
        privilege_profile="docker-socket-procfs-read-v1",
        workload=Workload(
            uid=CID,
            image_digest=IMAGE_ID[7:],
            instance_digest=digest([CID, value["Created"]]),
            target_digest="7" * 64,
        ),
        limits=Limits(
            runtime_seconds=20,
            lease_seconds=3,
            memory_bytes=p.memory_bytes,
            cpu_millicores=p.cpu_millicores,
            pids=p.pid_limit,
            filesystem_bytes=p.filesystem_bytes_per_mount,
            filesystem_inodes=p.filesystem_inodes_per_mount,
            output_bytes=0,
        ),
    )
    return adapter, a, value, calls, process


def test_docker_exact_kill_then_cleanup(docker):
    adapter, a, _, calls, _ = docker
    adapter.kill(a)
    obs = adapter.observe(a)
    assert any(o.category == "termination" and o.state == "verified" for o in obs)
    adapter.cleanup(a)
    obs = adapter.observe(a)
    assert {o.category for o in obs} == {"termination", "cleanup"}
    effects = [c for c in calls if c[0] in {"kill", "rm"}]
    assert effects == [["kill", "--signal", "KILL", CID], ["rm", CID]]
    assert all(c[0] not in {"create", "start", "run", "prune", "unpause"} for c in calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Id", "f" * 64),
        ("Name", "/different"),
        ("Image", "sha256:" + "f" * 64),
        ("Created", "new-creation"),
        ("RestartCount", 1),
    ],
)
def test_docker_identity_mismatch_never_kills(docker, field, value):
    adapter, a, raw, calls, _ = docker
    raw[field] = value
    with pytest.raises((Denied, BackendBlocked)):
        adapter.kill(a)
    assert not any(c[0] in {"kill", "rm"} for c in calls)


@pytest.mark.parametrize(
    "label", ["tessera.nonce", "tessera.operation", "tessera.package", "tessera.scope"]
)
def test_docker_nonce_tenant_and_package_labels_fenced(docker, label):
    adapter, a, raw, calls, _ = docker
    raw["Config"]["Labels"][label] = "f" * 64
    with pytest.raises(BackendBlocked):
        adapter.kill(a)
    assert not any(c[0] == "kill" for c in calls)


@pytest.mark.parametrize("mutation", ["pid", "start", "ticks"])
def test_pid_reuse_and_container_restart_are_fenced(docker, mutation):
    adapter, a, raw, calls, process = docker
    adapter.owned(a)
    if mutation == "pid":
        raw["State"]["Pid"] += 1
    elif mutation == "start":
        raw["State"]["StartedAt"] = "new"
    else:
        process["start_ticks"] += 1
    with pytest.raises(Denied):
        adapter.kill(a)
    assert not any(c[0] == "kill" for c in calls)


def test_running_cleanup_and_paused_unpause_denied(docker):
    adapter, a, raw, calls, _ = docker
    with pytest.raises(Denied):
        adapter.cleanup(a)
    raw["State"]["Paused"] = True
    with pytest.raises(Denied):
        adapter.kill(a)
    assert not any(c[0] in {"kill", "rm", "unpause"} for c in calls)


def test_host_privilege_explicit_and_target_pinned(docker, monkeypatch):
    adapter, a, _, calls, _ = docker
    adapter.allowed = False
    with pytest.raises(Denied):
        adapter.preflight(a)
    assert not calls
    adapter.allowed = True
    monkeypatch.setattr(adapter, "target", lambda: "f" * 64)
    with pytest.raises(Denied):
        adapter.kill(a)
    assert not calls


def test_unknown_native_observer_never_substitutes_inspect(docker, monkeypatch):
    adapter, a, _, _, _ = docker

    def unavailable(*args):
        raise BackendBlocked("namespace unknown")

    monkeypatch.setattr(adapter.engine, "verify_network", unavailable)
    states = {o.category: o.state for o in adapter.observe(a)}
    assert states["identity"] == "verified"
    assert all(
        states[k] == "unknown"
        for k in ("namespace", "network", "resources", "filesystem", "custody")
    )


def test_docker_historical_cleanup_does_not_require_live_authority(docker):
    adapter, a, raw, _, _ = docker
    raw["State"].update(Running=False, Status="exited")
    adapter.cleanup(a.model_copy(update={"expires_ms": 1}))
    assert all(o.state == "verified" for o in adapter.observe(a))
