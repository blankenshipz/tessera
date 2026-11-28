"""The guardian's bound on a Salad attempt, and the receipt-backed defaults built on it (ADR 0216).

A Salad attempt's hold is the most the guardian lets its frame spend: the trusted server refuses a
create whose budget binding is below the profile's cost cap, and the guardian fences the frame at
that cap. Once the guardian's own final says the frame is closed (``stopped``, or ``reconciled``
by the safety service's provider-confirmed close, never an operator attestation), with no orphan,
no pending escalation and no estimate or bill above the hold, nothing more can accrue and the
hold bounds the cost. Settling at the hold never under-records: the settlement is the provisional
``guardian_bounded_hold``, so a later recorded charge confirms it or adds the excess.

Pure reads only. The callers (the Salad driver's charge deadline, the human retryable reconcile
route) apply the result in their own transaction.
"""

from __future__ import annotations

import json
import math
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from .campaign_store import _GuardianFinal
from .salad_guardian import RECONCILE_PROVIDER

# How long a settled-outcome attempt waits for a recorded compute charge before the
# receipt-backed default applies (``awaiting_charge``), counted from the first time a person was
# asked to record it. Without a default the campaign blocks once, for that attempt only.
CHARGE_DEADLINE_SECONDS = 3600
# The waiting reason (and attempt detail) of an attempt whose charge deadline passed with no
# default: the blocked campaign's evidence pass re-reads exactly this reason every pass.
CHARGE_OVERDUE = "charge overdue: no recorded charge and no guardian-bounded default"
CHARGE_DEADLINE_SETTLED_EVENT = "campaign.salad_charge_deadline_settled"
CHARGE_OVERDUE_RESUMED_EVENT = "campaign.salad_charge_overdue_resumed"
CHARGE_OVERDUE_BLOCK_REASON = "charge_overdue"
# The overdue record of a campaign that cannot be blocked (it is cancelling).
CHARGE_OVERDUE_EVENT = "campaign.salad_charge_overdue"


def _final(row: dict[str, Any]) -> tuple[_GuardianFinal | None, str | None]:
    if row.get("guardian_final_json") is None:
        return None, "no_guardian_final"
    try:
        return _GuardianFinal.model_validate_json(row["guardian_final_json"]), None
    except ValueError:
        return None, "guardian_final_unreadable"


def guardian_closed(final: _GuardianFinal, *, attested: bool = False) -> bool:
    """The guardian's final says its frame is closed: ``stopped``, or ``reconciled`` by the
    safety service's own provider-confirmed close. An operator's attested reconcile qualifies
    only with ``attested`` (a person's own reconcile route), never for an automatic default."""
    return final.status == "stopped" or (
        final.status == "reconciled" and (attested or final.reason == RECONCILE_PROVIDER)
    )


def guardian_bound_refusal(
    attempt: dict[str, Any], row: dict[str, Any], *, attested: bool = False
) -> str | None:
    """``None`` when the attempt's hold is a receipt-backed bound on its cost (see the module
    docstring); otherwise the closed code of the first condition that fails (fail closed).
    ``attested``: see ``guardian_closed``."""
    final, code = _final(row)
    if final is None:
        return code
    if not guardian_closed(final, attested=attested):
        return "guardian_frame_not_closed"
    if final.orphan or final.escalation_pending:
        return "guardian_final_not_clean"
    # ADR 0212's human case stands: an uncertain allocation is bounded only by the safety
    # service's provider-confirmed close (or, on a person's own route, an operator's attested
    # reconcile), never by a plain ``stopped`` (as strict as ``_clean_guardian``).
    if final.allocation_uncertain and not (
        final.status == "reconciled" and (attested or final.reason == RECONCILE_PROVIDER)
    ):
        return "allocation_uncertain"
    hold = int(attempt.get("hold_cents") or 0)
    if hold < 1:
        return "no_hold"
    if final.estimated_exposure_cents is not None:
        try:
            estimate = Decimal(final.estimated_exposure_cents)
        except InvalidOperation:
            return "estimate_unreadable"
        if not estimate.is_finite() or estimate < 0:
            return "estimate_unreadable"
        if math.ceil(estimate) > hold:
            return "estimate_above_hold"
    if final.actual_billed_cost_cents is not None and final.actual_billed_cost_cents > hold:
        return "billed_above_hold"
    return None


def output_kept(
    connection: sqlite3.Connection, attempt: dict[str, Any], row: dict[str, Any]
) -> str | None:
    """The closed code when any output of the attempt may have been retained, sealed or collected
    (``output_retained``, ``output_collected``, ``live_receipt_unreadable``); ``None`` when
    control's records show none. A pure read; an unreadable record counts as kept."""
    try:
        final = json.loads(row.get("guardian_final_json") or "null")
    except ValueError:
        final = None
    if type(final) is dict and final.get("retained_before_move") is True:
        return "output_retained"
    if row.get("manifest_id") is not None or attempt.get("manifest_id") is not None:
        return "output_collected"
    try:
        receipt = json.loads(row.get("live_receipt_json") or "null")
    except ValueError:
        return "live_receipt_unreadable"
    if receipt is not None:
        if type(receipt) is not dict:
            return "live_receipt_unreadable"
        cancel = receipt.get("control_cancel")
        if (
            receipt.get("status") == "passed"
            or receipt.get("outputs")
            or receipt.get("output_sha256") is not None
            or (type(cancel) is dict and cancel.get("output_retained") is True)
        ):
            return "output_retained"
    grant_id = attempt.get("grant_id")
    for table in ("artifact_manifests", "artifacts"):
        if (
            grant_id is None
            or connection.execute(
                f"SELECT 1 FROM {table} WHERE grant_id=? LIMIT 1", (grant_id,)
            ).fetchone()
            is not None
        ):
            return "output_collected"
    return None
