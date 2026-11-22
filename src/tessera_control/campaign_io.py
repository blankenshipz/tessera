"""Persistent stage I/O index and cross-campaign artifact inputs (ADR 0040 section 3).

``campaign_stage_io`` names every resolved input and output of an attempt: org, project,
campaign, stage, attempt, direction, name, kind, media type, sha256, size, the snapshot or
(manifest, path), the platform's recorded data class and the lineage node. Rows are written
in the runner transaction that marks the input ready or the attempt succeeded, and triggers
refuse update and delete. The bytes stay in snapshots and retained manifests; the index
only names them. No class is ever upgraded: outputs are ``private_derived`` and an
``artifact`` input records the platform's class for the pinned row, never the spec's claim.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .data_class import OUTPUT_DATA_CLASS
from .store import Conflict


@dataclass(frozen=True)
class IoEntry:
    name: str
    kind: str
    sha256: str
    size_bytes: int
    data_class: str
    media_type: str | None = None
    snapshot_id: str | None = None
    manifest_id: str | None = None
    path: str | None = None
    lineage_node_id: str | None = None


@dataclass(frozen=True)
class ResolvedArtifact:
    """One pinned output of an earlier campaign, ready to compose as a manifest source."""

    name: str
    manifest_id: str
    manifest_digest: str
    path: str
    sha256: str
    size_bytes: int
    data_class: str
    kind: str
    media_type: str | None
    source_campaign_id: str
    source_stage: str
    source_attempt_id: str
    lineage_node_id: str | None


def write_io(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    stage_key: str,
    attempt_id: str | None,
    direction: str,
    entries: Sequence[IoEntry],
    now: str,
) -> int:
    """Append index rows for one (attempt, direction); a repeat writes nothing.

    Called inside the transaction that advances the attempt, so the rows and the state
    change commit together or not at all.
    """
    existing = connection.execute(
        """SELECT name FROM campaign_stage_io WHERE campaign_id=? AND stage_key=?
        AND attempt_id IS ? AND direction=?""",
        (campaign["id"], stage_key, attempt_id, direction),
    ).fetchall()
    seen = {row["name"] for row in existing}
    written = 0
    for entry in entries:
        if entry.name in seen:
            continue
        connection.execute(
            """INSERT INTO campaign_stage_io (id, org_id, project_id, campaign_id, stage_key,
            attempt_id, direction, name, kind, media_type, sha256, size_bytes, snapshot_id,
            manifest_id, path, data_class, lineage_node_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "stageio_" + uuid.uuid4().hex,
                campaign["org_id"],
                campaign["project_id"],
                campaign["id"],
                stage_key,
                attempt_id,
                direction,
                entry.name,
                entry.kind,
                entry.media_type,
                entry.sha256,
                entry.size_bytes,
                entry.snapshot_id,
                entry.manifest_id,
                entry.path,
                entry.data_class,
                entry.lineage_node_id,
                now,
            ),
        )
        written += 1
    return written


def lineage_node(connection: sqlite3.Connection, project_id: str, sha256: str) -> str | None:
    row = connection.execute(
        "SELECT id FROM artifact_lineage_nodes WHERE project_id=? AND sha256=?",
        (project_id, sha256),
    ).fetchone()
    return None if row is None else row["id"]


def output_entries(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    stage: Any,
    manifest_id: str,
) -> list[IoEntry]:
    """Index entries for the declared outputs a stage's manifest actually contains."""
    row = connection.execute(
        "SELECT artifacts_json FROM artifact_manifests WHERE id=?", (manifest_id,)
    ).fetchone()
    listed = (
        {} if row is None else {a["relative_path"]: a for a in json.loads(row["artifacts_json"])}
    )
    entries = []
    for declared in stage.outputs:
        item = listed.get(declared.path)
        if item is None:
            continue
        entries.append(
            IoEntry(
                name=declared.name,
                kind=declared.kind,
                media_type=declared.media_type,
                sha256=item["sha256"],
                size_bytes=int(item["size"]),
                data_class=OUTPUT_DATA_CLASS,
                manifest_id=manifest_id,
                path=declared.path,
                lineage_node_id=lineage_node(connection, campaign["project_id"], item["sha256"]),
            )
        )
    return entries


def stage_io(
    connection: sqlite3.Connection, campaign_id: str, attempt_id: str | None = None
) -> list[dict[str, Any]]:
    """The index rows of a campaign (or one attempt), oldest first."""
    sql = "SELECT * FROM campaign_stage_io WHERE campaign_id=?"
    params: tuple = (campaign_id,)
    if attempt_id is not None:
        sql += " AND attempt_id=?"
        params = (campaign_id, attempt_id)
    return [dict(r) for r in connection.execute(sql + " ORDER BY created_at, id", params)]


def _refuse(source: Any, why: str) -> Conflict:
    return Conflict(f"Artifact input {source.name!r} does not resolve: {why}.")


def resolve_artifact(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], source: Any
) -> ResolvedArtifact:
    """Resolve a pinned ``artifact`` input or raise Conflict (409).

    It resolves only to an output row of the same org and project, from a succeeded attempt
    that the source campaign's terminal receipt lists, whose manifest is custody-released
    and ``retain_quarantined`` and holds a file with that sha256. Anything else is refused.
    It needs the source receipt's v2 ``io`` list, so a campaign that finished before schema
    v65 (a v1 receipt) can never be an artifact source.
    """
    row = connection.execute(
        """SELECT io.*, a.status AS attempt_status, a.attempt AS attempt_number,
        a.manifest_digest AS attempt_manifest_digest
        FROM campaign_stage_io io JOIN campaign_stage_attempts a ON a.id = io.attempt_id
        WHERE io.org_id=? AND io.project_id=? AND io.campaign_id=? AND io.stage_key=?
        AND io.direction='output' AND io.name=? AND io.sha256=?""",
        (
            campaign["org_id"],
            campaign["project_id"],
            source.campaign_id,
            source.stage,
            source.output,
            source.sha256,
        ),
    ).fetchone()
    if row is None:
        raise _refuse(source, "no such output in this project")
    if row["attempt_status"] != "succeeded":
        raise _refuse(source, "the producing attempt did not succeed")
    receipt_row = connection.execute(
        "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", (source.campaign_id,)
    ).fetchone()
    if receipt_row is None:
        raise _refuse(source, "the source campaign has no terminal receipt")
    receipt = json.loads(receipt_row["receipt_json"])
    listed = False
    for stage in receipt.get("stages", []):
        if stage.get("stage_key") != source.stage:
            continue
        for attempt in stage.get("attempts", []):
            if attempt.get("attempt") == row["attempt_number"] and attempt.get("status") == (
                "succeeded"
            ):
                listed = any(
                    item.get("direction") == "output"
                    and item.get("name") == source.output
                    and item.get("sha256") == source.sha256
                    for item in attempt.get("io", [])
                )
    if not listed:
        raise _refuse(source, "the source campaign receipt does not list it")
    manifest = connection.execute(
        "SELECT * FROM artifact_manifests WHERE id=?", (row["manifest_id"],)
    ).fetchone()
    if manifest is None or manifest["project_id"] != campaign["project_id"]:
        raise _refuse(source, "its manifest is not in this project")
    disposition = connection.execute(
        "SELECT * FROM artifact_dispositions WHERE manifest_id=? ORDER BY revision DESC LIMIT 1",
        (manifest["id"],),
    ).fetchone()
    release = connection.execute(
        "SELECT * FROM artifact_custody_releases WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest["id"],),
    ).fetchone()
    if (
        disposition is None
        or disposition["disposition"] != "retain_quarantined"
        or release is None
        or release["state"] != "released"
        or release["manifest_digest"] != manifest["manifest_digest"]
    ):
        raise _refuse(source, "its manifest is not custody-released and retained")
    files = {a["relative_path"]: a for a in json.loads(manifest["artifacts_json"])}
    item = files.get(row["path"])
    if item is None or item["sha256"] != source.sha256:
        raise _refuse(source, "the manifest holds no file with that sha256")
    return ResolvedArtifact(
        name=source.name,
        manifest_id=manifest["id"],
        manifest_digest=manifest["manifest_digest"],
        path=row["path"],
        sha256=source.sha256,
        size_bytes=int(item["size"]),
        # The platform's recorded class for the pinned row; never relabelled.
        data_class=row["data_class"],
        kind=row["kind"],
        media_type=row["media_type"],
        source_campaign_id=source.campaign_id,
        source_stage=source.stage,
        source_attempt_id=row["attempt_id"],
        lineage_node_id=row["lineage_node_id"],
    )
