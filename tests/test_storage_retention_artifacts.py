from __future__ import annotations

# ruff: noqa: E501, I001

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_artifacts import _payload
from test_input_delivery import _delivery

from tessera_control.artifacts import ArtifactStore
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.project_artifacts import FilesystemProjectArtifactBackend
from tessera_control.storage_retention_artifacts import (
    delete_artifact,
    list_artifact_candidates,
)
from tessera_control.storage_retention_models import RetentionPolicy
from tessera_control.store import Conflict, canonical_digest

NOW = datetime(2026, 6, 1, tzinfo=UTC)
OLD = "2026-01-01T00:00:00.000000Z"
POLICY = RetentionPolicy(artifact_max_age_days=30)


class Fixture:
    def __init__(self, tmp_path: Path, *, age: bool = True) -> None:
        self.state, _, self.owner, self.worker, self.grant, self.lease = _delivery(tmp_path)
        self.backend = FilesystemProjectArtifactBackend(tmp_path / "project-store")
        payload = _payload(self.lease, b"retention bytes")
        self.artifact = ArtifactStore(self.state).upload_artifact(
            self.worker, self.lease["id"], "upload", canonical_digest(payload), payload
        )[1]
        request = {
            "expected_epoch": self.lease["epoch"],
            "operation_id": payload["operation_id"],
            "artifacts": [
                {
                    "id": self.artifact["id"],
                    "sha256": self.artifact["sha256"],
                    "size": self.artifact["size"],
                }
            ],
        }
        ProjectArtifactStore(self.state, self.backend).commit_project_artifacts(
            self.worker, self.lease["id"], "place", canonical_digest(request), request
        )
        self.project_id = self.grant["project_id"]
        self.operation_id = payload["operation_id"]
        if age:
            self.sql(
                "UPDATE project_artifact_retention SET created_at=?, activity_at=?", (OLD, OLD)
            )

    def sql(self, statement: str, args: tuple = ()) -> None:
        with sqlite3.connect(self.state / "tessera.sqlite3") as connection:
            connection.execute(statement, args)

    def manifest(self, manifest_id: str = "manifest-ref") -> None:
        self.sql(
            "INSERT INTO artifact_manifests (id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,operation_id,plan_digest,artifacts_json,total_bytes,manifest_digest,created_at,status,classification,late) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                manifest_id,
                self.project_id,
                self.grant["task_id"],
                self.grant["id"],
                self.lease["id"],
                self.worker["id"],
                self.lease["epoch"],
                self.operation_id,
                "0" * 64,
                '[{"id":"' + self.artifact["id"] + '"}]',
                self.artifact["size"],
                "1" * 64,
                "now",
                "quarantined",
                "unclassified",
                0,
            ),
        )

    def candidate(self):
        found, _ = list_artifact_candidates(self.state, POLICY, now=NOW)
        assert len(found) == 1
        return found[0]


def test_old_unreferenced_artifact_is_selected(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    candidate = fixture.candidate()
    assert candidate.decision == "delete" and candidate.reasons == ["max_age"]
    assert candidate.kind == "artifact" and candidate.project_id == fixture.project_id
    assert (
        candidate.ref == fixture.artifact["id"] and candidate.sha256 == fixture.artifact["sha256"]
    )
    assert candidate.size_bytes == fixture.artifact["size"]
    assert not candidate.path.startswith("/")


def test_manifest_reference_is_kept(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.manifest()
    candidate = fixture.candidate()
    assert candidate.decision == "keep" and "referenced" in candidate.reasons


def test_experiment_evidence_reference_is_kept(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.sql(
        "INSERT INTO experiment_artifact_evidence_references (id,artifact_id,project_id,attempt_id,measurement_id,reference_kind,created_at) VALUES ('ref',?,?,'attempt',NULL,'attempt_artifact','now')",
        (fixture.artifact["id"], fixture.project_id),
    )
    assert fixture.candidate().reasons == ["referenced"]


def test_malformed_manifest_fails_closed(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.manifest()
    fixture.sql("UPDATE artifact_manifests SET artifacts_json='not json'")
    candidate = fixture.candidate()
    assert candidate.decision == "keep" and "referenced" in candidate.reasons


def test_held_artifact_is_kept(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.sql("UPDATE project_artifact_retention SET retention_class='held'")
    candidate = fixture.candidate()
    assert candidate.decision == "keep" and candidate.reasons == ["held"]


def test_checkpoint_artifact_is_kept(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.manifest()
    fixture.sql(
        "INSERT INTO execution_result_checkpoints (id,project_id,task_id,grant_id,lease_id,operation_id,manifest_id,manifest_digest,grant_revision,epoch,validation_contract,validation_json,validation_digest,commit_digest,committed_at,committed_by_principal_id,state) VALUES ('cp',?,?,?,?,?,?,?,1,1,'synthetic-declared-output-v1','{}',?,?,'now',?,'committed_quarantined')",
        (
            fixture.project_id,
            fixture.grant["task_id"],
            fixture.grant["id"],
            fixture.lease["id"],
            fixture.operation_id,
            "manifest-ref",
            "1" * 64,
            "2" * 64,
            "3" * 64,
            fixture.owner["id"],
        ),
    )
    assert {"referenced", "checkpoint"} <= set(fixture.candidate().reasons)


def test_lineage_reference_is_kept_by_id_and_by_digest(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    insert = "INSERT INTO artifact_lineage_nodes (id,org_id,project_id,kind,sha256,label,origin,artifact_id,created_by_principal_id,created_at) VALUES ('node',?,?,'dataset',?,'x','external_import',?,?,'now')"
    org_id = fixture.owner["org_id"]
    fixture.sql(
        insert, (org_id, fixture.project_id, "f" * 64, fixture.artifact["id"], fixture.owner["id"])
    )
    assert fixture.candidate().reasons == ["lineage"]
    fixture.sql("DROP TRIGGER artifact_lineage_nodes_append_only_update")
    fixture.sql("DROP TRIGGER artifact_lineage_nodes_append_only_delete")
    fixture.sql(
        "UPDATE artifact_lineage_nodes SET artifact_id=NULL, sha256=?",
        (fixture.artifact["sha256"],),
    )
    assert fixture.candidate().reasons == ["lineage"]


def test_unreleased_disposition_hold_is_kept(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.manifest()
    fixture.sql(
        "INSERT INTO artifact_dispositions (id,manifest_id,manifest_digest,revision,disposition,rationale,actor_principal_id,created_at) VALUES ('d','manifest-ref',?,1,'retain_quarantined','review',?,'now')",
        ("1" * 64, fixture.owner["id"]),
    )
    assert "unreleased_hold" in fixture.candidate().reasons


def test_degraded_and_deleted_states_are_kept(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.sql("UPDATE project_artifact_retention SET state='degraded'")
    assert fixture.candidate().reasons == ["degraded"]
    fixture.sql("UPDATE project_artifact_retention SET state='deleted'")
    assert "deleted" in fixture.candidate().reasons


def test_too_new_is_kept(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, age=False)
    fixture.sql("UPDATE project_artifact_retention SET activity_at='2026-05-30T00:00:00Z'")
    candidate = fixture.candidate()
    assert candidate.decision == "keep" and candidate.reasons == ["too_new"]


def test_no_age_knob_selects_nothing(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    found, skipped = list_artifact_candidates(fixture.state, RetentionPolicy(), now=NOW)
    assert skipped == [] and [c.decision for c in found] == ["keep"]
    assert found[0].reasons == ["no_age_limit"]


def test_delete_refuses_when_referenced_after_planning(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    candidate = fixture.candidate()
    assert candidate.decision == "delete"
    fixture.manifest()
    with pytest.raises(Conflict):
        delete_artifact(fixture.state, fixture.backend, candidate, run_id="run-1")
    with sqlite3.connect(fixture.state / "tessera.sqlite3") as connection:
        state = connection.execute("SELECT state FROM project_artifact_retention").fetchone()[0]
    assert state == "active"
    assert fixture.backend.inspect(
        fixture.owner["org_id"],
        fixture.project_id,
        candidate.ref,
        candidate.sha256,
        candidate.size_bytes,
    )


def test_delete_refuses_lineage_reference_added_after_planning(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    candidate = fixture.candidate()
    fixture.sql(
        "INSERT INTO artifact_lineage_nodes (id,org_id,project_id,kind,sha256,label,origin,artifact_id,created_by_principal_id,created_at) VALUES ('node',?,?,'dataset',?,'x','external_import',NULL,?,'now')",
        (fixture.owner["org_id"], fixture.project_id, candidate.sha256, fixture.owner["id"]),
    )
    with pytest.raises(Conflict, match="lineage"):
        delete_artifact(fixture.state, fixture.backend, candidate, run_id="run-1")


def test_delete_removes_bytes_and_marks_row_deleted(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    candidate = fixture.candidate()
    assert fixture.backend.inspect(
        fixture.owner["org_id"],
        fixture.project_id,
        candidate.ref,
        candidate.sha256,
        candidate.size_bytes,
    )
    receipt = delete_artifact(fixture.state, fixture.backend, candidate, run_id="run-1")
    assert receipt == {
        "ref": candidate.ref,
        "project_id": fixture.project_id,
        "size_bytes": candidate.size_bytes,
        "sha256": candidate.sha256,
        "state": "deleted",
    }
    assert (
        fixture.backend.inspect(
            fixture.owner["org_id"],
            fixture.project_id,
            candidate.ref,
            candidate.sha256,
            candidate.size_bytes,
        )
        is None
    )
    with sqlite3.connect(fixture.state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT state FROM project_artifact_retention").fetchone()[0]
            == "deleted"
        )
    # A retry of the same run is idempotent and a later listing keeps the deleted row.
    assert (
        delete_artifact(fixture.state, fixture.backend, candidate, run_id="run-1")["state"]
        == "deleted"
    )
    assert "deleted" in fixture.candidate().reasons


def test_delete_rejects_keep_candidate(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.sql("UPDATE project_artifact_retention SET retention_class='held'")
    with pytest.raises(ValueError):
        delete_artifact(fixture.state, fixture.backend, fixture.candidate(), run_id="run-1")


def test_campaign_stage_io_by_digest_keeps_the_artifact(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.sql(
        "INSERT INTO campaign_stage_io (id,org_id,project_id,campaign_id,stage_key,direction,name,kind,sha256,size_bytes,data_class,created_at) VALUES ('io1','org',?,'camp','stage','input','in','artifact',?,1,'public','now')",
        (fixture.project_id, fixture.artifact["sha256"]),
    )
    candidate = fixture.candidate()
    assert candidate.decision == "keep" and "campaign" in candidate.reasons


def test_campaign_gate_row_keeps_the_artifact(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.sql(
        "INSERT INTO gate_ingested_rows (id,org_id,project_id,campaign_id,stage_key,attempt_id,artifact_id,artifact_sha256,lock_id,membership_digest,variant_id,seed,metric,unit,partition,sample_count,value,created_at,created_by_principal_id) VALUES ('gate_ingested_1','org',?,'camp','stage','attempt',?,?,'lock','m','variant',0,'metric','unit','p',1,1.0,'now','principal')",
        (fixture.project_id, fixture.artifact["id"], fixture.artifact["sha256"]),
    )
    candidate = fixture.candidate()
    assert candidate.decision == "keep" and "referenced" in candidate.reasons


def test_a_delete_refused_after_the_tombstone_says_so(tmp_path: Path, monkeypatch) -> None:
    from tessera_control import storage_retention_artifacts as module

    fixture = Fixture(tmp_path)
    candidate = fixture.candidate()

    def refuse(*args, **kwargs):
        raise Conflict("Referenced project artifacts cannot be deleted.")

    monkeypatch.setattr(module._SystemRetentionStore, "reconcile_delete", refuse)
    with pytest.raises(module.RetentionDeleteError, match="refused after tombstone") as caught:
        delete_artifact(fixture.state, fixture.backend, candidate, run_id="run")
    assert caught.value.tombstoned is True
    assert "Referenced project artifacts" in str(caught.value)


def test_run_receipts_an_artifact_refused_after_its_tombstone_without_raw_error_text(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from tessera_control import storage_retention
    from tessera_control import storage_retention_artifacts as module
    from tessera_control.storage_retention_models import POLICY_FILE

    fixture = Fixture(tmp_path)
    (fixture.state / POLICY_FILE).write_text(json.dumps({"artifact_max_age_days": 30}))
    plan = storage_retention.run(fixture.state, mode="dry_run", trigger="manual")
    assert plan["plan"]["candidates"][0]["decision"] == "delete"

    def refuse(*args, **kwargs):
        raise OSError("/private/path/that/must/not/appear")

    monkeypatch.setattr(module._SystemRetentionStore, "reconcile_delete", refuse)
    done = storage_retention.run(
        fixture.state, mode="apply", trigger="manual", expect_plan_digest=plan["plan_digest"]
    )
    (failed,) = done["failed"]
    assert failed["state_after"] == "tombstoned" and done["deleted"] == []
    assert "OSError" in failed["reason"] and "/private/path" not in json.dumps(done)
