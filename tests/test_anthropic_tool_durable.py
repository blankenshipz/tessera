"""Durable paused tool loop, per-turn tool provenance and unrun last-turn tools (ADR 0083).

Synthetic only: a scripted fake adapter that follows the on_park/restore/on_unpark contract
stands in for the Messages client. No network, no API key, no model or tool server.
"""

import copy
import hashlib
import json
import os
import sqlite3
import stat

import pytest
import test_anthropic_tool_runtime as base
from test_anthropic_tool_runtime import (
    API_KEY,
    COMPLETION,
    DAY1,
    MODEL,
    PROMPT,
    SEARCH,
    TOOL_INPUT,
    TOOL_RESULT,
    FakeBridge,
    Time,
    _sha,
    end_turn,
    grant,
    make_profile,
    reject,
    run_main,
    tool_turn,
)

import tessera_agent.remote_reasoning_runtime as rr
import tessera_agent.remote_reasoning_store as rs
from tessera_agent.provenance_client import sanitize_envelope
from tessera_agent.remote_reasoning_runtime import (
    ModelConditionBlocked,
    ReasoningSupervisor,
    RuntimeBlocked,
)
from tessera_agent.remote_reasoning_store import ReasoningStore, tool_turn_digest
from tessera_control.agent_provenance_models import AgentProvenanceRecord

CONTENT = (PROMPT, COMPLETION, TOOL_INPUT, TOOL_RESULT, API_KEY)
ROGUE = ("tessera_unlisted_rogue", {"x": 1})


def _blocked(error, *, sent, resumable=False):
    error.request_sent, error.resumable = sent, resumable
    return error


class DurableLoop:
    """Scripted stand-in for the adapter contract: park snapshots, restore and unpark."""

    def __init__(self, profile, *, credentials_dir=None, tool_bridge=None, script=()):
        self.profile, self.tool_bridge, self.script = profile, tool_bridge, list(script)
        self.budget_check = self.output_sink = None
        self.on_model_call = self.on_tool_call = self.between_turns = None
        self.on_park = self.on_unpark = None
        self.on_tool_intent = self.on_tool_result = self.before_tool = None
        self.max_steps = profile["max_steps"]
        self.sent, self.prompts, self.resumes, self.restored = 0, 0, 0, []
        self._pending, self._obs, self._messages = {}, {}, {}

    def verify(self):
        return True

    def create(self, row):
        return None

    def interrupt(self, row):
        return None

    def prompt(self, row, text):
        self.prompts += 1
        self._messages[row["id"]] = [{"role": "user", "content": text}]
        self._loop(row, 1, 0)

    def resume(self, row):
        state = self._pending.pop(row["id"], None)
        if state is None:
            raise _blocked(RuntimeBlocked("model_resume_unavailable"), sent=None)
        self.resumes += 1
        try:
            self._loop(row, state["turn"], state["tool_calls"])
        finally:
            if row["id"] not in self._pending and self.on_unpark is not None:
                self.on_unpark(row)

    def restore(self, row, state):
        if state.get("version") != 1 or state.get("run_id") != row["id"]:
            raise _blocked(RuntimeBlocked("model_resume_state_invalid"), sent=None)
        cumulative = state["cumulative"]
        self._obs[row["id"]] = {
            "tokens": cumulative["tokens"],
            "cost": cumulative["cost"],
            "usage": dict(cumulative["usage"]),
            "outcome": None,
            "reported_model": None,
            "rate_limit": None,
        }
        self._messages[row["id"]] = copy.deepcopy(state["messages"])
        self._pending[row["id"]] = {"turn": state["turn"], "tool_calls": state["tool_calls_used"]}
        self.restored.append(state["turn"])

    def _snapshot(self, row, turn, tool_calls):
        observed = self._obs[row["id"]]
        return {
            "version": 1,
            "run_id": row["id"],
            "turn": turn,
            "messages": copy.deepcopy(self._messages[row["id"]]),
            "tools_sha256": "a" * 64,
            "tool_calls_used": tool_calls,
            "turns_used": turn - 1,
            "model_id": MODEL,
            "cumulative": {
                "tokens": observed["tokens"],
                "cost": observed["cost"],
                "usage": dict(observed["usage"]),
            },
        }

    def _loop(self, row, turn, tool_calls):
        run_id = row["id"]
        while turn <= self.max_steps:
            if turn > 1 and self.between_turns is not None:
                try:
                    self.between_turns(row)
                except Exception as error:  # noqa: BLE001
                    raise _blocked(error, sent=True) from None
            if self.budget_check is not None and not self.budget_check(1100, 1.6):
                raise _blocked(RuntimeBlocked("model_call_cap_exceeded"), sent=False)
            step = self.script.pop(0)
            if step["kind"] == "reject":
                error = ModelConditionBlocked(step["condition"], step["retry_after"])
                if turn == 1:
                    raise _blocked(error, sent=False)
                if self.on_park is not None:
                    try:
                        self.on_park(row, self._snapshot(row, turn, tool_calls))
                    except Exception:  # noqa: BLE001
                        raise _blocked(RuntimeBlocked("model_park_unrecorded"), sent=None) from None
                self._pending[run_id] = {"turn": turn, "tool_calls": tool_calls}
                raise _blocked(error, sent=True, resumable=True)
            self.sent += 1
            usage = {"input": step["in"], "output": step["out"], "reasoning": 0}
            usage |= {"cache_read": 0, "cache_write": 0}
            cost = (step["in"] * 300 + step["out"] * 1500) / 1e6
            previous = self._obs.get(run_id, {"usage": dict.fromkeys(usage, 0), "cost": 0})
            total = {k: previous["usage"][k] + v for k, v in usage.items()}
            self._obs[run_id] = {
                "tokens": sum(total.values()),
                "cost": previous["cost"] + cost,
                "outcome": None,
                "usage": total,
                "reported_model": "anthropic/" + MODEL,
                "rate_limit": None,
            }
            uses = step.get("calls", [])
            last = step["kind"] == "tool_use" and turn == self.max_steps
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
                    "unrun_tool_count": len(uses) if last else 0,
                },
            )
            if step["kind"] == "end":
                self._obs[run_id]["outcome"] = "succeeded"
                self.output_sink(row, step["text"])
                return
            allowed_names = rr.allowed_tools(self.profile)
            results = []
            for index, (name, arguments) in enumerate(uses):
                allowed = name in allowed_names
                position = {
                    "turn": turn,
                    "index": index,
                    "name": name,
                    "input_sha256": _sha(arguments),
                }
                if last:
                    # Requested on the last allowed turn: refused, never executed.
                    self.on_tool_call(
                        row,
                        position
                        | {"result_sha256": None, "is_error": None, "truncated": None}
                        | {"allowed": allowed, "status": "refused"}
                        | {"skip_reason": "turn_limit_reached"},
                    )
                    continue
                tool_calls += 1
                if allowed:
                    handle = self.on_tool_intent(row, position | {"allowed": True})
                    text, is_error = self.tool_bridge.call(name, arguments)
                    self.on_tool_result(
                        handle,
                        {
                            "status": "error" if is_error else "succeeded",
                            "result_sha256": _sha(text),
                            "is_error": is_error,
                            "truncated": False,
                        },
                    )
                else:
                    text, is_error = "tool not permitted", True
                    self.on_tool_call(
                        row,
                        position
                        | {"result_sha256": _sha(text), "is_error": True, "truncated": False}
                        | {"allowed": False, "status": "denied", "skip_reason": None},
                    )
                results.append({"type": "tool_result", "content": text})
            if last:
                break
            messages = self._messages[run_id]
            messages.append({"role": "assistant", "content": [{"input": u[1]} for u in uses]})
            messages.append({"role": "user", "content": results})
            turn += 1
        self._obs[run_id]["outcome"] = "failed"
        self._obs[run_id]["reason"] = "turn_limit_reached"

    def status(self, row):
        found = self._obs.get(row["id"])
        if found is None:
            raise RuntimeBlocked("usage_unknown")
        return copy.deepcopy(found)


def binding(profile):
    return {k: profile[k] for k in ("grant_id", "revision", "epoch", "subject_credential_id")} | {
        "destination": "lab",
        "model": MODEL,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "profile_sha256": hashlib.sha256(b"profile").hexdigest(),
    }


def build(tmp_path, script, *, profile=None, clock=None, bind=True):
    """A supervisor + store on ``tmp_path/state``; called again it simulates a restart."""
    profile = profile or make_profile()
    clock = clock or Time()
    store = ReasoningStore(tmp_path / "state")
    if bind:
        store.bind(profile["run_id"], binding(profile), clock())
    harness = DurableLoop(profile, tool_bridge=FakeBridge(allowed=rr.allowed_tools(profile)))
    harness.script = list(script)
    supervisor = ReasoningSupervisor(store, harness, lambda _: grant(), profile, clock=clock)
    return store, supervisor, harness, clock


def pending_path(tmp_path, run_id="run1"):
    return tmp_path / "state" / "pending" / f"{run_id}.json"


def journal_bytes(store, tmp_path):
    store.db.commit()
    raw = (tmp_path / "state" / "reasoning.sqlite3").read_bytes()
    return raw + "\n".join(store.db.iterdump()).encode()


def records(store, run_id="run1"):
    return [json.loads(r["body"])["record"] for r in store.provenance_pending(run_id, 64)]


def tool_turn_records(store):
    return [r for r in records(store) if (r["tool_id"] or "").startswith("tessera_tool_turn")]


def park(tmp_path, script=None):
    """Turn 1 runs a tool, turn 2 is rejected and parked."""
    script = script or [tool_turn(SEARCH), reject(retry_after=30), end_turn()]
    store, supervisor, harness, clock = build(tmp_path, script)
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "running" and store.effect("run1", "resume.2") == "not_sent"
    return store, supervisor, harness, clock


# -- durable park ------------------------------------------------------------------------------


def test_park_writes_private_file_and_digest_only_row(tmp_path):
    store, _, _, _ = park(tmp_path)
    path = pending_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = path.read_bytes()
    state = json.loads(data)
    assert state["turn"] == 2 and state["run_id"] == "run1"
    assert PROMPT in data.decode() and TOOL_RESULT in data.decode()  # content is in the file
    (parked,) = store.loop_parks("run1")
    assert (parked["seq"], parked["turn"]) == (1, 2)
    assert parked["state_sha256"] == hashlib.sha256(data).hexdigest()
    assert parked["observed_at"] == DAY1
    blob = journal_bytes(store, tmp_path)
    for secret in CONTENT:
        assert secret.encode() not in blob
    assert list(path.parent.iterdir()) == [path]  # no temporary file left behind


def test_restart_restores_from_disk_and_resumes(tmp_path):
    store, _, harness, clock = park(tmp_path)
    digest = store.latest_loop_park("run1")["state_sha256"]
    totals = store.get("run1")
    store.close()
    # Restart: a new store and supervisor on the same state dir; nothing in memory.
    clock.now = DAY1 + 30
    store, supervisor, fresh, _ = build(tmp_path, [end_turn()], clock=clock, bind=False)
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "completed"
    assert fresh.restored == [2] and fresh.prompts == 0 and fresh.resumes == 1
    assert fresh.sent == 1 and harness.sent == 1  # turn 1 is never replayed
    assert [c["turn"] for c in store.model_calls("run1")] == [1, 2]
    assert row["tokens"] == totals["tokens"] + 150
    (resumed,) = store.loop_resumes("run1")
    assert resumed["restored_from_disk"] is True and resumed["turn"] == 2
    assert resumed["state_sha256"] == digest
    starts = [r for r in records(store) if r["kind"] == "agent" and r["outcome"] == "started"]
    resumes = [r for r in starts if r["content_digest"] == "sha256:" + digest]
    assert len(resumes) == 1
    assert store.effect("run1", "resume.2") == "acknowledged"
    assert not pending_path(tmp_path).exists()  # deleted on completion
    store.close()


def test_in_memory_resume_receipt_is_not_from_disk(tmp_path):
    store, supervisor, harness, clock = park(tmp_path)
    clock.now = DAY1 + 30
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert harness.restored == [] and harness.resumes == 1
    (resumed,) = store.loop_resumes("run1")
    assert resumed["restored_from_disk"] is False
    assert not pending_path(tmp_path).exists()


def tamper(path):
    path.write_bytes(path.read_bytes().replace(b'"turn":2', b'"turn":3'))


def drop(path):
    path.unlink()


def bump_totals(store):
    store.usage("run1", store.get("run1")["tokens"] + 1, store.get("run1")["cost"])


@pytest.mark.parametrize(
    "damage", ["tamper", "missing", "totals", "snapshot_totals", "snapshot_tool_calls"]
)
def test_invalid_parked_state_halts(tmp_path, damage):
    store, _, _, clock = park(tmp_path)
    path = pending_path(tmp_path)
    if damage == "tamper":
        tamper(path)
    elif damage == "missing":
        drop(path)
    elif damage == "totals":
        bump_totals(store)
    else:
        # Self-consistent file and journal digest, but the snapshot differs from the journal:
        # its cumulative usage, or its tool_calls_used versus the answered tool rows.
        state = json.loads(path.read_bytes())
        if damage == "snapshot_totals":
            state["cumulative"]["tokens"] += 7
        else:
            assert state["tool_calls_used"] == 1
            state["tool_calls_used"] = 2
        data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        store.loop_park("run1", 2, store.write_pending("run1", data), DAY1)
    store.close()
    clock.now = DAY1 + 30
    store, supervisor, fresh, _ = build(tmp_path, [end_turn()], clock=clock, bind=False)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_resume_state_invalid")
    assert fresh.sent == 0 and fresh.restored == [] and store.loop_resumes("run1") == []
    assert not path.exists()
    store.close()


def test_parked_snapshot_counts_answered_tool_calls(tmp_path):
    # Turn 1: one executed (succeeded) and one denied call; both answered, both counted.
    script = [tool_turn(SEARCH, ROGUE), reject(retry_after=30), end_turn()]
    store, _, _, _ = park(tmp_path, script)
    state = json.loads(pending_path(tmp_path).read_bytes())
    assert [t["status"] for t in store.tool_calls("run1")] == ["succeeded", "denied"]
    assert state["tool_calls_used"] == 2
    store.close()


@pytest.mark.parametrize("restart", [True, False])
def test_started_row_while_parked_halts_uncertain_before_restore(tmp_path, restart):
    store, supervisor, harness, clock = park(tmp_path)
    path = pending_path(tmp_path)
    assert path.exists()
    # A tool call journaled as started whose process died before its completion.
    intent = {"turn": 1, "index": 1, "name": "tessera_search", "input_sha256": "c" * 64}
    store.tool_call_begin("run1", intent | {"allowed": True}, DAY1)
    clock.now = DAY1 + 30
    if restart:
        store.close()
        store, supervisor, harness, _ = build(tmp_path, [end_turn()], clock=clock, bind=False)
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "tool_call_uncertain")
    # Halted before any restore or resume: nothing sent, nothing replayed, file deleted.
    assert harness.restored == [] and harness.resumes == 0
    assert store.loop_resumes("run1") == [] and not path.exists()
    assert [t["status"] for t in store.tool_calls("run1")] == ["succeeded", "uncertain"]
    assert supervisor.tick("run1", PROMPT)["reason"] == "tool_call_uncertain"
    assert harness.restored == [] and harness.resumes == 0
    store.close()


def test_pending_file_refused_when_not_private(tmp_path):
    store, _, _, _ = park(tmp_path)
    pending_path(tmp_path).chmod(0o644)
    with pytest.raises(ValueError):
        store.read_pending("run1")
    with pytest.raises(ValueError):
        store.write_pending("../run1", b"{}")


def test_pending_file_deleted_on_halt_and_stop(tmp_path):
    store, supervisor, _, clock = park(tmp_path)
    store.stop("run1")
    assert not pending_path(tmp_path).exists()
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "stop_requested"


def test_park_unrecorded_when_file_cannot_be_written(tmp_path):
    script = [tool_turn(SEARCH), reject(retry_after=30), end_turn()]
    store, supervisor, harness, _ = build(tmp_path, script)

    def fail(*_):
        raise OSError("disk full")

    store.write_pending = fail
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_park_unrecorded"
    assert store.loop_parks("run1") == []


def _fail_tool_turn(store):
    def fail(*_):
        raise ValueError("outbox write failed")

    store.tool_turn = fail


def test_tool_turn_flush_failure_never_masks_the_loop_error(tmp_path):
    store, supervisor, harness, _ = build(tmp_path, [tool_turn(SEARCH, SEARCH), end_turn()])
    calls = []

    def call(name, arguments):
        calls.append(name)
        if len(calls) == 2:
            raise _blocked(RuntimeBlocked("tool_server_unavailable"), sent=True)
        return TOOL_RESULT, False

    harness.tool_bridge.call = call
    _fail_tool_turn(store)
    row = supervisor.tick("run1", PROMPT)
    # The loop's own code survives; the failed flush leaves turn 1 journal-only.
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "tool_server_unavailable")
    assert [(t["turn"], t["index"], t["status"]) for t in store.tool_calls("run1")] == [
        (1, 0, "succeeded"),
        (1, 1, "started"),  # the fake adapter never completed it
    ]
    assert tool_turn_records(store) == []
    assert store.effect("run1", "prompt") == "acknowledged"


def test_tool_turn_flush_failure_without_loop_error_still_halts(tmp_path):
    profile = make_profile()
    profile["max_steps"] = 1  # the loop ends normally with turn 1's tools still open
    store, supervisor, _, _ = build(tmp_path, [tool_turn(SEARCH)], profile=profile)
    _fail_tool_turn(store)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "uncertain_prompt"
    assert store.effect("run1", "prompt") == "uncertain"
    assert tool_turn_records(store) == []


def test_printed_receipt_includes_park_and_resume(tmp_path, monkeypatch, capsys):
    creds = tmp_path / "credentials"
    creds.mkdir(mode=0o700)
    for name, value in (("anthropic-api-key", API_KEY), ("agent-token", "token")):
        (creds / name).write_text(value + "\n")
        (creds / name).chmod(0o600)
    script = [tool_turn(SEARCH), reject(retry_after=0.001), end_turn()]
    monkeypatch.setattr(base, "FakeLoop", DurableLoop)
    code, _ = run_main(tmp_path, creds, monkeypatch, make_profile(), script)
    receipt = json.loads(capsys.readouterr().out)
    assert code == 0 and receipt["state"] == "completed"
    assert [p["turn"] for p in receipt["loop_parks"]] == [2]
    assert [(r["turn"], r["restored_from_disk"]) for r in receipt["loop_resumes"]] == [(2, False)]


# -- per-turn tool provenance ------------------------------------------------------------------


def test_tool_turn_provenance_matches_journal(tmp_path):
    script = [tool_turn(SEARCH, ROGUE), tool_turn(SEARCH), end_turn()]
    store, supervisor, _, _ = build(tmp_path, script)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    found = tool_turn_records(store)
    assert [r["tool_turn"]["turn"] for r in found] == [1, 2]
    journal = store.tool_calls("run1")
    for record in found:
        turn = record["tool_turn"]["turn"]
        rows = [r for r in journal if r["turn"] == turn]
        projected = [
            {
                k: r[k]
                for k in (
                    "index",
                    "name",
                    "input_sha256",
                    "result_sha256",
                    "is_error",
                    "truncated",
                    "allowed",
                    "status",
                )
            }
            for r in rows
        ]
        assert record["content_digest"] == "sha256:" + tool_turn_digest(projected)
        assert record["kind"] == "tool" and record["tool_id"] == "tessera_tool_turn"
    first, second = found
    # Status-based counts: the denied call has an error result but is counted as denied.
    assert first["tool_turn"] == {
        "turn": 1,
        "calls": 2,
        "errors": 0,
        "denied": 1,
        "truncated": 0,
        "unrun": 0,
        "refused": 0,
        "uncertain": 0,
    }
    assert first["outcome"] == second["outcome"] == "succeeded"
    assert second["tool_turn"]["calls"] == 1


def test_unrun_last_turn_tools_are_refused_turn_limit_and_counted(tmp_path):
    profile = make_profile(max_steps=2)
    store, supervisor, harness, _ = build(
        tmp_path, [tool_turn(SEARCH), tool_turn(SEARCH, SEARCH)], profile=profile
    )
    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("failed", "turn_limit_reached")
    assert harness.tool_bridge.calls == ["tessera_search"]  # the last turn's tools never ran
    unrun = [t for t in store.tool_calls("run1") if t["turn"] == 2]
    assert len(unrun) == 2
    assert all(
        t["status"] == "refused"
        and t["skip_reason"] == "turn_limit_reached"
        and (t["result_sha256"], t["is_error"], t["truncated"]) == (None, None, None)
        for t in unrun
    )
    assert [c["unrun_tool_count"] for c in store.model_calls("run1")] == [0, 2]
    last = tool_turn_records(store)[-1]
    assert last["tool_turn"] == {
        "turn": 2,
        "calls": 2,
        "errors": 0,
        "denied": 0,
        "truncated": 0,
        "unrun": 2,
        "refused": 0,  # turn-limit refusals are counted as unrun only
        "uncertain": 0,
    }
    assert last["outcome"] == "succeeded"


def test_tool_turn_overflow_is_recorded_once_and_outbox_bounded(tmp_path, monkeypatch):
    profile = make_profile(max_steps=6, limits=make_profile()["limits"] | {"max_tool_calls": 10})
    script = [tool_turn(SEARCH)] * 5 + [end_turn()]
    store, supervisor, _, _ = build(tmp_path, script, profile=profile)
    before = store.provenance_coverage("run1")["recorded"]
    monkeypatch.setattr(rs, "TOOL_TURN_OUTBOX_LIMIT", before + 6)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    found = tool_turn_records(store)
    overflow = [r for r in found if r["tool_id"] == "tessera_tool_turn_overflow"]
    assert len(overflow) == 1
    assert overflow[0]["outcome"] == "unknown" and overflow[0]["content_digest"] is None
    normal = [r["tool_turn"]["turn"] for r in found if r["tool_id"] == "tessera_tool_turn"]
    assert normal and normal == list(range(1, len(normal) + 1))
    assert overflow[0]["tool_turn"]["turn"] == len(normal) + 1 <= 5
    assert store.provenance_coverage("run1")["recorded"] <= 64
    # Later turns stay journal-only; the journal still holds every call.
    assert len(store.tool_calls("run1")) == 5


def test_outbox_never_exceeds_bound_with_default_threshold(tmp_path):
    profile = make_profile(
        max_steps=16, limits=make_profile()["limits"] | {"max_tool_calls": 64}, max_tokens=32768
    )
    script = [tool_turn(SEARCH, input_tokens=1, output_tokens=1)] * 15 + [end_turn()]
    store, supervisor, _, _ = build(tmp_path, script, profile=profile)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert store.provenance_coverage("run1")["recorded"] <= 64
    assert len(tool_turn_records(store)) == 15


# -- store shapes and migration ----------------------------------------------------------------

DENIED = {
    "turn": 1,
    "index": 0,
    "name": "tessera_search",
    "input_sha256": "a" * 64,
    "result_sha256": "b" * 64,
    "is_error": True,
    "truncated": False,
    "allowed": False,
    "status": "denied",
    "skip_reason": None,
}
UNRUN = DENIED | {
    "result_sha256": None,
    "is_error": None,
    "truncated": None,
    "allowed": True,
    "status": "refused",
    "skip_reason": "turn_limit_reached",
}


def test_store_accepts_only_never_executed_terminal_rows(tmp_path):
    store, _, _, _ = build(tmp_path, [])
    for good in (DENIED, UNRUN, UNRUN | {"allowed": False}, UNRUN | {"skip_reason": "stopped"}):
        store.tool_call("run1", good, DAY1)
    for bad in (
        DENIED | {"status": "succeeded"},  # executed rows need an intent first
        DENIED | {"status": "started"},
        DENIED | {"allowed": True},
        DENIED | {"is_error": False},
        DENIED | {"result_sha256": None},
        DENIED | {"skip_reason": "turn_limit_reached"},
        UNRUN | {"result_sha256": "b" * 64},
        UNRUN | {"is_error": False},
        UNRUN | {"truncated": False},
        UNRUN | {"skip_reason": None},
        UNRUN | {"skip_reason": "Bad Code"},
        DENIED | {"executed": False},  # the removed legacy field is refused
    ):
        with pytest.raises(ValueError):
            store.tool_call("run1", bad, DAY1)
    rows = store.tool_calls("run1")
    assert [(r["status"], r["skip_reason"]) for r in rows] == [
        ("denied", None),
        ("refused", "turn_limit_reached"),
        ("refused", "turn_limit_reached"),
        ("refused", "stopped"),
    ]
    assert all("executed" not in r for r in rows)


def test_old_state_dir_migrates_tool_calls(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / "reasoning.sqlite3")
    db.executescript(
        """
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
          VALUES('old','{}','d','ses_x','msg_y',1);
        """
    )
    db.execute(
        "INSERT INTO tool_calls VALUES('old',1,1,0,'tessera_search',?,?,0,0,1,5)",
        ("a" * 64, "b" * 64),
    )
    db.commit()
    db.close()
    os.chmod(directory / "reasoning.sqlite3", 0o600)
    store = ReasoningStore(directory)
    (row,) = store.tool_calls("old")
    assert (row["status"], row["skip_reason"]) == ("succeeded", None)
    assert row["result_sha256"] == "b" * 64
    store.tool_call("old", UNRUN | {"turn": 2}, 6)
    assert store.tool_calls("old")[-1]["result_sha256"] is None
    assert store.loop_parks("old") == [] and store.loop_resumes("old") == []
    store.close()
    ReasoningStore(directory).close()  # idempotent


def test_executed_skip_reason_state_dir_migrates_to_status(tmp_path):
    # The interim shape: an ``executed`` flag and a ``skip_reason`` for unrun last-turn rows.
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / "reasoning.sqlite3")
    db.executescript(
        """
        CREATE TABLE runs (id TEXT PRIMARY KEY, binding TEXT NOT NULL, digest TEXT NOT NULL,
          session_id TEXT NOT NULL, message_id TEXT NOT NULL, started REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'ready', stop_requested INTEGER NOT NULL DEFAULT 0,
          reason TEXT, tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0);
        CREATE TABLE tool_calls (
          run_id TEXT NOT NULL, seq INTEGER NOT NULL, turn INTEGER NOT NULL,
          tool_index INTEGER NOT NULL, name TEXT NOT NULL, input_sha256 TEXT NOT NULL,
          result_sha256 TEXT, is_error INTEGER NOT NULL, truncated INTEGER NOT NULL,
          allowed INTEGER NOT NULL, observed_at REAL NOT NULL,
          executed INTEGER NOT NULL DEFAULT 1, skip_reason TEXT,
          PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
        INSERT INTO runs(id,binding,digest,session_id,message_id,started)
          VALUES('old','{}','d','ses_x','msg_y',1);
        """
    )
    a, b = "a" * 64, "b" * 64
    for values in (
        (1, 1, 0, "tessera_search", a, b, 0, 1, 1, 5, 1, None),  # ran, truncated
        (2, 1, 1, "tessera_search", a, b, 1, 0, 1, 5, 1, None),  # ran, errored
        (3, 1, 2, "unlisted", a, b, 1, 0, 0, 5, 0, None),  # denied
        (4, 2, 0, "tessera_search", a, None, 0, 0, 1, 6, 0, "turn_limit_reached"),
        (5, 2, 1, "unlisted", a, None, 0, 0, 0, 6, 0, "turn_limit_reached"),
    ):
        db.execute("INSERT INTO tool_calls VALUES('old',?,?,?,?,?,?,?,?,?,?,?,?)", values)
    db.commit()
    db.close()
    os.chmod(directory / "reasoning.sqlite3", 0o600)
    store = ReasoningStore(directory)
    rows = store.tool_calls("old")
    assert [
        (r["status"], r["skip_reason"], r["result_sha256"], r["is_error"], r["truncated"])
        for r in rows
    ] == [
        ("succeeded", None, b, False, True),
        ("error", None, b, True, False),
        ("denied", None, b, True, False),
        ("refused", "turn_limit_reached", None, None, None),
        ("refused", "turn_limit_reached", None, None, None),
    ]
    assert all("executed" not in r and r["completed_at"] == r["observed_at"] for r in rows)
    assert "executed" not in store._columns("tool_calls")
    store.close()
    ReasoningStore(directory).close()  # idempotent


# -- control model -----------------------------------------------------------------------------

BASE = {
    "record_id": "runtime_abc",
    "kind": "tool",
    "outcome": "succeeded",
    "correlation_id": "ses_1",
}
TURN = {"turn": 3, "calls": 2, "errors": 0, "denied": 1, "truncated": 0, "unrun": 0}


def test_record_without_tool_turn_serialises_as_before():
    dumped = AgentProvenanceRecord.model_validate(BASE).model_dump(mode="json")
    # Exactly the pre-change wire keys: absent optional metadata is omitted.
    assert set(dumped) == {
        "record_id",
        "kind",
        "outcome",
        "correlation_id",
        "causation_id",
        "model_id",
        "tool_id",
        "references",
        "remote",
        "grant_revision",
        "grant_epoch",
        "content_digest",
        "error_code",
    }
    before = sanitize_envelope(BASE)
    assert "tool_turn" not in before["record"]
    digest = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
    again = sanitize_envelope(dict(BASE))
    assert hashlib.sha256(json.dumps(again, sort_keys=True).encode()).hexdigest() == digest
    with_turn = AgentProvenanceRecord.model_validate(BASE | {"tool_turn": TURN})
    assert with_turn.model_dump(mode="json")["tool_turn"] == TURN  # absent counts omitted
    full = TURN | {"refused": 1, "uncertain": 0}
    with_counts = AgentProvenanceRecord.model_validate(BASE | {"tool_turn": full})
    assert with_counts.model_dump(mode="json")["tool_turn"] == full


@pytest.mark.parametrize(
    "bad",
    [
        TURN | {"turn": 0},
        TURN | {"turn": 17},
        TURN | {"calls": 65},
        TURN | {"errors": -1},
        TURN | {"unrun": True},
        TURN | {"calls": "2"},
        TURN | {"extra": 1},
        TURN | {"refused": 65},
        TURN | {"uncertain": -1},
        TURN | {"refused": True},
        {k: v for k, v in TURN.items() if k != "unrun"},
    ],
)
def test_tool_turn_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        AgentProvenanceRecord.model_validate(BASE | {"tool_turn": bad})
