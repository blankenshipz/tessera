"""Metadata-only, reviewable dataset provenance for MixFM Outcome 0/1.

This service records caller-supplied snapshot claims.  It never fetches bytes,
contacts a source, checks a license, or validates a dataset's contents.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from pydantic import ValidationError

from .dataset_ingestion_models import (
    CreateIngestionProposal,
    RegisterDatasetSnapshot,
    ReviewIngestionProposal,
)
from .research import ResearchStore
from .store import Conflict, NotFound, StoreError, canonical_digest, utc_now

DATASET_RECORD_LIMIT = 1_000


def create_dataset_ingestion_schema(connection: sqlite3.Connection) -> None:
    """Install empty metadata/proposal records without inferring dataset history."""
    statements = (
        """CREATE TABLE IF NOT EXISTS dataset_snapshots (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            name TEXT NOT NULL, snapshot_digest TEXT NOT NULL CHECK (
                length(snapshot_digest)=64 AND snapshot_digest NOT GLOB '*[^0-9a-f]*'
            ), schema_json TEXT NOT NULL, schema_digest TEXT NOT NULL CHECK (
                length(schema_digest)=64 AND schema_digest NOT GLOB '*[^0-9a-f]*'
            ), record_count INTEGER NOT NULL CHECK (record_count >= 0),
            validation_summary_json TEXT NOT NULL, split_strategy_json TEXT NOT NULL,
            revision INTEGER NOT NULL, created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            UNIQUE(study_id, snapshot_digest)
        )""",
        "CREATE INDEX IF NOT EXISTS dataset_snapshots_study "
        "ON dataset_snapshots(study_id,created_at,id)",
        """CREATE TABLE IF NOT EXISTS dataset_snapshot_sources (
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(id),
            source_id TEXT NOT NULL REFERENCES research_sources(id),
            expected_source_revision INTEGER NOT NULL CHECK (expected_source_revision >= 1),
            expected_content_digest TEXT NOT NULL CHECK (
                length(expected_content_digest)=64
                AND expected_content_digest NOT GLOB '*[^0-9a-f]*'
            ), license_expression TEXT NOT NULL,
            license_status TEXT NOT NULL CHECK (license_status IN ('declared','unsupported')),
            provenance_kind TEXT NOT NULL,
            provenance_status TEXT NOT NULL CHECK (provenance_status IN ('declared','unsupported')),
            PRIMARY KEY(snapshot_id,source_id)
        )""",
        """CREATE TABLE IF NOT EXISTS dataset_ingestion_proposals (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(id),
            intended_use TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected')),
            revision INTEGER NOT NULL, rationale TEXT, reviewed_at TEXT,
            created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            reviewed_by_principal_id TEXT REFERENCES principals(id)
        )""",
        "CREATE INDEX IF NOT EXISTS dataset_ingestion_proposals_study "
        "ON dataset_ingestion_proposals(study_id,created_at,id)",
    )
    for statement in statements:
        connection.execute(statement)


class DatasetIngestionStore(ResearchStore):
    """Tenant-scoped snapshot declarations and human-reviewed ingestion proposals."""

    @staticmethod
    def _register_payload(
        payload: dict[str, Any] | RegisterDatasetSnapshot,
    ) -> dict[str, Any]:
        try:
            value = (
                payload
                if isinstance(payload, RegisterDatasetSnapshot)
                else RegisterDatasetSnapshot.model_validate(payload)
            )
        except ValidationError as error:
            raise StoreError("Invalid research request.") from error
        return value.model_dump(mode="json", by_alias=True)

    @staticmethod
    def _snapshot(
        connection: sqlite3.Connection, actor: dict[str, Any], snapshot_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT d.* FROM dataset_snapshots d JOIN research_studies s ON s.id=d.study_id
            JOIN projects p ON p.id=s.project_id WHERE d.id=? AND p.org_id=?
            AND (? IS NULL OR s.project_id=?)""",
            (snapshot_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Dataset snapshot was not found.")
        return dict(row)

    @staticmethod
    def _proposal(
        connection: sqlite3.Connection, actor: dict[str, Any], proposal_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT i.* FROM dataset_ingestion_proposals i
            JOIN research_studies s ON s.id=i.study_id JOIN projects p ON p.id=s.project_id
            WHERE i.id=? AND p.org_id=? AND (? IS NULL OR s.project_id=?)""",
            (proposal_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Dataset ingestion proposal was not found.")
        return dict(row)

    @staticmethod
    def _snapshot_response(connection: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["schema"] = json.loads(result.pop("schema_json"))
        result["validation_summary"] = json.loads(result.pop("validation_summary_json"))
        result["split_strategy"] = json.loads(result.pop("split_strategy_json"))
        result["sources"] = [
            dict(item)
            for item in connection.execute(
                "SELECT source_id,expected_source_revision,expected_content_digest,"
                "license_expression,"
                "license_status,provenance_kind,provenance_status FROM dataset_snapshot_sources "
                "WHERE snapshot_id=? ORDER BY source_id",
                (result["id"],),
            )
        ]
        result["content_validation"] = "not_performed"
        return result

    @staticmethod
    def _proposal_response(row: dict[str, Any]) -> dict[str, Any]:
        return dict(row)

    def register_dataset_snapshot(
        self,
        principal: dict[str, Any],
        study_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | RegisterDatasetSnapshot,
    ) -> tuple[int, dict[str, Any]]:
        body = self._register_payload(payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            study = self._study(connection, actor, study_id)
            if study["status"] != "open":
                raise Conflict("Closed studies cannot register dataset snapshots.")
            for source_record in body["sources"]:
                source = self._source(connection, actor, source_record["source_id"])
                if source["study_id"] != study_id:
                    raise NotFound("Dataset source was not found.")
                if (
                    source["revision"] != source_record["expected_source_revision"]
                    or source["content_digest"] != source_record["expected_content_digest"]
                ):
                    raise Conflict("Dataset source digest or revision no longer matches.")
            schema_json = json.dumps(body["schema"], sort_keys=True, separators=(",", ":"))
            now, snapshot_id = utc_now(), "dataset_" + uuid.uuid4().hex
            try:
                connection.execute(
                    """INSERT INTO dataset_snapshots
                    (id,study_id,name,snapshot_digest,schema_json,schema_digest,record_count,
                     validation_summary_json,split_strategy_json,revision,created_at,created_by_principal_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        snapshot_id,
                        study_id,
                        body["name"],
                        body["snapshot_digest"],
                        schema_json,
                        canonical_digest(body["schema"]),
                        body["record_count"],
                        json.dumps(
                            body["validation_summary"], sort_keys=True, separators=(",", ":")
                        ),
                        json.dumps(body["split_strategy"], sort_keys=True, separators=(",", ":")),
                        1,
                        now,
                        actor["id"],
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise Conflict(
                    "Dataset snapshot digest is already registered for this study."
                ) from error
            connection.executemany(
                """INSERT INTO dataset_snapshot_sources
                (snapshot_id,source_id,expected_source_revision,expected_content_digest,license_expression,
                 license_status,provenance_kind,provenance_status) VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        snapshot_id,
                        source["source_id"],
                        source["expected_source_revision"],
                        source["expected_content_digest"],
                        source["license_expression"],
                        source["license_status"],
                        source["provenance_kind"],
                        source["provenance_status"],
                    )
                    for source in body["sources"]
                ],
            )
            result = self._snapshot_response(
                connection,
                {
                    "id": snapshot_id,
                    "study_id": study_id,
                    "name": body["name"],
                    "snapshot_digest": body["snapshot_digest"],
                    "schema_json": schema_json,
                    "schema_digest": canonical_digest(body["schema"]),
                    "record_count": body["record_count"],
                    "validation_summary_json": json.dumps(
                        body["validation_summary"], sort_keys=True, separators=(",", ":")
                    ),
                    "split_strategy_json": json.dumps(
                        body["split_strategy"], sort_keys=True, separators=(",", ":")
                    ),
                    "revision": 1,
                    "created_at": now,
                    "created_by_principal_id": actor["id"],
                },
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.dataset_snapshot_registered",
                1,
                {
                    "study_id": study_id,
                    "snapshot_id": snapshot_id,
                    "content_validation": "not_performed",
                },
            )
            return 201, result

        return self._mutate(
            principal, key, "POST", f"/studies/{study_id}/dataset-snapshots", digest, command
        )

    def list_dataset_snapshots(
        self, principal: dict[str, Any], study_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise StoreError("Invalid dataset snapshot pagination.")
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            actor = self._live_principal(connection, principal)
            self._study(connection, actor, study_id)
            return [
                self._snapshot_response(connection, dict(row))
                for row in connection.execute(
                    "SELECT * FROM dataset_snapshots WHERE study_id=? "
                    "ORDER BY created_at,id LIMIT ? OFFSET ?",
                    (study_id, limit, offset),
                )
            ]
        finally:
            connection.close()

    def create_ingestion_proposal(
        self,
        principal: dict[str, Any],
        study_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateIngestionProposal,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(CreateIngestionProposal, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            study = self._study(connection, actor, study_id)
            snapshot = self._snapshot(connection, actor, body["snapshot_id"])
            if study["status"] != "open" or snapshot["study_id"] != study_id:
                raise Conflict("Dataset ingestion proposal requires an open study snapshot.")
            if snapshot["revision"] != body["expected_snapshot_revision"]:
                raise Conflict("Dataset snapshot revision is no longer current.")
            now, proposal_id = utc_now(), "dataset_ingestion_" + uuid.uuid4().hex
            result = {
                "id": proposal_id,
                "study_id": study_id,
                "snapshot_id": snapshot["id"],
                "intended_use": body["intended_use"],
                "status": "proposed",
                "revision": 1,
                "rationale": None,
                "reviewed_at": None,
                "created_at": now,
                "created_by_principal_id": actor["id"],
                "reviewed_by_principal_id": None,
            }
            connection.execute(
                """INSERT INTO dataset_ingestion_proposals
                (id,study_id,snapshot_id,intended_use,status,revision,rationale,reviewed_at,created_at,
                 created_by_principal_id,reviewed_by_principal_id)
                VALUES (:id,:study_id,:snapshot_id,:intended_use,:status,:revision,:rationale,
                        :reviewed_at,
                        :created_at,:created_by_principal_id,:reviewed_by_principal_id)""",
                result,
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.dataset_ingestion_proposed",
                1,
                {"study_id": study_id, "proposal_id": proposal_id, "snapshot_id": snapshot["id"]},
            )
            return 201, result

        return self._mutate(
            principal,
            key,
            "POST",
            f"/studies/{study_id}/dataset-ingestion-proposals",
            digest,
            command,
        )

    def review_ingestion_proposal(
        self,
        principal: dict[str, Any],
        proposal_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | ReviewIngestionProposal,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(ReviewIngestionProposal, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "review a dataset ingestion proposal")
            proposal = self._proposal(connection, actor, proposal_id)
            if (
                proposal["status"] != "proposed"
                or proposal["revision"] != body["expected_revision"]
            ):
                raise Conflict("Dataset ingestion proposal is no longer reviewable.")
            snapshot = self._snapshot(connection, actor, proposal["snapshot_id"])
            study = self._study(connection, actor, proposal["study_id"])
            if body["decision"] == "approve":
                self._require_supported_snapshot_sources(connection, actor, snapshot["id"])
            now, revision = utc_now(), proposal["revision"] + 1
            status = {"approve": "approved", "reject": "rejected"}[body["decision"]]
            connection.execute(
                """UPDATE dataset_ingestion_proposals
                SET status=?,revision=?,rationale=?,reviewed_at=?,
                reviewed_by_principal_id=? WHERE id=? AND revision=? AND status='proposed'""",
                (
                    status,
                    revision,
                    body["rationale"],
                    now,
                    actor["id"],
                    proposal_id,
                    body["expected_revision"],
                ),
            )
            result = {
                **proposal,
                "status": status,
                "revision": revision,
                "rationale": body["rationale"],
                "reviewed_at": now,
                "reviewed_by_principal_id": actor["id"],
            }
            self._event_for(
                connection,
                actor,
                study,
                "research.dataset_ingestion_reviewed",
                revision,
                {
                    "study_id": study["id"],
                    "proposal_id": proposal_id,
                    "snapshot_id": snapshot["id"],
                    "decision": body["decision"],
                },
            )
            return 200, result

        return self._mutate(
            principal,
            key,
            "POST",
            f"/dataset-ingestion-proposals/{proposal_id}/review",
            digest,
            command,
        )

    def _require_supported_snapshot_sources(
        self, connection: sqlite3.Connection, actor: dict[str, Any], snapshot_id: str
    ) -> None:
        sources = connection.execute(
            """SELECT r.*,s.study_id,so.revision,so.content_digest,so.verdict,so.superseded_at
            FROM dataset_snapshot_sources r JOIN dataset_snapshots s ON s.id=r.snapshot_id
            JOIN research_sources so ON so.id=r.source_id WHERE r.snapshot_id=?""",
            (snapshot_id,),
        ).fetchall()
        if not sources:
            raise Conflict("Dataset snapshot has no provenance records.")
        for source in sources:
            # Keep the tenant-scoped lookup in the review path, not only at registration.
            current = self._source(connection, actor, source["source_id"])
            if (
                current["study_id"] != source["study_id"]
                or current["revision"] != source["expected_source_revision"]
                or current["content_digest"] != source["expected_content_digest"]
                or current["verdict"] != "supported"
                or current["superseded_at"] is not None
            ):
                raise Conflict("Dataset source provenance is unsupported or stale.")
            if source["license_status"] != "declared" or source["provenance_status"] != "declared":
                raise Conflict("Dataset license or provenance is unsupported.")
