"""Explicit deterministic TEST witnesses. Never loaded by live configuration.

Fixture observations intentionally model a narrow supported target; they are not
claims about Docker Desktop, the local kernel, a cluster or a running daemon.
"""

from __future__ import annotations

import copy
from datetime import timedelta

from tessera_control.store import canonical_digest

from .capability_activation import stamp
from .capability_inspection import negative_jobs
from .capability_witness import (
    Cleanup,
    Resource,
    WitnessPin,
    WitnessReport,
    challenge_for,
    validate_report,
)
from .capability_witness_transport import partition


def fake_report(plan, request, pin, probes):
    m, q = plan.activation, plan.activation.quota
    start = stamp(request.started_at)
    finish = start + timedelta(seconds=4) if pin.kind == "watchdog" else start
    resources = []

    def resource(name, kind="Pod", observer=False):
        r = Resource(
            kind=kind,
            name="fake-" + name,
            uid="fake-" + name,
            namespace=pin.namespace if observer else m.namespace,
            namespace_uid=pin.namespace_uid if observer else m.namespace_uid,
            run_nonce=m.nonce,
            resource_version="1",
            created_at=start.isoformat(),
            node_uid=pin.node_uid if kind == "Pod" else None,
            identity=(
                f"system:serviceaccount:{pin.namespace if observer else m.namespace}:fake-{name}"
                if kind == "Pod"
                else None
            ),
        )
        resources.append(r)
        return r.uid

    if pin.kind == "cni":
        o = {
            "policy_uid": "fixture-NetworkPolicy",
            "policy_resource_version": "1",
            "converged_at": start.isoformat(),
            "samples": [],
        }
        for index, category in enumerate(
            (
                "allowed",
                "allowed-dns",
                "public",
                "private",
                "metadata",
                "control",
                "dns-bypass",
                "proxy",
            )
        ):
            allowed = category in {"allowed", "allowed-dns"}
            dns = category in {"allowed-dns", "dns-bypass"}
            o["samples"].append(
                {
                    "class": category,
                    "pod_uid": resource(category),
                    "endpoint_uid": resource(category + "-endpoint", observer=True),
                    "identity": f"system:serviceaccount:{m.namespace}:fake-{category}",
                    "address": f"192.0.2.{index + 1}",
                    "port": 53 if dns else 8080,
                    "protocol": "DNS" if dns else "HTTP",
                    "request": request.challenge,
                    "response": request.challenge if allowed else None,
                    "connect": "connected" if allowed else "timeout",
                    "http_status": 200 if allowed and not dns else None,
                    "dns_rcode": 0 if allowed and dns else None,
                    "cni": "FORWARDED" if allowed else "DROP",
                    "policy_uid": o["policy_uid"],
                    "packets_sent": 1,
                    "packets_dropped": 0 if allowed else 1,
                    "proxy_environment": [],
                    "proxy_used": False,
                    "observed_at": start.isoformat(),
                    "endpoint_challenge": request.challenge,
                }
            )
    elif pin.kind == "quota":
        pod = resource("quota")
        o = {
            "pod_uid": pod,
            "pvc_uid": m.pvc_uid,
            "cgroup_version": 2,
            "cgroup_id": "fake-cgroup-id",
            "cpu_max": f"{q.cpu_millicores * 100} 100000",
            "memory_max": q.memory_bytes,
            "pids_max": plan.pids,
            "cpu_stat_before": {"nr_throttled": 0},
            "cpu_stat_after": {"nr_throttled": 1},
            "memory_events_before": {"oom_kill": 0},
            "memory_events_after": {"oom_kill": 1},
            "pids_events_before": {"max": 0},
            "pids_events_after": {"max": 1},
            "memory_peak": q.memory_bytes,
            "pids_peak": plan.pids,
            "allocation_bytes": q.memory_bytes + 1,
            "fork_attempts": plan.pids + 1,
            "termination": {"reason": "OOMKilled", "exit_code": 137, "restart_count": 0},
            "events": [{"pod_uid": pod, "reason": "OOMKilled"}],
            "filesystems": [],
            "outside_workspace": {
                "root_write": "EROFS",
                "host_path": "ENOENT",
                "control_pvc": "ENOENT",
                "parent_escape": "EACCES",
                "symlink_escape": "EACCES",
                "ambient_token": "ENOENT",
            },
        }
        for name, limit in (
            ("output-bytes", q.output_bytes),
            ("output-inodes", q.output_inodes),
            ("ephemeral-bytes", q.ephemeral_bytes),
        ):
            o["filesystems"].append(
                {
                    "resource": name,
                    "mechanism": "filesystem-project-quota",
                    "mount_id": "fake-mount",
                    "project_id": 1,
                    "hard_limit": limit,
                    "usage_before": limit,
                    "usage_after": limit,
                    "attempted": 1,
                    "errno": "EDQUOT",
                    "mount_pvc_uid": None if name.startswith("ephemeral") else m.pvc_uid,
                }
            )
        o["limits_before"] = {
            k: o[k]
            for k in (
                "pod_uid",
                "pvc_uid",
                "cgroup_id",
                "cgroup_version",
                "cpu_max",
                "memory_max",
                "pids_max",
            )
        }
    elif pin.kind == "watchdog":
        o = {"watchdog_identity": m.watchdog.identity, "clock_boot_id": pin.boot_id, "cases": []}
        for trigger in ("broker-suspended", "revoked"):
            kill = start + timedelta(seconds=m.watchdog.lease_seconds)
            o["cases"].append(
                {
                    "trigger": trigger,
                    "pod_uid": resource(trigger),
                    "heartbeat_sequence": 1,
                    "heartbeat_challenge": request.challenge,
                    "heartbeat_at": start.isoformat(),
                    "trigger_at": start.isoformat(),
                    "kill_at": kill.isoformat(),
                    "confirmed_at": kill.isoformat(),
                    "quiet_until": finish.isoformat(),
                    "process_exit": 137,
                    "cgroup_populated": 0,
                    "write_count_before": 1,
                    "write_count_after": 1,
                    "credential_state": "revoked",
                    "kill_requests": 1,
                }
            )
    else:
        o = {"policies": [], "bindings": [], "dry_runs": {}, "actual": []}
        for obj in plan.objects():
            if obj["kind"].startswith("ValidatingAdmission"):
                obj = copy.deepcopy(obj)
                obj["metadata"].update(
                    uid="fake-" + obj["metadata"]["name"], resourceVersion="1", generation=1
                )
                policy = obj["kind"] == "ValidatingAdmissionPolicy"
                if policy:
                    obj["status"] = {
                        "observedGeneration": 1,
                        "typeChecking": {"expressionWarnings": []},
                    }
                o["policies" if policy else "bindings"].append(obj)
        for kind in ("Job", "Pod"):
            for key, job in {"positive": plan.job(), **negative_jobs(plan)}.items():
                obj = (
                    job
                    if kind == "Job"
                    else {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": plan.metadata(plan.name),
                        "spec": job["spec"]["template"]["spec"],
                    }
                )
                from .capability_inspection import RawEvidenceInspector

                positive = key == "positive"
                response = (
                    copy.deepcopy(obj)
                    if positive
                    else RawEvidenceInspector.expected_response(plan, key, policy_uid="unused")
                )
                o["dry_runs"][f"{kind}:{key}"] = {
                    "request": obj,
                    "response": response,
                    "status": 201 if positive else 403,
                    "audit_id": f"fake-{kind}-{key}",
                    "field_validation": "Strict",
                    "dry_run": "All",
                }
                if positive:
                    uid = resource("admission-" + kind.lower(), kind=kind)
                    resources[-1] = resources[-1].model_copy(update={"name": plan.name})
                    if kind == "Pod":
                        resources[-1] = resources[-1].model_copy(
                            update={
                                "identity": f"system:serviceaccount:{m.namespace}:"
                                + m.service_account,
                            }
                        )
                    persisted = copy.deepcopy(obj)
                    persisted["metadata"].update(uid=uid, resourceVersion="1", generation=1)
                    o["actual"].append(
                        {
                            "request": obj,
                            "response": persisted,
                            "persisted": persisted,
                            "status": 201,
                            "audit_id": "fake-actual-" + kind,
                            "field_validation": "Strict",
                        }
                    )
    return WitnessReport(
        source="fake",
        request=request,
        request_digest=canonical_digest(request.model_dump(mode="json")),
        witness=pin,
        started_at=start.isoformat(),
        completed_at=finish.isoformat(),
        monotonic_elapsed_ms=int((finish - start).total_seconds() * 1000),
        state="observed",
        resources=tuple(resources),
        observations=o,
        probes=copy.deepcopy(probes),
        cleanup=tuple(
            Cleanup(
                resource=r,
                preconditions={"uid": r.uid, "resourceVersion": r.resource_version},
                delete_status=200,
                readback={"kind": "Status", "code": 404, "reason": "NotFound"},
                observed_at=finish.isoformat(),
            )
            for r in resources
        ),
    )


class FakeRuntimeWitnesses:
    """Offline-only collector seam; accepts an explicit raw fixture, never defaults."""

    def __init__(self, snapshot, *, now):
        self.snapshot, self.now = snapshot, now
        self.records = {}

    def observe(self, plan, requests, *, deadline):
        # Fake clocks let the watchdog exercise a complete expiry/quiet window
        # without sleeping or running/killing any process in default pytest.
        result = {}
        for kind, inputs in partition(requests).items():
            pin = WitnessPin(
                kind=kind,
                identity=plan.activation.watchdog.identity
                if kind == "watchdog"
                else "fake-" + kind,
                namespace="fake-observers",
                namespace_uid="fake-observers-uid",
                node_uid="fake-node",
                boot_id="fake-boot",
            )
            start = self.now() - timedelta(seconds=4) if kind == "watchdog" else self.now()
            request = challenge_for(
                plan,
                kind,
                challenge="d" * 64,
                started_at=start.isoformat(),
                deadline=(self.now() + timedelta(seconds=1)).isoformat(),
                requests=inputs,
            )
            probes = {key: copy.deepcopy(self.snapshot["probes"][key]) for key in inputs}
            report = fake_report(plan, request, pin, probes)
            validate_report(report, request, pin, plan, now=self.now(), fake=True)
            self.records[kind] = report.model_dump(mode="json")
            result.update(report.probes)
        return result
