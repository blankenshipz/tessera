"""Strict experiment-worker protocol objects; these are not yet HTTP route bindings."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .execution_contracts import Observation
from .ordinary_docker_contract import (
    DIGEST,
    IDENTIFIER,
    DockerLease,
    DockerPackage,
    DockerTarget,
    Frozen,
    digest,
)
from .outputs import CollectedOutput


class DockerClaim(Frozen):
    dispatch_id: str = Field(pattern=IDENTIFIER)
    package: DockerPackage
    lease: DockerLease

    @model_validator(mode="after")
    def pins(self):
        package, lease = self.package, self.lease
        if (
            lease.id,
            lease.grant_id,
            lease.worker_principal_id,
            lease.epoch,
            lease.hard_expires_at,
        ) != (
            package.lease_id,
            package.grant_id,
            package.worker_principal_id,
            package.epoch,
            package.hard_expires_at,
        ):
            raise ValueError("claim lease/package pins mismatch")
        return self


class NativeDockerReport(Frozen):
    provider: Literal["docker"] = "docker"
    dispatch_id: str = Field(pattern=IDENTIFIER)
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    admitted_attempt_id: str = Field(pattern=IDENTIFIER)
    lease_id: str = Field(pattern=IDENTIFIER)
    expected_epoch: int = Field(ge=1)
    package_digest: str = Field(pattern=DIGEST)
    target_digest: str = Field(pattern=DIGEST)
    container_id: str | None = Field(default=None, pattern=DIGEST)
    state: Literal["pending", "running", "succeeded", "failed", "unknown"]
    observed_at: datetime
    termination_confirmed: bool
    stop_requested: bool
    exit_code: int | None = Field(default=None, strict=True, ge=0, le=255)

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware observation timestamp required")
        return value

    @model_validator(mode="after")
    def evidence(self):
        if (
            self.state in {"pending", "running", "succeeded", "failed"}
            and self.container_id is None
        ):
            raise ValueError("native state requires actual container identity")
        if self.state in {"succeeded", "failed"} and not self.termination_confirmed:
            raise ValueError("terminal state requires actual termination")
        if self.state in {"pending", "running"} and self.termination_confirmed:
            raise ValueError("running/pending is not termination")
        return self

    def require_claim(self, claim):
        p = claim.package
        expected = {
            "dispatch_id": claim.dispatch_id,
            "operation_id": p.operation_id,
            "admitted_attempt_id": p.admitted_attempt_id,
            "lease_id": p.lease_id,
            "expected_epoch": p.epoch,
            "package_digest": p.package_digest,
            "target_digest": p.target_digest,
        }
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise ValueError("report claim identity mismatch")
        return self

    @classmethod
    def from_observation(cls, claim: DockerClaim, observation: Observation, *, stopped=False):
        p = claim.package
        if observation.handle.operation_id != p.operation_id or (
            observation.handle.target.identity_digest != p.target_digest
        ):
            raise ValueError("observation operation/target mismatch")
        return cls(
            dispatch_id=claim.dispatch_id,
            operation_id=p.operation_id,
            admitted_attempt_id=p.admitted_attempt_id,
            lease_id=p.lease_id,
            expected_epoch=p.epoch,
            package_digest=p.package_digest,
            target_digest=p.target_digest,
            container_id=observation.handle.provider_id,
            state=observation.state,
            observed_at=observation.observed_at,
            termination_confirmed=observation.termination_confirmed,
            stop_requested=stopped,
        )


class NativeDockerOutput(Frozen):
    path: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=DIGEST)
    size: int = Field(ge=0, le=16 * 1024**2)


class NativeDockerManifest(Frozen):
    provider: Literal["docker"] = "docker"
    dispatch_id: str = Field(pattern=IDENTIFIER)
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    admitted_attempt_id: str = Field(pattern=IDENTIFIER)
    package_digest: str = Field(pattern=DIGEST)
    target_digest: str = Field(pattern=DIGEST)
    container_id: str = Field(pattern=DIGEST)
    lease_id: str = Field(pattern=IDENTIFIER)
    epoch: int = Field(ge=1)
    agent_principal_id: str = Field(pattern=IDENTIFIER)
    worker_principal_id: str = Field(pattern=IDENTIFIER)
    grant_id: str = Field(pattern=IDENTIFIER)
    outputs: tuple[NativeDockerOutput, ...] = Field(min_length=1, max_length=128)

    @classmethod
    def from_capture(cls, claim, metadata, outputs: tuple[CollectedOutput, ...]):
        value = cls.model_validate_json(json.dumps({**metadata, "dispatch_id": claim.dispatch_id}))
        p = claim.package
        expected = {
            "operation_id": p.operation_id,
            "admitted_attempt_id": p.admitted_attempt_id,
            "package_digest": p.package_digest,
            "target_digest": p.target_digest,
            "lease_id": p.lease_id,
            "epoch": p.epoch,
            "agent_principal_id": p.agent_principal_id,
            "worker_principal_id": p.worker_principal_id,
            "grant_id": p.grant_id,
        }
        if any(getattr(value, key) != item for key, item in expected.items()):
            raise ValueError("native output attribution mismatch")
        declared = {item.path: item for item in value.outputs}
        if len(declared) != len(value.outputs) or set(declared) != set(p.output_paths):
            raise ValueError("native output declaration mismatch")
        if (
            len(outputs) != len(declared)
            or sum(len(o.content) for o in outputs) > p.max_output_bytes
        ):
            raise ValueError("native output byte/count bound exceeded")
        for output in outputs:
            item = declared.get(output.relative_path)
            if (
                item is None
                or item.size != len(output.content)
                or item.sha256 != output.sha256
                or (hashlib.sha256(output.content).hexdigest() != item.sha256)
            ):
                raise ValueError("native output bytes/digest mismatch")
        return value

    @property
    def metadata_digest(self):
        return digest(self.model_dump(mode="json"))


class DockerDeliveryAck(Frozen):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    key: str = Field(min_length=1, max_length=200)
    payload_digest: str = Field(pattern=DIGEST)
    manifest_id: str | None = Field(default=None, pattern=IDENTIFIER)
    # Acknowledged control custody is quarantine, never scientific acceptance.
    custody: Literal["quarantined"] | None = None


class DockerControlReadback(Frozen):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    container_id: str | None = Field(default=None, pattern=DIGEST)
    desired: Literal["run", "cancel_requested"]
    acknowledgements: tuple[DockerDeliveryAck, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def matching_acknowledgements(self):
        if any(ack.operation_id != self.operation_id for ack in self.acknowledgements):
            raise ValueError("readback acknowledgement operation mismatch")
        if len({ack.key for ack in self.acknowledgements}) != len(self.acknowledgements):
            raise ValueError("readback acknowledgement key replay")
        return self


class DockerWorkerBackend(Protocol):
    """Authenticated worker-only bounded calls; never an owner-token proxy.

    Implementations recheck the principal for every call. Current authority must
    validate live grant/task/agent/credential/epoch/target/budget predicates. The
    source seam intentionally does not guess a parallel backend's HTTP endpoints.
    """

    def heartbeat(self, worker_id: str, key: str, *, timeout_seconds: float) -> datetime: ...
    def claim(
        self, worker_id: str, target: DockerTarget, key: str, *, timeout_seconds: float
    ) -> DockerClaim | None: ...
    def current_authority(self, claim: DockerClaim, *, timeout_seconds: float) -> DockerLease: ...
    def renew(self, claim: DockerClaim, key: str, *, timeout_seconds: float) -> DockerLease: ...
    def report(
        self, claim: DockerClaim, value: NativeDockerReport, key: str, *, timeout_seconds: float
    ) -> DockerDeliveryAck: ...
    def output(
        self,
        claim: DockerClaim,
        value: NativeDockerManifest,
        outputs: tuple[CollectedOutput, ...],
        key: str,
        *,
        timeout_seconds: float,
    ) -> DockerDeliveryAck: ...
    def cancel(
        self, claim: DockerClaim, value: NativeDockerReport, key: str, *, timeout_seconds: float
    ) -> DockerDeliveryAck: ...
    def read(self, claim: DockerClaim, *, timeout_seconds: float) -> DockerControlReadback: ...
