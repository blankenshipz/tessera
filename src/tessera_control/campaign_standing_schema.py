"""Standing-approval tables (ADR 0042): templates, starts and triggers (schema 67)."""

from __future__ import annotations

import sqlite3


def create_standing_schema(connection: sqlite3.Connection) -> None:
    """Idempotent: the standing tables, their guards and the approval column.

    Shared by the v66 -> v67 migration and fresh databases so both converge.
    """
    statements = (
        """CREATE TABLE IF NOT EXISTS campaign_standing_approvals (
        id TEXT PRIMARY KEY CHECK(id GLOB 'standing_*'),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        spec_json TEXT NOT NULL,
        spec_digest TEXT NOT NULL CHECK(length(spec_digest)=64
            AND spec_digest NOT GLOB '*[^0-9a-f]*'),
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_credential_id TEXT REFERENCES credentials(id),
        decision_class TEXT NOT NULL, automation_label TEXT,
        trust_policy_digest TEXT NOT NULL,
        per_start_budget_cap_cents INTEGER NOT NULL
            CHECK(typeof(per_start_budget_cap_cents)='integer'
            AND per_start_budget_cap_cents BETWEEN 0 AND 5000),
        max_starts INTEGER NOT NULL CHECK(max_starts BETWEEN 1 AND 100),
        max_total_budget_cents INTEGER NOT NULL
            CHECK(max_total_budget_cents >= per_start_budget_cap_cents),
        min_interval_seconds INTEGER CHECK(min_interval_seconds IS NULL
            OR min_interval_seconds BETWEEN 60 AND 2592000),
        max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrent BETWEEN 1 AND 4),
        "trigger" TEXT NOT NULL CHECK("trigger" IN ('schedule','follow_on')),
        follow_on_standing_approval_id TEXT REFERENCES campaign_standing_approvals(id)
            CHECK(follow_on_standing_approval_id IS NULL OR follow_on_standing_approval_id<>id),
        retry_max INTEGER NOT NULL DEFAULT 0 CHECK(retry_max BETWEEN 0 AND 5),
        retry_on TEXT NOT NULL DEFAULT 'failed' CHECK(retry_on IN ('failed','failed_or_stopped')),
        not_before TEXT, not_after TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','exhausted','expired','revoked')),
        starts_used INTEGER NOT NULL DEFAULT 0,
        committed_budget_cents INTEGER NOT NULL DEFAULT 0,
        last_start_at TEXT,
        last_refusal_reason TEXT CHECK(last_refusal_reason IS NULL
            OR length(last_refusal_reason) BETWEEN 1 AND 300),
        revoked_by_principal_id TEXT REFERENCES principals(id),
        revoked_at TEXT, revoke_reason TEXT,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(starts_used <= max_starts AND committed_budget_cents <= max_total_budget_cents))""",
        "CREATE INDEX IF NOT EXISTS campaign_standing_by_project "
        "ON campaign_standing_approvals(project_id, status)",
        """CREATE TABLE IF NOT EXISTS campaign_standing_starts (
        standing_approval_id TEXT NOT NULL REFERENCES campaign_standing_approvals(id),
        start_index INTEGER NOT NULL CHECK(start_index >= 1),
        campaign_id TEXT NOT NULL UNIQUE REFERENCES campaigns(id),
        approval_id TEXT NOT NULL UNIQUE REFERENCES campaign_approvals(id),
        kind TEXT NOT NULL CHECK(kind IN ('scheduled','retry','follow_on')),
        trigger_campaign_id TEXT REFERENCES campaigns(id),
        retry_number INTEGER NOT NULL CHECK(retry_number BETWEEN 0 AND 5),
        budget_cap_cents INTEGER NOT NULL CHECK(budget_cap_cents BETWEEN 0 AND 5000),
        created_at TEXT NOT NULL,
        PRIMARY KEY(standing_approval_id, start_index),
        UNIQUE(trigger_campaign_id, kind))""",
        """CREATE TABLE IF NOT EXISTS campaign_standing_triggers (
        trigger_campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        kind TEXT NOT NULL CHECK(kind IN ('retry','follow_on')),
        target_standing_approval_id TEXT NOT NULL REFERENCES campaign_standing_approvals(id),
        retry_number INTEGER NOT NULL CHECK(retry_number BETWEEN 0 AND 5),
        status TEXT NOT NULL CHECK(status IN ('pending','started','abandoned')),
        start_index INTEGER, reason TEXT,
        created_at TEXT NOT NULL, resolved_at TEXT,
        PRIMARY KEY(trigger_campaign_id, kind))""",
        "CREATE INDEX IF NOT EXISTS campaign_standing_triggers_by_target "
        "ON campaign_standing_triggers(target_standing_approval_id, status)",
    )
    for statement in statements:
        connection.execute(statement)
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS campaign_standing_starts_no_{event.lower()}
            BEFORE {event} ON campaign_standing_starts
            BEGIN SELECT RAISE(ABORT,'campaign standing starts are append-only'); END"""
        )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(campaign_approvals)")}
    if "standing_approval_id" not in existing:
        connection.execute("ALTER TABLE campaign_approvals ADD COLUMN standing_approval_id TEXT")
