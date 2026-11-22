"""Daily caps are checked atomically with the reservation insert (review rv-atu2 finding 2).

Two supervisors sharing one state dir must not both pass the daily-cap check and together
exceed it. The preflight ``budget_check`` stays as an early check; the authoritative check is
``reserve_model_call``, which takes the write lock (``BEGIN IMMEDIATE``), re-reads the UTC
day's ledger (settled and pending rows of every run) and inserts the pending row in the same
transaction, refusing (writing nothing) when either daily cap would be exceeded.

Synthetic only: the e2e fake transport, a sentinel key, no network.
"""

import threading

import pytest
from test_anthropic_tool_e2e import DAY1, PROMPT, RATE_LIMITED, end_turn, setup, tool_use

from tessera_agent.remote_reasoning_store import DailyCapRefused, ReasoningStore

DAY = "2026-11-28"
WORST_TOKENS = 100 + 1000  # counted input + max_output_tokens_per_call
WORST_CENTS = (100 * 300 + 1000 * 1500) / 1e6  # 1.53 cents
TURN1_TOKENS = 150


def other_supervisor_store(tmp_path, run_id="run2"):
    """A second connection to the same state dir, as another supervisor process has."""
    other = ReasoningStore(tmp_path / "state")
    other.bind(run_id, {"model": "m"}, DAY1)
    return other


def reserve_between_preflight_and_reservation(supervisor, reserve):
    """Run ``reserve`` right after this supervisor's preflight passes (before it reserves)."""
    harness = supervisor.harness
    preflight = harness.budget_check
    passed = []

    def budget_check(tokens, cents):
        allowed = preflight(tokens, cents)
        passed.append(allowed)
        if allowed:
            reserve()
        return allowed

    harness.budget_check = budget_check
    return passed


@pytest.mark.parametrize("cap", ["daily_token_cap", "daily_cost_cap_cents"])
def test_reservation_refused_after_another_supervisor_reserved_under_the_cap(tmp_path, cap):
    store, supervisor, transport, _, _ = setup(tmp_path, end_turn())
    limits = supervisor.profile["limits"]
    limits["daily_token_cap"], limits["daily_cost_cap_cents"] = 2 * WORST_TOKENS, 100
    if cap == "daily_cost_cap_cents":
        limits["daily_token_cap"], limits["daily_cost_cap_cents"] = 5000, 3
    other = other_supervisor_store(tmp_path)
    # B's reservation leaves room for less than A's worst case on the capped dimension.
    b_tokens = WORST_TOKENS + 1 if cap == "daily_token_cap" else 10
    b_cents = 0.01 if cap == "daily_token_cap" else 3 - WORST_CENTS + 0.01

    def b_reserves():
        other.reserve_model_call(
            "run2",
            {},
            DAY1,
            tokens_delta=b_tokens,
            cost_cents_delta=b_cents,
        )

    passed = reserve_between_preflight_and_reservation(supervisor, b_reserves)
    row = supervisor.tick("run1", PROMPT)
    assert passed == [True], "A must pass the (early) preflight for the race to exist"
    # A's authoritative reservation saw B's pending row and refused: nothing sent by A.
    assert transport.billed() == []
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "daily_cap_exceeded")
    assert store.effect("run1", "prompt") == "not_sent"
    assert store.model_calls("run1", every=True) == []
    assert (row["tokens"], row["cost"]) == (0, 0.0)
    # The day holds only B's reservation and stays within both caps.
    day = store.daily_usage(DAY)
    assert day == {"tokens": b_tokens, "cost_cents": pytest.approx(b_cents)}
    assert day["tokens"] <= limits["daily_token_cap"]
    assert day["cost_cents"] <= limits["daily_cost_cap_cents"]
    # Not resumed or resent on a later tick.
    supervisor.tick("run1", PROMPT)
    assert transport.billed() == []
    other.close()


def test_turn_two_reservation_refused_halts_without_sending(tmp_path):
    store, supervisor, transport, bridge, _ = setup(tmp_path, tool_use(), end_turn())
    limits = supervisor.profile["limits"]
    limits["daily_token_cap"] = TURN1_TOKENS + 2 * WORST_TOKENS
    other = other_supervisor_store(tmp_path)
    calls = []

    def b_reserves_on_turn_two():
        calls.append(1)
        if len(calls) == 2:  # after turn 1 settled, before turn 2 reserves
            other.reserve_model_call(
                "run2",
                {},
                DAY1,
                tokens_delta=WORST_TOKENS + 1,
                cost_cents_delta=0.0,
            )

    passed = reserve_between_preflight_and_reservation(supervisor, b_reserves_on_turn_two)
    row = supervisor.tick("run1", PROMPT)
    assert passed == [True, True]
    assert len(transport.billed()) == 1, "turn 2 must not be sent"
    assert row["state"] == "stop_acknowledged"
    assert row["reason"] == "daily_cap_exceeded"
    assert [c["status"] for c in store.model_calls("run1", every=True)] == ["settled"]
    assert row["tokens"] == TURN1_TOKENS
    assert store.daily_usage(DAY)["tokens"] == TURN1_TOKENS + WORST_TOKENS + 1
    supervisor.tick("run1", PROMPT)
    assert len(transport.billed()) == 1 and bridge.calls != []  # turn-1 tool ran once only
    other.close()


# -- release order (a failed void keeps the reservation held) -----------------------------


def fail_voids(store):
    store.db.execute(
        "CREATE TEMP TRIGGER fail_void BEFORE UPDATE ON model_calls "
        "WHEN NEW.status='void' BEGIN SELECT RAISE(ABORT, 'injected void failure'); END"
    )


@pytest.mark.parametrize("replies", [(RATE_LIMITED,), (tool_use(), RATE_LIMITED)])
def test_failed_release_keeps_the_reservation_and_never_resends(tmp_path, replies):
    store, supervisor, transport, _, clock = setup(tmp_path, *replies, end_turn())
    fail_voids(store)
    row = supervisor.tick("run1", PROMPT)
    sent = len(transport.billed())
    assert sent == len(replies)
    # The void did not commit: the reservation stands as the uncertain receipt, the step is
    # not journaled as not_sent, and the run halts (outcome unknown), never resumed.
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_unrecorded")
    assert store.effect("run1", "prompt") != "not_sent"
    rows = store.model_calls("run1", every=True)
    assert [c["status"] for c in rows] == ["settled"] * len(replies)
    held = rows[-1]
    assert (held["outcome_class"], held["cost_basis"]) == (
        "uncertain",
        "reserved_worst_case_estimate",
    )
    assert held["tokens"]["total"] == WORST_TOKENS
    assert store.daily_usage(DAY)["tokens"] == row["tokens"]
    assert supervisor._reserved == {}
    clock.now = DAY1 + 60
    supervisor.tick("run1", PROMPT)
    assert len(transport.billed()) == sent
    assert store.daily_usage(DAY)["tokens"] == row["tokens"]


# -- store ---------------------------------------------------------------------------------


def test_store_refusal_writes_nothing_and_normal_reservation_works(tmp_path):
    store = ReasoningStore(tmp_path / "state")
    store.bind("run1", {"model": "m"}, DAY1)
    caps = {"daily_token_cap": 2000, "daily_cost_cap_cents": 3.0}
    seq = store.reserve_model_call(
        "run1", {"cost": 1.5}, DAY1, tokens_delta=1100, cost_cents_delta=1.5, **caps
    )
    (held,) = store.model_calls("run1", every=True)
    assert held["status"] == "pending" and held["seq"] == seq
    assert (held["daily_tokens_after"], held["daily_cost_cents_after"]) == (1100, 1.5)
    for tokens, cents in ((901, 0.1), (10, 1.51)):
        with pytest.raises(DailyCapRefused, match="daily_cap_exceeded"):
            store.reserve_model_call(
                "run1", {}, DAY1, tokens_delta=tokens, cost_cents_delta=cents, **caps
            )
    assert not store.db.in_transaction
    assert len(store.model_calls("run1", every=True)) == 1
    assert store.daily_usage(DAY) == {"tokens": 1100, "cost_cents": 1.5}
    assert (store.get("run1")["tokens"], store.get("run1")["cost"]) == (1100, 1.5)
    # Exactly at the cap is allowed.
    store.reserve_model_call("run1", {}, DAY1, tokens_delta=900, cost_cents_delta=1.5, **caps)
    assert store.daily_usage(DAY) == {"tokens": 2000, "cost_cents": 3.0}
    store.close()


def test_store_connection_waits_for_another_writer(tmp_path):
    store = ReasoningStore(tmp_path / "state")
    (timeout_ms,) = store.db.execute("PRAGMA busy_timeout").fetchone()
    assert timeout_ms >= 5000
    store.close()


def test_two_threads_racing_reservations_never_exceed_the_cap(tmp_path):
    first = ReasoningStore(tmp_path / "state")
    first.bind("run1", {"model": "m"}, DAY1)
    first.bind("run2", {"model": "m"}, DAY1)
    first.close()
    cap_tokens, cap_cents, each = 10_000, 50.0, 700
    barrier = threading.Barrier(2)
    granted, errors = {}, []

    def worker(run_id):
        store = ReasoningStore(tmp_path / "state")
        count = 0
        try:
            barrier.wait()
            for _ in range(40):
                # Early check (preflight) then reservation, like a supervisor tick.
                day = store.daily_usage(DAY)
                if day["tokens"] + each > cap_tokens:
                    continue
                try:
                    store.reserve_model_call(
                        run_id,
                        {},
                        DAY1,
                        tokens_delta=each,
                        cost_cents_delta=1.0,
                        daily_token_cap=cap_tokens,
                        daily_cost_cap_cents=cap_cents,
                    )
                    count += 1
                except DailyCapRefused:
                    pass
        except Exception as error:  # noqa: BLE001 - surfaced below
            errors.append(repr(error))
        finally:
            granted[run_id] = count
            store.close()

    threads = [threading.Thread(target=worker, args=(r,)) for r in ("run1", "run2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    check = ReasoningStore(tmp_path / "state")
    day = check.daily_usage(DAY)
    assert day["tokens"] <= cap_tokens and day["cost_cents"] <= cap_cents
    assert sum(granted.values()) == cap_tokens // each == day["tokens"] // each
    pending = check.db.execute("SELECT COUNT(*) FROM model_calls WHERE status='pending'")
    assert pending.fetchone()[0] == sum(granted.values())
    check.close()
