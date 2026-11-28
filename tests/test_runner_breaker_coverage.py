"""The runner breaker reaches every campaign status and every pre-stage hook (ADR 0210).

A campaign the breaker cannot block (blocked, paused, cancelling) gets one event per class; only
an already blocked one's drive backs off, a cancelling one is driven every pass. A provider hook
that raises is recorded once and never starves the stages.
Real stores and runner on temporary state; errors are simulated by patching one runner method
or one provider hook, and the clock is the runner's own ``now``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World, stage_spec
from test_runner_error_breaker import _campaign, _money, _reserved

from tessera_control.campaign_models import stage_provider
from tessera_control.campaign_runner import (
    CANCEL_RUNNER_ERROR_EVENT,
    HOOK_RUNNER_ERROR_EVENT,
    MAX_STUCK_BACKOFF_SECONDS,
    RUNNER_ERROR_EVENT,
    RUNNER_ERROR_THRESHOLD,
    STUCK_BACKOFF_SECONDS,
    STUCK_RUNNER_ERROR_EVENT,
    CampaignRunner,
)

BREAKER_EVENTS = (RUNNER_ERROR_EVENT, STUCK_RUNNER_ERROR_EVENT, CANCEL_RUNNER_ERROR_EVENT)
PROVIDER = type(stage_provider("local-docker"))
RUNNER_ENTRIES = {
    "close_unreported_exits": "_close_unreported_exits",
    "reclaim_unclaimed_grants": "_reclaim_unclaimed_grants",
    "release_unleased_grants": "_release_unleased_grants",
    "drive_blocked": "_drive_blocked",
    "drive_cancel": "_drive_cancel",
}
HOOKS = ("after_tick", "evidence_pass")
FORWARD = ("running", "stopping")
PREAMBLE = ["close_unreported_exits"]

# (status, entry point it reaches, the one breaker event it must record, status afterwards)
CASES = [
    *[("running", e, RUNNER_ERROR_EVENT, "blocked") for e in (*PREAMBLE, *HOOKS)],
    # A stopping campaign's stages are refused forward steps and it completes in one pass, so
    # only the tick preamble (which precedes the stages) can fail it ten times running.
    ("stopping", "close_unreported_exits", RUNNER_ERROR_EVENT, "blocked"),
    *[
        ("blocked", e, STUCK_RUNNER_ERROR_EVENT, "blocked")
        for e in (*PREAMBLE, "reclaim_unclaimed_grants", "drive_blocked", "evidence_pass")
    ],
    *[
        ("paused_delegation_lapsed", e, STUCK_RUNNER_ERROR_EVENT, "paused_delegation_lapsed")
        for e in (*PREAMBLE, "reclaim_unclaimed_grants", "release_unleased_grants")
    ],
    *[
        ("cancel_requested", e, CANCEL_RUNNER_ERROR_EVENT, "cancel_requested")
        for e in (*PREAMBLE, "drive_cancel")
    ],
]


class Clock:
    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def _put_in_status(world: World, cid: str, status: str, **extra: str) -> None:
    if status == "cancel_requested":
        world.cancel({"id": cid})
    elif status != "running":
        world.env.execute("UPDATE campaigns SET status=? WHERE id=?", status, cid)
    if status == "blocked":
        # A stop reason only a person's cancel clears: a blocked campaign with nothing unknown
        # and no reason would otherwise resume on its own and leave the stuck states.
        extra = {"stop_reason": "budget_overrun:synthetic"} | extra
    for column, value in extra.items():
        world.env.execute(f"UPDATE campaigns SET {column}=? WHERE id=?", value, cid)
    assert _campaign(world, cid)["status"] == status


def _inject(monkeypatch, entry: str, error=ValueError) -> list[int]:
    """Make one entry point raise on every call; the returned list counts the calls."""
    calls: list[int] = []

    def raising(*_args, **_kwargs):
        calls.append(1)
        raise error("synthetic")

    if entry in HOOKS:
        monkeypatch.setattr(PROVIDER, entry, raising, raising=False)
    else:
        monkeypatch.setattr(CampaignRunner, RUNNER_ENTRIES[entry], raising)
    return calls


def _counts(world: World) -> dict[str, int]:
    return {name: len(world.events_of(name)) for name in (*BREAKER_EVENTS, HOOK_RUNNER_ERROR_EVENT)}


@pytest.mark.parametrize(
    ("status", "entry", "event", "after"),
    CASES,
    ids=[f"{status}-{entry}" for status, entry, _, _ in CASES],
)
def test_one_breaker_event_per_status_and_entry_point(
    tmp_path, monkeypatch, status, entry, event, after
):
    world = World(tmp_path)
    cid, _ = _reserved(world)
    _put_in_status(world, cid, status)
    before = _money(world, cid)
    calls = _inject(monkeypatch, entry)
    clock = Clock()
    world.runner.now = clock
    stuck = status == "blocked"  # only an already blocked campaign backs off

    for _ in range(RUNNER_ERROR_THRESHOLD - 1):
        world.runner.tick()
    assert [_counts(world)[name] for name in BREAKER_EVENTS] == [0, 0, 0]
    report = world.runner.tick()  # the tenth counted drive
    expected = dict.fromkeys(BREAKER_EVENTS, 0) | {event: 1}
    assert {name: _counts(world)[name] for name in BREAKER_EVENTS} == expected
    assert _campaign(world, cid)["status"] == after
    if status in FORWARD:
        assert f"blocked:{cid}:runner_error:ValueError" in report.actions
        assert _campaign(world, cid)["stop_reason"] == "runner_error:ValueError"
    elif stuck:
        assert f"backing_off:{cid}:ValueError" in report.actions
    else:
        assert f"backing_off:{cid}:ValueError" not in report.actions
    if event == STUCK_RUNNER_ERROR_EVENT:
        (payload,) = [e["payload"] for e in world.events_of(event)]
        assert payload == {
            "campaign_id": cid,
            "status": status,
            "error_class": "ValueError",
            "consecutive": RUNNER_ERROR_THRESHOLD,
            "threshold": RUNNER_ERROR_THRESHOLD,
        }
    if entry in HOOKS:
        assert _counts(world)[HOOK_RUNNER_ERROR_EVENT] == 1

    tripped_calls = len(calls)
    for _ in range(3 * RUNNER_ERROR_THRESHOLD):
        report = world.runner.tick()
        if stuck:
            assert f"backoff:{cid}" in report.actions
        elif status not in FORWARD:
            assert f"backoff:{cid}" not in report.actions
    if stuck:
        assert len(calls) == tripped_calls  # the drive is skipped while it backs off
    elif status not in FORWARD:
        # Never backed off: a cancelling campaign's cancel (and a paused one's upkeep) runs
        # on every pass.
        assert len(calls) >= tripped_calls + 3 * RUNNER_ERROR_THRESHOLD
    assert {name: _counts(world)[name] for name in BREAKER_EVENTS} == expected

    # Past each back-off the drive runs again and the loop is still recorded once.
    for _ in range(4):
        clock.advance(MAX_STUCK_BACKOFF_SECONDS + 1)
        for _ in range(RUNNER_ERROR_THRESHOLD + 2):
            world.runner.tick()
        assert {name: _counts(world)[name] for name in BREAKER_EVENTS} == expected
        if entry in HOOKS:
            assert _counts(world)[HOOK_RUNNER_ERROR_EVENT] == 1
    if stuck:
        assert len(calls) > tripped_calls
    row = _campaign(world, cid)
    assert row["status"] == after
    if entry in PREAMBLE or status not in FORWARD:
        assert _money(world, cid) == before  # nothing held, granted or attempted moved


def test_a_stuck_campaign_whose_block_already_names_the_class_records_no_second_event(
    tmp_path, monkeypatch
):
    world = World(tmp_path)
    cid, before = _reserved(world)
    _put_in_status(world, cid, "blocked", stop_reason="runner_error:ValueError")
    calls = _inject(monkeypatch, "drive_blocked")
    for _ in range(RUNNER_ERROR_THRESHOLD):
        report = world.runner.tick()
    assert f"backing_off:{cid}:ValueError" in report.actions
    assert all(world.events_of(name) == [] for name in BREAKER_EVENTS)
    # A different class under the same block is not named by it: one event of its own.
    calls.clear()
    monkeypatch.undo()
    _inject(monkeypatch, "drive_blocked", KeyError)
    clock = Clock()
    clock.advance(MAX_STUCK_BACKOFF_SECONDS + 1)
    world.runner.now = clock
    for _ in range(RUNNER_ERROR_THRESHOLD):
        world.runner.tick()
    (payload,) = [e["payload"] for e in world.events_of(STUCK_RUNNER_ERROR_EVENT)]
    assert payload["error_class"] == "KeyError"
    assert _money(world, cid) == before


def _trip_blocked(world: World, monkeypatch, clock: Clock):
    cid, before = _reserved(world)
    _put_in_status(world, cid, "blocked")
    world.runner.now = clock
    calls = _inject(monkeypatch, "drive_blocked")
    for _ in range(RUNNER_ERROR_THRESHOLD):
        world.runner.tick()
    assert len(world.events_of(STUCK_RUNNER_ERROR_EVENT)) == 1
    return cid, before, calls


def test_the_backoff_doubles_from_thirty_seconds_and_is_bounded(tmp_path, monkeypatch):
    world = World(tmp_path)
    clock = Clock()
    cid, _, calls = _trip_blocked(world, monkeypatch, clock)
    delay = STUCK_BACKOFF_SECONDS
    for _ in range(8):
        assert world.runner._stuck_backoff[cid][1] == delay
        driven = len(calls)
        clock.advance(delay - 1)
        assert f"backoff:{cid}" in world.runner.tick().actions
        assert len(calls) == driven
        clock.advance(2)
        assert f"backing_off:{cid}:ValueError" in world.runner.tick().actions
        assert len(calls) == driven + 1
        delay = min(delay * 2, MAX_STUCK_BACKOFF_SECONDS)
    assert delay == MAX_STUCK_BACKOFF_SECONDS
    assert len(world.events_of(STUCK_RUNNER_ERROR_EVENT)) == 1


def test_a_change_to_the_campaign_row_lifts_the_backoff(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, _, calls = _trip_blocked(world, monkeypatch, Clock())
    driven = len(calls)
    assert f"backoff:{cid}" in world.runner.tick().actions
    assert len(calls) == driven
    world.env.execute("UPDATE campaigns SET revision=revision+1 WHERE id=?", cid)
    report = world.runner.tick()
    assert f"backoff:{cid}" not in report.actions
    assert len(calls) == driven + 1
    assert len(world.events_of(STUCK_RUNNER_ERROR_EVENT)) == 1


def test_a_status_change_lifts_the_backoff(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, _, calls = _trip_blocked(world, monkeypatch, Clock())
    assert f"backoff:{cid}" in world.runner.tick().actions
    monkeypatch.undo()
    cancel_calls = _inject(monkeypatch, "drive_cancel")
    world.cancel({"id": cid})
    assert _campaign(world, cid)["status"] == "cancel_requested"
    report = world.runner.tick()
    assert f"backoff:{cid}" not in report.actions
    assert len(cancel_calls) == 1  # the new status is driven at once, inside the old back-off


def test_a_clean_drive_clears_the_backoff_and_restarts_the_count(tmp_path, monkeypatch):
    world = World(tmp_path)
    clock = Clock()
    cid, _ = _reserved(world)
    _put_in_status(world, cid, "blocked")
    world.runner.now = clock
    original = CampaignRunner._drive_blocked
    failing = {"on": True}

    def flaky(self, ctx):
        if failing["on"]:
            raise ValueError("synthetic")
        return original(self, ctx)

    monkeypatch.setattr(CampaignRunner, "_drive_blocked", flaky)
    for _ in range(RUNNER_ERROR_THRESHOLD):
        world.runner.tick()
    assert f"backoff:{cid}" in world.runner.tick().actions
    clock.advance(STUCK_BACKOFF_SECONDS + 1)
    failing["on"] = False
    assert f"backoff:{cid}" not in world.runner.tick().actions  # the clean drive
    failing["on"] = True
    # No back-off remains and the streak starts again: the next trip needs a full count.
    for _ in range(RUNNER_ERROR_THRESHOLD - 1):
        actions = world.runner.tick().actions
        assert f"backoff:{cid}" not in actions
        assert f"backing_off:{cid}:ValueError" not in actions
    assert f"backing_off:{cid}:ValueError" in world.runner.tick().actions
    assert len(world.events_of(STUCK_RUNNER_ERROR_EVENT)) == 1


def _two_stage_world(tmp_path) -> tuple[World, str]:
    tmp_path.mkdir(exist_ok=True)
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "a"), stage_spec(world.env, "b")]
    return world, world.campaign(spec)["id"]


def _progress(world: World, cid: str) -> list[tuple[str, int, str]]:
    return [(a["stage_key"], a["attempt"], a["status"]) for a in world.attempts(cid)]


@pytest.mark.parametrize("hook", HOOKS)
def test_a_raising_hook_never_starves_the_sibling_stages(tmp_path, monkeypatch, hook):
    control, control_cid = _two_stage_world(tmp_path / "control")
    world, cid = _two_stage_world(tmp_path / "hooked")
    expected = []  # the stages' progress after each pass with no hook raising
    for _ in range(RUNNER_ERROR_THRESHOLD - 1):
        control.runner.tick()
        expected.append(_progress(control, control_cid))
    calls = _inject(monkeypatch, hook)
    provider_id = stage_provider("local-docker").descriptor.provider_id
    for passes in expected:
        report = world.runner.tick()
        assert f"hook_error:{cid}:{hook}:ValueError" in report.actions
        assert _progress(world, cid) == passes
    assert {key for key, *_ in _progress(world, cid)} == {"a", "b"}
    assert len(calls) >= RUNNER_ERROR_THRESHOLD - 1
    assert _campaign(world, cid)["status"] == "running"
    assert [e["payload"] for e in world.events_of(HOOK_RUNNER_ERROR_EVENT)] == [
        {"campaign_id": cid, "hook": hook, "provider": provider_id, "error_class": "ValueError"}
    ]
    world.runner.tick()
    row = _campaign(world, cid)
    assert (row["status"], row["stop_reason"]) == ("blocked", "runner_error:ValueError")
    assert len(world.events_of(RUNNER_ERROR_EVENT)) == 1
    assert len(world.events_of(HOOK_RUNNER_ERROR_EVENT)) == 1


def test_hook_errors_are_recorded_once_per_hook_and_class(tmp_path, monkeypatch):
    world, cid = _two_stage_world(tmp_path)

    def alternating():
        errors = iter([ValueError, KeyError] * 4 * RUNNER_ERROR_THRESHOLD)

        def raising(*_args, **_kwargs):
            raise next(errors)("synthetic")

        return raising

    monkeypatch.setattr(PROVIDER, "after_tick", alternating())
    monkeypatch.setattr(PROVIDER, "evidence_pass", alternating(), raising=False)
    for _ in range(3 * RUNNER_ERROR_THRESHOLD):
        world.runner.tick()
    seen = sorted(
        (e["payload"]["hook"], e["payload"]["error_class"])
        for e in world.events_of(HOOK_RUNNER_ERROR_EVENT)
    )
    # Every (hook, class) pair that was raised is recorded exactly once, however often it recurs.
    assert seen == [
        ("after_tick", "KeyError"),
        ("after_tick", "ValueError"),
        ("evidence_pass", "KeyError"),
        ("evidence_pass", "ValueError"),
    ]
    # Alternating classes never reach ten consecutive drives of one class.
    assert _campaign(world, cid)["status"] == "running"
