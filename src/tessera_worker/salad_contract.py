"""Opt-in Salad GPU configuration only. No SDK, credential resolver or transport.

This adapter implements the neutral interface by rejecting every execution effect.
Even an opted-in, valid configuration cannot enable live provider execution.
"""

import hashlib
import json
import re
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

from .execution_contracts import (
    ExecutionCapabilities,
    ExecutionEnvelope,
    PreflightRequest,
    PreflightResult,
    TargetBinding,
    TargetHealth,
)

Name = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GPUResources(ContractModel):
    cpu_cores: Annotated[int, Field(ge=1, le=256)]
    memory_mb: Annotated[int, Field(ge=512, le=1_048_576)]
    gpu_class: Name
    gpu_count: Annotated[int, Field(ge=1, le=16)]
    disk_mb: Annotated[int, Field(ge=1024, le=16_777_216)]


class AdapterTimeouts(ContractModel):
    connect_seconds: Annotated[int, Field(ge=1, le=60)]
    request_seconds: Annotated[int, Field(ge=1, le=300)]
    startup_seconds: Annotated[int, Field(ge=1, le=3600)]
    run_seconds: Annotated[int, Field(ge=1, le=604800)]
    stop_seconds: Annotated[int, Field(ge=1, le=3600)]

    @model_validator(mode="after")
    def ordered(self):
        if self.connect_seconds > self.request_seconds:
            raise ValueError("connect timeout must not exceed request timeout")
        return self


class AdapterLease(ContractModel):
    duration_seconds: Annotated[int, Field(ge=10, le=3600)]
    renew_every_seconds: Annotated[int, Field(ge=1, le=1800)]

    @model_validator(mode="after")
    def bounded_renewal(self):
        if self.renew_every_seconds * 2 >= self.duration_seconds:
            raise ValueError("lease renewal interval must be less than half its duration")
        return self


class SaladTargetConfig(ContractModel):
    version: Literal[1]
    adapter_kind: Literal["salad_gpu_future"]
    endpoint: Annotated[str, StringConstraints(max_length=2048)]
    auth_reference: Annotated[
        str, StringConstraints(pattern=r"^credential-ref:[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    ]
    image: Annotated[str, StringConstraints(max_length=512)]
    resources: GPUResources
    timeouts: AdapterTimeouts
    lease: AdapterLease
    labels: dict[Name, Name] = Field(min_length=1, max_length=32)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value):
        # Retain only the historical non-routable fixture or the fixed management
        # origin. This legacy rejecting adapter is never a configurable HTTP proxy.
        if value not in {"https://salad.example.invalid/api", "https://api.salad.com/api/public"}:
            raise ValueError("only the fixed Salad management endpoint is supported")
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "?" in value
            or "#" in value
            or re.search(r"[\s\\\x00-\x1f\x7f]", value)
            or (url.port is not None and not 1 <= url.port <= 65535)
            or not re.fullmatch(r"[A-Za-z0-9.-]+", url.hostname)
            or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in url.hostname.split(".")
            )
            or not re.fullmatch(r"/[A-Za-z0-9/_.-]*|", url.path)
        ):
            raise ValueError("endpoint must be an HTTPS URL without auth, query or fragment")
        return value

    @field_validator("image")
    @classmethod
    def pinned_image(cls, value):
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9][a-z0-9/_.-]*"
            r"@sha256:[0-9a-f]{64}",
            value,
        ):
            raise ValueError("image must name a registry/repository pinned by SHA-256 digest")
        return value

    @model_validator(mode="after")
    def request_fits_lease(self):
        if self.timeouts.request_seconds >= self.lease.duration_seconds:
            raise ValueError("request timeout must be shorter than the lease")
        return self

    def binding(self) -> TargetBinding:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return TargetBinding("salad_gpu_future", "1", hashlib.sha256(encoded.encode()).hexdigest())


class SaladUnavailable(RuntimeError):
    pass


class SaladContractAdapter:
    """Local introspection only; opt-in permits config, not credentials or effects."""

    def __init__(self, config: SaladTargetConfig | None = None, *, opt_in: bool = False):
        if type(opt_in) is not bool:
            raise ValueError("opt_in must be boolean")
        if config is not None and not isinstance(config, SaladTargetConfig):
            raise ValueError("config must be a SaladTargetConfig")
        if config is not None and not opt_in:
            raise ValueError("Salad configuration requires explicit opt_in=True")
        if opt_in and config is None:
            raise ValueError("opt-in requires an explicit validated target configuration")
        # Snapshot validated JSON so later mutation of a caller's labels cannot
        # change this adapter's identity. Never look up the auth reference.
        self._target = (
            SaladTargetConfig.model_validate(config.model_dump(mode="json")).binding()
            if config is not None
            else None
        )

    def _match(self, target: TargetBinding) -> None:
        if self._target is None or target != self._target:
            raise SaladUnavailable("Salad is disabled or the target binding does not match")

    def health(self, target: TargetBinding | None = None) -> TargetHealth:
        if target is not None:
            self._match(target)
        return TargetHealth(
            self._target,
            "disabled" if self._target is None else "unavailable",
            "Local contract only; provider health was not queried; live execution is unsupported",
        )

    def capabilities(self, target: TargetBinding) -> ExecutionCapabilities:
        self._match(target)
        return ExecutionCapabilities(
            preflight=True,
            submit=False,
            observe=False,
            cancel=False,
            reconcile=False,
            collect=False,
            account=False,
            limitations=(
                "Local rejecting preflight only; GPU availability and enforcement unknown",
            ),
        )

    def preflight(self, request: PreflightRequest) -> PreflightResult:
        return PreflightResult(
            request.target,
            self.capabilities(request.target),
            False,
            "Salad live execution is not implemented; configuration is not execution authority",
        )

    def _unavailable(self, envelope: ExecutionEnvelope) -> None:
        self._match(envelope.target)
        raise SaladUnavailable(
            "Salad live execution is not implemented; no provider effect attempted"
        )

    def submit(self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None):
        self._unavailable(envelope)

    def observe(self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None):
        self._unavailable(envelope)

    def cancel(self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None):
        self._unavailable(envelope)

    def reconcile(self, envelope: ExecutionEnvelope, *, timeout_seconds: float | None = None):
        self._unavailable(envelope)

    def collect(self, envelope: ExecutionEnvelope):
        self._unavailable(envelope)

    def account(self, envelope: ExecutionEnvelope):
        self._unavailable(envelope)
