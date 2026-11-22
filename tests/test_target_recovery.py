"""Synthetic copy/readback and UID-scoped target recovery tests; no cluster calls."""

import io
import json
import shutil
import tarfile

import pytest
from test_legacy_operator_isolation import legacy_io_guard as legacy_io_guard

from tessera_control.recovery import backup_state
from tessera_control.store import initialize_state
from tessera_operator import activation, private_import, target_recovery
from tessera_operator import kubernetes as operator
from tessera_operator import upgrade_support as support


def _bundle(tmp_path):
    source = tmp_path / "source"
    initialize_state(source)
    backup = tmp_path / "bundle"
    backup_state(source, backup)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:") as archive:
        archive.add(backup / "manifest.json", arcname="./manifest.json")
        archive.add(backup / "snapshot.sqlite3", arcname="./snapshot.sqlite3")
    return stream.getvalue()


def test_online_target_backup_private_mac_custody(tmp_path, monkeypatch, legacy_io_guard):
    payload = _bundle(tmp_path)
    pod = {"pod_name": "owned", "pod_uid": "pod-1", "pv_uid": "pv-1"}
    monkeypatch.setattr(support, "ready_pod", lambda: pod)

    class Completed:
        returncode = 0

    class Stream:
        def __init__(self):
            self.stdout = io.BytesIO(payload)

        def kill(self):
            pass

        def wait(self, timeout):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(support.subprocess, "run", lambda *_a, **_k: Completed())
    monkeypatch.setattr(support.subprocess, "Popen", lambda *_a, **_k: Stream())
    output = tmp_path / "mac-backup"
    result = target_recovery._pod_backup(pod, "before", output)
    assert result["state"] == "independent_private_target_backup_verified"
    assert set(path.name for path in output.iterdir()) == {"manifest.json", "snapshot.sqlite3"}
    assert all(path.stat().st_mode & 0o077 == 0 for path in output.iterdir())
    assert "owner.header" not in json.dumps(result)
    assert legacy_io_guard == []
    with pytest.raises(operator.AdmissionError, match="prior target backup"):
        target_recovery._pod_backup(pod, "before", output)


def test_target_stream_refuses_owner_header(tmp_path, monkeypatch):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:") as archive:
        entry = tarfile.TarInfo("./owner.header")
        entry.size = len(b"SYNTHETIC_PRIVATE")
        archive.addfile(entry, io.BytesIO(b"SYNTHETIC_PRIVATE"))

    class Completed:
        returncode = 0

    class Stream:
        def __init__(self):
            self.stdout = io.BytesIO(stream.getvalue())

        def kill(self):
            pass

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(support.subprocess, "run", lambda *_a, **_k: Completed())
    monkeypatch.setattr(support.subprocess, "Popen", lambda *_a, **_k: Stream())
    monkeypatch.setattr(support, "ready_pod", lambda: {"pod_uid": "owned"})
    output = tmp_path / "mac-backup"
    with pytest.raises(operator.AdmissionError, match="unsafe target backup member") as error:
        target_recovery._pod_backup(
            {"pod_name": "owned", "pod_uid": "owned", "pv_uid": "pv"}, "before", output
        )
    assert "SYNTHETIC_PRIVATE" not in str(error.value)
    assert not (output / "owner.header").exists()


def test_recreate_conditional_pod_delete_and_new_uid(tmp_path, monkeypatch):
    before = tmp_path / "before"
    _bundle(tmp_path)
    backup_state(tmp_path / "source", before)
    intent = before.with_name(before.name + ".intent")
    intent.write_text(json.dumps({"pod_uid": "old", "pv_uid": "pv"}))
    intent.chmod(0o600)
    monkeypatch.setattr(target_recovery, "BEFORE", before)
    monkeypatch.setattr(target_recovery, "RECREATE", tmp_path / "recreate.json")
    old = {"pod_name": "owned-pod", "pod_uid": "old", "pv_uid": "pv"}
    state = {"deleted": False}
    monkeypatch.setattr(
        activation, "ready_pod", lambda: {**old, "pod_uid": "new"} if state["deleted"] else old
    )
    monkeypatch.setattr(
        operator,
        "_kubectl",
        lambda *args, **kwargs: {"metadata": {"uid": "old", "resourceVersion": "rv-5"}},
    )

    def request(method, uri, body):
        assert target_recovery.RECREATE.exists()
        assert method == "DELETE" and uri.endswith("/pods/owned-pod")
        assert body["preconditions"] == {"uid": "old", "resourceVersion": "rv-5"}
        state["deleted"] = True
        return 200

    monkeypatch.setattr(private_import, "_api_request", request)
    result = target_recovery.recreate()
    assert result["new_pod_uid"] == "new" and result["pv_uid"] == "pv"
    with pytest.raises(operator.AdmissionError, match="prior Pod recreation"):
        target_recovery.recreate()


def test_backup_rejects_foreign_pod_before_work(tmp_path, monkeypatch):
    monkeypatch.setattr(target_recovery, "AFTER", tmp_path / "after")
    monkeypatch.setattr(target_recovery, "RECREATE", tmp_path / "recreate.json")
    target_recovery.RECREATE.write_text(json.dumps({"old_pod_uid": "old", "pv_uid": "pv"}))
    target_recovery.RECREATE.chmod(0o600)
    monkeypatch.setattr(activation, "ready_pod", lambda: {"pod_uid": "old", "pv_uid": "foreign"})
    with pytest.raises(operator.AdmissionError, match="not recreated on same PVC"):
        target_recovery.backup_target("after")


def test_post_recreation_compares_record_backend_prefix(tmp_path, monkeypatch):
    _bundle(tmp_path)
    before = tmp_path / "before"
    after = tmp_path / "after"
    backup_state(tmp_path / "source", before)
    monkeypatch.setattr(target_recovery, "BEFORE", before)
    monkeypatch.setattr(target_recovery, "AFTER", after)
    monkeypatch.setattr(target_recovery, "RECREATE", tmp_path / "recreate.json")
    target_recovery.RECREATE.write_text(json.dumps({"old_pod_uid": "old", "pv_uid": "pv"}))
    target_recovery.RECREATE.chmod(0o600)
    monkeypatch.setattr(activation, "ready_pod", lambda: {"pod_uid": "new", "pv_uid": "pv"})

    def copy_backup(pod, stage, output):
        shutil.copytree(before, output)
        return {
            "incarnation": target_recovery._identity(after / "snapshot.sqlite3")["incarnation"],
            "recovery_state": target_recovery._identity(after / "snapshot.sqlite3")[
                "recovery_state"
            ],
            "backend_records_sha256": target_recovery._identity(after / "snapshot.sqlite3")[
                "backend_records_sha256"
            ],
            "event_cursor": target_recovery._identity(after / "snapshot.sqlite3")["event_cursor"],
            "pv_uid": "pv",
        }

    monkeypatch.setattr(target_recovery, "_pod_backup", copy_backup)
    assert (
        target_recovery.backup_target("after")["state"]
        == "recreated_Pod_same_PVC_verified_record_prefix"
    )
