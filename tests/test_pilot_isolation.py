"""Offline contract checks; these do NOT evaluate CEL in a Kubernetes API server."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_comparison_pilot_remote import remote_fixture
from test_kubernetes_execution import _cluster

from tessera_worker.comparison_pilot import PilotError, digest
from tessera_worker.comparison_pilot_remote import RemoteKubernetes, load_target
from tessera_worker.kubernetes_execution import KubernetesOutputBinding
from tessera_worker.pilot_authority_probe import access_cases, dry_run_cases, probe_authority
from tessera_worker.pilot_isolation import DISPATCHER, OUTPUT, PilotIsolation

IMAGE = "example.test/python@sha256:" + "a" * 64


def profile():
    return PilotIsolation("org_local", "project_example", "tessera-control", IMAGE)


def observed(profile):
    objects = {
        item["kind"] + "/" + item["metadata"]["name"]: deepcopy(item)
        for item in profile.manifests()
    }
    for obj in objects.values():
        obj["metadata"].update(uid="observed-uid", generation=1)
        if obj["kind"] == "ValidatingAdmissionPolicy":
            obj["status"] = {"observedGeneration": 1, "typeChecking": {}}
    return objects


def readback(objects, calls):
    kinds = {
        "validatingadmissionpolicies": "ValidatingAdmissionPolicy",
        "validatingadmissionpolicybindings": "ValidatingAdmissionPolicyBinding",
        "resourcequotas": "ResourceQuota",
    }

    def request(method, path):
        calls.append((method, path))
        assert method == "GET"
        resource, name = path.split("/")[-2:]
        return objects.get(kinds[resource] + "/" + name)

    return request


def test_namespace_is_tenant_and_project_scoped_and_excludes_control():
    p = profile()
    assert p.namespace == profile().namespace
    assert p.namespace != replace(p, project_id="project_other").namespace
    assert p.namespace != replace(p, org_id="org_other").namespace
    with pytest.raises(PilotError, match="control state"):
        replace(p, control_namespace=p.namespace)
    with pytest.raises(PilotError, match="digest-pinned"):
        replace(p, image="python:latest")


def test_manifest_least_privilege_and_no_token_secret_or_control_mount():
    p = profile()
    items = p.manifests()
    assert len(items) == 13
    assert items[0]["kind"] == "ValidatingAdmissionPolicy"
    assert {i["metadata"]["namespace"] for i in items if "namespace" in i["metadata"]} == {
        p.namespace
    }
    assert not any(i["kind"] in {"Secret", "Pod", "Job", "Deployment"} for i in items)
    roles = [i for i in items if i["kind"] in {"Role", "ClusterRole"}]
    for role in roles:
        for rule in role["rules"]:
            assert "*" not in str(rule)
            if "namespaces" in rule["resources"]:
                assert rule["verbs"] == ["get"]
                assert rule["resourceNames"] == ["kube-system", p.namespace]
    # Evaluate resource-based RBAC for THESE manifests only, not a live audit.
    for expected, attrs in access_cases(p):
        allowed = False
        for role in roles:
            if role["kind"] == "Role" and attrs["namespace"] != p.namespace:
                continue
            for rule in role["rules"]:
                resource = attrs["resource"] + (
                    "/" + attrs["subresource"] if attrs["subresource"] else ""
                )
                if (
                    attrs["group"] in rule["apiGroups"]
                    and resource in rule["resources"]
                    and attrs["verb"] in rule["verbs"]
                    and ("resourceNames" not in rule or attrs["name"] in rule["resourceNames"])
                ):
                    allowed = True
        assert allowed is expected, attrs
    sa = next(i for i in items if i["kind"] == "ServiceAccount")
    assert sa["metadata"]["name"] == DISPATCHER and sa["automountServiceAccountToken"] is False


def test_policy_checks_jobs_and_pods_by_namespace_name_not_labels():
    p = profile()
    for resource in ("jobs", "pods"):
        spec = p.policy_spec(resource)
        assert spec["failurePolicy"] == "Fail"
        assert spec["matchConditions"] == [
            {
                "name": "execution-namespace",
                "expression": "request.namespace == " + json.dumps(p.namespace),
            }
        ]
        assert spec["matchConstraints"]["namespaceSelector"] == {}
        assert spec["matchConstraints"]["objectSelector"] == {}
        (rule,) = spec["matchConstraints"]["resourceRules"]
        assert rule["operations"] == ["CREATE", "UPDATE"]
        assert rule["resources"] == (
            ["jobs"] if resource == "jobs" else ["pods", "pods/ephemeralcontainers"]
        )
        assert p.binding_spec(resource)["validationActions"] == ["Deny"]
        expressions = " ".join(v["expression"] for v in spec["validations"])
        for required in (
            "automountServiceAccountToken",
            "serviceAccountName == 'default'",
            "pilot-output",
            "initContainers",
            "ephemeralContainers",
            "envFrom",
            "hostNetwork",
            "hostPID",
            "hostIPC",
            "readOnlyRootFilesystem",
            "capabilities",
            "print(1)",
            "print(2)",
            IMAGE,
            "volumeMounts",
        ):
            assert required in expressions
        assert ("activeDeadlineSeconds <= 120" in expressions) is (resource == "jobs")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "namespace-label",
        "missing-policy",
        "warn-binding",
        "policy-change",
        "stale-generation",
        "type-warning",
        "quota",
    ],
)
def test_readback_fail_closed_for_missing_or_weakened_enforcement(fault):
    p = profile()
    objects = observed(p)
    ns = objects["Namespace/" + p.namespace]
    policy = objects["ValidatingAdmissionPolicy/" + p.namespace + "-jobs"]
    if fault == "namespace-label":
        ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] = "baseline"
    elif fault == "missing-policy":
        del objects["ValidatingAdmissionPolicy/" + p.namespace + "-pods"]
    elif fault == "warn-binding":
        objects["ValidatingAdmissionPolicyBinding/" + p.namespace + "-jobs"]["spec"][
            "validationActions"
        ] = ["Warn"]
    elif fault == "policy-change":
        policy["spec"]["matchConditions"] = []
    elif fault == "stale-generation":
        policy["status"]["observedGeneration"] = 0
    elif fault == "type-warning":
        policy["status"]["typeChecking"]["expressionWarnings"] = [{"warning": "invalid CEL"}]
    elif fault == "quota":
        objects["ResourceQuota/pilot-bounds"]["spec"]["hard"]["pods"] = "100"
    calls = []
    if fault:
        with pytest.raises(PilotError):
            p.verify(readback(objects, calls), ns)
    else:
        p.verify(readback(objects, calls), ns)
        assert len(calls) == 5


def test_shared_namespace_rejected_before_credential_read(tmp_path):
    p = profile()
    with pytest.raises(PilotError, match="dedicated project"):
        RemoteKubernetes(
            _cluster(),
            KubernetesOutputBinding(OUTPUT, "/outputs", "pvc-uid", "a" * 64),
            tmp_path / "missing-ca",
            tmp_path / "missing-token",
            None,
            {
                "org_id": p.org_id,
                "project_id": p.project_id,
                "service_account": DISPATCHER,
                "expires_at": datetime.now(UTC).isoformat(),
            },
            datetime.now(UTC),
            None,
            isolation={"control_namespace": p.control_namespace, "image": IMAGE},
        )


def test_null_network_evidence_can_be_loaded_but_does_not_become_enforced():
    from dataclasses import asdict

    cluster = asdict(_cluster()) | {"network_none_evidence": None}
    result, _ = load_target(
        {
            "cluster": cluster,
            "output": {
                "pvc_name": OUTPUT,
                "mount_path": "/outputs",
                "pvc_uid": "uid",
                "pvc_spec_sha256": "a" * 64,
            },
        }
    )
    assert result.network_none_evidence is None


def fake_probe_remote(tmp_path):
    remote, journal = remote_fixture(tmp_path)
    p = profile()
    remote.isolation = p
    remote.cluster = replace(_cluster(), namespace=p.namespace, network_none_evidence=None)
    remote.scope = {
        "org_id": p.org_id,
        "project_id": p.project_id,
        "service_account": DISPATCHER,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    remote.jobs_path = f"/apis/batch/v1/namespaces/{p.namespace}/jobs"
    remote.output = KubernetesOutputBinding(
        OUTPUT, "/outputs", "pvc-uid", digest({"synthetic": True})
    )
    objects = observed(p)
    access = iter(access_cases(p))
    shapes = iter(dry_run_cases(remote.cluster, remote.output, IMAGE))
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path))
        if path.endswith("selfsubjectreviews"):
            return {
                "status": {
                    "userInfo": {"username": f"system:serviceaccount:{p.namespace}:{DISPATCHER}"}
                }
            }
        if path.endswith("selfsubjectaccessreviews"):
            expected, attrs = next(access)
            assert body["spec"]["resourceAttributes"] == attrs
            return {"status": {"allowed": expected}}
        if path.endswith("?dryRun=All"):
            assert kwargs == {"admission_probe": True}
            _, allowed, _ = next(shapes)
            return body if allowed else {"admission_denied": True, "policy": p.namespace}
        if path.endswith("persistentvolumeclaims/" + OUTPUT):
            return {"metadata": {"uid": remote.output.pvc_uid}, "spec": {"synthetic": True}}
        if path.endswith("namespaces/kube-system"):
            return {"metadata": {"uid": remote.cluster.cluster_uid}}
        if path.endswith("namespaces/" + p.namespace):
            return {"metadata": {"uid": remote.cluster.namespace_uid, "labels": p.labels}}
        return readback(objects, [])(method, path)

    remote.request = request
    return remote, journal, calls


def test_bounded_probe_uses_actual_subject_identity_and_only_dry_run_jobs(tmp_path):
    remote, journal, calls = fake_probe_remote(tmp_path)
    try:
        result = probe_authority(remote)
        assert result["state"] == "passed" and result["workloads_started"] is False
        assert result["network_enforcement"] == "unverified"
        assert result["calls"] == len(calls) <= 64
        assert len(result["checks"]) == 44
        assert not any(method == "DELETE" for method, _ in calls)
        assert all(
            path.endswith("?dryRun=All")
            for method, path in calls
            if method == "POST" and "/jobs" in path
        )
    finally:
        journal.close()


@pytest.mark.parametrize(
    "fault",
    [
        "extra-permission",
        "generic-denial",
        "admission-bypass",
        "cluster-uid",
        "no-network-evidence",
    ],
)
def test_probe_and_submission_failures_do_not_start_workloads(tmp_path, fault):
    remote, journal, calls = fake_probe_remote(tmp_path)
    original = remote.request

    def request(method, path, body=None, **kwargs):
        result = original(method, path, body, **kwargs)
        if fault == "extra-permission" and path.endswith("selfsubjectaccessreviews"):
            return {"status": {"allowed": True}}
        if fault == "generic-denial" and path.endswith("?dryRun=All"):
            raise PilotError("generic 403 is not admission evidence")
        if fault == "admission-bypass" and path.endswith("?dryRun=All"):
            return body
        if fault == "cluster-uid" and path.endswith("namespaces/kube-system"):
            return {"metadata": {"uid": "replacement"}}
        return result

    remote.request = request
    try:
        with pytest.raises(PilotError):
            if fault == "no-network-evidence":
                remote.verify_target(submission=True)
            else:
                probe_authority(remote)
        assert all(
            path.endswith("?dryRun=All")
            for method, path in calls
            if method == "POST" and "/jobs" in path
        )
    finally:
        journal.close()


def test_example_render_is_offline_and_default_cli_ignores_config(monkeypatch, capsys):
    script = Path(__file__).parents[1] / "scripts/check_pilot_isolation.py"
    spec = importlib.util.spec_from_file_location("check_pilot_isolation", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main = module.main

    monkeypatch.setattr(
        RemoteKubernetes, "__init__", lambda *_args, **_kwargs: pytest.fail("remote constructed")
    )
    assert main(["--config", "/does-not-exist"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "not_started"
    example = Path(__file__).parents[1] / "deploy/kubernetes/pilot-isolation.example.json"
    assert main(["--render", "--config", str(example)]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 13
    assert main(["--probe"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "blocked"
