"""Salad GPU stage driver for the campaign runner (ADR 0038 amendment).

The runner keeps every generic phase (prepare, compose, reserve, release, grant, admit,
collect, resolve, lineage). For a ``salad-gpu-v1`` stage this driver adds what is
specific: a stage-level study scaffold, the per-attempt reviews, capability and route
steps, verification of the executor's recorded outcome, and the unwind of an attempt that
claimed no provider effect. It never calls a provider, never resolves a human decision,
never records a charge and never settles without a charge or an unclaimed proof; the
operator-run executor (``campaign_salad_executor``) owns every provider-facing effect.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from . import campaign_preapproval, operator_config_freshness, provider_trust
from . import campaign_review_carryover as review_carryover
from . import campaign_salad_lease_lapse as lease_lapse
from . import execution_frames as frames
from .artifact_manifests import grant_manifest_is_partial
from .campaign_autoreconcile import MAX_AUTO_REQUEUES, NOT_STARTED, repeat_count
from .campaign_infra_retry import (
    INFRA_SALAD_CREATE_NOT_SENT,
    INFRA_SALAD_REALLOCATED,
    INFRA_SALAD_RECONCILED_NO_OUTPUT,
    REALLOCATED_PRECUSTODY,
    RECONCILED_NO_OUTPUT_REASON,
    SALAD_INTERRUPTED,
    infra_attempts,
    provisioning_overrun,
    safety_stop_cause,
)
from .campaign_models import (
    SALAD_TARGET,
    TERMINAL_CAMPAIGN,
    SaladStageSpec,
    StageSpec,
    stage_on_candidate,
)
from .campaign_checkpoint import CHECKPOINT_PATH, composed_checkpoint
from .campaign_salad_adapter import MIB, bind_workload, is_late_bound
from .campaign_salad_commands import LIVE_CHECK_NO_RECEIPT, SILENT_EXECUTOR
from .campaign_salad_executor import (
    FINISH_RETRY_SECONDS,
    SUBPROCESS_GRACE_SECONDS,
    TERMINATION_CONFIRM_MAX_SECONDS,
)
from .campaign_settlement import apply_late_charge, confirm_settlement
from .campaign_stage_failure_stop import STAGE_FAILED_STOP
from .campaign_stage_failure_stop import attempt_held as stop_held
from .campaign_steps import StepFailed
from .campaign_store import _GuardianFinal
from .campaign_unclaimed import Refused
from .campaign_unclaimed import assess as assess_unclaimed
from .execution import reconcile_grant_row
from .external_operations import current_operation_reconciliation
from .guardian_record_reader import validate_frame_record
from .programs import program_context
from .remote_execution_models import (
    ExecutionSelection,
    TargetPolicy,
    salad_route_window_description,
)
from .salad_campaign_profiles import (
    DATA_POLICY_PUBLIC,
    DATA_POLICY_TRUST,
    SaladCampaignProfile,
    capability_declaration,
    derive_selection,
    derived_digests,
    stage_data_policy,
    target_policy,
)
from .salad_guardian import RECONCILE_PROVIDER
from .salad_precreate import sent_launch_effects
from .salad_termination import (
    UNCONFIRMED_CODE,
    UNCONFIRMED_EVENT,
    UNCONFIRMED_NEW_ALLOCATION_CODE,
    provider_termination,
)
from .salad_unclaimed import create_claimed
from .store import Conflict, _parse_utc, canonical_digest

# Failure detail of an attempt whose live receipt shows the run ended before any create was
# sent (ADR 0205); it is unwound as unclaimed (``t1_salad_unclaimed``).
CREATE_NOT_SENT = "live_check_create_not_sent"
# A create never sent because the guardian's open-frame fence was full at the reserve
# (``single_operation_orphan_exposure``, the race the executor's pre-launch capacity read cannot
# close): a wait for capacity, requeued as not started within ADR 0046's repeat bound.
FRAME_FENCE_BEFORE_CREATE = "guardian_frame_fence_before_create"
FRAME_FENCE_REFUSAL = "single_operation_orphan_exposure"

if TYPE_CHECKING:
    from .campaign_runner import CampaignRunner, _Ctx

# Attempt phases in which provider exposure may exist or is unresolved: a new reservation
# with a positive hold waits while any attempt of the campaign is in one of them.
EXPOSURE_PHASES = (
    "leased",
    "awaiting_review",
    "reviewed",
    "capability_ready",
    "route_proposed",
    "route_started",
    "executing",
    "outcome_recorded",
    "awaiting_charge",
    "unknown",
)
# Phases whose cost is not bounded by a live hold of their own: a charge is owed or unknown.
# A zero-cent infrastructure retry waits only for these, not for a healthy running sibling.
UNRESOLVED_EXPOSURE_PHASES = ("outcome_recorded", "awaiting_charge", "unknown")
PRE_ROUTE_PHASES = ("leased", "awaiting_review", "reviewed", "capability_ready", "route_proposed")
DECISION_HOURS = 23
CAPABILITY_HOURS = 23
SILENT_EXECUTOR_GRACE_SECONDS = 600
# An ``executing`` attempt whose lease the executor keeps renewing is not called silent while
# it is inside the executor's own budget: the route window, the child's subprocess grace, the
# longest termination-confirmation wait an operator may configure and the deferred finish.
# Derived from the executor's constants so the two sides cannot drift; never past this cap, and
# never past the lease's own expiry.
EXECUTING_RENEWED_GRACE_SECONDS = (
    SUBPROCESS_GRACE_SECONDS + TERMINATION_CONFIRM_MAX_SECONDS + int(FINISH_RETRY_SECONDS)
)
# ``resolve_decision`` slots of a carried-over review (plan, source use): apart from the
# pre-approval's 0 and 1, so a refused carryover never replays into the pre-approval path.
CARRYOVER_ORDINAL = 10
# v0.104.0: the silent window passed for a route control can prove was never sent anything;
# it takes the existing unclaimed unwind and is requeued as not started (ADR 0046), never
# ``unknown``.
SILENT_NEVER_SENT = "executor_silent_never_sent"
# Events after which a campaign is forward-legal again: the silent clock restarts there, so
# time spent blocked or paused (when the executor was not allowed to report) never counts.
RESUME_EVENTS = ("campaign.unblocked", "campaign.approved")
# One event per attempt whose route already shows provider-confirmed termination when the
# silent window passed: the attempt waits for the executor's import instead of going unknown.
IMPORT_AWAITED_EVENT = "campaign.salad_import_awaited"
PARTITION = "operational"
CONDITION = "operational_run"
REVIEW_REASON = (
    "remote_execution._reviews requires current human program and source-use reviews "
    "bound to this admitted attempt (ADR 0032, not delegated)"
)


# Credential states that stop a stage up front (the sibling credential health module owns the
# states; 'unknown' and 'warning' never block: this is an early guard and the guardian stays
# the authority when the credential is resolved).
CREDENTIAL_BLOCKING_STATES = frozenset({"expired", "revoked", "version_mismatch"})
_CREDENTIAL_CODES = {
    "expired": "credential_expired",
    "revoked": "credential_revoked",
    "version_mismatch": "credential_version_mismatch",
}


def credential_blocked(state: Any) -> bool:
    """Whether a ``credential_state`` answer is one that refuses a stage."""
    return isinstance(state, dict) and state.get("state") in CREDENTIAL_BLOCKING_STATES


def credential_code(state: dict[str, Any]) -> str:
    return _CREDENTIAL_CODES.get(str(state.get("state")), "credential_unavailable")


def credential_message(state: dict[str, Any]) -> str:
    """The sanitized refusal text for a blocking credential state: the reference, version and
    time only (never a value), and what the operator does next."""
    code = credential_code(state)
    reference, profile = state.get("reference"), state.get("profile")
    if code == "credential_version_mismatch":
        return (
            f"{code}: profile {profile} pins the Salad management credential {reference} at "
            f"version {state.get('pinned_version')} but the enrolled credential is version "
            f"{state.get('version')}; update the profile credential_version "
            "(docs/salad-operator-setup.md)."
        )
    when = state.get("expires_at")
    stamp = "an unknown time"
    if isinstance(when, int | float) and math.isfinite(when):
        stamp = datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%d %H:%M UTC")
    verb = "was revoked" if code == "credential_revoked" else f"expired at {stamp}"
    return (
        f"{code}: the Salad management credential {reference} version {state.get('version')} "
        f"{verb} (profile {profile}); rotate it (docs/salad-operator-setup.md / the enrollment "
        "tool) and update the profile credential_version."
    )


def salad_config(spec_stage: Any) -> SaladStageSpec:
    """The stage's Salad fields: ``.salad`` (spec v1) or ``provider_config`` (normal form)."""
    found = getattr(spec_stage, "salad", None)
    if found is None:
        found = getattr(spec_stage, "provider_config", None)
    if isinstance(found, SaladStageSpec):
        return found
    if not found:
        raise StepFailed("salad", "step_refused", "salad_config_missing")
    return SaladStageSpec.model_validate(found)


PREVIEW_FAILED = "review_preview_failed"


_PREVIEW_VALUE_PREFIX = "Value error, "
_PREVIEW_UNSAFE = re.compile(r"[0-9a-fA-F]{12,}|\S*[/\\@~]\S*|[^\x20-\x7e]")


def _preview_message(text: str) -> str:
    """A validation message with anything environment-specific removed: digests, paths, hosts,
    home directories and non-printable characters; no commas (the reason list is comma-joined)."""
    text = text.removeprefix(_PREVIEW_VALUE_PREFIX)
    text = _PREVIEW_UNSAFE.sub("<redacted>", text).replace(",", ";")
    return " ".join(text.split())[:160]


def _preview_validation(error: ValidationError) -> str:
    """The closed part of a pydantic failure: per error its type, a location made only of
    identifier-like parts, and the message. Never the input values or the exception object."""
    found = []
    for item in error.errors(include_url=False, include_input=False, include_context=False)[:3]:
        loc = ".".join(
            str(part)
            for part in item.get("loc", ())
            if re.fullmatch(r"[A-Za-z0-9_]{1,40}", str(part))
        )
        kind = re.sub(r"[^a-z0-9_]", "", str(item.get("type", ""))) or "error"
        found.append(
            f"{kind}{'@' + loc if loc else ''} {_preview_message(str(item.get('msg', '')))}"
        )
    return "[" + " | ".join(found) + "]" if found else ""


def _preview_failure(error: Exception) -> str:
    """A short stable reason for a stage the driver could not preview: the closed code, then the
    exception type and, for a refused step, its own closed detail code. A pydantic validation
    failure also carries its error types, locations and messages, with digests, paths, hosts and
    input values removed; any other message is never recorded (it may carry a path or host)."""
    code = PREVIEW_FAILED + ":" + type(error).__name__
    detail = getattr(error, "detail", None)
    if isinstance(detail, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", detail):
        code += ":" + detail
    if isinstance(error, ValidationError):
        code += ":" + _preview_validation(error)
    return code


# Late-bound stage-output inputs (``campaign_salad_adapter.late_bind_key``). The event records,
# per bound snapshot, each resolved input the Salad stage receives; the refusal codes are closed:
# the resolver's own codes (``late_bound_inputs.REFUSAL_CODES``) or one of these.
LATE_BOUND_EVENT = "campaign.salad_late_bound_inputs"
LATE_BIND_REFUSED_EVENT = "campaign.salad_late_bind_refused"
BIND_REFUSED_EVENT = "campaign.stage_input_bind_refused"  # the runner's refusal event (ADR 0140)
LATE_BIND_UNRESOLVED = "late_bound_input_unresolved"  # no composed input snapshot recorded
LATE_BIND_MISSING = "late_bound_input_missing"  # a resolved output is not in the snapshot
LATE_BIND_MISMATCH = "late_bound_input_digest_mismatch"  # snapshot bytes differ from receipt
LATE_BIND_OVER_CAP = "late_bound_input_over_cap"  # over the reviewed contract max_bytes
LATE_BIND_RESOLVER = "late_bound_resolver_unavailable"  # the core resolver is not installed
LATE_BIND_CODES = frozenset(
    {
        LATE_BIND_UNRESOLVED,
        LATE_BIND_MISSING,
        LATE_BIND_MISMATCH,
        LATE_BIND_OVER_CAP,
        LATE_BIND_RESOLVER,
    }
)


# A reviewed workload contract (or the operator profile) cannot form a valid route selection.
SELECTION_INVALID = "salad_workload_contract_invalid"
# A checkpoint retry whose raised input cap does not fit the operator profile (ADR 0211).
CHECKPOINT_OVER_CAP = "salad_checkpoint_over_cap"


def stage_checkpoint_name(spec_stage: Any) -> str:
    """The declared checkpoint output's name (for a refusal that names it)."""
    return next((o.name for o in spec_stage.outputs if o.path == CHECKPOINT_PATH), "checkpoint")


def stage_workload(
    stage: dict[str, Any], spec_stage: Any, *, checkpoint_bytes: int | None = None
) -> Any:
    """The workload contract the stage's route carries.

    An adapted stage whose inputs include another stage's output declares its input by the
    late-bind key; once the runner has composed the input snapshot, the contract carries that
    snapshot's digest instead (``bind_workload``), so the route's ``manifests.data``, the
    contract's input and the argv ``input:`` token are one digest. Before composition the
    reviewed contract is returned unchanged, which never matches a route's data digest.

    A checkpoint-resume retry (ADR 0197) composes a new snapshot with one more manifest source,
    so the digest a pinned (not late-bound) adapted contract declared no longer names the input
    and the reviewed cap has no room for the checkpoint. ``checkpoint_bytes`` is the size the
    checkpoint's own receipt recorded (``composed_checkpoint``), never caller-supplied: the
    contract is then bound to the composed digest with ``input.max_bytes`` raised by exactly that
    many bytes (ADR 0211). ``retry_input_refusal`` refuses it before any lease when the raised cap
    no longer fits the operator profile."""
    salad = salad_config(spec_stage)
    contract = salad.workload
    if not salad.adapted or contract.input is None:
        return contract
    if not is_late_bound(spec_stage, contract) and not checkpoint_bytes:
        return contract
    digest = stage.get("input_snapshot_digest")
    if not digest:
        return contract
    cap = contract.input.max_bytes + (checkpoint_bytes or 0)
    return bind_workload(contract, str(digest), cap if checkpoint_bytes else None)


def _termination_confirmed(state: dict[str, Any] | None) -> bool:
    """The route's own provider receipts confirm the run stopped (the precondition
    ``verify_outcome`` checks first; a reallocated group needs the provider's own read)."""
    if state is None:
        return False
    if state.get("reallocated"):
        return provider_termination(state).confirmed
    return (
        state.get("termination_confirmed") is True
        and state.get("provider_status") == "stopped"
        and any(r.get("provenance") == "provider" for r in state.get("receipts", []))
    )


def _waiting() -> type[Exception]:
    from .campaign_runner import _Waiting

    return _Waiting


def _resolve_build(decision_id: str, revision: int, kind: str, digest: str, pre_id: str):
    def build() -> tuple[dict[str, str], dict[str, Any]]:
        return (
            {"decision_id": decision_id, "preapproval_id": pre_id},
            {
                "decision_id": decision_id,
                "expected_revision": revision,
                "decision": "approve",
                "rationale": (
                    f"Delegated under review pre-approval {pre_id} (exact {kind} digest {digest})"
                ),
            },
        )

    return build


def _carryover_build(
    decision_id: str, revision: int, kind: str, prior_id: str, prior_attempt: int | None
):
    def build() -> tuple[dict[str, str], dict[str, Any]]:
        return (
            {"decision_id": decision_id, "carryover_decision_id": prior_id},
            {
                "decision_id": decision_id,
                "expected_revision": revision,
                "decision": "approve",
                "rationale": (
                    f"Carried over from approved {kind} review {prior_id} of attempt "
                    f"{prior_attempt}, requeued before its route for a lapse (exact review core)"
                ),
            },
        )

    return build


def _credential_event(state: dict[str, Any]) -> dict[str, Any]:
    """Event payload: states, reference, version and time; never any credential value."""
    return {
        "state": state.get("state"),
        "reference": state.get("reference"),
        "version": state.get("version"),
        "expires_at": state.get("expires_at"),
        "reason": credential_message(state) if credential_blocked(state) else state.get("reason"),
    }


def stage_data_digest(stage: dict[str, Any], spec_stage: Any) -> str:
    """The digest the stage's data manifest binds: the recorded input snapshot digest, or, for a
    stage with no snapshot (only ``public_artifact`` inputs, ADR 0097), the digest of its pinned
    public files, computed as the runner records it. Never None, so the pins stay bound."""
    recorded = stage.get("input_snapshot_digest")
    if recorded:
        return str(recorded)
    rows: list[list[Any]] = []
    for item in spec_stage.inputs:
        if getattr(item, "kind", None) != "public_artifact":
            continue
        files = getattr(item, "public_files", None)
        if files is None or (not files and getattr(item, "files", None)):
            files = [(f.name, f.url, f.sha256, f.max_bytes) for f in item.files]
        rows += [[f"{item.name}/{name}", sha, size] for name, _url, sha, size in files]
    return canonical_digest(sorted(rows))


# Closed values naming which signal made a reallocation retryable (ADR 0179 addendum).
SIGNAL_ROUTE_FLAG = "route_flag"
SIGNAL_GUARDIAN = "guardian_evidence"


def _guardian_precustody_evidence(
    receipt: dict[str, Any], *, provider_confirmed: bool = True
) -> bool:
    """Fail closed: only the live receipt's own pre-custody word counts. Custody must be
    literally False (not None), the stop cause an allocation change and, unless the route's own
    provider read already confirmed it, the provider's stop confirmed."""
    return (
        receipt.get("reason") == REALLOCATED_PRECUSTODY
        and receipt.get("guardian_stop_cause") == "allocation_changed"
        and receipt.get("custody_work_begun") is False
        and (not provider_confirmed or receipt.get("provider_stop_confirmed") is True)
    )


def _guardian_interruption_evidence(
    receipt: dict[str, Any], *, provider_confirmed: bool = True
) -> bool:
    """Fail closed: the live receipt's own word that custody work HAD begun (literally True,
    not None) when the guardian stopped on an allocation change, and (unless the route's own
    provider read already confirmed it) the provider's stop confirmed."""
    return (
        receipt.get("guardian_stop_cause") == "allocation_changed"
        and receipt.get("custody_work_begun") is True
        and (not provider_confirmed or receipt.get("provider_stop_confirmed") is True)
    )


def _never_allocated(row: dict[str, Any]) -> bool:
    """The executor's recorded guardian final says the workload never ran: the guardian's
    allocation-timeout record (cancelled, or the route limit reached, with no machine ever
    placed, never seen running and no custody begun), or its route-limit record of a placed
    machine that never got going (``provisioning_overrun``). Only that recorded evidence counts,
    never a reason string."""
    try:
        found = json.loads(row.get("guardian_final_json") or "null")
    except ValueError:
        return False
    if type(found) is dict and provisioning_overrun(found.get("route_deadline")):
        return found.get("allocation_timeout") is None
    timeout = found.get("allocation_timeout") if type(found) is dict else None
    return (
        type(timeout) is dict
        and type(timeout.get("elapsed_seconds")) is int
        and type(timeout.get("deadline_seconds")) is int
        and found.get("route_deadline") is None
    )


def _was_interrupted(attempt: dict[str, Any]) -> bool:
    """The attempt ended on a post-custody interruption: its class, or (before the runner has
    recorded the class) the detail the verdict step wrote."""
    detail = str(attempt.get("failure_detail") or "").removeprefix("salad:")
    return attempt.get("failure_class") == SALAD_INTERRUPTED or detail.startswith(SALAD_INTERRUPTED)


# Classes of a move the provider itself made, its stop confirmed by its own read and an exited
# worker, whose custody could not be placed after the move (``infra_salad_reallocated``) or was
# the guardian's pre-custody word (``provider_reallocated_before_custody``). The guardian closes
# such a frame by the provider-confirmed reconcile, so its final reads ``reconciled`` with
# ``allocation_uncertain`` set, never ``stopped``. ADR 0212's reconciled-without-output class
# closes the same way (the safety service's provider-confirmed reconcile of an uncertain
# allocation), so it settles and is read the same way.
PROVIDER_MOVED_CLASSES = (
    INFRA_SALAD_REALLOCATED,
    REALLOCATED_PRECUSTODY,
    INFRA_SALAD_RECONCILED_NO_OUTPUT,
)

# The verify step's word for a route whose own provider receipts never confirmed termination.
ROUTE_UNCONFIRMED = "route termination is not confirmed by provider receipts"
# ADR 0212: why a re-read attempt is still held (closed code), recorded once per change of code;
# an unexpected error of that re-read, recorded once per error class; control's refresh of the
# guardian final it re-reads; and the least time between two such guardian reads per attempt.
RECONCILED_NO_OUTPUT_HELD_EVENT = "campaign.salad_reconciled_no_output_held"
RECONCILED_NO_OUTPUT_ERROR_EVENT = "campaign.salad_reconciled_no_output_error"
GUARDIAN_FINAL_REFRESHED_EVENT = "campaign.salad_guardian_final_refreshed"
GUARDIAN_FINAL_REFRESH_SECONDS = 60
# Guardian statuses after which its final never changes (the guardian reconciles only a frame
# that is neither stopped nor reconciled).
_TERMINAL_FINAL = frozenset({"stopped", "reconciled"})
# Provider readback states that list a live (or starting) instance for the route.
_LIVE_READ_STATES = frozenset({"running", "pending", "deploying"})


def _lists_live_instance(state: dict[str, Any], since: datetime) -> bool:
    """The route's latest provider readback observed at or after ``since`` (the guardian
    final's observation) lists a live or starting instance (or any allocation). Readbacks older
    than the final are superseded by the safety service's later provider-confirmed close, and a
    later clean readback supersedes an earlier live one. A provider readback whose time cannot
    be read counts as current and is never superseded (fail closed)."""
    current: list[tuple[datetime | None, dict[str, Any]]] = []
    for receipt in state.get("receipts") or []:
        if type(receipt) is not dict or receipt.get("provenance") != "provider":
            continue
        try:
            observed = _parse_utc(str(receipt.get("observed_at")))
        except (TypeError, ValueError):
            observed = None
        if observed is None or observed >= since:
            current.append((observed, receipt))

    def live(receipt: dict[str, Any]) -> bool:
        return receipt.get("state") in _LIVE_READ_STATES or receipt.get("allocation_id") is not None

    if any(observed is None and live(receipt) for observed, receipt in current):
        return True
    timed = [(observed, receipt) for observed, receipt in current if observed is not None]
    if not timed:
        return False
    latest = timed[0]
    for item in timed[1:]:
        if item[0] >= latest[0]:  # the later-recorded of equal times wins
            latest = item
    return live(latest[1])


def reconciled_no_output_refusal(
    connection: sqlite3.Connection,
    attempt: dict[str, Any],
    row: dict[str, Any],
    state: dict[str, Any],
) -> str | None:
    """ADR 0212: ``None`` when an attempt whose route never confirmed termination may resolve as
    ``infra_salad_reconciled_no_output``; otherwise the closed code of the first condition that
    keeps it ``unknown`` (fail closed). Pure read.

    Every condition must hold:

    * the executor's recorded guardian final is ``reconciled`` by the safety service's own
      provider-confirmed close (``RECONCILE_PROVIDER``, never an operator attestation) with
      ``allocation_uncertain`` set;
    * that final is clean (no orphan, escalation, budget fence or unknown rate, no billed cost)
      and carries a finite cost estimate whose next whole cent is at least 0 and within the hold;
    * no output was retained, sealed or collected: no retained-before-move marker, no manifest or
      uploaded artifact under the grant, and no output named or retained on the live receipt;
    * no provider readback recorded on the route since that final lists a live instance."""
    if row.get("guardian_final_json") is None:
        return "no_guardian_final"
    try:
        final = _GuardianFinal.model_validate_json(row["guardian_final_json"])
    except ValueError:
        return "guardian_final_unreadable"
    if final.status != "reconciled" or final.reason != RECONCILE_PROVIDER:
        return "guardian_not_provider_reconciled"
    if not final.allocation_uncertain:
        return "allocation_not_uncertain"
    if final.orphan or final.escalation_pending or final.budget_fenced or final.rate_unknown:
        return "guardian_final_not_clean"
    if final.actual_billed_cost_cents is not None:
        return "billed_cost_recorded"
    try:
        estimate = Decimal(final.estimated_exposure_cents or "")
        rate = Decimal(final.published_hourly_rate_usd or "")
    except InvalidOperation:
        return "no_cost_estimate"
    if not estimate.is_finite() or estimate < 0 or not rate.is_finite() or rate <= 0:
        return "no_cost_estimate"
    hold = int(attempt.get("hold_cents") or 0)
    if hold < 1 or max(1, math.ceil(estimate)) > hold:
        return "estimate_above_hold"
    if final.retained_before_move is True:
        return "output_retained"
    if row.get("manifest_id") is not None or attempt.get("manifest_id") is not None:
        return "output_collected"
    try:
        receipt = json.loads(row.get("live_receipt_json") or "null")
    except ValueError:
        return "live_receipt_unreadable"
    if type(receipt) is not dict:
        return "live_receipt_unreadable"
    cancel = receipt.get("control_cancel")
    if (
        receipt.get("status") == "passed"
        or receipt.get("outputs")
        or receipt.get("output_sha256") is not None
        or (type(cancel) is dict and cancel.get("output_retained") is True)
    ):
        return "output_retained"
    grant_id = attempt.get("grant_id")
    for table in ("artifact_manifests", "artifacts"):
        if (
            grant_id is None
            or connection.execute(
                f"SELECT 1 FROM {table} WHERE grant_id=? LIMIT 1", (grant_id,)
            ).fetchone()
            is not None
        ):
            return "output_collected"
    try:
        since = _parse_utc(final.observed_at)
    except (TypeError, ValueError):
        return "guardian_final_unreadable"
    if _lists_live_instance(state, since):
        return "provider_lists_live_instance"
    return None


def _provider_moved(attempt: dict[str, Any]) -> bool:
    """The attempt ended on a provider-confirmed move (``PROVIDER_MOVED_CLASSES``): its class or,
    before the runner has recorded it, the detail control's own verify step wrote from the
    route's provider read (never a receipt reason)."""
    detail = str(attempt.get("failure_detail") or "").removeprefix("salad:")
    return attempt.get("failure_class") in PROVIDER_MOVED_CLASSES or detail.startswith(
        PROVIDER_MOVED_CLASSES
    )


def _recorded_final(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        found = json.loads(row.get("guardian_final_json") or "null")
    except ValueError:
        return None
    return found if type(found) is dict else None


def _retained_before_move(row: dict[str, Any]) -> bool:
    """The executor's recorded guardian final says the run retained its output first and the
    frame was then closed by the provider-confirmed reconcile after an allocation change while
    stopping (custody unsealed): a provider-confirmed move, settled like one."""
    final = _recorded_final(row)
    return final is not None and final.get("retained_before_move") is True


def _provider_closed(row: dict[str, Any]) -> bool:
    """The recorded guardian final was closed by the safety service's own provider-confirmed
    reconcile (``RECONCILE_PROVIDER``), whatever recorded marker (``retained_before_move``) or
    class the attempt carries: an attempt recorded before those markers existed settles the same
    way as one recorded after, on the guardian's own evidence only. Not a safety-plane stop."""
    final = _recorded_final(row)
    return (
        final is not None
        and final.get("status") == "reconciled"
        and final.get("reason") == RECONCILE_PROVIDER
        and not _safety_stopped(row)
    )


def _safety_stopped(row: dict[str, Any]) -> bool:
    """The executor's recorded guardian final names a safety-plane stop cause (closed table)."""
    try:
        return safety_stop_cause(json.loads(row.get("guardian_final_json") or "null")) is not None
    except ValueError:
        return False


def _phase_open(connection: sqlite3.Connection, campaign_id: str, phases: tuple[str, ...]) -> bool:
    marks = ",".join("?" for _ in phases)
    return (
        connection.execute(
            f"""SELECT 1 FROM campaign_salad_attempts sa JOIN campaign_stage_attempts a
            ON a.id = sa.attempt_id WHERE a.campaign_id=? AND sa.phase IN ({marks}) LIMIT 1""",
            (campaign_id, *phases),
        ).fetchone()
        is not None
    )


class SaladStageDriver:
    def __init__(
        self, runner: CampaignRunner, profiles: Any = None, guardian_reader: Any = None
    ) -> None:
        # ``runner`` is the ``RunnerServices`` object the provider was handed; the driver
        # also uses the runner's step/attempt helpers (ADR 0040 §2).
        self.runner = runner
        self._checkpoint_cache: dict[tuple[str, str, str], int | None] = {}
        self._profiles = profiles
        # Control's own read of the guardian's per-frame record (ADR 0041 section 7); without
        # it no frame settles on a guardian verdict.
        self._guardian_reader = guardian_reader
        # (input, source stage) of the resolver refusal found by ``_late_bound_refusal``.
        self._refused_inputs: dict[str, tuple[str | None, str | None]] = {}
        # ADR 0212: per attempt, when control may next read the guardian's final, and the code
        # of the last read (repeated while throttled, so the held code stays stable).
        self._final_refresh_at: dict[str, datetime] = {}
        self._final_refresh_code: dict[str, str | None] = {}

    # -- small helpers --------------------------------------------------------------

    def profile(self, spec_stage: StageSpec) -> SaladCampaignProfile:
        profiles = (
            self._profiles
            if self._profiles is not None
            else getattr(self.runner, "salad_profiles", None)
        )
        found = None if profiles is None else profiles.get(salad_config(spec_stage).profile)
        if (
            found is None
            or found.worker_principal_id != salad_config(spec_stage).worker_principal_id
        ):
            raise StepFailed("salad", "step_refused", "salad_profile_unavailable")
        if not found.image_allowed(spec_stage.image):
            raise StepFailed("salad", "step_refused", "salad_image_not_allowlisted")
        return found

    def _stage_row(self, ctx: _Ctx, key: str) -> dict[str, Any]:
        profile = salad_config(self._spec(ctx, key)).profile
        with self.runner.store.runner_connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO campaign_salad_stages
                (campaign_id, stage_key, profile, revision, updated_at) VALUES (?,?,?,1,?)""",
                (ctx.campaign["id"], key, profile, self._stamp()),
            )
            row = connection.execute(
                "SELECT * FROM campaign_salad_stages WHERE campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
            ).fetchone()
        return dict(row)

    def _spec(self, ctx: _Ctx, key: str) -> StageSpec:
        return self.runner._spec_stage(ctx, key)

    def _stamp(self) -> str:
        from .campaign_runner import _iso

        return _iso(self.runner.now())

    def _set_stage_row(self, ctx: _Ctx, key: str, **fields: Any) -> None:
        with self.runner.store.runner_connection() as connection:
            self.runner._touch(
                connection,
                "campaign_salad_stages",
                "campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
                **fields,
            )

    def row(self, attempt_id: str) -> dict[str, Any]:
        with self.runner.store.runner_connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO campaign_salad_attempts
                (attempt_id, phase, revision, updated_at) VALUES (?, 'awaiting_lease', 1, ?)""",
                (attempt_id, self._stamp()),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM campaign_salad_attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            )

    def _move(self, attempt_id: str, **fields: Any) -> None:
        with self.runner.store.runner_connection() as connection:
            self.runner._touch(
                connection, "campaign_salad_attempts", "attempt_id=?", (attempt_id,), **fields
            )

    def _emit_once(self, ctx: _Ctx, event_type: str, match: dict[str, Any], payload: dict) -> None:
        """One event per (type, match); the runner re-derives state every tick."""
        clauses = " AND ".join(f"json_extract(payload_json, '$.{name}') IS ?" for name in match)
        with self.runner.store.runner_connection() as connection:
            seen = connection.execute(
                f"""SELECT 1 FROM events WHERE project_id=? AND event_type=?
                AND json_extract(payload_json, '$.campaign_id') = ? AND {clauses} LIMIT 1""",
                (ctx.campaign["project_id"], event_type, ctx.campaign["id"], *match.values()),
            ).fetchone()
            if seen is None:
                self.runner.store.record_runner_event(
                    connection, ctx.campaign, event_type, {**match, **payload}
                )

    def _awaiting_human(
        self, ctx: _Ctx, key: str, attempt: int | None, records: list[str], reason: str
    ) -> None:
        self._emit_once(
            ctx,
            "campaign.stage_awaiting_human",
            {"stage_key": key, "attempt": attempt, "reason": reason},
            {"records": records},
        )

    def _task(self, task_id: str) -> dict[str, Any]:
        row = self.runner._one(
            "SELECT id, revision, status, current_plan_digest FROM tasks WHERE id=?", (task_id,)
        )
        assert row is not None
        return row

    def _grant(self, attempt: dict[str, Any]) -> dict[str, Any]:
        row = self.runner._one("SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],))
        assert row is not None
        return row

    def route_state(self, route_id: str | None) -> dict[str, Any] | None:
        if route_id is None:
            return None
        row = self.runner._one(
            """SELECT payload_json FROM events WHERE event_type='remote_execution.state'
            AND json_extract(payload_json,'$.id')=? ORDER BY cursor DESC LIMIT 1""",
            (route_id,),
        )
        return None if row is None else json.loads(row["payload_json"])

    def claimed(self, ctx: _Ctx, operation_id: str | None) -> bool:
        """True when control holds a provider create or start claim for the operation."""
        return self._claimed(ctx.campaign["org_id"], operation_id)

    def _claimed(self, org_id: str, operation_id: str | None) -> bool:
        with self.runner.store.runner_connection() as connection:
            return create_claimed(connection, org_id, operation_id)

    def reconciled_settlement(self, attempt: dict[str, Any], grant: dict[str, Any]) -> int | None:
        """Cents for a grant someone else reconciled; None means wait for a human charge."""
        found = self.reconciled(attempt, grant)
        return None if found is None else found[0]

    def reconciled(self, attempt: dict[str, Any], grant: dict[str, Any]) -> tuple[int, str] | None:
        """``(cents, basis)`` for a grant someone else reconciled, or None to wait.

        Billed work is never settled at 0 by default. Unclaimed (no create/start claim on any
        operation of the grant) settles at the recorded settlement, else 0. Claimed settles
        at the larger of the recorded settlement and the recorded compute charges; a
        settlement of 0 or NULL with no charge waits, unless the guardian bounds the hold.
        """
        recorded = grant["settlement_cents"]
        campaign = self.runner._campaign(attempt["campaign_id"])
        row = self.runner._one(
            "SELECT * FROM campaign_salad_attempts WHERE attempt_id=?", (attempt["id"],)
        )
        if row is None:
            return None  # no Salad bookkeeping to judge claims by: wait, never raise
        operations = [
            r["operation_id"]
            for r in self.runner._rows(
                "SELECT operation_id FROM external_operations WHERE grant_id=?", (grant["id"],)
            )
        ]
        claimed = any(
            self._claimed(campaign["org_id"], operation)
            for operation in (row["operation_id"], *operations)
        )
        if not claimed:
            if recorded is not None and int(recorded) > 0:
                return int(recorded), "human_reconcile"
            return 0, "unclaimed_zero"
        count, total = self.charges(attempt)
        if count and total >= int(recorded or 0):
            return total, "human_charge"
        if recorded is not None and int(recorded) > 0:
            return int(recorded), "human_reconcile"
        bounded = self.bounded_hold(attempt, row)
        if bounded is not None and self._read_verdict(attempt, row):
            return bounded, "guardian_bounded_hold"
        if self._guardian_overrun(attempt, row):
            self._block_overrun(attempt)
            return None
        self._await_charge(attempt)
        return None

    def _operator_config_problem(self, spec_stage: StageSpec) -> str | None:
        """Refusal code when the operator's files no longer match this process, else None."""
        name = salad_config(spec_stage).profile
        try:
            profile = self.profile(spec_stage)
        except StepFailed:
            profile = None  # refused later with its own reason
        return operator_config_freshness.launch_problem(name, profile)

    def _checkpoint_bytes(self, ctx: _Ctx, stage: dict[str, Any]) -> int | None:
        """Bytes of the checkpoint the stage's current input snapshot was composed with, from the
        receipt recorded when it was composed (None: the base composition). Read once per
        composed snapshot, not per call."""
        snapshot_id = stage.get("input_snapshot_id")
        if not snapshot_id:
            return None
        cache_key = (ctx.campaign["id"], stage["stage_key"], snapshot_id)
        if cache_key not in self._checkpoint_cache:
            with self.runner.store.runner_connection() as connection:
                found = composed_checkpoint(
                    connection, ctx.campaign, stage["stage_key"], snapshot_id
                )
            self._checkpoint_cache[cache_key] = None if found is None else found.bytes
        return self._checkpoint_cache[cache_key]

    def _workload(self, ctx: _Ctx, stage: dict[str, Any], spec_stage: Any) -> Any:
        """``stage_workload`` for this attempt: bound to the checkpoint-composed snapshot, with
        the cap raised by the checkpoint's bytes, when the current snapshot was composed with
        one (ADR 0197, ADR 0211)."""
        return stage_workload(
            stage, spec_stage, checkpoint_bytes=self._checkpoint_bytes(ctx, stage)
        )

    def retry_input_refusal(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: Any
    ) -> str | None:
        """A closed reason when a checkpoint retry's raised input cap no longer fits the operator
        profile (``max_input_bytes`` of the stage adapter, and the declared fetch plus input bytes
        within ``disk_mb``), else None. Checked right after the retry composes, before any lease
        or hold, so the stage fails with the checkpoint named instead of after packaging."""
        if not self._checkpoint_bytes(ctx, stage):
            return None
        contract = self._workload(ctx, stage, spec_stage)
        if contract.input is None:
            return None
        profile = self.profile(spec_stage)
        cap = contract.input.max_bytes
        ceiling = getattr(getattr(profile, "stage_adapter", None), "max_input_bytes", None)
        name = stage_checkpoint_name(spec_stage)
        if ceiling is not None and cap > ceiling:
            return (
                f"{CHECKPOINT_OVER_CAP}: checkpoint {name!r} makes the input {cap} bytes, over "
                f"the profile's max_input_bytes {ceiling}"
            )
        if contract.egress.declared_bytes() + cap > profile.disk_mb * MIB:
            return (
                f"{CHECKPOINT_OVER_CAP}: checkpoint {name!r} makes the input {cap} bytes, over "
                f"profile disk_mb {profile.disk_mb} with the declared fetches"
            )
        return None

    def _selection(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        row: dict[str, Any],
        *,
        closed: bool = True,
    ) -> ExecutionSelection:
        """The route's selection. A reviewed contract or operator profile that cannot form a
        valid one never will on a retry, so before the route starts (``closed``) that is a
        closed stage failure, not a raw error the runner would retry forever."""
        try:
            return derive_selection(
                self.profile(spec_stage),
                self._workload(ctx, stage, spec_stage),
                operation_id=row["operation_id"],
                epoch=row["epoch"],
                snapshot_digest=stage_data_digest(stage, spec_stage),
                image=spec_stage.image,
                max_cost_cents=spec_stage.max_exposure_cents,
                plan_max_seconds=spec_stage.max_duration_seconds,
                data_policy=self._data_policy(stage, spec_stage),
            )
        except ValueError as error:  # a ValidationError is one: the contract or selection is bad
            if not closed:
                raise
            raise StepFailed("salad", "step_refused", SELECTION_INVALID) from error

    def _started_selection(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: StageSpec, row: dict[str, Any]
    ) -> ExecutionSelection | None:
        """The selection of a route that already started: the one it was proposed with (what
        was approved and started), else a fresh derivation, else None. Never raises for a bad
        contract or profile: a replay after start only finishes bookkeeping."""
        state = self.route_state(row.get("route_id"))
        proposed = ((state or {}).get("proposal") or {}).get("selection")
        if isinstance(proposed, dict):
            try:
                return ExecutionSelection.model_validate(proposed)
            except ValueError:
                pass
        try:
            return self._selection(ctx, stage, spec_stage, row, closed=False)
        except (ValueError, StepFailed):
            return None

    def _data_policy(self, stage: dict[str, Any], spec_stage: StageSpec) -> str:
        """The route literal: public only when the declared classes and the admitted input
        (recorded public, no code approval) are public (ADR 0040 section 4)."""
        policy = stage_data_policy(spec_stage)
        snapshot_id = stage.get("input_snapshot_id")
        if policy == DATA_POLICY_PUBLIC and snapshot_id:
            with self.runner.store.runner_connection() as connection:
                if provider_trust.admission_basis(connection, snapshot_id) != "public_only":
                    return DATA_POLICY_TRUST
        return policy

    # -- stage scaffold (before the first reservation) ------------------------------

    def _credential_state(self, spec_stage: StageSpec) -> dict[str, Any] | None:
        """The provider's metadata-only credential state for the stage's profile, or None when
        the provider cannot say (no credential directory, older provider): never blocks."""
        from .campaign_providers import get_provider

        try:
            provider = get_provider("salad")
            state_of = getattr(provider, "credential_state", None)
            if state_of is None:
                return None
            state = state_of(self.profile(spec_stage))
        except Exception:  # noqa: BLE001 - the early guard never fails a stage by itself
            return None
        return state if isinstance(state, dict) else None

    def scaffold(self, ctx: _Ctx, key: str) -> str:
        """``ready``, ``progress`` (a step ran) or ``waiting`` (a human must act)."""
        spec_stage = self._spec(ctx, key)
        salad = salad_config(spec_stage)
        row = self._stage_row(ctx, key)
        project = ctx.campaign["project_id"]
        problem = self._operator_config_problem(spec_stage)
        if problem is not None:
            # The operator's provider files changed after this process read them, or the
            # executor's copy of the profile differs: refuse before any step, hold or
            # reservation rather than place on a selection the operator no longer names.
            self.runner.emit_once(
                ctx,
                "campaign.stage_operator_profile_mismatch"
                if problem == operator_config_freshness.MISMATCH_CODE
                else "campaign.stage_operator_config_stale",
                {"stage_key": key},
                {
                    "reason": problem,
                    "detail": "the operator provider files changed since this process "
                    "started, or differ from the executor's profiles; fix them and "
                    "restart tessera serve",
                },
            )
            self.runner._stage_failed(ctx, key, problem)
            return "waiting"
        state = self._credential_state(spec_stage)
        if credential_blocked(state):
            # Refused up front, before any step, hold or reservation, with the reason on the
            # campaign (stop_reason) and an event, instead of failing deep in the run.
            self.runner.emit_once(
                ctx,
                "campaign.stage_credential_blocked",
                {"stage_key": key},
                _credential_event(state),
            )
            self.runner._stage_failed(ctx, key, credential_code(state))
            return "waiting"
        if isinstance(state, dict) and state.get("state") == "warning":
            self.runner.emit_once(
                ctx,
                "campaign.stage_credential_warning",
                {"stage_key": key},
                _credential_event(state),
            )
        if row["study_id"] is None:
            result = self.runner._step(
                ctx,
                key,
                None,
                "create_study",
                0,
                lambda: (
                    {"project_id": project},
                    {
                        "purpose": (
                            f"Operational Salad GPU stage {key} of campaign "
                            f"{ctx.campaign['id']}, outcome {salad.program.outcome}; "
                            "not a scientific study"
                        )[:4000],
                        "protocol": (
                            "Admitted attempts of one pre-approved stage with quarantined output; "
                            "route approval is not a scientific result."
                        ),
                    },
                ),
            )
            self._set_stage_row(ctx, key, study_id=result.response["id"])
            return "progress"
        if row["source_id"] is None:
            template = salad.program.source
            result = self.runner._step(
                ctx,
                key,
                None,
                "create_study_source",
                0,
                lambda: (
                    {"study_id": row["study_id"]},
                    template.model_dump(mode="json", exclude_none=True),
                ),
            )
            self._set_stage_row(ctx, key, source_id=result.response["id"])
            return "progress"
        source = self.runner._one(
            "SELECT verdict FROM research_sources WHERE id=?", (row["source_id"],)
        )
        if source is None or source["verdict"] == "unverified":
            if source is not None and self._delegate_source(ctx, key, row):
                return "progress"
            self._awaiting_human(
                ctx,
                key,
                None,
                [row["source_id"]],
                "the source assessment verdict is a human review; assess source "
                f"{row['source_id']} as supported (ADR 0038 amendment, not delegated)",
            )
            return "waiting"
        if source["verdict"] != "supported":
            self.runner._stage_failed(ctx, key, "source_not_supported")
            return "waiting"
        return "ready"

    def experiment_ready(self, ctx: _Ctx, stage: dict[str, Any], spec_stage: StageSpec) -> bool:
        """Study variant and split lock for the stage experiment; False after a step ran."""
        key = stage["stage_key"]
        refusal = self._late_bound_refusal(ctx, stage, spec_stage)
        if refusal is not None:
            self._refuse_late_bound(ctx, key, refusal, spec_stage)
            return False
        row = self._stage_row(ctx, key)
        contract = self._workload(ctx, stage, spec_stage)
        if row["study_variant_id"] is None:
            code = derived_digests(self.profile(spec_stage), contract, spec_stage.image)
            result = self.runner._step(
                ctx,
                key,
                None,
                "register_study_variant",
                0,
                lambda: (
                    {"study_id": row["study_id"]},
                    {
                        "name": f"{key} baseline"[:200],
                        "method_digest": code["code_digest"],
                        "input_digest": stage_data_digest(stage, spec_stage),
                        "metadata": {"experiment_id": stage["experiment_id"]},
                    },
                ),
            )
            self._set_stage_row(ctx, key, study_variant_id=result.response["id"])
            return False
        if row["split_lock_id"] is None:
            member = f"{ctx.campaign['id']}/{key}/{stage_data_digest(stage, spec_stage)}"[:200]
            result = self.runner._step(
                ctx,
                key,
                None,
                "create_split_lock",
                0,
                lambda: (
                    {"project_id": ctx.campaign["project_id"]},
                    {
                        "name": f"{key} operational"[:200],
                        "partitions": {PARTITION: {"keys": [member]}},
                    },
                ),
            )
            self._set_stage_row(ctx, key, split_lock_id=result.response["id"])
            return False
        return True

    # -- late-bound stage-output inputs ------------------------------------------------

    def _late_bound_refusal(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: StageSpec
    ) -> str | None:
        """None when the stage has no late-bound input or every one checks out, else a closed
        refusal code. Runs once the runner has composed the input snapshot and before any
        study step, review or route, so nothing reaches Salad for a refused input.

        Each stage-output input is resolved by the core resolver from the upstream stage's
        recorded, settled outputs; the provider instance must admit each resolved output's
        recorded data class and inherited licence labels (ADR 0040 section 4, re-checked after
        resolution); and the composed snapshot the route will name must hold exactly the
        resolved bytes (same sha256 and size at ``<input>/<path>``) within the reviewed
        ``max_bytes``. On success the resolved inputs are recorded once per bound snapshot."""
        salad = salad_config(spec_stage)
        if not salad.adapted or not is_late_bound(spec_stage, salad.workload):
            return None
        key = stage["stage_key"]
        snapshot_id, digest = stage.get("input_snapshot_id"), stage.get("input_snapshot_digest")
        if not snapshot_id or not digest:
            return LATE_BIND_UNRESOLVED
        try:
            from . import late_bound_inputs as resolver
        except ImportError:  # pragma: no cover - the core module ships with the runner
            return LATE_BIND_RESOLVER
        campaign = ctx.campaign
        with self.runner.store.runner_connection() as connection:
            try:
                earlier = resolver.bound_inputs(connection, campaign, key)
                resolved = resolver.resolve_stage_inputs(
                    connection,
                    campaign["id"],
                    spec_stage,
                    org_id=campaign["org_id"],
                    project_id=campaign["project_id"],
                    expected={(item.name, item.output): item.digest for item in earlier},
                )
                resolver.check_resolved_routing(
                    connection,
                    spec_stage,
                    resolved,
                    worker=salad.worker_principal_id,
                    approval_digest=(ctx.approval or {}).get("trust_policy_digest"),
                )
            except resolver.LateBindRefused as error:
                self._refused_inputs[key] = (error.input_name, error.source_stage)
                return error.code
            snapshot = connection.execute(
                "SELECT digest, manifest_json FROM source_snapshots WHERE id=? AND project_id=?",
                (snapshot_id, campaign["project_id"]),
            ).fetchone()
        if snapshot is None or snapshot["digest"] != digest:
            return LATE_BIND_MISMATCH
        files = {
            entry["relative_path"]: entry
            for entry in json.loads(snapshot["manifest_json"])["entries"]
        }
        wanted = {
            f"{item.name}/{path}"
            for item in spec_stage.inputs
            if getattr(item, "kind", None) == "stage"
            for path in item.paths
        }
        seen: set[str] = set()
        for item in resolved:
            path = f"{item.name}/{item.path}"
            entry = files.get(path)
            if entry is None:
                return LATE_BIND_MISSING
            if entry["sha256"] != item.digest or int(entry["size"]) != item.bytes:
                return LATE_BIND_MISMATCH
            seen.add(path)
        if wanted - seen:
            return LATE_BIND_MISSING
        total = sum(int(entry["size"]) for entry in files.values())
        if total > salad.workload.input.max_bytes:
            return LATE_BIND_OVER_CAP
        self.runner.emit_once(
            ctx,
            LATE_BOUND_EVENT,
            {"stage_key": key, "snapshot_digest": digest},
            {
                "snapshot_bytes": total,
                "max_bytes": salad.workload.input.max_bytes,
                "inputs": [
                    {
                        "name": item.name,
                        "source_stage": item.source_stage,
                        "output": item.output,
                        "digest": item.digest,
                        "bytes": item.bytes,
                        "data_class": item.data_class,
                        "license_labels": list(item.license_labels),
                    }
                    for item in resolved
                ],
            },
        )
        return None

    def _refuse_late_bound(self, ctx: _Ctx, key: str, code: str, spec_stage: StageSpec) -> None:
        """Fail the stage with the closed code: no unstarted attempt keeps its reservation and
        no step, review or route is made for it."""
        for attempt in self.runner._attempts(ctx, key):
            if attempt["status"] == "reserved" and attempt["grant_id"] is None:
                self.runner._cancel_unstarted(ctx, attempt)
        input_name, source_stage = self._refused_inputs.pop(key, (None, None))
        # One transaction: the Salad event and the runner's refusal event (the receipt's single
        # source of truth; same shape as the runner's, closed code only, no free-text reason).
        with self.runner.store.runner_connection() as connection:
            self.runner._emit_once_in(
                connection, ctx, LATE_BIND_REFUSED_EVENT, {"stage_key": key}, {"code": code}
            )
            self.runner._emit_once_in(
                connection,
                ctx,
                BIND_REFUSED_EVENT,
                {"stage_key": key},
                {
                    "code": code,
                    "input": input_name,
                    "source_stage": source_stage,
                    "provider": "salad",
                    "worker_principal_id": salad_config(spec_stage).worker_principal_id,
                },
            )
        self.runner._stage_failed(ctx, key, code)

    # -- attempt lifecycle ------------------------------------------------------------

    def admitted(self, attempt_id: str) -> None:
        """Called when admission completes: the executor may now lease this grant."""
        self.row(attempt_id)

    def advance(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: StageSpec, attempt: dict[str, Any]
    ) -> bool:
        attempt = self.adopt_started(ctx, stage["stage_key"], attempt)
        row = self.row(attempt["id"])
        phase = row["phase"]
        grant = self._grant(attempt)
        expired = self.runner.now() >= _parse_utc(grant["expires_at"])
        if phase == "awaiting_lease":
            if expired:
                return self.runner._do_observe(ctx, stage, spec_stage, attempt)
            raise _waiting()()
        lapsed = lease_lapse.before_handler(self, ctx, attempt, row)
        if lapsed is not None:
            # Worker authority lapsed before the route (a long review wait): requeued as not
            # started within ADR 0046's repeat bound, never a permanent stage failure.
            return lease_lapse.requeue(self, ctx, attempt, lapsed)
        if phase in PRE_ROUTE_PHASES and (expired or grant["status"] != "active"):
            return self._abandon(ctx, stage, spec_stage, attempt, "grant_expired_before_route")
        if phase == "route_started" and self._held_by_sibling_stop(ctx, attempt, row):
            return self._abandon(ctx, stage, spec_stage, attempt, STAGE_FAILED_STOP)
        handler = {
            "leased": self._propose_decisions,
            "awaiting_review": self._await_review,
            "reviewed": self._capability,
            "capability_ready": self._propose_route,
            "route_proposed": self._approve_and_start,
            "route_started": self._await_outcome,
            "executing": self._await_outcome,
            "outcome_recorded": self._verify,
        }.get(phase)
        if handler is None:
            raise _waiting()()
        try:
            return handler(ctx, stage, spec_stage, attempt, row)
        except StepFailed as error:
            # A pre-route refusal of the worker's own authority (disconnected worker, lapsed
            # lease): control's own Conflict, confirmed from the store; nothing was sent, so it
            # is requeued, not permanent.
            lapsed = lease_lapse.after_refusal(self, ctx, attempt, row, error)
            if lapsed is None:
                raise
            return lease_lapse.requeue(self, ctx, attempt, lapsed)

    # decisions ----------------------------------------------------------------------

    def _plan_scope(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        row: dict[str, Any],
        context: dict[str, Any],
        salad_row: dict[str, Any],
        preview: bool = False,
    ) -> dict[str, Any]:
        salad = salad_config(spec_stage)
        profile = self.profile(spec_stage)
        template = salad.program
        selection = self._selection(ctx, stage, spec_stage, row, closed=not preview)
        try:
            digests = derived_digests(
                profile, self._workload(ctx, stage, spec_stage), spec_stage.image
            )
        except ValueError as error:  # a ValidationError is one: the contract is bad
            if preview:
                raise
            raise StepFailed("salad", "step_refused", SELECTION_INVALID) from error
        max_seconds = selection.resources.max_seconds
        gate = template.gate
        return {
            "type": "research_program",
            "version": 1,
            "study_id": salad_row["study_id"],
            "outcome": template.outcome,
            "action": "plan",
            "basis_digest": context["basis_digest"],
            "supersedes_id": self._head(context, template.outcome, "plan"),
            "reason": f"Campaign {ctx.campaign['id']} stage {stage['stage_key']} Salad GPU run",
            "brief": {
                **template.brief.model_dump(mode="json"),
                "data_source_ids": [salad_row["source_id"]],
                "compute": {
                    "budget_cents": spec_stage.max_exposure_cents,
                    "max_wall_seconds": max_seconds,
                    "max_gpu_hours": round(max_seconds / 3600, 6),
                    "target": SALAD_TARGET,
                    "resources": (
                        "One Salad GPU container, exact reviewed class; "
                        + salad_route_window_description(
                            max_seconds,
                            self._workload(ctx, stage, spec_stage).workload_seconds,
                            profile.route_max_seconds,
                        )
                    ),
                },
                "missing_prerequisites": [],
            },
            "conditions": {
                CONDITION: {
                    "disposition": "selected",
                    "rationale": "The declared stage run is the only condition in scope",
                    "variant_ids": [salad_row["study_variant_id"]],
                }
            },
            "evaluation_protocol": {
                "version": 2,
                "identity": {
                    "data_digest": stage_data_digest(stage, spec_stage),
                    "code_digest": digests["code_digest"],
                    "environment_digest": digests["environment_digest"],
                },
                "gates": [
                    {
                        "id": f"{template.outcome}_gate"[:200],
                        "category": gate.category,
                        "variant_id": salad_row["study_variant_id"],
                        "experiment_variant_id": stage["variant_id"],
                        "metric": gate.metric,
                        "unit": gate.unit,
                        "split_lock_id": salad_row["split_lock_id"],
                        "split_partition": PARTITION,
                        "split_digest": (
                            "0" * 64 if preview else self._lock_digest(salad_row["split_lock_id"])
                        ),
                        "procedure": gate.procedure,
                        "comparison": gate.comparison,
                        "threshold": gate.threshold,
                        "statistic": gate.statistic,
                        "minimum_replicates": gate.minimum_replicates,
                        "minimum_samples": gate.minimum_samples,
                        "allowed_sources": list(gate.allowed_sources),
                    }
                ],
            },
        }

    def _source_use_scope(
        self, ctx: _Ctx, spec_stage: StageSpec, salad_row: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        outcome = salad_config(spec_stage).program.outcome
        return {
            "type": "research_program",
            "version": 1,
            "study_id": salad_row["study_id"],
            "outcome": outcome,
            "action": "source_use",
            "basis_digest": context["basis_digest"],
            "supersedes_id": self._head(context, outcome, "source_use"),
            "reason": f"Campaign {ctx.campaign['id']} stage {spec_stage.key} Salad GPU run",
            "source_ids": [salad_row["source_id"]],
            "intended_use": "Workload provenance for one operational stage run only",
        }

    def preview_review_digests(
        self, ctx: _Ctx, key: str, provider_id: str | None = None
    ) -> dict[str, str]:
        """The three digests a human binds in ``preapprovals`` for this stage (ADR 0044).

        Read-only: builds the scopes with placeholder run-minted ids through the same code
        the runner uses to propose them, then digests the cores. ``provider_id`` names a Salad
        alternate of the stage (ADR 0066); absent, the stage's own provider."""
        spec_stage = ctx.spec.stage(key)
        if provider_id is not None:
            spec_stage = stage_on_candidate(spec_stage, provider_id)
        salad = salad_config(spec_stage)
        # A stage may have no snapshot input (only public_artifact inputs, ADR 0097).
        snapshot = next((i for i in spec_stage.inputs if i.kind == "snapshot"), None)
        preview_digest = None if snapshot is None else snapshot.digest
        # An adapted stage's reviewed contract declares the staged input the runner composes and
        # binds as the route's data manifest; that digest, not the first ``snapshot`` input (which
        # may be the code snapshot), is what the route carries. A non-adapted stage is unchanged.
        declared = getattr(salad.workload, "input", None) if salad.adapted else None
        if declared is not None:
            preview_digest = declared.snapshot_digest
        placeholder = {
            "study_id": "study_preview",
            "source_id": "source_preview",
            "study_variant_id": "variant_preview",
            "split_lock_id": "lock_preview",
        }
        context = {"basis_digest": "0" * 64, "reviews": []}
        row = {"operation_id": "operation-preview", "epoch": 1}
        stage = {
            "stage_key": key,
            "input_snapshot_digest": preview_digest,
            "variant_id": "variant_preview",
        }
        plan = self._plan_scope(ctx, stage, spec_stage, row, context, placeholder, preview=True)
        use = self._source_use_scope(ctx, spec_stage, placeholder, context)
        template = salad.program.source.model_dump(mode="json")
        return {
            "stage_key": key,
            **({} if provider_id is None else {"provider": provider_id}),
            "source_assessment_digest": campaign_preapproval.source_core_digest(
                {
                    "uri": template["uri"],
                    "quoted_passage": template["quoted_passage"],
                    "content_digest": template["content_digest"],
                    "locator": template.get("locator"),
                }
            ),
            "plan_digest": campaign_preapproval.review_core_digest(plan),
            "source_use_digest": campaign_preapproval.review_core_digest(use),
        }

    def _lock_digest(self, lock_id: str) -> str:
        row = self.runner._one(
            "SELECT membership_digest FROM gate_split_locks WHERE id=?", (lock_id,)
        )
        assert row is not None
        return row["membership_digest"]

    @staticmethod
    def _head(context: dict[str, Any], outcome: str, action: str) -> str | None:
        heads = [
            r["id"]
            for r in context["reviews"]
            if r["scope"]["outcome"] == outcome and r["scope"]["action"] == action
        ]
        return heads[-1] if heads else None

    def _context(self, ctx: _Ctx, study_id: str) -> dict[str, Any]:
        with self.runner.store.runner_connection() as connection:
            return program_context(
                connection, {"org_id": ctx.campaign["org_id"], "project_id": None}, study_id
            )

    def _propose_decisions(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        row: dict[str, Any],
    ) -> bool:
        key = stage["stage_key"]
        assert ctx.approval is not None
        salad_row = self._stage_row(ctx, key)
        grant = self._grant(attempt)
        approver = ctx.approval["approving_principal_id"]
        expires = min(
            _parse_utc(grant["expires_at"]), self.runner.now() + timedelta(hours=DECISION_HOURS)
        )
        from .campaign_runner import _iso

        def build(action: str):
            def make() -> tuple[dict[str, str], dict[str, Any]]:
                context = self._context(ctx, salad_row["study_id"])
                if action == "plan":
                    scope = self._plan_scope(ctx, stage, spec_stage, row, context, salad_row)
                    kind = "direction"
                else:
                    scope = self._source_use_scope(ctx, spec_stage, salad_row, context)
                    kind = "access"
                return {}, {
                    "project_id": ctx.campaign["project_id"],
                    "kind": kind,
                    "scope": scope,
                    "expires_at": _iso(expires),
                    "reviewer_principal_id": approver,
                }

            return make

        if row["plan_decision_id"] is None:
            result = self.runner._step(
                ctx, key, attempt["attempt"], "propose_decision", 0, build("plan")
            )
            self._move(attempt["id"], plan_decision_id=result.response["id"])
            return True
        if row["source_decision_id"] is None:
            result = self.runner._step(
                ctx, key, attempt["attempt"], "propose_decision", 1, build("source_use")
            )
            self._move(attempt["id"], source_decision_id=result.response["id"])
            return True
        self._move(attempt["id"], phase="awaiting_review")
        with self.runner.store.runner_connection() as connection:
            carried = review_carryover.available(
                connection, ctx.campaign["id"], key, attempt["attempt"]
            )
        if self._preapproval(ctx, key) is None and not carried:
            self._awaiting_human(
                ctx,
                key,
                attempt["attempt"],
                [row["plan_decision_id"], row["source_decision_id"]],
                REVIEW_REASON,
            )
        return True

    # review pre-approval (ADR 0044) -----------------------------------------------------

    def _preapproval(self, ctx: _Ctx, key: str) -> dict[str, Any] | None:
        """The stage's explicit approve-body pre-approval (ADR 0044), if any. A bare approval
        never implies one, and an operator profile can only narrow: ``preapprove_reviews:
        false`` ignores the entry and restores the human stop."""
        if ctx.approval is None:
            return None
        with self.runner.store.runner_connection() as connection:
            found = campaign_preapproval.stage_preapproval(connection, ctx.approval["id"], key)
            # ADR 0150: the delegation the campaign was approved under, or the one the
            # approving owner bound to this approval's reviews (amendment 2026-11-25).
            delegated = campaign_preapproval._delegation_of(ctx.approval, connection)
        if found is None and not delegated:
            return None
        try:
            if not self.profile(self._spec(ctx, key)).preapprove_reviews:
                return None
        except StepFailed:
            return None
        return found if found is not None else self._delegated_preapproval(ctx, key)

    def _delegated_preapproval(self, ctx: _Ctx, key: str) -> dict[str, Any] | None:
        """ADR 0150: at the stage's first review stop, record the review pre-approval the
        owner-signed delegation this campaign was approved under writes for the stage, bound to
        the digests this driver derives from the approved spec. Anything outside the
        delegation records nothing and leaves the stop with a person, with the reasons on a
        ``campaign.preapproval_declined`` event."""
        assert ctx.approval is not None
        with self.runner.store.runner_connection() as connection:
            delegation_id = campaign_preapproval._delegation_of(ctx.approval, connection)
        failure = None
        placed = self._placed_alternate(ctx, key)
        try:
            digests = self.preview_review_digests(ctx, key, placed)
            alternates = self._alternate_profile_digests(ctx, key, placed)
        except Exception as error:  # noqa: BLE001 - a stage the driver cannot preview is not covered
            digests, failure = None, _preview_failure(error)
        if digests is None:
            row, reasons = None, [failure or PREVIEW_FAILED]
        else:
            # The candidate the digests were derived for (ADR 0066). Resolved before the write
            # transaction: _spec reads the fallback stage through its own runner_connection(),
            # and a second BEGIN IMMEDIATE inside this one deadlocks on the same thread.
            provider_id = placed or self._spec(ctx, key).provider
            with self.runner.store.runner_connection() as connection:
                row, reasons = campaign_preapproval.record_delegated_preapproval(
                    connection,
                    campaign_id=ctx.campaign["id"],
                    approval_id=ctx.approval["id"],
                    stage_key=key,
                    digests=digests,
                    alternate_profiles=alternates,
                )
                if row is not None and "covered_kinds" in row:
                    self.runner.store.record_runner_event(
                        connection,
                        ctx.campaign,
                        campaign_preapproval.DELEGATED_PREAPPROVAL_EVENT,
                        {
                            "campaign_id": ctx.campaign["id"],
                            "stage_key": key,
                            "provider_id": provider_id,
                            "preapproval_id": row["id"],
                            "delegation_id": row["delegation_id"],
                            "terms_digest": row["terms_digest"],
                            "kinds": row["covered_kinds"],
                            **({"profiles": row["profiles"]} if "profiles" in row else {}),
                            "max_attempt_cents": row["max_attempt_cents"],
                            "max_total_cents": row["max_total_cents"],
                            "expires_at": row["expires_at"],
                        },
                    )
        if row is None:
            self._declined(ctx, key, None, "delegated_review", delegation_id, ",".join(reasons))
        return row

    def _alternate_profile_digests(
        self, ctx: _Ctx, key: str, placed: str | None
    ) -> list[dict[str, str]] | None:
        """The plan digest of each profile the approved spec lists after the candidate's own
        (ADR 0151), derived as the approve-body preview does, the own profile first; None for
        a candidate that lists none. A failure to derive them propagates to the caller's
        preview guard: nothing is recorded then."""
        stage = ctx.spec.stage(key)
        if placed is not None:
            stage = stage_on_candidate(stage, placed)
        found = campaign_preapproval.alternate_profile_digests(
            self, ctx.campaign, ctx.spec, stage
        ).get("alternate_profiles")
        if not found:
            return None
        from .salad_alternate_profiles import covered_profiles

        return [{"profile": covered_profiles(stage)[0], "plan_digest": ""}, *found]

    def _placed_alternate(self, ctx: _Ctx, key: str) -> str | None:
        """The Salad alternate the stage was placed on (ADR 0060/0066), else None for the stage's
        own provider. The reviews a delegation clears are the ones of the candidate that runs,
        so they are previewed on it: the stage's own spec names no Salad configuration when its
        preferred provider is another one and Salad is only its alternate."""
        placed = self._spec(ctx, key).provider
        return None if placed == ctx.spec.stage(key).provider else placed

    def _declined(
        self, ctx: _Ctx, key: str, attempt: int | None, kind: str, subject: str, reason: str
    ) -> None:
        self._emit_once(
            ctx,
            "campaign.preapproval_declined",
            {"stage_key": key, "attempt": attempt, "kind": kind, "subject_id": subject},
            {"reason": reason},
        )

    def _delegate_source(self, ctx: _Ctx, key: str, row: dict[str, Any]) -> bool:
        """Resolve the source assessment under an exact-digest pre-approval, else False."""
        pre = self._preapproval(ctx, key)
        if pre is None:
            return False
        source_id = row["source_id"]
        assert ctx.approval is not None
        with self.runner.store.runner_connection() as connection:
            verdict = campaign_preapproval.evaluate(
                connection,
                campaign_id=ctx.campaign["id"],
                approval_id=ctx.approval["id"],
                stage_key=key,
                kind=campaign_preapproval.KIND_SOURCE,
                subject_id=source_id,
            )
            revision = connection.execute(
                "SELECT revision FROM research_sources WHERE id=?", (source_id,)
            ).fetchone()["revision"]
        if verdict.reason is not None:
            self._declined(ctx, key, None, "source_assessment", source_id, verdict.reason)
            return False
        try:
            self.runner._step(
                ctx,
                key,
                None,
                "assess_source",
                0,
                lambda: (
                    {"source_id": source_id, "preapproval_id": pre["id"]},
                    {
                        "expected_revision": revision,
                        "verdict": "supported",
                        "reason": (
                            "Delegated under review pre-approval "
                            f"{pre['id']} (exact source digest {verdict.digest})"
                        )[:4000],
                    },
                ),
            )
        except StepFailed as error:
            self._declined(
                ctx, key, None, "source_assessment", source_id, f"step_{error.error_class}"
            )
            return False
        return True

    def _delegate_reviews(
        self, ctx: _Ctx, key: str, attempt: dict[str, Any], row: dict[str, Any]
    ) -> None:
        """Approve this attempt's plan and source-use reviews under a pre-approval; any
        decline leaves the review pending for a human."""
        self._carry_reviews(ctx, key, attempt, row)
        pre = self._preapproval(ctx, key)
        if pre is None:
            return
        assert ctx.approval is not None
        for ordinal, (kind, column) in enumerate(
            (
                (campaign_preapproval.KIND_PLAN, "plan_decision_id"),
                (campaign_preapproval.KIND_SOURCE_USE, "source_decision_id"),
            )
        ):
            decision_id = row[column]
            with self.runner.store.runner_connection() as connection:
                current = connection.execute(
                    "SELECT status, revision FROM decision_requests WHERE id=?", (decision_id,)
                ).fetchone()
                if current is None or current["status"] != "pending":
                    continue
                verdict = campaign_preapproval.evaluate(
                    connection,
                    campaign_id=ctx.campaign["id"],
                    approval_id=ctx.approval["id"],
                    stage_key=key,
                    kind=kind,
                    subject_id=decision_id,
                )
            if verdict.reason is not None:
                self._declined(ctx, key, attempt["attempt"], kind, decision_id, verdict.reason)
                continue
            try:
                self.runner._step(
                    ctx,
                    key,
                    attempt["attempt"],
                    "resolve_decision",
                    ordinal,
                    _resolve_build(
                        decision_id, current["revision"], kind, verdict.digest, pre["id"]
                    ),
                )
            except StepFailed as error:
                self._declined(
                    ctx, key, attempt["attempt"], kind, decision_id, f"step_{error.error_class}"
                )

    def _carry_reviews(
        self, ctx: _Ctx, key: str, attempt: dict[str, Any], row: dict[str, Any]
    ) -> None:
        """Resolve this attempt's pending reviews on the preceding attempt's approval when it
        was requeued for a pre-route lapse and the review is exactly the one approved
        (``campaign_review_carryover``); anything else stays pending for a person."""
        for ordinal, (kind, column) in enumerate(
            (("plan", "plan_decision_id"), ("source_use", "source_decision_id")),
            start=CARRYOVER_ORDINAL,
        ):
            decision_id = row[column]
            with self.runner.store.runner_connection() as connection:
                current = connection.execute(
                    "SELECT status, revision FROM decision_requests WHERE id=?", (decision_id,)
                ).fetchone()
                if current is None or current["status"] != "pending":
                    continue
                verdict = review_carryover.evaluate(
                    connection,
                    campaign_id=ctx.campaign["id"],
                    stage_key=key,
                    kind=kind,
                    subject_id=decision_id,
                )
            if verdict.reason in (
                review_carryover.NO_PRIOR,
                review_carryover.PRIOR_NOT_LAPSED,
                review_carryover.NOT_PENDING,
            ):
                continue  # not a requeued attempt's review: nothing to carry
            if verdict.reason is not None or verdict.prior_decision_id is None:
                self._carryover_declined(
                    ctx, key, attempt["attempt"], kind, decision_id, str(verdict.reason)
                )
                continue
            try:
                self.runner._step(
                    ctx,
                    key,
                    attempt["attempt"],
                    "resolve_decision",
                    ordinal,
                    _carryover_build(
                        decision_id,
                        current["revision"],
                        kind,
                        verdict.prior_decision_id,
                        verdict.prior_attempt,
                    ),
                )
            except StepFailed as error:
                # Refused at use time: the review waits for a person, as before.
                self._carryover_declined(
                    ctx, key, attempt["attempt"], kind, decision_id, f"step_{error.error_class}"
                )

    def _carryover_declined(
        self, ctx: _Ctx, key: str, attempt: int | None, kind: str, subject: str, reason: str
    ) -> None:
        self._emit_once(
            ctx,
            review_carryover.DECLINED_EVENT,
            {"stage_key": key, "attempt": attempt, "kind": kind, "subject_id": subject},
            {"reason": reason},
        )

    def _await_review(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        row: dict[str, Any],
    ) -> bool:
        salad_row = self._stage_row(ctx, stage["stage_key"])
        self._delegate_reviews(ctx, stage["stage_key"], attempt, row)
        context = self._context(ctx, salad_row["study_id"])
        reviews = {r["id"]: r for r in context["reviews"]}
        now = self.runner.now()
        states = []
        for decision_id in (row["plan_decision_id"], row["source_decision_id"]):
            review = reviews.get(decision_id)
            decision = self.runner._one(
                "SELECT status, expires_at FROM decision_requests WHERE id=?", (decision_id,)
            )
            if review is None or decision is None:
                return self._abandon(ctx, stage, spec_stage, attempt, "decision_missing")
            if decision["status"] in {"rejected", "expired"} or (
                decision["status"] == "pending" and now >= _parse_utc(decision["expires_at"])
            ):
                return self._abandon(ctx, stage, spec_stage, attempt, "decision_not_approved")
            if decision["status"] == "approved" and not review["basis_current"]:
                return self._abandon(ctx, stage, spec_stage, attempt, "review_basis_stale")
            states.append(decision["status"] == "approved" and review["is_effective_review"])
        if not all(states):
            with self.runner.store.runner_connection() as connection:
                carried = review_carryover.available(
                    connection, ctx.campaign["id"], stage["stage_key"], attempt["attempt"]
                )
            # Whoever skipped the review notice at proposal (a pre-approval, an offered
            # carryover) and did not resolve both reviews now hands them to a person.
            if self._preapproval(ctx, stage["stage_key"]) is not None or carried:
                self._awaiting_human(
                    ctx,
                    stage["stage_key"],
                    attempt["attempt"],
                    [row["plan_decision_id"], row["source_decision_id"]],
                    REVIEW_REASON,
                )
            raise _waiting()()
        self._move(attempt["id"], phase="reviewed")
        return True

    # capability and route -------------------------------------------------------------

    def _capability(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        row: dict[str, Any],
    ) -> bool:
        key = stage["stage_key"]
        salad_row = self._stage_row(ctx, key)
        profile = self.profile(spec_stage)
        declaration = capability_declaration(
            profile, self._workload(ctx, stage, spec_stage), spec_stage.image
        )
        digest = canonical_digest(declaration)
        existing = (
            self.runner._one(
                "SELECT * FROM capability_policies WHERE id=?", (salad_row["capability_id"],)
            )
            if salad_row["capability_id"]
            else None
        )
        if existing is not None and existing["declaration_digest"] != digest:
            # The stage's capability was declared for another profile (ADR 0151): it never
            # stands in for this one, and a new one would need a plan revision.
            raise StepFailed(
                "request_capability",
                "step_refused",
                "capability declaration differs from the stage's current profile",
            )
        if existing is not None and existing["status"] == "approved":
            if _parse_utc(existing["expires_at"]) <= self.runner.now() + timedelta(minutes=5):
                raise StepFailed(
                    "request_capability",
                    "step_refused",
                    "capability authority expired; a plan revision is required",
                )
            self._move(attempt["id"], phase="capability_ready")
            return True
        if existing is None:
            task = self._task(stage["task_id"])
            result = self.runner._step(
                ctx,
                key,
                None,
                "request_capability",
                0,
                lambda: (
                    {"task_id": stage["task_id"]},
                    {
                        "expected_plan_revision": task["revision"]
                        - (1 if task["status"] == "approved" else 0),
                        "expected_plan_digest": task["current_plan_digest"],
                        "declaration": declaration,
                    },
                ),
            )
            self._set_stage_row(ctx, key, capability_id=result.response["id"])
            return True
        from .campaign_runner import _iso

        expires = _iso(self.runner.now() + timedelta(hours=CAPABILITY_HOURS))
        self.runner._step(
            ctx,
            key,
            None,
            "approve_capability",
            0,
            lambda: (
                {"capability_id": existing["id"]},
                {
                    "expected_revision": existing["revision"],
                    "declaration_digest": digest,
                    "expires_at": expires,
                },
            ),
        )
        return True

    def _propose_route(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        row: dict[str, Any],
    ) -> bool:
        key = stage["stage_key"]
        salad_row = self._stage_row(ctx, key)
        selection = self._selection(ctx, stage, spec_stage, row)
        policy = target_policy(selection)
        task = self._task(stage["task_id"])
        result = self.runner._step(
            ctx,
            key,
            attempt["attempt"],
            "propose_route",
            0,
            lambda: (
                {"task_id": stage["task_id"]},
                {
                    "operation_id": row["operation_id"],
                    "package_digest": row["package_digest"],
                    "attempt_id": attempt["admitted_attempt_id"],
                    "plan_digest": task["current_plan_digest"],
                    "lease_id": row["lease_id"],
                    "epoch": row["epoch"],
                    "selection": selection.model_dump(mode="json"),
                    "study_id": salad_row["study_id"],
                    "program_review_id": row["plan_decision_id"],
                    "source_review_id": row["source_decision_id"],
                },
            ),
            route_policy=policy,
        )
        self._move(
            attempt["id"],
            phase="route_proposed",
            route_id=result.response["id"],
            proposal_digest=result.response["proposal_digest"],
        )
        return True

    def _command(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        attempt: dict[str, Any],
        row: dict[str, Any],
        step: str,
        command: str,
        selection: ExecutionSelection | None,
        policy: TargetPolicy | None = None,
    ) -> None:
        if selection is not None:
            policy = target_policy(selection)

        def build() -> tuple[dict[str, str], dict[str, Any]]:
            state = self.route_state(row["route_id"])
            assert state is not None
            return {"route_id": row["route_id"]}, {
                "expected_revision": state["revision"],
                "command": command,
                "proposal_digest": row["proposal_digest"],
            }

        self.runner._step(
            ctx, stage["stage_key"], attempt["attempt"], step, 0, build, route_policy=policy
        )

    def _approve_and_start(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        row: dict[str, Any],
    ) -> bool:
        with self.runner.store.runner_connection() as connection:
            started = self.runner.steps.completed(
                connection,
                ctx.campaign["id"],
                stage["stage_key"],
                attempt["attempt"],
                "start_route",
                0,
            )
        # Only before the route started: a replay after start_route is recorded must
        # finish its bookkeeping, never fail an attempt that is already running.
        if started is None:
            problem = self._operator_config_problem(spec_stage)
            if problem is not None:
                raise StepFailed("salad", "step_refused", problem)
            selection: ExecutionSelection | None = self._selection(ctx, stage, spec_stage, row)
        else:
            selection = self._started_selection(ctx, stage, spec_stage, row)
        self._command(ctx, stage, attempt, row, "approve_route", "approve", selection)
        self._command(ctx, stage, attempt, row, "start_route", "start", selection)
        self._record_started(attempt["id"], row["operation_id"])
        return True

    def _record_started(self, attempt_id: str, operation_id: str) -> None:
        """Phase and operation id move together: a crash cannot leave only one of them."""
        with self.runner.store.runner_connection() as connection:
            self.runner._touch(
                connection,
                "campaign_salad_attempts",
                "attempt_id=?",
                (attempt_id,),
                phase="route_started",
            )
            self.runner._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt_id,),
                status="running",
                operation_id=operation_id,
            )

    def adopt_started(self, ctx: _Ctx, key: str, attempt: dict[str, Any]) -> dict[str, Any]:
        """Resume after a crash between a completed start_route and its bookkeeping."""
        if attempt["operation_id"] is not None:
            return attempt
        with self.runner.store.runner_connection() as connection:
            done = self.runner.steps.completed(
                connection, ctx.campaign["id"], key, attempt["attempt"], "start_route", 0
            )
        if done is None:
            return attempt
        row = self.row(attempt["id"])
        if row["phase"] == "route_proposed":
            self._record_started(attempt["id"], row["operation_id"])
        else:
            self.runner._set_attempt(attempt["id"], operation_id=row["operation_id"])
        return self.runner._attempt(attempt["id"])

    def _cancel_route(
        self, ctx: _Ctx, stage: dict[str, Any], attempt: dict[str, Any], row: dict[str, Any]
    ) -> None:
        state = self.route_state(row["route_id"])
        assert state is not None
        policy = TargetPolicy(
            selection=ExecutionSelection.model_validate(state["proposal"]["selection"])
        )
        self._command(ctx, stage, attempt, row, "cancel_route", "cancel", None, policy)

    # waiting for the executor -----------------------------------------------------------

    def _await_outcome(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        row: dict[str, Any],
    ) -> bool:
        state = self.route_state(row["route_id"])
        if (
            row["phase"] == "executing"
            and row["waiting_reason"] == LIVE_CHECK_NO_RECEIPT
            and self._never_sent(ctx, row, state)
        ):
            # The live check exited with no receipt and control holds no create/start claim
            # or sent route effect: the existing unclaimed unwind (cancel route, revoke,
            # reconcile at 0 with the store's own no-claim proof) ends it. Anything else waits
            # as before.
            return self._abandon(ctx, stage, spec_stage, attempt, LIVE_CHECK_NO_RECEIPT)
        if row["phase"] == "route_started" and self._lease_fresh(row):
            # Queued behind the executor's own route limits but alive (it renews the lease):
            # not silent. The route-window clock starts at ``mark_executing`` (v0.104.0).
            raise _waiting()()
        window = (
            state["proposal"]["selection"]["resources"]["max_seconds"]
            if state
            else self.profile(spec_stage).route_max_seconds
        )
        deadline = self._silent_clock_start(ctx, row) + timedelta(
            seconds=window + SILENT_EXECUTOR_GRACE_SECONDS
        )
        if (
            row["phase"] == "executing"
            and self.runner.now() > deadline
            and self.runner.now()
            <= self._silent_clock_start(ctx, row)
            + timedelta(seconds=window + EXECUTING_RENEWED_GRACE_SECONDS)
            and self._lease_fresh(row)
        ):
            # The executor is still renewing a live lease inside its own time budget.
            raise _waiting()()
        if self.runner.now() > deadline:
            if self._never_sent(ctx, row, state):
                # Nothing was ever sent for this route: the existing unclaimed unwind (cancel
                # the route, revoke, reconcile at 0 on the store's own proof), not unknown.
                return self._abandon(ctx, stage, spec_stage, attempt, SILENT_NEVER_SENT)
            if _termination_confirmed(state):
                # The provider already confirmed the run stopped: its receipt is (or will be)
                # imported by the executor; wait for it instead of calling the run unknown.
                self._emit_once(
                    ctx,
                    IMPORT_AWAITED_EVENT,
                    {"attempt_id": attempt["id"]},
                    {
                        "stage_key": attempt["stage_key"],
                        "route_id": row["route_id"],
                        "at": self._stamp(),
                    },
                )
                awaited = self._import_awaited_at(ctx, attempt)
                if awaited is not None and self.runner.now() <= awaited + timedelta(
                    seconds=window + SILENT_EXECUTOR_GRACE_SECONDS
                ):
                    raise _waiting()()
                # The wait is bounded (one more route window plus grace): an executor whose
                # receipt directory is gone never imports, and the attempt must not stay
                # ``running`` on its hold forever. It goes unknown for the silent reason (an
                # unreadable wait mark does too), so a person is asked and the sanctioned late
                # import still applies.
            self._unknown(attempt, SILENT_EXECUTOR)
            return True
        raise _waiting()()

    def _import_awaited_at(self, ctx: _Ctx, attempt: dict[str, Any]) -> datetime | None:
        """When the runner first waited for this attempt's import (its ``at``, or the event's
        own time for one recorded before it carried one); None when unreadable."""
        event = self.runner._one(
            """SELECT created_at, json_extract(payload_json,'$.at') AS at FROM events
            WHERE project_id=? AND event_type=?
            AND json_extract(payload_json,'$.campaign_id')=?
            AND json_extract(payload_json,'$.attempt_id')=?
            ORDER BY created_at LIMIT 1""",
            (ctx.campaign["project_id"], IMPORT_AWAITED_EVENT, ctx.campaign["id"], attempt["id"]),
        )
        if event is None:
            return None
        try:
            return _parse_utc(event["at"] or event["created_at"])
        except (TypeError, ValueError):
            return None

    def _silent_clock_start(self, ctx: _Ctx, row: dict[str, Any]) -> datetime:
        """When the silent-executor clock starts: the Salad row's last move, or the campaign's
        last return to a forward-legal state if later. While blocked or paused the executor may
        not report forward progress, so that time is never counted (v0.104.0)."""
        start = _parse_utc(row["updated_at"])
        marks = ",".join("?" for _ in RESUME_EVENTS)
        for event in self.runner._rows(
            f"""SELECT created_at, json_extract(payload_json,'$.at') AS at FROM events
            WHERE project_id=? AND event_type IN ({marks})
            AND json_extract(payload_json,'$.campaign_id')=?""",
            (ctx.campaign["project_id"], *RESUME_EVENTS, ctx.campaign["id"]),
        ):
            try:
                resumed = _parse_utc(event["at"] or event["created_at"])
            except (TypeError, ValueError):
                continue
            start = max(start, resumed)
        return start

    def _lease_fresh(self, row: dict[str, Any]) -> bool:
        """The executor renewed this attempt's lease since the route started, and the lease is
        still live: a queued route of a live executor (fails closed: no lease is not fresh)."""
        if row.get("lease_id") is None:
            return False
        lease = self.runner._one(
            "SELECT expires_at, renewed_at FROM execution_leases WHERE id=?", (row["lease_id"],)
        )
        if lease is None:
            return False
        try:
            return _parse_utc(lease["expires_at"]) > self.runner.now() and _parse_utc(
                lease["renewed_at"]
            ) > _parse_utc(row["updated_at"])
        except (TypeError, ValueError):
            return False

    def _held_by_sibling_stop(
        self, ctx: _Ctx, attempt: dict[str, Any], row: dict[str, Any]
    ) -> bool:
        """A queued route (``route_started``, not yet executing) of a campaign that a sibling's
        failure stopped, with nothing sent: it must not launch (ADR 0208)."""
        with self.runner.store.runner_connection() as connection:
            if not stop_held(connection, attempt["id"]):
                return False
        return self._never_sent(ctx, row, self.route_state(row["route_id"]))

    def _never_sent(self, ctx: _Ctx, row: dict[str, Any], state: dict[str, Any] | None) -> bool:
        """True when control's own records prove no provider create or start was sent.

        The route exists and names no provider resource; each of its create/start effects (if
        any) was refused by the trusted server before its use-time claim; control holds no
        create/start claim for the operation; and the attempt has no manifest, provider id or
        passed live receipt (ADR 0205). A pure read."""
        if (
            state is None
            or state.get("provider_id") is not None
            or sent_launch_effects(state)
            or row["provider_id"] is not None
            or row["manifest_id"] is not None
        ):
            return False
        if row["live_receipt_json"] is not None:
            receipt = json.loads(row["live_receipt_json"])
            if receipt.get("status") == "passed" or receipt.get("provider_id"):
                return False
        return not self.claimed(ctx, row["operation_id"])

    def _unknown(self, attempt: dict[str, Any], detail: str) -> None:
        self._move(attempt["id"], phase="unknown", waiting_reason=detail[:300])
        self.runner._set_attempt(
            attempt["id"],
            status="unknown",
            failure_class="observed_unknown",
            failure_detail=detail[:500],
        )

    # verification ---------------------------------------------------------------------

    def _verify(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        row: dict[str, Any],
    ) -> bool:
        if self._never_sent(ctx, row, self.route_state(row["route_id"])):
            # An honest receipt of a run that ended before any create was sent (a pre-create
            # read timeout, a refused reserve): nothing was allocated or spent, so it takes
            # the same unclaimed unwind as a run with no receipt, with no person (ADR 0205).
            receipt = json.loads(row["live_receipt_json"] or "{}")
            if (
                type(receipt) is dict
                and receipt.get("create_preflight_refusal") == FRAME_FENCE_REFUSAL
            ):
                return self._abandon(ctx, stage, spec_stage, attempt, FRAME_FENCE_BEFORE_CREATE)
            return self._abandon(ctx, stage, spec_stage, attempt, CREATE_NOT_SENT)
        verdict, detail, evidence = self.verify_outcome(ctx, attempt, row)
        if verdict == "unknown" and detail in (UNCONFIRMED_CODE, UNCONFIRMED_NEW_ALLOCATION_CODE):
            self._termination_unconfirmed(ctx, attempt, row)
            return True
        if verdict == "unknown":
            self._unknown(attempt, detail)
            return True
        manifest = self.runner._one(
            "SELECT id, manifest_digest FROM artifact_manifests WHERE id=?", (row["manifest_id"],)
        )
        self._move(attempt["id"], salad_evidence_digest=evidence)
        fields: dict[str, Any] = {
            "status": "exited",
            "operation_id": row["operation_id"],
            # No exit code or run receipt exists for a Salad attempt: the verdict and the
            # Salad evidence digest are recorded in their own fields.
            "salad_verdict": verdict,
            "salad_evidence_digest": evidence,
            "manifest_id": manifest["id"] if manifest else None,
            "manifest_digest": manifest["manifest_digest"] if manifest else None,
        }
        if verdict != "ok":
            fields["failure_detail"] = detail[:500]
        if attempt["status"] != "unknown":
            self.runner._set_attempt(attempt["id"], **fields)
            return True
        # The sanctioned late import (v0.104.0): the attempt was unknown only because the
        # executor was silent. The unchanged verification passed or failed on provider-confirmed
        # termination, so it leaves unknown and its stage resumes, in one transaction; the
        # ordinary collect, resolve and charge path settles it (never unclaimed-zero).
        if verdict == "ok":
            fields.update(failure_class=None, failure_detail=None)
        else:
            fields["failure_class"] = None
        with self.runner.store.runner_connection() as connection:
            self.runner._touch(
                connection, "campaign_stage_attempts", "id=?", (attempt["id"],), **fields
            )
            self.runner._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=? AND status='blocked_unknown'",
                (ctx.campaign["id"], attempt["stage_key"]),
                status="running",
            )
            self.runner.store.record_runner_event(
                connection,
                ctx.campaign,
                "campaign.salad_late_import_verified",
                {
                    "stage_key": attempt["stage_key"],
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "verdict": verdict,
                    "salad_evidence_digest": evidence,
                },
            )
        return True

    def evidence_pass(self, ctx: _Ctx) -> None:
        """Evidence-only work, run while the campaign is blocked and before the stage loop of a
        running pass (so before any silent-clock check) (v0.104.0).

        * An ``outcome_recorded`` attempt (recorded during a block, or late-imported for an
          attempt called silent) is verified by the unchanged ``verify_outcome``.
        * An attempt already ``unknown`` only for the silent reason, whose route still wants
          to run but was provably never sent anything, gets the reduce-only ``cancel_route``
          when the only unclaimed refusal is ``route_wants_run``; the unchanged unclaimed proof
          then decides on the runner's own reconcile.
        * An attempt already ``unknown`` because its route never confirmed termination is
          re-read once per pass: when the guardian's recorded final now shows the provider-
          confirmed close of an uncertain allocation with no output (ADR 0212), it resolves
          as retryable infrastructure and its stage resumes (``_resume_reconciled_no_output``).

        A reconciled grant or a settled attempt is never touched."""
        rows = self.runner._rows(
            """SELECT a.* FROM campaign_stage_attempts a
            JOIN campaign_salad_attempts sa ON sa.attempt_id = a.id
            JOIN execution_grants g ON g.id = a.grant_id
            WHERE a.campaign_id=? AND a.settled_cents IS NULL
            AND g.status IN ('active','cancel_requested')
            AND ((sa.phase='outcome_recorded' AND a.status IN ('running','unknown'))
              OR (sa.phase='unknown' AND sa.waiting_reason IN (?,?) AND a.status='unknown'))
            ORDER BY a.stage_key, a.attempt""",
            (ctx.campaign["id"], SILENT_EXECUTOR, ROUTE_UNCONFIRMED),
        )
        for attempt in rows:
            key = attempt["stage_key"]
            try:
                spec_stage = self._spec(ctx, key)
            except KeyError:
                continue
            row = self.row(attempt["id"])
            try:
                if row["phase"] == "outcome_recorded":
                    if attempt["status"] == "unknown" and not self._late_imported(attempt):
                        continue
                    self._verify(ctx, self.runner._stage_row(ctx, key), spec_stage, attempt, row)
                elif row["phase"] == "unknown" and row["waiting_reason"] == ROUTE_UNCONFIRMED:
                    self._resume_guarded(ctx, attempt, row)
                else:
                    self._cancel_never_sent(ctx, key, attempt, row)
            except StepFailed as error:
                # A refused reduce-only step inside verification (``_abandon``'s unwind: a
                # sticky failed ``cancel_route`` or ``revoke_grant`` intent) fails this attempt
                # exactly as the stage loop would, and the rest of the pass goes on; it never
                # escapes to the tick and wedges every later pass.
                self.runner._attempt_failed(ctx, key, attempt, error.error_class, error.detail)
            except _waiting():
                continue

    def _resume_reconciled_no_output(
        self, ctx: _Ctx, attempt: dict[str, Any], row: dict[str, Any]
    ) -> None:
        """ADR 0212 for an attempt the verify step already left ``unknown`` (route termination
        unconfirmed): the unchanged ``verify_outcome`` decides. Only its reconciled-without-output
        verdict moves the attempt; then, in one transaction, the attempt leaves ``unknown`` with
        the failed verdict (the provider adapter classifies it, the runner retries it within
        ``retry.infra_retries`` and settles it at the guardian's estimate), its stage leaves
        ``blocked_unknown`` and one event records it. Anything else keeps today's block, its
        waiting reason unchanged (so the next blocked pass re-reads it again), with the closed
        code of why it is held recorded once per change of code.

        A stored guardian final that is not terminal yet (the executor recorded before the
        guardian's close) is first refreshed from control's own guardian read
        (``_refresh_guardian_final``); an unreadable guardian holds."""
        row, refresh_code = self._refresh_guardian_final(ctx, attempt, row)
        verdict, detail, evidence = self.verify_outcome(ctx, attempt, row)
        if verdict != "failed" or not detail.startswith(INFRA_SALAD_RECONCILED_NO_OUTPUT):
            self._note_held(ctx, attempt, refresh_code or self._held_code(attempt, row, verdict))
            return
        runner = self.runner
        with runner.store.runner_connection() as connection:
            runner._touch(
                connection,
                "campaign_salad_attempts",
                "attempt_id=? AND phase='unknown'",
                (attempt["id"],),
                phase="outcome_recorded",
                waiting_reason=None,
                salad_evidence_digest=evidence,
            )
            runner._touch(
                connection,
                "campaign_stage_attempts",
                "id=? AND status='unknown'",
                (attempt["id"],),
                status="exited",
                operation_id=row["operation_id"],
                salad_verdict="failed",
                salad_evidence_digest=evidence,
                manifest_id=None,
                manifest_digest=None,
                failure_class=None,
                failure_detail=detail[:500],
            )
            runner._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=? AND status='blocked_unknown'",
                (ctx.campaign["id"], attempt["stage_key"]),
                status="running",
            )
            runner.store.record_runner_event(
                connection,
                ctx.campaign,
                "campaign.salad_reconciled_no_output",
                {
                    "stage_key": attempt["stage_key"],
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "reason": RECONCILED_NO_OUTPUT_REASON,
                    "failure_class": INFRA_SALAD_RECONCILED_NO_OUTPUT,
                    "salad_evidence_digest": evidence,
                },
            )

    def _resume_guarded(self, ctx: _Ctx, attempt: dict[str, Any], row: dict[str, Any]) -> None:
        """``_resume_reconciled_no_output`` inside a guard: a blocked campaign has no runner
        breaker, so an unexpected error here is recorded once per (attempt, error class) and the
        pass goes on; the attempt stays held and is re-read on the next pass. Control flow (a
        lost lease, a lapsed delegation, waiting), a refused step and contention propagate."""
        from .campaign_runner import _LeaseLost
        from .campaign_steps import DelegationLapsed

        try:
            self._resume_reconciled_no_output(ctx, attempt, row)
        except (StepFailed, _LeaseLost, DelegationLapsed, sqlite3.OperationalError, _waiting()):
            raise
        except Exception as error:  # noqa: BLE001 - classified and recorded, never silent
            try:
                self._emit_once(
                    ctx,
                    RECONCILED_NO_OUTPUT_ERROR_EVENT,
                    {"attempt_id": attempt["id"], "error_class": type(error).__name__[:80]},
                    {"stage_key": attempt["stage_key"], "attempt": attempt["attempt"]},
                )
            except (sqlite3.Error, _LeaseLost):
                pass

    def _held_code(self, attempt: dict[str, Any], row: dict[str, Any], verdict: str) -> str:
        """The closed code of why the re-read attempt is still held (pure read)."""
        state = self.route_state(row["route_id"])
        if state is None:
            return "route_state_missing"
        if row.get("live_receipt_json") is None:
            return "live_receipt_missing"
        if verdict != "unknown":
            return f"verdict_{verdict}"[:40]
        with self.runner.store.runner_connection() as connection:
            refusal = reconciled_no_output_refusal(connection, attempt, row, state)
        return refusal or "verify_unknown"

    def _note_held(self, ctx: _Ctx, attempt: dict[str, Any], code: str) -> None:
        """One ``campaign.salad_reconciled_no_output_held`` event each time the held code
        changes (never one per pass); the waiting reason is never rewritten."""
        runner = self.runner
        with runner.store.runner_connection() as connection:
            last = connection.execute(
                """SELECT json_extract(payload_json,'$.refusal') AS refusal FROM events
                WHERE project_id=? AND event_type=?
                AND json_extract(payload_json,'$.campaign_id')=?
                AND json_extract(payload_json,'$.attempt_id')=? ORDER BY cursor DESC LIMIT 1""",
                (
                    ctx.campaign["project_id"],
                    RECONCILED_NO_OUTPUT_HELD_EVENT,
                    ctx.campaign["id"],
                    attempt["id"],
                ),
            ).fetchone()
            if last is not None and last["refusal"] == code:
                return
            runner._hold_lease(connection)
            runner.store.record_runner_event(
                connection,
                ctx.campaign,
                RECONCILED_NO_OUTPUT_HELD_EVENT,
                {
                    "stage_key": attempt["stage_key"],
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "refusal": code,
                },
            )

    def _refresh_guardian_final(
        self, ctx: _Ctx, attempt: dict[str, Any], row: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """ADR 0212: ``(row, code)``. The executor records the guardian final once; when that
        copy is missing or not terminal (not ``stopped``/``reconciled``), control reads the
        guardian's status itself over its own authenticated read (``read_final``), at most once
        per ``GUARDIAN_FINAL_REFRESH_SECONDS`` per attempt. Only a terminal fresh final replaces
        the stored one (one row write and one event, in one transaction), so a stored terminal
        final is never overwritten. ``code`` names why nothing was refreshed when control
        could not read (``guardian_final_unreadable``) or has no reader
        (``guardian_final_unrefreshable``); None otherwise. Fail closed: a failed read changes
        nothing."""
        stored = _recorded_final(row)
        if stored is not None and stored.get("status") in _TERMINAL_FINAL:
            return row, None
        key = attempt["id"]
        now = self.runner.now()
        due = self._final_refresh_at.get(key)
        if due is not None and now < due:
            return row, self._final_refresh_code.get(key)
        self._final_refresh_at[key] = now + timedelta(seconds=GUARDIAN_FINAL_REFRESH_SECONDS)
        read = getattr(self._guardian_reader, "read_final", None)
        if read is None or row.get("route_id") is None or row.get("proposal_digest") is None:
            self._final_refresh_code[key] = "guardian_final_unrefreshable"
            return row, "guardian_final_unrefreshable"
        try:
            fresh = read(canonical_digest([row["route_id"], row["proposal_digest"]]))
            if fresh is not None:
                _GuardianFinal.model_validate(fresh)
        except Exception:  # noqa: BLE001 - an unreadable guardian holds (fail closed)
            fresh = None
        if type(fresh) is not dict:
            self._final_refresh_code[key] = "guardian_final_unreadable"
            return row, "guardian_final_unreadable"
        self._final_refresh_code[key] = None
        if fresh.get("status") not in _TERMINAL_FINAL:
            return row, None
        runner = self.runner
        with runner.store.runner_connection() as connection:
            runner._touch(
                connection,
                "campaign_salad_attempts",
                "attempt_id=? AND phase='unknown'",
                (key,),
                guardian_final_json=json.dumps(fresh, sort_keys=True),
            )
            runner.store.record_runner_event(
                connection,
                ctx.campaign,
                GUARDIAN_FINAL_REFRESHED_EVENT,
                {
                    "stage_key": attempt["stage_key"],
                    "attempt": attempt["attempt"],
                    "attempt_id": key,
                    "previous_status": None if stored is None else stored.get("status"),
                    "status": fresh["status"],
                    "reason": str(fresh.get("reason") or "")[:80],
                },
            )
        return self.row(key), None

    def reconcile_closed_no_output(
        self, ctx: _Ctx, attempt: dict[str, Any], settlement: int, basis: str
    ) -> bool:
        """ADR 0212: reconcile the grant of a reconciled-without-output attempt at ``settlement``
        (the guardian-bounded estimate ``settlement_for`` derived) when no worker ever reported
        a confirmed exit, so the grant route's exit-observation check cannot pass. The
        termination evidence is the guardian's recorded provider-confirmed close, re-checked
        here (``reconciled_no_output_refusal``) in the same transaction as the reconcile, the
        attempt's ``reconciled`` state, its settlement and one event.

        False when the ordinary ``reconcile_grant`` step applies (a confirmed exit exists, or the
        grant is no longer unresolved). When the rule no longer holds the attempt goes back to
        ``unknown`` for a person (True: the step ran) and nothing is reconciled."""
        runner = self.runner
        row = self.row(attempt["id"])
        state = self.route_state(row["route_id"])
        with runner.store.runner_connection() as connection:
            reconciliation = current_operation_reconciliation(connection, row["operation_id"])
            if (
                reconciliation is not None
                and reconciliation["observed_state"] == "exited"
                and reconciliation["termination_confirmed"]
            ):
                return False
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],)
            ).fetchone()
            if grant is None or grant["status"] not in ("active", "cancel_requested"):
                return False
            refusal = (
                "route_state_missing"
                if state is None
                else reconciled_no_output_refusal(connection, attempt, row, state)
            )
            if refusal is None:
                runner._hold_lease(connection)
                now = self._stamp()
                actor = runner._actor_principal(connection, ctx.campaign["id"])
                if (
                    reconcile_grant_row(
                        connection, grant, settlement_cents=settlement, principal_id=actor, now=now
                    )
                    is None
                ):
                    raise StepFailed(
                        "reconcile_grant",
                        "settlement_unavailable",
                        "execution reservation ledger is inconsistent; authority remains held",
                    )
                runner._touch(
                    connection,
                    "campaign_stage_attempts",
                    "id=?",
                    (attempt["id"],),
                    status="reconciled",
                )
                runner._settle(connection, attempt["id"], settlement, basis)
                runner.store.record_runner_event(
                    connection,
                    ctx.campaign,
                    "execution.reconciled_no_output_grant_reconciled",
                    {
                        "grant_id": grant["id"],
                        "stage_key": attempt["stage_key"],
                        "attempt": attempt["attempt"],
                        "attempt_id": attempt["id"],
                        "settlement_cents": settlement,
                        "settlement_basis": basis,
                        # The amount is the guardian's recorded estimate rounded up, not a bill.
                        "settlement_is_estimate": True,
                        "termination_evidence": "guardian_provider_confirmed_close",
                        "reason": RECONCILED_NO_OUTPUT_REASON,
                        "operational_only": True,
                    },
                    task_id=grant["task_id"],
                )
                return True
        # The waiting reason stays the one the blocked re-read keys on (a temporary refusal is
        # re-checked on the next pass); the refusal code is recorded on its own event.
        self._unknown(attempt, ROUTE_UNCONFIRMED)
        self._note_held(ctx, attempt, f"reconcile_{refusal}"[:60])
        return True

    def _late_imported(self, attempt: dict[str, Any]) -> bool:
        """An unknown attempt's outcome is verified only after the sanctioned late import: the
        runner called it silent, and control then recorded its outcome as a late import."""
        if attempt["failure_detail"] != SILENT_EXECUTOR:
            return False
        found = self.runner._one(
            """SELECT 1 FROM events WHERE event_type='campaign.salad_outcome_recorded'
            AND json_extract(payload_json,'$.attempt_id')=?
            AND json_extract(payload_json,'$.late_import')=1 LIMIT 1""",
            (attempt["id"],),
        )
        return found is not None

    def _cancel_never_sent(
        self, ctx: _Ctx, key: str, attempt: dict[str, Any], row: dict[str, Any]
    ) -> None:
        state = self.route_state(row["route_id"])
        if state is None or state.get("desired") != "run":
            return
        if not self._never_sent(ctx, row, state):
            return
        provider = self.runner._attempt_provider(ctx, key, attempt)
        with self.runner.store.runner_connection() as connection:
            fresh = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            verdict = assess_unclaimed(connection, fresh, ctx.campaign, provider)
        if not (
            isinstance(verdict, Refused)
            and verdict.reason == "claim_state_unknown"
            and verdict.provider_reason == "route_wants_run"
        ):
            return
        try:
            self._cancel_route(ctx, self.runner._stage_row(ctx, key), attempt, row)
        except StepFailed:
            return  # the unchanged proof keeps refusing; the human path stays

    def verify_outcome(
        self, ctx: _Ctx, attempt: dict[str, Any], row: dict[str, Any]
    ) -> tuple[str, str, str | None]:
        """Read-only: ``("ok"|"failed"|"unknown", detail, salad_evidence_digest)``.

        ``failed`` means termination is confirmed but the run did not pass; ``unknown``
        means the evidence is missing, contradictory or does not match.
        """
        state = self.route_state(row["route_id"])
        if state is None or row["live_receipt_json"] is None:
            return "unknown", "route state or live receipt is missing", None
        provider = [r for r in state["receipts"] if r.get("provenance") == "provider"]
        if state["reallocated"]:
            return self._verify_reallocated(ctx.campaign, row)
        if (
            not state["termination_confirmed"]
            or state["reallocated"]
            or state["provider_status"] != "stopped"
            or not provider
        ):
            with self.runner.store.runner_connection() as connection:
                refusal = reconciled_no_output_refusal(connection, attempt, row, state)
            if refusal is None:
                # ADR 0212: the guardian's own provider-confirmed close of an uncertain
                # allocation, with nothing retained: retryable infrastructure, never accepted.
                return (
                    "failed",
                    f"{INFRA_SALAD_RECONCILED_NO_OUTPUT}: reason={RECONCILED_NO_OUTPUT_REASON}; "
                    "the route never confirmed termination; the safety service confirmed the "
                    "exact group stopped after the allocation became uncertain; no output "
                    "retained; settles at the guardian's recorded estimate",
                    # The route's evidence plus the guardian final the verdict rests on.
                    canonical_digest(
                        {
                            "route": self._evidence(row, state, provider, None),
                            "guardian_final": json.loads(row["guardian_final_json"]),
                        }
                    ),
                )
            return "unknown", ROUTE_UNCONFIRMED, None
        with self.runner.store.runner_connection() as connection:
            reconciliation = current_operation_reconciliation(connection, row["operation_id"])
        if (
            reconciliation is None
            or reconciliation["observed_state"] != "exited"
            or not reconciliation["termination_confirmed"]
        ):
            return "unknown", "no confirmed exited worker observation", None
        receipt = json.loads(row["live_receipt_json"])
        manifest = (
            None
            if row["manifest_id"] is None
            else self.runner._one(
                """SELECT * FROM artifact_manifests WHERE id=? AND grant_id=? AND lease_id=?
                AND operation_id=? AND epoch=?""",
                (
                    row["manifest_id"],
                    attempt["grant_id"],
                    row["lease_id"],
                    row["operation_id"],
                    row["epoch"],
                ),
            )
        )
        passed = (
            receipt.get("status") == "passed"
            and receipt.get("reason") == "verified_journey"
            and receipt.get("stop_confirmed") is True
        )
        if not passed:
            digest = self._evidence(row, state, provider, manifest)
            reason = str(receipt.get("reason", "not_passed"))[:80]
            if _guardian_precustody_evidence(receipt):
                # ADR 0179 as amended: the route never flagged a reallocation, but the live
                # receipt carries the guardian's own pre-custody word, with the provider's stop
                # read confirmed above. Same retryable class as a route-flagged one.
                return (
                    "failed",
                    f"{REALLOCATED_PRECUSTODY}: signal={SIGNAL_GUARDIAN}; "
                    "allocation changed before custody; termination confirmed by provider read",
                    digest,
                )
            if _guardian_interruption_evidence(receipt):
                # The provider moved the instance after custody began and its own read
                # confirmed the stop: a retryable interruption. Outputs are never accepted.
                return (
                    "failed",
                    f"{SALAD_INTERRUPTED}: signal={SIGNAL_GUARDIAN}; "
                    "allocation changed after custody began; termination confirmed by "
                    "provider read",
                    digest,
                )
            return "failed", f"live receipt not passed: {reason}", digest
        if manifest is None or not self._outputs_match(receipt, manifest):
            return "unknown", "manifest does not match the imported output hashes", None
        with self.runner.store.runner_connection() as connection:
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],)
            ).fetchone()
            partial = grant is None or grant_manifest_is_partial(
                connection, grant, manifest["artifacts_json"]
            )
        if partial:
            # A manifest that lacks a declared output (an interrupted attempt's) is never a pass.
            return "unknown", "manifest lists fewer files than the plan declares", None
        return "ok", "verified_journey", self._evidence(row, state, provider, manifest)

    def scoped_route(self, campaign: dict[str, Any], route_id: str | None) -> dict[str, Any] | None:
        """The route's latest state, read only within the campaign's own org and project."""
        if route_id is None:
            return None
        row = self.runner._one(
            """SELECT payload_json FROM events WHERE event_type='remote_execution.state'
            AND org_id=? AND project_id=? AND json_extract(payload_json,'$.id')=?
            ORDER BY cursor DESC LIMIT 1""",
            (campaign["org_id"], campaign["project_id"], route_id),
        )
        return None if row is None else json.loads(row["payload_json"])

    def _verify_reallocated(
        self, campaign: dict[str, Any], row: dict[str, Any]
    ) -> tuple[str, str, str | None]:
        """ADR 0149: a reallocated route ends ``failed`` only on the provider's own confirmed
        read of the stopped group plus a matching confirmed exit; otherwise it stays held.

        Its outputs are never accepted (custody could not be sealed after the change), so the
        verdict is never ``ok``; the route's own termination fence is left as it is."""
        state = self.scoped_route(campaign, row["route_id"])
        if state is None:
            return "unknown", "route state is missing", None
        reading = provider_termination(state)
        if not reading.confirmed:
            return "unknown", reading.code, None
        with self.runner.store.runner_connection() as connection:
            reconciliation = current_operation_reconciliation(connection, row["operation_id"])
        if (
            reconciliation is None
            or reconciliation["observed_state"] != "exited"
            or not reconciliation["termination_confirmed"]
        ):
            return "unknown", "no confirmed exited worker observation", None
        provider = [r for r in state["receipts"] if r.get("provenance") == "provider"]
        receipt = json.loads(row["live_receipt_json"]) if row["live_receipt_json"] else {}
        detail = "allocation changed; termination confirmed by provider read"
        if _guardian_precustody_evidence(receipt, provider_confirmed=False):
            # ADR 0179: the guardian's own word that no custody work began. The class rides on
            # the detail so the provider adapter reports a retryable interruption.
            detail = f"{REALLOCATED_PRECUSTODY}: signal={SIGNAL_ROUTE_FLAG}; {detail}"
        elif _guardian_interruption_evidence(receipt, provider_confirmed=False):
            # Custody began, then the provider moved the instance, and its own read confirmed
            # the stop: a retryable interruption on a NEW operation. Outputs stay unaccepted.
            detail = f"{SALAD_INTERRUPTED}: signal={SIGNAL_ROUTE_FLAG}; {detail}"
        else:
            # The provider's own move, confirmed by its read and an exited worker, without the
            # guardian's word on custody (another stop cause, or custody unknown): never the
            # workload's failure. A fresh attempt may retry it within ``retry.infra_retries``;
            # it settles like any failed attempt (never at an interruption's estimate).
            detail = f"{INFRA_SALAD_REALLOCATED}: signal={SIGNAL_ROUTE_FLAG}; {detail}"
        return "failed", detail, self._evidence(row, state, provider, None)

    def _termination_unconfirmed(
        self, ctx: _Ctx, attempt: dict[str, Any], row: dict[str, Any]
    ) -> None:
        """The provider never confirmed a reallocated group stopped: the attempt is unknown and
        held, with a short code on the Salad row and one event, in one transaction. Nothing is
        released or settled; the human reconcile path is the way out (ADR 0149)."""
        reading = provider_termination(self.scoped_route(ctx.campaign, row["route_id"]))
        code = reading.code
        runner = self.runner
        with runner.store.runner_connection() as connection:
            runner._touch(
                connection,
                "campaign_salad_attempts",
                "attempt_id=?",
                (attempt["id"],),
                phase="unknown",
                waiting_reason=code,
            )
            runner._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status="unknown",
                failure_class="observed_unknown",
                failure_detail=f"{code}: provider has not confirmed the stopped group",
            )
            seen = connection.execute(
                """SELECT 1 FROM events WHERE project_id=? AND event_type=?
                AND json_extract(payload_json,'$.campaign_id')=?
                AND json_extract(payload_json,'$.attempt_id')=? LIMIT 1""",
                (ctx.campaign["project_id"], UNCONFIRMED_EVENT, ctx.campaign["id"], attempt["id"]),
            ).fetchone()
            if seen is None:
                runner.store.record_runner_event(
                    connection,
                    ctx.campaign,
                    UNCONFIRMED_EVENT,
                    {
                        "stage_key": attempt["stage_key"],
                        "attempt": attempt["attempt"],
                        "attempt_id": attempt["id"],
                        "route_id": row["route_id"],
                        "reason": code,
                        "new_allocation_unconfirmed": reading.new_allocation_unconfirmed,
                        "provider_reads_after_stop": reading.provider_reads,
                        "hold_cents": attempt["hold_cents"],
                        "hold_released": False,
                    },
                )

    @staticmethod
    def _outputs_match(receipt: dict[str, Any], manifest: dict[str, Any]) -> bool:
        listed = {
            item["relative_path"]: (item["sha256"], item["size"])
            for item in json.loads(manifest["artifacts_json"])
        }
        outputs = receipt.get("outputs")
        if outputs is not None:
            expected = {item["name"]: (item["sha256"], item["bytes"]) for item in outputs}
        elif receipt.get("output_sha256") is not None:
            expected = {"output.json": (receipt["output_sha256"], receipt.get("output_bytes"))}
        else:
            return False
        return bool(expected) and listed == expected

    @staticmethod
    def _evidence(
        row: dict[str, Any],
        state: dict[str, Any],
        provider: list[dict[str, Any]],
        manifest: dict[str, Any] | None,
    ) -> str:
        return canonical_digest(
            {
                "route_id": row["route_id"],
                "proposal_digest": row["proposal_digest"],
                "receipts": [canonical_digest(r) for r in provider],
                "live_receipt_digest": row["live_receipt_digest"],
                "manifest_digest": None if manifest is None else manifest["manifest_digest"],
            }
        )

    # unclaimed and unwinding --------------------------------------------------------

    def _abandon(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        reason: str,
    ) -> bool:
        """A pre-route failure: cancel authority, prove no claim, retry within the budget.

        A create proven never sent (``CREATE_NOT_SENT``) with an infrastructure retry left
        spends that retry (``infra_salad_create_not_sent``), not a scientific one; every other
        reason, or none left, keeps ``t1_salad_unclaimed`` (ADR 0205 amendment)."""
        key = stage["stage_key"]
        done = self.unwind(ctx, key, attempt)
        if done:
            failure_class, detail = "t1_salad_unclaimed", reason
            if reason == FRAME_FENCE_BEFORE_CREATE:
                # The guardian's open-frame fence was full when the create's reserve asked: a
                # wait for capacity, not a failure of the attempt. Free (not started) within the
                # repeat bound; past it, the ordinary never-sent handling below decides.
                others = [a for a in self.runner._attempts(ctx, key) if a["id"] != attempt["id"]]
                repeats = repeat_count(others, reason)
                if repeats < MAX_AUTO_REQUEUES:
                    failure_class = NOT_STARTED
                    detail = (
                        f"{reason}: the guardian's open-frame limit was full at the reserve; "
                        f"nothing was sent to the provider; released at 0 ({reason})"
                    )
                else:
                    reason = CREATE_NOT_SENT
                    detail = (
                        f"{CREATE_NOT_SENT}; not requeued as not started: "
                        f"{FRAME_FENCE_BEFORE_CREATE} repeated {repeats + 1} times"
                    )
            if reason == STAGE_FAILED_STOP:
                # A sibling's failure stopped the campaign before this route was sent anything:
                # released at 0 and never launched. Not started, whatever the repeat count.
                failure_class = NOT_STARTED
                detail = (
                    f"{reason}: a sibling stage failed before this route was sent anything; "
                    "released at 0, not launched"
                )
            if reason == SILENT_NEVER_SENT:
                detail = (
                    f"{reason}: the route window passed and control holds no create or start "
                    f"claim, sent route effect or provider reference; released at 0 ({reason})"
                )
                others = [a for a in self.runner._attempts(ctx, key) if a["id"] != attempt["id"]]
                repeats = repeat_count(others, reason)
                if repeats < MAX_AUTO_REQUEUES:
                    # Queued, never sent: an attempt number, not a scientific retry (ADR 0046).
                    failure_class = NOT_STARTED
                else:
                    # ADR 0046's identical-repeat bound: the release at 0 stands (nothing was
                    # sent), but the stage is no longer requeued for free; the attempt keeps the
                    # unclaimed class, so the stage's own scientific retry bound ends it.
                    detail = (
                        f"{detail}; not requeued as not started: {reason} repeated "
                        f"{repeats + 1} times"
                    )
                    self._emit_once(
                        ctx,
                        "campaign.autoreconcile_stopped",
                        {"stage_key": key, "attempt": attempt["attempt"]},
                        {
                            "reason": f"not auto-requeued: {reason} repeated {repeats + 1} times",
                            "not_started_reason": reason,
                        },
                    )
            if reason == CREATE_NOT_SENT:
                limit = spec_stage.retry.infra_retries
                used = infra_attempts(
                    [a for a in self.runner._attempts(ctx, key) if a["id"] != attempt["id"]]
                )
                if used < limit:
                    failure_class = INFRA_SALAD_CREATE_NOT_SENT
                    detail = (
                        f"{detail}; infrastructure failure, retry {used + 1} of {limit} allowed"
                    )
            self.runner._set_attempt(
                attempt["id"],
                status="failed_transient",
                failure_class=failure_class,
                failure_detail=detail,
            )
        return done

    def unwind(self, ctx: _Ctx, key: str, attempt: dict[str, Any]) -> bool:
        """Drive a packaged Salad attempt to settled; True when nothing is outstanding."""
        runner = self.runner
        attempt = self.adopt_started(ctx, key, attempt)
        row = self.row(attempt["id"])
        spec_stage = self._spec(ctx, key)
        seen = runner._observe(attempt)
        grant = seen["grant"]
        if grant["status"] == "reconciled":
            with runner.store.runner_connection() as connection:
                ours = runner.steps.completed(
                    connection,
                    ctx.campaign["id"],
                    key,
                    attempt["attempt"],
                    "reconcile_unclaimed_salad",
                    0,
                )
            if ours is not None:
                amount = int(grant["settlement_cents"] or 0)
                found: tuple[int, str] | None = (
                    amount,
                    "unclaimed_zero" if amount == 0 else "human_reconcile",
                )
            else:
                found = self.reconciled(attempt, grant)
            if found is None:
                return False  # claimed, no recorded charge: waiting for a human
            settled, basis = found
            with runner.store.runner_connection() as connection:
                if ours is not None:
                    connection.execute(
                        """UPDATE campaign_salad_attempts SET phase='unclaimed',
                        revision=revision+1, updated_at=? WHERE attempt_id=?""",
                        (self._stamp(), attempt["id"]),
                    )
                runner._settle(connection, attempt["id"], settled, basis)
            return True
        stage = runner._stage_row(ctx, key)
        route = self.route_state(row["route_id"])
        if route is not None and route["desired"] != "stop":
            self._cancel_route(ctx, stage, attempt, row)
            return False
        if grant["status"] == "active":
            runner._revoke(ctx, key, attempt, grant)
            return False
        if self.claimed(ctx, seen["operation_id"] or row["operation_id"]):
            return self._unwind_claimed(ctx, key, spec_stage, attempt, row, seen, route)
        try:
            result = runner._step(
                ctx,
                key,
                attempt["attempt"],
                "reconcile_unclaimed_salad",
                0,
                lambda: (
                    {"grant_id": grant["id"]},
                    {"expected_revision": grant["revision"]},
                ),
            )
        except StepFailed as error:
            self._unknown(attempt, f"unclaimed reconcile refused: {error.error_class}")
            return False
        with runner.store.runner_connection() as connection:
            connection.execute(
                """UPDATE campaign_salad_attempts SET phase='unclaimed', revision=revision+1,
                updated_at=? WHERE attempt_id=?""",
                (self._stamp(), attempt["id"]),
            )
            amount = int(result.response.get("settlement_cents") or 0)
            # Only a zero is an unclaimed proof; any recorded amount keeps its own label.
            runner._settle(
                connection,
                attempt["id"],
                amount,
                "unclaimed_zero" if amount == 0 else "human_reconcile",
            )
        return True

    def _unwind_claimed(
        self,
        ctx: _Ctx,
        key: str,
        spec_stage: StageSpec,
        attempt: dict[str, Any],
        row: dict[str, Any],
        seen: dict[str, Any],
        route: dict[str, Any] | None,
    ) -> bool:
        """A provider effect was claimed: only confirmed termination plus a charge settles."""
        runner = self.runner
        reconciliation = seen["reconciliation"]
        if (
            reconciliation
            and reconciliation["observed_state"] == "exited"
            and reconciliation["termination_confirmed"]
        ):
            settlement, basis = runner._settlement_for(spec_stage, attempt)
            result = runner._step(
                ctx,
                key,
                attempt["attempt"],
                "reconcile_grant",
                1,
                lambda: (
                    {"grant_id": attempt["grant_id"]},
                    {
                        "expected_revision": runner._one(
                            "SELECT revision FROM execution_grants WHERE id=?",
                            (attempt["grant_id"],),
                        )["revision"],
                        "settlement_cents": settlement,
                        "provider_terminated": True,
                    },
                ),
            )
            amount = int(result.response.get("settlement_cents") or 0)
            if amount != settlement:
                # A replayed step answers with what it recorded earlier; no basis can honestly
                # describe that amount now, so settle nothing and block (the runner's policy).
                self._move(attempt["id"], phase="unknown", waiting_reason="settlement_mismatch")
                runner.block_settlement_mismatch(attempt)
                return True
            with runner.store.runner_connection() as connection:
                runner._settle(connection, attempt["id"], amount, basis)
            return True
        window = (
            route["proposal"]["selection"]["resources"]["max_seconds"]
            if route
            else self.profile(spec_stage).route_max_seconds
        )
        deadline = _parse_utc(row["updated_at"]) + timedelta(
            seconds=window + SILENT_EXECUTOR_GRACE_SECONDS
        )
        if runner.now() > deadline and row["phase"] != "unknown":
            self._unknown(attempt, "claimed provider effect without confirmed termination")
        return False

    # settlement -------------------------------------------------------------------------

    def charges(self, attempt: dict[str, Any]) -> tuple[int, int]:
        """(entry count, total cents) of recorded compute charges for the admitted attempt."""
        stage = self.runner._one(
            "SELECT experiment_id FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
            (attempt["campaign_id"], attempt["stage_key"]),
        )
        row = self.runner._one(
            """SELECT count(*) AS n, COALESCE(SUM(amount_cents), 0) AS total
            FROM experiment_budget_entries WHERE experiment_id=? AND admitted_attempt_id=?
            AND category='compute'""",
            (stage["experiment_id"], attempt["admitted_attempt_id"]),
        )
        assert row is not None
        return int(row["n"]), int(row["total"])

    def settlement_for(self, stage: StageSpec, attempt: dict[str, Any]) -> tuple[int, str]:
        """``(cents, basis)``. Never 0 without an unclaimed proof.

        Order: unclaimed proof, human-reconciled grant, human-recorded charge, the
        guardian-bounded hold; otherwise wait for a human charge.
        """
        runner = self.runner
        row = self.row(attempt["id"])
        if row["phase"] == "unclaimed":
            return 0, "unclaimed_zero"
        grant = runner._one("SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],))
        ours = runner._one(
            """SELECT 1 AS n FROM campaign_step_intents WHERE campaign_id=? AND stage_key=?
            AND attempt=? AND step IN ('reconcile_grant','reconcile_unclaimed_salad')
            AND status='done'""",
            (attempt["campaign_id"], attempt["stage_key"], attempt["attempt"]),
        )
        if grant is not None and grant["status"] == "reconciled" and ours is None:
            found = self.reconciled(attempt, grant)
            if found is None:
                raise _waiting()()
            return found
        count, total = self.charges(attempt)
        if count:
            return total, "human_charge"
        if self._guardian_overrun(attempt, row):
            self._block_overrun(attempt)
            raise _waiting()()
        bounded = self.bounded_hold(attempt, row)
        if bounded is not None and self._read_verdict(attempt, row):
            return bounded, "guardian_bounded_hold"
        self._await_charge(attempt)
        raise _waiting()()

    def _clean_guardian(
        self,
        attempt: dict[str, Any],
        row: dict[str, Any],
        *,
        interrupted: bool = False,
        provider_close: bool = False,
    ) -> Decimal | None:
        """The estimate (cents) when the guardian final is clean and the verdict is
        ``ok``/``failed``; None for anything missing, malformed or not clean.

        ``interrupted`` (the guardian stopped on an allocation change after custody began, the
        provider's read confirmed) tolerates exactly the ``allocation_uncertain`` flag that cause
        sets, when the final's own reason is that cause or the final is ``reconciled``. It also
        accepts a ``reconciled`` final: after a confirmed interruption the guardian may only
        reconcile the frame (a ``stop_only`` refusal of the live check's cancel), and the
        provider's own read already confirmed the stop.

        ``provider_close`` (a provider-confirmed move, or a safety-plane stop) also accepts a
        ``reconciled`` final, but only one the guardian reached by the safety service's own
        provider-confirmed close (reason ``RECONCILE_PROVIDER``: a fresh exact read of the group
        stopped with zero instances); an operator's attested reconcile never qualifies. That close
        is reached only from an uncertain allocation, so it tolerates exactly that flag. Every
        other flag still refuses."""
        if attempt.get("salad_verdict") not in ("ok", "failed"):
            return None
        if row.get("guardian_final_json") is None:
            return None
        try:
            final = _GuardianFinal.model_validate_json(row["guardian_final_json"])
            rate = Decimal(final.published_hourly_rate_usd or "")
            estimate = Decimal(final.estimated_exposure_cents or "")
        except (ValueError, InvalidOperation):
            return None
        # Fails safe: if a real final words its reason differently, the flag is not tolerated
        # and the attempt waits for a charge (held), never under-recorded. Not checked live.
        closed_by_provider = (
            provider_close and final.status == "reconciled" and final.reason == RECONCILE_PROVIDER
        )
        stopped = (
            final.status == "stopped"
            or (interrupted and final.status == "reconciled")
            or closed_by_provider
        )
        uncertain = final.allocation_uncertain and not (
            (interrupted and (final.reason == "allocation_changed" or final.status == "reconciled"))
            or (provider_close and final.reason == "allocation_changed")
            or closed_by_provider
        )
        flags = (
            final.orphan,
            uncertain,
            final.escalation_pending,
            final.budget_fenced,
            final.rate_unknown,
        )
        if (
            not stopped
            or any(flags)
            or not rate.is_finite()
            or rate <= 0
            or final.actual_billed_cost_cents is not None
            or not estimate.is_finite()
            or estimate < 0
        ):
            return None
        return estimate

    def bounded_hold(self, attempt: dict[str, Any], row: dict[str, Any]) -> int | None:
        """The attempt's hold when guardian evidence bounds it (ADR 0038); pure read.

        An interrupted attempt (``salad_interrupted``) settles at its accrued guardian estimate
        instead (rounded up, at least one cent, within the hold), still provisional: a recorded
        charge raises or confirms it later. An estimate above the hold bounds nothing."""
        hold = int(attempt["hold_cents"])
        if _never_allocated(row):
            # The guardian recorded that nothing was ever placed (no machine latched, never seen
            # running, no custody begun): Salad bills only while an instance runs on a machine,
            # so the hold is not spent. Settle the guardian's own accrued estimate (zero when it
            # saw nothing run; a backdated poll gap rounds up), still provisional like any
            # bounded hold: a recorded charge raises or confirms it later.
            estimate = self._clean_guardian(attempt, row)
            if estimate is None or hold < 1 or math.ceil(estimate) > hold:
                return None
            return math.ceil(estimate)
        if _was_interrupted(attempt):
            estimate = self._clean_guardian(attempt, row, interrupted=True)
            if estimate is None or hold < 1 or math.ceil(estimate) > hold:
                return None
            return max(1, math.ceil(estimate))
        if _provider_moved(attempt) or _retained_before_move(row) or _provider_closed(row):
            # (``_provider_closed``: an attempt recorded before the move markers existed.)
            # The provider moved the instance and its own read confirmed the stop (control's
            # verify step; never a receipt reason). The guardian accrued only while it saw the
            # group running and froze accrual at its provider-confirmed close, so its estimate
            # bounds the cost: settle it rounded up and at least one cent (custody may have
            # begun, so never zero), within the hold. Provisional like every bounded hold: a
            # recorded charge raises or confirms it (``late_charges``).
            estimate = self._clean_guardian(attempt, row, provider_close=True)
            if estimate is None or hold < 1 or math.ceil(estimate) > hold:
                return None
            return max(1, math.ceil(estimate))
        # A safety-plane stop settles like any failed run (the whole hold), also when the
        # guardian could only close the frame by the provider-confirmed reconcile.
        estimate = self._clean_guardian(attempt, row, provider_close=_safety_stopped(row))
        if estimate is None or hold < 1 or math.ceil(estimate) > hold:
            return None
        return hold

    def _read_verdict(self, attempt: dict[str, Any], row: dict[str, Any]) -> bool:
        """Read the guardian's per-frame record ourselves; True only when it says ``stopped``
        (or, for a provider-confirmed interruption, ``reconciled``: the guardian closed the
        frame by reconciling a stop it could not issue itself, ``stop_only``).

        The executor's recorded final supplies the cost estimate (``_clean_guardian``) but never
        the verdict: a bounded-hold settlement needs control's own read of the guardian record
        (ADR 0041 sections 2 and 7.1). Every read is appended to the frame's guardian records;
        the first ``stopped`` read also sets the frame's guardian verdict digest to the record's
        digest. No reader, an unreachable or malformed answer, or an ``open`` verdict returns
        False and nothing settles: the hold stays, and the human charge path is unchanged."""
        reader = self._guardian_reader
        if reader is None or row.get("route_id") is None or row.get("proposal_digest") is None:
            return False
        reservation = canonical_digest([row["route_id"], row["proposal_digest"]])
        try:
            record = reader.read(reservation)
        except Exception:
            return False
        if validate_frame_record(record, reservation) is None:
            return False
        campaign = self.runner._campaign(attempt["campaign_id"])
        tenant = frames.Tenant(campaign["org_id"], campaign["project_id"])
        with self.runner.store.runner_connection() as connection:
            self.runner._hold_lease(connection)
            frame = self.runner._open_frame(connection, attempt["id"])
            if frame["state"] in {"settling", "closed"}:
                return False
            actor = frames.Actor(
                "guardian_verdict", self.runner._actor_principal(connection, attempt["campaign_id"])
            )
            now = self._stamp()
            try:
                frames.bind_guardian_reservation(
                    connection,
                    frame["frame_id"],
                    tenant,
                    reservation=reservation,
                    actor=actor,
                    now=now,
                )
                frames.record_guardian_read(
                    connection,
                    frame["frame_id"],
                    tenant,
                    verdict=record["verdict"],
                    record_sequence=record["sequence"],
                    record_digest=record["record_digest"],
                    now=now,
                )
                # A provider-confirmed interruption or move, or a safety-plane stop, may close
                # only by the guardian's reconcile; ``_clean_guardian`` separately requires the
                # recorded final's own shape (for a move or a safety stop, the provider-confirmed
                # close, never an operator attestation).
                closed = (
                    {"stopped", "reconciled"}
                    if _was_interrupted(attempt)
                    or _provider_moved(attempt)
                    or _retained_before_move(row)
                    or _provider_closed(row)
                    or _safety_stopped(row)
                    else {"stopped"}
                )
                if record["verdict"] not in closed:
                    return False
                frame = frames.get_frame(connection, frame["frame_id"], tenant)
                if frame["guardian_verdict_digest"] is None:
                    frames.record_guardian_verdict(
                        connection,
                        frame["frame_id"],
                        tenant,
                        verdict_digest=record["record_digest"],
                        actor=actor,
                        now=now,
                    )
            except (frames.FrameRefused, Conflict):
                return False
        return True

    def _guardian_overrun(self, attempt: dict[str, Any], row: dict[str, Any]) -> bool:
        estimate = self._clean_guardian(attempt, row)
        return (
            estimate is not None
            and row["phase"] != "unknown"
            and math.ceil(estimate) > int(attempt["hold_cents"])
        )

    def _block_overrun(self, attempt: dict[str, Any]) -> None:
        """The guardian's estimate exceeds the hold: unknown attempt, blocked campaign."""
        detail = "estimated_overrun"
        with self.runner.store.runner_connection() as connection:
            self.runner._hold_lease(connection)
            # One transaction: an attempt marked unknown always comes with its block.
            self.runner._touch(
                connection,
                "campaign_salad_attempts",
                "attempt_id=?",
                (attempt["id"],),
                phase="unknown",
                waiting_reason=detail,
            )
            self.runner._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status="unknown",
                failure_class="observed_unknown",
                failure_detail=detail,
            )
            campaign = dict(
                connection.execute(
                    "SELECT * FROM campaigns WHERE id=?", (attempt["campaign_id"],)
                ).fetchone()
            )
            blocked = connection.execute(
                """UPDATE campaigns SET status='blocked', stop_reason=COALESCE(stop_reason,
                'estimated_overrun'), updated_at=? WHERE id=? AND status='running'""",
                (self._stamp(), attempt["campaign_id"]),
            ).rowcount
            if blocked:
                self.runner.store.record_runner_event(
                    connection, campaign, "campaign.blocked", {"reason": "estimated_overrun"}
                )
                self.runner._free_campaign_releases(
                    connection, attempt["campaign_id"], "campaign_blocked"
                )

    def _await_charge(self, attempt: dict[str, Any]) -> None:
        row = self.row(attempt["id"])
        reason = (
            "record the compute charge for admitted attempt "
            f"{attempt['admitted_attempt_id']} at /experiments/<id>/budget/charges"
        )
        if row["phase"] not in {"awaiting_charge", "settled", "unclaimed", "unknown"}:
            self._move(attempt["id"], phase="awaiting_charge", waiting_reason=reason[:300])
        campaign = self.runner._campaign(attempt["campaign_id"])
        stage = self.runner._one(
            "SELECT experiment_id FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
            (attempt["campaign_id"], attempt["stage_key"]),
        )
        with self.runner.store.runner_connection() as connection:
            seen = connection.execute(
                """SELECT 1 FROM events WHERE project_id=? AND event_type=?
                AND json_extract(payload_json,'$.campaign_id')=?
                AND json_extract(payload_json,'$.attempt_id')=? LIMIT 1""",
                (
                    campaign["project_id"],
                    "campaign.stage_awaiting_human",
                    campaign["id"],
                    attempt["id"],
                ),
            ).fetchone()
            if seen is None:
                self.runner.store.record_runner_event(
                    connection,
                    campaign,
                    "campaign.stage_awaiting_human",
                    {
                        "stage_key": attempt["stage_key"],
                        "attempt": attempt["attempt"],
                        "attempt_id": attempt["id"],
                        "records": [attempt["admitted_attempt_id"]],
                        "experiment_id": stage["experiment_id"],
                        "reason": reason,
                    },
                )

    # exposure, late charges ----------------------------------------------------------

    def exposure_open(self, connection: sqlite3.Connection, campaign_id: str) -> bool:
        return _phase_open(connection, campaign_id, EXPOSURE_PHASES)

    def unresolved_exposure_open(self, connection: sqlite3.Connection, campaign_id: str) -> bool:
        """Open exposure whose cost is unresolved (a charge owed or unknown); not an attempt
        that is merely leased or running inside its own hold."""
        return _phase_open(connection, campaign_id, UNRESOLVED_EXPOSURE_PHASES)

    def late_charges(self, ctx: _Ctx) -> None:
        """Charges recorded after settlement are added once, by CAS, as late charges.

        A provisional ``guardian_bounded_hold`` settlement is confirmed by the first human
        charge: X <= hold only confirms (amounts unchanged); X > hold adds the excess.
        """
        self._late_charges_for(ctx.campaign["id"])

    def sweep_unconfirmed(self, limit: int = 50) -> None:
        """Apply charges recorded after a terminal campaign settled (events only).

        Only attempts with a charge the settlement has not absorbed are picked, so a
        campaign waiting for its first charge never occupies a slot."""
        marks = ",".join("?" for _ in TERMINAL_CAMPAIGN)
        rows = self.runner._rows(
            f"""SELECT DISTINCT c.id FROM campaign_salad_attempts sa
            JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
            JOIN campaigns c ON c.id = a.campaign_id
            JOIN campaign_stages st ON st.campaign_id = a.campaign_id
              AND st.stage_key = a.stage_key
            WHERE c.status IN ({marks}) AND sa.phase='settled' AND (
              SELECT COUNT(*) FROM experiment_budget_entries e
              WHERE e.experiment_id = st.experiment_id
              AND e.admitted_attempt_id = a.admitted_attempt_id AND e.category='compute') > 0
            AND ((a.settlement_basis='guardian_bounded_hold' AND sa.settlement_confirmed_at IS NULL)
              OR (SELECT COALESCE(SUM(e.amount_cents), 0) FROM experiment_budget_entries e
              WHERE e.experiment_id = st.experiment_id
              AND e.admitted_attempt_id = a.admitted_attempt_id AND e.category='compute')
              > COALESCE(sa.charged_cents, a.settled_cents, 0)
              OR (a.settlement_basis='guardian_bounded_hold'
              AND sa.settlement_confirmed_at IS NOT NULL
              AND (SELECT COALESCE(SUM(e.amount_cents), 0)
              FROM experiment_budget_entries e WHERE e.experiment_id = st.experiment_id
              AND e.admitted_attempt_id = a.admitted_attempt_id AND e.category='compute')
              = a.settled_cents))
            ORDER BY c.id LIMIT ?""",
            (*sorted(TERMINAL_CAMPAIGN), limit),
        )
        for entry in rows:
            self._late_charges_for(entry["id"])

    def _late_charges_for(self, campaign_id: str) -> None:
        """Absorb recorded compute charges into settled attempts, each once, by CAS.

        ``charged_cents`` is the amount the settlement already covers: a provisional
        bounded-hold settlement covers its whole hold, so charges below the hold (however
        many entries an invoice is split into) change nothing, and only the total beyond
        the settled amount is added. The campaign and attempt totals follow the charges even
        after the campaign is terminal; the stored receipt is never rewritten, and a cap
        overrun of a terminal campaign is the event ``campaign.late_overrun`` (a running
        one is blocked).
        """
        rows = self.runner._rows(
            """SELECT sa.attempt_id, sa.charged_cents, sa.revision, sa.settlement_confirmed_at,
            a.settlement_basis, a.settled_cents AS covered FROM campaign_salad_attempts sa
            JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
            WHERE a.campaign_id=? AND sa.phase='settled'
            ORDER BY sa.attempt_id""",
            (campaign_id,),
        )
        for entry in rows:
            provisional = (
                entry["settlement_basis"] == "guardian_bounded_hold"
                and entry["settlement_confirmed_at"] is None
            )
            attempt = self.runner._attempt(entry["attempt_id"])
            count, total = self.charges(attempt)
            # A settled attempt whose charge was never initialised is covered by its settled
            # amount, never by zero (no re-adding of already settled money).
            charged = entry["charged_cents"]
            if charged is None:
                charged = entry["covered"] or 0
            if provisional and count == 0:
                continue
            delta = total - charged
            settled = attempt["settled_cents"]
            if delta <= 0 and not provisional:
                if (
                    entry["settlement_basis"] == "guardian_bounded_hold"
                    and count
                    and total == settled
                ):
                    # The charges now equal the settled amount: only then is it a human charge.
                    with self.runner.store.runner_connection() as connection:
                        self.runner._hold_lease(connection)
                        confirm_settlement(
                            connection, entry["attempt_id"], charged_total=total, now=self._stamp()
                        )
                continue
            with self.runner.store.runner_connection() as connection:
                self.runner._hold_lease(connection)
                fields = "charged_cents=?, revision=revision+1, updated_at=?"
                # Never lower the covered amount: a partial charge keeps the hold.
                params: list[Any] = [max(charged, total), self._stamp()]
                if provisional:
                    fields += ", settlement_confirmed_at=?"
                    params.append(self._stamp())
                claimed = connection.execute(
                    f"UPDATE campaign_salad_attempts SET {fields} "
                    "WHERE attempt_id=? AND revision=?",
                    (*params, entry["attempt_id"], entry["revision"]),
                ).rowcount
                if claimed != 1:
                    continue
                campaign = dict(
                    connection.execute(
                        "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
                    ).fetchone()
                )
                terminal = campaign["status"] in TERMINAL_CAMPAIGN
                if provisional:
                    # A partial charge below the hold confirms the hold but keeps its basis
                    # (the settled amount is still the hold); the label follows the amount.
                    if delta > 0 or total == settled:
                        confirm_settlement(
                            connection, entry["attempt_id"], charged_total=total, now=self._stamp()
                        )
                    if delta <= 0:
                        self.runner.store.record_runner_event(
                            connection,
                            campaign,
                            "campaign.settlement_confirmed",
                            {
                                "attempt_id": entry["attempt_id"],
                                "provisional_cents": charged,
                                "charged_cents": total,
                            },
                        )
                        continue
                apply_late_charge(
                    connection,
                    entry["attempt_id"],
                    delta_cents=delta,
                    charged_total=total,
                    now=self._stamp(),
                )
                campaign["settled_cents"] += delta
                self.runner.store.record_runner_event(
                    connection,
                    campaign,
                    "campaign.late_charge",
                    {
                        "attempt_id": entry["attempt_id"],
                        "delta_cents": delta,
                        "charged_cents": total,
                        "campaign_terminal": terminal,
                    },
                )
                if (
                    campaign["held_cents"] + campaign["settled_cents"]
                    > campaign["budget_cap_cents"]
                ):
                    if terminal:
                        self.runner.store.record_runner_event(
                            connection, campaign, "campaign.late_overrun", {"delta_cents": delta}
                        )
                        continue
                    blocked = connection.execute(
                        """UPDATE campaigns SET status='blocked', stop_reason=COALESCE(stop_reason,
                        'budget_overrun'), updated_at=? WHERE id=? AND status='running'""",
                        (self._stamp(), campaign_id),
                    ).rowcount
                    if blocked:
                        self.runner.store.record_runner_event(
                            connection, campaign, "campaign.blocked", {"reason": "budget_overrun"}
                        )
                        self.runner._free_campaign_releases(
                            connection, campaign_id, "campaign_blocked"
                        )
