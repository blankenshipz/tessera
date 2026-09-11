"""Opt-in orchestration of existing synthetic OpenCode and worker fixtures.

This starts no real OpenCode server, provider, model, or credential flow. It
requires a locally present pinned Docker image because the existing development
worker fixture uses Docker.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPT_IN = "TESSERA_RUN_SYNTHETIC_COMBINED_E2E"
COMMAND_TIMEOUT_SECONDS = 300
CLEANUP_GRACE_SECONDS = 10


def _stop_process_group(process: subprocess.Popen[object]) -> None:
    """Best-effort stop for the fixture command and its locally spawned children."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.communicate(timeout=CLEANUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.communicate(timeout=CLEANUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _run_command(label: str, command: list[str], environment: dict[str, str]) -> None:
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        start_new_session=True,
    )
    try:
        process.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _stop_process_group(process)
        raise RuntimeError(
            f"{label} exceeded its {COMMAND_TIMEOUT_SECONDS}-second timeout; "
            "process-group cleanup was attempted, but this does not confirm child or "
            "container termination."
        ) from error
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)


def main() -> None:
    if os.environ.get(OPT_IN) != "1":
        raise SystemExit(f"Refusing Docker fixture; rerun with {OPT_IN}=1.")

    commands = (
        (
            "OpenCode synthetic harness fixtures",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_opencode_harness.py",
                "tests/test_opencode_live_adapter.py",
            ],
        ),
        (
            "development worker fake-provider fixture",
            [sys.executable, "scripts/check_development_e2e.py"],
        ),
    )
    environment = os.environ.copy()
    environment[OPT_IN] = "1"
    for label, command in commands:
        print(f"==> {label}")
        _run_command(label, command, environment)
    print("Synthetic combined E2E passed; no real OpenCode/provider/model activation occurred.")


if __name__ == "__main__":
    main()
