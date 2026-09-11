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
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .experiments_models import (
    AmendConclusion,
    CreateConclusion,
    CreateExperiment,
    ExportExperiment,
    LinkAttemptArtifacts,
    RecordAttempt,
    RecordMeasurement,
    ReviewConclusion,
    ReviseExperimentDesign,
    UpdateConclusion,
)
from .project_artifact_retention import admit_experiment_artifact_evidence_reference
from .store import Conflict, Forbidden, NotFound, Store, StoreError, _connect, utc_now

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
            attempt = self._attempt(connection, actor, attempt_id)
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Experiment attempt revision is no longer current.")
            for artifact_id in body["artifact_ids"]:
                if attempt["provenance_kind"] == "human_reported_observation":
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
                admit_experiment_artifact_evidence_reference(
                    connection,
                    attempt["project_id"],
                    artifact_id,
                    attempt_id,
                    f"experiment_evidence_attempt_{attempt_id}_{artifact_id}",
                    "attempt_artifact",
                    now,
                )
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
            attempt = self._attempt(connection, actor, attempt_id)
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Experiment attempt revision is no longer current.")
            if (
                body["artifact_id"] is not None
                and connection.execute(
                    "SELECT 1 FROM research_attempt_artifacts WHERE attempt_id=? AND artifact_id=?",
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
            connection.execute(
                """INSERT INTO research_attempt_measurements (id,attempt_id,name,value_json,artifact_id,created_at,created_by_principal_id) VALUES (:id,:attempt_id,:name,:value_json,:artifact_id,:created_at,:created_by_principal_id)""",
                row,
            )
            if body["artifact_id"] is not None:
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
    ) -> list[tuple[str, str, str | None, str | None]]:
        rows: list[tuple[str, str, str | None, str | None]] = []
        for citation in evidence:
            attempt = connection.execute(
                """SELECT a.id FROM research_experiment_attempts a JOIN research_experiment_variants v ON v.id=a.variant_id
            WHERE a.id=? AND v.experiment_id=?""",
                (citation["attempt_id"], experiment_id),
            ).fetchone()
            if attempt is None:
                raise NotFound("Cited attempt was not found.")
            for artifact_id in citation["artifact_ids"]:
                if (
                    connection.execute(
                        "SELECT 1 FROM research_attempt_artifacts WHERE attempt_id=? AND artifact_id=?",
                        (citation["attempt_id"], artifact_id),
                    ).fetchone()
                    is None
                ):
                    raise Conflict(
                        "Conclusions may cite only artifacts linked to the cited attempt."
                    )
                rows.append((citation["attempt_id"], artifact_id, artifact_id, None))
            for measurement_id in citation["measurement_ids"]:
                if (
                    connection.execute(
                        "SELECT 1 FROM research_attempt_measurements WHERE id=? AND attempt_id=?",
                        (measurement_id, citation["attempt_id"]),
                    ).fetchone()
                    is None
                ):
                    raise Conflict(
                        "Conclusions may cite only measurements linked to the cited attempt."
                    )
                rows.append((citation["attempt_id"], measurement_id, None, measurement_id))
        if len({(item[0], item[1]) for item in rows}) != len(rows):
            raise Conflict("Conclusion evidence must be unique.")
        return rows

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
            connection.executemany(
                "INSERT INTO research_conclusion_evidence (conclusion_id,attempt_id,artifact_id,measurement_id) VALUES (?,?,?,?)",
                [(conclusion_id, item[0], item[2], item[3]) for item in evidence],
            )
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
            connection.executemany(
                "INSERT INTO research_conclusion_evidence (conclusion_id,attempt_id,artifact_id,measurement_id) VALUES (?,?,?,?)",
                [(ident, item[0], item[2], item[3]) for item in evidence],
            )
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
                "SELECT attempt_id,artifact_id,measurement_id FROM research_conclusion_evidence WHERE conclusion_id=? ORDER BY attempt_id,artifact_id,measurement_id",
                (row["id"],),
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
                    """SELECT count(DISTINCT aa.artifact_id) FROM research_attempt_artifacts aa
                    JOIN research_experiment_attempts a ON a.id=aa.attempt_id
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE v.experiment_id=?""",
                    (experiment_id,),
                ),
                (
                    "artifact links",
                    """SELECT count(*) FROM research_attempt_artifacts aa
                    JOIN research_experiment_attempts a ON a.id=aa.attempt_id
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE v.experiment_id=?""",
                    (experiment_id,),
                ),
                (
                    "operations",
                    """SELECT count(DISTINCT o.operation_id) FROM external_operations o
                    JOIN research_experiment_attempts a ON a.operation_id=o.operation_id
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE v.experiment_id=?""",
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
                    """SELECT count(*) FROM research_attempt_measurements m
                    JOIN research_experiment_attempts a ON a.id=m.attempt_id
                    JOIN research_experiment_variants v ON v.id=a.variant_id
                    WHERE v.experiment_id=?""",
                    (experiment_id,),
                ),
                (
                    "conclusions",
                    "SELECT count(*) FROM research_conclusions WHERE experiment_id=?",
                    (experiment_id,),
                ),
                (
                    "conclusion evidence",
                    """SELECT count(*) FROM research_conclusion_evidence ce
                    JOIN research_conclusions c ON c.id=ce.conclusion_id
                    WHERE c.experiment_id=?""",
                    (experiment_id,),
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
                """SELECT o.* FROM external_operations o WHERE o.org_id=? AND o.project_id=?
                AND EXISTS (SELECT 1 FROM research_experiment_attempts a
                JOIN research_experiment_variants v ON v.id=a.variant_id
                WHERE a.operation_id=o.operation_id AND v.experiment_id=?)
                ORDER BY o.created_at,o.operation_id""",
                (actor["org_id"], experiment["project_id"], experiment_id),
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
                """SELECT a.id,a.project_id,a.task_id,a.grant_id,a.lease_id,a.worker_principal_id,
                a.epoch,a.operation_id,a.relative_path,a.sha256,a.size,a.created_at,a.status,
                classification,late,r.retention_class,r.state AS retention_state,
                CASE WHEN p.artifact_id IS NULL THEN 0 ELSE 1 END AS has_placement
                FROM artifacts a
                LEFT JOIN project_artifact_retention r ON r.artifact_id=a.id AND r.project_id=a.project_id
                LEFT JOIN project_artifact_placements p ON p.artifact_id=a.id AND p.project_id=a.project_id
                WHERE a.project_id=? AND EXISTS (SELECT 1 FROM research_attempt_artifacts aa
                JOIN research_experiment_attempts ea ON ea.id=aa.attempt_id
                JOIN research_experiment_variants v ON v.id=ea.variant_id
                WHERE aa.artifact_id=a.id AND v.experiment_id=?) ORDER BY a.created_at,a.id""",
                (experiment["project_id"], experiment_id),
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
