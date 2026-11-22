"""Synthetic maintenance bundles; separate unmodified app-format chunk/restore test."""

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_recovery_artifact_bundle import _placed_object

from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.recovery import backup_state, restore_state
from tessera_operator import bundle_export as b


@pytest.fixture(autouse=True)
def _private_umask():
    # Operator destinations must be owner-private; do not depend on the caller's umask.
    previous = os.umask(0o077)
    yield
    os.umask(previous)


_RETAINED_APPLICATION_CHECK = r"""
import hashlib,json,sqlite3,sys,time
from pathlib import Path
source,repo,work,schema=Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),int(sys.argv[4])
sys.path[:0]=[str(source/'src'),str(repo/'tests'),str(repo/'src')]
from tessera_control.migrations import LATEST_SCHEMA_VERSION
assert LATEST_SCHEMA_VERSION==schema
from test_recovery_artifact_bundle import _placed_object
from tessera_control.recovery import backup_state,restore_state
import tessera_operator
tessera_operator.__path__.insert(0,str(repo/'src/tessera_operator'))
from tessera_operator.bundle_export import BundleExport,read_chunk
state,_,placement=_placed_object(work)
scratch=work/'scratch';scratch.mkdir(mode=0o700)
phase='backup' if schema==56 else 'validate'
prefix='pre' if schema==56 else 'post'
manifest=backup_state(state,scratch/prefix)
assert manifest['schema_version']==schema and manifest['format_version']==2
restore_state(scratch/prefix,work/'held')
def dbinfo(path):
    before=path.stat()
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True) as c:
        version=c.execute('SELECT schema_version FROM schema_meta').fetchone()[0]
        incarnation=c.execute('SELECT incarnation FROM control_recovery').fetchone()[0]
    after=path.stat()
    assert (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(
        after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==sha
    assert version==schema
    return dict(schema=version,incarnation=incarnation,sha256=sha,
                device=after.st_dev,inode=after.st_ino,size=after.st_size)
snapshot=dbinfo(scratch/prefix/'snapshot.sqlite3')
db=dbinfo(state/'tessera.sqlite3')
held=dbinfo(work/'held/tessera.sqlite3')
with sqlite3.connect(work/'held/tessera.sqlite3') as c:
    assert c.execute('SELECT state,source_backup_digest FROM control_recovery').fetchone()==(
        'held_restore',manifest['sha256'])
assert held['incarnation']!=db['incarnation']==snapshot['incarnation']
target=dict(deployment_uid='scripted-dep',pvc_uid='scripted-pvc',pv_uid='scripted-pv')
operation='real-bundle-'+str(schema)
sha=lambda raw:hashlib.sha256(raw).hexdigest()
manifest_sha=sha((scratch/prefix/'manifest.json').read_bytes())
proof=dict(operation=operation,phase=phase,target=target,source_incarnation=db['incarnation'],
    input_db_sha256=db['sha256'],post_db_sha256=db['sha256'],manifest_sha256=manifest_sha,
    snapshot_sha256=snapshot['sha256'],held_db=held,state='held_restore')
raw=json.dumps(proof,sort_keys=True).encode()
(scratch/(phase+'.restore-proof.json')).write_bytes(raw)
receipt={'remote':dict(operation=operation,phase=phase,target=target,
    input_db_sha256=db['sha256'],db=db,custody=dict(manifest=manifest,
    manifest_sha256=manifest_sha,restore_receipt=proof,restore_receipt_sha256=sha(raw)))}
plan=dict(operation=operation,deadline=time.time()+60,target=target,
          db_incarnation=db['incarnation'])
class LocalSource:
    identity=dict(operation=operation,phase=phase,root=str(scratch),transport='scripted-local')
    calls=0
    def read(self,name,offset,count):
        self.calls+=1
        return read_chunk(scratch,name,offset,count)
destination=work/'destination';destination.mkdir(mode=0o700)
export=BundleExport(work/'export-journal',destination)
transport=LocalSource()
result=export.export(plan,phase,receipt,transport)
calls=transport.calls
assert export.reconcile()['files']==result['files']
assert transport.calls==calls
bundle=destination/'export'/prefix
assert (bundle/'manifest.json').read_bytes()==(scratch/prefix/'manifest.json').read_bytes()
restore_state(bundle,work/'reconstructed')
with sqlite3.connect(work/'reconstructed/tessera.sqlite3') as c:
    assert c.execute('SELECT schema_version FROM schema_meta').fetchone()==(schema,)
    assert c.execute('SELECT state,source_backup_digest FROM control_recovery').fetchone()==(
        'held_restore',manifest['sha256'])
    assert c.execute('SELECT COUNT(*) FROM project_artifact_placements').fetchone()[0]>=1
obj=work/'reconstructed/project-artifacts'/placement['tenant']/placement['project']/placement['key']
assert obj.read_bytes()==b'bundle object bytes'
summary=dict(schema=schema,phase=phase,files=len(result['files']),object_sha256=sha(obj.read_bytes()),
    snapshot_sha256=manifest['sha256'],manifest_sha256=manifest_sha,
    application='genuine pinned source setup/backup/restore; no relabel or verifier override',
    transport='scripted local source, not cluster or offhost custody')
(work/'accepted.json').write_text(json.dumps(summary,sort_keys=True))
print(json.dumps(summary,sort_keys=True))
"""


@pytest.mark.parametrize("schema", [56, 58])
def test_retained_application_backup_export_restore(tmp_path, schema):
    """Opt-in exact retained source; default skips without touching frozen inputs."""
    if os.environ.get("TESSERA_RETAINED_BUNDLE_CHECK") != "1":
        pytest.skip("opt-in retained schema56/58 source prerequisite")
    repo = Path(__file__).resolve().parents[1]
    manifest_path = Path(
        "/private/tmp/opencode/native74-authentic-fixture-review-ihhgi1ab/"
        "candidate-reviewed-manifest.json"
    )
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == (
        "b233a5977a6acfff279d83025ae7ce283ae28df17fd699f36b235f4f13e84ef0"
    )
    reviewed = json.loads(manifest_path.read_bytes())
    if schema == 58:
        source = Path(reviewed["source_root"])
        inventory = reviewed["candidate_files"]
    else:
        source = (
            repo
            / ".tessera/private/local-control-upgrade/builds/"
            / ("1e3f6de6a54b4d68b93869f909de18b7/context")
        )
        inventory = reviewed["baseline_files"]
    inputs = {name: digest for name, digest in inventory.items() if name.startswith("src/")}
    assert inputs

    def verify():
        for name, digest in inputs.items():
            path = source / name
            assert path.resolve() == path and path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name

    verify()
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _RETAINED_APPLICATION_CHECK,
            str(source),
            str(repo),
            str(tmp_path),
            str(schema),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    (tmp_path / "child.stdout").write_bytes(completed.stdout)
    (tmp_path / "child.stderr").write_bytes(completed.stderr)
    (tmp_path / "provenance.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "schema": schema,
                "manifest": str(manifest_path),
                "inputs": inputs,
                "exit_code": completed.returncode,
            },
            sort_keys=True,
        )
    )
    verify()
    assert completed.returncode == 0, (
        f"schema{schema} failed; retained {tmp_path}/child.stderr; "
        f"{completed.stderr[-2000:].decode(errors='replace')}"
    )
    assert json.loads((tmp_path / "accepted.json").read_bytes())["schema"] == schema


def fixture_db(path, incarnation):
    # Construct a disposable minimal DB, never relabel an application database.
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE schema_meta(schema_version INTEGER);"
            "INSERT INTO schema_meta VALUES(58);"
            "CREATE TABLE control_recovery(incarnation TEXT);"
        )
        conn.execute("INSERT INTO control_recovery VALUES(?)", (incarnation,))
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as conn:
        assert conn.execute("SELECT schema_version FROM schema_meta").fetchone() == (58,)
        assert conn.execute("SELECT incarnation FROM control_recovery").fetchone() == (incarnation,)
    return {
        "schema": 58,
        "incarnation": incarnation,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "device": after.st_dev,
        "inode": after.st_ino,
        "size": len(raw),
    }


def setup(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    (scratch / "post").mkdir(mode=0o700)
    snapshot = fixture_db(scratch / "post/snapshot.sqlite3", "source")
    placement = {"tenant": "a" * 64, "project": "b" * 64, "key": "c" * 64}
    object_path = (
        scratch / "post/objects" / placement["tenant"] / placement["project"] / placement["key"]
    )
    object_path.parent.mkdir(parents=True, mode=0o700)
    object_path.write_bytes(b"bundle object bytes")
    manifest = {
        "backup_id": "d" * 32,
        "created_at": "2026-10-24T00:00:00Z",
        "event_watermark": 0,
        "format_version": 2,
        "schema_version": 58,
        "sha256": snapshot["sha256"],
        "size": snapshot["size"],
        "legacy_artifact_ids": [],
        "objects": [
            {
                "artifact_id": "synthetic",
                "tenant_namespace": placement["tenant"],
                "project_namespace": placement["project"],
                "object_key": placement["key"],
                "size": 19,
                "sha256": hashlib.sha256(b"bundle object bytes").hexdigest(),
                "availability": "present",
            }
        ],
    }
    (scratch / "post/manifest.json").write_bytes(
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    held = fixture_db(scratch / "held.sqlite3", "distinct-held")
    db = snapshot
    plan = {
        "operation": "export-89",
        "deadline": time.time() + 120,
        "target": {"deployment_uid": "dep", "pvc_uid": "pvc", "pv_uid": "pv"},
        "db_incarnation": db["incarnation"],
    }

    def sha(raw):
        return hashlib.sha256(raw).hexdigest()

    manifest_sha = sha((scratch / "post/manifest.json").read_bytes())
    proof = {
        "operation": plan["operation"],
        "phase": "validate",
        "target": plan["target"],
        "source_incarnation": db["incarnation"],
        "input_db_sha256": db["sha256"],
        "post_db_sha256": db["sha256"],
        "manifest_sha256": manifest_sha,
        "snapshot_sha256": snapshot["sha256"],
        "state": "held_restore",
        "held_db": held,
    }
    raw = json.dumps(proof, sort_keys=True).encode()
    (scratch / "validate.restore-proof.json").write_bytes(raw)
    remote = {
        "operation": plan["operation"],
        "phase": "validate",
        "target": plan["target"],
        "input_db_sha256": db["sha256"],
        "db": db,
        "custody": {
            "manifest": manifest,
            "manifest_sha256": manifest_sha,
            "restore_receipt": proof,
            "restore_receipt_sha256": sha(raw),
        },
    }

    class LocalSource:
        identity = {"root": str(scratch), "operation": "export-89", "phase": "validate"}
        calls = 0

        def read(self, name, offset, count):
            self.calls += 1
            return b.read_chunk(scratch, name, offset, count)

    destination = tmp_path / "destination"
    destination.mkdir(mode=0o700)
    return (
        b.BundleExport(tmp_path / "journal", destination),
        plan,
        {"remote": remote},
        LocalSource(),
        placement,
    )


def test_synthetic_maintenance_export_exact_bytes_and_duplicates(tmp_path):
    export, plan, receipt, source, placement = setup(tmp_path)
    result = export.export(plan, "validate", receipt, source)
    assert "not off-host custody" in result["coverage"]
    assert result["target"] == plan["target"]
    root = export.destination / "export/post"
    assert (root / "manifest.json").read_bytes() == (
        tmp_path / "scratch/post/manifest.json"
    ).read_bytes()
    obj = root / "objects"
    assert (
        obj / placement["tenant"] / placement["project"] / placement["key"]
    ).read_bytes() == b"bundle object bytes"
    with sqlite3.connect(root / "snapshot.sqlite3") as db:
        assert db.execute("SELECT incarnation FROM control_recovery").fetchone() == ("source",)
    count = source.calls
    assert export.reconcile()["files"] == result["files"]
    with pytest.raises(FileExistsError):
        export.export(plan, "validate", receipt, source)
    assert source.calls == count


def test_actual_current_application_chunk_bundle_reconstructs_without_schema_relabel(tmp_path):
    # Exercises actual recovery output + RO chunk probe + actual restore, NOT the
    # maintenance phase exporter: checkout schema57 is intentionally inadmissible.
    state, _, placement = _placed_object(tmp_path)
    original = tmp_path / "application-backup"
    manifest = backup_state(state, original)
    assert manifest["schema_version"] == LATEST_SCHEMA_VERSION
    copied = tmp_path / "application-copy"
    copied.mkdir()
    for path in original.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(original).as_posix()
        target = copied / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        offset = 0
        with target.open("xb") as output:
            while True:
                value = b.read_chunk(original, relative, offset, b.CHUNK)
                chunk = base64.b64decode(value["data"], validate=True)
                output.write(chunk)
                offset += len(chunk)
                if offset == value["size"]:
                    break
        assert target.read_bytes() == path.read_bytes()
    restore_state(copied, tmp_path / "actual-held")
    obj = tmp_path / "actual-held/project-artifacts"
    assert (
        obj / placement["tenant"] / placement["project"] / placement["key"]
    ).read_bytes() == b"bundle object bytes"
    with sqlite3.connect(tmp_path / "actual-held/tessera.sqlite3") as conn:
        assert conn.execute("SELECT state FROM control_recovery").fetchone() == ("held_restore",)
        assert conn.execute("SELECT schema_version FROM schema_meta").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )
    export, plan, receipt, source, _ = setup(tmp_path)
    receipt["remote"]["db"]["schema"] = 57
    with pytest.raises(b.SupervisedDenied, match="phase identity"):
        export.export(plan, "validate", receipt, source)
    assert source.calls == 0


@pytest.mark.parametrize("reformat", [False, True])
def test_exact_manifest_bytes_not_guessed_serialization(tmp_path, reformat):
    export, plan, receipt, source, _ = setup(tmp_path)
    path = tmp_path / "scratch/post/manifest.json"
    custody = receipt["remote"]["custody"]
    raw = json.dumps(custody["manifest"], indent=2).encode() + b"\n\n"
    path.write_bytes(raw)
    if reformat:
        custody["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        custody["restore_receipt"]["manifest_sha256"] = custody["manifest_sha256"]
        proof = json.dumps(custody["restore_receipt"], sort_keys=True).encode()
        (tmp_path / "scratch/validate.restore-proof.json").write_bytes(proof)
        custody["restore_receipt_sha256"] = hashlib.sha256(proof).hexdigest()
        export.export(plan, "validate", receipt, source)
        assert (export.destination / "export/post/manifest.json").read_bytes() == raw
    else:
        with pytest.raises(b.SupervisedDenied, match="uncertain"):
            export.export(plan, "validate", receipt, source)
        assert (export.journal / "failure.json").exists()


@pytest.mark.parametrize("fault", ["missing", "symlink", "hardlink", "drift", "timeout"])
def test_partial_failure_preserved_no_retry_or_recopy(tmp_path, fault):
    export, plan, receipt, source, _ = setup(tmp_path)
    snapshot = tmp_path / "scratch/post/snapshot.sqlite3"
    original = snapshot.read_bytes()
    if fault == "missing":
        snapshot.unlink()
    elif fault in {"symlink", "hardlink"}:
        other = tmp_path / "other"
        snapshot.rename(other)
        if fault == "symlink":
            snapshot.symlink_to(other)
        else:
            import os

            os.link(other, snapshot)
    elif fault == "drift":
        snapshot.write_bytes(b"x" + original[1:])
    else:
        read = source.read

        def expired(*args):
            value = read(*args)
            export.clock = lambda: plan["deadline"] + 1
            return value

        source.read = expired
    with pytest.raises(b.SupervisedDenied, match="uncertain"):
        export.export(plan, "validate", receipt, source)
    assert (export.journal / "claim.json").is_file()
    assert (export.journal / "failure.json").is_file()
    assert not (export.journal / "result.json").exists()
    count = source.calls
    with pytest.raises((b.SupervisedDenied, FileExistsError)):
        export.export(plan, "validate", receipt, source)
    with pytest.raises((b.SupervisedDenied, ValueError)):
        export.reconcile()
    assert source.calls == count
    assert (export.destination / "export").exists()


def test_lost_ack_readback_only_no_transfer_retry(tmp_path, monkeypatch):
    export, plan, receipt, source, _ = setup(tmp_path)
    write = b.target._write_once

    def lost(path, value):
        if path.name == "result.json":
            raise TimeoutError("scripted lost local acknowledgement")
        return write(path, value)

    monkeypatch.setattr(b.target, "_write_once", lost)
    with pytest.raises(b.SupervisedDenied, match="uncertain"):
        export.export(plan, "validate", receipt, source)
    count = source.calls
    assert export.reconcile()["operation"] == plan["operation"]
    assert source.calls == count
    assert (export.journal / "failure.json").exists()


@pytest.mark.parametrize("fault", ["target", "held", "manifest", "path"])
def test_lineage_and_unsafe_inventory_deny_without_snapshot_transfer(tmp_path, fault):
    export, plan, receipt, source, _ = setup(tmp_path)
    custody = receipt["remote"]["custody"]
    if fault == "target":
        receipt["remote"]["target"] = {"pvc_uid": "another"}
    elif fault == "held":
        custody["restore_receipt"]["source_incarnation"] = "another"
    elif fault == "manifest":
        custody["manifest"]["size"] += 1
    else:
        custody["manifest"]["objects"][0]["object_key"] = "../../escape"
    with pytest.raises(b.SupervisedDenied):
        export.export(plan, "validate", receipt, source)
    if fault == "manifest":
        # Structural size claim is checked against exact metadata bytes after
        # claiming, not guessed by serializing the receipt's parsed JSON.
        assert source.calls == 1
        assert (export.journal / "claim.json").exists()
        assert (export.journal / "failure.json").exists()
        assert not (export.destination / "export/post/snapshot.sqlite3").exists()
    else:
        assert source.calls == 0
        assert not export.journal.exists()


def test_destination_hash_drift_links_extra_bytes_deny(tmp_path):
    export, plan, receipt, source, _ = setup(tmp_path)
    export.export(plan, "validate", receipt, source)
    snapshot = export.destination / "export/post/snapshot.sqlite3"
    raw = snapshot.read_bytes()
    snapshot.write_bytes(b"x" + raw[1:])
    with pytest.raises(b.SupervisedDenied, match="checksum"):
        export.reconcile()
    snapshot.write_bytes(raw)
    extra = export.destination / "export/extra"
    extra.write_text("unexpected")
    with pytest.raises(b.SupervisedDenied, match="inventory"):
        export.reconcile()


def test_scripted_pod_adapter_uses_fixed_source_and_rejects_replacement(tmp_path):
    class Transport:
        changed = False
        calls = []

        def call(self, command):
            argv = command["argv"]
            self.calls.append(argv)
            if "exec" in argv:
                assert "'/scratch'" in argv[-1] and "read_chunk" in argv[-1]
                return {"data": "eA==", "size": 1}
            return {
                "metadata": {"uid": "new" if self.changed else "pod"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": "new",
                            "containerID": "runtime",
                            "imageID": "image",
                            "state": {"running": {}},
                        }
                    ]
                },
            }

    transport = Transport()
    source = b.PodBundleSource(
        "export-89",
        "validate",
        {"uid": "pod", "containerID": "runtime", "imageID": "image"},
        transport=transport,
    )
    assert source.read("post/manifest.json", 0, 1) == {"data": "eA==", "size": 1}
    assert len(transport.calls) == 3
    transport.changed = True
    with pytest.raises(b.SupervisedDenied, match="runtime"):
        source.read("post/manifest.json", 0, 1)
