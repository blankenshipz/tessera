"""Opt-in create-only, UID-scoped TLS probe clients for the local network gate.

The gateway and its distinct, owned public CA Secret must already exist. This
creates no dispatch authority, reads only public certificate keys, and never
replaces/deletes a Kubernetes object.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

from bootstrap_local_kubernetes_composition import CONTROL, IMAGE, OWNER_LABEL, WORKLOAD
from gate_local_kubernetes_network import GateError, _name


class ProbeSetupError(RuntimeError):
    pass


GATEWAY_HOST = "tessera-control-tls.tessera-lab.svc.cluster.local"


def verify_public_chain(ca: bytes, leaf: bytes, *, run=subprocess.run) -> None:
    """Validate public CA role and the server chain/name without accessing keys."""
    if not ca or not leaf or ca == leaf:
        raise ProbeSetupError("distinct public CA and gateway leaf certificates required")
    try:
        with tempfile.TemporaryDirectory(prefix="tessera-probe-certs-") as directory:
            ca_file, leaf_file = Path(directory) / "ca.pem", Path(directory) / "leaf.pem"
            ca_file.write_bytes(ca)
            leaf_file.write_bytes(leaf)

            def openssl(*args: str) -> str:
                result = run(
                    ["openssl", *args], capture_output=True, text=True, timeout=8, check=False
                )
                if result.returncode:
                    raise ProbeSetupError("public certificate validation failed")
                return result.stdout

            ca_info = openssl("x509", "-in", str(ca_file), "-noout", "-text")
            if "CA:TRUE" not in ca_info or "Certificate Sign" not in ca_info:
                raise ProbeSetupError("public trust certificate is not an issuing CA")
            leaf_san = openssl("x509", "-in", str(leaf_file), "-noout", "-ext", "subjectAltName")
            if GATEWAY_HOST not in re.findall(r"DNS:([^,\s]+)", leaf_san):
                # OpenSSL hostname verification may fall back to CN if SAN is absent.
                raise ProbeSetupError("gateway DNS subject alternative name missing")
            openssl(
                "verify",
                "-x509_strict",
                "-purpose",
                "sslserver",
                "-verify_hostname",
                GATEWAY_HOST,
                "-CAfile",
                str(ca_file),
                str(leaf_file),
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProbeSetupError("bounded public certificate validation unavailable") from error


def prepare(
    kubeconfig: Path,
    namespace_uids: dict[str, str],
    nonce: str,
    image: str,
    ca_secret: str,
    gateway_pod: str,
    gateway_tls_secret: str,
    *,
    run=subprocess.run,
) -> dict:
    if not kubeconfig.is_absolute() or not kubeconfig.is_file() or kubeconfig.is_symlink():
        raise ProbeSetupError("absolute regular kubeconfig required")
    if not IMAGE.fullmatch(image):
        raise ProbeSetupError("exact pinned local lab image required")
    if len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
        raise ProbeSetupError("explicit ownership nonce required")
    if set(namespace_uids) != {CONTROL, WORKLOAD} or not all(namespace_uids.values()):
        raise ProbeSetupError("both namespace UIDs required")
    try:
        _name(ca_secret)
        _name(gateway_pod)
        _name(gateway_tls_secret)
    except GateError as error:
        raise ProbeSetupError("invalid explicit name") from error
    if ca_secret == gateway_tls_secret:
        raise ProbeSetupError("CA Secret must be distinct from gateway TLS Secret")

    base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", "docker-desktop"]

    def call(*args: str, data: dict | None = None) -> dict:
        try:
            result = run(
                [*base, *args],
                input=json.dumps(data) if data is not None else None,
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProbeSetupError("bounded kubectl unavailable") from error
        if result.returncode:
            raise ProbeSetupError(f"kubectl {args[0]} failed (exit {result.returncode})")
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise ProbeSetupError("malformed Kubernetes JSON") from error

    def get(kind: str, name: str, namespace: str | None = CONTROL) -> dict:
        obj = call(
            *(["-n", namespace] if namespace else []),
            "get",
            kind,
            name,
            "-o",
            "json",
            "--request-timeout=5s",
        )
        meta = obj.get("metadata", {})
        if meta.get("name") != name or not meta.get("uid"):
            raise ProbeSetupError("named object identity mismatch")
        return obj

    def check_namespaces() -> None:
        for name, uid in namespace_uids.items():
            meta = get("namespace", name, None)["metadata"]
            if meta["uid"] != uid or meta.get("labels", {}).get(OWNER_LABEL) != nonce:
                raise ProbeSetupError("owned namespace changed")

    check_namespaces()
    gateway = get("pod", gateway_pod)
    labels = gateway["metadata"].get("labels", {})
    if (
        gateway["metadata"].get("namespace") != CONTROL
        or gateway.get("status", {}).get("phase") != "Running"
        or not gateway.get("status", {}).get("podIP")
        or labels.get("app.kubernetes.io/name") != "tessera-control-tls"
        or labels.get("tessera.io/control-hop") != "true"
    ):
        raise ProbeSetupError("gateway Pod identity/labels/readiness missing")
    if not any(
        volume.get("secret", {}).get("secretName") == gateway_tls_secret
        for volume in gateway.get("spec", {}).get("volumes", [])
    ):
        raise ProbeSetupError("gateway Pod does not mount the named TLS Secret")
    service = get("service", "tessera-control-tls")
    spec = service.get("spec", {})
    if (
        service["metadata"].get("namespace") != CONTROL
        or spec.get("selector") != {"app.kubernetes.io/name": "tessera-control-tls"}
        or not any(
            p.get("port") == 443 and p.get("protocol", "TCP") == "TCP"
            for p in spec.get("ports", [])
        )
    ):
        raise ProbeSetupError("TLS gateway Service selector/port mismatch")

    # Select only the public key in each named Secret. Never request a full
    # Secret JSON (which may include tls.key), nor expose kubectl output/errors.
    def public_certificate(name: str, key: str, *, owned: bool) -> bytes:
        template = (
            '{{printf "{\\"name\\":%q,\\"uid\\":%q,\\"namespace\\":%q,'
            '\\"owner\\":%q,\\"certificate\\":%q}" '
            ".metadata.name .metadata.uid .metadata.namespace "
            f'(index .metadata.labels "{OWNER_LABEL}") (index .data "{key}")}}}}'
        )
        obj = call(
            "-n",
            CONTROL,
            "get",
            "secret",
            name,
            "-o",
            f"go-template={template}",
            "--request-timeout=5s",
        )
        if (
            not isinstance(obj, dict)
            or obj.get("name") != name
            or not obj.get("uid")
            or obj.get("namespace") != CONTROL
            or (owned and obj.get("owner") != nonce)
        ):
            raise ProbeSetupError("public certificate Secret identity/ownership mismatch")
        encoded = obj.get("certificate")
        if not isinstance(encoded, str):
            raise ProbeSetupError("public certificate key missing")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ProbeSetupError("public certificate key malformed") from error

    ca = public_certificate(ca_secret, "ca.crt", owned=True)
    leaf = public_certificate(gateway_tls_secret, "tls.crt", owned=False)
    verify_public_chain(ca, leaf)
    default = get("networkpolicy", "default-deny")
    if default.get("spec") != {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}:
        raise ProbeSetupError("control default-deny changed")

    suffix = secrets.token_hex(5)
    names = {role: f"tessera-tls-probe-{role}-{suffix}" for role in ("allowed", "denied")}
    selector = {"tessera.io/probe-run": suffix, "tessera.io/probe-role": "allowed"}
    gateway_selector = {"app.kubernetes.io/name": "tessera-control-tls"}
    policies = [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": f"tessera-tls-probe-ingress-{suffix}", "namespace": CONTROL},
            "spec": {
                "podSelector": {"matchLabels": gateway_selector},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": selector}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": f"tessera-tls-probe-egress-{suffix}", "namespace": CONTROL},
            "spec": {
                "podSelector": {"matchLabels": selector},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"podSelector": {"matchLabels": gateway_selector}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
        },
    ]
    pods = []
    for role, name in names.items():
        pod_labels = {"tessera.io/probe-run": suffix, "tessera.io/probe-role": role}
        if role == "allowed":
            pod_labels["tessera.io/tls-control-client"] = "true"
        pods.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": CONTROL, "labels": pod_labels},
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
                            "command": ["python", "-c", "import time; time.sleep(240)"],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "ca", "mountPath": "/run/tessera-ca", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "ca",
                            "secret": {
                                "secretName": ca_secret,
                                "items": [{"key": "ca.crt", "path": "ca.crt"}],
                            },
                        }
                    ],
                },
            }
        )
    created = []
    # A partial create is retained for exact-UID reconciliation; never apply or delete.
    for manifest in [*policies, *pods]:
        check_namespaces()
        obj = call("-n", CONTROL, "create", "-f", "-", "-o", "json", data=manifest)
        meta = obj.get("metadata", {})
        if meta.get("name") != manifest["metadata"]["name"] or not meta.get("uid"):
            raise ProbeSetupError(f"creation identity missing; prior created={created}")
        created.append({"kind": manifest["kind"], "name": meta["name"], "uid": meta["uid"]})
    check_namespaces()
    return {
        "state": "probe_clients_created_not_network_proven",
        "probe_pods": names,
        "gateway_pod": gateway_pod,
        "ca_path": "/run/tessera-ca/ca.crt",
        "created": created,
        "namespace_uids": namespace_uids,
        "owner_nonce": nonce,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--control-uid", required=True)
    parser.add_argument("--workload-uid", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--ca-secret", required=True)
    parser.add_argument("--gateway-pod", required=True)
    parser.add_argument("--gateway-tls-secret", required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required")
    try:
        print(
            json.dumps(
                prepare(
                    args.kubeconfig,
                    {CONTROL: args.control_uid, WORKLOAD: args.workload_uid},
                    args.nonce,
                    args.image,
                    args.ca_secret,
                    args.gateway_pod,
                    args.gateway_tls_secret,
                ),
                sort_keys=True,
            )
        )
        return 0
    except ProbeSetupError as error:
        print(json.dumps({"state": "blocked_or_partial_no_dispatch", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
