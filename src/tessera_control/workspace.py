"""Bounded, read-only local workspace inventory and skill snapshots.

This module deliberately has no execution, network, or upload behavior. Inputs
are treated as untrusted local data and snapshots are immutable descriptions of
what was read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_NODES = 10_000
DEFAULT_MAX_EXCLUSIONS = 10_000
CAPTURE_MAX_FILES = 1_000
CAPTURE_MAX_FILE_BYTES = 16 * 1024 * 1024
CAPTURE_MAX_TOTAL_BYTES = 16 * 1024 * 1024
SKILL_MAX_BYTES = 1024 * 1024
_IGNORED_DIRS = {".git", ".venv", ".tessera", "node_modules"}
_CREDENTIAL_NAMES = {"owner.header", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}


class WorkspaceError(ValueError):
    """A selected workspace cannot be safely inventoried or snapshotted."""


@dataclass(frozen=True)
class InventoryEntry:
    relative_path: str
    size: int
    sha256: str
    provenance: str = "imported_unverified"
    classification: str = "unclassified"


@dataclass(frozen=True)
class InventoryExclusion:
    relative_path: str
    reason: str


@dataclass(frozen=True)
class WorkspaceInventory:
    root: str
    entries: tuple[InventoryEntry, ...]
    exclusions: tuple[InventoryExclusion, ...]
    total_bytes: int


@dataclass(frozen=True)
class CapturedWorkspace:
    inventory: WorkspaceInventory
    files: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class SkillSnapshot:
    name: str
    description: str
    digest: str
    manifest: tuple[tuple[str, str, int], ...]
    provenance: str = "imported_unverified"
    classification: str = "unclassified"


def _credential_reason(name: str) -> str | None:
    if name == "owner.header" or name in _CREDENTIAL_NAMES:
        return "credential_path"
    if name.startswith(".env"):
        return "credential_path"
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return "credential_path"
    return None


def _safe_open_flags(*, directory: bool = False, nonblocking: bool = False) -> int:
    """Return required no-follow flags, failing closed where they are unavailable."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(no_follow, int) or no_follow == 0:
        raise WorkspaceError("safe filesystem traversal requires O_NOFOLLOW support")
    flags = os.O_RDONLY | no_follow
    if directory:
        open_directory = getattr(os, "O_DIRECTORY", None)
        if not isinstance(open_directory, int) or open_directory == 0:
            raise WorkspaceError("safe filesystem traversal requires O_DIRECTORY support")
        flags |= open_directory
    if nonblocking:
        nonblocking_flag = getattr(os, "O_NONBLOCK", None)
        if not isinstance(nonblocking_flag, int) or nonblocking_flag == 0:
            raise WorkspaceError("safe regular-file reads require O_NONBLOCK support")
        flags |= nonblocking_flag
    return flags


def _selected_path(root: str | os.PathLike[str], *, label: str) -> Path:
    selected = os.path.expanduser(os.fspath(root))
    if not os.path.isabs(selected):
        raise WorkspaceError(f"{label} root must be an explicit absolute path")
    if any(component in {".", ".."} for component in selected.split(os.sep)):
        raise WorkspaceError(f"{label} root contains an unsafe path component")
    return Path(os.path.abspath(selected))


def _open_root_directory(selected: Path, *, label: str) -> int:
    """Open every absolute component without following a symlinked ancestor."""
    flags = _safe_open_flags(directory=True)
    try:
        directory_fd = os.open(os.sep, flags)
    except OSError as exc:
        raise WorkspaceError(f"{label} root is unavailable: {exc.strerror}") from exc
    try:
        for component in selected.parts[1:]:
            if component in {"", ".", ".."}:
                raise WorkspaceError(f"{label} root contains an unsafe path component")
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise WorkspaceError(
                    f"{label} root must be a real directory: {exc.strerror}"
                ) from exc
            os.close(directory_fd)
            directory_fd = child_fd
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise WorkspaceError(f"{label} root must be a real directory")
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _directory_identity(directory_fd: int) -> tuple[int, int, int, int]:
    info = os.fstat(directory_fd)
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _directory_names(
    directory_fd: int, relative_path: str, remaining_nodes: int, limit_message: str
) -> tuple[tuple[int, int, int, int], list[str]]:
    before = _directory_identity(directory_fd)
    try:
        with os.scandir(directory_fd) as scanned:
            names = []
            for item in scanned:
                names.append(item.name)
                if len(names) > remaining_nodes:
                    raise WorkspaceError(limit_message)
    except OSError as exc:
        raise WorkspaceError(f"cannot read directory {relative_path}: {exc.strerror}") from exc
    return before, sorted(names)


def _check_directory_unchanged(
    directory_fd: int, before: tuple[int, int, int, int], relative_path: str
) -> None:
    if _directory_identity(directory_fd) != before:
        raise WorkspaceError(f"directory changed during snapshot: {relative_path}")


def _relative(parent: str, name: str) -> str:
    return name if parent == "." else f"{parent}/{name}"


def _read_open_file(fd: int, limit: int, relative_path: str) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise WorkspaceError(f"nonregular file: {relative_path}")
    result = bytearray()
    while True:
        chunk = os.read(fd, min(1024 * 1024, limit - len(result) + 1))
        if not chunk:
            break
        result.extend(chunk)
        if len(result) > limit:
            raise WorkspaceError(f"file exceeds byte limit: {relative_path}")
    after = os.fstat(fd)
    if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_dev,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise WorkspaceError(f"file changed during read: {relative_path}")
    return bytes(result)


def _read_bytes_at(directory_fd: int, name: str, limit: int, relative_path: str) -> bytes:
    try:
        fd = os.open(name, _safe_open_flags(nonblocking=True), dir_fd=directory_fd)
    except OSError as exc:
        raise WorkspaceError(f"cannot safely read {relative_path}: {exc.strerror}") from exc
    try:
        return _read_open_file(fd, limit, relative_path)
    finally:
        os.close(fd)


def _open_child_directory(directory_fd: int, name: str, relative_path: str) -> int:
    try:
        child_fd = os.open(name, _safe_open_flags(directory=True), dir_fd=directory_fd)
    except OSError as exc:
        raise WorkspaceError(
            f"cannot safely read directory {relative_path}: {exc.strerror}"
        ) from exc
    if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
        os.close(child_fd)
        raise WorkspaceError(f"nonregular file: {relative_path}")
    return child_fd


def _walk_workspace(
    root: str | os.PathLike[str],
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
    max_nodes: int,
    max_exclusions: int,
    include_files: bool,
) -> CapturedWorkspace:
    if min(max_files, max_file_bytes, max_total_bytes, max_nodes, max_exclusions) < 0:
        raise WorkspaceError("bounds must be nonnegative")
    selected = _selected_path(root, label="workspace")
    root_fd = _open_root_directory(selected, label="workspace")
    entries: list[InventoryEntry] = []
    exclusions: list[InventoryExclusion] = []
    files: list[tuple[str, bytes]] = []
    total = 0
    nodes = 0
    pending = [(".", root_fd)]

    def exclude(relative_path: str, reason: str) -> None:
        if len(exclusions) >= max_exclusions:
            raise WorkspaceError("workspace exclusion limit exceeded")
        exclusions.append(InventoryExclusion(relative_path, reason))

    try:
        while pending:
            parent, directory_fd = pending.pop()
            try:
                before, names = _directory_names(
                    directory_fd,
                    parent,
                    max_nodes - nodes,
                    "workspace node limit exceeded",
                )
                for name in names:
                    if nodes >= max_nodes:
                        raise WorkspaceError("workspace node limit exceeded")
                    nodes += 1
                    relative_path = _relative(parent, name)
                    credential = _credential_reason(name)
                    if credential:
                        exclude(relative_path, credential)
                        continue
                    try:
                        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise WorkspaceError(
                            f"cannot inspect {relative_path}: {exc.strerror}"
                        ) from exc
                    if stat.S_ISLNK(info.st_mode):
                        exclude(relative_path, "symlink")
                    elif stat.S_ISDIR(info.st_mode):
                        if name in _IGNORED_DIRS:
                            exclude(relative_path, "ignored_directory")
                        else:
                            pending.append(
                                (
                                    relative_path,
                                    _open_child_directory(directory_fd, name, relative_path),
                                )
                            )
                    elif not stat.S_ISREG(info.st_mode):
                        exclude(relative_path, "nonregular_file")
                    else:
                        if len(entries) >= max_files:
                            raise WorkspaceError("workspace file count limit exceeded")
                        payload = _read_bytes_at(directory_fd, name, max_file_bytes, relative_path)
                        if total + len(payload) > max_total_bytes:
                            raise WorkspaceError("workspace total byte limit exceeded")
                        total += len(payload)
                        entries.append(
                            InventoryEntry(
                                relative_path, len(payload), hashlib.sha256(payload).hexdigest()
                            )
                        )
                        if include_files:
                            files.append((relative_path, payload))
                _check_directory_unchanged(directory_fd, before, parent)
            finally:
                os.close(directory_fd)
        entries.sort(key=lambda entry: entry.relative_path)
        exclusions.sort(key=lambda exclusion: exclusion.relative_path)
        files.sort(key=lambda item: item[0])
        return CapturedWorkspace(
            WorkspaceInventory(str(selected), tuple(entries), tuple(exclusions), total),
            tuple(files),
        )
    finally:
        for _, directory_fd in pending:
            os.close(directory_fd)


def inventory_workspace(
    root: str | os.PathLike[str],
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_exclusions: int = DEFAULT_MAX_EXCLUSIONS,
) -> WorkspaceInventory:
    """Inventory regular files under an explicit root, without following links."""
    return _walk_workspace(
        root,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_nodes=max_nodes,
        max_exclusions=max_exclusions,
        include_files=False,
    ).inventory


def capture_workspace(
    root: str | os.PathLike[str],
    *,
    max_files: int = CAPTURE_MAX_FILES,
    max_file_bytes: int = CAPTURE_MAX_FILE_BYTES,
    max_total_bytes: int = CAPTURE_MAX_TOTAL_BYTES,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_exclusions: int = DEFAULT_MAX_EXCLUSIONS,
) -> CapturedWorkspace:
    """Read and retain an immutable, descriptor-safe workspace input snapshot."""
    return _walk_workspace(
        root,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_nodes=max_nodes,
        max_exclusions=max_exclusions,
        include_files=True,
    )


def inventory_digest(inventory: WorkspaceInventory) -> str:
    """Hash canonical inventory metadata without incorporating captured file bytes."""
    payload = json.dumps(asdict(inventory), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_FRONTMATTER = re.compile(r"\A---\s*\n(?P<header>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_QUALIFIED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)+$")


def _skill_metadata(payload: bytes) -> tuple[str, str]:
    try:
        content = payload.decode("utf-8")
    except UnicodeError as exc:
        raise WorkspaceError(f"SKILL.md is not valid UTF-8: {exc}") from exc
    match = _FRONTMATTER.match(content)
    if not match:
        raise WorkspaceError("SKILL.md requires YAML frontmatter")
    try:
        metadata: Any = yaml.safe_load(match.group("header"))
    except (yaml.YAMLError, RecursionError):
        raise WorkspaceError("invalid SKILL.md YAML") from None
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not isinstance(metadata.get("description"), str)
    ):
        raise WorkspaceError("SKILL.md frontmatter needs string name and description")
    name, description = metadata["name"].strip(), metadata["description"].strip()
    if not name or not description:
        raise WorkspaceError("SKILL.md name and description must be nonempty")
    return name, description


def snapshot_skill(
    root: str | os.PathLike[str],
    *,
    qualified_name: str | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> SkillSnapshot:
    """Validate an instruction-only skill and hash its deterministic file manifest."""
    if min(max_files, max_file_bytes, max_total_bytes, max_nodes) < 0:
        raise WorkspaceError("bounds must be nonnegative")
    selected = _selected_path(root, label="skill")
    root_fd = _open_root_directory(selected, label="skill")
    if qualified_name is not None and not _QUALIFIED.fullmatch(qualified_name):
        os.close(root_fd)
        raise WorkspaceError("qualified skill name must contain a namespace and name")
    manifest: list[tuple[str, str, int]] = []
    metadata: tuple[str, str] | None = None
    total = 0
    nodes = 0
    pending = [(".", root_fd)]
    try:
        while pending:
            parent, directory_fd = pending.pop()
            try:
                before, names = _directory_names(
                    directory_fd,
                    parent,
                    max_nodes - nodes,
                    "skill node limit exceeded",
                )
                for name in names:
                    if nodes >= max_nodes:
                        raise WorkspaceError("skill node limit exceeded")
                    nodes += 1
                    relative_path = _relative(parent, name)
                    if _credential_reason(name):
                        raise WorkspaceError(f"skill contains credential path: {relative_path}")
                    try:
                        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise WorkspaceError(
                            f"cannot inspect {relative_path}: {exc.strerror}"
                        ) from exc
                    if stat.S_ISLNK(info.st_mode):
                        raise WorkspaceError(f"skill contains symlink: {relative_path}")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(
                            (
                                relative_path,
                                _open_child_directory(directory_fd, name, relative_path),
                            )
                        )
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise WorkspaceError(f"skill contains nonregular file: {relative_path}")
                    if len(manifest) >= max_files:
                        raise WorkspaceError("skill file count limit exceeded")
                    limit = (
                        min(max_file_bytes, SKILL_MAX_BYTES)
                        if relative_path == "SKILL.md"
                        else max_file_bytes
                    )
                    payload = _read_bytes_at(directory_fd, name, limit, relative_path)
                    if total + len(payload) > max_total_bytes:
                        raise WorkspaceError("skill total byte limit exceeded")
                    total += len(payload)
                    digest = hashlib.sha256(payload).hexdigest()
                    manifest.append((relative_path, digest, len(payload)))
                    if relative_path == "SKILL.md":
                        metadata = _skill_metadata(payload)
                _check_directory_unchanged(directory_fd, before, parent)
            finally:
                os.close(directory_fd)
        if metadata is None:
            raise WorkspaceError("skill requires a regular SKILL.md")
        manifest.sort(key=lambda item: item[0])
        encoded = "".join(f"{path}\0{digest}\0{size}\n" for path, digest, size in manifest).encode()
        return SkillSnapshot(
            qualified_name or metadata[0],
            metadata[1],
            hashlib.sha256(encoded).hexdigest(),
            tuple(manifest),
        )
    finally:
        for _, directory_fd in pending:
            os.close(directory_fd)


validate_skill = snapshot_skill
