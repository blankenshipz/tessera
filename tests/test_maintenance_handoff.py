"""Real local export custody and durable claims; scripted API, no host proof."""

import copy

import pytest
from maintenance_custody_fixture import database, export_phase

from tessera_operator import maintenance_handoff as h
from tessera_operator.schema_supervised import KubernetesSupervisedProfile as Profile


def fixture(tmp_path, monkeypatch, *, running=False, lose=False):
    pre = tmp_path / "pre.sqlite3"
    pre_db = database(pre, 56, "synthetic-source")
    post = tmp_path / "post.sqlite3"
    post_db = database(post, 58, "synthetic-source")
    plan = {
        "operation": "handoff-89",
        "deadline": 200,
        "target": {"pvc_uid": "pvc"},
        "old_image": "test@sha256:" + "a" * 64,
        "new_image": "test@sha256:" + "b" * 64,
        "db_incarnation": "synthetic-source",
        "db_sha256": pre_db["sha256"],
    }
    exports = {}
    receipts = {}
    for phase, path, input_sha in (
        ("backup", pre, pre_db["sha256"]),
        ("validate", post, post_db["sha256"]),
    ):
        remote, exports[phase] = export_phase(tmp_path / phase, plan, phase, path, input_sha)
        receipts[phase] = {"remote": remote}
    receipts["migrate"] = {
        "remote": dict(
            operation=plan["operation"],
            target=plan["target"],
            input_db_sha256=pre_db["sha256"],
            db=post_db,
        )
    }
    plan["phase_receipts"] = receipts
    pod = Profile.pod(plan)
    pod["metadata"].update(uid="owned", resourceVersion="123")
    statuses = []
    for name in ("old", "new"):
        runtime = "containerd://" + name
        state = (
            {"running": {}}
            if running
            else {
                "terminated": {
                    "containerID": runtime,
                    "finishedAt": "2026-10-24T00:00:00Z",
                    "exitCode": 0,
                }
            }
        )
        statuses.append(
            {
                "name": name,
                "containerID": runtime,
                "imageID": name + "-image",
                "restartCount": 0,
                "state": state,
            }
        )
    pod["status"] = {"containerStatuses": statuses}
    pins = {
        "uid": "owned",
        "spec_sha256": h.digest(pod["spec"]),
        "containers": {
            s["name"]: {k: s[k] for k in ("containerID", "imageID", "restartCount")}
            for s in statuses
        },
    }

    class Transport:
        current = pod
        deletes = []

        def get(self, name):
            assert name == "handoff-89"
            return {"items": [] if self.current is None else [copy.deepcopy(self.current)]}

        def delete(self, name, body):
            self.deletes.append((name, body))
            if lose:
                raise TimeoutError("scripted lost ack")
            return 202

    transport = Transport()
    handoff = h.MaintenanceHandoff(tmp_path / "handoff", transport=transport, clock=lambda: 100)
    handoff.fixture_exports = exports
    return handoff, plan, pins, transport


def test_exact_delete_claim_terminal_reconnect_and_no_replay(tmp_path, monkeypatch):
    handoff, plan, pins, transport = fixture(tmp_path, monkeypatch)
    result = handoff.apply(plan, pins, handoff.fixture_exports)
    name, body = transport.deletes[0]
    assert body["preconditions"] == {"uid": "owned", "resourceVersion": "123"}
    assert body["gracePeriodSeconds"] == 30
    assert name == plan["operation"] and result["admission"] == "held"
    assert result["host_fence"].startswith("unsupported")
    transport.current = None
    reopened = h.MaintenanceHandoff(handoff.directory, transport=transport, clock=lambda: 100)
    assert reopened.reconcile()["deletion_acknowledged"] is True
    with pytest.raises(h.SupervisedDenied):
        handoff.apply(plan, pins, handoff.fixture_exports)
    assert len(transport.deletes) == 1


def test_terminating_running_and_disappearance_never_prove_terminal(tmp_path, monkeypatch):
    handoff, plan, pins, transport = fixture(tmp_path, monkeypatch, running=True)
    transport.current["metadata"]["deletionTimestamp"] = "2026-10-24T00:00:00Z"
    with pytest.raises(h.SupervisedDenied, match="terminal runtime"):
        handoff.apply(plan, pins, handoff.fixture_exports)
    transport.current = None
    with pytest.raises(h.SupervisedDenied, match="terminal runtime"):
        handoff.reconcile()
    assert len(transport.deletes) == 1


def test_lost_ack_reconcile_keeps_uncertainty_and_replacement_denies(tmp_path, monkeypatch):
    handoff, plan, pins, transport = fixture(tmp_path, monkeypatch, lose=True)
    with pytest.raises(h.SupervisedDenied, match="uncertain"):
        handoff.apply(plan, pins, handoff.fixture_exports)
    assert (handoff.directory / "failure.json").exists()
    assert handoff.reconcile()["deletion_acknowledged"] is False
    with pytest.raises(FileExistsError):
        handoff.apply(plan, pins, handoff.fixture_exports)
    assert len(transport.deletes) == 1
    transport.current["metadata"]["uid"] = "replacement"
    with pytest.raises(h.SupervisedDenied, match="identity"):
        handoff.reconcile()


def test_missing_real_custody_prevents_claim_and_delete(tmp_path, monkeypatch):
    handoff, plan, pins, transport = fixture(tmp_path, monkeypatch)
    monkeypatch.undo()
    with pytest.raises(h.SupervisedDenied, match="durable exports"):
        handoff.apply(plan, pins, {})
    assert transport.deletes == []
    assert not handoff.directory.exists()


@pytest.mark.xfail(
    strict=True,
    reason="Release now requires native MaintenanceCompletion confirmation "
    "(maintenance_hold.confirm_completion); this handoff flow does not wire one yet.",
)
def test_activate_requires_handoff_even_direct_compile(tmp_path, monkeypatch):
    handoff, plan, pins, transport = fixture(tmp_path, monkeypatch)
    evidence = {
        "maintenance_pod_uid": "owned",
        "maintenance_spec_sha256": pins["spec_sha256"],
        "db": plan["phase_receipts"]["validate"]["remote"]["db"],
    }
    with pytest.raises(h.SupervisedDenied, match="handoff required"):
        Profile().compile(plan, "activate", {"evidence": evidence}, None)
    handoff.apply(plan, pins, handoff.fixture_exports)
    profile = Profile(handoff=handoff)
    from maintenance_hold_fixture import fixture_hold

    profile.hold, _, _, enforcement = fixture_hold(tmp_path / "quota", plan, handoff=handoff)
    profile.hold.prepare("hold", plan)
    profile.hold.apply("hold", plan)
    enforcement()
    profile.hold.prepare("release", plan)
    profile.hold.apply("release", plan)
    profile.check_handoff(plan, evidence)
    with pytest.raises(h.SupervisedDenied, match="binding"):
        profile.check_handoff(plan, evidence | {"maintenance_pod_uid": "replacement"})
    with pytest.raises(h.SupervisedDenied, match="binding"):
        profile.check_handoff(plan, evidence | {"db": {"schema": 56}})


def test_running_then_exact_runtime_terminated_readback(tmp_path, monkeypatch):
    handoff, plan, pins, transport = fixture(tmp_path, monkeypatch, running=True)
    with pytest.raises(h.SupervisedDenied, match="terminal runtime"):
        handoff.apply(plan, pins, handoff.fixture_exports)
    for status in transport.current["status"]["containerStatuses"]:
        status["state"] = {
            "terminated": {
                "containerID": status["containerID"],
                "finishedAt": "2026-10-24T00:00:01Z",
                "exitCode": 0,
            }
        }
    assert handoff.reconcile()["schema"] == 58
    assert len(transport.deletes) == 1


def test_api_denial_preserves_claim_without_retry(tmp_path, monkeypatch):
    handoff, plan, pins, transport = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(transport, "delete", lambda name, body: 403)
    with pytest.raises(h.SupervisedDenied, match="denied/uncertain"):
        handoff.apply(plan, pins, handoff.fixture_exports)
    assert (handoff.directory / "claim.json").exists()
    assert (handoff.directory / "failure.json").exists()
    assert not handoff.reconcile()["deletion_acknowledged"]


@pytest.mark.parametrize("fault", ["bytes", "phase", "lineage", "replacement", "unknown_ack"])
def test_joined_custody_or_runtime_fault_denies_activation(tmp_path, monkeypatch, fault):
    from pathlib import Path

    handoff, plan, pins, transport = fixture(tmp_path, monkeypatch, lose=fault == "unknown_ack")
    if fault == "unknown_ack":
        with pytest.raises(h.SupervisedDenied, match="uncertain"):
            handoff.apply(plan, pins, handoff.fixture_exports)
    else:
        handoff.apply(plan, pins, handoff.fixture_exports)
    if fault == "bytes":
        root = Path(handoff.fixture_exports["validate"]["destination"])
        (root / "export/post/snapshot.sqlite3").write_bytes(b"corrupted retained destination")
    elif fault == "phase":
        handoff.fixture_exports["validate"] = handoff.fixture_exports["backup"]
        # Test new admission with a swapped real journal, without editing durable claims.
        fresh = h.MaintenanceHandoff(tmp_path / "fresh", transport=transport, clock=lambda: 100)
        with pytest.raises(h.SupervisedDenied, match="target/phase"):
            fresh.apply(plan, pins, handoff.fixture_exports)
        assert not fresh.directory.exists()
        return
    elif fault == "lineage":
        plan["phase_receipts"]["migrate"]["remote"]["input_db_sha256"] = "0" * 64
        fresh = h.MaintenanceHandoff(tmp_path / "fresh", transport=transport, clock=lambda: 100)
        with pytest.raises(h.SupervisedDenied, match="migration lineage"):
            fresh.apply(plan, pins, handoff.fixture_exports)
        assert not fresh.directory.exists()
        return
    elif fault == "replacement":
        transport.current["metadata"]["uid"] = "replacement"
    evidence = dict(
        maintenance_pod_uid=pins["uid"],
        maintenance_spec_sha256=pins["spec_sha256"],
        db=plan["phase_receipts"]["validate"]["remote"]["db"],
    )
    with pytest.raises(h.SupervisedDenied):
        Profile(handoff=handoff).check_handoff(plan, evidence)
    assert len(transport.deletes) == 1
