"""Immutable campaign receipt (ADR 0038 section 9), built from recorded rows only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .campaign_gates import unlock_receipt_section
from .campaign_heavy_guard import recorded as recorded_heavy_guards
from .campaign_holdout import receipt_section as holdout_receipt_section
from .campaign_io import stage_io
from .campaign_models import (
    normalize,
    parse_spec_json,
    public_fetch_host,
    stage_on_candidate,
    stage_provider,
)
from .campaign_placement import placements as recorded_placements
from .campaign_rerun import lineage_of
from .campaign_submissions import submission_receipt_section
from .code_approval import input_classes, snapshot_code_approval
from .stage_watch import watch_receipt_section
from .store import canonical_digest

# Version 2 (ADR 0040): per-attempt provider, runtime, trust and settlement fields, the
# evidence kind and digest, and the stage I/O index. Version 1 receipts stay as written.
RECEIPT_VERSION = 2


def code_summary(connection: sqlite3.Connection, spec_stage: Any) -> dict[str, Any] | None:
    """The stage's declared code input as recorded: its pinned snapshot, the digest of its files,
    whether a valid human code approval covers it, and the image the code runs in.

    ``None`` unless the stage declares ``code_input``, so every other receipt is unchanged. Only
    recorded values are read: an unreadable manifest gives a null ``code_digest`` and a zero file
    count, never a guess, and file names are never copied out. The same function builds the receipt
    section and the delivery event, so they cannot disagree."""
    name = getattr(spec_stage, "code_input", None)
    if not name:
        return None
    source = next((i for i in spec_stage.inputs if i.name == name and i.kind == "snapshot"), None)
    if source is None:
        return None
    files: list[dict[str, Any]] | None = None
    row = connection.execute(
        "SELECT manifest_json FROM source_snapshots WHERE id=?", (source.snapshot_id,)
    ).fetchone()
    if row is not None:
        try:
            entries = json.loads(row[0])["entries"]
            files = sorted(
                (
                    {"path": e["relative_path"], "sha256": e["sha256"], "size": e["size"]}
                    for e in entries
                ),
                key=lambda f: f["path"],
            )
        except (KeyError, TypeError, ValueError):
            files = None
    code_digest = None
    if files is not None:
        pairs = [[f["path"], f["sha256"]] for f in files]
        code_digest = hashlib.sha256(
            json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    approval = snapshot_code_approval(connection, source.snapshot_id) if row is not None else None
    image = spec_stage.image
    return {
        "input": name,
        "snapshot_id": source.snapshot_id,
        "snapshot_digest": source.digest,
        "code_digest": code_digest,
        # The count only: private file paths and per-file hashes never appear on a receipt;
        # ``code_digest`` binds the exact content.
        "files": len(files or []),
        "approved": approval is not None and approval.get("valid") is True,
        "image_digest": image.split("@", 1)[1] if "@" in image else None,
    }


def code_delivery(
    summary: dict[str, Any],
    *,
    provider_id: str | None,
    worker_principal_id: str | None,
    trust_policy_digest: str | None,
    admission_basis: str | None,
) -> dict[str, Any]:
    """The record of where a stage's code went: shared by the attempt's receipt and the event."""
    return {
        "provider_id": provider_id,
        "worker_principal_id": worker_principal_id,
        "code_digest": summary["code_digest"],
        "image_digest": summary["image_digest"],
        "trust_policy_digest": trust_policy_digest,
        "admission_basis": admission_basis,
    }


def _json(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


def _evidence(attempt: sqlite3.Row) -> tuple[str | None, str | None]:
    """Evidence kind and digest recorded on the attempt (never inferred)."""
    if attempt["run_receipt_digest"] is not None:
        return "run_receipt", attempt["run_receipt_digest"]
    if attempt["salad_evidence_digest"] is not None:
        return "salad_live_receipt", attempt["salad_evidence_digest"]
    return None, None


def _io_item(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "direction",
        "name",
        "kind",
        "media_type",
        "sha256",
        "size_bytes",
        "data_class",
        "snapshot_id",
        "manifest_id",
        "path",
        "lineage_node_id",
    )
    return {key: row[key] for key in keys}


def _lineage(connection: sqlite3.Connection, campaign_id: str) -> dict[str, list[str]]:
    nodes: set[str] = set()
    edges: set[str] = set()
    rows = connection.execute(
        """SELECT step, response_json FROM campaign_step_intents WHERE campaign_id=?
        AND status='done' AND step IN ('register_lineage_node','record_lineage_edge')
        ORDER BY created_at, id""",
        (campaign_id,),
    )
    for row in rows:
        response = json.loads(row["response_json"])
        if row["step"] == "register_lineage_node":
            nodes.add(response["id"])
        else:
            edges.add(response["id"])
            nodes.update((response["child_node_id"], response["parent_node_id"]))
    return {"node_ids": sorted(nodes), "edge_ids": sorted(edges)}


def _egress_entry(
    connection: sqlite3.Connection, attempt: sqlite3.Row, spec_stage: Any
) -> dict[str, Any] | None:
    """The approved egress policy and the worker's recorded journal for one attempt.

    Absent for a stage without egress, so existing receipts keep their shape. The policy is
    the human-approved plan (including any declared license/source per host); the journal is
    the append-only worker report, ``None`` until the worker has recorded it.
    """
    policy = getattr(spec_stage, "egress", None)
    if policy is None:
        return None
    from .egress_journal import egress_journal_for_operation

    recorded = (
        None
        if attempt["operation_id"] is None
        else egress_journal_for_operation(connection, attempt["operation_id"])
    )
    journal = (
        None
        if recorded is None
        else {
            "journal_sha256": recorded["journal_sha256"],
            "cap_exceeded": recorded["cap_exceeded"],
            "summary": recorded["summary"],
        }
    )
    return {
        "policy": policy.model_dump(mode="json"),
        "journal": journal,
    }


def _scratch(connection: sqlite3.Connection, attempt: sqlite3.Row, spec_stage: Any):
    """What was asked for and what the worker reports it ran with (ADR 0059).

    ``granted_bytes`` is the worker's own run-receipt figure, never inferred; it is absent
    until a run receipt records it."""
    requested = getattr(spec_stage, "scratch_bytes", None)
    granted = None
    if attempt["operation_id"] is not None:
        row = connection.execute(
            "SELECT receipt_payload_json FROM execution_run_receipts WHERE operation_id=?",
            (attempt["operation_id"],),
        ).fetchone()
        if row is not None:
            value = json.loads(row["receipt_payload_json"]).get("scratch_bytes")
            granted = value if type(value) is int else None
    if requested is None and granted is None:
        return None
    return {"requested_bytes": requested, "granted_bytes": granted}


def public_fetch_entry(
    provider_id: str | None, spec_stage: Any, fetched: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """The ``public_fetch`` receipt object of a stage with ``public_artifact`` inputs (ADR 0097).

    The pin (host, sha256) always comes from the approved spec. ``bytes`` and ``seconds`` come
    only from the provider's own reported ``fetched`` rows (``url``, ``sha256``, ``bytes``,
    ``seconds``) for the same url and digest, marked ``provider_reported``; with none they are
    null and the file is ``declared_only``. Only the host is printed, never the URL."""
    files: list[tuple[str, str, str]] = []
    hosts: set[str] = set()
    for source in getattr(spec_stage, "inputs", ()):
        if getattr(source, "kind", None) != "public_artifact":
            continue
        files += [(name, url, sha) for name, url, sha, _size in source.public_files]
        hosts |= set(source.redirect_hosts)
    if not files:
        return None
    reported = {
        (row.get("url"), row.get("sha256")): row for row in fetched or () if isinstance(row, dict)
    }
    entries = []
    for name, url, sha in files:
        host = public_fetch_host(url)
        hosts.add(host)
        row = reported.get((url, sha))
        size = None if row is None else row.get("bytes")
        seconds = None if row is None else row.get("seconds")
        if not isinstance(size, int) or isinstance(size, bool):
            row, size, seconds = None, None, None
        if seconds is not None and (
            isinstance(seconds, bool) or not isinstance(seconds, (int, float))
        ):
            seconds = None
        entries.append(
            {
                "name": name,
                "source_host": host,
                "sha256": sha,
                "bytes": size,
                "seconds": seconds,
                "evidence": "declared_only" if row is None else "provider_reported",
            }
        )
    return {
        "provider": provider_id,
        "placement": "provider_side",
        "hosts": sorted(hosts),
        "files": entries,
        "via_control": False,
    }


def _retained_fetched(connection: sqlite3.Connection, provider_id: str, attempt: Any):
    """The provider's retained ``fetched`` rows for the attempt, or None when the provider has
    no way to reach them from the database (the file bytes live in custody storage, not here)."""
    try:
        reader = getattr(stage_provider(provider_id), "retained_fetched", None)
    except KeyError:
        return None
    return None if reader is None else reader(connection, dict(attempt))


def _placed_runtime(spec_stage: Any, provider_id: str | None) -> str:
    """The runtime of the candidate the stage was last placed on (ADR 0060)."""
    try:
        return stage_on_candidate(spec_stage, provider_id or spec_stage.provider).runtime
    except KeyError:
        return spec_stage.runtime


# ADR 0095: the platform granted an attempt's worker access to the stage's pinned code input.
# Written in the grant's own transaction; records the grant and the code approval as it stood
# then, not that the worker fetched the input.
CODE_GRANTED_EVENT = "campaign.code_granted"


def _code_grant(connection: sqlite3.Connection, attempt: sqlite3.Row) -> dict[str, Any] | None:
    """The attempt's recorded code grant event, or ``None`` when none was written."""
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type=?
        AND json_extract(payload_json, '$.campaign_id') = ?
        AND json_extract(payload_json, '$.stage_key') = ?
        AND json_extract(payload_json, '$.attempt') = ? ORDER BY cursor LIMIT 1""",
        (CODE_GRANTED_EVENT, attempt["campaign_id"], attempt["stage_key"], attempt["attempt"]),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def _attempt_entry(
    connection: sqlite3.Connection,
    attempt: sqlite3.Row,
    stage: sqlite3.Row,
    spec_stage: Any,
    code: dict[str, Any],
    io_rows: list[dict[str, Any]],
    grants: dict[str, str],
    placed: dict[tuple[str, int], dict[str, Any]] | None = None,
    code_info: dict[str, Any] | None = None,
    guards: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    kind, digest = _evidence(attempt)
    provider_id = attempt["provider_id"] or stage["provider_id"] or spec_stage.provider
    if spec_stage.has_candidates:
        # ADR 0060: the attempt ran on one of the stage's approved candidates.
        try:
            spec_stage = stage_on_candidate(spec_stage, provider_id)
        except KeyError:
            pass
    entry = {
        "attempt": attempt["attempt"],
        "status": attempt["status"],
        "failure_class": attempt["failure_class"],
        "provider_id": provider_id,
        "runtime": spec_stage.runtime,
        "worker_principal_id": grants.get(attempt["grant_id"]),
        "trust_policy_digest": attempt["trust_policy_digest"],
        "admission_basis": attempt["admission_basis"],
        "input_data_class": stage["input_data_class"],
        **code,
        "grant_id": attempt["grant_id"],
        "admitted_attempt_id": attempt["admitted_attempt_id"],
        "operation_id": attempt["operation_id"],
        "run_receipt_digest": attempt["run_receipt_digest"],
        "exit_code": attempt["exit_code"],
        "evidence_kind": kind,
        "evidence_digest": digest,
        "manifest_id": attempt["manifest_id"],
        "manifest_digest": attempt["manifest_digest"],
        "hold_cents": attempt["hold_cents"],
        "settled_cents": attempt["settled_cents"],
        "settlement_basis": attempt["settlement_basis"],
        "settlement_final": attempt["settlement_basis"] != "guardian_bounded_hold",
        "measurements": _json(attempt["measurements_json"], []),
        "io": [_io_item(row) for row in io_rows if row["attempt_id"] == attempt["id"]],
    }
    egress = _egress_entry(connection, attempt, spec_stage)
    if egress is not None:
        entry["egress"] = egress
    if code_info is not None:
        # The code's transfer: where it went, under which trust setting. ``granted`` and the
        # approval come only from the recorded grant event, never from the grant id, the stage's
        # status or today's approval state; with no event they are false and unknown (null).
        granted = _code_grant(connection, attempt)
        entry["code_transfer"] = {
            **code_delivery(
                code_info,
                provider_id=provider_id,
                worker_principal_id=entry["worker_principal_id"],
                trust_policy_digest=attempt["trust_policy_digest"],
                admission_basis=attempt["admission_basis"],
            ),
            "granted": granted is not None,
            "approved": None if granted is None else granted.get("approved"),
            "approval_digest": None if granted is None else granted.get("approval_digest"),
        }
    public = public_fetch_entry(
        provider_id, spec_stage, _retained_fetched(connection, provider_id, attempt)
    )
    if public is not None:
        entry["public_fetch"] = public
    decision = (placed or {}).get((attempt["stage_key"], attempt["attempt"]))
    if decision is not None:
        # ADR 0060: the candidate chosen and why each other one was skipped.
        entry["placement"] = {
            key: decision.get(key)
            for key in (
                "candidates",
                "provider_id",
                "hold_cents",
                "skipped",
                "not_evaluated",
                "steering",
                "budget_refusal",
                "recorded_at",
            )
        }
        if decision.get("preference") is not None:
            # ADR 0070: the operator rule that ordered the approved candidates.
            entry["placement"]["preference"] = decision["preference"]
    # ADR 0107: a heavy stage that fell back to a local provider while a remote profile was
    # registered carries the warning (from its placement, or for a one-provider stage from the
    # recorded warning event).
    guard = (decision or {}).get("heavy_guard") or (guards or {}).get(
        (attempt["stage_key"], attempt["attempt"])
    )
    if guard is not None:
        entry.setdefault("placement", {})["heavy_guard"] = guard
    try:
        extra = stage_provider(provider_id).receipt_fields(connection, dict(attempt))
    except KeyError:
        extra = {}  # an unregistered provider adds nothing; the core fields stand
    scratch = _scratch(connection, attempt, spec_stage)
    if scratch is not None:
        entry["scratch"] = scratch
    if str(attempt["failure_class"] or "").startswith("infra_"):
        # ADR 0058: the attempt ended for an infrastructure reason and was retried (or, when
        # the bound was spent, the stage failed); the reason is the recorded detail.
        entry["infrastructure_retry"] = {"reason": attempt["failure_detail"]}
    if attempt["failure_class"] == "not_started":
        # ADR 0046: recorded as no evidence; it is never a pass or a fail.
        entry["not_started"] = {"reason": attempt["failure_detail"], "is_evidence": False}
    resolved = connection.execute(
        """SELECT payload_json FROM events WHERE event_type='campaign.attempt_exit_resolved'
        AND json_extract(payload_json, '$.attempt_id') = ? ORDER BY cursor DESC LIMIT 1""",
        (attempt["id"],),
    ).fetchone()
    if resolved is not None:
        # ADR 0053: resolved on the worker's confirmed exit, whatever the outcome class; it is
        # never a pass and never a measurement.
        seen = json.loads(resolved["payload_json"])
        entry["exit_resolved"] = {
            "reason": attempt["failure_detail"],
            "outcome": seen.get("outcome"),
            "evidence_digest": seen.get("evidence_digest"),
            "is_evidence": False,
        }
    return {**entry, **extra}


def _license_section(
    connection: sqlite3.Connection,
    campaign: Any,
    normal: Any,
    spec_stage: Any,
    stage_provider: str | None = None,
) -> dict[str, Any]:
    """The stage's effective licence labels (ADR 0113), only when they matter: some candidate
    provider restricts labels, or an input carries a label other than ``unlabelled``. Otherwise
    nothing is added, so existing receipts keep their bytes. Never raises."""
    try:
        from . import license_labels, provider_trust

        policy = provider_trust.current_policy(connection)
        restricted = any(
            policy.lists_license_labels(provider) for provider in spec_stage.candidate_ids
        )
        labels = license_labels.stage_labels(
            connection,
            normal,
            spec_stage.key,
            org_id=campaign["org_id"],
            project_id=campaign["project_id"],
        )
        provider = stage_provider or spec_stage.provider
        seen = connection.execute(
            """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
            AND json_extract(payload_json, '$.campaign_id') = ?
            AND json_extract(payload_json, '$.stage_key') = ?
            AND json_extract(payload_json, '$.provider') IS ?
            ORDER BY rowid DESC LIMIT 1""",
            (
                campaign["project_id"],
                "campaign.stage_license_checked",
                campaign["id"],
                spec_stage.key,
                provider,
            ),
        ).fetchone()
        if seen is not None:
            # As checked when the stage was dispatched (labels added later do not rewrite it).
            return {
                "license_labels": sorted(json.loads(seen[0])["labels"]),
                "license_labels_checked": "dispatch",
            }
        if restricted or labels != {license_labels.UNLABELLED}:
            return {"license_labels": sorted(labels), "license_labels_checked": "current"}
    except Exception:  # noqa: BLE001 - a receipt never fails because labels are unreadable
        return {}
    return {}


def steering_receipt_section(connection: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    """Pause, concurrency limit and stage cancels (ADR 0061); empty for an unsteered campaign,
    so existing receipts keep their shape and digest."""
    row = connection.execute(
        "SELECT * FROM campaign_steering WHERE campaign_id=?", (campaign_id,)
    ).fetchone()
    cancels = connection.execute(
        """SELECT stage_key, reason, requested_at, completed_at FROM campaign_stage_cancels
        WHERE campaign_id=? ORDER BY requested_at, stage_key""",
        (campaign_id,),
    ).fetchall()
    if row is None and not cancels:
        return {}
    return {
        "steering": {
            "paused_at_finish": bool(row and row["paused"]),
            "pause_count": 0 if row is None else row["pause_count"],
            "max_concurrent_stages": None if row is None else row["max_concurrent_stages"],
            "stage_cancels": [dict(item) for item in cancels],
        }
    }


def _smoke_grant(connection: sqlite3.Connection, principal_id: str) -> dict[str, str]:
    """``smoke_grant_id`` when the approval was made under a smoke credential grant."""
    from .smoke_credentials import grant_id_for_principal

    grant_id = grant_id_for_principal(connection, principal_id)
    return {} if grant_id is None else {"smoke_grant_id": grant_id}


def build_receipt(
    connection: sqlite3.Connection, campaign: sqlite3.Row | dict[str, Any], status: str
) -> tuple[dict[str, Any], str]:
    """Return ``(receipt, receipt_digest)`` for a campaign about to become ``status``."""
    campaign_id = campaign["id"]
    current = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    approval = connection.execute(
        """SELECT * FROM campaign_approvals WHERE campaign_id=?
        ORDER BY (status='active') DESC, approved_at DESC, id DESC LIMIT 1""",
        (campaign_id,),
    ).fetchone()
    stages = connection.execute(
        "SELECT * FROM campaign_stages WHERE campaign_id=? ORDER BY topo_index", (campaign_id,)
    ).fetchall()
    attempts = connection.execute(
        """SELECT * FROM campaign_stage_attempts WHERE campaign_id=?
        ORDER BY stage_key, attempt""",
        (campaign_id,),
    ).fetchall()
    normal = normalize(parse_spec_json(current["spec_json"]))
    io_rows = stage_io(connection, campaign_id)
    placed = {
        (item["stage_key"], item["attempt"]): item
        for item in recorded_placements(connection, current)
    }
    guards = recorded_heavy_guards(connection, current)
    grants = {
        row["id"]: row["worker_principal_id"]
        for row in connection.execute(
            "SELECT id, worker_principal_id FROM execution_grants WHERE id IN "
            "(SELECT grant_id FROM campaign_stage_attempts WHERE campaign_id=? "
            "AND grant_id IS NOT NULL)",
            (campaign_id,),
        )
    }
    stage_rows = []
    for stage in stages:
        code = (
            {"input_code": None, "input_code_files": []}
            if stage["input_snapshot_id"] is None
            else {
                key: value
                for key, value in input_classes(connection, stage["input_snapshot_id"]).items()
                if key in ("input_code", "input_code_files")
            }
        )
        spec_stage = normal.stage(stage["stage_key"])
        code_info = code_summary(connection, spec_stage)
        stage_rows.append(
            {
                "stage_key": stage["stage_key"],
                "provider_id": stage["provider_id"] or spec_stage.provider,
                "runtime": _placed_runtime(spec_stage, stage["provider_id"]),
                "status": stage["status"],
                "task_id": stage["task_id"],
                "experiment_id": stage["experiment_id"],
                "input": {
                    "snapshot_id": stage["input_snapshot_id"],
                    "digest": stage["input_snapshot_digest"],
                    "data_class": stage["input_data_class"],
                    **code,
                    **_license_section(
                        connection, current, normal, spec_stage, stage["provider_id"]
                    ),
                },
                **({} if code_info is None else {"code": code_info}),
                "output_data_class": stage["output_data_class"],
                "attempts": [
                    _attempt_entry(
                        connection,
                        attempt,
                        stage,
                        spec_stage,
                        code,
                        io_rows,
                        grants,
                        placed,
                        code_info,
                        guards,
                    )
                    for attempt in attempts
                    if attempt["stage_key"] == stage["stage_key"]
                ],
                "stop_results": _json(stage["stop_results_json"], []),
            }
        )
    reason = current["stop_reason"]
    unlock = unlock_receipt_section(connection, campaign_id)
    # ADR 0038: a stage bound to a program and gate leaves the scientific verdict to a human.
    bound = any(
        getattr(item, "salad", None) is not None
        for item in parse_spec_json(current["spec_json"]).stages
    )
    scientific = "pending_human_approval" if bound else "not_assessed"
    operational = "stop" if status in {"stopped", "failed", "cancelled"} else "continue"
    from .campaign_preapproval import preapproval_summary

    delegated = preapproval_summary(connection, campaign_id)
    rerun = lineage_of(connection, campaign_id, current["project_id"])
    return _finish(
        {
            "version": RECEIPT_VERSION,
            "campaign_id": campaign_id,
            "project_id": current["project_id"],
            "spec_digest": current["spec_digest"],
            "approval": None
            if approval is None
            else {
                "id": approval["id"],
                "approving_principal_id": approval["approving_principal_id"],
                "decision_class": approval["decision_class"],
                "automation_label": approval["automation_label"],
                "approved_at": approval["approved_at"],
                "expires_at": approval["expires_at"],
                **_smoke_grant(connection, approval["approving_principal_id"]),
            },
            "status": status,
            "stop_reason": reason,
            "budget": {
                "cap": current["budget_cap_cents"],
                "held": current["held_cents"],
                "settled": current["settled_cents"],
                "currency": "USD",
            },
            "stages": stage_rows,
            "lineage": _lineage(connection, campaign_id),
            # ADR 0080: inputs submit resolved from an earlier campaign's receipt; absent
            # when none, so existing receipts keep their shape.
            **_chained_inputs(current["spec_json"]),
            **({"unlock": unlock} if unlock["requirements"] else {}),
            **steering_receipt_section(connection, campaign_id),
            # ADR 0093: the approved submission this campaign was created from and its
            # decisions; absent otherwise, so existing receipts keep their shape.
            **submission_receipt_section(connection, campaign_id),
            # ADR 0068: the one-time holdout claims and their scored values; absent otherwise.
            **holdout_receipt_section(connection, campaign_id),
            # ADR 0087: unverified worker stage watch claims; absent when none was reported.
            **watch_receipt_section(connection, campaign_id),
            "verdict": {"operational": operational, "scientific_verdict": scientific},
            "evidence_class": "operational",
            # Reviews resolved by delegation, citing the approval (ADR 0044); absent when
            # none was recorded, so existing receipts keep their shape.
            **({"preapprovals": delegated} if delegated else {}),
            # The campaign this one re-runs, the stages it re-runs and the outputs it reuses by
            # digest (ADR 0069); absent for a campaign that is not a re-run.
            **({"rerun": rerun} if rerun else {}),
        }
    )


def _chained_inputs(spec_json: str) -> dict[str, Any]:
    """Receipt section for ``artifact`` inputs that carry ``resolved_from`` (ADR 0080)."""
    items = []
    for stage in json.loads(spec_json).get("stages", []):
        for source in stage.get("inputs", []):
            lineage = source.get("resolved_from")
            if source.get("kind") == "artifact" and lineage:
                items.append(
                    {
                        "stage_key": stage["key"],
                        "input": source["name"],
                        "campaign_id": source["campaign_id"],
                        "stage": source["stage"],
                        "output": source["output"],
                        "sha256": source["sha256"],
                        "predecessor": lineage.get("predecessor"),
                        "source_receipt_sha256": lineage["source_receipt_sha256"],
                    }
                )
    return {"chained_inputs": items} if items else {}


def _finish(receipt: dict[str, Any]) -> tuple[dict[str, Any], str]:
    return receipt, canonical_digest(receipt)
