"""The live path of a campaign Salad route, for the public route view (read-only).

A campaign stage's Salad route is dispatched by the Salad executor through the guardian, not by
control's built-in ``SaladProxy`` (whose transport is permanently disabled). The route view used
to describe only that disabled proxy (``no_go_live_activation_blocked``), which misled readers into
thinking a route that was actually running was blocked. For a route that belongs to a campaign
attempt this module reports what is recorded about the real path instead:

- which dispatcher drives it,
- the executor's registered worker: lifecycle state and heartbeat age,
- the execution lease the attempt holds: held or expired,
- what the guardian has recorded for the attempt (evidence, live receipt, final state), and
- a readiness reading derived from those facts.

Nothing here enables, authorizes or renews anything: ``readiness`` is a reading, never a grant,
and the guardian's *conformance* is not asserted by this view (it names only what is recorded).
The built-in proxy's state stays visible, under its own ``builtin_proxy`` key.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from .store import _parse_utc
from .workers import HEARTBEAT_MAX_AGE, derive_credential_status

DISPATCHER = "salad_executor_guardian"
CONTROL_SURFACE = "campaign_executor_guardian"
# Phases in which the executor still drives the attempt (the ones it asks control for).
ACTIVE_PHASES = (
    "awaiting_lease",
    "leased",
    "awaiting_review",
    "reviewed",
    "capability_ready",
    "route_proposed",
    "route_started",
    "executing",
)
# The worker registry's own bound for a fresh heartbeat.
HEARTBEAT_MAX_AGE_SECONDS = int(HEARTBEAT_MAX_AGE.total_seconds())
# Before the attempt is leased there is no lease to read yet.
_LEASE_NOT_YET = frozenset({"awaiting_lease"})


def _age(now: datetime, stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        return max(0, int((now - _parse_utc(stamp)).total_seconds()))
    except (ValueError, TypeError):
        return None


def campaign_live_path(
    db: sqlite3.Connection, org_id: str, route_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """The recorded live path of the campaign attempt that owns ``route_id``, else ``None``."""
    now = now or datetime.now(UTC)
    row = db.execute(
        """SELECT sa.phase, sa.lease_id, sa.salad_evidence_digest, sa.live_receipt_digest,
        sa.guardian_final_json, c.project_id, a.campaign_id, a.stage_key, a.attempt,
        g.worker_principal_id
        FROM campaign_salad_attempts sa
        JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
        JOIN campaigns c ON c.id = a.campaign_id
        LEFT JOIN execution_grants g ON g.id = a.grant_id
        WHERE sa.route_id = ? AND c.org_id = ?""",
        (route_id, org_id),
    ).fetchone()
    if row is None:
        return None
    reasons: list[str] = []
    executor: dict[str, Any] = {"registered": False}
    worker = None
    if row["worker_principal_id"] is not None:
        worker = db.execute(
            "SELECT r.id, r.lifecycle_state, r.last_seen_at, r.project_id, "
            "k.revoked_at AS credential_revoked_at, k.expires_at AS credential_expires_at "
            "FROM worker_registry r LEFT JOIN credentials k ON k.id = r.credential_id "
            "WHERE r.worker_principal_id = ? AND r.org_id = ?",
            (row["worker_principal_id"], org_id),
        ).fetchone()
    if worker is not None:
        age = _age(now, worker["last_seen_at"])
        fresh = age is not None and age <= HEARTBEAT_MAX_AGE_SECONDS
        executor = {
            "registered": True,
            "lifecycle_state": worker["lifecycle_state"],
            "heartbeat_age_seconds": age,
            "heartbeat_max_age_seconds": HEARTBEAT_MAX_AGE_SECONDS,
            "freshness": "fresh" if fresh else "stale",
        }
        if worker["lifecycle_state"] == "retired":
            reasons.append("executor_retired")
        elif worker["lifecycle_state"] != "ready":
            # require_ready_worker refuses every state but ready (draining, disconnected, ...).
            reasons.append(f"executor_not_ready:{worker['lifecycle_state']}")
        if not fresh and worker["lifecycle_state"] != "retired":
            reasons.append("executor_heartbeat_stale")
        # The store's credential and project checks, reusing workers' credential derivation.
        credential = (
            derive_credential_status(
                worker["credential_revoked_at"], worker["credential_expires_at"], now
            )
            if worker["credential_expires_at"] is not None
            or worker["credential_revoked_at"] is not None
            else "expiry_unknown"
        )
        if credential != "active":
            reasons.append(f"executor_credential_{credential}")
        if worker["project_id"] != row["project_id"]:
            reasons.append("executor_project_mismatch")
    else:
        reasons.append("executor_not_registered")
    lease: dict[str, Any] = {"state": "none"}
    if row["lease_id"] is not None:
        held = db.execute(
            "SELECT expires_at, renewed_at FROM execution_leases WHERE id = ?",
            (row["lease_id"],),
        ).fetchone()
        if held is not None:
            try:
                left = int((_parse_utc(held["expires_at"]) - now).total_seconds())
            except (ValueError, TypeError):
                left = -1
            lease = {
                "state": "held" if left > 0 else "expired",
                "expires_in_seconds": max(0, left),
                "renewed_age_seconds": _age(now, held["renewed_at"]),
            }
    active = row["phase"] in ACTIVE_PHASES
    if active and row["phase"] not in _LEASE_NOT_YET:
        if lease["state"] == "none":
            reasons.append("no_lease")
        elif lease["state"] == "expired":
            reasons.append("lease_expired")
    if not active:
        readiness = "finished"
    elif not reasons:
        readiness = "ready"
    elif any(r != "executor_heartbeat_stale" and r.startswith("executor_") for r in reasons):
        # Refused outright by the store (not merely stale): not registered, retired, not ready,
        # credential revoked/expired, or another project's worker.
        readiness = "not_ready"
    else:
        readiness = "degraded"
    return {
        "applies_to": "campaign_stage",
        "dispatcher": DISPATCHER,
        "campaign_id": row["campaign_id"],
        "stage_key": row["stage_key"],
        "attempt": row["attempt"],
        "attempt_phase": row["phase"],
        "executor": executor,
        "lease": lease,
        "guardian": {
            "evidence_recorded": row["salad_evidence_digest"] is not None,
            "live_receipt_recorded": row["live_receipt_digest"] is not None,
            "final_recorded": row["guardian_final_json"] is not None,
            "conformance": "not_attested_by_this_view",
        },
        "readiness": readiness,
        "reasons": reasons,
        "authorizes_nothing": True,
    }


def builtin_proxy_view(safety: dict[str, Any]) -> dict[str, Any]:
    """The built-in SaladProxy's state, labelled as what it is: not the path campaigns use."""
    return {
        "state": "disabled",
        "go_no_go": "no_go_live_activation_blocked",
        "control_surface": "desktop_control_only",
        "proxy_safety": safety,
        "applies_to": "routes dispatched through control's built-in proxy only",
        "campaign_stages": "dispatched by the Salad executor through the guardian: see live_path",
    }
