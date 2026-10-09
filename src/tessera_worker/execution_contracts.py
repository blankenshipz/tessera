"""Provider-neutral execution-plane contracts.

The control plane freezes authority and intent before an adapter receives an
``ExecutionEnvelope``.  Adapters translate the opaque envelope payload into
their provider-specific request shape; this module deliberately does not model
Jobs, cloud instances, credentials, or artifact storage implementations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")

ExecutionState = Literal["pending", "running", "succeeded", "failed", "unknown"]
ExposureState = Literal["unknown", "reported"]


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value.encode("utf-8")) > 512:
        raise ValueError(f"{field} must be a bounded non-empty identifier.")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field} must be a SHA-256 digest.")
    return value


def _operation_id(value: object) -> str:
    if not isinstance(value, str) or not _OPERATION_ID.fullmatch(value):
        raise ValueError("operation_id must be exactly 32 lowercase hexadecimal characters.")
    return value


def _deadline(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class TargetBinding:
    """An opaque, digest-pinned execution target selection.

    ``adapter_kind`` and ``profile_version`` select a compatible adapter.  The
    target-specific identity material is represented only by its digest here;
    its validation remains in the adapter that understands it.
    """

    adapter_kind: str
    profile_version: str
    identity_digest: str

    def __post_init__(self) -> None:
        _identifier(self.adapter_kind, "adapter_kind")
        _identifier(self.profile_version, "profile_version")
        _digest(self.identity_digest, "target identity_digest")


@dataclass(frozen=True)
class ExecutionEnvelope:
    """Frozen operation authority plus an adapter-owned opaque payload.

    The envelope is not permission to replace an uncertain operation.  Its
    operation ID, package digest, target binding, epoch, and absolute deadline
    remain stable across submit, observation, cancellation, and recovery.
    """

    operation_id: str
    package_digest: str
    target: TargetBinding
    lease_id: str
    epoch: int
    deadline: datetime
    payload: object

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _digest(self.package_digest, "package_digest")
        if not isinstance(self.target, TargetBinding):
            raise ValueError("target must be a TargetBinding.")
        _identifier(self.lease_id, "lease_id")
        if type(self.epoch) is not int or not 1 <= self.epoch <= 2**63 - 1:
            raise ValueError("epoch must be a positive integer.")
        _deadline(self.deadline, "deadline")


@dataclass(frozen=True)
class OperationHandle:
    """An attributed external-operation identity, possibly still unresolved."""

    target: TargetBinding
    operation_id: str
    provider_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, TargetBinding):
            raise ValueError("target must be a TargetBinding.")
        _operation_id(self.operation_id)
        if self.provider_id is not None:
            _identifier(self.provider_id, "provider_id")


@dataclass(frozen=True)
class Observation:
    """One attributed provider observation; ``unknown`` remains actionable."""

    handle: OperationHandle
    state: ExecutionState
    observed_at: datetime
    termination_confirmed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.handle, OperationHandle):
            raise ValueError("handle must be an OperationHandle.")
        if self.state not in {"pending", "running", "succeeded", "failed", "unknown"}:
            raise ValueError("state is invalid.")
        _deadline(self.observed_at, "observed_at")
        if type(self.termination_confirmed) is not bool:
            raise ValueError("termination_confirmed must be boolean.")


@dataclass(frozen=True)
class ExecutionCapabilities:
    """Declared adapter operations; limitations are informative, never authority."""

    preflight: bool
    submit: bool
    observe: bool
    cancel: bool
    reconcile: bool
    collect: bool
    account: bool
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        flags = (
            self.preflight,
            self.submit,
            self.observe,
            self.cancel,
            self.reconcile,
            self.collect,
            self.account,
        )
        if any(type(value) is not bool for value in flags):
            raise ValueError("execution capability flags must be boolean.")
        if type(self.limitations) is not tuple or any(
            not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512
            for value in self.limitations
        ):
            raise ValueError("capability limitations must be bounded non-empty strings.")


@dataclass(frozen=True)
class PreflightRequest:
    target: TargetBinding
    envelope: ExecutionEnvelope

    def __post_init__(self) -> None:
        if not isinstance(self.target, TargetBinding) or not isinstance(
            self.envelope, ExecutionEnvelope
        ):
            raise ValueError("preflight requires typed target and envelope.")
        if self.target != self.envelope.target:
            raise ValueError("preflight target conflicts with the execution envelope.")


@dataclass(frozen=True)
class PreflightResult:
    target: TargetBinding
    capabilities: ExecutionCapabilities
    admissible: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, TargetBinding) or not isinstance(
            self.capabilities, ExecutionCapabilities
        ):
            raise ValueError("preflight result requires typed target and capabilities.")
        if type(self.admissible) is not bool:
            raise ValueError("admissible must be boolean.")
        if self.admissible and self.reason is not None:
            raise ValueError("an admissible preflight cannot carry a rejection reason.")
        if not self.admissible:
            _identifier(self.reason, "preflight rejection reason")


@dataclass(frozen=True)
class CancellationObservation:
    """Cancellation request evidence without treating an acknowledgement as a stop."""

    handle: OperationHandle
    requested: bool
    termination_confirmed: bool
    reconciliation_required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.handle, OperationHandle):
            raise ValueError("handle must be an OperationHandle.")
        flags = (self.requested, self.termination_confirmed, self.reconciliation_required)
        if any(type(value) is not bool for value in flags):
            raise ValueError("cancellation flags must be boolean.")
        if self.termination_confirmed and self.reconciliation_required:
            raise ValueError("confirmed termination cannot require reconciliation.")


@dataclass(frozen=True)
class CollectionHandoff:
    """A target-bound collection request, not evidence bytes or result acceptance."""

    handle: OperationHandle
    output_paths: tuple[str, ...]
    state: Literal["collect_requested"] = "collect_requested"

    def __post_init__(self) -> None:
        if not isinstance(self.handle, OperationHandle):
            raise ValueError("handle must be an OperationHandle.")
        if (
            type(self.output_paths) is not tuple
            or not self.output_paths
            or any(
                not isinstance(path, str)
                or not path
                or "\0" in path
                or len(path.encode("utf-8")) > 4096
                for path in self.output_paths
            )
        ):
            raise ValueError("output_paths must be a non-empty bounded tuple.")
        if self.state != "collect_requested":
            raise ValueError("collection state must be collect_requested.")


@dataclass(frozen=True)
class AccountingObservation:
    """Provider exposure observation; unknown exposure is never converted to zero."""

    handle: OperationHandle
    exposure_state: ExposureState
    usage: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.handle, OperationHandle):
            raise ValueError("handle must be an OperationHandle.")
        if self.exposure_state not in {"unknown", "reported"}:
            raise ValueError("exposure_state is invalid.")
        if self.usage is not None and not isinstance(self.usage, dict):
            raise ValueError("usage must be an object when reported.")
        if self.exposure_state == "unknown" and self.usage is not None:
            raise ValueError("unknown exposure cannot carry reported usage.")


class TargetPreflight(Protocol):
    def capabilities(self, target: TargetBinding) -> ExecutionCapabilities: ...

    def preflight(self, request: PreflightRequest) -> PreflightResult: ...


@dataclass(frozen=True)
class TargetHealth:
    """Discovery is information, never admission or proof of provider execution."""

    target: TargetBinding | None
    status: Literal["disabled", "unavailable", "unknown", "healthy"]
    reason: str

    def __post_init__(self) -> None:
        if self.target is not None and not isinstance(self.target, TargetBinding):
            raise ValueError("health requires a typed target when configured")
        if self.status not in {"disabled", "unavailable", "unknown", "healthy"}:
            raise ValueError("invalid target health status")
        _identifier(self.reason, "health reason")


class TargetDiscovery(Protocol):
    def capabilities(self, target: TargetBinding) -> ExecutionCapabilities: ...

    def health(self, target: TargetBinding | None = None) -> TargetHealth: ...


class ExecutionSubmitter(Protocol):
    def submit(
        self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None
    ) -> Observation: ...


class ExecutionObserver(Protocol):
    def observe(
        self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None
    ) -> Observation: ...


class ExecutionCanceller(Protocol):
    def cancel(
        self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None
    ) -> CancellationObservation: ...


class ExecutionReconciler(Protocol):
    def reconcile(
        self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None
    ) -> Observation: ...


class ArtifactCollector(Protocol):
    def collect(self, envelope: ExecutionEnvelope) -> CollectionHandoff: ...


class ExecutionAccountant(Protocol):
    def account(self, envelope: ExecutionEnvelope) -> AccountingObservation: ...


@runtime_checkable
class ExecutionAdapter(
    TargetPreflight,
    ExecutionSubmitter,
    ExecutionObserver,
    ExecutionCanceller,
    ExecutionReconciler,
    ArtifactCollector,
    ExecutionAccountant,
    Protocol,
):
    """Provider-neutral composition used by future execution adapters."""


__all__ = [
    "AccountingObservation",
    "ArtifactCollector",
    "CancellationObservation",
    "CollectionHandoff",
    "ExecutionAccountant",
    "ExecutionAdapter",
    "ExecutionCapabilities",
    "ExecutionCanceller",
    "ExecutionEnvelope",
    "ExecutionObserver",
    "ExecutionReconciler",
    "ExecutionState",
    "ExecutionSubmitter",
    "ExposureState",
    "Observation",
    "OperationHandle",
    "PreflightRequest",
    "PreflightResult",
    "TargetBinding",
    "TargetDiscovery",
    "TargetHealth",
    "TargetPreflight",
]
