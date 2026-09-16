"""Normal control/worker dispatch flow over the fake Kubernetes adapter only."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes

from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.workers import WorkerRegistryStore
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)
from tessera_worker.kubernetes_execution import KubernetesExecutionAdapter


class _ApiChannel:
    def __init__(self, client: TestClient, *, lose_claim_reply: bool = False) -> None:
        self.client = client
        self.lose_claim_reply = lose_claim_reply
        self.keys: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def post(self, path: str, body: dict, key: str) -> dict:
        self.keys.append(key)
        self.calls.append((path, key))
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


@pytest.mark.parametrize("authority_change", ("release", "issuer", "limits"))
def test_claim_uses_the_same_full_grant_predicate_as_lease_authority(tmp_path, authority_change):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client = TestClient(create_app(state))
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        if authority_change == "release":
            connection.execute(
                "UPDATE snapshot_releases SET expires_at=? WHERE id=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    connection.execute(
                        "SELECT snapshot_release_id FROM execution_grants WHERE id=?",
                        (package.package["grant_id"],),
                    ).fetchone()[0],
                ),
            )
        elif authority_change == "issuer":
            issuer_id = connection.execute(
                "SELECT created_by_principal_id FROM execution_grants WHERE id=?",
                (package.package["grant_id"],),
            ).fetchone()[0]
            connection.execute("UPDATE principals SET role='agent' WHERE id=?", (issuer_id,))
        else:
            connection.execute(
                "UPDATE execution_project_limits SET monetary_limit_cents=0 WHERE project_id=?",
                (package.package["project_id"],),
            )
    rejected = client.post(
        f"/worker/kubernetes-dispatches/{dispatch['id']}/claim",
        json={
            "target_digest": cluster_identity_digest(cluster),
            "profile_version": cluster.profile,
        },
        headers={
            "Authorization": "Bearer worker-token",
            "Idempotency-Key": f"full-{authority_change}",
        },
    )
    assert 400 <= rejected.status_code < 500


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
    # A reporting failure preserves the exact report for recovery but cannot
    # prevent local reconciliation from returning its bounded state.
    assert (
        _service(tmp_path, client, dispatch, fake, cluster, channel=lost).run(max_cycles=1)[
            "status"
        ]
        == "running"
    )
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
    assert len(rows) == 3 and {row[0] for row in rows} == {dispatch["id"], second_dispatch["id"]}
    assert len({row[2] for row in rows}) == 3


def test_restart_after_revocation_reconciles_then_cancels_without_a_create(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))

    assert (
        _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)["status"] == "running"
    )
    revoked = client.post(
        f"/execution-grants/{package.package['grant_id']}/revoke",
        json={"expected_revision": 1},
        headers=_owner_headers(state) | {"Idempotency-Key": "restart-revoke"},
    )
    assert revoked.status_code == 200

    # The Job has disappeared before recovery, so the first reconciliation is
    # authoritative unknown.  Revocation must still reach the UID-scoped stop.
    fake.jobs.clear()
    recovered = _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=2)
    assert recovered["status"] == "cancel_requested"
    assert recovered["cancellation"]["job_uid"] == "job-fixture"
    assert recovered["cancellation"]["termination_confirmed"] is True
    assert recovered["reconciliation_required"] is False
    assert fake.creates == 1


def test_restart_uses_the_original_immutable_hard_deadline(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [datetime.now(UTC)]
    first = _service(tmp_path, client, dispatch, fake, cluster)
    first.now = lambda: now[0]
    assert first.run(max_cycles=1)["status"] == "running"

    now[0] += timedelta(seconds=package.binding.active_deadline_seconds)
    restarted = _service(tmp_path, client, dispatch, fake, cluster)
    restarted.now = lambda: now[0]
    result = restarted.run(max_cycles=1)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "hard_deadline"
    assert fake.creates == 1


def test_renewal_retries_its_exact_key_after_a_lost_reply(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [datetime.now(UTC)]

    class LostRenewal(_ApiChannel):
        def __init__(self, client):
            super().__init__(client)
            self.renewals: list[tuple[dict, str]] = []
            self.lost = False

        def post(self, path, body, key):
            if path == "/worker/leases/renew":
                self.renewals.append((body, key))
                response = super().post(path, body, key)
                if not self.lost:
                    self.lost = True
                    raise ConnectionError("lost renewal reply")
                return response
            return super().post(path, body, key)

    channel = LostRenewal(client)
    service = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    service.now = lambda: now[0]
    service.renewal_seconds = 1
    service.sleep = lambda _: now.__setitem__(0, now[0] + timedelta(seconds=2))
    assert service.run(max_cycles=3)["status"] == "running"
    assert len(channel.renewals) == 2 and channel.renewals[0] == channel.renewals[1]
    assert fake.creates == 1


def test_restart_replays_lost_renewal_before_enforcing_cached_lease_expiry(tmp_path, monkeypatch):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda: now[0]))
    monkeypatch.setattr(WorkerRegistryStore, "_now", staticmethod(lambda: now[0]))

    class LostRenewal(_ApiChannel):
        def __init__(self, client):
            super().__init__(client)
            self.renewals: list[str] = []
            self.renewal_responses: list[dict] = []
            self.lost = True

        def post(self, path, body, key):
            result = super().post(path, body, key)
            if path == "/worker/leases/renew":
                self.renewals.append(key)
                self.renewal_responses.append(result)
                if self.lost:
                    self.lost = False
                    raise ConnectionError("lost renewal reply before restart")
            return result

    channel = LostRenewal(client)
    first = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    first.now = lambda: now[0]
    first.renewal_seconds = 1
    first.sleep = lambda _: now.__setitem__(0, now[0] + timedelta(seconds=2))
    assert first.run(max_cycles=2)["status"] == "running"

    # The control mutation extended the lease, but the worker crashed with only
    # its older, now-expired cache. Recovery must replay the pending exact key
    # before treating that cache as authoritative.
    now[0] = package.lease_expires_at + timedelta(seconds=1)
    assert datetime.fromisoformat(channel.renewal_responses[0]["expires_at"]) > now[0]
    restarted = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    restarted.now = lambda: now[0]
    assert restarted.claims.pending_action("renew") is not None
    restarted_result = restarted.run(max_cycles=1)
    assert restarted.claims.supervision()[0] > now[0]
    assert restarted_result["status"] == "running", restarted_result
    assert channel.renewals[0] == channel.renewals[1]
    assert fake.creates == 1


@pytest.mark.parametrize("invalid_field", ("id", "expires_at", "hard_expires_at"))
def test_invalid_renewal_identity_or_expiry_cancels_existing_job(tmp_path, invalid_field):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]

    class InvalidRenewal(_ApiChannel):
        def post(self, path, body, key):
            result = super().post(path, body, key)
            if path == "/worker/leases/renew":
                if invalid_field == "id":
                    result["id"] = "lease-conflict"
                elif invalid_field == "expires_at":
                    result["expires_at"] = (
                        package.hard_expires_at + timedelta(seconds=1)
                    ).isoformat()
                else:
                    result["hard_expires_at"] = (
                        package.hard_expires_at + timedelta(seconds=1)
                    ).isoformat()
            return result

    service = _service(tmp_path, client, dispatch, fake, cluster, channel=InvalidRenewal(client))
    service.now = lambda: now[0]
    service.renewal_seconds = 1
    service.sleep = lambda _: now.__setitem__(0, now[0] + timedelta(seconds=2))
    result = service.run(max_cycles=3)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "authority_lost"
    assert fake.creates == 1


def test_pending_running_report_flushes_before_fresh_terminal_report(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))

    class LostFirstReport(_ApiChannel):
        def __init__(self, client):
            super().__init__(client)
            self.reports: list[tuple[str, str]] = []
            self.lost = False

        def post(self, path, body, key):
            if path == "/worker/observations":
                self.reports.append((body["observed_state"], key))
                response = super().post(path, body, key)
                if not self.lost:
                    self.lost = True
                    raise ConnectionError("lost report reply")
                return response
            return super().post(path, body, key)

    channel = LostFirstReport(client)
    _service(tmp_path, client, dispatch, fake, cluster, channel=channel).run(max_cycles=1)
    fake.jobs[package.binding.job_name]["status"] = {"succeeded": 1}
    pod = fake.pod("Succeeded")
    template = fake.jobs[package.binding.job_name]["spec"]["template"]
    pod["metadata"] = deepcopy(template["metadata"]) | {
        "namespace": cluster.namespace,
        "uid": "pod-fixture",
        "ownerReferences": [{"kind": "Job", "uid": "job-fixture", "controller": True}],
    }
    pod["spec"] = deepcopy(template["spec"])
    fake.pods = [pod]
    assert (
        _service(tmp_path, client, dispatch, fake, cluster, channel=channel).run(max_cycles=1)[
            "status"
        ]
        == "succeeded"
    )
    assert channel.reports[:3] == [
        ("running", channel.reports[0][1]),
        ("running", channel.reports[0][1]),
        ("exited", channel.reports[2][1]),
    ]


def test_cancellation_callback_uncertainty_retains_job_ownership_for_reconciliation(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    fake.delete = lambda _name, _uid: (_ for _ in ()).throw(RuntimeError("delete unknown"))
    assert (
        client.post(
            f"/execution-grants/{package.package['grant_id']}/revoke",
            json={"expected_revision": 1},
            headers=_owner_headers(state) | {"Idempotency-Key": "uncertain-cancel"},
        ).status_code
        == 200
    )

    result = _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=2)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["job_uid"] == "job-fixture"
    assert result["cancellation"]["termination_confirmed"] is False
    assert result["reconciliation_required"] is True
    assert fake.creates == 1


def test_full_control_outage_is_bounded_to_observe_then_cancel_without_create(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    now = [datetime.now(UTC)]

    class Offline:
        def post(self, _path, _body, _key):
            raise ConnectionError("control offline")

    service = _service(tmp_path, client, dispatch, fake, cluster, channel=Offline())
    service.now = lambda: now[0]
    service.control_outage_seconds = 1
    service.sleep = lambda _: now.__setitem__(0, now[0] + timedelta(seconds=2))
    result = service.run(max_cycles=3)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "control_outage"
    assert fake.creates == 1


def test_unknown_recovery_reconciles_existing_job_without_handoff_or_replacement_create(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    root = tmp_path / "worker"
    root.mkdir(mode=0o700)
    credential = tmp_path / "worker.credential"
    credential.write_text("worker-token")
    credential.chmod(0o600)
    journal = root / "kubernetes.sqlite3"
    reads = 0

    def transient_pod_read(_job_uid: str):
        nonlocal reads
        reads += 1
        if reads <= 2:
            raise RuntimeError("temporary Pod read failure")
        return fake.pods

    unknown = KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig("http://127.0.0.1:8765", dispatch["id"], credential, root),
        cluster,
        package.output,
        KubernetesExecutionAdapter(
            journal,
            cluster,
            inspect_job=fake.jobs.get,
            create_job=fake.create,
            delete_job=fake.delete,
            observe_pods=transient_pod_read,
        ),
        channel=_ApiChannel(client),
    ).run(max_cycles=1)
    assert unknown["status"] == "unknown"
    assert unknown["reconciliation_required"] is True
    assert unknown["recovery_owner"] == "worker"
    assert "artifact_handoff" not in unknown and fake.creates == 1

    recovered = KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig("http://127.0.0.1:8765", dispatch["id"], credential, root),
        cluster,
        package.output,
        fake.adapter(journal),
        channel=_ApiChannel(client),
    ).run(max_cycles=1)
    assert recovered["status"] == "running"
    assert fake.creates == 1


def test_unknown_at_expired_deadline_still_cancels_the_recorded_job(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [datetime.now(UTC)]
    first = _service(tmp_path, client, dispatch, fake, cluster)
    first.now = lambda: now[0]
    assert first.run(max_cycles=1)["status"] == "running"

    reads = 0

    def fail_then_recover_pod_read(_job_uid: str):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("temporary Pod read failure")
        return fake.pods

    now[0] += timedelta(seconds=package.binding.active_deadline_seconds)
    root = tmp_path / "worker"
    credential = tmp_path / "worker.credential"
    expired = KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig("http://127.0.0.1:8765", dispatch["id"], credential, root),
        cluster,
        package.output,
        KubernetesExecutionAdapter(
            root / "kubernetes.sqlite3",
            cluster,
            inspect_job=fake.jobs.get,
            create_job=fake.create,
            delete_job=fake.delete,
            observe_pods=fail_then_recover_pod_read,
        ),
        channel=_ApiChannel(client),
        now=lambda: now[0],
    ).run(max_cycles=1)
    assert expired["status"] == "cancel_requested"
    assert expired["cancellation"]["reason"] == "hard_deadline"
    assert expired["cancellation"]["termination_confirmed"] is True
    assert fake.creates == 1


def test_control_hard_expiry_and_delayed_start_narrow_local_and_provider_deadlines(tmp_path):
    response, _worker, package = _admitted_package(
        tmp_path, max_duration_seconds=300, grant_seconds=20
    )
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at + timedelta(seconds=5)]
    service = _service(tmp_path, client, dispatch, fake, cluster)
    service.now = lambda: now[0]

    assert package.binding.active_deadline_seconds <= 20
    assert service.run(max_cycles=1)["status"] == "running"
    manifest = fake.jobs[package.binding.job_name]
    assert manifest["spec"]["activeDeadlineSeconds"] <= 15

    now[0] = package.hard_expires_at
    restarted = _service(tmp_path, client, dispatch, fake, cluster)
    restarted.now = lambda: now[0]
    result = restarted.run(max_cycles=1)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "hard_deadline"
    assert fake.creates == 1


def test_first_start_delayed_to_hard_expiry_never_creates_provider_job(tmp_path):
    response, _worker, package = _admitted_package(
        tmp_path, max_duration_seconds=300, grant_seconds=20
    )
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    service = _service(tmp_path, client, dispatch, fake, cluster)
    service.now = lambda: package.hard_expires_at

    result = service.run(max_cycles=1)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "hard_deadline"
    assert fake.creates == 0


def test_provider_deadline_and_sleep_never_round_past_remaining_authority(tmp_path):
    response, _worker, package = _admitted_package(
        tmp_path, max_duration_seconds=300, grant_seconds=5
    )
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    fake.delete = lambda _name, _uid: None
    now = [package.lease_issued_at]
    monotonic = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)
        monotonic[0] += seconds

    service = _service(tmp_path, client, dispatch, fake, cluster)
    service.now = lambda: now[0]
    service.monotonic = lambda: monotonic[0]
    service.poll_seconds = 60
    service.heartbeat_seconds = 120
    service.renewal_seconds = 120
    service.sleep = sleep
    initial_hard_remaining = package.hard_expires_at - now[0]
    initial_lease_remaining = package.lease_expires_at - now[0]

    result = service.run(max_cycles=2)
    manifest = fake.jobs[package.binding.job_name]
    assert manifest["spec"]["activeDeadlineSeconds"] <= int(initial_hard_remaining.total_seconds())
    assert sleeps and sleeps[0] <= initial_lease_remaining.total_seconds()
    assert result["status"] == "cancel_requested"


def test_valid_renewals_outlive_initial_lease_without_shortening_provider_deadline(
    tmp_path, monkeypatch
):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda: now[0]))
    monkeypatch.setattr(WorkerRegistryStore, "_now", staticmethod(lambda: now[0]))

    service = _service(tmp_path, client, dispatch, fake, cluster)
    service.now = lambda: now[0]
    service.renewal_seconds = 10
    service.heartbeat_seconds = 20
    service.sleep = lambda _seconds: now.__setitem__(0, now[0] + timedelta(seconds=10))

    result = service.run(max_cycles=5)
    provider_seconds = fake.jobs[package.binding.job_name]["spec"]["activeDeadlineSeconds"]
    initial_lease_seconds = int(
        (package.lease_expires_at - package.lease_issued_at).total_seconds()
    )
    assert now[0] > package.lease_expires_at
    assert result["status"] == "running"
    assert provider_seconds > initial_lease_seconds
    assert fake.creates == 1

    restarted = _service(tmp_path, client, dispatch, fake, cluster)
    restarted.now = lambda: now[0]
    restarted_result = restarted.run(max_cycles=1)
    assert restarted_result["status"] == "running"
    assert fake.jobs[package.binding.job_name]["spec"]["activeDeadlineSeconds"] == provider_seconds
    assert fake.creates == 1


def test_mid_loop_pod_read_error_is_reported_unknown_then_recovers_without_resubmit(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    reads = 0

    def fail_second_read(_job_uid: str):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise ConnectionError("temporary Pod observation outage")
        return fake.pods

    root = tmp_path / "worker"
    root.mkdir(mode=0o700)
    credential = tmp_path / "worker.credential"
    credential.write_text("worker-token")
    credential.chmod(0o600)
    service = KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig("http://127.0.0.1:8765", dispatch["id"], credential, root),
        cluster,
        package.output,
        KubernetesExecutionAdapter(
            root / "kubernetes.sqlite3",
            cluster,
            inspect_job=fake.jobs.get,
            create_job=fake.create,
            delete_job=fake.delete,
            observe_pods=fail_second_read,
        ),
        channel=_ApiChannel(client),
        sleep=lambda _: None,
    )

    assert service.run(max_cycles=3)["status"] == "running"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        observations = [
            row[0]
            for row in connection.execute(
                "SELECT observed_state FROM execution_observations "
                "WHERE operation_id=? ORDER BY rowid",
                (package.operation_id,),
            )
        ]
    assert "unknown" in observations and observations[-1] == "running"
    assert fake.creates == 1


def test_mid_loop_job_read_error_remains_supervised_and_never_replaces_job(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    fail_read = [False]

    def inspect_job(name: str):
        if fail_read[0]:
            fail_read[0] = False
            raise ConnectionError("temporary Job observation outage")
        return fake.jobs.get(name)

    root = tmp_path / "worker"
    root.mkdir(mode=0o700)
    credential = tmp_path / "worker.credential"
    credential.write_text("worker-token")
    credential.chmod(0o600)
    service = KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig("http://127.0.0.1:8765", dispatch["id"], credential, root),
        cluster,
        package.output,
        KubernetesExecutionAdapter(
            root / "kubernetes.sqlite3",
            cluster,
            inspect_job=inspect_job,
            create_job=fake.create,
            delete_job=fake.delete,
            observe_pods=lambda _uid: fake.pods,
        ),
        channel=_ApiChannel(client),
        sleep=lambda _: fail_read.__setitem__(0, True),
    )
    result = service.run(max_cycles=2)
    assert result["status"] == "unknown"
    assert result["reconciliation_required"] is True
    assert fake.creates == 1


def test_simultaneous_control_and_cluster_observation_outage_retains_cancellation(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    now = [datetime.now(UTC)]

    class Offline:
        def post(self, _path, _body, _key):
            raise ConnectionError("control offline")

    def unreadable_job(_name: str):
        raise ConnectionError("cluster offline")

    root = tmp_path / "worker"
    service = KubernetesDispatchWorkerService(
        KubernetesDispatchWorkerConfig(
            "http://127.0.0.1:8765",
            dispatch["id"],
            tmp_path / "worker.credential",
            root,
        ),
        cluster,
        package.output,
        KubernetesExecutionAdapter(
            root / "kubernetes.sqlite3",
            cluster,
            inspect_job=unreadable_job,
            create_job=fake.create,
            delete_job=fake.delete,
            observe_pods=lambda _uid: (_ for _ in ()).throw(ConnectionError("cluster offline")),
        ),
        channel=Offline(),
        now=lambda: now[0],
        sleep=lambda _: now.__setitem__(0, now[0] + timedelta(seconds=2)),
        control_outage_seconds=1,
    )
    result = service.run(max_cycles=3)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "control_outage"
    assert result["reconciliation_required"] is True
    assert fake.creates == 1


def test_shared_clock_heartbeats_keep_long_work_fresh_but_missing_beats_fail_closed(
    tmp_path, monkeypatch
):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda: now[0]))
    monkeypatch.setattr(WorkerRegistryStore, "_now", staticmethod(lambda: now[0]))

    channel = _ApiChannel(client)
    service = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    service.now = lambda: now[0]
    service.renewal_seconds = 10
    service.heartbeat_seconds = 20
    service.sleep = lambda _: now.__setitem__(0, now[0] + timedelta(seconds=10))
    result = service.run(max_cycles=8)
    assert result["status"] == "running"
    assert now[0] >= package.lease_issued_at + timedelta(seconds=70)
    assert sum(path == "/worker/heartbeat" for path, _key in channel.calls) >= 4
    assert sum(path == "/worker/leases/renew" for path, _key in channel.calls) >= 6

    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    now[0] = datetime.now(UTC)
    stale_response, _worker, stale_package = _admitted_package(stale_root)
    stale_state, stale_cluster = stale_root / "state", _cluster()
    stale_client = TestClient(create_app(stale_state))
    stale_fake = _FakeKubernetes(stale_package, stale_cluster)
    stale_dispatch = _dispatch(
        stale_client,
        stale_state,
        stale_response,
        cluster_identity_digest(stale_cluster),
    )
    now[0] = stale_package.lease_issued_at
    stale = _service(stale_root, stale_client, stale_dispatch, stale_fake, stale_cluster)
    stale.now = lambda: now[0]
    stale.renewal_seconds = 10
    stale.heartbeat_seconds = 120
    stale.sleep = lambda _: now.__setitem__(0, now[0] + timedelta(seconds=10))
    stale_result = stale.run(max_cycles=9)
    assert stale_result["status"] == "cancel_requested"
    assert stale_result["cancellation"]["reason"] == "authority_lost"
    assert stale_fake.creates == 1


def test_lost_heartbeat_reply_is_replayed_with_same_durable_key_after_restart(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))

    class LostHeartbeatReply(_ApiChannel):
        def __init__(self, client):
            super().__init__(client)
            self.heartbeats: list[str] = []
            self.lost = True

        def post(self, path, body, key):
            response = super().post(path, body, key)
            if path == "/worker/heartbeat":
                self.heartbeats.append(key)
                if self.lost:
                    self.lost = False
                    raise ConnectionError("lost heartbeat reply")
            return response

    channel = LostHeartbeatReply(client)
    with pytest.raises(ConnectionError, match="lost heartbeat"):
        _service(tmp_path, client, dispatch, fake, cluster, channel=channel).run()
    recovered = _service(tmp_path, client, dispatch, fake, cluster, channel=channel).run(
        max_cycles=1
    )
    assert recovered["status"] == "running"
    assert channel.heartbeats[0] == channel.heartbeats[1]
    assert fake.creates == 1


def test_current_lease_expiry_cancels_locally_before_another_control_claim(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]
    channel = _ApiChannel(client)
    service = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    service.now = lambda: now[0]
    service.renewal_seconds = 120
    service.heartbeat_seconds = 120
    service.sleep = lambda _: now.__setitem__(0, package.lease_expires_at)

    result = service.run(max_cycles=2)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "lease_expiry"
    assert sum(path.endswith("/claim") for path, _key in channel.calls) == 1
    assert fake.creates == 1


def test_restart_preserves_first_control_outage_deadline_and_reconciles_same_job(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    now = [datetime.now(UTC)]

    class Offline:
        def post(self, _path, _body, _key):
            raise ConnectionError("control offline")

    first = _service(tmp_path, client, dispatch, fake, cluster, channel=Offline())
    first.now = lambda: now[0]
    first.control_outage_seconds = 10
    assert first.run(max_cycles=1)["status"] == "running"

    now[0] += timedelta(seconds=11)
    restarted = _service(tmp_path, client, dispatch, fake, cluster, channel=Offline())
    restarted.now = lambda: now[0]
    restarted.control_outage_seconds = 10
    result = restarted.run(max_cycles=1)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "control_outage"
    assert fake.creates == 1


def test_recovered_control_cannot_clear_an_expired_persisted_outage_deadline(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    now = [datetime.now(UTC)]

    class Offline:
        def post(self, _path, _body, _key):
            raise ConnectionError("control offline")

    offline = _service(tmp_path, client, dispatch, fake, cluster, channel=Offline())
    offline.now = lambda: now[0]
    offline.control_outage_seconds = 5
    assert offline.run(max_cycles=1)["status"] == "running"

    now[0] += timedelta(seconds=6)
    recovered_channel = _ApiChannel(client)
    recovered = _service(tmp_path, client, dispatch, fake, cluster, channel=recovered_channel)
    recovered.now = lambda: now[0]
    recovered.control_outage_seconds = 5
    result = recovered.run(max_cycles=1)
    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "control_outage"
    assert not any(path.endswith("/claim") for path, _key in recovered_channel.calls)
    assert not any(path == "/worker/heartbeat" for path, _key in recovered_channel.calls)


@pytest.mark.parametrize(
    ("outage_seconds", "expected_reason"),
    ((30, "clock_rollback"), (1, "control_outage")),
)
def test_wall_clock_rollback_during_outage_fails_closed_on_monotonic_time(
    tmp_path, outage_seconds, expected_reason
):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    wall = [datetime.now(UTC)]
    monotonic = [100.0]

    class Offline:
        def post(self, _path, _body, _key):
            raise ConnectionError("control offline")

    service = _service(tmp_path, client, dispatch, fake, cluster, channel=Offline())
    service.now = lambda: wall[0]
    service.monotonic = lambda: monotonic[0]
    service.control_outage_seconds = outage_seconds

    def rollback_sleep(_seconds):
        wall[0] -= timedelta(seconds=5)
        monotonic[0] += 2

    service.sleep = rollback_sleep
    result = service.run(max_cycles=3)

    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == expected_reason
    assert fake.creates == 1


def test_restart_detects_persisted_wall_clock_rollback_before_control_recovery(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    wall = [datetime.now(UTC)]

    class Offline:
        def __init__(self):
            self.calls = []

        def post(self, path, _body, _key):
            self.calls.append(path)
            raise ConnectionError("control offline")

    first_channel = Offline()
    first = _service(tmp_path, client, dispatch, fake, cluster, channel=first_channel)
    first.now = lambda: wall[0]
    first.monotonic = lambda: 10.0
    first.control_outage_seconds = 30
    assert first.run(max_cycles=1)["status"] == "running"

    wall[0] -= timedelta(seconds=5)
    restarted_channel = Offline()
    restarted = _service(tmp_path, client, dispatch, fake, cluster, channel=restarted_channel)
    restarted.now = lambda: wall[0]
    restarted.monotonic = lambda: 1.0
    result = restarted.run(max_cycles=1)

    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "clock_rollback"
    assert "/worker/heartbeat" not in restarted_channel.calls
    assert "/worker/leases/renew" not in restarted_channel.calls
    assert not any(path.endswith("/claim") for path in restarted_channel.calls)
    assert fake.creates == 1


def test_renewal_response_crash_replays_then_atomically_completes_authority(tmp_path):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]

    class RenewalChannel(_ApiChannel):
        def __init__(self, client):
            super().__init__(client)
            self.renewals: list[str] = []

        def post(self, path, body, key):
            response = super().post(path, body, key)
            if path == "/worker/leases/renew":
                self.renewals.append(key)
            return response

    channel = RenewalChannel(client)
    first = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    first.now = lambda: now[0]
    first.renewal_seconds = 1
    first.sleep = lambda _seconds: now.__setitem__(0, now[0] + timedelta(seconds=2))
    original_complete = first.claims.complete_renewal
    first.claims.complete_renewal = lambda _sequence, _expiry: (_ for _ in ()).throw(
        RuntimeError("synthetic crash before local renewal commit")
    )
    with pytest.raises(RuntimeError, match="synthetic crash"):
        first.run(max_cycles=3)
    first.claims.complete_renewal = original_complete

    with sqlite3.connect(tmp_path / "worker" / "kubernetes-claims.sqlite3") as connection:
        assert connection.execute(
            "SELECT completed FROM dispatch_actions WHERE dispatch_id=? AND action='renew'",
            (dispatch["id"],),
        ).fetchone() == (0,)
        cached_before = connection.execute(
            "SELECT lease_expires_at FROM dispatch_supervision WHERE dispatch_id=?",
            (dispatch["id"],),
        ).fetchone()[0]

    restarted = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    restarted.now = lambda: now[0]
    assert restarted.run(max_cycles=1)["status"] == "running"
    assert channel.renewals[0] == channel.renewals[1]
    with sqlite3.connect(tmp_path / "worker" / "kubernetes-claims.sqlite3") as connection:
        assert connection.execute(
            "SELECT completed FROM dispatch_actions WHERE dispatch_id=? AND action='renew'",
            (dispatch["id"],),
        ).fetchone() == (1,)
        cached_after = connection.execute(
            "SELECT lease_expires_at FROM dispatch_supervision WHERE dispatch_id=?",
            (dispatch["id"],),
        ).fetchone()[0]
    assert cached_after > cached_before


def test_pending_renewal_cannot_erase_an_independently_expired_outage_deadline(
    tmp_path, monkeypatch
):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda: now[0]))
    monkeypatch.setattr(WorkerRegistryStore, "_now", staticmethod(lambda: now[0]))
    channel = _ApiChannel(client)
    service = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    service.now = lambda: now[0]
    assert service.run(max_cycles=1)["status"] == "running"

    outage = service.claims.begin_outage(package.lease_expires_at)
    now[0] += timedelta(seconds=5)
    body, key, sequence = service.claims.action(
        "renew",
        {"lease_id": package.binding.lease_id, "expected_epoch": package.binding.epoch},
    )
    renewal = channel.post("/worker/leases/renew", body, key)
    assert datetime.fromisoformat(renewal["expires_at"]) > outage
    assert service.claims.pending_action("renew") == (body, key, sequence)

    now[0] = outage + timedelta(seconds=1)
    calls_before_restart = len(channel.calls)
    restarted = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    restarted.now = lambda: now[0]
    result = restarted.run(max_cycles=1)

    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "control_outage"
    recovery_calls = channel.calls[calls_before_restart:]
    assert not any(path == "/worker/heartbeat" for path, _key in recovery_calls)
    assert not any(path == "/worker/leases/renew" for path, _key in recovery_calls)
    assert restarted.claims.pending_action("renew") == (body, key, sequence)
    assert fake.creates == 1


def test_pending_renewal_that_finishes_after_outage_deadline_remains_uncommitted(
    tmp_path, monkeypatch
):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda: now[0]))
    monkeypatch.setattr(WorkerRegistryStore, "_now", staticmethod(lambda: now[0]))
    first = _service(tmp_path, client, dispatch, fake, cluster)
    first.now = lambda: now[0]
    assert first.run(max_cycles=1)["status"] == "running"

    outage = first.claims.begin_outage(now[0] + timedelta(seconds=5))
    body, key, sequence = first.claims.action(
        "renew",
        {"lease_id": package.binding.lease_id, "expected_epoch": package.binding.epoch},
    )

    class LateRenewal(_ApiChannel):
        def post(self, path, body, key):
            result = super().post(path, body, key)
            if path == "/worker/leases/renew":
                now[0] = outage + timedelta(seconds=1)
            return result

    restarted = _service(tmp_path, client, dispatch, fake, cluster, channel=LateRenewal(client))
    restarted.now = lambda: now[0]
    result = restarted.run(max_cycles=1)

    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["reason"] == "control_outage"
    assert restarted.claims.supervision()[1] == outage
    assert restarted.claims.pending_action("renew") == (body, key, sequence)
    assert fake.creates == 1


@pytest.mark.parametrize("failed_action", ("heartbeat", "renew"))
def test_failed_lifecycle_actions_are_paced_and_reuse_their_pending_key(
    tmp_path, monkeypatch, failed_action
):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda: now[0]))
    monkeypatch.setattr(WorkerRegistryStore, "_now", staticmethod(lambda: now[0]))

    class FailedLifecycleAction(_ApiChannel):
        def __init__(self, client):
            super().__init__(client)
            self.initial_heartbeat_complete = False
            self.failed: list[tuple[datetime, str]] = []

        def post(self, path, body, key):
            if path == "/worker/heartbeat" and not self.initial_heartbeat_complete:
                result = super().post(path, body, key)
                self.initial_heartbeat_complete = True
                return result
            if path == f"/worker/{failed_action}" or (
                failed_action == "renew" and path == "/worker/leases/renew"
            ):
                self.calls.append((path, key))
                self.failed.append((now[0], key))
                raise ConnectionError(f"synthetic {failed_action} outage")
            return super().post(path, body, key)

    channel = FailedLifecycleAction(client)
    service = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    service.now = lambda: now[0]
    service.poll_seconds = 0.5
    service.heartbeat_seconds = 2 if failed_action == "heartbeat" else 120
    service.renewal_seconds = 2 if failed_action == "renew" else 120
    service.sleep = lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds))

    assert service.run(max_cycles=11)["status"] == "running"
    assert len(channel.failed) >= 2
    assert all(
        later_at - earlier_at >= timedelta(seconds=2)
        for (earlier_at, _), (later_at, _) in zip(channel.failed, channel.failed[1:], strict=False)
    )
    assert len({key for _attempted_at, key in channel.failed}) == 1
    pending = service.claims.pending_action(failed_action)
    assert pending is not None and pending[1] == channel.failed[0][1]
    assert fake.creates == 1


def test_periodic_lost_heartbeat_aged_replay_gets_a_fresh_durable_key(tmp_path, monkeypatch):
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    now = [package.lease_issued_at]
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda: now[0]))
    monkeypatch.setattr(WorkerRegistryStore, "_now", staticmethod(lambda: now[0]))

    class LostPeriodicHeartbeat(_ApiChannel):
        def __init__(self, client):
            super().__init__(client)
            self.heartbeats: list[tuple[str, str]] = []

        def post(self, path, body, key):
            response = super().post(path, body, key)
            if path == "/worker/heartbeat":
                self.heartbeats.append((key, response["heartbeat_at"]))
                if len(self.heartbeats) == 2:
                    raise ConnectionError("lost periodic heartbeat reply")
            return response

    channel = LostPeriodicHeartbeat(client)
    first = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    first.now = lambda: now[0]
    first.heartbeat_seconds = 5
    first.renewal_seconds = 120
    first.sleep = lambda _seconds: now.__setitem__(0, now[0] + timedelta(seconds=6))
    assert first.run(max_cycles=2)["status"] == "running"

    now[0] += timedelta(seconds=6)
    restarted = _service(tmp_path, client, dispatch, fake, cluster, channel=channel)
    restarted.now = lambda: now[0]
    restarted.heartbeat_seconds = 5
    assert restarted.run(max_cycles=1)["status"] == "running"
    keys = [key for key, _heartbeat_at in channel.heartbeats]
    assert keys[1] == keys[2]
    assert keys[3] != keys[2]
