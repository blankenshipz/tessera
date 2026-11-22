"""Supervisor/store wiring of the direct Messages API tool loop (ADR 0083).

Synthetic only: a scripted fake loop adapter stands in for the Messages client and a fake
bridge for the Tessera MCP server. No network, no API key, no model or tool server.
"""

import copy
import hashlib
import json
import sqlite3

import pytest

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.provenance_client import DeliveryResult
from tessera_agent.remote_reasoning_runtime import (
    MAX_PROMPT_ATTEMPTS,
    ModelConditionBlocked,
    ReasoningSupervisor,
    RuntimeBlocked,
)
from tessera_agent.remote_reasoning_store import ReasoningStore

PROMPT = "SENTINEL-PROMPT-TEXT-do-not-journal"
COMPLETION = "SENTINEL-COMPLETION-TEXT-do-not-journal"
TOOL_INPUT = "SENTINEL-TOOL-INPUT-do-not-journal"
TOOL_RESULT = "SENTINEL-TOOL-RESULT-do-not-journal"
AGENT_TOKEN = "SENTINEL-AGENT-TOKEN"
API_KEY = "sk-ant-SENTINEL-API-KEY-do-not-leak"
REF = "anthropic-api-key://owner1/SENTINEL-REFERENCE"
MODEL = "claude-sonnet-4-5"
DAY1 = 1795860000.0  # 2026-11-28T10:00:00Z
DENY = [{"action": "*", "resource": "*", "effect": "deny"}]
TOOLS = [
    {"name": "tessera_search", "description": "Search.", "input_schema": {"type": "object"}},
    {"name": "tessera_propose", "description": "Propose.", "input_schema": {"type": "object"}},
]
SECRETS = (PROMPT, COMPLETION, TOOL_INPUT, TOOL_RESULT, AGENT_TOKEN, API_KEY, "SENTINEL-REFERENCE")


def make_profile(run_id="run1", tools=("tessera_search", "tessera_propose"), **extra):
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
        "max_steps": 4,
        "max_tokens": 20000,
        "max_duration_seconds": 3600,
        "permissions": copy.deepcopy(DENY)
        + [{"action": name, "resource": "*", "effect": "allow"} for name in tools],
        "limits": {
            "max_output_tokens_per_call": 1000,
            "max_input_tokens_per_call": 500,
            "max_cost_cents_per_call": 10,
            "daily_token_cap": 5000,
            "daily_cost_cap_cents": 100,
        },
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
    }
    if tools:
        profile["limits"].update(max_tool_calls=4, max_tool_result_bytes=4096)
        profile["tool_catalog_sha256"] = rr.catalog_sha256([t for t in TOOLS if t["name"] in tools])
    profile.update(extra)
    return profile


class Time:
    def __init__(self, now=DAY1):
        self.now = now

    def __call__(self):
        return self.now


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


def tool_turn(*calls, input_tokens=100, output_tokens=50):
    return {"kind": "tool_use", "calls": list(calls), "in": input_tokens, "out": output_tokens}


def end_turn(text=COMPLETION, input_tokens=100, output_tokens=50):
    return {"kind": "end", "text": text, "in": input_tokens, "out": output_tokens}


def reject(condition="model_rate_limited", retry_after=30.0):
    return {"kind": "reject", "condition": condition, "retry_after": retry_after}


def _sha(value) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class FakeLoop:
    """Scripted stand-in for the contract's tool loop (prompt/resume/status + hooks)."""

    def __init__(self, profile, *, credentials_dir=None, tool_bridge=None, script=()):
        self.profile, self.tool_bridge, self.script = profile, tool_bridge, list(script)
        self.budget_check = self.output_sink = None
        self.on_model_call = self.on_tool_call = self.between_turns = None
        self.on_tool_intent = self.on_tool_result = self.before_tool = None
        self.max_steps = profile["max_steps"]
        self.sent, self.prompts, self.resumes, self.tool_runs = 0, 0, 0, []
        self._pending, self._obs = {}, {}

    def verify(self):
        return True

    def create(self, row):
        return None

    def interrupt(self, row):
        return None

    def prompt(self, row, text):
        assert text == PROMPT
        self.prompts += 1
        self._loop(row, 1)

    def resume(self, row):
        self.resumes += 1
        self._loop(row, self._pending.pop(row["id"]))

    def _blocked(self, error, *, sent, resumable=False):
        error.request_sent, error.resumable = sent, resumable
        return error

    def _loop(self, row, turn):
        tool_calls = self._obs.get(row["id"], {}).get("tool_calls", 0)
        while turn <= self.max_steps:
            if turn > 1 and self.between_turns is not None:
                try:
                    self.between_turns(row)
                except Exception as error:  # noqa: BLE001
                    raise self._blocked(error, sent=True) from None
            if self.budget_check is not None and not self.budget_check(1100, 1.6):
                raise self._blocked(
                    RuntimeBlocked("model_call_cap_exceeded"), sent=turn > 1, resumable=turn > 1
                )
            step = self.script.pop(0)
            if step["kind"] == "reject":
                error = ModelConditionBlocked(step["condition"], step["retry_after"])
                if turn == 1:
                    raise self._blocked(error, sent=False)
                self._pending[row["id"]] = turn
                raise self._blocked(error, sent=True, resumable=True)
            self.sent += 1
            usage = {
                "input": step["in"],
                "output": step["out"],
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
            cost = (step["in"] * 300 + step["out"] * 1500) / 1e6
            previous = self._obs.get(row["id"], {"usage": dict.fromkeys(usage, 0), "cost": 0})
            total = {k: previous["usage"][k] + v for k, v in usage.items()}
            self._obs[row["id"]] = {
                "tokens": sum(total.values()),
                "cost": previous["cost"] + cost,
                "outcome": None,
                "usage": total,
                "reported_model": "anthropic/" + MODEL,
                "rate_limit": None,
                "tool_calls": tool_calls,
            }
            uses = step.get("calls", [])
            self.on_model_call(
                row,
                {
                    "turn": turn,
                    "usage": usage,
                    "total": sum(usage.values()),
                    "cost": cost,
                    "cost_cents_estimate_max": 1.6,
                    "reported_model": "anthropic/" + MODEL,
                    "stop_reason": "tool_use" if step["kind"] == "tool_use" else "end_turn",
                    "tool_use_count": len(uses),
                },
            )
            if step["kind"] == "end":
                self._obs[row["id"]]["outcome"] = "succeeded"
                self.output_sink(row, step["text"])
                return
            if turn == self.max_steps:
                break
            for index, (name, arguments) in enumerate(uses):
                if tool_calls + 1 > self.profile["limits"]["max_tool_calls"]:
                    raise self._blocked(RuntimeBlocked("tool_call_cap_exceeded"), sent=True)
                tool_calls += 1
                self._obs[row["id"]]["tool_calls"] = tool_calls
                allowed = name in rr.allowed_tools(self.profile)
                position = {
                    "turn": turn,
                    "index": index,
                    "name": name,
                    "input_sha256": _sha(arguments),
                }
                if not allowed:
                    self.on_tool_call(
                        row,
                        position
                        | {
                            "result_sha256": _sha("tool not permitted"),
                            "is_error": True,
                            "truncated": False,
                            "allowed": False,
                            "status": "denied",
                            "skip_reason": None,
                        },
                    )
                    continue
                handle = self.on_tool_intent(row, position | {"allowed": True})
                text, is_error = self.tool_bridge.call(name, arguments)
                self.tool_runs.append(name)
                self.on_tool_result(
                    handle,
                    {
                        "status": "error" if is_error else "succeeded",
                        "result_sha256": _sha(text),
                        "is_error": is_error,
                        "truncated": False,
                    },
                )
            turn += 1
        self._obs[row["id"]]["outcome"] = "failed"
        self._obs[row["id"]]["reason"] = "turn_limit_reached"

    def status(self, row):
        found = self._obs.get(row["id"])
        if found is None:
            raise RuntimeBlocked("usage_unknown")
        return copy.deepcopy(found)


class FakeBridge:
    instances = []

    def __init__(self, *, tools=TOOLS, fail_start=False, **kwargs):
        self.kwargs, self._tools, self.fail_start = kwargs, tools, fail_start
        self.started = self.closed = False
        self.calls = []
        FakeBridge.instances.append(self)

    def start(self):
        if self.fail_start:
            raise rr.ToolBridgeBlocked("tool_server_unavailable")
        self.started = True

    def tools(self):
        return [copy.deepcopy(t) for t in self._tools if t["name"] in self.kwargs["allowed"]]

    def call(self, name, arguments):
        self.calls.append(name)
        return TOOL_RESULT, False

    def close(self):
        self.closed = True


def setup(tmp_path, script, *, profile=None, clock=None, authority=None):
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
    harness = FakeLoop(profile, tool_bridge=FakeBridge(allowed=rr.allowed_tools(profile)))
    harness.script = list(script)
    authority = authority if authority is not None else grant()
    supervisor = ReasoningSupervisor(store, harness, lambda _: authority, profile, clock=clock)
    return store, supervisor, harness, clock, authority


def leak_blob(store, tmp_path, run_id="run1"):
    store.db.commit()
    state = tmp_path / "state"
    files = b"".join(p.read_bytes() for p in state.rglob("*") if p.is_file() and p.parent == state)
    return (
        files
        + "\n".join(store.db.iterdump()).encode()
        + json.dumps(store.model_calls(run_id)).encode()
        + json.dumps(store.tool_calls(run_id)).encode()
        + json.dumps(store.provenance_pending(run_id, 64)).encode()
    )


def assert_no_leaks(blob: bytes):
    for secret in SECRETS:
        assert secret.encode() not in blob


SEARCH = ("tessera_search", {"query": TOOL_INPUT})


# -- per-turn receipts -----------------------------------------------------------------------


def test_per_turn_model_receipts_and_digest_only_tool_receipts(tmp_path):
    script = [tool_turn(SEARCH, SEARCH), tool_turn(SEARCH), end_turn()]
    store, supervisor, harness, _, _ = setup(tmp_path, script)
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "completed"
    calls = store.model_calls("run1")
    assert [c["seq"] for c in calls] == [1, 2, 3]
    assert [c["turn"] for c in calls] == [1, 2, 3]
    assert [c["stop_reason"] for c in calls] == ["tool_use", "tool_use", "end_turn"]
    assert [c["tool_use_count"] for c in calls] == [2, 1, 0]
    assert [c["daily_tokens_after"] for c in calls] == [150, 300, 450]
    per_call = (100 * 300 + 50 * 1500) / 1e6
    for n, call in enumerate(calls, 1):
        assert call["tokens"]["total"] == 150 and call["tokens"]["input"] == 100
        assert call["cost"] == pytest.approx(per_call)
        assert call["daily_cost_cents_after"] == pytest.approx(per_call * n)
        assert call["cost_basis"] == "computed_from_pinned_prices"
        assert call["limits"] == make_profile()["limits"]
    # Only the final turn produced an output file; its digest is on that turn's receipt.
    assert [c["output_sha256"] for c in calls[:2]] == [None, None]
    assert calls[2]["output_sha256"] == hashlib.sha256(COMPLETION.encode()).hexdigest()
    assert (tmp_path / "state" / "outputs" / "run1-3.txt").read_text() == COMPLETION
    assert row["tokens"] == 450 and row["cost"] == pytest.approx(per_call * 3)
    assert store.daily_usage("2026-11-28")["tokens"] == 450
    tools = store.tool_calls("run1")
    assert [(t["seq"], t["turn"], t["index"]) for t in tools] == [(1, 1, 0), (2, 1, 1), (3, 2, 0)]
    assert {t["name"] for t in tools} == {"tessera_search"}
    assert all(t["allowed"] and not t["is_error"] and not t["truncated"] for t in tools)
    assert tools[0]["input_sha256"] == _sha(SEARCH[1])
    assert tools[0]["result_sha256"] == _sha(TOOL_RESULT)
    blob = leak_blob(store, tmp_path)
    assert_no_leaks(blob)


def test_denied_tool_name_is_never_journaled(tmp_path):
    rogue = ("SENTINEL_ROGUE_TOOL", {"x": TOOL_INPUT})
    store, supervisor, harness, _, _ = setup(tmp_path, [tool_turn(rogue), end_turn()])
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    (call,) = store.tool_calls("run1")
    assert call["name"] == "unlisted" and call["allowed"] is False and call["is_error"]
    assert harness.tool_bridge.calls == []  # never executed
    assert b"SENTINEL_ROGUE_TOOL" not in leak_blob(store, tmp_path)


def test_store_refuses_content_in_tool_receipts(tmp_path):
    store, _, _, _, _ = setup(tmp_path, [])
    intent = {
        "turn": 1,
        "index": 0,
        "name": "tessera_search",
        "input_sha256": "a" * 64,
        "allowed": True,
    }
    assert store.tool_call_begin("run1", intent, DAY1) == 1
    for bad in (
        intent | {"input_sha256": TOOL_INPUT},
        intent | {"result": TOOL_RESULT},
        intent | {"name": "tessera search; " + TOOL_INPUT},
        intent | {"allowed": 1},
        intent | {"allowed": False},  # a disallowed tool is never started
        {k: v for k, v in intent.items() if k != "turn"},
    ):
        with pytest.raises(ValueError):
            store.tool_call_begin("run1", bad, DAY1)
    denied = intent | {
        "name": "unlisted",
        "allowed": False,
        "result_sha256": "b" * 64,
        "is_error": True,
        "truncated": False,
        "status": "denied",
        "skip_reason": None,
    }
    assert store.tool_call("run1", denied, DAY1) == 2
    for bad in (
        denied | {"result_sha256": TOOL_RESULT},
        denied | {"allowed": True},
        denied | {"status": "succeeded"},  # executed rows need an intent first
        denied | {"status": "started"},
        denied | {"skip_reason": "stop_requested"},
        denied | {"result": TOOL_RESULT},
        denied | {"status": "refused", "skip_reason": "stop requested " + TOOL_INPUT},
    ):
        with pytest.raises(ValueError):
            store.tool_call("run1", bad, DAY1)
    assert len(store.tool_calls("run1")) == 2


def test_store_tool_transitions_are_once_and_monotonic(tmp_path):
    store, _, _, _, _ = setup(tmp_path, [])
    intent = {
        "turn": 1,
        "index": 0,
        "name": "tessera_search",
        "input_sha256": "a" * 64,
        "allowed": True,
    }
    first = store.tool_call_begin("run1", intent, DAY1)
    (row,) = store.tool_calls("run1")
    assert (row["status"], row["result_sha256"], row["is_error"], row["truncated"]) == (
        "started",
        None,
        None,
        None,
    )
    for status, fields in (
        ("started", {}),
        ("denied", {}),
        ("refused", {}),
        ("succeeded", {}),  # a completed call needs its result digest
        ("succeeded", {"result_sha256": "b" * 64, "is_error": True, "truncated": False}),
        ("error", {"result_sha256": "b" * 64, "is_error": False, "truncated": False}),
        ("uncertain", {"result_sha256": "b" * 64}),
    ):
        with pytest.raises(ValueError):
            store.tool_call_finish("run1", first, status, DAY1, **fields)
    done = {"result_sha256": "b" * 64, "is_error": False, "truncated": False}
    store.tool_call_finish("run1", first, "succeeded", DAY1 + 1, **done)
    for status, fields in (("succeeded", done), ("uncertain", {})):
        with pytest.raises(ValueError):  # exactly once
            store.tool_call_finish("run1", first, status, DAY1, **fields)
    second = store.tool_call_begin("run1", intent | {"index": 1}, DAY1)
    store.tool_call_finish("run1", second, "uncertain", DAY1)
    third = store.tool_call_begin("run1", intent | {"index": 2}, DAY1)
    assert store.tool_calls_interrupted("run1", DAY1 + 2) == 1
    assert store.tool_calls_interrupted("run1", DAY1 + 3) == 0
    with pytest.raises(ValueError):
        store.tool_call_finish("run1", third, "succeeded", DAY1, **done)
    rows = store.tool_calls("run1")
    assert [r["status"] for r in rows] == ["succeeded", "uncertain", "uncertain"]
    assert rows[0]["completed_at"] == DAY1 + 1 and rows[0]["is_error"] is False
    with pytest.raises(sqlite3.IntegrityError):  # the schema enforces the same rules
        with store.db:
            store.db.execute("UPDATE tool_calls SET result_sha256=NULL WHERE seq=?", (first,))


def test_turn_limit_keeps_receipts_and_fails(tmp_path):
    profile = make_profile(max_steps=2)
    store, supervisor, _, _, _ = setup(
        tmp_path, [tool_turn(SEARCH), tool_turn(SEARCH)], profile=profile
    )
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("failed", "turn_limit_reached")
    assert [c["turn"] for c in store.model_calls("run1")] == [1, 2]
    assert len(store.tool_calls("run1")) == 1


def test_tool_call_cap_halts_with_its_reason(tmp_path):
    script = [tool_turn(SEARCH, SEARCH, SEARCH), tool_turn(SEARCH, SEARCH), end_turn()]
    store, supervisor, harness, _, _ = setup(tmp_path, script)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "tool_call_cap_exceeded")
    assert len(store.tool_calls("run1")) == 4
    assert harness.sent == 2
    assert store.effect("run1", "prompt") == "acknowledged"


# -- kill switch between turns -----------------------------------------------------------------


def test_stop_request_between_turns_stops_before_next_billed_turn(tmp_path):
    script = [tool_turn(SEARCH), tool_turn(SEARCH), end_turn()]
    store, supervisor, harness, _, _ = setup(tmp_path, script)
    original = harness.tool_bridge.call

    def stop_during_tool(name, arguments):
        store.stop("run1")  # e.g. SIGTERM / `tessera-reasoning stop` while a tool runs
        return original(name, arguments)

    harness.tool_bridge.call = stop_during_tool
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "stop_requested")
    assert harness.sent == 1 and len(harness.script) == 2  # turn 2 never billed
    assert [c["turn"] for c in store.model_calls("run1")] == [1]
    assert len(store.tool_calls("run1")) == 1
    assert store.effect("run1", "prompt") == "acknowledged"


def test_grant_revocation_between_turns_halts(tmp_path):
    script = [tool_turn(SEARCH), end_turn()]
    store, supervisor, harness, _, authority = setup(tmp_path, script)
    original = harness.tool_bridge.call

    def revoke(name, arguments):
        authority["status"] = "revoked"
        return original(name, arguments)

    harness.tool_bridge.call = revoke
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "grant_revoked")
    assert harness.sent == 1


def test_daily_cap_reached_mid_loop_halts(tmp_path):
    # Turn 1 used more than the count preflight suggested: the day's cap is now exceeded.
    script = [tool_turn(SEARCH, input_tokens=4800, output_tokens=300), end_turn()]
    store, supervisor, harness, _, _ = setup(tmp_path, script)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "daily_cap_exceeded")
    assert harness.sent == 1
    (call,) = store.model_calls("run1")
    assert call["daily_tokens_after"] == 5100


def test_per_turn_cost_cap_halts_after_receipting_the_turn(tmp_path):
    script = [tool_turn(SEARCH, input_tokens=400, output_tokens=7000), end_turn()]
    store, supervisor, harness, _, _ = setup(tmp_path, script)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_call_cap_exceeded"
    assert harness.sent == 1 and len(store.model_calls("run1")) == 1


# -- resumable rejections ------------------------------------------------------------------------


def test_rejected_later_turn_backs_off_then_resumes_without_replay(tmp_path):
    script = [tool_turn(SEARCH), reject(retry_after=30), end_turn()]
    store, supervisor, harness, clock, _ = setup(tmp_path, script)
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "running" and row["reason"] is None
    assert store.effect("run1", "prompt") == "acknowledged"
    assert store.latest_rate_limit("run1")["backoff_until"] == DAY1 + 30
    clock.now = DAY1 + 10
    supervisor.tick("run1", PROMPT)
    assert harness.resumes == 0  # still backing off
    clock.now = DAY1 + 30
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "completed"
    assert (harness.prompts, harness.resumes, harness.sent) == (1, 1, 2)
    assert [c["turn"] for c in store.model_calls("run1")] == [1, 2]
    assert len(store.tool_calls("run1")) == 1  # the tool ran once; nothing replayed
    assert store.effect("run1", "resume.2") == "acknowledged"
    assert_no_leaks(leak_blob(store, tmp_path))


def test_resume_attempts_are_capped(tmp_path):
    script = [tool_turn(SEARCH)] + [reject(retry_after=1)] * MAX_PROMPT_ATTEMPTS
    store, supervisor, harness, clock, _ = setup(tmp_path, script)
    for step in range(MAX_PROMPT_ATTEMPTS + 1):
        clock.now = DAY1 + step * 2
        row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_retry_exhausted"
    assert harness.prompts == 1
    assert harness.resumes == MAX_PROMPT_ATTEMPTS - 1  # 8 attempts at turn 2 in all
    assert harness.script == []
    assert store.attempts("run1", "resume.2") == MAX_PROMPT_ATTEMPTS
    assert store.provenance_coverage("run1")["recorded"] <= 64


def test_cap_rejection_of_later_turn_halts(tmp_path):
    store, supervisor, harness, _, _ = setup(tmp_path, [tool_turn(SEARCH), end_turn()])
    checks = iter([True, False])
    harness.budget_check = lambda tokens, cents: next(checks)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_cap_exceeded")
    assert harness.sent == 1


# -- legacy state ---------------------------------------------------------------------------------


def test_legacy_state_dir_opens_with_tool_calls_table(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / "reasoning.sqlite3")
    db.executescript(
        """
        CREATE TABLE runs (id TEXT PRIMARY KEY, binding TEXT NOT NULL, digest TEXT NOT NULL,
          session_id TEXT NOT NULL, message_id TEXT NOT NULL, started REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'ready', stop_requested INTEGER NOT NULL DEFAULT 0,
          reason TEXT, tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0);
        CREATE TABLE model_calls (run_id TEXT NOT NULL, seq INTEGER NOT NULL,
          receipt_json TEXT NOT NULL, observed_at REAL NOT NULL, day TEXT NOT NULL,
          tokens_delta INTEGER NOT NULL DEFAULT 0, cost_cents_delta REAL NOT NULL DEFAULT 0,
          PRIMARY KEY(run_id,seq));
        INSERT INTO runs(id,binding,digest,session_id,message_id,started)
          VALUES('old','{}','d','ses_x','msg_y',1);
        INSERT INTO model_calls VALUES('old',1,'{"tier":"sonnet"}',86400,'1970-01-02',7,0);
        """
    )
    db.commit()
    db.close()
    (directory / "reasoning.sqlite3").chmod(0o600)
    store = ReasoningStore(directory)
    assert store.tool_calls("old") == []
    assert [c["seq"] for c in store.model_calls("old")] == [1]
    store.close()
    ReasoningStore(directory).close()


# -- main() ---------------------------------------------------------------------------------


def private(path, content):
    path.write_text(content)
    path.chmod(0o600)
    return path


@pytest.fixture
def creds(tmp_path):
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    private(root / "anthropic-api-key", API_KEY + "\n")
    private(root / "agent-token", AGENT_TOKEN + "\n")
    return root


class Control:
    def __init__(self, *args, **kwargs):
        pass

    def call(self, method, path, body=None, key=None):
        if "/holdout-claims" in path:
            return {"claims": [], "truncated": False}
        # A billed run reserves and settles on the control plane's model-spend ledger.
        if path == "/model-spend/reservations":
            return {
                "allowed": True,
                "call_id": f"call{(body or {}).get('seq', 1)}",
                "status": "reserved",
                "reserved_micro_cents": 1,
            }
        if path.endswith("/release"):
            return {"call_id": path.split("/")[-2], "status": "released"}
        if path.endswith("/settlement"):
            return {
                "call_id": path.split("/")[-2],
                "status": "settled",
                "settled_micro_cents": 1,
                "over_reservation": False,
                "cap_exceeded": False,
            }
        return {**grant(), "project_id": "project1"}


class Collector:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def append(self, project_id, record, proof, key):
        return DeliveryResult("unknown", "collector_unavailable", record["record_id"])


def run_main(tmp_path, creds, monkeypatch, profile, script, bridge_kwargs=None, command="run"):
    profile = profile | {
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "control_url": "http://127.0.0.1:1",
        "agent_token_env": "T_AGENT",
        "project_id": "project1",
    }
    path = private(tmp_path / "profile.json", json.dumps(profile))
    prompt = private(tmp_path / "prompt.txt", PROMPT)
    monkeypatch.setenv("T_AGENT", AGENT_TOKEN)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    built = []

    def adapter(profile, *, credentials_dir, **options):
        harness = FakeLoop(profile, credentials_dir=credentials_dir, **options)
        harness.script = list(script)
        built.append((harness, options))
        return harness

    def bridge(**kwargs):
        return FakeBridge(**(bridge_kwargs or {}), **kwargs)

    FakeBridge.instances.clear()
    monkeypatch.setattr(rr, "JsonEndpoint", Control)
    monkeypatch.setattr(rr, "ProvenanceClient", Collector)
    monkeypatch.setattr(rr, "AnthropicMessagesReasoning", adapter)
    monkeypatch.setattr(rr, "McpToolBridge", bridge)
    monkeypatch.setattr(rr.time, "sleep", lambda _: None)
    argv = [command, "--profile", str(path), "--state-dir", str(tmp_path / "state")]
    if command == "run":
        argv += ["--prompt-file", str(prompt)]
    return rr.main(argv), built


def test_main_builds_bridge_for_allowed_tools_and_closes_it(tmp_path, creds, monkeypatch, capsys):
    script = [tool_turn(SEARCH), end_turn()]
    code, built = run_main(tmp_path, creds, monkeypatch, make_profile(), script)
    out = capsys.readouterr().out
    assert code == 0
    ((harness, options),) = built
    (bridge,) = FakeBridge.instances
    assert options["tool_bridge"] is bridge and bridge.started and bridge.closed
    assert bridge.kwargs["allowed"] == frozenset({"tessera_search", "tessera_propose"})
    assert bridge.kwargs["token_file"] == creds / "agent-token"
    assert bridge.kwargs["max_result_bytes"] == 4096
    assert (bridge.kwargs["project_id"], bridge.kwargs["destination"]) == ("project1", "lab")
    receipt = json.loads(out)
    assert [c["turn"] for c in receipt["model_calls"]] == [1, 2]
    (tool,) = receipt["tool_calls"]
    assert tool["name"] == "tessera_search" and tool["input_sha256"] == _sha(SEARCH[1])
    assert_no_leaks(out.encode())
    state = tmp_path / "state"
    assert_no_leaks(b"".join(p.read_bytes() for p in state.iterdir() if p.is_file()))


def test_main_without_tool_allows_builds_no_bridge(tmp_path, creds, monkeypatch, capsys):
    profile = make_profile(tools=(), max_steps=1)
    code, built = run_main(tmp_path, creds, monkeypatch, profile, [end_turn()])
    assert code == 0 and FakeBridge.instances == []
    ((_, options),) = built
    assert "tool_bridge" not in options
    assert json.loads(capsys.readouterr().out)["tool_calls"] == []


@pytest.mark.parametrize(
    "bridge_kwargs,reason",
    [
        ({"tools": [TOOLS[0], TOOLS[1] | {"description": "Changed."}]}, "tool_catalog_changed"),
        ({"fail_start": True}, "tool_server_unavailable"),
    ],
)
def test_main_catalog_mismatch_halts_before_any_call(
    tmp_path, creds, monkeypatch, capsys, bridge_kwargs, reason
):
    code, built = run_main(
        tmp_path, creds, monkeypatch, make_profile(), [end_turn()], bridge_kwargs
    )
    receipt = json.loads(capsys.readouterr().out)
    assert code == 2
    assert (receipt["state"], receipt["reason"]) == ("stop_acknowledged", reason)
    assert receipt["model_calls"] == [] and built[0][0].sent == 0
    assert all(b.closed for b in FakeBridge.instances)


def test_main_bridge_closed_when_run_raises(tmp_path, creds, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("supervisor failure")

    monkeypatch.setattr(rr, "ReasoningSupervisor", boom)
    with pytest.raises(RuntimeError):
        run_main(tmp_path, creds, monkeypatch, make_profile(), [end_turn()])
    (bridge,) = FakeBridge.instances
    assert bridge.closed


def test_catalog_command_prints_pin_without_model_call(tmp_path, creds, monkeypatch, capsys):
    profile = make_profile(tool_catalog_sha256="placeholder")
    (creds / "anthropic-api-key").unlink()  # no API key is needed or read
    code, built = run_main(tmp_path, creds, monkeypatch, profile, [], command="catalog")
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and built == []
    assert printed == {
        "tool_catalog_sha256": rr.catalog_sha256(TOOLS),
        "tools": ["tessera_propose", "tessera_search"],
    }
    (bridge,) = FakeBridge.instances
    assert bridge.closed and bridge.kwargs["token_file"] == creds / "agent-token"
    assert not (tmp_path / "state").exists()
    # No tools allowed: refused.
    code, _ = run_main(tmp_path, creds, monkeypatch, make_profile(tools=()), [], command="catalog")
    assert code == 2 and json.loads(capsys.readouterr().out) == {"error": "no_tools_allowed"}


def test_legacy_tool_rows_migrate_to_statuses(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / "reasoning.sqlite3")
    a, b = "a" * 64, "b" * 64
    db.executescript(
        f"""
        CREATE TABLE runs (id TEXT PRIMARY KEY, binding TEXT NOT NULL, digest TEXT NOT NULL,
          session_id TEXT NOT NULL, message_id TEXT NOT NULL, started REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'ready', stop_requested INTEGER NOT NULL DEFAULT 0,
          reason TEXT, tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0);
        CREATE TABLE tool_calls (
          run_id TEXT NOT NULL, seq INTEGER NOT NULL, turn INTEGER NOT NULL,
          tool_index INTEGER NOT NULL, name TEXT NOT NULL, input_sha256 TEXT NOT NULL,
          result_sha256 TEXT NOT NULL, is_error INTEGER NOT NULL, truncated INTEGER NOT NULL,
          allowed INTEGER NOT NULL, observed_at REAL NOT NULL,
          PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
        INSERT INTO runs(id,binding,digest,session_id,message_id,started)
          VALUES('old','{{}}','d','ses_x','msg_y',1);
        INSERT INTO tool_calls VALUES('old',1,1,0,'tessera_search','{a}','{b}',0,0,1,5);
        INSERT INTO tool_calls VALUES('old',2,1,1,'tessera_search','{a}','{b}',1,1,1,5);
        INSERT INTO tool_calls VALUES('old',3,1,2,'unlisted','{a}','{b}',1,0,0,5);
        """
    )
    db.commit()
    db.close()
    (directory / "reasoning.sqlite3").chmod(0o600)
    store = ReasoningStore(directory)
    rows = store.tool_calls("old")
    assert [(r["status"], r["skip_reason"], r["is_error"], r["truncated"]) for r in rows] == [
        ("succeeded", None, False, False),
        ("error", None, True, True),
        ("denied", None, True, False),
    ]
    assert all(r["result_sha256"] == b for r in rows)
    store.close()
    assert len(ReasoningStore(directory).tool_calls("old")) == 3
