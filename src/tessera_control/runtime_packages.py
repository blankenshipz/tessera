"""Freeze CPU operation identity from live, scoped control-plane authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .execution import ExecutionStore
from .migrations import plan_payload
from .models import ExecutionSpec
from .store import Conflict, NotFound, StoreError, _parse_utc, canonical_digest


class RuntimePackageStore(ExecutionStore):
    def _package(self, connection, worker, lease_id, epoch):
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
        grant = self._active_worker_grant(connection, worker, lease["grant_id"])
        if grant["epoch"] != epoch:
            raise Conflict("Execution grant epoch is fenced.")
        revision = connection.execute(
            """SELECT * FROM task_revisions WHERE task_id = ? AND plan_digest = ?
            AND revision < ? ORDER BY revision DESC LIMIT 1""",
            (grant["task_id"], grant["plan_digest"], grant["task_revision"]),
        ).fetchone()
        if revision is None:
            raise Conflict("Approved execution plan is missing.")
        try:
            spec = json.loads(revision["execution_spec_json"])
            ExecutionSpec.model_validate(spec)
            plan = plan_payload(
                revision["title"],
                revision["purpose"],
                revision["budget_cents"],
                revision["max_duration_seconds"],
                spec,
            )
        except (ValueError, TypeError) as error:
            raise Conflict("Approved execution plan is invalid.") from error
        if canonical_digest(plan) != grant["plan_digest"]:
            raise Conflict("Approved execution plan checksum is invalid.")
        if (
            spec["profile"] != "local-cpu"
            or spec["network_enabled"] is not False
            or spec["pinned_skill_digests"]
            or spec["input_snapshot_digest"] != grant["snapshot_digest"]
            or spec["max_output_bytes"] > 16 * 1024 * 1024
        ):
            raise Conflict("Execution plan exceeds the supported CPU package capabilities.")
        package = {
            "version": 1,
            "profile_version": "local-cpu-v1",
            "operation_id": hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32],
            "lease_id": lease_id,
            "grant_id": grant["id"],
            "worker_principal_id": worker["id"],
            "project_id": grant["project_id"],
            "task_id": grant["task_id"],
            "task_revision": grant["task_revision"],
            "epoch": epoch,
            "plan_digest": grant["plan_digest"],
            "plan": plan,
            "snapshot_id": grant["snapshot_id"],
            "snapshot_digest": grant["snapshot_digest"],
            "pinned_image": grant["pinned_image"],
            "hard_expires_at": lease["hard_expires_at"],
        }
        if expiry <= self._now():
            raise Conflict("Execution lease expired while preparing its package.")
        return {"package": package, "package_digest": canonical_digest(package)}, dict(lease)

    def execution_package(
        self, worker: dict[str, Any], lease_id: str, key: str, digest: str, expected_epoch: int
    ) -> tuple[int, dict[str, Any]]:
        if (
            not isinstance(lease_id, str)
            or not 1 <= len(lease_id) <= 200
            or type(expected_epoch) is not int
            or not 1 <= expected_epoch <= 2**63 - 1
        ):
            raise StoreError("Invalid package request.")
        live_lease: dict[str, Any] = {}

        def command(connection, live):
            metadata, lease = self._package(connection, live, lease_id, expected_epoch)
            live_lease.update(lease)
            package = metadata["package"]
            self._event(
                connection,
                live,
                package["project_id"],
                package["task_id"],
                "execution.package_prepared",
                expected_epoch,
                {
                    "operation_id": package["operation_id"],
                    "lease_id": lease_id,
                    "package_digest": metadata["package_digest"],
                    "execution_dispatched": False,
                },
            )
            return 201, metadata

        def replay_guard(connection, live, cached):
            metadata, lease = self._package(connection, live, lease_id, expected_epoch)
            if cached != metadata:
                raise Conflict("Execution package changed since admission.")
            live_lease.update(lease)

        status, metadata = self._mutate(
            worker,
            key,
            "POST",
            f"/worker/leases/{lease_id}/package",
            digest,
            command,
            audience="worker",
            replay_guard=replay_guard,
        )
        return status, {**metadata, "lease": live_lease, "execution_dispatched": False}
