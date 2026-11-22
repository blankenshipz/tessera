"""Campaign gate tables (ADR 0045): human gate decisions, unlock evaluations and
paired-delta replicate groups. Additive and idempotent (schema 69)."""

from __future__ import annotations

import sqlite3


def create_campaign_gate_schema(connection: sqlite3.Connection) -> None:
    """Shared by the migration and fresh databases so both converge."""
    statements = (
        # A human's recorded direction on a finished campaign. Append-only: the latest row
        # of a campaign is effective, so a later no_go retracts an earlier go.
        """CREATE TABLE IF NOT EXISTS campaign_gate_decisions (
        id TEXT PRIMARY KEY CHECK(id GLOB 'gatedec_*'),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        campaign_spec_digest TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('go','no_go')),
        note TEXT NOT NULL CHECK(length(note) BETWEEN 1 AND 1000),
        decided_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        decided_credential_id TEXT REFERENCES credentials(id),
        decision_class TEXT NOT NULL, automation_label TEXT,
        created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS campaign_gate_decisions_by_campaign "
        "ON campaign_gate_decisions(campaign_id)",
        # Why a campaign was admitted: one row per satisfied requirement.
        """CREATE TABLE IF NOT EXISTS campaign_unlock_evaluations (
        id TEXT PRIMARY KEY CHECK(id GLOB 'unlock_*'),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        approval_id TEXT NOT NULL REFERENCES campaign_approvals(id),
        standing_approval_id TEXT REFERENCES campaign_standing_approvals(id),
        requirement_index INTEGER NOT NULL CHECK(requirement_index >= 0),
        requirement_digest TEXT NOT NULL,
        named_campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        basis_kind TEXT NOT NULL CHECK(basis_kind IN ('human_gate_go','bound_outcome')),
        gate_decision_id TEXT REFERENCES campaign_gate_decisions(id),
        outcome TEXT CHECK(outcome IS NULL OR outcome IN ('pass','fail')),
        result_digest TEXT,
        evaluated_at TEXT NOT NULL,
        UNIQUE(approval_id, requirement_index))""",
        "CREATE INDEX IF NOT EXISTS campaign_unlock_evaluations_by_campaign "
        "ON campaign_unlock_evaluations(campaign_id)",
        """CREATE TABLE IF NOT EXISTS gate_paired_groups (
        id TEXT PRIMARY KEY CHECK(id GLOB 'gate_paired_*'),
        project_id TEXT NOT NULL REFERENCES projects(id),
        arm_a_variant_id TEXT NOT NULL, arm_b_variant_id TEXT NOT NULL,
        metric TEXT NOT NULL, unit TEXT NOT NULL,
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        partition TEXT NOT NULL, membership_digest TEXT NOT NULL,
        ci_method TEXT NOT NULL, statistics_version TEXT NOT NULL,
        n INTEGER NOT NULL CHECK(n >= 2),
        mean REAL NOT NULL, std REAL NOT NULL, ci_low REAL NOT NULL, ci_high REAL NOT NULL,
        min_sample_count INTEGER NOT NULL,
        sources_json TEXT NOT NULL, excluded_attempts_json TEXT NOT NULL,
        result_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id))""",
        """CREATE TABLE IF NOT EXISTS gate_paired_members (
        group_id TEXT NOT NULL REFERENCES gate_paired_groups(id),
        seed INTEGER NOT NULL,
        record_a_id TEXT NOT NULL REFERENCES gate_metric_records(id),
        record_b_id TEXT NOT NULL REFERENCES gate_metric_records(id),
        delta REAL NOT NULL,
        PRIMARY KEY(group_id, seed))""",
    )
    for statement in statements:
        connection.execute(statement)
    for table in (
        "campaign_gate_decisions",
        "campaign_unlock_evaluations",
        "gate_paired_groups",
        "gate_paired_members",
    ):
        for event in ("UPDATE", "DELETE"):
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()}
                BEFORE {event} ON {table}
                BEGIN SELECT RAISE(ABORT,'{table} rows are append-only'); END"""
            )
    existing = {
        row[1] for row in connection.execute("PRAGMA table_info(campaign_standing_approvals)")
    }
    if "unlock_json" not in existing:
        connection.execute("ALTER TABLE campaign_standing_approvals ADD COLUMN unlock_json TEXT")
