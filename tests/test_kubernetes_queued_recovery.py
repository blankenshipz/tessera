"""Isolated never-claimed recovery; all provider readbacks here are synthetic."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_dispatch import _dispatch, _owner_headers
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes

from tessera_control.api import create_app
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_dispatch_service import KubernetesClaimIntentJournal
from tessera_worker.kubernetes_output_collector import KubernetesOutputCollector
from tessera_worker.kubernetes_queued_recovery import attest_never_claimed


def _fixture(tmp_path, *, claim=False, fence=True, mismatch=True):
    response, _, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client = TestClient(create_app(state))
    declared = cluster_identity_digest(
        replace(cluster, network_none_evidence=None) if mismatch else cluster
    )
    dispatch = _dispatch(client, state, response, declared)
    worker = {"Authorization": "Bearer worker-token"}
    if claim:
        claimed = client.post(
            f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
            json={"target_digest": declared, "profile_version": cluster.profile},
            headers=worker | {"Idempotency-Key": "claim"},
        )
        assert claimed.status_code == 200, claimed.text
    workspace = tmp_path / "original-worker"
    workspace.mkdir()
    claims = KubernetesClaimIntentJournal(workspace, "queue")
    claims.key()
    claims.complete()
    adapter = _FakeKubernetes(package, cluster).adapter(workspace / "kubernetes-jobs.sqlite3")
    KubernetesOutputCollector(
        None,
        replace(package.output, operation_scoped=True),
        "repo/tessera@sha256:" + "a" * 64,
        workspace / "output-pods.sqlite3",
    )
    grant = client.get(
        f"/execution-grants/{dispatch['grant_id']}", headers=_owner_headers(state)
    ).json()
    if fence:
        revoked = client.post(
            f"/execution-grants/{grant['id']}/revoke",
            json={"expected_revision": grant["revision"]},
            headers=_owner_headers(state) | {"Idempotency-Key": "revoke"},
        )
        assert revoked.status_code == 200, revoked.text
        grant = revoked.json()
    binding = {
        "expected_revision": grant["revision"] + int(not fence),
        "grant_epoch": grant["epoch"] + int(not fence),
        "dispatch_id": dispatch["id"],
        "operation_id": package.operation_id,
        "lease_id": dispatch["lease_id"],
        "epoch": dispatch["epoch"],
        "package_digest": dispatch["package_digest"],
        "target_digest": declared,
    }
    return client, state, cluster, package, workspace, adapter, grant, worker, binding


def _proof(workspace, binding, cluster, **overrides):
    readers = {
        "inspect_namespace": lambda name: {
            "metadata": {"name": name, "uid": cluster.namespace_uid}
        },
        "inspect_job": lambda _: None,
        "list_operation_jobs": lambda _: [],
        "list_operation_pods": lambda _: [],
        "list_helper_pods": lambda _: [],
    }
    return attest_never_claimed(
        workspace, binding, cluster, exclusive_stopped_worker=True, **(readers | overrides)
    )


def _attest(client, grant, worker, proof, key="attest"):
    return client.post(
        f"/worker/execution-grants/{grant['id']}/attest-never-claimed",
        json=proof,
        headers=worker | {"Idempotency-Key": key},
    )


def _release(client, state, grant, proof, receipt, key="release"):
    return client.post(
        f"/execution-grants/{grant['id']}/reconcile-attested-never-claimed",
        json={
            "expected_revision": grant["revision"],
            "operation_id": proof["operation_id"],
            "attestation_digest": receipt["attestation_digest"],
        },
        headers=_owner_headers(state) | {"Idempotency-Key": key},
    )


def test_never_claimed_mismatched_witness_release_replay_retains_history(tmp_path):
    client, state, cluster, package, root, adapter, grant, worker, binding = _fixture(tmp_path)
    proof = _proof(root, binding, cluster)
    assert proof["target_relation"] == "declared_without_network_witness"
    attested = _attest(client, grant, worker, proof)
    assert attested.status_code == 201, attested.text
    assert _attest(client, grant, worker, proof).json() == attested.json()
    released = _release(client, state, grant, proof, attested.json())
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "reconciled"
    assert released.json()["cancellation"]["state"] != "confirmed_termination"
    assert _release(client, state, grant, proof, attested.json()).json() == released.json()
    assert _release(client, state, grant, proof, attested.json(), "new-release").status_code == 409
    assert _attest(client, grant, worker, proof).status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        for table in (
            "execution_leases",
            "external_operations",
            "execution_runtime_packages",
            "kubernetes_dispatches",
            "research_experiment_admitted_attempts",
        ):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        assert db.execute(
            "SELECT held_cents,settled_cents FROM execution_project_limits"
        ).fetchone() == (0, 0)
        events = db.execute(
            "SELECT payload_json FROM events WHERE event_type="
            "'execution.attested_never_claimed_reconciled'"
        ).fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0])["outcome"] == "not_executed"
        assert json.loads(events[0][0])["provider_termination_inferred"] is False
        assert db.execute(
            "SELECT outcome,operational_state FROM research_experiment_admitted_attempts"
        ).fetchone() == (None, "interrupted")
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        adapter._bind(package)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_revision", 99),
        ("grant_epoch", 99),
        ("epoch", 99),
        ("lease_id", "different"),
        ("target_digest", "f" * 64),
        ("package_digest", "e" * 64),
        ("dispatch_id", "different"),
        ("namespace_uid_checked", False),
    ],
)
def test_stale_or_invalid_binding_denied(tmp_path, field, value):
    client, state, cluster, _, root, _, grant, worker, binding = _fixture(tmp_path)
    proof = _proof(root, binding, cluster)
    denied = _attest(client, grant, worker, proof | {field: value})
    assert denied.status_code in (409, 422), denied.text
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT status FROM execution_grants").fetchone()[0] == "cancel_requested"
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type='execution.kubernetes_never_claimed_attested'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "reader,value",
    [
        ("inspect_job", {"metadata": {"uid": "job"}}),
        ("list_operation_jobs", [{}]),
        ("list_operation_pods", [{}]),
        ("list_helper_pods", [{}]),
        ("list_operation_jobs", None),
        ("inspect_namespace", {"metadata": {"uid": "wrong"}}),
    ],
)
def test_provider_contradiction_keeps_durable_fence(tmp_path, reader, value):
    _, _, cluster, package, root, adapter, _, _, binding = _fixture(tmp_path)
    with pytest.raises(ValueError):
        _proof(root, binding, cluster, **{reader: lambda _: value})
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        adapter._bind(package)


def test_claimed_or_reclaimed_history_never_accepted(tmp_path):
    client, state, cluster, _, root, _, grant, worker, binding = _fixture(tmp_path, claim=True)
    proof = _proof(root, binding, cluster)
    assert _attest(client, grant, worker, proof).status_code == 409
    # Simulate a corrupted/nullified current projection: append-only claim event wins.
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE kubernetes_dispatches SET claimed_at=NULL,claimed_by_principal_id=NULL")
    assert _attest(client, grant, worker, proof, "null-claim").status_code == 409


def test_submit_race_vs_local_fence(tmp_path):
    _, _, cluster, package, root, adapter, _, _, binding = _fixture(tmp_path)

    def submit():
        try:
            adapter._bind(package)
            return "intent"
        except sqlite3.IntegrityError:
            return "fenced"

    def recover():
        try:
            _proof(root, binding, cluster)
            return "proof"
        except ValueError:
            return "unknown"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(submit), pool.submit(recover)
        results = (a.result(), b.result())
    assert results in (("intent", "unknown"), ("fenced", "proof"))


def test_credential_expiry_stale_evidence_and_accounting_rollback(tmp_path):
    client, state, cluster, _, root, _, grant, worker, binding = _fixture(tmp_path)
    proof = _proof(root, binding, cluster)
    stale = proof | {"observed_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()}
    assert _attest(client, grant, worker, stale, "stale").status_code == 409
    attested = _attest(client, grant, worker, proof)
    assert attested.status_code == 201, attested.text
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER deny_recovery BEFORE UPDATE ON execution_project_limits "
            "BEGIN SELECT RAISE(ABORT, 'test atomic failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        _release(client, state, grant, proof, attested.json())
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT status FROM execution_grants").fetchone()[0] == "cancel_requested"
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type='execution.attested_never_claimed_reconciled'"
            ).fetchone()[0]
            == 0
        )
        db.execute("DROP TRIGGER deny_recovery")
        db.execute(
            "UPDATE credentials SET expires_at=? WHERE principal_id=? AND audience='worker'",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), grant["worker_principal_id"]),
        )
    assert _attest(client, grant, worker, proof, "expired").status_code == 401
    assert _release(client, state, grant, proof, attested.json()).status_code in (401, 403)


def test_claim_vs_cancel_and_recovery_serializes_control_history(tmp_path):
    client, state, cluster, _, root, _, grant, worker, binding = _fixture(tmp_path, fence=False)

    def claim():
        return client.post(
            f"/worker/kubernetes-dispatches/{binding['dispatch_id']}/claim",
            json={"target_digest": binding["target_digest"], "profile_version": cluster.profile},
            headers=worker | {"Idempotency-Key": "racing-claim"},
        ).status_code

    def recover():
        revoked = client.post(
            f"/execution-grants/{grant['id']}/revoke",
            json={"expected_revision": grant["revision"]},
            headers=_owner_headers(state) | {"Idempotency-Key": "racing-revoke"},
        )
        assert revoked.status_code == 200, revoked.text
        proof = _proof(root, binding, cluster)
        return _attest(client, revoked.json(), worker, proof).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(claim), pool.submit(recover)
        result = (a.result(), b.result())
    assert result in ((200, 409), (409, 201))


def test_unknown_after_attestation_blocks_release_and_tenant_denied(tmp_path):
    from tessera_control.store import initialize_state

    client, state, cluster, package, root, _, grant, worker, binding = _fixture(tmp_path)
    proof = _proof(root, binding, cluster)
    receipt = _attest(client, grant, worker, proof)
    assert receipt.status_code == 201, receipt.text
    other = tmp_path / "other-org"
    initialize_state(other)
    assert _release(client, other, grant, proof, receipt.json()).status_code in (401, 403, 404)
    assert _attest(
        client, grant, _owner_headers(state), proof, "human-impersonation"
    ).status_code in (401, 403)
    observed = client.post(
        "/worker/observations",
        json={
            "lease_id": package.binding.lease_id,
            "operation_id": package.operation_id,
            "expected_epoch": package.binding.epoch,
            "observed_state": "unknown",
            "observed_at": datetime.now(UTC).isoformat(),
            "provider_id": None,
            "termination_confirmed": False,
        },
        headers=worker | {"Idempotency-Key": "unknown"},
    )
    assert observed.status_code in (200, 201), observed.text
    assert _release(client, state, grant, proof, receipt.json()).status_code == 409


@pytest.mark.parametrize("defect", ["missing", "empty", "descriptor", "adapter", "wrong_target"])
def test_original_journal_or_target_defects_are_unknown(tmp_path, defect):
    _, _, cluster, package, root, adapter, _, _, binding = _fixture(tmp_path)
    if defect == "missing":
        (root / "kubernetes-jobs.sqlite3").unlink()
    elif defect == "empty":
        with sqlite3.connect(root / "kubernetes-claims.sqlite3") as db:
            db.execute("DELETE FROM claim_intents")
    elif defect == "descriptor":
        KubernetesClaimIntentJournal(root, binding["dispatch_id"]).bind_descriptor(
            {"operation": package.operation_id}
        )
    elif defect == "adapter":
        adapter._bind(package)
    else:
        binding = binding | {"target_digest": "f" * 64}
    with pytest.raises(ValueError):
        _proof(root, binding, cluster)


def test_fences_deny_legacy_descriptor_and_helper_inserts_and_proof_rewrite(tmp_path):
    client, _, cluster, _, root, _, grant, worker, binding = _fixture(tmp_path)
    proof = _proof(root, binding, cluster)
    assert _attest(client, grant, worker, proof).status_code == 201
    assert (
        _attest(
            client, grant, worker, proof | {"actual_target_digest": "a" * 64}, "rewrite"
        ).status_code
        == 409
    )
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        KubernetesClaimIntentJournal(root, binding["dispatch_id"]).bind_descriptor(
            {"operation": binding["operation_id"]}
        )
    with sqlite3.connect(root / "output-pods.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError, match="fenced"):
            db.execute(
                "INSERT INTO collectors VALUES (?, '{}', NULL, NULL)",
                ("tessera-output-prepare-" + binding["operation_id"],),
            )


def test_human_notebook_joins_distinct_no_start_events_without_raw_attestation(tmp_path):
    client, state, cluster, _, root, _, grant, worker, binding = _fixture(tmp_path)
    proof = _proof(root, binding, cluster)
    receipt = _attest(client, grant, worker, proof)
    assert receipt.status_code == 201, receipt.text
    assert _release(client, state, grant, proof, receipt.json()).status_code == 200
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        experiment_id = db.execute(
            "SELECT experiment_id FROM research_experiment_admitted_attempts WHERE grant_id=?",
            (grant["id"],),
        ).fetchone()[0]
    exported = client.get(
        f"/experiments/{experiment_id}/export",
        headers=_owner_headers(state),
    )
    assert exported.status_code == 200, exported.text
    notebook = exported.json()["agent_notebook"]
    events = {event["event_type"]: event for event in notebook["control_events"]}
    for kind in (
        "execution.kubernetes_never_claimed_attested",
        "execution.attested_never_claimed_reconciled",
    ):
        assert events[kind]["references"]["operation_id"] == binding["operation_id"]
        assert events[kind]["references"]["grant_id"] == grant["id"]
        assert events[kind]["payload_projection"] == "opaque_references_only"
        assert "attestation" not in events[kind]
    assert notebook["execution_verification"] == "not_established_by_provenance"


def test_exact_target_supported_but_stale_consumption_still_holds_reservation(tmp_path):
    client, state, cluster, _, root, _, grant, worker, binding = _fixture(tmp_path, mismatch=False)
    proof = _proof(root, binding, cluster)
    assert proof["target_relation"] == "exact"
    receipt = _attest(client, grant, worker, proof)
    assert receipt.status_code == 201, receipt.text
    now = datetime.now(UTC)
    client.app.state.execution._now = lambda: now + timedelta(seconds=61)
    assert _release(client, state, grant, proof, receipt.json()).status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT status FROM execution_grants").fetchone()[0] == "cancel_requested"
