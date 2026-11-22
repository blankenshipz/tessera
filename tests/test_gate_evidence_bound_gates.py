"""Version-2 bound gates: platform replicate aggregates, never caller floats.

Synthetic fixtures test bookkeeping, not scientific performance.
"""

import sqlite3
from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_program_evaluations import prepared as prepared_v1
from test_program_evaluations import protocol as protocol_v1
from test_program_interactions import ProgramJourney

from tessera_control.evaluation_models import (
    BoundObservation,
    EvaluationProtocol,
    EvaluationRecord,
    evaluate_bound_record,
)
from tessera_control.store import canonical_digest

LOCK, REF, DIGEST = "lock-1", "ref-lock-1", "1" * 64
GROUP = "group-1"


# Synthetic lock partitions; reference locks need their partitions covered by audits.
PARTITIONS = '[{"name": "test", "member_count": 10, "group_count": 0, "digest": "%s"}]' % ("4" * 64)


def gate(**changes):
    return {
        "id": "g1",
        "category": "calibration",
        "variant_id": "study-variant",
        "experiment_variant_id": "exp-variant",
        "metric": "ece",
        "unit": "fraction",
        "split_lock_id": LOCK,
        "split_partition": "test",
        "split_digest": DIGEST,
        "training_examples": None,
        "procedure": "Synthetic fixture; no scientific measurement",
        "comparison": "ge",
        "threshold": 0.5,
        "statistic": "ci_low",
        "minimum_replicates": 3,
        "minimum_samples": 10,
        "allowed_sources": ["derived"],
        "required_reference_lock_ids": [REF],
        **changes,
    }


def protocol(**gate_changes):
    return {
        "version": 2,
        "identity": {
            "data_digest": "a" * 64,
            "code_digest": "b" * 64,
            "environment_digest": "c" * 64,
        },
        "gates": [gate(**gate_changes)],
    }


def record(p, plan_id="plan", plan_digest="e" * 64, group=GROUP):
    return {
        "version": 2,
        "plan_decision_id": plan_id,
        "plan_scope_digest": plan_digest,
        "protocol_digest": canonical_digest(
            EvaluationProtocol.model_validate(p).model_dump(mode="json")
        ),
        "identity": deepcopy(p["identity"]),
        "evidence_kind": "platform_replicate_aggregate",
        "observations": [{"gate_id": "g1", "replicate_group_id": group}],
    }


def facts(**changes):
    return {
        "id": GROUP,
        "experiment_id": "exp",
        "variant_id": "exp-variant",
        "metric": "ece",
        "unit": "fraction",
        "lock_id": LOCK,
        "partition": "test",
        "membership_digest": DIGEST,
        "ci_method": "t_interval_95",
        "n": 3,
        "mean": 0.9,
        "std": 0.1,
        "ci_low": 0.6,
        "ci_high": 1.0,
        "min_sample_count": 10,
        "sources": ["derived"],
        "excluded_attempts": [],
        "result_digest": "9" * 64,
        "audit": {"within_lock": "clean", "cross_set": {REF: "clean"}},
        "variant_linked": True,
        "current": True,
        **changes,
    }


def run(p, r, groups):
    return evaluate_bound_record(
        EvaluationProtocol.model_validate(p), EvaluationRecord.model_validate(r), groups
    )


def only(report):
    assert len(report["gates"]) == 1
    return report["gates"][0]


def test_pass_report_shape_and_determinism():
    p = protocol()
    report = run(p, record(p), {GROUP: facts()})
    assert report["status"] == "pass" and report["version"] == 2
    assert report["execution_granted"] is False
    assert report["scientific_validity"] == "not_assessed"
    assert report["evidence_binding"] == "platform_replicate_aggregate"
    g = only(report)
    assert g["evidence_binding"] == "platform_replicate_aggregate"
    assert g["group"]["result_digest"] == "9" * 64 and g["group"]["ci_method"] == "t_interval_95"
    assert run(p, record(p), {GROUP: facts()}) == report


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({"variant_id": "other"}, "variant_mismatch"),
        ({"metric": "other"}, "metric_unit_mismatch"),
        ({"unit": "other"}, "metric_unit_mismatch"),
        ({"lock_id": "other"}, "split_mismatch"),
        ({"membership_digest": "2" * 64}, "split_mismatch"),
        ({"partition": "other"}, "split_mismatch"),
        (
            {"audit": {"within_lock": "overlap_detected", "cross_set": {REF: "clean"}}},
            "split_leakage_not_clean",
        ),
        (
            {"audit": {"within_lock": "missing", "cross_set": {REF: "clean"}}},
            "split_leakage_not_clean",
        ),
        (
            {"audit": {"within_lock": "clean", "cross_set": {REF: "overlap_detected"}}},
            "cross_set_leakage_not_clean",
        ),
        ({"audit": {"within_lock": "clean", "cross_set": {}}}, "cross_set_leakage_not_clean"),
        ({"sources": ["derived", "human_entered"]}, "disallowed_measurement_source"),
        ({"n": 2}, "insufficient_replicates"),
        ({"min_sample_count": 9}, "insufficient_samples"),
        ({"variant_linked": False}, "variant_not_linked"),
        ({"ci_method": "percentile_bootstrap_95"}, "ci_method_mismatch"),
        ({"current": False}, "replicate_group_stale"),
        ({"current": None}, "replicate_group_stale"),
    ],
)
def test_binding_failures_are_inconclusive_never_pass(mutation, reason):
    p = protocol()
    g = only(run(p, record(p), {GROUP: facts(**mutation)}))
    assert g["status"] == "inconclusive" and g["reason"] == reason


def test_missing_observation_reason_and_unknown_group():
    p = protocol()
    r = record(p)
    assert only(run(p, r, {}))["reason"] == "replicate_group_not_found"
    r["observations"] = []
    assert only(run(p, r, {GROUP: facts()}))["reason"] == "metric_not_recorded"
    r["observations"] = [{"gate_id": "g1", "missing_reason": "run lost"}]
    g = only(run(p, r, {GROUP: facts()}))
    assert g["status"] == "inconclusive" and g["reason"] == "metric_missing: run lost"


def test_statistic_selection_and_fail():
    row = facts(mean=0.9, ci_low=0.4)
    p = protocol(statistic="mean")
    assert run(p, record(p), {GROUP: row})["status"] == "pass"
    p = protocol(statistic="ci_low")
    report = run(p, record(p), {GROUP: row})
    assert report["status"] == "fail" and only(report)["reason"] == "threshold_not_met"
    p = protocol(statistic="ci_high", comparison="le", threshold=0.9)
    assert run(p, record(p), {GROUP: facts(ci_high=1.0)})["status"] == "fail"


def test_fail_dominates_inconclusive_and_human_entered_can_be_allowed():
    p = protocol(allowed_sources=["derived", "human_entered"])
    assert run(p, record(p), {GROUP: facts(sources=["human_entered"])})["status"] == "pass"


def test_caller_float_and_mixed_records_rejected():
    p = protocol()
    for extra in ({"value": 0.9}, {"sample_count": 5}, {"provenance": "x"}):
        r = record(p)
        r["observations"][0].update(extra)
        with pytest.raises(ValidationError):
            EvaluationRecord.model_validate(r)
    r = record(p)
    r["observations"] = [{"gate_id": "g1", "value": 0.9}]
    with pytest.raises(ValidationError):
        EvaluationRecord.model_validate(r)
    r = record(p)
    r["evidence_kind"] = "caller_reported"
    with pytest.raises(ValidationError):
        EvaluationRecord.model_validate(r)
    with pytest.raises(ValidationError):
        BoundObservation(gate_id="g1")
    with pytest.raises(ValidationError):
        BoundObservation(gate_id="g1", replicate_group_id="a", missing_reason="b")


def test_protocol_validation_rules():
    for changes in (
        {"minimum_replicates": 1},
        {"minimum_samples": 0},
        {"allowed_sources": []},
        {"allowed_sources": ["derived", "derived"]},
        {"required_reference_lock_ids": ["a", "a"]},
        {"statistic": "median"},
        {"category": "few_shot_curve"},
    ):
        with pytest.raises(ValidationError):
            EvaluationProtocol.model_validate(protocol(**changes))
    mixed = protocol()
    mixed["version"] = 1
    with pytest.raises(ValidationError):
        EvaluationProtocol.model_validate(mixed)
    v1 = protocol_v1()
    v1["version"] = 2
    with pytest.raises(ValidationError):
        EvaluationProtocol.model_validate(v1)


def test_v1_protocol_digest_and_dump_unchanged():
    p = protocol_v1()
    assert EvaluationProtocol.model_validate(p).model_dump(mode="json") == p
    assert canonical_digest(EvaluationProtocol.model_validate(p).model_dump(mode="json")) == (
        canonical_digest(p)
    )


# ---- Program journey integration -------------------------------------------------


def linked_journey(tmp_path, monkeypatch, capsys):
    from test_local_api_workflow import Journey, complete

    execution = Journey(tmp_path, monkeypatch, capsys)
    for kind in ("baseline", "intervention"):
        _, _, attempt_id, _, fake = execution.admit(kind)
        execution.run(fake)
        complete(fake, "succeeded")
        execution.run(fake)
        execution.resolve(attempt_id, "succeeded")
    return ProgramJourney(tmp_path, execution=execution)


def sql(j, statements):
    connection = sqlite3.connect(j.state / "tessera.sqlite3")
    connection.execute("PRAGMA foreign_keys=OFF")
    with connection:
        for statement, parameters in statements:
            connection.execute(statement, parameters)
    connection.close()


def seed(j, *, within="clean", cross="clean", n=3):
    execution = j.context()["basis"]["executions"][0]
    experiment_id = execution["experiment"]["id"]
    exp_variant = execution["variants"][0]["id"]
    project = j.project["id"]
    principal = j.actor.principal_id if hasattr(j.actor, "principal_id") else "p"
    now = "2026-01-01T00:00:00Z"

    def lock(ident):
        return (
            "INSERT INTO gate_split_locks (id,project_id,name,membership_digest,partitions_json,"
            "member_count,created_at,created_by_principal_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                ident,
                project,
                ident,
                DIGEST if ident == LOCK else "2" * 64,
                PARTITIONS,
                1,
                now,
                principal,
            ),
        )

    def audit(ident, kind, reference, status):
        overlap = 0 if status == "clean" else 1
        return (
            "INSERT INTO gate_leakage_audits (id,project_id,lock_id,kind,reference_lock_id,"
            "eval_partitions_json,reference_partitions_json,key_overlap_count,"
            "group_overlap_count,overlap_sample_json,status,audit_digest,created_at,"
            "created_by_principal_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ident,
                project,
                LOCK,
                kind,
                reference,
                '["test"]',  # the clean cross-set audit must cover the gate partition
                '["test"]',  # ...and every partition of the reference lock
                overlap,
                0,
                "[]",
                status,
                "3" * 64,
                now,
                principal,
            ),
        )

    sql(
        j,
        [
            lock(LOCK),
            lock(REF),
            audit("a1", "within_lock", None, within),
            audit("a2", "cross_set", REF, cross),
            (
                "INSERT INTO gate_replicate_groups (id,project_id,experiment_id,variant_id,metric,"
                "unit,lock_id,partition,membership_digest,ci_method,statistics_version,n,mean,std,"
                "ci_low,ci_high,min_sample_count,sources_json,excluded_attempts_json,"
                "result_digest,created_at,created_by_principal_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    GROUP,
                    project,
                    experiment_id,
                    exp_variant,
                    "ece",
                    "fraction",
                    LOCK,
                    "test",
                    DIGEST,
                    "t_interval_95",
                    1,
                    n,
                    0.9,
                    0.1,
                    0.6,
                    1.0,
                    10,
                    '["derived"]',
                    "[]",
                    "9" * 64,
                    now,
                    principal,
                ),
            ),
        ],
    )
    return exp_variant


def bound_plan(j, exp_variant):
    p = protocol(variant_id=j.variant["id"], experiment_variant_id=exp_variant)
    plan = j.propose(j.scope(evaluation_protocol=p))
    j.resolve(plan)
    row = next(r for r in j.context()["reviews"] if r["id"] == plan["id"])
    j.resolve(j.propose(j.scope("source_use")))
    return p, record(p, plan["id"], row["scope_digest"])


def go_for(j, finding, evaluation):
    return j.propose(
        j.scope(
            "go_no_go",
            recommendation="go",
            finding_ids=[finding["id"]],
            evaluation_id=evaluation["id"],
        )
    )


def test_bound_pass_allows_human_go_and_agent_cannot_approve(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    finding = j.finding()
    exp_variant = seed(j)
    _, r = bound_plan(j, exp_variant)
    evaluation = j.propose(j.scope("evaluation", evaluation=r))
    j.resolve(evaluation, token=j.agent, status=403)
    j.resolve(evaluation)
    go = go_for(j, finding, evaluation)
    j.resolve(go, token=j.agent, status=403)
    j.resolve(go)
    context = j.context()
    rows = {v["id"]: v for v in context["reviews"]}
    assert rows[evaluation["id"]]["evaluation_report"]["status"] == "pass"
    assert rows[evaluation["id"]]["gate_evidence_binding"] == "platform_replicate_aggregate"
    assert rows[go["id"]]["gate_evidence_binding"] == "platform_replicate_aggregate"
    assert context["execution_granted"] is False


def test_bound_plan_rejects_unknown_lock_and_digest_mismatch(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    exp_variant = seed(j)
    j.propose(
        j.scope(evaluation_protocol=protocol(variant_id=j.variant["id"], split_lock_id="nope")),
        status=404,
    )
    j.propose(
        j.scope(evaluation_protocol=protocol(variant_id=j.variant["id"], split_digest="5" * 64)),
        status=409,
    )
    j.propose(
        j.scope(
            evaluation_protocol=protocol(
                variant_id=j.variant["id"], required_reference_lock_ids=["nope"]
            )
        ),
        status=404,
    )
    assert exp_variant


def test_overlap_audit_makes_evaluation_inconclusive_and_blocks_go(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    finding = j.finding()
    exp_variant = seed(j, within="overlap_detected")
    _, r = bound_plan(j, exp_variant)
    evaluation = j.propose(j.scope("evaluation", evaluation=r))
    j.resolve(evaluation)  # a human may acknowledge an inconclusive report
    go = go_for(j, finding, evaluation)
    assert "passing declared gates" in j.resolve(go, status=409)["detail"]
    report = next(v for v in j.context()["reviews"] if v["id"] == evaluation["id"])[
        "evaluation_report"
    ]
    assert report["status"] == "inconclusive"
    assert report["gates"][0]["reason"] == "split_leakage_not_clean"


def test_unlinked_experiment_variant_is_inconclusive(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    seed(j)
    _, r = bound_plan(j, "not-a-linked-variant")
    evaluation = j.propose(j.scope("evaluation", evaluation=r))
    report = next(v for v in j.context()["reviews"] if v["id"] == evaluation["id"])[
        "evaluation_report"
    ]
    assert report["gates"][0]["reason"] in {"variant_not_linked", "variant_mismatch"}
    assert report["status"] == "inconclusive"


def test_v1_style_record_against_v2_plan_is_rejected(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    exp_variant = seed(j)
    p, r = bound_plan(j, exp_variant)
    v1 = {
        "version": 1,
        "plan_decision_id": r["plan_decision_id"],
        "plan_scope_digest": r["plan_scope_digest"],
        "protocol_digest": r["protocol_digest"],
        "identity": r["identity"],
        "evidence_kind": "caller_reported",
        "observations": [
            {
                "gate_id": "g1",
                "value": 0.99,
                "sample_count": 99,
                "evidence_digest": "f" * 64,
                "provenance": "caller says so",
            }
        ],
    }
    detail = j.propose(j.scope("evaluation", evaluation=v1), status=409)["detail"]
    assert "does not match" in detail
    assert not any(v["scope"]["action"] == "evaluation" for v in j.context()["reviews"])


def test_undeclared_gate_is_a_conflict_not_a_server_error(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    exp_variant = seed(j)
    _p, r = bound_plan(j, exp_variant)
    r["observations"].append({"gate_id": "undeclared", "replicate_group_id": GROUP})
    detail = j.propose(j.scope("evaluation", evaluation=r), status=409)["detail"]
    assert "undeclared" in detail


def test_legacy_bindings_are_labelled(tmp_path):
    j = ProgramJourney(tmp_path)
    finding, _, r = prepared_v1(j)
    evaluation = j.propose(j.scope("evaluation", evaluation=r))
    j.resolve(evaluation)
    go = go_for(j, finding, evaluation)
    j.resolve(go)
    rows = {v["id"]: v for v in j.context()["reviews"]}
    assert rows[evaluation["id"]]["gate_evidence_binding"] == "unbound_caller_reported_legacy"
    assert rows[go["id"]]["gate_evidence_binding"] == "unbound_caller_reported_legacy"
    assert rows[evaluation["id"]]["evaluation_report"]["evidence_binding"] == (
        "unbound_caller_reported_legacy"
    )
