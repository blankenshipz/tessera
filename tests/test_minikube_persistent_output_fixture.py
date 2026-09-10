from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tessera_worker.kubernetes_contracts import KubernetesJobBinding, canonical_json
from tessera_worker.minikube_persistent_output_fixture import (
    HELLO_BYTES,
    FixtureResult,
    PersistentOutputTarget,
    fixture_exit_code,
    fixture_manifest,
    hello_binding,
    run_fixture,
)

PVC_SPEC = {
    "accessModes": ["ReadWriteOnce"],
    "resources": {"requests": {"storage": "1Gi"}},
}
PVC_SPEC_SHA256 = hashlib.sha256(canonical_json(PVC_SPEC)).hexdigest()


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
        "pvc-fixture",
        PVC_SPEC_SHA256,
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
        output_pvc_uid="pvc-fixture",
        output_pvc_spec_sha256=PVC_SPEC_SHA256,
    )


class FakeRunner:
    def __init__(
        self, fixture_target: PersistentOutputTarget, fixture_binding: KubernetesJobBinding
    ) -> None:
        self.target = fixture_target
        self.binding = fixture_binding
        self.calls: list[tuple[str, ...]] = []
        self.created = False
        self.failure: BaseException | None = None
        self.pvc: object = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": fixture_target.pvc,
                "namespace": fixture_target.namespace,
                "uid": fixture_target.pvc_uid,
            },
            "spec": PVC_SPEC,
        }

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, tuple)
        assert kwargs["check"] is True and kwargs["text"] is True
        assert kwargs["capture_output"] is True
        assert isinstance(kwargs["timeout"], int) and kwargs["timeout"] > 0
        self.calls.append(argv)
        if self.failure is not None and argv[0] == "kubectl" and "get" in argv:
            raise self.failure
        if argv[0] == "minikube":
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert "--request-timeout=10s" in argv
        if "get" in argv and "namespace" in argv:
            name = argv[argv.index("namespace") + 1]
            uid = self.target.cluster_uid if name == "kube-system" else self.target.namespace_uid
            return self._result(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": name, "uid": uid},
                }
            )
        if "get" in argv and "pvc" in argv:
            return self._result(self.pvc)
        if "get" in argv and "job" in argv:
            if not self.created:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="Error from server (NotFound): jobs missing"
                )
            observed = fixture_manifest(self.target, self.binding)
            observed["metadata"]["uid"] = "job-fixture"
            observed["status"] = {"succeeded": 1}
            return self._result(observed)
        if "create" in argv:
            self.created = True
            observed = json.loads(str(kwargs["input"]))
            observed["metadata"]["uid"] = "job-fixture"
            observed["status"] = {"succeeded": 1}
            return self._result(observed)
        if "logs" in argv:
            assert "--tail=1" in argv and "--limit-bytes=1024" in argv
            return subprocess.CompletedProcess(argv, 0, HELLO_BYTES.decode(), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _result(self, value: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(self.calls[-1], 0, json.dumps(value), "")


def test_manifest_writes_only_hello_to_verified_explicit_pvc_mount(tmp_path: Path) -> None:
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
    annotations = manifest["metadata"]["annotations"]
    assert annotations["tessera.dev/output-pvc-uid"] == "pvc-fixture"
    assert annotations["tessera.dev/output-pvc-spec-sha256"] == PVC_SPEC_SHA256
    command = container["command"][-1]
    assert "hello.txt" in command and "hello world\\n" in command
    assert "test -z" in command
    assert "rm -f" not in command


def test_explicit_target_validation_rejects_missing_or_mismatched_inputs(tmp_path: Path) -> None:
    broken = replace(target(tmp_path), kubeconfig=Path("relative"))
    result = run_fixture(broken, binding(), tmp_path / "journal.sqlite3")
    assert result.state == "unavailable"

    with pytest.raises(ValueError, match="exactly match"):
        fixture_manifest(target(tmp_path), replace(binding(), output_pvc_uid="other"))


def test_missing_tools_skip_without_creating_a_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tessera_worker.minikube_persistent_output_fixture.shutil.which", lambda _name: None
    )
    runner = FakeRunner(target(tmp_path), binding())
    result = run_fixture(target(tmp_path), binding(), tmp_path / "journal.sqlite3", runner)
    assert result.state == "skipped"
    assert runner.calls == []


def test_verified_target_precedes_one_argv_create_and_reports_persistent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tessera_worker.minikube_persistent_output_fixture.shutil.which", lambda _name: "/fixture"
    )
    fixture_target = target(tmp_path)
    fixture_binding = binding()
    runner = FakeRunner(fixture_target, fixture_binding)

    result = run_fixture(fixture_target, fixture_binding, tmp_path / "journal.sqlite3", runner)

    assert result.state == "succeeded"
    assert result.output == "PVC fixture-output:/persistent-output/hello.txt"
    create_index = next(index for index, command in enumerate(runner.calls) if "create" in command)
    assert all("get" in command for command in runner.calls[1:create_index])
    assert not any("delete" in command for command in runner.calls)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            subprocess.CalledProcessError(
                1, ("kubectl",), stderr="Error from server (Forbidden): secret detail"
            ),
            "Kubernetes authentication or authorization failure",
        ),
        (
            subprocess.CalledProcessError(
                1, ("kubectl",), stderr="Unable to connect to the server: secret detail"
            ),
            "Kubernetes connectivity failure",
        ),
        (subprocess.TimeoutExpired(("kubectl",), 15), "Kubernetes connectivity or timeout failure"),
    ],
)
def test_precreate_kubernetes_failures_are_sanitized_and_never_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException, message: str
) -> None:
    monkeypatch.setattr(
        "tessera_worker.minikube_persistent_output_fixture.shutil.which", lambda _name: "/fixture"
    )
    runner = FakeRunner(target(tmp_path), binding())
    runner.failure = failure

    result = run_fixture(target(tmp_path), binding(), tmp_path / "journal.sqlite3", runner)

    assert result.state == "failed"
    assert result.message == f"{message}; no Job was created."
    assert "secret detail" not in result.message
    assert not any("create" in command for command in runner.calls)


def test_not_found_and_malformed_pvc_skip_or_fail_without_creating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tessera_worker.minikube_persistent_output_fixture.shutil.which", lambda _name: "/fixture"
    )
    fixture_target = target(tmp_path)
    runner = FakeRunner(fixture_target, binding())
    runner.failure = subprocess.CalledProcessError(
        1, ("kubectl",), stderr="Error from server (NotFound): persistentvolumeclaims missing"
    )
    result = run_fixture(fixture_target, binding(), tmp_path / "missing.sqlite3", runner)
    assert result.state == "skipped"
    assert not any("create" in command for command in runner.calls)

    runner = FakeRunner(fixture_target, binding())
    runner.pvc = {"not": "json Kubernetes PVC"}
    result = run_fixture(fixture_target, binding(), tmp_path / "malformed.sqlite3", runner)
    assert result.state == "failed"
    assert result.message == "Kubernetes target response was malformed; no Job was created."
    assert not any("create" in command for command in runner.calls)


@pytest.mark.parametrize("field", ["cluster_uid", "namespace_uid", "pvc_uid", "pvc_spec_sha256"])
def test_contacted_identity_and_pvc_spec_must_match_before_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(
        "tessera_worker.minikube_persistent_output_fixture.shutil.which", lambda _name: "/fixture"
    )
    fixture_target = target(tmp_path)
    runner = FakeRunner(fixture_target, binding())
    if field == "pvc_spec_sha256":
        runner.pvc = {**runner.pvc, "spec": {"accessModes": ["ReadOnlyMany"]}}
        fixture_binding = binding()
    else:
        changed = replace(fixture_target, **{field: f"wrong-{field}"})
        fixture_binding = replace(
            binding(),
            output_pvc_uid=changed.pvc_uid,
            output_pvc_spec_sha256=changed.pvc_spec_sha256,
        )
        result = run_fixture(changed, fixture_binding, tmp_path / f"{field}.sqlite3", runner)
        assert result.state == "failed"
        assert not any("create" in command for command in runner.calls)
        return

    result = run_fixture(fixture_target, fixture_binding, tmp_path / "pvc-spec.sqlite3", runner)
    assert result.state == "failed"
    assert not any("create" in command for command in runner.calls)


def test_exit_status_reserves_zero_for_verified_fixture_not_a_skip() -> None:
    assert fixture_exit_code(FixtureResult("succeeded", "verified")) == 0
    assert fixture_exit_code(FixtureResult("skipped", "missing")) == 2
    assert fixture_exit_code(FixtureResult("failed", "failed")) == 1
