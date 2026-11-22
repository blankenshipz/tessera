"""Closed watch reports and the control sink that delivers them.

A report carries ids, a kind from a closed set, an ISO 8601 time, integer counters from a
closed key set, the kill-switch state and the contract digest. It never carries free text, a
log line, a prompt, a destination or a credential.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol

from tessera_control.stage_watch_limits import TERMINAL_KINDS
from tessera_worker.control_channel import ControlChannelError

ReportKind = Literal["heartbeat", "progress", "stuck", "failed"]
REPORT_KINDS: frozenset[str] = frozenset({"heartbeat", "progress", "stuck", "failed"})
COUNTER_KEYS: frozenset[str] = frozenset(
    {
        "elapsed_s",
        "silent_s",
        "mem_bytes",
        "cpu_ms",
        "output_bytes",
        "restarts",
        # Local outbox gaps, present once non-zero: reports control refused, and reports
        # abandoned after 24 h or dropped at the total report cap.
        "refused_reports",
        "dropped_reports",
    }
)
BODY_KEYS: frozenset[str] = frozenset(
    {
        "lease_id",
        "expected_epoch",
        "kind",
        "observed_at",
        "seq",
        "counters",
        "kill_switch_engaged",
        "contract_digest",
    }
)
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_COUNTER = 2**53  # the control route accepts counters up to 2**53

_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNTER:
        raise ValueError(f"{name} must be a non-negative integer.")


@dataclass(frozen=True)
class WatchReport:
    operation_id: str
    lease_id: str
    epoch: int
    kind: ReportKind
    observed_at: datetime
    seq: int
    counters: Mapping[str, int]
    kill_switch_engaged: bool
    contract_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not _OPERATION_ID.fullmatch(self.operation_id):
            raise ValueError("operation_id must be 32 lowercase hex characters.")
        if not isinstance(self.lease_id, str) or not _TOKEN.fullmatch(self.lease_id):
            raise ValueError("lease_id must be a bounded identifier token.")
        _non_negative_int("epoch", self.epoch)
        _non_negative_int("seq", self.seq)
        if self.kind not in REPORT_KINDS:
            raise ValueError("kind must be heartbeat, progress, stuck or failed.")
        if not isinstance(self.observed_at, datetime) or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be a timezone-aware datetime.")
        if not isinstance(self.counters, Mapping):
            raise ValueError("counters must be a mapping.")
        counters = dict(self.counters)
        for key, value in counters.items():
            if key not in COUNTER_KEYS:
                raise ValueError("counters has a key outside the closed set.")
            _non_negative_int(f"counters[{key}]", value)
        object.__setattr__(self, "counters", MappingProxyType(counters))
        if not isinstance(self.kill_switch_engaged, bool):
            raise ValueError("kill_switch_engaged must be a bool.")
        if not isinstance(self.contract_digest, str) or not _DIGEST.fullmatch(self.contract_digest):
            raise ValueError("contract_digest must be a sha256 hex digest.")

    @property
    def idempotency_key(self) -> str:
        return idempotency_key(self.operation_id, self.seq)

    def to_body(self) -> dict[str, Any]:
        """The closed JSON body for the control watch-reports route."""
        return {
            "lease_id": self.lease_id,
            "expected_epoch": self.epoch,
            "kind": self.kind,
            "observed_at": self.observed_at.isoformat(),
            "seq": self.seq,
            "counters": {key: self.counters[key] for key in sorted(self.counters)},
            "kill_switch_engaged": self.kill_switch_engaged,
            "contract_digest": self.contract_digest,
        }

    @classmethod
    def from_body(cls, operation_id: str, body: Mapping[str, Any]) -> WatchReport:
        if not isinstance(body, Mapping) or set(body) != BODY_KEYS:
            raise ValueError("report body must have exactly the closed key set.")
        observed_at = body["observed_at"]
        if not isinstance(observed_at, str):
            raise ValueError("observed_at must be an ISO 8601 string.")
        return cls(
            operation_id=operation_id,
            lease_id=body["lease_id"],
            epoch=body["expected_epoch"],
            kind=body["kind"],
            observed_at=datetime.fromisoformat(observed_at),
            seq=body["seq"],
            counters=body["counters"],
            kill_switch_engaged=body["kill_switch_engaged"],
            contract_digest=body["contract_digest"],
        )


def idempotency_key(operation_id: str, seq: int) -> str:
    """Stable per-report key: the same report is always redelivered under the same key."""
    key = f"watch-{operation_id}-{seq}"
    if len(key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ValueError("idempotency key exceeds its bound.")
    return key


def watch_reports_path(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError("operation_id must be 32 lowercase hex characters.")
    return f"/worker/operations/{operation_id}/watch-reports"


class ReportSink(Protocol):
    """The control channel's POST shape (tessera_worker.control_channel.HttpControlChannel)."""

    def post(
        self,
        path: str,
        body: dict[str, Any],
        key: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


DeliveryOutcome = Literal["delivered", "refused", "transient"]

# The only statuses that settle a report as refused: control understood the report and no
# retry under the same key can change its answer. ``stuck``/``failed`` settle only on these;
# every other status (401/403 whatever the message, 408, 409, 425, 429, other 4xx, 5xx, or no
# answer) is retried under the same key until the outbox's 24 h abandon deadline, because an
# expired credential, a clock skew or a busy control plane can clear on retry.
TERMINAL_REFUSAL_STATUSES: frozenset[int] = frozenset({400, 404, 410, 413, 422})
# Routine kinds also settle on 409 (control's routine budget, or an out-of-order report).
ROUTINE_REFUSAL_STATUSES: frozenset[int] = TERMINAL_REFUSAL_STATUSES | {409}


def refusal_statuses(kind: str) -> frozenset[int]:
    """The statuses that settle a report of ``kind`` as refused (everything else retries)."""
    return TERMINAL_REFUSAL_STATUSES if kind in TERMINAL_KINDS else ROUTINE_REFUSAL_STATUSES


@dataclass(frozen=True)
class DeliveryResult:
    """What one delivery attempt established.

    - ``delivered``: control accepted the report (2xx).
    - ``refused``: control definitely and permanently refused this report (a status in
      ``refusal_statuses(kind)``). It is settled as refused, counted, and never retried or
      resent under another key.
    - ``transient``: anything else (network error, timeout, 5xx, 401/403, 408/425/429, a
      malformed reply, and 409 or another 4xx for ``stuck``/``failed``). The report stays
      pending and is retried under the SAME key.
    """

    outcome: DeliveryOutcome
    status: int | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ("delivered", "refused", "transient"):
            raise ValueError("outcome must be delivered, refused or transient.")
        if self.status is not None and (
            isinstance(self.status, bool) or not isinstance(self.status, int)
        ):
            raise ValueError("status must be None or an integer.")
        if self.outcome == "refused" and (self.status is None or not 400 <= self.status < 500):
            raise ValueError("a refusal carries the 4xx status control answered.")


DELIVERED = DeliveryResult("delivered")
TRANSIENT = DeliveryResult("transient")


def classify_status(status: int | None, kind: str) -> DeliveryResult:
    """Map a control error status for a report of ``kind`` to a delivery result.

    None means no HTTP answer. Only the status is used, never a message.
    """
    if isinstance(status, bool) or not isinstance(status, int):
        return TRANSIENT
    if status in refusal_statuses(kind):
        return DeliveryResult("refused", status)
    return DeliveryResult("transient", status)


class WatchReportSink(Protocol):
    def deliver(self, report: WatchReport) -> DeliveryResult: ...


class ControlSink:
    """Deliver one report through the worker's control channel.

    The channel returns normally only on a 2xx reply (the route answers 201): that is
    ``delivered``. A ``ControlChannelError`` is classified by its status alone
    (``classify_status``): only a permanent status for the report's kind is ``refused``.
    Anything else, including 401/403 (the channel refreshes its credential), 408/425/429, 5xx,
    a timeout, a transport error, and 409 for ``stuck``/``failed``, is ``transient``: the
    report stays in the durable outbox and is redelivered under the SAME idempotency key; it
    is never replayed under another key.
    """

    def __init__(self, channel: ReportSink, *, timeout_seconds: float | None = None):
        self.channel = channel
        self.timeout_seconds = timeout_seconds

    def deliver(self, report: WatchReport) -> DeliveryResult:
        try:
            reply = self.channel.post(
                watch_reports_path(report.operation_id),
                report.to_body(),
                report.idempotency_key,
                timeout_seconds=self.timeout_seconds,
            )
        except ControlChannelError as error:
            return classify_status(error.status, report.kind)
        except Exception:
            return TRANSIENT
        return DELIVERED if isinstance(reply, dict) else TRANSIENT
