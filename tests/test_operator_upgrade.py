"""Synthetic-only upgrade admission. No real Docker/Kubernetes/backup actions."""

import asyncio
import copy
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tessera_operator import build_receipt, upgrade
from tessera_operator import upgrade_support as activation
from tessera_operator import upgrade_support as private_import
from tessera_operator import upgrade_support as target

OLD = "docker.io/library/tessera-local-persistent@sha256:" + "a" * 64
NEW = "docker.io/library/tessera-local-persistent@sha256:" + "b" * 64
OP = "1" * 32


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    monkeypatch.setattr(upgrade, "ROOT", root / "upgrades")
    upgrade.ROOT.mkdir(mode=0o700)
    (upgrade.ROOT / "candidates").mkdir(mode=0o700)
    monkeypatch.setattr(activation, "INTENT", root / "activation.json")
    base = {
        "namespace_uid": "ns",
        "deployment_uid": "deploy",
        "pvc_uid": "pvc",
        "pv_uid": "pv",
        "image": OLD,
    }
    activation._write_once(activation.INTENT, base)
    deployment = {
        "metadata": {"uid": "deploy", "resourceVersion": "rv-1"},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "template": {
                "spec": {
                    "containers": [{"name": "control", "image": OLD}],
                    "volumes": [
                        {
                            "name": "state",
                            "persistentVolumeClaim": {"claimName": "tessera-control-state"},
                        }
                    ],
                }
            },
        },
    }
    backend = {
        "schema": 56,
        "incarnation": "inc",
        "recovery_state": "active",
        "store_sha256": "c" * 64,
        "application_http": 200,
    }
    inventory = {
        "git_commit": "d" * 40,
        "inventory_sha256": "e" * 64,
        "overlays": {"src/tessera_control/store.py": "c" * 64},
    }
    activation._write_once(
        upgrade.ROOT / "candidates" / ("b" * 64 + ".json"),
        {
            "image": NEW,
            "git_commit": inventory["git_commit"],
            "schema": 56,
            "inventory_sha256": inventory["inventory_sha256"],
        },
    )
    state = {
        "patches": [],
        "backups": [],
        "response": 200,
        "apply_effect": True,
        "new_pod": "new-pod",
        "clock": 1000.0,
        "head": "1" * 64,
    }

    def ready(expected):
        if (
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] != expected["image"]
            or expected["pvc_uid"] != "pvc"
        ):
            raise target.AdmissionError("owned ready image/PVC mismatch")
        return {
            "pod_uid": "old-pod" if expected["image"] == OLD else state["new_pod"],
            "pod_name": "owned-pod",
            "deployment_uid": "deploy",
            "pv_uid": "pv",
        }

    def backup(pod, stage, output):
        state["backups"].append((stage, output))
        output.mkdir(mode=0o700)
        return {
            "pod_uid": pod["pod_uid"],
            "pv_uid": "pv",
            "backup_id": "backup-1",
            "snapshot_sha256": "f" * 64,
            "incarnation": "inc",
            "recovery_state": "active",
        }

    def patch(method, uri, body, *, content_type):
        assert (upgrade.ROOT / "apply-claim.json").exists()
        assert method == "PATCH" and uri.endswith("/deployments/tessera-control")
        assert content_type == "application/json-patch+json"
        state["patches"].append(body)
        if state["apply_effect"]:
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] = NEW
            deployment["metadata"]["resourceVersion"] = "rv-2"
        return state["response"]

    monkeypatch.setattr(activation, "_ready_pod_for", ready)
    monkeypatch.setattr(upgrade, "_deployment", lambda: copy.deepcopy(deployment))
    monkeypatch.setattr(upgrade, "_backend", lambda pod: dict(backend))
    monkeypatch.setattr(upgrade, "candidate_inventory", lambda: copy.deepcopy(inventory))
    monkeypatch.setattr(upgrade, "_capacity", lambda: None)
    monkeypatch.setattr(target, "_docker", lambda *args: OLD + "\n" + NEW)
    monkeypatch.setattr(upgrade.recovery, "_pod_backup", backup)
    monkeypatch.setattr(target, "validate_backup", lambda path: {"snapshot_sha256": "f" * 64})
    monkeypatch.setattr(private_import, "_api_request", patch)
    # Existing upgrade-admission unit tests isolate the separately tested producer.
    monkeypatch.setattr(build_receipt, "verify_receipt", lambda receipt, inventory: None)
    monkeypatch.setattr(upgrade, "_now", lambda: state["clock"], raising=False)
    monkeypatch.setattr(upgrade, "_control_head", lambda pod: state["head"], raising=False)
    monkeypatch.setattr(upgrade, "_snapshot_head", lambda path: "1" * 64, raising=False)
    return SimpleNamespace(
        deployment=deployment, backend=backend, inventory=inventory, state=state, base=base
    )


def prepared():
    plan = upgrade.plan(OP, NEW)
    upgrade.prepare(OP, NEW, plan["plan_sha256"])
    return plan


def test_valid_occupied_target_update_readiness_and_reconnect(case):
    saved = prepared()
    assert case.state["backups"][0][0] == "upgrade-" + OP
    assert case.state["backups"][0][1] == upgrade.ROOT / OP / "backup"
    assert not case.state["patches"]
    assert (
        upgrade.apply(OP, saved["plan_sha256"])["state"]
        == "image_patch_submitted_readiness_unproven"
    )
    patch = case.state["patches"][0]
    assert [(p["path"], p["value"]) for p in patch[:-1]] == [
        ("/metadata/uid", "deploy"),
        ("/metadata/resourceVersion", "rv-1"),
        ("/spec/replicas", 1),
        ("/spec/strategy", {"type": "Recreate"}),
        ("/spec/template/spec/containers/0/image", OLD),
    ]
    with pytest.raises(target.AdmissionError, match="image/PVC"):
        activation.ready_pod()  # Supervisor cannot adopt new image before reconciliation.
    assert upgrade.reconcile(OP)["pod_uid"] == "new-pod"
    assert activation.ready_pod()["pod_uid"] == "new-pod"
    assert upgrade.reconcile(OP)["pod_uid"] == "new-pod"  # Reconnect is read/verify only.
    assert len(case.state["patches"]) == 1
    assert private_import._receipt(activation.INTENT) == case.base  # Original receipt immutable.
    with pytest.raises(target.AdmissionError, match="reconcile only"):
        upgrade.apply(OP, saved["plan_sha256"])


@pytest.mark.parametrize("drift", ["uid", "rv", "replicas", "strategy", "pvc"])
def test_stale_target_never_patches(case, drift):
    saved = prepared()
    if drift == "uid":
        case.deployment["metadata"]["uid"] = "foreign"
    if drift == "rv":
        case.deployment["metadata"]["resourceVersion"] = "rv-foreign"
    if drift == "replicas":
        case.deployment["spec"]["replicas"] = 2
    if drift == "strategy":
        case.deployment["spec"]["strategy"] = {"type": "RollingUpdate"}
    if drift == "pvc":
        case.deployment["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"][
            "claimName"
        ] = "foreign"
    with pytest.raises(target.AdmissionError):
        upgrade.apply(OP, saved["plan_sha256"])
    assert not case.state["patches"] and not (upgrade.ROOT / "apply-claim.json").exists()


def test_backup_collision_preserves_first_preparation(case):
    saved = prepared()
    with pytest.raises(target.AdmissionError, match="no overwrite"):
        upgrade.prepare(OP, NEW, saved["plan_sha256"])
    assert len(case.state["backups"]) == 1
    assert (upgrade.ROOT / OP / "backup.json").exists()


def test_interrupted_apply_reconciles_without_resending(case):
    saved = prepared()
    case.state["response"] = 503  # Mutation happened, response was lost/ambiguous.
    with pytest.raises(target.AdmissionError, match="uncertain"):
        upgrade.apply(OP, saved["plan_sha256"])
    with pytest.raises(target.AdmissionError, match="reconcile only"):
        upgrade.apply(OP, saved["plan_sha256"])
    assert upgrade.reconcile(OP)["pod_uid"] == "new-pod"
    assert len(case.state["patches"]) == 1


def test_unknown_unapplied_intent_cannot_relaunch_under_another_id(case):
    saved = prepared()
    case.state.update(response=409, apply_effect=False)
    with pytest.raises(target.AdmissionError):
        upgrade.apply(OP, saved["plan_sha256"])
    with pytest.raises(target.AdmissionError):
        upgrade.reconcile(OP)
    with pytest.raises(target.AdmissionError, match="reconcile only"):
        upgrade.prepare("2" * 32, NEW, saved["plan_sha256"])
    assert len(case.state["patches"]) == 1


def test_capacity_and_overlay_fence_before_backup(case, monkeypatch):
    saved = upgrade.plan(OP, NEW)

    def insufficient():
        raise target.AdmissionError("capacity")

    monkeypatch.setattr(upgrade, "_capacity", insufficient)
    with pytest.raises(target.AdmissionError, match="capacity"):
        upgrade.prepare(OP, NEW, saved["plan_sha256"])
    assert not case.state["backups"]
    case.backend["store_sha256"] = "unreviewed"
    with pytest.raises(target.AdmissionError, match="overlay"):
        upgrade.plan(OP, NEW)


def test_candidate_and_ready_identity_must_not_drift(case):
    saved = prepared()
    case.inventory["inventory_sha256"] = "0" * 64
    with pytest.raises(target.AdmissionError, match="candidate"):
        upgrade.apply(OP, saved["plan_sha256"])
    assert not case.state["patches"]


def test_rollback_never_overwrites_authority_or_patches_old_image(case):
    saved = prepared()
    upgrade.apply(OP, saved["plan_sha256"])
    upgrade.reconcile(OP)
    case.backend["metadata_writes"] = True
    response = upgrade.rollback(OP)
    assert response["state"] == "blocked"
    assert "rollback_unsafe" in response["reason"]
    assert len(case.state["patches"]) == 1
    assert (upgrade.ROOT / OP / "backup.json").exists()


def test_ready_receipt_corruption_never_admits_image(case):
    saved = prepared()
    upgrade.apply(OP, saved["plan_sha256"])
    upgrade.reconcile(OP)
    path = upgrade.ROOT / OP / "plan.json"
    value = json.loads(path.read_text())
    value["new_image"] = OLD
    path.write_text(json.dumps(value))
    with pytest.raises(target.AdmissionError, match="receipt changed"):
        activation.ready_pod()


def test_real_stdio_registers_upgrade_without_bootstrap_or_target_calls():
    async def connect():
        params = StdioServerParameters(
            command=sys.executable, args=["-B", "-m", "tessera_operator.upgrade_server"]
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {
                    "local_control_upgrade_plan",
                    "local_control_upgrade_prepare",
                    "local_control_upgrade_apply",
                    "local_control_upgrade_reconcile",
                    "local_control_upgrade_rollback",
                    "local_control_candidate_inventory",
                    "local_control_build_plan",
                    "local_control_build",
                    "local_control_build_status",
                } == names

    asyncio.run(connect())


def test_rev004_pointer_interruption_then_pod_recreation(case, monkeypatch):
    saved = prepared()
    upgrade.apply(OP, saved["plan_sha256"])
    write_once = activation._write_once

    def interrupt(path, value):
        if path == upgrade.ROOT / "current.json":
            raise OSError("synthetic pointer publication interruption")
        return write_once(path, value)

    monkeypatch.setattr(activation, "_write_once", interrupt)
    with pytest.raises(OSError, match="interruption"):
        upgrade.reconcile(OP)
    prior = {p: p.read_bytes() for p in (upgrade.ROOT / OP).glob("ready*.json")}
    assert prior and not (upgrade.ROOT / "current.json").exists()
    case.state["new_pod"] = "replacement-pod"
    monkeypatch.setattr(activation, "_write_once", write_once)
    assert upgrade.reconcile(OP)["pod_uid"] == "replacement-pod"
    assert all(p.read_bytes() == content for p, content in prior.items())
    assert len(list((upgrade.ROOT / OP).glob("ready*.json"))) == 2
    assert activation.ready_pod()["pod_uid"] == "replacement-pod"
    assert len(case.state["patches"]) == 1


@pytest.mark.parametrize("drift", ["age", "write", "unknown"])
def test_rev005_stale_backup_rejected_before_claim(case, monkeypatch, drift):
    saved = prepared()
    if drift == "age":
        case.state["clock"] += 601
    elif drift == "write":
        case.state["head"] = "2" * 64  # Authority write, same deployment/backend identity.
    else:
        monkeypatch.setattr(upgrade, "_control_head", lambda pod: None, raising=False)
    with pytest.raises(target.AdmissionError, match="backup.*(stale|unverifiable|changed)"):
        upgrade.apply(OP, saved["plan_sha256"])
    assert not (upgrade.ROOT / "apply-claim.json").exists()
    assert not case.state["patches"]


def test_rev005_refresh_is_new_create_only_operation(case, monkeypatch):
    saved = prepared()
    prior = {p: p.read_bytes() for p in (upgrade.ROOT / OP).glob("*.json")}
    case.state["clock"] += 601
    case.state["head"] = "2" * 64
    with pytest.raises(target.AdmissionError, match="backup stale"):
        upgrade.apply(OP, saved["plan_sha256"])
    other = "2" * 32
    monkeypatch.setattr(upgrade, "_snapshot_head", lambda path: case.state["head"])
    fresh = upgrade.plan(other, NEW)
    upgrade.prepare(other, NEW, fresh["plan_sha256"])
    upgrade.apply(other, fresh["plan_sha256"])
    assert len(case.state["backups"]) == 2
    assert all(p.read_bytes() == content for p, content in prior.items())
    assert len(case.state["patches"]) == 1


def test_rev005_preparation_with_intervening_write_retains_unusable_backup(case, monkeypatch):
    saved = upgrade.plan(OP, NEW)
    original = upgrade.recovery._pod_backup

    def backup_with_write(*args):
        result = original(*args)
        case.state["head"] = "2" * 64
        return result

    monkeypatch.setattr(upgrade.recovery, "_pod_backup", backup_with_write)
    with pytest.raises(target.AdmissionError, match="backup control state changed"):
        upgrade.prepare(OP, NEW, saved["plan_sha256"])
    assert (upgrade.ROOT / OP / "backup.json").exists()
    assert not (upgrade.ROOT / OP / "backup-freshness.json").exists()
    with pytest.raises(target.AdmissionError):
        upgrade.apply(OP, saved["plan_sha256"])
    assert not (upgrade.ROOT / "apply-claim.json").exists()


def test_rev005_actual_sqlite_head_covers_non_event_authority_write(tmp_path):
    database = tmp_path / "control.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE events(cursor INTEGER); INSERT INTO events VALUES(1);"
            "CREATE TABLE delegation(revision INTEGER, enabled INTEGER);"
            "INSERT INTO delegation VALUES(1,1);"
        )
        with sqlite3.connect(snapshot) as out:
            db.backup(out)
    first = upgrade._snapshot_head(database)
    assert first == upgrade._snapshot_head(snapshot)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE delegation SET revision=2, enabled=0")
    assert upgrade._snapshot_head(database) != first
    assert upgrade._snapshot_head(snapshot) == first


def test_rev004_published_proof_remains_valid_after_subsequent_pod_recreation(case):
    saved = prepared()
    upgrade.apply(OP, saved["plan_sha256"])
    upgrade.reconcile(OP)
    pointer = (upgrade.ROOT / "current.json").read_bytes()
    case.state["new_pod"] = "later-pod"
    upgrade.reconcile(OP)
    assert (upgrade.ROOT / "current.json").read_bytes() == pointer
    assert activation.ready_pod()["pod_uid"] == "later-pod"
    assert len(case.state["patches"]) == 1
