"""Opt-in disposable Docker Desktop base installation; never adopts or tears down namespaces.

This installs only the control and workload foundations. Credentials, TLS gateway,
network witnesses, worker binding and authenticated dispatch are separate stages.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
from pathlib import Path

import yaml
from check_local_kubernetes_composition import ROOT, inspect_cluster

CONTROL = "tessera-lab"
WORKLOAD = "tessera-workloads"
OWNER_LABEL = "tessera.io/disposable-bootstrap"
IMAGE = re.compile(r"docker\.io/library/tessera-local-lab@sha256:[a-f0-9]{64}\Z")


class BootstrapError(RuntimeError):
    pass


def documents(image: str) -> list[dict]:
    if not IMAGE.fullmatch(image):
        raise BootstrapError("expected immutable docker.io/library/tessera-local-lab digest")
    result = []
    for name in ("lab-control-plane.yaml", "workload-target.example.yaml"):
        source = ROOT / "deploy/kubernetes" / name
        for doc in yaml.safe_load_all(source.read_text()):
            if doc["kind"] == "Namespace":
                continue
            if name == "lab-control-plane.yaml":
                if doc["kind"] == "Deployment":
                    pod = doc["spec"]["template"]["spec"]
                    pod["initContainers"][0]["image"] = image
                    pod["containers"][0]["image"] = image
                    pod["initContainers"][0]["imagePullPolicy"] = "Never"
                    pod["containers"][0]["imagePullPolicy"] = "Never"
            result.append(doc)
    return result


def _kubectl(base: list[str], *args: str, data: str | None = None) -> dict:
    try:
        response = subprocess.run(
            [*base, *args], input=data, text=True, capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError(f"kubectl {args[0]} unavailable") from error
    if response.returncode:
        # kubectl stderr can contain server-supplied data; do not print it.
        raise BootstrapError(f"kubectl {args[0]} failed (exit {response.returncode})")
    if not response.stdout.strip():
        return {}
    try:
        return json.loads(response.stdout)
    except json.JSONDecodeError as error:
        raise BootstrapError(f"kubectl {args[0]} returned invalid JSON") from error


def verify_node_image(image: str) -> None:
    """Never pull from a registry or accept a mutable tag as a substitute."""
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "ls",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError("Docker Desktop node image inspection unavailable") from error
    if result.returncode or image not in result.stdout.splitlines():
        raise BootstrapError("exact immutable image missing in Docker Desktop node containerd")


def bootstrap(kubeconfig: Path, image: str) -> dict:
    resources = documents(image)
    observed = inspect_cluster(kubeconfig, "docker-desktop", CONTROL, WORKLOAD)
    if any(item["state"] != "absent_at_inspection_not_reserved" for item in observed.values()):
        raise BootstrapError("namespace occupied; refuse adoption")
    verify_node_image(image)
    base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", "docker-desktop"]
    nonce = secrets.token_hex(16)
    uids = {}
    # Creation is atomic per namespace; no apply/replace of another owner's namespace.
    # A failed run deliberately retains any created resources for UID-scoped inspection.
    try:
        for name in (CONTROL, WORKLOAD):
            ns = {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": name, "labels": {OWNER_LABEL: nonce}},
            }
            created = _kubectl(base, "create", "-f", "-", "-o", "json", data=json.dumps(ns))
            uid = created.get("metadata", {}).get("uid")
            if not isinstance(uid, str) or not uid:
                raise BootstrapError(f"namespace {name} creation returned no UID; stop")
            uids[name] = uid
        for name in (CONTROL, WORKLOAD):
            scoped = [doc for doc in resources if doc["metadata"]["namespace"] == name]
            for doc in scoped:
                current = _kubectl(
                    base, "get", "namespace", name, "-o", "json", "--request-timeout=5s"
                )
                meta = current.get("metadata", {})
                if (
                    meta.get("uid") != uids[name]
                    or meta.get("labels", {}).get(OWNER_LABEL) != nonce
                ):
                    raise BootstrapError(f"namespace {name} identity changed; stop")
                _kubectl(base, "-n", name, "create", "-f", "-", "-o", "json", data=json.dumps(doc))
    except BootstrapError as error:
        raise BootstrapError(
            f"{error}; partial ownership nonce={nonce}, observed creation UIDs={json.dumps(uids)}"
        ) from error
    return {
        "state": "foundations_applied_not_ready",
        "namespace_uids": uids,
        "owner_nonce": nonce,
        "image": image,
        "limitations": [
            "no TLS gateway or credentials",
            "no network enforcement witness",
            "no worker binding, Job or receipt",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--run", action="store_true", help="create two absent namespaces and base resources"
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required for this effects-bearing fixture")
    try:
        print(json.dumps(bootstrap(args.kubeconfig, args.image), sort_keys=True))
        return 0
    except (BootstrapError, ValueError, OSError, yaml.YAMLError) as error:
        print(json.dumps({"state": "blocked_or_partial_no_automatic_cleanup", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
