"""Transactional, tenant-scoped research ledger records.

This is deliberately an evidence/accounting service.  It does not fetch sources,
execute work, infer scientific truth, or manufacture execution receipts.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .research_models import (
    AcceptFinding,
    AssessSource,
    CloseStudy,
    CorrectSource,
    CreateFinding,
    CreateSource,
    CreateStudy,
    ExportStudy,
    ReaffirmFinding,
    RecordVariantStatus,
    RegisterVariant,
    RetractFinding,
    ReviseFinding,
)
from .store import Conflict, Forbidden, NotFound, Store, StoreError, utc_now

T = TypeVar("T", bound=BaseModel)
EXPORT_RECORD_LIMIT = 1_000


def create_research_schema(connection: sqlite3.Connection) -> None:
    """Install the standalone ledger schema during the pre-migration slice.

    Each statement is intentionally executed independently: callers may run this
    inside their own migration transaction without `executescript` transaction
    behavior changing the enclosing boundary.
    """

    statements = (
        """CREATE TABLE IF NOT EXISTS research_studies (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            purpose TEXT NOT NULL, protocol TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('exploratory','confirmatory')),
            status TEXT NOT NULL CHECK (status IN ('open','closed')), revision INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id))""",
        "CREATE INDEX IF NOT EXISTS research_studies_project "
        "ON research_studies(project_id, created_at, id)",
        """CREATE TABLE IF NOT EXISTS research_variants (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            name TEXT NOT NULL, method_digest TEXT NOT NULL, input_digest TEXT NOT NULL,
            metadata_json TEXT NOT NULL, replication_label TEXT NOT NULL CHECK
            (replication_label IN ('rerun','same_pipeline','unavailable')),
            status TEXT NOT NULL CHECK
            (status IN ('registered','completed','failed','excluded','abandoned')),
            reason TEXT, outcome_json TEXT, outcome_provenance TEXT,
            revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            CHECK ((status NOT IN ('excluded','abandoned')) OR reason IS NOT NULL))""",
        "CREATE INDEX IF NOT EXISTS research_variants_study "
        "ON research_variants(study_id, created_at, id)",
        """CREATE TABLE IF NOT EXISTS research_sources (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            uri TEXT NOT NULL, quoted_passage TEXT NOT NULL, content_digest TEXT NOT NULL,
            locator TEXT, verdict TEXT NOT NULL CHECK
            (verdict IN ('unverified','supported','unsupported')),
            assessment_reason TEXT, revision INTEGER NOT NULL,
            supersedes_source_id TEXT REFERENCES research_sources(id),
            superseded_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            assessed_by_principal_id TEXT REFERENCES principals(id))""",
        "CREATE INDEX IF NOT EXISTS research_sources_study "
        "ON research_sources(study_id, created_at, id)",
        "CREATE INDEX IF NOT EXISTS research_sources_supersedes "
        "ON research_sources(supersedes_source_id)",
        """CREATE TABLE IF NOT EXISTS research_findings (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            statement TEXT NOT NULL, status TEXT NOT NULL CHECK
            (status IN ('candidate','accepted','needs_review','retracted','superseded')),
            revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            accepted_by_principal_id TEXT REFERENCES principals(id),
            supersedes_finding_id TEXT UNIQUE REFERENCES research_findings(id),
            revision_kind TEXT CHECK (revision_kind IN ('narrow','supersede')),
            revision_reason TEXT,
            CHECK ((supersedes_finding_id IS NULL AND revision_kind IS NULL
                    AND revision_reason IS NULL)
                OR (supersedes_finding_id IS NOT NULL AND revision_kind IS NOT NULL
                    AND revision_reason IS NOT NULL)),
            CHECK (supersedes_finding_id IS NULL OR supersedes_finding_id != id))""",
        "CREATE INDEX IF NOT EXISTS research_findings_study "
        "ON research_findings(study_id, created_at, id)",
        """CREATE TABLE IF NOT EXISTS research_finding_sources (
            finding_id TEXT NOT NULL REFERENCES research_findings(id),
            source_id TEXT NOT NULL REFERENCES research_sources(id),
            PRIMARY KEY(finding_id, source_id))""",
        """CREATE TABLE IF NOT EXISTS research_finding_variants (
            finding_id TEXT NOT NULL REFERENCES research_findings(id),
            variant_id TEXT NOT NULL REFERENCES research_variants(id),
            PRIMARY KEY(finding_id, variant_id))""",
        """CREATE TABLE IF NOT EXISTS research_corrections (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            original_source_id TEXT NOT NULL REFERENCES research_sources(id),
            replacement_source_id TEXT NOT NULL REFERENCES research_sources(id),
            reason TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
            created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS research_corrections_study "
        "ON research_corrections(study_id, created_at, id)",
    )
    for statement in statements:
        connection.execute(statement)


class ResearchStore(Store):
    """Typed ledger operations layered on the control store transaction boundary."""

    @staticmethod
    def _payload(model: type[T], payload: dict[str, Any] | T) -> dict[str, Any]:
        try:
            value = payload if isinstance(payload, model) else model.model_validate(payload)
        except ValidationError as error:
            raise StoreError("Invalid research request.") from error
        return value.model_dump(mode="json")

    @staticmethod
    def _human(actor: dict[str, Any], action: str) -> None:
        if actor["role"] != "human":
            raise Forbidden(f"Human review is required to {action}.")

    @staticmethod
    def _study(
        connection: sqlite3.Connection, actor: dict[str, Any], study_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT s.* FROM research_studies s JOIN projects p ON p.id = s.project_id
            WHERE s.id = ? AND p.org_id = ?""",
            (study_id, actor["org_id"]),
        ).fetchone()
        if row is None or (
            actor["project_id"] is not None and actor["project_id"] != row["project_id"]
        ):
            raise NotFound("Study was not found.")
        return dict(row)

    @staticmethod
    def _variant(
        connection: sqlite3.Connection, actor: dict[str, Any], variant_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT v.* FROM research_variants v JOIN research_studies s ON s.id = v.study_id
            JOIN projects p ON p.id = s.project_id WHERE v.id = ? AND p.org_id = ?
            AND (? IS NULL OR s.project_id = ?)""",
            (variant_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Variant was not found.")
        return dict(row)

    @staticmethod
    def _source(
        connection: sqlite3.Connection, actor: dict[str, Any], source_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT so.* FROM research_sources so JOIN research_studies s ON s.id = so.study_id
            JOIN projects p ON p.id = s.project_id WHERE so.id = ? AND p.org_id = ?
            AND (? IS NULL OR s.project_id = ?)""",
            (source_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Source was not found.")
        return dict(row)

    @staticmethod
    def _finding(
        connection: sqlite3.Connection, actor: dict[str, Any], finding_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT f.* FROM research_findings f JOIN research_studies s ON s.id = f.study_id
            JOIN projects p ON p.id = s.project_id WHERE f.id = ? AND p.org_id = ?
            AND (? IS NULL OR s.project_id = ?)""",
            (finding_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Finding was not found.")
        return dict(row)

    @staticmethod
    def _event_for(
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        study: dict[str, Any],
        name: str,
        revision: int,
        payload: dict[str, Any],
    ) -> None:
        Store._event(connection, actor, study["project_id"], None, name, revision, payload)

    def create_study(
        self,
        principal: dict[str, Any],
        project_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateStudy,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(CreateStudy, payload)
        path = f"/projects/{project_id}/studies"

        return self._mutate(
            principal,
            key,
            "POST",
            path,
            digest,
            lambda c, a: self._create_study_transaction(c, a, project_id, body),
        )

    def _create_study_transaction(self, connection, actor, project_id, body):
        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._project(connection, actor, project_id)
            now, study_id = utc_now(), "study_" + uuid.uuid4().hex
            study = {
                "id": study_id,
                "project_id": project_id,
                **body,
                "status": "open",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "closed_at": None,
                "created_by_principal_id": actor["id"],
            }
            connection.execute(
                """INSERT INTO research_studies
                (id,project_id,purpose,protocol,mode,status,revision,created_at,updated_at,
                 closed_at,created_by_principal_id)
                VALUES
                (:id,:project_id,:purpose,:protocol,:mode,:status,:revision,:created_at,
                 :updated_at,:closed_at,:created_by_principal_id)""",
                study,
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.study_created",
                1,
                {"study_id": study_id, "mode": body["mode"]},
            )
            return 201, study

        return command(connection, actor)

    def list_studies(
        self, principal: dict[str, Any], project_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise StoreError("Invalid study pagination.")
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            actor = self._live_principal(connection, principal)
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT * FROM research_studies WHERE project_id = ?
                ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_study(self, principal: dict[str, Any], study_id: str) -> dict[str, Any]:
        """Return the bounded full ledger detail view."""
        return self.export_study(principal, study_id)

    def list_variants(
        self, principal: dict[str, Any], study_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._list_study_records(principal, study_id, "research_variants", offset, limit)

    def list_sources(
        self, principal: dict[str, Any], study_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._list_study_records(principal, study_id, "research_sources", offset, limit)

    def list_findings(
        self, principal: dict[str, Any], study_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._list_study_records(principal, study_id, "research_findings", offset, limit)

    def _list_study_records(
        self,
        principal: dict[str, Any],
        study_id: str,
        table: str,
        offset: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise StoreError("Invalid study pagination.")
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            actor = self._live_principal(connection, principal)
            self._study(connection, actor, study_id)
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE study_id = ? "
                "ORDER BY created_at, id LIMIT ? OFFSET ?",
                (study_id, limit, offset),
            ).fetchall()
            if table == "research_variants":
                return [self._variant_response(dict(row)) for row in rows]
            if table == "research_findings":
                return [self._finding_response(connection, dict(row)) for row in rows]
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def register_variant(
        self,
        principal: dict[str, Any],
        study_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | RegisterVariant,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(RegisterVariant, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            study = self._study(connection, actor, study_id)
            if study["status"] != "open":
                raise Conflict("Closed studies cannot register variants.")
            now, variant_id = utc_now(), "variant_" + uuid.uuid4().hex
            result = {
                "id": variant_id,
                "study_id": study_id,
                "name": body["name"],
                "method_digest": body["method_digest"],
                "input_digest": body["input_digest"],
                "metadata": body["metadata"],
                "replication_label": body["replication_label"],
                "independence_status": "unavailable",
                "status": "registered",
                "reason": None,
                "outcome": None,
                "outcome_provenance": None,
                "execution_receipt": None,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "created_by_principal_id": actor["id"],
            }
            connection.execute(
                """INSERT INTO research_variants
                (id,study_id,name,method_digest,input_digest,metadata_json,replication_label,
                 status,reason,outcome_json,outcome_provenance,revision,created_at,updated_at,
                 created_by_principal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    variant_id,
                    study_id,
                    body["name"],
                    body["method_digest"],
                    body["input_digest"],
                    json.dumps(body["metadata"], sort_keys=True, separators=(",", ":")),
                    body["replication_label"],
                    "registered",
                    None,
                    None,
                    None,
                    1,
                    now,
                    now,
                    actor["id"],
                ),
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.variant_registered",
                1,
                {
                    "study_id": study_id,
                    "variant_id": variant_id,
                    "independence_status": "unavailable",
                },
            )
            return 201, result

        return self._mutate(
            principal, key, "POST", f"/studies/{study_id}/variants", digest, command
        )

    def record_variant_status(
        self,
        principal: dict[str, Any],
        variant_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | RecordVariantStatus,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(RecordVariantStatus, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            variant = self._variant(connection, actor, variant_id)
            if body["status"] in {"excluded", "abandoned"}:
                self._human(actor, body["status"] + " a variant")
                if not body["reason"]:
                    raise StoreError("Exclusion and abandonment require a reason.")
            if variant["revision"] != body["expected_revision"]:
                raise Conflict("Variant revision is no longer current.")
            if variant["status"] != "registered":
                raise Conflict("Variant already has a terminal status.")
            study = self._study(connection, actor, variant["study_id"])
            now, revision = utc_now(), variant["revision"] + 1
            connection.execute(
                """UPDATE research_variants
                SET status=?,reason=?,outcome_json=?,outcome_provenance=?,revision=?,updated_at=?
                WHERE id=? AND revision=? AND status='registered'""",
                (
                    body["status"],
                    body["reason"],
                    json.dumps(body["outcome"], sort_keys=True, separators=(",", ":"))
                    if body["outcome"] is not None
                    else None,
                    body["provenance"] if body["outcome"] is not None else None,
                    revision,
                    now,
                    variant_id,
                    body["expected_revision"],
                ),
            )
            # A registered variant can be cited before its result is known. Once its
            # outcome is recorded, a previously accepted interpretation needs a
            # human recheck; the historical finding and its citations stay intact.
            flagged_findings = connection.execute(
                """UPDATE research_findings SET status='needs_review',revision=revision+1,
                updated_at=? WHERE id IN (
                    SELECT finding_id FROM research_finding_variants WHERE variant_id=?
                ) AND status='accepted'""",
                (now, variant_id),
            ).rowcount
            result = self._variant_response(
                {
                    **variant,
                    "status": body["status"],
                    "reason": body["reason"],
                    "outcome_json": json.dumps(
                        body["outcome"], sort_keys=True, separators=(",", ":")
                    )
                    if body["outcome"] is not None
                    else None,
                    "outcome_provenance": body["provenance"]
                    if body["outcome"] is not None
                    else None,
                    "revision": revision,
                    "updated_at": now,
                }
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.variant_recorded",
                revision,
                {
                    "study_id": study["id"],
                    "variant_id": variant_id,
                    "status": body["status"],
                    "provenance": result["outcome_provenance"],
                    "execution_receipt": None,
                    "dependent_findings_flagged": flagged_findings,
                },
            )
            return 200, result

        return self._mutate(
            principal, key, "POST", f"/variants/{variant_id}/status", digest, command
        )

    record_variant = record_variant_status

    def create_source(
        self,
        principal: dict[str, Any],
        study_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateSource,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(CreateSource, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            study = self._study(connection, actor, study_id)
            now, source_id = utc_now(), "source_" + uuid.uuid4().hex
            result = {
                "id": source_id,
                "study_id": study_id,
                **body,
                "verdict": "unverified",
                "assessment_reason": None,
                "revision": 1,
                "supersedes_source_id": None,
                "superseded_at": None,
                "created_at": now,
                "updated_at": now,
                "created_by_principal_id": actor["id"],
                "assessed_by_principal_id": None,
            }
            connection.execute(
                """INSERT INTO research_sources
                (id,study_id,uri,quoted_passage,content_digest,locator,verdict,
                 assessment_reason,revision,supersedes_source_id,superseded_at,created_at,
                 updated_at,created_by_principal_id,assessed_by_principal_id)
                VALUES
                (:id,:study_id,:uri,:quoted_passage,:content_digest,:locator,:verdict,
                 :assessment_reason,:revision,:supersedes_source_id,:superseded_at,:created_at,
                 :updated_at,:created_by_principal_id,:assessed_by_principal_id)""",
                result,
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.source_recorded",
                1,
                {"study_id": study_id, "source_id": source_id, "verdict": "unverified"},
            )
            return 201, result

        return self._mutate(principal, key, "POST", f"/studies/{study_id}/sources", digest, command)

    # A friendly alias for callers using evidence terminology.
    add_source = create_source

    def assess_source(
        self,
        principal: dict[str, Any],
        source_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | AssessSource,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(AssessSource, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "assess a source")
            source = self._source(connection, actor, source_id)
            if source["revision"] != body["expected_revision"]:
                raise Conflict("Source revision is no longer current.")
            if source["superseded_at"] is not None:
                raise Conflict("Superseded sources cannot be assessed.")
            study = self._study(connection, actor, source["study_id"])
            now, revision = utc_now(), source["revision"] + 1
            connection.execute(
                "UPDATE research_sources SET verdict=?,assessment_reason=?,revision=?,"
                "updated_at=?,assessed_by_principal_id=? WHERE id=? AND revision=?",
                (
                    body["verdict"],
                    body["reason"],
                    revision,
                    now,
                    actor["id"],
                    source_id,
                    body["expected_revision"],
                ),
            )
            flagged_findings = 0
            if source["verdict"] == "supported" and body["verdict"] != "supported":
                flagged_findings = connection.execute(
                    """UPDATE research_findings SET status='needs_review', revision=revision+1,
                    updated_at=? WHERE id IN (
                        SELECT finding_id FROM research_finding_sources WHERE source_id=?
                    ) AND status='accepted'""",
                    (now, source_id),
                ).rowcount
            result = {
                **source,
                "verdict": body["verdict"],
                "assessment_reason": body["reason"],
                "revision": revision,
                "updated_at": now,
                "assessed_by_principal_id": actor["id"],
            }
            self._event_for(
                connection,
                actor,
                study,
                "research.source_assessed",
                revision,
                {
                    "study_id": study["id"],
                    "source_id": source_id,
                    "verdict": body["verdict"],
                    "reason": body["reason"],
                    "dependent_findings_flagged": flagged_findings,
                },
            )
            return 200, result

        return self._mutate(
            principal, key, "POST", f"/sources/{source_id}/assessment", digest, command
        )

    def create_finding(
        self,
        principal: dict[str, Any],
        study_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateFinding,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(CreateFinding, payload)
        self._validate_finding_reference_ids(body)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            study = self._study(connection, actor, study_id)
            if study["status"] != "open":
                raise Conflict("Closed studies cannot add findings.")
            self._validate_finding_references(connection, actor, study_id, body)
            result = self._insert_finding(connection, actor, study_id, body)
            self._event_for(
                connection,
                actor,
                study,
                "research.finding_created",
                1,
                {"study_id": study_id, "finding_id": result["id"], "status": "candidate"},
            )
            return 201, result

        return self._mutate(
            principal, key, "POST", f"/studies/{study_id}/findings", digest, command
        )

    @staticmethod
    def _validate_finding_reference_ids(body: dict[str, Any]) -> None:
        if len(set(body["source_ids"])) != len(body["source_ids"]) or len(
            set(body["variant_ids"])
        ) != len(body["variant_ids"]):
            raise StoreError("Finding references must be unique.")

    def _validate_finding_references(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        study_id: str,
        body: dict[str, Any],
    ) -> None:
        for source_id in body["source_ids"]:
            if self._source(connection, actor, source_id)["study_id"] != study_id:
                raise NotFound("Source was not found.")
        for variant_id in body["variant_ids"]:
            if self._variant(connection, actor, variant_id)["study_id"] != study_id:
                raise NotFound("Variant was not found.")

    @staticmethod
    def _insert_finding(
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        study_id: str,
        body: dict[str, Any],
        *,
        supersedes_finding_id: str | None = None,
        revision_kind: str | None = None,
        revision_reason: str | None = None,
    ) -> dict[str, Any]:
        now, finding_id = utc_now(), "finding_" + uuid.uuid4().hex
        result = {
            "id": finding_id,
            "study_id": study_id,
            "statement": body["statement"],
            "status": "candidate",
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "created_by_principal_id": actor["id"],
            "accepted_by_principal_id": None,
            "supersedes_finding_id": supersedes_finding_id,
            "revision_kind": revision_kind,
            "revision_reason": revision_reason,
            "source_ids": body["source_ids"],
            "variant_ids": body["variant_ids"],
        }
        connection.execute(
            """INSERT INTO research_findings
            (id,study_id,statement,status,revision,created_at,updated_at,
             created_by_principal_id,accepted_by_principal_id,supersedes_finding_id,
             revision_kind,revision_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                finding_id,
                study_id,
                body["statement"],
                "candidate",
                1,
                now,
                now,
                actor["id"],
                None,
                supersedes_finding_id,
                revision_kind,
                revision_reason,
            ),
        )
        connection.executemany(
            "INSERT INTO research_finding_sources (finding_id,source_id) VALUES (?,?)",
            [(finding_id, item) for item in body["source_ids"]],
        )
        connection.executemany(
            "INSERT INTO research_finding_variants (finding_id,variant_id) VALUES (?,?)",
            [(finding_id, item) for item in body["variant_ids"]],
        )
        return result

    def revise_finding(
        self,
        principal: dict[str, Any],
        finding_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | ReviseFinding,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(ReviseFinding, payload)
        self._validate_finding_reference_ids(body)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "revise a finding")
            finding = self._finding(connection, actor, finding_id)
            study = self._study(connection, actor, finding["study_id"])
            if finding["revision"] != body["expected_revision"]:
                raise Conflict("Finding revision is no longer revisable.")
            if finding["status"] not in {"candidate", "accepted", "needs_review"}:
                raise Conflict("Only nonterminal findings can be revised.")
            self._validate_finding_references(connection, actor, study["id"], body)
            prior_revision, prior_status = finding["revision"], finding["status"]
            now, revision = utc_now(), prior_revision + 1
            updated = connection.execute(
                """UPDATE research_findings SET status='superseded',revision=?,updated_at=?
                WHERE id=? AND revision=? AND status IN ('candidate','accepted','needs_review')""",
                (revision, now, finding_id, body["expected_revision"]),
            )
            if updated.rowcount != 1:
                raise Conflict("Finding revision is no longer revisable.")
            original = self._finding_response(
                connection,
                {
                    **finding,
                    "status": "superseded",
                    "revision": revision,
                    "updated_at": now,
                },
            )
            replacement = self._insert_finding(
                connection,
                actor,
                study["id"],
                body,
                supersedes_finding_id=finding_id,
                revision_kind=body["kind"],
                revision_reason=body["reason"],
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.finding_revised",
                revision,
                {
                    "study_id": study["id"],
                    "finding_id": finding_id,
                    "replacement_finding_id": replacement["id"],
                    "prior_revision": prior_revision,
                    "prior_status": prior_status,
                    "kind": body["kind"],
                    "reason": body["reason"],
                },
            )
            return 201, {"original": original, "replacement": replacement}

        return self._mutate(
            principal, key, "POST", f"/findings/{finding_id}/revise", digest, command
        )

    def accept_finding(
        self,
        principal: dict[str, Any],
        finding_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | AcceptFinding,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(AcceptFinding, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "accept a finding")
            finding = self._finding(connection, actor, finding_id)
            study = self._study(connection, actor, finding["study_id"])
            if finding["revision"] != body["expected_revision"] or finding["status"] != "candidate":
                raise Conflict("Finding revision is no longer acceptable.")
            sources = connection.execute(
                """SELECT so.verdict, so.superseded_at FROM research_sources so
                JOIN research_finding_sources fs ON fs.source_id=so.id
                WHERE fs.finding_id=?""",
                (finding_id,),
            ).fetchall()
            if not sources or any(
                row["verdict"] != "supported" or row["superseded_at"] is not None for row in sources
            ):
                raise Conflict(
                    "Accepted findings require currently supported, unsuperseded cited sources."
                )
            unresolved = connection.execute(
                "SELECT 1 FROM research_variants WHERE study_id=? AND status='registered' LIMIT 1",
                (study["id"],),
            ).fetchone()
            if study["mode"] == "confirmatory" and (
                study["status"] != "closed" or unresolved is not None
            ):
                raise Conflict(
                    "Confirmatory acceptance requires a closed, reconciled study ledger."
                )
            now, revision = utc_now(), finding["revision"] + 1
            connection.execute(
                "UPDATE research_findings SET status='accepted',revision=?,updated_at=?,"
                "accepted_by_principal_id=? WHERE id=? AND revision=? AND status='candidate'",
                (revision, now, actor["id"], finding_id, body["expected_revision"]),
            )
            result = self._finding_response(
                connection,
                {
                    **finding,
                    "status": "accepted",
                    "revision": revision,
                    "updated_at": now,
                    "accepted_by_principal_id": actor["id"],
                },
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.finding_accepted",
                revision,
                {"study_id": study["id"], "finding_id": finding_id, "status": "accepted"},
            )
            return 200, result

        return self._mutate(
            principal, key, "POST", f"/findings/{finding_id}/accept", digest, command
        )

    def reaffirm_finding(
        self,
        principal: dict[str, Any],
        finding_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | ReaffirmFinding,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(ReaffirmFinding, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "reaffirm a finding")
            finding = self._finding(connection, actor, finding_id)
            study = self._study(connection, actor, finding["study_id"])
            if finding["revision"] != body["expected_revision"]:
                raise Conflict("Finding revision is no longer reaffirmable.")
            if finding["status"] != "needs_review":
                raise Conflict("Only findings needing review can be reaffirmed.")
            sources = connection.execute(
                """SELECT so.verdict, so.superseded_at FROM research_sources so
                JOIN research_finding_sources fs ON fs.source_id=so.id
                WHERE fs.finding_id=?""",
                (finding_id,),
            ).fetchall()
            if not sources or any(
                row["verdict"] != "supported" or row["superseded_at"] is not None for row in sources
            ):
                raise Conflict(
                    "Reaffirmed findings require currently supported, unsuperseded cited sources."
                )
            unresolved = connection.execute(
                "SELECT 1 FROM research_variants WHERE study_id=? AND status='registered' LIMIT 1",
                (study["id"],),
            ).fetchone()
            if study["mode"] == "confirmatory" and (
                study["status"] != "closed" or unresolved is not None
            ):
                raise Conflict(
                    "Confirmatory reaffirmation requires a closed, reconciled study ledger."
                )
            prior_revision, prior_status = finding["revision"], finding["status"]
            now, revision = utc_now(), prior_revision + 1
            connection.execute(
                "UPDATE research_findings SET status='accepted',revision=?,updated_at=? "
                "WHERE id=? AND revision=? AND status='needs_review'",
                (revision, now, finding_id, body["expected_revision"]),
            )
            result = self._finding_response(
                connection,
                {
                    **finding,
                    "status": "accepted",
                    "revision": revision,
                    "updated_at": now,
                },
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.finding_reaffirmed",
                revision,
                {
                    "study_id": study["id"],
                    "finding_id": finding_id,
                    "prior_revision": prior_revision,
                    "reason": body["reason"],
                    "prior_status": prior_status,
                },
            )
            return 200, result

        return self._mutate(
            principal, key, "POST", f"/findings/{finding_id}/reaffirm", digest, command
        )

    def retract_finding(
        self,
        principal: dict[str, Any],
        finding_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | RetractFinding,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(RetractFinding, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "retract a finding")
            finding = self._finding(connection, actor, finding_id)
            study = self._study(connection, actor, finding["study_id"])
            if finding["revision"] != body["expected_revision"]:
                raise Conflict("Finding revision is no longer retractable.")
            if finding["status"] not in {"candidate", "accepted", "needs_review"}:
                raise Conflict("Only nonterminal findings can be retracted.")
            prior_revision, prior_status = finding["revision"], finding["status"]
            now, revision = utc_now(), prior_revision + 1
            updated = connection.execute(
                "UPDATE research_findings SET status='retracted',revision=?,updated_at=? "
                "WHERE id=? AND revision=? AND status IN ('candidate','accepted','needs_review')",
                (revision, now, finding_id, body["expected_revision"]),
            )
            if updated.rowcount != 1:
                raise Conflict("Finding revision is no longer retractable.")
            result = self._finding_response(
                connection,
                {
                    **finding,
                    "status": "retracted",
                    "revision": revision,
                    "updated_at": now,
                },
            )
            self._event_for(
                connection,
                actor,
                study,
                "research.finding_retracted",
                revision,
                {
                    "study_id": study["id"],
                    "finding_id": finding_id,
                    "prior_revision": prior_revision,
                    "prior_status": prior_status,
                    "reason": body["reason"],
                },
            )
            return 200, result

        return self._mutate(
            principal, key, "POST", f"/findings/{finding_id}/retract", digest, command
        )

    def close_study(
        self,
        principal: dict[str, Any],
        study_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CloseStudy,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(CloseStudy, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "close a study")
            study = self._study(connection, actor, study_id)
            if study["revision"] != body["expected_revision"] or study["status"] != "open":
                raise Conflict("Study revision is no longer closable.")
            if connection.execute(
                "SELECT 1 FROM research_variants WHERE study_id=? AND status='registered' LIMIT 1",
                (study_id,),
            ).fetchone():
                raise Conflict("Study ledger has unresolved registered variants.")
            now, revision = utc_now(), study["revision"] + 1
            connection.execute(
                "UPDATE research_studies SET status='closed',revision=?,updated_at=?,"
                "closed_at=? WHERE id=? AND revision=? AND status='open'",
                (revision, now, now, study_id, body["expected_revision"]),
            )
            result = {
                **study,
                "status": "closed",
                "revision": revision,
                "updated_at": now,
                "closed_at": now,
            }
            self._event_for(
                connection,
                actor,
                result,
                "research.study_closed",
                revision,
                {"study_id": study_id, "status": "closed"},
            )
            return 200, result

        return self._mutate(principal, key, "POST", f"/studies/{study_id}/close", digest, command)

    def correct_source(
        self,
        principal: dict[str, Any],
        source_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CorrectSource,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(CorrectSource, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "correct a source")
            original = self._source(connection, actor, source_id)
            if (
                original["revision"] != body["expected_revision"]
                or original["superseded_at"] is not None
            ):
                raise Conflict("Source revision is no longer correctable.")
            study = self._study(connection, actor, original["study_id"])
            now, new_id = utc_now(), "source_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO research_sources
                (id,study_id,uri,quoted_passage,content_digest,locator,verdict,
                 assessment_reason,revision,supersedes_source_id,superseded_at,created_at,
                 updated_at,created_by_principal_id,assessed_by_principal_id)
                VALUES (?,?,?,?,?,?, 'unverified',NULL,1,?,NULL,?,?,?,NULL)""",
                (
                    new_id,
                    study["id"],
                    body["uri"],
                    body["quoted_passage"],
                    body["content_digest"],
                    body["locator"],
                    source_id,
                    now,
                    now,
                    actor["id"],
                ),
            )
            connection.execute(
                "UPDATE research_sources SET superseded_at=?,revision=revision+1,"
                "updated_at=? WHERE id=? AND revision=?",
                (now, now, source_id, body["expected_revision"]),
            )
            correction_id = "correction_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO research_corrections
                (id,study_id,original_source_id,replacement_source_id,reason,actor_principal_id,
                 created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (correction_id, study["id"], source_id, new_id, body["reason"], actor["id"], now),
            )
            connection.execute(
                """UPDATE research_findings SET status='needs_review',revision=revision+1,
                updated_at=? WHERE id IN (
                    SELECT finding_id FROM research_finding_sources WHERE source_id=?
                ) AND status IN ('candidate','accepted')""",
                (now, source_id),
            )
            replacement = {
                "id": new_id,
                "study_id": study["id"],
                "uri": body["uri"],
                "quoted_passage": body["quoted_passage"],
                "content_digest": body["content_digest"],
                "locator": body["locator"],
                "verdict": "unverified",
                "assessment_reason": None,
                "revision": 1,
                "supersedes_source_id": source_id,
                "superseded_at": None,
                "created_at": now,
                "updated_at": now,
                "created_by_principal_id": actor["id"],
                "assessed_by_principal_id": None,
            }
            self._event_for(
                connection,
                actor,
                study,
                "research.source_corrected",
                original["revision"] + 1,
                {
                    "study_id": study["id"],
                    "source_id": source_id,
                    "replacement_source_id": new_id,
                    "correction_id": correction_id,
                    "dependent_findings_flagged": connection.execute("SELECT changes()").fetchone()[
                        0
                    ],
                },
            )
            return 201, {
                "correction_id": correction_id,
                "original_source_id": source_id,
                "replacement": replacement,
            }

        return self._mutate(
            principal, key, "POST", f"/sources/{source_id}/correction", digest, command
        )

    def export_study(
        self,
        principal: dict[str, Any],
        study_id: str,
        payload: dict[str, Any] | ExportStudy | None = None,
    ) -> dict[str, Any]:
        body = self._payload(ExportStudy, payload or {})
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            # A single deferred read transaction gives all export queries one SQLite
            # snapshot while allowing an in-flight writer to finish independently.
            connection.execute("BEGIN")
            actor = self._live_principal(connection, principal)
            study = self._study(connection, actor, study_id)

            event_pattern = f'%"study_id":"{study_id}"%'
            bounded_queries = (
                (
                    "dataset_snapshots",
                    "dataset_snapshots",
                    "SELECT COUNT(*) FROM dataset_snapshots WHERE study_id=?",
                    (study_id,),
                ),
                (
                    "dataset_ingestion_proposals",
                    "dataset_ingestion_proposals",
                    "SELECT COUNT(*) FROM dataset_ingestion_proposals WHERE study_id=?",
                    (study_id,),
                ),
                (
                    "variants",
                    "research_variants",
                    "SELECT COUNT(*) FROM research_variants WHERE study_id=?",
                    (study_id,),
                ),
                (
                    "sources",
                    "research_sources",
                    "SELECT COUNT(*) FROM research_sources WHERE study_id=?",
                    (study_id,),
                ),
                (
                    "findings",
                    "research_findings",
                    "SELECT COUNT(*) FROM research_findings WHERE study_id=?",
                    (study_id,),
                ),
                (
                    "corrections",
                    "research_corrections",
                    "SELECT COUNT(*) FROM research_corrections WHERE study_id=?",
                    (study_id,),
                ),
                (
                    "events",
                    "events",
                    "SELECT COUNT(*) FROM events WHERE project_id=? "
                    "AND event_type LIKE 'research.%' AND payload_json LIKE ?",
                    (study["project_id"], event_pattern),
                ),
            )
            for _, _, query, parameters in bounded_queries:
                count = connection.execute(query, parameters).fetchone()[0]
                if count > EXPORT_RECORD_LIMIT:
                    raise Conflict("Export exceeds the per-study record limit.")

            result: dict[str, Any] = {
                "study": study,
                "dataset_snapshots": [],
                "dataset_ingestion_proposals": [],
                "variants": [],
                "sources": [],
                "findings": [],
                "corrections": [],
                "events": [],
                "completeness": "complete_in_scope_study_ledger",
            }
            # Count the initial envelope, then each row before retaining it.  The
            # small per-row delimiter margin is conservative; exact encoding below
            # is a final assertion, never an allocation-time truncation mechanism.
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
            if used > body["max_bytes"]:
                raise Conflict("Export exceeds the requested byte cap.")

            def append(section: str, item: dict[str, Any]) -> None:
                nonlocal used
                used += (
                    len(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
                    + 2
                )
                if used > body["max_bytes"]:
                    raise Conflict("Export exceeds the requested byte cap.")
                result[section].append(item)

            for row in connection.execute(
                "SELECT * FROM dataset_snapshots WHERE study_id=? ORDER BY created_at,id",
                (study_id,),
            ):
                append("dataset_snapshots", self._dataset_snapshot_response(connection, dict(row)))
            for row in connection.execute(
                "SELECT * FROM dataset_ingestion_proposals WHERE study_id=? ORDER BY created_at,id",
                (study_id,),
            ):
                append("dataset_ingestion_proposals", dict(row))

            for row in connection.execute(
                "SELECT * FROM research_variants WHERE study_id=? ORDER BY created_at,id",
                (study_id,),
            ):
                append("variants", self._variant_response(dict(row)))
            for row in connection.execute(
                "SELECT * FROM research_sources WHERE study_id=? ORDER BY created_at,id",
                (study_id,),
            ):
                append("sources", dict(row))
            for row in connection.execute(
                "SELECT * FROM research_findings WHERE study_id=? ORDER BY created_at,id",
                (study_id,),
            ):
                append("findings", self._finding_response(connection, dict(row)))
            for row in connection.execute(
                "SELECT * FROM research_corrections WHERE study_id=? ORDER BY created_at,id",
                (study_id,),
            ):
                append("corrections", dict(row))
            for row in connection.execute(
                """SELECT cursor,event_type,resource_revision,actor_principal_id,created_at,
                payload_json FROM events WHERE project_id=? AND event_type LIKE 'research.%'
                AND payload_json LIKE ?
                ORDER BY cursor""",
                (study["project_id"], event_pattern),
            ):
                event = dict(row)
                event["payload"] = json.loads(event.pop("payload_json"))
                if event["payload"].get("study_id") == study_id:
                    append("events", event)
            from .agent_provenance import project_agent_notebook

            if actor["kind"] in {"local_owner", "human_session"}:
                result["agent_notebook"] = project_agent_notebook(
                    connection, actor, study["project_id"], max_bytes=max(1024, body["max_bytes"])
                )
            else:
                result["agent_notebook"] = {
                    "availability": "unavailable",
                    "reason": "separate_project_notebook_authority_required",
                    "completeness": "not_disclosed",
                }
            encoded = json.dumps(
                result, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()
            if len(encoded) > body["max_bytes"]:
                raise Conflict("Export exceeds the requested byte cap.")
            return result
        finally:
            connection.close()

    @staticmethod
    def _variant_response(row: dict[str, Any]) -> dict[str, Any]:
        response = dict(row)
        response["metadata"] = json.loads(response.pop("metadata_json"))
        response["outcome"] = (
            json.loads(response.pop("outcome_json")) if response.get("outcome_json") else None
        )
        response.pop("outcome_json", None)
        response["execution_receipt"] = None
        response["independence_status"] = "unavailable"
        return response

    @staticmethod
    def _dataset_snapshot_response(
        connection: sqlite3.Connection, row: dict[str, Any]
    ) -> dict[str, Any]:
        response = dict(row)
        response["schema"] = json.loads(response.pop("schema_json"))
        response["validation_summary"] = json.loads(response.pop("validation_summary_json"))
        response["split_strategy"] = json.loads(response.pop("split_strategy_json"))
        response["sources"] = [
            dict(item)
            for item in connection.execute(
                "SELECT source_id,expected_source_revision,expected_content_digest,"
                "license_expression,"
                "license_status,provenance_kind,provenance_status FROM dataset_snapshot_sources "
                "WHERE snapshot_id=? ORDER BY source_id",
                (response["id"],),
            )
        ]
        response["content_validation"] = "not_performed"
        return response

    @staticmethod
    def _finding_response(connection: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["source_ids"] = [
            item[0]
            for item in connection.execute(
                "SELECT source_id FROM research_finding_sources WHERE finding_id=? "
                "ORDER BY source_id",
                (row["id"],),
            )
        ]
        result["variant_ids"] = [
            item[0]
            for item in connection.execute(
                "SELECT variant_id FROM research_finding_variants WHERE finding_id=? "
                "ORDER BY variant_id",
                (row["id"],),
            )
        ]
        return result
