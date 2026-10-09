"""Closed, networkless conformance profile, NOT an execution/package capability."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import tarfile
from typing import Literal

from pydantic import Field, field_validator

from tessera_control.store import canonical_digest

from .capability_activation import Frozen, Nonce
from .capability_effects import BackendBlocked
from .local_docker_namespace import OBSERVER

PROFILE = "local-networkless-fixture-v1"
OUTPUT = b"hello tessera local fixture\n"
FS_BYTES = 1024 * 1024
INODES = 32
MEMORY = 128 * 1024 * 1024
PIDS = 32
CAPTURE = 128 * 1024
IMAGE = re.compile(r"[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}\Z")
ID = re.compile(r"[a-f0-9]{64}\Z")
# Closed diagnostics only: never retain raw exception messages, paths or readbacks.
LIMIT_CHECKS = (
    "uid",
    "gid",
    "network",
    "capabilities",
    "no_new_privileges",
    "seccomp",
    "memory_max",
    "memory_swap_max",
    "pids_max",
    "cpu_max",
    "readonly_root",
    *(
        f"{root}.{check}"
        for root in ("scratch", "output")
        for check in ("statvfs_bytes", "statvfs_inodes", "byte_enospc", "inode_enospc")
    ),
)
LIMIT_FAILURES = (
    "assertion",
    "missing",
    "permission_denied",
    "no_space",
    "read_only",
    "os_error",
    "exception",
)


class FixturePlan(Frozen):
    # Fixture identities are intentionally not caller-supplied control credentials.
    profile: Literal["local-networkless-fixture-v1"] = PROFILE
    org_id: Literal["local-fixture-org"] = "local-fixture-org"
    project_id: Literal["local-fixture-project"] = "local-fixture-project"
    worker_id: Literal["local-fixture-supervisor"] = "local-fixture-supervisor"
    operation_id: Nonce
    nonce: Nonce
    image: str = Field(max_length=256)
    mode: Literal["success", "failure", "wait"]
    runtime_seconds: int = Field(default=10, ge=3, le=15)
    cpu_millicores: Literal[250] = 250
    memory_bytes: Literal[134217728] = MEMORY
    pid_limit: Literal[32] = PIDS
    filesystem_bytes_per_mount: Literal[1048576] = FS_BYTES
    filesystem_inodes_per_mount: Literal[32] = INODES
    output_bytes: Literal[28] = len(OUTPUT)
    input_sha256: Literal[hashlib.sha256(OUTPUT).hexdigest()] = hashlib.sha256(OUTPUT).hexdigest()

    @field_validator("image")
    @classmethod
    def pinned(cls, value):
        # Even tag@digest is disallowed. No registry credentials, ports or mutable refs.
        if not IMAGE.fullmatch(value):
            raise ValueError("immutable untagged repository digest required")
        return value

    @property
    def program_digest(self):
        return hashlib.sha256(PROGRAM.encode()).hexdigest()

    @property
    def digest(self):
        return canonical_digest(
            [
                self.model_dump(mode="json"),
                self.program_digest,
                hashlib.sha256(OBSERVER.encode()).hexdigest(),
            ]
        )

    @property
    def name(self):
        return "tessera-local-" + self.nonce

    @property
    def labels(self):
        return {
            "tessera.fixture": PROFILE,
            "tessera.package": self.digest,
            "tessera.operation": self.operation_id,
            "tessera.nonce": self.nonce,
            "tessera.scope": canonical_digest([self.org_id, self.project_id]),
        }


class LocalFixtureConfig(Frozen):
    enabled: bool = False
    target: Literal["unix:///var/run/docker.sock"] = "unix:///var/run/docker.sock"


def message(key, plan, sequence, action):
    payload = dict(package=plan.digest, nonce=plan.nonce, sequence=sequence, action=action)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps(
        dict(payload=payload, mac=hmac.new(key, b"local-fixture-v1\0" + raw, "sha256").hexdigest())
    ).encode()


def authenticate(raw, key, plan, sequence):
    try:
        if len(raw) > 1024:
            raise ValueError
        value = json.loads(raw)
        action = value["payload"]["action"]
        if action not in {"heartbeat", "cancel"}:
            raise ValueError
        if not hmac.compare_digest(raw, message(key, plan, sequence, action)):
            raise ValueError
        return action
    except Exception:
        raise BackendBlocked("invalid fixture control message") from None


def capture_archive(raw):
    """Never extract Docker's tar onto the host. Exact root/file, bounded, no links."""
    if len(raw) > CAPTURE:
        raise BackendBlocked("output bound")
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for index, entry in enumerate(archive):
                if index > 2 or entry.pax_headers:
                    raise ValueError
                if entry.name in {"output", "output/", "."} and entry.isdir():
                    continue
                if (
                    entry.name not in {"output/result.txt", "./result.txt", "result.txt"}
                    or not entry.isfile()
                    or entry.size != len(OUTPUT)
                    or files
                    or entry.linkname
                ):
                    raise ValueError
                stream = archive.extractfile(entry)
                if stream is None:
                    raise ValueError
                content = stream.read(len(OUTPUT) + 1)
                # A closed fixture accepts only declared harmless bytes. No heuristic
                # secret scanner pretends to sanitize arbitrary workload output.
                if content != OUTPUT:
                    raise ValueError
                files["result.txt"] = content
        if len(files) != 1:
            raise ValueError
    except Exception:
        raise BackendBlocked("invalid or undeclared output") from None
    return files["result.txt"]


# Runs only from an already local, reviewed Python image. No imports of project
# code, shell, downloads, installs, environment values, inputs or arbitrary argv.
# The supervisor must observe ready and release /scratch/go before the workload.
PROGRAM = r"""
import errno, json, os, pathlib, signal, socket, sys, time
mode, nonce, package = sys.argv[1:]
if mode == 'wait':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
def emit(stage, **extra):
    value = dict(stage=stage, nonce=nonce, package=package, **extra)
    print(json.dumps(value, sort_keys=True), flush=True)
def check(root):
    global check_id
    check_id = root[1:] + '.statvfs_bytes'
    v = os.statvfs(root)
    assert v.f_blocks * v.f_frsize == 1048576
    check_id = root[1:] + '.statvfs_inodes'
    assert v.f_files == 32
    check_id = root[1:] + '.byte_enospc'
    p = pathlib.Path(root)
    byte_error = False
    try:
        with (p / 'quota').open('wb', buffering=0) as f:
            for _ in range(257):
                f.write(b'x' * 4096)
    except OSError as e:
        byte_error = e.errno == errno.ENOSPC
    finally:
        (p / 'quota').unlink(missing_ok=True)
    assert byte_error
    check_id = root[1:] + '.inode_enospc'
    inode_error = False
    try:
        for i in range(33):
            (p / str(i)).touch()
    except OSError as e:
        inode_error = e.errno == errno.ENOSPC
    finally:
        for item in p.iterdir():
            item.unlink()
    assert inode_error
try:
    check_id = 'uid'
    assert os.getuid() == 65532
    check_id = 'gid'
    assert os.getgid() == 65532
    check_id = 'network'
    # if_nameindex queries this process's network namespace. A sysfs mount can
    # reflect another namespace; directory presence is not a connectivity witness.
    assert [name for _, name in socket.if_nameindex()] == ['lo']
    check_id = 'capabilities'
    status = pathlib.Path('/proc/self/status').read_text()
    assert 'CapEff:\t0000000000000000' in status
    check_id = 'no_new_privileges'
    assert 'NoNewPrivs:\t1' in status
    check_id = 'seccomp'
    assert 'Seccomp:\t2' in status
    check_id = 'memory_max'
    cg = pathlib.Path('/sys/fs/cgroup')
    assert (cg / 'memory.max').read_text().strip() == '134217728'
    check_id = 'memory_swap_max'
    assert (cg / 'memory.swap.max').read_text().strip() == '0'
    check_id = 'pids_max'
    assert (cg / 'pids.max').read_text().strip() == '32'
    check_id = 'cpu_max'
    quota, period = map(int, (cg / 'cpu.max').read_text().split())
    assert quota / period == 0.25
    check_id = 'readonly_root'
    try:
        pathlib.Path('/root-write-denied').write_text('x')
        raise AssertionError()
    except OSError as e:
        assert e.errno == errno.EROFS
    for root in ['/scratch', '/output']:
        check(root)
except Exception as error:
    if isinstance(error, AssertionError):
        failure = 'assertion'
    elif isinstance(error, OSError):
        failure = {
            errno.ENOENT: 'missing', errno.EACCES: 'permission_denied',
            errno.EPERM: 'permission_denied', errno.ENOSPC: 'no_space',
            errno.EROFS: 'read_only',
        }.get(error.errno, 'os_error')
    else:
        failure = 'exception'
    emit('limits_unknown', check=check_id, failure=failure)
    sys.exit(78)
emit('ready', filesystem='tmpfs-byte-inode-exhaustion', network='none')
deadline = time.monotonic() + 18
while not pathlib.Path('/scratch/go').exists():
    if time.monotonic() >= deadline:
        sys.exit(79)
    time.sleep(0.05)
if mode == 'wait':
    while time.monotonic() < deadline:
        time.sleep(0.05)
    sys.exit(80)
pathlib.Path('/output/result.txt').write_bytes(b'hello tessera local fixture\n')
emit('done', exit_code=0 if mode == 'success' else 7)
while not pathlib.Path('/scratch/finish').exists():
    if time.monotonic() >= deadline:
        sys.exit(81)
    time.sleep(0.05)
sys.exit(0 if mode == 'success' else 7)
"""


def log_records(raw, plan):
    """Retain only the finite reviewed log protocol, never raw errors or secrets."""
    if len(raw) > 4096:
        raise BackendBlocked("log bound")
    allowed = [
        dict(stage="limits_unknown", nonce=plan.nonce, package=plan.digest),
        dict(
            stage="ready",
            nonce=plan.nonce,
            package=plan.digest,
            filesystem="tmpfs-byte-inode-exhaustion",
            network="none",
        ),
        dict(
            stage="done",
            nonce=plan.nonce,
            package=plan.digest,
            exit_code=0 if plan.mode == "success" else 7,
        ),
    ]
    try:
        records = [json.loads(line) for line in raw.splitlines()]
        # Legacy undifferentiated unknown remains blocked, never upgraded to ready.
        if len(records) == 1 and records[0] in [
            allowed[0] | dict(check=check, failure=failure)
            for check in LIMIT_CHECKS
            for failure in LIMIT_FAILURES
        ]:
            return records
        if records not in [[], [allowed[0]], [allowed[1]], [allowed[1], allowed[2]]]:
            raise ValueError
        return records
    except Exception:
        raise BackendBlocked("unexpected fixture log; raw text discarded") from None
