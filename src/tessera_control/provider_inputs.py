"""Provider input tickets (ADR 0037 s3): bearer-less, snapshot-scoped pulls for a provider workload.

A provider workload is not a Tessera principal.  The lease-holding worker obtains a random
ticket; only its sha256 is stored (never in idempotency rows, events or logs).  Issuance is
deliberately NOT idempotent.  Every pull re-checks every fence in its own transaction.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import threading
from collections import OrderedDict
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .data_class import DataClassRefused, require_public_provider_input
from .input_delivery import InputDeliveryStore
from .snapshot_chunks import CHUNK_BYTES, chunk_index, chunk_plan, is_chunked, read_chunk
from .snapshots import SnapshotStore
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

TICKET_TTL = timedelta(hours=1)
MANIFEST_PATH = "/provider-inputs/manifest"
CHUNK_PATH_TEMPLATE = "/provider-inputs/chunks/{seq}"


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lease_id: str = Field(min_length=1, max_length=200)
    expected_epoch: int = Field(ge=1, le=2**63 - 1)


class TicketInvalid(Exception):
    """Uniform pull failure; carries no detail so the 401 is not an oracle."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class ProviderInputStore(InputDeliveryStore):
    # ---- worker side -------------------------------------------------------------------
    def _operation_for_worker(
        self, connection: sqlite3.Connection, live: dict[str, Any], operation_id: str, lease_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM external_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if (
            row is None
            or row["org_id"] != live["org_id"]
            or row["worker_principal_id"] != live["id"]
        ):
            raise NotFound("External operation was not found.")
        if row["lease_id"] != lease_id:
            raise Conflict("External operation is bound to a different lease.")
        return row

    @staticmethod
    def _operation_live(operation: sqlite3.Row) -> None:
        if operation["desired_state"] != "admitted" or operation["observed_state"] == "exited":
            raise Conflict("External operation is cancelled or has exited.")

    def _worker_write(self, worker: dict[str, Any], path: str, body: dict[str, Any], command):
        try:
            request = TicketRequest.model_validate(body)
        except Exception:
            raise StoreError("Invalid provider input ticket request.") from None
        connection = _connect(self.database_path)
        live: dict[str, Any] | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            live = self._live_principal(connection, worker, audience="worker")
            recovery = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            if recovery is None or recovery["state"] != "active":
                raise Forbidden("Control state is held after restore; ticket changes are disabled.")
            result = command(connection, live, request)
            connection.execute("COMMIT")
            return result
        except Exception as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, Forbidden) and live is not None:
                self._record_denial(live, "POST", path)
            raise
        finally:
            connection.close()

    def issue_ticket(
        self, worker: dict[str, Any], operation_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Mint a fresh ticket; never idempotent, never routed through ``_mutate``."""

        def command(connection, live, request: TicketRequest) -> dict[str, Any]:
            operation = self._operation_for_worker(connection, live, operation_id, request.lease_id)
            lease, grant, _ = self._lease_fences(
                connection, live, request.lease_id, request.expected_epoch
            )
            if operation["epoch"] != request.expected_epoch:
                raise Conflict("External operation epoch is not current.")
            self._operation_live(operation)
            release = self._authorize_snapshot(
                connection,
                live,
                grant["snapshot_id"],
                live["id"],
                grant["snapshot_digest"],
                grant["snapshot_release_id"],
            )
            # ADR 0039/0040, defence in depth behind route admission: a ticket is minted
            # only for an input the route's provider instance (target provider plus the
            # operation's worker principal) is trusted to receive. A route-less
            # operation receives public data only.
            try:
                admitted, provider_id, bound_worker = require_public_provider_input(
                    connection, live["org_id"], operation_id, grant["snapshot_id"]
                )
            except DataClassRefused as error:
                raise Forbidden(f"Provider input refused: {error}.") from None
            now = self._now()
            # The soft lease (30 s, renewed by heartbeats) is re-checked on every pull; the
            # ticket's own lifetime follows the lease's hard expiry, release and one hour.
            hard_expiry = _parse_utc(lease["hard_expires_at"])
            expires = min(hard_expiry, _parse_utc(release["expires_at"]), now + TICKET_TTL)
            if expires <= now:
                raise Conflict("Provider input ticket would already be expired.")
            stamp = utc_now()
            revoked = connection.execute(
                """UPDATE provider_input_tickets SET revoked_at = ?
                WHERE operation_id = ? AND revoked_at IS NULL""",
                (stamp, operation_id),
            ).rowcount
            token = secrets.token_urlsafe(32)
            connection.execute(
                """INSERT INTO provider_input_tickets
                (token_hash, operation_id, snapshot_id, snapshot_digest, lease_id, epoch,
                 worker_principal_id, expires_at, revoked_at, chunks_served, last_served_at,
                 created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?)""",
                (
                    _hash(token),
                    operation_id,
                    grant["snapshot_id"],
                    grant["snapshot_digest"],
                    lease["id"],
                    request.expected_epoch,
                    live["id"],
                    _utc_string(expires),
                    stamp,
                ),
            )
            self._event(
                connection,
                live,
                grant["project_id"],
                grant["task_id"],
                "execution.provider_input_ticket_issued",
                request.expected_epoch,
                {
                    "operation_id": operation_id,
                    "snapshot_id": grant["snapshot_id"],
                    "snapshot_digest": grant["snapshot_digest"],
                    "expires_at": _utc_string(expires),
                    "revoked_previous": revoked,
                    "provider_id": provider_id,
                    "worker_principal_id": bound_worker,
                    "trust_policy_digest": admitted.policy_digest,
                    "admission_basis": admitted.basis,
                    "input_data_class": admitted.input_data_class,
                    "input_code": admitted.input_code,
                },
            )
            return {
                "token": token,
                "expires_at": _utc_string(expires),
                "snapshot_id": grant["snapshot_id"],
                "snapshot_digest": grant["snapshot_digest"],
                "manifest_path": MANIFEST_PATH,
                "chunk_path_template": CHUNK_PATH_TEMPLATE,
            }

        return self._worker_write(
            worker, f"/worker/external-operations/{operation_id}/input-tickets", body, command
        )

    def revoke_tickets(
        self, worker: dict[str, Any], operation_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        def command(connection, live, request: TicketRequest) -> dict[str, Any]:
            operation = self._operation_for_worker(connection, live, operation_id, request.lease_id)
            if operation["epoch"] != request.expected_epoch:
                raise Conflict("External operation epoch is not current.")
            lease = connection.execute(
                "SELECT epoch, grant_id FROM execution_leases WHERE id = ?", (request.lease_id,)
            ).fetchone()
            if lease is None or lease["epoch"] != request.expected_epoch:
                raise Conflict("Execution lease is no longer current.")
            revoked = connection.execute(
                """UPDATE provider_input_tickets SET revoked_at = ?
                WHERE operation_id = ? AND revoked_at IS NULL""",
                (utc_now(), operation_id),
            ).rowcount
            grant = connection.execute(
                "SELECT project_id, task_id FROM execution_grants WHERE id = ?",
                (lease["grant_id"],),
            ).fetchone()
            self._event(
                connection,
                live,
                grant["project_id"],
                grant["task_id"],
                "execution.provider_input_ticket_revoked",
                request.expected_epoch,
                {"operation_id": operation_id, "revoked": revoked},
            )
            return {"operation_id": operation_id, "revoked": revoked}

        return self._worker_write(
            worker,
            f"/worker/external-operations/{operation_id}/input-tickets/revoke",
            body,
            command,
        )

    # ---- pull side ---------------------------------------------------------------------
    def _check_ticket(self, connection: sqlite3.Connection, token: str):
        """Re-check every fence; any failure raises the uniform ``TicketInvalid``."""
        try:
            ticket = connection.execute(
                "SELECT * FROM provider_input_tickets WHERE token_hash = ?", (_hash(token),)
            ).fetchone()
            if (
                ticket is None
                or ticket["revoked_at"] is not None
                or _parse_utc(ticket["expires_at"]) <= self._now()
            ):
                raise TicketInvalid
            recovery = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id = ?",
                (ticket["operation_id"],),
            ).fetchone()
            if (
                recovery is None
                or recovery["state"] != "active"
                or operation is None
                or operation["lease_id"] != ticket["lease_id"]
                or operation["epoch"] != ticket["epoch"]
                or operation["worker_principal_id"] != ticket["worker_principal_id"]
                or operation["desired_state"] != "admitted"
                or operation["observed_state"] == "exited"
            ):
                raise TicketInvalid
            live = self._live_principal(
                connection,
                {"id": ticket["worker_principal_id"], "org_id": operation["org_id"]},
                audience="worker",
            )
            _, grant, _ = self._lease_fences(
                connection, live, ticket["lease_id"], ticket["epoch"], verify_retained=False
            )
            if (
                grant["snapshot_id"] != ticket["snapshot_id"]
                or grant["snapshot_digest"] != ticket["snapshot_digest"]
            ):
                raise TicketInvalid
            self._authorize_snapshot(
                connection,
                live,
                ticket["snapshot_id"],
                live["id"],
                ticket["snapshot_digest"],
                grant["snapshot_release_id"],
                verify_retained=False,
            )
            # ADR 0039: every pull re-checks the class, so a ticket minted before the
            # guard (or before a class could change) never outlives it.
            require_public_provider_input(
                connection, operation["org_id"], ticket["operation_id"], ticket["snapshot_id"]
            )
            return ticket, live
        except TicketInvalid:
            raise
        except (StoreError, sqlite3.Error, KeyError, TypeError, ValueError):
            raise TicketInvalid from None

    @staticmethod
    def _legacy_files(connection: sqlite3.Connection, snapshot_id: str, manifest: dict[str, Any]):
        entries = manifest["entries"]
        blobs: dict[str, bytes] = {}
        for entry in entries:
            row = connection.execute(
                "SELECT content FROM snapshot_files WHERE snapshot_id = ? AND relative_path = ?",
                (snapshot_id, entry["relative_path"]),
            ).fetchone()
            content = row["content"] if row is not None else None
            if (
                not isinstance(content, bytes)
                or len(content) != entry["size"]
                or hashlib.sha256(content).hexdigest() != entry["sha256"]
            ):
                raise Conflict("Snapshot file integrity check failed.")
            blobs[entry["relative_path"]] = content
        return blobs

    def _legacy_index(
        self, connection: sqlite3.Connection, snapshot_id: str, manifest: dict[str, Any]
    ):
        blobs = self._legacy_files(connection, snapshot_id, manifest)
        index = []
        for item in chunk_plan(manifest["entries"]):
            part = blobs[item["relative_path"]][item["offset"] : item["offset"] + item["size"]]
            index.append({**item, "sha256": hashlib.sha256(part).hexdigest()})
        return index, blobs

    _INDEX_CACHE_MAX = 64

    @staticmethod
    def _retention_present(connection: sqlite3.Connection, ticket: sqlite3.Row, value) -> bool:
        manifest, chunked, index = value
        snapshot_id = ticket["snapshot_id"]
        row = connection.execute(
            "SELECT digest FROM source_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None or row["digest"] != ticket["snapshot_digest"]:
            return False
        table, column = (
            ("snapshot_chunks", "owner_id") if chunked else ("snapshot_files", "snapshot_id")
        )
        count = connection.execute(
            f"SELECT count(*) FROM {table} WHERE {column} = ?",  # noqa: S608 - fixed names
            (snapshot_id,),
        ).fetchone()[0]
        expected = len(index) if chunked else len(manifest["entries"])
        return count == expected

    def _verified_index(
        self, connection: sqlite3.Connection, ticket: sqlite3.Row, live: dict[str, Any]
    ):
        """Verified (manifest, chunked, index) for a ticket, computed once and cached.

        Snapshot rows and chunk metadata are immutable once adopted, so the O(chunks)
        metadata check and index build happen on the first pull per ticket, not on every pull.
        Every per-request fence is still re-checked in ``_check_ticket``.
        """
        lock = self.__dict__.setdefault("_ticket_index_lock", threading.Lock())
        cache = self.__dict__.setdefault("_ticket_index_cache", OrderedDict())
        key = ticket["token_hash"]
        with lock:
            hit = cache.get(key)
            if hit is not None and hit[0] == (ticket["snapshot_id"], ticket["snapshot_digest"]):
                cache.move_to_end(key)
                # Cheap retention recheck on every pull: the cached index must still
                # describe rows that exist (a purge mid-ticket stops serving).
                if self._retention_present(connection, ticket, hit[1]):
                    return hit[1]
                cache.pop(key, None)
                raise TicketInvalid
        snapshot_id = ticket["snapshot_id"]
        try:
            row = SnapshotStore._snapshot(connection, live, snapshot_id)
            manifest = SnapshotStore._response(row)["manifest"]
            SnapshotStore._verify_retained(connection, live, snapshot_id)
        except StoreError:
            raise TicketInvalid from None
        chunked = is_chunked(connection, snapshot_id)
        index = (
            chunk_index(connection, snapshot_id)
            if chunked
            else self._legacy_index(connection, snapshot_id, manifest)[0]
        )
        value = (manifest, chunked, index)
        with lock:
            cache[key] = ((snapshot_id, ticket["snapshot_digest"]), value)
            cache.move_to_end(key)
            while len(cache) > self._INDEX_CACHE_MAX:
                cache.popitem(last=False)
        return value

    def _pull(self, token: str, seq: int | None) -> dict[str, Any]:
        # Reads run in a deferred transaction so they never hold the control write lock; only
        # the short chunks_served update below takes it.
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            ticket, live = self._check_ticket(connection, token)
            snapshot_id = ticket["snapshot_id"]
            manifest, chunked, index = self._verified_index(connection, ticket, live)
            if seq is None:
                body = {
                    "snapshot_id": snapshot_id,
                    "digest": ticket["snapshot_digest"],
                    "manifest": manifest,
                    "delivery": "chunked",
                    "chunks": index,
                }
            else:
                if chunked:
                    meta, content = read_chunk(connection, snapshot_id, seq)
                else:
                    plan = chunk_plan(manifest["entries"])
                    if not 0 <= seq < len(plan):
                        raise NotFound("Snapshot chunk was not found.")
                    item = plan[seq]
                    entry = next(
                        e
                        for e in manifest["entries"]
                        if e["relative_path"] == item["relative_path"]
                    )
                    blobs = self._legacy_files(connection, snapshot_id, {"entries": [entry]})
                    content = blobs[entry["relative_path"]][
                        item["offset"] : item["offset"] + item["size"]
                    ]
                    meta = {**item, "sha256": hashlib.sha256(content).hexdigest()}
                if len(content) > CHUNK_BYTES:
                    raise Conflict("Snapshot chunk integrity check failed.")
                body = {
                    "snapshot_id": snapshot_id,
                    **meta,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            connection.execute("ROLLBACK")
            self._record_served(ticket["token_hash"], count=seq is not None)
            return body
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _still_live(self, connection: sqlite3.Connection, token_hash: str) -> bool:
        """Final fence under the write lock: revocation, expiries and operation state."""
        now = self._now()
        ticket = connection.execute(
            "SELECT * FROM provider_input_tickets WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if ticket is None or ticket["revoked_at"] is not None:
            return False
        lease = connection.execute(
            "SELECT epoch, expires_at FROM execution_leases WHERE id = ?", (ticket["lease_id"],)
        ).fetchone()
        release = connection.execute(
            """SELECT expires_at FROM snapshot_releases WHERE snapshot_id = ?
            AND worker_principal_id = ? AND status = 'active'""",
            (ticket["snapshot_id"], ticket["worker_principal_id"]),
        ).fetchone()
        recovery = connection.execute(
            "SELECT state FROM control_recovery WHERE singleton=1"
        ).fetchone()
        operation = connection.execute(
            "SELECT * FROM external_operations WHERE operation_id = ?",
            (ticket["operation_id"],),
        ).fetchone()
        if (
            recovery is None
            or recovery["state"] != "active"
            or operation is None
            or operation["lease_id"] != ticket["lease_id"]
            or operation["epoch"] != ticket["epoch"]
            or operation["worker_principal_id"] != ticket["worker_principal_id"]
            or operation["desired_state"] != "admitted"
            or operation["observed_state"] == "exited"
        ):
            return False
        try:
            return (
                _parse_utc(ticket["expires_at"]) > now
                and lease is not None
                and lease["epoch"] == ticket["epoch"]
                and _parse_utc(lease["expires_at"]) > now
                and release is not None
                and _parse_utc(release["expires_at"]) > now
            )
        except (ValueError, TypeError):
            return False

    def _record_served(self, token_hash: str, count: bool = True) -> None:
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = 0
            if self._still_live(connection, token_hash):
                updated = connection.execute(
                    """UPDATE provider_input_tickets SET chunks_served = chunks_served + ?,
                    last_served_at = ? WHERE token_hash = ? AND revoked_at IS NULL""",
                    (int(count), utc_now(), token_hash),
                ).rowcount
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        if updated == 0:
            # Revoked between the read and the count: do not hand the bytes out.
            raise TicketInvalid

    def read_manifest(self, token: str) -> dict[str, Any]:
        return self._pull(token, None)

    def read_chunk_for_ticket(self, token: str, seq: int) -> dict[str, Any]:
        if type(seq) is not int or seq < 0:
            raise TicketInvalid
        return self._pull(token, seq)


__all__ = ["ProviderInputStore", "TicketInvalid", "TicketRequest"]
