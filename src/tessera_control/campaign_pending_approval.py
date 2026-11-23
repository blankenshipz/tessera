"""What a campaign is waiting for a person to decide (audit G-11).

A read-only derivation from recorded state, computed on each read: nothing is stored, so an item
clears as soon as recorded state no longer shows it awaited (the campaign leaves ``submitted`` or
``paused_delegation_lapsed``, the review's decision request is no longer ``pending``, or the
campaign reaches a terminal status). Only the live campaign view (``GET /campaigns/{id}``,
``tessera_get_campaign``) carries the list, as ``pending_human_approval``; campaign receipts do not
record it (the receipt's ``scientific_verdict`` value of the same name is unrelated). Each item
names what is awaited and, for a review, the stage, attempt and decision request id; never a
worker path or a secret.

- ``campaign_approval``: the campaign is ``submitted`` and waits for a person to approve its spec
  digest.
- ``campaign_reapproval``: the campaign is ``paused_delegation_lapsed`` and waits for a fresh
  approval of its spec digest.
- ``salad_review``: in a non-terminal campaign, a Salad attempt in phase ``awaiting_review`` whose
  ``plan`` or ``source_use`` decision request is still ``pending`` and not past its
  ``expires_at`` (a request the expiry sweep has not yet marked expired is already cleared here;
  an unparsable ``expires_at`` fails closed to listing it).
  A pre-approval clears an item only by resolving that request: this view reads the request's own
  status and never looks at pre-approvals. For a campaign approved under an owner-signed
  delegation (ADR 0150), an item also carries ``delegation_declined``: the stable reason codes
  of the latest recorded decline (why the delegation did not clear it); absent otherwise.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from .store import _parse_utc

_REVIEW_COLUMNS = (("plan", "plan_decision_id"), ("source_use", "source_decision_id"))


def _still_open(expires_at: Any, now: datetime) -> bool:
    """A pending request past its expiry can no longer be approved (``resolve_decision`` refuses
    it) even before ``expire_decisions`` records it, so it is not awaited. An expiry that cannot
    be read as a timestamp with an offset fails closed: the request stays listed for a person."""
    try:
        return _parse_utc(str(expires_at)) > now
    except (TypeError, ValueError):
        return True


def pending_human_approval(
    connection: sqlite3.Connection, campaign: sqlite3.Row | dict[str, Any]
) -> list[dict[str, Any]]:
    """The decisions still awaited from a person for this campaign, oldest first."""
    items: list[dict[str, Any]] = []
    status = campaign["status"]
    if status == "submitted":
        items.append(
            {
                "kind": "campaign_approval",
                "awaiting": "a person's approval of this spec digest",
                "spec_digest": campaign["spec_digest"],
            }
        )
    elif status == "paused_delegation_lapsed":
        items.append(
            {
                "kind": "campaign_reapproval",
                "awaiting": "a person's re-approval (the approval lapsed)",
                "spec_digest": campaign["spec_digest"],
            }
        )
    if status in {"cancelled", "succeeded", "stopped", "failed"}:
        return items
    now = datetime.now(UTC)
    delegated = (
        connection.execute(
            """SELECT 1 FROM campaign_approvals WHERE campaign_id=? AND status='active'
            AND delegation_id IS NOT NULL""",
            (campaign["id"],),
        ).fetchone()
        is not None
    )
    rows = connection.execute(
        """SELECT a.stage_key, a.attempt, s.plan_decision_id, s.source_decision_id
        FROM campaign_salad_attempts s JOIN campaign_stage_attempts a ON a.id = s.attempt_id
        WHERE a.campaign_id=? AND s.phase='awaiting_review'
        ORDER BY a.stage_key, a.attempt""",
        (campaign["id"],),
    ).fetchall()
    for row in rows:
        for review, column in _REVIEW_COLUMNS:
            decision_id = row[column]
            if decision_id is None:
                continue
            pending = connection.execute(
                "SELECT expires_at FROM decision_requests WHERE id=? AND status='pending'",
                (decision_id,),
            ).fetchone()
            if pending is not None and _still_open(pending["expires_at"], now):
                declined = (
                    _delegation_declined(connection, campaign["id"], row["stage_key"], decision_id)
                    if delegated
                    else None
                )
                items.append(
                    {
                        "kind": "salad_review",
                        "stage_key": row["stage_key"],
                        "attempt": row["attempt"],
                        "review": review,
                        "awaiting": f"a person's decision on the {review.replace('_', ' ')} review",
                        "decision_request_id": decision_id,
                        **({"delegation_declined": declined} if declined else {}),
                    }
                )
    return items


def _delegation_declined(
    connection: sqlite3.Connection, campaign_id: str, stage_key: str, decision_id: str
) -> list[str] | None:
    """ADR 0150: why the owner-signed delegation this campaign was approved under did not
    clear this review (stable reason codes, never an id), from the latest decline recorded for
    the request or for the stage's delegated pre-approval; None when none was recorded."""
    found = connection.execute(
        """SELECT json_extract(payload_json, '$.reason') AS reason FROM events
        WHERE event_type='campaign.preapproval_declined'
        AND json_extract(payload_json, '$.campaign_id')=?
        AND json_extract(payload_json, '$.stage_key')=?
        AND (json_extract(payload_json, '$.subject_id')=?
             OR json_extract(payload_json, '$.kind')='delegated_review')
        ORDER BY cursor DESC LIMIT 1""",
        (campaign_id, stage_key, decision_id),
    ).fetchone()
    if found is None or not found["reason"]:
        return None
    return sorted(set(str(found["reason"]).split(",")))
