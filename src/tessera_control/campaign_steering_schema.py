"""Pause, concurrency and single-stage cancel records for a campaign (ADR 0061).

Additive and idempotent. Rows exist only once an agent or person steers a campaign, so a
campaign nobody steered keeps its row set, its receipt and its digests exactly as before.
"""

from __future__ import annotations

import sqlite3


def create_campaign_steering_schema(connection: sqlite3.Connection) -> None:
    """Shared by the migration and fresh databases so both converge."""
    for statement in (
        # One row per steered campaign. ``paused`` stops NEW stage reservations only.
        # ``max_concurrent_stages`` is a lower-only cap on stages with an unfinished attempt.
        """CREATE TABLE IF NOT EXISTS campaign_steering (
        campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
        paused_by_role TEXT CHECK(paused_by_role IS NULL OR paused_by_role IN ('agent','human')),
        paused_reason TEXT,
        paused_at TEXT,
        pause_count INTEGER NOT NULL DEFAULT 0 CHECK(pause_count >= 0),
        max_concurrent_stages INTEGER CHECK(max_concurrent_stages IS NULL
            OR max_concurrent_stages >= 1),
        updated_at TEXT NOT NULL)""",
        # A request to cancel one stage; the runner unwinds it and the stage ends `cancelled`.
        """CREATE TABLE IF NOT EXISTS campaign_stage_cancels (
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        stage_key TEXT NOT NULL,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 1000),
        requested_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        requested_at TEXT NOT NULL,
        completed_at TEXT,
        PRIMARY KEY(campaign_id, stage_key))""",
    ):
        connection.execute(statement)
