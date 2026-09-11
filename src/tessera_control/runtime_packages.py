"""Freeze CPU operation identity from live, scoped control-plane authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .execution import ExecutionStore
from .experiments import admitted_attempt_package_identity, bind_admitted_attempt_package
from .migrations import plan_payload
from .models import ExecutionSpec
from .persistent_output_storage import validate_persistent_output_storage
from .store import Conflict, NotFound, StoreError, _parse_utc, canonical_digest


class RuntimePackageStore(ExecutionStore):
    def _package(
        self, connection, worker, lease_id, epoch, persistent_output_storage, admitted_attempt_id
    ):
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
            "adapter_version": "docker-local-cpu-v1",
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
        package["docker_lookup_key"] = f"tessera-op-{package['operation_id']}"
        bound = connection.execute(
            "SELECT id FROM research_experiment_admitted_attempts WHERE grant_id=?", (grant["id"],)
        ).fetchone()
        if bound is not None:
            if admitted_attempt_id is not None and admitted_attempt_id != bound["id"]:
                raise Conflict("Package request admitted attempt does not match the grant binding.")
            admitted_attempt_id = bound["id"]
        elif admitted_attempt_id is not None:
            raise Conflict("Package request names an attempt not bound to this execution grant.")
        if admitted_attempt_id is not None:
            _attempt, binding = admitted_attempt_package_identity(
                connection, worker, package, admitted_attempt_id
            )
            package["experiment"] = binding
        if persistent_output_storage is not None:
            if spec["max_output_bytes"] > persistent_output_storage["capacity_bytes"]:
                raise Conflict("Persistent output storage byte capacity is below declared outputs.")
            package["persistent_output_storage"] = persistent_output_storage
        if expiry <= self._now():
            raise Conflict("Execution lease expired while preparing its package.")
        return {
            "package": package,
            "package_digest": canonical_digest(package),
            "admitted_attempt_id": admitted_attempt_id,
        }, dict(lease)

    @staticmethod
    def _operation_response(row: Any) -> dict[str, Any]:
        return dict(row)

    def _record_operation_intent(
        self,
        connection: Any,
        worker: dict[str, Any],
        package: dict[str, Any],
        package_digest: str,
        request_digest: str,
    ) -> tuple[dict[str, Any], bool]:
        """Create one immutable provider-operation identity before package return."""
        existing = connection.execute(
            "SELECT * FROM external_operations WHERE lease_id=?", (package["lease_id"],)
        ).fetchone()
        expected = {
            "operation_id": package["operation_id"],
            "org_id": worker["org_id"],
            "project_id": package["project_id"],
            "task_id": package["task_id"],
            "grant_id": package["grant_id"],
            "lease_id": package["lease_id"],
            "worker_principal_id": package["worker_principal_id"],
            "epoch": package["epoch"],
            "package_digest": package_digest,
            "adapter_version": package["adapter_version"],
            "profile_version": package["profile_version"],
            "docker_lookup_key": package["docker_lookup_key"],
            "request_digest": request_digest,
            "retry_class": "manual_reconciliation",
            "experiment_attempt_id": None,
            "experiment_id": None,
            "experiment_design_revision": None,
            "experiment_variant_id": None,
        }
        if package.get("experiment") is not None:
            binding = package["experiment"]
            expected.update(
                experiment_attempt_id=binding["admitted_attempt_id"],
                experiment_id=binding["experiment_id"],
                experiment_design_revision=binding["design_revision"],
                experiment_variant_id=binding["variant_id"],
            )
        if existing is not None:
            if any(existing[field] != value for field, value in expected.items()):
                raise Conflict("External operation identity or package digest conflicts.")
            return self._operation_response(existing), False
        now = self._now().isoformat()
        row = expected | {
            "desired_state": "admitted",
            "observed_state": "unknown",
            "provider_id": None,
            "created_at": now,
            "updated_at": now,
        }
        connection.execute(
            """INSERT INTO external_operations
            (operation_id, org_id, project_id, task_id, grant_id, lease_id, worker_principal_id,
             epoch, package_digest, adapter_version, profile_version, docker_lookup_key,
             request_digest, retry_class, desired_state, observed_state, provider_id, created_at,
              updated_at, experiment_attempt_id, experiment_id, experiment_design_revision,
              experiment_variant_id)
            VALUES (:operation_id,:org_id,:project_id,:task_id,:grant_id,:lease_id,
            :worker_principal_id,:epoch,:package_digest,:adapter_version,:profile_version,
            :docker_lookup_key,:request_digest,:retry_class,:desired_state,:observed_state,
             :provider_id,:created_at,:updated_at,:experiment_attempt_id,:experiment_id,
             :experiment_design_revision,:experiment_variant_id)""",
            row,
        )
        return row, True

    def execution_package(
        self,
        worker: dict[str, Any],
        lease_id: str,
        key: str,
        digest: str,
        expected_epoch: int,
        persistent_output_collection: bool = False,
        persistent_output_storage: Any = None,
        admitted_attempt_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if (
            not isinstance(lease_id, str)
            or not 1 <= len(lease_id) <= 200
            or type(expected_epoch) is not int
            or not 1 <= expected_epoch <= 2**63 - 1
            or (
                admitted_attempt_id is not None
                and (
                    not isinstance(admitted_attempt_id, str)
                    or not 1 <= len(admitted_attempt_id) <= 200
                )
            )
        ):
            raise StoreError("Invalid package request.")
        if persistent_output_collection:
            if persistent_output_storage is None:
                raise Conflict("Persistent output storage capability is required.")
            persistent_output_storage = validate_persistent_output_storage(
                persistent_output_storage
            )
        elif persistent_output_storage is not None:
            raise Conflict(
                "Persistent output storage capability requires persistent output collection."
            )
        live_lease: dict[str, Any] = {}
        operation_request = {"expected_epoch": expected_epoch}
        if admitted_attempt_id is not None:
            operation_request["admitted_attempt_id"] = admitted_attempt_id
        if persistent_output_collection:
            operation_request["persistent_output_collection"] = True
            operation_request["persistent_output_storage"] = persistent_output_storage
        operation_request_digest = canonical_digest(operation_request)

        def command(connection, live):
            metadata, lease = self._package(
                connection,
                live,
                lease_id,
                expected_epoch,
                persistent_output_storage,
                admitted_attempt_id,
            )
            live_lease.update(lease)
            package = metadata["package"]
            operation, created = self._record_operation_intent(
                connection, live, package, metadata["package_digest"], operation_request_digest
            )
            bound_attempt_id = metadata["admitted_attempt_id"]
            if bound_attempt_id is not None:
                bind_admitted_attempt_package(
                    connection, live, package, metadata["package_digest"], bound_attempt_id
                )
            metadata["operation"] = operation
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
                    "external_operation_intent": created,
                    "retry_class": "manual_reconciliation",
                    "execution_dispatched": False,
                    "admitted_attempt_id": bound_attempt_id,
                },
            )
            return 201, metadata

        def replay_guard(connection, live, cached):
            metadata, lease = self._package(
                connection,
                live,
                lease_id,
                expected_epoch,
                persistent_output_storage,
                admitted_attempt_id,
            )
            operation, _ = self._record_operation_intent(
                connection,
                live,
                metadata["package"],
                metadata["package_digest"],
                operation_request_digest,
            )
            metadata["operation"] = operation
            bound_attempt_id = metadata["admitted_attempt_id"]
            if bound_attempt_id is not None:
                bind_admitted_attempt_package(
                    connection,
                    live,
                    metadata["package"],
                    metadata["package_digest"],
                    bound_attempt_id,
                )
            if (
                cached.get("package") != metadata["package"]
                or cached.get("package_digest") != metadata["package_digest"]
                or cached.get("operation", {}).get("operation_id") != operation["operation_id"]
            ):
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
