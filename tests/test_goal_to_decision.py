"""Focused durable goal-to-decision ledger tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_result_checkpoints import _reconciled_manifest

from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.project_artifact_retention import ProjectArtifactRetentionStore
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _token_hash,
    canonical_digest,
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


def _operation(state: Path, owner, grant, lease, operation_id: str) -> None:
    now = utc_now()
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
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
    assert migrate_state(state) == 28
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
    assert migrate_state(state) == 28
    migrated = ExperimentStore(state).get_experiment(owner, first["id"])
    migrated_designs = {item["revision"]: item for item in migrated["designs"]}
    assert migrated_designs[1]["goal"] is None
    assert migrated_designs[1]["success_criterion"] is None
    assert migrated_designs[2]["goal"] == second_design["goal"]
    assert migrated_designs[2]["success_criterion"] == second_design["success_criterion"]
    assert {item["design_revision"] for item in migrated["conclusions"]} == {1, 2}
