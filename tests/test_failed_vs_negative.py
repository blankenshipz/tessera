"""A failed run is not a negative result: stances need a succeeded, assessable attempt."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_attempt_result_assessment import _admit, _assess, _current, _resolve, _succeed
from test_goal_to_decision import _command

from tessera_control.api import create_app
from tessera_control.store import Conflict, canonical_digest

REFUSED = "stance"


def _measure(world) -> dict:
    return _command(
        world["records"].record_measurement,
        world["owner"],
        world["admitted"]["id"],
        payload={
            "expected_revision": _current(world)["revision"],
            "name": "comparison",
            "value": {"synthetic": True},
        },
    )


def _citation(world, stance: str | None = None) -> dict:
    cited = {"attempt_id": world["admitted"]["id"], "measurement_ids": [_measure(world)["id"]]}
    return cited | ({"stance": stance} if stance else {})


def _token(world) -> str:
    return (world["state"] / "owner.header").read_text().split("Bearer ", 1)[1].strip()


def _post(world, citation: dict, key: str = "c1"):
    with TestClient(create_app(world["state"])) as client:
        return client.post(
            f"/experiments/{world['experiment']['id']}/conclusions",
            json={"statement": "Interpretation.", "evidence": [citation]},
            headers={"Authorization": f"Bearer {_token(world)}", "Idempotency-Key": key},
        )


def _create(world, citation: dict) -> dict:
    return _command(
        world["records"].create_conclusion,
        world["owner"],
        world["experiment"]["id"],
        payload={"statement": "Interpretation.", "evidence": [citation]},
    )


def _item(conclusion: dict) -> dict:
    return conclusion["evidence"][0]


def _export(world) -> dict:
    with TestClient(create_app(world["state"])) as client:
        response = client.get(
            f"/experiments/{world['experiment']['id']}/export",
            headers={"Authorization": f"Bearer {_token(world)}"},
        )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("outcome", ("failed", "cancelled", "unknown"))
@pytest.mark.parametrize("stance", ("supports", "opposes"))
def test_non_success_attempt_cannot_carry_a_stance(tmp_path: Path, outcome, stance) -> None:
    world = _admit(tmp_path)
    _resolve(world, outcome, "The run did not complete.")
    citation = _citation(world, stance)
    with pytest.raises(Conflict, match=REFUSED):
        _create(world, citation)
    response = _post(world, citation)
    assert response.status_code == 409
    assert "stance" in response.json()["detail"].lower()


def test_failed_attempt_is_never_a_negative_result(tmp_path: Path) -> None:
    world = _admit(tmp_path)
    _resolve(world, "failed", "The run crashed.")
    with pytest.raises(Conflict, match="not a negative result"):
        _assess(world, "negative")
    assert _current(world)["result_assessment"] is None


def test_failed_attempt_cited_as_context_is_allowed_and_flagged(tmp_path: Path) -> None:
    world = _admit(tmp_path)
    _resolve(world, "failed", "The run crashed.")
    response = _post(world, _citation(world))
    assert response.status_code == 201
    item = _item(response.json())
    assert item["stance"] is None
    assert item["attempt_kind"] == "admitted"
    assert item["attempt_outcome"] == "failed"
    assert item["result_assessment"] is None
    assert item["run_failed"] is True


@pytest.mark.parametrize("stance", ("supports", "opposes"))
def test_valid_negative_result_may_carry_a_stance_and_is_not_a_failed_run(
    tmp_path: Path, stance
) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    _assess(world, "negative")
    response = _post(world, _citation(world, stance))
    assert response.status_code == 201
    item = _item(response.json())
    assert item["stance"] == stance
    assert item["attempt_kind"] == "admitted"
    assert item["attempt_outcome"] == "succeeded"
    assert item["result_assessment"] == "negative"
    assert item["run_failed"] is False


def test_positive_and_unassessed_succeeded_attempts_may_carry_a_stance(tmp_path: Path) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    unassessed = _create(world, _citation(world, "supports"))
    assert _item(unassessed)["result_assessment"] is None
    assert _item(unassessed)["attempt_outcome"] == "succeeded"
    assert _item(unassessed)["run_failed"] is False
    _assess(world, "positive")
    positive = _create(world, _citation(world, "supports"))
    assert _item(positive)["result_assessment"] == "positive"
    assert _item(positive)["run_failed"] is False


@pytest.mark.parametrize("stance", ("supports", "opposes"))
def test_inconclusive_assessment_cannot_carry_a_stance(tmp_path: Path, stance) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    _assess(world, "inconclusive")
    citation = _citation(world, stance)
    with pytest.raises(Conflict, match=REFUSED):
        _create(world, citation)
    assert _post(world, citation).status_code == 409
    context = _post(world, _citation(world), key="ctx")
    assert context.status_code == 201
    assert _item(context.json())["result_assessment"] == "inconclusive"
    assert _item(context.json())["run_failed"] is False


@pytest.mark.parametrize("stance", ("supports", "opposes"))
def test_unresolved_admitted_attempt_cannot_carry_a_stance(tmp_path: Path, stance) -> None:
    world = _admit(tmp_path)
    citation = _citation(world, stance)
    with pytest.raises(Conflict, match=REFUSED):
        _create(world, citation)
    context = _create(world, _citation(world))
    item = _item(context)
    assert item["attempt_kind"] == "admitted"
    assert item["attempt_outcome"] is None
    assert item["result_assessment"] is None
    assert item["run_failed"] is False


def test_update_revalidates_the_stance(tmp_path: Path) -> None:
    world = _admit(tmp_path)
    _resolve(world, "failed", "The run crashed.")
    draft = _create(world, _citation(world))
    citation = _citation(world, "opposes")
    body = {
        "statement": "Revised.",
        "evidence": [citation],
        "expected_revision": draft["revision"],
    }
    with pytest.raises(Conflict, match=REFUSED):
        world["records"].update_conclusion(
            world["owner"], draft["id"], "upd", canonical_digest(body), body
        )
    with TestClient(create_app(world["state"])) as client:
        response = client.put(
            f"/conclusions/{draft['id']}",
            json=body,
            headers={"Authorization": f"Bearer {_token(world)}", "Idempotency-Key": "upd2"},
        )
    assert response.status_code == 409
    again = _command(
        world["records"].update_conclusion,
        world["owner"],
        draft["id"],
        payload=body | {"evidence": [_citation(world)]},
    )
    assert _item(again)["stance"] is None and _item(again)["run_failed"] is True


def test_update_accepts_a_stance_on_a_valid_negative_result(tmp_path: Path) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    _assess(world, "negative")
    draft = _create(world, _citation(world))
    updated = _command(
        world["records"].update_conclusion,
        world["owner"],
        draft["id"],
        payload={
            "statement": "Revised.",
            "evidence": [_citation(world, "opposes")],
            "expected_revision": draft["revision"],
        },
    )
    assert _item(updated)["stance"] == "opposes"
    assert _item(updated)["result_assessment"] == "negative"


def test_export_shows_the_same_fields(tmp_path: Path) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    _assess(world, "negative")
    created = _create(world, _citation(world, "opposes"))
    exported = _export(world)
    row = next(item for item in exported["conclusions"] if item["id"] == created["id"])
    item = _item(row)
    assert (
        item["attempt_kind"],
        item["attempt_outcome"],
        item["result_assessment"],
        item["run_failed"],
        item["stance"],
    ) == ("admitted", "succeeded", "negative", False, "opposes")


def test_export_flags_a_failed_run_cited_as_context(tmp_path: Path) -> None:
    world = _admit(tmp_path)
    _resolve(world, "failed", "The run crashed.")
    created = _create(world, _citation(world))
    row = next(c for c in _export(world)["conclusions"] if c["id"] == created["id"])
    item = _item(row)
    assert item["attempt_outcome"] == "failed" and item["run_failed"] is True
    assert item["result_assessment"] is None


def test_legacy_attempt_evidence_reports_kind_and_outcome(tmp_path: Path) -> None:
    """A succeeded human-reported attempt shows its kind and outcome and may carry a stance."""
    from test_conclusion_decision import _draft

    _state, owner, records, reviewed, citation = _draft(tmp_path)
    item = reviewed["evidence"][0]
    assert item["attempt_kind"] == "legacy"
    assert item["attempt_outcome"] == "succeeded"
    assert item["result_assessment"] is None
    assert item["run_failed"] is False
    body = {"statement": "x", "evidence": [citation | {"stance": "supports"}]}
    created = _command(records.create_conclusion, owner, reviewed["experiment_id"], payload=body)
    assert created["evidence"][0]["stance"] == "supports"


# -- a conclusion that predates the rule can still be edited (ADR 0128 amendment) -------------


def _legacy_stance(world, conclusion_id: str, stance: str) -> None:
    """Store a stance on a failed run's link the way a conclusion written before the rule has it."""
    with sqlite3.connect(world["state"] / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE research_admitted_conclusion_evidence SET stance=? WHERE conclusion_id=?",
            (stance, conclusion_id),
        )


def _legacy_draft(tmp_path: Path):
    world = _admit(tmp_path)
    _resolve(world, "failed", "The run crashed.")
    cited = _citation(world)
    draft = _create(world, cited)
    _legacy_stance(world, draft["id"], "opposes")
    return world, cited, draft


def _update(world, draft: dict, evidence: list[dict], statement: str = "Revised."):
    body = {"statement": statement, "evidence": evidence, "expected_revision": draft["revision"]}
    return _command(world["records"].update_conclusion, world["owner"], draft["id"], payload=body)


def test_a_statement_only_edit_keeps_unchanged_legacy_evidence(tmp_path: Path) -> None:
    world, cited, draft = _legacy_draft(tmp_path)
    updated = _update(world, draft, [cited | {"stance": "opposes"}])
    assert updated["statement"] == "Revised." and updated["revision"] == draft["revision"] + 1
    assert _item(updated)["stance"] == "opposes" and _item(updated)["run_failed"] is True


def test_a_new_stanced_link_on_a_failed_run_is_still_refused(tmp_path: Path) -> None:
    world, cited, draft = _legacy_draft(tmp_path)
    fresh = _citation(world, "opposes")  # a new measurement of the same failed run
    body = {
        "statement": "Revised.",
        "evidence": [cited | {"stance": "opposes"}, fresh],
        "expected_revision": draft["revision"],
    }
    with pytest.raises(Conflict, match=REFUSED):
        world["records"].update_conclusion(
            world["owner"], draft["id"], "new", canonical_digest(body), body
        )


def test_a_changed_stance_on_a_legacy_link_is_still_refused(tmp_path: Path) -> None:
    world, cited, draft = _legacy_draft(tmp_path)
    body = {
        "statement": "Revised.",
        "evidence": [cited | {"stance": "supports"}],
        "expected_revision": draft["revision"],
    }
    with pytest.raises(Conflict, match=REFUSED):
        world["records"].update_conclusion(
            world["owner"], draft["id"], "chg", canonical_digest(body), body
        )


def test_a_link_that_was_stored_without_a_stance_cannot_gain_one(tmp_path: Path) -> None:
    world = _admit(tmp_path)
    _resolve(world, "failed", "The run crashed.")
    cited = _citation(world)
    draft = _create(world, cited)
    body = {
        "statement": "Revised.",
        "evidence": [cited | {"stance": "opposes"}],
        "expected_revision": draft["revision"],
    }
    with pytest.raises(Conflict, match=REFUSED):
        world["records"].update_conclusion(
            world["owner"], draft["id"], "gain", canonical_digest(body), body
        )
