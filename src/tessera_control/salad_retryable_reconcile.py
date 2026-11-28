"""A person's reconcile of an unknown Salad attempt as retryable infrastructure (ADR 0216).

The ordinary human grant reconcile (``POST /execution-grants/{id}/reconcile``) needs the caller
to name a settlement and always fails the attempt's stage permanently. This route is the other
option: the person asks only that the attempt be treated as an infrastructure failure. The
amount is never the caller's: it is the guardian's bound on the attempt (its hold), and the route
refuses unless control's own records show the guardian's frame closed and no output kept
(``salad_guardian_bound``). The grant is reconciled at that bound with the shared accounting
(ADR 0048) and one ``execution.grant_reconciled_retryable_infra`` event; the runner then settles
the attempt at it and retries the stage within ``retry.infra_retries`` (never a scientific
retry), or, with none left, fails that stage only.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from .campaign_infra_retry import RETRYABLE_RECONCILE_EVENT
from .execution import ExecutionStore, reconcile_grant_row
from .execution_models import ExecutionRequest, Revision
from .guardian_record_reader import validate_frame_record
from .salad_guardian_bound import guardian_bound_refusal, output_kept
from .store import Conflict, StoreError, _connect, canonical_digest, utc_now

PATH = "/execution-grants/{grant_id}/reconcile-retryable-salad"

REFUSALS = {
    "no_unknown_attempt": "Only the grant of an unsettled, unknown Salad campaign attempt "
    "can be reconciled as retryable infrastructure.",
    "no_guardian_final": "No guardian final is recorded for this attempt; use the ordinary "
    "reconcile.",
    "guardian_final_unreadable": "The recorded guardian final cannot be read.",
    "guardian_frame_not_closed": "The guardian has not closed this frame (stopped, or "
    "reconciled by its provider-confirmed close).",
    "guardian_final_not_clean": "The guardian final records an orphan or a pending escalation.",
    "no_hold": "The attempt holds nothing to settle at.",
    "estimate_unreadable": "The guardian's cost estimate cannot be read.",
    "estimate_above_hold": "The guardian's estimate is above the hold; use the ordinary "
    "reconcile with the recorded charge.",
    "billed_above_hold": "The recorded bill is above the hold; use the ordinary reconcile.",
    "hold_above_grant": "The attempt's hold is above the grant's exposure.",
    "output_retained": "An output of this attempt was retained; it is not retried as "
    "infrastructure.",
    "output_collected": "An output of this attempt was collected; it is not retried as "
    "infrastructure.",
    "live_receipt_unreadable": "The attempt's live receipt cannot be read.",
    "allocation_uncertain": "The allocation was uncertain and the guardian never confirmed "
    "its close with the provider.",
    "guardian_unreadable": "Control could not read the guardian's own record of this frame; "
    "nothing was reconciled.",
    "guardian_frame_open": "Control's own read of the guardian record shows the frame still "
    "open; nothing was reconciled.",
}
# Guardian record verdicts that mean the frame is closed (control's own read, ADR 0041 s7.1).
_CLOSED_VERDICTS = frozenset({"stopped", "reconciled"})


def _guardian_reader() -> Any:
    """Control's own read-only guardian client, the one the Salad provider holds; None when
    this process has none (then the route refuses: fail closed)."""
    from .campaign_providers import get_provider

    try:
        return getattr(get_provider("salad"), "guardian_reader", None)
    except KeyError:
        return None


def guardian_read(reader: Any, reservation: str) -> tuple[str | None, dict[str, Any] | None]:
    """``(refusal, record)`` from control's own read of the guardian's frame record: None and
    the record when it reads closed; otherwise the closed refusal code (fail closed)."""
    if reader is None:
        return "guardian_unreadable", None
    try:
        record = validate_frame_record(reader.read(reservation), reservation)
    except Exception:  # noqa: BLE001 - an unreachable guardian refuses, never passes
        record = None
    if record is None:
        return "guardian_unreadable", None
    if record["verdict"] not in _CLOSED_VERDICTS:
        return "guardian_frame_open", None
    return None, record


class ReconcileRetryableSalad(ExecutionRequest):
    """Only the revision: the settlement is the guardian's bound, never the caller's."""

    expected_revision: Revision


def retryable_refusal(
    connection: sqlite3.Connection, grant_id: str
) -> tuple[str | None, dict[str, Any] | None, int]:
    """``(refusal, attempt, bound)``: ``refusal`` is None when the grant may be reconciled as
    retryable infrastructure at ``bound`` cents. A pure read."""
    found = connection.execute(
        """SELECT a.*, sa.guardian_final_json, sa.live_receipt_json,
        sa.manifest_id AS salad_manifest_id FROM campaign_stage_attempts a
        JOIN campaign_salad_attempts sa ON sa.attempt_id = a.id
        WHERE a.grant_id=? AND a.status='unknown' AND a.settled_cents IS NULL""",
        (grant_id,),
    ).fetchall()
    if len(found) != 1:
        return "no_unknown_attempt", None, 0
    attempt = dict(found[0])
    row = {
        "guardian_final_json": attempt["guardian_final_json"],
        "live_receipt_json": attempt["live_receipt_json"],
        "manifest_id": attempt["salad_manifest_id"],
    }
    # A person's route: an operator-attested guardian reconcile closes the frame too.
    refusal = guardian_bound_refusal(attempt, row, attested=True) or output_kept(
        connection, attempt, row
    )
    return refusal, attempt, int(attempt.get("hold_cents") or 0)


class SaladRetryableReconcileStore(ExecutionStore):
    def reconcile_retryable(
        self,
        principal: dict[str, Any],
        grant_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        try:
            payload = ReconcileRetryableSalad.model_validate(payload).model_dump(mode="json")
        except ValidationError:
            raise StoreError("Invalid retryable Salad reconcile request.") from None
        path = PATH.format(grant_id=grant_id)
        # ADR 0216 item 3 (review F2): control's own read of the guardian record, before the
        # write transaction (a network read never holds the database). The person's request and
        # an operator's attestation never replace it.
        reservation = self._reservation(grant_id)
        read_refusal, record = (
            ("no_unknown_attempt", None)
            if reservation is None
            else guardian_read(_guardian_reader(), reservation)
        )

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(actor)
            grant = self._grant(connection, actor, grant_id)
            if grant["revision"] != payload["expected_revision"]:
                raise Conflict("Execution grant revision is no longer current.")
            if grant["status"] not in ("active", "cancel_requested"):
                raise Conflict("Only an unresolved grant can be reconciled.")
            refusal, attempt, bound = retryable_refusal(connection, grant_id)
            if refusal is None and bound > int(grant["max_exposure_cents"]):
                refusal = "hold_above_grant"
            if refusal is None and (
                read_refusal is not None or self._reservation(grant_id, connection) != reservation
            ):
                refusal = read_refusal or "guardian_unreadable"
            if refusal is not None or attempt is None:
                raise Conflict(REFUSALS[refusal or "no_unknown_attempt"])
            limits = self._limit(connection, actor, grant["project_id"])
            now = utc_now()
            if (
                reconcile_grant_row(
                    connection, grant, settlement_cents=bound, principal_id=actor["id"], now=now
                )
                is None
            ):
                raise Conflict("Execution reservation ledger is inconsistent; authority held.")
            final = json.loads(attempt["guardian_final_json"])
            next_revision = grant["revision"] + 1
            updated = dict(grant)
            updated.update(
                status="reconciled",
                revision=next_revision,
                updated_at=now,
                reconciled_at=now,
                reconciled_by_principal_id=actor["id"],
                settlement_cents=bound,
            )
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                RETRYABLE_RECONCILE_EVENT,
                next_revision,
                {
                    "grant_id": grant_id,
                    "campaign_id": attempt["campaign_id"],
                    "stage_key": attempt["stage_key"],
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "settlement_cents": bound,
                    "settlement_basis": "guardian_bound",
                    "settlement_supplied_by_caller": False,
                    "currency": limits["currency"],
                    "termination_evidence": {
                        "guardian_status": final.get("status"),
                        "guardian_reason": str(final.get("reason") or "")[:80],
                    },
                    "output_kept": False,
                    "guardian_read": {
                        "verdict": None if record is None else record["verdict"],
                        "record_digest": None if record is None else record["record_digest"],
                        "sequence": None if record is None else record["sequence"],
                    },
                    "operational_only": True,
                },
            )
            return 200, self._grant_response(connection, updated)

        return self._mutate(principal, key, "POST", path, digest, command)

    def _reservation(
        self, grant_id: str, connection: sqlite3.Connection | None = None
    ) -> str | None:
        """The guardian reservation of the grant's one unknown Salad attempt (its route and
        proposal digest), or None."""
        own = connection is None
        connection = connection or _connect(self.database_path)
        try:
            rows = connection.execute(
                """SELECT sa.route_id, sa.proposal_digest FROM campaign_stage_attempts a
                JOIN campaign_salad_attempts sa ON sa.attempt_id = a.id
                WHERE a.grant_id=? AND a.status='unknown' AND a.settled_cents IS NULL""",
                (grant_id,),
            ).fetchall()
        finally:
            if own:
                connection.close()
        if len(rows) != 1 or rows[0]["route_id"] is None or rows[0]["proposal_digest"] is None:
            return None
        return canonical_digest([rows[0]["route_id"], rows[0]["proposal_digest"]])
