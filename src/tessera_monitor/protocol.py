"""Portable, bounded HMAC protocol. HMAC authenticates custody, not host integrity.

Keys are provisioned out of band to distinct authority/channel/evidence audiences;
they are never carried in documents. Remote transports additionally require mTLS.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_FRAME = 64 * 1024
ZERO = "0" * 64
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Category = Literal[
    "identity",
    "namespace",
    "network",
    "resources",
    "filesystem",
    "custody",
    "watchdog",
    "termination",
    "cleanup",
    "recovery",
]
CATEGORIES = (
    "identity",
    "namespace",
    "network",
    "resources",
    "filesystem",
    "custody",
    "watchdog",
    "termination",
    "cleanup",
    "recovery",
)


class Denied(ValueError):
    """Finite error text only; never echo validation input or upstream diagnostics."""


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def encode(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_FRAME:
        raise Denied("payload bound")
    return raw


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


class Workload(Frozen):
    # Container IDs / Kubernetes UID and resource-version / future provider allocation.
    uid: Digest
    image_digest: Digest
    instance_digest: Digest  # adapter-specific immutable creation/generation identity
    target_digest: Digest


class Limits(Frozen):
    runtime_seconds: int = Field(ge=1, le=120)
    lease_seconds: int = Field(ge=1, le=10)
    memory_bytes: int = Field(ge=1, le=512 * 1024 * 1024)
    cpu_millicores: int = Field(ge=1, le=1000)
    pids: int = Field(ge=1, le=128)
    filesystem_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    filesystem_inodes: int = Field(ge=1, le=4096)
    output_bytes: int = Field(ge=0, le=1024 * 1024)
    network: Literal["none"] = "none"


class Assignment(Frozen):
    version: Literal[1] = 1
    tenant: Name
    project: Name
    plan_digest: Digest
    package_digest: Digest
    operation: Name
    nonce: Digest
    monitor: Name
    worker: Name
    authority: Name
    adapter: Literal["docker-local-v1", "kubernetes-v1", "salad-v1", "fake-v1"]
    workload: Workload
    limits: Limits
    required: tuple[Category, ...] = CATEGORIES
    privilege_profile: Literal["docker-socket-procfs-read-v1", "disabled", "fake"]
    issued_ms: int = Field(ge=1, le=2**53)
    expires_ms: int = Field(ge=1, le=2**53)

    @model_validator(mode="after")
    def bounded(self):
        if not 0 < self.expires_ms - self.issued_ms <= self.limits.runtime_seconds * 1000:
            raise ValueError("assignment lifetime")
        if set(self.required) != set(CATEGORIES) or len(self.required) != len(CATEGORIES):
            raise ValueError("all evidence categories required")
        if len({self.monitor, self.worker, self.authority}) != 3:
            raise ValueError("separate principals required")
        return self


class Envelope(Frozen):
    payload: str = Field(max_length=MAX_FRAME)
    mac: Digest


def seal(value, key, domain):
    if type(key) is not bytes or len(key) != 32:
        raise Denied("invalid protocol key")
    raw = encode(value)
    result = Envelope(
        payload=raw.decode(),
        mac=hmac.new(key, b"tessera-monitor-v1\0" + domain + b"\0" + raw, "sha256").hexdigest(),
    )
    encode(result)
    return result


def open_envelope(envelope, model, key, domain):
    try:
        if type(key) is not bytes or len(key) != 32:
            raise ValueError
        envelope = Envelope.model_validate_json(encode(envelope))
        result = model.model_validate_json(envelope.payload)
        expected = seal(result, key, domain)
        if expected.payload != envelope.payload or not hmac.compare_digest(
            expected.mac, envelope.mac
        ):
            raise ValueError
        return result
    except Exception:
        raise Denied("invalid authenticated document") from None


class Pulse(Frozen):
    assignment: Digest
    nonce: Digest
    monitor: Name
    sequence: int = Field(ge=1, le=256)
    challenge: Digest
    action: Literal["challenge", "continue", "revoke"]
    issued_ms: int = Field(ge=1, le=2**53)


class Fact(Frozen):
    # No arbitrary log strings, paths, tokens, environment, certificates or commands.
    name: Literal[
        "container",
        "image",
        "instance",
        "running",
        "pid",
        "start_ticks",
        "net_inode",
        "mnt_inode",
        "pid_inode",
        "memory_max",
        "pids_max",
        "cpu_millicores",
        "readonly",
        "network_none",
        "uid",
        "exit_code",
        "absent",
        "native_proof",
        "privilege_profile",
        "writers_frozen",
        "bytes",
        "sha256",
        "receipt",
        "sequence",
        "elapsed_ms",
        "loopback_index",
        "loopback_flags",
        "ipv4_prefix",
        "ipv6_prefix",
        "route_count",
        "ipv4_positive",
        "ipv4_refused",
        "ipv4_unreachable",
        "ipv6_positive",
        "ipv6_refused",
        "ipv6_unreachable",
        "filesystem_bytes",
        "filesystem_inodes",
        "memory_swap_max",
    ]
    value: Digest | bool | int = Field(union_mode="left_to_right")


class Artifact(Frozen):
    sha256: Digest
    size: int = Field(ge=0, le=1024 * 1024)
    custody_receipt: Digest
    disposition: Literal["quarantined"] = "quarantined"


class Observation(Frozen):
    category: Category
    state: Literal["verified", "unknown", "failed", "not_applicable"]
    origin: Literal["adapter", "daemon-readback", "host-procfs", "namespace-probe", "journal"] = (
        "adapter"
    )
    facts: tuple[Fact, ...] = Field(default=(), max_length=64)
    artifact: Artifact | None = None

    @model_validator(mode="after")
    def evidence(self):
        if self.state == "verified" and not self.facts:
            raise ValueError("raw facts required")
        if self.artifact is not None and self.category != "custody":
            raise ValueError("artifact must link custody")
        if len({f.name for f in self.facts}) != len(self.facts):
            raise ValueError("duplicate facts")
        return self


class Receipt(Frozen):
    assignment: Digest
    nonce: Digest
    monitor: Name
    sequence: int = Field(ge=1, le=1024)
    previous: Digest
    observed_ms: int = Field(ge=1, le=2**53)
    elapsed_ms: int = Field(ge=0, le=2**53)
    kind: Literal[
        "armed",
        "heartbeat",
        "observation",
        "stop_intent",
        "cleanup_intent",
        "unknown",
        "recovery",
        "terminal",
    ]
    source: Literal["execution-monitor", "fake"]
    observations: tuple[Observation, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def unique_categories(self):
        if len({o.category for o in self.observations}) != len(self.observations):
            raise ValueError("duplicate observation category")
        return self


def bound(receipt, assignment):
    if (receipt.assignment, receipt.nonce, receipt.monitor) != (
        digest(assignment),
        assignment.nonce,
        assignment.monitor,
    ):
        raise Denied("assignment binding mismatch")
