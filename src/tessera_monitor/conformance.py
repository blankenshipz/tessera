"""One opt-in harmless Docker monitoring case; never releases the fixture's go gate.

Separate guardian/monitor processes, local authenticated socket, fixture-only
authority keys. No package issuance, production control endpoint or model call.
"""

import json
import multiprocessing
import os
import secrets
import shutil
import socket
import time
from pathlib import Path

from tessera_control.monitor_evidence import MonitorReceiver
from tessera_worker.local_docker_contract import FixturePlan, LocalFixtureConfig
from tessera_worker.local_docker_engine import DockerCLI, LocalDockerEngine
from tessera_worker.local_docker_namespace import NamespaceUnknown, NativeNamespaceIdentity

from .channel import Channel, serve_monitor
from .daemon import GuardianPeer, supervise_child
from .docker import DockerAdapter
from .journal import MonitorJournal
from .protocol import Assignment, Denied, Limits, Pulse, Workload, digest, open_envelope, seal
from .receiver import receive_one
from .runtime import Monitor


def prerequisites():
    result = dict(state="blocked", effects=0, activation="blocked", host=os.uname().sysname)
    if shutil.which("docker") is None:
        return result | dict(gate="docker_executable_unavailable")
    try:
        NativeNamespaceIdentity().preflight()
    except NamespaceUnknown as error:
        return result | dict(gate=str(error))
    return result | dict(state="available", gate="image_and_daemon_preflight_still_required")


def engine_at(root):
    return LocalDockerEngine(DockerCLI(LocalFixtureConfig(enabled=True), root / "docker-config"))


def _child(root, p, document, keys, sock, peer):
    journal = MonitorJournal(root / "monitor")
    try:
        m = Monitor(
            document,
            identity="fixture-monitor",
            journal=journal,
            adapter=DockerAdapter(engine_at(root), p, allow_host_socket=True),
            preclaimed=True,
            **keys,
        )
        guard = GuardianPeer(peer, m.a, keys["channel_key"])
        serve_monitor(m, Channel(sock), guard=guard.check)
    finally:
        journal.close()
        peer.close()
        sock.close()


def _guardian(root, p, document, keys, sock):
    a = open_envelope(document, Assignment, keys["authority_key"], b"assignment")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_child, args=(root, p, document, keys, sock, child))
    try:
        process.start()
        child.close()
        sock.close()
        supervise_child(process, parent, a, keys["channel_key"])
    finally:
        child.close()
        parent.close()
        if process.pid is not None and process.is_alive():
            raise Denied("fixture monitor still alive")
        journal = MonitorJournal(root / "monitor")
        try:
            Monitor(
                document,
                identity=a.monitor,
                journal=journal,
                adapter=DockerAdapter(engine_at(root), p, allow_host_socket=True),
                recover=True,
                **keys,
            )
        finally:
            journal.close()


def save(root, name, value):
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def run_once(root, image):
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root or not root.parent.is_dir():
        raise Denied("fresh canonical evidence directory required")
    root.mkdir(mode=0o700)
    checked = prerequisites()
    save(root, "preflight.json", checked)
    if checked["state"] != "available":
        return checked | dict(attempts=0)
    p = FixturePlan(
        operation_id=secrets.token_hex(32),
        nonce=secrets.token_hex(32),
        image=image,
        mode="wait",
        runtime_seconds=10,
    )
    engine = engine_at(root)
    image_id = engine.preflight(p)  # local pinned image only; never pull/build
    target = DockerAdapter(engine, p, allow_host_socket=True).target()
    save(root, "create-intent.json", dict(plan=p.model_dump(mode="json"), image_id=image_id))
    cid = engine.create(p, image_id)  # exactly one attempt; lost reply stays unknown
    value = engine.inspect(p, image_id, cid)
    save(root, "created.json", dict(container=cid, created=value["Created"]))
    now = time.time_ns() // 1_000_000
    a = Assignment(
        tenant=p.org_id,
        project=p.project_id,
        plan_digest=p.digest,
        package_digest=p.digest,
        operation=p.operation_id,
        nonce=p.nonce,
        monitor="fixture-monitor",
        worker=p.worker_id,
        authority="fixture-control",
        adapter="docker-local-v1",
        workload=Workload(
            uid=cid,
            image_digest=image_id[7:],
            instance_digest=digest([cid, value["Created"]]),
            target_digest=target,
        ),
        limits=Limits(
            runtime_seconds=10,
            lease_seconds=6,
            memory_bytes=p.memory_bytes,
            cpu_millicores=p.cpu_millicores,
            pids=p.pid_limit,
            filesystem_bytes=p.filesystem_bytes_per_mount,
            filesystem_inodes=p.filesystem_inodes_per_mount,
            output_bytes=0,
        ),
        privilege_profile="docker-socket-procfs-read-v1",
        issued_ms=now,
        expires_ms=now + 10000,
    )
    keys = {
        name: secrets.token_bytes(32) for name in ("authority_key", "channel_key", "evidence_key")
    }
    for name, key in keys.items():
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    document = seal(a, keys["authority_key"], b"assignment")
    save(root, "assignment.json", document.model_dump())
    journal = MonitorJournal(root / "monitor")
    journal.claim(document, a)  # arm custody before allowing any start
    journal.close()
    control = MonitorJournal(root / "control")
    receiver = MonitorReceiver(
        document,
        journal=control,
        tenant=a.tenant,
        project=a.project,
        monitor=a.monitor,
        now=lambda: time.time_ns() // 1_000_000,
        **keys,
    )
    left, right = socket.socketpair()
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_guardian, args=(root, p, document, keys, right))
    channel = Channel(left, timeout=2)
    try:
        process.start()
        right.close()
        challenge = channel.receive()
        pulse = open_envelope(challenge, Pulse, keys["channel_key"], b"challenge")
        response = receiver.respond(challenge, authorized=lambda assigned: assigned == a)
        if pulse.sequence != 1:
            raise Denied("fixture arming unknown")
        save(root, "start-intent.json", dict(container=cid, assignment=digest(a)))
        engine.run(["start", cid])  # only after independently armed challenge, at most once
        channel.send(response)
        deadline = time.monotonic() + 15
        while process.is_alive() and time.monotonic() < deadline:
            try:
                receive_one(channel, receiver, authorized=lambda assigned: False)
            except (Denied, OSError):
                break
    finally:
        channel.close()
        right.close()
        if process.pid is not None:
            process.join(timeout=25)
        if process.pid is not None and process.is_alive():
            control.close()
            raise Denied("guardian unresolved; do not retry")
    journal = MonitorJournal(root / "monitor")
    try:
        for offset in range(0, 1024, 32):
            batch = journal.documents(offset)
            for doc in batch:
                receiver.ingest(doc)
            if len(batch) < 32:
                break
        status = receiver.status()
        result = status | dict(
            attempts=1,
            state="passed"
            if status["cleanup_confirmed"] and status["termination_confirmed"]
            else "unknown",
            scope="monitor-stop-cleanup-only",
            assignment=digest(a),
            operation=a.operation,
            nonce=a.nonce,
            container=cid,
            evidence_digest=digest(
                journal.documents(len(journal.receipts(keys["evidence_key"])) - 1, 1)[0]
            ),
        )
        save(root, "summary.json", result)
        return result
    finally:
        journal.close()
        control.close()
        if process.pid is not None:
            process.close()
