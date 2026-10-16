"""Closed, metadata-only administrative commands. No secret value/locator fields."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,128}$")]
MetadataId = Annotated[str, StringConstraints(pattern=r"^admin_[a-f0-9]{32}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Provider = Literal["none", "kubernetes", "local_fixture", "salad", "ai_gateway", "artifact_store"]
Action = Literal["configuration.read", "configuration.propose", "readiness.read"]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Configuration(Closed):
    kind: Literal["configuration"] = "configuration"
    component: Literal[
        "control_plane", "execution_target", "artifact_store", "monitor", "model_gateway"
    ]
    provider: Provider = "none"
    deployment: Literal["existing_local", "remote_https", "kubernetes_metadata"] = "existing_local"
    endpoint: Annotated[str, Field(max_length=300)] | None = None
    namespace: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")] | None = None
    salad_organization: (
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")] | None
    ) = None
    salad_project: (
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")] | None
    ) = None
    salad_proposed_project: (
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")] | None
    ) = None
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    cluster_identity: Digest | None = None
    ca_digest: Digest | None = None
    kubeconfig_secret_id: MetadataId | None = None
    management_secret_id: MetadataId | None = None
    gateway_secret_id: MetadataId | None = None
    artifact_secret_id: MetadataId | None = None
    image_digest: Digest | None = None
    artifact_profile_digest: Digest | None = None
    monitor_profile_digest: Digest | None = None
    network_policy: Literal["networkless", "restricted_unverified", "public_egress_unverified"] = (
        "networkless"
    )
    custody_policy: Literal["quarantine_only"] = "quarantine_only"
    data_policy: Literal["public_released_only"] = "public_released_only"
    max_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 60
    max_cpu: Annotated[int, Field(strict=True, ge=1, le=64)] = 1
    max_gpu: Annotated[int, Field(strict=True, ge=0, le=8)] = 0
    max_memory_mib: Annotated[int, Field(strict=True, ge=128, le=262144)] = 512
    max_output_bytes: Annotated[int, Field(strict=True, ge=0, le=1073741824)] = 0

    @model_validator(mode="after")
    def bindings(self):
        providers = {
            "control_plane": {"none", "kubernetes"},
            "execution_target": {"kubernetes", "local_fixture", "salad"},
            "artifact_store": {"artifact_store"},
            "monitor": {"none", "kubernetes"},
            "model_gateway": {"ai_gateway"},
        }
        if self.provider not in providers[self.component]:
            raise ValueError("component/provider mismatch")
        if (
            self.salad_organization
            or self.salad_project
            or self.salad_proposed_project
            or self.country
        ) and self.provider != "salad":
            raise ValueError("Salad account metadata requires Salad")
        if self.endpoint is not None:
            value = self.endpoint
            parsed = urlsplit(value)
            if (
                any(c.isspace() or ord(c) < 32 for c in value)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or "?" in value
                or "#" in value
                or not parsed.hostname
                or parsed.netloc.endswith(":")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or not (
                    parsed.scheme == "https"
                    or (
                        parsed.scheme == "http"
                        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                        and self.deployment == "existing_local"
                    )
                )
            ):
                raise ValueError("invalid origin")
        if self.component != "control_plane" and self.endpoint is not None:
            raise ValueError("provider endpoints are not configurable")
        if self.management_secret_id and self.provider != "salad":
            raise ValueError("management binding requires Salad")
        if self.gateway_secret_id and self.provider != "ai_gateway":
            raise ValueError("gateway binding requires a separate gateway profile")
        if self.kubeconfig_secret_id and self.provider != "kubernetes":
            raise ValueError("kubeconfig binding requires Kubernetes")
        return self


class SecretMetadata(Closed):
    kind: Literal["secret"] = "secret"
    backend: Literal["fake", "production_unavailable", "os_keychain"] = "production_unavailable"
    purpose: Literal["salad_management", "ai_gateway", "kubeconfig", "artifact_store", "monitor"]
    provider: Provider
    audience: Literal["server_proxy", "gateway", "operator", "artifact_service", "monitor"]
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def binding(self):
        bindings = {
            "salad_management": ("salad", "server_proxy"),
            "ai_gateway": ("ai_gateway", "gateway"),
            "kubeconfig": ("kubernetes", "operator"),
            "artifact_store": ("artifact_store", "artifact_service"),
            "monitor": ("none", "monitor"),
        }
        if bindings[self.purpose] != (self.provider, self.audience):
            raise ValueError("purpose/provider/audience mismatch")
        return self


class Delegation(Closed):
    kind: Literal["delegation"] = "delegation"
    subject_id: Identifier
    subject_role: Literal["human", "operator", "agent", "worker", "proxy", "monitor"]
    target_id: MetadataId
    provider: Provider
    resource: Literal["configuration"] = "configuration"
    actions: Annotated[list[Action], Field(min_length=1, max_length=3)]
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def unique(self):
        if len(self.actions) != len(set(self.actions)):
            raise ValueError("duplicate actions")
        return self


Spec = Annotated[Configuration | SecretMetadata | Delegation, Field(discriminator="kind")]


class AdminCreate(Closed):
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=0)] = 0
    spec: Spec


class AdminCommand(Closed):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    action: Literal[
        "approve", "reject", "revoke", "expire", "delete", "rotate", "verify", "enable", "revise"
    ]
    configuration: Configuration | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def fields_for_action(self):
        if (self.action == "revise") != (self.configuration is not None):
            raise ValueError("revision requires configuration only")
        if (self.action == "rotate") != (self.expires_at is not None):
            raise ValueError("rotation requires expiry only")
        return self


class AdminRecord(Closed):
    id: Identifier
    project_id: Identifier
    revision: int
    spec: Spec
    state: Literal[
        "proposed", "approved", "enrolling", "rejected", "active", "revoked", "expired", "deleted"
    ]
    proposed_by: Identifier
    approved_by: Identifier | None
    created_at: str
    updated_at: str
    version: int
    versions: list[dict]
    last_used_at: None = None
    health: Literal[
        "unknown", "synthetic_only", "available", "unavailable", "expired", "revoked", "deleted"
    ]
    reference: (
        Annotated[str, StringConstraints(pattern=r"^credential-ref:[a-f0-9]{32}$")] | None
    ) = None
    enrollment_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")] | None = None
    enrollment_deadline: str | None = None
    cleanup_pending: bool = False
    custody: Literal["macos_keychain", "development_memory_NON_PRODUCTION"] | None = None
    verification: Literal["unknown", "blocked", "stale"]
    verified_revision: int | None = None
    verified_secret_versions: dict[str, int] = Field(default_factory=dict)
    enabled: Literal[False] = False
    effective: bool = False


class SaladSetupView(Closed):
    record: AdminRecord
    authority: Literal["root", "delegated"]
    state: Literal["project_creation_requires_portal", "project_selected_unverified"]
    proposed_project: str
    project_created: bool | None
    portal_url: Literal["https://docs.salad.com/general/tutorials/account-setup"]
    provider_validation: Literal["not_performed_by_control"] = "not_performed_by_control"
    deployment: Literal["unverified"] = "unverified"
    guardian_custody: Literal["unverified"] = "unverified"
    billing: Literal["no_cash_hard_cap_unverified"] = "no_cash_hard_cap_unverified"
    control_integration: Literal["metadata_only_connected"] = "metadata_only_connected"
    execution_enabled: Literal[False] = False


class AdminWorkspace(Closed):
    records: list[AdminRecord]
    next_offset: int | None
    held: bool
    root_admin: bool
    control_status: Literal["ready", "held"]
    observed_at: str
    provider_enabled: Literal[False] = False
    production_secret_backend: Literal["unavailable"] = "unavailable"
    invitations: Literal["unavailable_identity_enrollment_required"] = (
        "unavailable_identity_enrollment_required"
    )
    runtime_delegation: Literal["unavailable"] = "unavailable"
