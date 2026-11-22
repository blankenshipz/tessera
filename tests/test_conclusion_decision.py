"""Conclusion decision (continue/revise/reject) and per-evidence stance, human-recorded only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_conclusion_amendments import _reviewed_conclusion
from test_goal_to_decision import _command

from tessera_control import agent_research_results as results
from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.programs import omit_unset_decision
from tessera_control.store import (
    Conflict,
    NotFound,
    Store,
    _token_hash,
    canonical_digest,
    utc_now,
)


def _draft(tmp_path: Path, **extra):
    """A reviewed conclusion provides the attempt/measurement; create fresh drafts on it."""
    state, owner, records, reviewed = _reviewed_conclusion(tmp_path)
    citation = {
        "attempt_id": reviewed["evidence"][0]["attempt_id"],
        "measurement_ids": [reviewed["evidence"][0]["measurement_id"]],
    }
    return state, owner, records, reviewed, citation


def _create(records, owner, reviewed, citation, **fields):
    return _command(
        records.create_conclusion,
        owner,
        reviewed["experiment_id"],
        payload={
            "statement": "Draft interpretation.",
            "evidence": [citation],
            "design_revision": reviewed["design_revision"],
        }
        | fields,
    )


def _token(state: Path) -> str:
    return (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()


def test_create_with_decision_and_stance_is_shown_and_exported(tmp_path: Path) -> None:
    state, owner, records, reviewed, citation = _draft(tmp_path)
    created = _create(records, owner, reviewed, citation | {"stance": "opposes"}, decision="reject")
    assert created["decision"] == "reject"
    assert created["scientific_validity"] == "not_assessed"
    assert [row["stance"] for row in created["evidence"]] == ["opposes"]
    exported = records.get_experiment(owner, reviewed["experiment_id"])
    row = next(item for item in exported["conclusions"] if item["id"] == created["id"])
    assert row["decision"] == "reject"
    assert [item["stance"] for item in row["evidence"]] == ["opposes"]
    assert row["scientific_validity"] == "not_assessed"


def test_artifact_and_measurement_rows_store_their_own_stance(tmp_path: Path) -> None:
    state, _owner, _records, _reviewed, _citation = _draft(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        ExperimentStore._write_conclusion_evidence(
            db,
            "c-legacy",
            [
                ("a1", "art1", "art1", None, False, "supports"),
                ("a1", "m1", None, "m1", False, None),
            ],
        )
        rows = db.execute(
            "SELECT artifact_id,stance FROM research_conclusion_evidence WHERE conclusion_id=?"
            " ORDER BY attempt_id,artifact_id",
            ("c-legacy",),
        ).fetchall()
    assert rows == [(None, None), ("art1", "supports")]


def test_admitted_attempt_evidence_also_carries_stance(tmp_path: Path) -> None:
    state, owner, _records, _reviewed, _citation = _draft(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=OFF")
        ExperimentStore._write_conclusion_evidence(
            db, "c-admitted", [("adm1", "art1", "art1", None, True, "opposes")]
        )
        row = db.execute(
            "SELECT stance FROM research_admitted_conclusion_evidence "
            "WHERE conclusion_id='c-admitted'"
        ).fetchone()
        assert row["stance"] == "opposes"
        assert (
            db.execute(
                "SELECT count(*) FROM research_conclusion_evidence WHERE conclusion_id='c-admitted'"
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO research_admitted_conclusion_evidence "
                "(conclusion_id,attempt_id,artifact_id,measurement_id,stance) "
                "VALUES ('c','a','x',NULL,'maybe')"
            )


def test_omitted_decision_and_stance_stay_null_and_legacy_conclusion_works(tmp_path: Path) -> None:
    state, owner, records, reviewed, citation = _draft(tmp_path)
    # The fixture's own reviewed conclusion was created without either field.
    assert reviewed["decision"] is None
    assert [row["stance"] for row in reviewed["evidence"]] == [None]
    created = _create(records, owner, reviewed, citation)
    assert created["decision"] is None
    assert created["evidence"][0]["stance"] is None
    reviewed_again = _command(
        records.review_conclusion,
        owner,
        created["id"],
        payload={"expected_revision": created["revision"]},
    )
    assert reviewed_again["status"] == "reviewed" and reviewed_again["decision"] is None


def test_draft_update_changes_decision_and_stance_but_reviewed_row_is_immutable(
    tmp_path: Path,
) -> None:
    state, owner, records, reviewed, citation = _draft(tmp_path)
    created = _create(records, owner, reviewed, citation, decision="continue")
    updated = _command(
        records.update_conclusion,
        owner,
        created["id"],
        payload={
            "statement": "Revised interpretation.",
            "evidence": [citation | {"stance": "supports"}],
            "design_revision": reviewed["design_revision"],
            "decision": "revise",
            "expected_revision": created["revision"],
        },
    )
    assert updated["decision"] == "revise"
    assert [row["stance"] for row in updated["evidence"]] == ["supports"]
    cleared = _command(
        records.update_conclusion,
        owner,
        created["id"],
        payload={
            "statement": "Revised interpretation.",
            "evidence": [citation],
            "expected_revision": updated["revision"],
        },
    )
    assert cleared["decision"] is None and cleared["evidence"][0]["stance"] is None
    final = _command(
        records.review_conclusion,
        owner,
        created["id"],
        payload={"expected_revision": cleared["revision"]},
    )
    body = {
        "statement": "Revised interpretation.",
        "evidence": [citation],
        "decision": "reject",
        "expected_revision": final["revision"],
    }
    with pytest.raises(Conflict, match="Reviewed or stale"):
        records.update_conclusion(owner, created["id"], "late", canonical_digest(body), body)
    reread = records.get_experiment(owner, reviewed["experiment_id"])["conclusions"]
    assert next(c for c in reread if c["id"] == created["id"])["decision"] is None


def test_invalid_decision_and_stance_are_rejected_by_the_api(tmp_path: Path) -> None:
    state, owner, records, reviewed, citation = _draft(tmp_path)
    headers = {"Authorization": f"Bearer {_token(state)}"}
    url = f"/experiments/{reviewed['experiment_id']}/conclusions"
    base = {"statement": "x", "evidence": [citation]}
    with TestClient(create_app(state)) as client:
        bad_decision = client.post(
            url, json=base | {"decision": "accept"}, headers=headers | {"Idempotency-Key": "a"}
        )
        bad_stance = client.post(
            url,
            json={"statement": "x", "evidence": [citation | {"stance": "neutral"}]},
            headers=headers | {"Idempotency-Key": "b"},
        )
        extra = client.post(
            url, json=base | {"verdict": "yes"}, headers=headers | {"Idempotency-Key": "c"}
        )
        ok = client.post(
            url,
            json=base | {"decision": "continue"},
            headers=headers | {"Idempotency-Key": "d"},
        )
        got = client.put(
            f"/conclusions/{ok.json()['id']}",
            json=base | {"decision": "bogus", "expected_revision": 1},
            headers=headers | {"Idempotency-Key": "e"},
        )
    assert bad_decision.status_code == bad_stance.status_code == extra.status_code == 422
    assert got.status_code == 422
    assert ok.status_code == 201 and ok.json()["decision"] == "continue"


def test_agent_results_envelope_shows_decision(tmp_path: Path) -> None:
    state, owner, records, reviewed, citation = _draft(tmp_path)
    created = _create(records, owner, reviewed, citation, decision="revise")
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        payload = results._conclusions(db, owner, {"id": reviewed["experiment_id"]})
    by_id = {row["id"]: row for row in payload["conclusions"]}
    assert by_id[created["id"]]["decision"] == "revise"
    assert by_id[reviewed["id"]]["decision"] is None


def test_other_tenant_cannot_read_a_decision(tmp_path: Path) -> None:
    state, owner, records, reviewed, citation = _draft(tmp_path)
    created = _create(records, owner, reviewed, citation, decision="reject")
    other_token = "y" * 43
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("INSERT INTO organizations VALUES ('org_other','Other',?)", (utc_now(),))
        connection.execute(
            "INSERT INTO principals VALUES ('other','org_other','human',?,NULL,?)",
            (_token_hash(other_token), utc_now()),
        )
        connection.execute(
            """INSERT INTO credentials VALUES
            ('other_credential','other','local_owner','control',NULL,NULL,NULL,NULL,?,'other')""",
            (utc_now(),),
        )
    other = Store(state).authenticate(other_token)
    with pytest.raises(NotFound):
        records.get_experiment(other, reviewed["experiment_id"])
    with pytest.raises(NotFound):
        body = {"expected_revision": created["revision"]}
        records.review_conclusion(other, created["id"], "x", canonical_digest(body), body)


def test_program_basis_row_omits_unset_decision_and_keeps_digest(tmp_path: Path) -> None:
    state, owner, records, reviewed, citation = _draft(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        current = [dict(r) for r in db.execute("SELECT * FROM research_conclusions ORDER BY id")]
    assert all(row["decision"] is None for row in current)
    rows = [dict(row) for row in current]
    omit_unset_decision(rows)
    assert all("decision" not in row for row in rows)
    # The digest equals the one computed over the pre-change column set.
    legacy = [{k: v for k, v in row.items() if k != "decision"} for row in current]
    assert canonical_digest(rows) == canonical_digest(legacy)
    created = _create(records, owner, reviewed, citation, decision="continue")
    with_decision = [{"id": created["id"], "decision": "continue"}]
    omit_unset_decision(with_decision)
    assert with_decision[0]["decision"] == "continue"
