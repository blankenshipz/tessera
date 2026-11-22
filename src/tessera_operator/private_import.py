"""Credential-free PVC binding and fixed node-path admission (no private import yet)."""

from __future__ import annotations

import json
import os
import re
import time

from mcp.types import ToolAnnotations

from . import kubernetes as target
from .upgrade_support import _api_request as _api_request
from .upgrade_support import _receipt as _receipt

BINDER = "tessera-pvc-bind"
RECEIPT = target.SOURCE / "private/local-persistent-binder.json"
RELEASE = target.SOURCE / "private/local-persistent-binder-release.json"
EXPECTED = [
    ("PersistentVolumeClaim", "tessera-control-state"),
    ("Deployment", "tessera-control"),
    ("Service", "tessera-control"),
    ("NetworkPolicy", "default-deny"),
]


def _owned() -> tuple[dict, dict, dict]:
    record = _receipt(target.RESOURCE_RECEIPT)
    created = record.get("created", [])
    if (
        not isinstance(created, list)
        or [(entry.get("kind"), entry.get("name")) for entry in created] != EXPECTED
    ):
        raise target.AdmissionError("inert provisioning incomplete")
    namespace = target._kubectl("get", "namespace", target.NAMESPACE, "-o", "json")
    meta = namespace.get("metadata", {})
    if meta.get("uid") != record.get("namespace_uid") or meta.get("labels", {}).get(
        "tessera.io/local-operator-nonce"
    ) != record.get("nonce"):
        raise target.AdmissionError("namespace identity changed")
    pvc = target._kubectl(
        "-n", target.NAMESPACE, "get", "pvc", "tessera-control-state", "-o", "json"
    )
    if (
        pvc.get("metadata", {}).get("uid") != created[0].get("uid")
        or pvc.get("spec", {}).get("storageClassName") != "standard"
    ):
        raise target.AdmissionError("PVC identity changed")
    deployment = target._kubectl(
        "-n", target.NAMESPACE, "get", "deployment", "tessera-control", "-o", "json"
    )
    if (
        deployment.get("metadata", {}).get("uid") != created[1].get("uid")
        or deployment.get("spec", {}).get("replicas") != 0
    ):
        raise target.AdmissionError("control writer already active")
    policy = target._kubectl(
        "-n", target.NAMESPACE, "get", "networkpolicy", "default-deny", "-o", "json"
    )
    if policy.get("metadata", {}).get("uid") != created[3].get("uid"):
        raise target.AdmissionError("network policy identity changed")
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    if not re.fullmatch(r"docker\.io/library/tessera-local-persistent@sha256:[a-f0-9]{64}", image):
        raise target.AdmissionError("immutable image unavailable")
    return record, pvc, deployment


def binder_create() -> dict:
    """Create only a credential-free fixed Pod to trigger WaitForFirstConsumer."""
    record, pvc, deployment = _owned()
    if pvc.get("status", {}).get("phase") != "Pending":
        raise target.AdmissionError("PVC not waiting for first consumer")
    if RECEIPT.exists() or RECEIPT.is_symlink():
        raise target.AdmissionError("prior binder attempt retained")
    if target._kubectl("-n", target.NAMESPACE, "get", "pods", "-o", "json").get("items") != []:
        raise target.AdmissionError("namespace contains Pod; refuse binder")
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": BINDER,
            "namespace": target.NAMESPACE,
            "labels": {
                "tessera.io/local-operator-nonce": record["nonce"],
                "tessera.io/operator-scope": "local-persistent-binder",
            },
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "fsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "binder",
                    "image": image,
                    "imagePullPolicy": "Never",
                    "command": ["python", "-c", "import time; time.sleep(120)"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "state", "mountPath": "/state"}],
                }
            ],
            "volumes": [
                {"name": "state", "persistentVolumeClaim": {"claimName": "tessera-control-state"}}
            ],
        },
    }
    # Durable intent precedes the create. A lost response is not retried/adopted.
    fd = os.open(RECEIPT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(
            json.dumps(
                {
                    "namespace_uid": record["namespace_uid"],
                    "pvc_uid": pvc["metadata"]["uid"],
                    "pod_uid": None,
                }
            ).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(RECEIPT.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    created = target._kubectl("-n", target.NAMESPACE, "create", "-f", "-", "-o", "json", data=pod)
    uid = created.get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise target.AdmissionError("binder creation uncertain; inspect retained intent")
    updated = RECEIPT.with_name(RECEIPT.name + ".new")
    fd = os.open(updated, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(
            json.dumps(
                {
                    "namespace_uid": record["namespace_uid"],
                    "pvc_uid": pvc["metadata"]["uid"],
                    "pod_uid": uid,
                }
            ).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(updated, RECEIPT)
    directory = os.open(RECEIPT.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {
        "binder_pod_uid": uid,
        "namespace_uid": record["namespace_uid"],
        "state": "credential_free_bind_only; do_not_transfer_owner",
    }


def pv_inspect(*, require_binder: bool = True) -> dict:
    """Read fixed claim/PV/node mapping; no reads inside any PVC directory."""
    record, pvc, _ = _owned()
    binder = _receipt(RECEIPT)
    if (
        binder.get("namespace_uid") != record["namespace_uid"]
        or binder.get("pvc_uid") != pvc["metadata"]["uid"]
    ):
        raise target.AdmissionError("binder intent identity drifted")
    if require_binder:
        pod = target._kubectl("-n", target.NAMESPACE, "get", "pod", BINDER, "-o", "json")
        if (
            pod.get("metadata", {}).get("uid") != binder.get("pod_uid")
            or pod.get("metadata", {}).get("labels", {}).get("tessera.io/local-operator-nonce")
            != record["nonce"]
        ):
            raise target.AdmissionError("binder Pod identity changed")
    if pvc.get("status", {}).get("phase") != "Bound":
        raise target.AdmissionError("PVC not Bound")
    volume_name = pvc.get("spec", {}).get("volumeName")
    if not isinstance(volume_name, str) or not re.fullmatch(r"pvc-[a-f0-9-]{36}", volume_name):
        raise target.AdmissionError("unexpected local-path volume")
    pv = target._kubectl("get", "pv", volume_name, "-o", "json")
    spec = pv.get("spec", {})
    claim = spec.get("claimRef", {})
    if (
        pv.get("metadata", {}).get("name") != volume_name
        or claim.get("uid") != pvc["metadata"]["uid"]
        or claim.get("name") != "tessera-control-state"
        or claim.get("namespace") != target.NAMESPACE
        or spec.get("storageClassName") != "standard"
        or spec.get("persistentVolumeReclaimPolicy") != "Delete"
    ):
        raise target.AdmissionError("PV binding drifted")
    host_path = (
        f"/var/local-path-provisioner/{volume_name}_{target.NAMESPACE}_tessera-control-state"
    )
    if spec.get("hostPath", {}).get("path") != host_path:
        raise target.AdmissionError("unexpected PV host path")
    affinity = spec.get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", [])
    if affinity != [
        {
            "matchExpressions": [
                {
                    "key": "kubernetes.io/hostname",
                    "operator": "In",
                    "values": ["desktop-control-plane"],
                }
            ]
        }
    ]:
        raise target.AdmissionError("PV node identity drifted")
    target._docker_identity()
    return {
        "namespace_uid": record["namespace_uid"],
        "pvc_uid": pvc["metadata"]["uid"],
        "pv_uid": pv["metadata"]["uid"],
        "node": "desktop-control-plane",
        "state": "bound_identity_only; no PVC content inspection",
    }


def _delete_binder(uid: str, resource_version: str) -> None:
    """Kubernetes DELETE with server-side UID/RV preconditions, via private mTLS."""
    body = {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "preconditions": {"uid": uid, "resourceVersion": resource_version},
    }
    if _api_request("DELETE", f"/api/v1/namespaces/{target.NAMESPACE}/pods/{BINDER}", body) not in {
        200,
        202,
    }:
        raise target.AdmissionError("UID-preconditioned binder deletion refused")


def binder_release() -> dict:
    """Remove exact binder only; a partial deletion is retained for reconciliation."""
    mapping = pv_inspect()
    record = _receipt(RECEIPT)
    if RELEASE.exists() or RELEASE.is_symlink():
        raise target.AdmissionError("prior binder release attempt retained")
    pod = target._kubectl("-n", target.NAMESPACE, "get", "pod", BINDER, "-o", "json")
    meta = pod.get("metadata", {})
    if meta.get("uid") != record.get("pod_uid") or not meta.get("resourceVersion"):
        raise target.AdmissionError("binder resource version or UID changed")
    fd = os.open(RELEASE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(
            json.dumps({"pod_uid": record["pod_uid"], "pv_uid": mapping["pv_uid"]}).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())
    _delete_binder(record["pod_uid"], meta["resourceVersion"])
    for _ in range(30):
        pods = target._kubectl("-n", target.NAMESPACE, "get", "pods", "-o", "json")
        if pods.get("items") == []:
            after = pv_inspect(require_binder=False)
            if after != mapping:
                raise target.AdmissionError("PV identity changed after binder release")
            return {**after, "state": "binder_absent_no_pods; no_credential_import"}
        if any(
            item.get("metadata", {}).get("uid") != record["pod_uid"]
            for item in pods.get("items", [])
        ):
            raise target.AdmissionError("replacement or foreign Pod detected")
        time.sleep(0.2)
    raise target.AdmissionError("binder removal unconfirmed; retained intent")


def register(server) -> None:
    from .activation import register as register_activation
    from .node_transfer import register as register_node_transfer
    from .target_recovery import register as register_target_recovery
    from .upgrade import register as register_upgrade

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def local_pvc_bind() -> dict:
        """Create one credential-free binder; no owner secret ever mounted."""
        return binder_create()

    @server.tool(annotations=read)
    def local_pv_inspect() -> dict:
        """Read only exact operator PVC/PV node/path identity metadata."""
        return pv_inspect()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def local_pvc_release() -> dict:
        """UID/RV-preconditioned DELETE of only fixed binder; require no Pods."""
        return binder_release()

    register_node_transfer(server)
    register_activation(server)
    register_target_recovery(server)
    register_upgrade(server)
