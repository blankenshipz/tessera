"""Deterministic fake acceptance slice for Kubernetes experiment execution."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import _owner
from test_kubernetes_dispatch import _ApiChannel, _dispatch, _owner_headers, _service
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes

from tessera_control.api import create_app
from tessera_control.artifacts import ArtifactStore
from tessera_control.experiments import ExperimentStore
from tessera_control.store import Conflict, Store, canonical_digest, initialize_state
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_execution import KubernetesExecutionAdapter
from tessera_worker.kubernetes_provider import KubernetesUnresolvedError


def _command(method, principal: dict, *args: str, payload: dict) -> dict:
    status, result = method(
        principal,
        *args,
        f"kubernetes-experiment-slice-{method.__name__}",
        canonical_digest(payload),
        payload,
    )
    assert status in {200, 201}
    return result


def test_approval_alone_creates_no_grant_operation_or_kubernetes_job(tmp_path: Path) -> None:
    """Task approval is distinct from all execution authority and effects."""
    state = tmp_path / "state"
    initialize_state(state)
    owner = _owner(state)
    store = Store(state)
    _status, project = store.create_project(
        owner, "project", canonical_digest({"name": "slice"}), {"name": "slice"}
    )
    _status, task = store.create_task(
        owner,
        project["id"],
        "task",
        canonical_digest({"title": "slice"}),
        {
            "title": "slice",
            "purpose": "Verify approval does not dispatch Kubernetes work.",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    plan = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": 0,
        "max_duration_seconds": 60,
        "execution_spec": {
            "argv": ["deterministic", "--write", "result.txt"],
            "input_snapshot_digest": "a" * 64,
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "profile": "kubernetes-opencode",
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }
    _status, planned = store.revise_task_plan(
        owner, task["id"], "plan", canonical_digest(plan), plan
    )
    _status, approved = store.approve_task(
        owner,
        task["id"],
        "approve",
        canonical_digest(
            {
                "expected_revision": planned["revision"],
                "expected_plan_digest": planned["current_plan_digest"],
            }
        ),
        planned["revision"],
        planned["current_plan_digest"],
    )

    assert approved["status"] == "approved"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM execution_grants").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM kubernetes_dispatches").fetchone() == (0,)
    assert not (tmp_path / "worker" / "kubernetes.sqlite3").exists()


def test_registered_variants_admission_dispatch_restart_and_measurement_linkage(
    tmp_path: Path,
) -> None:
    """Exercise the durable chain without treating collection as result acceptance."""
    response, worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    fake = _FakeKubernetes(package, cluster)

    with TestClient(create_app(state)) as control:
        before_dispatch = control.get(
            f"/experiments/{package.package['experiment']['experiment_id']}",
            headers=_owner_headers(state),
        ).json()
        variants = before_dispatch["experiment"]["design"]["variants"]
        assert {variant["kind"] for variant in variants} == {
            "baseline",
            "intervention",
        }
        assert before_dispatch["admitted_attempts"][0]["id"] == response["admitted_attempt_id"]
        assert before_dispatch["kubernetes_dispatches"] == []
        assert fake.creates == 0

        dispatch = _dispatch(control, state, response, cluster_identity_digest(cluster))
        # Human dispatch writes only an outbox row; the enrolled worker owns Job creation.
        assert fake.creates == 0
        assert dispatch["operation_id"] == response["operation"]["operation_id"]
        first = _service(tmp_path, control, dispatch, fake, cluster).run(max_cycles=1)

    # Reopening the same state models a control-process restart. The worker journal
    # and the control package must still name the original external operation.
    with TestClient(create_app(state)) as restarted_control:
        restarted = _service(tmp_path, restarted_control, dispatch, fake, cluster).run(max_cycles=1)
        exported = restarted_control.get(
            f"/experiments/{package.package['experiment']['experiment_id']}",
            headers=_owner_headers(state),
        ).json()

    assert first["operation_id"] == restarted["operation_id"] == package.operation_id
    assert fake.creates == 1
    assert exported["kubernetes_dispatches"][0]["operation_id"] == package.operation_id
    assert (
        exported["kubernetes_dispatches"][0]["admitted_attempt_id"]
        == response["admitted_attempt_id"]
    )

    output = b"hello tessera kubernetes fake slice\n"
    output_sha256 = hashlib.sha256(output).hexdigest()
    artifact_payload = {
        "expected_epoch": package.binding.epoch,
        "operation_id": package.operation_id,
        "relative_path": "result.txt",
        "sha256": output_sha256,
        "content_base64": base64.b64encode(output).decode("ascii"),
    }
    wrong_derived_payload = artifact_payload | {
        "operation_id": hashlib.sha256(
            f"tessera-cpu-v1:{package.binding.lease_id}".encode()
        ).hexdigest()[:32]
    }
    with pytest.raises(Conflict, match="control-issued lease operation"):
        ArtifactStore(state).upload_artifact(
            worker,
            package.binding.lease_id,
            "kubernetes-derived-operation",
            canonical_digest(wrong_derived_payload),
            wrong_derived_payload,
        )
    unrelated_operation_payload = artifact_payload | {"operation_id": "f" * 32}
    with pytest.raises(Conflict, match="control-issued lease operation"):
        ArtifactStore(state).upload_artifact(
            worker,
            package.binding.lease_id,
            "kubernetes-unrelated-operation",
            canonical_digest(unrelated_operation_payload),
            unrelated_operation_payload,
        )
    _status, artifact = ArtifactStore(state).upload_artifact(
        worker,
        package.binding.lease_id,
        "deterministic-kubernetes-output",
        canonical_digest(artifact_payload),
        artifact_payload,
    )
    conflicting_content = b"conflicting immutable result\n"
    conflicting_path_payload = artifact_payload | {
        "sha256": hashlib.sha256(conflicting_content).hexdigest(),
        "content_base64": base64.b64encode(conflicting_content).decode("ascii"),
    }
    with pytest.raises(Conflict, match="immutable"):
        ArtifactStore(state).upload_artifact(
            worker,
            package.binding.lease_id,
            "kubernetes-immutable-path",
            canonical_digest(conflicting_path_payload),
            conflicting_path_payload,
        )
    records = ExperimentStore(state)
    admitted = records.get_experiment(
        _owner(state), package.package["experiment"]["experiment_id"]
    )["admitted_attempts"][0]
    linked = _command(
        records.link_attempt_artifacts,
        _owner(state),
        admitted["id"],
        payload={"expected_revision": admitted["revision"], "artifact_ids": [artifact["id"]]},
    )
    measurement = _command(
        records.record_measurement,
        _owner(state),
        admitted["id"],
        payload={
            "expected_revision": linked["revision"],
            "name": "deterministic output digest",
            "value": {"bytes": len(output), "sha256": output_sha256},
            "artifact_id": artifact["id"],
        },
    )
    evidence = records.get_experiment(_owner(state), package.package["experiment"]["experiment_id"])
    assert artifact["sha256"] == output_sha256
    assert artifact["operation_id"] == package.operation_id
    assert measurement["attempt_id"] == response["admitted_attempt_id"]
    assert measurement["artifact_id"] == artifact["id"]
    assert evidence["measurements"][0] == {
        field: measurement[field]
        for field in (
            "id",
            "attempt_id",
            "name",
            "value",
            "artifact_id",
            "created_at",
            "created_by_principal_id",
        )
    }


def test_lost_create_and_report_replay_remain_bound_to_one_operation(tmp_path: Path) -> None:
    """A lost Kubernetes create/report response cannot mint replacement work."""
    response, _worker, package = _admitted_package(tmp_path)
    fake = _FakeKubernetes(package, _cluster())
    journal = tmp_path / "lost-create.sqlite3"
    create_calls = 0

    def create_without_reply(_manifest: object) -> None:
        nonlocal create_calls
        create_calls += 1

    lost_create = KubernetesExecutionAdapter(
        journal,
        fake.cluster,
        inspect_job=fake.jobs.get,
        create_job=create_without_reply,
        delete_job=fake.delete,
        observe_pods=lambda _uid: fake.pods,
    )
    with pytest.raises(KubernetesUnresolvedError, match="lost"):
        lost_create.submit(package)
    with pytest.raises(KubernetesUnresolvedError, match="inspect only"):
        fake.adapter(journal).submit(package)
    assert create_calls == 1

    state, cluster = tmp_path / "state", _cluster()
    reporting_fake = _FakeKubernetes(package, cluster)
    with TestClient(create_app(state)) as control:
        dispatch = _dispatch(control, state, response, cluster_identity_digest(cluster))

        class LostReportReply(_ApiChannel):
            def __init__(self, client: TestClient) -> None:
                super().__init__(client)
                self.reports: list[tuple[dict, str]] = []
                self.lost = True

            def post(self, path: str, body: dict, key: str) -> dict:
                result = super().post(path, body, key)
                if path == "/worker/observations":
                    self.reports.append((body, key))
                    if self.lost:
                        self.lost = False
                        raise ConnectionError("synthetic lost report reply")
                return result

        channel = LostReportReply(control)
        assert (
            _service(tmp_path, control, dispatch, reporting_fake, cluster, channel=channel).run(
                max_cycles=1
            )["status"]
            == "running"
        )
        assert (
            _service(tmp_path, control, dispatch, reporting_fake, cluster, channel=channel).run(
                max_cycles=1
            )["status"]
            == "running"
        )

    assert channel.reports[0] == channel.reports[1]
    assert reporting_fake.creates == 1


@pytest.mark.parametrize(("delete_fails", "confirmed"), ((False, True), (True, False)))
def test_revocation_retains_confirmed_and_unknown_termination_distinctions(
    tmp_path: Path, delete_fails: bool, confirmed: bool
) -> None:
    response, _worker, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    fake = _FakeKubernetes(package, cluster)
    with TestClient(create_app(state)) as control:
        dispatch = _dispatch(control, state, response, cluster_identity_digest(cluster))
        first = _service(tmp_path, control, dispatch, fake, cluster).run(max_cycles=1)
        assert first["status"] == "running"
        if delete_fails:
            fake.delete = lambda _name, _uid: (_ for _ in ()).throw(RuntimeError("delete unknown"))
        revoked = control.post(
            f"/execution-grants/{package.package['grant_id']}/revoke",
            json={"expected_revision": 1},
            headers=_owner_headers(state) | {"Idempotency-Key": "revoke-kubernetes-slice"},
        )
        assert revoked.status_code == 200
        result = _service(tmp_path, control, dispatch, fake, cluster).run(max_cycles=2)

    assert result["status"] == "cancel_requested"
    assert result["cancellation"]["termination_confirmed"] is confirmed
    assert result["reconciliation_required"] is not confirmed
    assert result["cancellation"]["job_uid"] == "job-fixture"
    assert fake.creates == 1
