"""Raw native sensor decoding and bounded, authenticated supervisor RPC.

The native supervisor is a separately deployed trusted service. No shell, exec,
CRI socket discovery or uploaded report is accepted by this client. Native
drivers must obtain these records from the node, not from the fixture stdout.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Protocol

from .capability_activation import canonical, stamp
from .capability_kube_client import APIRequest, CollectionError, decode, encoded
from .capability_witness import exact, number, require, safe_payload

NATIVE_REQUEST = b"tessera-native-request-v1\0"
NATIVE_RESPONSE = b"tessera-native-response-v1\0"
OPERATIONS = frozenset(
    "preflight prepare inventory cleanup ancillary network-fixture policy-convergence "
    "network-exchange quota-fixture cgroup pressure filesystem access termination "
    "watchdog-fixture watchdog-arm watchdog-fault watchdog-observe pod-identity".split()
)


class NativeDriver(Protocol):
    """Deployment-owned Linux/CNI/CRI interface, never a broker callback.

    Each operation has an externally enforced deadline. Creates/kill/faults burn
    durable intent first, are never retried, and are restricted to reviewed
    fixture code in nonce-owned empty storage. `cleanup` uses native journaled
    UIDs, not an inventory submitted by a client. Raw outputs are bounded and
    credential-free. Unsupported drivers raise CollectionError('unavailable').
    """

    def execute(self, operation, request, arguments, *, deadline): ...


class NativeSupervisorRPC:
    def __init__(self, *, target, transport, request_key, response_key, monotonic=time.monotonic):
        require(
            type(request_key) is bytes
            and type(response_key) is bytes
            and min(len(request_key), len(response_key)) >= 32
            and request_key != response_key
        )
        self.target, self.transport, self.clock = target, transport, monotonic
        self._request_key, self._response_key = request_key, response_key
        self.sequence, self.bytes, self.records, self.used = 0, 0, [], set()

    def call(self, request, operation, arguments, *, deadline):
        require(operation in OPERATIONS)
        # Even a lost response consumes the exact operation/arguments locally.
        key = hashlib.sha256(encoded([operation, arguments])).hexdigest()
        require(key not in self.used)
        self.used.add(key)
        self.sequence += 1
        p = self.target.profile
        require(self.sequence <= p.max_calls and self.clock() < deadline)
        binding = {
            "target_digest": p.digest,
            "request_digest": hashlib.sha256(canonical(request)).hexdigest(),
            "challenge": request.challenge,
            "nonce": request.run_nonce,
            "sequence": self.sequence,
            "operation": operation,
            "arguments": arguments,
        }
        payload = safe_payload(binding, secrets=(self._request_key, self._response_key))
        body = encoded(
            {
                "binding": binding,
                "signature": hmac.new(
                    self._request_key, NATIVE_REQUEST + payload, "sha256"
                ).hexdigest(),
            }
        )
        try:
            reply = self.transport.send(
                APIRequest("POST", "/v1/witness/" + request.kind, body),
                deadline=deadline,
                limit=p.max_bytes - self.bytes,
            )
            require(self.clock() < deadline and reply.status == 200)
            self.bytes += len(reply.body)
            require(self.bytes <= p.max_bytes)
            envelope = decode(reply.body)
            exact(envelope, "payload signature")
            raw = safe_payload(envelope["payload"], secrets=(self._request_key, self._response_key))
            require(
                hmac.compare_digest(
                    envelope["signature"],
                    hmac.new(self._response_key, NATIVE_RESPONSE + raw, "sha256").hexdigest(),
                )
            )
            result = envelope["payload"]
            exact(result, "binding state data")
            require(result["binding"] == binding)
            self.records.append(result)
            require(result["state"] == "observed")
            return result["data"]
        except Exception:
            raise CollectionError("unavailable") from None


def counters(text):
    require(type(text) is str and len(text) <= 4096)
    pairs = [line.split() for line in text.splitlines()]
    require(all(len(p) == 2 and re.fullmatch(r"[a-z_]+", p[0]) for p in pairs))
    require(len({p[0] for p in pairs}) == len(pairs))
    require(all(re.fullmatch(r"[0-9]+", p[1]) for p in pairs))
    return {k: number(int(v)) for k, v in pairs}


def integer(text):
    require(type(text) is str and re.fullmatch(r"[0-9]+\n?", text) is not None)
    return number(int(text))


def cri_identity(raw, *, pod_uid, node_uid, image, identity):
    """Pinned native CRI status + node mapping; an API Pod spec is insufficient."""
    exact(raw, "node_uid pod_uid identity status cgroup_id")
    require((raw["node_uid"], raw["pod_uid"], raw["identity"]) == (node_uid, pod_uid, identity))
    status = raw["status"]
    require(status["labels"]["io.kubernetes.pod.uid"] == pod_uid)
    require(status["imageRef"] == image and bool(status["id"]))
    require(re.fullmatch(r"[a-f0-9]{64}", status["id"]) is not None)
    require(type(raw["cgroup_id"]) is str and bool(raw["cgroup_id"]))
    return raw["cgroup_id"]


def cgroup_observation(raw, plan, pin, resource):
    exact(raw, "cri files filesystem_type pvc_uid")
    require(raw["filesystem_type"] == "cgroup2fs")
    cg = cri_identity(
        raw["cri"],
        pod_uid=resource.uid,
        node_uid=pin.node_uid,
        image=plan.activation.image,
        identity=resource.identity,
    )
    require(raw["pvc_uid"] == plan.activation.pvc_uid)
    files = raw["files"]
    exact(
        files,
        "cpu.max cpu.stat memory.max memory.peak memory.events pids.max pids.peak pids.events",
    )
    require(re.fullmatch(r"[0-9]+ [0-9]+\n?", files["cpu.max"]) is not None)
    return {
        "pod_uid": resource.uid,
        "pvc_uid": raw["pvc_uid"],
        "cgroup_version": 2,
        "cgroup_id": cg,
        "cpu_max": files["cpu.max"].strip(),
        "memory_max": integer(files["memory.max"]),
        "pids_max": integer(files["pids.max"]),
        "memory_peak": integer(files["memory.peak"]),
        "pids_peak": integer(files["pids.peak"]),
        "cpu_stat": {"nr_throttled": counters(files["cpu.stat"])["nr_throttled"]},
        "memory_events": {"oom_kill": counters(files["memory.events"])["oom_kill"]},
        "pids_events": {"max": counters(files["pids.events"])["max"]},
    }


def network_observation(raw, request):
    exact(raw, "sample socket http dns flow listener route")
    s, flow, listener = raw["sample"], raw["flow"], raw["listener"]
    # Source and routing equivalence are node-side measurements, not names assigned
    # to a private endpoint and then relabeled 'public' or 'metadata'.
    require(
        raw["route"] == {"class": s["class"], "equivalent_policy_path": True, "fixture_only": True}
    )
    require(
        listener
        == {
            "endpoint_uid": s["endpoint_uid"],
            "challenge": request.challenge,
            "response": request.challenge,
            "healthy": True,
        }
    )
    require(
        flow
        == {
            "source_pod_uid": s["pod_uid"],
            "source_identity": s["identity"],
            "destination": s["address"],
            "port": s["port"],
            "protocol": s["protocol"],
            "endpoint_uid": s["endpoint_uid"],
            "policy_uid": s["policy_uid"],
            "verdict": s["cni"],
            "sent": s["packets_sent"],
            "dropped": s["packets_dropped"],
            "challenge": request.challenge,
        }
    )
    require(
        raw["socket"] == {"connect": s["connect"], "proxy_environment": [], "proxy_used": False}
    )
    require(
        raw["http"]
        == {"status": s["http_status"], "body": s["response"] if s["protocol"] == "HTTP" else None}
    )
    require(
        raw["dns"]
        == {"rcode": s["dns_rcode"], "answer": s["response"] if s["protocol"] == "DNS" else None}
    )
    return s


def filesystem_observation(raw, plan):
    exact(raw, "resource mount quota_before quota_after syscall")
    name, mount = raw["resource"], raw["mount"]
    require(name in {"output-bytes", "output-inodes", "ephemeral-bytes"})
    exact(mount, "id project_id pvc_uid filesystem options dedicated empty_at_prepare")
    require(mount["filesystem"] in {"xfs", "ext4"})
    require(
        "prjquota" in mount["options"]
        and mount["dedicated"] is True
        and mount["empty_at_prepare"] is True
    )
    require(mount["pvc_uid"] == (None if name == "ephemeral-bytes" else plan.activation.pvc_uid))
    before, after = raw["quota_before"], raw["quota_after"]
    for value in (before, after):
        exact(value, "bytes_hard bytes_used inodes_hard inodes_used project_id enforcement")
        require(value["enforcement"] == "hard" and value["project_id"] == mount["project_id"])
        for key in ("bytes_hard", "bytes_used", "inodes_hard", "inodes_used", "project_id"):
            number(value[key])
    # A failed one-unit write must not change *either* counter; comparing only the
    # tested dimension hides byte/inode underreporting and allocation side effects.
    require(before == after)
    exact(raw["syscall"], "attempted result errno")
    require(raw["syscall"]["result"] == -1)
    unit = "inodes" if name == "output-inodes" else "bytes"
    return {
        "resource": name,
        "mechanism": "filesystem-project-quota",
        "mount_id": mount["id"],
        "project_id": mount["project_id"],
        "mount_pvc_uid": mount["pvc_uid"],
        "hard_limit": before[unit + "_hard"],
        "usage_before": before[unit + "_used"],
        "usage_after": after[unit + "_used"],
        "attempted": raw["syscall"]["attempted"],
        "errno": raw["syscall"]["errno"],
    }


def watchdog_observation(raw, request, plan, pin, resource):
    exact(raw, "case cri clock heartbeat kill process writes")
    c, clock = raw["case"], raw["clock"]
    cri_identity(
        raw["cri"],
        pod_uid=resource.uid,
        node_uid=pin.node_uid,
        image=plan.activation.image,
        identity=resource.identity,
    )
    exact(clock, "boot_id heartbeat_ns trigger_ns kill_ns confirmed_ns quiet_ns")
    require(clock["boot_id"] == pin.boot_id)
    ticks = [
        number(clock[k])
        for k in ("heartbeat_ns", "trigger_ns", "kill_ns", "confirmed_ns", "quiet_ns")
    ]
    require(ticks == sorted(ticks))
    hb, trigger, kill, confirmed, quiet = ticks
    w = plan.activation.watchdog
    require(quiet - confirmed >= w.heartbeat_seconds * 10**9)
    require(confirmed - trigger <= (w.lease_seconds + w.kill_grace_seconds) * 10**9)
    if c["trigger"] == "broker-suspended":
        require(kill - hb >= w.lease_seconds * 10**9)
    require(
        raw["heartbeat"] == {"challenge": request.challenge, "sequence": 1, "acknowledged": True}
    )
    require(
        raw["kill"]
        == {
            "requests": 1,
            "container_id": raw["cri"]["status"]["id"],
            "credential_state": "revoked",
        }
    )
    require(raw["process"] == {"exit_code": c["process_exit"], "cgroup.events": "populated 0\n"})
    require(
        raw["writes"]
        == {
            "before": c["write_count_before"],
            "after": c["write_count_after"],
            "source": "independent-filesystem-counter",
        }
    )
    return c


def validate_native(report, plan):
    """Recompute normalized enforcement observations at collector AND use time."""
    from .capability_witness_target import TargetProfile

    native = report.native
    exact(native, "target_digest profile records api")
    profile = TargetProfile.model_validate_json(encoded(native["profile"]))
    require(native["target_digest"] == report.witness.target_digest)
    require(
        profile.digest == native["target_digest"]
        and profile.plan_digest == plan.digest
        and profile.cluster_digest == plan.activation.cluster_digest
        and profile.image == plan.activation.image
        and (profile.node_uid, profile.boot_id) == (report.witness.node_uid, report.witness.boot_id)
    )
    records = native["records"]
    require(type(records) is list and 0 < len(records) <= 128)
    digest = hashlib.sha256(canonical(report.request)).hexdigest()
    for i, record in enumerate(records, 1):
        exact(record, "binding state data")
        b = record["binding"]
        exact(b, "target_digest request_digest challenge nonce sequence operation arguments")
        require(
            b["target_digest"] == native["target_digest"]
            and b["request_digest"] == digest
            and b["challenge"] == report.request.challenge
            and b["nonce"] == report.request.run_nonce
            and b["sequence"] == i
            and b["operation"] in OPERATIONS
            and record["state"] == "observed"
        )
    by_op = {}
    for r in records:
        by_op.setdefault(r["binding"]["operation"], []).append(r["data"])
    require(len(by_op.get("preflight", [])) == len(by_op.get("prepare", [])) == 1)
    preflight = by_op["preflight"][0]
    exact(
        preflight,
        "node_uid boot_id image binary_sha256 cri cgroup filesystem cni "
        "fixture_empty supervisor_process",
    )
    require(
        preflight["node_uid"] == profile.node_uid
        and preflight["boot_id"] == profile.boot_id
        and preflight["image"] == profile.image
        and preflight["binary_sha256"] == profile.fixture_binary_sha256
        and preflight["fixture_empty"] is True
        and preflight["cri"] == profile.cri_driver
        and preflight["supervisor_process"] == "independent-external-service"
    )
    require(by_op["prepare"] == [{"ready": True, "resources": []}])
    require(len(by_op.get("cleanup", [])) == 1)
    remote = by_op["cleanup"][0]
    exact(remote, "resources cleanup")
    if report.request.kind != "admission":
        require(remote["resources"] == [r.model_dump(mode="json") for r in report.resources])
        require(remote["cleanup"] == [c.model_dump(mode="json") for c in report.cleanup])
    require(type(native["api"]) is list and native["api"])
    require(len(native["api"]) + len(records) <= profile.max_calls)
    for record in native["api"]:
        exact(record, "request response status audit_id observed_at")
        require(
            record["audit_id"]
            and stamp(report.started_at)
            <= stamp(record["observed_at"])
            <= stamp(report.completed_at)
        )
    require(len({r["audit_id"] for r in native["api"]}) == len(native["api"]))
    resources = {r.uid: r for r in report.resources}
    if report.request.kind == "cni":
        require(preflight["cni"] == profile.cni_driver == "native-policy-flow-v1")
        require(
            by_op.get("policy-convergence")
            == [{k: v for k, v in report.observations.items() if k != "samples"}]
        )
        samples = [
            network_observation(r, report.request) for r in by_op.get("network-exchange", [])
        ]
        require(samples == report.observations["samples"])
        for s in samples:
            source = profile.probe_service_accounts[s["class"]][0]
            endpoint = profile.endpoint_service_accounts[s["class"]][0]
            require(s["identity"] == f"system:serviceaccount:{plan.activation.namespace}:{source}")
            r = resources[s["endpoint_uid"]]
            if r.kind == "Pod":
                require(
                    r.identity == f"system:serviceaccount:{report.witness.namespace}:{endpoint}"
                )
    elif report.request.kind == "quota":
        require(
            preflight["cgroup"] == "cgroup2fs"
            and preflight["filesystem"] == profile.storage_driver == "linux-project-quota-v1"
        )
        o = report.observations
        snapshots = [
            cgroup_observation(r, plan, report.witness, resources[o["pod_uid"]])
            for r in by_op.get("cgroup", [])
        ]
        require(len(snapshots) == 2)
        for index, side in enumerate(("before", "after")):
            snap = snapshots[index]
            for k in ("cpu_stat", "memory_events", "pids_events"):
                require(snap[k] == o[k + "_" + side])
            for k in o["limits_before"]:
                require(snap[k] == o[k])
        require(
            snapshots[1]["memory_peak"] == o["memory_peak"]
            and snapshots[1]["pids_peak"] == o["pids_peak"]
        )
        require(len(by_op.get("filesystem", [])) == 1)
        require(
            [filesystem_observation(r, plan) for r in by_op["filesystem"][0]["items"]]
            == o["filesystems"]
        )
    elif report.request.kind == "watchdog":
        cases = [
            watchdog_observation(
                r, report.request, plan, report.witness, resources[r["case"]["pod_uid"]]
            )
            for r in by_op.get("watchdog-observe", [])
        ]
        require(cases == report.observations["cases"])
    else:
        # Raw API records must agree, rather than being an unrelated successful
        # request alongside claimed admission observations.
        o = report.observations
        for dry in o["dry_runs"].values():
            matching = [
                r
                for r in native["api"]
                if r["request"]["method"] == "POST"
                and "dryRun=All&fieldValidation=Strict" in r["request"]["path"]
                and r["request"]["body"] == dry["request"]
            ]
            require(
                len(matching) == 1
                and matching[0]["response"] == dry["response"]
                and matching[0]["status"] == dry["status"]
                and matching[0]["audit_id"] == dry["audit_id"]
            )
        for actual in o["actual"]:
            matching = [
                r
                for r in native["api"]
                if r["request"]["method"] == "POST"
                and r["request"]["path"].endswith("?fieldValidation=Strict")
                and r["request"]["body"] == actual["request"]
            ]
            require(
                len(matching) == 1
                and matching[0]["response"] == actual["response"]
                and matching[0]["audit_id"] == actual["audit_id"]
            )
            require(
                any(
                    r["request"]["method"] == "GET" and r["response"] == actual["persisted"]
                    for r in native["api"]
                )
            )
