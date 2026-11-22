"""A researcher's one-time result assessment of a succeeded admitted attempt."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_goal_to_decision import _command, _design
from test_input_delivery import _delivery

from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _token_hash,
    canonical_digest,
    utc_now,
)

RATIONALE = "The intervention matched the baseline within noise."


def _admit(tmp_path: Path):
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    admitted = _command(
        records.admit_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
            "expected_design_revision": experiment["current_design_revision"],
            "grant_id": grant["id"],
        },
    )
    world = dict(
        state=state,
        execution=execution,
        owner=owner,
        worker=worker,
        grant=grant,
        lease=lease,
        records=records,
        experiment=experiment,
        admitted=admitted,
    )
    return world


def _current(world) -> dict:
    found = world["records"].get_experiment(world["owner"], world["experiment"]["id"])
    return found["admitted_attempts"][0]


def _succeed(world) -> dict:
    """Drive the attempt to a confirmed exit and resolve it as succeeded."""
    execution, worker, lease = world["execution"], world["worker"], world["lease"]
    package = RuntimePackageStore(world["state"]).execution_package(
        worker, lease["id"], "assess-package", "assess-package", lease["epoch"]
    )[1]
    operation_id = package["operation"]["operation_id"]
    receipt = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "provider_kind": "fake",
        "provider_version": "1",
        "provider_id": "synthetic-assess-provider",
        "receipt_payload": {"synthetic": True},
        "receipt_digest": canonical_digest({"synthetic": True}),
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "termination_confirmed": True,
    }
    _command(execution.report_external_operation_receipt, worker, operation_id, payload=receipt)
    _command(
        execution.report_execution_observation,
        worker,
        lease["id"],
        payload={
            "expected_epoch": lease["epoch"],
            "observed_state": "exited",
            "observed_at": receipt["observed_at"],
            "provider_id": receipt["provider_id"],
            "termination_confirmed": True,
            "operation_id": operation_id,
        },
    )
    return _resolve(world, "succeeded", None)


def _resolve(world, outcome: str, reason: str | None) -> dict:
    return _command(
        world["records"].resolve_admitted_attempt,
        world["owner"],
        world["admitted"]["id"],
        payload={
            "expected_revision": _current(world)["revision"],
            "outcome": outcome,
            "reason": reason,
        },
    )


def _assess(world, assessment="negative", **overrides):
    payload = {
        "expected_revision": _current(world)["revision"],
        "assessment": assessment,
        "rationale": RATIONALE,
    } | overrides
    return world["records"].assess_attempt_result(
        world["owner"],
        world["admitted"]["id"],
        f"assess-{len(str(payload))}-{utc_now()}",
        canonical_digest(payload),
        payload,
    )


@pytest.mark.parametrize("assessment", ("positive", "negative", "inconclusive"))
def test_succeeded_attempt_can_be_assessed_without_relabelling(tmp_path, assessment) -> None:
    world = _admit(tmp_path)
    resolved = _succeed(world)
    assert resolved["result_assessment"] is None
    assert _current(world)["result_assessment"] is None
    status, result = _assess(world, assessment)
    assert status == 200
    value = result["result_assessment"]
    assert value["assessment"] == assessment and value["rationale"] == RATIONALE
    assert value["decided_by_principal_id"] == world["owner"]["id"] and value["decided_at"]
    assert set(value) == {"assessment", "rationale", "decided_at", "decided_by_principal_id"}
    after = _current(world)
    assert after["result_assessment"] == value
    # Nothing is relabelled and no validity or approval is inferred.
    assert after["outcome"] == "succeeded" and after["state"] == "succeeded"
    assert after["revision"] == resolved["revision"]
    assert after["scientific_validity"] == "not_assessed"
    token = (world["state"] / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with TestClient(create_app(world["state"])) as client:
        response = client.get(
            f"/experiments/{world['experiment']['id']}/export",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    exported = response.json()
    assert exported["admitted_attempts"][0]["result_assessment"] == value


@pytest.mark.parametrize("outcome", ("failed", "cancelled", "unknown"))
def test_operational_non_success_is_not_a_negative_result(tmp_path, outcome) -> None:
    world = _admit(tmp_path)
    _resolve(world, outcome, "Recorded for the assessment refusal test.")
    with pytest.raises(Conflict, match="not a negative result"):
        _assess(world)
    assert _current(world)["result_assessment"] is None


def test_unresolved_attempt_is_refused(tmp_path) -> None:
    world = _admit(tmp_path)
    with pytest.raises(Conflict, match="not resolved"):
        _assess(world)


def test_second_assessment_and_stale_revision_are_refused(tmp_path) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    with pytest.raises(Conflict, match="no longer current"):
        _assess(world, expected_revision=_current(world)["revision"] + 1)
    _assess(world, "inconclusive")
    with pytest.raises(Conflict, match="already has a result assessment"):
        _assess(world, "positive")
    assert _current(world)["result_assessment"]["assessment"] == "inconclusive"


def test_replay_returns_same_response_and_writes_once(tmp_path) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    payload = {
        "expected_revision": _current(world)["revision"],
        "assessment": "negative",
        "rationale": RATIONALE,
    }
    args = (world["owner"], world["admitted"]["id"], "assess-replay", canonical_digest(payload))
    first = world["records"].assess_attempt_result(*args, payload)
    assert world["records"].assess_attempt_result(*args, payload) == first
    with sqlite3.connect(world["state"] / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT count(*) FROM research_attempt_result_assessments"
        ).fetchone() == (1,)
        events = db.execute(
            "SELECT payload_json FROM events WHERE event_type='experiment.attempt_result_assessed'"
        ).fetchall()
    assert len(events) == 1
    assert '"assessment": "negative"' in events[0][0] or '"assessment":"negative"' in events[0][0]
    assert "not_assessed" in events[0][0]


def test_rationale_is_required_and_assessment_is_closed_set(tmp_path) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    for bad in ({"rationale": "  "}, {"assessment": "approved"}):
        with pytest.raises(Exception, match="Invalid experiment request"):
            _assess(world, **bad)
    assert _current(world)["result_assessment"] is None


def test_immutable_triggers(tmp_path) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    _assess(world)
    with sqlite3.connect(world["state"] / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            db.execute("UPDATE research_attempt_result_assessments SET assessment='positive'")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            db.execute("DELETE FROM research_attempt_result_assessments")


def test_agents_workers_and_other_tenants_are_refused(tmp_path) -> None:
    world = _admit(tmp_path)
    _succeed(world)
    state = world["state"]
    payload = {
        "expected_revision": _current(world)["revision"],
        "assessment": "negative",
        "rationale": RATIONALE,
    }
    with pytest.raises(Forbidden):
        world["records"].assess_attempt_result(
            world["worker"], world["admitted"]["id"], "worker", canonical_digest(payload), payload
        )
    agent_token, other_token = "a" * 43, "z" * 43
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        parent = db.execute(
            "SELECT id FROM credentials WHERE principal_id=?", (world["owner"]["id"],)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO principals VALUES ('agent_p',?,'agent',?,NULL,?)",
            (world["owner"]["org_id"], _token_hash(agent_token), utc_now()),
        )
        db.execute(
            """INSERT INTO credentials VALUES
            ('agent_credential','agent_p','agent','control',?,?,?,NULL,?,?)""",
            (
                world["grant"]["project_id"],
                parent,
                (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                utc_now(),
                world["owner"]["id"],
            ),
        )
        db.execute("INSERT INTO organizations VALUES ('org_other','Other',?)", (utc_now(),))
        db.execute(
            "INSERT INTO principals VALUES ('other','org_other','human',?,NULL,?)",
            (_token_hash(other_token), utc_now()),
        )
        db.execute(
            """INSERT INTO credentials VALUES
            ('other_credential','other','local_owner','control',NULL,NULL,NULL,NULL,?,'other')""",
            (utc_now(),),
        )
    path = f"/experiment-admitted-attempts/{world['admitted']['id']}/result-assessment"
    client = TestClient(create_app(state))

    def post(token, key):
        return client.post(
            path, json=payload, headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key}
        )

    assert post(agent_token, "agent-key").status_code == 403
    assert post(other_token, "other-key").status_code == 404
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    ok = post(owner_token, "owner-key")
    assert ok.status_code == 200 and ok.json()["result_assessment"]["assessment"] == "negative"
    assert post(owner_token, "owner-key").json() == ok.json()
    with pytest.raises(NotFound):
        other = Store(state).authenticate(other_token)
        world["records"].assess_attempt_result(
            other, world["admitted"]["id"], "other-store", canonical_digest(payload), payload
        )


def test_legacy_resolution_rows_are_unaffected(tmp_path) -> None:
    world = _admit(tmp_path)
    resolved = _resolve(world, "unknown", "Legacy-shaped human resolution.")
    after = _current(world)
    assert after["human_outcome"] == resolved["human_outcome"]
    assert after["result_assessment"] is None
    with sqlite3.connect(world["state"] / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT count(*) FROM research_attempt_result_assessments"
        ).fetchone() == (0,)
