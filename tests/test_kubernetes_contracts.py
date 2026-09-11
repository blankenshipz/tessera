from __future__ import annotations

from copy import deepcopy

import pytest

from tessera_worker.kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesContractError,
    KubernetesJobBinding,
    KubernetesJobJournal,
    KubernetesNetworkPolicyEvidence,
    KubernetesUnresolvedError,
    cluster_identity_digest,
    domain_identifier_from_resource_name,
    job_manifest,
    job_manifest_digest,
    kubernetes_resource_name,
    parse_job,
    parse_pod,
)


def _cluster() -> KubernetesClusterConfig:
    return KubernetesClusterConfig(
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
        network_none_evidence=KubernetesNetworkPolicyEvidence(
            "network-policy-enforced-v1", "tessera-deny-all", "policy-fixture", "f" * 64
        ),
    )


def _binding(**changes: object) -> KubernetesJobBinding:
    values: dict[str, object] = {
        "operation_id": "1" * 32,
        "attempt_id": "attempt-1",
        "lease_id": "lease-1",
        "epoch": 7,
        "package_sha256": "d" * 64,
        "image": "registry.example/tessera/opencode@sha256:" + "e" * 64,
        "command": ("opencode", "serve"),
        "active_deadline_seconds": 300,
    }
    values.update(changes)
    return KubernetesJobBinding(**values)  # type: ignore[arg-type]


def _job(
    binding: KubernetesJobBinding, *, uid: str = "job-fixture", state: str = "running"
) -> dict:
    job = deepcopy(job_manifest(binding, _cluster()))
    job["metadata"]["uid"] = uid
    job["status"] = {"active": 1} if state == "running" else {state: 1}
    return job


def _pod(binding: KubernetesJobBinding, job_uid: str = "job-fixture") -> dict:
    template = deepcopy(job_manifest(binding, _cluster())["spec"]["template"])
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            **template["metadata"],
            "namespace": _cluster().namespace,
            "uid": "pod-fixture",
            "ownerReferences": [{"kind": "Job", "uid": job_uid, "controller": True}],
        },
        "spec": template["spec"],
        "status": {"phase": "Running"},
    }


def test_explicit_identity_and_digest_pinned_manifest_are_deterministic() -> None:
    binding = _binding()
    manifest = job_manifest(binding, _cluster())

    assert binding.job_name == "tessera-op-" + binding.operation_id
    assert job_manifest(binding, _cluster()) == manifest
    assert manifest["metadata"]["annotations"]["tessera.dev/spec-sha256"] == job_manifest_digest(
        manifest
    )
    assert (
        manifest["metadata"]["annotations"]
        == manifest["spec"]["template"]["metadata"]["annotations"]
    )
    assert manifest["metadata"]["annotations"]["tessera.dev/package-sha256"] == "d" * 64
    assert manifest["metadata"]["annotations"]["tessera.dev/cluster-uid"] == "cluster-fixture"
    assert manifest["metadata"]["annotations"]["tessera.dev/cluster-config-sha256"] == (
        cluster_identity_digest(_cluster())
    )
    for key in ("operation", "attempt", "lease", "epoch"):
        assert f"tessera.dev/{key}" in manifest["metadata"]["labels"]


def test_domain_identifiers_are_reversibly_encoded_for_labels_and_preserved_in_annotations() -> (
    None
):
    binding = _binding(
        attempt_id="experiment_admitted_attempt_" + "a" * 32,
        lease_id="lease_" + "b" * 32,
    )
    manifest = job_manifest(binding, _cluster())
    metadata = manifest["metadata"]
    annotations = metadata["annotations"]
    labels = metadata["labels"]

    assert len(binding.attempt_id) == 60 and "_" in binding.attempt_id
    assert annotations["tessera.dev/attempt-id"] == binding.attempt_id
    assert annotations["tessera.dev/lease-id"] == binding.lease_id
    assert annotations["tessera.dev/operation-id"] == binding.operation_id
    for field, identifier in (("attempt", binding.attempt_id), ("lease", binding.lease_id)):
        encoded = kubernetes_resource_name(identifier)
        assert labels[f"tessera.dev/{field}"] == encoded
        assert annotations[f"tessera.dev/{field}-resource-name"] == encoded
        assert domain_identifier_from_resource_name(encoded) == identifier
    assert parse_job(_job(binding), binding, _cluster()).state == "running"

    tampered = _job(binding)
    tampered["metadata"]["annotations"]["tessera.dev/attempt-id"] = "renamed-attempt"
    with pytest.raises(KubernetesContractError, match="labels, or annotations conflict"):
        parse_job(tampered, binding, _cluster())


@pytest.mark.parametrize("value", ["-bad", "bad-", "bad-9", "bad-3zz"])
def test_resource_name_decoder_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        domain_identifier_from_resource_name(value)


def test_manifest_has_fixed_isolation_and_resource_fields() -> None:
    manifest = job_manifest(_binding(), _cluster())
    spec = manifest["spec"]
    pod_spec = spec["template"]["spec"]
    container = pod_spec["containers"][0]

    assert spec["activeDeadlineSeconds"] == 300
    assert spec["backoffLimit"] == 0
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["hostNetwork"] is False
    assert pod_spec["hostPID"] is False
    assert pod_spec["hostIPC"] is False
    assert pod_spec["volumes"] == [{"emptyDir": {"medium": "Memory"}, "name": "work"}]
    assert container["image"].endswith("@sha256:" + "e" * 64)
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": 65532,
    }
    assert container["resources"]["limits"] == container["resources"]["requests"]


def test_network_none_requires_explicit_target_capability_and_policy_evidence() -> None:
    missing_evidence = KubernetesClusterConfig(
        **{**_cluster().__dict__, "network_none_evidence": None}
    )

    with pytest.raises(KubernetesContractError, match="capability and enforcing-policy"):
        job_manifest(_binding(), missing_evidence)

    manifest = job_manifest(_binding(), _cluster())
    assert manifest["metadata"]["annotations"]["tessera.dev/network-mode"] == "none"
    assert manifest["metadata"]["annotations"]["tessera.dev/network-policy-uid"] == "policy-fixture"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kubeconfig", "relative-kubeconfig"),
        ("context", "CURRENT"),
        ("server", "http://127.0.0.1:8443"),
        ("server", "https://user:pass@127.0.0.1:8443"),
        ("namespace", "bad_namespace"),
    ],
)
def test_cluster_identity_rejects_ambient_or_malformed_values(field: str, value: str) -> None:
    values = _cluster().__dict__.copy()
    values[field] = value
    with pytest.raises(ValueError):
        KubernetesClusterConfig(**values)


def test_parse_job_and_pod_derive_stable_identity_and_reject_mismatches() -> None:
    binding = _binding()
    parsed = parse_job(_job(binding), binding, _cluster())
    pod = parse_pod(_pod(binding, parsed.job_uid), binding, _cluster(), parsed.job_uid)

    assert parsed.state == "running"
    assert parsed.provider_identity.provider_id.startswith("kubernetes-v1:")
    assert pod.state == "running"

    mismatched = _job(binding)
    mismatched["spec"]["template"]["spec"]["containers"][0]["image"] = "unsafe:latest"
    with pytest.raises(KubernetesContractError, match="immutable spec"):
        parse_job(mismatched, binding, _cluster())

    wrong_pod = _pod(binding)
    wrong_pod["metadata"]["ownerReferences"][0]["uid"] = "other-job"
    with pytest.raises(KubernetesContractError, match="not owned"):
        parse_pod(wrong_pod, binding, _cluster(), parsed.job_uid)


def test_observations_allow_known_defaults_but_reject_unallowlisted_executable_fields() -> None:
    binding = _binding()
    observed_job = _job(binding)
    observed_job["spec"].update(
        {
            "completionMode": "NonIndexed",
            "completions": 1,
            "parallelism": 1,
            "suspend": False,
        }
    )
    observed_job["spec"]["template"]["spec"].update(
        {
            "dnsPolicy": "ClusterFirst",
            "schedulerName": "default-scheduler",
            "serviceAccount": "default",
            "serviceAccountName": "default",
        }
    )
    observed_job["spec"]["template"]["spec"]["containers"][0].update(
        {
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File",
        }
    )
    assert parse_job(observed_job, binding, _cluster()).state == "running"

    unsafe_job = deepcopy(observed_job)
    unsafe_job["spec"]["template"]["spec"]["initContainers"] = []
    with pytest.raises(KubernetesContractError, match="unallowlisted executable fields"):
        parse_job(unsafe_job, binding, _cluster())

    unsafe_pod = _pod(binding)
    unsafe_pod["spec"]["containers"][0]["securityContext"]["procMount"] = "Unmasked"
    with pytest.raises(KubernetesContractError, match="Pod spec conflicts"):
        parse_pod(unsafe_pod, binding, _cluster(), "job-fixture")


def test_observations_accept_omitted_false_host_defaults_but_reject_true() -> None:
    binding = _binding()
    observed_job = _job(binding)
    pod_spec = observed_job["spec"]["template"]["spec"]
    for field in ("hostIPC", "hostNetwork", "hostPID"):
        pod_spec.pop(field)

    assert parse_job(observed_job, binding, _cluster()).state == "running"
    pod_spec["hostNetwork"] = True
    with pytest.raises(KubernetesContractError, match="default hostNetwork conflicts"):
        parse_job(observed_job, binding, _cluster())


def test_malformed_or_unknown_observations_fail_closed() -> None:
    binding = _binding()
    malformed = _job(binding)
    malformed["metadata"].pop("uid")
    with pytest.raises(KubernetesContractError, match="UID"):
        parse_job(malformed, binding, _cluster())

    unknown = _job(binding)
    unknown["status"] = {"active": "maybe"}
    assert parse_job(unknown, binding, _cluster()).state == "unknown"

    contradictory = _job(binding)
    contradictory["status"] = {"active": 1, "succeeded": 1}
    with pytest.raises(KubernetesContractError, match="contradictory"):
        parse_job(contradictory, binding, _cluster())

    malformed_pod = _pod(binding)
    malformed_pod["metadata"]["ownerReferences"] = "not-a-list"
    with pytest.raises(KubernetesContractError, match="owner reference"):
        parse_pod(malformed_pod, binding, _cluster(), "job-fixture")

    foreign_pod = _pod(binding)
    foreign_pod["kind"] = "ConfigMap"
    with pytest.raises(KubernetesContractError, match="type"):
        parse_pod(foreign_pod, binding, _cluster(), "job-fixture")


def test_create_replay_and_lost_create_never_submit_a_second_job(tmp_path) -> None:
    binding = _binding()
    jobs: dict[str, dict] = {}
    create_calls: list[str] = []

    def inspect(name: str) -> dict | None:
        return jobs.get(name)

    def create(manifest: dict) -> None:
        create_calls.append(manifest["metadata"]["name"])
        jobs[manifest["metadata"]["name"]] = _job(binding)
        return None

    journal = KubernetesJobJournal(tmp_path / "journal.sqlite3")
    first = journal.reconcile_create(binding, _cluster(), inspect, create)
    replay = journal.reconcile_create(binding, _cluster(), inspect, create)

    assert first == replay
    assert create_calls == [binding.job_name]

    lost_calls: list[str] = []
    lost = KubernetesJobJournal(tmp_path / "lost.sqlite3")
    with pytest.raises(KubernetesUnresolvedError, match="lost"):
        lost.reconcile_create(
            binding, _cluster(), lambda _name: None, lambda _manifest: lost_calls.append("x")
        )
    with pytest.raises(KubernetesUnresolvedError, match="inspect only"):
        lost.reconcile_create(
            binding, _cluster(), lambda _name: None, lambda _manifest: lost_calls.append("x")
        )
    assert lost_calls == ["x"]


def test_duplicate_or_changed_binding_conflicts_without_provider_side_effect(tmp_path) -> None:
    binding = _binding()
    journal = KubernetesJobJournal(tmp_path / "journal.sqlite3")
    calls: list[str] = []

    def inspect(_name: str) -> dict:
        return _job(binding)

    journal.reconcile_create(binding, _cluster(), inspect, lambda _manifest: calls.append("create"))
    with pytest.raises(KubernetesContractError, match="durable intent"):
        journal.reconcile_create(
            _binding(image="registry.example/tessera/opencode@sha256:" + "f" * 64),
            _cluster(),
            inspect,
            lambda _manifest: calls.append("create"),
        )
    changed_cluster = _cluster().__dict__.copy()
    changed_cluster["context"] = "other-minikube"
    with pytest.raises(KubernetesContractError, match="durable intent"):
        journal.reconcile_create(
            binding,
            KubernetesClusterConfig(**changed_cluster),
            inspect,
            lambda _manifest: calls.append("create"),
        )
    assert calls == []


def test_invalid_observed_job_never_attempts_create(tmp_path) -> None:
    binding = _binding()
    journal = KubernetesJobJournal(tmp_path / "journal.sqlite3")
    calls: list[str] = []
    malformed = _job(binding)
    malformed["metadata"]["annotations"]["tessera.dev/package-sha256"] = "0" * 64

    with pytest.raises(KubernetesContractError):
        journal.reconcile_create(
            binding, _cluster(), lambda _name: malformed, lambda _manifest: calls.append("x")
        )
    assert calls == []


def test_cancel_acknowledgement_is_unresolved_until_fresh_job_and_pod_absence(tmp_path) -> None:
    binding = _binding()
    journal = KubernetesJobJournal(tmp_path / "journal.sqlite3")
    jobs = {binding.job_name: _job(binding)}
    pods = [_pod(binding)]
    deletes: list[tuple[str, str]] = []

    unresolved = journal.request_cancel(
        binding,
        _cluster(),
        jobs.get,
        lambda name, uid: deletes.append((name, uid)),
        lambda _uid: pods,
    )
    assert unresolved.termination_confirmed is False
    assert deletes == [(binding.job_name, "job-fixture")]

    def delete(name: str, uid: str) -> None:
        deletes.append((name, uid))
        jobs.pop(name)
        pods.clear()

    confirmed = journal.request_cancel(binding, _cluster(), jobs.get, delete, lambda _uid: pods)
    assert confirmed.termination_confirmed is True
    assert confirmed.job_observed is True
    assert confirmed.pods_observed is True


def test_cancel_converges_after_restart_without_recreating_an_absent_job(tmp_path) -> None:
    binding = _binding()
    journal_path = tmp_path / "journal.sqlite3"
    journal = KubernetesJobJournal(journal_path)
    jobs = {binding.job_name: _job(binding)}
    pods = [_pod(binding)]
    deletes: list[tuple[str, str]] = []

    def delete(name: str, uid: str) -> None:
        deletes.append((name, uid))
        jobs.pop(name)

    first = journal.request_cancel(binding, _cluster(), jobs.get, delete, lambda _uid: pods)
    assert first.termination_confirmed is False
    assert deletes == [(binding.job_name, "job-fixture")]

    pods.clear()
    restarted = KubernetesJobJournal(journal_path)
    confirmed = restarted.request_cancel(
        binding,
        _cluster(),
        jobs.get,
        lambda _name, _uid: pytest.fail("must not recreate or delete an absent Job"),
        lambda _uid: pods,
    )

    assert confirmed.termination_confirmed is True
    assert confirmed.job_observed is True
    assert confirmed.pods_observed is True


def test_cancel_before_create_persists_its_fence_before_callbacks_and_restart(tmp_path) -> None:
    binding = _binding()
    journal_path = tmp_path / "journal.sqlite3"
    journal = KubernetesJobJournal(journal_path)
    create_calls: list[str] = []

    def inspect_after_cancel(_name: str) -> None:
        import sqlite3

        with sqlite3.connect(journal_path) as connection:
            assert connection.execute(
                "SELECT cancellation_requested FROM kubernetes_jobs WHERE operation_id=?",
                (binding.operation_id,),
            ).fetchone() == (1,)
        return None

    cancelled = journal.request_cancel(
        binding,
        _cluster(),
        inspect_after_cancel,
        lambda _name, _uid: pytest.fail("must not delete an absent Job"),
        lambda _uid: pytest.fail("must not observe Pods without an attributed Job"),
    )

    assert cancelled.requested is True
    restarted = KubernetesJobJournal(journal_path)
    with pytest.raises(KubernetesUnresolvedError, match="durable cancellation"):
        restarted.reconcile_create(
            binding,
            _cluster(),
            lambda _name: pytest.fail("cancelled create must not inspect or recreate"),
            lambda _manifest: create_calls.append("create"),
        )
    assert create_calls == []


def test_observed_job_cancelled_before_restart_never_recreates_and_reconciles_pods(
    tmp_path,
) -> None:
    binding = _binding()
    journal_path = tmp_path / "journal.sqlite3"
    journal = KubernetesJobJournal(journal_path)
    jobs = {binding.job_name: _job(binding)}
    pods = [_pod(binding)]
    deletes: list[tuple[str, str]] = []
    create_calls: list[str] = []

    observed = journal.reconcile_create(
        binding,
        _cluster(),
        jobs.get,
        lambda _manifest: pytest.fail("must observe the existing Job, not create"),
    )
    assert observed.job_uid == "job-fixture"

    def delete(name: str, uid: str) -> None:
        deletes.append((name, uid))
        jobs.pop(name)

    unresolved = journal.request_cancel(binding, _cluster(), jobs.get, delete, lambda _uid: pods)
    assert unresolved.termination_confirmed is False
    assert deletes == [(binding.job_name, "job-fixture")]

    restarted = KubernetesJobJournal(journal_path)
    with pytest.raises(KubernetesUnresolvedError, match="durable cancellation"):
        restarted.reconcile_create(
            binding,
            _cluster(),
            lambda _name: pytest.fail("cancelled create must not inspect or recreate"),
            lambda _manifest: create_calls.append("create"),
        )
    assert create_calls == []

    pods.clear()
    confirmed = restarted.request_cancel(
        binding,
        _cluster(),
        jobs.get,
        lambda _name, _uid: pytest.fail("must not delete an absent Job"),
        lambda _uid: pods,
    )
    assert confirmed.termination_confirmed is True
    assert confirmed.pods_observed is True
