# ruff: noqa: E501
"""Run the explicitly authorized, retained Docker Desktop Kubernetes Job/PVC E2E.

This is a narrow provider-disabled conformance run, not control-plane dispatch,
OpenCode compatibility, artifact acceptance, or pilot evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tessera_worker.docker_desktop_kubernetes import (
    CONTEXT,
    DockerDesktopKubernetesAdapter,
    LiveKubernetesError,
    build_and_load_exact_image,
    new_nonce,
    target_from_kubeconfig,
)


def _fixture_result(
    verdict: str, *, state: str, message: str | None = None, **evidence: object
) -> tuple[dict[str, object], int]:
    """Keep fixture assessment separate from the observed operational state."""
    result: dict[str, object] = {"fixture_verdict": verdict, "state": state, **evidence}
    if message is not None:
        result["message"] = message
    return result, 0 if verdict == "passed" else 1


def _emit_fixture_result(result: dict[str, object], exit_code: int) -> int:
    print(json.dumps(result, sort_keys=True))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="build/load and create retained resources"
    )
    parser.add_argument("--kubeconfig", type=Path, help="explicit kubeconfig path")
    parser.add_argument("--context", default=CONTEXT, choices=[CONTEXT])
    parser.add_argument(
        "--journal", type=Path, help="outside-repository local create/cancel journal"
    )
    parser.add_argument(
        "--namespace",
        help="existing explicit retained namespace; no namespace create occurs when supplied",
    )
    args = parser.parse_args()
    if not args.run:
        result, exit_code = _fixture_result(
            "skipped",
            state="not_started",
            message="--run was not supplied; this command intentionally has no ambient live mode",
        )
        return _emit_fixture_result(result, exit_code)
    if args.kubeconfig is None:
        result, exit_code = _fixture_result(
            "failed", state="not_started", message="--kubeconfig is required when --run is supplied"
        )
        return _emit_fixture_result(result, exit_code)
    nonce = new_nonce()
    journal = args.journal or Path("/tmp") / f"tessera-kubernetes-e2e-{nonce}.sqlite3"
    try:
        target = target_from_kubeconfig(args.kubeconfig)
        image = build_and_load_exact_image(Path(__file__).resolve().parents[1], nonce)
        adapter = DockerDesktopKubernetesAdapter(target, journal)
        evidence = (
            adapter.run(image, nonce)
            if args.namespace is None
            else adapter.run(image, nonce, args.namespace)
        )
    except (LiveKubernetesError, OSError, ValueError) as error:
        result, exit_code = _fixture_result("failed", state="failed", message=str(error))
        return _emit_fixture_result(result, exit_code)
    result, exit_code = _fixture_result(
        "passed",
        **{
            "state": "succeeded",
            "context": CONTEXT,
            "journal": str(journal),
            "namespace": evidence.namespace,
            "namespace_uid": evidence.namespace_uid,
            "pvc": evidence.pvc,
            "pvc_uid": evidence.pvc_uid,
            "pvc_spec_sha256": evidence.pvc_spec_sha256,
            "job": evidence.job,
            "job_uid": evidence.job_uid,
            "pod_uids": evidence.pod_uids,
            "collector": evidence.collector,
            "collector_uid": evidence.collector_uid,
            "collector_image_id": evidence.collector_image_id,
            "image": evidence.image.reference,
            "image_manifest_digest": evidence.image.manifest_digest,
            "image_config_digest": evidence.image.config_digest,
            "image_archive_sha256": evidence.image.archive_sha256,
            "output_utf8": evidence.output_bytes.decode("utf-8"),
            "output_sha256": evidence.output_sha256,
            "job_state": evidence.job_state,
            "limitations": [
                "provider-disabled Kubernetes Job/PVC conformance only",
                "no control-plane remote dispatch or result/artifact acceptance",
                "no OpenCode, model, provider, or credential invocation",
                "resources are retained; no namespace deletion or prune was issued",
            ],
        },
    )
    return _emit_fixture_result(result, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
