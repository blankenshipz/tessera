"""Synthetic-only durable commits of reconciled quarantined execution outputs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .artifact_manifests import MAX_MANIFEST_ENTRIES, ArtifactManifestStore, ManifestArtifact
from .artifacts import ArtifactStore
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    canonical_digest,
    utc_now,
)

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def create_result_checkpoint_schema(connection: sqlite3.Connection) -> None:
    """Install immutable records; manifests and their bytes remain quarantined."""
    connection.execute(
        """CREATE TABLE execution_result_checkpoints (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        operation_id TEXT NOT NULL,
        manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id) UNIQUE,
        manifest_digest TEXT NOT NULL CHECK (
            length(manifest_digest) = 64 AND manifest_digest NOT GLOB '*[^0-9a-f]*'
        ),
        grant_revision INTEGER NOT NULL CHECK (grant_revision >= 1),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        validation_contract TEXT NOT NULL CHECK (
            validation_contract = 'synthetic-declared-output-v1'
        ),
        validation_json TEXT NOT NULL,
        validation_digest TEXT NOT NULL CHECK (
            length(validation_digest) = 64 AND validation_digest NOT GLOB '*[^0-9a-f]*'
        ),
        commit_digest TEXT NOT NULL CHECK (
            length(commit_digest) = 64 AND commit_digest NOT GLOB '*[^0-9a-f]*'
        ),
        committed_at TEXT NOT NULL,
        committed_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        state TEXT NOT NULL CHECK (state = 'committed_quarantined')
        )"""
    )
    connection.execute(
        """CREATE INDEX execution_result_checkpoints_by_project
        ON execution_result_checkpoints(project_id, committed_at, id)"""
    )


class CommitExecutionResultCheckpoint(BaseModel):
    """A human-controlled synthetic byte/metadata validation command."""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_grant_revision: int = Field(strict=True, ge=1, le=2**63 - 1)
    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)
    lease_id: Identifier
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    manifest_id: Identifier
    expected_manifest_digest: Digest
    validation_contract: Literal["synthetic-declared-output-v1"]
    expected_artifacts: list[ManifestArtifact] = Field(max_length=MAX_MANIFEST_ENTRIES)
    expected_total_bytes: int = Field(strict=True, ge=0, le=16 * 1024 * 1024)


def _response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["validation"] = json.loads(value.pop("validation_json"))
    # This is an execution/accounting checkpoint only.  It intentionally does not
    # alter the quarantined manifest, claim an independent execution receipt, or
    # make any scientific assessment.
    value["synthetic_validation"] = True
    value["scientific_validity"] = "not_assessed"
    value["execution_receipt"] = "trusted_worker_termination_observation_only"
    return value


class ResultCheckpointStore(Store):
    """Human checkpoint commits over already reconciled synthetic execution records."""

    @staticmethod
    def _require_human(principal: dict[str, Any]) -> None:
        if principal["role"] != "human":
            raise Forbidden("A human is required to commit an execution result checkpoint.")

    @staticmethod
    def _checkpoint_id(manifest_id: str) -> str:
        return "result_checkpoint_" + hashlib.sha256(manifest_id.encode()).hexdigest()[:32]

    @staticmethod
    def _validation(
        manifest: sqlite3.Row, artifacts: list[dict[str, Any]], total_bytes: int
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "contract": "synthetic-declared-output-v1",
            "manifest_digest": manifest["manifest_digest"],
            "artifacts": artifacts,
            "total_bytes": total_bytes,
        }

    def _manifest_for_commit(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        grant_id: str,
        body: dict[str, Any],
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        grant = connection.execute(
            """SELECT g.* FROM execution_grants g JOIN projects p ON p.id=g.project_id
            WHERE g.id=? AND p.org_id=?""",
            (grant_id, actor["org_id"]),
        ).fetchone()
        if grant is None:
            raise NotFound("Execution grant was not found.")
        self._project(connection, actor, grant["project_id"])
        if grant["revision"] != body["expected_grant_revision"]:
            raise Conflict("Execution grant revision is no longer current.")
        if grant["status"] != "reconciled":
            raise Conflict("Execution result checkpoint requires a reconciled execution grant.")
        lease = connection.execute(
            "SELECT * FROM execution_leases WHERE id=? AND grant_id=?",
            (body["lease_id"], grant_id),
        ).fetchone()
        if lease is None:
            raise Conflict("Execution lease does not belong to this grant.")
        if grant["epoch"] != body["expected_epoch"] or lease["epoch"] != body["expected_epoch"]:
            raise Conflict("Execution result checkpoint epoch is no longer current.")
        if body["operation_id"] != ArtifactStore._expected_operation(lease["id"]):
            raise Conflict("Execution result checkpoint operation does not match the lease.")
        observation = connection.execute(
            """SELECT lease_id, epoch, observed_state, termination_confirmed
            FROM execution_observations WHERE grant_id=? ORDER BY rowid DESC LIMIT 1""",
            (grant_id,),
        ).fetchone()
        if (
            observation is None
            or observation["lease_id"] != lease["id"]
            or observation["epoch"] != body["expected_epoch"]
            or observation["observed_state"] != "exited"
            or not observation["termination_confirmed"]
        ):
            raise Conflict("Confirmed worker termination is required for result checkpointing.")
        manifest = connection.execute(
            """SELECT * FROM artifact_manifests WHERE id=? AND project_id=? AND task_id=?
            AND grant_id=? AND lease_id=?""",
            (body["manifest_id"], grant["project_id"], grant["task_id"], grant_id, lease["id"]),
        ).fetchone()
        if manifest is None:
            raise NotFound("Artifact manifest was not found.")
        if (
            manifest["worker_principal_id"] != lease["worker_principal_id"]
            or manifest["epoch"] != body["expected_epoch"]
            or manifest["operation_id"] != body["operation_id"]
            or manifest["manifest_digest"] != body["expected_manifest_digest"]
            or manifest["status"] != "quarantined"
            or manifest["classification"] != "unclassified"
        ):
            raise Conflict("Artifact manifest no longer matches this execution operation.")
        if manifest["late"]:
            raise Conflict("Late artifact manifests cannot be committed as execution results.")
        return grant, lease, manifest

    @staticmethod
    def _validate_manifest(
        connection: sqlite3.Connection,
        grant: sqlite3.Row,
        lease: sqlite3.Row,
        manifest: sqlite3.Row,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            manifest_entries = json.loads(manifest["artifacts_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise Conflict("Artifact manifest metadata is invalid.") from error
        if not isinstance(manifest_entries, list):
            raise Conflict("Artifact manifest metadata is invalid.")
        manifest_digest = canonical_digest(
            ArtifactManifestStore._canonical_payload(
                lease, grant, {"id": manifest["worker_principal_id"]}, manifest_entries
            )
        )
        if manifest_digest != manifest["manifest_digest"]:
            raise Conflict("Artifact manifest digest no longer matches its declared outputs.")
        expected = sorted(body["expected_artifacts"], key=lambda entry: entry["relative_path"])
        if (
            manifest_entries != expected
            or sum(item["size"] for item in expected) != body["expected_total_bytes"]
        ):
            raise Conflict("Synthetic validation does not exactly match the artifact manifest.")
        if manifest["total_bytes"] != body["expected_total_bytes"]:
            raise Conflict("Synthetic validation total does not match the artifact manifest.")
        plan, _ = ArtifactStore._plan(connection, grant)
        paths = [item["relative_path"] for item in expected]
        if len(paths) != len(set(paths)) or set(paths) != set(
            plan["execution_spec"]["output_paths"]
        ):
            raise Conflict("Artifact manifest is incomplete for the declared execution outputs.")
        verified: list[dict[str, Any]] = []
        for entry in expected:
            artifact = connection.execute(
                """SELECT id, relative_path, sha256, size, content,
                length(content) AS content_length,
                typeof(content) AS content_type, status, classification, late
                FROM artifacts WHERE id=? AND project_id=? AND task_id=? AND grant_id=?
                AND lease_id=?
                AND worker_principal_id=? AND epoch=? AND operation_id=? AND relative_path=?""",
                (
                    entry["id"],
                    grant["project_id"],
                    grant["task_id"],
                    grant["id"],
                    lease["id"],
                    lease["worker_principal_id"],
                    lease["epoch"],
                    manifest["operation_id"],
                    entry["relative_path"],
                ),
            ).fetchone()
            if artifact is None:
                raise Conflict("Artifact manifest entry is not owned by this execution operation.")
            if (
                artifact["sha256"] != entry["sha256"]
                or artifact["size"] != entry["size"]
                or artifact["content_type"] != "blob"
                or artifact["content_length"] != artifact["size"]
                or artifact["status"] != "quarantined"
                or artifact["classification"] != "unclassified"
                or artifact["late"]
                or hashlib.sha256(artifact["content"]).hexdigest() != artifact["sha256"]
            ):
                raise Conflict(
                    "Quarantined artifact integrity check failed during synthetic validation."
                )
            verified.append(entry)
        return ResultCheckpointStore._validation(manifest, verified, body["expected_total_bytes"])

    def commit(
        self,
        actor: dict[str, Any],
        grant_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = CommitExecutionResultCheckpoint.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid execution result checkpoint request.") from error
        path = f"/execution-grants/{grant_id}/result-checkpoint"

        def command(connection: sqlite3.Connection, principal: dict[str, Any]):
            self._require_human(principal)
            grant, lease, manifest = self._manifest_for_commit(
                connection, principal, grant_id, body
            )
            validation = self._validate_manifest(connection, grant, lease, manifest, body)
            validation_digest = canonical_digest(validation)
            existing = connection.execute(
                "SELECT * FROM execution_result_checkpoints WHERE manifest_id=?", (manifest["id"],)
            ).fetchone()
            if existing is not None:
                if existing["commit_digest"] != digest:
                    raise Conflict("Execution result checkpoint is immutable.")
                return 200, _response(existing)
            row = {
                "id": self._checkpoint_id(manifest["id"]),
                "project_id": grant["project_id"],
                "task_id": grant["task_id"],
                "grant_id": grant["id"],
                "lease_id": lease["id"],
                "operation_id": manifest["operation_id"],
                "manifest_id": manifest["id"],
                "manifest_digest": manifest["manifest_digest"],
                "grant_revision": grant["revision"],
                "epoch": lease["epoch"],
                "validation_contract": body["validation_contract"],
                "validation_json": json.dumps(validation, separators=(",", ":"), sort_keys=True),
                "validation_digest": validation_digest,
                "commit_digest": digest,
                "committed_at": utc_now(),
                "committed_by_principal_id": principal["id"],
                "state": "committed_quarantined",
            }
            connection.execute(
                """INSERT INTO execution_result_checkpoints
                (id, project_id, task_id, grant_id, lease_id, operation_id, manifest_id,
                manifest_digest, grant_revision, epoch, validation_contract, validation_json,
                validation_digest, commit_digest, committed_at, committed_by_principal_id, state)
                VALUES (:id,:project_id,:task_id,:grant_id,:lease_id,:operation_id,:manifest_id,
                :manifest_digest,:grant_revision,:epoch,:validation_contract,:validation_json,
                :validation_digest,:commit_digest,:committed_at,:committed_by_principal_id,:state)""",
                row,
            )
            self._event(
                connection,
                principal,
                grant["project_id"],
                grant["task_id"],
                "execution.result_checkpoint_committed",
                grant["revision"],
                {
                    "checkpoint_id": row["id"],
                    "grant_id": grant["id"],
                    "lease_id": lease["id"],
                    "operation_id": manifest["operation_id"],
                    "manifest_id": manifest["id"],
                    "manifest_digest": manifest["manifest_digest"],
                    "validation_digest": validation_digest,
                    "synthetic_validation": True,
                    "scientific_validity": "not_assessed",
                    "execution_receipt": "trusted_worker_termination_observation_only",
                },
            )
            return 201, _response(row)

        return self._mutate(actor, key, "POST", path, digest, command)

    def list_checkpoints(
        self, actor: dict[str, Any], project_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._project(connection, live, project_id)
            rows = connection.execute(
                """SELECT * FROM execution_result_checkpoints WHERE project_id=?
                ORDER BY committed_at, id LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [_response(row) for row in rows]
        finally:
            connection.close()
