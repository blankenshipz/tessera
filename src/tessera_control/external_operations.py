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
RECONCILIATION_SOURCE_ORDER = ("receipt", "recovery_observer", "worker")


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


def create_execution_recovery_schema(connection: sqlite3.Connection) -> None:
    """Install empty, operation-bound loss recovery records.

    Recovery evidence is intentionally separate from worker observations: the
    original worker can no longer use its credential, and an observer must not
    be represented as that worker.  One recovery row can exist only for the
    original immutable operation identity.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS execution_recovery_reconciliations (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        operation_id TEXT NOT NULL UNIQUE REFERENCES external_operations(operation_id),
        original_worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        identity_loss_reason TEXT NOT NULL CHECK (identity_loss_reason IN
            ('credential_revoked','credential_expired','principal_revoked','worker_retired','registry_missing')),
        state TEXT NOT NULL CHECK (state IN ('identity_lost','reconciliation_required',
            'cancel_requested','independently_observed','terminal_confirmed','unresolved')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        cancellation_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancellation_requested IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        closed_at TEXT, closed_by_principal_id TEXT REFERENCES principals(id)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS execution_recovery_by_grant
        ON execution_recovery_reconciliations(grant_id, created_at, id)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS execution_recovery_observations (
        id TEXT PRIMARY KEY,
        recovery_id TEXT NOT NULL REFERENCES execution_recovery_reconciliations(id),
        observer_principal_id TEXT NOT NULL REFERENCES principals(id),
        action TEXT NOT NULL CHECK (action IN ('lookup','cancel_lookup')),
        evidence_reference TEXT NOT NULL CHECK (length(evidence_reference) BETWEEN 1 AND 200),
        evidence_digest TEXT NOT NULL CHECK (length(evidence_digest)=64
            AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
        provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 200),
        observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown','running','exited')),
        observed_at TEXT NOT NULL, termination_confirmed INTEGER NOT NULL CHECK
            (termination_confirmed IN (0,1)), recorded_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS execution_recovery_observations_by_recovery
        ON execution_recovery_observations(recovery_id, recorded_at, id)"""
    )


def confirmed_exit_code(connection: sqlite3.Connection, operation_id: str) -> int | None:
    """The exit code the worker reported with its confirmed exit of this operation, if any.

    Evidence only: callers still require ``current_operation_reconciliation`` to say the exit
    is confirmed and uncontradicted. The worker reports the code before it collects outputs,
    so a run that failed without leaving outputs (and so never posted a run receipt) still
    has its code on record (ADR 0063). It is read from the observation event, so no schema
    changes."""
    row = connection.execute(
        """SELECT json_extract(payload_json,'$.exit_code') AS exit_code FROM events
        WHERE event_type='execution.observation_reported'
        AND json_extract(payload_json,'$.operation_id')=?
        AND json_extract(payload_json,'$.observed_state')='exited'
        AND json_extract(payload_json,'$.termination_confirmed')=1
        AND json_extract(payload_json,'$.exit_code') IS NOT NULL
        ORDER BY cursor DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    return None if row is None else int(row["exit_code"])


def current_operation_reconciliation(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, object] | None:
    """Return the fail-closed current observation across worker and provider sources.

    Each source is ordered by its parsed ``observed_at`` and an opaque cursor:
    a worker cursor is its immutable observation ID, and a receipt cursor is its
    monotonically increasing receipt observation sequence.  Cross-source order is
    by ``observed_at`` only.  At the same timestamp no source is preferred: an
    ``unknown``, ``running``, or unconfirmed exit contradicts a confirmed exit and
    therefore blocks terminal reconciliation. ``contradiction_state`` explicitly
    distinguishes an equal-time conflict from a later adverse observation.
    """
    worker = connection.execute(
        """SELECT id,observed_state,observed_at,termination_confirmed FROM execution_observations
        WHERE operation_id=? ORDER BY rowid""",
        (operation_id,),
    ).fetchall()
    receipt = connection.execute(
        """SELECT observed_state,observed_at,termination_confirmed,observation_sequence
        FROM external_operation_receipts WHERE operation_id=?""",
        (operation_id,),
    ).fetchone()
    recovery = connection.execute(
        """SELECT o.id,o.observed_state,o.observed_at,o.termination_confirmed
        FROM execution_recovery_observations o
        JOIN execution_recovery_reconciliations r ON r.id=o.recovery_id
        WHERE r.operation_id=? ORDER BY o.rowid""",
        (operation_id,),
    ).fetchall()
    sources: list[dict[str, object]] = []
    for observation in worker:
        sources.append(
            {
                "source": "worker",
                "cursor": f"observation:{observation['id']}",
                "observed_state": observation["observed_state"],
                "observed_at": observation["observed_at"],
                "termination_confirmed": bool(observation["termination_confirmed"]),
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
    for observation in recovery:
        sources.append(
            {
                "source": "recovery_observer",
                "cursor": f"recovery-observation:{observation['id']}",
                "observed_state": observation["observed_state"],
                "observed_at": observation["observed_at"],
                "termination_confirmed": bool(observation["termination_confirmed"]),
            }
        )
    if not sources:
        return None

    def observed_time(item: dict[str, object]) -> datetime:
        return datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00"))

    def confirmed_exit(item: dict[str, object]) -> bool:
        return item["observed_state"] == "exited" and bool(item["termination_confirmed"])

    # Existing records contain variable precision and equivalent UTC offsets.
    # TEXT MAX and SQLite floating-point Julian days both lose exact ordering.
    # Parse history before selecting each source's latest instant; retain every
    # equal-time sample so alternate encodings cannot hide contradictions.
    latest_time = max(observed_time(item) for item in sources)
    prior_confirmed_exit = any(
        observed_time(item) < latest_time and confirmed_exit(item) for item in sources
    )
    source_times = {
        source: max(observed_time(item) for item in sources if item["source"] == source)
        for source in {str(item["source"]) for item in sources}
    }
    sources = [item for item in sources if observed_time(item) == source_times[str(item["source"])]]
    source_rank = {source: rank for rank, source in enumerate(RECONCILIATION_SOURCE_ORDER)}
    sources.sort(
        key=lambda item: (
            observed_time(item),
            source_rank[str(item["source"])],
            str(item["cursor"]),
        )
    )
    latest = [item for item in sources if observed_time(item) == latest_time]
    latest_at = str(latest[0]["observed_at"])

    latest_confirmed_exit = all(confirmed_exit(item) for item in latest)
    adverse_latest = any(
        item["observed_state"] != "exited" or not item["termination_confirmed"] for item in latest
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


# Evidence classes are disjoint by construction: the fake receipt table can hold
# only `fake`, and this table can never hold it, so a real run cannot be
# mislabelled synthetic and a fake one cannot be relabelled real.
RUN_RECEIPT_PROVIDER_KINDS = ("local_docker_cpu", "remote_kubernetes", "remote_salad")
RUN_RECEIPT_EVIDENCE = "observed_provider_execution"


def create_execution_run_receipt_schema(connection: sqlite3.Connection) -> None:
    """Install empty non-fake run receipts beside, never inside, fake receipts.

    A run receipt attributes one terminal observation of a real provider (exit
    code, command digest, wall-clock window, output digest) to one immutable
    external operation.  It is evidence that a process ran and exited, not of
    provider success, artifact release or scientific validity.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS execution_run_receipts (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        provider_kind TEXT NOT NULL CHECK (
            provider_kind IN ('local_docker_cpu', 'remote_kubernetes', 'remote_salad')
        ),
        receipt_evidence TEXT NOT NULL CHECK (receipt_evidence = 'observed_provider_execution'),
        adapter_version TEXT NOT NULL CHECK (length(adapter_version) BETWEEN 1 AND 100),
        provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 200),
        command_digest TEXT NOT NULL CHECK (
            length(command_digest) = 64 AND command_digest NOT GLOB '*[^0-9a-f]*'
        ),
        exit_code INTEGER NOT NULL CHECK (exit_code BETWEEN -2147483648 AND 2147483647),
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
        output_digest TEXT NOT NULL CHECK (
            length(output_digest) = 64 AND output_digest NOT GLOB '*[^0-9a-f]*'
        ),
        receipt_payload_json TEXT NOT NULL CHECK (length(receipt_payload_json) <= 65536),
        receipt_digest TEXT NOT NULL CHECK (
            length(receipt_digest) = 64 AND receipt_digest NOT GLOB '*[^0-9a-f]*'
        ),
        provider_success TEXT NOT NULL CHECK (provider_success = 'not_established'),
        scientific_validity TEXT NOT NULL CHECK (scientific_validity = 'not_assessed'),
        created_at TEXT NOT NULL,
        CHECK (finished_at >= started_at),
        UNIQUE(provider_kind, adapter_version, provider_id)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS execution_run_receipts_by_project
        ON execution_run_receipts(project_id, created_at, operation_id)"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS execution_run_receipts_append_only_update
        BEFORE UPDATE ON execution_run_receipts
        BEGIN SELECT RAISE(ABORT, 'execution run receipts are immutable'); END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS execution_run_receipts_append_only_delete
        BEFORE DELETE ON execution_run_receipts
        BEGIN SELECT RAISE(ABORT, 'execution run receipts are immutable'); END"""
    )
    # One operation has at most one evidence class, enforced in both directions.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS execution_run_receipts_exclude_fake
        BEFORE INSERT ON execution_run_receipts
        WHEN EXISTS (SELECT 1 FROM external_operation_receipts WHERE operation_id=NEW.operation_id)
        BEGIN SELECT RAISE(ABORT, 'operation already has fake provider evidence'); END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS external_operation_receipts_exclude_run
        BEFORE INSERT ON external_operation_receipts
        WHEN EXISTS (SELECT 1 FROM execution_run_receipts WHERE operation_id=NEW.operation_id)
        BEGIN SELECT RAISE(ABORT, 'operation already has observed provider evidence'); END"""
    )
