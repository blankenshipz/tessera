"""Serialized synthetic API/native records. No cluster, sockets or live processes."""

import copy
import errno
import json
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_capability_activation import NOW, candidate  # noqa: F401
from test_capability_collector import collection  # noqa: F401
from test_capability_runtime_witnesses import pin_for

from tessera_worker.capability_activation import canonical
from tessera_worker.capability_collector import CollectorJournal, resource_path
from tessera_worker.capability_collector_cli import main
from tessera_worker.capability_inspection import probe_requests
from tessera_worker.capability_kube_client import APIResponse, CollectionError, decode, encoded
from tessera_worker.capability_witness import WitnessReport, challenge_for, validate_report
from tessera_worker.capability_witness_fake import fake_report
from tessera_worker.capability_witness_live import DockerDesktopNodeSensors
from tessera_worker.capability_witness_native import NativeSupervisorRPC
from tessera_worker.capability_witness_probes import (
    AdmissionWitness,
    CNIWitness,
    ExternalWatchdogWitness,
    QuotaWitness,
)
from tessera_worker.capability_witness_supervisor import (
    LinuxCgroupFiles,
    LinuxQuotaSentinel,
    MonotonicFixtureWatchdog,
    NativeSupervisorService,
)
from tessera_worker.capability_witness_target import LiveWitnessTarget, TargetProfile
from tessera_worker.capability_witness_transport import partition

PROGRAMS = {
    "cni": CNIWitness,
    "quota": QuotaWitness,
    "watchdog": ExternalWatchdogWitness,
    "admission": AdmissionWitness,
}


def cri(resource, h):
    return {
        "node_uid": h.pin.node_uid,
        "pod_uid": resource.uid,
        "identity": resource.identity,
        "status": {
            "id": "a" * 64,
            "imageRef": h.plan.activation.image,
            "labels": {"io.kubernetes.pod.uid": resource.uid},
        },
        "cgroup_id": "fake-cgroup-id",
    }


def network_raw(s, request):
    return {
        "sample": s,
        "socket": {"connect": s["connect"], "proxy_environment": [], "proxy_used": False},
        "http": {
            "status": s["http_status"],
            "body": s["response"] if s["protocol"] == "HTTP" else None,
        },
        "dns": {
            "rcode": s["dns_rcode"],
            "answer": s["response"] if s["protocol"] == "DNS" else None,
        },
        "flow": {
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
        },
        "listener": {
            "endpoint_uid": s["endpoint_uid"],
            "challenge": request.challenge,
            "response": request.challenge,
            "healthy": True,
        },
        "route": {"class": s["class"], "equivalent_policy_path": True, "fixture_only": True},
    }


class FakeNativeDriver:
    def __init__(self, h):
        self.h, self.calls, self.fault = h, [], None

    def execute(self, operation, request, arguments, *, deadline):
        self.calls.append((operation, arguments))
        h, o = self.h, self.h.report.observations
        p = h.target.profile
        if operation == "preflight":
            value = {
                "node_uid": p.node_uid,
                "boot_id": p.boot_id,
                "image": p.image,
                "binary_sha256": p.fixture_binary_sha256,
                "cri": p.cri_driver,
                "cgroup": "cgroup2fs",
                "filesystem": p.storage_driver,
                "cni": p.cni_driver,
                "fixture_empty": True,
                "supervisor_process": "independent-external-service",
            }
        elif operation == "prepare":
            value = {"ready": True, "resources": []}
        elif operation == "ancillary":
            value = h.report.probes
        elif operation == "cleanup":
            resources = h.report.resources if h.pin.kind != "admission" else ()
            value = {
                "resources": [r.model_dump(mode="json") for r in resources],
                "cleanup": [
                    c.model_dump(mode="json") for c in h.report.cleanup if c.resource in resources
                ],
            }
        elif operation == "network-fixture":
            category = arguments["category"]
            value = {
                "fixture": category,
                "resources": [
                    r.model_dump(mode="json")
                    for r in h.report.resources
                    if r.uid in {"fake-" + category, "fake-" + category + "-endpoint"}
                ],
            }
        elif operation == "policy-convergence":
            value = {k: v for k, v in o.items() if k != "samples"}
        elif operation == "network-exchange":
            value = network_raw(
                next(s for s in o["samples"] if s["class"] == arguments["fixture"]), request
            )
        elif operation in {"quota-fixture", "watchdog-fixture"}:
            fixture = arguments.get("trigger", "quota")
            value = {
                "fixture": fixture,
                "resources": [
                    r.model_dump(mode="json")
                    for r in h.report.resources
                    if r.uid == "fake-" + fixture
                ],
            }
        elif operation == "cgroup":
            resource = h.report.resources[0]
            side = "before" if arguments["snapshot"] == 1 else "after"
            value = {
                "cri": cri(resource, h),
                "filesystem_type": "cgroup2fs",
                "pvc_uid": o["pvc_uid"],
                "files": {
                    "cpu.max": o["cpu_max"] + "\n",
                    "memory.max": str(o["memory_max"]),
                    "pids.max": str(o["pids_max"]),
                    "memory.peak": str(o["memory_peak"]),
                    "pids.peak": str(o["pids_peak"]),
                    **{
                        file: "".join(f"{k} {v}\n" for k, v in o[field + "_" + side].items())
                        for file, field in (
                            ("cpu.stat", "cpu_stat"),
                            ("memory.events", "memory_events"),
                            ("pids.events", "pids_events"),
                        )
                    },
                },
            }
        elif operation == "filesystem":
            items = []
            for item in o["filesystems"]:
                unit = "inodes" if item["resource"] == "output-inodes" else "bytes"
                quota = {
                    "bytes_hard": 100,
                    "bytes_used": 1,
                    "inodes_hard": 100,
                    "inodes_used": 1,
                    "project_id": 1,
                    "enforcement": "hard",
                    unit + "_hard": item["hard_limit"],
                    unit + "_used": item["usage_before"],
                }
                items.append(
                    {
                        "resource": item["resource"],
                        "mount": {
                            "id": item["mount_id"],
                            "project_id": 1,
                            "pvc_uid": item["mount_pvc_uid"],
                            "filesystem": "xfs",
                            "options": ["prjquota"],
                            "dedicated": True,
                            "empty_at_prepare": True,
                        },
                        "quota_before": quota,
                        "quota_after": copy.deepcopy(quota),
                        "syscall": {"attempted": 1, "result": -1, "errno": "EDQUOT"},
                    }
                )
            value = {"items": items}
        elif operation == "access":
            value = o["outside_workspace"]
        elif operation == "pressure":
            value = {k: o[k] for k in ("allocation_bytes", "fork_attempts")}
        elif operation == "termination":
            value = {k: o[k] for k in ("termination", "events")}
        elif operation == "watchdog-arm":
            value = {"challenge": request.challenge, "sequence": 1, "armed": True}
        elif operation == "watchdog-fault":
            value = {"injected": arguments["trigger"]}
        elif operation == "watchdog-observe":
            c = next(c for c in o["cases"] if c["trigger"] == arguments["fixture"])
            resource = next(r for r in h.report.resources if r.uid == c["pod_uid"])
            value = {
                "case": c,
                "cri": cri(resource, h),
                "clock": {
                    "boot_id": h.pin.boot_id,
                    "heartbeat_ns": 0,
                    "trigger_ns": 0,
                    "kill_ns": h.plan.activation.watchdog.lease_seconds * 10**9,
                    "confirmed_ns": h.plan.activation.watchdog.lease_seconds * 10**9,
                    "quiet_ns": 4 * 10**9,
                },
                "heartbeat": {"challenge": request.challenge, "sequence": 1, "acknowledged": True},
                "kill": {"requests": 1, "container_id": "a" * 64, "credential_state": "revoked"},
                "process": {"exit_code": 137, "cgroup.events": "populated 0\n"},
                "writes": {"before": 1, "after": 1, "source": "independent-filesystem-counter"},
            }
        elif operation == "pod-identity":
            value = {
                "uid": arguments["uid"],
                "node_uid": h.pin.node_uid,
                "identity": f"system:serviceaccount:{h.plan.activation.namespace}:"
                + h.plan.activation.service_account,
            }
        else:
            raise AssertionError(operation)
        value = json.loads(encoded(value))  # serialized raw observation boundary
        if self.fault:
            self.fault(operation, value)
        return value


class FakeAPI:
    def __init__(self, h):
        self.h, self.calls, self.persisted, self.fault = h, [], {}, None

    def send(self, request, *, deadline, limit):
        self.calls.append(request)
        h, p, m = self.h, self.h.target.profile, self.h.plan.activation
        path = request.path.split("?")[0]
        status = 200
        if path.endswith("selfsubjectreviews"):
            value = {"status": {"userInfo": {"username": h.pin.identity}}}
        elif path == "/version":
            value = {"gitVersion": p.kubernetes_version}
        elif path == "/api/v1/nodes/" + p.node_name:
            value = {
                "metadata": {"uid": p.node_uid},
                "status": {
                    "nodeInfo": {
                        "bootID": p.boot_id,
                        "operatingSystem": "linux",
                        "containerRuntimeVersion": "containerd://1.7.0",
                        "kubeletVersion": p.kubernetes_version,
                    }
                },
            }
        elif "/serviceaccounts/" in path:
            uid = m.service_account_uid
            for accounts in (p.probe_service_accounts, p.endpoint_service_accounts):
                for name, candidate_uid in accounts.values():
                    if path.endswith("/" + name):
                        uid = candidate_uid
            value = {
                "metadata": {"uid": uid},
                "automountServiceAccountToken": False,
            }
        elif path in {
            "/api/v1/namespaces/" + ns for ns in ("kube-system", m.namespace, h.pin.namespace)
        }:
            name = path.rsplit("/", 1)[1]
            uid = {
                "kube-system": "cluster-uid",
                m.namespace: m.namespace_uid,
                h.pin.namespace: h.pin.namespace_uid,
            }[name]
            value = {"kind": "Namespace", "metadata": {"name": name, "uid": uid}}
        elif "validatingadmission" in path:
            objects = h.report.observations["policies"] + h.report.observations["bindings"]
            value = next(
                o for o in objects if resource_path(o["kind"], None, o["metadata"]["name"]) == path
            )
        elif "dryRun=All" in request.path:
            body = decode(request.body)
            record = next(
                r for r in h.report.observations["dry_runs"].values() if r["request"] == body
            )
            status, value = record["status"], record["response"]
        elif request.method == "POST":
            body = decode(request.body)
            record = next(r for r in h.report.observations["actual"] if r["request"] == body)
            status, value = 201, copy.deepcopy(record["response"])
            value["metadata"]["creationTimestamp"] = NOW.isoformat()
            self.persisted[path + "/" + value["metadata"]["name"]] = value
        elif request.method == "DELETE":
            obj = self.persisted[path]
            assert decode(request.body)["preconditions"] == {
                "uid": obj["metadata"]["uid"],
                "resourceVersion": obj["metadata"]["resourceVersion"],
            }
            del self.persisted[path]
            value = {"kind": "Status", "code": 200}
        elif path in self.persisted:
            value = self.persisted[path]
        else:
            status, value = 404, {"kind": "Status", "code": 404, "reason": "NotFound"}
        value = copy.deepcopy(value)
        if self.fault:
            self.fault(request, value)
        return APIResponse(status, encoded(value), f"api-{len(self.calls)}")


@pytest.fixture
def live(collection, tmp_path):  # noqa: F811
    journals = []

    def build(kind):
        plan = collection.plan
        profile = TargetProfile(
            platform="docker-desktop-kubernetes",
            context="docker-desktop",
            cluster_digest=plan.activation.cluster_digest,
            plan_digest=plan.digest,
            node_name="docker-desktop",
            node_uid="node-uid",
            boot_id="boot-uid",
            kubernetes_version="v1.30.0",
            admission_profile="exact-no-unreviewed-defaults-v1",
            fixture_protocol="nonce-owned-disposable-v1",
            image=plan.activation.image,
            fixture_binary_sha256="1" * 64,
            probe_service_accounts={
                c: ("fake-" + c, "source-" + c)
                for c in (
                    "allowed",
                    "allowed-dns",
                    "public",
                    "private",
                    "metadata",
                    "control",
                    "dns-bypass",
                    "proxy",
                )
            },
            endpoint_service_accounts={
                c: ("fake-" + c + "-endpoint", "endpoint-" + c)
                for c in (
                    "allowed",
                    "allowed-dns",
                    "public",
                    "private",
                    "metadata",
                    "control",
                    "dns-bypass",
                    "proxy",
                )
            },
            cni_driver="native-policy-flow-v1",
            storage_driver="linux-project-quota-v1",
            cri_driver="containerd-cri-v1",
            max_calls=128,
            max_bytes=2 * 1024 * 1024,
            max_seconds=5,
            max_resources=32,
            retries=0,
        )
        pin = pin_for(plan, kind).model_copy(update={"target_digest": profile.digest})
        binding = collection.clients[0].binding.model_dump(mode="json") | {
            "context": "docker-desktop",
            "identity": pin.identity,
            "namespace": pin.namespace,
            "namespace_uid": pin.namespace_uid,
        }
        ref = {"binding": binding, "kubeconfig": "/private/unused-kubeconfig"}
        target = LiveWitnessTarget.model_validate_json(
            encoded(
                {
                    "profile": profile.model_dump(mode="json"),
                    "pin": pin.model_dump(mode="json"),
                    "api": ref,
                    "supervisor": {
                        "credentials": ref,
                        "request_key_file": "/private/request",
                        "response_key_file": "/private/response",
                        "tls": {
                            "certificate_file": "/private/cert",
                            "private_key_file": "/private/key",
                            "certificate_sha256": "2" * 64,
                            "peer_certificate_sha256": "3" * 64,
                        },
                    },
                }
            )
        )
        inputs = partition({k: v for k, v in probe_requests(plan).items() if "dryRun" not in v})[
            kind
        ]
        request = challenge_for(
            plan,
            kind,
            challenge="9" * 64,
            started_at=NOW.isoformat(),
            deadline=(NOW + timedelta(seconds=5)).isoformat(),
            requests=inputs,
        )
        report = fake_report(
            plan, request, pin, {key: collection.snapshot["probes"][key] for key in inputs}
        )
        h = SimpleNamespace(plan=plan, pin=pin, target=target, request=request, report=report)
        h.api, h.driver = FakeAPI(h), FakeNativeDriver(h)
        for name in ("service", "adapter"):
            journal = CollectorJournal(tmp_path / f"{kind}-{name}-{len(journals)}")
            journals.append(journal)
            setattr(h, name + "_journal", journal)
        h.service = NativeSupervisorService(
            target=target,
            request=request,
            driver=h.driver,
            journal=h.service_journal,
            request_key=b"r" * 32,
            response_key=b"s" * 32,
            authority=lambda _: True,
            now=lambda: NOW,
            monotonic=lambda: 10.0,
        )

        class Transport:
            calls = 0

            def send(self, req, *, deadline, limit):
                self.calls += 1
                return APIResponse(200, h.service.handle(req.body), str(self.calls))

        h.transport = Transport()
        h.rpc = NativeSupervisorRPC(
            target=target,
            transport=h.transport,
            request_key=b"r" * 32,
            response_key=b"s" * 32,
            monotonic=lambda: 10.0,
        )
        h.sensors = DockerDesktopNodeSensors(
            target=target,
            plan=plan,
            api_transport=h.api,
            supervisor=h.rpc,
            journal=h.adapter_journal,
            monotonic=lambda: 10.0,
            now=lambda: NOW,
        )
        h.program = PROGRAMS[kind](h.sensors, monotonic=lambda: 10.0)
        return h

    yield build
    for journal in journals:
        journal.close()


def run(h):
    data = h.program(h.request, deadline=15.0)
    return h.report.model_copy(update={"source": "independent-daemon", **data})


@pytest.mark.parametrize("kind", PROGRAMS)
def test_raw_native_live_adapter_round_trip(live, kind):
    h = live(kind)
    report = run(h)
    assert report.state == "observed"
    # Reparse serialized evidence and validate it again, including raw native records.
    report = WitnessReport.model_validate_json(canonical(report))
    validate_report(report, h.request, h.pin, h.plan, now=NOW + timedelta(seconds=4))
    assert report.native["target_digest"] == h.target.profile.digest
    assert report.cleanup
    assert all(
        "dryRun=All" in r.path
        for r in h.api.calls
        if r.method == "POST"
        and r.body
        and decode(r.body).get("spec", {}).get("hostNetwork") is True
    )
    with pytest.raises(CollectionError):
        h.sensors.prepare(h.request, deadline=15.0)


@pytest.mark.parametrize(
    "kind,operation,path,value",
    [
        ("cni", "preflight", ("cni",), "unavailable"),
        ("cni", "network-exchange", ("flow", "verdict"), "FORWARDED"),
        ("cni", "network-exchange", ("socket", "proxy_used"), True),
        ("cni", "network-exchange", ("listener", "healthy"), False),
        ("cni", "network-exchange", ("flow", "source_pod_uid"), "wrong"),
        ("cni", "network-exchange", ("route", "equivalent_policy_path"), False),
        ("quota", "preflight", ("cgroup",), "unavailable"),
        ("quota", "preflight", ("filesystem",), "local-path"),
        ("quota", "cgroup", ("cri", "node_uid"), "wrong"),
        ("quota", "cgroup", ("cri", "identity"), "wrong"),
        ("quota", "cgroup", ("cri", "status", "imageRef"), "wrong"),
        ("quota", "cgroup", ("files", "memory.max"), "max"),
        ("quota", "filesystem", ("items", 0, "quota_after", "bytes_used"), 0),
        ("quota", "filesystem", ("items", 0, "quota_after", "inodes_used"), 0),
        ("quota", "filesystem", ("items", 1, "mount", "pvc_uid"), "wrong"),
        ("quota", "filesystem", ("items", 1, "mount", "empty_at_prepare"), False),
        ("watchdog", "watchdog-observe", ("heartbeat", "acknowledged"), False),
        ("watchdog", "watchdog-observe", ("process", "cgroup.events"), "populated 1\n"),
        ("watchdog", "watchdog-observe", ("writes", "after"), 2),
        ("watchdog", "watchdog-observe", ("kill", "requests"), 2),
        ("watchdog", "watchdog-observe", ("clock", "boot_id"), "wrong"),
        ("watchdog", "watchdog-observe", ("clock", "quiet_ns"), 2 * 10**9),
    ],
)
def test_native_faults_block(live, kind, operation, path, value):
    h = live(kind)

    def fault(op, raw):
        if op == operation:
            for part in path[:-1]:
                raw = raw[part]
            raw[path[-1]] = value

    h.driver.fault = fault
    report = run(h)
    assert report.state == "unknown"
    with pytest.raises(CollectionError):
        validate_report(report, h.request, h.pin, h.plan, now=NOW + timedelta(seconds=4))
    assert sum(op == "cleanup" for op, _ in h.driver.calls) <= 1


@pytest.mark.parametrize(
    "fault", ["default", "mutation", "policy-bypass", "uid", "secret", "cleanup-owner"]
)
def test_admission_real_request_catalogue_failures(live, fault):
    h = live("admission")

    def mutate(req, value):
        if "dryRun=All" in req.path and value.get("kind") in {"Pod", "Job"}:
            if fault == "default":
                value["spec"]["unreviewedDefault"] = True
            elif fault == "mutation":
                value["spec"]["hostNetwork"] = True
        if "validatingadmissionpolicies/" in req.path and fault == "policy-bypass":
            value["spec"]["failurePolicy"] = "Ignore"
        if req.method == "GET" and req.path in h.api.persisted:
            if fault in {"uid", "cleanup-owner"}:
                value["metadata"]["uid" if fault == "uid" else "annotations"] = (
                    "wrong" if fault == "uid" else {"tessera.io/nonce": "wrong"}
                )
        if fault == "secret":
            value["password"] = "must-not-retain"

    h.api.fault = mutate
    report = run(h)
    assert report.state == "unknown"
    assert "must-not-retain" not in canonical(report).decode()
    if fault in {"default", "mutation", "policy-bypass", "secret"}:
        assert not h.api.persisted  # no actual create after failed dry-run/preflight
    if fault in {"uid", "cleanup-owner"}:
        assert not any(r.method == "DELETE" for r in h.api.calls)


def test_native_lost_response_is_not_retried_and_nonce_is_burned(live):
    h = live("quota")
    original = h.transport.send

    def lost(req, **kwargs):
        original(req, **kwargs)
        raise TimeoutError("unsafe upstream text")

    h.transport.send = lost
    for _ in range(2):
        with pytest.raises(CollectionError):
            h.rpc.call(h.request, "preflight", {}, deadline=15.0)
    assert h.transport.calls == 1
    assert len(h.driver.calls) == 1


@pytest.mark.parametrize(
    "field", ["nonce", "challenge", "request_digest", "target_digest", "sequence"]
)
def test_serialized_replay_binding_is_revalidated(live, field):
    h = live("cni")
    report = run(h)
    data = report.model_dump(mode="json")
    data["native"]["records"][0]["binding"][field] = 99 if field == "sequence" else "0" * 64
    with pytest.raises(CollectionError):
        validate_report(
            WitnessReport.model_validate_json(encoded(data)),
            h.request,
            h.pin,
            h.plan,
            now=NOW + timedelta(seconds=4),
        )


def test_default_target_cli_does_not_read_or_connect(monkeypatch, capsys):
    monkeypatch.setattr(
        "tessera_worker.capability_collector_cli.private_read",
        lambda *_: pytest.fail("default read"),
    )
    assert main(["--target", "/must/not/be/read"]) == 0
    assert "no effects" in capsys.readouterr().out
    assert main(["--target-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "target" in schema["required"] and "witness_tls" in schema["required"]
    assert main(["--live"]) == 2


@pytest.mark.parametrize("fault", [None, "post-write", "false-exit", "lost-kill", "lost-heartbeat"])
def test_external_monotonic_watchdog_no_retry(tmp_path, fault):
    clock = [10.0]

    class Driver:
        kills = 0
        inspections = 0

        def fault(self, fixture, trigger, *, deadline):
            assert fixture == "owned" and trigger == "broker-suspended"

        def kill(self, fixture, *, deadline):
            self.kills += 1
            if fault == "lost-kill":
                raise TimeoutError

        def inspect(self, fixture, *, deadline):
            self.inspections += 1
            return {
                "exit_code": 0 if fault == "false-exit" else 137,
                "populated": 0,
                "credential_state": "revoked",
                "writes": 2 if fault == "post-write" and self.inspections == 2 else 1,
            }

    journal = CollectorJournal(tmp_path / "watchdog")
    try:
        driver = Driver()
        w = MonotonicFixtureWatchdog(
            journal=journal,
            driver=driver,
            boot_id="boot",
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
        w.arm("owned", "challenge")
        kwargs = dict(
            challenge="lost" if fault == "lost-heartbeat" else "challenge",
            trigger="broker-suspended",
            lease=2,
            grace=0,
            quiet=1,
            deadline=15.0,
        )
        if fault is None:
            result = w.terminate_and_observe("owned", **kwargs)
            assert result["kill_requests"] == 1 and result["before"] == result["after"]
        else:
            with pytest.raises((CollectionError, TimeoutError)):
                w.terminate_and_observe("owned", **kwargs)
        with pytest.raises(CollectionError):
            w.terminate_and_observe("owned", **kwargs)
        assert driver.kills == (0 if fault == "lost-heartbeat" else 1)
    finally:
        journal.close()


def test_cgroup_reader_rejects_non_linux_before_reading(monkeypatch):
    monkeypatch.setattr("tessera_worker.capability_witness_supervisor.sys.platform", "darwin")
    with pytest.raises(CollectionError):
        LinuxCgroupFiles(-1)


@pytest.mark.parametrize("fault", [None, "symlink", "oversize", "unavailable"])
def test_linux_cgroup_file_reader_on_fake_filesystem(tmp_path, monkeypatch, fault):
    monkeypatch.setattr("tessera_worker.capability_witness_supervisor.sys.platform", "linux")
    for name in LinuxCgroupFiles.FILES:
        (tmp_path / name).write_text("100\n")
    if fault == "symlink":
        (tmp_path / "cpu.max").unlink()
        (tmp_path / "cpu.max").symlink_to(tmp_path / "memory.max")
    elif fault == "oversize":
        (tmp_path / "cpu.max").write_text("x" * 4097)
    elif fault == "unavailable":
        (tmp_path / "cpu.max").unlink()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        reader = LinuxCgroupFiles(fd, monotonic=lambda: 10)
        if fault:
            with pytest.raises((CollectionError, OSError)):
                reader.snapshot(deadline=15)
        else:
            assert reader.snapshot(deadline=15) == {k: "100\n" for k in LinuxCgroupFiles.FILES}
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "fault", [None, "enospc", "success", "underreport", "no-driver", "inode-mismatch"]
)
def test_linux_quota_sentinel_only_one_byte_and_no_capacity_fallback(tmp_path, monkeypatch, fault):
    monkeypatch.setattr("tessera_worker.capability_witness_supervisor.sys.platform", "linux")
    path = tmp_path / "owned-boundary"
    path.write_bytes(b"")
    directory, boundary = os.open(tmp_path, os.O_RDONLY), os.open(path, os.O_WRONLY)
    calls = []
    reads = []

    def query(fd, *, deadline):
        reads.append(fd)
        if fault == "no-driver":
            raise CollectionError("unavailable")
        return {
            "mount": {
                "id": "mount",
                "project_id": 1,
                "pvc_uid": "pvc",
                "filesystem": "xfs",
                "options": ["prjquota"],
                "dedicated": True,
                "empty_at_prepare": True,
            },
            "quota": {
                "bytes_hard": 10,
                "bytes_used": 9 if fault == "underreport" else 10,
                "inodes_hard": 2,
                "inodes_used": 2 if fault == "inode-mismatch" and len(reads) == 2 else 1,
                "project_id": 1,
                "enforcement": "hard",
            },
        }

    def write(fd, value):
        assert fd == boundary and value == b"x"
        calls.append(value)
        if fault == "success":
            return 1
        raise OSError(errno.ENOSPC if fault == "enospc" else errno.EDQUOT, "synthetic")

    monkeypatch.setattr("tessera_worker.capability_witness_supervisor.os.write", write)
    try:
        sensor = LinuxQuotaSentinel(
            directory_fd=directory,
            boundary_fd=boundary,
            query=query,
            nonce="a" * 64,
            monotonic=lambda: 10,
        )
        if fault:
            with pytest.raises(CollectionError):
                sensor.observe(resource="output-bytes", hard_limit=10, deadline=15)
        else:
            assert (
                sensor.observe(resource="output-bytes", hard_limit=10, deadline=15)["syscall"][
                    "errno"
                ]
                == "EDQUOT"
            )
        assert len(calls) == (0 if fault in {"underreport", "no-driver"} else 1)
    finally:
        os.close(boundary)
        os.close(directory)


@pytest.mark.parametrize(
    "fault", ["pids", "oom", "bytes", "inodes", "cleanup", "policy", "raw-admission"]
)
def test_normalized_and_raw_contradictions_cannot_activate(live, fault):
    kind = "admission" if fault == "raw-admission" else "cni" if fault == "policy" else "quota"
    h = live(kind)
    report = run(h)
    assert report.state == "observed"
    data = report.model_dump(mode="json")
    o = data["observations"]
    if fault == "pids":
        o["pids_events_after"]["max"] = 0
    elif fault == "oom":
        o["termination"]["reason"] = "Completed"
    elif fault in {"bytes", "inodes"}:
        o["filesystems"][0 if fault == "bytes" else 1]["usage_before"] -= 1
    elif fault == "cleanup":
        data["cleanup"][0]["preconditions"]["uid"] = "not-owned"
    elif fault == "policy":
        o["policy_resource_version"] = "stale"
    else:
        records = data["native"]["api"]
        next(r for r in records if "dryRun=All" in r["request"]["path"])["response"]["spec"][
            "mutated"
        ] = True
    with pytest.raises(CollectionError):
        validate_report(
            WitnessReport.model_validate_json(encoded(data)),
            h.request,
            h.pin,
            h.plan,
            now=NOW + timedelta(seconds=4),
        )


def test_native_service_replay_and_unsigned_request_do_not_reexecute(live):
    h = live("quota")
    messages = []
    original = h.transport.send

    def capture(req, **kwargs):
        messages.append(req.body)
        return original(req, **kwargs)

    h.transport.send = capture
    h.rpc.call(h.request, "preflight", {}, deadline=15)
    with pytest.raises(CollectionError):
        h.service.handle(messages[0])
    bad = decode(messages[0])
    bad["binding"]["sequence"] = 2
    with pytest.raises(CollectionError):
        h.service.handle(encoded(bad))
    assert len(h.driver.calls) == 1
