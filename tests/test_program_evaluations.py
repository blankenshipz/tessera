"""Synthetic caller-reported numbers test bookkeeping, not MixFM performance."""

from copy import deepcopy
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_program_interactions import ProgramJourney

from tessera_control.api import create_app
from tessera_control.evaluation_models import (
    EvaluationProtocol,
    EvaluationRecord,
    MetricCategory,
    evaluate_record,
)
from tessera_control.store import canonical_digest


def protocol(variant_id="fixture-variant"):
    return {
        "version": 1,
        "identity": {
            "data_digest": "a" * 64,
            "code_digest": "b" * 64,
            "environment_digest": "c" * 64,
        },
        "gates": [
            {
                "id": category,
                "category": category,
                "variant_id": variant_id,
                "metric": "synthetic_score",
                "unit": "fraction",
                "split_digest": "d" * 64,
                "training_examples": 8 if category == "few_shot_curve" else None,
                "procedure": "Synthetic fixture; no scientific measurement",
                "comparison": "ge",
                "threshold": 0.5,
                "minimum_samples": 2,
            }
            for category in get_args(MetricCategory)
        ],
    }


def record(p, plan_id="fixture-plan", plan_digest="e" * 64):
    return {
        "version": 1,
        "plan_decision_id": plan_id,
        "plan_scope_digest": plan_digest,
        "protocol_digest": canonical_digest(p),
        "identity": deepcopy(p["identity"]),
        "evidence_kind": "caller_reported",
        "observations": [
            {
                "gate_id": g["id"],
                "value": 0.5,
                "sample_count": 2,
                "evidence_digest": "f" * 64,
                "provenance": "Synthetic test input, not experimental evidence",
            }
            for g in p["gates"]
        ],
    }


def evaluate(p, r):
    return evaluate_record(EvaluationProtocol.model_validate(p), EvaluationRecord.model_validate(r))


def test_all_metric_categories_deterministic_bound_report_and_missing_reasons():
    p = protocol()
    r = record(p)
    report = evaluate(p, r)
    assert report["status"] == "pass"
    assert len(report["gates"]) == 11
    assert evaluate(p, r) == report
    assert report["identity"] == p["identity"]
    assert report["plan_scope_digest"] == r["plan_scope_digest"]
    assert report["execution_granted"] is False
    r["observations"].pop()
    r["observations"][0] = {"gate_id": "teacher_imitation", "missing_reason": "teacher unavailable"}
    r["observations"][1]["sample_count"] = 1
    missing = evaluate(p, r)
    assert missing["status"] == "inconclusive"
    assert {g["reason"] for g in missing["gates"]} >= {
        "metric_not_recorded",
        "metric_missing: teacher unavailable",
        "insufficient_samples",
    }
    r["observations"][2]["value"] = 0.4
    failed = evaluate(p, r)
    assert failed["status"] == "fail"  # missing results remain visible alongside failure
    assert failed["gates"][-1]["status"] == "inconclusive"
    assert failed["report_digest"] != report["report_digest"]


@pytest.mark.parametrize(
    "comparison,value,status",
    [
        ("ge", 0.5, "pass"),
        ("gt", 0.5, "fail"),
        ("le", 0.5, "pass"),
        ("lt", 0.5, "fail"),
        ("eq", 0.5, "pass"),
        ("eq", 0.6, "fail"),
    ],
)
def test_exact_threshold_comparisons(comparison, value, status):
    p = protocol()
    p["gates"] = [p["gates"][0]]
    p["gates"][0]["comparison"] = comparison
    r = record(p)
    r["observations"][0]["value"] = value
    assert evaluate(p, r)["status"] == status


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", float("nan")),
        ("value", float("inf")),
        ("value", True),
        ("value", "0.5"),
        ("sample_count", 0),
        ("sample_count", True),
        ("evidence_digest", None),
        ("provenance", " "),
        ("missing_reason", "not actually missing"),
    ],
)
def test_invalid_measurements(field, value):
    r = record(protocol())
    r["observations"][0][field] = value
    with pytest.raises(ValidationError):
        EvaluationRecord.model_validate(r)


def test_undeclared_duplicate_empty_and_mismatched_protocols():
    p = protocol()
    r = record(p)
    r["observations"][0]["gate_id"] = "undeclared"
    with pytest.raises(ValueError, match="undeclared"):
        evaluate(p, r)
    r = record(p)
    r["observations"].append(r["observations"][0])
    with pytest.raises(ValidationError, match="one observation"):
        evaluate(p, r)
    r = record(p)
    r["identity"]["data_digest"] = "0" * 64
    with pytest.raises(ValueError, match="identity"):
        evaluate(p, r)
    r = record(p)
    p["gates"][0]["threshold"] = 0.1
    with pytest.raises(ValueError, match="protocol digest"):
        evaluate(p, r)
    p["gates"] = []
    with pytest.raises(ValidationError):
        EvaluationProtocol.model_validate(p)
    p = protocol()
    p["gates"].append(p["gates"][0])
    with pytest.raises(ValidationError, match="unique"):
        EvaluationProtocol.model_validate(p)
    p = protocol()
    next(g for g in p["gates"] if g["category"] == "few_shot_curve")["training_examples"] = None
    with pytest.raises(ValidationError, match="training_examples"):
        EvaluationProtocol.model_validate(p)


def test_few_shot_curve_requires_every_declared_point_without_interpolation():
    p = protocol()
    point = next(g for g in p["gates"] if g["category"] == "few_shot_curve")
    p["gates"] = [point, {**point, "id": "few_shot_16", "training_examples": 16}]
    r = record(p)
    assert evaluate(p, r)["status"] == "pass"
    r["observations"].pop()
    report = evaluate(p, r)
    assert report["status"] == "inconclusive"
    assert report["gates"][1]["gate"]["training_examples"] == 16
    assert report["gates"][1]["reason"] == "metric_not_recorded"


def prepared(j):
    j.assess()
    finding = j.finding()
    p = protocol(j.variant["id"])
    plan = j.propose(j.scope(evaluation_protocol=p))
    j.resolve(plan)
    row = next(r for r in j.context()["reviews"] if r["id"] == plan["id"])
    j.resolve(j.propose(j.scope("source_use")))
    return finding, p, record(p, plan["id"], row["scope_digest"])


@pytest.mark.parametrize("status", ["pass", "fail", "inconclusive"])
def test_human_gate_review_idempotency_restart_and_go_fences(tmp_path, status):
    j = ProgramJourney(tmp_path)
    finding, p, r = prepared(j)
    if status == "fail":
        r["observations"][0]["value"] = 0.1
    elif status == "inconclusive":
        r["observations"].pop()
    scope = j.scope("evaluation", evaluation=r)
    expiry = j.expiry()
    j.expiry = lambda: expiry
    proposal = j.propose(scope, token=j.agent, key="evaluation")
    assert j.propose(scope, token=j.agent, key="evaluation") == proposal
    j.resolve(proposal, token=j.agent, status=403)
    direction = j.scope(
        "go_no_go", recommendation="go", finding_ids=[finding["id"]], evaluation_id=proposal["id"]
    )
    go = j.propose(direction)
    assert "human-reviewed evaluation" in j.resolve(go, status=409)["detail"]
    j.resolve(proposal)  # human may acknowledge a failed or incomplete report
    j.resolve(go, status=200 if status == "pass" else 409)
    context = j.context()
    row = next(v for v in context["reviews"] if v["id"] == proposal["id"])
    assert row["evaluation_report"]["status"] == status
    assert row["evaluation_report"] == evaluate(p, r)
    assert row["basis_snapshot"] == context["basis"]
    j.client = TestClient(create_app(j.state))
    assert j.context() == context
    if status == "pass":
        replacement = j.propose(j.scope("evaluation", evaluation=r))
        assert not next(v for v in j.context()["reviews"] if v["id"] == go["id"])[
            "is_effective_review"
        ]
        j.resolve(replacement)
    j.resolve(j.propose(j.scope("go_no_go")))  # no-go always permits early stop review
    assert j.context()["execution_granted"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_decision_id", "missing"),
        ("plan_scope_digest", "0" * 64),
        ("protocol_digest", "0" * 64),
    ],
)
def test_plan_binding_is_checked_before_recording(tmp_path, field, value):
    j = ProgramJourney(tmp_path)
    _, _, r = prepared(j)
    r[field] = value
    j.propose(j.scope("evaluation", evaluation=r), status=409)
    assert not any(v["scope"]["action"] == "evaluation" for v in j.context()["reviews"])


def test_stale_plan_cross_study_and_stale_evidence_cannot_approve(tmp_path):
    j = ProgramJourney(tmp_path)
    _, p, r = prepared(j)
    other = j.post(
        f"/projects/{j.project['id']}/studies",
        {"purpose": "Separate fixture study", "protocol": "No declared evaluation plan"},
    )
    context = j.client.get(
        f"/interaction/studies/{other['id']}/program-context",
        headers={"Authorization": j.owner},
    ).json()
    j.propose(
        j.scope(
            "evaluation", evaluation=r, study_id=other["id"], basis_digest=context["basis_digest"]
        ),
        status=409,
    )
    proposal = j.propose(j.scope("evaluation", evaluation=r))
    j.post(
        f"/sources/{j.source['id']}/assess",
        {
            "expected_revision": 2,
            "verdict": "supported",
            "reason": "New fixture assessment",
        },
    )
    assert "stale" in j.resolve(proposal, status=409)["detail"]
    proposal = j.propose(j.scope("evaluation", evaluation=r))
    replacement = j.propose(j.scope(evaluation_protocol=p))
    j.resolve(proposal, status=409)
    j.resolve(replacement)
    j.propose(j.scope("evaluation", evaluation=r), status=409)


def test_unknown_protocol_variant_and_injected_report_are_rejected(tmp_path):
    j = ProgramJourney(tmp_path)
    j.propose(j.scope(evaluation_protocol=protocol("another-study-variant")), status=404)
    _, _, r = prepared(j)
    r["report"] = {"status": "pass"}
    j.propose(j.scope("evaluation", evaluation=r), status=422)
