"""Focused append-only reviewed-conclusion correction tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_goal_to_decision import _command, _design
from test_result_checkpoints import _reconciled_manifest

from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _token_hash,
    canonical_digest,
    utc_now,
)


def _reviewed_conclusion(tmp_path: Path) -> tuple[Path, dict, ExperimentStore, dict]:
    state, owner, _worker, grant, _lease, _manifest, _artifact = _reconciled_manifest(tmp_path)
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    attempt = _command(
        records.record_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
            "provenance_kind": "human_reported_observation",
            "human_observation": "A human recorded the bounded comparison observation.",
            "outcome": "inconclusive",
            "reason": "The reviewed interpretation remains conditional.",
        },
    )
    measurement = _command(
        records.record_measurement,
        owner,
        attempt["id"],
        payload={
            "expected_revision": attempt["revision"],
            "name": "comparison",
            "value": {"synthetic": True},
        },
    )
    conclusion = _command(
        records.create_conclusion,
        owner,
        experiment["id"],
        payload={
            "statement": "The comparison needs a later human correction if evidence changes.",
            "evidence": [{"attempt_id": attempt["id"], "measurement_ids": [measurement["id"]]}],
        },
    )
    return (
        state,
        owner,
        records,
        _command(
            records.review_conclusion,
            owner,
            conclusion["id"],
            payload={"expected_revision": conclusion["revision"]},
        ),
    )


def _amendment_body(conclusion: dict, relation: str) -> dict:
    return {
        "expected_revision": conclusion["revision"],
        "expected_design_revision": conclusion["design_revision"],
        "relation": relation,
        "reason": f"A human records the explicit {relation} relationship without acceptance.",
    }


def _authority_counts(state: Path) -> tuple[int, int, int]:
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("execution_grants", "execution_leases", "external_operations")
        )


def test_reviewed_conclusions_append_supersede_and_retract_lineage_without_authority(
    tmp_path: Path,
) -> None:
    state, owner, records, superseded = _reviewed_conclusion(tmp_path)
    original = dict(superseded)
    before_authority = _authority_counts(state)
    body = _amendment_body(superseded, "supersedes")
    status, response = records.amend_conclusion(
        owner, superseded["id"], "supersede", canonical_digest(body), body
    )
    assert status == 201
    assert response["original"]["status"] == "reviewed"
    assert response["original"]["revision"] == original["revision"]
    assert response["original"]["statement"] == original["statement"]
    assert response["original"]["evidence"] == original["evidence"]
    assert response["original"]["reviewed_at"] == original["reviewed_at"]
    assert response["original"]["reviewed_by_principal_id"] == original["reviewed_by_principal_id"]
    amendment = response["amendment"]
    assert amendment["relation"] == "supersedes"
    assert amendment["actor_principal_id"] == owner["id"]
    assert amendment["expected_design_revision"] == original["design_revision"]
    assert amendment["scientific_validity"] == "not_assessed"
    assert _authority_counts(state) == before_authority

    # A second reviewed conclusion proves that retraction has the same immutable,
    # human-only lineage behavior through the authenticated API route.
    duplicate = _command(
        records.create_conclusion,
        owner,
        superseded["experiment_id"],
        payload={
            "statement": "A second conclusion may be retracted without deleting evidence.",
            "evidence": [
                {
                    "attempt_id": superseded["evidence"][0]["attempt_id"],
                    "measurement_ids": [superseded["evidence"][0]["measurement_id"]],
                }
            ],
            "design_revision": superseded["design_revision"],
        },
    )
    reviewed = _command(
        records.review_conclusion,
        owner,
        duplicate["id"],
        payload={"expected_revision": duplicate["revision"]},
    )
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with TestClient(create_app(state)) as client:
        result = client.post(
            f"/conclusions/{reviewed['id']}/amendments",
            json=_amendment_body(reviewed, "retracts"),
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "retract"},
        )
    assert result.status_code == 201, result.text
    assert result.json()["amendment"]["relation"] == "retracts"
    exported = ExperimentStore(state).get_experiment(owner, superseded["experiment_id"])
    reopened = {item["id"]: item for item in exported["conclusions"]}
    assert reopened[superseded["id"]]["lineage"] == [amendment]
    assert reopened[reviewed["id"]]["lineage"][0]["relation"] == "retracts"
    assert {item["relation"] for item in exported["conclusion_amendments"]} == {
        "supersedes",
        "retracts",
    }
    assert _authority_counts(state) == before_authority


def test_conclusion_amendment_rejects_stale_conflicting_nonhuman_and_other_tenant_actions(
    tmp_path: Path,
) -> None:
    state, owner, records, conclusion = _reviewed_conclusion(tmp_path)
    body = _amendment_body(conclusion, "supersedes")
    with pytest.raises(Conflict, match="revision"):
        stale = body | {"expected_revision": body["expected_revision"] - 1}
        records.amend_conclusion(owner, conclusion["id"], "stale", canonical_digest(stale), stale)
    with pytest.raises(Conflict, match="design snapshot"):
        wrong_design = body | {"expected_design_revision": body["expected_design_revision"] + 1}
        records.amend_conclusion(
            owner, conclusion["id"], "wrong-design", canonical_digest(wrong_design), wrong_design
        )
    first = records.amend_conclusion(owner, conclusion["id"], "once", canonical_digest(body), body)
    assert (
        records.amend_conclusion(owner, conclusion["id"], "once", canonical_digest(body), body)
        == first
    )
    changed = body | {"relation": "retracts"}
    with pytest.raises(Conflict):
        records.amend_conclusion(
            owner, conclusion["id"], "once", canonical_digest(changed), changed
        )
    with pytest.raises(Conflict, match="revision"):
        records.amend_conclusion(
            owner, conclusion["id"], "conflict", canonical_digest(changed), changed
        )

    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    project_id = records.get_experiment(owner, conclusion["experiment_id"])["experiment"][
        "project_id"
    ]
    credential = {
        "kind": "agent",
        "audience": "control",
        "project_id": project_id,
        "expires_at": expires_at,
    }
    Store(state).issue_credential(
        owner, "amendment-agent", canonical_digest(credential), credential, "a" * 43
    )
    with pytest.raises(Forbidden, match="Human review"):
        records.amend_conclusion(
            Store(state).authenticate("a" * 43),
            conclusion["id"],
            "agent",
            canonical_digest(body),
            body,
        )
    other_token = "z" * 43
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
    with pytest.raises(NotFound):
        records.amend_conclusion(
            Store(state).authenticate(other_token),
            conclusion["id"],
            "other-tenant",
            canonical_digest(body),
            body,
        )
