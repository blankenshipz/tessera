"""Explicit, one-shot development worker runner.

This is deliberately a supervisor-side command, not a scheduler.  It consumes
one already-issued grant and one already-registered worker credential; it never
creates human authority or interprets an exit as a research result.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control_channel import HttpControlChannel
from .cpu import DEVELOPMENT_OUTPUT_PROFILE, DockerCpuAdapter
from .development_runtime import DevelopmentCpuRuntime, DevelopmentOutputProfile
from .provider import Provider
from .supervisor import DriverResult, SupervisorError


class DevelopmentWorkerError(SupervisorError):
    """The opt-in development worker cannot safely advance its one operation."""


@dataclass(frozen=True)
class DevelopmentWorkerConfig:
    """Local-only configuration; credentials are always read from a private file."""

    control_url: str
    grant_id: str
    credential_file: Path
    workspace_root: Path
    development_output_root: Path
    allowed_image: str
    output_capacity_bytes: int = 16 * 1024 * 1024
    output_capacity_inodes: int = 256
    poll_seconds: float = 1.0
    max_ticks: int = 90_000
    renew_every_ticks: int = 15

    def __post_init__(self) -> None:
        if (
            not isinstance(self.grant_id, str)
            or not self.grant_id
            or len(self.grant_id) > 200
            or self.poll_seconds <= 0
            or self.max_ticks < 0
            or self.renew_every_ticks < 1
            or self.output_capacity_bytes < 1
            or self.output_capacity_inodes < 1
        ):
            raise DevelopmentWorkerError("Development worker configuration is invalid.")


def read_private_credential(path: Path) -> str:
    """Read a bounded bearer value without following links or disclosing it."""

    path = Path(path)
    try:
        info = path.lstat()
        private = (
            not stat.S_ISLNK(info.st_mode)
            and stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
        )
        if not private:
            raise DevelopmentWorkerError("Worker credential file is unavailable or not private.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            encoded = source.read(4097)
    except OSError as error:
        raise DevelopmentWorkerError(
            "Worker credential file is unavailable or not private."
        ) from error
    if len(encoded) > 4096:
        raise DevelopmentWorkerError("Worker credential file exceeds its bound.")
    try:
        token = encoded.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise DevelopmentWorkerError("Worker credential file is invalid.") from error
    if not token or any(character.isspace() for character in token):
        raise DevelopmentWorkerError("Worker credential file is invalid.")
    return token


def _private_directory(path: Path) -> Path:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise DevelopmentWorkerError("Development worker directory is unavailable.") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DevelopmentWorkerError("Development worker directory must be private 0700 custody.")
    return path.resolve(strict=True)


class RenewalIntentJournal:
    """Persist one renewal key before its request, so a lost reply is replayed."""

    def __init__(self, root: Path) -> None:
        self.path = root / "renewals.sqlite3"
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS renewal_intents (
                lease_id TEXT NOT NULL, epoch INTEGER NOT NULL, sequence INTEGER NOT NULL,
                key TEXT NOT NULL,
                completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
                PRIMARY KEY (lease_id, epoch, sequence))"""
            )
        os.chmod(self.path, 0o600)

    def key(self, lease_id: str, epoch: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT key FROM renewal_intents WHERE lease_id=? AND epoch=? AND completed=0
                ORDER BY sequence DESC LIMIT 1""",
                (lease_id, epoch),
            ).fetchone()
            if row is not None:
                return row[0]
            sequence = connection.execute(
                "SELECT coalesce(max(sequence), 0) + 1 FROM renewal_intents "
                "WHERE lease_id=? AND epoch=?",
                (lease_id, epoch),
            ).fetchone()[0]
            key = (
                "development-renew-"
                + hashlib.sha256(f"v1:{lease_id}:{epoch}:{sequence}".encode()).hexdigest()
            )
            connection.execute(
                """INSERT INTO renewal_intents (lease_id, epoch, sequence, key, completed)
                VALUES (?, ?, ?, ?, 0)""",
                (lease_id, epoch, sequence, key),
            )
            return key

    def complete(self, lease_id: str, epoch: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE renewal_intents SET completed=1
                WHERE lease_id=? AND epoch=? AND completed=0""",
                (lease_id, epoch),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        return connection


class DevelopmentWorkerService:
    """Run exactly one existing grant with development-only output custody."""

    def __init__(
        self,
        config: DevelopmentWorkerConfig,
        *,
        channel: Any | None = None,
        adapter: Any | None = None,
        provider: Provider | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.config = config
        self.root = _private_directory(config.workspace_root)
        self.output_root = _private_directory(config.development_output_root)
        self.token = read_private_credential(config.credential_file)
        self.channel = channel or HttpControlChannel(config.control_url, self.token)
        self.adapter = adapter
        self.provider = provider
        self.sleep = sleep
        self.renewals = RenewalIntentJournal(self.root)

    def run(self) -> dict[str, Any]:
        """Heartbeat, lease, execute, and quarantine only a single grant attempt."""

        # Registration is an owner-only, prior human command.  A heartbeat is the
        # first worker mutation and deliberately precedes lease acquisition.
        self.channel.post("/worker/heartbeat", {}, self._key("heartbeat"))
        lease = self.channel.post(
            "/worker/leases", {"grant_id": self.config.grant_id}, self._key("lease")
        )
        lease_id, epoch, worker_id = self._lease_identity(lease)
        inputs = self.root / "inputs"
        runtime_dir = self.root / "runtime"
        inputs.mkdir(mode=0o700, exist_ok=True)
        runtime_dir.mkdir(mode=0o700, exist_ok=True)
        output_base = self.output_root / "development-only-cpu-outputs"
        output_base.mkdir(mode=0o700, exist_ok=True)
        _private_directory(output_base)
        output_workspace_root = output_base / "workspaces"
        output_workspace_root.mkdir(mode=0o700, exist_ok=True)
        _private_directory(output_workspace_root)
        adapter = self.adapter or DockerCpuAdapter(
            frozenset({self.config.allowed_image}),
            input_root=inputs,
            development_output_root=output_workspace_root,
        )
        profile = DevelopmentOutputProfile(
            self.output_root, self.config.output_capacity_bytes, self.config.output_capacity_inodes
        )
        with DevelopmentCpuRuntime(
            runtime_dir,
            inputs,
            self.channel,
            adapter,
            worker_id=worker_id,
            development_profile=profile,
            provider=self.provider,
        ) as runtime:
            result = runtime.start(lease_id, epoch)
            return self._drive(runtime, result)

    def _drive(self, runtime: DevelopmentCpuRuntime, result: DriverResult) -> dict[str, Any]:
        for tick in range(self.config.max_ticks + 1):
            if result.phase == "terminal" and result.observation.termination_confirmed:
                # collect reports a fresh confirmed exit before touching outputs.
                try:
                    receipt = runtime.collect(result)
                except SupervisorError:
                    # A confirmed provider exit does not make missing, unsafe, or
                    # untransferable output safe to reclaim.  Keep its workspace.
                    runtime.development_outputs.mark_unknown(runtime.operation.operation_id)  # type: ignore[union-attr]
                    return {"state": "unknown", "quarantined": False, "reason": "quarantine_failed"}
                runtime.development_outputs.cleanup(runtime.operation.operation_id)  # type: ignore[union-attr]
                return {"state": "exited", "quarantined": True, "receipt": receipt}
            if result.phase in {"terminal", "denied", "uncertain"}:
                if runtime.operation is not None:
                    runtime.development_outputs.mark_unknown(runtime.operation.operation_id)
                self._report(runtime, result)
                return {"state": "unknown", "quarantined": False, "reason": result.reason}
            self._report(runtime, result)
            if tick == self.config.max_ticks:
                # Do not abandon Docker merely because this foreground loop has
                # exhausted its own budget. Request cancellation and retain the
                # workspace for reconciliation rather than claiming it stopped.
                cancelled = runtime.cancel(reason="worker_tick_limit")
                if runtime.operation is not None:
                    runtime.development_outputs.mark_unknown(runtime.operation.operation_id)
                self._report(runtime, cancelled)
                return {
                    "state": "unknown",
                    "quarantined": False,
                    "reason": "tick_limit_cancellation_requested",
                }
            if tick and tick % self.config.renew_every_ticks == 0:
                self._renew(runtime)
            self.sleep(self.config.poll_seconds)
            result = runtime.tick()
        raise AssertionError("bounded worker loop did not return")

    def _renew(self, runtime: DevelopmentCpuRuntime) -> None:
        if runtime.lease is None:
            raise DevelopmentWorkerError("Development runtime has no lease to renew.")
        lease = runtime.lease
        key = self.renewals.key(lease.id, lease.epoch)  # durable before the HTTP mutation
        self.channel.post(
            "/worker/leases/renew",
            {"lease_id": lease.id, "expected_epoch": lease.epoch},
            key,
        )
        self.renewals.complete(lease.id, lease.epoch)
        # Re-read frozen authority; renewal never changes operation identity.
        runtime._refresh_lease()

    @staticmethod
    def _report(runtime: DevelopmentCpuRuntime, result: DriverResult) -> None:
        runtime.report(result)

    @staticmethod
    def _lease_identity(lease: dict[str, Any]) -> tuple[str, int, str]:
        try:
            lease_id, epoch, worker_id = lease["id"], lease["epoch"], lease["worker_principal_id"]
        except (KeyError, TypeError) as error:
            raise DevelopmentWorkerError("Control returned an invalid lease.") from error
        if (
            not isinstance(lease_id, str)
            or type(epoch) is not int
            or not isinstance(worker_id, str)
        ):
            raise DevelopmentWorkerError("Control returned an invalid lease.")
        return lease_id, epoch, worker_id

    def _key(self, action: str) -> str:
        return (
            "development-worker-"
            + hashlib.sha256(
                json.dumps([action, self.config.grant_id], separators=(",", ":")).encode()
            ).hexdigest()
        )


__all__ = [
    "DEVELOPMENT_OUTPUT_PROFILE",
    "DevelopmentWorkerConfig",
    "DevelopmentWorkerError",
    "DevelopmentWorkerService",
    "RenewalIntentJournal",
    "read_private_credential",
]
