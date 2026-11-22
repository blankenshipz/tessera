"""Operator bounds on a stage container's scratch and memory (ADR 0059).

The ``/work`` scratch is a memory-backed tmpfs, so its capacity is charged to the
container's memory limit. Both are operator settings, never a plan's. The defaults are
conservative (16 MiB scratch, a 64 MiB ceiling), the allowed maximum is large enough for a
real model fetch, and any setting above the historical bounds is checked against the host's
memory before the poller runs a grant, so a stage that cannot fit is refused at admission
instead of failing with ``ENOSPC`` or an out-of-memory kill mid-run.
"""

from __future__ import annotations

from pathlib import Path

MIB = 1024 * 1024
GIB = 1024 * MIB
MIN_SCRATCH_BYTES = MIB
DEFAULT_SCRATCH_BYTES = 16 * MIB
# The ceiling when the operator sets none: the historical maximum.
LEGACY_SCRATCH_CEILING = 64 * MIB
MIN_MEMORY_BYTES = 32 * MIB
LEGACY_MEMORY_CEILING = GIB
# The most an operator may configure for either; the host check bounds it further.
MAX_BYTES = 64 * GIB
# Memory a container needs beyond a full scratch tmpfs (its own processes).
MEMORY_HEADROOM_BYTES = 64 * MIB
# Fractions of host memory an operator setting may claim.
MAX_MEMORY_SHARE = 0.75
MAX_SCRATCH_SHARE = 0.5
MEMINFO = Path("/proc/meminfo")


def host_memory_bytes(meminfo: Path = MEMINFO) -> int | None:
    """Total host memory, or ``None`` where it cannot be read (the check is then skipped)."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def check_host_capacity(
    memory_bytes: int | None, max_scratch_bytes: int | None, *, meminfo: Path = MEMINFO
) -> None:
    """Refuse a setting the host cannot back; raises ValueError naming the setting.

    Only settings above the historical bounds are checked, so a default installation never
    depends on reading the host."""
    if (memory_bytes is None or memory_bytes <= LEGACY_MEMORY_CEILING) and (
        max_scratch_bytes is None or max_scratch_bytes <= LEGACY_SCRATCH_CEILING
    ):
        return
    total = host_memory_bytes(meminfo)
    if total is None:
        return
    if memory_bytes is not None and memory_bytes > total * MAX_MEMORY_SHARE:
        raise ValueError(
            f"memory_bytes {memory_bytes} exceeds {int(MAX_MEMORY_SHARE * 100)}% of host memory"
        )
    if max_scratch_bytes is not None and max_scratch_bytes > total * MAX_SCRATCH_SHARE:
        raise ValueError(
            f"max_scratch_bytes {max_scratch_bytes} exceeds "
            f"{int(MAX_SCRATCH_SHARE * 100)}% of host memory"
        )


def valid_bytes(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high
