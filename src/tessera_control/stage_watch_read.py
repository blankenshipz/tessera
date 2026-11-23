"""Read the recorded stage watch reports of a campaign (ADR 0087, 0111, 0125).

``GET /campaigns/{campaign_id}/stage-watch`` shows, per stage attempt, the state changes, stalls,
resource-limit signals and decision requests the worker-side watcher reported, newest first and
paged. It needs the enrolled ``campaign:logs`` action for an external agent (a human is
tenant-checked only), stays inside the credential's organization and project, and selects a
closed allowlist: closed kind and reason codes, integer counters, timestamps, sequence numbers
and the stage key and attempt number. It never returns a path, a log line, a prompt or any
worker free text. Every value is an unverified worker claim (``trusted_worker_assertion``): this
is observation, never evidence of success, cost or custody, and the route changes nothing.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request

from .agent_campaign_reads import BOUNDARY, LOGS_SCOPE, AgentCampaignReads
from .external_agent_api import Actor
from .stage_watch import EVENT_TYPE, OBSERVATION_PROOF
from .stage_watch_decisions import DECISION_EVENT_TYPE
from .stage_watch_limits import DECISION_KIND_REASONS
from .stage_watch_models import WATCH_KINDS
from .store import StoreError, _connect

COUNTER_NAMES = (
    "elapsed_s",
    "silent_s",
    "mem_bytes",
    "cpu_ms",
    "output_bytes",
    "restarts",
    "refused_reports",
    "dropped_reports",
)
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
_ADMISSIONS = ("not_admitted",)
_JOIN = """FROM campaigns c
    JOIN campaign_stage_attempts a ON a.campaign_id = c.id
    JOIN events e ON e.project_id = c.project_id AND e.event_type IN (?, ?)
    AND json_extract(e.payload_json, '$.operation_id') = a.operation_id
    AND json_extract(e.payload_json, '$.grant_id') = a.grant_id
    WHERE c.id = ? AND a.operation_id IS NOT NULL"""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _report_item(stage_key: str, attempt: int, record: dict[str, Any]) -> dict[str, Any]:
    counters = record.get("counters")
    counters = counters if isinstance(counters, dict) else {}
    kind = record.get("kind")
    return {
        "type": "report",
        "stage_key": stage_key,
        "attempt": attempt,
        "kind": kind if kind in WATCH_KINDS else None,
        "seq": _int(record.get("seq")),
        "observed_at": record.get("observed_at")
        if isinstance(record.get("observed_at"), str)
        else None,
        "counters": {n: counters[n] for n in COUNTER_NAMES if _int(counters.get(n)) is not None},
        "kill_switch_engaged": bool(record.get("kill_switch_engaged")),
    }


def _decision_item(stage_key: str, attempt: int, record: dict[str, Any]) -> dict[str, Any]:
    kind, reason = record.get("kind"), record.get("reason")
    allowed = DECISION_KIND_REASONS.get(kind, ()) if isinstance(kind, str) else ()
    bounds = record.get("bounds")
    return {
        "type": "decision",
        "stage_key": stage_key,
        "attempt": attempt,
        "kind": kind if kind in DECISION_KIND_REASONS else None,
        "reason": reason if reason in allowed else None,
        "decision_seq": _int(record.get("decision_seq")),
        "requested_memory_bytes": _int(record.get("requested_memory_bytes")),
        "within_envelope": bool(bounds.get("within_envelope"))
        if isinstance(bounds, dict)
        else None,
        "admission": record.get("admission") if record.get("admission") in _ADMISSIONS else None,
        "observed_at": record.get("observed_at")
        if isinstance(record.get("observed_at"), str)
        else None,
    }


class StageWatchRead(AgentCampaignReads):
    def campaign_stage_watch(
        self, principal, campaign_id: str, offset: int = 0, limit: int = DEFAULT_LIMIT
    ) -> dict[str, Any]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= MAX_LIMIT:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, LOGS_SCOPE)
            self._campaign(connection, actor, campaign_id)
            args = (EVENT_TYPE, DECISION_EVENT_TYPE, campaign_id)
            summary: dict[tuple[str, int], dict[str, Any]] = {}
            for row in connection.execute(
                f"""SELECT a.stage_key, a.attempt, e.event_type,
                json_extract(e.payload_json, '$.kind') AS kind, count(*) AS n,
                max(json_extract(e.payload_json, '$.observed_at')) AS last_seen {_JOIN}
                GROUP BY a.stage_key, a.attempt, e.event_type, kind
                ORDER BY a.stage_key, a.attempt""",
                args,
            ):
                entry = summary.setdefault(
                    (row["stage_key"], row["attempt"]),
                    {
                        "stage_key": row["stage_key"],
                        "attempt": row["attempt"],
                        "counts_by_kind": dict.fromkeys(WATCH_KINDS, 0),
                        "decisions_recorded": 0,
                        "last_seen": None,
                    },
                )
                if row["event_type"] == DECISION_EVENT_TYPE:
                    entry["decisions_recorded"] += row["n"]
                elif row["kind"] in WATCH_KINDS:
                    entry["counts_by_kind"][row["kind"]] += row["n"]
                seen = row["last_seen"]
                if isinstance(seen, str) and (
                    entry["last_seen"] is None or seen > entry["last_seen"]
                ):
                    entry["last_seen"] = seen
            total = sum(
                sum(e["counts_by_kind"].values()) + e["decisions_recorded"]
                for e in summary.values()
            )
            rows = connection.execute(
                f"""SELECT a.stage_key, a.attempt, e.event_type, e.payload_json {_JOIN}
                ORDER BY e.cursor DESC LIMIT ? OFFSET ?""",
                (*args, limit, offset),
            ).fetchall()
            items = []
            for row in rows:
                record = json.loads(row["payload_json"])
                build = _decision_item if row["event_type"] == DECISION_EVENT_TYPE else _report_item
                items.append(build(row["stage_key"], row["attempt"], record))
            return {
                "campaign_id": campaign_id,
                "attempts": list(summary.values()),
                "items": items,
                "total": total,
                "offset": offset,
                "limit": limit,
                "proof": OBSERVATION_PROOF,
                "claims_unverified": True,
                "boundary": BOUNDARY
                + " Worker watch reports are unverified claims; they never decide cost or success.",
            }
        finally:
            connection.close()


router = APIRouter()


@router.get("/campaigns/{campaign_id}/stage-watch")
def stage_watch(
    campaign_id: str,
    request: Request,
    actor: Actor,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    return request.app.state.stage_watch_read.campaign_stage_watch(
        actor, campaign_id, offset, limit
    )
