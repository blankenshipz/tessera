"""Server-side campaign runner (ADR 0038).

``CampaignRunner.tick()`` is synchronous and idempotent: every decision is re-derived from
database rows. A delegated step is *intent* (own transaction), *effect* (the ordinary
store call, which completes the intent in the same transaction) and *advance* (this
module updates stage/attempt rows). A completed intent is found again on the next tick and
re-applied, so a crash between effect and advance loses nothing. The runner never starts
containers or calls a provider: workers do that through the existing worker routes.

The core is provider-neutral (ADR 0040): everything a compute provider decides (scaffolding,
grants, observation, cancel, settlement, receipt fields) goes through the
``campaign_providers.ComputeProvider`` protocol, and ``CampaignRunner`` is the narrow
``RunnerServices`` facade a provider may use. No stage is routed by comparing provider ids.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import campaign_class_fallback as class_fallback
from . import campaign_heavy_guard as heavy_guard
from . import campaign_hold, provider_trust
from . import campaign_holdout as holdout
from . import campaign_image_pull as image_pull
from . import campaign_placement as placement
from . import campaign_salad_fallback as salad_fallback
from . import execution_frames as frames
from . import late_bound_inputs as late_bound
from .artifact_manifests import grant_manifest_is_partial
from .campaign_autoreconcile import (
    MAX_AUTO_REQUEUES,
    NOT_STARTED,
    NotStarted,
    assess,
    not_started_count,
    repeat_count,
)
from .campaign_checkpoint import (
    CHECKPOINT_EVENT,
    COMPOSED_EVENT,
    composed_checkpoint,
    ever_composed,
    retry_checkpoint,
    retry_checkpoint_from,
    salvaged_checkpoint,
)
from .campaign_delegation import FORWARD_LEGAL_STATUSES, REDUCE_ONLY_STEPS, approver_is_live
from .campaign_exit_without_report import EXIT_REPORT_GRACE_SECONDS
from .campaign_exit_without_report import assess as assess_unreported_exit
from .campaign_exited_resolve import (
    UploadProgress,
    exit_recorded_at,
    hold_deadline,
    release_grant,
    uploads_arriving,
)
from .campaign_exited_resolve import assess as assess_exited
from .campaign_infra_retry import (
    INFRA_ALLOCATION_TIMEOUT,
    INFRA_CONTAINER_KILLED,
    INFRA_RESOURCE_LIMIT,
    REALLOCATED_PRECUSTODY,
    RESOURCE_LIMIT_CLASSES,
    SALAD_INTERRUPTED,
    infra_attempts,
    is_infra_class,
    reallocation_attempts,
    resource_limit_stop,
)
from .campaign_io import (
    IoEntry,
    output_entries,
    resolve_artifact,
    write_io,
)
from .campaign_leased_hold import (
    CONFIRMED_EXIT,
    HELD,
    OBSERVED,
    expired_leased,
    held_evidence,
    record_slot_held,
)
from .campaign_models import (
    EXPERIMENT_CEILING_CENTS,
    FAILURE_CLASSES,
    GRANT_SETTLE_MARGIN_SECONDS,
    NormalSpec,
    NormalStage,
    custody_inputs,
    needs_composition,
    normalize,
    parse_spec_json,
    placement_candidates,
    spec_digest,
    stage_input_class,
    stage_on_candidate,
    stage_provider,
)
from .campaign_providers import (
    Waiting,
    provider_ids,
)
from .campaign_receipts import (
    CODE_GRANTED_EVENT,
    build_receipt,
    code_delivery,
    code_summary,
    transfer_violations,
)
from .campaign_release_scope import (
    RELEASE_WAITING_EVENT,
    free_idle_campaign_releases,
    freed_by_pause,
    holder_refusal,
    idle_holders,
    release_waiters_ahead,
)
from .campaign_settlement import (
    BASES,
    FrameSettlement,
    SettlementRefused,
    settle_frame,
    validate_amount,
)
from .campaign_slot_reclaim import (
    EXPIRED_UNCLAIMED_GRANT,
    NOT_STARTED_EVIDENCE,
    PAUSED_RELEASE_DETAIL,
    PAUSED_UNLEASED_GRANT,
    SLOT_REACQUIRED_EVENT,
    STUCK_CAMPAIGN,
    idle_slot_holders,
    record_freed_slots,
    record_slot_freed,
    releasable_paused_grant,
    unclaimed_candidates,
    unclaimed_expired_grant,
    waiters_ahead,
)
from .campaign_steps import (
    DelegationLapsed,
    StepExecutor,
    StepFailed,
    StepResult,
    StepTransient,
)
from .campaign_store import CampaignStore, LicenseRefused
from .campaign_unclaimed import Refused, Unclaimed, automatic_count
from .campaign_unclaimed import assess as assess_unclaimed
from .campaign_unclaimed_recovery import requested as unclaimed_requested
from .code_approval import snapshot_code_approval
from .data_class import DataClassRefused
from .execution import reconcile_grant_row
from .experiment_budgets import budget_status
from .external_operations import current_operation_reconciliation
from .grant_release import campaign_actor, release_expired_terminal_grants
from .image_provenance import IMAGE_PROVENANCE_EVENT, image_provenance
from .provider_fetch_record import OUTPUT_NAME as PROVIDER_FETCH_OUTPUT
from .remote_execution_models import TargetPolicy
from .snapshot_composition import composed_inventory
from .store import Conflict, Forbidden, NotFound, _parse_utc, _utc_string, canonical_digest
from .workspace import inventory_digest

log = logging.getLogger("tessera.campaign_runner")

MAX_STEPS_PER_TICK = 64
# ADR 0157: automatic reconciles of unclaimed attempts one campaign may apply (0 disables).
DEFAULT_UNCLAIMED_AUTORECONCILE_LIMIT = 2
MAX_UNCLAIMED_AUTORECONCILE_LIMIT = 10
MAX_STAGE_LOOPS = 12
# The attempt-number range of campaign_stage_attempts.
MAX_ATTEMPT_NUMBERS = 5
# Step slot of a snapshot release renewed before its grant is issued (ADR 0048).
RELEASE_RENEWAL_ORDINAL = 2
RELEASE_SLACK_SECONDS = 120
RELEASE_MAX_SECONDS = 23 * 3600
SETTLE_MARGIN_SECONDS = GRANT_SETTLE_MARGIN_SECONDS
MAX_LINEAGE_EDGES = 256

TERMINAL_STAGE = frozenset(
    {"succeeded", "failed", "refused_budget", "blocked_unknown", "cancelled", "skipped"}
)
DEAD_STAGE = frozenset({"failed", "refused_budget", "blocked_unknown", "cancelled", "skipped"})
TERMINAL_ATTEMPT = frozenset(
    {
        "succeeded",
        "failed_transient",
        "failed_permanent",
        "unknown",
        "refused_budget",
        "cancelled",
    }
)
ACTIVE_ATTEMPT = frozenset(
    {
        "reserved",
        "granted",
        "admitted",
        "running",
        "exited",
        "collected",
        "resolved",
        "measured",
        "reconciled",
    }
)
_COMPARE = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
}


@dataclass(frozen=True)
class TickReport:
    actions: tuple[str, ...]
    campaigns: int


class _ExposureBlocked(Exception):
    """Raised inside the reserve transaction when provider exposure blocks a positive hold."""


class _LeaseLost(Exception):
    """Internal: another runner holds the lease; abandon the rest of this tick."""


# One class for the runner and for providers: a provider's ``Waiting`` is the runner's wait.
_Waiting = Waiting
_BASES = BASES


@dataclass
class _Ctx:
    campaign: dict[str, Any]
    spec: NormalSpec
    approval: dict[str, Any] | None
    generation: int


def _is_busy(error: sqlite3.OperationalError) -> bool:
    """Lock contention is transient; any other operational error is a real fault."""
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _iso(value: datetime) -> str:
    return _utc_string(value.replace(microsecond=0))


def _approval_digest(approval: dict[str, Any] | None) -> str | None:
    """The digest of a snapshot's recorded code approval (its files, approver, decision class
    and time), so the grant names which approval stood without copying file paths."""
    if approval is None:
        return None
    return canonical_digest({k: v for k, v in approval.items() if k != "valid"})


def _approval_expiry(ctx: _Ctx) -> datetime | None:
    return None if ctx.approval is None else _parse_utc(ctx.approval["expires_at"])


class CampaignRunner:
    # ADR 0158: control-clock seconds after a watcher-reported exit before the attempt may be
    # closed without a run report (validated against the closed bounds on every use).
    exit_report_grace_seconds: int = EXIT_REPORT_GRACE_SECONDS

    def __init__(
        self,
        state_dir: Path,
        *,
        now: Callable[[], datetime] | None = None,
        holder_id: str | None = None,
        lease_seconds: int = 30,
        scheduler: bool = True,
        unclaimed_autoreconcile_limit: int = DEFAULT_UNCLAIMED_AUTORECONCILE_LIMIT,
    ) -> None:
        if (
            type(unclaimed_autoreconcile_limit) is not int
            or not 0 <= unclaimed_autoreconcile_limit <= MAX_UNCLAIMED_AUTORECONCILE_LIMIT
        ):
            raise ValueError(
                "unclaimed_autoreconcile_limit must be an integer in "
                f"0..{MAX_UNCLAIMED_AUTORECONCILE_LIMIT}"
            )
        self.unclaimed_autoreconcile_limit = unclaimed_autoreconcile_limit
        self.state_dir = Path(state_dir)
        self.store = CampaignStore(self.state_dir)
        self.steps = StepExecutor(self.store, self.state_dir)
        self.now = now or (lambda: datetime.now(UTC))
        self.holder_id = holder_id or "runner_" + uuid.uuid4().hex
        self.lease_seconds = lease_seconds
        self.scheduler = scheduler
        self.provider_state: dict[str, Any] = {}
        self._budget = 0
        self._executed = False
        self._actions: list[str] = []
        # Settlements refused because the attempt's provider is not registered: recorded in
        # their own transaction once the refused one has rolled back (attempt -> provider).
        self._settlement_blocked: dict[str, str | None] = {}
        # Where the next expired-grant release scan carries on (ADR 0048).
        self._release_cursor: tuple[str, str] | None = None
        # Where the next idle-holder reconcile sweep carries on (ADR 0153).
        self._idle_cursor: str | None = None
        # Which open output uploads are still gaining bytes (ADR 0064).
        self.upload_progress = UploadProgress()

    # -- tick -----------------------------------------------------------------------

    def tick(self) -> TickReport:
        self._actions = []
        self._budget = MAX_STEPS_PER_TICK
        try:
            with self.store.runner_connection() as connection:
                if not self._lease(connection):
                    return TickReport((), 0)
                ids = [row["id"] for row in self.store.runnable_campaigns(connection)]
        except Forbidden:
            return TickReport((), 0)
        if self._schedule():
            with self.store.runner_connection() as connection:
                ids = [row["id"] for row in self.store.runnable_campaigns(connection)]
        try:
            self._release_expired_grants()
            self._free_idle_releases()
        except _LeaseLost:
            self._actions.append("lease_lost")
            return TickReport(tuple(self._actions), len(ids))
        for campaign_id in ids:
            if self._budget <= 0:
                break
            try:
                try:
                    self._drive(campaign_id)
                finally:
                    self._record_blocked_settlements()
            except _LeaseLost:
                self._actions.append("lease_lost")
                break
            except (StepTransient, sqlite3.OperationalError) as error:
                if isinstance(error, sqlite3.OperationalError) and not _is_busy(error):
                    # A schema or SQL fault is not contention: record it, never defer it.
                    log.error("campaign %s database error: %s", campaign_id, error)
                    self._actions.append(f"error:{campaign_id}:OperationalError")
                    continue
                log.warning("campaign %s step deferred: %s", campaign_id, type(error).__name__)
                self._actions.append(f"deferred:{campaign_id}")
            except Exception as error:  # noqa: BLE001 - one campaign must not stop the others
                log.error("campaign %s runner error: %s", campaign_id, type(error).__name__)
                self._actions.append(f"error:{campaign_id}:{type(error).__name__}")
        # Optional tick-level provider upkeep (``sweep``), for example confirming late
        # settlements of finished campaigns.
        for provider_id in provider_ids():
            sweep = getattr(stage_provider(provider_id), "sweep", None)
            if sweep is None:
                continue
            try:
                try:
                    sweep(self)
                finally:
                    self._record_blocked_settlements()
            except _LeaseLost:
                self._actions.append("lease_lost")
                break
            except StepTransient:
                pass
            except sqlite3.OperationalError as error:
                if not _is_busy(error):
                    log.error("provider %s sweep database error: %s", provider_id, error)
                    raise
        return TickReport(tuple(self._actions), len(ids))

    def _release_expired_grants(self) -> None:
        """Free the slot of every expired grant whose confirmed exit is recorded (ADR 0048)."""
        try:
            with self.store.runner_connection() as connection:
                self._hold_lease(connection)
                released, self._release_cursor = release_expired_terminal_grants(
                    connection, now=self.now(), after=self._release_cursor
                )
        except sqlite3.OperationalError as error:
            if not _is_busy(error):
                raise
            return
        for item in released:
            self._actions.append(f"grant_released:{item['grant_id']}")

    def _free_idle_releases(self) -> None:
        """Reconcile sweep: free releases still held by idle campaigns (every tick, bounded).

        Covers campaigns that went idle before releases were freed at the transition, and a
        blocked campaign whose last live attempt has since settled; also records the provider
        slots such campaigns no longer hold (ADR 0153)."""
        try:
            with self.store.runner_connection() as connection:
                self._hold_lease(connection)
                after = self._idle_cursor
                held, held_next = idle_holders(connection, after=after)
                slots, slots_next = idle_slot_holders(connection, after=after)
                # Both lists are id-ordered windows after the cursor. Visit ids up to the
                # nearer end of a full window (the rest come next pass); wrap around once
                # both windows come up short.
                ends = [end for end in (held_next, slots_next) if end is not None]
                cursor = min(ends) if ends else None
                holders = {*held, *slots}
                for campaign_id in sorted(holders):
                    if cursor is not None and campaign_id > cursor:
                        break
                    self._free_campaign_releases(connection, campaign_id, "reconcile_sweep")
        except sqlite3.OperationalError as error:
            if not _is_busy(error):
                raise
            return
        self._idle_cursor = cursor

    def _free_campaign_releases(
        self, connection: sqlite3.Connection, campaign_id: str, trigger: str
    ) -> None:
        """Free, in the caller's transaction, the releases an idle campaign no longer needs,
        and record the provider slots its unrequested attempts stop holding (ADR 0153)."""
        for item in record_freed_slots(
            connection, campaign_id, trigger=trigger, record_event=self.store.record_runner_event
        ):
            self._actions.append(f"slot_freed:{item['attempt_id']}")
        freed = free_idle_campaign_releases(
            connection,
            campaign_id,
            trigger=trigger,
            now=_iso(self.now()),
            record_event=self.store.record_runner_event,
        )
        for item in freed:
            self._actions.append(f"release_freed:{item['release_id']}")

    def _schedule(self) -> int:
        """Start standing-approval campaigns that are due (ADR 0042); returns the count."""
        if not self.scheduler:
            return 0
        from .campaign_standing import run_scheduler

        try:
            return run_scheduler(self)
        except _LeaseLost:
            self._actions.append("lease_lost")
        except sqlite3.OperationalError as error:
            if not _is_busy(error):
                raise
        return 0

    def _lease(self, connection: sqlite3.Connection) -> bool:
        now = self.now()
        expires = _iso(now + timedelta(seconds=self.lease_seconds))
        row = connection.execute(
            "SELECT holder_id, expires_at FROM campaign_runner_lease WHERE singleton=1"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO campaign_runner_lease (singleton, holder_id, expires_at) "
                "VALUES (1, ?, ?)",
                (self.holder_id, expires),
            )
            return True
        if row["holder_id"] == self.holder_id or _parse_utc(row["expires_at"]) <= now:
            connection.execute(
                "UPDATE campaign_runner_lease SET holder_id=?, expires_at=? WHERE singleton=1",
                (self.holder_id, expires),
            )
            return True
        return False

    def _renew_lease(self) -> None:
        """CAS the runner lease before a step; a lost lease aborts the tick."""
        now = self.now()
        with self.store.runner_connection() as connection:
            row = connection.execute(
                "SELECT holder_id, expires_at FROM campaign_runner_lease WHERE singleton=1"
            ).fetchone()
            if row is None or row["holder_id"] != self.holder_id:
                raise _LeaseLost
            claimed = connection.execute(
                """UPDATE campaign_runner_lease SET expires_at=?
                WHERE singleton=1 AND holder_id=? AND expires_at=?""",
                (
                    _iso(now + timedelta(seconds=self.lease_seconds)),
                    self.holder_id,
                    row["expires_at"],
                ),
            )
            if claimed.rowcount != 1:
                raise _LeaseLost

    # -- loading --------------------------------------------------------------------

    def _load(self, campaign_id: str) -> _Ctx | None:
        with self.store.runner_connection() as connection:
            row = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if row is None:
                return None
            campaign = dict(row)
            approval = connection.execute(
                "SELECT * FROM campaign_approvals WHERE campaign_id=? AND status='active'",
                (campaign_id,),
            ).fetchone()
            count = connection.execute(
                "SELECT count(*) FROM campaign_approvals WHERE campaign_id=?", (campaign_id,)
            ).fetchone()[0]
        try:
            model = parse_spec_json(campaign["spec_json"])
            valid = spec_digest(model) == campaign["spec_digest"]
            spec = normalize(model)
        except ValidationError:
            valid = False
        if not valid:
            self._finish(campaign, "failed", reason="spec_digest_mismatch")
            return None
        return _Ctx(campaign, spec, None if approval is None else dict(approval), max(count - 1, 0))

    def _campaign(self, campaign_id: str) -> dict[str, Any]:
        with self.store.runner_connection() as connection:
            return dict(
                connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            )

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.store.runner_connection() as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _stage_row(self, ctx: _Ctx, key: str) -> dict[str, Any]:
        row = self._one(
            "SELECT * FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
            (ctx.campaign["id"], key),
        )
        assert row is not None
        return row

    def _attempts(self, ctx: _Ctx, key: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=? "
            "ORDER BY attempt",
            (ctx.campaign["id"], key),
        )

    def _attempt(self, attempt_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt_id,))
        assert row is not None
        return row

    # -- driving one campaign -------------------------------------------------------

    def _drive(self, campaign_id: str) -> None:
        ctx = self._load(campaign_id)
        if ctx is None:
            return
        if ctx.campaign["status"] in STUCK_CAMPAIGN:
            # Reduce-only (ADR 0153): an expired grant no worker ever claimed cannot run, so a
            # stuck campaign stops holding its project slot; it needs no live approval.
            self._reclaim_unclaimed_grants(ctx)
        if self._paused_for_release(ctx):
            self._release_paused_grants(ctx)
        if ctx.approval is None:
            return
        status = ctx.campaign["status"]
        if status in STUCK_CAMPAIGN:
            # ADR 0153: the evidence paths only (observation, confirmed exit, not started).
            self._reconcile_stuck(ctx)
        else:
            # Reduce-only and evidence-based (ADR 0053).
            self._resolve_exited_attempts(ctx)
        self._close_unreported_exits(ctx)  # ADR 0158: after ADR 0053, which always wins
        if status == "paused_delegation_lapsed":
            return
        now = self.now()
        if status in {"running", "stopping"}:
            with self.store.runner_connection() as connection:
                live = approver_is_live(connection, ctx.approval)
            if not live or now >= _parse_utc(ctx.approval["expires_at"]):
                self._pause(ctx, "approver_not_live" if not live else "approval_expired")
                return
        # The not_before hold (G-06): approval expiry above still bounds the wait; nothing starts.
        if campaign_hold.waiting(ctx.campaign["spec_json"], status, now) is not None:
            self._actions.append(f"waiting:{campaign_id}:{campaign_hold.REASON}")
            return
        try:
            if status == "cancel_requested":
                self._drive_cancel(ctx)
            elif status == "blocked":
                self._drive_blocked(ctx)
            else:
                self._drive_running(ctx)
        except DelegationLapsed:
            if status in {"running", "stopping"}:
                self._pause(ctx, "approver_not_live")

    def _paused_for_release(self, ctx: _Ctx) -> bool:
        """The campaign is paused: delegation lapsed, or an agent's or person's pause (ADR 0061)."""
        status = ctx.campaign["status"]
        if status == "paused_delegation_lapsed":
            return True
        if status != "running":
            return False
        steering = self._one(
            "SELECT paused FROM campaign_steering WHERE campaign_id=?", (ctx.campaign["id"],)
        )
        return bool(steering and steering["paused"])

    def _release_paused_grants(self, ctx: _Ctx) -> None:
        """A paused campaign gives back each grant no worker ever took (ADR 0167).

        Reduce-only and fail closed. A grant with any lease, operation, a recorded attempt
        operation or ``cancel_requested`` status is kept, as is any grant of a holdout stage
        or of a provider that is not a registered worker-grant provider. The release is the
        shared reconcile accounting (hold moved to settled at zero); the attempt ends
        ``failed_transient``/``not_started``, so it uses an attempt number but no scientific
        retry (ADR 0046). Resuming places a new attempt through the normal admission path."""
        with self.store.runner_connection() as connection:
            candidates = unclaimed_candidates(connection, ctx.campaign["id"])
        for attempt_id in candidates:
            attempt = self._attempt(attempt_id)
            try:
                spec_stage = self._spec_stage(ctx, attempt["stage_key"])
                kind = self._provider(spec_stage).descriptor.kind
            except KeyError:
                continue  # unregistered provider: fail closed, the grant stays
            if kind != "worker_grant" or spec_stage.holdout is not None:
                continue
            with self.store.runner_connection() as connection:
                self._hold_lease(connection)
                campaign = connection.execute(
                    "SELECT * FROM campaigns WHERE id=?", (ctx.campaign["id"],)
                ).fetchone()
                steering = connection.execute(
                    "SELECT paused FROM campaign_steering WHERE campaign_id=?",
                    (campaign["id"],),
                ).fetchone()
                paused = campaign["status"] == "paused_delegation_lapsed" or (
                    campaign["status"] == "running" and bool(steering and steering["paused"])
                )
                if not paused:
                    return
                grant = releasable_paused_grant(connection, attempt_id)
                if grant is None:
                    continue
                now = _iso(self.now())
                actor = self._actor_principal(connection, campaign["id"])
                if (
                    reconcile_grant_row(
                        connection, grant, settlement_cents=0, principal_id=actor, now=now
                    )
                    is None
                ):
                    continue  # the reservation ledger cannot cover it: a person reconciles
                self._touch(
                    connection,
                    "campaign_stage_attempts",
                    "id=?",
                    (attempt_id,),
                    status="failed_transient",
                    failure_class=NOT_STARTED,
                    failure_detail=PAUSED_RELEASE_DETAIL,
                )
                self._settle(connection, attempt_id, 0, "unclaimed_zero")
                released = connection.execute(
                    "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt_id,)
                ).fetchone()
                self.store.record_runner_event(
                    connection,
                    campaign,
                    "execution.paused_unleased_grant_released",
                    {
                        "grant_id": grant["id"],
                        "lease_count": 0,
                        "operation_count": 0,
                        "settlement_cents": 0,
                        "consumed_scientific_retry": False,
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
                    reason=PAUSED_UNLEASED_GRANT,
                    trigger="paused_campaign_release",
                    record_event=self.store.record_runner_event,
                    grant_id=grant["id"],
                )
            self._actions.append(f"slot_freed:{attempt_id}:{PAUSED_UNLEASED_GRANT}")
        self._free_paused_releases(ctx)

    def _free_paused_releases(self, ctx: _Ctx) -> None:
        """Give back the input releases a paused campaign no longer needs (ADR 0167).

        Same fail-closed rule as an idle campaign (``release_still_needed``): nothing is
        freed while any attempt using the release is in flight, ``unknown`` or unsettled, or a
        grant against it is unresolved. Idempotent: a revoked release is never active."""
        with self.store.runner_connection() as connection:
            self._hold_lease(connection)
            row = connection.execute(
                "SELECT status FROM campaigns WHERE id=?", (ctx.campaign["id"],)
            ).fetchone()
            steering = connection.execute(
                "SELECT paused FROM campaign_steering WHERE campaign_id=?", (ctx.campaign["id"],)
            ).fetchone()
            if row["status"] == "running" and not (steering and steering["paused"]):
                return
            freed = free_idle_campaign_releases(
                connection,
                ctx.campaign["id"],
                trigger="paused_campaign_release",
                now=_iso(self.now()),
                record_event=self.store.record_runner_event,
                steering_paused=True,
            )
        for item in freed:
            self._actions.append(f"release_freed:{item['release_id']}")

    def _reclaim_unclaimed_grants(self, ctx: _Ctx) -> None:
        """Reconcile at zero each expired grant of a stuck campaign that no worker claimed.

        The same evidence and accounting as the expired-unattempted reconcile route and the
        running campaign's own ``reconcile_unattempted`` step: the attempt ends
        ``failed_transient``/``t1_grant_expired_unleased`` settled at zero, the grant is
        reconciled (its hold moves to settled at zero) and the project slot it held is free.
        A grant that was leased, or has an operation, is left for the evidence path."""
        with self.store.runner_connection() as connection:
            candidates = unclaimed_candidates(connection, ctx.campaign["id"])
        for attempt_id in candidates:
            with self.store.runner_connection() as connection:
                self._hold_lease(connection)
                campaign = connection.execute(
                    "SELECT * FROM campaigns WHERE id=?", (ctx.campaign["id"],)
                ).fetchone()
                if campaign["status"] not in STUCK_CAMPAIGN:
                    return
                grant = unclaimed_expired_grant(connection, attempt_id, self.now())
                if grant is None:
                    continue
                now = _iso(self.now())
                actor = self._actor_principal(connection, campaign["id"])
                if (
                    reconcile_grant_row(
                        connection, grant, settlement_cents=0, principal_id=actor, now=now
                    )
                    is None
                ):
                    continue  # the reservation ledger cannot cover it: a person reconciles
                self._touch(
                    connection,
                    "campaign_stage_attempts",
                    "id=?",
                    (attempt_id,),
                    status="failed_transient",
                    failure_class="t1_grant_expired_unleased",
                    failure_detail="grant expired unclaimed while the campaign was not running",
                )
                self._settle(connection, attempt_id, 0, "unclaimed_zero")
                attempt = connection.execute(
                    "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt_id,)
                ).fetchone()
                self.store.record_runner_event(
                    connection,
                    campaign,
                    "execution.expired_unattempted_grant_reconciled",
                    {
                        "grant_id": grant["id"],
                        "expired_authority": True,
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
                    attempt,
                    reason=EXPIRED_UNCLAIMED_GRANT,
                    trigger="stuck_campaign_reconcile",
                    record_event=self.store.record_runner_event,
                    grant_id=grant["id"],
                )
            self._actions.append(f"slot_freed:{attempt_id}:{EXPIRED_UNCLAIMED_GRANT}")

    def _reconcile_stuck(self, ctx: _Ctx) -> None:
        """Drive a stuck campaign's attempts through the evidence paths only (ADR 0153).

        An expired leased grant is classified as the running flow's observation would; a
        confirmed exit (ADR 0053) and not-started evidence (ADR 0046) then settle it with their
        own accounting. Whatever no evidence settles keeps its slot and is reported once per
        spell. Nothing is started, cancelled or re-run and no lease is issued."""
        self._observe_stuck_leased(ctx)
        self._resolve_exited_attempts(ctx)
        if ctx.campaign["status"] == "blocked":
            # v0.104.0: evidence of spend already made still reaches the attempt while blocked
            # (reduce-only steps only; a paused campaign admits none, so it waits for resume).
            self._evidence_pass(ctx)
        for attempt in self._rows(
            """SELECT * FROM campaign_stage_attempts WHERE campaign_id=?
            AND status='unknown' ORDER BY stage_key, attempt""",
            (ctx.campaign["id"],),
        ):
            self._auto_reconcile(ctx, attempt["stage_key"], attempt)
        self._hold_stuck_leased(ctx)

    def _observe_stuck_leased(self, ctx: _Ctx) -> None:
        """Classify each expired leased worker-grant attempt of a stuck campaign as
        ``_do_observe`` would on an expired grant; a live grant is never touched."""
        with self.store.runner_connection() as connection:
            pairs = expired_leased(connection, ctx.campaign["id"], self.now(), OBSERVED)
        for attempt, _grant in pairs:
            attempt = dict(attempt)
            try:
                kind = self._provider(self._spec_stage(ctx, attempt["stage_key"])).descriptor.kind
            except KeyError:
                continue  # an unregistered provider: fail closed, the slot stays held
            if kind != "worker_grant":
                continue  # a route provider settles through its own guardian
            seen = self._observe(attempt)
            verdict = self._expired_observation(ctx, attempt, seen)
            if verdict is None:
                continue
            with self.store.runner_connection() as connection:
                self._hold_lease(connection)
                campaign = connection.execute(
                    "SELECT status FROM campaigns WHERE id=?", (ctx.campaign["id"],)
                ).fetchone()
                if campaign["status"] not in STUCK_CAMPAIGN:
                    return
                current = connection.execute(
                    "SELECT revision FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
                ).fetchone()
                # Nothing else can write while this transaction holds the lock, so an
                # unchanged observation and attempt mean the verdict still holds.
                if (
                    current["revision"] != attempt["revision"]
                    or self._observe_in(connection, attempt) != seen
                ):
                    continue
                self._touch(
                    connection,
                    "campaign_stage_attempts",
                    "id=? AND revision=?",
                    (attempt["id"], attempt["revision"]),
                    status="unknown",
                    failure_class=verdict[0],
                    failure_detail=verdict[1],
                    **({"operation_id": seen["operation_id"]} if seen["operation_id"] else {}),
                )
            self._actions.append(f"stuck_observed:{attempt['id']}:{verdict[0]}")

    def _expired_observation(
        self, ctx: _Ctx, attempt: dict[str, Any], seen: dict[str, Any]
    ) -> tuple[str, str] | None:
        """``_do_observe``'s classification of an attempt whose leased grant has expired:
        (failure_class, detail) when it would go ``unknown``, else None (keep waiting)."""
        if seen["grant"]["status"] not in ("active", "cancel_requested"):
            return None
        if seen["operation_id"] is None:
            if seen["leases"]:
                return "observed_unknown", "leased grant expired without an operation"
            return None
        reconciliation = seen["reconciliation"]
        state = reconciliation["observed_state"] if reconciliation else "unknown"
        confirmed = bool(reconciliation and reconciliation["termination_confirmed"])
        if state == "running":
            return None
        if state != "exited" or not confirmed:
            return "observed_unknown", f"observed {state}, termination confirmed={confirmed}"
        receipt, manifest = seen["receipt"], seen["manifest"]
        if (receipt is None or (manifest is None and receipt["exit_code"] == 0)) and (
            self._outputs_arriving(ctx, attempt, seen)
        ):
            return None  # outputs still uploading (ADR 0064)
        if receipt is None:
            return "missing_receipt", "exited without a run receipt before grant expiry"
        if manifest is None and receipt["exit_code"] == 0:
            return "missing_manifest", "successful exit without a quarantined output manifest"
        return None  # a complete run: the normal flow collects it on resume

    def _hold_stuck_leased(self, ctx: _Ctx) -> None:
        """One ``campaign.slot_held`` per expired leased grant no evidence settled, per spell."""
        with self.store.runner_connection() as connection:
            self._hold_lease(connection)
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (ctx.campaign["id"],)
            ).fetchone()
            if campaign["status"] not in STUCK_CAMPAIGN:
                return
            now = self.now()
            for attempt, grant in expired_leased(
                connection, campaign["id"], now, HELD, unheld=True
            ):
                item = record_slot_held(
                    connection,
                    campaign,
                    attempt,
                    grant,
                    held_evidence(connection, grant, now),
                    record_event=self.store.record_runner_event,
                )
                self._actions.append(f"slot_held:{attempt['id']}:{item['reason']}")

    def _pause(self, ctx: _Ctx, reason: str) -> None:
        with self.store.runner_connection() as connection:
            updated = connection.execute(
                """UPDATE campaigns SET status='paused_delegation_lapsed', updated_at=?
                WHERE id=? AND status IN ('running','stopping')""",
                (_iso(self.now()), ctx.campaign["id"]),
            )
            if updated.rowcount:
                self.store.record_runner_event(
                    connection, ctx.campaign, "campaign.delegation_lapsed", {"reason": reason}
                )
                self._free_campaign_releases(connection, ctx.campaign["id"], "campaign_paused")
        self._actions.append(f"paused:{ctx.campaign['id']}:{reason}")

    # -- step plumbing --------------------------------------------------------------

    def _step(
        self,
        ctx: _Ctx,
        stage_key: str | None,
        attempt: int | None,
        step: str,
        ordinal: int,
        build: Callable[[], tuple[dict[str, str], dict[str, Any]]],
        route_policy: TargetPolicy | None = None,
    ) -> StepResult:
        assert ctx.approval is not None
        self._renew_lease()
        if (
            step not in REDUCE_ONLY_STEPS
            and ctx.campaign["status"] in FORWARD_LEGAL_STATUSES
            and not self._still_forward_legal(ctx)
        ):
            # The campaign left running/stopping earlier in this pass: no forward step is
            # attempted, nothing is written, and the pass for this campaign ends (the tick
            # defers it). The attempt keeps its reservation and hold (ADR 0038 addendum).
            raise StepTransient(f"{step}: campaign no longer running")
        provider_id = None if stage_key is None else self._spec_stage(ctx, stage_key).provider
        result = self.steps.run(
            campaign=ctx.campaign,
            approval=ctx.approval,
            generation=ctx.generation,
            stage_key=stage_key,
            attempt=attempt,
            step=step,
            ordinal=ordinal,
            build=build,
            approver_is_live=approver_is_live,
            route_policy=route_policy,
            before_call=self._renew_lease,
            provider_id=provider_id,
        )
        if result.executed:
            self._budget -= 1
            self._executed = True
            self._actions.append(f"{step}:{stage_key or '-'}:{attempt or '-'}")
        return result

    def _hold_lease(self, connection: sqlite3.Connection) -> None:
        """Abort a state transition committed by a runner that no longer holds the lease."""
        row = connection.execute(
            "SELECT holder_id FROM campaign_runner_lease WHERE singleton=1"
        ).fetchone()
        if row is None or row["holder_id"] != self.holder_id:
            raise _LeaseLost

    def _touch(
        self, connection: sqlite3.Connection, table: str, where: str, params: tuple, **fields: Any
    ) -> None:
        self._hold_lease(connection)
        assignments = ", ".join(f"{name}=?" for name in fields)
        stamp = _iso(self.now())
        connection.execute(
            f"UPDATE {table} SET {assignments}{', ' if assignments else ''}revision=revision+1, "
            f"updated_at=? WHERE {where}",
            (*fields.values(), stamp, *params),
        )
        if table == "campaign_stage_attempts" and where.startswith("id=?"):
            # The attempt's frame follows its observed state in the same transaction.
            self._sync_frame(connection, params[0])

    def _set_stage(self, ctx: _Ctx, key: str, **fields: Any) -> None:
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
                **fields,
            )

    def _set_attempt(self, attempt_id: str, **fields: Any) -> None:
        with self.store.runner_connection() as connection:
            self._touch(connection, "campaign_stage_attempts", "id=?", (attempt_id,), **fields)

    def _spec_stage(self, ctx: _Ctx, key: str) -> NormalStage:
        """The stage as it runs now: on the candidate it was last placed on (ADR 0060).

        A stage that names one provider (no alternates, no ADR 0058 reroute targets) always runs
        on it, unchanged, unless an earlier attempt failed there before start with nothing spent
        and the stage lists another profile (ADR 0060 addendum): it then runs on the profile its
        recorded ``campaign.salad_profile_switched`` events name (ADR 0151)."""
        base = ctx.spec.stage(key)
        if base.has_candidates:
            row = self._one(
                "SELECT provider_id FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
            )
            base = self._on_candidate(base, None if row is None else row["provider_id"])
        # ADR 0151: a Salad stage that lists alternate profiles runs on the profile its recorded
        # pre-start switches on this candidate name (without alternates: unchanged, no read).
        return salad_fallback.current_stage(self, ctx, key, base)

    @staticmethod
    def _on_candidate(base: NormalStage, provider_id: str | None) -> NormalStage:
        if not provider_id:
            return base
        try:
            return stage_on_candidate(base, provider_id)
        except KeyError:  # never placed outside the approved list; keep the stage's own
            return base

    @staticmethod
    def _provider(spec_stage: NormalStage) -> Any:
        return stage_provider(spec_stage.provider)

    @staticmethod
    def _candidate_ids(ctx: _Ctx) -> list[str]:
        """Every provider any stage of the approved spec may run on (ADR 0060)."""
        return sorted({pid for s in ctx.spec.stages for pid in s.candidate_ids})

    def _providers(self, ctx: _Ctx) -> list[Any]:
        found = []
        for pid in self._candidate_ids(ctx):
            try:
                found.append(stage_provider(pid))
            except KeyError:  # dropped since approval: its stages fail in _advance_stage
                continue
        return found

    def _dropped_provider_exposure(
        self, connection: sqlite3.Connection, ctx: _Ctx, campaign_id: str
    ) -> bool:
        """True when a provider no longer registered still has an unsettled attempt.

        Such a provider cannot be asked about its exposure, so it is treated as open
        (fail closed) until the provider is registered again."""
        for pid in self._candidate_ids(ctx):
            try:
                stage_provider(pid)
                continue
            except KeyError:
                pass
            if connection.execute(
                """SELECT 1 FROM campaign_stage_attempts a JOIN campaign_stages s
                ON s.campaign_id=a.campaign_id AND s.stage_key=a.stage_key
                WHERE a.campaign_id=? AND COALESCE(a.provider_id, s.provider_id)=?
                AND a.settled_cents IS NULL LIMIT 1""",
                (campaign_id, pid),
            ).fetchone():
                return True
        return False

    # -- the facade providers use (RunnerServices) -----------------------------------

    def connection(self):
        """The runner connection (a context manager); writes need the held lease."""
        return self.store.runner_connection()

    def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return self._rows(sql, params)

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        return self._one(sql, params)

    def step(self, ctx, key, attempt, step, ordinal, build, route_policy=None):
        return self._step(ctx, key, attempt, step, ordinal, build, route_policy)

    def bound_inputs(self, ctx: _Ctx, key: str) -> tuple[late_bound.ResolvedStageInput, ...]:
        """The stage-output inputs bound when the stage started (ADR 0140): only resolved
        digests, sizes, classes and labels, never a reference a provider must resolve."""
        with self.store.runner_connection() as connection:
            bound = late_bound.bound_inputs(connection, ctx.campaign, key)
            # A retry of an interrupted attempt also gets that attempt's salvaged checkpoint,
            # bound by digest under its declared name; a stage with none starts clean.
            checkpoint = retry_checkpoint(connection, ctx.campaign, key)
        if checkpoint is None or any(item.name == checkpoint.name for item in bound):
            return bound
        return (*bound, checkpoint)

    def set_stage(self, ctx: _Ctx, key: str, **fields: Any) -> None:
        self._set_stage(ctx, key, **fields)

    def set_attempt(self, attempt_id: str, **fields: Any) -> None:
        self._set_attempt(attempt_id, **fields)

    def touch(
        self, connection: sqlite3.Connection, table: str, where: str, params: tuple, **fields: Any
    ) -> None:
        self._touch(connection, table, where, params, **fields)

    def hold_lease(self, connection: sqlite3.Connection) -> None:
        self._hold_lease(connection)

    def stamp(self) -> str:
        return _iso(self.now())

    @staticmethod
    def parse_utc(value: str) -> datetime:
        return _parse_utc(value)

    def campaign(self, campaign_id: str) -> dict[str, Any]:
        return self._campaign(campaign_id)

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._attempt(attempt_id)

    def stage_row(self, ctx: _Ctx, key: str) -> dict[str, Any]:
        return self._stage_row(ctx, key)

    def spec_stage(self, ctx: _Ctx, key: str) -> NormalStage:
        return self._spec_stage(ctx, key)

    def stage_for(self, attempt: Mapping[str, Any]) -> NormalStage:
        """The normalized spec stage an attempt row belongs to."""
        row = self._one("SELECT spec_json FROM campaigns WHERE id=?", (attempt["campaign_id"],))
        assert row is not None
        stage = normalize(parse_spec_json(row["spec_json"])).stage(attempt["stage_key"])
        # ``attempt`` may be a dict or an sqlite3.Row; both index by column name.
        return self._on_candidate(stage, attempt["provider_id"])

    def stage_failed(self, ctx: _Ctx, key: str, reason: str) -> bool:
        return self._stage_failed(ctx, key, reason)

    def record_event(
        self, connection: sqlite3.Connection, campaign: dict[str, Any], event_type: str, payload
    ) -> None:
        self.store.record_runner_event(connection, campaign, event_type, payload)

    def completed_step(
        self, connection, campaign_id: str, key: str, attempt: int, step: str, ordinal: int
    ):
        return self.steps.completed(connection, campaign_id, key, attempt, step, ordinal)

    def observe_grant_state(self, attempt: Mapping[str, Any]) -> dict[str, Any]:
        """Grant, lease, operation, reconciliation, receipt and manifest of an attempt."""
        return self._observe(dict(attempt))

    def observe_grant(self, ctx: _Ctx, stage: dict[str, Any], attempt: dict[str, Any]) -> bool:
        """Drive a worker-grant attempt from granted to exited (shared grant observation)."""
        return self._do_observe(ctx, stage, self._spec_stage(ctx, stage["stage_key"]), attempt)

    def revoke_grant(
        self, ctx: _Ctx, key: str, attempt: dict[str, Any], grant: dict[str, Any]
    ) -> None:
        self._revoke(ctx, key, attempt, grant)

    def settle_attempt(
        self, connection: sqlite3.Connection, attempt_id: str, cents: int, basis: str
    ) -> None:
        self._settle(connection, attempt_id, cents, basis)

    def emit_once(self, ctx: _Ctx, event_type: str, match: dict[str, Any], payload: dict) -> None:
        """One event per (type, match); the runner re-derives state every tick."""
        with self.store.runner_connection() as connection:
            self._emit_once_in(connection, ctx, event_type, match, payload)

    def _emit_once_in(
        self,
        connection: sqlite3.Connection,
        ctx: _Ctx,
        event_type: str,
        match: dict[str, Any],
        payload: dict,
    ) -> None:
        """``emit_once`` inside a transaction the caller already holds."""
        clauses = " AND ".join(f"json_extract(payload_json, '$.{name}') IS ?" for name in match)
        seen = connection.execute(
            f"""SELECT 1 FROM events WHERE project_id=? AND event_type=?
            AND json_extract(payload_json, '$.campaign_id') = ? AND {clauses} LIMIT 1""",
            (ctx.campaign["project_id"], event_type, ctx.campaign["id"], *match.values()),
        ).fetchone()
        if seen is None:
            self.store.record_runner_event(
                connection, ctx.campaign, event_type, {**match, **payload}
            )

    def awaiting_human(
        self,
        ctx: _Ctx,
        key: str,
        record: str | list[str],
        reason: str,
        attempt: int | None = None,
    ) -> None:
        records = [record] if isinstance(record, str) else list(record)
        self.emit_once(
            ctx,
            "campaign.stage_awaiting_human",
            {"stage_key": key, "attempt": attempt, "reason": reason},
            {"records": records},
        )

    # -- ledger ---------------------------------------------------------------------

    def _settle(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        settlement: int,
        basis: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        """Settle and close one attempt's frame exactly once (lease check, then the core).

        Fails closed when the attempt's provider is not registered: its settlement
        bookkeeping (``on_settled``) cannot run, so nothing settles, the step fails with
        ``settlement_unavailable`` and the refusal is recorded once this transaction has
        rolled back (``provider_unregistered``)."""
        self._hold_lease(connection)
        try:
            validate_amount(basis, settlement)
        except SettlementRefused as refused:
            raise StepFailed("reconcile_grant", "settlement_unavailable", str(refused)) from None
        row = self._attempt_context(connection, attempt_id)
        if row is None:
            raise NotFound(f"campaign attempt {attempt_id} not found")
        provider = self._provider_or_none(row["resolved_provider_id"])
        if provider is None:
            self._settlement_blocked[attempt_id] = row["resolved_provider_id"]
            raise StepFailed(
                "reconcile_grant",
                "settlement_unavailable",
                f"provider {row['resolved_provider_id']!r} is not registered",
            )
        # Provider bookkeeping tied to the settlement commits in the same transaction; a
        # registered provider without the hook has none.
        hook = getattr(provider, "on_settled", None)
        now = _iso(self.now())
        tenant = frames.Tenant(row["org_id"], row["project_id"])
        actor = frames.Actor("runner", self._actor_principal(connection, row["campaign_id"]))
        frame = self._open_frame(connection, attempt_id)
        if frame["state"] not in {"settling", "closed"}:
            frame = self._frame_terminal(connection, row, frame, tenant, actor, now)
        try:
            settle_frame(
                connection,
                frame["frame_id"],
                FrameSettlement(
                    tenant=tenant,
                    cents=settlement,
                    basis=basis,
                    # Deterministic, so a replay after a crash settles nothing a second time.
                    request_digest=canonical_digest([attempt_id, settlement, basis]),
                    now=now,
                    actor=actor,
                    # A verdict-based basis on an ambiguous billable frame is gated in
                    # settle_frame; the receipt names the provider that supplied the settlement.
                    evidence={
                        "campaign_id": row["campaign_id"],
                        "stage_key": row["stage_key"],
                        "settlement_source": row["resolved_provider_id"],
                        **(evidence or {}),
                    },
                    on_settled=hook,
                ),
            )
        except SettlementRefused as refused:
            raise StepFailed("reconcile_grant", "settlement_unavailable", str(refused)) from None

    # -- execution frames (ADR 0041) -------------------------------------------------

    @staticmethod
    def _attempt_context(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT a.*, COALESCE(a.provider_id, s.provider_id) AS resolved_provider_id,
            c.org_id, c.project_id, c.spec_digest, c.status AS campaign_status
            FROM campaign_stage_attempts a
            JOIN campaign_stages s ON s.campaign_id=a.campaign_id AND s.stage_key=a.stage_key
            JOIN campaigns c ON c.id=a.campaign_id WHERE a.id=?""",
            (attempt_id,),
        ).fetchone()

    @staticmethod
    def _actor_principal(connection: sqlite3.Connection, campaign_id: str) -> str:
        """Frame events are attributed like runner events: the approver, else the submitter."""
        actor = campaign_actor(connection, campaign_id)
        assert actor is not None
        return actor

    def _open_frame(self, connection: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
        """The attempt's stage frame, opened through ``reserved`` if it has none yet.

        A reservation opens it in the hold's own transaction; an attempt reserved before
        frames existed gets one when it is next settled."""
        row = self._attempt_context(connection, attempt_id)
        assert row is not None
        tenant = frames.Tenant(row["org_id"], row["project_id"])
        existing = frames.frame_for_attempt(connection, attempt_id, tenant)
        if existing is not None:
            return existing
        actor = frames.Actor("runner", self._actor_principal(connection, row["campaign_id"]))
        now = _iso(self.now())
        frame_id = frames.stage_frame_id(
            frames.campaign_frame_id(row["campaign_id"]), row["stage_key"], row["attempt"]
        )
        frame = frames.open_frame(
            connection,
            frame_id=frame_id,
            tenant=tenant,
            kind="stage",
            provider_id=row["resolved_provider_id"] or "unassigned",
            idempotency_key="tessera-frame-" + frame_id[:32],
            hold_cents=row["hold_cents"],
            actor=actor,
            now=now,
            parent_frame_id=frames.campaign_frame_id(row["campaign_id"]),
            campaign_attempt_id=attempt_id,
        )
        # The approved spec pins the stage's shape; its admission checks ran at approval.
        frame = frames.transition(
            connection,
            frame_id,
            tenant,
            to_state="validated",
            expected_revision=frame["revision"],
            trigger="approved_spec",
            actor=actor,
            now=now,
            fields={"shape_digest": canonical_digest([row["spec_digest"], row["stage_key"]])},
        )
        return frames.transition(
            connection,
            frame_id,
            tenant,
            to_state="reserved",
            expected_revision=frame["revision"],
            trigger="campaign_hold",
            actor=actor,
            now=now,
            evidence_digest=canonical_digest([attempt_id, row["hold_cents"]]),
        )

    @staticmethod
    def _outcome_evidence(row: Mapping[str, Any]) -> str:
        return canonical_digest(
            {
                name: row[name]
                for name in (
                    "id",
                    "status",
                    "grant_id",
                    "operation_id",
                    "run_receipt_digest",
                    "exit_code",
                    "manifest_digest",
                    "failure_class",
                )
            }
        )

    @staticmethod
    def _bindings(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "grant_id": row["grant_id"],
            "operation_id": row["operation_id"],
            "admitted_attempt_id": row["admitted_attempt_id"],
        }

    def _sync_frame(self, connection: sqlite3.Connection, attempt_id: str) -> None:
        """Move the frame forward to what the attempt row shows; never past a terminal.

        A grant is the create intent (``creating``); an operation is the provider handle
        (``running``); a confirmed exit with a run receipt is the terminal outcome. An
        unknown observation flags the frame ambiguous. Anything else waits for settlement."""
        row = self._attempt_context(connection, attempt_id)
        if row is None:
            return
        tenant = frames.Tenant(row["org_id"], row["project_id"])
        frame = frames.frame_for_attempt(connection, attempt_id, tenant)
        if frame is None or frame["state"] in {"settling", "closed"}:
            return
        actor = frames.Actor("runner", self._actor_principal(connection, row["campaign_id"]))
        now = _iso(self.now())
        bindings = self._bindings(row)
        if frame["state"] == "reserved" and row["grant_id"] is not None:
            frame = frames.transition(
                connection,
                frame["frame_id"],
                tenant,
                to_state="creating",
                expected_revision=frame["revision"],
                trigger="grant_issued",
                actor=actor,
                now=now,
                evidence_digest=canonical_digest([row["grant_id"]]),
                fields={**bindings, "create_intent_id": row["grant_id"]},
            )
        if frame["state"] == "creating" and row["operation_id"] is not None:
            frame = frames.transition(
                connection,
                frame["frame_id"],
                tenant,
                to_state="running",
                expected_revision=frame["revision"],
                trigger="operation_bound",
                actor=actor,
                now=now,
                evidence_digest=canonical_digest([row["operation_id"]]),
                fields={**bindings, "provider_handle": row["operation_id"]},
            )
        if (
            frame["state"] == "running"
            and row["status"] == "exited"
            and row["exit_code"] is not None
            and row["run_receipt_digest"] is not None
        ):
            outcome = "succeeded" if row["exit_code"] == 0 else "failed"
            evidence = self._outcome_evidence(row)
            frame = frames.transition(
                connection,
                frame["frame_id"],
                tenant,
                to_state=outcome,
                expected_revision=frame["revision"],
                trigger="exit_confirmed",
                actor=actor,
                now=now,
                evidence_digest=evidence,
                fields={**bindings, "outcome": outcome, "outcome_evidence_digest": evidence},
            )
        if row["status"] == "unknown" and frame["state"] in frames.AMBIGUOUS_STATES:
            frames.flag_ambiguous(
                connection,
                frame["frame_id"],
                tenant,
                reason="observed_unknown",
                expected_revision=frame["revision"],
                actor=actor,
                now=now,
                evidence_digest=self._outcome_evidence(row),
            )

    def _frame_terminal(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        frame: dict[str, Any],
        tenant: frames.Tenant,
        actor: frames.Actor,
        now: str,
    ) -> dict[str, Any]:
        """Bring a frame to its terminal outcome at settlement time.

        The outcome comes from the attempt as settled: no grant ever issued is ``cancelled``
        before any create intent; a grant that never bound an operation is ``lost`` (the
        reconcile that settles it is the absence proof); a run with a confirmed zero exit
        is ``succeeded``; a cancel is ``cancelled``; a nonzero exit is ``failed``; a run with no
        observed exit is ``lost``."""
        self._sync_frame(connection, row["id"])
        frame = frames.get_frame(connection, frame["frame_id"], tenant)
        if frame["state"] in frames.TERMINAL_STATES:
            return frame
        ran = row["operation_id"] is not None or frame["state"] == "running"
        if row["failure_class"] == NOT_STARTED and frame["state"] in frames.AMBIGUOUS_STATES:
            # ADR 0046: control-plane records show nothing ever started, so the frame is
            # closed as lost (no outcome is claimed) and settles zero with that evidence.
            outcome = "lost"
        elif row["grant_id"] is None and frame["state"] in {"requested", "validated", "reserved"}:
            outcome = "cancelled"
        elif not ran:
            outcome = "lost"
        elif (
            row["exit_code"] == 0
            and row["run_receipt_digest"] is not None
            and row["status"]
            not in {"failed_transient", "failed_permanent", "unknown", "cancelled"}
        ):
            outcome = "succeeded"
        elif row["exit_code"] is not None and row["exit_code"] != 0:
            outcome = "failed"
        elif row["status"] == "cancelled" or row["campaign_status"] == "cancel_requested":
            outcome = "cancelled"
        elif row["exit_code"] is None:
            # No exit was observed (an unknown attempt a human reconciled): never claim a
            # failure nobody saw. ``lost`` keeps the ambiguity until the frame closes.
            outcome = "lost"
        else:
            outcome = "failed"
        evidence = self._outcome_evidence(row)
        fields = {**self._bindings(row), "outcome": outcome, "outcome_evidence_digest": evidence}
        if row["grant_id"] is not None:
            fields["create_intent_id"] = row["grant_id"]
        if row["operation_id"] is not None:
            fields["provider_handle"] = row["operation_id"]
        return frames.drive_to(
            connection,
            frame["frame_id"],
            tenant,
            outcome,
            trigger="settlement_outcome",
            actor=actor,
            now=now,
            evidence_digest=evidence,
            fields=fields,
        )

    def _record_blocked_settlements(self) -> None:
        """Record each settlement refused for an unregistered provider, once per attempt:
        a ``campaign.settlement_unavailable`` event and the frame's ``provider_unregistered``
        ambiguity flag, in a transaction of their own."""
        pending, self._settlement_blocked = self._settlement_blocked, {}
        for attempt_id, provider_id in sorted(pending.items(), key=lambda item: item[0]):
            try:
                with self.store.runner_connection() as connection:
                    self._hold_lease(connection)
                    row = self._attempt_context(connection, attempt_id)
                    if row is None or row["settled_cents"] is not None:
                        continue
                    frame = self._open_frame(connection, attempt_id)
                    if frame["ambiguous_reason"] == frames.PROVIDER_UNREGISTERED:
                        continue  # already recorded
                    actor = frames.Actor(
                        "runner", self._actor_principal(connection, row["campaign_id"])
                    )
                    frames.flag_ambiguous(
                        connection,
                        frame["frame_id"],
                        frames.Tenant(row["org_id"], row["project_id"]),
                        reason=frames.PROVIDER_UNREGISTERED,
                        expected_revision=frame["revision"],
                        actor=actor,
                        now=_iso(self.now()),
                    )
                    campaign = dict(
                        connection.execute(
                            "SELECT * FROM campaigns WHERE id=?", (row["campaign_id"],)
                        ).fetchone()
                    )
                    self.store.record_runner_event(
                        connection,
                        campaign,
                        "campaign.settlement_unavailable",
                        {
                            "stage_key": row["stage_key"],
                            "attempt": row["attempt"],
                            "attempt_id": attempt_id,
                            "provider_id": provider_id,
                            "frame_id": frame["frame_id"],
                            "reason": frames.PROVIDER_UNREGISTERED,
                        },
                    )
                self._actions.append(f"settlement_unavailable:{attempt_id}")
            except _LeaseLost:
                return

    @staticmethod
    def _provider_or_none(provider_id: str | None) -> Any:
        try:
            return None if provider_id is None else stage_provider(provider_id)
        except KeyError:
            return None

    def _settlement_for(self, stage: NormalStage, attempt: Mapping[str, Any]) -> tuple[int, str]:
        """``(cents, basis)`` to settle a reconciled attempt, from its provider.

        A provider with no settlement source refuses rather than erase a real cost; a basis
        outside the closed core enum is a runner error that blocks the attempt, never stored.
        """
        provider = self._provider_or_none(stage.provider)
        if provider is None:
            raise StepFailed(
                "reconcile_grant",
                "settlement_unavailable",
                f"no settlement source for provider {stage.provider}",
            )
        settlement = provider.settle(self, dict(attempt))  # may raise Waiting
        if settlement.basis not in _BASES:
            self._block_unknown_basis(attempt)
            raise _Waiting
        return settlement.cents, settlement.basis

    def block_settlement_mismatch(self, attempt: Mapping[str, Any]) -> None:
        """A replayed settlement differs from the current one: the attempt is unknown, nothing
        is settled and the campaign blocks, all in one transaction."""
        detail = "recorded settlement differs from the current settlement"
        with self.store.runner_connection() as connection:
            self._hold_lease(connection)
            self._touch(
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
                'settlement_mismatch'), updated_at=? WHERE id=? AND status='running'""",
                (_iso(self.now()), attempt["campaign_id"]),
            ).rowcount
            if blocked:
                self.store.record_runner_event(
                    connection, campaign, "campaign.blocked", {"reason": "settlement_mismatch"}
                )
                self._free_campaign_releases(connection, campaign["id"], "campaign_blocked")

    def _block_unknown_basis(self, attempt: Mapping[str, Any]) -> None:
        self._set_attempt(
            attempt["id"],
            status="unknown",
            failure_class="observed_unknown",
            failure_detail="provider returned an unknown settlement basis",
        )

    # -- running campaigns ----------------------------------------------------------

    def _drive_running(self, ctx: _Ctx) -> None:
        providers = self._providers(ctx)
        for provider in providers:
            provider.after_tick(self, ctx)
        if providers and self._campaign(ctx.campaign["id"])["status"] not in {
            "running",
            "stopping",
        }:
            return
        # Evidence first (v0.104.0): a run recorded during a block, or late-imported, is
        # verified before any stage's silent-executor clock is read.
        self._evidence_pass(ctx, providers)
        order = self._rows(
            "SELECT stage_key FROM campaign_stages WHERE campaign_id=? ORDER BY topo_index",
            (ctx.campaign["id"],),
        )
        for entry in order:
            if self._budget <= 0:
                return
            if not self._still_forward_legal(ctx):
                # A stage of this pass blocked the campaign (an unknown attempt, a fallback
                # hold, a settlement mismatch) or a person cancelled it: the pass ends here, so
                # no later stage takes a forward step it would be refused (ADR 0038 addendum).
                self._actions.append(f"pass_ended:{ctx.campaign['id']}")
                return
            if self._drive_stage_cancel(ctx, entry["stage_key"]):
                continue
            self._unwind_failed(ctx, entry["stage_key"])
            for _ in range(MAX_STAGE_LOOPS):
                self._executed = False
                if not self._advance_stage(ctx, entry["stage_key"]) or self._executed:
                    break
                if not self._still_forward_legal(ctx):
                    break
        self._maybe_complete(ctx)

    def _evidence_pass(self, ctx: _Ctx, providers: list[Any] | None = None) -> None:
        """Each provider's optional evidence-only hook (``evidence_pass``): verification of
        recorded outcomes and reduce-only steps only, never a forward step."""
        for provider in self._providers(ctx) if providers is None else providers:
            hook = getattr(provider, "evidence_pass", None)
            if hook is None:
                continue
            try:
                hook(self, ctx)
            except _Waiting:
                continue

    def _still_forward_legal(self, ctx: _Ctx) -> bool:
        """The campaign, re-read now, still admits forward steps (``running``/``stopping``)."""
        return self._campaign(ctx.campaign["id"])["status"] in FORWARD_LEGAL_STATUSES

    def _drive_stage_cancel(self, ctx: _Ctx, key: str) -> bool:
        """Unwind one stage a steering request cancelled; True while it owns the stage (ADR 0061).

        The same grant revoke and reconcile path as a campaign cancel runs for this stage only;
        stages that read its output are then skipped like any other dead dependency.
        """
        request = self._one(
            "SELECT * FROM campaign_stage_cancels WHERE campaign_id=? AND stage_key=?",
            (ctx.campaign["id"], key),
        )
        if request is None:
            return False
        stage = self._stage_row(ctx, key)
        if stage["status"] in TERMINAL_STAGE:
            self._finish_stage_cancel(ctx, key, request)
            return True
        if stage["status"] == "pending":
            self._set_stage(ctx, key, status="cancelled")
        elif self._cancel_stage_attempts(ctx, key):
            return True
        self._finish_stage_cancel(ctx, key, request)
        return True

    def _finish_stage_cancel(self, ctx: _Ctx, key: str, request: dict[str, Any]) -> None:
        if request["completed_at"] is not None:
            return
        stage = self._stage_row(ctx, key)
        with self.store.runner_connection() as connection:
            connection.execute(
                "UPDATE campaign_stage_cancels SET completed_at=? WHERE campaign_id=? "
                "AND stage_key=? AND completed_at IS NULL",
                (_iso(self.now()), ctx.campaign["id"], key),
            )
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "campaign.stage_cancelled",
                {"stage_key": key, "stage_status": stage["status"], "reason": request["reason"]},
            )

    def _stopping(self, ctx: _Ctx) -> bool:
        return self._campaign(ctx.campaign["id"])["stop_reason"] is not None

    def _advance_stage(self, ctx: _Ctx, key: str) -> bool:
        """Do one unit of progress; True when state changed and another loop may help."""
        if self._budget <= 0:
            return False
        stage = self._stage_row(ctx, key)
        spec_stage = self._spec_stage(ctx, key)
        status = stage["status"]
        if status in TERMINAL_STAGE:
            return False
        attempts = self._attempts(ctx, key)
        latest = attempts[-1] if attempts else None
        # ADR 0060: a stage with alternates is placed afresh whenever it needs a new attempt;
        # placement itself skips an unregistered candidate. Otherwise the stage's (or the live
        # attempt's) provider must still be registered.
        base = ctx.spec.stage(key)
        placing = (
            bool(base.alternates)
            or (base.has_candidates and placement.reroute_due(base, attempts))
            or class_fallback.prestart_attempts(attempts) > 0
        ) and (status == "pending" or latest is None or latest["status"] == "failed_transient")
        if not placing:
            try:
                self._provider(spec_stage)
            except KeyError:
                # The provider registry changed after approval: refuse the stage cleanly.
                return self._stage_failed(ctx, key, "provider_unavailable")
        stopping = self._stopping(ctx)
        if status == "pending":
            sources = [s.stage for s in spec_stage.inputs if s.kind == "stage"]
            states = [self._stage_row(ctx, name)["status"] for name in sources]
            if stopping or any(state in DEAD_STAGE for state in states):
                self._set_stage(ctx, key, status="skipped")
                return True
            if any(state != "succeeded" for state in states):
                return False
            return self._place_and_reserve(ctx, key, prepare=True)
        if latest is None:
            return self._place_and_reserve(ctx, key)
        attempt_status = latest["status"]
        if attempt_status == "succeeded":
            return self._stage_succeeded(ctx, key, latest)
        if attempt_status in {"failed_permanent"}:
            # A recorded exit code is part of the outcome the campaign reports (ADR 0063).
            code = latest["exit_code"]
            suffix = "" if code is None else f":exit_code={code}"
            # The stop event (if any) and the stage's state change commit in one transaction.
            with self.store.runner_connection() as connection:
                fallback = self._salad_profile_fallback(
                    ctx, key, stage, latest, stopping, connection
                )
                if fallback.action == salad_fallback.HOLD:
                    # The strict rule (ADR 0169): no profile move after a possible start; the
                    # stage is held for an explicit retry decision with one stop event.
                    self._mark_stage_unknown(connection, ctx, key, fallback.outcome)
                else:
                    self._mark_stage_failed(
                        connection, ctx, key, f"{latest['failure_class']}{suffix}"
                    )
            if fallback.action == salad_fallback.HOLD:
                self._actions.append(f"blocked:{key}")
                return False
            return True
        if attempt_status == "refused_budget":
            return False
        if attempt_status == "cancelled":
            self._set_stage(ctx, key, status="skipped")
            return True
        if attempt_status == "unknown":
            if self._auto_reconcile(ctx, key, latest):
                return True
            return self._stage_unknown(ctx, key)
        if attempt_status == "failed_transient":
            if spec_stage.holdout is not None and self._holdout_opened(ctx, key):
                # ADR 0068: once the claim opened, the sealed partition was (or may have been)
                # released to a worker. No further attempt, not even a not-started requeue
                # (ADR 0046), may release it again: the stage fails and the claim is spent.
                return self._stage_failed(ctx, key, f"holdout_spent:{latest['failure_class']}")
            # An attempt that never started uses an attempt number, not a scientific retry.
            # An infrastructure retry (ADR 0058) has its own bound, not a scientific one, and so
            # does a pre-custody reallocation retry (ADR 0179) and a move to the next candidate
            # after a pre-start failure (ADR 0060 addendum).
            realloc_used = reallocation_attempts(attempts)
            moved = class_fallback.prestart_attempts(attempts)
            scientific = (
                stage["attempts_used"]
                - not_started_count(attempts)
                - infra_attempts(attempts)
                - realloc_used
                - moved
            )
            if (
                latest["failure_class"] == REALLOCATED_PRECUSTODY
                and realloc_used > spec_stage.retry.max_reallocation_retries
            ):
                # ADR 0179: the bound on fresh attempts after a pre-custody reallocation.
                return self._stage_failed(ctx, key, f"{REALLOCATED_PRECUSTODY}_exhausted")
            if (
                not stopping
                and latest["failure_class"] == class_fallback.PRESTART_NO_SPEND
                and (
                    moved + self._timeout_move_count(ctx, key) > base.retry.max_class_fallbacks
                    or stage["attempts_used"] >= MAX_ATTEMPT_NUMBERS
                )
            ):
                return self._stage_failed(ctx, key, class_fallback.EXHAUSTED)
            if (
                stopping
                or scientific >= spec_stage.retry.max_attempts
                or stage["attempts_used"] >= MAX_ATTEMPT_NUMBERS
            ):
                return self._stage_failed(ctx, key, f"{latest['failure_class']}_exhausted")
            return self._place_and_reserve(ctx, key)
        try:
            return self._advance_attempt(ctx, stage, spec_stage, latest)
        except StepFailed as error:
            self._attempt_failed(ctx, key, latest, error.error_class, error.detail)
            return True
        except _Waiting:
            return False

    def _timeout_move_count(self, ctx: _Ctx, key: str) -> int:
        """Allocation-timeout profile moves of the stage (they share
        ``retry.max_class_fallbacks`` with the pre-start moves)."""
        with self.store.runner_connection() as connection:
            return len(salad_fallback.timeout_moves(connection, ctx.campaign, key))

    def _salad_profile_fallback(
        self,
        ctx: _Ctx,
        key: str,
        stage: Mapping[str, Any],
        latest: Mapping[str, Any],
        stopping: bool,
        connection: sqlite3.Connection,
    ) -> salad_fallback.Verdict:
        """ADR 0151 with the strict rule of ADR 0169: after a failed attempt of a stage that lists
        alternate profiles, whether the stage is held for an explicit retry decision (the run
        ended unstable after a possible start, or a pre-start move was not covered) or nothing
        changes. It never moves the stage to another profile.

        Only a stage whose approved spec lists alternate profiles (or whose profiles the operator
        chains fallbacks after, when its ``max_class_fallbacks`` allows a move) is read; a
        stopping campaign
        keeps the existing behavior. Runs in ``connection``, where the caller applies the
        verdict to the stage."""
        base = ctx.spec.stage(key)
        approved = self._on_candidate(base, stage["provider_id"]) if base.has_candidates else base
        if not salad_fallback.reads_fallback(approved) or stopping:
            return salad_fallback.Verdict(None)
        return salad_fallback.after_failure(self, ctx, key, approved, dict(latest), connection)

    # -- placement (ADR 0060) ---------------------------------------------------------

    def _place_and_reserve(self, ctx: _Ctx, key: str, *, prepare: bool = False) -> bool:
        """Place the stage's next attempt on an approved candidate, prepare it there and
        reserve its hold. A stage that names one provider keeps the single-provider path.

        ADR 0060 section 5a: ``alternate_providers`` and ADR 0058's ``retry.reroute_providers``
        are one candidate list, so an infrastructure retry is placed here like any attempt. A
        stage with reroute targets but no alternates is placed only when a reroute can apply;
        its other attempts keep the single-provider path exactly as ADR 0058 had it."""
        if self._reservation_held_back(ctx, key):
            return False
        base = ctx.spec.stage(key)
        attempts = self._attempts(ctx, key)
        # ADR 0060 addendum: after a pre-start failure the next attempt is placed (every
        # check runs again on the next class), even for a stage that names one provider.
        moved = class_fallback.prestart_attempts(attempts) > 0
        if not moved and (
            not base.has_candidates
            or (not base.alternates and not placement.reroute_due(base, attempts))
        ):
            floor = self._single_provider_floor(base)
            if floor is not None:
                # The one provider's executor image is below the stage's runtime floor: refused
                # with the closed reason before anything is prepared or held (never retried).
                # Placement checks the floor before input admission and the image pull size.
                number = self._stage_row(ctx, key)["attempts_used"] + 1
                refused = placement.Decision((base.provider,), None, (floor,))
                return self._placement_refused(ctx, key, number, refused)
            refused = self._image_pull_preflight(ctx, key, base)
            if refused is not None:
                return refused
            verdict = self._single_provider_guard(ctx, key, base)
            if verdict != "go":
                return verdict == "failed"
            if prepare:
                progress = self._provider(base).prepare(self, ctx, key)
                if progress != "ready":
                    return progress == "progress"
            return self._reserve(ctx, key)
        stage_row = self._stage_row(ctx, key)
        number = stage_row["attempts_used"] + 1
        with self.store.runner_connection() as connection:
            record = placement.recorded(connection, ctx.campaign, key, number)
        if record is None:
            decision = self._decide_placement(ctx, key, base, stage_row)
            if decision.chosen is None:
                return self._placement_refused(ctx, key, number, decision)
            self._record_placement(ctx, key, number, decision)
            chosen = decision.chosen
        else:
            chosen = record["provider_id"]  # binding for this attempt number once recorded
        spec_stage = self._on_candidate(base, chosen)
        try:
            provider = self._provider(spec_stage)
        except KeyError:
            return self._stage_failed(ctx, key, "provider_unavailable")
        # A provider's preparation (Salad scaffolding, for example) may take several ticks or
        # wait for a person, so it runs before every reservation on this path until it says
        # ``ready``; nothing is reserved before. No earlier attempt is taken as proof that the
        # provider is still prepared (a stage may move A -> B -> A): ``prepare`` is idempotent
        # and returns ``ready`` without effects once its work stands (ComputeProvider).
        progress = provider.prepare(self, ctx, key)
        if progress != "ready":
            return progress == "progress"
        return self._reserve(ctx, key)

    def _image_pull_preflight(self, ctx: _Ctx, key: str, base: NormalStage) -> bool | None:
        """The image pull-size preflight for a stage that is not placed: the recorded section,
        once per attempt number. None means go on; a strict provider's over-limit image ends
        the stage (True: the tick made progress), after the trust and licence checks."""
        try:
            section = image_pull.evaluate(self._provider(base), base)
        except KeyError:
            return None
        if section is None:
            return None
        if section["status"] == "refused":
            # Trust and licence refusals come first, as at placement: a stage whose inputs may
            # not go to the provider reports that, never an image-size refusal.
            refused = self._inputs_refused(ctx, key, record=False)
            if refused is not None:
                return refused
        number = self._stage_row(ctx, key)["attempts_used"] + 1
        with self.store.runner_connection() as connection:
            self._emit_once_in(
                connection,
                ctx,
                image_pull.EVENT,
                {"stage_key": key, "attempt": number},
                {
                    k: v
                    for k, v in image_pull.event_payload(
                        key, number, base.provider, section
                    ).items()
                    if k not in ("stage_key", "attempt")
                },
            )
        if section["status"] == "refused":
            return self._stage_failed(ctx, key, image_pull.REFUSED_REASON)
        return None

    def _single_provider_floor(self, base: NormalStage) -> placement.Skip | None:
        """The runtime image floor for a stage that is not placed: its own provider's image
        must declare what the stage's pinned-fetch manifest needs (``runtime_floor_skip``)."""
        try:
            provider = self._provider(base)
        except KeyError:
            return None  # the registry check upstream fails the stage with its own reason
        return placement.runtime_floor_skip(provider, base)

    def _single_provider_guard(self, ctx: _Ctx, key: str, base: NormalStage) -> str:
        """ADR 0107 for a stage that is not placed (one provider): a heavy stage on a local
        provider while a remote profile is registered records its warning once per attempt
        number (warn) or waits with the reason (refuse): ``go`` or ``wait``. With no explicit
        fallback reason (amendment 2026-11-23b) the stage fails instead (``failed``) unless the
        operator set ``heavy_unexplained_local: warn``."""
        with self.connection() as connection:
            guard = heavy_guard.applies(connection, ctx.campaign, base)
        if guard is None or not guard.is_local(base.provider):
            return "go"
        number = self._stage_row(ctx, key)["attempts_used"] + 1
        attempts = self._attempts(ctx, key)
        # A remote listed only in ``retry.reroute_providers`` is skipped as on the placement
        # path (``infra_retry_only``), so both paths give the same reasons.
        _, excluded = placement.candidate_order(base, attempts)
        codes, detail = heavy_guard.reasons(
            guard, base, base.provider, excluded.values(), None, None, attempts
        )
        if guard.refuses_unexplained(codes, detail):
            block = heavy_guard.record(
                guard, codes, detail, refused=True, code=heavy_guard.UNEXPLAINED_REASON
            )
            self._heavy_local_refused(ctx, key, number, base.provider, block)
            return "failed"
        with self.store.runner_connection() as connection:
            if guard.mode == "refuse":
                block = heavy_guard.record(guard, codes, detail, refused=True)
                skip = placement.Skip(base.provider, heavy_guard.REFUSED_REASON, ",".join(codes))
                self._emit_once_in(
                    connection,
                    ctx,
                    placement.WAITING_EVENT,
                    {
                        "stage_key": key,
                        "attempt": number,
                        "signature": f"{skip.provider_id}:{skip.reason}",
                    },
                    {"skipped": [skip.payload()], "steering": None, "heavy_guard": block},
                )
                return "wait"
            block = heavy_guard.record(guard, codes, detail)
            self._emit_once_in(
                connection,
                ctx,
                heavy_guard.EVENT,
                {"stage_key": key, "attempt": number},
                {
                    k: v
                    for k, v in heavy_guard.event_payload(key, number, base.provider, block).items()
                    if k not in ("stage_key", "attempt")
                },
            )
        return "go"

    def _heavy_local_refused(
        self, ctx: _Ctx, key: str, number: int, provider_id: str, block: Mapping[str, Any]
    ) -> bool:
        """ADR 0107 amendment 2026-11-23b: a heavy stage would land on ``provider_id`` (local)
        with no explicit fallback reason. One ``campaign.heavy_stage_local_refused`` event and
        the stage failure (stop reason ``heavy_local_unexplained``) in one transaction; nothing
        is placed, held or run."""
        with self.store.runner_connection() as connection:
            self._emit_once_in(
                connection,
                ctx,
                heavy_guard.REFUSED_EVENT,
                {"stage_key": key, "attempt": number},
                {
                    k: v
                    for k, v in heavy_guard.event_payload(key, number, provider_id, block).items()
                    if k not in ("stage_key", "attempt")
                },
            )
            self._mark_stage_failed(connection, ctx, key, heavy_guard.UNEXPLAINED_REASON)
        self._actions.append(f"heavy_local_refused:{key}:{number}")
        return True

    def _decide_placement(
        self, ctx: _Ctx, key: str, base: NormalStage, stage_row: Mapping[str, Any]
    ) -> placement.Decision:
        def exposure_open(connection: sqlite3.Connection) -> bool:
            return any(
                provider.exposure_open(connection, ctx.campaign["id"])
                for provider in self._providers(ctx)
            ) or self._dropped_provider_exposure(connection, ctx, ctx.campaign["id"])

        # Once the stage's task plan binds an execution spec, a later attempt may move only to
        # a candidate that runs the identical spec (same image, argv and limits).
        mismatched: dict[str, placement.Skip] = {}
        if stage_row["prep_phase"] not in (None, "none", "task_created", "input_ready"):
            digest = stage_row["input_snapshot_digest"] or "0" * 64
            try:
                current = self._on_candidate(base, stage_row["provider_id"])
                planned = self._provider(current).execution_spec(current, digest)
            except KeyError:
                planned = None
            for pid in base.candidate_ids if planned is not None else ():
                stage = self._on_candidate(base, pid)
                try:
                    same = self._provider(stage).execution_spec(stage, digest) == planned
                except KeyError:
                    continue  # unregistered: the decision records that reason itself
                if not same:
                    mismatched[pid] = placement.Skip(
                        pid, "plan_mismatch", "differs from the stage's task plan"
                    )
        # ADR 0067: a candidate whose attempt ended on a resource limit is not chosen again
        # unless its free memory is shown above the limit (a time-budget stop never is).
        attempts = self._attempts(ctx, key)
        if attempts and attempts[-1]["failure_class"] in RESOURCE_LIMIT_CLASSES:
            for pid in base.candidate_ids:
                blocked = self._resource_limit_retry_blocked(ctx, key, attempts, pid)
                if blocked is not None:
                    mismatched.setdefault(
                        pid,
                        placement.Skip(
                            pid,
                            "resource_limit_last_attempt",
                            "ended on a resource limit; "
                            + (
                                "a time-budget stop is not repeated there"
                                if blocked["blocked_by"] == "time_budget_not_raisable"
                                else "free memory not shown above its limit"
                            ),
                        ),
                    )
        return placement.decide(
            self, ctx, base, exposure_open, extra_skip=mismatched, attempts=attempts
        )

    def _record_placement(
        self, ctx: _Ctx, key: str, number: int, decision: placement.Decision
    ) -> None:
        """The choice and every skip reason, with the stage's current provider, in one
        transaction (the receipt and tessera_get_campaign read them back)."""
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
                provider_id=decision.chosen,
            )
            self.store.record_runner_event(
                connection, ctx.campaign, placement.PLACED_EVENT, decision.payload(key, number)
            )
            if decision.heavy_guard is not None and decision.chosen is not None:
                # ADR 0107: the heavy stage's local fallback, once, with the placement.
                self.store.record_runner_event(
                    connection,
                    ctx.campaign,
                    heavy_guard.EVENT,
                    heavy_guard.event_payload(key, number, decision.chosen, decision.heavy_guard),
                )
            try:
                chosen = self._on_candidate(ctx.spec.stage(key), decision.chosen)
                section = image_pull.evaluate(self._provider(chosen), chosen)
            except KeyError:
                section = None
            if section is not None:
                self._emit_once_in(
                    connection,
                    ctx,
                    image_pull.EVENT,
                    {"stage_key": key, "attempt": number},
                    {
                        k: v
                        for k, v in image_pull.event_payload(
                            key, number, decision.chosen, section
                        ).items()
                        if k not in ("stage_key", "attempt")
                    },
                )
        self._actions.append(f"placed:{key}:{number}:{decision.chosen}")

    def _placement_refused(
        self, ctx: _Ctx, key: str, number: int, decision: placement.Decision
    ) -> bool:
        """No candidate may run now. Wait while a reason can clear on its own; otherwise end
        the stage as a single-provider stage would, never on an unapproved destination."""
        payload = decision.payload(key, number)
        if decision.waiting:
            signature = ",".join(f"{s.provider_id}:{s.reason}" for s in decision.skipped)
            waiting = {"skipped": payload["skipped"], "steering": payload["steering"]}
            if decision.heavy_guard is not None:
                waiting["heavy_guard"] = decision.heavy_guard  # ADR 0107 refusal
            self.emit_once(
                ctx,
                placement.WAITING_EVENT,
                {"stage_key": key, "attempt": number, "signature": signature},
                waiting,
            )
            return False
        budget = next((s for s in decision.skipped if s.reason == "over_campaign_budget"), None)
        if budget is not None:
            # A truthful budget refusal on the first candidate the cap refused: the recorded
            # attempt and stop reason are the single-provider ones (ADR 0038). ``prepare`` is
            # deliberately not run: the refused attempt takes no hold, release or grant and runs
            # nothing, so the provider is never asked to scaffold work that will not happen.
            self._record_placement(
                ctx,
                key,
                number,
                placement.Decision(
                    decision.candidates,
                    budget.provider_id,
                    tuple(s for s in decision.skipped if s is not budget),
                    decision.steering,
                    self._on_candidate(ctx.spec.stage(key), budget.provider_id).max_exposure_cents,
                    budget_refusal=True,
                    preference=decision.preference,
                ),
            )
            return self._reserve(ctx, key)
        self.emit_once(
            ctx,
            placement.WAITING_EVENT,
            {"stage_key": key, "attempt": number, "signature": "refused"},
            {
                "candidates": payload["candidates"],
                "skipped": payload["skipped"],
                "steering": payload["steering"],
                "final": True,
            },
        )
        reason = placement.failure_reason(decision)
        if reason == heavy_guard.UNEXPLAINED_REASON and decision.heavy_guard is not None:
            refused = next(s for s in decision.skipped if s.reason == reason)
            return self._heavy_local_refused(
                ctx, key, number, refused.provider_id, decision.heavy_guard
            )
        if reason == "resource_limit_no_provider":
            attempts = self._attempts(ctx, key)
            self.emit_once(
                ctx,
                "campaign.stage_resource_limit_no_provider",
                {"stage_key": key, "after_attempt": attempts[-1]["attempt"]},
                {
                    "reason": attempts[-1]["failure_class"],
                    "blocked_by": "no_other_candidate",
                    "skipped": payload["skipped"],
                },
            )
        return self._stage_failed(ctx, key, reason)

    # -- reservation ----------------------------------------------------------------

    def _reservation_held_back(self, ctx: _Ctx, key: str) -> bool:
        """ADR 0061: an agent's or person's pause and per-campaign concurrency limit.

        Both only delay NEW reservations of a stage; an attempt already running, its settlement,
        cancellation and the deadline are unaffected. Each wait is recorded once.
        """
        steering = self._one(
            "SELECT * FROM campaign_steering WHERE campaign_id=?", (ctx.campaign["id"],)
        )
        if steering is None:
            return False
        if steering["paused"]:
            self.emit_once(
                ctx,
                "campaign.stage_waiting_paused",
                {"stage_key": key, "pause_count": steering["pause_count"]},
                {},
            )
            return True
        limit = steering["max_concurrent_stages"]
        if limit is not None:
            marks = ",".join("?" for _ in ACTIVE_ATTEMPT)
            busy = self._one(
                f"""SELECT count(DISTINCT stage_key) AS n FROM campaign_stage_attempts
                WHERE campaign_id=? AND stage_key<>? AND status IN ({marks})""",
                (ctx.campaign["id"], key, *sorted(ACTIVE_ATTEMPT)),
            )
            if busy["n"] >= limit:
                self.emit_once(
                    ctx,
                    "campaign.stage_waiting_concurrency",
                    {"stage_key": key, "limit": limit},
                    {"running_stages": busy["n"]},
                )
                return True
        return False

    def _resource_limit_retry_blocked(
        self, ctx: _Ctx, key: str, attempts: list[dict[str, Any]], provider_id: str
    ) -> dict[str, Any] | None:
        """Why the stage may not run again on ``provider_id`` after a resource-limit stop
        (ADR 0067), or ``None`` when it may.

        Only a provider whose own attempt ended on a resource limit is held back. A stop on the
        stage's own time budget is never repeated there (free memory says nothing about time and
        the approved spec has no raised budget). A kill is repeated in place when the stage has
        no other approved place (ADR 0058), and otherwise only if that provider's free memory
        is shown to exceed the limit it hit: the limit from the run receipt of the attempt that
        ran there, else the worker's reported container limit; the free memory from the
        worker's fresh report. Unknown is not enough. The memory gate covers every code in
        ``infra_exit_codes``: control cannot tell an out-of-memory kill from another one.
        Both figures are the worker's own claims, never verified."""
        if not attempts or attempts[-1]["failure_class"] not in RESOURCE_LIMIT_CLASSES:
            return None
        base = ctx.spec.stage(key)
        hit = next(
            (
                a
                for a in reversed(attempts)
                if (a["provider_id"] or base.provider) == provider_id
                and a["failure_class"] in RESOURCE_LIMIT_CLASSES
            ),
            None,
        )
        if hit is None:
            return None
        if hit["failure_class"] == INFRA_RESOURCE_LIMIT:
            return {"blocked_by": "time_budget_not_raisable", "provider": provider_id}
        if not base.has_candidates:
            return None
        stage = self._on_candidate(base, provider_id)
        try:
            provider = self._provider(stage)
        except KeyError:
            return {"blocked_by": "provider_unregistered", "provider": provider_id}
        report = None
        hook = getattr(provider, "memory_report", None)
        if hook is not None:
            report = hook(self, ctx, stage)
        free, container = (None, None) if report is None else report
        limit = None
        if hit["operation_id"] is not None:
            receipt = self._one(
                "SELECT receipt_payload_json FROM execution_run_receipts WHERE operation_id=?",
                (hit["operation_id"],),
            )
            if receipt is not None:
                value = json.loads(receipt["receipt_payload_json"]).get("memory_bytes")
                limit = value if type(value) is int else None
        if limit is None:
            limit = container
        if free is not None and limit is not None and free > limit:
            return None
        return {
            "blocked_by": "memory_not_shown_sufficient",
            "provider": provider_id,
            "free_memory_bytes": free,
            "limit_bytes": limit,
        }

    def _resource_limit_ended(self, ctx: _Ctx, key: str, attempts: list[dict[str, Any]]) -> bool:
        """The stage's only place just stopped it on its time budget: record why and end the
        stage (bounded, never a silent loop)."""
        base = ctx.spec.stage(key)
        blocked = self._resource_limit_retry_blocked(ctx, key, attempts, base.provider)
        self.emit_once(
            ctx,
            "campaign.stage_resource_limit_no_provider",
            {"stage_key": key, "after_attempt": attempts[-1]["attempt"]},
            {"reason": attempts[-1]["failure_class"], **(blocked or {})},
        )
        return self._stage_failed(ctx, key, "resource_limit_no_provider")

    def _reserve(self, ctx: _Ctx, key: str) -> bool:
        if self._reservation_held_back(ctx, key):
            return False
        attempts = self._attempts(ctx, key)
        if (
            attempts
            and not ctx.spec.stage(key).has_candidates
            and self._resource_limit_retry_blocked(ctx, key, attempts, ctx.spec.stage(key).provider)
            is not None
        ):
            return self._resource_limit_ended(ctx, key, attempts)
        # Placed already (ADR 0060; an infrastructure retry's reroute too, ADR 0058).
        spec_stage = self._spec_stage(ctx, key)
        target = spec_stage.provider
        refused = self._inputs_refused(ctx, key)
        if refused is not None:
            return refused
        hold = spec_stage.max_exposure_cents
        if attempts and is_infra_class(attempts[-1]["failure_class"]):
            self.emit_once(
                ctx,
                "campaign.stage_infra_retry",
                {"stage_key": key, "after_attempt": attempts[-1]["attempt"]},
                {
                    "reason": attempts[-1]["failure_class"],
                    "infra_retries_used": infra_attempts(attempts),
                    "infra_retries_max": spec_stage.retry.infra_retries,
                    "provider": target,
                    "rerouted": target != (attempts[-1]["provider_id"] or spec_stage.provider),
                },
            )
        try:
            return self._reserve_hold(ctx, key, hold, spec_stage.provider)
        except _ExposureBlocked:
            # An attempt with unresolved provider exposure blocks new positive holds.
            self.emit_once(
                ctx,
                "campaign.stage_waiting_exposure",
                {"stage_key": key},
                {"hold_cents": hold},
            )
            return False

    def _inputs_refused(self, ctx: _Ctx, key: str, *, record: bool = True) -> bool | None:
        """ADR 0040 at reservation: nothing is held for a stage whose inputs the provider
        instance is not trusted to receive (a 409-class refusal naming provider and worker),
        nor (ADR 0113) for labels it may not receive. None means admitted; otherwise the stage
        failed as ``input_not_admitted`` (True). ``record`` records the labels as checked."""
        spec_stage = self._spec_stage(ctx, key)
        target = spec_stage.provider
        try:
            with self.store.runner_connection() as connection:
                checked = CampaignStore.check_stage_inputs(
                    connection,
                    spec_stage,
                    self._provider(spec_stage),
                    approval_digest=(ctx.approval or {}).get("trust_policy_digest"),
                    spec=ctx.spec,
                    scope=(ctx.campaign["org_id"], ctx.campaign["project_id"]),
                )
                if (
                    record
                    and checked is not None
                    and (
                        checked != {"unlabelled"}
                        or provider_trust.current_policy(connection).lists_license_labels(target)
                    )
                ):
                    # The labels as checked at dispatch, for the receipt (a later label add
                    # never rewrites what this stage was admitted with).
                    self._emit_once_in(
                        connection,
                        ctx,
                        "campaign.stage_license_checked",
                        {"stage_key": key, "provider": target},
                        {"labels": sorted(checked)},
                    )
        except LicenseRefused as error:
            # ADR 0113: a provider's licence-label restriction (tightened after approval, or a
            # label added since) refuses the inputs; on the record, nothing held.
            self.emit_once(
                ctx,
                "campaign.stage_license_refused",
                {"stage_key": key},
                {"provider": target, "labels": list(error.labels), "reason": str(error)[:300]},
            )
            return self._stage_failed(ctx, key, "input_not_admitted")
        except Conflict:
            return self._stage_failed(ctx, key, "input_not_admitted")
        return None

    def _reserve_hold(self, ctx: _Ctx, key: str, hold: int, provider_id: str | None = None) -> bool:
        provider_id = provider_id or ctx.spec.stage(key).provider
        refused_event: dict[str, Any] | None = None
        with self.store.runner_connection() as connection:
            campaign = dict(
                connection.execute(
                    "SELECT * FROM campaigns WHERE id=?", (ctx.campaign["id"],)
                ).fetchone()
            )
            stage = connection.execute(
                "SELECT * FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
                (campaign["id"], key),
            ).fetchone()
            if stage["status"] in TERMINAL_STAGE or campaign["status"] != "running":
                return False
            # Checked in the hold's own transaction, so no exposure can open in between.
            if (
                hold > 0
                and any(
                    provider.exposure_open(connection, campaign["id"])
                    for provider in self._providers(ctx)
                )
                or (hold > 0 and self._dropped_provider_exposure(connection, ctx, campaign["id"]))
            ):
                raise _ExposureBlocked
            number = stage["attempts_used"] + 1
            unknown = connection.execute(
                "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND status='unknown'",
                (campaign["id"],),
            ).fetchone() or self._experiment_exposure_unknown(connection, campaign, stage)
            over = (
                campaign["held_cents"] + campaign["settled_cents"] + hold
                > campaign["budget_cap_cents"]
            )
            now = _iso(self.now())
            attempt_id = "campaign_attempt_" + uuid.uuid4().hex
            refused = bool(unknown) or over
            connection.execute(
                """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt,
                status, hold_cents, settled_cents, failure_class, revision, created_at, updated_at,
                provider_id) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)""",
                (
                    attempt_id,
                    campaign["id"],
                    key,
                    number,
                    "refused_budget" if refused else "reserved",
                    hold,
                    0 if refused else None,
                    ("exposure_unknown" if unknown else "budget_refused") if refused else None,
                    now,
                    now,
                    provider_id,
                ),
            )
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=?",
                (campaign["id"], key),
                status="refused_budget" if refused else "preparing",
                attempts_used=number,
            )
            if refused:
                if campaign["stop_reason"] is None:
                    connection.execute(
                        "UPDATE campaigns SET stop_reason=?, updated_at=? WHERE id=?",
                        (f"budget_refused:{key}", now, campaign["id"]),
                    )
                refused_event = {
                    "stage_key": key,
                    "attempt": number,
                    "hold_cents": hold,
                    "held_cents": campaign["held_cents"],
                    "settled_cents": campaign["settled_cents"],
                    "budget_cap_cents": campaign["budget_cap_cents"],
                    "reason": "exposure_unknown" if unknown else "budget_refused",
                }
                self.store.record_runner_event(
                    connection, campaign, "campaign.stage_refused", refused_event
                )
            else:
                connection.execute(
                    "UPDATE campaigns SET held_cents=held_cents+? WHERE id=?",
                    (hold, campaign["id"]),
                )
                self._open_frame(connection, attempt_id)
        self._actions.append(f"{'refused' if refused_event else 'reserved'}:{key}:{number}")
        return not refused_event

    @staticmethod
    def _experiment_exposure_unknown(
        connection: sqlite3.Connection, campaign: Mapping[str, Any], stage: Mapping[str, Any]
    ) -> bool:
        """ADR 0038 section 3: unknown exposure on the stage's experiment budget blocks."""
        if stage["experiment_id"] is None:
            return False
        try:
            status = budget_status(
                connection,
                {"org_id": campaign["org_id"], "project_id": None},
                stage["experiment_id"],
            )
        except NotFound:
            return False
        return bool(status["exposure_unknown"])

    # -- stage outcomes -------------------------------------------------------------

    def _stage_succeeded(self, ctx: _Ctx, key: str, attempt: dict[str, Any]) -> bool:
        spec_stage = self._spec_stage(ctx, key)
        measured = {
            item["name"]: item["value"]
            for item in json.loads(attempt["measurements_json"] or "[]")
            if item.get("value") is not None
        }
        results = []
        triggered = None
        for rule in spec_stage.stop_when:
            observed = measured.get(rule.metric)
            if observed is None:
                outcome: Any = "not_recorded"
            else:
                outcome = bool(_COMPARE[rule.op](observed, rule.value))
            results.append(
                {
                    "metric": rule.metric,
                    "op": rule.op,
                    "value": rule.value,
                    "observed": observed,
                    "result": outcome,
                }
            )
            if outcome is True and triggered is None:
                triggered = f"stop_rule:{key}.{rule.metric}"
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=? AND status IN ('preparing','running')",
                (ctx.campaign["id"], key),
                status="succeeded",
                stop_results_json=json.dumps(results, sort_keys=True),
            )
            if spec_stage.holdout is not None:
                # ADR 0068: the claim records the scored values in the same transaction.
                holdout.score_claim(connection, ctx.campaign["id"], key, attempt, _iso(self.now()))
            if triggered is not None:
                connection.execute(
                    "UPDATE campaigns SET stop_reason=? WHERE id=? AND stop_reason IS NULL",
                    (triggered, ctx.campaign["id"]),
                )
                # A true stop rule winds the campaign down in the ``stopping`` state.
                moved = connection.execute(
                    "UPDATE campaigns SET status='stopping', updated_at=? "
                    "WHERE id=? AND status='running'",
                    (_iso(self.now()), ctx.campaign["id"]),
                ).rowcount
                if moved:
                    self.store.record_runner_event(
                        connection,
                        dict(
                            connection.execute(
                                "SELECT * FROM campaigns WHERE id=?", (ctx.campaign["id"],)
                            ).fetchone()
                        ),
                        "campaign.stopping",
                        {"stop_reason": triggered, "stage_key": key},
                    )
        return True

    def _stage_failed(self, ctx: _Ctx, key: str, reason: str) -> bool:
        with self.store.runner_connection() as connection:
            self._mark_stage_failed(connection, ctx, key, reason)
        return True

    def _mark_stage_failed(
        self, connection: sqlite3.Connection, ctx: _Ctx, key: str, reason: str
    ) -> None:
        self._touch(
            connection,
            "campaign_stages",
            "campaign_id=? AND stage_key=? AND status NOT IN "
            "('failed','succeeded','blocked_unknown')",
            (ctx.campaign["id"], key),
            status="failed",
        )
        connection.execute(
            "UPDATE campaigns SET stop_reason=? WHERE id=? AND stop_reason IS NULL",
            (f"stage_failed:{key}:{reason}"[:300], ctx.campaign["id"]),
        )

    def _stage_unknown(self, ctx: _Ctx, key: str, reason: str | None = None) -> bool:
        with self.store.runner_connection() as connection:
            self._mark_stage_unknown(connection, ctx, key, reason)
        self._actions.append(f"blocked:{key}")
        return False

    def _mark_stage_unknown(
        self, connection: sqlite3.Connection, ctx: _Ctx, key: str, reason: str | None
    ) -> None:
        """The stage ``blocked_unknown`` and the campaign blocked, in the caller's transaction."""
        self._touch(
            connection,
            "campaign_stages",
            "campaign_id=? AND stage_key=?",
            (ctx.campaign["id"], key),
            status="blocked_unknown",
        )
        updated = connection.execute(
            """UPDATE campaigns SET status='blocked', updated_at=?
            WHERE id=? AND status IN ('running','stopping','cancel_requested')""",
            (_iso(self.now()), ctx.campaign["id"]),
        )
        if updated.rowcount:
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "campaign.blocked",
                {"stage_key": key, **({"reason": reason} if reason else {})},
            )
            self._free_campaign_releases(connection, ctx.campaign["id"], "campaign_blocked")

    def _attempt_failed(
        self, ctx: _Ctx, key: str, attempt: dict[str, Any], failure_class: str, detail: str
    ) -> None:
        """Permanent failure; the hold is released only when no grant could still run."""
        detail = detail[:500]
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=? AND status NOT IN ('succeeded','failed_permanent','cancelled')",
                (attempt["id"],),
                status="failed_permanent",
                failure_class="step_refused"
                if failure_class not in _FAILURE_CLASSES
                else failure_class,
                failure_detail=detail,
            )
            if attempt["grant_id"] is None:
                self._settle(connection, attempt["id"], 0, "unclaimed_zero")
        self._actions.append(f"failed:{key}:{attempt['attempt']}:{failure_class}")

    def _cancel_unstarted(self, ctx: _Ctx, attempt: dict[str, Any]) -> None:
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=? AND grant_id IS NULL AND status='reserved'",
                (attempt["id"],),
                status="cancelled",
                failure_class="cancelled",
            )
            self._settle(connection, attempt["id"], 0, "unclaimed_zero")

    # -- stage preparation ----------------------------------------------------------

    def _advance_attempt(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        if stage["prep_phase"] != "experiment_ready":
            return self._prep(ctx, stage, spec_stage, attempt)
        provider = self._provider(spec_stage)

        def observe(ctx_, stage_, spec_stage_, attempt_):
            return provider.advance(self, ctx_, stage_, attempt_)

        handler = {
            "reserved": self._do_reserved,
            "granted": self._do_admit,
            "admitted": observe,
            "running": observe,
            "exited": self._do_collect,
            "collected": self._do_resolve,
            "resolved": self._do_evidence,
            "measured": self._do_reconcile,
            "reconciled": self._do_conclude,
        }[attempt["status"]]
        return handler(ctx, stage, spec_stage, attempt)

    def _task_row(self, task_id: str) -> dict[str, Any]:
        row = self._one(
            "SELECT id, revision, current_plan_digest FROM tasks WHERE id=?", (task_id,)
        )
        assert row is not None
        return row

    def _prep(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        key = stage["stage_key"]
        campaign = ctx.campaign
        project = campaign["project_id"]
        hold = spec_stage.max_exposure_cents
        phase = stage["prep_phase"]
        title = f"{ctx.spec.name}/{key}"[:200]
        purpose = f"Campaign {campaign['id']} stage {key} (ADR 0038)"
        # Every attempt, scientific, infrastructure (ADR 0058) or class fallback (ADR 0060
        # addendum), holds its own exposure; the stage's own exposure is the per-run cap every
        # candidate fits under (ADR 0060).
        budget = min(
            ctx.spec.stage(key).max_exposure_cents * spec_stage.retry.hold_attempts,
            EXPERIMENT_CEILING_CENTS,
        )

        if phase == "none":
            result = self._step(
                ctx,
                key,
                None,
                "create_task",
                0,
                lambda: (
                    {"project_id": project},
                    {
                        "title": title,
                        "purpose": purpose,
                        "budget_cents": budget,
                        "max_duration_seconds": spec_stage.max_duration_seconds,
                    },
                ),
            )
            self._set_stage(
                ctx,
                key,
                task_id=result.response["id"],
                task_revision=result.response["revision"],
                plan_digest=result.response["current_plan_digest"],
                prep_phase="task_created",
            )
            return True
        if phase == "task_created":
            return self._prep_input(ctx, stage, spec_stage, attempt)
        task = self._task_row(stage["task_id"])
        if phase == "input_ready":
            result = self._step(
                ctx,
                key,
                None,
                "revise_plan",
                0,
                lambda: (
                    {"task_id": stage["task_id"]},
                    {
                        "expected_revision": task["revision"],
                        "title": title,
                        "purpose": purpose,
                        "budget_cents": budget,
                        "max_duration_seconds": spec_stage.max_duration_seconds,
                        "execution_spec": self._provider(spec_stage).execution_spec(
                            spec_stage, stage["input_snapshot_digest"]
                        ),
                    },
                ),
            )
            self._set_stage(
                ctx,
                key,
                task_revision=result.response.get("revision"),
                plan_digest=result.response.get("current_plan_digest")
                or result.response.get("plan_digest"),
                prep_phase="planned",
            )
            return True
        if phase == "planned":
            result = self._step(
                ctx,
                key,
                None,
                "approve_task",
                0,
                lambda: (
                    {"task_id": stage["task_id"]},
                    {
                        # The plan this runner revised, not a fresh read: an interleaved
                        # human revision must conflict rather than be approved by delegation.
                        "expected_revision": stage["task_revision"],
                        "expected_plan_digest": stage["plan_digest"],
                    },
                ),
            )
            self._set_stage(
                ctx,
                key,
                task_revision=result.response.get("revision", stage["task_revision"]),
                prep_phase="task_approved",
            )
            return True
        if phase == "task_approved":
            if stage["experiment_id"] is None:
                return self._prep_experiment(ctx, stage, spec_stage, task)
            # Optional provider hook: scaffolding that needs the experiment and its input
            # (False after it ran a step, so the next loop re-derives).
            ready = getattr(self._provider(spec_stage), "experiment_ready", None)
            if ready is not None and not ready(self, ctx, stage, spec_stage):
                return True
            if hold > 0:
                assert ctx.approval is not None
                self._step(
                    ctx,
                    key,
                    None,
                    "authorize_budget",
                    0,
                    lambda: (
                        {"experiment_id": stage["experiment_id"]},
                        {
                            "expected_revision": 0,
                            "ceiling_cents": budget,
                            "expires_at": ctx.approval["expires_at"],
                            "evidence_digest": ctx.campaign["spec_digest"],
                            "source_reference": f"campaign:{campaign['id']}/{key}"[:256],
                            "prior_settled_cents": 0,
                            "prior_pending_cents": 0,
                            "ancillary_cents": 0,
                            "shutdown_cents": 0,
                        },
                    ),
                )
            self._set_stage(ctx, key, prep_phase="experiment_ready")
            return True
        raise _Waiting

    def _prep_experiment(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, task: dict[str, Any]
    ) -> bool:
        key = stage["stage_key"]
        metadata = {"campaign_id": ctx.campaign["id"], "stage_key": key, "single_arm": True}
        # The stage exactly as submitted (v1 digests are unchanged by normalization).
        submitted = json.loads(ctx.campaign["spec_json"])["stages"]
        method_digest = canonical_digest(next(s for s in submitted if s["key"] == key))

        def variant(kind: str) -> dict[str, Any]:
            return {
                "kind": kind,
                "name": f"{key}-{kind}",
                "method_digest": method_digest,
                "input_digest": stage["input_snapshot_digest"],
                "metadata": metadata,
                "execution_task_id": stage["task_id"],
                "execution_plan_digest": task["current_plan_digest"],
            }

        result = self._step(
            ctx,
            key,
            None,
            "create_experiment",
            0,
            lambda: (
                {"project_id": ctx.campaign["project_id"]},
                {
                    "goal": f"Operational run of campaign {ctx.campaign['id']} stage {key}.",
                    "success_criterion": "The declared stage runs to a confirmed exit with a "
                    "run receipt; no scientific claim is made (ADR 0038).",
                    "protocol": "Single-arm stage: baseline and intervention reference the "
                    "same approved task plan. Not a comparison.",
                    "variants": [variant("baseline"), variant("intervention")],
                },
            ),
        )
        variants = result.response["design"]["variants"]
        baseline = next(item for item in variants if item["kind"] == "baseline")
        self._set_stage(
            ctx,
            key,
            experiment_id=result.response["id"],
            variant_id=baseline["id"],
            design_revision=result.response["current_design_revision"],
            variant_revision=baseline["revision"],
        )
        return True

    def _snapshot_entries(self, snapshot_id: str) -> list[dict[str, Any]]:
        row = self._one("SELECT manifest_json FROM source_snapshots WHERE id=?", (snapshot_id,))
        assert row is not None
        return list(json.loads(row["manifest_json"])["entries"])

    def _upstream_manifest(self, ctx: _Ctx, stage_key: str) -> dict[str, Any]:
        row = self._one(
            """SELECT a.manifest_id, m.manifest_digest, m.artifacts_json
            FROM campaign_stage_attempts a JOIN artifact_manifests m ON m.id=a.manifest_id
            WHERE a.campaign_id=? AND a.stage_key=? AND a.status='succeeded'
            ORDER BY a.attempt DESC LIMIT 1""",
            (ctx.campaign["id"], stage_key),
        )
        if row is None:
            raise StepFailed("compose_snapshot", "step_refused", "upstream output is missing")
        return row

    def _recorded_class(self, ctx: _Ctx, key: str, snapshot_id: str) -> str:
        """Most restrictive of the spec's derived class and the platform's recorded class."""
        declared = stage_input_class(ctx.spec, key)
        with self.store.runner_connection() as connection:
            recorded = CampaignStore.recorded_snapshot_class(connection, snapshot_id)
        return "private_derived" if "private_derived" in (declared, recorded) else "public"

    @staticmethod
    def _public_entries(item: Any) -> list[IoEntry]:
        """Index entries for a pinned public artifact (ADR 0097): the pinned digest of each file
        and its declared size bound (the provider fetches; the control plane never sees bytes)."""
        return [
            IoEntry(
                name=f"{item.name}/{name}",
                kind="other",
                sha256=sha256,
                size_bytes=max_bytes,
                data_class="public",
                path=name,
            )
            for name, _url, sha256, max_bytes in item.public_files
        ]

    def _input_entries(
        self, ctx: _Ctx, spec_stage: NormalStage, input_snapshot_id: str | None
    ) -> list[IoEntry]:
        """Index entries for every resolved input, with the platform's recorded classes."""
        project = ctx.campaign["project_id"]
        entries: list[IoEntry] = []
        for item in sorted(spec_stage.inputs, key=lambda i: i.name):
            if item.kind == "public_artifact":
                entries.extend(self._public_entries(item))
            elif item.kind == "snapshot":
                with self.store.runner_connection() as connection:
                    recorded = CampaignStore.recorded_snapshot_class(connection, item.snapshot_id)
                size = sum(int(e["size"]) for e in self._snapshot_entries(item.snapshot_id))
                entries.append(
                    IoEntry(
                        name=item.name,
                        kind="dataset",
                        sha256=item.digest,
                        size_bytes=size,
                        data_class=recorded,
                        snapshot_id=item.snapshot_id,
                        lineage_node_id=self._node_for(project, item.digest),
                    )
                )
            elif item.kind == "stage":
                manifest = self._upstream_manifest(ctx, item.stage)
                listed = {e["relative_path"]: e for e in json.loads(manifest["artifacts_json"])}
                for path in sorted(item.paths):
                    entries.append(
                        IoEntry(
                            name=f"{item.name}/{path}",
                            kind="other",
                            sha256=listed[path]["sha256"],
                            size_bytes=int(listed[path]["size"]),
                            data_class="private_derived",
                            manifest_id=manifest["manifest_id"],
                            path=path,
                            lineage_node_id=self._node_for(project, listed[path]["sha256"]),
                        )
                    )
            elif item.kind == "artifact":
                resolved = self._resolve_artifact(ctx, item)
                entries.append(
                    IoEntry(
                        name=item.name,
                        kind=resolved.kind,
                        media_type=resolved.media_type,
                        sha256=resolved.sha256,
                        size_bytes=resolved.size_bytes,
                        data_class=resolved.data_class,
                        manifest_id=resolved.manifest_id,
                        path=resolved.path,
                        lineage_node_id=resolved.lineage_node_id,
                    )
                )
        with self.store.runner_connection() as connection:
            checkpoint = composed_checkpoint(
                connection, ctx.campaign, spec_stage.key, input_snapshot_id
            )
        if checkpoint is not None:
            # ADR 0197: the interrupted attempt's checkpoint is part of this attempt's snapshot.
            entries.append(
                IoEntry(
                    name=f"{checkpoint.name}/{checkpoint.path}",
                    kind="other",
                    sha256=checkpoint.digest,
                    size_bytes=checkpoint.bytes,
                    data_class=checkpoint.data_class,
                    manifest_id=checkpoint.manifest_id,
                    path=checkpoint.path,
                    lineage_node_id=self._node_for(project, checkpoint.digest),
                )
            )
        return entries

    def _resolve_artifact(self, ctx: _Ctx, item: Any) -> Any:
        try:
            with self.store.runner_connection() as connection:
                return resolve_artifact(connection, ctx.campaign, item)
        except Conflict as error:
            raise StepFailed("compose_snapshot", "step_refused", str(error)[:300]) from None

    def _input_refused(
        self,
        ctx: _Ctx,
        key: str,
        spec_stage: NormalStage,
        snapshot_id: str,
        digest: str,
        data_class: str,
        reason: str,
        worker: str | None,
    ) -> bool:
        """No release is ever issued for an input the provider instance may not receive."""
        self._set_stage(
            ctx,
            key,
            input_snapshot_id=snapshot_id,
            input_snapshot_digest=digest,
            input_data_class=data_class,
        )
        for attempt in self._attempts(ctx, key):
            if attempt["status"] == "reserved" and attempt["grant_id"] is None:
                self._cancel_unstarted(ctx, attempt)
        self.emit_once(
            ctx,
            "campaign.stage_input_refused",
            {"stage_key": key},
            {
                "provider_id": spec_stage.provider,
                "worker_principal_id": worker,
                "reason": reason[:300],
            },
        )
        self._stage_failed(ctx, key, "input_not_admitted")
        return True

    def _bind_stage_outputs(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage
    ) -> tuple[late_bound.ResolvedStageInput, ...] | None:
        """ADR 0140: bind every in-campaign stage-output input to its recorded digest when the
        stage starts, re-check routing for the resolved artifacts and record the binding (the
        lineage edge the receipt shows) in the same transaction. A refusal is a closed code: the
        stage fails, nothing unstarted stays reserved, and None is returned."""
        key = stage["stage_key"]
        worker = self._provider(spec_stage).grant_request(self, ctx, spec_stage).worker_principal_id
        try:
            with self.store.runner_connection() as connection:
                earlier = late_bound.bound_inputs(connection, ctx.campaign, key)
                resolved = late_bound.resolve_stage_inputs(
                    connection,
                    ctx.campaign["id"],
                    spec_stage,
                    org_id=ctx.campaign["org_id"],
                    project_id=ctx.campaign["project_id"],
                    expected={(item.name, item.output): item.digest for item in earlier},
                )
                late_bound.check_resolved_routing(
                    connection,
                    spec_stage,
                    resolved,
                    worker=worker,
                    approval_digest=(ctx.approval or {}).get("trust_policy_digest"),
                )
                self._emit_once_in(
                    connection,
                    ctx,
                    late_bound.BOUND_EVENT,
                    {"stage_key": key},
                    {"provider": spec_stage.provider, **late_bound.launch_payload(resolved)},
                )
        except late_bound.LateBindRefused as error:
            for attempt in self._attempts(ctx, key):
                if attempt["status"] == "reserved" and attempt["grant_id"] is None:
                    self._cancel_unstarted(ctx, attempt)
            self.emit_once(
                ctx,
                late_bound.REFUSED_EVENT,
                {"stage_key": key},
                {
                    "code": error.code,
                    "input": error.input_name,
                    "source_stage": error.source_stage,
                    "provider": spec_stage.provider,
                    "worker_principal_id": worker,
                    "reason": error.detail,
                },
            )
            self._stage_failed(ctx, key, f"late_bind_refused:{error.code}")
            return None
        return resolved

    def _input_ready(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: NormalStage,
        snapshot_id: str,
        digest: str,
        attempt: dict[str, Any] | None = None,
    ) -> bool:
        key = stage["stage_key"]
        data_class = self._recorded_class(ctx, key, snapshot_id)
        provider = self._provider(spec_stage)
        worker = provider.grant_request(self, ctx, spec_stage).worker_principal_id
        narrowed = frozenset(spec_stage.send_classes) if spec_stage.send_classes else None
        try:
            with self.store.runner_connection() as connection:
                admitted = provider_trust.provider_admits(
                    connection,
                    spec_stage.provider,
                    worker,
                    snapshot_id,
                    narrowed,
                    (ctx.approval or {}).get("trust_policy_digest"),
                )
        except DataClassRefused as error:
            return self._input_refused(
                ctx, key, spec_stage, snapshot_id, digest, data_class, str(error), worker
            )
        entries = self._input_entries(ctx, spec_stage, snapshot_id)
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
                input_snapshot_id=snapshot_id,
                input_snapshot_digest=digest,
                input_data_class=data_class,
                prep_phase="input_ready",
            )
            if attempt is not None:
                self._touch(
                    connection,
                    "campaign_stage_attempts",
                    "id=?",
                    (attempt["id"],),
                    provider_id=spec_stage.provider,
                    trust_policy_digest=admitted.policy_digest,
                    admission_basis=admitted.basis,
                )
            write_io(
                connection,
                ctx.campaign,
                key,
                None if attempt is None else attempt["id"],
                "input",
                entries,
                _iso(self.now()),
            )
        return True

    def _input_ready_public(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: NormalStage,
        attempt: dict[str, Any] | None,
    ) -> bool:
        """A stage whose only inputs are pinned public artifacts (ADR 0097): nothing to compose,
        admit or release; the provider fetches them. The stage records no snapshot, a digest of
        the pinned sources and the ``public`` class."""
        key = stage["stage_key"]
        entries = self._input_entries(ctx, spec_stage, None)
        digest = canonical_digest(
            [[e.name, e.sha256, e.size_bytes] for e in sorted(entries, key=lambda e: e.name)]
        )
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
                input_snapshot_id=None,
                input_snapshot_digest=digest,
                input_data_class="public",
                prep_phase="input_ready",
            )
            if attempt is not None:
                self._touch(
                    connection,
                    "campaign_stage_attempts",
                    "id=?",
                    (attempt["id"],),
                    provider_id=spec_stage.provider,
                    trust_policy_digest=provider_trust.current_policy(connection).digest,
                    admission_basis="public_only",
                )
            write_io(
                connection,
                ctx.campaign,
                key,
                None if attempt is None else attempt["id"],
                "input",
                entries,
                _iso(self.now()),
            )
        return True

    def _stamp_public(
        self, ctx: _Ctx, key: str, spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> None:
        entries = self._input_entries(ctx, spec_stage, None)
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                provider_id=spec_stage.provider,
                trust_policy_digest=provider_trust.current_policy(connection).digest,
                admission_basis="public_only",
            )
            write_io(
                connection, ctx.campaign, key, attempt["id"], "input", entries, _iso(self.now())
            )
        return None

    def _prep_input(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: NormalStage,
        attempt: dict[str, Any] | None = None,
    ) -> bool:
        custody = custody_inputs(spec_stage)
        if not custody:
            return self._input_ready_public(ctx, stage, spec_stage, attempt)
        pinned: dict[tuple[str, str], str] = {}
        if any(item.kind == "stage" for item in custody):
            bound = self._bind_stage_outputs(ctx, stage, spec_stage)
            if bound is None:
                return True
            pinned = {(item.name, item.path): item.digest for item in bound}
        if not needs_composition(spec_stage):
            source = custody[0]
            return self._input_ready(
                ctx, stage, spec_stage, source.snapshot_id, source.digest, attempt
            )

        build = self._compose_build(ctx, custody, pinned)

        result = self._step(ctx, stage["stage_key"], None, "compose_snapshot", 0, build)
        return self._input_ready(
            ctx, stage, spec_stage, result.response["id"], result.response["digest"], attempt
        )

    def _manifest_digest(self, manifest_id: str | None) -> str:
        row = self._one("SELECT manifest_digest FROM artifact_manifests WHERE id=?", (manifest_id,))
        if row is None:
            raise StepFailed("compose_snapshot", "step_refused", "checkpoint manifest is missing")
        return row["manifest_digest"]

    def _compose_for_retry(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> dict[str, Any]:
        """ADR 0197: the stage row for a retry attempt, its input snapshot composed with the
        checkpoint the immediately preceding interrupted attempt left in custody.

        A retry with no such checkpoint gets the base composition: unchanged when no attempt
        ever carried one, restored (the first composition replays) when an earlier retry did.
        The checkpoint rides as one more manifest source under its declared output name, bound
        by the digest recorded when it was salvaged."""
        key = stage["stage_key"]
        custody = custody_inputs(spec_stage)
        if not custody:
            return stage
        with self.store.runner_connection() as connection:
            found = retry_checkpoint_from(connection, ctx.campaign, key)
            composed_before = ever_composed(connection, ctx.campaign, key)
            bound = late_bound.bound_inputs(connection, ctx.campaign, key)
        checkpoint = None if found is None else found[0]
        if checkpoint is None and not composed_before:
            return stage
        pinned = {(item.name, item.path): item.digest for item in bound}
        if checkpoint is None and not needs_composition(spec_stage):
            snapshot_id, digest = custody[0].snapshot_id, custody[0].digest
        else:
            if checkpoint is not None:
                # Composition reads released custody only: release the interrupted attempt's
                # manifest under the same spec pre-approval a collected attempt gets. That is
                # custody, not acceptance: its verdict stays failed and only the checkpoint
                # path is composed.
                self._release_manifest(ctx, key, found[1], checkpoint.manifest_id)
            result = self._step(
                ctx,
                key,
                None if checkpoint is None else attempt["attempt"],
                "compose_snapshot",
                0,
                self._compose_build(ctx, custody, pinned, checkpoint),
            )
            snapshot_id, digest = result.response["id"], result.response["digest"]
        data_class = self._recorded_class(ctx, key, snapshot_id)
        with self.store.runner_connection() as connection:
            if checkpoint is not None:
                self._emit_once_in(
                    connection,
                    ctx,
                    COMPOSED_EVENT,
                    {"stage_key": key, "attempt": attempt["attempt"]},
                    {
                        "snapshot_id": snapshot_id,
                        "snapshot_digest": digest,
                        "input": checkpoint.to_json(),
                    },
                )
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
                input_snapshot_id=snapshot_id,
                input_snapshot_digest=digest,
                input_data_class=data_class,
            )
        return self._stage_row(ctx, key)

    def _compose_build(
        self,
        ctx: _Ctx,
        custody: tuple[Any, ...],
        pinned: dict[tuple[str, str], str],
        checkpoint: late_bound.ResolvedStageInput | None = None,
    ) -> Callable[[], tuple[dict[str, str], dict[str, Any]]]:
        """The ``compose_snapshot`` request for a stage's custody inputs, plus (for a retry of an
        interrupted attempt, ADR 0197) the checkpoint that attempt left in custody, as one more
        manifest source under its declared output name."""

        def build() -> tuple[dict[str, str], dict[str, Any]]:
            sources: list[dict[str, Any]] = []
            entries: list[tuple[str, str, int, str]] = []
            for item in custody:
                if item.kind == "snapshot":
                    sources.append(
                        {
                            "name": item.name,
                            "kind": "snapshot",
                            "snapshot_id": item.snapshot_id,
                            "expected_digest": item.digest,
                        }
                    )
                    for entry in self._snapshot_entries(item.snapshot_id):
                        entries.append(
                            (
                                f"{item.name}/{entry['relative_path']}",
                                entry["sha256"],
                                entry["size"],
                                "imported_unverified",
                            )
                        )
                elif item.kind == "stage":
                    manifest = self._upstream_manifest(ctx, item.stage)
                    listed = {e["relative_path"]: e for e in json.loads(manifest["artifacts_json"])}
                    missing = [path for path in item.paths if path not in listed]
                    if missing:
                        raise StepFailed(
                            "compose_snapshot", "step_refused", "declared output was not produced"
                        )
                    if any(listed[p]["sha256"] != pinned.get((item.name, p)) for p in item.paths):
                        # ADR 0140: composition uses exactly the digests bound at stage start.
                        raise StepFailed(
                            "compose_snapshot", "step_refused", "late-bound input digest changed"
                        )
                    sources.append(
                        {
                            "name": item.name,
                            "kind": "manifest",
                            "manifest_id": manifest["manifest_id"],
                            "expected_manifest_digest": manifest["manifest_digest"],
                            "relative_paths": list(item.paths),
                        }
                    )
                    for path in item.paths:
                        entries.append(
                            (
                                f"{item.name}/{path}",
                                listed[path]["sha256"],
                                listed[path]["size"],
                                "imported_unverified",
                            )
                        )
                elif item.kind == "artifact":
                    resolved = self._resolve_artifact(ctx, item)
                    sources.append(
                        {
                            "name": item.name,
                            "kind": "manifest",
                            "manifest_id": resolved.manifest_id,
                            "expected_manifest_digest": resolved.manifest_digest,
                            "relative_paths": [resolved.path],
                        }
                    )
                    entries.append(
                        (
                            f"{item.name}/{resolved.path}",
                            resolved.sha256,
                            resolved.size_bytes,
                            "imported_unverified",
                        )
                    )
            if checkpoint is not None:
                sources.append(
                    {
                        "name": checkpoint.name,
                        "kind": "manifest",
                        "manifest_id": checkpoint.manifest_id,
                        "expected_manifest_digest": self._manifest_digest(checkpoint.manifest_id),
                        "relative_paths": [checkpoint.path],
                    }
                )
                entries.append(
                    (
                        f"{checkpoint.name}/{checkpoint.path}",
                        checkpoint.digest,
                        checkpoint.bytes,
                        "imported_unverified",
                    )
                )
            digest = inventory_digest(composed_inventory(entries))
            return {"project_id": ctx.campaign["project_id"]}, {
                "sources": sources,
                "expected_snapshot_digest": digest,
            }

        return build

    # -- attempt lifecycle: authority -----------------------------------------------

    def _do_reserved(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        if attempt.get("trust_policy_digest") is None:
            # A retry: compose its input snapshot (with the interrupted predecessor's
            # checkpoint when one was salvaged), then re-check it under the setting in force now.
            stage = self._compose_for_retry(ctx, stage, spec_stage, attempt)
            stamped = self._stamp_trust(ctx, stage, spec_stage, attempt)
            if stamped is not None:
                return stamped
        if self._deadline_too_short(ctx, stage, spec_stage, attempt):
            return True
        if stage["input_snapshot_id"] is None and attempt["release_id"] is None:
            # No custody input: the execution grant needs a snapshot to bind (execution plane),
            # on every provider. A stage whose only inputs are public_artifact therefore fails
            # closed here until the grant path can bind no snapshot; give it a snapshot input.
            raise StepFailed(
                "issue_grant",
                "step_refused",
                "public_only_stage_needs_a_snapshot_input: an execution grant binds a "
                "snapshot, so a stage whose only inputs are public_artifact cannot run yet; "
                "add a snapshot input (the public files are still fetched on the provider)",
            )
        if attempt["release_id"] is None:
            if self._sealed_input_refused(ctx, spec_stage, attempt):
                return True
            if spec_stage.holdout is not None and not self._open_holdout(ctx, spec_stage, attempt):
                return True
            return self._ensure_release(ctx, stage, spec_stage, attempt)
        return self._issue_grant(ctx, stage, spec_stage, attempt)

    def _sealed_input_refused(
        self, ctx: _Ctx, spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        """ADR 0068: re-check sealed partitions when the release is about to be created.

        A campaign approved before another campaign opened its claim (or before a holdout
        stage's outputs existed) is refused here, before anything is released."""
        with self.store.runner_connection() as connection:
            reason = holdout.sealed_input_refusal(
                connection, ctx.campaign["project_id"], spec_stage
            )
        if reason is None:
            return False
        self._attempt_failed(ctx, spec_stage.key, attempt, holdout.SEALED_PARTITION, reason)
        return True

    def _holdout_opened(self, ctx: _Ctx, key: str) -> bool:
        return (
            self._one(
                "SELECT 1 FROM campaign_holdout_claims WHERE campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], key),
            )
            is not None
        )

    def _open_holdout(self, ctx: _Ctx, spec_stage: NormalStage, attempt: dict[str, Any]) -> bool:
        """ADR 0068: open the stage's one-time holdout claim before any input is released.

        The claim and its event commit together. When another campaign or stage already holds
        the (partition, scope) claim the attempt fails permanently and nothing is released."""
        assert ctx.approval is not None
        stamp = _iso(self.now())
        with self.store.runner_connection() as connection:
            self._hold_lease(connection)
            existed = connection.execute(
                "SELECT 1 FROM campaign_holdout_claims WHERE campaign_id=? AND stage_key=?",
                (ctx.campaign["id"], spec_stage.key),
            ).fetchone()
            reused = (
                existed is not None
                and connection.execute(
                    """SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=?
                AND attempt<>? AND release_id IS NOT NULL""",
                    (ctx.campaign["id"], spec_stage.key, attempt["attempt"]),
                ).fetchone()
            )
            if reused:
                # Defense in depth: an earlier attempt already released the partition.
                held = False
                claim = next(
                    c
                    for c in holdout.campaign_claims(connection, ctx.campaign["id"])
                    if c["stage_key"] == spec_stage.key
                )
            else:
                held, claim = holdout.open_claim(
                    connection, ctx.campaign, ctx.approval, spec_stage, stamp
                )
            if held and existed is None:
                self.store.record_runner_event(
                    connection,
                    ctx.campaign,
                    "campaign.holdout_claimed",
                    {
                        "stage_key": spec_stage.key,
                        "claim_id": claim["claim_id"],
                        "partition_digest": claim["partition_digest"],
                        "claim_scope": claim["claim_scope"],
                        "attempt": attempt["attempt"],
                    },
                )
        if held:
            return True
        self._attempt_failed(
            ctx,
            spec_stage.key,
            attempt,
            holdout.HOLDOUT_ALREADY_CLAIMED,
            f"claimed by campaign {claim['campaign_id']} stage {claim['stage_key']} "
            f"({claim['status']})",
        )
        return False

    def _deadline_too_short(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        """Refuse, before any release or grant, a window that cannot hold the stage.

        True once the attempt and stage are failed closed with ``deadline_too_short``."""
        assert ctx.approval is not None
        key = stage["stage_key"]
        with self.store.runner_connection() as connection:
            if self.steps.completed(
                connection, ctx.campaign["id"], key, attempt["attempt"], "issue_grant", 0
            ):
                return False
        worker = self._provider(spec_stage).grant_request(self, ctx, spec_stage).worker_principal_id
        limits = [_parse_utc(ctx.approval["expires_at"])]
        limits += [
            _parse_utc(row["expires_at"])
            for row in self._rows(
                """SELECT expires_at FROM credentials WHERE revoked_at IS NULL
                AND expires_at IS NOT NULL AND principal_id IN (?, ?)""",
                (ctx.approval["approving_principal_id"], worker),
            )
        ]
        remaining = int((min(limits) - self.now()).total_seconds())
        required = int(spec_stage.max_duration_seconds) + SETTLE_MARGIN_SECONDS
        if remaining >= required:
            return False
        detail = (
            f"remaining window {remaining}s is shorter than the required {required}s "
            f"(max_duration {int(spec_stage.max_duration_seconds)}s + settle margin "
            f"{SETTLE_MARGIN_SECONDS}s); a re-approval is needed"
        )
        self._attempt_failed(ctx, key, attempt, "deadline_too_short", detail)
        self.emit_once(
            ctx,
            "campaign.stage_deadline_too_short",
            {"stage_key": key},
            {"remaining_seconds": remaining, "required_seconds": required, "reason": detail},
        )
        return self._stage_failed(ctx, key, "deadline_too_short")

    def _stamp_trust(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool | None:
        """Admit the input again for a retry attempt and record the trust evidence on it.

        Returns None once stamped; True when the input is no longer admitted (refused)."""
        key = stage["stage_key"]
        snapshot_id = stage["input_snapshot_id"]
        worker = self._provider(spec_stage).grant_request(self, ctx, spec_stage).worker_principal_id
        narrowed = frozenset(spec_stage.send_classes) if spec_stage.send_classes else None
        if snapshot_id is None:
            return self._stamp_public(ctx, key, spec_stage, attempt)
        try:
            with self.store.runner_connection() as connection:
                admitted = provider_trust.provider_admits(
                    connection,
                    spec_stage.provider,
                    worker,
                    snapshot_id,
                    narrowed,
                    (ctx.approval or {}).get("trust_policy_digest"),
                )
        except DataClassRefused as error:
            return self._input_refused(
                ctx,
                key,
                spec_stage,
                snapshot_id,
                stage["input_snapshot_digest"],
                stage["input_data_class"] or stage_input_class(ctx.spec, key),
                str(error),
                worker,
            )
        entries = self._input_entries(ctx, spec_stage, snapshot_id)
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                provider_id=spec_stage.provider,
                trust_policy_digest=admitted.policy_digest,
                admission_basis=admitted.basis,
            )
            write_io(
                connection, ctx.campaign, key, attempt["id"], "input", entries, _iso(self.now())
            )
        return None

    def _release_must_wait(
        self,
        ctx: _Ctx,
        stage: dict[str, Any],
        attempt: dict[str, Any],
        active: dict[str, Any] | None,
    ) -> bool:
        """ADR 0167: a campaign that gave its release back on pause waits for the snapshot
        (first come, first served) instead of failing the stage when another campaign holds it,
        and nobody takes a free snapshot ahead of an earlier waiter. A campaign that never
        gave a release back keeps the original refusal."""
        snapshot_id = stage["input_snapshot_id"]
        with self.store.runner_connection() as connection:
            ahead = release_waiters_ahead(
                connection, ctx.campaign["project_id"], snapshot_id, attempt
            )
            returning = freed_by_pause(connection, ctx.campaign, snapshot_id)
            held_by_other = False
            if active is not None and _parse_utc(active["expires_at"]) > self.now():
                held_by_other = (
                    connection.execute(
                        "SELECT 1 FROM campaign_stage_attempts "
                        "WHERE campaign_id=? AND release_id=?",
                        (ctx.campaign["id"], active["id"]),
                    ).fetchone()
                    is None
                )
            if not ahead and not (held_by_other and returning):
                return False  # free, or the original refusal for a first-time request
            self._emit_once_in(
                connection,
                ctx,
                RELEASE_WAITING_EVENT,
                {"stage_key": stage["stage_key"], "attempt_id": attempt["id"]},
                {
                    "attempt": attempt["attempt"],
                    "snapshot_id": snapshot_id,
                    "reason": "held_by_campaign" if held_by_other else "earlier_waiter",
                    "returning_from_pause": returning,
                },
            )
        return True

    def _ensure_release(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        key = stage["stage_key"]
        assert ctx.approval is not None
        request = self._provider(spec_stage).grant_request(self, ctx, spec_stage)
        worker = request.worker_principal_id
        classification = stage["input_data_class"] or stage_input_class(ctx.spec, key)
        need_until = self.now() + timedelta(seconds=request.ttl_seconds + RELEASE_SLACK_SECONDS)
        active = self._one(
            """SELECT * FROM snapshot_releases WHERE snapshot_id=? AND worker_principal_id=?
            AND status='active'""",
            (stage["input_snapshot_id"], worker),
        )
        if self._release_must_wait(ctx, stage, attempt, active):
            raise _Waiting
        owned = None
        if active is not None:
            owned = self._one(
                "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND release_id=?",
                (ctx.campaign["id"], active["id"]),
            )
        if active is not None and owned is None:
            # A release_snapshot that committed before the attempt row recorded it is ours.
            # Read outside the transaction below: it opens its own.
            slot = self._release_ordinal(ctx, key, attempt)
            with self.store.runner_connection() as connection:
                done = self.steps.completed(
                    connection,
                    ctx.campaign["id"],
                    key,
                    attempt["attempt"],
                    "release_snapshot",
                    slot,
                )
            if done is not None and done.response.get("id") == active["id"]:
                self._set_attempt(attempt["id"], release_id=active["id"])
                return True
            # An expired release is never active, whoever held it: it does not refuse this
            # campaign, and release_snapshot lapses it in the new release's transaction.
            if _parse_utc(active["expires_at"]) > self.now():
                with self.store.runner_connection() as connection:
                    detail = holder_refusal(connection, active, ctx.campaign["org_id"])
                raise StepFailed("release_snapshot", "step_refused", detail)
        elif active is not None:
            if (
                _parse_utc(active["expires_at"]) >= need_until
                and active["classification"] == classification
                and active["digest"] == stage["input_snapshot_digest"]
            ):
                self._set_attempt(attempt["id"], release_id=active["id"])
                return True
            self._step(
                ctx,
                key,
                attempt["attempt"],
                "revoke_release",
                0,
                lambda: ({"release_id": active["id"]}, {"expected_revision": active["revision"]}),
            )
            return True
        anchor = _parse_utc(attempt["updated_at"])
        caps = [
            _parse_utc(row["expires_at"])
            for row in self._rows(
                """SELECT expires_at FROM credentials WHERE revoked_at IS NULL
                AND expires_at IS NOT NULL AND principal_id IN (?, ?)""",
                (ctx.approval["approving_principal_id"], worker),
            )
        ]
        expiry = min(
            anchor + timedelta(seconds=RELEASE_MAX_SECONDS),
            _parse_utc(ctx.approval["expires_at"]),
            *caps,
        )
        result = self._step(
            ctx,
            key,
            attempt["attempt"],
            "release_snapshot",
            self._release_ordinal(ctx, key, attempt),
            lambda: (
                {"snapshot_id": stage["input_snapshot_id"]},
                {
                    "expected_digest": stage["input_snapshot_digest"],
                    "worker_principal_id": worker,
                    "classification": classification,
                    "permits_worker_transfer": True,
                    "rationale": (
                        f"Pre-approved by campaign {ctx.campaign['id']} spec "
                        f"{ctx.campaign['spec_digest']} for stage {key}"
                    ),
                    "expires_at": _iso(expiry),
                },
            ),
        )
        self._set_attempt(attempt["id"], release_id=result.response["id"])
        return True

    def _limits_allow(
        self, project_id: str, hold: int, attempt: dict[str, Any] | None = None
    ) -> bool:
        """The project limits admit one more grant, behind every attempt queued earlier
        (fair FIFO: a freed slot goes to the longest waiter, never to a newcomer)."""
        with self.store.runner_connection() as connection:
            ahead = 0 if attempt is None else waiters_ahead(connection, project_id, attempt)
            return placement.project_limits_allow(connection, project_id, hold, ahead)

    def _note_waiting_limits(self, ctx: _Ctx, key: str, hold: int, attempt: dict[str, Any]) -> None:
        """One event per attempt when project limits keep it from getting a grant; it also
        puts the attempt in the FIFO line (``waiters_ahead``)."""
        with self.store.runner_connection() as connection:
            seen = connection.execute(
                """SELECT 1 FROM events WHERE project_id=? AND event_type=?
                AND json_extract(payload_json, '$.campaign_id')=?
                AND json_extract(payload_json, '$.stage_key')=?
                AND json_extract(payload_json, '$.attempt_id')=? LIMIT 1""",
                (
                    ctx.campaign["project_id"],
                    "campaign.stage_waiting_limits",
                    ctx.campaign["id"],
                    key,
                    attempt["id"],
                ),
            ).fetchone()
            if seen is None:
                self.store.record_runner_event(
                    connection,
                    ctx.campaign,
                    "campaign.stage_waiting_limits",
                    {
                        "stage_key": key,
                        "hold_cents": hold,
                        "attempt": attempt["attempt"],
                        "attempt_id": attempt["id"],
                    },
                )

    def _issue_grant(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        key = stage["stage_key"]
        with self.store.runner_connection() as connection:
            existing = self.steps.completed(
                connection, ctx.campaign["id"], key, attempt["attempt"], "issue_grant", 0
            )
        # A replay after a crash finds the intent already written (and maybe the grant
        # already created): it neither re-checks limits nor waits for a slot its own grant
        # holds, and its anchor stays fixed so the request rebuilds identically.
        fresh = existing is None and self._no_issue_intent(ctx, key, attempt)
        if fresh and not self._limits_allow(
            ctx.campaign["project_id"], spec_stage.max_exposure_cents, attempt
        ):
            self._note_waiting_limits(ctx, key, spec_stage.max_exposure_cents, attempt)
            raise _Waiting
        provider = self._provider(spec_stage)
        slot_free = getattr(provider, "slot_free", None)
        if fresh and slot_free is not None and not slot_free(self, ctx, spec_stage):
            # ADR 0048: the lifetime clock starts when a slot is free, not before.
            self.emit_once(
                ctx,
                "campaign.stage_waiting_slot",
                {"stage_key": key},
                {"attempt": attempt["attempt"]},
            )
            raise _Waiting
        if fresh:
            # The grant's lifetime is anchored on the attempt's last update. Restart it now
            # that a slot is free so time spent waiting for one never eats the lifetime.
            self._set_attempt(attempt["id"])
            attempt = self._attempt(attempt["id"])
            if self._release_too_short(ctx, key, spec_stage, attempt):
                return True
        release = self._one(
            "SELECT expires_at FROM snapshot_releases WHERE id=?", (attempt["release_id"],)
        )
        assert release is not None
        request = self._provider(spec_stage).grant_request(self, ctx, spec_stage)
        anchor = _parse_utc(attempt["updated_at"])
        expiry = min(
            anchor + timedelta(seconds=request.ttl_seconds),
            _parse_utc(release["expires_at"]),
        )
        if request.expires_not_after is not None:
            expiry = min(expiry, _parse_utc(request.expires_not_after))
        result = self._step(
            ctx,
            key,
            attempt["attempt"],
            "issue_grant",
            0,
            lambda: (
                {"project_id": ctx.campaign["project_id"]},
                {
                    "task_id": stage["task_id"],
                    "expected_task_revision": stage["task_revision"],
                    "expected_plan_digest": stage["plan_digest"],
                    "worker_principal_id": request.worker_principal_id,
                    "snapshot_id": stage["input_snapshot_id"],
                    "pinned_image": spec_stage.image,
                    "expires_at": _iso(expiry),
                    "max_exposure_cents": spec_stage.max_exposure_cents,
                    "currency": "USD",
                    "acknowledges_unmeasured_infrastructure": True,
                },
            ),
        )
        # Read before the transaction: an adapter's manifest read may be slow and is never
        # allowed to hold the database.
        provenance = image_provenance(spec_stage, self._provider(spec_stage))
        with self.store.runner_connection() as connection:
            # The grant and its code record are one write (AGENTS.md): a crash leaves both or
            # neither, so a receipt never shows a grant whose code record is missing.
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                grant_id=result.response["id"],
                status="granted",
            )
            self._record_code_grant_in(
                connection, ctx, stage, spec_stage, attempt, request.worker_principal_id
            )
            self._emit_once_in(
                connection,
                ctx,
                IMAGE_PROVENANCE_EVENT,
                {"stage_key": stage["stage_key"], "attempt": attempt["attempt"]},
                provenance,
            )
            self._record_slot_reacquired(connection, ctx, key, attempt, result.response["id"])
        return True

    def _record_slot_reacquired(
        self,
        connection: sqlite3.Connection,
        ctx: _Ctx,
        key: str,
        attempt: dict[str, Any],
        grant_id: str,
    ) -> None:
        """A grant issued after a paused-campaign release (ADR 0167) is the slot re-acquired:
        one event on the receipt, in the grant's own transaction."""
        previous = connection.execute(
            """SELECT id, attempt FROM campaign_stage_attempts WHERE campaign_id=?
            AND stage_key=? AND attempt<? AND failure_class=? AND failure_detail=?
            ORDER BY attempt DESC LIMIT 1""",
            (ctx.campaign["id"], key, attempt["attempt"], NOT_STARTED, PAUSED_RELEASE_DETAIL),
        ).fetchone()
        if previous is None or previous["attempt"] != attempt["attempt"] - 1:
            return
        self.store.record_runner_event(
            connection,
            ctx.campaign,
            SLOT_REACQUIRED_EVENT,
            {
                "stage_key": key,
                "attempt": attempt["attempt"],
                "attempt_id": attempt["id"],
                "previous_attempt_id": previous["id"],
                "grant_id": grant_id,
                "released_by": "system",
                "automatic": True,
            },
        )

    def _record_code_grant_in(
        self,
        connection: sqlite3.Connection,
        ctx: _Ctx,
        stage: dict[str, Any],
        spec_stage: NormalStage,
        attempt: dict[str, Any],
        worker_principal_id: str,
    ) -> None:
        """Record, in the caller's grant transaction, that the platform granted the attempt's
        worker access to the stage's pinned code input (ADR 0095): the digests, the provider,
        the trust basis and the code approval as it stood at grant time. Ids and digests only,
        never the files, argv or any bytes. It records the grant, not that the worker fetched
        the input."""
        summary = code_summary(connection, spec_stage)
        if summary is None:
            return
        row = connection.execute(
            """SELECT provider_id, trust_policy_digest, admission_basis
            FROM campaign_stage_attempts WHERE id=?""",
            (attempt["id"],),
        ).fetchone()
        delivery = code_delivery(
            summary,
            provider_id=row["provider_id"] or spec_stage.provider,
            worker_principal_id=worker_principal_id,
            trust_policy_digest=row["trust_policy_digest"],
            admission_basis=row["admission_basis"],
        )
        approval = snapshot_code_approval(connection, summary["snapshot_id"])
        self._emit_once_in(
            connection,
            ctx,
            CODE_GRANTED_EVENT,
            {"stage_key": stage["stage_key"], "attempt": attempt["attempt"]},
            {
                "attempt_id": attempt["id"],
                "input": summary["input"],
                "snapshot_id": summary["snapshot_id"],
                **delivery,
                "approved": approval is not None and approval.get("valid") is True,
                "approval_digest": _approval_digest(approval),
            },
        )

    def _release_too_short(
        self, ctx: _Ctx, key: str, spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        """Renew a snapshot release that would cut the grant short; True while doing so.

        The grant never outlives its release, and the release was made before the wait for a
        slot, so a long wait could leave it expiring inside the grant's lifetime. It is then
        revoked and the attempt's release cleared so the next pass makes a fresh one. A
        release another live grant still uses is left alone and the issue waits."""
        release = self._one("SELECT * FROM snapshot_releases WHERE id=?", (attempt["release_id"],))
        ttl = self._provider(spec_stage).grant_request(self, ctx, spec_stage).ttl_seconds
        wanted = _parse_utc(attempt["updated_at"]) + timedelta(seconds=ttl)
        if release is None or _parse_utc(release["expires_at"]) >= wanted:
            return False
        if self._release_ordinal(ctx, key, attempt):
            return False  # already renewed once for this attempt; never loop
        request = self._provider(spec_stage).grant_request(self, ctx, spec_stage)
        assert ctx.approval is not None
        reachable = min(
            _parse_utc(attempt["updated_at"]) + timedelta(seconds=RELEASE_MAX_SECONDS),
            _parse_utc(ctx.approval["expires_at"]),
            *[
                _parse_utc(row["expires_at"])
                for row in self._rows(
                    """SELECT expires_at FROM credentials WHERE revoked_at IS NULL
                    AND expires_at IS NOT NULL AND principal_id IN (?, ?)""",
                    (ctx.approval["approving_principal_id"], request.worker_principal_id),
                )
            ],
        )
        if reachable <= _parse_utc(release["expires_at"]):
            return False  # a new release would end no later: the approval window is the cap
        if release["status"] != "active":
            # A release someone else revoked cannot be renewed through this attempt's steps
            # (its release step is already done), so the stage is refused, never retried.
            raise StepFailed(
                "release_snapshot",
                "step_refused",
                "the snapshot release is no longer active and would end before the grant",
            )
        if self._one(
            """SELECT 1 FROM execution_grants WHERE snapshot_release_id=?
            AND status IN ('active','cancel_requested')""",
            (release["id"],),
        ):
            self.emit_once(
                ctx,
                "campaign.stage_waiting_release",
                {"stage_key": key},
                {"attempt": attempt["attempt"], "release_id": release["id"]},
            )
            raise _Waiting
        self._step(
            ctx,
            key,
            attempt["attempt"],
            "revoke_release",
            RELEASE_RENEWAL_ORDINAL,
            lambda: ({"release_id": release["id"]}, {"expected_revision": release["revision"]}),
        )
        self._set_attempt(attempt["id"], release_id=None)
        return True

    def _release_ordinal(self, ctx: _Ctx, key: str, attempt: dict[str, Any]) -> int:
        """The step slot for this attempt's release: fresh once it was renewed.

        Slot 1 of ``revoke_release`` belongs to the campaign's final release cleanup, so the
        renewal uses its own slot."""
        with self.store.runner_connection() as connection:
            renewed = self.steps.completed(
                connection,
                ctx.campaign["id"],
                key,
                attempt["attempt"],
                "revoke_release",
                RELEASE_RENEWAL_ORDINAL,
            )
        return RELEASE_RENEWAL_ORDINAL if renewed is not None else 0

    def _no_issue_intent(self, ctx: _Ctx, key: str, attempt: dict[str, Any]) -> bool:
        """True while no ``issue_grant`` intent exists for the attempt (pending or done)."""
        return (
            self._one(
                """SELECT 1 FROM campaign_step_intents WHERE campaign_id=? AND stage_key=?
                AND attempt=? AND step='issue_grant' LIMIT 1""",
                (ctx.campaign["id"], key, attempt["attempt"]),
            )
            is None
        )

    def _do_admit(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        key = stage["stage_key"]

        def build() -> tuple[dict[str, str], dict[str, Any]]:
            variant = self._one(
                "SELECT revision, design_revision FROM research_experiment_variants WHERE id=?",
                (stage["variant_id"],),
            )
            experiment = self._one(
                "SELECT current_design_revision FROM research_experiments WHERE id=?",
                (stage["experiment_id"],),
            )
            assert variant is not None and experiment is not None
            return {"variant_id": stage["variant_id"]}, {
                "expected_variant_revision": variant["revision"],
                "expected_design_revision": experiment["current_design_revision"],
                "grant_id": attempt["grant_id"],
            }

        result = self._step(ctx, key, attempt["attempt"], "admit_attempt", 0, build)
        admitted = result.response["id"]
        gate = spec_stage.gate_evidence
        if gate is not None:
            # Record the admitted id first so a refused binding unwinds the real attempt. The
            # split, seed and metrics come only from the approved spec, before dispatch.
            self._set_attempt(attempt["id"], admitted_attempt_id=admitted)
            self._step(
                ctx,
                key,
                attempt["attempt"],
                "bind_split",
                0,
                lambda: (
                    {"attempt_id": admitted, "split_lock_id": gate.split_lock_id},
                    {
                        "expected_revision": self._admitted_revision(admitted),
                        "split_lock_id": gate.split_lock_id,
                        "seed": gate.seed,
                        "metrics": [m.model_dump(mode="json") for m in gate.metrics],
                    },
                ),
            )
        self._set_attempt(attempt["id"], admitted_attempt_id=admitted, status="admitted")
        self._provider(spec_stage).after_admit(self, ctx, self._attempt(attempt["id"]))
        self._set_stage(ctx, key, status="running")
        return True

    # -- attempt lifecycle: observation ---------------------------------------------

    def _observe(self, attempt: dict[str, Any]) -> dict[str, Any]:
        with self.store.runner_connection() as connection:
            return self._observe_in(connection, attempt)

    @staticmethod
    def _observe_in(connection: sqlite3.Connection, attempt: Mapping[str, Any]) -> dict[str, Any]:
        """``_observe`` inside a transaction the caller already holds."""
        grant = connection.execute(
            "SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],)
        ).fetchone()
        leases = connection.execute(
            "SELECT count(*) FROM execution_leases WHERE grant_id=?", (attempt["grant_id"],)
        ).fetchone()[0]
        operation = connection.execute(
            "SELECT operation_id FROM external_operations WHERE grant_id=?",
            (attempt["grant_id"],),
        ).fetchone()
        reconciliation = receipt = None
        if operation is not None:
            reconciliation = current_operation_reconciliation(connection, operation["operation_id"])
            receipt = connection.execute(
                "SELECT receipt_digest, exit_code FROM execution_run_receipts WHERE operation_id=?",
                (operation["operation_id"],),
            ).fetchone()
        manifest = connection.execute(
            "SELECT id, manifest_digest, artifacts_json FROM artifact_manifests WHERE grant_id=?",
            (attempt["grant_id"],),
        ).fetchone()
        return {
            "grant": dict(grant),
            "leases": leases,
            "operation_id": operation["operation_id"] if operation else None,
            "reconciliation": reconciliation,
            "receipt": dict(receipt) if receipt else None,
            "manifest": dict(manifest) if manifest else None,
            # A manifest that lists fewer files than the plan declares never stands for a
            # complete run (it is written only for an interrupted or retryable attempt).
            "manifest_partial": manifest is not None
            and grant_manifest_is_partial(connection, grant, manifest["artifacts_json"]),
        }

    def _outputs_arriving(self, ctx: _Ctx, attempt: dict[str, Any], seen: dict[str, Any]) -> bool:
        """Whether this exited attempt's outputs are still reaching control (ADR 0064)."""
        with self.store.runner_connection() as connection:
            recorded = exit_recorded_at(connection, seen["reconciliation"])
            deadline = hold_deadline(seen["grant"], recorded, _approval_expiry(ctx))
            return uploads_arriving(
                connection,
                attempt["grant_id"],
                self.now(),
                self.upload_progress,
                deadline,
                None if seen["manifest"] else recorded,
            )

    def _do_observe(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        key = stage["stage_key"]
        seen = self._observe(attempt)
        grant = seen["grant"]
        expired = self.now() >= _parse_utc(grant["expires_at"])
        if grant["status"] == "reconciled":
            # Our own reconcile may have committed with the attempt update lost: finish it as T1.
            with self.store.runner_connection() as connection:
                ours = self.steps.completed(
                    connection,
                    ctx.campaign["id"],
                    key,
                    attempt["attempt"],
                    "reconcile_unattempted",
                    0,
                )
            if ours is not None and seen["operation_id"] is None:
                self._finish_attempt(
                    attempt, "failed_transient", "t1_grant_expired_unleased", ours.response
                )
                return True
            self._set_attempt(
                attempt["id"],
                status="unknown",
                failure_class="observed_unknown",
                failure_detail="grant was reconciled outside the campaign",
            )
            return True
        if seen["operation_id"] is None:
            if seen["leases"] == 0 and expired:
                result = self._step(
                    ctx,
                    key,
                    attempt["attempt"],
                    "reconcile_unattempted",
                    0,
                    lambda: ({"grant_id": grant["id"]}, {"expected_revision": grant["revision"]}),
                )
                self._finish_attempt(
                    attempt, "failed_transient", "t1_grant_expired_unleased", result.response
                )
                return True
            if seen["leases"] and expired:
                self._set_attempt(
                    attempt["id"],
                    status="unknown",
                    failure_class="observed_unknown",
                    failure_detail="leased grant expired without an operation",
                )
                return True
            raise _Waiting
        reconciliation = seen["reconciliation"]
        if reconciliation is None and not expired:
            # The operation is recorded at lease time; the worker's first observation
            # follows. No observation yet is not "unknown": wait until the grant expires.
            raise _Waiting
        state = reconciliation["observed_state"] if reconciliation else "unknown"
        confirmed = bool(reconciliation and reconciliation["termination_confirmed"])
        if state == "running":
            if attempt["status"] != "running":
                self._set_attempt(
                    attempt["id"], status="running", operation_id=seen["operation_id"]
                )
                return True
            raise _Waiting
        if state != "exited" or not confirmed:
            self._set_attempt(
                attempt["id"],
                status="unknown",
                failure_class="observed_unknown",
                operation_id=seen["operation_id"],
                failure_detail=f"observed {state}, termination confirmed={confirmed}",
            )
            return True
        receipt, manifest = seen["receipt"], seen["manifest"]
        if (receipt is None or (manifest is None and receipt["exit_code"] == 0)) and (
            self._outputs_arriving(ctx, attempt, seen)
        ):
            raise _Waiting  # outputs still uploading: do not mark it unknown (ADR 0064)
        if receipt is None:
            if expired:
                self._set_attempt(
                    attempt["id"],
                    status="unknown",
                    failure_class="missing_receipt",
                    operation_id=seen["operation_id"],
                    failure_detail="exited without a run receipt before grant expiry",
                )
                return True
            raise _Waiting
        if (manifest is None or seen["manifest_partial"]) and receipt["exit_code"] == 0:
            self._set_attempt(
                attempt["id"],
                status="unknown",
                failure_class="missing_manifest",
                operation_id=seen["operation_id"],
                failure_detail=(
                    "successful exit without a quarantined output manifest"
                    if manifest is None
                    else "successful exit with a partial output manifest"
                ),
            )
            return True
        self._set_attempt(
            attempt["id"],
            status="exited",
            operation_id=seen["operation_id"],
            run_receipt_digest=receipt["receipt_digest"],
            exit_code=receipt["exit_code"],
            manifest_id=manifest["id"] if manifest else None,
            manifest_digest=manifest["manifest_digest"] if manifest else None,
        )
        return True

    def _finish_attempt(
        self,
        attempt: dict[str, Any],
        status: str,
        failure_class: str | None,
        response: Mapping[str, Any],
    ) -> None:
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status=status,
                failure_class=failure_class,
            )
            self._settle(
                connection,
                attempt["id"],
                int(response.get("settlement_cents") or 0),
                "unclaimed_zero",
            )

    # -- attempt lifecycle: collection ----------------------------------------------

    def _do_collect(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        key = stage["stage_key"]
        manifest_id = attempt["manifest_id"]
        if manifest_id is not None:
            self._release_manifest(
                ctx, key, attempt["attempt"], manifest_id, attempt["manifest_digest"]
            )
        self._set_attempt(attempt["id"], status="collected")
        return True

    def _release_manifest(
        self,
        ctx: _Ctx,
        key: str,
        attempt_number: int,
        manifest_id: str,
        manifest_digest: str | None = None,
    ) -> None:
        """Record the pre-approved ``retain_quarantined`` disposition and release the manifest's
        custody (the two steps that make its files composable as an input)."""
        if manifest_digest is None:
            manifest_digest = self._manifest_digest(manifest_id)
        rationale = (
            f"Pre-approved by campaign {ctx.campaign['id']} spec {ctx.campaign['spec_digest']}"
        )
        for step, table, extra in (
            ("record_disposition", "artifact_dispositions", {"disposition": "retain_quarantined"}),
            ("custody_release", "artifact_custody_releases", {"action": "release"}),
        ):
            self._step(
                ctx,
                key,
                attempt_number,
                step,
                0,
                lambda table=table, extra=extra: (
                    {"manifest_id": manifest_id},
                    {
                        "expected_manifest_digest": manifest_digest,
                        "expected_revision": self._latest_revision(table, manifest_id),
                        **extra,
                        "rationale": rationale,
                    },
                ),
            )

    def _latest_revision(self, table: str, manifest_id: str) -> int:
        row = self._one(
            f"SELECT max(revision) AS r FROM {table} WHERE manifest_id=?", (manifest_id,)
        )
        return int(row["r"] or 0) if row else 0

    def _admitted_revision(self, admitted_id: str) -> int:
        row = self._one(
            "SELECT revision FROM research_experiment_admitted_attempts WHERE id=?", (admitted_id,)
        )
        assert row is not None
        return int(row["revision"])

    def _observation(self, spec_stage: NormalStage, attempt: dict[str, Any]) -> Any:
        """The provider's report on a collected attempt (read-only)."""
        observed = self._provider(spec_stage).observe(self, dict(attempt))
        if observed.outcome != "succeeded":
            return observed
        # ADR 0134: a reported transfer outside the stage's egress allowlist is an invariant
        # breach whatever the provider's own verdict: the attempt fails and its outputs are not
        # a usable result. The violation stays on the receipt.
        with self.store.runner_connection() as connection:
            refused = transfer_violations(connection, attempt, spec_stage)
        if not refused:
            return observed
        hosts = ", ".join(sorted({str(item["host"]) for item in refused}))
        return dataclasses.replace(
            observed,
            outcome="failed_permanent",
            failure_class="egress_violation",
            detail=f"transfer outside the egress allowlist: {hosts}",
        )

    def _attempt_passed(self, spec_stage: NormalStage, attempt: dict[str, Any]) -> bool:
        return self._observation(spec_stage, attempt).outcome == "succeeded"

    def _failure_reason(self, spec_stage: NormalStage, attempt: dict[str, Any]) -> str:
        return self._observation(spec_stage, attempt).detail[:500]

    def _do_resolve(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        succeeded = self._attempt_passed(spec_stage, attempt)
        self._step(
            ctx,
            stage["stage_key"],
            attempt["attempt"],
            "resolve_attempt",
            0,
            lambda: (
                {"attempt_id": attempt["admitted_attempt_id"]},
                {
                    "expected_revision": self._admitted_revision(attempt["admitted_attempt_id"]),
                    "outcome": "succeeded" if succeeded else "failed",
                    "reason": None if succeeded else self._failure_reason(spec_stage, attempt),
                },
            ),
        )
        self._set_attempt(attempt["id"], status="resolved")
        return True

    def _do_evidence(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        if not self._attempt_passed(spec_stage, attempt):
            return self._do_reconcile(ctx, stage, spec_stage, attempt)
        key = stage["stage_key"]
        manifest = self._one(
            "SELECT artifacts_json FROM artifact_manifests WHERE id=?", (attempt["manifest_id"],)
        )
        artifacts = json.loads(manifest["artifacts_json"]) if manifest else []
        by_path = {item["relative_path"]: item["id"] for item in artifacts}
        admitted = attempt["admitted_attempt_id"]
        if artifacts:
            self._step(
                ctx,
                key,
                attempt["attempt"],
                "link_artifacts",
                0,
                lambda: (
                    {"attempt_id": admitted},
                    {
                        "expected_revision": self._admitted_revision(admitted),
                        "artifact_ids": sorted({item["id"] for item in artifacts}),
                    },
                ),
            )
            self._record_provider_fetch(ctx, key, spec_stage, attempt, by_path)
        measurements: list[dict[str, Any]] = []
        for index, metric in enumerate(spec_stage.metrics):
            artifact_id = by_path.get(metric.path)
            entry: dict[str, Any] = {
                "name": metric.name,
                "artifact_id": artifact_id,
                "json_pointer": metric.json_pointer,
                "id": None,
                "value": None,
            }
            if artifact_id is not None:
                try:
                    result = self._step(
                        ctx,
                        key,
                        attempt["attempt"],
                        "derive_measurement",
                        index,
                        lambda aid=artifact_id, m=metric: (
                            {"attempt_id": admitted},
                            {
                                "expected_revision": self._admitted_revision(admitted),
                                "name": m.name,
                                "artifact_id": aid,
                                "json_pointer": m.json_pointer,
                            },
                        ),
                    )
                    entry["id"] = result.response["id"]
                    entry["value"] = result.response["value"]["number"]
                except StepFailed:
                    entry["value"] = None  # recorded as not_recorded, never inferred
                gate_names = (
                    {m.name for m in spec_stage.gate_evidence.metrics}
                    if spec_stage.gate_evidence is not None
                    else set()
                )
                if entry["id"] is not None and metric.name in gate_names:
                    # ADR 0049: the measurement becomes a gate metric record under this
                    # campaign's approval. A refusal leaves no record, which blocks any
                    # unlock that needs it (fail closed); it is never retried as success.
                    try:
                        record = self._step(
                            ctx,
                            key,
                            attempt["attempt"],
                            "record_gate_metric",
                            index,
                            lambda mid=entry["id"]: (
                                {"attempt_id": admitted},
                                {"measurement_id": mid, "checkpoint_sha": None},
                            ),
                        )
                        entry["gate_metric_record_id"] = record.response["id"]
                    except StepFailed as error:
                        entry["gate_metric_record_id"] = None
                        entry["gate_metric_error"] = error.error_class
            measurements.append(entry)
        for index, output in enumerate(spec_stage.outputs):
            if output.evidence is None:
                continue
            # ADR 0052: the platform ingests the stage's typed evidence rows itself. A refusal
            # (a gap, an extra cell, a lock mismatch) ingests nothing and is recorded as a
            # not-recorded entry, so any unlock that needs the rows stays closed.
            artifact_id = by_path.get(output.path)
            entry = {
                "name": f"evidence_rows.{output.name}",
                "artifact_id": artifact_id,
                "json_pointer": "",
                "id": None,
                "value": None,
            }
            if artifact_id is not None:
                try:
                    result = self._step(
                        ctx,
                        key,
                        attempt["attempt"],
                        "ingest_evidence_rows",
                        index,
                        lambda aid=artifact_id, o=output: (
                            {"attempt_id": admitted},
                            {
                                "expected_revision": self._admitted_revision(admitted),
                                "output_name": o.name,
                                "artifact_id": aid,
                            },
                        ),
                    )
                    entry["value"] = result.response["rows"]
                    entry["artifact_sha256"] = result.response["artifact_sha256"]
                except StepFailed as error:
                    entry["error_class"] = error.error_class
            measurements.append(entry)
        self._set_attempt(
            attempt["id"], status="measured", measurements_json=json.dumps(measurements)
        )
        return True

    def _record_provider_fetch(
        self,
        ctx: _Ctx,
        key: str,
        spec_stage: NormalStage,
        attempt: dict[str, Any],
        by_path: dict[str, str],
    ) -> None:
        """ADR 0097: a provider-run stage with pinned public files has its fetch rows read by
        control from the retained output.json and checked against the frozen pins. Only for a
        provider that takes the step (a local worker reports its own); a refusal records
        nothing, so the receipt stays ``declared_only`` and the passed stage is unaffected."""
        if not any(getattr(s, "kind", None) == "public_artifact" for s in spec_stage.inputs):
            return
        artifact_id = by_path.get(PROVIDER_FETCH_OUTPUT)
        if artifact_id is None:
            return
        try:
            steps = self._provider(spec_stage).descriptor.delegable_steps
        except KeyError:
            return
        if "record_provider_fetch" not in steps:
            return
        admitted = attempt["admitted_attempt_id"]
        try:
            self._step(
                ctx,
                key,
                attempt["attempt"],
                "record_provider_fetch",
                0,
                lambda: (
                    {"attempt_id": admitted},
                    {
                        "expected_revision": self._admitted_revision(admitted),
                        "artifact_id": artifact_id,
                    },
                ),
            )
        except StepFailed:
            pass  # nothing recorded: the pins stay declared_only, never inferred

    def _do_reconcile(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        grant_id = attempt["grant_id"]
        settlement, basis = self._settlement_for(spec_stage, attempt)

        def build() -> tuple[dict[str, str], dict[str, Any]]:
            grant = self._one("SELECT revision FROM execution_grants WHERE id=?", (grant_id,))
            assert grant is not None
            return {"grant_id": grant_id}, {
                "expected_revision": grant["revision"],
                "settlement_cents": settlement,
                "provider_terminated": True,
            }

        result = self._step(
            ctx, stage["stage_key"], attempt["attempt"], "reconcile_grant", 0, build
        )
        settled = int(result.response.get("settlement_cents") or 0)
        if settled != settlement:
            # A replayed step carries the amount it was first run with; if the settlement has
            # since changed (a human charge arrived) the basis would describe another amount.
            self.block_settlement_mismatch(attempt)
            return True
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status="reconciled",
            )
            self._settle(connection, attempt["id"], settled, basis)
        return True

    def _do_conclude(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        observed = self._observation(spec_stage, attempt)
        if observed.outcome == "succeeded":
            stop = resource_limit_stop(
                spec_stage.retry.resource_limit_metrics, attempt["measurements_json"]
            )
            infra = (
                None
                if stop is None
                else self._infra_failure(
                    ctx,
                    stage["stage_key"],
                    spec_stage,
                    attempt,
                    attempt["exit_code"],
                    "succeeded",
                    "",
                    stop=stop,
                )
            )
            if infra is None:
                return self._lineage(ctx, stage, spec_stage, attempt)
            self._set_attempt(
                attempt["id"], status=infra[0], failure_class=infra[1], failure_detail=infra[2]
            )
            return True
        failure_class = (
            observed.failure_class if observed.failure_class in FAILURE_CLASSES else "step_refused"
        )
        status = {
            "failed_transient": "failed_transient",
            "failed_permanent": "failed_permanent",
        }.get(observed.outcome, "unknown")
        if status == "unknown":
            failure_class = "observed_unknown"
        detail = observed.detail[:500] or None
        infra = self._infra_failure(
            ctx,
            stage["stage_key"],
            spec_stage,
            attempt,
            attempt["exit_code"],
            status,
            failure_class,
        )
        if infra is not None:
            status, failure_class, detail = infra
        elif status == "failed_permanent" and self._class_fallback(
            ctx, stage["stage_key"], spec_stage, attempt, "failed"
        ):
            return True
        elif (
            failure_class == INFRA_ALLOCATION_TIMEOUT
            and status == "failed_transient"
            and self._allocation_timeout_fallback(ctx, stage["stage_key"], attempt, detail)
        ):
            return True
        elif failure_class == INFRA_ALLOCATION_TIMEOUT and status == "failed_transient":
            status, detail = self._placement_failure(
                ctx, stage["stage_key"], spec_stage, attempt, detail
            )
        elif failure_class == SALAD_INTERRUPTED and status == "failed_transient":
            # Spends one infra retry; with none left the attempt (and so the stage) fails
            # permanently under the same class. The salvaged checkpoint, if custody holds one,
            # is recorded first (once, idempotently) so a replay after a crash never loses it.
            self._record_interrupted_checkpoint(ctx, stage["stage_key"], spec_stage, attempt)
            status, detail = self._placement_failure(
                ctx, stage["stage_key"], spec_stage, attempt, detail
            )
        self._set_attempt(
            attempt["id"], status=status, failure_class=failure_class, failure_detail=detail
        )
        return True

    def _record_interrupted_checkpoint(
        self, ctx: _Ctx, key: str, spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> None:
        """``interrupted_checkpointed``: the digest and size of the checkpoint custody salvaged
        from an interrupted attempt, recorded once. Nothing else of the attempt is accepted."""
        with self.store.runner_connection() as connection:
            found = salvaged_checkpoint(connection, ctx.campaign, spec_stage, attempt)
            if found is None:
                return
            self._emit_once_in(
                connection,
                ctx,
                CHECKPOINT_EVENT,
                {"stage_key": key, "attempt": attempt["attempt"]},
                {
                    "outcome": "interrupted_checkpointed",
                    "digest": found.digest,
                    "bytes": found.bytes,
                    "input": found.to_json(),
                },
            )

    def _allocation_timeout_fallback(
        self, ctx: _Ctx, key: str, attempt: Mapping[str, Any], observed_detail: str | None
    ) -> bool:
        """The one exception to ADR 0169 point 3: an observed ``infra_allocation_timeout`` of a
        CPU Salad stage whose group was started but never got an instance moves the stage to its
        next CPU profile (the spec alternates, then the operator chain), recorded as one
        ``campaign.salad_profile_switched`` event (reason ``allocation_timeout``) in the SAME
        transaction as the attempt's ``failed_transient`` / ``infra_allocation_timeout``
        transition. True when it did; False changes nothing (today's infrastructure retry path).

        Fails closed: only a stage that names one provider (no alternate or reroute candidates:
        placement would otherwise send the next attempt elsewhere), whose fallback gate reads
        (``reads_fallback``), with a class fallback left (``retry.max_class_fallbacks`` counts
        pre-start moves and these together), on a running campaign, with the provider's proof
        (``allocation_timeout_evidence``) and a covered CPU next profile. The attempt still counts
        as an infrastructure attempt (``infra_retries``) exactly as today, so the worst case
        ``max_attempts + infra_retries + max_class_fallbacks`` stays an upper bound. Settlement is
        untouched: the attempt already settled at its hold (``guardian_bounded_hold``)."""
        base = ctx.spec.stage(key)
        if base.has_candidates or base.retry.max_class_fallbacks < 1:
            return False
        if not class_fallback.enabled(base, placement_candidates(base)):
            return False
        if not salad_fallback.reads_fallback(base) or self._stopping(ctx):
            return False
        with self.store.runner_connection() as connection:
            fresh = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            if fresh is None or fresh["status"] != "reconciled":
                return False
            if (fresh["provider_id"] or base.provider) != base.provider:
                return False
            number = int(fresh["attempt"])
            rows = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=? "
                "AND id<>?",
                (ctx.campaign["id"], key, attempt["id"]),
            ).fetchall()
            moves = salad_fallback.timeout_moves(connection, ctx.campaign, key)
            used = class_fallback.prestart_attempts(rows) + sum(1 for n in moves if n != number)
            if used >= base.retry.max_class_fallbacks:
                return False
            outcome = salad_fallback.allocation_timeout_move(
                self, ctx, key, base, dict(fresh), connection
            )
            if outcome != salad_fallback.MOVED:
                return False
            base_detail = observed_detail or "allocation_timeout: no instance was allocated"
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status="failed_transient",
                failure_class=INFRA_ALLOCATION_TIMEOUT,
                failure_detail=(
                    f"{base_detail}; moved to the next profile, class fallback "
                    f"{used + 1} of {base.retry.max_class_fallbacks}"
                )[:500],
            )
        self._actions.append(f"allocation_timeout_fallback:{key}:{number}")
        return True

    def _class_fallback(
        self,
        ctx: _Ctx,
        key: str,
        spec_stage: NormalStage,
        attempt: Mapping[str, Any],
        path: str,
        connection: sqlite3.Connection | None = None,
        settled: int | None = None,
    ) -> bool:
        """ADR 0060 addendum: record a failed attempt that provably never started and settled
        at zero as ``prestart_no_spend``, so the stage is placed again on its next eligible
        class or candidate (for a Salad stage with alternates, its next listed profile with a
        ``campaign.salad_profile_switched`` event). True when it did. The attempt transition
        and its events commit together (in ``connection`` when the caller has one open); a
        provider without the proof, any amount other than zero, or a stage without alternates
        changes nothing. A next profile that is not covered records one stop event and returns
        False (the stage is then held, never moved)."""
        base = ctx.spec.stage(key)
        if not class_fallback.enabled(base, placement_candidates(base)):
            return False
        try:
            provider = self._provider(spec_stage)
        except KeyError:
            return False

        def apply(connection: sqlite3.Connection) -> bool:
            fresh = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            if fresh is None:
                return False
            amount = fresh["settled_cents"] if settled is None else settled
            code = class_fallback.reason(provider, connection, fresh, amount)
            if code is None:
                return False
            pid = fresh["provider_id"] or spec_stage.provider
            rows = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=? "
                "AND id<>?",
                (ctx.campaign["id"], key, attempt["id"]),
            ).fetchall()
            # ADR 0151 / ADR 0169: a Salad candidate that lists alternate profiles (then the
            # operator's fallback chain) moves to its next profile, recorded as a switch event in
            # this same transaction. Past the
            # bound nothing is recorded: the stage fails ``class_fallback_exhausted``.
            approved = self._on_candidate(base, pid) if base.has_candidates else base
            # Allocation-timeout moves share the bound with pre-start moves (none: unchanged).
            used = class_fallback.prestart_attempts(rows) + len(
                salad_fallback.timeout_moves(connection, ctx.campaign, key)
            )
            if salad_fallback.reads_fallback(approved) and used < base.retry.max_class_fallbacks:
                outcome = salad_fallback.prestart_move(
                    self, ctx, key, approved, fresh, code, connection
                )
                if outcome == salad_fallback.PROFILE_NOT_APPROVED:
                    return False  # held for a person (one stop event), never moved
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status="failed_transient",
                failure_class=class_fallback.PRESTART_NO_SPEND,
                failure_detail=code,
            )
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                class_fallback.EVENT,
                class_fallback.payload(
                    fresh,
                    pid,
                    class_fallback.failures(rows, base.provider).get(pid, 0),
                    code,
                    path,
                    used + 1,
                    base.retry.max_class_fallbacks,
                ),
            )
            return True

        if connection is not None:
            moved = apply(connection)
        else:
            with self.store.runner_connection() as own:
                moved = apply(own)
        if moved:
            self._actions.append(f"class_fallback:{key}:{attempt['attempt']}")
        return moved

    def _infra_failure(
        self,
        ctx: _Ctx,
        key: str,
        spec_stage: NormalStage,
        attempt: dict[str, Any],
        exit_code: int | None,
        status: str,
        failure_class: str,
        connection: sqlite3.Connection | None = None,
        stop: tuple[str, float] | None = None,
    ) -> tuple[str, str, str] | None:
        """Reclassify a confirmed exit that the infrastructure caused (ADR 0058), or a stop on
        a resource limit its own metric reported (``stop``, ADR 0067).

        Only a recorded exit code the spec names as infrastructure (default 137: the engine
        or the kernel's out-of-memory killer ended the container) on an attempt that would
        otherwise be a permanent undeclared-exit failure qualifies, and only when the spec
        enabled infrastructure retries. A declared transient code, a task failure, a refused
        step and every budget or policy refusal keep their own class. Both the observation
        path and the confirmed-exit path (ADR 0053) come here. ``connection`` is the caller's
        open transaction, if any. Returns ``None`` when nothing changes; when the bound is
        spent a kill stays a permanent failure and says so, once, on the timeline, and a stop
        metric is accepted as the result it recorded (ADR 0067: a spent bound never turns a
        success into a failure), with the same event naming the metric."""
        limit = spec_stage.retry.infra_retries
        if limit < 1:
            return None
        if stop is None and (
            status != "failed_permanent"
            or failure_class != "undeclared_exit_code"
            or exit_code is None
            or exit_code not in spec_stage.retry.infra_exit_codes
        ):
            return None
        sql = (
            "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=? "
            "ORDER BY attempt"
        )
        params = (ctx.campaign["id"], key)
        if connection is not None:
            used = infra_attempts(connection.execute(sql, params).fetchall())
        else:
            used = infra_attempts(self._rows(sql, params))
        if used >= limit:
            exhausted = {"attempt": attempt["attempt"], "exit_code": exit_code}
            exhausted["infra_retries_max"] = limit
            if stop is not None:
                exhausted["resource_limit_metric"] = stop[0]
            if connection is not None:
                self._emit_once_in(
                    connection,
                    ctx,
                    "campaign.stage_infra_retries_exhausted",
                    {"stage_key": key},
                    exhausted,
                )
            else:
                self.emit_once(
                    ctx, "campaign.stage_infra_retries_exhausted", {"stage_key": key}, exhausted
                )
            return None
        if stop is not None:
            return (
                "failed_transient",
                INFRA_RESOURCE_LIMIT,
                f"stopped on a resource limit ({stop[0]}={stop[1]:g}); "
                f"infrastructure failure, retry {used + 1} of {limit}"[:500],
            )
        return (
            "failed_transient",
            INFRA_CONTAINER_KILLED,
            f"exit_code:{exit_code}; infrastructure failure, retry {used + 1} of {limit}"[:500],
        )

    def _placement_failure(
        self,
        ctx: _Ctx,
        key: str,
        spec_stage: NormalStage,
        attempt: dict[str, Any],
        observed_detail: str | None = None,
    ) -> tuple[str, str]:
        """``(status, detail)`` for an attempt the provider could not place (no instance was
        ever allocated): a retryable infrastructure failure within ``retry.infra_retries``, which
        placement may send to another approved candidate; with no retries left or enabled it
        fails permanently, never retried and never a scientific retry. Each retry is a new
        attempt with its own hold, so the campaign budget and project limits still bound it."""
        limit = spec_stage.retry.infra_retries
        used = infra_attempts(
            self._rows(
                "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=? "
                "ORDER BY attempt",
                (ctx.campaign["id"], key),
            )
        )
        base = (
            observed_detail
            or attempt["failure_detail"]
            or "allocation_timeout: no instance was allocated"
        )
        if limit < 1 or used >= limit:
            self.emit_once(
                ctx,
                "campaign.stage_infra_retries_exhausted",
                {"stage_key": key},
                {"attempt": attempt["attempt"], "infra_retries_max": limit},
            )
            return "failed_permanent", f"{base}; no infrastructure retry left ({used} of {limit})"[
                :500
            ]
        return "failed_transient", f"{base}; infrastructure failure, retry {used + 1} of {limit}"[
            :500
        ]

    # -- lineage --------------------------------------------------------------------

    def _node_for(self, project_id: str, sha: str) -> str | None:
        row = self._one(
            "SELECT id FROM artifact_lineage_nodes WHERE project_id=? AND sha256=?",
            (project_id, sha),
        )
        return row["id"] if row else None

    def _edge_exists(self, child: str, parent: str) -> bool:
        return (
            self._one(
                """SELECT 1 FROM artifact_lineage_edges WHERE child_node_id=? AND parent_node_id=?
                AND relation='derived_from'""",
                (child, parent),
            )
            is not None
        )

    def _edge_step_done(self, ctx: _Ctx, key: str, attempt: dict[str, Any], ordinal: int) -> bool:
        with self.store.runner_connection() as connection:
            return (
                self.steps.completed(
                    connection,
                    ctx.campaign["id"],
                    key,
                    attempt["attempt"],
                    "record_lineage_edge",
                    ordinal,
                )
                is not None
            )

    def _lineage(
        self, ctx: _Ctx, stage: dict[str, Any], spec_stage: NormalStage, attempt: dict[str, Any]
    ) -> bool:
        key = stage["stage_key"]
        project = ctx.campaign["project_id"]
        manifest = self._one(
            "SELECT artifacts_json FROM artifact_manifests WHERE id=?", (attempt["manifest_id"],)
        )
        outputs = sorted(
            (json.loads(manifest["artifacts_json"]) if manifest else []),
            key=lambda item: item["relative_path"],
        )
        inputs: list[tuple[str, str, str | None, str]] = []  # (label, sha, artifact_id, kind)
        for source in sorted(spec_stage.inputs, key=lambda item: item.name):
            if source.kind == "snapshot":
                inputs.append((f"{source.name}", source.digest, None, "dataset"))
            elif source.kind == "artifact":
                # The node that produced the pinned output already exists (same project).
                inputs.append((f"{source.name}", source.sha256, None, "other"))
            elif source.kind == "public_artifact":
                for name, _url, sha256, _max in source.public_files:
                    inputs.append((f"{source.name}/{name}", sha256, None, "other"))
            else:
                upstream = self._upstream_manifest(ctx, source.stage)
                listed = {e["relative_path"]: e for e in json.loads(upstream["artifacts_json"])}
                for path in sorted(source.paths):
                    inputs.append((f"{source.stage}/{path}", listed[path]["sha256"], None, "other"))
        ordinal = 0
        node_ids: dict[str, str] = {}
        plan: list[tuple[str, dict[str, Any]]] = []
        for label, sha, _artifact, kind in inputs:
            plan.append(
                (
                    sha,
                    {
                        "kind": kind,
                        "sha256": sha,
                        "label": label[:200],
                        "origin": "external_import",
                        "source_uri": None,
                    },
                )
            )
        for item in outputs:
            plan.append(
                (
                    item["sha256"],
                    {
                        "kind": "other",
                        "sha256": item["sha256"],
                        "label": f"{key}/{item['relative_path']}"[:200],
                        "origin": "tessera_attempt",
                        "artifact_id": item["id"],
                        "producing_attempt_id": attempt["admitted_attempt_id"],
                        "operation_id": attempt["operation_id"],
                    },
                )
            )
        for sha, body in plan:
            existing = node_ids.get(sha) or self._node_for(project, sha)
            if existing is None:
                result = self._step(
                    ctx,
                    key,
                    attempt["attempt"],
                    "register_lineage_node",
                    ordinal,
                    lambda b=body: ({"project_id": project}, b),
                )
                existing = result.response["id"]
            node_ids[sha] = existing
            ordinal += 1
        edge_ordinal = 0
        for item in outputs:
            for _label, sha, _artifact, _kind in inputs:
                child, parent = node_ids[item["sha256"]], node_ids[sha]
                if child == parent or edge_ordinal >= MAX_LINEAGE_EDGES:
                    continue
                if self._edge_exists(child, parent) and not self._edge_step_done(
                    ctx, key, attempt, edge_ordinal
                ):
                    # Content-addressed nodes: a rerun with identical bytes repeats an edge.
                    edge_ordinal += 1
                    continue
                self._step(
                    ctx,
                    key,
                    attempt["attempt"],
                    "record_lineage_edge",
                    edge_ordinal,
                    lambda c=child, p=parent: (
                        {"project_id": project},
                        {
                            "child_node_id": c,
                            "parent_node_id": p,
                            "relation": "derived_from",
                            "rationale": f"Output of campaign {ctx.campaign['id']} stage {key} "
                            f"attempt {attempt['attempt']}",
                        },
                    ),
                )
                edge_ordinal += 1
        # Success and the output index rows commit together.
        with self.store.runner_connection() as connection:
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status="succeeded",
            )
            if attempt["manifest_id"] is not None:
                write_io(
                    connection,
                    ctx.campaign,
                    key,
                    attempt["id"],
                    "output",
                    output_entries(connection, ctx.campaign, spec_stage, attempt["manifest_id"]),
                    _iso(self.now()),
                )
        return True

    # -- unwinding authority (failure, cancel) --------------------------------------

    def _unwind_failed(self, ctx: _Ctx, key: str) -> None:
        """Revoke and reconcile any grant left behind by a permanently failed attempt."""
        for attempt in self._attempts(ctx, key):
            if (
                attempt["status"] == "failed_permanent"
                and attempt["grant_id"] is not None
                and attempt["settled_cents"] is None
            ):
                try:
                    self._unwind_attempt(ctx, key, attempt)
                except (StepFailed, _Waiting):
                    continue

    def _unwind_attempt(self, ctx: _Ctx, key: str, attempt: dict[str, Any]) -> bool:
        """Drive one attempt's authority to reconciled; True when nothing is outstanding.

        Routed through the stage's provider, never by whether an operation was recorded.
        """
        try:
            provider = self._provider(self._spec_stage(ctx, key))
        except KeyError:
            # Authority can only be unwound through its provider: stay open, never settle.
            raise StepFailed(
                "unwind", "provider_unavailable", "the attempt's provider is not registered"
            ) from None
        return provider.cancel(self, ctx, key, attempt)

    def _revoke(self, ctx: _Ctx, key: str, attempt: dict[str, Any], grant: dict[str, Any]) -> None:
        self._step(
            ctx,
            key,
            attempt["attempt"],
            "revoke_grant",
            0,
            lambda: ({"grant_id": grant["id"]}, {"expected_revision": grant["revision"]}),
        )

    def _drive_cancel(self, ctx: _Ctx) -> None:
        outstanding = False
        for entry in self._rows(
            "SELECT stage_key, status FROM campaign_stages WHERE campaign_id=? ORDER BY topo_index",
            (ctx.campaign["id"],),
        ):
            key = entry["stage_key"]
            if entry["status"] == "pending":
                self._set_stage(ctx, key, status="cancelled")
            elif self._cancel_stage_attempts(ctx, key):
                outstanding = True
        if outstanding or self._budget <= 0:
            return
        if self._revoke_releases(ctx):
            return
        if self._one(
            "SELECT 1 FROM campaign_stages WHERE campaign_id=? AND status='blocked_unknown'",
            (ctx.campaign["id"],),
        ):
            return
        self._finish(self._campaign(ctx.campaign["id"]), "cancelled")

    def _cancel_stage_attempts(self, ctx: _Ctx, key: str) -> bool:
        """Unwind this stage's attempts; True while any authority is still outstanding."""
        outstanding = False
        for attempt in self._attempts(ctx, key):
            settled = attempt["settled_cents"] is not None
            if attempt["status"] == "unknown":
                if self._auto_reconcile(ctx, key, attempt):
                    continue
                self._stage_unknown(ctx, key)
                return True
            if attempt["grant_id"] is None:
                if attempt["status"] == "reserved":
                    self._cancel_unstarted(ctx, attempt)
                continue
            if settled:
                if attempt["status"] in ACTIVE_ATTEMPT:
                    self._set_attempt(attempt["id"], status="cancelled", failure_class="cancelled")
                continue
            try:
                done = self._unwind_attempt(ctx, key, attempt)
            except StepFailed as error:
                self._attempt_failed(ctx, key, attempt, error.error_class, error.detail)
                outstanding = True
                continue
            except _Waiting:
                outstanding = True
                continue
            if done and attempt["status"] in ACTIVE_ATTEMPT:
                self._set_attempt(attempt["id"], status="cancelled", failure_class="cancelled")
            elif not done:
                outstanding = True
        if not outstanding:
            stage = self._stage_row(ctx, key)
            if stage["status"] not in TERMINAL_STAGE:
                self._set_stage(ctx, key, status="cancelled")
        return outstanding

    def _revoke_releases(self, ctx: _Ctx) -> bool:
        """Revoke campaign-created active releases; True while one is still active."""
        # Each revoke is keyed to its release (the first attempt that owns it, ordinal 1; ordinal 0
        # is the in-stage replacement of an expiring release), never to list position.
        active = self._rows(
            """SELECT r.id, r.revision, (SELECT a.stage_key FROM campaign_stage_attempts a
                WHERE a.campaign_id=? AND a.release_id=r.id ORDER BY a.stage_key, a.attempt
                LIMIT 1) AS stage_key, (SELECT a.attempt FROM campaign_stage_attempts a
                WHERE a.campaign_id=? AND a.release_id=r.id ORDER BY a.stage_key, a.attempt
                LIMIT 1) AS attempt
            FROM snapshot_releases r WHERE r.status='active' AND
            EXISTS (SELECT 1 FROM campaign_stage_attempts a WHERE a.campaign_id=?
                    AND a.release_id=r.id) ORDER BY r.id""",
            (ctx.campaign["id"],) * 3,
        )
        for release in active:
            self._step(
                ctx,
                release["stage_key"],
                release["attempt"],
                "revoke_release",
                1,
                lambda r=release: ({"release_id": r["id"]}, {"expected_revision": r["revision"]}),
            )
        return bool(active)

    # -- automatic reconciliation of attempts that never started (ADR 0046) ---------

    def _auto_reconcile(self, ctx: _Ctx, key: str, attempt: dict[str, Any]) -> bool:
        """Reconcile an unknown attempt shown to have never started; True when it was.

        The attempt becomes ``failed_transient``/``not_started`` with zero settlement, its
        grant is reconciled at zero and the stage is re-queued without using a scientific
        retry. Identical repeats are bounded: past ``MAX_AUTO_REQUEUES`` the attempt stays
        unknown for a person, with the repeat recorded as the reason."""
        with self.store.runner_connection() as connection:
            verdict = assess(connection, attempt, now=self.now())
        if verdict is None:
            # Not an ADR 0046 case; perhaps one whose provider claimed no create (ADR 0157).
            return self._auto_reconcile_unclaimed(ctx, key, attempt)
        others = [a for a in self._attempts(ctx, key) if a["id"] != attempt["id"]]
        repeats = repeat_count(others, verdict.reason)
        stage = self._stage_row(ctx, key)
        if repeats >= MAX_AUTO_REQUEUES or stage["attempts_used"] >= MAX_ATTEMPT_NUMBERS:
            reason = (
                f"not auto-reconciled: {verdict.reason} repeated {repeats + 1} times"
                if repeats >= MAX_AUTO_REQUEUES
                else "not auto-reconciled: attempt numbers are exhausted"
            )
            self.emit_once(
                ctx,
                "campaign.autoreconcile_stopped",
                {"stage_key": key, "attempt": attempt["attempt"]},
                {
                    "reason": reason,
                    "not_started_reason": verdict.reason,
                    "evidence_digest": verdict.digest,
                },
            )
            self._stage_unknown(ctx, key, reason)
            detail = (reason + "; a person must reconcile")[:500]
            if attempt["failure_detail"] != detail:
                self._set_attempt(attempt["id"], failure_detail=detail)
            return False
        return self._apply_not_started(ctx, key, attempt, verdict, repeats)

    def _apply_not_started(
        self, ctx: _Ctx, key: str, attempt: dict[str, Any], verdict: NotStarted, repeats: int
    ) -> bool:
        with self.store.runner_connection() as connection:
            fresh = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            again = assess(connection, fresh, now=self.now())
            if again is None or again.reason != verdict.reason:
                return False
            self._hold_lease(connection)
            now = _iso(self.now())
            actor = self._actor_principal(connection, ctx.campaign["id"])
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (fresh["grant_id"],)
            ).fetchone()
            # The shared reconcile accounting (ADR 0048); a zero-exposure grant moves no hold.
            if (
                grant["max_exposure_cents"] != 0
                or grant["status"] not in ("active", "cancel_requested")
                or reconcile_grant_row(
                    connection, grant, settlement_cents=0, principal_id=actor, now=now
                )
                is None
            ):
                return False
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status="failed_transient",
                failure_class=NOT_STARTED,
                failure_detail=verdict.detail[:500],
            )
            self._settle(
                connection,
                attempt["id"],
                0,
                "unclaimed_zero",
                {"not_started": {"reason": verdict.reason, "evidence_digest": verdict.digest}},
            )
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=? AND status='blocked_unknown'",
                (ctx.campaign["id"], key),
                status="preparing",
            )
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "execution.not_started_grant_reconciled",
                {
                    "grant_id": grant["id"],
                    "operation_ids": verdict.evidence["operation_ids"],
                    "settlement_cents": 0,
                    "launch_error_reported_by_worker": True,
                    "provider_termination_inferred": False,
                    "operational_only": True,
                },
                task_id=grant["task_id"],
            )
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "campaign.attempt_not_started",
                {
                    "stage_key": key,
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "grant_id": grant["id"],
                    "reason": verdict.reason,
                    "evidence_digest": verdict.digest,
                    "settlement_cents": 0,
                    "released_hold_cents": 0,
                    "consumed_scientific_retry": False,
                    "identical_repeats": repeats + 1,
                    "automatic": True,
                    "is_evidence": False,
                },
                task_id=grant["task_id"],
            )
            current = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (ctx.campaign["id"],)
            ).fetchone()
            if current["status"] in STUCK_CAMPAIGN:
                record_slot_freed(
                    connection,
                    current,
                    fresh,
                    reason=NOT_STARTED_EVIDENCE,
                    trigger="stuck_campaign_reconcile",
                    record_event=self.store.record_runner_event,
                    grant_id=grant["id"],
                )
        self._actions.append(f"not_started:{key}:{attempt['attempt']}:{verdict.reason}")
        return True

    # -- automatic reconciliation of attempts with no claimed provider create (ADR 0157) --

    def _attempt_provider(self, ctx: _Ctx, key: str, attempt: Mapping[str, Any]) -> Any:
        """The provider the attempt ran on, or None when it is not registered."""
        with self.store.runner_connection() as connection:
            row = self._attempt_context(connection, attempt["id"])
        provider_id = None if row is None else row["resolved_provider_id"]
        if provider_id is None:
            try:
                provider_id = self._spec_stage(ctx, key).provider
            except KeyError:
                return None
        return self._provider_or_none(provider_id)

    def _unclaimed_refused(
        self, ctx: _Ctx, key: str, attempt: Mapping[str, Any], refused: Refused
    ) -> bool:
        """Record once why this attempt keeps the human path; always False."""
        if refused.reason == "attempt_not_unknown":
            return False
        self.emit_once(
            ctx,
            "campaign.unclaimed_autoreconcile_refused",
            {
                "stage_key": key,
                "attempt": attempt["attempt"],
                "reason": refused.reason,
                "provider_reason": refused.provider_reason,
            },
            {"attempt_id": attempt["id"], "readable": refused.readable},
        )
        return False

    def _auto_reconcile_unclaimed(self, ctx: _Ctx, key: str, attempt: dict[str, Any]) -> bool:
        """Reconcile an unknown attempt whose provider proves no create was claimed.

        The same zero reconcile a person runs through the operator route: the grant is revoked
        through the delegated step if still active, then in one transaction the guards are
        re-read, the grant is reconciled at zero (its hold released), the attempt settles
        ``unclaimed_zero`` as ``not_started`` and the stage is re-queued under its own caps.
        Bounded per campaign by ``unclaimed_autoreconcile_limit``; past it, or when any guard
        fails, the attempt stays unknown for a person with the reason recorded."""
        provider = self._attempt_provider(ctx, key, attempt)
        with self.store.runner_connection() as connection:
            fresh = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            if fresh is None:
                return False
            verdict = assess_unclaimed(connection, fresh, ctx.campaign, provider)
            used = automatic_count(connection, ctx.campaign)
            # The owner's ``reconcile-unclaimed-attempts`` request for exactly this evidence
            # lifts only the per-campaign automatic limit; every guard still holds.
            asked = (
                None
                if isinstance(verdict, Refused)
                else unclaimed_requested(connection, ctx.campaign, attempt["id"], verdict.digest)
            )
        if isinstance(verdict, Refused):
            return self._unclaimed_refused(ctx, key, attempt, verdict)
        limit = self.unclaimed_autoreconcile_limit
        if used >= limit and asked is None:
            # Re-admission itself stays under the stage's own caps (attempt numbers, retries).
            reason = (
                "not auto-reconciled: automatic unclaimed reconcile is disabled"
                if limit == 0
                else f"not auto-reconciled: {used} of {limit} automatic reconciles used"
            )
            self.emit_once(
                ctx,
                "campaign.autoreconcile_stopped",
                {"stage_key": key, "attempt": attempt["attempt"], "kind": "unclaimed"},
                {
                    "reason": reason,
                    "limit": limit,
                    "used": used,
                    "evidence_digest": verdict.digest,
                },
            )
            return False
        grant = self._one("SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],))
        assert grant is not None
        if grant["status"] == "active":
            # The platform's own reduce-only revoke, as the unwind of any attempt runs it.
            try:
                self._revoke(ctx, key, attempt, grant)
            except StepFailed:
                return self._unclaimed_refused(ctx, key, attempt, Refused("revoke_refused"))
        applied = self._apply_unclaimed(ctx, key, attempt, verdict, provider)
        if isinstance(applied, Refused):
            return self._unclaimed_refused(ctx, key, attempt, applied)
        return applied

    def _apply_unclaimed(
        self,
        ctx: _Ctx,
        key: str,
        attempt: dict[str, Any],
        verdict: Unclaimed,
        provider: Any,
    ) -> bool | Refused:
        with self.store.runner_connection() as connection:
            fresh = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            again = assess_unclaimed(connection, fresh, ctx.campaign, provider)
            if isinstance(again, Refused):
                return again
            if again.digest != verdict.digest:
                return False  # the records moved: judge them afresh next tick
            asked = unclaimed_requested(connection, ctx.campaign, attempt["id"], again.digest)
            if (
                asked is None
                and automatic_count(connection, ctx.campaign) >= self.unclaimed_autoreconcile_limit
            ):
                return False
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=? AND project_id=?",
                (fresh["grant_id"], ctx.campaign["project_id"]),
            ).fetchone()
            if grant is None or grant["status"] != "cancel_requested":
                return False  # the operator route's own prerequisite: a revoked grant
            self._hold_lease(connection)
            now = _iso(self.now())
            actor = self._actor_principal(connection, ctx.campaign["id"])
            # The operator route's accounting (ADR 0048 shared reconcile), at zero.
            revision = reconcile_grant_row(
                connection, grant, settlement_cents=0, principal_id=actor, now=now
            )
            if revision is None:
                return Refused("ledger_inconsistent")
            provider.mark_unclaimed(connection, attempt["id"], now)
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status="failed_transient",
                failure_class=NOT_STARTED,
                failure_detail=verdict.detail[:500],
            )
            self._settle(
                connection,
                attempt["id"],
                0,
                "unclaimed_zero",
                {"unclaimed": {"proof": verdict.proof, "evidence_digest": verdict.digest}},
            )
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=? AND status='blocked_unknown'",
                (ctx.campaign["id"], key),
                status="preparing",
            )
            limits = connection.execute(
                "SELECT currency FROM execution_project_limits WHERE project_id=?",
                (grant["project_id"],),
            ).fetchone()
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "execution.grant_reconciled_unclaimed",
                {
                    "grant_id": grant["id"],
                    "operation_ids": verdict.evidence["operation_ids"],
                    "proof": verdict.proof,
                    "settlement_cents": 0,
                    "currency": None if limits is None else limits["currency"],
                    "automatic": asked is None,
                    **({} if asked is None else {"requested_by_principal_id": asked}),
                    "provider_termination_inferred": False,
                    "operational_only": True,
                },
                task_id=grant["task_id"],
            )
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "campaign.attempt_unclaimed_reconciled",
                {
                    "stage_key": key,
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "grant_id": grant["id"],
                    "reason": "no_create_claimed",
                    "proof": verdict.proof,
                    "evidence_digest": verdict.digest,
                    "settlement_cents": 0,
                    "released_hold_cents": grant["max_exposure_cents"],
                    "consumed_scientific_retry": False,
                    "automatic": asked is None,
                    **({} if asked is None else {"requested_by_principal_id": asked}),
                    "is_evidence": False,
                },
                task_id=grant["task_id"],
            )
        self._actions.append(f"unclaimed:{key}:{attempt['attempt']}")
        return True

    # -- confirmed exit (ADR 0053) -------------------------------------------------

    def _resolve_exited_attempts(self, ctx: _Ctx) -> None:
        """Resolve this campaign's admitted, running or unknown attempts with a confirmed exit."""
        rows = self._rows(
            "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND status IN "
            "('admitted','running','unknown') AND settled_cents IS NULL AND grant_id IS NOT NULL",
            (ctx.campaign["id"],),
        )
        for attempt in rows:
            try:
                self._resolve_exited(ctx, attempt)
            except (sqlite3.IntegrityError, Conflict):
                log.error("exited resolution rolled back for attempt %s", attempt["id"])

    def _resolve_exited(self, ctx: _Ctx, attempt: dict[str, Any]) -> bool:
        """Settle one attempt and release its grant and slot in one transaction; True when it did.

        Re-assessed inside the transaction under the runner lease, guarded on the grant's revision,
        so a repeat, a second sweep or a concurrent human reconcile leaves exactly one effect."""
        key = attempt["stage_key"]
        try:
            stage = self._spec_stage(ctx, key)
            codes = frozenset(stage.retry.transient_exit_codes)
            descriptor = self._provider(stage).descriptor
        except KeyError:
            return False
        free = descriptor.kind == "worker_grant" and not descriptor.billable
        with self.store.runner_connection() as connection:
            fresh = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            if fresh is None:
                return False
            verdict = assess_exited(
                connection,
                fresh,
                now=self.now(),
                transient_codes=codes,
                free_worker_grant=free,
                infra_exit_codes=frozenset(stage.retry.infra_exit_codes),
                infra_retries_enabled=stage.retry.infra_retries > 0,
                progress=self.upload_progress,
                approval_expires=_approval_expiry(ctx),
            )
            if verdict is None:
                return False
            self._hold_lease(connection)
            infra = self._infra_failure(
                ctx,
                key,
                stage,
                fresh,
                verdict.evidence["exit_code"],
                verdict.status,
                verdict.failure_class,
                connection,
            )
            if infra is not None:
                verdict = dataclasses.replace(
                    verdict,
                    status=infra[0],
                    failure_class=infra[1],
                    detail=f"{infra[2]}; resolved on the worker's confirmed exit"[:500],
                )
            now = _iso(self.now())
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (fresh["grant_id"],)
            ).fetchone()
            actor = self._actor_principal(connection, ctx.campaign["id"])
            if (
                release_grant(connection, grant, cents=verdict.cents, principal_id=actor, now=now)
                is None
            ):
                return False
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=?",
                (attempt["id"],),
                status=verdict.status,
                failure_class=verdict.failure_class,
                failure_detail=verdict.detail,
                **(
                    {}
                    if verdict.evidence["exit_code"] is None
                    else {
                        "exit_code": verdict.evidence["exit_code"],
                        "run_receipt_digest": verdict.evidence["run_receipt_digest"],
                    }
                ),
            )
            self._settle(
                connection,
                attempt["id"],
                verdict.cents,
                verdict.basis,
                {"exit_resolved": {"evidence_digest": verdict.digest, **verdict.evidence}},
            )
            self._touch(
                connection,
                "campaign_stages",
                "campaign_id=? AND stage_key=? AND status='blocked_unknown'",
                (ctx.campaign["id"], key),
                status="preparing",
            )
            payload = {
                "grant_id": grant["id"],
                "operation_id": verdict.evidence["operation_id"],
                "settlement_cents": verdict.cents,
                "released_hold_cents": verdict.evidence["hold_cents"],
                "observation_cursor": verdict.evidence["observation_cursor"],
                "provider_termination_inferred": False,
                "operational_only": True,
            }
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "execution.exited_grant_reconciled",
                payload,
                task_id=grant["task_id"],
            )
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "campaign.attempt_exit_resolved",
                {
                    "stage_key": key,
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "outcome": verdict.status,
                    "failure_class": verdict.failure_class,
                    "evidence_digest": verdict.digest,
                    "was_status": verdict.evidence["was_status"],
                    "automatic": True,
                    "is_evidence": False,
                    **{
                        k: payload[k]
                        for k in ("grant_id", "settlement_cents", "released_hold_cents")
                    },
                },
                task_id=grant["task_id"],
            )
            current = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (ctx.campaign["id"],)
            ).fetchone()
            if current["status"] in STUCK_CAMPAIGN:
                record_slot_freed(
                    connection,
                    current,
                    fresh,
                    reason=CONFIRMED_EXIT,
                    trigger="stuck_campaign_reconcile",
                    record_event=self.store.record_runner_event,
                    grant_id=grant["id"],
                )
        self._actions.append(f"exit_resolved:{key}:{attempt['attempt']}:{verdict.failure_class}")
        return True

    # -- exited without a report (ADR 0158) -----------------------------------------

    def _close_unreported_exits(self, ctx: _Ctx) -> None:
        """Close admitted or running attempts whose container the stage watcher saw exit and
        whose executor never reported (``campaign_exit_without_report``)."""
        rows = self._rows(
            "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND status IN "
            "('admitted','running') AND settled_cents IS NULL AND grant_id IS NOT NULL "
            "AND operation_id IS NOT NULL",
            (ctx.campaign["id"],),
        )
        for attempt in rows:
            try:
                self._close_unreported_exit(ctx, attempt)
            except (sqlite3.IntegrityError, Conflict):
                log.error("unreported-exit close rolled back for attempt %s", attempt["id"])

    def _close_unreported_exit(self, ctx: _Ctx, attempt: dict[str, Any]) -> bool:
        """Close one attempt, settle it at zero and release its grant and slot in one transaction.

        Re-assessed inside the transaction under the runner lease and guarded on the grant's
        revision (``release_grant``), exactly like ``_resolve_exited``: a second closer (ADR 0053,
        ADR 0048, a human reconcile, a second sweep) raises ``Conflict`` and rolls this back."""
        key = attempt["stage_key"]
        try:
            stage = self._spec_stage(ctx, key)
            descriptor = self._provider(stage).descriptor
        except KeyError:
            return False
        free = descriptor.kind == "worker_grant" and not descriptor.billable
        with self.store.runner_connection() as connection:
            fresh = connection.execute(
                "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            if fresh is None:
                return False
            verdict = assess_unreported_exit(
                connection,
                fresh,
                now=self.now(),
                free_worker_grant=free,
                transient_codes=frozenset(stage.retry.transient_exit_codes),
                grace=self.exit_report_grace_seconds,
                progress=self.upload_progress,
                approval_expires=_approval_expiry(ctx),
            )
            if verdict is None:
                return False
            self._hold_lease(connection)
            status, failure_class, detail = verdict.status, verdict.failure_class, verdict.detail
            if (
                verdict.exit_class in ("killed", "oom")
                and stage.retry.infra_retries > 0
                and verdict.exit_code in stage.retry.infra_exit_codes
            ):
                # ADR 0058: with infrastructure retries on, a kill spends the infra bound, not a
                # scientific retry. Once that bound is spent the close stays failed_transient /
                # exited_without_report (never permanent on the watcher's word) and the stage's
                # own max_attempts ends it.
                infra = self._infra_failure(
                    ctx,
                    key,
                    stage,
                    fresh,
                    verdict.exit_code,
                    "failed_permanent",
                    "undeclared_exit_code",
                    connection,
                )
                if infra is not None:
                    status, failure_class = infra[0], infra[1]
                    detail = f"{infra[2]}; {verdict.detail}"[:500]
            now = _iso(self.now())
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (fresh["grant_id"],)
            ).fetchone()
            actor = self._actor_principal(connection, ctx.campaign["id"])
            if (
                release_grant(connection, grant, cents=verdict.cents, principal_id=actor, now=now)
                is None
            ):
                return False
            self._touch(
                connection,
                "campaign_stage_attempts",
                "id=? AND settled_cents IS NULL AND status IN ('admitted','running')",
                (attempt["id"],),
                status=status,
                failure_class=failure_class,
                failure_detail=detail,
            )
            evidence = {"evidence_digest": verdict.digest, **verdict.evidence}
            self._settle(
                connection, attempt["id"], verdict.cents, verdict.basis, {"exit_resolved": evidence}
            )
            payload = {
                "grant_id": grant["id"],
                "operation_id": verdict.evidence["operation_id"],
                "settlement_cents": verdict.cents,
                "released_hold_cents": verdict.evidence["hold_cents"],
                "observation_cursor": None,
                "watch_event_cursor": verdict.evidence["watch_event_cursor"],
                "provider_termination_inferred": False,
                "operational_only": True,
            }
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "execution.exited_grant_reconciled",
                payload,
                task_id=grant["task_id"],
            )
            self.store.record_runner_event(
                connection,
                ctx.campaign,
                "campaign.attempt_exit_resolved",
                {
                    "stage_key": key,
                    "attempt": attempt["attempt"],
                    "attempt_id": attempt["id"],
                    "outcome": status,
                    "failure_class": failure_class,
                    "closed_reason": verdict.failure_class,
                    "exit_class": verdict.exit_class,
                    "exit_code": verdict.exit_code,
                    "watch_event_cursor": verdict.evidence["watch_event_cursor"],
                    "evidence_digest": verdict.digest,
                    "was_status": verdict.evidence["was_status"],
                    "automatic": True,
                    "is_evidence": False,
                    **{
                        k: payload[k]
                        for k in ("grant_id", "settlement_cents", "released_hold_cents")
                    },
                },
                task_id=grant["task_id"],
            )
        self._actions.append(
            f"exited_without_report:{key}:{attempt['attempt']}:{verdict.exit_class}"
        )
        return True

    # -- blocked --------------------------------------------------------------------

    def _drive_blocked(self, ctx: _Ctx) -> None:
        """A human reconciles an unknown grant through the evidence routes; observe it."""
        if (self._campaign(ctx.campaign["id"])["stop_reason"] or "").startswith("budget_overrun"):
            return  # a late Salad charge overran the cap; only a human cancel proceeds
        rows = self._rows(
            "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND status='unknown'",
            (ctx.campaign["id"],),
        )
        for attempt in rows:
            # Not-started evidence (ADR 0046) already ran this tick (``_reconcile_stuck``).
            grant = self._one("SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],))
            if grant is not None and grant["status"] == "reconciled":
                try:
                    cents, basis = self._settlement_for(
                        self._spec_stage(ctx, attempt["stage_key"]), attempt
                    )
                except _Waiting:
                    continue  # claimed and no recorded charge yet: wait, never zero
                key = attempt["stage_key"]
                current = self._spec_stage(ctx, key)
                with self.store.runner_connection() as connection:
                    # ADR 0060 addendum: a reconciled attempt that provably never started,
                    # settled at zero, moves the stage to its next candidate instead.
                    moved = self._class_fallback(
                        ctx,
                        key,
                        current,
                        attempt,
                        "reconciled_unknown",
                        connection,
                        settled=cents,
                    )
                    if not moved:
                        self._touch(
                            connection,
                            "campaign_stage_attempts",
                            "id=?",
                            (attempt["id"],),
                            status="failed_permanent",
                        )
                    self._settle(connection, attempt["id"], cents, basis)
                    if moved:
                        self._touch(
                            connection,
                            "campaign_stages",
                            "campaign_id=? AND stage_key=? AND status='blocked_unknown'",
                            (ctx.campaign["id"], key),
                            status="preparing",
                        )
                    else:
                        self._free_campaign_releases(
                            connection, ctx.campaign["id"], "attempt_settled_while_blocked"
                        )
                if moved:
                    continue
                self._set_stage(ctx, attempt["stage_key"], status="failed")
                with self.store.runner_connection() as connection:
                    connection.execute(
                        "UPDATE campaigns SET stop_reason=? WHERE id=? AND stop_reason IS NULL",
                        (
                            f"stage_failed:{attempt['stage_key']}:observed_unknown",
                            ctx.campaign["id"],
                        ),
                    )
        remaining = self._one(
            "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND status='unknown'",
            (ctx.campaign["id"],),
        )
        if remaining is None:
            campaign = self._campaign(ctx.campaign["id"])
            cancelling = (campaign["stop_reason"] or "").startswith("cancel:")
            with self.store.runner_connection() as connection:
                connection.execute(
                    "UPDATE campaigns SET status=?, updated_at=? WHERE id=? AND status='blocked'",
                    (
                        "cancel_requested" if cancelling else "running",
                        _iso(self.now()),
                        ctx.campaign["id"],
                    ),
                )
                self.store.record_runner_event(
                    connection,
                    campaign,
                    "campaign.unblocked",
                    # ``at`` (the runner's clock) restarts the Salad silent-executor clock.
                    {"cancelling": cancelling, "at": _iso(self.now())},
                )

    # -- completion -----------------------------------------------------------------

    def _maybe_complete(self, ctx: _Ctx) -> None:
        campaign = self._campaign(ctx.campaign["id"])
        if campaign["status"] not in {"running", "stopping"}:
            return
        stages = self._rows(
            "SELECT stage_key, status FROM campaign_stages WHERE campaign_id=?", (campaign["id"],)
        )
        if any(stage["status"] not in TERMINAL_STAGE for stage in stages):
            return
        attempts = self._rows(
            "SELECT * FROM campaign_stage_attempts WHERE campaign_id=?", (campaign["id"],)
        )
        if any(
            attempt["status"] not in TERMINAL_ATTEMPT
            or (attempt["grant_id"] is not None and attempt["settled_cents"] is None)
            for attempt in attempts
        ):
            return
        if self._budget > 0 and self._revoke_releases(ctx):
            return
        if self._rows(
            """SELECT 1 FROM snapshot_releases r WHERE r.status='active' AND EXISTS (SELECT 1
            FROM campaign_stage_attempts a WHERE a.campaign_id=? AND a.release_id=r.id)""",
            (campaign["id"],),
        ):
            return
        reason = campaign["stop_reason"] or ""
        if any(stage["status"] == "failed" for stage in stages):
            final = "failed"
        elif reason.startswith("cancel:"):
            final = "cancelled"
        elif reason:
            final = "stopped"
        elif all(stage["status"] == "succeeded" for stage in stages):
            final = "succeeded"
        elif self._rows(
            "SELECT 1 FROM campaign_stage_cancels WHERE campaign_id=?", (campaign["id"],)
        ) and all(stage["status"] in {"succeeded", "cancelled", "skipped"} for stage in stages):
            final, reason = "stopped", "stage_cancelled"
        else:
            final = "failed"
        self._finish(campaign, final, reason=reason or None)

    def _finish(self, campaign: dict[str, Any], status: str, *, reason: str | None = None) -> None:
        """Terminal status, immutable receipt and completion event in one transaction."""
        with self.store.runner_connection() as connection:
            current = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (campaign["id"],)
            ).fetchone()
            if current is None or current["status"] in {
                "succeeded",
                "stopped",
                "failed",
                "cancelled",
            }:
                return
            stamp = _iso(self.now())
            connection.execute(
                "UPDATE campaigns SET status=?, stop_reason=COALESCE(stop_reason, ?), "
                "updated_at=? WHERE id=?",
                (status, reason, stamp, campaign["id"]),
            )
            self._free_campaign_releases(connection, campaign["id"], "campaign_terminal_transition")
            # ADR 0068: a claim the campaign never scored stays claimed, as spent.
            holdout.spend_open_claims(connection, campaign["id"], stamp)
            receipt, digest = build_receipt(connection, current, status)
            connection.execute(
                "INSERT INTO campaign_receipts (campaign_id, receipt_json, receipt_digest, "
                "created_at) VALUES (?,?,?,?)",
                (
                    campaign["id"],
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                    digest,
                    stamp,
                ),
            )
            self.store.record_runner_event(
                connection,
                dict(current),
                "campaign.completed",
                {"status": status, "receipt_digest": digest},
            )
        self._actions.append(f"completed:{campaign['id']}:{status}")


_FAILURE_CLASSES = frozenset(FAILURE_CLASSES) | {"deadline_too_short"}


class RunnerHandle:
    def __init__(self, thread: threading.Thread, stop: threading.Event) -> None:
        self._thread = thread
        self._stop = stop

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._thread.join(timeout)


MAX_BACKOFF_SECONDS = 60.0


def backoff_seconds(interval_seconds: float, failures: int) -> float:
    """The wait after ``failures`` consecutive failed ticks: doubling, bounded."""
    if failures <= 0:
        return interval_seconds
    return min(max(interval_seconds, 1.0) * 2 ** min(failures, 16), MAX_BACKOFF_SECONDS)


def start_background(
    state_dir: Path,
    interval_seconds: float,
    *,
    scheduler: bool = True,
    unclaimed_autoreconcile_limit: int = DEFAULT_UNCLAIMED_AUTORECONCILE_LIMIT,
) -> RunnerHandle:
    runner = CampaignRunner(
        state_dir,
        scheduler=scheduler,
        unclaimed_autoreconcile_limit=unclaimed_autoreconcile_limit,
    )
    stop = threading.Event()

    def loop() -> None:
        failures = 0
        while not stop.is_set():
            try:
                runner.tick()
                failures = 0
            except Exception as error:  # noqa: BLE001 - keep the loop alive, log no payloads
                failures += 1
                log.error(
                    "campaign runner tick failed (%d in a row): %s",
                    failures,
                    type(error).__name__,
                )
            stop.wait(backoff_seconds(interval_seconds, failures))

    thread = threading.Thread(target=loop, name="campaign-runner", daemon=True)
    thread.start()
    return RunnerHandle(thread, stop)


__all__ = ["CampaignRunner", "RunnerHandle", "TickReport", "start_background"]
