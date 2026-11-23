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
  ``plan`` or ``source_use`` decision request is still ``pending``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

_REVIEW_COLUMNS = (("plan", "plan_decision_id"), ("source_use", "source_decision_id"))


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
                "SELECT 1 FROM decision_requests WHERE id=? AND status='pending'",
                (decision_id,),
            ).fetchone()
            if pending is not None:
                items.append(
                    {
                        "kind": "salad_review",
                        "stage_key": row["stage_key"],
                        "attempt": row["attempt"],
                        "review": review,
                        "awaiting": f"a person's decision on the {review.replace('_', ' ')} review",
                        "decision_request_id": decision_id,
                    }
                )
    return items
