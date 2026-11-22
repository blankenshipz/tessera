"""Durable local evidence/claims with scripted API, not quota enforcement proof."""

import copy
import os
from pathlib import Path

import pytest
from maintenance_hold_fixture import fixture_hold
from test_maintenance_handoff import fixture as handoff_fixture

from tessera_operator.schema_supervised import KubernetesSupervisedProfile, SupervisedDenied


@pytest.fixture(autouse=True)
def _private_umask():
    # Operator destinations must be owner-private; do not depend on the caller's umask.
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def setup(tmp_path, monkeypatch):
    handoff, plan, pins, _ = handoff_fixture(tmp_path / "handoff-input", monkeypatch)
    hold, api, record, enforcement = fixture_hold(tmp_path / "quota", plan, handoff=handoff)
    return hold, api, record, enforcement, handoff, plan, pins


def capture(hold, plan, enforcement):
    hold.prepare("hold", plan)
    hold.apply("hold", plan)
    enforcement()


def test_capture_release_and_reconnect_never_exec_again(tmp_path, monkeypatch):
    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    with hold.db_window(plan):
        pass
    handoff.apply(plan, pins, handoff.fixture_exports)
    hold.prepare("release", plan)
    hold.apply("release", plan)
    observed = hold.reconcile("release")
    assert observed["state"] == "observed_unconfirmed"
    assert observed["ack_observed"] and not observed["completion_confirmed"]
    with pytest.raises(SupervisedDenied, match="native completion unavailable"):
        hold.released(plan)
    assert api.calls == 2
    reopened = type(hold)(
        hold.directory,
        prerequisites=hold.prerequisites,
        transport=api,
        handoff=handoff,
        clock=lambda: 100,
    )
    assert reopened.reconcile("release")["state"] == "observed_unconfirmed"
    with pytest.raises(SupervisedDenied, match="native completion unavailable"):
        reopened.released(plan)
    with pytest.raises(SupervisedDenied, match="release claimed"):
        with reopened.db_window(plan):
            pytest.fail("postrelease exec")
    profile = KubernetesSupervisedProfile(hold=reopened, clock=lambda: 100)
    with pytest.raises(SupervisedDenied, match="release claimed"):
        profile.readback(dict(operation=plan["operation"], target=plan["target"], phase="backup"))
    with pytest.raises(SupervisedDenied, match="claimed"):
        hold.apply("release", plan)


@pytest.mark.parametrize("phase", ["hold", "release"])
def test_lost_ack_is_unknown_never_replayed(tmp_path, monkeypatch, phase):
    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    if phase == "release":
        capture(hold, plan, enforcement)
        handoff.apply(plan, pins, handoff.fixture_exports)
    hold.prepare(phase, plan)
    api.lose = True
    with pytest.raises(TimeoutError):
        hold.apply(phase, plan)
    calls = api.calls
    assert hold.reconcile(phase)["state"] == "unknown_ack"
    with pytest.raises(SupervisedDenied, match="claimed"):
        hold.apply(phase, plan)
    assert api.calls == calls
    assert (hold.directory / (phase + ".failure.json")).exists()
    if phase == "release":
        with pytest.raises(SupervisedDenied, match="release claimed"):
            hold.held(plan)


@pytest.mark.parametrize("field", ["uid", "spec", "annotation", "rv"])
def test_release_cas_drift_and_replacements(tmp_path, monkeypatch, field):
    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    handoff.apply(plan, pins, handoff.fixture_exports)
    hold.prepare("release", plan)
    if field == "spec":
        api.quota["spec"]["hard"]["pods"] = "2"
    elif field == "annotation":
        api.quota["metadata"]["annotations"] = {}
    else:
        api.quota["metadata"]["uid" if field == "uid" else "resourceVersion"] = "replacement"
    with pytest.raises(SupervisedDenied):
        hold.apply("release", plan)
    assert api.calls == 1


def test_no_adoption_enforcement_or_premature_release(tmp_path, monkeypatch):
    hold, api, _, enforcement, _, plan, _ = setup(tmp_path, monkeypatch)
    api.quota = copy.deepcopy(hold.spec(plan))
    with pytest.raises(SupervisedDenied, match="adopted"):
        hold.prepare("hold", plan)
    api.quota = None
    hold.prepare("hold", plan)
    hold.apply("hold", plan)
    with pytest.raises(KeyError):
        hold.held(plan)  # metadata is insufficient; no enforcement receipt
    enforcement()
    hold.handoff = None
    with pytest.raises(SupervisedDenied, match="handoff"):
        hold.prepare("release", plan)
    with pytest.raises(SupervisedDenied, match="claimed"):
        hold.apply("hold", plan)
    hold.clock = lambda: 201
    with pytest.raises(SupervisedDenied, match="expired"):
        hold.held(plan)


@pytest.mark.parametrize("fault", ["exclusive", "dispatch", "writers", "provider"])
def test_external_prerequisites_are_separate_and_unknown_denies(tmp_path, monkeypatch, fault):
    hold, api, record, _, _, plan, _ = setup(tmp_path, monkeypatch)
    if fault == "exclusive":
        record(
            fault,
            operator_subject="operator",
            access_inventory_sha256="inventory",
            competing_edit_exec_subjects=["other"],
        )
    elif fault == "dispatch":
        record(fault, inventory_sha256="inventory", unsettled_attempts=["attempt"], dispatchers=[])
    elif fault == "writers":
        record(
            fault, inventory_sha256="inventory", unaccounted_writers=["unknown"], old_runtimes=[]
        )
    else:
        record(
            fault,
            inventory_sha256="inventory",
            unaccounted_operations=[],
            operations=[dict(operation_id="provider-op", state="unknown")],
            exposure_cents=100,
        )
    with pytest.raises(SupervisedDenied):
        hold.prepare("hold", plan)
    assert api.calls == 0


@pytest.mark.parametrize("fault", ["enforcement", "custody", "provider_lease"])
def test_release_requires_independent_evidence(tmp_path, monkeypatch, fault):
    hold, api, record, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    handoff.apply(plan, pins, handoff.fixture_exports)
    from tessera_operator.maintenance_witness import read_json

    kind = (
        "provider"
        if fault == "provider_lease"
        else "enforcement"
        if fault == "enforcement"
        else "custody"
    )
    data = read_json(hold.prerequisites[kind]["path"])[0]
    for key in ("kind", "operation", "target", "observed_at", "valid_until"):
        data.pop(key)
    if fault == "enforcement":
        data["quota_uid"] = "replacement"
    elif fault == "custody":
        data["destinations"]["validate"]["device"] += 1
    else:
        data["operations"] = [
            dict(
                operation_id="affected-provider",
                state="guarded",
                guardian_uid="guardian",
                assignment_sha256="assignment",
                lease_until=99,
                reserved_cents=100,
            )
        ]
    record(kind, **data)
    with pytest.raises(SupervisedDenied):
        hold.prepare("release", plan)
    assert api.calls == 1


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "truncated",
        "tampered",
        "identity",
        "alias",
        "retention",
        "policy",
        "receipt",
    ],
)
def test_materialized_custody_and_outstanding_writes_are_independent(tmp_path, monkeypatch, fault):
    # Actual retained test bytes; ONLY external topology/reservation/retention are synthetic.
    from tessera_operator.maintenance_witness import read_json

    hold, api, record, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    handoff.apply(plan, pins, handoff.fixture_exports)
    data = read_json(hold.prerequisites["custody"]["path"])[0]
    for key in ("kind", "operation", "target", "observed_at", "valid_until"):
        data.pop(key)
    root = Path(handoff.fixture_exports["backup"]["destination"]) / "export"
    file = next(p for p in root.rglob("*") if p.is_file())
    if fault == "missing":
        file.unlink()
    elif fault == "truncated":
        file.write_bytes(file.read_bytes()[:1])
    elif fault == "tampered":
        content = file.read_bytes()
        file.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    elif fault == "identity":
        content = file.read_bytes()
        file.unlink()
        file.write_bytes(content)
        file.chmod(0o600)
    elif fault == "alias":
        moved = root.with_name("moved")
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
    elif fault == "retention":
        data["retention_until"] = plan["deadline"] - 1
    elif fault == "policy":
        data["retention_policy"] = "delete_at_expiry"
    elif fault == "receipt":
        data["materialized_receipt"]["sha256"] = "supplied-not-verified"
    record("custody", **data)
    with pytest.raises((SupervisedDenied, OSError, ValueError)):
        hold.prepare("release", plan)
    assert api.calls == 1
    assert not (hold.directory / "release.claim.json").exists()


def test_materialized_bytes_need_no_second_reservation_and_reconnect_retains(tmp_path, monkeypatch):
    from tessera_operator.maintenance_witness import read_json

    hold, api, record, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    data = read_json(hold.prerequisites["custody"]["path"])[0]
    assert "reserved_bytes" not in data  # only bounded outstanding journal writes reserved
    assert "capacity_receipt_sha256" not in data
    for key in ("kind", "operation", "target", "observed_at", "valid_until"):
        data.pop(key)
    data.pop("release_journal", None)
    record("custody", **data)
    handoff.apply(plan, pins, handoff.fixture_exports)
    hold.prepare("release", plan)
    hold.apply("release", plan)
    hold.clock = lambda: plan["deadline"] + 1
    assert hold.reconcile("release")["state"] == "observed_unconfirmed"
    assert api.calls == 2
    assert all(Path(e["destination"]).exists() for e in handoff.fixture_exports.values())
    with pytest.raises(SupervisedDenied, match="stale/mismatched owner evidence"):
        hold.custody(plan)
    destination = Path(handoff.fixture_exports["backup"]["destination"])
    next(p for p in (destination / "export").rglob("*") if p.is_file()).unlink()
    with pytest.raises((SupervisedDenied, OSError, ValueError)):
        hold.reconcile("release")
    assert api.calls == 2


def test_future_release_receipt_bound_is_enforced_before_write(tmp_path, monkeypatch):
    from tessera_operator.bundle_export import directory_identity

    hold, api, _, _, _, _, _ = setup(tmp_path, monkeypatch)
    hold.directory.mkdir(parents=True, exist_ok=True)
    with pytest.raises(SupervisedDenied, match="exceeds bound"):
        hold.write(
            "release",
            "prepared",
            {"journal_identity": directory_identity(hold.directory), "too_large": "x" * 262144},
        )
    assert not (hold.directory / "release.prepared.json").exists()
    assert api.calls == 0


def test_db_window_excludes_release_and_claim_expiry_is_readonly(tmp_path, monkeypatch):
    hold, _, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    handoff.apply(plan, pins, handoff.fixture_exports)
    with hold.db_window(plan):
        with pytest.raises(SupervisedDenied, match="busy"):
            hold.prepare("release", plan)
    hold.clock = lambda: 201
    assert hold.reconcile("hold")["state"] == "captured"
    with pytest.raises(SupervisedDenied, match="expired"):
        hold.prepare("release", plan)


def slot_object(name="unrelated", **fields):
    from tessera_operator import upgrade_support as target

    return dict(metadata=dict(name=name, namespace=target.NAMESPACE, uid=name + "-uid"), **fields)


@pytest.mark.parametrize("phase", ["Pending", "Running", "Unknown", None])
@pytest.mark.parametrize("terminating", [False, True])
def test_rel90_hold01_nonterminal_namespace_pods_deny_before_claim(
    tmp_path, monkeypatch, phase, terminating
):
    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    handoff.apply(plan, pins, handoff.fixture_exports)
    pod = slot_object(
        name=plan["operation"] if terminating else "unrelated-no-pvc", status=dict(phase=phase)
    )
    if terminating:
        pod["metadata"]["deletionTimestamp"] = "2026-10-24T00:00:00Z"
    api.inventory["pods"]["items"] = [pod]
    with pytest.raises(SupervisedDenied, match="sole startup slot"):
        hold.prepare("release", plan)
    assert not (hold.directory / "release.claim.json").exists()
    assert api.calls == 1


@pytest.mark.parametrize(
    "resource,spec",
    [
        ("deployments.apps", {"replicas": 1}),
        ("replicasets.apps", {"replicas": 1}),
        ("replicationcontrollers", {"replicas": 1}),
        ("statefulsets.apps", {"replicas": 1}),
        ("daemonsets.apps", {}),
        ("jobs.batch", {}),
        ("cronjobs.batch", {"suspend": False}),
        ("horizontalpodautoscalers.autoscaling", {}),
    ],
)
def test_rel90_hold01_competing_controller_desire_denies(tmp_path, monkeypatch, resource, spec):
    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    handoff.apply(plan, pins, handoff.fixture_exports)
    api.inventory[resource]["items"] = [slot_object(spec=spec)]
    with pytest.raises(SupervisedDenied, match="competing"):
        hold.prepare("release", plan)
    assert api.calls == 1
    assert not (hold.directory / "release.claim.json").exists()


@pytest.mark.parametrize(
    "fault", ["pagination", "missing_items", "missing_revision", "unknown_namespace"]
)
def test_rel90_hold01_missing_or_partial_inventory_denies(tmp_path, monkeypatch, fault):
    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    handoff.apply(plan, pins, handoff.fixture_exports)
    result = api.inventory["pods"]
    if fault == "pagination":
        result["metadata"]["continue"] = "next-page"
    elif fault == "missing_items":
        result.pop("items")
    elif fault == "missing_revision":
        result["metadata"].pop("resourceVersion")
    else:
        pod = slot_object(status=dict(phase="Succeeded"))
        pod["metadata"].pop("namespace")
        result["items"] = [pod]
    with pytest.raises(SupervisedDenied, match="incomplete"):
        hold.prepare("release", plan)
    assert api.calls == 1


def test_rel90_hold01_apply_rechecks_slot_and_terminal_pods_do_not_count(tmp_path, monkeypatch):
    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    capture(hold, plan, enforcement)
    handoff.apply(plan, pins, handoff.fixture_exports)
    # Terminal Pods do not consume hard.pods, unlike count/pods. Retain them.
    api.inventory["pods"]["items"] = [
        slot_object(name=p, status=dict(phase=p)) for p in ("Succeeded", "Failed")
    ]
    hold.prepare("release", plan)
    api.inventory["pods"]["items"].append(slot_object(status=dict(phase="Pending")))
    with pytest.raises(SupervisedDenied, match="sole startup slot"):
        hold.apply("release", plan)
    assert not (hold.directory / "release.claim.json").exists()
    api.inventory["pods"]["items"].pop()
    hold.apply("release", plan)
    assert api.quota["spec"] == {"hard": {"pods": "1"}}
