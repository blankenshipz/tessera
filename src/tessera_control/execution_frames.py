"""Execution frames (ADR 0041): one durable, provider-agnostic record per stage attempt.

A frame owns one attempt's reservation and one state machine:

    requested -> validated -> reserved -> creating -> running
        -> {succeeded | failed | rejected | cancelled | lost} -> settling -> closed

``ambiguous`` is not a state. It is a flag with a reason (``ambiguous_reason``) on an open
frame that marks it as still holding. Provider-outcome ambiguity (``OUTCOME_AMBIGUITY``)
belongs on a ``creating``, ``running`` or ``lost`` frame; ``provider_unregistered`` marks a
frame whose settlement is refused because its provider is not registered, and may sit on
any open frame.

Every function runs in the caller's transaction and never commits: frame state, the
append-only ``execution_frame_events`` row and the ordinary ``events`` row commit together
with whatever ledger write the caller makes. Every read and write is scoped by the tenant
(``org_id``, ``project_id``); a frame of another tenant is ``NotFound``. State and the other
mutable fields change only by compare-and-swap on ``revision``, and SQLite triggers refuse an
identity change, a second write of a set-once field, a ``(from, to)`` pair outside the ADR
table, any change to a ``closed`` row, and any update or delete of an event, settlement or
experiment release row.

Money moves only in ``campaign_settlement.settle_frame``; this module never writes a ledger.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .store import Conflict, NotFound, canonical_digest

STATES = (
    "requested",
    "validated",
    "reserved",
    "creating",
    "running",
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "lost",
    "settling",
    "closed",
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "rejected", "cancelled", "lost"})
# The ADR 0041 section 1 table, and nothing else.
TRANSITIONS = frozenset(
    {
        ("requested", "validated"),
        ("requested", "rejected"),
        ("validated", "reserved"),
        ("reserved", "creating"),
        ("reserved", "cancelled"),
        ("creating", "running"),
        ("creating", "rejected"),
        ("creating", "lost"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "lost"),
        *((state, "settling") for state in sorted(TERMINAL_STATES)),
        ("settling", "closed"),
    }
)
KINDS = ("campaign", "stage", "late_resource")
AMBIGUOUS_STATES = frozenset({"creating", "running", "lost"})
OUTCOME_AMBIGUITY = frozenset({"create_unanswered", "observed_unknown", "lookup_unanswerable"})
PROVIDER_UNREGISTERED = "provider_unregistered"
AMBIGUITY_REASONS = OUTCOME_AMBIGUITY | {PROVIDER_UNREGISTERED}

# Fields that are NULL until first known and then immutable (enforced by trigger).
SET_ONCE_FIELDS = (
    "shape_digest",
    "grant_id",
    "admitted_attempt_id",
    "operation_id",
    "guardian_reservation_id",
    "create_intent_id",
    "provider_handle",
    "create_admitted_at",
    "outcome",
    "outcome_evidence_digest",
    "guardian_verdict_digest",
    "settlement_id",
    "receipt_digest",
    "closed_at",
)
IDENTITY_FIELDS = (
    "frame_id",
    "parent_frame_id",
    "kind",
    "org_id",
    "project_id",
    "provider_id",
    "provider_instance",
    "idempotency_key",
    "campaign_attempt_id",
    "hold_cents",
    "created_at",
)


class FrameRefused(ValueError):
    """A frame operation the state machine does not allow (no state was written)."""


@dataclass(frozen=True)
class Tenant:
    org_id: str
    project_id: str


@dataclass(frozen=True)
class Actor:
    """Who moved the frame: ``runner``, ``guardian_verdict`` or ``human:<principal>``.

    ``principal_id`` attributes the ordinary ``events`` row (it must be a principal)."""

    label: str
    principal_id: str


def stage_frame_id(parent_frame_id: str, stage_key: str, attempt: int) -> str:
    """Deterministic: ``canonical_digest([parent_frame_id, stage_key, attempt])``."""
    return canonical_digest([parent_frame_id, stage_key, attempt])


def campaign_frame_id(campaign_id: str) -> str:
    """The parent reference of a campaign's stage frames (ADR 0041 section 5)."""
    return canonical_digest(["campaign", campaign_id])


def legal(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in TRANSITIONS


def path(from_state: str, to_state: str) -> list[str]:
    """The shortest legal sequence of states after ``from_state`` that ends in ``to_state``."""
    if from_state == to_state:
        return []
    queue: deque[list[str]] = deque([[from_state]])
    seen = {from_state}
    while queue:
        walk = queue.popleft()
        for source, target in sorted(TRANSITIONS):
            if source != walk[-1] or target in seen:
                continue
            if target == to_state:
                return [*walk[1:], target]
            seen.add(target)
            queue.append([*walk, target])
    raise FrameRefused(f"no legal path from {from_state} to {to_state}")


# -- schema ------------------------------------------------------------------------------


def _pairs_sql() -> str:
    return ", ".join(f"'{a}>{b}'" for a, b in sorted(TRANSITIONS))


def create_execution_frame_schema(connection: sqlite3.Connection) -> None:
    """Idempotent: the frame tables and their guard triggers (schema 66)."""
    states = ", ".join(f"'{state}'" for state in STATES)
    kinds = ", ".join(f"'{kind}'" for kind in KINDS)
    money = "typeof({0})='integer' AND {0} BETWEEN 0 AND 9007199254740991"
    statements = [
        f"""CREATE TABLE IF NOT EXISTS execution_frames (
            frame_id TEXT PRIMARY KEY,
            parent_frame_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ({kinds})),
            org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            provider_id TEXT NOT NULL,
            provider_instance TEXT,
            idempotency_key TEXT NOT NULL,
            campaign_attempt_id TEXT UNIQUE,
            hold_cents INTEGER NOT NULL CHECK ({money.format("hold_cents")}),
            created_at TEXT NOT NULL,
            shape_digest TEXT,
            grant_id TEXT,
            admitted_attempt_id TEXT,
            operation_id TEXT,
            guardian_reservation_id TEXT,
            create_intent_id TEXT,
            provider_handle TEXT,
            create_admitted_at TEXT,
            outcome TEXT CHECK (outcome IS NULL OR outcome IN
                ('succeeded','failed','rejected','cancelled','lost')),
            outcome_evidence_digest TEXT,
            guardian_verdict_digest TEXT,
            settlement_id TEXT,
            receipt_digest TEXT,
            closed_at TEXT,
            state TEXT NOT NULL CHECK (state IN ({states})),
            ambiguous_reason TEXT CHECK (ambiguous_reason IS NULL OR state <> 'closed'),
            next_resolve_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS execution_frames_by_project "
        "ON execution_frames(org_id, project_id, state)",
        "CREATE INDEX IF NOT EXISTS execution_frames_by_parent "
        "ON execution_frames(parent_frame_id)",
        f"""CREATE TABLE IF NOT EXISTS execution_frame_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            frame_id TEXT NOT NULL REFERENCES execution_frames(frame_id),
            org_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            from_state TEXT CHECK (from_state IS NULL OR from_state IN ({states})),
            to_state TEXT NOT NULL CHECK (to_state IN ({states})),
            trigger TEXT NOT NULL,
            evidence_digest TEXT,
            ambiguous_reason TEXT,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (frame_id, revision)
        )""",
        f"""CREATE TABLE IF NOT EXISTS execution_frame_settlements (
            frame_id TEXT PRIMARY KEY REFERENCES execution_frames(frame_id),
            settlement_id TEXT NOT NULL UNIQUE,
            org_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            campaign_attempt_id TEXT,
            cents INTEGER NOT NULL CHECK ({money.format("cents")}),
            basis TEXT NOT NULL,
            final INTEGER NOT NULL CHECK (final IN (0, 1)),
            request_digest TEXT NOT NULL,
            receipt_digest TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        # A new frame starts at ``requested``, revision 1, with no outcome yet.
        """CREATE TRIGGER IF NOT EXISTS execution_frames_insert_requested
        BEFORE INSERT ON execution_frames
        WHEN NEW.state <> 'requested' OR NEW.revision <> 1 OR NEW.outcome IS NOT NULL
            OR NEW.settlement_id IS NOT NULL OR NEW.closed_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'a frame starts requested at revision 1'); END""",
        """CREATE TRIGGER IF NOT EXISTS execution_frames_closed_final
        BEFORE UPDATE ON execution_frames WHEN OLD.state = 'closed'
        BEGIN SELECT RAISE(ABORT, 'a closed frame is final'); END""",
        """CREATE TRIGGER IF NOT EXISTS execution_frames_revision_cas
        BEFORE UPDATE ON execution_frames WHEN NEW.revision <> OLD.revision + 1
        BEGIN SELECT RAISE(ABORT, 'frame revision must advance by one'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS execution_frames_transition
        BEFORE UPDATE OF state ON execution_frames
        WHEN NEW.state <> OLD.state AND (OLD.state || '>' || NEW.state) NOT IN ({_pairs_sql()})
        BEGIN SELECT RAISE(ABORT, 'illegal frame transition'); END""",
        """CREATE TRIGGER IF NOT EXISTS execution_frames_close_needs_settlement
        BEFORE UPDATE ON execution_frames
        WHEN NEW.state = 'closed' AND (NEW.settlement_id IS NULL OR NEW.receipt_digest IS NULL
            OR NEW.closed_at IS NULL)
        BEGIN SELECT RAISE(ABORT, 'a frame closes only with a settlement and receipt'); END""",
        "CREATE TRIGGER IF NOT EXISTS execution_frames_identity_write_once "
        "BEFORE UPDATE ON execution_frames WHEN "
        + " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in IDENTITY_FIELDS)
        + " BEGIN SELECT RAISE(ABORT, 'frame identity is write-once'); END",
        "CREATE TRIGGER IF NOT EXISTS execution_frames_set_once "
        "BEFORE UPDATE ON execution_frames WHEN "
        + " OR ".join(
            f"(OLD.{name} IS NOT NULL AND NEW.{name} IS NOT OLD.{name})" for name in SET_ONCE_FIELDS
        )
        + " BEGIN SELECT RAISE(ABORT, 'a set-once frame field cannot change'); END",
        """CREATE TRIGGER IF NOT EXISTS execution_frames_no_delete
        BEFORE DELETE ON execution_frames
        BEGIN SELECT RAISE(ABORT, 'frames are never deleted'); END""",
    ]
    for table in ("execution_frame_events", "execution_frame_settlements"):
        for event in ("UPDATE", "DELETE"):
            statements.append(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()}
                BEFORE {event} ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"""
            )
    for statement in statements:
        connection.execute(statement)


# -- reads -------------------------------------------------------------------------------


def get_frame(connection: sqlite3.Connection, frame_id: str, tenant: Tenant) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM execution_frames WHERE frame_id=? AND org_id=? AND project_id=?",
        (frame_id, tenant.org_id, tenant.project_id),
    ).fetchone()
    if row is None:
        raise NotFound("execution frame not found")
    return dict(row)


def frame_for_attempt(
    connection: sqlite3.Connection, attempt_id: str, tenant: Tenant
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT * FROM execution_frames WHERE campaign_attempt_id=? AND org_id=?
        AND project_id=?""",
        (attempt_id, tenant.org_id, tenant.project_id),
    ).fetchone()
    return None if row is None else dict(row)


def frame_events(
    connection: sqlite3.Connection, frame_id: str, tenant: Tenant
) -> list[dict[str, Any]]:
    get_frame(connection, frame_id, tenant)
    return [
        dict(row)
        for row in connection.execute(
            """SELECT * FROM execution_frame_events WHERE frame_id=? AND org_id=?
            AND project_id=? ORDER BY sequence""",
            (frame_id, tenant.org_id, tenant.project_id),
        )
    ]


def entered(connection: sqlite3.Connection, frame: Mapping[str, Any], state: str) -> bool:
    """Whether the frame ever moved into ``state`` (from its own event log)."""
    return (
        connection.execute(
            "SELECT 1 FROM execution_frame_events WHERE frame_id=? AND to_state=? LIMIT 1",
            (frame["frame_id"], state),
        ).fetchone()
        is not None
    )


# -- writes ------------------------------------------------------------------------------


def _record(
    connection: sqlite3.Connection,
    frame: Mapping[str, Any],
    *,
    revision: int,
    from_state: str | None,
    to_state: str,
    trigger: str,
    evidence_digest: str | None,
    ambiguous_reason: str | None,
    actor: Actor,
    now: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    connection.execute(
        """INSERT INTO execution_frame_events (frame_id, org_id, project_id, revision,
        from_state, to_state, trigger, evidence_digest, ambiguous_reason, actor, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            frame["frame_id"],
            frame["org_id"],
            frame["project_id"],
            revision,
            from_state,
            to_state,
            trigger,
            evidence_digest,
            ambiguous_reason,
            actor.label,
            now,
        ),
    )
    body = {
        "frame_id": frame["frame_id"],
        "kind": frame["kind"],
        "provider_id": frame["provider_id"],
        "campaign_attempt_id": frame["campaign_attempt_id"],
        "from": from_state,
        "to": to_state,
        "trigger": trigger,
        "evidence_digest": evidence_digest,
        "ambiguous_reason": ambiguous_reason,
        "actor": actor.label,
        **(payload or {}),
    }
    connection.execute(
        """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
        actor_principal_id, created_at, correlation_id, causation_id, payload_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            frame["org_id"],
            frame["project_id"],
            None,
            event_type,
            revision,
            actor.principal_id,
            now,
            None,
            None,
            json.dumps(body, separators=(",", ":"), sort_keys=True),
        ),
    )


def note_event(
    connection: sqlite3.Connection,
    frame: Mapping[str, Any],
    event_type: str,
    payload: Mapping[str, Any],
    *,
    actor: Actor,
    now: str,
) -> None:
    """An ordinary ``events`` row about a frame that changes no frame state."""
    body = {
        "frame_id": frame["frame_id"],
        "kind": frame["kind"],
        "provider_id": frame["provider_id"],
        "campaign_attempt_id": frame["campaign_attempt_id"],
        "actor": actor.label,
        **payload,
    }
    connection.execute(
        """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
        actor_principal_id, created_at, correlation_id, causation_id, payload_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            frame["org_id"],
            frame["project_id"],
            None,
            event_type,
            frame["revision"],
            actor.principal_id,
            now,
            None,
            None,
            json.dumps(body, separators=(",", ":"), sort_keys=True),
        ),
    )


def open_frame(
    connection: sqlite3.Connection,
    *,
    frame_id: str,
    tenant: Tenant,
    kind: str,
    provider_id: str,
    idempotency_key: str,
    hold_cents: int,
    actor: Actor,
    now: str,
    parent_frame_id: str | None = None,
    provider_instance: str | None = None,
    campaign_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Insert a ``requested`` frame, or return the same frame again (idempotent).

    A frame id that exists with a different identity (or under another tenant) conflicts."""
    if kind not in KINDS:
        raise FrameRefused(f"unknown frame kind {kind!r}")
    identity = {
        "frame_id": frame_id,
        "parent_frame_id": parent_frame_id,
        "kind": kind,
        "org_id": tenant.org_id,
        "project_id": tenant.project_id,
        "provider_id": provider_id,
        "provider_instance": provider_instance,
        "idempotency_key": idempotency_key,
        "campaign_attempt_id": campaign_attempt_id,
        "hold_cents": hold_cents,
    }
    existing = connection.execute(
        "SELECT * FROM execution_frames WHERE frame_id=?", (frame_id,)
    ).fetchone()
    if existing is not None:
        if any(existing[name] != value for name, value in identity.items()):
            raise Conflict("execution frame exists with a different identity")
        return dict(existing)
    connection.execute(
        f"""INSERT INTO execution_frames ({", ".join(identity)}, created_at, state, revision,
        updated_at) VALUES ({", ".join("?" for _ in identity)}, ?, 'requested', 1, ?)""",
        (*identity.values(), now, now),
    )
    frame = get_frame(connection, frame_id, tenant)
    _record(
        connection,
        frame,
        revision=1,
        from_state=None,
        to_state="requested",
        trigger="open",
        evidence_digest=None,
        ambiguous_reason=None,
        actor=actor,
        now=now,
        event_type="frame.state_changed",
    )
    return frame


def transition(
    connection: sqlite3.Connection,
    frame_id: str,
    tenant: Tenant,
    *,
    to_state: str,
    expected_revision: int,
    trigger: str,
    actor: Actor,
    now: str,
    evidence_digest: str | None = None,
    fields: Mapping[str, Any] | None = None,
    event_type: str = "frame.state_changed",
) -> dict[str, Any]:
    """Move one frame by one legal pair, by compare-and-swap on ``revision``.

    ``fields`` may set NULL set-once fields (a value equal to the stored one is a no-op).
    Leaving for a terminal state clears provider-outcome ambiguity (the outcome is known);
    closing clears every flag."""
    frame = get_frame(connection, frame_id, tenant)
    if frame["revision"] != expected_revision:
        raise Conflict("execution frame revision is no longer current")
    if not legal(frame["state"], to_state):
        raise FrameRefused(f"illegal frame transition {frame['state']} -> {to_state}")
    updates: dict[str, Any] = {}
    for name, value in (fields or {}).items():
        if name not in SET_ONCE_FIELDS:
            raise FrameRefused(f"{name} is not a set-once frame field")
        if value is None or frame[name] == value:
            continue
        if frame[name] is not None:
            raise FrameRefused(f"{name} is already set")
        updates[name] = value
    reason = frame["ambiguous_reason"]
    if to_state == "closed" or (
        to_state in TERMINAL_STATES and reason in OUTCOME_AMBIGUITY and to_state != "lost"
    ):
        reason = None
    assignments = "".join(f"{name}=?, " for name in updates)
    claimed = connection.execute(
        f"""UPDATE execution_frames SET {assignments}state=?, ambiguous_reason=?,
        revision=revision+1, updated_at=? WHERE frame_id=? AND revision=? AND org_id=?
        AND project_id=?""",
        (
            *updates.values(),
            to_state,
            reason,
            now,
            frame_id,
            expected_revision,
            tenant.org_id,
            tenant.project_id,
        ),
    )
    if claimed.rowcount != 1:
        raise Conflict("execution frame changed concurrently")
    _record(
        connection,
        frame,
        revision=expected_revision + 1,
        from_state=frame["state"],
        to_state=to_state,
        trigger=trigger,
        evidence_digest=evidence_digest,
        ambiguous_reason=reason,
        actor=actor,
        now=now,
        event_type=event_type,
    )
    return get_frame(connection, frame_id, tenant)


def record_guardian_verdict(
    connection: sqlite3.Connection,
    frame_id: str,
    tenant: Tenant,
    *,
    verdict_digest: str,
    actor: Actor,
    now: str,
) -> dict[str, Any]:
    """Record that control read the guardian verdict for an open frame (state unchanged).

    Set once: the same digest again is a no-op, a different one is refused. A frame whose
    outcome is ambiguous settles on an estimate only after this (``settle_frame``)."""
    frame = get_frame(connection, frame_id, tenant)
    if frame["guardian_verdict_digest"] == verdict_digest:
        return frame
    if frame["guardian_verdict_digest"] is not None:
        raise FrameRefused("guardian_verdict_digest is already set")
    if frame["state"] in {"settling", "closed"}:
        raise FrameRefused(f"a {frame['state']} frame takes no guardian verdict")
    claimed = connection.execute(
        """UPDATE execution_frames SET guardian_verdict_digest=?, revision=revision+1,
        updated_at=? WHERE frame_id=? AND revision=? AND org_id=? AND project_id=?""",
        (
            verdict_digest,
            now,
            frame_id,
            frame["revision"],
            tenant.org_id,
            tenant.project_id,
        ),
    )
    if claimed.rowcount != 1:
        raise Conflict("execution frame changed concurrently")
    _record(
        connection,
        frame,
        revision=frame["revision"] + 1,
        from_state=frame["state"],
        to_state=frame["state"],
        trigger="guardian_verdict_read",
        evidence_digest=verdict_digest,
        ambiguous_reason=frame["ambiguous_reason"],
        actor=actor,
        now=now,
        event_type="frame.guardian_verdict",
    )
    return get_frame(connection, frame_id, tenant)


def flag_ambiguous(
    connection: sqlite3.Connection,
    frame_id: str,
    tenant: Tenant,
    *,
    reason: str,
    expected_revision: int,
    actor: Actor,
    now: str,
    evidence_digest: str | None = None,
) -> dict[str, Any]:
    """Set the ambiguity flag (state unchanged). Setting the same reason again is a no-op."""
    if reason not in AMBIGUITY_REASONS:
        raise FrameRefused(f"unknown ambiguity reason {reason!r}")
    frame = get_frame(connection, frame_id, tenant)
    if frame["ambiguous_reason"] == reason:
        return frame
    if frame["revision"] != expected_revision:
        raise Conflict("execution frame revision is no longer current")
    if frame["state"] == "closed" or (
        reason in OUTCOME_AMBIGUITY and frame["state"] not in AMBIGUOUS_STATES
    ):
        raise FrameRefused(f"{reason} cannot flag a {frame['state']} frame")
    claimed = connection.execute(
        """UPDATE execution_frames SET ambiguous_reason=?, revision=revision+1, updated_at=?
        WHERE frame_id=? AND revision=? AND org_id=? AND project_id=?""",
        (reason, now, frame_id, expected_revision, tenant.org_id, tenant.project_id),
    )
    if claimed.rowcount != 1:
        raise Conflict("execution frame changed concurrently")
    _record(
        connection,
        frame,
        revision=expected_revision + 1,
        from_state=frame["state"],
        to_state=frame["state"],
        trigger=f"ambiguous:{reason}",
        evidence_digest=evidence_digest,
        ambiguous_reason=reason,
        actor=actor,
        now=now,
        event_type="frame.ambiguous",
    )
    return get_frame(connection, frame_id, tenant)


def drive_to(
    connection: sqlite3.Connection,
    frame_id: str,
    tenant: Tenant,
    to_state: str,
    *,
    trigger: str,
    actor: Actor,
    now: str,
    evidence_digest: str | None = None,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Walk the shortest legal path to ``to_state``; each step is its own CAS and event.

    Set-once ``fields`` are written on the first step that can carry them."""
    frame = get_frame(connection, frame_id, tenant)
    pending = dict(fields or {})
    for state in path(frame["state"], to_state):
        step_fields = dict(pending)
        if state not in TERMINAL_STATES:
            step_fields.pop("outcome", None)
            step_fields.pop("outcome_evidence_digest", None)
        frame = transition(
            connection,
            frame_id,
            tenant,
            to_state=state,
            expected_revision=frame["revision"],
            trigger=trigger,
            actor=actor,
            now=now,
            evidence_digest=evidence_digest if state == to_state else None,
            fields=step_fields,
        )
        for name in step_fields:
            pending.pop(name, None)
    return frame


def build_receipt(
    frame: Mapping[str, Any],
    *,
    cents: int,
    basis: str,
    final: bool,
    request_digest: str,
    evidence: Mapping[str, Any],
    estimate_cents: int | None,
) -> dict[str, Any]:
    """The immutable frame receipt (ADR 0041 section 2); its digest is stored on close."""
    return {
        "schema": "tessera.execution-frame-receipt.v1",
        "frame_id": frame["frame_id"],
        "parent_frame_id": frame["parent_frame_id"],
        "kind": frame["kind"],
        "org_id": frame["org_id"],
        "project_id": frame["project_id"],
        "provider_id": frame["provider_id"],
        "provider_instance": frame["provider_instance"],
        "idempotency_key": frame["idempotency_key"],
        "campaign_attempt_id": frame["campaign_attempt_id"],
        "provider_handle": frame["provider_handle"],
        "outcome": frame["outcome"],
        "evidence": {
            "outcome_evidence_digest": frame["outcome_evidence_digest"],
            "guardian_verdict_digest": frame["guardian_verdict_digest"],
            **{key: value for key, value in sorted(evidence.items()) if value is not None},
        },
        "hold_cents": frame["hold_cents"],
        "settled_cents": cents,
        "basis": basis,
        "final": final,
        "request_digest": request_digest,
        "estimate": None
        if estimate_cents is None
        else {"cents": estimate_cents, "label": "estimate_not_invoice"},
    }
