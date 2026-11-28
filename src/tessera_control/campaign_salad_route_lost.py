"""ADR 0212 addendum: a Salad route whose live check is gone with no receipt resolves on its own.

The executor finishes a route only from the live check's receipt on its disk. When the executor
crashes, is killed for memory, reboots or times the child out mid-route, no receipt is ever
written, and the runner used to wait out one or two route windows and then leave the attempt
``unknown`` (``SILENT_EXECUTOR``) for a person. The guardian keeps running meanwhile and stops
the group on its own (its deadline or a safety stop), so its final for the route says the spend
ended.

On resume with no receipt the executor reads the guardian's status (read-only) and, once it is
terminal, records it with ``record_salad_route_lost`` (``ROUTE_LOST_EVENT``). The Salad driver
then resolves the attempt here, as ``infra_salad_executor_lost``, only when every condition of
``route_lost_no_output_refusal`` holds; otherwise it keeps today's wait and ``unknown`` (fail
closed). Outputs are never accepted: with no receipt nothing of the run can be imported.
"""

from __future__ import annotations

import json
import math
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from .campaign_infra_retry import EXECUTOR_LOST_REASON, INFRA_SALAD_EXECUTOR_LOST
from .campaign_salad_commands import ROUTE_LOST_EVENT, SILENT_EXECUTOR
from .campaign_store import _GuardianFinal
from .store import canonical_digest

if TYPE_CHECKING:
    from .campaign_salad import SaladStageDriver

ROUTE_LOST_RESOLVED_EVENT = "campaign.salad_route_lost_resolved"
ROUTE_LOST_HELD_EVENT = "campaign.salad_route_lost_held"
ROUTE_LOST_ERROR_EVENT = "campaign.salad_route_lost_error"
ROUTE_LOST_DETAIL = (
    f"{INFRA_SALAD_EXECUTOR_LOST}: reason={EXECUTOR_LOST_REASON}; the route's live check is "
    "gone with no receipt on the executor's disk; the guardian's own final says the exact group "
    "stopped; no output retained; settles at the guardian's recorded estimate"
)


def route_lost_reported(connection: sqlite3.Connection, attempt_id: str) -> bool:
    """The executor recorded that this attempt's live check is gone with no receipt."""
    return (
        connection.execute(
            """SELECT 1 FROM events WHERE event_type=?
            AND json_extract(payload_json,'$.attempt_id')=? LIMIT 1""",
            (ROUTE_LOST_EVENT, attempt_id),
        ).fetchone()
        is not None
    )


def route_lost_no_output_refusal(
    connection: sqlite3.Connection,
    attempt: dict[str, Any],
    row: dict[str, Any],
    state: dict[str, Any] | None,
) -> str | None:
    """``None`` when a lost route may resolve as ``infra_salad_executor_lost``; otherwise the
    closed code of the first condition that keeps today's path (fail closed). Pure read.

    Every condition must hold:

    * the executor recorded the lost route (``ROUTE_LOST_EVENT``) and no live receipt was ever
      recorded for the attempt;
    * the recorded guardian final is terminal: ``stopped`` (the exact group stopped), or
      ``reconciled`` by the safety service's own provider-confirmed close (never an operator
      attestation);
    * that final is clean: no orphan, escalation, budget fence or unknown rate, no billed cost,
      and ``allocation_uncertain`` only where the guardian's settlement rule tolerates it (a
      provider-confirmed close, or a stop on an allocation change);
    * it carries a finite cost estimate whose next whole cent (at least one) fits the hold;
    * no output was retained, sealed or collected: no retained-before-move marker, no manifest
      on the attempt and no manifest or uploaded artifact under the grant;
    * no provider readback recorded on the route since that final lists a live instance."""
    from .campaign_salad import _lists_live_instance, _parse_utc
    from .salad_guardian import RECONCILE_PROVIDER

    if not route_lost_reported(connection, attempt["id"]):
        return "route_lost_unreported"
    if row.get("live_receipt_json") is not None:
        return "live_receipt_recorded"
    if state is None:
        return "route_state_missing"
    if row.get("guardian_final_json") is None:
        return "no_guardian_final"
    try:
        final = _GuardianFinal.model_validate_json(row["guardian_final_json"])
    except ValueError:
        return "guardian_final_unreadable"
    closed_by_provider = final.status == "reconciled" and final.reason == RECONCILE_PROVIDER
    if final.status != "stopped" and not closed_by_provider:
        return "guardian_not_stopped"
    if final.allocation_uncertain and not (
        closed_by_provider or final.reason == "allocation_changed"
    ):
        return "allocation_uncertain"
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


def evidence_digest(driver: SaladStageDriver, row: dict[str, Any], state: dict[str, Any]) -> str:
    """The route's evidence plus the guardian final the verdict rests on."""
    provider = [r for r in state.get("receipts") or [] if r.get("provenance") == "provider"]
    return canonical_digest(
        {
            "route": driver._evidence(row, state, provider, None),
            "guardian_final": json.loads(row["guardian_final_json"]),
            "reason": EXECUTOR_LOST_REASON,
        }
    )


def resolved_failure_class(
    driver: SaladStageDriver, ctx: Any, resolved: dict[str, Any]
) -> str | None:
    """The class the attempt will end with once resolved, from the stage provider's own
    ``observe`` (the single source the runner records), read against the same guardian final.
    Usually ``infra_salad_executor_lost``; a guardian final naming the run's own outcome (a
    non-zero exit, a start failure, an allocation timeout, a provisioning overrun) keeps that
    class. ``None`` when the provider gives no failed verdict for it."""
    runner = driver.runner
    provider = runner._provider(runner._spec_stage(ctx, resolved["stage_key"]))
    observed = provider.observe(runner, resolved)
    if observed.outcome not in ("failed_transient", "failed_permanent"):
        return None
    return observed.failure_class


def resolve(
    driver: SaladStageDriver, ctx: Any, attempt: dict[str, Any], row: dict[str, Any]
) -> bool:
    """Resolve a reported lost route (``executing``, or ``unknown`` for the silent reason) when
    ``route_lost_no_output_refusal`` holds: in one transaction the attempt leaves its phase with
    the failed verdict (the provider adapter classifies it and the runner retries it within
    ``retry.infra_retries`` at the guardian's estimate), an ``unknown`` one's stage leaves
    ``blocked_unknown``, and one ``ROUTE_LOST_RESOLVED_EVENT`` records it. True when it
    resolved. Otherwise the closed code of why it is held is recorded once per change of code
    and nothing else moves (the caller keeps today's path)."""
    runner = driver.runner
    phase = row["phase"]
    if phase not in ("executing", "unknown"):
        return False
    with runner.store.runner_connection() as connection:
        if not route_lost_reported(connection, attempt["id"]):
            return False  # the common case, read first: no route state is fetched
    state = driver.route_state(row["route_id"])
    # The class the attempt ends with is read first, outside the write (the provider's observe
    # opens its own reads), against the guardian final the write then checks is unchanged.
    fields: dict[str, Any] | None = None
    ends_as: str | None = None
    if state is not None and row.get("guardian_final_json") is not None:
        try:
            evidence = evidence_digest(driver, row, state)
        except ValueError:
            evidence = None  # the refusal below names the unreadable final
        if evidence is not None:
            fields = {
                "status": "exited",
                "operation_id": row["operation_id"],
                "salad_verdict": "failed",
                "salad_evidence_digest": evidence,
                "manifest_id": None,
                "manifest_digest": None,
                "failure_detail": ROUTE_LOST_DETAIL[:500],
            }
            if attempt["status"] == "unknown":
                fields["failure_class"] = None
            ends_as = resolved_failure_class(driver, ctx, {**attempt, **fields})
    with runner.store.runner_connection() as connection:
        refusal = route_lost_no_output_refusal(connection, attempt, row, state)
        if refusal is None:
            if fields is None or ends_as is None:
                return False  # no verdict to name: today's path decides (fail closed)
            evidence = fields["salad_evidence_digest"]
            runner._hold_lease(connection)
            current = connection.execute(
                """SELECT sa.phase, sa.live_receipt_json, sa.guardian_final_json, a.status
                FROM campaign_salad_attempts sa
                JOIN campaign_stage_attempts a ON a.id = sa.attempt_id WHERE sa.attempt_id=?""",
                (attempt["id"],),
            ).fetchone()
            if current is None or (
                current["phase"],
                current["live_receipt_json"],
                current["guardian_final_json"],
                current["status"],
            ) != (phase, None, row["guardian_final_json"], attempt["status"]):
                # Moved under us (a late outcome, a cancel, a new final the class was not read
                # from): today's path decides.
                return False
            runner._touch(
                connection,
                "campaign_salad_attempts",
                "attempt_id=?",
                (attempt["id"],),
                phase="outcome_recorded",
                waiting_reason=None,
                salad_evidence_digest=evidence,
            )
            runner._touch(connection, "campaign_stage_attempts", "id=?", (attempt["id"],), **fields)
            if attempt["status"] == "unknown":
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
                ROUTE_LOST_RESOLVED_EVENT,
                {
                    "stage_key": attempt["stage_key"],
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "from_phase": phase,
                    "reason": EXECUTOR_LOST_REASON,
                    "failure_class": ends_as,
                    "salad_evidence_digest": evidence,
                },
            )
            return True
    if refusal != "route_lost_unreported":
        driver._note_held(ctx, attempt, refusal, ROUTE_LOST_HELD_EVENT)
    return False


def resolve_guarded(
    driver: SaladStageDriver, ctx: Any, attempt: dict[str, Any], row: dict[str, Any]
) -> bool:
    """``resolve`` for the blocked evidence pass (no runner breaker there): an unexpected error
    is recorded once per (attempt, error class) and the attempt stays held. Control flow, a
    refused step and contention propagate."""
    from .campaign_runner import _LeaseLost
    from .campaign_salad import _waiting
    from .campaign_steps import DelegationLapsed, StepFailed

    try:
        return resolve(driver, ctx, attempt, row)
    except (StepFailed, _LeaseLost, DelegationLapsed, sqlite3.OperationalError, _waiting()):
        raise
    except Exception as error:  # noqa: BLE001 - classified and recorded, never silent
        try:
            driver._emit_once(
                ctx,
                ROUTE_LOST_ERROR_EVENT,
                {"attempt_id": attempt["id"], "error_class": type(error).__name__[:80]},
                {"stage_key": attempt["stage_key"], "attempt": attempt["attempt"]},
            )
        except (sqlite3.Error, _LeaseLost):
            pass
        return False


def held_back(driver: SaladStageDriver, attempt: dict[str, Any]) -> None:
    """A grant reconcile refused the rule after the verdict: back to ``unknown`` for the silent
    reason (the blocked re-read keys on it), never reconciled."""
    driver._unknown(attempt, SILENT_EXECUTOR)


EXECUTOR_REFUSED_RELEASE_EVENT = "execution.executor_refused_grant_released"
EXECUTOR_REFUSED_SLOT = "executor_refused_unleased_grant"


def release_refused_unleased(
    driver: SaladStageDriver, ctx: Any, attempt: dict[str, Any], reason: str
) -> bool:
    """ADR 0212 addendum: the executor refused an ``awaiting_lease`` attempt (a closed code).
    No worker ever took a lease and no operation exists, so the grant is given back at zero in
    one transaction (the shared reconcile accounting, ADR 0048; the same evidence as a paused
    or blocked campaign's release, ``releasable_unleased_grant``, which also re-reads inside
    this transaction that the Salad row is still ``awaiting_lease`` with no lease or
    operation) and the attempt ends ``failed_transient``
    / ``t1_salad_unclaimed`` with the refusal as its detail: the stage's own retry budget
    decides, at once, never by waiting out the grant. False (nothing changed) when that
    evidence no longer holds; the caller then waits for the next phase."""
    from .campaign_slot_reclaim import record_slot_freed, releasable_unleased_grant
    from .execution import reconcile_grant_row

    runner = driver.runner
    with runner.store.runner_connection() as connection:
        runner._hold_lease(connection)
        grant = releasable_unleased_grant(connection, attempt["id"])
        if grant is None:
            return False
        actor = runner._actor_principal(connection, ctx.campaign["id"])
        now = driver._stamp()
        if (
            reconcile_grant_row(connection, grant, settlement_cents=0, principal_id=actor, now=now)
            is None
        ):
            return False  # the reservation ledger cannot cover it: today's path decides
        runner._touch(
            connection,
            "campaign_stage_attempts",
            "id=?",
            (attempt["id"],),
            status="failed_transient",
            failure_class="t1_salad_unclaimed",
            failure_detail=reason[:500],
        )
        runner._settle(connection, attempt["id"], 0, "unclaimed_zero")
        released = connection.execute(
            "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
        ).fetchone()
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (ctx.campaign["id"],)
        ).fetchone()
        runner.store.record_runner_event(
            connection,
            campaign,
            EXECUTOR_REFUSED_RELEASE_EVENT,
            {
                "grant_id": grant["id"],
                "stage_key": attempt["stage_key"],
                "attempt": attempt["attempt"],
                "attempt_id": attempt["id"],
                "reason": reason[:80],
                "lease_count": 0,
                "operation_count": 0,
                "settlement_cents": 0,
                "provider_termination_inferred": False,
                "operational_only": True,
                "automatic": True,
            },
            task_id=grant["task_id"],
        )
        record_slot_freed(
            connection,
            campaign,
            released,
            reason=EXECUTOR_REFUSED_SLOT,
            trigger="executor_refusal",
            record_event=runner.store.record_runner_event,
            grant_id=grant["id"],
        )
    return True
