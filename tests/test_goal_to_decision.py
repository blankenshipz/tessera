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
from tessera_control.migrations import MIGRATIONS
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


def _design(revision: int | None = None):
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
    return body


def test_goal_design_attempt_evidence_conclusion_and_reopen_chain(tmp_path: Path) -> None:
    state, owner, _worker, grant, lease, manifest, artifact = _reconciled_manifest(tmp_path)
    _operation(state, owner, grant, lease, manifest["operation_id"])
    records = ExperimentStore(state)
    created = _command(records.create_experiment, owner, grant["project_id"], payload=_design())
    assert {item["kind"] for item in created["design"]["variants"]} == {
        "baseline",
        "intervention",
    }
    revised = _command(records.revise_design, owner, created["id"], payload=_design(1))
    assert revised["current_design_revision"] == 2
    baseline = next(item for item in revised["design"]["variants"] if item["kind"] == "baseline")
    attempt = _command(
        records.record_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
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
    # v26 materializes only existing evidence relationships for retention lookup;
    # a failed migration leaves v25 and its data untouched for an explicit retry.
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE experiment_artifact_evidence_references")
        connection.execute("UPDATE schema_meta SET schema_version=25")
    assert migrate_state(state) == 26
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT reference_kind FROM experiment_artifact_evidence_references "
            "ORDER BY reference_kind"
        ).fetchall() == [("attempt_artifact",), ("measurement_artifact",)]
        connection.execute("DROP TABLE experiment_artifact_evidence_references")
        connection.execute("UPDATE schema_meta SET schema_version=25")

    def fail_migration(connection):
        from tessera_control.experiments import create_experiment_evidence_retention_schema

        create_experiment_evidence_retention_schema(connection)
        raise RuntimeError("v26 fault")

    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(MIGRATIONS, 25, fail_migration)
        with pytest.raises(RuntimeError, match="v26 fault"):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 25
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='experiment_artifact_evidence_references'"
            ).fetchone()
            is None
        )
    assert migrate_state(state) == 26


def test_outcomes_tenant_revision_and_human_fences(tmp_path: Path) -> None:
    state, owner, _worker, grant, lease, manifest, _artifact = _reconciled_manifest(tmp_path)
    _operation(state, owner, grant, lease, manifest["operation_id"])
    records = ExperimentStore(state)
    experiment = _command(records.create_experiment, owner, grant["project_id"], payload=_design())
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    for outcome in ("failed", "cancelled", "inconclusive"):
        attempt = _command(
            records.record_attempt,
            owner,
            baseline["id"],
            payload={
                "expected_variant_revision": baseline["revision"],
                "operation_id": manifest["operation_id"],
                "outcome": outcome,
                "reason": f"Synthetic {outcome} outcome remains recorded.",
            },
        )
        assert attempt["outcome"] == outcome
        assert attempt["scientific_validity"] == "not_assessed"
    with pytest.raises(Conflict, match="revision"):
        _command(records.revise_design, owner, experiment["id"], payload=_design(2))
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
    experiment = _command(records.create_experiment, owner, grant["project_id"], payload=_design())
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    attempt = _command(
        records.record_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
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
            "operation_id": manifest["operation_id"],
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
    experiment = _command(records.create_experiment, owner, grant["project_id"], payload=_design())
    baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
    attempt = _command(
        records.record_attempt,
        owner,
        baseline["id"],
        payload={
            "expected_variant_revision": baseline["revision"],
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
    assert migrate_state(state) == 26
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
