"""Bounded, synthetic worker capability observations.

This module only evaluates caller-supplied facts.  It does not enroll a worker,
write control state, or establish build trust.  In particular, a successful
report is informational and cannot authorize dispatch.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tessera_control.persistent_output_storage import validate_persistent_output_storage

MAX_CHECKS = 8
MAX_DETAIL_BYTES = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_SECRET = re.compile(r"(?:secret|token|credential|authorization|password|api[_-]?key)", re.I)
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}\Z")
_FACT_FIELDS = frozenset(
    {
        "runtime_profile_version",
        "expected_runtime_profile_version",
        "schema_version",
        "expected_schema_version",
        "runtime_root",
        "input_root",
        "persistent_output_storage",
        "opencode_pinned_version",
        "opencode_version_observer",
    }
)


class CapabilityProbeError(ValueError):
    """The probe input is not a bounded, secret-free fact set."""


def _text(value: object, field: str, *, version: bool = False) -> str:
    pattern = _VERSION if version else _IDENTIFIER
    if type(value) is not str or not pattern.fullmatch(value):
        raise CapabilityProbeError(f"{field} must be a bounded identifier.")
    return value


def _secret_free(value: object, *, depth: int = 0) -> None:
    if depth > 3:
        raise CapabilityProbeError("Probe payload is too deeply nested.")
    if isinstance(value, Mapping):
        if len(value) > 16:
            raise CapabilityProbeError("Probe payload has too many fields.")
        for key, item in value.items():
            if type(key) is not str or not _IDENTIFIER.fullmatch(key) or _SECRET.search(key):
                raise CapabilityProbeError("Probe payload contains an unsafe field.")
            _secret_free(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 16:
            raise CapabilityProbeError("Probe payload has too many values.")
        for item in value:
            _secret_free(item, depth=depth + 1)
    elif isinstance(value, str) and len(value.encode()) > MAX_DETAIL_BYTES:
        raise CapabilityProbeError("Probe text exceeds its bound.")
    elif not (value is None or type(value) in (bool, int, float, str)):
        raise CapabilityProbeError("Probe payload contains an unsupported value.")


@dataclass(frozen=True)
class CapabilityProbeFacts:
    """Explicit observations and expectations supplied to the read-only probe."""

    runtime_profile_version: str
    expected_runtime_profile_version: str
    schema_version: str
    expected_schema_version: str
    runtime_root: Path
    input_root: Path
    persistent_output_storage: dict[str, Any] | None = None
    opencode_pinned_version: str | None = None
    opencode_version_observer: Callable[[], object] | None = None

    def __post_init__(self) -> None:
        for field in ("runtime_profile_version", "expected_runtime_profile_version"):
            _text(getattr(self, field), field, version=True)
        for field in ("schema_version", "expected_schema_version"):
            _text(getattr(self, field), field, version=True)
        for field in ("runtime_root", "input_root"):
            value = getattr(self, field)
            if (
                not isinstance(value, Path)
                or not value.is_absolute()
                or len(str(value).encode()) > MAX_DETAIL_BYTES
            ):
                raise CapabilityProbeError(f"{field} must be an absolute Path.")
        if self.persistent_output_storage is not None:
            if type(self.persistent_output_storage) is not dict:
                raise CapabilityProbeError("persistent_output_storage must be an object.")
            _secret_free(self.persistent_output_storage)
        if self.opencode_pinned_version is not None:
            _text(self.opencode_pinned_version, "opencode_pinned_version", version=True)
        if self.opencode_version_observer is not None and not callable(
            self.opencode_version_observer
        ):
            raise CapabilityProbeError("opencode_version_observer must be callable.")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CapabilityProbeFacts:
        if type(value) is not dict:
            raise CapabilityProbeError("Probe facts must be a plain object.")
        unknown = set(value) - _FACT_FIELDS
        if unknown or any(type(key) is not str for key in value):
            raise CapabilityProbeError("Probe facts contain an unsupported field.")
        required = _FACT_FIELDS - {
            "persistent_output_storage",
            "opencode_pinned_version",
            "opencode_version_observer",
        }
        missing = required - set(value)
        if missing:
            raise CapabilityProbeError("Probe facts are missing a required field.")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CapabilityCheck:
    identifier: str
    passed: bool
    detail: str
    observation: str | None = None

    def __post_init__(self) -> None:
        _text(self.identifier, "check identifier")
        if type(self.passed) is not bool or type(self.detail) is not str:
            raise CapabilityProbeError("Capability checks have invalid strict types.")
        if len(self.detail.encode()) > MAX_DETAIL_BYTES:
            raise CapabilityProbeError("Capability check detail exceeds its bound.")
        if self.observation is not None and type(self.observation) is not str:
            raise CapabilityProbeError("Capability observation must be text.")
        if self.observation is not None and len(self.observation.encode()) > MAX_DETAIL_BYTES:
            raise CapabilityProbeError("Capability observation exceeds its bound.")


@dataclass(frozen=True)
class CapabilityProbeReport:
    checks: tuple[CapabilityCheck, ...]
    authority_effect: str = "none"
    attestation_verified: bool = False
    dispatch_enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.checks) is not tuple or not 1 <= len(self.checks) <= MAX_CHECKS:
            raise CapabilityProbeError("Capability report has an invalid check set.")
        if any(type(check) is not CapabilityCheck for check in self.checks):
            raise CapabilityProbeError("Capability report contains an invalid check.")
        if (
            self.authority_effect != "none"
            or self.attestation_verified is not False
            or self.dispatch_enabled is not False
        ):
            raise CapabilityProbeError("Capability reports are never authoritative.")

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def _root_check(identifier: str, root: Path) -> CapabilityCheck:
    try:
        info = root.lstat()
        is_private = stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
        detail = (
            "exists as a non-symlink private directory"
            if is_private
            else "not a non-symlink 0700 directory"
        )
        return CapabilityCheck(identifier, is_private, detail, str(root))
    except (OSError, ValueError):
        return CapabilityCheck(
            identifier, False, "does not exist or cannot be inspected", str(root)
        )


def evaluate_capability_probe(
    facts: CapabilityProbeFacts | Mapping[str, object],
) -> CapabilityProbeReport:
    """Evaluate bounded facts without changing filesystem or control-plane state."""
    if not isinstance(facts, CapabilityProbeFacts):
        facts = CapabilityProbeFacts.from_mapping(facts)
    checks = [
        CapabilityCheck(
            "runtime-profile-version",
            facts.runtime_profile_version == facts.expected_runtime_profile_version,
            "exact runtime profile version match"
            if facts.runtime_profile_version == facts.expected_runtime_profile_version
            else "runtime profile version mismatch",
            facts.runtime_profile_version,
        ),
        CapabilityCheck(
            "schema-version",
            facts.schema_version == facts.expected_schema_version,
            "exact schema version match"
            if facts.schema_version == facts.expected_schema_version
            else "schema version mismatch",
            facts.schema_version,
        ),
        _root_check("runtime-root-private", facts.runtime_root),
        _root_check("input-root-private", facts.input_root),
    ]
    if facts.persistent_output_storage is not None:
        try:
            canonical = validate_persistent_output_storage(dict(facts.persistent_output_storage))
            checks.append(
                CapabilityCheck(
                    "persistent-output-declaration",
                    True,
                    "declared/unverified; canonicalized by the existing validator",
                    repr(tuple(canonical.items())),
                )
            )
        except Exception:
            checks.append(
                CapabilityCheck(
                    "persistent-output-declaration",
                    False,
                    "declared/unverified declaration is malformed",
                )
            )
    if facts.opencode_pinned_version is not None:
        try:
            observed = (
                facts.opencode_version_observer() if facts.opencode_version_observer else None
            )
            passed = type(observed) is str and observed == facts.opencode_pinned_version
            detail = (
                "exact pinned OpenCode version observed"
                if passed
                else "OpenCode version mismatch or missing"
            )
            checks.append(
                CapabilityCheck(
                    "opencode-version",
                    passed,
                    detail,
                    observed if isinstance(observed, str) else None,
                )
            )
        except Exception:
            checks.append(
                CapabilityCheck("opencode-version", False, "OpenCode observation failed closed")
            )
    return CapabilityProbeReport(tuple(checks))


__all__ = [
    "CapabilityCheck",
    "CapabilityProbeError",
    "CapabilityProbeFacts",
    "CapabilityProbeReport",
    "evaluate_capability_probe",
]
