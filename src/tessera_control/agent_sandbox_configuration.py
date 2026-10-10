"""Digest-approved server composition and authenticated independent witness refs."""

import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from pydantic import Field

from tessera_worker.capability_witness import KINDS, WitnessPin, open_report, safe_payload

from .agent_sandbox_models import Digest, FrozenModel, SandboxTarget
from .store import canonical_digest

Reference = Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^/")]


class WitnessReference(FrozenModel):
    report_reference: Reference
    report_sha256: Digest
    authentication_reference: Reference
    pin: WitnessPin


class SandboxOperatorConfig(FrozenModel):
    target: SandboxTarget
    ca_reference: Reference
    token_reference: Reference
    witnesses: dict[str, WitnessReference]
    collector_image: Annotated[str, Field(pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$")] | None = None


def private_bytes(reference, maximum):
    path = Path(reference)
    if not path.is_absolute():
        raise ValueError("absolute private reference required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise ValueError("private reference unavailable")
        with os.fdopen(fd, "rb", closefd=False) as source:
            value = source.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("private reference exceeds bound")
        return value
    finally:
        os.close(fd)


def scope_digest(target):
    # The receipt hash is excluded to avoid a self-referential digest. All actual
    # tenant/namespace/runtime/controller/CRD and witness identity pins remain.
    return canonical_digest(target.model_dump(exclude={"witness_sha256"}))


CASES = {
    "cni": {"ingress", "egress", "dns"},
    "quota": {"cpu", "memory", "storage"},
    "watchdog": {"host-pid", "host-filesystem", "runtime-isolation", "terminated-uid"},
    "admission": {
        "fixed-profile",
        "exclusive-writers",
        "single-uid-gate",
        "replacement-gate",
        "collector-readonly",
        "forbidden-priority",
        "forbidden-sidecar",
    },
}


def validate_native_pair(kind, item, target):
    values = []
    for label, uid in (("control", item["control_uid"]), ("isolated", item["subject_uid"])):
        receipt = item[label].get("native_receipt")
        if not isinstance(receipt, dict) or set(receipt) != {"request", "response"}:
            raise ValueError("native request/response custody required")
        request, response = receipt["request"], receipt["response"]
        if (
            not isinstance(request, dict)
            or not isinstance(response, dict)
            or request.get("case") != item["case"]
            or request.get("subject_uid") != uid
            or request.get("node_uid") != item["node_uid"]
        ):
            raise ValueError("native observation attribution mismatch")
        values.append(response)
    control, isolated = values
    if kind == "cni":
        valid = (
            control.get("connected") is True
            and type(control.get("bytes_received")) is int
            and control["bytes_received"] > 0
            and isolated.get("connected") is False
            and isolated.get("errno") in {"ETIMEDOUT", "EACCES", "EPERM"}
            and type(isolated.get("elapsed_ms")) is int
            and 1 <= isolated["elapsed_ms"] <= 5000
        )
    elif kind == "admission":
        valid = (
            control.get("http_status") == 201
            and isolated.get("http_status") == 403
            and isolated.get("reason") == "Forbidden"
        )
    elif kind == "quota":
        valid = (
            all(type(v.get(key)) is int for v in values for key in ("usage", "limit"))
            and 0 < control["limit"] == isolated["limit"]
            and control["usage"] > control["limit"] >= isolated["usage"] >= 0
        )
    elif item["case"] == "terminated-uid":
        valid = (
            control.get("running_pids")
            and isolated.get("running_pids") == []
            and isolated.get("pod_uid") == item["subject_uid"]
            and isolated.get("termination_observed_at") == item["observed_at"]
        )
    else:
        valid = (
            all(
                type(v.get(key)) is int and v[key] > 0
                for v in values
                for key in ("pid_namespace_inode", "mount_namespace_inode")
            )
            and control["pid_namespace_inode"] != isolated["pid_namespace_inode"]
            and control["mount_namespace_inode"] != isolated["mount_namespace_inode"]
            and isolated.get("runtime_handler") == target.runtime_handler
        )
    if not valid:
        raise ValueError("native observations do not establish the required boundary")


class SignedSandboxWitnessVerifier:
    """Existing HMAC witness custody codec, with explicit Sandbox-specific scope.

    Trusted witness services must retain their native observations and emit this
    profile, not translate a Job/availability assertion into a Sandbox pass.
    HMAC authenticates that trusted custody; it cannot prove sensor independence.
    """

    def __init__(self, config):
        self.config = config

    def __call__(self, target):
        if target != self.config.target or set(self.config.witnesses) != set(KINDS):
            raise ValueError("witness target/kinds mismatch")
        now, digests, finishes, identities, keys = datetime.now(UTC), {}, [], set(), set()
        for kind in KINDS:
            reference = self.config.witnesses[kind]
            key = private_bytes(reference.authentication_reference, 4096)
            encoded = private_bytes(reference.report_reference, 2 * 1024 * 1024)
            if hashlib.sha256(encoded).hexdigest() != reference.report_sha256:
                raise ValueError("witness reference digest changed")
            import json

            report = open_report(json.loads(encoded), key)
            safe_payload(report.model_dump(mode="json"), secrets=(key,))
            if (
                report.source != "independent-daemon"
                or report.state != "observed"
                or report.witness != reference.pin
                or report.request.kind != kind
                or reference.pin.kind != kind
                or reference.pin.namespace == target.namespace
                or report.request.org_id != target.org_id
                or report.request.project_id != target.project_id
                or report.request.namespace != target.namespace
                or report.request.namespace_uid != target.namespace_uid
                or report.request_digest != canonical_digest(report.request.model_dump(mode="json"))
            ):
                raise ValueError("independent witness authority mismatch")
            inputs = report.request.inputs
            if (
                inputs.get("profile") != "agent-sandbox-network-none-v1"
                or inputs.get("target_scope") != scope_digest(target)
                or inputs.get("upstream_commit") != target.upstream_commit
            ):
                raise ValueError("generic report does not cover direct Sandbox scope")
            finish = datetime.fromisoformat(report.completed_at.replace("Z", "+00:00"))
            start = datetime.fromisoformat(report.started_at.replace("Z", "+00:00"))
            deadline = datetime.fromisoformat(report.request.deadline.replace("Z", "+00:00"))
            if (
                not start <= finish <= now < deadline
                or now - finish > timedelta(minutes=5)
                or abs((finish - start).total_seconds() * 1000 - report.monotonic_elapsed_ms) > 250
            ):
                raise ValueError("independent witness expired or contradictory")
            raw = report.native.get("sandbox_observations")
            if (
                not isinstance(raw, list)
                or {item.get("case") for item in raw} != CASES[kind]
                or len(raw) != len(CASES[kind])
            ):
                raise ValueError("complete native Sandbox observations required")
            resource_uids = {item.uid for item in report.resources}
            for item in raw:
                if (
                    set(item)
                    != {
                        "case",
                        "subject_uid",
                        "control_uid",
                        "node_uid",
                        "observed_at",
                        "control",
                        "isolated",
                    }
                    or item["subject_uid"] not in resource_uids
                    or item["control_uid"] not in resource_uids
                    or item["subject_uid"] == item["control_uid"]
                    or item["node_uid"] != reference.pin.node_uid
                    or not start
                    <= datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
                    <= finish
                    or not isinstance(item["control"], dict)
                    or not isinstance(item["isolated"], dict)
                    or item["control"].get("outcome") != "observed"
                    or item["isolated"].get("outcome") != "enforced"
                    or not item["control"].get("native_receipt")
                    or not item["isolated"].get("native_receipt")
                ):
                    raise ValueError("native control/restricted observations missing")
                validate_native_pair(kind, item, target)
            identities.add(reference.pin.identity)
            keys.add(hashlib.sha256(key).hexdigest())
            finishes.append(finish)
            digests[kind] = reference.report_sha256
        if len(identities) != 4 or len(keys) != 4:
            raise ValueError("independent witness identities/keys must be distinct")
        summary = {
            "observed_at": min(finishes).isoformat(),
            "org_id": target.org_id,
            "project_id": target.project_id,
            "cluster_uid": target.cluster_uid,
            "namespace_uid": target.namespace_uid,
            "runtime_class_uid": target.runtime_class_uid,
            "runtime_handler": target.runtime_handler,
            "network": "ingress-egress-denied-with-positive-controls",
            "runtime": "independently-measured-isolation",
            "admission": "exclusive-writers-fixed-profile-one-uid-release",
            "signed_report_digests": digests,
        }
        if canonical_digest(summary) != target.witness_sha256:
            raise ValueError("approved witness receipt checksum mismatch")
        return summary


def load_agent_sandbox_backend(config_reference, approved_sha256):
    from tessera_worker.agent_sandbox import authenticated_backend

    try:
        if not config_reference or not approved_sha256:
            raise ValueError("config and its approval digest are both required")
        encoded = private_bytes(config_reference, 128 * 1024)
        if hashlib.sha256(encoded).hexdigest() != approved_sha256:
            raise ValueError("operator config digest mismatch")
        config = SandboxOperatorConfig.model_validate_json(encoded)
        token = private_bytes(config.token_reference, 8192)
        if not token or any(value in token for value in (b"\n", b"\r")):
            raise ValueError("projected token reference unavailable")
        verifier = SignedSandboxWitnessVerifier(config)
        verifier(config.target)  # Fail before serving/provider writes.
        backend = authenticated_backend(
            config.target, config.ca_reference, config.token_reference, verifier
        )
        backend.operator_config = config
        return backend
    except Exception:
        raise ValueError(
            "Sandbox operator configuration or authenticated witness rejected"
        ) from None
