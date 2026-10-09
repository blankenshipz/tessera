"""Explicit opt-in hook for the provider-disabled single-active restart drill.

Without ``--run`` this command performs no Kubernetes, Docker, network, provider,
or filesystem mutation. It intentionally does not automate PVC loss or a held
restore promotion: those procedures retain evidence and require human fencing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser()
    command.add_argument(
        "--run", action="store_true", help="run the existing isolated restart fixture"
    )
    command.add_argument("--kubeconfig", type=Path)
    command.add_argument("--context")
    command.add_argument("--namespace")
    return command


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if not arguments.run:
        print("Refusing live infrastructure effects without --run.", file=sys.stderr)
        return 2
    if not all((arguments.kubeconfig, arguments.context, arguments.namespace)):
        print("--run requires --kubeconfig, --context, and --namespace.", file=sys.stderr)
        return 2
    if arguments.context != "docker-desktop":
        print(
            "The isolated provider-disabled harness supports only docker-desktop.", file=sys.stderr
        )
        return 2
    script = Path(__file__).with_name("check_kubernetes_control_plane_restart.py")
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--run",
            "--kubeconfig",
            str(arguments.kubeconfig),
            "--context",
            arguments.context,
            "--namespace",
            arguments.namespace,
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
