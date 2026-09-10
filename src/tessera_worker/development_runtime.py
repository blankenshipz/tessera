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
from .outputs import OutputCollectionError, validate_output_paths
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
            )
        except OutputSealError as error:
            raise DevelopmentRuntimeError(str(error)) from error
        self._transition(
            operation_id, {"reserved", "running", "unknown", "terminated", "sealed"}, "sealed"
        )
        return seal_id

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

    def _operation(self, descriptor: dict[str, Any]) -> CpuOperation:
        operation = super()._operation(descriptor)
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
        seal_id = self.development_outputs.seal(self.operation.operation_id, observation)
        return self.development_outputs.upload(self.operation.operation_id, self.channel, seal_id)


__all__ = [
    "DEVELOPMENT_OUTPUT_DIRECTORY",
    "DEVELOPMENT_QUARANTINE_LABEL",
    "DevelopmentCpuRuntime",
    "DevelopmentOutputBackend",
    "DevelopmentOutputProfile",
    "DevelopmentRuntimeError",
]
