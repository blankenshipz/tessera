"""Typed evidence rows a campaign stage emits, ingested by the platform itself (ADR 0052).

A stage output of kind ``evidence_rows`` holds ``{"rows": [{variant, seed, metric, value,
partition, sample_count}]}``. When the stage's approved spec declares the grid (split lock,
variants, seeds, metrics with units, partitions), the runner asks the platform to ingest the
retained output under the campaign's approval. The platform, not the stage or a person,
validates every row against the split lock and the pre-registered grid and stores it
append-only, so a paired-delta gate no longer depends on a person transcribing numbers.

Rows are measured numbers, ``not_assessed``. A partial, padded or inconsistent file is
refused whole: nothing is ingested and any unlock that needs it stays closed.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .approval_trust import DELEGATED_ACTOR_KEY
from .store import Conflict, Forbidden, NotFound, StoreError, utc_now

MAX_ROWS = 5000
_Ident = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EvidenceRow(_Strict):
    variant: _Ident
    seed: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    metric: _Ident
    value: Annotated[float, Field(allow_inf_nan=False)]
    partition: _Ident
    sample_count: Annotated[int, Field(strict=True, ge=1, le=2**53)]


class EvidenceRowsDocument(_Strict):
    rows: Annotated[list[EvidenceRow], Field(min_length=1, max_length=MAX_ROWS)]


class IngestEvidenceRows(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    output_name: _Ident
    artifact_id: _Ident


def create_evidence_ingest_schema(connection: sqlite3.Connection) -> None:
    """Idempotent and additive; shared by the migration and fresh databases."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS gate_ingested_rows (
        id TEXT PRIMARY KEY CHECK(id GLOB 'gate_ingested_*'),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        stage_key TEXT NOT NULL,
        attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
        artifact_id TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        membership_digest TEXT NOT NULL,
        variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        seed INTEGER NOT NULL CHECK(seed >= 0),
        metric TEXT NOT NULL, unit TEXT NOT NULL, partition TEXT NOT NULL,
        sample_count INTEGER NOT NULL CHECK(sample_count >= 1),
        value REAL NOT NULL,
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(attempt_id, variant_id, lock_id, metric, partition, seed))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS gate_ingested_rows_by_series "
        "ON gate_ingested_rows(variant_id, lock_id, metric, partition, seed)"
    )
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS gate_ingested_rows_no_{event.lower()}
            BEFORE {event} ON gate_ingested_rows
            BEGIN SELECT RAISE(ABORT,'gate_ingested_rows rows are append-only'); END"""
        )


def ingested_series(
    connection: sqlite3.Connection,
    project_id: str,
    variant_id: str,
    lock_id: str,
    metric: str,
    partition: str,
    campaign_id: str | None = None,
) -> list[dict[str, Any]]:
    """Ingested rows of one series shaped like metric records (pure read).

    With ``campaign_id`` only that campaign's rows (a repeat run of the same grid in another
    campaign never collides with it); without it every campaign's rows, used only to refuse
    storing a group that would silently mix runs.
    """
    return [
        {
            **dict(row),
            "attempt_id": row["attempt_id"],
            "measurement_source": "derived",
        }
        for row in connection.execute(
            """SELECT * FROM gate_ingested_rows WHERE project_id=? AND variant_id=?
            AND lock_id=? AND metric=? AND partition=? AND (? IS NULL OR campaign_id=?)
            ORDER BY seed""",
            (project_id, variant_id, lock_id, metric, partition, campaign_id, campaign_id),
        )
    ]


def _unique_object(pairs):
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _parse_rows(content: bytes) -> list[EvidenceRow]:
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
        )
        return EvidenceRowsDocument.model_validate(document).rows
    except (UnicodeDecodeError, ValueError, ValidationError) as error:
        raise Conflict("Evidence rows are not a valid {rows:[...]} document.") from error


class EvidenceIngestMixin:
    """Mixed into ExperimentStore (needs its backend, _evidence_attempt and custody read)."""

    def ingest_evidence_rows(
        self, principal: dict[str, Any], attempt_id: str, key: str, digest: str, payload: Any
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = IngestEvidenceRows.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid evidence rows request.") from error

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            delegated = actor.get(DELEGATED_ACTOR_KEY)
            if not delegated:
                # No human route: only a campaign's approved stage may ingest (ADR 0052).
                raise Forbidden("Evidence rows are ingested only by an approved campaign stage.")
            self._human(actor)  # type: ignore[attr-defined]
            attempt = self._evidence_attempt(connection, actor, attempt_id)  # type: ignore[attr-defined]
            if attempt["attempt_kind"] != "admitted" or attempt["outcome"] != "succeeded":
                raise Conflict("Evidence rows need a succeeded admitted attempt.")
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Experiment attempt revision is no longer current.")
            spec = self._approved_evidence_spec(
                connection, delegated, attempt_id, body, actor["org_id"], attempt["project_id"]
            )
            content, sha256 = self._read_retained_output(  # type: ignore[attr-defined]
                connection, actor, attempt, attempt_id, body["artifact_id"]
            )
            rows = _parse_rows(content)
            units = self._validate_grid(connection, attempt["project_id"], spec, rows)
            lock = connection.execute(
                "SELECT membership_digest FROM gate_split_locks WHERE id=? AND project_id=?",
                (spec.split_lock_id, attempt["project_id"]),
            ).fetchone()
            now = utc_now()
            try:
                for row in rows:
                    connection.execute(
                        """INSERT INTO gate_ingested_rows (id,org_id,project_id,campaign_id,
                        stage_key,attempt_id,artifact_id,artifact_sha256,lock_id,
                        membership_digest,variant_id,seed,metric,unit,partition,sample_count,
                        value,created_at,created_by_principal_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "gate_ingested_" + uuid.uuid4().hex,
                            actor["org_id"],
                            attempt["project_id"],
                            delegated["campaign_id"],
                            spec_stage_key(connection, delegated),
                            attempt_id,
                            body["artifact_id"],
                            sha256,
                            spec.split_lock_id,
                            lock["membership_digest"],
                            row.variant,
                            row.seed,
                            row.metric,
                            units[row.metric],
                            row.partition,
                            row.sample_count,
                            row.value,
                            now,
                            actor["id"],
                        ),
                    )
            except sqlite3.IntegrityError as error:
                raise Conflict("An evidence row conflicts with an existing record.") from error
            experiment = self._experiment(connection, actor, attempt["experiment_id"])  # type: ignore[attr-defined]
            self._event(  # type: ignore[attr-defined]
                connection,
                actor,
                experiment,
                "gate.evidence_rows_ingested",
                attempt["revision"],
                {
                    "attempt_id": attempt_id,
                    "artifact_id": body["artifact_id"],
                    "artifact_sha256": sha256,
                    "lock_id": spec.split_lock_id,
                    "rows": len(rows),
                    "scientific_validity": "not_assessed",
                },
            )
            return 201, {
                "attempt_id": attempt_id,
                "rows": len(rows),
                "artifact_sha256": sha256,
                "lock_id": spec.split_lock_id,
                "scientific_validity": "not_assessed",
            }

        return self._mutate(  # type: ignore[attr-defined]
            principal,
            key,
            "POST",
            f"/experiment-attempts/{attempt_id}/evidence-rows",
            digest,
            command,
        )

    @staticmethod
    def _approved_evidence_spec(connection, delegated, attempt_id, body, org_id, project_id):
        """The approved output spec for this intent's stage, never the request's claim."""
        from .campaign_delegation import _normal_spec

        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=? AND org_id=? AND project_id=?",
            (delegated["campaign_id"], org_id, project_id),
        ).fetchone()
        stage_key = spec_stage_key(connection, delegated)
        owned = connection.execute(
            """SELECT manifest_id FROM campaign_stage_attempts WHERE campaign_id=?
            AND stage_key=? AND admitted_attempt_id=?""",
            (delegated["campaign_id"], stage_key, attempt_id),
        ).fetchone()
        if campaign is None or owned is None:
            raise Forbidden("The attempt does not belong to this campaign stage.")
        try:
            output = next(
                o
                for o in _normal_spec(campaign).stage(stage_key).outputs
                if o.name == body["output_name"]
            )
        except (StopIteration, ValueError, TypeError):
            raise Forbidden("The stage declares no such output.") from None
        if output.evidence is None:
            raise Forbidden("The stage output does not declare evidence rows.")
        manifest = connection.execute(
            "SELECT artifacts_json FROM artifact_manifests WHERE id=? AND project_id=?",
            (owned["manifest_id"], project_id),
        ).fetchone()
        artifacts = json.loads(manifest["artifacts_json"]) if manifest else []
        if not any(
            item["id"] == body["artifact_id"] and item["relative_path"] == output.path
            for item in artifacts
        ):
            raise Conflict("The artifact is not the declared output of this attempt.")
        return output.evidence

    @staticmethod
    def _validate_grid(connection, project_id, spec, rows) -> dict[str, str]:
        """Every row is inside the approved grid and the lock; the grid is filled exactly."""
        lock = connection.execute(
            "SELECT partitions_json FROM gate_split_locks WHERE id=? AND project_id=?",
            (spec.split_lock_id, project_id),
        ).fetchone()
        if lock is None:
            raise NotFound("Split lock was not found.")
        members = {p["name"]: p["member_count"] for p in json.loads(lock["partitions_json"])}
        if not set(spec.partitions) <= set(members):
            raise Conflict("A declared partition is not part of the split lock.")
        for variant in spec.variants:
            found = connection.execute(
                """SELECT 1 FROM research_experiment_variants v
                JOIN research_experiments e ON e.id=v.experiment_id
                WHERE v.id=? AND e.project_id=?""",
                (variant, project_id),
            ).fetchone()
            if found is None:
                raise NotFound("A declared evidence variant was not found.")
        expected = {
            (v, s, m, p)
            for v in spec.variants
            for s in spec.seeds
            for m in spec.metrics
            for p in spec.partitions
        }
        seen: set[tuple[str, int, str, str]] = set()
        for row in rows:
            cell = (row.variant, row.seed, row.metric, row.partition)
            if cell not in expected:
                raise Conflict("An evidence row is outside the pre-registered grid.")
            if cell in seen:
                raise Conflict("An evidence cell appears twice.")
            seen.add(cell)
            if row.sample_count != members[row.partition]:
                raise Conflict("An evidence row's sample_count differs from the split lock.")
            if not math.isfinite(row.value):
                raise Conflict("An evidence row value is not finite.")
        if seen != expected:
            raise Conflict(
                f"Evidence rows leave {len(expected - seen)} pre-registered cell(s) empty."
            )
        return dict(spec.metrics)


def spec_stage_key(connection: sqlite3.Connection, delegated: dict[str, Any]) -> str:
    row = connection.execute(
        "SELECT stage_key FROM campaign_step_intents WHERE id=? AND campaign_id=?",
        (delegated["intent_id"], delegated["campaign_id"]),
    ).fetchone()
    if row is None or row["stage_key"] is None:
        raise Forbidden("Delegation does not authorize this step.")
    return row["stage_key"]
