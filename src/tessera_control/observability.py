"""Read-only, secret-free operator projections for local control state."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .store import Forbidden, Store, _connect, _parse_utc, utc_now
from .workers import HEARTBEAT_MAX_AGE

_LOG = logging.getLogger(__name__)
BACKUP_STALE_AFTER = timedelta(hours=24)


def _held_source_is_safe(state_dir: Path) -> bool:
    """Require an actual state directory and database before using its sidecar.

    The sidecar is operational metadata, never a reason to follow an unsafe
    supplied recovery source after a rejected backup or mutation.
    """
    try:
        directory = state_dir.lstat()
        database = (state_dir / "tessera.sqlite3").lstat()
        parent = state_dir.parent.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(directory.st_mode)
        and not stat.S_ISLNK(directory.st_mode)
        and stat.S_ISREG(database.st_mode)
        and not stat.S_ISLNK(database.st_mode)
        and stat.S_ISDIR(parent.st_mode)
        and not stat.S_ISLNK(parent.st_mode)
    )


def _held_sink_path(state_dir: Path) -> Path:
    """Return a sibling operational sink, deliberately outside held history."""
    identity = hashlib.sha256(os.fsencode(str(state_dir.absolute()))).hexdigest()
    return state_dir.parent / f".tessera-held-operations-{identity}.sqlite3"


def _open_held_sink(state_dir: Path, *, create: bool) -> sqlite3.Connection | None:
    """Open an external held-state sink without ever writing the held directory."""
    if not _held_source_is_safe(state_dir):
        return None
    path = _held_sink_path(state_dir)
    try:
        if path.exists():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
        elif not create:
            return None
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        connection = _connect(path)
        connection.executescript(
            """CREATE TABLE IF NOT EXISTS held_operator_audit_events (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id TEXT NOT NULL,
            project_id TEXT,
            event_type TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('allowed','denied')),
            actor_principal_id TEXT,
            correlation_id TEXT,
            causation_id TEXT,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS held_operator_audit_events_by_org_cursor
            ON held_operator_audit_events(org_id, cursor);
            CREATE TABLE IF NOT EXISTS held_operator_backup_health (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            last_success_at TEXT,
            last_backup_id TEXT,
            last_failure_at TEXT,
            last_failure_code TEXT
            );
            INSERT OR IGNORE INTO held_operator_backup_health
            (singleton,last_success_at,last_backup_id,last_failure_at,last_failure_code)
            VALUES (1,NULL,NULL,NULL,NULL);"""
        )
        return connection
    except (OSError, sqlite3.Error):
        _LOG.warning("Held operational audit/health sink is unavailable.", exc_info=True)
        return None


def record_held_denial(state_dir: Path, principal: dict[str, Any], method: str, path: str) -> bool:
    """Durably retain a held-state denial outside its immutable history database."""
    connection = _open_held_sink(state_dir, create=True)
    if connection is None:
        _LOG.warning("Held command denial could not be durably recorded outside held history.")
        return False
    try:
        command_id = "held-command-" + hashlib.sha256(os.urandom(32)).hexdigest()
        connection.execute(
            """INSERT INTO held_operator_audit_events
            (org_id,project_id,event_type,decision,actor_principal_id,correlation_id,
             causation_id,created_at,payload_json)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                principal["org_id"],
                principal.get("project_id"),
                "privileged.command_denied",
                "denied",
                principal["id"],
                command_id,
                command_id,
                utc_now(),
                json.dumps({"method": method, "path": path}, separators=(",", ":"), sort_keys=True),
            ),
        )
        return True
    except sqlite3.Error:
        _LOG.warning(
            "Held command denial could not be durably recorded outside held history.", exc_info=True
        )
        return False
    finally:
        connection.close()


def record_held_backup_health(state_dir: Path, *, backup_id: str | None, failure: bool) -> bool:
    """Record held-copy backup health in the external operational sink."""
    connection = _open_held_sink(state_dir, create=True)
    if connection is None:
        _LOG.warning("Held backup health could not be durably recorded outside held history.")
        return False
    try:
        if failure:
            connection.execute(
                """UPDATE held_operator_backup_health SET last_failure_at=?,
                last_failure_code='backup_failed' WHERE singleton=1""",
                (utc_now(),),
            )
        else:
            assert backup_id is not None
            connection.execute(
                """UPDATE held_operator_backup_health SET last_success_at=?,last_backup_id=?,
                last_failure_at=NULL,last_failure_code=NULL WHERE singleton=1""",
                (utc_now(), backup_id),
            )
        return True
    except sqlite3.Error:
        _LOG.warning(
            "Held backup health could not be durably recorded outside held history.", exc_info=True
        )
        return False
    finally:
        connection.close()


def _held_audit_events(
    state_dir: Path, org_id: str, after: int, limit: int
) -> list[dict[str, Any]]:
    connection = _open_held_sink(state_dir, create=False)
    if connection is None:
        return []
    try:
        return [
            {
                **{key: row[key] for key in row.keys() if key != "payload_json"},
                "payload": json.loads(row["payload_json"]),
                "storage": "external_held_operational_sink",
            }
            for row in connection.execute(
                """SELECT cursor,project_id,event_type,decision,actor_principal_id,
                correlation_id,causation_id,created_at,payload_json
                FROM held_operator_audit_events WHERE org_id=? AND cursor>?
                ORDER BY cursor LIMIT ?""",
                (org_id, after, limit),
            ).fetchall()
        ]
    finally:
        connection.close()


def _held_backup_health(state_dir: Path) -> sqlite3.Row | None:
    connection = _open_held_sink(state_dir, create=False)
    if connection is None:
        return None
    try:
        return connection.execute(
            "SELECT * FROM held_operator_backup_health WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()


def _backup_freshness(backup: sqlite3.Row, now: datetime) -> tuple[str, int | None]:
    """Classify only a recorded backup timestamp; this is not an RPO claim."""
    recorded_at = backup["last_success_at"]
    if recorded_at is None:
        return "unknown", None
    try:
        age_seconds = int((now - _parse_utc(recorded_at)).total_seconds())
    except (TypeError, ValueError):
        return "unknown", None
    # A clock-skewed future timestamp cannot demonstrate backup freshness.
    if age_seconds < 0:
        return "unknown", None
    return ("stale" if age_seconds > BACKUP_STALE_AFTER.total_seconds() else "fresh"), age_seconds


def create_observability_schema(connection: sqlite3.Connection) -> None:
    """Install operator-only audit and backup-health metadata.

    This is deliberately separate from project labbook events: credential issuance
    can be organization-wide, and neither audit stream may contain bearer values,
    token hashes, secret references, or request bodies.
    """

    connection.execute(
        """CREATE TABLE IF NOT EXISTS operator_audit_events (
        cursor INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT REFERENCES projects(id),
        event_type TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('allowed','denied')),
        actor_principal_id TEXT REFERENCES principals(id),
        correlation_id TEXT,
        causation_id TEXT,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS operator_audit_events_by_org_cursor
        ON operator_audit_events(org_id, cursor)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS operator_backup_health (
        singleton INTEGER PRIMARY KEY CHECK (singleton=1),
        last_success_at TEXT,
        last_backup_id TEXT,
        last_failure_at TEXT,
        last_failure_code TEXT
        )"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO operator_backup_health
        (singleton,last_success_at,last_backup_id,last_failure_at,last_failure_code)
        VALUES (1,NULL,NULL,NULL,NULL)"""
    )


class OperatorObservabilityStore(Store):
    """Owner-only operational status; this view grants no recovery authority."""

    @staticmethod
    def _require_owner(actor: dict[str, Any]) -> None:
        if not Store._is_root_owner(actor):
            raise Forbidden("Only a local owner can read operator observability.")

    def audit_events(
        self, principal: dict[str, Any], after: int, limit: int
    ) -> list[dict[str, Any]]:
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            self._require_owner(actor)
            rows = connection.execute(
                """SELECT cursor,project_id,event_type,decision,actor_principal_id,
                correlation_id,causation_id,created_at,payload_json
                FROM operator_audit_events WHERE org_id=? AND cursor>? ORDER BY cursor LIMIT ?""",
                (actor["org_id"], after, limit),
            ).fetchall()
            events = [
                {
                    **{key: row[key] for key in row.keys() if key != "payload_json"},
                    "payload": json.loads(row["payload_json"]),
                    "storage": "control_history",
                }
                for row in rows
            ]
            recovery = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            if recovery is None or recovery["state"] != "held_restore" or len(events) >= limit:
                return events
            watermark = connection.execute(
                "SELECT COALESCE(MAX(cursor),0) FROM operator_audit_events"
            ).fetchone()[0]
            held_after = max(0, after - watermark)
            for event in _held_audit_events(
                self.database_path.parent, actor["org_id"], held_after, limit - len(events)
            ):
                # The immutable source cursor is the stable prefix; external events
                # continue after it without claiming they were historical records.
                event["cursor"] = watermark + event["cursor"]
                events.append(event)
            return events
        finally:
            connection.close()

    def health(self, principal: dict[str, Any]) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            self._require_owner(actor)
            now = datetime.now(UTC)
            stale = 0
            unknown_worker_freshness = 0
            for worker in connection.execute(
                """SELECT last_seen_at FROM worker_registry
                WHERE org_id=? AND lifecycle_state != 'retired'""",
                (actor["org_id"],),
            ):
                try:
                    is_stale = _parse_utc(worker["last_seen_at"]) < now - HEARTBEAT_MAX_AGE
                except ValueError:
                    unknown_worker_freshness += 1
                else:
                    stale += int(is_stale)
            reconciliation_rows = connection.execute(
                """SELECT state,COUNT(*) AS count,MIN(created_at) AS oldest_opened_at
                FROM execution_recovery_reconciliations
                WHERE org_id=? AND closed_at IS NULL GROUP BY state ORDER BY state""",
                (actor["org_id"],),
            ).fetchall()
            reconciliation_backlog = sum(row["count"] for row in reconciliation_rows)
            oldest_opened_at = min(
                (row["oldest_opened_at"] for row in reconciliation_rows if row["oldest_opened_at"]),
                default=None,
            )
            backup = connection.execute(
                "SELECT * FROM operator_backup_health WHERE singleton=1"
            ).fetchone()
            recovery = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            held_backup = (
                _held_backup_health(self.database_path.parent)
                if recovery is not None and recovery["state"] == "held_restore"
                else None
            )
            backup_storage = "control_history"
            if held_backup is not None:
                backup = held_backup
                backup_storage = "external_held_operational_sink"
            backup_freshness, backup_age_seconds = _backup_freshness(backup, now)
            budgets = [
                self._budget_projection(row)
                for row in connection.execute(
                    """SELECT limits.*,COUNT(grant.id) AS active_grants,
                    COALESCE(SUM(CASE WHEN grant.id IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM execution_leases lease
                    WHERE lease.grant_id=grant.id)
                    AND NOT EXISTS (SELECT 1 FROM external_operations operation
                    WHERE operation.grant_id=grant.id)
                    THEN grant.max_exposure_cents ELSE 0 END),0) AS unattempted_reserved_cents,
                    COALESCE(SUM(CASE WHEN grant.id IS NOT NULL
                    AND (EXISTS (SELECT 1 FROM execution_leases lease
                    WHERE lease.grant_id=grant.id)
                    OR EXISTS (SELECT 1 FROM external_operations operation
                    WHERE operation.grant_id=grant.id))
                    THEN grant.max_exposure_cents ELSE 0 END),0) AS attempted_reserved_cents
                    FROM execution_project_limits limits
                    JOIN projects project ON project.id=limits.project_id
                    LEFT JOIN execution_grants grant ON grant.project_id=limits.project_id
                    AND grant.status IN ('active','cancel_requested')
                    WHERE project.org_id=? GROUP BY limits.project_id ORDER BY limits.project_id""",
                    (actor["org_id"],),
                )
            ]
            failed_backup = backup["last_failure_at"] is not None and (
                backup["last_success_at"] is None
                or backup["last_failure_at"] > backup["last_success_at"]
            )
            alerts: list[dict[str, str]] = []
            if stale:
                alerts.append({"code": "worker_heartbeat_stale", "severity": "warning"})
            if unknown_worker_freshness:
                alerts.append({"code": "worker_heartbeat_unknown", "severity": "warning"})
            if reconciliation_backlog:
                alerts.append({"code": "reconciliation_backlog", "severity": "warning"})
            if failed_backup:
                alerts.append({"code": "backup_failed", "severity": "critical"})
            elif backup_freshness == "stale":
                alerts.append({"code": "backup_stale", "severity": "warning"})
            elif backup_freshness == "unknown":
                alerts.append({"code": "backup_unverified", "severity": "warning"})
            if any(budget["status"] == "exhausted" for budget in budgets):
                alerts.append({"code": "resource_budget_exhausted", "severity": "warning"})
            if any(budget["status"] == "reserved_unattempted_authority" for budget in budgets):
                alerts.append(
                    {
                        "code": "resource_budget_unattempted_authority_reserved",
                        "severity": "warning",
                    }
                )
            return {
                "projection_at": utc_now(),
                "alerts": alerts,
                "workers": {
                    "stale_count": stale,
                    "unknown_freshness_count": unknown_worker_freshness,
                    "heartbeat_max_age_seconds": int(HEARTBEAT_MAX_AGE.total_seconds()),
                },
                "reconciliation": {
                    "open_count": reconciliation_backlog,
                    "open_by_state": {row["state"]: row["count"] for row in reconciliation_rows},
                    "oldest_opened_at": oldest_opened_at,
                },
                "backups": {
                    "status": "failed"
                    if failed_backup
                    else "stale"
                    if backup_freshness == "stale"
                    else "healthy"
                    if backup_freshness == "fresh"
                    else "unknown",
                    "last_success_at": backup["last_success_at"],
                    "last_backup_id": backup["last_backup_id"],
                    "last_failure_at": backup["last_failure_at"],
                    "last_failure_code": backup["last_failure_code"],
                    "storage": backup_storage,
                    "freshness": backup_freshness,
                    "age_seconds": backup_age_seconds,
                    "stale_after_seconds": int(BACKUP_STALE_AFTER.total_seconds()),
                },
                "resource_budgets": budgets,
                "limitations": {
                    "provider_usage": "not_accounted_by_this_local projection",
                    "backup_failure_recording": (
                        "unavailable when the source database or external held operational sink "
                        "cannot be opened"
                    ),
                    "recovery_authority": (
                        "read-only projection; it cannot fence writers or promote/resume a "
                        "held restore"
                    ),
                },
            }
        finally:
            connection.close()

    @staticmethod
    def _budget_projection(row: sqlite3.Row) -> dict[str, Any]:
        """Describe reservation pressure without turning zero attempts into spend.

        A zero-attempt reservation still blocks new authority.  It is therefore
        neither reported as available capacity nor as provider usage/exhausted
        spend.  Any held amount without a matching unresolved grant is retained
        as unattributed exposure and continues to block admission.
        """
        unattempted = row["unattempted_reserved_cents"]
        attempted = row["attempted_reserved_cents"]
        recorded = unattempted + attempted
        unattributed = max(0, row["held_cents"] - recorded)
        remaining = max(0, row["monetary_limit_cents"] - row["held_cents"] - row["settled_cents"])
        unavailable = remaining == 0
        entirely_unattempted = (
            unavailable
            and row["settled_cents"] == 0
            and attempted == 0
            and unattributed == 0
            and unattempted > 0
        )
        return {
            "project_id": row["project_id"],
            "currency": row["currency"],
            "monetary_limit_cents": row["monetary_limit_cents"],
            "held_cents": row["held_cents"],
            "settled_cents": row["settled_cents"],
            "unattempted_reserved_cents": unattempted,
            "attempted_reserved_cents": attempted,
            "unattributed_held_cents": unattributed,
            "remaining_cents": remaining,
            "authority_admission": "blocked" if unavailable else "available",
            "max_concurrency": row["max_concurrency"],
            "active_or_cancelling_grants": row["active_grants"],
            "status": (
                "reserved_unattempted_authority"
                if entirely_unattempted
                else "exhausted"
                if unavailable
                else "within_limit"
            ),
        }
