"""End-to-end: ReasoningSupervisor + ReasoningStore + the real Messages tool loop.

Synthetic only: a scripted fake transport and a fake tool bridge; no network, and the API
key is a sentinel string in a private temporary file.
"""

import copy
import hashlib
import json

import pytest

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.anthropic_messages import API_URL, COUNT_URL, AnthropicMessagesReasoning
from tessera_agent.remote_reasoning_runtime import ReasoningSupervisor
from tessera_agent.remote_reasoning_store import ReasoningStore

PROMPT = "SENTINEL-PROMPT-TEXT-do-not-journal"
COMPLETION = "SENTINEL-COMPLETION-TEXT-do-not-journal"
TOOL_INPUT = "SENTINEL-TOOL-INPUT-do-not-journal"
TOOL_RESULT = "SENTINEL-TOOL-RESULT-do-not-journal"
API_KEY = "sk-ant-SENTINEL-API-KEY-do-not-leak"
REF = "anthropic-api-key://owner1/SENTINEL-REFERENCE"
MODEL = "claude-sonnet-4-5"
DAY1 = 1795860000.0  # 2026-11-28T10:00:00Z
DENY = [{"action": "*", "resource": "*", "effect": "deny"}]
TOOLS = [{"name": "tessera_search", "description": "Search.", "input_schema": {"type": "object"}}]
SECRETS = (PROMPT, COMPLETION, TOOL_INPUT, TOOL_RESULT, API_KEY, "SENTINEL-REFERENCE")


def make_profile():
    return {
        "run_id": "run1",
        "grant_id": "g1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "agent1",
        "destination": "lab",
        "account_mode": "anthropic-api-key-ref",
        "credential_ref": REF,
        "model": {"providerID": "anthropic", "id": MODEL},
        "max_steps": 4,
        "max_tokens": 20000,
        "max_duration_seconds": 3600,
        "permissions": copy.deepcopy(DENY)
        + [{"action": "tessera_search", "resource": "*", "effect": "allow"}],
        "limits": {
            "max_output_tokens_per_call": 1000,
            "max_input_tokens_per_call": 500,
            "max_cost_cents_per_call": 10,
            "daily_token_cap": 5000,
            "daily_cost_cap_cents": 100,
            "max_tool_calls": 4,
            "max_tool_result_bytes": 4096,
        },
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
        "tool_catalog_sha256": rr.catalog_sha256(TOOLS),
    }


class Time:
    def __init__(self, now=DAY1):
        self.now = now

    def __call__(self):
        return self.now


def grant():
    return {
        "revision": 1,
        "epoch": 1,
        "status": "active",
        "effective_status": "active",
        "remaining_budget_cents": 50,
        "bounds": {
            "subject_credential_id": "agent1",
            "destination": "lab",
            "model": MODEL,
            "budget_cents": 100,
            "max_duration_seconds": 7200,
            "expires_at": "2100-01-01T00:00:00+00:00",
        },
    }


def tool_use():
    content = [
        {"type": "text", "text": "thinking " + COMPLETION},
        {"type": "tool_use", "id": "toolu_0", "name": "tessera_search", "input": {"q": TOOL_INPUT}},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "tessera_forbidden",
            "input": {"q": TOOL_INPUT},
        },
    ]
    usage = {"input_tokens": 100, "output_tokens": 50}
    return 200, {}, {"model": MODEL, "stop_reason": "tool_use", "content": content, "usage": usage}


def end_turn():
    usage = {"input_tokens": 200, "output_tokens": 20}
    content = [{"type": "text", "text": COMPLETION}]
    return 200, {}, {"model": MODEL, "stop_reason": "end_turn", "content": content, "usage": usage}


RATE_LIMITED = (429, {"retry-after": "30"}, {"error": {"type": "rate_limit_error"}})


class Transport:
    def __init__(self, *replies):
        self.replies, self.sent, self.bodies, self.timeouts = list(replies), [], [], []

    def post_json(self, url, headers, body, *, timeout=None):
        self.sent.append(url)
        self.bodies.append((url, copy.deepcopy(body)))
        self.timeouts.append((url, timeout))
        if url == COUNT_URL:
            return 200, {}, {"input_tokens": 100}
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply()  # runs while the billed call is in flight
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def billed(self):
        return [u for u in self.sent if u != COUNT_URL]


class Bridge:
    last_truncated = False

    def __init__(self):
        self.calls = []

    def tools(self):
        return copy.deepcopy(TOOLS)

    def call(self, name, arguments):
        self.calls.append(name)
        return TOOL_RESULT, False


def setup(tmp_path, *replies, clock=None, bind=True, authority=None):
    creds = tmp_path / "credentials"
    if bind:
        creds.mkdir(mode=0o700)
        key = creds / "anthropic-api-key"
        key.write_text(API_KEY + "\n")
        key.chmod(0o600)
    profile, clock = make_profile(), clock or Time()
    authority = authority if authority is not None else grant()
    store = ReasoningStore(tmp_path / "state")
    binding = {
        k: profile[k] for k in ("grant_id", "revision", "epoch", "subject_credential_id")
    } | {
        "destination": "lab",
        "model": MODEL,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "profile_sha256": hashlib.sha256(b"profile").hexdigest(),
    }
    if bind:
        store.bind("run1", binding, clock())
    transport, bridge = Transport(*replies), Bridge()
    adapter = AnthropicMessagesReasoning(
        profile, credentials_dir=creds, transport=transport, clock=clock, tool_bridge=bridge
    )
    supervisor = ReasoningSupervisor(store, adapter, lambda _: authority, profile, clock=clock)
    return store, supervisor, transport, bridge, clock


def receipt(store, row):
    return json.dumps(
        [
            row,
            store.model_calls("run1"),
            store.tool_calls("run1"),
            store.provenance_pending("run1", 64),
        ],
        default=repr,
    )


def assert_private(store, tmp_path, row):
    store.db.commit()
    state = tmp_path / "state"
    for path in state.rglob("*"):
        if not path.is_file():
            continue
        if path.parent.name == "outputs":
            assert COMPLETION in path.read_text()  # the one private place for the completion
            continue
        if path.parent.name == "pending":
            # The parked conversation is private content; never the key or its reference.
            assert path.stat().st_mode & 0o077 == 0
            assert TOOL_RESULT.encode() in path.read_bytes()
            for secret in (API_KEY, "SENTINEL-REFERENCE"):
                assert secret.encode() not in path.read_bytes(), path
            continue
        for secret in SECRETS:
            assert secret.encode() not in path.read_bytes(), path
    dump = "\n".join(store.db.iterdump()) + receipt(store, row)
    for secret in SECRETS:
        assert secret not in dump


def test_full_loop_receipts_and_privacy(tmp_path):
    store, supervisor, transport, bridge, _ = setup(tmp_path, tool_use(), end_turn())
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "completed"
    calls = store.model_calls("run1")
    assert [c["turn"] for c in calls] == [1, 2]
    assert [c["stop_reason"] for c in calls] == ["tool_use", "end_turn"]
    assert [c["tool_use_count"] for c in calls] == [2, 0]
    assert [c["daily_tokens_after"] for c in calls] == [150, 370]
    assert store.daily_usage("2026-11-28")["tokens"] == 370
    tools = store.tool_calls("run1")
    assert [(t["turn"], t["index"], t["allowed"]) for t in tools] == [(1, 0, True), (1, 1, False)]
    assert all(len(t["input_sha256"]) == 64 and len(t["result_sha256"]) == 64 for t in tools)
    assert tools[1]["is_error"] is True
    assert [(t["status"], t["skip_reason"]) for t in tools] == [
        ("succeeded", None),
        ("denied", None),
    ]
    assert bridge.calls == ["tessera_search"]  # the disallowed tool never reached the bridge
    assert len(transport.billed()) == 2
    assert_private(store, tmp_path, row)


def test_rate_limit_on_turn_two_backs_off_and_resumes_without_replay(tmp_path):
    store, supervisor, transport, bridge, clock = setup(
        tmp_path, tool_use(), RATE_LIMITED, end_turn()
    )
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "running"
    assert store.latest_rate_limit("run1")["backoff_until"] == DAY1 + 30
    assert len(transport.billed()) == 2 and bridge.calls == ["tessera_search"]
    clock.now = DAY1 + 10
    supervisor.tick("run1", PROMPT)
    assert len(transport.billed()) == 2  # still backing off: nothing sent
    clock.now = DAY1 + 30
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "completed"
    assert len(transport.billed()) == 3  # turn 1, rejected turn 2, accepted turn 2
    assert bridge.calls == ["tessera_search"]  # the tool was not re-called
    assert [c["turn"] for c in store.model_calls("run1")] == [1, 2]
    assert len(store.tool_calls("run1")) == 2
    assert_private(store, tmp_path, row)


def test_stop_before_turn_two_prevents_second_billed_call(tmp_path):
    store, supervisor, transport, bridge, _ = setup(tmp_path, tool_use(), end_turn())
    original = bridge.call

    def stop_during_tool(name, arguments):
        store.stop("run1")
        return original(name, arguments)

    bridge.call = stop_during_tool
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "stop_requested")
    assert len(transport.billed()) == 1
    assert [c["turn"] for c in store.model_calls("run1")] == [1]
    assert_private(store, tmp_path, row)


def provenance(store):
    return [json.loads(r["body"])["record"] for r in store.provenance_pending("run1", 64)]


def test_restart_after_parked_rate_limit_resumes_turn_two_from_disk(tmp_path):
    store, supervisor, transport, bridge, clock = setup(tmp_path, tool_use(), RATE_LIMITED)
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "running"
    assert len(transport.billed()) == 2 and bridge.calls == ["tessera_search"]
    pending = tmp_path / "state" / "pending" / "run1.json"
    assert pending.exists()
    (parked,) = store.loop_parks("run1")
    assert parked["turn"] == 2
    rejected_turn_two = [b for u, b in transport.bodies if u != COUNT_URL][-1]["messages"]
    assert_private(store, tmp_path, row)  # while parked, content is only in pending/
    totals = store.get("run1")
    # Restart: discard every in-memory piece, reopen the journal on the same state dir.
    del supervisor
    store.close()
    clock.now = DAY1 + 30
    store, supervisor, fresh, fresh_bridge, _ = setup(tmp_path, end_turn(), clock=clock, bind=False)
    for _ in range(3):
        row = supervisor.tick("run1", PROMPT)
        if row["state"] != "running":
            break
    assert row["state"] == "completed"
    assert fresh.sent == [COUNT_URL, fresh.sent[-1]] and len(fresh.billed()) == 1
    assert fresh.bodies[-1][1]["messages"] == rejected_turn_two
    assert fresh_bridge.calls == [] and bridge.calls == ["tessera_search"]
    assert len(transport.billed()) == 2  # the discarded transport was never used again
    assert row["tokens"] == totals["tokens"] + 220
    (resumed,) = store.loop_resumes("run1")
    assert (resumed["turn"], resumed["restored_from_disk"]) == (2, True)
    assert resumed["state_sha256"] == parked["state_sha256"]
    found = provenance(store)
    resumes = [
        r
        for r in found
        if r["kind"] == "agent" and r["content_digest"] == "sha256:" + parked["state_sha256"]
    ]
    assert len(resumes) == 1
    turns = [r for r in found if (r["tool_id"] or "").startswith("tessera_tool_turn")]
    assert [r["tool_turn"]["turn"] for r in turns] == [1]
    assert [c["turn"] for c in store.model_calls("run1")] == [1, 2]
    assert [c["stop_reason"] for c in store.model_calls("run1")] == ["tool_use", "end_turn"]
    assert not pending.exists()
    assert_private(store, tmp_path, row)
    store.close()


# -- per-turn receipt/count rule (ADR 0083 §5): every billed turn is a row, counted today -----

TURN1_COST = (100 * 300 + 50 * 1500) / 1e6  # tool_use(): 100 in / 50 out
WORST_TOKENS = 100 + 1000  # turn 2's counted input + output cap
WORST_CENTS = (100 * 300 + 1000 * 1500) / 1e6


@pytest.mark.parametrize(
    "second,reason",
    [
        (TimeoutError("SENTINEL-UPSTREAM-DETAIL"), "uncertain_prompt"),
        (ConnectionResetError("SENTINEL-UPSTREAM-DETAIL"), "uncertain_prompt"),
        ((200, {}, {"model": MODEL, "content": []}), "uncertain_prompt"),
        ((502, {}, {"error": {"type": "SENTINEL-UPSTREAM-DETAIL"}}), "model_call_failed"),
    ],
)
def test_turn_two_unknown_outcome_reserves_worst_case_and_counts_today(tmp_path, second, reason):
    store, supervisor, transport, bridge, _ = setup(tmp_path, tool_use(), second, end_turn())
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", reason)
    assert store.effect("run1", "prompt") == "uncertain"
    first, reserved = store.model_calls("run1")
    assert (first["turn"], first["stop_reason"], first["cost_basis"]) == (
        1,
        "tool_use",
        "computed_from_pinned_prices",
    )
    assert "outcome_class" not in first and first["tokens"]["total"] == 150
    assert reserved["turn"] == 2
    assert reserved["outcome_class"] == "uncertain"
    assert reserved["cost_basis"] == "reserved_worst_case_estimate"
    assert reserved["tokens"]["total"] == WORST_TOKENS
    assert reserved["cost"] == pytest.approx(WORST_CENTS)
    assert reserved["model_reported"] is None and reserved["output_sha256"] is None
    # The daily ledger holds turn 1's actual usage plus turn 2's reservation.
    assert reserved["daily_tokens_after"] == 150 + WORST_TOKENS
    assert reserved["daily_cost_cents_after"] == pytest.approx(TURN1_COST + WORST_CENTS)
    day = store.daily_usage("2026-11-28")
    assert day["tokens"] == 150 + WORST_TOKENS
    assert day["cost_cents"] == pytest.approx(TURN1_COST + WORST_CENTS)
    run = store.get("run1")
    assert (run["tokens"], run["cost"]) == (
        150 + WORST_TOKENS,
        pytest.approx(TURN1_COST + WORST_CENTS),
    )
    # Never replayed: a later tick sends nothing.
    supervisor.tick("run1", PROMPT)
    assert len(transport.billed()) == 2 and bridge.calls == ["tessera_search"]
    assert "SENTINEL-UPSTREAM-DETAIL" not in receipt(store, row)
    assert_private(store, tmp_path, row)


def test_turn_two_substituted_model_receipts_worst_case(tmp_path):
    substituted = end_turn()
    substituted[2]["model"] = "claude-haiku-4-5"
    store, supervisor, transport, _, _ = setup(tmp_path, tool_use(), substituted)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_substituted")
    first, second = store.model_calls("run1")
    assert first["turn"] == 1 and "outcome_class" not in first
    assert (second["turn"], second["stop_reason"]) == (2, "end_turn")
    assert second["outcome_class"] == "model_substituted"
    # Another model's rates may exceed the pinned prices: charged max(actual, reserved).
    assert second["cost_basis"] == "worst_case_for_substituted_model"
    assert second["model_reported"] == "anthropic/claude-haiku-4-5"
    assert second["tokens"]["input"] == 200 and second["tokens"]["total"] == 1100
    worst = (100 * 300 + 1000 * 1500) / 1e6
    assert second["cost"] == pytest.approx(worst)
    assert second["output_sha256"] is None
    assert not (tmp_path / "state" / "outputs").exists()  # the completion is never kept
    assert store.daily_usage("2026-11-28")["tokens"] == 150 + 1100
    assert store.get("run1")["cost"] == pytest.approx(TURN1_COST + worst)
    assert len(transport.billed()) == 2
    assert_private(store, tmp_path, row)


def test_final_turn_output_unrecorded_is_one_actual_row(tmp_path):
    store, supervisor, _, _, _ = setup(tmp_path, tool_use(), end_turn())
    outputs = tmp_path / "state" / "outputs"
    outputs.mkdir(mode=0o755)
    outputs.chmod(0o755)  # not private: the sink refuses
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_output_unrecorded"
    first, second = store.model_calls("run1")
    assert "outcome_class" not in first
    assert (second["turn"], second["outcome_class"]) == (2, "output_unrecorded")
    assert second["cost_basis"] == "computed_from_pinned_prices"
    assert second["tokens"]["total"] == 220 and second["output_sha256"] is None
    assert store.daily_usage("2026-11-28")["tokens"] == 370


def test_billed_timeout_is_used_on_every_turn(tmp_path):
    _, supervisor, transport, _, _ = setup(tmp_path, tool_use(), end_turn())
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert transport.timeouts == [(COUNT_URL, 30.0), (API_URL, pytest.approx(100.0))] * 2


def test_resumed_turn_timeout_after_restart_reserves_and_leaves_no_park(tmp_path):
    # Park turn 2 (rate limit), restart, resume from disk; the resumed turn 2 times out.
    store, supervisor, transport, bridge, clock = setup(tmp_path, tool_use(), RATE_LIMITED)
    assert supervisor.tick("run1", PROMPT)["state"] == "running"
    pending = tmp_path / "state" / "pending" / "run1.json"
    (parked,) = store.loop_parks("run1")
    # The snapshot's cumulative usage equals the store's run totals (turn 1 only).
    snapshot = json.loads(pending.read_bytes())
    totals = store.get("run1")
    assert snapshot["cumulative"]["tokens"] == totals["tokens"] == 150
    assert snapshot["cumulative"]["cost"] == pytest.approx(totals["cost"])
    del supervisor
    store.close()
    clock.now = DAY1 + 30
    store, supervisor, fresh, _, _ = setup(
        tmp_path, TimeoutError("SENTINEL-UPSTREAM-DETAIL"), clock=clock, bind=False
    )
    for _ in range(3):
        row = supervisor.tick("run1", PROMPT)
        if row["state"] != "running":
            break
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "uncertain_resume.2")
    assert len(fresh.billed()) == 1 and bridge.calls == ["tessera_search"]
    (resumed,) = store.loop_resumes("run1")
    assert (resumed["turn"], resumed["state_sha256"]) == (2, parked["state_sha256"])
    first, reserved = store.model_calls("run1")
    assert first["turn"] == 1 and "outcome_class" not in first
    assert (reserved["turn"], reserved["outcome_class"]) == (2, "uncertain")
    assert reserved["cost_basis"] == "reserved_worst_case_estimate"
    assert reserved["tokens"]["total"] == WORST_TOKENS
    # Run totals and the daily ledger: turn 1 actual + turn 2 reservation; nothing parked.
    run = store.get("run1")
    assert run["tokens"] == 150 + WORST_TOKENS
    assert run["cost"] == pytest.approx(TURN1_COST + WORST_CENTS)
    assert store.daily_usage("2026-11-28")["tokens"] == 150 + WORST_TOKENS
    assert len(store.loop_parks("run1")) == 1 and not pending.exists()
    # Never replayed.
    supervisor.tick("run1", PROMPT)
    assert len(fresh.billed()) == 1
    assert_private(store, tmp_path, row)
    store.close()


# -- every tool call on receipts; no tool after a fence trip (ADR 0083) ------------------------

SEARCH_BLOCK = {"type": "tool_use", "name": "tessera_search", "input": {"q": TOOL_INPUT}}


def two_searches(input_tokens=100, output_tokens=50):
    content = [SEARCH_BLOCK | {"id": "toolu_0"}, SEARCH_BLOCK | {"id": "toolu_1"}]
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    return 200, {}, {"model": MODEL, "stop_reason": "tool_use", "content": content, "usage": usage}


def statuses(store):
    return [(t["index"], t["status"], t["skip_reason"]) for t in store.tool_calls("run1")]


def test_intent_row_exists_before_the_bridge_sees_the_call(tmp_path):
    store, supervisor, _, bridge, _ = setup(tmp_path, tool_use(), end_turn())
    seen = []

    def call(name, arguments):
        seen.append(
            [(t["name"], t["status"], t["result_sha256"]) for t in store.tool_calls("run1")]
        )
        return TOOL_RESULT, False

    bridge.call = call
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert seen == [[("tessera_search", "started", None)]]
    assert statuses(store) == [(0, "succeeded", None), (1, "denied", None)]
    (first, _) = store.tool_calls("run1")
    assert first["completed_at"] is not None and first["is_error"] is False


@pytest.mark.parametrize(
    "failure", [rr.ToolBridgeBlocked("tool_call_uncertain"), OSError(TOOL_RESULT)]
)
def test_uncertain_tool_call_is_receipted_and_halts(tmp_path, failure):
    store, supervisor, transport, bridge, _ = setup(tmp_path, tool_use(), end_turn())

    def call(name, arguments):
        bridge.calls.append(name)
        raise failure

    bridge.call = call
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "tool_call_uncertain")
    (only,) = store.tool_calls("run1")  # the turn halted: the denied block was never reached
    assert (only["status"], only["result_sha256"], only["is_error"]) == ("uncertain", None, None)
    assert only["allowed"] is True and only["completed_at"] is not None
    supervisor.tick("run1", PROMPT)  # never retried
    assert bridge.calls == ["tessera_search"] and len(transport.billed()) == 1
    assert_private(store, tmp_path, row)


def test_stop_during_billed_turn_runs_no_tool(tmp_path):
    store = None

    def stop_in_flight():
        store.stop("run1")
        return tool_use()

    store, supervisor, transport, bridge, _ = setup(tmp_path, stop_in_flight, end_turn())
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "stop_requested")
    assert bridge.calls == []
    assert statuses(store) == [(0, "refused", "stop_requested"), (1, "refused", "stop_requested")]
    tools = store.tool_calls("run1")
    assert [t["allowed"] for t in tools] == [True, False]
    assert [t["name"] for t in tools] == ["tessera_search", "unlisted"]
    assert all(t["result_sha256"] is None and t["is_error"] is None for t in tools)
    assert len(transport.billed()) == 1
    assert [c["turn"] for c in store.model_calls("run1")] == [1]  # the billed turn is receipted
    assert store.effect("run1", "prompt") == "acknowledged"
    assert_private(store, tmp_path, row)


def test_grant_revoked_during_billed_turn_runs_no_tool(tmp_path):
    authority = grant()

    def revoke_in_flight():
        authority["status"] = "revoked"
        return tool_use()

    store, supervisor, transport, bridge, _ = setup(
        tmp_path, revoke_in_flight, end_turn(), authority=authority
    )
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "grant_revoked")
    assert bridge.calls == []
    assert statuses(store) == [(0, "refused", "grant_revoked"), (1, "refused", "grant_revoked")]
    assert len(transport.billed()) == 1


def test_daily_cap_exceeded_by_billed_turn_runs_no_tool(tmp_path):
    # Turn 1 reported more than its preflight count: the day's 5000-token cap is now passed.
    heavy = two_searches(input_tokens=4800, output_tokens=300)
    store, supervisor, transport, bridge, _ = setup(tmp_path, heavy, end_turn())
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "daily_cap_exceeded")
    assert bridge.calls == []
    assert statuses(store) == [
        (0, "refused", "daily_cap_exceeded"),
        (1, "refused", "daily_cap_exceeded"),
    ]
    (call,) = store.model_calls("run1")
    assert call["daily_tokens_after"] == 5100
    assert len(transport.billed()) == 1


def test_fence_trip_between_two_tools_of_one_turn(tmp_path):
    store, supervisor, transport, bridge, _ = setup(tmp_path, two_searches(), end_turn())

    def stop_after_first(name, arguments):
        bridge.calls.append(name)
        store.stop("run1")
        return TOOL_RESULT, False

    bridge.call = stop_after_first
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "stop_requested")
    assert bridge.calls == ["tessera_search"]
    assert statuses(store) == [(0, "succeeded", None), (1, "refused", "stop_requested")]
    assert len(transport.billed()) == 1
    # The fence refusal is counted in the turn's aggregated tool provenance.
    (record,) = tool_turns(store)
    assert record["tool_turn"] == {
        "turn": 1,
        "calls": 2,
        "errors": 0,
        "denied": 0,
        "truncated": 0,
        "unrun": 0,
        "refused": 1,
        "uncertain": 0,
    }


def tool_turns(store):
    found = [json.loads(r["body"])["record"] for r in store.provenance_pending("run1", 64)]
    return [r for r in found if r["tool_id"] == "tessera_tool_turn"]


class Killed(BaseException):
    """Stands in for the process dying (SIGKILL, power loss): nothing below runs."""


def test_crash_between_tool_call_and_its_completion_is_uncertain_on_restart(tmp_path):
    store, supervisor, transport, bridge, clock = setup(tmp_path, tool_use(), end_turn())

    def die(*args, **kwargs):
        raise Killed

    store.tool_call_finish = die
    with pytest.raises(Killed):
        supervisor.tick("run1", PROMPT)
    assert bridge.calls == ["tessera_search"]
    assert statuses(store) == [(0, "started", None)]
    assert tool_turns(store) == []  # a turn with a started row has no provenance record yet
    store.close()
    # A new process: a fresh store, adapter and supervisor over the same state dir.
    profile = make_profile()
    store = ReasoningStore(tmp_path / "state")
    transport2, bridge2 = Transport(end_turn()), Bridge()
    adapter = AnthropicMessagesReasoning(
        profile,
        credentials_dir=tmp_path / "credentials",
        transport=transport2,
        clock=clock,
        tool_bridge=bridge2,
    )
    supervisor = ReasoningSupervisor(store, adapter, lambda _: grant(), profile, clock=clock)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "tool_call_uncertain")
    (only,) = store.tool_calls("run1")
    assert (only["status"], only["result_sha256"]) == ("uncertain", None)
    assert transport2.sent == [] and bridge2.calls == []  # nothing replayed
    # Marking the row uncertain completes the turn: its record is written then, once.
    (record,) = tool_turns(store)
    assert record["outcome"] == "unknown"
    assert (record["tool_turn"]["calls"], record["tool_turn"]["uncertain"]) == (1, 1)
    assert supervisor.tick("run1", PROMPT)["reason"] == "tool_call_uncertain"
    assert transport2.sent == []
    assert_private(store, tmp_path, row)


def test_completion_hook_failure_leaves_started_row_marked_uncertain_next_tick(tmp_path):
    store, supervisor, transport, bridge, _ = setup(tmp_path, tool_use(), end_turn())

    def refuse(*args, **kwargs):
        raise ValueError("disk")

    original = store.tool_call_finish
    store.tool_call_finish = refuse
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "tool_call_unrecorded")
    assert statuses(store) == [(0, "started", None)]
    assert tool_turns(store) == []
    store.tool_call_finish = original
    supervisor.tick("run1", PROMPT)
    assert statuses(store) == [(0, "uncertain", None)]
    assert [r["tool_turn"]["uncertain"] for r in tool_turns(store)] == [1]
    assert bridge.calls == ["tessera_search"] and len(transport.billed()) == 1
