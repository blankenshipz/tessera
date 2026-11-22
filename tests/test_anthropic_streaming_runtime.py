"""Streaming end to end: real ReasoningSupervisor + store + adapter + AllowlistedTransport.

Only the opener is fake (count_tokens JSON, then an SSE body). Synthetic; no network.
"""

import pytest
from test_anthropic_runtime_integration import (
    COMPLETION,
    MODEL,
    PROMPT,
    WORST_CENTS,
    WORST_TOKENS,
    Transport,
    assert_no_secrets,
    creds,  # noqa: F401 - fixture
    ok,
    setup,
)
from test_anthropic_streaming import (
    END,
    Clock,
    Idle,
    JsonResponse,
    Opener,
    StreamResponse,
    chunks,
    sse,
    text_events,
)

from tessera_agent.anthropic_messages import AllowlistedTransport

DAY = "2026-11-22"
VOLATILE = {"id", "at", "created_at", "ts", "time", "timestamp", "recorded_at"}


def stream_events():
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    return [
        {"type": "message_start", "message": message},
        *text_events(COMPLETION[:10], COMPLETION[10:]),
        *END,
    ]


def real_transport(*stream_items):
    clock = Clock()
    response = StreamResponse(stream_items, clock=clock)
    opener = Opener(JsonResponse({"input_tokens": 100}), response)
    return AllowlistedTransport("anthropic", opener=opener, monotonic=clock), opener


def stable(call):
    return {k: v for k, v in call.items() if k not in VOLATILE}


def test_streamed_success_receipt_equals_the_non_streamed_one(tmp_path, creds):  # noqa: F811
    transport, opener = real_transport(*chunks(sse(stream_events()), 13))
    store, supervisor, _, _ = setup(tmp_path / "s", creds, transport)
    supervisor.tick("run1", PROMPT)
    (streamed,) = store.model_calls("run1")
    assert streamed["cost_basis"] == "computed_from_pinned_prices"
    assert streamed["tokens"]["total"] == 150
    assert [r.full_url.rsplit("/", 1)[-1] for r, _ in opener.calls] == ["count_tokens", "messages"]

    plain = Transport(ok())
    store2, supervisor2, _, _ = setup(tmp_path / "p", creds, plain)
    supervisor2.tick("run1", PROMPT)
    (reference,) = store2.model_calls("run1")
    assert stable(streamed) == stable(reference)
    assert store.daily_usage(DAY) == store2.daily_usage(DAY)
    assert_no_secrets(store, tmp_path / "s")


@pytest.mark.parametrize("kind", ["broken", "idle"])
def test_interrupted_stream_reserves_the_worst_case_and_halts(tmp_path, creds, kind):  # noqa: F811
    head = chunks(sse(stream_events()[:2]), 11)
    tail = ConnectionResetError("reset") if kind == "broken" else Idle(10_000)
    transport, opener = real_transport(*head, tail)
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "uncertain_prompt")
    (call,) = store.model_calls("run1")
    assert call["outcome_class"] == "uncertain"
    assert call["cost_basis"] == "reserved_worst_case_estimate"
    assert call["tokens"]["output"] == 1000  # the cap, never the partial count
    assert call["tokens"]["total"] == WORST_TOKENS
    assert call["cost"] == pytest.approx(WORST_CENTS)
    assert store.daily_usage(DAY) == {
        "tokens": WORST_TOKENS,
        "cost_cents": pytest.approx(WORST_CENTS),
    }
    sent = len(opener.calls)
    assert sent == 2
    # Halted: a further tick never resends.
    supervisor.tick("run1", PROMPT)
    assert len(opener.calls) == sent
    assert len(store.model_calls("run1")) == 1
    assert_no_secrets(store, tmp_path)
