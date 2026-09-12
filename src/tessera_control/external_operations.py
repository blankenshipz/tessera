"""Schema for durable, provider-facing operation intent and receipt records.

The control plane records one operation before returning an executable package.
Intent and provider receipt evidence are deliberately separate: an operation
may have no provider receipt, especially after a lost submit reply.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

# This order makes returned evidence deterministic. It is presentation order,
# never authority precedence for evidence at an equal observed time.
RECONCILIATION_SOURCE_ORDER = ("receipt", "worker")


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


def create_kubernetes_dispatch_schema(connection: sqlite3.Connection) -> None:
    """Install empty durable package and Kubernetes dispatch records.

    A package remains control-issued authority.  The dispatch row is a separate
    human decision that binds that immutable package to one already identified
    Kubernetes target; it is not a provider receipt or a Kubernetes credential.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS execution_runtime_packages (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        package_digest TEXT NOT NULL CHECK (
            length(package_digest)=64 AND package_digest NOT GLOB '*[^0-9a-f]*'),
        package_json TEXT NOT NULL, created_at TEXT NOT NULL)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS kubernetes_dispatches (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES external_operations(operation_id),
        admitted_attempt_id TEXT NOT NULL UNIQUE
            REFERENCES research_experiment_admitted_attempts(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL UNIQUE REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        package_digest TEXT NOT NULL CHECK (
            length(package_digest)=64 AND package_digest NOT GLOB '*[^0-9a-f]*'),
        target_digest TEXT NOT NULL CHECK (
            length(target_digest)=64 AND target_digest NOT GLOB '*[^0-9a-f]*'),
        profile_version TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued','claimed','cancel_requested')),
        claimed_at TEXT, claimed_by_principal_id TEXT REFERENCES principals(id),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS kubernetes_dispatches_by_worker "
        "ON kubernetes_dispatches(worker_principal_id,state,created_at,id)"
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS kubernetes_dispatches_fence_cancel
        AFTER UPDATE OF desired_state ON external_operations
        WHEN NEW.desired_state='cancel_requested'
        BEGIN
          UPDATE kubernetes_dispatches SET state='cancel_requested',updated_at=NEW.updated_at
          WHERE operation_id=NEW.operation_id AND state IN ('queued','claimed');
        END"""
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
        observation_sequence INTEGER NOT NULL DEFAULT 1 CHECK (observation_sequence >= 1),
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
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(external_operation_receipts)")
    }
    if "observation_sequence" not in columns:
        connection.execute(
            "ALTER TABLE external_operation_receipts ADD COLUMN observation_sequence "
            "INTEGER NOT NULL DEFAULT 1"
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


def current_operation_reconciliation(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, object] | None:
    """Return the fail-closed current observation across worker and provider sources.

    Each source is ordered by its reported ``observed_at`` and an opaque cursor:
    a worker cursor is its immutable observation ID, and a receipt cursor is its
    monotonically increasing receipt observation sequence.  Cross-source order is
    by ``observed_at`` only.  At the same timestamp no source is preferred: an
    ``unknown``, ``running``, or unconfirmed exit contradicts a confirmed exit and
    therefore blocks terminal reconciliation. ``contradiction_state`` explicitly
    distinguishes an equal-time conflict from a later adverse observation.
    """
    worker = connection.execute(
        """SELECT id,observed_state,observed_at,termination_confirmed FROM execution_observations
        WHERE operation_id=? ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    receipt = connection.execute(
        """SELECT observed_state,observed_at,termination_confirmed,observation_sequence
        FROM external_operation_receipts WHERE operation_id=?""",
        (operation_id,),
    ).fetchone()
    sources: list[dict[str, object]] = []
    if worker is not None:
        sources.append(
            {
                "source": "worker",
                "cursor": f"observation:{worker['id']}",
                "observed_state": worker["observed_state"],
                "observed_at": worker["observed_at"],
                "termination_confirmed": bool(worker["termination_confirmed"]),
            }
        )
    if receipt is not None:
        sources.append(
            {
                "source": "receipt",
                "cursor": f"receipt:{operation_id}:{receipt['observation_sequence']}",
                "observed_state": receipt["observed_state"],
                "observed_at": receipt["observed_at"],
                "termination_confirmed": bool(receipt["termination_confirmed"]),
            }
        )
    if not sources:
        return None

    def observed_time(item: dict[str, object]) -> datetime:
        return datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00"))

    source_rank = {source: rank for rank, source in enumerate(RECONCILIATION_SOURCE_ORDER)}
    sources.sort(
        key=lambda item: (
            observed_time(item),
            source_rank[str(item["source"])],
            str(item["cursor"]),
        )
    )
    latest_time = max(observed_time(item) for item in sources)
    latest = [item for item in sources if observed_time(item) == latest_time]
    latest_at = str(latest[0]["observed_at"])

    def confirmed_exit(item: dict[str, object]) -> bool:
        return item["observed_state"] == "exited" and bool(item["termination_confirmed"])

    latest_confirmed_exit = all(confirmed_exit(item) for item in latest)
    adverse_latest = any(
        item["observed_state"] != "exited" or not item["termination_confirmed"] for item in latest
    )
    prior_confirmed_exit = any(
        observed_time(item) < latest_time and confirmed_exit(item) for item in sources
    )
    if latest_confirmed_exit:
        observed_state, termination_confirmed = "exited", True
    elif any(item["observed_state"] == "unknown" for item in latest):
        observed_state, termination_confirmed = "unknown", False
    elif any(item["observed_state"] == "running" for item in latest):
        observed_state, termination_confirmed = "running", False
    else:
        observed_state, termination_confirmed = "exited", False
    contradiction_state = (
        "same_time_contradiction"
        if adverse_latest and any(confirmed_exit(item) for item in latest)
        else "later_adverse_observation"
        if adverse_latest and prior_confirmed_exit
        else "unconfirmed_current_observation"
        if adverse_latest
        else "none"
    )
    cursor = (
        str(latest[0]["cursor"])
        if len(latest) == 1
        else "reconciliation:" + ":".join(sorted(str(item["cursor"]) for item in latest))
    )
    return {
        "observed_state": observed_state,
        "observed_at": latest_at,
        "termination_confirmed": termination_confirmed,
        "cursor": cursor,
        "sources": sources,
        "source_order": list(RECONCILIATION_SOURCE_ORDER),
        "contradiction_state": contradiction_state,
    }
