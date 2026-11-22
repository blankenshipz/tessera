"""Real local records/composition, scripted quota API; no native authority proof."""

import json

import pytest
from maintenance_hold_fixture import fixture_hold

from tessera_operator import maintenance_entrypoint as entry
from tessera_operator.maintenance_producers import save
from tessera_operator.schema_supervised import SupervisedDenied


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(entry.target, "SOURCE", tmp_path)
    service = entry.MaintenanceEntrypoint(clock=lambda: 100)
    root = service.directory("one-operation")
    (root / "inputs").mkdir(parents=True, mode=0o700)
    # All directories traversed by the entrypoint are private, not just the leaf.
    root.chmod(0o700)
    plan = dict(
        operation="one-operation",
        deadline=200,
        target={"pvc_uid": "pvc"},
        db_incarnation="test",
        db_sha256="test",
        old_image="test-old",
        new_image="test-new",
    )
    save(root / "inputs" / "plan.json", plan)
    _, api, _, _ = fixture_hold(root / "producer", plan)
    refs = {}
    for path in (root / "producer").glob("*.json"):
        path.chmod(0o600)
        refs[path.stem] = dict(
            path=str(path),
            sha256=entry.read_json(path)[1]["sha256"],
            owner="synthetic-owner-not-live",
        )
    (root / "producer").chmod(0o700)
    for action in entry.ACTIONS:
        save(root / "inputs" / (action + ".json"), dict(prerequisites=refs))
    original = entry.MaintenanceHold

    def hold(*args, **kwargs):
        return original(*args, **kwargs, transport=api)

    monkeypatch.setattr(entry, "MaintenanceHold", hold)
    service.seal("one-operation")
    return service, root, api


def test_real_composed_hold_and_retained_reconnect_after_expiry(setup):
    service, root, api = setup
    packet = service.prepare("one-operation", "hold")
    assert api.calls == 0
    result = service.effect("one-operation", "hold", packet["action_sha256"])
    assert result["uid"] == "quota-owned" and api.calls == 1
    reopened = entry.MaintenanceEntrypoint(clock=lambda: 201)
    status = reopened.reconcile("one-operation", "hold")
    assert status["state"] == "captured" and status["expired"]
    assert status["native_admission"] == "external_per_effect_required"
    with pytest.raises(SupervisedDenied, match="expired"):
        reopened.effect("one-operation", "hold", packet["action_sha256"])
    assert (root / "hold.claim.json").exists() and api.calls == 1


def test_unknown_ack_claim_prevents_replay_and_records_failure(setup):
    service, root, api = setup
    packet = service.prepare("one-operation", "hold")
    api.lose = True
    with pytest.raises(TimeoutError):
        service.effect("one-operation", "hold", packet["action_sha256"])
    assert service.reconcile("one-operation", "hold")["state"] == "unknown_ack"
    with pytest.raises(FileExistsError):
        service.effect("one-operation", "hold", packet["action_sha256"])
    with pytest.raises(SupervisedDenied, match="claimed"):
        service.prepare("one-operation", "hold")
    assert api.calls == 1 and (root / "hold.failure.json").exists()


@pytest.mark.parametrize("fault", ["digest", "plan", "inputs", "reference", "public", "alias"])
def test_tamper_and_alias_deny_before_dispatch(setup, fault):
    service, root, api = setup
    packet = service.prepare("one-operation", "hold")
    expected = packet["action_sha256"]
    if fault == "digest":
        expected = "changed"
    elif fault == "plan":
        (root / "inputs" / "plan.json").write_text("{}")
    elif fault == "inputs":
        path = root / "inputs" / "hold.json"
        path.write_text(path.read_text() + " ")
    elif fault == "reference":
        (root / "producer" / "exclusive.json").write_text("{}")
    elif fault == "public":
        (root / "sealed.json").chmod(0o644)
    else:
        moved = root / "other-hold"
        (root / "hold").rename(moved)
        (root / "hold").symlink_to(moved, target_is_directory=True)
    with pytest.raises((SupervisedDenied, KeyError)):
        service.effect("one-operation", "hold", expected)
    assert api.calls == 0 and not (root / "hold.claim.json").exists()


def test_path_escape_unknown_operation_action_and_absent_producer(setup):
    service, root, api = setup
    for operation in ("../escape", "/arbitrary"):
        with pytest.raises(SupervisedDenied):
            service.prepare(operation, "hold")
    with pytest.raises(FileNotFoundError):
        service.prepare("unknown", "hold")
    assert not service.directory("unknown").exists()
    with pytest.raises(SupervisedDenied, match="unknown maintenance"):
        service.prepare("one-operation", "arbitrary-url")
    path = root / "inputs" / "hold.json"
    path.write_text(json.dumps(dict(prerequisites={})))
    with pytest.raises(KeyError):
        service.prepare("one-operation", "hold")
    path.write_text(json.dumps(dict(prerequisites={}, approval=True)))
    with pytest.raises(SupervisedDenied, match="unsupported owner"):
        service.prepare("one-operation", "hold")
    assert api.calls == 0


def test_outside_evidence_reference_rejected(setup):
    service, root, api = setup
    outside = root.parent / "outside.json"
    save(outside, {})
    (root / "inputs" / "hold.json").write_text(
        json.dumps(
            dict(
                prerequisites={
                    "exclusive": dict(
                        path=str(outside), sha256=entry.read_json(outside)[1]["sha256"], owner="x"
                    )
                }
            )
        )
    )
    with pytest.raises(SupervisedDenied, match="escape"):
        service.prepare("one-operation", "hold")
    assert api.calls == 0


def test_exact_single_phase_routing_and_forward_only_retained_status(setup, monkeypatch):
    service, root, api = setup
    # Scripted profile preparation only: this tests routing, not native fencing.
    calls = []

    def prepare(op, stage):
        calls.append(("prepare", stage))
        packet = {"scripted": stage}
        save(op.directory / (stage + ".prepared.json"), packet)
        return packet

    def apply(owner, stage):
        assert type(owner.operation) is entry.SupervisedOperation
        assert owner.operation.profile.hold is owner.hold
        assert owner.operation.profile.handoff is owner.handoff
        assert type(owner.enforcement.api) is entry.FixedQuotaDryRunTransport
        calls.append(("apply", stage))
        return {"synthetic": stage}

    monkeypatch.setattr(entry.SupervisedOperation, "prepare", prepare)
    monkeypatch.setattr(entry.OwnerMaintenance, "apply", apply)
    packet = service.prepare("one-operation", "scale_zero")
    service.effect("one-operation", "scale_zero", packet["action_sha256"])
    assert calls == [("prepare", "scale_zero"), ("apply", "scale_zero")]
    save(root / "phases" / "migrate.claim.json", {})
    assert (
        service.reconcile("one-operation", "scale_zero")["recovery"] == "compatible58 forward-only"
    )
    assert api.calls == 0


def test_release_claim_and_expiry_deny_before_further_work(setup):
    service, root, api = setup
    packet = service.prepare("one-operation", "hold")
    service.effect("one-operation", "hold", packet["action_sha256"])
    save(root / "hold" / "release.claim.json", {"unknown_ack": True})
    with pytest.raises(SupervisedDenied, match="release claimed"):
        service.prepare("one-operation", "custody")
    expired = entry.MaintenanceEntrypoint(clock=lambda: 201)
    with pytest.raises(SupervisedDenied, match="expired"):
        expired.prepare("one-operation", "enforcement")
    assert api.calls == 1
    assert not (root / "custody.prepared.json").exists()
    assert not (root / "enforcement.claim.json").exists()
