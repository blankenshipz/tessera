"""Baseline-vs-intervention campaign comparison: pure rules, then the HTTP route; synthetic data."""

from __future__ import annotations

import json
import math

import pytest
from campaign_fixture import declare_snapshot_public
from fastapi.testclient import TestClient
from test_execution import _setup

from tessera_control.api import create_app
from tessera_control.campaign_comparison import ComparisonRefused, compare_arms
from tessera_control.store import Conflict, _connect

SPLIT_A = "a" * 64
SPLIT_B = "b" * 64


def _attempt(n=1, status="succeeded", metrics=None, split=SPLIT_A, split_name="eval", extra_io=()):
    io = [] if split is None else [{"direction": "input", "name": split_name, "sha256": split}]
    return {
        "attempt": n,
        "status": status,
        "measurements": [{"name": k, "value": v} for k, v in (metrics or {}).items()],
        "io": [*io, *extra_io],
        "run_receipt_digest": f"rr-{n}",
        "evidence_kind": "run_receipt",
        "evidence_digest": f"ev-{n}",
    }


def _doc(**stages):
    return {
        "campaign_id": "c1",
        "receipt": {"stages": [{"stage_key": k, "attempts": v} for k, v in stages.items()]},
        "receipt_digest": "d" * 64,
    }


def _good(**overrides):
    stages = {
        "base": [_attempt(metrics={"acc": 0.5, "loss": 2.0})],
        "int1": [_attempt(metrics={"acc": 0.75, "loss": 1.5})],
        "int2": [_attempt(metrics={"acc": 0.25, "loss": 3.0})],
    }
    stages.update(overrides)
    return _doc(**stages)


def _run(doc=None, **kw):
    args = {
        "baseline": "base",
        "interventions": ["int1", "int2"],
        "metrics": ["acc", "loss"],
        "evaluation_inputs": ["eval"],
        "declared": {},
    }
    args.update(kw)
    return compare_arms(doc or _good(), **args)


def _code(doc=None, **kw):
    with pytest.raises(ComparisonRefused) as raised:
        _run(doc, **kw)
    assert isinstance(raised.value, Conflict)
    return raised.value.code


def test_happy_path_deltas_identity_and_no_verdict():
    result = _run()
    assert result["campaign_id"] == "c1" and result["receipt_digest"] == "d" * 64
    assert result["scientific_validity"] == "not_assessed"
    assert not {"decision", "verdict", "recommendation"} & set(result)
    assert result["baseline"]["stage_key"] == "base"
    assert result["baseline"]["attempt"] == 1
    assert result["baseline"]["run_receipt_digest"] == "rr-1"
    assert result["baseline"]["evidence_digest"] == "ev-1"
    assert [i["stage_key"] for i in result["interventions"]] == ["int1", "int2"]
    assert result["evaluation_inputs"]["eval"]["sha256"] == SPLIT_A
    assert sorted(result["evaluation_inputs"]["eval"]["arms"]) == ["base", "int1", "int2"]
    by_name = {m["name"]: m for m in result["metrics"]}
    assert by_name["acc"]["baseline"]["value"] == 0.5
    acc = {i["stage_key"]: i for i in by_name["acc"]["interventions"]}
    assert acc["int1"]["delta"] == pytest.approx(0.25)
    assert acc["int2"]["delta"] == pytest.approx(-0.25)
    assert acc["int1"]["run_receipt_digest"] == "rr-1"
    loss = {i["stage_key"]: i["delta"] for i in by_name["loss"]["interventions"]}
    assert loss == {"int1": pytest.approx(-0.5), "int2": pytest.approx(1.0)}
    assert "interval" in result["method"] or "uncertainty" in result["method"]


def test_uses_the_succeeded_attempt_not_the_failed_one():
    doc = _good(
        base=[
            _attempt(1, "failed", {"acc": 0.0, "loss": 9.0}),
            _attempt(2, "succeeded", {"acc": 0.5, "loss": 2.0}),
        ]
    )
    result = _run(doc)
    assert result["baseline"]["attempt"] == 2
    assert result["metrics"][0]["baseline"]["value"] == 0.5


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"interventions": ["base"]}, "arms_not_distinct"),
        ({"interventions": ["int1", "int1"]}, "arms_not_distinct"),
        ({"interventions": []}, "no_intervention"),
        ({"metrics": []}, "no_metric"),
        ({"interventions": [f"i{n}" for n in range(9)]}, "too_many_arms"),
        ({"metrics": [f"m{n}" for n in range(17)]}, "too_many_metrics"),
        ({"interventions": ["int1", "ghost"]}, "stage_missing"),
        ({"baseline": "ghost"}, "stage_missing"),
        ({"evaluation_inputs": []}, "evaluation_inputs_unnamed"),
        ({"evaluation_inputs": ["other"]}, "evaluation_input_missing"),
        ({"metrics": ["acc", "nope"]}, "metric_not_recorded"),
    ],
)
def test_refusal_codes(kw, code):
    assert _code(**kw) == code


def test_refusal_checks_never_drop_an_arm():
    # Too many arms is refused even though every stage exists.
    many = {f"i{n}": [_attempt(metrics={"acc": 1.0, "loss": 1.0})] for n in range(9)}
    doc = _good(**many)
    assert _code(doc, interventions=list(many)) == "too_many_arms"


def test_only_attempt_failed_is_not_a_result():
    doc = _good(int1=[_attempt(1, "failed", {"acc": 0.9, "loss": 1.0})])
    assert _code(doc) == "arm_attempt_not_unique"
    doc = _good(base=[_attempt(1, "failed", {"acc": 0.9, "loss": 1.0})])
    assert _code(doc) == "arm_attempt_not_unique"


def test_two_succeeded_attempts_are_refused():
    doc = _good(
        int2=[
            _attempt(1, metrics={"acc": 1, "loss": 1}),
            _attempt(2, metrics={"acc": 2, "loss": 2}),
        ]
    )
    assert _code(doc) == "arm_attempt_not_unique"


def test_no_attempts_at_all_is_refused():
    assert _code(_good(int1=[])) == "arm_attempt_not_unique"


def test_evaluation_input_must_be_an_input_with_identity():
    out = _attempt(metrics={"acc": 1, "loss": 1}, split=None)
    out["io"] = [{"direction": "output", "name": "eval", "sha256": SPLIT_A}]
    assert _code(_good(int1=[out])) == "evaluation_input_missing"
    # Missing on the baseline is equally refused.
    assert _code(_good(base=[out])) == "evaluation_input_missing"


def test_different_evaluation_digest_is_refused():
    doc = _good(int2=[_attempt(metrics={"acc": 0.25, "loss": 3.0}, split=SPLIT_B)])
    assert _code(doc) == "evaluation_inputs_differ"


def test_same_named_inputs_with_equal_digests_pass():
    doc = _good(int1=[_attempt(metrics={"acc": 0.75, "loss": 1.5}, split=SPLIT_A)])
    assert _run(doc)["evaluation_inputs"]["eval"]["sha256"] == SPLIT_A


def test_second_named_evaluation_input_must_also_match():
    def with_other(digest):
        return _attempt(
            metrics={"acc": 1.0, "loss": 1.0},
            extra_io=[{"direction": "input", "name": "extra", "sha256": digest}],
        )

    doc = _good(
        base=[with_other("1" * 64)], int1=[with_other("1" * 64)], int2=[with_other("2" * 64)]
    )
    assert _code(doc, evaluation_inputs=["eval", "extra"]) == "evaluation_inputs_differ"


def test_duplicated_measurement_is_not_recorded_uniquely():
    attempt = _attempt(metrics={"acc": 1.0, "loss": 1.0})
    attempt["measurements"].append({"name": "acc", "value": 1.0})
    assert _code(_good(int1=[attempt])) == "metric_not_recorded"


@pytest.mark.parametrize("value", [True, False, math.nan, math.inf, "0.5", None])
def test_non_numeric_measurement_is_refused(value):
    doc = _good(int1=[_attempt(metrics={"acc": value, "loss": 1.0})])
    assert _code(doc) == "metric_not_numeric"


def test_integer_values_are_accepted():
    doc = _good(int1=[_attempt(metrics={"acc": 3, "loss": 1})])
    result = _run(doc)
    acc = next(m for m in result["metrics"] if m["name"] == "acc")
    assert next(i for i in acc["interventions"] if i["stage_key"] == "int1")["delta"] == 2.5


# ---- HTTP layer: a real campaign row, receipt row written directly (see report) ----

IMAGE = "python@sha256:" + "a" * 64


@pytest.fixture
def web(tmp_path):
    state, execution, owner, worker, project, _ = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot = connection.execute(
            "SELECT r.snapshot_id, s.digest FROM snapshot_releases r "
            "JOIN source_snapshots s ON s.id = r.snapshot_id"
        ).fetchone()
    declare_snapshot_public(state, snapshot[0])
    client = TestClient(create_app(state))
    headers = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    stage = {
        "key": "s1",
        "backend": "docker-local-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "output_paths": ["data.json"],
        "max_output_bytes": 4096,
        "max_duration_seconds": 60,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": snapshot[0],
                "digest": snapshot[1],
                "data_class": "public",
            }
        ],
    }
    spec = {
        "version": 1,
        "name": "synthetic campaign",
        "budget_cap_cents": 4,
        "deadline_seconds": 600,
        "worker_principal_id": worker["id"],
        "stages": [stage],
    }
    response = client.post(
        f"/projects/{project['id']}/campaigns",
        json={"spec": spec},
        headers={**headers, "Idempotency-Key": "k1"},
    )
    assert response.status_code in (200, 201), response.text
    return client, headers, response.json()["id"], execution.database_path


def _write_receipt(database_path, campaign_id, doc, stages=None):
    """Store the receipt row; ``stages`` replaces the stored spec's stages (what each arm declared).

    The receipt carries the campaign's real spec digest, which is how the route finds the spec."""
    with _connect(database_path) as connection:
        row = connection.execute(
            "SELECT spec_json, spec_digest FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        doc["receipt"]["spec_digest"] = row["spec_digest"]
        if stages is not None:
            spec = json.loads(row["spec_json"])
            spec["stages"] = stages
            connection.execute(
                "UPDATE campaigns SET spec_json=? WHERE id=?", (json.dumps(spec), campaign_id)
            )
        connection.execute(
            "INSERT INTO campaign_receipts(campaign_id, receipt_json, receipt_digest, created_at)"
            " VALUES (?,?,?,?)",
            (
                campaign_id,
                json.dumps(doc["receipt"]),
                doc["receipt_digest"],
                "2026-01-01T00:00:00Z",
            ),
        )


QUERY = (
    "baseline=base&intervention=int1&intervention=int2&metric=acc&metric=loss&evaluation_input=eval"
)


def test_route_returns_comparison_for_a_receipt(web):
    client, headers, cid, database = web
    _write_receipt(database, cid, _good())
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == cid and body["receipt_digest"] == "d" * 64
    assert body["scientific_validity"] == "not_assessed"
    assert body["evaluation_inputs"]["eval"]["sha256"] == SPLIT_A
    acc = next(m for m in body["metrics"] if m["name"] == "acc")
    assert [i["delta"] for i in acc["interventions"]] == [pytest.approx(0.25), pytest.approx(-0.25)]
    assert not {"decision", "verdict", "recommendation"} & set(body)


def test_route_is_404_without_a_receipt(web):
    client, headers, cid, _ = web
    assert client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers).status_code == 404
    assert client.get(f"/campaigns/missing/comparison?{QUERY}", headers=headers).status_code == 404


def test_route_refuses_differing_evaluation_inputs_with_the_code(web):
    client, headers, cid, database = web
    differing = _good(int2=[_attempt(metrics={"acc": 0.25, "loss": 3.0}, split=SPLIT_B)])
    _write_receipt(database, cid, differing)
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 409
    assert "evaluation_inputs_differ" in response.json()["detail"]


def _declared(**by_stage):
    return {key: {"key": key, **value} for key, value in by_stage.items()}


def _arms(**per_stage):
    return [
        {"key": key, **value}
        for key, value in {"base": {}, "int1": {}, "int2": {}, **per_stage}.items()
    ]


def test_route_refuses_arms_with_different_split_locks_even_if_the_named_input_matches(web):
    client, headers, cid, database = web
    stages = _arms(
        int1={"gate_evidence": {"split_lock_id": "lock-b"}},
        base={"gate_evidence": {"split_lock_id": "lock-a"}},
        int2={"gate_evidence": {"split_lock_id": "lock-a"}},
    )
    _write_receipt(database, cid, _good(), stages)  # the named `eval` input matches in every arm
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 409
    assert "split_lock_differs" in response.json()["detail"]


def test_route_refuses_a_split_lock_on_one_arm_only(web):
    client, headers, cid, database = web
    stages = _arms(int2={"gate_evidence": {"split_lock_id": "lock-a"}})
    _write_receipt(database, cid, _good(), stages)
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 409 and "split_lock_differs" in response.json()["detail"]


def test_route_refuses_when_an_unnamed_held_out_input_differs(web):
    """The caller names only `code`; the approved spec declares `heldout` as the holdout input
    and the arms consumed different bytes there. Naming a matching input must not hide that."""
    client, headers, cid, database = web
    holdout = {"evaluation": {"final": True, "holdout": {"input": "heldout"}}}
    stages = _arms(base=holdout, int1=holdout, int2=holdout)
    extra = ({"direction": "input", "name": "heldout", "sha256": SPLIT_A},)
    differing = _good(
        base=[_attempt(metrics={"acc": 0.5, "loss": 2.0}, split_name="code", extra_io=extra)],
        int1=[_attempt(metrics={"acc": 0.75, "loss": 1.5}, split_name="code", extra_io=extra)],
        int2=[
            _attempt(
                metrics={"acc": 0.25, "loss": 3.0},
                split_name="code",
                extra_io=({"direction": "input", "name": "heldout", "sha256": SPLIT_B},),
            )
        ],
    )
    _write_receipt(database, cid, differing, stages)
    query = QUERY.replace("evaluation_input=eval", "evaluation_input=code")
    response = client.get(f"/campaigns/{cid}/comparison?{query}", headers=headers)
    assert response.status_code == 409, response.text
    assert "evaluation_inputs_differ" in response.json()["detail"]


def test_route_accepts_matching_declared_split_lock_and_held_out_input(web):
    client, headers, cid, database = web
    holdout = {
        "evaluation": {"final": True, "holdout": {"input": "heldout"}},
        "gate_evidence": {"split_lock_id": "lock-a"},
    }
    stages = _arms(base=holdout, int1=holdout, int2=holdout)
    extra = ({"direction": "input", "name": "heldout", "sha256": SPLIT_B},)
    doc = _good(
        base=[_attempt(metrics={"acc": 0.5, "loss": 2.0}, extra_io=extra)],
        int1=[_attempt(metrics={"acc": 0.75, "loss": 1.5}, extra_io=extra)],
        int2=[_attempt(metrics={"acc": 0.25, "loss": 3.0}, extra_io=extra)],
    )
    _write_receipt(database, cid, doc, stages)
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 200, response.text
    assert set(response.json()["evaluation_inputs"]) == {"eval", "heldout"}


def test_route_refuses_when_the_receipt_names_a_spec_that_is_not_the_stored_one(web):
    client, headers, cid, database = web
    doc = _good()
    _write_receipt(database, cid, doc)
    with _connect(database) as connection:
        stored = json.loads(
            connection.execute(
                "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", (cid,)
            ).fetchone()[0]
        )
        stored["spec_digest"] = "e" * 64
        connection.execute("DROP TRIGGER IF EXISTS campaign_receipts_no_update")
        connection.execute(
            "UPDATE campaign_receipts SET receipt_json=? WHERE campaign_id=?",
            (json.dumps(stored), cid),
        )
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 409 and "spec_unavailable" in response.json()["detail"]


def test_pure_declared_checks_are_required_and_derive_from_the_record():
    base = _good()
    assert _code(base, declared=_declared(int1={"gate_evidence": {"split_lock_id": "x"}})) == (
        "split_lock_differs"
    )
    with pytest.raises(TypeError):  # the declared record is not optional
        compare_arms(
            base,
            baseline="base",
            interventions=["int1"],
            metrics=["acc"],
            evaluation_inputs=["eval"],
        )
