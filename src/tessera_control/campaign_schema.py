"""Campaign tables (ADR 0038): specs, approvals, stages, attempts, intents, receipts."""

from __future__ import annotations

import sqlite3

_STAGE_STATUSES = (
    "'pending','preparing','running','succeeded','failed',"
    "'refused_budget','blocked_unknown','cancelled','skipped'"
)
_ATTEMPT_STATUSES = (
    "'reserved','granted','admitted','running','exited','collected','resolved','measured',"
    "'reconciled','succeeded','failed_transient','failed_permanent','unknown',"
    "'refused_budget','cancelled'"
)
_CAMPAIGN_STATUSES = (
    "'submitted','running','paused_delegation_lapsed','stopping','cancel_requested',"
    "'blocked','succeeded','stopped','failed','cancelled'"
)


def create_campaign_schema(connection: sqlite3.Connection) -> None:
    """Install the campaign tables; idempotent so v62 and fresh databases share it."""
    statements = (
        f"""CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        spec_json TEXT NOT NULL,
        spec_digest TEXT NOT NULL CHECK(length(spec_digest)=64
            AND spec_digest NOT GLOB '*[^0-9a-f]*'),
        status TEXT NOT NULL CHECK(status IN ({_CAMPAIGN_STATUSES})),
        stop_reason TEXT CHECK(stop_reason IS NULL OR length(stop_reason) BETWEEN 1 AND 300),
        budget_cap_cents INTEGER NOT NULL CHECK(typeof(budget_cap_cents)='integer'
            AND budget_cap_cents BETWEEN 0 AND 5000),
        held_cents INTEGER NOT NULL DEFAULT 0 CHECK(held_cents >= 0),
        settled_cents INTEGER NOT NULL DEFAULT 0 CHECK(settled_cents >= 0),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        submitted_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        submitted_credential_id TEXT REFERENCES credentials(id),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS campaigns_by_project ON campaigns(project_id, created_at, id)",
        """CREATE TABLE IF NOT EXISTS campaign_approvals (
        id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        spec_digest TEXT NOT NULL,
        approving_principal_id TEXT NOT NULL REFERENCES principals(id),
        decision_class TEXT NOT NULL, automation_label TEXT,
        approved_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','superseded','revoked')),
        ended_at TEXT, trust_policy_digest TEXT, delegation_id TEXT)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS campaign_approvals_one_active
        ON campaign_approvals(campaign_id) WHERE status='active'""",
        f"""CREATE TABLE IF NOT EXISTS campaign_stages (
        campaign_id TEXT NOT NULL REFERENCES campaigns(id), stage_key TEXT NOT NULL,
        topo_index INTEGER NOT NULL CHECK(topo_index >= 0),
        status TEXT NOT NULL CHECK(status IN ({_STAGE_STATUSES})),
        prep_phase TEXT NOT NULL DEFAULT 'none' CHECK(prep_phase IN ('none','task_created',
            'planned','task_approved','input_ready','experiment_ready')),
        task_id TEXT REFERENCES tasks(id), task_revision INTEGER, plan_digest TEXT,
        experiment_id TEXT REFERENCES research_experiments(id), variant_id TEXT,
        design_revision INTEGER, variant_revision INTEGER,
        input_snapshot_id TEXT REFERENCES source_snapshots(id), input_snapshot_digest TEXT,
        input_data_class TEXT CHECK(input_data_class IS NULL
            OR input_data_class IN ('public','private_derived')),
        output_data_class TEXT NOT NULL CHECK(output_data_class IN ('public','private_derived')),
        attempts_used INTEGER NOT NULL DEFAULT 0 CHECK(attempts_used BETWEEN 0 AND 5),
        stop_results_json TEXT, revision INTEGER NOT NULL CHECK(revision >= 1),
        updated_at TEXT NOT NULL, provider_id TEXT,
        PRIMARY KEY(campaign_id, stage_key))""",
        f"""CREATE TABLE IF NOT EXISTS campaign_stage_attempts (
        id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        stage_key TEXT NOT NULL, attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 5),
        status TEXT NOT NULL CHECK(status IN ({_ATTEMPT_STATUSES})),
        hold_cents INTEGER NOT NULL CHECK(hold_cents >= 0),
        settled_cents INTEGER CHECK(settled_cents IS NULL OR settled_cents >= 0),
        release_id TEXT REFERENCES snapshot_releases(id),
        grant_id TEXT UNIQUE REFERENCES execution_grants(id),
        admitted_attempt_id TEXT REFERENCES research_experiment_admitted_attempts(id),
        operation_id TEXT, run_receipt_digest TEXT, exit_code INTEGER,
        salad_verdict TEXT CHECK(salad_verdict IS NULL OR salad_verdict IN ('ok','failed')),
        salad_evidence_digest TEXT,
        manifest_id TEXT, manifest_digest TEXT, measurements_json TEXT,
        failure_class TEXT,
        failure_detail TEXT CHECK(failure_detail IS NULL OR length(failure_detail) <= 500),
        settlement_basis TEXT CHECK(settlement_basis IS NULL OR settlement_basis IN
            ('local_zero','unclaimed_zero','human_charge','human_reconcile',
             'guardian_bounded_hold','provider_billed')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        provider_id TEXT, trust_policy_digest TEXT, admission_basis TEXT,
        settlement_request_digest TEXT,
        UNIQUE(campaign_id, stage_key, attempt),
        FOREIGN KEY(campaign_id, stage_key) REFERENCES campaign_stages(campaign_id, stage_key))""",
        """CREATE TABLE IF NOT EXISTS campaign_step_intents (
        id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        approval_id TEXT NOT NULL REFERENCES campaign_approvals(id),
        stage_key TEXT, attempt INTEGER, step TEXT NOT NULL,
        ordinal INTEGER NOT NULL DEFAULT 0,
        principal_id TEXT NOT NULL REFERENCES principals(id),
        method TEXT NOT NULL CHECK(method IN ('POST','PUT')), path TEXT NOT NULL,
        request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        resources_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','done','failed','abandoned')),
        response_status INTEGER, response_json TEXT, error_class TEXT,
        created_at TEXT NOT NULL, completed_at TEXT,
        UNIQUE(campaign_id, approval_id, stage_key, attempt, step, ordinal),
        UNIQUE(principal_id, idempotency_key, method, path))""",
        """CREATE TABLE IF NOT EXISTS campaign_receipts (
        campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id),
        receipt_json TEXT NOT NULL, receipt_digest TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS campaign_salad_stages (
        campaign_id TEXT NOT NULL, stage_key TEXT NOT NULL, profile TEXT NOT NULL,
        study_id TEXT, source_id TEXT, study_variant_id TEXT, split_lock_id TEXT,
        capability_id TEXT, revision INTEGER NOT NULL CHECK(revision >= 1),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(campaign_id, stage_key),
        FOREIGN KEY(campaign_id, stage_key) REFERENCES campaign_stages(campaign_id, stage_key))""",
        """CREATE TABLE IF NOT EXISTS campaign_salad_attempts (
        attempt_id TEXT PRIMARY KEY REFERENCES campaign_stage_attempts(id),
        phase TEXT NOT NULL CHECK(phase IN ('awaiting_lease','leased','awaiting_review',
            'reviewed','capability_ready','route_proposed','route_started','executing',
            'outcome_recorded','awaiting_charge','settled','unclaimed','unknown')),
        operation_id TEXT, lease_id TEXT, epoch INTEGER, package_digest TEXT,
        plan_decision_id TEXT, source_decision_id TEXT, route_id TEXT UNIQUE,
        proposal_digest TEXT, live_receipt_digest TEXT, live_receipt_json TEXT,
        manifest_id TEXT, provider_id TEXT, estimated_exposure TEXT,
        charged_cents INTEGER CHECK(charged_cents IS NULL OR charged_cents >= 0),
        salad_evidence_digest TEXT,
        guardian_final_json TEXT CHECK(guardian_final_json IS NULL
            OR length(guardian_final_json) <= 2000),
        settlement_confirmed_at TEXT,
        waiting_reason TEXT CHECK(waiting_reason IS NULL OR length(waiting_reason) <= 300),
        revision INTEGER NOT NULL CHECK(revision >= 1), updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS campaign_stage_io (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        campaign_id TEXT NOT NULL REFERENCES campaigns(id), stage_key TEXT NOT NULL,
        attempt_id TEXT REFERENCES campaign_stage_attempts(id),
        direction TEXT NOT NULL CHECK(direction IN ('input','output')),
        name TEXT NOT NULL, kind TEXT NOT NULL, media_type TEXT,
        sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        -- snapshot_id only names the snapshot (sha256 is the evidence); no FK, so a
        -- snapshot purge is never blocked by the index.
        snapshot_id TEXT, manifest_id TEXT, path TEXT,
        data_class TEXT NOT NULL CHECK(data_class IN ('public','private_derived')),
        lineage_node_id TEXT, created_at TEXT NOT NULL,
        FOREIGN KEY(campaign_id, stage_key) REFERENCES campaign_stages(campaign_id, stage_key))""",
        "CREATE INDEX IF NOT EXISTS campaign_stage_io_by_digest "
        "ON campaign_stage_io(org_id, project_id, sha256)",
        "CREATE INDEX IF NOT EXISTS campaign_stage_io_by_attempt "
        "ON campaign_stage_io(campaign_id, stage_key, attempt_id, direction)",
        """CREATE TABLE IF NOT EXISTS campaign_runner_lease (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), holder_id TEXT NOT NULL,
        expires_at TEXT NOT NULL)""",
    )
    for statement in statements:
        connection.execute(statement)
    ensure_budget_entry_index(connection)
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS campaign_receipts_no_{event.lower()}
            BEFORE {event} ON campaign_receipts
            BEGIN SELECT RAISE(ABORT,'campaign receipts are append-only'); END"""
        )
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS campaign_stage_io_no_{event.lower()}
            BEFORE {event} ON campaign_stage_io
            BEGIN SELECT RAISE(ABORT,'campaign stage io is append-only'); END"""
        )


def ensure_budget_entry_index(connection: sqlite3.Connection) -> None:
    """Settlement and the late-charge sweep sum compute entries per admitted attempt."""
    present = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='experiment_budget_entries'"
    ).fetchone()
    if present:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS experiment_budget_entries_by_attempt "
            "ON experiment_budget_entries(experiment_id, admitted_attempt_id, category)"
        )


# Columns added after the v64 tables (ADR 0040, and the ADR 0038 settlement amendment that
# databases created before it lack).
V65_COLUMNS = (
    (
        "campaign_stage_attempts",
        "settlement_basis TEXT CHECK(settlement_basis IS NULL OR settlement_basis IN "
        "('local_zero','unclaimed_zero','human_charge','human_reconcile',"
        "'guardian_bounded_hold','provider_billed'))",
    ),
    (
        "campaign_salad_attempts",
        "guardian_final_json TEXT CHECK(guardian_final_json IS NULL "
        "OR length(guardian_final_json) <= 2000)",
    ),
    ("campaign_salad_attempts", "settlement_confirmed_at TEXT"),
    ("campaign_approvals", "trust_policy_digest TEXT"),
    ("campaign_stages", "provider_id TEXT"),
    ("campaign_stage_attempts", "provider_id TEXT"),
    ("campaign_stage_attempts", "trust_policy_digest TEXT"),
    ("campaign_stage_attempts", "admission_basis TEXT"),
    ("campaign_stage_attempts", "settlement_request_digest TEXT"),
)


def add_v65_columns(connection: sqlite3.Connection) -> None:
    """Idempotently add the ADR 0040 columns to tables created before v65."""
    ensure_budget_entry_index(connection)
    for table, column in V65_COLUMNS:
        existing = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
        if column.split()[0] not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
