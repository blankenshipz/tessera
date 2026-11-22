"""Billed-call reservations (ADR 0083 §5; review rv-atu findings 1-2).

Before every billed request (single call, each tool-loop turn, resumed turns) a ``pending``
model_calls row reserves the worst case in the run totals and the UTC-day ledger in one
transaction. The response settles it; a definite rejection voids it; a row left pending by a
dead process becomes a settled ``reserved_worst_case_estimate``/``uncertain`` receipt.

Synthetic only: the e2e fake transport and bridge, a sentinel key, no network.
"""

import sqlite3

import pytest
from test_anthropic_tool_e2e import (
    DAY1,
    PROMPT,
    RATE_LIMITED,
    end_turn,
    setup,
    tool_use,
)

from tessera_agent.remote_reasoning_store import ReasoningStore

DAY = "2026-11-28"
TURN1_TOKENS = 150
TURN1_COST = (100 * 300 + 50 * 1500) / 1e6
WORST_TOKENS = 100 + 1000  # counted input + max_output_tokens_per_call
WORST_CENTS = (100 * 300 + 1000 * 1500) / 1e6  # 1.53 cents


class Killed(BaseException):
    """Stands in for SIGKILL/OOM: nothing in the process handles it."""


def kill():
    raise Killed()


def restart(tmp_path, clock, *replies):
    return setup(tmp_path, *replies, clock=clock, bind=False)


def test_crash_during_turn_two_request_leaves_a_reserved_uncertain_receipt(tmp_path):
    # The reviewer's repro: turn 1 tool_use, the process dies during turn 2's request.
    store, supervisor, transport, _, clock = setup(tmp_path, tool_use(), kill)
    with pytest.raises(Killed):
        supervisor.tick("run1", PROMPT)
    assert len(transport.billed()) == 2
    store.close()
    store, supervisor, fresh, bridge, _ = restart(tmp_path, clock, end_turn())
    row = supervisor.tick("run1", PROMPT)
    # Turn 2 was possibly billed: it has a receipt (asserted before the halt reason).
    assert len(store.model_calls("run1")) == 2, "turn 2 (possibly billed) has no receipt row"
    first, second = store.model_calls("run1")
    assert (first["turn"], first["status"]) == (1, "settled") and "outcome_class" not in first
    assert (second["turn"], second["status"]) == (2, "settled")
    assert (second["outcome_class"], second["cost_basis"]) == (
        "uncertain",
        "reserved_worst_case_estimate",
    )
    assert second["tokens"]["total"] == WORST_TOKENS
    assert second["cost"] == pytest.approx(WORST_CENTS)
    assert second["model_reported"] is None
    # Counted once: turn 1 actual + turn 2 reservation, on the run and on the day.
    day = store.daily_usage(DAY)
    assert day == {
        "tokens": TURN1_TOKENS + WORST_TOKENS,
        "cost_cents": pytest.approx(TURN1_COST + WORST_CENTS),
    }
    assert row["tokens"] == TURN1_TOKENS + WORST_TOKENS
    assert row["cost"] == pytest.approx(TURN1_COST + WORST_CENTS)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_uncertain")
    # Never resent, also on later ticks.
    supervisor.tick("run1", PROMPT)
    assert fresh.sent == [] and bridge.calls == []
    assert store.daily_usage(DAY)["tokens"] == TURN1_TOKENS + WORST_TOKENS


def test_crash_during_a_single_call_prompt_leaves_a_reserved_uncertain_receipt(tmp_path):
    store, supervisor, _, _, clock = setup(tmp_path, kill)
    with pytest.raises(Killed):
        supervisor.tick("run1", PROMPT)
    store.close()
    store, supervisor, fresh, _, _ = restart(tmp_path, clock, end_turn())
    row = supervisor.tick("run1", PROMPT)
    (call,) = store.model_calls("run1")
    assert (call["turn"], call["outcome_class"]) == (1, "uncertain")
    assert call["cost_basis"] == "reserved_worst_case_estimate"
    assert store.daily_usage(DAY) == {
        "tokens": WORST_TOKENS,
        "cost_cents": pytest.approx(WORST_CENTS),
    }
    assert (row["tokens"], row["cost"]) == (WORST_TOKENS, pytest.approx(WORST_CENTS))
    assert fresh.sent == []
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_uncertain")


def test_crash_during_a_resumed_turn_leaves_a_reserved_uncertain_receipt(tmp_path):
    # Park turn 2 on a rate limit, restart, and die during the resumed turn 2.
    store, supervisor, _, _, clock = setup(tmp_path, tool_use(), RATE_LIMITED)
    assert supervisor.tick("run1", PROMPT)["state"] == "running"
    store.close()
    clock.now = DAY1 + 30
    store, supervisor, transport, _, _ = restart(tmp_path, clock, kill)
    with pytest.raises(Killed):
        supervisor.tick("run1", PROMPT)
    assert len(transport.billed()) == 1
    store.close()
    store, supervisor, fresh, _, _ = restart(tmp_path, clock, end_turn())
    row = supervisor.tick("run1", PROMPT)
    first, second = store.model_calls("run1")
    assert (second["turn"], second["outcome_class"]) == (2, "uncertain")
    assert store.daily_usage(DAY)["tokens"] == TURN1_TOKENS + WORST_TOKENS
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_uncertain")
    assert fresh.sent == []


def test_in_flight_reservation_is_durable_and_refuses_the_next_preflight(tmp_path):
    seen = {}

    def in_flight():
        # Another process (a second connection) sees the reservation while turn 2 runs.
        other = ReasoningStore(tmp_path / "state")
        seen["day"] = other.daily_usage(DAY)
        seen["rows"] = [c["status"] for c in other.model_calls("run1", every=True)]
        other.close()
        # turn 1 (150) + turn 2's reservation (1100) + another worst case (1100) > 1500.
        seen["allowed"] = supervisor.budget_check(WORST_TOKENS, WORST_CENTS)
        return end_turn()

    store, supervisor, _, _, _ = setup(tmp_path, tool_use(), in_flight)
    supervisor.profile["limits"]["daily_token_cap"] = 1500
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "completed"
    assert seen["day"] == {
        "tokens": TURN1_TOKENS + WORST_TOKENS,
        "cost_cents": pytest.approx(TURN1_COST + WORST_CENTS),
    }
    assert seen["rows"] == ["settled", "pending"]
    assert seen["allowed"] is False


def test_settlement_replaces_the_reservation_exactly(tmp_path):
    store, supervisor, _, _, _ = setup(tmp_path, tool_use(), end_turn())
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "completed"
    calls = store.model_calls("run1", every=True)
    assert [c["status"] for c in calls] == ["settled", "settled"]
    assert [c["cost_basis"] for c in calls] == ["computed_from_pinned_prices"] * 2
    assert all("outcome_class" not in c for c in calls)
    assert [c["daily_tokens_after"] for c in calls] == [150, 370]
    assert len(calls[1]["output_sha256"]) == 64
    turn2 = (200 * 300 + 20 * 1500) / 1e6
    assert store.daily_usage(DAY) == {
        "tokens": 370,
        "cost_cents": pytest.approx(TURN1_COST + turn2),
    }
    assert (row["tokens"], row["cost"]) == (370, pytest.approx(TURN1_COST + turn2))


def test_rate_limit_voids_the_reservation(tmp_path):
    store, supervisor, transport, _, clock = setup(tmp_path, tool_use(), RATE_LIMITED, end_turn())
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "running"
    # The rejected turn reserved, then released: totals are turn 1's only.
    assert store.daily_usage(DAY) == {
        "tokens": TURN1_TOKENS,
        "cost_cents": pytest.approx(TURN1_COST),
    }
    assert (row["tokens"], row["cost"]) == (TURN1_TOKENS, pytest.approx(TURN1_COST))
    rows = store.model_calls("run1", every=True)
    assert [c["status"] for c in rows] == ["settled", "void"]
    assert [c["turn"] for c in store.model_calls("run1")] == [1]
    clock.now = DAY1 + 30
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "completed"
    assert [c["status"] for c in store.model_calls("run1", every=True)] == [
        "settled",
        "void",
        "settled",
    ]
    assert store.daily_usage(DAY)["tokens"] == 370 and row["tokens"] == 370
    assert len(transport.billed()) == 3


def test_cap_refusal_before_the_request_reserves_nothing(tmp_path):
    store, supervisor, transport, _, _ = setup(tmp_path, end_turn())
    supervisor.profile["limits"]["daily_token_cap"] = WORST_TOKENS - 1
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_call_cap_exceeded"
    assert transport.billed() == []
    assert store.model_calls("run1", every=True) == []
    assert store.daily_usage(DAY) == {"tokens": 0, "cost_cents": 0.0}


# -- store ---------------------------------------------------------------------------------


def bound_store(tmp_path):
    store = ReasoningStore(tmp_path / "state")
    store.bind("run1", {"model": "m"}, DAY1)
    return store


def test_store_settle_and_void_move_totals_by_the_difference(tmp_path):
    store = bound_store(tmp_path)
    seq = store.reserve_model_call(
        "run1", {"cost": 1.5}, DAY1, tokens_delta=1100, cost_cents_delta=1.5
    )
    assert (store.get("run1")["tokens"], store.daily_usage(DAY)["tokens"]) == (1100, 1100)
    store.settle_model_call(
        "run1", seq, {"cost": 0.5}, DAY1, tokens_delta=300, cost_cents_delta=0.5
    )
    assert store.daily_usage(DAY) == {"tokens": 300, "cost_cents": 0.5}
    assert (store.get("run1")["tokens"], store.get("run1")["cost"]) == (300, 0.5)
    # Settling twice (or voiding a settled row) is refused and changes nothing.
    with pytest.raises(ValueError):
        store.settle_model_call("run1", seq, {}, DAY1, tokens_delta=1, cost_cents_delta=0.0)
    with pytest.raises(ValueError):
        store.void_model_call("run1", seq, DAY1)
    second = store.reserve_model_call("run1", {}, DAY1, tokens_delta=1100, cost_cents_delta=1.5)
    store.void_model_call("run1", second, DAY1)
    assert store.daily_usage(DAY) == {"tokens": 300, "cost_cents": 0.5}
    assert store.get("run1")["tokens"] == 300
    assert [c["status"] for c in store.model_calls("run1", every=True)] == ["settled", "void"]
    assert len(store.model_calls("run1")) == 1


def test_store_settle_failure_rolls_back_and_keeps_the_reservation(tmp_path):
    store = bound_store(tmp_path)
    seq = store.reserve_model_call("run1", {}, DAY1, tokens_delta=1100, cost_cents_delta=1.5)
    store.db.execute(
        "CREATE TEMP TRIGGER fail_settle BEFORE UPDATE ON model_calls "
        "BEGIN SELECT RAISE(ABORT, 'injected settle failure'); END"
    )
    with pytest.raises(sqlite3.DatabaseError, match="injected settle failure"):
        store.settle_model_call("run1", seq, {}, DAY1, tokens_delta=300, cost_cents_delta=0.5)
    # The run-total update in the same transaction rolled back with the failed row update.
    assert (store.get("run1")["tokens"], store.get("run1")["cost"]) == (1100, 1.5)
    assert store.daily_usage(DAY) == {"tokens": 1100, "cost_cents": 1.5}
    (held,) = store.model_calls("run1", every=True)
    assert held["status"] == "pending"


def test_store_interrupted_reservation_settles_without_double_counting(tmp_path):
    store = bound_store(tmp_path)
    store.reserve_model_call(
        "run1",
        {"cost": 1.5, "cost_basis": "reserved_worst_case_estimate", "outcome_class": "uncertain"},
        DAY1,
        tokens_delta=1100,
        cost_cents_delta=1.5,
    )
    assert store.model_calls("run1") == []
    assert store.model_calls_interrupted("run1", DAY1 + 5) == 1
    assert store.model_calls_interrupted("run1", DAY1 + 6) == 0
    (call,) = store.model_calls("run1")
    assert (call["status"], call["outcome_class"]) == ("settled", "uncertain")
    assert store.daily_usage(DAY) == {"tokens": 1100, "cost_cents": 1.5}
    assert store.get("run1")["tokens"] == 1100


def test_old_state_dir_rows_migrate_as_settled(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "reasoning.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE model_calls (
        run_id TEXT NOT NULL, seq INTEGER NOT NULL, receipt_json TEXT NOT NULL,
        observed_at REAL NOT NULL, day TEXT NOT NULL,
        tokens_delta INTEGER NOT NULL DEFAULT 0, cost_cents_delta REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(run_id,seq));
      INSERT INTO model_calls VALUES('run1',1,'{"cost":0.2}',1795860000.0,'2026-11-28',220,0.2);
    """)
    db.commit()
    db.close()
    path.chmod(0o600)
    store = ReasoningStore(state)
    (call,) = store.model_calls("run1")
    assert (call["status"], call["cost"]) == ("settled", 0.2)
    assert store.daily_usage(DAY) == {"tokens": 220, "cost_cents": 0.2}
    assert store.model_calls_interrupted("run1", DAY1) == 0
