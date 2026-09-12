# ruff: noqa: E501
"""Tenant-scoped, explicit goal-to-decision records.

This ledger only links already-recorded operational evidence.  In particular, an
operational ``succeeded`` outcome and a reviewed conclusion never imply scientific
validity.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .experiments_models import (
    AdmitExperimentAttempt,
    AmendConclusion,
    ClaimKubernetesDispatch,
    CreateConclusion,
    CreateExperiment,
    DispatchAdmittedAttempt,
    ExportExperiment,
    LinkAttemptArtifacts,
    RecordAttempt,
    RecordMeasurement,
    ResolveAdmittedAttempt,
    ReviewConclusion,
    ReviseExperimentDesign,
    UpdateConclusion,
)
from .external_operations import current_operation_reconciliation
from .project_artifact_retention import (
    admit_experiment_artifact_evidence_reference,
    validate_experiment_artifact_evidence_reference,
)
from .store import Conflict, Forbidden, NotFound, Store, StoreError, _connect, _parse_utc, utc_now

T = TypeVar("T", bound=BaseModel)
EXPORT_RECORD_LIMIT = 1_000
EXPORT_DEFAULT_BYTES = 1_000_000
# The Electron connection refuses control responses larger than four MiB. Keep
# complete experiment snapshots within that same transport boundary.
EXPORT_MAX_BYTES = 4 * 1024 * 1024


def create_experiment_schema(connection: sqlite3.Connection) -> None:
    """Install empty additive goal-to-decision records without backfilling evidence."""
    statements = (
        """CREATE TABLE IF NOT EXISTS research_experiments (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        goal TEXT NOT NULL, success_criterion TEXT NOT NULL,
        current_design_revision INTEGER NOT NULL CHECK (current_design_revision >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id))""",
        "CREATE INDEX IF NOT EXISTS research_experiments_by_project ON research_experiments(project_id,created_at,id)",
        """CREATE TABLE IF NOT EXISTS research_experiment_designs (
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        revision INTEGER NOT NULL CHECK (revision >= 1), goal TEXT NOT NULL, success_criterion TEXT NOT NULL,
        protocol TEXT NOT NULL,
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY (experiment_id,revision))""",
        """CREATE TABLE IF NOT EXISTS research_experiment_variants (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('baseline','intervention')),
        name TEXT NOT NULL, method_digest TEXT NOT NULL, input_digest TEXT NOT NULL,
        metadata_json TEXT NOT NULL, execution_task_id TEXT REFERENCES tasks(id),
        execution_plan_digest TEXT, revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(experiment_id,design_revision,kind),
        CHECK ((execution_task_id IS NULL) = (execution_plan_digest IS NULL)),
        FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision))""",
        "CREATE INDEX IF NOT EXISTS research_experiment_variants_by_experiment ON research_experiment_variants(experiment_id,design_revision,id)",
        """CREATE TABLE IF NOT EXISTS research_experiment_attempts (
        id TEXT PRIMARY KEY, variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        design_revision INTEGER NOT NULL CHECK (design_revision >= 1),
        provenance_kind TEXT NOT NULL CHECK (provenance_kind IN
            ('admitted_operation','human_reported_observation','legacy_operation_unverified')),
        operation_id TEXT REFERENCES external_operations(operation_id),
        execution_plan_digest TEXT, package_digest TEXT,
        operation_desired_state TEXT CHECK (operation_desired_state IN ('admitted','cancel_requested')),
        operation_observed_state TEXT NOT NULL CHECK (operation_observed_state IN
            ('exited','not_observed','legacy_unverified')),
        termination_status TEXT NOT NULL CHECK (termination_status IN
            ('confirmed','observed_exited_unconfirmed','not_applicable','legacy_unknown')),
        human_observation TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','cancelled','inconclusive')),
        reason TEXT, revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK ((outcome = 'succeeded') OR reason IS NOT NULL),
        CHECK ((provenance_kind='admitted_operation' AND operation_id IS NOT NULL
               AND execution_plan_digest IS NOT NULL AND package_digest IS NOT NULL
               AND operation_desired_state IS NOT NULL AND operation_observed_state='exited'
               AND termination_status IN ('confirmed','observed_exited_unconfirmed')
               AND human_observation IS NULL)
            OR (provenance_kind='human_reported_observation' AND operation_id IS NULL
                AND execution_plan_digest IS NULL AND package_digest IS NULL
                AND operation_desired_state IS NULL AND operation_observed_state='not_observed'
                AND termination_status='not_applicable' AND human_observation IS NOT NULL)
            OR (provenance_kind='legacy_operation_unverified' AND operation_id IS NOT NULL
                AND operation_observed_state='legacy_unverified'
                AND termination_status='legacy_unknown')))""",
        "CREATE INDEX IF NOT EXISTS research_experiment_attempts_by_variant ON research_experiment_attempts(variant_id,created_at,id)",
        """CREATE UNIQUE INDEX IF NOT EXISTS research_experiment_admitted_operation_once
        ON research_experiment_attempts(operation_id) WHERE provenance_kind='admitted_operation'""",
        """CREATE TABLE IF NOT EXISTS research_attempt_artifacts (
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(id), created_at TEXT NOT NULL,
        PRIMARY KEY(attempt_id,artifact_id))""",
        """CREATE TABLE IF NOT EXISTS research_attempt_measurements (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        name TEXT NOT NULL, value_json TEXT NOT NULL, artifact_id TEXT REFERENCES artifacts(id),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id))""",
        "CREATE INDEX IF NOT EXISTS research_attempt_measurements_by_attempt ON research_attempt_measurements(attempt_id,created_at,id)",
        """CREATE TABLE IF NOT EXISTS research_conclusions (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER,
        statement TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('draft','reviewed')),
        revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id), reviewed_at TEXT,
        reviewed_by_principal_id TEXT REFERENCES principals(id),
        FOREIGN KEY(experiment_id,design_revision)
        REFERENCES research_experiment_designs(experiment_id,revision))""",
        "CREATE INDEX IF NOT EXISTS research_conclusions_by_experiment ON research_conclusions(experiment_id,created_at,id)",
        """CREATE TABLE IF NOT EXISTS research_conclusion_lineage (
        conclusion_id TEXT PRIMARY KEY REFERENCES research_conclusions(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        amendment_id TEXT UNIQUE, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS research_conclusion_amendments (
        id TEXT PRIMARY KEY, conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        relation TEXT NOT NULL CHECK (relation IN ('supersedes','retracts')),
        reason TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
        expected_design_revision INTEGER NOT NULL CHECK (expected_design_revision >= 1),
        prior_lineage_revision INTEGER NOT NULL CHECK (prior_lineage_revision >= 1),
        lineage_revision INTEGER NOT NULL CHECK (lineage_revision >= 1),
        created_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(conclusion_id,lineage_revision))""",
        "CREATE INDEX IF NOT EXISTS research_conclusion_amendments_by_experiment ON research_conclusion_amendments(experiment_id,created_at,id)",
        """CREATE TABLE IF NOT EXISTS research_conclusion_evidence (
        conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        artifact_id TEXT REFERENCES artifacts(id), measurement_id TEXT REFERENCES research_attempt_measurements(id),
        PRIMARY KEY(conclusion_id,attempt_id,artifact_id,measurement_id),
        CHECK ((artifact_id IS NOT NULL AND measurement_id IS NULL) OR
               (artifact_id IS NULL AND measurement_id IS NOT NULL)))""",
    )
    for statement in statements:
        connection.execute(statement)


def create_experiment_admission_schema(connection: sqlite3.Connection) -> None:
    """Install empty pre-execution bindings without changing prior attempts.

    The admission is distinct from the legacy human result ledger: it exists
    before package return and keeps the immutable experiment binding while the
    external operation is still uncertain.
    """
    statements = (
        """CREATE TABLE IF NOT EXISTS research_experiment_admitted_attempts (
        id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER NOT NULL CHECK (design_revision >= 1),
        variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        plan_digest TEXT NOT NULL CHECK (length(plan_digest)=64 AND plan_digest NOT GLOB '*[^0-9a-f]*'),
        grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grants(id),
        operation_id TEXT UNIQUE REFERENCES external_operations(operation_id),
        package_digest TEXT CHECK (package_digest IS NULL OR (length(package_digest)=64 AND package_digest NOT GLOB '*[^0-9a-f]*')),
        state TEXT NOT NULL CHECK (state IN ('admitted','preparing','running','unknown','interrupted','restart_pending','succeeded','failed','cancelled')),
        outcome TEXT CHECK (outcome IN ('succeeded','failed','cancelled','unknown')),
        reason TEXT,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        resolved_by_principal_id TEXT REFERENCES principals(id),
        CHECK ((outcome IS NULL AND reason IS NULL AND resolved_by_principal_id IS NULL)
               OR (outcome IS NOT NULL AND state=outcome AND resolved_by_principal_id IS NOT NULL)),
        FOREIGN KEY(experiment_id,design_revision)
        REFERENCES research_experiment_designs(experiment_id,revision)
        )""",
        "CREATE INDEX IF NOT EXISTS research_experiment_admitted_attempts_by_experiment ON research_experiment_admitted_attempts(experiment_id,created_at,id)",
        "CREATE INDEX IF NOT EXISTS research_experiment_admitted_attempts_by_variant ON research_experiment_admitted_attempts(variant_id,created_at,id)",
        """CREATE TABLE IF NOT EXISTS research_experiment_admitted_attempt_states (
        attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        state TEXT NOT NULL CHECK (state IN ('admitted','preparing','running','unknown','interrupted','restart_pending','succeeded','failed','cancelled')),
        observed_state TEXT CHECK (observed_state IN ('unknown','running','exited')),
        reason TEXT, operation_id TEXT REFERENCES external_operations(operation_id),
        recorded_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(attempt_id,sequence))""",
    )
    for statement in statements:
        connection.execute(statement)


def create_admitted_attempt_resolution_schema(connection: sqlite3.Connection) -> None:
    """Add immutable human decisions without relabeling historical outcomes."""
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(research_experiment_admitted_attempts)")
    }
    for name, definition in (
        (
            "operational_state",
            "TEXT NOT NULL DEFAULT 'legacy_unverified' CHECK (operational_state IN "
            "('admitted','preparing','running','unknown','interrupted','restart_pending','legacy_unverified'))",
        ),
        (
            "operational_observed_state",
            "TEXT NOT NULL DEFAULT 'legacy_unverified' CHECK (operational_observed_state IN "
            "('not_observed','running','unknown','exited','legacy_unverified'))",
        ),
        (
            "operational_termination_confirmed",
            "INTEGER CHECK (operational_termination_confirmed IN (0,1))",
        ),
    ):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE research_experiment_admitted_attempts ADD COLUMN {name} {definition}"
            )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS research_experiment_admitted_attempt_resolutions (
        attempt_id TEXT PRIMARY KEY REFERENCES research_experiment_admitted_attempts(id),
        outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','cancelled','unknown')),
        reason TEXT, expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
        decided_at TEXT NOT NULL, decided_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK ((outcome='succeeded') OR reason IS NOT NULL))"""
    )


def create_admitted_attempt_observation_projection_schema(
    connection: sqlite3.Connection,
) -> None:
    """Add the latest operational sample projection without growing history.

    The opaque cursor identifies the latest worker observation or provider receipt
    that advanced this projection.  It is deliberately not a pagination cursor:
    detailed history remains the bounded semantic state-transition ledger.
    """
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(research_experiment_admitted_attempts)")
    }
    for name, definition in (
        ("operational_observation_cursor", "TEXT"),
        ("operational_observed_at", "TEXT"),
    ):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE research_experiment_admitted_attempts ADD COLUMN {name} {definition}"
            )


def create_admitted_attempt_evidence_schema(connection: sqlite3.Connection) -> None:
    """Keep pre-execution attempt identity usable by the ordinary evidence APIs.

    These tables deliberately do not manufacture a terminal legacy attempt.  An
    admitted attempt remains the same ID from admission through uncertainty,
    evidence, conclusion review, and human resolution.
    """
    statements = (
        """CREATE TABLE IF NOT EXISTS research_admitted_attempt_artifacts (
        attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(id), created_at TEXT NOT NULL,
        PRIMARY KEY(attempt_id,artifact_id))""",
        """CREATE TABLE IF NOT EXISTS research_admitted_attempt_measurements (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL
        REFERENCES research_experiment_admitted_attempts(id), name TEXT NOT NULL,
        value_json TEXT NOT NULL, artifact_id TEXT REFERENCES artifacts(id),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id))""",
        "CREATE INDEX IF NOT EXISTS research_admitted_attempt_measurements_by_attempt ON research_admitted_attempt_measurements(attempt_id,created_at,id)",
        """CREATE TABLE IF NOT EXISTS admitted_experiment_artifact_evidence_references (
        id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id), attempt_id TEXT NOT NULL
        REFERENCES research_experiment_admitted_attempts(id), measurement_id TEXT,
        reference_kind TEXT NOT NULL CHECK (reference_kind IN ('attempt_artifact','measurement_artifact')),
        created_at TEXT NOT NULL,
        CHECK ((reference_kind='attempt_artifact' AND measurement_id IS NULL) OR
               (reference_kind='measurement_artifact' AND measurement_id IS NOT NULL)))""",
        "CREATE INDEX IF NOT EXISTS admitted_experiment_artifact_evidence_references_by_artifact ON admitted_experiment_artifact_evidence_references(project_id,artifact_id,id)",
        """CREATE TABLE IF NOT EXISTS research_admitted_conclusion_evidence (
        conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
        attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
        artifact_id TEXT REFERENCES artifacts(id), measurement_id TEXT,
        PRIMARY KEY(conclusion_id,attempt_id,artifact_id,measurement_id),
        CHECK ((artifact_id IS NOT NULL AND measurement_id IS NULL) OR
               (artifact_id IS NULL AND measurement_id IS NOT NULL)))""",
    )
    for statement in statements:
        connection.execute(statement)


def _append_admitted_attempt_state(
    connection: sqlite3.Connection,
    attempt: dict[str, Any],
    state: str,
    actor_principal_id: str,
    *,
    observed_state: str | None = None,
    reason: str | None = None,
    operation_id: str | None = None,
    outcome: str | None = None,
    resolved: bool = False,
    termination_confirmed: bool | None = None,
    observation_cursor: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Append operational facts without allowing them to rewrite a human decision."""
    if (
        attempt.get("operational_state", attempt["state"]) == state
        and not resolved
        and observed_state is None
    ):
        return attempt
    now, revision = utc_now(), attempt["revision"] + 1
    result = dict(attempt)
    human_outcome = attempt["outcome"] if not resolved else outcome
    human_reason = attempt["reason"] if not resolved else reason
    human_actor = attempt["resolved_by_principal_id"] if not resolved else actor_principal_id
    result.update(
        # Keep the legacy state/outcome projection for older readers.  Operational
        # observations advance their own fields after a human decision.
        state=outcome if resolved else (attempt["state"] if human_outcome is not None else state),
        operational_state=attempt.get("operational_state", attempt["state"]) if resolved else state,
        operational_observed_state=observed_state
        if observed_state is not None
        else attempt.get("operational_observed_state", "not_observed"),
        operational_termination_confirmed=(
            int(termination_confirmed)
            if termination_confirmed is not None
            else attempt.get("operational_termination_confirmed")
        ),
        operational_observation_cursor=(
            observation_cursor
            if observation_cursor is not None
            else attempt.get("operational_observation_cursor")
        ),
        operational_observed_at=(
            observed_at if observed_at is not None else attempt.get("operational_observed_at")
        ),
        outcome=human_outcome,
        reason=human_reason,
        revision=revision,
        updated_at=now,
        resolved_by_principal_id=human_actor,
    )
    if (
        connection.execute(
            """UPDATE research_experiment_admitted_attempts SET state=:state,
            operational_state=:operational_state,
            operational_observed_state=:operational_observed_state,
            operational_termination_confirmed=:operational_termination_confirmed,
            operational_observation_cursor=:operational_observation_cursor,
            operational_observed_at=:operational_observed_at,
            outcome=:outcome,reason=:reason,revision=:revision,updated_at=:updated_at,
            resolved_by_principal_id=:resolved_by_principal_id
            WHERE id=:id AND revision=:expected_revision""",
            result | {"expected_revision": attempt["revision"]},
        ).rowcount
        != 1
    ):
        raise Conflict("Admitted experiment attempt revision is no longer current.")
    sequence = connection.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM research_experiment_admitted_attempt_states WHERE attempt_id=?",
        (attempt["id"],),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO research_experiment_admitted_attempt_states
        (attempt_id,sequence,state,observed_state,reason,operation_id,recorded_at,actor_principal_id)
        VALUES (?,?,?,?,?,?,?,?)""",
        (
            attempt["id"],
            sequence,
            state,
            observed_state,
            reason,
            operation_id,
            now,
            actor_principal_id,
        ),
    )
    return result


def _bump_admitted_attempt_revision(connection: sqlite3.Connection, attempt: dict[str, Any]) -> int:
    """Fence evidence writes without inventing an operational state transition."""
    revision = attempt["revision"] + 1
    if (
        connection.execute(
            """UPDATE research_experiment_admitted_attempts SET revision=?,updated_at=?
        WHERE id=? AND revision=?""",
            (revision, utc_now(), attempt["id"], attempt["revision"]),
        ).rowcount
        != 1
    ):
        raise Conflict("Admitted experiment attempt revision is no longer current.")
    return revision


def admitted_attempt_package_identity(
    connection: sqlite3.Connection,
    worker: dict[str, Any],
    package: dict[str, Any],
    admitted_attempt_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a current immutable binding only when it matches the worker grant."""
    row = connection.execute(
        """SELECT a.*,v.execution_task_id,v.execution_plan_digest,v.design_revision AS variant_design_revision,
        e.current_design_revision,p.org_id FROM research_experiment_admitted_attempts a
        JOIN research_experiment_variants v ON v.id=a.variant_id
        JOIN research_experiments e ON e.id=a.experiment_id
        JOIN projects p ON p.id=e.project_id
        WHERE a.id=? AND p.org_id=? AND e.project_id=?""",
        (admitted_attempt_id, worker["org_id"], package["project_id"]),
    ).fetchone()
    if row is None:
        raise NotFound("Admitted experiment attempt was not found.")
    attempt = dict(row)
    if attempt["state"] in {"succeeded", "failed", "cancelled"}:
        raise Conflict("Admitted experiment attempt is already resolved.")
    if (
        attempt["experiment_id"] != package.get("experiment_id", attempt["experiment_id"])
        or attempt["design_revision"] != attempt["variant_design_revision"]
        or attempt["design_revision"] != attempt["current_design_revision"]
        or attempt["task_id"] != attempt["execution_task_id"]
        or attempt["plan_digest"] != attempt["execution_plan_digest"]
        or attempt["grant_id"] != package["grant_id"]
        or attempt["task_id"] != package["task_id"]
        or attempt["plan_digest"] != package["plan_digest"]
    ):
        raise Conflict(
            "Admitted experiment attempt no longer matches its variant, design, grant, or plan."
        )
    return attempt, {
        "admitted_attempt_id": attempt["id"],
        "experiment_id": attempt["experiment_id"],
        "design_revision": attempt["design_revision"],
        "variant_id": attempt["variant_id"],
        "task_id": attempt["task_id"],
        "plan_digest": attempt["plan_digest"],
        "grant_id": attempt["grant_id"],
    }


def bind_admitted_attempt_package(
    connection: sqlite3.Connection,
    worker: dict[str, Any],
    package: dict[str, Any],
    package_digest: str,
    admitted_attempt_id: str,
    restart_intent: bool = False,
) -> dict[str, Any]:
    """Fence a worker package to its prior human admission in the same transaction."""
    attempt, identity = admitted_attempt_package_identity(
        connection, worker, package, admitted_attempt_id
    )
    if package.get("experiment") != identity:
        raise Conflict("Execution package experiment binding conflicts.")
    if attempt["operation_id"] not in {None, package["operation_id"]} or attempt[
        "package_digest"
    ] not in {None, package_digest}:
        raise Conflict(
            "Admitted experiment attempt cannot be reused with another operation or package."
        )
    connection.execute(
        """UPDATE research_experiment_admitted_attempts SET operation_id=?,package_digest=?,updated_at=?
        WHERE id=? AND operation_id IS NULL AND package_digest IS NULL""",
        (package["operation_id"], package_digest, utc_now(), attempt["id"]),
    )
    updated = dict(
        connection.execute(
            "SELECT * FROM research_experiment_admitted_attempts WHERE id=?", (attempt["id"],)
        ).fetchone()
    )
    next_state = (
        "restart_pending"
        if restart_intent and updated["operational_state"] in {"unknown", "interrupted"}
        else "preparing"
        if updated["operational_state"] == "admitted"
        else updated["operational_state"]
    )
    return _append_admitted_attempt_state(
        connection,
        updated,
        next_state,
        worker["id"],
        operation_id=package["operation_id"],
    )


def record_admitted_attempt_observation(
    connection: sqlite3.Connection,
    operation: sqlite3.Row | dict[str, Any],
    worker: dict[str, Any],
) -> bool:
    """Project every sample while appending only a semantic state transition.

    ``execution_observations`` and fake-provider receipts retain operational
    samples.  This bounded experiment-facing ledger records only changed
    operational state, observed state, or termination-confirmation semantics,
    while always retaining the latest opaque sample cursor and observed time.
    """
    attempt_id = operation["experiment_attempt_id"]
    if attempt_id is None:
        return False
    row = connection.execute(
        "SELECT * FROM research_experiment_admitted_attempts WHERE id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise Conflict("External operation experiment admission is missing.")
    attempt = dict(row)
    if (
        attempt["operation_id"] != operation["operation_id"]
        or attempt["experiment_id"] != operation["experiment_id"]
        or attempt["design_revision"] != operation["experiment_design_revision"]
        or attempt["variant_id"] != operation["experiment_variant_id"]
        or attempt["grant_id"] != operation["grant_id"]
        or attempt["task_id"] != operation["task_id"]
        or attempt["package_digest"] != operation["package_digest"]
    ):
        raise Conflict("External operation experiment admission binding conflicts.")
    reconciliation = current_operation_reconciliation(connection, operation["operation_id"])
    if reconciliation is None:
        raise Conflict("External operation has no current observation to project.")
    observed_state = str(reconciliation["observed_state"])
    termination_confirmed = bool(reconciliation["termination_confirmed"])
    state = {
        "running": "running",
        "unknown": "unknown",
        # An exit (even after cancellation) is operational evidence, not a human
        # cancelled outcome. Confirmation is retained in its separate field.
        "exited": "interrupted",
    }[observed_state]
    prior_state = attempt.get("operational_state", attempt["state"])
    prior_observed_state = attempt.get("operational_observed_state", "not_observed")
    prior_confirmation = attempt.get("operational_termination_confirmed")
    semantic_transition = (
        prior_state != state
        or prior_observed_state != observed_state
        or prior_confirmation is None
        or bool(prior_confirmation) != termination_confirmed
    )
    if not semantic_transition:
        # A fresh sample changes operational freshness, not the research-facing
        # semantic history or revision.  The enclosing observation command keeps
        # this mutable projection and the raw sample in one transaction.
        connection.execute(
            """UPDATE research_experiment_admitted_attempts
            SET operational_observation_cursor=?,operational_observed_at=? WHERE id=?""",
            (reconciliation["cursor"], reconciliation["observed_at"], attempt["id"]),
        )
        return False
    _append_admitted_attempt_state(
        connection,
        attempt,
        state,
        worker["id"],
        observed_state=observed_state,
        operation_id=operation["operation_id"],
        termination_confirmed=termination_confirmed,
        observation_cursor=str(reconciliation["cursor"]),
        observed_at=str(reconciliation["observed_at"]),
    )
    return True


def create_v25_experiment_schema(connection: sqlite3.Connection) -> None:
    """Install the exact v25 ledger shape; later provenance columns are absent."""
    statements = (
        """CREATE TABLE research_experiments (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        goal TEXT NOT NULL, success_criterion TEXT NOT NULL,
        current_design_revision INTEGER NOT NULL CHECK (current_design_revision >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id))""",
        "CREATE INDEX research_experiments_by_project ON research_experiments(project_id,created_at,id)",
        """CREATE TABLE research_experiment_designs (
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        revision INTEGER NOT NULL CHECK (revision >= 1), protocol TEXT NOT NULL,
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY (experiment_id,revision))""",
        """CREATE TABLE research_experiment_variants (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('baseline','intervention')),
        name TEXT NOT NULL, method_digest TEXT NOT NULL, input_digest TEXT NOT NULL,
        metadata_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(experiment_id,design_revision,kind),
        FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision))""",
        "CREATE INDEX research_experiment_variants_by_experiment ON research_experiment_variants(experiment_id,design_revision,id)",
        """CREATE TABLE research_experiment_attempts (
        id TEXT PRIMARY KEY, variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        operation_id TEXT NOT NULL REFERENCES external_operations(operation_id),
        outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','cancelled','inconclusive')),
        reason TEXT, revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK ((outcome = 'succeeded') OR reason IS NOT NULL))""",
        "CREATE INDEX research_experiment_attempts_by_variant ON research_experiment_attempts(variant_id,created_at,id)",
        """CREATE TABLE research_attempt_artifacts (
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(id), created_at TEXT NOT NULL,
        PRIMARY KEY(attempt_id,artifact_id))""",
        """CREATE TABLE research_attempt_measurements (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        name TEXT NOT NULL, value_json TEXT NOT NULL, artifact_id TEXT REFERENCES artifacts(id),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id))""",
        "CREATE INDEX research_attempt_measurements_by_attempt ON research_attempt_measurements(attempt_id,created_at,id)",
        """CREATE TABLE research_conclusions (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        statement TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('draft','reviewed')),
        revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id), reviewed_at TEXT,
        reviewed_by_principal_id TEXT REFERENCES principals(id))""",
        "CREATE INDEX research_conclusions_by_experiment ON research_conclusions(experiment_id,created_at,id)",
        """CREATE TABLE research_conclusion_evidence (
        conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        artifact_id TEXT REFERENCES artifacts(id), measurement_id TEXT REFERENCES research_attempt_measurements(id),
        PRIMARY KEY(conclusion_id,attempt_id,artifact_id,measurement_id),
        CHECK ((artifact_id IS NOT NULL AND measurement_id IS NULL) OR
               (artifact_id IS NULL AND measurement_id IS NOT NULL)))""",
    )
    for statement in statements:
        connection.execute(statement)


def create_v27_experiment_conclusion_schema(connection: sqlite3.Connection) -> None:
    """Install only the v27 conclusion shape while v25 attempts remain unchanged."""
    connection.execute(
        """CREATE TABLE research_conclusions (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER,
        statement TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('draft','reviewed')),
        revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id), reviewed_at TEXT,
        reviewed_by_principal_id TEXT REFERENCES principals(id),
        FOREIGN KEY(experiment_id,design_revision)
        REFERENCES research_experiment_designs(experiment_id,revision))"""
    )
    connection.execute(
        "CREATE INDEX research_conclusions_by_experiment ON research_conclusions(experiment_id,created_at,id)"
    )
    connection.execute(
        """CREATE TABLE research_conclusion_evidence (
        conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        artifact_id TEXT REFERENCES artifacts(id), measurement_id TEXT REFERENCES research_attempt_measurements(id),
        PRIMARY KEY(conclusion_id,attempt_id,artifact_id,measurement_id),
        CHECK ((artifact_id IS NOT NULL AND measurement_id IS NULL) OR
               (artifact_id IS NULL AND measurement_id IS NOT NULL)))"""
    )


def create_v26_experiment_evidence_retention_schema(connection: sqlite3.Connection) -> None:
    """Install the v26 retention indexes for already durable evidence links."""
    statements = (
        """CREATE TABLE IF NOT EXISTS experiment_artifact_evidence_references (
        id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        measurement_id TEXT REFERENCES research_attempt_measurements(id),
        reference_kind TEXT NOT NULL CHECK (reference_kind IN ('attempt_artifact','measurement_artifact')),
        created_at TEXT NOT NULL,
        CHECK ((reference_kind='attempt_artifact' AND measurement_id IS NULL) OR
               (reference_kind='measurement_artifact' AND measurement_id IS NOT NULL))
        )""",
        "CREATE INDEX IF NOT EXISTS experiment_artifact_evidence_references_by_artifact ON experiment_artifact_evidence_references(project_id,artifact_id,id)",
    )
    for statement in statements:
        connection.execute(statement)


def create_experiment_evidence_retention_schema(connection: sqlite3.Connection) -> None:
    """Install the current retention schema, unchanged since v26."""
    create_v26_experiment_evidence_retention_schema(connection)


class ExperimentStore(Store):
    """Commands for one bounded design, evidence chain, and human review."""

    @staticmethod
    def _body(model: type[T], payload: dict[str, Any] | T) -> dict[str, Any]:
        try:
            return (
                payload if isinstance(payload, model) else model.model_validate(payload)
            ).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid experiment request.") from error

    @staticmethod
    def _human(actor: dict[str, Any]) -> None:
        if actor["role"] != "human":
            raise Forbidden("Human review is required for goal-to-decision records.")

    def _experiment(
        self, connection: sqlite3.Connection, actor: dict[str, Any], ident: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT e.* FROM research_experiments e JOIN projects p ON p.id=e.project_id
            WHERE e.id=? AND p.org_id=? AND (? IS NULL OR e.project_id=?)""",
            (ident, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Experiment was not found.")
        return dict(row)

    def list_experiments(
        self, principal: dict[str, Any], project_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List one project's bounded goal-to-decision records for read-only clients."""
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise StoreError("Invalid experiment pagination.")
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            self._project(connection, actor, project_id)
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM research_experiments WHERE project_id=?
                    ORDER BY created_at,id LIMIT ? OFFSET ?""",
                    (project_id, limit, offset),
                )
            ]
        finally:
            connection.close()

    def _variant(
        self, connection: sqlite3.Connection, actor: dict[str, Any], ident: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT v.* FROM research_experiment_variants v JOIN research_experiments e ON e.id=v.experiment_id
            JOIN projects p ON p.id=e.project_id WHERE v.id=? AND p.org_id=?
            AND (? IS NULL OR e.project_id=?)""",
            (ident, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Experiment variant was not found.")
        return dict(row)

    def _attempt(
        self, connection: sqlite3.Connection, actor: dict[str, Any], ident: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT a.*,v.experiment_id,e.project_id FROM research_experiment_attempts a
            JOIN research_experiment_variants v ON v.id=a.variant_id
            JOIN research_experiments e ON e.id=v.experiment_id JOIN projects p ON p.id=e.project_id
            WHERE a.id=? AND p.org_id=? AND (? IS NULL OR e.project_id=?)""",
            (ident, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Experiment attempt was not found.")
        return dict(row)

    def _evidence_attempt(
        self, connection: sqlite3.Connection, actor: dict[str, Any], ident: str
    ) -> dict[str, Any]:
        """Resolve the public attempt identity without converting admissions to legacy rows."""
        try:
            return self._attempt(connection, actor, ident) | {"attempt_kind": "legacy"}
        except NotFound:
            row = connection.execute(
                """SELECT a.*,e.project_id FROM research_experiment_admitted_attempts a
                JOIN research_experiments e ON e.id=a.experiment_id JOIN projects p ON p.id=e.project_id
                WHERE a.id=? AND p.org_id=? AND (? IS NULL OR e.project_id=?)""",
                (ident, actor["org_id"], actor["project_id"], actor["project_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Experiment attempt was not found.") from None
            return dict(row) | {"attempt_kind": "admitted"}

    @staticmethod
    def _admit_bound_artifact_reference(
        connection: sqlite3.Connection,
        attempt: dict[str, Any],
        artifact_id: str,
        ident: str,
        kind: str,
        now: str,
        measurement_id: str | None = None,
    ) -> None:
        validate_experiment_artifact_evidence_reference(
            connection, attempt["project_id"], artifact_id
        )
        connection.execute(
            """INSERT OR IGNORE INTO admitted_experiment_artifact_evidence_references
            (id,artifact_id,project_id,attempt_id,measurement_id,reference_kind,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (ident, artifact_id, attempt["project_id"], attempt["id"], measurement_id, kind, now),
        )

    def _event(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        experiment: dict[str, Any],
        name: str,
        revision: int,
        payload: dict[str, Any],
    ) -> None:
        super()._event(connection, actor, experiment["project_id"], None, name, revision, payload)

    @staticmethod
    def _variant_response(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    @staticmethod
    def _measurement_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["value"] = json.loads(result.pop("value_json"))
        return result

    def create_experiment(
        self,
        principal: dict[str, Any],
        project_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateExperiment,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(CreateExperiment, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            self._project(connection, actor, project_id)
            self._validate_variant_execution_plans(connection, actor, project_id, body["variants"])
            now, ident = utc_now(), "experiment_" + uuid.uuid4().hex
            experiment = {
                "id": ident,
                "project_id": project_id,
                "goal": body["goal"],
                "success_criterion": body["success_criterion"],
                "current_design_revision": 1,
                "created_at": now,
                "updated_at": now,
                "created_by_principal_id": actor["id"],
            }
            connection.execute(
                """INSERT INTO research_experiments (id,project_id,goal,success_criterion,current_design_revision,created_at,updated_at,created_by_principal_id) VALUES (:id,:project_id,:goal,:success_criterion,:current_design_revision,:created_at,:updated_at,:created_by_principal_id)""",
                experiment,
            )
            self._insert_design(
                connection, actor, experiment, 1, body["protocol"], body["variants"], now
            )
            self._event(
                connection,
                actor,
                experiment,
                "experiment.design_registered",
                1,
                {
                    "experiment_id": ident,
                    "design_revision": 1,
                    "variant_kinds": ["baseline", "intervention"],
                },
            )
            return 201, self._experiment_response(connection, experiment)

        return self._mutate(
            principal, key, "POST", f"/projects/{project_id}/experiments", digest, command
        )

    def _insert_design(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        experiment: dict[str, Any],
        revision: int,
        protocol: str,
        variants: list[dict[str, Any]],
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO research_experiment_designs
            (experiment_id,revision,goal,success_criterion,protocol,created_at,created_by_principal_id)
            VALUES (?,?,?,?,?,?,?)""",
            (
                experiment["id"],
                revision,
                experiment["goal"],
                experiment["success_criterion"],
                protocol,
                now,
                actor["id"],
            ),
        )
        for variant in variants:
            connection.execute(
                """INSERT INTO research_experiment_variants
                (id,experiment_id,design_revision,kind,name,method_digest,input_digest,metadata_json,
                 execution_task_id,execution_plan_digest,revision,created_at,created_by_principal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    "experiment_variant_" + uuid.uuid4().hex,
                    experiment["id"],
                    revision,
                    variant["kind"],
                    variant["name"],
                    variant["method_digest"],
                    variant["input_digest"],
                    json.dumps(variant["metadata"], sort_keys=True, separators=(",", ":")),
                    variant["execution_task_id"],
                    variant["execution_plan_digest"],
                    now,
                    actor["id"],
                ),
            )

    def _validate_variant_execution_plans(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        project_id: str,
        variants: list[dict[str, Any]],
    ) -> None:
        """Keep optional execution bases immutable, tenant-scoped, and real."""
        for variant in variants:
            task_id = variant["execution_task_id"]
            if task_id is None:
                continue
            row = connection.execute(
                """SELECT 1 FROM task_revisions r JOIN tasks t ON t.id=r.task_id
                JOIN projects p ON p.id=t.project_id
                WHERE r.task_id=? AND r.plan_digest=? AND t.project_id=? AND p.org_id=?
                AND (? IS NULL OR t.project_id=?)""",
                (
                    task_id,
                    variant["execution_plan_digest"],
                    project_id,
                    actor["org_id"],
                    actor["project_id"],
                    actor["project_id"],
                ),
            ).fetchone()
            if row is None:
                raise NotFound("Experiment execution plan was not found.")

    def revise_design(
        self,
        principal: dict[str, Any],
        experiment_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | ReviseExperimentDesign,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(ReviseExperimentDesign, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            experiment = self._experiment(connection, actor, experiment_id)
            if experiment["current_design_revision"] != body["expected_revision"]:
                raise Conflict("Experiment design revision is no longer current.")
            revision, now = body["expected_revision"] + 1, utc_now()
            if (
                connection.execute(
                    "UPDATE research_experiments SET goal=?,success_criterion=?,current_design_revision=?,updated_at=? WHERE id=? AND current_design_revision=?",
                    (
                        body["goal"],
                        body["success_criterion"],
                        revision,
                        now,
                        experiment_id,
                        body["expected_revision"],
                    ),
                ).rowcount
                != 1
            ):
                raise Conflict("Experiment design revision is no longer current.")
            updated = experiment | {
                "goal": body["goal"],
                "success_criterion": body["success_criterion"],
                "current_design_revision": revision,
                "updated_at": now,
            }
            self._validate_variant_execution_plans(
                connection, actor, experiment["project_id"], body["variants"]
            )
            self._insert_design(
                connection, actor, updated, revision, body["protocol"], body["variants"], now
            )
            self._event(
                connection,
                actor,
                updated,
                "experiment.design_revised",
                revision,
                {"experiment_id": experiment_id, "design_revision": revision},
            )
            return 201, self._experiment_response(connection, updated)

        return self._mutate(
            principal, key, "POST", f"/experiments/{experiment_id}/designs", digest, command
        )

    def admit_attempt(
        self,
        principal: dict[str, Any],
        variant_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | AdmitExperimentAttempt,
    ) -> tuple[int, dict[str, Any]]:
        """Create an immutable human-reviewed binding before any package is returned."""
        body = self._body(AdmitExperimentAttempt, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            variant = self._variant(connection, actor, variant_id)
            experiment = self._experiment(connection, actor, variant["experiment_id"])
            if (
                variant["revision"] != body["expected_variant_revision"]
                or variant["design_revision"] != body["expected_design_revision"]
                or experiment["current_design_revision"] != body["expected_design_revision"]
            ):
                raise Conflict("Experiment variant or design revision is no longer current.")
            if variant["execution_task_id"] is None:
                raise Conflict("Experiment variant does not declare an immutable execution plan.")
            grant = connection.execute(
                """SELECT g.* FROM execution_grants g JOIN projects p ON p.id=g.project_id
                WHERE g.id=? AND p.org_id=? AND g.project_id=?""",
                (body["grant_id"], actor["org_id"], experiment["project_id"]),
            ).fetchone()
            if grant is None:
                raise NotFound("Execution grant was not found.")
            if (
                grant["status"] != "active"
                or grant["task_id"] != variant["execution_task_id"]
                or grant["plan_digest"] != variant["execution_plan_digest"]
            ):
                raise Conflict(
                    "Execution grant does not match the immutable experiment variant plan."
                )
            if connection.execute(
                "SELECT 1 FROM external_operations WHERE grant_id=?", (grant["id"],)
            ).fetchone():
                raise Conflict(
                    "Execution grant already has an operation or package intent and cannot be admitted."
                )
            if connection.execute(
                "SELECT 1 FROM research_experiment_admitted_attempts WHERE grant_id=?",
                (grant["id"],),
            ).fetchone():
                raise Conflict(
                    "Execution grant is already bound to an admitted experiment attempt."
                )
            now = utc_now()
            row = {
                "id": "experiment_admitted_attempt_" + uuid.uuid4().hex,
                "experiment_id": experiment["id"],
                "design_revision": variant["design_revision"],
                "variant_id": variant["id"],
                "task_id": variant["execution_task_id"],
                "plan_digest": variant["execution_plan_digest"],
                "grant_id": grant["id"],
                "operation_id": None,
                "package_digest": None,
                "state": "admitted",
                "operational_state": "admitted",
                "operational_observed_state": "not_observed",
                "operational_termination_confirmed": None,
                "operational_observation_cursor": None,
                "operational_observed_at": None,
                "outcome": None,
                "reason": None,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "created_by_principal_id": actor["id"],
                "resolved_by_principal_id": None,
            }
            connection.execute(
                """INSERT INTO research_experiment_admitted_attempts
                (id,experiment_id,design_revision,variant_id,task_id,plan_digest,grant_id,operation_id,
                 package_digest,state,operational_state,operational_observed_state,
                 operational_termination_confirmed,outcome,reason,revision,created_at,updated_at,
                 created_by_principal_id,resolved_by_principal_id)
                VALUES (:id,:experiment_id,:design_revision,:variant_id,:task_id,:plan_digest,:grant_id,
                 :operation_id,:package_digest,:state,:operational_state,:operational_observed_state,
                 :operational_termination_confirmed,:outcome,:reason,:revision,:created_at,:updated_at,
                :created_by_principal_id,:resolved_by_principal_id)""",
                row,
            )
            connection.execute(
                """INSERT INTO research_experiment_admitted_attempt_states
                (attempt_id,sequence,state,observed_state,reason,operation_id,recorded_at,actor_principal_id)
                VALUES (?,1,'admitted',NULL,NULL,NULL,?,?)""",
                (row["id"], now, actor["id"]),
            )
            self._event(
                connection,
                actor,
                experiment,
                "experiment.attempt_admitted",
                1,
                {
                    "experiment_id": experiment["id"],
                    "design_revision": row["design_revision"],
                    "variant_id": row["variant_id"],
                    "admitted_attempt_id": row["id"],
                    "grant_id": row["grant_id"],
                    "task_id": row["task_id"],
                    "plan_digest": row["plan_digest"],
                    "execution_dispatched": False,
                },
            )
            return 201, row

        return self._mutate(
            principal,
            key,
            "POST",
            f"/experiment-variants/{variant_id}/admitted-attempts",
            digest,
            command,
        )

    def dispatch_admitted_attempt(
        self,
        principal: dict[str, Any],
        attempt_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | DispatchAdmittedAttempt,
    ) -> tuple[int, dict[str, Any]]:
        """Write the human dispatch outbox after, never before, package admission.

        The normal package endpoint intentionally remains the sole package issuer.
        This command only authorizes the already frozen package to be claimed on
        the named target; it makes no Kubernetes call.
        """
        body = self._body(DispatchAdmittedAttempt, payload)
        path = f"/experiment-admitted-attempts/{attempt_id}/dispatch"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            attempt_row = connection.execute(
                """SELECT a.*,e.project_id,p.org_id FROM research_experiment_admitted_attempts a
                JOIN research_experiments e ON e.id=a.experiment_id JOIN projects p ON p.id=e.project_id
                WHERE a.id=? AND p.org_id=? AND (? IS NULL OR e.project_id=?)""",
                (attempt_id, actor["org_id"], actor["project_id"], actor["project_id"]),
            ).fetchone()
            if attempt_row is None:
                raise NotFound("Admitted experiment attempt was not found.")
            attempt = dict(attempt_row)
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Admitted experiment attempt revision is no longer current.")
            if attempt["outcome"] is not None or attempt["operation_id"] is None:
                raise Conflict("An unresolved packaged admitted attempt is required for dispatch.")
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (attempt["operation_id"],)
            ).fetchone()
            package = connection.execute(
                "SELECT * FROM execution_runtime_packages WHERE operation_id=?",
                (attempt["operation_id"],),
            ).fetchone()
            if (
                operation is None
                or package is None
                or operation["adapter_version"] != "kubernetes-job-v1"
                or operation["profile_version"] != body["profile_version"]
                or operation["desired_state"] != "admitted"
                or operation["package_digest"] != attempt["package_digest"]
                or package["package_digest"] != attempt["package_digest"]
                or operation["experiment_attempt_id"] != attempt_id
            ):
                raise Conflict("Admitted attempt has no current Kubernetes package authority.")
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (operation["grant_id"],)
            ).fetchone()
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (operation["lease_id"],)
            ).fetchone()
            if (
                grant is None
                or lease is None
                or grant["status"] != "active"
                or grant["epoch"] != lease["epoch"]
                or operation["epoch"] != lease["epoch"]
                or _parse_utc(lease["expires_at"]) <= datetime.now(UTC)
            ):
                raise Conflict("Kubernetes dispatch authority is no longer current.")
            existing = connection.execute(
                "SELECT * FROM kubernetes_dispatches WHERE operation_id=?",
                (operation["operation_id"],),
            ).fetchone()
            expected = {
                "operation_id": operation["operation_id"],
                "admitted_attempt_id": attempt_id,
                "org_id": actor["org_id"],
                "project_id": attempt["project_id"],
                "grant_id": operation["grant_id"],
                "lease_id": operation["lease_id"],
                "worker_principal_id": operation["worker_principal_id"],
                "epoch": operation["epoch"],
                "package_digest": operation["package_digest"],
                "target_digest": body["target_digest"],
                "profile_version": body["profile_version"],
            }
            if existing is not None:
                if any(existing[field] != value for field, value in expected.items()):
                    raise Conflict("Kubernetes dispatch identity or target conflicts.")
                return 200, dict(existing)
            now = utc_now()
            row = expected | {
                "id": "kubernetes_dispatch_" + uuid.uuid4().hex,
                "state": "queued",
                "claimed_at": None,
                "claimed_by_principal_id": None,
                "created_at": now,
                "updated_at": now,
                "created_by_principal_id": actor["id"],
            }
            connection.execute(
                """INSERT INTO kubernetes_dispatches
                (id,operation_id,admitted_attempt_id,org_id,project_id,grant_id,lease_id,
                 worker_principal_id,epoch,package_digest,target_digest,profile_version,state,
                 claimed_at,claimed_by_principal_id,created_at,updated_at,created_by_principal_id)
                VALUES (:id,:operation_id,:admitted_attempt_id,:org_id,:project_id,:grant_id,:lease_id,
                :worker_principal_id,:epoch,:package_digest,:target_digest,:profile_version,:state,
                :claimed_at,:claimed_by_principal_id,:created_at,:updated_at,:created_by_principal_id)""",
                row,
            )
            self._event(
                connection,
                actor,
                self._experiment(connection, actor, attempt["experiment_id"]),
                "experiment.kubernetes_dispatch_approved",
                attempt["revision"],
                {
                    "admitted_attempt_id": attempt_id,
                    "operation_id": row["operation_id"],
                    "target_digest": row["target_digest"],
                    "profile_version": row["profile_version"],
                    "execution_dispatched": True,
                },
            )
            return 201, row

        return self._mutate(principal, key, "POST", path, digest, command)

    def claim_kubernetes_dispatch(
        self,
        principal: dict[str, Any],
        dispatch_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | ClaimKubernetesDispatch,
    ) -> tuple[int, dict[str, Any]]:
        """Return one exact package only to its enrolled target-bound worker."""
        body = self._body(ClaimKubernetesDispatch, payload)
        path = f"/kubernetes-dispatches/{dispatch_id}/claim"

        def current(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row, dict[str, Any]]:
            """Recheck the authority behind a claimed-package replay.

            Claim responses are executable material, unlike an ordinary mutation
            receipt.  The idempotency row may preserve its immutable package, but
            it must never preserve authority after a revoke, expiry, or worker
            lifecycle change.
            """
            dispatch = connection.execute(
                "SELECT * FROM kubernetes_dispatches WHERE id=? AND org_id=?",
                (dispatch_id, worker["org_id"]),
            ).fetchone()
            if dispatch is None or dispatch["worker_principal_id"] != worker["id"]:
                raise NotFound("Kubernetes dispatch was not found.")
            if (
                dispatch["target_digest"] != body["target_digest"]
                or dispatch["profile_version"] != body["profile_version"]
            ):
                raise Conflict("Kubernetes dispatch target or profile does not match this worker.")
            from .workers import WorkerRegistryStore

            WorkerRegistryStore.require_ready_worker(connection, worker, dispatch["project_id"])
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?",
                (dispatch["operation_id"],),
            ).fetchone()
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (dispatch["lease_id"],)
            ).fetchone()
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (dispatch["grant_id"],)
            ).fetchone()
            package = connection.execute(
                "SELECT * FROM execution_runtime_packages WHERE operation_id=?",
                (dispatch["operation_id"],),
            ).fetchone()
            if (
                operation is None
                or lease is None
                or grant is None
                or package is None
                or dispatch["state"] == "cancel_requested"
                or grant["status"] != "active"
                or grant["epoch"] != dispatch["epoch"]
                or lease["epoch"] != dispatch["epoch"]
                or operation["epoch"] != dispatch["epoch"]
                or lease["grant_id"] != dispatch["grant_id"]
                or operation["grant_id"] != dispatch["grant_id"]
                or operation["lease_id"] != dispatch["lease_id"]
                or lease["worker_principal_id"] != worker["id"]
                or operation["worker_principal_id"] != worker["id"]
                or operation["package_digest"] != dispatch["package_digest"]
                or package["package_digest"] != dispatch["package_digest"]
                or _parse_utc(lease["expires_at"]) <= datetime.now(UTC)
            ):
                raise Conflict("Kubernetes dispatch authority is no longer current.")
            try:
                package_value = json.loads(package["package_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise Conflict("Kubernetes dispatch package record is invalid.") from error
            if (
                not isinstance(package_value, dict)
                or package_value.get("operation_id") != dispatch["operation_id"]
            ):
                raise Conflict("Kubernetes dispatch package identity conflicts.")
            return dispatch, operation, lease, grant, package, package_value

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            dispatch, operation, lease, _grant, _package, package_value = current(
                connection, worker
            )
            if dispatch["state"] == "queued":
                now = utc_now()
                connection.execute(
                    "UPDATE kubernetes_dispatches SET state='claimed',claimed_at=?,claimed_by_principal_id=?,updated_at=? WHERE id=? AND state='queued'",
                    (now, worker["id"], now, dispatch_id),
                )
                dispatch = connection.execute(
                    "SELECT * FROM kubernetes_dispatches WHERE id=?", (dispatch_id,)
                ).fetchone()
                self._event(
                    connection,
                    worker,
                    {"project_id": dispatch["project_id"]},
                    "experiment.kubernetes_dispatch_claimed",
                    dispatch["epoch"],
                    {
                        "dispatch_id": dispatch_id,
                        "admitted_attempt_id": dispatch["admitted_attempt_id"],
                        "operation_id": dispatch["operation_id"],
                        "execution_dispatched": True,
                    },
                )
            elif dispatch["claimed_by_principal_id"] != worker["id"]:
                raise Conflict("Kubernetes dispatch was claimed by another worker.")
            return 200, {
                "dispatch": dict(dispatch),
                "operation": dict(operation),
                "package": package_value,
                "package_digest": dispatch["package_digest"],
                "admitted_attempt_id": dispatch["admitted_attempt_id"],
                "lease": dict(lease),
                "execution_dispatched": True,
            }

        def replay_guard(
            connection: sqlite3.Connection, worker: dict[str, Any], response: dict[str, Any]
        ) -> None:
            dispatch, _operation, _lease, _grant, _package, _package_value = current(
                connection, worker
            )
            if (
                dispatch["state"] != "claimed"
                or dispatch["claimed_by_principal_id"] != worker["id"]
                or response.get("dispatch", {}).get("id") != dispatch_id
            ):
                raise Conflict("Kubernetes dispatch claim is no longer current.")

        return self._mutate(
            principal,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=replay_guard,
        )

    def resolve_admitted_attempt(
        self,
        principal: dict[str, Any],
        attempt_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | ResolveAdmittedAttempt,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(ResolveAdmittedAttempt, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            row = connection.execute(
                """SELECT a.* FROM research_experiment_admitted_attempts a
                JOIN research_experiments e ON e.id=a.experiment_id JOIN projects p ON p.id=e.project_id
                WHERE a.id=? AND p.org_id=? AND (? IS NULL OR e.project_id=?)""",
                (attempt_id, actor["org_id"], actor["project_id"], actor["project_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Admitted experiment attempt was not found.")
            attempt = dict(row)
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Admitted experiment attempt revision is no longer current.")
            if attempt["outcome"] is not None:
                raise Conflict("Admitted experiment attempt is already resolved.")
            self._validate_admitted_resolution_evidence(connection, attempt, body["outcome"])
            now = utc_now()
            connection.execute(
                """INSERT INTO research_experiment_admitted_attempt_resolutions
                (attempt_id,outcome,reason,expected_revision,decided_at,decided_by_principal_id)
                VALUES (?,?,?,?,?,?)""",
                (
                    attempt_id,
                    body["outcome"],
                    body["reason"],
                    body["expected_revision"],
                    now,
                    actor["id"],
                ),
            )
            updated = _append_admitted_attempt_state(
                connection,
                attempt,
                body["outcome"],
                actor["id"],
                reason=body["reason"],
                operation_id=attempt["operation_id"],
                outcome=body["outcome"],
                resolved=True,
            )
            updated["human_outcome"] = {
                "outcome": body["outcome"],
                "reason": body["reason"],
                "expected_revision": body["expected_revision"],
                "decided_at": now,
                "decided_by_principal_id": actor["id"],
            }
            experiment = self._experiment(connection, actor, attempt["experiment_id"])
            self._event(
                connection,
                actor,
                experiment,
                "experiment.admitted_attempt_resolved",
                updated["revision"],
                {
                    "experiment_id": attempt["experiment_id"],
                    "admitted_attempt_id": attempt_id,
                    "operation_id": attempt["operation_id"],
                    "outcome": body["outcome"],
                    "scientific_validity": "not_assessed",
                },
            )
            return 200, updated

        return self._mutate(
            principal,
            key,
            "POST",
            f"/experiment-admitted-attempts/{attempt_id}/resolve",
            digest,
            command,
        )

    @staticmethod
    def _validate_admitted_resolution_evidence(
        connection: sqlite3.Connection, attempt: dict[str, Any], outcome: str
    ) -> None:
        """Require terminal attributed execution evidence for success and cancellation.

        ``unknown`` deliberately remains available when a package, provider reply,
        or terminal observation is missing.  A human label never turns a package
        intent or an unconfirmed exit into an execution success/cancellation.
        """
        if outcome not in {"succeeded", "cancelled"}:
            return
        if attempt["operation_id"] is None or attempt["package_digest"] is None:
            raise Conflict(
                "Execution success or cancellation requires a bound package and operation."
            )
        operation = connection.execute(
            "SELECT operation_id,desired_state,package_digest FROM external_operations WHERE operation_id=?",
            (attempt["operation_id"],),
        ).fetchone()
        reconciliation = current_operation_reconciliation(connection, attempt["operation_id"])
        if (
            operation is None
            or operation["package_digest"] != attempt["package_digest"]
            or reconciliation is None
            or reconciliation["observed_state"] != "exited"
            or not reconciliation["termination_confirmed"]
        ):
            raise Conflict(
                "Execution success or cancellation requires a confirmed attributed terminal observation."
            )
        if outcome == "succeeded" and operation["desired_state"] != "admitted":
            raise Conflict("Execution success cannot override a requested cancellation.")
        if outcome == "cancelled" and operation["desired_state"] != "cancel_requested":
            raise Conflict("Cancelled outcome requires a requested cancellation.")

    def record_attempt(
        self,
        principal: dict[str, Any],
        variant_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | RecordAttempt,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(RecordAttempt, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            variant = self._variant(connection, actor, variant_id)
            if variant["revision"] != body["expected_variant_revision"]:
                raise Conflict("Experiment variant revision is no longer current.")
            experiment = self._experiment(connection, actor, variant["experiment_id"])
            provenance = self._attempt_provenance(connection, actor, experiment, variant, body)
            now, ident = utc_now(), "experiment_attempt_" + uuid.uuid4().hex
            row = {
                "id": ident,
                "variant_id": variant_id,
                "design_revision": variant["design_revision"],
                **provenance,
                "outcome": body["outcome"],
                "reason": body["reason"],
                "revision": 1,
                "created_at": now,
                "created_by_principal_id": actor["id"],
                "scientific_validity": "not_assessed",
            }
            connection.execute(
                """INSERT INTO research_experiment_attempts
                (id,variant_id,design_revision,provenance_kind,operation_id,execution_plan_digest,
                 package_digest,operation_desired_state,operation_observed_state,termination_status,
                 human_observation,outcome,reason,revision,created_at,created_by_principal_id)
                VALUES (:id,:variant_id,:design_revision,:provenance_kind,:operation_id,
                :execution_plan_digest,:package_digest,:operation_desired_state,
                :operation_observed_state,:termination_status,:human_observation,:outcome,:reason,
                :revision,:created_at,:created_by_principal_id)""",
                row,
            )
            self._event(
                connection,
                actor,
                experiment,
                "experiment.attempt_recorded",
                1,
                {
                    "experiment_id": experiment["id"],
                    "variant_id": variant_id,
                    "attempt_id": ident,
                    "provenance_kind": provenance["provenance_kind"],
                    "operation_id": provenance["operation_id"],
                    "execution_plan_digest": provenance["execution_plan_digest"],
                    "package_digest": provenance["package_digest"],
                    "operation_desired_state": provenance["operation_desired_state"],
                    "operation_observed_state": provenance["operation_observed_state"],
                    "termination_status": provenance["termination_status"],
                    "outcome": body["outcome"],
                    "scientific_validity": "not_assessed",
                },
            )
            return 201, row

        return self._mutate(
            principal, key, "POST", f"/experiment-variants/{variant_id}/attempts", digest, command
        )

    def _attempt_provenance(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        experiment: dict[str, Any],
        variant: dict[str, Any],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if body["provenance_kind"] == "human_reported_observation":
            return {
                "provenance_kind": "human_reported_observation",
                "operation_id": None,
                "execution_plan_digest": None,
                "package_digest": None,
                "operation_desired_state": None,
                "operation_observed_state": "not_observed",
                "termination_status": "not_applicable",
                "human_observation": body["human_observation"],
            }
        if variant["execution_task_id"] is None:
            raise Conflict("Experiment variant does not declare an immutable execution plan.")
        operation = connection.execute(
            """SELECT o.*,g.plan_digest AS grant_plan_digest FROM external_operations o
            JOIN execution_grants g ON g.id=o.grant_id
            WHERE o.operation_id=? AND o.org_id=? AND o.project_id=?""",
            (body["operation_id"], actor["org_id"], experiment["project_id"]),
        ).fetchone()
        if operation is None:
            raise NotFound("External operation was not found.")
        if operation["experiment_attempt_id"] is not None:
            raise Conflict(
                "External operation is already bound to a pre-execution admitted attempt."
            )
        if (
            operation["task_id"] != variant["execution_task_id"]
            or operation["grant_plan_digest"] != variant["execution_plan_digest"]
            or operation["observed_state"] != "exited"
        ):
            raise Conflict(
                "External operation is not admitted for this immutable experiment variant."
            )
        observation = connection.execute(
            """SELECT observed_state,termination_confirmed FROM execution_observations
            WHERE operation_id=? AND grant_id=? AND lease_id=? AND worker_principal_id=?
            AND epoch=? ORDER BY rowid DESC LIMIT 1""",
            (
                operation["operation_id"],
                operation["grant_id"],
                operation["lease_id"],
                operation["worker_principal_id"],
                operation["epoch"],
            ),
        ).fetchone()
        if observation is None or observation["observed_state"] != "exited":
            raise Conflict("External operation has no attributed terminal execution observation.")
        if connection.execute(
            """SELECT 1 FROM research_experiment_attempts
            WHERE operation_id=? AND provenance_kind='admitted_operation'""",
            (operation["operation_id"],),
        ).fetchone():
            raise Conflict("External operation is already bound to an experiment variant.")
        return {
            "provenance_kind": "admitted_operation",
            "operation_id": operation["operation_id"],
            "execution_plan_digest": operation["grant_plan_digest"],
            "package_digest": operation["package_digest"],
            "operation_desired_state": operation["desired_state"],
            "operation_observed_state": "exited",
            "termination_status": (
                "confirmed"
                if observation["termination_confirmed"]
                else "observed_exited_unconfirmed"
            ),
            "human_observation": None,
        }

    def link_attempt_artifacts(
        self,
        principal: dict[str, Any],
        attempt_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | LinkAttemptArtifacts,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(LinkAttemptArtifacts, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            attempt = self._evidence_attempt(connection, actor, attempt_id)
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Experiment attempt revision is no longer current.")
            for artifact_id in body["artifact_ids"]:
                if (
                    attempt["attempt_kind"] == "legacy"
                    and attempt["provenance_kind"] == "human_reported_observation"
                ):
                    linked = connection.execute(
                        "SELECT 1 FROM artifacts WHERE id=? AND project_id=?",
                        (artifact_id, attempt["project_id"]),
                    ).fetchone()
                else:
                    linked = connection.execute(
                        """SELECT 1 FROM artifacts a JOIN external_operations o ON o.lease_id=a.lease_id
                    WHERE a.id=? AND o.operation_id=? AND o.project_id=?""",
                        (artifact_id, attempt["operation_id"], attempt["project_id"]),
                    ).fetchone()
                if linked is None:
                    raise NotFound("Artifact is not linked to this attempt provenance.")
            now, revision = utc_now(), attempt["revision"] + 1
            for artifact_id in body["artifact_ids"]:
                if attempt["attempt_kind"] == "admitted":
                    self._admit_bound_artifact_reference(
                        connection,
                        attempt,
                        artifact_id,
                        f"admitted_experiment_evidence_attempt_{attempt_id}_{artifact_id}",
                        "attempt_artifact",
                        now,
                    )
                else:
                    admit_experiment_artifact_evidence_reference(
                        connection,
                        attempt["project_id"],
                        artifact_id,
                        attempt_id,
                        f"experiment_evidence_attempt_{attempt_id}_{artifact_id}",
                        "attempt_artifact",
                        now,
                    )
            if attempt["attempt_kind"] == "admitted":
                connection.executemany(
                    "INSERT OR IGNORE INTO research_admitted_attempt_artifacts (attempt_id,artifact_id,created_at) VALUES (?,?,?)",
                    [(attempt_id, artifact_id, now) for artifact_id in body["artifact_ids"]],
                )
                revision = _bump_admitted_attempt_revision(connection, attempt)
            else:
                connection.executemany(
                    "INSERT OR IGNORE INTO research_attempt_artifacts (attempt_id,artifact_id,created_at) VALUES (?,?,?)",
                    [(attempt_id, artifact_id, now) for artifact_id in body["artifact_ids"]],
                )
                if (
                    connection.execute(
                        "UPDATE research_experiment_attempts SET revision=? WHERE id=? AND revision=?",
                        (revision, attempt_id, body["expected_revision"]),
                    ).rowcount
                    != 1
                ):
                    raise Conflict("Experiment attempt revision is no longer current.")
            experiment = self._experiment(connection, actor, attempt["experiment_id"])
            self._event(
                connection,
                actor,
                experiment,
                "experiment.attempt_artifacts_linked",
                revision,
                {
                    "experiment_id": experiment["id"],
                    "attempt_id": attempt_id,
                    "artifact_ids": body["artifact_ids"],
                },
            )
            return 200, {
                "attempt_id": attempt_id,
                "artifact_ids": body["artifact_ids"],
                "revision": revision,
            }

        return self._mutate(
            principal, key, "POST", f"/experiment-attempts/{attempt_id}/artifacts", digest, command
        )

    def record_measurement(
        self,
        principal: dict[str, Any],
        attempt_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | RecordMeasurement,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(RecordMeasurement, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            attempt = self._evidence_attempt(connection, actor, attempt_id)
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Experiment attempt revision is no longer current.")
            if (
                body["artifact_id"] is not None
                and connection.execute(
                    (
                        "SELECT 1 FROM research_admitted_attempt_artifacts WHERE attempt_id=? AND artifact_id=?"
                        if attempt["attempt_kind"] == "admitted"
                        else "SELECT 1 FROM research_attempt_artifacts WHERE attempt_id=? AND artifact_id=?"
                    ),
                    (attempt_id, body["artifact_id"]),
                ).fetchone()
                is None
            ):
                raise Conflict("Measurement artifact must already be linked to the attempt.")
            now, revision, ident = (
                utc_now(),
                attempt["revision"] + 1,
                "measurement_" + uuid.uuid4().hex,
            )
            row = {
                "id": ident,
                "attempt_id": attempt_id,
                "name": body["name"],
                "value_json": json.dumps(body["value"], sort_keys=True, separators=(",", ":")),
                "artifact_id": body["artifact_id"],
                "created_at": now,
                "created_by_principal_id": actor["id"],
            }
            table = (
                "research_admitted_attempt_measurements"
                if attempt["attempt_kind"] == "admitted"
                else "research_attempt_measurements"
            )
            connection.execute(
                f"INSERT INTO {table} (id,attempt_id,name,value_json,artifact_id,created_at,created_by_principal_id) VALUES (:id,:attempt_id,:name,:value_json,:artifact_id,:created_at,:created_by_principal_id)",
                row,
            )
            if body["artifact_id"] is not None:
                if attempt["attempt_kind"] == "admitted":
                    self._admit_bound_artifact_reference(
                        connection,
                        attempt,
                        body["artifact_id"],
                        f"admitted_experiment_evidence_measurement_{ident}",
                        "measurement_artifact",
                        now,
                        ident,
                    )
                else:
                    admit_experiment_artifact_evidence_reference(
                        connection,
                        attempt["project_id"],
                        body["artifact_id"],
                        attempt_id,
                        f"experiment_evidence_measurement_{ident}",
                        "measurement_artifact",
                        now,
                        ident,
                    )
            if attempt["attempt_kind"] == "admitted":
                revision = _bump_admitted_attempt_revision(connection, attempt)
            else:
                connection.execute(
                    "UPDATE research_experiment_attempts SET revision=? WHERE id=? AND revision=?",
                    (revision, attempt_id, body["expected_revision"]),
                )
            experiment = self._experiment(connection, actor, attempt["experiment_id"])
            self._event(
                connection,
                actor,
                experiment,
                "experiment.measurement_recorded",
                revision,
                {
                    "experiment_id": experiment["id"],
                    "attempt_id": attempt_id,
                    "measurement_id": ident,
                    "scientific_validity": "not_assessed",
                },
            )
            return 201, self._measurement_response(row) | {
                "attempt_revision": revision,
                "scientific_validity": "not_assessed",
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/experiment-attempts/{attempt_id}/measurements",
            digest,
            command,
        )

    def _validate_evidence(
        self, connection: sqlite3.Connection, experiment_id: str, evidence: list[dict[str, Any]]
    ) -> list[tuple[str, str, str | None, str | None, bool]]:
        rows: list[tuple[str, str, str | None, str | None, bool]] = []
        for citation in evidence:
            legacy = connection.execute(
                """SELECT a.id FROM research_experiment_attempts a JOIN research_experiment_variants v ON v.id=a.variant_id
            WHERE a.id=? AND v.experiment_id=?""",
                (citation["attempt_id"], experiment_id),
            ).fetchone()
            admitted = connection.execute(
                "SELECT id FROM research_experiment_admitted_attempts WHERE id=? AND experiment_id=?",
                (citation["attempt_id"], experiment_id),
            ).fetchone()
            if legacy is None and admitted is None:
                raise NotFound("Cited attempt was not found.")
            artifact_table = (
                "research_admitted_attempt_artifacts"
                if admitted is not None
                else "research_attempt_artifacts"
            )
            measurement_table = (
                "research_admitted_attempt_measurements"
                if admitted is not None
                else "research_attempt_measurements"
            )
            for artifact_id in citation["artifact_ids"]:
                if (
                    connection.execute(
                        f"SELECT 1 FROM {artifact_table} WHERE attempt_id=? AND artifact_id=?",
                        (citation["attempt_id"], artifact_id),
                    ).fetchone()
                    is None
                ):
                    raise Conflict(
                        "Conclusions may cite only artifacts linked to the cited attempt."
                    )
                rows.append(
                    (citation["attempt_id"], artifact_id, artifact_id, None, admitted is not None)
                )
            for measurement_id in citation["measurement_ids"]:
                if (
                    connection.execute(
                        f"SELECT 1 FROM {measurement_table} WHERE id=? AND attempt_id=?",
                        (measurement_id, citation["attempt_id"]),
                    ).fetchone()
                    is None
                ):
                    raise Conflict(
                        "Conclusions may cite only measurements linked to the cited attempt."
                    )
                rows.append(
                    (
                        citation["attempt_id"],
                        measurement_id,
                        None,
                        measurement_id,
                        admitted is not None,
                    )
                )
        if len({(item[0], item[1]) for item in rows}) != len(rows):
            raise Conflict("Conclusion evidence must be unique.")
        return rows

    @staticmethod
    def _write_conclusion_evidence(
        connection: sqlite3.Connection,
        conclusion_id: str,
        evidence: list[tuple[str, str, str | None, str | None, bool]],
    ) -> None:
        connection.executemany(
            "INSERT INTO research_conclusion_evidence (conclusion_id,attempt_id,artifact_id,measurement_id) VALUES (?,?,?,?)",
            [(conclusion_id, item[0], item[2], item[3]) for item in evidence if not item[4]],
        )
        connection.executemany(
            "INSERT INTO research_admitted_conclusion_evidence (conclusion_id,attempt_id,artifact_id,measurement_id) VALUES (?,?,?,?)",
            [(conclusion_id, item[0], item[2], item[3]) for item in evidence if item[4]],
        )

    def create_conclusion(
        self,
        principal: dict[str, Any],
        experiment_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateConclusion,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(CreateConclusion, payload)
        return self._write_conclusion(principal, experiment_id, key, digest, body)

    def update_conclusion(
        self,
        principal: dict[str, Any],
        conclusion_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | UpdateConclusion,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(UpdateConclusion, payload)
        path = f"/conclusions/{conclusion_id}"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            conclusion = self._conclusion(connection, actor, conclusion_id)
            if (
                conclusion["status"] != "draft"
                or conclusion["revision"] != body["expected_revision"]
            ):
                raise Conflict("Reviewed or stale conclusions cannot be changed.")
            if (
                body["design_revision"] is not None
                and body["design_revision"] != conclusion["design_revision"]
            ):
                raise Conflict("Conclusion design snapshot cannot change.")
            evidence = self._validate_evidence(
                connection, conclusion["experiment_id"], body["evidence"]
            )
            revision, now = conclusion["revision"] + 1, utc_now()
            connection.execute(
                "UPDATE research_conclusions SET statement=?,revision=?,updated_at=? WHERE id=? AND revision=? AND status='draft'",
                (body["statement"], revision, now, conclusion_id, body["expected_revision"]),
            )
            connection.execute(
                "DELETE FROM research_conclusion_evidence WHERE conclusion_id=?", (conclusion_id,)
            )
            connection.execute(
                "DELETE FROM research_admitted_conclusion_evidence WHERE conclusion_id=?",
                (conclusion_id,),
            )
            self._write_conclusion_evidence(connection, conclusion_id, evidence)
            experiment = self._experiment(connection, actor, conclusion["experiment_id"])
            self._event(
                connection,
                actor,
                experiment,
                "experiment.conclusion_revised",
                revision,
                {"experiment_id": experiment["id"], "conclusion_id": conclusion_id},
            )
            return 200, self._conclusion_response(
                connection,
                conclusion
                | {"statement": body["statement"], "revision": revision, "updated_at": now},
            )

        return self._mutate(principal, key, "PUT", path, digest, command)

    def _write_conclusion(
        self,
        principal: dict[str, Any],
        experiment_id: str,
        key: str,
        digest: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            experiment = self._experiment(connection, actor, experiment_id)
            evidence = self._validate_evidence(connection, experiment_id, body["evidence"])
            design_revision = body["design_revision"] or experiment["current_design_revision"]
            if (
                connection.execute(
                    "SELECT 1 FROM research_experiment_designs WHERE experiment_id=? AND revision=?",
                    (experiment_id, design_revision),
                ).fetchone()
                is None
            ):
                raise Conflict("Experiment design snapshot was not found.")
            now, ident = utc_now(), "conclusion_" + uuid.uuid4().hex
            row = {
                "id": ident,
                "experiment_id": experiment_id,
                "design_revision": design_revision,
                "statement": body["statement"],
                "status": "draft",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "created_by_principal_id": actor["id"],
                "reviewed_at": None,
                "reviewed_by_principal_id": None,
            }
            connection.execute(
                """INSERT INTO research_conclusions
                (id,experiment_id,design_revision,statement,status,revision,created_at,updated_at,
                 created_by_principal_id,reviewed_at,reviewed_by_principal_id)
                VALUES (:id,:experiment_id,:design_revision,:statement,:status,:revision,:created_at,
                :updated_at,:created_by_principal_id,:reviewed_at,:reviewed_by_principal_id)""",
                row,
            )
            self._write_conclusion_evidence(connection, ident, evidence)
            self._event(
                connection,
                actor,
                experiment,
                "experiment.conclusion_created",
                1,
                {
                    "experiment_id": experiment_id,
                    "conclusion_id": ident,
                    "design_revision": design_revision,
                    "scientific_validity": "not_assessed",
                },
            )
            return 201, self._conclusion_response(connection, row)

        return self._mutate(
            principal, key, "POST", f"/experiments/{experiment_id}/conclusions", digest, command
        )

    def _conclusion(
        self, connection: sqlite3.Connection, actor: dict[str, Any], ident: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT c.* FROM research_conclusions c JOIN research_experiments e ON e.id=c.experiment_id JOIN projects p ON p.id=e.project_id WHERE c.id=? AND p.org_id=? AND (? IS NULL OR e.project_id=?)""",
            (ident, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Conclusion was not found.")
        return dict(row)

    def review_conclusion(
        self,
        principal: dict[str, Any],
        conclusion_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | ReviewConclusion,
    ) -> tuple[int, dict[str, Any]]:
        body = self._body(ReviewConclusion, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            conclusion = self._conclusion(connection, actor, conclusion_id)
            if (
                conclusion["status"] != "draft"
                or conclusion["revision"] != body["expected_revision"]
            ):
                raise Conflict("Conclusion revision is no longer reviewable.")
            if (
                body["design_revision"] is not None
                and body["design_revision"] != conclusion["design_revision"]
            ):
                raise Conflict("Conclusion design snapshot is no longer reviewable.")
            now, revision = utc_now(), conclusion["revision"] + 1
            connection.execute(
                "UPDATE research_conclusions SET status='reviewed',revision=?,updated_at=?,reviewed_at=?,reviewed_by_principal_id=? WHERE id=? AND revision=? AND status='draft'",
                (revision, now, now, actor["id"], conclusion_id, body["expected_revision"]),
            )
            experiment = self._experiment(connection, actor, conclusion["experiment_id"])
            reviewed = conclusion | {
                "status": "reviewed",
                "revision": revision,
                "updated_at": now,
                "reviewed_at": now,
                "reviewed_by_principal_id": actor["id"],
            }
            self._event(
                connection,
                actor,
                experiment,
                "experiment.conclusion_reviewed",
                revision,
                {
                    "experiment_id": experiment["id"],
                    "conclusion_id": conclusion_id,
                    "design_revision": conclusion["design_revision"],
                    "scientific_validity": "not_assessed",
                },
            )
            return 200, self._conclusion_response(connection, reviewed)

        return self._mutate(
            principal, key, "POST", f"/conclusions/{conclusion_id}/review", digest, command
        )

    def amend_conclusion(
        self,
        principal: dict[str, Any],
        conclusion_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | AmendConclusion,
    ) -> tuple[int, dict[str, Any]]:
        """Append, but never rewrite, one reviewed-conclusion correction relationship.

        This is a labbook correction only. It neither changes a plan nor consults
        or creates execution authority, and it deliberately keeps scientific
        validity not assessed.
        """
        body = self._body(AmendConclusion, payload)
        path = f"/conclusions/{conclusion_id}/amendments"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor)
            conclusion = self._conclusion(connection, actor, conclusion_id)
            if conclusion["status"] != "reviewed":
                raise Conflict("Only reviewed conclusions can be amended or retracted.")
            if conclusion["design_revision"] != body["expected_design_revision"]:
                raise Conflict("Conclusion design snapshot is no longer the expected revision.")
            # A head row supplies a compare-and-swap fence without changing the
            # reviewed conclusion's statement, evidence, review attribution, or
            # revision. Its initial revision is the immutable reviewed revision.
            now = utc_now()
            connection.execute(
                """INSERT OR IGNORE INTO research_conclusion_lineage
                (conclusion_id,revision,amendment_id,created_at,updated_at) VALUES (?,?,NULL,?,?)""",
                (conclusion_id, conclusion["revision"], now, now),
            )
            head = connection.execute(
                "SELECT * FROM research_conclusion_lineage WHERE conclusion_id=?", (conclusion_id,)
            ).fetchone()
            if head is None:  # pragma: no cover - database constraints make this defensive.
                raise Conflict("Conclusion lineage is unavailable.")
            if head["amendment_id"] is not None or head["revision"] != body["expected_revision"]:
                raise Conflict("Conclusion amendment revision is no longer current.")
            amendment_id = "conclusion_amendment_" + uuid.uuid4().hex
            lineage_revision = head["revision"] + 1
            if (
                connection.execute(
                    """UPDATE research_conclusion_lineage
                    SET revision=?,amendment_id=?,updated_at=?
                    WHERE conclusion_id=? AND revision=? AND amendment_id IS NULL""",
                    (
                        lineage_revision,
                        amendment_id,
                        now,
                        conclusion_id,
                        body["expected_revision"],
                    ),
                ).rowcount
                != 1
            ):
                raise Conflict("Conclusion amendment revision is no longer current.")
            amendment = {
                "id": amendment_id,
                "conclusion_id": conclusion_id,
                "experiment_id": conclusion["experiment_id"],
                "relation": body["relation"],
                "reason": body["reason"],
                "expected_revision": body["expected_revision"],
                "expected_design_revision": body["expected_design_revision"],
                "prior_lineage_revision": head["revision"],
                "lineage_revision": lineage_revision,
                "created_at": now,
                "actor_principal_id": actor["id"],
                "scientific_validity": "not_assessed",
            }
            connection.execute(
                """INSERT INTO research_conclusion_amendments
                (id,conclusion_id,experiment_id,relation,reason,expected_revision,
                 expected_design_revision,prior_lineage_revision,lineage_revision,
                 created_at,actor_principal_id)
                VALUES (:id,:conclusion_id,:experiment_id,:relation,:reason,:expected_revision,
                :expected_design_revision,:prior_lineage_revision,:lineage_revision,
                :created_at,:actor_principal_id)""",
                amendment,
            )
            experiment = self._experiment(connection, actor, conclusion["experiment_id"])
            self._event(
                connection,
                actor,
                experiment,
                "experiment.conclusion_amended",
                lineage_revision,
                {
                    "experiment_id": experiment["id"],
                    "conclusion_id": conclusion_id,
                    "amendment_id": amendment_id,
                    "relation": body["relation"],
                    "reason": body["reason"],
                    "expected_revision": body["expected_revision"],
                    "expected_design_revision": body["expected_design_revision"],
                    "scientific_validity": "not_assessed",
                },
            )
            return 201, {
                "original": self._conclusion_response(connection, conclusion),
                "amendment": amendment,
            }

        return self._mutate(principal, key, "POST", path, digest, command)

    def _conclusion_response(
        self, connection: sqlite3.Connection, row: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(row)
        result["evidence"] = [
            dict(item)
            for item in connection.execute(
                """SELECT attempt_id,artifact_id,measurement_id FROM research_conclusion_evidence
                WHERE conclusion_id=? UNION ALL SELECT attempt_id,artifact_id,measurement_id
                FROM research_admitted_conclusion_evidence WHERE conclusion_id=?
                ORDER BY attempt_id,artifact_id,measurement_id""",
                (row["id"], row["id"]),
            )
        ]
        result["scientific_validity"] = "not_assessed"
        result["design_snapshot"] = (
            self._design_response(connection, row["experiment_id"], row["design_revision"])
            if row["design_revision"] is not None
            else None
        )
        lineage = connection.execute(
            "SELECT revision FROM research_conclusion_lineage WHERE conclusion_id=?", (row["id"],)
        ).fetchone()
        result["lineage_revision"] = lineage["revision"] if lineage is not None else row["revision"]
        result["lineage"] = [
            self._amendment_response(item)
            for item in connection.execute(
                """SELECT * FROM research_conclusion_amendments WHERE conclusion_id=?
                ORDER BY lineage_revision,id""",
                (row["id"],),
            )
        ]
        return result

    @staticmethod
    def _amendment_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["scientific_validity"] = "not_assessed"
        return result

    def _design_response(
        self, connection: sqlite3.Connection, experiment_id: str, revision: int
    ) -> dict[str, Any]:
        design = connection.execute(
            "SELECT * FROM research_experiment_designs WHERE experiment_id=? AND revision=?",
            (experiment_id, revision),
        ).fetchone()
        if design is None:
            raise Conflict("Experiment design snapshot is missing.")
        result = dict(design)
        result["variants"] = [
            self._variant_response(dict(row))
            for row in connection.execute(
                """SELECT * FROM research_experiment_variants
                WHERE experiment_id=? AND design_revision=? ORDER BY kind""",
                (experiment_id, revision),
            )
        ]
        return result

    def _experiment_response(
        self, connection: sqlite3.Connection, experiment: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(experiment)
        result["design"] = self._design_response(
            connection, experiment["id"], experiment["current_design_revision"]
        )
        return result

    @staticmethod
    def _artifact_evidence_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        """Expose storage custody separately from immutable quarantine intake status."""
        result = dict(row)
        retention_state = result.get("retention_state")
        if retention_state is None:
            retention_state = (
                "retention_metadata_missing" if result.pop("has_placement") else "legacy_unplaced"
            )
        else:
            result.pop("has_placement")
        result["retention_state"] = retention_state
        result["custody_state"] = retention_state
        return result

    def export_experiment(
        self,
        principal: dict[str, Any],
        experiment_id: str,
        payload: dict[str, Any] | ExportExperiment | None = None,
    ) -> dict[str, Any]:
        body = self._body(ExportExperiment, payload or {})
        connection = _connect(self.database_path)
        try:
            # One deferred transaction fences authorization, revision context, all
            # aggregate preflight checks, and the resulting snapshot together.
            connection.execute("BEGIN")
            actor = self._live_principal(connection, principal)
            experiment = self._experiment(connection, actor, experiment_id)
            # Bound every exported collection before retaining rows. Nested
            # conclusion evidence and the distinct artifact projection count too;
            # otherwise a "complete" response could still grow without limit.
            bounded_queries = (
                (
                    "designs",
                    "SELECT count(*) FROM research_experiment_designs WHERE experiment_id=?",
                    (experiment_id,),
                ),
                (
                    "variants",
                    "SELECT count(*) FROM research_experiment_variants WHERE experiment_id=?",
                    (experiment_id,),
                ),
                (
                    "artifacts",
                    """SELECT count(DISTINCT artifact_id) FROM (
                    SELECT aa.artifact_id FROM research_attempt_artifacts aa
                    JOIN research_experiment_attempts a ON a.id=aa.attempt_id
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE v.experiment_id=? UNION SELECT aa.artifact_id
                    FROM research_admitted_attempt_artifacts aa
                    JOIN research_experiment_admitted_attempts a ON a.id=aa.attempt_id
                    WHERE a.experiment_id=?)""",
                    (experiment_id, experiment_id),
                ),
                (
                    "artifact links",
                    """SELECT count(*) FROM (SELECT aa.artifact_id FROM research_attempt_artifacts aa
                    JOIN research_experiment_attempts a ON a.id=aa.attempt_id
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE v.experiment_id=? UNION ALL SELECT aa.artifact_id
                    FROM research_admitted_attempt_artifacts aa
                    JOIN research_experiment_admitted_attempts a ON a.id=aa.attempt_id
                    WHERE a.experiment_id=?)""",
                    (experiment_id, experiment_id),
                ),
                (
                    "operations",
                    """SELECT count(DISTINCT o.operation_id) FROM external_operations o
                    WHERE o.experiment_id=? OR EXISTS (SELECT 1 FROM research_experiment_attempts a
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE a.operation_id=o.operation_id AND v.experiment_id=?) OR EXISTS (
                    SELECT 1 FROM research_experiment_admitted_attempts a
                    WHERE a.operation_id=o.operation_id AND a.experiment_id=?)""",
                    (experiment_id, experiment_id, experiment_id),
                ),
                (
                    "admitted attempts",
                    "SELECT count(*) FROM research_experiment_admitted_attempts WHERE experiment_id=?",
                    (experiment_id,),
                ),
                (
                    "kubernetes dispatches",
                    """SELECT count(*) FROM kubernetes_dispatches d
                    JOIN research_experiment_admitted_attempts a ON a.id=d.admitted_attempt_id
                    WHERE a.experiment_id=?""",
                    (experiment_id,),
                ),
                (
                    "admitted attempt state history",
                    """SELECT count(*) FROM research_experiment_admitted_attempt_states s
                    JOIN research_experiment_admitted_attempts a ON a.id=s.attempt_id
                    WHERE a.experiment_id=?""",
                    (experiment_id,),
                ),
                (
                    "attempts",
                    """SELECT count(*) FROM research_experiment_attempts a
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE v.experiment_id=?""",
                    (experiment_id,),
                ),
                (
                    "measurements",
                    """SELECT count(*) FROM (SELECT m.id FROM research_attempt_measurements m
                    JOIN research_experiment_attempts a ON a.id=m.attempt_id
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE v.experiment_id=? UNION ALL SELECT m.id
                    FROM research_admitted_attempt_measurements m
                    JOIN research_experiment_admitted_attempts a ON a.id=m.attempt_id
                    WHERE a.experiment_id=?)""",
                    (experiment_id, experiment_id),
                ),
                (
                    "conclusions",
                    "SELECT count(*) FROM research_conclusions WHERE experiment_id=?",
                    (experiment_id,),
                ),
                (
                    "conclusion evidence",
                    """SELECT count(*) FROM (SELECT ce.conclusion_id FROM research_conclusion_evidence ce
                    JOIN research_conclusions c ON c.id=ce.conclusion_id
                    WHERE c.experiment_id=? UNION ALL SELECT ce.conclusion_id
                    FROM research_admitted_conclusion_evidence ce
                    JOIN research_conclusions c ON c.id=ce.conclusion_id
                    WHERE c.experiment_id=?)""",
                    (experiment_id, experiment_id),
                ),
                (
                    "conclusion amendments",
                    "SELECT count(*) FROM research_conclusion_amendments WHERE experiment_id=?",
                    (experiment_id,),
                ),
            )
            for collection, query, parameters in bounded_queries:
                if connection.execute(query, parameters).fetchone()[0] > EXPORT_RECORD_LIMIT:
                    raise Conflict(f"Export exceeds the per-experiment {collection} record limit.")

            result: dict[str, Any] = {
                "experiment": self._experiment_response(connection, experiment),
                "designs": [],
                "variants": [],
                "attempts": [],
                "admitted_attempts": [],
                "kubernetes_dispatches": [],
                "operations": [],
                "artifact_links": [],
                "artifacts": [],
                "measurements": [],
                "conclusions": [],
                "conclusion_amendments": [],
                "completeness": "complete_in_scope_goal_to_decision_chain",
            }

            # This is Starlette JSONResponse's compact UTF-8 encoding. The exact
            # final assertion is defense in depth; append rejects before retaining
            # an over-limit aggregate and never drops rows.
            def encoded_size(value: dict[str, Any]) -> int:
                return len(
                    json.dumps(
                        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode()
                )

            used = encoded_size(result)
            if used > body["max_bytes"]:
                raise Conflict("Export exceeds the requested byte cap.")

            def append(section: str, item: dict[str, Any]) -> None:
                nonlocal used
                used += encoded_size(item) + (1 if result[section] else 0)
                if used > body["max_bytes"]:
                    raise Conflict("Export exceeds the requested byte cap.")
                result[section].append(item)

            for row in connection.execute(
                "SELECT * FROM research_experiment_designs WHERE experiment_id=? ORDER BY revision",
                (experiment_id,),
            ):
                append("designs", self._design_response(connection, experiment_id, row["revision"]))
            for row in connection.execute(
                "SELECT * FROM research_experiment_variants WHERE experiment_id=? ORDER BY design_revision,kind",
                (experiment_id,),
            ):
                append("variants", self._variant_response(dict(row)))
            for row in connection.execute(
                """SELECT a.*,v.experiment_id FROM research_experiment_attempts a
                JOIN research_experiment_variants v ON v.id=a.variant_id
                WHERE v.experiment_id=? ORDER BY a.created_at,a.id""",
                (experiment_id,),
            ):
                attempt = dict(row)
                attempt.pop("experiment_id")
                attempt["scientific_validity"] = "not_assessed"
                append("attempts", attempt)
            for row in connection.execute(
                """SELECT * FROM research_experiment_admitted_attempts WHERE experiment_id=?
                ORDER BY created_at,id""",
                (experiment_id,),
            ):
                admission = dict(row)
                decision = connection.execute(
                    """SELECT outcome,reason,expected_revision,decided_at,decided_by_principal_id
                    FROM research_experiment_admitted_attempt_resolutions WHERE attempt_id=?""",
                    (admission["id"],),
                ).fetchone()
                admission["human_outcome"] = dict(decision) if decision is not None else None
                if admission["operational_termination_confirmed"] is not None:
                    admission["operational_termination_confirmed"] = bool(
                        admission["operational_termination_confirmed"]
                    )
                admission["state_history"] = [
                    dict(state)
                    for state in connection.execute(
                        """SELECT * FROM research_experiment_admitted_attempt_states
                        WHERE attempt_id=? ORDER BY sequence""",
                        (admission["id"],),
                    )
                ]
                admission["scientific_validity"] = "not_assessed"
                append("admitted_attempts", admission)
            for row in connection.execute(
                """SELECT d.* FROM kubernetes_dispatches d
                JOIN research_experiment_admitted_attempts a ON a.id=d.admitted_attempt_id
                WHERE a.experiment_id=? ORDER BY d.created_at,d.id""",
                (experiment_id,),
            ):
                append("kubernetes_dispatches", dict(row))
            for row in connection.execute(
                """SELECT o.* FROM external_operations o WHERE o.org_id=? AND o.project_id=?
                AND (o.experiment_id=? OR EXISTS (SELECT 1 FROM research_experiment_attempts a
                JOIN research_experiment_variants v ON v.id=a.variant_id
                WHERE a.operation_id=o.operation_id AND v.experiment_id=?) OR EXISTS (
                SELECT 1 FROM research_experiment_admitted_attempts a
                WHERE a.operation_id=o.operation_id AND a.experiment_id=?))
                ORDER BY o.created_at,o.operation_id""",
                (
                    actor["org_id"],
                    experiment["project_id"],
                    experiment_id,
                    experiment_id,
                    experiment_id,
                ),
            ):
                append("operations", dict(row))
            for row in connection.execute(
                """SELECT aa.* FROM research_attempt_artifacts aa
                JOIN research_experiment_attempts a ON a.id=aa.attempt_id
                JOIN research_experiment_variants v ON v.id=a.variant_id
                WHERE v.experiment_id=? ORDER BY aa.attempt_id,aa.artifact_id""",
                (experiment_id,),
            ):
                append("artifact_links", dict(row))
            for row in connection.execute(
                """SELECT aa.* FROM research_admitted_attempt_artifacts aa
                JOIN research_experiment_admitted_attempts a ON a.id=aa.attempt_id
                WHERE a.experiment_id=? ORDER BY aa.attempt_id,aa.artifact_id""",
                (experiment_id,),
            ):
                append("artifact_links", dict(row))
            for row in connection.execute(
                """SELECT a.id,a.project_id,a.task_id,a.grant_id,a.lease_id,a.worker_principal_id,
                a.epoch,a.operation_id,a.relative_path,a.sha256,a.size,a.created_at,a.status,
                classification,late,r.retention_class,r.state AS retention_state,
                CASE WHEN p.artifact_id IS NULL THEN 0 ELSE 1 END AS has_placement
                FROM artifacts a
                LEFT JOIN project_artifact_retention r ON r.artifact_id=a.id AND r.project_id=a.project_id
                LEFT JOIN project_artifact_placements p ON p.artifact_id=a.id AND p.project_id=a.project_id
                WHERE a.project_id=? AND (EXISTS (SELECT 1 FROM research_attempt_artifacts aa
                JOIN research_experiment_attempts ea ON ea.id=aa.attempt_id
                JOIN research_experiment_variants v ON v.id=ea.variant_id
                WHERE aa.artifact_id=a.id AND v.experiment_id=?) OR EXISTS (
                SELECT 1 FROM research_admitted_attempt_artifacts aa
                JOIN research_experiment_admitted_attempts ea ON ea.id=aa.attempt_id
                WHERE aa.artifact_id=a.id AND ea.experiment_id=?)) ORDER BY a.created_at,a.id""",
                (experiment["project_id"], experiment_id, experiment_id),
            ):
                append("artifacts", self._artifact_evidence_response(row))
            for row in connection.execute(
                """SELECT m.* FROM research_attempt_measurements m
                JOIN research_experiment_attempts a ON a.id=m.attempt_id
                JOIN research_experiment_variants v ON v.id=a.variant_id
                WHERE v.experiment_id=? ORDER BY m.created_at,m.id""",
                (experiment_id,),
            ):
                append("measurements", self._measurement_response(row))
            for row in connection.execute(
                """SELECT m.* FROM research_admitted_attempt_measurements m
                JOIN research_experiment_admitted_attempts a ON a.id=m.attempt_id
                WHERE a.experiment_id=? ORDER BY m.created_at,m.id""",
                (experiment_id,),
            ):
                append("measurements", self._measurement_response(row))
            for row in connection.execute(
                "SELECT * FROM research_conclusions WHERE experiment_id=? ORDER BY created_at,id",
                (experiment_id,),
            ):
                append("conclusions", self._conclusion_response(connection, dict(row)))
            for row in connection.execute(
                """SELECT * FROM research_conclusion_amendments WHERE experiment_id=?
                ORDER BY created_at,id""",
                (experiment_id,),
            ):
                append("conclusion_amendments", self._amendment_response(row))
            if encoded_size(result) > body["max_bytes"]:
                raise Conflict("Export exceeds the requested byte cap.")
            return result
        finally:
            connection.close()

    get_experiment = export_experiment
