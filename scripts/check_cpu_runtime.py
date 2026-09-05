"""Opt-in live Docker fixture; no control grant or research result is fabricated.

Run with `.venv/bin/python scripts/check_cpu_runtime.py`. The pinned image must
already exist locally. This starts only synthetic, networkless containers and
removes those exact containers after observing their ownership labels.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tessera_worker.cpu import CpuOperation, DockerCpuAdapter
from tessera_worker.supervisor import CpuSupervisor, Lease

IMAGE = (
    "python:3.11-slim-bookworm@sha256:"
    "528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84"
)
ISOLATION_PROBE = """
import errno
import os
import socket
from pathlib import Path

assert os.getuid() == 65532
assert not Path('/var/run/docker.sock').exists()
# Some kernels expose dormant tunnel devices even in a networkless namespace.
# Check usable interfaces and routing rather than assuming only one sysfs entry.
for _, name in socket.if_nameindex():
    flags = int(Path('/sys/class/net', name, 'flags').read_text(), 16)
    assert name == 'lo' or not flags & 1, 'non-loopback interface is up'
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
    try:
        probe.connect(('192.0.2.1', 9))
    except OSError as error:
        assert error.errno == errno.ENETUNREACH
    else:
        raise AssertionError('external route exists')
status = Path('/proc/self/status').read_text().splitlines()
assert 'NoNewPrivs:\t1' in status
assert any(line.startswith('CapEff:') and int(line.split()[1], 16) == 0 for line in status)
try:
    Path('/etc/tessera-synthetic-probe').write_text('must fail')
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES)
else:
    raise AssertionError('root filesystem was writable')
Path('/work/synthetic.txt').write_text('bounded scratch is writable')
"""


def docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode:
        raise RuntimeError(f"Docker fixture command failed: {result.stderr[:1000].strip()}")
    return result


def run_fixture(name: str, code: str, *, revoke: bool = False, duration: int = 10) -> dict:
    operation = CpuOperation(
        uuid.uuid4().hex,
        IMAGE,
        ("python", "-c", code),
        hashlib.sha256(code.encode()).hexdigest(),
        duration,
    )
    lease = Lease(
        id="synthetic-local-fixture",
        operation_id=operation.operation_id,
        plan_digest=operation.plan_digest,
        epoch=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        task_revision=1,
        project_id="synthetic-no-control-dispatch",
    )
    adapter = DockerCpuAdapter(frozenset({IMAGE}))
    authority = True
    try:
        with tempfile.TemporaryDirectory(prefix="tessera-cpu-fixture-") as directory:
            with CpuSupervisor(Path(directory), adapter, lambda _: authority) as supervisor:
                result = supervisor.drive_once(operation, lease)
                if revoke:
                    if result.observation.state != "running":
                        raise RuntimeError("Cancellation fixture did not reach running state.")
                    authority = False
                deadline = time.monotonic() + 15
                while result.phase not in {"terminal", "denied"}:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Fixture exceeded its independent watchdog.")
                    time.sleep(0.1)
                    result = supervisor.tick(operation, lease)
                observed = adapter.observe(operation)
                if not observed.termination_confirmed:
                    raise RuntimeError("Docker has not confirmed termination.")
                if name == "isolation" and observed.exit_code != 0:
                    raise RuntimeError("Container isolation assertion failed.")
                if revoke and result.reason != "cancelled_lease_denied":
                    raise RuntimeError("Revocation did not cause the observed stop.")
                if name == "duration" and result.reason != "cancelled_duration_elapsed":
                    raise RuntimeError("Duration limit did not cause the observed stop.")
                return {
                    "fixture": name,
                    "provider_terminated": observed.termination_confirmed,
                    "exit_code": observed.exit_code,
                    "reason": result.reason,
                }
    finally:
        # This is fixture cleanup, separate from evidence about supervisor stops.
        # A label mismatch raises and prevents deleting a foreign container.
        observed = adapter.observe(operation)
        if observed.container_id is not None:
            docker("rm", "--force", observed.container_id)
        elif observed.state != "absent":
            raise RuntimeError(
                f"Cleanup is unconfirmed; inspect fixture {operation.container_name}."
            )


def main() -> None:
    docker("image", "inspect", IMAGE)
    for name, code, revoke, duration in (
        ("isolation", ISOLATION_PROBE, False, 10),
        ("revocation", "import time; time.sleep(120)", True, 10),
        ("duration", "import time; time.sleep(120)", False, 2),
    ):
        report = run_fixture(name, code, revoke=revoke, duration=duration)
        print(json.dumps(report, sort_keys=True))
    print("Synthetic CPU fixtures passed; control dispatch and artifacts remain untested.")


if __name__ == "__main__":
    main()
