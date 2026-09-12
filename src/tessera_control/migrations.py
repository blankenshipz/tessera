"""Explicit, transactional SQLite schema migrations for the local control plane."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

LATEST_SCHEMA_VERSION = 36
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
