"""Schema for durable, provider-facing operation intent and receipt records.

The control plane records one operation before returning an executable package.
Intent and provider receipt evidence are deliberately separate: an operation
may have no provider receipt, especially after a lost submit reply.
"""

from __future__ import annotations

import sqlite3


def create_external_operation_schema(connection: sqlite3.Connection) -> None:
    """Install v18 operation intent records without fabricating legacy history."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS external_operations (
        operation_id TEXT PRIMARY KEY CHECK (
            length(operation_id) = 32 AND operation_id NOT GLOB '*[^0-9a-f]*'
        ),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL UNIQUE REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        package_digest TEXT NOT NULL CHECK (
            length(package_digest) = 64 AND package_digest NOT GLOB '*[^0-9a-f]*'
        ),
        adapter_version TEXT NOT NULL,
        profile_version TEXT NOT NULL,
        docker_lookup_key TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL CHECK (
            length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'
        ),
        retry_class TEXT NOT NULL CHECK (retry_class = 'manual_reconciliation'),
        desired_state TEXT NOT NULL CHECK (desired_state IN ('admitted', 'cancel_requested')),
         observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown', 'running', 'exited')),
          provider_id TEXT,
          created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
        )"""
    )


def create_experiment_operation_binding_schema(connection: sqlite3.Connection) -> None:
    """Add nullable immutable experiment identity to new operation intents only."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(external_operations)")}
    for name, definition in (
        ("experiment_attempt_id", "TEXT"),
        ("experiment_id", "TEXT"),
        ("experiment_design_revision", "INTEGER"),
        ("experiment_variant_id", "TEXT"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE external_operations ADD COLUMN {name} {definition}")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS external_operations_experiment_attempt_once
        ON external_operations(experiment_attempt_id) WHERE experiment_attempt_id IS NOT NULL"""
    )


def create_external_operation_receipt_schema(connection: sqlite3.Connection) -> None:
    """Install v19 receipt records without assigning evidence to v18 rows."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS external_operation_receipts (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        provider_kind TEXT NOT NULL CHECK (provider_kind = 'fake'),
        provider_version TEXT NOT NULL CHECK (length(provider_version) BETWEEN 1 AND 100),
        provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 200),
        receipt_payload_json TEXT NOT NULL CHECK (length(receipt_payload_json) <= 65536),
        receipt_digest TEXT NOT NULL CHECK (
            length(receipt_digest) = 64 AND receipt_digest NOT GLOB '*[^0-9a-f]*'
        ),
        observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown', 'running', 'exited')),
        observed_at TEXT NOT NULL,
        termination_confirmed INTEGER NOT NULL CHECK (termination_confirmed IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(provider_kind, provider_version, provider_id)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS external_operation_receipts_by_provider
        ON external_operation_receipts(provider_kind, provider_id)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS external_operations_by_project
        ON external_operations(project_id, created_at, operation_id)"""
    )
    # Existing v17 observations have no operation receipt.  Keep that explicit
    # null boundary rather than attributing them to an invented operation.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(execution_observations)")}
    if "operation_id" not in columns:
        connection.execute("ALTER TABLE execution_observations ADD COLUMN operation_id TEXT")
    connection.execute(
        """CREATE INDEX IF NOT EXISTS execution_observations_by_operation
        ON execution_observations(operation_id, recorded_at, id)"""
    )
