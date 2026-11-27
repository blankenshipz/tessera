"""Follow-ups to the Salad container exit watch.

* The watch backs off (gaps double every 4 reads, and again per failed read), stops every watch
  read for the UTC day after a rate-limited answer, has a daily budget across frames, and the
  post-stop events read is skipped once the watch already recorded the events.
* A crash loop the guardian stopped first reads ``[1, 137]``: the earlier non-zero exit is the
  workload's failure, not a provider interruption. A kill that came first keeps today's class.

Fakes only (and the real guardian's SQLite store): no provider, no network, no credentials.
"""

from __future__ import annotations

import json

import pytest
from test_salad_nonzero_exit import (
    INTERRUPTED,
    _events,
    _Guardian,
    _observation,
    _observe,
    _Service,
)
from test_salad_safety_service import service_case  # noqa: F401
from test_salad_start_failure import OPERATION, world  # noqa: F401

from tessera_control import salad_exit_watch
from tessera_control.campaign_infra_retry import SALAD_INTERRUPTED
from tessera_control.campaign_salad_executor import project_guardian_final
from tessera_control.salad_container_log import summarize_container_log, unavailable
from tessera_control.salad_events import summarize_events
from tessera_control.salad_exit_watch import (
    FAILURE_CLASS,
    STDERR_UNAVAILABLE,
    WATCH_INTERVAL_SECONDS,
    WATCH_MAX_INTERVAL_SECONDS,
    container_exit_receipt,
    nonzero_exit_codes,
)

FIELD_LINE = "ValueError: expected exactly one input named shampoo_features.json.gz, found 0"
SECRET = "sk-0123456789abcdefghijklmnop"


class _RateLimited(ValueError):
    detail = "rate_limited"


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _service(records, clock=None):
    return _Service(_Guardian(), records, clock=clock or _Clock())


def _pass(service, reservation="r"):
    """One watch pass that may start a read, the read's end, and the pass that takes its answer."""
    service._watch_container_exit(reservation)
    state = service._exit_watch.get(reservation)
    if state and state["thread"] is not None:
        state["thread"].join(5)
    service._watch_container_exit(reservation)


# -- F4: the earliest non-zero exit decides --------------------------------------------------


def test_a_crash_loop_the_guardian_stopped_first_is_the_workloads_failure():
    assert nonzero_exit_codes(_events([1, 137])) == [1]
    observation = _observe({"start_events": _events([1, 137])}, **INTERRUPTED)
    assert observation.outcome == "failed_permanent"
    assert observation.failure_class == FAILURE_CLASS
    assert "code 1" in observation.detail


@pytest.mark.parametrize("codes", [[137, 1], [143, 2], [0, 137]])
def test_a_kill_that_came_first_keeps_todays_class(codes):
    assert nonzero_exit_codes(_events(codes)) == []
    observation = _observe({"start_events": _events(codes)}, **INTERRUPTED)
    assert observation.failure_class == SALAD_INTERRUPTED


def test_events_keep_their_exit_codes_in_first_seen_order():
    newest_first = [
        {"time": "2026-11-27T00:03:00Z", "text_log": "Instance Exited: 137"},
        {"time": "2026-11-27T00:02:00Z", "text_log": "Instance Exited: 1"},
        {"time": "2026-11-27T00:01:00Z", "text_log": "Instance Exited: 1"},
    ]
    assert summarize_events(newest_first)["exit_codes"] == [1, 137]
    # Without times the query's descending order is reversed.
    untimed = [{"text_log": "Instance Exited: 137"}, {"text_log": "Instance Exited: 2"}]
    assert summarize_events(untimed)["exit_codes"] == [2, 137]


def test_the_watch_stops_a_crash_loop_read_as_one_then_kill():
    service = _service([_observation([1, 137])])
    _pass(service)
    assert ("cancel", "op") in service.effects
    assert service.guardian.armed == [True]


# -- F2: backoff, rate limit, daily budget ---------------------------------------------------


def test_the_gap_between_reads_doubles_every_four_reads():
    clock = _Clock()
    service = _service([_observation([]) for _ in range(20)], clock)
    gaps = []
    for _ in range(10):
        _pass(service)
        state = service._exit_watch["r"]
        gaps.append(state["next"] - clock.now)
        clock.now = state["next"]
    assert gaps[:4] == [WATCH_INTERVAL_SECONDS] * 4
    assert gaps[4:8] == [2 * WATCH_INTERVAL_SECONDS] * 4
    assert gaps[8] == 4 * WATCH_INTERVAL_SECONDS
    assert max(gaps) <= WATCH_MAX_INTERVAL_SECONDS


def test_a_failed_read_backs_off_further():
    clock = _Clock()
    service = _service([], clock)  # every read raises
    _pass(service)
    state = service._exit_watch["r"]
    assert state["failures"] == 1
    assert state["next"] - clock.now == 2 * WATCH_INTERVAL_SECONDS
    assert service.diagnostic_write_failures == 0


def test_a_rate_limited_answer_ends_every_watch_read_for_the_utc_day():
    clock = _Clock()
    service = _service([], clock)

    def limited(row, action, provider_id):
        service.effects.append("read")
        raise _RateLimited()

    service._bounded_send = limited
    _pass(service, "r")
    assert service.effects.count("read") == 1
    # No other frame reads today, however long it waits.
    for _ in range(5):
        clock.now += WATCH_MAX_INTERVAL_SECONDS + 1
        _pass(service, "r")
        _pass(service, "other")
    assert service.effects.count("read") == 1
    assert service.guardian.armed == []


def test_the_rate_limit_lifts_on_the_next_utc_day(monkeypatch):
    clock = _Clock()
    service = _service([_observation([])], clock)
    service._exit_watch_day.update(quota_day="2000-01-01")
    _pass(service)
    assert service.effects.count("read") == 1


def test_the_daily_budget_bounds_every_frame_together(monkeypatch):
    monkeypatch.setattr(salad_exit_watch, "WATCH_DAILY_READ_BUDGET", 3)
    clock = _Clock()
    service = _service([_observation([]) for _ in range(10)], clock)
    for reservation in ("a", "b", "c", "d", "e"):
        _pass(service, reservation)
    assert service.effects.count("read") == 3


# -- F2: no second events query after the watch recorded the events --------------------------


def test_the_post_stop_read_is_skipped_once_the_events_are_recorded(world):  # noqa: F811
    guardian, clock, seen, started, stops = world
    guardian.record_start_events(
        OPERATION,
        dict(outcome="read", events=["instance_exited"], exit_codes=[1], machines=1),
    )
    guardian.cancel(OPERATION)
    assert guardian.claim_start_events_read(OPERATION) is False


def test_the_post_stop_read_still_runs_when_nothing_was_recorded(world):  # noqa: F811
    guardian, clock, seen, started, stops = world
    guardian.cancel(OPERATION)
    assert guardian.claim_start_events_read(OPERATION) is True


# -- F6: the real safety service, from a non-zero exit to a confirmed stop -------------------


def test_the_real_service_stops_a_crash_looping_group_and_closes_custody(service_case):  # noqa: F811
    from types import SimpleNamespace

    s, body, request, token, clock, provider, *_ = service_case
    reservation = body["reservation"]
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    real_send = s._bounded_send
    reads = []

    def send(row, action, *args, **kwargs):
        if action == "read_logs":
            reads.append(action)
            return SimpleNamespace(
                observation=dict(
                    provider_id="p",
                    container_log=summarize_container_log(
                        [
                            {"time": "2026-11-27T00:00:02Z", "text_log": FIELD_LINE},
                            {"time": "2026-11-27T00:00:01Z", "text_log": "export TOKEN=" + SECRET},
                        ]
                    ),
                )
            )
        if action == "read_events":
            reads.append(action)
            return SimpleNamespace(
                observation=dict(
                    events=["instance_started", "instance_exited"],
                    exit_codes=[1, 137],
                    machines=1,
                    empty=False,
                )
            )
        return real_send(row, action, *args, **kwargs)

    s._bounded_send = send
    # The fixture's own loop already sent one watch read (the fake provider has no events, so it
    # failed): take its answer, then clear the backoff (this frame's deadline is only 30 s).
    state = s._exit_watch[reservation]
    state["thread"].join(5)
    clock[0] += 5
    s.loop()
    assert state["result"] is None and state["failures"] == 1
    state["next"] = 0.0
    clock[0] += 5
    s.loop()  # the watch starts its read off the loop thread
    s._exit_watch[reservation]["thread"].join(5)
    clock[0] += 5
    s.loop()  # the answer: record, fence, stop

    status = s.admin(dict(operation="status", reservation=reservation))
    assert status["stop_cause"] == salad_exit_watch.NONZERO_EXIT_CAUSE
    for _ in range(3):  # the group still reads running: the guardian sends the exact stop
        clock[0] += 2
        s.loop()
    assert "stop" in provider.calls
    # Custody is closed to the workload: a late output is refused.
    output = {"late": 1}
    import hashlib

    from test_salad_safety_service import encoded

    with pytest.raises(ValueError):
        s.workload(
            "/v1/workload/output",
            dict(
                binding=request, output=output, sha256=hashlib.sha256(encoded(output)).hexdigest()
            ),
            token,
        )
    # The provider confirms the stop; the one post-stop events read is not sent again.
    provider.state = "stopped"
    for _ in range(3):
        clock[0] += 5
        s.loop()
    final = s.admin(dict(operation="status", reservation=reservation))
    assert final["stop_cause"] == salad_exit_watch.NONZERO_EXIT_CAUSE
    assert final["status"] in {"stopped", "unknown"}
    assert final["start_events"]["exit_codes"] == [1, 137]
    # One events read (the watch's; the post-stop one is skipped) and one container-log read.
    assert sorted(reads) == ["read_events", "read_logs"]
    tail = final["container_log"]
    assert tail["untrusted"] is True and tail["lines"][-1] == FIELD_LINE
    assert SECRET not in json.dumps(final)
    # The tail reaches the recorded guardian final and the receipt's exit evidence.
    projected = project_guardian_final(final)
    assert projected is not None and projected["container_log"] == tail
    receipt = container_exit_receipt(projected["start_events"], projected["container_log"])
    assert receipt["exit_codes"] == [1] and receipt["stderr_tail"] == tail


# -- F1: the container-log tail -------------------------------------------------------------


def test_without_a_recorded_tail_the_receipt_says_unavailable():
    receipt = container_exit_receipt(_events([2]))
    assert receipt["stderr_tail"] == STDERR_UNAVAILABLE


def test_a_tail_that_is_not_the_closed_shape_is_never_on_the_receipt():
    forged = dict(summarize_container_log([{"text_log": "x"}]), lines=["y"])  # digest mismatch
    assert container_exit_receipt(_events([2]), forged)["stderr_tail"] == STDERR_UNAVAILABLE


def test_the_guardian_keeps_one_closed_tail_and_claims_the_read_once(world):  # noqa: F811
    guardian, clock, seen, started, stops = world
    assert guardian.claim_container_log_read(OPERATION) is True
    assert guardian.claim_container_log_read(OPERATION) is False  # never a second query
    guardian.record_container_log(OPERATION, {"outcome": "read", "lines": ["raw"]})  # not closed
    assert "container_log" not in guardian.status(OPERATION)
    first = summarize_container_log([{"text_log": "first"}])
    guardian.record_container_log(OPERATION, first)
    guardian.record_container_log(OPERATION, summarize_container_log([{"text_log": "second"}]))
    assert guardian.status(OPERATION)["container_log"] == first


def test_a_rate_limited_log_read_records_a_closed_token():
    service = _service([])
    reads = []

    def limited(row, action, provider_id):
        reads.append(action)
        raise _RateLimited()

    service._bounded_send = limited
    service.guardian.claim_container_log_read = lambda operation: True
    kept = []
    service.guardian.record_container_log = lambda operation, value: kept.append(value)
    service._read_container_log(service.row)
    import time

    deadline = time.monotonic() + 5  # the read runs on its own thread; a loaded box is slow
    while not service._container_log_done and time.monotonic() < deadline:
        time.sleep(0.01)
    service._drain_container_logs()
    assert reads == ["read_logs"]
    assert kept == [unavailable("log_quota_exhausted")]


def test_after_a_rate_limit_today_the_log_read_is_not_sent():
    service = _service([])
    service._exit_watch_day.update(quota_day=salad_exit_watch._utc_day())
    service.guardian.claim_container_log_read = lambda operation: True
    kept = []
    service.guardian.record_container_log = lambda operation, value: kept.append(value)
    service._read_container_log(service.row)
    service._drain_container_logs()
    assert "read" not in service.effects
    assert kept == [unavailable("log_quota_exhausted")]
