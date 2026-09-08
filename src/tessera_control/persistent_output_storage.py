"""Versioned, fail-closed contract for persistent CPU output storage.

This is an admission contract only. It does not provision, mount, collect from,
or release an output workspace.
"""

from __future__ import annotations

from typing import Any

from .store import Conflict

CONTRACT_VERSION = 1
_IDENTITY_FIELDS = ("allocation_id", "filesystem_id", "mount_id")
_REQUIRED_FIELDS = (
    "contract_version",
    *_IDENTITY_FIELDS,
    "capacity_bytes",
    "capacity_inodes",
    "noexec",
    "nosuid",
    "nodev",
    "write_uid",
    "write_access_verified",
    "supervisor_custody_verified",
    "no_nested_mounts_verified",
)


def validate_persistent_output_storage(value: Any) -> dict[str, Any]:
    """Return a canonical capability or raise a deterministic admission error."""
    if not isinstance(value, dict):
        raise Conflict("Persistent output storage capability must be an object.")
    for field in _REQUIRED_FIELDS:
        if field not in value:
            raise Conflict(f"Persistent output storage capability is missing {field}.")
    if any(not isinstance(field, str) for field in value):
        raise Conflict("Persistent output storage capability has an invalid property name.")
    extras = sorted(set(value) - set(_REQUIRED_FIELDS))
    if extras:
        raise Conflict(
            f"Persistent output storage capability has unsupported property {extras[0]}."
        )
    if value["contract_version"] != CONTRACT_VERSION or isinstance(value["contract_version"], bool):
        raise Conflict("Persistent output storage capability version is unsupported.")
    for field in _IDENTITY_FIELDS:
        item = value[field]
        if not isinstance(item, str) or not item.strip() or len(item) > 200:
            raise Conflict(f"Persistent output storage capability has invalid {field}.")
    for field in ("capacity_bytes", "capacity_inodes"):
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 2**63 - 1:
            raise Conflict(f"Persistent output storage capability has invalid {field}.")
    for field in ("noexec", "nosuid", "nodev"):
        if value[field] is not True:
            raise Conflict(f"Persistent output storage capability requires {field}.")
    if value["write_uid"] != 65532 or isinstance(value["write_uid"], bool):
        raise Conflict("Persistent output storage capability requires write_uid 65532.")
    for field in (
        "write_access_verified",
        "supervisor_custody_verified",
        "no_nested_mounts_verified",
    ):
        if value[field] is not True:
            raise Conflict(f"Persistent output storage capability requires {field}.")
    return {field: value[field] for field in _REQUIRED_FIELDS}
