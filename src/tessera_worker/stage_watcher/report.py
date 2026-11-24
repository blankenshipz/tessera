"""Closed watch reports and the control sink that delivers them.

A report carries ids, a kind from a closed set, an ISO 8601 time, integer counters from a
closed key set, the kill-switch state and the contract digest. A ``failed`` report about a
container the probe read as definitively exited (an integer exit code) may also carry the
optional closed ``exit`` object ``{exit_code, oom_killed}`` (ADR 0158); no other report ever
does. It never carries free text, a log line, a prompt, a destination or a credential.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol

from tessera_control.stage_watch_limits import (
    PERMANENT_REASONS,
    REASON_STATUS,
    TERMINAL_KINDS,
)
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
# Optional body keys: present only when set. ``exit`` is only ever on a ``failed`` report.
OPTIONAL_BODY_KEYS: frozenset[str] = frozenset({"exit"})
EXIT_KEYS: frozenset[str] = frozenset({"exit_code", "oom_killed"})
MAX_EXIT_CODE = 255
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_COUNTER = 2**53  # the control route accepts counters up to 2**53

_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNTER:
        raise ValueError(f"{name} must be a non-negative integer.")


@dataclass(frozen=True)
class WatchExit:
    """What the probe read for a definitively exited container: its integer exit code and
    whether the kernel's out-of-memory killer ended it. Never built for an unknown, absent or
    deadline failure, nor for an exit with no code."""

    exit_code: int
    oom_killed: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.exit_code, bool)
            or not isinstance(self.exit_code, int)
            or not 0 <= self.exit_code <= MAX_EXIT_CODE
        ):
            raise ValueError("exit_code must be an integer in 0..255.")
        if not isinstance(self.oom_killed, bool):
            raise ValueError("oom_killed must be a bool.")

    def to_body(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "oom_killed": self.oom_killed}

    @classmethod
    def from_body(cls, body: object) -> WatchExit:
        if not isinstance(body, Mapping) or set(body) != EXIT_KEYS:
            raise ValueError("exit must have exactly the closed key set.")
        return cls(exit_code=body["exit_code"], oom_killed=body["oom_killed"])


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
    exit: WatchExit | None = None

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
        if self.exit is not None:
            if not isinstance(self.exit, WatchExit):
                raise ValueError("exit must be None or a WatchExit.")
            if self.kind != "failed":
                raise ValueError("only a failed report may carry an exit.")

    @property
    def idempotency_key(self) -> str:
        return idempotency_key(self.operation_id, self.seq)

    def to_body(self) -> dict[str, Any]:
        """The closed JSON body for the control watch-reports route (``exit`` only when set)."""
        body: dict[str, Any] = {
            "lease_id": self.lease_id,
            "expected_epoch": self.epoch,
            "kind": self.kind,
            "observed_at": self.observed_at.isoformat(),
            "seq": self.seq,
            "counters": {key: self.counters[key] for key in sorted(self.counters)},
            "kill_switch_engaged": self.kill_switch_engaged,
            "contract_digest": self.contract_digest,
        }
        if self.exit is not None:
            body["exit"] = self.exit.to_body()
        return body

    @classmethod
    def from_body(cls, operation_id: str, body: Mapping[str, Any]) -> WatchReport:
        if (
            not isinstance(body, Mapping)
            or set(body) - OPTIONAL_BODY_KEYS != BODY_KEYS
            or not set(body) <= BODY_KEYS | OPTIONAL_BODY_KEYS
        ):
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
            exit=WatchExit.from_body(body["exit"]) if "exit" in body else None,
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

# The statuses that settle a report as refused on their own: control understood the report
# and no retry under the same key can change its answer. For ``stuck``/``failed`` every other
# status (401/403, 408, 409, 425, 429, other 4xx, 5xx, or no answer) is retried under the same
# key until the outbox's 24 h abandon deadline, unless control's reason code says the refusal
# is permanent (``reason_settles``), because an expired credential, a clock skew or a busy
# control plane can clear on retry.
TERMINAL_REFUSAL_STATUSES: frozenset[int] = frozenset({400, 404, 410, 413, 422})
# Routine kinds also settle on 409 (control's routine budget, or an out-of-order report).
ROUTINE_REFUSAL_STATUSES: frozenset[int] = TERMINAL_REFUSAL_STATUSES | {409}
# The only statuses a reason code can settle: 401 never does (a credential refresh fixes it),
# and a code settles only with the status control pairs it with in ``REASON_STATUS``.
REASON_REFUSAL_STATUSES: frozenset[int] = frozenset({403, 404, 409})
_REASON_TOKEN = re.compile(r"[a-z][a-z_]{0,40}")


def refusal_statuses(kind: str) -> frozenset[int]:
    """The statuses that settle a report of ``kind`` without a reason code."""
    return TERMINAL_REFUSAL_STATUSES if kind in TERMINAL_KINDS else ROUTINE_REFUSAL_STATUSES


def reason_settles(status: object, reason: object) -> bool:
    """True when control's ``reason`` code names a permanent refusal for this ``status``.

    The code must be in control's closed ``PERMANENT_REASONS`` and be the one control answers
    with exactly this status (``REASON_STATUS``), and the status must be 403, 404 or 409. An
    unknown, missing, mismatched or transient code (``too_early``) settles nothing.
    """
    if isinstance(status, bool) or not isinstance(status, int):
        return False
    if status not in REASON_REFUSAL_STATUSES or not isinstance(reason, str):
        return False
    return reason in PERMANENT_REASONS and REASON_STATUS.get(reason) == status


def settles(kind: str, status: object, reason: object = None) -> bool:
    """Whether a refusal with ``status`` (and control's ``reason`` code) settles ``kind``."""
    if isinstance(status, bool) or not isinstance(status, int):
        return False
    return status in refusal_statuses(kind) or reason_settles(status, reason)


@dataclass(frozen=True)
class DeliveryResult:
    """What one delivery attempt established.

    - ``delivered``: control accepted the report (2xx).
    - ``refused``: control definitely and permanently refused this report (``settles``: a
      status in ``refusal_statuses(kind)``, or a 403/404/409 whose ``reason`` code is a
      permanent one for that status). It is settled as refused, counted, and never retried
      or resent under another key.
    - ``transient``: anything else (network error, timeout, 5xx, 401, 403/409 without a
      permanent reason code, 408/425/429, a malformed reply, and another 4xx for
      ``stuck``/``failed``). The report stays pending and is retried under the SAME key.

    ``reason`` is control's refusal code when it sent a short token, else None.
    """

    outcome: DeliveryOutcome
    status: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ("delivered", "refused", "transient"):
            raise ValueError("outcome must be delivered, refused or transient.")
        if self.status is not None and (
            isinstance(self.status, bool) or not isinstance(self.status, int)
        ):
            raise ValueError("status must be None or an integer.")
        if self.outcome == "refused" and (self.status is None or not 400 <= self.status < 500):
            raise ValueError("a refusal carries the 4xx status control answered.")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not _REASON_TOKEN.fullmatch(self.reason)
        ):
            raise ValueError("reason must be None or a short lowercase token.")


DELIVERED = DeliveryResult("delivered")
TRANSIENT = DeliveryResult("transient")


def classify_status(status: int | None, kind: str, reason: str | None = None) -> DeliveryResult:
    """Map a control error status (and its reason code) for ``kind`` to a delivery result.

    None means no HTTP answer. Only the status and the closed reason code are used, never a
    message; a reason that is not a short token is ignored.
    """
    if not isinstance(reason, str) or not _REASON_TOKEN.fullmatch(reason):
        reason = None
    if isinstance(status, bool) or not isinstance(status, int):
        return TRANSIENT
    if settles(kind, status, reason):
        return DeliveryResult("refused", status, reason)
    return DeliveryResult("transient", status, reason)


class WatchReportSink(Protocol):
    def deliver(self, report: WatchReport) -> DeliveryResult: ...


# The shortest HTTP timeout a post is given, however little of the caller's budget is left.
MIN_POST_TIMEOUT_SECONDS = 0.05


def _positive(value: object) -> float | None:
    """``value`` as a float when it is a positive finite number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if 0 < value < float("inf") else None


def effective_post_timeout(
    channel: object,
    timeout_seconds: float | None,
    remaining: Callable[[], float | None] | None,
) -> float | None:
    """The HTTP timeout for one post: the smallest of ``timeout_seconds``, the channel's own
    ``timeout_seconds`` and, when ``remaining`` is given, the caller's remaining time budget
    (never below ``MIN_POST_TIMEOUT_SECONDS``). None: the channel's own default."""
    timeout = _positive(timeout_seconds)
    channel_timeout = _positive(getattr(channel, "timeout_seconds", None))
    if channel_timeout is not None:
        timeout = channel_timeout if timeout is None else min(timeout, channel_timeout)
    if remaining is not None:
        left = remaining()
        if left is not None:
            left = max(MIN_POST_TIMEOUT_SECONDS, float(left))
            timeout = left if timeout is None else min(timeout, left)
    return timeout


class ControlSink:
    """Deliver one report through the worker's control channel.

    The channel returns normally only on a 2xx reply (the route answers 201): that is
    ``delivered``. A ``ControlChannelError`` is classified by its status and control's reason
    code (``classify_status``): a permanent status for the report's kind, or a 403/404/409
    whose code is a permanent one for that status, is ``refused``. Anything else, including
    401 and 403 without such a code (the channel refreshes its credential), 408/425/429, 5xx,
    a timeout, a transport error, and 409 for ``stuck``/``failed`` without a permanent code
    (``too_early``, unknown or none), is ``transient``: the report stays in the durable
    outbox and is redelivered under the SAME idempotency key; it is never replayed under
    another key.

    The HTTP timeout of each post is the smallest of ``timeout_seconds``, the channel's own
    ``timeout_seconds`` (the channel refuses a longer one) and, when ``remaining`` is given,
    the caller's remaining time budget (never below ``MIN_POST_TIMEOUT_SECONDS``).
    """

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

    def deliver(self, report: WatchReport) -> DeliveryResult:
        try:
            reply = self.channel.post(
                watch_reports_path(report.operation_id),
                report.to_body(),
                report.idempotency_key,
                timeout_seconds=self.effective_timeout(),
            )
        except ControlChannelError as error:
            return classify_status(
                error.status, report.kind, getattr(error, "refusal_reason", None)
            )
        except Exception:
            return TRANSIENT
        return DELIVERED if isinstance(reply, dict) else TRANSIENT
