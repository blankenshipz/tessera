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
from .local_docker_namespace import (
    KERNEL_FALLBACK_TUNNELS,
    OBSERVER,
    STRONG_CLASS,
    WEAK_CLASS,
    WEAK_OBSERVER,
    attestation_fields,
)

PROFILE = "local-networkless-fixture-v1"
# The attestation class is DERIVED from the profile (no second field to desync), so
# v1 plans keep a byte-identical model_dump and digest.
PROFILE_ENGINE_ATTESTED = "local-networkless-fixture-engine-attested-v1"
PROFILE_CLASS = {PROFILE: STRONG_CLASS, PROFILE_ENGINE_ATTESTED: WEAK_CLASS}
OUTPUT = b"hello tessera local fixture\n"
FS_BYTES = 1024 * 1024
INODES = 32
MEMORY = 128 * 1024 * 1024
PIDS = 32
CAPTURE = 128 * 1024
OUTPUT_VOLUME_PREFIX = "tessera-local-out-"
# Labels that bind a volume (and its container) to one operation; read back on both.
VOLUME_LABELS = (
    "tessera.fixture",
    "tessera.package",
    "tessera.operation",
    "tessera.nonce",
    "tessera.output_volume",
    "tessera.scope",
)


def output_volume_options():
    """Same size/inode/mode/noexec limits the container --tmpfs carried, as a local volume.

    The daemon archive API reads rootfs and volumes only; a container-namespace tmpfs
    is invisible to `docker cp`, a tmpfs-typed local volume is not.
    """
    return dict(
        type="tmpfs",
        device="tmpfs",
        o=(f"size={FS_BYTES},nr_inodes={INODES},uid=65532,gid=65532,mode=0700,noexec,nosuid,nodev"),
    )


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
    profile: Literal[
        "local-networkless-fixture-v1", "local-networkless-fixture-engine-attested-v1"
    ] = PROFILE
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
    def attestation_class(self):
        return PROFILE_CLASS[self.profile]

    @property
    def attestation(self):
        return attestation_fields(self.attestation_class)

    @property
    def output_volume_name(self):
        return OUTPUT_VOLUME_PREFIX + self.nonce

    @property
    def output_volume_options(self):
        return output_volume_options()

    @property
    def program_digest(self):
        return hashlib.sha256(program_for(self).encode()).hexdigest()

    @property
    def digest(self):
        return canonical_digest(
            [
                self.model_dump(mode="json"),
                self.program_digest,
                hashlib.sha256(observer_for(self).encode()).hexdigest(),
                # The /output volume (exact name, driver, options) is part of the package.
                dict(
                    name=self.output_volume_name,
                    driver="local",
                    options=self.output_volume_options,
                ),
            ]
        )

    @property
    def name(self):
        return "tessera-local-" + self.nonce

    @property
    def labels(self):
        return {
            "tessera.fixture": self.profile,
            "tessera.package": self.digest,
            "tessera.operation": self.operation_id,
            "tessera.nonce": self.nonce,
            "tessera.output_volume": self.output_volume_name,
            "tessera.scope": canonical_digest([self.org_id, self.project_id]),
        }


class LocalFixtureConfig(Frozen):
    enabled: bool = False
    target: Literal["unix:///var/run/docker.sock"] = "unix:///var/run/docker.sock"
    # Default strong. The weak class is explicit operator selection, never a fallback.
    attestation_class: Literal["daemon_host_independent", "engine_declared_workload_observed"] = (
        STRONG_CLASS
    )


def require_class_match(plan, config):
    """Config class must equal the plan class, before any filesystem/process effect."""
    if plan.attestation_class != config.attestation_class:
        raise BackendBlocked("attestation class mismatch between plan and config")


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


# Weak class only. The strong program above (and every v1 digest) is untouched.
# Same strict intent: lo exactly once, and any other link must be a reviewed kernel
# fallback tunnel that is DOWN with no IPv4/IPv6 address and no route referencing it.
WEAK_NETWORK_HELPERS = r"""import fcntl, struct
FALLBACK = frozenset(__FALLBACK__)
def table(path):
    with open(path) as f:
        data = f.read(262145)
    assert len(data) <= 262144
    return [line.split() for line in data.splitlines() if line.strip()]
def netstate():
    rows = socket.if_nameindex()
    state = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for index, name in rows:
            request = struct.pack('16s16x', name.encode()[:15])
            flags = struct.unpack('16xH14x', fcntl.ioctl(s.fileno(), 0x8913, request))[0]
            try:
                fcntl.ioctl(s.fileno(), 0x8915, request)
                v4 = 1
            except OSError as e:
                if e.errno != errno.EADDRNOTAVAIL:
                    raise
                v4 = 0
            state[name] = dict(index=index, up=bool(flags & 1), v4=v4, v6=0, routes=0)
    stray = 0
    for path, column, key, skip in (('/proc/net/route', 0, 'routes', 1),
            ('/proc/net/if_inet6', 5, 'v6', 0), ('/proc/net/ipv6_route', -1, 'routes', 0)):
        for row in table(path)[skip:]:
            if row[column] in state:
                state[row[column]][key] += 1
            else:
                stray += 1
    return rows, state, stray
def netdiag():
    rows, state, stray = netstate()
    try:
        nameservers = sum(1 for line in table('/etc/resolv.conf')
            if line[:1] == ['nameserver'])
    except Exception:
        nameservers = -1
    items = [dict(name=name if name == 'lo' or name in FALLBACK else 'other',
            index=min(v['index'], 2147483647), up=v['up'], v4=min(v['v4'], 99),
            v6=min(v['v6'], 9999), routes=min(v['routes'], 9999))
        for name, v in list(state.items())[:16]]
    result = dict(interfaces=items, link_count=min(len(rows), 4096),
        nameservers=min(nameservers, 99))
    if len(json.dumps(result)) > 1024:
        result['interfaces'] = []
    return result
"""
WEAK_NETWORK_CHECK = r"""    check_id = 'network'
    names = [name for _, name in socket.if_nameindex()]
    assert len(set(names)) == len(names) and names.count('lo') == 1
    assert all(name == 'lo' or name in FALLBACK for name in names)
    _, state, stray = netstate()
    assert stray == 0 and sorted(state) == sorted(names)
    for name, v in state.items():
        assert name == 'lo' or (not v['up'] and not v['v4'] and not v['v6'] and not v['routes'])
"""
WEAK_EMIT = r"""    extra = {}
    if check_id == 'network':
        try:
            extra = dict(network_diag=netdiag())
        except Exception:
            pass
    emit('limits_unknown', check=check_id, failure=failure, **extra)
"""
STRONG_NETWORK_CHECK = PROGRAM[
    PROGRAM.index("    check_id = 'network'") : PROGRAM.index("    check_id = 'capabilities'")
]
STRONG_EMIT = "    emit('limits_unknown', check=check_id, failure=failure)\n"
TRY_HEAD = "try:\n    check_id = 'uid'"
assert STRONG_NETWORK_CHECK.count("\n") == 4 and STRONG_EMIT in PROGRAM and TRY_HEAD in PROGRAM
WEAK_PROGRAM = (
    PROGRAM.replace(
        TRY_HEAD,
        WEAK_NETWORK_HELPERS.replace("__FALLBACK__", repr(tuple(sorted(KERNEL_FALLBACK_TUNNELS))))
        + TRY_HEAD,
    )
    .replace(STRONG_NETWORK_CHECK, WEAK_NETWORK_CHECK)
    .replace(STRONG_EMIT, WEAK_EMIT)
)


def program_for(plan):
    return WEAK_PROGRAM if plan.attestation_class == WEAK_CLASS else PROGRAM


def observer_for(plan):
    return WEAK_OBSERVER if plan.attestation_class == WEAK_CLASS else OBSERVER


def _count(value, low, high):
    return type(value) is int and low <= value <= high


def valid_network_diag(diag):
    """Strict shape/type/size gate for the weak-only bounded diagnostic."""
    try:
        known = {"lo"} | KERNEL_FALLBACK_TUNNELS | {"other"}
        if not isinstance(diag, dict) or set(diag) != {"interfaces", "link_count", "nameservers"}:
            return False
        items = diag["interfaces"]
        if not isinstance(items, list) or len(items) > 16:
            return False
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"name", "index", "up", "v4", "v6", "routes"}
                or item["name"] not in known
                or not _count(item["index"], 0, 2147483647)
                or type(item["up"]) is not bool
                or not _count(item["v4"], 0, 99)
                or not _count(item["v6"], 0, 9999)
                or not _count(item["routes"], 0, 9999)
            ):
                return False
        return (
            _count(diag["link_count"], 0, 4096)
            and _count(diag["nameservers"], -1, 99)
            and len(json.dumps(diag)) <= 1024
        )
    except Exception:
        return False


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
        # Weak class only: the same limits_unknown record may carry one bounded,
        # path-free network diagnostic, and only for the network check.
        if (
            len(records) == 1
            and plan.attestation_class == WEAK_CLASS
            and isinstance(records[0], dict)
            and "network_diag" in records[0]
        ):
            bare = {k: v for k, v in records[0].items() if k != "network_diag"}
            if valid_network_diag(records[0]["network_diag"]) and bare in [
                allowed[0] | dict(check="network", failure=failure) for failure in LIMIT_FAILURES
            ]:
                return records
            raise ValueError
        if records not in [[], [allowed[0]], [allowed[1]], [allowed[1], allowed[2]]]:
            raise ValueError
        return records
    except Exception:
        raise BackendBlocked("unexpected fixture log; raw text discarded") from None
