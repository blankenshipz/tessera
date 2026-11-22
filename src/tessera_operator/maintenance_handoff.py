"""Unregistered exact owned Pod deletion; Kubernetes status is not host fencing.

Entry requires phase-linked pre/post local durable export readback. One normal
UID/RV-preconditioned DELETE, then read-only reconnect. Never force, replay, adopt
a replacement, release admission or choose an old-schema rollback.
"""

import re
import time
from pathlib import Path

from . import upgrade_support as target
from .bundle_export import BundleExport, inventory
from .maintenance_witness import read_json
from .schema_supervised import (
    BoundedKubernetes,
    KubernetesSupervisedProfile,
    SupervisedDenied,
    digest,
)


class PodTransport:
    def __init__(self):
        self.reader = BoundedKubernetes()

    def get(self, name):
        # --ignore-not-found supplies an explicit empty List, not a command error.
        return self.reader.call(
            {
                "argv": [
                    *target.KUBECTL,
                    "-n",
                    target.NAMESPACE,
                    "get",
                    "pods",
                    "--field-selector",
                    "metadata.name=" + name,
                    "-o",
                    "json",
                ]
            }
        )

    def delete(self, name, body):
        return target._api_request(
            "DELETE", f"/api/v1/namespaces/{target.NAMESPACE}/pods/{name}", body
        )


def owned(pod, plan, pins):
    if (
        pod["metadata"]["uid"] != pins["uid"]
        or pod["metadata"]["name"] != plan["operation"]
        or pod["metadata"].get("labels", {}).get("tessera-maintenance-operation")
        != plan["operation"]
        or digest(pod["spec"]) != pins["spec_sha256"]
    ):
        raise SupervisedDenied("owned maintenance Pod identity changed")
    if pod["spec"] != KubernetesSupervisedProfile.pod(plan)["spec"]:
        raise SupervisedDenied("not the exact supervised maintenance resource")
    statuses = pod["status"].get("containerStatuses", [])
    if {c["name"] for c in statuses} != {"old", "new"} or len(statuses) != 2:
        raise SupervisedDenied("incomplete runtime coverage")
    for status in statuses:
        pin = pins["containers"][status["name"]]
        if any(status[k] != pin[k] for k in ("containerID", "imageID", "restartCount")):
            raise SupervisedDenied("maintenance runtime replaced")
    return statuses


class MaintenanceHandoff:
    def __init__(self, directory, *, transport=None, clock=time.time):
        self.directory = Path(directory)
        self.transport, self.clock = transport or PodTransport(), clock

    def custody(self, plan, exports):
        if set(exports) != {"backup", "validate"}:
            raise SupervisedDenied("complete phase-linked pre/post durable exports required")
        references = {}
        for phase, stage in (("backup", "migrate"), ("validate", "activate")):
            entry = exports[phase]
            exporter = BundleExport(entry["journal"], entry["destination"], clock=self.clock)
            claim, claim_ref = read_json(Path(entry["journal"]) / "claim.json")
            if (
                claim["operation"] != plan["operation"]
                or claim["target"] != plan["target"]
                or claim["phase"] != phase
            ):
                raise SupervisedDenied("export target/phase changed")
            result = exporter.reconcile()  # byte readback, never recopy
            expected = inventory(plan, phase, plan["phase_receipts"][phase])
            if set(result["files"]) != set(expected):
                raise SupervisedDenied("incomplete export")
            for name, item in expected.items():
                got = result["files"][name]
                if got["sha256"] != item["sha256"] or (
                    item["size"] is not None and got["size"] != item["size"]
                ):
                    raise SupervisedDenied("export differs from producing phase")
            prefix = "pre" if phase == "backup" else "post"
            root = Path(entry["destination"]) / "export"
            KubernetesSupervisedProfile.check_custody(
                plan,
                stage,
                {
                    "backup_custody": {
                        "manifest_path": str(root / prefix / "manifest.json"),
                        "restore_receipt_path": str(root / (phase + ".restore-proof.json")),
                    }
                },
            )
            references[phase] = {"claim": claim_ref, "readback": result}
        return references

    def apply(self, plan, pins, exports):
        """Future per-effect native admission must surround this unregistered call."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", plan["operation"]):
            raise SupervisedDenied("invalid maintenance operation")
        if self.clock() + 10 >= plan["deadline"]:
            raise SupervisedDenied("insufficient bounded deletion window; admission held")
        custody = self.custody(plan, exports)
        pod_list = self.transport.get(plan["operation"])
        if len(pod_list["items"]) != 1:
            raise SupervisedDenied("exact owned Pod unavailable")
        pod = pod_list["items"][0]
        owned(pod, plan, pins)
        if self.clock() + 10 >= plan["deadline"]:
            raise SupervisedDenied("deletion window expired during prerequisite readback")
        body = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "gracePeriodSeconds": 30,
            "preconditions": {
                "uid": pins["uid"],
                "resourceVersion": pod["metadata"]["resourceVersion"],
            },
        }
        claim = {
            "plan": plan,
            "pins": pins,
            "exports": exports,
            "custody": custody,
            "body": body,
            "claimed_at": self.clock(),
            "admission": "held",
            "recovery": "compatible58 forward-only; never replay DELETE",
        }
        self.directory.mkdir(mode=0o700, exist_ok=True)
        target._write_once(self.directory / "claim.json", claim)
        # Retain already terminal kubelet states before deletion can remove them.
        self.record_terminal(claim, pod)
        try:
            code = self.transport.delete(plan["operation"], body)
            if code not in {200, 202}:
                raise SupervisedDenied("UID-preconditioned deletion refused")
            target._write_once(self.directory / "ack.json", {"http_status": code})
        except Exception as exc:
            target._write_once(
                self.directory / "failure.json",
                {
                    "error_type": type(exc).__name__,
                    "next_action": "read existing destination/Pod state only; no replay",
                },
            )
            raise SupervisedDenied("deletion denied/uncertain; admission held") from None
        return self.reconcile()

    def record_terminal(self, claim, pod):
        statuses = owned(pod, claim["plan"], claim["pins"])
        terminal = all(
            set(s["state"]) == {"terminated"}
            and s["state"]["terminated"].get("finishedAt")
            and s["state"]["terminated"].get("containerID") == s["containerID"]
            for s in statuses
        )
        if terminal and not (self.directory / "terminal.json").exists():
            target._write_once(
                self.directory / "terminal.json",
                {
                    "operation": claim["plan"]["operation"],
                    "target": claim["plan"]["target"],
                    "pod": pod,
                    "observed_at": self.clock(),
                    "coverage": "kubelet exact-container termination; not host/global fencing",
                },
            )

    def reconcile(self):
        claim, _ = read_json(self.directory / "claim.json")
        # No mutation or polling/retry. Absence cannot invent a termination record.
        self.custody(claim["plan"], claim["exports"])
        pods = self.transport.get(claim["plan"]["operation"])["items"]
        if len(pods) > 1:
            raise SupervisedDenied("ambiguous maintenance Pod readback")
        if pods:
            self.record_terminal(claim, pods[0])
        if not (self.directory / "terminal.json").exists():
            raise SupervisedDenied("terminal runtime observation missing; host proof unsupported")
        terminal, reference = read_json(self.directory / "terminal.json")
        statuses = owned(terminal["pod"], claim["plan"], claim["pins"])
        if not all(
            set(s["state"]) == {"terminated"}
            and s["state"]["terminated"].get("containerID") == s["containerID"]
            and s["state"]["terminated"].get("finishedAt")
            for s in statuses
        ):
            raise SupervisedDenied("retained terminal observation invalid")
        return {
            "operation": claim["plan"]["operation"],
            "target": claim["plan"]["target"],
            "validate_sha256": digest(claim["plan"]["phase_receipts"]["validate"]),
            "terminal": reference,
            "pins": claim["pins"],
            "admission": "held",
            "schema": 58,
            "deletion_acknowledged": (self.directory / "ack.json").exists(),
            "host_fence": "unsupported independent host interface",
            "offhost_custody": "not established by local byte readback",
        }
