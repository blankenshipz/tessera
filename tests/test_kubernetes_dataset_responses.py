"""Isolated subprocess responses; these tests are NOT live cluster evidence."""

import hashlib
import json
import subprocess
from copy import deepcopy
from dataclasses import replace

import pytest
from test_dataset_job_inputs import delivery
from test_kubernetes_contracts import _cluster

from tessera_worker.docker_desktop_kubernetes import (
    DockerDesktopKubernetesAdapter,
    DockerDesktopTarget,
    LiveKubernetesError,
    _run,
)
from tessera_worker.kubernetes_contracts import canonical_json
from tessera_worker.kubernetes_execution import KubernetesOutputBinding


@pytest.fixture
def transport(tmp_path, monkeypatch):
    config = tmp_path / "kubeconfig"
    config.write_text("synthetic reference, not credentials")
    adapter = DockerDesktopKubernetesAdapter(
        DockerDesktopTarget(
            config,
            "docker-desktop",
            "https://127.0.0.1:1",
            "a" * 64,
            hashlib.sha256(config.read_bytes()).hexdigest(),
        ),
        tmp_path / "journal.sqlite3",
    )
    _, resources = delivery()
    resource = deepcopy(resources[0])
    resource["metadata"]["uid"] = "actual-fixture-uid"
    native = {"stdout": json.dumps(resource), "stderr": "", "exit": 0, "calls": []}

    def command(argv, **kw):
        native["calls"].append(tuple(argv))
        kw["stdout"].write(native["stdout"])
        kw["stderr"].write(native["stderr"])
        return subprocess.CompletedProcess(argv, native["exit"])

    monkeypatch.setattr(subprocess, "run", command)
    return adapter, resource, native


def test_generic_boundary_reproduced_without_widening(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "x" * 65537, "")
    )
    with pytest.raises(LiveKubernetesError, match="bounded adapter limit"):
        _run(("kubectl", "get", "configmap", "synthetic"))


def test_near_four_mib_all_chunk_create_read_receipts(transport):
    adapter, _, native = transport
    content = b"id,x,target,split\n" + b"".join(
        f"row-{i},{'0' * 1000},1,{('train', 'validation', 'test')[i % 3]}\n".encode()
        for i in range(4000)
    )
    _, resources = delivery(content)
    assert 3_900_000 < len(content) <= 4 * 1024 * 1024
    for resource in resources:
        observed = deepcopy(resource)
        observed["metadata"]["uid"] = "native-" + resource["metadata"]["name"]
        native["stdout"] = json.dumps(observed)
        name = resource["metadata"]["name"]
        assert adapter._dataset_resource(name, "tessera-fixture", manifest=resource) == observed
        assert adapter._dataset_resource(name, "tessera-fixture") == observed
    receipts = list(adapter.journal_path.parent.glob("dataset-command-*"))
    assert len(receipts) == 2 * len(resources)
    assert any((p / "stdout").stat().st_size > 65536 for p in receipts)
    for path in receipts:
        assert path.stat().st_mode & 0o777 == 0o700
        for filename in ("stdout", "stderr", "result.json"):
            assert (path / filename).stat().st_mode & 0o777 == 0o600
        assert json.loads((path / "result.json").read_text())["exit"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "oversize",
        "stderr-oversize",
        "missing-uid",
        "malformed",
        "mutable",
        "wrong-namespace",
        "command-error",
    ],
)
def test_invalid_response_retained_and_never_deleted(transport, mutation):
    adapter, resource, native = transport
    if mutation == "oversize":
        native["stdout"] = "x" * (1024 * 1024 + 1)
    elif mutation == "stderr-oversize":
        native["stderr"] = "x" * 65537
    elif mutation == "missing-uid":
        del resource["metadata"]["uid"]
    elif mutation == "malformed":
        native["stdout"] = "not JSON: private fixture bytes"
    elif mutation == "mutable":
        resource["immutable"] = False
    elif mutation == "wrong-namespace":
        resource["metadata"]["namespace"] = "foreign"
    else:
        native.update(exit=1, stderr="private rejection details")
    if mutation in ("missing-uid", "mutable", "wrong-namespace"):
        native["stdout"] = json.dumps(resource)
    with pytest.raises(LiveKubernetesError) as error:
        adapter._dataset_resource(resource["metadata"]["name"], "tessera-fixture")
    assert "private receipt:" in str(error.value)
    assert "private rejection details" not in str(error.value)
    receipt = next(adapter.journal_path.parent.glob("dataset-command-*"))
    assert (receipt / "stdout").read_text() == native["stdout"]
    assert (receipt / "stderr").read_text() == native["stderr"]
    assert json.loads((receipt / "result.json").read_text())["exit"] == native["exit"]
    assert all("delete" not in cmd for cmd in native["calls"])


def test_only_native_notfound_read_returns_absent(transport):
    adapter, resource, native = transport
    native.update(
        stdout="", stderr='Error from server (NotFound): configmaps "owned" not found', exit=1
    )
    assert adapter._dataset_resource(resource["metadata"]["name"], "tessera-fixture") is None
    with pytest.raises(LiveKubernetesError, match="rejected"):
        adapter._dataset_resource(
            resource["metadata"]["name"], "tessera-fixture", manifest=resource
        )


def test_timeout_retains_partial_receipt(transport, monkeypatch):
    adapter, resource, _ = transport

    def timed_out(argv, **kw):
        kw["stdout"].write("partial private bytes")
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(LiveKubernetesError, match="private receipt"):
        adapter._dataset_resource(resource["metadata"]["name"], "tessera-fixture")
    receipt = next(adapter.journal_path.parent.glob("dataset-command-*"))
    assert (receipt / "stdout").read_text() == "partial private bytes"
    assert json.loads((receipt / "result.json").read_text())["failure"] == "TimeoutExpired"


def test_oversized_request_has_no_native_effect(transport):
    adapter, resource, native = transport
    resource["binaryData"] = {"part": "x" * (1024 * 1024)}
    with pytest.raises(LiveKubernetesError, match="request exceeds"):
        adapter._dataset_resource(
            resource["metadata"]["name"], "tessera-fixture", manifest=resource
        )
    assert not native["calls"]
    assert not list(adapter.journal_path.parent.glob("dataset-command-*"))


def test_spawn_failure_retains_native_failure_category(transport, monkeypatch):
    adapter, resource, _ = transport

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("synthetic unavailable executable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(LiveKubernetesError, match="private receipt"):
        adapter._dataset_resource(resource["metadata"]["name"], "tessera-fixture")
    receipt = next(adapter.journal_path.parent.glob("dataset-command-*"))
    metadata = json.loads((receipt / "result.json").read_text())
    assert metadata["exit"] is None and metadata["failure"] == "FileNotFoundError"


def test_dataset_client_fences_before_and_after_create(transport, monkeypatch):
    adapter, resource, native = transport
    spec = {"accessModes": ["ReadWriteOnce"]}
    output = KubernetesOutputBinding(
        "outputs", "/outputs", "pvc-uid", hashlib.sha256(canonical_json(spec)).hexdigest(), True
    )
    policy = {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    cluster = _cluster()
    cluster = replace(
        cluster,
        network_none_evidence=replace(
            cluster.network_none_evidence,
            policy_spec_sha256=hashlib.sha256(canonical_json(policy)).hexdigest(),
        ),
    )
    identities = {
        ("namespace", "kube-system"): {"metadata": {"uid": cluster.cluster_uid}},
        ("namespace", cluster.namespace): {"metadata": {"uid": cluster.namespace_uid}},
        ("pvc", "outputs"): {
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "outputs", "namespace": cluster.namespace, "uid": "pvc-uid"},
            "spec": spec,
            "status": {"phase": "Bound"},
        },
        ("networkpolicy", cluster.network_none_evidence.policy_name): {
            "metadata": {"uid": cluster.network_none_evidence.policy_uid},
            "spec": policy,
        },
    }
    monkeypatch.setattr(adapter, "_get", lambda kind, name, namespace: identities[kind, name])
    monkeypatch.setattr(
        adapter, "_kubectl", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "yes\n", "")
    )
    client = adapter.dataset_client(cluster, output)
    assert client.dataset_create(resource) == resource
    assert client.dataset_read(resource["metadata"]["name"]) == resource
    identities["namespace", cluster.namespace]["metadata"]["uid"] = "foreign"
    count = len(native["calls"])
    with pytest.raises(LiveKubernetesError, match="namespace UID"):
        client.dataset_create(resource)
    assert len(native["calls"]) == count
