"""A route whose execution grant control set ``cancel_requested`` (or whose epoch it fenced) is
stopped by the live check within one poll interval, from control's own authoritative record;
a control stall or a failed read of that record is never a cancel.

Isolated orchestration (fake sessions) plus the real control store for the authority read and
the beat's use-time authority. Never provider evidence; no network.
"""

import sqlite3
from contextlib import closing, nullcontext

import pytest
from test_salad_live_check_running_cancel import (
    DeployingSession,
    RunningSession,
    cancelled_before_output,
    drive,
)

from tessera_control.salad_live_check import (
    CANCEL_CAUSE_CLASSES,
    CONTROL_CALLS,
    POLL_INTERVAL_CAP_SECONDS,
    RUNNING_CANCEL_PREFIX,
    ControlStall,
    SaladLiveSession,
)
from tessera_control.store import Conflict, Forbidden

ACTIVE = {"grant_status": "active", "operation_desired": "admitted", "epoch_current": True}
CANCELLED = {
    "grant_status": "cancel_requested",
    "operation_desired": "cancel_requested",
    "epoch_current": False,
}


class AuthoritySession(RunningSession):
    """Control cancels the grant at ``cancel_at`` (session seconds); every authority read is
    timed. ``authority_errors`` are raised by the read, in order, before any answer."""

    def __init__(self, *, cancel_at=None, authority_errors=(), **kwargs):
        super().__init__(**kwargs)
        self.cancel_at = cancel_at
        self.authority_errors = list(authority_errors)
        self.authority_reads = []
        self.cancelled_at = None

    def execution_authority(self, route):
        self.authority_reads.append(self.now)
        if self.authority_errors:
            raise self.authority_errors.pop(0)
        if self.cancel_at is not None and self.now >= self.cancel_at:
            return dict(CANCELLED)
        return dict(ACTIVE)

    def cancel(self, route):
        if self.cancelled_at is None:
            self.cancelled_at = self.now
        super().cancel(route)
        # The guardian fences the reservation and stops the exact group on the cancel.
        self.proof["stop_confirmed"] = True


class DeployingAuthoritySession(AuthoritySession, DeployingSession):
    pass


def _assert_control_cancel(session, receipt, cancel_at):
    assert cancelled_before_output(session)
    assert session.cancelled_at - cancel_at <= POLL_INTERVAL_CAP_SECONDS
    cause = receipt["cancel_cause"]
    assert cause["class"] == "control_cancel_requested" in CANCEL_CAUSE_CLASSES
    assert cause["exception"] == "ControlCancelRequested"
    assert receipt["control_cancel"] == {
        "grant_status": "cancel_requested",
        "operation_desired": "cancel_requested",
        "epoch_current": False,
        "output_retained": False,
    }
    # The existing finally path: the exact stop was sent and its confirmation read.
    assert "stop" in session.actions
    assert receipt["stop_confirmed"] is True
    assert receipt["status"] != "passed"


# --- (1) a grant control set cancel_requested ends a running route within one interval ------


def test_a_cancel_requested_grant_stops_a_running_route_within_one_interval():
    # The beat keeps succeeding (nothing refuses): the authoritative read alone decides.
    session = AuthoritySession(cancel_at=35)
    receipt = drive(session, max_polls=40, max_seconds=400)
    _assert_control_cancel(session, receipt, 35)
    assert receipt["reason"] == RUNNING_CANCEL_PREFIX + "control_cancel_requested"
    assert receipt["running_latch"]["state"] == "seen_running"


def test_a_control_cancel_wins_over_a_held_unverified_refusal_of_the_beat():
    # After the cancel the beat is refused with an unverified Forbidden (held as a stall on a
    # running route); the authority read still stops the route at the next pass.
    session = AuthoritySession(
        cancel_at=35, beat_errors=[None] * 4 + [Forbidden("Unauthorized")] * 50
    )
    receipt = drive(session, max_polls=40, max_seconds=400)
    _assert_control_cancel(session, receipt, 35)


def test_the_slow_cadence_heartbeat_loop_checks_control_every_interval():
    session = DeployingAuthoritySession(cancel_at=45)
    receipt = drive(session, max_polls=40, max_seconds=400)
    _assert_control_cancel(session, receipt, 45)
    reads = session.authority_reads
    assert max(b - a for a, b in zip(reads, reads[1:], strict=False)) <= POLL_INTERVAL_CAP_SECONDS


def test_output_retained_before_the_cancel_still_completes():
    session = AuthoritySession(cancel_at=0, retain_after=0)
    session.proof.update(output_retained=True, stop_confirmed=True)
    receipt = drive(session, max_polls=40, max_seconds=400)
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["control_cancel"]["output_retained"] is True


# --- (2) a transient control stall while running is still held -------------------------------


@pytest.mark.parametrize(
    "stall",
    [
        lambda: ControlStall(beat_delivered=True),
        lambda: sqlite3.OperationalError("database is locked"),
        lambda: Forbidden("Unauthorized"),
    ],
)
def test_a_control_stall_while_running_with_an_active_grant_is_held(stall):
    session = AuthoritySession(retain_after=6, beat_errors=[None] + [stall() for _ in range(5)])
    receipt = drive(session, max_polls=40, max_seconds=400)
    assert receipt["status"] == "passed", receipt
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert "control_cancel" not in receipt


# --- (3) a failed read of grant state is not a cancel ----------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        lambda: sqlite3.OperationalError("database is locked"),
        lambda: TimeoutError("bounded live operation expired"),
        lambda: Forbidden("Unauthorized"),
        lambda: Conflict("Remote tenant/plan/attempt/package/manifest binding mismatch."),
        lambda: ValueError("execution authority unreadable"),
    ],
)
def test_a_failed_authority_read_is_never_a_cancel(error):
    session = AuthoritySession(retain_after=4, authority_errors=[error() for _ in range(6)])
    receipt = drive(session, max_polls=40, max_seconds=400)
    assert receipt["status"] == "passed", receipt
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["control_authority_read_failures"] == 6
    assert "control_cancel" not in receipt
    assert set(receipt["last_control_authority_read_failure"]) >= {"exception", "message"}


def test_a_malformed_authority_answer_is_a_failed_read():
    session = AuthoritySession(retain_after=2)
    session.execution_authority = lambda route: {"grant_status": None}
    receipt = drive(session, max_polls=40, max_seconds=400)
    assert receipt["status"] == "passed", receipt
    assert receipt["control_authority_read_failures"] >= 1


# --- the real store: the authority read, and the beat's own refusal after a grant cancel ----


@pytest.fixture
def real_route(tmp_path, monkeypatch):
    from test_salad_heartbeat_independence import _server

    monkeypatch.setattr("tessera_control.salad_live_check._wall_bound", lambda s: nullcontext())
    h, route, _, database = _server(tmp_path, monkeypatch)
    session = object.__new__(SaladLiveSession)
    session.server = h.server
    session.state = lambda r: h.store.get(h.worker, r)
    session._registry_heartbeat = lambda: None
    return h, route, database, session


def _cancel_grant(h, route, database):
    """Control's own grant cancellation (the transition every cancel path uses)."""
    from tessera_control.execution import ExecutionStore
    from tessera_control.store import _connect

    proposal = h.store.get(h.worker, route)["proposal"]
    with closing(_connect(database)) as db:
        db.execute("BEGIN IMMEDIATE")
        grant_id = db.execute(
            "SELECT grant_id FROM external_operations WHERE operation_id=?",
            (proposal["operation_id"],),
        ).fetchone()[0]
        grant = db.execute("SELECT * FROM execution_grants WHERE id=?", (grant_id,)).fetchone()
        ExecutionStore._request_grant_cancellation(h.store, db, grant, grant["revision"])
        db.execute("COMMIT")


def test_real_authority_read_sees_the_cancel_and_the_beat_refuses(real_route):
    h, route, database, session = real_route
    assert session.execution_authority(route) == ACTIVE
    session.keepalive(route)
    _cancel_grant(h, route, database)
    assert session.execution_authority(route) == CANCELLED
    # The beat's own use-time authority refuses too (a Conflict, terminal for the driver).
    before = len(h.safety_calls)
    with pytest.raises(Conflict) as refused:
        session.keepalive(route)
    assert refused.value.live_check_call == "TrustedSaladServer.heartbeat"
    assert h.safety_calls[before:] == []
    assert "Store.execution_authority" in CONTROL_CALLS


def test_real_authority_read_under_a_write_lock_is_a_stall_not_a_cancel(real_route):
    h, route, database, session = real_route
    state = h.store.get(h.worker, route)
    session.state = lambda r: state
    holder = sqlite3.connect(database, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError) as stalled:
            session.execution_authority(route)
        assert stalled.value.live_check_call == "Store.execution_authority"
    finally:
        holder.execute("ROLLBACK")
        holder.close()
