#!/usr/bin/env python3
"""Guarded human admission for an already installed ordinary research bench.

Default is a no-effects plan. --run never provisions Kubernetes or starts a
service: exclusive operator deployment must first resolve the printed gates.
The worker's normal service submits/collects/uploads/manifests. This script only
uses normal human admission and worker lease/package HTTP APIs, then hands off
to the automatic-output verifier. No fixture adapter or manual byte upload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tessera_worker.kubernetes_contracts import KubernetesClusterConfig, cluster_identity_digest
from tessera_worker.kubernetes_remote_service import _network, _NoRedirect
from tessera_worker.service import read_private_credential


class BenchError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def private_path(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise BenchError("runtime reference must be absolute and not a symlink")
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise BenchError("runtime reference must be owner-only")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise BenchError("runtime reference has incorrect type")
    return path


def write_private(path, value):
    path = Path(path)
    private_path(path.parent, directory=True)
    if path.exists():
        private_path(path)
    temporary = path.with_name(path.name + ".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as destination:
        json.dump(value, destination, sort_keys=True)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)


def plan():
    return {
        "state": "planned_no_effects",
        "operator_order": [
            "Exclusive deploy after writers finish; recheck ownership UID/resourceVersion",
            "Import immutable control/worker/collector images; preserve durable PVC journals",
            "Rotate CA/cert: service DNS, localhost and127.0.0.1 SANs; publish public CA only",
            "Keep service DNS accepted and add localhost Host; retain valid internal worker origin",
            "Process-local NODE_EXTRA_CA_CERTS for Electron; no TLS bypass/global trust change",
            "Scoped Pod get/create/delete/list and pods/log get; UID-clean after custody/receipt",
            "Quota3 fits empty serial target, not retained old Job plus two variants/helper",
            "Retained target needs minimum4 Pods/3 Jobs; UID/RV-scope any bounded quota update",
            "Fresh bound network witness within15m; collector_image names exact installed digest",
            "Owner-recover expired worker; retain history/journals; renew active identity timely",
            "UID-check gateway Pod; bounded loopback18443:443 forward; stop only owned PID",
            "Pin final namespace/PVC/deployment/role/quota UIDs+resourceVersions in private input",
            "Admit serial variants; continuously running ordinary service produces outputs",
            "Automatic-output verifier; interruption uses its owned ordinary worker/same workspace",
            "Separately revoke bounded sleep-write attempt; verify late quarantine, not success",
        ],
        "human_effects": "Explicit --run --human-approve creates plans/grants/releases/dispatches",
        "acceptance_not_claimed": [
            "TLS",
            "enrollment",
            "execution",
            "interruption",
            "late outputs",
            "native journey",
        ],
    }


def validate(manifest):
    required = {
        "origin",
        "ca_file",
        "owner_credential_file",
        "worker_credential_file",
        "kubeconfig",
        "context",
        "project_id",
        "snapshot_id",
        "snapshot_digest",
        "worker_principal_id",
        "workload_image",
        "target_file",
        "resource_pins",
        "control_namespace",
        "workload_namespace",
        "installed_image",
        "public_deterministic_snapshot",
    }
    if not required <= manifest.keys():
        raise BenchError("input manifest is missing required runtime references or identities")
    if manifest["public_deterministic_snapshot"] is not True:
        raise BenchError("human must explicitly confirm the input snapshot is public test data")
    origin = urllib.parse.urlsplit(manifest["origin"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise BenchError("origin must be explicit HTTPS without credentials/path")
    if manifest["context"] != "docker-desktop":
        raise BenchError("only explicitly owned docker-desktop target is supported here")
    for key in ("project_id", "snapshot_id", "worker_principal_id"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", manifest[key]):
            raise BenchError("invalid control identity")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest["snapshot_digest"]):
        raise BenchError("invalid snapshot digest")
    for field in ("workload_image", "installed_image"):
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", manifest[field]):
            raise BenchError("installed/workload images must be immutable")
    for key in ("control_namespace", "workload_namespace"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", manifest[key]):
            raise BenchError("invalid namespace")
    if manifest["control_namespace"] == manifest["workload_namespace"]:
        raise BenchError("control and workload namespaces must differ")
    pins = manifest["resource_pins"]
    identities = {(p.get("kind"), p.get("name"), p.get("namespace")) for p in pins}
    if len(identities) != len(pins):
        raise BenchError("duplicate resource pins")
    required_pins = {
        ("namespace", manifest["control_namespace"], None),
        ("namespace", manifest["workload_namespace"], None),
        ("namespace", "kube-system", None),
        ("pvc", "outputs", manifest["workload_namespace"]),
        ("deployment", "tessera-control", manifest["control_namespace"]),
        ("deployment", "tessera-dispatch-worker", manifest["control_namespace"]),
        ("role", "tessera-dispatch-workload", manifest["workload_namespace"]),
        ("resourcequota", "bounded-workloads", manifest["workload_namespace"]),
        ("networkpolicy", "default-deny", manifest["workload_namespace"]),
    }
    if not required_pins <= identities:
        raise BenchError("pin namespaces, output PVC, control/worker deployments, role and quota")
    allowed = {
        "namespace",
        "pvc",
        "deployment",
        "role",
        "resourcequota",
        "service",
        "configmap",
        "pod",
        "networkpolicy",
    }
    for pin in manifest["resource_pins"]:
        if pin.get("kind") not in allowed or not pin.get("uid") or not pin.get("resource_version"):
            raise BenchError("resource pins require supported type, UID and resourceVersion")
        for key in ("name", "namespace"):
            if key in pin and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", pin[key]):
                raise BenchError("invalid concrete resource reference")
    return manifest


def guard_resources(manifest, previous=()):
    kubeconfig = private_path(manifest["kubeconfig"])
    observed = []
    for pin in manifest["resource_pins"]:
        argv = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            manifest["context"],
            "--request-timeout=5s",
        ]
        if pin.get("namespace"):
            argv.extend(["-n", pin["namespace"]])
        result = subprocess.run(
            argv + ["get", pin["kind"], pin["name"], "-o", "json"],
            capture_output=True,
            timeout=8,
            check=False,
        )
        if result.returncode or len(result.stdout) > 1_000_000:
            raise BenchError("named resource inspection failed; no reset permitted")
        resource = json.loads(result.stdout)
        metadata = resource["metadata"]
        definition = digest(
            {
                "spec": resource.get("spec"),
                "rules": resource.get("rules"),
                "data": resource.get("data"),
                "labels": metadata.get("labels"),
                "annotations": metadata.get("annotations"),
                "owners": metadata.get("ownerReferences"),
            }
        )
        prior = next(
            (
                r
                for r in previous
                if r["kind"] == pin["kind"]
                and r["name"] == pin["name"]
                and r.get("namespace") == pin.get("namespace")
            ),
            None,
        )
        if metadata.get("uid") != pin["uid"]:
            raise BenchError("resource UID/resourceVersion changed; reconcile before effects")
        if prior is None and metadata.get("resourceVersion") != pin["resource_version"]:
            raise BenchError("initial resourceVersion differs from explicit operator pin")
        if prior is not None and prior["definition_digest"] != definition:
            raise BenchError("resource declaration changed since initial guarded admission")
        if pin["kind"] == "deployment" and pin["name"] in (
            "tessera-control",
            "tessera-dispatch-worker",
        ):
            images = [c["image"] for c in resource["spec"]["template"]["spec"]["containers"]]
            if images != [manifest["installed_image"]]:
                raise BenchError("control/worker deployment is not the declared installed image")
        observed.append(
            {
                "kind": pin["kind"],
                "name": pin["name"],
                "namespace": pin.get("namespace"),
                "uid": pin["uid"],
                "resource_version": metadata["resourceVersion"],
                "definition_digest": definition,
            }
        )
    return observed


class Admission:
    def __init__(self, manifest, receipt):
        self.manifest, self.path = manifest, receipt
        self.state = (
            json.loads(private_path(receipt).read_text())
            if receipt.exists()
            else {
                "input_digest": digest(manifest),
                "steps": {},
                "attempts": [],
            }
        )
        if self.state["input_digest"] != digest(manifest):
            raise BenchError("changed input is not a retry; retain original receipt")
        self.state["attempts"].append(
            {
                "state": "initialization_pending",
                "at": datetime.now(UTC).isoformat(),
            }
        )
        self.save()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=str(Path(manifest["ca_file"])))
            ),
        )
        self.owner = read_private_credential(private_path(manifest["owner_credential_file"]))
        self.worker = read_private_credential(private_path(manifest["worker_credential_file"]))
        self.state["attempts"][-1]["state"] = "initialized"
        self.save()

    def save(self):
        write_private(self.path, self.state)

    def request(self, path, *, body=None, method=None, name=None, worker=False):
        method = method or ("GET" if body is None else "POST")
        if name in self.state["steps"]:
            old = self.state["steps"][name]
            if old["request_digest"] != digest([method, path, body]):
                raise BenchError("idempotent step input changed")
            return old["response"]
        attempt = {
            "name": name,
            "method": method,
            "path": path,
            "at": datetime.now(UTC).isoformat(),
            "state": "pending",
            "request_digest": digest([method, path, body]),
        }
        self.state["attempts"].append(attempt)
        self.save()  # Lost replies retain the same step/key and immutable body.
        headers = {"Authorization": "Bearer " + (self.worker if worker else self.owner)}
        if body is not None:
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Idempotency-Key": "ordinary-bench-"
                    + digest([self.state["input_digest"], name]),
                }
            )
        request = urllib.request.Request(
            self.manifest["origin"].rstrip("/") + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=8) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise BenchError("API response exceeded bound")
                value = json.loads(raw)
            safe = {
                k: value[k]
                for k in (
                    "id",
                    "revision",
                    "current_plan_digest",
                    "digest",
                    "project_id",
                    "epoch",
                    "status",
                    "execution_granted",
                    "principal_id",
                    "outcome",
                    "operation_id",
                    "current_design_revision",
                    "design",
                )
                if k in value
            }
            attempt["state"] = "acknowledged"
            if name:
                self.state["steps"][name] = {
                    "request_digest": attempt["request_digest"],
                    "response": safe,
                }
            self.save()
            return value if method == "GET" else safe
        except Exception as error:
            attempt.update(state="failed_or_uncertain", error_type=type(error).__name__)
            if isinstance(error, urllib.error.HTTPError):
                attempt["http_status"] = error.code
            self.save()
            raise BenchError(
                "API step uncertain; replay original receipt, never replace authority blindly"
            ) from None

    def admit(self, kind, cluster):
        m, steps = self.manifest, self.state["steps"]
        if kind + "-dispatch" in steps:
            return self.result(kind)
        snapshot = self.request(f"/snapshots/{m['snapshot_id']}")
        if (
            snapshot.get("digest") != m["snapshot_digest"]
            or snapshot.get("project_id") != m["project_id"]
        ):
            raise BenchError("snapshot/project identity changed")
        status = self.request("/worker/status", worker=True)
        if (
            status.get("project_id") != m["project_id"]
            or status.get("worker_principal_id") != m["worker_principal_id"]
            or status.get("lifecycle_state") != "ready"
        ):
            raise BenchError("worker is not registered to declared project")
        limits = self.request(f"/projects/{m['project_id']}/execution-limits")
        if (
            limits.get("currency"),
            limits.get("monetary_limit_cents"),
            limits.get("max_concurrency"),
        ) != ("USD", 0, 1):
            raise BenchError("existing zero-dollar single-concurrency limits are required")
        if kind == "intervention":
            grant = self.request(f"/execution-grants/{steps['baseline-grant']['response']['id']}")
            if grant.get("status") != "reconciled":
                raise BenchError("baseline authority not reconciled; do not mint another grant")
        if kind + "-expiry" not in self.state:
            self.state[kind + "-expiry"] = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
            self.save()
        expiry = self.state[kind + "-expiry"]
        if datetime.fromisoformat(expiry) <= datetime.now(UTC):
            raise BenchError("retained admission expired; explicit reconciliation required")
        releases = self.request(f"/snapshots/{m['snapshot_id']}/releases?limit=200")["releases"]
        eligible = [
            r
            for r in releases
            if r.get("status") == "active"
            and r.get("worker_principal_id") == m["worker_principal_id"]
            and r.get("digest") == m["snapshot_digest"]
            and r.get("classification") == "public"
            and datetime.fromisoformat(r["expires_at"]) > datetime.now(UTC)
        ]
        if eligible:
            if len(eligible) != 1:
                raise BenchError("ambiguous active snapshot release")
            release = eligible[0]
            narrowed = min(
                datetime.fromisoformat(expiry), datetime.fromisoformat(release["expires_at"])
            )
            if kind + "-grant" not in steps:
                expiry = narrowed.isoformat()
                self.state[kind + "-expiry"] = expiry
            self.state[kind + "-existing-release"] = {
                k: release[k] for k in ("id", "expires_at", "digest", "worker_principal_id")
            }
            self.save()
        else:
            self.request(
                f"/snapshots/{m['snapshot_id']}/releases",
                name=kind + "-release",
                body={
                    "expected_digest": m["snapshot_digest"],
                    "worker_principal_id": m["worker_principal_id"],
                    "classification": "public",
                    "permits_worker_transfer": True,
                    "rationale": "Human-approved public deterministic bench acceptance",
                    "expires_at": expiry,
                },
            )
        variants = []
        for label, iterations in (("baseline", 10), ("intervention", 20)):
            task = self.request(
                f"/projects/{m['project_id']}/tasks",
                name=label + "-task",
                body={
                    "title": "Ordinary-service CPU " + label,
                    "purpose": "Public deterministic sum squares",
                    "budget_cents": 0,
                    "max_duration_seconds": 120,
                },
            )
            argv = ["/hello", "--cpu-sum-squares", str(iterations), "/outputs/result.txt"]
            planned = self.request(
                f"/tasks/{task['id']}/plan",
                method="PUT",
                name=label + "-plan",
                body={
                    "expected_revision": task["revision"],
                    "title": "Ordinary-service CPU " + label,
                    "purpose": "Public deterministic sum squares",
                    "budget_cents": 0,
                    "max_duration_seconds": 120,
                    "execution_spec": {
                        "argv": argv,
                        "input_snapshot_digest": m["snapshot_digest"],
                        "pinned_skill_digests": [],
                        "output_paths": ["result.txt"],
                        "profile": "kubernetes-opencode",
                        "network_enabled": False,
                        "max_output_bytes": 1024,
                    },
                },
            )
            approved = self.request(
                f"/tasks/{task['id']}/approve",
                name=label + "-approval",
                body={
                    "expected_revision": planned["revision"],
                    "expected_plan_digest": planned["current_plan_digest"],
                },
            )
            if approved.get("execution_granted") is not False:
                raise BenchError("task approval unexpectedly granted execution")
            variants.append(
                {
                    "kind": label,
                    "name": label,
                    "method_digest": digest(argv),
                    "input_digest": m["snapshot_digest"],
                    "metadata": {"iterations": iterations},
                    "execution_task_id": task["id"],
                    "execution_plan_digest": approved["current_plan_digest"],
                }
            )
        experiment = self.request(
            f"/projects/{m['project_id']}/experiments",
            name="experiment",
            body={
                "goal": "Ordinary-service public CPU comparison",
                "success_criterion": "Retain385/2870; human review pending",
                "protocol": "Fenced custody; serial zero-dollar grants; no scientific claim",
                "variants": variants,
            },
        )
        approval = steps[kind + "-approval"]["response"]
        grant = self.request(
            f"/projects/{m['project_id']}/execution-grants",
            name=kind + "-grant",
            body={
                "task_id": approval["id"],
                "expected_task_revision": approval["revision"],
                "expected_plan_digest": approval["current_plan_digest"],
                "worker_principal_id": m["worker_principal_id"],
                "snapshot_id": m["snapshot_id"],
                "pinned_image": m["workload_image"],
                "expires_at": expiry,
                "max_exposure_cents": 0,
                "currency": "USD",
                "acknowledges_unmeasured_infrastructure": True,
            },
        )
        variant = next(v for v in experiment["design"]["variants"] if v["kind"] == kind)
        admitted = self.request(
            f"/experiment-variants/{variant['id']}/admitted-attempts",
            name=kind + "-admitted",
            body={
                "expected_variant_revision": variant["revision"],
                "expected_design_revision": experiment["current_design_revision"],
                "grant_id": grant["id"],
            },
        )
        self.request("/worker/heartbeat", name=kind + "-heartbeat", worker=True, body={})
        lease = self.request(
            "/worker/leases", name=kind + "-lease", worker=True, body={"grant_id": grant["id"]}
        )
        self.request(
            f"/worker/leases/{lease['id']}/package",
            name=kind + "-package",
            worker=True,
            body={
                "expected_epoch": lease["epoch"],
                "execution_adapter": "kubernetes-job",
                "admitted_attempt_id": admitted["id"],
            },
        )
        current = self.request(f"/experiments/{experiment['id']}")
        attempt = next(a for a in current["admitted_attempts"] if a["id"] == admitted["id"])
        dispatch_body = {
            "expected_revision": attempt["revision"],
            "target_digest": cluster_identity_digest(cluster),
            "profile_version": cluster.profile,
        }
        intent_key = kind + "-dispatch-body"
        if intent_key not in self.state:
            self.state[intent_key] = dispatch_body
            self.save()
        dispatched = self.request(
            f"/experiment-admitted-attempts/{admitted['id']}/dispatch",
            name=kind + "-dispatch",
            body=self.state[intent_key],
        )
        assert dispatched["id"]
        return self.result(kind)

    def result(self, kind):
        expectations = []
        for label, count, value in (("baseline", 10, 385), ("intervention", 20, 2870)):
            if label + "-dispatch" not in self.state["steps"]:
                continue
            content = (f'{{"iterations":{count},"sum_squares":{value}}}\n').encode()
            expectations.append(
                {
                    "operation_id": self.state["steps"][label + "-dispatch"]["response"][
                        "operation_id"
                    ],
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "late": False,
                }
            )
        expectation_file = self.path.with_suffix(".expectations.json")
        write_private(expectation_file, expectations)
        return {
            "state": "admitted_dispatched_not_executed",
            "dispatch_id": self.state["steps"][kind + "-dispatch"]["response"]["id"],
            "experiment_id": self.state["steps"]["experiment"]["response"]["id"],
            "receipt": str(self.path),
            "expectations_file": str(expectation_file),
            "next": "Automatic-output verifier after both variants; human byte release required",
            "verifier_argv": [
                sys.executable,
                str(Path(__file__).with_name("check_ordinary_kubernetes_output_delivery.py")),
                "--control-url",
                self.manifest["origin"],
                "--control-ca-file",
                self.manifest["ca_file"],
                "--owner-credential-file",
                self.manifest["owner_credential_file"],
                "--project-id",
                self.manifest["project_id"],
                "--expectations-file",
                str(expectation_file),
                "--timeout-seconds",
                "180",
            ],
            "verifier_custody_consent": "Add --human-release-fixture-outputs explicitly",
            "interruption": (
                "Use verifier --worker-command-file/--workspace/--interrupt-operation only "
                "with exclusive ownership of an installed ordinary service and same journal"
            ),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--human-approve", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--phase", choices=("baseline", "intervention"), default="baseline")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(plan(), sort_keys=True))
        return 0
    if not args.human_approve or not args.manifest or not args.receipt:
        parser.error("--run requires --human-approve, --manifest and --receipt")
    admission = None
    try:
        manifest = validate(json.loads(private_path(args.manifest).read_text()))
        private_path(args.receipt.parent, directory=True)
        admission = Admission.__new__(Admission)
        admission.__init__(manifest, args.receipt)
        admission.save()
        admission.state["resource_observations"] = guard_resources(
            manifest, admission.state.get("resource_observations", ())
        )
        admission.save()
        target = json.loads(private_path(manifest["target_file"]).read_text())
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", target.get("collector_image", "")):
            raise BenchError("target requires digest-pinned installed collector_image")
        evidence = _network(target["network_evidence"])
        cluster = KubernetesClusterConfig(**target["cluster"], network_none_evidence=evidence)
        if target["collector_image"] != manifest["installed_image"]:
            raise BenchError("collector is not the declared installed image")
        system_pin = next(
            p
            for p in manifest["resource_pins"]
            if p["kind"] == "namespace" and p["name"] == "kube-system"
        )
        policy_pin = next(
            p
            for p in manifest["resource_pins"]
            if p["kind"] == "networkpolicy" and p["name"] == "default-deny"
        )
        if cluster.cluster_uid != system_pin["uid"] or evidence.policy_uid != policy_pin["uid"]:
            raise BenchError("cluster/policy identity differs from pinned network target")
        namespace_pin = next(
            p
            for p in manifest["resource_pins"]
            if p["kind"] == "namespace" and p["name"] == manifest["workload_namespace"]
        )
        pvc_pin = next(
            p for p in manifest["resource_pins"] if p["kind"] == "pvc" and p["name"] == "outputs"
        )
        if (
            cluster.namespace != manifest["workload_namespace"]
            or cluster.namespace_uid != namespace_pin["uid"]
            or target["output"]["pvc_uid"] != pvc_pin["uid"]
        ):
            raise BenchError("target does not match pinned workload namespace/PVC")
        if cluster.profile != "development-minikube-opencode-v1" or not evidence.is_fresh_for(
            cluster.cluster_uid, cluster.namespace_uid
        ):
            raise BenchError("supported target requires fresh bound network evidence")
        identity = cluster_identity_digest(cluster)
        if admission.state.get("target_digest", identity) != identity:
            raise BenchError("target/witness changed; reconcile old identities before rebinding")
        admission.state["target_digest"] = identity
        admission.save()
        print(json.dumps(admission.admit(args.phase, cluster), sort_keys=True))
        return 0
    except Exception as error:
        # Never print exceptions, HTTP bodies, request headers or credential values.
        recorded = False
        if (
            admission is not None
            and hasattr(admission, "state")
            and admission.state.get("input_digest") == digest(manifest)
        ):
            admission.state["attempts"].append(
                {
                    "state": "phase_failed",
                    "phase": args.phase,
                    "at": datetime.now(UTC).isoformat(),
                    "error_type": type(error).__name__,
                }
            )
            try:
                admission.save()
                recorded = True
            except (OSError, BenchError):
                pass  # Never overwrite an unsafe receipt or leak exception contents.
        print(
            json.dumps(
                {
                    "state": "failed_or_partial",
                    "error_type": type(error).__name__,
                    "failure_recorded": recorded,
                    "next": "Inspect private receipt; reconcile same identities before replay",
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
