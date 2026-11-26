"""A transient control-database lock is waited out or held, never turned into a cancel.

* The official control connection waits out a lock longer than the ~30 s one observed live
  (rollback-journal mode is kept: backup/restore normalizes to journal_mode=DELETE).
* A real SQLite write lock that blocks the concrete keepalive (lease renewal and guardian beat)
  past the guardian silence bound, on a route last read running with no guardian stop cause,
  is recorded as a control stall and the route is not cancelled.
* ``tessera campaign reconcile-completion-cancels --campaign-id ...`` (a repeatable flag) is
  dispatched without quoting the id list.

Temporary databases and fake sessions only; no provider, no network.
"""

import json
import sqlite3

import pytest
from test_salad_live_check_running_cancel import (  # noqa: F401 (autouse fixture)
    RunningSession,
    _unbounded_wall,
    concrete,
    drive,
)

from tessera_control.salad_live_check import (
    GUARDIAN_SILENCE_SECONDS,
    RUNNING_CANCEL_PREFIX,
    ControlStall,
    SaladLiveSession,
)
from tessera_control.store import CONTROL_BUSY_TIMEOUT_SECONDS, _connect

# --- the official control connection waits out a transient lock --------------------------------


def test_official_control_connection_waits_out_a_thirty_second_lock(tmp_path):
    connection = _connect(tmp_path / "control.sqlite3")
    try:
        (busy_ms,) = connection.execute("PRAGMA busy_timeout").fetchone()
        (journal,) = connection.execute("PRAGMA journal_mode").fetchone()
    finally:
        connection.close()
    assert busy_ms == CONTROL_BUSY_TIMEOUT_SECONDS * 1000
    # Longer than the observed ~30 s control lock with a margin.
    assert busy_ms > (GUARDIAN_SILENCE_SECONDS + 10) * 1000
    # The rollback journal stays (no WAL sidecars for backup/restore to reject).
    assert journal.lower() == "delete"


# --- integration: a real SQLite lock through the concrete keepalive ------------------------------


class _LockedStore:
    """Lease renewal is a real write on a temporary database that may be locked."""

    def __init__(self, path):
        self.path, self.calls, self.renewals = path, [], 0

    def renew_execution_lease(self, *args):
        self.calls.append("renew")
        writer = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            writer.execute("UPDATE lease SET renewals = renewals + 1")
        finally:
            writer.close()
        self.renewals += 1


@pytest.fixture
def locked_db(tmp_path):
    path = tmp_path / "control.sqlite3"
    setup = sqlite3.connect(path, isolation_level=None)
    setup.execute("CREATE TABLE lease (renewals INTEGER NOT NULL)")
    setup.execute("INSERT INTO lease VALUES (0)")
    setup.close()
    holder = sqlite3.connect(path, isolation_level=None)
    yield path, holder
    if holder.in_transaction:
        holder.execute("ROLLBACK")
    holder.close()


def test_concrete_keepalive_raises_a_stall_on_a_real_lock(locked_db):
    path, holder = locked_db
    session = concrete()
    session.server.store = _LockedStore(path)
    session.server.heartbeat = lambda route: _write(path)
    holder.execute("BEGIN EXCLUSIVE")
    with pytest.raises(ControlStall) as raised:
        session.keepalive("route")
    assert raised.value.beat_delivered is False
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert "locked" in str(raised.value.__cause__)


def _write(path):
    writer = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        writer.execute("UPDATE lease SET renewals = renewals")
    finally:
        writer.close()


class ConcreteBeatSession(RunningSession):
    """A RunningSession whose keepalive is SaladLiveSession.keepalive against a real database
    that another connection holds write-locked for ``locked_beats`` post-start beats."""

    def __init__(self, path, holder, *, locked_beats, **kwargs):
        super().__init__(**kwargs)
        self.holder, self.locked_beats, self.beats_after_start = holder, locked_beats, 0
        self.inner = concrete()
        self.inner.server.store = _LockedStore(path)
        self.inner.server.heartbeat = lambda route: _write(path)

    def keepalive(self, route):
        self.keepalives += 1
        if "start" in self.actions:
            self.beats_after_start += 1
            locked = 2 <= self.beats_after_start < 2 + self.locked_beats
            if locked and not self.holder.in_transaction:
                self.holder.execute("BEGIN EXCLUSIVE")
            elif not locked and self.holder.in_transaction:
                self.holder.execute("ROLLBACK")
        SaladLiveSession.keepalive(self.inner, route)


def test_a_real_lock_past_the_silence_bound_does_not_cancel_a_running_route(locked_db):
    path, holder = locked_db
    session = ConcreteBeatSession(path, holder, locked_beats=6, retain_after=9)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["control_stalls"] == 6
    assert receipt["guardian_refusal"] is None
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)
    # Six blocked beats at the 10 s interval: well past the guardian silence bound.
    assert 6 * 10 > GUARDIAN_SILENCE_SECONDS
    # Renewals resumed once the lock was released.
    assert session.inner.server.store.renewals >= 2


def test_a_real_lock_with_a_guardian_stop_cause_is_not_held(locked_db):
    path, holder = locked_db
    session = ConcreteBeatSession(path, holder, locked_beats=20, stop_cause="missed_poll")
    receipt = drive(session)
    assert session.cancelled
    assert receipt["cancel_cause"]["class"] == "guardian_stop_cause"
    assert receipt["cancel_cause"]["exception"] == "ControlStall"


# --- the recovery verb dispatches before any id quoting ------------------------------------------


@pytest.mark.parametrize("ids", [["campaign_a"], ["campaign_a", "campaign/b"]])
def test_reconcile_completion_cancels_cli_takes_repeated_campaign_ids(
    ids, tmp_path, monkeypatch, capsys
):
    from tessera_control import owner_identity
    from tessera_control.cli import parser, run_campaign

    monkeypatch.delenv(owner_identity.LABEL_ENV, raising=False)
    monkeypatch.delenv(owner_identity.DELEGATION_ENV, raising=False)
    header = tmp_path / "owner-header"
    header.write_text("Authorization: Bearer test-owner\n")
    header.chmod(0o600)
    argv = ["campaign", "reconcile-completion-cancels", "--owner-header", str(header)]
    for campaign_id in ids:
        argv += ["--campaign-id", campaign_id]
    arguments = parser().parse_args(argv + ["--dry-run"])
    assert isinstance(arguments.campaign_id, list)
    calls = []

    def factory(origin, authorization):
        def transport(method, path, body, key):
            calls.append((method, path))
            return 200, {"ok": True}

        return transport

    assert run_campaign(arguments, transport_factory=factory) == 0
    assert calls == [
        ("GET", "/campaigns/campaign_a/completion-cancels"),
        *([("GET", "/campaigns/campaign%2Fb/completion-cancels")] if len(ids) > 1 else []),
    ]
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [result["campaign_id"] for result in printed["results"]] == ids


# --- control unreadiness: a refused beat past the silence bound is held -------------------------


@pytest.mark.parametrize("unready", ["state_unavailable", OSError("locked")])
def test_a_refused_beat_while_control_is_unready_is_held_past_the_silence_bound(unready):
    from test_salad_live_check_control_refusal import ProbedSession, tagged

    from tessera_control.store import Forbidden

    refusals = [None] + [tagged(Forbidden("Unauthorized")) for _ in range(10)]
    session = ProbedSession(readiness=(unready,), retain_after=12, beat_errors=refusals)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["control_refusals_skipped"] == 10
    assert receipt["control_refusal"]["control_readiness"] == "state_unavailable"
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)
