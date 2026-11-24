"""Supervisor/store integration for the direct Anthropic Messages API runtime.

Synthetic only: a fake transport stands in for the allowlisted HTTPS transport, the API
key is a sentinel string in a private temporary file, and no network call is made.
"""

import copy
import hashlib
import json
import math
import os
from datetime import UTC, datetime

import pytest

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.anthropic_messages import COUNT_URL, AnthropicMessagesReasoning
from tessera_agent.remote_reasoning_runtime import (
    ReasoningSupervisor,
    RuntimeBlocked,
    validate_profile,
)
from tessera_agent.remote_reasoning_store import ReasoningStore
from tessera_agent.service_startup import launch_spec

API_KEY = "sk-ant-SENTINEL-API-KEY-do-not-leak"
PROMPT = "SENTINEL-PROMPT-TEXT-do-not-journal"
COMPLETION = "SENTINEL-COMPLETION-TEXT-do-not-journal"
REF = "anthropic-api-key://owner1/SENTINEL-REFERENCE"
MODEL = "claude-sonnet-4-5"
DAY1 = datetime(2026, 11, 22, 10, tzinfo=UTC).timestamp()
DAY2 = datetime(2026, 11, 23, 0, 0, 1, tzinfo=UTC).timestamp()
SOURCE = hashlib.sha256(b"source").hexdigest()
DENY = [{"action": "*", "resource": "*", "effect": "deny"}]


def make_profile(run_id="run1", **extra):
    profile = {
        "run_id": run_id,
        "grant_id": "g1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "agent1",
        "destination": "lab",
        "account_mode": "anthropic-api-key-ref",
        "credential_ref": REF,
        "model": {"providerID": "anthropic", "id": MODEL},
        "max_steps": 3,
        "max_tokens": 5000,
        "max_duration_seconds": 3600,
        "permissions": copy.deepcopy(DENY),
        "limits": {
            "max_output_tokens_per_call": 1000,
            "max_input_tokens_per_call": 500,
            "max_cost_cents_per_call": 10,
            "daily_token_cap": 1200,
            "daily_cost_cap_cents": 100,
        },
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
    }
    profile.update(extra)
    return profile


def private(path, content):
    path.write_text(content)
    path.chmod(0o600)
    return path


class Time:
    def __init__(self, now=DAY1):
        self.now = now

    def __call__(self):
        return self.now


def ok(model=MODEL, input_tokens=100, output_tokens=50, text=COMPLETION):
    return (
        200,
        {},
        {
            "model": model,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "content": [{"type": "text", "text": text}],
        },
    )


class Transport:
    """Fake allowlisted transport: count_tokens always answers; messages pop replies."""

    def __init__(self, *replies, input_tokens=100):
        self.replies, self.input_tokens, self.sent = list(replies), input_tokens, []

    def post_json(self, url, headers, body, *, timeout=None):
        self.sent.append((url, dict(headers), json.dumps(body)))
        self.timeouts = [*getattr(self, "timeouts", []), timeout]
        if url == COUNT_URL:
            return 200, {}, {"input_tokens": self.input_tokens}
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def messages(self):
        return [s for s in self.sent if s[0] != COUNT_URL]


def grant(remaining=50):
    return {
        "revision": 1,
        "epoch": 1,
        "status": "active",
        "effective_status": "active",
        "remaining_budget_cents": remaining,
        "bounds": {
            "subject_credential_id": "agent1",
            "destination": "lab",
            "model": MODEL,
            "budget_cents": 100,
            "max_duration_seconds": 7200,
            "expires_at": "2100-01-01T00:00:00+00:00",
        },
    }


@pytest.fixture
def creds(tmp_path):
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    private(root / "anthropic-api-key", API_KEY + "\n")
    return root


def setup(
    tmp_path,
    creds,
    transport,
    *,
    profile=None,
    clock=None,
    remaining=50,
    lookup=None,
):
    profile = profile or make_profile()
    clock = clock or Time()
    store = ReasoningStore(tmp_path / "state")
    binding = {
        k: profile[k] for k in ("grant_id", "revision", "epoch", "subject_credential_id")
    } | {
        "destination": "lab",
        "model": MODEL,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "profile_sha256": hashlib.sha256(b"profile").hexdigest(),
    }
    store.bind(profile["run_id"], binding, clock())
    adapter = AnthropicMessagesReasoning(
        profile, credentials_dir=creds, transport=transport, clock=clock
    )
    authority = grant(remaining)
    supervisor = ReasoningSupervisor(
        store, adapter, lambda _: authority, profile, clock=clock, holdout_lookup=lookup
    )
    return store, supervisor, clock, authority


def journal_bytes(tmp_path):
    state = tmp_path / "state"
    return b"".join(p.read_bytes() for p in state.iterdir() if p.is_file())


def assert_no_secrets(store, tmp_path, run_id="run1", extra=""):
    store.db.commit()
    blob = journal_bytes(tmp_path) + "\n".join(store.db.iterdump()).encode()
    texts = json.dumps(store.model_calls(run_id)) + json.dumps(store.provenance_pending(run_id, 64))
    for secret in (API_KEY, PROMPT, COMPLETION, "SENTINEL-REFERENCE"):
        assert secret.encode() not in blob
        assert secret not in texts + extra


# -- profile validation --------------------------------------------------------------------


def test_valid_anthropic_profile_needs_no_harness_fields():
    profile = make_profile()
    for field in ("agent", "harness_url", "harness_token_env", "workspace", "schema_sha256"):
        assert field not in profile
    validate_profile(profile)
    validate_profile(make_profile(agent="researcher"))


def mutations():
    def drop(*path):
        def apply(p):
            target = p
            for key in path[:-1]:
                target = target[key]
            target.pop(path[-1])

        return apply

    def put(value, *path):
        def apply(p):
            target = p
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value

        return apply

    return [
        drop("limits"),
        drop("limits", "daily_token_cap"),
        put(0, "limits", "max_input_tokens_per_call"),
        put(-1, "limits", "daily_token_cap"),
        put(1.5, "limits", "daily_cost_cap_cents"),
        put(True, "limits", "max_cost_cents_per_call"),
        put(32769, "limits", "max_output_tokens_per_call"),
        put(5001, "limits", "max_cost_cents_per_call"),
        put(100001, "limits", "daily_cost_cap_cents"),
        drop("pricing_cents_per_mtok"),
        drop("pricing_cents_per_mtok", "cache_write"),
        put(-1, "pricing_cents_per_mtok", "input"),
        put(math.nan, "pricing_cents_per_mtok", "output"),
        put(math.inf, "pricing_cents_per_mtok", "output"),
        put("300", "pricing_cents_per_mtok", "input"),
        put(1, "pricing_cents_per_mtok", "extra"),
        put(
            DENY + [{"action": "tessera_propose", "resource": "*", "effect": "allow"}],
            "permissions",
        ),
        put([{"action": "*", "resource": "*", "effect": "allow"}], "permissions"),
        put([], "permissions"),
        drop("credential_ref"),
        put("sk-ant-" + API_KEY, "credential_ref"),
        put("claude-subscription://owner1/ref", "credential_ref"),
        put("claude-subscription-ref", "account_mode"),
        put(0, "max_tokens"),
        put("claude-unknown-x", "model", "id"),
        put("claude-fable-1", "model", "id"),
    ]


@pytest.mark.parametrize("mutate", mutations())
def test_invalid_anthropic_profile_refused_without_echo(mutate):
    profile = make_profile()
    mutate(profile)
    with pytest.raises(ValueError) as caught:
        validate_profile(profile)
    assert API_KEY not in str(caught.value) and "SENTINEL" not in str(caught.value)


def test_openai_profile_rules_unchanged():
    profile = {
        "run_id": "run1",
        "agent": "researcher",
        "grant_id": "g1",
        "subject_credential_id": "agent1",
        "account_mode": "included-chatgpt-oauth",
        "model": {"providerID": "openai", "id": "gpt-model"},
        "max_steps": 3,
        "max_tokens": 100,
        "max_duration_seconds": 60,
        "permissions": DENY + [{"action": "tessera_propose", "resource": "*", "effect": "allow"}],
    }
    validate_profile(profile)  # No limits/pricing needed; tool allows still admitted.
    with pytest.raises(ValueError):
        validate_profile({k: v for k, v in profile.items() if k != "agent"})
    with pytest.raises(ValueError):
        validate_profile({**profile, "credential_ref": REF})


# -- per-call receipts, output sink, secrets --------------------------------------------------


def test_successful_call_receipt_output_file_and_no_secrets(tmp_path, creds):
    transport = Transport(ok())
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    (call,) = store.model_calls("run1")
    assert call["seq"] == 1
    assert call["cost_basis"] == "computed_from_pinned_prices"
    assert call["model_configured"] == "anthropic/" + MODEL
    assert call["model_reported"] == "anthropic/" + MODEL
    assert call["tokens"]["input"] == 100 and call["tokens"]["output"] == 50
    assert call["tokens"]["total"] == 150
    assert call["cost"] == pytest.approx((100 * 300 + 50 * 1500) / 1e6)
    assert call["cost_cents_estimate_max"] == pytest.approx((100 * 300 + 1000 * 1500) / 1e6)
    assert call["limits"] == make_profile()["limits"]
    assert call["daily_tokens_after"] == 150
    assert call["daily_cost_cents_after"] == pytest.approx(call["cost"])
    assert call["tier"] == "sonnet"
    output = tmp_path / "state" / "outputs" / "run1-1.txt"
    assert output.read_text() == COMPLETION
    assert output.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state" / "outputs").stat().st_mode & 0o077 == 0
    assert call["output_sha256"] == hashlib.sha256(COMPLETION.encode()).hexdigest()
    # The key goes only into the x-api-key header of allowlisted requests.
    for _, headers, body in transport.sent:
        assert headers["x-api-key"] == API_KEY
        assert set(headers) == {"x-api-key", "anthropic-version", "content-type"}
        assert API_KEY not in body
    assert_no_secrets(store, tmp_path)
    assert API_KEY not in repr(supervisor.harness)


def test_receipts_are_append_only_rows_with_seq(tmp_path, creds):
    store, _, _, _ = setup(tmp_path, creds, Transport())
    first = store.model_call("run1", {"tier": "sonnet", "cost": 1.0}, DAY1, tokens_delta=10)
    second = store.model_call(
        "run1", {"tier": "sonnet", "cost": 1.0}, DAY1 + 1, tokens_delta=5, cost_cents_delta=2.5
    )
    assert (first, second) == (1, 2)
    rows = store.model_calls("run1")
    assert [r["seq"] for r in rows] == [1, 2] and rows[0]["observed_at"] == DAY1
    assert store.daily_usage("2026-11-22") == {"tokens": 15, "cost_cents": 2.5}
    assert store.daily_usage("2026-11-23") == {"tokens": 0, "cost_cents": 0.0}
    with pytest.raises(ValueError):
        store.model_call("run1", {"completion": COMPLETION}, DAY1)
    with pytest.raises(ValueError):
        store.model_call("run1", {"tier": "x"}, DAY1, tokens_delta=-1)


# -- daily caps ----------------------------------------------------------------------------


def test_daily_cap_spans_runs_and_resets_next_utc_day(tmp_path, creds):
    transport = Transport(ok(), ok(), ok())
    store, first, clock, _ = setup(tmp_path, creds, transport)
    assert first.tick("run1", PROMPT)["state"] == "completed"  # 150 tokens today
    store.close()
    # Second run, same state dir and day: worst case 150 + 100 + 1000 > 1200.
    store, second, _, _ = setup(
        tmp_path, creds, transport, profile=make_profile("run2"), clock=clock
    )
    row = second.tick("run2", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_cap_exceeded")
    assert len(transport.messages()) == 1  # nothing sent to /v1/messages
    assert store.effect("run2", "prompt") == "not_sent"
    assert store.model_calls("run2") == []
    store.close()
    # Next UTC day: the ledger is empty again.
    clock.now = DAY2
    store, third, _, _ = setup(
        tmp_path, creds, transport, profile=make_profile("run3"), clock=clock
    )
    assert third.tick("run3", PROMPT)["state"] == "completed"
    assert len(transport.messages()) == 2
    (call,) = store.model_calls("run3")
    assert call["daily_tokens_after"] == 150
    assert store.daily_usage("2026-11-22")["tokens"] == 150


def test_daily_cap_rechecked_after_the_call(tmp_path, creds):
    # The count preflight under-reported; the actual usage exceeds the day's cap.
    transport = Transport(ok(input_tokens=1100, output_tokens=200))
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "daily_cap_exceeded"
    (call,) = store.model_calls("run1")
    assert call["daily_tokens_after"] == 1300


# -- grant budget fence ----------------------------------------------------------------------


def test_grant_budget_fences_billed_cost(tmp_path, creds):
    transport = Transport(ok())
    store, supervisor, _, authority = setup(tmp_path, creds, transport, remaining=1)
    # Worst case 1.53 cents exceeds the grant's 1 remaining cent: refused before sending.
    assert supervisor.tick("run1", PROMPT)["reason"] == "model_call_cap_exceeded"
    assert transport.messages() == []
    store.close()


def test_billed_run_cost_above_grant_remaining_halts(tmp_path, creds):
    transport = Transport(ok())
    store, supervisor, _, authority = setup(tmp_path, creds, transport)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    row = store.get("run1")
    assert row["cost"] > 0  # billed cost alone is not a usage fence
    authority["remaining_budget_cents"] = row["cost"] / 2
    with pytest.raises(RuntimeBlocked, match="budget_exhausted"):
        supervisor.fence(row)


def test_non_billed_openai_fence_unchanged(tmp_path):
    class Harness:
        def verify(self):
            return True

        def create(self, row):
            pass

        def prompt(self, row, text):
            pass

        def status(self, row):
            return {"tokens": 5, "cost": 0.01, "outcome": "succeeded"}

        def interrupt(self, row):
            pass

    profile = {
        "run_id": "run1",
        "account_mode": "included-chatgpt-oauth",
        "model": {"providerID": "openai", "id": "gpt-model"},
        "max_tokens": 1000,
        "max_duration_seconds": 3600,
        "permissions": DENY,
    }
    store = ReasoningStore(tmp_path / "state")
    store.bind(
        "run1",
        {"grant_id": "g1", "revision": 1, "epoch": 1, "subject_credential_id": "agent1",
         "destination": "lab", "model": MODEL,
         "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest()},
        DAY1,
    )  # fmt: skip
    supervisor = ReasoningSupervisor(store, Harness(), lambda _: grant(), profile, clock=Time())
    assert supervisor.tick("run1", PROMPT)["reason"] == "usage_fence"
    (call,) = store.model_calls("run1")
    assert call["cost_basis"] == "reported_cost"
    assert store.daily_usage("2026-11-22")["cost_cents"] == 0


# -- rejections, uncertainty, substitution ---------------------------------------------------


@pytest.mark.parametrize(
    "reply,condition,wait",
    [
        (
            (429, {"retry-after": "30"}, {"error": {"type": "rate_limit_error"}}),
            "model_rate_limited",
            30,
        ),
        ((529, {}, {"error": {"type": "overloaded_error"}}), "model_rate_limited", 60),
        (
            (400, {"retry-after": "120"}, {"error": {"type": "billing_error"}}),
            "model_quota_exhausted",
            120,
        ),
        ((402, {}, {}), "model_quota_exhausted", 3600),
    ],
)
def test_definite_rejection_backs_off_then_prompts_again(tmp_path, creds, reply, condition, wait):
    transport = Transport(reply, ok())
    store, supervisor, clock, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "ready" and row["reason"] is None
    assert store.effect("run1", "prompt") == "not_sent"
    assert store.latest_rate_limit("run1") == {
        "condition": condition,
        "backoff_until": DAY1 + wait,
    }
    clock.now = DAY1 + wait - 1
    supervisor.tick("run1", PROMPT)
    assert len(transport.messages()) == 1  # still backing off: nothing sent
    clock.now = DAY1 + wait
    if wait >= make_profile()["max_duration_seconds"]:
        # Backoff outlived the run's duration bound: halted, never re-sent.
        assert supervisor.tick("run1", PROMPT)["reason"] == "duration_expired"
        assert len(transport.messages()) == 1
        return
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert len(transport.messages()) == 2  # the same prompt, sent again
    assert transport.messages()[0][2] == transport.messages()[1][2]
    assert store.attempts("run1", "prompt") == 2
    (call,) = store.model_calls("run1")  # only the accepted call is receipted
    assert call["model_reported"] == "anthropic/" + MODEL
    records = [json.loads(r["body"])["record"] for r in store.provenance_pending("run1", 64)]
    assert sum("rate_limit" in r for r in records) == 1  # one per event
    assert any(r["error_code"] == condition and r["kind"] == "tool" for r in records)
    assert_no_secrets(store, tmp_path)


def test_repeated_rejections_are_bounded(tmp_path, creds):
    replies = [(429, {"retry-after": "1"}, {"error": {"type": "rate_limit_error"}})] * 8
    transport = Transport(*replies)
    store, supervisor, clock, _ = setup(tmp_path, creds, transport)
    for step in range(9):
        clock.now = DAY1 + step * 2
        row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_retry_exhausted"
    assert len(transport.messages()) == 8
    assert store.provenance_coverage("run1")["recorded"] <= 64


def test_uncertain_transport_failure_is_never_replayed(tmp_path, creds):
    transport = Transport(TimeoutError("SENTINEL-UPSTREAM-DETAIL"), ok())
    store, supervisor, clock, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "uncertain_prompt"
    assert store.effect("run1", "prompt") == "uncertain"
    clock.now += 7200
    supervisor.tick("run1", PROMPT)
    assert len(transport.messages()) == 1
    (call,) = store.model_calls("run1")  # the reservation, receipted once
    assert call["outcome_class"] == "uncertain"
    assert "SENTINEL-UPSTREAM-DETAIL" not in json.dumps(store.provenance_pending("run1", 64))
    assert "SENTINEL-UPSTREAM-DETAIL" not in json.dumps(store.model_calls("run1"))


def test_model_substitution_halts_and_model_never_changes(tmp_path, creds):
    transport = Transport(ok(model="claude-haiku-4-5"), ok())
    store, supervisor, clock, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_substituted"
    assert store.effect("run1", "prompt") == "uncertain"
    clock.now += 60
    supervisor.tick("run1", PROMPT)
    assert len(transport.messages()) == 1
    assert supervisor.profile["model"]["id"] == MODEL
    assert json.loads(store.get("run1")["binding"])["model"] == MODEL
    assert all(json.loads(body)["model"] == MODEL for _, _, body in transport.sent)
    (call,) = store.model_calls("run1")
    # The receipt names what the response reported; the configured model never changes.
    assert call["model_configured"] == "anthropic/" + MODEL
    assert call["model_reported"] == "anthropic/claude-haiku-4-5"


WORST_TOKENS = 100 + 1000  # counted input + max_output_tokens_per_call
WORST_CENTS = (100 * 300 + 1000 * 1500) / 1e6


@pytest.mark.parametrize(
    "reply",
    [
        TimeoutError("SENTINEL-UPSTREAM-DETAIL"),
        ConnectionResetError("SENTINEL-UPSTREAM-DETAIL"),
        (200, {}, {"model": MODEL, "content": [{"type": "text", "text": COMPLETION}]}),
    ],
)
def test_uncertain_call_reserves_worst_case_on_the_daily_ledger(tmp_path, creds, reply):
    # Review probe 1: after an uncertain call the day's ledger holds the reserved worst case.
    transport = Transport(reply, ok())
    store, supervisor, clock, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "uncertain_prompt")
    assert store.effect("run1", "prompt") == "uncertain"
    (call,) = store.model_calls("run1")
    assert call["cost_basis"] == "reserved_worst_case_estimate"
    assert call["outcome_class"] == "uncertain"
    assert call["tokens"] == {
        "input": 100,
        "output": 1000,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
        "total": WORST_TOKENS,
    }
    assert call["cost"] == pytest.approx(WORST_CENTS)
    assert call["cost_cents_estimate_max"] == pytest.approx(WORST_CENTS)
    assert call["model_reported"] is None and call["output_sha256"] is None
    assert call["daily_tokens_after"] == WORST_TOKENS
    assert call["daily_cost_cents_after"] == pytest.approx(WORST_CENTS)
    day = store.daily_usage("2026-11-22")
    assert day["tokens"] == WORST_TOKENS
    assert day["cost_cents"] == pytest.approx(WORST_CENTS)
    # The run cost the grant-budget fence reads includes the reservation.
    row = store.get("run1")
    assert (row["tokens"], row["cost"]) == (WORST_TOKENS, pytest.approx(WORST_CENTS))
    assert_no_secrets(store, tmp_path)
    assert "SENTINEL-UPSTREAM-DETAIL" not in json.dumps(store.model_calls("run1"))
    store.close()
    # A second run the same day: 1100 reserved + 1100 worst case > 1200, refused unsent.
    store, second, _, _ = setup(
        tmp_path, creds, transport, profile=make_profile("run2"), clock=clock
    )
    row = second.tick("run2", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_cap_exceeded")
    assert len(transport.messages()) == 1
    assert store.effect("run2", "prompt") == "not_sent"
    assert store.model_calls("run2") == []


@pytest.mark.parametrize("status,reserved", [(500, True), (503, True), (400, False), (401, False)])
def test_billed_non_200_reserves_only_when_the_outcome_is_unknown(
    tmp_path, creds, status, reserved
):
    transport = Transport((status, {}, {"error": {"type": "SENTINEL-UPSTREAM-DETAIL"}}))
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_call_failed"  # halt code kept
    assert store.effect("run1", "prompt") == "uncertain"
    calls = store.model_calls("run1")
    if reserved:
        (call,) = calls
        assert call["cost_basis"] == "reserved_worst_case_estimate"
        assert call["outcome_class"] == "uncertain"
        assert store.daily_usage("2026-11-22")["tokens"] == WORST_TOKENS
    else:
        assert calls == []
        assert store.daily_usage("2026-11-22") == {"tokens": 0, "cost_cents": 0.0}
    assert "SENTINEL-UPSTREAM-DETAIL" not in json.dumps(calls)


def test_reservation_alone_pushes_past_the_daily_cost_cap(tmp_path, creds):
    # Token cap generous; the reserved cents leave too little for the next worst case.
    limits = dict(make_profile()["limits"], daily_token_cap=10**6, daily_cost_cap_cents=2)
    transport = Transport(TimeoutError(), ok())
    store, first, clock, _ = setup(tmp_path, creds, transport, profile=make_profile(limits=limits))
    assert first.tick("run1", PROMPT)["reason"] == "uncertain_prompt"
    assert store.daily_usage("2026-11-22")["cost_cents"] == pytest.approx(WORST_CENTS)
    store.close()
    store, second, _, _ = setup(
        tmp_path, creds, transport, profile=make_profile("run2", limits=limits), clock=clock
    )
    assert second.tick("run2", PROMPT)["reason"] == "model_call_cap_exceeded"
    assert len(transport.messages()) == 1


def test_substituted_model_receipts_worst_case_and_counts_today(tmp_path, creds):
    # Review probe 2: a 200 naming another model (400 in / 900 out) is billed and receipted.
    # rv-rmd4 finding 1: priced at max(actual at pinned prices, reserved worst case) per
    # dimension: 1300 actual tokens > 1100 reserved, but 1.47 actual cents < 1.53 reserved.
    transport = Transport(ok(model="claude-haiku-4-5", input_tokens=400, output_tokens=900))
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_substituted"
    (call,) = store.model_calls("run1")
    actual = (400 * 300 + 900 * 1500) / 1e6
    assert actual < WORST_CENTS
    assert call["outcome_class"] == "model_substituted"
    assert call["cost_basis"] == "worst_case_for_substituted_model"
    assert call["model_reported"] == "anthropic/claude-haiku-4-5"
    assert call["tokens"]["input"] == 400 and call["tokens"]["output"] == 900
    assert call["tokens"]["total"] == 1300
    assert call["cost"] == pytest.approx(WORST_CENTS)
    assert call["output_sha256"] is None
    assert not (tmp_path / "state" / "outputs").exists()  # the completion is never kept
    assert call["daily_tokens_after"] == 1300
    day = store.daily_usage("2026-11-22")
    assert day["tokens"] == 1300 and day["cost_cents"] == pytest.approx(WORST_CENTS)
    assert store.get("run1")["cost"] == pytest.approx(WORST_CENTS)
    assert_no_secrets(store, tmp_path)


def test_output_unrecorded_receipts_actual_usage(tmp_path, creds):
    transport = Transport(ok())
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    (tmp_path / "state" / "outputs").mkdir(mode=0o755)  # not private: the sink refuses
    (tmp_path / "state" / "outputs").chmod(0o755)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_output_unrecorded"
    assert store.effect("run1", "prompt") == "uncertain"
    (call,) = store.model_calls("run1")
    assert call["outcome_class"] == "output_unrecorded"
    assert call["cost_basis"] == "computed_from_pinned_prices"
    assert call["tokens"]["total"] == 150
    assert call["cost"] == pytest.approx((100 * 300 + 50 * 1500) / 1e6)
    assert call["output_sha256"] is None
    assert store.daily_usage("2026-11-22")["tokens"] == 150
    assert_no_secrets(store, tmp_path)


def test_billed_call_timeout_follows_the_profile_output_cap(tmp_path, creds):
    limits = dict(make_profile()["limits"], max_output_tokens_per_call=4000)
    limits.update(max_cost_cents_per_call=100, daily_token_cap=10**6, daily_cost_cap_cents=1000)
    transport = Transport(ok())
    _, supervisor, _, _ = setup(
        tmp_path,
        creds,
        transport,
        profile=make_profile(limits=limits, max_tokens=32768),
        remaining=100,
    )
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert transport.timeouts == [30, pytest.approx(60 + 0.04 * 4000)]


def test_request_timeout_is_policy_only_when_present(tmp_path, creds):
    base = make_profile()
    configured = make_profile(limits=dict(base["limits"], request_timeout_seconds=240))
    validate_profile(configured)
    # Absent: the policy (and so every existing policy digest) is unchanged.
    assert "request_timeout_seconds" not in ReasoningSupervisor.policy(base)["limits"]
    assert ReasoningSupervisor.policy(configured)["limits"]["request_timeout_seconds"] == 240
    assert ReasoningSupervisor.policy(configured) != ReasoningSupervisor.policy(base)
    transport = Transport(ok())
    store, supervisor, _, _ = setup(tmp_path, creds, transport, profile=configured)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert transport.timeouts == [30, 240]
    (call,) = store.model_calls("run1")
    assert call["limits"]["request_timeout_seconds"] == 240


def test_dated_model_snapshot_is_accepted(tmp_path, creds):
    transport = Transport(ok(model=MODEL + "-20250929"))
    store, supervisor, _, _ = setup(tmp_path, creds, transport)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"


def test_restart_after_acknowledged_prompt_fails_closed(tmp_path, creds):
    transport = Transport(ok())
    store, supervisor, clock, _ = setup(tmp_path, creds, transport)
    supervisor.tick("run1", PROMPT)
    store.state("run1", "running")  # simulate a crash before the terminal state write
    store.close()
    store, resumed, _, _ = setup(tmp_path, creds, transport, clock=clock)
    assert resumed.tick("run1", PROMPT)["reason"] == "usage_unknown"
    assert len(transport.messages()) == 1


# -- sealed-holdout guard ----------------------------------------------------------------------


def test_sealed_input_halts_before_any_request(tmp_path, creds):
    seen = []

    def lookup(project, digest):
        seen.append((project, digest))
        return {"claims": [{"id": "claim"}] if digest == SOURCE else [], "truncated": False}

    profile = make_profile(project_id="project1", input_digests={"source": SOURCE})
    transport = Transport(ok())
    store, supervisor, _, _ = setup(tmp_path, creds, transport, profile=profile, lookup=lookup)
    assert supervisor.tick("run1", PROMPT)["reason"] == "model_input_sealed"
    assert transport.sent == []
    prompt_digest = hashlib.sha256(PROMPT.encode()).hexdigest()
    assert {d for _, d in seen} == {SOURCE, prompt_digest}
    assert {p for p, _ in seen} == {"project1"}


@pytest.mark.parametrize(
    "extra,lookup",
    [
        ({"input_digests": {"source": SOURCE}}, lambda p, d: {"claims": [], "truncated": False}),
        ({"project_id": "project1"}, None),
        ({"project_id": "project1"}, lambda p, d: {"claims": [], "truncated": True}),
        ({"project_id": "project1"}, lambda p, d: (_ for _ in ()).throw(RuntimeBlocked("x"))),
        ({"project_id": "project1"}, lambda p, d: {"unexpected": True}),
    ],
)
def test_unverifiable_seal_fails_closed(tmp_path, creds, extra, lookup):
    transport = Transport(ok())
    profile = make_profile(**extra)
    store, supervisor, _, _ = setup(tmp_path, creds, transport, profile=profile, lookup=lookup)
    assert supervisor.tick("run1", PROMPT)["reason"] == "model_input_seal_unknown"
    assert transport.sent == []


def test_guard_skipped_without_digests_or_project(tmp_path, creds):
    def lookup(project, digest):
        raise AssertionError("lookup must not be called")

    transport = Transport(ok())
    store, supervisor, _, _ = setup(tmp_path, creds, transport, lookup=lookup)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"


def test_unsealed_inputs_proceed(tmp_path, creds):
    profile = make_profile(project_id="project1", input_digests={"source": SOURCE})
    transport = Transport(ok())
    store, supervisor, _, _ = setup(
        tmp_path,
        creds,
        transport,
        profile=profile,
        lookup=lambda p, d: {"claims": [], "truncated": False},
    )
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    (call,) = store.model_calls("run1")
    assert call["input_digests"] == {"source": SOURCE}


# -- main() and startup ------------------------------------------------------------------------


def write_main_inputs(tmp_path):
    profile = make_profile(
        prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
        control_url="http://127.0.0.1:1",
        agent_token_env="T_AGENT",
    )
    path = private(tmp_path / "profile.json", json.dumps(profile))
    prompt = private(tmp_path / "prompt.txt", PROMPT)
    return profile, path, prompt


def test_main_selects_anthropic_adapter(tmp_path, creds, monkeypatch, capsys):
    _, path, prompt = write_main_inputs(tmp_path)
    monkeypatch.setenv("T_AGENT", "SENTINEL-AGENT-TOKEN")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    transport = Transport(ok())
    built = []

    class Control:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, path, body=None, key=None):
            # A billed run settles on the control plane's model-spend ledger (model-spend ledger).
            if path == "/model-spend/reservations":
                return {
                    "allowed": True,
                    "call_id": "call1",
                    "status": "reserved",
                    "reserved_micro_cents": 1,
                }
            if path.endswith("/settlement"):
                return {
                    "call_id": "call1",
                    "status": "settled",
                    "settled_micro_cents": 1,
                    "over_reservation": False,
                    "cap_exceeded": False,
                }
            return {**grant(), "project_id": "project1"}

    def adapter(profile, *, credentials_dir):
        built.append(credentials_dir)
        return AnthropicMessagesReasoning(
            profile, credentials_dir=credentials_dir, transport=transport
        )

    def no_opencode(*args, **kwargs):
        raise AssertionError("OpenCode must not be used for the Anthropic runtime")

    monkeypatch.setattr(rr, "JsonEndpoint", Control)
    monkeypatch.setattr(rr, "OpenCodeV2Reasoning", no_opencode)
    monkeypatch.setattr(rr, "AnthropicMessagesReasoning", adapter)
    state = tmp_path / "state"
    code = rr.main(
        ["run", "--profile", str(path), "--state-dir", str(state), "--prompt-file", str(prompt)]
    )
    out = capsys.readouterr().out
    assert code == 0 and built == [creds]
    receipt = json.loads(out)
    (call,) = receipt["model_calls"]
    assert call["spend_ledger"]["status"] == "settled"
    assert [r["state"] for r in receipt["spend_ledger"]] == ["settled"]
    assert call["cost_basis"] == "computed_from_pinned_prices"
    assert call["output_sha256"] == hashlib.sha256(COMPLETION.encode()).hexdigest()
    for secret in (API_KEY, PROMPT, COMPLETION, "SENTINEL-AGENT-TOKEN"):
        assert secret not in out
        assert secret.encode() not in journal_bytes(tmp_path)
    assert API_KEY not in json.dumps(dict(os.environ))


def test_main_refuses_anthropic_without_credentials_directory(tmp_path, monkeypatch):
    _, path, prompt = write_main_inputs(tmp_path)
    monkeypatch.setenv("T_AGENT", "SENTINEL-AGENT-TOKEN")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setattr(
        rr, "AnthropicMessagesReasoning", lambda *a, **k: pytest.fail("adapter built")
    )
    code = rr.main(
        ["run", "--profile", str(path), "--state-dir", str(tmp_path / "state"),
         "--prompt-file", str(prompt)]
    )  # fmt: skip
    assert code == rr.EXIT_CONFIG  # missing_manager_credential_directory: never restarted
    assert not (tmp_path / "state").exists()


def test_startup_never_forwards_the_api_key(tmp_path, creds):
    profile = make_profile(agent_token_env="TESSERA_REASONING_AGENT_TOKEN")
    private(creds / "profile", json.dumps(profile))
    private(creds / "prompt", PROMPT)
    private(creds / "agent-token", "synthetic-agent\n")
    private(creds / "model-credential", REF + "\n")
    env = {"CREDENTIALS_DIRECTORY": str(creds), "PATH": "/usr/bin"}
    command, child = launch_spec("reasoning", tmp_path / "state", env)
    assert API_KEY not in json.dumps([command, child])
    assert "TESSERA_REASONING_HARNESS_TOKEN" not in child
    assert not any(name.startswith(("ANTHROPIC_", "CLAUDE_")) for name in child)


def test_legacy_single_row_model_calls_table_migrates(tmp_path):
    import sqlite3

    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / "reasoning.sqlite3")
    db.executescript(
        """
        CREATE TABLE effects (run_id TEXT NOT NULL, boundary TEXT NOT NULL,
          outcome TEXT NOT NULL, PRIMARY KEY(run_id,boundary));
        CREATE TABLE model_calls (run_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL,
          observed_at REAL NOT NULL);
        INSERT INTO effects VALUES('old','prompt','acknowledged');
        INSERT INTO model_calls VALUES('old', '{"tier":"sonnet","tokens":{"total":7}}', 86400);
        """
    )
    db.commit()
    db.close()
    (directory / "reasoning.sqlite3").chmod(0o600)
    store = ReasoningStore(directory)
    assert store.daily_usage("1970-01-02") == {"tokens": 7, "cost_cents": 0.0}
    assert store.attempts("old", "prompt") == 1
    rows = store.db.execute("SELECT run_id, seq, receipt_json FROM model_calls").fetchall()
    assert [tuple(r) for r in rows] == [("old", 1, '{"tier":"sonnet","tokens":{"total":7}}')]
    store.close()
    ReasoningStore(directory).close()  # reopening a migrated dir is a no-op
