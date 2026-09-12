"""Normal control/worker dispatch flow over the fake Kubernetes adapter only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes

from tessera_control.api import create_app
from tessera_worker.control_channel import ControlChannelError
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
        if response.status_code >= 400:
            raise ControlChannelError("fixture control rejection", response.status_code)
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


def _service(tmp_path, client, dispatch, fake, cluster, *, lose_claim_reply=False, channel=None):
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
        channel=channel or _ApiChannel(client, lose_claim_reply=lose_claim_reply),
    )


def test_approved_dispatch_claims_once_runs_control_package_and_exports_linkage(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))

    first = _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    second = _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)

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
    recovered = _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
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


@pytest.mark.parametrize("authority_change", ("revoke", "expire", "drain"))
def test_claim_replay_with_same_key_rechecks_current_authority(tmp_path, authority_change):
    response, worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    claim_path = f"/worker/kubernetes-dispatches/{dispatch['id']}/claim"
    claim_body = {
        "target_digest": cluster_identity_digest(cluster),
        "profile_version": cluster.profile,
    }
    headers = {"Authorization": "Bearer worker-token", "Idempotency-Key": "replayed-claim"}
    assert client.post(claim_path, json=claim_body, headers=headers).status_code == 200

    if authority_change == "revoke":
        changed = client.post(
            f"/execution-grants/{package.package['grant_id']}/revoke",
            json={"expected_revision": 1},
            headers=_owner_headers(state) | {"Idempotency-Key": "revoke-replayed-claim"},
        )
        assert changed.status_code == 200
    elif authority_change == "expire":
        with sqlite3.connect(state / "tessera.sqlite3") as connection:
            connection.execute(
                "UPDATE execution_leases SET expires_at=? WHERE id=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    package.package["lease_id"],
                ),
            )
    else:
        with sqlite3.connect(state / "tessera.sqlite3") as connection:
            registry_id, revision = connection.execute(
                "SELECT id,revision FROM worker_registry WHERE worker_principal_id=?",
                (worker["id"],),
            ).fetchone()
        changed = client.post(
            f"/workers/{registry_id}/drain",
            json={"expected_revision": revision},
            headers=_owner_headers(state) | {"Idempotency-Key": "drain-replayed-claim"},
        )
        assert changed.status_code == 200

    assert client.post(claim_path, json=claim_body, headers=headers).status_code == 409
    with pytest.raises(ControlChannelError):
        _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    assert fake.creates == 0


def test_supervisor_cancels_long_running_dispatch_and_restart_after_disconnect(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))

    def revoke_on_poll(_: float) -> None:
        revoked = client.post(
            f"/execution-grants/{package.package['grant_id']}/revoke",
            json={"expected_revision": 1},
            headers=_owner_headers(state) | {"Idempotency-Key": "supervisor-revoke"},
        )
        assert revoked.status_code == 200

    cancelled = _service(tmp_path, client, dispatch, fake, cluster)
    cancelled.sleep = revoke_on_poll
    result = cancelled.run(max_cycles=2)
    assert result["status"] == "cancel_requested" and fake.creates == 1

    # A separate dispatch loses the control connection while its Job is running.
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_response, _worker, second_package = _admitted_package(second_root)
    second_state, second_cluster = second_root / "state", _cluster()
    second_client = TestClient(create_app(second_state))
    second_fake = _FakeKubernetes(second_package, second_cluster)
    second_dispatch = _dispatch(
        second_client, second_state, second_response, cluster_identity_digest(second_cluster)
    )

    class DisconnectingChannel(_ApiChannel):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.claims = 0

        def post(self, path, body, key):
            if path.endswith("/claim"):
                self.claims += 1
                if self.claims == 2:
                    raise ControlChannelError("synthetic disconnect")
            return super().post(path, body, key)

    root = tmp_path / "shared-worker"
    root.mkdir(mode=0o700)
    credential = tmp_path / "second.credential"
    credential.write_text("worker-token")
    credential.chmod(0o600)
    disconnected = KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig(
            "http://127.0.0.1:8765", second_dispatch["id"], credential, root
        ),
        second_cluster,
        second_fake.package.output,
        second_fake.adapter(root / "second-kubernetes.sqlite3"),
        channel=DisconnectingChannel(second_client),
        sleep=lambda _: None,
    )
    assert disconnected.run(max_cycles=2)["status"] == "running"
    restarted = KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig(
            "http://127.0.0.1:8765", second_dispatch["id"], credential, root
        ),
        second_cluster,
        second_fake.package.output,
        second_fake.adapter(root / "second-kubernetes.sqlite3"),
        channel=_ApiChannel(second_client),
    )
    assert restarted.run(max_cycles=1)["status"] == "running"
    assert second_fake.creates == 1


def test_lost_observation_reply_replays_exact_body_key_and_dispatch_scope(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))

    class LostObservationReply(_ApiChannel):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.observations: list[tuple[str, str]] = []
            self.lost = False

        def post(self, path, body, key):
            if path == "/worker/observations":
                self.observations.append(
                    (json.dumps(body, sort_keys=True, separators=(",", ":")), key)
                )
                response = super().post(path, body, key)
                if not self.lost:
                    self.lost = True
                    raise ConnectionError("synthetic lost observation reply")
                return response
            return super().post(path, body, key)

    lost = LostObservationReply(client)
    with pytest.raises(ConnectionError, match="lost observation"):
        _service(tmp_path, client, dispatch, fake, cluster, channel=lost).run(max_cycles=1)
    recovered = _service(tmp_path, client, dispatch, fake, cluster, channel=lost)
    assert recovered.run(max_cycles=1)["status"] == "running"
    assert lost.observations[0] == lost.observations[1]

    # A second dispatch under this root must receive a distinct, scoped report intent.
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_response, _worker, second_package = _admitted_package(second_root)
    second_state, second_cluster = tmp_path / "second" / "state", _cluster()
    second_client = TestClient(create_app(second_state))
    second_fake = _FakeKubernetes(second_package, second_cluster)
    second_dispatch = _dispatch(
        second_client, second_state, second_response, cluster_identity_digest(second_cluster)
    )
    _service(tmp_path, second_client, second_dispatch, second_fake, second_cluster).run(
        max_cycles=1
    )
    with sqlite3.connect(tmp_path / "worker" / "kubernetes-claims.sqlite3") as connection:
        rows = connection.execute(
            "SELECT dispatch_id,operation_id,key FROM dispatch_reports "
            "ORDER BY dispatch_id,operation_id"
        ).fetchall()
    assert len(rows) == 2 and {row[0] for row in rows} == {dispatch["id"], second_dispatch["id"]}
    assert len({row[2] for row in rows}) == 2
