"""Focused durable goal-to-decision ledger tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_input_delivery import _delivery
from test_result_checkpoints import _reconciled_manifest

from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.experiments import (
    ExperimentStore,
    create_v25_experiment_schema,
    create_v26_experiment_evidence_retention_schema,
)
from tessera_control.external_operations import (
    create_external_operation_receipt_schema,
    create_external_operation_schema,
    current_operation_reconciliation,
)
from tessera_control.migrations import MIGRATIONS, migrate_v27_to_v28, migrate_v30_to_v31
from tessera_control.project_artifact_retention import ProjectArtifactRetentionStore
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.recovery import backup_state, restore_state
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _token_hash,
    canonical_digest,
    initialize_state,
    migrate_state,
    utc_now,
)


def _command(method, principal, *args, payload):
    status, response = method(
        principal,
        *args,
        f"goal-to-decision-{method.__name__}-{len(str(args))}-{utc_now()}",
        canonical_digest(payload),
        payload,
    )
    assert status in {200, 201}
    return response


def _insert_operation(
    connection: sqlite3.Connection, owner, grant, lease, operation_id: str
) -> None:
    now = utc_now()
    connection.execute(
        """INSERT INTO external_operations
        (operation_id,org_id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,
         package_digest,adapter_version,profile_version,docker_lookup_key,request_digest,
         retry_class,desired_state,observed_state,provider_id,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,'development-local-cpu-v1','local-cpu-v1',?, ?,
        'manual_reconciliation','admitted','exited',?,?,?)""",
        (
            operation_id,
            owner["org_id"],
            grant["project_id"],
            grant["task_id"],
            grant["id"],
            lease["id"],
            lease["worker_principal_id"],
            lease["epoch"],
            "a" * 64,
            f"lookup-{operation_id}",
            "b" * 64,
            f"fake-{operation_id}",
            now,
            now,
        ),
    )
    connection.execute(
        "UPDATE execution_observations SET operation_id=? WHERE lease_id=?",
        (operation_id, lease["id"]),
    )


def _operation(state: Path, owner, grant, lease, operation_id: str) -> None:
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        _insert_operation(connection, owner, grant, lease, operation_id)


def _design(revision: int | None = None, grant=None):
    body = {
        "goal": "Reduce synthetic processing time.",
        "success_criterion": "A human compares retained measurements to the baseline.",
        "protocol": "Record the baseline and one bounded intervention.",
        "variants": [
            {
                "kind": "baseline",
                "name": "baseline",
                "method_digest": "a" * 64,
                "input_digest": "b" * 64,
                "metadata": {"synthetic": True},
            },
            {
                "kind": "intervention",
                "name": "intervention",
                "method_digest": "c" * 64,
                "input_digest": "d" * 64,
                "metadata": {"synthetic": True},
            },
        ],
    }
    if revision is not None:
        body["expected_revision"] = revision
    if grant is not None:
        for variant in body["variants"]:
            variant["execution_task_id"] = grant["task_id"]
            variant["execution_plan_digest"] = grant["plan_digest"]
    return body


def test_goal_design_attempt_evidence_conclusion_and_reopen_chain(tmp_path: Path) -> None:
    state, owner, _worker, grant, lease, manifest, artifact = _reconciled_manifest(tmp_path)
    _operation(state, owner, grant, lease, manifest["operation_id"])
    records = ExperimentStore(state)
    created = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    assert {item["kind"] for item in created["design"]["variants"]} == {
        "baseline",
        "intervention",
    }
    revised = _command(records.revise_design, owner, created["id"], payload=_design(1, grant))
    assert revised["current_design_revision"] == 2
    baseline = next(item for item in revised["design"]["variants"] if item["kind"] == "baseline")
    attempt = _command(
        records.record_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
            "provenance_kind": "admitted_operation",
            "operation_id": manifest["operation_id"],
            "outcome": "succeeded",
        },
    )
    with pytest.raises(Conflict, match="linked to the cited attempt"):
        _command(
            records.create_conclusion,
            owner,
            created["id"],
            payload={
                "statement": "Unlinked evidence is not acceptable.",
                "evidence": [{"attempt_id": attempt["id"], "artifact_ids": [artifact["id"]]}],
            },
        )
    linked = _command(
        records.link_attempt_artifacts,
        owner,
        attempt["id"],
        payload={"expected_revision": attempt["revision"], "artifact_ids": [artifact["id"]]},
    )
    measurement = _command(
        records.record_measurement,
        owner,
        attempt["id"],
        payload={
            "expected_revision": linked["revision"],
            "name": "synthetic duration",
            "value": {"seconds": 1},
            "artifact_id": artifact["id"],
        },
    )
    conclusion = _command(
        records.create_conclusion,
        owner,
        created["id"],
        payload={
            "statement": "The intervention merits follow-up, subject to human review.",
            "evidence": [
                {
                    "attempt_id": attempt["id"],
                    "artifact_ids": [artifact["id"]],
                    "measurement_ids": [measurement["id"]],
                }
            ],
        },
    )
    reviewed = _command(
        records.review_conclusion,
        owner,
        conclusion["id"],
        payload={"expected_revision": conclusion["revision"]},
    )
    assert reviewed["scientific_validity"] == "not_assessed"
    with pytest.raises(Conflict, match="Reviewed"):
        _command(
            records.update_conclusion,
            owner,
            reviewed["id"],
            payload={
                "expected_revision": reviewed["revision"],
                "statement": "Mutating reviewed interpretation is forbidden.",
                "evidence": [{"attempt_id": attempt["id"], "artifact_ids": [artifact["id"]]}],
            },
        )
    reopened = ExperimentStore(state).get_experiment(owner, created["id"])
    assert reopened["attempts"][0]["id"] == attempt["id"]
    assert reopened["attempts"][0]["operation_id"] == manifest["operation_id"]
    assert reopened["attempts"][0]["scientific_validity"] == "not_assessed"
    assert reopened["operations"][0]["operation_id"] == manifest["operation_id"]
    assert reopened["artifact_links"][0]["attempt_id"] == attempt["id"]
    assert reopened["artifact_links"][0]["artifact_id"] == artifact["id"]
    assert reopened["artifacts"][0]["id"] == artifact["id"]
    assert reopened["measurements"][0]["id"] == measurement["id"]
    assert reopened["conclusions"][0]["status"] == "reviewed"
    assert reopened["conclusions"][0]["design_snapshot"]["revision"] == 2


def test_outcomes_tenant_revision_and_human_fences(tmp_path: Path) -> None:
    state, owner, _worker, grant, lease, manifest, _artifact = _reconciled_manifest(tmp_path)
    _operation(state, owner, grant, lease, manifest["operation_id"])
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    for outcome in ("failed", "cancelled", "inconclusive"):
        attempt = _command(
            records.record_attempt,
            owner,
            baseline["id"],
            payload={
                "expected_variant_revision": baseline["revision"],
                "provenance_kind": "human_reported_observation",
                "human_observation": f"Human recorded the synthetic {outcome} observation.",
                "outcome": outcome,
                "reason": f"Synthetic {outcome} outcome remains recorded.",
            },
        )
        assert attempt["outcome"] == outcome
        assert attempt["scientific_validity"] == "not_assessed"
    with pytest.raises(Conflict, match="revision"):
        _command(records.revise_design, owner, experiment["id"], payload=_design(2, grant))
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    credential = {
        "kind": "agent",
        "audience": "control",
        "project_id": grant["project_id"],
        "expires_at": expires_at,
    }
    Store(state).issue_credential(
        owner, "agent", canonical_digest(credential), credential, "a" * 43
    )
    with pytest.raises(Forbidden, match="Human review"):
        _command(
            ExperimentStore(state).create_experiment,
            Store(state).authenticate("a" * 43),
            grant["project_id"],
            payload=_design(),
        )
    assert [item["id"] for item in records.list_experiments(owner, grant["project_id"])] == [
        experiment["id"]
    ]
    with pytest.raises(StoreError, match="pagination"):
        records.list_experiments(owner, grant["project_id"], limit=201)
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
    other = Store(state).authenticate(other_token)
    with pytest.raises(NotFound):
        records.get_experiment(other, experiment["id"])
    client = TestClient(create_app(state))
    response = client.get(
        f"/experiments/{experiment['id']}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert response.status_code == 404
    response = client.get(
        f"/projects/{grant['project_id']}/experiments?offset=0&limit=100",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("state", ("unknown", "running"))
def test_attempt_rejects_nonterminal_or_unrelated_operations(tmp_path: Path, state: str) -> None:
    state_dir, owner, _worker, grant, lease, manifest, _artifact = _reconciled_manifest(tmp_path)
    _operation(state_dir, owner, grant, lease, manifest["operation_id"])
    records = ExperimentStore(state_dir)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE external_operations SET observed_state=? WHERE operation_id=?",
            (state, manifest["operation_id"]),
        )
        connection.execute(
            "UPDATE execution_observations SET observed_state=? WHERE operation_id=?",
            (state, manifest["operation_id"]),
        )
    body = {
        "expected_variant_revision": baseline["revision"],
        "provenance_kind": "admitted_operation",
        "operation_id": manifest["operation_id"],
        "outcome": "succeeded",
    }
    with pytest.raises(Conflict, match="not admitted"):
        _command(records.record_attempt, owner, baseline["id"], payload=body)
    with sqlite3.connect(state_dir / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE external_operations SET observed_state='exited',task_id=? WHERE operation_id=?",
            ("unrelated_task", manifest["operation_id"]),
        )
        connection.execute(
            "UPDATE execution_observations SET observed_state='exited' WHERE operation_id=?",
            (manifest["operation_id"],),
        )
    with pytest.raises(Conflict, match="not admitted"):
        _command(records.record_attempt, owner, baseline["id"], payload=body)


def test_attempt_provenance_admission_human_replay_reuse_and_cancellation(tmp_path: Path) -> None:
    state, owner, _worker, grant, lease, manifest, _artifact = _reconciled_manifest(tmp_path)
    _operation(state, owner, grant, lease, manifest["operation_id"])
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    intervention = next(
        item for item in experiment["design"]["variants"] if item["kind"] == "intervention"
    )
    body = {
        "expected_variant_revision": baseline["revision"],
        "provenance_kind": "admitted_operation",
        "operation_id": manifest["operation_id"],
        "outcome": "succeeded",
    }
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE external_operations SET desired_state='cancel_requested' WHERE operation_id=?",
            (manifest["operation_id"],),
        )
    status, attempt = records.record_attempt(
        owner, baseline["id"], "attempt-replay", canonical_digest(body), body
    )
    assert status == 201
    assert attempt["provenance_kind"] == "admitted_operation"
    assert attempt["design_revision"] == baseline["design_revision"]
    assert attempt["operation_observed_state"] == "exited"
    assert attempt["termination_status"] == "confirmed"
    assert attempt["scientific_validity"] == "not_assessed"
    assert records.record_attempt(
        owner, baseline["id"], "attempt-replay", canonical_digest(body), body
    ) == (status, attempt)
    conflicting = body | {"outcome": "inconclusive", "reason": "Different retry payload."}
    with pytest.raises(Conflict):
        records.record_attempt(
            owner, baseline["id"], "attempt-replay", canonical_digest(conflicting), conflicting
        )
    with pytest.raises(Conflict, match="already bound"):
        _command(
            records.record_attempt,
            owner,
            intervention["id"],
            payload=body | {"expected_variant_revision": intervention["revision"]},
        )
    human = _command(
        records.record_attempt,
        owner,
        intervention["id"],
        payload={
            "expected_variant_revision": intervention["revision"],
            "provenance_kind": "human_reported_observation",
            "human_observation": (
                "A human recorded an instrument reading outside Tessera execution."
            ),
            "outcome": "inconclusive",
            "reason": "No execution observation is claimed.",
        },
    )
    assert human["operation_id"] is None
    assert human["operation_observed_state"] == "not_observed"
    assert human["termination_status"] == "not_applicable"
    # The first operation stays consumed. A cancellation request never changes its
    # recorded terminal observation into scientific or execution success.
    assert attempt["operation_desired_state"] == "cancel_requested"


def test_pre_execution_admitted_attempt_binds_package_operation_and_restart_accounting(
    tmp_path: Path,
) -> None:
    state, _execution, owner, worker, grant, lease = _delivery(tmp_path)
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    admission_body = {
        "expected_variant_revision": baseline["revision"],
        "expected_design_revision": experiment["current_design_revision"],
        "grant_id": grant["id"],
    }
    admitted = _command(records.admit_attempt, owner, baseline["id"], payload=admission_body)
    assert admitted["state"] == "admitted" and admitted["operation_id"] is None

    package_body = {
        "expected_epoch": lease["epoch"],
    }
    package = RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "admitted-package",
        canonical_digest(package_body),
        lease["epoch"],
    )[1]
    assert package["package"]["experiment"] == {
        "admitted_attempt_id": admitted["id"],
        "experiment_id": experiment["id"],
        "design_revision": 1,
        "variant_id": baseline["id"],
        "task_id": grant["task_id"],
        "plan_digest": grant["plan_digest"],
        "grant_id": grant["id"],
    }
    assert package["operation"]["experiment_attempt_id"] == admitted["id"]
    assert package["operation"]["experiment_variant_id"] == baseline["id"]

    execution = ExecutionStore(state)
    for state_name in ("running", "unknown"):
        observation = {
            "expected_epoch": lease["epoch"],
            "operation_id": package["operation"]["operation_id"],
            "observed_state": state_name,
            "observed_at": datetime.now(UTC).isoformat(),
            "provider_id": "synthetic-admission-container",
            "termination_confirmed": False,
        }
        _command(
            execution.report_execution_observation,
            worker,
            lease["id"],
            payload=observation,
        )
    # A package refresh retains the same operation and does not claim a restart.
    refreshed = RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "admitted-package-refresh",
        canonical_digest(package_body),
        lease["epoch"],
    )[1]
    assert refreshed["operation"]["operation_id"] == package["operation"]["operation_id"]
    assert (
        records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]["operational_state"]
        == "unknown"
    )
    # A distinct, explicit restart intent retains that operation identity and
    # records restart_pending rather than admitting replacement work.
    replay_body = package_body | {"restart_intent": True}
    replay = RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "admitted-package-restart",
        canonical_digest(replay_body),
        lease["epoch"],
        restart_intent=True,
    )[1]
    assert replay["operation"]["operation_id"] == package["operation"]["operation_id"]
    exported = records.get_experiment(owner, experiment["id"])
    current = exported["admitted_attempts"][0]
    assert [item["state"] for item in current["state_history"]] == [
        "admitted",
        "preparing",
        "running",
        "unknown",
        "restart_pending",
    ]
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with TestClient(create_app(state)) as client:
        response = client.get(
            f"/experiments/{experiment['id']}/export",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    api_export = response.json()
    assert api_export["admitted_attempts"] == exported["admitted_attempts"]
    assert api_export["operations"][0]["experiment_attempt_id"] == admitted["id"]
    assert api_export["operations"][0]["experiment_id"] == experiment["id"]
    resolved = _command(
        records.resolve_admitted_attempt,
        owner,
        admitted["id"],
        payload={
            "expected_revision": current["revision"],
            "outcome": "unknown",
            "reason": "The synthetic worker restarted before a terminal result was attributable.",
        },
    )
    assert resolved["state"] == resolved["outcome"] == "unknown"
    final = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    assert final["state_history"][-1]["state"] == "unknown"
    _command(
        execution.report_execution_observation,
        worker,
        lease["id"],
        payload=observation | {"observed_at": datetime.now(UTC).isoformat()},
    )
    before_replay = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "admitted-package-restart",
        canonical_digest(replay_body),
        lease["epoch"],
        restart_intent=True,
    )
    assert records.get_experiment(owner, experiment["id"])["admitted_attempts"][0] == before_replay


def test_admitted_attempt_projects_polling_samples_without_growing_semantic_history(
    tmp_path: Path,
) -> None:
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
    package_body = {"expected_epoch": lease["epoch"]}
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "polling-package", canonical_digest(package_body), lease["epoch"]
    )[1]
    operation_id = package["operation"]["operation_id"]
    first_body = {
        "expected_epoch": lease["epoch"],
        "operation_id": operation_id,
        "observed_state": "running",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "polling-synthetic-provider",
        "termination_confirmed": False,
    }
    first_key = "polling-observation-replay"
    first_digest = canonical_digest(first_body)
    first_status, first = execution.report_execution_observation(
        worker, lease["id"], first_key, first_digest, first_body
    )
    assert execution.report_execution_observation(
        worker, lease["id"], first_key, first_digest, first_body
    ) == (first_status, first)
    with pytest.raises(Conflict, match="Idempotency key"):
        execution.report_execution_observation(
            worker,
            lease["id"],
            first_key,
            canonical_digest(first_body | {"observed_state": "unknown"}),
            first_body | {"observed_state": "unknown"},
        )
    latest = first
    for index in range(1_001):
        body = first_body | {"observed_at": datetime.now(UTC).isoformat()}
        _, latest = execution.report_execution_observation(
            worker,
            lease["id"],
            f"polling-observation-{index}",
            canonical_digest(body),
            body,
        )
    for state_name in ("unknown", "running", "exited"):
        body = first_body | {
            "observed_state": state_name,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        _, latest = execution.report_execution_observation(
            worker,
            lease["id"],
            f"polling-transition-{state_name}",
            canonical_digest(body),
            body,
        )
    detail = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    assert detail["id"] == admitted["id"]
    assert [entry["state"] for entry in detail["state_history"]] == [
        "admitted",
        "preparing",
        "running",
        "unknown",
        "running",
        "interrupted",
    ]
    assert detail["operational_observed_state"] == "exited"
    assert detail["operational_observed_at"] == latest["observed_at"]
    assert detail["operational_observation_cursor"] == f"observation:{latest['id']}"
    assert detail["operational_termination_confirmed"] is False
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_observations WHERE operation_id=?", (operation_id,)
            ).fetchone()[0]
            == 1_005
        )
        # Operational polling is retained separately and no longer floods the
        # labbook/event projection between semantic transitions.
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='execution.observation_reported'"
            ).fetchone()[0]
            == 4
        )
    exported = records.export_experiment(owner, experiment["id"])
    assert exported["admitted_attempts"] == [detail]


def test_admitted_human_outcome_is_immutable_against_late_observations_and_replay(
    tmp_path: Path,
) -> None:
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
    package_body = {"expected_epoch": lease["epoch"]}
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "outcome-package", canonical_digest(package_body), lease["epoch"]
    )[1]
    current = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    resolution_body = {
        "expected_revision": current["revision"],
        "outcome": "unknown",
        "reason": "No terminal observation was available when the human resolved it.",
    }
    status, resolved = records.resolve_admitted_attempt(
        owner,
        admitted["id"],
        "immutable-unknown",
        canonical_digest(resolution_body),
        resolution_body,
    )
    assert status == 200 and resolved["outcome"] == "unknown"
    assert records.resolve_admitted_attempt(
        owner,
        admitted["id"],
        "immutable-unknown",
        canonical_digest(resolution_body),
        resolution_body,
    ) == (status, resolved)
    with pytest.raises(Conflict, match="already resolved"):
        _command(
            records.resolve_admitted_attempt,
            owner,
            admitted["id"],
            payload=resolution_body
            | {
                "expected_revision": resolved["revision"],
                "outcome": "failed",
                "reason": "Conflict.",
            },
        )
    observation = {
        "expected_epoch": lease["epoch"],
        "operation_id": package["operation"]["operation_id"],
        "observed_state": "running",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "late-synthetic-provider",
        "termination_confirmed": False,
    }
    _command(execution.report_execution_observation, worker, lease["id"], payload=observation)
    exported = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    assert exported["outcome"] == "unknown"
    assert exported["human_outcome"]["outcome"] == "unknown"
    assert exported["operational_state"] == "running"
    # Neither a fresh package read nor an idempotent replay rewrites the newer
    # operational projection with the human unknown outcome.
    for key in ("outcome-package", "outcome-refresh"):
        RuntimePackageStore(state).execution_package(
            worker, lease["id"], key, canonical_digest(package_body), lease["epoch"]
        )
        assert records.get_experiment(owner, experiment["id"])["admitted_attempts"][0] == exported


def test_admitted_success_requires_package_and_confirmed_exit(tmp_path: Path) -> None:
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
    success = {"expected_revision": admitted["revision"], "outcome": "succeeded", "reason": None}
    with pytest.raises(Conflict, match="bound package"):
        _command(records.resolve_admitted_attempt, owner, admitted["id"], payload=success)
    package_body = {"expected_epoch": lease["epoch"]}
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "success-package", canonical_digest(package_body), lease["epoch"]
    )[1]
    exited = {
        "expected_epoch": lease["epoch"],
        "operation_id": package["operation"]["operation_id"],
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "unconfirmed-synthetic-provider",
        "termination_confirmed": False,
    }
    _command(execution.report_execution_observation, worker, lease["id"], payload=exited)
    current = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    with pytest.raises(Conflict, match="confirmed attributed"):
        _command(
            records.resolve_admitted_attempt,
            owner,
            admitted["id"],
            payload=success | {"expected_revision": current["revision"]},
        )


def _admitted_operation_with_confirmed_sources(tmp_path: Path):
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
    body = {"expected_epoch": lease["epoch"]}
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "combined-observation-package", canonical_digest(body), lease["epoch"]
    )[1]
    observed_at = datetime.now(UTC)
    receipt = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "provider_kind": "fake",
        "provider_version": "1",
        "provider_id": "combined-synthetic-provider",
        "receipt_payload": {"synthetic": "combined-observation"},
        "receipt_digest": canonical_digest({"synthetic": "combined-observation"}),
        "observed_state": "exited",
        "observed_at": observed_at.isoformat(),
        "termination_confirmed": True,
    }
    _command(
        execution.report_external_operation_receipt,
        worker,
        package["operation"]["operation_id"],
        payload=receipt,
    )
    worker_exit = {
        "expected_epoch": lease["epoch"],
        "operation_id": package["operation"]["operation_id"],
        "observed_state": "exited",
        "observed_at": observed_at.isoformat(),
        "provider_id": receipt["provider_id"],
        "termination_confirmed": True,
    }
    _command(execution.report_execution_observation, worker, lease["id"], payload=worker_exit)
    return (
        state,
        execution,
        records,
        owner,
        worker,
        grant,
        lease,
        admitted,
        package["operation"]["operation_id"],
        receipt,
        worker_exit,
    )


def test_newer_receipt_unknown_blocks_admitted_success_after_confirmed_worker_exit(
    tmp_path: Path,
) -> None:
    (
        _state,
        execution,
        records,
        owner,
        worker,
        _grant,
        _lease,
        admitted,
        operation_id,
        receipt,
        _worker_exit,
    ) = _admitted_operation_with_confirmed_sources(tmp_path)
    newer_unknown = receipt | {
        "observed_state": "unknown",
        "observed_at": (
            datetime.fromisoformat(receipt["observed_at"]) + timedelta(milliseconds=1)
        ).isoformat(),
        "termination_confirmed": False,
    }
    _command(
        execution.report_external_operation_receipt, worker, operation_id, payload=newer_unknown
    )
    current = records.get_experiment(owner, admitted["experiment_id"])["admitted_attempts"][0]
    assert current["operational_state"] == current["operational_observed_state"] == "unknown"
    with pytest.raises(Conflict, match="confirmed attributed terminal observation"):
        _command(
            records.resolve_admitted_attempt,
            owner,
            admitted["id"],
            payload={
                "expected_revision": current["revision"],
                "outcome": "succeeded",
                "reason": None,
            },
        )


def test_interleaved_delayed_source_observations_use_observed_time_not_arrival_order(
    tmp_path: Path,
) -> None:
    (
        _state,
        execution,
        records,
        owner,
        worker,
        _grant,
        lease,
        admitted,
        operation_id,
        receipt,
        worker_exit,
    ) = _admitted_operation_with_confirmed_sources(tmp_path)
    base = datetime.fromisoformat(receipt["observed_at"])
    # The provider's later confirmation arrives before this worker sample, but
    # the worker sample is accepted in its own source order and cannot overwrite
    # the provider's newer observed-time state.
    provider_later = receipt | {"observed_at": (base + timedelta(milliseconds=3)).isoformat()}
    _command(
        execution.report_external_operation_receipt, worker, operation_id, payload=provider_later
    )
    delayed_worker_exit = worker_exit | {
        "observed_at": (base + timedelta(milliseconds=2)).isoformat()
    }
    _command(
        execution.report_execution_observation, worker, lease["id"], payload=delayed_worker_exit
    )
    current = records.get_experiment(owner, admitted["experiment_id"])["admitted_attempts"][0]
    assert current["operational_observed_at"] == provider_later["observed_at"].replace(
        "+00:00", "Z"
    )
    resolved = _command(
        records.resolve_admitted_attempt,
        owner,
        admitted["id"],
        payload={"expected_revision": current["revision"], "outcome": "succeeded", "reason": None},
    )
    assert resolved["human_outcome"]["outcome"] == "succeeded"


def test_same_time_receipt_contradiction_is_fail_closed(tmp_path: Path) -> None:
    (
        _state,
        execution,
        records,
        owner,
        worker,
        _grant,
        _lease,
        admitted,
        operation_id,
        receipt,
        _worker_exit,
    ) = _admitted_operation_with_confirmed_sources(tmp_path)
    same_time_unknown = receipt | {"observed_state": "unknown", "termination_confirmed": False}
    _command(
        execution.report_external_operation_receipt, worker, operation_id, payload=same_time_unknown
    )
    current = records.get_experiment(owner, admitted["experiment_id"])["admitted_attempts"][0]
    assert current["operational_observed_state"] == "unknown"
    assert current["operational_observation_cursor"].startswith("reconciliation:")
    with _connect(_state / "tessera.sqlite3") as connection:
        reconciliation = current_operation_reconciliation(connection, operation_id)
    assert reconciliation is not None
    assert reconciliation["source_order"] == ["receipt", "worker"]
    assert reconciliation["contradiction_state"] == "same_time_contradiction"
    with pytest.raises(Conflict, match="confirmed attributed terminal observation"):
        _command(
            records.resolve_admitted_attempt,
            owner,
            admitted["id"],
            payload={
                "expected_revision": current["revision"],
                "outcome": "succeeded",
                "reason": None,
            },
        )


def test_newer_worker_running_blocks_reconciliation_and_exposure_release(tmp_path: Path) -> None:
    (
        _state,
        execution,
        _records,
        owner,
        worker,
        grant,
        lease,
        _admitted,
        _operation_id,
        receipt,
        worker_exit,
    ) = _admitted_operation_with_confirmed_sources(tmp_path)
    running = worker_exit | {
        "observed_state": "running",
        "observed_at": (
            datetime.fromisoformat(receipt["observed_at"]) + timedelta(milliseconds=1)
        ).isoformat(),
        "termination_confirmed": False,
    }
    _command(execution.report_execution_observation, worker, lease["id"], payload=running)
    with pytest.raises(Conflict, match="Current operation reconciliation"):
        _command(
            execution.reconcile_execution_grant,
            owner,
            grant["id"],
            payload={
                "expected_revision": grant["revision"],
                "settlement_cents": 0,
                "provider_terminated": True,
            },
        )
    assert ExecutionStore(_state).get_execution_grant(owner, grant["id"])["status"] == "active"
    assert (
        execution.get_project_execution(owner, grant["project_id"])["held_cents"]
        == grant["max_exposure_cents"]
    )


def test_v35_receipt_sequence_migration_preserves_existing_receipt_state(tmp_path: Path) -> None:
    (
        state,
        _execution,
        _records,
        _owner,
        _worker,
        _grant,
        _lease,
        _admitted,
        operation_id,
        _receipt,
        _worker_exit,
    ) = _admitted_operation_with_confirmed_sources(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "ALTER TABLE external_operation_receipts DROP COLUMN observation_sequence"
        )
        connection.execute("UPDATE schema_meta SET schema_version=34")
    assert migrate_state(state) == 35
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT observation_sequence FROM external_operation_receipts WHERE operation_id=?",
            (operation_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize("outcome", ("succeeded", "cancelled"))
def test_admitted_confirmed_exit_preserves_separate_operational_and_human_records(
    tmp_path: Path,
    outcome: str,
) -> None:
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
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "terminal-package", "terminal-package", lease["epoch"]
    )[1]
    operation_id = package["operation"]["operation_id"]
    receipt = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "provider_kind": "fake",
        "provider_version": "1",
        "provider_id": "synthetic-terminal-provider",
        "receipt_payload": {"synthetic": True},
        "receipt_digest": canonical_digest({"synthetic": True}),
        "observed_state": "running",
        "observed_at": datetime.now(UTC).isoformat(),
        "termination_confirmed": False,
    }
    _command(execution.report_external_operation_receipt, worker, operation_id, payload=receipt)
    if outcome == "cancelled":
        execution.revoke_execution_grant(owner, grant["id"], "revoke", "revoke", 1)
        reported = _command(
            execution.report_external_operation_receipt, worker, operation_id, payload=receipt
        )
        assert reported["observed_state"] == "running"
        with pytest.raises(Conflict, match="epoch"):
            _command(
                execution.report_external_operation_receipt,
                worker,
                operation_id,
                payload=receipt | {"expected_epoch": lease["epoch"] + 1},
            )
    confirmed = receipt | {
        "observed_state": "exited",
        "termination_confirmed": True,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    _command(execution.report_external_operation_receipt, worker, operation_id, payload=confirmed)
    _command(
        execution.report_execution_observation,
        worker,
        lease["id"],
        payload={
            name: confirmed[name]
            for name in (
                "expected_epoch",
                "observed_state",
                "observed_at",
                "provider_id",
                "termination_confirmed",
            )
        }
        | {"operation_id": operation_id},
    )
    current = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    assert current["state"] == current["operational_state"] == "interrupted"
    assert current["outcome"] is current["human_outcome"] is None
    assert current["operational_termination_confirmed"] is True
    resolved = _command(
        records.resolve_admitted_attempt,
        owner,
        admitted["id"],
        payload={
            "expected_revision": current["revision"],
            "outcome": outcome,
            "reason": "Human terminal bookkeeping.",
        },
    )
    final = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    assert final["human_outcome"] == resolved["human_outcome"]
    assert final["state"] == final["outcome"] == outcome
    assert final["operational_state"] == "interrupted"
    assert final["operational_observed_state"] == "exited"
    assert final["state_history"][-1]["state"] == outcome
    if outcome == "cancelled":
        adverse = confirmed | {
            "observed_state": "unknown",
            "termination_confirmed": False,
            "observed_at": (datetime.now(UTC) + timedelta(milliseconds=1)).isoformat(),
        }
        _command(execution.report_external_operation_receipt, worker, operation_id, payload=adverse)
        late = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
        assert late["human_outcome"] == final["human_outcome"]
        assert late["operational_observed_state"] == "unknown"
        assert late["state_history"][-1]["observed_state"] == "unknown"


def test_revocation_records_original_adverse_observations_without_new_authority(
    tmp_path: Path,
) -> None:
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    package_body = {"expected_epoch": lease["epoch"]}
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "revoke-package", canonical_digest(package_body), lease["epoch"]
    )[1]
    _, revoked = execution.revoke_execution_grant(owner, grant["id"], "revoke", "revoke", 1)
    with pytest.raises(Conflict, match="not active"):
        execution.renew_execution_lease(worker, lease["id"], "renew", "renew", lease["epoch"])
    with pytest.raises(Conflict, match="not active"):
        RuntimePackageStore(state).execution_package(
            worker, lease["id"], "replacement", canonical_digest(package_body), lease["epoch"]
        )
    unknown = {
        "expected_epoch": lease["epoch"],
        "operation_id": package["operation"]["operation_id"],
        "observed_state": "unknown",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "revoke-synthetic-provider",
        "termination_confirmed": False,
    }
    reported = _command(
        execution.report_execution_observation, worker, lease["id"], payload=unknown
    )
    assert reported["desired_state"] == "cancel_requested"
    with pytest.raises(Conflict, match="Current operation reconciliation"):
        _command(
            execution.reconcile_execution_grant,
            owner,
            grant["id"],
            payload={
                "expected_revision": revoked["revision"],
                "settlement_cents": 0,
                "provider_terminated": True,
            },
        )
    confirmed = unknown | {
        "observed_state": "exited",
        "termination_confirmed": True,
        "observed_at": (datetime.now(UTC) + timedelta(milliseconds=1)).isoformat(),
    }
    _, observation = execution.report_execution_observation(
        worker, lease["id"], "revoked-confirmed", canonical_digest(confirmed), confirmed
    )
    assert observation["desired_state"] == "cancel_requested"
    running = unknown | {
        "observed_state": "running",
        "observed_at": (datetime.now(UTC) + timedelta(milliseconds=2)).isoformat(),
    }
    _command(execution.report_execution_observation, worker, lease["id"], payload=running)
    with pytest.raises(Conflict, match="Current operation reconciliation"):
        _command(
            execution.reconcile_execution_grant,
            owner,
            grant["id"],
            payload={
                "expected_revision": revoked["revision"],
                "settlement_cents": 0,
                "provider_terminated": True,
            },
        )
    cancellation = ExecutionStore(state).get_execution_grant(owner, grant["id"])["cancellation"]
    assert cancellation["state"] == "unresolved"
    assert (
        execution.get_project_execution(owner, grant["project_id"])["held_cents"]
        == grant["max_exposure_cents"]
    )


@pytest.mark.parametrize("outcome", ("unknown", "failed", "succeeded", "cancelled"))
def test_v32_resolution_migration_preserves_legacy_outcomes_without_operational_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    state, _execution, owner, _worker, grant, _lease = _delivery(tmp_path)
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
    _command(
        records.resolve_admitted_attempt,
        owner,
        admitted["id"],
        payload={
            "expected_revision": admitted["revision"],
            "outcome": "unknown",
            "reason": "Legacy state retained uncertainty.",
        },
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE research_experiment_admitted_attempt_resolutions")
        # Reconstruct actual v32 columns, not a relabeled v33 projection.
        for column in (
            "operational_state",
            "operational_observed_state",
            "operational_termination_confirmed",
            "operational_observation_cursor",
            "operational_observed_at",
        ):
            connection.execute(
                f"ALTER TABLE research_experiment_admitted_attempts DROP COLUMN {column}"
            )
        connection.execute(
            "UPDATE research_experiment_admitted_attempts SET state=?,outcome=? WHERE id=?",
            (outcome, outcome, admitted["id"]),
        )
        connection.execute(
            "UPDATE research_experiment_admitted_attempt_states SET state=? "
            "WHERE attempt_id=? AND sequence=2",
            (outcome, admitted["id"]),
        )
        connection.execute("UPDATE schema_meta SET schema_version=32")
        before = list(connection.iterdump())
    migrate = MIGRATIONS[32]

    def interrupted(connection):
        migrate(connection)
        raise RuntimeError("synthetic v33 interruption")

    monkeypatch.setitem(MIGRATIONS, 32, interrupted)
    with pytest.raises(RuntimeError, match="synthetic v33 interruption"):
        migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert list(connection.iterdump()) == before
    monkeypatch.setitem(MIGRATIONS, 32, migrate)
    assert migrate_state(state) == 35
    migrated = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    assert migrated["outcome"] == outcome
    assert migrated["human_outcome"]["outcome"] == outcome
    assert migrated["operational_state"] == "legacy_unverified"
    assert migrated["operational_observed_state"] == "legacy_unverified"
    assert migrated["operational_termination_confirmed"] is None
    assert migrate_state(state) == 35
    assert records.get_experiment(owner, experiment["id"])["admitted_attempts"][0] == migrated
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    schemas = []
    for directory in (state, fresh):
        with sqlite3.connect(directory / "tessera.sqlite3") as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            schemas.append(
                connection.execute(
                    "SELECT name,sql FROM sqlite_schema "
                    "WHERE name LIKE 'research_%admitted%' ORDER BY name"
                ).fetchall()
            )
    assert schemas[0] == schemas[1]
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_state(state, backup)
    restore_state(backup, restored)
    token = (restored / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    held_owner = Store(restored).authenticate(token)
    assert (
        ExperimentStore(restored).get_experiment(held_owner, experiment["id"])["admitted_attempts"][
            0
        ]
        == migrated
    )


def test_v33_observation_projection_migration_backfills_only_existing_latest_sample(
    tmp_path: Path,
) -> None:
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
    package_body = {"expected_epoch": lease["epoch"]}
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "migration-package", canonical_digest(package_body), lease["epoch"]
    )[1]
    body = {
        "expected_epoch": lease["epoch"],
        "operation_id": package["operation"]["operation_id"],
        "observed_state": "running",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "migration-synthetic-provider",
        "termination_confirmed": False,
    }
    _, observation = execution.report_execution_observation(
        worker, lease["id"], "migration-observation", canonical_digest(body), body
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "ALTER TABLE research_experiment_admitted_attempts "
            "DROP COLUMN operational_observation_cursor"
        )
        connection.execute(
            "ALTER TABLE research_experiment_admitted_attempts DROP COLUMN operational_observed_at"
        )
        connection.execute("UPDATE schema_meta SET schema_version=33")

    assert migrate_state(state) == 35
    migrated = ExperimentStore(state).get_experiment(owner, experiment["id"])["admitted_attempts"][
        0
    ]
    assert migrated["id"] == admitted["id"]
    assert migrated["operational_observation_cursor"] == f"observation:{observation['id']}"
    assert migrated["operational_observed_at"] == observation["observed_at"]


def test_export_rejects_unbounded_admitted_attempt_state_history_before_materialization(
    tmp_path: Path,
) -> None:
    state, _execution, owner, _worker, grant, _lease = _delivery(tmp_path)
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
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.executemany(
            """INSERT INTO research_experiment_admitted_attempt_states
            (attempt_id,sequence,state,observed_state,reason,operation_id,recorded_at,actor_principal_id)
            VALUES (?,?,'admitted',NULL,NULL,NULL,?,?)""",
            [(admitted["id"], sequence, utc_now(), owner["id"]) for sequence in range(2, 1002)],
        )
    with pytest.raises(Conflict, match="admitted attempt state history record limit"):
        records.get_experiment(owner, experiment["id"])
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with TestClient(create_app(state)) as client:
        response = client.get(
            f"/experiments/{experiment['id']}/export",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert "admitted attempt state history record limit" in response.text


def test_admitted_attempt_rejects_stale_design_and_grant_reuse(tmp_path: Path) -> None:
    state, _execution, owner, _worker, grant, _lease = _delivery(tmp_path)
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    original_baseline = next(
        item for item in experiment["design"]["variants"] if item["kind"] == "baseline"
    )
    body = {
        "expected_variant_revision": original_baseline["revision"],
        "expected_design_revision": experiment["current_design_revision"],
        "grant_id": grant["id"],
    }
    _command(records.revise_design, owner, experiment["id"], payload=_design(1, grant))
    with pytest.raises(Conflict, match="variant or design revision"):
        _command(records.admit_attempt, owner, original_baseline["id"], payload=body)

    current = records.get_experiment(owner, experiment["id"])
    baseline = next(
        item for item in current["experiment"]["design"]["variants"] if item["kind"] == "baseline"
    )
    admitted = _command(
        records.admit_attempt,
        owner,
        baseline["id"],
        payload=body
        | {
            "expected_variant_revision": baseline["revision"],
            "expected_design_revision": current["experiment"]["current_design_revision"],
        },
    )
    intervention = next(
        item
        for item in current["experiment"]["design"]["variants"]
        if item["kind"] == "intervention"
    )
    with pytest.raises(Conflict, match="already bound"):
        _command(
            records.admit_attempt,
            owner,
            intervention["id"],
            payload=body
            | {
                "expected_variant_revision": intervention["revision"],
                "expected_design_revision": current["experiment"]["current_design_revision"],
            },
        )
    assert admitted["grant_id"] == grant["id"]


def test_normal_package_and_evidence_apis_keep_the_admitted_attempt_identity(
    tmp_path: Path,
) -> None:
    state, _execution, owner, worker, grant, lease = _delivery(tmp_path)
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
    body = {"expected_epoch": lease["epoch"]}
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "normal-package", canonical_digest(body), lease["epoch"]
    )[1]
    assert package["package"]["experiment"]["admitted_attempt_id"] == admitted["id"]
    # Treat the first successful reply as lost; replaying its durable key cannot
    # drop the automatic binding or create another package/operation identity.
    assert (
        RuntimePackageStore(state).execution_package(
            worker, lease["id"], "normal-package", canonical_digest(body), lease["epoch"]
        )[1]["package_digest"]
        == package["package_digest"]
    )
    with pytest.raises(Conflict, match="does not match"):
        RuntimePackageStore(state).execution_package(
            worker,
            lease["id"],
            "wrong-bound-package",
            canonical_digest(body | {"admitted_attempt_id": "wrong"}),
            lease["epoch"],
            admitted_attempt_id="wrong",
        )
    # A package intent is already durable, so a second admission cannot race in after it.
    with pytest.raises(Conflict, match="operation or package intent"):
        _command(
            records.admit_attempt,
            owner,
            baseline["id"],
            payload={
                "expected_variant_revision": baseline["revision"],
                "expected_design_revision": experiment["current_design_revision"],
                "grant_id": grant["id"],
            },
        )
    artifact_id = "artifact_admitted_identity"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO artifacts
            (id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,operation_id,
             relative_path,sha256,size,content,created_at,status,classification,late)
            VALUES (?,?,?,?,?,?,?,?,?,?,1,x'78',?,'quarantined','unclassified',0)""",
            (
                artifact_id,
                grant["project_id"],
                grant["task_id"],
                grant["id"],
                lease["id"],
                worker["id"],
                lease["epoch"],
                package["operation"]["operation_id"],
                "result.txt",
                "0" * 64,
                utc_now(),
            ),
        )
    admitted = records.get_experiment(owner, experiment["id"])["admitted_attempts"][0]
    linked = _command(
        records.link_attempt_artifacts,
        owner,
        admitted["id"],
        payload={"expected_revision": admitted["revision"], "artifact_ids": [artifact_id]},
    )
    measurement = _command(
        records.record_measurement,
        owner,
        admitted["id"],
        payload={
            "expected_revision": linked["revision"],
            "name": "bound duration",
            "value": {"seconds": 1},
            "artifact_id": artifact_id,
        },
    )
    conclusion = _command(
        records.create_conclusion,
        owner,
        experiment["id"],
        payload={
            "statement": "Bound evidence is still pending human scientific review.",
            "evidence": [{"attempt_id": admitted["id"], "measurement_ids": [measurement["id"]]}],
        },
    )
    exported = records.get_experiment(owner, experiment["id"])
    assert conclusion["evidence"][0]["attempt_id"] == admitted["id"]
    assert exported["admitted_attempts"][0]["id"] == admitted["id"]
    assert exported["measurements"][0]["attempt_id"] == admitted["id"]
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM research_experiment_attempts").fetchone()[0]
            == 0
        )


def test_experiment_evidence_retention_admission_delete_fence_and_export(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, manifest, artifact = _reconciled_manifest(tmp_path)
    _operation(state, owner, grant, lease, manifest["operation_id"])
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": manifest["operation_id"],
        "artifacts": [
            {"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}
        ],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(placement), placement
    )
    # The manifest is separate retention evidence; remove it to prove the durable
    # experiment reference itself prevents deletion.
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DELETE FROM artifact_manifests WHERE id=?", (manifest["id"],))
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
            "provenance_kind": "admitted_operation",
            "operation_id": manifest["operation_id"],
            "outcome": "succeeded",
        },
    )
    link_body = {"expected_revision": attempt["revision"], "artifact_ids": [artifact["id"]]}
    status, linked = records.link_attempt_artifacts(
        owner, attempt["id"], "evidence-link", canonical_digest(link_body), link_body
    )
    assert status == 200
    # A same-key replay does not create another custody reference.
    assert records.link_attempt_artifacts(
        owner, attempt["id"], "evidence-link", canonical_digest(link_body), link_body
    ) == (status, linked)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT reference_kind,project_id FROM experiment_artifact_evidence_references"
        ).fetchall() == [("attempt_artifact", grant["project_id"])]
    retention = ProjectArtifactRetentionStore(state, ProjectArtifactStore(state).backend)
    with pytest.raises(Conflict, match="Referenced"):
        retention.tombstone(owner, grant["project_id"], artifact["id"], "tomb", "tomb")

    # A deletion reservation rejects both kinds of new evidence reference and
    # rolls each command back together.
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE project_artifact_retention SET state='delete_intent' WHERE artifact_id=?",
            (artifact["id"],),
        )
    blocked_attempt = _command(
        records.record_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
            "provenance_kind": "human_reported_observation",
            "human_observation": "Human recorded a second observation during deletion reservation.",
            "outcome": "inconclusive",
            "reason": "Deletion reservation blocks a new evidence link.",
        },
    )
    blocked_link_body = {
        "expected_revision": blocked_attempt["revision"],
        "artifact_ids": [artifact["id"]],
    }
    with pytest.raises(Conflict, match="reserved for deletion"):
        records.link_attempt_artifacts(
            owner,
            blocked_attempt["id"],
            "link-during-delete",
            canonical_digest(blocked_link_body),
            blocked_link_body,
        )
    measurement_body = {
        "expected_revision": linked["revision"],
        "name": "blocked during delete",
        "value": {"seconds": 1},
        "artifact_id": artifact["id"],
    }
    with pytest.raises(Conflict, match="reserved for deletion"):
        records.record_measurement(
            owner,
            attempt["id"],
            "measurement-during-delete",
            canonical_digest(measurement_body),
            measurement_body,
        )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM research_attempt_measurements").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM experiment_artifact_evidence_references"
            ).fetchone()[0]
            == 1
        )
        connection.execute(
            "UPDATE project_artifact_retention SET state='deleted' WHERE artifact_id=?",
            (artifact["id"],),
        )
    exported = records.get_experiment(owner, experiment["id"])
    assert exported["artifacts"][0]["status"] == "quarantined"
    assert exported["artifacts"][0]["custody_state"] == "deleted"
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    response = TestClient(create_app(state)).get(
        f"/experiments/{experiment['id']}", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert response.status_code == 200
    assert response.json()["artifacts"][0]["retention_state"] == "deleted"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE project_artifact_retention SET state='degraded' WHERE artifact_id=?",
            (artifact["id"],),
        )
    assert (
        records.get_experiment(owner, experiment["id"])["artifacts"][0]["retention_state"]
        == "degraded"
    )


def test_nonplaced_experiment_evidence_remains_explicit_legacy_custody(tmp_path: Path) -> None:
    state, owner, _worker, grant, lease, manifest, artifact = _reconciled_manifest(tmp_path)
    _operation(state, owner, grant, lease, manifest["operation_id"])
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
            "provenance_kind": "admitted_operation",
            "operation_id": manifest["operation_id"],
            "outcome": "succeeded",
        },
    )
    _command(
        records.link_attempt_artifacts,
        owner,
        attempt["id"],
        payload={"expected_revision": attempt["revision"], "artifact_ids": [artifact["id"]]},
    )
    assert records.get_experiment(owner, experiment["id"])["artifacts"][0]["custody_state"] == (
        "legacy_unplaced"
    )


def test_schema25_adds_empty_chain_tables_without_rewriting_existing_evidence(
    tmp_path: Path,
) -> None:
    state, _owner, _worker, _grant, _lease, manifest, _artifact = _reconciled_manifest(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        for table in (
            "research_conclusion_evidence",
            "research_conclusions",
            "research_attempt_measurements",
            "research_attempt_artifacts",
            "research_experiment_attempts",
            "research_experiment_variants",
            "research_experiment_designs",
            "research_experiments",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=24")
    assert migrate_state(state) == 35
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT id FROM artifact_manifests WHERE id=?", (manifest["id"],)
        ).fetchone()
        assert connection.execute("SELECT count(*) FROM research_experiments").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM experiment_artifact_evidence_references"
            ).fetchone()[0]
            == 0
        )


def _legacy_experiment_state(
    tmp_path: Path, source_version: int
) -> tuple[Path, dict, str, str, str]:
    """Seed one populated historical v25-v29 ledger with its exact later tables absent."""
    state, owner, _worker, grant, lease, manifest, artifact = _reconciled_manifest(tmp_path)
    experiment_id, attempt_id, measurement_id, conclusion_id = (
        "experiment_legacy",
        "attempt_legacy",
        "measurement_legacy",
        "conclusion_legacy",
    )
    now = utc_now()
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in (
            "research_admitted_conclusion_evidence",
            "admitted_experiment_artifact_evidence_references",
            "research_admitted_attempt_measurements",
            "research_admitted_attempt_artifacts",
            "research_experiment_admitted_attempt_resolutions",
            "research_experiment_admitted_attempt_states",
            "research_experiment_admitted_attempts",
            "research_conclusion_amendments",
            "research_conclusion_lineage",
            "experiment_artifact_evidence_references",
            "research_conclusion_evidence",
            "research_conclusions",
            "research_attempt_measurements",
            "research_attempt_artifacts",
            "research_experiment_attempts",
            "research_experiment_variants",
            "research_experiment_designs",
            "research_experiments",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TABLE external_operation_receipts")
        connection.execute("DROP TABLE external_operations")
        # An authentic v25-v29 ledger predates the v30 operation binding
        # columns/index. Recreate that historical shape before placing its
        # populated attempt/evidence/conclusion chain.
        create_external_operation_schema(connection)
        create_external_operation_receipt_schema(connection)
        _insert_operation(connection, owner, grant, lease, manifest["operation_id"])
        create_v25_experiment_schema(connection)
        connection.execute(
            """INSERT INTO research_experiments
            (id,project_id,goal,success_criterion,current_design_revision,created_at,updated_at,
             created_by_principal_id) VALUES (?,?,?, ?,2,?,?,?)""",
            (
                experiment_id,
                grant["project_id"],
                "Current legacy goal remains a projection.",
                "Current legacy criterion remains a projection.",
                now,
                now,
                owner["id"],
            ),
        )
        connection.executemany(
            """INSERT INTO research_experiment_designs
            (experiment_id,revision,protocol,created_at,created_by_principal_id)
            VALUES (?,?,?,?,?)""",
            [
                (experiment_id, 1, "Original protocol.", now, owner["id"]),
                (experiment_id, 2, "Current protocol.", now, owner["id"]),
            ],
        )
        connection.executemany(
            """INSERT INTO research_experiment_variants
            (id,experiment_id,design_revision,kind,name,method_digest,input_digest,metadata_json,
             revision,created_at,created_by_principal_id)
            VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
            [
                (
                    "variant_legacy_baseline_1",
                    experiment_id,
                    1,
                    "baseline",
                    "original baseline",
                    "a" * 64,
                    "b" * 64,
                    "{}",
                    now,
                    owner["id"],
                ),
                (
                    "variant_legacy_intervention_1",
                    experiment_id,
                    1,
                    "intervention",
                    "original intervention",
                    "c" * 64,
                    "d" * 64,
                    "{}",
                    now,
                    owner["id"],
                ),
                (
                    "variant_legacy_baseline_2",
                    experiment_id,
                    2,
                    "baseline",
                    "current baseline",
                    "e" * 64,
                    "f" * 64,
                    "{}",
                    now,
                    owner["id"],
                ),
                (
                    "variant_legacy_intervention_2",
                    experiment_id,
                    2,
                    "intervention",
                    "current intervention",
                    "0" * 64,
                    "1" * 64,
                    "{}",
                    now,
                    owner["id"],
                ),
            ],
        )
        connection.execute(
            """INSERT INTO research_experiment_attempts
            (id,variant_id,operation_id,outcome,reason,revision,created_at,created_by_principal_id)
            VALUES (?,?,?,'failed',?,3,?,?)""",
            (
                attempt_id,
                "variant_legacy_baseline_1",
                manifest["operation_id"],
                "Original operation failed; evidence remains reviewable.",
                now,
                owner["id"],
            ),
        )
        connection.execute(
            """INSERT INTO research_attempt_artifacts (attempt_id,artifact_id,created_at)
            VALUES (?,?,?)""",
            (attempt_id, artifact["id"], now),
        )
        connection.execute(
            """INSERT INTO research_attempt_measurements
            (id,attempt_id,name,value_json,artifact_id,created_at,created_by_principal_id)
            VALUES (?,?,?,'{"seconds":12}',?,?,?)""",
            (measurement_id, attempt_id, "original duration", artifact["id"], now, owner["id"]),
        )
        connection.execute(
            """INSERT INTO research_conclusions
            (id,experiment_id,statement,status,revision,created_at,updated_at,created_by_principal_id,
             reviewed_at,reviewed_by_principal_id)
            VALUES (?,?,'Original conclusion.','reviewed',2,?,?,?, ?,?)""",
            (conclusion_id, experiment_id, now, now, owner["id"], now, owner["id"]),
        )
        connection.execute(
            """INSERT INTO research_conclusion_evidence
            (conclusion_id,attempt_id,artifact_id,measurement_id) VALUES (?,?,NULL,?)""",
            (conclusion_id, attempt_id, measurement_id),
        )
        connection.execute("UPDATE schema_meta SET schema_version=25")
        if source_version >= 26:
            create_v26_experiment_evidence_retention_schema(connection)
            connection.execute(
                """INSERT INTO experiment_artifact_evidence_references
                (id,artifact_id,project_id,attempt_id,measurement_id,reference_kind,created_at)
                VALUES (?,?,?,?,NULL,'attempt_artifact',?)""",
                (
                    f"experiment_evidence_attempt_{attempt_id}_{artifact['id']}",
                    artifact["id"],
                    grant["project_id"],
                    attempt_id,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO experiment_artifact_evidence_references
                (id,artifact_id,project_id,attempt_id,measurement_id,reference_kind,created_at)
                VALUES (?,?,?,?,?,'measurement_artifact',?)""",
                (
                    f"experiment_evidence_measurement_{measurement_id}",
                    artifact["id"],
                    grant["project_id"],
                    attempt_id,
                    measurement_id,
                    now,
                ),
            )
            connection.execute("UPDATE schema_meta SET schema_version=26")
        if source_version >= 27:
            MIGRATIONS[26](connection)
        if source_version >= 28:
            MIGRATIONS[27](connection)
        if source_version >= 29:
            MIGRATIONS[28](connection)
        connection.commit()
    return state, owner, experiment_id, attempt_id, measurement_id


def _operation_binding_schema(
    state: Path,
) -> tuple[list[tuple[object, ...]], tuple[object, ...] | None]:
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        columns = connection.execute("PRAGMA table_info(external_operations)").fetchall()
        index = connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_schema "
            "WHERE type='index' AND name='external_operations_experiment_attempt_once'"
        ).fetchone()
    return columns, index


@pytest.mark.parametrize("source_version", (25, 26, 27, 28, 29))
def test_v25_through_v29_experiment_ledgers_upgrade_backup_restore_and_repeat_without_loss(
    tmp_path: Path, source_version: int
) -> None:
    state, owner, experiment_id, attempt_id, measurement_id = _legacy_experiment_state(
        tmp_path, source_version
    )

    assert migrate_state(state) == 35
    records = ExperimentStore(state)
    exported = records.export_experiment(owner, experiment_id)
    reopened = ExperimentStore(state).get_experiment(owner, experiment_id)
    assert reopened == exported
    assert migrate_state(state) == 35
    assert ExperimentStore(state).get_experiment(owner, experiment_id) == exported

    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    assert _operation_binding_schema(state) == _operation_binding_schema(fresh)
    backup = tmp_path / "backup"
    backup_state(state, backup)
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    restored_token = (restored / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    restored_owner = Store(restored).authenticate(restored_token)
    assert ExperimentStore(restored).get_experiment(restored_owner, experiment_id) == exported
    assert Store(restored).recovery_status(restored_owner)["state"] == "held_restore"

    designs = {item["revision"]: item for item in exported["designs"]}
    assert (designs[1]["goal"], designs[1]["success_criterion"]) == (None, None)
    assert (designs[2]["goal"], designs[2]["success_criterion"]) == (
        "Current legacy goal remains a projection.",
        "Current legacy criterion remains a projection.",
    )
    assert exported["measurements"] == [
        {
            "artifact_id": exported["artifacts"][0]["id"],
            "attempt_id": attempt_id,
            "created_at": exported["measurements"][0]["created_at"],
            "created_by_principal_id": owner["id"],
            "id": measurement_id,
            "name": "original duration",
            "value": {"seconds": 12},
        }
    ]
    assert exported["conclusions"][0]["design_revision"] == 1
    assert exported["conclusions"][0]["design_snapshot"]["goal"] is None
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT execution_task_id,execution_plan_digest FROM research_experiment_variants "
            "WHERE id='variant_legacy_baseline_1'"
        ).fetchone() == (None, None)
        assert connection.execute(
            """SELECT provenance_kind,operation_id,execution_plan_digest,package_digest,
            operation_desired_state,operation_observed_state,termination_status,human_observation
            FROM research_experiment_attempts WHERE id=?""",
            (attempt_id,),
        ).fetchone() == (
            "legacy_operation_unverified",
            exported["operations"][0]["operation_id"],
            None,
            None,
            None,
            "legacy_unverified",
            "legacy_unknown",
            None,
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM experiment_artifact_evidence_references"
            ).fetchone()[0]
            == 2
        )


def test_v27_to_v28_experiment_provenance_migration_rolls_back_complete_legacy_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _owner, _experiment_id, _attempt_id, _measurement_id = _legacy_experiment_state(
        tmp_path, 27
    )
    database = state / "tessera.sqlite3"
    with sqlite3.connect(database) as connection:
        before = list(connection.iterdump())

    def fail_after_provenance_rebuild(connection: sqlite3.Connection) -> None:
        migrate_v27_to_v28(connection)
        raise RuntimeError("injected provenance migration failure")

    monkeypatch.setitem(MIGRATIONS, 27, fail_after_provenance_rebuild)
    with pytest.raises(RuntimeError, match="injected provenance"):
        migrate_state(state)
    with sqlite3.connect(database) as connection:
        assert list(connection.iterdump()) == before
    monkeypatch.undo()
    assert migrate_state(state) == 35


def test_design_revisions_and_conclusions_retain_their_original_context(tmp_path: Path) -> None:
    state, owner, _worker, grant, lease, manifest, artifact = _reconciled_manifest(tmp_path)
    _operation(state, owner, grant, lease, manifest["operation_id"])
    records = ExperimentStore(state)
    first = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline_one = next(item for item in first["design"]["variants"] if item["kind"] == "baseline")
    attempt_one = _command(
        records.record_attempt,
        owner,
        baseline_one["id"],
        payload={
            "expected_variant_revision": baseline_one["revision"],
            "provenance_kind": "admitted_operation",
            "operation_id": manifest["operation_id"],
            "outcome": "failed",
            "reason": "The original bounded operation failed before the revision.",
        },
    )
    linked_one = _command(
        records.link_attempt_artifacts,
        owner,
        attempt_one["id"],
        payload={"expected_revision": attempt_one["revision"], "artifact_ids": [artifact["id"]]},
    )
    measurement_one = _command(
        records.record_measurement,
        owner,
        attempt_one["id"],
        payload={
            "expected_revision": linked_one["revision"],
            "name": "first duration",
            "value": {"seconds": 2},
            "artifact_id": artifact["id"],
        },
    )
    conclusion_one = _command(
        records.create_conclusion,
        owner,
        first["id"],
        payload={
            "design_revision": 1,
            "statement": "First design needs human review.",
            "evidence": [
                {"attempt_id": attempt_one["id"], "measurement_ids": [measurement_one["id"]]}
            ],
        },
    )
    _command(
        records.review_conclusion,
        owner,
        conclusion_one["id"],
        payload={"expected_revision": conclusion_one["revision"], "design_revision": 1},
    )

    second_design = _design(1, grant)
    second_design.update(
        goal="Reduce synthetic processing time without increasing failures.",
        success_criterion="A human confirms both duration and failure-rate measurements.",
        protocol="Repeat the changed baseline and intervention under the revised method.",
        variants=[
            {
                "kind": "baseline",
                "name": "revised baseline",
                "method_digest": "e" * 64,
                "input_digest": "f" * 64,
                "metadata": {"synthetic": "second"},
                "execution_task_id": grant["task_id"],
                "execution_plan_digest": grant["plan_digest"],
            },
            {
                "kind": "intervention",
                "name": "revised intervention",
                "method_digest": "1" * 64,
                "input_digest": "2" * 64,
                "metadata": {"synthetic": "second"},
                "execution_task_id": grant["task_id"],
                "execution_plan_digest": grant["plan_digest"],
            },
        ],
    )
    second = _command(records.revise_design, owner, first["id"], payload=second_design)
    baseline_two = next(item for item in second["design"]["variants"] if item["kind"] == "baseline")
    attempt_two = _command(
        records.record_attempt,
        owner,
        baseline_two["id"],
        payload={
            "expected_variant_revision": baseline_two["revision"],
            "provenance_kind": "human_reported_observation",
            "human_observation": "Human recorded the revised-baseline observation.",
            "outcome": "succeeded",
        },
    )
    measurement_two = _command(
        records.record_measurement,
        owner,
        attempt_two["id"],
        payload={
            "expected_revision": attempt_two["revision"],
            "name": "second duration",
            "value": {"seconds": 1},
            "artifact_id": None,
        },
    )
    conclusion_two = _command(
        records.create_conclusion,
        owner,
        first["id"],
        payload={
            "design_revision": 2,
            "statement": "Second design needs human review.",
            "evidence": [
                {"attempt_id": attempt_two["id"], "measurement_ids": [measurement_two["id"]]}
            ],
        },
    )
    _command(
        records.review_conclusion,
        owner,
        conclusion_two["id"],
        payload={"expected_revision": conclusion_two["revision"], "design_revision": 2},
    )

    exported = records.export_experiment(owner, first["id"])
    reopened = ExperimentStore(state).get_experiment(owner, first["id"])
    assert reopened == exported
    designs = {item["revision"]: item for item in exported["designs"]}
    assert designs[1]["goal"] == _design()["goal"]
    assert designs[1]["success_criterion"] == _design()["success_criterion"]
    assert designs[1]["protocol"] == _design()["protocol"]
    assert [(item["method_digest"], item["input_digest"]) for item in designs[1]["variants"]] == [
        ("a" * 64, "b" * 64),
        ("c" * 64, "d" * 64),
    ]
    assert designs[2]["goal"] == second_design["goal"]
    assert designs[2]["success_criterion"] == second_design["success_criterion"]
    assert designs[2]["protocol"] == second_design["protocol"]
    assert [(item["method_digest"], item["input_digest"]) for item in designs[2]["variants"]] == [
        ("e" * 64, "f" * 64),
        ("1" * 64, "2" * 64),
    ]
    assert {item["id"] for item in exported["variants"]} == {
        variant["id"] for design in designs.values() for variant in design["variants"]
    }
    assert exported["attempts"][0]["outcome"] == "failed"
    assert exported["attempts"][0]["operation_id"] == manifest["operation_id"]
    assert exported["attempts"][1]["provenance_kind"] == "human_reported_observation"
    assert not any(
        attempt["variant_id"] == designs[2]["variants"][1]["id"] for attempt in exported["attempts"]
    )
    snapshots = {
        item["design_revision"]: item["design_snapshot"] for item in exported["conclusions"]
    }
    assert snapshots == designs

    # Simulate the v26 table shapes after records already existed. Only the current
    # projection held goal/criterion then, so migration must not invent them for v1.
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE migration_27_designs AS SELECT * FROM research_experiment_designs"
        )
        connection.execute("DROP TABLE research_experiment_designs")
        connection.execute(
            """CREATE TABLE research_experiment_designs (
            experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
            revision INTEGER NOT NULL CHECK (revision >= 1), protocol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            PRIMARY KEY (experiment_id,revision))"""
        )
        connection.execute(
            """INSERT INTO research_experiment_designs
            (experiment_id,revision,protocol,created_at,created_by_principal_id)
            SELECT experiment_id,revision,protocol,created_at,created_by_principal_id
            FROM migration_27_designs"""
        )
        connection.execute("DROP TABLE migration_27_designs")
        connection.execute(
            "CREATE TABLE migration_27_conclusions AS SELECT * FROM research_conclusions"
        )
        connection.execute(
            """CREATE TABLE migration_27_conclusion_evidence AS
            SELECT * FROM research_conclusion_evidence"""
        )
        connection.execute("DROP TABLE research_conclusion_evidence")
        connection.execute("DROP TABLE research_conclusions")
        connection.execute(
            """CREATE TABLE research_conclusions (
            id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
            statement TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('draft','reviewed')),
            revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            reviewed_at TEXT, reviewed_by_principal_id TEXT REFERENCES principals(id))"""
        )
        connection.execute(
            """INSERT INTO research_conclusions
            (id,experiment_id,statement,status,revision,created_at,updated_at,
             created_by_principal_id,reviewed_at,reviewed_by_principal_id)
            SELECT id,experiment_id,statement,status,revision,created_at,updated_at,
            created_by_principal_id,reviewed_at,reviewed_by_principal_id
            FROM migration_27_conclusions"""
        )
        connection.execute(
            """CREATE TABLE research_conclusion_evidence (
            conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
            attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
            artifact_id TEXT REFERENCES artifacts(id),
            measurement_id TEXT REFERENCES research_attempt_measurements(id),
            PRIMARY KEY(conclusion_id,attempt_id,artifact_id,measurement_id),
            CHECK ((artifact_id IS NOT NULL AND measurement_id IS NULL) OR
                   (artifact_id IS NULL AND measurement_id IS NOT NULL)))"""
        )
        connection.execute(
            """INSERT INTO research_conclusion_evidence
            SELECT conclusion_id,attempt_id,artifact_id,measurement_id
            FROM migration_27_conclusion_evidence"""
        )
        connection.execute("DROP TABLE migration_27_conclusion_evidence")
        connection.execute("DROP TABLE migration_27_conclusions")
        connection.execute("UPDATE schema_meta SET schema_version=26")
    assert migrate_state(state) == 35
    migrated = ExperimentStore(state).get_experiment(owner, first["id"])
    migrated_designs = {item["revision"]: item for item in migrated["designs"]}
    assert migrated_designs[1]["goal"] is None
    assert migrated_designs[1]["success_criterion"] is None
    assert migrated_designs[2]["goal"] == second_design["goal"]
    assert migrated_designs[2]["success_criterion"] == second_design["success_criterion"]
    assert {item["design_revision"] for item in migrated["conclusions"]} == {1, 2}


def test_v30_to_v31_binding_repair_rolls_back_and_preserves_authentic_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _owner, _experiment_id, _attempt_id, _measurement_id = _legacy_experiment_state(
        tmp_path, 29
    )
    database = state / "tessera.sqlite3"
    with sqlite3.connect(database) as connection:
        MIGRATIONS[29](connection)
        connection.commit()
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 30
        before = list(connection.iterdump())

    def fail_after_binding_repair(connection: sqlite3.Connection) -> None:
        migrate_v30_to_v31(connection)
        raise RuntimeError("injected binding repair failure")

    monkeypatch.setitem(MIGRATIONS, 30, fail_after_binding_repair)
    with pytest.raises(RuntimeError, match="injected binding repair"):
        migrate_state(state)
    with sqlite3.connect(database) as connection:
        assert list(connection.iterdump()) == before
    monkeypatch.undo()
    assert migrate_state(state) == 35


def test_experiment_export_rejects_large_measurement_aggregate_before_materializing(
    tmp_path: Path,
) -> None:
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
            "human_observation": "Synthetic bounded export observation.",
            "outcome": "inconclusive",
            "reason": "Fixture records a measurement aggregate only.",
        },
    )
    now = utc_now()
    large_value = '{"payload":"' + "x" * 40_000 + '"}'
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.executemany(
            """INSERT INTO research_attempt_measurements
            (id,attempt_id,name,value_json,artifact_id,created_at,created_by_principal_id)
            VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    f"measurement-limit-{index}",
                    attempt["id"],
                    f"large measurement {index}",
                    large_value,
                    None,
                    now,
                    owner["id"],
                )
                for index in range(1_001)
            ],
        )
    with pytest.raises(Conflict, match=r"per-experiment .* record limit"):
        records.export_experiment(owner, experiment["id"])


def test_experiment_export_byte_cap_and_api_response_boundary(tmp_path: Path) -> None:
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
            "human_observation": "Synthetic response-size observation.",
            "outcome": "inconclusive",
            "reason": "Fixture records a byte-bound aggregate only.",
        },
    )
    now = utc_now()
    large_value = '{"payload":"' + "x" * 40_000 + '"}'
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.executemany(
            """INSERT INTO research_attempt_measurements
            (id,attempt_id,name,value_json,artifact_id,created_at,created_by_principal_id)
            VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    f"measurement-bytes-{index}",
                    attempt["id"],
                    f"large measurement {index}",
                    large_value,
                    None,
                    now,
                    owner["id"],
                )
                for index in range(30)
            ],
        )
    with pytest.raises(Conflict, match="requested byte cap"):
        records.export_experiment(owner, experiment["id"])

    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    client = TestClient(create_app(state))
    rejected = client.get(
        f"/experiments/{experiment['id']}/export",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "Export exceeds the requested byte cap."}
    accepted = client.get(
        f"/experiments/{experiment['id']}/export?max_bytes={4 * 1024 * 1024}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert accepted.status_code == 200
    assert len(accepted.content) <= 4 * 1024 * 1024
    assert (
        client.get(
            f"/experiments/{experiment['id']}/export?max_bytes={4 * 1024 * 1024 + 1}",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 422
    )


def _insert_human_attempts(connection, owner, variant_id: str, count: int) -> list[str]:
    now = utc_now()
    ids = [f"attempt-bound-{index}" for index in range(count)]
    connection.executemany(
        """INSERT INTO research_experiment_attempts
        (id,variant_id,design_revision,provenance_kind,operation_id,execution_plan_digest,
         package_digest,operation_desired_state,operation_observed_state,termination_status,
         human_observation,outcome,reason,revision,created_at,created_by_principal_id)
        VALUES (?,?,1,'human_reported_observation',NULL,NULL,NULL,NULL,'not_observed',
        'not_applicable',?,'inconclusive',?,1,?,?)""",
        [
            (
                ident,
                variant_id,
                f"Synthetic observation {index}.",
                f"Reason {index}.",
                now,
                owner["id"],
            )
            for index, ident in enumerate(ids)
        ],
    )
    return ids


@pytest.mark.parametrize(
    "collection",
    (
        "designs",
        "variants",
        "attempts",
        "operations",
        "artifact links",
        "artifacts",
        "measurements",
        "conclusions",
        "conclusion evidence",
    ),
)
def test_experiment_export_preflights_every_collection_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collection: str
) -> None:
    state, owner, _worker, grant, lease, manifest, artifact = _reconciled_manifest(tmp_path)
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    now = utc_now()
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        if collection == "designs":
            for revision in (1, 2):
                _command(
                    records.revise_design, owner, experiment["id"], payload=_design(revision, grant)
                )
        elif collection == "variants":
            _command(records.revise_design, owner, experiment["id"], payload=_design(1, grant))
        elif collection == "operations":
            operation_ids = []
            for index in range(3):
                operation_id = f"{index + 1:032x}"
                lease_id = f"lease-bound-{index}"
                connection.execute(
                    """INSERT INTO execution_leases
                    (id,grant_id,worker_principal_id,epoch,issued_at,expires_at,hard_expires_at,renewed_at)
                    SELECT ?,grant_id,worker_principal_id,epoch,issued_at,expires_at,
                    hard_expires_at,renewed_at
                    FROM execution_leases WHERE id=?""",
                    (lease_id, lease["id"]),
                )
                connection.execute(
                    """INSERT INTO external_operations
                    (operation_id,org_id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,
                     package_digest,adapter_version,profile_version,docker_lookup_key,request_digest,
                     retry_class,desired_state,observed_state,provider_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'development-local-cpu-v1','local-cpu-v1',?, ?,
                    'manual_reconciliation','admitted','exited',?,?,?)""",
                    (
                        operation_id,
                        owner["org_id"],
                        grant["project_id"],
                        grant["task_id"],
                        grant["id"],
                        lease_id,
                        lease["worker_principal_id"],
                        lease["epoch"],
                        "a" * 64,
                        f"lookup-{operation_id}",
                        "b" * 64,
                        f"fake-{operation_id}",
                        now,
                        now,
                    ),
                )
                operation_ids.append(operation_id)
            connection.executemany(
                """INSERT INTO research_experiment_attempts
                (id,variant_id,design_revision,provenance_kind,operation_id,execution_plan_digest,
                 package_digest,operation_desired_state,operation_observed_state,termination_status,
                 human_observation,outcome,reason,revision,created_at,created_by_principal_id)
                VALUES (?,?,1,'admitted_operation',?,?,?,'admitted','exited','confirmed',NULL,
                'succeeded',NULL,1,?,?)""",
                [
                    (
                        f"attempt-operation-{index}",
                        baseline["id"],
                        operation_id,
                        grant["plan_digest"],
                        "a" * 64,
                        now,
                        owner["id"],
                    )
                    for index, operation_id in enumerate(operation_ids)
                ],
            )
        elif collection == "artifacts":
            for index in range(2):
                connection.execute(
                    """INSERT INTO artifacts
                    (id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,operation_id,
                     relative_path,sha256,size,content,created_at,status,classification,late)
                    SELECT ?,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,
                    operation_id,?,sha256,size,content,created_at,status,classification,late
                    FROM artifacts WHERE id=?""",
                    (f"artifact-bound-{index}", f"bound-{index}.txt", artifact["id"]),
                )
            attempts = _insert_human_attempts(connection, owner, baseline["id"], 3)
            connection.executemany(
                """INSERT INTO research_attempt_artifacts (attempt_id,artifact_id,created_at)
                VALUES (?,?,?)""",
                [
                    (attempt, artifact_id, now)
                    for attempt, artifact_id in zip(
                        attempts,
                        [artifact["id"], "artifact-bound-0", "artifact-bound-1"],
                        strict=True,
                    )
                ],
            )
        elif collection == "artifact links":
            attempts = _insert_human_attempts(connection, owner, baseline["id"], 3)
            connection.executemany(
                """INSERT INTO research_attempt_artifacts (attempt_id,artifact_id,created_at)
                VALUES (?,?,?)""",
                [(attempt, artifact["id"], now) for attempt in attempts],
            )
        elif collection == "measurements":
            attempt = _insert_human_attempts(connection, owner, baseline["id"], 1)[0]
            connection.executemany(
                """INSERT INTO research_attempt_measurements
                (id,attempt_id,name,value_json,artifact_id,created_at,created_by_principal_id)
                VALUES (?,?,?,?,NULL,?,?)""",
                [
                    (f"measurement-bound-{index}", attempt, str(index), "{}", now, owner["id"])
                    for index in range(3)
                ],
            )
        elif collection == "conclusions":
            connection.executemany(
                """INSERT INTO research_conclusions
                (id,experiment_id,design_revision,statement,status,revision,created_at,updated_at,
                 created_by_principal_id,reviewed_at,reviewed_by_principal_id)
                VALUES (?,?,1,?,'draft',1,?,?,?,NULL,NULL)""",
                [
                    (
                        f"conclusion-bound-{index}",
                        experiment["id"],
                        str(index),
                        now,
                        now,
                        owner["id"],
                    )
                    for index in range(3)
                ],
            )
        elif collection == "conclusion evidence":
            attempt = _insert_human_attempts(connection, owner, baseline["id"], 1)[0]
            connection.execute(
                """INSERT INTO research_conclusions
                (id,experiment_id,design_revision,statement,status,revision,created_at,updated_at,
                 created_by_principal_id,reviewed_at,reviewed_by_principal_id)
                VALUES ('conclusion-bound',?,1,'Fixture','draft',1,?,?,?,NULL,NULL)""",
                (experiment["id"], now, now, owner["id"]),
            )
            connection.executemany(
                """INSERT INTO research_conclusion_evidence
                (conclusion_id,attempt_id,artifact_id,measurement_id)
                VALUES ('conclusion-bound',?,?,NULL)""",
                [(attempt, artifact["id"]) for _ in range(3)],
            )
        else:
            _insert_human_attempts(connection, owner, baseline["id"], 3)
    monkeypatch.setattr("tessera_control.experiments.EXPORT_RECORD_LIMIT", 2)
    with pytest.raises(Conflict, match=f"{collection} record limit"):
        records.export_experiment(owner, experiment["id"], {"max_bytes": 4 * 1024 * 1024})
