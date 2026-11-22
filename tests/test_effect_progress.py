"""A sent, receipted call is never journaled as ``not_sent`` (review rv-atu2 finding 1).

If ``on_model_call`` stops the loop before a turn's settlement is written, that turn's
pending reservation stands as its ``uncertain`` receipt. ``effect()`` converts it before it
decides whether the boundary progressed, so the prompt (or ``resume.N``) intent is closed as
``acknowledged``: the request was sent and has a receipt. The receipt itself carries the
uncertainty (``outcome_class='uncertain'``, worst-case ``reserved_worst_case_estimate``), as
for any halt between turns; the run is stopped and nothing is resent.

Synthetic only: the e2e fake transport and bridge, a sentinel key, no network.
"""

from test_anthropic_tool_e2e import DAY1, PROMPT, RATE_LIMITED, end_turn, setup, tool_use

WORST_TOKENS = 100 + 1000  # counted input + max_output_tokens_per_call


def halt_before_settling(supervisor):
    """on_model_call's own usage_unknown stop, raised before the settlement is written."""

    def hook(row, call):
        supervisor._halt_in_loop(row["id"], "usage_unknown")

    supervisor.harness.on_model_call = hook


def test_prompt_halted_before_settlement_is_not_recorded_not_sent(tmp_path):
    store, supervisor, transport, bridge, _ = setup(tmp_path, end_turn(), end_turn())
    halt_before_settling(supervisor)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "usage_unknown")
    assert len(transport.billed()) == 1
    assert store.effect("run1", "prompt") == "acknowledged"
    (call,) = store.model_calls("run1", every=True)
    assert (call["turn"], call["status"]) == (1, "settled")
    assert (call["outcome_class"], call["cost_basis"]) == (
        "uncertain",
        "reserved_worst_case_estimate",
    )
    assert call["tokens"]["total"] == WORST_TOKENS
    # Nothing is resent on a later tick.
    supervisor.tick("run1", PROMPT)
    assert len(transport.billed()) == 1 and bridge.calls == []
    assert len(store.model_calls("run1", every=True)) == 1


def test_resumed_turn_halted_before_settlement_is_not_recorded_not_sent(tmp_path):
    # Turn 1 tool_use, turn 2 rate limited (parked); the resumed turn 2 is sent and halted.
    store, supervisor, transport, bridge, clock = setup(
        tmp_path, tool_use(), RATE_LIMITED, end_turn(), end_turn()
    )
    assert supervisor.tick("run1", PROMPT)["state"] == "running"
    assert store.effect("run1", "resume.2") == "not_sent"
    billed, tools = len(transport.billed()), len(bridge.calls)
    clock.now = DAY1 + 30
    halt_before_settling(supervisor)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "usage_unknown")
    assert len(transport.billed()) == billed + 1
    assert store.effect("run1", "resume.2") == "acknowledged"
    # The 429'd first attempt at turn 2 sent nothing billable: its reservation is void.
    assert [c["status"] for c in store.model_calls("run1", every=True)] == [
        "settled",
        "void",
        "settled",
    ]
    first, second = store.model_calls("run1")
    assert (first["turn"], first["status"]) == (1, "settled") and "outcome_class" not in first
    assert (second["turn"], second["status"]) == (2, "settled")
    assert (second["outcome_class"], second["cost_basis"]) == (
        "uncertain",
        "reserved_worst_case_estimate",
    )
    supervisor.tick("run1", PROMPT)
    assert len(transport.billed()) == billed + 1 and len(bridge.calls) == tools
    assert len(store.model_calls("run1", every=True)) == 3


def test_failed_conversion_still_counts_the_sent_call_and_the_next_tick_settles_it(tmp_path):
    store, supervisor, transport, bridge, _ = setup(tmp_path, end_turn(), end_turn())
    halt_before_settling(supervisor)
    convert, calls = store.model_calls_interrupted, []

    def flaky(run_id, now):
        calls.append(run_id)
        if len(calls) == 2:  # the in-effect conversion (the first is the tick's own sweep)
            raise OSError("disk")
        return convert(run_id, now)

    store.model_calls_interrupted = flaky
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "usage_unknown")
    assert store.effect("run1", "prompt") == "acknowledged"
    assert [c["status"] for c in store.model_calls("run1", every=True)] == ["pending"]
    supervisor.tick("run1", PROMPT)
    (call,) = store.model_calls("run1", every=True)
    assert (call["status"], call["outcome_class"]) == ("settled", "uncertain")
    assert len(transport.billed()) == 1 and bridge.calls == []
