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
from tessera_worker.kubernetes_execution import KubernetesExecutionAdapter


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
    fake.jobs[package.binding.job_name] = fake.job("succeeded")
    fake.pods = [fake.pod("Succeeded")]
    assert (
        _service(tmp_path, client, dispatch, fake, cluster, channel=channel).run()["status"]
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
