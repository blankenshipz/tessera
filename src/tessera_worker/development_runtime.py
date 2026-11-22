"""Explicit development-only CPU output backend.

This module is intentionally separate from normal package admission.  It is a
developer fixture profile, never a pilot storage capability: it reserves a local
directory in userspace, mounts it only through the marked CPU operation, and
keeps every collected output quarantined.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from .artifacts import ArtifactChannel
from .cpu import (
    DEVELOPMENT_FIXTURE_MARKER,
    DEVELOPMENT_OUTPUT_PROFILE,
    CpuOperation,
    DevelopmentOutputMount,
)
from .outputs import OutputCollectionError, validate_output_paths, worker_output_limit
from .run_receipt import (
    ADAPTER_VERSION,
    build_run_receipt,
    idempotency_key,
    terminal_facts,
)
from .runtime import CpuRuntime
from .sealed_outputs import OutputSealError, SealedOutputJournal
from .supervisor import DriverResult, SupervisorError

DEVELOPMENT_OUTPUT_DIRECTORY = "development-only-cpu-outputs"
DEVELOPMENT_QUARANTINE_LABEL = "development-only-quarantined"
_OPERATION_ID = re.compile(r"[a-f0-9]{32}\Z")


class DevelopmentRuntimeError(SupervisorError):
    """The explicitly marked development backend cannot advance safely."""


@dataclass(frozen=True)
class DevelopmentOutputProfile:
    """A developer-owned fixture profile; this is not a pilot admission flag."""

    root: Path
    capacity_bytes: int
    capacity_inodes: int
    # Operator ceiling on one operation's declared output bytes; None means the
    # installation default (tessera_control.artifact_limits). A plan above it is refused.
    worker_max_output_bytes: int | None = None
    profile: Literal["development-local-cpu-v1"] = DEVELOPMENT_OUTPUT_PROFILE
    fixture_marker: Literal["development-local-fixture-v1"] = DEVELOPMENT_FIXTURE_MARKER

    def __post_init__(self) -> None:
        if (
            not isinstance(self.root, Path)
            or not self.root.is_absolute()
            or self.profile != DEVELOPMENT_OUTPUT_PROFILE
            or self.fixture_marker != DEVELOPMENT_FIXTURE_MARKER
            or type(self.capacity_bytes) is not int
            or type(self.capacity_inodes) is not int
            or self.capacity_bytes < 1
            or self.capacity_inodes < 1
        ):
            raise DevelopmentRuntimeError(
                "Development output profile requires explicit fixture marker and bounded capacity."
            )


class DevelopmentOutputBackend:
    """Durable userspace reservations and explicit reclamation for fixture outputs."""

    def __init__(self, profile: DevelopmentOutputProfile) -> None:
        self.profile = profile
        self.label = DEVELOPMENT_QUARANTINE_LABEL
        # The caller, not this helper, owns and provisions the development
        # root.  Refusing a missing or shared parent prevents a convenient test
        # seam from becoming an ambient host-output directory.
        self._assert_private_directory(profile.root)
        self.root = profile.root / DEVELOPMENT_OUTPUT_DIRECTORY
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_private_directory(self.root)
        self.workspace_root = self.root / "workspaces"
        self.workspace_root.mkdir(mode=0o700, exist_ok=True)
        self._assert_private_directory(self.workspace_root)
        self.path = self.root / "reservations.sqlite3"
        self._initialize()
        self.seals = SealedOutputJournal(self.root / "sealed-quarantine")

    @staticmethod
    def _assert_private_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as error:
            raise DevelopmentRuntimeError("Development output root is unavailable.") from error
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise DevelopmentRuntimeError("Development output root must be private 0700 custody.")

    def reserve(
        self,
        *,
        operation_id: str,
        lease_id: str,
        epoch: int,
        output_paths: tuple[str, ...],
        max_output_bytes: int,
    ) -> Path:
        """Atomically reserve declared bytes/inodes before Docker can create."""
        if (
            not _OPERATION_ID.fullmatch(operation_id)
            or not isinstance(lease_id, str)
            or not lease_id
            or type(epoch) is not int
            or epoch < 1
            or type(max_output_bytes) is not int
            or not 0 <= max_output_bytes <= self.profile.capacity_bytes
            or type(output_paths) is not tuple
            or len(output_paths) > 128
        ):
            raise DevelopmentRuntimeError("Development output reservation is invalid.")
        try:
            output_paths = validate_output_paths(output_paths)
        except OutputCollectionError as error:
            raise DevelopmentRuntimeError("Development output reservation is invalid.") from error
        try:
            ceiling = worker_output_limit(self.profile.worker_max_output_bytes)
        except OutputCollectionError as error:
            raise DevelopmentRuntimeError("Development output reservation is invalid.") from error
        if max_output_bytes > ceiling:
            raise DevelopmentRuntimeError(
                "Planned output limit exceeds this worker's configured maximum."
            )
        # Reserve every declared leaf plus its parents and the workspace itself.
        inode_count = (
            1
            + len(output_paths)
            + len(
                {
                    "/".join(path.split("/")[:index])
                    for path in output_paths
                    for index in range(1, len(path.split("/")))
                }
            )
        )
        workspace = self.workspace_root / operation_id
        paths_json = self._json(list(output_paths))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM reservations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["lease_id"],
                    existing["epoch"],
                    existing["paths_json"],
                    existing["byte_count"],
                    existing["inode_count"],
                ) != (lease_id, epoch, paths_json, max_output_bytes, inode_count):
                    connection.rollback()
                    raise DevelopmentRuntimeError(
                        "Development output reservation identity conflicts."
                    )
                if (
                    existing["status"] == "cleaned"
                    or not workspace.is_dir()
                    or workspace.is_symlink()
                ):
                    connection.rollback()
                    raise DevelopmentRuntimeError(
                        "Development output workspace is not recoverable."
                    )
                connection.commit()
                return workspace
            used = connection.execute(
                "SELECT coalesce(sum(byte_count), 0), coalesce(sum(inode_count), 0) "
                "FROM reservations WHERE status != 'cleaned'"
            ).fetchone()
            if (
                used[0] + max_output_bytes > self.profile.capacity_bytes
                or used[1] + inode_count > self.profile.capacity_inodes
            ):
                connection.rollback()
                raise DevelopmentRuntimeError(
                    "Development output capacity reservation is exhausted."
                )
            if workspace.exists() or workspace.is_symlink():
                connection.rollback()
                raise DevelopmentRuntimeError(
                    "Development output workspace already exists outside reservation."
                )
            connection.execute(
                """INSERT INTO reservations
                (operation_id, lease_id, epoch, paths_json, byte_count, inode_count, status)
                VALUES (?, ?, ?, ?, ?, ?, 'reserved')""",
                (operation_id, lease_id, epoch, paths_json, max_output_bytes, inode_count),
            )
            connection.commit()
        try:
            workspace.mkdir(mode=0o700)
        except OSError as error:
            raise DevelopmentRuntimeError(
                "Cannot create isolated development output workspace."
            ) from error
        self._assert_private_directory(workspace)
        return workspace

    def mark_unknown(self, operation_id: str) -> None:
        self._transition(operation_id, {"reserved", "running", "terminated", "unknown"}, "unknown")

    def mark_running(self, operation_id: str) -> None:
        # Restart recovery can observe an already-running operation whose
        # reservation was marked before the prior worker process stopped.
        self._transition(operation_id, {"reserved", "running"}, "running")

    def seal(self, operation_id: str, termination_observation: dict[str, Any]) -> str:
        row = self._reservation(operation_id)
        if row["status"] not in {"reserved", "running", "unknown", "terminated", "sealed"}:
            raise DevelopmentRuntimeError("Development output state cannot be sealed.")
        workspace = self.workspace_root / operation_id
        try:
            self._assert_declared_tree(workspace, tuple(json.loads(row["paths_json"])))
            seal_id = self.seals.seal(
                workspace=workspace,
                lease_id=row["lease_id"],
                expected_epoch=row["epoch"],
                output_paths=tuple(json.loads(row["paths_json"])),
                max_output_bytes=row["byte_count"],
                termination_observation=termination_observation,
                worker_max_output_bytes=self.profile.worker_max_output_bytes,
            )
        except OutputSealError as error:
            raise DevelopmentRuntimeError(str(error)) from error
        self._transition(
            operation_id, {"reserved", "running", "unknown", "terminated", "sealed"}, "sealed"
        )
        return seal_id

    def uploaded_seal_id(self, operation_id: str) -> str | None:
        """The seal already uploaded for this operation (restart replay), if any."""
        row = self._reservation(operation_id)
        if row["status"] != "uploaded":
            return None
        # seals.lease_id is UNIQUE, so a lease has at most one seal; the operation filter
        # additionally refuses a seal that was made for a different operation.
        with self.seals._connect() as connection:
            seal = connection.execute(
                "SELECT id FROM seals WHERE lease_id=? AND epoch=? AND operation_id=?",
                (row["lease_id"], row["epoch"], operation_id),
            ).fetchone()
        if seal is None:
            raise DevelopmentRuntimeError("Uploaded development output has no seal.")
        return seal["id"]

    def manifest_digest(self, seal_id: str) -> str:
        """The sealed manifest digest recorded when the output was sealed."""
        with self.seals._connect() as connection:
            row = connection.execute(
                "SELECT manifest_digest FROM seals WHERE id=?", (seal_id,)
            ).fetchone()
        if row is None:
            raise DevelopmentRuntimeError("Development output seal was not found.")
        return row["manifest_digest"]

    @staticmethod
    def _assert_declared_tree(workspace: Path, paths: tuple[str, ...]) -> None:
        """Fail closed on post-stop output floods before sealed collection."""
        expected_files = set(paths)
        expected_directories = {
            "/".join(path.split("/")[:index])
            for path in paths
            for index in range(1, len(path.split("/")))
        }
        try:
            root = workspace.lstat()
            if stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode):
                raise ValueError
            root_device = root.st_dev

            def walk(directory: Path, prefix: str) -> None:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        relative = f"{prefix}/{entry.name}" if prefix else entry.name
                        info = path.lstat()
                        if stat.S_ISLNK(info.st_mode) or info.st_dev != root_device:
                            raise ValueError
                        if stat.S_ISDIR(info.st_mode):
                            if relative not in expected_directories:
                                raise ValueError
                            walk(path, relative)
                        elif not stat.S_ISREG(info.st_mode) or relative not in expected_files:
                            raise ValueError

            walk(workspace, "")
        except (OSError, ValueError) as error:
            raise DevelopmentRuntimeError(
                "Development output contains undeclared or unsafe nodes."
            ) from error

    def upload(self, operation_id: str, channel: ArtifactChannel, seal_id: str) -> dict[str, Any]:
        row = self._reservation(operation_id)
        if row["status"] not in {"sealed", "uploaded"}:
            raise DevelopmentRuntimeError("Development output is not durably sealed.")
        try:
            receipt = self.seals.resume(channel, seal_id)
        except OutputSealError as error:
            raise DevelopmentRuntimeError(str(error)) from error
        if receipt.get("status") != "quarantined" or receipt.get("classification") not in {
            None,
            "unclassified",
        }:
            raise DevelopmentRuntimeError("Development output transfer did not remain quarantined.")
        if receipt.get("project_placements_committed") is not True:
            raise DevelopmentRuntimeError(
                "Development output lacks committed project artifact placement."
            )
        # The transfer helper has also received exact committed placement receipts;
        # quarantine alone is intentionally insufficient to permit reclamation.
        self._transition(operation_id, {"sealed", "uploaded"}, "uploaded")
        return {**receipt, "output_label": self.label, "development_profile": self.profile.profile}

    def cleanup(self, operation_id: str) -> None:
        """Reclaim only after exact durable project placement, never quarantine alone."""
        row = self._reservation(operation_id)
        if row["status"] != "uploaded":
            raise DevelopmentRuntimeError("Refusing cleanup for unresolved development output.")
        workspace = self.workspace_root / operation_id
        try:
            self._assert_private_directory(workspace)
            self._assert_declared_tree(workspace, tuple(json.loads(row["paths_json"])))
            shutil.rmtree(workspace)
        except OSError as error:
            raise DevelopmentRuntimeError("Cannot reclaim development output workspace.") from error
        self._transition(operation_id, {"uploaded"}, "cleaned")

    def _reservation(self, operation_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reservations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise DevelopmentRuntimeError("Development output reservation was not found.")
        return row

    def _transition(self, operation_id: str, allowed: set[str], target: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM reservations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or row["status"] not in allowed:
                connection.rollback()
                raise DevelopmentRuntimeError("Development output state transition is fenced.")
            connection.execute(
                "UPDATE reservations SET status=? WHERE operation_id=?", (target, operation_id)
            )
            connection.commit()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS reservations (
                operation_id TEXT PRIMARY KEY,
                lease_id TEXT NOT NULL UNIQUE,
                epoch INTEGER NOT NULL,
                paths_json TEXT NOT NULL,
                byte_count INTEGER NOT NULL,
                inode_count INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN
                    ('reserved', 'running', 'unknown', 'terminated', 'sealed', 'uploaded',
                     'cleaned'))
                )"""
            )
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


class DevelopmentCpuRuntime(CpuRuntime):
    """A marked development wrapper around the normal supervisor seam.

    It never changes package admission.  Its caller must explicitly request this
    profile and use a Docker adapter configured with this backend's private root.
    Collection is an explicit post-exit action, so cancellation never waits for it.
    """

    _control_manifest_digest: str | None = None

    def __init__(
        self, *args: Any, development_profile: DevelopmentOutputProfile, **kwargs: Any
    ) -> None:
        if "persistent_output_collection" in kwargs or "persistent_output_storage" in kwargs:
            raise DevelopmentRuntimeError(
                "Development profile cannot activate persistent pilot storage."
            )
        self.development_outputs = DevelopmentOutputBackend(development_profile)
        adapter = args[3] if len(args) > 3 else kwargs.get("adapter")
        if (
            getattr(adapter, "development_output_root", None)
            != self.development_outputs.workspace_root
        ):
            raise DevelopmentRuntimeError(
                "Adapter is not configured for this development output root."
            )
        super().__init__(*args, **kwargs)
        # The sealed manifest digest belongs to exactly one operation; both are reset
        # whenever a new operation is built so a reused runtime cannot carry a stale
        # digest into the next receipt.
        self._sealed_manifest_digest: str | None = None
        self._sealed_operation_id: str | None = None
        self._control_manifest_digest: str | None = None

    def _operation(self, descriptor: dict[str, Any]) -> CpuOperation:
        operation = super()._operation(descriptor)
        self._sealed_manifest_digest = None
        self._sealed_operation_id = None
        self._control_manifest_digest = None
        package = descriptor["control"]["package"]
        workspace = self.development_outputs.reserve(
            operation_id=operation.operation_id,
            lease_id=package["lease_id"],
            epoch=package["epoch"],
            output_paths=tuple(package["plan"]["execution_spec"]["output_paths"]),
            max_output_bytes=package["plan"]["execution_spec"]["max_output_bytes"],
        )
        return replace(operation, development_output=DevelopmentOutputMount(str(workspace)))

    def start(self, lease_id: str, epoch: int) -> DriverResult:
        result = super().start(lease_id, epoch)
        if self.operation is not None and result.phase == "running":
            self.development_outputs.mark_running(self.operation.operation_id)
        return result

    def recover(self, lease_id: str, epoch: int) -> DriverResult:
        """Reconcile a prior descriptor without treating it as a new start."""

        result = super().recover(lease_id, epoch)
        if self.operation is not None and result.phase == "running":
            self.development_outputs.mark_running(self.operation.operation_id)
        return result

    def cancel(self, *, reason: str = "requested") -> DriverResult:
        if self.operation is None:
            raise DevelopmentRuntimeError("Runtime has no bound operation.")
        result = self.supervisor.cancel(self.operation, reason=reason)
        if not result.observation.termination_confirmed:
            self.development_outputs.mark_unknown(self.operation.operation_id)
        return result

    def collect(
        self, result: DriverResult, *, reported_observation: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Report a fresh confirmed exit, then seal and upload quarantined bytes."""
        if (
            self.operation is None
            or result.phase != "terminal"
            or not result.observation.termination_confirmed
        ):
            if self.operation is not None:
                self.development_outputs.mark_unknown(self.operation.operation_id)
            raise DevelopmentRuntimeError("Collection requires a fresh confirmed termination.")
        observation = reported_observation or self.report(result)
        # After a restart past upload (e.g. a failed run-receipt post) the bytes are
        # already sealed and transferred; replay that seal rather than re-sealing.
        seal_id = self.development_outputs.uploaded_seal_id(
            self.operation.operation_id
        ) or self.development_outputs.seal(self.operation.operation_id, observation)
        self._sealed_manifest_digest = None
        self._sealed_operation_id = None
        digest = self.development_outputs.manifest_digest(seal_id)
        self._sealed_manifest_digest = digest
        self._sealed_operation_id = self.operation.operation_id
        self._control_manifest_digest = None
        uploaded = self.development_outputs.upload(
            self.operation.operation_id, self.channel, seal_id
        )
        # Control's own manifest digest, already recomputed and checked by the
        # transfer helper; the run receipt must cite this, not the local seal digest.
        self._control_manifest_digest = uploaded.get("manifest_digest")
        return uploaded

    def post_run_receipt(self, result: DriverResult) -> dict[str, Any] | None:
        """Post one observed run receipt after a confirmed exit and sealed collection.

        Returns ``None`` when no receipt applies (a fake provider is configured, a
        different adapter version, or the adapter exposes no inspect seam).  Any
        failure raises; the caller must not claim the receipt was recorded.  One
        attempt, with a deterministic key so a restart replays the same request.
        """
        if (
            self.operation is None
            or self.lease is None
            or self.descriptor is None
            or self.provider_dispatch is not None
            or self.descriptor["control"]["package"].get("adapter_version") != ADAPTER_VERSION
            or not callable(getattr(self.adapter, "run", None))
        ):
            return None
        observation = result.observation
        if (
            result.phase != "terminal"
            or observation.state != "exited"
            or not observation.termination_confirmed
            or observation.container_id is None
            or type(observation.exit_code) is not int
            or self._sealed_manifest_digest is None
            or not isinstance(self._control_manifest_digest, str)
            or self._sealed_operation_id != self.operation.operation_id
        ):
            raise DevelopmentRuntimeError(
                "Run receipt requires a confirmed exit and sealed output."
            )
        operation = self.operation
        reply = self.adapter.run(["inspect", "--type", "container", operation.container_name])
        if reply.returncode:
            raise DevelopmentRuntimeError("Terminal inspect for the run receipt failed.")
        facts = terminal_facts(
            reply.stdout,
            operation_id=operation.operation_id,
            plan_digest=operation.plan_digest,
            container_id=observation.container_id,
            exit_code=observation.exit_code,
        )
        body = build_run_receipt(
            lease_id=self.lease.id,
            epoch=self.lease.epoch,
            operation_id=operation.operation_id,
            plan_digest=operation.plan_digest,
            image=operation.image,
            argv=operation.argv,
            network_profile=operation.network_profile,
            facts=facts,
            control_manifest_digest=self._control_manifest_digest,
            sealed_manifest_digest=self._sealed_manifest_digest,
            scratch_bytes=operation.scratch_bytes,
        )
        key = idempotency_key(operation.operation_id, self.lease.id, self.lease.epoch)
        return self.channel.post(
            f"/worker/external-operations/{operation.operation_id}/run-receipt", body, key
        )


__all__ = [
    "DEVELOPMENT_OUTPUT_DIRECTORY",
    "DEVELOPMENT_QUARANTINE_LABEL",
    "DevelopmentCpuRuntime",
    "DevelopmentOutputBackend",
    "DevelopmentOutputProfile",
    "DevelopmentRuntimeError",
]
