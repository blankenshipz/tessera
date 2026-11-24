"""Worker-only, just-in-time delivery of an authorized immutable snapshot."""

from __future__ import annotations

import base64
import sqlite3
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .code_approval import refuse_local_transfer
from .execution import ExecutionStore
from .snapshot_chunks import chunk_index, is_chunked, read_chunk
from .snapshots import SnapshotStore
from .store import Conflict, Forbidden, NotFound, StoreError, _connect, _parse_utc, utc_now


class DeliverInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_epoch: int = Field(ge=1, le=2**63 - 1)


class InputDeliveryStore(ExecutionStore):
    def _lease_fences(
        self,
        connection: sqlite3.Connection,
        worker: dict[str, Any],
        lease_id: str,
        epoch: int,
        *,
        verify_retained: bool = True,
    ) -> tuple[Any, Any, Any]:
        lease = connection.execute(
            "SELECT * FROM execution_leases WHERE id = ?", (lease_id,)
        ).fetchone()
        if lease is None or lease["worker_principal_id"] != worker["id"]:
            raise NotFound("Execution lease was not found.")
        try:
            expiry = _parse_utc(lease["expires_at"])
        except (ValueError, TypeError) as error:
            raise Conflict("Execution lease expiry is invalid.") from error
        if lease["epoch"] != epoch or expiry <= self._now():
            raise Conflict("Execution lease is no longer current.")
        # A lease whose grant now belongs to another worker is answered exactly as a grant
        # that does not exist (404, same body), and the refusal records nothing.
        grant = self._active_worker_grant(
            connection,
            worker,
            lease["grant_id"],
            verify_retained=verify_retained,
            foreign_as_not_found=True,
        )
        if grant["epoch"] != epoch:
            raise Conflict("Execution grant epoch is no longer current.")
        return lease, grant, expiry

    def _lease_bundle(
        self, connection: sqlite3.Connection, worker: dict[str, Any], lease_id: str, epoch: int
    ) -> dict[str, Any]:
        lease, grant, expiry = self._lease_fences(connection, worker, lease_id, epoch)
        snapshot = SnapshotStore._snapshot(connection, worker, grant["snapshot_id"])
        refuse_local_transfer(connection, grant["snapshot_id"])
        verified = SnapshotStore._verify_retained(connection, worker, grant["snapshot_id"])
        if verified.get("chunked"):
            # ADR 0037: chunked delivery carries an index; bytes come one chunk at a time.
            if expiry <= self._now():
                raise Conflict("Execution lease expired while preparing input delivery.")
            return {
                "lease": lease,
                "grant": grant,
                "snapshot": {
                    "id": snapshot["id"],
                    "digest": verified["digest"],
                    "manifest": SnapshotStore._response(snapshot)["manifest"],
                    "delivery": "chunked",
                    "chunks": chunk_index(connection, snapshot["id"]),
                },
            }
        files = [
            {
                "relative_path": row["relative_path"],
                "content_base64": base64.b64encode(row["content"]).decode("ascii"),
            }
            for row in connection.execute(
                """SELECT relative_path, content FROM snapshot_files
                WHERE snapshot_id = ? ORDER BY relative_path""",
                (grant["snapshot_id"],),
            )
        ]
        if expiry <= self._now():
            raise Conflict("Execution lease expired while preparing input delivery.")
        return {
            "lease": lease,
            "grant": grant,
            "snapshot": {
                "id": snapshot["id"],
                "digest": verified["digest"],
                "manifest": SnapshotStore._response(snapshot)["manifest"],
                "files": files,
            },
        }

    def read_input_chunk(
        self, worker: dict[str, Any], lease_id: str, seq: int, expected_epoch: int
    ) -> dict[str, Any]:
        """Read one chunk under the same fences as ``_lease_bundle``; never stored or replayed."""
        if (
            not isinstance(lease_id, str)
            or not lease_id.strip()
            or len(lease_id) > 200
            or type(seq) is not int
            or seq < 0
            or type(expected_epoch) is not int
            or not 1 <= expected_epoch <= 2**63 - 1
        ):
            raise StoreError("Invalid input chunk request.")
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            live = self._live_principal(connection, worker, audience="worker")
            recovery = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            if recovery is None or recovery["state"] != "active":
                raise Forbidden("Control state is held after restore; input delivery is disabled.")
            lease, grant, expiry = self._lease_fences(connection, live, lease_id, expected_epoch)
            refuse_local_transfer(connection, grant["snapshot_id"])
            if not is_chunked(connection, grant["snapshot_id"]):
                raise NotFound("Snapshot is not delivered in chunks.")
            SnapshotStore._verify_retained(connection, live, grant["snapshot_id"])
            meta, content = read_chunk(connection, grant["snapshot_id"], seq)
            if expiry <= self._now():
                raise Conflict("Execution lease expired while preparing input delivery.")
            return {
                "snapshot_id": grant["snapshot_id"],
                **meta,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

    def deliver_inputs(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, expected_epoch: int
    ) -> tuple[int, dict[str, Any]]:
        if (
            not isinstance(lease_id, str)
            or not lease_id.strip()
            or len(lease_id) > 200
            or type(expected_epoch) is not int
            or not 1 <= expected_epoch <= 2**63 - 1
        ):
            raise StoreError("Invalid input delivery request.")
        path = f"/worker/leases/{lease_id}/inputs"
        receipt_id = "delivery_" + uuid.uuid4().hex
        bundle: dict[str, Any] = {}

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            bundle.update(self._lease_bundle(connection, live, lease_id, expected_epoch))
            receipt = {
                "id": receipt_id,
                "lease_id": lease_id,
                "grant_id": bundle["grant"]["id"],
                "worker_principal_id": live["id"],
                "snapshot_id": bundle["snapshot"]["id"],
                "snapshot_digest": bundle["snapshot"]["digest"],
                "epoch": expected_epoch,
                "authorized_at": utc_now(),
                "delivery_confirmed": False,
            }
            self._event(
                connection,
                live,
                bundle["grant"]["project_id"],
                bundle["grant"]["task_id"],
                "worker.inputs_release_authorized",
                expected_epoch,
                {
                    "receipt_id": receipt_id,
                    "lease_id": lease_id,
                    "snapshot_id": receipt["snapshot_id"],
                    "snapshot_digest": receipt["snapshot_digest"],
                    "delivery_confirmed": False,
                },
            )
            return 201, {"receipt": receipt}

        def replay_guard(
            connection: sqlite3.Connection, live: dict[str, Any], _: dict[str, Any]
        ) -> None:
            bundle.update(self._lease_bundle(connection, live, lease_id, expected_epoch))

        status, metadata = self._mutate(
            worker,
            key,
            "POST",
            path,
            digest,
            command,
            audience="worker",
            replay_guard=replay_guard,
        )
        return status, {**metadata, "snapshot": bundle["snapshot"]}
