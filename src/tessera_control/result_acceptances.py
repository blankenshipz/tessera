"""Bounded human bookkeeping ledger over immutable quarantined checkpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .artifacts import ArtifactStore
from .result_checkpoints import ResultCheckpointStore
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

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
OperationId = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
]


def create_result_acceptance_schema(connection: sqlite3.Connection) -> None:
    """Install append-only result bookkeeping decisions without backfilling history."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS execution_result_acceptances (
        id TEXT PRIMARY KEY,
        checkpoint_id TEXT NOT NULL REFERENCES execution_result_checkpoints(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        action TEXT NOT NULL CHECK (action IN ('accept', 'reject', 'retract')),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 8000),
        checkpoint_digest TEXT NOT NULL CHECK (length(checkpoint_digest) = 64),
        manifest_digest TEXT NOT NULL CHECK (length(manifest_digest) = 64),
        validation_digest TEXT NOT NULL CHECK (length(validation_digest) = 64),
        receipt_digest TEXT NOT NULL CHECK (length(receipt_digest) = 64),
        grant_revision INTEGER NOT NULL CHECK (grant_revision >= 1),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        operation_id TEXT NOT NULL CHECK (length(operation_id) = 32),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        scientific_validity TEXT NOT NULL CHECK (scientific_validity = 'not_assessed'),
        provider_success TEXT NOT NULL CHECK (provider_success = 'not_established'),
        artifact_release TEXT NOT NULL CHECK (artifact_release = 'not_performed'),
        evidence_label TEXT NOT NULL CHECK (evidence_label = 'development_synthetic_only'),
        receipt_evidence TEXT NOT NULL CHECK (receipt_evidence = 'fake_provider_development_only'),
        UNIQUE(checkpoint_id, revision)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS execution_result_acceptances_by_checkpoint
        ON execution_result_acceptances(checkpoint_id, revision)"""
    )


class ResultAcceptanceRequest(BaseModel):
    """One human append to a result bookkeeping ledger."""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(strict=True, ge=0, le=2**63 - 1)
    action: Literal["accept", "reject", "retract"]
    rationale: str = Field(min_length=1, max_length=8000)
    expected_checkpoint_digest: Digest
    expected_manifest_digest: Digest
    expected_validation_digest: Digest
    expected_receipt_digest: Digest
    expected_grant_revision: int = Field(strict=True, ge=1, le=2**63 - 1)
    lease_id: str = Field(min_length=1, max_length=200)
    operation_id: OperationId
    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)


def _response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    # These are intentionally fixed facts, never conclusions about the result.
    value["authoritative_scope"] = "tessera_result_bookkeeping_and_ownership_only"
    return value


class ResultAcceptanceStore(Store):
    """Append bounded human result bookkeeping without altering result evidence."""

    @staticmethod
    def _require_human(actor: dict[str, Any]) -> None:
        if actor["role"] != "human":
            raise Forbidden("Human result acceptance is required.")

    @staticmethod
    def _record_id(checkpoint_id: str, revision: int) -> str:
        suffix = hashlib.sha256(f"{checkpoint_id}:{revision}".encode()).hexdigest()[:32]
        return f"result_acceptance_{suffix}"

    def _checkpoint_for_actor(
        self, connection: sqlite3.Connection, actor: dict[str, Any], checkpoint_id: str
    ) -> sqlite3.Row:
        checkpoint = connection.execute(
            """SELECT c.*, p.org_id FROM execution_result_checkpoints c
            JOIN projects p ON p.id=c.project_id WHERE c.id=? AND p.org_id=?""",
            (checkpoint_id, actor["org_id"]),
        ).fetchone()
        if checkpoint is None or (
            actor["project_id"] is not None and actor["project_id"] != checkpoint["project_id"]
        ):
            raise NotFound("Execution result checkpoint was not found.")
        return checkpoint

    def _verify_current_evidence(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        checkpoint: sqlite3.Row,
        body: dict[str, Any],
    ) -> None:
        if checkpoint["state"] != "committed_quarantined" or checkpoint[
            "operation_id"
        ] != ArtifactStore._expected_operation(checkpoint["lease_id"]):
            raise Conflict("Result acceptance requires a committed quarantined checkpoint.")
        if (
            body["expected_checkpoint_digest"] != checkpoint["commit_digest"]
            or body["expected_manifest_digest"] != checkpoint["manifest_digest"]
            or body["expected_validation_digest"] != checkpoint["validation_digest"]
            or body["lease_id"] != checkpoint["lease_id"]
            or body["operation_id"] != checkpoint["operation_id"]
            or body["expected_epoch"] != checkpoint["epoch"]
            or body["expected_grant_revision"] != checkpoint["grant_revision"]
        ):
            raise Conflict(
                "Result acceptance digests or authority identities do not match checkpoint."
            )
        try:
            validation = json.loads(checkpoint["validation_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise Conflict("Execution result checkpoint validation is invalid.") from error
        if canonical_digest(validation) != checkpoint["validation_digest"]:
            raise Conflict("Execution result checkpoint validation digest is invalid.")
        if (
            validation.get("version") != 1
            or validation.get("contract") != checkpoint["validation_contract"]
            or validation.get("manifest_digest") != checkpoint["manifest_digest"]
            or not isinstance(validation.get("artifacts"), list)
            or not isinstance(validation.get("total_bytes"), int)
        ):
            raise Conflict("Execution result checkpoint validation is invalid.")
        grant = connection.execute(
            """SELECT * FROM execution_grants WHERE id=? AND project_id=? AND task_id=?""",
            (checkpoint["grant_id"], checkpoint["project_id"], checkpoint["task_id"]),
        ).fetchone()
        lease = connection.execute(
            "SELECT * FROM execution_leases WHERE id=? AND grant_id=?",
            (checkpoint["lease_id"], checkpoint["grant_id"]),
        ).fetchone()
        if (
            grant is None
            or lease is None
            or grant["status"] != "reconciled"
            or grant["revision"] != checkpoint["grant_revision"]
            or grant["epoch"] != checkpoint["epoch"]
            or lease["epoch"] != checkpoint["epoch"]
        ):
            raise Conflict("Result acceptance requires the current reconciled grant and epoch.")
        manifest = connection.execute(
            "SELECT * FROM artifact_manifests WHERE id=?", (checkpoint["manifest_id"],)
        ).fetchone()
        if manifest is None or (
            manifest["project_id"],
            manifest["task_id"],
            manifest["grant_id"],
            manifest["lease_id"],
            manifest["operation_id"],
            manifest["epoch"],
            manifest["manifest_digest"],
            manifest["status"],
            manifest["classification"],
            bool(manifest["late"]),
        ) != (
            checkpoint["project_id"],
            checkpoint["task_id"],
            checkpoint["grant_id"],
            checkpoint["lease_id"],
            checkpoint["operation_id"],
            checkpoint["epoch"],
            checkpoint["manifest_digest"],
            "quarantined",
            "unclassified",
            False,
        ):
            raise Conflict("Result acceptance requires a non-late quarantined manifest.")
        ResultCheckpointStore._validate_manifest(
            connection,
            grant,
            lease,
            manifest,
            {
                "expected_artifacts": validation["artifacts"],
                "expected_total_bytes": validation["total_bytes"],
            },
        )
        receipt = connection.execute(
            """SELECT o.org_id, o.project_id, o.task_id, o.grant_id, o.lease_id,
            o.worker_principal_id, o.epoch, o.operation_id, o.observed_state,
            r.receipt_payload_json, r.receipt_digest, r.observed_state,
            r.termination_confirmed
            FROM external_operations o JOIN external_operation_receipts r
            ON r.operation_id=o.operation_id WHERE o.operation_id=?""",
            (checkpoint["operation_id"],),
        ).fetchone()
        if receipt is None:
            raise Conflict("Result acceptance receipt does not match current reconciled evidence.")
        try:
            receipt_payload = json.loads(receipt["receipt_payload_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise Conflict("Result acceptance receipt is invalid.") from error
        if canonical_digest(receipt_payload) != receipt["receipt_digest"]:
            raise Conflict("Result acceptance receipt digest is invalid.")
        if (
            receipt["org_id"],
            receipt["project_id"],
            receipt["task_id"],
            receipt["grant_id"],
            receipt["lease_id"],
            receipt["worker_principal_id"],
            receipt["epoch"],
            receipt["operation_id"],
            receipt["observed_state"],
            receipt["receipt_digest"],
            receipt["observed_state"],
            bool(receipt["termination_confirmed"]),
        ) != (
            actor["org_id"],
            checkpoint["project_id"],
            checkpoint["task_id"],
            checkpoint["grant_id"],
            checkpoint["lease_id"],
            manifest["worker_principal_id"],
            checkpoint["epoch"],
            checkpoint["operation_id"],
            "exited",
            body["expected_receipt_digest"],
            "exited",
            True,
        ):
            raise Conflict("Result acceptance receipt does not match current reconciled evidence.")

    def record(
        self,
        actor: dict[str, Any],
        checkpoint_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = ResultAcceptanceRequest.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid result acceptance request.") from error
        if not body["rationale"].strip():
            raise StoreError("Result acceptance rationale must not be blank.")
        path = f"/result-checkpoints/{checkpoint_id}/acceptances"

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._require_human(live)
            checkpoint = self._checkpoint_for_actor(connection, live, checkpoint_id)
            self._verify_current_evidence(connection, live, checkpoint, body)
            current = connection.execute(
                """SELECT revision, action FROM execution_result_acceptances
                WHERE checkpoint_id=? ORDER BY revision DESC LIMIT 1""",
                (checkpoint_id,),
            ).fetchone()
            current_revision = current["revision"] if current is not None else 0
            if body["expected_revision"] != current_revision:
                raise Conflict("Result acceptance revision is no longer current.")
            if current is None and body["action"] == "retract":
                raise Conflict(
                    "A result acceptance requires an earlier decision before retraction."
                )
            if current is not None and (
                body["action"] != "retract" or current["action"] == "retract"
            ):
                raise Conflict("Result acceptance already has a conflicting terminal state.")
            revision = current_revision + 1
            row = {
                "id": self._record_id(checkpoint_id, revision),
                "checkpoint_id": checkpoint_id,
                "revision": revision,
                "action": body["action"],
                "rationale": body["rationale"],
                "checkpoint_digest": checkpoint["commit_digest"],
                "manifest_digest": checkpoint["manifest_digest"],
                "validation_digest": checkpoint["validation_digest"],
                "receipt_digest": body["expected_receipt_digest"],
                "grant_revision": checkpoint["grant_revision"],
                "lease_id": checkpoint["lease_id"],
                "operation_id": checkpoint["operation_id"],
                "epoch": checkpoint["epoch"],
                "actor_principal_id": live["id"],
                "created_at": utc_now(),
                "scientific_validity": "not_assessed",
                "provider_success": "not_established",
                "artifact_release": "not_performed",
                "evidence_label": "development_synthetic_only",
                "receipt_evidence": "fake_provider_development_only",
            }
            connection.execute(
                """INSERT INTO execution_result_acceptances
                (id, checkpoint_id, revision, action, rationale, checkpoint_digest, manifest_digest,
                validation_digest, receipt_digest, grant_revision, lease_id, operation_id, epoch,
                actor_principal_id, created_at, scientific_validity, provider_success,
                artifact_release,
                evidence_label, receipt_evidence)
                VALUES (:id,:checkpoint_id,:revision,:action,:rationale,:checkpoint_digest,
                :manifest_digest,:validation_digest,:receipt_digest,:grant_revision,:lease_id,
                :operation_id,:epoch,:actor_principal_id,:created_at,:scientific_validity,
                :provider_success,:artifact_release,:evidence_label,:receipt_evidence)""",
                row,
            )
            self._event(
                connection,
                live,
                checkpoint["project_id"],
                checkpoint["task_id"],
                "human.result_acceptance_recorded",
                revision,
                {
                    key: row[key]
                    for key in (
                        "checkpoint_id",
                        "revision",
                        "action",
                        "checkpoint_digest",
                        "manifest_digest",
                        "validation_digest",
                        "receipt_digest",
                        "scientific_validity",
                        "provider_success",
                        "artifact_release",
                        "evidence_label",
                        "receipt_evidence",
                    )
                },
            )
            return 201, _response(row)

        def replay_guard(connection: sqlite3.Connection, live: dict[str, Any], _: Any) -> None:
            self._require_human(live)
            self._checkpoint_for_actor(connection, live, checkpoint_id)

        return self._mutate(actor, key, "POST", path, digest, command, replay_guard=replay_guard)

    def list_records(
        self, actor: dict[str, Any], checkpoint_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._checkpoint_for_actor(connection, live, checkpoint_id)
            rows = connection.execute(
                """SELECT * FROM execution_result_acceptances WHERE checkpoint_id=?
                ORDER BY revision LIMIT ? OFFSET ?""",
                (checkpoint_id, limit, offset),
            ).fetchall()
            return [_response(row) for row in rows]
        finally:
            connection.close()
