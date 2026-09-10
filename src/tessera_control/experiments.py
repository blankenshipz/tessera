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
    CreateConclusion,
    CreateExperiment,
    LinkAttemptArtifacts,
    RecordAttempt,
    RecordMeasurement,
    ReviewConclusion,
    ReviseExperimentDesign,
    UpdateConclusion,
)
from .store import Conflict, Forbidden, NotFound, Store, StoreError, _connect, utc_now

T = TypeVar("T", bound=BaseModel)
EXPORT_RECORD_LIMIT = 1_000


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
        revision INTEGER NOT NULL CHECK (revision >= 1), protocol TEXT NOT NULL,
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY (experiment_id,revision))""",
        """CREATE TABLE IF NOT EXISTS research_experiment_variants (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('baseline','intervention')),
        name TEXT NOT NULL, method_digest TEXT NOT NULL, input_digest TEXT NOT NULL,
        metadata_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(experiment_id,design_revision,kind),
        FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision))""",
        "CREATE INDEX IF NOT EXISTS research_experiment_variants_by_experiment ON research_experiment_variants(experiment_id,design_revision,id)",
        """CREATE TABLE IF NOT EXISTS research_experiment_attempts (
        id TEXT PRIMARY KEY, variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        operation_id TEXT NOT NULL REFERENCES external_operations(operation_id),
        outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','cancelled','inconclusive')),
        reason TEXT, revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK ((outcome = 'succeeded') OR reason IS NOT NULL))""",
        "CREATE INDEX IF NOT EXISTS research_experiment_attempts_by_variant ON research_experiment_attempts(variant_id,created_at,id)",
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
        statement TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('draft','reviewed')),
        revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id), reviewed_at TEXT,
        reviewed_by_principal_id TEXT REFERENCES principals(id))""",
        "CREATE INDEX IF NOT EXISTS research_conclusions_by_experiment ON research_conclusions(experiment_id,created_at,id)",
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
            "INSERT INTO research_experiment_designs (experiment_id,revision,protocol,created_at,created_by_principal_id) VALUES (?,?,?,?,?)",
            (experiment["id"], revision, protocol, now, actor["id"]),
        )
        for variant in variants:
            connection.execute(
                """INSERT INTO research_experiment_variants (id,experiment_id,design_revision,kind,name,method_digest,input_digest,metadata_json,revision,created_at,created_by_principal_id) VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    "experiment_variant_" + uuid.uuid4().hex,
                    experiment["id"],
                    revision,
                    variant["kind"],
                    variant["name"],
                    variant["method_digest"],
                    variant["input_digest"],
                    json.dumps(variant["metadata"], sort_keys=True, separators=(",", ":")),
                    now,
                    actor["id"],
                ),
            )

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
            operation = connection.execute(
                "SELECT project_id FROM external_operations WHERE operation_id=?",
                (body["operation_id"],),
            ).fetchone()
            if operation is None or operation["project_id"] != experiment["project_id"]:
                raise NotFound("External operation was not found.")
            now, ident = utc_now(), "experiment_attempt_" + uuid.uuid4().hex
            row = {
                "id": ident,
                "variant_id": variant_id,
                "operation_id": body["operation_id"],
                "outcome": body["outcome"],
                "reason": body["reason"],
                "revision": 1,
                "created_at": now,
                "created_by_principal_id": actor["id"],
                "scientific_validity": "not_assessed",
            }
            connection.execute(
                """INSERT INTO research_experiment_attempts (id,variant_id,operation_id,outcome,reason,revision,created_at,created_by_principal_id) VALUES (:id,:variant_id,:operation_id,:outcome,:reason,:revision,:created_at,:created_by_principal_id)""",
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
                    "operation_id": body["operation_id"],
                    "outcome": body["outcome"],
                    "scientific_validity": "not_assessed",
                },
            )
            return 201, row

        return self._mutate(
            principal, key, "POST", f"/experiment-variants/{variant_id}/attempts", digest, command
        )

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
                linked = connection.execute(
                    """SELECT 1 FROM artifacts a JOIN external_operations o ON o.lease_id=a.lease_id
                WHERE a.id=? AND o.operation_id=? AND o.project_id=?""",
                    (artifact_id, attempt["operation_id"], attempt["project_id"]),
                ).fetchone()
                if linked is None:
                    raise NotFound("Artifact is not linked to this attempt operation.")
            now, revision = utc_now(), attempt["revision"] + 1
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
            now, ident = utc_now(), "conclusion_" + uuid.uuid4().hex
            row = {
                "id": ident,
                "experiment_id": experiment_id,
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
                """INSERT INTO research_conclusions (id,experiment_id,statement,status,revision,created_at,updated_at,created_by_principal_id,reviewed_at,reviewed_by_principal_id) VALUES (:id,:experiment_id,:statement,:status,:revision,:created_at,:updated_at,:created_by_principal_id,:reviewed_at,:reviewed_by_principal_id)""",
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
                    "scientific_validity": "not_assessed",
                },
            )
            return 200, self._conclusion_response(connection, reviewed)

        return self._mutate(
            principal, key, "POST", f"/conclusions/{conclusion_id}/review", digest, command
        )

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
        return result

    def _experiment_response(
        self, connection: sqlite3.Connection, experiment: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(experiment)
        design = connection.execute(
            "SELECT * FROM research_experiment_designs WHERE experiment_id=? AND revision=?",
            (experiment["id"], experiment["current_design_revision"]),
        ).fetchone()
        result["design"] = dict(design)
        result["design"]["variants"] = [
            self._variant_response(dict(row))
            for row in connection.execute(
                "SELECT * FROM research_experiment_variants WHERE experiment_id=? AND design_revision=? ORDER BY kind",
                (experiment["id"], experiment["current_design_revision"]),
            )
        ]
        return result

    def export_experiment(self, principal: dict[str, Any], experiment_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._live_principal(connection, principal)
            experiment = self._experiment(connection, actor, experiment_id)
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
                "completeness": "complete_in_scope_goal_to_decision_chain",
            }
            tables = (
                "research_experiment_designs",
                "research_experiment_variants",
                "research_conclusions",
            )
            if any(
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE experiment_id=?", (experiment_id,)
                ).fetchone()[0]
                > EXPORT_RECORD_LIMIT
                for table in tables
            ):
                raise Conflict("Export exceeds the per-experiment record limit.")
            for row in connection.execute(
                "SELECT * FROM research_experiment_designs WHERE experiment_id=? ORDER BY revision",
                (experiment_id,),
            ):
                result["designs"].append(dict(row))
            for row in connection.execute(
                "SELECT * FROM research_experiment_variants WHERE experiment_id=? ORDER BY design_revision,kind",
                (experiment_id,),
            ):
                result["variants"].append(self._variant_response(dict(row)))
            for row in connection.execute(
                """SELECT a.*,v.experiment_id FROM research_experiment_attempts a JOIN research_experiment_variants v ON v.id=a.variant_id WHERE v.experiment_id=? ORDER BY a.created_at,a.id""",
                (experiment_id,),
            ):
                attempt = dict(row)
                attempt.pop("experiment_id")
                attempt["scientific_validity"] = "not_assessed"
                result["attempts"].append(attempt)
            attempt_ids = [item["id"] for item in result["attempts"]]
            if attempt_ids:
                placeholders = ",".join("?" for _ in attempt_ids)
                operation_ids = [item["operation_id"] for item in result["attempts"]]
                operation_placeholders = ",".join("?" for _ in operation_ids)
                result["operations"] = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM external_operations WHERE operation_id IN ({operation_placeholders}) "
                        "ORDER BY created_at,operation_id",
                        operation_ids,
                    )
                ]
                result["artifact_links"] = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM research_attempt_artifacts WHERE attempt_id IN ({placeholders}) ORDER BY attempt_id,artifact_id",
                        attempt_ids,
                    )
                ]
                artifact_ids = [item["artifact_id"] for item in result["artifact_links"]]
                if artifact_ids:
                    artifact_placeholders = ",".join("?" for _ in artifact_ids)
                    result["artifacts"] = [
                        dict(row)
                        for row in connection.execute(
                            f"""SELECT id,project_id,task_id,grant_id,lease_id,worker_principal_id,
                            epoch,operation_id,relative_path,sha256,size,created_at,status,
                            classification,late FROM artifacts WHERE id IN ({artifact_placeholders})
                            ORDER BY created_at,id""",
                            artifact_ids,
                        )
                    ]
                result["measurements"] = [
                    self._measurement_response(row)
                    for row in connection.execute(
                        f"SELECT * FROM research_attempt_measurements WHERE attempt_id IN ({placeholders}) ORDER BY created_at,id",
                        attempt_ids,
                    )
                ]
            for row in connection.execute(
                "SELECT * FROM research_conclusions WHERE experiment_id=? ORDER BY created_at,id",
                (experiment_id,),
            ):
                result["conclusions"].append(self._conclusion_response(connection, dict(row)))
            return result
        finally:
            connection.close()

    get_experiment = export_experiment
