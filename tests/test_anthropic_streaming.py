"""Streamed billed /v1/messages call: fake openers serving chunked SSE bytes, no network."""

import copy
import io
import json
import logging
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest

from tessera_agent import anthropic_messages
from tessera_agent.anthropic_messages import (
    API_URL,
    COUNT_TIMEOUT_SECONDS,
    COUNT_URL,
    MAX_STREAM_BYTES,
    MAX_STREAM_LINE_BYTES,
    STREAM_IDLE_TIMEOUT_SECONDS,
    AllowlistedTransport,
    AnthropicMessagesReasoning,
    EgressRefused,
    TransportFailed,
    stream_timeout,
)
from tessera_agent.remote_reasoning_runtime import (
    ModelConditionBlocked,
    ReasoningSupervisor,
    RuntimeBlocked,
)

SENTINEL = "sk-ant-SENTINEL-never-leak-0123456789"
TEXT = "COMPLETION-TEXT"
ROW = {"id": "run-1"}
MODEL = "claude-sonnet-4-5"

# What the non-streaming call returns for the same completion.
FULL = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": MODEL,
    "content": [{"type": "text", "text": TEXT}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 100,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 10,
        "output_tokens": 50,
    },
}


def start_event(**usage):
    message = copy.deepcopy(FULL) | {"content": [], "stop_reason": None}
    message["usage"] = dict(FULL["usage"], output_tokens=1) | usage
    return {"type": "message_start", "message": message}


def text_events(*pieces, index=0):
    yield {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    }
    for piece in pieces:
        yield {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": piece},
        }
    yield {"type": "content_block_stop", "index": index}


END = [
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 50},
    },
    {"type": "message_stop"},
]


def full_events():
    return [start_event(), {"type": "ping"}, *text_events("COMPLETION-", "TEXT"), *END]


def sse(events, *, crlf=False) -> bytes:
    nl = "\r\n" if crlf else "\n"
    return "".join(f"event: {e['type']}{nl}data: {json.dumps(e)}{nl}{nl}" for e in events).encode()


def chunks(data: bytes, size=7):
    return [data[i : i + size] for i in range(0, len(data), size)]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Idle:
    """A gap in the byte stream: times out like a socket if longer than the read timeout."""

    def __init__(self, seconds):
        self.seconds = seconds


class StreamResponse:
    """Serves scripted chunks through read1, like http.client on a chunked SSE body."""

    def __init__(self, items, *, clock, status=200, headers=None):
        self.items, self.clock, self.status = list(items), clock, status
        self.headers = headers if headers is not None else {"Content-Type": "text/event-stream"}
        self.timeout = None
        self.closed = False
        self.reads = 0

    def read1(self, n=-1):
        self.reads += 1
        while self.items:
            item = self.items.pop(0)
            if isinstance(item, Idle):
                if item.seconds > self.timeout:
                    self.clock.now += self.timeout
                    raise TimeoutError("timed out")
                self.clock.now += item.seconds
                continue
            if isinstance(item, BaseException):
                raise item
            assert n < 0 or len(item) <= n
            return item
        return b""

    def read(self, n=-1):
        raise AssertionError("a stream must be read incrementally")

    def close(self):
        self.closed = True


class JsonResponse:
    def __init__(self, body, status=200):
        self.status, self.headers, self.closed = status, {}, False
        self._body = json.dumps(body).encode()

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, StreamResponse):
            item.timeout = timeout
        return item


def profile(**limits):
    found = {
        "max_output_tokens_per_call": 1000,
        "max_input_tokens_per_call": 5000,
        "max_cost_cents_per_call": 10,
        "daily_token_cap": 100000,
        "daily_cost_cap_cents": 500,
    } | limits
    return {
        "account_mode": "anthropic-api-key-ref",
        "model": {"providerID": "anthropic", "id": MODEL},
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        "limits": found,
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
    }


@pytest.fixture
def creds(tmp_path):
    path = tmp_path / "anthropic-api-key"
    path.write_text(SENTINEL + "\n")
    path.chmod(0o600)
    return tmp_path


def run(creds, *items, limits=None, status=200, headers=None, sink=None):
    """Real transport: count_tokens (JSON) then the streamed billed call."""
    clock = Clock()
    response = StreamResponse(items, clock=clock, status=status, headers=headers)
    opener = Opener(JsonResponse({"input_tokens": 100}), response)
    reasoning = AnthropicMessagesReasoning(
        profile(**(limits or {})),
        credentials_dir=creds,
        transport=AllowlistedTransport("anthropic", opener=opener, monotonic=clock),
        output_sink=(lambda row, text: sink.append(text)) if sink is not None else None,
    )
    return reasoning, opener, response


def assert_uncertain_reserved(error):
    assert isinstance(error, RuntimeBlocked) and str(error) == "model_call_uncertain"
    assert error.request_sent is None
    call = error.billed_call
    assert call["outcome_class"] == "uncertain"
    assert call["cost_basis"] == "reserved_worst_case_estimate"
    assert call["usage"]["output"] == 1000  # the output cap, never a partial count


def assert_clean(*things):
    for thing in things:
        text = repr(thing) + str(thing) + repr(getattr(thing, "__dict__", ""))
        assert SENTINEL not in text and "COMPLETION" not in text and "secret" not in text


# -- reassembly ------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 7, 64, 100000])
@pytest.mark.parametrize("crlf", [False, True])
def test_stream_reassembles_the_non_streaming_body(size, crlf):
    clock = Clock()
    response = StreamResponse(chunks(sse(full_events(), crlf=crlf), size), clock=clock)
    opener = Opener(response)
    transport = AllowlistedTransport("anthropic", opener=opener, monotonic=clock)
    status, headers, body = transport.post_stream(
        API_URL, {"x-api-key": "k"}, {"model": MODEL}, idle_timeout=60, total_timeout=600
    )
    assert (status, body) == (200, FULL)
    assert headers == {"content-type": "text/event-stream"}
    request, timeout = opener.calls[0]
    assert json.loads(request.data) == {"model": MODEL, "stream": True}
    assert timeout == 60 and response.closed


def test_tool_use_and_thinking_blocks_reassemble():
    events = [
        start_event(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hm"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        {"type": "content_block_stop", "index": 0},
        *text_events("a", "b", index=1),
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"a": '},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": "[1]}"},
        },
        {"type": "content_block_stop", "index": 2},
        {"type": "future_event", "x": 1},
        *END,
    ]
    status, body = anthropic_messages._reassemble_stream(events)
    assert status == 200
    assert body["content"] == [
        {"type": "thinking", "thinking": "hm", "signature": "sig"},
        {"type": "text", "text": "ab"},
        {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": [1]}},
    ]


@pytest.mark.parametrize(
    "events",
    [
        [*text_events("x"), *END],  # content before message_start
        [start_event(), start_event(), *END],
        [
            start_event(),
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        ],
        [
            start_event(),
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "x"},
            },
        ],
        [start_event(), *text_events(7)],  # non-string text
    ],
)
def test_malformed_streams_are_refused(events):
    with pytest.raises(TransportFailed, match="model_stream_invalid"):
        anthropic_messages._reassemble_stream(events)


# -- adapter end to end ------------------------------------------------------------------------


def test_streamed_call_matches_the_non_streaming_observation(creds):
    sink = []
    reasoning, opener, _ = run(creds, *chunks(sse(full_events()), 5), sink=sink)
    reasoning.prompt(ROW, "hello")
    assert sink == [TEXT]
    (count_request, count_timeout), (request, timeout) = opener.calls
    assert count_request.full_url == COUNT_URL and count_timeout == COUNT_TIMEOUT_SECONDS
    assert "stream" not in json.loads(count_request.data)  # count_tokens stays non-streaming
    assert request.full_url == API_URL and timeout == STREAM_IDLE_TIMEOUT_SECONDS
    assert json.loads(request.data) == {
        "model": MODEL,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }

    class Plain:
        def __init__(self):
            self.replies = [(200, {}, {"input_tokens": 100}), (200, {}, copy.deepcopy(FULL))]

        def post_json(self, url, headers, body, *, timeout=None):
            return self.replies.pop(0)

    plain = AnthropicMessagesReasoning(profile(), credentials_dir=creds, transport=Plain())
    plain.prompt(ROW, "hello")
    assert reasoning.status(ROW) == plain.status(ROW)
    assert_clean(reasoning.status(ROW))


def test_long_generation_keeps_streaming_past_the_old_600s_cap(creds):
    # 2000 s of generation with a byte every 30 s: idle never trips, the total cap is larger.
    limits = {"max_output_tokens_per_call": 32768, "max_cost_cents_per_call": 5000}
    pieces = [Idle(30)] * 66
    items = [
        sse(
            [
                start_event(),
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ]
        )
    ]
    for piece in pieces:
        items += [
            piece,
            sse(
                [
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "x"},
                    }
                ]
            ),
        ]
    items += [sse([{"type": "content_block_stop", "index": 0}, *END])]
    sink = []
    reasoning, _, _ = run(creds, *items, limits=limits, sink=sink)
    reasoning.prompt(ROW, "hi")
    assert sink == ["x" * 66]
    assert stream_timeout(32768) == pytest.approx(3396.8)
    assert stream_timeout(1000) == pytest.approx(220.0)


def test_idle_gap_beyond_the_idle_timeout_is_uncertain_and_reserved(creds):
    head = sse([start_event(), *text_events("COMPLETION-")])
    reasoning, opener, response = run(
        creds, head, Idle(25), Idle(31), sse(END), limits={"stream_idle_timeout_seconds": 30}
    )
    with pytest.raises(RuntimeBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert_uncertain_reserved(caught.value)
    assert opener.calls[1][1] == 30 and response.closed
    assert_clean(caught.value, caught.value.billed_call)


def test_total_cap_ends_a_trickling_stream_uncertain(creds):
    trickle = [Idle(50), sse([{"type": "ping"}])] * 10
    reasoning, _, response = run(
        creds, sse([start_event()]), *trickle, sse(END), limits={"request_timeout_seconds": 240}
    )
    with pytest.raises(RuntimeBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert_uncertain_reserved(caught.value)
    assert response.items  # stopped reading before the stream ended


@pytest.mark.parametrize(
    "tail",
    [
        [],  # EOF after message_start, no message_stop
        [ConnectionResetError("peer reset with secret detail")],
        [b"data: {not json\n\n"],
        [sse([{"type": "error", "error": {"type": "overloaded_error", "message": "secret"}}])],
    ],
)
def test_stream_breaking_after_message_start_is_uncertain(creds, tail):
    # message_start and message_delta already reported partial usage: still the worst case.
    head = sse([start_event(output_tokens=7), *text_events("COMPLETION-"), END[0]])
    reasoning, _, _ = run(creds, head, *tail)
    with pytest.raises(RuntimeBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert_uncertain_reserved(caught.value)
    assert_clean(caught.value, caught.value.__context__, caught.value.billed_call)
    with pytest.raises(RuntimeBlocked, match="usage_unknown"):
        reasoning.status(ROW)


def test_stream_breaking_before_any_event_is_uncertain(creds):
    reasoning, _, _ = run(creds, b": comment\n\n", ConnectionResetError())
    with pytest.raises(RuntimeBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert_uncertain_reserved(caught.value)


@pytest.mark.parametrize(
    "kind,code,condition",
    [
        ("rate_limit_error", "model_rate_limited", True),
        ("overloaded_error", "model_rate_limited", True),
        ("billing_error", "model_quota_exhausted", True),
        ("invalid_request_error", "model_call_failed", False),
        ("authentication_error", "model_call_failed", False),
    ],
)
def test_error_event_before_message_start_is_a_definite_rejection(creds, kind, code, condition):
    event = {"type": "error", "error": {"type": kind, "message": "secret upstream text"}}
    reasoning, _, _ = run(creds, sse([{"type": "ping"}, event]), headers={"Retry-After": "12"})
    with pytest.raises(RuntimeBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert str(caught.value) == code
    assert isinstance(caught.value, ModelConditionBlocked) is condition
    assert not hasattr(caught.value, "billed_call")
    if condition:
        assert caught.value.request_sent is False and caught.value.retry_after == 12
    assert_clean(caught.value)


@pytest.mark.parametrize("kind", ["api_error", "never_heard_of_it", None])
def test_server_error_event_before_start_reserves_like_a_5xx(creds, kind):
    reasoning, _, _ = run(creds, sse([{"type": "error", "error": {"type": kind}}]))
    with pytest.raises(RuntimeBlocked, match="model_call_failed") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is None
    assert caught.value.billed_call["cost_basis"] == "reserved_worst_case_estimate"


def test_429_status_on_the_streamed_call_is_rate_limited(creds):
    clock = Clock()
    error = HTTPError(
        API_URL,
        429,
        "Too Many",
        {"Retry-After": "7"},
        io.BytesIO(b'{"error":{"type":"rate_limit_error","message":"secret"}}'),
    )
    opener = Opener(JsonResponse({"input_tokens": 100}), error)
    reasoning = AnthropicMessagesReasoning(
        profile(),
        credentials_dir=creds,
        transport=AllowlistedTransport("anthropic", opener=opener, monotonic=clock),
    )
    with pytest.raises(ModelConditionBlocked, match="model_rate_limited") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is False and caught.value.retry_after == 7
    assert not hasattr(caught.value, "billed_call")


def test_streamed_call_is_never_retried(creds):
    reasoning, opener, _ = run(creds, ConnectionResetError())
    with pytest.raises(RuntimeBlocked, match="model_call_uncertain"):
        reasoning.prompt(ROW, "hi")
    assert [r.full_url for r, _ in opener.calls] == [COUNT_URL, API_URL]


# -- egress and bounds ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://api.anthropic.com/v1/messages",
        "https://evil.example/v1/messages",
        "https://api.anthropic.com@evil.example/v1/messages",
        "https://api.anthropic.com:8443/v1/messages",
        "https://160.79.104.10/v1/messages",
    ],
)
def test_streaming_refuses_non_allowlisted_destinations_before_opening(url):
    opener = Opener()
    with pytest.raises(EgressRefused, match="model_egress_refused"):
        AllowlistedTransport("anthropic", opener=opener).post_stream(
            url, {}, {}, idle_timeout=60, total_timeout=600
        )
    assert opener.calls == []


def test_streaming_refuses_redirects(creds):
    reasoning, _, response = run(
        creds, sse(full_events()), status=307, headers={"Location": "https://evil.example/"}
    )
    with pytest.raises(RuntimeBlocked, match="model_egress_refused") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is False
    assert response.reads == 0 and response.closed


def test_streaming_uses_the_proxyless_redirect_refusing_opener(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    transport = AllowlistedTransport("anthropic")
    handlers = transport._opener.handlers
    assert not [h for h in handlers if isinstance(h, ProxyHandler) and h.proxies]
    assert any(isinstance(h, anthropic_messages._RefuseRedirects) for h in handlers)
    seen = []
    clock = Clock()

    def fake_open(request, timeout=None):
        seen.append((request.full_url, timeout))
        return StreamResponse([sse(full_events())], clock=clock)

    monkeypatch.setattr(transport._opener, "open", fake_open)
    status, _, body = transport.post_stream(API_URL, {}, {}, idle_timeout=20, total_timeout=15)
    assert (status, body, seen) == (200, FULL, [(API_URL, 15)])


def test_stream_total_bytes_are_bounded():
    pings = sse([{"type": "ping"}])
    chunk = pings * (anthropic_messages.STREAM_CHUNK_BYTES // len(pings))
    clock = Clock()
    response = StreamResponse(
        [sse([start_event()])] + [chunk] * (MAX_STREAM_BYTES // len(chunk) + 2), clock=clock
    )
    transport = AllowlistedTransport("anthropic", opener=Opener(response), monotonic=clock)
    with pytest.raises(TransportFailed, match="model_response_bound"):
        transport.post_stream(API_URL, {}, {}, idle_timeout=60, total_timeout=600)
    assert response.closed


def test_stream_line_length_is_bounded():
    clock = Clock()
    line = b"data: " + b"x" * MAX_STREAM_LINE_BYTES
    response = StreamResponse(chunks(line, 64 * 1024), clock=clock)
    transport = AllowlistedTransport("anthropic", opener=Opener(response), monotonic=clock)
    with pytest.raises(TransportFailed, match="model_response_bound"):
        transport.post_stream(API_URL, {}, {}, idle_timeout=60, total_timeout=600)


def test_stream_never_logs_or_prints_text(creds, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    sink = []
    reasoning, _, _ = run(creds, sse(full_events()), sink=sink)
    reasoning.prompt(ROW, "hi")
    broken, _, _ = run(creds, sse([start_event(), *text_events("COMPLETION-")]))
    with pytest.raises(RuntimeBlocked) as caught:
        broken.prompt(ROW, "hi")
    assert sink == [TEXT]  # the only path the text takes
    assert_clean(caught.value, reasoning.status(ROW), reasoning.transport)
    assert TEXT not in caplog.text and "COMPLETION" not in caplog.text
    out = capsys.readouterr()
    assert "COMPLETION" not in out.out + out.err


# -- limits ------------------------------------------------------------------------------------


@pytest.mark.parametrize("idle", [10, 45, 300])
def test_configured_stream_idle_timeout_is_used(creds, idle):
    reasoning, opener, _ = run(
        creds, sse(full_events()), limits={"stream_idle_timeout_seconds": idle}
    )
    reasoning.prompt(ROW, "hi")
    # The socket timeout never exceeds the total cap (220 s at a 1000-token output cap).
    assert opener.calls[1][1] == min(idle, stream_timeout(1000))


@pytest.mark.parametrize("bad", [0, 9, 301, 60.0, "60", True, None])
def test_invalid_stream_idle_timeout_is_refused_without_echo(creds, bad):
    with pytest.raises(ValueError) as caught:
        AnthropicMessagesReasoning(
            profile(stream_idle_timeout_seconds=bad), credentials_dir=creds, transport=Opener()
        )
    assert str(caught.value) == "anthropic_limits_invalid"


def test_stream_idle_timeout_is_policy_only_when_present():
    base, configured = profile(), profile(stream_idle_timeout_seconds=90)
    assert "stream_idle_timeout_seconds" not in ReasoningSupervisor.policy(base)["limits"]
    assert ReasoningSupervisor.policy(configured)["limits"]["stream_idle_timeout_seconds"] == 90
    assert ReasoningSupervisor.policy(configured) != ReasoningSupervisor.policy(base)
