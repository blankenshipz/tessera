"""Heartbeat independence, soft-lease protection and service-initiated stop causes.

* The guardian beat goes out before any control write and its authority re-check is a read,
  so a control write lock of 31 s (or an exclusive lock of 45 s) leaves a running frame armed.
  Those two tests use a real SQLite lock on the control database and the real safety service
  with the real clock.
* The keepalive's control connections give up on a lock within their own bound.
* The trusted server never cancels the guardian on a refusal for a launched route; a soft
  lease that lapsed only because renewals stalled is neither a refusal nor unrenewable, while a
  genuine revocation or a passed hard expiry still refuses.
* Every stop the safety service starts records its first closed cause.

Temporary databases, synthetic HTTP and fake provider transports only; no network, no provider.
"""

import sqlite3
import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from test_remote_execution_routing import command, dispatch  # noqa: E402
from test_salad_live_check import concrete_session  # noqa: E402
from test_salad_live_check_running_cancel import concrete  # noqa: E402
from test_salad_safety_service import Provider, make_service, reservation  # noqa: E402
from test_salad_server import fixture, preflight, wire  # noqa: E402
from test_salad_workload_jwt import GROUP  # noqa: E402

from tessera_control.execution import ExecutionStore  # noqa: E402
from tessera_control.salad_guardian import SERVICE_STOP_CAUSES  # noqa: E402
from tessera_control.salad_live_check import (  # noqa: E402
    GUARDIAN_SILENCE_SECONDS,
    KEEPALIVE_LOCK_WAIT_SECONDS,
    ControlStall,
    _running_cancel,
)
from tessera_control.salad_safety_service import RATE_STALE_WINDOW_SECONDS  # noqa: E402
from tessera_control.salad_server import AuthorityStalled  # noqa: E402
from tessera_control.store import (  # noqa: E402
    CONTROL_BUSY_TIMEOUT_SECONDS,
    Conflict,
    Forbidden,
    _connect,
    bounded_lock_wait,
    canonical_digest,
)
from tessera_control.workers import WorkerRegistryStore  # noqa: E402


@pytest.fixture
def unbounded_wall(monkeypatch):
    """The SIGALRM wall bound needs a free real-time timer; the test runner may hold it. The
    lock wait (not a timer) is what bounds a SQLite wait, so it stays in force."""
    monkeypatch.setattr("tessera_control.salad_live_check._wall_bound", lambda s: nullcontext())


def _launch(h):
    """Real create and start effects through the synthetic wire (the route is launched)."""
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    dispatch(h, "create")
    wire(h, *preflight(h, absent=False), (200, h.provider_group), (202, b""))
    return dispatch(h, "start")


def _lease(state_dir_db, lease_id):
    db = sqlite3.connect(state_dir_db)
    try:
        return db.execute(
            "SELECT expires_at, hard_expires_at FROM execution_leases WHERE id=?", (lease_id,)
        ).fetchone()
    finally:
        db.close()


def _set_lease(database, lease_id, **columns):
    db = sqlite3.connect(database, isolation_level=None)
    try:
        for name, value in columns.items():
            db.execute(f"UPDATE execution_leases SET {name}=? WHERE id=?", (value, lease_id))
    finally:
        db.close()


def _iso(delta_seconds):
    return (datetime.now(UTC) + timedelta(seconds=delta_seconds)).isoformat().replace("+00:00", "Z")


# --- the lock wait is the bound -------------------------------------------------------------


def test_bounded_lock_wait_only_lowers_the_control_wait(tmp_path):
    path = tmp_path / "control.sqlite3"
    with bounded_lock_wait(2):
        with bounded_lock_wait(10):  # nesting never raises it again
            connection = _connect(path)
    (busy_ms,) = connection.execute("PRAGMA busy_timeout").fetchone()
    (journal,) = connection.execute("PRAGMA journal_mode").fetchone()
    connection.close()
    assert busy_ms == 2000 and journal.lower() == "delete"
    connection = _connect(path)
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == (
        CONTROL_BUSY_TIMEOUT_SECONDS * 1000
    )
    connection.close()
    for bad in (0, -1, CONTROL_BUSY_TIMEOUT_SECONDS + 1):
        with pytest.raises(ValueError), bounded_lock_wait(bad):
            pass


def test_live_check_control_call_gives_up_within_its_bound_under_write_lock(tmp_path, monkeypatch):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    try:
        holder = sqlite3.connect(session.server.store.database_path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE control_recovery SET state=state")
        started = time.monotonic()
        with (
            bounded_lock_wait(KEEPALIVE_LOCK_WAIT_SECONDS),
            pytest.raises(sqlite3.OperationalError),
        ):
            WorkerRegistryStore(session.server.store.database_path.parent).heartbeat(
                session.server.worker, "lock-wait-probe", canonical_digest({}), {}
            )
        assert time.monotonic() - started < KEEPALIVE_LOCK_WAIT_SECONDS + 3
        holder.execute("ROLLBACK")
        holder.close()
    finally:
        session.close()
        h.server.resolver.close()


# --- a real lock, the real safety service and its real clock --------------------------------


def _beating_world(tmp_path, monkeypatch):
    """A launched control route whose guardian beat reaches a real, bound safety-service frame."""
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    _launch(h)
    route = h.route["id"]
    root = tmp_path / "guardian"
    root.mkdir(mode=0o700)
    provider = Provider()
    service = make_service(root, time.time, ec.generate_private_key(ec.SECP256R1()), provider)
    body = reservation(seconds=300, now=time.time())
    service.loop()
    service.admin(body)
    service.admin(
        dict(
            operation="bind",
            reservation=body["reservation"],
            provider_id=GROUP,
            allocation_id=None,
        )
    )
    provider.state = "running"
    service.loop()
    session._registry_heartbeat()

    def beat(state):
        # The real service loop runs continuously in its own process; one pass here keeps it
        # as fresh as that loop is (the admin path refuses a stale loop).
        service.loop()
        service.admin(dict(operation="heartbeat", reservation=body["reservation"]))

    monkeypatch.setattr(session.server.safety, "heartbeat", beat)
    return session, h, route, service, body


def _hold_lock(database, mode, seconds, locked):
    holder = sqlite3.connect(database, isolation_level=None)
    try:
        holder.execute(f"BEGIN {mode}")
        holder.execute("UPDATE control_recovery SET state=state")
        locked.set()
        time.sleep(seconds)
        holder.execute("ROLLBACK")
    finally:
        holder.close()


def _keep_beating_through_lock(tmp_path, monkeypatch, mode, seconds):
    session, h, route, service, body = _beating_world(tmp_path, monkeypatch)
    try:
        lease_id = session.state(route)["proposal"]["lease_id"]
        session.keepalive(route)  # verified authority before the lock
        service.loop()
        # Fresh worker-registry liveness just before the lock: under machine load the setup and
        # service passes above can eat into the registry's 60 s liveness bound.
        session._registry_heartbeat()
        locked = threading.Event()
        holder = threading.Thread(
            target=_hold_lock,
            args=(session.server.store.database_path, mode, seconds, locked),
        )
        holder.start()
        assert locked.wait(10)
        outcomes, last = [], time.monotonic()
        released = None
        while released is None or time.monotonic() - released < 7:
            if released is None and not holder.is_alive():
                released = time.monotonic()
            if time.monotonic() - last >= 5:
                last = time.monotonic()
                try:
                    session.keepalive(route)
                    outcomes.append("ok")
                except ControlStall as stall:
                    outcomes.append(("stall", stall.beat_delivered))
            service.loop()  # the real service clock: its 30 s silence bound applies
            time.sleep(0.5)
        holder.join()
        status = service.admin(dict(operation="status", reservation=body["reservation"]))
        frame = service._row(body["reservation"])
        beats = session.beat_record(route)
        lease = _lease(session.server.store.database_path, lease_id)
        return outcomes, status, frame, beats, lease
    finally:
        service.close()
        session.close()
        h.server.resolver.close()


@pytest.mark.usefixtures("unbounded_wall")
def test_keepalive_delivers_guardian_beat_within_silence_bound_while_control_write_lock_is_held_31s(  # noqa: E501
    tmp_path, monkeypatch
):
    outcomes, status, frame, beats, lease = _keep_beating_through_lock(
        tmp_path, monkeypatch, "IMMEDIATE", 31
    )
    # The lock really blocked the control writes, and every beat still went out.
    assert ("stall", True) in outcomes, outcomes
    assert ("stall", False) not in outcomes, outcomes
    assert outcomes[-1] == "ok"  # writes resumed once the lock was released
    assert status["status"] == "armed" and status["stop_cause"] is None
    assert frame["recovery"] == 0
    assert beats["max_gap_seconds"] < GUARDIAN_SILENCE_SECONDS
    # The lease stays current: renewed after the lock, inside its hard expiry.
    assert datetime.fromisoformat(lease[0].replace("Z", "+00:00")) > datetime.now(UTC)


@pytest.mark.usefixtures("unbounded_wall")
def test_a_45s_exclusive_control_lock_leaves_the_safety_frame_armed(tmp_path, monkeypatch):
    # Exclusive: even the authority read is blocked, so the beat goes out on the last verified
    # authority while the soft lease that verification saw is unexpired.
    outcomes, status, frame, beats, lease = _keep_beating_through_lock(
        tmp_path, monkeypatch, "EXCLUSIVE", 45
    )
    assert ("stall", True) in outcomes and ("stall", False) not in outcomes, outcomes
    assert outcomes[-1] == "ok"
    assert status["status"] == "armed" and status["stop_cause"] is None
    assert frame["recovery"] == 0
    assert beats["on_last_verified_authority"] >= 1
    assert beats["max_gap_seconds"] < GUARDIAN_SILENCE_SECONDS
    assert datetime.fromisoformat(lease[0].replace("Z", "+00:00")) > datetime.now(UTC)


# --- the trusted server's heartbeat ---------------------------------------------------


def _seen(h):
    """Fresh worker registry liveness (a slow fixture setup may pass the 60 s bound)."""
    WorkerRegistryStore(h.store.database_path.parent).heartbeat(
        h.worker, "seen-" + str(time.monotonic_ns()), canonical_digest({}), {}
    )


def _server(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    state = _launch(h)
    _seen(h)
    return h, state["id"], h.body["lease_id"], h.store.database_path


def test_heartbeat_authority_is_a_read_under_another_writers_lock(tmp_path, monkeypatch):
    h, route, _, database = _server(tmp_path, monkeypatch)
    holder = sqlite3.connect(database, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE control_recovery SET state=state")
    try:
        before = len(h.safety_calls)
        with bounded_lock_wait(KEEPALIVE_LOCK_WAIT_SECONDS):
            h.server.heartbeat(route)
        assert [b["operation"] for b in h.safety_calls[before:]] == ["heartbeat"]
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_stalled_authority_read_beats_only_on_an_unexpired_verification(tmp_path, monkeypatch):
    h, route, _, _ = _server(tmp_path, monkeypatch)
    h.server.heartbeat(route)  # verified
    real_get = h.store.get

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(h.store, "get", locked)
    before = len(h.safety_calls)
    with pytest.raises(AuthorityStalled) as stalled:
        h.server.heartbeat(route)
    assert stalled.value.beat_delivered is True
    assert [b["operation"] for b in h.safety_calls[before:]] == ["heartbeat"]
    # Past the soft lease that verification saw: no beat on it any more.
    h.server._verified_authority[route]["lease_expires_at"] = time.time() - 1
    before = len(h.safety_calls)
    with pytest.raises(AuthorityStalled) as stalled:
        h.server.heartbeat(route)
    assert stalled.value.beat_delivered is False
    assert h.safety_calls[before:] == []
    h.server._verified_authority[route] = dict(
        state=real_get(h.worker, route), lease_expires_at=time.time() + 60
    )
    # A disk or schema error is not a lock stall: it is raised as it is, with no beat, and the
    # earlier verification is dropped.
    monkeypatch.setattr(
        h.store, "get", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("no such"))
    )
    before = len(h.safety_calls)
    with pytest.raises(sqlite3.OperationalError):
        h.server.heartbeat(route)
    assert h.safety_calls[before:] == [] and route not in h.server._verified_authority
    monkeypatch.setattr(h.store, "get", real_get)


def test_server_heartbeat_does_not_cancel_guardian_on_lapsed_soft_lease_for_running_route(
    tmp_path, monkeypatch
):
    h, route, lease_id, database = _server(tmp_path, monkeypatch)
    _set_lease(database, lease_id, expires_at=_iso(-5))  # renewals stalled past 150 s
    before = len(h.safety_calls)
    h.server.heartbeat(route)
    assert [b["operation"] for b in h.safety_calls[before:]] == ["heartbeat"]
    # The route's own worker renews the lapsed lease through the trusted store...
    epoch = h.body["epoch"]
    digest = canonical_digest({"lease_id": lease_id, "expected_epoch": epoch})
    h.store.renew_execution_lease(h.worker, lease_id, "renew-after-lapse", digest, epoch)
    assert datetime.fromisoformat(
        _lease(database, lease_id)[0].replace("Z", "+00:00")
    ) > datetime.now(UTC)
    # ...while the ordinary (HTTP) renewal path still refuses a lapsed lease.
    _set_lease(database, lease_id, expires_at=_iso(-5))
    with pytest.raises(Conflict, match="no longer current"):
        ExecutionStore(database.parent).renew_execution_lease(
            h.worker, lease_id, "plain-renew", digest, epoch
        )


def test_slow_registry_write_does_not_skip_lease_renewal():
    session = concrete()

    def slow():
        raise TimeoutError("bounded live operation expired")

    session._registry_heartbeat = slow
    with pytest.raises(ControlStall) as raised:
        session.keepalive("route")
    assert raised.value.beat_delivered is True
    assert session.server.store.calls == ["guardian_heartbeat", "renew"]


def test_a_passed_hard_expiry_still_refuses_the_beat_and_the_renewal(tmp_path, monkeypatch):
    h, route, lease_id, database = _server(tmp_path, monkeypatch)
    _set_lease(database, lease_id, expires_at=_iso(-5), hard_expires_at=_iso(-1))
    before = len(h.safety_calls)
    with pytest.raises(Conflict):
        h.server.heartbeat(route)
    assert h.safety_calls[before:] == []
    epoch = h.body["epoch"]
    digest = canonical_digest({"lease_id": lease_id, "expected_epoch": epoch})
    with pytest.raises(Conflict):
        h.store.renew_execution_lease(h.worker, lease_id, "renew-past-hard", digest, epoch)


def test_genuine_revocation_on_a_running_route_blocks_the_beat_and_the_driver_cancels(
    tmp_path, monkeypatch
):
    h, route, _, _ = _server(tmp_path, monkeypatch)
    h.server.heartbeat(route)
    command(h, "cancel")  # control authority revoked
    before = len(h.safety_calls)
    with pytest.raises((Conflict, Forbidden)):
        h.server.heartbeat(route)
    # No beat on the revoked authority, and no server-side cancel before the driver classifies.
    assert h.safety_calls[before:] == []
    # A later lock stall never beats on the verification taken before the revocation.
    assert route not in h.server._verified_authority


def test_the_driver_still_cancels_a_running_route_on_a_conflict_from_the_beat():
    from test_salad_live_check_running_cancel import RunningSession, cancelled_before_output
    from test_salad_live_check_running_cancel import drive as run_driver

    error = Conflict("Remote execution lease/epoch is fenced.")
    error.live_check_call = "TrustedSaladServer.heartbeat"
    session = RunningSession(beat_errors=[None, None, error])
    receipt = run_driver(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "control_refused"


def test_beat_record_counts_beats_and_their_gaps(monkeypatch):
    session = concrete()
    ticks = iter([100.0, 112.5, 118.0])
    monkeypatch.setattr("tessera_control.salad_live_check.time.monotonic", lambda: next(ticks))
    for _ in range(3):
        session.keepalive("route")
    assert session.beat_record("route") == {
        "delivered": 3,
        "on_last_verified_authority": 0,
        "max_gap_seconds": 12.5,
        "last_gap_seconds": 5.5,
    }
    assert session.beat_record("other") is None


# --- service-initiated stops record their first cause --------------------------------------


@pytest.fixture
def frame(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    service = make_service(tmp_path, lambda: clock[0], key, provider)
    body = reservation(seconds=300)
    service.loop()
    service.admin(body)
    service.admin(
        dict(
            operation="bind",
            reservation=body["reservation"],
            provider_id=GROUP,
            allocation_id=None,
        )
    )
    provider.state = "running"
    service.loop()
    world = dict(service=service, body=body, clock=clock, provider=provider, key=key, root=tmp_path)
    yield world
    world["service"].close()


def _status(world):
    return world["service"].admin(
        dict(operation="status", reservation=world["body"]["reservation"])
    )


def _beat(world):
    world["service"].admin(dict(operation="heartbeat", reservation=world["body"]["reservation"]))


def _service_cause(world):
    return world["service"]._row(world["body"]["reservation"])["stop_cause"]


def test_service_level_heartbeat_expiry_records_stop_cause(frame):
    frame["clock"][0] += 31  # no heartbeat for longer than the service's 30 s bound
    frame["service"].loop()
    assert _status(frame)["stop_cause"] == "heartbeat_silence"
    assert _service_cause(frame) == "heartbeat_silence"


def test_service_level_deadline_records_stop_cause(frame):
    frame["clock"][0] = frame["body"]["deadline"] + 1
    frame["service"].loop()
    assert _status(frame)["stop_cause"] == "deadline"


def test_service_level_rate_outage_records_stop_cause(frame):
    _beat(frame)
    frame["service"].retry_sleep = lambda seconds: None
    frame["provider"].rate_failures = 10_000
    last_good = frame["clock"][0]
    # A running frame keeps its last rate through a transient outage inside the stale window
    # (ADR 0204); heartbeats keep arriving, so only the rate outage can stop it.
    while frame["clock"][0] + 5 - last_good < RATE_STALE_WINDOW_SECONDS:
        frame["clock"][0] += 5
        frame["service"].loop()
        _beat(frame)
        assert _status(frame)["stop_cause"] is None
    frame["clock"][0] += 5
    frame["service"].loop()
    status = _status(frame)
    assert status["rate_unknown"] and status["stop_cause"] == "rate_unknown"
    assert _service_cause(frame) == "rate_unknown"


def test_service_level_cost_cap_records_stop_cause(frame):
    _beat(frame)
    frame["provider"].rate = "1000"  # worst-case exposure now passes the approved cap
    frame["clock"][0] += 5
    frame["service"].loop()
    assert _status(frame)["stop_cause"] == "cost_cap"


def test_service_level_total_cap_records_stop_cause(frame):
    _beat(frame)
    frame["service"].total_cap_cents = 1  # an overshoot of the aggregate cap
    frame["clock"][0] += 5
    frame["service"].loop()
    assert _status(frame)["stop_cause"] == "total_cap"


def test_service_level_loop_frame_failure_records_stop_cause(frame, monkeypatch):
    _beat(frame)
    service = frame["service"]

    def broken(row, **kwargs):
        raise RuntimeError("synthetic frame failure")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_account", broken)
        frame["clock"][0] += 5
        service.loop()
    assert _status(frame)["stop_cause"] == "loop_frame_failed"


def test_service_restart_records_guardian_restart(frame):
    frame["service"].close()
    frame["service"] = make_service(
        frame["root"], lambda: frame["clock"][0], frame["key"], frame["provider"]
    )
    frame["service"].loop()
    assert _status(frame)["stop_cause"] == "guardian_restart"


def test_control_cancel_records_stop_cause_and_the_first_cause_wins(frame):
    frame["service"].admin(dict(operation="cancel", reservation=frame["body"]["reservation"]))
    assert _status(frame)["stop_cause"] == "control_cancel"
    frame["clock"][0] += 31
    frame["service"].loop()
    assert _status(frame)["stop_cause"] == "control_cancel"
    assert _service_cause(frame) == "control_cancel"


def test_heartbeat_silence_then_cancel_keeps_heartbeat_silence(frame):
    frame["clock"][0] += 31
    frame["service"].loop()
    frame["service"].admin(dict(operation="cancel", reservation=frame["body"]["reservation"]))
    assert _status(frame)["stop_cause"] == "heartbeat_silence"


def test_a_cancel_after_a_retained_output_records_no_cause(frame):
    guardian = frame["service"].guardian
    operation = frame["service"]._row(frame["body"]["reservation"])["operation"]
    guardian.complete(operation)
    guardian.cancel(operation)
    assert guardian.status(operation)["stop_cause"] is None


def test_every_recorded_service_cause_is_closed():
    assert {
        "heartbeat_silence",
        "deadline",
        "rate_unknown",
        "total_cap",
        "loop_frame_failed",
        "guardian_restart",
        "control_cancel",
    } <= SERVICE_STOP_CAUSES


def test_the_drivers_own_cancel_recorded_as_control_cancel_is_still_a_running_cancel():
    receipt = {
        "cancel_cause": {"class": "observe_timeout", "last_provider_status": "running"},
        "guardian_stop_cause": "control_cancel",
    }
    assert _running_cancel(receipt)
    receipt["guardian_stop_cause"] = "heartbeat_silence"
    assert not _running_cancel(receipt)
