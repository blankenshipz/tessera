"""Unregistered observation reader, NOT an independent fence or permission issuer.

Uses existing bounded Kubernetes/RO DB and retained phase-result interfaces.
There is no supported schema-maintenance host/admission/guardian witness transport
in this repository. Never serialize this report as schema_supervised evidence.
"""

import hashlib
import json
import os
import stat
import time
from datetime import datetime
from pathlib import Path

from . import upgrade_support as target
from .schema_supervised import BoundedKubernetes, KubernetesSupervisedProfile, SupervisedDenied
from .stopped_observation import StoppedObservation

GAPS = {
    "admission": "schema_supervised.py:evidence consumes files; no maintenance hold readback",
    "host_writers": "stopped_observation.py:snapshot covers namespace Pods, not host writers",
    "guardian": "local_docker_guardian.py:GuardianLease owns fixture peers, not this PVC",
    "pod_replacement_fence": "stopped_observation.py:observe uses named-Pod exec, no UID CAS",
    "durable_export": "schema_supervised.py:pod uses emptyDir scratch; no export/readback adapter",
}


def read_json(path):
    """Bounded stable regular-file read. Integrity is NOT authenticated provenance."""
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise SupervisedDenied("noncanonical evidence path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 262144:
            raise SupervisedDenied("unsupported evidence file")
        raw = stream.read(262145)
        after = os.fstat(stream.fileno())

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    if (
        identity(before) != identity(after)
        or len(raw) > 262144
        or identity(path.stat()) != identity(after)
    ):
        raise SupervisedDenied("evidence changed during read")
    return json.loads(raw), {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def custody_readback(plan, stage, receipts, manifest_path, proof_path):
    """Compare real local copies to producing phase receipts, not arbitrary hashes.

    Reuses resolved SUP-01 phase/operation/target/incarnation/pre-post lineage.
    Does not assert the custodian, snapshot/object bytes, or a live restore ran.
    """
    manifest, manifest_ref = read_json(manifest_path)
    proof, proof_ref = read_json(proof_path)
    evidence = {
        "backup_custody": {
            "manifest_path": str(manifest_path),
            "restore_receipt_path": str(proof_path),
        }
    }
    KubernetesSupervisedProfile.check_custody(plan | {"phase_receipts": receipts}, stage, evidence)
    source = "validate" if stage == "activate" else "backup"
    remote = receipts[source]["remote"]
    KubernetesSupervisedProfile.validate_remote(
        {
            "phase": source,
            "operation": plan["operation"],
            "target": plan["target"],
            "db": remote["db"] | {"sha256": remote["input_db_sha256"]},
        },
        remote,
    )
    if (
        manifest["schema_version"] != remote["db"]["schema"]
        or not manifest["backup_id"]
        or proof["state"] != "held_restore"
    ):
        raise SupervisedDenied("unsupported manifest/held restore lineage")
    return {
        "producer_phase": source,
        "manifest": manifest_ref,
        "held_restore_proof": proof_ref,
        "backup_id": manifest["backup_id"],
        "snapshot_sha256": manifest["sha256"],
        "held_db": proof["held_db"],
        "durable_custodian": None,
        "coverage": "retained phase result and local manifest/proof bytes only",
    }


class MaintenanceReadback:
    """One claimed read attempt; failures persist and cannot be replayed here."""

    def __init__(self, directory, *, transport=None, clock=time.time):
        self.directory = Path(directory)
        self.transport = transport or BoundedKubernetes()
        self.clock = clock

    def collect(self, plan, phase, pins, *, custody=None):
        started = self.clock()
        if phase not in {"scale_zero", "migrate", "validate", "activate"}:
            raise SupervisedDenied("unsupported readback phase")
        if started >= plan["deadline"]:
            raise SupervisedDenied("outage deadline expired")
        if any(pins[k + "_uid"] != plan["target"][k + "_uid"] for k in ("deployment", "pvc", "pv")):
            raise SupervisedDenied("observer/plan target mismatch")
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        target._write_once(
            self.directory / "claim.json",
            {
                "operation": plan["operation"],
                "phase": phase,
                "target": plan["target"],
                "started_at": started,
                "pins": pins,
                "deadline": plan["deadline"],
            },
        )
        try:

            def run(argv):
                return json.dumps(self.transport.call({"argv": argv})).encode()

            seen = StoppedObservation(pins, run=run).observe()
            finished = self.clock()
            observed = datetime.fromisoformat(seen["observed_at"]).timestamp()
            if not started <= observed <= finished < plan["deadline"]:
                raise SupervisedDenied("stale/future observation or expired outage")
            if seen["db"]["incarnation"] != plan["db_incarnation"]:
                raise SupervisedDenied("DB incarnation mismatch")
            local_custody = None
            receipt_refs = {}
            if custody is not None:
                receipts = {}
                for name, path in custody["phase_results"].items():
                    receipts[name], receipt_refs[name] = read_json(path)
                local_custody = custody_readback(
                    plan, phase, receipts, custody["manifest_path"], custody["proof_path"]
                )
            report = {
                "operation": plan["operation"],
                "phase": phase,
                "target": seen["target"],
                "db": seen["db"],
                "observer": seen["observer"],
                "runtime": {
                    k: pins[k]
                    for k in (
                        "observer_image",
                        "observer_image_id",
                        "observer_container_id",
                        "observer_spec_sha256",
                    )
                },
                "started_at": started,
                "finished_at": finished,
                "provenance": {
                    "interface": "StoppedObservation.observe via BoundedKubernetes.call",
                    "context": target.CONTEXT,
                    "namespace": target.NAMESPACE,
                    "snapshot_sha256": seen["snapshot_sha256"],
                    "phase_receipts": receipt_refs,
                    "authentication": "transport context; not independent owner attestation",
                },
                "coverage": "pinned namespace PVC consumers and RO DB snapshot only",
                "custody": local_custody,
                "unsupported": GAPS,
                "stopped_db_proof": False,
                "continuous_fence": False,
            }
            target._write_once(self.directory / "result.json", report)
            return report
        except Exception as exc:
            target._write_once(
                self.directory / "failure.json",
                {
                    "error_type": type(exc).__name__,
                    "resubmit": False,
                    "recovery": "read existing attempt only; admission held after migration claim",
                },
            )
            raise SupervisedDenied("readback failed; retained claim, no retry") from None

    def read(self, *, max_age):
        claim, _ = read_json(self.directory / "claim.json")
        result, _ = read_json(self.directory / "result.json")
        now = self.clock()
        if (
            result["operation"] != claim["operation"]
            or result["phase"] != claim["phase"]
            or not result["started_at"] == claim["started_at"] <= result["finished_at"] <= now
            or max_age < 0
            or now - result["started_at"] > max_age
            or now >= claim["deadline"]
        ):
            raise SupervisedDenied("stale/mismatched retained observation")
        return result

    def require_maintenance_preconditions(self, *, max_age):
        self.read(max_age=max_age)
        # Deliberately not overridable by a report's booleans or file hashes.
        raise SupervisedDenied(
            "unsupported independent maintenance interfaces: "
            + "; ".join(key + ": " + value for key, value in GAPS.items())
        )
