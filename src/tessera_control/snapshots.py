"""Local, immutable input capture; no worker transfer or execution authority."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict
from typing import Any

from .approval_trust import decision_provenance, require_decision_actor
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
from .workspace import CapturedWorkspace, inventory_digest

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_FILES = 1000


def create_snapshot_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE source_snapshots (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        digest TEXT NOT NULL, manifest_json TEXT NOT NULL,
        created_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id)
        )"""
    )
    connection.execute(
        "CREATE INDEX snapshots_by_project ON source_snapshots(project_id, created_at, id)"
    )
    connection.execute(
        """CREATE TABLE snapshot_files (
        snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        relative_path TEXT NOT NULL, content BLOB NOT NULL,
        PRIMARY KEY (snapshot_id, relative_path)
        )"""
    )


def _validate_capture(capture: CapturedWorkspace, expected_digest: str) -> dict[str, Any]:
    inventory = capture.inventory
    if inventory_digest(inventory) != expected_digest:
        raise Conflict("Workspace changed since inventory review; inventory and review it again.")
    if (
        len(inventory.entries) > MAX_SNAPSHOT_FILES
        or len(capture.files) != len(inventory.entries)
        or len(inventory.exclusions) > 10000
    ):
        raise StoreError("Snapshot file count exceeds bounds or does not match its manifest.")
    files = dict(capture.files)
    if (
        len(files) != len(capture.files)
        or len({entry.relative_path for entry in inventory.entries}) != len(inventory.entries)
        or set(files) != {entry.relative_path for entry in inventory.entries}
    ):
        raise StoreError("Snapshot paths must be unique.")
    total = 0
    for entry in inventory.entries:
        path = entry.relative_path
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or "\0" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise StoreError("Snapshot contains an unsafe relative path.")
        payload = files.get(path)
        if (
            not isinstance(payload, bytes)
            or len(payload) != entry.size
            or hashlib.sha256(payload).hexdigest() != entry.sha256
            or entry.provenance != "imported_unverified"
            or entry.classification != "unclassified"
        ):
            raise StoreError("Snapshot bytes or provenance do not match the inventory.")
        total += len(payload)
        if total > MAX_SNAPSHOT_BYTES:
            raise StoreError("Snapshot exceeds its byte bound.")
    if total != inventory.total_bytes:
        raise StoreError("Snapshot total does not match the inventory.")
    manifest = asdict(inventory)
    if len(json.dumps(manifest).encode()) > 1024 * 1024:
        raise StoreError("Snapshot manifest exceeds its byte bound.")
    return json.loads(json.dumps(manifest))


class SnapshotStore(Store):
    def adopt_snapshot(
        self,
        actor: dict[str, Any],
        project_id: str,
        key: str,
        expected_digest: str,
        capture: CapturedWorkspace,
        *,
        replay_guard: Any | None = None,
        data_class: Any = None,
        code_approval: Any = None,
        require_decision_label: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        """Commit reviewed bytes, manifest, audit and replay atomically, locally.

        ``data_class`` is an optional ``DataClassDeclaration`` (or its dict); without
        one the snapshot is ``private_derived`` (ADR 0039). ``code_approval`` is an
        optional ``CodeApproval`` of exact paths as private code (amendment 1).
        ``require_decision_label`` makes a bare owner credential (no automation label) a refusal,
        like other decisions; the adoption event records who stood behind it either way.
        """
        from .code_approval import parse_code_approval, record_code_approval
        from .data_class import (
            parse_declaration,
            record_snapshot_data_class,
            refuse_relabelled_files,
        )

        try:
            declaration = parse_declaration(data_class)
            approval = parse_code_approval(code_approval)
        except ValueError as error:
            raise StoreError(str(error)) from None
        manifest = _validate_capture(capture, expected_digest)

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._project(connection, live, project_id)
            if live["role"] != "human":
                raise Forbidden("A human must confirm local snapshot adoption.")
            if require_decision_label:
                require_decision_actor(live, forbidden=Forbidden)
            ident, now = "snapshot_" + uuid.uuid4().hex, utc_now()
            connection.execute(
                "INSERT INTO source_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (ident, project_id, expected_digest, json.dumps(manifest), now, live["id"]),
            )
            connection.executemany(
                "INSERT INTO snapshot_files VALUES (?, ?, ?)",
                ((ident, path, content) for path, content in capture.files),
            )
            record = record_snapshot_data_class(connection, live, ident, declaration, now=now)
            code = record_code_approval(connection, live, ident, approval, now=now)
            refuse_relabelled_files(connection, live["org_id"], ident)
            adopted = {
                "snapshot_id": ident,
                "digest": expected_digest,
                "file_count": len(capture.files),
                "total_bytes": capture.inventory.total_bytes,
                "provenance": "imported_unverified",
                "classification": "unclassified",
                "data_class": record,
                **decision_provenance(live),
            }
            if code is not None:
                adopted["code_approval"] = code
            self._event(
                connection, live, project_id, None, "workspace.snapshot_adopted", 1, adopted
            )
            response = {
                "id": ident,
                "project_id": project_id,
                "digest": expected_digest,
                "manifest": manifest,
                "created_at": now,
                "actor_principal_id": live["id"],
                "transfer_authorized": False,
                "data_class": record,
            }
            if code is not None:
                response["code_approval"] = code
            return 201, response

        return self._mutate(
            actor,
            key,
            "POST",
            f"/projects/{project_id}/snapshots",
            expected_digest
            if declaration is None and approval is None
            else canonical_digest(
                {"digest": expected_digest}
                | (
                    {}
                    if declaration is None
                    else {"data_class": declaration.model_dump(mode="json")}
                )
                | ({} if approval is None else {"code_approval": approval.model_dump(mode="json")})
            ),
            command,
            replay_guard=replay_guard,
        )

    @staticmethod
    def _snapshot(connection, live, snapshot_id):
        row = connection.execute(
            """SELECT s.id, s.project_id, s.digest, s.created_at, s.actor_principal_id,
            substr(s.manifest_json, 1, 4194305) AS manifest_json,
            EXISTS(SELECT 1 FROM snapshot_chunk_uploads u
                   WHERE u.id = s.id AND u.status = 'completed') AS chunked
            FROM source_snapshots s JOIN projects p ON p.id = s.project_id
            WHERE s.id = ? AND p.org_id = ?""",
            (snapshot_id, live["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Snapshot was not found.")
        Store._project(connection, live, row["project_id"])
        return row

    @staticmethod
    def _response(row):
        response = dict(row)
        encoded = response.pop("manifest_json")
        chunked = bool(response.pop("chunked", 0))
        # ADR 0037: chunked snapshots carry larger manifests; legacy bounds are unchanged.
        max_manifest = 4 * 1024 * 1024 if chunked else 1024 * 1024
        max_files = 10_000 if chunked else MAX_SNAPSHOT_FILES
        max_bytes = 2 * 1024**3 if chunked else MAX_SNAPSHOT_BYTES
        try:
            if not isinstance(encoded, str) or len(encoded.encode()) > max_manifest:
                raise ValueError("manifest size")
            manifest = json.loads(encoded)
            if (
                not isinstance(manifest, dict)
                or set(manifest) != {"root", "entries", "exclusions", "total_bytes"}
                or not isinstance(manifest["root"], str)
                or not isinstance(manifest["entries"], list)
                or len(manifest["entries"]) > max_files
                or not isinstance(manifest["exclusions"], list)
                or len(manifest["exclusions"]) > 10000
                or type(manifest["total_bytes"]) is not int
                or not 0 <= manifest["total_bytes"] <= max_bytes
            ):
                raise ValueError("manifest structure")
            paths = set()
            total = 0
            for entry in manifest["entries"]:
                if (
                    not isinstance(entry, dict)
                    or set(entry)
                    != {"relative_path", "size", "sha256", "provenance", "classification"}
                    or not isinstance(entry["relative_path"], str)
                    or not isinstance(entry["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                    or type(entry["size"]) is not int
                    or not 0 <= entry["size"] <= max_bytes
                    or entry["provenance"] != "imported_unverified"
                    or entry["classification"] != "unclassified"
                ):
                    raise ValueError("manifest entry")
                paths.add(entry["relative_path"])
                total += entry["size"]
            if len(paths) != len(manifest["entries"]) or total != manifest["total_bytes"]:
                raise ValueError("manifest file set")
            for exclusion in manifest["exclusions"]:
                if (
                    not isinstance(exclusion, dict)
                    or set(exclusion) != {"relative_path", "reason"}
                    or not all(isinstance(value, str) for value in exclusion.values())
                ):
                    raise ValueError("manifest exclusion")
            canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            if hashlib.sha256(canonical.encode()).hexdigest() != response["digest"]:
                raise ValueError("manifest digest")
        except (ValueError, TypeError, KeyError, RecursionError):
            raise Conflict("Snapshot manifest integrity check failed.") from None
        response["manifest"] = manifest
        response["transfer_authorized"] = False
        return response

    def get_snapshot(self, actor: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            from .code_approval import snapshot_code_approval
            from .data_class import snapshot_data_class

            response = self._response(self._snapshot(connection, live, snapshot_id))
            response["data_class"] = snapshot_data_class(connection, snapshot_id)
            code = snapshot_code_approval(connection, snapshot_id)
            if code is not None:
                response["code_approval"] = code
            return response
        finally:
            connection.close()

    def list_snapshots(self, actor, project_id, offset=0, limit=100):
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._project(connection, live, project_id)
            return [
                {key: row[key] for key in ("id", "digest", "created_at", "actor_principal_id")}
                for row in connection.execute(
                    """SELECT id, digest, created_at, actor_principal_id FROM source_snapshots
                    WHERE project_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?""",
                    (project_id, limit, offset),
                )
            ]
        finally:
            connection.close()

    @staticmethod
    def _authorized_snapshot(
        connection,
        actor,
        snapshot_id,
        worker_id,
        expected_digest,
        *,
        verify_retained=True,
        allow_private_derived=False,
    ):
        from .snapshot_releases import authorize_snapshot

        return authorize_snapshot(
            connection,
            actor,
            snapshot_id,
            worker_id,
            expected_digest,
            verify_retained=verify_retained,
            allow_private_derived=allow_private_derived,
        )

    @staticmethod
    def _verify_retained(connection, live, snapshot_id, *, full: bool = False) -> dict[str, Any]:
        row = SnapshotStore._snapshot(connection, live, snapshot_id)
        response = SnapshotStore._response(row)
        manifest = response["manifest"]
        entries = manifest["entries"]
        if row["chunked"]:
            return SnapshotStore._verify_chunked(connection, snapshot_id, row, manifest, full)
        if len(entries) > MAX_SNAPSHOT_FILES:
            raise Conflict("Snapshot file count exceeds its bound.")
        count = connection.execute(
            "SELECT count(*) FROM snapshot_files WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()[0]
        if count != len(entries):
            raise Conflict("Snapshot file set integrity check failed.")
        total = 0
        for entry in entries:
            content = connection.execute(
                """SELECT CASE WHEN length(content) = ? AND length(content) <= ?
                THEN content ELSE NULL END FROM snapshot_files
                WHERE snapshot_id = ? AND relative_path = ?""",
                (entry["size"], MAX_SNAPSHOT_BYTES, snapshot_id, entry["relative_path"]),
            ).fetchone()
            if content is None or content[0] is None or len(content[0]) != entry["size"]:
                raise Conflict("Snapshot file integrity check failed.")
            total += len(content[0])
            if (
                total > MAX_SNAPSHOT_BYTES
                or hashlib.sha256(content[0]).hexdigest() != entry["sha256"]
            ):
                raise Conflict("Snapshot file integrity check failed.")
        if total != manifest["total_bytes"]:
            raise Conflict("Snapshot size integrity check failed.")
        return {
            "id": snapshot_id,
            "digest": row["digest"],
            "integrity": "verified",
            "file_count": count,
            "total_bytes": total,
            "transfer_authorized": False,
            "provenance": "imported_unverified",
            "classification": "unclassified",
        }

    @staticmethod
    def _verify_chunked(connection, snapshot_id, row, manifest, full) -> dict[str, Any]:
        """Chunked: metadata check by default (no gigabyte re-hash); ``full`` re-hashes bytes."""
        from .snapshot_chunks import check_chunk_metadata, verify_chunk_content

        entries = manifest["entries"]
        if connection.execute(
            "SELECT count(*) FROM snapshot_files WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()[0]:
            raise Conflict("Snapshot file set integrity check failed.")
        (verify_chunk_content if full else check_chunk_metadata)(connection, snapshot_id, entries)
        return {
            "id": snapshot_id,
            "digest": row["digest"],
            "integrity": "verified" if full else "metadata_verified",
            "chunked": True,
            "file_count": len(entries),
            "total_bytes": manifest["total_bytes"],
            "transfer_authorized": False,
            "provenance": "imported_unverified",
            "classification": "unclassified",
        }

    def verify_snapshot(self, actor, snapshot_id) -> dict[str, Any]:
        """Rehash retained bytes locally; return metadata without exporting content."""
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            live = self._live_principal(connection, actor)
            return self._verify_retained(connection, live, snapshot_id, full=True)
        finally:
            connection.close()
