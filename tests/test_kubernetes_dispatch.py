"""Normal control/worker dispatch flow over the fake Kubernetes adapter only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes

from tessera_control.api import create_app
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)


class _ApiChannel:
    def __init__(self, client: TestClient, *, lose_claim_reply: bool = False) -> None:
        self.client = client
        self.lose_claim_reply = lose_claim_reply
        self.keys: list[str] = []

    def post(self, path: str, body: dict, key: str) -> dict:
        self.keys.append(key)
        response = self.client.post(
            path,
            json=body,
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": key},
        )
        assert response.status_code < 400, response.text
        if self.lose_claim_reply and path.endswith("/claim"):
            self.lose_claim_reply = False
            raise ConnectionError("synthetic lost claim reply")
        return response.json()


def _owner_headers(state) -> dict[str, str]:
    return {
        "Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1],
    }


def _dispatch(client: TestClient, state, response: dict, target: str) -> dict:
    package = response["package"]
    exported = client.get(
        f"/experiments/{package['experiment']['experiment_id']}", headers=_owner_headers(state)
    )
    assert exported.status_code == 200
    attempt = next(
        item
        for item in exported.json()["admitted_attempts"]
        if item["id"] == response["admitted_attempt_id"]
    )
    approved = client.post(
        f"/experiment-admitted-attempts/{attempt['id']}/dispatch",
        json={
            "expected_revision": attempt["revision"],
            "target_digest": target,
            "profile_version": "development-minikube-opencode-v1",
        },
        headers=_owner_headers(state) | {"Idempotency-Key": "human-kubernetes-dispatch"},
    )
    assert approved.status_code == 201, approved.text
    return approved.json()


def _service(tmp_path, client, dispatch, fake, cluster, *, lose_claim_reply=False):
    root = tmp_path / "worker"
    root.mkdir(mode=0o700, exist_ok=True)
    credential = tmp_path / "worker.credential"
    credential.write_text("worker-token")
    credential.chmod(0o600)
    return KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig("http://127.0.0.1:8765", dispatch["id"], credential, root),
        cluster,
        fake.package.output,
        fake.adapter(root / "kubernetes.sqlite3"),
        channel=_ApiChannel(client, lose_claim_reply=lose_claim_reply),
    )


def test_approved_dispatch_claims_once_runs_control_package_and_exports_linkage(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))

    first = _service(tmp_path, client, dispatch, fake, cluster).run()
    second = _service(tmp_path, client, dispatch, fake, cluster).run()

    assert first["admitted_attempt_id"] == response["admitted_attempt_id"]
    assert second["operation_id"] == response["operation"]["operation_id"]
    assert fake.creates == 1
    exported = client.get(
        f"/experiments/{package.package['experiment']['experiment_id']}",
        headers=_owner_headers(state),
    ).json()
    assert exported["kubernetes_dispatches"][0]["operation_id"] == first["operation_id"]
    assert exported["operations"][0]["experiment_attempt_id"] == first["admitted_attempt_id"]


def test_wrong_target_lost_claim_reply_and_revocation_fence_adapter_effects(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))

    wrong = client.post(
        f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
        json={"target_digest": "f" * 64, "profile_version": cluster.profile},
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "wrong-target"},
    )
    assert wrong.status_code == 409 and fake.creates == 0
    with pytest.raises(ConnectionError, match="lost claim"):
        _service(tmp_path, client, dispatch, fake, cluster, lose_claim_reply=True).run()
    recovered = _service(tmp_path, client, dispatch, fake, cluster).run()
    assert recovered["status"] == "running" and fake.creates == 1

    revoked = client.post(
        f"/execution-grants/{package.package['grant_id']}/revoke",
        json={"expected_revision": 1},
        headers=_owner_headers(state) | {"Idempotency-Key": "cancel-dispatch"},
    )
    assert revoked.status_code == 200
    claim = client.post(
        f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
        json={
            "target_digest": cluster_identity_digest(cluster),
            "profile_version": cluster.profile,
        },
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "claim-after-cancel"},
    )
    assert claim.status_code == 409 and fake.creates == 1


def test_wrong_worker_and_stale_epoch_cannot_claim(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client = TestClient(create_app(state))
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    token = "other-worker-" + "x" * 48
    credential = client.post(
        "/credentials",
        json={
            "kind": "worker",
            "audience": "worker",
            "token": token,
            "project_id": package.package["project_id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        headers=_owner_headers(state) | {"Idempotency-Key": "other-worker-credential"},
    ).json()
    client.post(
        f"/projects/{package.package['project_id']}/workers",
        json={
            "worker_principal_id": credential["principal_id"],
            "approved_build_identifier": "test",
            "approved_build_digest": "a" * 64,
            "trust_class": "test",
            "region": "local",
        },
        headers=_owner_headers(state) | {"Idempotency-Key": "other-worker-register"},
    )
    wrong_worker = client.post(
        f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
        json={
            "target_digest": cluster_identity_digest(cluster),
            "profile_version": cluster.profile,
        },
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "wrong-worker"},
    )
    assert wrong_worker.status_code == 404
    # Revocation advances the grant epoch atomically with the dispatch fence.
    client.post(
        f"/execution-grants/{package.package['grant_id']}/revoke",
        json={"expected_revision": 1},
        headers=_owner_headers(state) | {"Idempotency-Key": "fence-epoch"},
    )
    stale = client.post(
        f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
        json={
            "target_digest": cluster_identity_digest(cluster),
            "profile_version": cluster.profile,
        },
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "stale-epoch"},
    )
    assert stale.status_code == 409
