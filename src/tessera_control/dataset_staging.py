"""Stage human-released collected outputs as a local immutable input snapshot (ADR 0034 s4).

The only new authority here is a human composing already custody-released bytes into a
``source_snapshots`` row, which is always ``private_derived`` (ADR 0039).  Worker transfer
still needs a separate human ``ReleaseSnapshot``; nothing is accepted or sent to a worker
by staging.

Atomicity note: ``SnapshotStore.adopt_snapshot`` keeps its transaction closure private, so
staging cannot compose the staging event into that call without editing snapshots.py.  The
snapshot rows and the ``workspace.snapshot_adopted`` event are therefore written here with the
same validation (``_validate_capture``) and shapes as ``adopt_snapshot``, in ONE transaction
together with the ``snapshot.staged_from_artifacts`` event and the idempotency record.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifact_custody import ArtifactCustodyStore
from .data_class import PRIVATE_DERIVED, record_snapshot_data_class
from .snapshot_chunks import (
    collect_expired_uploads,
    enforce_project_chunk_cap,
    insert_chunked_snapshot_rows,
    insert_chunks,
    validate_chunked_manifest,
)
from .snapshots import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES, SnapshotStore, _validate_capture
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    canonical_digest,
    utc_now,
)
from .workspace import CapturedWorkspace, InventoryEntry, WorkspaceInventory, inventory_digest

# ADR 0037: above these totals staging yields a chunked snapshot instead of failing.
CHUNKED_STAGING_BYTES = MAX_SNAPSHOT_BYTES
CHUNKED_STAGING_FILES = MAX_SNAPSHOT_FILES
STAGED_EVENT = "snapshot.staged_from_artifacts"
Digest = Field(pattern=r"^[a-f0-9]{64}$")


class StageArtifactSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_id: str = Field(min_length=1, max_length=200)
    expected_manifest_digest: str = Digest
    relative_paths: list[str] | None = Field(default=None, min_length=1, max_length=10_000)
    expected_snapshot_digest: str = Digest


def staged_inventory(manifest_id: str, entries: list[dict[str, Any]]) -> WorkspaceInventory:
    """Canonical inventory for staged files; clients compute the expected digest with this."""
    ordered = sorted(entries, key=lambda item: item["relative_path"])
    return WorkspaceInventory(
        root=f"artifact-manifest:{manifest_id}",
        entries=tuple(
            InventoryEntry(item["relative_path"], item["size"], item["sha256"]) for item in ordered
        ),
        exclusions=(),
        total_bytes=sum(item["size"] for item in ordered),
    )


def staging_for_snapshot(connection: sqlite3.Connection, snapshot_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT cursor, created_at, actor_principal_id, payload_json FROM events "
        "WHERE event_type=? AND json_extract(payload_json,'$.snapshot_id')=? "
        "ORDER BY cursor LIMIT 1",
        (STAGED_EVENT, snapshot_id),
    ).fetchone()
    if row is None:
        return None
    return {
        **json.loads(row["payload_json"]),
        "event_cursor": row["cursor"],
        "staged_at": row["created_at"],
        "staged_by_principal_id": row["actor_principal_id"],
    }


class DatasetStagingStore(SnapshotStore):
    def __init__(self, state_dir, custody: ArtifactCustodyStore | None = None) -> None:
        super().__init__(state_dir)
        self.custody = custody or ArtifactCustodyStore(state_dir)

    def stage(
        self, actor: dict[str, Any], project_id: str, key: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = StageArtifactSnapshot.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact staging request.") from error
        custody = self.custody

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._project(connection, live, project_id)
            if live["role"] != "human":
                raise Forbidden("A human must stage collected outputs as an input snapshot.")
            manifest = custody._manifest(connection, live, body["manifest_id"])
            if manifest["project_id"] != project_id:
                raise NotFound("Artifact manifest was not found.")
            if manifest["manifest_digest"] != body["expected_manifest_digest"]:
                raise Conflict("Artifact manifest digest does not match.")
            release = custody._current_release(connection, manifest["id"])
            disposition = custody._latest_disposition(connection, manifest["id"])
            if (
                release is None
                or release["state"] != "released"
                or release["manifest_digest"] != manifest["manifest_digest"]
                or disposition is None
                or disposition["disposition"] != "retain_quarantined"
            ):
                raise Conflict("An active custody release of this exact manifest is required.")
            listed = {
                entry["relative_path"]: entry for entry in json.loads(manifest["artifacts_json"])
            }
            wanted = body["relative_paths"]
            if wanted is None:
                wanted = sorted(listed)
            if len(set(wanted)) != len(wanted) or any(path not in listed for path in wanted):
                raise StoreError("Selected paths must be unique members of the manifest.")
            if (
                sum(listed[path]["size"] for path in wanted) > CHUNKED_STAGING_BYTES
                or len(wanted) > CHUNKED_STAGING_FILES
            ):
                return self._stage_chunked(
                    connection, live, project_id, body, manifest, listed, wanted
                )
            files, entries = [], []
            for path in wanted:
                item = listed[path]
                _artifact, content = custody._read_artifact(connection, live, manifest, item["id"])
                if (
                    hashlib.sha256(content).hexdigest() != item["sha256"]
                    or len(content) != item["size"]
                ):
                    raise Conflict("Released artifact bytes no longer match the manifest.")
                files.append((path, content))
                entries.append(
                    {
                        "artifact_id": item["id"],
                        "relative_path": path,
                        "sha256": item["sha256"],
                        "size": item["size"],
                    }
                )
            inventory = staged_inventory(manifest["id"], entries)
            capture = CapturedWorkspace(inventory, tuple(sorted(files)))
            digest = body["expected_snapshot_digest"]
            if inventory_digest(inventory) != digest:
                raise Conflict("Staged snapshot digest does not match the expected digest.")
            snapshot_manifest = _validate_capture(capture, digest)
            ident, now = "snapshot_" + uuid.uuid4().hex, utc_now()
            connection.execute(
                "INSERT INTO source_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (ident, project_id, digest, json.dumps(snapshot_manifest), now, live["id"]),
            )
            connection.executemany(
                "INSERT INTO snapshot_files VALUES (?, ?, ?)",
                ((ident, path, content) for path, content in capture.files),
            )
            # ADR 0039: built from run outputs, so always private_derived; no API
            # field can declare it public (StageArtifactSnapshot forbids extras).
            record = record_snapshot_data_class(connection, live, ident, derived=True, now=now)
            self._event(
                connection,
                live,
                project_id,
                None,
                "workspace.snapshot_adopted",
                1,
                {
                    "snapshot_id": ident,
                    "digest": digest,
                    "file_count": len(capture.files),
                    "total_bytes": inventory.total_bytes,
                    "provenance": "imported_unverified",
                    "classification": "unclassified",
                    "data_class": record,
                },
            )
            operation_id = _operation_id(connection, manifest["id"])
            staging = {
                "snapshot_id": ident,
                "snapshot_digest": digest,
                "manifest_id": manifest["id"],
                "manifest_digest": manifest["manifest_digest"],
                "operation_id": operation_id,
                "files": sorted(entries, key=lambda e: e["relative_path"]),
                "data_class": PRIVATE_DERIVED,
            }
            self._event(connection, live, project_id, manifest["task_id"], STAGED_EVENT, 1, staging)
            return 201, {
                "id": ident,
                "project_id": project_id,
                "digest": digest,
                "manifest": snapshot_manifest,
                "created_at": now,
                "actor_principal_id": live["id"],
                "transfer_authorized": False,
                "staging": staging,
                "data_class": record,
            }

        def replay_guard(connection: sqlite3.Connection, live: dict[str, Any], _: Any) -> None:
            self._project(connection, live, project_id)
            if live["role"] != "human":
                raise Forbidden("A human must stage collected outputs as an input snapshot.")

        return self._mutate(
            actor,
            key,
            "POST",
            f"/projects/{project_id}/artifact-snapshots",
            canonical_digest(body),
            command,
            replay_guard=replay_guard,
        )

    def _stage_chunked(
        self,
        connection: sqlite3.Connection,
        live: dict[str, Any],
        project_id: str,
        body: dict[str, Any],
        manifest: Any,
        listed: dict[str, Any],
        wanted: list[str],
    ) -> tuple[int, dict[str, Any]]:
        """ADR 0037: staged totals above the legacy bounds become a chunked snapshot.

        Same transaction as the small path: snapshot + chunk rows, both events, replay record.
        Bytes are streamed one artifact at a time; the legacy 16 MiB path is unchanged.
        """
        entries = [
            {
                "artifact_id": listed[path]["id"],
                "relative_path": path,
                "sha256": listed[path]["sha256"],
                "size": listed[path]["size"],
            }
            for path in wanted
        ]
        inventory = staged_inventory(manifest["id"], entries)
        digest = body["expected_snapshot_digest"]
        if inventory_digest(inventory) != digest:
            raise Conflict("Staged snapshot digest does not match the expected digest.")
        snapshot_manifest = validate_chunked_manifest(asdict(inventory), digest)
        ident, now = "snapshot_" + uuid.uuid4().hex, utc_now()
        collect_expired_uploads(connection, project_id)
        enforce_project_chunk_cap(connection, project_id, snapshot_manifest["total_bytes"])
        insert_chunked_snapshot_rows(
            connection, ident, project_id, digest, snapshot_manifest, live["id"], now
        )
        by_path = {entry["relative_path"]: entry for entry in entries}
        seq = 0
        for item in snapshot_manifest["entries"]:
            _artifact, content = self.custody._read_artifact(
                connection, live, manifest, by_path[item["relative_path"]]["artifact_id"]
            )
            if (
                hashlib.sha256(content).hexdigest() != item["sha256"]
                or len(content) != item["size"]
            ):
                raise Conflict("Released artifact bytes no longer match the manifest.")
            seq = insert_chunks(connection, ident, item["relative_path"], content, seq)
        record = record_snapshot_data_class(connection, live, ident, derived=True, now=now)
        self._event(
            connection,
            live,
            project_id,
            None,
            "workspace.snapshot_adopted",
            1,
            {
                "snapshot_id": ident,
                "digest": digest,
                "file_count": len(entries),
                "total_bytes": inventory.total_bytes,
                "provenance": "imported_unverified",
                "classification": "unclassified",
                "chunked": True,
                "chunk_count": seq,
                "data_class": record,
            },
        )
        staging = {
            "snapshot_id": ident,
            "snapshot_digest": digest,
            "manifest_id": manifest["id"],
            "manifest_digest": manifest["manifest_digest"],
            "operation_id": _operation_id(connection, manifest["id"]),
            "files": sorted(entries, key=lambda e: e["relative_path"]),
            "data_class": PRIVATE_DERIVED,
        }
        self._event(connection, live, project_id, manifest["task_id"], STAGED_EVENT, 1, staging)
        return 201, {
            "id": ident,
            "project_id": project_id,
            "digest": digest,
            "chunked": True,
            "file_count": len(entries),
            "total_bytes": inventory.total_bytes,
            "chunk_count": seq,
            "created_at": now,
            "actor_principal_id": live["id"],
            "transfer_authorized": False,
            "staging": staging,
            "data_class": record,
        }

    def get_staging(self, actor: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._snapshot(connection, live, snapshot_id)
            record = staging_for_snapshot(connection, snapshot_id)
            if record is None:
                raise NotFound("Snapshot was not staged from collected outputs.")
            return record
        finally:
            connection.close()


def _operation_id(connection: sqlite3.Connection, manifest_id: str) -> str | None:
    row = connection.execute(
        "SELECT operation_id FROM artifact_manifests WHERE id=?", (manifest_id,)
    ).fetchone()
    return row["operation_id"] if row is not None else None
