"""Strict, secret-free declarations for optional fenced artifact/package work."""

from __future__ import annotations

import ipaddress
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .credential_lease_models import RenewCredentialAccessLease

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Positive = Annotated[int, Field(ge=1, le=2**53 - 1)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Destination(Strict):
    host: Annotated[
        str, StringConstraints(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$", max_length=253)
    ]
    port: Annotated[int, Field(ge=1, le=65535)]

    @field_validator("host")
    @classmethod
    def public_host(cls, value: str) -> str:
        if "." not in value or value.endswith((".localhost", ".local", ".internal")):
            raise ValueError("public, fully qualified destinations are required")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return value
        if not public_address(address):
            raise ValueError("private, metadata and special-use addresses are forbidden")
        return value


def public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global and not (address.is_multicast or address.is_reserved)


def observed_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in value
        or "\\" in value
        or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ValueError("canonical HTTPS URLs without credentials, queries or traversal required")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid port")
    if parsed.netloc != parsed.hostname + (f":{parsed.port}" if parsed.port else ""):
        raise ValueError("noncanonical authority")
    return value


def artifact_url(value: str) -> str:
    observed_url(value)
    parsed = urlsplit(value)
    Destination(host=parsed.hostname, port=parsed.port or 443)
    return value


def fenced_root(value: str) -> str:
    path = PurePosixPath(value)
    if (
        str(path) != value
        or any(ord(c) < 32 or ord(c) >= 127 for c in value)
        or "\\" in value
        or ".." in path.parts
        or not any(
            path.is_relative_to(base) for base in ("/workspace/artifacts", "/workspace/packages")
        )
    ):
        raise ValueError("only canonical sandbox artifact/package roots are supported")
    return value


class ObservedAction(Strict):
    """Secret-free observations may describe prohibited destinations or roots.

    They are evidence, never declarations or authority. Preserve violations rather
    than requiring a worker to relabel them as approved effects.
    """

    id: Identifier
    kind: Literal["model", "data", "package"]
    name: Identifier
    version: Identifier
    url: Annotated[str, Field(max_length=2048)]
    sha256: Digest
    max_bytes: Annotated[int, Field(ge=1, le=2**40)]
    max_seconds: Annotated[int, Field(ge=1, le=3600)]
    filesystem_root: Annotated[str, Field(max_length=512)]
    manager: Literal["pip", "uv", "conda"] | None = None
    channel: Annotated[str, Field(max_length=2048)] | None = None
    # The pinned lock manifest must enumerate ALL transitive artifacts, versions,
    # and hashes. No dependency discovery or arbitrary shell command is admitted.
    lockfile_sha256: Digest | None = None
    credential_ref: Identifier | None = None
    expected_size: Annotated[int, Field(ge=0, le=16 * 1024 * 1024)] | None = None
    content_type: (
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$", max_length=128)]
        | None
    ) = None

    _observed_url = field_validator("url")(observed_url)

    @field_validator("channel")
    @classmethod
    def observed_channel(cls, value):
        return observed_url(value) if value is not None else None

    @field_validator("filesystem_root")
    @classmethod
    def printable_root(cls, value):
        if not value or any(ord(c) < 32 or ord(c) >= 127 for c in value):
            raise ValueError("root must be a printable path")
        return value


class ArtifactAction(ObservedAction):
    _url = field_validator("url")(artifact_url)
    _root = field_validator("filesystem_root")(fenced_root)

    @model_validator(mode="after")
    def package_contract(self):
        if self.kind == "package":
            if not self.manager or not self.channel or not self.lockfile_sha256:
                raise ValueError("packages require manager, channel and pinned lockfile")
            artifact_url(self.channel)
        elif any(value is not None for value in (self.manager, self.channel, self.lockfile_sha256)):
            raise ValueError("install fields are only valid for packages")
        return self


class PackageChannel(Strict):
    manager: Literal["pip", "uv", "conda"]
    url: Annotated[str, Field(max_length=2048)]

    _url = field_validator("url")(artifact_url)


class BrokerBasis(Strict):
    """Explicit human-approved identities, not inferred scientific provenance."""

    protocol_digest: Digest
    data_digest: Digest
    code_digest: Digest
    environment_digest: Digest


class CapabilityDeclaration(Strict):
    broker_basis: BrokerBasis | None = None
    environment_id: Identifier
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
    network: Annotated[list[Destination], Field(max_length=32)] = []
    package_channels: Annotated[list[PackageChannel], Field(max_length=16)] = []
    model_registries: Annotated[list[str], Field(max_length=16)] = []
    data_registries: Annotated[list[str], Field(max_length=16)] = []
    filesystem_roots: Annotated[list[str], Field(max_length=16)] = []
    credential_refs: Annotated[list[Identifier], Field(max_length=16)] = []
    max_bytes: Annotated[int, Field(ge=0, le=2**40)] = 0
    max_seconds: Annotated[int, Field(ge=0, le=86400)] = 0
    actions: Annotated[list[ArtifactAction], Field(max_length=128)] = []

    @model_validator(mode="after")
    def bounded_scope(self):
        for root in self.filesystem_roots:
            fenced_root(root)
        for registry in self.model_registries + self.data_registries:
            if len(registry) > 2048:
                raise ValueError("registry too long")
            artifact_url(registry)
        if len({a.id for a in self.actions}) != len(self.actions):
            raise ValueError("duplicate action ids")
        if (
            sum(a.max_bytes for a in self.actions) > self.max_bytes
            or sum(a.max_seconds for a in self.actions) > self.max_seconds
        ):
            raise ValueError("actions exceed aggregate byte/time budget")
        for action in self.actions:
            source = urlsplit(action.url)
            if Destination(host=source.hostname, port=source.port or 443) not in self.network:
                raise ValueError("destination/port not declared")
            if action.filesystem_root not in self.filesystem_roots:
                raise ValueError("filesystem root not declared")
            if action.credential_ref and action.credential_ref not in self.credential_refs:
                raise ValueError("credential reference not declared")
            if action.kind == "package":
                if (
                    PackageChannel(manager=action.manager, url=action.channel)
                    not in self.package_channels
                ):
                    raise ValueError("package manager/channel not declared")
                registries = [action.channel]
            else:
                registries = (
                    self.model_registries if action.kind == "model" else self.data_registries
                )
            if not any(
                action.url.startswith(registry.rstrip("/") + "/") for registry in registries
            ):
                raise ValueError("artifact is outside declared registry/channel")
        return self


class RequestCapabilities(Strict):
    expected_plan_revision: Positive
    expected_plan_digest: Digest
    declaration: CapabilityDeclaration


class ApproveCapabilities(RenewCredentialAccessLease):
    declaration_digest: Digest


class RevokeCapabilities(Strict):
    expected_revision: Positive


class AdmitCapability(Strict):
    execution_lease_id: Identifier
    expected_epoch: Positive
    environment_id: Identifier
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
    action: ArtifactAction
    credential_access_lease_id: Identifier | None = None


class InstalledPackage(Strict):
    name: Identifier
    version: Identifier
    artifact_sha256: Digest


class CapabilityReceipt(Strict):
    actual: ObservedAction
    bytes_downloaded: Annotated[int, Field(ge=0, le=2**53 - 1)]
    elapsed_ms: Annotated[int, Field(ge=0, le=2**53 - 1)]
    outcome: Literal["downloaded", "installed", "failed"]
    # Hash of the resulting artifact or installed inventory, even for a partial failure.
    result_sha256: Digest
    downloaded_sha256: Digest
    installed_packages: Annotated[list[InstalledPackage], Field(max_length=128)]
