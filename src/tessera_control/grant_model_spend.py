"""Model-spend calls charged to an agent authority grant's budget (ADR 0142).

A reasoning call may name the agent authority grant that pays for it. The link is written in the
same transaction as the model-spend reservation, and the grant's spend is *derived* from the
ledger rows it links: an open reservation counts its worst case, a settled call its settled
amount, a released call nothing. So settling, releasing and a person's resolution move the grant's
remaining budget in the very statement that moves the ledger row, with no second write to keep in
step, and a retry cannot erase a real cost. Amounts are integer micro-cents (ADR 0090), so sums
are exact; the grant's own ``reserved_cents`` (execution and delegated budget) is counted beside it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

MICRO = 1_000_000


def create_grant_model_spend_schema(connection: sqlite3.Connection) -> None:
    """Shared by the migration and fresh databases. Additive, idempotent, append-only."""
    connection.execute("""CREATE TABLE IF NOT EXISTS model_spend_grant_links (
        call_id TEXT PRIMARY KEY REFERENCES model_spend_calls(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        grant_id TEXT NOT NULL REFERENCES agent_authority_grants(id),
        created_at TEXT NOT NULL)""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS model_spend_grant_links_grant "
        "ON model_spend_grant_links(grant_id)"
    )
    for action in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS model_spend_grant_links_no_{action.lower()} "
            f"BEFORE {action} ON model_spend_grant_links "
            "BEGIN SELECT RAISE(ABORT, 'model spend grant links are append-only'); END"
        )


def grant_spend_micro(connection: sqlite3.Connection, grant_id: str) -> int:
    """Micro-cents the grant's model calls hold (open) or have spent (settled)."""
    row = connection.execute(
        """SELECT COALESCE(SUM(CASE c.status WHEN 'reserved' THEN c.reserved_micro
        WHEN 'settled' THEN COALESCE(c.settled_micro, c.reserved_micro) ELSE 0 END), 0)
        FROM model_spend_grant_links l JOIN model_spend_calls c ON c.id=l.call_id
        WHERE l.grant_id=?""",
        (grant_id,),
    ).fetchone()
    return int(row[0])


def grant_remaining_micro(connection: sqlite3.Connection, grant: sqlite3.Row) -> int:
    """Budget left after execution/delegation holds and model spend; may be negative."""
    budget = json.loads(grant["bounds_json"])["budget_cents"]
    return (budget - grant["reserved_cents"]) * MICRO - grant_spend_micro(connection, grant["id"])


def cents_view(micro: int) -> int | float:
    """Whole cents as an int (the pre-existing shape), else exact fractional cents."""
    micro = max(0, micro)
    return micro // MICRO if micro % MICRO == 0 else micro / MICRO


def grant_refusal(
    connection: sqlite3.Connection, actor: dict, project_id: str, grant_id: str, worst_micro: int
) -> tuple[dict | None, sqlite3.Row | None]:
    """(refusal, grant row). A grant that is not the caller's own reads as not found (None row,
    refusal None is never returned for it: the caller raises). The refusal is a closed code."""
    row = connection.execute(
        "SELECT * FROM agent_authority_grants WHERE id=? AND org_id=?",
        (grant_id, actor["org_id"]),
    ).fetchone()
    if (
        row is None
        or row["project_id"] != project_id
        or row["subject_credential_id"] != actor["credential_id"]
    ):
        return None, None
    bounds = json.loads(row["bounds_json"])
    expiry = datetime.fromisoformat(bounds["expires_at"].replace("Z", "+00:00"))
    if row["status"] != "active":
        return {"code": "grant_not_active"}, row
    if expiry.tzinfo is None or datetime.now(UTC) >= expiry:
        return {"code": "grant_not_active"}, row
    remaining = grant_remaining_micro(connection, row)
    if worst_micro > remaining:
        return {
            "code": "grant_budget_exceeded",
            "remaining_cents": cents_view(remaining),
            "would_be_cents": worst_micro / MICRO,
        }, row
    return None, row
