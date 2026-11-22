"""Actual temporary byte readback; topology is explicitly unsupported."""

import os

import maintenance_custody_fixture
import pytest
from test_maintenance_producers import ready

from tessera_operator.bundle_export import BundleExport
from tessera_operator.maintenance_composition import OwnerMaintenance
from tessera_operator.maintenance_witness import read_json
from tessera_operator.schema_supervised import SupervisedDenied


@pytest.fixture(autouse=True)
def stable_disposable_db_atime(monkeypatch):
    # Existing read_db compares full stat tuples including atime. Isolate these
    # disposable fixtures from APFS first-read atime updates; production code and
    # metadata drift guards stay unchanged. This is not live recovery evidence.
    original = maintenance_custody_fixture.read_db

    def read(root, relative):
        path = root / relative
        info = path.stat()
        os.utime(path, ns=(max(info.st_atime_ns, info.st_mtime_ns) + 60000000000, info.st_mtime_ns))
        return original(root, relative)

    monkeypatch.setattr(maintenance_custody_fixture, "read_db", read)


def composed(tmp_path, monkeypatch):
    hold, _, plan = ready(tmp_path, monkeypatch)
    owner = OwnerMaintenance(None, hold, hold.handoff, structured_api=None)
    exports = {
        phase: BundleExport(value["journal"], value["destination"], clock=lambda: 100)
        for phase, value in hold.handoff.fixture_exports.items()
    }
    return owner, plan, exports


def test_actual_storage_readback_is_not_reservation_or_topology(tmp_path, monkeypatch):
    owner, plan, exports = composed(tmp_path, monkeypatch)
    ref = owner.custody_readback(plan, exports)
    observation, _ = read_json(ref["path"])
    for phase in ("backup", "validate"):
        entry = observation["destinations"][phase]
        assert entry["verified_bytes"] > 0
        assert entry["available_bytes"] >= 0
        assert entry["reserved_bytes"] is None
        assert entry["topology"] == "unsupported"
    assert "custody" not in owner.hold.prerequisites
    with pytest.raises(SupervisedDenied, match="missing native owner-proven"):
        owner.release(plan, exports)


@pytest.mark.parametrize("fault", ["tamper", "public", "symlink", "expiry", "missing"])
def test_custody_refuses_changed_bytes_paths_or_expiry(tmp_path, monkeypatch, fault):
    owner, plan, exports = composed(tmp_path, monkeypatch)
    destination = exports["backup"].destination
    if fault == "tamper":
        file = next((destination / "export").rglob("*.sqlite3"), None)
        if file is None:
            file = next(p for p in (destination / "export").rglob("*") if p.is_file())
        file.write_bytes(b"tamper")
    elif fault == "public":
        destination.chmod(0o755)
    elif fault == "symlink":
        moved = destination.with_name("moved")
        destination.rename(moved)
        destination.symlink_to(moved, target_is_directory=True)
    elif fault == "expiry":
        owner.hold.clock = lambda: plan["deadline"]
    else:
        exports.pop("validate")
    with pytest.raises(SupervisedDenied):
        owner.custody_readback(plan, exports)
    assert not (owner.hold.directory / "custody.observation.json").exists()


def test_retained_reconnect_after_expiry_does_not_release_or_clean(tmp_path, monkeypatch):
    owner, plan, exports = composed(tmp_path, monkeypatch)
    owner.custody_readback(plan, exports)
    owner.hold.clock = lambda: plan["deadline"] + 1
    reopened = OwnerMaintenance(None, owner.hold, owner.handoff, structured_api=None)
    observation = reopened.reconcile_custody(plan, exports)
    assert observation["future_capacity"] == "observed_not_reserved"
    assert observation["retention"] == "no_auto_cleanup_retain_through_recovery"
    assert all(e.destination.exists() for e in exports.values())
    with pytest.raises(SupervisedDenied, match="claimed|expired"):
        reopened.custody_readback(plan, exports)
    assert not (owner.hold.directory / "release.claim.json").exists()


@pytest.mark.parametrize("fault", ["missing", "identity", "claim", "alias"])
def test_reconnect_detects_retained_custody_drift(tmp_path, monkeypatch, fault):
    owner, plan, exports = composed(tmp_path, monkeypatch)
    owner.custody_readback(plan, exports)
    exporter = exports["backup"]
    if fault in {"missing", "identity"}:
        file = next(p for p in (exporter.destination / "export").rglob("*") if p.is_file())
        content = file.read_bytes()
        file.unlink()
        if fault == "identity":
            file.write_bytes(content)
            file.chmod(0o600)
    elif fault == "claim":
        with (exporter.journal / "claim.json").open("a") as stream:
            stream.write(" ")
    else:
        exports["validate"] = exports["backup"]
    with pytest.raises((SupervisedDenied, OSError, ValueError)):
        owner.reconcile_custody(plan, exports)
    assert not (owner.hold.directory / "release.claim.json").exists()


def test_capacity_observation_not_a_reservation_and_no_replay(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tessera_operator import maintenance_composition as module

    owner, plan, exports = composed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.os, "statvfs", lambda path: SimpleNamespace(f_bavail=0, f_frsize=4096)
    )
    with pytest.raises(SupervisedDenied, match="capacity"):
        owner.custody_readback(plan, exports)
    assert (owner.hold.directory / "custody.claim.json").exists()
    assert not (owner.hold.directory / "custody.observation.json").exists()
    with pytest.raises(SupervisedDenied, match="claimed"):
        owner.custody_readback(plan, exports)
    assert all(e.destination.exists() for e in exports.values())


@pytest.mark.parametrize("fault", ["exclusive", "dispatch", "expired", "none"])
def test_release_keeps_existing_owner_drain_and_handoff_gates(tmp_path, monkeypatch, fault):
    # External topology/ownership values below remain SYNTHETIC fixture evidence.
    # This proves composition, not native topology or permission.
    from test_maintenance_hold import capture, setup

    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    owner = OwnerMaintenance(None, hold, handoff, structured_api=None)
    exports = {
        phase: BundleExport(value["journal"], value["destination"], clock=lambda: 100)
        for phase, value in handoff.fixture_exports.items()
    }
    handoff.apply(plan, pins, handoff.fixture_exports)
    if fault in {"exclusive", "dispatch"}:
        hold.prerequisites[fault]["sha256"] = "changed"
    elif fault == "expired":
        hold.clock = lambda: plan["deadline"]
    if fault != "none":
        with pytest.raises(SupervisedDenied):
            owner.release(plan, exports)
        assert api.calls == 1
        assert not (hold.directory / "release.claim.json").exists()
    else:
        owner.release(plan, exports)
        assert api.calls == 2
        result = hold.reconcile("release")
        assert result["state"] == "observed_unconfirmed"
        assert not result["completion_confirmed"]
        with pytest.raises(SupervisedDenied, match="native completion unavailable"):
            hold.released(plan)
        with pytest.raises(SupervisedDenied, match="claimed"):
            owner.release(plan, exports)
