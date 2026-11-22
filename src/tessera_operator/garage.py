"""Fixed-target, stdio-only operator tools. No general shell, Docker or filesystem interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import ssl
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# Deployment-specific values come from the operator's environment, never from the package.
HOST = os.environ.get("TESSERA_GARAGE_HOST", "control-host.example")
USER = os.environ.get("TESSERA_GARAGE_USER", "tessera")
PROJECT = "tessera-garage-control"
GATEWAY_NAME = os.environ.get("TESSERA_GARAGE_GATEWAY", "control-gateway")
ROOT = "/srv/tessera-garage"
COMPOSE = Path(__file__).with_name("garage-compose.yaml")
SSH = (
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "StrictHostKeyChecking=yes",
    f"{USER}@{HOST}",
)

# This script is passed as a single, constant Python program. It accepts no remote
# arguments or paths from MCP calls. Remote stdout is structured, never raw Docker logs.
REMOTE = r"""
import datetime, hashlib, json, os, pathlib, subprocess, sys
root = pathlib.Path("/srv/tessera-garage")
project = "tessera-garage-control"
compose = root / "compose.yaml"
state = root / "control"
backups = root / "backups"
action = sys.argv[1]
expected = sys.argv[2]
def directory(p):
    if not p.is_dir() or p.is_symlink():
        raise ValueError("required dedicated directory unavailable")
def safe():
    directory(root)
    directory(state)
    directory(backups)
    if compose.is_symlink() or (compose.exists() and not compose.is_file()):
        raise ValueError("unsafe compose path")
def run(argv):
    result = subprocess.run(
        argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=90, check=False,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "DOCKER_HOST": "unix:///var/run/docker.sock",
             "TESSERA_TRUSTED_HOST": GATEWAY_NAME,
             "TESSERA_PUBLIC_ORIGIN": f"https://{GATEWAY_NAME}"},
    )
    if result.returncode:
        raise ValueError("dedicated operation failed; inspect locally on host")
    return result.stdout.decode("utf-8", "replace")
try:
    safe()
    if action == "inspect":
        configured = compose.exists() and (
            hashlib.sha256(compose.read_bytes()).hexdigest() == expected
        )
        backups_present = backups.is_dir() and not backups.is_symlink()
        print(json.dumps({"configured": configured,
                          "state_present": None,  # private 0700 state is not readable by SSH user
                          "backup_directory_present": backups_present}))
    elif action == "configure":
        body = sys.stdin.buffer.read(16385)
        if len(body) > 16384 or hashlib.sha256(body).hexdigest() != expected:
            raise ValueError("compose content mismatch")
        if compose.exists():
            if compose.read_bytes() != body:
                raise ValueError("existing compose differs; no overwrite")
        else:
            fd = os.open(compose, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        print(json.dumps({"configured": True}))
    else:
        if not compose.is_file() or hashlib.sha256(compose.read_bytes()).hexdigest() != expected:
            raise ValueError("dedicated compose not configured")
        base = ["docker", "--host", "unix:///var/run/docker.sock", "compose", "-p", project,
                "-f", str(compose)]
        if action == "status":
            data = json.loads(run(base + ["ps", "--format", "json"]) or "[]")
            if isinstance(data, dict): data = [data]
            print(json.dumps({"control": [{"service": row.get("Service"), "state": row.get("State")}
                                         for row in data if row.get("Service") == "control"]}))
        elif action == "deploy":
            # State is private to UID 65532; check only from the networkless
            # maintenance container, never make the SSH operator read owner files.
            run(base + ["--profile", "maintenance", "run", "--rm", "--no-deps",
                        "backup", "python", "-c",
                        "from pathlib import Path; "
                        "assert Path('/state/control/tessera.sqlite3').is_file()"])
            run(base + ["up", "-d", "--no-build", "--no-deps", "control"])
            print(json.dumps({"deployed": True}))
        elif action == "backup":
            directory(backups)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            name = "backup-" + stamp
            run(base + ["--profile", "maintenance", "run", "--rm", "--no-deps", "backup",
                        "tessera", "backup",
                        "--state-dir", "/state/control", "--output", "/backups/" + name])
            print(json.dumps({"backup": name}))
        else: raise ValueError("unsupported operation")
except Exception:
    print(json.dumps({"error": "dedicated operation unavailable; inspect garage host locally"}))
    sys.exit(1)
"""


class OperatorError(RuntimeError):
    pass


def compose_bytes() -> bytes:
    return COMPOSE.read_bytes()


def remote(action: str) -> dict:
    if action not in {"inspect", "configure", "status", "deploy", "backup"}:
        raise OperatorError("unsupported operation")
    body = compose_bytes()
    digest = hashlib.sha256(body).hexdigest()
    # SSH is fixed-host, key-only, and checked against an already trusted host key.
    # Neither stderr nor unstructured output is passed back to an MCP caller.
    result = subprocess.run(
        [*SSH, " ".join(map(shlex.quote, ("python3", "-c", REMOTE, action, digest)))],
        input=body if action == "configure" else None,
        capture_output=True,
        timeout=110,
        check=False,
    )
    if result.returncode:
        raise OperatorError("garage operation failed; inspect authorized host locally")
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or "error" in value:
            raise ValueError()
        return value
    except ValueError as error:
        raise OperatorError("unexpected garage response") from error


def verify_gateway() -> None:
    # A missing/untrusted private-network HTTPS gateway blocks deploy. No HTTP
    # fallback, insecure TLS mode, or implicit public proxy installation.
    import socket

    with socket.create_connection((HOST, 443), timeout=5) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=GATEWAY_NAME):
            pass


def create_server() -> FastMCP:
    server = FastMCP("Tessera garage human operator", log_level="CRITICAL")
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def garage_plan() -> dict:
        """Offline target/port/volume/Compose digest; performs no host access."""
        return {
            "target": f"{USER}@{HOST}",
            "project": PROJECT,
            "root": ROOT,
            "bind": "127.0.0.1:18766",
            "gateway": f"https://{GATEWAY_NAME}",
            "compose_sha256": hashlib.sha256(compose_bytes()).hexdigest(),
        }

    @server.tool(annotations=read)
    def garage_inspect() -> dict:
        """Inspect only dedicated installation paths, not arbitrary host files."""
        return remote("inspect")

    @server.tool(annotations=read)
    def garage_status() -> dict:
        """Read only this Compose project's control service state."""
        return remote("status")

    @server.tool(annotations=write)
    def garage_configure() -> dict:
        """Install fixed Compose only in pre-provisioned dedicated directory; never overwrite."""
        return remote("configure")

    @server.tool(annotations=write)
    def garage_deploy() -> dict:
        """Start only preconfigured control after verified HTTPS gateway and prepared state."""
        verify_gateway()
        return remote("deploy")

    @server.tool(annotations=write)
    def garage_backup() -> dict:
        """Create a timestamped SQLite-consistent backup in dedicated retained volume."""
        return remote("backup")

    return server


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Human-only fixed garage stdio operator MCP")
    parser.parse_args(argv)
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
