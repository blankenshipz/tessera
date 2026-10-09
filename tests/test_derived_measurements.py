"""Derived observations bind retained bytes to a specific operation and attempt."""

import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers
from test_artifact_manifests import _body as _manifest_body
from test_artifacts import _payload, _two_path_delivery
from test_goal_to_decision import _command, _design, _operation
from test_result_checkpoints import _owner

from tessera_control.api import create_app
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.artifacts import ArtifactStore
from tessera_control.execution import ExecutionStore
from tessera_control.experiments import ExperimentStore
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.store import Conflict, canonical_digest


def _scenario(tmp_path):
    state, worker, lease = _two_path_delivery(tmp_path)
    owner = _owner(state)
    # Register the exact synthetic JSON bytes before the immutable manifest and
    # reconciliation. Placement and derived measurements must retain that identity.
    content = b'{"metrics":{"seconds":1.25,"count":0}}'
    artifacts = ArtifactStore(state)
    outputs = []
    for key, path, data in (("first", "first.txt", content), ("second", "second.txt", b"chunked")):
        payload = _payload(lease, data) | {"relative_path": path}
        _, output = artifacts.upload_artifact(
            worker, lease["id"], key, canonical_digest(payload), payload
        )
        outputs.append(output)
    artifact = outputs[0]
    manifest_body = _manifest_body(lease, *outputs)
    _, manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(manifest_body), manifest_body
    )
    entry = next(item for item in manifest["artifacts"] if item["id"] == artifact["id"])
    assert entry["sha256"] == artifact["sha256"] == hashlib.sha256(content).hexdigest()
    assert entry["size"] == artifact["size"] == len(content)
    execution = ExecutionStore(state)
    observation = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "synthetic-provider-operation",
        "termination_confirmed": True,
    }
    execution.report_execution_observation(
        worker, lease["id"], "exited", canonical_digest(observation), observation
    )
    grant = execution.get_execution_grant(owner, artifact["grant_id"])
    reconcile = {
        "expected_revision": grant["revision"],
        "settlement_cents": 0,
        "provider_terminated": True,
    }
    _, grant = execution.reconcile_execution_grant(
        owner, grant["id"], "reconcile", canonical_digest(reconcile), reconcile
    )
    _operation(state, owner, grant, lease, manifest["operation_id"])
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": manifest["operation_id"],
        "artifacts": [
            {
                "id": artifact["id"],
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        ],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "place-json", canonical_digest(placement), placement
    )
    records = ExperimentStore(state)
    experiment = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    baseline = next(x for x in experiment["design"]["variants"] if x["kind"] == "baseline")
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
    link = _command(
        records.link_attempt_artifacts,
        owner,
        attempt["id"],
        payload={
            "expected_revision": attempt["revision"],
            "artifact_ids": [artifact["id"]],
        },
    )
    return state, owner, records, experiment, attempt, link, artifact


def test_derived_json_measurement_api_replay_and_export(tmp_path):
    state, owner, records, experiment, attempt, link, artifact = _scenario(tmp_path)
    body = {
        "expected_revision": link["revision"],
        "name": "duration seconds",
        "artifact_id": artifact["id"],
        "json_pointer": "/metrics/seconds",
    }
    client = TestClient(create_app(state))
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "derived-duration",
    }
    url = f"/experiment-attempts/{attempt['id']}/derived-measurements"
    other = client.post(
        url,
        headers=_other_tenant_headers(state) | {"Idempotency-Key": "foreign-measurement"},
        json=body,
    )
    assert other.status_code == 404
    first = client.post(url, headers=headers, json=body)
    assert first.status_code == 201, first.text
    assert first.json()["value"] == {
        "number": 1.25,
        "derivation": "retained-json-number-v1",
        "json_pointer": "/metrics/seconds",
        "artifact_sha256": hashlib.sha256(b'{"metrics":{"seconds":1.25,"count":0}}').hexdigest(),
    }
    assert first.json()["scientific_validity"] == "not_assessed"
    assert client.post(url, headers=headers, json=body).json() == first.json()
    exported = records.export_experiment(owner, experiment["id"])
    assert exported["measurements"][0]["value"] == first.json()["value"]
    stale = dict(body, json_pointer="/metrics/count")
    assert client.post(url, headers=headers, json=stale).status_code == 409
    assert (
        client.post(url, headers=headers | {"Idempotency-Key": "stale"}, json=body).status_code
        == 409
    )


def test_derived_measurement_rejects_spoofing_and_invalid_custody(tmp_path):
    state, owner, records, _experiment, attempt, link, artifact = _scenario(tmp_path)
    body = {
        "expected_revision": link["revision"],
        "name": "duration",
        "artifact_id": artifact["id"],
        "json_pointer": "/metrics/seconds",
    }
    for invalid_pointer in ("/metrics/missing", "/metrics/~2", "/metrics"):
        with pytest.raises(Conflict):
            _command(
                records.derive_output_measurement,
                owner,
                attempt["id"],
                payload=body | {"json_pointer": invalid_pointer},
            )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE artifacts SET operation_id=? WHERE id=?", ("f" * 32, artifact["id"])
        )
    with pytest.raises(Conflict, match="provenance"):
        _command(records.derive_output_measurement, owner, attempt["id"], payload=body)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE artifacts SET operation_id=? WHERE id=?",
            (attempt["operation_id"], artifact["id"]),
        )
        connection.execute(
            "UPDATE project_artifact_retention SET state='degraded' WHERE artifact_id=?",
            (artifact["id"],),
        )
    with pytest.raises(Conflict):
        _command(records.derive_output_measurement, owner, attempt["id"], payload=body)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE project_artifact_retention SET state='active' WHERE artifact_id=?",
            (artifact["id"],),
        )
        connection.execute("UPDATE artifacts SET sha256=? WHERE id=?", ("0" * 64, artifact["id"]))
    with pytest.raises(Conflict, match="placement"):
        _command(records.derive_output_measurement, owner, attempt["id"], payload=body)


def test_derived_measurement_rejects_corrupt_placed_output(tmp_path):
    state, owner, records, _experiment, attempt, link, artifact = _scenario(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        placement = connection.execute(
            "SELECT tenant_namespace,project_namespace,object_key "
            "FROM project_artifact_placements WHERE artifact_id=?",
            (artifact["id"],),
        ).fetchone()
    path = state / "project-artifacts" / placement[0] / placement[1] / placement[2]
    path.write_bytes(b'{"metrics":{"seconds":9.99,"count":0}}')
    with pytest.raises(Conflict, match="custody integrity"):
        _command(
            records.derive_output_measurement,
            owner,
            attempt["id"],
            payload={
                "expected_revision": link["revision"],
                "name": "duration",
                "artifact_id": artifact["id"],
                "json_pointer": "/metrics/seconds",
            },
        )
