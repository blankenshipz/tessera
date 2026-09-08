"""Durably seal synthetic stopped-workspace outputs before quarantine transfer.

This local journal is deliberately not control-plane authority.  Callers must
give it a fresh, control-acknowledged termination observation before it reads an
unsealed workspace.  A sealed snapshot can subsequently resume transfer without
reading the workspace again.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .artifacts import ArtifactChannel, upload_output_set
from .outputs import CollectedOutput, OutputCollectionError, collect_outputs

MAX_TERMINATION_OBSERVATION_AGE = timedelta(minutes=5)


class OutputSealError(ValueError):
    """A local output seal could not be safely created or resumed."""


def _operation_id(lease_id: str) -> str:
    return hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]


def _seal_id(lease_id: str, epoch: int, operation_id: str) -> str:
    value = f"tessera-sealed-output-v1:{lease_id}:{epoch}:{operation_id}"
    return "output_seal_" + hashlib.sha256(value.encode()).hexdigest()[:32]


class SealedOutputJournal:
    """Private SQLite custody for one or more immutable synthetic output seals.

    The journal must live under a private synthetic runtime root.  It is intended
    for fixture/recovery evidence only; it neither enables a persistent runtime
    mount nor accepts a result.  ``seal`` always obtains a fresh observation from
    its caller, while ``resume`` intentionally uses only already sealed bytes.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(self.root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OutputSealError("Output seal journal root must be a directory.")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise OutputSealError("Output seal journal root must be private (0700).")
        self.path = self.root / "sealed-outputs.sqlite3"
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise OutputSealError("Output seal journal must be a regular file.")
        self._initialize()

    def seal(
        self,
        *,
        workspace: Path,
        lease_id: str,
        expected_epoch: int,
        output_paths: Sequence[str],
        max_output_bytes: int,
        termination_observation: dict[str, Any],
        now: datetime | None = None,
    ) -> str:
        """Collect and atomically retain an immutable output snapshot.

        Repeating ``seal`` checks the current workspace against the original
        sealed bytes, so a caller cannot silently replace a seal.  Use ``resume``
        after a crash or lost transfer response instead of rereading the source.
        """

        operation_id = self._validate_identity(lease_id, expected_epoch)
        self._validate_termination(
            termination_observation,
            lease_id,
            expected_epoch,
            operation_id,
            now or datetime.now(UTC),
        )
        try:
            outputs = collect_outputs(Path(workspace), output_paths, max_output_bytes)
        except OutputCollectionError as error:
            raise OutputSealError(str(error)) from error
        seal_id = _seal_id(lease_id, expected_epoch, operation_id)
        paths_json = self._json(list(output_paths))
        manifest = self._manifest(seal_id, lease_id, expected_epoch, operation_id, outputs)
        manifest_json = self._json(manifest)
        manifest_digest = hashlib.sha256(manifest_json.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM seals WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["id"] != seal_id
                    or existing["epoch"] != expected_epoch
                    or existing["operation_id"] != operation_id
                    or existing["paths_json"] != paths_json
                    or existing["max_output_bytes"] != max_output_bytes
                ):
                    connection.rollback()
                    raise OutputSealError("Output seal identity or declaration conflicts.")
                if self._outputs(connection, seal_id) != tuple(
                    sorted(outputs, key=lambda item: item.relative_path)
                ):
                    connection.rollback()
                    raise OutputSealError("Output bytes changed after sealing.")
                if existing["manifest_digest"] != manifest_digest:
                    connection.rollback()
                    raise OutputSealError("Output seal journal integrity check failed.")
                connection.commit()
                return seal_id
            connection.execute(
                """INSERT INTO seals
                (id, lease_id, epoch, operation_id, paths_json, max_output_bytes,
                 observation_id, manifest_json, manifest_digest, transfer_receipt_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    seal_id,
                    lease_id,
                    expected_epoch,
                    operation_id,
                    paths_json,
                    max_output_bytes,
                    termination_observation["id"],
                    manifest_json,
                    manifest_digest,
                ),
            )
            connection.executemany(
                """INSERT INTO sealed_outputs (seal_id, relative_path, sha256, size, content)
                VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        seal_id,
                        output.relative_path,
                        output.sha256,
                        len(output.content),
                        output.content,
                    )
                    for output in outputs
                ],
            )
            connection.commit()
        return seal_id

    def resume(self, channel: ArtifactChannel, seal_id: str) -> dict[str, Any]:
        """Transfer an already sealed snapshot through the existing resumable protocol."""

        with self._connect() as connection:
            seal = connection.execute("SELECT * FROM seals WHERE id=?", (seal_id,)).fetchone()
            if seal is None:
                raise OutputSealError("Output seal was not found.")
            if seal["transfer_receipt_json"] is not None:
                return json.loads(seal["transfer_receipt_json"])
            outputs = self._outputs(connection, seal_id)
            manifest_json = self._json(
                self._manifest(
                    seal_id, seal["lease_id"], seal["epoch"], seal["operation_id"], outputs
                )
            )
            if (
                manifest_json != seal["manifest_json"]
                or hashlib.sha256(manifest_json.encode()).hexdigest() != seal["manifest_digest"]
            ):
                raise OutputSealError("Sealed output journal integrity check failed.")
        receipt = upload_output_set(
            channel,
            lease_id=seal["lease_id"],
            expected_epoch=seal["epoch"],
            outputs=outputs,
        )
        encoded = self._json(receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT transfer_receipt_json FROM seals WHERE id=?", (seal_id,)
            ).fetchone()
            if current is None:
                connection.rollback()
                raise OutputSealError("Output seal disappeared during transfer.")
            if current["transfer_receipt_json"] is None:
                connection.execute(
                    "UPDATE seals SET transfer_receipt_json=? WHERE id=?", (encoded, seal_id)
                )
                connection.commit()
                return receipt
            connection.commit()
            return json.loads(current["transfer_receipt_json"])

    @staticmethod
    def _validate_identity(lease_id: str, epoch: int) -> str:
        if not isinstance(lease_id, str) or not lease_id or len(lease_id) > 128:
            raise OutputSealError("Output seal lease identity is invalid.")
        if type(epoch) is not int or not 1 <= epoch <= 2**63 - 1:
            raise OutputSealError("Output seal lease epoch is invalid.")
        return _operation_id(lease_id)

    @staticmethod
    def _validate_termination(
        observation: dict[str, Any],
        lease_id: str,
        epoch: int,
        operation_id: str,
        now: datetime,
    ) -> None:
        try:
            observed_at = datetime.fromisoformat(observation["observed_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise OutputSealError("Termination observation is malformed.") from error
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() != timedelta(0)
        ):
            raise OutputSealError("Termination observation must use UTC timestamps.")
        if (
            not isinstance(observation.get("id"), str)
            or not observation["id"]
            or observation.get("lease_id") != lease_id
            or observation.get("epoch") != epoch
            or operation_id != _operation_id(lease_id)
            or observation.get("observed_state") != "exited"
            or observation.get("termination_confirmed") is not True
            or not isinstance(observation.get("provider_id"), str)
            or not observation["provider_id"]
            or observation.get("observation_proof") != "trusted_worker_assertion"
            or observation.get("operational_only") is not True
            or observed_at < now - MAX_TERMINATION_OBSERVATION_AGE
            or observed_at > now + timedelta(seconds=5)
        ):
            raise OutputSealError("Fresh matching termination confirmation is required.")

    @staticmethod
    def _manifest(
        seal_id: str,
        lease_id: str,
        epoch: int,
        operation_id: str,
        outputs: tuple[CollectedOutput, ...],
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "seal_id": seal_id,
            "lease_id": lease_id,
            "epoch": epoch,
            "operation_id": operation_id,
            "artifacts": [
                {
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size": len(item.content),
                }
                for item in sorted(outputs, key=lambda item: item.relative_path)
            ],
        }

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _outputs(self, connection: sqlite3.Connection, seal_id: str) -> tuple[CollectedOutput, ...]:
        rows = connection.execute(
            """SELECT relative_path, sha256, size, content FROM sealed_outputs
            WHERE seal_id=? ORDER BY relative_path""",
            (seal_id,),
        ).fetchall()
        outputs: list[CollectedOutput] = []
        for row in rows:
            content = row["content"]
            if (
                type(content) is not bytes
                or len(content) != row["size"]
                or hashlib.sha256(content).hexdigest() != row["sha256"]
            ):
                raise OutputSealError("Sealed output journal integrity check failed.")
            outputs.append(CollectedOutput(row["relative_path"], content, row["sha256"]))
        return tuple(outputs)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS seals (
                    id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL UNIQUE,
                    epoch INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    paths_json TEXT NOT NULL,
                    max_output_bytes INTEGER NOT NULL,
                    observation_id TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    transfer_receipt_json TEXT
                );
                CREATE TABLE IF NOT EXISTS sealed_outputs (
                    seal_id TEXT NOT NULL REFERENCES seals(id),
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    PRIMARY KEY (seal_id, relative_path)
                );
                """
            )
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


__all__ = ["MAX_TERMINATION_OBSERVATION_AGE", "OutputSealError", "SealedOutputJournal"]
