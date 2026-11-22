"""Reserved worst-case receipts on code review jobs (ADR 0092).

A billed call whose usage is unknown (timeout or 5xx after the request was sent) is receipted at
the client's reserved worst case under cost basis ``reserved_worst_case``. That receipt is a
reservation, not a measured cost: it can never complete a job, but it is recorded on failed,
vanished and late entries (``bound: false``) and counted in ``cost_cents_total``.

Synthetic only: no model runs; the runner protocol is driven directly with injected clocks.
"""

import pytest
from pydantic import ValidationError
from test_code_review_jobs import (  # noqa: F401
    _subject_snapshots,
    attempts_of,
    count,
    cr_job,
    events,
    ingest,
    normalized,
    output,
    receipt,
    world,
)

from tessera_control.code_review_jobs import receipt_bound
from tessera_control.review_jobs import ReviewRefused
from tessera_control.review_jobs_models import ModelCallReceipt
from tessera_control.store import Conflict, canonical_digest


def reserved(**over):
    """The default bound receipt, but costed at the reserved worst case: no response model."""
    return receipt(
        **{
            "model_reported": None,
            "model_identity_basis": "harness_reported_unverified",
            "cost": 1.53,
            "cost_basis": "reserved_worst_case",
            "cost_cents": 2,
            "cost_cents_estimate_max": 1.53,
            "output_sha256": None,
            **over,
        }
    )


def test_reserved_receipt_validates_and_requires_ceil_cents():
    assert ModelCallReceipt.model_validate(reserved()).cost_basis == "reserved_worst_case"
    # Even with a provider-named model the basis alone keeps it a reservation.
    ModelCallReceipt.model_validate(reserved(model_reported="claude-sonnet-4-5"))
    for cents in (None, 1, 3):
        with pytest.raises(ValidationError):
            ModelCallReceipt.model_validate(reserved(cost_cents=cents))


def test_reserved_receipt_is_never_bound():
    row = {
        "inputs_json": '[{"snapshot_id": "s", "digest": "' + "a" * 64 + '"}]',
        "prompt_digest": "b" * 64,
        "destination_id": "openai",
    }
    base = {
        "input_digests": {"s": "a" * 64},
        "prompt_sha256": "b" * 64,
        "input_class": "private_derived",
        "model_configured": "openai/gpt-codex",
        "model_identity_basis": "harness_reported_unverified",
    }
    # Every other binding rule holds (a non-anthropic destination skips the price rule).
    assert receipt_bound(row, {**base, "cost_basis": "reported_cost"}) is True
    assert receipt_bound(row, {**base, "cost_basis": "reserved_worst_case"}) is False


def test_reserved_receipt_refused_on_http_ingest(world):  # noqa: F811
    j = world.submit(cr_job()).json()
    r = ingest(world, j["id"], output(receipt=reserved()))
    assert r.status_code == 422 and r.json()["reason"] == "failed_output_invalid", r.text
    assert count(world, "review_findings") == 0
    cr = attempts_of(world, j["id"])
    assert cr["verdict"] is None and cr["attempt_receipts"] == []


def test_reserved_receipt_refused_on_runner_completion_then_recorded_on_fail(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    with pytest.raises(ReviewRefused) as refused:
        store.complete_code_review(jid, "r1", output(receipt=reserved()), 1001.0)
    assert refused.value.status == 422 and refused.value.reason == "failed_output_invalid"
    assert count(world, "review_findings") == 0
    assert attempts_of(world, jid)["attempt_receipts"] == []
    failed = store.fail_code_review(jid, "r1", "model_failed", 1002.0, receipt=reserved())
    digest = canonical_digest(normalized(reserved()))
    assert failed["state"] == "failed" and failed["receipt_digest"] == digest
    cr = attempts_of(world, jid)
    assert cr["receipt"] is None and cr["verdict"] is None
    assert cr["attempt_receipts"] == [
        {
            "attempt": 1,
            "outcome": "failed",
            "reason": "model_failed",
            "receipt": normalized(reserved()),
            "receipt_digest": digest,
            "bound": False,
            "receipt_invalid": False,
        }
    ]
    assert cr["cost_cents_total"] == 2
    ((event, _),) = events(world, "review_job.failed")
    assert event["reason"] == "model_failed" and event["receipt_digest"] == digest


def test_reserved_vanish_then_pinned_price_completion_counts_both(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    assert store.requeue_vanished_code_review(jid, "r1", 1001.0, receipt=reserved()) == "queued"
    store.claim_code_review("r1", 2, 60, 1002.0)
    # A pinned-price receipt still completes the job.
    _, done = store.complete_code_review(jid, "r1", output(receipt=receipt(attempts=2)), 1003.0)
    cr = done["job"]["code_review"]
    assert done["job"]["state"] == "completed"
    assert [(e["attempt"], e["outcome"], e["bound"]) for e in cr["attempt_receipts"]] == [
        (1, "vanished", False),
        (2, "completed", True),
    ]
    assert cr["attempt_receipts"][0]["receipt"] == normalized(reserved())
    assert cr["receipt"] == normalized(receipt(attempts=2))
    assert cr["cost_cents_total"] == 2 + 1


def test_reserved_late_receipt_recorded_unbound(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)  # lease to 1060
    with pytest.raises(Conflict):
        store.fail_code_review(jid, "r1", "model_failed", 1061.0, receipt=reserved())
    cr = attempts_of(world, jid)
    (entry,) = cr["attempt_receipts"]
    assert entry["outcome"] == "late" and entry["bound"] is False
    assert entry["receipt"] == normalized(reserved())
    assert cr["cost_cents_total"] == 2
    ((event, _),) = events(world, "review_job.late_receipt")
    assert event["receipt_digest"] == canonical_digest(normalized(reserved()))


def test_late_reserved_completion_is_conflict_and_stores_no_verdict(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    with pytest.raises(Conflict):
        store.complete_code_review(jid, "r1", output(receipt=reserved()), 1061.0)
    cr = attempts_of(world, jid)
    assert cr["verdict"] is None and cr["receipt"] is None
    assert [(e["outcome"], e["bound"]) for e in cr["attempt_receipts"]] == [("late", False)]
    assert count(world, "review_findings") == 0
