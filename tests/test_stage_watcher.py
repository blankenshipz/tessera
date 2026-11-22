from __future__ import annotations

import errno
import json
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tessera_control.stage_watch_limits import (
    MAX_REPORTS_PER_OPERATION,
    TERMINAL_RESERVE,
    routine_limit,
)
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.stage_watcher import (
    BODY_KEYS,
    COUNTER_KEYS,
    DELIVERED,
    ROUTINE_REFUSAL_STATUSES,
    TERMINAL_REFUSAL_STATUSES,
    TRANSIENT,
    ControlSink,
    DeliveryResult,
    KillSwitch,
    ProbeSample,
    StageContract,
    StageWatcher,
    WatchOutbox,
    WatchReport,
    classify_status,
    idempotency_key,
    routine_cap,
)

OPERATION_ID = "0123456789abcdef0123456789abcdef"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_contract(**overrides) -> StageContract:
    fields = {
        "operation_id": OPERATION_ID,
        "lease_id": "lease-1",
        "epoch": 3,
        "stage_key": "train",
        "provider": "local",
        "deadline_seconds": 3600,
        "silent_seconds": 300,
        "heartbeat_seconds": 60,
        "max_reports": 100,
        "kill_switch_path": None,
    }
    fields.update(overrides)
    return StageContract(**fields)


class FakeClock:
    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeProbe:
    """Scripted samples: each entry is a dict of ProbeSample fields (minus observed_at)."""

    def __init__(self, script: list[dict] | None = None):
        self.script = list(script or [])
        self.default: dict = {"state": "running"}
        self.calls = 0

    def sample(self, now: datetime) -> ProbeSample:
        self.calls += 1
        fields = self.script.pop(0) if self.script else self.default
        if fields == "raise":
            raise RuntimeError("probe blew up")
        return ProbeSample(observed_at=now, **fields)


class FakeSink:
    def __init__(self):
        self.delivered: list[WatchReport] = []
        self.attempts: list[tuple[int, str]] = []
        self.fail = 0  # number of upcoming deliveries that fail
        self.raise_on_fail = False

    def deliver(self, report: WatchReport) -> bool:
        self.attempts.append((report.seq, report.idempotency_key))
        if self.fail:
            self.fail -= 1
            if self.raise_on_fail:
                raise ConnectionError("control down")
            return False
        self.delivered.append(report)
        return True


class Harness:
    def __init__(self, tmp_path: Path, contract: StageContract | None = None, **kwargs):
        self.contract = contract or make_contract()
        self.clock = FakeClock()
        self.probe = FakeProbe()
        self.sink = FakeSink()
        self.sleeps: list[float] = []
        self.outbox_path = tmp_path / "watch.sqlite"
        self.kill_switch = kwargs.pop("kill_switch", KillSwitch(None))
        self.watcher = self.build(**kwargs)

    def build(self, **kwargs) -> StageWatcher:
        return StageWatcher(
            self.contract,
            self.probe,
            self.sink,
            self.kill_switch,
            self.clock,
            WatchOutbox(self.outbox_path),
            sleep=self.sleeps.append,
            **kwargs,
        )

    def kinds(self, reports: list[WatchReport]) -> list[str]:
        return [report.kind for report in reports]


# -- contract ---------------------------------------------------------------------------------


def test_contract_digest_is_stable_and_canonical():
    contract = make_contract()
    again = make_contract()
    assert contract.digest() == again.digest()
    assert len(contract.digest()) == 64
    canonical = json.loads(contract.canonical_json())
    assert list(canonical) == sorted(canonical)
    assert canonical["operation_id"] == OPERATION_ID
    assert make_contract(epoch=4).digest() != contract.digest()
    assert make_contract(kill_switch_path="/run/tessera/stop").digest() != contract.digest()


def test_contract_verify():
    contract = make_contract()
    assert contract.verify(contract.digest())
    assert not contract.verify(make_contract(max_reports=5).digest())
    assert not contract.verify("not-a-digest")
    assert not contract.verify(contract.digest().upper())
    assert not contract.verify(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation_id": "XYZ"},
        {"operation_id": OPERATION_ID.upper()},
        {"lease_id": ""},
        {"lease_id": "has space"},
        {"stage_key": "x" * 200},
        {"provider": "Salad Cloud"},
        {"epoch": -1},
        {"epoch": True},
        {"epoch": 1.0},
        {"deadline_seconds": 0},
        {"deadline_seconds": True},
        {"deadline_seconds": 8 * 24 * 3600},
        {"silent_seconds": 0},
        {"silent_seconds": 4000},
        {"heartbeat_seconds": -5},
        {"heartbeat_seconds": 30.0},
        {"max_reports": 0},
        {"max_reports": False},
        {"max_reports": 201},
        {"max_reports": 10_000},
        {"kill_switch_path": "relative/marker"},
        {"kill_switch_path": ""},
        {"kill_switch_path": "/bad\x00path"},
    ],
)
def test_contract_validation_rejects(overrides):
    with pytest.raises(ValueError):
        make_contract(**overrides)


def test_contract_has_no_credential_or_url_fields():
    assert set(json.loads(make_contract().canonical_json())) == {
        "operation_id",
        "lease_id",
        "epoch",
        "stage_key",
        "provider",
        "deadline_seconds",
        "silent_seconds",
        "heartbeat_seconds",
        "max_reports",
        "kill_switch_path",
    }


def test_watcher_refuses_contract_that_does_not_verify(tmp_path):
    contract = make_contract()
    with pytest.raises(ValueError):
        Harness(tmp_path, contract, expected_digest=make_contract(epoch=9).digest())
    Harness(tmp_path, contract, expected_digest=contract.digest())


# -- kill switch ------------------------------------------------------------------------------


def test_kill_switch_none_is_engaged():
    switch = KillSwitch(None)
    assert switch.engaged()
    assert not switch.may_request()
    assert not switch.may_call_model()
    assert switch.may_observe() and switch.may_report()


def test_kill_switch_absent_under_valid_parent_is_clear(tmp_path):
    switch = KillSwitch(tmp_path / "stop")
    assert not switch.engaged()
    assert switch.may_request() and switch.may_call_model()


def test_kill_switch_present_marker_is_engaged(tmp_path):
    (tmp_path / "stop").write_text("")
    assert KillSwitch(tmp_path / "stop").engaged()


def test_kill_switch_dangling_symlink_is_engaged(tmp_path):
    (tmp_path / "stop").symlink_to(tmp_path / "nowhere")
    assert KillSwitch(tmp_path / "stop").engaged()


def test_kill_switch_missing_parent_is_engaged(tmp_path):
    assert KillSwitch(tmp_path / "missing" / "stop").engaged()


def test_kill_switch_parent_is_file_is_engaged(tmp_path):
    (tmp_path / "file").write_text("")
    assert KillSwitch(tmp_path / "file" / "stop").engaged()


def test_kill_switch_name_too_long_is_engaged(tmp_path):
    path = tmp_path / ("x" * 4096)
    with pytest.raises(OSError) as raised:
        os.lstat(path)
    assert raised.value.errno == errno.ENAMETOOLONG
    assert KillSwitch(path).engaged()


def test_kill_switch_relative_path_is_engaged():
    assert KillSwitch("relative/stop").engaged()


def test_kill_switch_rereads_every_call(tmp_path):
    marker = tmp_path / "stop"
    switch = KillSwitch(marker)
    assert not switch.engaged()
    marker.write_text("")
    assert switch.engaged()
    assert not switch.may_request()
    marker.unlink()
    assert not switch.engaged()
    assert switch.may_request()


# -- watcher classification -------------------------------------------------------------------


def test_healthy_run_heartbeats_at_cadence(tmp_path):
    h = Harness(tmp_path)
    assert h.kinds(h.watcher.tick()) == ["heartbeat"]
    for _ in range(5):
        h.clock.advance(10)
        assert h.watcher.tick() == []
    h.clock.advance(10)
    assert h.kinds(h.watcher.tick()) == ["heartbeat"]
    h.clock.advance(59)
    assert h.watcher.tick() == []
    h.clock.advance(1)
    assert h.kinds(h.watcher.tick()) == ["heartbeat"]
    assert [r.seq for r in h.sink.delivered] == [0, 1, 2]
    assert not h.watcher.done


def test_progress_when_counters_advance(tmp_path):
    h = Harness(tmp_path)
    h.probe.script = [
        {"state": "running", "output_bytes": 10, "cpu_ms": 100},
        {"state": "running", "output_bytes": 10, "cpu_ms": 100},
        {"state": "running", "output_bytes": 10, "cpu_ms": 250},
        {"state": "running", "output_bytes": 20, "cpu_ms": 250},
    ]
    assert h.kinds(h.watcher.tick()) == ["heartbeat"]
    h.clock.advance(5)
    assert h.watcher.tick() == []
    h.clock.advance(5)
    [report] = h.watcher.tick()
    assert report.kind == "progress"
    assert report.counters["cpu_ms"] == 250
    h.clock.advance(5)
    assert h.watcher.tick() == []  # advanced, but progress is paced to the routine interval
    h.probe.default = {"state": "running", "output_bytes": 30, "cpu_ms": 250}
    h.clock.advance(25)
    assert h.kinds(h.watcher.tick()) == ["progress"]


def test_stuck_reported_once_and_rearmed_after_progress(tmp_path):
    h = Harness(tmp_path, make_contract(silent_seconds=120, heartbeat_seconds=60))
    h.probe.default = {"state": "running", "cpu_ms": 5}
    kinds = []
    for _ in range(10):  # 0..270s with no advance
        kinds += h.kinds(h.watcher.tick())
        h.clock.advance(30)
    # Stuck once per episode; heartbeats continue on their cadence while stuck.
    assert kinds == ["heartbeat", "heartbeat", "stuck", "heartbeat", "heartbeat"]
    [stuck] = [r for r in h.sink.delivered if r.kind == "stuck"]
    assert stuck.counters["silent_s"] == 120
    # Progress resumes: progress report, then a new silent episode is reported again.
    h.probe.default = {"state": "running", "cpu_ms": 6}
    assert h.kinds(h.watcher.tick()) == ["progress"]
    kinds = []
    for _ in range(6):  # progress at 300s, heartbeat at 360s, stuck at 420s, heartbeat at 480s
        h.clock.advance(30)
        kinds += h.kinds(h.watcher.tick())
    assert kinds == ["heartbeat", "stuck", "heartbeat"]


def test_no_stuck_without_a_progress_signal(tmp_path):
    h = Harness(tmp_path, make_contract(silent_seconds=60, heartbeat_seconds=60))
    kinds = []
    for _ in range(6):
        kinds += h.kinds(h.watcher.tick())
        h.clock.advance(60)
    assert kinds == ["heartbeat"] * 6


@pytest.mark.parametrize(
    "terminal",
    [
        {"state": "exited", "exit_code": 1},
        {"state": "exited", "exit_code": 137, "oom_killed": True},
        {"state": "exited", "exit_code": 0, "oom_killed": True},
        {"state": "exited", "exit_code": None},
    ],
)
def test_failed_on_bad_exit(tmp_path, terminal):
    h = Harness(tmp_path)
    h.probe.script = [{"state": "running"}, terminal]
    h.watcher.tick()
    h.clock.advance(1)
    assert h.kinds(h.watcher.tick()) == ["failed"]
    assert h.watcher.done and h.watcher.outcome == "failed"
    calls = h.probe.calls
    h.clock.advance(1)
    assert h.watcher.tick() == []
    assert h.probe.calls == calls  # a finished watch observes nothing more


def test_failed_on_deadline_while_running(tmp_path):
    h = Harness(tmp_path, make_contract(deadline_seconds=600, heartbeat_seconds=600))
    assert h.kinds(h.watcher.tick()) == ["heartbeat"]
    h.clock.advance(599)
    assert h.watcher.tick() == []
    h.clock.advance(1)
    [report] = h.watcher.tick()
    assert report.kind == "failed" and report.counters["elapsed_s"] == 600
    assert h.watcher.done


@pytest.mark.parametrize("state", ["unknown", "absent", "raise"])
def test_failed_after_three_consecutive_unknown(tmp_path, state):
    h = Harness(tmp_path)
    miss = "raise" if state == "raise" else {"state": state}
    h.probe.script = [{"state": "running"}, miss, miss, {"state": "running"}, miss, miss, miss]
    kinds = []
    for _ in range(7):
        h.clock.advance(1)
        kinds += h.kinds(h.watcher.tick())
    # One or two misses are never a failure; the third consecutive miss is.
    assert kinds == ["heartbeat", "failed"]
    assert h.watcher.done


def test_clean_exit_ends_watch_without_report(tmp_path):
    h = Harness(tmp_path)
    h.probe.script = [{"state": "running"}, {"state": "exited", "exit_code": 0}]
    h.watcher.tick()
    h.clock.advance(1)
    assert h.watcher.tick() == []
    assert h.watcher.done and h.watcher.outcome == "exited_ok"


def test_max_reports_cap(tmp_path):
    h = Harness(tmp_path, make_contract(max_reports=3, heartbeat_seconds=1))
    total = []
    for _ in range(10):
        total += h.watcher.tick()
        h.clock.advance(30)
    # Routine reports stop at the routine cap; the last slot is held for ``failed``.
    assert h.kinds(total) == ["heartbeat", "heartbeat"]
    assert h.watcher.budget_exhausted
    h.probe.script = [{"state": "exited", "exit_code": 2}]
    [failed] = h.watcher.tick()
    assert failed.kind == "failed" and failed.seq == 2
    assert h.sink.delivered[-1] == failed
    assert h.watcher.done
    assert WatchOutbox(h.outbox_path).gaps().dropped == 0


def test_max_reports_cap_survives_restart(tmp_path):
    h = Harness(tmp_path, make_contract(max_reports=2, heartbeat_seconds=1))
    assert h.kinds(h.watcher.tick()) == ["heartbeat"]
    h.clock.advance(30)
    assert h.watcher.tick() == []
    restarted = h.build()
    h.clock.advance(30)
    assert restarted.tick() == []
    assert restarted.budget_exhausted
    h.probe.script = [{"state": "exited", "exit_code": 2}]
    h.clock.advance(1)
    assert h.kinds(restarted.tick()) == ["failed"]
    assert h.kinds(h.sink.delivered) == ["heartbeat", "failed"]


# -- authority --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["stop", "cancel", "retry", "resize", "reroute", "re_route", "kill", "restart", "terminate"],
)
def test_watcher_exposes_no_action_beyond_observe_and_report(tmp_path, name):
    h = Harness(tmp_path)
    assert not hasattr(h.watcher, name)
    assert not hasattr(StageWatcher, name)


def test_engaged_kill_switch_does_not_stop_observe_or_report(tmp_path):
    marker = tmp_path / "stop"
    marker.write_text("")
    h = Harness(tmp_path, kill_switch=KillSwitch(marker))
    [report] = h.watcher.tick()
    assert report.kill_switch_engaged is True
    assert h.sink.delivered == [report]
    assert h.watcher.may_request() is False
    assert h.watcher.may_call_model() is False
    marker.unlink()
    h.clock.advance(60)
    [report] = h.watcher.tick()
    assert report.kill_switch_engaged is False
    assert h.watcher.may_request() is True


def test_default_kill_switch_comes_from_contract(tmp_path):
    h = Harness(tmp_path, kill_switch=None)
    assert h.watcher.may_request() is False  # contract path None => engaged


# -- report body ------------------------------------------------------------------------------


def _report(**overrides) -> WatchReport:
    fields = {
        "operation_id": OPERATION_ID,
        "lease_id": "lease-1",
        "epoch": 2,
        "kind": "heartbeat",
        "observed_at": T0,
        "seq": 0,
        "counters": {"elapsed_s": 5, "mem_bytes": 1024},
        "kill_switch_engaged": False,
        "contract_digest": make_contract().digest(),
    }
    fields.update(overrides)
    return WatchReport(**fields)


def test_report_body_is_closed(tmp_path):
    h = Harness(tmp_path)
    h.probe.default = {
        "state": "running",
        "cpu_ms": 5,
        "mem_bytes": 7,
        "output_bytes": 9,
        "restarts": 1,
    }
    [report] = h.watcher.tick()
    body = report.to_body()
    assert set(body) == BODY_KEYS
    assert set(body["counters"]) <= COUNTER_KEYS
    assert all(type(v) is int for v in body["counters"].values())
    assert type(body["expected_epoch"]) is int and type(body["seq"]) is int
    assert type(body["kill_switch_engaged"]) is bool
    strings = {k: v for k, v in body.items() if isinstance(v, str)}
    assert set(strings) == {"lease_id", "kind", "observed_at", "contract_digest"}
    assert datetime.fromisoformat(body["observed_at"]).utcoffset() is not None
    assert body["observed_at"].endswith("+00:00")
    json.dumps(body, allow_nan=False)
    assert WatchReport.from_body(OPERATION_ID, body) == report


def test_report_iso_time_keeps_offset():
    tz = timezone(timedelta(hours=-5))
    body = _report(observed_at=datetime(2026, 1, 1, 7, tzinfo=tz)).to_body()
    assert body["observed_at"] == "2026-01-01T07:00:00-05:00"


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "observed"},
        {"kind": "note"},
        {"counters": {"log": 1}},
        {"counters": {"elapsed_s": "5"}},
        {"counters": {"elapsed_s": True}},
        {"counters": {"elapsed_s": -1}},
        {"counters": {"elapsed_s": 1.5}},
        {"observed_at": datetime(2026, 1, 1)},
        {"seq": -1},
        {"contract_digest": "abc"},
        {"operation_id": "op"},
        {"kill_switch_engaged": 1},
    ],
)
def test_report_validation_rejects(overrides):
    with pytest.raises(ValueError):
        _report(**overrides)


def test_report_counters_are_immutable():
    report = _report()
    with pytest.raises(TypeError):
        report.counters["elapsed_s"] = 9  # type: ignore[index]


def test_report_from_body_rejects_extra_keys():
    body = _report().to_body()
    body["message"] = "free text"
    with pytest.raises(ValueError):
        WatchReport.from_body(OPERATION_ID, body)


def test_idempotency_key_shape():
    key = idempotency_key(OPERATION_ID, 12345)
    assert key == f"watch-{OPERATION_ID}-12345"
    assert len(idempotency_key(OPERATION_ID, 2**63 - 1)) <= 128


class FakeChannel:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.posts: list[tuple[str, dict, str, float | None]] = []

    def post(self, path, body, key, *, timeout_seconds=None):
        self.posts.append((path, body, key, timeout_seconds))
        if self.error is not None:
            raise self.error
        return {"accepted": True}


def test_control_sink_posts_to_route_with_stable_key():
    channel = FakeChannel()
    report = _report(seq=7)
    assert ControlSink(channel, timeout_seconds=5).deliver(report) == DELIVERED
    [(path, body, key, timeout)] = channel.posts
    assert path == f"/worker/operations/{OPERATION_ID}/watch-reports"
    assert body == report.to_body()
    assert key == f"watch-{OPERATION_ID}-7"
    assert timeout == 5


def test_control_sink_untyped_failure_is_transient():
    channel = FakeChannel(ConnectionError("control rejected request (HTTP 503)"))
    assert ControlSink(channel).deliver(_report()) == TRANSIENT


PERMANENT = [400, 404, 410, 413, 422]
CLEARS_ON_RETRY = [None, 307, 401, 403, 408, 425, 429, 451, 499, 500, 502, 503, 504, 201]


@pytest.mark.parametrize("kind", ["stuck", "failed"])
@pytest.mark.parametrize("status", PERMANENT)
def test_control_sink_terminal_settles_only_on_permanent_status(kind, status):
    channel = FakeChannel(ControlChannelError("control rejected request", status))
    assert ControlSink(channel).deliver(_report(kind=kind)) == DeliveryResult("refused", status)


@pytest.mark.parametrize("kind", ["stuck", "failed"])
@pytest.mark.parametrize("status", [*CLEARS_ON_RETRY, 409])
def test_control_sink_terminal_status_that_can_clear_is_transient(kind, status):
    # 401/403 (whatever the message: the channel refreshes its credential), 409 (for example
    # a clock just ahead of control), 425 and every other non-permanent answer stay pending.
    error = ControlChannelError("control rejected request", status, authority_gone=status == 403)
    result = ControlSink(FakeChannel(error)).deliver(_report(kind=kind))
    assert result.outcome == "transient"


@pytest.mark.parametrize("kind", ["heartbeat", "progress"])
@pytest.mark.parametrize("status", [*PERMANENT, 409])
def test_control_sink_routine_settles_on_permanent_status_and_409(kind, status):
    channel = FakeChannel(ControlChannelError("control rejected request", status))
    assert ControlSink(channel).deliver(_report(kind=kind)) == DeliveryResult("refused", status)


@pytest.mark.parametrize("kind", ["heartbeat", "progress"])
@pytest.mark.parametrize("status", CLEARS_ON_RETRY)
def test_control_sink_routine_other_status_is_transient(kind, status):
    channel = FakeChannel(ControlChannelError("control transport unavailable", status))
    assert ControlSink(channel).deliver(_report(kind=kind)).outcome == "transient"


def test_control_sink_non_object_reply_is_transient():
    class OddChannel(FakeChannel):
        def post(self, path, body, key, *, timeout_seconds=None):
            return ["not", "an", "object"]

    assert ControlSink(OddChannel()).deliver(_report()).outcome == "transient"


def test_classify_status_and_result_validation():
    assert classify_status(409, "heartbeat") == DeliveryResult("refused", 409)
    assert classify_status(409, "failed").outcome == "transient"
    assert classify_status(401, "heartbeat").outcome == "transient"
    assert classify_status(429, "progress").outcome == "transient"
    assert classify_status(True, "failed").outcome == "transient"  # type: ignore[arg-type]
    assert TERMINAL_REFUSAL_STATUSES == frozenset({400, 404, 410, 413, 422})
    assert ROUTINE_REFUSAL_STATUSES == TERMINAL_REFUSAL_STATUSES | {409}
    for bad in (("refused", None), ("refused", 503), ("maybe", None), ("transient", "500")):
        with pytest.raises(ValueError):
            DeliveryResult(*bad)  # type: ignore[arg-type]


# -- outbox -----------------------------------------------------------------------------------


def test_outbox_file_is_owner_only(tmp_path):
    WatchOutbox(tmp_path / "o.sqlite")
    assert stat.S_IMODE(os.stat(tmp_path / "o.sqlite").st_mode) == 0o600


def test_outbox_parent_must_exist(tmp_path):
    with pytest.raises(ValueError):
        WatchOutbox(tmp_path / "missing" / "o.sqlite")
    (tmp_path / "file").write_text("")
    with pytest.raises(ValueError):
        WatchOutbox(tmp_path / "file" / "o.sqlite")


def test_outbox_refuses_another_contract(tmp_path):
    outbox = WatchOutbox(tmp_path / "o.sqlite")
    outbox.bind(OPERATION_ID, make_contract().digest(), T0)
    with pytest.raises(ValueError):
        outbox.bind(OPERATION_ID, make_contract(epoch=9).digest(), T0)


def test_outbox_records_once_per_seq(tmp_path):
    outbox = WatchOutbox(tmp_path / "o.sqlite")
    outbox.record(_report())
    with pytest.raises(ValueError):
        outbox.record(_report(kind="progress"))
    assert outbox.pending() == [_report()]


def test_record_before_post_and_redelivery_under_same_key(tmp_path):
    h = Harness(tmp_path)
    h.sink.fail = 3
    h.sink.raise_on_fail = True
    [report] = h.watcher.tick()
    # Recorded and still pending although every delivery attempt failed.
    outbox = WatchOutbox(h.outbox_path)
    assert outbox.pending() == [report]
    assert h.sink.delivered == []
    assert h.sink.attempts == [(0, report.idempotency_key)] * 3
    assert h.sleeps == [0.5, 1.0]  # bounded backoff through the injected sleep
    h.clock.advance(1)
    assert h.watcher.tick() == []
    assert h.sink.delivered == [report]
    assert {key for _, key in h.sink.attempts} == {f"watch-{OPERATION_ID}-0"}
    assert outbox.pending() == []
    assert outbox.gaps().last_delivered_seq == 0


def test_report_is_recorded_before_sink_is_called(tmp_path):
    outbox_path = tmp_path / "watch.sqlite"
    seen: list[list[int]] = []

    class InspectingSink:
        def deliver(self, report):
            seen.append([r.seq for r in WatchOutbox(outbox_path).pending()])
            return True

    watcher = StageWatcher(
        make_contract(),
        FakeProbe(),
        InspectingSink(),
        KillSwitch(None),
        FakeClock(),
        WatchOutbox(outbox_path),
        sleep=lambda _: None,
    )
    watcher.tick()
    assert seen == [[0]]


def test_transient_delivery_keeps_order_and_holds_later_reports(tmp_path):
    h = Harness(tmp_path, make_contract(heartbeat_seconds=1))
    h.sink.fail = 3
    h.watcher.tick()
    h.sink.fail = 3
    h.clock.advance(30)
    h.watcher.tick()
    assert [r.seq for r in WatchOutbox(h.outbox_path).pending()] == [0, 1]
    assert {seq for seq, _ in h.sink.attempts} == {0}  # seq 1 waits behind the transient seq 0
    h.clock.advance(30)
    h.watcher.tick()
    assert [r.seq for r in h.sink.delivered] == [0, 1, 2]


def test_abandon_after_24h_shows_in_gaps(tmp_path):
    h = Harness(tmp_path)
    h.sink.fail = 10**6
    h.watcher.tick()
    outbox = WatchOutbox(h.outbox_path)
    assert outbox.gaps().undelivered == 1
    h.probe.default = {"state": "exited", "exit_code": 0}
    h.clock.advance(24 * 3600 - 1)
    h.watcher.tick()
    gaps = outbox.gaps()
    assert (gaps.undelivered, gaps.abandoned) == (1, 0)
    h.clock.advance(1)
    h.watcher.tick()
    gaps = outbox.gaps()
    assert (gaps.undelivered, gaps.abandoned, gaps.last_delivered_seq) == (0, 1, None)
    attempts = len(h.sink.attempts)
    h.clock.advance(60)
    h.watcher.tick()
    assert len(h.sink.attempts) == attempts  # abandoned reports are never resent


def test_outbox_survives_restart(tmp_path):
    h = Harness(tmp_path, make_contract(heartbeat_seconds=1))
    h.sink.fail = 3
    [first] = h.watcher.tick()
    # Process restart: new outbox handle and watcher on the same file.
    restarted = h.build()
    h.clock.advance(1)
    assert restarted.tick() == []  # the heartbeat cadence survives the restart too
    h.clock.advance(29)
    [second] = restarted.tick()
    assert second.seq == 1  # seq continues; never reuses a key
    assert [r.seq for r in h.sink.delivered] == [0, 1]
    assert h.sink.delivered[0] == first


def test_restart_does_not_extend_watch_deadline(tmp_path):
    h = Harness(
        tmp_path, make_contract(deadline_seconds=100, silent_seconds=100, heartbeat_seconds=100)
    )
    h.watcher.tick()
    h.clock.advance(100)
    restarted = h.build()
    assert h.kinds(restarted.tick()) == ["failed"]


# -- budget pressure, refusals, restarts and gaps ---------------------------------------------


class ScriptedSink:
    """Answers per seq: a list of DeliveryResults consumed in order, then DELIVERED."""

    def __init__(self, answers: dict[int, list[DeliveryResult]] | None = None):
        self.answers = {seq: list(results) for seq, results in (answers or {}).items()}
        self.attempts: list[tuple[int, str, str]] = []
        self.delivered: list[WatchReport] = []

    def deliver(self, report: WatchReport) -> DeliveryResult:
        self.attempts.append((report.seq, report.kind, report.idempotency_key))
        queue = self.answers.get(report.seq)
        result = queue.pop(0) if queue else DELIVERED
        if result.outcome == "delivered":
            self.delivered.append(report)
        return result


REFUSED_409 = DeliveryResult("refused", 409)


@pytest.mark.parametrize("max_reports", [3, 4, 20, 200])
def test_failed_is_recorded_and_delivered_after_budget_pressure(tmp_path, max_reports):
    h = Harness(
        tmp_path,
        make_contract(
            max_reports=max_reports,
            deadline_seconds=7 * 24 * 3600,
            silent_seconds=60,
            heartbeat_seconds=1,
        ),
    )
    made: list[WatchReport] = []
    output = 0
    for _ in range(1500):  # 7500 s with output advancing on every 5 s tick
        output += 1
        h.probe.default = {"state": "running", "output_bytes": output}
        made += h.watcher.tick()
        h.clock.advance(5)
    routine = [r for r in made if r.kind in ("heartbeat", "progress")]
    assert len(routine) == routine_cap(max_reports) <= routine_limit(max_reports)
    assert h.watcher.budget_exhausted
    by_kind = [r.observed_at for r in routine if r.kind == "progress"]
    assert all(b - a >= timedelta(seconds=30) for a, b in zip(by_kind, by_kind[1:], strict=False))
    h.probe.script = [{"state": "exited", "exit_code": 137, "oom_killed": True}]
    [failed] = h.watcher.tick()
    assert failed.kind == "failed"
    assert h.sink.delivered[-1] == failed
    outbox = WatchOutbox(h.outbox_path)
    assert outbox.pending() == []
    assert len(outbox.recorded()) <= max_reports
    assert outbox.gaps().dropped == 0


def test_stuck_episodes_never_take_the_last_slot_from_failed(tmp_path):
    h = Harness(tmp_path, make_contract(max_reports=8, silent_seconds=60, heartbeat_seconds=60))
    cpu = 0
    for episode in range(20):  # each episode: advance once, then stay flat past silent_seconds
        cpu += 1
        h.probe.default = {"state": "running", "cpu_ms": cpu}
        for _ in range(4):
            h.watcher.tick()
            h.clock.advance(30)
        assert episode < 19 or h.watcher.budget_exhausted
    outbox = WatchOutbox(h.outbox_path)
    assert len(outbox.recorded()) == 7  # one slot left, held for ``failed``
    assert outbox.gaps().budget_dropped > 0  # dropped ``stuck`` reports are counted
    h.probe.script = [{"state": "exited", "exit_code": 1}]
    [failed] = h.watcher.tick()
    assert failed.seq == 7 and h.sink.delivered[-1] == failed
    assert failed.counters["dropped_reports"] == outbox.gaps().budget_dropped


def test_closing_report_past_the_total_cap_is_a_counted_gap(tmp_path):
    h = Harness(tmp_path, make_contract(max_reports=1, silent_seconds=60, heartbeat_seconds=60))
    h.probe.default = {"state": "running", "cpu_ms": 1}
    for _ in range(4):
        assert h.watcher.tick() == []  # no routine budget; ``stuck`` may not take the last slot
        h.clock.advance(30)
    assert WatchOutbox(h.outbox_path).gaps().budget_dropped == 1
    h.probe.script = [{"state": "exited", "exit_code": 1}]
    [failed] = h.watcher.tick()
    assert failed.seq == 0 and failed.counters["dropped_reports"] == 1
    assert h.sink.delivered == [failed]


def test_refused_report_does_not_hide_a_later_failed(tmp_path):
    h = Harness(tmp_path)
    h.sink = ScriptedSink({0: [REFUSED_409]})
    h.watcher = h.build()
    [heartbeat] = h.watcher.tick()
    assert h.sink.attempts == [(0, "heartbeat", heartbeat.idempotency_key)]  # never retried
    outbox = WatchOutbox(h.outbox_path)
    assert outbox.pending() == []
    assert outbox.refused_status(0) == 409
    assert outbox.gaps().refused == 1
    h.probe.script = [{"state": "exited", "exit_code": 1}]
    h.clock.advance(1)
    [failed] = h.watcher.tick()
    assert h.sink.delivered == [failed]
    assert failed.counters["refused_reports"] == 1
    for _ in range(3):
        h.clock.advance(60)
        h.watcher.tick()
    assert [seq for seq, _, _ in h.sink.attempts] == [0, 1]  # refused seq 0 is never resent


def test_refusal_does_not_stop_the_flush(tmp_path):
    h = Harness(tmp_path, make_contract(heartbeat_seconds=30))
    h.sink = ScriptedSink({0: [TRANSIENT] * 6})
    h.watcher = h.build()
    h.watcher.tick()
    h.clock.advance(30)
    h.watcher.tick()
    assert [r.seq for r in WatchOutbox(h.outbox_path).pending()] == [0, 1]
    h.sink.answers = {0: [REFUSED_409], 1: [DeliveryResult("refused", 422)]}
    h.clock.advance(30)
    h.watcher.tick()  # seq 0 and 1 refused, seq 2 still delivered in the same flush
    assert [r.seq for r in h.sink.delivered] == [2]
    gaps = WatchOutbox(h.outbox_path).gaps()
    assert (gaps.refused, gaps.undelivered, gaps.last_delivered_seq) == (2, 0, 2)


def test_failed_is_flushed_ahead_of_a_transiently_failing_routine_report(tmp_path):
    h = Harness(tmp_path)
    h.sink = ScriptedSink({0: [TRANSIENT] * 6})
    h.watcher = h.build()
    [heartbeat] = h.watcher.tick()
    h.probe.script = [{"state": "exited", "exit_code": 1}]
    h.clock.advance(1)
    [failed] = h.watcher.tick()
    assert h.sink.delivered == [failed]  # offered first, delivered despite seq 0 failing
    assert [r.seq for r in WatchOutbox(h.outbox_path).pending()] == [0]
    h.clock.advance(1)
    h.watcher.tick()
    assert h.sink.delivered == [failed, heartbeat]
    assert {key for seq, _, key in h.sink.attempts if seq == 0} == {heartbeat.idempotency_key}
    assert len([1 for seq, _, _ in h.sink.attempts if seq == 0]) == 7  # retried, same key


def test_restart_while_output_grows_reports_no_stuck(tmp_path):
    h = Harness(tmp_path, make_contract(silent_seconds=120, heartbeat_seconds=60))
    output = 0
    kinds: list[str] = []
    for _ in range(5):
        output += 10
        h.probe.default = {"state": "running", "output_bytes": output}
        kinds += h.kinds(h.watcher.tick())
        h.clock.advance(30)
    h.clock.advance(600)  # the watcher was down for 10 minutes; the run kept writing
    restarted = h.build()
    for _ in range(10):
        output += 10
        h.probe.default = {"state": "running", "output_bytes": output}
        kinds += h.kinds(restarted.tick())
        h.clock.advance(30)
    assert "stuck" not in kinds
    assert "progress" in kinds[kinds.index("progress") + 1 :]


def test_restart_with_flat_output_reports_stuck_after_silence_since_first_sample(tmp_path):
    h = Harness(tmp_path, make_contract(silent_seconds=120, heartbeat_seconds=60))
    h.probe.default = {"state": "running", "output_bytes": 10}
    h.watcher.tick()
    h.clock.advance(600)  # down for longer than silent_seconds
    restarted = h.build()
    first = h.clock.now
    kinds = []
    while h.clock.now - first < timedelta(seconds=120):
        kinds += h.kinds(restarted.tick())
        h.clock.advance(30)
    assert "stuck" not in kinds
    [stuck] = restarted.tick()
    assert stuck.kind == "stuck" and stuck.counters["silent_s"] == 120
    assert stuck.counters["elapsed_s"] == 600 + 120


def test_restart_after_failed_keeps_the_watch_finished(tmp_path):
    h = Harness(tmp_path)
    h.probe.script = [{"state": "exited", "exit_code": 1}]
    assert h.kinds(h.watcher.tick()) == ["failed"]
    restarted = h.build()
    calls = h.probe.calls
    assert restarted.done and restarted.outcome == "failed"
    assert restarted.tick() == [] and h.probe.calls == calls


def test_abandoned_reports_are_reported_as_dropped(tmp_path):
    h = Harness(tmp_path, make_contract(deadline_seconds=7 * 24 * 3600))
    h.sink.fail = 10**6
    h.watcher.tick()
    h.clock.advance(24 * 3600)
    h.watcher.tick()  # abandons seq 0, records a heartbeat (still failing)
    h.sink.fail = 0
    h.clock.advance(60)
    [report] = h.watcher.tick()
    assert report.counters["dropped_reports"] == 1
    assert "refused_reports" not in report.counters


def test_every_file_next_to_the_outbox_is_owner_only(tmp_path):
    old = os.umask(0)
    try:
        h = Harness(tmp_path, make_contract(heartbeat_seconds=30))
        h.sink = ScriptedSink({1: [REFUSED_409]})
        h.watcher = h.build()
        for _ in range(4):
            h.watcher.tick()
            h.clock.advance(30)
        h.probe.script = [{"state": "exited", "exit_code": 1}]
        h.watcher.tick()
    finally:
        os.umask(old)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["watch.sqlite", "watch.sqlite-journal"]
    for path in tmp_path.iterdir():
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600, path.name


def test_outbox_refuses_a_symlinked_journal(tmp_path):
    (tmp_path / "o.sqlite-journal").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        WatchOutbox(tmp_path / "o.sqlite")
    assert not (tmp_path / "elsewhere").exists()


def test_outbox_mark_refused_requires_a_4xx(tmp_path):
    outbox = WatchOutbox(tmp_path / "o.sqlite")
    outbox.record(_report())
    for bad in (503, 200, True, None):
        with pytest.raises(ValueError):
            outbox.mark_refused(0, bad)  # type: ignore[arg-type]
    outbox.mark_refused(0, 404, T0)
    outbox.mark_delivered(0, T0)  # a settled refusal stays refused
    assert outbox.gaps().refused == 1 and outbox.gaps().last_delivered_seq is None


def test_contract_max_reports_is_the_shared_control_limit():
    assert make_contract(max_reports=MAX_REPORTS_PER_OPERATION).max_reports == 200
    with pytest.raises(ValueError):
        make_contract(max_reports=MAX_REPORTS_PER_OPERATION + 1)


def test_outbox_mark_refused_never_settles_a_terminal_report_on_a_status_that_can_clear(tmp_path):
    outbox = WatchOutbox(tmp_path / "o.sqlite")
    outbox.record(_report(kind="failed"))
    for status in (401, 403, 409, 425, 429, 451):
        with pytest.raises(ValueError):
            outbox.mark_refused(0, status, T0)
    assert [r.seq for r in outbox.pending()] == [0]
    outbox.mark_refused(0, 422, T0)
    assert outbox.refused_status(0) == 422


# -- terminal reports survive refusals that clear on retry ------------------------------------


class FakeControl:
    """The control route's rules that matter to delivery, as a control channel.

    Queued ``answers`` are raised first (one per post). Then: an exact replay under a stored
    key returns the stored reply; ``observed_at`` beyond control's clock + 5 s is refused
    with ``too_early`` (425); earlier than the last recorded report with 409; a routine
    report once ``MAX - TERMINAL_RESERVE`` reports of any kind are recorded with 409; any
    report at the total cap with 409.
    """

    def __init__(self, clock, answers=(), too_early: int = 425):
        self.clock = clock
        self.answers = list(answers)
        self.too_early = too_early
        self.posts: list[tuple[str, dict]] = []
        self.recorded: list[dict] = []
        self.replies: dict[str, dict] = {}
        self.refusals: list[tuple[str, int]] = []

    def _refuse(self, body, status):
        self.refusals.append((body["kind"], status))
        raise ControlChannelError("control rejected request", status)

    def post(self, path, body, key, *, timeout_seconds=None):
        self.posts.append((key, body))
        if self.answers:
            status = self.answers.pop(0)
            raise ControlChannelError("control rejected request", status)
        if key in self.replies:
            return self.replies[key]
        at = datetime.fromisoformat(body["observed_at"])
        if at > self.clock() + timedelta(seconds=5):
            self._refuse(body, self.too_early)
        if self.recorded and at < datetime.fromisoformat(self.recorded[-1]["observed_at"]):
            self._refuse(body, 409)
        if body["kind"] in ("heartbeat", "progress") and len(self.recorded) >= (
            MAX_REPORTS_PER_OPERATION - TERMINAL_RESERVE
        ):
            self._refuse(body, 409)
        if len(self.recorded) >= MAX_REPORTS_PER_OPERATION:
            self._refuse(body, 409)
        self.recorded.append(body)
        self.replies[key] = {"recorded": True}
        return self.replies[key]


def _control_harness(tmp_path, control: FakeControl | None = None, **contract) -> Harness:
    h = Harness(tmp_path, make_contract(**contract))
    h.control = control or FakeControl(h.clock)
    h.sink = ControlSink(h.control)
    h.watcher = h.build()
    return h


def _fail_next(h: Harness) -> WatchReport:
    h.probe.script = [{"state": "exited", "exit_code": 1}]
    [failed] = h.watcher.tick()
    assert failed.kind == "failed"
    return failed


@pytest.mark.parametrize("status", [401, 403, 408, 409, 425, 429, 451, 500, 503])
def test_failed_refused_by_a_status_that_clears_is_delivered_later_under_the_same_key(
    tmp_path, status
):
    h = _control_harness(tmp_path)
    h.control.answers = [status] * 3  # every attempt of this tick (a token expired, ...)
    failed = _fail_next(h)
    outbox = WatchOutbox(h.outbox_path)
    assert [r.seq for r in outbox.pending()] == [failed.seq]
    assert outbox.refused_status(failed.seq) is None and outbox.gaps().refused == 0
    assert not h.watcher.finished
    h.clock.advance(5)
    h.watcher.tick()  # the credential was refreshed; control accepts now
    assert h.control.recorded == [failed.to_body()]
    assert {key for key, _ in h.control.posts} == {failed.idempotency_key}
    assert len(h.control.posts) == 4
    assert outbox.pending() == [] and outbox.gaps().refused == 0
    assert h.watcher.finished


@pytest.mark.parametrize("status", [401, 403, 409, 425])
def test_a_sink_cannot_settle_a_failed_on_a_status_that_clears(tmp_path, status):
    h = Harness(tmp_path)
    h.sink = ScriptedSink({0: [DeliveryResult("refused", status)] * 3})
    h.watcher = h.build()
    failed = _fail_next(h)
    outbox = WatchOutbox(h.outbox_path)
    assert outbox.refused_status(0) is None and [r.seq for r in outbox.pending()] == [0]
    h.watcher.flush()
    assert h.sink.delivered == [failed]
    assert {key for _, _, key in h.sink.attempts} == {failed.idempotency_key}


@pytest.mark.parametrize("status", [400, 404, 410, 413, 422])
def test_failed_refused_with_a_permanent_status_is_settled(tmp_path, status):
    h = _control_harness(tmp_path)
    h.control.answers = [status]
    failed = _fail_next(h)
    outbox = WatchOutbox(h.outbox_path)
    assert outbox.refused_status(failed.seq) == status and outbox.gaps().refused == 1
    h.clock.advance(60)
    h.watcher.tick()
    assert len(h.control.posts) == 1  # never resent
    assert h.watcher.finished


@pytest.mark.parametrize(
    ("status", "settled"),
    [(401, False), (403, False), (425, False), (503, False), (409, True), (422, True)],
)
def test_routine_report_settles_only_on_permanent_status_or_409(tmp_path, status, settled):
    h = _control_harness(tmp_path)
    h.control.answers = [status] * 3
    [heartbeat] = h.watcher.tick()
    outbox = WatchOutbox(h.outbox_path)
    assert (outbox.refused_status(0) == status) is settled
    h.clock.advance(1)
    h.watcher.tick()
    assert (h.control.recorded == [heartbeat.to_body()]) is not settled
    assert len(h.control.posts) == (1 if settled else 4)  # retried next tick, same key


def test_terminal_report_is_not_abandoned_while_a_later_report_is_pending(tmp_path):
    outbox = WatchOutbox(tmp_path / "o.sqlite")
    hour = timedelta(hours=1)
    outbox.record(_report(seq=0, kind="heartbeat", observed_at=T0))
    outbox.record(_report(seq=1, kind="stuck", observed_at=T0 + hour))
    outbox.record(_report(seq=2, kind="heartbeat", observed_at=T0 + 2 * hour))
    # 24 h after the stuck: the earlier heartbeat goes, the stuck waits for seq 2.
    assert outbox.abandon_expired(T0 + 25 * hour) == 1
    assert [r.seq for r in outbox.pending()] == [1, 2]
    # Once the later report expires too, both go in the same pass (still bounded).
    assert outbox.abandon_expired(T0 + 26 * hour) == 2
    assert outbox.pending() == [] and outbox.gaps().abandoned == 3


def test_observed_at_is_monotonic_when_the_clock_steps_back(tmp_path):
    control_clock = FakeClock()
    h = _control_harness(tmp_path, FakeControl(control_clock), heartbeat_seconds=30)
    [first] = h.watcher.tick()
    h.clock.advance(-120)  # the worker clock steps back two minutes
    restarted = h.build()  # also across a restart (the floor comes from the outbox)
    h.clock.advance(1)
    h.probe.script = [{"state": "exited", "exit_code": 1}]
    [failed] = restarted.tick()
    assert failed.observed_at == first.observed_at
    assert failed.counters["elapsed_s"] == 0  # counters still use the clock
    assert h.control.recorded == [first.to_body(), failed.to_body()]
    assert h.control.refusals == []
    recorded = [r.observed_at for r in WatchOutbox(h.outbox_path).recorded()]
    assert recorded == sorted(recorded)


@pytest.mark.parametrize("too_early", [425, 409])
def test_failed_dated_just_ahead_of_control_is_delivered_once_control_catches_up(
    tmp_path, too_early
):
    control_clock = FakeClock(T0 - timedelta(seconds=6))  # the worker runs 6 s ahead
    h = _control_harness(tmp_path, FakeControl(control_clock, too_early=too_early))
    h.probe.script = [{"state": "exited", "exit_code": 1}]
    [failed] = h.watcher.tick()
    assert h.control.refusals == [("failed", too_early)] * 3
    assert WatchOutbox(h.outbox_path).refused_status(failed.seq) is None
    control_clock.advance(2)
    h.clock.advance(2)
    h.watcher.flush()
    assert h.control.recorded == [failed.to_body()]  # the identical body, same key
    assert {key for key, _ in h.control.posts} == {failed.idempotency_key}
    assert h.watcher.finished


def test_routine_budget_counts_every_recorded_report_like_control(tmp_path):
    h = _control_harness(
        tmp_path,
        max_reports=MAX_REPORTS_PER_OPERATION,
        deadline_seconds=7 * 24 * 3600,
        silent_seconds=60,
        heartbeat_seconds=30,
    )
    cpu = 0
    for _ in range(200):  # episodes: one advance, then flat past silent_seconds (a ``stuck``)
        cpu += 1
        h.probe.default = {"state": "running", "cpu_ms": cpu}
        for _ in range(4):
            h.watcher.tick()
            h.clock.advance(30)
        if h.watcher.budget_exhausted:
            break
    kinds = [body["kind"] for body in h.control.recorded]
    assert kinds.count("stuck") >= 10  # several stuck episodes used routine-era slots
    assert h.watcher.budget_exhausted
    assert h.control.refusals == []  # control never had to refuse a routine report
    assert WatchOutbox(h.outbox_path).gaps().refused == 0
    routine = [i for i, kind in enumerate(kinds) if kind in ("heartbeat", "progress")]
    assert routine[-1] < MAX_REPORTS_PER_OPERATION - TERMINAL_RESERVE
    failed = _fail_next(h)
    assert h.control.recorded[-1] == failed.to_body()


def test_counter_drop_after_a_container_restart_is_progress(tmp_path):
    h = Harness(tmp_path, make_contract(silent_seconds=120, heartbeat_seconds=60))
    kinds: list[str] = []
    for output in (1000, 2000):
        h.probe.default = {"state": "running", "output_bytes": output, "cpu_ms": output}
        kinds += h.kinds(h.watcher.tick())
        h.clock.advance(30)
    # The container restarts: counters reset and grow again, still below the old mark.
    for output in range(10, 200, 20):
        h.probe.default = {"state": "running", "output_bytes": output, "cpu_ms": output}
        kinds += h.kinds(h.watcher.tick())
        h.clock.advance(30)
    assert "stuck" not in kinds
    assert kinds.count("progress") >= 3


def test_restart_count_rise_is_progress_and_flat_counters_still_go_stuck(tmp_path):
    h = Harness(tmp_path, make_contract(silent_seconds=120, heartbeat_seconds=60))
    h.probe.default = {"state": "running", "output_bytes": 50, "restarts": 0}
    h.watcher.tick()
    h.clock.advance(90)
    h.probe.default = {"state": "running", "output_bytes": 50, "restarts": 1}
    assert h.kinds(h.watcher.tick()) == ["progress"]
    kinds: list[str] = []
    for _ in range(5):  # unchanged counters: stuck once silent_seconds pass
        h.clock.advance(30)
        kinds += h.kinds(h.watcher.tick())
    assert "stuck" in kinds
    [stuck] = [r for r in h.sink.delivered if r.kind == "stuck"]
    assert stuck.counters["silent_s"] == 120


def test_flush_delivers_after_done_without_sampling_until_finished(tmp_path):
    h = Harness(tmp_path)
    h.sink = ScriptedSink({0: [TRANSIENT] * 3})
    h.watcher = h.build()
    failed = _fail_next(h)
    assert h.watcher.done and h.watcher.pending_count == 1 and not h.watcher.finished
    calls = h.probe.calls
    h.watcher.flush()
    assert h.probe.calls == calls  # flush never samples
    assert h.sink.delivered == [failed]
    assert h.watcher.pending_count == 0 and h.watcher.finished
