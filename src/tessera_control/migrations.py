"""Explicit, transactional SQLite schema migrations for the local control plane."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

LATEST_SCHEMA_VERSION = 14
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
