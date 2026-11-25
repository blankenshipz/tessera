"""A stalled lease renewal does not kill a healthy stage; a lease that really ends still does.

The development worker renews its lease every 10 s (30 s here; 150 s by default since ADR 0189).
One renewal that gets no answer in time (control busy, a slow host) is retried with backoff
until the lease's own expiry less a safety margin; only a refusal (a 409 fence) or that deadline
stops the stage, and the grant
result records the failed attempts and names the cause (ADR 0178).

These tests model the incident: the channel reports the HTTP channel's 15 s timeout and a
request that gets no answer uses up the whole limit it was given, and the worker and control
share one simulated timeline, so control's lease expiries move with the worker's time.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_development_worker_service import (
    ApiChannel,
    _answer_like_the_http_channel,
    _service,
    _unleased_delivery,
)
from test_supervisor import FakeAdapter

from tessera_control.execution import ExecutionStore
from tessera_control.store import _connect
from tessera_worker.control_channel import (
    DEFAULT_TIMEOUT_SECONDS,
    ControlChannelError,
    classify_control_failure,
)
from tessera_worker.runtime import LEASE_SAFETY_MARGIN_SECONDS, CpuRuntime
from tessera_worker.service import DevelopmentWorkerConfig
from tessera_worker.supervisor import Lease

RENEW = "/worker/leases/renew"
HEARTBEAT = "/worker/heartbeat"
ROUND_TRIP = 0.05  # one answered control request


@pytest.fixture(autouse=True)
def _thirty_second_lease(monkeypatch):
    """These tests pin ADR 0178's deadline arithmetic on the 30 s lease it was written for.

    Control's lease is 150 s since ADR 0189 (tests/test_lease_stall_margin.py), but a lease is
    still as short as its hard expiry makes it, and the retry, per-request share and
    cancel-before-expiry rules are the same at any length; 30 s keeps the timings here exact.
    """
    from tessera_control import execution

    monkeypatch.setattr(execution, "LEASE_SECONDS", 30)


class Timeline:
    """One simulated clock for the worker (wall and monotonic) and for control."""

    def __init__(self):
        # The worker stamps its observations with real time, which control checks against the
        # lease's issue time and its own clock (5 s skew). Start just behind real time, right
        # before the run: simulated time then runs ahead, and those checks hold either way.
        self.base = datetime.now(UTC) - timedelta(seconds=1)
        self.seconds = 0.0

    def reset(self):
        self.__init__()

    def wall(self):
        return self.base + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


HANG = "hang"  # no answer: the request uses up the whole limit it was given, then times out


class TimedChannel(ApiChannel):
    """Control over HTTP on the shared timeline, with the HTTP channel's 15 s timeout.

    ``fail(channel, path)`` returns None (answer), ``HANG``, ``("slow", fraction)`` (answer
    after that fraction of the limit) or an exception to raise after one round trip.
    """

    timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    def __init__(self, state, timeline, fail):
        super().__init__(state)
        self.timeline = timeline
        self.fail = fail
        self.calls: list[tuple[str, float, float]] = []  # (path, limit, time it started)
        self.renew_attempts = 0
        self.package_posts = 0

    def post(self, path, body, key, *, timeout_seconds=None):
        limit = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        assert 0 < limit <= self.timeout_seconds  # the HTTP channel refuses anything else
        self.calls.append((path, limit, self.timeline.seconds))
        if path == RENEW:
            self.renew_attempts += 1
        if path.endswith("/package"):
            self.package_posts += 1
        failure = self.fail(self, path)
        if failure == HANG:
            self.timeline.advance(limit)
            raise ControlChannelError("control response deadline exceeded")
        if isinstance(failure, tuple):
            self.timeline.advance(limit * failure[1])
        else:
            self.timeline.advance(ROUND_TRIP)
        if isinstance(failure, BaseException):
            raise failure
        return super().post(path, body, key)

    def limits(self, path):
        return [limit for called, limit, _ in self.calls if called == path]


class CancelRecordingAdapter(FakeAdapter):
    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline
        self.cancelled_at: list[datetime] = []

    def cancel(self, operation):
        self.cancelled_at.append(self.timeline.wall())
        return super().cancel(operation)


def _timed(tmp_path, monkeypatch, fail, *, max_ticks, poll_seconds=1.0, defaults=True, **config):
    """A service whose worker and control run on one simulated timeline."""
    timeline = Timeline()
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(timeline.wall))
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    channel = TimedChannel(state, timeline, fail)
    adapter = CancelRecordingAdapter(timeline)
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=max_ticks,
        sleep=timeline.advance,
    )
    if defaults:
        # The shipped cadence: renew every 10 s (or 15 ticks), re-read authority every 10 s.
        config.setdefault("renew_every_ticks", DevelopmentWorkerConfig.renew_every_ticks)
    service.config = dataclasses.replace(service.config, poll_seconds=poll_seconds, **config)
    service.clock = timeline.monotonic
    service.wall_clock = timeline.wall
    timeline.reset()  # the run starts now
    configure = service._configure_runtime

    def on_the_timeline(runtime):
        configure(runtime)
        runtime._clock = timeline.wall
        runtime._monotonic = timeline.monotonic
        runtime.supervisor._now = timeline.wall

    service._configure_runtime = on_the_timeline
    return service, channel, adapter, state


def _control_lease_expiry(state):
    with _connect(state / "tessera.sqlite3") as connection:
        (expires_at,) = connection.execute("SELECT expires_at FROM execution_leases").fetchone()
    return datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(UTC)


def _nth_renewal(n, failure):
    def fail(channel, path):
        return failure if path == RENEW and channel.renew_attempts == n else None

    return fail


# -- decision 1: one hung request on the shipped defaults ------------------------------------


def test_one_hung_renewal_is_survived_on_the_default_settings(tmp_path, monkeypatch):
    """The incident: the second renewal gets no answer within its limit; the stage survives."""
    service, channel, adapter, _ = _timed(
        tmp_path, monkeypatch, _nth_renewal(2, HANG), max_ticks=35
    )

    result = service.run()

    assert result["reason"] == "tick_limit_cancellation_requested"
    assert adapter.calls.count("cancel") == 1  # only the tick-limit cancellation
    assert result["lease_renewal_retries"] == {
        "failed_attempts": 1,
        "recovered_stalls": 1,
        "unconfirmed_attempts": 0,
        "last_failure": (
            "ControlChannelError: Control channel failed: control response deadline exceeded"
        ),
    }
    limits = channel.limits(RENEW)
    # About 15 s were left before the deadline at the second renewal: the hung attempt got a
    # third of it, not the channel's whole 15 s, so the retry still had time.
    assert limits[1] < DEFAULT_TIMEOUT_SECONDS / 2
    assert len(limits) >= 4 and all(limit <= DEFAULT_TIMEOUT_SECONDS for limit in limits)


def test_a_hung_request_leaves_room_for_more_attempts_before_the_deadline(tmp_path, monkeypatch):
    """Every renewal attempt hangs: several attempts fit before the deadline, not one."""

    def fail(channel, path):
        return HANG if path == RENEW and channel.renew_attempts >= 2 else None

    service, channel, adapter, state = _timed(tmp_path, monkeypatch, fail, max_ticks=200)

    result = service.run()

    assert result["reason"] == "heartbeat_or_renewal_failure"
    assert "lease expired before a renewal was confirmed" in result["renewal_failure"]
    assert result["lease_renewal_retries"]["failed_attempts"] >= 3
    # The cancel landed before control's own lease expired (ADR 0178 rule 1).
    (cancelled_at,) = adapter.cancelled_at
    assert cancelled_at < _control_lease_expiry(state)


def test_a_slow_heartbeat_shortens_the_renewal_that_follows_it(tmp_path, monkeypatch):
    """The limit is worked out again before each call, not once for the heartbeat and renewal."""

    def fail(channel, path):
        heartbeats = len(channel.limits(HEARTBEAT))
        return ("slow", 0.9) if path == HEARTBEAT and heartbeats == 3 else None

    service, channel, _, _ = _timed(tmp_path, monkeypatch, fail, max_ticks=25)

    service.run()

    beats = [i for i, (path, _, _) in enumerate(channel.calls) if path == HEARTBEAT]
    third = beats[2]  # the start's heartbeat, then the first two renewals' heartbeats
    (_, heartbeat_limit, started), (path, renewal_limit, renewal_started) = channel.calls[
        third : third + 2
    ]
    assert path == RENEW
    assert renewal_started == pytest.approx(started + 0.9 * heartbeat_limit)
    # The heartbeat got a third of the time left; the renewal a third of what was left after
    # the slow heartbeat (2.1 heartbeat limits), not the heartbeat's own limit again.
    assert heartbeat_limit < DEFAULT_TIMEOUT_SECONDS
    assert renewal_limit == pytest.approx(0.7 * heartbeat_limit, abs=0.05)


# -- decision 2: every control call is bounded by the lease; the cancel lands in time --------


def test_a_lasting_outage_cancels_before_controls_lease_expires(tmp_path, monkeypatch):
    """After the second renewal every control call hangs (reports and authority reads too)."""
    outage = {"from": None}

    def fail(channel, path):
        if outage["from"] is None and path == RENEW and channel.renew_attempts == 2:
            outage["from"] = channel.timeline.seconds
            return None  # this one is answered; everything after it hangs
        return HANG if outage["from"] is not None else None

    service, channel, adapter, state = _timed(tmp_path, monkeypatch, fail, max_ticks=200)

    result = service.run()

    # Stopped by the renewal deadline, or by the tick's authority check near it (the lease is
    # denied, and the cancel's exit report then waits in the outbox).
    assert result["reason"] in {"heartbeat_or_renewal_failure", "observation_delivery_pending"}
    cancelled_at = adapter.cancelled_at[0]  # the first cancel stops the stage
    expiry = _control_lease_expiry(state)
    assert cancelled_at < expiry  # the stage stopped while control's lease was still live
    # No call made during the outage, before the cancel, could outlast the lease deadline by
    # more than the minimum request time.
    deadline = (expiry - channel.timeline.base).total_seconds() - LEASE_SAFETY_MARGIN_SECONDS
    cancel_time = (cancelled_at - channel.timeline.base).total_seconds()
    for _path, limit, started in channel.calls:
        if outage["from"] < started < cancel_time:
            assert started + limit <= max(deadline, started) + 1.0


# -- decision 3: a cached read never moves the expiry earlier ---------------------------------


def test_a_reused_read_never_moves_the_lease_expiry_earlier():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    held = Lease("lease-1", "op", "d" * 64, 3, now + timedelta(seconds=30), 1, "p")
    stale = {
        "package_digest": "pkg",
        "package": {
            "operation_id": "op",
            "plan_digest": "d" * 64,
            "task_revision": 1,
            "project_id": "p",
        },
        "lease": {"id": "lease-1", "epoch": 3, "expires_at": now.isoformat()},
    }
    runtime = SimpleNamespace(
        lease=held,
        descriptor={"control": {"package_digest": "pkg"}},
        _current_package=lambda lease_id, epoch, max_age: stale,
        _lease=CpuRuntime._lease,
    )
    renewed = {"id": "lease-1", "epoch": 3, "expires_at": (now + timedelta(seconds=40)).isoformat()}
    assert CpuRuntime.apply_renewal(runtime, renewed) is True
    CpuRuntime._refresh_lease(runtime, max_age=10.0)
    assert runtime.lease.expires_at == now + timedelta(seconds=40)
    # A later expiry from a fresh read is still taken.
    stale["lease"]["expires_at"] = (now + timedelta(seconds=55)).isoformat()
    CpuRuntime._refresh_lease(runtime)
    assert runtime.lease.expires_at == now + timedelta(seconds=55)


# -- decision 5: a disconnect while the lease is live -----------------------------------------


def test_a_disconnect_while_the_lease_is_live_is_ridden_out(tmp_path, monkeypatch):
    """Control is unreachable (connection refused, at once) for 8 s, then answers again."""
    window = {"from": None}

    def fail(channel, path):
        if window["from"] is None and path == RENEW and channel.renew_attempts == 2:
            window["from"] = channel.timeline.seconds
        start = window["from"]
        channel.disconnected = start is not None and start <= channel.timeline.seconds < start + 8
        return None

    service, channel, adapter, _ = _timed(tmp_path, monkeypatch, fail, max_ticks=35)

    result = service.run()

    assert result["reason"] == "tick_limit_cancellation_requested"
    assert adapter.calls.count("cancel") == 1
    retries = result["lease_renewal_retries"]
    assert retries["failed_attempts"] >= 2 and retries["recovered_stalls"] == 1
    assert retries["unconfirmed_attempts"] == 0
    assert "synthetic control disconnect" in retries["last_failure"]


def test_a_disconnect_that_outlasts_the_lease_cancels_before_control_expires(tmp_path, monkeypatch):
    def fail(channel, path):
        if path == RENEW and channel.renew_attempts == 2:
            channel.disconnected = True
        return None

    service, _, adapter, state = _timed(tmp_path, monkeypatch, fail, max_ticks=200)

    result = service.run()

    # Stopped by the renewal deadline, or by the tick's authority check near it (the lease is
    # denied, and the cancel's exit report then waits in the outbox).
    assert result["reason"] in {"heartbeat_or_renewal_failure", "observation_delivery_pending"}
    cancelled_at = adapter.cancelled_at[0]  # the first cancel stops the stage
    assert cancelled_at < _control_lease_expiry(state)


def test_the_api_channel_maps_control_errors_like_the_http_channel():
    def answer(status, body):
        return SimpleNamespace(status_code=status, json=lambda: body)

    def kind(status, body=None):
        with pytest.raises(ControlChannelError) as raised:
            _answer_like_the_http_channel(answer(status, body or {"detail": "x"}))
        assert raised.value.status == status
        return classify_control_failure(raised.value)

    assert kind(401) == "auth"
    assert kind(403) == "auth"
    assert kind(403, {"detail": "Worker is retired."}) == "permanent"
    assert kind(409) == "permanent"
    assert kind(404) == "permanent"
    assert kind(503) == "transient"
    assert _answer_like_the_http_channel(answer(200, {"ok": 1})) == {"ok": 1}


def test_a_rejected_credential_on_renewal_stops_at_once(tmp_path, monkeypatch):
    service, channel, adapter, _ = _timed(
        tmp_path,
        monkeypatch,
        _nth_renewal(2, ControlChannelError("control rejected command", 401)),
        max_ticks=60,
    )

    result = service.run()

    assert result["reason"] == "heartbeat_or_renewal_failure"
    assert result["renewal_failure"] == "control refused the lease renewal (HTTP 401)"
    assert channel.renew_attempts == 2
    assert adapter.calls.count("cancel") == 1


# -- refusals, cadence, and the authority grace ------------------------------------------------


def test_a_refusal_after_a_stall_stops_the_stage_and_names_the_cause(tmp_path, monkeypatch):
    def fail(channel, path):
        if path != RENEW:
            return None
        if channel.renew_attempts == 2:
            return HANG
        if channel.renew_attempts == 3:
            return ControlChannelError("control rejected command", 409)
        return None

    service, channel, adapter, _ = _timed(tmp_path, monkeypatch, fail, max_ticks=60)

    result = service.run()

    assert result["reason"] == "heartbeat_or_renewal_failure"
    assert adapter.calls.count("cancel") == 1
    assert channel.renew_attempts == 3
    assert result["renewal_failure"].startswith(
        "control refused the lease renewal (HTTP 409) after 1 unanswered renewal attempt(s)"
    )
    assert result["lease_renewal_retries"]["failed_attempts"] == 2


def test_a_refusal_without_a_stall_still_stops_at_once(tmp_path, monkeypatch):
    service, channel, adapter, _ = _timed(
        tmp_path,
        monkeypatch,
        _nth_renewal(2, ControlChannelError("control rejected command", 409)),
        max_ticks=60,
    )

    result = service.run()

    assert result["reason"] == "heartbeat_or_renewal_failure"
    assert result["renewal_failure"] == "control refused the lease renewal (HTTP 409)"
    assert channel.renew_attempts == 2
    assert adapter.calls.count("cancel") == 1


def test_a_local_failure_is_not_retried_as_transient(tmp_path, monkeypatch):
    """Only a control failure is retried; any other error (here an OSError) stops at once."""
    service, channel, adapter, _ = _timed(
        tmp_path, monkeypatch, _nth_renewal(2, OSError("local disk failure")), max_ticks=60
    )

    result = service.run()

    assert result["reason"] == "heartbeat_or_renewal_failure"
    assert result["renewal_failure"].startswith("the renewal could not be attempted")
    assert channel.renew_attempts == 2
    assert adapter.calls.count("cancel") == 1


def test_running_ticks_reread_authority_at_a_bounded_cadence(tmp_path, monkeypatch):
    """Ticks reuse the last authority read; each renewal forces one (not two per tick)."""
    service, channel, _, _ = _timed(tmp_path, monkeypatch, lambda channel, path: None, max_ticks=40)

    service.run()

    assert channel.renew_attempts >= 4
    # The start reads the package a few times; after that at most one read per 10 s of the
    # timeline besides the one each renewal forces.
    assert channel.package_posts <= 2 * channel.renew_attempts + 4
    assert channel.package_posts < 40


def test_per_tick_reread_stays_available(tmp_path, monkeypatch):
    service, channel, _, _ = _timed(
        tmp_path,
        monkeypatch,
        lambda channel, path: None,
        max_ticks=30,
        authority_reread_seconds=0.0,
    )

    service.run()

    assert channel.package_posts >= 2 * 30


def test_authority_reread_must_stay_inside_the_lease(tmp_path, monkeypatch):
    from tessera_worker.service import DevelopmentWorkerError

    service, _, _, _ = _timed(tmp_path, monkeypatch, lambda channel, path: None, max_ticks=1)
    for bad in (-1, 21):
        with pytest.raises((DevelopmentWorkerError, ValueError)):
            dataclasses.replace(service.config, authority_reread_seconds=bad)


def test_authority_check_tolerates_only_a_transient_failure_with_lease_time_left():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    runtime = SimpleNamespace(_lease_clock=lambda: now, transient_lease_grace=True)

    def lease(seconds_left):
        return SimpleNamespace(expires_at=now + timedelta(seconds=seconds_left))

    timeout = ControlChannelError("control response deadline exceeded")
    grace = CpuRuntime._transient_grace
    assert grace(runtime, lease(20), timeout) is True
    assert grace(runtime, lease(20), ControlChannelError("server error", 503)) is True
    assert grace(runtime, lease(20), TimeoutError("timed out")) is True
    # Near or past the lease's own expiry: deny, so the stage stops before control's lease ends.
    assert grace(runtime, lease(LEASE_SAFETY_MARGIN_SECONDS), timeout) is False
    assert grace(runtime, lease(-1), timeout) is False
    # A refusal, a rejected credential, or a malformed answer is never tolerated.
    assert grace(runtime, lease(20), ControlChannelError("fenced", 409)) is False
    assert grace(runtime, lease(20), ControlChannelError("gone", 404)) is False
    assert grace(runtime, lease(20), ControlChannelError("credential", 401)) is False
    assert grace(runtime, lease(20), KeyError("package")) is False
    # Transient is classified positively: a local OSError (or a bare ConnectionError that did
    # not come from the control channel) is not a control failure.
    assert grace(runtime, lease(20), OSError("disk")) is False
    assert grace(runtime, lease(20), ConnectionError("local")) is False
    # A bare runtime (no renewal retry around it) keeps denying on any failed read.
    runtime.transient_lease_grace = False
    assert grace(runtime, lease(20), timeout) is False
