"""The live restart fixture has an explicit no-effect default."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml


def test_restart_runner_skips_without_run(monkeypatch, capsys) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["runner"])
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)["fixture_verdict"] == "skipped"


def test_fixture_manifest_has_separate_kubernetes_documents(monkeypatch) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    manifest = module._manifest("tessera-control-e2e-fixture", "fixture", "fixture:image")
    documents = list(yaml.safe_load_all(manifest))
    assert len(documents) == 8
    assert {document["kind"] for document in documents} == {
        "Namespace",
        "ConfigMap",
        "PersistentVolumeClaim",
        "Deployment",
        "Service",
        "NetworkPolicy",
    }
    config = next(document for document in documents if document["kind"] == "ConfigMap")
    assert config["data"] == {
        "TESSERA_TRUSTED_HOST": "localhost",
        "TESSERA_PUBLIC_ORIGIN": "https://localhost",
    }
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    assert deployment["spec"]["template"]["metadata"]["labels"][module.LABEL] == "fixture"
    control = deployment["spec"]["template"]["spec"]["containers"][0]
    for probe in (control["readinessProbe"], control["livenessProbe"]):
        assert probe["httpGet"]["httpHeaders"] == [{"name": "Host", "value": "localhost"}]
    policies = {
        document["metadata"]["name"]
        for document in documents
        if document["kind"] == "NetworkPolicy"
    }
    assert policies == {
        "default-deny",
        "allow-labelled-control-clients",
        "allow-labelled-control-client-egress",
    }


def test_fixture_image_is_loaded_into_the_docker_desktop_containerd_store(
    monkeypatch, tmp_path
) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_image", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    commands: list[list[str]] = []
    expected_reference = "docker.io/library/tessera-control-restart:fixture"

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Os": "linux", "Architecture": "amd64"}])
        if command[:3] == ["docker", "image", "save"]:
            Path(command[4]).write_bytes(b"fixture image")
        if command[-3:] == ["images", "ls", "--quiet"]:
            return f"tessera-control-restart:fixture\n{expected_reference}\n"
        return ""

    monkeypatch.setattr(module, "_run", fake_run)
    reference = module._load_image(tmp_path, "fixture")
    assert reference == expected_reference
    assert commands[:2] == [
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--platform",
            "linux/amd64",
            "--target",
            "lab",
            "--tag",
            reference,
            str(tmp_path),
        ],
        ["docker", "image", "inspect", reference],
    ]
    assert commands[2][:4] == ["docker", "image", "save", "--output"]
    assert commands[2][-1] == reference
    assert [
        "docker",
        "exec",
        "-i",
        module.DOCKER_DESKTOP_KIND_NODE,
        "ctr",
        "--namespace",
        "k8s.io",
        "images",
        "import",
        "--base-name",
        reference,
        "-",
    ] in commands
    assert [
        "docker",
        "exec",
        module.DOCKER_DESKTOP_KIND_NODE,
        "crictl",
        "inspecti",
        reference,
    ] in commands


def test_cleanup_requires_the_fixture_namespace_label_and_uid(monkeypatch, tmp_path) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_cleanup", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "get" in command:
            return json.dumps({"metadata": {"labels": {module.LABEL: "other-run"}}})
        return ""

    monkeypatch.setattr(module, "_run", fake_run)
    assert (
        module._cleanup(
            tmp_path / "config", "tessera-control-e2e-fixture", "fixture", "expected-uid"
        )
        == "retained_cleanup_unverified"
    )
    assert not any("delete" in command for command in commands)


def test_cleanup_refuses_a_replacement_namespace_uid(monkeypatch, tmp_path) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_cleanup_uid", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return json.dumps(
            {"metadata": {"labels": {module.LABEL: "fixture"}, "uid": "replacement-uid"}}
        )

    monkeypatch.setattr(module, "_run", fake_run)
    assert (
        module._cleanup(
            tmp_path / "config", "tessera-control-e2e-fixture", "fixture", "expected-uid"
        )
        == "retained_cleanup_unverified"
    )
    assert not any("delete" in command for command in commands)


def test_fixture_requires_containerd_image_store(monkeypatch) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_store", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_run",
        lambda *_args, **_kwargs: json.dumps([["Backing Filesystem", "extfs"]]),
    )
    try:
        module._require_containerd_image_store()
    except module.FixtureError as error:
        assert "legacy image store" in str(error)
    else:
        raise AssertionError("legacy Docker image store was accepted")


def test_fixture_requires_a_running_docker_desktop_kind_cluster(monkeypatch) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_cluster", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_run", lambda *_args, **_kwargs: "State: running\nMode: kind\n")

    module._require_docker_desktop_kind_cluster()


def test_image_never_pull_is_reported_as_a_containerd_load_failure(monkeypatch) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_never_pull", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    assert module._reported_image_never_pull({"pod_events": [{"reason": "ErrImageNeverPull"}]})
    assert not module._reported_image_never_pull({"pod_events": [{"reason": "Pulled"}]})


def test_fixture_wait_requires_ready_and_live_health_probes(monkeypatch) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_health", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    requests: list[str] = []

    def fake_request(_port, path, _headers):
        requests.append(path)
        return (200, {"backend_identity": "opaque"})

    monkeypatch.setattr(module, "_request", fake_request)

    assert module._wait_ready(18765) == {"backend_identity": "opaque"}
    assert requests == ["/health/ready", "/health/live"]


def test_fixture_project_request_is_an_authenticated_json_post(monkeypatch) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_request", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    observed = {}

    class Response:
        status = 201

        def read(self):
            return b'{"id":"project"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, *, timeout):
        observed.update(
            method=request.method,
            body=request.data,
            authorization=request.get_header("Authorization"),
            content_type=request.get_header("Content-type"),
            timeout=timeout,
        )
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    assert module._request(
        18765,
        "/projects",
        {"Authorization": "Bearer fixture"},
        method="POST",
        payload={"name": "Fixture"},
    ) == (201, {"id": "project"})
    assert observed == {
        "method": "POST",
        "body": b'{"name": "Fixture"}',
        "authorization": "Bearer fixture",
        "content_type": "application/json",
        "timeout": 2,
    }


def test_fixture_requires_one_stable_project_created_event(monkeypatch) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_event", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    requests = []
    event = {"cursor": 12, "event_type": "project.created", "payload": {"name": "Fixture"}}

    def fake_request(port, path, headers, **_kwargs):
        requests.append((port, path, headers))
        return 200, {"events": [event]}

    monkeypatch.setattr(module, "_request", fake_request)
    assert (
        module._project_created_event(18765, {"id": "project"}, {"Authorization": "Bearer fixture"})
        == event
    )
    assert requests == [(18765, "/projects/project/labbook", {"Authorization": "Bearer fixture"})]


def test_fixture_parses_only_an_authorization_owner_header(monkeypatch) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_owner_header", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    assert module._owner_authorization("Authorization: Bearer fixture") == "Bearer fixture"
    for value in ("Bearer fixture", "Other: Bearer fixture", "Authorization: Basic fixture"):
        try:
            module._owner_authorization(value)
        except module.FixtureError:
            pass
        else:
            raise AssertionError("malformed fixture owner header was accepted")


def test_fixture_port_forward_is_restarted_after_the_control_pod_changes(
    monkeypatch, tmp_path
) -> None:
    path = Path(__file__).parents[1] / "scripts/check_kubernetes_control_plane_restart.py"
    spec = importlib.util.spec_from_file_location("control_restart_port_forward", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    commands = []

    class Forward:
        def poll(self):
            return None

        def terminate(self):
            commands.append(["terminate"])

        def wait(self, *, timeout):
            commands.append(["wait", str(timeout)])

    def fake_popen(command, **_kwargs):
        commands.append(command)
        return Forward()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    forward = module._start_port_forward(tmp_path / "config", "tessera-control-e2e-fixture", 18765)
    module._stop_port_forward(forward)

    assert commands == [
        [
            "kubectl",
            "--kubeconfig",
            str(tmp_path / "config"),
            "--context",
            module.CONTEXT,
            "--namespace",
            "tessera-control-e2e-fixture",
            "port-forward",
            "service/control",
            "18765:8765",
        ],
        ["terminate"],
        ["wait", "10"],
    ]
