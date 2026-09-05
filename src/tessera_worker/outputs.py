"""Bounded collection of declared files from a stopped workspace.

This module only reads files after a caller has established that the workspace
belongs to a stopped operation.  It does not inspect provider state, establish
custody, or seal/upload the returned bytes.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import stat
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_PATHS = 1000
MAX_OUTPUT_PATH_BYTES = 4096
MAX_OUTPUT_SEGMENTS = 64
MAX_OUTPUT_SEGMENT_BYTES = 255


class OutputCollectionError(Exception):
    """A declared output could not be collected safely."""


@dataclass(frozen=True)
class CollectedOutput:
    """Immutable bytes and digest for one declared relative output path."""

    relative_path: str
    content: bytes
    sha256: str


def _failure(reason: str) -> OutputCollectionError:
    return OutputCollectionError(f"Output collection failed: {reason}.")


def _validate_paths(output_paths: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(output_paths, Sequence) or isinstance(output_paths, (str, bytes, bytearray)):
        raise _failure("invalid output path list")
    if len(output_paths) > MAX_OUTPUT_PATHS:
        raise _failure("too many output paths")

    parsed: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for value in output_paths:
        if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
            raise _failure("invalid output path")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _failure("invalid output path") from error
        if len(encoded) > MAX_OUTPUT_PATH_BYTES or value.startswith("/"):
            raise _failure("invalid output path")
        drive, _ = ntpath.splitdrive(value)
        if drive:
            raise _failure("invalid output path")

        parts = value.split("/")
        if not 1 <= len(parts) <= MAX_OUTPUT_SEGMENTS or any(
            not part or part in {".", ".."} for part in parts
        ):
            raise _failure("invalid output path")
        if any(len(part.encode("utf-8")) > MAX_OUTPUT_SEGMENT_BYTES for part in parts):
            raise _failure("invalid output path")
        normalized = tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)
        parsed.append((value, normalized, parts))

    # A declaration may not alias another declaration, including a file/dir
    # prefix collision.  Keep the original spelling for returned metadata.
    for index, (_, normalized, _) in enumerate(parsed):
        for _, other, _ in parsed[index + 1 :]:
            if (
                normalized == other
                or normalized[: len(other)] == other
                or other[: len(normalized)] == normalized
            ):
                raise _failure("ambiguous output paths")

    # Distinct spellings for a shared directory prefix are unsafe even when
    # their final files differ (for example, ``a/x`` and ``A/y``).
    prefixes: dict[tuple[str, ...], tuple[str, ...]] = {}
    for _, normalized, raw_parts in parsed:
        for end in range(1, len(normalized)):
            prefix = normalized[:end]
            raw_prefix = tuple(raw_parts[:end])
            previous = prefixes.setdefault(prefix, raw_prefix)
            if previous != raw_prefix:
                raise _failure("ambiguous output paths")
    return tuple(value for value, _, _ in parsed)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _validate_directory(info: os.stat_result, root_device: int) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != root_device:
        raise _failure("unsafe output directory")


def _validate_regular(info: os.stat_result, root_device: int) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != root_device:
        raise _failure("unsafe output file")


def _open_root(root: Path) -> tuple[int, os.stat_result]:
    if not isinstance(root, Path) or not root.is_absolute():
        raise _failure("invalid output root")
    try:
        path_info = os.lstat(root)
        if not stat.S_ISDIR(path_info.st_mode):
            raise _failure("invalid output root")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OutputCollectionError:
        raise
    except OSError as error:
        raise _failure("invalid output root") from error
    try:
        fd_info = os.fstat(root_fd)
        if not _same_identity(path_info, fd_info):
            raise _failure("changed output root")
        _validate_directory(fd_info, fd_info.st_dev)
        return root_fd, fd_info
    except BaseException:
        os.close(root_fd)
        raise


def _read_one(root_fd: int, parts: tuple[str, ...], remaining: int, root_device: int) -> bytes:
    directory_fds: list[int] = []
    current_fd = root_fd
    file_fd: int | None = None
    try:
        for component in parts[:-1]:
            next_fd: int | None = None
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
                info = os.fstat(next_fd)
            except OSError as error:
                if next_fd is not None:
                    os.close(next_fd)
                raise _failure("output path unavailable") from error
            try:
                _validate_directory(info, root_device)
            except BaseException:
                os.close(next_fd)
                raise
            directory_fds.append(next_fd)
            current_fd = next_fd

        try:
            preflight = os.stat(parts[-1], dir_fd=current_fd, follow_symlinks=False)
        except OSError as error:
            raise _failure("output file unavailable") from error
        _validate_regular(preflight, root_device)
        if preflight.st_size < 0 or preflight.st_size > remaining:
            raise _failure("output size exceeded")

        try:
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=current_fd,
            )
            before = os.fstat(file_fd)
        except OSError as error:
            raise _failure("output file unavailable") from error
        _validate_regular(before, root_device)
        if (
            not _same_identity(preflight, before)
            or preflight.st_dev != before.st_dev
            or preflight.st_mode != before.st_mode
            or preflight.st_size != before.st_size
        ):
            raise _failure("output file changed while opening")

        try:
            content = os.read(file_fd, before.st_size + 1)
        except OSError as error:
            raise _failure("output file unreadable") from error
        if len(content) != before.st_size:
            raise _failure("output file changed while reading")

        try:
            after = os.fstat(file_fd)
        except OSError as error:
            raise _failure("output file changed while reading") from error
        if (
            not _same_identity(before, after)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or before.st_nlink != after.st_nlink
            or before.st_mode != after.st_mode
        ):
            raise _failure("output file changed while reading")
        return content
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for fd in reversed(directory_fds):
            os.close(fd)


def collect_outputs(
    root: Path, output_paths: Sequence[str], max_output_bytes: int
) -> tuple[CollectedOutput, ...]:
    """Collect declared regular files under an anchored, trusted directory.

    The complete tuple is returned only after every declared file has passed all
    descriptor and metadata checks.  A failure never returns partial output.
    """

    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
        raise _failure("invalid output byte limit")
    if not 0 <= max_output_bytes <= MAX_OUTPUT_BYTES:
        raise _failure("invalid output byte limit")
    paths = _validate_paths(output_paths)
    root_fd, root_info = _open_root(root)
    outputs: list[CollectedOutput] = []
    consumed = 0
    try:
        for relative_path in paths:
            parts = tuple(relative_path.split("/"))
            content = _read_one(root_fd, parts, max_output_bytes - consumed, root_info.st_dev)
            consumed += len(content)
            outputs.append(
                CollectedOutput(
                    relative_path=relative_path,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        try:
            final_root = os.lstat(root)
        except OSError as error:
            raise _failure("changed output root") from error
        if not _same_identity(root_info, final_root):
            raise _failure("changed output root")
        return tuple(outputs)
    finally:
        os.close(root_fd)


__all__ = ["CollectedOutput", "OutputCollectionError", "collect_outputs"]
