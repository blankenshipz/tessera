"""Attestation classes for the local fixture. Offline/synthetic: no Docker contact."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from test_local_docker_fixture import CID, IMAGE_ID, FakeEngine, plan
from test_local_docker_namespace import observations

from tessera_worker import local_docker_namespace as net
from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.capability_journal import JournalConflict
from tessera_worker.local_docker_contract import FixturePlan, LocalFixtureConfig
from tessera_worker.local_docker_engine import LocalDockerEngine
from tessera_worker.local_docker_fixture import (
    FixtureJournal,
    LocalFixtureAdapter,
    recover,
    supervise,
)

STRONG, WEAK = net.STRONG_CLASS, net.WEAK_CLASS
WEAK_PROFILE = "local-networkless-fixture-engine-attested-v1"

# The model_dump is byte-identical to base bf1b5d8. The digests were re-captured when
# the /output volume spec (exact name, driver, options) became part of the package.
GOLDEN_V1_DIGEST = "9123d9e0c6a399d50faa494c236c550414666dbbc33243695a92ccef3d66bdde"
GOLDEN_V1_WAIT7_DIGEST = "dbeafbaee8e70edf88f0e6bfd963d5a599439de1764a0c8eb843f936202c534c"
GOLDEN_V1_KEYS = {
    "cpu_millicores",
    "filesystem_bytes_per_mount",
    "filesystem_inodes_per_mount",
    "image",
    "input_sha256",
    "memory_bytes",
    "mode",
    "nonce",
    "operation_id",
    "org_id",
    "output_bytes",
    "pid_limit",
    "profile",
    "project_id",
    "runtime_seconds",
    "worker_id",
}


def weak_plan(**changes):
    return plan(profile=WEAK_PROFILE, **changes)


# ---------------------------------------------------------------- digests / config


def test_v1_plan_digest_and_dump_are_byte_identical_to_base():
    assert plan().digest == GOLDEN_V1_DIGEST
    assert plan(mode="wait", runtime_seconds=7).digest == GOLDEN_V1_WAIT7_DIGEST
    dump = plan().model_dump(mode="json")
    assert set(dump) == GOLDEN_V1_KEYS
    assert dump["profile"] == "local-networkless-fixture-v1"
    assert plan().labels["tessera.fixture"] == "local-networkless-fixture-v1"
    assert plan().attestation_class == STRONG


def test_weak_plan_has_distinct_digest_labels_and_class():
    p, w = plan(), weak_plan()
    assert w.digest != p.digest
    assert w.labels["tessera.fixture"] == WEAK_PROFILE
    assert w.labels["tessera.package"] != p.labels["tessera.package"]
    assert w.attestation_class == WEAK
    assert w.attestation == dict(
        attestation_class=WEAK, observer_independence="none_engine_and_image_trusted"
    )
    assert p.attestation == dict(
        attestation_class=STRONG, observer_independence="independent_daemon_host_reader"
    )


@pytest.mark.parametrize("profile", ["", "local-networkless-fixture-v2", "weak", WEAK, None])
def test_unknown_profile_rejected(profile):
    with pytest.raises(ValueError):
        plan(profile=profile)


def test_default_config_is_strong_and_unknown_class_rejected():
    assert LocalFixtureConfig().attestation_class == STRONG
    for value in ("", "weak", "Engine_Declared_Workload_Observed", None):
        with pytest.raises(ValueError):
            LocalFixtureConfig(enabled=True, attestation_class=value)
    assert LocalFixtureConfig(enabled=True, attestation_class=WEAK).attestation_class == WEAK


@pytest.mark.parametrize(
    "p,cls",
    [(weak_plan(), STRONG), (plan(), WEAK)],
    ids=["weak-plan-strong-cfg", "strong-plan-weak-cfg"],
)
def test_class_mismatch_blocks_before_any_effect(tmp_path, monkeypatch, p, cls):
    spawned = []
    monkeypatch.setattr(
        "tessera_worker.local_docker_fixture.multiprocessing.get_context",
        lambda *a: spawned.append(a) or pytest.fail("no process may be spawned"),
    )
    adapter = LocalFixtureAdapter(LocalFixtureConfig(enabled=True, attestation_class=cls))
    root = tmp_path / "evidence"
    with pytest.raises(BackendBlocked, match="attestation class mismatch"):
        adapter.launch(p, root)
    assert not root.exists() and not spawned and list(tmp_path.iterdir()) == []


def test_child_also_rejects_mismatch_before_journal(tmp_path):
    from tessera_worker.local_docker_fixture import _child

    root = tmp_path / "j"
    with pytest.raises(BackendBlocked, match="attestation class mismatch"):
        _child(root, weak_plan(), LocalFixtureConfig(enabled=True), None, b"k" * 32)
    assert not root.exists()


# ---------------------------------------------------------------- weak verifier


def weak_observations():
    p, _host, o = observations()
    p = weak_plan()
    o["nonce"], o["package"] = p.nonce, p.digest
    value = dict(
        Id=CID,
        Name="/" + p.name,
        State=dict(StartedAt="synthetic", Running=True),
        HostConfig=dict(NetworkMode="none"),
        NetworkSettings=dict(Networks={"none": {}}),
        Config=dict(Entrypoint=["/usr/local/bin/python"], Cmd=["-I"]),
    )
    peers = [dict(id=CID, net="none", pid="", ipc="none")]
    snap = net.EngineDeclaredIdentity().snapshot(value, peers)
    for key in ("before", "after"):
        o[key]["command_sha256"] = snap["command_sha256"]
    return p, value, peers, snap, o


def weak_verify(snap, o, p, after=None):
    return net.verify_engine_declared_observation(
        json.dumps(o).encode(), snap, snap if after is None else after, p
    )


def test_complete_weak_observation_is_workload_observed_never_verified():
    p, _value, _peers, snap, o = weak_observations()
    result = weak_verify(snap, o, p)
    assert result["state"] == "workload_observed" != "verified"
    assert result["attestation_class"] == WEAK
    assert result["observer_independence"] == "none_engine_and_image_trusted"


def test_weak_verifier_uses_no_host_procfs_or_namespace_reader(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("independent daemon-host reader must not run in the weak class")

    monkeypatch.setattr(net, "process_identity", forbidden)
    monkeypatch.setattr(net, "bounded_text", forbidden)
    monkeypatch.setattr(net.os, "stat", forbidden)
    monkeypatch.setattr(net.os, "readlink", forbidden)
    p, value, peers, _snap, o = weak_observations()
    snap = net.EngineDeclaredIdentity().snapshot(value, peers)
    assert weak_verify(snap, o, p)["state"] == "workload_observed"


# The same in-workload faults as the strong class, same gates.
WORKLOAD_FAULTS = [
    ({"sysfs_interfaces": ["eth0", "lo"]}, "ambiguous_sysfs_visibility"),
    (
        {"interfaces": [dict(index=1, name="lo", flags=73), dict(index=2, name="eth0", flags=1)]},
        "unexpected_interface",
    ),
    ({"proxy": True}, "proxy_bypass_unknown"),
    ({"observer_pid": 1}, "observer_process_unknown"),
    ({"uid": 0}, "observer_process_unknown"),
    ({"routes": []}, "route_observation_unknown"),
    ({"addresses": []}, "unexpected_address"),
    ({"sysfs_mounts": []}, "sysfs_mount_identity_unknown"),
    ({"status": ""}, "observer_security_unknown"),
    ({"nonce": "3" * 64}, "namespace_wrong_operation"),
]


@pytest.mark.parametrize("change,gate", WORKLOAD_FAULTS)
def test_workload_faults_block_both_classes_with_same_gate(change, gate):
    p, host, o = observations()
    o.update(change)
    with pytest.raises(net.NamespaceUnknown, match=gate):
        net.verify_observation(json.dumps(o).encode(), host, host, p)
    wp, _value, _peers, snap, wo = weak_observations()
    wo.update(change)
    with pytest.raises(net.NamespaceUnknown, match=gate):
        weak_verify(snap, wo, wp)


@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
@pytest.mark.parametrize(
    "change",
    [dict(positive=False), dict(unreachable=0), dict(unreachable=110), dict(refused=0)],
)
def test_weak_connectivity_controls_block(family, change):
    p, _value, _peers, snap, o = weak_observations()
    o["probes"][family].update(change)
    with pytest.raises(net.NamespaceUnknown, match="connectivity_unknown_or_unexpected"):
        weak_verify(snap, o, p)


@pytest.mark.parametrize("change", [dict(index=2), dict(gateway=True), dict(type=1)])
def test_weak_route_faults_block(change):
    p, _value, _peers, snap, o = weak_observations()
    o["routes"][0].update(change)
    with pytest.raises(net.NamespaceUnknown, match="unexpected_route"):
        weak_verify(snap, o, p)


@pytest.mark.parametrize("field", ["interfaces", "addresses", "routes", "probes", "proxy"])
def test_weak_missing_observation_never_passes(field):
    p, _value, _peers, snap, o = weak_observations()
    del o[field]
    with pytest.raises(net.NamespaceUnknown):
        weak_verify(snap, o, p)


@pytest.mark.parametrize("raw", [b"{}", b"not json", b"x" * 16385, b'{"unknown":"anything"}'])
def test_weak_unavailable_or_malformed_blocks(raw):
    p, _value, _peers, snap, _o = weak_observations()
    with pytest.raises(net.NamespaceUnknown):
        net.verify_engine_declared_observation(raw, snap, snap, p)


@pytest.mark.parametrize(
    "mode_field,mode",
    [
        ("net", "container:" + CID),
        ("pid", "container:" + CID),
        ("ipc", "container:" + CID),
        ("net", "container:" + CID[:12]),
        ("net", "container:tessera-local-" + "2" * 64),
    ],
)
def test_container_peer_sharing_workload_blocks(mode_field, mode):
    _p, value, peers, _snap, _o = weak_observations()
    peers.append(dict(id="d" * 64, net="none", pid="", ipc="none") | {mode_field: mode})
    with pytest.raises(net.NamespaceUnknown, match="engine_declared_shared_namespace"):
        net.EngineDeclaredIdentity().snapshot(value, peers)


def test_unrelated_container_peer_and_unknown_inventory():
    _p, value, peers, _snap, _o = weak_observations()
    peers.append(dict(id="d" * 64, net="container:" + "e" * 64, pid="", ipc="none"))
    net.EngineDeclaredIdentity().snapshot(value, peers)  # references someone else
    with pytest.raises(net.NamespaceUnknown, match="peer_inventory_unknown"):
        net.EngineDeclaredIdentity().snapshot(value, [dict(id="d" * 64, net="none")])
    with pytest.raises(net.NamespaceUnknown):
        net.EngineDeclaredIdentity().snapshot(value, [])


def test_engine_declared_network_must_be_none():
    _p, value, peers, _snap, _o = weak_observations()
    bad = copy.deepcopy(value)
    bad["HostConfig"]["NetworkMode"] = "bridge"
    with pytest.raises(net.NamespaceUnknown):
        net.EngineDeclaredIdentity().snapshot(bad, peers)
    bad = copy.deepcopy(value)
    bad["NetworkSettings"]["Networks"] = {"none": {}, "bridge": {}}
    with pytest.raises(net.NamespaceUnknown):
        net.EngineDeclaredIdentity().snapshot(bad, peers)


def test_changed_engine_snapshot_command_and_self_namespace_block():
    p, _value, _peers, snap, o = weak_observations()
    with pytest.raises(net.NamespaceUnknown, match="engine_declared_snapshot_changed"):
        weak_verify(snap, o, p, after=snap | {"started_at": "later"})
    _p, _value, _peers, snap, o = weak_observations()
    o["before"]["command_sha256"] = o["after"]["command_sha256"] = "f" * 64
    with pytest.raises(net.NamespaceUnknown, match="namespace_identity_mismatch"):
        weak_verify(snap, o, p)
    _p, _value, _peers, snap, o = weak_observations()
    o["before"]["self_namespaces"] = observations()[1]["host_namespaces"]
    o["after"] = copy.deepcopy(o["before"])
    with pytest.raises(net.NamespaceUnknown, match="observer_namespace_mismatch"):
        weak_verify(snap, o, p)
    _p, _value, _peers, snap, o = weak_observations()
    o["after"]["start_ticks"] += 1
    with pytest.raises(net.NamespaceUnknown, match="namespace_identity_changed"):
        weak_verify(snap, o, p)


# ---------------------------------------------------------------- engine selection


def weak_engine_run(p, value, o, calls):
    template_ids = [CID]

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["container", "ls"]:
            return (CID + "\n").encode()
        if args[:2] == ["container", "inspect"]:
            assert args[2] == "--format" and "HostConfig" in args[3]
            return b"".join(
                json.dumps(dict(id=i, net="none", pid="", ipc="none")).encode() + b"\n"
                for i in template_ids
            )
        assert args[:3] == ["exec", CID, "/usr/local/bin/python"] and args[-2:] == [
            p.nonce,
            p.digest,
        ]
        return json.dumps(o).encode()

    return run


def test_weak_engine_uses_engine_declared_identity_and_same_exec(monkeypatch):
    p, value, _peers, _snap, o = weak_observations()
    calls = []
    engine = LocalDockerEngine(weak_engine_run(p, value, o, calls), attestation_class=WEAK)
    assert isinstance(engine.identity, net.EngineDeclaredIdentity)
    monkeypatch.setattr(engine, "inspect", lambda *a: copy.deepcopy(value))
    monkeypatch.setattr(engine, "verify_security", lambda v: None)
    result = engine.verify_network(p, IMAGE_ID, CID)
    assert result["state"] == "workload_observed" and result["attestation_class"] == WEAK
    assert sum(a[0] == "exec" for a in calls) == 1
    assert not any(a[0] in {"run", "create", "start", "cp", "pause"} for a in calls)


def test_default_engine_is_strong_native_reader():
    engine = LocalDockerEngine(lambda *a, **k: b"")
    assert engine.attestation_class == STRONG
    assert isinstance(engine.identity, net.NativeNamespaceIdentity)
    with pytest.raises(BackendBlocked):
        LocalDockerEngine(None, attestation_class="weak")


@pytest.mark.parametrize("engine_class,p", [(STRONG, weak_plan()), (WEAK, plan())])
def test_engine_refuses_plan_of_other_class_with_no_docker_call(engine_class, p):
    calls = []
    engine = LocalDockerEngine(lambda *a, **k: calls.append(a), attestation_class=engine_class)
    with pytest.raises(BackendBlocked, match="attestation class mismatch"):
        engine.verify_network(p, IMAGE_ID, CID)
    assert calls == []


def test_strong_preflight_failure_never_falls_back_to_weak(monkeypatch):
    def forbidden(self, *args):
        raise AssertionError("engine-declared identity must never be consulted")

    monkeypatch.setattr(net.EngineDeclaredIdentity, "preflight", forbidden)
    monkeypatch.setattr(net.EngineDeclaredIdentity, "snapshot", forbidden)

    def unavailable(pid):
        raise FileNotFoundError("not a Linux daemon host")

    monkeypatch.setattr(net, "process_identity", unavailable)
    engine = LocalDockerEngine(lambda *a, **k: b"")
    with pytest.raises(net.NamespaceUnknown, match="daemon_host_namespace_observer_unavailable"):
        engine.namespace_preflight()


# ---------------------------------------------------------------- supervision


class ClassEngine(FakeEngine):
    """FakeEngine whose network proof follows the plan's class; records re-checks."""

    def __init__(self, p, fault=None, *, proof=None):
        super().__init__(p, fault)
        self.proof = proof

    def verify_network(self, *args):
        self.calls.append("verify_network")
        if self.fault == "network":
            raise net.NamespaceUnknown("namespace_observation_unavailable_or_malformed")
        if self.proof is not None:
            return self.proof
        if self.plan.attestation_class == WEAK:
            return dict(state="workload_observed", **self.plan.attestation)
        return dict(state="verified", source="fake-test-only")

    def verify_security(self, value):
        self.calls.append("verify_security")
        if self.fault == "security":
            raise BackendBlocked("security settings unknown")


OBSERVER_CALLS = {"namespace_preflight", "verify_network", "verify_security"}


def run_class(tmp_path, cls, *, mode="success", fault=None, control=None, preclaimed=False, **kw):
    p = (weak_plan if cls == WEAK else plan)(mode=mode)
    engine = ClassEngine(p, fault, **kw)
    journal = FixtureJournal(tmp_path / "journal")
    clock = [0.0]

    def sleep(value):
        clock[0] += value

    try:
        if preclaimed:
            journal.claim(p)
        result = supervise(
            p,
            journal,
            engine,
            control or (lambda n: "heartbeat"),
            clock=lambda: clock[0],
            sleep=sleep,
            preclaimed=preclaimed,
        )
        return result, engine, p
    finally:
        journal.close()


def destructive(engine):
    return [c for c in engine.calls if c not in OBSERVER_CALLS]


SCENARIOS = {
    "success": dict(),
    "failure": dict(mode="failure"),
    "cancel": dict(mode="wait", control=lambda n: "cancel"),
    "deadline": dict(mode="wait", control=lambda n: "heartbeat"),
    "broker-loss": dict(mode="wait", control=lambda n: "lost"),
    "heartbeat-loss": dict(mode="wait", control=lambda n: None),
    "guardian-preclaimed": dict(preclaimed=True),
    "kill-unknown": dict(fault="kill"),
    "cleanup-unknown": dict(fault="cleanup"),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_supervision_parity_between_classes(tmp_path, scenario):
    out = {}
    for cls in (STRONG, WEAK):
        (tmp_path / cls).mkdir()
        out[cls] = run_class(tmp_path / cls, cls, **SCENARIOS[scenario])
    (strong, s_engine, _), (weak, w_engine, _) = out[STRONG], out[WEAK]
    assert destructive(s_engine) == destructive(w_engine)
    for key in ("state", "termination_confirmed", "cleanup_confirmed", "activation"):
        assert strong[key] == weak[key]
    assert strong["activation"] == "blocked" and weak["automatic_retry"] is False
    assert list(strong["records"]) == list(weak["records"])
    assert strong["attestation_class"] == STRONG and weak["attestation_class"] == WEAK
    for report, cls in ((strong, STRONG), (weak, WEAK)):
        for key in ("network_preflight", "network"):
            if key in report["records"]:
                obs = report["records"][key]["observation"]
                assert obs["attestation_class"] == cls
                assert obs["observer_independence"] == report["observer_independence"]


def test_weak_success_records_class_and_never_verified(tmp_path):
    report, engine, _ = run_class(tmp_path, WEAK)
    assert report["state"] == "completed"
    network = report["records"]["network"]["observation"]
    assert network["state"] == "workload_observed" and network["attestation_class"] == WEAK
    assert network["observer_independence"] == "none_engine_and_image_trusted"
    assert report["records"]["network_preflight"]["observation"]["attestation_class"] == WEAK
    assert "verified" not in json.dumps(report["records"]["network"]["observation"])
    assert engine.calls.count("verify_security") == 1  # paused re-check before custody


def test_strong_success_does_not_run_paused_recheck(tmp_path):
    report, engine, _ = run_class(tmp_path, STRONG)
    assert report["state"] == "completed" and "verify_security" not in engine.calls
    assert report["records"]["network"]["observation"]["state"] == "verified"
    assert report["records"]["network"]["observation"]["attestation_class"] == STRONG


def test_weak_paused_recheck_failure_is_unknown_before_custody(tmp_path):
    report, engine, _ = run_class(tmp_path, WEAK, fault="security")
    assert report["state"] == "unknown"
    assert "custody" not in report["records"] and "cp" not in engine.calls
    assert report["termination_confirmed"] and report["cleanup_confirmed"]


@pytest.mark.parametrize(
    "cls,proof",
    [
        (WEAK, dict(state="verified", source="forged")),
        (WEAK, dict(state="workload_observed", **plan().attestation)),
        (WEAK, dict(state="workload_observed")),
        (STRONG, dict(state="workload_observed", **weak_plan().attestation)),
        (STRONG, dict(state="verified", **weak_plan().attestation)),
    ],
)
def test_proof_of_the_wrong_class_never_releases(tmp_path, cls, proof):
    report, engine, _ = run_class(tmp_path, cls, proof=proof)
    assert report["state"] == "blocked"
    assert report["records"]["network"]["observation"]["state"] == "unknown"
    assert not {"go", "cp", "finish"}.intersection(engine.calls)
    assert report["termination_confirmed"] and report["cleanup_confirmed"]


@pytest.mark.parametrize("fault", ["namespace_preflight", "network"])
def test_weak_gates_block_like_strong(tmp_path, fault):
    for cls in (STRONG, WEAK):
        (tmp_path / cls).mkdir()
        report, engine, _ = run_class(tmp_path / cls, cls, fault=fault)
        assert report["state"] == "blocked"
        assert report["records"]["network"]["observation"]["attestation_class"] == cls
        assert not {"go", "cp", "finish"}.intersection(engine.calls)
        assert engine.calls.count("create") == (0 if fault == "namespace_preflight" else 1)


def test_recover_is_class_agnostic(tmp_path):
    result = {}
    for cls in (STRONG, WEAK):
        (tmp_path / cls).mkdir()
        _report, _engine, _ = run_class(tmp_path / cls, cls, fault="kill")
        p = (weak_plan if cls == WEAK else plan)()
        fresh = ClassEngine(p)
        recovered = recover(
            tmp_path / cls / "journal", LocalFixtureConfig(enabled=True), engine=fresh
        )
        result[cls] = (destructive(fresh), recovered["cleanup_confirmed"], recovered["state"])
        assert recovered["attestation_class"] == cls
    assert result[STRONG] == result[WEAK]


# ---------------------------------------------------------------- journal


def claimed(tmp_path, p):
    journal = FixtureJournal(tmp_path / "journal")
    journal.claim(p)
    return journal


@pytest.mark.parametrize("key", ["network_preflight", "network"])
@pytest.mark.parametrize(
    "p,tampered",
    [
        (weak_plan(), dict(attestation_class=STRONG, observer_independence="x")),
        (weak_plan(), dict(attestation_class=STRONG)),
        (weak_plan(), {}),  # a weak plan never has the legacy no-class excuse
        (plan(), dict(attestation_class=WEAK)),
        (plan(), dict(observer_independence="none_engine_and_image_trusted")),
    ],
)
def test_report_rejects_class_event_that_differs_from_plan(tmp_path, key, p, tampered):
    journal = claimed(tmp_path, p)
    try:
        journal.event(key, dict(state="available") | tampered)
        with pytest.raises(JournalConflict, match="attestation class"):
            journal.report()
    finally:
        journal.close()


def test_legacy_strong_record_without_class_still_reads(tmp_path):
    journal = claimed(tmp_path, plan())
    try:
        journal.event("network_preflight", dict(state="available", source="legacy"))
        report = journal.report()
        assert report["attestation_class"] == STRONG
    finally:
        journal.close()


def test_weak_journal_cannot_be_moved_into_strong_class(tmp_path):
    weak = weak_plan()
    forged = json.loads(weak.model_dump_json()) | {"profile": "local-networkless-fixture-v1"}
    assert FixturePlan.model_validate(forged).digest != weak.digest
    # Intent row relabelled to the strong profile but keeping the weak digest.
    journal = FixtureJournal(tmp_path / "journal")
    try:
        with journal.connect() as db:
            db.execute(
                "INSERT INTO local_fixture_intent VALUES (1,?,?)",
                (json.dumps(forged), weak.digest),
            )
        with pytest.raises(JournalConflict, match="intent mismatch"):
            journal.plan()
        with pytest.raises(JournalConflict):
            journal.report()
    finally:
        journal.close()


# ---------------------------------------------------------------- CLI


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location(
        "check_local_docker_fixture_attestation",
        Path(__file__).parents[1] / "scripts/check_local_docker_fixture.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def forbid_effects(cli, monkeypatch):
    monkeypatch.setattr(cli, "run_case", lambda *a, **k: pytest.fail("no effects expected"))
    monkeypatch.setattr(cli, "recover", lambda *a, **k: pytest.fail("no effects expected"))


def test_cli_default_output_unchanged_and_zero_effects(cli, monkeypatch, capsys):
    forbid_effects(cli, monkeypatch)
    assert cli.main([]) == 0
    assert json.loads(capsys.readouterr().out) == dict(
        state="disabled", effects=0, activation="blocked"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--attestation-class", WEAK],
        ["--attestation-class", STRONG],
        ["--run", "--recover", "/tmp/x", "--attestation-class", WEAK],
    ],
)
def test_cli_class_flag_valid_only_with_run_and_no_recover(cli, monkeypatch, capsys, argv):
    forbid_effects(cli, monkeypatch)
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    assert stop.value.code == 2 and capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "value", ["weak", "Engine_Declared_Workload_Observed", "engine", "", "auto"]
)
def test_cli_class_value_is_exact_no_aliases(cli, monkeypatch, value):
    forbid_effects(cli, monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["--run", "--attestation-class", value])


def fake_reports(cls):
    base = dict(termination_confirmed=True, cleanup_confirmed=True, attestation_class=cls)
    states = dict(success="completed", failure="failed", cancel="cancelled", timeout="unknown") | {
        "broker-loss": "unknown"
    }
    return {case: base | dict(state=state, records={}) for case, state in states.items()}


def run_cli(cli, monkeypatch, capsys, tmp_path, argv, *, report_class):
    seen = []
    reports = fake_reports(report_class)

    def run_case(root, image, case, attestation_class=STRONG):
        seen.append(attestation_class)
        return reports[case]

    monkeypatch.setattr(cli, "run_case", run_case)
    image = "python@sha256:" + "a" * 64
    code = cli.main(["--run", "--image", image, "--evidence-dir", str(tmp_path / "ev"), *argv])
    return code, json.loads(capsys.readouterr().out), seen


def test_cli_default_runs_strong_and_reports_plain_passed(cli, monkeypatch, capsys, tmp_path):
    code, out, seen = run_cli(cli, monkeypatch, capsys, tmp_path, [], report_class=STRONG)
    assert code == 0 and out["state"] == "passed" and out["attestation_class"] == STRONG
    assert set(seen) == {STRONG}


def test_cli_weak_is_never_plain_passed(cli, monkeypatch, capsys, tmp_path):
    argv = ["--attestation-class", WEAK]
    code, out, seen = run_cli(cli, monkeypatch, capsys, tmp_path, argv, report_class=WEAK)
    assert code == 0 and out["state"] == "passed_weak_attestation" != "passed"
    assert out["attestation_class"] == WEAK
    assert out["observer_independence"] == "none_engine_and_image_trusted"
    assert set(seen) == {WEAK}


@pytest.mark.parametrize("requested,recorded", [(WEAK, STRONG), (STRONG, WEAK), (WEAK, None)])
def test_cli_requested_recorded_mismatch_is_blocked(
    cli, monkeypatch, capsys, tmp_path, requested, recorded
):
    argv = ["--attestation-class", requested]
    code, out, _ = run_cli(cli, monkeypatch, capsys, tmp_path, argv, report_class=recorded)
    assert code == 1 and out["state"] == "blocked"


def test_cli_run_case_binds_class_into_plan_and_config(cli, monkeypatch, tmp_path):
    captured = {}

    class Stop(Exception):
        pass

    class Adapter:
        def __init__(self, config):
            captured["config"] = config

        def launch(self, p, root):
            captured["plan"] = p
            raise Stop

    monkeypatch.setattr(cli, "LocalFixtureAdapter", Adapter)
    image = "python@sha256:" + "a" * 64
    with pytest.raises(Stop):
        cli.run_case(tmp_path / "x", image, "success", WEAK)
    assert captured["plan"].profile == WEAK_PROFILE
    assert captured["config"].attestation_class == WEAK
    with pytest.raises(Stop):
        cli.run_case(tmp_path / "y", image, "success")
    assert captured["plan"].profile == "local-networkless-fixture-v1"
    assert captured["config"].attestation_class == STRONG


@pytest.mark.parametrize(
    "argv",
    [
        ["--cases", "cancel"],
        ["--run", "--recover", "/tmp/x", "--cases", "cancel"],
        ["--run", "--cases", "bogus"],
        ["--run", "--cases", "cancel,cancel"],
        ["--run", "--cases", ""],
        ["--run", "--cases", "Cancel"],
    ],
)
def test_cli_cases_flag_is_strict_and_run_only(cli, monkeypatch, capsys, argv):
    forbid_effects(cli, monkeypatch)
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    assert stop.value.code == 2 and capsys.readouterr().out == ""


def test_cli_cases_subset_runs_only_selected_and_is_never_a_full_pass(
    cli, monkeypatch, capsys, tmp_path
):
    argv = ["--attestation-class", WEAK, "--cases", "cancel,broker-loss"]
    code, out, seen = run_cli(cli, monkeypatch, capsys, tmp_path, argv, report_class=WEAK)
    assert code == 0 and len(seen) == 2
    assert set(out["cases"]) == {"cancel", "broker-loss"}
    assert out["cases_selected"] == ["cancel", "broker-loss"]
    assert out["state"] == "partial_passed_weak_attestation"
    assert out["attestation_class"] == WEAK


def test_cli_strong_subset_is_partial_not_passed(cli, monkeypatch, capsys, tmp_path):
    code, out, _ = run_cli(
        cli, monkeypatch, capsys, tmp_path, ["--cases", "cancel"], report_class=STRONG
    )
    assert code == 0 and out["state"] == "partial_passed"


def test_cli_subset_failure_blocks(cli, monkeypatch, capsys, tmp_path):
    seen = []

    def run_case(root, image, case, attestation_class=STRONG):
        seen.append(case)
        return dict(
            state="unknown",
            termination_confirmed=True,
            cleanup_confirmed=True,
            attestation_class=attestation_class,
            records={},
        )

    monkeypatch.setattr(cli, "run_case", run_case)
    image = "python@sha256:" + "a" * 64
    argv = ["--run", "--image", image, "--evidence-dir", str(tmp_path / "ev"), "--cases", "cancel"]
    code = cli.main(argv)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["state"] == "blocked" and seen == ["cancel"]
