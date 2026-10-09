"""Frozen ordinary Docker package, distinct from local fixture/Kubernetes packages."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .execution_contracts import ExecutionEnvelope, TargetBinding
from .outputs import OutputCollectionError, validate_output_paths

PROFILE = "ordinary-docker-v1"
ADAPTER = "docker-container-v1"
DIGEST = r"^[a-f0-9]{64}$"
IDENTIFIER = r"^[a-zA-Z0-9_-]{1,128}$"


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DockerTarget(Frozen):
    daemon_id: str = Field(min_length=1, max_length=128)
    endpoint_digest: str = Field(pattern=DIGEST)
    image: str = Field(max_length=256)
    os: Literal["linux"] = "linux"
    architecture: Literal["amd64"] = "amd64"
    guard_version: Literal["ordinary-docker-guard-v1"] = "ordinary-docker-guard-v1"

    @field_validator("image")
    @classmethod
    def pinned_image(cls, value):
        if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}", value):
            raise ValueError("immutable repository digest required")
        return value

    @property
    def binding(self):
        return TargetBinding(ADAPTER, PROFILE, digest(self.model_dump(mode="json")))


class DockerPackage(Frozen):
    version: Literal[1] = 1
    adapter_version: Literal["docker-container-v1"] = ADAPTER
    profile_version: Literal["ordinary-docker-v1"] = PROFILE
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    org_id: str = Field(pattern=IDENTIFIER)
    project_id: str = Field(pattern=IDENTIFIER)
    task_id: str = Field(pattern=IDENTIFIER)
    admitted_attempt_id: str = Field(pattern=IDENTIFIER)
    agent_principal_id: str = Field(pattern=IDENTIFIER)
    worker_principal_id: str = Field(pattern=IDENTIFIER)
    grant_id: str = Field(pattern=IDENTIFIER)
    lease_id: str = Field(pattern=IDENTIFIER)
    epoch: int = Field(ge=1, le=2**63 - 1)
    target_digest: str = Field(pattern=DIGEST)
    plan_digest: str = Field(pattern=DIGEST)
    hard_expires_at: datetime
    image: str
    argv: tuple[str, ...] = Field(min_length=1, max_length=32)
    cpu_millicores: int = Field(ge=100, le=2000)
    memory_bytes: int = Field(ge=64 * 1024**2, le=512 * 1024**2)
    pids_limit: int = Field(ge=16, le=128)
    scratch_bytes: int = Field(ge=1024**2, le=16 * 1024**2)
    max_output_bytes: int = Field(ge=1, le=16 * 1024**2)
    output_paths: tuple[str, ...] = Field(min_length=1, max_length=128)
    network: Literal["none"] = "none"

    @field_validator("hard_expires_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware hard expiry required")
        return value.astimezone(UTC)

    @field_validator("image")
    @classmethod
    def pinned(cls, value):
        return DockerTarget.pinned_image(value)

    @field_validator("argv")
    @classmethod
    def command(cls, value):
        if any(not v or "\0" in v or len(v.encode()) > 4096 for v in value):
            raise ValueError("bounded argv required")
        if sum(len(v.encode()) for v in value) > 16384:
            raise ValueError("aggregate argv byte limit exceeded")
        if PurePosixPath(value[0]).name in {"sh", "bash", "dash", "zsh", "fish", "docker", "ssh"}:
            raise ValueError("shell/host proxy commands are not supported")
        return value

    @field_validator("output_paths")
    @classmethod
    def paths(cls, value):
        try:
            paths = validate_output_paths(value)
        except OutputCollectionError:
            raise ValueError("invalid declared Docker output path") from None
        if any(len(("outputs/" + path).encode()) > 100 for path in paths):
            raise ValueError("USTAR output path limit exceeded")
        return paths

    @property
    def package_digest(self):
        return digest(self.model_dump(mode="json"))

    def envelope(self, target: DockerTarget):
        if self.target_digest != target.binding.identity_digest or self.image != target.image:
            raise ValueError("Docker package target mismatch")
        return ExecutionEnvelope(
            self.operation_id,
            self.package_digest,
            target.binding,
            self.lease_id,
            self.epoch,
            self.hard_expires_at,
            self,
        )

    @property
    def name(self):
        return "tessera-docker-" + self.operation_id

    @property
    def labels(self):
        return {
            "tessera." + key: str(value)
            for key, value in {
                "profile": PROFILE,
                "operation": self.operation_id,
                "package": self.package_digest,
                "target": self.target_digest,
                "org": self.org_id,
                "project": self.project_id,
                "task": self.task_id,
                "attempt": self.admitted_attempt_id,
                "agent": self.agent_principal_id,
                "worker": self.worker_principal_id,
                "grant": self.grant_id,
                "lease": self.lease_id,
                "epoch": self.epoch,
            }.items()
        }

    def container_config(self):
        guard = {
            "version": "ordinary-docker-guard-v1",
            "operation_id": self.operation_id,
            "argv": self.argv,
            "hard_expires_at": self.hard_expires_at.isoformat(),
        }
        encoded = base64.b64encode(json.dumps(guard, separators=(",", ":")).encode()).decode()
        output_size = max(1024**2, self.max_output_bytes + 128 * 4096)
        return {
            "Image": self.image,
            "User": "65532:65532",
            "WorkingDir": "/outputs",
            "Entrypoint": ["python", "-m", "tessera_worker.ordinary_docker_guard"],
            "Cmd": [encoded],
            "Env": [],
            "Labels": self.labels,
            "AttachStdout": False,
            "AttachStderr": False,
            "Tty": False,
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "Privileged": False,
                "CapDrop": ["ALL"],
                "CapAdd": [],
                "SecurityOpt": ["no-new-privileges:true"],
                "NanoCpus": self.cpu_millicores * 1_000_000,
                "Memory": self.memory_bytes,
                "MemorySwap": self.memory_bytes,
                "PidsLimit": self.pids_limit,
                "RestartPolicy": {"Name": "no"},
                "AutoRemove": False,
                "Init": False,
                "Binds": [],
                "Mounts": [],
                "Devices": [],
                "PidMode": "",
                "IpcMode": "private",
                "PortBindings": {},
                "Tmpfs": {
                    "/scratch": (
                        f"rw,noexec,nosuid,nodev,size={self.scratch_bytes},"
                        "nr_inodes=512,uid=65532,gid=65532,mode=0700"
                    ),
                    "/outputs": (
                        f"rw,noexec,nosuid,nodev,size={output_size},"
                        "nr_inodes=512,uid=65532,gid=65532,mode=0700"
                    ),
                },
                "LogConfig": {"Type": "none"},
            },
        }


class DockerLease(Frozen):
    id: str
    grant_id: str
    worker_principal_id: str
    epoch: int = Field(ge=1)
    expires_at: datetime
    hard_expires_at: datetime

    @model_validator(mode="after")
    def deadlines(self):
        if any(
            v.tzinfo is None or v.utcoffset() is None
            for v in (self.expires_at, self.hard_expires_at)
        ):
            raise ValueError("aware lease deadlines required")
        if self.expires_at > self.hard_expires_at:
            raise ValueError("lease exceeds hard deadline")
        return self
