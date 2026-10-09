"""Read-only, fail-closed checks before binding an opt-in Kubernetes dispatch target.

This is not a connectivity, admission-policy enforcement, or execution proof.
No implicit kubectl context, namespace, or control-state PVC is accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class TargetError(RuntimeError):
    pass


def inspect_target(
    kubeconfig: Path,
    context: str,
    control_namespace: str,
    workload_namespace: str,
    *,
    run=subprocess.run,
) -> dict[str, str]:
    """Inspect only named resources; no get-all, secret or logs requests."""
    if not kubeconfig.is_absolute() or not kubeconfig.is_file() or kubeconfig.is_symlink():
        raise TargetError("explicit regular absolute kubeconfig is required")
    if not context or context.startswith("-") or not control_namespace or not workload_namespace:
        raise TargetError("explicit context and two namespaces are required")
    if control_namespace == workload_namespace:
        raise TargetError("control and workload namespaces must differ")
    if any(
        len(value) > 63 or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value) is None
        for value in (control_namespace, workload_namespace)
    ):
        raise TargetError("namespace names must be concrete DNS labels")

    def get(kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
        cmd = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]
        if namespace:
            cmd.extend(["--namespace", namespace])
        cmd.extend(["get", kind, name, "-o", "json", "--request-timeout=5s"])
        try:
            result = run(cmd, check=True, capture_output=True, text=True, timeout=8)
            body = json.loads(result.stdout)
            if not isinstance(body, dict):
                raise ValueError("unexpected resource response")
            return body
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise TargetError(f"unable to inspect named {kind} prerequisite") from error

    control = get("namespace", control_namespace)
    workload = get("namespace", workload_namespace)
    pvc = get("pvc", "outputs", workload_namespace)
    policy = get("networkpolicy", "default-deny", workload_namespace)
    for resource, name, namespace in (
        (control, control_namespace, None),
        (workload, workload_namespace, None),
        (pvc, "outputs", workload_namespace),
        (policy, "default-deny", workload_namespace),
    ):
        metadata = resource.get("metadata")
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or (namespace is not None and metadata.get("namespace") != namespace)
        ):
            raise TargetError("named resource identity does not match the requested target")
    control_uid = control.get("metadata", {}).get("uid")
    workload_uid = workload.get("metadata", {}).get("uid")
    pvc_uid = pvc.get("metadata", {}).get("uid")
    policy_uid = policy.get("metadata", {}).get("uid")
    if not all(
        isinstance(uid, str) and uid for uid in (control_uid, workload_uid, pvc_uid, policy_uid)
    ):
        raise TargetError("target identities are unavailable")
    if control_uid == workload_uid:
        raise TargetError("namespace identities are not distinct")
    if pvc.get("status", {}).get("phase") != "Bound" or pvc.get("spec", {}).get("accessModes") != [
        "ReadWriteOnce"
    ]:
        raise TargetError("output PVC must be Bound and ReadWriteOnce")
    if policy.get("spec") != {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}:
        raise TargetError("exact workload default-deny declaration is required")
    pvc_spec = pvc.get("spec")
    if not isinstance(pvc_spec, dict):
        raise TargetError("output PVC specification is unavailable")
    return {
        "status": "prerequisites_observed_not_enforcement_proven",
        "control_namespace_uid": control_uid,
        "workload_namespace_uid": workload_uid,
        "output_pvc_uid": pvc_uid,
        "output_pvc_spec_sha256": hashlib.sha256(
            json.dumps(pvc_spec, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
        "default_deny_policy_uid": policy_uid,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--control-namespace", required=True)
    parser.add_argument("--workload-namespace", required=True)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                inspect_target(
                    args.kubeconfig, args.context, args.control_namespace, args.workload_namespace
                )
            )
        )
        return 0
    except TargetError as error:
        print(f"dispatch target check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
