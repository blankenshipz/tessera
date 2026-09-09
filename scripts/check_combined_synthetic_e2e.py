"""Opt-in orchestration of existing synthetic OpenCode and worker fixtures.

This starts no real OpenCode server, provider, model, or credential flow. It
requires a locally present pinned Docker image because the existing development
worker fixture uses Docker.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPT_IN = "TESSERA_RUN_SYNTHETIC_COMBINED_E2E"


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
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
    print("Synthetic combined E2E passed; no real OpenCode/provider/model activation occurred.")


if __name__ == "__main__":
    main()
