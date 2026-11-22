import itertools
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pytest

from tessera_agent.standing_loop import (
    ApiKeyPresent,
    CallNotStarted,
    InMemoryCallLedger,
    KillSwitch,
    LedgerInUse,
    LedgerNotDurable,
    LoopPolicy,
    QuotaExceeded,
    SqliteCallLedger,
    StandingLoop,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


_ledger_ids = itertools.count()


def make(tmp_path, items, start, **over):
    ledger = over.pop("ledger", None)
    if ledger is None:
        ledger = SqliteCallLedger(tmp_path / f"ledger-{next(_ledger_ids)}.sqlite")
    clock = over.pop("clock", None) or Clock()
    args = dict(
        policy_digest="sha256:abc",
        identity_ref="cred-ref-1",
        model="m1",
        tier="t1",
        max_calls_per_hour=100,
        max_consecutive_failures=3,
        base_backoff_seconds=10,
        max_backoff_seconds=60,
    )
    args.update(over)
    events = []
    marker = tmp_path / "kill"
    loop = StandingLoop(
        LoopPolicy(**args),
        KillSwitch(marker),
        lambda: list(items),
        start,
        events.append,
        clock,
        environ={},
        ledger=ledger,
    )
    return loop, events, clock, marker


def reasons(events):
    return [e.get("reason") for e in events if e["event"] == "skipped"]


def test_policy_validation():
    base = dict(
        policy_digest="d", identity_ref="i", model="m", tier="t",
        max_calls_per_hour=1, max_consecutive_failures=1,
        base_backoff_seconds=1, max_backoff_seconds=2,
    )  # fmt: skip
    LoopPolicy(**base)
    for key, bad in [("model", ""), ("max_calls_per_hour", 0), ("base_backoff_seconds", -1)]:
        with pytest.raises(ValueError):
            LoopPolicy(**{**base, key: bad})


def test_kill_switch_blocks_and_does_not_touch_inflight(tmp_path):
    calls = []
    loop, events, _, marker = make(
        tmp_path, [{"id": "a", "kind": "k"}], lambda i, **k: calls.append(i)
    )
    marker.write_text("")
    out = loop.tick()
    assert out["skipped"] == "kill_switch" and calls == []
    assert [e["event"] for e in events] == ["kill_switch", "skipped"]
    marker.unlink()
    assert loop.tick()["started"] == "a"


def test_marker_fails_closed(tmp_path):
    assert KillSwitch(None).engaged()
    assert KillSwitch(tmp_path / "missing-dir" / "kill").engaged()
    assert not KillSwitch(tmp_path / "kill").engaged()
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert KillSwitch(blocker / "kill").engaged()  # parent is not a directory
    assert KillSwitch(Path(tmp_path) / ("x" * 5000)).engaged()  # unreadable (name too long)


def test_quota_backoff_exponential_cap_retry_after_no_downshift(tmp_path):
    seen_kwargs = []
    outcomes = [
        QuotaExceeded(),
        QuotaExceeded(),
        QuotaExceeded(),
        QuotaExceeded(),
        QuotaExceeded(500),
        None,
    ]

    def start(item, **kw):
        seen_kwargs.append(kw)
        out = outcomes.pop(0)
        if out:
            raise out

    loop, events, clock, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    delays = []
    for _ in range(5):
        res = loop.tick()
        assert res["state"] == "backing_off"
        until = [e for e in events if e["event"] == "backoff"][-1]["until"]
        delays.append(until - clock.t)
        clock.t += 1
        assert loop.tick()["skipped"] == "backoff"
        clock.t = until
    assert delays == [10, 20, 40, 60, 500]
    assert loop.tick()["started"] == "a"
    assert len(seen_kwargs) == 6 and all(k == seen_kwargs[0] for k in seen_kwargs)
    assert seen_kwargs[0] == dict(
        model="m1", tier="t1", identity_ref="cred-ref-1", policy_digest="sha256:abc"
    )


def test_mismatch_skipped_never_downshift(tmp_path):
    calls = []
    items = [
        {"id": "a", "kind": "k", "tier": "cheap"},
        {"id": "b", "kind": "k", "identity_ref": "other"},
    ]
    loop, events, _, _ = make(tmp_path, items, lambda i, **k: calls.append(i))
    out = loop.tick()
    assert calls == [] and out["skipped"] == "identity_or_tier_mismatch"
    assert reasons(events) == ["identity_or_tier_mismatch"] * 2


def test_rate_cap_rolling_hour(tmp_path):
    items = [{"id": str(n), "kind": "k"} for n in range(5)]
    loop, events, clock, _ = make(tmp_path, items, lambda i, **k: None, max_calls_per_hour=2)
    assert loop.tick()["started"] == "0"
    clock.t += 10
    assert loop.tick()["started"] == "1"
    assert loop.tick()["skipped"] == "rate_cap"
    clock.t += 3600 - 10
    assert loop.tick()["started"] == "2"


def test_halt_after_failures_and_reset(tmp_path):
    fail = [True]

    def start(item, **kw):
        if fail[0]:
            raise CallNotStarted("boom secret-ish detail")

    loop, events, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    states = [loop.tick()["state"] for _ in range(3)]
    assert states == ["idle", "idle", "halted"]
    assert loop.tick()["skipped"] == "halted"
    assert not any("boom" in str(e) for e in events)
    fail[0] = False
    loop.reset()
    assert loop.tick()["started"] == "a"


def test_idempotent_ids_and_seen_store(tmp_path):
    calls = []
    loop, _, _, _ = make(
        tmp_path,
        [{"id": "a", "kind": "k"}, {"id": "b", "kind": "k"}],
        lambda i, **k: calls.append(i["id"]),
    )
    assert loop.tick()["started"] == "a"
    assert loop.tick()["started"] == "b"
    assert loop.tick()["skipped"] == "no_work"
    assert calls == ["a", "b"]

    class Seen:
        def __init__(self):
            self.ids = {"a"}

        def has(self, i):
            return i in self.ids

        def add(self, i):
            self.ids.add(i)

    store = Seen()
    calls2 = []
    loop2, _, _, _ = make(
        tmp_path,
        [{"id": "a", "kind": "k"}, {"id": "c", "kind": "k"}],
        lambda i, **k: calls2.append(i["id"]),
    )
    loop2.seen = store
    assert loop2.tick()["started"] == "c" and calls2 == ["c"] and "c" in store.ids


def test_events_have_only_allowed_keys(tmp_path):
    items = [{"id": "a", "kind": "k", "prompt": "SECRET PROMPT", "token": "tok"}]
    loop, events, _, marker = make(tmp_path, items, lambda i, **k: None)
    loop.tick()
    marker.write_text("")
    loop.tick()
    allowed = {"event", "reason", "item_id", "kind", "model", "tier", "identity_ref",
               "policy_digest", "outcome", "error", "until", "engaged"}  # fmt: skip
    for e in events:
        assert set(e) <= allowed
    assert "SECRET" not in str(events) and "tok" not in str(events).replace("token", "")


def test_api_key_blocks_construction_and_tick(tmp_path):
    calls = []
    loop, events, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], lambda i, **k: calls.append(i))
    for env in ({"ANTHROPIC_API_KEY": "sk-secret"}, {"ANTHROPIC_API_KEY": ""}):
        with pytest.raises(ApiKeyPresent) as exc:
            StandingLoop(loop.policy, loop.kill_switch, loop.observe, loop.start_call,
                         events.append, loop.clock, environ=env,
                         ledger=loop.ledger)  # fmt: skip
        assert "sk-secret" not in str(exc.value)
    env = {}
    live = StandingLoop(loop.policy, loop.kill_switch, loop.observe, loop.start_call,
                        events.append, loop.clock, environ=env,
                        ledger=loop.ledger)  # fmt: skip
    env["ANTHROPIC_API_KEY"] = "sk-secret"
    assert live.tick()["skipped"] == "api_key_present"
    assert calls == [] and "sk-secret" not in str(events)


def make_with_ledger(tmp_path, items, start, **over):
    loop, events, clock, _ = make(tmp_path, items, start, **over)
    return loop, events, clock, loop.ledger


def test_crash_between_begin_and_finish_not_restarted(tmp_path):
    ledger = SqliteCallLedger(tmp_path / "ledger.sqlite")
    assert ledger.begin("a") is True and ledger.begin("a") is False
    assert ledger.state("a") == "started"  # simulated crash: no finish
    ledger.close()
    calls = []
    loop, events, _, _ = make(
        tmp_path, [{"id": "a", "kind": "k"}, {"id": "b", "kind": "k"}],
        lambda i, **k: calls.append(i["id"]), ledger=SqliteCallLedger(tmp_path / "ledger.sqlite"),
    )  # fmt: skip
    out = loop.tick()
    assert out["started"] is None and out["uncertain"] == ["a"] and calls == []
    assert [e for e in events if e["event"] == "uncertain_outcome"] == [
        {"event": "uncertain_outcome", "item_id": "a"}
    ]
    assert loop.ledger.state("a") == "uncertain"
    assert loop.tick()["started"] == "b" and calls == ["b"]  # others proceed once settled
    assert loop.tick()["skipped"] == "no_work" and calls == ["b"]


def test_unknown_exception_is_uncertain_not_retried(tmp_path):
    calls = []

    def start(item, **kw):
        calls.append(item["id"])
        if item["id"] == "a":
            raise RuntimeError("timeout after send")

    loop, events, _, ledger = make_with_ledger(
        tmp_path, [{"id": "a", "kind": "k"}, {"id": "b", "kind": "k"}], start
    )
    out = loop.tick()
    assert out["started"] == "a" and out["uncertain"] == ["a"]
    assert ledger.state("a") == "uncertain"
    assert loop.tick()["started"] == "b"
    assert loop.tick()["skipped"] == "no_work"
    assert calls == ["a", "b"]
    assert [e["event"] for e in events].count("uncertain_outcome") == 1
    assert not any("timeout" in str(e) for e in events)


def test_no_new_call_while_started_exists(tmp_path):
    loop, _, _, ledger = make_with_ledger(
        tmp_path, [{"id": "b", "kind": "k"}, {"id": "a", "kind": "k"}], lambda i, **k: None
    )
    ledger.begin("a")
    out = loop.tick()
    assert out["started"] is None and out["skipped"] == "uncertain_outcome"
    assert ledger.state("b") is None


def test_call_not_started_retries(tmp_path):
    outcomes = [CallNotStarted(), None]

    def start(item, **kw):
        out = outcomes.pop(0)
        if out:
            raise out

    loop, _, _, ledger = make_with_ledger(tmp_path, [{"id": "a", "kind": "k"}], start)
    out = loop.tick()
    assert out["started"] == "a" and out["uncertain"] == []
    assert ledger.state("a") == "failed_before_start"
    assert loop.tick()["started"] == "a" and ledger.state("a") == "succeeded"


def test_quota_marks_failed_before_start_and_retries_after_backoff(tmp_path):
    outcomes = [QuotaExceeded(), None]

    def start(item, **kw):
        out = outcomes.pop(0)
        if out:
            raise out

    loop, _, clock, ledger = make_with_ledger(tmp_path, [{"id": "a", "kind": "k"}], start)
    assert loop.tick()["state"] == "backing_off"
    assert ledger.state("a") == "failed_before_start"
    assert loop.tick()["skipped"] == "backoff"
    clock.t += 10
    assert loop.tick()["started"] == "a" and ledger.state("a") == "succeeded"


def test_resolve_uncertain_paths(tmp_path):
    calls = []

    def start(item, **kw):
        calls.append(item["id"])
        if len(calls) <= 2:
            raise RuntimeError("x")

    items = [{"id": "a", "kind": "k"}, {"id": "b", "kind": "k"}]
    loop, events, _, ledger = make_with_ledger(tmp_path, items, start)
    loop.tick()
    loop.tick()
    assert loop.tick()["uncertain"] == ["a", "b"] and calls == ["a", "b"]
    with pytest.raises(ValueError):
        loop.resolve_uncertain("a", "bogus")
    with pytest.raises(ValueError):
        loop.resolve_uncertain("zzz", "succeeded")
    loop.resolve_uncertain("a", "succeeded")
    loop.resolve_uncertain("b", "not_started")
    assert ledger.state("a") == "succeeded" and ledger.state("b") == "failed_before_start"
    out = loop.tick()
    assert out["started"] == "b" and out["uncertain"] == [] and calls == ["a", "b", "b"]
    assert loop.tick()["skipped"] == "no_work"


def test_journal_dedupes_gated_states(tmp_path):
    loop, events, clock, marker = make(tmp_path, [{"id": "a", "kind": "k"}], lambda i, **k: None)
    marker.write_text("")
    for _ in range(5):
        assert loop.tick()["skipped"] == "kill_switch"
    assert [e["event"] for e in events] == ["kill_switch", "skipped"]
    marker.unlink()
    assert loop.tick()["started"] == "a"
    marker.write_text("")
    loop.tick()
    assert reasons(events) == ["kill_switch", "kill_switch"]
    marker.unlink()

    def quota(item, **kw):
        raise QuotaExceeded()

    loop2, events2, clock2, _ = make(tmp_path, [{"id": "a", "kind": "k"}], quota)
    loop2.tick()
    for _ in range(4):
        assert loop2.tick()["skipped"] == "backoff"
    assert reasons(events2) == ["backoff"]


def test_halted_journals_once(tmp_path):
    def start(item, **kw):
        raise CallNotStarted()

    loop, events, _, _ = make(
        tmp_path, [{"id": "a", "kind": "k"}], start, max_consecutive_failures=1
    )
    loop.tick()
    for _ in range(4):
        assert loop.tick()["skipped"] == "halted"
    assert reasons(events) == ["halted"]


def test_ledger_rows_hold_no_secrets(tmp_path):
    items = [{"id": "a", "kind": "k", "prompt": "SECRET PROMPT", "token": "tok-123"}]

    def start(item, **kw):
        raise RuntimeError("sk-leaky detail")

    loop, _, _, ledger = make_with_ledger(tmp_path, items, start)
    loop.tick()
    with sqlite3.connect(ledger.path) as conn:
        rows = conn.execute("SELECT * FROM calls").fetchall()
        cols = [c[1] for c in conn.execute("PRAGMA table_info(calls)")]
    assert cols == ["item_id", "state", "created_at", "updated_at"]
    assert rows[0][:2] == ("a", "uncertain")
    raw = Path(ledger.path).read_bytes()
    assert b"SECRET" not in raw and b"tok-123" not in raw and b"sk-leaky" not in raw
    assert Path(ledger.path).stat().st_mode & 0o777 == 0o600


_shared: dict = {}


def _loop_over(tmp_path, items, start, clock, events=None, **over):
    """A fresh loop instance over the same sqlite file (simulated restart).

    The previous instance is closed first (the ledger lock is exclusive).
    """
    prev = _shared.pop(tmp_path, None)
    if prev is not None:
        prev.close()
    loop = make(
        tmp_path, items, start, clock=clock,
        ledger=SqliteCallLedger(tmp_path / "shared.sqlite"), **over,
    )  # fmt: skip
    _shared[tmp_path] = loop[0].ledger
    return loop


def test_ledger_must_be_durable(tmp_path):
    loop, events, clock, _ = make(tmp_path, [], lambda i, **k: None)
    args = (loop.policy, loop.kill_switch, loop.observe, loop.start_call, events.append, clock)
    with pytest.raises(LedgerNotDurable):
        StandingLoop(*args, environ={})
    with pytest.raises(LedgerNotDurable):
        StandingLoop(*args, environ={}, ledger=InMemoryCallLedger())
    assert issubclass(LedgerNotDurable, ValueError)
    assert SqliteCallLedger.durable is True and InMemoryCallLedger.durable is False
    StandingLoop(*args, environ={}, ledger=InMemoryCallLedger(), allow_volatile_ledger=True)
    StandingLoop(*args, environ={}, allow_volatile_ledger=True)


def test_rate_window_survives_restart(tmp_path):
    items = [{"id": str(n), "kind": "k"} for n in range(6)]
    clock = Clock()
    loop, _, _, _ = _loop_over(tmp_path, items, lambda i, **k: None, clock, max_calls_per_hour=2)
    assert loop.tick()["started"] == "0" and loop.tick()["started"] == "1"
    loop2, _, _, _ = _loop_over(tmp_path, items, lambda i, **k: None, clock, max_calls_per_hour=2)
    assert loop2.tick()["skipped"] == "rate_cap"
    clock.t += 3600
    assert loop2.tick()["started"] == "2"


def test_halted_survives_restart_until_reset(tmp_path):
    def fail(item, **kw):
        raise CallNotStarted()

    items = [{"id": "a", "kind": "k"}]
    clock = Clock()
    loop, _, _, _ = _loop_over(tmp_path, items, fail, clock, max_consecutive_failures=2)
    loop.tick()
    assert loop.tick()["state"] == "halted"
    calls = []
    loop2, _, _, _ = _loop_over(
        tmp_path, items, lambda i, **k: calls.append(i), clock, max_consecutive_failures=2
    )
    assert loop2.state == "halted"
    assert loop2.tick()["skipped"] == "halted" and calls == []
    loop2.reset()
    assert loop2.ledger.loop_state_get("halted") == "0"
    loop3, _, _, _ = _loop_over(tmp_path, items, lambda i, **k: calls.append(i), clock)
    assert loop3.tick()["started"] == "a" and [c["id"] for c in calls] == ["a"]


def test_failure_count_survives_restart(tmp_path):
    def fail(item, **kw):
        raise CallNotStarted()

    items = [{"id": "a", "kind": "k"}]
    clock = Clock()
    loop, _, _, _ = _loop_over(tmp_path, items, fail, clock, max_consecutive_failures=2)
    assert loop.tick()["state"] == "idle"
    loop2, _, _, _ = _loop_over(tmp_path, items, fail, clock, max_consecutive_failures=2)
    assert loop2.tick()["state"] == "halted"


def test_backoff_survives_restart(tmp_path):
    outcomes = [QuotaExceeded(), QuotaExceeded(), None]

    def start(item, **kw):
        out = outcomes.pop(0)
        if out:
            raise out

    items = [{"id": "a", "kind": "k"}]
    clock = Clock()
    loop, events, _, _ = _loop_over(tmp_path, items, start, clock)
    assert loop.tick()["state"] == "backing_off"
    loop2, events2, _, _ = _loop_over(tmp_path, items, start, clock)
    assert loop2.tick()["skipped"] == "backoff" and len(outcomes) == 2
    clock.t += 10
    assert loop2.tick()["state"] == "backing_off"  # backoff_n persisted: next delay doubles
    assert [e for e in events2 if e["event"] == "backoff"][-1]["until"] == clock.t + 20
    clock.t += 20
    assert loop2.tick()["started"] == "a"


def test_any_started_row_blocks_even_if_unobserved(tmp_path):
    calls = []
    loop, events, _, ledger = make_with_ledger(
        tmp_path, [{"id": "b", "kind": "k"}], lambda i, **k: calls.append(i["id"])
    )
    ledger.begin("ghost")  # crash leftover, no longer returned by observe()
    assert ledger.started_items() == ["ghost"]
    out = loop.tick()
    assert out["started"] is None and out["skipped"] == "uncertain_outcome"
    assert out["uncertain"] == ["ghost"] and ledger.state("ghost") == "uncertain"
    assert ledger.state("b") is None and calls == []
    assert loop.tick()["started"] == "b"


def test_in_memory_ledger_protocol(tmp_path):
    ledger = InMemoryCallLedger()
    assert ledger.begin("a", 5.0) and ledger.starts_since(4.0) == [5.0]
    assert ledger.starts_since(5.0) == [] and ledger.started_items() == ["a"]
    assert ledger.loop_state_get("k") is None
    ledger.loop_state_set("k", "v")
    assert ledger.loop_state_get("k") == "v"


def test_rate_cap_journals_once_and_rearms(tmp_path):
    items = [{"id": str(n), "kind": "k"} for n in range(5)]
    loop, events, clock, _ = make(tmp_path, items, lambda i, **k: None, max_calls_per_hour=1)
    assert loop.tick()["started"] == "0"
    for _ in range(4):
        assert loop.tick()["skipped"] == "rate_cap"
    assert reasons(events) == ["rate_cap"]
    clock.t += 3600
    assert loop.tick()["started"] == "1"
    for _ in range(3):
        assert loop.tick()["skipped"] == "rate_cap"
    assert reasons(events) == ["rate_cap", "rate_cap"]


def test_second_ledger_on_same_path_refused_until_close(tmp_path):
    path = tmp_path / "l.sqlite"
    first = SqliteCallLedger(path)
    with pytest.raises(LedgerInUse):
        SqliteCallLedger(path)
    first.begin("a")
    first.close()
    first.close()  # idempotent
    with SqliteCallLedger(path) as second:
        assert second.started_items() == ["a"]
        with pytest.raises(LedgerInUse):
            SqliteCallLedger(path)
        loop, events, _, _ = make(tmp_path, [], lambda i, **k: None, ledger=second)
        assert loop.tick()["uncertain"] == ["a"]
    assert os.stat(str(path) + ".lock").st_mode & 0o777 == 0o600
    SqliteCallLedger(path).close()


def test_lock_released_on_garbage_collection(tmp_path):
    import gc

    path = tmp_path / "gc.sqlite"
    SqliteCallLedger(path)
    gc.collect()
    SqliteCallLedger(path).close()


def test_save_state_is_atomic_in_ledger(tmp_path):
    class Boom(Mapping):
        def __getitem__(self, key):
            raise KeyError(key)

        def __len__(self):
            return 2

        def __iter__(self):
            yield "consecutive_failures"
            raise RuntimeError("mid-transaction")

        def items(self):
            yield "consecutive_failures", "9"
            raise RuntimeError("mid-transaction")

    for ledger in (SqliteCallLedger(tmp_path / "a.sqlite"), InMemoryCallLedger()):
        ledger.loop_state_set_many({"consecutive_failures": "1", "halted": "0"})
        assert ledger.loop_state_get("consecutive_failures") == "1"
        with pytest.raises(RuntimeError):
            ledger.loop_state_set_many(Boom())
        assert ledger.loop_state_get("consecutive_failures") == "1"
        assert ledger.loop_state_get("halted") == "0"
        if isinstance(ledger, SqliteCallLedger):
            with pytest.raises(sqlite3.IntegrityError):
                ledger.loop_state_set_many({"halted": "1", "bad": None})
            assert ledger.loop_state_get("halted") == "0"


def test_loop_save_state_uses_single_transaction(tmp_path):
    def fail(item, **kw):
        raise CallNotStarted()

    loop, events, _, ledger = make_with_ledger(
        tmp_path, [{"id": "a", "kind": "k"}], fail, max_consecutive_failures=1
    )
    many = []
    orig = ledger.loop_state_set_many
    ledger.loop_state_set_many = lambda m: (many.append(dict(m)), orig(m))[1]
    loop.tick()
    assert many[-1]["halted"] == "1" and many[-1]["consecutive_failures"] == "1"
    assert ledger.loop_state_get("halted") == "1"


def test_existing_group_or_other_accessible_file_refused(tmp_path):
    path = tmp_path / "open.sqlite"
    SqliteCallLedger(path).close()
    os.chmod(path, 0o640)
    with pytest.raises(ValueError):
        SqliteCallLedger(path)
    assert os.stat(path).st_mode & 0o777 == 0o640  # not silently chmodded
    os.chmod(path, 0o600)
    SqliteCallLedger(path).close()
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_prune_starts_keeps_hourly_window(tmp_path):
    ledger = SqliteCallLedger(tmp_path / "p.sqlite")
    for ts in (1.0, 50.0, 100.0):
        ledger.begin(f"i{ts}", ts)
    ledger.prune_starts(50.0)
    assert ledger.starts_since(0.0) == [50.0, 100.0]
    assert ledger.state("i1.0") == "started"  # calls rows retained for audit
    mem = InMemoryCallLedger()
    mem.begin("x", 1.0)
    mem.begin("y", 60.0)
    mem.prune_starts(50.0)
    assert mem.starts_since(0.0) == [60.0]


def test_loop_prunes_and_window_still_works(tmp_path):
    items = [{"id": str(n), "kind": "k"} for n in range(6)]
    loop, _, clock, ledger = make_with_ledger(
        tmp_path, items, lambda i, **k: None, max_calls_per_hour=2
    )
    ledger.begin("old", 1.0)
    ledger.finish("old", "succeeded")
    clock.t = 10000.0
    assert loop.tick()["started"] == "0"
    assert ledger.starts_since(0.0) == [10000.0]  # old start pruned
    assert loop.tick()["started"] == "1"
    assert loop.tick()["skipped"] == "rate_cap"
    clock.t += 3600
    assert loop.tick()["started"] == "2"
