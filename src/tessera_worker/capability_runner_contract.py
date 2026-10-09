"""Capability-specific render/verification contract; rendering has no cluster effects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import Field

from tessera_control.capability_package import SignedCapabilityPackage
from tessera_control.store import canonical_digest

from .capability_activation import ActivationManifest, Frozen, canonical, stamp
from .capability_effects import BackendBlocked

PROFILE = "capability-runner-v1"
ADMISSION = "admissionregistration.k8s.io/v1"


@dataclass(frozen=True, repr=False)
class VerifiedCapability:
    package: SignedCapabilityPackage
    _issuer: object


class PackageVerifier:
    """Supervisor-owned authenticated control callback, NOT package.decode/signature presence.

    The callback must verify the signature and current tenant/lease/epoch/revocation.
    Default control authority rejects isolated packages. Global claim stays separate.
    """

    def __init__(self, *, authority, identity, approved_plan_digest=None, now=None):
        self.authority, self.identity = authority, tuple(identity)
        self.approved_plan_digest = approved_plan_digest
        self.now = now or (lambda: datetime.now(UTC))
        self._seal = object()

    def verify(self, package):
        result = VerifiedCapability(package, self._seal)
        self.require(result)
        return result

    def require(self, verified):
        try:
            if type(verified) is not VerifiedCapability or verified._issuer is not self._seal:
                raise ValueError
            body = verified.package.decode()
            if (
                body.mode != "isolated"
                or (body.org_id, body.project_id, body.worker_id) != self.identity
                or self.authority(verified.package) is not True
                or self.now() >= stamp(body.expires_at)
            ):
                raise ValueError
            return body
        except Exception:
            raise BackendBlocked("verified current isolated package required") from None


class ResourceBounds(Frozen):
    cpu_millicores: int = Field(ge=1, le=1000)
    memory_bytes: int = Field(ge=16 * 1024 * 1024, le=512 * 1024 * 1024)
    ephemeral_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    output_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    output_inodes: int = Field(ge=1, le=4096)
    pids: int = Field(ge=1, le=128)
    seconds: int = Field(ge=1, le=120)


class RunnerPlan(Frozen):
    activation: ActivationManifest
    # Both bounds must come from authenticated project/package admission, not workload JSON.
    project_bounds: ResourceBounds
    package_bounds: ResourceBounds
    pids: int = Field(ge=1, le=128)

    def validate_package(self, verified, verifier):
        try:
            # Reparse to defeat model_copy/model_construct validation bypasses.
            plan = RunnerPlan.model_validate_json(self.model_dump_json())
            if plan.digest != verifier.approved_plan_digest:
                raise ValueError
            m = plan.activation
            body = verifier.require(verified)
            if (
                (m.org_id, m.project_id, m.worker_id, m.operation_id, m.package_digest)
                != (
                    body.org_id,
                    body.project_id,
                    body.worker_id,
                    body.operation_id,
                    verified.package.digest,
                )
                or m.environment_digest != body.declaration.broker_basis.environment_digest
                or m.image.split("@", 1)[1] != body.binding.image_digest
                or m.destination.url != body.binding.action.url
                or not 0
                <= (verifier.now() - stamp(m.destination.dns_observed_at)).total_seconds()
                <= 5
                or not stamp(m.issued_at) <= verifier.now() < stamp(m.expires_at)
                or stamp(m.expires_at) > stamp(body.expires_at)
                or m.timeout_seconds
                > min(body.binding.action.max_seconds, body.declaration.max_seconds)
                or m.quota.output_bytes
                > min(body.binding.action.max_bytes, body.declaration.max_bytes)
                or body.binding.action.expected_size > m.quota.output_bytes
            ):
                raise ValueError
            requested = m.quota.model_dump(exclude={"mechanism"}) | {
                "pids": plan.pids,
                "seconds": m.timeout_seconds,
            }
            for bounds in (plan.project_bounds, plan.package_bounds):
                if any(value > getattr(bounds, key) for key, value in requested.items()):
                    raise ValueError
            return body
        except Exception:
            raise BackendBlocked("runner package or quota bounds mismatch") from None

    @property
    def digest(self):
        return hashlib.sha256(canonical(self)).hexdigest()

    @property
    def labels(self):
        m = self.activation
        # IDs can be 200 chars; label values cannot. Full nonce/digests also in annotations.
        return m.labels | {
            "tessera.io/tenant": canonical_digest(m.org_id)[:32],
            "tessera.io/project": canonical_digest(m.project_id)[:32],
            "tessera.io/operation": canonical_digest(m.operation_id)[:32],
            "tessera.io/package": m.package_digest[:32],
            "tessera.io/nonce": m.nonce[:32],
        }

    @property
    def annotations(self):
        m = self.activation
        return {
            "tessera.io/nonce": m.nonce,
            "tessera.io/package": m.package_digest,
            "tessera.io/plan": self.digest,
        }

    @property
    def name(self):
        return "cap-" + self.activation.nonce[:40]

    def metadata(self, name, *, namespaced=True):
        result = {"name": name, "labels": self.labels, "annotations": self.annotations}
        if namespaced:
            result["namespace"] = self.activation.namespace
        return result

    def resources(self):
        q = self.activation.quota
        return {
            "cpu": f"{q.cpu_millicores}m",
            "memory": str(q.memory_bytes),
            "ephemeral-storage": str(q.ephemeral_bytes),
        }

    def job(self, *, suspended=True):
        m = self.activation
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": self.metadata(self.name),
            "spec": {
                "suspend": suspended,
                "backoffLimit": 0,
                "parallelism": 1,
                "completions": 1,
                "activeDeadlineSeconds": m.timeout_seconds,
                "template": {
                    "metadata": {"labels": self.labels, "annotations": self.annotations},
                    "spec": {
                        "serviceAccountName": m.service_account,
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "hostNetwork": False,
                        "hostPID": False,
                        "hostIPC": False,
                        "restartPolicy": "Never",
                        "terminationGracePeriodSeconds": 1,
                        "dnsPolicy": "None",
                        "dnsConfig": {"nameservers": ["127.0.0.1"]},
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65532,
                            "runAsGroup": 65532,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "installer",
                                "image": m.image,
                                "imagePullPolicy": "Never",
                                "command": list(m.command),
                                "args": [
                                    "--package-digest",
                                    m.package_digest,
                                    "--nonce",
                                    m.nonce,
                                    "--operation-digest",
                                    canonical_digest(m.operation_id),
                                ],
                                "env": [],
                                "resources": {
                                    "requests": self.resources(),
                                    "limits": self.resources(),
                                },
                                "securityContext": {
                                    "privileged": False,
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {"name": "workspace", "mountPath": "/workspace"},
                                    {"name": "output", "mountPath": "/outputs"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "workspace",
                                "emptyDir": {"sizeLimit": str(m.quota.ephemeral_bytes)},
                            },
                            {"name": "output", "persistentVolumeClaim": {"claimName": m.pvc}},
                        ],
                    },
                },
            },
        }

    def validate_job(self, job, *, suspended=True):
        # Deliberately exact: no unknown fields, env, mounts, injected containers or resources.
        if job != self.job(suspended=suspended):
            raise BackendBlocked("noncanonical capability Job")

    def objects(self):
        """Operator-installed prerequisites. Runner never creates namespace/RBAC/PVCs."""
        m = self.activation
        hard = {
            f"{kind}.{key}": value
            for kind in ("requests", "limits")
            for key, value in self.resources().items()
        }
        hard |= {
            "pods": "1",
            "count/jobs.batch": "1",
            "persistentvolumeclaims": "1",
            "requests.storage": str(m.quota.output_bytes),
            "count/secrets": "0",
            "count/configmaps": "0",
            "services": "0",
            "count/deployments.apps": "0",
        }
        objects = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": self.metadata(m.namespace, namespaced=False),
            },
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": self.metadata(m.service_account),
                "automountServiceAccountToken": False,
            },
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": self.metadata(self.name),
                "spec": {"hard": hard},
            },
            {
                "apiVersion": "v1",
                "kind": "LimitRange",
                "metadata": self.metadata(self.name),
                "spec": {
                    "limits": [
                        {
                            "type": "Container",
                            "min": self.resources(),
                            "max": self.resources(),
                            "default": self.resources(),
                            "defaultRequest": self.resources(),
                        }
                    ]
                },
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": self.metadata(self.name),
                "spec": {
                    "podSelector": {},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [],
                    "egress": [
                        {
                            "to": [
                                {"ipBlock": {"cidr": ip + ("/128" if ":" in ip else "/32")}}
                                for ip in m.destination.addresses
                            ],
                            "ports": [{"protocol": "TCP", "port": 443}],
                        }
                    ],
                },
            },
        ]
        objects.extend(
            [
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": self.metadata(self.name),
                    "rules": [
                        {
                            "apiGroups": ["batch"],
                            "resources": ["jobs"],
                            "verbs": ["create", "get", "patch", "delete"],
                        },
                        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                    ],
                },
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": self.metadata(self.name),
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "Role",
                        "name": self.name,
                    },
                    "subjects": [
                        {
                            "kind": "User",
                            "apiGroup": "rbac.authorization.k8s.io",
                            "name": m.dispatcher_identity,
                        }
                    ],
                },
            ]
        )
        for resource in ("jobs", "pods"):
            name = self.name + "-" + resource
            spec = self.job()["spec"]["template"]["spec"]
            expressions = _cel_exact("variables.p", spec)
            expressions += [
                f"!has(variables.p.{field})"
                for field in (
                    "initContainers",
                    "ephemeralContainers",
                    "imagePullSecrets",
                    "hostAliases",
                    "nodeName",
                    "nodeSelector",
                    "tolerations",
                    "affinity",
                    "runtimeClassName",
                    "shareProcessNamespace",
                    "serviceAccount",
                )
            ]
            expressions += [
                f"!has(variables.p.containers[0].{field})"
                for field in (
                    "envFrom",
                    "volumeDevices",
                    "lifecycle",
                    "livenessProbe",
                    "readinessProbe",
                    "startupProbe",
                    "ports",
                    "stdin",
                    "tty",
                )
            ]
            expressions += ["!has(variables.p.securityContext.sysctls)"]
            expressions += [
                "!has(variables.p.containers[0].securityContext.capabilities.add)",
                "!has(variables.p.containers[0].securityContext.procMount)",
                "!has(variables.p.containers[0].securityContext.runAsUser)",
                "!has(variables.p.containers[0].securityContext.seccompProfile)",
                "!has(variables.p.securityContext.seLinuxOptions)",
                "!has(variables.p.securityContext.windowsOptions)",
                "!has(variables.p.securityContext.supplementalGroups)",
                "variables.p.containers[0].volumeMounts.all(m, !has(m.subPath) && "
                "!has(m.subPathExpr) && !has(m.mountPropagation))",
                "variables.p.volumes.all(v, !has(v.hostPath) && !has(v.projected) && "
                "!has(v.secret) && !has(v.configMap) && !has(v.csi) && !has(v.nfs))",
            ]
            if resource == "jobs":
                expressions += [
                    "object.spec.backoffLimit == 0",
                    "object.spec.parallelism == 1",
                    "object.spec.completions == 1",
                    f"object.spec.activeDeadlineSeconds == {m.timeout_seconds}",
                ]
            objects.extend(
                [
                    {
                        "apiVersion": ADMISSION,
                        "kind": "ValidatingAdmissionPolicy",
                        "metadata": self.metadata(name, namespaced=False),
                        "spec": {
                            "failurePolicy": "Fail",
                            "matchConstraints": {
                                "matchPolicy": "Exact",
                                "resourceRules": [
                                    {
                                        "apiGroups": ["batch" if resource == "jobs" else ""],
                                        "apiVersions": ["v1"],
                                        "operations": ["CREATE", "UPDATE"],
                                        "resources": [resource]
                                        if resource == "jobs"
                                        else ["pods", "pods/ephemeralcontainers"],
                                        "scope": "Namespaced",
                                    }
                                ],
                            },
                            "matchConditions": [
                                {
                                    "name": "namespace",
                                    "expression": "request.namespace == " + json.dumps(m.namespace),
                                }
                            ],
                            "variables": [
                                {
                                    "name": "p",
                                    "expression": "object.spec.template.spec"
                                    if resource == "jobs"
                                    else "object.spec",
                                }
                            ],
                            "validations": [
                                {"expression": e, "message": PROFILE} for e in expressions
                            ],
                        },
                    },
                    {
                        "apiVersion": ADMISSION,
                        "kind": "ValidatingAdmissionPolicyBinding",
                        "metadata": self.metadata(name, namespaced=False),
                        "spec": {"policyName": name, "validationActions": ["Deny"]},
                    },
                ]
            )
        return objects


def _cel_exact(path, value):
    """Explicit leaf checks, not object-vs-map equality (CEL Kubernetes typed objects)."""
    if isinstance(value, list):
        return [f"size({path}) == {len(value)}"] + [
            e for i, child in enumerate(value) for e in _cel_exact(f"{path}[{i}]", child)
        ]
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            entry = (
                f"{path}[{json.dumps(key)}]"
                if path.endswith((".limits", ".requests"))
                else f"{path}.{key}"
            )
            result.extend(_cel_exact(entry, child))
        if path.endswith((".limits", ".requests")):
            result.append(f"size({path}) == {len(value)}")
        return result
    return [f"{path} == {json.dumps(value)}"]
