"""Explicit, transactional SQLite schema migrations for the local control plane."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

CAMPAIGN_SUBMISSIONS_SCHEMA_VERSION = 76
# Literal: must equal the highest registered constant (OUTPUT_LOOKUP_INDEXES_SCHEMA_VERSION, below).
LATEST_SCHEMA_VERSION = 92
LOCAL_ORG_ID = "org_local"


def canonical_digest(payload: dict[str, Any]) -> str:
    """Import lazily to avoid a persistence-module import cycle."""
    from .store import canonical_digest as digest

    return digest(payload)


def plan_payload(
    title: str,
    purpose: str,
    budget_cents: int,
    max_duration_seconds: int,
    execution_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "budget_cents": budget_cents,
        "execution_spec": execution_spec,
        "max_duration_seconds": max_duration_seconds,
        "purpose": purpose,
        "title": title,
    }


def plan_digest(
    title: str,
    purpose: str,
    budget_cents: int,
    max_duration_seconds: int,
    execution_spec: dict[str, Any] | None,
) -> str:
    return canonical_digest(
        plan_payload(title, purpose, budget_cents, max_duration_seconds, execution_spec)
    )


def _schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT schema_version FROM schema_meta WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise ValueError("Control-plane database has no supported schema.") from error
    if row is None or not isinstance(row[0], int):
        raise ValueError("Control-plane database has no supported schema.")
    return row[0]


def _creator_for_task(connection: sqlite3.Connection, task_id: str) -> str | None:
    row = connection.execute(
        """SELECT actor_principal_id FROM events
        WHERE task_id = ? AND event_type = 'task.created' ORDER BY cursor LIMIT 1""",
        (task_id,),
    ).fetchone()
    return row[0] if row is not None else None


def _approver_for_task(connection: sqlite3.Connection, task_id: str) -> tuple[str, str] | None:
    row = connection.execute(
        """SELECT actor_principal_id, created_at FROM events
        WHERE task_id = ? AND event_type = 'task.approved' ORDER BY cursor DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Add immutable plan revisions without altering existing v1 rows or events."""
    connection.execute("ALTER TABLE tasks ADD COLUMN current_plan_digest TEXT")
    connection.execute(
        """CREATE TABLE task_revisions (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        revision INTEGER NOT NULL,
        title TEXT NOT NULL,
        purpose TEXT NOT NULL,
        budget_cents INTEGER NOT NULL CHECK (budget_cents >= 0),
        max_duration_seconds INTEGER NOT NULL CHECK (max_duration_seconds BETWEEN 1 AND 86400),
        execution_spec_json TEXT,
        plan_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        actor_principal_id TEXT REFERENCES principals(id),
        PRIMARY KEY (task_id, revision)
        )"""
    )
    connection.execute("CREATE INDEX task_revisions_by_task ON task_revisions(task_id, revision)")
    connection.execute(
        """CREATE TABLE task_approvals (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        plan_revision INTEGER NOT NULL,
        plan_digest TEXT NOT NULL,
        approving_principal_id TEXT NOT NULL REFERENCES principals(id),
        approved_at TEXT NOT NULL,
        PRIMARY KEY (task_id, plan_revision)
        )"""
    )

    rows = connection.execute(
        """SELECT id, title, purpose, budget_cents, max_duration_seconds, status,
        revision, created_at, updated_at FROM tasks"""
    ).fetchall()
    for row in rows:
        task_id, title, purpose, budget, duration, status, revision, created_at, updated_at = row
        digest = plan_digest(title, purpose, budget, duration, None)
        plan_revision = revision - 1 if status == "approved" and revision > 1 else revision
        connection.execute(
            "UPDATE tasks SET current_plan_digest = ? WHERE id = ?", (digest, task_id)
        )
        connection.execute(
            """INSERT INTO task_revisions
            (task_id, revision, title, purpose, budget_cents, max_duration_seconds,
             execution_spec_json, plan_digest, created_at, actor_principal_id)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                task_id,
                plan_revision,
                title,
                purpose,
                budget,
                duration,
                digest,
                created_at,
                _creator_for_task(connection, task_id),
            ),
        )
        if status == "approved":
            approval = _approver_for_task(connection, task_id)
            if approval is not None:
                principal_id, approved_at = approval
                connection.execute(
                    """INSERT INTO task_approvals
                    (task_id, plan_revision, plan_digest, approving_principal_id, approved_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (task_id, plan_revision, digest, principal_id, approved_at or updated_at),
                )
    connection.execute("UPDATE schema_meta SET schema_version = 2 WHERE singleton = 1")


def migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Add credentials while retaining one unambiguous v1 local bootstrap owner.

    Version 2 had no supported identity-issuance path: the sole human in the
    local organization is therefore the v1 bootstrap owner.  More than one
    active local human is ambiguous and must be reconciled before migration.
    Other legacy principals are revoked and must enroll again with bounded
    credential metadata; assigning them an unscoped, non-expiring credential
    would broaden their authority.
    """
    owners = connection.execute(
        """SELECT id, created_at FROM principals
        WHERE org_id = ? AND role = 'human' AND revoked_at IS NULL
        ORDER BY created_at, id""",
        (LOCAL_ORG_ID,),
    ).fetchall()
    if len(owners) != 1:
        raise ValueError("Cannot identify one unambiguous local bootstrap owner for migration.")
    owner_id, owner_created_at = owners[0]
    connection.execute(
        """CREATE TABLE credentials (
        id TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id),
        kind TEXT NOT NULL CHECK (kind IN ('local_owner', 'human_session', 'agent', 'worker')),
        audience TEXT NOT NULL CHECK (audience IN ('control', 'worker')),
        project_id TEXT REFERENCES projects(id),
        parent_id TEXT REFERENCES credentials(id),
        expires_at TEXT,
        revoked_at TEXT,
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT REFERENCES principals(id)
        )"""
    )
    connection.execute("CREATE INDEX credentials_by_parent ON credentials(parent_id)")
    connection.execute(
        """INSERT INTO credentials
        (id, principal_id, kind, audience, project_id, parent_id, expires_at,
         revoked_at, created_at, created_by_principal_id)
        VALUES (?, ?, 'local_owner', 'control', NULL, NULL, NULL, NULL, ?, ?)""",
        (f"credential_legacy_{owner_id}", owner_id, owner_created_at, owner_id),
    )
    connection.execute(
        """UPDATE principals SET revoked_at = created_at
        WHERE id != ? AND revoked_at IS NULL""",
        (owner_id,),
    )
    connection.execute("UPDATE schema_meta SET schema_version = 3 WHERE singleton = 1")


def migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Install research and interaction records without changing v3 records."""
    # These imports must stay local: both services layer operations on Store,
    # which imports this migration module to obtain the latest schema version.
    from .interaction import create_interaction_schema
    from .research import create_research_schema

    create_research_schema(connection)
    create_interaction_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 4 WHERE singleton = 1")


def migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    from .snapshots import create_snapshot_schema

    create_snapshot_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 5 WHERE singleton = 1")


def migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
    from .execution import create_execution_schema
    from .snapshot_releases import create_release_schema

    create_release_schema(connection)
    create_execution_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 6 WHERE singleton = 1")


def migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
    from .artifacts import create_artifact_schema

    create_artifact_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 7 WHERE singleton = 1")


def migrate_v7_to_v8(connection: sqlite3.Connection) -> None:
    """Add resumable artifact staging without rewriting completed v7 artifacts."""
    from .artifacts import create_artifact_upload_schema

    create_artifact_upload_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 8 WHERE singleton = 1")


def migrate_v8_to_v9(connection: sqlite3.Connection) -> None:
    """Add quarantined output-manifest metadata without changing artifacts or uploads."""
    from .artifact_manifests import create_artifact_manifest_schema

    create_artifact_manifest_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 9 WHERE singleton = 1")


def migrate_v9_to_v10(connection: sqlite3.Connection) -> None:
    """Add append-only human artifact dispositions without changing manifests."""
    from .artifact_dispositions import create_artifact_disposition_schema

    create_artifact_disposition_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 10 WHERE singleton = 1")


def migrate_v10_to_v11(connection: sqlite3.Connection) -> None:
    """Add explicit recovery lineage and a fail-closed restored-state hold."""
    from .recovery import create_recovery_schema

    create_recovery_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 11 WHERE singleton = 1")


def _rebuild_findings(connection: sqlite3.Connection) -> None:
    """Preserve existing finding columns while installing the current schema."""
    from .research import create_research_schema

    tables = ("research_findings", "research_finding_sources", "research_finding_variants")
    # Remove dependent tables first. Dropping a referenced parent with deferred
    # foreign keys can leave a deferred violation even after its replacement.
    for table in tables:
        connection.execute(f"CREATE TEMP TABLE migration_12_{table} AS SELECT * FROM {table}")
    for table in reversed(tables):
        connection.execute(f"DROP TABLE {table}")
    create_research_schema(connection)
    for table in tables:
        columns = ",".join(
            '"' + row[1].replace('"', '""') + '"'
            for row in connection.execute(f"PRAGMA table_info(migration_12_{table})")
        )
        connection.execute(
            f"INSERT INTO {table} ({columns}) SELECT {columns} FROM migration_12_{table}"
        )
        connection.execute(f"DROP TABLE migration_12_{table}")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Finding migration would leave invalid references.")


def migrate_v11_to_v12(connection: sqlite3.Connection) -> None:
    """Extend finding status without losing citations or disabling foreign keys."""
    _rebuild_findings(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 12 WHERE singleton = 1")


def migrate_v12_to_v13(connection: sqlite3.Connection) -> None:
    """Add durable finding replacement links and superseded terminal status."""
    _rebuild_findings(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 13 WHERE singleton = 1")


def migrate_v13_to_v14(connection: sqlite3.Connection) -> None:
    """Add worker registration records without altering existing v13 authority."""
    from .workers import create_worker_schema

    create_worker_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 14 WHERE singleton = 1")


def migrate_v14_to_v15(connection: sqlite3.Connection) -> None:
    """Add immutable synthetic result checkpoint records without touching v14 evidence."""
    from .result_checkpoints import create_result_checkpoint_schema

    create_result_checkpoint_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 15 WHERE singleton = 1")


def migrate_v15_to_v16(connection: sqlite3.Connection) -> None:
    """Add metadata-only skill snapshots, worker releases and grant release bindings."""
    from .skills import create_skill_schema

    create_skill_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 16 WHERE singleton = 1")


def migrate_v16_to_v17(connection: sqlite3.Connection) -> None:
    """Add bounded confirmed-stop workspace handoff records without changing snapshots."""
    from .handoffs import create_handoff_schema

    create_handoff_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 17 WHERE singleton = 1")


def migrate_v17_to_v18(connection: sqlite3.Connection) -> None:
    """Add empty operation intent records; v17 leases/observations stay legacy-null."""
    from .external_operations import create_external_operation_schema

    create_external_operation_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 18 WHERE singleton = 1")


def migrate_v18_to_v19(connection: sqlite3.Connection) -> None:
    """Add empty one-to-one provider receipts; no v18 intent gains invented evidence."""
    from .external_operations import create_external_operation_receipt_schema

    create_external_operation_receipt_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 19 WHERE singleton = 1")


def migrate_v19_to_v20(connection: sqlite3.Connection) -> None:
    """Add an empty human result-bookkeeping ledger without inventing acceptances."""
    from .result_acceptances import create_result_acceptance_schema

    create_result_acceptance_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 20 WHERE singleton = 1")


def migrate_v20_to_v21(connection: sqlite3.Connection) -> None:
    """Add empty metadata-only credential access leases without inferring secrets."""
    from .credential_leases import create_credential_access_lease_schema

    create_credential_access_lease_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 21 WHERE singleton = 1")


def migrate_v21_to_v22(connection: sqlite3.Connection) -> None:
    """Add empty project-storage metadata without inventing legacy BLOB placements."""
    from .project_artifact_store import create_project_artifact_storage_schema

    create_project_artifact_storage_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 22 WHERE singleton = 1")


def migrate_v22_to_v23(connection: sqlite3.Connection) -> None:
    """Separate durable storage intent/reconciliation metadata from v22 authority."""
    connection.execute(
        "ALTER TABLE project_artifact_storage_operations RENAME TO migration_23_operations"
    )
    from .project_artifact_store import create_project_artifact_storage_schema

    create_project_artifact_storage_schema(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(migration_23_operations)")}
    intent_at = "staged_at" if "staged_at" in columns else "intent_at"
    connection.execute(
        f"""INSERT INTO project_artifact_storage_operations
        (id,artifact_id,project_id,backend_kind,operation_key,state,sha256,size,retention_class,
         intent_at,reconcile_started_at,committed_at,reconciled_at,last_error)
        SELECT id,artifact_id,project_id,backend_kind,operation_key,
        CASE state WHEN 'committed' THEN 'committed' ELSE 'intent' END,
        sha256,size,retention_class,{intent_at},
        CASE WHEN state='committed' THEN {intent_at} END,
        committed_at,reconciled_at,NULL FROM migration_23_operations"""
    )
    connection.execute("DROP TABLE migration_23_operations")
    connection.execute("UPDATE schema_meta SET schema_version = 23 WHERE singleton = 1")


def migrate_v23_to_v24(connection: sqlite3.Connection) -> None:
    """Add explicit retention/recovery metadata without changing object custody."""
    from .project_artifact_retention import create_project_artifact_retention_schema

    create_project_artifact_retention_schema(connection)
    connection.execute(
        """INSERT OR IGNORE INTO project_artifact_retention
        (artifact_id,project_id,retention_class,state,created_at,activity_at,
         tombstoned_at,delete_intent_at,deleted_at,incident_at,incident_reason)
        SELECT artifact_id,project_id,retention_class,'active',committed_at,reconciled_at,
        NULL,NULL,NULL,NULL,NULL FROM project_artifact_placements"""
    )
    connection.execute("UPDATE schema_meta SET schema_version = 24 WHERE singleton = 1")


def migrate_v24_to_v25(connection: sqlite3.Connection) -> None:
    """Add empty goal-to-decision records without altering existing research/execution history."""
    from .experiments import create_v25_experiment_schema

    create_v25_experiment_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 25 WHERE singleton = 1")


def migrate_v25_to_v26(connection: sqlite3.Connection) -> None:
    """Index existing experiment evidence for retention without changing its custody."""
    from .experiments import create_v26_experiment_evidence_retention_schema

    create_v26_experiment_evidence_retention_schema(connection)
    connection.execute(
        """INSERT OR IGNORE INTO experiment_artifact_evidence_references
        (id,artifact_id,project_id,attempt_id,measurement_id,reference_kind,created_at)
        SELECT 'experiment_evidence_attempt_' || aa.attempt_id || '_' || aa.artifact_id,
        aa.artifact_id,e.project_id,aa.attempt_id,NULL,'attempt_artifact',aa.created_at
        FROM research_attempt_artifacts aa
        JOIN research_experiment_attempts a ON a.id=aa.attempt_id
        JOIN research_experiment_variants v ON v.id=a.variant_id
        JOIN research_experiments e ON e.id=v.experiment_id"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO experiment_artifact_evidence_references
        (id,artifact_id,project_id,attempt_id,measurement_id,reference_kind,created_at)
        SELECT 'experiment_evidence_measurement_' || m.id,m.artifact_id,e.project_id,
        m.attempt_id,m.id,'measurement_artifact',m.created_at
        FROM research_attempt_measurements m
        JOIN research_experiment_attempts a ON a.id=m.attempt_id
        JOIN research_experiment_variants v ON v.id=a.variant_id
        JOIN research_experiments e ON e.id=v.experiment_id
        WHERE m.artifact_id IS NOT NULL"""
    )
    connection.execute("UPDATE schema_meta SET schema_version = 26 WHERE singleton = 1")


def migrate_v26_to_v27(connection: sqlite3.Connection) -> None:
    """Freeze known current design context and honestly retain unknown legacy history.

    Before v27, goal and success criterion lived only on the mutable experiment
    projection. The current revision can therefore be copied exactly; earlier
    revisions cannot be reconstructed and remain NULL rather than being assigned
    the current context. Conclusions with evidence from exactly one design revision
    are linked to it; mixed legacy citations remain explicitly unbound.
    """
    design_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(research_experiment_designs)")
    }
    if "goal" in design_columns:
        # Fresh-schema helpers are intentionally current-version definitions. A
        # test/recovery path replaying intermediate migration numbers can already
        # have this additive shape without any historical rows to infer.
        connection.execute("UPDATE schema_meta SET schema_version = 27 WHERE singleton = 1")
        return
    connection.execute("ALTER TABLE research_experiment_designs ADD COLUMN goal TEXT")
    connection.execute("ALTER TABLE research_experiment_designs ADD COLUMN success_criterion TEXT")
    connection.execute(
        """UPDATE research_experiment_designs AS d SET
        goal=(SELECT e.goal FROM research_experiments e WHERE e.id=d.experiment_id),
        success_criterion=(SELECT e.success_criterion FROM research_experiments e
                           WHERE e.id=d.experiment_id)
        WHERE d.revision=(SELECT e.current_design_revision FROM research_experiments e
                          WHERE e.id=d.experiment_id)"""
    )
    connection.execute(
        """CREATE TEMP TABLE migration_27_conclusions AS
        SELECT c.*,
        CASE WHEN (SELECT count(DISTINCT v.design_revision)
                   FROM research_conclusion_evidence ce
                   JOIN research_experiment_attempts a ON a.id=ce.attempt_id
                   JOIN research_experiment_variants v ON v.id=a.variant_id
                   WHERE ce.conclusion_id=c.id AND v.experiment_id=c.experiment_id)=1
             THEN (SELECT min(v.design_revision)
                   FROM research_conclusion_evidence ce
                   JOIN research_experiment_attempts a ON a.id=ce.attempt_id
                   JOIN research_experiment_variants v ON v.id=a.variant_id
                   WHERE ce.conclusion_id=c.id AND v.experiment_id=c.experiment_id)
        END AS design_revision
        FROM research_conclusions c"""
    )
    connection.execute(
        """CREATE TEMP TABLE migration_27_conclusion_evidence AS
        SELECT * FROM research_conclusion_evidence"""
    )
    connection.execute("DROP TABLE research_conclusion_evidence")
    connection.execute("DROP TABLE research_conclusions")
    from .experiments import create_v27_experiment_conclusion_schema

    create_v27_experiment_conclusion_schema(connection)
    connection.execute(
        """INSERT INTO research_conclusions
        (id,experiment_id,design_revision,statement,status,revision,created_at,updated_at,
         created_by_principal_id,reviewed_at,reviewed_by_principal_id)
        SELECT id,experiment_id,design_revision,statement,status,revision,created_at,updated_at,
        created_by_principal_id,reviewed_at,reviewed_by_principal_id
        FROM migration_27_conclusions"""
    )
    connection.execute(
        """INSERT INTO research_conclusion_evidence
        SELECT conclusion_id,attempt_id,artifact_id,measurement_id
        FROM migration_27_conclusion_evidence"""
    )
    connection.execute("DROP TABLE migration_27_conclusion_evidence")
    connection.execute("DROP TABLE migration_27_conclusions")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Design-context migration would leave invalid references.")
    connection.execute("UPDATE schema_meta SET schema_version = 27 WHERE singleton = 1")


def migrate_v27_to_v28(connection: sqlite3.Connection) -> None:
    """Type new attempt provenance without inventing old execution evidence."""
    attempt_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(research_experiment_attempts)")
    }
    if "provenance_kind" in attempt_columns:
        # New databases created by a newer binary can pass through intermediate
        # migrations in tests; their records already have the v28 shape.
        connection.execute("UPDATE schema_meta SET schema_version = 28 WHERE singleton = 1")
        return
    connection.execute("ALTER TABLE research_experiment_variants ADD COLUMN execution_task_id TEXT")
    connection.execute(
        "ALTER TABLE research_experiment_variants ADD COLUMN execution_plan_digest TEXT"
    )
    tables = (
        "research_conclusion_evidence",
        "experiment_artifact_evidence_references",
        "research_attempt_measurements",
        "research_attempt_artifacts",
        "research_experiment_attempts",
    )
    for table in tables:
        connection.execute(f"CREATE TEMP TABLE migration_28_{table} AS SELECT * FROM {table}")
    for table in tables:
        connection.execute(f"DROP TABLE {table}")
    from .experiments import create_experiment_evidence_retention_schema, create_experiment_schema

    create_experiment_schema(connection)
    create_experiment_evidence_retention_schema(connection)
    connection.execute(
        """INSERT INTO research_experiment_attempts
        (id,variant_id,design_revision,provenance_kind,operation_id,execution_plan_digest,
         package_digest,operation_desired_state,operation_observed_state,termination_status,
         human_observation,outcome,reason,revision,created_at,created_by_principal_id)
        SELECT a.id,a.variant_id,v.design_revision,'legacy_operation_unverified',a.operation_id,
        NULL,NULL,NULL,'legacy_unverified','legacy_unknown',NULL,a.outcome,a.reason,a.revision,
        a.created_at,a.created_by_principal_id
        FROM migration_28_research_experiment_attempts a
        JOIN research_experiment_variants v ON v.id=a.variant_id"""
    )
    for table in ("research_attempt_artifacts", "research_attempt_measurements"):
        columns = ",".join(
            row[1] for row in connection.execute(f"PRAGMA table_info(migration_28_{table})")
        )
        connection.execute(
            f"INSERT INTO {table} ({columns}) SELECT {columns} FROM migration_28_{table}"
        )
    for table in ("experiment_artifact_evidence_references", "research_conclusion_evidence"):
        columns = ",".join(
            row[1] for row in connection.execute(f"PRAGMA table_info(migration_28_{table})")
        )
        connection.execute(
            f"INSERT INTO {table} ({columns}) SELECT {columns} FROM migration_28_{table}"
        )
    for table in tables:
        connection.execute(f"DROP TABLE migration_28_{table}")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Operation-provenance migration would leave invalid references.")
    connection.execute("UPDATE schema_meta SET schema_version = 28 WHERE singleton = 1")


def migrate_v28_to_v29(connection: sqlite3.Connection) -> None:
    """Add empty append-only conclusion correction lineage without changing reviews."""
    from .experiments import create_experiment_schema

    create_experiment_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 29 WHERE singleton = 1")


def migrate_v29_to_v30(connection: sqlite3.Connection) -> None:
    """Add empty pre-execution experiment admission bindings without backfills."""
    from .experiments import create_experiment_admission_schema
    from .external_operations import create_experiment_operation_binding_schema

    create_experiment_admission_schema(connection)
    create_experiment_operation_binding_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 30 WHERE singleton = 1")


def migrate_v30_to_v31(connection: sqlite3.Connection) -> None:
    """Repair the v30 bootstrap omission without changing experiment records.

    v30 migrations created the immutable external-operation binding index, but
    a fresh v30 database did not.  Reapply the idempotent binding helper so old
    fresh databases and correctly migrated v30 databases converge.
    """
    from .external_operations import create_experiment_operation_binding_schema

    create_experiment_operation_binding_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 31 WHERE singleton = 1")


def migrate_v31_to_v32(connection: sqlite3.Connection) -> None:
    """Add empty evidence tables for the already-authoritative admission identity.

    Existing operation-backed and human-reported attempts remain in their legacy
    tables.  No historical attempt, conclusion, or custody link is inferred.
    """
    from .experiments import create_admitted_attempt_evidence_schema

    create_admitted_attempt_evidence_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 32 WHERE singleton = 1")


def migrate_v32_to_v33(connection: sqlite3.Connection) -> None:
    """Separate historical human outcome labels from later operational observations.

    v32 kept its outcome on a mutable admission projection.  Preserve every old
    label in both that compatibility projection and one immutable decision row;
    absent historical operational detail remains explicitly legacy-unverified.
    """
    from .experiments import create_admitted_attempt_resolution_schema

    create_admitted_attempt_resolution_schema(connection)
    connection.execute(
        """INSERT OR IGNORE INTO research_experiment_admitted_attempt_resolutions
        (attempt_id,outcome,reason,expected_revision,decided_at,decided_by_principal_id)
        SELECT id,outcome,reason,revision,updated_at,resolved_by_principal_id
        FROM research_experiment_admitted_attempts
        WHERE outcome IS NOT NULL AND resolved_by_principal_id IS NOT NULL"""
    )
    connection.execute("UPDATE schema_meta SET schema_version = 33 WHERE singleton = 1")


def migrate_v33_to_v34(connection: sqlite3.Connection) -> None:
    """Keep latest operational sample freshness outside semantic state history."""
    from .experiments import create_admitted_attempt_observation_projection_schema

    create_admitted_attempt_observation_projection_schema(connection)
    connection.execute(
        """UPDATE research_experiment_admitted_attempts
        SET operational_observation_cursor=(
            SELECT 'observation:' || o.id FROM execution_observations o
            WHERE o.operation_id=research_experiment_admitted_attempts.operation_id
            ORDER BY rowid DESC LIMIT 1),
            operational_observed_at=(
            SELECT o.observed_at FROM execution_observations o
            WHERE o.operation_id=research_experiment_admitted_attempts.operation_id
            ORDER BY rowid DESC LIMIT 1)
        WHERE operation_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM execution_observations o
            WHERE o.operation_id=research_experiment_admitted_attempts.operation_id)"""
    )
    connection.execute("UPDATE schema_meta SET schema_version = 34 WHERE singleton = 1")


def migrate_v34_to_v35(connection: sqlite3.Connection) -> None:
    """Add receipt-local ordering without rewriting any operational history."""
    from .external_operations import create_external_operation_receipt_schema

    create_external_operation_receipt_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 35 WHERE singleton = 1")


def migrate_v35_to_v36(connection: sqlite3.Connection) -> None:
    """Add empty human-approved Kubernetes dispatch/outbox records."""
    from .external_operations import create_kubernetes_dispatch_schema

    create_kubernetes_dispatch_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 36 WHERE singleton = 1")


def migrate_v36_to_v37(connection: sqlite3.Connection) -> None:
    """Add an empty context-only harness delivery journal without changing legacy chat."""
    from .interaction import create_interaction_schema

    create_interaction_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 37 WHERE singleton = 1")


def migrate_v37_to_v38(connection: sqlite3.Connection) -> None:
    """Fence new delivery claims to their original operation/grant/lease tuple.

    Existing v37 delivery rows intentionally remain NULL and unclaimable: their
    complete lease association was never recorded and must not be reconstructed.
    """
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(interaction_message_deliveries)")
    }
    for column, definition in (
        ("operation_id", "TEXT REFERENCES external_operations(operation_id)"),
        ("grant_id", "TEXT REFERENCES execution_grants(id)"),
        ("lease_id", "TEXT REFERENCES execution_leases(id)"),
    ):
        if column not in columns:
            connection.execute(
                f"ALTER TABLE interaction_message_deliveries ADD COLUMN {column} {definition}"
            )
    connection.execute("UPDATE schema_meta SET schema_version = 38 WHERE singleton = 1")


def migrate_v38_to_v39(connection: sqlite3.Connection) -> None:
    """Add empty immutable operation-to-harness bindings without inferring sessions.

    Existing delivery rows remain historical records with a NULL binding.  They
    retain read/cancel semantics but cannot acquire newly introduced binding
    authority.
    """
    from .interaction import create_interaction_schema

    create_interaction_schema(connection)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(interaction_message_deliveries)")
    }
    if "harness_session_binding_id" not in columns:
        connection.execute(
            "ALTER TABLE interaction_message_deliveries ADD COLUMN "
            "harness_session_binding_id TEXT REFERENCES operation_harness_session_bindings(id)"
        )
    connection.execute("UPDATE schema_meta SET schema_version = 39 WHERE singleton = 1")


def migrate_v39_to_v40(connection: sqlite3.Connection) -> None:
    """Add empty provider-disabled Astra conversation/coordinator records.

    No historical conversation, harness binding, approval, grant, dispatch, or
    proposal is inferred from v39 data.
    """
    from .astra import create_astra_schema

    create_astra_schema(connection, include_proposal_promotion=False)
    connection.execute("UPDATE schema_meta SET schema_version = 40 WHERE singleton = 1")


def migrate_v40_to_v41(connection: sqlite3.Connection) -> None:
    """Add empty human proposal-to-design registrations without execution authority."""
    from .astra import create_astra_schema

    columns = {row[1] for row in connection.execute("PRAGMA table_info(astra_proposals)")}
    if "design_json" not in columns:
        connection.execute("ALTER TABLE astra_proposals ADD COLUMN design_json TEXT")
    create_astra_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 41 WHERE singleton = 1")


def migrate_v41_to_v42(connection: sqlite3.Connection) -> None:
    """Add empty, non-execution Astra explain-status discussion records.

    No old coordinator turn is inferred to be a tool call, answer, binding, or
    lease.  These records are deliberately separate from operation harness
    session bindings and execution leases.
    """
    from .astra_status import create_astra_status_schema

    create_astra_status_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 42 WHERE singleton = 1")


def migrate_v42_to_v43(connection: sqlite3.Connection) -> None:
    """Add empty operation-bound remote lifecycle records without inferred runs."""
    from .remote_workflows import create_remote_workflow_schema

    create_remote_workflow_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 43 WHERE singleton = 1")


def migrate_v43_to_v44(connection: sqlite3.Connection) -> None:
    """Add empty lost-worker recovery records without inferring identity loss."""
    from .external_operations import create_execution_recovery_schema

    create_execution_recovery_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 44 WHERE singleton = 1")


def migrate_v44_to_v45(connection: sqlite3.Connection) -> None:
    """Add empty coordinator consumer leases and availability records."""
    from .astra import create_astra_consumer_schema

    create_astra_consumer_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 45 WHERE singleton = 1")


def migrate_v45_to_v46(connection: sqlite3.Connection) -> None:
    """Add empty human artifact-read custody and immutable bundle records.

    Existing quarantined manifests remain unreleased. The migration neither
    infers a custody decision nor exports bytes, inputs, or environment material.
    """
    from .artifact_custody import create_artifact_custody_schema

    create_artifact_custody_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 46 WHERE singleton = 1")


def migrate_v46_to_v47(connection: sqlite3.Connection) -> None:
    """Add secret-free operator audit/health metadata and optional event lineage."""
    from .observability import create_observability_schema

    columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    if "correlation_id" not in columns:
        connection.execute("ALTER TABLE events ADD COLUMN correlation_id TEXT")
    if "causation_id" not in columns:
        connection.execute("ALTER TABLE events ADD COLUMN causation_id TEXT")
    create_observability_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version = 47 WHERE singleton = 1")


def migrate_v47_to_v48(connection: sqlite3.Connection) -> None:
    """Add empty, metadata-only program dataset ingestion/provenance records.

    The migration does not download datasets, validate prior bytes, infer licenses,
    or create ingestion approvals from existing sources or artifacts.
    """
    from .dataset_ingestion import create_dataset_ingestion_schema

    create_dataset_ingestion_schema(connection)
    # A pre-upgrade backup cannot cover the new records and cannot be restored
    # by this release. Do not carry its freshness claim into active v48 state.
    # Keep failures visible and leave held audit history alone. This runs only
    # on the version transition, not on an idempotent current-schema migration.
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version = 48 WHERE singleton = 1")


def migrate_v48_to_v49(connection: sqlite3.Connection) -> None:
    """Add empty capability policies/intents; never infer approval or enable execution."""
    from .capabilities import create_capability_schema

    create_capability_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version = 49 WHERE singleton = 1")


def migrate_v49_to_v50(connection: sqlite3.Connection) -> None:
    """Add empty external-agent scopes/proposals; infer no disclosure authority."""
    from .external_agents import create_external_agent_schema

    create_external_agent_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version = 50 WHERE singleton = 1")


def migrate_v50_to_v51(connection: sqlite3.Connection) -> None:
    """Install empty bounded agent authority/provenance; infer no existing grant."""
    from .agent_authority import create_agent_authority_schema
    from .agent_provenance import create_agent_provenance_schema
    from .agent_research import create_agent_research_schema

    create_agent_authority_schema(connection)
    create_agent_provenance_schema(connection)
    create_agent_research_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=51 WHERE singleton=1")


def migrate_v51_to_v52(connection: sqlite3.Connection) -> None:
    """Install empty output UID bindings; never infer historical provider identity."""
    from .agent_provenance_outputs import create_output_provenance_schema

    create_output_provenance_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=52 WHERE singleton=1")


def migrate_v52_to_v53(connection: sqlite3.Connection) -> None:
    """Admit public generated artifacts without any inferred execution identity."""
    from .generated_outputs import create_generated_output_schema

    create_generated_output_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=53 WHERE singleton=1")


def migrate_v53_to_v54(connection: sqlite3.Connection) -> None:
    """Empty explicit Docker target/outbox; infer no capability or execution grant."""
    from .ordinary_docker import create_ordinary_docker_schema

    create_ordinary_docker_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=54 WHERE singleton=1")


def migrate_v54_to_v55(connection: sqlite3.Connection) -> None:
    """Install empty runtime bindings; invalidate only active pre-upgrade freshness."""
    from .runtime_bindings import create_runtime_binding_schema

    create_runtime_binding_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=55 WHERE singleton=1")


def migrate_v55_to_v56(connection: sqlite3.Connection) -> None:
    """Add empty Sandbox bindings; no inferred target, authority or provider effects."""
    from .agent_sandbox import create_agent_sandbox_schema

    create_agent_sandbox_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=56 WHERE singleton=1")


def migrate_v56_to_v57(connection: sqlite3.Connection) -> None:
    """Install empty experiment budgets, without inferring financial authority."""
    from .experiment_budgets import create_experiment_budget_schema

    create_experiment_budget_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=57 WHERE singleton=1")


def migrate_v57_to_v58(connection: sqlite3.Connection) -> None:
    """Add empty lineage and non-fake run receipt records; no inferred history.

    Existing fake receipts and acceptances keep their CHECK-constrained
    development labels; nothing is relabelled or backfilled.
    """
    from .artifact_lineage import create_artifact_lineage_schema
    from .external_operations import create_execution_run_receipt_schema
    from .result_acceptances import create_observed_result_acceptance_schema

    create_execution_run_receipt_schema(connection)
    create_observed_result_acceptance_schema(connection)
    create_artifact_lineage_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=58 WHERE singleton=1")


def migrate_v58_to_v59(connection: sqlite3.Connection) -> None:
    """Install empty gate-evidence records and the server-set measurement source.

    Existing measurements are classified only from their durable derivation event;
    no lock, binding, metric, aggregate, audit or program is inferred.
    """
    from .experiment_budgets import create_bound_resolution_schema
    from .gate_evidence import create_gate_evidence_schema

    create_gate_evidence_schema(connection)
    # Databases that reached v57 before one-shot bound resolution existed lack this
    # table; backup validates the exact table set, so install it here (idempotent).
    create_bound_resolution_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=59 WHERE singleton=1")


def migrate_v59_to_v60(connection: sqlite3.Connection) -> None:
    """Add chunked snapshot upload/chunk tables and provider input tickets."""
    from .snapshot_chunks import create_snapshot_chunk_schema

    create_snapshot_chunk_schema(connection)
    connection.execute("UPDATE schema_meta SET schema_version=60 WHERE singleton=1")


def migrate_v60_to_v61(connection: sqlite3.Connection) -> None:
    """Pre-register seeds and metric declarations on split bindings, and make program
    outcome ids plan-declared data.

    Existing bindings keep NULL seed and metric declarations; program outcome rows are
    copied unchanged while the enumerated-outcome CHECK is dropped. Nothing is inferred.
    """
    from .gate_evidence import create_gate_evidence_schema

    create_gate_evidence_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=61 WHERE singleton=1")


def migrate_v61_to_v62(connection: sqlite3.Connection) -> None:
    """Record a data class for every staged snapshot (ADR 0039).

    Every existing snapshot becomes ``private_derived`` (origin ``migrated``), failing
    closed: nothing is inferred public. A public class needs a new human staging with
    license and source evidence. (The campaign runner track's migration, also written as v62,
    must be renumbered: v64 since ADR 0039 amendment 1 took v63.)
    """
    from .data_class import create_data_class_schema, migrate_existing_snapshots
    from .store import utc_now

    create_data_class_schema(connection)
    now = utc_now()
    migrate_existing_snapshots(connection, now)
    # Tickets minted before the guard existed are revoked; new ones re-check the class.
    connection.execute(
        "UPDATE provider_input_tickets SET revoked_at=? WHERE revoked_at IS NULL", (now,)
    )
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=62 WHERE singleton=1")


def migrate_v62_to_v63(connection: sqlite3.Connection) -> None:
    """Add the immutable private-code approval record (ADR 0039, amendment 1).

    Nothing is approved by migration: every existing snapshot has no approval, so the
    third-party rule for it is unchanged (public data only). An approval needs a new
    human staging decision. (The campaign runner track's migration must take v64.)
    """
    from .code_approval import create_code_approval_schema

    create_code_approval_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=63 WHERE singleton=1")


def migrate_v63_to_v64(connection: sqlite3.Connection) -> None:
    """Campaign tables (ADR 0038) and a snapshot-release data class of private_derived.

    Release rows are copied unchanged into a table whose CHECK also admits
    ``private_derived``; nothing is reclassified. Existing grants keep their frozen
    release ids, so referencing rows are re-validated by foreign_key_check.
    """
    from .campaign_schema import create_campaign_schema
    from .code_approval import create_code_approval_schema
    from .data_class import create_data_class_schema, migrate_existing_snapshots
    from .snapshot_releases import create_release_schema
    from .store import utc_now

    # A development database stamped 62 or 63 by an earlier campaign build never ran
    # the data class (v62) or code approval (v63) steps; all three calls are
    # idempotent, and migrate_existing_snapshots only records snapshots without a row.
    create_data_class_schema(connection)
    migrate_existing_snapshots(connection, utc_now())
    create_code_approval_schema(connection)
    preexisting = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='snapshot_releases'"
    ).fetchone()
    if row is not None and "private_derived" not in row[0]:
        # execution_grants references snapshot_releases. Defer the check to COMMIT and
        # re-insert the same ids into a table of the same name before then.
        connection.execute("PRAGMA defer_foreign_keys=ON")
        connection.execute("CREATE TABLE snapshot_releases_v61 AS SELECT * FROM snapshot_releases")
        connection.execute("DROP TABLE snapshot_releases")
        create_release_schema(connection)
        connection.execute("INSERT INTO snapshot_releases SELECT * FROM snapshot_releases_v61")
        connection.execute("DROP TABLE snapshot_releases_v61")
    create_campaign_schema(connection)
    # An earlier development build may lack the Salad attempt result columns.
    attempt_columns = {
        r[1] for r in connection.execute("PRAGMA table_info(campaign_stage_attempts)")
    }
    for column in ("salad_verdict TEXT", "salad_evidence_digest TEXT"):
        if column.split()[0] not in attempt_columns:
            connection.execute(f"ALTER TABLE campaign_stage_attempts ADD COLUMN {column}")
    # Databases rebuilt by older migrations may carry violations this step cannot own;
    # it must only never add any (the release rebuild keeps every referenced id).
    if len(connection.execute("PRAGMA foreign_key_check").fetchall()) > preexisting:
        raise ValueError("Migration v64 introduced foreign key violations.")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=64 WHERE singleton=1")


_V1_BACKEND_PROVIDERS = {"docker-local-cpu-v1": "local-docker", "salad-gpu-v1": "salad"}


def migrate_v64_to_v65(connection: sqlite3.Connection) -> None:
    """Provider trust policies, campaign stage I/O and provider ids (ADR 0040).

    Additive only: no CHECK rebuild and no reclassification. provider_id is backfilled
    from each stored v1 stage backend; the last backup is marked stale.
    """
    from .campaign_schema import add_v65_columns, create_campaign_schema
    from .provider_trust import create_provider_trust_schema

    create_campaign_schema(connection)
    add_v65_columns(connection)
    create_provider_trust_schema(connection)
    for campaign_id, spec_json in connection.execute(
        "SELECT id, spec_json FROM campaigns"
    ).fetchall():
        try:
            stages = json.loads(spec_json).get("stages", [])
        except (ValueError, AttributeError):
            continue
        for stage in stages:
            provider = _V1_BACKEND_PROVIDERS.get(stage.get("backend"))
            if provider is None:
                continue
            connection.execute(
                "UPDATE campaign_stages SET provider_id=? "
                "WHERE campaign_id=? AND stage_key=? AND provider_id IS NULL",
                (provider, campaign_id, stage.get("key")),
            )
            connection.execute(
                "UPDATE campaign_stage_attempts SET provider_id=? "
                "WHERE campaign_id=? AND stage_key=? AND provider_id IS NULL",
                (provider, campaign_id, stage.get("key")),
            )
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=65 WHERE singleton=1")


def migrate_v65_to_v66(connection: sqlite3.Connection) -> None:
    """Execution frames and write-once experiment hold releases (ADR 0041 stages 1 and 3).

    Additive only: existing attempts get no frame; the runner opens one when it next
    settles such an attempt. The last backup is marked stale.
    """
    from .execution_frames import create_execution_frame_schema
    from .experiment_budgets import create_budget_release_schema

    create_execution_frame_schema(connection)
    create_budget_release_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=66 WHERE singleton=1")


def migrate_v66_to_v67(connection: sqlite3.Connection) -> None:
    """Standing approvals and scheduled campaign starts (ADR 0042).

    Additive only: three tables, an append-only guard on the starts table and the
    ``campaign_approvals.standing_approval_id`` column (null for every existing approval).
    The last backup is marked stale.
    """
    from .campaign_standing_schema import create_standing_schema

    create_standing_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=67 WHERE singleton=1")


def migrate_v67_to_v68(connection: sqlite3.Connection) -> None:
    """Salad review pre-approvals (ADR 0044): additive, append-only tables."""
    from .campaign_preapproval import create_preapproval_schema

    create_preapproval_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=68 WHERE singleton=1")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
    3: migrate_v3_to_v4,
    4: migrate_v4_to_v5,
    5: migrate_v5_to_v6,
    6: migrate_v6_to_v7,
    7: migrate_v7_to_v8,
    8: migrate_v8_to_v9,
    9: migrate_v9_to_v10,
    10: migrate_v10_to_v11,
    11: migrate_v11_to_v12,
    12: migrate_v12_to_v13,
    13: migrate_v13_to_v14,
    14: migrate_v14_to_v15,
    15: migrate_v15_to_v16,
    16: migrate_v16_to_v17,
    17: migrate_v17_to_v18,
    18: migrate_v18_to_v19,
    19: migrate_v19_to_v20,
    20: migrate_v20_to_v21,
    21: migrate_v21_to_v22,
    22: migrate_v22_to_v23,
    23: migrate_v23_to_v24,
    24: migrate_v24_to_v25,
    25: migrate_v25_to_v26,
    26: migrate_v26_to_v27,
    27: migrate_v27_to_v28,
    28: migrate_v28_to_v29,
    29: migrate_v29_to_v30,
    30: migrate_v30_to_v31,
    31: migrate_v31_to_v32,
    32: migrate_v32_to_v33,
    33: migrate_v33_to_v34,
    34: migrate_v34_to_v35,
    35: migrate_v35_to_v36,
    36: migrate_v36_to_v37,
    37: migrate_v37_to_v38,
    38: migrate_v38_to_v39,
    39: migrate_v39_to_v40,
    40: migrate_v40_to_v41,
    41: migrate_v41_to_v42,
    42: migrate_v42_to_v43,
    43: migrate_v43_to_v44,
    44: migrate_v44_to_v45,
    45: migrate_v45_to_v46,
    46: migrate_v46_to_v47,
    47: migrate_v47_to_v48,
    48: migrate_v48_to_v49,
    49: migrate_v49_to_v50,
    50: migrate_v50_to_v51,
    51: migrate_v51_to_v52,
    52: migrate_v52_to_v53,
    53: migrate_v53_to_v54,
    54: migrate_v54_to_v55,
    55: migrate_v55_to_v56,
    56: migrate_v56_to_v57,
    57: migrate_v57_to_v58,
    58: migrate_v58_to_v59,
    59: migrate_v59_to_v60,
    60: migrate_v60_to_v61,
    61: migrate_v61_to_v62,
    62: migrate_v62_to_v63,
    63: migrate_v63_to_v64,
    64: migrate_v64_to_v65,
    65: migrate_v65_to_v66,
    66: migrate_v66_to_v67,
    67: migrate_v67_to_v68,
}


def migrate_database(database_path: Path, connect: Callable[[Path], sqlite3.Connection]) -> int:
    """Migrate a database only when explicitly requested, returning its version."""
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = _schema_version(connection)
        if version > LATEST_SCHEMA_VERSION:
            raise ValueError("Control-plane database schema version is from a newer Tessera.")
        while version < LATEST_SCHEMA_VERSION:
            try:
                migration = MIGRATIONS[version]
            except KeyError as error:
                raise ValueError("Control-plane database schema version is unsupported.") from error
            migration(connection)
            version = _schema_version(connection)
        connection.execute("COMMIT")
        return version
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def migrate_v68_to_v69(connection: sqlite3.Connection) -> None:
    """Campaign gates (ADR 0045): human gate decisions, unlock evaluations, paired-delta
    replicate groups and the standing approval ``unlock_json`` column.

    Additive only; the last backup is marked stale.
    """
    from .campaign_gate_schema import create_campaign_gate_schema

    create_campaign_gate_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute("UPDATE schema_meta SET schema_version=69 WHERE singleton=1")


MIGRATIONS[68] = migrate_v68_to_v69


# Evidence rows ingested from campaign stage outputs (ADR 0052). The step is additive and
# idempotent (CREATE ... IF NOT EXISTS), and registered with setdefault so it can never replace
# another feature's migration at the same version. If another change already owns this version
# when branches merge, raise EVIDENCE_ROWS_SCHEMA_VERSION (and LATEST_SCHEMA_VERSION and the
# fresh-database INSERT in store.py); nothing else changes.
EVIDENCE_ROWS_SCHEMA_VERSION = 70


def migrate_evidence_rows_schema(connection: sqlite3.Connection) -> None:
    from .gate_evidence_ingest import create_evidence_ingest_schema

    create_evidence_ingest_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (EVIDENCE_ROWS_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(EVIDENCE_ROWS_SCHEMA_VERSION - 1, migrate_evidence_rows_schema)


# Campaign pause, concurrency limit and single-stage cancel
# (ADR 0061). Additive and idempotent; registered with setdefault. If another change owns this
# version when branches merge, raise STEERING_SCHEMA_VERSION (and LATEST_SCHEMA_VERSION).
STEERING_SCHEMA_VERSION = 71


def migrate_steering_schema(connection: sqlite3.Connection) -> None:
    from .campaign_steering_schema import create_campaign_steering_schema

    create_campaign_steering_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1", (STEERING_SCHEMA_VERSION,)
    )


MIGRATIONS.setdefault(STEERING_SCHEMA_VERSION - 1, migrate_steering_schema)


# One-time holdout claims of evaluation stages (ADR 0068). Additive and idempotent; registered
# with setdefault. If another change owns this version when branches merge, raise
# HOLDOUT_SCHEMA_VERSION (and LATEST_SCHEMA_VERSION and the fresh-database INSERT in store.py).
HOLDOUT_SCHEMA_VERSION = 72


def migrate_holdout_schema(connection: sqlite3.Connection) -> None:
    from .campaign_holdout import create_holdout_schema

    create_holdout_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1", (HOLDOUT_SCHEMA_VERSION,)
    )


MIGRATIONS.setdefault(HOLDOUT_SCHEMA_VERSION - 1, migrate_holdout_schema)


# Review jobs (schema 73): client-requested research/review jobs, findings and human
# decisions (ADR 0084). Additive and idempotent; registered with setdefault.
REVIEW_SCHEMA_VERSION = 73


def migrate_review_schema(connection: sqlite3.Connection) -> None:
    from .review_jobs import create_review_jobs_schema

    create_review_jobs_schema(connection)
    from .people import create_people_schema

    create_people_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1", (REVIEW_SCHEMA_VERSION,)
    )


MIGRATIONS.setdefault(REVIEW_SCHEMA_VERSION - 1, migrate_review_schema)


# Stuck-campaign findings (schema 74, ADR 0091). Additive and idempotent; registered with
# setdefault. If another change owns this version when branches merge, raise
# STUCK_SCHEMA_VERSION (and LATEST_SCHEMA_VERSION and the fresh-database INSERT in store.py).
STUCK_SCHEMA_VERSION = 74


def migrate_stuck_schema(connection: sqlite3.Connection) -> None:
    from .campaign_stuck import create_stuck_schema

    create_stuck_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1", (STUCK_SCHEMA_VERSION,)
    )


MIGRATIONS.setdefault(STUCK_SCHEMA_VERSION - 1, migrate_stuck_schema)


# Code review jobs (schema 75, ADR 0092). Widens the review_jobs kind CHECK to admit
# 'code_review' by a same-id rebuild and adds the code_review_jobs side table (with
# attempt_receipts_json) and the code_review_runners host-bound table. Idempotent;
# registered with setdefault. If another change owns this version when branches merge, raise
# CODE_REVIEW_SCHEMA_VERSION (and LATEST_SCHEMA_VERSION and the fresh-database INSERT in store.py).
CODE_REVIEW_SCHEMA_VERSION = 75


def migrate_code_review_schema(connection: sqlite3.Connection) -> None:
    from .review_jobs import create_review_jobs_schema

    preexisting = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='review_jobs'"
    ).fetchone()
    if row is not None and "'code_review'" not in row[0]:
        # review_findings, review_job_approvals and code_review_jobs reference review_jobs.
        # Defer the check to COMMIT and re-insert the same ids into a table of the same name.
        # The copied columns are those the old table has and the new definition keeps, read
        # from the tables themselves so a column added by any earlier migration is preserved.
        connection.execute("PRAGMA defer_foreign_keys=ON")
        old_columns = [r[1] for r in connection.execute("PRAGMA table_info(review_jobs)")]
        connection.execute("CREATE TABLE review_jobs_v74 AS SELECT * FROM review_jobs")
        connection.execute("DROP TABLE review_jobs")
        create_review_jobs_schema(connection)
        new_columns = {r[1] for r in connection.execute("PRAGMA table_info(review_jobs)")}
        copied = ",".join(c for c in old_columns if c in new_columns)
        connection.execute(
            f"INSERT INTO review_jobs ({copied}) SELECT {copied} FROM review_jobs_v74"
        )
        connection.execute("DROP TABLE review_jobs_v74")
    create_review_jobs_schema(connection)
    if len(connection.execute("PRAGMA foreign_key_check").fetchall()) > preexisting:
        raise ValueError("Migration v75 introduced foreign key violations.")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1", (CODE_REVIEW_SCHEMA_VERSION,)
    )


MIGRATIONS.setdefault(CODE_REVIEW_SCHEMA_VERSION - 1, migrate_code_review_schema)


# Approved campaign submissions (ADR 0143): a human's recorded approval an agent may consume
# once, plus its append-only decisions. Additive and idempotent; registered with setdefault.
# If another change owns this version when branches merge, renumber
# CAMPAIGN_SUBMISSIONS_SCHEMA_VERSION (LATEST_SCHEMA_VERSION follows it; also the
# fresh-database INSERT literal in store.py).


def migrate_submission_schema(connection: sqlite3.Connection) -> None:
    from .campaign_submissions import create_submission_schema

    create_submission_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (CAMPAIGN_SUBMISSIONS_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(CAMPAIGN_SUBMISSIONS_SCHEMA_VERSION - 1, migrate_submission_schema)


# Model-spend limits and ledger (ADR 0090). Additive and idempotent; registered with setdefault.
# This single constant carries the version: if another change owns it when branches merge,
# raise MODEL_SPEND_SCHEMA_VERSION, LATEST_SCHEMA_VERSION (the literal near the top) and the
# fresh-database INSERT in store.py.
MODEL_SPEND_SCHEMA_VERSION = 77


def migrate_model_spend_schema(connection: sqlite3.Connection) -> None:
    from .model_spend import create_model_spend_schema

    create_model_spend_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1", (MODEL_SPEND_SCHEMA_VERSION,)
    )


MIGRATIONS.setdefault(MODEL_SPEND_SCHEMA_VERSION - 1, migrate_model_spend_schema)


# Conclusion decision and evidence stance (schema 78). Additive and idempotent: a nullable
# human-recorded decision on conclusions and a nullable per-link stance (NULL means unstated).
# If another change owns this version when branches merge, raise CONCLUSION_DECISION_SCHEMA_VERSION
# (and LATEST_SCHEMA_VERSION and the fresh-database INSERT in store.py).
CONCLUSION_DECISION_SCHEMA_VERSION = 78

_STANCE_COLUMN = "stance TEXT CHECK (stance IS NULL OR stance IN ('supports','opposes'))"


def migrate_conclusion_decision_schema(connection: sqlite3.Connection) -> None:
    additions = (
        (
            "research_conclusions",
            "decision",
            "decision TEXT CHECK (decision IS NULL OR decision IN ('continue','revise','reject'))",
        ),
        ("research_conclusion_evidence", "stance", _STANCE_COLUMN),
        ("research_admitted_conclusion_evidence", "stance", _STANCE_COLUMN),
    )
    for table, column, definition in additions:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (CONCLUSION_DECISION_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(CONCLUSION_DECISION_SCHEMA_VERSION - 1, migrate_conclusion_decision_schema)


# Heavy-stage stuck codes (schema 79). Widens the campaign_stuck_findings code CHECK to admit
# 'heavy_stage_on_local' and 'heavy_stage_refused_local' by a table rebuild (SQLite cannot alter
# a CHECK). Rows are preserved; the index and the append-only/identity triggers are recreated by
# create_stuck_schema. Migrations must be contiguous (migrate_database has no gap handling), so
# this takes the number after the conclusion decision migration (if another change takes 79
# first, raise STUCK_HEAVY_STAGE_SCHEMA_VERSION, LATEST_SCHEMA_VERSION and the fresh-database
# INSERT in store.py).
STUCK_HEAVY_STAGE_SCHEMA_VERSION = 79


def migrate_stuck_heavy_stage_schema(connection: sqlite3.Connection) -> None:
    from .campaign_stuck import create_stuck_schema

    connection.execute(
        "CREATE TEMP TABLE campaign_stuck_findings_old AS SELECT * FROM campaign_stuck_findings"
    )
    # Dropping the table also drops its index and triggers; create_stuck_schema restores them.
    connection.execute("DROP TABLE campaign_stuck_findings")
    create_stuck_schema(connection)
    connection.execute(
        "INSERT INTO campaign_stuck_findings SELECT * FROM campaign_stuck_findings_old"
    )
    connection.execute("DROP TABLE campaign_stuck_findings_old")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (STUCK_HEAVY_STAGE_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(STUCK_HEAVY_STAGE_SCHEMA_VERSION - 1, migrate_stuck_heavy_stage_schema)


# Review/people schema parity (schema 80). While schema 73 (review jobs) was unreleased its
# tables were edited in place, so databases that migrated through 73 earlier keep the old
# shapes and fail backup/restore validation against a fresh database. This converges every
# known old shape: the missing ``enrolled_people`` table, nullable review_jobs columns added
# later, and ``review_finding_decisions.separation_basis`` (NOT NULL, which SQLite cannot add):
# that table is rebuilt to the fresh DDL (immutability triggers included). Rows of the old shape
# are copied back with separation_basis='legacy_unrecorded': their basis was never recorded, and
# inventing distinct_person or single_owner_opt_in would forge separation-of-duties evidence.
# Idempotent. If another
# change owns this version when branches merge, raise REVIEW_PARITY_SCHEMA_VERSION (and
# LATEST_SCHEMA_VERSION and the fresh-database INSERT in store.py).
REVIEW_PARITY_SCHEMA_VERSION = 80

_REVIEW_PARITY_NULLABLE_COLUMNS = (
    ("review_jobs", "findings_actor_id", "TEXT"),
    ("review_jobs", "findings_separation_basis", "TEXT"),
    ("enrolled_people", "renewed_at", "TEXT"),
)


def _rebuild_review_decisions(connection: sqlite3.Connection, create_schema) -> None:
    from .store import StoreError

    guidance = (
        "Cannot migrate review finding decisions to schema 80 safely. Nothing was changed. "
        "File-copy the whole state directory (the database and its -wal/-shm files) first, "
        "then report this error with that copy."
    )
    columns = [row[1] for row in connection.execute("PRAGMA table_info(review_finding_decisions)")]
    has_basis = "separation_basis" in columns
    before = connection.execute("SELECT count(*) FROM review_finding_decisions").fetchone()[0]
    fk_before = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    connection.execute("DROP TABLE IF EXISTS temp._review_decisions_old")
    connection.execute(
        "CREATE TEMP TABLE _review_decisions_old AS SELECT * FROM review_finding_decisions"
    )
    connection.execute("DROP TABLE review_finding_decisions")  # its triggers go with it
    create_schema(connection)
    keep = [c for c in columns if c != "separation_basis"]
    names = ",".join(keep)
    basis = "separation_basis" if has_basis else "'legacy_unrecorded'"
    connection.execute(
        f"INSERT INTO review_finding_decisions ({names},separation_basis) "
        f"SELECT {names},{basis} FROM temp._review_decisions_old"
    )
    after = connection.execute("SELECT count(*) FROM review_finding_decisions").fetchone()[0]
    fk_after = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    connection.execute("DROP TABLE temp._review_decisions_old")
    if after != before or fk_after > fk_before:
        raise StoreError(
            f"{guidance} (rows {before} -> {after}, foreign key problems "
            f"{fk_before} -> {fk_after})."
        )


def migrate_review_parity_schema(connection: sqlite3.Connection) -> None:
    from .people import create_people_schema
    from .review_jobs import create_review_jobs_schema

    decision_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='review_finding_decisions'"
    ).fetchone()
    if decision_sql is not None and "legacy_unrecorded" not in (decision_sql[0] or ""):
        _rebuild_review_decisions(connection, create_review_jobs_schema)
    create_review_jobs_schema(connection)
    create_people_schema(connection)
    for table, column, definition in _REVIEW_PARITY_NULLABLE_COLUMNS:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (REVIEW_PARITY_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(REVIEW_PARITY_SCHEMA_VERSION - 1, migrate_review_parity_schema)


# code-review-submit (ADR 0108)
# Adds code_review_sources (the git source of a code review submitted from a platform branch)
# with no-update and no-delete triggers. Idempotent; registered with setdefault. If another change
# owns this version when branches merge, raise CODE_REVIEW_SUBMIT_SCHEMA_VERSION (and
# LATEST_SCHEMA_VERSION and the fresh-database INSERT in store.py).
CODE_REVIEW_SUBMIT_SCHEMA_VERSION = 81


def migrate_code_review_submit_schema(connection: sqlite3.Connection) -> None:
    from .code_review_jobs import create_code_review_sources_schema

    preexisting = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    create_code_review_sources_schema(connection)
    if len(connection.execute("PRAGMA foreign_key_check").fetchall()) > preexisting:
        raise ValueError("Migration v81 introduced foreign key violations.")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (CODE_REVIEW_SUBMIT_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(CODE_REVIEW_SUBMIT_SCHEMA_VERSION - 1, migrate_code_review_submit_schema)


# Attempt result assessment (README step 3): an immutable researcher assessment of a succeeded
# admitted attempt, kept apart from operational failure. Additive and idempotent; registered
# with setdefault. This single constant carries the version: if another change owns it when
# branches merge, raise ATTEMPT_ASSESSMENT_SCHEMA_VERSION, LATEST_SCHEMA_VERSION (the literal
# near the top) and the fresh-database INSERT in store.py. The version is the one the number
# allocator reserved (82); versions 80 and 81 belong to other work and must be registered
# before this one ships (a database cannot skip a version: it is refused as unsupported).
# Like every schema bump here it clears the backup-health success markers, so the next backup
# is taken on the new schema.
ATTEMPT_ASSESSMENT_SCHEMA_VERSION = 82


def migrate_attempt_assessment_schema(connection: sqlite3.Connection) -> None:
    from .experiments import create_attempt_result_assessment_schema

    create_attempt_result_assessment_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (ATTEMPT_ASSESSMENT_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(ATTEMPT_ASSESSMENT_SCHEMA_VERSION - 1, migrate_attempt_assessment_schema)


# Design environment and metric (schema 83). Additive and idempotent: a nullable environment
# digest on each design variant and a nullable headline metric name on each design revision.
# Existing rows read as NULL and are never relabelled. This is the version reserved for this
# change (ADR 0118); the allocator's migration number cannot be used because the registry
# requires a contiguous chain. If another change owns this version when branches merge, raise
# DESIGN_ENVIRONMENT_SCHEMA_VERSION and LATEST_SCHEMA_VERSION and the fresh-database INSERT
# in store.py.
DESIGN_ENVIRONMENT_SCHEMA_VERSION = 83


def migrate_design_environment_schema(connection: sqlite3.Connection) -> None:
    additions = (
        (
            "research_experiment_variants",
            "environment_digest",
            "environment_digest TEXT CHECK (environment_digest IS NULL OR "
            "(length(environment_digest)=64 AND environment_digest NOT GLOB '*[^0-9a-f]*'))",
        ),
        ("research_experiment_designs", "metric", "metric TEXT"),
    )
    for table, column, definition in additions:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (DESIGN_ENVIRONMENT_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(DESIGN_ENVIRONMENT_SCHEMA_VERSION - 1, migrate_design_environment_schema)


# Smoke credential grants: a short-lived, local-docker-only, $0-budget agent credential paired
# with a narrowly scoped approver credential (see smoke_credentials.py). Additive and idempotent;
# registered with setdefault. The number is renumbered when it merges after other schema changes
# (raise SMOKE_CREDENTIALS_SCHEMA_VERSION, LATEST_SCHEMA_VERSION and the INSERT in store.py).
SMOKE_CREDENTIALS_SCHEMA_VERSION = 84


def migrate_smoke_credentials_schema(connection: sqlite3.Connection) -> None:
    from .smoke_credentials import create_smoke_credential_schema

    preexisting = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    create_smoke_credential_schema(connection)
    if len(connection.execute("PRAGMA foreign_key_check").fetchall()) > preexisting:
        raise ValueError("Migration v84 introduced foreign key violations.")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (SMOKE_CREDENTIALS_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(SMOKE_CREDENTIALS_SCHEMA_VERSION - 1, migrate_smoke_credentials_schema)


# Licence labels on snapshots (ADR 0113). Additive and idempotent. The branch reserved 86, but
# the registry requires a contiguous chain, so it takes the next free version (85) after the
# smoke credential grants (84). If another change owns this version when branches merge, raise
# LICENSE_SCHEMA_VERSION (and LATEST_SCHEMA_VERSION and the fresh-database INSERT in store.py).
LICENSE_SCHEMA_VERSION = 85


def migrate_license_schema(connection: sqlite3.Connection) -> None:
    from .license_labels import create_license_schema

    create_license_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1", (LICENSE_SCHEMA_VERSION,)
    )


MIGRATIONS.setdefault(LICENSE_SCHEMA_VERSION - 1, migrate_license_schema)


# Owner-signed approval delegations (ADR 0122, schema 86): the delegation record, its
# append-only decisions, and a nullable delegation_id on campaign approvals and gate decisions.
# Additive and idempotent. The next version after the licence labels (85); if another change owns
# it when branches merge, raise APPROVAL_DELEGATION_SCHEMA_VERSION, LATEST_SCHEMA_VERSION (the
# literal near the top) and the fresh-database INSERT in store.py.
APPROVAL_DELEGATION_SCHEMA_VERSION = 86


def migrate_approval_delegation_schema(connection: sqlite3.Connection) -> None:
    from .approval_delegations import create_delegation_schema

    create_delegation_schema(connection)
    for table in ("campaign_approvals", "campaign_gate_decisions"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "delegation_id" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN delegation_id TEXT")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (APPROVAL_DELEGATION_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(APPROVAL_DELEGATION_SCHEMA_VERSION - 1, migrate_approval_delegation_schema)


# The control-side table of what control read from the guardian's per-frame record (ADR 0041
# section 7, schema 87). Additive and idempotent. The next version after the approval delegations
# (86); it was reserved as 93 on its branch and renumbered at merge. If another change owns it
# when branches merge, raise GUARDIAN_RECORD_SCHEMA_VERSION, LATEST_SCHEMA_VERSION (the literal
# near the top) and the fresh-database INSERT in store.py.
GUARDIAN_RECORD_SCHEMA_VERSION = 87


def migrate_guardian_record_schema(connection: sqlite3.Connection) -> None:
    from .execution_frames import create_execution_frame_schema

    create_execution_frame_schema(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (GUARDIAN_RECORD_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(GUARDIAN_RECORD_SCHEMA_VERSION - 1, migrate_guardian_record_schema)


# Model calls charged to an agent authority grant (ADR 0142, schema 88): an append-only link from
# a model-spend call to the grant that pays for it; the grant's spend is derived from the ledger
# rows it links. Additive and idempotent. The next version after the guardian record (87); if
# another change owns it when branches merge, raise GRANT_MODEL_SPEND_SCHEMA_VERSION,
# LATEST_SCHEMA_VERSION (the literal near the top) and the fresh-database INSERT in store.py.
GRANT_MODEL_SPEND_SCHEMA_VERSION = 88


def migrate_grant_model_spend_schema(connection: sqlite3.Connection) -> None:
    from .grant_model_spend import create_grant_model_spend_schema

    create_grant_model_spend_schema(connection)
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (GRANT_MODEL_SPEND_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(GRANT_MODEL_SPEND_SCHEMA_VERSION - 1, migrate_grant_model_spend_schema)


# The project concurrency ceiling (ADR 0171, schema 89): rebuild execution_project_limits so its
# CHECK is the hard cap (1 to 64) instead of 1 to 8; the operator's ceiling (default 8) is enforced
# where a request is accepted. Rows are copied unchanged. Reserved as 102 on its branch; the
# registry requires a contiguous chain, so it takes the next free version (89). If another change
# owns it when branches merge, raise PROJECT_CONCURRENCY_SCHEMA_VERSION, LATEST_SCHEMA_VERSION (the
# literal near the top) and the fresh-database INSERT in store.py.
PROJECT_CONCURRENCY_SCHEMA_VERSION = 89


def migrate_project_concurrency_schema(connection: sqlite3.Connection) -> None:
    from .execution import PROJECT_LIMITS_TABLE_SQL
    from .store import StoreError

    current = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='execution_project_limits'"
    ).fetchone()
    if current is None:
        connection.execute(PROJECT_LIMITS_TABLE_SQL)
    elif current[0] != PROJECT_LIMITS_TABLE_SQL:
        before = connection.execute(
            "SELECT * FROM execution_project_limits ORDER BY project_id"
        ).fetchall()
        fk_before = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        columns = (
            "project_id,revision,currency,monetary_limit_cents,max_concurrency,"
            "held_cents,settled_cents,created_at,updated_at"
        )
        connection.execute("DROP TABLE IF EXISTS temp._project_limits_old")
        connection.execute(
            f"CREATE TEMP TABLE _project_limits_old AS SELECT {columns} "
            "FROM execution_project_limits"
        )
        connection.execute("DROP TABLE execution_project_limits")
        connection.execute(PROJECT_LIMITS_TABLE_SQL)
        connection.execute(
            f"INSERT INTO execution_project_limits ({columns}) "
            f"SELECT {columns} FROM temp._project_limits_old"
        )
        connection.execute("DROP TABLE temp._project_limits_old")
        after = connection.execute(
            "SELECT * FROM execution_project_limits ORDER BY project_id"
        ).fetchall()
        fk_after = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if [tuple(row) for row in after] != [tuple(row) for row in before] or fk_after > fk_before:
            raise StoreError(
                "Cannot migrate project execution limits to schema 89 safely. Nothing was "
                "changed. File-copy the whole state directory (the database and its -wal/-shm "
                "files) first, then report this error with that copy."
            )
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (PROJECT_CONCURRENCY_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(PROJECT_CONCURRENCY_SCHEMA_VERSION - 1, migrate_project_concurrency_schema)


# Recorded stage outputs keep their licence labels (ADR 0113 amendment 2026-11-23, schema 90):
# one add-only row per campaign stage. Additive and idempotent. Reserved as 100; it takes the next
# contiguous version after the project concurrency ceiling (89). If another change owns it when
# branches merge, raise OUTPUT_LICENSE_SCHEMA_VERSION, LATEST_SCHEMA_VERSION (the literal near the
# top) and the fresh-database INSERT in store.py.
OUTPUT_LICENSE_SCHEMA_VERSION = 90


def migrate_output_license_schema(connection: sqlite3.Connection) -> None:
    from .license_labels import create_output_license_schema

    create_output_license_schema(connection)
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (OUTPUT_LICENSE_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(OUTPUT_LICENSE_SCHEMA_VERSION - 1, migrate_output_license_schema)


# Configurable review tiers (schema 91; migration number 101 reserved). Relaxes the review_jobs
# tier CHECK from the four built-in names to a well-formed tier name (the configured set is
# checked by the store) by the same-id rebuild schema 75 uses, and adds review_job_tier_grants
# (the grant a tier approval binds, append-only). Existing rows are copied unchanged, so every
# old tier stays valid. Idempotent. The next contiguous version after the output licence labels
# (90); if another change owns it when branches merge, raise REVIEW_TIERS_SCHEMA_VERSION,
# LATEST_SCHEMA_VERSION (the literal near the top) and the fresh-database INSERT in store.py.
REVIEW_TIERS_SCHEMA_VERSION = 91


def migrate_review_tiers_schema(connection: sqlite3.Connection) -> None:
    from .review_jobs import create_review_jobs_schema

    preexisting = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='review_jobs'"
    ).fetchone()
    if row is not None and "tier IN (" in row[0]:
        before = connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0]
        connection.execute("PRAGMA defer_foreign_keys=ON")
        old_columns = [r[1] for r in connection.execute("PRAGMA table_info(review_jobs)")]
        connection.execute("CREATE TABLE review_jobs_v88 AS SELECT * FROM review_jobs")
        connection.execute("DROP TABLE review_jobs")
        create_review_jobs_schema(connection)
        new_columns = {r[1] for r in connection.execute("PRAGMA table_info(review_jobs)")}
        copied = ",".join(c for c in old_columns if c in new_columns)
        connection.execute(
            f"INSERT INTO review_jobs ({copied}) SELECT {copied} FROM review_jobs_v88"
        )
        connection.execute("DROP TABLE review_jobs_v88")
        after = connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0]
        if after != before:
            raise ValueError("Migration v91 changed the number of review jobs.")
    create_review_jobs_schema(connection)
    if len(connection.execute("PRAGMA foreign_key_check").fetchall()) > preexisting:
        raise ValueError("Migration v91 introduced foreign key violations.")
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (REVIEW_TIERS_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(REVIEW_TIERS_SCHEMA_VERSION - 1, migrate_review_tiers_schema)


# Bounded output-delivery lookups (ADR 0190, schema 92; migration number 108 reserved): indexes on
# events(event_type, cursor), execution_observations(lease_id), a partial index of confirmed exits
# and snapshot_chunk_uploads(status, expires_at), so a run's observation posts, uploads, manifest
# and run receipt no longer read whole growing tables inside the write lock. Additive and
# idempotent; no row changes. The next contiguous version after the review tiers (91); if another
# change owns it when branches merge, raise OUTPUT_LOOKUP_INDEXES_SCHEMA_VERSION,
# LATEST_SCHEMA_VERSION (the literal near the top) and the fresh-database INSERT in store.py.
OUTPUT_LOOKUP_INDEXES_SCHEMA_VERSION = 92


def migrate_output_lookup_indexes(connection: sqlite3.Connection) -> None:
    from .output_lookup_indexes import create_output_lookup_indexes

    create_output_lookup_indexes(connection)
    connection.execute(
        """UPDATE operator_backup_health SET last_success_at=NULL,last_backup_id=NULL
        WHERE singleton=1 AND EXISTS (
            SELECT 1 FROM control_recovery WHERE singleton=1 AND state='active'
        )"""
    )
    connection.execute(
        "UPDATE schema_meta SET schema_version=? WHERE singleton=1",
        (OUTPUT_LOOKUP_INDEXES_SCHEMA_VERSION,),
    )


MIGRATIONS.setdefault(OUTPUT_LOOKUP_INDEXES_SCHEMA_VERSION - 1, migrate_output_lookup_indexes)
