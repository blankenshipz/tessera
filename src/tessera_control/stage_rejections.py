"""Record a control-plane refusal of a stage's executor step as a bounded stage event.

The Salad executor acts as the worker through the control stores. When a store refuses a step
(a lease, the lease package, the grant poll, a renewal) the refusal used to survive only in the
executor's own journal. ``record`` appends one ``campaign.stage_rejection`` event carrying a short
stable code, the step name, the attempt number and the time, so ``tessera_campaign_stage_status``
can show it. The event holds no message text, host, address, token or payload. It is bounded and
deduplicated: the same (attempt, step, code) is not re-recorded within ``DEDUP_SECONDS``, and a
stage keeps at most ``MAX_PER_STAGE`` such events. Recording never changes a refusal's outcome.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from .store import Conflict, Forbidden, NotFound, StoreError, _parse_utc

EVENT = "campaign.stage_rejection"
STEPS = frozenset(
    {
        "grant_poll",
        "lease",
        "package",
        "record_package",
        "renew",
        "execute",
        "mark_executing",
        "observe",
    }
)
DEDUP_SECONDS = 600
MAX_PER_STAGE = 20
_CODE = re.compile(r"[a-z0-9_]{1,48}")

# First match wins. Matched against the refusal text, which is never stored: only the code is.
_KNOWN = (
    ("not running", "campaign_not_running"),
    ("already has a lease", "lease_already_held"),
    ("authority expires", "worker_authority_expiring"),
    ("heartbeat", "stale_heartbeat"),
    ("epoch", "stale_epoch"),
    ("expired", "lease_expired"),
    ("capability is required", "capability_required"),
    ("unauthorized", "worker_unauthorized"),
)


def code_for(error: BaseException) -> str:
    """A short stable code for a store refusal; free text never leaves this function."""
    text = str(error).lower()
    for needle, code in _KNOWN:
        if needle in text:
            return code
    if isinstance(error, Conflict):
        return "refused_conflict"
    if isinstance(error, Forbidden):
        return "refused_forbidden"
    if isinstance(error, NotFound):
        return "refused_not_found"
    if isinstance(error, StoreError):
        return "refused"
    return "unexpected_error"  # not a store refusal: only the fact is recorded, never the text


def record(store: Any, worker: dict[str, Any], attempt_id: str, step: str, error: BaseException):
    """Append the rejection event for ``attempt_id`` unless bounded out. Returns True if written.

    ``store`` is a CampaignStore; ``worker`` is the worker's authenticated principal. Any
    exception is recorded, a store refusal under its specific code and anything else as
    ``unexpected_error``, and only for an attempt of this worker's grant in its own organization.
    """
    if step not in STEPS:
        return False
    code = code_for(error)
    if not _CODE.fullmatch(code):
        return False
    with store.runner_connection() as connection:
        row = connection.execute(
            """SELECT a.campaign_id, a.stage_key, a.attempt FROM campaign_stage_attempts a
            JOIN campaigns c ON c.id = a.campaign_id
            JOIN execution_grants g ON g.id = a.grant_id
            WHERE a.id = ? AND c.org_id = ? AND g.worker_principal_id = ?""",
            (attempt_id, worker["org_id"], worker["id"]),
        ).fetchone()
        if row is None:
            return False
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (row["campaign_id"],)
        ).fetchone()
        base = (
            "SELECT created_at FROM events WHERE project_id=? AND event_type=? "
            "AND json_extract(payload_json,'$.campaign_id')=? "
            "AND json_extract(payload_json,'$.stage_key')=? "
        )
        args = (campaign["project_id"], EVENT, row["campaign_id"], row["stage_key"])
        total = connection.execute(base.replace("created_at", "count(*) AS n"), args).fetchone()[
            "n"
        ]
        if total >= MAX_PER_STAGE:
            return False
        last = connection.execute(
            base + "AND json_extract(payload_json,'$.attempt')=? "
            "AND json_extract(payload_json,'$.step')=? "
            "AND json_extract(payload_json,'$.code')=? ORDER BY cursor DESC LIMIT 1",
            (*args, row["attempt"], step, code),
        ).fetchone()
        if last is not None:
            try:
                age = (datetime.now(UTC) - _parse_utc(last["created_at"])).total_seconds()
            except ValueError:
                age = DEDUP_SECONDS
            if age < DEDUP_SECONDS:
                return False
        store.record_runner_event(
            connection,
            campaign,
            EVENT,
            {
                "campaign_id": row["campaign_id"],
                "stage_key": row["stage_key"],
                "attempt": row["attempt"],
                "step": step,
                "code": code,
            },
        )
        return True


def attempt_for(store: Any, worker: dict[str, Any], grant_id: str | None, lease_id: str | None):
    """The campaign attempt id behind a grant or a lease of this worker, else None.

    A lease counts only when this worker holds it, so a refusal on another worker's lease
    (for example after its grant was reassigned) is never recorded against this attempt."""
    from .store import _connect

    connection = _connect(store.database_path)
    try:
        row = connection.execute(
            """SELECT a.id FROM campaign_stage_attempts a
            JOIN execution_grants g ON g.id = a.grant_id
            LEFT JOIN execution_leases l ON l.grant_id = g.id
            WHERE g.worker_principal_id = ?
              AND (g.id = ? OR (l.id = ? AND l.worker_principal_id = ?))
            ORDER BY a.attempt DESC, a.created_at DESC, a.id DESC LIMIT 1""",
            (worker["id"], grant_id, lease_id, worker["id"]),
        ).fetchone()
        return None if row is None else row["id"]
    finally:
        connection.close()


@contextmanager
def recording(
    store: Any,
    worker: dict[str, Any],
    step: str,
    *,
    grant_id: str | None = None,
    lease_id: str | None = None,
) -> Iterator[None]:
    """Wrap a worker-facing control call of ANY provider: a refusal is recorded, then re-raised.

    The wrapped call's outcome is unchanged (the same exception propagates). Recording failures
    are swallowed, so a stuck recorder can never turn into a different refusal.
    """
    try:
        yield
    except Exception as error:
        try:
            attempt_id = attempt_for(store, worker, grant_id, lease_id)
            if attempt_id is not None:
                record(store, worker, attempt_id, step, error)
        except Exception:  # noqa: BLE001 - recording must never change the refusal
            pass
        raise
