"""Unregistered owner-only observations. No credential lookup or real-create fallback.

The owner supplies a bounded structured API transport; kubectl error strings are
not API Status objects. Installing that native transport is separate work.
Kubernetes v1.34 resourcequota/controller.go checks requests before its dry-run
short circuit (which skips quota status writes). A successful dry-run is NOT
enforcement evidence. No real Pod is ever requested here.
"""

import json
import os
import re

from . import upgrade_support as target
from .maintenance_hold import NAME, plan_key
from .maintenance_witness import read_json
from .schema_supervised import SupervisedDenied, digest


def save(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > 262144:
        raise SupervisedDenied("receipt exceeds bound")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return read_json(path)[1]


class QuotaEnforcement:
    def __init__(self, hold, structured_api):
        self.hold, self.api = hold, structured_api

    def collect(self, plan):
        hold = self.hold
        with hold.locked():
            if hold.clock() >= plan["deadline"]:
                raise SupervisedDenied("expired enforcement observation")
            if (hold.directory / "release.claim.json").exists():
                raise SupervisedDenied("release claimed; no probe")
            hold.prerequisites_check(plan)
            ack = hold.read("hold.ack")
            if ack["plan_sha256"] != plan_key(plan):
                raise SupervisedDenied("hold plan changed")
            quota = hold.quota()
            hold.match(plan, quota, hold.spec(plan)["spec"], ack["uid"])
            name = "tessera-quota-probe-" + digest(plan["operation"])[:16]
            request = dict(
                method="POST",
                path=f"/api/v1/namespaces/{target.NAMESPACE}/pods",
                query={"dryRun": "All", "fieldValidation": "Strict"},
                body=dict(
                    apiVersion="v1",
                    kind="Pod",
                    metadata=dict(
                        name=name,
                        namespace=target.NAMESPACE,
                        annotations={"tessera-maintenance-operation": plan["operation"]},
                    ),
                    spec=dict(
                        restartPolicy="Never",
                        automountServiceAccountToken=False,
                        schedulingGates=[{"name": "tessera.invalid/never-schedule"}],
                        containers=[dict(name="probe", image="registry.k8s.io/pause:3.10")],
                    ),
                ),
            )
            claim = dict(
                operation=plan["operation"],
                target=plan["target"],
                plan_sha256=plan_key(plan),
                quota_uid=ack["uid"],
                quota_spec_sha256=digest(quota["spec"]),
                quota_resource_version=quota["metadata"]["resourceVersion"],
                request=request,
            )
            claim_path = hold.directory / "enforcement.claim.json"
            if claim_path.exists():
                raise SupervisedDenied("probe claimed; retained receipt only, no replay")
            save(claim_path, claim)
            try:
                response = self.api(request)
                status = response["body"]
                # Exact admission form, not a substring in an RBAC/error message.
                message = (
                    f'pods "{name}" is forbidden: exceeded quota: {NAME}, '
                    "requested: pods=1, used: pods="
                )
                matched = (
                    response["code"] == 403
                    and status.get("kind") == "Status"
                    and status.get("apiVersion") == "v1"
                    and status.get("code") == 403
                    and status.get("reason") == "Forbidden"
                    and status.get("status") == "Failure"
                    and status.get("details") == {"name": name, "kind": "pods"}
                    and re.fullmatch(
                        re.escape(message) + r"\d+, limited: pods=0", status.get("message", "")
                    )
                    is not None
                )
                current = hold.quota()
                hold.match(plan, current, quota["spec"], ack["uid"])
                if current["metadata"]["resourceVersion"] != quota["metadata"]["resourceVersion"]:
                    matched = False
                if hold.clock() >= plan["deadline"]:
                    matched = False
                outcome = "quota_admission_rejection" if matched else "not_quota_proof"
            except Exception:
                # Never persist exception text, API messages or credentials.
                save(
                    hold.directory / "enforcement.result.json",
                    {"outcome": "uncertain", "claim_sha256": digest(claim)},
                )
                raise SupervisedDenied("probe uncertain; retained claim, no replay") from None
            receipt = save(
                hold.directory / "enforcement.result.json",
                dict(outcome=outcome, claim_sha256=digest(claim)),
            )
            if not matched:
                raise SupervisedDenied("response is not quota admission proof")
            value = dict(
                kind="enforcement",
                operation=plan["operation"],
                target=plan["target"],
                observed_at=hold.clock(),
                valid_until=plan["deadline"],
                quota_uid=ack["uid"],
                quota_spec_sha256=digest(quota["spec"]),
                denial_reason="exceeded quota",
                probe_receipt_sha256=receipt["sha256"],
            )
            ref = save(hold.directory / "enforcement.evidence.json", value)
            hold.prerequisites["enforcement"] = dict(ref, owner=f"local-uid:{os.getuid()}")
            return ref
