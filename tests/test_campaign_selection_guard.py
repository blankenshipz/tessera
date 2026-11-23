"""Selection split guard: choose an arm on validation metrics only; synthetic data."""

from __future__ import annotations

import json

import pytest
from test_campaign_comparison import _attempt, _doc, _good, _run, _write_receipt, web  # noqa: F401

from tessera_control.campaign_comparison import ComparisonRefused, compare_arms

VAL = "1" * 64
TEST = "2" * 64

# Unique, easily searched test values; int2 has the best TEST value but not the best validation.
TEST_VALUES = {"base": 0.5111, "int1": 0.7123, "int2": 0.9234, "int3": 0.6345}
VACC = {"base": 0.5, "int1": 0.9, "int2": 0.8, "int3": 0.7}
VLOSS = {"base": 3.0, "int1": 0.61, "int2": 0.52, "int3": 0.27}


def _arm(key, vacc=None, vloss=None, tacc=None, val=VAL, test=TEST):
    io = [
        {"direction": "input", "name": "val", "sha256": val},
        {"direction": "input", "name": "held", "sha256": test},
    ]
    metrics = {
        "vacc": VACC[key] if vacc is None else vacc,
        "vloss": VLOSS[key] if vloss is None else vloss,
        "tacc": TEST_VALUES[key] if tacc is None else tacc,
    }
    return [_attempt(metrics=metrics, split=None, extra_io=io)]


def _sel_doc(**overrides):
    stages = {k: _arm(k) for k in ("base", "int1", "int2", "int3")}
    stages.update(overrides)
    return _doc(**stages)


def _sel(doc=None, **kw):
    args = {
        "baseline": "base",
        "interventions": ["int1", "int2", "int3"],
        "validation_metrics": ["vacc", "vloss"],
        "test_metrics": ["tacc"],
        "validation_inputs": ["val"],
        "test_inputs": ["held"],
        "select_by": "vacc",
        "select_direction": "max",
        "declared": {},
    }
    args.update(kw)
    return compare_arms(doc or _sel_doc(), **args)


def _sel_code(doc=None, **kw):
    with pytest.raises(ComparisonRefused) as raised:
        _sel(doc, **kw)
    return raised.value.code


def _metric(result, name):
    return next(m for m in result["metrics"] if m["name"] == name)


def _assert_unselected_test_values_absent(result, selected):
    text = json.dumps(result)
    for key, value in TEST_VALUES.items():
        if key in ("base", selected):
            assert str(value) in text
        else:
            assert str(value) not in text, key


def test_selects_best_on_validation_max_not_best_on_test():
    result = _sel()
    sel = result["selection"]
    assert sel["by"] == "vacc" and sel["direction"] == "max"
    assert sel["split_role"] == "validation"
    assert sorted(sel["candidates"]) == ["int1", "int2", "int3"]
    assert sel["selected"] == "int1"  # int2 has the best test value but is not selected
    assert sel["values"] == {"int1": 0.9, "int2": 0.8, "int3": 0.7}
    assert result["scientific_validity"] == "not_assessed"
    assert not {"decision", "verdict", "recommendation"} & set(result)


def test_selects_best_on_validation_min():
    result = _sel(select_by="vloss", select_direction="min")
    assert result["selection"]["selected"] == "int3"
    assert result["selection"]["by"] == "vloss"
    _assert_unselected_test_values_absent(result, "int3")


def test_validation_metrics_for_every_arm_and_roles_labelled():
    result = _sel()
    for name in ("vacc", "vloss"):
        metric = _metric(result, name)
        assert metric["split_role"] == "validation"
        assert [i["stage_key"] for i in metric["interventions"]] == ["int1", "int2", "int3"]
    assert _metric(result, "vacc")["baseline"]["value"] == 0.5
    assert _metric(result, "tacc")["split_role"] == "test"


def test_test_metrics_only_for_baseline_and_selected_arm():
    result = _sel()
    tacc = _metric(result, "tacc")
    assert tacc["baseline"]["value"] == TEST_VALUES["base"]
    assert [i["stage_key"] for i in tacc["interventions"]] == ["int1"]
    assert tacc["interventions"][0]["value"] == TEST_VALUES["int1"]
    _assert_unselected_test_values_absent(result, "int1")


def test_unselected_test_values_absent_when_best_test_arm_is_unselected():
    result = _sel()
    text = json.dumps(result)
    assert str(TEST_VALUES["int2"]) not in text
    assert str(TEST_VALUES["int3"]) not in text


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"select_by": "tacc"}, "selection_reads_test_metric"),
        ({"select_by": "ghost"}, "split_role_missing"),
        ({"metrics": ["vacc"]}, "split_role_missing"),
        ({"validation_inputs": []}, "split_role_missing"),
        ({"test_inputs": []}, "split_role_missing"),
        ({"validation_metrics": []}, "split_role_missing"),
        ({"test_metrics": ["tacc", "vacc"]}, "split_role_conflict"),
        ({"test_inputs": ["val"]}, "split_role_conflict"),
        ({"select_direction": None}, "selection_direction_missing"),
    ],
)
def test_selection_refusals(kw, code):
    assert _sel_code(**kw) == code


def test_validation_equal_to_test_identity_is_refused():
    # every arm agrees on the data, but the validation data is the held-out data
    doc = _doc(**{k: _arm(k, val=TEST, test=TEST) for k in ("base", "int1", "int2", "int3")})
    assert _sel_code(doc) == "validation_equals_test"


def test_validation_inputs_differing_across_arms_is_refused():
    doc = _sel_doc(int2=_arm("int2", val="3" * 64))
    assert _sel_code(doc) == "evaluation_inputs_differ"


def test_tie_on_best_validation_value_is_refused():
    doc = _sel_doc(int2=_arm("int2", vacc=0.9))
    assert _sel_code(doc) == "selection_tie"


def test_tie_on_a_non_best_value_is_fine():
    doc = _sel_doc(int2=_arm("int2", vacc=0.7))
    result = _sel(doc)
    assert result["selection"]["selected"] == "int1"
    assert result["selection"]["values"] == {"int1": 0.9, "int2": 0.7, "int3": 0.7}


def _no_test_metric(key):
    attempt = _arm(key)[0]
    attempt["measurements"] = [m for m in attempt["measurements"] if m["name"] != "tacc"]
    return [attempt]


def test_selection_never_reads_test_metrics_of_unselected_arms():
    # int2 and int3 record no test metric at all: reading it would refuse metric_not_recorded.
    doc = _sel_doc(int2=_no_test_metric("int2"), int3=_no_test_metric("int3"))
    result = _sel(doc)
    assert result["selection"]["selected"] == "int1"
    # Changing every test value cannot change the selection.
    flipped = _sel_doc(**{k: _arm(k, tacc=1.0 - TEST_VALUES[k]) for k in ("int1", "int2", "int3")})
    assert _sel(flipped)["selection"]["selected"] == "int1"


HOLDOUT = {"evaluation": {"final": True, "holdout": {"input": "held"}}}


def _declared_all(spec):
    return {k: spec for k in ("base", "int1", "int2", "int3")}


def test_selection_refuses_arms_whose_declared_split_locks_differ():
    declared = _declared_all({"gate_evidence": {"split_lock_id": "lock-a"}})
    declared["int2"] = {"gate_evidence": {"split_lock_id": "lock-b"}}
    assert _sel_code(declared=declared) == "split_lock_differs"


def test_selection_declared_held_out_input_is_test_role_even_when_unnamed():
    result = _sel(test_inputs=["val2"], declared=_declared_all(HOLDOUT), doc=_sel_doc_val2())
    assert result["evaluation_inputs"]["held"]["split_role"] == "test"


def _sel_doc_val2():
    extra = {"direction": "input", "name": "val2", "sha256": "4" * 64}
    stages = {k: _arm(k) for k in ("base", "int1", "int2", "int3")}
    for attempts in stages.values():
        attempts[0]["io"].append(extra)
    return _doc(**stages)


def test_selection_refuses_the_declared_held_out_input_as_validation_data():
    # The caller labels the spec's held-out input as validation data and names another test input.
    result_code = _sel_code(
        _sel_doc_val2(),
        validation_inputs=["held"],
        test_inputs=["val2"],
        declared=_declared_all(HOLDOUT),
    )
    assert result_code == "validation_equals_test"


def test_selection_declared_held_out_input_must_match_across_arms():
    doc = _sel_doc(int3=_arm("int3", test="5" * 64))
    assert _sel_code(doc, declared=_declared_all(HOLDOUT)) == "evaluation_inputs_differ"


def test_failed_arm_attempt_still_refused_in_selection_mode():
    failed = [_attempt(1, "failed", {"vacc": 0.99, "vloss": 0.1, "tacc": 0.1}, split=None)]
    assert _sel_code(_sel_doc(int1=failed)) == "arm_attempt_not_unique"


def test_non_selection_behaviour_is_unchanged():
    result = _run()
    assert "selection" not in result
    assert [m["name"] for m in result["metrics"]] == ["acc", "loss"]
    assert all("split_role" not in m for m in result["metrics"])
    assert _good()  # fixture style helper still builds


# ---- HTTP layer ----

SEL_QUERY = (
    "baseline=base&intervention=int1&intervention=int2&intervention=int3"
    "&validation_metric=vacc&validation_metric=vloss&test_metric=tacc"
    "&validation_input=val&test_input=held&select_by=vacc&select_direction=max"
)


def test_route_selection_returns_block_without_unselected_test_values(web):  # noqa: F811
    client, headers, cid, database = web
    _write_receipt(database, cid, _sel_doc())
    response = client.get(f"/campaigns/{cid}/comparison?{SEL_QUERY}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selection"]["selected"] == "int1"
    assert body["selection"]["split_role"] == "validation"
    for key in ("int2", "int3"):
        assert str(TEST_VALUES[key]) not in response.text
    assert str(TEST_VALUES["int1"]) in response.text


def test_route_refuses_selection_on_a_test_metric(web):  # noqa: F811
    client, headers, cid, database = web
    _write_receipt(database, cid, _sel_doc())
    query = SEL_QUERY.replace("select_by=vacc", "select_by=tacc")
    response = client.get(f"/campaigns/{cid}/comparison?{query}", headers=headers)
    assert response.status_code == 409
    assert "comparison_refused:selection_reads_test_metric" in response.json()["detail"]


def test_route_rejects_a_bad_select_direction(web):  # noqa: F811
    client, headers, cid, database = web
    _write_receipt(database, cid, _sel_doc())
    query = SEL_QUERY.replace("select_direction=max", "select_direction=sideways")
    response = client.get(f"/campaigns/{cid}/comparison?{query}", headers=headers)
    assert response.status_code == 422
