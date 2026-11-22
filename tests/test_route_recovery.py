"""Synthetic custody/preflight only; never import or invoke live entrypoints."""

import fcntl
import hashlib
import json
import os

import pytest

from tessera_operator import route_recovery as recovery


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    identity = dict.fromkeys(recovery.IDENTITY_KEYS, "synthetic")
    identity["image"] = "example.invalid/control@sha256:" + "a" * 64
    intent = {"pod_name": "tessera-control-synthetic", "pod_uid": "synthetic", "port": 18765}
    route = tmp_path / "local-persistent-route.json"
    route.write_bytes(json.dumps(intent).encode())
    route.chmod(0o600)
    digest = hashlib.sha256(route.read_bytes()).hexdigest()
    args = dict(
        directory=tmp_path,
        expected_intent_sha256=digest,
        expected_identity=identity,
        inspect_identity=lambda: dict(identity),
        inspect_processes=lambda: {"complete": True, "processes": [], "listeners": []},
    )
    return route, args


def test_custody_exact_bytes_idempotent_and_no_retirement(setup):
    route, args = setup
    original = route.read_bytes()
    before = route.stat().st_mtime_ns
    plan = recovery.prepare_custody(**args)
    bundle = route.parent / ("route-recovery-" + args["expected_intent_sha256"])
    assert (bundle / "original.json").read_bytes() == original
    timestamps = {p.name: p.stat().st_mtime_ns for p in bundle.iterdir()}
    assert recovery.prepare_custody(**args) == plan
    assert timestamps == {p.name: p.stat().st_mtime_ns for p in bundle.iterdir()}
    assert route.read_bytes() == original
    assert route.stat().st_mtime_ns == before
    assert plan["state"] == "custody_only_integration_required"
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in bundle.iterdir())
    with pytest.raises(recovery.RecoveryRefused, match="integration_required"):
        recovery.publish_reconciliation(all_writers_locked=True)
    assert route.read_bytes() == original


def test_stale_digest(setup):
    route, args = setup
    args["expected_intent_sha256"] = "0" * 64
    with pytest.raises(recovery.RecoveryRefused, match="stale_intent_digest"):
        recovery.prepare_custody(**args)
    assert not list(route.parent.glob("route-recovery-*"))


@pytest.mark.parametrize("extra", [{"process_id": 42}, {"started": "old"}, {"unknown": 1}])
def test_full_or_unknown_intent_refused(setup, extra):
    route, args = setup
    intent = json.loads(route.read_bytes()) | extra
    route.write_bytes(json.dumps(intent).encode())
    args["expected_intent_sha256"] = hashlib.sha256(route.read_bytes()).hexdigest()
    with pytest.raises(recovery.RecoveryRefused, match="exact_incomplete_intent"):
        recovery.prepare_custody(**args)


@pytest.mark.parametrize(
    "observation",
    [
        {"complete": False, "processes": [], "listeners": []},
        {"complete": True, "processes": [42], "listeners": []},
        {"complete": True, "processes": [], "listeners": ["foreign"]},
        {"complete": 1, "processes": [], "listeners": []},
        {},
    ],
)
def test_uninspectable_or_live_or_foreign_refused(setup, observation):
    _, args = setup
    args["inspect_processes"] = lambda: observation
    with pytest.raises(recovery.RecoveryRefused, match="process_or_listener_uncertain"):
        recovery.prepare_custody(**args)


@pytest.mark.parametrize("key", sorted(recovery.IDENTITY_KEYS))
def test_admitted_identity_drift(setup, key):
    _, args = setup
    args["inspect_identity"] = lambda: args["expected_identity"] | {key: "changed"}
    with pytest.raises(recovery.RecoveryRefused, match="admitted_identity_changed"):
        recovery.prepare_custody(**args)


def test_inspector_exception_sanitized(setup):
    _, args = setup

    def unavailable():
        raise PermissionError("SYNTHETIC_SECRET must not propagate")

    args["inspect_processes"] = unavailable
    with pytest.raises(recovery.RecoveryRefused, match="^inspection_unavailable$"):
        recovery.prepare_custody(**args)


def test_conflicting_supervisor_writer(setup):
    route, args = setup
    fd = os.open(route.parent / "local-route-supervisor.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(recovery.RecoveryRefused, match="conflicting_writer"):
            recovery.prepare_custody(**args)
    finally:
        os.close(fd)
    assert recovery.prepare_custody(**args)["version"] == 1


@pytest.mark.parametrize("failure_point", ["original", "plan", "staging_sync", "rename"])
def test_crash_retains_partial_custody_and_refuses_replay(setup, monkeypatch, failure_point):
    route, args = setup
    original = route.read_bytes()
    write = recovery._write
    sync = recovery._sync

    def failing_write(path, data):
        write(path, data)
        if path.name == failure_point + ".json":
            raise OSError("synthetic crash")

    def failing_sync(path):
        sync(path)
        if failure_point == "staging_sync":
            raise OSError("synthetic crash")

    def failing_rename(*_):
        raise OSError("synthetic crash")

    monkeypatch.setattr(recovery, "_write", failing_write)
    monkeypatch.setattr(recovery, "_sync", failing_sync)
    if failure_point == "rename":
        monkeypatch.setattr(recovery.os, "rename", failing_rename)
    with pytest.raises(OSError, match="synthetic crash"):
        recovery.prepare_custody(**args)
    assert route.read_bytes() == original
    assert list(route.parent.glob(".route-recovery-preparing-*"))
    with pytest.raises(recovery.RecoveryRefused, match="uncertain_preparation_retained"):
        recovery.prepare_custody(**args)


def test_crash_after_rename_rechecks_complete_bundle(setup, monkeypatch):
    route, args = setup
    sync = recovery._sync

    def fail_parent(path):
        if path == route.parent:
            raise OSError("synthetic parent sync failure")
        sync(path)

    monkeypatch.setattr(recovery, "_sync", fail_parent)
    with pytest.raises(OSError):
        recovery.prepare_custody(**args)
    monkeypatch.setattr(recovery, "_sync", sync)
    assert recovery.prepare_custody(**args)["state"] == "custody_only_integration_required"


def test_tampered_custody_refused(setup):
    route, args = setup
    recovery.prepare_custody(**args)
    bundle = route.parent / ("route-recovery-" + args["expected_intent_sha256"])
    (bundle / "original.json").write_bytes(b"changed")
    with pytest.raises(recovery.RecoveryRefused, match="custody_changed"):
        recovery.prepare_custody(**args)


def test_uncooperative_writer_during_inspection_does_not_clear_route(setup):
    route, args = setup
    count = 0

    def inspect():
        nonlocal count
        count += 1
        if count == 2:
            route.write_bytes(b"uncooperative writer")
        return {"complete": True, "processes": [], "listeners": []}

    args["inspect_processes"] = inspect
    with pytest.raises(recovery.RecoveryRefused, match="intent_changed_during_preparation"):
        recovery.prepare_custody(**args)
    assert route.read_bytes() == b"uncooperative writer"
    assert list(route.parent.glob(".route-recovery-preparing-*"))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_private_file_custody_required(setup, kind):
    route, args = setup
    if kind == "symlink":
        target = route.with_name("target")
        route.rename(target)
        route.symlink_to(target)
    elif kind == "hardlink":
        os.link(route, route.with_name("link"))
    else:
        route.chmod(0o644)
    with pytest.raises((OSError, recovery.RecoveryRefused)):
        recovery.prepare_custody(**args)
