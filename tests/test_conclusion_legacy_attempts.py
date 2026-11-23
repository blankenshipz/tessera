"""Legacy (human-reported) attempts obey the same stance rule as admitted ones (ADR 0128)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_goal_to_decision import _command, _design
from test_result_checkpoints import _reconciled_manifest

from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.store import Conflict


def _legacy_attempt(tmp_path: Path, outcome: str) -> dict:
    """A human-reported attempt of ``outcome`` with one measurement, on a fresh experiment."""
    state, owner, _worker, grant, _lease, _manifest, _artifact = _reconciled_manifest(tmp_path)
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(v for v in experiment["design"]["variants"] if v["kind"] == "baseline")
    attempt = _command(
        records.record_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
            "provenance_kind": "human_reported_observation",
            "human_observation": f"A human recorded a synthetic {outcome} observation.",
            "outcome": outcome,
            "reason": f"Synthetic {outcome} outcome.",
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
    return {
        "state": state,
        "owner": owner,
        "records": records,
        "experiment_id": experiment["id"],
        "attempt_id": attempt["id"],
        "measurement_id": measurement["id"],
    }


def _citation(world: dict, stance: str | None = None) -> dict:
    cited = {"attempt_id": world["attempt_id"], "measurement_ids": [world["measurement_id"]]}
    return cited | ({"stance": stance} if stance else {})


def _create(world: dict, citation: dict) -> dict:
    return _command(
        world["records"].create_conclusion,
        world["owner"],
        world["experiment_id"],
        payload={"statement": "Interpretation.", "evidence": [citation]},
    )


def _post(world: dict, citation: dict, key: str):
    token = (world["state"] / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with TestClient(create_app(world["state"])) as client:
        return client.post(
            f"/experiments/{world['experiment_id']}/conclusions",
            json={"statement": "Interpretation.", "evidence": [citation]},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        )


@pytest.mark.parametrize("outcome", ("failed", "inconclusive"))
@pytest.mark.parametrize("stance", ("supports", "opposes"))
def test_legacy_non_success_attempt_cannot_carry_a_stance(tmp_path, outcome, stance) -> None:
    world = _legacy_attempt(tmp_path, outcome)
    citation = _citation(world, stance)
    with pytest.raises(Conflict, match="stance"):
        _create(world, citation)
    response = _post(world, citation, "refused")
    assert response.status_code == 409
    assert "stance" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    ("outcome", "run_failed"), (("failed", True), ("cancelled", True), ("inconclusive", False))
)
def test_legacy_non_success_attempt_cited_as_context(tmp_path, outcome, run_failed) -> None:
    world = _legacy_attempt(tmp_path, outcome)
    response = _post(world, _citation(world), "context")
    assert response.status_code == 201
    item = response.json()["evidence"][0]
    assert item["stance"] is None
    assert item["attempt_kind"] == "legacy"
    assert item["attempt_outcome"] == outcome
    assert item["result_assessment"] is None
    assert item["run_failed"] is run_failed


@pytest.mark.parametrize("stance", ("supports", "opposes"))
def test_legacy_succeeded_attempt_may_carry_a_stance(tmp_path, stance) -> None:
    world = _legacy_attempt(tmp_path, "succeeded")
    created = _create(world, _citation(world, stance))
    item = created["evidence"][0]
    assert item["stance"] == stance
    assert item["attempt_kind"] == "legacy"
    assert item["attempt_outcome"] == "succeeded"
    assert item["result_assessment"] is None
    assert item["run_failed"] is False
