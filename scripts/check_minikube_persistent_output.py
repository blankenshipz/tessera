"""Run the explicit Minikube persistent-output development fixture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tessera_worker.minikube_persistent_output_fixture import (
    PROFILE,
    FixtureResult,
    PersistentOutputTarget,
    fixture_exit_code,
    fixture_manifest,
    hello_binding,
    run_fixture,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="explicitly create the fixture Job")
    required = (
        "kubeconfig",
        "context",
        "server",
        "ca-file",
        "namespace",
        "profile",
        "minikube-profile",
        "pvc",
        "output-mount",
        "cluster-uid",
        "namespace-uid",
        "pvc-uid",
        "pvc-spec-sha256",
        "operation-id",
        "attempt-id",
        "lease-id",
        "image",
    )
    for name in required:
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--journal", type=Path, default=Path(".tessera/minikube-fixture.sqlite3"))
    args = parser.parse_args()
    target = PersistentOutputTarget(
        kubeconfig=Path(args.kubeconfig),
        context=args.context,
        server=args.server,
        ca_file=Path(args.ca_file),
        namespace=args.namespace,
        profile=args.profile,
        minikube_profile=args.minikube_profile,
        pvc=args.pvc,
        output_mount=args.output_mount,
        cluster_uid=args.cluster_uid,
        namespace_uid=args.namespace_uid,
        pvc_uid=args.pvc_uid,
        pvc_spec_sha256=args.pvc_spec_sha256,
    )
    if args.profile != PROFILE:
        parser.error(f"--profile must be {PROFILE}")
    binding = hello_binding(
        operation_id=args.operation_id,
        attempt_id=args.attempt_id,
        lease_id=args.lease_id,
        epoch=args.epoch,
        image=args.image,
        output_pvc=args.pvc,
        output_mount=args.output_mount,
        output_pvc_uid=args.pvc_uid,
        output_pvc_spec_sha256=args.pvc_spec_sha256,
    )
    if not args.run:
        print(json.dumps(fixture_manifest(target, binding), sort_keys=True))
        return 0
    result: FixtureResult = run_fixture(target, binding, args.journal)
    print(json.dumps(result.__dict__, sort_keys=True))
    # Exit zero is only a verified fixture result, never result acceptance.
    return fixture_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
