"""Isolated Anthropic Messages adapter tests: fake openers/transports, no network or keys."""

import io
import json
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from tessera_agent.anthropic_messages import (
    API_URL,
    API_VERSION,
    COUNT_TIMEOUT_SECONDS,
    COUNT_URL,
    MAX_RESPONSE_BYTES,
    AllowlistedTransport,
    AnthropicMessagesReasoning,
    EgressRefused,
    _RefuseRedirects,
    messages_timeout,
)
from tessera_agent.remote_reasoning_runtime import ModelConditionBlocked, RuntimeBlocked

SENTINEL = "sk-ant-SENTINEL-never-leak-0123456789"
ROW = {"id": "run-1"}


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}
        self.closed = False

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# -- transport ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://api.anthropic.com/v1/messages",
        "https://api.openai.com/v1/messages",
        "https://api.anthropic.com.evil.example/v1/messages",
        "https://evil.example/v1/messages",
        "https://api.anthropic.com@evil.example/v1/messages",
        "https://user:pw@api.anthropic.com/v1/messages",
        "https://evil.example\\@api.anthropic.com/v1/messages",
        "https://160.79.104.10/v1/messages",
        "https://[2607:6bc0::10]/v1/messages",
        "https://api.anthropic.com:8443/v1/messages",
        "https://api.anthropic.com:443x/v1/messages",
        "https://api.anthropic.com./v1/messages",
        "https://API.anthropic.com\n/v1/messages",
        "https://api.anthropic.com/v1/messages#frag",
        "ftp://api.anthropic.com/v1/messages",
        None,
    ],
)
def test_transport_refuses_non_allowlisted_destinations_before_opening(url):
    opener = FakeOpener(FakeResponse())
    with pytest.raises(EgressRefused) as caught:
        AllowlistedTransport("anthropic", opener=opener).post_json(url, {}, {})
    assert str(caught.value) == "model_egress_refused"
    assert opener.calls == []


@pytest.mark.parametrize("provider", ["openai", "unknown", ""])
def test_transport_refuses_other_providers_for_anthropic_host(provider):
    opener = FakeOpener(FakeResponse())
    with pytest.raises(EgressRefused):
        AllowlistedTransport(provider, opener=opener).post_json(API_URL, {}, {})
    assert opener.calls == []


def test_transport_allows_explicit_443_and_returns_lower_headers():
    opener = FakeOpener(FakeResponse(200, {"ok": 1}, {"Retry-After": "3"}))
    transport = AllowlistedTransport("anthropic", opener=opener, timeout=7)
    status, headers, body = transport.post_json(
        "https://api.anthropic.com:443/v1/messages", {"x-api-key": "k"}, {"a": 1}
    )
    assert (status, headers, body) == (200, {"retry-after": "3"}, {"ok": 1})
    request, timeout = opener.calls[0]
    assert timeout == 7 and request.get_method() == "POST"
    assert json.loads(request.data) == {"a": 1}


def test_default_opener_ignores_environment_proxies(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    opener = AllowlistedTransport("anthropic")._opener
    assert not [h for h in opener.handlers if isinstance(h, ProxyHandler) and h.proxies]
    # Control: urllib's default opener would have picked the environment proxy up.
    default = build_opener()
    assert any(isinstance(h, ProxyHandler) and h.proxies for h in default.handlers)
    assert any(isinstance(h, _RefuseRedirects) for h in opener.handlers)


def test_redirects_are_refused():
    handler = _RefuseRedirects()
    request = Request(API_URL)
    with pytest.raises(EgressRefused):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/")
    opener = FakeOpener(FakeResponse(307, b"", {"Location": "https://evil.example/"}))
    with pytest.raises(EgressRefused):
        AllowlistedTransport("anthropic", opener=opener).post_json(API_URL, {}, {})


def test_response_size_is_bounded():
    from tessera_agent.anthropic_messages import TransportFailed

    big = FakeResponse(200, b"x" * (MAX_RESPONSE_BYTES + 10))
    with pytest.raises(TransportFailed, match="model_response_bound"):
        AllowlistedTransport("anthropic", opener=FakeOpener(big)).post_json(API_URL, {}, {})
    assert big.closed


def test_http_error_returns_status_headers_and_json():
    error = HTTPError(
        API_URL,
        429,
        "Too Many",
        {"Retry-After": "12"},
        io.BytesIO(b'{"error":{"type":"rate_limit_error","message":"x"}}'),
    )
    status, headers, body = AllowlistedTransport("anthropic", opener=FakeOpener(error)).post_json(
        API_URL, {}, {}
    )
    assert status == 429 and headers["retry-after"] == "12"
    assert body["error"]["type"] == "rate_limit_error"


# -- adapter -----------------------------------------------------------------------------------


def profile(**limits):
    found = {
        "max_output_tokens_per_call": 1000,
        "max_input_tokens_per_call": 5000,
        "max_cost_cents_per_call": 10,
        "daily_token_cap": 100000,
        "daily_cost_cap_cents": 500,
    }
    found.update(limits)
    return {
        "account_mode": "anthropic-api-key-ref",
        "model": {"providerID": "anthropic", "id": "claude-sonnet-4-5"},
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


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.timeouts = []

    def post_json(self, url, headers, body, *, timeout=None):
        self.calls.append((url, dict(headers), body))
        self.timeouts.append(timeout)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def count(n=100):
    return (200, {}, {"input_tokens": n})


def message(model="claude-sonnet-4-5", **usage):
    found = {"input_tokens": 100, "output_tokens": 50}
    found.update(usage)
    return (
        200,
        {"request-id": "req_secret"},
        {
            "id": "msg_1",
            "model": model,
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "COMPLETION-TEXT"}],
            "usage": found,
        },
    )


def adapter(creds, transport, **kwargs):
    return AnthropicMessagesReasoning(
        profile(**kwargs.pop("limits", {})), credentials_dir=creds, transport=transport, **kwargs
    )


def assert_no_secret(*things):
    for thing in things:
        text = repr(thing) + str(thing)
        assert SENTINEL not in text
        assert "COMPLETION-TEXT" not in text


def test_successful_call_preflights_then_one_messages_call(creds):
    transport = FakeTransport(count(), message(cache_read_input_tokens=10))
    sink = []
    reasoning = adapter(creds, transport, output_sink=lambda row, text: sink.append((row, text)))
    assert reasoning.verify() is True
    reasoning.create(ROW)
    reasoning.prompt(ROW, "hello")
    assert [c[0] for c in transport.calls] == [COUNT_URL, API_URL]
    for _, headers, _ in transport.calls:
        assert headers == {
            "x-api-key": SENTINEL,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
    assert transport.calls[0][2] == {
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "hello"}],
    }
    assert transport.calls[1][2] == {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": "hello"}],
    }
    observed = reasoning.status(ROW)
    assert observed["outcome"] == "succeeded"
    assert observed["tokens"] == 160
    assert observed["usage"] == {
        "input": 100,
        "output": 50,
        "cache_read": 10,
        "cache_write": 0,
        "reasoning": 0,
    }
    # (100*300 + 50*1500 + 10*30) / 1e6 cents
    assert observed["cost"] == pytest.approx(0.1053)
    assert observed["cost_cents_estimate_max"] == pytest.approx((100 * 300 + 1000 * 1500) / 1e6)
    assert observed["reported_model"] == "anthropic/claude-sonnet-4-5"
    assert observed["rate_limit"] is None
    assert "request_id" not in observed
    assert sink == [(ROW, "COMPLETION-TEXT")]
    assert_no_secret(observed, reasoning, reasoning.transport.__class__)
    assert "req_secret" not in repr(observed)
    reasoning.interrupt(ROW)


def test_dated_model_variant_is_accepted(creds):
    reasoning = adapter(creds, FakeTransport(count(), message(model="claude-sonnet-4-5-20250929")))
    reasoning.prompt(ROW, "hi")
    assert reasoning.status(ROW)["reported_model"] == "anthropic/claude-sonnet-4-5-20250929"


@pytest.mark.parametrize(
    "model", ["claude-haiku-4-5", "claude-sonnet-4-5-2025092", "claude-sonnet-4-5-latest", None]
)
def test_model_substitution_halts(creds, model):
    reasoning = adapter(creds, FakeTransport(count(), message(model=model)))
    with pytest.raises(RuntimeBlocked, match="model_substituted"):
        reasoning.prompt(ROW, "hi")
    with pytest.raises(RuntimeBlocked, match="usage_unknown"):
        reasoning.status(ROW)


def test_key_is_read_from_credentials_dir_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-must-not-be-used")
    transport = FakeTransport(count(), message())
    reasoning = adapter(tmp_path, transport)
    with pytest.raises(RuntimeBlocked, match="model_credential_unavailable") as caught:
        reasoning.verify()
    assert caught.value.request_sent is False
    with pytest.raises(RuntimeBlocked, match="model_credential_unavailable"):
        reasoning.prompt(ROW, "hi")
    assert transport.calls == []
    path = tmp_path / "anthropic-api-key"
    path.write_text(SENTINEL)
    path.chmod(0o644)
    with pytest.raises(RuntimeBlocked, match="model_credential_unavailable") as caught:
        reasoning.verify()
    assert_no_secret(caught.value)
    path.chmod(0o600)
    reasoning.prompt(ROW, "hi")
    assert transport.calls[1][1]["x-api-key"] == SENTINEL
    assert "env-key-must-not-be-used" not in repr(transport.calls)


@pytest.mark.parametrize(
    ("input_tokens", "limits", "budget"),
    [
        (5001, {}, None),  # input cap
        (100, {"max_cost_cents_per_call": 1}, None),  # worst case 1.53 cents > 1
        (100, {}, lambda tokens, cents: False),  # daily/grant budget
        (100, {}, lambda tokens, cents: (_ for _ in ()).throw(ValueError())),
    ],
)
def test_cap_refusals_make_no_messages_call(creds, input_tokens, limits, budget):
    transport = FakeTransport(count(input_tokens), message())
    reasoning = adapter(creds, transport, limits=limits, budget_check=budget)
    with pytest.raises(RuntimeBlocked, match="model_call_cap_exceeded") as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.request_sent is False
    assert [c[0] for c in transport.calls] == [COUNT_URL]
    assert_no_secret(caught.value)


def test_budget_check_receives_worst_case_and_can_raise_runtime_code(creds):
    seen = []

    def budget(tokens, cents):
        seen.append((tokens, cents))
        raise RuntimeBlocked("daily_cap_exceeded")

    transport = FakeTransport(count(100), message())
    with pytest.raises(RuntimeBlocked, match="daily_cap_exceeded"):
        adapter(creds, transport, budget_check=budget).prompt(ROW, "hi")
    assert seen == [(1100, pytest.approx(1.53))]
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("status", "kind", "condition"),
    [
        (429, "rate_limit_error", "model_rate_limited"),
        (429, None, "model_rate_limited"),
        (529, "overloaded_error", "model_rate_limited"),
        (500, "overloaded_error", "model_rate_limited"),
        (400, "billing_error", "model_quota_exhausted"),
        (402, None, "model_quota_exhausted"),
    ],
)
def test_limit_errors_map_to_conditions_with_retry_after(creds, status, kind, condition):
    body = {"type": "error", "error": {"type": kind, "message": SENTINEL + " upstream text"}}
    transport = FakeTransport(count(), (status, {"retry-after": "42"}, body))
    reasoning = adapter(creds, transport)
    with pytest.raises(ModelConditionBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert str(caught.value) == condition
    assert caught.value.retry_after == 42.0
    assert caught.value.request_sent is False
    assert_no_secret(caught.value)
    assert "upstream" not in repr(caught.value)


def test_rate_limit_reset_header_used_when_retry_after_absent(creds):
    now = 1_800_000_000.0
    reset = datetime.fromtimestamp(now + 30, UTC).isoformat().replace("+00:00", "Z")
    body = {"error": {"type": "rate_limit_error"}}
    transport = FakeTransport(count(), (429, {"anthropic-ratelimit-tokens-reset": reset}, body))
    reasoning = adapter(creds, transport, clock=lambda: now)
    with pytest.raises(ModelConditionBlocked) as caught:
        reasoning.prompt(ROW, "hi")
    assert caught.value.retry_after == pytest.approx(30.0)


def test_rate_limit_on_preflight_makes_no_messages_call(creds):
    transport = FakeTransport((429, {}, {"error": {"type": "rate_limit_error"}}))
    with pytest.raises(ModelConditionBlocked, match="model_rate_limited") as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert caught.value.retry_after is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": {"type": "invalid_request_error", "message": SENTINEL}}),
        (401, {"error": {"type": "authentication_error"}}),
        (500, {"error": {"type": "api_error"}}),
        (404, {}),
    ],
)
def test_other_errors_are_model_call_failed(creds, status, body):
    transport = FakeTransport(count(), (status, {}, body))
    with pytest.raises(RuntimeBlocked) as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert str(caught.value) == "model_call_failed"
    assert not isinstance(caught.value, ModelConditionBlocked)
    assert_no_secret(caught.value)


def test_transport_exception_on_messages_call_is_uncertain(creds):
    transport = FakeTransport(count(), TimeoutError("timed out with " + SENTINEL))
    with pytest.raises(RuntimeBlocked) as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert str(caught.value) == "model_call_uncertain"
    assert caught.value.request_sent is None
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert_no_secret(caught.value)


def test_transport_exception_on_preflight_is_failed_not_billed(creds):
    transport = FakeTransport(OSError("boom"))
    with pytest.raises(RuntimeBlocked, match="model_call_failed") as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert caught.value.request_sent is False


def test_egress_refusal_is_sanitized(creds):
    transport = FakeTransport(count(), EgressRefused("model_egress_refused"))
    with pytest.raises(RuntimeBlocked, match="model_egress_refused"):
        adapter(creds, transport).prompt(ROW, "hi")


def test_adapter_with_real_transport_and_fake_opener_end_to_end(creds):
    opener = FakeOpener(FakeResponse(200, {"input_tokens": 10}), FakeResponse(200, message()[2]))
    reasoning = adapter(creds, AllowlistedTransport("anthropic", opener=opener))
    reasoning.prompt(ROW, "hi")
    urls = [r.full_url for r, _ in opener.calls]
    assert urls == [COUNT_URL, API_URL]
    sent = {k.lower(): v for k, v in opener.calls[1][0].header_items()}
    assert sent == {
        "x-api-key": SENTINEL,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    assert_no_secret(reasoning.status(ROW), reasoning, reasoning.transport)


def test_completion_text_never_in_status_without_sink(creds):
    reasoning = adapter(creds, FakeTransport(count(), message()))
    reasoning.prompt(ROW, "hi")
    assert "COMPLETION-TEXT" not in json.dumps(reasoning.status(ROW))


def test_unaccountable_success_is_uncertain(creds):
    transport = FakeTransport(count(), (200, {}, {"model": "claude-sonnet-4-5", "content": []}))
    with pytest.raises(RuntimeBlocked, match="model_call_uncertain"):
        adapter(creds, transport).prompt(ROW, "hi")


@pytest.mark.parametrize(
    "change",
    [
        {"limits": None},
        {"model": {"providerID": "openai", "id": "gpt-5"}},
        {"permissions": [{"action": "*", "resource": "*", "effect": "allow"}]},
        {
            "pricing_cents_per_mtok": {
                "input": 1,
                "output": float("nan"),
                "cache_read": 0,
                "cache_write": 0,
            }
        },
    ],
)
def test_invalid_static_config_is_refused(creds, change):
    found = profile() | change
    with pytest.raises(ValueError):
        AnthropicMessagesReasoning(found, credentials_dir=creds, transport=FakeTransport())


@pytest.mark.parametrize(
    "limits",
    [
        {"max_output_tokens_per_call": 32769},
        {"max_cost_cents_per_call": 5001},
        {"daily_cost_cap_cents": 100001},
        {"daily_token_cap": 0},
        {"max_input_tokens_per_call": True},
    ],
)
def test_limit_bounds(creds, limits):
    with pytest.raises(ValueError):
        AnthropicMessagesReasoning(
            profile(**limits), credentials_dir=creds, transport=FakeTransport()
        )


def test_usage_accumulates_per_row(creds):
    transport = FakeTransport(count(), message(), count(), message())
    reasoning = adapter(creds, transport)
    reasoning.prompt(ROW, "one")
    reasoning.prompt(ROW, "two")
    observed = reasoning.status(ROW)
    assert observed["tokens"] == 300
    assert observed["cost"] == pytest.approx(2 * 0.105)


# -- billed calls that halt: what the supervisor receipts (review rv-rmd3 finding 1) ----------

WORST_CENTS = (100 * 300 + 1000 * 1500) / 1e6  # counted input + output cap at pinned prices


def assert_reserved(billed, outcome_class):
    assert billed == {
        "outcome_class": outcome_class,
        "cost_basis": "reserved_worst_case_estimate",
        "usage": {"input": 100, "output": 1000, "cache_read": 0, "cache_write": 0, "reasoning": 0},
        "tokens": 1100,
        "cost": pytest.approx(WORST_CENTS),
        "reported_model": None,
        "cost_cents_estimate_max": pytest.approx(WORST_CENTS),
    }


def test_timeout_after_sending_carries_worst_case_reservation(creds):
    transport = FakeTransport(count(), TimeoutError("timed out with " + SENTINEL))
    with pytest.raises(RuntimeBlocked, match="model_call_uncertain") as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert_reserved(caught.value.billed_call, "uncertain")
    assert_no_secret(caught.value, caught.value.billed_call)


def test_unaccountable_success_carries_worst_case_reservation(creds):
    transport = FakeTransport(count(), (200, {}, {"model": "claude-sonnet-4-5", "content": []}))
    with pytest.raises(RuntimeBlocked, match="model_call_uncertain") as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert_reserved(caught.value.billed_call, "uncertain")


def test_substituted_model_carries_actual_usage_and_reported_model(creds):
    transport = FakeTransport(count(), message(model="claude-haiku-4-5", output_tokens=900))
    with pytest.raises(RuntimeBlocked, match="model_substituted") as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    billed = caught.value.billed_call
    assert billed["outcome_class"] == "model_substituted"
    assert billed["cost_basis"] == "computed_from_pinned_prices"
    assert billed["usage"] == {
        "input": 100,
        "output": 900,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
    }
    assert billed["tokens"] == 1000
    assert billed["cost"] == pytest.approx((100 * 300 + 900 * 1500) / 1e6)
    assert billed["reported_model"] == "anthropic/claude-haiku-4-5"
    assert billed["cost_cents_estimate_max"] == pytest.approx(WORST_CENTS)
    assert caught.value.request_sent is None
    assert_no_secret(caught.value, billed)


@pytest.mark.parametrize("model", [None, "Not A Model Id!"])
def test_substituted_model_never_echoes_an_invalid_identifier(creds, model):
    transport = FakeTransport(count(), message(model=model))
    with pytest.raises(RuntimeBlocked, match="model_substituted") as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert caught.value.billed_call["reported_model"] is None
    assert caught.value.billed_call["tokens"] == 150


def test_substituted_model_without_usage_reserves_worst_case(creds):
    reply = (200, {}, {"model": "claude-haiku-4-5", "content": []})
    with pytest.raises(RuntimeBlocked, match="model_substituted") as caught:
        adapter(creds, FakeTransport(count(), reply)).prompt(ROW, "hi")
    assert_reserved(caught.value.billed_call, "model_substituted")


def test_output_unrecorded_carries_actual_usage(creds):
    def sink(row, text):
        raise OSError("disk full")

    transport = FakeTransport(count(), message())
    with pytest.raises(RuntimeBlocked, match="model_output_unrecorded") as caught:
        adapter(creds, transport, output_sink=sink).prompt(ROW, "hi")
    billed = caught.value.billed_call
    assert (billed["outcome_class"], billed["cost_basis"]) == (
        "output_unrecorded",
        "computed_from_pinned_prices",
    )
    assert billed["tokens"] == 150
    assert billed["cost"] == pytest.approx((100 * 300 + 50 * 1500) / 1e6)
    assert billed["reported_model"] == "anthropic/claude-sonnet-4-5"
    assert_no_secret(caught.value, billed)


@pytest.mark.parametrize(
    "error", [EgressRefused("model_egress_refused"), (429, {}, {}), (400, {}, {})]
)
def test_definite_or_unbilled_failures_carry_no_billed_call(creds, error):
    with pytest.raises(RuntimeBlocked) as caught:
        adapter(creds, FakeTransport(count(), error)).prompt(ROW, "hi")
    assert not hasattr(caught.value, "billed_call")


# -- timeouts (review rv-rmd3 finding 2) --------------------------------------------------------


@pytest.mark.parametrize(
    "max_output,expected", [(1000, 100.0), (4096, 223.84), (13500, 600.0), (32768, 600.0)]
)
def test_billed_call_timeout_scales_with_output_cap(creds, max_output, expected):
    transport = FakeTransport(count(10), message())
    limits = {"max_output_tokens_per_call": max_output, "max_cost_cents_per_call": 5000}
    adapter(creds, transport, limits=limits).prompt(ROW, "hi")
    assert messages_timeout(max_output) == pytest.approx(expected)
    assert transport.timeouts == [COUNT_TIMEOUT_SECONDS, pytest.approx(expected)]
    assert COUNT_TIMEOUT_SECONDS == 30


def test_transport_passes_per_request_timeout_to_the_opener():
    opener = FakeOpener(FakeResponse(), FakeResponse())
    transport = AllowlistedTransport("anthropic", opener=opener)
    transport.post_json(API_URL, {}, {}, timeout=412.5)
    transport.post_json(COUNT_URL, {}, {})
    assert [timeout for _, timeout in opener.calls] == [412.5, COUNT_TIMEOUT_SECONDS]


def test_real_transport_end_to_end_uses_derived_timeouts(creds):
    opener = FakeOpener(FakeResponse(200, {"input_tokens": 10}), FakeResponse(200, message()[2]))
    reasoning = adapter(creds, AllowlistedTransport("anthropic", opener=opener))
    reasoning.prompt(ROW, "hi")
    assert [timeout for _, timeout in opener.calls] == [30, pytest.approx(100.0)]


@pytest.mark.parametrize("configured", [30, 45, 600])
def test_configured_request_timeout_overrides_the_derived_default(creds, configured):
    transport = FakeTransport(count(10), message())
    limits = {"max_output_tokens_per_call": 32768, "request_timeout_seconds": configured}
    limits["max_cost_cents_per_call"] = 5000
    adapter(creds, transport, limits=limits).prompt(ROW, "hi")
    assert transport.timeouts == [COUNT_TIMEOUT_SECONDS, configured]


@pytest.mark.parametrize("bad", [0, 29, 601, 60.0, "120", True, None])
def test_invalid_request_timeout_is_refused_without_echo(creds, bad):
    with pytest.raises(ValueError) as caught:
        adapter(creds, FakeTransport(), limits={"request_timeout_seconds": bad})
    assert str(caught.value) == "anthropic_limits_invalid"


@pytest.mark.parametrize("status", [500, 502, 503, 504, 302 + 300])
def test_unknown_outcome_status_on_billed_call_reserves_worst_case(creds, status):
    transport = FakeTransport(count(), (status, {}, {"error": {"type": "api_error"}}))
    with pytest.raises(RuntimeBlocked, match="model_call_failed") as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert caught.value.request_sent is None
    assert_reserved(caught.value.billed_call, "uncertain")


@pytest.mark.parametrize(
    "status,kind",
    [
        (400, "invalid_request_error"),
        (401, "authentication_error"),
        (403, "permission_error"),
        (404, "not_found_error"),
        (413, "request_too_large"),
    ],
)
def test_request_errors_on_billed_call_carry_no_billed_call(creds, status, kind):
    transport = FakeTransport(count(), (status, {}, {"error": {"type": kind}}))
    with pytest.raises(RuntimeBlocked, match="model_call_failed") as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert not hasattr(caught.value, "billed_call")


@pytest.mark.parametrize("status", [500, 401])
def test_count_tokens_failures_carry_no_billed_call(creds, status):
    transport = FakeTransport((status, {}, {}))
    with pytest.raises(RuntimeBlocked) as caught:
        adapter(creds, transport).prompt(ROW, "hi")
    assert not hasattr(caught.value, "billed_call")
    assert [c[0] for c in transport.calls] == [COUNT_URL]
