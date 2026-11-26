"""Heavy-stage placement guard (ADR 0107).

A heavy stage (ADR 0088's signals, plus a ``pretraining`` tag, large pinned inputs and a large
declared output limit) that is placed on a local candidate while the ``salad`` provider has a
campaign profile for the project is a fallback the operator should see: the placement carries a
``heavy_guard`` warning with the signals that made the stage heavy and the reasons the remote
capacity was not used, and one ``campaign.heavy_stage_local_fallback`` event is recorded in the
same transaction as the placement. With ``heavy_local_fallback: refuse`` in the operator's
``placement_preferences`` the local candidate is skipped instead, with the transient reason
``heavy_local_refused``, so the stage waits until a remote candidate qualifies.

Amendment 2026-11-23b: a heavy stage reaches a local candidate only with an explicit fallback
reason (``EXPLAINED_REASONS``, or ``not_listed`` explained by the stage's ADR 0141
``heavy_remote_candidate`` record). Unexplained (``not_listed`` with no such record,
``remote_not_tried``), the local candidate is skipped with the permanent reason
``heavy_local_unexplained`` (``UNEXPLAINED_REASON``): an untried remote candidate is tried first,
and when none is left the stage fails with that code and one ``REFUSED_EVENT``. The operator's
``heavy_unexplained_local: warn`` restores the recorded warning.

Amendment 2026-11-23c: ``refusals`` rebuilds, from recorded events only, the refusal of a heavy
stage that has no attempt for it, for its receipt stage entry; a refused ADR 0141 candidate's
structured ``cause`` (``CAUSE_KEYS``) is copied into its ``detail`` entry.

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
from .campaign_heavy_remote import FIELD as RECORD_SOURCE
from .campaign_heavy_remote import NOT_ADDED_REASONS, records
from .campaign_infra_retry import is_infra_class
from .campaign_models import NormalStage

EVENT = "campaign.heavy_stage_local_fallback"
REFUSED_REASON = "heavy_local_refused"
# Amendment 2026-11-23b: a heavy stage's local candidate with no explicit fallback reason.
UNEXPLAINED_REASON = "heavy_local_unexplained"
REFUSED_EVENT = "campaign.heavy_stage_local_refused"
REMOTE_PROVIDER = "salad"
# Extra heavy tag beyond ADR 0088's ``heavy`` and ``training`` (the class is not changed by it).
EXTRA_HEAVY_TAGS = frozenset({"pretraining"})
# Remote skip reasons that mean the remote side could not be reached or authorized right now.
CREDENTIAL_REASONS = frozenset(
    {
        "credential_unavailable",  # placement's skip when the provider's own check_stage says so
        "credential_expired",
        "credential_unusable",
        "safety_service_down",
    }
)
PROFILE_REASONS = frozenset({"not_enrolled", "provider_invalid"})
STEERING_REASONS = frozenset({"excluded_by_steering", "not_pinned"})
# Reason codes, in the order they are reported.
REASON_ORDER = (
    "no_remote_profile",
    "credential_unavailable",
    "no_fitting_profile",
    "remote_failed_infra",
    "remote_skipped",
    "operator_override",
    "allocation_unstable",
    "remote_candidate_not_added",
    "not_listed",
    "remote_not_tried",
)
# Explicit fallback reasons: Salad unavailable (no profile, credential, fitting profile,
# infrastructure, a placement check skipping it: capacity, a cap, a trust or licence refusal),
# an operator's own placement decision (steering, a placement rule, the default policy switched
# off), the operator's allocation-instability rule (ADR 0148: it moved the unstable remote
# class behind this candidate for a stage that had not started anywhere), or the ADR 0141 record
# saying why no remote candidate was added.
EXPLAINED_REASONS = frozenset(
    {
        "no_remote_profile",
        "credential_unavailable",
        "no_fitting_profile",
        "remote_failed_infra",
        "remote_skipped",
        "operator_override",
        "allocation_unstable",
        "remote_candidate_not_added",
    }
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
    ``tag:training``), a gpu requirement (``tag:gpu*``: the stage asks for hardware a local
    provider rarely has), plus ``tag:pretraining``, ``large_inputs`` (pinned inputs the store
    records summing to at least the heavy threshold, read only with a connection),
    ``large_public_inputs`` (pinned public downloads, ADR 0097, whose ``max_bytes`` sum to at
    least it) and ``large_outputs`` (declared output ``max_bytes`` summing to at least it). A
    stage that declares free-form egress is never guarded: it cannot list rented capacity (ADR
    0088). Pinned public downloads are not egress: the provider fetches them by digest."""
    cls, class_signals = preference.stage_class(stage)
    if cls == "egress_bound":
        return False, []
    signals = list(class_signals)
    signals += [f"tag:{tag}" for tag in sorted(EXTRA_HEAVY_TAGS & set(stage.placement_tags))]
    threshold = preference.heavy_bytes_threshold()
    if (
        connection is not None
        and campaign is not None
        and input_bytes(connection, campaign, stage) >= threshold
    ):
        signals.append("large_inputs")
    if public_bytes(stage) >= threshold:
        signals.append("large_public_inputs")
    if stage.max_output_bytes >= threshold:
        signals.append("large_outputs")
    return bool(signals), signals


def public_bytes(stage: NormalStage) -> int:
    """The summed ``max_bytes`` of the stage's pinned ``public_artifact`` files (ADR 0097): what
    the executing provider downloads and verifies by digest. Pure; read from the spec only."""
    return sum(
        size
        for item in stage.inputs
        if item.kind == "public_artifact"
        for _name, _url, _sha, size in item.public_files
    )


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


def _record_explains(record: Mapping[str, Any]) -> bool:
    return record.get("added") is False and record.get("reason") in NOT_ADDED_REASONS


def explained(
    reasons: Iterable[str],
    detail: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
) -> bool:
    """True when at least one reason is an explicit fallback reason (``EXPLAINED_REASONS``).

    ``not_listed`` alone is explained only by the stage's ADR 0141 record saying why no remote
    candidate was added (a reason in ``NOT_ADDED_REASONS``). ``detail`` may be that record, the
    recorded ``heavy_guard`` block, or the block's ``detail`` list (an entry whose ``source`` is
    ``heavy_remote_candidate``); a placement skip entry never explains ``not_listed``."""
    codes = set(reasons)
    if codes & EXPLAINED_REASONS:
        return True
    if "not_listed" not in codes or detail is None:
        return False
    if isinstance(detail, Mapping):
        if "added" in detail:
            return _record_explains(detail)
        detail = detail.get("detail") or ()
    try:
        entries = list(detail)
    except TypeError:
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("source") == RECORD_SOURCE
        and item.get("reason") in NOT_ADDED_REASONS
        for item in entries
    )


def _stage_record(campaign: Mapping[str, Any], stage_key: str) -> dict[str, Any] | None:
    """The stage's ADR 0141 ``heavy_remote_candidate`` record in the stored (approved) spec."""
    try:
        spec_json = campaign["spec_json"]
    except (KeyError, IndexError, TypeError):
        return None
    if not spec_json:
        return None
    try:
        return records(spec_json).get(stage_key)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


@dataclass(frozen=True)
class Guard:
    """The guard's view of one stage for one placement: it applies (heavy, profile registered)."""

    signals: tuple[str, ...]
    mode: str
    kinds: Mapping[str, str]
    # False: no usable Salad profile for the project; the guard then only records (warn).
    registered: bool = True
    # Amendment 2026-11-23b: ``refuse`` (default) or ``warn`` for an unexplained local fallback.
    unexplained: str = "refuse"
    # The stage's ADR 0141 record, when submit wrote one.
    remote_record: Mapping[str, Any] | None = None

    def refuses_unexplained(self, codes: Iterable[str], detail: Iterable[Any]) -> bool:
        return self.unexplained == "refuse" and not explained(codes, list(detail))

    def is_local(self, provider_id: str) -> bool:
        return self.kinds.get(provider_id) == "local"

    def is_remote(self, provider_id: str) -> bool:
        return str(self.kinds.get(provider_id, "")).startswith("remote_")


def applies(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    stage: NormalStage,
    heavy_result: tuple[bool, list[str]] | None = None,
) -> Guard | None:
    """The guard for this stage, or None (not heavy): then nothing about the placement changes.

    Without a usable Salad profile the guard only records (``no_remote_profile``) and always
    in warn mode, even when the operator set ``refuse``: a stage never waits for capacity that
    cannot exist, but the placement still carries the record and the event. ``heavy_result`` is
    ``heavy(...)`` when the caller already computed it."""
    registered = salad_registered(connection, campaign["org_id"], campaign["project_id"])
    is_heavy, signals = heavy_result or heavy(stage, connection, campaign)
    if not is_heavy:
        return None
    kinds = {pid: preference.candidate_kind(stage, pid) for pid in stage.candidate_ids}
    return Guard(
        tuple(signals),
        mode() if registered else "warn",
        kinds,
        registered,
        preference.heavy_unexplained_local(),
        _stage_record(campaign, stage.key),
    )


def _skip_entry(skip: Any) -> dict[str, Any]:
    """A remote skip's detail entry: provider and reason code, plus its structured cause
    (CAUSE_KEYS) when placement recorded one for a licence-label or data-class refusal."""
    entry: dict[str, Any] = {"provider_id": skip.provider_id, "reason": skip.reason}
    cause = getattr(skip, "cause", None) or {}
    for key in CAUSE_KEYS:
        if cause.get(key) is not None:
            entry[key] = cause[key]
    return entry


def reasons(
    guard: Guard,
    base: NormalStage,
    local_id: str,
    skipped: Iterable[Any],
    steering: Mapping[str, Any] | None,
    order_record: Mapping[str, Any] | None,
    attempts: Iterable[Mapping[str, Any]] = (),
    *,
    instability: Mapping[str, Any] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Why a heavy stage reached the local candidate ``local_id``: reason codes in
    ``REASON_ORDER`` and each remote candidate's skip (provider id and reason code).
    ``attempts`` are the stage's earlier attempts: a remote candidate whose attempt failed on
    infrastructure is moved last by placement, which is ``remote_failed_infra`` (the provider
    id and the failure class in ``detail``), not an untried remote. ``instability`` is the
    placement's ADR 0148 record: when the operator's allocation-instability rule moved an
    unstable remote candidate behind ``local_id`` (outcome ``allocation_unstable_fallback`` with
    ``local_id`` eligible), that is the explicit reason ``allocation_unstable``, recorded with
    the remote candidate in ``detail``; the warning is recorded, never silent."""
    skipped = list(skipped)
    remote = [pid for pid in base.candidate_ids if guard.is_remote(pid)]
    remote_skips = [item for item in skipped if guard.is_remote(item.provider_id)]
    detail = [_skip_entry(s) for s in remote_skips]
    if not guard.registered:
        return ["no_remote_profile"], detail
    found: set[str] = set()
    infra_failed: dict[str, str] = {}
    for attempt in attempts:
        failure = str(attempt["failure_class"] or "")
        pid = attempt["provider_id"] or base.provider
        if is_infra_class(failure) and guard.is_remote(pid):
            infra_failed[pid] = failure
    for pid, failure in infra_failed.items():
        found.add("remote_failed_infra")
        detail.append({"provider_id": pid, "reason": failure})
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
        record = guard.remote_record
        if record is not None and record.get("added") is False:
            # ADR 0141: submit recorded why no remote candidate was added (codes only here),
            # with a refused candidate's structured cause when the record has one.
            entry = {
                "provider_id": str(record.get("provider") or REMOTE_PROVIDER),
                "reason": str(record.get("reason") or ""),
                "source": RECORD_SOURCE,
            }
            for key in CAUSE_KEYS:
                if record.get(key) is not None:
                    entry[key] = record[key]
            detail.append(entry)
        found.add(
            "remote_candidate_not_added" if record and _record_explains(record) else "not_listed"
        )
    else:
        tried = {item.provider_id for item in remote_skips} | set(infra_failed)
        untried = [pid for pid in remote if pid not in tried]
        moved = _instability_moved(instability, local_id, remote)
        if moved is not None:
            found.add("allocation_unstable")
            detail.append(moved)
            untried = [pid for pid in untried if pid != moved["provider_id"]]
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


def _instability_moved(
    instability: Mapping[str, Any] | None, local_id: str, remote: list[str]
) -> dict[str, str] | None:
    """The remote candidate an ADR 0148 fallback moved behind ``local_id``, or None."""
    from .campaign_allocation_instability import FALLBACK

    if not instability or instability.get("outcome") != FALLBACK:
        return None
    head = instability.get("from_provider")
    if head not in remote or local_id not in (instability.get("eligible") or ()):
        return None
    return {"provider_id": str(head), "reason": "allocation_unstable"}


def record(
    guard: Guard,
    codes: list[str],
    detail: list[dict[str, str]],
    *,
    refused: bool = False,
    code: str | None = None,
) -> dict[str, Any]:
    """The ``heavy_guard`` block of a placement (warning) or of a waiting record (refusal).

    An unexplained fallback (amendment 2026-11-23b) also carries ``unexplained_local``, the
    effective operator mode, and a refusal of it the ``code`` ``heavy_local_unexplained``;
    explained ones keep the earlier shape."""
    value: dict[str, Any] = {
        "warning": not refused,
        "signals": list(guard.signals),
        "reasons": list(codes),
        "detail": list(detail),
        "mode": guard.mode,
    }
    if refused:
        value["refused"] = True
    if code is not None:
        value["code"] = code
    if not explained(codes, detail):
        value["unexplained_local"] = guard.unexplained
    return value


def event_payload(
    stage_key: str, attempt: int, provider_id: str, block: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``campaign.heavy_stage_local_fallback`` (or ``REFUSED_EVENT``) payload: ids and
    codes only."""
    value = {
        "stage_key": stage_key,
        "attempt": attempt,
        "provider_id": provider_id,
        "mode": block["mode"],
        "signals": list(block["signals"]),
        "reasons": list(block["reasons"]),
        "detail": [dict(item) for item in block["detail"]],
    }
    for key in OPTIONAL_KEYS:
        if key in block:
            value[key] = block[key]
    return value


# Keys a block carries only for an unexplained fallback (absent keeps earlier payloads).
OPTIONAL_KEYS = ("code", "unexplained_local")
# ADR 0141 amendment 2026-11-23c: a refused candidate's structured cause, copied from the record
# into its detail entry only when the record has it (no free text, no path).
CAUSE_KEYS = ("cause", "labels", "data_class")
# Stage statuses in which a refuse-mode wait has ended (receipts).
ENDED_WAIT_STATUSES = frozenset({"failed", "cancelled"})


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
            **{key: payload[key] for key in OPTIONAL_KEYS if key in payload},
        }
    return out


def _refusal_block(
    payload: Mapping[str, Any], code: str, provider_id: Any, recorded_at: Any
) -> dict[str, Any]:
    block = {
        "warning": False,
        "refused": True,
        "code": code,
        "provider_id": provider_id,
        "attempt": payload.get("attempt"),
        "mode": payload.get("mode"),
        "signals": list(payload.get("signals") or ()),
        "reasons": list(payload.get("reasons") or ()),
        "detail": [dict(item) for item in payload.get("detail") or ()],
    }
    if "unexplained_local" in payload:
        block["unexplained_local"] = payload["unexplained_local"]
    block["recorded_at"] = recorded_at
    return block


def refusals(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    stages: Iterable[Mapping[str, Any]],
    attempted: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Each stage's heavy-guard refusal for its receipt entry (amendment 2026-11-23c), from
    recorded events only (no clock read), by stage key:

    - the stage's latest ``REFUSED_EVENT`` (code ``heavy_local_unexplained``: the stage failed
      without an attempt for that number);
    - otherwise, for a stage that has no attempt and has ended (``ENDED_WAIT_STATUSES``), its
      latest placement wait when that wait was a refuse-mode refusal (code
      ``heavy_local_refused``, from the recorded skip).

    A stage with neither is absent, so its receipt entry is unchanged."""
    from .campaign_placement import WAITING_EVENT

    attempted = set(attempted)
    out: dict[str, dict[str, Any]] = {}
    rows = connection.execute(
        """SELECT payload_json, created_at FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor""",
        (campaign["project_id"], REFUSED_EVENT, campaign["id"]),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        out[payload.get("stage_key")] = _refusal_block(
            payload,
            str(payload.get("code") or UNEXPLAINED_REASON),
            payload.get("provider_id"),
            row["created_at"],
        )
    ended = {
        stage["stage_key"]
        for stage in stages
        if stage["status"] in ENDED_WAIT_STATUSES
        and stage["stage_key"] not in attempted
        and stage["stage_key"] not in out
    }
    if not ended:
        return out
    waits: dict[str, tuple[dict[str, Any], Any]] = {}
    for row in connection.execute(
        """SELECT payload_json, created_at FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor""",
        (campaign["project_id"], WAITING_EVENT, campaign["id"]),
    ):
        payload = json.loads(row["payload_json"])
        if payload.get("stage_key") in ended:
            waits[payload["stage_key"]] = (payload, row["created_at"])
    for key, (payload, recorded_at) in waits.items():
        block = payload.get("heavy_guard")
        refused = next(
            (
                item
                for item in payload.get("skipped") or ()
                if isinstance(item, Mapping) and item.get("reason") == REFUSED_REASON
            ),
            None,
        )
        if not isinstance(block, Mapping) or not block.get("refused") or refused is None:
            continue  # the latest wait was not the guard's refusal
        out[key] = _refusal_block(
            {**block, "attempt": payload.get("attempt")},
            REFUSED_REASON,
            refused.get("provider_id"),
            recorded_at,
        )
    return out
