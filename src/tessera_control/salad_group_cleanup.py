"""Audited cleanup of stopped, zero-instance Salad container groups Tessera created.

Candidates come ONLY from Tessera's own route records (the retained
``remote_execution.state`` events). This module never lists the provider's groups,
so a group Tessera cannot prove it created is never read, let alone deleted. A
route is eligible only if its record binds a provider id AND the recorded group name
is exactly ``attempt-<operation_id>``; name-only ownership is never enough. Candidates
come only from routes whose external operation belongs to the calling worker.

Deleting a stopped group is hygiene (it frees the project's group quota). It does
not release experiment liability, budget or any recorded cost; receipts say so by
carrying no cost and no relief claim.

Live deletion is NOT available in this milestone, and there is no production port:
no adapter over the real HTTP transport exists. ``apply`` accepts only a provider port
marked ``live is False``; ``TrustedSaladServer.cleanup_settled`` additionally requires
the server's own transport to be non-live, which the real one is not, so against a real
server the hook always refuses. The mark is a guard against accidental live use, not a
security boundary (a subclass or an instance attribute can satisfy it). Real calls
would skip the one-use claim, preflight freshness, rebinding, stop and input-revocation
guards. Receipts stay ``synthetic`` and, with the audit events, say the read-back came
from a non-live port. Limits: the no-transaction guard sees only store-mutate lineage;
the provider read, drift re-read and delete are separate calls.
"""

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Protocol
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .store import (
    _EVENT_LINEAGE,
    Conflict,
    Forbidden,
    NotFound,
    _parse_utc,
    canonical_digest,
    utc_now,
)

AUDIT_EVENT = "remote.group_cleanup_recorded"
MARKER = "cleanup of a stopped zero-instance group"
STATE_EVENT = "remote_execution.state"
SALAD_TARGET = "salad_container"
DEFAULT_MIN_AGE_SECONDS = 3600

# Finite skip-reason vocabulary (static eligibility, in evaluation order).
ELIGIBLE = "eligible"
NOT_SALAD_TARGET = "not_salad_container_target"
ALREADY_CLEANED = "already_cleaned"
DELETE_ALREADY_ATTEMPTED = "delete_already_attempted"
UNRESOLVED_EFFECT = "unresolved_effect"
NOT_STOPPED = "not_stopped"
TERMINATION_NOT_CONFIRMED = "termination_not_confirmed"
STOP_NOT_DESIRED = "stop_not_desired"
REALLOCATED = "reallocated"
NO_BOUND_PROVIDER_ID = "no_bound_provider_id"
PROVIDER_ID_MALFORMED = "provider_id_malformed"
GROUP_NAME_MISMATCH = "group_name_mismatch"
STOP_TOO_RECENT = "stop_too_recent"
OUT_OF_SCOPE = "outside_server_scope"
STATIC_REASONS = (
    NOT_SALAD_TARGET,
    ALREADY_CLEANED,
    DELETE_ALREADY_ATTEMPTED,
    UNRESOLVED_EFFECT,
    NOT_STOPPED,
    TERMINATION_NOT_CONFIRMED,
    STOP_NOT_DESIRED,
    REALLOCATED,
    NO_BOUND_PROVIDER_ID,
    PROVIDER_ID_MALFORMED,
    GROUP_NAME_MISMATCH,
    STOP_TOO_RECENT,
    OUT_OF_SCOPE,
)
# Apply-time (read-back before any intent) skip reasons.
READ_FAILED = "provider_read_failed"
IDENTITY_MISMATCH = "identity_mismatch"
GROUP_ABSENT = "group_absent_at_read"
GROUP_NOT_STOPPED = "group_not_stopped"
GROUP_PENDING_CHANGE = "group_pending_change"
GROUP_HAS_INSTANCES = "group_has_instances"
BEGIN_REFUSED = "begin_refused"
NOT_AUTHORIZED = "not_authorized"
UNEXPECTED_ERROR = "unexpected_error"
_DELETE_DECISIONS = frozenset(
    {"deleted", "delete_unconfirmed", "delete_error", "delete_unrecorded"}
)
INTENT_NOT_RECORDED = "intent_not_recorded"
DRIFT_ABORT = "delete_aborted_drift"
DELETE_UNRECORDED = "delete_unrecorded"
READBACK_SOURCE = "non_live_port"
APPLY_REASONS = (
    READ_FAILED,
    IDENTITY_MISMATCH,
    GROUP_ABSENT,
    GROUP_NOT_STOPPED,
    GROUP_PENDING_CHANGE,
    GROUP_HAS_INSTANCES,
    BEGIN_REFUSED,
    NOT_AUTHORIZED,
    INTENT_NOT_RECORDED,
)

DRY_RUN_STATEMENT = (
    "Dry run: no provider call was made and nothing was changed. Provider read-back "
    "is NOT performed in a dry run; every candidate is re-verified against the "
    "provider at apply time."
)
LIABILITY_STATEMENT = (
    "Deleting a stopped group does not release experiment liability or budget and "
    "records no cost relief."
)


class GroupIdentityMismatch(Exception):
    """The provider's group is not the one the route record proves Tessera created."""


class ProviderError(Exception):
    """A provider read or delete failed or was uncertain; never retried."""


@dataclass(frozen=True)
class GroupObservation:
    """One exact read of a bound group. ``present=False`` is an absent group (404)."""

    present: bool
    name: str | None = None
    provider_id: str | None = None
    state: str | None = None
    instance_counts: dict = field(default_factory=dict)
    pending_change: bool | None = None
    instances: tuple = ()
    response_status: int | None = None


@dataclass(frozen=True)
class RouteSnapshot:
    state: dict
    recorded_at: str  # when the route's latest state revision was recorded

    @property
    def revision(self):
        """The route revision this snapshot was read at (checked again by begin)."""
        return self.state.get("revision")


# ---- read-only access to Tessera's own records -------------------------------------


def open_readonly(database_path):
    """Open the state database read-only; never creates or writes it."""
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError("state database not found")
    uri = "file:" + quote(str(path.resolve())) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def read_route_snapshots(database_path, *, org_id=None, admit=None):
    """Latest state of every recorded route, ordered by (recorded_at, route id).

    ``admit(db, state)`` (optional) is the caller's authorization filter, run on the
    read-only connection; a route it refuses is dropped. The CLI passes none: it is a
    local operator tool that reads every organization in the state directory.
    """
    latest = {}
    with closing(open_readonly(database_path)) as db:
        rows = db.execute(
            "SELECT org_id, created_at, payload_json FROM events "
            "WHERE event_type=? ORDER BY cursor",
            (STATE_EVENT,),
        )
        for row in rows:
            if org_id is not None and row["org_id"] != org_id:
                continue
            state = json.loads(row["payload_json"])
            latest[(row["org_id"], state["id"])] = RouteSnapshot(state, row["created_at"])
        if admit is not None:
            latest = {k: v for k, v in latest.items() if admit(db, v.state)}
    return sorted(latest.values(), key=lambda s: (s.recorded_at, s.state["id"]))


# ---- eligibility (pure, from the route record only) --------------------------------


def expected_group(operation_id):
    return "attempt-" + str(operation_id)


def evaluate_route(snapshot, *, now, min_age_seconds):
    """Return ELIGIBLE or exactly one finite skip-reason token."""
    state = snapshot.state
    proposal = state.get("proposal") or {}
    selection = proposal.get("selection") or {}
    salad = selection.get("salad") or {}
    if selection.get("target") != SALAD_TARGET or not salad:
        return NOT_SALAD_TARGET
    if state.get("state") == "deleted" or state.get("provider_status") == "deleted":
        return ALREADY_CLEANED
    if any(e.get("action") == "delete" for e in state.get("effects", [])):
        return DELETE_ALREADY_ATTEMPTED
    if state.get("state") == "unknown":
        return UNRESOLVED_EFFECT
    if state.get("state") != "stopped":
        return NOT_STOPPED
    if state.get("termination_confirmed") is not True:
        return TERMINATION_NOT_CONFIRMED
    if state.get("desired") != "stop":
        return STOP_NOT_DESIRED
    if state.get("reallocated"):
        return REALLOCATED
    provider_id = state.get("provider_id")
    if not provider_id:
        return NO_BOUND_PROVIDER_ID
    if not isinstance(provider_id, str) or not re.fullmatch(r"[a-f0-9]{32}", provider_id):
        return PROVIDER_ID_MALFORMED
    operation_id = proposal.get("operation_id")
    group = salad.get("group")
    if (
        not isinstance(operation_id, str)
        or not re.fullmatch(r"[a-f0-9]{32}", operation_id)
        or group != expected_group(operation_id)
    ):
        return GROUP_NAME_MISMATCH
    try:
        age = now - _parse_utc(snapshot.recorded_at).timestamp()
    except ValueError:
        return STOP_TOO_RECENT
    if age < min_age_seconds:
        return STOP_TOO_RECENT
    return ELIGIBLE


def _decision(snapshot, reason, *, now):
    state = snapshot.state
    proposal = state.get("proposal") or {}
    salad = (proposal.get("selection") or {}).get("salad") or {}
    try:
        age = max(0, int(now - _parse_utc(snapshot.recorded_at).timestamp()))
    except ValueError:
        age = None
    return dict(
        route_id=state["id"],
        operation_id=proposal.get("operation_id"),
        group=salad.get("group"),
        provider_id=state.get("provider_id"),
        age_seconds=age,
        decision="eligible" if reason == ELIGIBLE else "skipped",
        reason=reason,
    )


def _summary(decisions):
    counts = {}
    for d in decisions:
        counts[d["reason"]] = counts.get(d["reason"], 0) + 1
    return dict(
        considered=len(decisions),
        eligible=sum(1 for d in decisions if d["decision"] == "eligible"),
        skipped=sum(1 for d in decisions if d["decision"] == "skipped"),
        reasons=dict(sorted(counts.items())),
    )


def build_plan(snapshots, *, now, min_age_seconds):
    """Dry-run decision list from records only: no provider call, no write."""
    decisions = [
        _decision(s, evaluate_route(s, now=now, min_age_seconds=min_age_seconds), now=now)
        for s in snapshots
    ]
    return dict(
        dry_run=True,
        min_age_seconds=min_age_seconds,
        provider_calls=0,
        provider_readback_performed=False,
        changed_anything=False,
        statement=DRY_RUN_STATEMENT,
        liability=LIABILITY_STATEMENT,
        decisions=decisions,
        counts=_summary(decisions),
    )


# ---- provider port ----------------------------------------------------------------


class GroupPort(Protocol):
    """What ``apply`` needs from a provider port. ``live`` must be exactly False."""

    live: bool

    def read_group(self, state) -> GroupObservation: ...

    def delete(self, state) -> int: ...


_TOKEN = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,47}$")]


class CleanupAudit(BaseModel):
    """Strict shape of one cleanup audit event; nothing else may be recorded."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    phase: Literal["plan", "intent", "skip", "outcome"]
    decision: _TOKEN
    reason: _TOKEN
    delete_status: Literal["not_sent", "error", "accepted_pending", "sent"]
    confirmed_gone: bool | None
    dry_run: bool
    receipt_marker: Literal["cleanup of a stopped zero-instance group"]
    liability_released: Literal[False]
    cost_relief_claimed: Literal[False]
    min_age_seconds: Annotated[int, Field(ge=0, le=10**9)]
    effect_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")] | None = None
    needs_reconciliation: bool | None = None
    readback_source: Literal["non_live_port"] | None = None
    marker_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None


def _check_observation(obs, state, group):
    """Return None if the read proves a deletable group, else a skip reason."""
    if obs.name != group or obs.provider_id != state["provider_id"]:
        return IDENTITY_MISMATCH
    if obs.state != "stopped":
        return GROUP_NOT_STOPPED
    if obs.pending_change is not False:
        return GROUP_PENDING_CHANGE
    counts = obs.instance_counts
    if (
        set(counts) != {"allocating_count", "creating_count", "running_count", "stopping_count"}
        or any(type(v) is not int or v != 0 for v in counts.values())
        or obs.instances
    ):
        return GROUP_HAS_INSTANCES
    return None


# ---- the cleanup -------------------------------------------------------------------


class SaladGroupCleanup:
    """Collaborators: a route store (begin/record/record_group_cleanup/route_snapshots),
    the worker principal that owns the routes, and a provider port.

    Only apply() can reach the provider, one route at a time, in deterministic order,
    and only through a port marked non-live.
    """

    def __init__(
        self,
        store,
        worker,
        port=None,
        *,
        min_age_seconds=DEFAULT_MIN_AGE_SECONDS,
        clock=time.time,
    ):
        if type(min_age_seconds) is not int or min_age_seconds < 0:
            raise ValueError("min_age_seconds must be a non-negative integer")
        self.store, self.worker, self.port = store, dict(worker), port
        self.min_age_seconds, self.clock = min_age_seconds, clock

    # -- helpers
    def _audit(self, route_id, **fields):
        return self.store.record_group_cleanup(
            self.worker,
            route_id,
            dict(
                fields,
                receipt_marker=MARKER,
                liability_released=False,
                cost_relief_claimed=False,
                min_age_seconds=self.min_age_seconds,
            ),
        )

    @staticmethod
    def require_non_live(port):
        if getattr(port, "live", True) is not False:
            raise ProviderError("live group deletion has no activation gate in this milestone")

    @staticmethod
    def _require_no_transaction():
        # Store commands set an event lineage for their whole transaction.
        if _EVENT_LINEAGE.get() is not None:
            raise Conflict("Group cleanup never runs inside a store transaction.")

    # -- dry run
    def plan(self, *, record_plan=False, accept=None):
        """Decisions from records only. No provider call; writes only if record_plan."""
        self._require_no_transaction()
        now = self.clock()
        snapshots = self.store.route_snapshots(self.worker)
        plan = build_plan(
            [s for s in snapshots if accept is None or accept(s.state)],
            now=now,
            min_age_seconds=self.min_age_seconds,
        )
        plan["recorded"] = False
        if record_plan:
            plan["unrecorded"] = 0
            for d in plan["decisions"]:
                if d["reason"] == NOT_SALAD_TARGET:
                    continue  # never write onto routes that are not Salad routes
                try:
                    self._audit(
                        d["route_id"],
                        phase="plan",
                        decision=d["decision"],
                        reason=d["reason"],
                        delete_status="not_sent",
                        confirmed_gone=None,
                        dry_run=True,
                    )
                except (Conflict, Forbidden, NotFound):
                    d["audited"] = False  # never abort the plan midway
                    plan["unrecorded"] += 1
            plan["recorded"] = plan["unrecorded"] == 0
        return plan

    # -- apply
    def apply(self, *, max_groups=1, port=None, accept=None):
        """Verify, record intent, delete once, read back, record the outcome.

        Per eligible route, in order: provider read of the exact bound group ->
        begin('delete', expected_revision=...) (the durable intent commit) -> intent
        audit carrying the effect id -> second provider read, required to still show
        the same stopped, idle group -> provider delete (202 is pending) -> read back
        -> receipt -> outcome audit. Stops the whole run at the first provider error,
        identity mismatch or aborted delete. A route is never deleted twice; a drift
        abort consumes the one-shot effect and leaves the route unresolved for
        read-only reconciliation (the safe direction).
        """
        self._require_no_transaction()
        port = port or self.port
        if port is None:
            raise ValueError("a provider port is required to apply")
        self.require_non_live(port)
        if type(max_groups) is not int or max_groups < 1:
            raise ValueError("max_groups must be a positive integer")
        now = self.clock()
        decisions, processed, stopped = [], 0, None
        snapshots = self.store.route_snapshots(self.worker)
        for snap in snapshots:
            state = snap.state
            if accept is not None and not accept(state):
                decisions.append(_decision(snap, OUT_OF_SCOPE, now=now))
                continue
            reason = evaluate_route(snap, now=now, min_age_seconds=self.min_age_seconds)
            decision = _decision(snap, reason, now=now)
            decisions.append(decision)
            if reason != ELIGIBLE:
                continue
            if stopped is not None or processed >= max_groups:
                decision.update(
                    decision="deferred",
                    reason="run_stopped" if stopped is not None else "max_groups_reached",
                )
                continue
            processed += 1
            try:
                stopped = self._apply_one(port, snap, decision)
            except (Forbidden, NotFound):
                # A store refusal before any delete was sent: stop safely, unaborted.
                # (Once a delete was sent, _apply_one reports delete_unrecorded itself.)
                decision.update(decision="skipped", reason=NOT_AUTHORIZED)
                stopped = NOT_AUTHORIZED
            except Exception as exc:
                # Never lose the decisions gathered so far (earlier deletes may have been
                # sent): stop the run, mark this route for reconciliation, still report.
                decision.update(needs_reconciliation=True, error=type(exc).__name__)
                if not decision.get("delete_sent"):
                    decision.update(decision="skipped", reason=UNEXPECTED_ERROR)
                elif decision["decision"] not in _DELETE_DECISIONS:
                    # A delete went out: never report it as skipped.
                    decision.update(decision=DELETE_UNRECORDED, reason=UNEXPECTED_ERROR)
                stopped = UNEXPECTED_ERROR
        counts = _summary(decisions)
        counts.update(
            deleted_confirmed=sum(1 for d in decisions if d["decision"] == "deleted"),
            delete_unconfirmed=sum(1 for d in decisions if d["decision"] == "delete_unconfirmed"),
            delete_errors=sum(1 for d in decisions if d["decision"] == "delete_error"),
            delete_unrecorded=sum(1 for d in decisions if d["decision"] == DELETE_UNRECORDED),
            delete_aborted_drift=sum(1 for d in decisions if d["decision"] == DRIFT_ABORT),
            deferred=sum(1 for d in decisions if d["decision"] == "deferred"),
        )
        return dict(
            dry_run=False,
            min_age_seconds=self.min_age_seconds,
            max_groups=max_groups,
            decisions=decisions,
            counts=counts,
            run_stopped=stopped,
            liability=LIABILITY_STATEMENT,
        )

    def _apply_one(self, port, snap, decision):
        """Process one statically eligible route. Return a stop reason or None."""
        state = snap.state
        ident, group = state["id"], decision["group"]
        # (a) read-only provider read of the exact bound group.
        try:
            obs = port.read_group(state)
        except GroupIdentityMismatch:
            return self._skip(decision, IDENTITY_MISMATCH, stop=True)
        except Exception:
            return self._skip(decision, READ_FAILED, stop=True)
        if not obs.present:
            return self._skip(decision, GROUP_ABSENT, stop=False)
        reason = _check_observation(obs, state, group)
        if reason is not None:
            return self._skip(decision, reason, stop=reason == IDENTITY_MISMATCH)
        # (b) begin is the durable intent: it commits the one-shot delete effect only if
        # the route is still at the revision that was evaluated.
        try:
            intent = self.store.begin(self.worker, ident, "delete", expected_revision=snap.revision)
        except (Forbidden, NotFound):
            return self._skip(decision, NOT_AUTHORIZED, stop=True)
        except Conflict:
            return self._skip(decision, BEGIN_REFUSED, stop=False)
        effect_id = intent["effects"][-1]["id"]
        try:
            self._audit(
                ident,
                phase="intent",
                decision="delete_intended",
                reason=ELIGIBLE,
                effect_id=effect_id,
                delete_status="not_sent",
                confirmed_gone=None,
                readback_source=READBACK_SOURCE,
                dry_run=False,
            )
        except (Conflict, Forbidden, NotFound):
            # No intent evidence, so no delete; the effect stays for reconciliation.
            decision.update(decision="skipped", reason=INTENT_NOT_RECORDED)
            return INTENT_NOT_RECORDED
        # (c) re-read immediately before the delete: any drift sends nothing.
        drift = None
        try:
            again = port.read_group(state)
            if not again.present:
                drift = GROUP_ABSENT
            else:
                drift = _check_observation(again, state, group)
        except GroupIdentityMismatch:
            drift = IDENTITY_MISMATCH
        except Exception:
            drift = READ_FAILED
        if drift is not None:
            decision.update(decision=DRIFT_ABORT, reason=drift)
            self._audit_outcome_guarded(ident, decision, effect_id, "not_sent", None, needs=True)
            return drift if drift in {IDENTITY_MISMATCH, READ_FAILED} else None
        # (d) send the delete. 202 is pending, never confirmation. From here on a delete
        # may have been sent, so nothing below may report the route as skipped.
        decision["delete_sent"] = True  # from here a delete may have reached the provider
        try:
            port.delete(state)
        except Exception:
            decision.update(decision="delete_error", reason="delete_failed")
            self._audit_outcome_guarded(ident, decision, effect_id, "error", None, needs=True)
            return "delete_failed"  # route stays unresolved; never retried
        # (e) read back once.
        gone = False
        try:
            gone = not port.read_group(state).present
        except Exception:
            gone = False
        # (f) receipt on the route, then the outcome audit.
        try:
            self._record_receipt(ident, state, effect_id, gone)
        except (Conflict, Forbidden, NotFound):
            decision.update(decision=DELETE_UNRECORDED, reason="receipt_not_recorded")
            self._audit_outcome_guarded(ident, decision, effect_id, "sent", gone, needs=True)
            return DELETE_UNRECORDED
        if gone:
            decision.update(decision="deleted", reason="confirmed_gone")
        else:
            decision.update(decision="delete_unconfirmed", reason="group_not_confirmed_gone")
        try:
            self._audit_outcome(
                ident,
                decision,
                effect_id,
                "sent" if gone else "accepted_pending",
                gone,
                needs=not gone,
            )
        except (Conflict, Forbidden, NotFound):
            decision.update(decision=DELETE_UNRECORDED, reason="outcome_not_audited")
            decision["audited"] = False
            return DELETE_UNRECORDED
        return None

    def _skip(self, decision, reason, *, stop):
        decision.update(decision="skipped", reason=reason)
        try:
            self._audit(
                decision["route_id"],
                phase="skip",
                decision="skipped",
                reason=reason,
                delete_status="not_sent",
                confirmed_gone=None,
                dry_run=False,
            )
        except (Conflict, Forbidden, NotFound):
            decision["audited"] = False  # never abort the run unaudited-and-unreported
        return reason if stop else None

    def _audit_outcome_guarded(self, ident, decision, effect_id, status, gone, *, needs):
        """Best-effort outcome audit; a refusal is reported on the decision, never raised."""
        try:
            self._audit_outcome(ident, decision, effect_id, status, gone, needs=needs)
        except (Conflict, Forbidden, NotFound):
            decision["audited"] = False

    def _audit_outcome(self, ident, decision, effect_id, status, gone, *, needs):
        self._audit(
            ident,
            phase="outcome",
            decision=decision["decision"],
            reason=decision["reason"],
            effect_id=effect_id,
            delete_status=status,
            confirmed_gone=gone,
            needs_reconciliation=needs,
            readback_source=READBACK_SOURCE,
            marker_sha256=hashlib.sha256(MARKER.encode()).hexdigest(),
            dry_run=False,
        )

    def _record_receipt(self, ident, state, effect_id, gone):
        # Only a confirmed-gone group is recorded as deleted. Anything else is an
        # accepted but unreconciled delete: state unknown, termination NOT confirmed,
        # so the route can neither be started nor deleted again.
        evidence = [
            dict(
                kind="event",
                sha256=canonical_digest(
                    dict(
                        marker=MARKER,
                        effect_id=effect_id,
                        confirmed_gone=gone,
                        readback_source=READBACK_SOURCE,
                    )
                ),
            )
        ]
        readback = dict(
            provider_id=state["provider_id"],
            state="deleted" if gone else "unknown",
            observed_at=utc_now(),
            request_id=effect_id,
            response_status=404 if gone else None,
            termination_confirmed=gone,
            cost_cents=None,
            evidence=evidence,
            provenance="synthetic",  # the read-back came from a non-live port
            proxy_reason="observed" if gone else "accepted_pending",
        )
        self.store.record(self.worker, ident, effect_id, readback)
