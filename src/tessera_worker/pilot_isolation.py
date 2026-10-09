"""Pure operator manifests and live readback for the constant-compute pilot.

This is not provisioning authority. Rendering performs no I/O. Applying requires
an operator; the dispatch credential cannot change any of these safeguards.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .comparison_pilot import PilotError

DISPATCHER = "pilot-dispatcher"
OUTPUT = "pilot-output"
PROFILE = "constant-pilot-isolation-v1"
ADMISSION = "admissionregistration.k8s.io/v1"
RBAC = "rbac.authorization.k8s.io/v1"


@dataclass(frozen=True)
class PilotIsolation:
    org_id: str
    project_id: str
    control_namespace: str
    image: str

    def __post_init__(self):
        for value in (self.org_id, self.project_id):
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}", value
            ):
                raise PilotError("isolation requires label-safe organization/project identities")
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", self.control_namespace):
            raise PilotError("explicit control namespace required")
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", self.image):
            raise PilotError("isolation requires the approved digest-pinned image")
        if self.control_namespace == self.namespace:
            raise PilotError("execution namespace must not contain control state")

    @property
    def namespace(self):
        key = json.dumps([self.org_id, self.project_id], separators=(",", ":")).encode()
        return "tessera-exec-" + hashlib.sha256(key).hexdigest()[:24]

    @property
    def labels(self):
        return {
            "tessera.io/isolation": PROFILE,
            "tessera.io/org-id": self.org_id,
            "tessera.io/project-id": self.project_id,
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "v1.30",
        }

    def policy_spec(self, resource="jobs"):
        # Match the namespace NAME, not mutable workload/namespace selector labels.
        # Both Job templates and controller-created Pods are checked, including
        # ephemeral-container updates. Restricted PSA is an additional Pod gate.
        expressions = [
            "has(variables.p.automountServiceAccountToken) && "
            "!variables.p.automountServiceAccountToken",
            "!has(variables.p.serviceAccountName) || variables.p.serviceAccountName == 'default'",
            "!has(variables.p.serviceAccount) || variables.p.serviceAccount == 'default'",
            "!has(variables.p.imagePullSecrets) || size(variables.p.imagePullSecrets) == 0",
            "!has(variables.p.initContainers) || size(variables.p.initContainers) == 0",
            "!has(variables.p.ephemeralContainers) || size(variables.p.ephemeralContainers) == 0",
            "!has(variables.p.hostNetwork) || !variables.p.hostNetwork",
            "!has(variables.p.hostPID) || !variables.p.hostPID",
            "!has(variables.p.hostIPC) || !variables.p.hostIPC",
            "variables.p.restartPolicy == 'Never'",
            "size(variables.p.containers) == 1",
            "has(variables.p.volumes) && size(variables.p.volumes) == 2 && "
            "variables.p.volumes.all(v, (v.name == 'work' && has(v.emptyDir) && "
            "v.emptyDir.medium == 'Memory') || (v.name == 'persistent-output' && "
            "has(v.persistentVolumeClaim) && v.persistentVolumeClaim.claimName == 'pilot-output'))",
            "has(variables.p.securityContext) && variables.p.securityContext.runAsNonRoot && "
            "variables.p.securityContext.runAsUser == 65532 && "
            "variables.p.securityContext.runAsGroup == 65532 && "
            "variables.p.securityContext.seccompProfile.type == 'RuntimeDefault'",
            "variables.c.name == 'opencode'",
            "variables.c.image == " + json.dumps(self.image),
            "variables.c.imagePullPolicy == 'Never'",
            "variables.c.command == ['python', '-c', 'print(1)'] || "
            "variables.c.command == ['python', '-c', 'print(2)']",
            "!has(variables.c.args) || size(variables.c.args) == 0",
            "!has(variables.c.env) || size(variables.c.env) == 0",
            "!has(variables.c.envFrom) || size(variables.c.envFrom) == 0",
            "has(variables.c.volumeMounts) && size(variables.c.volumeMounts) == 2 && "
            "variables.c.volumeMounts.all(m, !has(m.subPath) && !has(m.subPathExpr) && "
            "!has(m.mountPropagation) && ((m.name == 'work' && m.mountPath == '/work') || "
            "(m.name == 'persistent-output' && m.mountPath == '/outputs')))",
            "!has(variables.c.volumeDevices) || size(variables.c.volumeDevices) == 0",
            "!has(variables.c.lifecycle) && !has(variables.c.livenessProbe) && "
            "!has(variables.c.readinessProbe) && !has(variables.c.startupProbe)",
            "has(variables.c.securityContext) && "
            "!variables.c.securityContext.allowPrivilegeEscalation && "
            "!variables.c.securityContext.privileged && "
            "variables.c.securityContext.readOnlyRootFilesystem && "
            "variables.c.securityContext.runAsNonRoot && "
            "variables.c.securityContext.runAsUser == 65532 && "
            "variables.c.securityContext.capabilities.drop == ['ALL'] && "
            "(!has(variables.c.securityContext.capabilities.add) || "
            "size(variables.c.securityContext.capabilities.add) == 0)",
            "size(variables.c.resources.limits) == 3 && "
            "variables.c.resources.limits == variables.c.resources.requests && "
            "quantity(variables.c.resources.limits['cpu']) == quantity('500m') && "
            "quantity(variables.c.resources.limits['memory']) == quantity('256Mi') && "
            "quantity(variables.c.resources.limits['ephemeral-storage']) == quantity('64Mi')",
            "request.resource.resource != 'jobs' || (object.spec.backoffLimit == 0 && "
            "object.spec.activeDeadlineSeconds > 0 && object.spec.activeDeadlineSeconds <= 120 && "
            "(!has(object.spec.parallelism) || object.spec.parallelism == 1) && "
            "(!has(object.spec.completions) || object.spec.completions == 1))",
        ]
        if resource not in {"jobs", "pods"}:
            raise ValueError("unsupported policy resource")
        if resource == "pods":
            expressions.pop()  # Job-level deadline/parallelism has no Pod field.
        return {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "matchPolicy": "Exact",
                "namespaceSelector": {},
                "objectSelector": {},
                "resourceRules": [
                    {
                        "apiGroups": [group],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE", "UPDATE"],
                        "resources": resources,
                        "scope": "Namespaced",
                    }
                    for group, resources in (
                        [("batch", ["jobs"])]
                        if resource == "jobs"
                        else [("", ["pods", "pods/ephemeralcontainers"])]
                    )
                ],
            },
            "matchConditions": [
                {
                    "name": "execution-namespace",
                    "expression": "request.namespace == " + json.dumps(self.namespace),
                }
            ],
            "variables": [
                {
                    "name": "p",
                    "expression": "object.spec.template.spec"
                    if resource == "jobs"
                    else "object.spec",
                },
                {"name": "c", "expression": "variables.p.containers[0]"},
            ],
            "validations": [
                {"expression": expression, "message": f"{PROFILE}: constraint {index}"}
                for index, expression in enumerate(expressions)
            ],
        }

    def binding_spec(self, resource="jobs"):
        return {"policyName": self.namespace + "-" + resource, "validationActions": ["Deny"]}

    def quota_spec(self):
        return {
            "hard": {
                "count/jobs.batch": "2",
                "pods": "2",
                "requests.cpu": "1",
                "limits.cpu": "1",
                "requests.memory": "512Mi",
                "limits.memory": "512Mi",
                "requests.ephemeral-storage": "128Mi",
                "limits.ephemeral-storage": "128Mi",
                "persistentvolumeclaims": "1",
                "requests.storage": "64Mi",
            }
        }

    def manifests(self):
        def obj(kind, name, *, api="v1", cluster=False, **fields):
            metadata = {"name": name}
            if not cluster:
                metadata["namespace"] = self.namespace
            if kind == "PersistentVolumeClaim":
                metadata["labels"] = {
                    "app.kubernetes.io/managed-by": "tessera",
                    "tessera.io/output-role": "dedicated-pilot-output",
                    "tessera.io/project-id": self.project_id,
                }
            return {"apiVersion": api, "kind": kind, "metadata": metadata, **fields}

        # Install policy/binding before any credential is issued. Rendering is
        # intentionally not apply, token issuance, migration, or teardown.
        admission = [
            obj(kind, self.namespace + "-" + resource, api=ADMISSION, cluster=True, spec=spec)
            for resource in ("jobs", "pods")
            for kind, spec in (
                ("ValidatingAdmissionPolicy", self.policy_spec(resource)),
                ("ValidatingAdmissionPolicyBinding", self.binding_spec(resource)),
            )
        ]
        return admission + [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": self.namespace, "labels": self.labels},
            },
            obj("ResourceQuota", "pilot-bounds", spec=self.quota_spec()),
            obj(
                "NetworkPolicy",
                "default-deny",
                api="networking.k8s.io/v1",
                spec={"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
            ),
            obj("ServiceAccount", DISPATCHER, automountServiceAccountToken=False),
            obj(
                "PersistentVolumeClaim",
                OUTPUT,
                spec={
                    "accessModes": ["ReadWriteOnce"],
                    "volumeMode": "Filesystem",
                    "storageClassName": "standard",
                    "resources": {"requests": {"storage": "64Mi"}},
                },
            ),
            obj(
                "Role",
                DISPATCHER,
                api=RBAC,
                rules=[
                    {
                        "apiGroups": ["batch"],
                        "resources": ["jobs"],
                        "verbs": ["create", "get", "delete"],
                    },
                    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                    {
                        "apiGroups": [""],
                        "resources": ["persistentvolumeclaims"],
                        "resourceNames": [OUTPUT],
                        "verbs": ["get"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["resourcequotas"],
                        "resourceNames": ["pilot-bounds"],
                        "verbs": ["get"],
                    },
                    {
                        "apiGroups": ["networking.k8s.io"],
                        "resources": ["networkpolicies"],
                        "resourceNames": ["default-deny"],
                        "verbs": ["get"],
                    },
                ],
            ),
            obj(
                "RoleBinding",
                DISPATCHER,
                api=RBAC,
                roleRef={
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": DISPATCHER,
                },
                subjects=[
                    {"kind": "ServiceAccount", "name": DISPATCHER, "namespace": self.namespace}
                ],
            ),
            obj(
                "ClusterRole",
                self.namespace,
                api=RBAC,
                cluster=True,
                rules=[
                    {
                        "apiGroups": [""],
                        "resources": ["namespaces"],
                        "resourceNames": ["kube-system", self.namespace],
                        "verbs": ["get"],
                    },
                    {
                        "apiGroups": ["admissionregistration.k8s.io"],
                        "resources": [
                            "validatingadmissionpolicies",
                            "validatingadmissionpolicybindings",
                        ],
                        "resourceNames": [self.namespace + "-jobs", self.namespace + "-pods"],
                        "verbs": ["get"],
                    },
                    {
                        "apiGroups": ["authentication.k8s.io"],
                        "resources": ["selfsubjectreviews"],
                        "verbs": ["create"],
                    },
                    {
                        "apiGroups": ["authorization.k8s.io"],
                        "resources": ["selfsubjectaccessreviews"],
                        "verbs": ["create"],
                    },
                ],
            ),
            obj(
                "ClusterRoleBinding",
                self.namespace,
                api=RBAC,
                cluster=True,
                roleRef={
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": self.namespace,
                },
                subjects=[
                    {"kind": "ServiceAccount", "name": DISPATCHER, "namespace": self.namespace}
                ],
            ),
        ]

    def verify(self, request, namespace):
        """Read current enforcement, not a caller's boolean attestation.

        UID verification is owned by RemoteKubernetes. Namespace separation is a
        storage/RBAC boundary, never evidence of network enforcement.
        """
        if any(
            namespace.get("metadata", {}).get("labels", {}).get(k) != v
            for k, v in self.labels.items()
        ):
            raise PilotError("dedicated execution namespace labels/Pod Security changed")
        for resource, plural, expected in (
            (kind, name, spec)
            for kind in ("jobs", "pods")
            for name, spec in (
                ("validatingadmissionpolicies", self.policy_spec(kind)),
                ("validatingadmissionpolicybindings", self.binding_spec(kind)),
            )
        ):
            value = request("GET", f"/apis/{ADMISSION}/{plural}/{self.namespace}-{resource}")
            if not value or value.get("spec") != expected:
                raise PilotError("pilot admission policy/binding missing or changed")
            if plural == "validatingadmissionpolicies":
                status = value.get("status", {})
                if (
                    status.get("observedGeneration") != value["metadata"].get("generation")
                    or "observedGeneration" not in status
                    or "typeChecking" not in status
                    or status["typeChecking"].get("expressionWarnings")
                ):
                    raise PilotError("pilot admission policy type checking not ready")
        quota = request("GET", f"/api/v1/namespaces/{self.namespace}/resourcequotas/pilot-bounds")
        if not quota or quota.get("spec") != self.quota_spec():
            raise PilotError("pilot resource quota missing or changed")
