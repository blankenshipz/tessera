"""Actual registered temporary outputs and HTTP/SQLite joins; no provider execution."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_artifact_manifests import _body, _completed_outputs
from test_goal_to_decision import _design

from tessera_control.agent_provenance import (
    create_agent_provenance_schema,
    project_agent_notebook,
    project_external_agent_notebook,
)
from tessera_control.agent_provenance_models import AppendAgentProvenance
from tessera_control.agent_provenance_outputs import record_output_provider_binding
from tessera_control.api import create_app
from tessera_control.artifact_custody import ArtifactCustodyStore
from tessera_control.artifact_dispositions import ArtifactDispositionStore
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.execution import ExecutionStore
from tessera_control.experiments import ExperimentStore
from tessera_control.external_agents import ExternalAgentStore
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import Conflict, Forbidden, Store, _connect, canonical_digest

JOB = {
    "cluster_uid": "cluster-fixture",
    "namespace_uid": "namespace-fixture",
    "job_uid": "job-fixture",
}


@pytest.fixture
def output(tmp_path):
    # Both byte delivery paths, immutable worker manifest and control transitions
    # are the existing production store commands operating on temporary state.
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": first["operation_id"],
        "artifacts": [
            {name: artifact[name] for name in ("id", "sha256", "size")}
            for artifact in (first, second)
        ],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(placement), placement
    )
    store = Store(state)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    human = store.authenticate(token)
    execution = ExecutionStore(state)
    grant = execution.get_execution_grant(human, first["grant_id"])
    records = ExperimentStore(state)
    design = _design(grant=grant)
    experiment = records.create_experiment(
        human, first["project_id"], "experiment", canonical_digest(design), design
    )[1]
    variant = experiment["design"]["variants"][0]
    admission = {
        "expected_variant_revision": variant["revision"],
        "expected_design_revision": experiment["current_design_revision"],
        "grant_id": grant["id"],
    }
    attempt = records.admit_attempt(
        human, variant["id"], "attempt", canonical_digest(admission), admission
    )[1]
    package = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "package", "package", lease["epoch"]
    )[1]
    assert package["operation"]["operation_id"] == _body(lease, first, second)["operation_id"]
    for index in range(2):
        execution.renew_execution_lease(
            worker, lease["id"], f"renew_{index}", f"renew_{index}", lease["epoch"]
        )
    cancel = {"expected_revision": grant["revision"]}
    execution.revoke_execution_grant(
        human, grant["id"], "cancel", canonical_digest(cancel), grant["revision"]
    )
    provider_id = (
        "kubernetes-v1:"
        + hashlib.sha256(
            json.dumps(
                JOB | {"version": "kubernetes-provider-v1"}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    observation = {
        "expected_epoch": lease["epoch"],
        "operation_id": package["operation"]["operation_id"],
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": provider_id,
        "termination_confirmed": True,
    }
    execution.report_execution_observation(
        worker, lease["id"], "terminal", canonical_digest(observation), observation
    )
    attempt = records.get_experiment(human, experiment["id"])["admitted_attempts"][0]
    resolution = {
        "expected_revision": attempt["revision"],
        "outcome": "cancelled",
        "reason": "Synthetic fixture retains late output after an attributed stop.",
    }
    records.resolve_admitted_attempt(
        human, attempt["id"], "resolve", canonical_digest(resolution), resolution
    )
    manifest_body = _body(lease, first, second)
    manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(manifest_body), manifest_body
    )[1]
    assert manifest["late"] is True
    review = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 0,
        "disposition": "retain_quarantined",
        "rationale": "Synthetic retained quarantine.",
    }
    ArtifactDispositionStore(state).record_disposition(
        human, manifest["id"], "retain", canonical_digest(review), review
    )
    release = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 0,
        "action": "release",
        "rationale": "Synthetic metadata disclosure.",
    }
    ArtifactCustodyStore(state).record_release(
        human, manifest["id"], "release", canonical_digest(release), release
    )
    agent_token = "synthetic-output-notebook-agent"
    enrollment = {
        "kind": "agent",
        "audience": "control",
        "project_id": first["project_id"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "external_agent": {
            "actions": ["context:read", "notebook:read", "provenance:append"],
            "destination": "critic.local",
        },
    }
    credential = store.issue_credential(
        human, "enroll", canonical_digest(enrollment), enrollment, agent_token
    )[1]
    agent = store.authenticate(agent_token) | {"agent_destination": "critic.local"}
    disclosure = {
        "credential_id": credential["credential_id"],
        "destination": "critic.local",
        "manifest_id": manifest["id"],
        "expected_manifest_digest": manifest["manifest_digest"],
        "artifact_id": first["id"],
        "max_bytes": 1024,
        "classification": "project",
        "disclosure_approved": True,
        "title": "Synthetic exact artifact context",
    }
    ExternalAgentStore(state).create_context(
        human, first["project_id"], "disclose", disclosure, artifact=True
    )
    with _connect(state / "tessera.sqlite3") as conn:
        create_agent_provenance_schema(conn)
    return {
        "state": state,
        "human": human,
        "agent": agent,
        "agent_token": agent_token,
        "worker": worker,
        "manifest": manifest,
        "first": first,
        "second": second,
        "attempt": attempt,
        "experiment": experiment,
        "release": release,
    }


def notebook(data, agent=False, **kwargs):
    with _connect(data["state"] / "tessera.sqlite3") as conn:
        conn.execute("BEGIN")
        return (project_external_agent_notebook if agent else project_agent_notebook)(
            conn, data["agent"] if agent else data["human"], data["first"]["project_id"], **kwargs
        )


def joined(data, agent=False):
    return next(
        event["output"]
        for event in notebook(data, agent)["control_events"]
        if event["event_type"] == "worker.artifact_manifest_quarantined"
    )


def payload(data, proof=True):
    manifest = data["manifest"]
    return {
        "record": {
            "record_id": "output_report_1",
            "kind": "attempt",
            "outcome": "late",
            "correlation_id": "output_collection_1",
            "references": {
                "output_manifest_id": manifest["id"],
                "artifact_id": data["first"]["id"],
                "attempt_id": data["attempt"]["id"],
                "provider_job_uid": JOB["job_uid"] if proof else None,
            },
        },
        "output_proof": {
            "output_manifest_id": manifest["id"],
            "manifest_digest": manifest["manifest_digest"],
            "provider_job": JOB if proof else None,
        },
    }


def append(data, body=None, key="output_append_1"):
    with TestClient(create_app(data["state"])) as client:
        return client.post(
            f"/projects/{data['first']['project_id']}/agent-provenance",
            json=body or payload(data),
            headers={
                "Authorization": "Bearer " + data["agent_token"],
                "X-Agent-Destination": "critic.local",
                "Idempotency-Key": key,
            },
        )


def test_historical_manifest_event_joins_late_cancelled_hashes_without_provider_invention(output):
    result = joined(output)
    assert result["output_manifest_id"] == output["manifest"]["id"]
    assert result["manifest_digest"] == output["manifest"]["manifest_digest"]
    assert result["operation_id"] == output["manifest"]["operation_id"]
    assert result["attempt_id"] == output["attempt"]["id"]
    assert result["late"] and result["collection_outcome"] == "late"
    assert result["status"] == "quarantined" and result["control_outcome"] == "cancelled"
    assert result["disposition"] == "retain_quarantined" and result["custody_state"] == "released"
    assert (
        result["provider_job_uid"] is None
        and result["provider_identity_status"] == "job_uid_unknown"
    )
    assert {entry["sha256"] for entry in result["artifacts"]} == {
        output["first"]["sha256"],
        output["second"]["sha256"],
    }
    encoded = json.dumps(notebook(output))
    assert (
        "first.txt" not in encoded and "content_base64" not in encoded and "chunked" not in encoded
    )
    assert result["scientific_validity"] == "not_assessed"
    assert result["timeline"]["renewal_acknowledgments"]["count"] == 2
    assert (
        result["timeline"]["first_recorded_cancellation_request"]["actor_principal_id"]
        == output["human"]["id"]
    )
    assert result["timeline"]["cancellation_reason"] == "not_recorded_in_closed_metadata"
    assert "unregistered_history_unknown" in notebook(output)["output_coverage"]
    owner_token = (output["state"] / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with TestClient(create_app(output["state"])) as client:
        exported = client.get(
            f"/experiments/{output['experiment']['id']}/export",
            headers={"Authorization": "Bearer " + owner_token},
        )
    assert exported.status_code == 200
    export_notebook = exported.json()["agent_notebook"]
    assert any(
        event.get("output", {}).get("output_manifest_id") == output["manifest"]["id"]
        and event["output"]["control_outcome"] == "cancelled"
        for event in export_notebook["control_events"]
    )
    assert "content_base64" not in json.dumps(export_notebook)


def test_http_output_append_validates_job_hash_link_and_replays(output):
    response = append(output)
    assert response.status_code == 201, response.text
    assert response.json()["references"]["provider_job_uid"] == JOB["job_uid"]
    assert (
        response.json()["references"]
        == AppendAgentProvenance.model_validate(payload(output)).model_dump(mode="json")["record"][
            "references"
        ]
    )
    assert append(output).json() == response.json()
    assert append(output, key="new-key").json() == response.json()
    result = joined(output)
    assert result["provider_job_uid"] == JOB["job_uid"]
    assert result["provider_identity_status"] == "recorded_uid_hash_match"
    agent_result = joined(output, agent=True)
    assert [entry["artifact_id"] for entry in agent_result["artifacts"]] == [output["first"]["id"]]
    assert agent_result["completeness"] == "restricted_output_metadata"


@pytest.mark.parametrize("change", ["digest", "job", "manifest", "artifact", "operation"])
def test_output_collector_denies_bad_refs_atomically(output, change):
    body = payload(output)
    if change == "digest":
        body["output_proof"]["manifest_digest"] = "a" * 64
    elif change == "job":
        body["output_proof"]["provider_job"] = JOB | {"job_uid": "foreign-job"}
    elif change == "manifest":
        body["record"]["references"]["output_manifest_id"] = "foreign_manifest"
        body["output_proof"]["output_manifest_id"] = "foreign_manifest"
    elif change == "artifact":
        body["record"]["references"]["artifact_id"] = output["second"]["id"]
    else:
        body["record"]["references"]["operation_id"] = "a" * 32
    assert append(output, body).status_code in {403, 409}
    with _connect(output["state"] / "tessera.sqlite3") as conn:
        assert (
            conn.execute("SELECT count(*) FROM agent_output_provider_bindings").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_provenance WHERE record_id='output_report_1'"
            ).fetchone()[0]
            == 0
        )


def test_disclosure_withdrawal_filters_history_and_denies_replay(output):
    assert append(output).status_code == 201
    manifest = output["manifest"]
    body = output["release"] | {"expected_revision": 1, "action": "revoke"}
    ArtifactCustodyStore(output["state"]).record_release(
        output["human"], manifest["id"], "withdraw", canonical_digest(body), body
    )
    result = joined(output, agent=True)
    assert result["visibility"] == "withheld_current_disclosure"
    encoded = json.dumps(notebook(output, agent=True))
    assert output["first"]["sha256"] not in encoded
    assert output["second"]["sha256"] not in encoded
    assert manifest["manifest_digest"] not in encoded
    assert append(output).status_code == 403
    assert joined(output)["control_outcome"] == "cancelled"
    assert joined(output)["custody_state"] == "revoked"


def test_output_binding_fences_tenant_and_same_snapshot_pagination_and_caps(output):
    with _connect(output["state"] / "tessera.sqlite3") as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(Forbidden):
            record_output_provider_binding(
                conn,
                output["human"] | {"org_id": "foreign"},
                output["first"]["project_id"],
                output["manifest"]["id"],
                JOB,
            )
        binding = record_output_provider_binding(
            conn, output["worker"], output["first"]["project_id"], output["manifest"]["id"], JOB
        )
        assert binding["job_uid"] == JOB["job_uid"]
        full = project_agent_notebook(conn, output["human"], output["first"]["project_id"])
        page = project_agent_notebook(conn, output["human"], output["first"]["project_id"], limit=1)
        found = []
        while True:
            found.extend(event["cursor"] for event in page["control_events"])
            assert page["watermark"] == full["watermark"]
            if not page["has_more"]:
                break
            page = project_agent_notebook(
                conn, output["human"], output["first"]["project_id"], limit=1, **page["next"]
            )
        assert found == [event["cursor"] for event in full["control_events"]]
        with pytest.raises(Conflict):
            project_agent_notebook(
                conn, output["human"], output["first"]["project_id"], max_bytes=1024
            )


def test_on_time_manifest_is_not_execution_success_and_projection_never_reads_bytes(tmp_path):
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    human = Store(state).authenticate(
        (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    )
    body = _body(lease, first, second)
    manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "on_time", canonical_digest(body), body
    )[1]
    with _connect(state / "tessera.sqlite3") as conn:
        Store._event(
            conn,
            human,
            first["project_id"],
            None,
            "worker.artifact_manifest_quarantined",
            1,
            {
                "id": manifest["id"],
                "manifest_digest": "PRIVATE_EVENT_DIGEST",
                "prompt": "PRIVATE_EVENT_CONTENT",
                "provider_job_uid": "forged-job",
            },
        )
        conn.commit()
        conn.set_authorizer(
            lambda action, table, column, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "artifacts" and column == "content"
                else sqlite3.SQLITE_OK
            )
        )
        conn.execute("BEGIN")
        result = project_agent_notebook(conn, human, first["project_id"])
        metadata = next(
            event["output"]
            for event in result["control_events"]
            if event["event_type"] == "worker.artifact_manifest_quarantined"
        )
        assert metadata["late"] is False and metadata["collection_outcome"] == "on_time"
        assert metadata["control_outcome"] == "unknown"
        assert metadata["manifest_digest"] == manifest["manifest_digest"]
        assert metadata["provider_job_uid"] is None
        assert "PRIVATE_EVENT" not in json.dumps(result)
        assert "forged-job" not in json.dumps(result)
