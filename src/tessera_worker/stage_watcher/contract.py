"""The observe-only stage contract (ADR 0087 section 2, slice S2 subset).

The contract carries identity, bounds and the kill-switch reference only. It has no credential,
URL, destination or free-text field: anything not in the contract is not allowed.

The optional decision envelope (``retry_attempts_remaining``, ``transient_exit_codes``,
``memory_bytes_current``, ``memory_bytes_max``) bounds what the watcher may REQUEST (see
``decisions.py``); it is covered by the digest only when set, so existing digests are unchanged.
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
MAX_RETRY_ATTEMPTS = 16
MAX_TRANSIENT_EXIT_CODES = 8
# Control caps requested_memory_bytes at 1 TiB; the envelope uses the same bound.
MAX_BYTES = 2**40
ENVELOPE_FIELDS: tuple[str, ...] = (
    "retry_attempts_remaining",
    "transient_exit_codes",
    "memory_bytes_current",
    "memory_bytes_max",
)


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
    # Optional decision envelope (``decisions.py``): what the approved stage allows the watcher
    # to ASK for. All None (the default) means no request is ever filed. A None field is left
    # out of the canonical JSON, so a contract without an envelope keeps its digest.
    retry_attempts_remaining: int | None = None
    transient_exit_codes: tuple[int, ...] | None = None
    memory_bytes_current: int | None = None
    memory_bytes_max: int | None = None

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
        self._check_envelope()

    def _check_envelope(self) -> None:
        attempts = self.retry_attempts_remaining
        if attempts is not None and (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 0 <= attempts <= MAX_RETRY_ATTEMPTS
        ):
            raise ValueError(f"retry_attempts_remaining must be None or 0..{MAX_RETRY_ATTEMPTS}.")
        codes = self.transient_exit_codes
        if codes is not None:
            if not isinstance(codes, (tuple, list)) or len(codes) > MAX_TRANSIENT_EXIT_CODES:
                raise ValueError(
                    f"transient_exit_codes must be None or <= {MAX_TRANSIENT_EXIT_CODES} codes."
                )
            for code in codes:
                if isinstance(code, bool) or not isinstance(code, int) or not 1 <= code <= 255:
                    raise ValueError("a transient exit code must be an integer in 1..255.")
            if len(set(codes)) != len(codes):
                raise ValueError("transient_exit_codes must not repeat a code.")
            # Canonical order, so the same set always has the same digest.
            object.__setattr__(self, "transient_exit_codes", tuple(sorted(codes)))
        for name in ("memory_bytes_current", "memory_bytes_max"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_BYTES
            ):
                raise ValueError(f"{name} must be None or a positive integer.")
        if (
            self.memory_bytes_current is not None
            and self.memory_bytes_max is not None
            and self.memory_bytes_current > self.memory_bytes_max
        ):
            raise ValueError("memory_bytes_current must not exceed memory_bytes_max.")

    @property
    def has_envelope(self) -> bool:
        return any(getattr(self, name) is not None for name in ENVELOPE_FIELDS)

    def canonical_json(self) -> str:
        fields = {
            key: value
            for key, value in asdict(self).items()
            if not (key in ENVELOPE_FIELDS and value is None)
        }
        return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def digest(self) -> str:
        """sha256 hex of the canonical JSON (sorted keys, compact) of every field (envelope
        fields only when set)."""
        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()

    def verify(self, expected_digest: str) -> bool:
        """True only if expected_digest is a well-formed digest equal to this contract's."""
        if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
            return False
        return hmac.compare_digest(self.digest(), expected_digest)
