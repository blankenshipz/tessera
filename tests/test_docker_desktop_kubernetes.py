# ruff: noqa: E501
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from tessera_worker.docker_desktop_kubernetes import (
    RESULT_BYTES,
    RESULT_PATH,
    ExactImage,
    LiveKubernetesError,
    _collector_manifest,
    _pvc_spec_sha256,
    _run,
    _verify_bound_pvc,
    _verify_collector,
    _verify_oci_archive,
)


def _oci_archive(path: Path, tag: str, *, architecture: str = "amd64") -> tuple[str, str]:
    config = json.dumps({"architecture": architecture, "os": "linux"}).encode()
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = json.dumps({"schemaVersion": 2, "config": {"digest": config_digest}}).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": manifest_digest,
                    "annotations": {"org.opencontainers.image.ref.name": tag},
                }
            ],
        }
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, value in (
            ("index.json", index),
            (f"blobs/sha256/{config_digest[7:]}", config),
            (f"blobs/sha256/{manifest_digest[7:]}", manifest),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return manifest_digest, config_digest


def test_oci_image_verification_requires_exact_reference_digest_and_architecture(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "image.tar"
    manifest, config = _oci_archive(archive, "tessera-e2e-hello:fixture")
    image = _verify_oci_archive(archive, "tessera-e2e-hello:fixture")

    assert image.reference == f"tessera-e2e-hello:fixture@{manifest}"
    assert image.config_digest == config
    assert image.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    _oci_archive(archive, "tessera-e2e-hello:fixture", architecture="arm64")
    with pytest.raises(LiveKubernetesError, match="linux/amd64"):
        _verify_oci_archive(archive, "tessera-e2e-hello:fixture")


def test_oci_image_verification_can_use_the_node_canonical_repository(tmp_path: Path) -> None:
    archive = tmp_path / "image.tar"
    manifest, _ = _oci_archive(archive, "tessera-e2e-hello:fixture")

    image = _verify_oci_archive(
        archive, "tessera-e2e-hello:fixture", "docker.io/library/tessera-e2e-hello"
    )

    assert image.reference == f"docker.io/library/tessera-e2e-hello@{manifest}"


def test_collector_is_read_only_digest_pinned_and_never_pulls() -> None:
    manifest = _collector_manifest("tessera-e2e-fixture", "collector", "example@sha256:" + "a" * 64)
    container = manifest["spec"]["containers"][0]  # type: ignore[index]

    assert container["command"] == ["/hello", "--read", RESULT_PATH]
    assert container["imagePullPolicy"] == "Never"
    assert container["volumeMounts"] == [
        {"name": "outputs", "mountPath": "/outputs", "readOnly": True}
    ]
    assert manifest["spec"]["volumes"] == [  # type: ignore[index]
        {
            "name": "outputs",
            "persistentVolumeClaim": {"claimName": "outputs", "readOnly": True},
        }
    ]
    assert RESULT_BYTES == b"hello tessera kubernetes e2e\n"


def test_collector_observation_rejects_pvc_or_security_mutation() -> None:
    image = ExactImage(
        "example@sha256:" + "a" * 64,
        "sha256:" + "a" * 64,
        "sha256:config",
        "b" * 64,
    )
    resource = _collector_manifest("tessera-e2e-fixture", "collector", image.reference)
    resource["metadata"]["uid"] = "collector-uid"  # type: ignore[index]
    resource["status"] = {"containerStatuses": [{"imageID": image.manifest_digest}]}

    assert _verify_collector(
        resource, "tessera-e2e-fixture", "collector", image, pvc_name="outputs"
    ) == ("collector-uid", image.manifest_digest)
    resource["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"] = False  # type: ignore[index]
    with pytest.raises(LiveKubernetesError, match="PVC binding or security"):
        _verify_collector(resource, "tessera-e2e-fixture", "collector", image, pvc_name="outputs")
    resource["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"] = True  # type: ignore[index]
    resource["spec"]["securityContext"]["runAsUser"] = 0  # type: ignore[index]
    with pytest.raises(LiveKubernetesError, match="PVC binding or security"):
        _verify_collector(resource, "tessera-e2e-fixture", "collector", image, pvc_name="outputs")


def test_live_command_failure_identifies_the_failed_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tessera_worker.docker_desktop_kubernetes.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr="import failed"
        ),
    )

    with pytest.raises(
        LiveKubernetesError,
        match=r"live command failed \(docker exec desktop-control-plane ctr images import image\.tar\): import failed",
    ):
        _run(("docker", "exec", "desktop-control-plane", "ctr", "images", "import", "image.tar"))


def test_live_command_accepts_binary_oci_archive_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("tessera_worker.docker_desktop_kubernetes.subprocess.run", run)

    _run(
        ("docker", "exec", "-i", "desktop-control-plane", "ctr", "images", "import", "-"),
        input=b"oci",
    )

    assert calls == [
        {"input": b"oci", "text": False, "capture_output": True, "timeout": 120, "check": False}
    ]


def test_bound_pvc_must_keep_its_pre_schedule_identity() -> None:
    resource = {
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "outputs", "namespace": "tessera-e2e-fixture", "uid": "pvc-uid"},
        "spec": {"accessModes": ["ReadWriteOnce"]},
        "status": {"phase": "Bound"},
    }
    spec_sha256 = _pvc_spec_sha256(resource)

    _verify_bound_pvc(resource, "tessera-e2e-fixture", "pvc-uid", spec_sha256)
    resource["spec"]["volumeName"] = "late-bound-pv"
    _verify_bound_pvc(resource, "tessera-e2e-fixture", "pvc-uid", spec_sha256)
    resource["status"] = {"phase": "Pending"}
    with pytest.raises(LiveKubernetesError, match="did not bind"):
        _verify_bound_pvc(resource, "tessera-e2e-fixture", "pvc-uid", spec_sha256)
