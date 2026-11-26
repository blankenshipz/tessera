"""Plain-words campaign report for the owner, built from the campaign's receipt.

The report states only what the receipt, the attempt rows and the model-spend ledger record.
A figure that is not recorded yet is ``None`` in the dict and said so in the text; nothing is
estimated. Without ``detail`` no internal id, digest or path appears anywhere in the result.
Tenant access is the caller's job; every read here is still filtered by the campaign's own
organization and project.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from . import campaign_hold
from .campaign_models import parse_spec_json
from .campaign_read_scope import own_clause
from .campaign_receipt_read import load_verified_receipt
from .campaign_receipts import build_receipt
from .model_spend import MICRO

REPORT_SCHEMA = "tessera.campaign-report.v1"

_TERMINAL = {"succeeded", "stopped", "failed", "cancelled"}
_MICRO_PER_DOLLAR = MICRO * 100

_FAILURES = {
    "t1_grant_expired_unleased": "the permission to run expired before a worker took the job",
    "t2_declared_exit_code": "the program exited with a code the campaign declares as a failure",
    "undeclared_exit_code": "the program exited with a code the campaign did not declare",
    "salad_not_passed": "the cloud run did not pass its checks",
    "route_deadline_reached": "the cloud run was stopped at its route deadline",
    "observed_unknown": "the platform could not confirm what happened to the run",
    "quarantine_failed": "the outputs could not be collected safely",
    "missing_receipt": "the worker never sent its run receipt",
    "missing_manifest": "the worker never reported its output files",
    "step_refused": "a platform step was refused",
    "budget_refused": "the remaining budget did not allow it to start",
    "exposure_unknown": "the possible cost exposure could not be established",
    "t1_salad_unclaimed": "no cloud worker picked up the job",
    "infra_container_killed": "the infrastructure killed the container",
    "infra_resource_limit": "the container hit a resource limit",
    "infra_allocation_timeout": "no cloud instance was allocated in time",
    "not_started": "it was never started",
    "exit_confirmed_unresolved": "the program was confirmed to have exited but its result could "
    "not be resolved",
    "exited_without_report": "the program's container was seen to exit but the worker never "
    "reported the result",
    "cancelled": "it was cancelled",
    "holdout_already_claimed": "the held-out data had already been used",
    "sealed_partition": "it needed data from a sealed partition",
    "public_fetch_unreported": "the worker never reported fetching its pinned public files",
    "egress_violation": "it transferred data outside the stage's egress allowlist",
    "prestart_no_spend": "it never started on that machine class and cost nothing, so the next "
    "class was tried",
}

_STATUS_WORDS = {
    "succeeded": "finished",
    "failed": "failed",
    "stopped": "was stopped early",
    "cancelled": "was cancelled",
    "submitted": "has been submitted but has not started",
    "running": "is still running",
    "stopping": "is stopping",
    "cancel_requested": "is being cancelled",
    "blocked": "is blocked",
    "paused_delegation_lapsed": "is paused because the delegated approval lapsed",
}

_SKIP_WORDS = {
    "no_free_slot": "had no free slot",
    "project_limits": "was at the project limits",
    "exposure_open": "had unresolved cost exposure",
    "program_budget": "was over the program budget",
    "budget_in_flight": "had budget still in flight",
    "credential_unavailable": "had no usable credential",
    "excluded_by_steering": "was excluded by an operator",
    "not_pinned": "was not the pinned choice",
    "untrusted": "was not trusted",
    "not_enrolled": "was not enrolled",
    "over_campaign_budget": "would not fit the campaign budget",
}


# -- formatting ----------------------------------------------------------------------------


def _dollars_from_cents(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def _dollars_from_micro(micro: int) -> str:
    """Exact dollars from micro-cents: at least two decimals, more only when sub-cent."""
    whole, rest = divmod(micro, _MICRO_PER_DOLLAR)
    decimals = f"{rest:08d}".rstrip("0").ljust(2, "0")
    return f"${whole}.{decimals}"


def _duration_text(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min" + (f" {rest} s" if rest else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h" + (f" {minutes} min" if minutes else "")


def _size_text(size: int | None) -> str | None:
    if size is None:
        return None
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _seconds_between(start: str | None, end: str | None) -> int | None:
    first, last = _parse_time(start), _parse_time(end)
    if first is None or last is None:
        return None
    return max(0, int((last - first).total_seconds()))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(value: str) -> str:
    return value.replace("_", " ")


def _failure_words(failure_class: str | None, exit_code: int | None = None) -> str:
    text = _FAILURES.get(failure_class or "", _plain(failure_class or "an unrecorded reason"))
    if failure_class in {"undeclared_exit_code", "t2_declared_exit_code"} and exit_code is not None:
        text += f" (exit code {exit_code})"
    return text


# -- receipt access ------------------------------------------------------------------------


def _receipt_for(
    connection: sqlite3.Connection, campaign: sqlite3.Row
) -> tuple[dict[str, Any], str | None]:
    """The terminal receipt as stored (refused unless its digest verifies), else one built
    now from the recorded rows."""
    verified = load_verified_receipt(connection, campaign["id"])
    if verified is not None:
        return verified.receipt, verified.digest
    receipt, _digest = build_receipt(connection, campaign, campaign["status"])
    return receipt, None


def _spec_name(spec_json: str) -> str | None:
    try:
        name = getattr(parse_spec_json(spec_json), "name", None)
    except Exception:  # an unreadable spec simply has no name  # noqa: BLE001
        return None
    return name if isinstance(name, str) and name else None


# -- stages --------------------------------------------------------------------------------


def _what_ran(runtime: str | None, provider_id: str | None) -> str:
    local = provider_id == "local-docker"
    if runtime == "docker-cpu-v1":
        return "a container on the local machine" if local else "a container"
    if runtime == "salad-workload-v1":
        return "a GPU workload in the Salad cloud"
    return f"a {runtime} job" if runtime else "a job of an unrecorded kind"


def _where(attempts: list[dict[str, Any]], stage: dict[str, Any]) -> str | None:
    providers: list[str] = []
    for attempt in attempts:
        provider = attempt.get("provider_id")
        if provider and (not providers or providers[-1] != provider):
            providers.append(provider)
    final = providers[-1] if providers else stage.get("provider_id")
    if final is None:
        return None
    for attempt in reversed(attempts):
        skipped = (attempt.get("placement") or {}).get("skipped") or []
        if skipped:
            first = skipped[0]
            why = _SKIP_WORDS.get(first.get("reason"), _plain(str(first.get("reason"))))
            return f"{final} (moved there because {first.get('provider_id')} {why})"
    if len(providers) > 1:
        return f"{final} (moved there from {providers[-2]} after an earlier attempt failed)"
    return final


def _io_items(attempts: list[dict[str, Any]], direction: str) -> list[dict[str, Any]]:
    """The recorded data of the last attempt that recorded any in this direction."""
    for attempt in reversed(attempts):
        items = [item for item in attempt.get("io", []) if item["direction"] == direction]
        if items:
            return [
                {
                    "name": item["name"],
                    "size_bytes": item["size_bytes"],
                    "size_text": _size_text(item["size_bytes"]),
                    "data_class": item["data_class"],
                }
                for item in items
            ]
    return []


def _egress(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    entries = [a["egress"] for a in attempts if a.get("egress") is not None]
    if not entries:
        return None
    journals = [e["journal"] for e in entries if e.get("journal") is not None]
    allowed = sorted({d["host"] for e in entries for d in e["policy"].get("destinations", [])})
    result: dict[str, Any] = {
        "reported": len(journals) == len(entries),
        "allowed_hosts": allowed,
        "hosts": None,
        "denied_hosts": None,
        "bytes": None,
        "bytes_up": None,
        "bytes_down": None,
        "calls": None,
        "calls_denied": None,
        "cap_exceeded": None,
        "destinations": [],
    }
    if not journals:
        return result
    per_host: dict[tuple[str, str], list[int]] = {}
    for journal in journals:
        for item in journal["summary"]["destinations"]:
            row = per_host.setdefault((item["host"], item["decision"]), [0, 0, 0])
            row[0] += item["bytes_up"]
            row[1] += item["bytes_down"]
            row[2] += item["connections"]
    result["destinations"] = [
        {
            "host": host,
            "decision": decision,
            "bytes_up": up,
            "bytes_down": down,
            "connections": calls,
        }
        for (host, decision), (up, down, calls) in sorted(per_host.items())
    ]
    summaries = [j["summary"] for j in journals]
    result["hosts"] = sorted({h for (h, d), v in per_host.items() if d == "allow" and v[2]})
    result["denied_hosts"] = sorted({h for (h, d), v in per_host.items() if d == "deny"})
    result["bytes_up"] = sum(s["bytes_up"] for s in summaries)
    result["bytes_down"] = sum(s["bytes_down"] for s in summaries)
    result["bytes"] = result["bytes_up"] + result["bytes_down"]
    result["calls"] = sum(s["connections_allowed"] for s in summaries)
    result["calls_denied"] = sum(s["connections_denied"] for s in summaries)
    result["cap_exceeded"] = any(bool(j["cap_exceeded"]) for j in journals)
    return result


def _stage_outcome(
    stage: dict[str, Any], attempts: list[dict[str, Any]], finished: bool
) -> tuple[str, str]:
    """(short state, plain outcome sentence fragment)."""
    status = stage["status"]
    last = attempts[-1] if attempts else {}
    if status == "succeeded":
        return "succeeded", "succeeded"
    if status == "failed":
        return "failed", "failed because " + _failure_words(
            last.get("failure_class"), last.get("exit_code")
        )
    if status == "refused_budget":
        return "not_started", "was not started because the remaining budget was too small"
    if status == "blocked_unknown":
        return "blocked", "is blocked because the platform cannot confirm what happened to it"
    if status == "cancelled":
        return "stopped", "was stopped"
    if status == "skipped":
        return "not_started", "was not run because an earlier stage did not succeed"
    if last.get("not_started"):
        return "not_started", "was not started"
    if status == "pending":
        return "not_started", "was not started" if finished else "has not started yet"
    return "in_progress", (
        "was still in progress when the campaign ended" if finished else "is still in progress"
    )


def _stage_report(
    stage: dict[str, Any],
    attempt_rows: dict[int, sqlite3.Row],
    finished: bool,
    detail: bool,
) -> dict[str, Any]:
    attempts = stage["attempts"]
    state, outcome = _stage_outcome(stage, attempts, finished)
    notes: list[str] = []
    reasons = []
    for attempt in attempts[:-1]:
        if attempt.get("failure_class"):
            words = _failure_words(attempt["failure_class"], attempt.get("exit_code"))
            if words not in reasons:
                reasons.append(words)
    retried = None
    if reasons:
        retried = "an earlier attempt failed: " + "; ".join(reasons)
    spans = [
        _seconds_between(
            attempt_rows[a["attempt"]]["created_at"], attempt_rows[a["attempt"]]["updated_at"]
        )
        for a in attempts
        if a["attempt"] in attempt_rows
    ]
    duration = None if not spans or None in spans else sum(spans)
    cost: int | None
    if not attempts:
        cost = 0
    elif any(a["settled_cents"] is None for a in attempts):
        cost = None
        notes.append("cost not yet settled")
    else:
        cost = sum(a["settled_cents"] for a in attempts)
        if any(a.get("settlement_final") is False for a in attempts):
            notes.append("the cost shown is provisional; final settlement is still pending")
    if attempts and any(
        a.get("run_receipt_digest") is None for a in attempts if a["exit_code"] is not None
    ):
        notes.append("the worker has not sent a run receipt for every attempt")
    for attempt in attempts:
        if attempt.get("exit_resolved"):
            if attempt["exit_resolved"].get("closed_reason"):
                notes.append(
                    "the attempt was closed after its container was seen to exit with no run "
                    "report; nothing was measured"
                )
            else:
                notes.append(
                    "the result was resolved from the worker's confirmed exit, not measured"
                )
            break
    egress = _egress(attempts)
    last_provider = attempts[-1].get("provider_id") if attempts else stage.get("provider_id")
    report: dict[str, Any] = {
        "name": stage["stage_key"],
        "state": state,
        "what_ran": _what_ran(
            attempts[-1].get("runtime") if attempts else stage.get("runtime"), last_provider
        ),
        "where": _where(attempts, stage),
        "attempts": len(attempts),
        "retried_because": retried,
        "outcome": outcome,
        "duration_seconds": duration,
        "cost_cents": cost,
        "data_in": _io_items(attempts, "input"),
        "data_out": _io_items(attempts, "output"),
        "egress": egress,
        "notes": notes,
    }
    if detail:
        report["ids"] = {
            "attempts": [
                {
                    "attempt": a["attempt"],
                    "attempt_id": attempt_rows[a["attempt"]]["id"]
                    if a["attempt"] in attempt_rows
                    else None,
                    "grant_id": a["grant_id"],
                    "operation_id": a["operation_id"],
                    "worker_principal_id": a["worker_principal_id"],
                    "evidence_digest": a["evidence_digest"],
                    "manifest_id": a["manifest_id"],
                    "manifest_digest": a["manifest_digest"],
                    "failure_detail": attempt_rows[a["attempt"]]["failure_detail"]
                    if a["attempt"] in attempt_rows
                    else None,
                }
                for a in attempts
            ]
        }
    return report


# -- model spend ---------------------------------------------------------------------------

_SPEND_NOTE = "Model spend is project-wide for this period; it is not attributed to this campaign."


def _model_spend(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    start: str,
    end: str,
    *,
    end_inclusive: bool = True,
) -> dict[str, Any]:
    """Ledger rows of this organization and project created from ``start`` to ``end`` (``end``
    excluded unless ``end_inclusive``). Only settled rows count as spend; open reservations
    are counted apart."""
    try:
        rows = connection.execute(
            """SELECT model, status, settled_micro FROM model_spend_calls
            WHERE org_id=? AND project_id=? AND created_at >= ?
            AND created_at <= ? AND (? OR created_at < ?)""",
            (org_id, project_id, start, end, end_inclusive, end),
        ).fetchall()
    except sqlite3.OperationalError:
        return {
            "calls": None,
            "settled_calls": None,
            "open_calls": None,
            "released_calls": None,
            "settled_micro_cents": None,
            "settled_dollars": None,
            "by_model": {},
            "note": "The model-spend ledger is not available. " + _SPEND_NOTE,
        }
    by_model: dict[str, int] = {}
    for row in rows:
        if row["status"] == "settled":
            by_model[row["model"]] = by_model.get(row["model"], 0) + (row["settled_micro"] or 0)
    total = sum(by_model.values())
    return {
        "calls": len(rows),
        "settled_calls": sum(1 for r in rows if r["status"] == "settled"),
        "open_calls": sum(1 for r in rows if r["status"] == "reserved"),
        "released_calls": sum(1 for r in rows if r["status"] == "released"),
        "settled_micro_cents": total,
        "settled_dollars": _dollars_from_micro(total),
        "by_model": {m: _dollars_from_micro(v) for m, v in sorted(by_model.items())},
        "note": _SPEND_NOTE,
    }


# -- campaign report -----------------------------------------------------------------------


def _why(status: str, reason: str | None, stages: list[dict[str, Any]]) -> str | None:
    if status == "succeeded":
        return None
    failed = next((s for s in stages if s["state"] == "failed"), None)
    reason = reason or ""
    if reason.startswith("cancel:"):
        given = reason[len("cancel:") :].strip()
        return "it was cancelled" + (f" (reason given: {given})" if given else "")
    if reason.startswith("stop_rule:"):
        stage, _, metric = reason[len("stop_rule:") :].partition(".")
        return f"a stop rule on {_plain(metric)} was met after stage {stage}"
    if reason.startswith("budget_refused:"):
        return f"the remaining budget was too small to start stage {reason.split(':', 1)[1]}"
    if reason.startswith("budget_overrun"):
        return "spending went over the campaign cap"
    if reason == "stage_cancelled":
        return "a stage was cancelled by an operator"
    if reason.startswith("stage_failed:") and failed is not None:
        return f"stage {failed['name']} {failed['outcome']}"
    if failed is not None:
        return f"stage {failed['name']} {failed['outcome']}"
    if reason:
        return _plain(reason)
    return None


def _waiting_words(hold: dict[str, Any]) -> str:
    return (
        f"approved and waiting to start at {hold['not_before']} "
        f"(about {_duration_text(hold['seconds_remaining'])} from now)"
    )


def _headline(report: dict[str, Any]) -> str:
    campaign = report["campaign"]
    if campaign.get("waiting"):
        return f"The campaign is {_waiting_words(campaign['waiting'])}; nothing has run yet."
    stages = report["stages"]
    done = sum(1 for s in stages if s["state"] == "succeeded")
    status = campaign["status"]
    cost = campaign["cost"]
    spend = f"cost {cost['spent_dollars']} of a {cost['cap_dollars']} cap"
    took = _duration_text(campaign["duration_seconds"])
    tail = f", {spend}" + (f", took {took}" if took else "") + "."
    counts = f"{done} of {len(stages)} stages succeeded"
    if status == "succeeded":
        return f"The campaign finished: {counts}{tail}"
    verb = _STATUS_WORDS.get(status, f"is {_plain(status)}")
    why = campaign["why"]
    failed = next((s for s in stages if s["state"] == "failed"), None)
    if status == "failed" and failed is not None:
        cause = why_part(failed)
        return f"The campaign failed at stage {failed['name']} because {cause}; {counts}{tail}"
    if status in {"stopped", "cancelled"}:
        return f"The campaign {verb}" + (f" because {why}" if why else "") + f"; {counts}{tail}"
    return f"The campaign {verb}: {counts} so far{tail}"


def why_part(stage: dict[str, Any]) -> str:
    return stage["outcome"].removeprefix("failed because ")


def _campaign_report(
    connection: sqlite3.Connection, campaign: sqlite3.Row, detail: bool
) -> dict[str, Any]:
    receipt, receipt_digest = _receipt_for(connection, campaign)
    status = campaign["status"]
    finished = status in _TERMINAL
    attempt_rows: dict[str, dict[int, sqlite3.Row]] = {}
    for row in connection.execute(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? ORDER BY stage_key, attempt",
        (campaign["id"],),
    ):
        attempt_rows.setdefault(row["stage_key"], {})[row["attempt"]] = row
    stages = [
        _stage_report(s, attempt_rows.get(s["stage_key"], {}), finished, detail)
        for s in receipt["stages"]
    ]
    starts = [r["created_at"] for rows in attempt_rows.values() for r in rows.values()]
    started_at = min(starts) if starts else None
    finished_at = campaign["updated_at"] if finished else None
    budget = receipt["budget"]
    cap, spent, held = budget["cap"], budget["settled"], budget["held"]
    hold = campaign_hold.waiting(campaign["spec_json"], status, datetime.now(UTC))
    campaign_info = {
        "name": _spec_name(campaign["spec_json"]),
        "status": status,
        "outcome": _STATUS_WORDS.get(status, _plain(status)),
        "why": _why(status, campaign["stop_reason"], stages),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _seconds_between(started_at, finished_at),
        "cost": {
            "cap_cents": cap,
            "spent_cents": spent,
            "held_cents": held,
            "cap_dollars": _dollars_from_cents(cap),
            "spent_dollars": _dollars_from_cents(spent),
        },
    }
    if hold is not None:
        campaign_info["waiting"] = hold
        campaign_info["why"] = "its start time (not_before) has not come yet"
    egress_hosts = sorted({h for s in stages if s["egress"] for h in s["egress"]["hosts"] or []})
    egress_bytes = [s["egress"]["bytes"] for s in stages if s["egress"]]
    durations = [s["duration_seconds"] for s in stages]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "scope": "campaign",
        "headline": "",
        "campaign": campaign_info,
        "stages": stages,
        "model_spend": _model_spend(
            connection,
            campaign["org_id"],
            campaign["project_id"],
            campaign["created_at"],
            campaign["updated_at"] if finished else _now(),
        ),
        "totals": {
            "stages": len(stages),
            "stages_succeeded": sum(1 for s in stages if s["state"] == "succeeded"),
            "stages_failed": sum(1 for s in stages if s["state"] == "failed"),
            "stages_not_run": sum(1 for s in stages if s["state"] == "not_started"),
            "attempts": sum(s["attempts"] for s in stages),
            "cost_cents": spent,
            "stage_duration_seconds": None if None in durations else sum(durations),
            "egress_hosts": egress_hosts,
            "egress_bytes": None if None in egress_bytes else sum(egress_bytes),
        },
    }
    report["headline"] = _headline(report)
    if detail:
        report["ids"] = {
            "campaign_id": campaign["id"],
            "project_id": campaign["project_id"],
            "receipt_digest": receipt_digest,
        }
    return report


def build_report(connection: sqlite3.Connection, campaign_id: str, *, detail: bool = False) -> dict:
    connection.row_factory = sqlite3.Row
    campaign = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None:
        raise LookupError("Campaign was not found.")
    return _campaign_report(connection, campaign, detail)


def window_report(
    connection: sqlite3.Connection,
    project_id: str,
    since: str,
    until: str,
    *,
    detail: bool = False,
    limit: int = 100,
    reader: Mapping[str, Any] | None = None,
) -> dict:
    """``reader`` (an agent, audit O-05) limits the window, its count and totals to the
    campaigns that reader may read; ``None`` is the human, project-wide view."""
    connection.row_factory = sqlite3.Row
    project = connection.execute("SELECT org_id FROM projects WHERE id=?", (project_id,)).fetchone()
    where = "project_id=? AND created_at >= ? AND created_at < ?"
    args: tuple[Any, ...] = (project_id, since, until)
    if reader is not None:
        clause, own_args = own_clause(reader, "campaigns.id")
        where += clause
        args = (*args, *own_args)
    total = connection.execute(f"SELECT COUNT(*) FROM campaigns WHERE {where}", args).fetchone()[0]
    rows = connection.execute(
        f"SELECT * FROM campaigns WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        (*args, max(0, limit)),
    ).fetchall()
    reports = [_campaign_report(connection, row, detail) for row in rows]
    entries = []
    for row, report in zip(rows, reports, strict=True):
        info = report["campaign"]
        entry = {
            "name": info["name"],
            "status": info["status"],
            "headline": report["headline"],
            "started_at": info["started_at"],
            "duration_seconds": info["duration_seconds"],
            "cost_cents": info["cost"]["spent_cents"],
            "stages": report["totals"]["stages"],
        }
        if detail:
            entry["ids"] = {"campaign_id": row["id"]}
        entries.append(entry)
    durations = [r["campaign"]["duration_seconds"] for r in reports]
    known = [d for d in durations if d is not None]
    counts = {
        key: sum(1 for r in reports if r["campaign"]["status"] == key)
        for key in ("succeeded", "failed", "stopped", "cancelled")
    }
    totals = {
        "campaigns": len(reports),
        "campaigns_matching": total,
        "truncated": total > len(reports),
        "campaigns_succeeded": counts["succeeded"],
        "campaigns_failed": counts["failed"],
        "campaigns_stopped": counts["stopped"],
        "campaigns_cancelled": counts["cancelled"],
        "stages": sum(r["totals"]["stages"] for r in reports),
        "stages_succeeded": sum(r["totals"]["stages_succeeded"] for r in reports),
        "stages_failed": sum(r["totals"]["stages_failed"] for r in reports),
        "cost_cents": sum(r["totals"]["cost_cents"] for r in reports),
        "duration_seconds": sum(known),
        "campaigns_without_duration": len(durations) - len(known),
        "hours": round(sum(known) / 3600, 2),
        "egress_hosts": sorted({h for r in reports for h in r["totals"]["egress_hosts"]}),
    }
    spend = (
        None
        if project is None
        else _model_spend(
            connection, project["org_id"], project_id, since, until, end_inclusive=False
        )
    )
    report = {
        "schema": REPORT_SCHEMA,
        "scope": "window",
        "headline": (
            f"{totals['campaigns']} campaigns in this period: {counts['succeeded']} finished, "
            f"{counts['failed']} failed, {counts['stopped'] + counts['cancelled']} stopped or "
            f"cancelled, cost {_dollars_from_cents(totals['cost_cents'])}."
        ),
        "window": {"since": since, "until": until},
        "campaigns": entries,
        "totals": totals,
        "model_spend": spend,
    }
    return report


# -- text ----------------------------------------------------------------------------------


def _egress_sentence(egress: dict[str, Any] | None) -> str:
    if egress is None:
        return "It had no internet access."
    if egress["hosts"] is None:
        allowed = ", ".join(egress["allowed_hosts"]) or "none"
        return (
            "It was allowed internet access (allowed hosts: "
            f"{allowed}), but the worker has not reported its network use."
        )
    parts = []
    for item in egress["destinations"]:
        if item["decision"] == "allow" and item["connections"]:
            parts.append(
                f"{item['host']} ({_size_text(item['bytes_up'])} sent, "
                f"{_size_text(item['bytes_down'])} received, {item['connections']} connections)"
            )
    text = "It contacted " + ", ".join(parts) if parts else "It made no internet connections"
    text += f"; {_size_text(egress['bytes'])} transferred in total."
    if egress["calls_denied"]:
        text += (
            f" {egress['calls_denied']} connection attempts were blocked"
            f" ({', '.join(egress['denied_hosts'])})."
        )
    if egress["cap_exceeded"]:
        text += " It went over its data transfer cap."
    if not egress["reported"]:
        text += " Some attempts have not reported their network use yet."
    return text


def _data_sentence(label: str, items: list[dict[str, Any]]) -> str | None:
    if not items:
        return None
    parts = [
        f"{i['name']} ({i['size_text'] or 'size not recorded'}, {_plain(i['data_class'])})"
        for i in items
    ]
    return f"{label}: " + ", ".join(parts) + "."


def _stage_text(number: int, stage: dict[str, Any]) -> str:
    verb = "ran as" if stage["attempts"] else "was set up to run as"
    lines = [f"{number}. Stage {stage['name']} {verb} {stage['what_ran']}"]
    if stage["where"]:
        lines[0] += f", on {stage['where']}"
    lines[0] += "." if stage["attempts"] else " (it never ran)."
    lines.append(f"It {stage['outcome']}.")
    if stage["attempts"] > 1:
        lines.append(f"It took {stage['attempts']} attempts ({stage['retried_because']}).")
    if stage["duration_seconds"] is not None:
        lines.append(f"It took about {_duration_text(stage['duration_seconds'])} (approximate).")
    elif stage["attempts"]:
        lines.append("Its duration is not recorded.")
    if stage["cost_cents"] is None:
        lines.append("Cost not yet settled.")
    else:
        lines.append(f"Cost {_dollars_from_cents(stage['cost_cents'])}.")
    for sentence in (
        _data_sentence("Data in", stage["data_in"]),
        _data_sentence("Data out", stage["data_out"]),
    ):
        if sentence:
            lines.append(sentence)
    lines.append(_egress_sentence(stage["egress"]))
    for note in stage["notes"]:
        if note != "cost not yet settled":
            lines.append(note[0].upper() + note[1:] + ".")
    return " ".join(lines)


def _spend_text(spend: dict[str, Any] | None) -> str:
    if spend is None:
        return "Model spend is not available for this period."
    if spend["calls"] is None:
        return spend["note"]
    if not spend["calls"]:
        return "No model calls were recorded for the project in this period. " + spend["note"]
    models = ", ".join(f"{m} {d}" for m, d in spend["by_model"].items())
    text = (
        f"Model calls by the project in this period: {spend['settled_calls']} settled, "
        f"costing {spend['settled_dollars']}" + (f" ({models})" if models else "") + "."
    )
    if spend["open_calls"]:
        text += f" {spend['open_calls']} more are reserved and not yet settled."
    if spend["released_calls"]:
        text += f" {spend['released_calls']} reservations were released without cost."
    return text + " " + spend["note"]


def _ids_text(ids: dict[str, Any]) -> list[str]:
    return [f"  {key}: {value}" for key, value in ids.items() if value is not None]


def render_text(report: dict) -> str:
    if report["scope"] == "window":
        window = report["window"]
        totals = report["totals"]
        lines = [report["headline"], ""]
        lines.append(f"Period: {window['since']} up to {window['until']}.")
        if totals["truncated"]:
            lines.append(
                f"Only the newest {totals['campaigns']} of {totals['campaigns_matching']} "
                "campaigns are listed and totalled."
            )
        for entry in report["campaigns"]:
            lines.append(f"- {entry['name'] or 'Unnamed campaign'}: {entry['headline']}")
        lines.append("")
        lines.append(
            f"Together they ran {totals['stages']} stages ({totals['stages_succeeded']} "
            f"succeeded, {totals['stages_failed']} failed) for about {totals['hours']} hours "
            f"(approximate) and cost {_dollars_from_cents(totals['cost_cents'])}."
        )
        hosts = totals["egress_hosts"]
        lines.append(
            "Internet hosts contacted: " + ", ".join(hosts) + "."
            if hosts
            else "No internet hosts were reported as contacted."
        )
        lines.append(_spend_text(report["model_spend"]))
        for entry in report["campaigns"]:
            if "ids" in entry:
                lines += ["", "Identifiers:", *_ids_text(entry["ids"])]
        return "\n".join(lines) + "\n"
    campaign = report["campaign"]
    lines = [report["headline"], ""]
    if campaign["name"]:
        lines.append(f"Campaign: {campaign['name']}.")
    if campaign["why"] and campaign["status"] != "succeeded":
        lines.append(f"Why: {campaign['why']}.")
    when = []
    if campaign["started_at"]:
        when.append(f"Started {campaign['started_at']}")
    if campaign["finished_at"]:
        when.append(f"finished {campaign['finished_at']}")
    if when:
        lines.append(", ".join(when) + ".")
    cost = campaign["cost"]
    money = f"Spent {cost['spent_dollars']} of the {cost['cap_dollars']} cap."
    if cost["held_cents"]:
        money += f" {_dollars_from_cents(cost['held_cents'])} is still held for unfinished work."
    lines += [money, "", "Stage by stage:"]
    lines += [_stage_text(i, s) for i, s in enumerate(report["stages"], 1)]
    lines += ["", _spend_text(report["model_spend"])]
    if "ids" in report:
        lines += ["", "Identifiers:", *_ids_text(report["ids"])]
        for stage in report["stages"]:
            for attempt in stage.get("ids", {}).get("attempts", []):
                lines.append(f"  {stage['name']} attempt {attempt['attempt']}:")
                lines += ["  " + t for t in _ids_text(attempt)]
    return "\n".join(lines) + "\n"
