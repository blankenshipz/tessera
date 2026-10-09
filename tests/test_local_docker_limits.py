"""Execute the actual fixed probe offline against synthetic kernel/filesystem reads."""

import builtins
import errno
import io
import json
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.local_docker_contract import (
    LIMIT_CHECKS,
    LIMIT_FAILURES,
    PROGRAM,
    FixturePlan,
    log_records,
)


def probe(fault=None, read_error=None):
    plan = FixturePlan(
        operation_id="1" * 64, nonce="2" * 64, image="python@sha256:" + "a" * 64, mode="success"
    )
    output = io.StringIO()
    files = set()
    reads = []

    class File(io.BytesIO):
        def __init__(self, root):
            super().__init__()
            self.root = root

        def write(self, data):
            if self.tell() + len(data) > 1048576 and fault != self.root + ".byte_enospc":
                raise OSError(errno.ENOSPC, "SECRET_CANARY")
            return super().write(data)

    class Path(PurePosixPath):
        def read_text(self):
            reads.append(str(self))
            if self.name == "status":
                return "\n".join(
                    value
                    for check, value in (
                        ("capabilities", "CapEff:\t0000000000000000"),
                        ("no_new_privileges", "NoNewPrivs:\t1"),
                        ("seccomp", "Seccomp:\t2"),
                    )
                    if check != fault
                )
            if self.name == "memory.swap.max" and read_error:
                raise read_error
            value = {
                "memory.max": "134217728",
                "memory.swap.max": "0",
                "pids.max": "32",
                "cpu.max": "25000 100000",
            }[self.name]
            return "1" if self.name.replace(".", "_") == fault else value

        def write_text(self, value):
            assert str(self) == "/root-write-denied"
            raise OSError(
                errno.EACCES if fault == "readonly_root" else errno.EROFS, "SECRET_CANARY"
            )

        def open(self, *args, **kwargs):
            return File(self.parent.name)

        def unlink(self, **kwargs):
            files.discard(self)

        def touch(self):
            if len(files) == 31 and fault != self.parent.name + ".inode_enospc":
                raise OSError(errno.ENOSPC, "SECRET_CANARY")
            files.add(self)

        def iterdir(self):
            return iter(list(files))

    def statvfs(root):
        return SimpleNamespace(
            f_blocks=0 if fault == root[1:] + ".statvfs_bytes" else 256,
            f_frsize=4096,
            f_files=0 if fault == root[1:] + ".statvfs_inodes" else 32,
        )

    class ReadyObserved(Exception):
        pass

    def stop_after_ready():
        raise ReadyObserved

    modules = {
        "os": SimpleNamespace(
            getuid=lambda: 0 if fault == "uid" else 65532,
            getgid=lambda: 0 if fault == "gid" else 65532,
            listdir=lambda _: ["eth0", "lo"] if fault == "network" else ["lo"],
            statvfs=statvfs,
        ),
        "pathlib": SimpleNamespace(Path=Path),
        "socket": SimpleNamespace(
            if_nameindex=lambda: [(1, "lo"), (2, "eth0")] if fault == "network" else [(1, "lo")]
        ),
        "sys": SimpleNamespace(
            argv=["-c", plan.mode, plan.nonce, plan.digest], exit=lambda n: exit_probe(n)
        ),
        "time": SimpleNamespace(monotonic=stop_after_ready),
    }

    def exit_probe(code):
        raise SystemExit(code)

    def imports(name, *args, **kwargs):
        return modules[name] if name in modules else builtins.__import__(name, *args, **kwargs)

    namespace = {
        "__builtins__": vars(builtins)
        | {
            "__import__": imports,
            "print": lambda *args, **kwargs: print(*args, **(kwargs | {"file": output})),
        }
    }
    with pytest.raises(SystemExit if fault or read_error else ReadyObserved) as stopped:
        exec(compile(PROGRAM, "<fixed-fixture-probe>", "exec"), namespace)
    if fault or read_error:
        assert stopped.value.code == 78
    raw = output.getvalue().encode()
    assert b"SECRET_CANARY" not in raw
    return log_records(raw, plan), plan, reads


@pytest.mark.parametrize("check", LIMIT_CHECKS)
def test_each_restriction_failure_identified_and_no_ready(check):
    records, _, _ = probe(fault=check)
    assert len(records) == 1
    assert records[0]["stage"] == "limits_unknown"
    assert records[0]["check"] == check
    assert records[0]["failure"] == ("exception" if check == "cpu_max" else "assertion")


@pytest.mark.parametrize(
    "error,failure",
    [
        (AssertionError("SECRET_CANARY"), "assertion"),
        (FileNotFoundError(errno.ENOENT, "SECRET_CANARY"), "missing"),
        (PermissionError(errno.EACCES, "SECRET_CANARY"), "permission_denied"),
        (OSError(errno.ENOSPC, "SECRET_CANARY"), "no_space"),
        (OSError(errno.EROFS, "SECRET_CANARY"), "read_only"),
        (OSError(errno.EIO, "SECRET_CANARY"), "os_error"),
        (ValueError("SECRET_CANARY"), "exception"),
    ],
)
def test_missing_or_unreadable_swap_bound_is_not_waived(error, failure):
    records, _, reads = probe(read_error=error)
    assert records[0]["check"] == "memory_swap_max"
    assert records[0]["failure"] == failure
    assert reads[-1] == "/sys/fs/cgroup/memory.swap.max"
    assert len(records) == 1 and records[0]["stage"] == "limits_unknown"


def test_all_restrictions_pass_before_ready():
    records, _, _ = probe()
    assert len(records) == 1 and records[0]["stage"] == "ready"


@pytest.mark.parametrize(
    "change",
    [
        {"check": "SECRET_CANARY"},
        {"failure": "SECRET_CANARY"},
        {"raw": "SECRET_CANARY"},
        {"nonce": "3" * 64},
        {"package": "4" * 64},
        {"stage": "ready"},
    ],
)
def test_diagnostics_protocol_rejects_unreviewed_text_and_identity(change):
    records, plan, _ = probe(fault="memory_max")
    with pytest.raises(BackendBlocked, match="discarded"):
        log_records(json.dumps(records[0] | change).encode(), plan)


def test_diagnostic_cannot_precede_ready_or_done():
    records, plan, _ = probe(fault="memory_max")
    ready, _, _ = probe()
    for failure in LIMIT_FAILURES:
        raw = json.dumps(records[0] | {"failure": failure}).encode()
        assert len(log_records(raw, plan)) == 1
        with pytest.raises(BackendBlocked):
            log_records(raw + b"\n" + json.dumps(ready[0]).encode(), plan)
