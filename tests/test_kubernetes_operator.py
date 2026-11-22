"""Local operator admission never mutates Kubernetes or discloses auth material."""

import glob
import io
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from test_legacy_operator_isolation import legacy_io_guard as legacy_io_guard

from tessera_operator import kubernetes as operator
from tessera_operator import upgrade_support as support


def test_inspect_fixed_context_private_references(tmp_path, monkeypatch, legacy_io_guard):
    config = tmp_path / "kubeconfig"
    config.write_text(
        "contexts:\n- name: docker-desktop\n  context: {cluster: desktop, user: secret-user}\n"
        "clusters:\n- name: desktop\n  cluster:\n"
        "    server: https://127.0.0.1:59675\n    certificate-authority-data: PUBLIC_CA\n"
        "users:\n- name: secret-user\n  user: {token: highly-secret}\n"
    )
    config.chmod(0o600)
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    for name in ("owner.header", "tessera.sqlite3"):
        (source / name).write_text("highly-secret")
        (source / name).chmod(0o600)
    monkeypatch.setattr(support, "KUBECONFIG", config)
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(
        support, "KUBECTL", ("kubectl", "--kubeconfig", str(config), "--context", "docker-desktop")
    )
    calls = []

    def fake(argv, **kwargs):
        assert argv[argv.index("--kubeconfig") + 1] == str(config)
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "items": [
                        {"metadata": {"name": "tessera-lab", "uid": "existing-uid"}},
                    ]
                }
            ).encode(),
            b"highly-secret stderr",
        )

    monkeypatch.setattr(support.subprocess, "run", fake)
    result = operator.inspect()
    assert result["namespace_absent_not_reserved"] is True
    assert result["legacy_namespace_uids"]["tessera-lab"] == "existing-uid"
    assert "highly-secret" not in repr(result)
    assert result["effects"] == "none"
    assert len(calls) == 1 and calls[0][-4:] == ["get", "namespaces", "-o", "json"]
    assert "--context" in calls[0] and calls[0][calls[0].index("--context") + 1] == "docker-desktop"
    assert legacy_io_guard == []


def test_rejects_insecure_kubeconfig_before_kubectl(tmp_path, monkeypatch):
    config = tmp_path / "kubeconfig"
    config.write_text("contexts: []\n")
    config.chmod(0o644)
    monkeypatch.setattr(support, "KUBECONFIG", config)
    monkeypatch.setattr(support.subprocess, "run", lambda *a, **k: pytest.fail("kubectl called"))
    with pytest.raises(operator.AdmissionError, match="private regular"):
        operator.inspect()


def test_kubectl_error_redacts_output(monkeypatch):
    def failure(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"token", b"owner secret")

    monkeypatch.setattr(support.subprocess, "run", failure)
    with pytest.raises(operator.AdmissionError) as caught:
        operator._kubectl("get", "namespaces", "-o", "json")
    assert "token" not in str(caught.value) and "secret" not in str(caught.value)


def test_kubectl_rejects_oversized_output(monkeypatch):
    def oversized(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"x" * (1024 * 1024 + 1), b"")

    monkeypatch.setattr(support.subprocess, "run", oversized)
    with pytest.raises(operator.AdmissionError, match="too large"):
        operator._kubectl("get", "namespaces", "-o", "json")


def test_image_plan_requires_absent_namespace_and_exact_targets(monkeypatch):
    monkeypatch.setattr(operator, "_kubeconfig_identity", lambda: {"server": "https://127.0.0.1"})
    monkeypatch.setattr(operator, "_docker_identity", lambda: "fixed-desktop-socket")
    monkeypatch.setattr(operator, "_source_digest", lambda: "a" * 64)
    monkeypatch.setattr(operator, "inspect", lambda: {"namespace_absent_not_reserved": False})
    with pytest.raises(operator.AdmissionError, match="occupied"):
        operator.image_plan()
    monkeypatch.setattr(operator, "inspect", lambda: {"namespace_absent_not_reserved": True})
    plan = operator.image_plan()
    assert len(plan["plan_sha256"]) == 64
    assert plan["namespace"] == "tessera-local-persistent"
    assert "owner.header" not in repr(plan)


def test_image_context_declares_every_package_data_input(tmp_path, monkeypatch):
    root = operator.REPOSITORY
    config = tomllib.loads((root / "pyproject.toml").read_text())
    groups = config["tool"]["setuptools"]["data-files"]
    dockerfile = (root / "deploy/kubernetes/local-persistent.Dockerfile").read_text()
    for directory in {
        pattern.split("/")[0]
        for patterns in groups.values()
        for pattern in patterns
        if "/" in pattern
    }:
        assert f"COPY {directory} ./{directory}" in dockerfile
    assert "COPY src ./src" in dockerfile
    assert len(operator._source_digest()) == 64

    # An isolated mirror lets the digest reject changed or missing package data
    # without modifying the real reviewed inputs or loading a Docker image.
    mirror = tmp_path / "image-context"
    mirror.mkdir()
    mandatory = (
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "MANIFEST.in",
        "CONTRIBUTING.md",
        "DESIGN.md",
        "CHANGELOG.md",
        "SECURITY.md",
        "AGENTS.md",
        "deploy/kubernetes/local-persistent.Dockerfile",
        "deploy/kubernetes/local-persistent.Dockerfile.dockerignore",
    )
    inputs = set(mandatory)
    for patterns in groups.values():
        for pattern in patterns:
            matches = glob.glob(pattern, root_dir=root)
            assert matches, pattern
            inputs.update(matches)
    for name in inputs:
        destination = mirror / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, destination)
    source = mirror / "src/tessera_control"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("# synthetic package\n")
    monkeypatch.setattr(operator, "REPOSITORY", mirror)
    first = operator._source_digest()
    skill = mirror / "skills/tessera-research-bench/SKILL.md"
    skill.write_bytes(skill.read_bytes() + b"\n# synthetic drift\n")
    assert first != operator._source_digest()
    skill.unlink()
    with pytest.raises(operator.AdmissionError, match="declared package file unavailable"):
        operator._source_digest()

    shutil.copyfile(root / "skills/tessera-research-bench/SKILL.md", skill)
    (source / "owner.header").write_text("synthetic-private-value")
    with pytest.raises(operator.AdmissionError, match="unexpected source file"):
        operator._source_digest()


def test_image_context_rejects_extra_secret_allowlist(tmp_path, monkeypatch):
    root = operator.REPOSITORY
    config = tomllib.loads((root / "pyproject.toml").read_text())
    for name in (
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "MANIFEST.in",
        "CONTRIBUTING.md",
        "DESIGN.md",
        "CHANGELOG.md",
        "SECURITY.md",
        "AGENTS.md",
    ):
        shutil.copyfile(root / name, tmp_path / name)
    for name in ("local-persistent.Dockerfile", "local-persistent.Dockerfile.dockerignore"):
        destination = tmp_path / "deploy/kubernetes" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "deploy/kubernetes" / name, destination)
    for patterns in config["tool"]["setuptools"]["data-files"].values():
        for pattern in patterns:
            for name in glob.glob(pattern, root_dir=root):
                destination = tmp_path / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, destination)
    (tmp_path / "src").mkdir()
    ignore = tmp_path / "deploy/kubernetes/local-persistent.Dockerfile.dockerignore"
    ignore.write_text(ignore.read_text() + "!.tessera/\n!.tessera/owner.header\n")
    monkeypatch.setattr(operator, "REPOSITORY", tmp_path)
    with pytest.raises(operator.AdmissionError, match="allowlist differs"):
        operator._source_digest()


def test_build_rejects_missing_or_drifted_approval_before_docker(monkeypatch):
    monkeypatch.setattr(operator, "image_plan", lambda: {"plan_sha256": "a" * 64})
    monkeypatch.setattr(operator, "_docker", lambda *a, **k: pytest.fail("Docker called"))
    with pytest.raises(operator.AdmissionError, match="digest"):
        operator.build_image("wrong")
    with pytest.raises(operator.AdmissionError, match="changed"):
        operator.build_image("b" * 64)


def test_fixed_build_import_pins_only_named_node_image(monkeypatch):
    plan = {"plan_sha256": "a" * 64}
    monkeypatch.setattr(operator, "image_plan", lambda: plan)
    calls = []
    digest = "sha256:" + "b" * 64
    pinned = "docker.io/library/tessera-local-persistent@" + digest

    def docker(*args, **kwargs):
        calls.append(args)
        if args[-2:] == ("ls", "--quiet"):
            return pinned
        if args[-2:] == ("images", "ls"):
            return f"REF TYPE DIGEST SIZE\n{operator.IMAGE_TAG} application {digest} 1MiB"
        return ""

    class Process:
        returncode = 0

        def __init__(self):
            self.stdout = io.BytesIO(b"image")

        def communicate(self, **kwargs):
            return b"", b""

    def popen(argv, **kwargs):
        calls.append(tuple(argv))
        return Process()

    monkeypatch.setattr(operator, "_docker", docker)
    monkeypatch.setattr(operator.subprocess, "Popen", popen)
    assert operator.build_image("a" * 64) == {
        "image": pinned,
        "stage": "node_image_pinned_no_namespace_writes",
    }
    assert calls[0][:3] == ("buildx", "build", "--load")
    assert calls[1][:5] == ("docker", "--context", "desktop-linux", "image", "save")
    assert "desktop-control-plane" in calls[2]
    assert all("kubectl" not in repr(call) for call in calls)


def test_docker_context_drift_rejected(monkeypatch):
    monkeypatch.setattr(operator, "_docker", lambda *a, **k: '"ssh://unapproved-host"')
    with pytest.raises(operator.AdmissionError, match="endpoint changed"):
        operator._docker_identity()


def test_staged_manifest_cannot_start_writer_or_expose_plain_http():
    profile = (
        Path(__file__).resolve().parents[1] / "deploy/kubernetes/local-persistent-control.yaml"
    )
    docs = list(yaml.safe_load_all(profile.read_text()))
    assert [doc["kind"] for doc in docs] == [
        "PersistentVolumeClaim",
        "Deployment",
        "Service",
        "NetworkPolicy",
    ]
    assert {doc["metadata"]["namespace"] for doc in docs} == {"tessera-local-persistent"}
    claim, deployment, service, policy = docs
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["storageClassName"] == "standard"
    assert deployment["spec"]["replicas"] == 0
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["imagePullPolicy"] == "Never"
    assert pod["containers"][0]["command"][-1] == "127.0.0.1"
    assert "REPLACE_EXACT_NODE_DIGEST" in pod["containers"][0]["image"]
    assert service["spec"]["type"] == "ClusterIP"
    assert policy["spec"]["podSelector"] == {}
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert "owner.header" not in profile.read_text()


def test_resource_plan_rejects_occupied_namespace_and_nonimmutable_image(monkeypatch):
    image = "docker.io/library/tessera-local-persistent@sha256:" + "a" * 64
    monkeypatch.setattr(operator, "inspect", lambda: {"namespace_absent_not_reserved": False})
    monkeypatch.setattr(operator, "_docker", lambda *a, **k: pytest.fail("Docker called"))
    with pytest.raises(operator.AdmissionError, match="immutable"):
        operator.resource_plan("latest")
    with pytest.raises(operator.AdmissionError, match="occupied"):
        operator.resource_plan(image)


def test_resource_plan_digest_binds_cluster_uid_and_fixed_inert_manifest(monkeypatch):
    image = "docker.io/library/tessera-local-persistent@sha256:" + "a" * 64
    observed = {
        "namespace_absent_not_reserved": True,
        "server": "https://127.0.0.1:59675",
        "kubeconfig_sha256": "c" * 64,
        "legacy_namespace_uids": {"tessera-lab": "old"},
    }
    monkeypatch.setattr(operator, "inspect", lambda: observed)
    monkeypatch.setattr(operator, "_docker_identity", lambda: "desktop-socket")
    monkeypatch.setattr(operator, "_docker", lambda *a, **k: image)
    monkeypatch.setattr(operator, "_kubectl", lambda *a, **k: {"metadata": {"uid": "cluster-1"}})
    plan = operator.resource_plan(image)
    assert len(plan["plan_sha256"]) == 64
    assert plan["documents"][1]["spec"]["replicas"] == 0
    assert plan["documents"][1]["spec"]["template"]["spec"]["containers"][0]["image"] == image
    assert plan["resources"] == [
        "namespace",
        "pvc/tessera-control-state",
        "deployment/tessera-control (replicas=0)",
        "service/tessera-control (ClusterIP)",
        "networkpolicy/default-deny (enforcement unverified)",
    ]
    assert "owner.header" not in repr(plan)
    monkeypatch.setattr(operator, "_kubectl", lambda *a, **k: {"metadata": {"uid": "cluster-2"}})
    assert operator.resource_plan(image)["plan_sha256"] != plan["plan_sha256"]


def test_resource_apply_fences_digest_before_any_effect(monkeypatch):
    monkeypatch.setattr(operator, "resource_plan", lambda image: {"plan_sha256": "a" * 64})
    monkeypatch.setattr(operator, "_receipt_write", lambda *a, **k: pytest.fail("receipt written"))
    monkeypatch.setattr(operator, "_kubectl", lambda *a, **k: pytest.fail("kubectl called"))
    with pytest.raises(operator.AdmissionError, match="changed"):
        operator.provision_resources("b" * 64, "immutable-image")


def test_resource_apply_named_create_only_and_uid_fence(tmp_path, monkeypatch):
    image = "docker.io/library/tessera-local-persistent@sha256:" + "a" * 64
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(operator, "RESOURCE_RECEIPT", receipt)
    monkeypatch.setattr(operator, "resource_plan", lambda image: {"plan_sha256": "b" * 64})
    calls = []

    def kubectl(*args, **kwargs):
        calls.append((args, kwargs.get("data")))
        if args[0] == "get":
            return {
                "metadata": {
                    "uid": "owned",
                    "labels": {
                        "tessera.io/local-operator-nonce": json.loads(receipt.read_text())["nonce"]
                    },
                }
            }
        return {"metadata": {"uid": "owned" if args[0] == "create" else "resource-id"}}

    monkeypatch.setattr(operator, "_kubectl", kubectl)
    result = operator.provision_resources("b" * 64, image)
    assert result["stage"].startswith("inert_resources_only")
    assert len(result["created"]) == 4
    assert calls[0][0] == ("create", "-f", "-", "-o", "json")
    assert all(command[0][0] in {"create", "get", "-n"} for command in calls)
    assert all(
        command[0][1] == "tessera-local-persistent" for command in calls if command[0][0] == "-n"
    )
    with pytest.raises(operator.AdmissionError, match="prior operator attempt"):
        operator.provision_resources("b" * 64, image)


def test_backup_plan_rejects_unsafe_reference_and_digest_drift(tmp_path, monkeypatch):
    source = tmp_path / "state"
    source.mkdir(mode=0o700)
    private = source / "private"
    private.mkdir(mode=0o700)
    for name in ("tessera.sqlite3", "owner.header"):
        (source / name).write_bytes(b"fixture")
        (source / name).chmod(0o600)
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "BACKUP_OUTPUT", private / "snapshot")
    plan = operator.backup_plan()
    monkeypatch.setattr(operator, "backup_state", lambda *a: pytest.fail("backup ran"))
    with pytest.raises(operator.AdmissionError, match="drifted"):
        operator.backup_source("f" * 64)
    (source / "owner.header").unlink()
    (source / "owner.header").symlink_to(source / "tessera.sqlite3")
    with pytest.raises(operator.AdmissionError, match="custody"):
        operator.backup_plan()
    assert len(plan["plan_sha256"]) == 64


def test_backup_output_is_redacted_and_never_moves_owner(tmp_path, monkeypatch):
    source = tmp_path / "state"
    source.mkdir(mode=0o700)
    private = source / "private"
    private.mkdir(mode=0o700)
    for name in ("tessera.sqlite3", "owner.header"):
        (source / name).write_bytes(b"bearer-secret-must-not-appear")
        (source / name).chmod(0o600)
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "BACKUP_OUTPUT", private / "snapshot")
    calls = []

    def fake_backup(src, out):
        calls.append((src, out))
        return {"backup_id": "fixture", "sha256": "c" * 64, "schema_version": 42}

    monkeypatch.setattr(operator, "backup_state", fake_backup)
    monkeypatch.setattr(
        operator,
        "validate_backup",
        lambda: {
            "backup_id": "fixture",
            "snapshot_sha256": "c" * 64,
            "schema_version": 42,
            "state": "verified_snapshot_only; owner authority not copied",
        },
    )
    result = operator.backup_source(operator.backup_plan()["plan_sha256"])
    assert calls == [(source, private / "snapshot")]
    assert "bearer-secret-must-not-appear" not in repr(result)
    assert "snapshot_only" in result["state"]


def test_backup_verification_rejects_changed_snapshot_and_object(tmp_path, monkeypatch):
    import hashlib

    snapshot = tmp_path / "snapshot.sqlite3"
    snapshot.write_bytes(b"synthetic snapshot")
    objects = tmp_path / "objects" / "tenant" / "project"
    objects.mkdir(parents=True)
    (objects / "object").write_bytes(b"synthetic object")
    entry = {
        "availability": "present",
        "tenant_namespace": "tenant",
        "project_namespace": "project",
        "object_key": "object",
        "sha256": hashlib.sha256(b"synthetic object").hexdigest(),
    }
    manifest = {
        "backup_id": "fixture",
        "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "schema_version": 42,
        "event_watermark": 7,
        "objects": [entry],
    }
    monkeypatch.setattr(support, "BACKUP_OUTPUT", tmp_path)
    monkeypatch.setattr(support, "_read_manifest", lambda path: manifest)
    monkeypatch.setattr(support, "_validate_snapshot", lambda *a: (42, 7))
    monkeypatch.setattr(support, "_validate_bundle_declaration", lambda *a: None)
    assert operator.validate_backup()["state"].startswith("verified_snapshot_only")
    (objects / "object").write_bytes(b"changed")
    with pytest.raises(operator.AdmissionError, match="object checksum"):
        operator.validate_backup()
    (objects / "object").write_bytes(b"synthetic object")
    snapshot.write_bytes(b"changed")
    with pytest.raises(operator.AdmissionError, match="snapshot checksum"):
        operator.validate_backup()


def test_cutover_preflight_fails_closed_on_uid_and_process_drift(tmp_path, monkeypatch):
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "namespace_uid": "owned-ns",
                "nonce": "nonce",
                "created": [
                    {
                        "kind": "PersistentVolumeClaim",
                        "name": "tessera-control-state",
                        "uid": "pvc",
                    },
                    {"kind": "Deployment", "name": "tessera-control", "uid": "deploy"},
                    {"kind": "Service", "name": "tessera-control", "uid": "svc"},
                    {"kind": "NetworkPolicy", "name": "default-deny", "uid": "policy"},
                ],
            }
        )
    )
    receipt.chmod(0o600)
    monkeypatch.setattr(operator, "RESOURCE_RECEIPT", receipt)
    monkeypatch.setattr(operator, "validate_backup", lambda: {"snapshot_sha256": "b" * 64})
    observed_pvc_uid = ["wrong"]

    def kubectl(*args, **kwargs):
        if args[1] == "namespace":
            return {
                "metadata": {
                    "uid": "owned-ns",
                    "labels": {"tessera.io/local-operator-nonce": "nonce"},
                }
            }
        if args[3] == "deployment":
            return {"metadata": {"uid": "deploy"}, "spec": {"replicas": 0}}
        if args[3] == "pvc":
            return {"metadata": {"uid": observed_pvc_uid[0]}, "status": {"phase": "Bound"}}
        if args[3] == "pods":
            return {"items": []}
        return {"metadata": {"uid": "svc"}, "spec": {"type": "ClusterIP"}}

    monkeypatch.setattr(operator, "_kubectl", kubectl)
    monkeypatch.setattr(operator.subprocess, "run", lambda *a, **k: pytest.fail("process probed"))
    with pytest.raises(operator.AdmissionError, match="PVC identity drifted"):
        operator.cutover_preflight()
    observed_pvc_uid[0] = "pvc"

    def process(argv, **kwargs):
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0, b"p123\n", b"")
        return subprocess.CompletedProcess(
            argv, 0, b"1000 /usr/bin/python untrusted serve --state-dir /elsewhere --port 8765", b""
        )

    monkeypatch.setattr(operator.subprocess, "run", process)
    with pytest.raises(operator.AdmissionError, match="source writer identity"):
        operator.cutover_preflight()


def test_marker_blocks_current_binary_and_missing_db_blocks_older_binary(tmp_path):
    from tessera_control.store import validate_database

    marker = tmp_path / ".authority-moved"
    marker.write_text("fenced")
    with pytest.raises(ValueError, match="authority was moved"):
        validate_database(tmp_path / "tessera.sqlite3")
    marker.unlink()
    with pytest.raises(ValueError, match="not a regular file"):
        validate_database(tmp_path / "tessera.sqlite3")


def test_fence_and_rollback_keep_unrelated_files_and_no_target_writer(tmp_path, monkeypatch):
    import signal

    source = tmp_path / "state"
    source.mkdir(mode=0o700)
    private = source / "private"
    private.mkdir(mode=0o700)
    for name in ("tessera.sqlite3", "owner.header"):
        (source / name).write_bytes(b"private fixture")
        (source / name).chmod(0o600)
    (source / "project-artifacts").mkdir(mode=0o700)
    (source / "receipts").mkdir(mode=0o700)
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "QUARANTINE", private / "quarantine")
    monkeypatch.setattr(operator, "FENCE_MARKER", source / ".authority-moved")
    monkeypatch.setattr(operator, "FENCED_BACKUP", private / "fenced")
    monkeypatch.setattr(operator, "ACTIVATED_MARKER", private / "activated")
    monkeypatch.setattr(
        operator,
        "cutover_preflight",
        lambda: {"source_pid_observed_not_fenced": 123, "namespace_uid": "owned"},
    )
    monkeypatch.setattr(operator, "_exact_process_start", lambda pid: b"fixed source identity")
    stopped = [False]

    def kill(pid, sig):
        assert pid == 123
        if sig == signal.SIGTERM:
            stopped[0] = True
        elif stopped[0]:
            raise ProcessLookupError()

    monkeypatch.setattr(operator.os, "kill", kill)
    monkeypatch.setattr(operator, "_source_listener_absent", lambda: None)
    monkeypatch.setattr(
        operator.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1, b"", b"")
    )
    monkeypatch.setattr(operator, "_backup_state", lambda src, dest: dest.mkdir(mode=0o700))
    monkeypatch.setattr(
        operator,
        "validate_backup",
        lambda path: {"snapshot_sha256": "f" * 64, "event_watermark": 1},
    )
    result = operator.fence_source()
    assert result["state"] == "source_quarantined_target_still_inactive"
    assert (source / "receipts").is_dir()
    assert not (source / "tessera.sqlite3").exists()
    assert (private / "quarantine" / "owner.header").is_file()

    def kubectl(*args, **kwargs):
        if args[1] == "namespace":
            return {"metadata": {"uid": "owned"}}
        if args[3] == "pods":
            return {"items": []}
        return {"metadata": {"generation": 1}, "spec": {"replicas": 0}}

    monkeypatch.setattr(operator, "_kubectl", kubectl)
    assert operator.rollback_source()["state"] == "old_source_restored_not_started"
    assert (source / "owner.header").read_bytes() == b"private fixture"
    assert not (source / ".authority-moved").exists()
    assert (source / "receipts").is_dir()


def test_fence_rejects_open_handle_before_quarantine(tmp_path, monkeypatch):
    source = tmp_path / "state"
    source.mkdir(mode=0o700)
    private = source / "private"
    private.mkdir(mode=0o700)
    for name in ("tessera.sqlite3", "owner.header"):
        (source / name).write_bytes(b"fixture")
        (source / name).chmod(0o600)
    (source / "project-artifacts").mkdir(mode=0o700)
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "QUARANTINE", private / "quarantine")
    monkeypatch.setattr(operator, "FENCE_MARKER", source / ".authority-moved")
    monkeypatch.setattr(operator, "FENCED_BACKUP", private / "fenced")
    monkeypatch.setattr(
        operator,
        "cutover_preflight",
        lambda: {"source_pid_observed_not_fenced": 123, "namespace_uid": "owned"},
    )
    monkeypatch.setattr(operator, "_exact_process_start", lambda pid: b"fixed source identity")
    monkeypatch.setattr(
        operator.os,
        "kill",
        lambda pid, sig: None if sig else (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(operator, "_source_listener_absent", lambda: None)
    monkeypatch.setattr(
        operator.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, b"p456\n", b""),
    )
    with pytest.raises(operator.AdmissionError, match="open handles"):
        operator.fence_source()
    assert not (source / ".authority-moved").exists()
    assert (source / "tessera.sqlite3").exists()


def test_fence_rejects_process_identity_race_without_signalling(tmp_path, monkeypatch):
    source = tmp_path / "state"
    source.mkdir(mode=0o700)
    private = source / "private"
    private.mkdir(mode=0o700)
    for name in ("tessera.sqlite3", "owner.header"):
        (source / name).write_bytes(b"fixture")
        (source / name).chmod(0o600)
    (source / "project-artifacts").mkdir(mode=0o700)
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "QUARANTINE", private / "quarantine")
    monkeypatch.setattr(operator, "FENCE_MARKER", source / ".authority-moved")
    monkeypatch.setattr(operator, "FENCED_BACKUP", private / "fenced")
    monkeypatch.setattr(
        operator,
        "cutover_preflight",
        lambda: {"source_pid_observed_not_fenced": 123, "namespace_uid": "owned"},
    )
    identity = iter((b"old process", b"new process"))
    monkeypatch.setattr(operator, "_exact_process_start", lambda pid: next(identity))
    monkeypatch.setattr(operator.os, "kill", lambda *a: pytest.fail("signalled stale PID"))
    with pytest.raises(operator.AdmissionError, match="changed before stop"):
        operator.fence_source()
    assert not (source / ".authority-moved").exists()


def test_absent_fence_quarantines_without_signal_and_retains_rollback(tmp_path, monkeypatch):
    source = tmp_path / "state"
    source.mkdir(mode=0o700)
    private = source / "private"
    private.mkdir(mode=0o700)
    for name in ("tessera.sqlite3", "owner.header", "tessera.sqlite3-wal"):
        (source / name).write_bytes(b"private fixture")
        (source / name).chmod(0o600)
    (source / "project-artifacts").mkdir(mode=0o700)
    (source / "unrelated").write_text("untouched")
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "QUARANTINE", private / "quarantine")
    monkeypatch.setattr(operator, "FENCE_MARKER", source / ".authority-moved")
    monkeypatch.setattr(operator, "FENCED_BACKUP", private / "fenced")
    monkeypatch.setattr(operator, "ACTIVATED_MARKER", private / "activated")
    monkeypatch.setattr(operator, "cutover_preflight", lambda **kw: {"namespace_uid": "owned"})
    monkeypatch.setattr(operator, "_source_listener_absent", lambda: None)
    monkeypatch.setattr(operator.os, "kill", lambda *a: pytest.fail("signalled stale PID"))
    monkeypatch.setattr(
        operator.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv,
            0 if argv[0] == "ps" else 1,
            b"1000 345 unrelated\n" if argv[0] == "ps" else b"",
            b"",
        ),
    )
    monkeypatch.setattr(operator, "_backup_state", lambda src, dst: dst.mkdir(mode=0o700))
    monkeypatch.setattr(
        operator,
        "validate_backup",
        lambda path: {"snapshot_sha256": "f" * 64, "event_watermark": 1},
    )
    result = operator.fence_absent_source()
    assert result["state"] == "source_quarantined_target_still_inactive"
    assert (private / "quarantine" / "tessera.sqlite3-wal").exists()
    assert (source / "unrelated").read_text() == "untouched"
    assert json.loads((source / ".authority-moved").read_text())["mode"] == "observed_absent_writer"
    monkeypatch.setattr(
        operator,
        "_kubectl",
        lambda *args, **kw: (
            {"metadata": {"uid": "owned"}}
            if args[1] == "namespace"
            else {"items": []}
            if args[3] == "pods"
            else {"metadata": {"generation": 1}, "spec": {"replicas": 0}}
        ),
    )
    assert operator.rollback_source()["state"] == "old_source_restored_not_started"


@pytest.mark.parametrize(
    "failure", ["writer", "port", "symlink", "permission", "changed", "partial"]
)
def test_absent_fence_rejects_races_without_signal(tmp_path, monkeypatch, failure):
    source = tmp_path / "state"
    source.mkdir(mode=0o700)
    private = source / "private"
    private.mkdir(mode=0o700)
    for name in ("tessera.sqlite3", "owner.header"):
        (source / name).write_bytes(b"fixture")
        (source / name).chmod(0o600)
    (source / "project-artifacts").mkdir(mode=0o700)
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "QUARANTINE", private / "quarantine")
    monkeypatch.setattr(operator, "FENCE_MARKER", source / ".authority-moved")
    monkeypatch.setattr(operator, "FENCED_BACKUP", private / "fenced")
    monkeypatch.setattr(operator, "cutover_preflight", lambda **kw: {"namespace_uid": "owned"})
    monkeypatch.setattr(operator.os, "kill", lambda *a: pytest.fail("signalled stale PID"))
    if failure == "symlink":
        (source / "owner.header").unlink()
        (source / "owner.header").symlink_to(source / "tessera.sqlite3")
    if failure == "permission":
        (source / "owner.header").chmod(0o644)
    calls = [0]

    def port():
        calls[0] += 1
        if failure == "port" and calls[0] >= 3:
            raise operator.AdmissionError("source port in use")

    monkeypatch.setattr(operator, "_source_listener_absent", port)

    def process(argv, **kw):
        if argv[0] == "ps":
            text = (
                f"1000 345 tessera serve --state-dir {source}\n"
                if failure == "writer"
                else "1000 345 unrelated\n"
            )
            return subprocess.CompletedProcess(argv, 0, text.encode(), b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(operator.subprocess, "run", process)
    if failure == "changed":
        original = operator._source_node_identity
        count = [0]

        def drift(names):
            count[0] += 1
            if count[0] == 2:
                (source / "tessera.sqlite3").write_bytes(b"drift")
            return original(names)

        monkeypatch.setattr(operator, "_source_node_identity", drift)
    if failure == "partial":
        original_rename = operator.os.rename
        count = [0]

        def rename(src, dst):
            count[0] += 1
            if count[0] == 2:
                raise OSError("injected partial rename")
            return original_rename(src, dst)

        monkeypatch.setattr(operator.os, "rename", rename)
    with pytest.raises((operator.AdmissionError, OSError)):
        operator.fence_absent_source()
    if failure == "partial":
        assert (source / ".authority-moved").is_file()
        assert (private / "quarantine" / "tessera.sqlite3").is_file()
    else:
        assert not (source / ".authority-moved").exists()


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("/opt/Python -I /repo/.venv/bin/tessera serve --state-dir /other/.tessera", False),
        ("/opt/Electron /app/observer.cjs /repo/.tessera --state-dir /repo/.tessera serve", False),
        ("/repo/.venv/bin/tessera serve --state-dir /repo/.tessera", True),
        ("/opt/Python -I /repo/.venv/bin/tessera serve --state-dir=/repo/.tessera", True),
        ("/opt/Python -m tessera_control.cli serve --state-dir /repo/.tessera", True),
        ("/opt/Python -m tessera_control serve --state-dir /repo/.tessera", True),
        ("/opt/Python -I /repo/.venv/bin/tessera serve", True),
        ("/opt/Python -I /repo/.venv/bin/tessera serve --state-dir='unterminated", True),
    ],
)
def test_absent_writer_classifies_process_argv(tmp_path, monkeypatch, command, blocked):
    source = tmp_path / "actual"
    source.mkdir()
    monkeypatch.setattr(operator, "SOURCE", source)
    command = command.replace("/repo/.tessera", str(source))
    monkeypatch.setattr(operator, "_source_listener_absent", lambda: None)

    def process(argv, **kwargs):
        assert argv[0] == "ps"
        return subprocess.CompletedProcess(argv, 0, f"501 123 {command}\n".encode(), b"")

    monkeypatch.setattr(operator.subprocess, "run", process)
    if blocked:
        with pytest.raises(operator.AdmissionError, match="source writer|ambiguous|unparseable"):
            operator._absent_writer_guard()
    else:
        operator._absent_writer_guard()


@pytest.mark.parametrize("state", ["relative", "symlink", "unknown_cwd"])
def test_absent_writer_relative_and_symlink_source(tmp_path, monkeypatch, state):
    source = tmp_path / "actual"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "_source_listener_absent", lambda: None)
    if state == "symlink":
        command = f"/repo/tessera serve --state-dir={alias}"
    else:
        command = "/repo/tessera serve --state-dir=actual"

    def process(argv, **kwargs):
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, f"501 123 {command}\n".encode(), b"")
        assert argv[:4] == ["lsof", "-a", "-p", "123"]
        if state == "unknown_cwd":
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        return subprocess.CompletedProcess(argv, 0, f"p123\nn{tmp_path}\n".encode(), b"")

    monkeypatch.setattr(operator.subprocess, "run", process)
    with pytest.raises(operator.AdmissionError, match="source writer|uncertain"):
        operator._absent_writer_guard()


@pytest.mark.parametrize("drift", ["uid", "pid", "command"])
def test_absent_writer_refuses_process_identity_drift(tmp_path, monkeypatch, drift):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(operator, "SOURCE", source)
    monkeypatch.setattr(operator, "_source_listener_absent", lambda: None)
    calls = [0]

    def process(argv, **kwargs):
        calls[0] += 1
        value = {
            "uid": "502 123",
            "pid": "501 124",
            "command": "501 123 /other/tessera serve --state-dir=/elsewhere",
        }[drift]
        line = (
            "501 123 /other/tessera serve --state-dir=/other"
            if calls[0] == 1
            else value + (" /other/tessera serve --state-dir=/other" if drift != "command" else "")
        )
        return subprocess.CompletedProcess(argv, 0, (line + "\n").encode(), b"")

    monkeypatch.setattr(operator.subprocess, "run", process)
    with pytest.raises(operator.AdmissionError, match="identity changed"):
        operator._absent_writer_guard()


def test_rollback_denies_target_that_may_have_written(tmp_path, monkeypatch):
    marker = tmp_path / ".authority-moved"
    marker.write_text(
        json.dumps(
            {
                "namespace_uid": "owned",
                "names": ["tessera.sqlite3", "owner.header", "project-artifacts"],
            }
        )
    )
    marker.chmod(0o600)
    monkeypatch.setattr(operator, "FENCE_MARKER", marker)
    monkeypatch.setattr(operator, "ACTIVATED_MARKER", tmp_path / "activated")
    monkeypatch.setattr(
        operator,
        "_kubectl",
        lambda *args, **kw: (
            {"metadata": {"uid": "owned"}}
            if args[1] == "namespace"
            else {"items": []}
            if args[3] == "pods"
            else {"metadata": {"generation": 2}, "spec": {"replicas": 0}}
        ),
    )
    with pytest.raises(operator.AdmissionError, match="might have written"):
        operator.rollback_source()
    (tmp_path / "activated").write_text("activation seen")
    with pytest.raises(operator.AdmissionError, match="rollback prohibited"):
        operator.rollback_source()


def test_operator_refuses_without_explicit_repository(monkeypatch):
    from tessera_operator import kubernetes, local_paths
    from tessera_operator.upgrade_support import AdmissionError

    monkeypatch.setattr(local_paths, "REPOSITORY_CONFIGURED", False)
    with pytest.raises(AdmissionError):
        kubernetes.main([])


def test_kubeconfig_list_uses_first_entry(monkeypatch):
    import importlib

    from tessera_operator import local_paths

    monkeypatch.setenv("KUBECONFIG", "/a/one:/b/two")
    try:
        assert importlib.reload(local_paths).KUBECONFIG == Path("/a/one")
    finally:
        monkeypatch.undo()
        importlib.reload(local_paths)
