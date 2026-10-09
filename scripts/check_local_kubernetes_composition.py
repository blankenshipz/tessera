"""Read-only gate for an opt-in Docker Desktop control/worker installation.

This does not create authority, credentials, images or cluster resources.  A
declaration of NetworkPolicy is not connectivity enforcement evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = (
    "lab-control-plane.yaml",
    "provider-disabled-coordinator.example.yaml",
    "remote-dispatch-worker.example.yaml",
    "workload-target.example.yaml",
)
PLACEHOLDERS = (
    "registry.example.invalid",
    "replace-me",
    "replace-with-existing-project-id",
    "192.0.2.1/32",
)


def inspect_manifests(directory: Path = ROOT / "deploy/kubernetes") -> list[str]:
    """Identify inputs that must be resolved before any apply, never render guesses."""
    blockers: list[str] = []
    for name in EXAMPLES:
        path = directory / name
        try:
            text = path.read_text()
            documents = list(yaml.safe_load_all(text))
        except (OSError, yaml.YAMLError) as error:
            blockers.append(f"{name}: unreadable or invalid YAML ({type(error).__name__})")
            continue
        if not documents or any(not isinstance(document, dict) for document in documents):
            blockers.append(f"{name}: invalid Kubernetes documents")
            continue
        # Inspect executable fields only: comments intentionally describe rejected values.
        rendered = json.dumps(documents, sort_keys=True)
        for placeholder in PLACEHOLDERS:
            if placeholder in rendered:
                blockers.append(f"{name}: unresolved placeholder {placeholder}")
    # Neither example includes a TLS endpoint or a scoped credential issuer.
    blockers.extend(
        (
            "TLS gateway, private CA and leaf identity are not provisioned by the examples",
            "project-scoped agent/worker principals and tokens are not provisioned by the examples",
            "fresh allowed and denied connectivity probes are not provisioned",
            "workload Pod admission and storage isolation are not verified by the examples",
        )
    )
    return blockers


def inspect_cluster(
    kubeconfig: Path, context: str, control: str, workload: str
) -> dict[str, object]:
    """Read only named namespace metadata; never adopt or delete existing objects."""
    if not kubeconfig.is_absolute() or not kubeconfig.is_file() or kubeconfig.is_symlink():
        raise ValueError("an absolute regular non-symlink kubeconfig is required")
    if context != "docker-desktop":
        raise ValueError("only the explicit docker-desktop context is supported")
    try:
        config = yaml.safe_load(kubeconfig.read_text())
        matches = [entry for entry in config["contexts"] if entry["name"] == context]
        if len(matches) != 1:
            raise ValueError("context identity is ambiguous")
        cluster_name = matches[0]["context"]["cluster"]
        clusters = [entry for entry in config["clusters"] if entry["name"] == cluster_name]
        if len(clusters) != 1:
            raise ValueError("cluster identity is ambiguous")
        cluster = clusters[0]["cluster"]
        server = urlsplit(cluster["server"])
        if (
            server.scheme != "https"
            or server.hostname not in {"127.0.0.1", "localhost", "::1"}
            or server.username
            or server.password
            or server.path not in {"", "/"}
            or server.query
            or server.fragment
            or cluster.get("insecure-skip-tls-verify")
            or not (
                cluster.get("certificate-authority-data") or cluster.get("certificate-authority")
            )
        ):
            raise ValueError("context must bind to a CA-verified local HTTPS API")
    except (OSError, KeyError, TypeError, yaml.YAMLError, ValueError) as error:
        raise ValueError("unable to verify docker-desktop context and local HTTPS CA") from error
    for name in (control, workload):
        if not 1 <= len(name) <= 63 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", name):
            raise ValueError("namespace names must be concrete DNS labels")
    if control == workload:
        raise ValueError("control and workload namespaces must be distinct")
    command = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]
    observations: dict[str, object] = {}
    for name in (control, workload):
        try:
            result = subprocess.run(
                [*command, "get", "namespace", name, "-o", "json", "--request-timeout=5s"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"namespace {name}: unable to inspect selected cluster") from error
        if result.returncode == 0:
            try:
                body = json.loads(result.stdout)
                metadata = body["metadata"]
                if metadata["name"] != name or not metadata["uid"]:
                    raise ValueError("identity mismatch")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(f"namespace {name}: malformed identity") from error
            observations[name] = {"state": "occupied_refuse_adoption", "uid": metadata["uid"]}
        elif "(NotFound)" in result.stderr and f'"{name}"' in result.stderr:
            observations[name] = {"state": "absent_at_inspection_not_reserved"}
        else:
            # Forbidden, API unavailable, and ambiguous failures must not imply absence.
            raise ValueError(f"namespace {name}: lookup failed (not proven absent)")
    return observations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--control-namespace", required=True)
    parser.add_argument("--workload-namespace", required=True)
    args = parser.parse_args()
    try:
        namespaces = inspect_cluster(
            args.kubeconfig, args.context, args.control_namespace, args.workload_namespace
        )
        blockers = inspect_manifests()
        blockers.extend(
            f"namespace {name}: already exists; refuse adoption"
            for name, observation in namespaces.items()
            if observation["state"] == "occupied_refuse_adoption"
        )
        print(
            json.dumps(
                {"status": "blocked_no_effects", "namespaces": namespaces, "blockers": blockers},
                sort_keys=True,
            )
        )
    except ValueError as error:
        print(json.dumps({"status": "blocked_no_effects", "error": str(error)}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
