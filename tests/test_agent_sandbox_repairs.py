"""Focused P1 checks; signed sensor/TLS data here are synthetic, not live proof."""

import hashlib
import json
import ssl
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from test_agent_sandbox import create, setup

from tessera_control.agent_sandbox_configuration import (
    CASES,
    SignedSandboxWitnessVerifier,
    scope_digest,
)
from tessera_control.agent_sandbox_models import SandboxTarget
from tessera_control.store import Conflict, canonical_digest
from tessera_worker.agent_sandbox import SandboxUnsafe
from tessera_worker.capability_witness import (
    Challenge,
    Resource,
    WitnessPin,
    WitnessReport,
    seal_report,
)
from tessera_worker.kubernetes_remote_service import NamespacedKubernetesClient


def write_private(path, value):
    path.write_bytes(value)
    path.chmod(0o600)
    return str(path)


def operator_config(tmp_path, f):
    target = f.cluster.target.model_copy(
        update={
            "server": "https://127.0.0.1:8443",
            "server_sha256": hashlib.sha256(b"https://127.0.0.1:8443").hexdigest(),
            "ca_sha256": hashlib.sha256(b"synthetic-ca").hexdigest(),
            "witness_sha256": "0" * 64,
            "kubeconfig": write_private(tmp_path / "kubeconfig", b"synthetic-kubeconfig"),
            "kubeconfig_sha256": hashlib.sha256(b"synthetic-kubeconfig").hexdigest(),
        }
    )
    refs, finishes, hashes = {}, [], {}
    finish = datetime.now(UTC) - timedelta(seconds=1)
    begin = finish - timedelta(milliseconds=100)
    for kind, cases in CASES.items():
        pin = WitnessPin(
            kind=kind,
            identity=f"system:serviceaccount:witness-{kind}:sensor",
            namespace=f"witness-{kind}",
            namespace_uid=f"witness-ns-{kind}",
            node_uid="node-uid",
            boot_id="boot",
            target_digest=scope_digest(target),
        )
        request = Challenge(
            kind=kind,
            run_nonce="a" * 64,
            challenge="b" * 64,
            manifest_digest="1" * 64,
            plan_digest="2" * 64,
            package_digest="3" * 64,
            operation_id="4" * 32,
            org_id=target.org_id,
            project_id=target.project_id,
            cluster_digest="5" * 64,
            namespace=target.namespace,
            namespace_uid=target.namespace_uid,
            started_at=begin.isoformat(),
            deadline=(finish + timedelta(minutes=4)).isoformat(),
            inputs={
                "profile": "agent-sandbox-network-none-v1",
                "target_scope": scope_digest(target),
                "upstream_commit": target.upstream_commit,
            },
        )
        resources = tuple(
            Resource(
                kind="Pod",
                name=uid,
                uid=uid,
                namespace=target.namespace,
                namespace_uid=target.namespace_uid,
                run_nonce="a" * 64,
                resource_version="1",
                created_at=begin.isoformat(),
                node_uid="node-uid",
                identity=f"system:serviceaccount:{target.namespace}:probe",
            )
            for uid in ("subject", "control")
        )
        raw = []
        for case in sorted(cases):
            if kind == "cni":
                responses = (
                    {"connected": True, "bytes_received": 1},
                    {"connected": False, "errno": "ETIMEDOUT", "elapsed_ms": 1000},
                )
            elif kind == "admission":
                responses = ({"http_status": 201}, {"http_status": 403, "reason": "Forbidden"})
            elif kind == "quota":
                responses = ({"usage": 101, "limit": 100}, {"usage": 100, "limit": 100})
            elif case == "terminated-uid":
                responses = (
                    {"running_pids": [1]},
                    {
                        "running_pids": [],
                        "pod_uid": "subject",
                        "termination_observed_at": finish.isoformat(),
                    },
                )
            else:
                responses = (
                    {"pid_namespace_inode": 1, "mount_namespace_inode": 2},
                    {
                        "pid_namespace_inode": 3,
                        "mount_namespace_inode": 4,
                        "runtime_handler": "runsc",
                    },
                )
            item = {
                "case": case,
                "subject_uid": "subject",
                "control_uid": "control",
                "node_uid": "node-uid",
                "observed_at": finish.isoformat(),
            }
            for label, uid, response in zip(
                ("control", "isolated"), ("control", "subject"), responses, strict=True
            ):
                item[label] = {
                    "outcome": "observed" if label == "control" else "enforced",
                    "native_receipt": {
                        "request": {"case": case, "subject_uid": uid, "node_uid": "node-uid"},
                        "response": response,
                    },
                }
            raw.append(item)
        report = WitnessReport(
            source="independent-daemon",
            request=request,
            request_digest=canonical_digest(request.model_dump(mode="json")),
            witness=pin,
            started_at=begin.isoformat(),
            completed_at=finish.isoformat(),
            monotonic_elapsed_ms=100,
            state="observed",
            resources=resources,
            observations={},
            probes={},
            cleanup=(),
            native={"sandbox_observations": raw},
        )
        key = hashlib.sha256((kind + "synthetic-key").encode()).digest()
        encoded = json.dumps(seal_report(report, key), sort_keys=True).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        refs[kind] = {
            "report_reference": write_private(tmp_path / f"{kind}.json", encoded),
            "report_sha256": digest,
            "authentication_reference": write_private(tmp_path / f"{kind}.key", key),
            "pin": pin.model_dump(mode="json"),
        }
        hashes[kind] = digest
        finishes.append(finish)
    summary = {
        "observed_at": min(finishes).isoformat(),
        "org_id": target.org_id,
        "project_id": target.project_id,
        "cluster_uid": target.cluster_uid,
        "namespace_uid": target.namespace_uid,
        "runtime_class_uid": target.runtime_class_uid,
        "runtime_handler": target.runtime_handler,
        "network": "ingress-egress-denied-with-positive-controls",
        "runtime": "independently-measured-isolation",
        "admission": "exclusive-writers-fixed-profile-one-uid-release",
        "signed_report_digests": hashes,
    }
    target = target.model_copy(update={"witness_sha256": canonical_digest(summary)})
    value = {
        "collector_image": "tessera@sha256:" + "7" * 64,
        "target": target.model_dump(mode="json"),
        "ca_reference": write_private(tmp_path / "ca", b"synthetic-ca"),
        "token_reference": write_private(tmp_path / "projected-token", b"synthetic-token"),
        "witnesses": refs,
    }
    encoded = json.dumps(value, sort_keys=True).encode()
    reference = write_private(tmp_path / "operator.json", encoded)
    return reference, hashlib.sha256(encoded).hexdigest(), value


def test_normal_serve_constructs_typed_authenticated_backend_without_custom_factory(
    tmp_path, monkeypatch
):
    from tessera_control import cli

    f = setup(tmp_path)
    reference, digest, value = operator_config(tmp_path, f)
    # Synthetic CA only. The actual client/opener composition is not replaced.
    monkeypatch.setattr(
        ssl, "create_default_context", lambda **_: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )
    apps = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **_: apps.append(app))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera",
            "serve",
            "--port",
            "8765",
            "--state-dir",
            str(f.state),
            "--agent-sandbox-config",
            reference,
            "--agent-sandbox-sha256",
            digest,
        ],
    )
    assert cli.main() == 0
    backend = apps[0].state.agent_sandbox_backend
    assert isinstance(backend.client, NamespacedKubernetesClient)
    assert isinstance(backend.verify_witness, SignedSandboxWitnessVerifier)
    assert backend.operator_config.collector_image == "tessera@sha256:" + "7" * 64
    assert backend.client.cluster == SandboxTarget.model_validate(value["target"])
    assert backend.client.opener is not None
    assert f.cluster.calls == []


@pytest.mark.parametrize(
    "fault",
    ["config-digest", "missing-report", "signature", "scope", "fake", "raw-boolean", "stale"],
)
def test_normal_serve_blocks_invalid_witness_before_client_or_effects(tmp_path, monkeypatch, fault):
    from tessera_control import cli
    from tessera_worker import agent_sandbox

    f = setup(tmp_path)
    reference, digest, value = operator_config(tmp_path, f)
    if fault == "config-digest":
        digest = "0" * 64
    elif fault == "missing-report":
        __import__("pathlib").Path(value["witnesses"]["cni"]["report_reference"]).unlink()
    else:
        entry = value["witnesses"]["cni"]
        path = __import__("pathlib").Path(entry["report_reference"])
        envelope = json.loads(path.read_bytes())
        if fault == "signature":
            envelope["signature"] = "0" * 64
        else:
            payload = envelope["payload"]
            if fault == "scope":
                payload["request"]["inputs"]["target_scope"] = "0" * 64
            elif fault == "fake":
                payload["source"] = "fake"
            elif fault == "stale":
                payload["completed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            else:
                payload["native"]["sandbox_observations"][0]["isolated"]["native_receipt"] = True
            key = __import__("pathlib").Path(entry["authentication_reference"]).read_bytes()
            envelope = seal_report(WitnessReport.model_validate_json(json.dumps(payload)), key)
        data = json.dumps(envelope, sort_keys=True).encode()
        path.write_bytes(data)
        entry["report_sha256"] = hashlib.sha256(data).hexdigest()
        data = json.dumps(value, sort_keys=True).encode()
        __import__("pathlib").Path(reference).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(
        agent_sandbox,
        "authenticated_backend",
        lambda *a: pytest.fail("invalid config reached client factory"),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: pytest.fail("invalid config served"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera",
            "serve",
            "--port",
            "8765",
            "--state-dir",
            str(f.state),
            "--agent-sandbox-config",
            reference,
            "--agent-sandbox-sha256",
            digest,
        ],
    )
    assert cli.main() == 2
    assert f.cluster.calls == []


def test_default_serve_remains_disabled(tmp_path, monkeypatch):
    from tessera_control import cli

    f = setup(tmp_path)
    apps = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **_: apps.append(app))
    monkeypatch.setattr(
        sys, "argv", ["tessera", "serve", "--port", "8765", "--state-dir", str(f.state)]
    )
    assert cli.main() == 0
    assert apps[0].state.agent_sandbox_backend is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("priority", 1000000),
        ("priorityClassName", "system-node-critical"),
        ("preemptionPolicy", "Never"),
    ],
)
def test_priority_admission_defaults_pinned_and_elevation_rejected(tmp_path, field, value):
    f = setup(tmp_path)
    create(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    pod = f.cluster.values[f.cluster.path("pod", "tessera-sb-" + f.operation_id)]
    assert pod["spec"]["priority"] == 0
    assert pod["spec"]["preemptionPolicy"] == "PreemptLowerPriority"
    pod["spec"][field] = value
    with pytest.raises(SandboxUnsafe):
        f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)
    assert not any(method == "PUT" and "/pods/" in path for method, path in f.cluster.calls)


def test_delayed_discovery_does_not_hold_writer_and_revocation_prevents_effect(
    tmp_path, monkeypatch
):
    f = setup(tmp_path)
    create(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    entered, resume = threading.Event(), threading.Event()
    request = f.cluster.request

    def delayed(method, path, body=None, **kwargs):
        if method == "GET" and path == f.cluster.runtime_path:
            entered.set()
            assert resume.wait(3)
        return request(method, path, body, **kwargs)

    monkeypatch.setattr(f.cluster, "request", delayed)
    errors = []

    def release():
        try:
            f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=release)
    thread.start()
    assert entered.wait(3)
    before = time.monotonic()
    f.execution.revoke_execution_grant(
        f.owner, f.grant["id"], "revoke", "revoke", f.grant["revision"]
    )
    assert time.monotonic() - before < 0.5
    resume.set()
    thread.join(3)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], Conflict)
    assert not any(method == "PUT" and "/pods/" in path for method, path in f.cluster.calls)


def test_total_discovery_timeout_retains_unclaimed_create_and_pvc_custody(tmp_path, monkeypatch):
    from tessera_worker import agent_sandbox_discovery

    f = setup(tmp_path)
    monkeypatch.setattr(agent_sandbox_discovery, "DISCOVERY_SECONDS", 0.03)
    request = f.cluster.request

    def delayed(method, path, body=None, **kwargs):
        time.sleep(0.02)
        return request(method, path, body, **kwargs)

    monkeypatch.setattr(f.cluster, "request", delayed)
    with pytest.raises(Conflict, match="prerequisites"):
        create(f)
    assert not any(method != "GET" for method, _ in f.cluster.calls)


def test_discovery_timeout_after_possible_execution_preserves_pvc_as_unknown(tmp_path, monkeypatch):
    from test_agent_sandbox import start

    from tessera_worker import agent_sandbox_discovery

    f = setup(tmp_path)
    start(f)
    monkeypatch.setattr(agent_sandbox_discovery, "DISCOVERY_SECONDS", 0.03)
    request = f.cluster.request

    def delayed(method, path, body=None, **kwargs):
        time.sleep(0.02)
        return request(method, path, body, **kwargs)

    monkeypatch.setattr(f.cluster, "request", delayed)
    result = f.adapter.action(f.owner, f.operation_id, "timeout-observe", "observe", f.body)[1]
    assert result["phase"] == "uncertain"
    assert not result["quiescent"]
    assert f.cluster.values[f.cluster.path("pvc", "outputs-tessera-sb-" + f.operation_id)]
    assert not any(method == "DELETE" for method, _ in f.cluster.calls)


def test_final_write_uses_short_total_budget_and_fresh_cas_identity(tmp_path):
    from tessera_worker.agent_sandbox_discovery import PreparedClient

    f = setup(tmp_path)
    create(f)
    path = f.cluster.path("sandbox", "tessera-sb-" + f.operation_id)
    prior = f.cluster.values[path]
    from copy import deepcopy

    reads = {path: deepcopy(prior)}
    recorded = []
    original = f.cluster.request

    def request(method, path, body=None, **kwargs):
        recorded.append(kwargs["timeout_seconds"])
        return original(method, path, body, **kwargs)

    client = type("Client", (), {"request": staticmethod(request), "cluster": f.cluster.cluster})()
    discovery = type(
        "Discovery", (), {"original": client, "reads": reads, "cluster": f.cluster.cluster}
    )()
    prepared = PreparedClient(discovery, f.cluster.target)
    desired = deepcopy(prior)
    desired["spec"]["operatingMode"] = "Running"
    f.cluster.values[path]["metadata"]["resourceVersion"] = "99"
    prepared.request("PUT", path, desired)
    assert recorded and all(0 < value <= 2 for value in recorded)
    assert f.cluster.values[path]["metadata"]["resourceVersion"] == "100"
