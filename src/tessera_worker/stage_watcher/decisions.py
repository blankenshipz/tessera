"""Closed decision requests the stage watcher may file (ADR 0087 section 4 "Requests").

A request PROPOSES; the runner decides. The watcher never performs a retry, resize, re-route or
stop, and nothing here does: a request is a closed record that is written to the durable outbox
before it is sent (record before act) and then delivered to
``POST /worker/operations/{operation_id}/watch-decisions`` under the stable key
``watch-decision-<operation_id>-<decision_seq>``.

What may be requested is decided deterministically from one probe sample and the contract's
optional envelope (``decide``):

- ``retry_request`` / ``transient_exit``: the run exited (not OOM-killed) with an exit code in
  ``transient_exit_codes`` and ``retry_attempts_remaining > 0``.
- ``resize_request`` / ``oom_killed``: the run exited OOM-killed and
  ``min(2 * memory_bytes_current, memory_bytes_max) > memory_bytes_current``; that is the
  ``requested_memory_bytes``.

A contract without an envelope never yields a request. ``probe_lost`` and ``resource_limit`` are
in the closed reason set for the control side but are never produced here (a lost probe only
annotates the ``failed`` report). The watcher files at most one request per kind per operation
per epoch, and none while the kill switch is engaged.

Delivery reuses ``DeliveryResult``. A request settles as refused on the same rules as a closing
report (``stuck``/``failed``): a status in ``TERMINAL_REFUSAL_STATUSES`` or a 403/404/409 whose
reason code is a permanent one for that status. Everything else is transient and retried under
the same key; an undelivered request is abandoned after 24 h and counted in the outbox gaps.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from tessera_worker.control_channel import ControlChannelError

from .contract import MAX_BYTES, StageContract
from .probe import ProbeSample
from .report import (
    DELIVERED,
    MAX_IDEMPOTENCY_KEY_CHARS,
    TERMINAL_REFUSAL_STATUSES,
    TRANSIENT,
    DeliveryResult,
    ReportSink,
    effective_post_timeout,
    reason_settles,
)

DecisionKind = Literal["retry_request", "resize_request"]
DecisionReason = Literal["transient_exit", "oom_killed", "resource_limit", "probe_lost"]
DECISION_KINDS: frozenset[str] = frozenset({"retry_request", "resize_request"})
DECISION_REASONS: frozenset[str] = frozenset(
    {"transient_exit", "oom_killed", "resource_limit", "probe_lost"}
)
# The closed kind-to-reason pairing (control's set; the watcher produces only
# transient_exit and oom_killed).
KIND_REASONS: Mapping[str, frozenset[str]] = {
    "retry_request": frozenset({"transient_exit", "probe_lost"}),
    "resize_request": frozenset({"oom_killed", "resource_limit"}),
}
DECISION_BODY_KEYS: frozenset[str] = frozenset(
    {
        "lease_id",
        "expected_epoch",
        "kind",
        "reason",
        "observed_at",
        "decision_seq",
        "requested_memory_bytes",
        "contract_digest",
    }
)
MAX_DECISION_SEQ = 2**53

_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REASON_TOKEN = re.compile(r"[a-z][a-z_]{0,40}")


def _non_negative_int(name: str, value: object, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a non-negative integer.")


@dataclass(frozen=True)
class DecisionRequest:
    operation_id: str
    lease_id: str
    epoch: int
    kind: DecisionKind
    reason: DecisionReason
    observed_at: datetime
    decision_seq: int
    requested_memory_bytes: int | None
    contract_digest: str
    kill_switch_engaged: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not _OPERATION_ID.fullmatch(self.operation_id):
            raise ValueError("operation_id must be 32 lowercase hex characters.")
        if not isinstance(self.lease_id, str) or not _TOKEN.fullmatch(self.lease_id):
            raise ValueError("lease_id must be a bounded identifier token.")
        _non_negative_int("epoch", self.epoch, 2**63 - 1)
        _non_negative_int("decision_seq", self.decision_seq, MAX_DECISION_SEQ)
        if self.kind not in DECISION_KINDS:
            raise ValueError("kind must be retry_request or resize_request.")
        if self.reason not in KIND_REASONS[self.kind]:
            raise ValueError("reason is outside the closed set for this kind.")
        if not isinstance(self.observed_at, datetime) or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be a timezone-aware datetime.")
        memory = self.requested_memory_bytes
        if self.kind == "resize_request":
            if (
                isinstance(memory, bool)
                or not isinstance(memory, int)
                or not 0 < memory <= MAX_BYTES
            ):
                raise ValueError("a resize request carries a positive requested_memory_bytes.")
        elif memory is not None:
            raise ValueError("only a resize request carries requested_memory_bytes.")
        if not isinstance(self.contract_digest, str) or not _DIGEST.fullmatch(self.contract_digest):
            raise ValueError("contract_digest must be a sha256 hex digest.")
        # A request is never filed while the switch is engaged (ADR 0087 section 7.1).
        if self.kill_switch_engaged is not False:
            raise ValueError("a decision request is never made with the kill switch engaged.")

    @property
    def idempotency_key(self) -> str:
        return decision_idempotency_key(self.operation_id, self.decision_seq)

    def to_body(self) -> dict[str, Any]:
        """The closed JSON body for the control watch-decisions route."""
        body: dict[str, Any] = {
            "lease_id": self.lease_id,
            "expected_epoch": self.epoch,
            "kind": self.kind,
            "reason": self.reason,
            "observed_at": self.observed_at.isoformat(),
            "decision_seq": self.decision_seq,
            "contract_digest": self.contract_digest,
        }
        if self.requested_memory_bytes is not None:
            body["requested_memory_bytes"] = self.requested_memory_bytes
        return body

    @classmethod
    def from_body(cls, operation_id: str, body: Mapping[str, Any]) -> DecisionRequest:
        if not isinstance(body, Mapping) or not (
            DECISION_BODY_KEYS - {"requested_memory_bytes"} <= set(body) <= DECISION_BODY_KEYS
        ):
            raise ValueError("decision body must have exactly the closed key set.")
        observed_at = body["observed_at"]
        if not isinstance(observed_at, str):
            raise ValueError("observed_at must be an ISO 8601 string.")
        return cls(
            operation_id=operation_id,
            lease_id=body["lease_id"],
            epoch=body["expected_epoch"],
            kind=body["kind"],
            reason=body["reason"],
            observed_at=datetime.fromisoformat(observed_at),
            decision_seq=body["decision_seq"],
            requested_memory_bytes=body.get("requested_memory_bytes"),
            contract_digest=body["contract_digest"],
        )


def decision_idempotency_key(operation_id: str, decision_seq: int) -> str:
    """Stable per-request key: a request is always redelivered under the same key."""
    key = f"watch-decision-{operation_id}-{decision_seq}"
    if len(key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ValueError("idempotency key exceeds its bound.")
    return key


def watch_decisions_path(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError("operation_id must be 32 lowercase hex characters.")
    return f"/worker/operations/{operation_id}/watch-decisions"


@dataclass(frozen=True)
class Proposal:
    """What ``decide`` proposes before the watcher numbers and records it."""

    kind: DecisionKind
    reason: DecisionReason
    requested_memory_bytes: int | None = None


def decide(contract: StageContract, sample: ProbeSample) -> tuple[Proposal, ...]:
    """The requests one probe sample justifies under the contract's envelope. Pure.

    Only an ``exited`` sample can justify a request. No envelope, no request.
    """
    if sample.state != "exited" or not contract.has_envelope:
        return ()
    if sample.oom_killed:
        current, cap = contract.memory_bytes_current, contract.memory_bytes_max
        if current is None or cap is None:
            return ()
        target = min(2 * current, cap)
        if target <= current:
            return ()
        return (Proposal("resize_request", "oom_killed", target),)
    codes = contract.transient_exit_codes or ()
    remaining = contract.retry_attempts_remaining
    if sample.exit_code in codes and remaining is not None and remaining > 0:
        return (Proposal("retry_request", "transient_exit"),)
    return ()


def decision_settles(status: object, reason: object = None) -> bool:
    """Whether a refusal settles a request: the closing-report rules (never a bare 409)."""
    if isinstance(status, bool) or not isinstance(status, int):
        return False
    return status in TERMINAL_REFUSAL_STATUSES or reason_settles(status, reason)


def classify_decision_status(status: int | None, reason: str | None = None) -> DeliveryResult:
    if not isinstance(reason, str) or not _REASON_TOKEN.fullmatch(reason):
        reason = None
    if isinstance(status, bool) or not isinstance(status, int):
        return TRANSIENT
    if decision_settles(status, reason):
        return DeliveryResult("refused", status, reason)
    return DeliveryResult("transient", status, reason)


class DecisionSink(Protocol):
    def deliver_decision(self, request: DecisionRequest) -> DeliveryResult: ...


class ControlDecisionSink:
    """Deliver one request through the worker's control channel (the same channel and the same
    three-valued result as ``ControlSink``; only the route and the settle rule differ).

    Each post's HTTP timeout follows the same rule as ``ControlSink``: the smallest of
    ``timeout_seconds``, the channel's own ``timeout_seconds`` and, when ``remaining`` is
    given, the caller's remaining time budget (never below ``MIN_POST_TIMEOUT_SECONDS``)."""

    def __init__(
        self,
        channel: ReportSink,
        *,
        timeout_seconds: float | None = None,
        remaining: Callable[[], float | None] | None = None,
    ):
        self.channel = channel
        self.timeout_seconds = timeout_seconds
        self.remaining = remaining

    def effective_timeout(self) -> float | None:
        """The timeout for the next post (None: the channel's own default)."""
        return effective_post_timeout(self.channel, self.timeout_seconds, self.remaining)

    def deliver_decision(self, request: DecisionRequest) -> DeliveryResult:
        try:
            reply = self.channel.post(
                watch_decisions_path(request.operation_id),
                request.to_body(),
                request.idempotency_key,
                timeout_seconds=self.effective_timeout(),
            )
        except ControlChannelError as error:
            return classify_decision_status(error.status, getattr(error, "refusal_reason", None))
        except Exception:
            return TRANSIENT
        return DELIVERED if isinstance(reply, dict) else TRANSIENT


@dataclass(frozen=True)
class DecisionEnvelope:
    """The optional envelope a caller hands the runner for one attempt (copied into the stage
    contract, where it is validated and covered by the digest). All None: no requests."""

    retry_attempts_remaining: int | None = None
    transient_exit_codes: tuple[int, ...] | None = None
    memory_bytes_current: int | None = None
    memory_bytes_max: int | None = None

    def contract_fields(self) -> dict[str, Any]:
        return {
            "retry_attempts_remaining": self.retry_attempts_remaining,
            "transient_exit_codes": self.transient_exit_codes,
            "memory_bytes_current": self.memory_bytes_current,
            "memory_bytes_max": self.memory_bytes_max,
        }
