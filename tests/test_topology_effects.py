"""External boundaries scripted. Child runner tests use harmless local Python."""

import json
import os
import sys

import pytest
from test_maintenance_topology import scene as scene

from tessera_operator import topology_effects as effects
from tessera_operator.schema_supervised import SupervisedDenied, digest


def durable_tree(root):
    """All durable entries, bytes and identities; exclude read-only atime."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            path.read_bytes() if path.is_file() else None,
        )
    return result


@pytest.fixture
def seam(scene, monkeypatch):
    root = scene["root"]
    root.chmod(0o700)
    (root / "phases").mkdir(mode=0o700)
    plan = scene["plan"]
    (root / "phases/plan.json").write_text(json.dumps(dict(value=plan, sha256=digest(plan))))
    monkeypatch.setattr(
        effects, "bounded_child", lambda argv, **kw: scene["execute"](tuple(argv), **kw)
    )
    original = effects.TopologyEffects.reconcile

    def invariant_reconcile(owner, operation):
        before = durable_tree(root)
        calls = len(scene["calls"])
        try:
            return original(owner, operation)
        finally:
            assert durable_tree(root) == before
            assert len(scene["calls"]) == calls

    monkeypatch.setattr(effects.TopologyEffects, "reconcile", invariant_reconcile)
    return effects.TopologyEffects(clock=lambda: 100), scene


@pytest.mark.parametrize("directory_exists", [False, True])
def test_unprepared_reconcile_never_creates_directory_lock_session(seam, directory_exists):
    owner, scene = seam
    directory = scene["root"] / "topology-effects"
    if directory_exists:
        directory.mkdir(mode=0o700)
    assert owner.reconcile("test") == dict(state="needs_prepare")
    assert not (directory / "lock").exists()
    assert not (directory / "session.json").exists()


def test_missing_operation_reconcile_does_not_create_it(seam):
    owner, scene = seam
    with pytest.raises(FileNotFoundError):
        owner.reconcile("missing")
    assert not (scene["root"].parent / "missing").exists()


def test_partial_capture_needs_explicit_prepare_for_next_packet(seam):
    owner, scene = seam
    packet = owner.prepare("test")["packet"]
    owner.effect("test", packet["digest"])
    assert owner.reconcile("test") == dict(state="needs_prepare")
    assert not (scene["root"] / "topology-effects/001.packet.json").exists()


@pytest.mark.parametrize("ending", ["finalize", "expire", "drift"])
def test_all_captured_not_finalized_reconcile_needs_explicit_finalize(seam, ending):
    owner, scene = seam
    for _ in range(48):
        packet = owner.prepare("test")["packet"]
        owner.effect("test", packet["digest"])
        observed = owner.reconcile("test")
        if observed["state"] == "needs_finalize":
            break
        assert observed == dict(state="needs_prepare")
    else:
        pytest.fail("collector failed to reach finalization boundary")
    assert not (scene["root"] / "topology.json").exists()
    assert owner.reconcile("test") == dict(state="needs_finalize")
    if ending == "expire":
        owner.clock = lambda: 161
        with pytest.raises(SupervisedDenied, match="expired"):
            owner.reconcile("test")
        return
    if ending == "drift":
        path = scene["root"] / "custody/backup"
        path.rename(path.with_name("prior"))
        path.mkdir(mode=0o700)
        with pytest.raises(SupervisedDenied, match="drift"):
            owner.reconcile("test")
        return
    # Only the explicitly writing prepare may publish the final receipt.
    finalized = owner.prepare("test")
    assert finalized["state"] == "complete"
    assert owner.reconcile("test") == finalized


def test_unprepared_expired_plan_does_not_start_session(seam):
    owner, _ = seam
    owner.clock = lambda: 201
    with pytest.raises(SupervisedDenied, match="expired"):
        owner.reconcile("test")


def test_join_and_reconnect_one_effect_at_a_time(seam):
    owner, scene = seam
    categories = set()
    for _ in range(48):
        prepared = owner.prepare("test")
        if prepared["state"] == "complete":
            break
        assert prepared["state"] == "prepared"
        packet = prepared["packet"]
        categories.add(packet["category"])
        calls = len(scene["calls"])
        # Reconnect does not dispatch or alter the packet.
        fresh = effects.TopologyEffects(clock=lambda: 100)
        assert fresh.reconcile("test") == prepared
        assert len(scene["calls"]) == calls
        assert owner.effect("test", packet["digest"])["state"] == "captured"
        assert len(scene["calls"]) == calls + 1
    else:
        pytest.fail("collector failed to converge")
    assert categories == {"metadata", "inspect", "nodeexec"}
    assert prepared["receipt"]["observed_at"] == 100
    assert prepared["receipt"]["valid_until"] == 160
    assert owner.reconcile("test") == prepared
    for path in (scene["root"] / "topology-effects").iterdir():
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert "NOT-IN-RECEIPT" not in path.read_text()


def test_wrong_digest_and_path_command_body_not_accepted(seam):
    owner, scene = seam
    packet = owner.prepare("test")["packet"]
    with pytest.raises(SupervisedDenied):
        owner.effect("test", "bad")
    with pytest.raises(TypeError):
        owner.effect("test", packet["digest"], argv=["echo", "bad"])
    with pytest.raises(SupervisedDenied):
        owner.prepare("../other")
    assert not scene["calls"]


def test_claimed_unknown_ack_never_reexecutes(seam):
    owner, scene = seam
    packet = owner.prepare("test")["packet"]
    directory = scene["root"] / "topology-effects"
    owner.put(directory, "000.claim.json", dict(packet=packet["digest"], claimed_at=100))
    assert owner.reconcile("test")["state"] == "unknown_ack"
    with pytest.raises(SupervisedDenied, match="unknown"):
        owner.effect("test", packet["digest"])
    assert not scene["calls"]


def test_failed_nodeexec_retained_no_retry(seam):
    owner, scene = seam
    while True:
        packet = owner.prepare("test")["packet"]
        if packet["category"] == "nodeexec":
            break
        owner.effect("test", packet["digest"])

    def denied(*args, **kwargs):
        raise PermissionError("DO-NOT-LOG-THIS")

    scene["hook"][0] = denied
    assert owner.effect("test", packet["digest"])["reason"] == "PermissionError"
    count = len(scene["calls"])
    for method in (owner.reconcile, owner.prepare):
        with pytest.raises(SupervisedDenied, match="never retry"):
            method("test")
    with pytest.raises(SupervisedDenied):
        owner.effect("test", packet["digest"])
    assert len(scene["calls"]) == count
    assert "DO-NOT-LOG-THIS" not in "".join(
        p.read_text() for p in (scene["root"] / "topology-effects").iterdir()
    )


def test_expiry_preserves_partial_output_no_new_effect(seam):
    owner, scene = seam
    packet = owner.prepare("test")["packet"]
    owner.effect("test", packet["digest"])
    path = scene["root"] / "topology-effects/000.result.json"
    saved = path.read_bytes()
    owner.clock = lambda: 161
    with pytest.raises(SupervisedDenied, match="expired"):
        owner.reconcile("test")
    assert path.read_bytes() == saved
    assert len(scene["calls"]) == 1


def test_successful_observations_not_current_after_join_expiry(seam):
    owner, scene = seam
    for _ in range(48):
        pending = owner.prepare("test")
        if pending["state"] == "complete":
            break
        owner.effect("test", pending["packet"]["digest"])
    else:
        pytest.fail("collector failed to converge")
    count = len(scene["calls"])
    owner.clock = lambda: 161
    with pytest.raises(SupervisedDenied, match="expired"):
        owner.reconcile("test")
    assert len(scene["calls"]) == count
    assert (scene["root"] / "topology.json").exists()


def test_observed_target_drift_fails_join_with_retained_partial_evidence(seam):
    owner, scene = seam
    for _ in range(48):
        pending = owner.prepare("test")
        packet = pending["packet"]
        if packet["category"] == "nodeexec":
            break
        owner.effect("test", packet["digest"])
    scene["resources"]["pvc"]["metadata"]["resourceVersion"] = "2"
    with pytest.raises(SupervisedDenied, match="raced"):
        for _ in range(48):
            pending = owner.prepare("test")
            owner.effect("test", pending["packet"]["digest"])
    assert not (scene["root"] / "topology.json").exists()
    assert (scene["root"] / "topology-effects/000.result.json").exists()


@pytest.mark.parametrize("drift", ["plan", "packet", "local", "output"])
def test_drift_rejected_without_effect(seam, drift):
    owner, scene = seam
    packet = owner.prepare("test")["packet"]
    root = scene["root"]
    if drift == "plan":
        plan = dict(scene["plan"], target={"pvc_uid": "other"})
        (root / "phases/plan.json").write_text(json.dumps(dict(value=plan, sha256=digest(plan))))
    elif drift == "packet":
        path = root / "topology-effects/000.packet.json"
        value = json.loads(path.read_text())
        value["argv"] = ["sh", "-c", "echo forbidden"]
        path.write_text(json.dumps(value))
    elif drift == "local":
        path = root / "custody/backup"
        path.rename(path.with_name("prior"))
        path.mkdir(mode=0o700)
    else:
        owner.effect("test", packet["digest"])
        path = root / "topology-effects/000.result.json"
        value = json.loads(path.read_text())
        value["output"] = "changed"
        path.write_text(json.dumps(value))
    count = len(scene["calls"])
    with pytest.raises(SupervisedDenied, match="drift"):
        owner.reconcile("test")
    assert len(scene["calls"]) == count


def test_bounded_harmless_child_runner():
    assert (
        effects.bounded_child([sys.executable, "-c", "print('ok')"], timeout=2, max_bytes=128)
        == "ok\n"
    )


@pytest.mark.parametrize(
    "program,error",
    [
        ("print('x'*10000)", BufferError),
        ("import sys; sys.stderr.write('x'*10000)", BufferError),
        ("import time; time.sleep(2)", TimeoutError),
        ("raise SystemExit(3)", ChildProcessError),
    ],
)
def test_harmless_child_runner_limits(program, error):
    with pytest.raises(error):
        effects.bounded_child([sys.executable, "-c", program], timeout=0.2, max_bytes=128)
