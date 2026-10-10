"""Local real byte collection/API transfer with synthetic Kubernetes transport."""

import base64
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_agent_sandbox import setup, start, stop

from tessera_control.api import create_app
from tessera_control.store import _connect
from tessera_worker.agent_sandbox_outputs import deliver_stopped_sandbox
from tessera_worker.kubernetes_output_pod import run


def delivery_fixture(tmp_path, monkeypatch, *, stopped=True, fault=None):
    f = setup(tmp_path)
    start(f)
    if stopped:
        f.cluster.terminal(f.operation_id)
        f.adapter.action(f.owner, f.operation_id, "terminal", "observe", f.body)
        stop(f)
    with _connect(f.state / "tessera.sqlite3") as db:
        package = f.adapter._bound(db, f.worker, f.operation_id)[1]
    root = tmp_path / "pvc-bytes"
    root.mkdir()
    (root / "result.txt").write_bytes(b"real locally collected Sandbox bytes")
    custody = tmp_path / "custody"
    custody.mkdir(mode=0o700)
    api = TestClient(create_app(f.state, agent_sandbox_backend=f.backend))

    class Channel:
        def post(self, path, body, key):
            result = api.post(
                path,
                json=body,
                headers={"Authorization": "Bearer worker-token", "Idempotency-Key": key},
            )
            if result.status_code >= 400:
                raise ValueError(f"control rejected {path}: {result.status_code} {result.json()}")
            return result.json()

    request = f.cluster.request
    results, created = {}, []

    def transport(method, path, body=None, **kwargs):
        if method == "POST" and path == f.cluster.path("pod"):
            created.append(deepcopy(body))
            pod = deepcopy(body)
            pod["metadata"].update(uid="collector-uid", resourceVersion="1")
            pod["spec"]["nodeName"] = "node-one"
            pod["status"] = {
                "phase": "Succeeded",
                "containerStatuses": [
                    {
                        "name": "output",
                        "state": {
                            "terminated": {
                                "exitCode": 0,
                                "finishedAt": datetime.now(UTC).isoformat(),
                            }
                        },
                    }
                ],
            }
            if fault == "unknown-collector-stop":
                pod["status"].pop("containerStatuses")
            payload = json.loads(base64.b64decode(body["spec"]["containers"][0]["command"][4]))
            results[body["metadata"]["name"]] = run(
                "sandbox-collect", payload["identity"], payload["paths"], root
            )
            if fault == "checksum":
                results[body["metadata"]["name"]]["outputs"][0]["sha256"] = "0" * 64
            if fault == "sidecar":
                pod["spec"]["containers"].append(deepcopy(pod["spec"]["containers"][0]))
            f.cluster.values[path + "/" + body["metadata"]["name"]] = pod
            return deepcopy(pod)
        if method == "GET" and "/log?" in path:
            return deepcopy(results[path.split("/pods/", 1)[1].split("/log", 1)[0]])
        return request(method, path, body, **kwargs)

    monkeypatch.setattr(f.cluster, "request", transport)
    return f, package, custody, Channel(), created


def test_stopped_sandbox_pvc_bytes_delivered_to_real_private_artifact_placement(
    tmp_path, monkeypatch
):
    f, package, custody, channel, created = delivery_fixture(tmp_path, monkeypatch)
    receipt = deliver_stopped_sandbox(
        package, f.backend, channel, image="tessera@sha256:" + "7" * 64, custody_root=custody
    )
    assert receipt["status"] == "quarantined"
    assert receipt["operation_id"] == f.operation_id
    assert receipt["total_bytes"] == len(b"real locally collected Sandbox bytes")
    assert len(created) == 1
    pod = created[0]
    assert pod["spec"]["runtimeClassName"] == "gvisor"
    assert pod["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"] is True
    assert pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"] is True
    assert "job_uid" not in json.dumps(pod)
    assert (
        deliver_stopped_sandbox(
            package, f.backend, channel, image="tessera@sha256:" + "7" * 64, custody_root=custody
        )
        == receipt
    )
    assert len(created) == 1
    body = {"manifest_id": receipt["id"], "manifest_digest": receipt["manifest_digest"]}
    assert (
        f.adapter.cleanup(f.owner, f.operation_id, "cleanup-delivered", body)[1]["phase"]
        == "cleanup_delete_sent"
    )


@pytest.mark.parametrize("fault", ["not-stopped", "checksum", "sidecar", "unknown-collector-stop"])
def test_uncertain_or_mutated_sandbox_collection_never_publishes_manifest(
    tmp_path, monkeypatch, fault
):
    f, package, custody, channel, created = delivery_fixture(
        tmp_path, monkeypatch, stopped=fault != "not-stopped", fault=fault
    )
    with pytest.raises((ValueError, RuntimeError)):
        deliver_stopped_sandbox(
            package, f.backend, channel, image="tessera@sha256:" + "7" * 64, custody_root=custody
        )
    with _connect(f.state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0] == 0
    assert f.cluster.values[f.cluster.path("pvc", "outputs-tessera-sb-" + f.operation_id)]
    if fault == "not-stopped":
        assert created == []


def test_deliver_cli_routes_stopped_bytes_through_existing_transfer(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from tessera_control import agent_sandbox_cli, agent_sandbox_configuration, cli

    f, package, custody, channel, created = delivery_fixture(tmp_path, monkeypatch)
    private_package = tmp_path / "frozen-package.json"
    private_package.write_text(json.dumps(package))
    private_package.chmod(0o600)
    f.backend.operator_config = SimpleNamespace(
        target=f.cluster.target, collector_image="tessera@sha256:" + "7" * 64
    )
    monkeypatch.setattr(
        agent_sandbox_configuration, "load_agent_sandbox_backend", lambda *a: f.backend
    )
    monkeypatch.setattr(agent_sandbox_cli, "HttpControlChannel", lambda *a: channel)
    monkeypatch.setattr(
        agent_sandbox_cli, "read_private_credential", lambda *a: "synthetic-worker-token"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera",
            "agent-sandbox",
            "deliver",
            "--control-url",
            "https://synthetic.invalid",
            "--credential-file",
            str(tmp_path / "worker-ref"),
            "--operation-id",
            f.operation_id,
            "--epoch",
            str(package["epoch"]),
            "--idempotency-key",
            "cli-delivery",
            "--operator-config",
            str(tmp_path / "operator-ref"),
            "--operator-sha256",
            "a" * 64,
            "--package-file",
            str(private_package),
            "--custody-root",
            str(custody),
        ],
    )
    assert cli.main() == 0
    assert len(created) == 1
