"""Connect control packages, released inputs and the durable CPU supervisor.

The transport and adapter are supplied by trusted supervisor configuration.
There is deliberately no unattended dispatch CLI until worker enrollment and
artifact/cost acceptance are integrated. Closing a desktop has no role here.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .cpu import CpuOperation
from .inputs import materialize_snapshot
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
        self.descriptor: dict[str, Any] | None = None
        self.operation: CpuOperation | None = None
        self.lease: Lease | None = None
        self.supervisor = CpuSupervisor(self.directory / "journal", adapter, self._validate)

    def close(self) -> None:
        self.supervisor.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request_package(self, lease_id: str, epoch: int) -> dict[str, Any]:
        return self.channel.post(
            f"/worker/leases/{lease_id}/package",
            {"expected_epoch": epoch},
            f"runtime-package-{lease_id}",
        )

    def _validate_package(self, response: dict[str, Any], lease_id: str, epoch: int) -> None:
        try:
            package, lease = response["package"], response["lease"]
            plan = package["plan"]
            spec = plan["execution_spec"]
            if (
                package["version"] != 1
                or package["profile_version"] != "local-cpu-v1"
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
            ):
                raise SupervisorError("Control package does not match runtime authority.")
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
            response = self._request_package(lease.id, lease.epoch)
            self._validate_package(response, lease.id, lease.epoch)
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
            current = self._request_package(self.lease.id, self.lease.epoch)
            self._validate_package(current, self.lease.id, self.lease.epoch)
            if current["package_digest"] == self.descriptor["control"]["package_digest"]:
                self.lease = self._lease(current)
        except Exception:
            # Retain old bounds; the supervisor's fresh validation will deny and
            # cancel possible execution when the control channel is unavailable.
            pass

    def renew(self) -> None:
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
        return self.channel.post(
            "/worker/observations",
            {
                "lease_id": self.lease.id,
                "expected_epoch": self.lease.epoch,
                "observed_state": state,
                "observed_at": datetime.now(UTC).isoformat(),
                "provider_id": observation.container_id,
                "termination_confirmed": state == "exited" and observation.termination_confirmed,
            },
            f"runtime-observation-{uuid.uuid4().hex}",
        )
