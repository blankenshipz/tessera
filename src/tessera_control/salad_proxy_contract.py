"""Server-owned Salad proxy configuration. Never serialize this into a package/API."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .capability_models import Digest, Identifier
from .remote_execution_models import Frozen, TargetPolicy

MANAGEMENT_ENDPOINT = "https://api.salad.com/api/public"
SAFETY_PREREQUISITES = (
    "independent_monitor_conformance",
    "allocation_and_termination_conformance",
    "cost_and_unknown_credit_exposure_conformance",
    "external_checkpoint_and_artifact_custody_conformance",
    "provider_trust_and_data_release_review",
    "proxy_deployment_and_secret_backend_review",
    "separately_authorized_live_acceptance",
)


def safety_projection():
    return dict(
        transport="disabled",
        blocked_reasons=["salad_live_transport_disabled", "salad_safety_conformance_missing"],
        safety_prerequisites=list(SAFETY_PREREQUISITES),
        isolation_witness=False,
        autonomous_sandbox=False,
        usage=None,
        cost_cents=None,
    )


class ManagementCredential(Frozen):
    reference: Annotated[str, StringConstraints(pattern=r"^credential-ref:[a-z0-9-]{1,63}$")]
    version: int = Field(ge=1, le=2**31 - 1)
    purpose: Literal["salad_management"] = "salad_management"


class ProxyPolicy(Frozen):
    org_id: Identifier
    project_id: Identifier
    target: TargetPolicy
    management: ManagementCredential = Field(repr=False, exclude=True)
    endpoint: Literal["https://api.salad.com/api/public"] = MANAGEMENT_ENDPOINT
    live_enabled: Literal[False] = False
    follow_redirects: Literal[False] = False
    trust_env: Literal[False] = False
    request_timeout_seconds: Literal[2] = 2
    retries: Literal[0] = 0

    @model_validator(mode="after")
    def salad_only(self):
        s = self.target.selection
        if s.target != "salad_container" or s.salad is None or s.salad.execution is None:
            raise ValueError("exact Salad executable profile required")
        if s.salad.group == "default":
            raise ValueError("unique operation-bound group required")
        return self


Operation = Literal[
    "read_project",
    "read_quota",
    "read_availability",
    "create_stopped",
    "read_group",
    "read_instance",
    "read_events",
    "read_logs",
    "start",
    "stop",
    "delete",
]


class ProxyRequest(Frozen):
    """Internal request IDs only; authentication supplies worker/tenant out of band."""

    route_id: Identifier
    effect_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    operation: Operation


class ProxyResult(Frozen):
    status: Literal["blocked", "observed", "unknown", "pending"]
    reason: Literal[
        "salad_live_transport_disabled",
        "admission_denied",
        "credential_unavailable",
        "provider_unavailable",
        "malformed_response",
        "redirect_denied",
        "identity_mismatch",
        # A bound group (provider id known) whose exact read is a definitive 404.
        "group_gone",
        "reallocation_uncertain",
        "accepted_pending",
        "observed",
        # A create answered with a clean ProblemDetails 400/422 (status stays
        # "unknown"); the observation carries only a bounded provider_rejection.
        "provider_rejected_request",
        # A create that provably never left the process (status 0 in its rejection).
        "request_not_sent",
    ]
    observation: dict | None = None
    # Closed diagnostic detail of a failed read, separate from the closed ``reason`` (which
    # callers still branch on): a vocabulary token such as ``non_2xx_5xx`` or ``reset``,
    # and for an identity/format failure the NAME of the offending reviewed field. Never a
    # value, a body, a status text, or an organization/project/group path.
    detail: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")] = None
    field: Annotated[str | None, Field(pattern=r"^[a-z][a-z_.\-]{0,47}$")] = None
    # Which provider request failed: list, quotas, gpu_classes, availability, read_group,
    # read_instance, create or other (closed vocabulary; never a path).
    request_kind: (
        Literal[
            "list",
            "quotas",
            "gpu_classes",
            "availability",
            "read_group",
            "read_instance",
            "read_events",
            "create",
            "other",
        ]
        | None
    ) = None


class ProviderCall(Frozen):
    """No generic HTTP method/path/header/body/URL or credential override surface."""

    operation: Operation
    organization: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    project: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    group: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    provider_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    selection_digest: Digest
    nonce: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    # Synthetic normalized spec; a future wire encoder needs separate conformance.
    spec_digest: Digest
