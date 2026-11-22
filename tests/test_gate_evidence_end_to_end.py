"""End to end over HTTP: a "go" needs stored per-seed measurements on a locked,
leakage-audited split, aggregated by the platform; a bare caller float cannot satisfy it.

Synthetic fixtures test bookkeeping, not scientific performance. Shortcut (raw SQL):
admitted attempts and their terminal accounting are inserted directly, as in
tests/gate_evidence_fixture.py (foreign keys off for the absent task/grant parents),
because real admission/dispatch is covered by other suites and the linked journey's own
attempts are already past 'admitted'. Locks, audits, bindings, measurements, metric
records, groups, plans, evaluations and decisions all go through the public API.
"""

import sqlite3
import statistics
from uuid import uuid4

import pytest
from test_gate_evidence_bound_gates import go_for, linked_journey, protocol, record

from tessera_control.evaluation_models import EvaluationProtocol
from tessera_control.store import utc_now

NUMBERS = [0.80, 0.85, 0.90]
HEX = "a" * 64
METRICS = [{"name": "ece", "partition": "test", "unit": "fraction"}]


def sql(j, statement, parameters=()):
    connection = sqlite3.connect(j.state / "tessera.sqlite3")
    connection.execute("PRAGMA foreign_keys=OFF")
    with connection:
        cursor = connection.execute(statement, parameters)
        row = cursor.fetchone() if statement.lstrip().startswith("SELECT") else None
    connection.close()
    return row


def admitted_attempt(j, experiment, variant_id):
    ident = "attempt_" + uuid4().hex
    now = utc_now()
    sql(
        j,
        """INSERT INTO research_experiment_admitted_attempts
        (id,experiment_id,design_revision,variant_id,task_id,plan_digest,grant_id,
         state,revision,created_at,updated_at,created_by_principal_id)
        VALUES (?,?,?,?,?,?,?,'admitted',1,?,?,?)""",
        (
            ident,
            experiment["id"],
            experiment["current_design_revision"],
            variant_id,
            "task_synthetic_" + ident,
            "e" * 64,
            "grant_synthetic_" + ident,
            now,
            now,
            j.actor["id"],
        ),
    )
    return ident


def resolve_attempt(j, attempt, outcome):
    sql(
        j,
        "UPDATE research_experiment_admitted_attempts SET state=?,outcome=?,reason=?,"
        "resolved_by_principal_id=?,operational_observed_state='exited',"
        "operational_termination_confirmed=1 WHERE id=?",
        (outcome, outcome, None if outcome == "succeeded" else "synthetic", j.actor["id"], attempt),
    )


def revision(j, attempt):
    return sql(
        j, "SELECT revision FROM research_experiment_admitted_attempts WHERE id=?", (attempt,)
    )[0]


def evidence(j, *, leaky=False, idle=False):
    """Build lock, audits, attempts, metrics and a group purely through the HTTP API."""
    execution = j.context()["basis"]["executions"][0]
    experiment = execution["experiment"]
    exp_variant = execution["variants"][0]["id"]
    project = j.project["id"]
    test_keys = [f"t{i}" for i in range(10)]
    lock = j.post(
        f"/projects/{project}/split-locks",
        {
            "name": "scaffold split",
            "partitions": {
                "train": {
                    "keys": [f"r{i}" for i in range(20)] + (["t0"] if leaky else []),
                    "groups": [f"g{i}" for i in range(20)] + (["g_t0"] if leaky else []),
                },
                "test": {"keys": test_keys, "groups": [f"s{i}" for i in range(10)]},
            },
        },
    )
    assert lock["within_lock_audit"]["status"] == ("overlap_detected" if leaky else "clean")
    corpus = j.post(
        f"/projects/{project}/split-locks",
        {"name": "pretraining corpus", "partitions": {"corpus": {"keys": ["c1", "c2", "c3"]}}},
    )
    cross = j.post(
        f"/split-locks/{lock['id']}/leakage-audits",
        {
            "reference_lock_id": corpus["id"],
            "reference_partitions": ["corpus"],
            "eval_partitions": ["test"],
        },
    )
    assert cross["status"] == "clean"
    attempts = []
    for seed, number in [*enumerate(NUMBERS), (3, None)]:
        attempt = admitted_attempt(j, experiment, exp_variant)
        bound = j.post(
            f"/experiment-attempts/{attempt}/split-binding",
            {
                "expected_revision": 1,
                "split_lock_id": lock["id"],
                "seed": seed,
                "metrics": METRICS,
            },
        )
        assert bound["membership_digest"] == lock["membership_digest"]
        if number is None:  # fourth attempt fails: excluded, never imputed
            resolve_attempt(j, attempt, "failed")
            attempts.append(attempt)
            continue
        measurement = j.post(
            f"/experiment-attempts/{attempt}/measurements",
            {
                "expected_revision": revision(j, attempt),
                "name": "ece",
                "value": {"number": number},
            },
        )
        resolve_attempt(j, attempt, "succeeded")
        j.post(
            f"/experiment-attempts/{attempt}/metric-records",
            {
                "measurement_id": measurement["id"],
                "checkpoint_sha": HEX,
            },
        )
        attempts.append(attempt)
    idle_attempt = None
    if idle:  # pre-registered seed never dispatched: blocks until cancelled with a reason
        idle_attempt = admitted_attempt(j, experiment, exp_variant)
        j.post(
            f"/experiment-attempts/{idle_attempt}/split-binding",
            {
                "expected_revision": 1,
                "split_lock_id": lock["id"],
                "seed": 8,
                "metrics": METRICS,
            },
        )
        attempts.append(idle_attempt)
        blocked = j.post(
            f"/projects/{project}/replicate-groups",
            {
                "experiment_variant_id": exp_variant,
                "metric": "ece",
                "split_lock_id": lock["id"],
                "partition": "test",
            },
            status=409,
        )
        assert idle_attempt in blocked["detail"]
        # The only way out is an explicit, reasoned human cancellation before dispatch.
        j.post(
            f"/experiment-admitted-attempts/{idle_attempt}/resolve",
            {
                "expected_revision": revision(j, idle_attempt),
                "outcome": "cancelled",
                "reason": "Seed withdrawn before dispatch: worker capacity unavailable",
            },
        )
    group = j.post(
        f"/projects/{project}/replicate-groups",
        {
            "experiment_variant_id": exp_variant,
            "metric": "ece",
            "split_lock_id": lock["id"],
            "partition": "test",
        },
    )
    return exp_variant, lock, corpus, group, attempts


def plan_and_record(j, exp_variant, lock, corpus, group, **gate_changes):
    p = protocol(
        variant_id=j.variant["id"],
        experiment_variant_id=exp_variant,
        split_lock_id=lock["id"],
        split_digest=lock["membership_digest"],
        required_reference_lock_ids=[corpus["id"]],
        **gate_changes,
    )
    plan = j.propose(j.scope(evaluation_protocol=p))
    j.resolve(plan)
    row = next(r for r in j.context()["reviews"] if r["id"] == plan["id"])
    r = record(p, plan["id"], row["scope_digest"], group=group["id"])
    return p, r


def report_of(j, evaluation):
    return next(v for v in j.context()["reviews"] if v["id"] == evaluation["id"])[
        "evaluation_report"
    ]


def test_go_requires_platform_aggregate_of_stored_bound_measurements(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    finding = j.finding()
    exp_variant, lock, corpus, group, attempts = evidence(j)

    # Platform-computed aggregate over exactly the three succeeded seeds.
    assert group["n"] == 3
    assert group["mean"] == pytest.approx(statistics.mean(NUMBERS))
    assert group["ci_low"] < group["mean"] < group["ci_high"]
    assert [e["attempt_id"] for e in group["excluded_attempts"]] == [attempts[3]]
    assert group["imputation"] == "none"
    assert group["sources"] == ["human_entered"]

    # Default gate trusts only derived measurements: human-entered data is inconclusive.
    p, r = plan_and_record(j, exp_variant, lock, corpus, group)
    strict = j.propose(j.scope("evaluation", evaluation=r))
    assert report_of(j, strict)["gates"][0]["reason"] == "disallowed_measurement_source"
    assert report_of(j, strict)["status"] == "inconclusive"

    # A gate that explicitly allows human_entered passes on the platform aggregate.
    p, r = plan_and_record(j, exp_variant, lock, corpus, group, allowed_sources=["human_entered"])
    assert EvaluationProtocol.model_validate(p)

    # Bare caller floats are refused: v1-style record, and a value on a v2 observation.
    v1 = r | {
        "version": 1,
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
    j.propose(j.scope("evaluation", evaluation=v1), status=409)
    floaty = r | {
        "observations": [{"gate_id": "g1", "replicate_group_id": group["id"], "value": 0.99}]
    }
    bare = j.client.post(
        "/interaction/decisions",
        json={
            "project_id": j.project["id"],
            "kind": "direction",
            "scope": j.scope("evaluation", evaluation=floaty),
            "expires_at": j.expiry(),
            "reviewer_principal_id": j.actor["id"],
        },
        headers={"Authorization": j.owner, "Idempotency-Key": uuid4().hex},
    )
    assert bare.status_code in (409, 422), bare.text

    j.resolve(j.propose(j.scope("source_use")))
    evaluation = j.propose(j.scope("evaluation", evaluation=r))
    j.resolve(evaluation, token=j.agent, status=403)
    j.resolve(evaluation)
    assert report_of(j, evaluation)["status"] == "pass"
    go = go_for(j, finding, evaluation)
    j.resolve(go, token=j.agent, status=403)
    j.resolve(go, status=200)
    rows = {v["id"]: v for v in j.context()["reviews"]}
    assert rows[go["id"]]["gate_evidence_binding"] == "platform_replicate_aggregate"
    assert j.context()["execution_granted"] is False


def test_split_overlap_makes_gate_inconclusive_and_blocks_go(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    finding = j.finding()
    exp_variant, lock, corpus, group, _ = evidence(j, leaky=True)
    _, r = plan_and_record(j, exp_variant, lock, corpus, group, allowed_sources=["human_entered"])
    j.resolve(j.propose(j.scope("source_use")))
    evaluation = j.propose(j.scope("evaluation", evaluation=r))
    j.resolve(evaluation)
    report = report_of(j, evaluation)
    assert report["status"] == "inconclusive"
    assert report["gates"][0]["reason"] == "split_leakage_not_clean"
    go = go_for(j, finding, evaluation)
    assert "passing declared gates" in j.resolve(go, status=409)["detail"]


def test_stale_replicate_group_cannot_satisfy_a_gate(tmp_path, monkeypatch, capsys):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    j.finding()
    exp_variant, lock, corpus, group, _ = evidence(j)
    # Another seed is bound to the same variant and lock after the group was cut:
    # the old subset no longer equals the series, whatever the new seed shows.
    experiment = j.context()["basis"]["executions"][0]["experiment"]
    late = admitted_attempt(j, experiment, exp_variant)
    j.post(
        f"/experiment-attempts/{late}/split-binding",
        {"expected_revision": 1, "split_lock_id": lock["id"], "seed": 9, "metrics": METRICS},
    )
    _, r = plan_and_record(j, exp_variant, lock, corpus, group, allowed_sources=["human_entered"])
    j.resolve(j.propose(j.scope("source_use")))
    report = report_of(j, j.propose(j.scope("evaluation", evaluation=r)))
    assert report["status"] == "inconclusive"
    assert report["gates"][0]["reason"] == "replicate_group_stale"


def test_never_dispatched_seed_blocks_until_cancelled_and_stays_visible(
    tmp_path, monkeypatch, capsys
):
    j = linked_journey(tmp_path, monkeypatch, capsys)
    j.assess()
    j.finding()
    exp_variant, lock, corpus, group, attempts = evidence(j, idle=True)
    idle = attempts[-1]
    excluded = {e["attempt_id"]: e for e in group["excluded_attempts"]}
    assert excluded[idle]["outcome"] == "cancelled" and group["n"] == 3
    assert excluded[idle]["reason"].startswith("Seed withdrawn before dispatch")
    p, r = plan_and_record(j, exp_variant, lock, corpus, group, allowed_sources=["human_entered"])
    j.resolve(j.propose(j.scope("source_use")))
    current = report_of(j, j.propose(j.scope("evaluation", evaluation=r)))
    assert current["gates"][0]["reason"] != "replicate_group_stale"
    assert idle in {e["attempt_id"] for e in current["gates"][0]["group"]["excluded_attempts"]}
