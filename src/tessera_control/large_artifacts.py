"""Worker upload of large content-addressed artifacts (chunked, bounded memory).

An output larger than the legacy single-BLOB path is received as fixed ``CHUNK_BYTES`` chunks,
stored in ``snapshot_chunks`` under the artifact id (the same chunk store as chunked input
snapshots, ADR 0037). The upload record is a ``snapshot_chunk_uploads`` row whose manifest
carries ``kind: "artifact"``. ``complete`` streams every chunk, checks each chunk hash, then
one sha256 over the whole artifact, and only then inserts the ordinary quarantined
``artifacts`` row. That row keeps an empty ``content`` and the real ``size``; the bytes stay in
the chunk store. Everything after quarantine (manifest, custody release, composition into a
stage input) uses the same identity, fences and receipts as a small artifact.

Authority is unchanged: the lease, grant, epoch, operation and declared-path fences are the
legacy ones; the plan's ``max_output_bytes`` and the operator's ``ArtifactLimits`` bound the
size; the chunk store's per-project cap counts these bytes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import artifact_limits as limits
from . import snapshot_chunks as chunks
from .artifact_limits import (
    ABSOLUTE_MAX_ARTIFACT_BYTES,
    CHUNK_BYTES,
    operator_limits,
    part_bytes_for,
)
from .artifacts import ArtifactStore, EmptyUploadRequest, PortablePath, _metadata
from .snapshot_chunks import (
    check_chunk_metadata,
    collect_expired_uploads,
    enforce_project_chunk_cap,
    verify_chunk_content,
)
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

UPLOAD_TTL = timedelta(hours=24)
_HEX64 = r"^[0-9a-f]{64}$"
MAX_CHUNK_BASE64_CHARS = 4 * ((CHUNK_BYTES + 2) // 3) + 16


class BeginLargeUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_epoch: int = Field(strict=True, ge=1, le=2**63 - 1)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    relative_path: PortablePath
    sha256: str = Field(pattern=_HEX64, min_length=64, max_length=64)
    size: int = Field(strict=True, ge=1, le=ABSOLUTE_MAX_ARTIFACT_BYTES)


class PutLargeChunk(BaseModel):
    """One storage chunk, whole (``part_offset`` omitted), or one part of it.

    ``sha256`` is always the digest of the WHOLE chunk. A part carries ``part_offset`` and the
    bytes from there; parts arrive in order and the chunk is stored once the last one makes its
    planned length and the whole-chunk digest matches (ADR 0050). Parts exist so a worker whose
    path bounds a request below one 4 MiB chunk can still upload."""

    model_config = ConfigDict(extra="forbid", strict=True)

    content_base64: str = Field(min_length=1, max_length=MAX_CHUNK_BASE64_CHARS)
    sha256: str = Field(pattern=_HEX64, min_length=64, max_length=64)
    part_offset: int | None = Field(default=None, strict=True, ge=0)


def is_large_artifact(row: sqlite3.Row | dict[str, Any]) -> bool:
    """A quarantined artifact whose bytes live in the chunk store, not in ``content``."""
    return row["size"] > 0 and isinstance(row["content"], bytes) and len(row["content"]) == 0


def artifact_entry(relative_path: str, size: int, sha256: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"relative_path": relative_path, "size": size}
    if sha256 is not None:
        entry["sha256"] = sha256
    return entry


def chunk_fingerprint(connection: sqlite3.Connection, artifact_id: str) -> str:
    """Hash over (seq, size, sha256) of every stored chunk; reads no chunk content."""
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT seq, size, sha256 FROM snapshot_chunks WHERE owner_id=? ORDER BY seq",
        (artifact_id,),
    ):
        digest.update(f"{row['seq']}:{row['size']}:{row['sha256']}\n".encode())
    return digest.hexdigest()


def verify_artifact_bytes(
    connection: sqlite3.Connection, artifact_id: str, relative_path: str, size: int, sha256: str
) -> None:
    """Stream every chunk (one at a time) and the whole-artifact sha256; raise on any mismatch."""
    verify_chunk_content(connection, artifact_id, [artifact_entry(relative_path, size, sha256)])


MAX_PARTIAL_CHUNKS_LISTED = 64


def _public(
    upload: sqlite3.Row,
    received: int,
    next_seq: int,
    partial: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    manifest = json.loads(upload["manifest_json"])
    return {
        "id": upload["id"],
        "lease_id": manifest["lease_id"],
        "epoch": manifest["epoch"],
        "operation_id": manifest["operation_id"],
        "relative_path": manifest["relative_path"],
        "sha256": manifest["sha256"],
        "size": manifest["size"],
        "status": upload["status"],
        "chunk_bytes": chunks.CHUNK_BYTES,
        "chunk_count": len(chunks.chunk_plan(manifest["entries"])),
        # What a worker needs to size its requests: control's body limit and the largest part of a
        # chunk that fits it with headroom (equal to chunk_bytes unless the operator lowered it).
        "max_request_bytes": operator_limits().max_request_bytes,
        "part_bytes": part_bytes_for(operator_limits().max_request_bytes),
        "expires_at": upload["expires_at"],
        "received_chunks": received,
        "next_seq": next_seq,
        # Chunks whose parts have not all arrived, with the bytes control holds from the start
        # of each: a restarted worker continues there instead of resending confirmed parts.
        "partial_chunks": partial or [],
    }


_STRIPES = tuple(threading.Lock() for _ in range(64))
MAX_HUMAN_SCAN = 512  # plausible candidates examined per human reclaim; repeat for more


def _stripe(principal_id: str, key: str) -> threading.Lock:
    """One of 64 locks chosen by (principal, key): same-key calls serialize, others rarely do."""
    return _STRIPES[int(hashlib.sha256(f"{principal_id}:{key}".encode()).hexdigest(), 16) % 64]


RECLAIM_EVENT = "control.large_artifact_reclaimed"
MAX_SWEEP_PER_BEGIN = 2  # artifacts reclaimed per large-upload begin
MAX_SWEEP_SCAN = 32  # candidates examined (oldest first) per large-upload begin
MAX_HUMAN_RECLAIM = 64  # artifacts per human reclaim request; repeat the call for more
_CANDIDATES = """SELECT a.id, a.relative_path, a.size, a.project_id, a.task_id, a.lease_id,
    a.created_at, (SELECT revision FROM tasks WHERE id=a.task_id) AS task_revision
    FROM artifacts a WHERE a.project_id=? AND length(a.content)=0 AND a.size>0
    AND (EXISTS (SELECT 1 FROM snapshot_chunks c WHERE c.owner_id=a.id)
         OR EXISTS (SELECT 1 FROM snapshot_chunk_uploads u WHERE u.id=a.id))"""


def candidate_by_id(connection: sqlite3.Connection, artifact_id: str) -> dict[str, Any] | None:
    """The reclaim candidate row for one artifact that still holds chunk bytes, else None."""
    row = connection.execute(
        _CANDIDATES.replace("a.project_id=?", "a.id=?"), (artifact_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _lease_is_live(connection: sqlite3.Connection, lease_id: str, now: datetime) -> bool:
    """Whether the artifact's execution may still be producing or finishing its outputs.

    A lease before its hard expiry, or a grant that is not reconciled and not yet expired,
    is live: its worker can still post the manifest for outputs it has already completed.
    """
    row = connection.execute(
        """SELECT l.hard_expires_at, g.status, g.expires_at FROM execution_leases l
        JOIN execution_grants g ON g.id = l.grant_id WHERE l.id=?""",
        (lease_id,),
    ).fetchone()
    if row is None:
        return False
    return _parse_utc(row["hard_expires_at"]) > now or (
        row["status"] != "reconciled" and _parse_utc(row["expires_at"]) > now
    )


def reclaim_reason(
    connection: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    now: datetime,
    orphan_seconds: int,
) -> str | None:
    """Why this large artifact's bytes may be freed now, or ``None`` to keep them.

    - ``unmanifested``: listed by no manifest, its lease is no longer live (see
      ``_lease_is_live``), and it is older than the operator's orphan age.
    - ``rejected``: every manifest listing it has a latest disposition of ``reject``. The
      reject itself reclaims, but a crash between its commit and the reclaim leaves bytes
      that this finishes.
    An artifact in a manifest awaiting a decision, or whose lease may still post its manifest,
    is never reclaimed. The reclaim re-runs this inside its first write transaction.
    """
    manifests = [
        m["id"]
        for m in connection.execute(
            """SELECT m.id FROM artifact_manifests m WHERE m.project_id=?
            AND EXISTS (SELECT 1 FROM json_each(m.artifacts_json) j
                        WHERE json_extract(j.value, '$.id') = ?)""",
            (row["project_id"], row["id"]),
        )
    ]
    if not manifests:
        if _lease_is_live(connection, row["lease_id"], now):
            return None
        if (now - _parse_utc(row["created_at"])).total_seconds() >= orphan_seconds:
            return "unmanifested"
        return None
    for manifest_id in manifests:
        latest = connection.execute(
            "SELECT disposition FROM artifact_dispositions WHERE manifest_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (manifest_id,),
        ).fetchone()
        if latest is None or latest["disposition"] != "reject":
            return None
    return "rejected"


# SQL prefilter for the sweep. ``reclaim_reason`` stays the authority (and is re-run under the
# write lock); this only keeps artifacts that cannot be eligible out of the scan window, so
# retained, undecided or live-lease artifacts never crowd newer orphans out of a bounded scan.
_LISTED = """EXISTS (SELECT 1 FROM json_each(m.artifacts_json) j
                WHERE json_extract(j.value, '$.id') = a.id)"""
_SWEEPABLE = f"""AND (
    (a.created_at <= :cutoff
     AND NOT EXISTS (SELECT 1 FROM artifact_manifests m
                     WHERE m.project_id = a.project_id AND {_LISTED})
     AND NOT EXISTS (SELECT 1 FROM execution_leases l
                     JOIN execution_grants g ON g.id = l.grant_id
                     WHERE l.id = a.lease_id
                     AND (l.hard_expires_at > :now
                          OR (g.status != 'reconciled' AND g.expires_at > :now))))
    OR EXISTS (SELECT 1 FROM artifact_manifests m
               WHERE m.project_id = a.project_id AND {_LISTED}
               AND (SELECT d.disposition FROM artifact_dispositions d
                    WHERE d.manifest_id = m.id ORDER BY d.revision DESC LIMIT 1) = 'reject')
)"""


def reclaimable(
    connection: sqlite3.Connection,
    project_id: str,
    now: datetime,
    orphan_seconds: int,
    limit: int | None = None,
    scan: int | None = None,
) -> list[dict[str, Any]]:
    """Large artifacts of a project whose bytes can be freed, with the reason (a snapshot).

    ``limit`` caps how many are returned and ``scan`` how many plausible candidates (oldest
    first, after the SQL prefilter) are examined, so one request does bounded work however many
    large artifacts a project holds, and ineligible ones never starve eligible ones.
    """
    found: list[dict[str, Any]] = []
    query = (
        _CANDIDATES.replace("a.project_id=?", "a.project_id=:project")
        + " "
        + _SWEEPABLE
        + " ORDER BY a.created_at, a.id"
        + ("" if scan is None else " LIMIT :scan")
    )
    parameters: dict[str, Any] = {
        "project": project_id,
        "now": _utc_string(now),
        "cutoff": _utc_string(now - timedelta(seconds=orphan_seconds)),
    }
    if scan is not None:
        parameters["scan"] = scan
    for row in connection.execute(query, parameters).fetchall():
        reason = reclaim_reason(connection, row, now, orphan_seconds)
        if reason is not None:
            found.append({**dict(row), "reason": reason})
            if limit is not None and len(found) >= limit:
                break
    return found


def control_is_active(connection: sqlite3.Connection) -> bool:
    """False while control is held after a restore: a restored database can lack manifests
    that committed later, so artifacts in use would look orphaned. Nothing is reclaimed then."""
    state = connection.execute("SELECT state FROM control_recovery WHERE singleton=1").fetchone()
    return state is not None and state["state"] == "active"


def reclaim_artifact_bytes(
    store: Any,
    live: dict[str, Any],
    artifact: dict[str, Any],
    reason: str,
    *,
    trigger: str,
    orphan_seconds: int,
    batch: int = 64,
    after_batch: Any = None,
) -> bool:
    """Free one large artifact's chunk rows in short transactions; return whether this call
    claimed the reclaim (and so recorded it).

    Every batch transaction first checks that control is not held after a restore (raising
    ``Forbidden``). The first one also re-checks ``reclaim_reason`` under the write lock, so an
    artifact that gained a manifest, a decision or a live lease since the scan is left alone
    (the check and the first delete are atomic). Once bytes start to go, a manifest can no
    longer commit (its chunk metadata check fails) and a retain is refused. The quarantine row
    stays as evidence.

    The reclaim is claimed by deleting the artifact's completed upload record, which exactly
    one of any number of interleaved calls can do; that call alone appends the
    ``control.large_artifact_reclaimed`` event, so sweeps, replays and repeated rejects record
    it once. ``after_batch`` is a test seam called after each committed batch.
    """
    connection = _connect(store.database_path)
    first = True
    try:
        while True:
            connection.execute("BEGIN IMMEDIATE")
            if not control_is_active(connection):
                raise Forbidden("Control state is held after restore; reclaiming is disabled.")
            if first:
                first = False
                still = reclaim_reason(connection, artifact, datetime.now(UTC), orphan_seconds)
                if still is None or still != reason:
                    connection.execute("ROLLBACK")
                    return False
            removed = connection.execute(
                "DELETE FROM snapshot_chunks WHERE rowid IN "
                "(SELECT rowid FROM snapshot_chunks WHERE owner_id=? LIMIT ?)",
                (artifact["id"], batch),
            ).rowcount
            claimed = False
            if removed == 0:
                claimed = (
                    connection.execute(
                        "DELETE FROM snapshot_chunk_uploads WHERE id=? AND status='completed'",
                        (artifact["id"],),
                    ).rowcount
                    == 1
                )
                if claimed:
                    revision = connection.execute(
                        "SELECT revision FROM tasks WHERE id=?", (artifact["task_id"],)
                    ).fetchone()
                    store._event(
                        connection,
                        live,
                        artifact["project_id"],
                        artifact["task_id"],
                        RECLAIM_EVENT,
                        revision["revision"] if revision is not None else 1,
                        {
                            "artifact_id": artifact["id"],
                            "relative_path": artifact["relative_path"],
                            "size": artifact["size"],
                            "reason": reason,
                            "trigger": trigger,
                        },
                    )
            connection.execute("COMMIT")
            if removed == 0:
                return claimed
            if after_batch is not None:
                after_batch()
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


class _ChunkRejected(Conflict):
    """A refused chunk whose partial storage was removed; the removal is committed."""


# Hashing a multi-gigabyte upload takes longer than a worker's request deadline (at most 20 s,
# the lease minus a renewal interval). ``complete`` therefore waits only this long for the
# verification and otherwise answers 202 "verifying"; the worker asks again with the same key
# and a repeat joins the verification already running instead of starting another (ADR 0050).
VERIFY_WAIT_SECONDS = 8.0
VERIFY_RETRY_AFTER_SECONDS = 5
# A finished answer is kept (it is guarded by the chunk fingerprint) at most this long, so an
# upload that never completes (expired, aborted) cannot pin it for the life of the process.
FLIGHT_KEEP_SECONDS = 3600.0
# Whole-file hashes running at once across uploads; the rest wait for a slot (their callers
# answer 202 meanwhile).
MAX_CONCURRENT_VERIFICATIONS = 4
_HASH_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_VERIFICATIONS)


class _Flight:
    """One whole-file verification: its future and when it started and finished."""

    def __init__(self) -> None:
        self.future: Future = Future()
        self.started = time.monotonic()
        self.finished: float | None = None


_FLIGHTS: dict[tuple[str, str, str], _Flight] = {}
_FLIGHTS_LOCK = threading.Lock()


def _forget(key: tuple[str, str, str], flight: _Flight) -> None:
    """Drop ``flight`` only if it is still the one under ``key`` (never a newer one)."""
    with _FLIGHTS_LOCK:
        if _FLIGHTS.get(key) is flight:
            _FLIGHTS.pop(key, None)


def _sweep_flights(now: float) -> None:
    """Forget finished answers older than ``FLIGHT_KEEP_SECONDS`` (called under the lock)."""
    for key in [
        k
        for k, flight in _FLIGHTS.items()
        if flight.finished is not None and now - flight.finished > FLIGHT_KEEP_SECONDS
    ]:
        _FLIGHTS.pop(key, None)


class _VerificationPending(Exception):  # noqa: N818 - a control-flow signal, not an error
    """The whole-file verification is still running; the caller answers 202."""


class LargeArtifactStore(ArtifactStore):
    """Receive and quarantine artifacts too large for the single-BLOB path."""

    def reclaim_orphans(
        self,
        principal: dict[str, Any],
        project_id: str,
        *,
        audience: str = "worker",
        limit: int | None = None,
        scan: int | None = None,
    ) -> list[dict[str, Any]]:
        """Free reclaimable large artifacts of a project; returns what this call reclaimed.

        Opportunistic at a large-upload begin (worker audience, a few artifacts per call) and
        available to a human for the project. Deletion runs in short batches, never under one
        long lock, and each artifact is re-checked atomically before its first delete.
        """
        orphan_seconds = operator_limits().orphan_artifact_seconds
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, principal, audience=audience)
            if audience == "control":
                self._project(connection, live, project_id)
                if live["role"] != "human":
                    raise Forbidden("A human must reclaim large artifacts.")
            if not control_is_active(connection):
                raise Forbidden("Control state is held after restore; reclaiming is disabled.")
            found = reclaimable(
                connection, project_id, datetime.now(UTC), orphan_seconds, limit, scan
            )
        finally:
            connection.close()
        trigger = "human" if audience == "control" else "worker_begin"
        freed = [
            artifact
            for artifact in found
            if reclaim_artifact_bytes(
                self,
                live,
                artifact,
                artifact["reason"],
                trigger=trigger,
                orphan_seconds=orphan_seconds,
            )
        ]
        return [
            {
                "id": a["id"],
                "relative_path": a["relative_path"],
                "size": a["size"],
                "reason": a["reason"],
            }
            for a in freed
        ]

    def reclaim_for_human(
        self, actor: dict[str, Any], project_id: str, key: str, digest: str
    ) -> tuple[int, dict[str, Any]]:
        """The human reclaim request, idempotent per key.

        A key that already has a recorded answer returns it **without sweeping again**; only a
        new key reclaims, and the answer listing exactly what it reclaimed is recorded. The
        lookup and the sweep run under one process lock so concurrent calls with the same key
        in this process cannot both reclaim; two control processes racing on one fresh key may
        each reclaim part of the set, and the recorded answer is the first writer's.
        """
        path = f"/projects/{project_id}/large-artifacts/reclaim"
        with _stripe(actor.get("id", ""), key):
            if self._recorded_for(actor, key, path, audience="control"):
                return self._mutate(
                    actor, key, "POST", path, digest, lambda connection, live: (200, {})
                )  # a replay: _mutate returns the stored answer (or refuses a changed request)
            freed = self.reclaim_orphans(
                actor, project_id, audience="control", limit=MAX_HUMAN_RECLAIM, scan=MAX_HUMAN_SCAN
            )
            return self._mutate(
                actor,
                key,
                "POST",
                path,
                digest,
                lambda connection, live: (200, {"reclaimed": freed}),
            )

    def _recorded(self, worker: dict[str, Any], key: str, path: str) -> bool:
        return self._recorded_for(worker, key, path, audience="worker")

    def _recorded_for(
        self, principal: dict[str, Any], key: str, path: str, *, audience: str
    ) -> bool:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, principal, audience=audience)
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

    def _sweep_for_lease(self, worker: dict[str, Any], lease_id: str, key: str, path: str) -> None:
        """Housekeeping at begin: bounded, skipped on a replay, and never blocks an upload.

        Any failure here is ignored; the begin that follows validates the request itself and
        reports its own errors.
        """
        try:
            if self._recorded(worker, key, path):
                return
            connection = _connect(self.database_path)
            try:
                actor = self._live_principal(connection, worker, audience="worker")
                _lease, grant = self._grant_for_upload(connection, actor, lease_id)
                project_id = grant["project_id"]
            finally:
                connection.close()
            self.reclaim_orphans(worker, project_id, limit=MAX_SWEEP_PER_BEGIN, scan=MAX_SWEEP_SCAN)
        except Exception:  # noqa: BLE001 - housekeeping must never block an upload
            return

    def _upload_row(
        self, connection: sqlite3.Connection, actor: dict[str, Any], lease_id: str, upload_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT * FROM snapshot_chunk_uploads WHERE id=? AND actor_principal_id=?
            AND json_extract(manifest_json, '$.kind') = 'artifact'
            AND json_extract(manifest_json, '$.lease_id') = ?""",
            (upload_id, actor["id"], lease_id),
        ).fetchone()
        if row is None:
            raise NotFound("Large artifact upload was not found.")
        return row

    @staticmethod
    def _progress(connection: sqlite3.Connection, upload_id: str) -> tuple[int, int]:
        """Complete chunks received and the first missing seq. A chunk whose parts have not all
        arrived (its stored length is below its planned length) is not received yet."""
        total = connection.execute(
            "SELECT json_extract(manifest_json, '$.size') FROM snapshot_chunk_uploads WHERE id=?",
            (upload_id,),
        ).fetchone()
        seqs = [
            row["seq"]
            for row in connection.execute(
                "SELECT seq, size FROM snapshot_chunks WHERE owner_id=? ORDER BY seq", (upload_id,)
            )
            if total is None
            or row["size"] == min(chunks.CHUNK_BYTES, total[0] - row["seq"] * chunks.CHUNK_BYTES)
        ]
        next_seq = 0
        for seq in seqs:
            if seq != next_seq:
                break
            next_seq += 1
        return len(seqs), next_seq

    @staticmethod
    def _partials(connection: sqlite3.Connection, upload_id: str) -> list[dict[str, int]]:
        """Partially stored chunks (stored length below the planned length), lowest seq first."""
        total = connection.execute(
            "SELECT json_extract(manifest_json, '$.size') FROM snapshot_chunk_uploads WHERE id=?",
            (upload_id,),
        ).fetchone()
        if total is None:
            return []
        found: list[dict[str, int]] = []
        for row in connection.execute(
            "SELECT seq, size FROM snapshot_chunks WHERE owner_id=? ORDER BY seq", (upload_id,)
        ):
            planned = min(chunks.CHUNK_BYTES, total[0] - row["seq"] * chunks.CHUNK_BYTES)
            if row["size"] < planned:
                found.append({"seq": row["seq"], "received_bytes": row["size"]})
                if len(found) >= MAX_PARTIAL_CHUNKS_LISTED:
                    break
        return found

    def _status_body(
        self, connection: sqlite3.Connection, actor: dict[str, Any], lease_id: str, upload_id: str
    ) -> dict[str, Any]:
        self._grant_for_upload(connection, actor, lease_id)
        row = self._upload_row(connection, actor, lease_id, upload_id)
        return _public(
            row, *self._progress(connection, upload_id), self._partials(connection, upload_id)
        )

    # -- begin ----------------------------------------------------------------------

    def begin_upload(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = BeginLargeUpload.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid large artifact upload request.") from error
        path = f"/worker/leases/{lease_id}/large-artifact-uploads"
        self._sweep_for_lease(worker, lease_id, key, path)

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            limit = self._validate_declaration(connection, grant, lease, body)
            if body["size"] <= limits.LEGACY_INLINE_BYTES:
                # Small files take the inline path, which places them in the project store.
                raise Conflict("The chunked upload is only for files above the inline bound.")
            upload_id = self._artifact_id(lease_id, body["relative_path"])
            # Expired uploads (this one included) are reclaimed first, so a stalled upload
            # can be restarted and no longer holds lease or project budget.
            collect_expired_uploads(connection, grant["project_id"])
            existing = connection.execute(
                "SELECT * FROM snapshot_chunk_uploads WHERE id=?", (upload_id,)
            ).fetchone()
            if (
                existing is not None
                and existing["status"] == "uploading"
                and json.loads(existing["manifest_json"])["epoch"] != lease["epoch"]
            ):
                # The lease was re-fenced: bytes sent under the old epoch are discarded.
                connection.execute("DELETE FROM snapshot_chunks WHERE owner_id=?", (upload_id,))
                connection.execute("DELETE FROM snapshot_chunk_uploads WHERE id=?", (upload_id,))
                existing = None
            if existing is not None:
                declared = json.loads(existing["manifest_json"])
                if any(
                    declared[field] != body[name]
                    for field, name in (
                        ("epoch", "expected_epoch"),
                        ("operation_id", "operation_id"),
                        ("sha256", "sha256"),
                        ("size", "size"),
                    )
                ):
                    raise Conflict("Artifact path is immutable.")
                return 200, _public(
                    existing,
                    *self._progress(connection, upload_id),
                    self._partials(connection, upload_id),
                )
            if (
                connection.execute(
                    "SELECT 1 FROM artifacts WHERE lease_id=? AND relative_path=?",
                    (lease_id, body["relative_path"]),
                ).fetchone()
                is not None
            ):
                raise Conflict("Artifact path is immutable.")
            if self._reserved_bytes(connection, lease_id) + body["size"] > limit:
                raise Conflict("Artifact output budget is exceeded.")
            enforce_project_chunk_cap(connection, grant["project_id"], body["size"])
            manifest = {
                "kind": "artifact",
                "lease_id": lease_id,
                "epoch": lease["epoch"],
                "operation_id": body["operation_id"],
                "relative_path": body["relative_path"],
                "sha256": body["sha256"],
                "size": body["size"],
                "total_bytes": body["size"],
                "entries": [artifact_entry(body["relative_path"], body["size"])],
            }
            now = utc_now()
            connection.execute(
                """INSERT INTO snapshot_chunk_uploads
                (id, project_id, expected_digest, manifest_json, status, actor_principal_id,
                 created_at, completed_at, expires_at)
                VALUES (?, ?, ?, ?, 'uploading', ?, ?, NULL, ?)""",
                (
                    upload_id,
                    grant["project_id"],
                    body["sha256"],
                    json.dumps(manifest),
                    actor["id"],
                    now,
                    _utc_string(datetime.now(UTC) + UPLOAD_TTL),
                ),
            )
            created = connection.execute(
                "SELECT * FROM snapshot_chunk_uploads WHERE id=?", (upload_id,)
            ).fetchone()
            metadata = _public(created, 0, 0)
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                "worker.artifact_upload_begun",
                grant["revision"],
                {**metadata, "storage": "chunked"},
            )
            return 201, metadata

        return self._mutate(
            worker,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=lambda connection, actor, _: self._grant_for_upload(
                connection, actor, lease_id
            ),
        )

    # -- status ---------------------------------------------------------------------

    def upload_status(
        self, worker: dict[str, Any], lease_id: str, upload_id: str, key: str, digest: str
    ) -> tuple[int, dict[str, Any]]:
        path = f"/worker/leases/{lease_id}/large-artifact-uploads/{upload_id}/status"
        fresh: dict[str, Any] = {}

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            fresh["body"] = self._status_body(connection, actor, lease_id, upload_id)
            return 200, fresh["body"]

        def replay_guard(connection, actor, _):
            fresh["body"] = self._status_body(connection, actor, lease_id, upload_id)

        status, _ = self._mutate(
            worker,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=replay_guard,
        )
        return status, fresh["body"]

    # -- chunks ---------------------------------------------------------------------

    def put_chunk(
        self,
        worker: dict[str, Any],
        lease_id: str,
        upload_id: str,
        seq: int,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Naturally idempotent write: no idempotency record, so no bytes are replayed or kept."""
        try:
            body = PutLargeChunk.model_validate(payload)
        except ValidationError as error:
            raise StoreError("Invalid large artifact chunk request.") from error
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise StoreError("Chunk content is not valid base64.") from None
        if len(content) > chunks.CHUNK_BYTES:
            raise StoreError("Artifact chunk exceeds its bound.")
        path = f"/worker/leases/{lease_id}/large-artifact-uploads/{upload_id}/chunks/{seq}"
        connection = _connect(self.database_path)
        actor: dict[str, Any] | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            actor = self._live_principal(connection, worker, audience="worker")
            state = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            if state is None or state["state"] != "active":
                raise Forbidden("Control state is held after restore; mutations are disabled.")
            lease, _grant = self._grant_for_upload(connection, actor, lease_id)
            upload = self._upload_row(connection, actor, lease_id, upload_id)
            manifest = json.loads(upload["manifest_json"])
            if upload["status"] != "uploading" or manifest["epoch"] != lease["epoch"]:
                raise Conflict("Large artifact upload is not accepting chunks.")
            if _parse_utc(upload["expires_at"]) <= datetime.now(UTC):
                raise Conflict("Large artifact upload has expired.")
            plan = chunks.chunk_plan(manifest["entries"])
            if not 0 <= seq < len(plan):
                raise NotFound("Chunk sequence is not part of the upload plan.")
            planned = plan[seq]
            status, stored = self._store_part(connection, upload_id, planned, seq, body, content)
            received, next_seq = self._progress(connection, upload_id)
            connection.execute("COMMIT")
            return status, {
                "seq": seq,
                "received_chunks": received,
                "next_seq": next_seq,
                "chunk_received_bytes": stored,
            }
        except Exception as error:
            if connection.in_transaction:
                connection.execute("COMMIT" if isinstance(error, _ChunkRejected) else "ROLLBACK")
            if isinstance(error, Forbidden) and actor is not None:
                self._record_denial(actor, "POST", path)
            raise
        finally:
            connection.close()

    @staticmethod
    def _store_part(
        connection: sqlite3.Connection,
        upload_id: str,
        planned: dict[str, Any],
        seq: int,
        body: PutLargeChunk,
        content: bytes,
    ) -> tuple[int, int]:
        """Store a whole chunk or one in-order part of it; ``(http status, bytes stored)``.

        A whole chunk (no ``part_offset``) must be exactly the planned length with the declared
        digest. A part appends at the stored length (a repeat of stored bytes is a no-op, a gap or
        different bytes is refused) and the chunk is final once it reaches its planned length, when
        the digest of the whole chunk must equal the declared one or the chunk is dropped."""
        size = planned["size"]
        if body.part_offset is None:
            if len(content) != size or hashlib.sha256(content).hexdigest() != body.sha256:
                raise Conflict("Chunk size or sha256 does not match the upload plan.")
            offset = 0
        else:
            offset = body.part_offset
            if not 0 < len(content) <= size - offset:
                raise Conflict("Chunk part does not fit the planned chunk.")
        existing = connection.execute(
            "SELECT sha256, content FROM snapshot_chunks WHERE owner_id=? AND seq=?",
            (upload_id, seq),
        ).fetchone()
        if existing is not None and existing["sha256"] != body.sha256:
            raise Conflict("Chunk was already uploaded with different bytes.")
        held = b"" if existing is None else bytes(existing["content"])
        if offset > len(held):
            raise Conflict("Chunk parts must arrive in order from the start.")
        overlap = held[offset : offset + len(content)]
        if overlap != content[: len(overlap)]:
            raise Conflict("Chunk was already uploaded with different bytes.")
        merged = held + content[len(overlap) :]
        if len(merged) == len(held):
            return 200, len(held)  # every byte was already stored
        if len(merged) == size and hashlib.sha256(merged).hexdigest() != body.sha256:
            # The assembled chunk is wrong (a corrupt part, a wrong digest): drop what was stored
            # for it, committed, so the worker can resend the chunk from its first part.
            connection.execute(
                "DELETE FROM snapshot_chunks WHERE owner_id=? AND seq=?", (upload_id, seq)
            )
            raise _ChunkRejected("Chunk size or sha256 does not match the upload plan.")
        if existing is None:
            connection.execute(
                "INSERT INTO snapshot_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    upload_id,
                    seq,
                    planned["relative_path"],
                    planned["offset"],
                    len(merged),
                    body.sha256,
                    merged,
                ),
            )
        else:
            connection.execute(
                "UPDATE snapshot_chunks SET size=?, content=? WHERE owner_id=? AND seq=?",
                (len(merged), merged, upload_id, seq),
            )
        return 201, len(merged)

    # -- complete -------------------------------------------------------------------

    def complete_upload(
        self, worker: dict[str, Any], lease_id: str, upload_id: str, key: str, digest: str
    ) -> tuple[int, dict[str, Any]]:
        path = f"/worker/leases/{lease_id}/large-artifact-uploads/{upload_id}/complete"
        # The multi-gigabyte hash runs on a read connection, outside the write lock. The
        # write transaction below then binds to exactly the chunk set that was hashed.
        try:
            verified = self._verify_stored(worker, lease_id, upload_id)
        except _VerificationPending:
            # Not recorded as the answer to this key: the worker repeats the same request.
            return 202, {
                "id": upload_id,
                "status": "verifying",
                "retry_after_seconds": VERIFY_RETRY_AFTER_SECONDS,
            }

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            lease, grant = self._grant_for_upload(connection, actor, lease_id)
            row = self._upload_row(connection, actor, lease_id, upload_id)
            manifest = json.loads(row["manifest_json"])
            existing = connection.execute(
                """SELECT id, project_id, task_id, grant_id, lease_id, worker_principal_id,
                epoch, operation_id, relative_path, sha256, size, created_at, status,
                classification, late FROM artifacts WHERE lease_id=? AND relative_path=?""",
                (lease_id, manifest["relative_path"]),
            ).fetchone()
            if row["status"] == "completed":
                if existing is None:
                    raise Conflict("Completed artifact upload is inconsistent.")
                return 200, _metadata(existing)
            if verified is None or manifest["epoch"] != lease["epoch"]:
                raise Conflict("Artifact upload size or checksum does not match.")
            check_chunk_metadata(connection, upload_id, manifest["entries"])
            if chunk_fingerprint(connection, upload_id) != verified:
                raise Conflict("Artifact chunks changed during verification.")
            body = {
                "expected_epoch": manifest["epoch"],
                "operation_id": manifest["operation_id"],
                "relative_path": manifest["relative_path"],
                "sha256": manifest["sha256"],
            }
            connection.execute(
                "UPDATE snapshot_chunk_uploads SET status='completed', completed_at=? WHERE id=?",
                (utc_now(), upload_id),
            )
            return 201, self._insert_artifact(
                connection, actor, lease, grant, body, b"", size=manifest["size"]
            )

        return self._mutate(
            worker,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=lambda connection, actor, _: self._grant_for_upload(
                connection, actor, lease_id
            ),
        )

    def _verify_stored(self, worker: dict[str, Any], lease_id: str, upload_id: str) -> str | None:
        """Hash the stored chunks on a read-only pass; ``None`` when the upload is not ready.

        One verification runs per upload and chunk set: a repeated ``complete`` (a worker whose
        deadline passed asks again) waits on it instead of hashing the file again. Raises
        ``_VerificationPending`` when it is still running after ``VERIFY_WAIT_SECONDS``."""
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, worker, audience="worker")
            self._grant_for_upload(connection, actor, lease_id)
            row = self._upload_row(connection, actor, lease_id, upload_id)
            if row["status"] == "completed":
                with _FLIGHTS_LOCK:  # nothing left to verify: forget this upload's answers
                    for stale in [
                        k for k in _FLIGHTS if k[:2] == (str(self.database_path), upload_id)
                    ]:
                        _FLIGHTS.pop(stale, None)
                return ""  # replayed completion; the command returns the recorded artifact
            manifest = json.loads(row["manifest_json"])
            fingerprint = chunk_fingerprint(connection, upload_id)
        finally:
            connection.close()
        key = (str(self.database_path), upload_id, fingerprint)
        with _FLIGHTS_LOCK:
            _sweep_flights(time.monotonic())
            flight = _FLIGHTS.get(key)
            if flight is None:
                flight = _FLIGHTS[key] = _Flight()
                threading.Thread(
                    target=self._verification_job,
                    args=(flight, upload_id, manifest, fingerprint),
                    daemon=True,
                ).start()
        try:
            result = flight.future.result(timeout=VERIFY_WAIT_SECONDS)
        except FutureTimeout:
            raise _VerificationPending from None
        except BaseException:
            _forget(key, flight)
            raise
        if result is None:  # not ready or mismatched: a later call hashes afresh
            _forget(key, flight)
        return result

    def _verification_job(
        self, flight: _Flight, upload_id: str, manifest: dict[str, Any], fingerprint: str
    ) -> None:
        with _HASH_SLOTS:
            connection = _connect(self.database_path)
            try:
                try:
                    verify_artifact_bytes(
                        connection,
                        upload_id,
                        manifest["relative_path"],
                        manifest["size"],
                        manifest["sha256"],
                    )
                except Conflict:
                    outcome: str | None = None
                else:
                    outcome = fingerprint
                flight.finished = time.monotonic()
                flight.future.set_result(outcome)
            except BaseException as error:
                flight.finished = time.monotonic()
                flight.future.set_exception(error)
            finally:
                connection.close()


__all__ = [
    "BeginLargeUpload",
    "EmptyUploadRequest",
    "LargeArtifactStore",
    "PutLargeChunk",
    "artifact_entry",
    "chunk_fingerprint",
    "is_large_artifact",
    "verify_artifact_bytes",
]
