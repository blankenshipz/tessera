"""The single campaign settlement routine.

This module is the only code that moves campaign attempt money: it settles an attempt's
hold (``settle_attempt``), attaches a late charge to an existing settlement
(``apply_late_charge``) and relabels a provisional settlement once its charge is known
(``confirm_settlement``). No other module writes ``settled_cents``, ``held_cents`` or
``settlement_basis`` on ``campaigns`` or ``campaign_stage_attempts``; the one exception is
the hold reservation in ``CampaignRunner._reserve_hold``, which adds a new attempt's hold
before any settlement exists. ``tests/test_campaign_settlement.py`` enforces this.

ADR 0041 ("Execution frames") stage 3: ``settle_frame`` is the single settlement path for
a framed attempt. It wraps ``settle_attempt``'s idempotency (a second call with the same
request digest returns the stored settlement and writes nothing, a different digest raises
``Conflict``, and the compare and swap on ``settled_cents IS NULL`` stays as a second
guard), records the frame settlement, releases the experiment hold for a zero ``final``
basis with evidence (ADR 0041 Q3) and closes the frame with its receipt. The
``provider_absent_zero`` basis is accepted only there, and only with absence evidence or
for a frame cancelled before any create intent. ``settle_attempt`` stays for unframed
callers. Late charges never reopen a settlement.

Every function runs in the caller's transaction and never commits. Provider bookkeeping
that must commit with a settlement is passed as ``on_settled``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, get_args

from . import execution_frames as frames
from .campaign_providers import SettlementBasis
from .experiment_budgets import RELEASE_BASES, release_hold
from .store import Conflict, NotFound, canonical_digest

# The closed core enum: a provider cannot invent a basis, and nothing unknown is stored.
BASES = frozenset(get_args(SettlementBasis))
# Bases that prove nothing was spent: they can only settle zero.
ZERO_ONLY_BASES = frozenset({"local_zero", "unclaimed_zero", "provider_absent_zero"})
# Only a provisional bounded hold is non-final; every other basis is a final amount.
NON_FINAL_BASES = frozenset({"guardian_bounded_hold"})
# Proven absence: settled only through settle_frame, with its evidence (ADR 0041 Q2).
ABSENCE_BASIS = "provider_absent_zero"
# Bases that rest on a provider or guardian estimate, not on proof of zero or a human's
# recorded charge. On an ambiguous billable frame they need the guardian verdict (ADR 0041).
VERDICT_BASES = frozenset({"guardian_bounded_hold", "provider_billed"})
# Frame states whose provider outcome is not a clean, verified terminal.
UNVERIFIED_STATES = frozenset({"lost", "failed"})


class SettlementRefused(ValueError):
    """The settlement itself is invalid (unknown basis, or a nonzero amount on a zero basis)."""


class VerdictRequired(SettlementRefused):
    """An ambiguous billable frame has no guardian verdict read: nothing settles, the frame
    stays open with its exposure held."""


def validate_amount(basis: str, cents: int) -> None:
    """The closed basis enum and the zero-only rule, checked before anything is read."""
    if basis not in BASES:
        raise SettlementRefused("unknown settlement basis")
    if basis in ZERO_ONLY_BASES and cents != 0:
        raise SettlementRefused(f"{basis} cannot settle {cents}")


def _attempt(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT campaign_id, hold_cents, settled_cents, settlement_basis,
        settlement_request_digest FROM campaign_stage_attempts WHERE id=?""",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"campaign attempt {attempt_id} not found")
    return row


def _stored(attempt_id: str, row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "settled_cents": row["settled_cents"],
        "settlement_basis": row["settlement_basis"],
        "request_digest": row["settlement_request_digest"],
        "replayed": replayed,
    }


def settle_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    cents: int,
    basis: str,
    request_digest: str,
    now: str,
    on_settled: Callable[[sqlite3.Connection, str, int, str], None] | None = None,
) -> dict[str, Any]:
    """Move one attempt's hold to settled, exactly once, in the caller's transaction."""
    if basis == ABSENCE_BASIS:
        raise SettlementRefused(f"{ABSENCE_BASIS} settles only through settle_frame")
    return _settle_attempt(
        connection,
        attempt_id,
        cents=cents,
        basis=basis,
        request_digest=request_digest,
        now=now,
        on_settled=on_settled,
    )


def _settle_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    cents: int,
    basis: str,
    request_digest: str,
    now: str,
    on_settled: Callable[[sqlite3.Connection, str, int, str], None] | None = None,
) -> dict[str, Any]:
    validate_amount(basis, cents)
    attempt = _attempt(connection, attempt_id)
    if attempt["settled_cents"] is not None:
        stored = attempt["settlement_request_digest"]
        same = (
            stored == request_digest
            if stored is not None
            # A settlement written before the digest was stored: compare what was recorded.
            else attempt["settlement_basis"] == basis
        )
        if not same:
            raise Conflict("attempt is already settled by a different request")
        return _stored(attempt_id, attempt, replayed=True)
    claimed = connection.execute(
        """UPDATE campaign_stage_attempts SET settled_cents=?, settlement_basis=?,
        settlement_request_digest=?, revision=revision+1, updated_at=?
        WHERE id=? AND settled_cents IS NULL""",
        (cents, basis, request_digest, now, attempt_id),
    )
    if not claimed.rowcount:  # lost a race inside the transaction: the other writer's wins
        raise Conflict("attempt was settled concurrently")
    connection.execute(
        """UPDATE campaigns SET held_cents=held_cents-?, settled_cents=settled_cents+?
        WHERE id=?""",
        (attempt["hold_cents"], cents, attempt["campaign_id"]),
    )
    if on_settled is not None:
        on_settled(connection, attempt_id, cents, now)
    return _stored(attempt_id, _attempt(connection, attempt_id), replayed=False)


def apply_late_charge(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    delta_cents: int,
    charged_total: int,
    now: str,
) -> None:
    """Add a charge beyond the settled amount to an existing settlement.

    The settlement is never reopened: its request digest and the hold already moved stay as
    they are, and a provisional bounded-hold basis becomes ``human_charge``."""
    if delta_cents <= 0 or charged_total < delta_cents:
        raise SettlementRefused("a late charge must be positive and within the charged total")
    attempt = _attempt(connection, attempt_id)
    if attempt["settled_cents"] is None:
        raise Conflict("a late charge needs a settled attempt")
    connection.execute(
        """UPDATE campaign_stage_attempts SET settled_cents=settled_cents+?,
        settlement_basis=CASE WHEN settlement_basis='guardian_bounded_hold'
        THEN 'human_charge' ELSE settlement_basis END,
        revision=revision+1, updated_at=? WHERE id=?""",
        (delta_cents, now, attempt_id),
    )
    connection.execute(
        "UPDATE campaigns SET settled_cents=settled_cents+? WHERE id=?",
        (delta_cents, attempt["campaign_id"]),
    )


def confirm_settlement(
    connection: sqlite3.Connection, attempt_id: str, *, charged_total: int, now: str
) -> bool:
    """Relabel a provisional bounded-hold settlement as a human charge (amount unchanged).

    Returns whether a label changed. Any other basis is left alone."""
    if charged_total < 0:
        raise SettlementRefused("charged total cannot be negative")
    return bool(
        connection.execute(
            """UPDATE campaign_stage_attempts SET settlement_basis='human_charge',
            revision=revision+1, updated_at=?
            WHERE id=? AND settled_cents IS NOT NULL
            AND settlement_basis='guardian_bounded_hold'""",
            (now, attempt_id),
        ).rowcount
    )


@dataclass(frozen=True)
class FrameSettlement:
    """One settlement request for a frame (ADR 0041 section 3).

    ``absence_evidence_digest`` is the digest of the absence reads (an exact-name miss and a
    complete list) that ``provider_absent_zero`` needs unless the frame was cancelled before
    any create intent. ``evidence`` lands on the receipt."""

    tenant: frames.Tenant
    cents: int
    basis: str
    request_digest: str
    now: str
    actor: frames.Actor
    final: bool | None = None  # None: derived from the basis
    absence_evidence_digest: str | None = None
    verdict_digest: str | None = None
    estimate_cents: int | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    on_settled: Callable[[sqlite3.Connection, str, int, str], None] | None = None


def _final(settlement: FrameSettlement) -> bool:
    derived = settlement.basis not in NON_FINAL_BASES
    if settlement.final is not None and settlement.final != derived:
        raise SettlementRefused(f"{settlement.basis} cannot settle with final={settlement.final}")
    return derived


def _unverified(frame: Mapping[str, Any]) -> bool:
    """A billable frame whose provider outcome is not a clean verified terminal."""
    if int(frame["hold_cents"]) < 1 or frame["state"] == "settling":
        return False
    return (
        frame["state"] in UNVERIFIED_STATES or frame["ambiguous_reason"] in frames.OUTCOME_AMBIGUITY
    )


def _stored_frame(frame: Mapping[str, Any], row: sqlite3.Row, *, replayed: bool) -> dict:
    return {
        "frame_id": frame["frame_id"],
        "settlement_id": row["settlement_id"],
        "attempt_id": row["campaign_attempt_id"],
        "settled_cents": row["cents"],
        "settlement_basis": row["basis"],
        "final": bool(row["final"]),
        "request_digest": row["request_digest"],
        "receipt_digest": row["receipt_digest"],
        "replayed": replayed,
    }


def settle_frame(
    connection: sqlite3.Connection, frame_id: str, settlement: FrameSettlement
) -> dict[str, Any]:
    """Settle and close one frame, exactly once, in the caller's transaction.

    The frame must be terminal (or already ``settling``). A second call with the same
    request digest returns the stored settlement and writes nothing; a different digest
    raises ``Conflict``. The caller holds the runner lease."""
    frame = frames.get_frame(connection, frame_id, settlement.tenant)
    stored = connection.execute(
        "SELECT * FROM execution_frame_settlements WHERE frame_id=?", (frame_id,)
    ).fetchone()
    if stored is not None:
        if stored["request_digest"] != settlement.request_digest:
            raise Conflict("frame is already settled by a different request")
        return _stored_frame(frame, stored, replayed=True)
    if frame["state"] not in frames.TERMINAL_STATES and frame["state"] != "settling":
        raise frames.FrameRefused(f"a {frame['state']} frame cannot settle")
    basis, cents = settlement.basis, settlement.cents
    validate_amount(basis, cents)
    final = _final(settlement)
    if basis == ABSENCE_BASIS:
        cancelled_unsent = frame["outcome"] == "cancelled" and frame["create_intent_id"] is None
        if not settlement.absence_evidence_digest and not cancelled_unsent:
            raise SettlementRefused(
                f"{ABSENCE_BASIS} needs absence reads or a cancel before any create intent"
            )
    verdict_digest = settlement.verdict_digest or frame["guardian_verdict_digest"]
    if basis in VERDICT_BASES and _unverified(frame):
        if not verdict_digest:
            raise VerdictRequired(
                f"{basis} on a {frame['state']} billable frame needs a guardian verdict read"
            )
        if cents > int(frame["hold_cents"]):
            raise SettlementRefused(f"{basis} cannot settle above the hold")
    if frame["state"] != "settling":
        frame = frames.transition(
            connection,
            frame_id,
            settlement.tenant,
            to_state="settling",
            expected_revision=frame["revision"],
            trigger="settle",
            actor=settlement.actor,
            now=settlement.now,
            evidence_digest=verdict_digest,
            fields={"guardian_verdict_digest": verdict_digest},
        )
    attempt_id = frame["campaign_attempt_id"]
    if attempt_id is not None:
        # The parent ledger: the campaign attempt and campaign move in this transaction.
        _settle_attempt(
            connection,
            attempt_id,
            cents=cents,
            basis=basis,
            request_digest=settlement.request_digest,
            now=settlement.now,
            on_settled=settlement.on_settled,
        )
    evidence_digest = settlement.absence_evidence_digest or frame["outcome_evidence_digest"]
    release = None
    admitted = frame["admitted_attempt_id"]
    if admitted is not None and basis in RELEASE_BASES and final and evidence_digest:
        release = release_hold(
            connection,
            admitted_attempt_id=admitted,
            frame_id=frame_id,
            basis=basis,
            evidence_digest=evidence_digest,
            now=settlement.now,
        )
    receipt = frames.build_receipt(
        frame,
        cents=cents,
        basis=basis,
        final=final,
        request_digest=settlement.request_digest,
        evidence={
            **settlement.evidence,
            "absence_evidence_digest": settlement.absence_evidence_digest,
            "experiment_release": None
            if release is None
            else {"admitted_attempt_id": admitted, "released_cents": release["released_cents"]},
        },
        estimate_cents=settlement.estimate_cents,
    )
    receipt_digest = canonical_digest(receipt)
    settlement_id = "frame_settlement_" + canonical_digest([frame_id, settlement.request_digest])
    connection.execute(
        """INSERT INTO execution_frame_settlements (frame_id, settlement_id, org_id,
        project_id, campaign_attempt_id, cents, basis, final, request_digest, receipt_digest,
        receipt_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            frame_id,
            settlement_id,
            frame["org_id"],
            frame["project_id"],
            attempt_id,
            cents,
            basis,
            int(final),
            settlement.request_digest,
            receipt_digest,
            json.dumps(receipt, sort_keys=True, separators=(",", ":")),
            settlement.now,
        ),
    )
    frames.note_event(
        connection,
        frame,
        "frame.settled",
        {
            "settlement_id": settlement_id,
            "settled_cents": cents,
            "basis": basis,
            "final": final,
            "receipt_digest": receipt_digest,
            "experiment_release": release is not None,
        },
        actor=settlement.actor,
        now=settlement.now,
    )
    frame = frames.transition(
        connection,
        frame_id,
        settlement.tenant,
        to_state="closed",
        expected_revision=frame["revision"],
        trigger=f"settled:{basis}",
        actor=settlement.actor,
        now=settlement.now,
        evidence_digest=receipt_digest,
        fields={
            "settlement_id": settlement_id,
            "receipt_digest": receipt_digest,
            "closed_at": settlement.now,
        },
        event_type="frame.closed",
    )
    row = connection.execute(
        "SELECT * FROM execution_frame_settlements WHERE frame_id=?", (frame_id,)
    ).fetchone()
    return _stored_frame(frame, row, replayed=False)
