"""Composed input snapshots (ADR 0038): named sources mounted at /inputs/<name>/.

A composed snapshot places each source under its own ``<name>/`` prefix. A source is a
retained project snapshot, or a manifest that is custody-released and
``retain_quarantined`` (the dataset-staging rule). The result is an ordinary immutable
``source_snapshots`` row, legacy (<= 16 MiB / 1000 files) or chunked above that, so the
existing bounds apply. Composition is human-only and delegable; it adds no table and no
worker transfer or classification authority.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .approval_trust import CAMPAIGN_DELEGATION_KEY, require_decision_actor
from .artifact_custody import ArtifactCustodyStore
from .campaign_delegation import admit, assert_compose_sources_owned
from .data_class import record_snapshot_data_class
from .license_labels import inherit_snapshot_labels
from .snapshot_chunks import (
    UPLOAD_TTL,
    check_chunk_metadata,
    chunk_plan,
    chunk_rows_fingerprint,
    collect_expired_uploads,
    copy_file_chunks_batched,
    enforce_project_chunk_cap,
    insert_chunked_snapshot_rows,
    insert_chunks,
    is_chunked,
    source_chunk_rows,
    validate_chunked_manifest,
)
from .snapshots import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES, SnapshotStore, _validate_capture
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    utc_now,
)
from .workspace import (
    CapturedWorkspace,
    InventoryEntry,
    WorkspaceInventory,
    inventory_digest,
)

COMPOSED_EVENT = "snapshot.composed"
COMPOSED_ROOT = "composed-snapshot"
MAX_EVENT_FILES = 1000

SourceName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ComposeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: SourceName
    kind: Literal["snapshot", "manifest"]
    snapshot_id: Identifier | None = None
    expected_digest: Digest | None = None
    manifest_id: Identifier | None = None
    expected_manifest_digest: Digest | None = None
    relative_paths: list[str] | None = Field(default=None, min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def _shape(self) -> ComposeSource:
        if self.kind == "snapshot":
            if (
                self.snapshot_id is None
                or self.expected_digest is None
                or self.manifest_id is not None
                or self.expected_manifest_digest is not None
                or self.relative_paths is not None
            ):
                raise ValueError("a snapshot source needs exactly snapshot_id and expected_digest")
        elif (
            self.manifest_id is None
            or self.expected_manifest_digest is None
            or self.snapshot_id is not None
            or self.expected_digest is not None
        ):
            raise ValueError(
                "a manifest source needs manifest_id and expected_manifest_digest only"
            )
        return self


class ComposeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A campaign stage composes every input: up to 8 data inputs plus its code input (ADR 0121).
    sources: list[ComposeSource] = Field(min_length=1, max_length=9)
    expected_snapshot_digest: Digest

    @model_validator(mode="after")
    def _unique_names(self) -> ComposeSnapshot:
        names = [source.name for source in self.sources]
        if len(set(names)) != len(names):
            raise ValueError("source names must be unique")
        return self


def composed_inventory(entries: list[tuple[str, str, int, str]]) -> WorkspaceInventory:
    """Canonical inventory; entries are (prefixed_path, sha256, size, provenance).

    Clients and the campaign runner use this to compute ``expected_snapshot_digest``.
    """
    ordered = sorted(entries, key=lambda item: item[0])
    return WorkspaceInventory(
        root=COMPOSED_ROOT,
        entries=tuple(
            InventoryEntry(path, size, sha, provenance) for path, sha, size, provenance in ordered
        ),
        exclusions=(),
        total_bytes=sum(item[2] for item in ordered),
    )


@dataclass(frozen=True)
class ChunkSource:
    """Bytes held as chunk rows (a chunked snapshot file or a large artifact).

    Composition copies these chunk by chunk, re-hashing as it goes, so memory stays at one
    chunk however large the file is.
    """

    owner_id: str
    relative_path: str
    size: int
    sha256: str
    mismatch: str


# (prefixed path, size, sha256, reader returning the verified bytes or a ChunkSource,
#  is_chunk). ``is_chunk`` is known without reading, so deciding the snapshot's shape never
# loads an inline source; each reader is called lazily, one source at a time.
_Item = tuple[str, int, str, Callable[[], "bytes | ChunkSource"], bool]


class SnapshotCompositionStore(SnapshotStore):
    def __init__(self, state_dir, custody: ArtifactCustodyStore | None = None) -> None:
        super().__init__(state_dir)
        self.custody = custody or ArtifactCustodyStore(state_dir)

    # -- source readers -------------------------------------------------------------

    def _snapshot_items(
        self, connection, live, project_id: str, source: ComposeSource
    ) -> tuple[list[_Item], dict[str, Any]]:
        row = self._snapshot(connection, live, source.snapshot_id)
        if row["project_id"] != project_id:
            raise NotFound("Snapshot was not found.")
        if row["digest"] != source.expected_digest:
            raise Conflict("Composition source snapshot digest does not match.")
        self._verify_retained(connection, live, source.snapshot_id)
        manifest = self._response(row)["manifest"]
        chunked = is_chunked(connection, source.snapshot_id)

        def reader(path: str, sha: str, size: int) -> Callable[[], bytes]:
            def read() -> bytes | ChunkSource:
                if chunked:
                    return ChunkSource(
                        source.snapshot_id,
                        path,
                        size,
                        sha,
                        "Composition source bytes no longer match the snapshot.",
                    )
                else:
                    found = connection.execute(
                        "SELECT content FROM snapshot_files "
                        "WHERE snapshot_id=? AND relative_path=?",
                        (source.snapshot_id, path),
                    ).fetchone()
                    content = found[0] if found is not None else b""
                if (
                    not isinstance(content, bytes)
                    or len(content) != size
                    or hashlib.sha256(content).hexdigest() != sha
                ):
                    raise Conflict("Composition source bytes no longer match the snapshot.")
                return content

            return read

        items = [
            (
                f"{source.name}/{entry['relative_path']}",
                entry["size"],
                entry["sha256"],
                reader(entry["relative_path"], entry["sha256"], entry["size"]),
                chunked,
            )
            for entry in manifest["entries"]
        ]
        return items, {
            "name": source.name,
            "kind": "snapshot",
            "id": source.snapshot_id,
            "digest": row["digest"],
        }

    def _manifest_items(
        self, connection, live, project_id: str, source: ComposeSource
    ) -> tuple[list[_Item], dict[str, Any]]:
        custody = self.custody
        manifest = custody._manifest(connection, live, source.manifest_id)
        if manifest["project_id"] != project_id:
            raise NotFound("Artifact manifest was not found.")
        if manifest["manifest_digest"] != source.expected_manifest_digest:
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
        listed = {entry["relative_path"]: entry for entry in json.loads(manifest["artifacts_json"])}
        wanted = source.relative_paths if source.relative_paths is not None else sorted(listed)
        if len(set(wanted)) != len(wanted) or any(path not in listed for path in wanted):
            raise StoreError("Selected paths must be unique members of the manifest.")

        larges = {
            path: custody.large_artifact(connection, live, manifest, listed[path]["id"])
            for path in wanted
        }

        def reader(item: dict[str, Any]) -> Callable[[], bytes]:
            def read() -> bytes | ChunkSource:
                large = larges[item["relative_path"]]
                if large is not None:
                    return ChunkSource(
                        item["id"],
                        large["relative_path"],
                        item["size"],
                        item["sha256"],
                        "Released artifact bytes no longer match the manifest.",
                    )
                _artifact, content = custody._read_artifact(connection, live, manifest, item["id"])
                if (
                    hashlib.sha256(content).hexdigest() != item["sha256"]
                    or len(content) != item["size"]
                ):
                    raise Conflict("Released artifact bytes no longer match the manifest.")
                return content

            return read

        items = [
            (
                f"{source.name}/{path}",
                listed[path]["size"],
                listed[path]["sha256"],
                reader(listed[path]),
                larges[path] is not None,
            )
            for path in wanted
        ]
        return items, {
            "name": source.name,
            "kind": "manifest",
            "id": manifest["id"],
            "digest": manifest["manifest_digest"],
        }

    # -- command --------------------------------------------------------------------

    def _resolve(self, connection, live, project_id: str, body: ComposeSnapshot):
        """Authorize and resolve every source; return items, descriptions and the inventory."""
        self._project(connection, live, project_id)
        if live["role"] != "human":
            raise Forbidden("A human must compose input snapshots.")
        require_decision_actor(live, forbidden=Forbidden)
        assert_compose_sources_owned(
            connection,
            live,
            [
                (
                    source.kind,
                    source.snapshot_id if source.kind == "snapshot" else source.manifest_id,
                )
                for source in body.sources
            ],
        )
        items: list[_Item] = []
        described: list[dict[str, Any]] = []
        for source in body.sources:
            found, info = (
                self._snapshot_items(connection, live, project_id, source)
                if source.kind == "snapshot"
                else self._manifest_items(connection, live, project_id, source)
            )
            items.extend(found)
            described.append(info)
        items.sort(key=lambda item: item[0])
        inventory = composed_inventory(
            [(path, sha, size, "imported_unverified") for path, size, sha, *_ in items]
        )
        if inventory_digest(inventory) != body.expected_snapshot_digest:
            raise Conflict("Composed snapshot digest does not match the expected digest.")
        return items, described, inventory

    @staticmethod
    def _starts(entries: list[dict[str, Any]]) -> dict[str, int]:
        """First chunk seq of every file in the composed snapshot's chunk plan."""
        starts: dict[str, int] = {}
        for item in chunk_plan(entries):
            starts.setdefault(item["relative_path"], item["seq"])
        return starts

    def _recorded(self, actor, key: str, path: str) -> bool:
        """Whether this idempotency key already has a recorded response (a replay)."""
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            return (
                connection.execute(
                    """SELECT 1 FROM idempotency WHERE principal_id=? AND org_id=?
                    AND idempotency_key=? AND method='POST' AND path=?""",
                    (live["id"], live["org_id"], key, path),
                ).fetchone()
                is not None
            )
        finally:
            connection.close()

    def _stage(
        self, actor, project_id: str, body: ComposeSnapshot, key: str, digest: str
    ) -> dict[str, Any] | None:
        """Copy chunk-stored sources into the new snapshot id before the write transaction.

        Copying gigabytes inside the command's write lock would stall every control write, so
        it happens here in short batches. The command then binds to exactly what was copied
        (chunk fingerprints) and inserts only the small remainder. Returns ``None`` when no
        source is chunk-stored. An unfinished staging upload expires and is collected like any
        other, and a failed compose deletes it at once.

        A campaign-delegated call is admitted here exactly as the command admits it (the
        same read-only check against its recorded approval and intent), so the staging read
        sees the same delegated actor; a non-delegated actor still needs a human_session
        credential or an automation label, and an actor whose delegation does not verify is
        refused before any copy.
        """
        reader = _connect(self.database_path)
        try:
            reader.execute("BEGIN")
            live = self._live_principal(reader, actor)
            marker = actor.get(CAMPAIGN_DELEGATION_KEY)
            if marker is not None:
                admit(
                    reader,
                    live,
                    marker,
                    "POST",
                    f"/projects/{project_id}/composed-snapshots",
                    digest,
                    key,
                )
            items, _described, inventory = self._resolve(reader, live, project_id, body)
            if not any(item[4] for item in items):
                return None
            expected = body.expected_snapshot_digest
            manifest = validate_chunked_manifest(asdict(inventory), expected)
            starts = self._starts(manifest["entries"])
            chunk_sources = {
                path: read() for path, _size, _sha, read, is_chunk in items if is_chunk
            }
        finally:
            reader.close()
        ident = "snapshot_" + uuid.uuid4().hex
        writer = _connect(self.database_path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            collect_expired_uploads(writer, project_id)
            enforce_project_chunk_cap(writer, project_id, manifest["total_bytes"])
            writer.execute(
                """INSERT INTO snapshot_chunk_uploads
                (id, project_id, expected_digest, manifest_json, status, actor_principal_id,
                 created_at, completed_at, expires_at)
                VALUES (?, ?, ?, ?, 'uploading', ?, ?, NULL, ?)""",
                (
                    ident,
                    project_id,
                    expected,
                    json.dumps(manifest),
                    live["id"],
                    utc_now(),
                    _utc_string(datetime.now(UTC) + UPLOAD_TTL),
                ),
            )
            writer.execute("COMMIT")
        except BaseException:
            if writer.in_transaction:
                writer.execute("ROLLBACK")
            raise
        finally:
            writer.close()
        fingerprints: dict[str, str] = {}
        try:
            for path, source in chunk_sources.items():
                _next, fingerprints[path] = copy_file_chunks_batched(
                    self.database_path,
                    source.owner_id,
                    source.relative_path,
                    ident,
                    path,
                    source.size,
                    source.sha256,
                    starts[path],
                    source.mismatch,
                )
        except BaseException:
            self._discard_staged(ident)
            raise
        return {"id": ident, "manifest": manifest, "fingerprints": fingerprints}

    def _discard_staged(self, ident: str) -> None:
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM snapshot_chunks WHERE owner_id=?", (ident,))
            connection.execute(
                "DELETE FROM snapshot_chunk_uploads WHERE id=? AND status='uploading'", (ident,)
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        finally:
            connection.close()

    def compose(self, actor, project_id, key, digest, payload) -> tuple[int, dict]:
        try:
            body = ComposeSnapshot.model_validate(payload)
        except ValidationError:
            raise StoreError("Invalid snapshot composition request.") from None
        path = f"/projects/{project_id}/composed-snapshots"
        staged = None
        if not self._recorded(actor, key, path):
            staged = self._stage(actor, project_id, body, key, digest)

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            items, described, inventory = self._resolve(connection, live, project_id, body)
            expected = body.expected_snapshot_digest
            total = inventory.total_bytes
            now = utc_now()
            ident = staged["id"] if staged is not None else "snapshot_" + uuid.uuid4().hex
            files = [{"path": path, "sha256": sha} for path, _size, sha, *_ in items]
            chunked = (
                staged is not None or total > MAX_SNAPSHOT_BYTES or len(items) > MAX_SNAPSHOT_FILES
            )
            if chunked:
                snapshot_manifest = validate_chunked_manifest(asdict(inventory), expected)
                if staged is not None:
                    self._bind_staged(connection, project_id, staged, snapshot_manifest, items)
                else:
                    collect_expired_uploads(connection, project_id)
                    enforce_project_chunk_cap(
                        connection, project_id, snapshot_manifest["total_bytes"]
                    )
                insert_chunked_snapshot_rows(
                    connection, ident, project_id, expected, snapshot_manifest, live["id"], now
                )
                starts = self._starts(snapshot_manifest["entries"])
                for path, _size, _sha, read, is_chunk in items:
                    if not is_chunk:  # inline sources load one at a time
                        insert_chunks(connection, ident, path, read(), starts[path])
                check_chunk_metadata(connection, ident, snapshot_manifest["entries"])
                adopted = {
                    "chunked": True,
                    "chunk_count": len(chunk_plan(snapshot_manifest["entries"])),
                }
            else:
                capture = CapturedWorkspace(
                    inventory, tuple(sorted((path, read()) for path, _s, _h, read, _c in items))
                )
                snapshot_manifest = _validate_capture(capture, expected)
                connection.execute(
                    "INSERT INTO source_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                    (ident, project_id, expected, json.dumps(snapshot_manifest), now, live["id"]),
                )
                connection.executemany(
                    "INSERT INTO snapshot_files VALUES (?, ?, ?)",
                    ((ident, path, content) for path, content in capture.files),
                )
                adopted = {}
            # ADR 0039: a composed snapshot is built from other snapshots' bytes and run
            # outputs, so it is always private_derived and never a human public declaration.
            record = record_snapshot_data_class(connection, live, ident, derived=True, now=now)
            # Licence labels follow the data (ADR 0113): the derivative carries the union of its
            # sources' labels, written with the snapshot row so no copy can shed one.
            inherited = inherit_snapshot_labels(
                connection,
                live,
                ident,
                project_id,
                [source.snapshot_id for source in body.sources if source.kind == "snapshot"],
                [source.manifest_id for source in body.sources if source.kind == "manifest"],
                now,
            )
            if inherited is not None:
                self._event(
                    connection,
                    live,
                    project_id,
                    None,
                    "snapshot.license_labels_added",
                    1,
                    inherited,
                )
            self._event(
                connection,
                live,
                project_id,
                None,
                "workspace.snapshot_adopted",
                1,
                {
                    "snapshot_id": ident,
                    "digest": expected,
                    "file_count": len(items),
                    "total_bytes": total,
                    "provenance": "imported_unverified",
                    "classification": "unclassified",
                    "data_class": record,
                    **adopted,
                },
            )
            composition = {
                "snapshot_id": ident,
                "digest": expected,
                "data_class": record,
                "sources": described,
                "files": files[:MAX_EVENT_FILES],
                "file_count": len(files),
                "files_truncated": len(files) > MAX_EVENT_FILES,
            }
            self._event(connection, live, project_id, None, COMPOSED_EVENT, 1, composition)
            return 201, {
                "id": ident,
                "project_id": project_id,
                "digest": expected,
                "manifest": snapshot_manifest,
                "created_at": now,
                "actor_principal_id": live["id"],
                "transfer_authorized": False,
                "composition": composition,
            }

        def replay_guard(connection: sqlite3.Connection, live: dict[str, Any], _: Any) -> None:
            self._project(connection, live, project_id)
            if live["role"] != "human":
                raise Forbidden("A human must compose input snapshots.")

        try:
            result = self._mutate(
                actor, key, "POST", path, digest, command, replay_guard=replay_guard
            )
        except BaseException:
            if staged is not None:
                self._discard_staged(staged["id"])
            raise
        if staged is not None and result[1].get("id") != staged["id"]:
            self._discard_staged(staged["id"])  # a concurrent replay answered with another id
        return result

    def _bind_staged(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        staged: dict[str, Any],
        manifest: dict[str, Any],
        items: list[_Item],
    ) -> None:
        """Check, under the write lock and from metadata only, that the staged copy is the
        one this request authorizes: same manifest, live upload, and sources unchanged."""
        upload = connection.execute(
            "SELECT * FROM snapshot_chunk_uploads WHERE id=? AND project_id=?",
            (staged["id"], project_id),
        ).fetchone()
        if (
            upload is None
            or upload["status"] != "uploading"
            or json.loads(upload["manifest_json"]) != manifest
            or manifest != staged["manifest"]
            or _parse_utc(upload["expires_at"]) <= datetime.now(UTC)
        ):
            raise Conflict("Staged composition bytes are no longer current; compose again.")
        starts = self._starts(manifest["entries"])
        for path, _size, _sha, read, is_chunk in items:
            if not is_chunk:
                continue
            source = read()
            now_rows = source_chunk_rows(connection, source.owner_id, source.relative_path)
            if chunk_rows_fingerprint(now_rows) != staged["fingerprints"].get(path):
                raise Conflict(source.mismatch)
            copied = connection.execute(
                "SELECT offset, size, sha256 FROM snapshot_chunks WHERE owner_id=? "
                "AND seq >= ? AND seq < ? ORDER BY seq",
                (staged["id"], starts[path], starts[path] + len(now_rows)),
            ).fetchall()
            if chunk_rows_fingerprint(copied) != staged["fingerprints"][path]:
                raise Conflict("Staged composition bytes do not match their source.")
