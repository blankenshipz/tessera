"""Connect control packages, released inputs and the durable CPU supervisor.

The transport and adapter are supplied by trusted supervisor configuration.
There is deliberately no unattended dispatch CLI until worker enrollment and
artifact/cost acceptance are integrated. Closing a desktop has no role here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from tessera_control.persistent_output_storage import validate_persistent_output_storage

from .cpu import CpuOperation
from .inputs import materialize_snapshot
from .provider import Provider, ProviderDispatch
from .supervisor import CpuSupervisor, DriverResult, Lease, SupervisorError

MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024


class ControlChannel(Protocol):
    def post(self, path: str, body: dict[str, Any], key: str) -> dict[str, Any]: ...


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CpuRuntime:
    """One durable operation per private runtime directory; fresh checks on ticks."""

    def __init__(
        self,
        directory: Path,
        input_root: Path,
        channel: ControlChannel,
        adapter: Any,
        *,
        worker_id: str,
        persistent_output_collection: bool = False,
        persistent_output_storage: dict[str, Any] | None = None,
        provider: Provider | None = None,
    ):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or stat.S_IMODE(self.directory.stat().st_mode) != 0o700:
            raise SupervisorError("Runtime requires a private 0700 directory.")
        self.input_root = Path(input_root)
        if self.input_root.is_symlink() or stat.S_IMODE(self.input_root.stat().st_mode) != 0o700:
            raise SupervisorError("Runtime inputs require private 0700 supervisor custody.")
        self.channel = channel
        self.adapter = adapter
        self.worker_id = worker_id
        self.persistent_output_collection = persistent_output_collection
        self.persistent_output_storage = persistent_output_storage
        self.descriptor: dict[str, Any] | None = None
        self.operation: CpuOperation | None = None
        self.lease: Lease | None = None
        self.provider_dispatch = (
            ProviderDispatch(self.directory, channel, provider) if provider is not None else None
        )
        self._initialize_observation_journal()
        self._initialize_package_refresh_journal()
        self.supervisor = CpuSupervisor(
            self.directory / "journal",
            adapter,
            self._validate,
            before_start=self.provider_dispatch.before_start if self.provider_dispatch else None,
            before_reconcile=(
                self.provider_dispatch.reconcile_stable if self.provider_dispatch else None
            ),
            before_cancel=self.provider_dispatch.cancel_stable if self.provider_dispatch else None,
        )

    def close(self) -> None:
        self.supervisor.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request_package(self, lease_id: str, epoch: int) -> dict[str, Any]:
        body = self._package_body(epoch)
        return self.channel.post(
            f"/worker/leases/{lease_id}/package",
            body,
            f"runtime-package-{lease_id}",
        )

    def _package_body(self, epoch: int) -> dict[str, Any]:
        body: dict[str, Any] = {"expected_epoch": epoch}
        if self.persistent_output_collection:
            body["persistent_output_collection"] = True
            body["persistent_output_storage"] = self.persistent_output_storage
        return body

    def _current_package(self, lease_id: str, epoch: int) -> dict[str, Any]:
        """Read current authority with a durable fresh key, not a cached package reply."""

        key = self._package_refresh_key(lease_id, epoch)
        try:
            response = self.channel.post(
                f"/worker/leases/{lease_id}/package", self._package_body(epoch), key
            )
            self._validate_package(response, lease_id, epoch)
        except Exception:
            # A lost response keeps this exact read intent pending, so a restart
            # replays it rather than silently treating a stale cached reply as live.
            raise
        self._complete_package_refresh(lease_id, epoch)
        return response

    def _validate_package(self, response: dict[str, Any], lease_id: str, epoch: int) -> None:
        try:
            package, lease, operation = (
                response["package"],
                response["lease"],
                response["operation"],
            )
            plan = package["plan"]
            spec = plan["execution_spec"]
            if (
                package["version"] != 1
                or package["profile_version"] != "local-cpu-v1"
                or package["adapter_version"] != "docker-local-cpu-v1"
                or _digest(package) != response["package_digest"]
                or _digest(plan) != package["plan_digest"]
                or package["lease_id"] != lease_id
                or lease["id"] != lease_id
                or type(package["epoch"]) is not int
                or package["epoch"] != epoch
                or lease["epoch"] != epoch
                or lease["grant_id"] != package["grant_id"]
                or package["worker_principal_id"] != self.worker_id
                or lease["worker_principal_id"] != self.worker_id
                or package["hard_expires_at"] != lease["hard_expires_at"]
                or spec["profile"] != "local-cpu"
                or spec["network_enabled"] is not False
                or spec["pinned_skill_digests"]
                or spec["input_snapshot_digest"] != package["snapshot_digest"]
                or package["operation_id"]
                != hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]
                or package["docker_lookup_key"] != f"tessera-op-{package['operation_id']}"
                or operation["operation_id"] != package["operation_id"]
                or operation["lease_id"] != lease_id
                or operation["grant_id"] != package["grant_id"]
                or operation["worker_principal_id"] != self.worker_id
                or operation["project_id"] != package["project_id"]
                or operation["task_id"] != package["task_id"]
                or operation["epoch"] != epoch
                or operation["package_digest"] != response["package_digest"]
                or operation["adapter_version"] != package["adapter_version"]
                or operation["profile_version"] != package["profile_version"]
                or operation["docker_lookup_key"] != package["docker_lookup_key"]
                or operation["retry_class"] != "manual_reconciliation"
                or operation["desired_state"] not in {"admitted", "cancel_requested"}
                or operation["observed_state"] not in {"unknown", "running", "exited"}
            ):
                raise SupervisorError("Control package does not match runtime authority.")
            experiment = package.get("experiment")
            operation_identity = {
                "experiment_attempt_id": operation.get("experiment_attempt_id"),
                "experiment_id": operation.get("experiment_id"),
                "design_revision": operation.get("experiment_design_revision"),
                "variant_id": operation.get("experiment_variant_id"),
            }
            if experiment is None:
                if any(value is not None for value in operation_identity.values()):
                    raise SupervisorError("Control operation has an unbound experiment identity.")
            elif (
                set(experiment)
                != {
                    "admitted_attempt_id",
                    "experiment_id",
                    "design_revision",
                    "variant_id",
                    "task_id",
                    "plan_digest",
                    "grant_id",
                }
                or experiment["task_id"] != package["task_id"]
                or experiment["plan_digest"] != package["plan_digest"]
                or experiment["grant_id"] != package["grant_id"]
                or experiment["admitted_attempt_id"] != operation_identity["experiment_attempt_id"]
                or experiment["experiment_id"] != operation_identity["experiment_id"]
                or experiment["design_revision"] != operation_identity["design_revision"]
                or experiment["variant_id"] != operation_identity["variant_id"]
            ):
                raise SupervisorError(
                    "Control package experiment identity does not match its operation."
                )
            capability = package.get("persistent_output_storage")
            if self.persistent_output_collection:
                if capability is None:
                    raise SupervisorError("Control package lacks persistent output storage.")
                if validate_persistent_output_storage(capability) != self.persistent_output_storage:
                    raise SupervisorError("Control package persistent output storage changed.")
                if spec["max_output_bytes"] > capability["capacity_bytes"]:
                    raise SupervisorError(
                        "Control package output storage capacity is insufficient."
                    )
            elif capability is not None:
                raise SupervisorError(
                    "Control package unexpectedly enables persistent output storage."
                )
            expiry = datetime.fromisoformat(lease["expires_at"])
            hard_expiry = datetime.fromisoformat(lease["hard_expires_at"])
            if expiry.tzinfo is None or hard_expiry.tzinfo is None or expiry > hard_expiry:
                raise SupervisorError("Control package lease bounds are invalid.")
        except (KeyError, TypeError, ValueError) as error:
            raise SupervisorError("Control package is malformed.") from error

    def _operation(self, descriptor: dict[str, Any]) -> CpuOperation:
        response = descriptor["control"]
        package = response["package"]
        self._validate_package(response, package["lease_id"], package["epoch"])
        if (
            hashlib.sha256(descriptor["input_manifest_json"].encode()).hexdigest()
            != package["snapshot_digest"]
        ):
            raise SupervisorError("Staged manifest does not match the control package.")
        return CpuOperation(
            operation_id=package["operation_id"],
            image=package["pinned_image"],
            argv=tuple(package["plan"]["execution_spec"]["argv"]),
            plan_digest=package["plan_digest"],
            max_duration_seconds=package["plan"]["max_duration_seconds"],
            input_directory=descriptor["input_directory"],
            input_manifest_json=descriptor["input_manifest_json"],
        )

    @staticmethod
    def _lease(response: dict[str, Any]) -> Lease:
        package, lease = response["package"], response["lease"]
        return Lease(
            id=lease["id"],
            operation_id=package["operation_id"],
            plan_digest=package["plan_digest"],
            epoch=lease["epoch"],
            expires_at=datetime.fromisoformat(lease["expires_at"]).astimezone(UTC),
            task_revision=package["task_revision"],
            project_id=package["project_id"],
        )

    def _validate(self, lease: Lease) -> bool:
        if self.descriptor is None:
            return False
        try:
            response = self._current_package(lease.id, lease.epoch)
            return response["package_digest"] == self.descriptor["control"][
                "package_digest"
            ] and datetime.fromisoformat(response["lease"]["expires_at"]) > datetime.now(UTC)
        except Exception:
            return False

    def start(self, lease_id: str, epoch: int) -> DriverResult:
        """Recover an existing identity or stage exactly one newly admitted package."""
        path = self.directory / "runtime.json"
        if path.exists() or path.is_symlink():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise SupervisorError("Runtime descriptor must be a regular file.")
                encoded = source.read(MAX_DESCRIPTOR_BYTES + 1)
            if len(encoded) > MAX_DESCRIPTOR_BYTES:
                raise SupervisorError("Runtime descriptor exceeds its bound.")
            descriptor = json.loads(encoded)
            response = descriptor["control"]
            self._validate_package(response, lease_id, epoch)
            if descriptor.get("experiment_identity") != response["package"].get("experiment"):
                raise SupervisorError("Runtime descriptor experiment identity changed.")
            self._bind_journal(lease_id, epoch)
        else:
            response = self._request_package(lease_id, epoch)
            self._validate_package(response, lease_id, epoch)
            self._bind_journal(lease_id, epoch)
            package = response["package"]
            bundle = self.channel.post(
                f"/worker/leases/{lease_id}/inputs",
                {"expected_epoch": epoch},
                f"runtime-inputs-{lease_id}",
            )
            staged = materialize_snapshot(
                bundle,
                self.input_root,
                expected_snapshot_digest=package["snapshot_digest"],
                expected_lease_id=lease_id,
                expected_epoch=epoch,
                expected_worker_id=self.worker_id,
            )
            descriptor = {
                "control": response,
                "experiment_identity": package.get("experiment"),
                "input_directory": str(staged.directory),
                "input_manifest_json": staged.manifest_json,
            }
            self._operation(descriptor)  # Validate before persisting local operation intent.
            encoded = json.dumps(descriptor, sort_keys=True).encode()
            if len(encoded) > MAX_DESCRIPTOR_BYTES:
                raise SupervisorError("Runtime descriptor exceeds its bound.")
            fd, temporary = tempfile.mkstemp(prefix="runtime-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.link(temporary, path)  # Publish complete bytes without replacing another intent.
            finally:
                os.unlink(temporary)
            parent_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        self.descriptor = descriptor
        self.operation = self._operation(descriptor)
        self.lease = self._lease(response)
        self._refresh_lease()
        return self.supervisor.drive_once(self.operation, self.lease)

    def recover(self, lease_id: str, epoch: int) -> DriverResult:
        """Reconcile a durable runtime intent without issuing a start.

        A descriptor can survive a worker crash after package admission but before
        the supervisor recorded (or received) a start.  Recovery must observe
        that stable identity rather than treating the descriptor as permission to
        create or start it again.
        """

        path = self.directory / "runtime.json"
        if not path.exists() and not path.is_symlink():
            raise SupervisorError("Runtime has no durable descriptor to recover.")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise SupervisorError("Runtime descriptor must be a regular file.")
            encoded = source.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(encoded) > MAX_DESCRIPTOR_BYTES:
            raise SupervisorError("Runtime descriptor exceeds its bound.")
        descriptor = json.loads(encoded)
        response = descriptor["control"]
        self._validate_package(response, lease_id, epoch)
        if descriptor.get("experiment_identity") != response["package"].get("experiment"):
            raise SupervisorError("Runtime descriptor experiment identity changed.")
        self._bind_journal(lease_id, epoch)
        self.descriptor = descriptor
        self.operation = self._operation(descriptor)
        self.lease = self._lease(response)
        self._refresh_lease()
        return self.supervisor.reconcile(self.operation, self.lease)

    def _bind_journal(self, lease_id: str, epoch: int) -> None:
        """Fence a second local journal for the same lease under this worker root."""
        if not isinstance(lease_id, str) or not 1 <= len(lease_id) <= 200:
            raise SupervisorError("Runtime lease identity is invalid.")
        path = self.input_root / f".runtime-{hashlib.sha256(lease_id.encode()).hexdigest()}.json"
        binding = {
            "lease_id": lease_id,
            "epoch": epoch,
            "worker_id": self.worker_id,
            "directory": str(self.directory.resolve()),
        }
        encoded = json.dumps(binding, sort_keys=True).encode()
        fd, temporary = tempfile.mkstemp(prefix=".binding-", dir=self.input_root)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(existing_fd, "rb") as source:
                    if source.read(4097) != encoded:
                        raise SupervisorError(
                            "Lease is bound to another runtime journal."
                        ) from None
            root_fd = os.open(self.input_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        finally:
            os.unlink(temporary)

    def _refresh_lease(self) -> None:
        if self.lease is None or self.descriptor is None:
            raise SupervisorError("Runtime has no bound operation.")
        try:
            current = self._current_package(self.lease.id, self.lease.epoch)
            if current["package_digest"] == self.descriptor["control"]["package_digest"]:
                self.lease = self._lease(current)
        except Exception:
            # Retain old bounds; the supervisor's fresh validation will deny and
            # cancel possible execution when the control channel is unavailable.
            pass

    def renew(self) -> None:
        """Compatibility seam; the development service uses its durable renewal journal."""
        if self.lease is None:
            raise SupervisorError("Runtime has no bound operation.")
        self.channel.post(
            "/worker/leases/renew",
            {
                "lease_id": self.lease.id,
                "expected_epoch": self.lease.epoch,
            },
            f"runtime-renew-{uuid.uuid4().hex}",
        )
        self._refresh_lease()

    def tick(self) -> DriverResult:
        if self.operation is None or self.lease is None:
            raise SupervisorError("Runtime has no bound operation.")
        self._refresh_lease()
        return self.supervisor.tick(self.operation, self.lease)

    def report(self, result: DriverResult) -> dict[str, Any]:
        """Report authenticated observations; never infer cost or scientific success."""
        if self.lease is None:
            raise SupervisorError("Runtime has no bound operation.")
        observation = result.observation
        state = observation.state if observation.state in {"running", "exited"} else "unknown"
        provider_id = observation.container_id
        if self.provider_dispatch is not None:
            if state == "exited" and observation.termination_confirmed:
                self.provider_dispatch.complete_stable(self.operation)
            provider_id = self.provider_dispatch.provider_id(self.operation)
        payload, key, intent_id = self._observation_intent(
            state,
            provider_id,
            state == "exited" and observation.termination_confirmed,
        )
        response = self.channel.post("/worker/observations", payload, key)
        with self._observation_connection() as connection:
            connection.execute("UPDATE reports SET completed=1 WHERE id=?", (intent_id,))
        return response

    def _initialize_observation_journal(self) -> None:
        with self._observation_connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, observed_state TEXT NOT NULL,
                provider_id TEXT, termination_confirmed INTEGER NOT NULL,
                payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                completed INTEGER NOT NULL CHECK (completed IN (0, 1)))"""
            )

    def _initialize_package_refresh_journal(self) -> None:
        with self._package_refresh_connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS package_refreshes (
                lease_id TEXT NOT NULL, epoch INTEGER NOT NULL, sequence INTEGER NOT NULL,
                key TEXT NOT NULL, completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
                PRIMARY KEY (lease_id, epoch, sequence))"""
            )

    def _package_refresh_key(self, lease_id: str, epoch: int) -> str:
        with self._package_refresh_connection() as connection:
            row = connection.execute(
                """SELECT key FROM package_refreshes WHERE lease_id=? AND epoch=? AND completed=0
                ORDER BY sequence DESC LIMIT 1""",
                (lease_id, epoch),
            ).fetchone()
            if row is not None:
                return row[0]
            sequence = connection.execute(
                "SELECT coalesce(max(sequence), 0) + 1 FROM package_refreshes "
                "WHERE lease_id=? AND epoch=?",
                (lease_id, epoch),
            ).fetchone()[0]
            key = (
                "runtime-package-refresh-"
                + hashlib.sha256(f"v1:{lease_id}:{epoch}:{sequence}".encode()).hexdigest()
            )
            connection.execute(
                """INSERT INTO package_refreshes (lease_id, epoch, sequence, key, completed)
                VALUES (?, ?, ?, ?, 0)""",
                (lease_id, epoch, sequence, key),
            )
            return key

    def _complete_package_refresh(self, lease_id: str, epoch: int) -> None:
        with self._package_refresh_connection() as connection:
            connection.execute(
                """UPDATE package_refreshes SET completed=1
                WHERE lease_id=? AND epoch=? AND completed=0""",
                (lease_id, epoch),
            )

    def _observation_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.directory / "runtime-observations.sqlite3")

    def _package_refresh_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.directory / "runtime-package-refreshes.sqlite3")

    def _observation_intent(
        self, state: str, provider_id: str | None, termination_confirmed: bool
    ) -> tuple[dict[str, Any], str, int]:
        if self.lease is None or self.operation is None:
            raise SupervisorError("Runtime has no bound operation.")
        with self._observation_connection() as connection:
            row = connection.execute(
                """SELECT id, payload_json, idempotency_key FROM reports
                WHERE operation_id=? AND observed_state=? AND provider_id IS ?
                AND termination_confirmed=? AND completed=0 ORDER BY id DESC LIMIT 1""",
                (self.operation.operation_id, state, provider_id, int(termination_confirmed)),
            ).fetchone()
            if row is not None:
                return json.loads(row[1]), row[2], row[0]
            payload = {
                "lease_id": self.lease.id,
                "expected_epoch": self.lease.epoch,
                "operation_id": self.operation.operation_id,
                "observed_state": state,
                "observed_at": datetime.now(UTC).isoformat(),
                "provider_id": provider_id,
                "termination_confirmed": termination_confirmed,
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            key = (
                "runtime-observation-"
                + hashlib.sha256(f"{self.operation.operation_id}:{encoded}".encode()).hexdigest()
            )
            cursor = connection.execute(
                """INSERT INTO reports
                (operation_id, observed_state, provider_id, termination_confirmed, payload_json,
                 idempotency_key, completed) VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (
                    self.operation.operation_id,
                    state,
                    provider_id,
                    int(termination_confirmed),
                    encoded,
                    key,
                ),
            )
            return payload, key, cursor.lastrowid
