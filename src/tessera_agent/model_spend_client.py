"""Client of the control plane's model-spend ledger (reserve, settle, release).

The reasoning supervisor reserves a call's worst-case cost on the control plane before a billed
model call, settles the actual cost after it, and releases the reservation when the call was
definitely not sent. The control plane enforces the per-call and daily caps; this client only
carries the three requests and fails closed: a transport error, a non-answer or a malformed
answer is "not allowed", never an exception into the model call and never a silent pass.

Only allowlisted fields are read from a response and nothing from a response body is logged or
stored beyond them. Idempotency keys are derived from (run, seq, operation), so a repeated
request after a crash or a lost answer returns the stored result instead of creating a second one.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

CAP_EXCEEDED = "model_spend_cap_exceeded"
NOT_CONFIGURED = "model_spend_not_configured"
UNAVAILABLE = "model_spend_unavailable"
# The grant that pays for a call refused it (ADR 0142): nothing was reserved or sent.
GRANT_BUDGET_EXCEEDED = "grant_budget_exceeded"
GRANT_NOT_ACTIVE = "grant_not_active"
# A reasoning grant pays only for its own provider and model.
GRANT_SCOPE_MISMATCH = "grant_scope_mismatch"
GRANT_CODES = frozenset({GRANT_BUDGET_EXCEEDED, GRANT_NOT_ACTIVE, GRANT_SCOPE_MISMATCH})
DENIAL_CODES = frozenset({CAP_EXCEEDED, NOT_CONFIGURED}) | GRANT_CODES
RESOLUTIONS = frozenset({"agent_settled", "agent_released", "person_released", "person_settled"})
_STATUSES = frozenset({"reserved", "settled", "released"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MICRO = 1_000_000


@dataclass(frozen=True)
class Reservation:
    allowed: bool
    call_id: str | None = None
    code: str | None = None
    reserved_micro_cents: int | None = None


@dataclass(frozen=True)
class Settlement:
    ok: bool
    call_id: str | None = None
    settled_micro_cents: int | None = None
    over_reservation: bool = False
    cap_exceeded: bool = False
    code: str | None = None


def _cents(value: Any) -> float | None:
    """A finite non-negative number of cents, rounded UP to a micro-cent (never understated)."""
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    return math.ceil(value * _MICRO) / _MICRO


@dataclass(frozen=True)
class LedgerStatus:
    """What the control plane says about one reservation (allowlisted fields only)."""

    call_id: str
    status: str
    resolution: str | None = None
    settled_micro_cents: int | None = None


class SpendLedger:
    """``control`` is any object with ``call(method, path, body=None, key=None) -> dict``."""

    def __init__(self, control: Any, project_id: str | None, provider_id: str, model: str) -> None:
        self.control, self.project_id = control, project_id
        self.provider_id, self.model = provider_id, model

    def _call(self, path: str, body: dict, key: str) -> dict | None:
        try:
            answer = self.control.call("POST", path, body, key=key)
        except Exception:  # noqa: BLE001 - sanitized: any failure is "not answered"
            return None
        return answer if isinstance(answer, dict) else None

    @staticmethod
    def _key(run_id: str, seq: int, op: str) -> str:
        """Hashed, never truncated: a long run id cannot cut off the sequence or the operation."""
        digest = hashlib.sha256(f"{run_id}|{seq}|{op}".encode()).hexdigest()
        return f"model-spend:{digest[:48]}"

    def reserve(
        self,
        run_id: str,
        seq: int,
        worst_cents: float,
        project_id: str | None = None,
        grant_id: str | None = None,
    ) -> Reservation:
        project = project_id or self.project_id
        worst = _cents(worst_cents)
        if not project or not isinstance(run_id, str) or type(seq) is not int or worst is None:
            return Reservation(False, code=UNAVAILABLE)
        answer = self._call(
            "/model-spend/reservations",
            {
                "project_id": project,
                "run_id": run_id,
                "seq": seq,
                "provider_id": self.provider_id,
                "model": self.model,
                "worst_case_cents": worst,
                **({"grant_id": grant_id} if grant_id else {}),
            },
            self._key(run_id, seq, "reserve"),
        )
        if answer is None or type(answer.get("allowed")) is not bool:
            return Reservation(False, code=UNAVAILABLE)
        if not answer["allowed"]:
            code = answer.get("code")
            return Reservation(False, code=code if code in DENIAL_CODES else UNAVAILABLE)
        call_id, micro = answer.get("call_id"), answer.get("reserved_micro_cents")
        if (
            not isinstance(call_id, str)
            or not _ID.fullmatch(call_id)
            or answer.get("status") != "reserved"
            or type(micro) is not int
            or micro < 0
        ):
            return Reservation(False, code=UNAVAILABLE)
        return Reservation(True, call_id=call_id, reserved_micro_cents=micro)

    def settle(
        self, call_id: str, run_id: str, seq: int, actual_cents: float, receipt_sha256: str
    ) -> Settlement:
        actual = _cents(actual_cents)
        if actual is None or not _ID.fullmatch(call_id or ""):
            return Settlement(False, code=UNAVAILABLE)
        answer = self._call(
            f"/model-spend/reservations/{call_id}/settlement",
            {"actual_cents": actual, "receipt_sha256": receipt_sha256},
            self._key(run_id, seq, "settle"),
        )
        if (
            answer is None
            or answer.get("call_id") != call_id
            or answer.get("status") != "settled"
            or type(answer.get("settled_micro_cents")) is not int
            or answer["settled_micro_cents"] < 0
            or type(answer.get("over_reservation")) is not bool
            or type(answer.get("cap_exceeded")) is not bool
        ):
            return Settlement(False, code=UNAVAILABLE)
        return Settlement(
            True,
            call_id=call_id,
            settled_micro_cents=answer["settled_micro_cents"],
            over_reservation=answer["over_reservation"],
            cap_exceeded=answer["cap_exceeded"],
        )

    def status(self, call_id: str, run_id: str) -> LedgerStatus | None:
        """Read one reservation of ``run_id``; None for any error or an answer that is not exactly
        the contract. The control plane answers only for the caller's own run, so another run's
        call id is never reported."""
        if not _ID.fullmatch(call_id or "") or not isinstance(run_id, str) or not run_id:
            return None
        try:
            answer = self.control.call(
                "GET", f"/model-spend/reservations/{call_id}?run_id={quote(run_id, safe='')}"
            )
        except Exception:  # noqa: BLE001 - sanitized: unknown, never an exception
            return None
        if not isinstance(answer, dict) or answer.get("call_id") != call_id:
            return None
        status, resolution = answer.get("status"), answer.get("resolution")
        micro = answer.get("settled_micro_cents")
        if (
            status not in _STATUSES
            or (resolution is not None and resolution not in RESOLUTIONS)
            or (micro is not None and (type(micro) is not int or micro < 0))
        ):
            return None
        return LedgerStatus(call_id, status, resolution, micro)

    def release(self, call_id: str, run_id: str, seq: int) -> bool:
        if not _ID.fullmatch(call_id or ""):
            return False
        answer = self._call(
            f"/model-spend/reservations/{call_id}/release",
            {"reason": "not_sent"},
            self._key(run_id, seq, "release"),
        )
        return (
            answer is not None
            and answer.get("call_id") == call_id
            and answer.get("status") == "released"
        )
