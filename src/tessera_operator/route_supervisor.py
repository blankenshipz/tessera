"""One bounded LaunchAgent tick; operator MCP stays separate from research MCP.

No direct Kubernetes effects, credential reads, process adoption or raw retries.
The operator's durable route receipt and identity guards govern every attempt.
"""

import asyncio
import fcntl
import json
import os
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import local_paths

ROOT = local_paths.REPOSITORY
PRIVATE = ROOT / ".tessera/private"


async def reconcile() -> bool:
    params = StdioServerParameters(
        command=str(ROOT / ".venv/bin/python"),
        args=["-m", "tessera_operator.kubernetes"],
        cwd=str(ROOT),
        env={
            "HOME": str(local_paths.HOME),
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    )
    # Child diagnostics and MCP bodies are not persisted: only success/failure.
    with open(os.devnull, "w") as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("local_loopback_route", {})
                return not result.isError


def private_lock(directory: Path) -> int:
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private supervisor directory required")
    fd = os.open(
        directory / "local-route-supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise ValueError("private supervisor lock required")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


async def bounded_tick() -> bool:
    async with asyncio.timeout(90):
        return await reconcile()


def _refuse(reason: str) -> None:
    # One fixed line; never exception text, which may carry sensitive diagnostics.
    print(f"route-supervisor: {reason}", file=sys.stderr)


def main() -> int:
    os.umask(0o077)
    if not local_paths.REPOSITORY_CONFIGURED:
        _refuse("repository is not configured")
        return 1
    try:
        lock = private_lock(PRIVATE)
    except BlockingIOError:
        _refuse("another supervisor tick holds the lock")
        return 1
    except Exception:
        _refuse("private supervisor directory or lock is not acceptable")
        return 1
    try:
        try:
            ok = asyncio.run(bounded_tick())
        except Exception:
            ok = False
        record = {
            "checked_at": datetime.now(UTC).isoformat(),
            "state": "route_verified" if ok else "blocked_inspect_operator_receipts",
        }
        fd, name = tempfile.mkstemp(prefix=".route-supervisor-", dir=PRIVATE)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, PRIVATE / "local-route-supervisor-status.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)
        if not ok:
            _refuse("route not verified; inspect operator receipts")
        return 0 if ok else 1
    finally:
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
