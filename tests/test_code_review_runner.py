import base64
import json
import os
import stat
import sys
import threading
import time

import pytest

from tessera_control import code_review_runner as crr
from tessera_control.code_review_runner import (
    ChildOutcome,
    CodeReviewRunner,
    SubprocessReviewModel,
    read_available_memory_mib,
)
from tessera_control.review_jobs import ReviewRefused
from tessera_control.store import Conflict, StoreError

D = "a" * 64
PINNED = "b" * 64
RECEIPT = {"cost_basis": "computed_from_pinned_prices", "cost_cents": 3, "tokens": {"total": 9}}
CONTENT = b"print(1)\n"
LOADED = [
    {
        "snapshot_id": "s1",
        "digest": D,
        "data_class": "public",
        "files": [{"path": "a.py", "content": CONTENT}],
    }
]
FAIL_REASONS = {
    "budget_exceeded",
    "model_failed",
    "failed_output_invalid",
    "model_child_vanished",
    "input_too_large",
    "input_unavailable",
    "prompt_not_pinned",
}
NOT_START_REASONS = {
    "destination_not_enrolled",
    "policy_check_failed",
    "prompt_not_pinned",
    "egress_not_permitted",
    "destination_not_supported",
}


@pytest.fixture(autouse=True)
def _pinned_prompt(monkeypatch):
    # Unit tests pin a stand-in digest; the real child constant is checked separately.
    monkeypatch.setattr(crr, "pinned_prompt_digests", lambda: frozenset({PINNED}))


class FakeStore:
    """In-memory store with the documented runner-protocol semantics (ADR 0092)."""

    def __init__(self, global_running=0):
        self.lock = threading.Lock()
        self.jobs = []
        self.calls = []
        self.complete_error = None
        self.inputs_error = None
        self.loaded = {}
        self.global_running = global_running  # running jobs owned by other hosts
        self.renewals = []
        self.max_seen = 0
        self.passed_attempts = []  # (operation, attempt) per terminal call

    def add(self, job_id, budget=100, **overrides):
        job = {
            "id": job_id,
            "kind": "code_review",
            "state": "queued",
            "attempts": 0,
            "lease_owner": None,
            "lease_expires_at": None,
            "reason": None,
            "inputs": [{"snapshot_id": "s1", "digest": D, "data_class": "public"}],
            "subject": {"mode": "snapshot", "head_digest": D, "base_digest": None},
            "destination_id": "anthropic",
            "tier": "sonnet",
            "budget_cents": budget,
            "egress": "model_runtime",
            "prompt_digest": PINNED,
            "attempt_receipts": [],
        }
        job.update(overrides)
        self.jobs.append(job)
        self.loaded[job_id] = LOADED

    def job(self, job_id):
        return next(j for j in self.jobs if j["id"] == job_id)

    def _record(self, j):
        hidden = ("subject", "attempt_receipts", "cost_cents_total")
        r = {k: v for k, v in j.items() if k not in hidden}
        r["code_review"] = {**j["subject"], "rubric_digest": D, "attempts": j["attempts"]}
        if "cost_cents_total" in j:
            r["code_review"]["cost_cents_total"] = j["cost_cents_total"]
        return r

    def _running(self):
        return sum(1 for j in self.jobs if j["state"] == "running") + self.global_running

    def _owned(self, job_id, runner_id, now):
        j = self.job(job_id)
        if j["state"] != "running" or j["lease_owner"] != runner_id:
            raise Conflict("code review job is not leased by this runner")
        if j["lease_expires_at"] < now:
            raise Conflict("code review lease expired")
        return j

    def _receipt(self, j, outcome, reason, receipt):
        j["attempt_receipts"].append(
            {"attempt": j["attempts"], "outcome": outcome, "reason": reason, "receipt": receipt}
        )

    def claim_code_review(self, runner_id, max_running, lease_seconds, now):
        with self.lock:
            self.calls.append("claim")
            if self._running() >= max_running:
                return None
            for j in self.jobs:
                if j["state"] == "queued":
                    j.update(
                        state="running",
                        attempts=j["attempts"] + 1,
                        lease_owner=runner_id,
                        lease_expires_at=now + lease_seconds,
                    )
                    self.max_seen = max(self.max_seen, self._running())
                    return self._record(j)
            return None

    def renew_code_review_lease(self, job_id, runner_id, lease_seconds, now):
        with self.lock:
            j = self.job(job_id)
            if j["state"] != "running" or j["lease_owner"] != runner_id:
                return False
            j["lease_expires_at"] = now + lease_seconds
            self.renewals.append(job_id)
            return True

    def code_review_inputs(self, job_id, runner_id, now):
        with self.lock:
            self.calls.append(("inputs", job_id, runner_id))
            self._owned(job_id, runner_id, now)
            if self.inputs_error:
                raise self.inputs_error
            return self.loaded[job_id]

    def _vanish(self, j):
        j["lease_owner"] = j["lease_expires_at"] = None
        if j["attempts"] < 2:
            j["state"] = "queued"
            return "queued"
        j["state"], j["reason"] = "failed", "model_child_vanished"
        return "failed"

    def requeue_vanished_code_review(self, job_id, runner_id, now, receipt=None, attempt=None):
        self.passed_attempts.append(("requeue", attempt))
        with self.lock:
            j = self._owned(job_id, runner_id, now)
            self._receipt(j, "vanished", "model_child_vanished", receipt)
            return self._vanish(j)

    def complete_code_review(self, job_id, runner_id, output, now, attempt=None):
        self.passed_attempts.append(("complete", attempt))
        with self.lock:
            j = self._owned(job_id, runner_id, now)
            if self.complete_error:
                raise self.complete_error
            self._receipt(j, "completed", None, output.get("receipt"))
            j.update(state="completed", output=output, lease_owner=None)
            return self._record(j)

    def fail_code_review(self, job_id, runner_id, reason, now, receipt=None, attempt=None):
        self.passed_attempts.append(("fail", attempt))
        if reason not in FAIL_REASONS:
            raise ValueError("unknown code review failure reason")
        with self.lock:
            j = self._owned(job_id, runner_id, now)
            self._receipt(j, "failed", reason, receipt)
            j.update(state="failed", reason=reason, lease_owner=None)
            return self._record(j)

    def not_start_code_review(self, job_id, runner_id, reason, now):
        if reason not in NOT_START_REASONS:
            raise ValueError("unknown not-started reason")
        with self.lock:
            j = self._owned(job_id, runner_id, now)
            j.update(state="not_started", reason=reason, lease_owner=None)
            return self._record(j)

    def reap_expired_code_review_leases(self, now):
        with self.lock:
            out = []
            for j in self.jobs:
                if j["state"] == "running" and j["lease_expires_at"] < now:
                    self._vanish(j)
                    out.append(j["id"])
            return out


class AllowPolicy:
    def __init__(self, reason=None):
        self.reason, self.seen = reason, []

    def check(self, destination_id, tier, classes, budget):
        self.seen.append((destination_id, tier, classes, budget))
        return self.reason


class ScriptModel:
    """Each scripted outcome is a ChildOutcome or a callable(extra) -> ChildOutcome."""

    def __init__(self, *outcomes):
        self.outcomes, self.attempts, self.extras = list(outcomes), [], []

    def run(self, job, attempt, extra):
        self.attempts.append((job["id"], attempt))
        self.extras.append(extra)
        outcome = self.outcomes.pop(0)
        return outcome(extra) if callable(outcome) else outcome


def make(store, model, policy=None, **kw):
    kw.setdefault("memory_probe", lambda: 4096)
    return CodeReviewRunner(store, model, policy or AllowPolicy(), runner_id="r1", **kw)


def run_all(runner):
    runner.run_once()
    runner.drain()


def write_receipt(extra, receipt=RECEIPT):
    with open(extra["receipt_path"], "w") as handle:
        json.dump(receipt, handle)


GOOD = {"verdict": "MERGE", "findings": [], "receipt": RECEIPT}


def test_completed_job_is_stored_verbatim():
    s = FakeStore()
    s.add("j1")
    r = make(s, ScriptModel(ChildOutcome.completed(GOOD)))
    run_all(r)
    assert s.job("j1")["state"] == "completed" and s.job("j1")["output"] is GOOD
    assert s.job("j1")["attempt_receipts"][0]["receipt"] == RECEIPT
    assert r.in_flight == 0


def test_max_concurrent_never_exceeded_and_drains_queue():
    s = FakeStore()
    for i in range(6):
        s.add(f"j{i}")
    gate, lock = threading.Event(), threading.Lock()
    state = {"now": 0, "peak": 0}

    class Blocking:
        def run(self, job, attempt, extra):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            gate.wait(5)
            with lock:
                state["now"] -= 1
            return ChildOutcome.completed(GOOD)

    r = make(s, Blocking(), max_concurrent=2)
    assert r.run_once() == 2
    assert r.run_once() == 0  # still full
    time.sleep(0.1)
    assert state["peak"] == 2 and r.in_flight == 2
    gate.set()
    r.drain()
    while any(j["state"] == "queued" for j in s.jobs):
        r.run_once()
        r.drain()
    assert state["peak"] <= 2 and s.max_seen <= 2
    assert all(j["state"] == "completed" for j in s.jobs)


def test_store_global_bound_respected():
    s = FakeStore(global_running=2)
    s.add("j1")
    r = make(s, ScriptModel(), max_concurrent=2)
    assert r.run_once() == 0
    assert s.job("j1")["state"] == "queued"


def test_low_or_unknown_memory_claims_nothing():
    s = FakeStore()
    s.add("j1")
    m = ScriptModel()
    assert make(s, m, memory_probe=lambda: 10).run_once() == 0
    assert make(s, m, memory_probe=lambda: 0.0).run_once() == 0

    def boom():
        raise OSError

    assert make(s, m, memory_probe=boom).run_once() == 0
    assert "claim" not in s.calls and s.job("j1")["state"] == "queued"


def test_meminfo_unreadable_is_zero(monkeypatch):
    def nope(*a, **k):
        raise OSError

    monkeypatch.setattr("builtins.open", nope)
    assert read_available_memory_mib() == 0.0


def test_meminfo_parsed(monkeypatch, tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal: 10 kB\nMemAvailable:    2097152 kB\n")
    real = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real(f, *a, **k))
    assert read_available_memory_mib() == 2048.0
    f.write_text("MemTotal: 10 kB\n")
    assert read_available_memory_mib() == 0.0


def test_vanished_retried_exactly_once():
    s = FakeStore()
    s.add("j1")
    m = ScriptModel(ChildOutcome.vanished("x"), ChildOutcome.vanished("y"))
    r = make(s, m)
    for _ in range(4):  # a requeued job is re-claimed through the normal claim path
        run_all(r)
    j = s.job("j1")
    assert (j["state"], j["reason"], j["attempts"]) == ("failed", "model_child_vanished", 2)
    assert m.attempts == [("j1", 1), ("j1", 2)]
    run_all(r)
    assert len(m.attempts) == 2


def test_vanish_then_success():
    s = FakeStore()
    s.add("j1")
    r = make(s, ScriptModel(ChildOutcome.vanished(), ChildOutcome.completed(GOOD)))
    run_all(r)
    run_all(r)
    assert s.job("j1")["state"] == "completed"


def test_policy_refusal_not_started_and_model_untouched():
    s = FakeStore()
    s.add("j1")
    m = ScriptModel()
    p = AllowPolicy("destination_not_enrolled")
    r = make(s, m, p)
    assert r.run_once() == 0
    j = s.job("j1")
    assert (j["state"], j["reason"]) == ("not_started", "destination_not_enrolled")
    assert m.attempts == [] and p.seen == [("anthropic", "sonnet", ["public"], 100)]
    assert not any(isinstance(c, tuple) for c in s.calls)  # no content was loaded


def test_policy_exception_fails_closed():
    class Bad:
        def check(self, *a):
            raise RuntimeError

    s = FakeStore()
    s.add("j1")
    make(s, ScriptModel(), Bad()).run_once()
    assert (s.job("j1")["state"], s.job("j1")["reason"]) == ("not_started", "policy_check_failed")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"prompt_digest": D}, "prompt_not_pinned"),
        ({"prompt_digest": None}, "prompt_not_pinned"),
        ({"egress": "none"}, "egress_not_permitted"),
        ({"egress": "public_internet"}, "egress_not_permitted"),
        ({"destination_id": "other-host"}, "destination_not_supported"),
    ],
)
def test_launch_checks_not_started_and_model_untouched(overrides, reason):
    s = FakeStore()
    s.add("j1", **overrides)
    m = ScriptModel()
    assert make(s, m).run_once() == 0
    assert (s.job("j1")["state"], s.job("j1")["reason"]) == ("not_started", reason)
    assert m.attempts == [] and not any(isinstance(c, tuple) for c in s.calls)


def test_policy_refusal_precedes_launch_checks():
    s = FakeStore()
    s.add("j1", destination_id="other-host")
    make(s, ScriptModel(), AllowPolicy("destination_not_enrolled")).run_once()
    assert s.job("j1")["reason"] == "destination_not_enrolled"


def test_missing_pinned_prompt_fails_closed(monkeypatch):
    monkeypatch.setattr(crr, "pinned_prompt_digests", frozenset)
    s = FakeStore()
    s.add("j1")
    m = ScriptModel()
    make(s, m).run_once()
    assert (s.job("j1")["state"], s.job("j1")["reason"]) == ("not_started", "prompt_not_pinned")
    assert m.attempts == []


def test_real_pinned_prompt_digests_are_the_child_constants(monkeypatch):
    monkeypatch.undo()  # the autouse stand-in
    from tessera_agent.code_review_child import (
        CODE_RECHECK_PROMPT_SHA256,
        CODE_REVIEW_PROMPT_SHA256,
    )

    assert crr.pinned_prompt_digests() == {CODE_REVIEW_PROMPT_SHA256, CODE_RECHECK_PROMPT_SHA256}
    assert CODE_REVIEW_PROMPT_SHA256 != CODE_RECHECK_PROMPT_SHA256
    assert all(len(item) == 64 for item in crr.pinned_prompt_digests())


def test_either_pinned_digest_launches(monkeypatch):
    monkeypatch.setattr(crr, "pinned_prompt_digests", lambda: frozenset({PINNED, "c" * 64}))
    s = FakeStore()
    s.add("j1")
    s.add("j2", prompt_digest="c" * 64)
    s.add("j3", prompt_digest="d" * 64)
    m = ScriptModel(ChildOutcome.completed(GOOD), ChildOutcome.completed(GOOD))
    runner = make(s, m)
    for _ in range(3):  # one launch per pass under the default concurrency
        run_all(runner)
    assert (s.job("j3")["state"], s.job("j3")["reason"]) == ("not_started", "prompt_not_pinned")
    assert sorted(job_id for job_id, _ in m.attempts) == ["j1", "j2"]
    assert s.job("j1")["state"] == s.job("j2")["state"] == "completed"


def test_inputs_loaded_and_base64_encoded_into_spec():
    s = FakeStore()
    s.add("j1")
    profile = {"model": {"providerID": "anthropic", "id": "m"}}
    m = ScriptModel(ChildOutcome.completed(GOOD))
    run_all(make(s, m, profile=profile))
    assert ("inputs", "j1", "r1") in s.calls
    (extra,) = m.extras
    assert extra["inputs"] == [
        {
            "snapshot_id": "s1",
            "digest": D,
            "data_class": "public",
            "files": [{"path": "a.py", "content_b64": base64.b64encode(CONTENT).decode()}],
        }
    ]
    assert extra["profile"] is profile
    assert os.path.isabs(extra["receipt_path"])
    spec = crr.job_spec(s._record(s.job("j1")), 1, "anthropic-api-key", extra)
    assert spec["inputs"] == extra["inputs"] and spec["receipt_path"] == extra["receipt_path"]
    assert spec["code_review"] == {
        "mode": "snapshot",
        "head_digest": D,
        "base_digest": None,
        "rubric_digest": D,
    }
    json.dumps(spec)  # the spec is plain JSON


@pytest.mark.parametrize(
    "error", [Conflict("lease"), StoreError("custody"), ReviewRefused("snapshot_unavailable")]
)
def test_input_load_failure_fails_input_unavailable(error):
    s = FakeStore()
    s.add("j1")
    s.inputs_error = error
    m = ScriptModel()
    run_all(make(s, m))
    j = s.job("j1")
    assert (j["state"], j["reason"]) == ("failed", "input_unavailable") and m.attempts == []


@pytest.mark.parametrize(
    "loaded",
    [
        [],  # not the job's inputs
        [{**LOADED[0], "digest": "c" * 64}],
        [{**LOADED[0], "files": [{"path": "a.py", "content": "text, not bytes"}]}],
        [{"snapshot_id": "s1", "digest": D}],
        None,
    ],
)
def test_inputs_not_matching_job_fail_input_unavailable(loaded):
    s = FakeStore()
    s.add("j1")
    s.loaded["j1"] = loaded
    m = ScriptModel()
    run_all(make(s, m))
    assert s.job("j1")["reason"] == "input_unavailable" and m.attempts == []


def test_completion_422_budget_vs_invalid_keeps_receipt():
    over = {
        "verdict": "MERGE",
        "findings": [],
        "receipt": {"cost_basis": "reported_cost", "cost_cents": 500},
    }
    for output, reason in ((over, "budget_exceeded"), (GOOD, "failed_output_invalid")):
        s = FakeStore()
        s.add("j1", budget=100)
        s.complete_error = ReviewRefused("failed_output_invalid", 422)
        run_all(make(s, ScriptModel(ChildOutcome.completed(output))))
        j = s.job("j1")
        assert (j["state"], j["reason"]) == ("failed", reason)
        assert "output" not in j
        assert j["attempt_receipts"] == [
            {"attempt": 1, "outcome": "failed", "reason": reason, "receipt": output["receipt"]}
        ]


def test_completion_422_store_detail_budget_exceeded():
    s = FakeStore()
    s.add("j1")
    s.complete_error = ReviewRefused("failed_output_invalid", 422, detail="budget_exceeded")
    run_all(make(s, ScriptModel(ChildOutcome.completed(GOOD))))
    assert s.job("j1")["reason"] == "budget_exceeded"
    assert s.job("j1")["attempt_receipts"][0]["receipt"] == RECEIPT


def test_completion_422_without_output_receipt_uses_receipt_file():
    output = {"verdict": "MERGE", "findings": []}
    s = FakeStore()
    s.add("j1")
    s.complete_error = ReviewRefused("failed_output_invalid", 422)

    def child(extra):
        write_receipt(extra)
        return ChildOutcome.completed(output)

    run_all(make(s, ScriptModel(child)))
    assert s.job("j1")["attempt_receipts"][0]["receipt"] == RECEIPT


def test_reported_cost_missing_cents_counts_as_budget():
    out = {"receipt": {"cost_basis": "reported_cost"}}
    assert crr._over_budget(out, 100) is True
    assert (
        crr._over_budget({"receipt": {"cost_basis": "reported_cost", "cost_cents": 5}}, 100)
        is False
    )


@pytest.mark.parametrize(
    ("receipt", "over"),
    [
        ({"cost_basis": "computed_from_pinned_prices", "cost_cents": 101}, True),
        ({"cost_basis": "computed_from_pinned_prices", "cost_cents": 100}, False),
        ({"cost_basis": "computed_from_pinned_prices"}, True),
        ({"cost_basis": "computed_from_pinned_prices", "cost_cents": True}, True),
        ({"cost_basis": "included_subscription_usage", "cost_cents": 999}, False),
    ],
)
def test_pinned_price_cost_is_billed_against_budget(receipt, over):
    assert crr._over_budget({"receipt": receipt}, 100) is over


def test_pinned_price_over_budget_fails_as_budget_exceeded():
    out = {
        "verdict": "MERGE",
        "findings": [],
        "receipt": {"cost_basis": "computed_from_pinned_prices", "cost": 250.4, "cost_cents": 251},
    }
    s = FakeStore()
    s.add("j1", budget=100)
    s.complete_error = ReviewRefused("failed_output_invalid", 422)
    run_all(make(s, ScriptModel(ChildOutcome.completed(out))))
    assert (s.job("j1")["state"], s.job("j1")["reason"]) == ("failed", "budget_exceeded")


@pytest.mark.parametrize(
    ("child_reason", "reason"),
    [
        ("budget_exceeded", "budget_exceeded"),
        ("input_too_large", "input_too_large"),
        ("failed_output_invalid", "failed_output_invalid"),
        ("prompt_not_pinned", "prompt_not_pinned"),
        ("rate_limited", "model_failed"),
        ("destination_not_supported", "model_failed"),
        ("input_unavailable", "model_failed"),
    ],
)
def test_child_failure_mapping_carries_receipt(child_reason, reason):
    s = FakeStore()
    s.add("j1")
    run_all(make(s, ScriptModel(ChildOutcome.failed(child_reason, RECEIPT))))
    j = s.job("j1")
    assert (j["state"], j["reason"]) == ("failed", reason)
    assert j["attempt_receipts"][0]["receipt"] == RECEIPT


def test_child_failure_without_receipt_passes_none():
    s = FakeStore()
    s.add("j1")
    run_all(make(s, ScriptModel(ChildOutcome.failed("budget_exceeded"))))
    assert s.job("j1")["attempt_receipts"][0]["receipt"] is None


def test_child_json_receipt_preferred_over_file():
    s = FakeStore()
    s.add("j1")
    other = {**RECEIPT, "cost_cents": 7}

    def child(extra):
        write_receipt(extra, other)
        return ChildOutcome.failed("model_failed", RECEIPT)

    run_all(make(s, ScriptModel(child)))
    assert s.job("j1")["attempt_receipts"][0]["receipt"] == RECEIPT


def test_spec_budget_is_job_budget_minus_spent():
    s = FakeStore()
    s.add("j1", budget=100)
    s.job("j1")["cost_cents_total"] = 30
    m = ScriptModel(ChildOutcome.completed(GOOD))
    run_all(make(s, m))
    assert m.extras[0]["budget_cents"] == 70
    spec = crr.job_spec(s._record(s.job("j1")), 2, None, m.extras[0])
    assert spec["budget_cents"] == 70


def test_spec_budget_without_spent_total_is_full_budget():
    s = FakeStore()
    s.add("j1", budget=100)
    m = ScriptModel(ChildOutcome.completed(GOOD))
    run_all(make(s, m))
    assert m.extras[0]["budget_cents"] == 100


@pytest.mark.parametrize(("budget", "spent"), [(100, 100), (100, 250), (100, -1), (100, "5")])
def test_spent_budget_fails_without_launch(budget, spent):
    s = FakeStore()
    s.add("j1", budget=budget)
    s.job("j1")["cost_cents_total"] = spent
    m = ScriptModel()
    run_all(make(s, m))
    j = s.job("j1")
    assert (j["state"], j["reason"]) == ("failed", "budget_exceeded")
    assert m.attempts == [] and not any(isinstance(c, tuple) for c in s.calls)
    assert j["attempt_receipts"][0]["receipt"] is None


def test_retry_after_vanish_gets_remaining_budget():
    s = FakeStore()
    s.add("j1", budget=100)

    def spend(extra):
        s.job("j1")["cost_cents_total"] = 40  # the store sums the vanished attempt's receipt
        return ChildOutcome.vanished()

    m = ScriptModel(spend, ChildOutcome.completed(GOOD))
    r = make(s, m)
    run_all(r)
    run_all(r)
    assert [e["budget_cents"] for e in m.extras] == [100, 60]


def test_remaining_budget_unit():
    assert crr.remaining_budget_cents({"budget_cents": 10}) == 10
    assert crr.remaining_budget_cents({"budget_cents": 10, "code_review": {}}) == 10
    job = {"budget_cents": 10, "code_review": {"cost_cents_total": None}}
    assert crr.remaining_budget_cents(job) == 10
    job = {"budget_cents": 10, "code_review": {"cost_cents_total": 4}}
    assert crr.remaining_budget_cents(job) == 6
    assert crr.remaining_budget_cents({"budget_cents": True}) == 0
    assert crr.remaining_budget_cents({"budget_cents": None}) == 0


def test_model_failed_and_adapter_crash():
    s = FakeStore()
    s.add("j1")
    s.add("j2")
    run_all(make(s, ScriptModel(ChildOutcome.failed("rate_limited"), ChildOutcome.failed("x"))))
    assert {s.job("j1")["reason"], s.job("j2")["reason"]} == {"model_failed"}

    seen = {}

    class Crash:
        def run(self, job, attempt, extra):
            seen["path"] = extra["receipt_path"]
            write_receipt(extra)
            raise RuntimeError("boom")

    s = FakeStore()
    s.add("j1")
    run_all(make(s, Crash()))
    assert s.job("j1")["reason"] == "model_failed"
    assert s.job("j1")["attempt_receipts"][0]["receipt"] == RECEIPT
    assert not os.path.exists(os.path.dirname(seen["path"]))


def test_vanished_with_receipt_file_requeues_with_receipt():
    s = FakeStore()
    s.add("j1")

    def child(extra):
        write_receipt(extra)
        return ChildOutcome.vanished("child_signalled")

    # One launch only: a fast child could otherwise be requeued and re-claimed in the same pass.
    probes = iter([4096])
    run_all(make(s, ScriptModel(child), memory_probe=lambda: next(probes, 0)))
    j = s.job("j1")
    assert j["state"] == "queued"
    assert j["attempt_receipts"] == [
        {"attempt": 1, "outcome": "vanished", "reason": "model_child_vanished", "receipt": RECEIPT}
    ]


def test_vanished_without_receipt_passes_none():
    s = FakeStore()
    s.add("j1")
    run_all(make(s, ScriptModel(ChildOutcome.vanished())))
    assert s.job("j1")["attempt_receipts"][0]["receipt"] is None


def test_receipt_dir_private_per_attempt_and_removed():
    s = FakeStore()
    s.add("j1")
    seen = []

    def child(extra):
        directory = os.path.dirname(extra["receipt_path"])
        info = os.lstat(directory)
        seen.append((directory, stat.S_ISDIR(info.st_mode), stat.S_IMODE(info.st_mode)))
        assert not os.path.exists(extra["receipt_path"])
        return ChildOutcome.vanished()

    run_all(make(s, ScriptModel(child, child)))
    run_all(make(s, ScriptModel(child, child)))
    assert len(seen) == 2 and seen[0][0] != seen[1][0]
    for directory, is_dir, mode in seen:
        assert is_dir and mode == 0o700
        assert not os.path.exists(directory)


def test_read_receipt_file_bounds(tmp_path):
    path = tmp_path / "receipt.json"
    assert crr.read_receipt_file(str(path)) is None  # absent
    path.write_text(json.dumps(RECEIPT))
    assert crr.read_receipt_file(str(path)) == RECEIPT
    path.write_text("[1, 2]")
    assert crr.read_receipt_file(str(path)) is None
    path.write_text("not json")
    assert crr.read_receipt_file(str(path)) is None
    path.write_text(json.dumps({"pad": "x" * crr.MAX_RECEIPT_BYTES}))
    assert crr.read_receipt_file(str(path)) is None
    real = tmp_path / "real.json"
    real.write_text(json.dumps(RECEIPT))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert crr.read_receipt_file(str(link)) is None
    (tmp_path / "adir").mkdir()
    assert crr.read_receipt_file(str(tmp_path / "adir")) is None


def test_lease_renewed_while_running():
    s = FakeStore()
    s.add("j1")
    gate = threading.Event()

    class Slow:
        def run(self, job, attempt, extra):
            gate.wait(5)
            return ChildOutcome.completed(GOOD)

    r = make(s, Slow(), lease_seconds=0.3, renew_interval_seconds=0.03)
    r.run_once()
    deadline = time.time() + 3
    while len(s.renewals) < 3 and time.time() < deadline:
        time.sleep(0.01)
    gate.set()
    r.drain()
    assert len(s.renewals) >= 3
    n = len(s.renewals)
    time.sleep(0.1)
    assert len(s.renewals) == n  # renewal stops with the job


def test_expired_lease_reaped_before_claiming():
    s = FakeStore()
    s.add("j1")
    s.jobs[0].update(state="running", attempts=1, lease_owner="dead", lease_expires_at=5.0)
    r = make(s, ScriptModel(ChildOutcome.completed(GOOD)), clock=lambda: 100.0)
    run_all(r)
    j = s.job("j1")
    assert j["state"] == "completed" and j["attempts"] == 2


def test_constructor_validation():
    for n in (0, 9, -1):
        with pytest.raises(ValueError):
            make(FakeStore(), ScriptModel(), max_concurrent=n)
    with pytest.raises(ValueError):
        make(FakeStore(), None)
    make(FakeStore(), ScriptModel(), max_concurrent=1)
    make(FakeStore(), ScriptModel(), max_concurrent=8)


# ---- SubprocessReviewModel ----

JOB = {
    "id": "j1",
    "kind": "code_review",
    "inputs": [{"snapshot_id": "s1", "digest": D, "data_class": "public"}],
    "code_review": {"mode": "snapshot", "head_digest": D, "rubric_digest": D, "attempts": 1},
    "prompt_digest": D,
    "destination_id": "anthropic",
    "tier": "sonnet",
    "budget_cents": 100,
    "egress": "model_runtime",
}


def script(tmp_path, body):
    p = tmp_path / "child.py"
    p.write_text(body)
    return [sys.executable, str(p)]


ECHO = (
    "import json,sys\nspec=json.load(sys.stdin)\n"
    "print(json.dumps({'outcome':'completed','output':{'echo':spec}}))\n"
)


def test_subprocess_completed_and_spec_on_stdin(tmp_path):
    out = SubprocessReviewModel(script(tmp_path, ECHO), 2).run(JOB, 2)
    assert out.kind == "completed" and out.receipt is None
    echo = out.output["echo"]
    assert echo["attempt"] == 2 and echo["job_id"] == "j1" and echo["prompt_digest"] == D
    assert echo["inputs"] == [{"snapshot_id": "s1", "digest": D, "data_class": "public"}]
    assert echo["code_review"] == {
        "mode": "snapshot",
        "head_digest": D,
        "base_digest": None,
        "rubric_digest": D,
    }


def test_subprocess_spec_carries_extra(tmp_path):
    extra = {
        "inputs": [{"snapshot_id": "s1", "digest": D, "data_class": "public", "files": []}],
        "profile": {"model": {"id": "m"}},
        "receipt_path": str(tmp_path / "receipt.json"),
    }
    out = SubprocessReviewModel(script(tmp_path, ECHO), 2, "anthropic-api-key").run(JOB, 1, extra)
    echo = out.output["echo"]
    assert {k: echo[k] for k in extra} == extra
    assert echo["credential_name"] == "anthropic-api-key"


def test_subprocess_completed_receipt_from_output(tmp_path):
    body = (
        "import json\n"
        f"print(json.dumps({{'outcome':'completed','output':{{'receipt':{RECEIPT!r}}}}}))\n"
    )
    out = SubprocessReviewModel(script(tmp_path, body), 2).run(JOB, 1)
    assert out.kind == "completed" and out.receipt == RECEIPT


def test_subprocess_failed_outcome(tmp_path):
    argv = script(
        tmp_path, "import json\nprint(json.dumps({'outcome':'failed','reason':'nope'}))\n"
    )
    out = SubprocessReviewModel(argv, 2).run(JOB, 1)
    assert (out.kind, out.detail, out.receipt) == ("failed", "nope", None)


def test_subprocess_failed_outcome_with_receipt(tmp_path):
    body = (
        "import json\nprint(json.dumps({'outcome':'failed','reason':'budget_exceeded',"
        f"'receipt':{RECEIPT!r}}}))\n"
    )
    out = SubprocessReviewModel(script(tmp_path, body), 2).run(JOB, 1)
    assert (out.kind, out.detail, out.receipt) == ("failed", "budget_exceeded", RECEIPT)
    body = "import json\nprint(json.dumps({'outcome':'failed','reason':'x','receipt':[1]}))\n"
    assert SubprocessReviewModel(script(tmp_path, body), 2).run(JOB, 1).receipt is None


def test_subprocess_sigkill_is_vanished(tmp_path):
    argv = script(tmp_path, "import os,signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    assert SubprocessReviewModel(argv, 2).run(JOB, 1).kind == "vanished"


def test_real_child_writes_receipt_then_sigkills_is_vanished_with_receipt(tmp_path):
    body = (
        "import json,os,signal,sys\n"
        "spec=json.load(sys.stdin)\n"
        "fd=os.open(spec['receipt_path'], os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)\n"
        f"os.write(fd, json.dumps({RECEIPT!r}).encode())\n"
        "os.close(fd)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    s = FakeStore()
    s.add("j1")
    run_all(make(s, SubprocessReviewModel(script(tmp_path, body), 10)))
    j = s.job("j1")
    assert j["state"] == "queued"
    assert j["attempt_receipts"] == [
        {"attempt": 1, "outcome": "vanished", "reason": "model_child_vanished", "receipt": RECEIPT}
    ]


@pytest.mark.parametrize(
    "body",
    [
        "print('garbage')",
        "pass",
        "print('[1,2]')",
        'print(\'{"outcome":"completed"}\')',
        'print(\'{"outcome":"other"}\')',
    ],
)
def test_subprocess_unparseable_is_vanished(tmp_path, body):
    assert SubprocessReviewModel(script(tmp_path, body + "\n"), 2).run(JOB, 1).kind == "vanished"


def test_subprocess_timeout_is_vanished(tmp_path):
    argv = script(tmp_path, "import time\ntime.sleep(30)\n")
    start = time.time()
    assert SubprocessReviewModel(argv, 0.5).run(JOB, 1).kind == "vanished"
    assert time.time() - start < 5


def test_subprocess_missing_executable_is_vanished(tmp_path):
    assert SubprocessReviewModel([str(tmp_path / "absent")], 1).run(JOB, 1).kind == "vanished"


def test_child_environment_is_minimal(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    monkeypatch.setenv("CLAUDE_API_KEY", "x")
    monkeypatch.setenv("SOME_OTHER_SECRET", "x")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/creds")
    argv = script(
        tmp_path,
        (
            "import json,os\n"
            "print(json.dumps({'outcome':'completed','output':{'env':sorted(os.environ)}}))\n"
        ),
    )
    out = SubprocessReviewModel(argv, 2).run(JOB, 1)
    names = set(out.output["env"])
    assert not names & set(crr.FORBIDDEN_ENVIRONMENT) and "SOME_OTHER_SECRET" not in names
    assert "CREDENTIALS_DIRECTORY" in names and "PATH" not in names
    assert names <= set(crr.CHILD_ENVIRONMENT_ALLOWLIST) | {"LC_CTYPE", "PYTHONHASHSEED"}
    assert set(
        crr.child_environment({"ANTHROPIC_API_KEY": "x", "PATH": "/p", "LANG": "C", "FOO": "1"})
    ) == {"LANG"}
    # A home-directory login is never handed to the child; credentials are references only.
    assert crr.child_environment({"HOME": "/h", "TMPDIR": "/t"}) == {"TMPDIR": "/t"}


def test_builtin_child_argv_is_isolated_module():
    assert crr.BUILTIN_CHILD_ARGV == (
        sys.executable,
        "-I",
        "-m",
        "tessera_agent.code_review_child",
    )


def test_subprocess_constructor_validation():
    with pytest.raises(ValueError):
        SubprocessReviewModel([], 1)
    with pytest.raises(ValueError):
        SubprocessReviewModel(["x"], 0)


# ---- CLI ----


def _profile_dict():
    return {
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
    }


def _profile(tmp_path, body=None):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(_profile_dict()) if body is None else body)
    return str(path)


def _creds(tmp_path, mode=0o600, name="anthropic-api-key", extra=()):
    directory = tmp_path / "creds"
    directory.mkdir()
    path = directory / name
    path.write_bytes(b"placeholder")
    path.chmod(mode)
    for other in extra:
        (directory / other).write_bytes(b"other")
    return {"CREDENTIALS_DIRECTORY": str(directory)}


def _args(tmp_path, *more, profile=None, credential="anthropic-api-key"):
    args = [
        "--state-dir",
        str(tmp_path / "state"),
        "--model-profile",
        profile or _profile(tmp_path),
    ]
    if credential is not None:
        args += ["--credential-name", credential]
    return [*args, *more]


class _AdmitAll:
    def check(self, destination_id, tier, data_classes, budget_cents):
        return None


def test_cli_refuses_api_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    rc = crr.main(_args(tmp_path), policy=_AdmitAll())
    assert rc == 2
    err = capsys.readouterr().err
    assert "api_key_environment_present" in err and "not-a-real-key" not in err


def test_cli_rejects_model_command(tmp_path):
    for flags in (["--model-command", "x"], ["--model-arg", "x"]):
        with pytest.raises(SystemExit) as raised:
            crr.main(_args(tmp_path, *flags), environment={}, policy=_AdmitAll())
        assert raised.value.code == 2


def test_cli_requires_model_profile_and_credential_name(tmp_path):
    with pytest.raises(SystemExit) as raised:
        crr.main(
            ["--state-dir", str(tmp_path), "--credential-name", "anthropic-api-key"],
            environment={},
            policy=_AdmitAll(),
        )
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        crr.main(_args(tmp_path, credential=None), environment={}, policy=_AdmitAll())
    assert raised.value.code == 2


@pytest.mark.parametrize("n", ["0", "9", "-1"])
def test_cli_max_concurrent_range(n, tmp_path):
    with pytest.raises(SystemExit):
        crr.main(_args(tmp_path, "--max-concurrent", n), environment={})


@pytest.mark.parametrize("name", crr.FORBIDDEN_ENVIRONMENT)
def test_refuse_each_forbidden_name(name):
    with pytest.raises(crr.RunnerRefused):
        crr.refuse_api_key_environment({name: "x"})
    crr.refuse_api_key_environment({"PATH": "x"})


def test_cli_refuses_without_operator_policy(tmp_path, capsys):
    rc = crr.main(_args(tmp_path), environment=_creds(tmp_path))
    assert rc == 2 and "policy_required" in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[1, 2]",
        "{}",
        json.dumps({**_profile_dict(), "model": {"providerID": "other", "id": "m"}}),
        json.dumps({**_profile_dict(), "permissions": []}),
        json.dumps({**_profile_dict(), "pricing_cents_per_mtok": {"input": 1}}),
    ],
)
def test_cli_invalid_model_profile_refused(tmp_path, capsys, body):
    env = _creds(tmp_path)
    rc = crr.main(
        _args(tmp_path, profile=_profile(tmp_path, body)), environment=env, policy=_AdmitAll()
    )
    err = capsys.readouterr().err
    assert rc == 2 and "model_profile_invalid" in err and str(tmp_path) not in err


def test_cli_missing_model_profile_refused(tmp_path, capsys):
    rc = crr.main(
        _args(tmp_path, profile=str(tmp_path / "absent.json")),
        environment=_creds(tmp_path),
        policy=_AdmitAll(),
    )
    assert rc == 2 and "model_profile_invalid" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["anthropic-key", "other", "a/b"])
def test_cli_credential_name_must_be_anthropic_api_key(tmp_path, capsys, name):
    rc = crr.main(
        _args(tmp_path, credential=name),
        environment=_creds(tmp_path, name="anthropic-key"),
        policy=_AdmitAll(),
    )
    assert rc == 2 and "credential_name_invalid" in capsys.readouterr().err


def test_cli_credential_directory_not_exclusive(tmp_path, capsys):
    env = _creds(tmp_path, extra=("subscription-login",))
    rc = crr.main(_args(tmp_path), environment=env, policy=_AdmitAll())
    err = capsys.readouterr().err
    assert rc == 2 and "credential_directory_not_exclusive" in err
    assert "subscription-login" not in err and str(tmp_path) not in err


def test_exclusive_directory_unit(tmp_path):
    env = _creds(tmp_path)
    crr.validate_exclusive_credential_directory("anthropic-api-key", env)
    (tmp_path / "creds" / ".hidden").write_bytes(b"x")
    with pytest.raises(crr.RunnerRefused, match="^credential_directory_not_exclusive$"):
        crr.validate_exclusive_credential_directory("anthropic-api-key", env)
    with pytest.raises(crr.RunnerRefused, match="^credential_directory_missing$"):
        crr.validate_exclusive_credential_directory("anthropic-api-key", {})


def test_cli_credential_refusals_are_sanitized(tmp_path, capsys):
    env = _creds(tmp_path, 0o644)
    rc = crr.main(_args(tmp_path), environment=env, policy=_AdmitAll())
    err = capsys.readouterr().err
    assert rc == 2 and "credential_not_private" in err
    assert str(tmp_path) not in err and "placeholder" not in err
    rc = crr.main(_args(tmp_path), environment={}, policy=_AdmitAll())
    assert rc == 2 and "credential_directory_missing" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = crr.main(
        _args(tmp_path), environment={"CREDENTIALS_DIRECTORY": str(empty)}, policy=_AdmitAll()
    )
    assert rc == 2 and "credential_missing" in capsys.readouterr().err


def test_cli_starts_only_builtin_child(tmp_path, monkeypatch):
    built = {}

    class FakeRunner:
        def __init__(self, store, model, policy, **kwargs):
            built.update(model=model, policy=policy, **kwargs)

        def run_once(self):
            raise KeyboardInterrupt

        def drain(self):
            built["drained"] = True

    import tessera_control.review_jobs as review_jobs

    monkeypatch.setattr(review_jobs, "ReviewJobStore", lambda path: object())
    monkeypatch.setattr(crr, "CodeReviewRunner", FakeRunner)
    policy = _AdmitAll()
    rc = crr.main(_args(tmp_path), environment=_creds(tmp_path), policy=policy)
    assert rc == 0 and built["drained"]
    model = built["model"]
    assert model.argv == list(crr.BUILTIN_CHILD_ARGV)
    assert model.credential_name == "anthropic-api-key"
    assert built["credential_name"] == "anthropic-api-key"
    assert built["profile"] == _profile_dict() and built["policy"] is policy


@pytest.mark.parametrize(
    "spec,reason",
    [
        ("nocolon", "policy_spec_invalid"),
        ("no_such_module_xyz:P", "policy_not_found"),
        ("json:dumps", "policy_invalid"),
    ],
)
def test_cli_policy_spec_refusals(spec, reason):
    with pytest.raises(crr.RunnerRefused, match=reason):
        crr.load_policy(spec)


def test_load_policy_accepts_object_and_class(monkeypatch):
    import types

    module = types.ModuleType("crr_test_policy_module")
    module.Policy = _AdmitAll
    module.instance = _AdmitAll()
    monkeypatch.setitem(sys.modules, "crr_test_policy_module", module)
    assert crr.load_policy("crr_test_policy_module:Policy").check("d", "sonnet", [], 1) is None
    assert crr.load_policy("crr_test_policy_module:instance") is module.instance


# ---- credential reference ----


def _cred(tmp_path, mode=0o600, name="anthropic-key"):
    path = tmp_path / name
    path.write_bytes(b"placeholder")
    path.chmod(mode)
    return {"CREDENTIALS_DIRECTORY": str(tmp_path)}


def test_credential_none_is_noop():
    crr.validate_credential_reference(None, {})


def test_credential_private_file_passes(tmp_path):
    crr.validate_credential_reference("anthropic-key", _cred(tmp_path))


def test_credential_never_opened(tmp_path, monkeypatch):
    env = _cred(tmp_path, mode=0o200)  # write-only: any read would fail
    real_open = open

    def guarded(file, *args, **kwargs):
        if str(file).startswith(str(tmp_path)):
            raise AssertionError("credential was opened")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded)
    crr.validate_credential_reference("anthropic-key", env)
    crr.validate_exclusive_credential_directory("anthropic-key", env)


@pytest.mark.parametrize(
    "name", ["a/b", "../x", "..", ".hidden", "", "a b", "x" * 129, "/abs/path"]
)
def test_credential_name_invalid(tmp_path, name):
    with pytest.raises(crr.RunnerRefused, match="^credential_name_invalid$"):
        crr.validate_credential_reference(name, _cred(tmp_path))


def test_credential_directory_missing(tmp_path):
    for env in ({}, {"CREDENTIALS_DIRECTORY": ""}):
        with pytest.raises(crr.RunnerRefused, match="^credential_directory_missing$"):
            crr.validate_credential_reference("anthropic-key", env)


def test_credential_missing_and_not_regular(tmp_path):
    env = {"CREDENTIALS_DIRECTORY": str(tmp_path)}
    with pytest.raises(crr.RunnerRefused, match="^credential_missing$"):
        crr.validate_credential_reference("absent", env)
    (tmp_path / "adir").mkdir()
    with pytest.raises(crr.RunnerRefused, match="^credential_missing$"):
        crr.validate_credential_reference("adir", env)


def test_credential_symlink_refused(tmp_path):
    target = tmp_path / "real"
    target.write_bytes(b"placeholder")
    target.chmod(0o600)
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(crr.RunnerRefused, match="^credential_missing$"):
        crr.validate_credential_reference("link", {"CREDENTIALS_DIRECTORY": str(tmp_path)})


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644, 0o777])
def test_credential_not_private(tmp_path, mode):
    with pytest.raises(crr.RunnerRefused, match="^credential_not_private$"):
        crr.validate_credential_reference("anthropic-key", _cred(tmp_path, mode))


def test_job_spec_carries_credential_name(tmp_path):
    assert crr.job_spec(JOB, 1)["credential_name"] is None
    assert crr.job_spec(JOB, 1, "k")["credential_name"] == "k"
    out = SubprocessReviewModel(script(tmp_path, ECHO), 2, credential_name="anthropic-api-key").run(
        JOB, 1
    )
    assert out.output["echo"]["credential_name"] == "anthropic-api-key"


def test_child_gets_credentials_directory_but_no_api_key_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    for name in crr.FORBIDDEN_ENVIRONMENT:
        monkeypatch.setenv(name, "placeholder")
    argv = script(
        tmp_path,
        (
            "import json,os\n"
            "print(json.dumps({'outcome':'completed','output':{'env':sorted(os.environ)}}))\n"
        ),
    )
    out = SubprocessReviewModel(argv, 2, credential_name="anthropic-api-key").run(JOB, 1)
    names = set(out.output["env"])
    assert "CREDENTIALS_DIRECTORY" in names and "HOME" not in names
    assert not names & set(crr.FORBIDDEN_ENVIRONMENT)


def test_credential_name_constructor_validation():
    with pytest.raises(ValueError):
        SubprocessReviewModel(["x"], 1, credential_name="a/b")
    with pytest.raises(ValueError):
        CodeReviewRunner(
            FakeStore(), ScriptModel(), AllowPolicy(), runner_id="r", credential_name="../x"
        )


def test_renewal_survives_a_transient_store_error():
    s = FakeStore()
    s.add("j1")
    real, state = s.renew_code_review_lease, {"raised": False}

    def flaky(job_id, runner_id, lease_seconds, now):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("transient")
        return real(job_id, runner_id, lease_seconds, now)

    s.renew_code_review_lease = flaky

    def slow(extra):
        deadline = time.time() + 5
        while len(s.renewals) < 2 and time.time() < deadline:
            time.sleep(0.01)
        return ChildOutcome.completed(GOOD)

    r = make(s, ScriptModel(slow), lease_seconds=0.3, renew_interval_seconds=0.03)
    run_all(r)
    assert state["raised"] and len(s.renewals) >= 2


def test_renewal_stops_when_the_lease_is_lost():
    s = FakeStore()
    calls = []

    def lost(job_id, runner_id, lease_seconds, now):
        calls.append(job_id)
        return False

    s.renew_code_review_lease = lost
    r = make(s, ScriptModel(ChildOutcome.completed(GOOD)), renew_interval_seconds=0.01)
    stop = threading.Event()
    thread = threading.Thread(target=r._renew_loop, args=("j1", stop))
    thread.start()
    thread.join(timeout=5)
    stop.set()
    assert not thread.is_alive() and calls == ["j1"]


class _TimedModel(ScriptModel):
    def __init__(self, timeout_seconds):
        super().__init__()
        self.timeout_seconds = timeout_seconds


def test_runner_refuses_child_timeout_not_shorter_than_lease():
    for timeout in (60, 61, 45):
        with pytest.raises(ValueError):
            make(FakeStore(), _TimedModel(timeout), lease_seconds=60)
    make(FakeStore(), _TimedModel(20), lease_seconds=60)


def test_cli_refuses_child_timeout_not_shorter_than_lease(tmp_path):
    for flags in (
        ["--lease-seconds", "60"],  # default child timeout is longer
        ["--lease-seconds", "60", "--child-timeout-seconds", "60"],
    ):
        with pytest.raises(SystemExit) as raised:
            crr.main(_args(tmp_path, *flags), environment={}, policy=_AdmitAll())
        assert raised.value.code == 2


@pytest.mark.parametrize("failure", ["complete", "fail"])
def test_late_lease_conflict_passes_the_receipt_once_and_changes_nothing_else(failure):
    s = FakeStore()
    s.add("j1")
    passed = []
    if failure == "complete":
        outcome = ChildOutcome.completed(GOOD)

        def late(job_id, runner_id, output, now, attempt=None):
            passed.append(output["receipt"])
            raise Conflict("code review lease expired")

        s.complete_code_review = late
    else:
        outcome = ChildOutcome.failed("model_failed", receipt=RECEIPT)

        def late(job_id, runner_id, reason, now, receipt=None, attempt=None):
            passed.append(receipt)
            raise Conflict("code review lease expired")

        s.fail_code_review = late
    s.requeue_vanished_code_review = lambda *a, **k: passed.append("requeue")
    r = make(s, ScriptModel(outcome))
    run_all(r)
    assert passed == [RECEIPT]
    assert r.in_flight == 0


def test_attempt_is_passed_on_every_terminal_call():
    # complete
    s = FakeStore()
    s.add("j1")
    run_all(make(s, ScriptModel(ChildOutcome.completed(GOOD))))
    assert s.passed_attempts == [("complete", 1)]
    # 422 -> fail after the failed complete
    s = FakeStore()
    s.add("j1")
    s.complete_error = ReviewRefused("failed_output_invalid", 422)
    run_all(make(s, ScriptModel(ChildOutcome.completed(GOOD))))
    assert s.passed_attempts == [("complete", 1), ("fail", 1)]
    # child failure
    s = FakeStore()
    s.add("j1")
    run_all(make(s, ScriptModel(ChildOutcome.failed("model_failed"))))
    assert s.passed_attempts == [("fail", 1)]
    # adapter crash
    s = FakeStore()
    s.add("j1")

    def boom(extra):
        raise RuntimeError("adapter")

    run_all(make(s, ScriptModel(boom)))
    assert s.passed_attempts == [("fail", 1)]
    # vanish, then the retry carries attempt 2
    s = FakeStore()
    s.add("j1")
    run_all(make(s, ScriptModel(ChildOutcome.vanished(), ChildOutcome.completed(GOOD))))
    run_all(make(s, ScriptModel(ChildOutcome.completed(GOOD))))
    assert s.passed_attempts == [("requeue", 1), ("complete", 2)]


def test_attempt_is_passed_when_no_attempt_was_launched():
    s = FakeStore()
    s.add("j1")
    s.inputs_error = StoreError("custody")
    m = ScriptModel()
    run_all(make(s, m))
    assert m.attempts == [] and s.passed_attempts == [("fail", 1)]
    s = FakeStore()
    s.add("j1", budget=100)
    s.job("j1")["cost_cents_total"] = 100
    m = ScriptModel()
    run_all(make(s, m))
    assert m.attempts == [] and s.passed_attempts == [("fail", 1)]


def test_cli_gives_each_runner_process_a_fresh_id(tmp_path, monkeypatch):
    # Late reports are attributed by (runner id, attempt); ids must never be reused.
    ids = []

    class FakeRunner:
        def __init__(self, store, model, policy, **kwargs):
            ids.append(kwargs["runner_id"])

        def run_once(self):
            raise KeyboardInterrupt

        def drain(self):
            pass

    import re

    import tessera_control.review_jobs as review_jobs

    monkeypatch.setattr(review_jobs, "ReviewJobStore", lambda path: object())
    monkeypatch.setattr(crr, "CodeReviewRunner", FakeRunner)
    environment = _creds(tmp_path)
    for _ in range(2):
        rc = crr.main(_args(tmp_path), environment=environment, policy=_AdmitAll())
        assert rc == 0
    assert len(ids) == 2 and ids[0] != ids[1]
    assert all(re.fullmatch(r"runner-[0-9a-f]{32}", i) for i in ids)
