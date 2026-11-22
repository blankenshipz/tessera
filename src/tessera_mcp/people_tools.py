"""Owner-only MCP tools to enrol and renew short-lived people (``--person-enrollment-tool``).

Run as a separate operator server with the OWNER's own credential file. The control API still
decides: it accepts these calls only from the owner, so any other credential gets a 403 that is
passed through unchanged.

Secret handling: ``tessera_enroll_person`` generates the new person's bearer token itself, sends
it to the control API, and writes it to ``credential_path`` as an ``Authorization: Bearer``
header file (mode 0600, created exclusively). The token is never part of a tool result or an
error message. Replaying the same idempotency key within one server process returns the stored
ids with ``credential_written: false`` and needs no token; the file is written only by the first
successful call. After a restart a replay is refused if the path already exists (never overwritten).
"""

import os
import secrets
import stat
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, StringConstraints

from tessera_mcp.client import ControlClient, ControlError, segment

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ValidHours = Annotated[int, Field(strict=True, ge=1, le=168)]


def _expiry(valid_hours: int) -> str:
    moment = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=valid_hours)
    return moment.isoformat().replace("+00:00", "Z")


def _check_target(credential_path: str) -> str:
    if not credential_path or "\x00" in credential_path:
        raise ValueError("credential_path is invalid")
    path = os.path.abspath(credential_path)
    directory = os.path.dirname(path)
    try:
        info = os.stat(directory)
    except OSError:
        raise ValueError("credential_path directory must already exist") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise ValueError("credential_path directory must not be group or world writable")
    if os.path.lexists(path):
        raise ValueError("credential_path already exists; refusing to overwrite")
    return path


def _write_header(path: str, token: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            stream.write(f"Authorization: Bearer {token}\n")
    except BaseException:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def register_person_enrollment_tools(server: FastMCP, client: ControlClient) -> None:
    write = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
    read = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    completed: dict[str, dict[str, Any]] = {}

    @server.tool(annotations=write)
    async def tessera_enroll_person(
        label: Label,
        credential_path: str,
        valid_hours: ValidHours = 72,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Enrol a short-lived person (the owner's credential only).

        Generates the person's token here, sends it to the control API and writes it to
        `credential_path` (mode 0600, created exclusively, never overwritten; the directory must
        exist and not be group/world writable). The result has only ids, label, expiry and the
        path, never the token. If the control call fails nothing is written. If the file cannot
        be written after control accepted, the enrolled credential exists but is unusable until
        renewed or revoked. A replay with the same key returns the stored ids with
        `credential_written: false`.
        """
        key = idempotency_key or "enroll-person-" + uuid.uuid4().hex
        if key in completed:
            return {**completed[key], "credential_written": False}
        path = _check_target(credential_path)
        token = secrets.token_urlsafe(32)
        stored = await client._request(
            "POST",
            "/people",
            body={"token": token, "label": label, "expires_at": _expiry(valid_hours)},
            key=key,
        )
        result = {
            name: stored.get(name)
            for name in (
                "principal_id",
                "credential_id",
                "label",
                "role",
                "kind",
                "expires_at",
                "enrolled_by_principal_id",
            )
        }
        try:
            _write_header(path, token)
        except OSError:
            raise ControlError(
                "The control API enrolled the person (principal "
                f"{result['principal_id']}, credential {result['credential_id']}) but the "
                "credential file could not be written; that credential is unusable until it is "
                "renewed or revoked. Not retried."
            ) from None
        result["credential_path"] = path
        result["credential_written"] = True
        completed[key] = result
        return result

    @server.tool(annotations=read)
    async def tessera_list_people(offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """List enrolled people (the owner's credential only)."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Pagination requires offset >= 0 and limit 1-100")
        return await client._request("GET", f"/people?offset={offset}&limit={limit}")

    @server.tool(annotations=write)
    async def tessera_renew_person(
        principal_id: str,
        valid_hours: ValidHours = 72,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Renew a person's expiry to now + valid_hours (the owner's credential only)."""
        return await client._request(
            "POST",
            f"/people/{segment(principal_id)}/renew",
            body={"expires_at": _expiry(valid_hours)},
            key=idempotency_key or "renew-person-" + uuid.uuid4().hex,
        )
