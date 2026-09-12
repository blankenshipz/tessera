"""One-shot worker consumer for a human-approved Kubernetes dispatch outbox.

This deliberately has no Kubernetes credentials, target discovery, or scheduler.
Its injected adapter is called only after the control plane returns the exact
claimed package for the enrolled worker and locally configured target identity.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .control_channel import HttpControlChannel
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
    """Keep one claim key durable until its response is known after a lost reply."""

    def __init__(self, root: Path, dispatch_id: str) -> None:
        self.path = root / "kubernetes-claims.sqlite3"
        self.dispatch_id = dispatch_id
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS claims (
                dispatch_id TEXT PRIMARY KEY, key TEXT NOT NULL, completed INTEGER NOT NULL
                CHECK (completed IN (0,1)))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS reports (
                sequence INTEGER PRIMARY KEY, key TEXT NOT NULL, completed INTEGER NOT NULL
                CHECK (completed IN (0,1)))"""
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

    def report_key(self) -> str:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT key FROM reports WHERE completed=0 ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                return row[0]
            sequence = connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM reports"
            ).fetchone()[0]
            key = (
                "kubernetes-dispatch-observation-"
                + hashlib.sha256(f"v1:{self.dispatch_id}:{sequence}".encode()).hexdigest()
            )
            connection.execute("INSERT INTO reports VALUES (?,?,0)", (sequence, key))
            return key

    def complete_report(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE reports SET completed=1 WHERE completed=0")


class KubernetesDispatchWorkerService:
    """Claim one dispatch and hand its unchanged control package to the adapter."""

    def __init__(
        self,
        config: KubernetesDispatchWorkerConfig,
        cluster: KubernetesClusterConfig,
        output: KubernetesOutputBinding,
        adapter: KubernetesExecutionAdapter,
        *,
        channel: Any | None = None,
    ) -> None:
        self.config = config
        self.cluster = cluster
        self.output = output
        self.adapter = adapter
        self.root = _private_directory(config.workspace_root)
        self.channel = channel or HttpControlChannel(
            config.control_url, read_private_credential(config.credential_file)
        )
        self.claims = KubernetesClaimIntentJournal(self.root, config.dispatch_id)

    def run(self) -> dict[str, Any]:
        target_digest = cluster_identity_digest(self.cluster)
        key = self.claims.key()
        response = self.channel.post(
            f"/worker/kubernetes-dispatches/{self.config.dispatch_id}/claim",
            {"target_digest": target_digest, "profile_version": self.cluster.profile},
            key,
        )
        # Complete only after an authenticated response. A lost response replays
        # this key and can neither claim another dispatch nor generate a new Job.
        self.claims.complete()
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
        status = self.adapter.submit(package)
        observation = {
            "lease_id": package.package["lease_id"],
            "expected_epoch": package.binding.epoch,
            "operation_id": package.operation_id,
            "observed_state": (
                "exited"
                if status.observed_state in {"succeeded", "failed"}
                else "unknown"
                if status.observed_state == "unknown"
                else "running"
            ),
            "observed_at": datetime.now(UTC).isoformat(),
            "termination_confirmed": False,
        }
        report_key = self.claims.report_key()
        self.channel.post("/worker/observations", observation, report_key)
        self.claims.complete_report()
        result: dict[str, Any] = {
            "dispatch_id": dispatch["id"],
            "admitted_attempt_id": dispatch["admitted_attempt_id"],
            "operation_id": package.operation_id,
            "status": status.observed_state,
            "execution_dispatched": True,
        }
        if status.observed_state == "succeeded":
            # This is target-bound collection intent only. It neither reads bytes
            # nor accepts an artifact/result.
            result["artifact_handoff"] = self.adapter.artifact_collection_handoff(package).__dict__
        return result


__all__ = [
    "KubernetesClaimIntentJournal",
    "KubernetesDispatchError",
    "KubernetesDispatchWorkerConfig",
    "KubernetesDispatchWorkerService",
]
