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
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_serializer,
    model_validator,
)

from .campaign_image_pull import (
    DEFAULT_INTERRUPTIBLE_MAX_IMAGE_PULL_BYTES,
    MAX_IMAGE_PULL_BYTES,
)
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
# Operator capacity fallback (``fallback_profiles``): at most this many names per profile.
MAX_FALLBACK_PROFILES = 3
# Fields a fallback profile must share with the profile that lists it: the same tenant, worker,
# account and the same derived workload (custody, watchdog, monitor, capability environment,
# destinations, adapter section), so moving a stage to it changes only where and on what
# hardware it runs, never who runs it or what runs (ADR 0151's exact-substitution rule).
FALLBACK_SHARED_FIELDS = (
    "org_id",
    "project_id",
    "worker_principal_id",
    "salad_organization",
    "capability_environment_id",
    "artifact_destination",
    "checkpoint_destination",
    "monitor_profile",
    "watchdog_profile",
    "custody_host",
    "stage_adapter",
)

_Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
_Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
_SaladName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
_Hex64 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
# Runtime capability names an operator may declare for an image (``campaign_providers``).
_RuntimeCapability = Literal["pinned_fetch_manifest"]
_GPU_CLASS = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# Same shape as the CPU rates (``remote_execution_models.CpuRate``); positive.
_GPU_HOUR_USD = r"^(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,6})?$"
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StageAdapterConfig(_Closed):
    """Operator opt-in for the stage adapter (ADR 0076): what the adapter cannot take from a stage.

    Every field is optional (ADR 0077): the section's presence is the opt-in. ``program`` is the
    operator-authored program template of an adapted stage (absent: the generic default of
    ``campaign_salad_adapter``); ``max_input_bytes`` is a ceiling on the stage's input (absent:
    only the profile's disk bounds it; the reviewed cap is the pinned input's exact size).

    ``gpu_hour_usd`` is required on, and only on, a GPU profile's section (ADR 0141 amendment
    2026-11-23): the operator's USD/hour price for that GPU class, the rate a candidate's
    full-duration estimate is computed from at submit. It is an estimate input like
    ``cpu_rate``, never invoice or cost proof; the published GPU rate is still read and checked
    against the candidate's per-run cap at admission (fails closed when higher)."""

    program: SaladProgramTemplate | None = None
    max_input_bytes: int | None = Field(default=None, ge=1, le=1 << 40)
    grant_ttl_seconds: int = Field(default=21600, ge=3600, le=43200)
    gpu_hour_usd: Annotated[str, StringConstraints(pattern=_GPU_HOUR_USD)] | None = None

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
    # Operator-declared runtime capabilities of allowlisted images, by digest (closed names). An
    # image absent here has unknown capabilities: a stage that needs one (a pinned-fetch stage
    # needs ``pinned_fetch_manifest``) is refused on it at placement; other stages are unaffected.
    image_capabilities: dict[_ImageDigest, list[_RuntimeCapability]] = Field(
        default_factory=dict, max_length=32
    )
    max_run_cents: int = Field(ge=1, le=MAX_RUN_CENTS)
    max_task_cents: int = Field(ge=1, le=MAX_TASK_CENTS)
    # Narrowing only (ADR 0044): False ignores any approve-body review pre-approval for
    # stages of this profile. True never pre-approves anything by itself.
    preapprove_reviews: bool = True
    # Present only on a profile the operator opened to adapted local-docker stages (ADR 0076); on
    # a GPU profile it also carries the operator's GPU price (ADR 0141 amendment 2026-11-23).
    stage_adapter: StageAdapterConfig | None = None
    # Ceiling on a stage image's compressed pull size: a node can be rescheduled while a large
    # image is still being pulled. 4 GiB unless changed; null turns the check off. An image over
    # it is a recorded warning, or a refusal at placement with ``strict_image_pull``.
    max_image_pull_bytes: int | None = Field(
        default=DEFAULT_INTERRUPTIBLE_MAX_IMAGE_PULL_BYTES, ge=1, le=MAX_IMAGE_PULL_BYTES
    )
    strict_image_pull: bool = False
    # Operator capacity fallback (CPU-only profiles): other profiles of this file a stage that
    # runs on this one may move to, in order, after the stage's own approved
    # ``alternate_profiles`` are used up, and ONLY before any possible start (an attempt proven
    # never started that settled at exactly zero, ADR 0169). Operator config, not spec: an
    # approved spec and its digest never change. Each move is still bounded by the stage's
    # ``retry.max_class_fallbacks`` and re-checked at move time against the stage's approved hold
    # (``campaign_salad_fallback``). Absent (never null or ``[]`` in a document) by default.
    fallback_profiles: (
        Annotated[list[_Key], Field(min_length=1, max_length=MAX_FALLBACK_PROFILES)] | None
    ) = None
    # Operator-recorded compressed pull sizes by allowed image digest (bytes); an image with
    # no entry has an unknown size.
    image_pull_sizes: dict[_ImageDigest, Annotated[int, Field(ge=0, le=MAX_IMAGE_PULL_BYTES)]] = (
        Field(default_factory=dict, max_length=32)
    )

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
        adapter = self.stage_adapter
        if adapter is not None and self.gpu and adapter.gpu_hour_usd is None:
            raise ValueError(
                "stage_adapter applies to a CPU-only profile, or to a GPU profile that names "
                "its operator price in stage_adapter.gpu_hour_usd"
            )
        if adapter is not None and not self.gpu and adapter.gpu_hour_usd is not None:
            raise ValueError("stage_adapter.gpu_hour_usd applies to a GPU profile only")
        if adapter is not None and adapter.gpu_hour_usd is not None:
            if Decimal(adapter.gpu_hour_usd) <= 0:
                raise ValueError("stage_adapter.gpu_hour_usd must be positive")
        if self.strict_image_pull and self.max_image_pull_bytes is None:
            raise ValueError("strict_image_pull needs max_image_pull_bytes")
        if not set(self.image_pull_sizes) <= set(self.allowed_image_digests):
            raise ValueError("image_pull_sizes names a digest that is not allowed")
        if not set(self.image_capabilities) <= set(self.allowed_image_digests):
            raise ValueError("image_capabilities names an image not in allowed_image_digests")
        fallbacks = self.fallback_profiles
        if fallbacks is not None:
            if self.gpu:
                raise ValueError("fallback_profiles applies to a CPU-only profile")
            if len(set(fallbacks)) != len(fallbacks):
                raise ValueError("fallback_profiles must be distinct")
            if self.name in fallbacks:
                raise ValueError("fallback_profiles must not name the profile itself")
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_fallbacks(self, handler):
        value = handler(self)
        if self.fallback_profiles is None:
            value.pop("fallback_profiles", None)
        return value

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

    def adapter_estimated_cents(self, seconds: int) -> Decimal:
        """Estimate (cents) of an adapted stage running ``seconds`` on this profile: the CPU rates,
        or the GPU price the operator set in ``stage_adapter.gpu_hour_usd``."""
        if not self.gpu:
            return self.estimated_cents(seconds)
        if self.stage_adapter is None or self.stage_adapter.gpu_hour_usd is None:
            raise ValueError("this GPU profile names no operator GPU price")
        return Decimal(self.stage_adapter.gpu_hour_usd) * Decimal(seconds) * 100 / 3600

    def image_allowed(self, image: str) -> bool:
        return _image_digest(image) in self.allowed_image_digests

    def image_runtime_capabilities(self, image: str) -> frozenset[str] | None:
        """The capabilities the operator declared for this allowlisted image, or None when it
        declared none for it (unknown: placement then fails closed for a stage that needs one)."""
        digest = _image_digest(image)
        if digest not in self.allowed_image_digests or digest not in self.image_capabilities:
            return None
        return frozenset(self.image_capabilities[digest])


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
        _check_fallback_graph(self.profiles)
        return self

    def get(self, name: str) -> SaladCampaignProfile | None:
        return next((p for p in self.profiles if p.name == name), None)

    def fallback_chain(self, name: str) -> tuple[str, ...]:
        """The operator fallback chain of profile ``name``: its ``fallback_profiles`` in order,
        each followed by its own chain (depth first), without repeats and without ``name``.
        Empty for an unknown profile or one without fallbacks. The load-time checks guarantee
        the graph is acyclic and every name resolves, so this always terminates."""
        found: list[str] = []

        def visit(current: str) -> None:
            profile = self.get(current)
            for item in (profile.fallback_profiles or ()) if profile is not None else ():
                if item != name and item not in found:
                    found.append(item)
                    visit(item)

        visit(name)
        return tuple(found)


def _check_fallback_graph(profiles: list[SaladCampaignProfile]) -> None:
    """File-level checks of ``fallback_profiles``: every name resolves to a profile of this
    file, of the same (CPU) kind and sharing ``FALLBACK_SHARED_FIELDS``, and the graph is
    acyclic. A refusal fails the whole file (closed)."""
    by_name = {p.name: p for p in profiles}
    for profile in profiles:
        for name in profile.fallback_profiles or ():
            other = by_name.get(name)
            if other is None:
                raise ValueError(f"profile {profile.name!r} falls back to an unknown profile")
            if other.gpu != profile.gpu:
                raise ValueError(f"profile {profile.name!r} falls back to another kind")
            for field in FALLBACK_SHARED_FIELDS:
                if getattr(other, field) != getattr(profile, field):
                    raise ValueError(
                        f"profile {profile.name!r} falls back to {name!r}, whose {field} differs"
                    )
    state: dict[str, int] = {}  # 1: on the current path, 2: done

    def visit(name: str) -> None:
        state[name] = 1
        for item in by_name[name].fallback_profiles or ():
            if state.get(item) == 1:
                raise ValueError("fallback_profiles form a cycle")
            if item not in state:
                visit(item)
        state[name] = 2

    for profile in profiles:
        if profile.name not in state:
            visit(profile.name)


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
