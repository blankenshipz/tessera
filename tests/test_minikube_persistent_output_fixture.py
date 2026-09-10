from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tessera_worker.kubernetes_contracts import KubernetesJobBinding
from tessera_worker.minikube_persistent_output_fixture import (
    HELLO_BYTES,
    PersistentOutputTarget,
    fixture_manifest,
    hello_binding,
    run_fixture,
)


def target(tmp_path: Path) -> PersistentOutputTarget:
    kubeconfig = tmp_path / "kubeconfig"
    ca_file = tmp_path / "ca.pem"
    kubeconfig.write_text("fixture")
    ca_file.write_text("fixture-ca")
    return PersistentOutputTarget(
        kubeconfig,
        "minikube-fixture",
        "https://127.0.0.1:8443",
        ca_file,
        "tessera-fixture",
        "development-minikube-opencode-v1",
        "minikube-fixture",
        "fixture-output",
        "/persistent-output",
        "cluster-fixture",
        "namespace-fixture",
    )


def binding() -> KubernetesJobBinding:
    return hello_binding(
        operation_id="1" * 32,
        attempt_id="attempt-1",
        lease_id="lease-1",
        epoch=1,
        image="example.test/fixture@sha256:" + "a" * 64,
        output_pvc="fixture-output",
        output_mount="/persistent-output",
    )


def test_manifest_writes_only_hello_to_explicit_pvc_mount(tmp_path: Path) -> None:
    manifest = fixture_manifest(target(tmp_path), binding())
    pod = manifest["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert {
        "name": "persistent-output",
        "persistentVolumeClaim": {"claimName": "fixture-output"},
    } in pod["volumes"]
    assert {"name": "persistent-output", "mountPath": "/persistent-output"} in container[
        "volumeMounts"
    ]
    command = container["command"][-1]
    assert "hello.txt" in command and "hello world\\n" in command
    assert "test -z" in command
    assert "rm -f" not in command


def test_explicit_target_validation_rejects_missing_or_mismatched_inputs(tmp_path: Path) -> None:
    broken = target(tmp_path)
    broken = replace(broken, kubeconfig=Path("relative"))
    result = run_fixture(broken, binding(), tmp_path / "journal.sqlite3")
    assert result.state == "unavailable"

    wrong_profile = replace(target(tmp_path), profile="other")
    result = run_fixture(wrong_profile, binding(), tmp_path / "journal.sqlite3")
    assert result.state == "unavailable"

    with pytest.raises(ValueError, match="exactly match"):
        fixture_manifest(
            target(tmp_path),
            hello_binding(
                operation_id="1" * 32,
                attempt_id="attempt-1",
                lease_id="lease-1",
                epoch=1,
                image="example.test/fixture@sha256:" + "a" * 64,
                output_pvc="other",
                output_mount="/persistent-output",
            ),
        )


def test_missing_prerequisites_skip_without_creating_a_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tessera_worker.minikube_persistent_output_fixture.shutil.which", lambda _name: None
    )
    calls: list[tuple[object, ...]] = []

    def runner(*args, **kwargs):
        calls.append(args)
        raise AssertionError("runner must not be called")

    result = run_fixture(target(tmp_path), binding(), tmp_path / "journal.sqlite3", runner)
    assert result.state == "skipped"
    assert calls == []


def test_success_reports_human_readable_persistent_file_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tessera_worker.minikube_persistent_output_fixture.shutil.which", lambda _name: "/fixture"
    )
    fixture_target = target(tmp_path)
    fixture_binding = binding()
    observed = fixture_manifest(fixture_target, fixture_binding)
    observed["metadata"]["uid"] = "job-fixture"
    observed["status"] = {"succeeded": 1}
    job_exists = False
    calls: list[tuple[object, ...]] = []

    def runner(*args, **kwargs):
        nonlocal job_exists
        command = args
        calls.append(command)
        stdout = ""
        if "get" in command and "job" in command:
            if not job_exists:
                raise subprocess.CalledProcessError(1, command)
            stdout = json.dumps(observed)
        elif "create" in command:
            job_exists = True
            stdout = json.dumps(observed)
        elif "logs" in command:
            stdout = HELLO_BYTES.decode()
        return subprocess.CompletedProcess(command, 0, stdout, "")

    result = run_fixture(fixture_target, fixture_binding, tmp_path / "journal.sqlite3", runner)
    assert result.state == "succeeded"
    assert result.output == "PVC fixture-output:/persistent-output/hello.txt"
    assert any("create" in command for command in calls)
    assert not any("delete" in command for command in calls)
