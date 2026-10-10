"""Thin direct upstream Sandbox transport. No operator, Claim, router or exec API.

Lifecycle definitions follow Kubernetes SIGs Agent Sandbox v1.0.6 (Apache-2.0),
commit cd0761d5001baaf293460cd382ee318744d6b249. Tessera owns authority/fences.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

from tessera_control.agent_sandbox_models import API_VERSION, GATE, SandboxPlan
from tessera_control.store import canonical_digest

from .execution_contracts import (
    CollectionHandoff,
    ExecutionEnvelope,
    OperationHandle,
    TargetBinding,
)


class SandboxUnsafe(RuntimeError):
    """Identity, drift, prerequisites or termination cannot be established."""


def envelope(package):
    """Reuse the neutral immutable authority/target envelope, not Job records."""
    target = SandboxPlan.model_validate(package["sandbox"]).target
    return ExecutionEnvelope(
        operation_id=package["operation_id"],
        package_digest=canonical_digest(package),
        target=TargetBinding(
            "agent-sandbox", "agent-sandbox-network-none-v1", canonical_digest(target.model_dump())
        ),
        lease_id=package["lease_id"],
        epoch=package["epoch"],
        deadline=datetime.fromisoformat(package["sandbox_expires_at"].replace("Z", "+00:00")),
        payload=package,
    )


def collection_handoff(package, sandbox_uid):
    """Typed PVC-collector request; never mislabel a stopped Sandbox as a Job."""
    authority = envelope(package)
    return CollectionHandoff(
        OperationHandle(authority.target, authority.operation_id, sandbox_uid),
        tuple(package["output_paths"]),
    )


def authenticated_backend(target, ca_reference, token_reference, verify_witness):
    """Build the existing HTTPS transport from explicit operator credential refs.

    This is application runtime setup, not credential discovery/delivery. Nothing
    is loaded from current-context, environment, Keychain, cloud or worker payload.
    The operator supplies the trusted independent capability verifier separately.
    """
    import ipaddress
    from urllib.parse import urlsplit

    from .kubernetes_remote_service import NamespacedKubernetesClient

    # Keep DNS resolution out of the short authority writer section. The initial
    # fixed target uses a certificate-valid literal API address, not ambient DNS.
    ipaddress.ip_address(urlsplit(target.server).hostname)

    class ReferencePinnedClient(NamespacedKubernetesClient):
        def request(self, *args, **kwargs):
            reference = Path(target.kubeconfig)
            if not reference.is_file() or reference.stat().st_size > 1024 * 1024:
                raise SandboxUnsafe("explicit kubeconfig reference unavailable")
            import hashlib

            if hashlib.sha256(reference.read_bytes()).hexdigest() != target.kubeconfig_sha256:
                raise SandboxUnsafe("explicit kubeconfig reference digest changed")
            return super().request(*args, **kwargs)

    client = ReferencePinnedClient(target, Path(ca_reference), Path(token_reference))
    return AgentSandboxBackend(client, verify_witness)


def manifest(package):
    envelope(package)
    plan = SandboxPlan.model_validate(package["sandbox"])
    name = "tessera-sb-" + package["operation_id"]
    labels = {"tessera.dev/operation": package["operation_id"]}
    resource = {"cpu": f"{plan.cpu_millicores}m", "memory": str(plan.memory_bytes)}
    pod = {
        "runtimeClassName": plan.target.runtime_class,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "restartPolicy": "Never",
        "priority": 0,
        "preemptionPolicy": "PreemptLowerPriority",
        "activeDeadlineSeconds": package["plan"]["max_duration_seconds"],
        "schedulingGates": [{"name": GATE}],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "fsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "agent",
                "image": package["pinned_image"],
                "imagePullPolicy": "IfNotPresent",
                "command": package["plan"]["execution_spec"]["argv"],
                "workingDir": "/outputs",
                "resources": {"requests": resource, "limits": resource},
                "securityContext": {
                    "readOnlyRootFilesystem": True,
                    "allowPrivilegeEscalation": False,
                    "privileged": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [{"name": "outputs", "mountPath": "/outputs"}],
            }
        ],
        "volumes": [{"name": "outputs", "persistentVolumeClaim": {"claimName": "outputs-" + name}}],
    }
    return {
        "apiVersion": API_VERSION,
        "kind": "Sandbox",
        "metadata": {
            "name": name,
            "namespace": plan.target.namespace,
            "labels": labels,
            "annotations": {"tessera.dev/package": canonical_digest(package)},
        },
        "spec": {
            "operatingMode": "Suspended",
            "shutdownPolicy": "Retain",
            "shutdownTime": package["sandbox_expires_at"],
            "service": False,
            "podTemplate": {"metadata": {"labels": labels}, "spec": pod},
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "outputs"},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "volumeMode": "Filesystem",
                        "storageClassName": plan.storage_class,
                        "resources": {"requests": {"storage": str(plan.storage_bytes)}},
                    },
                }
            ],
        },
    }


def _identity(value, kind, *, uid=None, name=None, namespace=None):
    if not isinstance(value, dict) or value.get("kind") != kind:
        raise SandboxUnsafe(f"{kind} readback unavailable")
    metadata = value.get("metadata", {})
    if not metadata.get("uid") or not metadata.get("resourceVersion"):
        raise SandboxUnsafe(f"{kind} identity unavailable")
    for key, expected in (("uid", uid), ("name", name), ("namespace", namespace)):
        if expected is not None and metadata.get(key) != expected:
            raise SandboxUnsafe(f"{kind} identity changed")
    if metadata.get("deletionTimestamp"):
        raise SandboxUnsafe(f"{kind} is being deleted")
    return metadata


def _selected(selector, labels):
    if not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"}:
        raise SandboxUnsafe("unknown NetworkPolicy selector")
    matches = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(matches, dict) or not isinstance(expressions, list):
        raise SandboxUnsafe("malformed NetworkPolicy selector")
    if any(labels.get(key) != value for key, value in matches.items()):
        return False
    for item in expressions:
        key, op, values = item.get("key"), item.get("operator"), item.get("values", [])
        if op not in {"In", "NotIn", "Exists", "DoesNotExist"} or not isinstance(values, list):
            raise SandboxUnsafe("unknown NetworkPolicy expression")
        present = key in labels
        selected = {
            "In": present and labels.get(key) in values,
            "NotIn": not present or labels.get(key) not in values,
            "Exists": present,
            "DoesNotExist": not present,
        }[op]
        if not selected:
            return False
    return True


class AgentSandboxBackend:
    """An operator-injected bounded HTTPS client and independent witness verifier.

    No available=true shortcut: verify_witness must verify an independent measured
    capability record and return its canonical content. Unconfigured servers fail
    closed. Synthetic clients/verifiers are confined to isolated tests.
    """

    def __init__(self, client, verify_witness):
        self.client, self.verify_witness = client, verify_witness

    def path(self, target, kind, name=None):
        ns = quote(target.namespace, safe="")
        base = {
            "sandbox": f"/apis/{API_VERSION}/namespaces/{ns}/sandboxes",
            "pod": f"/api/v1/namespaces/{ns}/pods",
            "pvc": f"/api/v1/namespaces/{ns}/persistentvolumeclaims",
            "policy": f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies",
        }[kind]
        return base + ("/" + quote(name, safe="") if name else "")

    def list(self, target, kind, list_kind):
        items, tokens, uids, version = [], set(), set(), None
        token = ""
        for _ in range(100):
            query = {"limit": 100}
            if token:
                query["continue"] = token
            page = self.client.request("GET", self.path(target, kind) + "?" + urlencode(query))
            if (
                not isinstance(page, dict)
                or page.get("kind") != list_kind
                or not isinstance(page.get("items"), list)
                or len(page["items"]) > 100
            ):
                raise SandboxUnsafe("incomplete resource discovery")
            metadata = page.get("metadata", {})
            rv = metadata.get("resourceVersion")
            if not rv or (version is not None and rv != version):
                raise SandboxUnsafe("discovery snapshot changed")
            version = rv
            for item in page["items"]:
                uid = item.get("metadata", {}).get("uid")
                if not uid or uid in uids:
                    raise SandboxUnsafe("duplicate resource identity")
                items.append(item)
                uids.add(uid)
                if len(items) > 10000:
                    raise SandboxUnsafe("resource discovery exceeds bound")
            token = metadata.get("continue", "")
            if not isinstance(token, str) or len(token) > 16384 or token in tokens:
                raise SandboxUnsafe("invalid continuation")
            if not token:
                return items
            tokens.add(token)
        raise SandboxUnsafe("resource discovery exceeds pagination bound")

    def identity(self, target):
        cluster = self.client.cluster
        for field in (
            "kubeconfig",
            "kubeconfig_sha256",
            "context",
            "server",
            "server_sha256",
            "ca_sha256",
            "cluster_uid",
            "namespace",
            "namespace_uid",
        ):
            if getattr(cluster, field, None) != getattr(target, field):
                raise SandboxUnsafe("client target does not match approved pins")
        for name, uid in (
            ("kube-system", target.cluster_uid),
            (target.namespace, target.namespace_uid),
        ):
            value = self.client.request("GET", "/api/v1/namespaces/" + quote(name, safe=""))
            _identity(value, "Namespace", uid=uid, name=name)

    def preflight(self, target, labels):
        self.identity(target)
        crd = self.client.request(
            "GET",
            "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/sandboxes.agents.x-k8s.io",
        )
        _identity(crd, "CustomResourceDefinition", uid=target.crd_uid)
        if canonical_digest(crd.get("spec")) != target.crd_spec_sha256:
            raise SandboxUnsafe("upstream CRD drift")
        runtime = self.client.request(
            "GET", "/apis/node.k8s.io/v1/runtimeclasses/" + target.runtime_class
        )
        _identity(runtime, "RuntimeClass", uid=target.runtime_class_uid)
        if runtime.get("handler") != target.runtime_handler:
            raise SandboxUnsafe("strong runtime handler drift")
        controller = self.client.request(
            "GET",
            f"/apis/apps/v1/namespaces/{target.controller_namespace}/deployments/{target.controller_deployment}",
        )
        _identity(controller, "Deployment", uid=target.controller_uid)
        containers = (
            controller.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        )
        status = controller.get("status", {})
        if (
            len(containers) != 1
            or containers[0].get("image") != target.controller_image
            or status.get("observedGeneration") != controller["metadata"].get("generation")
            or status.get("availableReplicas", 0) < 1
        ):
            raise SandboxUnsafe("pinned upstream controller unavailable")
        if self.verify_witness is None:
            raise SandboxUnsafe("independent isolation/admission witness verifier unavailable")
        try:
            witness = self.verify_witness(target)
        except (OSError, ValueError):
            raise SandboxUnsafe("authenticated witness unavailable or invalid") from None
        if not isinstance(witness, dict) or canonical_digest(witness) != target.witness_sha256:
            raise SandboxUnsafe("measured capability witness conflicts with approved pin")
        try:
            observed = datetime.fromisoformat(witness["observed_at"].replace("Z", "+00:00"))
            age = datetime.now(UTC) - observed
        except (KeyError, TypeError, ValueError):
            raise SandboxUnsafe("measured witness timestamp unavailable") from None
        if age < timedelta(0) or age > timedelta(minutes=15):
            raise SandboxUnsafe("measured witness expired")
        expected = {
            "org_id": target.org_id,
            "project_id": target.project_id,
            "cluster_uid": target.cluster_uid,
            "namespace_uid": target.namespace_uid,
            "runtime_class_uid": target.runtime_class_uid,
            "runtime_handler": target.runtime_handler,
            "network": "ingress-egress-denied-with-positive-controls",
            "runtime": "independently-measured-isolation",
            "admission": "exclusive-writers-fixed-profile-one-uid-release",
        }
        if any(witness.get(key) != value for key, value in expected.items()):
            raise SandboxUnsafe("witness does not cover required isolation/authority boundaries")
        denied = set()
        for policy in self.list(target, "policy", "NetworkPolicyList"):
            spec = policy.get("spec", {})
            if not _selected(spec.get("podSelector"), labels):
                continue
            if spec.get("ingress") or spec.get("egress"):
                raise SandboxUnsafe("additive NetworkPolicy permits traffic")
            types = spec.get("policyTypes", ["Ingress"])
            if not isinstance(types, list) or set(types) - {"Ingress", "Egress"}:
                raise SandboxUnsafe("unknown NetworkPolicy types")
            denied.update(types)
        if denied != {"Ingress", "Egress"}:
            raise SandboxUnsafe("default-deny ingress and egress required before creation")

    def sandbox(self, target, expected, uid=None):
        value = self.client.request(
            "GET", self.path(target, "sandbox", expected["metadata"]["name"])
        )
        if value is None:
            return None
        metadata = _identity(
            value, "Sandbox", uid=uid, name=expected["metadata"]["name"], namespace=target.namespace
        )
        if value.get("apiVersion") != API_VERSION or metadata.get("ownerReferences"):
            raise SandboxUnsafe("Sandbox is not a direct pinned upstream resource")
        if (
            metadata.get("annotations", {}).get("tessera.dev/package")
            != expected["metadata"]["annotations"]["tessera.dev/package"]
        ):
            raise SandboxUnsafe("Sandbox package identity drift")
        if any(
            key in {"agents.x-k8s.io/pod-name", "agents.x-k8s.io/sandbox-template-ref"}
            or key.startswith("extensions.agents.x-k8s.io/")
            for key in metadata.get("annotations", {})
        ):
            raise SandboxUnsafe("unexpected mutable upstream adoption annotation")
        spec = deepcopy(value.get("spec", {}))
        spec["operatingMode"] = "Suspended"
        if spec != expected["spec"]:
            raise SandboxUnsafe("frozen Sandbox blueprint/lifecycle drift")
        return value

    def change_mode(self, target, value, mode):
        updated = deepcopy(value)
        updated.pop("status", None)
        updated["spec"]["operatingMode"] = mode
        return self.client.request(
            "PUT", self.path(target, "sandbox", value["metadata"]["name"]), updated
        )

    def pods(self, target, expected, sandbox_uid):
        result = []
        for pod in self.list(target, "pod", "PodList"):
            meta = pod.get("metadata", {})
            refs = meta.get("ownerReferences", [])
            owned = any(
                ref.get("apiVersion") == API_VERSION
                and ref.get("kind") == "Sandbox"
                and ref.get("uid") == sandbox_uid
                and ref.get("controller") is True
                for ref in refs
            )
            candidate = (
                meta.get("name") == expected["metadata"]["name"]
                or meta.get("labels", {}).get("tessera.dev/operation")
                == expected["metadata"]["labels"]["tessera.dev/operation"]
            )
            if candidate and not owned:
                raise SandboxUnsafe("misowned Sandbox Pod")
            if owned:
                if meta.get("namespace") != target.namespace or not meta.get("resourceVersion"):
                    raise SandboxUnsafe("Pod identity unavailable")
                result.append(pod)
        if len(result) > 1:
            raise SandboxUnsafe("multiple Sandbox-owned Pods")
        return result

    def validate_pod(self, expected, pod, *, gated):
        expected_labels = expected["spec"]["podTemplate"]["metadata"]["labels"]
        if any(
            pod.get("metadata", {}).get("labels", {}).get(key) != value
            for key, value in expected_labels.items()
        ):
            raise SandboxUnsafe("Pod operation metadata drift")
        wanted = expected["spec"]["podTemplate"]["spec"]
        spec = deepcopy(pod.get("spec", {}))
        if pod.get("apiVersion") != "v1" or pod.get("kind") != "Pod":
            raise SandboxUnsafe("invalid Pod type")
        # Known apiserver defaults only. Anything else (including injected sidecars,
        # env, hostPath, credentials, extra privileges or alternate PVC) is drift.
        defaults = {
            "dnsPolicy": "ClusterFirst",
            "schedulerName": "default-scheduler",
            "terminationGracePeriodSeconds": 30,
            "serviceAccountName": "default",
            "serviceAccount": "default",
        }
        for key, value in defaults.items():
            if key in spec and spec.pop(key) != value:
                raise SandboxUnsafe("unapproved Pod default")
        tolerations = spec.pop("tolerations", [])
        if any(
            item
            not in [
                {"key": key, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}
                for key in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")
            ]
            for item in tolerations
        ):
            raise SandboxUnsafe("unapproved toleration")
        spec.pop("nodeName", None)
        containers = spec.get("containers", [])
        for container in containers:
            for key, value in (
                ("terminationMessagePath", "/dev/termination-log"),
                ("terminationMessagePolicy", "File"),
            ):
                if key in container and container.pop(key) != value:
                    raise SandboxUnsafe("unapproved container default")
        if not gated:
            if spec.get("schedulingGates", []) not in ([], [{"name": GATE}]):
                raise SandboxUnsafe("unapproved scheduling gate")
            spec["schedulingGates"] = [{"name": GATE}]
        if spec != wanted:
            raise SandboxUnsafe("Pod does not match fixed approved profile")
        if gated and (
            pod.get("spec", {}).get("nodeName")
            or pod.get("status", {}).get("phase") not in {None, "Pending"}
        ):
            raise SandboxUnsafe("gated Pod has already executed")

    def release(self, target, pod):
        updated = deepcopy(pod)
        updated.pop("status", None)
        updated["spec"]["schedulingGates"] = []
        return self.client.request(
            "PUT", self.path(target, "pod", pod["metadata"]["name"]), updated
        )

    def node_identity(self, name, uid=None):
        if not name:
            raise SandboxUnsafe("executed Pod node identity unavailable")
        node = self.client.request("GET", "/api/v1/nodes/" + quote(name, safe=""))
        metadata = _identity(node, "Node", name=name, uid=uid)
        if not any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in node.get("status", {}).get("conditions", [])
        ):
            raise SandboxUnsafe("executed Pod node is unknown or unhealthy")
        return metadata["uid"]

    def pvc(self, target, expected, sandbox_uid, uid=None):
        name = "outputs-" + expected["metadata"]["name"]
        value = self.client.request("GET", self.path(target, "pvc", name))
        metadata = _identity(
            value, "PersistentVolumeClaim", uid=uid, name=name, namespace=target.namespace
        )
        if not any(
            ref.get("uid") == sandbox_uid
            and ref.get("kind") == "Sandbox"
            and ref.get("apiVersion") == API_VERSION
            and ref.get("controller") is True
            for ref in metadata.get("ownerReferences", [])
        ):
            raise SandboxUnsafe("output PVC ownership conflict")
        wanted = expected["spec"]["volumeClaimTemplates"][0]["spec"]
        spec = deepcopy(value.get("spec", {}))
        spec.pop("volumeName", None)
        if spec != wanted:
            raise SandboxUnsafe("output PVC specification drift")
        return value

    def validate_pvc_set(self, target, expected, sandbox_uid, pvc_uid):
        owned = []
        for item in self.list(target, "pvc", "PersistentVolumeClaimList"):
            metadata = item.get("metadata", {})
            if any(ref.get("uid") == sandbox_uid for ref in metadata.get("ownerReferences", [])):
                owned.append(item)
        if (
            len(owned) != 1
            or owned[0].get("kind") != "PersistentVolumeClaim"
            or owned[0]["metadata"].get("uid") != pvc_uid
            or owned[0]["metadata"].get("name") != "outputs-" + expected["metadata"]["name"]
        ):
            raise SandboxUnsafe("unknown Sandbox-owned PVC prevents custody/deletion")

    def delete(self, target, kind, value):
        metadata = value["metadata"]
        return self.client.request(
            "DELETE",
            self.path(target, kind, metadata["name"]),
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground",
                "preconditions": {
                    "uid": metadata["uid"],
                    "resourceVersion": metadata["resourceVersion"],
                },
            },
        )
