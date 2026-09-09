from __future__ import annotations

from copy import deepcopy

import pytest

from tessera_worker.kubernetes_provider import (
    ClusterIdentity,
    JobBinding,
    KubernetesContractError,
    KubernetesJobJournal,
    KubernetesUnresolvedError,
    canonical_job_spec,
    canonical_job_spec_digest,
    deterministic_job_name,
    parse_job,
    reconcile_cancellation,
    reconcile_create,
    stable_provider_identity,
)


def cluster() -> ClusterIdentity:
    return ClusterIdentity(
        kubeconfig="/private/fixture/kubeconfig",
        kubeconfig_sha256="a" * 64,
        context="minikube-fixture",
        profile="development-minikube-opencode-v1",
        server="https://127.0.0.1:8443",
        server_sha256="b" * 64,
        ca_sha256="c" * 64,
        namespace="tessera-fixture",
        cluster_uid="cluster-fixture",
        namespace_uid="namespace-fixture",
    )


def binding(**changes: object) -> JobBinding:
    values: dict[str, object] = {
        "operation_id": "1" * 32,
        "attempt_id": "attempt-1",
        "lease_id": "lease-1",
        "epoch": 1,
        "package_sha256": "d" * 64,
        "image": "registry.example/opencode@sha256:" + "e" * 64,
        "command": ("opencode", "serve"),
        "active_deadline_seconds": 300,
    }
    values.update(changes)
    return JobBinding(**values)  # type: ignore[arg-type]


def job() -> dict[str, object]:
    observed = deepcopy(canonical_job_spec(binding(), cluster()))
    observed["metadata"]["uid"] = "job-fixture"  # type: ignore[index]
    observed["status"] = {"active": 1}
    return observed


def test_canonical_output_is_digest_pinned_and_secure() -> None:
    spec = canonical_job_spec(binding(), cluster())
    assert spec == canonical_job_spec(binding(), cluster())
    assert spec["metadata"]["annotations"]["tessera.dev/spec-sha256"] == (  # type: ignore[index]
        canonical_job_spec_digest(spec)
    )
    pod = spec["spec"]["template"]["spec"]  # type: ignore[index]
    container = pod["containers"][0]  # type: ignore[index]
    assert pod["restartPolicy"] == "Never"  # type: ignore[index]
    assert pod["automountServiceAccountToken"] is False  # type: ignore[index]
    assert not any("host" in volume for volume in pod["volumes"])  # type: ignore[index]
    assert container["securityContext"] == {  # type: ignore[index]
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": 65532,
    }
    assert pod["volumes"] == [{"emptyDir": {"medium": "Memory"}, "name": "work"}]


@pytest.mark.parametrize("operation_id", ["", "A" * 32, "1" * 31, "1" * 33])
def test_unknown_or_malformed_inputs_rejected(operation_id: str) -> None:
    with pytest.raises(ValueError):
        deterministic_job_name(operation_id)
    with pytest.raises(ValueError):
        JobBinding(**{**binding().__dict__, "operation_id": operation_id})


def test_identity_and_spec_digest_mismatches_fail_closed() -> None:
    observed = job()
    observed["spec"]["template"]["spec"]["containers"][0]["image"] = "unsafe:latest"  # type: ignore[index]
    with pytest.raises(KubernetesContractError, match="immutable spec"):
        parse_job(observed, binding(), cluster())
    changed = cluster().__dict__.copy()
    changed["context"] = "other-minikube"
    with pytest.raises(KubernetesContractError, match="conflict"):
        parse_job(job(), binding(), ClusterIdentity(**changed))
    assert stable_provider_identity(
        "cluster-fixture", "namespace-fixture", "job-fixture"
    ) == stable_provider_identity("cluster-fixture", "namespace-fixture", "job-fixture")
    assert (
        stable_provider_identity("cluster-fixture", "namespace-fixture", "job-fixture").provider_id
        != stable_provider_identity("other", "namespace-fixture", "job-fixture").provider_id
    )


def test_lost_create_is_fenced(tmp_path) -> None:
    journal = KubernetesJobJournal(tmp_path / "journal.sqlite3")
    calls: list[str] = []
    with pytest.raises(KubernetesUnresolvedError, match="lost"):
        reconcile_create(
            journal,
            binding(),
            cluster(),
            lambda _name: None,
            lambda _spec: calls.append("create"),
        )
    with pytest.raises(KubernetesUnresolvedError, match="inspect only"):
        reconcile_create(
            journal,
            binding(),
            cluster(),
            lambda _name: None,
            lambda _spec: calls.append("create"),
        )
    assert calls == ["create"]


def test_delete_ack_is_not_termination_confirmation(tmp_path) -> None:
    journal = KubernetesJobJournal(tmp_path / "journal.sqlite3")
    jobs = {binding().job_name: job()}
    template = deepcopy(canonical_job_spec(binding(), cluster())["spec"]["template"])
    template["metadata"].update(  # type: ignore[union-attr]
        {
            "namespace": cluster().namespace,
            "uid": "pod",
            "ownerReferences": [{"kind": "Job", "uid": "job-fixture", "controller": True}],
        }
    )
    pods = [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": template["metadata"],
            "spec": template["spec"],
            "status": {"phase": "Running"},
        }
    ]
    delete_calls = 0

    def delete(_name: str) -> None:
        nonlocal delete_calls
        delete_calls += 1
        if delete_calls == 2:
            jobs.clear()
            pods.clear()

    first = reconcile_cancellation(
        journal, binding(), cluster(), jobs.get, delete, lambda _uid: pods
    )
    assert first.termination_confirmed is False
    second = reconcile_cancellation(
        journal, binding(), cluster(), jobs.get, delete, lambda _uid: pods
    )
    assert second.termination_confirmed is True
