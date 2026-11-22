"""Durable paused tool loop and unrun last-turn tools: fake transport and bridge, no network."""

import copy
import hashlib
import json

import pytest
from test_anthropic_tool_loop import (
    API_URL,
    COMPLETION,
    COUNT_URL,
    READ,
    ROW,
    SENTINEL,
    TOOL_INPUT,
    TOOLS,
    FakeBridge,
    FakeTransport,
    Recorder,
    adapter,
    count,
    end_turn,
    profile,
    tool_use,
)

from tessera_agent.mcp_tool_bridge import catalog_sha256
from tessera_agent.remote_reasoning_runtime import ModelConditionBlocked, RuntimeBlocked


@pytest.fixture
def creds(tmp_path):
    path = tmp_path / "anthropic-api-key"
    path.write_text(SENTINEL + "\n")
    path.chmod(0o600)
    return tmp_path


LIMITED = (429, {"retry-after": "7"}, {"error": {"type": "rate_limit_error"}})
SNAPSHOT_KEYS = {
    "version",
    "run_id",
    "turn",
    "messages",
    "tools_sha256",
    "tool_calls_used",
    "turns_used",
    "model_id",
    "cumulative",
}


class Parks:
    def __init__(self, fail=False):
        self.snapshots, self.unparked, self.fail = [], [], fail

    def kwargs(self):
        return {"on_park": self.park, "on_unpark": lambda row: self.unparked.append(row["id"])}

    def park(self, row, state):
        if self.fail:
            raise OSError("disk full /secret/path")
        self.snapshots.append(json.loads(json.dumps(state)))  # As written to and read from disk.


def parked(creds, *, prof=None):
    """Run turn 1 (one tool call) and park on a definite rejection of turn 2."""
    transport = FakeTransport(count(), tool_use(READ), count(), LIMITED)
    bridge, rec, parks = FakeBridge(), Recorder(), Parks()
    reasoning = adapter(creds, transport, bridge, rec, prof=prof, **parks.kwargs())
    with pytest.raises(ModelConditionBlocked, match="model_rate_limited") as caught:
        reasoning.prompt(ROW, "hello")
    assert caught.value.request_sent is True and caught.value.resumable is True
    assert len(parks.snapshots) == 1 and parks.unparked == []
    return transport, bridge, parks.snapshots[0]


def test_park_snapshot_round_trips_and_a_new_adapter_resumes_at_the_parked_turn(creds):
    first_transport, first_bridge, snapshot = parked(creds)
    assert set(snapshot) == SNAPSHOT_KEYS
    assert snapshot["version"] == 1 and snapshot["run_id"] == ROW["id"]
    assert snapshot["model_id"] == "claude-sonnet-4-5"
    assert (snapshot["turn"], snapshot["turns_used"], snapshot["tool_calls_used"]) == (2, 1, 1)
    assert snapshot["tools_sha256"] == catalog_sha256([TOOLS["tessera_read"]])
    # Exactly the conversation the rejected turn sent.
    assert snapshot["messages"] == first_transport.calls[3][2]["messages"]
    assert snapshot["cumulative"] == {
        "tokens": 150,
        "cost": pytest.approx((100 * 300 + 50 * 1500) / 1e6),
        "usage": {"input": 100, "output": 50, "cache_read": 0, "cache_write": 0, "reasoning": 0},
    }
    text = json.dumps(snapshot)
    for secret in (SENTINEL, "x-api-key", "anthropic-version", "content-type"):
        assert secret not in text
    assert len(first_bridge.calls) == 1

    # A restart: a NEW adapter, transport and bridge; only the persisted snapshot survives.
    transport = FakeTransport(count(), end_turn())
    bridge, rec, parks = FakeBridge(), Recorder(), Parks()
    reasoning = adapter(creds, transport, bridge, rec, **parks.kwargs())
    reasoning.restore(ROW, snapshot)
    assert transport.calls == []
    reasoning.resume(ROW)
    assert [c[0] for c in transport.calls] == [COUNT_URL, API_URL]
    assert transport.calls[1][2]["messages"] == first_transport.calls[3][2]["messages"]
    assert bridge.calls == []  # No tool replayed.
    assert [r["turn"] for r in rec.models] == [2] and rec.tools == []
    assert rec.sink == [COMPLETION]
    observed = reasoning.status(ROW)
    assert (observed["outcome"], observed["turns"], observed["tool_calls"]) == ("succeeded", 2, 1)
    assert observed["tokens"] == 150 + 220
    assert observed["usage"]["input"] == 300
    assert parks.unparked == [ROW["id"]] and parks.snapshots == []
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)


def test_restored_loop_rejected_again_parks_again_without_unpark(creds):
    _, _, snapshot = parked(creds)
    transport = FakeTransport(count(), LIMITED)
    parks = Parks()
    reasoning = adapter(creds, transport, FakeBridge(), **parks.kwargs())
    reasoning.restore(ROW, snapshot)
    with pytest.raises(ModelConditionBlocked) as caught:
        reasoning.resume(ROW)
    assert caught.value.resumable is True
    assert parks.snapshots == [snapshot] and parks.unparked == []


def test_restored_loop_halted_by_kill_switch_unparks(creds):
    _, _, snapshot = parked(creds)
    transport, parks = FakeTransport(), Parks()

    def stop(row):
        raise RuntimeBlocked("run_stopped")

    reasoning = adapter(creds, transport, FakeBridge(), between_turns=stop, **parks.kwargs())
    reasoning.restore(ROW, snapshot)
    with pytest.raises(RuntimeBlocked, match="run_stopped") as caught:
        reasoning.resume(ROW)
    assert caught.value.resumable is False
    assert transport.calls == [] and parks.unparked == [ROW["id"]]


def test_unpark_failure_does_not_mask_success(creds):
    _, _, snapshot = parked(creds)

    def broken(row):
        raise OSError("gone")

    rec = Recorder()
    reasoning = adapter(creds, FakeTransport(count(), end_turn()), None, rec, on_unpark=broken)
    reasoning.restore(ROW, snapshot)
    reasoning.resume(ROW)
    assert reasoning.status(ROW)["outcome"] == "succeeded" and rec.sink == [COMPLETION]


def test_unparked_prompt_loop_never_calls_unpark(creds):
    parks = Parks()
    transport = FakeTransport(count(), tool_use(READ), count(), end_turn())
    adapter(creds, transport, **parks.kwargs()).prompt(ROW, "hi")
    assert parks.snapshots == [] and parks.unparked == []


def _set(path, value):
    def mutate(state):
        target = state
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _drop(key):
    return lambda state: state.pop(key)


def _extra(state):
    state["headers"] = {}


def _wrong_roles(state):
    state["messages"][1]["role"] = "user"


def _message_extra_key(state):
    state["messages"][0]["id"] = "x"


def _ends_with_assistant(state):
    state["messages"].pop()


def _tool_id_mismatch(state):
    state["messages"][2]["content"][0]["tool_use_id"] = "toolu_other"


def _not_tool_result(state):
    state["messages"][2]["content"] = [{"type": "text", "text": "x"}]


INVALID = {
    "version_2": _set(["version"], 2),
    "version_bool": _set(["version"], True),
    "missing_key": _drop("turns_used"),
    "extra_key": _extra,
    "run_id": _set(["run_id"], "run-2"),
    "model_id": _set(["model_id"], "claude-opus-4-1"),
    "tools_sha256_other": _set(["tools_sha256"], "b" * 64),
    "tools_sha256_shape": _set(["tools_sha256"], "not-a-digest"),
    "turn_one": _set(["turn"], 1),
    "turn_beyond_max": _set(["turn"], 5),
    "turn_string": _set(["turn"], "2"),
    "turns_used": _set(["turns_used"], 2),
    "tool_calls_over_cap": _set(["tool_calls_used"], 9),
    "tool_calls_mismatch": _set(["tool_calls_used"], 0),
    "messages_not_list": _set(["messages"], {"role": "user"}),
    "roles_not_alternating": _wrong_roles,
    "message_extra_key": _message_extra_key,
    "ends_with_assistant": _ends_with_assistant,
    "tool_use_id_mismatch": _tool_id_mismatch,
    "user_turn_not_tool_result": _not_tool_result,
    "empty_prompt": _set(["messages", 0, "content"], ""),
    "tokens_mismatch": _set(["cumulative", "tokens"], 151),
    "cost_mismatch": _set(["cumulative", "cost"], 0.5),
    "cost_nan": _set(["cumulative", "cost"], float("nan")),
    "usage_negative": _set(["cumulative", "usage", "output"], -1),
    "usage_missing": lambda state: state["cumulative"]["usage"].pop("reasoning"),
    "cumulative_extra": _set(["cumulative", "rate_limit"], None),
}


@pytest.mark.parametrize("mutate", INVALID.values(), ids=INVALID.keys())
def test_invalid_snapshot_is_refused(creds, mutate):
    _, _, snapshot = parked(creds)
    state = copy.deepcopy(snapshot)
    mutate(state)
    transport, bridge = FakeTransport(), FakeBridge()
    reasoning = adapter(creds, transport, bridge)
    with pytest.raises(RuntimeBlocked, match="model_resume_state_invalid") as caught:
        reasoning.restore(ROW, state)
    assert caught.value.request_sent is None and caught.value.resumable is False
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)
    assert transport.calls == [] and bridge.calls == []


def test_snapshot_that_is_not_a_dict_is_refused(creds):
    reasoning = adapter(creds, FakeTransport())
    with pytest.raises(RuntimeBlocked, match="model_resume_state_invalid"):
        reasoning.restore(ROW, [])


def test_changed_tool_catalog_refuses_restore(creds):
    _, _, snapshot = parked(creds)
    listed = [dict(TOOLS["tessera_read"], description="Changed.")]
    reasoning = adapter(creds, FakeTransport(), FakeBridge(listed=listed))
    with pytest.raises(RuntimeBlocked, match="model_resume_state_invalid"):
        reasoning.restore(ROW, snapshot)


def test_lower_max_steps_refuses_restore(creds):
    _, _, snapshot = parked(creds)
    reasoning = adapter(creds, FakeTransport(), prof=profile(max_steps=1))
    with pytest.raises(RuntimeBlocked, match="model_resume_state_invalid"):
        reasoning.restore(ROW, snapshot)


def test_restore_over_an_in_memory_parked_loop_is_refused(creds):
    transport = FakeTransport(count(), tool_use(READ), count(), LIMITED)
    parks = Parks()
    reasoning = adapter(creds, transport, **parks.kwargs())
    with pytest.raises(ModelConditionBlocked):
        reasoning.prompt(ROW, "hello")
    with pytest.raises(RuntimeBlocked, match="model_resume_state_invalid"):
        reasoning.restore(ROW, parks.snapshots[0])


def test_park_callback_failure_drops_the_loop_as_unrecorded(creds):
    transport = FakeTransport(count(), tool_use(READ), count(), LIMITED)
    parks = Parks(fail=True)
    reasoning = adapter(creds, transport, **parks.kwargs())
    with pytest.raises(RuntimeBlocked, match="model_park_unrecorded") as caught:
        reasoning.prompt(ROW, "hello")
    assert not isinstance(caught.value, ModelConditionBlocked)
    assert caught.value.request_sent is None and caught.value.resumable is False
    assert "disk full" not in repr(caught.value) and caught.value.__cause__ is None
    with pytest.raises(RuntimeBlocked, match="model_resume_unavailable"):
        reasoning.resume(ROW)
    assert parks.unparked == []


def test_last_turn_tool_use_is_refused_turn_limit_and_never_executed(creds):
    secret = ("tessera_secret", {"q": TOOL_INPUT})
    transport = FakeTransport(count(), tool_use(READ, secret))
    bridge, rec = FakeBridge(allowed=TOOLS), Recorder()
    reasoning = adapter(creds, transport, bridge, rec, prof=profile(max_steps=1))
    reasoning.prompt(ROW, "hi")
    assert bridge.calls == []
    assert len(rec.models) == 1
    assert (rec.models[0]["tool_use_count"], rec.models[0]["unrun_tool_count"]) == (2, 2)
    digest = hashlib.sha256(
        json.dumps({"q": TOOL_INPUT}, separators=(",", ":")).encode()
    ).hexdigest()
    assert rec.tools == [
        {
            "turn": 1,
            "index": index,
            "name": name,
            "input_sha256": digest,
            "result_sha256": None,
            "is_error": None,
            "truncated": None,
            "allowed": allowed,
            "status": "refused",
            "skip_reason": "turn_limit_reached",
        }
        for index, (name, allowed) in enumerate([("tessera_read", True), ("tessera_secret", False)])
    ]
    observed = reasoning.status(ROW)
    assert (observed["outcome"], observed["reason"]) == ("failed", "turn_limit_reached")
    assert observed["tool_calls"] == 0
