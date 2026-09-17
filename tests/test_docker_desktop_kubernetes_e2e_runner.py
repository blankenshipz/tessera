"""CLI outcome semantics for the retained Docker Desktop Kubernetes fixture."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def runner_module():
    path = Path(__file__).parents[1] / "scripts" / "check_docker_desktop_kubernetes_e2e.py"
    spec = importlib.util.spec_from_file_location("docker_desktop_kubernetes_e2e_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_reports_skipped_without_subprocesses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner_module
) -> None:
    monkeypatch.setattr(sys, "argv", ["runner"])

    assert runner_module.main() == 1

    assert json.loads(capsys.readouterr().out)["fixture_verdict"] == "skipped"


def test_cli_reports_passed_without_replacing_operational_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner_module
) -> None:
    class Evidence:
        namespace = "fixture"
        namespace_uid = "namespace-uid"
        pvc = "outputs"
        pvc_uid = "pvc-uid"
        pvc_spec_sha256 = "a" * 64
        job = "job"
        job_uid = "job-uid"
        pod_uids = ["pod-uid"]
        collector = "collector"
        collector_uid = "collector-uid"
        collector_image_id = "image-id"
        image = type(
            "Image",
            (),
            {
                "reference": "example@sha256:" + "b" * 64,
                "manifest_digest": "sha256:" + "b" * 64,
                "config_digest": "sha256:" + "c" * 64,
                "archive_sha256": "d" * 64,
            },
        )()
        output_bytes = b"fixture output\n"
        output_sha256 = "e" * 64
        job_state = "succeeded"

    monkeypatch.setattr(runner_module, "target_from_kubeconfig", lambda _path: object())
    monkeypatch.setattr(runner_module, "new_nonce", lambda: "fixture")
    monkeypatch.setattr(runner_module, "build_and_load_exact_image", lambda _root, _nonce: object())
    monkeypatch.setattr(
        runner_module,
        "DockerDesktopKubernetesAdapter",
        lambda _target, _journal: type(
            "Adapter", (), {"run": lambda self, _image, _nonce: Evidence()}
        )(),
    )
    monkeypatch.setattr(sys, "argv", ["runner", "--run", "--kubeconfig", "/tmp/fixture-kubeconfig"])

    assert runner_module.main() == 0

    result = json.loads(capsys.readouterr().out)
    assert result["fixture_verdict"] == "passed"
    assert result["state"] == "succeeded"
