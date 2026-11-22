"""Gate evidence records: verdicts bound to what actually ran.

A gate observation references a platform-computed replicate group, whose members
are typed per-seed metric records, each bound to one stored measurement of one
admitted attempt that was pre-registered against a locked, leakage-audited split.
Nothing here imputes a value, grants execution, or certifies scientific validity.
"""

from __future__ import annotations

import sqlite3

from .gate_evidence_metrics import GateMetricMixin
from .gate_evidence_paired import PairedGroupMixin
from .gate_evidence_programs import ResearchProgramMixin
from .gate_evidence_splits import SplitLockMixin
from .research import ResearchStore

_PROGRAM_OUTCOMES_TABLE = """CREATE TABLE IF NOT EXISTS gate_research_program_outcomes (
        program_id TEXT NOT NULL REFERENCES gate_research_programs(id),
        outcome TEXT NOT NULL CHECK (length(outcome) BETWEEN 1 AND 64),
        study_id TEXT NOT NULL REFERENCES research_studies(id),
        title TEXT NOT NULL, created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(program_id, outcome), UNIQUE(program_id, study_id))"""

_HEX64 = "length({0})=64 AND {0} NOT GLOB '*[^0-9a-f]*'"


def ensure_measurement_source_column(connection: sqlite3.Connection) -> None:
    """Add a server-set provenance column to both measurement tables, once.

    Existing rows are classified only from durable evidence: a row is `derived`
    when its creating transaction emitted `experiment.measurement_derived`; every
    other row is `human_entered`. An in-value key never decides provenance.
    """
    for table in ("research_attempt_measurements", "research_admitted_attempt_measurements"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns or "source" in columns:
            continue
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'human_entered' "
            "CHECK (source IN ('human_entered','derived'))"
        )
        connection.execute(
            f"""UPDATE {table} SET source='derived' WHERE id IN (
            SELECT json_extract(payload_json,'$.measurement_id') FROM events
            WHERE event_type='experiment.measurement_derived')"""
        )


def ensure_binding_seed_column(connection: sqlite3.Connection) -> None:
    """Add the pre-registered seed to split bindings, once; old rows keep NULL."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(gate_attempt_split_bindings)")
    }
    if columns and "seed" not in columns:
        connection.execute(
            "ALTER TABLE gate_attempt_split_bindings ADD COLUMN seed INTEGER "
            "CHECK (seed IS NULL OR seed >= 0)"
        )


def ensure_binding_metrics_column(connection: sqlite3.Connection) -> None:
    """Add pre-registered metric declarations to split bindings, once; old rows keep NULL."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(gate_attempt_split_bindings)")
    }
    if columns and "metrics_json" not in columns:
        connection.execute("ALTER TABLE gate_attempt_split_bindings ADD COLUMN metrics_json TEXT")


def ensure_plan_declared_outcome_ids(connection: sqlite3.Connection) -> None:
    """Drop the experiment-specific enumerated-outcome CHECK from program outcomes, once.

    Rows are copied unchanged; only the platform constraint is generalized.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='gate_research_program_outcomes'"
    ).fetchone()
    if row is None or "outcome IN (" not in row[0]:
        return
    connection.execute(
        "ALTER TABLE gate_research_program_outcomes RENAME TO gate_research_program_outcomes_v59"
    )
    connection.execute(_PROGRAM_OUTCOMES_TABLE)
    connection.execute(
        "INSERT INTO gate_research_program_outcomes "
        "SELECT * FROM gate_research_program_outcomes_v59"
    )
    connection.execute("DROP TABLE gate_research_program_outcomes_v59")


def create_gate_evidence_schema(connection: sqlite3.Connection) -> None:
    """Install empty gate-evidence records; never infer locks, metrics or audits."""
    ensure_measurement_source_column(connection)
    statements = (
        f"""CREATE TABLE IF NOT EXISTS gate_split_locks (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL, dataset_snapshot_id TEXT REFERENCES dataset_snapshots(id),
        membership_digest TEXT NOT NULL CHECK ({_HEX64.format("membership_digest")}),
        partitions_json TEXT NOT NULL,
        member_count INTEGER NOT NULL CHECK (member_count >= 1),
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(project_id, membership_digest))""",
        """CREATE TABLE IF NOT EXISTS gate_split_lock_members (
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        partition TEXT NOT NULL, member_key TEXT NOT NULL, group_key TEXT,
        PRIMARY KEY(lock_id, partition, member_key))""",
        "CREATE INDEX IF NOT EXISTS gate_split_lock_members_by_key "
        "ON gate_split_lock_members(lock_id, member_key)",
        "CREATE INDEX IF NOT EXISTS gate_split_lock_members_by_group "
        "ON gate_split_lock_members(lock_id, group_key)",
        f"""CREATE TABLE IF NOT EXISTS gate_leakage_audits (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        kind TEXT NOT NULL CHECK (kind IN ('within_lock','cross_set')),
        reference_lock_id TEXT REFERENCES gate_split_locks(id),
        eval_partitions_json TEXT NOT NULL, reference_partitions_json TEXT NOT NULL,
        key_overlap_count INTEGER NOT NULL CHECK (key_overlap_count >= 0),
        group_overlap_count INTEGER NOT NULL CHECK (group_overlap_count >= 0),
        overlap_sample_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('clean','overlap_detected')),
        audit_digest TEXT NOT NULL CHECK ({_HEX64.format("audit_digest")}),
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK ((kind='within_lock') = (reference_lock_id IS NULL)),
        CHECK ((status='clean') = (key_overlap_count=0 AND group_overlap_count=0)))""",
        "CREATE INDEX IF NOT EXISTS gate_leakage_audits_by_lock "
        "ON gate_leakage_audits(lock_id, created_at, id)",
        f"""CREATE TABLE IF NOT EXISTS gate_attempt_split_bindings (
        attempt_id TEXT PRIMARY KEY REFERENCES research_experiment_admitted_attempts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        membership_digest TEXT NOT NULL CHECK ({_HEX64.format("membership_digest")}),
        attempt_state_at_binding TEXT NOT NULL CHECK (attempt_state_at_binding='admitted'),
        bound_at TEXT NOT NULL,
        bound_by_principal_id TEXT NOT NULL REFERENCES principals(id))""",
        f"""CREATE TABLE IF NOT EXISTS gate_metric_records (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        attempt_id TEXT NOT NULL
            REFERENCES gate_attempt_split_bindings(attempt_id),
        measurement_id TEXT NOT NULL UNIQUE
            REFERENCES research_admitted_attempt_measurements(id),
        measurement_source TEXT NOT NULL
            CHECK (measurement_source IN ('human_entered','derived')),
        metric TEXT NOT NULL, unit TEXT NOT NULL,
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        partition TEXT NOT NULL,
        membership_digest TEXT NOT NULL CHECK ({_HEX64.format("membership_digest")}),
        sample_count INTEGER NOT NULL CHECK (sample_count >= 1),
        seed INTEGER NOT NULL CHECK (seed >= 0),
        checkpoint_sha TEXT CHECK (checkpoint_sha IS NULL OR ({_HEX64.format("checkpoint_sha")})),
        value REAL NOT NULL,
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(attempt_id, metric, partition),
        UNIQUE(variant_id, lock_id, metric, partition, seed))""",
        "CREATE INDEX IF NOT EXISTS gate_metric_records_by_series "
        "ON gate_metric_records(variant_id, lock_id, metric, partition, seed)",
        f"""CREATE TABLE IF NOT EXISTS gate_replicate_groups (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        metric TEXT NOT NULL, unit TEXT NOT NULL,
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        partition TEXT NOT NULL,
        membership_digest TEXT NOT NULL CHECK ({_HEX64.format("membership_digest")}),
        ci_method TEXT NOT NULL CHECK (ci_method IN ('t_interval_95','percentile_bootstrap_95')),
        statistics_version INTEGER NOT NULL CHECK (statistics_version=1),
        n INTEGER NOT NULL CHECK (n >= 2),
        mean REAL NOT NULL, std REAL NOT NULL, ci_low REAL NOT NULL, ci_high REAL NOT NULL,
        min_sample_count INTEGER NOT NULL CHECK (min_sample_count >= 1),
        sources_json TEXT NOT NULL, excluded_attempts_json TEXT NOT NULL,
        result_digest TEXT NOT NULL CHECK ({_HEX64.format("result_digest")}),
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK (ci_low <= mean AND mean <= ci_high))""",
        """CREATE TABLE IF NOT EXISTS gate_replicate_members (
        group_id TEXT NOT NULL REFERENCES gate_replicate_groups(id),
        metric_record_id TEXT NOT NULL REFERENCES gate_metric_records(id),
        PRIMARY KEY(group_id, metric_record_id))""",
        """CREATE TABLE IF NOT EXISTS gate_research_programs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL, objective TEXT NOT NULL, created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(project_id, name))""",
        _PROGRAM_OUTCOMES_TABLE,
    )
    for statement in statements:
        connection.execute(statement)
    ensure_binding_seed_column(connection)
    ensure_binding_metrics_column(connection)
    ensure_plan_declared_outcome_ids(connection)


class GateEvidenceStore(
    SplitLockMixin, GateMetricMixin, PairedGroupMixin, ResearchProgramMixin, ResearchStore
):
    """Tenant-scoped, human-authored, immutable gate-evidence records."""
