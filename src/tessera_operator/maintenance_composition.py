"""Local composition only; no MCP registration, issuer or permission boolean."""

import os
import stat
from pathlib import Path

from .bundle_export import BundleExport, directory_identity
from .maintenance_producers import QuotaEnforcement, save
from .maintenance_witness import read_json
from .schema_supervised import KubernetesSupervisedProfile, SupervisedDenied, SupervisedOperation


def stable_file(path):
    """Flush already materialized bytes, not a promise of future allocation."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
        ):
            raise SupervisedDenied("custody file must be private owned regular bytes")
        os.fsync(fd)
        after = os.fstat(fd)
        if before != after or after != path.stat():
            raise SupervisedDenied("custody changed during flush")
        return dict(
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
        )
    finally:
        os.close(fd)


class OwnerMaintenance:
    def __init__(self, operation, hold, handoff, *, structured_api):
        if hold.handoff is not handoff:
            raise SupervisedDenied("handoff owner mismatch")
        self.operation, self.hold, self.handoff = operation, hold, handoff
        self.enforcement = QuotaEnforcement(hold, structured_api)

    def custody_readback(self, plan, exports, *, future_receipt_bytes=262144):
        """Observe actual byte locations/capacity, never manufacture topology.

        Existing custodian evidence must already have authenticated topology,
        retention and outstanding-write reservation provenance. Hashes alone cannot
        authenticate these. Until a native producer exists this method produces
        observations only, never a replacement custody prerequisite.
        """
        with self.hold.locked():
            if self.hold.clock() >= plan["deadline"]:
                raise SupervisedDenied("expired custody observation")
            if set(exports) != {"backup", "validate"}:
                raise SupervisedDenied("both custody destinations required")
            # Only this collector's next JSON receipt is bounded here. Release
            # journals/remaining outage effects need their own exact admission;
            # this is neither a total-outage estimate nor a reservation.
            if type(future_receipt_bytes) is not int or future_receipt_bytes != 262144:
                raise SupervisedDenied("bounded custody receipt capacity required")
            if (self.hold.directory / "custody.claim.json").exists():
                raise SupervisedDenied("custody capture claimed; reconcile retained bytes")
            save(
                self.hold.directory / "custody.claim.json",
                dict(
                    operation=plan["operation"],
                    target=plan["target"],
                    exports={
                        p: dict(journal=str(e.journal), destination=str(e.destination))
                        for p, e in exports.items()
                    },
                    future_receipt_bytes=future_receipt_bytes,
                ),
            )
            observations = {}
            for phase, exporter in exports.items():
                if type(exporter) is not BundleExport:
                    raise SupervisedDenied("real exporter required")
                claim, claim_ref = read_json(exporter.journal / "claim.json")
                if (
                    claim["operation"] != plan["operation"]
                    or claim["target"] != plan["target"]
                    or claim["phase"] != phase
                ):
                    raise SupervisedDenied("custody lineage changed")
                path = Path(exporter.destination)
                info = path.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise SupervisedDenied("custody directory not private owner storage")
                exporter.reconcile()  # full stable destination-byte verification
                files = {name: stable_file(path / "export" / name) for name in claim["files"]}
                directories = {path, path / "export"}
                for name in claim["files"]:
                    parent = (path / "export" / name).parent
                    while parent != path:
                        directories.add(parent)
                        parent = parent.parent
                for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                    directory_identity(directory)
                    fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                exporter.reconcile()  # hashes/SQLite lineage after all flushes
                space = os.statvfs(path)
                if space.f_bavail * space.f_frsize < future_receipt_bytes:
                    raise SupervisedDenied("insufficient observed future receipt capacity")
                observations[phase] = dict(
                    destination=directory_identity(path),
                    owner_uid=info.st_uid,
                    available_bytes=space.f_bavail * space.f_frsize,
                    verified_bytes=sum(item["size"] for item in files.values()),
                    files=files,
                    claim_sha256=claim_ref["sha256"],
                    topology="unsupported",
                    retention="unproven",
                    reserved_bytes=None,
                )
            if self.hold.clock() >= plan["deadline"]:
                raise SupervisedDenied("custody capture expired; retained files not removed")
            journal_space = os.statvfs(self.hold.directory)
            if journal_space.f_bavail * journal_space.f_frsize < future_receipt_bytes:
                raise SupervisedDenied("insufficient custody journal capacity")
            return save(
                self.hold.directory / "custody.observation.json",
                dict(
                    operation=plan["operation"],
                    target=plan["target"],
                    observed_at=self.hold.clock(),
                    admission_deadline=plan["deadline"],
                    future_receipt_bytes=future_receipt_bytes,
                    future_capacity="observed_not_reserved",
                    retention="no_auto_cleanup_retain_through_recovery",
                    destinations=observations,
                    acceptance="observation_only_not_independence_or_reservation",
                ),
            )

    def reconcile_custody(self, plan, exports):
        """Read-only recovery after deadline too; never release or delete on expiry."""
        captured, _ = read_json(self.hold.directory / "custody.observation.json")
        claim, _ = read_json(self.hold.directory / "custody.claim.json")
        if (
            captured["operation"] != plan["operation"]
            or captured["target"] != plan["target"]
            or claim["operation"] != plan["operation"]
            or claim["target"] != plan["target"]
            or captured["admission_deadline"] != plan["deadline"]
            or set(exports) != {"backup", "validate"}
        ):
            raise SupervisedDenied("custody reconnect lineage mismatch")
        for phase, exporter in exports.items():
            if type(exporter) is not BundleExport or claim["exports"][phase] != dict(
                journal=str(exporter.journal), destination=str(exporter.destination)
            ):
                raise SupervisedDenied("custody reconnect exporter changed")
            entry = captured["destinations"][phase]
            if directory_identity(exporter.destination) != entry["destination"]:
                raise SupervisedDenied("custody storage identity changed")
            _, reference = read_json(exporter.journal / "claim.json")
            if reference["sha256"] != entry["claim_sha256"]:
                raise SupervisedDenied("custody producer claim drift")
            exporter.reconcile()
            producer, _ = read_json(exporter.journal / "claim.json")
            if (
                producer["operation"] != plan["operation"]
                or producer["target"] != plan["target"]
                or producer["phase"] != phase
                or set(entry["files"]) != set(producer["files"])
            ):
                raise SupervisedDenied("custody retained lineage changed")
            for name, expected in entry["files"].items():
                if stable_file(exporter.destination / "export" / name) != expected:
                    raise SupervisedDenied("custody retained file identity drift")
        return captured

    def apply(self, phase):
        # Existing lifecycle performs all owner evidence and claim checks. Missing
        # ACL/external creator/host writer/drain producers remain fail-closed.
        if (
            type(self.operation) is not SupervisedOperation
            or type(self.operation.profile) is not KubernetesSupervisedProfile
            or self.operation.profile.hold is not self.hold
            or self.operation.profile.handoff is not self.handoff
        ):
            raise SupervisedDenied("real hold-bound lifecycle required")
        return self.operation.apply(phase)

    def release(self, plan, exports):
        self.reconcile_custody(plan, exports)
        # This local observer cannot authenticate host/PVC topology. Only the
        # EXISTING owner-pinned custody prerequisite may provide it; never turn
        # observed free space or a pathname into that prerequisite.
        if "custody" not in self.hold.prerequisites:
            raise SupervisedDenied("missing native owner-proven sourcePVC/Mac topology and custody")
        self.hold.custody(plan)
        self.hold.prepare("release", plan)
        return self.hold.apply("release", plan)
