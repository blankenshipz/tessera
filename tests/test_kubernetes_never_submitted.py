"""Isolated control/worker no-start reconciliation; no Kubernetes provider calls."""

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
from tessera_control.store import _token_hash, canonical_digest, initialize_state
from tessera_worker.kubernetes_contracts import (
    KubernetesContractError,
    KubernetesJobJournal,
    KubernetesUnresolvedError,
    _job_manifest,
    cluster_identity_digest,
)


def _fixture(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client = TestClient(create_app(state))
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    worker_headers = {"Authorization": "Bearer worker-token"}
    claim = client.post(
        f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
        json={
            "target_digest": cluster_identity_digest(cluster),
            "profile_version": cluster.profile,
        },
        headers=worker_headers | {"Idempotency-Key": "claim"},
    )
    assert claim.status_code == 200, claim.text
    journal = KubernetesJobJournal(tmp_path / "original-worker" / "kubernetes.sqlite3")
    journal.request_cancel(
        package.binding,
        cluster,
        lambda _name: None,
        lambda _name, _uid: None,
        lambda _uid: [],
    )
    grant_id = dispatch["grant_id"]
    grant = client.get(f"/execution-grants/{grant_id}", headers=_owner_headers(state)).json()
    revoked = client.post(
        f"/execution-grants/{grant_id}/revoke",
        json={"expected_revision": grant["revision"]},
        headers=_owner_headers(state) | {"Idempotency-Key": "revoke"},
    )
    assert revoked.status_code == 200, revoked.text
    return client, state, cluster, package, journal, revoked.json(), worker_headers


def _evidence(journal, package, cluster):
    proof = journal.attest_never_submitted(
        package.binding,
        cluster,
        inspect_namespace=lambda name: {"metadata": {"name": name, "uid": cluster.namespace_uid}},
        inspect_job=lambda _name: None,
        list_operation_jobs=lambda _operation: [],
        list_operation_pods=lambda _operation: [],
    )
    return proof


def _pre_manifest_fixture(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    cluster = replace(_cluster(), network_none_evidence=None)
    client = TestClient(create_app(state))
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    worker = {"Authorization": "Bearer worker-token"}
    claim = client.post(
        f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
        json={
            "target_digest": cluster_identity_digest(cluster),
            "profile_version": cluster.profile,
        },
        headers=worker | {"Idempotency-Key": "claim"},
    )
    assert claim.status_code == 200, claim.text
    path = tmp_path / "original-worker" / "kubernetes.sqlite3"
    adapter = _FakeKubernetes(package, cluster).adapter(path)
    adapter._bind(package)
    adapter._mark_cancellation_requested(package.operation_id)
    grant_id = dispatch["grant_id"]
    grant = client.get(f"/execution-grants/{grant_id}", headers=_owner_headers(state)).json()
    revoked = client.post(
        f"/execution-grants/{grant_id}/revoke",
        json={"expected_revision": grant["revision"]},
        headers=_owner_headers(state) | {"Idempotency-Key": "revoke"},
    )
    assert revoked.status_code == 200, revoked.text
    return client, state, cluster, package, KubernetesJobJournal(path), revoked.json(), worker


def _pre_evidence(journal, package, cluster, **overrides):
    return journal.attest_pre_manifest_never_submitted(
        package.binding,
        cluster,
        inspect_namespace=overrides.get(
            "inspect_namespace",
            lambda name: {"metadata": {"name": name, "uid": cluster.namespace_uid}},
        ),
        inspect_job=overrides.get("inspect_job", lambda _name: None),
        list_operation_jobs=overrides.get("list_operation_jobs", lambda _operation: []),
        list_operation_pods=overrides.get("list_operation_pods", lambda _operation: []),
    )


def _attest(client, grant, worker_headers, evidence, key="attest"):
    payload = evidence | {"expected_revision": grant["revision"]}
    return client.post(
        f"/worker/execution-grants/{grant['id']}/attest-never-submitted",
        json=payload,
        headers=worker_headers | {"Idempotency-Key": key},
    )


def _release(client, state, grant, package, digest, key="settle"):
    return client.post(
        f"/execution-grants/{grant['id']}/reconcile-attested-never-submitted",
        json={
            "expected_revision": grant["revision"],
            "operation_id": package.operation_id,
            "attestation_digest": digest,
        },
        headers=_owner_headers(state) | {"Idempotency-Key": key},
    )


def _owner_recover(client, state, grant, proof, key="owner-pre-manifest"):
    return client.post(
        f"/execution-grants/{grant['id']}/reconcile-owner-pre-manifest-no-start",
        json=proof
        | {
            "expected_revision": grant["revision"],
            "owner_attests_original_exclusive_journal": True,
        },
        headers=_owner_headers(state) | {"Idempotency-Key": key},
    )


def _expire_worker(state, grant):
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE credentials SET expires_at=? WHERE principal_id=? AND audience='worker'",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), grant["worker_principal_id"]),
        )


def test_expired_worker_401_but_original_owner_attests_distinct_no_start_once(tmp_path):
    client, state, cluster, package, journal, grant, worker = _pre_manifest_fixture(tmp_path)
    proof = _pre_evidence(journal, package, cluster)
    active = _owner_recover(client, state, grant, proof, "active-worker")
    assert active.status_code == 409
    _expire_worker(state, grant)
    assert _attest(client, grant, worker, proof, "expired-worker").status_code == 401
    reconciled = _owner_recover(client, state, grant, proof)
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["status"] == "reconciled"
    assert reconciled.json()["settlement_cents"] == 0
    assert reconciled.json()["cancellation"]["state"] != "confirmed_termination"
    assert _owner_recover(client, state, grant, proof).json() == reconciled.json()
    assert _owner_recover(client, state, grant, proof, "new-key").status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT held_cents,settled_cents FROM execution_project_limits"
        ).fetchone() == (0, 0)
        attested = db.execute(
            "SELECT actor_principal_id,payload_json FROM events "
            "WHERE event_type='execution.owner_pre_manifest_no_start_attested'"
        ).fetchall()
        assert len(attested) == 1
        event = json.loads(attested[0][1])
        assert attested[0][0] == grant["created_by_principal_id"]
        assert (
            event["trust_boundary"] == "owner_attested_retained_journal_and_target_not_independent"
        )
        assert event["original_worker_principal_id"] == grant["worker_principal_id"]
        assert event["provider_termination_inferred"] is False


def test_owner_pre_manifest_rejects_cross_tenant_noncreator_and_stale_target(tmp_path):
    client, state, cluster, package, journal, grant, worker = _pre_manifest_fixture(tmp_path)
    _expire_worker(state, grant)
    proof = _pre_evidence(journal, package, cluster)
    other_state = tmp_path / "other-org"
    initialize_state(other_state)
    assert _owner_recover(client, other_state, grant, proof, "other-org").status_code in (
        401,
        403,
        404,
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        org_id = db.execute(
            "SELECT org_id FROM projects WHERE id=?", (grant["project_id"],)
        ).fetchone()[0]
        parent_id = db.execute(
            "SELECT id FROM credentials WHERE principal_id=?", (grant["created_by_principal_id"],)
        ).fetchone()[0]
        now = datetime.now(UTC).isoformat()
        db.execute(
            "INSERT INTO principals (id,org_id,role,token_hash,revoked_at,created_at) "
            "VALUES (?,?, 'human', ?,NULL,?)",
            ("noncreator", org_id, _token_hash("noncreator-token"), now),
        )
        db.execute(
            """INSERT INTO credentials
            (id,principal_id,kind,audience,project_id,parent_id,expires_at,revoked_at,
             created_at,created_by_principal_id)
            VALUES (?,?, 'human_session','control',?,?,?,?,?,?)""",
            (
                "noncreator-credential",
                "noncreator",
                grant["project_id"],
                parent_id,
                (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                None,
                now,
                grant["created_by_principal_id"],
            ),
        )
    same_org = client.post(
        f"/execution-grants/{grant['id']}/reconcile-owner-pre-manifest-no-start",
        json=proof
        | {
            "expected_revision": grant["revision"],
            "owner_attests_original_exclusive_journal": True,
        },
        headers={"Authorization": "Bearer noncreator-token", "Idempotency-Key": "noncreator"},
    )
    assert same_org.status_code == 403
    assert (
        _owner_recover(
            client, state, grant, proof | {"target_digest": "f" * 64}, "bad-target"
        ).status_code
        == 409
    )
    assert (
        _owner_recover(
            client, state, grant, proof | {"epoch": proof["epoch"] + 1}, "bad-epoch"
        ).status_code
        == 409
    )
    old = proof | {"observed_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()}
    assert _owner_recover(client, state, grant, old, "stale").status_code == 409
    invented = proof | {"journal_manifest_digest": "a" * 64}
    assert _owner_recover(client, state, grant, invented, "invented-manifest").status_code == 422
    assert (
        _owner_recover(
            client, state, grant, proof | {"proof_kind": "bound_job_intent"}, "wrong-kind"
        ).status_code
        == 422
    )
    assert _attest(client, grant, worker, proof, "expired-check").status_code == 401


def test_owner_pre_manifest_rejects_running_and_provider_identity_evidence(tmp_path):
    client, state, cluster, package, journal, grant, worker = _pre_manifest_fixture(tmp_path)
    proof = _pre_evidence(journal, package, cluster)
    observed = client.post(
        "/worker/observations",
        json={
            "lease_id": package.binding.lease_id,
            "operation_id": package.operation_id,
            "expected_epoch": package.binding.epoch,
            "observed_state": "running",
            "observed_at": datetime.now(UTC).isoformat(),
            "provider_id": None,
            "termination_confirmed": False,
        },
        headers=worker | {"Idempotency-Key": "unexpected-running"},
    )
    assert observed.status_code in (200, 201)
    _expire_worker(state, grant)
    assert _owner_recover(client, state, grant, proof).status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE external_operations SET provider_id=? WHERE operation_id=?",
            ("unexpected", package.operation_id),
        )
    assert _owner_recover(client, state, grant, proof, "provider-conflict").status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0]
            == grant["max_exposure_cents"]
        )


def test_owner_pre_manifest_preserves_original_unconfirmed_worker_unknown(tmp_path):
    client, state, cluster, package, journal, grant, worker = _pre_manifest_fixture(tmp_path)
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
        headers=worker | {"Idempotency-Key": "original-unknown"},
    )
    assert observed.status_code in (200, 201)
    _expire_worker(state, grant)
    proof = _pre_evidence(journal, package, cluster)
    reconciled = _owner_recover(client, state, grant, proof)
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["cancellation"]["state"] == "unresolved"


def test_owner_pre_manifest_event_failure_rolls_back_grant_and_held_cost(tmp_path, monkeypatch):
    client, state, cluster, package, journal, grant, _worker = _pre_manifest_fixture(tmp_path)
    _expire_worker(state, grant)
    proof = _pre_evidence(journal, package, cluster)
    original_event = client.app.state.execution._event

    def fail_on_release(connection, actor, project_id, task_id, event_type, revision, payload):
        if event_type == "execution.owner_pre_manifest_no_start_reconciled":
            raise RuntimeError("synthetic event failure")
        return original_event(connection, actor, project_id, task_id, event_type, revision, payload)

    monkeypatch.setattr(client.app.state.execution, "_event", fail_on_release)
    with pytest.raises(RuntimeError, match="synthetic event failure"):
        _owner_recover(client, state, grant, proof)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT status,revision FROM execution_grants WHERE id=?", (grant["id"],)
        ).fetchone() == ("cancel_requested", grant["revision"])
        assert (
            db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0]
            == grant["max_exposure_cents"]
        )
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type LIKE 'execution.owner_pre_manifest_no_start_%'"
            ).fetchone()[0]
            == 0
        )


def test_original_cancelled_journal_attestation_releases_only_after_human_and_replays(tmp_path):
    client, state, cluster, package, journal, grant, worker = _fixture(tmp_path)
    evidence = _evidence(journal, package, cluster)
    attested = _attest(client, grant, worker, evidence)
    assert attested.status_code == 201, attested.text
    assert attested.json()["authority_released"] is False
    assert _attest(client, grant, worker, evidence).json() == attested.json()
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type='execution.kubernetes_never_submitted_attested'"
            ).fetchone()[0]
            == 1
        )
    assert (
        client.get(f"/execution-grants/{grant['id']}", headers=_owner_headers(state)).json()[
            "status"
        ]
        == "cancel_requested"
    )
    released = _release(client, state, grant, package, attested.json()["attestation_digest"])
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "reconciled"
    assert (
        _release(client, state, grant, package, attested.json()["attestation_digest"]).json()
        == released.json()
    )
    assert released.json()["cancellation"]["state"] != "confirmed_termination"
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0] == 0


def test_missing_altered_or_submitted_journal_rejects_and_cannot_create_after_cancel(tmp_path):
    client, state, cluster, package, journal, grant, worker = _fixture(tmp_path)
    other = KubernetesJobJournal(tmp_path / "lost-worker" / "journal.sqlite3")
    with pytest.raises(KubernetesUnresolvedError, match="intact"):
        _evidence(other, package, cluster)
    with pytest.raises(KubernetesUnresolvedError, match="fenced"):
        journal.reconcile_create(
            package.binding, cluster, lambda _name: None, lambda _manifest: None
        )
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE kubernetes_jobs SET create_intent=1")
    with pytest.raises(KubernetesUnresolvedError, match="intact"):
        _evidence(journal, package, cluster)
    assert (
        client.get(f"/execution-grants/{grant['id']}", headers=_owner_headers(state)).json()[
            "status"
        ]
        == "cancel_requested"
    )
    assert worker


def test_actual_pre_manifest_failure_is_distinct_attested_no_start(tmp_path):
    client, state, cluster, package, journal, grant, worker = _pre_manifest_fixture(tmp_path)
    with sqlite3.connect(journal.path) as db:
        assert db.execute(
            "SELECT cancellation_requested,job_uid FROM kubernetes_execution_bindings"
        ).fetchone() == (1, None)
        assert db.execute("SELECT count(*) FROM kubernetes_jobs").fetchone()[0] == 0
    with pytest.raises(KubernetesContractError, match="network_mode=none"):
        _evidence(journal, package, cluster)
    proof = _pre_evidence(journal, package, cluster)
    assert proof["proof_kind"] == "pre_manifest"
    assert proof["manifest_not_generated"] and proof["journal_manifest_digest"] is None
    replay = _pre_evidence(KubernetesJobJournal(journal.path), package, cluster)
    assert {k: v for k, v in replay.items() if k != "observed_at"} == {
        k: v for k, v in proof.items() if k != "observed_at"
    }
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        journal._bind(package.binding, {"never": "submit"})
    posted = _attest(client, grant, worker, proof)
    assert posted.status_code == 201, posted.text
    assert _attest(client, grant, worker, proof).json() == posted.json()
    settled = _release(client, state, grant, package, posted.json()["attestation_digest"])
    assert settled.status_code == 200
    assert settled.json()["status"] == "reconciled"
    assert (
        _release(client, state, grant, package, posted.json()["attestation_digest"]).json()
        == settled.json()
    )
    assert (
        client.get(f"/execution-grants/{grant['id']}", headers=_owner_headers(state)).json()[
            "cancellation"
        ]["state"]
        != "confirmed_termination"
    )


def test_pre_manifest_no_start_keeps_original_unknown_worker_observation(tmp_path):
    client, state, cluster, package, journal, grant, worker = _pre_manifest_fixture(tmp_path)
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
        headers=worker | {"Idempotency-Key": "original-unknown"},
    )
    assert observed.status_code in (200, 201), observed.text
    proof = _pre_evidence(journal, package, cluster)
    attested = _attest(client, grant, worker, proof)
    assert attested.status_code == 201, attested.text
    released = _release(client, state, grant, package, attested.json()["attestation_digest"])
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "reconciled"
    assert released.json()["cancellation"]["state"] != "confirmed_termination"


def test_pre_manifest_no_start_rejects_running_observation(tmp_path):
    client, _state, cluster, package, journal, grant, worker = _pre_manifest_fixture(tmp_path)
    observed = client.post(
        "/worker/observations",
        json={
            "lease_id": package.binding.lease_id,
            "operation_id": package.operation_id,
            "expected_epoch": package.binding.epoch,
            "observed_state": "running",
            "observed_at": datetime.now(UTC).isoformat(),
            "provider_id": None,
            "termination_confirmed": False,
        },
        headers=worker | {"Idempotency-Key": "unexpected-running"},
    )
    assert observed.status_code in (200, 201), observed.text
    proof = _pre_evidence(journal, package, cluster)
    attested = _attest(client, grant, worker, proof)
    assert attested.status_code == 409
    assert "observations" in attested.text


def test_pre_manifest_cancel_before_bind_races_without_admitting_uncertain_create(tmp_path):
    client, state, cluster, package, journal, grant, _worker = _pre_manifest_fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attest = pool.submit(_pre_evidence, journal, package, cluster)
        bind = pool.submit(journal._bind, package.binding, {"would_be": "Job"})
        outcomes = []
        for future in (attest, bind):
            try:
                outcomes.append(future.result())
            except (sqlite3.IntegrityError, KubernetesUnresolvedError) as error:
                outcomes.append(error)
    if isinstance(outcomes[0], dict):
        assert isinstance(outcomes[1], sqlite3.IntegrityError)
        assert (
            _pre_evidence(KubernetesJobJournal(journal.path), package, cluster)["proof_kind"]
            == "pre_manifest"
        )
    else:
        assert isinstance(outcomes[0], KubernetesUnresolvedError)
        assert outcomes[1] is None
        with pytest.raises(KubernetesUnresolvedError, match="conflicted"):
            _pre_evidence(journal, package, cluster)
    assert (
        client.get(f"/execution-grants/{grant['id']}", headers=_owner_headers(state)).json()[
            "status"
        ]
        == "cancel_requested"
    )


def test_pre_manifest_missing_or_changed_original_journal_never_attests(tmp_path):
    client, state, cluster, package, journal, grant, _worker = _pre_manifest_fixture(tmp_path)
    replaced = KubernetesJobJournal(tmp_path / "lost-original" / "kubernetes.sqlite3")
    with pytest.raises(KubernetesUnresolvedError, match="table is missing"):
        _pre_evidence(replaced, package, cluster)
    with pytest.raises(KubernetesUnresolvedError, match="missing network witness"):
        _pre_evidence(journal, package, _cluster())
    wrong_epoch = replace(package.binding, epoch=package.binding.epoch + 1)
    with pytest.raises(KubernetesUnresolvedError, match="conflicted"):
        journal.attest_pre_manifest_never_submitted(
            wrong_epoch,
            cluster,
            inspect_namespace=lambda name: {
                "metadata": {"name": name, "uid": cluster.namespace_uid}
            },
            inspect_job=lambda _name: None,
            list_operation_jobs=lambda _operation: [],
            list_operation_pods=lambda _operation: [],
        )
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE kubernetes_execution_bindings SET cancellation_requested=0")
    with pytest.raises(KubernetesUnresolvedError, match="conflicted"):
        _pre_evidence(journal, package, cluster)
    assert (
        client.get(f"/execution-grants/{grant['id']}", headers=_owner_headers(state)).json()[
            "status"
        ]
        == "cancel_requested"
    )


def test_pre_manifest_read_failure_leaves_durable_fence_and_held_reservation(tmp_path):
    client, state, cluster, package, journal, grant, _worker = _pre_manifest_fixture(tmp_path)
    with pytest.raises(KubernetesUnresolvedError, match="incomplete"):
        _pre_evidence(journal, package, cluster, list_operation_pods=lambda _operation: None)
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        KubernetesJobJournal(journal.path)._bind(package.binding, {"would_be": "Job"})
    with pytest.raises(KubernetesUnresolvedError, match="Namespace UID"):
        _pre_evidence(
            journal,
            package,
            cluster,
            inspect_namespace=lambda name: {"metadata": {"name": name, "uid": "replacement"}},
        )
    assert (
        client.get(f"/execution-grants/{grant['id']}", headers=_owner_headers(state)).json()[
            "status"
        ]
        == "cancel_requested"
    )


def test_pre_manifest_worker_auth_target_epoch_and_kind_are_fenced(tmp_path):
    client, state, cluster, package, journal, grant, worker = _pre_manifest_fixture(tmp_path)
    proof = _pre_evidence(journal, package, cluster)
    assert (
        _attest(
            client, grant, worker, proof | {"epoch": proof["epoch"] + 1}, "wrong-epoch"
        ).status_code
        == 409
    )
    assert (
        _attest(
            client, grant, worker, proof | {"target_digest": "f" * 64}, "wrong-target"
        ).status_code
        == 409
    )
    assert (
        _attest(
            client, grant, worker, proof | {"proof_kind": "bound_job_intent"}, "wrong-kind"
        ).status_code
        == 422
    )
    assert (
        _attest(
            client,
            grant,
            worker,
            proof | {"journal_manifest_digest": "a" * 64},
            "invented-manifest",
        ).status_code
        == 422
    )
    assert _attest(
        client, grant, {"Authorization": _owner_headers(state)["Authorization"]}, proof, "human"
    ).status_code in {401, 403}
    assert _attest(client, grant, worker, proof, "valid").status_code == 201


def test_create_intent_before_cancel_fence_can_never_be_attested(tmp_path):
    _client, _state, cluster, package, _journal, _grant, _worker = _fixture(tmp_path)
    journal = KubernetesJobJournal(tmp_path / "original-unfenced" / "journal.sqlite3")
    journal._bind(
        package.binding,
        _job_manifest(package.binding, cluster, require_fresh_network_evidence=False),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(journal._mark_create_intent, package.operation_id)
        cancel = pool.submit(journal._mark_cancellation_requested, package.operation_id)
        cancel.result()
        try:
            create.result()
        except KubernetesUnresolvedError:
            pass
    with sqlite3.connect(journal.path) as db:
        attempted = db.execute("SELECT create_intent FROM kubernetes_jobs").fetchone()[0]
    if attempted:
        with pytest.raises(KubernetesUnresolvedError, match="intact"):
            _evidence(journal, package, cluster)
    else:
        assert _evidence(journal, package, cluster)["create_intent"] is False


def test_atomic_create_race_fences_one_side_and_provider_conflict_blocks_release(tmp_path):
    client, state, cluster, package, journal, grant, worker = _fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attest = pool.submit(_evidence, journal, package, cluster)
        create = pool.submit(journal._mark_create_intent, package.operation_id)
        evidence = attest.result()
        with pytest.raises(KubernetesUnresolvedError, match="fenced"):
            create.result()
    assert evidence["create_intent"] is False
    receipt = _attest(client, grant, worker, evidence)
    assert receipt.status_code == 201, receipt.text
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE external_operations SET provider_id='unexpected-job' WHERE operation_id=?",
            (package.operation_id,),
        )
    refused = _release(client, state, grant, package, receipt.json()["attestation_digest"])
    assert refused.status_code == 409, refused.text


def test_wrong_worker_target_stale_snapshot_and_replacement_are_rejected(tmp_path):
    client, state, cluster, package, journal, grant, worker = _fixture(tmp_path)
    with pytest.raises(KubernetesUnresolvedError, match="Namespace UID"):
        journal.attest_never_submitted(
            package.binding,
            cluster,
            inspect_namespace=lambda name: {"metadata": {"name": name, "uid": "replacement"}},
            inspect_job=lambda _name: None,
            list_operation_jobs=lambda _operation: [],
            list_operation_pods=lambda _operation: [],
        )
    with pytest.raises(KubernetesUnresolvedError, match="may exist"):
        journal.attest_never_submitted(
            package.binding,
            cluster,
            inspect_namespace=lambda name: {
                "metadata": {"name": name, "uid": cluster.namespace_uid}
            },
            inspect_job=lambda _name: {"metadata": {"uid": "replacement-job"}},
            list_operation_jobs=lambda _operation: [],
            list_operation_pods=lambda _operation: [],
        )
    evidence = _evidence(journal, package, cluster)
    wrong = _attest(client, grant, worker, evidence | {"target_digest": "f" * 64}, "wrong")
    assert wrong.status_code == 409
    stale = _attest(
        client,
        grant,
        worker,
        evidence | {"observed_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()},
        "stale",
    )
    assert stale.status_code == 409
    forbidden = _attest(
        client, grant, {"Authorization": _owner_headers(state)["Authorization"]}, evidence, "human"
    )
    assert forbidden.status_code in {401, 403}
    other_state = tmp_path / "other-org"
    initialize_state(other_state)
    assert _release(client, other_state, grant, package, "f" * 64, "other-org").status_code in {
        401,
        403,
        404,
    }
    assert (
        _attest(
            client, grant, worker, evidence | {"epoch": evidence["epoch"] + 1}, "epoch"
        ).status_code
        == 409
    )
    assert _attest(client, grant, worker, evidence, "correct").status_code == 201
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE kubernetes_dispatches SET target_digest=? WHERE grant_id=?",
            ("e" * 64, grant["id"]),
        )
    digest = canonical_digest(evidence | {"expected_revision": grant["revision"]})
    assert _release(client, state, grant, package, digest).status_code == 409


def test_attested_readback_expires_before_human_release(tmp_path):
    client, state, cluster, package, journal, grant, worker = _fixture(tmp_path)
    evidence = _evidence(journal, package, cluster)
    attested = _attest(client, grant, worker, evidence)
    assert attested.status_code == 201, attested.text
    client.app.state.execution._now = lambda: datetime.now(UTC) + timedelta(minutes=2)
    refused = _release(client, state, grant, package, attested.json()["attestation_digest"])
    assert refused.status_code == 409
    assert (
        client.get(f"/execution-grants/{grant['id']}", headers=_owner_headers(state)).json()[
            "status"
        ]
        == "cancel_requested"
    )
