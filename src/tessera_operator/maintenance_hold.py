"""Unregistered supervised quota effects; metadata is not enforcement proof.

One trusted operator owns edits/exec. Persistent quota excludes new Pods, not
existing writers. Owner-pinned external evidence is required, never issued here.
"""

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path

from . import upgrade_support as target
from .maintenance_witness import read_json
from .schema_supervised import BoundedKubernetes, SupervisedDenied, digest

NAME = "tessera-maintenance-hold"


def plan_key(plan):
    return digest({k: v for k, v in plan.items() if k not in {"phase_receipts", "phase_evidence"}})


class MaintenanceHold:
    def __init__(
        self,
        directory,
        *,
        prerequisites,
        transport=None,
        handoff=None,
        clock=time.time,
        completion=None,
    ):
        self.directory = Path(directory)
        self.prerequisites = prerequisites
        self.transport = transport or BoundedKubernetes()
        self.handoff, self.clock = handoff, clock
        self.completion = completion

    @contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.directory / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SupervisedDenied("maintenance effect/DB window busy") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self, name):
        return read_json(self.directory / (name + ".json"))[0]

    def record(self, kind, plan):
        ref = self.prerequisites[kind]
        value, observed = read_json(ref["path"])
        if (
            observed["sha256"] != ref["sha256"]
            or not ref["owner"]
            or value["operation"] != plan["operation"]
            or value["target"] != plan["target"]
            or value["kind"] != kind
            or value["valid_until"] < plan["deadline"]
            or not value["observed_at"] <= self.clock() < value["valid_until"]
        ):
            raise SupervisedDenied("stale/mismatched owner evidence")
        return value

    def prerequisites_check(self, plan):
        if self.clock() >= plan["deadline"]:
            raise SupervisedDenied("expired maintenance window; hold not released")
        exclusive = self.record("exclusive", plan)
        if (
            not exclusive["operator_subject"]
            or not exclusive["access_inventory_sha256"]
            or exclusive["competing_edit_exec_subjects"] != []
        ):
            raise SupervisedDenied("exclusive edit/exec ownership missing")
        drained = self.record("dispatch", plan)
        if (
            not drained["inventory_sha256"]
            or drained["unsettled_attempts"] != []
            or any(not d["terminal_receipt_sha256"] for d in drained["dispatchers"])
        ):
            raise SupervisedDenied("affected dispatch not drained")
        writers = self.record("writers", plan)
        if (
            not writers["inventory_sha256"]
            or writers["unaccounted_writers"] != []
            or not writers["old_runtimes"]
            or any(
                not w["container_id"] or not w["finished_at"] or not w["terminal_receipt_sha256"]
                for w in writers["old_runtimes"]
            )
        ):
            raise SupervisedDenied("old writer termination missing")
        provider = self.record("provider", plan)
        if (
            not provider["inventory_sha256"]
            or provider["unaccounted_operations"] != []
            or not 0 <= provider["exposure_cents"] <= 5000
        ):
            raise SupervisedDenied("provider inventory unknown")
        reserved = 0
        for op in provider["operations"]:
            if not op["operation_id"]:
                raise SupervisedDenied("affected provider identity missing")
            if op["state"] == "terminal_settled":
                if not op["terminal_receipt_sha256"] or not op["settlement_receipt_sha256"]:
                    raise SupervisedDenied("provider settlement missing")
            elif op["state"] == "guarded":
                if (
                    not op["guardian_uid"]
                    or not op["assignment_sha256"]
                    or op["lease_until"] < plan["deadline"]
                    or op["reserved_cents"] < 0
                ):
                    raise SupervisedDenied("affected provider guardian missing")
                reserved += op["reserved_cents"]
            else:
                raise SupervisedDenied("unknown provider state is not absent")
        if reserved > provider["exposure_cents"]:
            raise SupervisedDenied("provider reserves exceed accounted exposure")

    @staticmethod
    def spec(plan):
        return dict(
            apiVersion="v1",
            kind="ResourceQuota",
            metadata=dict(
                name=NAME,
                namespace=target.NAMESPACE,
                annotations={"tessera-maintenance-operation": plan["operation"]},
            ),
            spec={"hard": {"pods": "0"}},
        )

    def quota(self):
        items = self.transport.get("resourcequotas")["items"]
        matches = [q for q in items if q["metadata"]["name"] == NAME]
        if len(matches) > 1:
            raise SupervisedDenied("ambiguous quota")
        return matches[0] if matches else None

    def match(self, plan, quota, spec, uid=None):
        expected = self.spec(plan)
        if (
            not quota
            or quota["metadata"].get("namespace") != target.NAMESPACE
            or quota["metadata"].get("annotations") != expected["metadata"]["annotations"]
            or quota["spec"] != spec
            or not quota["metadata"].get("uid")
            or not quota["metadata"].get("resourceVersion")
            or (uid is not None and quota["metadata"]["uid"] != uid)
        ):
            raise SupervisedDenied("quota UID/spec/ownership changed")

    def held(self, plan):
        if (self.directory / "release.claim.json").exists():
            raise SupervisedDenied("release claimed; no further DB exec, including reconnect")
        self.prerequisites_check(plan)
        ack = self.read("hold.ack")
        if ack["plan_sha256"] != plan_key(plan):
            raise SupervisedDenied("hold plan changed")
        quota = self.quota()
        self.match(plan, quota, self.spec(plan)["spec"], ack["uid"])
        enforcement = self.record("enforcement", plan)
        if (
            enforcement["quota_uid"] != ack["uid"]
            or enforcement["quota_spec_sha256"] != digest(quota["spec"])
            or enforcement["denial_reason"] != "exceeded quota"
            or not enforcement["probe_receipt_sha256"]
        ):
            raise SupervisedDenied("quota enforcement proof missing")
        return quota

    def released(self, plan):
        from .release_journal import ReleaseJournal

        packet = self.read("release.claim")
        ack = ReleaseJournal(self.directory, packet["journal_identity"]).read("ack")
        if ack["plan_sha256"] != plan_key(plan):
            raise SupervisedDenied("release plan changed")
        self.match(plan, self.quota(), {"hard": {"pods": "1"}}, ack["uid"])
        self.confirm_completion(packet, ack)
        self.prerequisites_check(plan)
        self.custody(plan)

    def confirm_completion(self, packet, ack):
        from .maintenance_completion import MaintenanceCompletion

        if type(self.completion) is not MaintenanceCompletion:
            raise SupervisedDenied(
                "release ack observed/unconfirmed; native completion unavailable"
            )
        return self.completion.confirm(packet, ack)

    def custody(self, plan):
        """Retained bytes occupy their storage already; reserve only future writes.

        Directory device identity alone cannot prove independence from the PVC.
        The record's producing owner/provenance remains a live setup prerequisite.
        """
        record = self.record("custody", plan)
        if (
            record["failure_domain"] != "maintenance-pod-and-pvc"
            or record["source_pvc_uid"] != plan["target"]["pvc_uid"]
            or not record["custodian_subject"]
            or not record["topology_receipt_sha256"]
            or record["retention_until"] < plan["deadline"]
            or record.get("retention_policy") != "retain_through_recovery_no_auto_cleanup"
        ):
            raise SupervisedDenied("independent local custody evidence missing")
        if set(record["destinations"]) != {"backup", "validate"}:
            raise SupervisedDenied("pre/post custodian destinations required")
        handoff_claim, observation = self.retained_custody(plan)
        if record.get("materialized_receipt") != observation:
            raise SupervisedDenied("custody materialized receipt changed")
        for phase, entry in handoff_claim["exports"].items():
            claim, _ = read_json(Path(entry["journal"]) / "claim.json")
            if claim["destination"] != record["destinations"][phase]:
                raise SupervisedDenied("custodian destination changed")
        # Already materialized bytes need no duplicate capacity reservation.
        # Four bounded records are attempted, not guaranteed future storage.
        # Claim/fallback must fsync before quota; later I/O remains unconfirmed.

    def retained_custody(self, plan):
        """Local reconnect readback, including after admission/authority expiry."""
        handoff_claim, _ = read_json(self.handoff.directory / "claim.json")
        from .bundle_export import BundleExport
        from .maintenance_composition import OwnerMaintenance

        exports = {
            phase: BundleExport(entry["journal"], entry["destination"], clock=self.clock)
            for phase, entry in handoff_claim["exports"].items()
        }
        # Checks phase manifests/restore lineage as well as every actual byte.
        self.handoff.custody(plan, handoff_claim["exports"])
        OwnerMaintenance(None, self, self.handoff, structured_api=None).reconcile_custody(
            plan, exports
        )
        _, observation = read_json(self.directory / "custody.observation.json")
        return handoff_claim, observation

    def write(self, phase, suffix, value):
        path = self.directory / (phase + "." + suffix + ".json")
        if phase == "release":
            from .release_journal import ReleaseJournal

            packet = value if suffix == "prepared" else self.read("release.prepared")
            ReleaseJournal(self.directory, packet["journal_identity"]).write(suffix, value)
        else:
            target._write_once(path, value)

    @contextmanager
    def db_window(self, plan):
        with self.locked():
            self.held(plan)
            yield

    def binding(self, phase, plan):
        if phase not in {"hold", "release"}:
            raise SupervisedDenied("unknown quota effect")
        self.prerequisites_check(plan)
        if (self.directory / (phase + ".claim.json")).exists():
            raise SupervisedDenied("quota effect claimed; reconcile only, no replay")
        slot_inventory = None
        if phase == "hold":
            if self.quota() is not None:
                raise SupervisedDenied("existing quota cannot be adopted")
            command = dict(
                argv=[*target.KUBECTL, "-n", target.NAMESPACE, "create", "-f", "-", "-o", "json"],
                stdin=json.dumps(self.spec(plan)),
            )
        else:
            from .maintenance_handoff import MaintenanceHandoff

            quota = self.held(plan)
            if type(self.handoff) is not MaintenanceHandoff:
                raise SupervisedDenied("real custody/terminal handoff required")
            handoff = self.handoff.reconcile()
            if (
                handoff["operation"] != plan["operation"]
                or handoff["target"] != plan["target"]
                or not handoff["deletion_acknowledged"]
                or handoff["validate_sha256"] != digest(plan["phase_receipts"]["validate"])
            ):
                raise SupervisedDenied("handoff binding changed")
            self.custody(plan)
            slot_inventory = self.release_slot(plan)
            patch = [
                {"op": "test", "path": path, "value": value}
                for path, value in (
                    ("/metadata/uid", quota["metadata"]["uid"]),
                    ("/metadata/resourceVersion", quota["metadata"]["resourceVersion"]),
                    ("/metadata/annotations", quota["metadata"]["annotations"]),
                    ("/spec", quota["spec"]),
                )
            ]
            patch.append({"op": "replace", "path": "/spec", "value": {"hard": {"pods": "1"}}})
            command = dict(
                argv=[
                    *target.KUBECTL,
                    "-n",
                    target.NAMESPACE,
                    "patch",
                    "resourcequota",
                    NAME,
                    "--type=json",
                    "-p",
                    json.dumps(patch),
                    "-o",
                    "json",
                ]
            )
        packet = dict(
            phase=phase,
            plan_sha256=plan_key(plan),
            plan=plan,
            prerequisites=self.prerequisites,
            command=command,
            slot_inventory=slot_inventory,
        )
        if phase == "release":
            from .bundle_export import directory_identity

            packet["journal_identity"] = directory_identity(self.directory)
        return packet

    def release_slot(self, plan):
        """Snapshot only: exclusive ownership must prevent subsequent competition.

        hard.pods counts nonterminal Pods namespace-wide, not only PVC users.
        Conservatively deny nonterminal terminating/deadline-expired Pods too;
        neither disappearance requests nor a stale quota used count free a slot.
        Standard controllers are inspected for desired future work, including
        controllers whose creates are currently denied by hard.pods=0. External
        ownership inventory must also account for custom/external creators.
        """
        inventory = {}
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
        ):
            result = self.transport.call(
                {
                    "argv": [
                        *target.KUBECTL,
                        "-n",
                        target.NAMESPACE,
                        "get",
                        resource,
                        "--chunk-size=0",
                        "-o",
                        "json",
                    ]
                }
            )
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("items"), list)
                or not result.get("metadata", {}).get("resourceVersion")
                or result["metadata"].get("continue")
                or result["metadata"].get("remainingItemCount", 0) != 0
            ):
                raise SupervisedDenied("namespace slot inventory incomplete")
            items = result["items"]
            seen = set()
            for obj in items:
                meta = obj.get("metadata", {})
                if (
                    meta.get("namespace") != target.NAMESPACE
                    or not meta.get("uid")
                    or not meta.get("name")
                    or meta["uid"] in seen
                ):
                    raise SupervisedDenied("namespace slot object identity incomplete")
                seen.add(meta["uid"])
                spec = obj.get("spec", {})
                if resource == "pods":
                    if obj.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
                        raise SupervisedDenied("namespace Pod may consume sole startup slot")
                elif resource in {
                    "deployments.apps",
                    "replicasets.apps",
                    "replicationcontrollers",
                    "statefulsets.apps",
                }:
                    if type(spec.get("replicas")) is not int or spec["replicas"] != 0:
                        raise SupervisedDenied("competing controller replica intent")
                    if resource == "deployments.apps" and meta["name"] == "tessera-control":
                        if meta["uid"] != plan["target"].get("deployment_uid"):
                            raise SupervisedDenied("activation Deployment identity changed")
                elif resource == "jobs.batch":
                    terminal = any(
                        c.get("type") in {"Complete", "Failed"} and c.get("status") == "True"
                        for c in obj.get("status", {}).get("conditions", [])
                    )
                    if not terminal and spec.get("suspend") is not True:
                        raise SupervisedDenied("competing Job intent")
                elif resource == "cronjobs.batch":
                    if spec.get("suspend") is not True:
                        raise SupervisedDenied("competing CronJob intent")
                else:
                    # DaemonSets and autoscalers have no safe replica-zero hold.
                    raise SupervisedDenied("competing daemon/autoscaler intent")
            inventory[resource] = digest(sorted(items, key=lambda obj: obj["metadata"]["uid"]))
        return inventory

    def prepare(self, phase, plan):
        with self.locked():
            packet = self.binding(phase, plan)
            self.write(phase, "prepared", packet)
            return packet

    def apply(self, phase, plan):
        """Future native handler must admit EACH exact effect; never registered here."""
        with self.locked():
            packet = self.binding(phase, plan)
            if packet != self.read(phase + ".prepared"):
                raise SupervisedDenied("quota prepared effect drift")
            self.write(phase, "claim", packet)
            if phase == "release":
                # If either durable write fails, no quota call occurs. Even a
                # partial claim permanently refuses replay. No allocation fiction.
                self.write(
                    phase,
                    "failure",
                    dict(
                        state="unknown_ack",
                        resubmit=False,
                        plan_sha256=plan_key(plan),
                        claim_sha256=digest(packet),
                    ),
                )
            try:
                quota = self.transport.call(packet["command"])
                uid = None if phase == "hold" else self.read("hold.ack")["uid"]
                self.match(plan, quota, {"hard": {"pods": "0" if phase == "hold" else "1"}}, uid)
            except Exception as exc:
                if phase != "release":
                    self.write(
                        phase, "failure", {"error_type": type(exc).__name__, "resubmit": False}
                    )
                raise
            ack = dict(
                plan_sha256=plan_key(plan),
                uid=quota["metadata"]["uid"],
                resource_version=quota["metadata"]["resourceVersion"],
                response_sha256=digest(quota),
            )
            self.write(phase, "ack", ack)
            return ack

    def reconcile(self, phase):
        """Read-only reconnect; observed poststate never promotes unknown ack."""
        packet = self.read(phase + ".claim")
        if phase == "release":
            self.retained_custody(packet["plan"])
        quota = self.quota()
        ack_path = self.directory / (phase + ".ack.json")
        uid = self.read("hold.ack")["uid"] if (self.directory / "hold.ack.json").exists() else None
        self.match(packet["plan"], quota, {"hard": {"pods": "0" if phase == "hold" else "1"}}, uid)
        captured = ack_path.exists()
        ack_observed = False
        if phase == "release":
            from .release_journal import ReleaseJournal

            journal = ReleaseJournal(self.directory, packet["journal_identity"])
            journal.check()
            try:
                ack = journal.read("ack")
                ack_observed = (
                    ack["plan_sha256"] == plan_key(packet["plan"])
                    and ack["uid"] == uid
                    and bool(ack["resource_version"])
                    and bool(ack["response_sha256"])
                )
            except (OSError, ValueError, KeyError):
                ack_observed = False
            # Fresh reads have no independent native completion evidence. Even
            # a successful writer's visible bytes cannot be promoted here.
            captured = False
            if ack_observed and self.completion is not None:
                try:
                    self.prerequisites_check(packet["plan"])
                    self.custody(packet["plan"])
                    self.confirm_completion(packet, ack)
                    captured = True
                except (SupervisedDenied, OSError, ValueError, KeyError):
                    pass
        return dict(
            state=(
                "observed_unconfirmed"
                if phase == "release" and ack_observed and not captured
                else "captured"
                if captured
                else "unknown_ack"
            ),
            **(
                {
                    "completion_confirmed": captured,
                    "ack_observed": ack_observed,
                    "confirmation": "original_native_completion"
                    if captured
                    else "independent_native_completion_unconfirmed",
                }
                if phase == "release"
                else {}
            ),
            quota=quota,
            resubmit=False,
            enforcement_proven=False,
        )
