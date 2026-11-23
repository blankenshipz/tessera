"""The delegation section of a campaign receipt (ADR 0122). A leaf module: the receipt builder
imports it, so it must not import the campaign store."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def delegation_receipt_section(connection: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    """The delegation a campaign was approved or gated under, and every delegated decision on it.
    Absent when none was recorded, so existing receipts keep their shape. No terms body: its
    digest and the caps that were checked."""
    rows = connection.execute(
        """SELECT d.id, d.delegation_id, d.kind, d.outcome, d.reason, d.rule_name,
        d.evaluation_json, d.created_at, a.terms_digest, a.expires_at
        FROM delegated_decisions d JOIN approval_delegations a ON a.id=d.delegation_id
        WHERE d.campaign_id=? ORDER BY d.rowid""",
        (campaign_id,),
    ).fetchall()
    if not rows:
        return {}
    return {
        "delegation": {
            "delegations": sorted({row["delegation_id"] for row in rows}),
            "decisions": [
                {
                    "id": row["id"],
                    "delegation_id": row["delegation_id"],
                    "terms_digest": row["terms_digest"],
                    "kind": row["kind"],
                    "outcome": row["outcome"],
                    "reason": row["reason"],
                    "rule": row["rule_name"],
                    "evaluation": json.loads(row["evaluation_json"]),
                    "at": row["created_at"],
                }
                for row in rows
            ],
        }
    }
