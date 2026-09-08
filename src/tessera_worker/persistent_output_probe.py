"""Read-only inspection for an operator-provisioned persistent output slot.

The probe deliberately supports only a dedicated Linux mount. It reports the
existing versioned admission contract or ``None``; it never provisions storage,
changes ownership or permissions, mounts anything, or invokes Docker/helpers.
Host observations are not Docker exhaustion/restart evidence and do not activate
collection or dispatch.
"""

from __future__ import annotations

import os
import platform
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tessera_control.persistent_output_storage import validate_persistent_output_storage

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_MAX_MOUNTINFO_BYTES = 4 * 1024 * 1024
_REQUIRED_FLAGS = frozenset({"noexec", "nosuid", "nodev"})
_OUTPUT_UID = 65532


@dataclass(frozen=True)
class PathFacts:
    """A pair of non-following directory observations for pure validation."""

    is_directory: bool
    is_symlink: bool
    mode: int
    uid: int
    gid: int
    device: int
    inode: int


@dataclass(frozen=True)
class MountFacts:
    """Identity and mount flags observed from Linux ``mountinfo``."""

    mount_id: str
    filesystem_id: str
    mountpoint: str
    device: int
    flags: frozenset[str]


@dataclass(frozen=True)
class CapacityFacts:
    """Fixed capacity of the dedicated mounted slot, not free space."""

    bytes: int
    inodes: int


@dataclass(frozen=True)
class PersistentOutputProbeFacts:
    """Read-only observations consumed by the portable fail-closed validator."""

    platform_name: str
    requested_path: str
    canonical_path: str | None
    initial_path: PathFacts | None
    final_path: PathFacts | None
    mount: MountFacts | None
    nested_mounts: bool | None
    capacity: CapacityFacts | None
    uid_65532_write_access: bool | None
    supervisor_uid: int | None


def _valid_identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def _same_path(first: PathFacts, second: PathFacts) -> bool:
    return (
        first.is_directory == second.is_directory
        and first.is_symlink == second.is_symlink
        and first.mode == second.mode
        and first.uid == second.uid
        and first.gid == second.gid
        and first.device == second.device
        and first.inode == second.inode
    )


def _valid_capacity(value: object) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


def persistent_output_storage_contract(
    facts: PersistentOutputProbeFacts,
) -> dict[str, Any] | None:
    """Return the canonical existing contract only for complete safe observations.

    The pure validator keeps synthetic tests portable. Every missing, malformed,
    changed, or unsupported observation yields ``None`` rather than a partial
    declaration.
    """
    if facts.platform_name != "Linux":
        return None
    initial, final, mount, capacity = (
        facts.initial_path,
        facts.final_path,
        facts.mount,
        facts.capacity,
    )
    if (
        initial is None
        or final is None
        or mount is None
        or capacity is None
        or not isinstance(facts.requested_path, str)
        or facts.canonical_path != facts.requested_path
        or not _same_path(initial, final)
    ):
        return None
    if (
        not initial.is_directory
        or initial.is_symlink
        or stat.S_IMODE(initial.mode) != 0o700
        or initial.uid != _OUTPUT_UID
        or initial.gid != _OUTPUT_UID
        or initial.device < 0
        or initial.inode <= 0
    ):
        return None
    if facts.supervisor_uid != 0 or facts.uid_65532_write_access is not True:
        return None
    if (
        mount.mountpoint != facts.canonical_path
        or mount.device != initial.device
        or not _valid_identifier(mount.mount_id)
        or not _valid_identifier(mount.filesystem_id)
        or not _REQUIRED_FLAGS.issubset(mount.flags)
        or facts.nested_mounts is not False
    ):
        return None
    if not _valid_capacity(capacity.bytes) or not _valid_capacity(capacity.inodes):
        return None
    allocation_id = f"linux-allocation-{initial.device:x}-{initial.inode:x}"
    try:
        return validate_persistent_output_storage(
            {
                "contract_version": 1,
                "allocation_id": allocation_id,
                "filesystem_id": mount.filesystem_id,
                "mount_id": mount.mount_id,
                "capacity_bytes": capacity.bytes,
                "capacity_inodes": capacity.inodes,
                "noexec": True,
                "nosuid": True,
                "nodev": True,
                "write_uid": _OUTPUT_UID,
                "write_access_verified": True,
                "supervisor_custody_verified": True,
                "no_nested_mounts_verified": True,
            }
        )
    except Exception:
        return None


def _path_facts(path: Path) -> PathFacts | None:
    try:
        metadata = path.lstat()
    except (OSError, ValueError):
        return None
    return PathFacts(
        is_directory=stat.S_ISDIR(metadata.st_mode),
        is_symlink=stat.S_ISLNK(metadata.st_mode),
        mode=metadata.st_mode,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _unescape_mount_path(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _mounts_for(path: str) -> tuple[MountFacts | None, bool | None]:
    """Read Linux mountinfo without following or modifying the allocation path."""
    try:
        with open("/proc/self/mountinfo", "rb") as source:
            content = source.read(_MAX_MOUNTINFO_BYTES + 1)
        if len(content) > _MAX_MOUNTINFO_BYTES:
            return None, None
        lines = content.decode("utf-8", "strict").splitlines()
    except (OSError, UnicodeDecodeError):
        return None, None
    matches: list[MountFacts] = []
    nested = False
    prefix = path if path.endswith("/") else f"{path}/"
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_id, device, mountpoint, options = fields[0], fields[2], fields[4], fields[5]
        except (IndexError, ValueError):
            return None, None
        decoded = _unescape_mount_path(mountpoint)
        if decoded.startswith(prefix):
            nested = True
        if decoded != path:
            continue
        try:
            major, minor = device.split(":", 1)
            if separator + 3 >= len(fields) or not major.isdecimal() or not minor.isdecimal():
                return None, None
            mount_number = int(mount_id)
            if mount_number < 1:
                return None, None
            filesystem = fields[separator + 1]
            source = fields[separator + 2]
        except (IndexError, ValueError):
            return None, None
        filesystem_id = f"linux-fs-{major}:{minor}-{filesystem}-{source}".replace("/", "_")
        mount_identifier = f"linux-mount-{mount_number}"
        if not _valid_identifier(filesystem_id):
            return None, None
        matches.append(
            MountFacts(
                mount_id=mount_identifier,
                filesystem_id=filesystem_id,
                mountpoint=path,
                device=os.makedev(int(major), int(minor)),
                flags=frozenset(options.split(",")),
            )
        )
    if len(matches) != 1:
        return None, None
    return matches[0], nested


def _capacity(path: Path) -> CapacityFacts | None:
    try:
        data = os.statvfs(path)
        if type(data.f_frsize) is not int or type(data.f_blocks) is not int:
            return None
        return CapacityFacts(bytes=data.f_frsize * data.f_blocks, inodes=data.f_files)
    except (OSError, ValueError):
        return None


def inspect_persistent_output_storage(
    path: Path, *, platform_name: str | None = None
) -> dict[str, Any] | None:
    """Inspect an existing dedicated Linux mount without making filesystem changes."""
    observed_platform = platform.system() if platform_name is None else platform_name
    # Docker Desktop's macOS host observations do not establish Linux daemon mount
    # semantics. Other non-Linux platforms are unsupported for this backend too.
    if observed_platform != "Linux" or not isinstance(path, Path) or not path.is_absolute():
        return None
    requested = Path(os.path.abspath(os.fspath(path)))
    initial = _path_facts(requested)
    canonical: str | None = None
    if initial is not None:
        try:
            resolved = requested.resolve(strict=True)
            if resolved == requested:
                canonical = str(resolved)
        except (OSError, ValueError):
            pass
    mount, nested = _mounts_for(canonical) if canonical is not None else (None, None)
    capacity = _capacity(requested) if canonical is not None else None
    final = _path_facts(requested)
    write_access = bool(
        final is not None and final.uid == _OUTPUT_UID and stat.S_IMODE(final.mode) & stat.S_IWUSR
    )
    return persistent_output_storage_contract(
        PersistentOutputProbeFacts(
            platform_name=observed_platform,
            requested_path=str(requested),
            canonical_path=canonical,
            initial_path=initial,
            final_path=final,
            mount=mount,
            nested_mounts=nested,
            capacity=capacity,
            uid_65532_write_access=write_access,
            supervisor_uid=os.geteuid(),
        )
    )


__all__ = [
    "CapacityFacts",
    "MountFacts",
    "PathFacts",
    "PersistentOutputProbeFacts",
    "inspect_persistent_output_storage",
    "persistent_output_storage_contract",
]
