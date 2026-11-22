"""Isolated Anthropic Messages tool-loop tests: fake transport and tool bridge, no network."""

import hashlib
import json

import pytest

from tessera_agent.anthropic_messages import (
    API_URL,
    COUNT_URL,
    TOOL_NOT_PERMITTED,
    AnthropicMessagesReasoning,
    validate_anthropic_profile,
)
from tessera_agent.mcp_tool_bridge import ToolBridgeBlocked
from tessera_agent.remote_reasoning_runtime import ModelConditionBlocked, RuntimeBlocked

SENTINEL = "sk-ant-SENTINEL-never-leak-0123456789"
TOOL_INPUT = "TOOL-INPUT-SENTINEL"
TOOL_RESULT = "TOOL-RESULT-SENTINEL"
COMPLETION = "COMPLETION-TEXT"
ROW = {"id": "run-1"}
PIN = "a" * 64
TOOLS = {
    "tessera_read": {
        "name": "tessera_read",
        "description": "Read a record.",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
    "tessera_write": {
        "name": "tessera_write",
        "description": "Write a record.",
        "input_schema": {"type": "object"},
    },
    "tessera_secret": {
        "name": "tessera_secret",
        "description": "Not allowed by the profile.",
        "input_schema": {"type": "object"},
    },
}


def profile(allow=("tessera_read",), max_steps=4, **limits):
    found = {
        "max_output_tokens_per_call": 1000,
        "max_input_tokens_per_call": 5000,
        "max_cost_cents_per_call": 10,
        "daily_token_cap": 100000,
        "daily_cost_cap_cents": 500,
    }
    if allow:
        found |= {"max_tool_calls": 8, "max_tool_result_bytes": 4096}
    found.update(limits)
    result = {
        "account_mode": "anthropic-api-key-ref",
        "model": {"providerID": "anthropic", "id": "claude-sonnet-4-5"},
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}]
        + [{"action": name, "resource": "*", "effect": "allow"} for name in allow],
        "limits": found,
        "max_steps": max_steps,
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
    }
    if allow:
        result["tool_catalog_sha256"] = PIN
    return result


@pytest.fixture
def creds(tmp_path):
    path = tmp_path / "anthropic-api-key"
    path.write_text(SENTINEL + "\n")
    path.chmod(0o600)
    return tmp_path


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.timeouts = []

    def post_json(self, url, headers, body, *, timeout=None):
        self.calls.append((url, dict(headers), json.loads(json.dumps(body))))
        self.timeouts.append((url, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def billed(self):
        return [c for c in self.calls if c[0] == API_URL]


class FakeBridge:
    """tools()/call() like McpToolBridge; it over-lists so the adapter must filter."""

    def __init__(self, allowed=("tessera_read",), result=(TOOL_RESULT, False), listed=None):
        self.allowed = frozenset(allowed)
        self.result = result
        self.listed = listed if listed is not None else list(TOOLS.values())
        self.calls = []
        self.flag = False

    def tools(self):
        return [dict(t) for t in self.listed]

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        self.last_truncated = self.flag
        if name not in self.allowed:
            return "tool not permitted", True
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def count(n=100):
    return (200, {}, {"input_tokens": n})


def tool_use(*calls, model="claude-sonnet-4-5", input_tokens=100, output_tokens=50):
    content = [{"type": "text", "text": "thinking aloud " + COMPLETION}]
    for i, (name, arguments) in enumerate(calls):
        content.append({"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": arguments})
    return (
        200,
        {},
        {
            "model": model,
            "stop_reason": "tool_use",
            "content": content,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    )


def end_turn(model="claude-sonnet-4-5", text=COMPLETION):
    return (
        200,
        {},
        {
            "model": model,
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 200, "output_tokens": 20},
        },
    )


READ = ("tessera_read", {"q": TOOL_INPUT})


class Recorder:
    def __init__(self):
        self.models, self.tools, self.sink, self.fenced = [], [], [], []

    def kwargs(self):
        return {
            "on_model_call": lambda row, receipt: self.models.append(receipt),
            "on_tool_call": lambda row, receipt: self.tools.append(receipt),
            "output_sink": lambda row, text: self.sink.append(text),
            "between_turns": lambda row: self.fenced.append(row["id"]),
        }


def adapter(creds, transport, bridge=None, rec=None, prof=None, **kwargs):
    if rec is not None:
        kwargs = rec.kwargs() | kwargs
    return AnthropicMessagesReasoning(
        prof or profile(),
        credentials_dir=creds,
        transport=transport,
        tool_bridge=bridge if bridge is not None else FakeBridge(),
        **kwargs,
    )


def assert_private(*things):
    for thing in things:
        text = repr(thing) + json.dumps(thing, default=repr)
        for secret in (SENTINEL, TOOL_INPUT, TOOL_RESULT, COMPLETION):
            assert secret not in text


def test_two_turn_tool_loop_receipts_and_digests(creds):
    transport = FakeTransport(count(), tool_use(READ), count(300), end_turn())
    bridge, rec = FakeBridge(), Recorder()
    reasoning = adapter(creds, transport, bridge, rec)
    reasoning.prompt(ROW, "hello")
    assert [c[0] for c in transport.calls] == [COUNT_URL, API_URL, COUNT_URL, API_URL]
    # Only the allowed tool is offered, in every preflight and billed call.
    for _, _, body in transport.calls:
        assert body["tools"] == [TOOLS["tessera_read"]]
        assert "tool_choice" not in body
    assert bridge.calls == [("tessera_read", {"q": TOOL_INPUT})]
    second = transport.calls[3][2]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_0", "content": TOOL_RESULT, "is_error": False}
    ]
    assert rec.fenced == ["run-1"]  # Kill switch checked between turns only.
    assert rec.sink == [COMPLETION]
    assert [r["turn"] for r in rec.models] == [1, 2]
    assert set(rec.models[0]) == {
        "turn",
        "usage",
        "total",
        "cost",
        "cost_cents_estimate_max",
        "reported_model",
        "stop_reason",
        "tool_use_count",
        "unrun_tool_count",
    }
    assert rec.models[0]["usage"] == {
        "input": 100,
        "output": 50,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
    }
    assert rec.models[0]["total"] == 150
    assert rec.models[0]["cost"] == pytest.approx((100 * 300 + 50 * 1500) / 1e6)
    assert rec.models[0]["cost_cents_estimate_max"] == pytest.approx(
        (100 * 300 + 1000 * 1500) / 1e6
    )
    assert rec.models[0]["reported_model"] == "anthropic/claude-sonnet-4-5"
    assert (rec.models[0]["stop_reason"], rec.models[0]["tool_use_count"]) == ("tool_use", 1)
    assert (rec.models[1]["stop_reason"], rec.models[1]["tool_use_count"]) == ("end_turn", 0)
    assert [r["unrun_tool_count"] for r in rec.models] == [0, 0]
    assert rec.tools == [
        {
            "turn": 1,
            "index": 0,
            "name": "tessera_read",
            "input_sha256": hashlib.sha256(
                json.dumps({"q": TOOL_INPUT}, separators=(",", ":")).encode()
            ).hexdigest(),
            "result_sha256": hashlib.sha256(TOOL_RESULT.encode()).hexdigest(),
            "is_error": False,
            "truncated": False,
            "allowed": True,
            "status": "succeeded",
            "skip_reason": None,
        }
    ]
    observed = reasoning.status(ROW)
    assert observed["outcome"] == "succeeded"
    assert (observed["turns"], observed["tool_calls"]) == (2, 1)
    assert observed["tokens"] == 150 + 220
    assert observed["usage"]["input"] == 300
    assert_private(rec.models, rec.tools, observed, reasoning)


def test_no_allows_offers_no_tools(creds):
    transport = FakeTransport(count(), end_turn())
    bridge = FakeBridge()
    reasoning = adapter(creds, transport, bridge, prof=profile(allow=()))
    reasoning.prompt(ROW, "hi")
    assert all("tools" not in body for _, _, body in transport.calls)
    assert bridge.calls == []


def test_no_allows_tool_use_halts_without_executing(creds):
    transport = FakeTransport(count(), tool_use(READ))
    bridge, rec = FakeBridge(), Recorder()
    reasoning = adapter(creds, transport, bridge, rec, prof=profile(allow=()))
    with pytest.raises(RuntimeBlocked, match="tool_call_cap_exceeded") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is True and caught.value.resumable is False
    assert bridge.calls == []
    # Never executed: the block is receipted as refused by the cap.
    assert [(r["status"], r["skip_reason"], r["allowed"]) for r in rec.tools] == [
        ("refused", "tool_call_cap_exceeded", False)
    ]


def test_disallowed_tool_gets_error_result_and_is_never_executed(creds):
    transport = FakeTransport(
        count(), tool_use(("tessera_secret", {"q": TOOL_INPUT}), READ), count(), end_turn()
    )
    bridge, rec = FakeBridge(allowed=TOOLS), Recorder()  # The bridge would run anything.
    reasoning = adapter(creds, transport, bridge, rec)
    reasoning.prompt(ROW, "hi")
    assert bridge.calls == [("tessera_read", {"q": TOOL_INPUT})]
    assert [
        (r["index"], r["name"], r["allowed"], r["is_error"], r["status"], r["skip_reason"])
        for r in rec.tools
    ] == [
        (0, "tessera_secret", False, True, "denied", None),
        (1, "tessera_read", True, False, "succeeded", None),
    ]
    results = transport.calls[3][2]["messages"][2]["content"]
    assert results[0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_0",
        "content": TOOL_NOT_PERMITTED,
        "is_error": True,
    }
    assert reasoning.status(ROW)["tool_calls"] == 2


def test_catalog_missing_allowed_tool_blocks_before_any_call(creds):
    transport = FakeTransport()
    bridge = FakeBridge(listed=[TOOLS["tessera_write"]])
    with pytest.raises(RuntimeBlocked, match="tool_catalog_changed") as caught:
        adapter(creds, transport, bridge).prompt(ROW, "hi")
    assert caught.value.request_sent is False
    assert transport.calls == []


def test_tools_allowed_without_bridge_blocks(creds):
    reasoning = AnthropicMessagesReasoning(
        profile(), credentials_dir=creds, transport=FakeTransport()
    )
    with pytest.raises(RuntimeBlocked, match="tool_bridge_unavailable") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is False


def test_budget_refusal_on_turn_two_stops_before_second_billed_call(creds):
    seen = []

    def budget(tokens, cents):
        seen.append(tokens)
        return len(seen) == 1

    transport = FakeTransport(count(), tool_use(READ), count(), end_turn())
    rec = Recorder()
    reasoning = adapter(creds, transport, rec=rec, budget_check=budget)
    with pytest.raises(RuntimeBlocked, match="model_call_cap_exceeded") as caught:
        reasoning.prompt(ROW, "hi")
    assert len(seen) == 2
    assert [c[0] for c in transport.calls] == [COUNT_URL, API_URL, COUNT_URL]
    assert caught.value.request_sent is True and caught.value.resumable is True
    assert [r["turn"] for r in rec.models] == [1]
    assert reasoning.status(ROW)["outcome"] is None


def test_tool_call_cap_exceeded_across_turns(creds):
    transport = FakeTransport(
        count(), tool_use(READ), count(), tool_use(READ, READ), count(), end_turn()
    )
    bridge, rec = FakeBridge(), Recorder()
    reasoning = adapter(creds, transport, bridge, rec, prof=profile(max_tool_calls=2))
    with pytest.raises(RuntimeBlocked, match="tool_call_cap_exceeded") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is True and caught.value.resumable is False
    assert len(bridge.calls) == 2  # Turn 1 one call, turn 2 one call, then the cap.
    assert [(r["turn"], r["index"], r["status"]) for r in rec.tools] == [
        (1, 0, "succeeded"),
        (2, 0, "succeeded"),
        (2, 1, "refused"),
    ]
    assert rec.tools[2]["skip_reason"] == "tool_call_cap_exceeded"
    assert len(transport.billed()) == 2
    assert reasoning.status(ROW)["tool_calls"] == 2
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)


def test_between_turns_raising_stops_before_next_billed_call(creds):
    def fence(row):
        raise RuntimeBlocked("stop_requested")

    transport = FakeTransport(count(), tool_use(READ), count(), end_turn())
    bridge = FakeBridge()
    reasoning = adapter(creds, transport, bridge, between_turns=fence)
    with pytest.raises(RuntimeBlocked, match="stop_requested") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is True and caught.value.resumable is False
    assert [c[0] for c in transport.calls] == [COUNT_URL, API_URL]
    assert len(bridge.calls) == 1
    assert reasoning.status(ROW)["turns"] == 1
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)


def test_non_runtime_fence_error_is_sanitized(creds):
    def fence(row):
        raise ValueError(SENTINEL)

    transport = FakeTransport(count(), tool_use(READ))
    with pytest.raises(RuntimeBlocked, match="invalid_authority_or_usage") as caught:
        adapter(creds, transport, between_turns=fence).prompt(ROW, "hi")
    assert_private(caught.value)


def test_max_steps_exhausted_is_failed_turn_limit(creds):
    transport = FakeTransport(count(), tool_use(READ), count(), tool_use(READ))
    bridge, rec = FakeBridge(), Recorder()
    reasoning = adapter(creds, transport, bridge, rec, prof=profile(max_steps=2))
    reasoning.prompt(ROW, "hi")
    observed = reasoning.status(ROW)
    assert (observed["outcome"], observed["reason"]) == ("failed", "turn_limit_reached")
    assert observed["turns"] == 2
    # Tools requested on the last turn are not executed: no turn is left to use them.
    assert len(bridge.calls) == 1 and observed["tool_calls"] == 1
    assert [r["turn"] for r in rec.models] == [1, 2]
    assert [r["unrun_tool_count"] for r in rec.models] == [0, 1]
    # They are receipted refused (turn_limit_reached) with no result and no flags.
    assert [(r["turn"], r["status"]) for r in rec.tools] == [(1, "succeeded"), (2, "refused")]
    assert rec.tools[1]["skip_reason"] == "turn_limit_reached"
    assert (rec.tools[1]["result_sha256"], rec.tools[1]["is_error"]) == (None, None)
    assert rec.tools[1]["truncated"] is None
    assert rec.sink == []
    assert transport.responses == []


def test_resumable_rejection_on_turn_two_then_resume(creds):
    limited = (429, {"retry-after": "7"}, {"error": {"type": "rate_limit_error"}})
    transport = FakeTransport(count(), tool_use(READ), count(), limited, count(), end_turn())
    bridge, rec = FakeBridge(), Recorder()
    reasoning = adapter(creds, transport, bridge, rec)
    with pytest.raises(ModelConditionBlocked, match="model_rate_limited") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is True and caught.value.resumable is True
    assert caught.value.retry_after == 7.0
    with pytest.raises(RuntimeBlocked, match="model_resume_required"):
        reasoning.prompt(ROW, "hi")  # Never restart (replay) a parked conversation.
    reasoning.resume(ROW)
    urls = [c[0] for c in transport.calls]
    assert urls == [COUNT_URL, API_URL, COUNT_URL, API_URL, COUNT_URL, API_URL]
    # The resumed call is turn 2: the same conversation, turn 1 not re-sent on its own.
    assert transport.calls[5][2]["messages"] == transport.calls[3][2]["messages"]
    assert len(transport.calls[5][2]["messages"]) == 3
    assert len(bridge.calls) == 1  # No tool re-executed.
    assert [r["turn"] for r in rec.models] == [1, 2]
    assert len(rec.tools) == 1
    observed = reasoning.status(ROW)
    assert (observed["outcome"], observed["turns"], observed["tool_calls"]) == ("succeeded", 2, 1)
    assert rec.sink == [COMPLETION]
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)


def test_rejection_on_turn_one_stays_not_sent(creds):
    limited = (529, {}, {"error": {"type": "overloaded_error"}})
    transport = FakeTransport(count(), limited)
    reasoning = adapter(creds, transport)
    with pytest.raises(ModelConditionBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is False
    assert getattr(caught.value, "resumable", False) is False
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)


@pytest.mark.parametrize(
    "second",
    [
        TimeoutError("timed out " + SENTINEL),
        (500, {}, {"error": {"type": "api_error"}}),
        (200, {}, {"model": "claude-sonnet-4-5", "content": []}),
    ],
)
def test_uncertain_turn_two_drops_pending_state(creds, second):
    transport = FakeTransport(count(), tool_use(READ), count(), second)
    reasoning = adapter(creds, transport)
    with pytest.raises(RuntimeBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is None
    assert str(caught.value) in {"model_call_uncertain", "model_call_failed"}
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)
    with pytest.raises(RuntimeBlocked, match="usage_unknown"):
        reasoning.status(ROW)
    assert_private(caught.value)


def test_uncertain_tool_call_halts(creds):
    transport = FakeTransport(count(), tool_use(READ))
    bridge = FakeBridge(result=ToolBridgeBlocked("tool_call_uncertain"))
    reasoning = adapter(creds, transport, bridge)
    with pytest.raises(RuntimeBlocked, match="tool_call_uncertain") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is None
    assert_private(caught.value)
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)


def test_uncertain_tool_call_is_receipted_without_intent_hooks(creds):
    transport = FakeTransport(count(), tool_use(READ))
    bridge, rec = FakeBridge(result=OSError(TOOL_RESULT)), Recorder()
    with pytest.raises(RuntimeBlocked, match="tool_call_uncertain"):
        adapter(creds, transport, bridge, rec).prompt(ROW, "hi")
    (row,) = rec.tools
    assert (row["status"], row["result_sha256"], row["is_error"]) == ("uncertain", None, None)
    assert_private(rec.tools)


def test_intent_hook_runs_before_the_bridge_and_result_completes_it(creds):
    events = []

    class Ordered(FakeBridge):
        def call(self, name, arguments):
            events.append(("bridge", name))
            return super().call(name, arguments)

    def intent(row, call):
        events.append(("intent", call["name"], call["allowed"]))
        return ("handle", call["index"])

    def result(handle, outcome):
        events.append(("result", handle, outcome["status"]))

    transport = FakeTransport(count(), tool_use(READ), count(), end_turn())
    rec = Recorder()
    reasoning = adapter(
        creds, transport, Ordered(), rec, on_tool_intent=intent, on_tool_result=result
    )
    reasoning.prompt(ROW, "hi")
    assert events == [
        ("intent", "tessera_read", True),
        ("bridge", "tessera_read"),
        ("result", ("handle", 0), "succeeded"),
    ]
    assert rec.tools == []  # on_tool_call carries only never-executed rows here


def test_uncertain_tool_call_completes_its_intent_as_uncertain(creds):
    outcomes = []
    transport = FakeTransport(count(), tool_use(READ))
    bridge = FakeBridge(result=ToolBridgeBlocked("tool_call_uncertain"))
    reasoning = adapter(
        creds,
        transport,
        bridge,
        on_tool_intent=lambda row, call: "h",
        on_tool_result=lambda handle, outcome: outcomes.append((handle, outcome)),
    )
    with pytest.raises(RuntimeBlocked, match="tool_call_uncertain"):
        reasoning.prompt(ROW, "hi")
    assert outcomes == [
        ("h", {"status": "uncertain", "result_sha256": None, "is_error": None, "truncated": None})
    ]


def test_intent_hook_failure_never_runs_the_tool(creds):
    def broken(row, call):
        raise OSError("disk")

    transport = FakeTransport(count(), tool_use(READ))
    bridge = FakeBridge()
    reasoning = adapter(
        creds, transport, bridge, on_tool_intent=broken, on_tool_result=lambda h, o: None
    )
    with pytest.raises(RuntimeBlocked, match="tool_call_unrecorded"):
        reasoning.prompt(ROW, "hi")
    assert bridge.calls == []


@pytest.mark.parametrize(
    "error,code",
    [
        (RuntimeBlocked("stop_requested"), "stop_requested"),
        (RuntimeBlocked("grant_revoked"), "grant_revoked"),
        (ValueError(SENTINEL), "invalid_authority_or_usage"),
    ],
)
def test_before_tool_trip_refuses_every_remaining_block(creds, error, code):
    def fence(row):
        raise error

    transport = FakeTransport(count(), tool_use(READ, ("tessera_secret", {"q": TOOL_INPUT}), READ))
    bridge, rec = FakeBridge(), Recorder()
    reasoning = adapter(creds, transport, bridge, rec, before_tool=fence)
    with pytest.raises(RuntimeBlocked, match=code) as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is True and caught.value.resumable is False
    assert bridge.calls == []
    assert [(r["index"], r["status"], r["skip_reason"], r["allowed"]) for r in rec.tools] == [
        (0, "refused", code, True),
        (1, "refused", code, False),
        (2, "refused", code, True),
    ]
    assert all(r["result_sha256"] is None for r in rec.tools)
    assert reasoning.status(ROW)["tool_calls"] == 0
    assert_private(caught.value, rec.tools)


def test_before_tool_trip_between_tools_of_one_turn(creds):
    checks = iter([None, RuntimeBlocked("stop_requested")])

    def fence(row):
        found = next(checks)
        if found is not None:
            raise found

    transport = FakeTransport(count(), tool_use(READ, READ))
    bridge, rec = FakeBridge(), Recorder()
    reasoning = adapter(creds, transport, bridge, rec, before_tool=fence)
    with pytest.raises(RuntimeBlocked, match="stop_requested"):
        reasoning.prompt(ROW, "hi")
    assert len(bridge.calls) == 1
    assert [(r["index"], r["status"]) for r in rec.tools] == [(0, "succeeded"), (1, "refused")]


def test_bridge_truncation_flag_is_receipted(creds):
    transport = FakeTransport(count(), tool_use(READ), count(), end_turn())
    bridge, rec = FakeBridge(), Recorder()
    bridge.flag = True
    adapter(creds, transport, bridge, rec).prompt(ROW, "hi")
    assert rec.tools[0]["truncated"] is True


def test_bridge_catalog_change_maps_to_not_sent(creds):
    class Changed(FakeBridge):
        def tools(self):
            raise ToolBridgeBlocked("tool_catalog_changed")

    transport = FakeTransport()
    with pytest.raises(RuntimeBlocked, match="tool_catalog_changed") as caught:
        adapter(creds, transport, Changed()).prompt(ROW, "hi")
    assert caught.value.request_sent is False and transport.calls == []


def test_tool_result_is_truncated_to_profile_bound(creds):
    transport = FakeTransport(count(), tool_use(READ), count(), end_turn())
    bridge, rec = FakeBridge(result=("é" * 100, False)), Recorder()
    reasoning = adapter(creds, transport, bridge, rec, prof=profile(max_tool_result_bytes=11))
    reasoning.prompt(ROW, "hi")
    sent = transport.calls[3][2]["messages"][2]["content"][0]["content"]
    assert sent == "é" * 5 and len(sent.encode()) <= 11
    assert rec.tools[0]["truncated"] is True


@pytest.mark.parametrize("turn", [1, 2])
@pytest.mark.parametrize("model", ["claude-haiku-4-5", None])
def test_model_substitution_on_any_turn_halts(creds, turn, model):
    responses = [count(), tool_use(READ, model=model if turn == 1 else "claude-sonnet-4-5")]
    responses += [count(), end_turn(model=model)]
    transport = FakeTransport(*responses)
    bridge, rec = FakeBridge(), Recorder()
    reasoning = adapter(creds, transport, bridge, rec)
    # A reply without a string model is not a usable message: uncertain, not substituted.
    code = "model_substituted" if model else "model_call_uncertain"
    with pytest.raises(RuntimeBlocked, match=code) as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is None
    assert len(bridge.calls) == turn - 1
    assert len(rec.models) == turn - 1
    assert rec.sink == []
    with pytest.raises(RuntimeBlocked, match="usage_unknown"):
        reasoning.status(ROW)
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)


def test_receipt_callback_failure_is_uncertain(creds):
    def broken(row, receipt):
        raise OSError("disk")

    transport = FakeTransport(count(), tool_use(READ))
    bridge = FakeBridge()
    with pytest.raises(RuntimeBlocked, match="model_call_unrecorded") as caught:
        adapter(creds, transport, bridge, on_model_call=broken).prompt(ROW, "hi")
    assert caught.value.request_sent is None
    assert bridge.calls == []


def test_unexpected_tool_exception_is_uncertain_and_sanitized(creds):
    transport = FakeTransport(count(), tool_use(READ))
    bridge = FakeBridge(result=OSError(TOOL_RESULT))
    with pytest.raises(RuntimeBlocked, match="tool_call_uncertain") as caught:
        adapter(creds, transport, bridge).prompt(ROW, "hi")
    assert caught.value.request_sent is None
    assert_private(caught.value)


# -- per-turn receipt/count rule and timeouts (ADR 0083 §5) -----------------------------------


@pytest.mark.parametrize(
    "second",
    [
        TimeoutError("timed out " + SENTINEL),
        ConnectionResetError("reset " + SENTINEL),
        (503, {}, {"error": {"type": "api_error"}}),
        (200, {}, {"model": "claude-sonnet-4-5", "content": []}),
    ],
)
def test_unknown_turn_two_reserves_that_turns_worst_case(creds, second):
    # Turn 2's preflight counts 300 input tokens: the reservation is 300 + output cap.
    transport = FakeTransport(count(), tool_use(READ), count(300), second)
    rec = Recorder()
    with pytest.raises(RuntimeBlocked) as caught:
        adapter(creds, transport, rec=rec).prompt(ROW, "hi")
    worst = (300 * 300 + 1000 * 1500) / 1e6
    assert caught.value.billed_call == {
        "outcome_class": "uncertain",
        "cost_basis": "reserved_worst_case_estimate",
        "usage": {"input": 300, "output": 1000, "cache_read": 0, "cache_write": 0, "reasoning": 0},
        "tokens": 1300,
        "cost": pytest.approx(worst),
        "reported_model": None,
        "cost_cents_estimate_max": pytest.approx(worst),
        "turn": 2,
    }
    assert [r["turn"] for r in rec.models] == [1]  # turn 2 is never a second on_model_call row
    assert len([c for c in transport.calls if c[0] == API_URL]) == 2  # never replayed
    assert_private(caught.value, caught.value.billed_call)


def test_substituted_turn_two_carries_worst_case(creds):
    transport = FakeTransport(count(), tool_use(READ), count(300), end_turn("claude-haiku-4-5"))
    rec = Recorder()
    with pytest.raises(RuntimeBlocked, match="model_substituted") as caught:
        adapter(creds, transport, rec=rec).prompt(ROW, "hi")
    billed = caught.value.billed_call
    assert (billed["outcome_class"], billed["cost_basis"]) == (
        "model_substituted",
        "worst_case_for_substituted_model",
    )
    assert (billed["turn"], billed["stop_reason"]) == (2, "end_turn")
    assert billed["usage"] == {
        "input": 200,
        "output": 20,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
    }
    # Charged max(actual, reserved worst case): counted 300 input + the 1000 output cap.
    assert billed["tokens"] == 1300
    assert billed["cost"] == pytest.approx((300 * 300 + 1000 * 1500) / 1e6)
    assert billed["reported_model"] == "anthropic/claude-haiku-4-5"
    assert [r["turn"] for r in rec.models] == [1] and rec.sink == []
    assert_private(caught.value, billed)


def test_substituted_turn_two_uses_the_substitution_price_table(creds):
    prof = profile()
    prof["substitution_pricing_cents_per_mtok"] = {
        "input": 30000,
        "output": 150000,
        "cache_read": 3000,
        "cache_write": 37500,
    }
    transport = FakeTransport(count(), tool_use(READ), count(300), end_turn("claude-haiku-4-5"))
    with pytest.raises(RuntimeBlocked, match="model_substituted") as caught:
        adapter(creds, transport, prof=prof, rec=Recorder()).prompt(ROW, "hi")
    billed = caught.value.billed_call
    assert billed["cost_basis"] == "worst_case_for_substituted_model"
    # 200 in / 20 out at the substitution table (9 cents) exceeds the 1.59-cent reservation.
    assert billed["cost"] == pytest.approx((200 * 30000 + 20 * 150000) / 1e6)
    assert billed["turn"] == 2


def test_output_unrecorded_with_per_turn_hook_carries_no_second_row(creds):
    # on_model_call already receipted the final turn; a billed_call would double count it.
    def sink(row, text):
        raise OSError("disk full")

    transport = FakeTransport(count(), tool_use(READ), count(), end_turn())
    rec = Recorder()
    with pytest.raises(RuntimeBlocked, match="model_output_unrecorded") as caught:
        adapter(creds, transport, rec=rec, output_sink=sink).prompt(ROW, "hi")
    assert [r["turn"] for r in rec.models] == [1, 2]
    assert not hasattr(caught.value, "billed_call")


@pytest.mark.parametrize(
    "limits,billed_timeout",
    [({}, 60 + 0.04 * 1000), ({"request_timeout_seconds": 240}, 240.0)],
)
def test_every_turn_uses_the_per_request_timeouts(creds, limits, billed_timeout):
    transport = FakeTransport(count(), tool_use(READ), count(), tool_use(READ), count(), end_turn())
    adapter(creds, transport, prof=profile(**limits)).prompt(ROW, "hi")
    assert (
        transport.timeouts
        == [
            (COUNT_URL, 30.0),
            (API_URL, pytest.approx(billed_timeout)),
        ]
        * 3
    )


@pytest.mark.parametrize(
    "value",
    ["relative/ca.pem", "/", "/etc/../ca.pem", "/etc//ca.pem", "/etc/ca\n.pem", 7, None],
)
def test_control_ca_file_must_be_a_clean_absolute_path(value):
    bad = profile()
    bad["control_ca_file"] = value
    with pytest.raises(ValueError, match="^anthropic_control_ca_file_invalid$"):
        validate_anthropic_profile(bad)


def test_control_ca_file_absolute_path_accepted():
    good = profile()
    good["control_ca_file"] = "/etc/tessera/control-ca.pem"
    validate_anthropic_profile(good)
