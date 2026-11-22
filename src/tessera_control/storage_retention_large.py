"""Orphaned large artifacts (for example weights archives) in the retention policy.

A large artifact is held as chunk rows in the control database (ADR 0050). Which ones are
reclaimable is decided by ``large_artifacts.reclaimable`` and re-checked atomically by
``reclaim_artifact_bytes``; this module only lists them (read-only, opt-in, never shorter than
the operator's orphan age) and drives that existing reclaim as the project's root local owner.
The quarantine row stays as evidence and the existing reclaim event is recorded by it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artifact_limits import operator_limits
from .large_artifacts import (
    LargeArtifactStore,
    candidate_by_id,
    control_is_active,
    reclaim_artifact_bytes,
    reclaimable,
)
from .storage_retention_artifacts import _system_actor
from .storage_retention_models import Candidate, RetentionPolicy
from .store import _connect, _database_path


class LargeReclaimError(RuntimeError):
    """The large artifact was not reclaimed; the message is fixed text, never a path."""


def _skip(ref: str, reason: str) -> dict[str, str]:
    return {"kind": "large_artifact", "ref": ref, "reason": reason}


def list_large_candidates(
    state_dir: Path, policy: RetentionPolicy, *, now: datetime
) -> tuple[list[Candidate], list[dict[str, str]]]:
    """Read-only plan input: reclaimable large artifacts, only when the policy opts in."""
    if not policy.reclaim_orphaned_large_artifacts:
        return [], []
    database_path = _database_path(state_dir)
    if not database_path.is_file():
        return [], [_skip("", "control database not found")]
    now = now.astimezone(UTC)
    candidates: list[Candidate] = []
    skipped: list[dict[str, str]] = []
    connection = _connect(database_path)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")  # one snapshot for every project
        if not control_is_active(connection):
            connection.execute("COMMIT")
            return [], [_skip("", "control is held after restore; reclaiming is disabled")]
        orphan_seconds = operator_limits().orphan_artifact_seconds
        projects = [row[0] for row in connection.execute("SELECT id FROM projects ORDER BY id")]
        for project_id in projects:
            try:
                found = reclaimable(connection, project_id, now, orphan_seconds)
                for item in found:
                    row = connection.execute(
                        "SELECT sha256 FROM artifacts WHERE id=?", (item["id"],)
                    ).fetchone()
                    if row is None:
                        skipped.append(_skip(item["id"], "artifact row unreadable"))
                        continue
                    candidates.append(
                        Candidate(
                            kind="large_artifact",
                            ref=item["id"],
                            path=item["relative_path"],
                            size_bytes=item["size"],
                            sha256=row["sha256"],
                            created_at=item["created_at"],
                            decision="delete",
                            reasons=[item["reason"]],
                            project_id=project_id,
                        )
                    )
            except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
                skipped.append(_skip(project_id, f"project unreadable ({type(error).__name__})"))
        connection.execute("COMMIT")
    except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
        return [], [_skip("", f"control database unreadable ({type(error).__name__})")]
    finally:
        connection.close()
    return candidates, skipped


def delete_large(state_dir: Path, candidate: Candidate, *, run_id: str) -> dict[str, Any]:
    """Free one planned large artifact's chunk rows; raises, deleting nothing more, if refused.

    The artifact must still match the plan (digest and size) and ``reclaim_artifact_bytes``
    re-checks that it is still reclaimable, for the same reason, atomically.
    """
    if (
        candidate.kind != "large_artifact"
        or candidate.decision != "delete"
        or not candidate.project_id
    ):
        raise ValueError("Only a planned large artifact reclaim can be applied.")
    if not run_id:
        raise ValueError("A run id is required.")
    store = LargeArtifactStore(state_dir)
    orphan_seconds = operator_limits().orphan_artifact_seconds
    connection = _connect(store.database_path)
    try:
        artifact = candidate_by_id(connection, candidate.ref)
        if artifact is None or artifact["project_id"] != candidate.project_id:
            raise LargeReclaimError("Large artifact no longer holds reclaimable bytes.")
        row = connection.execute(
            "SELECT sha256 FROM artifacts WHERE id=?", (candidate.ref,)
        ).fetchone()
        if row is None or (row["sha256"], artifact["size"]) != (
            candidate.sha256,
            candidate.size_bytes,
        ):
            raise LargeReclaimError("Large artifact no longer matches the planned reclaim.")
        actor = _system_actor(connection, candidate.project_id)
        live = store._live_principal(connection, actor, audience="control")
    finally:
        connection.close()
    if not reclaim_artifact_bytes(
        store,
        live,
        artifact,
        candidate.reasons[0] if candidate.reasons else "",
        trigger="retention",
        orphan_seconds=orphan_seconds,
    ):
        raise LargeReclaimError("Large artifact is no longer reclaimable or was already reclaimed.")
    return {
        "ref": candidate.ref,
        "size_bytes": candidate.size_bytes,
        "sha256": candidate.sha256,
        "state": "reclaimed",
    }
