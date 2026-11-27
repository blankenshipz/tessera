"""A Salad lease that no fresh lease could make long enough (ADR 0207 amendment).

The pre-route window check (``campaign_salad_lease_lapse``) requeues an attempt whose lease lost
too much of its hard life to waiting, so the next attempt gets a fresh grant and lease. That
helps only when a fresh lease would be long enough. A lease's hard expiry is the earliest of the
grant's expiry, the worker credential's expiry and the task's own bound (declared duration plus
the provisioning allowance); a fresh grant is in turn bounded by the campaign approval's expiry,
the stage's grant lifetime and the approver's and worker's credentials. When one of those expiry
bounds leaves less than the run needs, every fresh lease is as short, and a requeue would only
loop until the identical-repeat bound and then spend a scientific retry.

So, before requeueing, the runner asks how long a fresh lease could be at most (``ceiling``):

* long enough: the requeue goes ahead as before;
* too short: the attempt is unwound the same way (nothing was sent, released at 0 on the
  store's own no-claim proof) and closed ``failed_transient`` / ``lease_capped_by_expiry``,
  which is not a scientific retry and not a not-started requeue. The campaign event
  ``campaign.salad_lease_capped_by_expiry`` records which bound binds and by how much. The stage
  then waits, placing no new attempt, until a fresh lease could cover the run again: an operator
  renews the expiring credential through its own renewal path, or a person re-approves the
  campaign (an expired approval pauses the campaign as before). Nothing here renews, extends or
  re-approves anything.
* too short because of the stage's own grant lifetime (``grant_ttl_seconds``, a static spec
  bound no renewal or re-approval changes): waiting could never end, so the attempt is unwound
  the same way and closed ``failed_permanent`` / ``lease_bound_by_grant_ttl`` (0, a
  configuration error, not a scientific retry), and the stage fails with that reason. Salad
  admission refuses such a stage up front (``admission_refusal``); this covers one admitted
  before that rule.

Every bound is read from the store; the ceiling is an upper bound (a fresh attempt spends some
time before its lease), so "too short" is never guessed in the attempt's favour.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from .remote_execution_models import SALAD_PROVISIONING_ALLOWANCE_SECONDS
from .store import _parse_utc

LEASE_CAPPED_BY_EXPIRY = "lease_capped_by_expiry"
LEASE_BOUND_BY_GRANT_TTL = "lease_bound_by_grant_ttl"
# The one bound read from the stage spec: no credential renewal or re-approval can lift it.
STATIC_BINDING = "grant_lifetime"
CAPPED_EVENT = "campaign.salad_lease_capped_by_expiry"
WAITING_EVENT = "campaign.salad_lease_cap_waiting"


def needed_seconds(window_seconds: int, max_duration_seconds: int | None) -> int:
    """What a route's lease must still cover: the route window, but never more than a lease is
    ever issued for (the declared duration plus the provisioning allowance), since no fresh
    lease could give more than that either."""
    if max_duration_seconds is None:
        return int(window_seconds)
    issued_for = int(max_duration_seconds) + SALAD_PROVISIONING_ALLOWANCE_SECONDS
    return min(int(window_seconds), issued_for)


def admission_refusal(
    grant_ttl_seconds: int, max_duration_seconds: int, route_max_seconds: int
) -> str | None:
    """Why a Salad stage's grant lifetime can never give a lease what its route needs, or None.

    A lease is issued for at most the declared duration plus the provisioning allowance and the
    route window is at most the profile's ``route_max_seconds`` (or the plan's own headroom);
    the run a lease must cover is the smaller (``needed_seconds``). A grant lifetime below it
    caps every lease short, so after any wait the stage could only stop."""
    from .remote_execution_models import route_window_limit

    window = min(
        int(route_max_seconds), route_window_limit("salad_container", int(max_duration_seconds))
    )
    need = needed_seconds(window, max_duration_seconds)
    if int(grant_ttl_seconds) >= need:
        return None
    return (
        f"{LEASE_BOUND_BY_GRANT_TTL}: grant_ttl_seconds {int(grant_ttl_seconds)} is shorter than "
        f"the {need}s a lease must cover (the route window, at most max_duration_seconds "
        f"{int(max_duration_seconds)} plus the {SALAD_PROVISIONING_ALLOWANCE_SECONDS}s "
        "provisioning allowance); raise grant_ttl_seconds or lower max_duration_seconds"
    )


def ceiling(
    runner: Any,
    approval: Mapping[str, Any] | None,
    worker_principal_id: str | None,
    grant_ttl_seconds: int | None,
    now: datetime,
) -> tuple[datetime, str] | None:
    """The latest hard expiry a fresh grant and lease could have, and which bound sets it.

    The bounds a fresh grant or lease takes that do not move with time: the campaign approval's
    expiry, the approver's and the worker's unrevoked credentials (every one, as the release and
    grant limits read them), and the stage's grant lifetime from now. None when none is known."""
    bounds: list[tuple[datetime, str]] = []
    if grant_ttl_seconds is not None:
        bounds.append((now + timedelta(seconds=int(grant_ttl_seconds)), "grant_lifetime"))
    if approval is not None and approval.get("expires_at"):
        bounds.append((_parse_utc(approval["expires_at"]), "campaign_approval"))
    for principal, name in (
        (None if approval is None else approval.get("approving_principal_id"), "approver"),
        (worker_principal_id, "worker"),
    ):
        if principal is None:
            continue
        for row in runner._rows(
            """SELECT expires_at FROM credentials WHERE revoked_at IS NULL
            AND expires_at IS NOT NULL AND principal_id=?""",
            (principal,),
        ):
            bounds.append((_parse_utc(row["expires_at"]), f"{name}_credential"))
    if not bounds:
        return None
    return min(bounds, key=lambda bound: bound[0])


def shortfall(
    runner: Any,
    approval: Mapping[str, Any] | None,
    worker_principal_id: str | None,
    grant_ttl_seconds: int | None,
    need_seconds: int,
    now: datetime,
) -> dict[str, Any] | None:
    """The capped record when no fresh lease could cover ``need_seconds`` from now, else None.

    A grant lifetime below the need is named first even when a renewable bound is earlier: it
    would still cap every lease after that bound were renewed, so waiting could never end."""
    if grant_ttl_seconds is not None and int(grant_ttl_seconds) < need_seconds:
        until = now + timedelta(seconds=int(grant_ttl_seconds))
        return {
            "binding": STATIC_BINDING,
            "need_seconds": int(need_seconds),
            "available_seconds": max(0, int(grant_ttl_seconds)),
            "ceiling": until.isoformat().replace("+00:00", "Z"),
        }
    found = ceiling(runner, approval, worker_principal_id, grant_ttl_seconds, now)
    if found is None:
        return None
    until, binding = found
    available = int((until - now).total_seconds())
    if available >= need_seconds:
        return None
    return {
        "binding": binding,
        "need_seconds": int(need_seconds),
        "available_seconds": max(0, available),
        "ceiling": until.isoformat().replace("+00:00", "Z"),
    }


def is_static(record: Mapping[str, Any]) -> bool:
    """True when the binding bound is the stage's grant lifetime (waiting cannot lift it)."""
    return record.get("binding") == STATIC_BINDING


# What lifts each renewable bound; the grant lifetime has none (a spec change).
_REMEDY = {
    "worker_credential": "the worker credential to be renewed by an operator",
    "approver_credential": "the approver's credential to be renewed",
    "campaign_approval": "the campaign to be re-approved",
}


def detail(record: Mapping[str, Any]) -> str:
    binding = record["binding"]
    head = (
        f"no fresh lease could cover the route; the {binding} leaves "
        f"{record['available_seconds']}s of the {record['need_seconds']}s needed; nothing was "
        "sent to the provider; released at 0; "
    )
    if binding == STATIC_BINDING:
        return (
            f"{LEASE_BOUND_BY_GRANT_TTL}: {head}the stage's grant_ttl_seconds is a spec bound no "
            "renewal or re-approval changes: raise grant_ttl_seconds or lower "
            "max_duration_seconds and resubmit"
        )
    remedy = _REMEDY.get(binding, "the expiring bound to be renewed")
    return f"{LEASE_CAPPED_BY_EXPIRY}: {head}waiting for {remedy}"


def capped_attempts(attempts: list[Mapping[str, Any]]) -> int:
    """Attempts closed because no fresh lease could cover the run: never a scientific retry."""
    return sum(1 for attempt in attempts if attempt["failure_class"] == LEASE_CAPPED_BY_EXPIRY)


def recorded(runner: Any, ctx: Any, attempt_id: str) -> dict[str, Any] | None:
    """The capped record emitted for this attempt, or None."""
    import json

    row = runner._one(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.attempt_id')=? LIMIT 1""",
        (ctx.campaign["project_id"], CAPPED_EVENT, ctx.campaign["id"], attempt_id),
    )
    return None if row is None else json.loads(row["payload_json"])


def still_capped(
    runner: Any, ctx: Any, latest: Mapping[str, Any], grant_ttl_seconds: int | None, now: datetime
) -> dict[str, Any] | None:
    """For a stage whose latest attempt closed ``lease_capped_by_expiry``: the current capped
    record while a fresh lease still could not cover the run, else None (place the next
    attempt). An unreadable record keeps waiting (fails closed: no loop)."""
    if latest["failure_class"] != LEASE_CAPPED_BY_EXPIRY:
        return None
    record = recorded(runner, ctx, latest["id"])
    if record is None or type(record.get("need_seconds")) is not int:
        return {"binding": "unreadable", "need_seconds": None, "available_seconds": None}
    return shortfall(
        runner,
        ctx.approval,
        record.get("worker_principal_id"),
        grant_ttl_seconds,
        record["need_seconds"],
        now,
    )


__all__ = [
    "CAPPED_EVENT",
    "LEASE_BOUND_BY_GRANT_TTL",
    "LEASE_CAPPED_BY_EXPIRY",
    "STATIC_BINDING",
    "WAITING_EVENT",
    "admission_refusal",
    "capped_attempts",
    "ceiling",
    "detail",
    "is_static",
    "needed_seconds",
    "shortfall",
    "still_capped",
]
