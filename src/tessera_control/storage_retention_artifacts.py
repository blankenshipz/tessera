"""Artifact side of the storage retention policy: select unreferenced old objects, delete safely.

Selection is read-only and fail closed: anything that might still be referenced is kept.
Deletion never touches the object store directly; it goes through the existing
``ProjectArtifactRetentionStore`` tombstone then reconcile-delete commands, so the store's
own transactional checks, idempotency records and degraded-state handling all apply.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .project_artifact_retention import ProjectArtifactRetentionStore
from .project_artifacts import ProjectArtifactBackend
from .storage_retention_models import Candidate, RetentionPolicy
from .store import Conflict, NotFound, StoreError, _connect, _database_path, canonical_digest

SYSTEM_ACTOR_LABEL = "system:retention"
DELETABLE_STATES = ("active", "tombstoned", "delete_intent")
# Tables that describe the object itself, not a use of it; every other table with an
# ``artifact_id`` column is treated as a live reference (new evidence tables fail closed).
_OWN_TABLES = frozenset(
    {
        "project_artifact_placements",
        "project_artifact_retention",
        "project_artifact_storage_operations",
    }
)
_LINEAGE_TABLE = "artifact_lineage_nodes"


class RetentionDeleteError(RuntimeError):
    """The object was not confirmed deleted; ``tombstoned`` says it was already reserved."""

    def __init__(self, message: str, *, tombstoned: bool = False) -> None:
        super().__init__(message)
        self.tombstoned = tombstoned


class _ReferenceIndex:
    """Everything that keeps an artifact alive, loaded once inside one connection/transaction."""

    def __init__(self) -> None:
        self.referenced: set[str] = set()
        self.checkpoint: set[str] = set()
        self.lineage_ids: set[str] = set()
        self.lineage_digests: set[tuple[str, str]] = set()  # (project_id, sha256)
        self.hold: set[str] = set()
        self.campaign_digests: set[tuple[str, str]] = set()  # (project_id, sha256) of stage I/O
        # Reasons that protect every artifact because metadata could not be understood.
        self.closed: set[str] = set()

    def reasons(self, project_id: str, artifact_id: str, sha256: str) -> list[str]:
        out: list[str] = []
        if artifact_id in self.referenced or "referenced" in self.closed:
            out.append("referenced")
        if artifact_id in self.checkpoint or "checkpoint" in self.closed:
            out.append("checkpoint")
        if (
            artifact_id in self.lineage_ids
            or (project_id, sha256) in self.lineage_digests
            or "lineage" in self.closed
        ):
            out.append("lineage")
        if artifact_id in self.hold or "unreleased_hold" in self.closed:
            out.append("unreleased_hold")
        if (project_id, sha256) in self.campaign_digests or "campaign" in self.closed:
            out.append("campaign")
        return out


def _manifest_ids(raw: Any) -> set[str] | None:
    """Artifact ids named by one manifest, or None when the metadata is not understood."""
    try:
        manifest = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(manifest, list) or any(not isinstance(item, dict) for item in manifest):
        return None
    ids = set()
    for item in manifest:
        value = item.get("id")
        if value is not None:
            if not isinstance(value, str):
                return None
            ids.add(value)
    return ids


def _load_references(connection: sqlite3.Connection) -> _ReferenceIndex:
    index = _ReferenceIndex()

    def guard(reason: str, load: Any) -> None:
        try:
            load()
        except (sqlite3.Error, TypeError, ValueError, KeyError):
            index.closed.add(reason)  # unknown table or malformed metadata: keep everything

    manifests: dict[str, set[str]] = {}

    def load_manifests() -> None:
        for row in connection.execute("SELECT id, artifacts_json FROM artifact_manifests"):
            ids = _manifest_ids(row[1])
            if ids is None:
                index.closed.update({"referenced", "checkpoint", "unreleased_hold"})
                continue
            manifests[row[0]] = ids
            index.referenced |= ids

    guard("referenced", load_manifests)

    def load_checkpoints() -> None:
        for row in connection.execute("SELECT manifest_id FROM execution_result_checkpoints"):
            index.checkpoint |= manifests.get(row[0], set())
            if row[0] not in manifests:
                index.closed.add("checkpoint")

    guard("checkpoint", load_checkpoints)

    def load_holds() -> None:
        # Dispositions are append-only; the latest revision per manifest is the live one and
        # only 'reject' releases it from quarantine review. Anything else is unreleased.
        for row in connection.execute(
            """SELECT d.manifest_id, d.disposition FROM artifact_dispositions d
            WHERE d.revision = (SELECT max(revision) FROM artifact_dispositions
                                WHERE manifest_id = d.manifest_id)"""
        ):
            if row[1] != "reject":
                index.hold |= manifests.get(row[0], set())
                if row[0] not in manifests:
                    index.closed.add("unreleased_hold")

    guard("unreleased_hold", load_holds)

    def load_tables() -> None:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            if table in _OWN_TABLES:
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")}
            if "artifact_id" not in columns:
                continue
            ids = {
                row[0]
                for row in connection.execute(
                    f"SELECT DISTINCT artifact_id FROM {quoted} WHERE artifact_id IS NOT NULL"
                )
            }
            if table == _LINEAGE_TABLE:
                index.lineage_ids |= ids
            else:
                index.referenced |= ids

    guard("referenced", load_tables)

    def load_lineage() -> None:
        # A lineage node names an object by digest as well; edges only join node ids, so a
        # node is the only way an artifact can be a parent, input or output of an edge.
        for row in connection.execute("SELECT project_id, sha256 FROM artifact_lineage_nodes"):
            index.lineage_digests.add((row[0], row[1]))
        connection.execute("SELECT 1 FROM artifact_lineage_edges LIMIT 1").fetchall()

    guard("lineage", load_lineage)

    def load_campaign_io() -> None:
        # A campaign stage's input or output names an object by digest (not by artifact id),
        # whatever the campaign's status: while the row exists the bytes stay.
        for row in connection.execute("SELECT project_id, sha256 FROM campaign_stage_io"):
            index.campaign_digests.add((row[0], row[1]))

    guard("campaign", load_campaign_io)
    return index


def _parse_time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def list_artifact_candidates(
    state_dir: Path, policy: RetentionPolicy, *, now: datetime
) -> tuple[list[Candidate], list[dict[str, str]]]:
    """Read-only plan input: every placed artifact with a retention row, deleted or kept."""
    database_path = _database_path(state_dir)
    if not database_path.is_file():
        return [], [{"kind": "artifact", "ref": "", "reason": "control database not found"}]
    cutoff = None
    if policy.artifact_max_age_days is not None:
        cutoff = now.astimezone(UTC) - timedelta(days=policy.artifact_max_age_days)
    candidates: list[Candidate] = []
    skipped: list[dict[str, str]] = []
    connection = _connect(database_path)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")  # one snapshot for the rows and the reference index
        rows = connection.execute(
            """SELECT p.artifact_id, p.project_id, p.object_key, p.sha256, p.size,
            p.retention_class AS placement_class, r.retention_class, r.state,
            r.created_at, r.activity_at
            FROM project_artifact_placements p
            JOIN project_artifact_retention r ON r.artifact_id = p.artifact_id
            ORDER BY r.created_at, p.artifact_id"""
        ).fetchall()
        index = _load_references(connection)
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        return [], [
            {"kind": "artifact", "ref": "", "reason": f"control database unreadable: {error}"}
        ]
    finally:
        connection.close()
    for row in rows:
        reasons: list[str] = []
        activity = _parse_time(row["activity_at"])
        if activity is None:
            reasons.append("bad_timestamp")
            skipped.append(
                {
                    "kind": "artifact",
                    "ref": row["artifact_id"],
                    "reason": "unreadable activity time",
                }
            )
        if row["state"] == "degraded":
            reasons.append("degraded")
        elif row["state"] == "deleted":
            reasons.append("deleted")
        elif row["state"] not in DELETABLE_STATES:
            reasons.append("unknown_state")
        if "held" in (row["retention_class"], row["placement_class"]):
            reasons.append("held")
        elif row["retention_class"] not in ("project_default", "short_lived"):
            reasons.append("unknown_class")
        reasons += index.reasons(row["project_id"], row["artifact_id"], row["sha256"])
        if cutoff is None:
            reasons.append("no_age_limit")
        elif activity is not None and activity >= cutoff:
            reasons.append("too_new")
        decision = "keep" if reasons else "delete"
        candidates.append(
            Candidate(
                kind="artifact",
                ref=row["artifact_id"],
                path=row["object_key"],
                size_bytes=row["size"],
                sha256=row["sha256"],
                created_at=row["created_at"],
                decision=decision,
                reasons=reasons or ["max_age"],
                project_id=row["project_id"],
            )
        )
    return candidates, skipped


class _SystemRetentionStore(ProjectArtifactRetentionStore):
    """The existing store plus the extra checks retention needs, run in its transactions."""

    expected: tuple[str, int] | None = None

    def _delete_allowed(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        super()._delete_allowed(connection, row)  # held, manifest, evidence, checkpoint
        if self.expected is not None and (row["sha256"], row["size"]) != self.expected:
            raise Conflict("Project artifact no longer matches the planned deletion.")
        reasons = _load_references(connection).reasons(
            row["project_id"], row["artifact_id"], row["sha256"]
        )
        if reasons:
            raise Conflict("Project artifact is protected from retention: " + ", ".join(reasons))


def _system_actor(connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """The project's root local owner, the only credential the store lets drive these commands.

    The retention run executes with local state-directory access, which already is owner
    authority; it never mints a credential. Store requirements (principal liveness, tenant
    scoping, control-recovery state) are still enforced by the store itself.
    """
    row = connection.execute(
        """SELECT pr.id, pr.org_id FROM principals pr
        JOIN credentials c ON c.principal_id = pr.id
        JOIN projects p ON p.org_id = pr.org_id
        WHERE p.id = ? AND pr.role = 'human' AND c.kind = 'local_owner'
        AND c.parent_id IS NULL AND pr.revoked_at IS NULL AND c.revoked_at IS NULL
        ORDER BY pr.created_at, pr.id LIMIT 1""",
        (project_id,),
    ).fetchone()
    if row is None:
        raise StoreError("No live local owner is available to record the retention deletion.")
    return {"id": row["id"], "org_id": row["org_id"]}


def _key(run_id: str, artifact_id: str, step: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{artifact_id}\0{step}".encode()).hexdigest()
    return f"{SYSTEM_ACTOR_LABEL}:{step}:{digest[:40]}"


def delete_artifact(
    state_dir: Path, backend: ProjectArtifactBackend, candidate: Candidate, *, run_id: str
) -> dict[str, Any]:
    """Tombstone then delete one planned artifact; raises, deleting nothing, if refused.

    Idempotency keys derive from ``run_id`` and the artifact id, so a retry of the same run
    replays the store's recorded result instead of acting twice.
    """
    if candidate.kind != "artifact" or candidate.decision != "delete" or not candidate.project_id:
        raise ValueError("Only a planned artifact deletion can be applied.")
    if not run_id:
        raise ValueError("A run id is required.")
    store = _SystemRetentionStore(state_dir, backend)
    store.expected = (candidate.sha256, candidate.size_bytes)
    connection = _connect(store.database_path)
    try:
        actor = _system_actor(connection, candidate.project_id)
    finally:
        connection.close()
    digest = canonical_digest(
        {
            "actor": SYSTEM_ACTOR_LABEL,
            "run_id": run_id,
            "artifact_id": candidate.ref,
            "sha256": candidate.sha256,
            "size_bytes": candidate.size_bytes,
        }
    )
    store.tombstone(
        actor, candidate.project_id, candidate.ref, _key(run_id, candidate.ref, "tombstone"), digest
    )
    try:
        _, result = store.reconcile_delete(
            actor,
            candidate.project_id,
            candidate.ref,
            _key(run_id, candidate.ref, "delete"),
            digest,
        )
    except Exception as error:
        # The artifact is tombstoned (no new references) but not deleted: say so, with why.
        # The store's own refusals are fixed text; anything else (an OS error can carry a
        # path) is named by type only.
        detail = str(error) if isinstance(error, Conflict | NotFound | StoreError) else ""
        raise RetentionDeleteError(
            f"refused after tombstone ({type(error).__name__})" + (f": {detail}" if detail else ""),
            tombstoned=True,
        ) from error
    if result.get("state") != "deleted":
        raise RetentionDeleteError(
            f"Artifact {candidate.ref} was not confirmed deleted (state {result.get('state')})."
        )
    return {
        "ref": candidate.ref,
        "project_id": candidate.project_id,
        "size_bytes": candidate.size_bytes,
        "sha256": candidate.sha256,
        "state": "deleted",
    }
