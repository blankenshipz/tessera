"""Raw observation inspection. Only supervisor-injected collectors may supply observations.

Neither a signature, a digest nor an agent-provided 'passed' field is accepted.
Fake observations are useful for regression only. Authenticated collector results
are one-use and locally issued; imported signed claims are not collector proof.
"""

from __future__ import annotations

import copy
import json

from tessera_control.store import canonical_digest

from .capability_activation import REQUIRED_PROBES, stamp
from .capability_effects import BackendBlocked
from .capability_kube_client import CollectionError
from .capability_runner_contract import PROFILE


def negative_jobs(plan):
    """Each admission challenge changes exactly the indicated forbidden capability."""
    changes = {
        "hostpath": lambda p: p["volumes"].__setitem__(
            0, {"name": "workspace", "hostPath": {"path": "/"}}
        ),
        "privileged": lambda p: p["containers"][0]["securityContext"].update(privileged=True),
        "hostnetwork": lambda p: p.update(hostNetwork=True),
        "hostpid-ipc": lambda p: p.update(hostPID=True, hostIPC=True),
        "extra-containers": lambda p: p["containers"].append(copy.deepcopy(p["containers"][0])),
        "image": lambda p: p["containers"][0].update(image="unapproved:latest"),
        "command": lambda p: p["containers"][0].update(command=["/bin/sh", "-c", "true"]),
        "control-pvc": lambda p: p["volumes"][1]["persistentVolumeClaim"].update(
            claimName="control"
        ),
        "secret": lambda p: p["containers"][0].update(envFrom=[{"secretRef": {"name": "secret"}}]),
        "ambient-token": lambda p: p.update(automountServiceAccountToken=True),
        "projected": lambda p: p["volumes"].__setitem__(
            0, {"name": "workspace", "projected": {"sources": []}}
        ),
        "extra-pvc": lambda p: p["volumes"].append(
            {"name": "extra", "persistentVolumeClaim": {"claimName": "other"}}
        ),
        "args": lambda p: p["containers"][0].update(args=["--shell"]),
        "service-account": lambda p: p.update(serviceAccountName="default"),
        "init": lambda p: p.update(initContainers=[copy.deepcopy(p["containers"][0])]),
        "ephemeral": lambda p: p.update(ephemeralContainers=[copy.deepcopy(p["containers"][0])]),
        "writable-root": lambda p: p["containers"][0]["securityContext"].update(
            readOnlyRootFilesystem=False
        ),
        "resources": lambda p: p["containers"][0]["resources"]["limits"].update(cpu="2"),
    }
    result = {}
    base = plan.job()
    for key, change in changes.items():
        job = copy.deepcopy(base)
        change(job["spec"]["template"]["spec"])
        result["admission-" + key] = job
    return result


RBAC_DENIALS = {
    "rbac-secrets": ("", "secrets", "get"),
    "rbac-token-issuance": ("", "serviceaccounts/token", "create"),
    "rbac-exec": ("", "pods/exec", "create"),
    "rbac-control-pvc": ("", "persistentvolumeclaims", "get"),
    "rbac-escalation": ("rbac.authorization.k8s.io", "rolebindings", "create"),
    "rbac-cross-namespace": ("batch", "jobs", "create"),
    "rbac-extra-pvc": ("", "persistentvolumeclaims", "create"),
    "rbac-extra-resource": ("apps", "deployments", "create"),
}


def probe_requests(plan):
    """Exact challenged inputs, not labels provided by a reporter."""
    m = plan.activation
    requests = {
        key: {"kind": "RuntimeProbe", "control": key, "nonce": m.nonce, "plan_digest": plan.digest}
        for key in REQUIRED_PROBES
    }
    requests.update(
        {key: {"dryRun": "All", "object": value} for key, value in negative_jobs(plan).items()}
    )
    requests["admission-positive"] = {"dryRun": "All", "object": plan.job()}
    for key, job in negative_jobs(plan).items():
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": plan.metadata(plan.name),
            "spec": job["spec"]["template"]["spec"],
        }
        requests["pod-" + key] = {"dryRun": "All", "object": pod}
    requests["pod-admission-positive"] = {
        "dryRun": "All",
        "object": {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": plan.metadata(plan.name),
            "spec": plan.job()["spec"]["template"]["spec"],
        },
    }
    for key, (group, resource, verb) in RBAC_DENIALS.items():
        ns = (
            m.control_namespace
            if key in {"rbac-cross-namespace", "rbac-control-pvc"}
            else m.namespace
        )
        resource, separator, subresource = resource.partition("/")
        requests[key] = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {
                "resourceAttributes": {
                    "namespace": ns,
                    "group": group,
                    "resource": resource,
                    "verb": verb,
                    **({"subresource": subresource} if separator else {}),
                }
            },
        }
    for key in ("cpu", "memory", "pids", "time"):
        requests["enforcement-" + key] = {
            "kind": "RuntimeProbe",
            "control": key,
            "nonce": m.nonce,
            "plan_digest": plan.digest,
        }
    return requests


def raw_object(obj):
    """Discard only server identity/status, never workload fields or mutating defaults.

    Supporting native defaults requires a reviewed Kubernetes-version contract;
    the authenticated collector rejects unknown defaulting rather than a wildcard.
    """
    result = copy.deepcopy(obj)
    result.pop("status", None)
    for key in ("uid", "resourceVersion", "generation", "creationTimestamp", "managedFields"):
        result["metadata"].pop(key, None)
    return result


def object_digests(plan):
    objects = plan.objects()
    by_kind = {o["kind"]: o for o in objects}
    return {
        "namespace_spec_digest": canonical_digest(plan.labels),
        "rbac_spec_digest": canonical_digest(
            [
                {k: v for k, v in o.items() if k != "metadata"}
                for o in objects
                if o["kind"] in {"Role", "RoleBinding"}
            ]
        ),
        "admission_spec_digest": canonical_digest(
            [o["spec"] for o in objects if o["apiVersion"].startswith("admissionregistration")]
        ),
        "network_policy_spec_digest": canonical_digest(by_kind["NetworkPolicy"]["spec"]),
        "resource_quota_spec_digest": canonical_digest(by_kind["ResourceQuota"]["spec"]),
    }


class RawEvidenceInspector:
    def __init__(self, *, client, now):
        self.client, self.now = client, now

    def inspect(self, plan):
        try:
            started = self.now()
            snapshot = self.client.collect(plan, probe_requests(plan))
            from .capability_collector import CollectedEvidence, KubernetesEvidenceCollector

            if isinstance(self.client, KubernetesEvidenceCollector):
                snapshot = self.client.unwrap(snapshot, plan)
            elif isinstance(snapshot, CollectedEvidence):
                raise ValueError
            if len(json.dumps(snapshot, allow_nan=False).encode()) > 2 * 1024 * 1024:
                raise ValueError
            self.validate(plan, snapshot, not_before=started)
            return canonical_digest(
                {
                    "operation": plan.activation.operation_id,
                    "package": plan.activation.package_digest,
                    "namespace": plan.activation.namespace_uid,
                    "plan": plan.digest,
                    "raw": snapshot,
                }
            )
        except CollectionError:
            raise
        except Exception:
            raise BackendBlocked("raw Kubernetes enforcement evidence rejected") from None

    def validate(self, plan, snapshot, *, not_before):
        m = plan.activation
        if set(snapshot) != {
            "plan_digest",
            "nonce",
            "cluster_digest",
            "observed_at",
            "objects",
            "pvc",
            "pv",
            "identity",
            "probes",
            "events",
            "rbac_inventory",
        }:
            raise ValueError
        self.fresh(snapshot["observed_at"], m, not_before)
        if (snapshot["plan_digest"], snapshot["nonce"], snapshot["cluster_digest"]) != (
            plan.digest,
            m.nonce,
            m.cluster_digest,
        ):
            raise ValueError
        if snapshot["identity"] != {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "SelfSubjectReview",
            "status": {"userInfo": {"username": m.dispatcher_identity}},
        }:
            raise ValueError
        expected = plan.objects()
        if len(snapshot["objects"]) != len(expected):
            raise ValueError
        for want, got in zip(expected, snapshot["objects"], strict=True):
            if (
                canonical_digest(raw_object(got)) != canonical_digest(want)
                or not got["metadata"].get("uid")
                or not got["metadata"].get("resourceVersion")
            ):
                raise ValueError
            kind = want["kind"]
            if kind == "Namespace" and (
                got["metadata"]["uid"] != m.namespace_uid
                or got.get("status") != {"phase": "Active"}
            ):
                raise ValueError
            if kind == "ServiceAccount" and got["metadata"]["uid"] != m.service_account_uid:
                raise ValueError
            if kind == "ValidatingAdmissionPolicy" and got.get("status") != {
                "observedGeneration": got["metadata"].get("generation"),
                "typeChecking": {"expressionWarnings": []},
            }:
                raise ValueError
            if kind == "ResourceQuota":
                status = got["status"]
                if status["hard"] != want["spec"]["hard"] or set(status["used"]) != set(
                    status["hard"]
                ):
                    raise ValueError
                # Prerequisites only: no existing Jobs/Pods or consumed compute capacity.
                expected_used = {k: "0" for k in status["hard"]}
                expected_used.update(
                    persistentvolumeclaims="1", **{"requests.storage": str(m.quota.output_bytes)}
                )
                if status["used"] != expected_used:
                    raise ValueError
        # Complete namespace RBAC inventory: no additional binding to the workload/dispatcher.
        if snapshot["rbac_inventory"] != [
            o for o in snapshot["objects"] if o["kind"] in {"Role", "RoleBinding"}
        ]:
            raise ValueError
        for field, digest in object_digests(plan).items():
            if getattr(m, field) != digest:
                raise ValueError
        pvc, pv = snapshot["pvc"], snapshot["pv"]
        if (
            pvc["kind"],
            pvc["metadata"]["name"],
            pvc["metadata"]["namespace"],
            pvc["metadata"]["uid"],
            pvc["metadata"]["labels"],
            pvc["metadata"]["annotations"],
        ) != (
            "PersistentVolumeClaim",
            m.pvc,
            m.namespace,
            m.pvc_uid,
            plan.labels,
            plan.annotations,
        ):
            raise ValueError
        if (
            pvc["status"] != {"phase": "Bound"}
            or canonical_digest(pvc["spec"]) != m.pvc_spec_digest
            or pvc["spec"]["resources"] != {"requests": {"storage": str(m.quota.output_bytes)}}
            or pvc["spec"]["accessModes"] != ["ReadWriteOncePod"]
            or pvc["spec"]["volumeMode"] != "Filesystem"
            or set(pvc["spec"])
            != {"resources", "accessModes", "volumeMode", "volumeName", "storageClassName"}
        ):
            raise ValueError
        # A CSI-backed exact claim; no hostPath/NFS/local/control volume adoption.
        if (
            pv["kind"] != "PersistentVolume"
            or pv["metadata"]["name"] != pvc["spec"]["volumeName"]
            or set(pv["spec"]) != {"claimRef", "csi", "persistentVolumeReclaimPolicy"}
            or pv["spec"]["claimRef"] != {"namespace": m.namespace, "name": m.pvc, "uid": m.pvc_uid}
            or pv["spec"]["persistentVolumeReclaimPolicy"] != "Retain"
            or set(pv["spec"]["csi"]) != {"driver", "volumeHandle"}
            or not all(pv["spec"]["csi"].values())
        ):
            raise ValueError
        if snapshot["events"] != {"kind": "EventList", "items": []}:
            # Unknown/contradictory/warning events cannot be silently ignored.
            raise ValueError
        requests = probe_requests(plan)
        if set(snapshot["probes"]) != set(requests):
            raise ValueError
        for key, request in requests.items():
            probe = snapshot["probes"][key]
            if set(probe) != {"request", "response", "observed_at", "nonce", "subject"}:
                raise ValueError
            self.fresh(probe["observed_at"], m, not_before)
            if (canonical_digest(probe["request"]), probe["nonce"], probe["subject"]) != (
                canonical_digest(request),
                m.nonce,
                plan.digest,
            ):
                raise ValueError
            policy_uid = next(
                o["metadata"]["uid"] for o in snapshot["objects"] if o["kind"] == "NetworkPolicy"
            )
            if canonical_digest(probe["response"]) != canonical_digest(
                self.expected_response(plan, key, policy_uid=policy_uid)
            ):
                raise ValueError

    def fresh(self, value, manifest, not_before):
        observed, now = stamp(value), self.now()
        if (
            not max(stamp(manifest.issued_at), not_before)
            <= observed
            <= now
            < stamp(manifest.expires_at)
        ):
            raise CollectionError("stale")
        if (now - observed).total_seconds() > 5:
            raise CollectionError("stale")

    @staticmethod
    def expected_response(plan, key, *, policy_uid):
        m = plan.activation
        if key.startswith(("admission-", "pod-admission-")) and key not in {
            "admission-positive",
            "pod-admission-positive",
            "admission-policy-mode",
        }:
            expected = {
                "kind": "Status",
                "status": "Failure",
                "code": 403,
                "reason": "Invalid",
                "details": {
                    "causes": [{"type": "FieldValueForbidden", "message": PROFILE, "field": "spec"}]
                },
            }
        elif key == "admission-positive":
            expected = {"code": 201, "object": plan.job()}
        elif key == "pod-admission-positive":
            expected = {
                "code": 201,
                "object": {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": plan.metadata(plan.name),
                    "spec": plan.job()["spec"]["template"]["spec"],
                },
            }
        elif key in RBAC_DENIALS:
            expected = {"subjects": []}
            group, resource, verb = RBAC_DENIALS[key]
            resource, separator, subresource = resource.partition("/")
            namespace = (
                m.control_namespace
                if key in {"rbac-cross-namespace", "rbac-control-pvc"}
                else m.namespace
            )
            for subject in (
                m.dispatcher_identity,
                f"system:serviceaccount:{m.namespace}:{m.service_account}",
            ):
                expected["subjects"].append(
                    {
                        "identity": {
                            "kind": "SelfSubjectReview",
                            "status": {"userInfo": {"username": subject}},
                        },
                        "review": {
                            "kind": "SelfSubjectAccessReview",
                            "status": {"allowed": False, "denied": True},
                        },
                        "negative_test": {
                            "kind": "Status",
                            "code": 403,
                            "reason": "Forbidden",
                            "details": {
                                "subject": subject,
                                "attributes": {
                                    "namespace": namespace,
                                    "group": group,
                                    "resource": resource,
                                    "verb": verb,
                                    **({"subresource": subresource} if separator else {}),
                                },
                                "authorizer": "RBAC",
                            },
                        },
                    }
                )
        elif key in {"output-byte-exhaustion", "output-inode-exhaustion", "ephemeral-exhaustion"}:
            limit = {
                "output-byte-exhaustion": m.quota.output_bytes,
                "output-inode-exhaustion": m.quota.output_inodes,
                "ephemeral-exhaustion": m.quota.ephemeral_bytes,
            }[key]
            expected = {
                "syscall": "write" if "inode" not in key else "openat",
                "errno": "EDQUOT",
                "limit": limit,
                "usage_before": limit,
                "usage_after": limit,
                "attempted": 1,
                "filesystem_uid": m.pvc_uid if key != "ephemeral-exhaustion" else m.namespace_uid,
                "mechanism": "filesystem-project-quota",
            }
        elif key.startswith("network-denied-"):
            expected = {
                "cni_verdict": "DROP",
                "policy_uid": policy_uid,
                "challenge": m.nonce,
                "destination_class": key.removeprefix("network-denied-"),
                "positive_control_challenge": m.nonce,
                "packets_sent": 1,
                "packets_dropped": 1,
            }
        elif key in {"network-allowed", "watchdog-health"}:
            expected = {
                "sent": m.nonce,
                "received": m.nonce,
                "peer": m.destination.url if key == "network-allowed" else m.watchdog.endpoint,
                "certificate_sha256": m.destination.certificate_sha256
                if key == "network-allowed"
                else m.watchdog.certificate_sha256,
                "identity": m.dispatcher_identity
                if key == "network-allowed"
                else m.watchdog.identity,
            }
        elif key == "tls-san-issuer":
            expected = {
                "peer_ip": m.destination.addresses[0],
                "certificate_sha256": m.destination.certificate_sha256,
                "issuer_sha256": m.destination.issuer_sha256,
                "verify_code": 0,
                "url": m.destination.url,
                "challenge": m.nonce,
            }
        elif key.startswith("tls-wrong-") or key == "redirect":
            expected = {"error": key, "bytes_delivered": 0, "connections": 1, "challenge": m.nonce}
        elif key in {"watchdog-broker-loss", "watchdog-revoke"}:
            expected = {
                "identity": m.watchdog.identity,
                "sequence": 1,
                "nonce": m.nonce,
                "job": {"kind": "Status", "code": 404, "reason": "NotFound"},
                "pods": {"kind": "PodList", "items": []},
                "elapsed_seconds": m.watchdog.lease_seconds,
                "credentials": "none-issued",
            }
        elif key.startswith("enforcement-"):
            resource = key.removeprefix("enforcement-")
            limit = {
                "cpu": m.quota.cpu_millicores,
                "memory": m.quota.memory_bytes,
                "pids": plan.pids,
                "time": m.timeout_seconds,
            }[resource]
            expected = {
                "resource": resource,
                "limit": limit,
                "attempted": limit + 1,
                "mechanism": "watchdog-deadline" if resource == "time" else "cgroup-v2",
                "kernel_event": {
                    "cpu": "throttled",
                    "memory": "oom_kill",
                    "pids": "max",
                    "time": "terminated",
                }[resource],
                "nonce": m.nonce,
            }
        elif key == "resource-quota":
            expected = {
                "kind": "Status",
                "code": 403,
                "reason": "Forbidden",
                "details": {"quota": plan.name, "resource": "pods", "requested": "2", "hard": "1"},
            }
        else:
            # Raw readback for structural controls, never an outcome boolean.
            expected = structural_response(plan, key)
        return expected


def structural_response(plan, key):
    m = plan.activation
    mapping = {
        "namespace": {"name": m.namespace, "uid": m.namespace_uid, "labels": plan.labels},
        "image-command": {
            "spec": plan.job()["spec"]["template"]["spec"]["containers"][0],
            "status": {"imageID": m.image, "restartCount": 0},
            "nonce": m.nonce,
        },
        "service-account": {
            "name": m.service_account,
            "uid": m.service_account_uid,
            "automountServiceAccountToken": False,
        },
        "output-pvc": {"name": m.pvc, "uid": m.pvc_uid, "namespace": m.namespace},
        "network-policy": next(o["spec"] for o in plan.objects() if o["kind"] == "NetworkPolicy"),
        "admission-policy-mode": [
            o["spec"] for o in plan.objects() if o["kind"].startswith("ValidatingAdmission")
        ],
        "dns-pins": m.destination.model_dump(mode="json"),
        "ambient-credentials": {
            "environment": [],
            "mounts": ["/workspace", "/outputs"],
            "token_paths": [],
        },
        "readonly-nonroot": {
            "uid": 65532,
            "root_write_errno": "EROFS",
            "effective_capabilities": "0",
        },
        "archive-escape": {"entry": "../escape", "errno": "installer-denied", "written_bytes": 0},
        "lockfile-closure": {"extra_artifacts": 1, "errno": "installer-denied", "written_bytes": 0},
    }
    return mapping[key]
