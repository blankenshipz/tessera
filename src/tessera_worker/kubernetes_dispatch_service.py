"""Durable worker supervisor for a human-approved Kubernetes dispatch outbox.

This deliberately has no Kubernetes credentials, target discovery, or scheduler.
Its injected adapter is called only after the control plane returns the exact
claimed package for the enrolled worker and locally configured target identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
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

_CLOCK_ROLLBACK_TOLERANCE = timedelta(seconds=1)


class KubernetesDispatchError(DevelopmentWorkerError):
    """The dispatch consumer cannot safely make a Kubernetes adapter effect."""


class _ClockRollback(BaseException):
    """Escape broad adapter exception handlers when the authority clock regresses."""


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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS dispatch_supervision (
                dispatch_id TEXT PRIMARY KEY, lease_expires_at TEXT NOT NULL,
                outage_expires_at TEXT, last_wall_at TEXT)"""
            )
            supervision_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(dispatch_supervision)")
            }
            if "last_wall_at" not in supervision_columns:
                connection.execute("ALTER TABLE dispatch_supervision ADD COLUMN last_wall_at TEXT")
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

    def complete_renewal(self, sequence: int, lease_expires_at: datetime) -> None:
        """Atomically retain renewed authority and replay completion.

        A renewal proves only that one lease command completed.  It must not erase
        an independently retained control-outage bound; the full control recovery
        path clears that bound only after the claim is revalidated.
        """
        encoded = lease_expires_at.astimezone(UTC).isoformat()
        with sqlite3.connect(self.path) as connection:
            if (
                connection.execute(
                    "UPDATE dispatch_supervision SET lease_expires_at=? WHERE dispatch_id=?",
                    (encoded, self.dispatch_id),
                ).rowcount
                != 1
                or connection.execute(
                    "UPDATE dispatch_actions SET completed=1 WHERE dispatch_id=? "
                    "AND action='renew' AND sequence=? AND completed=0",
                    (self.dispatch_id, sequence),
                ).rowcount
                != 1
            ):
                raise KubernetesDispatchError("Kubernetes renewal intent disappeared.")

    def complete_heartbeat(self, sequence: int) -> None:
        """Atomically retain heartbeat completion without changing supervision bounds."""
        with sqlite3.connect(self.path) as connection:
            if (
                connection.execute(
                    "UPDATE dispatch_actions SET completed=1 WHERE dispatch_id=? "
                    "AND action='heartbeat' AND sequence=? AND completed=0",
                    (self.dispatch_id, sequence),
                ).rowcount
                != 1
            ):
                raise KubernetesDispatchError("Kubernetes heartbeat intent disappeared.")

    def pending_action(self, action: str) -> tuple[dict[str, Any], str, int] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT sequence,payload_json,key FROM dispatch_actions "
                "WHERE dispatch_id=? AND action=? AND completed=0 ORDER BY sequence LIMIT 1",
                (self.dispatch_id, action),
            ).fetchone()
        return (json.loads(row[1]), row[2], row[0]) if row is not None else None

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

    def bind_supervision(self, lease_expires_at: datetime) -> None:
        encoded = lease_expires_at.astimezone(UTC).isoformat()
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT lease_expires_at FROM dispatch_supervision WHERE dispatch_id=?",
                (self.dispatch_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO dispatch_supervision "
                    "(dispatch_id,lease_expires_at,outage_expires_at,last_wall_at) "
                    "VALUES (?,?,NULL,NULL)",
                    (self.dispatch_id, encoded),
                )

    def clock_watermark(self) -> datetime | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT last_wall_at FROM dispatch_supervision WHERE dispatch_id=?",
                (self.dispatch_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return _utc_deadline(row[0])
        except (TypeError, ValueError) as error:
            raise KubernetesDispatchError("Kubernetes supervision clock is invalid.") from error

    def record_clock_watermark(self, observed_at: datetime) -> None:
        """Persist the greatest raw wall observation for restart rollback detection."""
        observed_at = observed_at.astimezone(UTC)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT last_wall_at FROM dispatch_supervision WHERE dispatch_id=?",
                (self.dispatch_id,),
            ).fetchone()
            if row is None:
                return
            if row[0] is not None:
                try:
                    previous = _utc_deadline(row[0])
                except (TypeError, ValueError) as error:
                    raise KubernetesDispatchError(
                        "Kubernetes supervision clock is invalid."
                    ) from error
                if previous >= observed_at:
                    return
            connection.execute(
                "UPDATE dispatch_supervision SET last_wall_at=? WHERE dispatch_id=?",
                (observed_at.isoformat(), self.dispatch_id),
            )

    def supervision(self) -> tuple[datetime, datetime | None] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT lease_expires_at,outage_expires_at FROM dispatch_supervision "
                "WHERE dispatch_id=?",
                (self.dispatch_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            lease = _utc_deadline(row[0])
            outage = _utc_deadline(row[1]) if row[1] else None
        except (TypeError, ValueError) as error:
            raise KubernetesDispatchError("Kubernetes supervision deadline is invalid.") from error
        return lease, outage

    def begin_outage(self, outage_expires_at: datetime) -> datetime:
        encoded = outage_expires_at.astimezone(UTC).isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE dispatch_supervision SET outage_expires_at=coalesce(outage_expires_at, ?) "
                "WHERE dispatch_id=?",
                (encoded, self.dispatch_id),
            )
            row = connection.execute(
                "SELECT outage_expires_at FROM dispatch_supervision WHERE dispatch_id=?",
                (self.dispatch_id,),
            ).fetchone()
        if row is None or row[0] is None:
            raise KubernetesDispatchError("Kubernetes supervision intent disappeared.")
        try:
            return _utc_deadline(row[0])
        except (TypeError, ValueError) as error:
            raise KubernetesDispatchError(
                "Kubernetes supervision outage deadline is invalid."
            ) from error

    def clear_outage(self) -> None:
        with sqlite3.connect(self.path) as connection:
            if (
                connection.execute(
                    "UPDATE dispatch_supervision SET outage_expires_at=NULL WHERE dispatch_id=?",
                    (self.dispatch_id,),
                ).rowcount
                != 1
            ):
                raise KubernetesDispatchError("Kubernetes supervision intent disappeared.")


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
        monotonic: Callable[[], float] = time.monotonic,
        renewal_seconds: float = 10.0,
        heartbeat_seconds: float = 30.0,
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
            or not isinstance(heartbeat_seconds, (int, float))
            or heartbeat_seconds <= 0
            or not isinstance(control_outage_seconds, (int, float))
            or control_outage_seconds <= 0
        ):
            raise KubernetesDispatchError("Kubernetes dispatch poll interval is invalid.")
        self.sleep = sleep
        self.poll_seconds = float(poll_seconds)
        self.renewal_seconds = float(renewal_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.control_outage_seconds = float(control_outage_seconds)
        self.now = now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic
        self.claims = KubernetesClaimIntentJournal(self.root, config.dispatch_id)
        self._clock_anchor_wall: datetime | None = None
        self._clock_anchor_monotonic: float | None = None
        self._clock_last_raw: datetime | None = None
        self._clock_last_monotonic: float | None = None
        self._clock_effective: datetime | None = None
        self._clock_rollback_pending = False
        self._clock_rollback_signalled = False
        self._clock_armed = False
        self._active_dispatch: dict[str, Any] | None = None
        self._active_package: KubernetesExecutionPackage | None = None
        self._active_bounds: tuple[datetime, datetime, datetime | None] | None = None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise KubernetesDispatchError("Kubernetes supervision clock is invalid.")
        return value.astimezone(UTC)

    def _start_clock(self) -> None:
        raw = self._as_utc(self.now())
        monotonic = float(self.monotonic())
        if not math.isfinite(monotonic):
            raise KubernetesDispatchError("Kubernetes monotonic clock is invalid.")
        persisted = self.claims.clock_watermark()
        self._clock_anchor_wall = max(raw, persisted) if persisted is not None else raw
        self._clock_anchor_monotonic = monotonic
        self._clock_last_raw = raw
        self._clock_last_monotonic = monotonic
        self._clock_effective = self._clock_anchor_wall
        self._clock_rollback_pending = (
            persisted is not None and raw < persisted - _CLOCK_ROLLBACK_TOLERANCE
        )
        self._clock_rollback_signalled = False
        self._clock_armed = False

    def _clock_now(self) -> datetime:
        if self._clock_anchor_wall is None or self._clock_anchor_monotonic is None:
            self._start_clock()
        assert self._clock_anchor_wall is not None
        assert self._clock_anchor_monotonic is not None
        assert self._clock_last_raw is not None
        assert self._clock_last_monotonic is not None
        assert self._clock_effective is not None
        raw = self._as_utc(self.now())
        monotonic = float(self.monotonic())
        if not math.isfinite(monotonic):
            self._clock_rollback_pending = True
            monotonic = self._clock_last_monotonic
        if (
            monotonic < self._clock_last_monotonic
            or raw < self._clock_last_raw - _CLOCK_ROLLBACK_TOLERANCE
        ):
            self._clock_rollback_pending = True
        elapsed = max(0.0, monotonic - self._clock_anchor_monotonic)
        monotonic_wall = self._clock_anchor_wall + timedelta(seconds=elapsed)
        self._clock_effective = max(self._clock_effective, monotonic_wall, raw)
        self._clock_last_raw = max(self._clock_last_raw, raw)
        self._clock_last_monotonic = max(self._clock_last_monotonic, monotonic)
        self.claims.record_clock_watermark(raw)
        if (
            self._clock_armed
            and self._clock_rollback_pending
            and not self._clock_rollback_signalled
        ):
            self._clock_rollback_signalled = True
            raise _ClockRollback
        return self._clock_effective

    def _arm_clock(self, dispatch: dict[str, Any], package: KubernetesExecutionPackage) -> None:
        self._active_dispatch = dispatch
        self._active_package = package
        self._clock_armed = True
        self._clock_now()

    def _set_active_bounds(
        self,
        hard_deadline: datetime,
        lease_deadline: datetime,
        outage_deadline: datetime | None,
    ) -> None:
        self._active_bounds = (hard_deadline, lease_deadline, outage_deadline)

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        key: str,
        deadline: datetime | None = None,
    ) -> dict[str, Any]:
        if deadline is None:
            return self.channel.post(path, body, key)
        remaining = (deadline - self._clock_now()).total_seconds()
        if remaining <= 0:
            raise ControlChannelError("control response deadline exceeded")
        if isinstance(self.channel, HttpControlChannel):
            response = self.channel.post(
                path,
                body,
                key,
                timeout_seconds=min(self.channel.timeout_seconds, remaining),
            )
        else:
            # Injected channels are test/embedding seams. Production transport above
            # receives the exact remaining timeout; legacy seams retain compatibility.
            response = self.channel.post(path, body, key)
        if self._clock_now() >= deadline:
            # A response that arrives after the authority/disconnect bound remains
            # uncertain. Its durable key is replayable, but it cannot revive work.
            raise ControlChannelError("control response deadline exceeded")
        return response

    def _claim(self, target_digest: str, deadline: datetime | None = None) -> dict[str, Any]:
        response = self._post(
            f"/worker/kubernetes-dispatches/{self.config.dispatch_id}/claim",
            {"target_digest": target_digest, "profile_version": self.cluster.profile},
            self.claims.key(),
            deadline,
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
            "observed_at": self._clock_now().isoformat(),
            "termination_confirmed": confirmed,
            **({"provider_id": cancellation["provider_id"]} if confirmed and cancellation else {}),
        }

    def _report(
        self,
        package: KubernetesExecutionPackage,
        status: str,
        cancellation: dict[str, object] | None = None,
        deadline: datetime | None = None,
    ) -> None:
        # A terminal report must never jump over a lost earlier report.  Both
        # payloads were persisted before their calls and retain distinct keys.
        pending = self.claims.pending_report(package.operation_id)
        if pending is not None:
            payload, key, sequence = pending
            self._post("/worker/observations", payload, key, deadline)
            self.claims.complete_report(package.operation_id, sequence)
        payload, key, sequence = self.claims.report(
            package.operation_id, self._observation(package, status, cancellation)
        )
        self._post("/worker/observations", payload, key, deadline)
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
        elif status == "unknown":
            # A bounded caller return is not a terminal outcome.  The same worker
            # root owns recovery through the immutable Job UID and descriptor.
            result["reconciliation_required"] = True
            result["recovery_owner"] = "worker"
        if status == "succeeded":
            # This is target-bound collection intent only. It neither reads bytes
            # nor accepts an artifact/result.
            result["artifact_handoff"] = self.adapter.artifact_collection_handoff(package).__dict__
        return result

    def _heartbeat(
        self,
        *,
        deadline: datetime | None = None,
        refresh_stale_replay: bool = True,
    ) -> dict[str, Any]:
        body, key, sequence = self.claims.action("heartbeat", {})
        response = self._post("/worker/heartbeat", body, key, deadline)
        try:
            heartbeat_at = _utc_deadline(response["heartbeat_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise KubernetesDispatchError(
                "Control returned an invalid worker heartbeat."
            ) from error
        self.claims.complete_heartbeat(sequence)
        if refresh_stale_replay and heartbeat_at < self._clock_now() - timedelta(
            seconds=self.heartbeat_seconds
        ):
            # A lost response may replay a once-valid control mutation after its
            # freshness has elapsed. Complete that exact pending key, then issue
            # one new durable heartbeat rather than treating the old receipt as
            # current worker health.
            return self._heartbeat(deadline=deadline, refresh_stale_replay=False)
        return response

    def _reconcile(
        self, package: KubernetesExecutionPackage, deadline: datetime
    ) -> KubernetesExecutionStatus:
        """Turn any mid-supervision provider observation failure into unknown."""
        try:
            return self.adapter.reconcile(
                package, timeout_seconds=max(0.0, (deadline - self._clock_now()).total_seconds())
            )
        except Exception:
            # The adapter has already persisted unknown where possible.  Even a
            # local journal/validation failure must not make this supervisor
            # abandon expiry, cancellation, or the original operation identity.
            return KubernetesExecutionStatus(package.operation_id, None, (), "unknown")

    @staticmethod
    def _same_package(
        current: KubernetesExecutionPackage, claimed: KubernetesExecutionPackage
    ) -> None:
        if (
            current.package_digest != claimed.package_digest
            or current.operation_id != claimed.operation_id
            or current.binding.lease_id != claimed.binding.lease_id
            or current.binding.epoch != claimed.binding.epoch
            or current.hard_expires_at != claimed.hard_expires_at
        ):
            raise KubernetesDispatchError("Control returned conflicting Kubernetes authority.")

    def _refresh_claim(
        self,
        target_digest: str,
        package: KubernetesExecutionPackage,
        deadline: datetime | None = None,
    ) -> dict[str, Any]:
        response = self._claim(target_digest, deadline)
        dispatch, claimed = self._package(response, target_digest)
        self._same_package(package, claimed)
        return dispatch

    def _renewed_expiry(
        self,
        response: dict[str, Any],
        package: KubernetesExecutionPackage,
        hard_deadline: datetime,
    ) -> datetime:
        if (
            response.get("id") != package.binding.lease_id
            or response.get("grant_id") != package.package.get("grant_id")
            or response.get("worker_principal_id") != package.package.get("worker_principal_id")
            or response.get("epoch") != package.binding.epoch
        ):
            raise KubernetesDispatchError("Control renewed a conflicting Kubernetes lease.")
        try:
            expiry = _utc_deadline(response["expires_at"])
            hard_expiry = _utc_deadline(response["hard_expires_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise KubernetesDispatchError("Control renewed an invalid Kubernetes lease.") from error
        if (
            expiry <= self._clock_now()
            or expiry > hard_deadline
            or hard_expiry != package.hard_expires_at
        ):
            raise KubernetesDispatchError("Control renewed an invalid Kubernetes lease.")
        return expiry

    def _outage_deadline(self, hard_deadline: datetime) -> datetime:
        return self.claims.begin_outage(
            min(
                hard_deadline,
                self._clock_now() + timedelta(seconds=self.control_outage_seconds),
            )
        )

    @staticmethod
    def _active_deadline(
        hard_deadline: datetime,
        lease_deadline: datetime,
        outage_deadline: datetime | None,
    ) -> datetime:
        return min(
            hard_deadline,
            lease_deadline,
            outage_deadline if outage_deadline is not None else hard_deadline,
        )

    def _bounded_sleep(
        self,
        hard_deadline: datetime,
        lease_deadline: datetime,
        outage_deadline: datetime | None,
        next_heartbeat: datetime,
        next_renewal: datetime,
    ) -> None:
        now = self._clock_now()
        wake_at = min(
            self._active_deadline(hard_deadline, lease_deadline, outage_deadline),
            next_heartbeat,
            next_renewal,
            now + timedelta(seconds=self.poll_seconds),
        )
        remaining = (wake_at - now).total_seconds()
        if remaining > 0:
            self.sleep(remaining)

    @staticmethod
    def _deadline_reason(
        now: datetime,
        hard_deadline: datetime,
        lease_deadline: datetime,
        outage_deadline: datetime | None,
    ) -> str | None:
        if now >= hard_deadline:
            return "hard_deadline"
        if outage_deadline is not None and now >= outage_deadline:
            return "control_outage"
        if now >= lease_deadline:
            return "lease_expiry"
        return None

    def run(self, *, max_cycles: int | None = None) -> dict[str, Any]:
        """Run with a monotonic process clock and fail closed on wall rollback."""
        self._active_dispatch = None
        self._active_package = None
        self._active_bounds = None
        self._start_clock()
        try:
            return self._run(max_cycles=max_cycles)
        except _ClockRollback:
            if self._active_dispatch is None or self._active_package is None:
                raise KubernetesDispatchError(
                    "Kubernetes wall clock rolled back before authority was bound."
                ) from None
            package = self._active_package
            hard_deadline = min(
                package.hard_expires_at,
                package.lease_issued_at + timedelta(seconds=package.approved_duration_seconds),
            )
            supervision = self.claims.supervision()
            if supervision is None:
                lease_deadline, outage_deadline = package.lease_expires_at, None
            else:
                lease_deadline, outage_deadline = supervision
            assert self._clock_effective is not None
            # Existing deadline precedence remains authoritative if monotonic
            # elapsed time reached a bound while the wall clock moved backward.
            reason = (
                self._deadline_reason(
                    self._clock_effective,
                    hard_deadline,
                    min(lease_deadline, hard_deadline),
                    outage_deadline,
                )
                or "clock_rollback"
            )
            return self._cancel(self._active_dispatch, package, reason)
        finally:
            self._clock_armed = False

    def _run(self, *, max_cycles: int | None = None) -> dict[str, Any]:
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
        started_at = self._clock_now()
        if descriptor is None:
            # A worker heartbeat is distinct from the execution lease.  It must
            # establish registry freshness before any executable claim response.
            self._heartbeat()
            response = self._claim(target_digest)
            dispatch, package = self._package(response, target_digest)
            self._arm_clock(dispatch, package)
            # Keep the exact control-issued execution descriptor private and
            # immutable before any injected Kubernetes create callback.
            hard_deadline = min(
                package.hard_expires_at,
                package.lease_issued_at + timedelta(seconds=package.approved_duration_seconds),
            )
            # A Kubernetes Job's relative active deadline cannot be extended by
            # later lease renewals. Keep that provider backstop at the immutable
            # hard bound; the supervisor separately enforces the renewable lease.
            remaining_seconds = math.floor((hard_deadline - self._clock_now()).total_seconds())
            provider_deadline_seconds = min(
                package.binding.active_deadline_seconds, max(1, remaining_seconds)
            )
            self.claims.bind_descriptor(
                {
                    "hard_deadline": hard_deadline.isoformat(),
                    "provider_active_deadline_seconds": provider_deadline_seconds,
                    "response": response,
                    "target_digest": target_digest,
                }
            )
            self.claims.bind_supervision(package.lease_expires_at)
            package = replace(
                package,
                binding=replace(
                    package.binding,
                    active_deadline_seconds=provider_deadline_seconds,
                ),
            )
            lease_deadline, outage_deadline = package.lease_expires_at, None
            provider_duration_available = remaining_seconds >= 1
        else:
            if descriptor.get("target_digest") != target_digest or not isinstance(
                descriptor.get("response"), dict
            ):
                raise KubernetesDispatchError("Kubernetes execution descriptor is invalid.")
            response = descriptor["response"]
            dispatch, package = self._package(response, target_digest)
            try:
                hard_deadline = _utc_deadline(descriptor["hard_deadline"])
            except (KeyError, TypeError, ValueError) as error:
                # An older descriptor has no immutable deadline.  Fail closed:
                # recovery may observe/cancel it, but must not extend its run.
                hard_deadline = started_at
                if "hard_deadline" in descriptor:
                    raise KubernetesDispatchError(
                        "Kubernetes execution deadline is invalid."
                    ) from error
            hard_deadline = min(
                hard_deadline,
                package.hard_expires_at,
                package.lease_issued_at + timedelta(seconds=package.approved_duration_seconds),
            )
            provider_seconds = descriptor.get("provider_active_deadline_seconds")
            if provider_seconds is not None:
                if (
                    type(provider_seconds) is not int
                    or not 1 <= provider_seconds <= package.binding.active_deadline_seconds
                ):
                    raise KubernetesDispatchError(
                        "Kubernetes provider execution deadline is invalid."
                    )
                package = replace(
                    package,
                    binding=replace(package.binding, active_deadline_seconds=provider_seconds),
                )
            supervision = self.claims.supervision()
            if supervision is None:
                # An older descriptor did not retain mutable lease/outage bounds.
                # Seed only from its control-issued response; never from restart time.
                self.claims.bind_supervision(package.lease_expires_at)
                supervision = (package.lease_expires_at, None)
            lease_deadline, outage_deadline = supervision
            lease_deadline = min(lease_deadline, hard_deadline)
            # Bind only after restoring the exact provider deadline persisted in
            # the adapter intent, so rollback cancellation reconciles that same
            # Job identity rather than an unnarrowed reconstruction.
            self._arm_clock(dispatch, package)
            pending_renewal = self.claims.pending_action("renew")
            expired_before_recovery = self._deadline_reason(
                self._clock_now(), hard_deadline, lease_deadline, outage_deadline
            )
            if expired_before_recovery is not None and not (
                expired_before_recovery == "lease_expiry" and pending_renewal is not None
            ):
                return self._cancel(dispatch, package, expired_before_recovery)
            active_deadline = (
                # The pending key proves that renewal was attempted while the
                # cached lease was live. Its reply may already have committed at
                # control, so one heartbeat and exact replay must not be fenced by
                # the superseded lease cache.  It does not supersede a separately
                # retained outage deadline.
                min(hard_deadline, outage_deadline or hard_deadline)
                if expired_before_recovery == "lease_expiry"
                else self._active_deadline(hard_deadline, lease_deadline, outage_deadline)
            )
            try:
                self._heartbeat(deadline=active_deadline)
            except KubernetesDispatchError:
                return self._cancel(dispatch, package, "authority_lost")
            except ConnectionError as error:
                if getattr(error, "status", None) in {401, 403, 404, 409}:
                    return self._cancel(dispatch, package, "authority_lost")
                outage_deadline = self._outage_deadline(hard_deadline)
            else:
                # Registry freshness alone does not renew execution authority.
                # First replay any uncertain renewal using its exact durable key;
                # a restart must not discard a committed lost response and then
                # incorrectly enforce the older cached lease expiry.
                try:
                    if pending_renewal is not None:
                        body, key, sequence = pending_renewal
                        renewal = self._post("/worker/leases/renew", body, key, active_deadline)
                        lease_deadline = self._renewed_expiry(renewal, package, hard_deadline)
                        self.claims.complete_renewal(sequence, lease_deadline)
                    active_deadline = self._active_deadline(
                        hard_deadline, lease_deadline, outage_deadline
                    )
                    dispatch = self._refresh_claim(target_digest, package, active_deadline)
                except KubernetesDispatchError:
                    return self._cancel(dispatch, package, "authority_lost")
                except ConnectionError as error:
                    if getattr(error, "status", None) in {401, 403, 404, 409}:
                        return self._cancel(dispatch, package, "authority_lost")
                    outage_deadline = self._outage_deadline(hard_deadline)
                else:
                    self.claims.clear_outage()
                    outage_deadline = None
            provider_duration_available = True
        next_renewal = started_at + timedelta(seconds=self.renewal_seconds)
        next_heartbeat = started_at + timedelta(seconds=self.heartbeat_seconds)
        reason = self._deadline_reason(
            self._clock_now(), hard_deadline, lease_deadline, outage_deadline
        )
        if reason is None and created and not provider_duration_available:
            # Kubernetes requires an integer activeDeadlineSeconds. Do not round
            # a sub-second authority remainder upward and create beyond it.
            reason = "lease_expiry" if lease_deadline <= hard_deadline else "hard_deadline"
        if reason is not None and created:
            # Delayed first startup has no authority to create even briefly.
            return self._cancel(dispatch, package, reason)
        try:
            active_deadline = self._active_deadline(hard_deadline, lease_deadline, outage_deadline)
            status = (
                self.adapter.submit(
                    package,
                    timeout_seconds=max(0.0, (active_deadline - self._clock_now()).total_seconds()),
                )
                if created
                else self._reconcile(package, active_deadline)
            )
        except Exception:
            status = self._reconcile(package, active_deadline)
        if reason is not None:
            # Recovery remains observation-only before the already-owned Job is
            # cancelled; this cannot create replacement work.
            return self._cancel(dispatch, package, reason)
        cycles = 0
        while True:
            active_deadline = self._active_deadline(hard_deadline, lease_deadline, outage_deadline)
            try:
                self._report(package, status.observed_state, deadline=active_deadline)
            except (ConnectionError, ControlChannelError):
                # Reporting can never delay local reconciliation or a stop.
                pass
            cycles += 1
            reason = self._deadline_reason(
                self._clock_now(), hard_deadline, lease_deadline, outage_deadline
            )
            if reason is not None:
                return self._cancel(dispatch, package, reason)
            if status.observed_state in {"succeeded", "failed"}:
                return self._result(dispatch, package, status.observed_state)
            if status.observed_state == "unknown":
                # Unknown is an authoritative adverse observation, not a reason to
                # stop enforcing cancellation.  Recheck the same claim before a
                # bounded return so a revocation/cancellation intent still reaches
                # the adapter's Job-UID-scoped stop/reconciliation path.
                try:
                    dispatch = self._refresh_claim(target_digest, package, active_deadline)
                    self.claims.clear_outage()
                    outage_deadline = None
                except KubernetesDispatchError:
                    return self._cancel(dispatch, package, "authority_lost")
                except ConnectionError as error:
                    if getattr(error, "status", None) in {401, 403, 404, 409}:
                        return self._cancel(dispatch, package, "authority_lost")
                    outage_deadline = self._outage_deadline(hard_deadline)
                    if self._clock_now() >= outage_deadline:
                        return self._cancel(dispatch, package, "control_outage")
            if max_cycles is not None and cycles >= max_cycles:
                return self._result(dispatch, package, status.observed_state)
            self._bounded_sleep(
                hard_deadline,
                lease_deadline,
                outage_deadline,
                next_heartbeat,
                next_renewal,
            )
            try:
                reason = self._deadline_reason(
                    self._clock_now(), hard_deadline, lease_deadline, outage_deadline
                )
                if reason is not None:
                    return self._cancel(dispatch, package, reason)
                active_deadline = self._active_deadline(
                    hard_deadline, lease_deadline, outage_deadline
                )
                if self._clock_now() >= next_heartbeat:
                    try:
                        self._heartbeat(deadline=active_deadline)
                    finally:
                        # A failed call retains its durable action/key, but it must
                        # not become a zero-sleep retry loop.
                        next_heartbeat = self._clock_now() + timedelta(
                            seconds=self.heartbeat_seconds
                        )
                if self._clock_now() >= next_renewal:
                    try:
                        body, key, sequence = self.claims.action(
                            "renew",
                            {
                                "lease_id": package.binding.lease_id,
                                "expected_epoch": package.binding.epoch,
                            },
                        )
                        response = self._post("/worker/leases/renew", body, key, active_deadline)
                        lease_deadline = self._renewed_expiry(response, package, hard_deadline)
                        self.claims.complete_renewal(sequence, lease_deadline)
                    finally:
                        # Preserve an uncertain renewal for exact replay while
                        # pacing the next attempt from completion/failure time.
                        next_renewal = self._clock_now() + timedelta(seconds=self.renewal_seconds)
                # This executable replay uses the original claim key deliberately:
                # control rechecks the complete active-grant predicate.
                active_deadline = self._active_deadline(
                    hard_deadline, lease_deadline, outage_deadline
                )
                dispatch = self._refresh_claim(target_digest, package, active_deadline)
                self.claims.clear_outage()
                outage_deadline = None
            except KubernetesDispatchError:
                return self._cancel(dispatch, package, "authority_lost")
            except ConnectionError as error:
                if getattr(error, "status", None) not in {401, 403, 404, 409}:
                    outage_deadline = self._outage_deadline(hard_deadline)
                    status = self._reconcile(
                        package,
                        self._active_deadline(hard_deadline, lease_deadline, outage_deadline),
                    )
                    if self._clock_now() >= outage_deadline:
                        return self._cancel(dispatch, package, "control_outage")
                    continue
                return self._cancel(dispatch, package, "authority_lost")
            status = self._reconcile(
                package,
                self._active_deadline(hard_deadline, lease_deadline, outage_deadline),
            )

    def _cancel(
        self, dispatch: dict[str, Any], package: KubernetesExecutionPackage, reason: str
    ) -> dict[str, Any]:
        stop_deadline = self._clock_now() + timedelta(seconds=self.control_outage_seconds)
        try:
            self.adapter.cancel(
                package,
                timeout_seconds=max(0.0, (stop_deadline - self._clock_now()).total_seconds()),
            )
        except Exception:
            # The adapter persists its cancellation fence and unknown observation
            # before reporting a callback failure; never skip the later reconcile.
            pass
        try:
            status = self.adapter.reconcile(
                package,
                timeout_seconds=max(0.0, (stop_deadline - self._clock_now()).total_seconds()),
            )
        except Exception:
            status = KubernetesExecutionStatus(package.operation_id, None, (), "unknown")
        proof = self.adapter.cancellation_proof(package) | {"reason": reason}
        try:
            self._report(package, status.observed_state, proof, stop_deadline)
        except (ConnectionError, ControlChannelError):
            pass
        return self._result(dispatch, package, "cancel_requested", proof)


def _utc_deadline(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


__all__ = [
    "KubernetesClaimIntentJournal",
    "KubernetesDispatchError",
    "KubernetesDispatchWorkerConfig",
    "KubernetesDispatchWorkerService",
]
