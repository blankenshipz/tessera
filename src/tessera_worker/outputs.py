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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tessera_control import artifact_limits as limits

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


def validate_output_paths(output_paths: Sequence[str]) -> tuple[str, ...]:
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


@contextmanager
def _open_output(
    root_fd: int, parts: tuple[str, ...], remaining: int, root_device: int
) -> Iterator[tuple[int, os.stat_result]]:
    """Open one declared file anchored at ``root_fd`` with every safety check.

    Yields the descriptor and its pre-read ``fstat``; closes every descriptor on exit.
    """
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
        yield file_fd, before
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for fd in reversed(directory_fds):
            os.close(fd)


def _assert_unchanged(file_fd: int, before: os.stat_result) -> None:
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


def _read_one(root_fd: int, parts: tuple[str, ...], remaining: int, root_device: int) -> bytes:
    with _open_output(root_fd, parts, remaining, root_device) as (file_fd, before):
        try:
            content = os.read(file_fd, before.st_size + 1)
        except OSError as error:
            raise _failure("output file unreadable") from error
        if len(content) != before.st_size:
            raise _failure("output file changed while reading")
        _assert_unchanged(file_fd, before)
        return content


def _stream_digest(file_fd: int, size: int) -> str:
    """sha256 of exactly ``size`` bytes, reading at most one chunk at a time."""
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(file_fd, min(limits.CHUNK_BYTES, size - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > size:
                raise _failure("output file changed while reading")
            digest.update(chunk)
    except OSError as error:
        raise _failure("output file unreadable") from error
    if total != size:
        raise _failure("output file changed while reading")
    return digest.hexdigest()


@dataclass(frozen=True)
class LargeOutput:
    """A reference to a verified output file larger than ``LEGACY_INLINE_BYTES``.

    The bytes are never held. ``open`` re-opens the file under its anchored root, refuses
    a changed identity, and the handle re-hashes the whole file before upload.
    """

    relative_path: str
    sha256: str
    size: int
    root: Path
    dev: int
    ino: int
    mtime_ns: int

    @contextmanager
    def open(self) -> Iterator[LargeOutputHandle]:
        root_fd, root_info = _open_root(self.root)
        try:
            with _open_output(
                root_fd, tuple(self.relative_path.split("/")), self.size, root_info.st_dev
            ) as (file_fd, before):
                if (
                    before.st_dev != self.dev
                    or before.st_ino != self.ino
                    or before.st_size != self.size
                    or before.st_mtime_ns != self.mtime_ns
                ):
                    raise _failure("output file changed after sealing")
                handle = LargeOutputHandle(self, file_fd, before)
                yield handle
                handle.assert_unchanged()
        finally:
            os.close(root_fd)


class LargeOutputHandle:
    """An open, identity-verified descriptor for one :class:`LargeOutput`."""

    def __init__(self, output: LargeOutput, file_fd: int, before: os.stat_result) -> None:
        self.output = output
        self._fd = file_fd
        self._before = before

    def verify_hash(self) -> None:
        """Re-hash the whole file in chunks; refuse if it differs from the reference."""
        os.lseek(self._fd, 0, os.SEEK_SET)
        if _stream_digest(self._fd, self.output.size) != self.output.sha256:
            raise _failure("output file changed after sealing")
        self.assert_unchanged()

    def read_chunk(self, offset: int, length: int) -> bytes:
        if not 0 <= offset < self.output.size or not 0 < length <= limits.CHUNK_BYTES:
            raise _failure("invalid output chunk request")
        length = min(length, self.output.size - offset)
        try:
            data = os.pread(self._fd, length, offset)
        except OSError as error:
            raise _failure("output file unreadable") from error
        if len(data) != length:
            raise _failure("output file changed while reading")
        return data

    def assert_unchanged(self) -> None:
        _assert_unchanged(self._fd, self._before)


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
    paths = validate_output_paths(output_paths)
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


def worker_output_limit(worker_max_output_bytes: int | None = None) -> int:
    """The operator's per-worker output ceiling (explicit value or the installation default)."""
    if worker_max_output_bytes is None:
        try:
            return limits.operator_limits().max_artifact_bytes
        except limits.ArtifactLimitError as error:
            raise _failure("invalid artifact limit configuration") from error
    if (
        type(worker_max_output_bytes) is not int
        or not limits.LEGACY_INLINE_BYTES
        <= worker_max_output_bytes
        <= limits.ABSOLUTE_MAX_ARTIFACT_BYTES
    ):
        raise _failure("invalid worker output limit")
    return worker_max_output_bytes


def collect_output_set(
    root: Path,
    output_paths: Sequence[str],
    max_output_bytes: int,
    worker_max_output_bytes: int | None = None,
) -> tuple[CollectedOutput | LargeOutput, ...]:
    """Collect declared files; large ones are verified and hashed by streaming.

    Files up to ``LEGACY_INLINE_BYTES`` come back as in-memory :class:`CollectedOutput`
    exactly as ``collect_outputs`` returns them (their combined size stays within that
    bound). Larger files return a :class:`LargeOutput` reference. A plan limit above the
    worker's configured maximum is refused, never truncated.
    """

    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
        raise _failure("invalid output byte limit")
    ceiling = worker_output_limit(worker_max_output_bytes)
    if max_output_bytes < 0:
        raise _failure("invalid output byte limit")
    if max_output_bytes > ceiling:
        raise _failure("planned output limit exceeds this worker's configured maximum")
    paths = validate_output_paths(output_paths)
    root_fd, root_info = _open_root(root)
    outputs: list[CollectedOutput | LargeOutput] = []
    consumed = 0
    inline = 0
    try:
        for relative_path in paths:
            parts = tuple(relative_path.split("/"))
            with _open_output(root_fd, parts, max_output_bytes - consumed, root_info.st_dev) as (
                file_fd,
                before,
            ):
                if before.st_size <= limits.LEGACY_INLINE_BYTES:
                    inline += before.st_size
                    if inline > limits.LEGACY_INLINE_BYTES:
                        raise _failure("inline output files exceed their combined size bound")
                    try:
                        content = os.read(file_fd, before.st_size + 1)
                    except OSError as error:
                        raise _failure("output file unreadable") from error
                    if len(content) != before.st_size:
                        raise _failure("output file changed while reading")
                    _assert_unchanged(file_fd, before)
                    consumed += len(content)
                    outputs.append(
                        CollectedOutput(
                            relative_path=relative_path,
                            content=content,
                            sha256=hashlib.sha256(content).hexdigest(),
                        )
                    )
                else:
                    digest = _stream_digest(file_fd, before.st_size)
                    _assert_unchanged(file_fd, before)
                    consumed += before.st_size
                    outputs.append(
                        LargeOutput(
                            relative_path=relative_path,
                            sha256=digest,
                            size=before.st_size,
                            root=root,
                            dev=before.st_dev,
                            ino=before.st_ino,
                            mtime_ns=before.st_mtime_ns,
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


__all__ = [
    "CollectedOutput",
    "LargeOutput",
    "LargeOutputHandle",
    "OutputCollectionError",
    "collect_output_set",
    "collect_outputs",
    "validate_output_paths",
    "worker_output_limit",
]
