"""Default checks for the single-active lab serving profile, without Kubernetes."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.deployment import DeploymentConfig, serving_config
from tessera_control.store import initialize_state


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    initialize_state(state)
    return state


def _remote() -> DeploymentConfig:
    return DeploymentConfig.remote(["control.example.test"], "https://control.example.test")


def test_remote_serving_requires_concrete_trusted_https_origin_and_preserves_local_defaults() -> (
    None
):
    assert serving_config("127.0.0.1", None, None) == DeploymentConfig.local()
    remote = serving_config("0.0.0.0", ["control.example.test"], "https://control.example.test")
    assert remote.profile == "remote-lab-v1"
    for hosts, origin in (
        ([], "https://control.example.test"),
        (["*"], "https://control.example.test"),
        (["control.example.test"], "http://control.example.test"),
        (["other.example.test"], "https://control.example.test"),
    ):
        with pytest.raises(ValueError):
            serving_config("0.0.0.0", hosts, origin)


def test_remote_host_filter_auth_and_browser_origin_remain_fail_closed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    client = TestClient(create_app(state, _remote()))
    good_host = {"Host": "control.example.test"}
    assert client.get("/health/live", headers=good_host).json() == {"status": "live"}
    assert client.get("/health/live", headers={"Host": "untrusted.example.test"}).status_code == 400
    assert client.get("/me", headers=good_host).status_code == 401
    assert client.get("/me", headers=good_host | owner).status_code == 200
    assert (
        client.get(
            "/health/live", headers=good_host | {"Origin": "https://control.example.test"}
        ).status_code
        == 403
    )


def test_readiness_is_distinct_and_backend_identity_and_state_survive_restart(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    first = TestClient(create_app(state))
    assert first.get("/health/live").json() == {"status": "live"}
    ready = first.get("/health/ready")
    assert ready.status_code == 200
    identity = ready.json()["backend_identity"]
    project = first.post(
        "/projects",
        headers=owner | {"Idempotency-Key": "durable-project"},
        json={"name": "Restart fixture"},
    )
    assert project.status_code == 201

    restarted = TestClient(create_app(state))
    after = restarted.get("/health/ready")
    assert after.status_code == 200
    assert after.json()["backend_identity"] == identity
    assert restarted.get("/projects", headers=owner).json()["projects"] == [project.json()]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; from pathlib import Path; "
                "from tessera_control.api import create_app; "
                "from tessera_control.deployment import backend_status; "
                "state=Path(sys.argv[1]); create_app(state); "
                "print(json.dumps(backend_status(state)))"
            ),
            str(state),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(child.stdout)["backend_identity"] == identity


def test_recovery_hold_and_schema_mismatch_are_unready_and_gate_authority(tmp_path: Path) -> None:
    state = _state(tmp_path)
    app = create_app(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE control_recovery SET state='held_restore' WHERE singleton=1")
    client = TestClient(app)
    held = client.get("/health/ready")
    assert held.status_code == 503
    assert held.json()["reason"] == "recovery_hold"
    assert (
        client.post(
            "/projects",
            headers=owner | {"Idempotency-Key": "held"},
            json={"name": "held"},
        ).status_code
        == 403
    )

    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE control_recovery SET state='active' WHERE singleton=1")
        connection.execute("UPDATE schema_meta SET schema_version=42 WHERE singleton=1")
    # Startup remains live enough to expose an explicit migration-required readiness state.
    mismatched = TestClient(create_app(state))
    assert mismatched.get("/health/live").status_code == 200
    schema = mismatched.get("/health/ready")
    assert schema.status_code == 503
    assert schema.json()["reason"] == "schema_mismatch"
    assert mismatched.get("/projects").status_code == 503


def test_lab_manifest_declares_single_writer_hardening_probes_and_client_policy() -> None:
    manifest = (Path(__file__).parents[1] / "deploy/kubernetes/lab-control-plane.yaml").read_text()
    for required in (
        "ReadWriteOnce",
        "replicas: 1",
        "type: Recreate",
        "automountServiceAccountToken: false",
        "runAsNonRoot: true",
        "allowPrivilegeEscalation: false",
        "readOnlyRootFilesystem: true",
        "path: /health/live",
        "path: /health/ready",
        "httpHeaders:",
        "value: tessera-control.tessera-lab.svc.cluster.local",
        "name: default-deny",
        "name: allow-labelled-control-clients",
        "name: allow-labelled-control-client-egress",
        "kind: Service",
    ):
        assert required in manifest

    documents = list(yaml.safe_load_all(manifest))
    config = next(document for document in documents if document["kind"] == "ConfigMap")
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    control = deployment["spec"]["template"]["spec"]["containers"][0]
    for probe in (control["readinessProbe"], control["livenessProbe"]):
        assert probe["httpGet"]["httpHeaders"] == [
            {"name": "Host", "value": config["data"]["TESSERA_TRUSTED_HOST"]}
        ]

    policies = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "NetworkPolicy"
    }
    assert policies["default-deny"]["spec"] == {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
    }
    client_egress = policies["allow-labelled-control-client-egress"]["spec"]
    assert client_egress["podSelector"] == {"matchLabels": {"tessera.io/control-client": "true"}}
    assert client_egress["egress"] == [
        {
            "to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "tessera-control"}}}],
            "ports": [{"protocol": "TCP", "port": 8765}],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        },
    ]


def test_compose_explicitly_uses_dev_target_and_lab_remains_opt_in() -> None:
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "compose.yaml").read_text())
    assert compose["services"]["control"]["build"] == {"context": ".", "target": "dev"}
    assert compose["services"]["test"]["build"] == {"context": ".", "target": "dev"}
    dockerfile = (root / "Dockerfile").read_text()
    assert " AS dev\n" in dockerfile
    assert " AS lab\n" in dockerfile
    assert dockerfile.index(" AS lab\n") < dockerfile.index(" AS dev\n")
