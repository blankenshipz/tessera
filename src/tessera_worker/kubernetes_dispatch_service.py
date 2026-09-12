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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .control_channel import ControlChannelError, HttpControlChannel
from .kubernetes_contracts import KubernetesClusterConfig, cluster_identity_digest
from .kubernetes_execution import (
    KubernetesExecutionAdapter,
    KubernetesExecutionPackage,
    KubernetesExecutionStatus,
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS dispatch_actions (
                dispatch_id TEXT NOT NULL, action TEXT NOT NULL, sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL, key TEXT NOT NULL, completed INTEGER NOT NULL
                CHECK (completed IN (0,1)),
                PRIMARY KEY (dispatch_id, action, sequence))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS dispatch_descriptors (
                dispatch_id TEXT PRIMARY KEY, descriptor_json TEXT NOT NULL)"""
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

    def pending_report(self, operation_id: str) -> tuple[dict[str, Any], str, int] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT sequence,payload_json,key FROM dispatch_reports
                WHERE dispatch_id=? AND operation_id=? AND completed=0
                ORDER BY sequence ASC LIMIT 1""",
                (self.dispatch_id, operation_id),
            ).fetchone()
        return (json.loads(row[1]), row[2], row[0]) if row is not None else None

    def action(self, action: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        """Persist a retryable authority action before its control call."""
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT sequence,payload_json,key FROM dispatch_actions
                WHERE dispatch_id=? AND action=? AND completed=0 ORDER BY sequence LIMIT 1""",
                (self.dispatch_id, action),
            ).fetchone()
            if row is not None:
                return json.loads(row[1]), row[2], row[0]
            sequence = connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM dispatch_actions "
                "WHERE dispatch_id=? AND action=?",
                (self.dispatch_id, action),
            ).fetchone()[0]
            key = (
                "kubernetes-dispatch-action-"
                + hashlib.sha256(
                    f"v1:{self.dispatch_id}:{action}:{sequence}:{encoded}".encode()
                ).hexdigest()
            )
            connection.execute(
                "INSERT INTO dispatch_actions VALUES (?,?,?,?,?,0)",
                (self.dispatch_id, action, sequence, encoded, key),
            )
            return payload, key, sequence

    def complete_action(self, action: str, sequence: int) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE dispatch_actions SET completed=1 WHERE dispatch_id=? AND action=? "
                "AND sequence=? AND completed=0",
                (self.dispatch_id, action, sequence),
            )

    def descriptor(self) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT descriptor_json FROM dispatch_descriptors WHERE dispatch_id=?",
                (self.dispatch_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def bind_descriptor(self, descriptor: dict[str, Any]) -> None:
        encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT descriptor_json FROM dispatch_descriptors WHERE dispatch_id=?",
                (self.dispatch_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO dispatch_descriptors VALUES (?,?)", (self.dispatch_id, encoded)
                )
            elif row[0] != encoded:
                raise KubernetesDispatchError(
                    "Kubernetes execution descriptor conflicts with local intent."
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
        now: Callable[[], datetime] | None = None,
        renewal_seconds: float = 10.0,
        control_outage_seconds: float = 30.0,
    ) -> None:
        self.config = config
        self.cluster = cluster
        self.output = output
        self.adapter = adapter
        self.root = _private_directory(config.workspace_root)
        self.channel = channel or HttpControlChannel(
            config.control_url, read_private_credential(config.credential_file)
        )
        if (
            not isinstance(poll_seconds, (int, float))
            or poll_seconds <= 0
            or not isinstance(renewal_seconds, (int, float))
            or renewal_seconds <= 0
            or not isinstance(control_outage_seconds, (int, float))
            or control_outage_seconds <= 0
        ):
            raise KubernetesDispatchError("Kubernetes dispatch poll interval is invalid.")
        self.sleep = sleep
        self.poll_seconds = float(poll_seconds)
        self.renewal_seconds = float(renewal_seconds)
        self.control_outage_seconds = float(control_outage_seconds)
        self.now = now or (lambda: datetime.now(UTC))
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

    def _observation(
        self,
        package: KubernetesExecutionPackage,
        status: str,
        cancellation: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        confirmed = bool(cancellation and cancellation.get("termination_confirmed"))
        return {
            "lease_id": package.package["lease_id"],
            "expected_epoch": package.binding.epoch,
            "operation_id": package.operation_id,
            "observed_state": (
                "exited"
                if status in {"succeeded", "failed"} or confirmed
                else "unknown"
                if status == "unknown"
                else "running"
            ),
            "observed_at": self.now().isoformat(),
            "termination_confirmed": confirmed,
            **({"provider_id": cancellation["provider_id"]} if confirmed and cancellation else {}),
        }

    def _report(
        self,
        package: KubernetesExecutionPackage,
        status: str,
        cancellation: dict[str, object] | None = None,
    ) -> None:
        # A terminal report must never jump over a lost earlier report.  Both
        # payloads were persisted before their calls and retain distinct keys.
        pending = self.claims.pending_report(package.operation_id)
        if pending is not None:
            payload, key, sequence = pending
            self.channel.post("/worker/observations", payload, key)
            self.claims.complete_report(package.operation_id, sequence)
        payload, key, sequence = self.claims.report(
            package.operation_id, self._observation(package, status, cancellation)
        )
        self.channel.post("/worker/observations", payload, key)
        self.claims.complete_report(package.operation_id, sequence)

    def _result(
        self,
        dispatch: dict[str, Any],
        package: KubernetesExecutionPackage,
        status: str,
        cancellation: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "dispatch_id": dispatch["id"],
            "admitted_attempt_id": dispatch["admitted_attempt_id"],
            "operation_id": package.operation_id,
            "status": status,
            "execution_dispatched": True,
        }
        if cancellation is not None:
            result["cancellation"] = cancellation
            result["reconciliation_required"] = bool(cancellation["reconciliation_required"])
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
        descriptor = self.claims.descriptor()
        created = descriptor is None
        started_at = self.now()
        if descriptor is None:
            heartbeat, heartbeat_key, heartbeat_sequence = self.claims.action("heartbeat", {})
            self.channel.post("/worker/heartbeat", heartbeat, heartbeat_key)
            self.claims.complete_action("heartbeat", heartbeat_sequence)
            response = self._claim(target_digest)
            dispatch, package = self._package(response, target_digest)
            # Keep the exact control-issued execution descriptor private and
            # immutable before any injected Kubernetes create callback.
            hard_deadline = started_at + timedelta(seconds=package.binding.active_deadline_seconds)
            self.claims.bind_descriptor(
                {
                    "hard_deadline": hard_deadline.isoformat(),
                    "response": response,
                    "target_digest": target_digest,
                }
            )
        else:
            if descriptor.get("target_digest") != target_digest or not isinstance(
                descriptor.get("response"), dict
            ):
                raise KubernetesDispatchError("Kubernetes execution descriptor is invalid.")
            response = descriptor["response"]
            dispatch, package = self._package(response, target_digest)
            try:
                hard_deadline = datetime.fromisoformat(str(descriptor["hard_deadline"]))
            except (KeyError, TypeError, ValueError) as error:
                # An older descriptor has no immutable deadline.  Fail closed:
                # recovery may observe/cancel it, but must not extend its run.
                hard_deadline = started_at
                if "hard_deadline" in descriptor:
                    raise KubernetesDispatchError(
                        "Kubernetes execution deadline is invalid."
                    ) from error
            if hard_deadline.tzinfo is None:
                raise KubernetesDispatchError("Kubernetes execution deadline is invalid.")
        next_renewal = started_at + timedelta(seconds=self.renewal_seconds)
        outage_started: datetime | None = None
        try:
            status = self.adapter.submit(package) if created else self.adapter.reconcile(package)
        except Exception:
            try:
                status = self.adapter.reconcile(package)
            except Exception:
                status = KubernetesExecutionStatus(package.operation_id, None, (), "unknown")
        cycles = 0
        while True:
            try:
                self._report(package, status.observed_state)
            except (ConnectionError, ControlChannelError):
                # Reporting can never delay local reconciliation or a stop.
                pass
            cycles += 1
            if status.observed_state in {"succeeded", "failed", "unknown"}:
                return self._result(dispatch, package, status.observed_state)
            if self.now() >= hard_deadline:
                return self._cancel(dispatch, package, "hard_deadline")
            if max_cycles is not None and cycles >= max_cycles:
                return self._result(dispatch, package, status.observed_state)
            self.sleep(self.poll_seconds)
            try:
                if self.now() >= next_renewal:
                    body, key, sequence = self.claims.action(
                        "renew",
                        {
                            "lease_id": package.binding.lease_id,
                            "expected_epoch": package.binding.epoch,
                        },
                    )
                    response = self.channel.post("/worker/leases/renew", body, key)
                    self.claims.complete_action("renew", sequence)
                    if response.get("id") != package.binding.lease_id:
                        raise KubernetesDispatchError(
                            "Control renewed a conflicting Kubernetes lease."
                        )
                    next_renewal = self.now() + timedelta(seconds=self.renewal_seconds)
                # This executable replay uses the original claim key deliberately:
                # control rechecks the complete active-grant predicate.
                response = self._claim(target_digest)
                dispatch, package = self._package(response, target_digest)
                outage_started = None
            except ConnectionError as error:
                if getattr(error, "status", None) not in {401, 403, 404, 409}:
                    outage_started = outage_started or self.now()
                    status = self.adapter.reconcile(package)
                    if self.now() - outage_started >= timedelta(
                        seconds=self.control_outage_seconds
                    ):
                        return self._cancel(dispatch, package, "control_outage")
                    continue
                return self._cancel(dispatch, package, "authority_lost")
            status = self.adapter.reconcile(package)

    def _cancel(
        self, dispatch: dict[str, Any], package: KubernetesExecutionPackage, reason: str
    ) -> dict[str, Any]:
        try:
            self.adapter.cancel(package)
        except Exception:
            # The adapter persists its cancellation fence and unknown observation
            # before reporting a callback failure; never skip the later reconcile.
            pass
        try:
            status = self.adapter.reconcile(package)
        except Exception:
            status = KubernetesExecutionStatus(package.operation_id, None, (), "unknown")
        proof = self.adapter.cancellation_proof(package) | {"reason": reason}
        try:
            self._report(package, status.observed_state, proof)
        except (ConnectionError, ControlChannelError):
            pass
        return self._result(dispatch, package, "cancel_requested", proof)


__all__ = [
    "KubernetesClaimIntentJournal",
    "KubernetesDispatchError",
    "KubernetesDispatchWorkerConfig",
    "KubernetesDispatchWorkerService",
]
