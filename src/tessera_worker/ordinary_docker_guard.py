"""In-container immutable hard timer; approved argv only, no Docker/host credentials."""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def run(config, *, result_path=Path("/scratch/tessera-result.json")):
    # Never run as a host helper: namespace PID 1 is required before group-wide
    # cleanup. Engine config pins private PID namespace and disables init wrapping.
    if os.getpid() != 1 or os.geteuid() != 65532:
        return 126
    if (
        set(config) != {"version", "operation_id", "argv", "hard_expires_at"}
        or config["version"] != "ordinary-docker-guard-v1"
    ):
        raise ValueError("closed guard config required")
    deadline = datetime.fromisoformat(config["hard_expires_at"])
    if deadline.tzinfo is None:
        raise ValueError("aware guard deadline required")
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if not 0 < remaining <= 3600:
        return 124
    monotonic_end = time.monotonic() + remaining
    stopped = [False]
    previous = signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__(0, True))
    process = None
    try:
        process = subprocess.Popen(
            config["argv"],
            cwd="/outputs",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/scratch", "TMPDIR": "/scratch"},
            start_new_session=True,
            close_fds=True,
        )
        while process.poll() is None:
            if stopped[0] or time.monotonic() >= monotonic_end or datetime.now(UTC) >= deadline:
                return 143 if stopped[0] else 124
            time.sleep(0.05)
        code = process.returncode
        if type(code) is not int or not 0 <= code <= 255:
            code = 125
        # A direct-child exit must not leave descendants modifying output while
        # the worker snapshots it. The owned child group is never the guard group.
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if code != 0:
            return code
        # Atomic closed metadata only. Workload stdout/stderr never enter receipts.
        result = json.dumps(
            {"version": 1, "operation_id": config["operation_id"], "exit_code": code}
        ).encode()
        stage = result_path.with_suffix(".tmp")
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, result)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(stage, result_path)
        # Hold bounded tmpfs until worker capture. SIGTERM after child completion
        # acknowledges capture; it is not used as evidence that a running child exited.
        while not stopped[0]:
            if time.monotonic() >= monotonic_end or datetime.now(UTC) >= deadline:
                return 124
            time.sleep(0.05)
        return code
    finally:
        if process is not None:
            # Always kill the child's owned process group, including background
            # descendants left after the direct child exits, before exposing output.
            try:
                os.kill(-1, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        signal.signal(signal.SIGTERM, previous)


def main():
    try:
        if len(sys.argv) != 2 or len(sys.argv[1]) > 65536:
            return 126
        config = json.loads(base64.b64decode(sys.argv[1], validate=True))
        return run(config)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
