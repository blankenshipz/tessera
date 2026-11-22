"""Real temporary SQLite/files/claims, scripted transport; no cluster/fencing proof."""

import json
import time

import pytest
from test_schema_supervised import concrete
from test_stopped_observation import database, fixture, scripted

from tessera_operator import maintenance_witness as w


def reader(tmp_path, *, drift=False):
    pins, objects = fixture()
    observer, calls = scripted(pins, objects, database(tmp_path), change_after=drift)

    class Transport:
        def call(self, command):
            assert set(command) == {"argv"}
            return json.loads(observer.run(command["argv"]))

    plan = {
        "operation": "read-only-attempt",
        "deadline": time.time() + 60,
        "db_incarnation": "inc-1",
        "target": {k + "_uid": pins[k + "_uid"] for k in ("deployment", "pvc", "pv")},
    }
    return w.MaintenanceReadback(tmp_path / "attempt", transport=Transport()), plan, pins, calls


def test_real_claim_and_scripted_scope_never_become_fence(tmp_path):
    r, plan, pins, calls = reader(tmp_path)
    result = r.collect(plan, "scale_zero", pins)
    assert len(calls) == 9
    assert result["db"]["incarnation"] == "inc-1"
    assert result["runtime"]["observer_container_id"] == "containerd://123"
    assert result["continuous_fence"] is False and result["stopped_db_proof"] is False
    assert result["custody"] is None
    assert r.read(max_age=30) == result
    with pytest.raises(w.SupervisedDenied, match="host_writers.*guardian.*pod_replacement"):
        r.require_maintenance_preconditions(max_age=30)
    count = len(calls)
    with pytest.raises(FileExistsError):
        r.collect(plan, "scale_zero", pins)
    assert len(calls) == count


def test_failed_observation_retains_claim_failure_without_retry(tmp_path):
    r, plan, pins, calls = reader(tmp_path, drift=True)
    with pytest.raises(w.SupervisedDenied, match="retained claim"):
        r.collect(plan, "scale_zero", pins)
    failure, _ = w.read_json(r.directory / "failure.json")
    assert failure["resubmit"] is False
    count = len(calls)
    with pytest.raises(FileExistsError):
        r.collect(plan, "scale_zero", pins)
    assert len(calls) == count


def test_expiry_target_and_incarnation_deny(tmp_path):
    r, plan, pins, calls = reader(tmp_path)
    with pytest.raises(w.SupervisedDenied, match="deadline"):
        r.collect(plan | {"deadline": 0}, "scale_zero", pins)
    with pytest.raises(w.SupervisedDenied, match="target"):
        r.collect(plan, "scale_zero", pins | {"pvc_uid": "another"})
    assert calls == []
    with pytest.raises(w.SupervisedDenied, match="retained claim"):
        r.collect(plan | {"db_incarnation": "another"}, "scale_zero", pins)


def test_saved_report_hash_or_boolean_cannot_assert_authority(tmp_path):
    r, plan, pins, _ = reader(tmp_path)
    r.collect(plan, "scale_zero", pins)
    path = r.directory / "result.json"
    data = json.loads(path.read_text())
    data.update(continuous_fence=True, stopped_db_proof=True, unsupported={})
    path.write_text(json.dumps(data))
    with pytest.raises(w.SupervisedDenied, match="unsupported independent"):
        r.require_maintenance_preconditions(max_age=30)
    r.clock = lambda: plan["deadline"] + 1
    with pytest.raises(w.SupervisedDenied, match="stale"):
        r.read(max_age=120)


def test_evidence_symlink_and_oversize_deny(tmp_path):
    path = tmp_path / "file.json"
    path.write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(w.SupervisedDenied, match="noncanonical"):
        w.read_json(link)
    path.write_bytes(b"x" * 262145)
    with pytest.raises(w.SupervisedDenied, match="unsupported evidence"):
        w.read_json(path)


@pytest.mark.parametrize("wrong", [None, "manifest", "restore", "phase", "operation"])
def test_real_local_phase_custody_parsing_uses_retained_lineage(tmp_path, wrong):
    op, _, _, _, pin = concrete(tmp_path)
    for stage in ("scale_zero", "network_policy", "maintenance_pod", "backup"):
        pin(stage)
        op.prepare(stage)
        op.apply(stage)
    backup, _ = w.read_json(op.directory / "backup.result.json")
    custody = backup["remote"]["custody"]
    manifest = tmp_path / "manifest-copy.json"
    proof = tmp_path / "proof-copy.json"
    if wrong == "manifest":
        custody["manifest"]["backup_id"] = "other-valid-backup"
    elif wrong == "restore":
        custody["restore_receipt"]["held_db"]["incarnation"] = "another-restore"
    elif wrong in {"phase", "operation"}:
        backup["remote"][wrong] = "another"
    manifest.write_text(json.dumps(custody["manifest"], sort_keys=True))
    proof.write_text(json.dumps(custody["restore_receipt"], sort_keys=True))
    if wrong is None:
        report = w.custody_readback(op.plan(), "migrate", {"backup": backup}, manifest, proof)
        assert report["backup_id"] == custody["manifest"]["backup_id"]
        assert report["durable_custodian"] is None
        assert report["held_restore_proof"]["path"] == str(proof)
    else:
        # Manifest/proof copies differ from ORIGINAL producing-phase record.
        original, _ = w.read_json(op.directory / "backup.result.json")
        receipts = {"backup": backup if wrong in {"phase", "operation"} else original}
        with pytest.raises(w.SupervisedDenied):
            w.custody_readback(op.plan(), "migrate", receipts, manifest, proof)
