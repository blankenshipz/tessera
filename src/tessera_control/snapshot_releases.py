"""Human-reviewed, destination-specific release of an immutable input snapshot."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from .snapshots import SnapshotStore
from .store import Conflict, Forbidden, NotFound, Store, StoreError, _connect, _parse_utc, utc_now


class ReleaseSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    worker_principal_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    classification: Literal["public"]
    permits_worker_transfer: Literal[True]
    rationale: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    expires_at: datetime

    @field_validator("permits_worker_transfer", mode="before")
    @classmethod
    def explicit_consent(cls, value):
        if value is not True:
            raise ValueError("Explicit transfer consent is required.")
        return value

    @field_validator("expires_at", mode="before")
    @classmethod
    def expiry_type(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("Expiry requires an ISO timestamp.")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Expiry requires a timezone.")
        return value.astimezone(UTC)


class RevokeSnapshotRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)


def create_release_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE snapshot_releases (
        id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        digest TEXT NOT NULL, classification TEXT NOT NULL CHECK(classification='public'),
        rationale TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        revision INTEGER NOT NULL, revoked_at TEXT)"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX active_snapshot_release
        ON snapshot_releases(snapshot_id, worker_principal_id) WHERE status='active'"""
    )


def _principal(connection, principal_id, org_id, audience):
    return Store._live_principal(
        connection, {"id": principal_id, "org_id": org_id}, audience=audience
    )


def authorize_snapshot(connection, actor, snapshot_id, worker_id, expected_digest):
    """Check data, human review and destination identity within the admission transaction."""
    snapshot = SnapshotStore._snapshot(connection, actor, snapshot_id)
    if snapshot["digest"] != expected_digest:
        raise Conflict("Input snapshot digest does not match the approved plan.")
    row = connection.execute(
        """SELECT * FROM snapshot_releases WHERE snapshot_id = ?
        AND worker_principal_id = ? AND status = 'active'""",
        (snapshot_id, worker_id),
    ).fetchone()
    if row is None or _parse_utc(row["expires_at"]) <= datetime.now(UTC):
        raise Forbidden("Snapshot has no live release for this worker.")
    if row["digest"] != expected_digest or row["classification"] != "public":
        raise Forbidden("Snapshot release is not eligible.")
    issuer = _principal(connection, row["actor_principal_id"], actor["org_id"], "control")
    worker = _principal(connection, worker_id, actor["org_id"], "worker")
    if issuer["role"] != "human" or worker["kind"] != "worker":
        raise Forbidden("Snapshot release identities are not eligible.")
    Store._project(connection, issuer, snapshot["project_id"])
    Store._project(connection, worker, snapshot["project_id"])
    SnapshotStore._verify_retained(connection, actor, snapshot_id)
    return dict(row)


class SnapshotReleaseStore(SnapshotStore):
    def release_snapshot(self, actor, snapshot_id, key, digest, payload):
        try:
            body = ReleaseSnapshot.model_validate(payload)
        except ValidationError:
            raise StoreError("Invalid snapshot release request.") from None

        def command(connection, live):
            if live["role"] != "human":
                raise Forbidden("A human must classify and release source data.")
            snapshot = self._snapshot(connection, live, snapshot_id)
            if snapshot["digest"] != body.expected_digest:
                raise Conflict("Snapshot digest changed from the reviewed basis.")
            self._verify_retained(connection, live, snapshot_id)
            worker = _principal(connection, body.worker_principal_id, live["org_id"], "worker")
            self._project(connection, worker, snapshot["project_id"])
            now = datetime.now(UTC)
            expiry = body.expires_at
            if not now < expiry <= now + timedelta(hours=24):
                raise StoreError("Snapshot release expiry must be within 24 hours.")
            for identity in (live, worker):
                if identity["expires_at"] and expiry > _parse_utc(identity["expires_at"]):
                    raise StoreError("Snapshot release cannot outlive its identities.")
            if connection.execute(
                """SELECT 1 FROM snapshot_releases WHERE snapshot_id = ?
                AND worker_principal_id = ? AND status='active'""",
                (snapshot_id, worker["id"]),
            ).fetchone():
                raise Conflict("Revoke the previous release before replacing it.")
            ident, timestamp = "release_" + uuid.uuid4().hex, utc_now()
            result = {
                "id": ident,
                "snapshot_id": snapshot_id,
                "worker_principal_id": worker["id"],
                "actor_principal_id": live["id"],
                "digest": body.expected_digest,
                "classification": body.classification,
                "rationale": body.rationale,
                "expires_at": expiry.isoformat(),
                "created_at": timestamp,
                "status": "active",
                "revision": 1,
                "revoked_at": None,
            }
            connection.execute(
                """INSERT INTO snapshot_releases VALUES
                (:id,:snapshot_id,:worker_principal_id,:actor_principal_id,:digest,:classification,
                 :rationale,:expires_at,:created_at,:status,:revision,:revoked_at)""",
                result,
            )
            self._event(
                connection,
                live,
                snapshot["project_id"],
                None,
                "workspace.snapshot_released",
                1,
                {
                    "snapshot_id": snapshot_id,
                    "release_id": ident,
                    "worker_principal_id": worker["id"],
                    "digest": body.expected_digest,
                    "classification": body.classification,
                    "expires_at": expiry.isoformat(),
                },
            )
            return 201, result

        return self._mutate(
            actor, key, "POST", f"/snapshots/{snapshot_id}/releases", digest, command
        )

    def revoke_snapshot_release(self, actor, release_id, key, digest, expected_revision):
        def command(connection, live):
            release = connection.execute(
                "SELECT * FROM snapshot_releases WHERE id = ?", (release_id,)
            ).fetchone()
            if release is None:
                raise NotFound("Snapshot release was not found.")
            snapshot = self._snapshot(connection, live, release["snapshot_id"])
            if live["role"] != "human":
                raise Forbidden("A human must revoke a data release.")
            if release["revision"] != expected_revision or release["status"] != "active":
                raise Conflict("Snapshot release revision is stale.")
            now = utc_now()
            connection.execute(
                """UPDATE snapshot_releases SET status='revoked', revision=revision+1,
                revoked_at=? WHERE id=?""",
                (now, release_id),
            )
            self._event(
                connection,
                live,
                snapshot["project_id"],
                None,
                "workspace.snapshot_release_revoked",
                expected_revision + 1,
                {"release_id": release_id, "snapshot_id": snapshot["id"]},
            )
            return 200, {
                **dict(release),
                "status": "revoked",
                "revoked_at": now,
                "revision": expected_revision + 1,
            }

        return self._mutate(
            actor, key, "POST", f"/snapshot-releases/{release_id}/revoke", digest, command
        )

    def list_snapshot_releases(self, actor, snapshot_id, offset=0, limit=100):
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._snapshot(connection, live, snapshot_id)
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM snapshot_releases WHERE snapshot_id=?
                    ORDER BY created_at,id LIMIT ? OFFSET ?""",
                    (snapshot_id, limit, offset),
                )
            ]
        finally:
            connection.close()
