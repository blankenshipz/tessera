"""Distinct evidence for the historical prepare-before-Job-create failure.

The historical create_intent remains true. This evidence is NOT compatible with
the zero-create/pre-manifest control contracts and must not be relabelled. Only
an exclusive original workspace plus fresh provider readbacks is meaningful.
No Job submission, helper cleanup, provider identity or terminal outcome occurs.
The explicit bound_job_intent CLI mode uses the existing zero-create contract
for intact, cancelled original journals; helper UID absence does not become a
provider termination receipt in either mode.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .kubernetes_contracts import (
    KubernetesJobJournal,
    _job_manifest,
    canonical_json,
    cluster_identity_digest,
)


def attest_preparation_failure(
    package, collector, execution_journal, *, exclusive_stopped_worker=False
):
    if exclusive_stopped_worker is not True:
        raise ValueError("Original submitting worker must be explicitly stopped and exclusive")
    for path in (execution_journal, collector.journal):
        if not path.is_file() or path.is_symlink():
            raise ValueError("Original exclusive worker journals are required")
    binding, cluster = package.binding, collector.client.cluster
    target = cluster_identity_digest(cluster)
    encoded_binding = canonical_json(binding.journal_fields).decode()
    encoded_manifest = canonical_json(
        _job_manifest(binding, cluster, require_fresh_network_evidence=False)
    ).decode()
    encoded_intent = canonical_json(
        {
            "binding": binding.journal_fields,
            "package_digest": package.package_digest,
            "target_digest": target,
        }
    ).decode()
    name = "tessera-output-prepare-" + package.operation_id
    with sqlite3.connect(f"file:{execution_journal}?mode=rw", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ? AS preparation", (f"file:{collector.journal}?mode=rw",))
        db.execute("BEGIN IMMEDIATE")
        job = db.execute(
            "SELECT * FROM kubernetes_jobs WHERE operation_id=?", (package.operation_id,)
        ).fetchone()
        adapter = db.execute(
            "SELECT * FROM kubernetes_execution_bindings WHERE operation_id=?",
            (package.operation_id,),
        ).fetchone()
        preparation = db.execute(
            "SELECT * FROM preparation.collectors WHERE name=?", (name,)
        ).fetchone()
        if (
            job is None
            or adapter is None
            or preparation is None
            or not binding.output_operation_scoped
            or job["binding_json"] != encoded_binding
            or job["manifest_json"] != encoded_manifest
            or job["create_intent"] != 1
            or job["job_uid"] is not None
            or adapter["intent_json"] != encoded_intent
            or adapter["target_digest"] != target
            or adapter["namespace_uid"] != cluster.namespace_uid
            or adapter["namespace"] != cluster.namespace
            or adapter["job_name"] != binding.job_name
            or adapter["pvc_uid"] != binding.output_pvc_uid
            or adapter["pvc_name"] != binding.output_pvc
            or adapter["pvc_spec_sha256"] != binding.output_pvc_spec_sha256
            or adapter["job_uid"] is not None
            or adapter["pod_uids_json"] != "[]"
            or adapter["observed_state"] != "unknown"
            or adapter["handoff_json"] is not None
            or preparation["result"] is not None
        ):
            raise ValueError("Original unacknowledged preparation gate is missing or conflicted")
        if not isinstance(preparation["uid"], str) or not preparation["uid"].strip():
            raise ValueError(
                "Original preparation helper UID was not retained; current lookup cannot repair it"
            )
        manifest = json.loads(preparation["spec"])
        image = manifest["spec"]["containers"][0]["image"]
        if not isinstance(image, str) or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("Original preparation image is not pinned")
        if canonical_json(manifest) != canonical_json(
            collector.manifest("prepare", package, image=image)
        ):
            raise ValueError("Original preparation manifest conflicts with package provenance")
        # Fence the original Job and adapter while retaining the true historical
        # create_intent. Locks include the collector receipt publication gate.
        db.execute(
            "UPDATE kubernetes_jobs SET cancellation_requested=1 WHERE operation_id=?",
            (package.operation_id,),
        )
        db.execute(
            "UPDATE kubernetes_execution_bindings SET cancellation_requested=1 "
            "WHERE operation_id=?",
            (package.operation_id,),
        )
        db.commit()  # A failed provider read must never erase the cancellation fence.
        db.execute("BEGIN IMMEDIATE")
        expected_job, expected_adapter = dict(job), dict(adapter)
        expected_job["cancellation_requested"] = expected_adapter["cancellation_requested"] = 1
        current_job = db.execute(
            "SELECT * FROM kubernetes_jobs WHERE operation_id=?", (package.operation_id,)
        ).fetchone()
        current_adapter = db.execute(
            "SELECT * FROM kubernetes_execution_bindings WHERE operation_id=?",
            (package.operation_id,),
        ).fetchone()
        current_preparation = db.execute(
            "SELECT * FROM preparation.collectors WHERE name=?", (name,)
        ).fetchone()
        if (
            current_job is None
            or dict(current_job) != expected_job
            or current_adapter is None
            or dict(current_adapter) != expected_adapter
            or current_preparation is None
            or dict(current_preparation) != dict(preparation)
        ):
            raise ValueError("Original preparation gate changed while fencing")
        collector.client.verify(collector.output)
        pod = collector.client.get("pod", name)
        collector.verify_pod(pod, manifest, preparation["uid"])
        if pod.get("status", {}).get("phase") != "Succeeded":
            raise ValueError("Preparation helper has no successful terminal evidence")

        observed_at = KubernetesJobJournal._never_submitted_readbacks(
            binding,
            cluster,
            lambda namespace: collector.client.get("namespace", namespace),
            lambda job_name: collector.client.get("job", job_name),
            lambda operation: operation_objects(collector.client, "job", operation),
            lambda operation: operation_objects(collector.client, "pod", operation),
        )
        collector.client.verify(collector.output)
        collector.verify_pod(collector.client.get("pod", name), manifest, pod["metadata"]["uid"])
        proof = {
            "proof_kind": "pre_job_preparation",
            "operation_id": binding.operation_id,
            "lease_id": binding.lease_id,
            "epoch": binding.epoch,
            "package_digest": package.package_digest,
            "target_digest": target,
            "namespace_uid": cluster.namespace_uid,
            "job_name": binding.job_name,
            "observed_at": observed_at,
            "create_intent": True,
            "cancellation_requested": True,
            "job_uid": None,
            "manifest_not_generated": False,
            "journal_binding_digest": hashlib.sha256(encoded_binding.encode()).hexdigest(),
            "journal_manifest_digest": hashlib.sha256(encoded_manifest.encode()).hexdigest(),
            "preparation_manifest_digest": hashlib.sha256(preparation["spec"].encode()).hexdigest(),
            "preparation_pod_uid": pod["metadata"]["uid"],
            "preparation_journal_pod_uid": preparation["uid"],
            "worker_attests_original_exclusive_stopped": True,
            "preparation_receipt_absent": True,
            "preparation_gate": "ordinary-output-prepare-before-job-v1",
            "job_request_not_reached": True,
            "namespace_uid_checked": True,
            "deterministic_job_absent": True,
            "operation_jobs_absent": True,
            "operation_pods_absent": True,
        }
    return proof


def manifest_label(operation):
    from urllib.parse import quote

    from .kubernetes_contracts import kubernetes_resource_name

    return quote(
        "tessera.dev/operation=" + kubernetes_resource_name(operation, "operation_id"), safe=""
    )


def operation_objects(client, kind, operation):
    response = client.request(
        "GET", client.path(kind) + "?limit=100&labelSelector=" + manifest_label(operation)
    )
    if (
        not isinstance(response, dict)
        or response.get("kind") != ("JobList" if kind == "job" else "PodList")
        or response.get("metadata", {}).get("continue")
        or not isinstance(response.get("items"), list)
    ):
        raise ValueError("Workload absence readback is incomplete")
    return response["items"]


def attest_bound_job_intent(package, collector, journal, *, exclusive_stopped_worker=False):
    if exclusive_stopped_worker is not True:
        raise ValueError("Original submitting worker must be explicitly stopped and exclusive")
    collector.client.verify(collector.output)
    proof = journal.attest_never_submitted(
        package.binding,
        collector.client.cluster,
        inspect_namespace=lambda namespace: collector.client.get("namespace", namespace),
        inspect_job=lambda name: collector.client.get("job", name),
        list_operation_jobs=lambda operation: operation_objects(collector.client, "job", operation),
        list_operation_pods=lambda operation: operation_objects(collector.client, "pod", operation),
    )
    collector.client.verify(collector.output)
    return proof


def main():
    """Installed, opt-in evidence CLI; never performs control reconciliation."""
    import argparse
    from dataclasses import replace
    from pathlib import Path

    from .kubernetes_execution import KubernetesExecutionPackage
    from .kubernetes_remote_service import configure

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--kubernetes-token-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--exclusive-stopped-worker", action="store_true", required=True)
    parser.add_argument(
        "--proof-kind",
        choices=("pre_job_preparation", "bound_job_intent"),
        default="pre_job_preparation",
    )
    args = parser.parse_args()
    claims = args.workspace / "kubernetes-claims.sqlite3"
    jobs = args.workspace / "kubernetes-jobs.sqlite3"
    for path in (claims, jobs, args.workspace / "output-pods.sqlite3"):
        if not path.is_file() or path.is_symlink():
            parser.error("original private worker journals must exist")
    with sqlite3.connect(f"file:{claims}?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT descriptor_json FROM dispatch_descriptors WHERE dispatch_id=?",
            (args.dispatch_id,),
        ).fetchone()
    if row is None:
        parser.error("original dispatch descriptor is missing")
    descriptor = json.loads(row[0])
    _, output, adapter = configure(
        args.target_file, args.ca_file, args.kubernetes_token_file, args.workspace
    )
    package = KubernetesExecutionPackage.from_admitted_package(descriptor["response"], output)
    seconds = descriptor.get("provider_active_deadline_seconds")
    if type(seconds) is not int or not 1 <= seconds <= package.binding.active_deadline_seconds:
        parser.error("original provider deadline is unavailable")
    package = replace(package, binding=replace(package.binding, active_deadline_seconds=seconds))
    collector = adapter._prepare_output.__self__
    if descriptor["target_digest"] != collector.identity(package)["target"]:
        parser.error("original target digest changed")
    proof = (
        attest_bound_job_intent(
            package,
            collector,
            adapter._journal,
            exclusive_stopped_worker=args.exclusive_stopped_worker,
        )
        if args.proof_kind == "bound_job_intent"
        else attest_preparation_failure(
            package, collector, jobs, exclusive_stopped_worker=args.exclusive_stopped_worker
        )
    )
    print(json.dumps(proof, sort_keys=True))


if __name__ == "__main__":
    main()
