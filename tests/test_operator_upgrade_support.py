"""Canonical helpers: real local files/tar/SQLite, synthetic Kubernetes processes."""

import importlib
import importlib.util
import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from tessera_control.recovery import backup_state
from tessera_control.store import initialize_state
from tessera_operator import upgrade_support as support

MOVED = {
    "kubernetes": [
        "AdmissionError",
        "_docker",
        "_kubeconfig_identity",
        "_kubectl",
        "validate_backup",
        "_source_listener_absent",
    ],
    "private_import": ["_receipt", "_api_request"],
    "activation": ["_write_once", "ready_pod", "_ready_pod_for"],
    "target_recovery": ["_identity", "_pod_backup"],
}


@pytest.mark.parametrize("module,names", MOVED.items())
def test_legacy_forwarders_are_same_canonical_objects(module, names):
    name = "tessera_operator." + module
    if importlib.util.find_spec(name) is None:
        # Published upgrade-only wheel intentionally omits legacy modules.
        assert all(callable(getattr(support, item)) for item in names)
        return
    legacy = importlib.import_module(name)
    assert all(getattr(legacy, item) is getattr(support, item) for item in names)


def test_create_only_private_receipt_and_unsafe_read_rejection(tmp_path):
    path = tmp_path / "intent.json"
    support._write_once(path, {"operation": "test"})
    assert support._receipt(path) == {"operation": "test"}
    with pytest.raises(FileExistsError):
        support._write_once(path, {"operation": "other"})
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(support.AdmissionError):
        support._receipt(link)
    path.chmod(0o644)
    with pytest.raises(support.AdmissionError):
        support._receipt(path)


@pytest.mark.parametrize("drift", [None, "pvc", "controller", "extra-pod", "source"])
def test_readiness_identity_single_writer_and_source_fence(tmp_path, monkeypatch, drift):
    record = tmp_path / "resources.json"
    support._write_once(record, {"namespace_uid": "ns"})
    monkeypatch.setattr(support, "RESOURCE_RECEIPT", record)
    monkeypatch.setattr(support, "SOURCE", tmp_path)
    monkeypatch.setattr(support, "_source_listener_absent", lambda: None)
    pod = {
        "metadata": {
            "name": "pod",
            "uid": "pod-uid",
            "ownerReferences": [
                {"kind": "ReplicaSet", "name": "rs", "uid": "rs-uid", "controller": True}
            ],
        },
        "spec": {"containers": [{"image": "pinned"}]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }
    objects = {
        "namespace": {"metadata": {"uid": "ns"}},
        "pvc": {"metadata": {"uid": "pvc"}, "spec": {"volumeName": "pv"}},
        "pv": {"metadata": {"uid": "pv"}, "spec": {"claimRef": {"uid": "pvc"}}},
        "deployment": {
            "metadata": {"uid": "deploy"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "pinned"}]}}},
        },
        "pods": {"items": [pod]},
        "replicaset": {
            "metadata": {
                "uid": "rs-uid",
                "ownerReferences": [{"uid": "deploy", "controller": True}],
            }
        },
    }
    if drift == "pvc":
        objects["pv"]["spec"]["claimRef"]["uid"] = "foreign"
    if drift == "controller":
        objects["replicaset"]["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    if drift == "extra-pod":
        objects["pods"]["items"].append(pod)
    if drift == "source":
        (tmp_path / "tessera.sqlite3").touch()

    def kubectl(*args):
        return objects[args[args.index("get") + 1]]

    monkeypatch.setattr(support, "_kubectl", kubectl)
    identity = {
        "namespace_uid": "ns",
        "deployment_uid": "deploy",
        "pvc_uid": "pvc",
        "pv_uid": "pv",
        "image": "pinned",
    }
    if drift:
        with pytest.raises(support.AdmissionError):
            support._ready_pod_for(identity)
    else:
        assert support._ready_pod_for(identity)["pod_uid"] == "pod-uid"


@pytest.mark.parametrize("unsafe", [False, True])
def test_online_backup_tar_and_snapshot_validation(tmp_path, monkeypatch, unsafe):
    source = tmp_path / "source"
    initialize_state(source)
    bundle = tmp_path / "bundle"
    backup_state(source, bundle)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:") as archive:
        if unsafe:
            item = tarfile.TarInfo("./owner.header")
            item.size = 3
            archive.addfile(item, io.BytesIO(b"bad"))
        else:
            for name in ("manifest.json", "snapshot.sqlite3"):
                archive.add(bundle / name, arcname="./" + name)
    pod = {"pod_name": "owned", "pod_uid": "pod", "pv_uid": "pv"}
    monkeypatch.setattr(support, "ready_pod", lambda: pod)
    monkeypatch.setattr(support.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    stream = SimpleNamespace(
        stdout=io.BytesIO(payload.getvalue()), kill=lambda: None, poll=lambda: 0, wait=lambda **k: 0
    )
    monkeypatch.setattr(support.subprocess, "Popen", lambda *a, **k: stream)
    output = tmp_path / "private-backup"
    if unsafe:
        with pytest.raises(support.AdmissionError, match="unsafe target backup member"):
            support._pod_backup(pod, "unique", output)
        assert not (output / "owner.header").exists()
    else:
        result = support._pod_backup(pod, "unique", output)
        assert result["state"] == "independent_private_target_backup_verified"
        assert support.validate_backup(output)["snapshot_sha256"] == result["snapshot_sha256"]
        assert "owner.header" not in json.dumps(result)
        with pytest.raises(support.AdmissionError):
            support._pod_backup(pod, "unique", output)
