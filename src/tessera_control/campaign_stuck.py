"""Stuck-campaign detector (platform, read-mostly): findings, receipts and acknowledgement.

The campaign runner drives campaigns, but nothing watched for one that stopped making progress.
This module scans a project's campaigns for six conditions, records each as a finding with a
reason code and a suggested next action, and puts every state change on a receipt (an event whose
payload carries a digest of the evidence). It takes no action on a campaign: it never cancels,
resumes, resolves or retries anything. A person (or an agent holding the scope) acts through the
existing routes; acknowledging a finding only says it has been seen.

Reason codes and suggested actions:

* ``campaign_blocked``: the campaign is ``blocked`` (evidence names the stop reason).
* ``delegation_lapsed``: ``paused_delegation_lapsed``.
* ``attempt_outcome_unknown``: an attempt is ``unknown`` or a stage is ``blocked_unknown``.
* ``stale_execution_lease``: a live grant's latest lease expired more than the grace ago.
* ``runner_lease_stale``: the runner lease expired more than the grace ago while campaigns are
  active (one finding per affected campaign).
* ``no_progress``: an active campaign and all its stages and attempts unchanged for
  ``unchanged_seconds`` and not already explained by a code above.

Evidence is ids, statuses and ages in seconds only; no spec, input, output or log content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .store import Store, _parse_utc, _utc_string

CODES = {
    "campaign_blocked": "inspect_stop_reason_then_resume_or_cancel",
    "delegation_lapsed": "renew_delegation_by_reapproval",
    "attempt_outcome_unknown": "resolve_unknown_attempt",
    "stale_execution_lease": "reconcile_or_cancel_the_grant",
    "runner_lease_stale": "check_the_campaign_runner_service",
    "no_progress": "check_provider_capacity_or_cancel",
}
ACTIVE = ("submitted", "running", "stopping", "cancel_requested")
TERMINAL_CAMPAIGN = ("succeeded", "stopped", "failed", "cancelled")
LIVE_ATTEMPT = ("granted", "admitted", "running", "exited", "collected", "resolved", "measured")
STATUSES = ("open", "acknowledged", "resolved")
DEFAULT_UNCHANGED_SECONDS = 3600
DEFAULT_LEASE_GRACE_SECONDS = 300
UNCHANGED_RANGE = (60, 30 * 86400)
GRACE_RANGE = (0, 86400)
DETECTED = "campaign.stuck_detected"
ACKNOWLEDGED = "campaign.stuck_acknowledged"
RESOLVED = "campaign.stuck_resolved"
SETTINGS_SET = "campaign.stuck_settings_set"


def create_stuck_schema(connection: sqlite3.Connection) -> None:
    """Shared by the migration and fresh databases so both converge. Additive, idempotent."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS campaign_stuck_settings (
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        unchanged_seconds INTEGER NOT NULL CHECK(unchanged_seconds BETWEEN 60 AND 2592000),
        lease_grace_seconds INTEGER NOT NULL CHECK(lease_grace_seconds BETWEEN 0 AND 86400),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        updated_by_principal_id TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(org_id, project_id))""",
        """CREATE TABLE IF NOT EXISTS campaign_stuck_findings (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        stage_key TEXT,
        code TEXT NOT NULL CHECK(code IN ('campaign_blocked','delegation_lapsed',
            'attempt_outcome_unknown','stale_execution_lease','runner_lease_stale',
            'no_progress')),
        subject TEXT NOT NULL,
        suggested_action TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','acknowledged','resolved')),
        occurrences INTEGER NOT NULL CHECK(occurrences >= 1),
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, resolved_at TEXT,
        evidence_json TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
        acknowledged_by TEXT, acknowledged_at TEXT,
        note TEXT CHECK(note IS NULL OR length(note) <= 500))""",
        """CREATE INDEX IF NOT EXISTS campaign_stuck_findings_by_project
        ON campaign_stuck_findings(project_id, status, last_seen_at)""",
        """CREATE TRIGGER IF NOT EXISTS campaign_stuck_findings_no_delete
        BEFORE DELETE ON campaign_stuck_findings
        BEGIN SELECT RAISE(ABORT, 'stuck findings are permanent'); END""",
        """CREATE TRIGGER IF NOT EXISTS campaign_stuck_findings_identity
        BEFORE UPDATE ON campaign_stuck_findings WHEN
            NEW.id IS NOT OLD.id OR NEW.org_id IS NOT OLD.org_id
            OR NEW.project_id IS NOT OLD.project_id OR NEW.campaign_id IS NOT OLD.campaign_id
            OR NEW.code IS NOT OLD.code OR NEW.subject IS NOT OLD.subject
            OR NEW.first_seen_at IS NOT OLD.first_seen_at
        BEGIN SELECT RAISE(ABORT, 'a stuck finding keeps its identity'); END""",
    ):
        connection.execute(statement)


def finding_id(campaign_id: str, code: str, subject: str) -> str:
    raw = f"{campaign_id}|{code}|{subject}".encode()
    return "stuck_" + hashlib.sha256(raw).hexdigest()[:32]


def receipt_digest(fid: str, code: str, evidence: dict[str, Any], occurrence: int) -> str:
    body = {"finding_id": fid, "code": code, "evidence": evidence, "occurrence": occurrence}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def get_settings(connection: sqlite3.Connection, org_id: str, project_id: str) -> dict[str, int]:
    row = connection.execute(
        "SELECT unchanged_seconds, lease_grace_seconds FROM campaign_stuck_settings "
        "WHERE org_id=? AND project_id=?",
        (org_id, project_id),
    ).fetchone()
    if row is None:
        return {
            "unchanged_seconds": DEFAULT_UNCHANGED_SECONDS,
            "lease_grace_seconds": DEFAULT_LEASE_GRACE_SECONDS,
        }
    return {k: int(row[k]) for k in ("unchanged_seconds", "lease_grace_seconds")}


def _age(now: datetime, stamp: str | None) -> int:
    return 0 if not stamp else max(0, int((now - _parse_utc(stamp)).total_seconds()))


def _finding(campaign_id, code, subject, stage_key, evidence) -> dict[str, Any]:
    return {
        "id": finding_id(campaign_id, code, subject),
        "campaign_id": campaign_id,
        "stage_key": stage_key,
        "code": code,
        "subject": subject,
        "suggested_action": CODES[code],
        "evidence": evidence,
    }


def detect(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    settings: dict[str, int],
    now: datetime,
) -> list[dict[str, Any]]:
    """Every stuck condition now present in the project, as findings (nothing is written)."""
    found: list[dict[str, Any]] = []
    campaigns = connection.execute(
        "SELECT id, status, stop_reason, updated_at FROM campaigns WHERE org_id=? AND project_id=?",
        (org_id, project_id),
    ).fetchall()
    explained: set[str] = set()
    for c in campaigns:
        if c["status"] == "blocked":
            found.append(
                _finding(
                    c["id"],
                    "campaign_blocked",
                    "",
                    None,
                    {
                        "status": "blocked",
                        "stop_reason": (c["stop_reason"] or "")[:120] or None,
                        "age_seconds": _age(now, c["updated_at"]),
                    },
                )
            )
        elif c["status"] == "paused_delegation_lapsed":
            found.append(
                _finding(
                    c["id"],
                    "delegation_lapsed",
                    "",
                    None,
                    {
                        "status": c["status"],
                        "stop_reason": (c["stop_reason"] or "")[:120] or None,
                        "age_seconds": _age(now, c["updated_at"]),
                    },
                )
            )
    open_ids = [c["id"] for c in campaigns if c["status"] not in TERMINAL_CAMPAIGN]
    # Unknown attempt outcomes (and stages blocked on one).
    for cid in open_ids:
        unknown_stages: set[str] = set()
        for a in connection.execute(
            "SELECT id, stage_key, attempt, updated_at FROM campaign_stage_attempts "
            "WHERE campaign_id=? AND status='unknown'",
            (cid,),
        ):
            unknown_stages.add(a["stage_key"])
            explained.add(cid)
            found.append(
                _finding(
                    cid,
                    "attempt_outcome_unknown",
                    a["id"],
                    a["stage_key"],
                    {
                        "attempt_id": a["id"],
                        "attempt": a["attempt"],
                        "attempt_status": "unknown",
                        "age_seconds": _age(now, a["updated_at"]),
                    },
                )
            )
        for s in connection.execute(
            "SELECT stage_key, updated_at FROM campaign_stages "
            "WHERE campaign_id=? AND status='blocked_unknown'",
            (cid,),
        ):
            if s["stage_key"] in unknown_stages:
                continue
            explained.add(cid)
            found.append(
                _finding(
                    cid,
                    "attempt_outcome_unknown",
                    s["stage_key"],
                    s["stage_key"],
                    {
                        "stage_status": "blocked_unknown",
                        "age_seconds": _age(now, s["updated_at"]),
                    },
                )
            )
    grace = settings["lease_grace_seconds"]
    # A live grant whose newest lease expired well past its time.
    for cid in open_ids:
        rows = connection.execute(
            f"""SELECT a.id AS attempt_id, a.stage_key, g.id AS grant_id, g.status AS grant_status,
            l.id AS lease_id, l.expires_at FROM campaign_stage_attempts a
            JOIN execution_grants g ON g.id = a.grant_id
            JOIN execution_leases l ON l.id = (SELECT id FROM execution_leases
                WHERE grant_id=g.id ORDER BY issued_at DESC, id DESC LIMIT 1)
            WHERE a.campaign_id=? AND g.status IN ('active','cancel_requested')
            AND a.status IN ({",".join("?" * len(LIVE_ATTEMPT))})""",
            (cid, *LIVE_ATTEMPT),
        ).fetchall()
        for r in rows:
            over = _age(now, r["expires_at"])
            if over > grace and _parse_utc(r["expires_at"]) < now:
                explained.add(cid)
                found.append(
                    _finding(
                        cid,
                        "stale_execution_lease",
                        r["lease_id"],
                        r["stage_key"],
                        {
                            "lease_id": r["lease_id"],
                            "grant_id": r["grant_id"],
                            "grant_status": r["grant_status"],
                            "attempt_id": r["attempt_id"],
                            "expired_seconds_ago": over,
                        },
                    )
                )
    # The runner service itself has stopped renewing its lease.
    runner = connection.execute(
        "SELECT expires_at FROM campaign_runner_lease WHERE singleton=1"
    ).fetchone()
    if runner is not None and _parse_utc(runner["expires_at"]) < now:
        over = _age(now, runner["expires_at"])
        if over > grace:
            for c in campaigns:
                if c["status"] in ACTIVE:
                    explained.add(c["id"])
                    found.append(
                        _finding(
                            c["id"],
                            "runner_lease_stale",
                            "",
                            None,
                            {"status": c["status"], "runner_lease_expired_seconds_ago": over},
                        )
                    )
    # No change at all, on a campaign nothing above explains.
    limit = settings["unchanged_seconds"]
    for c in campaigns:
        if c["status"] not in ACTIVE or c["id"] in explained:
            continue
        stamps = [c["updated_at"]]
        stamps += [
            r[0]
            for r in connection.execute(
                "SELECT updated_at FROM campaign_stages WHERE campaign_id=?", (c["id"],)
            )
        ]
        stamps += [
            r[0]
            for r in connection.execute(
                "SELECT updated_at FROM campaign_stage_attempts WHERE campaign_id=?", (c["id"],)
            )
        ]
        newest = max(_parse_utc(s) for s in stamps if s)
        idle = max(0, int((now - newest).total_seconds()))
        if idle > limit:
            found.append(
                _finding(
                    c["id"],
                    "no_progress",
                    "",
                    None,
                    {
                        "status": c["status"],
                        "unchanged_seconds": idle,
                        "threshold_seconds": limit,
                    },
                )
            )
    return sorted(found, key=lambda f: f["id"])


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "stage_key": row["stage_key"],
        "code": row["code"],
        "suggested_action": row["suggested_action"],
        "status": row["status"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "resolved_at": row["resolved_at"],
        "occurrences": row["occurrences"],
        "evidence": json.loads(row["evidence_json"]),
        "receipt_sha256": row["receipt_sha256"],
        "acknowledged_by": row["acknowledged_by"],
        "acknowledged_at": row["acknowledged_at"],
        "note": row["note"],
    }


def _event_payload(f: dict[str, Any], receipt: str) -> dict[str, Any]:
    return {
        "finding_id": f["id"],
        "campaign_id": f["campaign_id"],
        "stage_key": f["stage_key"],
        "code": f["code"],
        "suggested_action": f["suggested_action"],
        "evidence": f["evidence"],
        "receipt_sha256": receipt,
    }


def scan_project(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    now: datetime,
    actor_for: Callable[[str], str],
) -> dict[str, Any]:
    """Detect, then record: new findings open, cleared ones resolve, every change a receipt.

    Runs inside the caller's transaction. Idempotent for the same state and clock. The evidence of
    an occurrence is frozen at detection (its receipt digest covers exactly it), so a finding that
    is still detected only has its last-seen time updated, with no new event; a reappearance after
    resolution is a new occurrence with new evidence and a new digest."""
    stamp = _utc_string(now)
    found = {
        f["id"]: f
        for f in detect(
            connection, org_id, project_id, get_settings(connection, org_id, project_id), now
        )
    }
    existing = {
        r["id"]: r
        for r in connection.execute(
            "SELECT * FROM campaign_stuck_findings WHERE org_id=? AND project_id=?",
            (org_id, project_id),
        )
    }
    opened: list[dict[str, Any]] = []
    resolved: list[str] = []

    def emit(event: str, campaign_id: str, payload: dict[str, Any]) -> None:
        Store._event(
            connection,
            {"id": actor_for(campaign_id), "org_id": org_id},
            project_id,
            None,
            event,
            1,
            payload,
        )

    for fid, f in found.items():
        evidence_json = json.dumps(f["evidence"], sort_keys=True, separators=(",", ":"))
        old = existing.get(fid)
        if old is None or old["status"] == "resolved":
            occurrence = 1 if old is None else old["occurrences"] + 1
            receipt = receipt_digest(fid, f["code"], f["evidence"], occurrence)
            if old is None:
                connection.execute(
                    """INSERT INTO campaign_stuck_findings (id, org_id, project_id, campaign_id,
                    stage_key, code, subject, suggested_action, status, occurrences, first_seen_at,
                    last_seen_at, evidence_json, receipt_sha256)
                    VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?, ?, ?, ?)""",
                    (fid, org_id, project_id, f["campaign_id"], f["stage_key"], f["code"],
                     f["subject"], f["suggested_action"], occurrence, stamp, stamp, evidence_json,
                     receipt),
                )  # fmt: skip
            else:
                connection.execute(
                    """UPDATE campaign_stuck_findings SET status='open', occurrences=?,
                    last_seen_at=?, resolved_at=NULL, evidence_json=?, receipt_sha256=?,
                    acknowledged_by=NULL, acknowledged_at=NULL, note=NULL WHERE id=?""",
                    (occurrence, stamp, evidence_json, receipt, fid),
                )
            emit(DETECTED, f["campaign_id"], _event_payload(f, receipt))
            fresh = connection.execute(
                "SELECT * FROM campaign_stuck_findings WHERE id=?", (fid,)
            ).fetchone()
            opened.append(_row(fresh))
        else:
            connection.execute(
                "UPDATE campaign_stuck_findings SET last_seen_at=? WHERE id=?", (stamp, fid)
            )
    for fid, old in existing.items():
        if fid in found or old["status"] == "resolved":
            continue
        connection.execute(
            "UPDATE campaign_stuck_findings SET status='resolved', resolved_at=? WHERE id=?",
            (stamp, fid),
        )
        emit(
            RESOLVED,
            old["campaign_id"],
            {
                "finding_id": fid,
                "campaign_id": old["campaign_id"],
                "stage_key": old["stage_key"],
                "code": old["code"],
                "suggested_action": old["suggested_action"],
                "evidence": json.loads(old["evidence_json"]),
                "receipt_sha256": old["receipt_sha256"],
            },
        )
        resolved.append(fid)
    open_count = connection.execute(
        "SELECT COUNT(*) FROM campaign_stuck_findings WHERE org_id=? AND project_id=? "
        "AND status IN ('open','acknowledged')",
        (org_id, project_id),
    ).fetchone()[0]
    return {"scanned_at": stamp, "opened": opened, "resolved": resolved, "open_count": open_count}


def list_findings(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    status: str = "open",
    limit: int = 50,
) -> list[dict[str, Any]]:
    clause, args = "", [org_id, project_id]
    if status != "all":
        clause = " AND status=?"
        args.append(status)
    rows = connection.execute(
        f"SELECT * FROM campaign_stuck_findings WHERE org_id=? AND project_id=?{clause} "
        "ORDER BY last_seen_at DESC, id LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [_row(r) for r in rows]


def acknowledge(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    fid: str,
    actor_id: str,
    note: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Mark an open finding seen. ``None`` when the id is unknown here; a resolved finding is a
    conflict the caller reports. Acknowledging an acknowledged finding replays it unchanged."""
    row = connection.execute(
        "SELECT * FROM campaign_stuck_findings WHERE id=? AND org_id=? AND project_id=?",
        (fid, org_id, project_id),
    ).fetchone()
    if row is None:
        return None
    if row["status"] != "open":
        return _row(row)
    stamp = _utc_string(now)
    connection.execute(
        "UPDATE campaign_stuck_findings SET status='acknowledged', acknowledged_by=?, "
        "acknowledged_at=?, note=? WHERE id=?",
        (actor_id, stamp, note, fid),
    )
    row = connection.execute("SELECT * FROM campaign_stuck_findings WHERE id=?", (fid,)).fetchone()
    Store._event(
        connection,
        {"id": actor_id, "org_id": org_id},
        project_id,
        None,
        ACKNOWLEDGED,
        1,
        {
            "finding_id": fid,
            "campaign_id": row["campaign_id"],
            "stage_key": row["stage_key"],
            "code": row["code"],
            "suggested_action": row["suggested_action"],
            "evidence": json.loads(row["evidence_json"]),
            "receipt_sha256": row["receipt_sha256"],
            "note_present": note is not None,
        },
    )
    return _row(row)


STUCK_SCAN_FAILED = "campaign.stuck_scan_failed"
FAILURE_EVENT_EVERY = 10


def _record_scan_failure(connection, project, error: Exception, count: int) -> None:
    """A sanitized receipt of a failing scan: the project, the exception CLASS and the count.

    Written in its own transaction, at the first failure and every 10th consecutive one, so a
    persistently failing project is visible without a row per pass."""
    from .grant_release import campaign_actor

    try:
        campaign = connection.execute(
            "SELECT id FROM campaigns WHERE org_id=? AND project_id=? ORDER BY id LIMIT 1",
            (project["org_id"], project["project_id"]),
        ).fetchone()
        connection.execute("BEGIN IMMEDIATE")
        Store._event(
            connection,
            {
                "id": (campaign_actor(connection, campaign["id"]) if campaign else "") or "",
                "org_id": project["org_id"],
            },
            project["project_id"],
            None,
            STUCK_SCAN_FAILED,
            1,
            {
                "project_id": project["project_id"],
                "error_class": type(error).__name__,
                "consecutive_failures": count,
            },
        )
        connection.execute("COMMIT")
    except Exception:  # noqa: BLE001 - reporting a failure never raises
        if connection.in_transaction:
            connection.execute("ROLLBACK")


def scan_all(
    database_path,
    clock: Callable[[], datetime] | None = None,
    failures: dict[tuple[str, str], int] | None = None,
) -> int:
    """One pass over every project that has campaigns; the periodic scanner's unit of work.

    Each project is scanned in its own transaction, attributed to the campaign's approver (the
    principal the runner acts as). Returns the number of projects scanned. Raises nothing to the
    caller for a single project's failure; ``failures`` (kept by the caller across passes) counts
    consecutive failures per project and a failing project is recorded as a
    ``campaign.stuck_scan_failed`` event at the first failure and every 10th after."""
    from .grant_release import campaign_actor
    from .store import _connect

    now = (clock or (lambda: datetime.now(UTC)))()
    connection = _connect(database_path)
    scanned = 0
    failures = {} if failures is None else failures
    try:
        projects = connection.execute(
            "SELECT DISTINCT org_id, project_id FROM campaigns ORDER BY org_id, project_id"
        ).fetchall()
        for project in projects:
            try:
                connection.execute("BEGIN IMMEDIATE")
                scan_project(
                    connection,
                    project["org_id"],
                    project["project_id"],
                    now,
                    lambda campaign_id: campaign_actor(connection, campaign_id) or "",
                )
                connection.execute("COMMIT")
                scanned += 1
                failures.pop((project["org_id"], project["project_id"]), None)
            except Exception as error:  # noqa: BLE001 - one project's failure never stops the pass
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                key = (project["org_id"], project["project_id"])
                failures[key] = failures.get(key, 0) + 1
                if failures[key] == 1 or failures[key] % FAILURE_EVENT_EVERY == 0:
                    _record_scan_failure(connection, project, error, failures[key])
    finally:
        connection.close()
    return scanned


class ScannerHandle:
    def __init__(self, thread, stop) -> None:
        self._thread, self._stop = thread, stop

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def start_background(database_path, interval_seconds: float) -> ScannerHandle:
    """The periodic scanner: one ``scan_all`` pass every ``interval_seconds`` (0 disables)."""
    import threading

    stop = threading.Event()
    if interval_seconds <= 0:
        return ScannerHandle(None, stop)

    failures: dict[tuple[str, str], int] = {}

    def loop() -> None:
        while not stop.wait(interval_seconds):
            try:
                scan_all(database_path, failures=failures)
            except Exception:  # noqa: BLE001 - never crash the service; log no payloads
                pass

    thread = threading.Thread(target=loop, name="campaign-stuck-scanner", daemon=True)
    thread.start()
    return ScannerHandle(thread, stop)
