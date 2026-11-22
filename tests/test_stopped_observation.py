"""Real disposable SQLite probe; scripted Kubernetes, not live stopped proof."""

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tessera_operator import stopped_observation as obs


def database(tmp_path):
    state = tmp_path / "control"
    state.mkdir()
    db = state / "tessera.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE schema_meta(schema_version INTEGER);"
            "INSERT INTO schema_meta VALUES(56);"
            "CREATE TABLE control_recovery(incarnation TEXT);"
            "INSERT INTO control_recovery VALUES('inc-1');"
        )
    return db


def test_real_read_induced_atime_only(tmp_path):
    db = database(tmp_path)
    os.utime(db, ns=(0, db.stat().st_mtime_ns))
    before = db.stat()
    assert obs.read_db(tmp_path, "control/tessera.sqlite3")["schema"] == 56
    after = db.stat()
    changed = {
        name
        for name in dir(before)
        if name.startswith("st_") and getattr(before, name) != getattr(after, name)
    }
    assert changed <= {"st_atime", "st_atime_ns"}


@pytest.mark.parametrize(
    "field",
    [
        "st_atime",
        "st_atime_ns",
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_rdev",
        "st_flags",
        "st_gen",
        "st_birthtime",
        "st_birthtime_ns",
        "st_blocks",
        "st_blksize",
    ],
)
def test_probe_stat_field_guard(tmp_path, monkeypatch, field):
    # Synthetic stat delta over real disposable SQLite: deterministic even on
    # filesystems that disable atime. Optional platform fields are exercised too.
    db = database(tmp_path)
    original_stat, original_digest = Path.stat, hashlib.file_digest
    read_started = False

    def digest(*args, **kwargs):
        nonlocal read_started
        result = original_digest(*args, **kwargs)
        read_started = True
        return result

    def stat(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == db and read_started:
            values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            values[field] = values.get(field, 0) + 1
            return SimpleNamespace(**values)
        return info

    monkeypatch.setattr(hashlib, "file_digest", digest)
    monkeypatch.setattr(Path, "stat", stat)
    if field in {"st_atime", "st_atime_ns"}:
        assert obs.read_db(tmp_path, "control/tessera.sqlite3")["schema"] == 56
    else:
        with pytest.raises(ValueError, match="DB changed during read"):
            obs.read_db(tmp_path, "control/tessera.sqlite3")


@pytest.mark.parametrize("fault", ["hash", "content", "-wal", "-shm", "-journal"])
def test_probe_hash_and_late_sidecar_guard(tmp_path, monkeypatch, fault):
    db = database(tmp_path)
    original = hashlib.file_digest
    calls = 0

    def digest(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1 and fault == "content":
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE schema_meta SET schema_version=58")
        elif calls == 1 and fault != "hash":
            Path(str(db) + fault).write_bytes(b"late disposable sidecar")
        if calls == 2 and fault == "hash":
            # Isolate the independent content-hash guard from stat protection.
            return SimpleNamespace(hexdigest=lambda: "0" * 64)
        return result

    monkeypatch.setattr(hashlib, "file_digest", digest)
    with pytest.raises(ValueError, match="DB changed during read"):
        obs.read_db(tmp_path, "control/tessera.sqlite3")
    assert calls == 2


def fixture():
    objects = {}
    pins = {
        "relative_db": "control/tessera.sqlite3",
        "observer_uid": "observer-uid",
        "observer_resource_version": "1",
        "observer_image": "test@sha256:" + "a" * 64,
        "observer_image_id": "test@sha256:" + "a" * 64,
        "observer_container_id": "containerd://123",
    }
    for key in ("deployment", "pvc", "pv"):
        pins[key + "_uid"] = key + "-uid"
        pins[key + "_resource_version"] = "1"
        objects[key] = {"metadata": {"name": key, "uid": key + "-uid", "resourceVersion": "1"}}
    objects["deployment"].update(
        spec={"replicas": 0}, status={"observedGeneration": 2, "replicas": 0}
    )
    objects["deployment"]["metadata"]["generation"] = 2
    objects["pvc"].update(spec={"volumeName": "pv"}, status={"phase": "Bound"})
    objects["pv"]["spec"] = {
        "claimRef": {
            "name": "tessera-control-state",
            "namespace": obs.target.NAMESPACE,
            "uid": "pvc-uid",
        }
    }
    spec = {
        "automountServiceAccountToken": False,
        "volumes": [
            {
                "name": "state",
                "persistentVolumeClaim": {"claimName": "tessera-control-state", "readOnly": True},
            }
        ],
        "containers": [
            {
                "name": "observer",
                "image": pins["observer_image"],
                "volumeMounts": [{"name": "state", "mountPath": "/state", "readOnly": True}],
            }
        ],
    }
    pins["observer_spec_sha256"] = obs.digest(spec)
    objects["pods"] = {
        "metadata": {"resourceVersion": "1"},
        "items": [
            {
                "metadata": {"name": "observer", "uid": "observer-uid", "resourceVersion": "1"},
                "spec": spec,
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "observer",
                            "ready": True,
                            "state": {"running": {"startedAt": "test"}},
                            "imageID": pins["observer_image_id"],
                            "containerID": pins["observer_container_id"],
                        }
                    ],
                },
            }
        ],
    }
    return pins, objects


def scripted(pins, objects, db, *, change_after=False):
    calls = []

    def run(argv):
        calls.append(argv)
        if "exec" in argv:
            assert argv[-6:-1] == ["--", "/app/.venv/bin/python", "-I", "-B", "-c"]
            return json.dumps(obs.read_db(db.parent.parent, "control/tessera.sqlite3")).encode()
        assert "get" in argv and not set(argv) & {"patch", "create", "apply", "delete", "scale"}
        kind = argv[argv.index("get") + 1]
        value = copy.deepcopy(objects[kind])
        if change_after and any("exec" in a for a in calls):
            value["metadata"]["resourceVersion"] = "2"
        return json.dumps(value).encode()

    return obs.StoppedObservation(pins, run=run), calls


def test_real_readonly_probe_and_generated_script(tmp_path):
    db = database(tmp_path)
    before = db.read_bytes()
    result = obs.read_db(tmp_path, "control/tessera.sqlite3")
    assert result["schema"] == 56 and result["incarnation"] == "inc-1"
    import inspect

    code = (
        inspect.getsource(obs.read_db)
        + f"\nimport json; print(json.dumps(read_db({str(tmp_path)!r},'control/tessera.sqlite3')))"
    )
    actual = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code], capture_output=True, check=True
    )
    assert json.loads(actual.stdout) == result
    assert db.read_bytes() == before and list(db.parent.iterdir()) == [db]


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal"])
def test_sidecar_blocks_without_sqlite_open(tmp_path, monkeypatch, sidecar):
    db = database(tmp_path)
    db.with_name(db.name + sidecar).touch()
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("SQLite opened"))
    with pytest.raises(ValueError, match="journal"):
        obs.read_db(tmp_path, "control/tessera.sqlite3")


def test_scripted_scoped_observation_never_claims_stopped(tmp_path):
    pins, objects = fixture()
    collector, calls = scripted(pins, objects, database(tmp_path))
    seen = collector.observe()
    assert seen["stopped_db_proof"] is False and seen["backup_custody"] is None
    assert seen["target"]["pvc"]["uid"] == "pvc-uid"
    assert seen["db"]["incarnation"] == "inc-1"
    assert len(calls) == 9 and sum("exec" in c for c in calls) == 1


@pytest.mark.parametrize(
    "failure", ["writer", "replicas", "generation", "uid", "version", "mount", "no_observer"]
)
def test_scripted_precondition_denial_never_executes(tmp_path, failure):
    pins, objects = fixture()
    if failure == "writer":
        writer = copy.deepcopy(objects["pods"]["items"][0])
        writer["metadata"]["uid"] = "writer"
        objects["pods"]["items"].append(writer)
    elif failure == "replicas":
        objects["deployment"]["status"]["replicas"] = 1
    elif failure == "generation":
        objects["deployment"]["status"]["observedGeneration"] = 1
    elif failure == "uid":
        objects["pvc"]["metadata"]["uid"] = "replacement"
    elif failure == "version":
        objects["pv"]["metadata"]["resourceVersion"] = "2"
    elif failure == "mount":
        objects["pods"]["items"][0]["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"] = (
            False
        )
    else:
        objects["pods"]["items"] = []
    collector, calls = scripted(pins, objects, database(tmp_path))
    with pytest.raises(obs.ObservationDenied):
        collector.observe()
    assert not any("exec" in c for c in calls)


def test_scripted_changed_after_read_denies_without_retry(tmp_path):
    pins, objects = fixture()
    collector, calls = scripted(pins, objects, database(tmp_path), change_after=True)
    with pytest.raises(obs.ObservationDenied):
        collector.observe()
    assert sum("exec" in c for c in calls) == 1


def test_no_mcp_registration_or_native_authority_claim():
    assert not hasattr(obs, "register")
    assert not hasattr(obs, "NativeMaintenanceGate")


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_db_alias_rejected(tmp_path, kind):
    db = database(tmp_path)
    alias = db.with_name("alias")
    if kind == "symlink":
        db.rename(alias)
        db.symlink_to(alias)
    else:
        alias.hardlink_to(db)
    with pytest.raises(ValueError):
        obs.read_db(tmp_path, "control/tessera.sqlite3")


@pytest.mark.parametrize("field", ["imageID", "containerID", "ready"])
def test_runtime_identity_denied_before_exec(tmp_path, field):
    pins, objects = fixture()
    runtime = objects["pods"]["items"][0]["status"]["containerStatuses"][0]
    runtime[field] = False if field == "ready" else "replaced"
    collector, calls = scripted(pins, objects, database(tmp_path))
    with pytest.raises(obs.ObservationDenied):
        collector.observe()
    assert not any("exec" in c for c in calls)


def test_denied_transport_no_retry():
    pins, _ = fixture()
    calls = []

    def denied(argv):
        calls.append(argv)
        raise PermissionError("synthetic native transport denial")

    with pytest.raises(obs.ObservationDenied):
        obs.StoppedObservation(pins, run=denied).observe()
    assert len(calls) == 1
