"""The observe-only stage contract (ADR 0087 section 2, slice S2 subset).

The contract carries identity, bounds and the kill-switch reference only. It has no credential,
URL, destination or free-text field: anything not in the contract is not allowed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import asdict, dataclass

from tessera_control.stage_watch_limits import MAX_REPORTS_PER_OPERATION

_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")

MAX_DEADLINE_SECONDS = 7 * 24 * 3600
# The same setting control enforces per operation, so a watch never plans more reports than
# control accepts.
MAX_REPORTS_LIMIT = MAX_REPORTS_PER_OPERATION
MAX_EPOCH = 2**63 - 1
_MAX_PATH_BYTES = 4096


def _positive_int(name: str, value: object, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError(f"{name} must be an integer in 1..{maximum}.")


@dataclass(frozen=True)
class StageContract:
    operation_id: str
    lease_id: str
    epoch: int
    stage_key: str
    provider: str
    deadline_seconds: int
    silent_seconds: int
    heartbeat_seconds: int
    max_reports: int
    kill_switch_path: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not _OPERATION_ID.fullmatch(self.operation_id):
            raise ValueError("operation_id must be 32 lowercase hex characters.")
        for name in ("lease_id", "stage_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _TOKEN.fullmatch(value):
                raise ValueError(f"{name} must be a bounded identifier token.")
        if not isinstance(self.provider, str) or not _PROVIDER.fullmatch(self.provider):
            raise ValueError("provider must be a bounded lowercase identifier.")
        if (
            isinstance(self.epoch, bool)
            or not isinstance(self.epoch, int)
            or not 0 <= self.epoch <= MAX_EPOCH
        ):
            raise ValueError("epoch must be a non-negative integer.")
        _positive_int("deadline_seconds", self.deadline_seconds, MAX_DEADLINE_SECONDS)
        _positive_int("silent_seconds", self.silent_seconds, self.deadline_seconds)
        _positive_int("heartbeat_seconds", self.heartbeat_seconds, self.deadline_seconds)
        _positive_int("max_reports", self.max_reports, MAX_REPORTS_LIMIT)
        path = self.kill_switch_path
        if path is not None and (
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or len(path.encode("utf-8", "surrogateescape")) > _MAX_PATH_BYTES
            or not os.path.isabs(path)
        ):
            raise ValueError("kill_switch_path must be None or a bounded absolute path.")

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def digest(self) -> str:
        """sha256 hex of the canonical JSON (sorted keys, compact) of every field."""
        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()

    def verify(self, expected_digest: str) -> bool:
        """True only if expected_digest is a well-formed digest equal to this contract's."""
        if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
            return False
        return hmac.compare_digest(self.digest(), expected_digest)
