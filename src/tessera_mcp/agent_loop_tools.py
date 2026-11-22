"""Opt-in MCP tools to see and pause the standing agent loop (ADR 0089).

The loop's kill switch is a marker file (ADR 0085). These tools read that marker and, optionally,
the loop's call ledger, and can create the marker. Nothing here removes or edits a marker, and
nothing here talks to the control API, a provider or a worker.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tessera_agent.standing_loop import KillSwitch
from tessera_mcp.client import ControlClient, segment

UNCERTAIN_CAP = 50
REASON_CAP = 200
BOUNDARY = (
    "This is the operator's local kill-switch marker and loop ledger. It is not provider "
    "billing and not proof that a call ran or did not run."
)


def _marker_present(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def _read_ledger(ledger_path: Path | None) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "halted": None,
        "consecutive_failures": None,
        "backoff_until": None,
        "uncertain_item_ids": [],
    }
    if ledger_path is None:
        return empty
    try:
        uri = Path(os.path.abspath(ledger_path)).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)) as conn:
            state = dict(conn.execute("SELECT key, value FROM loop_state").fetchall())
            uncertain = conn.execute(
                "SELECT item_id FROM calls WHERE state='uncertain' ORDER BY item_id LIMIT ?",
                (UNCERTAIN_CAP,),
            ).fetchall()
        return {
            "halted": state.get("halted") == "1",
            "consecutive_failures": int(state.get("consecutive_failures") or 0),
            "backoff_until": float(state.get("backoff_until") or 0.0),
            "uncertain_item_ids": [row[0] for row in uncertain],
        }
    except (sqlite3.Error, OSError, ValueError):
        return empty


def _open_private_parent(marker_path: Path) -> int:
    """Open the marker's parent directory and check it; return the directory fd."""
    parent = marker_path.parent
    try:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(
            "Refusing to pause: the marker's parent directory is missing, a symlink or unreadable."
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Refusing to pause: the marker's parent is not owned by this user.")
        if info.st_mode & 0o022:
            raise ValueError(
                "Refusing to pause: the marker's parent directory is group- or world-writable."
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def register_agent_loop_tools(
    server: FastMCP,
    marker_path: Path,
    ledger_path: Path | None = None,
    client: ControlClient | None = None,
) -> None:
    """``client`` makes a pause a recorded platform act (an authenticated control call that
    writes an event) before the local marker is created; without it the tool is marker-only."""
    marker_path = Path(marker_path)
    switch = KillSwitch(marker_path)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    async def tessera_agent_loop_status() -> dict[str, Any]:
        """Show whether the standing agent loop's kill switch is engaged, plus ledger state.

        `engaged` is the loop's own fail-closed answer: a marker that exists, cannot be read or
        has a missing parent directory all count as engaged. Ledger fields are null when no
        ledger is configured or it cannot be read; they are never invented. The ledger is
        opened read-only.
        """
        engaged = switch.engaged()
        if not engaged:
            reason = "clear"
        elif _marker_present(marker_path):
            reason = "marker_present"
        else:
            reason = "marker_unreadable"
        return {
            "engaged": engaged,
            "reason": reason,
            **_read_ledger(ledger_path),
            "boundary": BOUNDARY,
        }

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    async def tessera_pause_agent_loop(reason: str) -> dict[str, Any]:
        """Pause the standing agent loop: record the request on the platform, then engage the
        kill switch by creating its marker file (the fast path the loop reads).

        The platform record needs the `loop:pause` scope and names the calling principal; a
        credential without it is refused and no marker is created. The record says a pause was
        requested, not that the loop saw it. Pause only. There is deliberately no resume tool:
        lifting a pause is the operator's act (delete the marker file). This never removes or
        modifies an existing marker. It cannot cancel in-flight calls, campaigns, workers or
        routes; the kill switch touches nothing else. Refused when the marker's parent directory
        is missing, a symlink, not owned by the current user, or group- or world-writable.
        """
        if not isinstance(reason, str):
            raise ValueError("reason must be a string.")
        recorded: dict[str, Any] | None = None
        if client is not None:
            project = segment(client.config.project_id)
            recorded = await client._request(
                "POST",
                f"/projects/{project}/agent-loop/pause",
                body={"reason": reason[:REASON_CAP] or "pause"},
                key="loop-pause-" + uuid.uuid4().hex,
            )
        dir_fd = _open_private_parent(marker_path)
        try:
            try:
                fd = os.open(
                    marker_path.name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=dir_fd,
                )
            except FileExistsError:
                return {"engaged": True, "already": True, "recorded": recorded}
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"paused_by": "mcp", "reason": reason[:REASON_CAP]}, handle)
        finally:
            os.close(dir_fd)
        return {"engaged": True, "already": False, "recorded": recorded}
