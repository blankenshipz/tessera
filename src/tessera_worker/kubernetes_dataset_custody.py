"""Durable, operation-owned immutable dataset custody in the claim journal.

Cached actual bytes support observation/cancellation recovery only. Every create
or adoption requires new authenticated control input delivery; cache is never
authority. Native client methods retain namespace/cluster/RBAC/UID fences.
"""

import hashlib
import json
import sqlite3

from .dataset_inputs import delete_input, prepare_dataset_inputs
from .kubernetes_contracts import canonical_json, cluster_identity_digest


class DatasetCustodyError(RuntimeError):
    pass


class KubernetesDatasetCustody:
    def __init__(self, claims, cluster, client, fetch_inputs):
        self.claims = claims
        self.cluster = cluster
        self.client = client
        self.fetch_inputs = fetch_inputs
        with sqlite3.connect(claims.path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS dataset_bundles (
                dispatch_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                package_digest TEXT NOT NULL, bundle_json TEXT NOT NULL,
                PRIMARY KEY(dispatch_id,operation_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS dataset_custody (
                dispatch_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                binding_digest TEXT NOT NULL, resources_digest TEXT NOT NULL,
                PRIMARY KEY(dispatch_id,operation_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS dataset_resource_custody (
                dispatch_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                name TEXT NOT NULL, resource_digest TEXT NOT NULL, uid TEXT,
                deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
                PRIMARY KEY(dispatch_id,operation_id,name))""")
            c.execute("""CREATE TABLE IF NOT EXISTS dataset_create_attempts (
                dispatch_id TEXT NOT NULL, operation_id TEXT NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY(dispatch_id,operation_id,name))""")

    def retain_bundle(self, response, bundle):
        package = response["package"]
        encoded = canonical_json(bundle)
        if len(encoded) > 8 * 1024 * 1024:
            raise DatasetCustodyError("Recovery input envelope exceeds bound")
        key = (self.claims.dispatch_id, package["operation_id"])
        with sqlite3.connect(self.claims.path) as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT package_digest,bundle_json FROM dataset_bundles "
                "WHERE dispatch_id=? AND operation_id=?",
                key,
            ).fetchone()
            if old is not None:
                # Receipts can refresh; the retained original exact bytes remain
                # solely a recovery input, not a fresh authorization response.
                if (
                    old[0] != response["package_digest"]
                    or json.loads(old[1])["snapshot"] != bundle["snapshot"]
                ):
                    raise DatasetCustodyError("Recovery dataset/package changed")
            else:
                c.execute(
                    "INSERT INTO dataset_bundles VALUES (?,?,?,?)",
                    (*key, response["package_digest"], encoded.decode()),
                )

    def recovery_bundle(self, response):
        with sqlite3.connect(self.claims.path) as c:
            row = c.execute(
                "SELECT package_digest,bundle_json FROM dataset_bundles "
                "WHERE dispatch_id=? AND operation_id=?",
                (self.claims.dispatch_id, response["package"]["operation_id"]),
            ).fetchone()
        if row is None or row[0] != response["package_digest"]:
            raise DatasetCustodyError("No exact retained dataset recovery envelope")
        return json.loads(row[1])

    def current(self, package):
        bundle = self.fetch_inputs(package)
        descriptor, resources = prepare_dataset_inputs(
            bundle,
            snapshot_digest=package.package["snapshot_digest"],
            manifest_digest=package.binding.dataset_input["manifest_digest"],
            lease_id=package.binding.lease_id,
            epoch=package.binding.epoch,
            worker_id=package.package["worker_principal_id"],
            operation_id=package.operation_id,
            namespace=self.cluster.namespace,
            grant_id=package.package["grant_id"],
            project_id=package.package["project_id"],
        )
        if (
            descriptor != package.binding.dataset_input
            or tuple(resources) != package.dataset_resources
        ):
            raise DatasetCustodyError("Current authorized input differs from native intent")

    def _key(self, package):
        return self.claims.dispatch_id, package.operation_id

    def _declare(self, package):
        resources = package.dataset_resources
        binding = {
            "lease": package.binding.lease_id,
            "epoch": package.binding.epoch,
            "package": package.package_digest,
            "cluster": cluster_identity_digest(self.cluster),
            "grant": package.package["grant_id"],
            "project": package.package["project_id"],
        }
        binding_digest = hashlib.sha256(canonical_json(binding)).hexdigest()
        resources_digest = hashlib.sha256(canonical_json(resources)).hexdigest()
        key = self._key(package)
        with sqlite3.connect(self.claims.path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT binding_digest,resources_digest FROM dataset_custody "
                "WHERE dispatch_id=? AND operation_id=?",
                key,
            ).fetchone()
            if row is not None and row != (binding_digest, resources_digest):
                raise DatasetCustodyError("Immutable dataset custody intent changed")
            c.execute(
                "INSERT OR IGNORE INTO dataset_custody VALUES (?,?,?,?)",
                (*key, binding_digest, resources_digest),
            )
            for resource in resources:
                name = resource["metadata"]["name"]
                sha = hashlib.sha256(canonical_json(resource)).hexdigest()
                old = c.execute(
                    "SELECT resource_digest,deleted FROM dataset_resource_custody "
                    "WHERE dispatch_id=? AND operation_id=? AND name=?",
                    (*key, name),
                ).fetchone()
                if old is not None and (old[0] != sha or old[1]):
                    raise DatasetCustodyError("Resource intent conflicts or was already deleted")
                c.execute(
                    "INSERT OR IGNORE INTO dataset_resource_custody VALUES (?,?,?,?,NULL,0)",
                    (*key, name, sha),
                )

    @staticmethod
    def _matches(actual, expected):
        if not isinstance(actual, dict) or actual.get("immutable") is not True:
            return False
        return all(
            actual.get(k) == expected[k] for k in ("apiVersion", "kind", "binaryData")
        ) and all(actual.get("metadata", {}).get(k) == v for k, v in expected["metadata"].items())

    def _record(self, package, receipt):
        key = (*self._key(package), receipt["name"])
        with sqlite3.connect(self.claims.path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT uid,deleted FROM dataset_resource_custody "
                "WHERE dispatch_id=? AND operation_id=? AND name=?",
                key,
            ).fetchone()
            if row is None or row[1] or row[0] not in (None, receipt["uid"]):
                raise DatasetCustodyError("Resource UID compare-and-set failed")
            c.execute(
                "UPDATE dataset_resource_custody SET uid=? "
                "WHERE dispatch_id=? AND operation_id=? AND name=? AND deleted=0",
                (receipt["uid"], *key),
            )

    def prepare(self, package):
        """Preserve standalone synchronous preparation and its custody journal."""
        steps = self.prepare_steps(package)
        result = None
        while True:
            try:
                callback, arguments = steps.send(result)
            except StopIteration as done:
                return done.value
            result = callback(*arguments)

    def prepare_steps(self, package):
        """Yield one native command at a time; record its UID before yielding again."""
        from .dataset_inputs import create_input_steps

        self.current(package)
        self._declare(package)  # every deterministic resource planned BEFORE effect

        def create_or_adopt(expected):
            name = expected["metadata"]["name"]
            with sqlite3.connect(self.claims.path) as c:
                prior = c.execute(
                    "SELECT uid,deleted FROM dataset_resource_custody "
                    "WHERE dispatch_id=? AND operation_id=? AND name=?",
                    (*self._key(package), name),
                ).fetchone()
            if prior is None or prior[1]:
                raise DatasetCustodyError("No live declared immutable resource intent")
            actual = yield self.client.dataset_read, (name,)
            if actual is None:
                if prior[0] is not None:
                    raise DatasetCustodyError(
                        "Previously bound resource disappeared; no replacement"
                    )
                with sqlite3.connect(self.claims.path) as c:
                    c.execute("BEGIN IMMEDIATE")
                    key = (*self._key(package), name)
                    if c.execute(
                        "SELECT 1 FROM dataset_create_attempts "
                        "WHERE dispatch_id=? AND operation_id=? AND name=?",
                        key,
                    ).fetchone():
                        raise DatasetCustodyError("Uncertain create remains absent; inspect only")
                    c.execute("INSERT INTO dataset_create_attempts VALUES (?,?,?)", key)
                self.current(package)
                actual = yield self.client.dataset_create, (expected,)
            elif prior[0] is not None and actual.get("metadata", {}).get("uid") != prior[0]:
                raise DatasetCustodyError("Native UID changed; refuse replacement adoption")
            elif not self._matches(actual, expected):
                raise DatasetCustodyError("Refuse foreign/conflicting immutable resource adoption")
            # A lost create reply leaves a durable planned name/spec. On restart
            # adoption is limited to those exact bytes/owner/namespace/operation.
            if not self._matches(actual, expected):
                raise DatasetCustodyError("Native resource conflicts with declared intent")
            return actual

        return (
            yield from create_input_steps(
                package.dataset_resources,
                check_current_authority=lambda: self.current(package),
                create_steps=create_or_adopt,
                read=self.client.dataset_read,
                record_receipt=lambda receipt: self._record(package, receipt),
            )
        )

    def cleanup(self, package):
        key = self._key(package)
        with sqlite3.connect(self.claims.path) as c:
            rows = c.execute(
                "SELECT name,resource_digest,uid,deleted FROM dataset_resource_custody "
                "WHERE dispatch_id=? AND operation_id=? ORDER BY name",
                key,
            ).fetchall()
        expected = {r["metadata"]["name"]: r for r in package.dataset_resources}
        for name, sha, uid, deleted in rows:
            if deleted:
                continue
            resource = expected.get(name)
            if resource is None or hashlib.sha256(canonical_json(resource)).hexdigest() != sha:
                raise DatasetCustodyError("Cleanup resource intent changed")
            if uid is None:
                with sqlite3.connect(self.claims.path) as c:
                    attempted = c.execute(
                        "SELECT 1 FROM dataset_create_attempts "
                        "WHERE dispatch_id=? AND operation_id=? AND name=?",
                        (*key, name),
                    ).fetchone()
                if attempted:
                    raise DatasetCustodyError("Uncertain create UID requires reconciliation")
            actual = self.client.dataset_read(name)
            if actual is not None:
                if (
                    uid is None
                    or not self._matches(actual, resource)
                    or actual["metadata"].get("uid") != uid
                ):
                    raise DatasetCustodyError("Uncertain/foreign resource requires reconciliation")
                receipt = {
                    "name": name,
                    "uid": uid,
                    "namespace": self.cluster.namespace,
                    "project_id": package.package["project_id"],
                    "grant_id": package.package["grant_id"],
                }
                delete_input(
                    receipt,
                    operation_id=package.operation_id,
                    read=self.client.dataset_read,
                    delete_exact_uid=self.client.dataset_delete,
                )
                if self.client.dataset_read(name) is not None:
                    raise DatasetCustodyError("Native UID deletion not yet confirmed")
            with sqlite3.connect(self.claims.path) as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "UPDATE dataset_resource_custody SET deleted=1 "
                    "WHERE dispatch_id=? AND operation_id=? AND name=? AND uid IS ?",
                    (*key, name, uid),
                )
