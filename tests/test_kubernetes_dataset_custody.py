"""Actual private SQLite/bytes; synthetic native client/control responses only."""

import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_dataset_job_inputs import delivery
from test_kubernetes_contracts import _binding, _cluster

from tessera_worker.kubernetes_dataset_custody import DatasetCustodyError, KubernetesDatasetCustody
from tessera_worker.kubernetes_dispatch_service import KubernetesClaimIntentJournal


class Native:
    def __init__(self):
        self.resources = {}
        self.creates = 0
        self.lost = False

    def dataset_read(self, name):
        return deepcopy(self.resources.get(name))

    def dataset_create(self, resource):
        self.creates += 1
        actual = deepcopy(resource)
        actual["metadata"]["uid"] = f"uid-{self.creates}"
        self.resources[actual["metadata"]["name"]] = actual
        if self.lost:
            self.lost = False
            raise ConnectionError("lost native create reply")
        return deepcopy(actual)

    def dataset_delete(self, name, uid):
        assert self.resources[name]["metadata"]["uid"] == uid
        del self.resources[name]


@pytest.fixture
def custody(tmp_path):
    descriptor, resources, bundle = delivery(include_bundle=True)
    binding = _binding(
        dataset_input=descriptor,
        active_deadline_seconds=120,
        command=(
            "python",
            "-m",
            "tessera_starter",
            "--snapshot-root",
            "/dataset",
            "--manifest-digest",
            descriptor["manifest_digest"],
            "--variant",
            "constant",
            "--output-root",
            "/work",
        ),
    )
    package = SimpleNamespace(
        binding=binding,
        operation_id=binding.operation_id,
        package_digest=binding.package_sha256,
        dataset_resources=tuple(resources),
        package={
            "snapshot_digest": descriptor["snapshot_digest"],
            "worker_principal_id": "worker-1",
            "grant_id": "grant-1",
            "project_id": "project-1",
        },
    )
    claims = KubernetesClaimIntentJournal(tmp_path, "dispatch-1")
    native = Native()
    owned = KubernetesDatasetCustody(claims, _cluster(), native, lambda _: deepcopy(bundle))
    return owned, package, native, bundle


def test_creation_intent_uid_cas_restart_and_cleanup(custody):
    owned, package, native, _ = custody
    receipts = owned.prepare(package)
    assert native.creates == len(receipts)
    restarted = KubernetesDatasetCustody(owned.claims, owned.cluster, native, owned.fetch_inputs)
    assert restarted.prepare(package) == receipts
    assert native.creates == len(receipts)
    restarted.cleanup(package)
    assert native.resources == {}
    restarted.cleanup(package)  # exact completed cleanup remains idempotent


def test_lost_reply_adopts_only_exact_planned_resource(custody):
    owned, package, native, _ = custody
    native.lost = True
    with pytest.raises(ConnectionError):
        owned.prepare(package)
    assert native.creates == 1
    restarted = KubernetesDatasetCustody(owned.claims, owned.cluster, native, owned.fetch_inputs)
    assert len(restarted.prepare(package)) == len(package.dataset_resources)
    assert native.creates == len(package.dataset_resources)


@pytest.mark.parametrize("mutation", ("uid", "data", "owner", "immutable", "namespace"))
def test_foreign_resource_never_adopted_or_deleted(custody, mutation):
    owned, package, native, _ = custody
    owned.prepare(package)
    value = next(iter(native.resources.values()))
    if mutation == "uid":
        value["metadata"]["uid"] = "foreign"
    elif mutation == "data":
        value["binaryData"] = {"part-0": "Zm9yZWlnbg=="}
    elif mutation == "owner":
        value["metadata"]["labels"]["tessera.dev/project"] = "foreign"
    elif mutation == "namespace":
        value["metadata"]["namespace"] = "foreign"
    else:
        value["immutable"] = False
    with pytest.raises((DatasetCustodyError, ValueError)):
        owned.prepare(package)
    with pytest.raises(DatasetCustodyError):
        owned.cleanup(package)
    assert native.resources


@pytest.mark.parametrize(("revoke_at", "creates"), [(3, 0), (5, 1)])
def test_current_control_fence_on_every_adoption_and_create(custody, revoke_at, creates):
    owned, package, native, bundle = custody
    calls = 0

    def current(_):
        nonlocal calls
        calls += 1
        if calls == revoke_at:
            raise PermissionError("current grant revoked")
        return deepcopy(bundle)

    owned.fetch_inputs = current
    with pytest.raises(PermissionError):
        owned.prepare(package)
    assert native.creates == creates


def test_recovery_bundle_not_authority(custody):
    owned, package, native, bundle = custody
    response = {
        "package": {"operation_id": package.operation_id},
        "package_digest": package.package_digest,
    }
    owned.retain_bundle(response, bundle)
    assert owned.recovery_bundle(response) == bundle

    def revoked(_):
        raise PermissionError("expired lease/release")

    owned.fetch_inputs = revoked
    with pytest.raises(PermissionError):
        owned.prepare(package)
    assert native.creates == 0


def test_changed_operation_cluster_and_resource_intent_denies(custody):
    owned, package, native, _ = custody
    owned.prepare(package)
    package.package_digest = "a" * 64
    with pytest.raises(DatasetCustodyError, match="intent changed"):
        owned.prepare(package)


def test_unknown_create_uid_requires_reconciliation_before_cleanup(custody):
    owned, package, native, _ = custody
    native.lost = True
    with pytest.raises(ConnectionError):
        owned.prepare(package)
    with pytest.raises(DatasetCustodyError, match="reconciliation"):
        owned.cleanup(package)
    assert native.resources


def test_disappeared_bound_resource_never_recreated(custody):
    owned, package, native, _ = custody
    owned.prepare(package)
    count = native.creates
    native.resources.clear()
    with pytest.raises(DatasetCustodyError, match="no replacement"):
        owned.prepare(package)
    assert native.creates == count


def test_uncertain_absent_create_never_blindly_retried(custody):
    owned, package, native, _ = custody
    calls = 0

    def lost_before_effect(resource):
        nonlocal calls
        calls += 1
        raise ConnectionError("unknown create outcome")

    native.dataset_create = lost_before_effect
    with pytest.raises(ConnectionError):
        owned.prepare(package)
    restarted = KubernetesDatasetCustody(owned.claims, owned.cluster, native, owned.fetch_inputs)
    with pytest.raises(DatasetCustodyError, match="inspect only"):
        restarted.prepare(package)
    with pytest.raises(DatasetCustodyError, match="reconciliation"):
        restarted.cleanup(package)
    assert calls == 1


def test_serial_steps_declare_all_plans_and_record_uid_before_next_command(custody):
    owned, package, native, _ = custody
    steps = owned.prepare_steps(package)
    callback, arguments = next(steps)
    with sqlite3.connect(owned.claims.path) as db:
        assert db.execute("SELECT COUNT(*) FROM dataset_resource_custody").fetchone()[0] == len(
            package.dataset_resources
        )
    assert native.creates == 0
    callback, arguments = steps.send(callback(*arguments))
    assert callback == native.dataset_create
    observed = callback(*arguments)
    callback, arguments = steps.send(observed)
    with sqlite3.connect(owned.claims.path) as db:
        assert (
            db.execute(
                "SELECT uid FROM dataset_resource_custody WHERE name=?", (arguments[0],)
            ).fetchone()[0]
            == observed["metadata"]["uid"]
        )
    assert callback == native.dataset_read
    steps.close()
    assert native.creates == 1
    owned.cleanup(package)
    assert not native.resources


def test_serial_steps_revocation_after_uid_keeps_receipt_without_next_effect(custody):
    owned, package, native, _ = custody
    steps = owned.prepare_steps(package)
    read, args = next(steps)
    create, args = steps.send(read(*args))
    actual = create(*args)

    def revoked(_):
        raise PermissionError("revoked during command")

    owned.fetch_inputs = revoked
    with pytest.raises(PermissionError):
        steps.send(actual)
    with sqlite3.connect(owned.claims.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM dataset_resource_custody WHERE uid IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
    assert native.creates == 1
