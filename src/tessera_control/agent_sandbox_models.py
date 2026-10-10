"""Explicit approved pins for the network-none direct Sandbox profile."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)]
Identity = Annotated[str, Field(min_length=1, max_length=200)]

UPSTREAM_COMMIT = "cd0761d5001baaf293460cd382ee318744d6b249"
UPSTREAM_VERSION = "v1.0.6"
API_VERSION = "agents.x-k8s.io/v1beta1"
GATE = "tessera.dev/authority"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SandboxTarget(FrozenModel):
    org_id: Identity
    project_id: Identity
    kubeconfig: Identity
    kubeconfig_sha256: Digest
    context: Identity
    server: Identity
    server_sha256: Digest
    ca_sha256: Digest
    cluster_uid: Identity
    namespace: Name
    namespace_uid: Identity
    runtime_class: Name
    runtime_class_uid: Identity
    runtime_handler: Literal["runsc", "kata", "kata-qemu", "kata-clh"]
    controller_namespace: Name
    controller_deployment: Name
    controller_uid: Identity
    controller_image: Annotated[str, Field(pattern=r"^.+@sha256:[0-9a-f]{64}$")]
    crd_uid: Identity
    crd_spec_sha256: Digest
    # Operator-controlled, independent measured evidence, not an available flag.
    witness_name: Name
    witness_uid: Identity
    witness_sha256: Digest
    upstream_commit: Literal["cd0761d5001baaf293460cd382ee318744d6b249"] = UPSTREAM_COMMIT

    @field_validator("kubeconfig")
    @classmethod
    def absolute_reference(cls, value):
        if not value.startswith("/"):
            raise ValueError("explicit absolute kubeconfig reference required")
        return value

    @field_validator("server")
    @classmethod
    def https_origin(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("explicit HTTPS origin required")
        return value


class SandboxPlan(FrozenModel):
    target: SandboxTarget
    cpu_millicores: Annotated[int, Field(ge=100, le=2000)]
    memory_bytes: Annotated[int, Field(ge=67108864, le=536870912)]
    storage_class: Name
    storage_bytes: Annotated[int, Field(ge=16777216, le=1073741824)]
    network: Literal["none"] = "none"


class SandboxCommand(FrozenModel):
    expected_epoch: Annotated[int, Field(ge=1, le=2**63 - 1)]


class SandboxCleanup(FrozenModel):
    manifest_id: Identity
    manifest_digest: Digest
