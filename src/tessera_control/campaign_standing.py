"""Standing approvals: request models and the runner-side scheduler (ADR 0042).

A standing approval is a human decision about one spec digest with explicit caps. The
scheduler turns it into ordinary campaigns plus ordinary approval rows. Every decision is
derived from SQLite and the runner clock; each candidate is handled in one runner
transaction so state, events and counters commit together.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from . import program_budget
from .approval_trust import CLASS_STANDING_APPROVAL
from .campaign_gate_models import UnlockList
from .campaign_gates import evaluate_unlock, parse_unlock_json, record_evaluations
from .campaign_models import (
    TERMINAL_CAMPAIGN,
    AnyCampaignSpec,
    Cents,
    Identifier,
    Sha256Digest,
    _Strict,
    normalize,
    parse_spec_json,
    spec_digest,
    stage_input_class,
    stage_output_class,
    stage_provider,
    topological_order,
)
from .store import Conflict, Forbidden, NotFound, Store, _parse_utc, _utc_string

log = logging.getLogger("tessera.campaign_standing")

MAX_STARTS_PER_TICK = 4
MAX_WINDOW_DAYS = 30
TERMINAL = tuple(sorted(TERMINAL_CAMPAIGN))
_MARKS = ",".join("?" for _ in TERMINAL)


class CreateStandingApproval(_Strict):
    spec: AnyCampaignSpec
    expected_spec_digest: Sha256Digest
    per_start_budget_cap_cents: Cents
    max_starts: Annotated[int, Field(strict=True, ge=1, le=100)]
    max_total_budget_cents: Annotated[int, Field(strict=True, ge=0, le=500000)]
    min_interval_seconds: Annotated[int, Field(strict=True, ge=60, le=2592000)] | None = None
    max_concurrent: Annotated[int, Field(strict=True, ge=1, le=4)] = 1
    not_before: str | None = None
    not_after: str
    trigger: Literal["schedule", "follow_on"] = "schedule"
    follow_on_standing_approval_id: Identifier | None = None
    retry_max: Annotated[int, Field(strict=True, ge=0, le=5)] = 0
    retry_on: Literal["failed", "failed_or_stopped"] = "failed"
    # Conditional unlock (ADR 0045): every start, retry and follow-on re-checks it.
    unlock: UnlockList | None = None


class RevokeStandingApproval(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


def _iso(moment) -> str:
    return _utc_string(moment.replace(microsecond=0))


def _principal(row: sqlite3.Row | dict) -> dict[str, str]:
    return {"id": row["created_by_principal_id"], "org_id": row["org_id"]}


def _event(connection, row, event_type: str, payload: dict[str, Any], revision=None) -> None:
    Store._event(
        connection,
        _principal(row),
        row["project_id"],
        None,
        event_type,
        revision if revision is not None else row["revision"],
        {"standing_approval_id": row["id"], **payload},
    )


def abandon_triggers(connection, row, reason: str, now: str) -> list[str]:
    """Abandon every pending trigger aimed at ``row`` (revoke, expiry, exhaustion)."""
    abandoned = []
    pending = connection.execute(
        """SELECT trigger_campaign_id, kind FROM campaign_standing_triggers
        WHERE target_standing_approval_id=? AND status='pending'
        ORDER BY trigger_campaign_id, kind""",
        (row["id"],),
    ).fetchall()
    for item in pending:
        connection.execute(
            """UPDATE campaign_standing_triggers SET status='abandoned', reason=?, resolved_at=?
            WHERE trigger_campaign_id=? AND kind=?""",
            (reason, now, item["trigger_campaign_id"], item["kind"]),
        )
        _event(
            connection,
            row,
            "campaign.standing_trigger_abandoned",
            {
                "trigger_campaign_id": item["trigger_campaign_id"],
                "kind": item["kind"],
                "reason": reason,
            },
        )
        abandoned.append(item["trigger_campaign_id"])
    return abandoned


def running_campaigns(connection, standing_id: str) -> list[str]:
    return [
        r["campaign_id"]
        for r in connection.execute(
            f"""SELECT s.campaign_id FROM campaign_standing_starts s
            JOIN campaigns c ON c.id=s.campaign_id
            WHERE s.standing_approval_id=? AND c.status NOT IN ({_MARKS})
            ORDER BY s.start_index""",
            (standing_id, *TERMINAL),
        )
    ]


def _lifecycle(connection, row, now) -> str | None:
    """Move an active standing approval to expired/exhausted; returns the new status."""
    stamp = _iso(now)
    if now >= _parse_utc(row["not_after"]):
        status = "expired"
    elif (
        row["starts_used"] >= row["max_starts"]
        or row["committed_budget_cents"] + row["per_start_budget_cap_cents"]
        > row["max_total_budget_cents"]
    ):
        status = "exhausted"
    else:
        return None
    revision = row["revision"] + 1
    connection.execute(
        "UPDATE campaign_standing_approvals SET status=?, revision=?, updated_at=? WHERE id=?",
        (status, revision, stamp, row["id"]),
    )
    _event(
        connection,
        row,
        f"campaign.standing_approval_{status}",
        {
            "starts_used": row["starts_used"],
            "committed_budget_cents": row["committed_budget_cents"],
        },
        revision,
    )
    abandon_triggers(connection, row, f"target_{status}", stamp)
    return status


def _refuse(connection, row, reason: str) -> None:
    reason = reason[:300]
    if row["last_refusal_reason"] == reason:
        return
    connection.execute(
        "UPDATE campaign_standing_approvals SET last_refusal_reason=? WHERE id=?",
        (reason, row["id"]),
    )
    _event(connection, row, "campaign.standing_start_refused", {"reason": reason})


def _record_triggers(connection, now) -> None:
    """Write at most one trigger row per finished campaign and kind (section 4)."""
    stamp = _iso(now)
    rows = connection.execute(
        """SELECT c.*, st.standing_approval_id AS sid, st.retry_number AS start_retry
        FROM campaign_standing_starts st JOIN campaigns c ON c.id=st.campaign_id
        WHERE c.status IN ('succeeded','stopped','failed') AND NOT EXISTS (
            SELECT 1 FROM campaign_standing_triggers t WHERE t.trigger_campaign_id=c.id)
        ORDER BY c.created_at, c.id"""
    ).fetchall()
    for campaign in rows:
        source = connection.execute(
            "SELECT * FROM campaign_standing_approvals WHERE id=?", (campaign["sid"],)
        ).fetchone()
        if campaign["status"] == "succeeded":
            target_id = source["follow_on_standing_approval_id"]
            receipt = connection.execute(
                "SELECT 1 FROM campaign_receipts WHERE campaign_id=?", (campaign["id"],)
            ).fetchone()
            if target_id is None or receipt is None or source["status"] == "revoked":
                continue
            target = connection.execute(
                "SELECT * FROM campaign_standing_approvals WHERE id=?", (target_id,)
            ).fetchone()
            status = "pending" if target["status"] == "active" else "abandoned"
            reason = None if status == "pending" else f"target_{target['status']}"
            connection.execute(
                """INSERT INTO campaign_standing_triggers (trigger_campaign_id, kind,
                target_standing_approval_id, retry_number, status, reason, created_at,
                resolved_at) VALUES (?, 'follow_on', ?, 0, ?, ?, ?, ?)""",
                (
                    campaign["id"],
                    target_id,
                    status,
                    reason,
                    stamp,
                    stamp if status == "abandoned" else None,
                ),
            )
            if status == "abandoned":
                _event(
                    connection,
                    target,
                    "campaign.standing_trigger_abandoned",
                    {"trigger_campaign_id": campaign["id"], "kind": "follow_on", "reason": reason},
                )
            continue
        wanted = {"failed"} if source["retry_on"] == "failed" else {"failed", "stopped"}
        reason = campaign["stop_reason"] or ""
        if (
            source["status"] != "active"
            or campaign["status"] not in wanted
            or campaign["start_retry"] >= source["retry_max"]
            or reason.startswith(("budget_refused:", "cancel:"))
            or campaign["held_cents"] > 0
            or connection.execute(
                "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND status='unknown'",
                (campaign["id"],),
            ).fetchone()
        ):
            continue
        connection.execute(
            """INSERT INTO campaign_standing_triggers (trigger_campaign_id, kind,
            target_standing_approval_id, retry_number, status, created_at)
            VALUES (?, 'retry', ?, ?, 'pending', ?)""",
            (campaign["id"], source["id"], campaign["start_retry"] + 1, stamp),
        )


def _headroom(connection, project_id: str) -> int | None:
    limits = connection.execute(
        "SELECT * FROM execution_project_limits WHERE project_id=?", (project_id,)
    ).fetchone()
    if limits is None:
        return None
    unspent = connection.execute(
        f"""SELECT COALESCE(SUM(MAX(budget_cap_cents - held_cents - settled_cents, 0)), 0)
        FROM campaigns WHERE project_id=? AND status NOT IN ({_MARKS})""",
        (project_id, *TERMINAL),
    ).fetchone()[0]
    return limits["monetary_limit_cents"] - limits["held_cents"] - limits["settled_cents"] - unspent


class _Permanent(Exception):
    """The stored spec no longer validates; a pending trigger is abandoned."""


def _check(runner, connection, row, now) -> tuple[str | None, Any]:
    """Checks 2 and 4 to 7 of section 3; returns (refusal reason, parsed spec)."""
    try:
        live = Store._live_principal(
            connection, {"id": row["created_by_principal_id"], "org_id": row["org_id"]}
        )
    except Forbidden:
        return "creator_not_live", None
    if live["role"] != "human":
        return "creator_not_human", None
    # The authority is the credential that created the standing approval: a credential that
    # replaced it (or the creator's other sessions) does not carry the decision forward.
    if (
        row["created_credential_id"] is not None
        and live["credential_id"] != row["created_credential_id"]
    ):
        return "creator_credential_changed", None
    try:
        Store._project(connection, live, row["project_id"])
    except NotFound:
        return "creator_not_project_member", None
    if row["min_interval_seconds"] and row["last_start_at"]:
        due = _parse_utc(row["last_start_at"]) + timedelta(seconds=row["min_interval_seconds"])
        if now < due:
            return "min_interval_not_elapsed", None
    active = connection.execute(
        f"""SELECT count(*) FROM campaign_standing_starts s JOIN campaigns c ON c.id=s.campaign_id
        WHERE s.standing_approval_id=? AND c.status NOT IN ({_MARKS})""",
        (row["id"], *TERMINAL),
    ).fetchone()[0]
    if active >= row["max_concurrent"]:
        return "max_concurrent_reached", None
    if connection.execute(
        """SELECT 1 FROM campaign_standing_starts s JOIN campaigns c ON c.id=s.campaign_id
        WHERE s.standing_approval_id=? AND (c.status='blocked' OR EXISTS (
            SELECT 1 FROM campaign_stage_attempts a
            WHERE a.campaign_id=c.id AND a.status='unknown')) LIMIT 1""",
        (row["id"],),
    ).fetchone():
        return "exposure_unresolved", None
    for stage in connection.execute(
        """SELECT c.*, st.experiment_id FROM campaign_standing_starts s
        JOIN campaigns c ON c.id=s.campaign_id
        JOIN campaign_stages st ON st.campaign_id=c.id
        WHERE s.standing_approval_id=? AND st.experiment_id IS NOT NULL""",
        (row["id"],),
    ).fetchall():
        if runner._experiment_exposure_unknown(connection, stage, stage):
            return "exposure_unresolved", None
    headroom = _headroom(connection, row["project_id"])
    if headroom is None:
        return "no_project_spend_limit", None
    if headroom < row["per_start_budget_cap_cents"]:
        return "project_spend_headroom", None
    try:
        spec = parse_spec_json(row["spec_json"], providers=True)
    except Exception as error:  # noqa: BLE001 - any parse failure is a permanent invalid spec
        raise _Permanent("spec_invalid") from error
    if spec_digest(spec) != row["spec_digest"]:
        raise _Permanent("spec_digest_changed")
    try:
        runner.store._check_references(connection, live, row["project_id"], spec)
        for stage in normalize(spec).stages:
            runner.store.check_stage_inputs(
                connection,
                stage,
                stage_provider(stage.provider),
                row["trust_policy_digest"],
                spec=normalize(spec),
                scope=(row["org_id"], row["project_id"]),
            )
    except (Conflict, Forbidden, NotFound, KeyError, ValueError) as error:
        return f"{type(error).__name__}: {error}"[:300], None
    except sqlite3.OperationalError:
        raise  # transient storage trouble is retried on the next tick, not a refusal
    except Exception as error:  # noqa: BLE001 - data-class refusals and the like
        return f"{type(error).__name__}: {error}"[:300], None
    return None, spec


def insert_approved_campaign(
    connection,
    *,
    org_id: str,
    project_id: str,
    spec,
    spec_json: str,
    spec_digest_value: str,
    budget_cap_cents: int,
    principal_id: str,
    credential_id: str | None,
    trust_policy_digest: str,
    decision_class: str,
    automation_label: str,
    standing_approval_id: str | None,
    now,
) -> tuple[str, str, str]:
    """Insert a running campaign, its stages and the approval row a human decision stands
    behind (standing approvals, ADR 0042; approved submissions, ADR 0143).

    The approving principal is always the human who made the recorded decision. Returns
    ``(campaign_id, approval_id, expires_at)``; the caller writes its own events."""
    stamp = _iso(now)
    normal = normalize(spec)
    campaign_id = "campaign_" + uuid.uuid4().hex
    approval_id = "approval_" + uuid.uuid4().hex
    connection.execute(
        """INSERT INTO campaigns VALUES (?,?,?,?,?, 'running', NULL, ?, 0, 0, 2, ?, ?, ?, ?)""",
        (
            campaign_id,
            org_id,
            project_id,
            spec_json,
            spec_digest_value,
            budget_cap_cents,
            principal_id,
            credential_id,
            stamp,
            stamp,
        ),
    )
    for position, stage_key in enumerate(topological_order(spec)):
        connection.execute(
            """INSERT INTO campaign_stages (campaign_id, stage_key, topo_index, status,
            prep_phase, input_data_class, output_data_class, revision, updated_at,
            provider_id) VALUES (?, ?, ?, 'pending', 'none', ?, ?, 1, ?, ?)""",
            (
                campaign_id,
                stage_key,
                position,
                stage_input_class(spec, stage_key),
                stage_output_class(spec, stage_key),
                stamp,
                normal.stage(stage_key).provider,
            ),
        )
    expires = _iso(now + timedelta(seconds=normal.deadline_seconds))
    connection.execute(
        """INSERT INTO campaign_approvals (id, campaign_id, spec_digest, approving_principal_id,
        decision_class, automation_label, approved_at, expires_at, status, ended_at,
        trust_policy_digest, standing_approval_id)
        VALUES (?,?,?,?,?,?,?,?, 'active', NULL, ?, ?)""",
        (
            approval_id,
            campaign_id,
            spec_digest_value,
            principal_id,
            decision_class,
            automation_label,
            stamp,
            expires,
            trust_policy_digest,
            standing_approval_id,
        ),
    )
    return campaign_id, approval_id, expires


def _start(connection, row, spec, now, trigger, evaluations=()) -> str:
    stamp = _iso(now)
    normal = normalize(spec)
    per_start = row["per_start_budget_cap_cents"]
    index = row["starts_used"] + 1
    kind = "scheduled" if trigger is None else trigger["kind"]
    retry_number = trigger["retry_number"] if trigger is not None and kind == "retry" else 0
    campaign_id, approval_id, expires = insert_approved_campaign(
        connection,
        org_id=row["org_id"],
        project_id=row["project_id"],
        spec=spec,
        spec_json=row["spec_json"],
        spec_digest_value=row["spec_digest"],
        budget_cap_cents=per_start,
        principal_id=row["created_by_principal_id"],
        credential_id=row["created_credential_id"],
        trust_policy_digest=row["trust_policy_digest"],
        decision_class=CLASS_STANDING_APPROVAL,
        automation_label=f"standing_approval:{row['id']}",
        standing_approval_id=row["id"],
        now=now,
    )
    connection.execute(
        """INSERT INTO campaign_standing_starts (standing_approval_id, start_index, campaign_id,
        approval_id, kind, trigger_campaign_id, retry_number, budget_cap_cents, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            row["id"],
            index,
            campaign_id,
            approval_id,
            kind,
            None if trigger is None else trigger["trigger_campaign_id"],
            retry_number,
            per_start,
            stamp,
        ),
    )
    record_evaluations(
        connection,
        org_id=row["org_id"],
        project_id=row["project_id"],
        campaign_id=campaign_id,
        approval_id=approval_id,
        standing_approval_id=row["id"],
        evaluations=list(evaluations),
        now=stamp,
    )
    revision = row["revision"] + 1
    connection.execute(
        """UPDATE campaign_standing_approvals SET starts_used=?, committed_budget_cents=?,
        last_start_at=?, last_refusal_reason=NULL, revision=?, updated_at=? WHERE id=?""",
        (index, row["committed_budget_cents"] + per_start, stamp, revision, stamp, row["id"]),
    )
    if trigger is not None:
        connection.execute(
            """UPDATE campaign_standing_triggers SET status='started', start_index=?,
            resolved_at=? WHERE trigger_campaign_id=? AND kind=?""",
            (index, stamp, trigger["trigger_campaign_id"], trigger["kind"]),
        )
    actor = _principal(row)
    Store._event(
        connection,
        actor,
        row["project_id"],
        None,
        "campaign.submitted",
        1,
        {
            "campaign_id": campaign_id,
            "spec_digest": row["spec_digest"],
            "stage_keys": [stage.key for stage in normal.stages],
        },
    )
    Store._event(
        connection,
        actor,
        row["project_id"],
        None,
        "campaign.approved",
        2,
        {
            "campaign_id": campaign_id,
            "approval_id": approval_id,
            "spec_digest": row["spec_digest"],
            "expires_at": expires,
            "superseded_approval_id": None,
            "resulting_status": "running",
            "decision_class": CLASS_STANDING_APPROVAL,
            "standing_approval_id": row["id"],
            "start_index": index,
            "kind": kind,
            "standing_decision_class": row["decision_class"],
        },
    )
    _event(
        connection,
        row,
        "campaign.standing_started",
        {
            "campaign_id": campaign_id,
            "approval_id": approval_id,
            "start_index": index,
            "kind": kind,
            "budget_cap_cents": per_start,
        },
        revision,
    )
    return campaign_id


def _attempt(runner, standing_id: str, trigger_key: tuple[str, str] | None, now) -> bool:
    """One candidate in one transaction; True when a campaign was started."""
    with runner.store.runner_connection() as connection:
        runner.hold_lease(connection)
        row = connection.execute(
            "SELECT * FROM campaign_standing_approvals WHERE id=?", (standing_id,)
        ).fetchone()
        trigger = None
        if trigger_key is not None:
            trigger = connection.execute(
                """SELECT * FROM campaign_standing_triggers
                WHERE trigger_campaign_id=? AND kind=? AND status='pending'""",
                trigger_key,
            ).fetchone()
            if trigger is None:
                return False
        if row is None or row["status"] != "active":
            return False
        if _lifecycle(connection, row, now) is not None:
            return False
        if row["not_before"] and now < _parse_utc(row["not_before"]):
            return False
        try:
            reason, spec = _check(runner, connection, row, now)
        except _Permanent as error:
            _refuse(connection, row, str(error))
            if trigger is not None:
                connection.execute(
                    """UPDATE campaign_standing_triggers SET status='abandoned', reason=?,
                    resolved_at=? WHERE trigger_campaign_id=? AND kind=?""",
                    (str(error), _iso(now), *trigger_key),
                )
                _event(
                    connection,
                    row,
                    "campaign.standing_trigger_abandoned",
                    {
                        "trigger_campaign_id": trigger_key[0],
                        "kind": trigger_key[1],
                        "reason": str(error),
                    },
                )
            return False
        evaluations: list[dict[str, Any]] = []
        if reason is None:
            reason = program_budget.check_admission(
                connection, row["project_id"], row["per_start_budget_cap_cents"]
            )
        if reason is None:
            try:
                requirements = parse_unlock_json(row["unlock_json"])
            except ValueError:
                reason = "unlock_not_satisfied:stored_requirements_invalid"
            else:
                if requirements:
                    reason, evaluations = evaluate_unlock(
                        connection, row["org_id"], row["project_id"], requirements
                    )
        if reason is not None:
            # Temporary: a pending trigger stays pending and the standing approval retries.
            _refuse(connection, row, reason)
            return False
        _start(connection, row, spec, now, trigger, evaluations)
        return True


def run_scheduler(runner) -> int:
    """Start due campaigns (at most one per standing approval, ``MAX_STARTS_PER_TICK`` total)."""
    from .campaign_runner import _LeaseLost

    now = runner.now()
    with runner.store.runner_connection() as connection:
        runner.hold_lease(connection)
        for row in connection.execute(
            "SELECT * FROM campaign_standing_approvals WHERE status='active' "
            "ORDER BY created_at, id"
        ).fetchall():
            _lifecycle(connection, row, now)
        _record_triggers(connection, now)
        candidates: list[tuple[str, tuple[str, str] | None]] = []
        seen: set[str] = set()
        for t in connection.execute(
            """SELECT t.* FROM campaign_standing_triggers t
            JOIN campaign_standing_approvals a ON a.id=t.target_standing_approval_id
            WHERE t.status='pending' AND a.status='active'
            ORDER BY t.created_at, t.trigger_campaign_id, t.kind"""
        ):
            if t["target_standing_approval_id"] not in seen:
                seen.add(t["target_standing_approval_id"])
                candidates.append(
                    (t["target_standing_approval_id"], (t["trigger_campaign_id"], t["kind"]))
                )
        for a in connection.execute(
            """SELECT id FROM campaign_standing_approvals
            WHERE status='active' AND "trigger"='schedule' ORDER BY created_at, id"""
        ):
            if a["id"] not in seen:
                seen.add(a["id"])
                candidates.append((a["id"], None))
    started = 0
    for standing_id, trigger_key in candidates:
        if started >= MAX_STARTS_PER_TICK:
            break
        try:
            if _attempt(runner, standing_id, trigger_key, now):
                started += 1
                runner._actions.append(f"standing_start:{standing_id}")
        except _LeaseLost:
            raise
        except Exception as error:  # noqa: BLE001 - one standing approval must not stop the rest
            log.error("standing approval %s scheduler error: %s", standing_id, type(error).__name__)
            runner._actions.append(f"error:{standing_id}:{type(error).__name__}")
    return started


__all__ = [
    "MAX_STARTS_PER_TICK",
    "CreateStandingApproval",
    "RevokeStandingApproval",
    "abandon_triggers",
    "insert_approved_campaign",
    "run_scheduler",
    "running_campaigns",
]
