"""Bounded, opt-in authorization/admission probes; never submit a real Job.

No network-isolation, PVC quota, control authority or cost claim is produced.
Dry-run success must precede policy-attributed negative cases. A generic 403,
quota rejection, API outage or CEL evaluation failure cannot count as a pass.
"""

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from datetime import UTC, datetime

from .comparison_pilot import PilotError, digest
from .kubernetes_contracts import KubernetesJobBinding, job_manifest
from .pilot_isolation import DISPATCHER, OUTPUT


def access_cases(isolation):
    ns = isolation.namespace
    # These are representative effective-authorization checks, not an exhaustive
    # cluster RBAC audit. They use the actual credential, never impersonation.
    cases = [
        (True, "create", "batch", "jobs", ns, "", ""),
        (True, "get", "batch", "jobs", ns, "probe", ""),
        (True, "delete", "batch", "jobs", ns, "probe", ""),
        (True, "list", "", "pods", ns, "", ""),
        (True, "get", "", "pods", ns, "probe", "log"),
        (True, "get", "", "persistentvolumeclaims", ns, OUTPUT, ""),
        (True, "get", "", "namespaces", "", ns, ""),
        (True, "get", "", "namespaces", "", "kube-system", ""),
        (False, "list", "", "namespaces", "", "", ""),
        (False, "patch", "", "namespaces", "", ns, ""),
        (False, "get", "", "secrets", ns, "probe", ""),
        (False, "create", "", "secrets", ns, "", ""),
        (False, "create", "", "serviceaccounts", ns, DISPATCHER, "token"),
        (False, "create", "", "pods", ns, "", ""),
        (False, "create", "", "pods", ns, "probe", "exec"),
        (False, "update", "", "pods", ns, "probe", "ephemeralcontainers"),
        (False, "create", "apps", "deployments", ns, "", ""),
        (False, "create", "batch", "cronjobs", ns, "", ""),
        (False, "create", "batch", "jobs", isolation.control_namespace, "", ""),
        (False, "create", "batch", "jobs", "kube-system", "", ""),
        (
            False,
            "get",
            "",
            "persistentvolumeclaims",
            isolation.control_namespace,
            "tessera-control-state",
            "",
        ),
        (False, "create", "", "persistentvolumeclaims", ns, "", ""),
        (False, "patch", "", "persistentvolumeclaims", ns, OUTPUT, ""),
        (False, "patch", "", "resourcequotas", ns, "pilot-bounds", ""),
        (False, "create", "networking.k8s.io", "networkpolicies", ns, "", ""),
        (False, "patch", "networking.k8s.io", "networkpolicies", ns, "default-deny", ""),
        (False, "create", "rbac.authorization.k8s.io", "rolebindings", ns, "", ""),
        (False, "escalate", "rbac.authorization.k8s.io", "roles", ns, DISPATCHER, ""),
        (False, "bind", "rbac.authorization.k8s.io", "clusterroles", "", "cluster-admin", ""),
        (False, "impersonate", "", "users", "", "probe", ""),
        (
            False,
            "patch",
            "admissionregistration.k8s.io",
            "validatingadmissionpolicies",
            "",
            ns + "-jobs",
            "",
        ),
        (
            False,
            "delete",
            "admissionregistration.k8s.io",
            "validatingadmissionpolicybindings",
            "",
            ns + "-jobs",
            "",
        ),
    ]
    return [
        (
            allowed,
            {
                "verb": verb,
                "group": group,
                "resource": resource,
                "namespace": namespace,
                "name": name,
                "subresource": subresource,
            },
        )
        for allowed, verb, group, resource, namespace, name, subresource in cases
    ]


def dry_run_cases(cluster, output, image):
    # No invented network-none evidence: this is an admission-only template which
    # is always sent with dryRun=All, not an executable package or pilot attempt.
    binding = KubernetesJobBinding(
        operation_id=uuid.uuid4().hex,
        attempt_id="authority-probe",
        lease_id="authority-probe",
        epoch=1,
        package_sha256="0" * 64,
        image=image,
        command=("python", "-c", "print(1)"),
        active_deadline_seconds=30,
        network_mode="cluster",
        **output.job_fields(),
    )
    baseline = job_manifest(binding, cluster)
    cases = [("baseline-shape", True, baseline)]

    def changed(name, mutate, allowed=False):
        value = deepcopy(baseline)
        mutate(value["spec"]["template"]["spec"])
        cases.append((name, allowed, value))

    changed(
        "intervention-shape",
        lambda p: p["containers"][0].update(command=["python", "-c", "print(2)"]),
        True,
    )
    changed(
        "control-pvc",
        lambda p: p["volumes"][1]["persistentVolumeClaim"].update(
            claimName="tessera-control-state"
        ),
    )
    changed(
        "host-path",
        lambda p: p["volumes"].__setitem__(0, {"name": "work", "hostPath": {"path": "/"}}),
    )
    changed(
        "projected-token",
        lambda p: p["volumes"].__setitem__(
            0,
            {
                "name": "work",
                "projected": {
                    "sources": [
                        {"serviceAccountToken": {"path": "token", "expirationSeconds": 600}}
                    ]
                },
            },
        ),
    )
    changed("dispatcher-account", lambda p: p.update(serviceAccountName=DISPATCHER))
    changed("automount-token", lambda p: p.update(automountServiceAccountToken=True))
    changed(
        "secret-env",
        lambda p: p["containers"][0].update(
            env=[
                {
                    "name": "UNAUTHORIZED",
                    "valueFrom": {"secretKeyRef": {"name": "probe", "key": "token"}},
                }
            ]
        ),
    )
    changed(
        "sidecar",
        lambda p: p["containers"].append(deepcopy(p["containers"][0]) | {"name": "sidecar"}),
    )
    changed("command", lambda p: p["containers"][0].update(command=["python", "-c", "print(3)"]))
    changed("privileged", lambda p: p["containers"][0]["securityContext"].update(privileged=True))
    changed("host-network", lambda p: p.update(hostNetwork=True))
    return cases


def probe_authority(remote):
    start = time.monotonic()
    original = remote.request
    calls = 0

    def bounded_request(method, path, body=None, **kwargs):
        nonlocal calls
        if calls >= 64 or time.monotonic() - start >= 120:
            raise PilotError("authority probe call/time bound exceeded")
        if method != "GET" and not (
            method == "POST"
            and (
                path
                in {
                    "/apis/authentication.k8s.io/v1/selfsubjectreviews",
                    "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                }
                or (
                    path == remote.jobs_path + "?dryRun=All"
                    and kwargs.get("admission_probe") is True
                )
            )
        ):
            raise PilotError("authority probe forbids persistent mutations")
        calls += 1
        return original(method, path, body, **kwargs)

    remote.request = bounded_request
    results = []
    try:
        remote.verify_target(authority_probe=True)
        pvc = remote.request(
            "GET", f"/api/v1/namespaces/{remote.cluster.namespace}/persistentvolumeclaims/{OUTPUT}"
        )
        if (
            not pvc
            or pvc["metadata"]["uid"] != remote.output.pvc_uid
            or digest(pvc["spec"]) != remote.output.pvc_spec_sha256
        ):
            raise PilotError("probe output PVC identity changed")
        for expected, attributes in access_cases(remote.isolation):
            result = remote.request(
                "POST",
                "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                {
                    "apiVersion": "authorization.k8s.io/v1",
                    "kind": "SelfSubjectAccessReview",
                    "spec": {"resourceAttributes": attributes},
                },
            )
            status = result.get("status", {})
            if status.get("allowed") is not expected or status.get("evaluationError"):
                raise PilotError("effective authorization differs from the pilot boundary")
            results.append({"access": attributes, "allowed": expected})
        for name, allowed, manifest in dry_run_cases(
            remote.cluster, remote.output, remote.isolation.image
        ):
            response = remote.request(
                "POST", remote.jobs_path + "?dryRun=All", manifest, admission_probe=True
            )
            if allowed:
                if (
                    response.get("kind") != "Job"
                    or response.get("metadata", {}).get("name") != manifest["metadata"]["name"]
                    or response.get("metadata", {}).get("namespace") != remote.cluster.namespace
                ):
                    raise PilotError("positive admission dry run failed")
            elif response != {"admission_denied": True, "policy": remote.isolation.namespace}:
                raise PilotError("forbidden Job shape was not denied by the selected policy")
            results.append({"dry_run": name, "allowed": allowed})
        return {
            "state": "passed",
            "workloads_started": False,
            "network_enforcement": "unverified",
            "cluster_uid": remote.cluster.cluster_uid,
            "namespace_uid": remote.cluster.namespace_uid,
            "observed_at": datetime.now(UTC).isoformat(),
            "calls": calls,
            "checks": results,
        }
    finally:
        remote.request = original
