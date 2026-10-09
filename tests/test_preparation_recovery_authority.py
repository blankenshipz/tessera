"""Isolated original journals/API authority; no live Kubernetes writes or calls."""

import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_dispatch import _dispatch, _owner_headers
from test_kubernetes_never_submitted import _expire_worker
from test_kubernetes_preparation_recovery import fixture

from tessera_control.api import create_app
from tessera_control.store import _token_hash
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_preparation_recovery import attest_preparation_failure


def prepared(tmp_path):
    package, collector, journal, _ = fixture(tmp_path)
    state = tmp_path / "state"
    client = TestClient(create_app(state))
    dispatch = _dispatch(
        client,
        state,
        {
            "package": package.package,
            "admitted_attempt_id": package.binding.attempt_id,
        },
        cluster_identity_digest(collector.client.cluster),
    )
    worker = {"Authorization": "Bearer worker-token"}
    claim = client.post(
        f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
        json={
            "target_digest": dispatch["target_digest"],
            "profile_version": collector.client.cluster.profile,
        },
        headers=worker | {"Idempotency-Key": "claim"},
    )
    assert claim.status_code == 200, claim.text
    owner = _owner_headers(state)
    grant = client.get(f"/execution-grants/{dispatch['grant_id']}", headers=owner).json()
    cancelled = client.post(
        f"/execution-grants/{grant['id']}/revoke",
        json={
            "expected_revision": grant["revision"],
        },
        headers=owner | {"Idempotency-Key": "human-cancel"},
    )
    assert cancelled.status_code == 200, cancelled.text
    grant = cancelled.json()
    # Retain the synthetic original helper-create UID before recovery readbacks;
    # a missing UID may not be replaced with a current Pod lookup in production.
    with sqlite3.connect(collector.journal) as db:
        db.execute("UPDATE collectors SET uid='prepared-owned'")
        original_uid = db.execute("SELECT uid FROM collectors").fetchone()[0]
    # This isolated fixture has no live submitting process; explicitly assert
    # that boundary before generating proof, rather than adding it afterward.
    proof = attest_preparation_failure(
        package, collector, journal, exclusive_stopped_worker=True
    ) | {
        "expected_revision": grant["revision"],
        "worker_attests_original_exclusive_stopped": True,
        "preparation_journal_pod_uid": original_uid,
    }
    return client, state, package, journal, grant, worker, owner, proof


def attest(client, grant, worker, proof, key="attest-preparation"):
    return client.post(
        f"/worker/execution-grants/{grant['id']}/attest-preparation-not-submitted",
        json=proof,
        headers=worker | {"Idempotency-Key": key},
    )


def release(client, grant, owner, proof, digest, key="release-preparation"):
    return client.post(
        f"/execution-grants/{grant['id']}/reconcile-attested-preparation-not-submitted",
        json={
            "expected_revision": grant["revision"],
            "operation_id": proof["operation_id"],
            "attestation_digest": digest,
        },
        headers=owner | {"Idempotency-Key": key},
    )


def test_distinct_worker_attestation_then_original_human_release_preserves_true_history(tmp_path):
    client, state, package, journal, grant, worker, owner, proof = prepared(tmp_path)
    held = grant["max_exposure_cents"]
    missing = release(client, grant, owner, proof, "f" * 64, "missing")
    assert missing.status_code == 409
    evidence = attest(client, grant, worker, proof)
    assert evidence.status_code == 201, evidence.text
    digest = evidence.json()["attestation_digest"]
    assert evidence.json()["authority_released"] is False
    assert attest(client, grant, worker, proof).json() == evidence.json()
    assert release(client, grant, worker, proof, digest, "worker-release").status_code in (401, 403)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0] == held
    result = release(client, grant, owner, proof, digest)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "reconciled"
    assert result.json()["settlement_cents"] == 0
    assert result.json()["cancellation"]["state"] != "confirmed_termination"
    assert release(client, grant, owner, proof, digest).json() == result.json()
    assert release(client, grant, owner, proof, digest, "new-key").status_code == 409
    assert attest(client, grant, worker, proof, "after-release").status_code == 409
    with sqlite3.connect(journal) as db:
        assert db.execute(
            "SELECT create_intent,cancellation_requested,job_uid FROM kubernetes_jobs"
        ).fetchone() == (1, 1, None)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT held_cents,settled_cents FROM execution_project_limits"
        ).fetchone() == (0, 0)
        assert db.execute(
            "SELECT provider_id FROM external_operations WHERE operation_id=?",
            (package.operation_id,),
        ).fetchone() == (None,)
        assert db.execute("SELECT COUNT(*) FROM external_operation_receipts").fetchone()[0] == 0
        events = db.execute(
            "SELECT event_type,payload_json FROM events "
            "WHERE event_type LIKE '%preparation_not_submitted%' ORDER BY cursor"
        ).fetchall()
        assert len(events) == 2
        report = json.loads(events[0][1])
        assert report["attestation"]["create_intent"] is True
        assert report["historical_create_intent"] is True
        assert (
            report["execution_object_selector"] == "tessera.dev/operation=" + package.operation_id
        )
        assert (
            report["helper_object_selector"]
            == "tessera.dev/output-operation=" + package.operation_id
        )
        assert report["preparation_pod_name"] == "tessera-output-prepare-" + package.operation_id
        assert all(
            json.loads(payload)["provider_termination_inferred"] is False for _, payload in events
        )


def test_distinct_schema_rejects_broad_false_malformed_stale_and_old_path_override(tmp_path):
    client, state, _, _, grant, worker, owner, proof = prepared(tmp_path)
    for field, value in (
        ("create_intent", False),
        ("manifest_not_generated", True),
        ("job_uid", "workload-uid"),
        ("operation_pods_absent", False),
        ("preparation_receipt_absent", False),
        ("preparation_manifest_digest", "invalid"),
        ("preparation_journal_pod_uid", "replaced-uid"),
        ("worker_attests_original_exclusive_stopped", False),
        ("proof_kind", "pre_manifest"),
        ("preparation_gate", "arbitrary-gate"),
    ):
        response = attest(client, grant, worker, proof | {field: value}, "malformed-" + field)
        assert response.status_code == 422, (field, response.text)
    for field, value in (
        ("expected_revision", grant["revision"] + 1),
        ("target_digest", "f" * 64),
        ("package_digest", "f" * 64),
        ("epoch", proof["epoch"] + 1),
        ("operation_id", "f" * 32),
        ("lease_id", "foreign-lease"),
        ("job_name", "wrong-job"),
        ("observed_at", (datetime.now(UTC) - timedelta(seconds=90)).isoformat()),
    ):
        response = attest(client, grant, worker, proof | {field: value}, "mismatch-" + field)
        assert response.status_code == 409, (field, response.text)
    assert (
        client.post(
            f"/worker/execution-grants/{grant['id']}/attest-never-submitted",
            json=proof,
            headers=worker | {"Idempotency-Key": "old-worker"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/execution-grants/{grant['id']}/reconcile-owner-pre-manifest-no-start",
            json=proof | {"owner_attests_original_exclusive_journal": True},
            headers=owner | {"Idempotency-Key": "old-owner"},
        ).status_code
        == 422
    )
    evidence = attest(client, grant, worker, proof)
    assert evidence.status_code == 201
    assert attest(client, grant, worker, proof | {"namespace_uid": "changed"}).status_code == 409
    assert (
        attest(
            client,
            grant,
            worker,
            proof
            | {
                "preparation_pod_uid": "replacement",
                "preparation_journal_pod_uid": "replacement",
            },
            "replace-original-helper",
        ).status_code
        == 409
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type LIKE '%preparation_not_submitted%' "
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM idempotency WHERE idempotency_key LIKE 'malformed-%' "
                "OR idempotency_key LIKE 'mismatch-%'"
            ).fetchone()[0]
            == 0
        )


def test_wrong_worker_foreign_tenant_nonowner_and_expired_identity_cannot_release(tmp_path):
    client, state, _, _, grant, worker, owner, proof = prepared(tmp_path)
    token = secrets.token_urlsafe(32)
    issued = client.post(
        "/credentials",
        headers=owner | {"Idempotency-Key": "other-worker"},
        json={
            "token": token,
            "kind": "worker",
            "audience": "worker",
            "project_id": grant["project_id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert issued.status_code == 201, issued.text
    assert (
        attest(
            client, grant, {"Authorization": "Bearer " + token}, proof, "wrong-worker"
        ).status_code
        == 403
    )
    human_token = secrets.token_urlsafe(32)
    issued = client.post(
        "/credentials",
        headers=owner | {"Idempotency-Key": "other-human"},
        json={
            "token": human_token,
            "kind": "human_session",
            "audience": "control",
            "project_id": grant["project_id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert issued.status_code == 201
    evidence = attest(client, grant, worker, proof)
    assert evidence.status_code == 201
    digest = evidence.json()["attestation_digest"]
    assert (
        release(
            client, grant, {"Authorization": "Bearer " + human_token}, proof, digest, "not-owner"
        ).status_code
        == 403
    )
    foreign_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("INSERT INTO organizations VALUES ('foreign-org','foreign',?)", (now,))
        db.execute(
            "INSERT INTO principals VALUES ('foreign-human','foreign-org','human',?,NULL,?)",
            (_token_hash(foreign_token), now),
        )
        db.execute(
            "INSERT INTO credentials VALUES "
            "('foreign-credential','foreign-human','local_owner','control',"
            "NULL,NULL,NULL,NULL,?,'foreign-human')",
            (now,),
        )
    assert (
        release(
            client, grant, {"Authorization": "Bearer " + foreign_token}, proof, digest, "foreign"
        ).status_code
        == 404
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE worker_registry SET lifecycle_state='retired' WHERE worker_principal_id=?",
            (grant["worker_principal_id"],),
        )
    assert release(client, grant, owner, proof, digest, "retired").status_code == 403
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE worker_registry SET lifecycle_state='ready' WHERE worker_principal_id=?",
            (grant["worker_principal_id"],),
        )
    _expire_worker(state, grant)
    assert attest(client, grant, worker, proof).status_code == 401
    assert release(client, grant, owner, proof, digest, "expired").status_code == 403
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT status FROM execution_grants WHERE id=?", (grant["id"],)
        ).fetchone() == ("cancel_requested",)


def test_owner_rechecks_freshness_and_adverse_observation_before_accounting(tmp_path):
    client, state, _, _, grant, worker, owner, proof = prepared(tmp_path)
    evidence = attest(client, grant, worker, proof)
    assert evidence.status_code == 201
    digest = evidence.json()["attestation_digest"]
    store = client.app.state.execution
    original_now = store._now
    store._now = lambda: datetime.now(UTC) + timedelta(seconds=61)
    assert release(client, grant, owner, proof, digest, "stale").status_code == 409
    assert attest(client, grant, worker, proof).status_code == 409
    store._now = original_now
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE external_operations SET provider_id='adverse-provider' WHERE operation_id=?",
            (proof["operation_id"],),
        )
    assert release(client, grant, owner, proof, digest, "adverse").status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='execution.attested_preparation_not_submitted_reconciled'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM idempotency WHERE idempotency_key IN ('stale','adverse')"
            ).fetchone()[0]
            == 0
        )


def test_preparation_reconciliation_event_failure_rolls_back_accounting_and_idempotency(
    tmp_path, monkeypatch
):
    client, state, _, _, grant, worker, owner, proof = prepared(tmp_path)
    evidence = attest(client, grant, worker, proof)
    assert evidence.status_code == 201
    store = client.app.state.execution
    original_event = store._event

    def fail_reconciliation(connection, actor, project_id, task_id, event_type, revision, payload):
        if event_type == "execution.attested_preparation_not_submitted_reconciled":
            raise RuntimeError("synthetic event failure")
        return original_event(connection, actor, project_id, task_id, event_type, revision, payload)

    monkeypatch.setattr(store, "_event", fail_reconciliation)
    with pytest.raises(RuntimeError, match="synthetic event failure"):
        release(
            client, grant, owner, proof, evidence.json()["attestation_digest"], "atomic-failure"
        )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT held_cents,settled_cents FROM execution_project_limits"
        ).fetchone() == (grant["max_exposure_cents"], 0)
        assert db.execute(
            "SELECT status,revision FROM execution_grants WHERE id=?", (grant["id"],)
        ).fetchone() == ("cancel_requested", grant["revision"])
        assert (
            db.execute(
                "SELECT COUNT(*) FROM idempotency WHERE idempotency_key='atomic-failure'"
            ).fetchone()[0]
            == 0
        )
