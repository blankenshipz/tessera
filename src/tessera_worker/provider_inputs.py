"""Pull a ticket-authorized input snapshot from control and verify it (ADR 0037 s3).

Stdlib-only so it can run inside a provider/GPU image.  The ticket is the only credential;
it is never logged or printed.  The expected digest comes from the approved plan, never
from the server.  Bytes are verified by ``materialize_chunked_snapshot`` and a partial tree
is removed on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .chunked_inputs import materialize_chunked_snapshot
from .inputs import InputIntegrityError, MaterializedSnapshot

# A 4 MiB manifest plus a chunk index for 10,000 files and 2 GiB fits with margin.
MAX_MANIFEST_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CHUNK_RESPONSE_BYTES = 6 * 1024 * 1024
TIMEOUT_SECONDS = 30.0
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

Opener = Callable[[Request, float], Any]


class ProviderInputError(InputIntegrityError):
    """A sanitized fetch failure; never contains the ticket."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        raise ProviderInputError("Redirects are not followed.")


def _default_open(request: Request, timeout: float) -> Any:
    return build_opener(ProxyHandler({}), _NoRedirects()).open(request, timeout=timeout)


def _origin(origin: str) -> str:
    if not isinstance(origin, str) or len(origin) > 2048:
        raise ProviderInputError("Control origin is invalid.")
    try:
        parts = urlsplit(origin)
        host = parts.hostname
        parts.port  # noqa: B018 - validates the port
    except ValueError as error:
        raise ProviderInputError("Control origin is invalid.") from error
    if (
        host is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in origin)
        or parts.scheme not in {"http", "https"}
        or (parts.scheme == "http" and host not in _LOOPBACK)
    ):
        raise ProviderInputError("Control origin must be HTTPS (HTTP only for loopback).")
    return f"{parts.scheme}://{parts.netloc}"


def _get(
    opener: Opener,
    base: str,
    path: str,
    token: str,
    limit: int,
    timeout: float = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request = Request(
        base + path,
        headers={"Authorization": "Ticket " + token, "Accept": "application/json"},
        method="GET",
    )
    try:
        response = opener(request, timeout)
    except HTTPError as error:
        status = error.code
        error.close()
        raise ProviderInputError(f"Control refused the input request (HTTP {status}).") from None
    except (URLError, OSError, ValueError):
        raise ProviderInputError("Control could not be reached for inputs.") from None
    try:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        content_type = response.headers.get("Content-Type", "")
        if status != 200 or content_type.split(";")[0].strip().lower() != "application/json":
            raise ProviderInputError("Control returned an unexpected input response.")
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ProviderInputError("Control input response exceeds its byte bound.")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise ProviderInputError("Control input response is not valid JSON.") from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not isinstance(value, dict):
        raise ProviderInputError("Control input response is malformed.")
    return value


def fetch_snapshot(
    origin: str,
    token: str,
    parent: Path,
    *,
    expected_snapshot_digest: str,
    opener: Opener | None = None,
) -> MaterializedSnapshot:
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 4096
        or any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in token)
    ):
        raise ProviderInputError("Input ticket is malformed.")
    base = _origin(origin)
    send = opener or _default_open
    manifest = _get(send, base, "/provider-inputs/manifest", token, MAX_MANIFEST_RESPONSE_BYTES)
    # Pre-fetch refusal: the plan-pinned digest must equal what the server claims; materialize
    # then re-derives the digest from the manifest itself and never trusts either.
    if manifest.get("digest") != expected_snapshot_digest:
        raise ProviderInputError("Server snapshot digest does not match the approved plan.")
    snapshot = {
        "id": manifest.get("snapshot_id"),
        "digest": manifest.get("digest"),
        "manifest": manifest.get("manifest"),
        "delivery": manifest.get("delivery"),
        "chunks": manifest.get("chunks"),
    }

    def fetch_chunk(seq: int) -> dict[str, Any]:
        return _get(send, base, f"/provider-inputs/chunks/{seq}", token, MAX_CHUNK_RESPONSE_BYTES)

    return materialize_chunked_snapshot(
        snapshot, fetch_chunk, Path(parent), expected_snapshot_digest=expected_snapshot_digest
    )


def _read_token(path: Path) -> str:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 4096
        ):
            raise ProviderInputError("Token file must be a private 0600 regular file.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            return os.read(descriptor, 4097).decode("ascii").strip()
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError) as error:
        raise ProviderInputError("Token file could not be read.") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tessera_worker.provider_inputs")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--parent", required=True, type=Path)
    parser.add_argument("--expected-digest", required=True)
    args = parser.parse_args(argv)
    try:
        snapshot = fetch_snapshot(
            args.origin,
            _read_token(args.token_file),
            args.parent,
            expected_snapshot_digest=args.expected_digest,
        )
    except InputIntegrityError as error:
        print(f"provider input fetch failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "directory": str(snapshot.directory),
                "snapshot_digest": snapshot.snapshot_digest,
                "file_count": snapshot.file_count,
                "total_bytes": snapshot.total_bytes,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
