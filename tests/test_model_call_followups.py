"""Review rv-rmd4 follow-ups 1-3 for the direct Anthropic Messages API runtime.

1. A wrong-model reply is charged the worst case: ``max(actual at pinned prices, reserved
   worst case)`` for cost and tokens, ``cost_basis: worst_case_for_substituted_model``.
2. A 200 whose body is not a usable message is ``model_call_uncertain`` (reserved worst
   case), never a substitution.
3. The run's cumulative usage, the receipt row and the daily ledger are one transaction.

Synthetic only: fake transport, sentinel key in a private temporary file, no network.
"""

import json
import sqlite3

import pytest
from test_anthropic_runtime_integration import (  # noqa: F401 - creds is a fixture
    COMPLETION,
    MODEL,
    PROMPT,
    WORST_CENTS,
    WORST_TOKENS,
    Transport,
    assert_no_secrets,
    creds,
    ok,
    setup,
)

from tessera_agent.anthropic_messages import AnthropicMessagesReasoning
from tessera_agent.remote_reasoning_runtime import RuntimeBlocked
from tessera_agent.remote_reasoning_store import ReasoningStore

DAY = "2026-11-22"


def billed_call(creds, reply):  # noqa: F811
    from test_anthropic_runtime_integration import make_profile

    adapter = AnthropicMessagesReasoning(
        make_profile(), credentials_dir=creds, transport=Transport(reply)
    )
    with pytest.raises(RuntimeBlocked) as caught:
        adapter.prompt({"id": "run1"}, PROMPT)
    return str(caught.value), caught.value.billed_call


# -- 1. wrong-model reply: charged the worst case ----------------------------------------------


def test_substitution_below_the_reservation_charges_the_reservation(tmp_path, creds):  # noqa: F811
    # 100 in / 50 out at pinned prices is far below the 100 + 1000 reserved worst case.
    transport = Transport(ok(model="claude-opus-4-1", input_tokens=100, output_tokens=50))
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_substituted")
    (call,) = store.model_calls("run1")
    assert call["outcome_class"] == "model_substituted"
    assert call["cost_basis"] == "worst_case_for_substituted_model"
    assert call["model_reported"] == "anthropic/claude-opus-4-1"
    assert call["model_configured"] == "anthropic/" + MODEL
    # The reported breakdown is kept; the charged total is the reservation.
    assert (call["tokens"]["input"], call["tokens"]["output"]) == (100, 50)
    assert call["tokens"]["total"] == WORST_TOKENS
    assert call["cost"] == pytest.approx(WORST_CENTS)
    assert call["cost_cents_estimate_max"] == pytest.approx(WORST_CENTS)
    day = store.daily_usage(DAY)
    assert day == {"tokens": WORST_TOKENS, "cost_cents": pytest.approx(WORST_CENTS)}
    run = store.get("run1")
    assert (run["tokens"], run["cost"]) == (WORST_TOKENS, pytest.approx(WORST_CENTS))
    assert_no_secrets(store, tmp_path)


def test_substitution_above_the_reservation_charges_the_actual(tmp_path, creds):  # noqa: F811
    # A reply larger than the reserved worst case (as a misbehaving upstream might report).
    transport = Transport(ok(model="claude-opus-4-1", input_tokens=400, output_tokens=1200))
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    assert supervisor.tick("run1", PROMPT)["reason"] == "model_substituted"
    (call,) = store.model_calls("run1")
    actual_cents = (400 * 300 + 1200 * 1500) / 1e6
    assert actual_cents > WORST_CENTS
    assert call["cost_basis"] == "worst_case_for_substituted_model"
    assert call["tokens"]["total"] == 1600
    assert call["cost"] == pytest.approx(actual_cents)
    assert store.daily_usage(DAY) == {"tokens": 1600, "cost_cents": pytest.approx(actual_cents)}
    assert store.get("run1")["cost"] == pytest.approx(actual_cents)


def test_adapter_substitution_billed_call_is_the_worst_case(creds):  # noqa: F811
    code, billed = billed_call(creds, ok(model="claude-haiku-4-5", output_tokens=900))
    assert code == "model_substituted"
    assert billed["outcome_class"] == "model_substituted"
    assert billed["cost_basis"] == "worst_case_for_substituted_model"
    assert billed["usage"] == {
        "input": 100,
        "output": 900,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
    }
    assert billed["tokens"] == WORST_TOKENS
    assert billed["cost"] == pytest.approx(WORST_CENTS)
    assert billed["reported_model"] == "anthropic/claude-haiku-4-5"


def test_invalid_model_identifier_is_substituted_but_never_echoed(creds):  # noqa: F811
    code, billed = billed_call(creds, ok(model="Not A Model Id!"))
    assert code == "model_substituted"
    assert billed["reported_model"] is None
    assert billed["cost_basis"] == "worst_case_for_substituted_model"


# -- 2. unusable 200 body: uncertain, never a substitution -------------------------------------


USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}
CONTENT = [{"type": "text", "text": COMPLETION}]


@pytest.mark.parametrize(
    "body",
    [
        {},  # what the transport returns for an empty or unparseable body
        [],
        "not a message",
        None,
        {"type": "error"},
        {"model": "claude-haiku-4-5"},  # model alone is not a usable message
        {"model": "claude-haiku-4-5", "content": []},  # no usage
        {"model": "claude-haiku-4-5", "usage": USAGE},  # no content
        {"content": CONTENT, "usage": USAGE},  # no model
        {"model": None, "content": CONTENT, "usage": USAGE},
        {"model": 7, "content": CONTENT, "usage": USAGE},
    ],
)
def test_unusable_200_body_is_uncertain_not_substituted(tmp_path, creds, body):  # noqa: F811
    code, billed = billed_call(creds, (200, {}, body))
    assert code == "model_call_uncertain"
    assert billed["outcome_class"] == "uncertain"
    assert billed["cost_basis"] == "reserved_worst_case_estimate"
    assert billed["reported_model"] is None
    assert (billed["tokens"], billed["cost"]) == (WORST_TOKENS, pytest.approx(WORST_CENTS))


def test_empty_200_body_halts_uncertain_with_reservation(tmp_path, creds):  # noqa: F811
    store, supervisor, _, _ = setup(tmp_path, creds, Transport((200, {}, {})))
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "uncertain_prompt")
    (call,) = store.model_calls("run1")
    assert call["outcome_class"] == "uncertain"
    assert call["cost_basis"] == "reserved_worst_case_estimate"
    assert call["model_reported"] is None
    assert store.daily_usage(DAY)["tokens"] == WORST_TOKENS


def test_usable_body_with_another_model_stays_substituted(tmp_path, creds):  # noqa: F811
    reply = (200, {}, {"model": "claude-haiku-4-5", "content": [], "usage": USAGE})
    code, billed = billed_call(creds, reply)
    assert code == "model_substituted"
    assert billed["outcome_class"] == "model_substituted"
    assert billed["reported_model"] == "anthropic/claude-haiku-4-5"


# -- 3. run usage, receipt and daily ledger in one transaction ---------------------------------


def fail_receipt_inserts(store):
    store.db.execute(
        "CREATE TEMP TRIGGER fail_receipt BEFORE INSERT ON model_calls "
        "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
    )


def fail_settlements(store):
    store.db.execute(
        "CREATE TEMP TRIGGER fail_settle BEFORE UPDATE ON model_calls "
        "WHEN NEW.status='settled' BEGIN SELECT RAISE(ABORT, 'injected settle failure'); END"
    )


@pytest.mark.parametrize("reply", [ok(), TimeoutError(), ok(model="claude-haiku-4-5")])
def test_failed_reservation_insert_sends_nothing(tmp_path, creds, reply):  # noqa: F811
    # The reservation is written before the billed request: if it cannot be, nothing is sent.
    transport = Transport(reply)
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    fail_receipt_inserts(store)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_unrecorded")
    assert store.effect("run1", "prompt") == "not_sent"
    assert [url for url, *_ in transport.sent if url.endswith("/v1/messages")] == []
    run = store.get("run1")
    assert (run["tokens"], run["cost"]) == (0, 0.0)
    assert store.model_calls("run1", every=True) == []
    assert store.daily_usage(DAY) == {"tokens": 0, "cost_cents": 0.0}


@pytest.mark.parametrize(
    "reply,reason",
    [
        (ok(), "model_call_unrecorded"),  # success path: on_model_call's settlement
        (TimeoutError(), "invalid_authority_or_usage"),  # billed failure: reserved worst case
        (ok(model="claude-haiku-4-5"), "invalid_authority_or_usage"),  # substitution
    ],
)
def test_failed_settlement_rolls_back_and_keeps_the_reservation(  # noqa: F811
    tmp_path,
    creds,  # noqa: F811
    reply,
    reason,
):
    store, supervisor, _, _ = setup(tmp_path, creds, Transport(reply))
    fail_settlements(store)
    row = supervisor.tick("run1", PROMPT)
    # The store failure halts the run fail-closed; the call is never replayed.
    assert (row["state"], row["reason"]) == ("stop_acknowledged", reason)
    assert store.effect("run1", "prompt") == "uncertain"
    # The settle rolled back as a whole: the reservation row, run totals and ledger stand.
    (held,) = store.model_calls("run1", every=True)
    assert held["status"] == "pending"
    assert (held["tokens"]["total"], held["cost"]) == (WORST_TOKENS, pytest.approx(WORST_CENTS))
    run = store.get("run1")
    assert (run["tokens"], run["cost"]) == (WORST_TOKENS, pytest.approx(WORST_CENTS))
    assert store.daily_usage(DAY) == {
        "tokens": WORST_TOKENS,
        "cost_cents": pytest.approx(WORST_CENTS),
    }
    # The next tick (the failure gone) settles it as the uncertain worst-case receipt.
    store.db.execute("DROP TRIGGER fail_settle")
    supervisor.tick("run1", PROMPT)
    (call,) = store.model_calls("run1")
    assert (call["status"], call["outcome_class"], call["cost_basis"]) == (
        "settled",
        "uncertain",
        "reserved_worst_case_estimate",
    )
    assert store.daily_usage(DAY)["tokens"] == WORST_TOKENS
    assert store.get("run1")["tokens"] == WORST_TOKENS


def test_record_model_call_writes_run_receipt_and_ledger_together(tmp_path):
    store = ReasoningStore(tmp_path / "state")
    store.bind("run1", {"model": MODEL}, 100.0)
    now = 1_795_000_000.0
    seq = store.record_model_call(
        "run1",
        {"tier": "sonnet", "cost": 1.5},
        now,
        run_tokens=1100,
        run_cost=1.5,
        tokens_delta=1100,
        cost_cents_delta=1.5,
    )
    assert seq == 1
    run = store.get("run1")
    assert (run["tokens"], run["cost"]) == (1100, 1.5)
    (call,) = store.model_calls("run1")
    assert call["cost"] == 1.5
    from tessera_agent.remote_reasoning_store import utc_day

    assert store.daily_usage(utc_day(now)) == {"tokens": 1100, "cost_cents": 1.5}
    # Regressed totals, bad deltas and unknown fields are refused before anything is written.
    with pytest.raises(ValueError):
        store.record_model_call("run1", {}, now, run_tokens=10, run_cost=1.5)
    with pytest.raises(ValueError):
        store.record_model_call("run1", {}, now, run_tokens=1200, run_cost=2.0, tokens_delta=-1)
    with pytest.raises(ValueError):
        store.record_model_call("run1", {"prompt": "x"}, now, run_tokens=1200, run_cost=2.0)
    assert (store.get("run1")["tokens"], len(store.model_calls("run1"))) == (1100, 1)


def test_record_model_call_rolls_back_the_run_update_on_insert_failure(tmp_path):
    store = ReasoningStore(tmp_path / "state")
    store.bind("run1", {"model": MODEL}, 100.0)
    fail_receipt_inserts(store)
    with pytest.raises(sqlite3.DatabaseError, match="injected receipt failure"):
        store.record_model_call(
            "run1", {"cost": 1.0}, 100.0, run_tokens=500, run_cost=1.0, tokens_delta=500
        )
    run = store.get("run1")
    assert (run["tokens"], run["cost"]) == (0, 0.0)
    assert store.model_calls("run1") == []
    assert json.loads(run["binding"]) == {"model": MODEL}


# -- nit: operator-pinned substitution prices --------------------------------------------------

SUBSTITUTION = {"input": 1500, "output": 7500, "cache_read": 150, "cache_write": 1875}


def test_substitution_prices_charge_the_actual_above_the_reservation(creds):  # noqa: F811
    from test_anthropic_runtime_integration import make_profile

    adapter = AnthropicMessagesReasoning(
        make_profile(substitution_pricing_cents_per_mtok=SUBSTITUTION),
        credentials_dir=creds,
        transport=Transport(ok(model="claude-opus-4-1", output_tokens=900)),
    )
    with pytest.raises(RuntimeBlocked) as caught:
        adapter.prompt({"id": "run1"}, PROMPT)
    billed = caught.value.billed_call
    actual = (100 * 1500 + 900 * 7500) / 1e6
    assert actual > WORST_CENTS
    assert billed["cost"] == pytest.approx(actual)
    assert billed["tokens"] == WORST_TOKENS
    assert billed["cost_basis"] == "worst_case_for_substituted_model"


def test_substitution_prices_never_lower_the_reservation_floor(creds):  # noqa: F811
    from test_anthropic_runtime_integration import make_profile

    adapter = AnthropicMessagesReasoning(
        make_profile(substitution_pricing_cents_per_mtok=SUBSTITUTION),
        credentials_dir=creds,
        transport=Transport(ok(model="claude-opus-4-1", input_tokens=1, output_tokens=1)),
    )
    with pytest.raises(RuntimeBlocked) as caught:
        adapter.prompt({"id": "run1"}, PROMPT)
    assert caught.value.billed_call["cost"] == pytest.approx(WORST_CENTS)


def test_matching_model_still_uses_the_pinned_prices(creds):  # noqa: F811
    from test_anthropic_runtime_integration import make_profile

    adapter = AnthropicMessagesReasoning(
        make_profile(substitution_pricing_cents_per_mtok=SUBSTITUTION),
        credentials_dir=creds,
        transport=Transport(ok()),
    )
    adapter.prompt({"id": "run1"}, PROMPT)
    assert adapter.status({"id": "run1"})["cost"] == pytest.approx((100 * 300 + 50 * 1500) / 1e6)


@pytest.mark.parametrize(
    "bad",
    [
        {**SUBSTITUTION, "input": 299},  # below the pinned price
        {**SUBSTITUTION, "output": -1},
        {**SUBSTITUTION, "input": float("nan")},
        {**SUBSTITUTION, "extra": 1},
        {"input": 1500},
        "SECRET-VALUE-9",
        None,
    ],
)
def test_invalid_substitution_prices_are_rejected_without_echo(bad):
    from test_anthropic_runtime_integration import make_profile

    from tessera_agent.anthropic_messages import validate_anthropic_profile

    with pytest.raises(ValueError) as caught:
        validate_anthropic_profile(make_profile(substitution_pricing_cents_per_mtok=bad))
    assert str(caught.value) == "anthropic_pricing_invalid"


def test_substitution_prices_join_the_policy_digest_only_when_present():
    from test_anthropic_runtime_integration import make_profile

    from tessera_agent.remote_reasoning_runtime import ReasoningSupervisor

    base = ReasoningSupervisor.policy(make_profile())
    assert "substitution_pricing_cents_per_mtok" not in base
    with_it = ReasoningSupervisor.policy(
        make_profile(substitution_pricing_cents_per_mtok=SUBSTITUTION)
    )
    assert with_it["substitution_pricing_cents_per_mtok"] == SUBSTITUTION
