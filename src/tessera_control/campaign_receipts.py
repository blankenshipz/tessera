"""Immutable campaign receipt (ADR 0038 section 9), built from recorded rows only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from . import campaign_allocation_instability, campaign_salad_fallback
from . import campaign_class_fallback as class_fallback
from .campaign_checkpoint import recorded_checkpoints
from .campaign_gates import unlock_receipt_section
from .campaign_heavy_guard import recorded as recorded_heavy_guards
from .campaign_heavy_guard import refusals as recorded_heavy_refusals
from .campaign_heavy_remote import records as heavy_remote_records
from .campaign_holdout import receipt_section as holdout_receipt_section
from .campaign_image_pull import SECTION as IMAGE_PULL_SECTION
from .campaign_image_pull import recorded as recorded_image_pulls
from .campaign_infra_retry import is_infra_class
from .campaign_io import stage_io
from .campaign_models import (
    PUBLIC_FETCH_UNREPORTED,
    normalize,
    parse_spec_json,
    public_fetch_host,
    stage_on_candidate,
    stage_provider,
)
from .campaign_placement import final_refusal
from .campaign_placement import placements as recorded_placements
from .campaign_rerun import lineage_of
from .campaign_seals import sealed_access
from .campaign_submissions import submission_receipt_section
from .code_approval import input_classes, snapshot_code_approval
from .delegation_receipts import delegation_receipt_section
from .image_provenance import IMAGE_PROVENANCE_EVENT
from .image_provenance import closed as image_provenance_closed
from .stage_egress_allowlist import refused_transfers, stage_allowlist
from .stage_watch import watch_receipt_section
from .store import _parse_utc, _utc_string, canonical_digest

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


CACHE_OUTCOMES = frozenset({"hit", "miss", "unavailable", "off"})
RECORDED_CACHE_OUTCOMES = frozenset({"hit", "miss", "unavailable"})


def cache_outcome(row: dict[str, Any]) -> str | None:
    """What the provider reported about its cache for one fetched file, from a closed set.

    A runtime reports ``cache`` (``hit``, ``miss``, ``unavailable`` when it has no usable
    persistent storage, ``off`` when the file was not flagged); a local worker reports a boolean
    ``cached`` (a recorded row may also carry the ``cache`` string, and ``unavailable`` then reads
    as itself rather than as unknown). Anything else is not trusted and reads as unknown
    (``None``)."""
    value = row.get("cache")
    if isinstance(value, str) and value in CACHE_OUTCOMES:
        return value
    cached = row.get("cached")
    if isinstance(cached, bool) and "cache" not in row:
        return "hit" if cached else "miss"
    return None


EGRESS_ALLOWLIST_STATEMENT = (
    "The hosts are the approved pinned sources and their redirect hosts. They are declared for "
    "review and the redirect set is enforced by the workload runtime; the provider's network "
    "does not restrict the stage to them."
)


def _rented_provider(provider_id: str | None) -> bool:
    """True for a provider that is not a local worker-grant provider (ADR 0097 amendment): its
    network egress is provider-wide (ADR 0036), so the allowlist is declared, not enforced."""
    if not provider_id:
        return False
    try:
        return stage_provider(provider_id).descriptor.kind != "worker_grant"
    except (KeyError, AttributeError):
        return False


def _refused(
    spec_stage: Any, rows: list[dict[str, Any]], allowlist: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    pins = {
        (url, sha)
        for source in getattr(spec_stage, "inputs", ())
        if getattr(source, "kind", None) == "public_artifact"
        for _name, url, sha, _size in source.public_files
    }
    allowed = stage_allowlist(spec_stage) if allowlist is None else allowlist
    return refused_transfers(allowed, pins, rows)


def transfer_violations(
    connection: sqlite3.Connection, attempt: Any, spec_stage: Any
) -> list[dict[str, Any]]:
    """Reported transfers of the attempt outside the stage's egress allowlist (ADR 0134).

    Provider-neutral: whatever rows the attempt's provider reports are checked; with none
    reported there is nothing to refuse (the receipt says ``declared_only``)."""
    provider_id = attempt["provider_id"] or spec_stage.provider
    rows = _retained_fetched(connection, provider_id, attempt, spec_stage)
    return _refused(spec_stage, list(rows or ()))


def public_fetch_entry(
    provider_id: str | None, spec_stage: Any, fetched: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """The ``public_fetch`` receipt object of a stage with ``public_artifact`` inputs, or of any
    stage whose provider reported a transfer outside its egress allowlist (ADR 0097, ADR 0134).

    The pin (host, sha256) always comes from the approved spec. ``bytes`` and ``seconds`` come
    only from the provider's own reported ``fetched`` rows (``url``, ``sha256``, ``bytes``,
    ``seconds``) for the same url and digest whose byte count is within the pin's ``max_bytes``,
    marked ``provider_reported``; with none they are null and the file is ``declared_only``. Only
    the host is printed, never the URL. For a provider that is not a local worker,
    ``egress_allowlist`` states that the hosts are declared and the redirect set enforced by the
    runtime, and that the provider's network is not restricted; its ``entries`` are the per-host
    ``{host, port, source}`` rows of the stage allowlist (ADR 0134). Every entry, local worker
    included, carries ``egress_verdict`` and, on a violation, ``refused_transfers``: each reported
    transfer checked against that allowlist."""
    files: list[tuple[str, str, str, int]] = []
    hosts: set[str] = set()
    cache_requested = False
    for source in getattr(spec_stage, "inputs", ()):
        if getattr(source, "kind", None) != "public_artifact":
            continue
        files += list(source.public_files)
        hosts |= set(source.redirect_hosts)
        cache_requested = cache_requested or bool(getattr(source, "cache", False))
    if not files and not _refused(spec_stage, list(fetched or ())):
        # No public_artifact input and no reported transfer: the receipt keeps its shape. A
        # reported transfer still gets an entry (ADR 0134 amendment): with no pins it is outside
        # the allowlist, so the violation that fails the attempt is on the receipt too.
        return None
    reported = {
        (row.get("url"), row.get("sha256")): row for row in fetched or () if isinstance(row, dict)
    }
    entries = []
    for name, url, sha, max_bytes in files:
        host = public_fetch_host(url)
        hosts.add(host)
        row = reported.get((url, sha))
        size = None if row is None else row.get("bytes")
        seconds = None if row is None else row.get("seconds")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= max_bytes:
            # A count the pin could not have produced contradicts it: not evidence of anything.
            row, size, seconds = None, None, None
        if seconds is not None and (
            isinstance(seconds, bool) or not isinstance(seconds, (int, float))
        ):
            seconds = None
        outcome = None if row is None else cache_outcome(row)
        item = {
            "name": name,
            "source_host": host,
            "sha256": sha,
            "bytes": size,
            "seconds": seconds,
            "evidence": "declared_only" if row is None else "provider_reported",
        }
        if cache_requested or outcome is not None:
            # Unknown is null when the spec asked for the cache; a file whose spec never asked
            # and whose provider said nothing keeps the receipt shape it always had.
            item["cache"] = outcome
        entries.append(item)
    allowlist = stage_allowlist(spec_stage)
    refused = _refused(spec_stage, list(fetched or ()), allowlist)
    entry = {
        "provider": provider_id,
        "placement": "provider_side",
        "hosts": sorted(hosts),
        "files": entries,
        "via_control": False,
        # ADR 0134: what the check of every reported transfer against the allowlist found.
        # The verdict agrees with the per-file evidence: a row the receipt could not use
        # (for example malformed bytes, shown above as declared_only) is not evidence.
        "egress_verdict": "violation"
        if refused
        else (
            "within_allowlist"
            if any(item["evidence"] == "provider_reported" for item in entries)
            else "declared_only"
        ),
    }
    if refused:
        entry["refused_transfers"] = refused
    if cache_requested:
        # The approved spec asked for the provider-side cache (ADR 0097): say so only then, so
        # the receipt of a stage that did not ask is unchanged.
        entry["cache_requested"] = True
    if _rented_provider(provider_id):
        entry["egress_allowlist"] = {
            "hosts": entry["hosts"],
            "declared": True,
            "redirect_enforced": True,
            "network_enforced": False,
            "statement": EGRESS_ALLOWLIST_STATEMENT,
            # ADR 0134: the per-host rows derived from the approved spec.
            "entries": allowlist,
        }
    return entry


def _local_fetched(
    connection: sqlite3.Connection, attempt: Any, spec_stage: Any
) -> list[dict[str, Any]] | None:
    """A local worker's recorded fetch rows (ADR 0097) as ``fetched`` rows keyed by the pin's
    url and digest, or None. Only rows that match a pinned file are used; the files themselves
    never reach the control plane, only these rows."""
    operation_id = attempt["operation_id"]
    if not operation_id:
        return None
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type = 'execution.public_fetch_recorded'
        AND json_extract(payload_json, '$.operation_id') = ? ORDER BY rowid LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        recorded = json.loads(row["payload_json"]).get("files")
    except ValueError:
        return None
    pins = {
        (source.name, name): (url, sha)
        for source in getattr(spec_stage, "inputs", ())
        if getattr(source, "kind", None) == "public_artifact"
        for name, url, sha, _size in source.public_files
    }
    rows = []
    for item in recorded if isinstance(recorded, list) else ():
        pin = pins.get((item.get("input"), item.get("name"))) if isinstance(item, dict) else None
        if pin is None or item.get("sha256") != pin[1]:
            continue
        reported = {
            "url": pin[0],
            "sha256": pin[1],
            "bytes": item.get("bytes"),
            "seconds": item.get("seconds"),
            "cached": item.get("cached"),
        }
        if "cache" in item:
            # Only the recorded tri-state is passed on; any other value reads as unknown.
            value = item["cache"]
            reported["cache"] = (
                value if isinstance(value, str) and value in RECORDED_CACHE_OUTCOMES else None
            )
        rows.append(reported)
    return rows or None


def image_build_entry(
    connection: sqlite3.Connection, attempt: Any, spec_stage: Any
) -> dict[str, Any] | None:
    """The ``build`` receipt object of an image-build stage (ADR 0123), or None for any other.

    Every pin (context digest, Dockerfile, builder image, base images, repository) comes from
    the approved spec, never from the worker. The produced image, digest and size come only from
    the validated row the worker reported for this attempt's operation, and only when that row's
    image is in the spec's repository; otherwise they are null and the entry is ``declared_only``.
    The registry's credentials and auth paths are never read or printed."""
    plan = getattr(spec_stage, "build", None)
    if plan is None:
        return None
    from .image_build_report import image_build_for_operation

    context = next((i for i in spec_stage.inputs if i.name == plan["context_input"]), None)
    repository = plan["repository"]
    entry: dict[str, Any] = {
        "context_input": plan["context_input"],
        "context_digest": None if context is None else context.digest,
        "dockerfile": plan["dockerfile"],
        "builder_image": spec_stage.image,
        "base_images": list(plan["base_images"]),
        "repository_host": repository.split("/", 1)[0],
        "repository": repository,
        "push": plan["push"],
        "via_control": False,
        "produced_image": None,
        "digest": None,
        "size_bytes": None,
        "pushed": None,
        "evidence": "declared_only",
    }
    operation_id = attempt["operation_id"]
    row = None if not operation_id else image_build_for_operation(connection, operation_id)
    if not isinstance(row, dict):
        return entry
    image, digest, size, pushed = (row.get(k) for k in ("image", "digest", "size_bytes", "pushed"))
    name, _, image_digest = image.partition("@") if isinstance(image, str) else ("", "", "")
    if (
        name != repository
        or not image_digest.startswith("sha256:")
        or image_digest != f"sha256:{digest}"
        or type(size) is not int
        or type(pushed) is not bool
    ):
        return entry
    entry.update(
        produced_image=image,
        digest=image_digest,
        size_bytes=size,
        pushed=pushed,
        evidence="provider_reported",
    )
    return entry


def _retained_fetched(
    connection: sqlite3.Connection, provider_id: str, attempt: Any, spec_stage: Any = None
):
    """The provider's retained ``fetched`` rows for the attempt, or None when the provider has
    no way to reach them from the database (the file bytes live in custody storage, not here).
    A local worker's rows come from its recorded fetch event instead."""
    try:
        reader = getattr(stage_provider(provider_id), "retained_fetched", None)
    except KeyError:
        return None
    if reader is None:
        return None if spec_stage is None else _local_fetched(connection, attempt, spec_stage)
    return reader(connection, dict(attempt))


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


def _image_provenance(
    connection: sqlite3.Connection, attempt: sqlite3.Row
) -> dict[str, Any] | None:
    """The image record written when the attempt was dispatched, reduced to its closed fields;
    ``None`` for an attempt dispatched before the record existed (receipts keep their shape)."""
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type=?
        AND json_extract(payload_json, '$.campaign_id') = ?
        AND json_extract(payload_json, '$.stage_key') = ?
        AND json_extract(payload_json, '$.attempt') = ? ORDER BY cursor LIMIT 1""",
        (
            IMAGE_PROVENANCE_EVENT,
            attempt["campaign_id"],
            attempt["stage_key"],
            attempt["attempt"],
        ),
    ).fetchone()
    return None if row is None else image_provenance_closed(json.loads(row["payload_json"]))


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
    pulls: dict[tuple[str, int], dict[str, Any]] | None = None,
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
    # ADR 0041 section 7: what control read from the guardian's per-frame record, when it did.
    from .execution_frames import latest_guardian_record

    guardian_record = latest_guardian_record(connection, attempt["id"])
    if guardian_record is not None:
        entry["guardian_record"] = guardian_record
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
        provider_id, spec_stage, _retained_fetched(connection, provider_id, attempt, spec_stage)
    )
    if public is not None:
        if attempt["failure_class"] == PUBLIC_FETCH_UNREPORTED:
            # ADR 0097: the attempt failed closed because the worker's fetch report never
            # arrived; its files stay declared_only and the receipt says why.
            public["refused"] = {
                "code": PUBLIC_FETCH_UNREPORTED,
                "reason": attempt["failure_detail"],
            }
        entry["public_fetch"] = public
    seen_image = _image_provenance(connection, attempt)
    if seen_image is not None:
        entry["image_provenance"] = seen_image
    built = image_build_entry(connection, attempt, spec_stage)
    if built is not None:
        entry["build"] = built
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
        if decision.get("allocation_instability") is not None:
            # ADR 0148: instability counts per GPU class and the fallback outcome.
            entry["placement"]["allocation_instability"] = decision["allocation_instability"]
        if decision.get("class_index"):
            # ADR 0060 addendum: the candidate's class after earlier pre-start failures there.
            entry["placement"]["class_index"] = decision["class_index"]
    observed = campaign_allocation_instability.attempt_entry(connection, attempt)
    if observed is not None:
        # ADR 0148: this attempt's own allocation change (only when the operator opted in).
        entry["allocation_instability"] = observed
    # ADR 0107: a heavy stage that fell back to a local provider while a remote profile was
    # registered carries the warning (from its placement, or for a one-provider stage from the
    # recorded warning event).
    guard = (decision or {}).get("heavy_guard") or (guards or {}).get(
        (attempt["stage_key"], attempt["attempt"])
    )
    if guard is not None:
        entry.setdefault("placement", {})["heavy_guard"] = guard
    pull = (pulls or {}).get((attempt["stage_key"], attempt["attempt"]))
    if pull is not None:
        # The image pull-size preflight recorded for this attempt (own section; sizes and
        # limits are integers, the reason a closed code).
        entry[IMAGE_PULL_SECTION] = pull
    try:
        extra = stage_provider(provider_id).receipt_fields(connection, dict(attempt))
    except KeyError:
        extra = {}  # an unregistered provider adds nothing; the core fields stand
    scratch = _scratch(connection, attempt, spec_stage)
    if scratch is not None:
        entry["scratch"] = scratch
    if is_infra_class(attempt["failure_class"]):
        # ADR 0058: the attempt ended for an infrastructure reason and was retried (or, when
        # the bound was spent, the stage failed); the reason is the recorded detail.
        entry["infrastructure_retry"] = {"reason": attempt["failure_detail"]}
    if attempt["failure_class"] == class_fallback.PRESTART_NO_SPEND:
        # ADR 0060 addendum: never started on this class, settled at zero; the stage moved on.
        # Closed codes and counts only, from the event recorded with the transition.
        moved = connection.execute(
            """SELECT payload_json FROM events WHERE event_type=?
            AND json_extract(payload_json, '$.attempt_id') = ? ORDER BY cursor DESC LIMIT 1""",
            (class_fallback.EVENT, attempt["id"]),
        ).fetchone()
        seen = {} if moved is None else json.loads(moved["payload_json"])
        code = attempt["failure_detail"]
        entry["class_fallback"] = {
            "reason": code if code in class_fallback.REASONS else None,
            "class_index": seen.get("class_index"),
            "path": seen.get("path") if seen.get("path") in class_fallback.PATHS else None,
            "is_evidence": False,
        }
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
        if seen.get("closed_reason") is not None:
            # ADR 0158: closed on a watcher-reported exit with no run report; the closed reason,
            # the exit class and the watch event it was based on (older entries keep their shape).
            entry["exit_resolved"].update(
                {
                    "closed_reason": seen["closed_reason"],
                    "exit_class": seen.get("exit_class"),
                    "exit_code": seen.get("exit_code"),
                    "watch_event_cursor": seen.get("watch_event_cursor"),
                }
            )
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


def _bound_section(connection: sqlite3.Connection, campaign: Any, stage_key: str) -> dict[str, Any]:
    """The stage-output inputs bound when the stage started (ADR 0140): per input the source
    stage, output name, resolved digest, bytes, data class, licence labels and ``bound_at`` (the
    binding event's timestamp). A refused binding is one ``input_not_pinned`` entry with the
    closed refusal code, appended after any entries the stage bound before it, so the final
    failure reads as a refused bind. Absent for a stage that bound or refused none, so existing
    receipts keep their bytes."""
    from .late_bound_inputs import (
        LAUNCH_INPUTS_KEY,
        bound_receipt_entries,
        refused_receipt_entries,
    )

    entries = bound_receipt_entries(connection, campaign, stage_key) + refused_receipt_entries(
        connection, campaign, stage_key
    )
    # ADR 0197: a checkpoint salvaged from an interrupted attempt is listed by digest too.
    entries = entries + recorded_checkpoints(connection, campaign, stage_key)
    return {LAUNCH_INPUTS_KEY: entries} if entries else {}


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


def _approval_delegation(
    connection: sqlite3.Connection, campaign_id: str, approval_id: str
) -> dict[str, str]:
    """The owner delegation the approving helper named, from the approval's own event.

    Recorded on ``campaign.approved`` by the decision provenance; absent when none was sent, so
    existing receipts keep their shape."""
    row = connection.execute(
        """SELECT json_extract(payload_json,'$.delegation_ref') AS ref FROM events
        WHERE event_type='campaign.approved' AND json_extract(payload_json,'$.approval_id')=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor DESC LIMIT 1""",
        (approval_id, campaign_id),
    ).fetchone()
    ref = row["ref"] if row is not None else None
    return {"delegation_ref": ref} if isinstance(ref, str) and ref else {}


def _smoke_grant(connection: sqlite3.Connection, principal_id: str) -> dict[str, str]:
    """``smoke_grant_id`` when the approval was made under a smoke credential grant."""
    from .smoke_credentials import grant_id_for_principal

    grant_id = grant_id_for_principal(connection, principal_id)
    return {} if grant_id is None else {"smoke_grant_id": grant_id}


def _wall_clock(connection: sqlite3.Connection, campaign_id: str, spec: Any) -> dict[str, Any]:
    """The campaign's total wall-clock bound, recorded (audit G-13).

    A campaign ends ``deadline_seconds`` after its FIRST approval; a re-approval gets at most the
    time that is left (``CampaignStore.approve_campaign``), so no number of re-approvals extends
    it. Only present once the campaign was approved. ``approvals`` is how many approval rows the
    campaign has had; the bound does not depend on it."""
    row = connection.execute(
        "SELECT MIN(approved_at) AS first, COUNT(*) AS n FROM campaign_approvals "
        "WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()
    if row is None or row["first"] is None:
        return {}
    first = _parse_utc(row["first"])
    seconds = int(spec.deadline_seconds)
    return {
        "wall_clock": {
            "deadline_seconds": seconds,
            "first_approved_at": _utc_string(first),
            "bound_at": _utc_string(first + timedelta(seconds=seconds)),
            "approvals": int(row["n"]),
            "extended_by_reapproval": False,
        }
    }


def _placement_refused(
    connection: sqlite3.Connection, campaign: sqlite3.Row, stage: sqlite3.Row
) -> dict[str, Any]:
    """ADR 0060: a stage no candidate could ever serve (over the per-run cap, untrusted, ...)
    failed before any attempt; the refusal and each candidate's reason are on the receipt.
    Absent otherwise, so other receipts keep their shape."""
    if stage["status"] != "failed":
        return {}
    refused = final_refusal(connection, campaign, stage["stage_key"])
    return {} if refused is None else {"placement_refused": refused}


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
    # ADR 0141: what submit did about each heavy stage's remote candidate (from the spec).
    remote_candidates = heavy_remote_records(current["spec_json"])
    # ADR 0107 amendment 2026-11-23c: a heavy stage the guard refused with no attempt for
    # that number (from the recorded refusal or the ended refuse-mode wait).
    refusals = recorded_heavy_refusals(
        connection, current, stages, {attempt["stage_key"] for attempt in attempts}
    )
    pulls = recorded_image_pulls(connection, current)
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
        # ADR 0164: what the stage reads under a seal, and for which role; absent otherwise.
        sealed = sealed_access(normal, stage["stage_key"])
        profiles = _salad_profiles(connection, current, spec_stage, stage)
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
                    **_bound_section(connection, current, stage["stage_key"]),
                },
                **({} if code_info is None else {"code": code_info}),
                **({"sealed_access": sealed} if sealed else {}),
                "output_data_class": stage["output_data_class"],
                "attempts": [
                    {
                        **_attempt_entry(
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
                            pulls,
                        ),
                        # ADR 0151: the profile this attempt ran on (stages with alternates),
                        # only when it ran on a candidate that lists them.
                        **(
                            {}
                            if profiles is None
                            else _attempt_salad_profile(connection, current, spec_stage, attempt)
                        ),
                    }
                    for attempt in attempts
                    if attempt["stage_key"] == stage["stage_key"]
                ],
                "stop_results": _json(stage["stop_results_json"], []),
                **(
                    {"heavy_remote_candidate": remote_candidates[stage["stage_key"]]}
                    if stage["stage_key"] in remote_candidates
                    else {}
                ),
                **(
                    {"placement": {"heavy_guard": refusals[stage["stage_key"]]}}
                    if stage["stage_key"] in refusals
                    else {}
                ),
                # ADR 0151: the approved profile order, each switch and why, and a stop;
                # absent for a stage without alternate profiles, so its receipt keeps its shape.
                **({} if profiles is None else {"salad_profiles": profiles}),
                **_placement_refused(connection, current, stage),
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
                **_approval_delegation(connection, campaign_id, approval["id"]),
                "approved_at": approval["approved_at"],
                "expires_at": approval["expires_at"],
                **(
                    {"delegation_id": approval["delegation_id"]}
                    if approval["delegation_id"] is not None
                    else {}
                ),
                **_review_delegation(connection, approval),
                **_smoke_grant(connection, approval["approving_principal_id"]),
            },
            **_wall_clock(connection, campaign_id, parse_spec_json(current["spec_json"])),
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
            # ADR 0122: the owner-signed delegation a decision on this campaign cited, and every
            # delegated decision (approved, gate go or no-go, escalated); absent otherwise.
            **delegation_receipt_section(connection, campaign_id),
            # ADR 0143: the approved submission this campaign was created from and its
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


def _salad_profiles(
    connection: sqlite3.Connection, campaign: Any, spec_stage: Any, stage: Any
) -> dict[str, Any] | None:
    """The stage's ADR 0151 section; None without alternates. The candidate is the one the
    stage is placed on, else the first approved candidate that lists alternates: a stage that
    moved on to (or ended on) another candidate keeps the record of its profile moves."""
    from .campaign_models import stage_on_candidate

    candidates = [stage["provider_id"], *spec_stage.candidate_ids]
    for pid in dict.fromkeys(item for item in candidates if item):
        try:
            approved = (
                spec_stage if pid == spec_stage.provider else stage_on_candidate(spec_stage, pid)
            )
        except KeyError:
            continue
        section = campaign_salad_fallback.receipt_section(
            connection, campaign, stage["stage_key"], approved
        )
        if section is not None:
            return section
    return None


def _attempt_salad_profile(
    connection: sqlite3.Connection, campaign: Any, spec_stage: Any, attempt: Any
) -> dict[str, Any]:
    """``{"salad_profile": ...}`` when the attempt ran on a candidate that lists alternate
    profiles, else nothing: an attempt placed on another provider never carries a Salad label,
    even when a switch named its attempt number (ADR 0151, ADR 0060)."""
    name = campaign_salad_fallback.attempt_profile(connection, campaign, spec_stage, dict(attempt))
    return {} if name is None else {"salad_profile": name}


def _review_delegation(connection: sqlite3.Connection, approval: Any) -> dict[str, Any]:
    """ADR 0150 amendment: the owner-signed delegation the approving owner bound to this direct
    approval's per-run reviews (id only, never the signer); absent when none was bound."""
    from .campaign_preapproval import bound_review_delegation

    if approval["delegation_id"] is not None:
        return {}
    found = bound_review_delegation(connection, approval)
    return {"review_delegation_id": found} if found else {}


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
