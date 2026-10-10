"""Isolated synthetic upstream/control effects, never live isolation evidence."""

import base64
import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_execution import _grant_payload, _owner, _setup

from tessera_control.agent_sandbox import AgentSandboxStore
from tessera_control.agent_sandbox_models import API_VERSION, GATE, SandboxPlan
from tessera_control.api import create_app
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.artifacts import ArtifactStore
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.store import Conflict, Forbidden, NotFound, Store, _connect, canonical_digest
from tessera_worker.agent_sandbox import AgentSandboxBackend, SandboxUnsafe


def resource(kind, name, uid, spec=None, namespace=None):
    value = {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "uid": uid,
            "resourceVersion": "1",
            "generation": 1,
        },
    }
    if spec is not None:
        value["spec"] = spec
    if namespace:
        value["metadata"]["namespace"] = namespace
    return value


def approved_plan(org_id="synthetic-org", project_id="synthetic-project"):
    witness = {
        "org_id": org_id,
        "project_id": project_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "cluster_uid": "cluster",
        "namespace_uid": "namespace",
        "runtime_class_uid": "runtime",
        "runtime_handler": "runsc",
        "network": "ingress-egress-denied-with-positive-controls",
        "runtime": "independently-measured-isolation",
        "admission": "exclusive-writers-fixed-profile-one-uid-release",
    }
    return SandboxPlan.model_validate(
        {
            "target": {
                "org_id": org_id,
                "project_id": project_id,
                "kubeconfig": "/synthetic/kubeconfig",
                "kubeconfig_sha256": "a" * 64,
                "context": "synthetic",
                "server": "https://synthetic.invalid",
                "server_sha256": hashlib.sha256(b"https://synthetic.invalid").hexdigest(),
                "ca_sha256": "c" * 64,
                "cluster_uid": "cluster",
                "namespace": "sandbox-test",
                "namespace_uid": "namespace",
                "runtime_class": "gvisor",
                "runtime_class_uid": "runtime",
                "runtime_handler": "runsc",
                "controller_namespace": "sandbox-system",
                "controller_deployment": "agent-sandbox",
                "controller_uid": "controller",
                "controller_image": "controller@sha256:" + "d" * 64,
                "crd_uid": "crd",
                "crd_spec_sha256": canonical_digest({"synthetic": True}),
                "witness_name": "operator-witness",
                "witness_uid": "witness",
                "witness_sha256": canonical_digest(witness),
            },
            "cpu_millicores": 500,
            "memory_bytes": 67108864,
            "storage_class": "test-retained",
            "storage_bytes": 16777216,
        }
    ), witness


class FakeCluster:
    def __init__(self, plan):
        target = plan.target
        self.cluster = SimpleNamespace(**target.model_dump())
        self.values, self.calls, self.fail = {}, [], None
        for name, uid in (
            ("kube-system", target.cluster_uid),
            (target.namespace, target.namespace_uid),
        ):
            self.values["/api/v1/namespaces/" + name] = resource("Namespace", name, uid)
        self.values[
            "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/sandboxes.agents.x-k8s.io"
        ] = resource(
            "CustomResourceDefinition", "sandboxes.agents.x-k8s.io", "crd", {"synthetic": True}
        )
        self.runtime_path = "/apis/node.k8s.io/v1/runtimeclasses/gvisor"
        self.values[self.runtime_path] = resource("RuntimeClass", "gvisor", "runtime") | {
            "handler": "runsc"
        }
        self.controller_path = "/apis/apps/v1/namespaces/sandbox-system/deployments/agent-sandbox"
        self.values[self.controller_path] = resource(
            "Deployment",
            "agent-sandbox",
            "controller",
            {"template": {"spec": {"containers": [{"image": target.controller_image}]}}},
        ) | {"status": {"observedGeneration": 1, "availableReplicas": 1}}
        self.values["/api/v1/nodes/node-one"] = resource("Node", "node-one", "node-uid") | {
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}
        }
        self.policies = [
            resource(
                "NetworkPolicy",
                "deny-all",
                "policy",
                {
                    "podSelector": {},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [],
                    "egress": [],
                },
            )
        ]
        self.backend = AgentSandboxBackend(self, None)
        self.target = target

    def path(self, kind, name=None):
        return self.backend.path(self.target, kind, name)

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        if self.fail == "discovery" and "pods?" in path:
            raise RuntimeError("synthetic HTTP 403")
        if self.fail == "delete" and method == "DELETE":
            raise TimeoutError("synthetic timeout")
        if method == "GET" and "?" in path:
            base = path.split("?", 1)[0]
            items = (
                self.policies
                if base == self.path("policy")
                else [value for key, value in self.values.items() if key.startswith(base + "/")]
            )
            kind = {
                self.path("policy"): "NetworkPolicyList",
                self.path("pvc"): "PersistentVolumeClaimList",
            }.get(base, "PodList")
            return {
                "apiVersion": "v1",
                "kind": kind,
                "metadata": {"resourceVersion": "9", "continue": ""},
                "items": deepcopy(items),
            }
        if method == "GET":
            return deepcopy(self.values.get(path))
        if method == "POST":
            assert path == self.path("sandbox")
            value = deepcopy(body)
            value["metadata"].update(uid="sandbox-uid", resourceVersion="1", generation=1)
            self.values[path + "/" + body["metadata"]["name"]] = value
            claim = deepcopy(body["spec"]["volumeClaimTemplates"][0])
            claim.update(apiVersion="v1", kind="PersistentVolumeClaim")
            claim["metadata"].update(
                name="outputs-" + body["metadata"]["name"],
                namespace=self.target.namespace,
                uid="pvc-uid",
                resourceVersion="1",
                ownerReferences=[self.owner()],
            )
            self.values[self.path("pvc", claim["metadata"]["name"])] = claim
            if self.fail == "lost-create":
                self.fail = None
                raise TimeoutError("synthetic lost create response")
            return deepcopy(value)
        if method == "PUT":
            old = self.values[path]
            assert old["metadata"]["uid"] == body["metadata"]["uid"]
            assert old["metadata"]["resourceVersion"] == body["metadata"]["resourceVersion"]
            value = deepcopy(body)
            value["metadata"]["resourceVersion"] = str(int(old["metadata"]["resourceVersion"]) + 1)
            if value["kind"] == "Sandbox":
                value["metadata"]["generation"] += 1
                if value["spec"]["operatingMode"] == "Running":
                    self.make_pod(value)
                else:
                    self.values.pop(self.path("pod", value["metadata"]["name"]), None)
                    value["status"] = {
                        "conditions": [
                            {
                                "type": "Suspended",
                                "status": "True",
                                "reason": "PodTerminated",
                                "observedGeneration": value["metadata"]["generation"],
                            }
                        ]
                    }
            self.values[path] = value
            if value["kind"] == "Pod" and self.fail == "lost-release":
                self.fail = None
                raise TimeoutError("synthetic lost release response")
            return deepcopy(value)
        if method == "DELETE":
            old = self.values[path]
            assert body["preconditions"] == {
                "uid": old["metadata"]["uid"],
                "resourceVersion": old["metadata"]["resourceVersion"],
            }
            self.values.pop(path)
            return {"kind": "Status", "status": "Success"}
        raise AssertionError((method, path))

    @staticmethod
    def owner():
        return {
            "apiVersion": API_VERSION,
            "kind": "Sandbox",
            "uid": "sandbox-uid",
            "controller": True,
        }

    def make_pod(self, sandbox, uid="pod-uid"):
        pod = deepcopy(sandbox["spec"]["podTemplate"])
        pod.update(apiVersion="v1", kind="Pod", status={"phase": "Pending"})
        pod["metadata"].update(
            name=sandbox["metadata"]["name"],
            namespace=self.target.namespace,
            uid=uid,
            resourceVersion="1",
            ownerReferences=[self.owner()],
        )
        self.values[self.path("pod", pod["metadata"]["name"])] = pod
        return pod

    def terminal(self, operation_id):
        pod = self.values[self.path("pod", "tessera-sb-" + operation_id)]
        pod["spec"]["nodeName"] = "node-one"
        pod["status"] = {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "agent",
                    "state": {
                        "terminated": {"exitCode": 0, "finishedAt": datetime.now(UTC).isoformat()}
                    },
                }
            ],
        }


def setup(tmp_path, *, target_project_id=None):
    state, execution, owner, worker, project, task = _setup(tmp_path)
    plan, witness = approved_plan(owner["org_id"], target_project_id or project["id"])
    store = Store(state)
    current = store.get_current_task_plan(owner, task["id"], task["current_plan_digest"])
    spec = current["execution_spec"] | {"profile": "agent-sandbox", "sandbox": plan.model_dump()}
    payload = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": 100,
        "max_duration_seconds": 60,
        "execution_spec": spec,
    }
    task = store.revise_task_plan(
        owner, task["id"], "sandbox-plan", canonical_digest(payload), payload
    )[1]
    task = store.approve_task(
        owner,
        task["id"],
        "sandbox-approval",
        "approval",
        task["revision"],
        task["current_plan_digest"],
    )[1]
    with _connect(state / "tessera.sqlite3") as connection:
        snapshot_id = connection.execute(
            "SELECT id FROM source_snapshots WHERE project_id=?", (project["id"],)
        ).fetchone()[0]
    grant_body = _grant_payload(task, worker, {"id": snapshot_id})
    grant = execution.issue_execution_grant(
        owner, project["id"], "sandbox-grant", canonical_digest(grant_body), grant_body
    )[1]
    lease = execution.acquire_execution_lease(worker, grant["id"], "sandbox-lease", "lease")[1]
    cluster = FakeCluster(plan)
    backend = AgentSandboxBackend(cluster, lambda target: deepcopy(witness))
    cluster.backend = backend
    adapter = AgentSandboxStore(state, backend)
    return SimpleNamespace(
        state=state,
        execution=execution,
        owner=owner,
        worker=worker,
        project=project,
        grant=grant,
        lease=lease,
        cluster=cluster,
        backend=backend,
        adapter=adapter,
        body={"expected_epoch": lease["epoch"]},
    )


def create(f):
    result = f.adapter.create(f.worker, f.lease["id"], "create", f.body)[1]
    f.operation_id = result["operation_id"]
    return result


def start(f):
    create(f)
    assert (
        f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)[1]["phase"]
        == "awaiting_gated_pod"
    )
    return f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)[1]


def observe(f, key="observe"):
    return f.adapter.action(f.owner, f.operation_id, key, "observe", f.body)[1]


def stop(f):
    return f.adapter.action(f.owner, f.operation_id, "stop", "stop", f.body)[1]


def durable_outputs(f):
    data = b"synthetic durable result"
    body = {
        "expected_epoch": f.lease["epoch"],
        "operation_id": f.operation_id,
        "relative_path": "result.txt",
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_base64": base64.b64encode(data).decode(),
    }
    artifact = ArtifactStore(f.state).upload_artifact(
        f.worker, f.lease["id"], "upload", canonical_digest(body), body
    )[1]
    placement_body = {
        "expected_epoch": f.lease["epoch"],
        "operation_id": f.operation_id,
        "artifacts": [{key: artifact[key] for key in ("id", "sha256", "size")}],
    }
    ProjectArtifactStore(f.state).commit_project_artifacts(
        f.worker, f.lease["id"], "placement", canonical_digest(placement_body), placement_body
    )
    manifest_body = {
        "expected_epoch": f.lease["epoch"],
        "operation_id": f.operation_id,
        "artifacts": [{key: artifact[key] for key in ("id", "relative_path", "sha256", "size")}],
    }
    record = ArtifactManifestStore(f.state).create_manifest(
        f.worker, f.lease["id"], "manifest", canonical_digest(manifest_body), manifest_body
    )[1]
    return {"manifest_id": record["id"], "manifest_digest": record["manifest_digest"]}, artifact


def test_authenticated_positive_direct_sandbox_flow_and_custody(tmp_path):
    f = setup(tmp_path)
    client = TestClient(create_app(f.state, agent_sandbox_backend=f.backend))
    worker_headers = {"Authorization": "Bearer worker-token", "Idempotency-Key": "api-create"}
    response = client.post(
        f"/worker/agent-sandbox/leases/{f.lease['id']}", headers=worker_headers, json=f.body
    )
    assert response.status_code == 200, response.text
    f.operation_id = response.json()["operation_id"]
    assert response.json()["phase"] == "created_suspended"
    expected = f.cluster.values[f.cluster.path("sandbox", "tessera-sb-" + f.operation_id)]
    assert expected["spec"]["operatingMode"] == "Suspended"
    assert expected["spec"]["shutdownPolicy"] == "Retain"
    assert "templateRef" not in expected["spec"] and "warmPoolRef" not in expected["spec"]
    for action in ("run", "release"):
        worker_headers["Idempotency-Key"] = "api-" + action
        response = client.post(
            f"/worker/agent-sandboxes/{f.operation_id}/{action}",
            headers=worker_headers,
            json=f.body,
        )
        assert response.status_code == 200, response.text
    assert response.json()["phase"] == "release_sent"
    pod = f.cluster.values[f.cluster.path("pod", expected["metadata"]["name"])]
    assert pod["spec"]["schedulingGates"] == []
    f.cluster.terminal(f.operation_id)
    observe(f)
    body, _ = durable_outputs(f)
    assert stop(f)["phase"] == "stop_sent_not_confirmed"
    assert observe(f, "stopped")["quiescent"] is True
    result = f.adapter.cleanup(f.owner, f.operation_id, "cleanup", body)[1]
    assert result["cleanup_acknowledged"] is True
    assert result["scientific_success"] is False
    with _connect(f.state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM kubernetes_dispatches").fetchone()[0] == 0


def test_lost_create_response_lookup_only_and_uid_replacement_fenced(tmp_path):
    f = setup(tmp_path)
    f.cluster.fail = "lost-create"
    assert create(f)["phase"] == "uncertain"
    result = f.adapter.create(f.worker, f.lease["id"], "retry-create", f.body)[1]
    assert result["phase"] == "created_suspended"
    assert sum(method == "POST" for method, _ in f.cluster.calls) == 1
    sandbox = f.cluster.values[f.cluster.path("sandbox", "tessera-sb-" + f.operation_id)]
    sandbox["metadata"]["uid"] = "replacement-sandbox"
    assert (
        f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)[1]["phase"] == "uncertain"
    )
    assert not any(method == "PUT" for method, _ in f.cluster.calls)


def test_lost_release_is_never_retried_and_replacements_keep_gate(tmp_path):
    f = setup(tmp_path)
    f.cluster.fail = "lost-release"
    assert start(f)["phase"] == "uncertain"
    calls = len(f.cluster.calls)
    f.adapter.action(f.worker, f.operation_id, "retry", "release", f.body)
    assert len(f.cluster.calls) == calls
    sandbox = f.cluster.values[f.cluster.path("sandbox", "tessera-sb-" + f.operation_id)]
    replacement = f.cluster.make_pod(sandbox, "replacement-pod")
    assert observe(f)["quiescent"] is False
    assert replacement["spec"]["schedulingGates"] == [{"name": GATE}]
    f.adapter.action(f.worker, f.operation_id, "replacement-release", "release", f.body)
    assert replacement["spec"]["schedulingGates"] == [{"name": GATE}]


@pytest.mark.parametrize(
    "fault",
    [
        "runtime-missing",
        "runtime-handler",
        "controller-image",
        "crd",
        "additive",
        "no-egress",
        "witness",
    ],
)
def test_missing_runtime_additive_network_or_pin_drift_prevents_create(tmp_path, fault):
    f = setup(tmp_path)
    if fault == "runtime-missing":
        f.cluster.values.pop(f.cluster.runtime_path)
    elif fault == "runtime-handler":
        f.cluster.values[f.cluster.runtime_path]["handler"] = "runc"
    elif fault == "controller-image":
        f.cluster.values[f.cluster.controller_path]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ] = "controller:latest"
    elif fault == "crd":
        f.cluster.values[
            "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/sandboxes.agents.x-k8s.io"
        ]["spec"] = {}
    elif fault == "additive":
        f.cluster.policies.append(
            resource(
                "NetworkPolicy",
                "allow",
                "allow",
                {"podSelector": {}, "egress": [{}], "policyTypes": ["Egress"]},
            )
        )
    elif fault == "no-egress":
        f.cluster.policies[0]["spec"]["policyTypes"] = ["Ingress"]
    else:
        f.backend.verify_witness = None
    with pytest.raises(Conflict, match="prerequisites"):
        create(f)
    assert not any(method != "GET" for method, _ in f.cluster.calls)


def test_policy_and_blueprint_drift_between_create_and_release(tmp_path):
    f = setup(tmp_path)
    create(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    f.cluster.policies.append(
        resource("NetworkPolicy", "allow", "allow", {"podSelector": {}, "ingress": [{}]})
    )
    with pytest.raises(SandboxUnsafe, match="additive"):
        f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)
    f.cluster.policies.pop()
    sandbox = f.cluster.values[f.cluster.path("sandbox", "tessera-sb-" + f.operation_id)]
    sandbox["spec"]["podTemplate"]["spec"]["containers"][0]["image"] = "mutable:latest"
    assert observe(f)["phase"] == "uncertain"


def test_revocation_at_release_boundary_never_removes_gate(tmp_path, monkeypatch):
    f = setup(tmp_path)
    create(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    original = f.adapter._effect

    def revoke_before_effect(*args, **kwargs):
        f.execution.revoke_execution_grant(
            f.owner, f.grant["id"], "revoke", "revoke", f.grant["revision"]
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(f.adapter, "_effect", revoke_before_effect)
    with pytest.raises(Conflict):
        f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)
    pod = f.cluster.values[f.cluster.path("pod", "tessera-sb-" + f.operation_id)]
    assert pod["spec"]["schedulingGates"] == [{"name": GATE}]
    assert f.adapter.status(f.owner, f.operation_id)["release_attempted"] is True


def test_revocation_at_create_boundary_replay_stale_epoch_and_human_stop(tmp_path, monkeypatch):
    f = setup(tmp_path)
    original = f.adapter._effect

    def revoke_before_effect(*args, **kwargs):
        f.execution.revoke_execution_grant(
            f.owner, f.grant["id"], "revoke", "revoke", f.grant["revision"]
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(f.adapter, "_effect", revoke_before_effect)
    with pytest.raises(Conflict):
        create(f)
    assert not any(method == "POST" for method, _ in f.cluster.calls)
    with pytest.raises(Conflict):
        create(f)


def test_tenant_and_worker_identity_fences(tmp_path):
    f = setup(tmp_path)
    create(f)
    with pytest.raises(NotFound):
        with _connect(f.state / "tessera.sqlite3") as connection:
            f.adapter._bound(connection, f.owner | {"org_id": "other-tenant"}, f.operation_id)
    with pytest.raises(Forbidden):
        with _connect(f.state / "tessera.sqlite3") as connection:
            f.adapter._bound(connection, f.worker | {"id": "other-worker"}, f.operation_id)
    with pytest.raises(Forbidden):
        f.adapter.action(
            f.worker, f.operation_id, "stale", "run", {"expected_epoch": f.lease["epoch"] + 1}
        )


@pytest.mark.parametrize(
    "fault",
    ["unknown-stop", "pagination", "misowned", "duplicate", "node-unknown", "replacement-ungated"],
)
def test_unknown_stop_pod_discovery_and_node_evidence_never_enable_cleanup(tmp_path, fault):
    f = setup(tmp_path)
    start(f)
    name = "tessera-sb-" + f.operation_id
    if fault == "unknown-stop":
        stop(f)
    elif fault == "pagination":
        f.cluster.fail = "discovery"
    elif fault == "misowned":
        f.cluster.values[f.cluster.path("pod", name)]["metadata"]["ownerReferences"] = []
    elif fault == "duplicate":
        pod = deepcopy(f.cluster.values[f.cluster.path("pod", name)])
        pod["metadata"].update(name="duplicate", uid="duplicate-pod")
        f.cluster.values[f.cluster.path("pod", "duplicate")] = pod
    elif fault == "node-unknown":
        f.cluster.terminal(f.operation_id)
        f.cluster.values["/api/v1/nodes/node-one"]["status"]["conditions"][0]["status"] = "Unknown"
    else:
        sandbox = f.cluster.values[f.cluster.path("sandbox", name)]
        f.cluster.make_pod(sandbox, "replacement-pod")["spec"]["schedulingGates"] = []
    assert observe(f)["quiescent"] is False
    with pytest.raises((Conflict, SandboxUnsafe)):
        f.adapter.cleanup(
            f.owner,
            f.operation_id,
            "cleanup",
            {"manifest_id": "missing", "manifest_digest": "a" * 64},
        )
    assert not any(method == "DELETE" for method, _ in f.cluster.calls)


@pytest.mark.parametrize(
    "fault", ["missing", "checksum", "retention", "timeout", "storage-missing"]
)
def test_cleanup_requires_exact_manifest_active_placement_and_retains_on_failure(tmp_path, fault):
    f = setup(tmp_path)
    start(f)
    f.cluster.terminal(f.operation_id)
    observe(f)
    body, artifact = durable_outputs(f)
    stop(f)
    assert observe(f, "stopped")["quiescent"]
    if fault == "missing":
        body["manifest_id"] = "missing"
    elif fault == "checksum":
        body["manifest_digest"] = "0" * 64
    elif fault == "retention":
        with _connect(f.state / "tessera.sqlite3") as connection:
            connection.execute(
                "UPDATE project_artifact_retention SET state='degraded' WHERE artifact_id=?",
                (artifact["id"],),
            )
    elif fault == "storage-missing":
        with _connect(f.state / "tessera.sqlite3") as connection:
            connection.execute(
                "DELETE FROM project_artifact_placements WHERE artifact_id=?", (artifact["id"],)
            )
    else:
        f.cluster.fail = "delete"
    if fault == "timeout":
        assert (
            f.adapter.cleanup(f.owner, f.operation_id, "cleanup", body)[1]["cleanup_acknowledged"]
            is False
        )
    else:
        with pytest.raises(Conflict):
            f.adapter.cleanup(f.owner, f.operation_id, "cleanup", body)
        assert not any(method == "DELETE" for method, _ in f.cluster.calls)
    assert f.cluster.path("pvc", "outputs-tessera-sb-" + f.operation_id) in f.cluster.values


def test_api_rejects_unauthenticated_and_default_backend_never_creates(tmp_path):
    f = setup(tmp_path)
    client = TestClient(create_app(f.state))
    path = f"/worker/agent-sandbox/leases/{f.lease['id']}"
    assert (
        client.post(path, json=f.body, headers={"Idempotency-Key": "missing-auth"}).status_code
        == 401
    )
    response = client.post(
        path,
        json=f.body,
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "default"},
    )
    assert response.status_code == 409
    assert not f.cluster.calls


def test_collection_handoff_is_typed_fresh_and_not_a_job(tmp_path):
    f = setup(tmp_path)
    start(f)
    with pytest.raises(Conflict):
        f.adapter.collect(f.worker, f.operation_id, "active-collect", f.body)
    f.cluster.terminal(f.operation_id)
    observe(f)
    stop(f)
    result = f.adapter.collect(f.worker, f.operation_id, "collect", f.body)[1]
    assert result["handoff"]["handle"]["target"]["adapter_kind"] == "agent-sandbox"
    assert result["handoff"]["handle"]["provider_id"] == "sandbox-uid"
    assert result["pvc"]["uid"] == "pvc-uid"
    assert result["bytes_collected"] is False
    assert result["quiescent"] is True


def test_unknown_owned_pvc_prevents_garbage_collection_of_uncopied_bytes(tmp_path):
    f = setup(tmp_path)
    start(f)
    f.cluster.terminal(f.operation_id)
    observe(f)
    body, _ = durable_outputs(f)
    stop(f)
    extra = resource("PersistentVolumeClaim", "extra", "extra-pvc", {}, f.cluster.target.namespace)
    extra["metadata"]["ownerReferences"] = [f.cluster.owner()]
    f.cluster.values[f.cluster.path("pvc", "extra")] = extra
    assert observe(f, "stopped")["quiescent"] is False
    with pytest.raises(SandboxUnsafe, match="unknown Sandbox-owned PVC"):
        f.adapter.cleanup(f.owner, f.operation_id, "cleanup", body)
    assert not any(method == "DELETE" for method, _ in f.cluster.calls)


def test_initial_lease_window_is_frozen_and_expiry_observation_requests_stop(tmp_path):
    f = setup(tmp_path)
    start(f)
    sandbox = f.cluster.values[f.cluster.path("sandbox", "tessera-sb-" + f.operation_id)]
    assert datetime.fromisoformat(sandbox["spec"]["shutdownTime"]) == datetime.fromisoformat(
        f.lease["expires_at"].replace("Z", "+00:00")
    )
    with _connect(f.state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (f.lease["id"],),
        )
    result = observe(f)
    assert result["desired_state"] == "cancel_requested"
    assert result["phase"] == "stop_sent_not_confirmed"
    assert result["quiescent"] is False
    with pytest.raises(Conflict):
        f.adapter.action(f.worker, f.operation_id, "expired-release", "release", f.body)
    assert stop(f)["phase"] == "stop_sent_not_confirmed"


def test_revoked_create_and_run_replays_do_not_reuse_old_authority(tmp_path):
    f = setup(tmp_path)
    create(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    f.execution.revoke_execution_grant(
        f.owner, f.grant["id"], "revoke", "revoke", f.grant["revision"]
    )
    writes = sum(method != "GET" for method, _ in f.cluster.calls)
    with pytest.raises(Conflict):
        create(f)
    with pytest.raises(Conflict):
        f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    assert sum(method != "GET" for method, _ in f.cluster.calls) == writes
    assert stop(f)["phase"] == "stop_sent_not_confirmed"


@pytest.mark.parametrize(
    "fault", ["host-network", "token", "sidecar", "image", "privilege", "env", "hostpath", "gate"]
)
def test_unapproved_pod_mutation_never_releases(tmp_path, fault):
    f = setup(tmp_path)
    create(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    pod = f.cluster.values[f.cluster.path("pod", "tessera-sb-" + f.operation_id)]
    spec = pod["spec"]
    if fault == "host-network":
        spec["hostNetwork"] = True
    elif fault == "token":
        spec["automountServiceAccountToken"] = True
    elif fault == "sidecar":
        spec["containers"].append(deepcopy(spec["containers"][0]) | {"name": "sidecar"})
    elif fault == "image":
        spec["containers"][0]["image"] = "image:latest"
    elif fault == "privilege":
        spec["containers"][0]["securityContext"]["privileged"] = True
    elif fault == "env":
        spec["containers"][0]["env"] = [{"name": "UNAPPROVED", "value": "test"}]
    elif fault == "hostpath":
        spec["volumes"] = [{"name": "outputs", "hostPath": {"path": "/"}}]
    else:
        spec["schedulingGates"] = []
    with pytest.raises(SandboxUnsafe):
        f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)
    assert not any(method == "PUT" and "/pods/" in path for method, path in f.cluster.calls)


@pytest.mark.parametrize(
    "fault", ["replayed-token", "changed-version", "missing-version", "403", "410"]
)
def test_complete_pagination_is_required_and_http_denials_are_not_absence(
    tmp_path, monkeypatch, fault
):
    f = setup(tmp_path)
    start(f)
    original = f.cluster.request

    def faulty(method, path, body=None, **kwargs):
        if method == "GET" and "pods?" in path:
            if fault in {"403", "410"}:
                raise RuntimeError("synthetic HTTP " + fault)
            value = original(method, path, body)
            value["items"] = []
            value["metadata"]["continue"] = "page-two"
            if "continue=" in path:
                value["metadata"]["continue"] = ""
                if fault == "changed-version":
                    value["metadata"]["resourceVersion"] = "10"
            if fault == "replayed-token":
                value["metadata"]["continue"] = "page-two"
            if fault == "missing-version":
                value["metadata"].pop("resourceVersion")
            return value
        return original(method, path, body)

    monkeypatch.setattr(f.cluster, "request", faulty)
    assert observe(f)["phase"] == "uncertain"
    assert f.adapter.status(f.owner, f.operation_id)["quiescent"] is False


@pytest.mark.parametrize("held", [False, True])
def test_v56_migration_empty_binding_and_backup_freshness(tmp_path, held):
    from tessera_control.store import initialize_state, migrate_state

    state = tmp_path / "migration"
    initialize_state(state)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE agent_sandbox_bindings")
        connection.execute("UPDATE schema_meta SET schema_version=55")
        if held:
            connection.execute("UPDATE control_recovery SET state='held_restore'")
        connection.execute(
            "UPDATE operator_backup_health SET last_success_at='prior-success',"
            "last_backup_id='prior',last_failure_code='prior-failure'"
        )
    assert migrate_state(state) == 56
    assert migrate_state(state) == 56
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM agent_sandbox_bindings").fetchone()[0] == 0
        health = connection.execute("SELECT * FROM operator_backup_health").fetchone()
        assert health["last_success_at"] == ("prior-success" if held else None)
        assert health["last_failure_code"] == "prior-failure"


def test_backup_restore_preserves_release_fence_but_holds_mutations(tmp_path):
    from tessera_control.recovery import backup_state, restore_state

    f = setup(tmp_path)
    start(f)
    backup = tmp_path / "sandbox-backup"
    backup_state(f.state, backup)
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    adapter = AgentSandboxStore(restored, f.backend)
    with _connect(restored / "tessera.sqlite3") as connection:
        row = connection.execute(
            "SELECT state_json FROM agent_sandbox_bindings WHERE operation_id=?", (f.operation_id,)
        ).fetchone()
        import json

        assert json.loads(row[0])["release_attempted"] is True
    with pytest.raises(Forbidden, match="held"):
        adapter.action(_owner(restored), f.operation_id, "restored-stop", "stop", f.body)


def test_cli_uses_authenticated_http_not_yaml_or_provider_shortcut(tmp_path, monkeypatch, capsys):
    from tessera_control.agent_sandbox_cli import run_agent_sandbox
    from tessera_control.cli import parser
    from tessera_worker.control_channel import HttpControlChannel

    credential = tmp_path / "synthetic-token"
    credential.write_text("synthetic-worker-token")
    credential.chmod(0o600)
    calls = []

    def post(channel, path, body, key):
        calls.append((path, body, key))
        return {"phase": "created_suspended"}

    monkeypatch.setattr(HttpControlChannel, "post", post)
    args = parser().parse_args(
        [
            "agent-sandbox",
            "create",
            "--control-url",
            "http://127.0.0.1:8765",
            "--credential-file",
            str(credential),
            "--idempotency-key",
            "cli",
            "--lease-id",
            "lease-test",
            "--epoch",
            "1",
        ]
    )
    assert run_agent_sandbox(args) == 0
    assert calls == [("/worker/agent-sandbox/leases/lease-test", {"expected_epoch": 1}, "cli")]
    assert "synthetic-worker-token" not in capsys.readouterr().out


@pytest.mark.parametrize("profile", ["local-cpu", "kubernetes-opencode", "agent-sandbox"])
def test_plan_serialization_preserves_old_profiles_and_keeps_sandbox_pins(profile):
    from tessera_control.models import ReviseTaskPlan

    spec = {
        "argv": ["true"],
        "input_snapshot_digest": "a" * 64,
        "pinned_skill_digests": [],
        "output_paths": ["result.txt"],
        "profile": profile,
        "network_enabled": False,
        "max_output_bytes": 1024,
    }
    if profile == "agent-sandbox":
        spec["sandbox"] = approved_plan()[0].model_dump()
    body = ReviseTaskPlan.model_validate(
        {
            "expected_revision": 1,
            "title": "Plan",
            "purpose": "Serialization compatibility",
            "budget_cents": 100,
            "max_duration_seconds": 60,
            "execution_spec": spec,
        }
    )
    serialized = body.model_dump(mode="json")["execution_spec"]
    assert ("sandbox" in serialized) == (profile == "agent-sandbox")
    if profile == "agent-sandbox":
        assert serialized["sandbox"] == spec["sandbox"]


def test_other_project_namespace_cannot_receive_an_execution_grant(tmp_path):
    with pytest.raises(Conflict, match="another tenant/project"):
        setup(tmp_path, target_project_id="other-project")


def test_authenticated_release_policy_denial_is_safe_409_and_preserves_gate(tmp_path):
    f = setup(tmp_path)
    create(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    f.cluster.policies.append(
        resource(
            "NetworkPolicy",
            "additive-allow",
            "allow-policy",
            {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]},
        )
    )
    client = TestClient(create_app(f.state, agent_sandbox_backend=f.backend))
    response = client.post(
        f"/worker/agent-sandboxes/{f.operation_id}/release",
        json=f.body,
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "denied-release"},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "sandbox_evidence_unresolved"}
    pod = f.cluster.values[f.cluster.path("pod", "tessera-sb-" + f.operation_id)]
    assert pod["spec"]["schedulingGates"] == [{"name": GATE}]
    assert f.adapter.status(f.owner, f.operation_id)["release_attempted"] is False
