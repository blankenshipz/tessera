"""UID-fenced, networkless pods for ordinary service output custody.

Uses only the explicitly configured namespaced client and pinned installed
Tessera image. No exec, ambient kubeconfig or token forwarding. Cleanup deletes
only acknowledged terminal helper Pods with immutable UID preconditions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import time

from .kubernetes_contracts import canonical_json, cluster_identity_digest, parse_job, parse_pod
from .kubernetes_pod_listing import list_owned_pods
from .outputs import MAX_OUTPUT_BYTES, CollectedOutput, validate_output_paths


class KubernetesOutputCollector:
    def __init__(self, client, output, image, journal):
        if output.operation_scoped is not True:
            raise ValueError("Ordinary output collection requires operation-scoped mounts")
        if not isinstance(image, str) or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("Output collector requires a digest-pinned installed Tessera image")
        self.client, self.output, self.image, self.journal = client, output, image, journal
        with sqlite3.connect(journal) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS collectors "
                "(name TEXT PRIMARY KEY, spec TEXT NOT NULL, uid TEXT, result TEXT)"
            )
            if "result" not in {row[1] for row in db.execute("PRAGMA table_info(collectors)")}:
                db.execute("ALTER TABLE collectors ADD COLUMN result TEXT")
        journal.chmod(0o600)
        from .kubernetes_failure_evidence import KubernetesFailureEvidence

        self.failures = KubernetesFailureEvidence(journal)

    def identity(self, package):
        return {
            "operation": package.operation_id,
            "attempt": package.binding.attempt_id,
            "lease": package.binding.lease_id,
            "epoch": package.binding.epoch,
            "package": package.package_digest,
            "pvc": self.output.pvc_uid,
            "target": cluster_identity_digest(self.client.cluster),
        }

    def prepare(self, package):
        paths = validate_output_paths(package.package["output_paths"])
        if len(paths) > 128:
            raise ValueError("Ordinary output collection supports at most 128 declared files")
        self.execute("prepare", package)
        try:
            self.release("prepare", package)
        except Exception as error:
            self.failures.record(package.operation_id, "prepare.cleanup", error)
            raise

    def release_collection(self, package):
        # Called only after immutable bytes have been committed to local custody.
        try:
            self.release("collect", package)
        except Exception as error:
            self.failures.record(package.operation_id, "collect.cleanup", error)
            raise

    def release(self, mode, package):
        name = "tessera-output-" + mode + "-" + package.operation_id
        with sqlite3.connect(self.journal) as db:
            row = db.execute(
                "SELECT spec,uid,result FROM collectors WHERE name=?", (name,)
            ).fetchone()
        if row is None or row[1] is None or row[2] is None:
            raise ValueError("Collector cleanup requires an acknowledged owned result")
        self.client.verify(self.output)
        pod = self.client.get("pod", name)
        if pod is None:
            return  # A prior UID-preconditioned delete may have lost its reply.
        self.verify_pod(pod, json.loads(row[0]), row[1])
        if pod.get("status", {}).get("phase") != "Succeeded":
            raise ValueError("Collector cleanup requires a confirmed terminal helper Pod")
        self.client.request(
            "DELETE",
            self.client.path("pod", name),
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "gracePeriodSeconds": 0,
                "preconditions": {"uid": row[1]},
            },
        )
        deadline = time.monotonic() + 5
        while True:
            pod = self.client.get("pod", name)
            if pod is None:
                return
            self.verify_pod(pod, json.loads(row[0]), row[1])
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned terminal collector deletion is still pending")
            time.sleep(0.1)

    def collect(self, handoff, package):
        if (
            handoff.operation_id != package.operation_id
            or handoff.admitted_attempt_id != package.binding.attempt_id
            or handoff.pvc_uid != self.output.pvc_uid
            or not handoff.job_uid
            or not handoff.pod_uids
            or handoff.target_digest != cluster_identity_digest(self.client.cluster)
            or handoff.namespace != self.client.cluster.namespace
            or handoff.namespace_uid != self.client.cluster.namespace_uid
            or handoff.pvc_name != self.output.pvc_name
            or handoff.pvc_spec_sha256 != self.output.pvc_spec_sha256
            or handoff.mount_path != self.output.mount_path
            or tuple(handoff.output_paths) != tuple(package.package["output_paths"])
        ):
            raise ValueError("Output handoff provenance mismatch")
        # Freshly verify terminal Job and owned Pod evidence before mounting.
        job = self.client.get("job", package.binding.job_name)
        if (job or {}).get("metadata", {}).get("uid") != handoff.job_uid:
            raise ValueError("Output Job UID changed")
        if parse_job(job, package.binding, self.client.cluster).state != "succeeded":
            raise ValueError("Output Job no longer has successful terminal evidence")
        owned = list_owned_pods(self.client, handoff.job_uid)
        self.client.verify(self.output)
        if (
            not owned
            or {pod.get("metadata", {}).get("uid") for pod in owned} != set(handoff.pod_uids)
            or any(pod.get("status", {}).get("phase") != "Succeeded" for pod in owned)
        ):
            raise ValueError("Output producer is not the stopped owned Pod set")
        for pod in owned:
            observation = parse_pod(
                {"apiVersion": "v1", "kind": "Pod", **pod},
                package.binding,
                self.client.cluster,
                handoff.job_uid,
            )
            if observation.state != "succeeded":
                raise ValueError("Output producer spec or termination evidence changed")
        result = self.execute("collect", package)
        outputs = []
        total = 0
        for item in result.get("outputs", []):
            content = base64.b64decode(item["content"], validate=True)
            total += len(content)
            if total > MAX_OUTPUT_BYTES or hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError("Collected output checksum or size mismatch")
            outputs.append(CollectedOutput(item["path"], content, item["sha256"]))
        paths = validate_output_paths([item.relative_path for item in outputs])
        if set(paths) != set(package.package["output_paths"]):
            raise ValueError("Collected output declaration mismatch")
        return tuple(outputs)

    def manifest(self, mode, package, *, image=None):
        identity = self.identity(package)
        payload = base64.b64encode(
            canonical_json({"identity": identity, "paths": package.package["output_paths"]})
        ).decode()
        name = "tessera-output-" + mode + "-" + package.operation_id
        spec = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "activeDeadlineSeconds": 60,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "fsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "output",
                    "image": self.image if image is None else image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": [
                        "python",
                        "-m",
                        "tessera_worker.kubernetes_output_pod",
                        mode,
                        payload,
                    ],
                    "securityContext": {
                        "privileged": False,
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "256Mi"},
                    },
                    "volumeMounts": [
                        {"name": "output", "mountPath": "/outputs", "readOnly": mode == "collect"}
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "output",
                    "persistentVolumeClaim": {
                        "claimName": self.output.pvc_name,
                        "readOnly": mode == "collect",
                    },
                }
            ],
        }
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": self.client.cluster.namespace,
                "labels": {"tessera.dev/output-operation": package.operation_id},
                "annotations": {
                    "tessera.dev/output-identity": hashlib.sha256(
                        canonical_json(identity)
                    ).hexdigest()
                },
            },
            "spec": spec,
        }
        return manifest

    def execute(self, mode, package):
        if mode not in {"prepare", "collect"}:
            raise ValueError("Invalid collector mode")
        stage = "intent"

        def mark_stage(value):
            nonlocal stage
            stage = value

        try:
            return self._execute(mode, package, mark_stage)
        except Exception as error:
            self.failures.record(package.operation_id, mode + "." + stage, error)
            raise

    def _execute(self, mode, package, mark_stage):
        identity = self.identity(package)
        manifest = self.manifest(mode, package)
        name = manifest["metadata"]["name"]
        encoded = canonical_json(manifest).decode()
        with sqlite3.connect(self.journal) as db:
            db.execute(
                "INSERT OR IGNORE INTO collectors (name,spec,uid,result) VALUES (?, ?, NULL, NULL)",
                (name, encoded),
            )
            prior = db.execute(
                "SELECT spec,uid,result FROM collectors WHERE name=?", (name,)
            ).fetchone()
        if prior[0] != encoded:
            raise ValueError("Collector intent changed")
        mark_stage("verify_target")
        self.client.verify(self.output)
        if prior[2] is not None:
            if not isinstance(prior[1], str) or not prior[1].strip():
                raise ValueError("Original collector UID is required for a retained result")
            return json.loads(prior[2])
        mark_stage("lookup")
        pod = self.client.get("pod", name)
        if pod is None:
            if prior[1] is not None:
                raise ValueError("Previously owned collector disappeared")
            # On a lost create response, restart looks up this same name/spec.
            mark_stage("create")
            pod = self.client.request("POST", self.client.path("pod"), manifest)
            # Capture only this exact successful create response, before broader
            # spec validation. A later GET must never manufacture original custody.
            mark_stage("capture_uid")
            uid = self.record_creation_uid(pod, manifest, encoded)
        else:
            if prior[1] is None:
                raise ValueError(
                    "Original collector UID was not retained; lookup cannot backfill it"
                )
            uid = prior[1]
        mark_stage("verify_pod")
        self.verify_pod(pod, manifest, uid)
        deadline = time.monotonic() + 20
        while pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
            mark_stage("wait")
            if time.monotonic() >= deadline:
                raise TimeoutError("Output pod is still running")
            time.sleep(0.5)
            self.client.verify(self.output)
            pod = self.client.get("pod", name)
            self.verify_pod(pod, manifest, uid)
        if pod.get("status", {}).get("phase") != "Succeeded":
            raise ValueError("Output pod did not succeed")
        mark_stage("logs")
        result = self.client.request(
            "GET",
            self.client.path("pod", name) + "/log?container=output&limitBytes=24000000",
            max_bytes=24_000_000,
        )
        mark_stage("verify_result")
        self.client.verify(self.output)
        self.verify_pod(self.client.get("pod", name), manifest, uid)
        if not isinstance(result, dict) or result.get("identity") != identity:
            raise ValueError("Output pod returned stale provenance")
        mark_stage("commit_result")
        with sqlite3.connect(self.journal) as db:
            db.execute("UPDATE collectors SET result=? WHERE name=?", (json.dumps(result), name))
        return result

    def record_creation_uid(self, pod, manifest, encoded):
        metadata = pod.get("metadata") if isinstance(pod, dict) else None
        expected = manifest["metadata"]
        if (
            not isinstance(metadata, dict)
            or pod.get("apiVersion") != "v1"
            or pod.get("kind") != "Pod"
            or metadata.get("name") != expected["name"]
            or metadata.get("namespace") != expected["namespace"]
            or not isinstance(metadata.get("labels"), dict)
            or not isinstance(metadata.get("annotations"), dict)
            or any(
                metadata.get("labels", {}).get(key) != value
                for key, value in expected["labels"].items()
            )
            or any(
                metadata.get("annotations", {}).get(key) != value
                for key, value in expected["annotations"].items()
            )
            or not isinstance(metadata.get("uid"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", metadata["uid"])
        ):
            raise ValueError("Collector create response is not attributable to this exact intent")
        uid = metadata["uid"]
        with sqlite3.connect(self.journal) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT spec,uid FROM collectors WHERE name=?", (expected["name"],)
            ).fetchone()
            if row is None or row[0] != encoded or (row[1] is not None and row[1] != uid):
                raise ValueError("Collector create response conflicts with durable intent or UID")
            db.execute(
                "UPDATE collectors SET uid=? WHERE name=? AND uid IS NULL", (uid, expected["name"])
            )
        return uid

    @staticmethod
    def verify_pod(pod, manifest, uid):
        def omitted_false_default(path, key, value):
            return (
                key == "readOnly"
                and value is False
                and (
                    len(path) == 6
                    and path[:2] == ("spec", "containers")
                    and type(path[2]) is int
                    and path[3] == "volumeMounts"
                    and type(path[4]) is int
                    and path[5] == "readOnly"
                    or len(path) == 5
                    and path[:2] == ("spec", "volumes")
                    and type(path[2]) is int
                    and path[3:] == ("persistentVolumeClaim", "readOnly")
                )
            )

        def contains(actual, wanted, path=()):
            if isinstance(wanted, dict):
                return isinstance(actual, dict) and all(
                    (
                        contains(actual[key], value, path + (key,))
                        if key in actual
                        else omitted_false_default(path + (key,), key, value)
                    )
                    for key, value in wanted.items()
                )
            if isinstance(wanted, list):
                return (
                    isinstance(actual, list)
                    and len(actual) == len(wanted)
                    and all(
                        contains(a, b, path + (index,))
                        for index, (a, b) in enumerate(zip(actual, wanted, strict=True))
                    )
                )
            return type(actual) is type(wanted) and actual == wanted

        if (
            not contains(pod, manifest)
            or not pod.get("metadata", {}).get("uid")
            or (uid is not None and pod["metadata"]["uid"] != uid)
            or any(
                pod.get("spec", {}).get(field)
                for field in (
                    "hostNetwork",
                    "hostPID",
                    "hostIPC",
                    "initContainers",
                    "ephemeralContainers",
                )
            )
        ):
            raise ValueError("Collector Pod identity or isolation changed")
