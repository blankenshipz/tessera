"""Stopped direct-Sandbox PVC bytes through existing collector and private transfer.

No Job identity is manufactured. Collector Pods are separate read-only readers;
their immutable creation UID/results and the retained byte delivery use existing
collector/delivery journals. Collection never releases scientific/public output.
"""

import base64
import hashlib
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

from tessera_control.agent_sandbox_models import GATE, SandboxPlan
from tessera_control.store import canonical_digest

from .agent_sandbox import SandboxUnsafe, collection_handoff
from .kubernetes_output_collector import KubernetesOutputCollector
from .kubernetes_output_delivery import KubernetesOutputDelivery
from .outputs import MAX_OUTPUT_BYTES, CollectedOutput, validate_output_paths


@dataclass(frozen=True)
class SandboxOutputPin:
    pvc_name: str
    pvc_uid: str
    pvc_spec_sha256: str
    mount_path: str = "/outputs"
    operation_scoped: bool = True


class SandboxCollectorClient:
    def __init__(self, backend, package, channel, handoff):
        self.backend, self.package, self.channel, self.handoff = backend, package, channel, handoff
        self.cluster = SandboxPlan.model_validate(package["sandbox"]).target

    def path(self, kind, name=None):
        return self.backend.path(self.cluster, kind, name)

    def request(self, method, path, body=None, **kwargs):
        self.verify(None)
        return self.backend.client.request(method, path, body, **kwargs)

    def get(self, kind, name):
        return self.request("GET", self.path(kind, name))

    def verify(self, output):
        operation = self.package["operation_id"]
        fresh = self.channel.post(
            f"/worker/agent-sandboxes/{operation}/collection-handoff",
            {"expected_epoch": self.package["epoch"]},
            "sandbox-collector-" + uuid.uuid4().hex,
        )
        for field in ("operation_id", "sandbox_uid", "pvc", "handoff"):
            if fresh.get(field) != self.handoff.get(field):
                raise SandboxUnsafe("stopped Sandbox collection provenance changed")
        if not fresh.get("quiescent") or fresh.get("desired_state") != "cancel_requested":
            raise SandboxUnsafe("stopped Sandbox proof unavailable")
        self.backend.preflight(self.cluster, {"tessera.dev/output-operation": operation})
        pvc = self.backend.client.request("GET", self.path("pvc", fresh["pvc"]["name"]))
        if (
            not pvc
            or pvc.get("metadata", {}).get("uid") != fresh["pvc"]["uid"]
            or pvc.get("metadata", {}).get("deletionTimestamp")
            or canonical_digest(pvc.get("spec")) != fresh["pvc"]["spec_sha256"]
        ):
            raise SandboxUnsafe("collection PVC identity/spec changed")


class SandboxPVCCollector(KubernetesOutputCollector):
    def __init__(self, client, output, image, journal):
        super().__init__(client, output, image, journal)
        self.validator = client.backend
        with sqlite3.connect(journal) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sandbox_collector_nodes "
                "(pod_uid TEXT PRIMARY KEY, node_name TEXT NOT NULL, node_uid TEXT NOT NULL)"
            )

    def identity(self, package):
        return {
            "operation": package.operation_id,
            "sandbox_uid": package.binding.attempt_id,
            "lease": package.binding.lease_id,
            "epoch": package.binding.epoch,
            "package": package.package_digest,
            "pvc": self.output.pvc_uid,
            "target": package.target_digest,
            "backend": "agent-sandbox",
        }

    def manifest(self, mode, package, *, image=None):
        if mode != "collect":
            raise SandboxUnsafe("Sandbox collection never prepares or writes producer storage")
        value = super().manifest(mode, package, image=image)
        value["spec"]["containers"][0]["command"][3] = "sandbox-collect"
        value["metadata"]["name"] = "tessera-sb-output-" + package.operation_id
        value["spec"].update(
            runtimeClassName=self.client.cluster.runtime_class,
            enableServiceLinks=False,
            hostNetwork=False,
            hostPID=False,
            hostIPC=False,
            priority=0,
            preemptionPolicy="PreemptLowerPriority",
        )
        return value

    def verify_pod(self, pod, manifest, uid):
        super().verify_pod(pod, manifest, uid)
        # The generic collector verifies required fields; this lane additionally
        # rejects unknown admission mutation, sidecars, credentials and privileges.
        from copy import deepcopy

        spec = deepcopy(manifest["spec"])
        spec["schedulingGates"] = [{"name": GATE}]
        expected = {"spec": {"podTemplate": {"metadata": manifest["metadata"], "spec": spec}}}
        self.validator.validate_pod(expected, pod, gated=False)
        node_name = pod["spec"].get("nodeName")
        if node_name:
            with sqlite3.connect(self.journal) as db:
                prior = db.execute(
                    "SELECT node_name,node_uid FROM sandbox_collector_nodes WHERE pod_uid=?", (uid,)
                ).fetchone()
                if prior and prior[0] != node_name:
                    raise SandboxUnsafe("collector Node assignment changed")
                node_uid = self.validator.node_identity(node_name, prior[1] if prior else None)
                db.execute(
                    "INSERT OR IGNORE INTO sandbox_collector_nodes VALUES (?,?,?)",
                    (uid, node_name, node_uid),
                )
        if pod.get("status", {}).get("phase") == "Succeeded":
            statuses = pod["status"].get("containerStatuses", [])
            if (
                len(statuses) != 1
                or statuses[0].get("name") != "output"
                or statuses[0].get("state", {}).get("terminated", {}).get("exitCode") != 0
                or not statuses[0].get("state", {}).get("terminated", {}).get("finishedAt")
                or pod["status"].get("initContainerStatuses")
                or pod["status"].get("ephemeralContainerStatuses")
            ):
                raise SandboxUnsafe("collector terminal container evidence unavailable")
            if not node_name:
                raise SandboxUnsafe("collector terminal Node identity unavailable")

    def collect_outputs(self, package):
        self.client.verify(self.output)
        result = self.execute("collect", package)
        outputs, total = [], 0
        for item in result.get("outputs", []):
            content = base64.b64decode(item["content"], validate=True)
            total += len(content)
            if total > MAX_OUTPUT_BYTES or hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise SandboxUnsafe("collected output checksum/size mismatch")
            outputs.append(CollectedOutput(item["path"], content, item["sha256"]))
        paths = validate_output_paths([item.relative_path for item in outputs])
        if set(paths) != set(package.package["output_paths"]):
            raise SandboxUnsafe("collected output declaration mismatch")
        return tuple(outputs)


def deliver_stopped_sandbox(package, backend, channel, *, image, custody_root):
    """Explicit operator invocation; no automatic cluster effects during serve."""
    root = Path(custody_root)
    if (
        not root.is_dir()
        or root.is_symlink()
        or root.stat().st_mode & 0o077
        or root.stat().st_uid != os.geteuid()
    ):
        raise ValueError("private existing collector custody directory required")
    operation = package["operation_id"]
    handoff = channel.post(
        f"/worker/agent-sandboxes/{operation}/collection-handoff",
        {"expected_epoch": package["epoch"]},
        "sandbox-delivery-" + uuid.uuid4().hex,
    )
    if (
        not handoff.get("quiescent")
        or handoff.get("desired_state") != "cancel_requested"
        or canonical_digest(handoff.get("handoff"))
        != canonical_digest(asdict(collection_handoff(package, handoff.get("sandbox_uid"))))
    ):
        raise SandboxUnsafe("fresh typed stopped Sandbox handoff required")
    output = SandboxOutputPin(
        handoff["pvc"]["name"], handoff["pvc"]["uid"], handoff["pvc"]["spec_sha256"]
    )
    neutral = SimpleNamespace(
        operation_id=operation,
        package=package,
        package_digest=canonical_digest(package),
        binding=SimpleNamespace(
            attempt_id=handoff["sandbox_uid"], lease_id=package["lease_id"], epoch=package["epoch"]
        ),
        output=output,
        target_digest=collection_handoff(
            package, handoff["sandbox_uid"]
        ).handle.target.identity_digest,
    )
    client = SandboxCollectorClient(backend, package, channel, handoff)
    collector = SandboxPVCCollector(client, output, image, root / "sandbox-collectors.sqlite3")
    # Retain terminal collector Pods; no automatic deletion under ambiguous Node
    # termination evidence. The journal and immutable bytes survive upload failure.
    return KubernetesOutputDelivery(root).deliver(neutral, collector, channel)
