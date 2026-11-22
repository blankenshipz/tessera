"""Unregistered local composition; future native admission surrounds EACH effect.

Owner producers write fixed private inputs; requests name only operation/action
and an exact prepared digest. No callback, approval boolean or credentials confer
authority here. No automatic phases, retry, installation or native registration.
"""

import os
import re
import stat
import time
from pathlib import Path

from . import upgrade_support as target
from .bundle_export import BundleExport, directory_identity
from .maintenance_composition import OwnerMaintenance
from .maintenance_handoff import MaintenanceHandoff
from .maintenance_hold import MaintenanceHold
from .maintenance_producers import save
from .maintenance_transport import FixedQuotaDryRunTransport
from .maintenance_witness import read_json
from .schema_supervised import (
    STAGES,
    KubernetesSupervisedProfile,
    SupervisedDenied,
    SupervisedOperation,
    digest,
)

ACTIONS = (*STAGES, "hold", "enforcement", "custody", "handoff", "release")
KINDS = {"exclusive", "dispatch", "writers", "provider", "enforcement", "custody"}


class MaintenanceEntrypoint:
    def __init__(self, *, clock=time.time, completion=None):
        self.clock = clock
        # Trusted integration only, not request arguments. No native context
        # producer/connection is registered or installed by this composition.
        self.completion = completion

    def directory(self, operation):
        if not isinstance(operation, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,62}", operation
        ):
            raise SupervisedDenied("invalid operation identity")
        return target.SOURCE / "private" / "local-control-upgrade" / "supervised" / operation

    def private(self, root, path):
        path = Path(path)
        if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(root):
            raise SupervisedDenied("operation path escape or alias")
        for item in (path, *path.parents):
            info = item.lstat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SupervisedDenied("operation records must be private owner bytes")
            if item == path and not stat.S_ISDIR(info.st_mode):
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SupervisedDenied("operation record not regular")
            if item == root:
                break
        return path

    def read(self, root, path):
        return read_json(self.private(root, path))

    def seal(self, operation):
        """LOCAL OWNER SETUP ONLY: consume producer plan, never request evidence.

        This seals integrity, not authenticated provenance or permission. Actual
        native topology/ownership/drain/reservation producers are still required.
        """
        root = self.directory(operation)
        plan, ref = self.read(root, root / "inputs" / "plan.json")
        if plan["operation"] != operation or self.clock() >= plan["deadline"]:
            raise SupervisedDenied("plan operation mismatch or expired")
        profile = KubernetesSupervisedProfile(clock=self.clock)
        op = SupervisedOperation(root / "phases", profile, clock=self.clock)
        op.create(plan)
        return save(
            root / "sealed.json",
            dict(
                operation=operation,
                plan=ref,
                plan_sha256=digest(plan),
                deadline=plan["deadline"],
                directory=directory_identity(root),
            ),
        )

    def load(self, operation, action):
        if action not in ACTIONS:
            raise SupervisedDenied("unknown maintenance action")
        root = self.directory(operation)
        sealed, _ = self.read(root, root / "sealed.json")  # unknown operation never created
        plan, ref = self.read(root, root / "inputs" / "plan.json")
        if (
            sealed
            != dict(
                operation=operation,
                plan=ref,
                plan_sha256=digest(plan),
                deadline=plan["deadline"],
                directory=directory_identity(root),
            )
            or plan["operation"] != operation
        ):
            raise SupervisedDenied("sealed operation drift")
        inputs, inputs_ref = self.read(root, root / "inputs" / (action + ".json"))
        if set(inputs) - {"prerequisites", "evidence", "pins"} or "prerequisites" not in inputs:
            raise SupervisedDenied("unsupported owner inputs")
        refs = inputs["prerequisites"]
        if set(refs) - KINDS:
            raise SupervisedDenied("unknown prerequisite kind")
        for reference in [
            *refs.values(),
            *[inputs[k] for k in ("evidence", "pins") if k in inputs],
        ]:
            _, observed = self.read(root, reference["path"])
            if observed["sha256"] != reference["sha256"]:
                raise SupervisedDenied("owner input reference drift")
        for name in ("phases", "hold", "handoff", "exports", "custody"):
            if (root / name).exists() or (root / name).is_symlink():
                self.private(root, root / name)
        self.read(root, root / "phases" / "plan.json")
        # Missing producers remain missing; file hashes are never native proof.
        handoff = MaintenanceHandoff(root / "handoff", clock=self.clock)
        hold = MaintenanceHold(
            root / "hold",
            prerequisites=refs,
            handoff=handoff,
            clock=self.clock,
            completion=self.completion,
        )
        profile = KubernetesSupervisedProfile(hold=hold, handoff=handoff, clock=self.clock)
        op = SupervisedOperation(root / "phases", profile, clock=self.clock)
        if op.plan() != plan:
            raise SupervisedDenied("phase plan drift")
        effective = plan | {
            "phase_receipts": {
                stage: self.read(root, op.directory / (stage + ".result.json"))[0]
                for stage in STAGES
                if (op.directory / (stage + ".result.json")).exists()
            }
        }
        owner = OwnerMaintenance(op, hold, handoff, structured_api=FixedQuotaDryRunTransport())
        return root, sealed, inputs_ref, inputs, effective, owner

    def exports(self, root):
        return {
            phase: BundleExport(
                root / "exports" / phase, root / "custody" / phase, clock=self.clock
            )
            for phase in ("backup", "validate")
        }

    def prepare(self, operation, action):
        root, sealed, reference, inputs, plan, owner = self.load(operation, action)
        if self.clock() >= sealed["deadline"]:
            raise SupervisedDenied("expired operation; admission remains held")
        if (root / (action + ".claim.json")).exists():
            raise SupervisedDenied("effect claimed; reconcile only")
        lower = None
        if action in STAGES:
            if "evidence" in inputs:
                evidence_path = owner.operation.directory / (action + ".evidence.json")
                if evidence_path.exists():
                    if self.read(root, evidence_path)[0] != inputs["evidence"]:
                        raise SupervisedDenied("phase evidence drift")
                else:
                    owner.operation.pin_evidence(action, inputs["evidence"])
            lower = owner.operation.prepare(action)
        elif action in {"hold", "release"}:
            if action == "release":
                owner.reconcile_custody(plan, self.exports(root))
            lower = owner.hold.prepare(action, plan)
        else:
            owner.hold.held(plan)
            if action == "handoff":
                if "pins" not in inputs:
                    raise SupervisedDenied("owned runtime pins producer missing")
                owner.handoff.custody(
                    plan,
                    {
                        p: dict(journal=str(e.journal), destination=str(e.destination))
                        for p, e in self.exports(root).items()
                    },
                )
        packet = dict(
            operation=operation,
            action=action,
            plan_sha256=sealed["plan_sha256"],
            deadline=sealed["deadline"],
            inputs=reference,
            lower_sha256=digest(lower),
        )
        save(root / (action + ".prepared.json"), packet)
        return dict(packet=packet, action_sha256=digest(packet))

    def effect(self, operation, action, expected_action_sha256):
        """ONE effect body for a future individually native-admitted handler.

        Deliberately no admission callback or caller boolean. Not registered; do
        not expose as a tool until actual native permission surrounds invocation.
        """
        root, sealed, reference, inputs, plan, owner = self.load(operation, action)
        packet, _ = self.read(root, root / (action + ".prepared.json"))
        if (
            digest(packet) != expected_action_sha256
            or packet["operation"] != operation
            or packet["action"] != action
            or packet["plan_sha256"] != sealed["plan_sha256"]
            or packet["inputs"] != reference
            or packet["deadline"] != sealed["deadline"]
        ):
            raise SupervisedDenied("prepared operation/action/input digest drift")
        if self.clock() >= sealed["deadline"]:
            raise SupervisedDenied("expired operation; admission remains held")
        if action in STAGES:
            lower = self.read(root, owner.operation.directory / (action + ".prepared.json"))[0]
        elif action in {"hold", "release"}:
            lower = self.read(root, owner.hold.directory / (action + ".prepared.json"))[0]
        else:
            lower = None
        if digest(lower) != packet["lower_sha256"]:
            raise SupervisedDenied("lower prepared effect drift")
        # O_EXCL is the cross-reconnect dispatch gate, including guard failures.
        save(root / (action + ".claim.json"), packet)
        try:
            if action in STAGES:
                result = owner.apply(action)
            elif action in {"hold", "release"}:
                if action == "release":
                    owner.reconcile_custody(plan, self.exports(root))
                result = owner.hold.apply(action, plan)
                if action == "release" and self.completion is not None:
                    from .maintenance_completion import MaintenanceCompletion

                    if type(self.completion) is not MaintenanceCompletion:
                        raise SupervisedDenied("native completion integration unavailable")
                    result = self.completion.result(lower, result, packet)
            elif action == "enforcement":
                result = owner.enforcement.collect(plan)
            elif action == "custody":
                owner.hold.held(plan)
                result = owner.custody_readback(plan, self.exports(root))
            else:
                owner.hold.held(plan)
                pins = self.read(root, inputs["pins"]["path"])[0]
                result = owner.handoff.apply(
                    plan,
                    pins,
                    {
                        p: dict(journal=str(e.journal), destination=str(e.destination))
                        for p, e in self.exports(root).items()
                    },
                )
            save(root / (action + ".result.json"), result)
            return result
        except Exception as error:
            save(
                root / (action + ".failure.json"),
                dict(error_type=type(error).__name__, resubmit=False),
            )
            raise

    def reconcile(self, operation, action):
        """Retained local status only; no auth/config/native probe, dispatch or cleanup."""
        root, sealed, _, _, _, _ = self.load(operation, action)
        files = {
            suffix: self.read(root, root / (action + "." + suffix + ".json"))[0]
            for suffix in ("claim", "result", "failure")
            if (root / (action + "." + suffix + ".json")).exists()
        }
        return dict(
            operation=operation,
            action=action,
            state="captured"
            if "result" in files
            else "unknown_ack"
            if "claim" in files
            else "unclaimed",
            records=files,
            expired=self.clock() >= sealed["deadline"],
            resubmit=False,
            recovery="compatible58 forward-only"
            if (root / "phases" / "migrate.claim.json").exists()
            else "no automatic rollback",
            native_admission="external_per_effect_required",
        )
