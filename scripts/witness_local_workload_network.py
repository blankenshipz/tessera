"""Opt-in create-only workload-namespace allowed/denied policy witness.

Returns a typed network witness from live owned Pods, not a dispatch target.
The probe resources are retained; no cleanup or Job is issued by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from bootstrap_local_kubernetes_composition import IMAGE, OWNER_LABEL, WORKLOAD

from tessera_worker.kubernetes_contracts import (
    KubernetesConnectivityEvidence,
    KubernetesNetworkPolicyEvidence,
    canonical_json,
)


class WorkloadWitnessError(RuntimeError):
    pass


SERVER = """import socket,time
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('0.0.0.0',8080));s.listen(4);s.settimeout(1)
end=time.monotonic()+240
while time.monotonic()<end:
 try:
  c,_=s.accept();c.close()
 except socket.timeout: pass
"""
PROBE = """import json,socket,sys
try:
 with socket.create_connection((sys.argv[1],8080),timeout=3):
  print(json.dumps({'outcome':'connected'}))
except OSError as e:
 print(json.dumps({'outcome':'denied_connect','error_type':type(e).__name__}))
"""


def witness(
    kubeconfig: Path,
    workload_uid: str,
    nonce: str,
    image: str,
    *,
    causal: bool = False,
    run=subprocess.run,
) -> dict:
    if not kubeconfig.is_absolute() or not kubeconfig.is_file() or kubeconfig.is_symlink():
        raise WorkloadWitnessError("absolute regular kubeconfig required")
    if not IMAGE.fullmatch(image) or not workload_uid:
        raise WorkloadWitnessError("exact pinned image and workload UID required")
    if len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
        raise WorkloadWitnessError("explicit namespace ownership nonce required")
    base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", "docker-desktop"]

    def call(*args: str, data: dict | None = None, timeout: int = 8) -> str:
        try:
            result = run(
                [*base, *args],
                input=json.dumps(data) if data is not None else None,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorkloadWitnessError("bounded kubectl call unavailable") from error
        if result.returncode:
            raise WorkloadWitnessError(f"kubectl command failed (exit {result.returncode})")
        return result.stdout

    def get(kind: str, name: str, namespace: str | None = WORKLOAD) -> dict:
        try:
            obj = json.loads(
                call(
                    *(["-n", namespace] if namespace else []),
                    "get",
                    kind,
                    name,
                    "-o",
                    "json",
                    "--request-timeout=5s",
                )
            )
        except (ValueError, TypeError) as error:
            raise WorkloadWitnessError("Kubernetes readback malformed") from error
        if (
            not isinstance(obj, dict)
            or obj.get("metadata", {}).get("name") != name
            or not obj["metadata"].get("uid")
            or (namespace is not None and obj["metadata"].get("namespace") != namespace)
        ):
            raise WorkloadWitnessError("named resource identity changed")
        return obj

    cluster_uid = get("namespace", "kube-system", None)["metadata"]["uid"]

    def snapshot() -> tuple[dict, dict, dict]:
        ns = get("namespace", WORKLOAD, None)
        if (
            ns["metadata"]["uid"] != workload_uid
            or ns["metadata"].get("labels", {}).get(OWNER_LABEL) != nonce
            or get("namespace", "kube-system", None)["metadata"]["uid"] != cluster_uid
        ):
            raise WorkloadWitnessError("cluster/owned workload namespace changed")
        policy = get("networkpolicy", "default-deny")
        if policy.get("spec") != {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}:
            raise WorkloadWitnessError("workload default-deny declaration changed")
        pvc = get("pvc", "outputs")
        if pvc.get("status", {}).get("phase") != "Bound":
            raise WorkloadWitnessError("output PVC not bound")
        return ns, policy, pvc

    _ns, policy, pvc = snapshot()
    initial_policy_uid = policy["metadata"]["uid"]
    initial_pvc_uid = pvc["metadata"]["uid"]
    initial_pvc_spec = hashlib.sha256(canonical_json(pvc["spec"])).hexdigest()
    suffix = secrets.token_hex(5)
    names = {
        role: f"tessera-workload-probe-{role}-{suffix}" for role in ("server", "allowed", "denied")
    }
    server_labels = {"tessera.io/workload-probe": suffix, "tessera.io/probe-role": "server"}
    allowed_labels = {"tessera.io/workload-probe": suffix, "tessera.io/probe-role": "allowed"}
    policies = (
        {
            "podSelector": {"matchLabels": server_labels},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": allowed_labels}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
        },
        {
            "podSelector": {"matchLabels": allowed_labels},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"podSelector": {"matchLabels": server_labels}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
        },
    )
    created: list[tuple[str, str, str]] = []

    def create(manifest: dict) -> None:
        snapshot()
        try:
            result = json.loads(
                call("-n", WORKLOAD, "create", "-f", "-", "-o", "json", data=manifest)
            )
        except (ValueError, TypeError) as error:
            raise WorkloadWitnessError(f"create response malformed; prior={created}") from error
        meta = result.get("metadata", {})
        if meta.get("name") != manifest["metadata"]["name"] or not meta.get("uid"):
            raise WorkloadWitnessError(f"create identity missing; prior={created}")
        created.append((manifest["kind"], meta["name"], meta["uid"]))

    for role, spec in zip(("ingress", "egress"), policies, strict=True):
        create(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {
                    "name": f"tessera-workload-probe-{role}-{suffix}",
                    "namespace": WORKLOAD,
                },
                "spec": spec,
            }
        )

    for role, name in names.items():
        labels = {"tessera.io/workload-probe": suffix, "tessera.io/probe-role": role}
        create(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": WORKLOAD, "labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "runAsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "imagePullPolicy": "Never",
                            "command": [
                                "python",
                                "-c",
                                SERVER if role == "server" else "import time; time.sleep(240)",
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            }
        )

    def readback() -> tuple[str, ...]:
        _, current_policy, current_pvc = snapshot()
        if (
            current_policy["metadata"]["uid"] != initial_policy_uid
            or current_pvc["metadata"]["uid"] != initial_pvc_uid
            or hashlib.sha256(canonical_json(current_pvc["spec"])).hexdigest() != initial_pvc_spec
        ):
            raise WorkloadWitnessError("workload policy/PVC identity changed")
        for role, spec in zip(("ingress", "egress"), policies, strict=True):
            name = f"tessera-workload-probe-{role}-{suffix}"
            resource = get("networkpolicy", name)
            if (
                resource["metadata"]["uid"] != next(uid for kind, n, uid in created if n == name)
                or resource.get("spec") != spec
            ):
                raise WorkloadWitnessError("probe policy UID/spec changed")
        result = []
        for role, name in names.items():
            pod = get("pod", name)
            meta = pod["metadata"]
            pod_spec = pod.get("spec", {})
            containers = pod_spec.get("containers", [])
            if (
                meta["uid"] != next(uid for kind, n, uid in created if n == name)
                or meta.get("labels")
                != {"tessera.io/workload-probe": suffix, "tessera.io/probe-role": role}
                or pod.get("status", {}).get("phase") != "Running"
                or pod_spec.get("automountServiceAccountToken") is not False
                or pod_spec.get("securityContext", {}).get("runAsNonRoot") is not True
                or pod_spec.get("securityContext", {}).get("runAsUser") != 65532
                or pod_spec.get("securityContext", {}).get("seccompProfile")
                != {"type": "RuntimeDefault"}
                or len(containers) != 1
                or containers[0].get("image") != image
                or containers[0].get("imagePullPolicy") != "Never"
                or containers[0].get("securityContext", {}).get("capabilities") != {"drop": ["ALL"]}
                or containers[0].get("securityContext", {}).get("allowPrivilegeEscalation")
                is not False
                or containers[0].get("securityContext", {}).get("readOnlyRootFilesystem")
                is not True
            ):
                raise WorkloadWitnessError("owned probe Pod not running with expected spec")
            result.append(meta["uid"])
            if role == "server":
                try:
                    ipaddress.ip_address(pod["status"]["podIP"])
                except (KeyError, ValueError, TypeError) as error:
                    raise WorkloadWitnessError("server Pod IP missing") from error
                result.append(pod["status"]["podIP"])
        return tuple(result)

    # Wait a bounded period for all clients to be running (and the server to listen).
    for name in names.values():
        call(
            "-n",
            WORKLOAD,
            "wait",
            "--for=jsonpath={.status.phase}=Running",
            f"pod/{name}",
            "--timeout=25s",
            timeout=30,
        )
    before = readback()
    started = time.monotonic()

    def probe(role: str) -> dict:
        try:
            reading = json.loads(
                call(
                    "-n",
                    WORKLOAD,
                    "exec",
                    names[role],
                    "--",
                    "python",
                    "-c",
                    PROBE,
                    before[1],
                    timeout=8,
                )
            )
        except (TypeError, ValueError) as error:
            raise WorkloadWitnessError("probe response malformed") from error
        if not isinstance(reading, dict) or set(reading) - {"outcome", "error_type"}:
            raise WorkloadWitnessError("probe response ambiguous")
        return reading

    # Pod Running may precede both the Python listener and policy propagation.
    # Establish a healthy positive before sampling denial; never retry a
    # connected denied client away as a transient failure.
    positive_deadline = time.monotonic() + 12
    while True:
        initial = probe("allowed")
        if initial == {"outcome": "connected"}:
            break
        if time.monotonic() >= positive_deadline:
            raise WorkloadWitnessError("allowed baseline never reached the named server")
        time.sleep(0.4)
    readings = (initial, probe("denied"), probe("allowed"))
    after = readback()
    if (
        before != after
        or time.monotonic() - started > 45
        or readings[0] != {"outcome": "connected"}
        or readings[2] != {"outcome": "connected"}
        or readings[1].get("outcome") != "denied_connect"
        or (not causal and readings[1].get("error_type") != "ConnectionRefusedError")
    ):
        raise WorkloadWitnessError(
            "fresh workload allow/deny witness failed or identity changed: "
            + json.dumps(readings, sort_keys=True)
        )
    transitions = []
    if causal:
        if readings[1].get("error_type") not in ("TimeoutError", "ConnectionRefusedError"):
            raise WorkloadWitnessError("baseline denied outcome is not a typed network refusal")
        denied_labels = {"tessera.io/workload-probe": suffix, "tessera.io/probe-role": "denied"}
        for index in (1, 2):
            grants = []
            try:
                for role, spec in zip(("ingress", "egress"), policies, strict=True):
                    changed = json.loads(json.dumps(spec))
                    if role == "ingress":
                        changed["ingress"][0]["from"][0]["podSelector"]["matchLabels"] = (
                            denied_labels
                        )
                    else:
                        changed["podSelector"]["matchLabels"] = denied_labels
                    name = f"tessera-workload-causal-{role}-{suffix}-{index}"
                    create(
                        {
                            "apiVersion": "networking.k8s.io/v1",
                            "kind": "NetworkPolicy",
                            "metadata": {"name": name, "namespace": WORKLOAD},
                            "spec": changed,
                        }
                    )
                    grants.append((name, created[-1][2]))
                if readback() != before:
                    raise WorkloadWitnessError("causal grant changed witness identities")
                deadline = time.monotonic() + 12
                while True:
                    positive = probe("denied")
                    if positive == {"outcome": "connected"}:
                        break
                    if time.monotonic() > deadline:
                        raise WorkloadWitnessError("causal workload grant not connected")
                    time.sleep(0.4)
                transitions.append(
                    {
                        "phase": "grant",
                        "at_unix": int(time.time()),
                        "outcome": "connected",
                        "policy_uids": dict(grants),
                    }
                )
            finally:
                for name, uid in reversed(grants):
                    if get("networkpolicy", name)["metadata"]["uid"] != uid:
                        raise WorkloadWitnessError("causal workload cleanup UID changed")
                    options = {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {"uid": uid},
                    }
                    path = (
                        f"/apis/networking.k8s.io/v1/namespaces/{WORKLOAD}/networkpolicies/{name}"
                    )
                    call("delete", f"--raw={path}", "-f", "-", data=options)
                    created.remove(("NetworkPolicy", name, uid))
            if readback() != before:
                raise WorkloadWitnessError("causal workload restoration identity changed")
            deadline = time.monotonic() + 12
            while True:
                negative = probe("denied")
                if negative.get("outcome") == "denied_connect" and negative.get("error_type") in (
                    "TimeoutError",
                    "ConnectionRefusedError",
                ):
                    break
                if time.monotonic() > deadline:
                    raise WorkloadWitnessError("causal workload revoke not denied")
                time.sleep(0.4)
            transitions.append({"phase": "revoke", "at_unix": int(time.time()), **negative})
        if probe("allowed") != {"outcome": "connected"} or readback() != before:
            raise WorkloadWitnessError("causal workload final positive/restoration failed")
        if time.monotonic() - started > 120:
            raise WorkloadWitnessError("causal workload witness stale")
    now = datetime.now(UTC)
    evidence = KubernetesNetworkPolicyEvidence(
        "network-policy-enforced-v1",
        "default-deny",
        policy["metadata"]["uid"],
        hashlib.sha256(canonical_json(policy["spec"])).hexdigest(),
        cluster_uid,
        workload_uid,
        now,
        KubernetesConnectivityEvidence(before[2], before[0], "allowed", 3, now),
        KubernetesConnectivityEvidence(before[3], before[0], "denied", 3, now),
    )

    # Record only observed identities and fresh network probe results, no target
    # file/dispatch authority; admission and storage evidence remain independent.
    def item(probe: KubernetesConnectivityEvidence) -> dict:
        return {
            "source_pod_uid": probe.source_pod_uid,
            "destination_pod_uid": probe.destination_pod_uid,
            "outcome": probe.outcome,
            "timeout_seconds": probe.timeout_seconds,
            "observed_at": probe.observed_at.isoformat(),
        }

    return {
        "state": "workload_network_witness_not_dispatch_authority",
        "network_evidence": {
            "capability": evidence.capability,
            "policy_name": evidence.policy_name,
            "policy_uid": evidence.policy_uid,
            "policy_spec_sha256": evidence.policy_spec_sha256,
            "cluster_uid": evidence.cluster_uid,
            "namespace_uid": evidence.namespace_uid,
            "observed_at": evidence.observed_at.isoformat(),
            "allowed_connectivity": item(evidence.allowed_connectivity),
            "denied_connectivity": item(evidence.denied_connectivity),
        },
        "pod_uids": dict(zip(names, (before[0], before[2], before[3]), strict=True)),
        "pvc_uid": initial_pvc_uid,
        "pvc_spec_sha256": initial_pvc_spec,
        "retained": created,
        "readings": readings,
        "causal_transitions": transitions if causal else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--workload-uid", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--causal", action="store_true")
    args = parser.parse_args()
    if not args.run:
        parser.error("--run required")
    try:
        print(
            json.dumps(
                witness(
                    args.kubeconfig, args.workload_uid, args.nonce, args.image, causal=args.causal
                ),
                sort_keys=True,
            )
        )
        return 0
    except WorkloadWitnessError as error:
        print(json.dumps({"state": "blocked_or_partial_no_dispatch", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
