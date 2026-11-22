"""Heavy-stage placement guard (ADR 0107).

A heavy stage (ADR 0088's signals, plus a ``pretraining`` tag, large pinned inputs and a large
declared output limit) that is placed on a local candidate while the ``salad`` provider has a
campaign profile for the project is a fallback the operator should see: the placement carries a
``heavy_guard`` warning with the signals that made the stage heavy and the reasons the remote
capacity was not used, and one ``campaign.heavy_stage_local_fallback`` event is recorded in the
same transaction as the placement. With ``heavy_local_fallback: refuse`` in the operator's
``placement_preferences`` the local candidate is skipped instead, with the transient reason
``heavy_local_refused``, so the stage waits until a remote candidate qualifies.

The guard never adds a destination, changes an order, skips another check or touches a spec. A
stage that is not heavy, or a project with no registered profile, is placed exactly as before.
It reads metadata only: the stage's declarations, recorded input sizes and the profile list.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import campaign_placement_preference as preference
from .campaign_models import NormalStage

EVENT = "campaign.heavy_stage_local_fallback"
REFUSED_REASON = "heavy_local_refused"
REMOTE_PROVIDER = "salad"
# Extra heavy tag beyond ADR 0088's ``heavy`` and ``training`` (the class is not changed by it).
EXTRA_HEAVY_TAGS = frozenset({"pretraining"})
# Remote skip reasons that mean the remote side could not be reached or authorized right now.
CREDENTIAL_REASONS = frozenset({"credential_expired", "credential_unusable", "safety_service_down"})
PROFILE_REASONS = frozenset({"not_enrolled", "provider_invalid"})
STEERING_REASONS = frozenset({"excluded_by_steering", "not_pinned"})
# Reason codes, in the order they are reported.
REASON_ORDER = (
    "credential_unavailable",
    "no_fitting_profile",
    "remote_skipped",
    "operator_override",
    "not_listed",
    "remote_not_tried",
)


# -- definitions -----------------------------------------------------------------------------


def input_bytes(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage: NormalStage
) -> int:
    """The summed byte size of the stage's pinned inputs the store records (same org and
    project only): a snapshot's manifest sizes and a pinned artifact's recorded output size.
    Stage-to-stage inputs and inputs whose size is unknown are ignored."""
    total = 0
    for item in stage.inputs:
        try:
            if item.kind == "snapshot" and item.snapshot_id:
                row = connection.execute(
                    "SELECT manifest_json FROM source_snapshots WHERE id=? AND project_id=?",
                    (item.snapshot_id, campaign["project_id"]),
                ).fetchone()
                if row is not None:
                    entries = json.loads(row["manifest_json"]).get("entries") or ()
                    total += sum(int(entry.get("size") or 0) for entry in entries)
            elif item.kind == "artifact" and item.campaign_id:
                row = connection.execute(
                    """SELECT size_bytes FROM campaign_stage_io WHERE org_id=? AND project_id=?
                    AND campaign_id=? AND stage_key=? AND direction='output' AND name=?
                    AND sha256=? LIMIT 1""",
                    (
                        campaign["org_id"],
                        campaign["project_id"],
                        item.campaign_id,
                        item.stage,
                        item.output,
                        item.sha256,
                    ),
                ).fetchone()
                if row is not None:
                    total += int(row["size_bytes"])
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            continue  # an unreadable size is unknown, never a placement failure
    return total


def heavy(
    stage: NormalStage,
    connection: sqlite3.Connection | None = None,
    campaign: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Whether the guard treats the stage as heavy, and the signals that decided it.

    ADR 0088's heavy signals (``declared_memory``, ``long_expected_runtime``, ``tag:heavy``,
    ``tag:training``), plus ``tag:pretraining``, ``large_inputs`` (pinned inputs the store
    records summing to at least the heavy threshold, read only with a connection) and
    ``large_outputs`` (declared output ``max_bytes`` summing to at least it). A stage that
    declares egress is never guarded: it cannot list rented capacity (ADR 0088)."""
    cls, class_signals = preference.stage_class(stage)
    if cls == "egress_bound":
        return False, []
    signals = [item for item in class_signals if not item.startswith("tag:gpu")]
    signals += [f"tag:{tag}" for tag in sorted(EXTRA_HEAVY_TAGS & set(stage.placement_tags))]
    threshold = preference.heavy_bytes_threshold()
    if (
        connection is not None
        and campaign is not None
        and input_bytes(connection, campaign, stage) >= threshold
    ):
        signals.append("large_inputs")
    if stage.max_output_bytes >= threshold:
        signals.append("large_outputs")
    return bool(signals), signals


def salad_registered(connection: sqlite3.Connection, org_id: str, project_id: str) -> bool:
    """The remote provider is registered and has a campaign profile for this org and project
    (the provider's own profile list; metadata only)."""
    from .campaign_providers import get_provider

    try:
        provider = get_provider(REMOTE_PROVIDER)
    except KeyError:
        return False
    profiles = getattr(getattr(provider, "profiles", None), "profiles", None) or ()
    if not profiles:
        return False
    project = connection.execute("SELECT org_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None or project["org_id"] != org_id:
        return False
    return any(
        getattr(item, "org_id", None) == org_id and getattr(item, "project_id", None) == project_id
        for item in profiles
    )


def mode() -> str:
    return preference.heavy_local_fallback()


@dataclass(frozen=True)
class Guard:
    """The guard's view of one stage for one placement: it applies (heavy, profile registered)."""

    signals: tuple[str, ...]
    mode: str
    kinds: Mapping[str, str]

    def is_local(self, provider_id: str) -> bool:
        return self.kinds.get(provider_id) == "local"

    def is_remote(self, provider_id: str) -> bool:
        return str(self.kinds.get(provider_id, "")).startswith("remote_")


def applies(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage: NormalStage
) -> Guard | None:
    """The guard for this stage, or None (not heavy, or no profile registered): then nothing
    about the placement changes."""
    if not salad_registered(connection, campaign["org_id"], campaign["project_id"]):
        return None
    is_heavy, signals = heavy(stage, connection, campaign)
    if not is_heavy:
        return None
    kinds = {pid: preference.candidate_kind(stage, pid) for pid in stage.candidate_ids}
    return Guard(tuple(signals), mode(), kinds)


def reasons(
    guard: Guard,
    base: NormalStage,
    local_id: str,
    skipped: Iterable[Any],
    steering: Mapping[str, Any] | None,
    order_record: Mapping[str, Any] | None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Why a heavy stage reached the local candidate ``local_id``: reason codes in
    ``REASON_ORDER`` and each remote candidate's skip (provider id and reason code)."""
    skipped = list(skipped)
    remote = [pid for pid in base.candidate_ids if guard.is_remote(pid)]
    remote_skips = [item for item in skipped if guard.is_remote(item.provider_id)]
    detail = [{"provider_id": s.provider_id, "reason": s.reason} for s in remote_skips]
    found: set[str] = set()
    for item in remote_skips:
        if item.reason in CREDENTIAL_REASONS:
            found.add("credential_unavailable")
        elif item.provider_id == REMOTE_PROVIDER and item.reason in PROFILE_REASONS:
            found.add("no_fitting_profile")
        elif item.reason in STEERING_REASONS:
            found.add("operator_override")
        else:
            found.add("remote_skipped")
    if steering and remote:
        pin = steering.get("pin")
        if (pin is not None and pin not in remote) or set(steering.get("exclude") or ()) & set(
            remote
        ):
            found.add("operator_override")
    if not remote:
        found.add("not_listed")
    else:
        tried = {item.provider_id for item in remote_skips}
        untried = [pid for pid in remote if pid not in tried]
        if untried:
            # A remote candidate came after the local one in the order used.
            operator_ordered = order_record is not None and (
                order_record.get("source") != "default_policy"
            )
            if operator_ordered or not preference.default_enabled():
                found.add("operator_override")
            elif "operator_override" not in found:
                found.add("remote_not_tried")
    return [code for code in REASON_ORDER if code in found], detail


def record(
    guard: Guard,
    codes: list[str],
    detail: list[dict[str, str]],
    *,
    refused: bool = False,
) -> dict[str, Any]:
    """The ``heavy_guard`` block of a placement (warning) or of a waiting record (refusal)."""
    value: dict[str, Any] = {
        "warning": not refused,
        "signals": list(guard.signals),
        "reasons": list(codes),
        "detail": list(detail),
        "mode": guard.mode,
    }
    if refused:
        value["refused"] = True
    return value


def event_payload(
    stage_key: str, attempt: int, provider_id: str, block: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``campaign.heavy_stage_local_fallback`` payload: ids and codes only."""
    return {
        "stage_key": stage_key,
        "attempt": attempt,
        "provider_id": provider_id,
        "mode": block["mode"],
        "signals": list(block["signals"]),
        "reasons": list(block["reasons"]),
        "detail": [dict(item) for item in block["detail"]],
    }


def recorded(connection: sqlite3.Connection, campaign: Mapping[str, Any]) -> dict:
    """Every recorded fallback warning of a campaign by (stage_key, attempt) (receipts)."""
    rows = connection.execute(
        """SELECT payload_json, created_at FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor""",
        (campaign["project_id"], EVENT, campaign["id"]),
    ).fetchall()
    out = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        out[(payload.get("stage_key"), payload.get("attempt"))] = {
            "warning": True,
            "signals": payload.get("signals") or [],
            "reasons": payload.get("reasons") or [],
            "detail": payload.get("detail") or [],
            "mode": payload.get("mode"),
        }
    return out
