"""Networkless output preparation/collection entry point in the installed image."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from .outputs import MAX_OUTPUT_BYTES, collect_outputs


def run(mode: str, identity: dict, paths: list[str], root: Path = Path("/outputs")):
    operation = identity["operation"]
    if not re.fullmatch(r"[0-9a-f]{32}", operation):
        raise ValueError("Invalid output operation")
    marker = root / (operation + ".provenance.json")
    directory = root / operation
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    if mode == "prepare":
        if not marker.exists():
            # Never adopt a pre-existing attempt directory or stale output set.
            if directory.exists() or directory.is_symlink():
                raise ValueError("Unowned output directory already exists")
            fd, temporary = tempfile.mkstemp(prefix=operation + ".pending-", dir=root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, marker, follow_symlinks=False)
                except FileExistsError:
                    pass  # Verify, never overwrite, a concurrently published marker.
            finally:
                os.unlink(temporary)
        verify(marker, encoded)
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Unsafe output directory")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"prepared": True, "identity": identity}
    if mode != "collect":
        raise ValueError("Invalid output pod mode")
    verify(marker, encoded)
    outputs = collect_outputs(directory, paths, MAX_OUTPUT_BYTES)
    verify(marker, encoded)
    return {
        "identity": identity,
        "outputs": [
            {
                "path": item.relative_path,
                "content": base64.b64encode(item.content).decode(),
                "sha256": item.sha256,
            }
            for item in outputs
        ],
    }


def verify(marker, expected):
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Unsafe output provenance marker")
        if stream.read(65537) != expected:
            raise ValueError("Output provenance does not match admitted attempt")


def main():
    payload = json.loads(base64.b64decode(sys.argv[2], validate=True))
    print(
        json.dumps(run(sys.argv[1], payload["identity"], payload["paths"]), separators=(",", ":"))
    )


if __name__ == "__main__":
    main()
