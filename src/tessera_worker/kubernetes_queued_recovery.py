"""Opt-in never-claimed evidence from retained journals and actual provider reads.

No claim, package reconstruction, Job submission, termination or control mutation.
The operator must stop the original worker and retain exclusive custody of its
workspace (never a replacement/backup). SQLite insert fences also deny old code.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .kubernetes_contracts import canonical_json, cluster_identity_digest

FIELDS = (
    "expected_revision",
    "grant_epoch",
    "dispatch_id",
    "operation_id",
    "lease_id",
    "epoch",
    "package_digest",
    "target_digest",
)


def attest_never_claimed(
    workspace,
    binding,
    cluster,
    *,
    exclusive_stopped_worker=False,
    inspect_namespace,
    inspect_job,
    list_operation_jobs,
    list_operation_pods,
    list_helper_pods,
):
    if exclusive_stopped_worker is not True:
        raise ValueError(
            "Original worker must be stopped with exclusive retained workspace custody"
        )
    if set(binding) != set(FIELDS):
        raise ValueError("Only exact non-executable recovery metadata is accepted")
    operation = binding["operation_id"]
    if not isinstance(operation, str) or not re.fullmatch("[0-9a-f]{32}", operation):
        raise ValueError("Invalid operation identity")
    if any(
        type(binding[field]) is not int or binding[field] < 1
        for field in ("expected_revision", "grant_epoch", "epoch")
    ):
        raise ValueError("Invalid revision/epoch")
    if binding["grant_epoch"] != binding["epoch"] + 1:
        raise ValueError("A cancelled original epoch is required")
    for field in ("target_digest", "package_digest"):
        if not isinstance(binding[field], str) or not re.fullmatch("[0-9a-f]{64}", binding[field]):
            raise ValueError("Invalid recovery digest")
    for field in ("dispatch_id", "lease_id"):
        if not isinstance(binding[field], str) or not 1 <= len(binding[field]) <= 200:
            raise ValueError("Invalid recovery identity")
    actual = cluster_identity_digest(cluster)
    if binding["target_digest"] == actual:
        relation = "exact"
    elif cluster.network_none_evidence is not None and binding[
        "target_digest"
    ] == cluster_identity_digest(replace(cluster, network_none_evidence=None)):
        relation = "declared_without_network_witness"
    else:
        raise ValueError("Declared target is neither exact nor same identity with omitted witness")
    paths = [
        workspace / name
        for name in ("kubernetes-jobs.sqlite3", "kubernetes-claims.sqlite3", "output-pods.sqlite3")
    ]
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError("All original journals must exist; never create/restore missing evidence")
    dispatch = binding["dispatch_id"].replace("'", "''")
    names = ["tessera-output-prepare-" + operation, "tessera-output-collect-" + operation]
    # Inspect exact operation absence; unrelated retained history is left intact.
    with sqlite3.connect(f"file:{paths[0]}?mode=rw", uri=True) as db:
        db.execute("ATTACH DATABASE ? AS claims", (f"file:{paths[1]}?mode=rw",))
        db.execute("ATTACH DATABASE ? AS output", (f"file:{paths[2]}?mode=rw",))

        def check():
            for schema in ("main", "claims", "output"):
                if db.execute(f"PRAGMA {schema}.integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Original journal integrity failed")
            if (
                db.execute(
                    "SELECT 1 FROM claims.claim_intents WHERE completed=1 LIMIT 1"
                ).fetchone()
                is None
            ):
                raise ValueError("Original scanner history is missing; empty journal is not proof")
            for schema in ("main", "output"):
                if db.execute(
                    f"SELECT 1 FROM {schema}.worker_failures WHERE operation_id=?", (operation,)
                ).fetchone():
                    raise ValueError(
                        "Worker effect/failure history prevents never-claimed evidence"
                    )
            for table in ("kubernetes_jobs", "kubernetes_execution_bindings"):
                if db.execute(
                    f"SELECT 1 FROM {table} WHERE operation_id=?", (operation,)
                ).fetchone():
                    raise ValueError("Job/adapter intent may have reached provider; keep unknown")
            if db.execute(
                "SELECT 1 FROM claims.dispatch_descriptors WHERE dispatch_id=?",
                (binding["dispatch_id"],),
            ).fetchone():
                raise ValueError("A retained descriptor prevents never-claimed evidence")
            if db.execute(
                "SELECT 1 FROM claims.dispatch_reports WHERE operation_id=?", (operation,)
            ).fetchone():
                raise ValueError("A retained worker report prevents never-claimed evidence")
            if db.execute("SELECT 1 FROM output.collectors WHERE name IN (?,?)", names).fetchone():
                raise ValueError("Helper intent prevents never-claimed evidence")

        db.execute("BEGIN IMMEDIATE")
        check()
        # Triggers fence inserts on the original databases, including old binaries.
        # No fabricated adapter binding, manifest or claim descriptor is inserted.
        triggers = []
        for schema, table, condition in (
            ("main", "kubernetes_jobs", f"NEW.operation_id='{operation}'"),
            ("main", "kubernetes_execution_bindings", f"NEW.operation_id='{operation}'"),
            ("claims", "dispatch_descriptors", f"NEW.dispatch_id='{dispatch}'"),
            ("output", "collectors", f"NEW.name IN ('{names[0]}','{names[1]}')"),
        ):
            name = "never_claimed_" + table + "_" + operation
            sql = (
                f"CREATE TRIGGER {schema}.{name} BEFORE INSERT ON {table} WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT, 'never-claimed operation fenced'); END"
            )
            prior = db.execute(
                f"SELECT sql FROM {schema}.sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchone()
            if prior is None:
                db.execute(sql)
            stored = db.execute(
                f"SELECT sql FROM {schema}.sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchone()[0]
            if stored != sql.replace(schema + ".", "", 1):
                raise ValueError("Original journal fence changed")
            triggers.append((schema, name, stored))
        db.commit()  # Read failure must leave a durable local submission fence.
        db.execute("BEGIN IMMEDIATE")
        check()
        observed_at = datetime.now(UTC).isoformat()
        namespace = inspect_namespace(cluster.namespace)
        if (
            not isinstance(namespace, dict)
            or namespace.get("metadata", {}).get("name") != cluster.namespace
            or namespace.get("metadata", {}).get("uid") != cluster.namespace_uid
        ):
            raise ValueError("Actual namespace identity changed/unavailable")
        job = inspect_job("tessera-op-" + operation)
        lists = [
            read(operation) for read in (list_operation_jobs, list_operation_pods, list_helper_pods)
        ]
        if job is not None or any(not isinstance(items, (list, tuple)) or items for items in lists):
            raise ValueError("Provider absence is contradicted or incomplete")
        check()
        for schema, name, sql in triggers:
            if (
                db.execute(
                    f"SELECT sql FROM {schema}.sqlite_master WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()[0]
                != sql
            ):
                raise ValueError("Journal fence changed during readbacks")
    return binding | {
        "proof_kind": "never_claimed",
        "actual_target_digest": actual,
        "target_relation": relation,
        "namespace_uid": cluster.namespace_uid,
        "job_name": "tessera-op-" + operation,
        "journal_fence_digest": hashlib.sha256(
            canonical_json([binding, actual, cluster.namespace_uid, triggers])
        ).hexdigest(),
        "observed_at": observed_at,
        "original_exclusive_journals": True,
        "descriptor_absent": True,
        "adapter_intent_absent": True,
        "job_intent_absent": True,
        "helper_intent_absent": True,
        "namespace_uid_checked": True,
        "deterministic_job_absent": True,
        "operation_jobs_absent": True,
        "operation_pods_absent": True,
        "helper_pods_absent": True,
    }


def main():
    import argparse
    from urllib.parse import quote

    from .kubernetes_preparation_recovery import operation_objects
    from .kubernetes_remote_service import configure

    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("target-file", "ca-file", "kubernetes-token-file", "workspace"):
        parser.add_argument("--" + flag, type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--binding-file", type=Path)
    mode.add_argument(
        "--maintenance-file",
        type=Path,
        help="Human-issued stop-only authorization; emits human reconciliation payload",
    )
    parser.add_argument("--exclusive-stopped-worker", action="store_true", required=True)
    args = parser.parse_args()
    # configure uses existing journals only: refuse missing files before its constructors.
    for name in ("kubernetes-claims.sqlite3", "kubernetes-jobs.sqlite3", "output-pods.sqlite3"):
        path = args.workspace / name
        if not path.is_file() or path.is_symlink():
            parser.error("original exclusive journals must exist")
        # Do not let configure's constructors make an empty replacement look intact.
        expected = {
            "kubernetes-claims.sqlite3": {
                "claim_intents",
                "dispatch_descriptors",
                "dispatch_reports",
            },
            "kubernetes-jobs.sqlite3": {
                "kubernetes_jobs",
                "kubernetes_execution_bindings",
                "worker_failures",
            },
            "output-pods.sqlite3": {"collectors", "worker_failures"},
        }[name]
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not expected <= tables:
                parser.error("original journal schema/history is missing")
    maintenance = None
    if args.maintenance_file is not None:
        from .kubernetes_maintenance_recovery import validate_authorization
        from .kubernetes_remote_service import _network, _private_json

        maintenance = json.loads(args.maintenance_file.read_bytes())
        metadata = _private_json(args.target_file)
        from .kubernetes_contracts import KubernetesClusterConfig

        preview_cluster = KubernetesClusterConfig(
            **metadata["cluster"], network_none_evidence=_network(metadata["network_evidence"])
        )
        validate_authorization(maintenance, preview_cluster)
        binding = {field: maintenance["authorization"][field] for field in FIELDS}
    else:
        binding = json.loads(args.binding_file.read_bytes())
    cluster, output, adapter = configure(
        args.target_file, args.ca_file, args.kubernetes_token_file, args.workspace
    )
    client = adapter._prepare_output.__self__.client

    def helpers(operation):
        response = client.request(
            "GET",
            client.path("pod")
            + "?limit=100&labelSelector="
            + quote("tessera.dev/output-operation=" + operation, safe=""),
        )
        if (
            not isinstance(response, dict)
            or response.get("kind") != "PodList"
            or response.get("metadata", {}).get("continue")
            or not isinstance(response.get("items"), list)
        ):
            raise ValueError("Helper absence readback is incomplete")
        return response["items"]

    readers = dict(
        exclusive_stopped_worker=True,
        inspect_namespace=lambda name: client.get("namespace", name),
        inspect_job=lambda name: client.get("job", name),
        list_operation_jobs=lambda op: operation_objects(client, "job", op),
        list_operation_pods=lambda op: operation_objects(client, "pod", op),
        list_helper_pods=helpers,
    )
    if maintenance is None:
        proof = attest_never_claimed(args.workspace, binding, cluster, **readers)
    else:
        from .kubernetes_maintenance_recovery import collect_maintenance_proof

        proof = collect_maintenance_proof(args.workspace, maintenance, cluster, **readers)
    client.verify(output)
    print(json.dumps(proof, sort_keys=True))


if __name__ == "__main__":
    main()
