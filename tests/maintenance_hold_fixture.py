"""Explicitly synthetic owner records/API; real claim and evidence files."""

import copy
import hashlib
import json
from pathlib import Path

from tessera_operator.maintenance_hold import NAME, MaintenanceHold
from tessera_operator.maintenance_witness import read_json
from tessera_operator.schema_supervised import digest


def fixture_hold(root, plan, *, handoff=None):
    root.mkdir(parents=True)
    refs = {}

    def record(kind, **fields):
        value = dict(
            kind=kind,
            operation=plan["operation"],
            target=plan["target"],
            observed_at=90,
            valid_until=200,
            **fields,
        )
        path = root / (kind + ".json")
        path.write_text(json.dumps(value, sort_keys=True))
        refs[kind] = dict(
            path=str(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            owner="scripted-independent-owner",
        )

    record(
        "exclusive",
        operator_subject="fixture-operator",
        access_inventory_sha256="synthetic",
        competing_edit_exec_subjects=[],
    )
    record("dispatch", inventory_sha256="synthetic", unsettled_attempts=[], dispatchers=[])
    record(
        "writers",
        inventory_sha256="synthetic",
        unaccounted_writers=[],
        old_runtimes=[
            dict(
                container_id="containerd://old-control",
                finished_at="2026-10-24T00:00:00Z",
                terminal_receipt_sha256="synthetic",
            )
        ],
    )
    record(
        "provider",
        inventory_sha256="synthetic-empty-inventory",
        operations=[],
        unaccounted_operations=[],
        exposure_cents=100,
    )

    class API:
        quota = None
        lose = False
        calls = 0

        inventory = {
            resource: dict(metadata=dict(resourceVersion="inventory-1"), items=[])
            for resource in (
                "pods",
                "deployments.apps",
                "replicasets.apps",
                "replicationcontrollers",
                "statefulsets.apps",
                "daemonsets.apps",
                "jobs.batch",
                "cronjobs.batch",
                "horizontalpodautoscalers.autoscaling",
            )
        }

        def get(self, kind):
            assert kind == "resourcequotas"
            return {"items": [] if self.quota is None else [copy.deepcopy(self.quota)]}

        def call(self, command):
            if "get" in command["argv"]:
                resource = command["argv"][command["argv"].index("get") + 1]
                return copy.deepcopy(self.inventory[resource])
            self.calls += 1
            if "create" in command["argv"]:
                assert self.quota is None
                self.quota = json.loads(command["stdin"])
                self.quota["metadata"].update(uid="quota-owned", resourceVersion="10")
            else:
                assert NAME in command["argv"]
                patch = json.loads(command["argv"][command["argv"].index("-p") + 1])
                for test in patch[:-1]:
                    seen = self.quota
                    for key in test["path"].strip("/").split("/"):
                        seen = seen[key]
                    assert seen == test["value"]
                self.quota["spec"] = patch[-1]["value"]
                self.quota["metadata"]["resourceVersion"] = "11"
            if self.lose:
                raise TimeoutError("scripted acknowledgement loss")
            return copy.deepcopy(self.quota)

    api = API()
    hold = MaintenanceHold(
        root / "journal", prerequisites=refs, transport=api, handoff=handoff, clock=lambda: 100
    )

    def enforcement():
        record(
            "enforcement",
            quota_uid="quota-owned",
            quota_spec_sha256=digest({"hard": {"pods": "0"}}),
            denial_reason="exceeded quota",
            probe_receipt_sha256="synthetic-denial-not-live",
        )
        if handoff is not None:
            from tessera_operator.bundle_export import BundleExport, directory_identity
            from tessera_operator.maintenance_composition import OwnerMaintenance

            exports = {
                phase: BundleExport(entry["journal"], entry["destination"], clock=lambda: 100)
                for phase, entry in handoff.fixture_exports.items()
            }
            materialized = OwnerMaintenance(
                None, hold, handoff, structured_api=None
            ).custody_readback(plan, exports)
            destinations = {
                phase: read_json(Path(entry["journal"]) / "claim.json")[0]["destination"]
                for phase, entry in handoff.fixture_exports.items()
            }
            record(
                "custody",
                failure_domain="maintenance-pod-and-pvc",
                source_pvc_uid=plan["target"]["pvc_uid"],
                custodian_subject="fixture-custodian",
                topology_receipt_sha256="synthetic-topology",
                materialized_receipt=materialized,
                retention_policy="retain_through_recovery_no_auto_cleanup",
                retention_until=200,
                destinations=destinations,
                release_journal=dict(
                    destination=directory_identity(hold.directory),
                    reserved_bytes=4 * 262144,
                    capacity_receipt_sha256="synthetic-reservation-not-live",
                ),
            )

    return hold, api, record, enforcement
