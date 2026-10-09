"""Isolated delivery faults and provenance checks; no Kubernetes effects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_worker_artifacts import MemoryChannel

from tessera_worker.kubernetes_execution import KubernetesOutputBinding
from tessera_worker.kubernetes_output_collector import KubernetesOutputCollector
from tessera_worker.kubernetes_output_delivery import KubernetesOutputDelivery
from tessera_worker.kubernetes_output_pod import run
from tessera_worker.outputs import CollectedOutput


def package():
    return SimpleNamespace(
        operation_id="a" * 32,
        package_digest="b" * 64,
        binding=SimpleNamespace(attempt_id="attempt_test", lease_id="lease_test", epoch=1),
        package={"output_paths": ["result.txt"]},
        output=KubernetesOutputBinding("outputs", "/outputs", "pvc-uid", "c" * 64, True),
    )


class Channel(MemoryChannel):
    def __init__(self, late=False, lose=None):
        super().__init__()
        self.late, self.lose = late, lose
        self.lost = False
        self.manifest = None

    def post(self, path, body, key):
        if path.endswith("/project-artifacts"):
            result = {
                "placements": [
                    {**entry, "artifact_id": entry["id"], "state": "committed"}
                    for entry in body["artifacts"]
                ]
            }
        elif path.endswith("/artifact-manifest"):
            canonical = {
                "version": 1,
                "project_id": "project_test",
                "task_id": "task_test",
                "grant_id": "grant_test",
                "lease_id": "lease_test",
                "worker_principal_id": "worker_test",
                "epoch": 1,
                "operation_id": body["operation_id"],
                "plan_digest": "d" * 64,
                "artifacts": body["artifacts"],
            }
            result = {
                **canonical,
                "total_bytes": sum(entry["size"] for entry in body["artifacts"]),
                "status": "quarantined",
                "classification": "unclassified",
                "late": self.late,
                "manifest_digest": hashlib.sha256(
                    json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            self.manifest = result
        else:
            result = super().post(path, body, key)
        if path.endswith("/" + str(self.lose)) and not self.lost:
            self.lost = True
            raise ConnectionError("lost committed receipt")
        return result


class Collector:
    def __init__(self, content=b"retained\x00\xff output"):
        self.calls = 0
        self.content = content

    def collect_outputs(self, _package):
        self.calls += 1
        return (
            CollectedOutput("result.txt", self.content, hashlib.sha256(self.content).hexdigest()),
        )


@pytest.mark.parametrize(
    "endpoint", ["artifact-uploads", "chunks", "complete", "project-artifacts", "artifact-manifest"]
)
def test_restart_resumes_immutable_bytes_after_committed_response_loss(tmp_path, endpoint):
    descriptor, collector, channel = package(), Collector(), Channel(lose=endpoint)
    with pytest.raises(ConnectionError):
        KubernetesOutputDelivery(tmp_path).deliver(descriptor, collector, channel)
    collector.content = b"changed after interruption"
    result = KubernetesOutputDelivery(tmp_path).deliver(descriptor, collector, channel)
    assert result["status"] == "quarantined"
    assert channel.content == b"retained\x00\xff output"
    assert collector.calls == 1
    assert result["operation_id"] == descriptor.operation_id
    assert KubernetesOutputDelivery(tmp_path).deliver(descriptor, collector, channel) == result


def test_late_delivery_stays_quarantined_history(tmp_path):
    result = KubernetesOutputDelivery(tmp_path).deliver(package(), Collector(), Channel(late=True))
    assert result["late"] is True
    assert result["classification"] == "unclassified"
    assert result["status"] == "quarantined"


@pytest.mark.parametrize(
    "field,value", [("attempt_id", "other-attempt"), ("lease_id", "other-lease"), ("epoch", 2)]
)
def test_queue_rejects_changed_attempt_lease_epoch(tmp_path, field, value):
    descriptor = package()
    queue = KubernetesOutputDelivery(tmp_path)
    queue.begin(descriptor)
    setattr(descriptor.binding, field, value)
    with pytest.raises(ValueError, match="provenance changed"):
        queue.deliver(descriptor, Collector(), Channel())


def test_queue_rejects_changed_pvc(tmp_path):
    descriptor = package()
    queue = KubernetesOutputDelivery(tmp_path)
    queue.begin(descriptor)
    descriptor.output = replace(descriptor.output, pvc_uid="other-pvc")
    with pytest.raises(ValueError, match="provenance changed"):
        queue.pending(descriptor)


def test_scoped_output_marker_and_bytes(tmp_path):
    identity = {"operation": "a" * 32, "attempt": "original", "nonce": "original"}
    assert run("prepare", identity, ["result.txt"], tmp_path)["prepared"]
    run("prepare", identity, ["result.txt"], tmp_path)
    (tmp_path / identity["operation"] / "result.txt").write_bytes(b"exact bytes")
    assert (
        run("collect", identity, ["result.txt"], tmp_path)["outputs"][0]["sha256"]
        == hashlib.sha256(b"exact bytes").hexdigest()
    )
    with pytest.raises(ValueError, match="provenance"):
        run("collect", {**identity, "nonce": "stale"}, ["result.txt"], tmp_path)
    with pytest.raises(ValueError, match="provenance"):
        run("collect", {**identity, "attempt": "replacement"}, ["result.txt"], tmp_path)


def test_never_adopts_stale_directory(tmp_path):
    identity = {"operation": "a" * 32}
    (tmp_path / identity["operation"]).mkdir()
    (tmp_path / identity["operation"] / "result.txt").write_bytes(b"old attempt")
    with pytest.raises(ValueError, match="Unowned"):
        run("prepare", identity, ["result.txt"], tmp_path)


def test_prepare_recovers_marker_before_directory_interruption(tmp_path):
    identity = {"operation": "a" * 32}
    (tmp_path / (identity["operation"] + ".provenance.json")).write_text(
        json.dumps(identity, sort_keys=True, separators=(",", ":"))
    )
    assert run("prepare", identity, ["result.txt"], tmp_path)["prepared"]
    assert (tmp_path / identity["operation"]).is_dir()


def test_collector_rejects_uid_replacement_and_privilege_expansion():
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "collector"},
        "spec": {"containers": [{"name": "output"}]},
    }
    pod = {**manifest, "metadata": {"name": "collector", "uid": "owned"}}
    KubernetesOutputCollector.verify_pod(pod, manifest, "owned")
    with pytest.raises(ValueError):
        KubernetesOutputCollector.verify_pod(pod, manifest, "replacement")
    with pytest.raises(ValueError):
        KubernetesOutputCollector.verify_pod(
            {**pod, "spec": {**pod["spec"], "hostNetwork": True}}, manifest, "owned"
        )


def test_scoped_job_mount_cannot_read_another_attempt_and_legacy_identity_is_preserved():
    from test_kubernetes_contracts import _binding, _cluster

    from tessera_worker.kubernetes_contracts import job_manifest

    binding = _binding(
        output_pvc="outputs", output_mount_path="/outputs", output_operation_scoped=True
    )
    manifest = job_manifest(binding, _cluster())
    mount = manifest["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][-1]
    assert mount["subPath"] == binding.operation_id
    assert binding.journal_fields["output_operation_scoped"] is True
    legacy = replace(binding, output_operation_scoped=False)
    assert "output_operation_scoped" not in legacy.journal_fields
    mount = job_manifest(legacy, _cluster())["spec"]["template"]["spec"]["containers"][0][
        "volumeMounts"
    ][-1]
    assert "subPath" not in mount


def test_helper_cleanup_is_uid_scoped_and_resumes_lost_delete_response(tmp_path):
    import sqlite3

    descriptor = package()
    name = "tessera-output-prepare-" + descriptor.operation_id
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name},
        "spec": {"containers": [{"name": "output"}]},
    }

    class Client:
        pod = {
            **manifest,
            "metadata": {"name": name, "uid": "owned"},
            "status": {"phase": "Succeeded"},
        }
        calls = []

        def verify(self, output):
            assert output.pvc_uid == descriptor.output.pvc_uid

        def get(self, kind, observed_name):
            assert kind == "pod" and observed_name == name
            return self.pod

        def path(self, kind, observed_name):
            assert kind == "pod" and observed_name == name
            return "/owned/pods/" + name

        def request(self, method, path, body):
            self.calls.append((method, path, body))
            self.pod = None
            raise ConnectionError("lost UID-scoped delete response")

    client = Client()
    collector = KubernetesOutputCollector(
        client, descriptor.output, "installed/tessera@sha256:" + "f" * 64, tmp_path / "pods.sqlite3"
    )
    with sqlite3.connect(collector.journal) as db:
        db.execute(
            "INSERT INTO collectors VALUES (?, ?, ?, ?)",
            (name, json.dumps(manifest), "owned", json.dumps({"prepared": True})),
        )
    with pytest.raises(ConnectionError):
        collector.release("prepare", descriptor)
    collector.release("prepare", descriptor)
    assert len(client.calls) == 1
    assert client.calls[0][2]["preconditions"] == {"uid": "owned"}
    client.pod = {**manifest, "metadata": {"name": name, "uid": "replacement"}}
    with pytest.raises(ValueError):
        collector.release("prepare", descriptor)
    assert len(client.calls) == 1


def test_collection_cleanup_never_precedes_durable_byte_custody(tmp_path):
    import sqlite3

    class CleanupCollector(Collector):
        failed = False

        def release_collector(self, descriptor):
            with sqlite3.connect(tmp_path / "output-delivery.sqlite3") as db:
                encoded = db.execute(
                    "SELECT outputs FROM deliveries WHERE operation=?", (descriptor.operation_id,)
                ).fetchone()[0]
            assert encoded is not None
            assert bytes.fromhex(json.loads(encoded)[0][1]) == self.content
            if not self.failed:
                self.failed = True
                raise ConnectionError("cleanup response lost")

    collector, channel = CleanupCollector(), Channel()
    with pytest.raises(ConnectionError):
        KubernetesOutputDelivery(tmp_path).deliver(package(), collector, channel)
    assert channel.requests == []
    assert (
        KubernetesOutputDelivery(tmp_path).deliver(package(), collector, channel)["status"]
        == "quarantined"
    )
    assert collector.calls == 1


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("location", ["mount", "pvc", "both"])
def test_pod_readonly_omission_only_accepts_known_false_defaults(readonly, location):
    from copy import deepcopy

    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "collector"},
        "spec": {
            "containers": [
                {
                    "name": "output",
                    "volumeMounts": [
                        {"name": "output", "mountPath": "/outputs", "readOnly": readonly}
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "output",
                    "persistentVolumeClaim": {"claimName": "outputs", "readOnly": readonly},
                }
            ],
        },
    }
    pod = deepcopy(manifest)
    pod["metadata"]["uid"] = "owned"
    if location in {"mount", "both"}:
        del pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"]
    if location in {"pvc", "both"}:
        del pod["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"]
    if readonly:
        with pytest.raises(ValueError):
            KubernetesOutputCollector.verify_pod(pod, manifest, "owned")
    else:
        KubernetesOutputCollector.verify_pod(pod, manifest, "owned")
        pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "foreign"
        with pytest.raises(ValueError):
            KubernetesOutputCollector.verify_pod(pod, manifest, "owned")


def test_false_default_exception_does_not_relax_other_isolation_fields():
    manifest = {
        "metadata": {"name": "collector"},
        "spec": {"containers": [{"name": "output", "securityContext": {"privileged": False}}]},
    }
    pod = {
        "metadata": {"name": "collector", "uid": "owned"},
        "spec": {"containers": [{"name": "output", "securityContext": {}}]},
    }
    with pytest.raises(ValueError):
        KubernetesOutputCollector.verify_pod(pod, manifest, "owned")


def creation_fixture(tmp_path, change=None):
    import sqlite3
    from copy import deepcopy

    from test_kubernetes_contracts import _cluster

    descriptor = package()

    class Client:
        cluster = _cluster()
        pod = None
        posts = 0

        def verify(self, output):
            assert output == descriptor.output

        def get(self, kind, name):
            assert kind == "pod"
            return self.pod

        def path(self, kind, name=None):
            return "/pods" + ("/" + name if name else "")

        def request(self, method, path, body=None, **_kwargs):
            if method == "GET":
                return {"prepared": True, "identity": collector.identity(descriptor)}
            assert method == "POST"
            self.posts += 1
            self.pod = deepcopy(body)
            self.pod["metadata"]["uid"] = "direct-create-owned"
            self.pod["status"] = {"phase": "Succeeded"}
            if change == "spec":
                self.pod["spec"]["containers"][0]["command"] = ["foreign"]
            elif change == "namespace":
                self.pod["metadata"]["namespace"] = "foreign"
            elif change == "labels":
                self.pod["metadata"]["labels"] = {}
            elif change == "uid":
                del self.pod["metadata"]["uid"]
            else:
                del self.pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"]
                del self.pod["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"]
            return self.pod

    client = Client()
    collector = KubernetesOutputCollector(
        client, descriptor.output, "installed/tessera@sha256:" + "f" * 64, tmp_path / "pods.sqlite3"
    )
    return descriptor, collector, client, sqlite3


@pytest.mark.parametrize("change", ["spec", "namespace", "labels", "uid"])
def test_create_response_captures_only_attributable_uid_before_spec_rejection(tmp_path, change):
    descriptor, collector, client, sqlite3 = creation_fixture(tmp_path, change)
    with pytest.raises(ValueError):
        collector.execute("prepare", descriptor)
    with sqlite3.connect(collector.journal) as db:
        uid, result = db.execute("SELECT uid,result FROM collectors").fetchone()
    assert uid == ("direct-create-owned" if change == "spec" else None)
    assert result is None and client.posts == 1


def test_create_response_with_omitted_false_defaults_retains_uid_and_result(tmp_path):
    descriptor, collector, client, sqlite3 = creation_fixture(tmp_path)
    result = collector.execute("prepare", descriptor)
    assert result["prepared"] is True
    with sqlite3.connect(collector.journal) as db:
        uid, encoded = db.execute("SELECT uid,result FROM collectors").fetchone()
    assert uid == "direct-create-owned" and json.loads(encoded) == result
    assert collector.execute("prepare", descriptor) == result
    assert client.posts == 1


@pytest.mark.parametrize("cached_result", [False, True])
def test_current_lookup_cannot_backfill_original_uid(tmp_path, cached_result):
    descriptor, collector, client, sqlite3 = creation_fixture(tmp_path)
    manifest = collector.manifest("prepare", descriptor)
    with sqlite3.connect(collector.journal) as db:
        db.execute(
            "INSERT INTO collectors VALUES (?, ?, NULL, ?)",
            (
                manifest["metadata"]["name"],
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                "{}" if cached_result else None,
            ),
        )
    client.pod = json.loads(json.dumps(manifest))
    client.pod["metadata"]["uid"] = "current-not-original"
    client.pod["status"] = {"phase": "Succeeded"}
    with pytest.raises(ValueError, match="Original collector UID"):
        collector.execute("prepare", descriptor)
    with sqlite3.connect(collector.journal) as db:
        assert db.execute("SELECT uid,result FROM collectors").fetchone() == (
            None,
            "{}" if cached_result else None,
        )
    assert client.posts == 0


@pytest.mark.parametrize("late", [False, True])
def test_control_api_retains_issued_kubernetes_operation_bytes_after_restart(
    tmp_path, monkeypatch, late
):
    from test_kubernetes_execution import _admitted_package
    from test_runtime import ApiChannel

    _, _, descriptor = _admitted_package(tmp_path)
    state = tmp_path / "state"
    import sqlite3

    if late:
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute(
                "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (descriptor.binding.lease_id,),
            )
    monkeypatch.setattr("tessera_worker.artifacts.CHUNK_BYTES", 3)

    class LostChunk(ApiChannel):
        lost = False

        def post(self, path, body, key):
            receipt = super().post(path, body, key)
            if path.endswith("/chunks") and not self.lost:
                self.lost = True
                raise ConnectionError("lost real API committed chunk")
            return receipt

    collector = Collector()
    with pytest.raises(ConnectionError):
        KubernetesOutputDelivery(tmp_path).deliver(descriptor, collector, LostChunk(state))
    result = KubernetesOutputDelivery(tmp_path).deliver(descriptor, collector, ApiChannel(state))
    assert result["late"] is late
    assert result["operation_id"] == descriptor.operation_id
    assert result["status"] == "quarantined"
    assert collector.calls == 1
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        artifacts = db.execute(
            "SELECT content,operation_id,late FROM artifacts WHERE lease_id=?",
            (descriptor.binding.lease_id,),
        ).fetchall()
        assert artifacts == [(collector.content, descriptor.operation_id, int(late))]


@pytest.mark.parametrize("lost_endpoint", ["chunks", "observations"])
def test_ordinary_service_owns_collection_and_restart_delivery(tmp_path, lost_endpoint):
    import sqlite3
    from copy import deepcopy

    from fastapi.testclient import TestClient
    from test_kubernetes_dispatch import _ApiChannel, _dispatch, _service
    from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes

    from tessera_control.api import create_app
    from tessera_worker.kubernetes_contracts import cluster_identity_digest

    response, _, descriptor = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(descriptor, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    _service(tmp_path, client, dispatch, fake, cluster).run(max_cycles=1)
    fake.jobs[descriptor.binding.job_name]["status"] = {"succeeded": 1}
    template = fake.jobs[descriptor.binding.job_name]["spec"]["template"]
    pod = fake.pod("Succeeded")
    pod["metadata"] = deepcopy(template["metadata"]) | {
        "namespace": cluster.namespace,
        "uid": "pod-terminal",
        "ownerReferences": [{"kind": "Job", "uid": "job-fixture", "controller": True}],
    }
    pod["spec"] = deepcopy(template["spec"])
    fake.pods = [pod]

    class Lost(_ApiChannel):
        lost = False

        def post(self, path, body, key):
            receipt = super().post(path, body, key)
            if path.endswith("/" + lost_endpoint) and not self.lost:
                self.lost = True
                raise ConnectionError("lost committed ordinary service response")
            return receipt

    collector = Collector()
    service = _service(tmp_path, client, dispatch, fake, cluster, channel=Lost(client))
    service.adapter._collect_output = lambda _handoff, package: collector.collect_outputs(package)
    first = service.run(max_cycles=1)
    assert first["status"] == "succeeded"
    assert first["reconciliation_required"] is True
    if lost_endpoint == "chunks":
        assert first["output_delivery"] == "pending"
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute(
                "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (descriptor.binding.lease_id,),
            )
    recovered = _service(tmp_path, client, dispatch, fake, cluster)
    recovered.adapter._collect_output = lambda *_: pytest.fail("restart must use retained bytes")
    result = recovered.run(max_cycles=1)
    assert result.get("output_delivery") == "completed" or result["status"] == "report_delivered"
    assert collector.calls == 1
    assert fake.creates == 1
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        rows = db.execute(
            "SELECT content,late FROM artifacts WHERE lease_id=?", (descriptor.binding.lease_id,)
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] == collector.content
        manifest = db.execute(
            "SELECT late FROM artifact_manifests WHERE lease_id=?", (descriptor.binding.lease_id,)
        ).fetchone()
        # Late history is classified by control, never repaired into success by worker.
        assert bool(manifest[0]) is (lost_endpoint == "chunks")
