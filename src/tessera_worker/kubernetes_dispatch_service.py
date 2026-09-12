"""Durable worker supervisor for a human-approved Kubernetes dispatch outbox.

This deliberately has no Kubernetes credentials, target discovery, or scheduler.
Its injected adapter is called only after the control plane returns the exact
claimed package for the enrolled worker and locally configured target identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .control_channel import ControlChannelError, HttpControlChannel
from .kubernetes_contracts import KubernetesClusterConfig, cluster_identity_digest
from .kubernetes_execution import (
    KubernetesExecutionAdapter,
    KubernetesExecutionPackage,
    KubernetesOutputBinding,
)
from .service import DevelopmentWorkerError, _private_directory, read_private_credential


class KubernetesDispatchError(DevelopmentWorkerError):
    """The dispatch consumer cannot safely make a Kubernetes adapter effect."""


@dataclass(frozen=True)
class KubernetesDispatchWorkerConfig:
    control_url: str
    dispatch_id: str
    credential_file: Path
    workspace_root: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.dispatch_id, str)
            or not self.dispatch_id
            or len(self.dispatch_id) > 200
        ):
            raise KubernetesDispatchError("Kubernetes dispatch configuration is invalid.")


class KubernetesClaimIntentJournal:
    """Keep claim/report identities and immutable report bytes under one worker root."""

    def __init__(self, root: Path, dispatch_id: str) -> None:
        self.path = root / "kubernetes-claims.sqlite3"
        self.dispatch_id = dispatch_id
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS claims (
                dispatch_id TEXT PRIMARY KEY, key TEXT NOT NULL, completed INTEGER NOT NULL
                CHECK (completed IN (0,1)))"""
            )
            legacy = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name='reports'"
            ).fetchone()
            if (
                legacy is not None
                and connection.execute("SELECT 1 FROM reports WHERE completed=0 LIMIT 1").fetchone()
                is not None
            ):
                # Earlier journals retained only a key, so they cannot reproduce
                # the report body after a lost reply.  Do not replace that unsafe
                # unknown report with a newly observed body.
                raise KubernetesDispatchError(
                    "Legacy Kubernetes report intent cannot be replayed safely."
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS dispatch_reports (
                dispatch_id TEXT NOT NULL, operation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL, key TEXT NOT NULL, completed INTEGER NOT NULL
                CHECK (completed IN (0,1)),
                PRIMARY KEY (dispatch_id, operation_id, sequence))"""
            )
        os.chmod(self.path, 0o600)

    def key(self) -> str:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT key FROM claims WHERE dispatch_id=?", (self.dispatch_id,)
            ).fetchone()
            if row is not None:
                return row[0]
            key = (
                "kubernetes-dispatch-claim-"
                + hashlib.sha256(f"v1:{self.dispatch_id}".encode()).hexdigest()
            )
            connection.execute("INSERT INTO claims VALUES (?,?,0)", (self.dispatch_id, key))
            return key

    def complete(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE claims SET completed=1 WHERE dispatch_id=?", (self.dispatch_id,)
            )

    def report(self, operation_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        """Return the pending report verbatim, or durably create its next identity."""
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT sequence,payload_json,key FROM dispatch_reports
                WHERE dispatch_id=? AND operation_id=? AND completed=0
                ORDER BY sequence ASC LIMIT 1""",
                (self.dispatch_id, operation_id),
            ).fetchone()
            if row is not None:
                return json.loads(row[1]), row[2], row[0]
            sequence = connection.execute(
                """SELECT coalesce(max(sequence),0)+1 FROM dispatch_reports
                WHERE dispatch_id=? AND operation_id=?""",
                (self.dispatch_id, operation_id),
            ).fetchone()[0]
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            key = (
                "kubernetes-dispatch-observation-"
                + hashlib.sha256(
                    f"v2:{self.dispatch_id}:{operation_id}:{sequence}:{encoded}".encode()
                ).hexdigest()
            )
            connection.execute(
                """INSERT INTO dispatch_reports
                (dispatch_id,operation_id,sequence,payload_json,key,completed)
                VALUES (?,?,?,?,?,0)""",
                (self.dispatch_id, operation_id, sequence, encoded, key),
            )
            return payload, key, sequence

    def complete_report(self, operation_id: str, sequence: int) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """UPDATE dispatch_reports SET completed=1
                WHERE dispatch_id=? AND operation_id=? AND sequence=? AND completed=0""",
                (self.dispatch_id, operation_id, sequence),
            )


class KubernetesDispatchWorkerService:
    """Supervise one dispatch until terminal evidence or an explicit bounded return."""

    def __init__(
        self,
        config: KubernetesDispatchWorkerConfig,
        cluster: KubernetesClusterConfig,
        output: KubernetesOutputBinding,
        adapter: KubernetesExecutionAdapter,
        *,
        channel: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 1.0,
    ) -> None:
        self.config = config
        self.cluster = cluster
        self.output = output
        self.adapter = adapter
        self.root = _private_directory(config.workspace_root)
        self.channel = channel or HttpControlChannel(
            config.control_url, read_private_credential(config.credential_file)
        )
        if not isinstance(poll_seconds, (int, float)) or poll_seconds <= 0:
            raise KubernetesDispatchError("Kubernetes dispatch poll interval is invalid.")
        self.sleep = sleep
        self.poll_seconds = float(poll_seconds)
        self.claims = KubernetesClaimIntentJournal(self.root, config.dispatch_id)

    def _claim(self, target_digest: str) -> dict[str, Any]:
        response = self.channel.post(
            f"/worker/kubernetes-dispatches/{self.config.dispatch_id}/claim",
            {"target_digest": target_digest, "profile_version": self.cluster.profile},
            self.claims.key(),
        )
        # Complete only after an authenticated response. A lost response replays
        # this key and can neither claim another dispatch nor generate a new Job.
        self.claims.complete()
        return response

    def _package(
        self, response: dict[str, Any], target_digest: str
    ) -> tuple[dict[str, Any], KubernetesExecutionPackage]:
        dispatch = response.get("dispatch")
        if (
            not isinstance(dispatch, dict)
            or dispatch.get("target_digest") != target_digest
            or dispatch.get("profile_version") != self.cluster.profile
            or dispatch.get("state") != "claimed"
        ):
            raise KubernetesDispatchError("Control returned an invalid Kubernetes dispatch claim.")
        package = KubernetesExecutionPackage.from_admitted_package(response, self.output)
        if package.operation_id != dispatch.get("operation_id"):
            raise KubernetesDispatchError(
                "Claimed Kubernetes package operation conflicts with dispatch."
            )
        return dispatch, package

    @staticmethod
    def _observation(package: KubernetesExecutionPackage, status: str) -> dict[str, Any]:
        return {
            "lease_id": package.package["lease_id"],
            "expected_epoch": package.binding.epoch,
            "operation_id": package.operation_id,
            "observed_state": (
                "exited"
                if status in {"succeeded", "failed"}
                else "unknown"
                if status == "unknown"
                else "running"
            ),
            "observed_at": datetime.now(UTC).isoformat(),
            "termination_confirmed": False,
        }

    def _report(self, package: KubernetesExecutionPackage, status: str) -> None:
        payload, key, sequence = self.claims.report(
            package.operation_id, self._observation(package, status)
        )
        self.channel.post("/worker/observations", payload, key)
        self.claims.complete_report(package.operation_id, sequence)

    def _result(
        self, dispatch: dict[str, Any], package: KubernetesExecutionPackage, status: str
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "dispatch_id": dispatch["id"],
            "admitted_attempt_id": dispatch["admitted_attempt_id"],
            "operation_id": package.operation_id,
            "status": status,
            "execution_dispatched": True,
        }
        if status == "succeeded":
            # This is target-bound collection intent only. It neither reads bytes
            # nor accepts an artifact/result.
            result["artifact_handoff"] = self.adapter.artifact_collection_handoff(package).__dict__
        return result

    def run(self, *, max_cycles: int | None = None) -> dict[str, Any]:
        """Claim, submit once, then recheck authority and reconcile until terminal.

        ``max_cycles`` is an explicit test/embedding bound; normal service use
        keeps supervising. A disconnected control channel does not authorize a
        replacement submit: the durable adapter performs observation-only
        reconciliation and the caller may restart this service to retry the same
        claim/report identities.
        """
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise KubernetesDispatchError("Kubernetes dispatch cycle limit is invalid.")
        target_digest = cluster_identity_digest(self.cluster)
        response = self._claim(target_digest)
        dispatch, package = self._package(response, target_digest)
        try:
            lease_expires_at = datetime.fromisoformat(response["lease"]["expires_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise KubernetesDispatchError("Claimed Kubernetes lease is invalid.") from error
        if lease_expires_at.tzinfo is None:
            raise KubernetesDispatchError("Claimed Kubernetes lease is invalid.")
        status = self.adapter.submit(package)
        cycles = 0
        while True:
            self._report(package, status.observed_state)
            cycles += 1
            if status.observed_state in {"succeeded", "failed", "unknown"}:
                return self._result(dispatch, package, status.observed_state)
            if datetime.now(UTC) >= lease_expires_at:
                self.adapter.cancel(package)
                status = self.adapter.reconcile(package)
                self._report(package, status.observed_state)
                return self._result(dispatch, package, "cancel_requested")
            if max_cycles is not None and cycles >= max_cycles:
                return self._result(dispatch, package, status.observed_state)
            self.sleep(self.poll_seconds)
            try:
                # This uses the original claim key deliberately. The control
                # replay guard re-evaluates grant, lease, expiry, and worker state.
                response = self._claim(target_digest)
                dispatch, package = self._package(response, target_digest)
                lease_expires_at = datetime.fromisoformat(response["lease"]["expires_at"])
                if lease_expires_at.tzinfo is None:
                    raise KubernetesDispatchError("Claimed Kubernetes lease is invalid.")
            except ControlChannelError as error:
                if error.status not in {401, 403, 404, 409}:
                    # Disconnection permits only reconciliation, never a new Job.
                    status = self.adapter.reconcile(package)
                    continue
                self.adapter.cancel(package)
                status = self.adapter.reconcile(package)
                self._report(package, status.observed_state)
                return self._result(dispatch, package, "cancel_requested")
            status = self.adapter.reconcile(package)


__all__ = [
    "KubernetesClaimIntentJournal",
    "KubernetesDispatchError",
    "KubernetesDispatchWorkerConfig",
    "KubernetesDispatchWorkerService",
]
