"""A single run makes one billed model request per step and never retries on failure.

Fakes only: no network, no provider call, no key.  A transport that fails every request
counts the requests; a retry would show up as a second billed request.
"""

import pytest

from tessera_agent.anthropic_messages import API_URL, COUNT_URL, AnthropicMessagesReasoning
from tessera_agent.remote_reasoning_runtime import RuntimeBlocked

ROW = {"id": "run-1"}
KEY = "sk-ant-FAKE-not-a-real-key-0123456789"
RATE_LIMIT = {"error": {"type": "rate_limit_error", "message": "slow down"}}


class AlwaysFailingTransport:
    """Free token count succeeds; every billed request fails the same way, every time."""

    def __init__(self, failure):
        self.failure = failure
        self.calls = []

    def post_json(self, url, headers, body, *, timeout=None):
        self.calls.append(url)
        if url == COUNT_URL:
            return 200, {}, {"input_tokens": 100}
        if isinstance(self.failure, BaseException):
            raise self.failure
        return self.failure

    @property
    def billed(self):
        return [url for url in self.calls if url == API_URL]


def run_once(tmp_path, transport):
    (tmp_path / "anthropic-api-key").write_text(KEY + "\n")
    (tmp_path / "anthropic-api-key").chmod(0o600)
    receipts, sinks = [], []
    reasoning = AnthropicMessagesReasoning(
        {
            "account_mode": "anthropic-api-key-ref",
            "model": {"providerID": "anthropic", "id": "claude-sonnet-4-5"},
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
            "limits": {
                "max_output_tokens_per_call": 1000,
                "max_input_tokens_per_call": 5000,
                "max_cost_cents_per_call": 10,
                "daily_token_cap": 100000,
                "daily_cost_cap_cents": 500,
            },
            "pricing_cents_per_mtok": {
                "input": 300,
                "output": 1500,
                "cache_read": 30,
                "cache_write": 375,
            },
        },
        credentials_dir=tmp_path,
        transport=transport,
        on_model_call=receipts.append,
        output_sink=lambda row, text: sinks.append(text),
    )
    reasoning.create(ROW)
    with pytest.raises(RuntimeBlocked) as caught:
        reasoning.prompt(ROW, "hello")
    return caught.value, receipts, sinks


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (TimeoutError("timed out"), "model_call_uncertain"),
        (ConnectionResetError("reset"), "model_call_uncertain"),
        ((429, {"retry-after": "1"}, RATE_LIMIT), "model_rate_limited"),
        ((529, {}, {"error": {"type": "overloaded_error"}}), "model_rate_limited"),
        ((500, {}, {"error": {"type": "api_error"}}), "model_call_failed"),
        ((503, {}, {}), "model_call_failed"),
        ((400, {}, {"error": {"type": "invalid_request_error"}}), "model_call_failed"),
    ],
)
def test_failed_billed_request_is_sent_once_and_surfaces(tmp_path, failure, reason):
    transport = AlwaysFailingTransport(failure)
    error, receipts, sinks = run_once(tmp_path, transport)
    assert str(error) == reason
    assert transport.calls == [COUNT_URL, API_URL]  # one free count, one billed request
    assert len(transport.billed) == 1
    assert sinks == []  # no output was produced or recorded
    assert len(receipts) <= 1  # any spend is recorded at most once, never per attempt


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), (500, {}, {"error": {"type": "api_error"}})],
)
def test_unknown_outcome_records_reserved_spend_exactly_once(tmp_path, failure):
    transport = AlwaysFailingTransport(failure)
    error, receipts, _ = run_once(tmp_path, transport)
    assert error.request_sent is None  # outcome unknown: surfaced, not retried
    billed = error.billed_call
    assert isinstance(billed, dict) and billed["turn"] == 1
    assert len(transport.billed) == 1
