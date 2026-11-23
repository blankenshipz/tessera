"""Operator-owned Salad campaign profiles and the pure route derivations (ADR 0038).

A profile is closed, secret-free operator data: account, project, GPU class, resources,
worker principal, credential *reference*, custody/watchdog/monitor ids and cent
ceilings. The campaign runner and the Salad executor each derive the exact
``ExecutionSelection`` from (profile, stage contract, operation, snapshot digest) with
the functions below, and the route review checks that both agree. A stage spec never
supplies policy: it may only name an image whose digest the operator allowlisted in the
profile (``allowed_image_digests``), so one campaign approval cannot run an
agent-chosen image on third-party compute.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .campaign_models import SaladProgramTemplate
from .remote_execution_models import CpuRate, ExecutionSelection, TargetPolicy, route_window_limit
from .salad_proxy_contract import ManagementCredential, ProxyPolicy
from .salad_workload_contract import WorkloadContractV1
from .store import canonical_digest

PROFILES_SCHEMA = "tessera.salad.campaign-profiles.v1"
# Route ``data_policy`` values (ADR 0040 §4): public input only, or whatever the operator's
# provider trust setting admitted for this instance.
DATA_POLICY_PUBLIC = "public_released_only"
DATA_POLICY_TRUST = "provider_trust_policy_v1"
DATA_POLICIES = (DATA_POLICY_PUBLIC, DATA_POLICY_TRUST)
MAX_PROFILES = 16
MAX_PROFILES_BYTES = 262144
# Same ceilings salad_live_setup applies to one owner-approved route.
MAX_RUN_CENTS = 5000
MAX_TASK_CENTS = 10000

_Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
_Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
_SaladName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
_Hex64 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
_GPU_CLASS = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StageAdapterConfig(_Closed):
    """Operator opt-in for the stage adapter (ADR 0076): what the adapter cannot take from a stage.

    Every field is optional (ADR 0077): the section's presence is the opt-in. ``program`` is the
    operator-authored program template of an adapted stage (absent: the generic default of
    ``campaign_salad_adapter``); ``max_input_bytes`` is a ceiling on the stage's input (absent:
    only the profile's disk bounds it; the reviewed cap is the pinned input's exact size)."""

    program: SaladProgramTemplate | None = None
    max_input_bytes: int | None = Field(default=None, ge=1, le=1 << 40)
    grant_ttl_seconds: int = Field(default=21600, ge=3600, le=43200)

    model_config = ConfigDict(extra="forbid", frozen=True)


class SaladCampaignProfile(_Closed):
    name: _Key
    org_id: _Identifier
    project_id: _Identifier
    worker_principal_id: _Identifier
    salad_organization: _SaladName
    salad_project: _SaladName
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
    # A GPU profile names its GPU class. A CPU-only profile omits it and instead carries the
    # operator's CPU rates (``cpu_rate``): no GPU is requested, and cost is estimated from the
    # rates, exactly one of the two (ADR 0062).
    gpu_class: Annotated[str, StringConstraints(pattern=_GPU_CLASS)] | None = None
    cpu_rate: CpuRate | None = None
    cpu_millicores: int = Field(ge=1000, le=64000)
    memory_mb: int = Field(ge=1024, le=262144)
    disk_mb: int = Field(ge=1024, le=1048576)
    route_max_seconds: int = Field(ge=1, le=7200)
    capability_environment_id: Annotated[
        str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    ]
    artifact_destination: Annotated[
        str, StringConstraints(pattern=r"^artifact-ref:[a-z0-9-]{1,63}$")
    ]
    checkpoint_destination: Annotated[
        str, StringConstraints(pattern=r"^checkpoint-ref:[a-z0-9-]{1,63}$")
    ]
    monitor_profile: _Hex64
    watchdog_profile: _Hex64
    custody_host: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    credential_reference: Annotated[
        str, StringConstraints(pattern=r"^credential-ref:[a-z0-9-]{1,63}$")
    ]
    credential_version: int = Field(ge=1, le=2**31 - 1)
    # Operator allowlist of image digests a stage may run (required, never from a spec).
    allowed_image_digests: Annotated[list[_ImageDigest], Field(min_length=1, max_length=32)]
    max_run_cents: int = Field(ge=1, le=MAX_RUN_CENTS)
    max_task_cents: int = Field(ge=1, le=MAX_TASK_CENTS)
    # Narrowing only (ADR 0044): False ignores any approve-body review pre-approval for
    # stages of this profile. True never pre-approves anything by itself.
    preapprove_reviews: bool = True
    # Present only on a CPU profile the operator opened to adapted local-docker stages (ADR 0076).
    stage_adapter: StageAdapterConfig | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.cpu_millicores % 1000:
            raise ValueError("resources: whole vCPUs")
        host = self.custody_host
        if not host.endswith(".ts.net") or not all(_LABEL.fullmatch(p) for p in host.split(".")):
            raise ValueError("custody_host must be an exact *.ts.net hostname")
        if self.max_task_cents < self.max_run_cents:
            raise ValueError("max_task_cents must be at least max_run_cents")
        if (self.gpu_class is None) == (self.cpu_rate is None):
            raise ValueError("a profile names a gpu_class (GPU) or cpu_rate (CPU-only), not both")
        if self.stage_adapter is not None and self.gpu:
            raise ValueError("stage_adapter applies to a CPU-only profile")
        return self

    @property
    def gpu(self) -> bool:
        """Whether this profile places a stage on a GPU (False: a CPU-only profile)."""
        return self.gpu_class is not None

    def estimated_cents(self, seconds: int) -> Decimal:
        """Operator-rate cost estimate of running this CPU profile for ``seconds`` (cents).

        Only a CPU profile has static rates; a GPU profile's rate is read from the provider
        at admission, so this refuses rather than guess one.
        """
        if self.cpu_rate is None:
            raise ValueError("only a CPU profile has a static rate")
        hourly = self.cpu_rate.hourly_usd(self.cpu_millicores, self.memory_mb)
        return hourly * Decimal(seconds) * 100 / 3600

    def image_allowed(self, image: str) -> bool:
        return _image_digest(image) in self.allowed_image_digests


class SaladCampaignProfiles(_Closed):
    schema_id: Annotated[str, Field(alias="schema")]
    profiles: Annotated[list[SaladCampaignProfile], Field(min_length=1, max_length=MAX_PROFILES)]

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    @model_validator(mode="after")
    def unique(self):
        if self.schema_id != PROFILES_SCHEMA:
            raise ValueError("unsupported profile file schema")
        if len({p.name for p in self.profiles}) != len(self.profiles):
            raise ValueError("duplicate profile names")
        return self

    def get(self, name: str) -> SaladCampaignProfile | None:
        return next((p for p in self.profiles if p.name == name), None)


def load_profiles(path: Path | str) -> SaladCampaignProfiles:
    """Read an owner-only (0600, single-link, no symlink) absolute profile file."""
    from .salad_live_check import _private_bytes
    from .salad_workload_jwt import strict_json

    try:
        return SaladCampaignProfiles.model_validate(
            strict_json(_private_bytes(path, MAX_PROFILES_BYTES))
        )
    except (OSError, ValueError):
        raise ValueError(
            "Salad campaign profiles must be an owner-only (0600) absolute JSON file "
            f"holding a valid {PROFILES_SCHEMA} document."
        ) from None


# -- pure derivations (mirror scripts/salad_live_setup.py, workload branch only) -------


def stage_has_nonpublic_input(stage: Any) -> bool:
    """Static test: stage and artifact inputs are always non-public, so is a snapshot
    input recorded beyond ``public`` and a ``send_classes`` that names any other class."""
    for source in getattr(stage, "inputs", ()):
        kind = getattr(source, "kind", "snapshot")
        if kind == "public_artifact":
            continue  # pinned public bytes fetched on the provider (ADR 0097): never private
        if kind != "snapshot" or getattr(source, "data_class", None) != "public":
            return True
    return any(c != "public" for c in (getattr(stage, "send_classes", None) or ()))


def stage_data_policy(stage: Any) -> str:
    """The route ``data_policy`` the runner and the executor both derive for a stage."""
    return DATA_POLICY_TRUST if stage_has_nonpublic_input(stage) else DATA_POLICY_PUBLIC


def _image_digest(image: str) -> str:
    return image.split("@", 1)[1]


def capability_declaration(
    profile: SaladCampaignProfile, contract: WorkloadContractV1, image: str
) -> dict[str, Any]:
    from .capability_models import CapabilityDeclaration

    return CapabilityDeclaration(
        environment_id=profile.capability_environment_id,
        image_digest=_image_digest(image),
        network=contract.capability_network(),
    ).model_dump(mode="json")


def execution_profile(
    profile: SaladCampaignProfile,
    contract: WorkloadContractV1,
    capability_digest: str,
    operation_id: str,
    epoch: int | str,
    max_cost_cents: int,
    data_policy: str = DATA_POLICY_PUBLIC,
) -> dict[str, Any]:
    if data_policy not in DATA_POLICIES:
        raise ValueError("unknown route data policy")
    return dict(
        platform="linux/amd64",
        command="/tessera/bin/run",
        argv=contract.derived_argv(operation_id, epoch)[1:],
        argv_binding="workload_v1",
        workload=contract.model_dump(mode="json"),
        env=dict(contract.env),
        max_output_bytes=contract.output_bytes,
        max_output_files=len(contract.outputs),
        capability_digest=capability_digest,
        network_policy="provider_broad_egress_explicitly_accepted",
        data_policy=data_policy,
        artifact_destination=profile.artifact_destination,
        checkpoint_destination=profile.checkpoint_destination,
        monitor_profile=profile.monitor_profile,
        watchdog_profile=profile.watchdog_profile,
        max_cost_cents=max_cost_cents,
        credit_exposure="unknown_blocks_new_work_credit_not_hard_cap",
        **({} if profile.gpu else {"cpu_rate": profile.cpu_rate.model_dump(mode="json")}),
    )


def derived_digests(
    profile: SaladCampaignProfile, contract: WorkloadContractV1, image: str
) -> dict[str, str]:
    return dict(
        target_digest=canonical_digest(
            dict(
                target="salad_container",
                organization=profile.salad_organization,
                project=profile.salad_project,
                gpu_class=profile.gpu_class,
                country=profile.country,
                image=image,
                # A CPU-only target is a different target from any GPU one; absent for GPU so
                # every existing GPU target digest is unchanged.
                **({} if profile.gpu else {"cpu_rate": profile.cpu_rate.model_dump(mode="json")}),
            )
        ),
        # The reviewed contract is the code identity of a generic workload route.
        code_digest=canonical_digest(contract.model_dump(mode="json")),
        model_digest=canonical_digest({"model": None, "workload": "contract"}),
        environment_digest=canonical_digest(
            dict(platform="linux/amd64", env=dict(contract.env), image=image)
        ),
    )


def derive_selection(
    profile: SaladCampaignProfile,
    contract: WorkloadContractV1,
    *,
    operation_id: str,
    epoch: int | str,
    snapshot_digest: str,
    image: str,
    max_cost_cents: int,
    plan_max_seconds: int,
    data_policy: str = DATA_POLICY_PUBLIC,
) -> ExecutionSelection:
    declaration = capability_declaration(profile, contract, image)
    execution = execution_profile(
        profile,
        contract,
        canonical_digest(declaration),
        operation_id,
        epoch,
        max_cost_cents,
        data_policy,
    )
    derived = derived_digests(profile, contract, image)
    return ExecutionSelection.model_validate(
        dict(
            target="salad_container",
            target_digest=derived["target_digest"],
            resources=dict(
                cpu_millicores=profile.cpu_millicores,
                memory_mb=profile.memory_mb,
                gpu_class=profile.gpu_class,
                gpu_count=1 if profile.gpu else 0,
                disk_mb=profile.disk_mb,
                max_seconds=min(
                    profile.route_max_seconds,
                    route_window_limit("salad_container", plan_max_seconds),
                ),
            ),
            manifests=dict(
                image=image,
                data=snapshot_digest,
                code=derived["code_digest"],
                model=derived["model_digest"],
                environment=derived["environment_digest"],
            ),
            salad=dict(
                organization=profile.salad_organization,
                project=profile.salad_project,
                group="attempt-" + operation_id,
                country=profile.country,
                provider_trust="broad_egress_ephemeral_reallocation_credit_not_hard_cap",
                execution=execution,
            ),
        )
    )


def target_policy(selection: ExecutionSelection) -> TargetPolicy:
    """Operator-owned allowlist entry; built per call, never from a request or spec."""
    return TargetPolicy(selection=selection)


def proxy_policy(profile: SaladCampaignProfile, selection: ExecutionSelection) -> ProxyPolicy:
    return ProxyPolicy(
        org_id=profile.org_id,
        project_id=profile.project_id,
        target=target_policy(selection),
        management=ManagementCredential(
            reference=profile.credential_reference, version=profile.credential_version
        ),
    )
