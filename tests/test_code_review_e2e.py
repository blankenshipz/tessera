"""Code review jobs end to end: real store and runner, a fake model child (ADR 0092).

Synthetic only: no model runs and no network. The fake model stands in for the built-in model
child; everything it returns goes through the real store validation. Jobs carry the child's
pinned prompt digest, ``model_runtime`` egress and the ``anthropic`` destination, and the runner
loads their verified custody content before launching.
"""

import base64
import json
import sqlite3
import threading

import pytest
from test_code_review_jobs import (  # noqa: F401
    CONTENT,
    _subject_snapshots,
    cr_job,
    normalized,
    output,
    receipt,
)
from test_review_jobs import AUTO, finding, world  # noqa: F401  (fixture)

from tessera_control.code_review_runner import ChildOutcome, CodeReviewRunner
from tessera_control.store import Conflict, canonical_digest


class Admit:
    def check(self, destination_id, tier, data_classes, budget_cents):
        return None


class Spy(Admit):
    def __init__(self):
        self.seen = []

    def check(self, destination_id, tier, data_classes, budget_cents):
        self.seen.append(list(data_classes))
        return None


class Refuse:
    def check(self, destination_id, tier, data_classes, budget_cents):
        return "destination_not_enrolled"


def e2e_job(mode="diff", **over):
    """A code review job the runner will launch: pinned prompt, model egress, anthropic."""
    from tessera_agent.code_review_child import CODE_REVIEW_PROMPT_SHA256

    return cr_job(mode, prompt_digest=CODE_REVIEW_PROMPT_SHA256, **over)


class Scripted:
    """Returns the scripted outcomes in order; blocks on ``gate`` when one is given.

    An outcome may be ``(outcome, receipt)``: the receipt is written to the spec's
    ``receipt_path`` first, as the child does right after a billed call.
    """

    def __init__(self, *outcomes, gate=None):
        self.outcomes = list(outcomes)
        self.gate = gate
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = []
        self.extras = []

    def run(self, job, attempt, extra=None):
        with self.lock:
            self.calls.append((job["id"], attempt))
            self.extras.append(extra)
            self.active += 1
            self.peak = max(self.peak, self.active)
            outcome = self.outcomes.pop(0) if self.outcomes else ChildOutcome.vanished("none")
        if isinstance(outcome, tuple):
            outcome, written = outcome
            with open(extra["receipt_path"], "w") as handle:
                json.dump(written, handle)
        try:
            if self.gate is not None:
                self.gate.wait(5)
            return outcome
        finally:
            with self.lock:
                self.active -= 1


def runner(w, model, policy=None, **over):
    return CodeReviewRunner(
        w.app.state.review_jobs,
        model,
        policy or Admit(),
        runner_id="r-test",
        memory_probe=lambda: 1 << 20,
        lease_seconds=60,
        **over,
    )


def settle(r, rounds=10):
    for _ in range(rounds):
        r.run_once()
        r.drain(5)


def get(w, jid):
    return w.client.get(f"/review-jobs/{jid}", headers=w.submitter).json()


def test_runner_completes_job_and_person_resolves_finding(world):  # noqa: F811
    jid = world.submit(e2e_job()).json()["id"]
    model = Scripted(ChildOutcome.completed(output()))
    spy = Spy()
    settle(runner(world, model, policy=spy))
    # The launch-time policy re-check sees the classes control derived at submit.
    assert spy.seen == [["private_derived"]]
    # The model got the verified custody bytes of every input.
    (extra,) = model.extras
    assert {
        item["digest"]: {f["path"]: base64.b64decode(f["content_b64"]) for f in item["files"]}
        for item in extra["inputs"]
    } == {item["digest"]: CONTENT[item["digest"]] for item in extra["inputs"]}
    assert len(extra["inputs"]) == 3
    done = get(world, jid)
    assert done["state"] == "completed", done
    assert done["code_review"]["verdict"] == "MERGE-WITH-FOLLOWUPS"
    assert done["code_review"]["receipt"] == receipt() and done["code_review"]["attempts"] == 1
    assert done["advisory"] is True and done["execution_authorized"] is False
    assert done["data_classes"] == ["private_derived"]  # derived from custody, not the client
    findings = world.client.get(
        f"/projects/{world.project}/review-findings?job_id={jid}", headers=world.submitter
    ).json()["findings"]
    assert [f["finding"]["id"] for f in findings] == ["1", "2"]
    f = findings[0]
    body = {
        "decision": "reject",
        "rationale": "not applicable",
        "expected_revision": f["revision"],
        "expected_digest": f["digest"],
    }
    path = f"/review-findings/{f['id']}/resolve"
    assert world.post(path, world.submitter, body).status_code == 403
    resolved = world.post(path, world.resolver, body)
    assert resolved.status_code == 201 and resolved.json()["state"] == "rejected"
    assert get(world, jid)["state"] == "completed"  # a decision changes nothing else


def test_vanished_child_retried_once_then_failed(world):  # noqa: F811
    once = world.submit(e2e_job()).json()["id"]
    model = Scripted(ChildOutcome.vanished("gone"), ChildOutcome.completed(output()))
    settle(runner(world, model))
    assert get(world, once)["state"] == "completed"
    assert [a for _, a in model.calls] == [1, 2]

    twice = world.submit(e2e_job()).json()["id"]
    model = Scripted(ChildOutcome.vanished("a"), ChildOutcome.vanished("b"))
    settle(runner(world, model))
    failed = get(world, twice)
    assert failed["state"] == "failed" and failed["reason"] == "model_child_vanished"
    assert len(model.calls) == 2  # exactly one automatic retry


def test_concurrency_never_exceeds_two(world):  # noqa: F811
    jobs = [world.submit(e2e_job()).json()["id"] for _ in range(5)]
    gate = threading.Event()
    model = Scripted(*[ChildOutcome.completed(output()) for _ in jobs], gate=gate)
    r = runner(world, model, max_concurrent=2)
    assert r.run_once() == 2
    # A second runner on the same store is held by the durable host-wide bound.
    assert runner(world, Scripted(), max_concurrent=2).run_once() == 0
    gate.set()
    settle(r)
    assert model.peak <= 2
    assert all(get(world, j)["state"] == "completed" for j in jobs)


def test_low_memory_admits_nothing_and_policy_refusal_is_not_started(world):  # noqa: F811
    jid = world.submit(e2e_job()).json()["id"]
    r = runner(world, Scripted(), min_free_memory_mib=10)
    r.memory_probe = lambda: 1
    assert r.run_once() == 0 and get(world, jid)["state"] == "queued"
    settle(runner(world, Scripted(), policy=Refuse()))
    refused = get(world, jid)
    assert refused["state"] == "not_started" and refused["reason"] == "destination_not_enrolled"


def test_invalid_and_over_budget_output_fail_truthfully(world):  # noqa: F811
    bad = world.submit(e2e_job()).json()["id"]
    over = world.submit(e2e_job()).json()["id"]
    model = Scripted(
        ChildOutcome.completed(
            output(findings=[finding("1", severity="blocking")], verdict="MERGE")
        ),
        ChildOutcome.completed(output(receipt=receipt(cost=899.5, cost_cents=900))),
    )
    settle(runner(world, model, max_concurrent=1))  # outcomes pair with jobs in order
    assert get(world, bad)["reason"] == "failed_output_invalid"
    assert get(world, over)["reason"] == "budget_exceeded"
    for jid in (bad, over):
        job = get(world, jid)
        assert job["state"] == "failed" and job["code_review"]["verdict"] is None
        assert job["code_review"]["receipt"] is None  # no completion receipt invented
    # The paid calls' receipts are still recorded on the failed attempts.
    spent = get(world, over)["code_review"]
    (entry,) = spent["attempt_receipts"]
    assert entry["outcome"] == "failed" and entry["reason"] == "budget_exceeded"
    assert entry["receipt"]["cost_cents"] == 900 and entry["bound"] is True
    assert spent["cost_cents_total"] == 900
    assert get(world, over)["receipt_digest"] == entry["receipt_digest"]
    (entry,) = get(world, bad)["code_review"]["attempt_receipts"]
    assert entry["receipt"] == normalized(receipt()) and entry["reason"] == "failed_output_invalid"


def test_anthropic_direct_api_receipt_completes_via_runner(world):  # noqa: F811
    jid = world.submit(e2e_job()).json()["id"]
    sent = receipt()
    assert sent["cost_basis"] == "computed_from_pinned_prices"
    assert sent["model_identity_basis"] == "provider_response_model"
    settle(runner(world, Scripted(ChildOutcome.completed(output(receipt=sent)))))
    done = get(world, jid)
    assert done["state"] == "completed", done
    stored = done["code_review"]["receipt"]
    assert stored == sent and stored["input_class"] == "private_derived"
    assert done["receipt_digest"] == canonical_digest(sent)


def test_pinned_price_over_budget_and_understated_cents_fail_via_runner(world):  # noqa: F811
    over = world.submit(e2e_job()).json()["id"]
    under = world.submit(e2e_job()).json()["id"]
    model = Scripted(
        ChildOutcome.completed(output(receipt=receipt(cost=100.2, cost_cents=101))),
        ChildOutcome.completed(output(receipt=receipt(cost=3.01, cost_cents=3))),
    )
    settle(runner(world, model, max_concurrent=1))  # outcomes pair with jobs in order
    assert get(world, over)["reason"] == "budget_exceeded"
    assert get(world, under)["reason"] == "failed_output_invalid"
    for jid in (over, under):
        job = get(world, jid)
        assert job["state"] == "failed" and job["code_review"]["receipt"] is None
        assert len(job["code_review"]["attempt_receipts"]) == 1
    assert get(world, over)["code_review"]["cost_cents_total"] == 101


def test_human_mark_running_cannot_bypass_the_runner_lease(world):  # noqa: F811
    jid = world.submit(e2e_job()).json()["id"]
    store = world.app.state.review_jobs
    principal = store.authenticate(world.resolver["Authorization"].split()[1])
    with pytest.raises(Conflict):
        store.mark_running(principal, jid, "k-run")
    assert get(world, jid)["state"] == "queued"


def test_vanished_after_billed_call_keeps_receipt_then_completes(world):  # noqa: F811
    jid = world.submit(e2e_job()).json()["id"]
    first = receipt(cost=4.2, cost_cents=5)
    model = Scripted(
        (ChildOutcome.vanished("killed"), first),
        ChildOutcome.completed(output(receipt=receipt(attempts=2))),
    )
    settle(runner(world, model))
    done = get(world, jid)
    assert done["state"] == "completed", done
    entries = done["code_review"]["attempt_receipts"]
    assert [(e["attempt"], e["outcome"]) for e in entries] == [(1, "vanished"), (2, "completed")]
    assert entries[0]["receipt"] == normalized(first)
    assert done["code_review"]["cost_cents_total"] == 5 + 1


def test_tampered_custody_fails_input_unavailable(world):  # noqa: F811
    jid = world.submit(e2e_job()).json()["id"]
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE snapshot_files SET content=? WHERE snapshot_id='snap-head'", (b"x",))
    model = Scripted(ChildOutcome.completed(output()))
    settle(runner(world, model))
    failed = get(world, jid)
    assert failed["state"] == "failed" and failed["reason"] == "input_unavailable"
    assert model.calls == []  # the model never saw unverified content
