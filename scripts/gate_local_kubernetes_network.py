"""Opt-in, read-only live network/HTTPS gate for owned Docker Desktop composition.

Requires separately provisioned, running Python probe clients. No Pod, policy or
credential is created here. A passing report is short-lived and is not a worker
target binding or dispatch authorization.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from bootstrap_local_kubernetes_composition import CONTROL, OWNER_LABEL, WORKLOAD


class GateError(RuntimeError):
    pass


PROBE = """import json,socket,ssl,sys
host,port,mode,ca=sys.argv[1:]
try:
    if mode=="https":
        name="tessera-control-tls.tessera-lab.svc.cluster.local"
        with socket.create_connection((host,int(port)),timeout=3) as raw:
            with ssl.create_default_context(cafile=ca).wrap_socket(raw,server_hostname=name) as tls:
                tls.settimeout(3)
                request="GET /health/ready HTTP/1.1\\r\\nHost: "+name
                tls.sendall((request+"\\r\\nConnection: close\\r\\n\\r\\n").encode())
                status=tls.recv(256).split(b"\\r\\n",1)[0]
                outcome="https_ready" if status==b"HTTP/1.1 200 OK" else "unexpected_http"
                print(json.dumps({"outcome":outcome}))
    else:
        try:
            with socket.create_connection((host,int(port)),timeout=3):
                print(json.dumps({"outcome":"connected"}))
        except (ConnectionRefusedError,TimeoutError,OSError) as e:
            print(json.dumps({"outcome":"denied_connect","error_type":type(e).__name__}))
except (OSError,ValueError) as e:
    print(json.dumps({"outcome":"failed","error_type":type(e).__name__}))
"""


def _name(name: str) -> str:
    import re

    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", name)
        or len(name) > 63
    ):
        raise GateError("invalid explicit resource name")
    return name


def gate(
    kubeconfig: Path,
    namespace_uids: dict[str, str],
    nonce: str,
    allowed: str,
    denied: str,
    gateway: str,
    ca_path: str,
    *,
    causal: bool = False,
    run=subprocess.run,
) -> dict:
    if not kubeconfig.is_absolute() or not kubeconfig.is_file() or kubeconfig.is_symlink():
        raise GateError("absolute regular kubeconfig required")
    if not nonce or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise GateError("explicit bootstrap ownership nonce required")
    if set(namespace_uids) != {CONTROL, WORKLOAD} or not all(namespace_uids.values()):
        raise GateError("both exact namespace UIDs required")
    if ca_path != "/run/tessera-ca/ca.crt":
        raise GateError("exact mounted in-Pod CA path required")
    allowed, denied, gateway = (_name(n) for n in (allowed, denied, gateway))
    if len({allowed, denied, gateway}) != 3:
        raise GateError("probe Pods must be distinct")
    base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", "docker-desktop"]

    def call(*args: str, data: dict | None = None) -> str:
        try:
            result = run(
                [*base, *args],
                input=json.dumps(data) if data is not None else None,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GateError("bounded kubectl call unavailable") from error
        if result.returncode:
            raise GateError("bounded kubectl call failed")
        return result.stdout

    def get(kind: str, name: str, namespace: str | None = None) -> dict:
        args = (["-n", namespace] if namespace else []) + [
            "get",
            kind,
            name,
            "-o",
            "json",
            "--request-timeout=5s",
        ]
        try:
            value = json.loads(call(*args))
        except (ValueError, TypeError) as error:
            raise GateError("malformed named Kubernetes resource") from error
        if value.get("metadata", {}).get("name") != name or not value.get("metadata", {}).get(
            "uid"
        ):
            raise GateError("resource identity mismatch")
        if namespace is not None and value["metadata"].get("namespace") != namespace:
            raise GateError("resource namespace identity mismatch")
        return value

    def snapshot() -> tuple[str, ...]:
        for name in (CONTROL, WORKLOAD):
            meta = get("namespace", name)["metadata"]
            if (
                meta["uid"] != namespace_uids[name]
                or meta.get("labels", {}).get(OWNER_LABEL) != nonce
            ):
                raise GateError("namespace UID/owner changed")
        pods = [get("pod", name, CONTROL) for name in (allowed, denied, gateway)]
        for pod in pods:
            meta = pod["metadata"]
            if (
                meta.get("namespace") != CONTROL
                or pod.get("status", {}).get("phase") != "Running"
                or not pod.get("status", {}).get("podIP")
            ):
                raise GateError("probe Pod not running with identity/IP")
            try:
                ipaddress.ip_address(pod["status"]["podIP"])
            except ValueError as error:
                raise GateError("probe Pod IP malformed") from error
        for pod in pods[:2]:
            created = pod["metadata"].get("creationTimestamp")
            try:
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(created.replace("Z", "+00:00"))
                ).total_seconds()
            except (AttributeError, ValueError, TypeError) as error:
                raise GateError("probe creation timestamp missing") from error
            if age < 0 or age > 240:
                raise GateError("probe Pod no longer fresh")
        labels = [pod["metadata"].get("labels", {}) for pod in pods]
        import re

        match = re.fullmatch(r"tessera-tls-probe-allowed-([a-f0-9]{10})", allowed)
        if not match or denied != f"tessera-tls-probe-denied-{match[1]}":
            raise GateError("probe clients must be one fresh owned pair")
        if (
            labels[0].get("tessera.io/tls-control-client") != "true"
            or labels[0].get("tessera.io/probe-role") != "allowed"
            or labels[1].get("tessera.io/probe-role") != "denied"
            or any(item.get("tessera.io/probe-run") != match[1] for item in labels[:2])
            or labels[1].get("tessera.io/tls-control-client") == "true"
            or labels[1].get("tessera.io/control-hop") == "true"
            or labels[2].get("app.kubernetes.io/name") != "tessera-control-tls"
            or labels[2].get("tessera.io/control-hop") != "true"
        ):
            raise GateError("probe Pod labels do not distinguish allowed/denied/gateway")
        for pod in pods[:2]:
            spec = pod.get("spec", {})
            if (
                spec.get("automountServiceAccountToken") is not False
                or len(spec.get("containers", [])) != 1
                or spec["containers"][0].get("imagePullPolicy") != "Never"
                or spec["containers"][0].get("securityContext", {}).get("capabilities")
                != {"drop": ["ALL"]}
                or spec["containers"][0].get("volumeMounts")
                != [{"name": "ca", "mountPath": "/run/tessera-ca", "readOnly": True}]
                or len(spec.get("volumes", [])) != 1
                or spec["volumes"][0].get("name") != "ca"
                or spec["volumes"][0].get("secret", {}).get("items")
                != [{"key": "ca.crt", "path": "ca.crt"}]
            ):
                raise GateError("probe Pod spec lacks expected isolation")
        service = get("service", "tessera-control-tls", CONTROL)
        service_spec = service.get("spec", {})
        if service_spec.get("selector") != {
            "app.kubernetes.io/name": "tessera-control-tls"
        } or not any(
            item.get("port") == 443 and item.get("protocol", "TCP") == "TCP"
            for item in service_spec.get("ports", [])
        ):
            raise GateError("TLS gateway Service identity/selector changed")
        policy = get("networkpolicy", "default-deny", CONTROL)
        if policy.get("spec") != {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}:
            raise GateError("control default-deny identity/spec changed")
        gateway_selector = {"app.kubernetes.io/name": "tessera-control-tls"}
        source_selector = {"tessera.io/probe-run": match[1], "tessera.io/probe-role": "allowed"}
        expected = {
            f"tessera-tls-probe-ingress-{match[1]}": {
                "podSelector": {"matchLabels": gateway_selector},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": source_selector}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
            f"tessera-tls-probe-egress-{match[1]}": {
                "podSelector": {"matchLabels": source_selector},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"podSelector": {"matchLabels": gateway_selector}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
        }
        policy_uids = [policy["metadata"]["uid"]]
        for name, spec in expected.items():
            resource = get("networkpolicy", name, CONTROL)
            if resource.get("spec") != spec:
                raise GateError("probe policy identity/spec changed")
            policy_uids.append(resource["metadata"]["uid"])
        return (
            *(pod["metadata"]["uid"] for pod in pods),
            pods[2]["status"]["podIP"],
            *policy_uids,
            service["metadata"]["uid"],
        )

    before = snapshot()

    def probe(pod: str, mode: str) -> dict:
        host = before[3]
        output = call(
            "-n", CONTROL, "exec", pod, "--", "python", "-c", PROBE, host, "443", mode, ca_path
        )
        try:
            value = json.loads(output.strip())
            if (
                set(value) - {"outcome", "error_type"}
                or value.get("outcome")
                not in {"https_ready", "unexpected_http", "connected", "denied_connect", "failed"}
                or (
                    "error_type" in value
                    and value["error_type"]
                    not in {
                        "ConnectionRefusedError",
                        "TimeoutError",
                        "OSError",
                        "SSLError",
                        "SSLCertVerificationError",
                        "ValueError",
                        "FileNotFoundError",
                        "gaierror",
                        "ConnectionResetError",
                    }
                )
            ):
                raise ValueError("unexpected probe result")
            return value
        except (ValueError, KeyError, TypeError) as error:
            raise GateError("probe output is not an attributable result") from error

    def wait_for(pod: str, mode: str, expected: str) -> dict:
        deadline = time.monotonic() + 12
        while True:
            reading = probe(pod, mode)
            if reading.get("outcome") == expected:
                return reading
            if time.monotonic() >= deadline:
                raise GateError("causal transition did not propagate to the same endpoint")
            time.sleep(0.4)

    started = time.monotonic()
    # Bracket negative observation with two CA-verified, HTTPS-ready positives.
    readings = (probe(allowed, "https"), probe(denied, "tcp"), probe(allowed, "https"))
    outcomes = tuple(item["outcome"] for item in readings)
    after = snapshot()
    baseline_ok = (
        before != after
        or time.monotonic() - started > 45
        or outcomes != ("https_ready", "denied_connect", "https_ready")
        or any("error_type" in item for item in (readings[0], readings[2]))
    )
    if baseline_ok or (not causal and readings[1].get("error_type") != "ConnectionRefusedError"):
        # Only typed probe outcomes and exception class names are carried out
        # of Pods; never print TLS exception messages, certs or credentials.
        safe_readings = tuple(
            {key: item[key] for key in ("outcome", "error_type") if key in item}
            for item in readings
        )
        raise GateError(
            "fresh positive/negative HTTPS network witness failed or identities changed: "
            + json.dumps(safe_readings, sort_keys=True)
        )
    if causal:
        if readings[1].get("error_type") not in {"TimeoutError", "ConnectionRefusedError"}:
            raise GateError("baseline denied result not a typed network refusal")
        # Two temporary, probe-only policies must change this *same denied Pod*
        # and the *same gateway IP/port* into a CA-verified HTTPS positive. A
        # failed restoration or stale UID cannot be reported as enforcement.
        suffix = allowed.removeprefix("tessera-tls-probe-allowed-")
        source = {"tessera.io/probe-run": suffix, "tessera.io/probe-role": "denied"}
        destination = {"app.kubernetes.io/name": "tessera-control-tls"}
        specs = {
            "ingress": {
                "podSelector": {"matchLabels": destination},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": source}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
            "egress": {
                "podSelector": {"matchLabels": source},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"podSelector": {"matchLabels": destination}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
        }
        transitions = []
        for index in (1, 2):
            owned = []
            try:
                for role, spec in specs.items():
                    name = f"tessera-tls-causal-{role}-{suffix}-{index}"
                    manifest = {
                        "apiVersion": "networking.k8s.io/v1",
                        "kind": "NetworkPolicy",
                        "metadata": {
                            "name": name,
                            "namespace": CONTROL,
                            "labels": {OWNER_LABEL: nonce},
                        },
                        "spec": spec,
                    }
                    value = json.loads(
                        call("-n", CONTROL, "create", "-f", "-", "-o", "json", data=manifest)
                    )
                    if value.get("metadata", {}).get("name") != name or not value["metadata"].get(
                        "uid"
                    ):
                        raise GateError("causal create identity unknown; reconcile named resource")
                    owned.append((name, value["metadata"]["uid"]))
                if snapshot() != before:
                    raise GateError("causal positive identity changed")
                positive = wait_for(denied, "https", "https_ready")
                transitions.append(
                    {
                        "phase": "grant",
                        "at_unix": int(time.time()),
                        "outcome": positive["outcome"],
                        "policy_uids": dict(owned),
                    }
                )
                if positive != {"outcome": "https_ready"}:
                    raise GateError("causal grant failed to restore CA-verified HTTPS")
            finally:
                for name, uid in reversed(owned):
                    if get("networkpolicy", name, CONTROL)["metadata"]["uid"] != uid:
                        raise GateError(
                            "causal cleanup UID changed; manual reconciliation required"
                        )
                    options = {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {"uid": uid},
                    }
                    path = f"/apis/networking.k8s.io/v1/namespaces/{CONTROL}/networkpolicies/{name}"
                    call("delete", f"--raw={path}", "-f", "-", data=options)
            if snapshot() != before:
                raise GateError("causal restoration identity changed")
            negative = wait_for(denied, "tcp", "denied_connect")
            transitions.append({"phase": "revoke", "at_unix": int(time.time()), **negative})
            if negative.get("outcome") != "denied_connect" or negative.get("error_type") not in {
                "TimeoutError",
                "ConnectionRefusedError",
            }:
                raise GateError("causal policy revocation did not deny the same endpoint")
        if probe(allowed, "https") != {"outcome": "https_ready"} or snapshot() != before:
            raise GateError("causal final positive or restoration failed")
        if time.monotonic() - started > 120:
            raise GateError("causal witness stale")
    return {
        "state": "network_https_probe_passed_not_dispatch_authority",
        "namespace_uids": namespace_uids,
        "pod_uids": {allowed: before[0], denied: before[1], gateway: before[2]},
        "gateway_ip": before[3],
        "service_uid": before[7],
        "policy_uids": {
            "default-deny": before[4],
            "probe-ingress": before[5],
            "probe-egress": before[6],
        },
        "outcomes": outcomes,
        "denied_error_type": readings[1]["error_type"],
        "observed_at_unix": int(time.time()),
        "causal_transitions": transitions if causal else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--control-uid", required=True)
    parser.add_argument("--workload-uid", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--allowed-pod", required=True)
    parser.add_argument("--denied-pod", required=True)
    parser.add_argument("--gateway-pod", required=True)
    parser.add_argument("--ca-path", required=True)
    parser.add_argument(
        "--causal", action="store_true", help="temporary UID-scoped probe-only policy transitions"
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required")
    try:
        print(
            json.dumps(
                gate(
                    args.kubeconfig,
                    {CONTROL: args.control_uid, WORKLOAD: args.workload_uid},
                    args.nonce,
                    args.allowed_pod,
                    args.denied_pod,
                    args.gateway_pod,
                    args.ca_path,
                    causal=args.causal,
                ),
                sort_keys=True,
            )
        )
        return 0
    except GateError as error:
        print(json.dumps({"state": "blocked_no_dispatch", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
