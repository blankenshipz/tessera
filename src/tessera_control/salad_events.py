"""Reduce one Salad system-events (``instance_controller``) page to closed tokens.

Pure: no I/O. The provider's log text is untrusted and never leaves this function. What comes
out is a closed vocabulary only: event names from ``EVENT_NAMES``, exit codes (integers
0-255) and a count of distinct machines. No body text, instance or machine id, hostname or
timestamp is kept.
"""

from __future__ import annotations

import re
from typing import Any

EVENT_NAMES = (
    "instance_allocated",
    "image_pull",
    "instance_created",
    "instance_started",
    "instance_exited",
    "other",
)
# ``empty``: the query answered with zero entries. A wrong label name reads the same as a real
# empty log, so it is never reported as a successful ``read``.
OUTCOMES = frozenset({"read", "empty", "log_quota_exhausted", "unavailable"})
MAX_NAMES = len(EVENT_NAMES)
MAX_EXIT_CODES = 8
MAX_MACHINES = 100
# (lower-case needle, name): the first match wins, so the more specific wording comes first.
_NAMES = (
    ("instance exited", "instance_exited"),
    ("instance started", "instance_started"),
    ("instance created", "instance_created"),
    ("creating", "instance_created"),
    ("pulling", "image_pull"),
    ("downloading", "image_pull"),
    ("image", "image_pull"),
    ("allocat", "instance_allocated"),
    ("assigned", "instance_allocated"),
)
_EXIT = re.compile(r"Instance Exited:\s*(\d{1,3})")


def _texts(entry: dict[str, Any]) -> list[str]:
    found = []
    text = entry.get("text_log")
    if isinstance(text, str):
        found.append(text)
    body = entry.get("json_log")
    if isinstance(body, dict):
        found.extend(v for v in body.values() if isinstance(v, str))
    return found


def summarize_events(items: Any) -> dict[str, Any]:
    """Closed summary of one page of log entries (a list of entry objects)."""
    names: list[str] = []
    codes: set[int] = set()
    machines: set[str] = set()
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        labels = resource.get("labels") if isinstance(resource, dict) else None
        machine = labels.get("machine_id") if isinstance(labels, dict) else None
        if isinstance(machine, str) and machine:
            machines.add(machine)
        for text in _texts(entry):
            lowered = text.lower()
            name = next((n for needle, n in _NAMES if needle in lowered), "other")
            if name not in names:
                names.append(name)
            for match in _EXIT.finditer(text):
                code = int(match.group(1))
                if code <= 255:
                    codes.add(code)
    return {
        "events": sorted(names, key=EVENT_NAMES.index),
        "exit_codes": sorted(codes)[:MAX_EXIT_CODES],
        "machines": min(len(machines), MAX_MACHINES),
        "empty": not (isinstance(items, list) and items),
    }


def closed_record(value: Any) -> dict[str, Any] | None:
    """The stored receipt shape when ``value`` is well formed, else None (never echoes input)."""
    if not isinstance(value, dict) or value.get("outcome") not in OUTCOMES:
        return None
    names, codes, machines = value.get("events"), value.get("exit_codes"), value.get("machines")
    if (
        not isinstance(names, list)
        or len(names) > MAX_NAMES
        or any(n not in EVENT_NAMES for n in names)
        or not isinstance(codes, list)
        or len(codes) > MAX_EXIT_CODES
        or any(type(c) is not int or not 0 <= c <= 255 for c in codes)
        or type(machines) is not int
        or not 0 <= machines <= MAX_MACHINES
    ):
        return None
    return {
        "outcome": value["outcome"],
        "events": list(names),
        "exit_codes": list(codes),
        "machines": machines,
    }


def empty_record(outcome: str) -> dict[str, Any]:
    return {"outcome": outcome, "events": [], "exit_codes": [], "machines": 0}
