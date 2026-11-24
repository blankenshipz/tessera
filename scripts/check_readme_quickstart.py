#!/usr/bin/env python3
"""Run the README "Local desktop/control setup" commands non-interactively.

Installs with ``uv sync --locked --extra mcp``, runs ``tessera init`` into a
temporary state directory, serves on a loopback port, probes ``/health``,
stops the child it started, then runs ``tessera backup`` and
``tessera deploy-check`` (state-only; no provider, Docker or secret is used).
The desktop steps (``npm``) are not exercised here.

Usage: python scripts/check_readme_quickstart.py [--skip-sync] [--timeout SECONDS]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README_COMMANDS = (
    "uv sync --locked --extra mcp",
    "uv run --locked tessera init --state-dir .tessera",
    "uv run --locked tessera serve --state-dir .tessera --port 8765",
)


def readme_quickstart_commands(readme_text: str) -> list[str]:
    """Return the fenced sh lines of the README local-setup section."""
    section = re.search(r"### Local desktop/control setup\n(.*?)\n##", readme_text, re.S)
    if section is None:
        raise ValueError("README has no 'Local desktop/control setup' section")
    commands: list[str] = []
    for block in re.findall(r"```sh\n(.*?)```", section.group(1), re.S):
        commands.extend(line.strip() for line in block.splitlines() if line.strip())
    return commands


def check_readme_matches(readme_text: str) -> None:
    documented = readme_quickstart_commands(readme_text)
    missing = [command for command in README_COMMANDS if command not in documented]
    if missing:
        raise ValueError(f"README quickstart no longer documents: {missing}")


def free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def tessera(*arguments: str) -> list[str]:
    return ["uv", "run", "--locked", "tessera", *arguments]


def run(command: list[str], *, timeout: float) -> str:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def wait_healthy(base: str, process: subprocess.Popen, deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("tessera serve exited before it became healthy")
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    return
        except (OSError, urllib.error.URLError, ValueError):
            time.sleep(0.2)
    raise RuntimeError("tessera serve did not become healthy in time")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-sync", action="store_true", help="environment already synced")
    parser.add_argument("--timeout", type=float, default=240, help="seconds per step")
    args = parser.parse_args(argv)
    check_readme_matches((ROOT / "README.md").read_text())
    with tempfile.TemporaryDirectory(prefix="tessera-quickstart-") as scratch:
        state = Path(scratch) / ".tessera"
        if not args.skip_sync:
            run(["uv", "sync", "--locked", "--extra", "mcp"], timeout=args.timeout)
        run(tessera("init", "--state-dir", str(state)), timeout=args.timeout)
        if (state / "owner.header").stat().st_mode & 0o077:
            raise RuntimeError("owner.header is not private")
        port = free_loopback_port()
        log_path = Path(scratch) / "serve.log"
        with log_path.open("w") as log:
            server = subprocess.Popen(
                tessera("serve", "--state-dir", str(state), "--port", str(port)),
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            try:
                wait_healthy(f"http://127.0.0.1:{port}", server, time.monotonic() + args.timeout)
                print("health: ok", flush=True)
            except Exception:
                print(log_path.read_text(), file=sys.stderr)
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=15)
        run(
            tessera("backup", "--state-dir", str(state), "--output", str(Path(scratch) / "bk")),
            timeout=args.timeout,
        )
        if not (Path(scratch) / "bk" / "manifest.json").is_file():
            raise RuntimeError("backup wrote no manifest")
        run(tessera("deploy-check", "--state-dir", str(state)), timeout=args.timeout)
    print("README quickstart: ok")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("UV_NO_PROGRESS", "1")
    raise SystemExit(main())
