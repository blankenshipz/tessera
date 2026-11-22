"""Stage adapter: a local-docker CPU stage as a Salad placement candidate (ADR 0076).

A v2 stage lists ``{"provider": "salad", "runtime": "salad-workload-v1", "provider_config":
{"adapt": {"profile": NAME}}}`` among its ``alternate_providers``. ``expand_spec`` turns that
request into the explicit ``salad`` block a hand-written candidate would carry, from the stage
itself and the operator's profile, before the spec is digested and stored. It is a pure function:
the same stage and profile give the same bytes, nothing is read from the network or the database,
and every refusal is a 409 that names the limit. The expanded candidate then passes the ordinary
submit, approval and placement checks of the Salad provider; this module relaxes none of them.

Nothing here adds an egress destination: the contract it builds declares no fetch and no redirect
host, so the workload can reach only what every Salad stage already can (provider-wide egress,
ADR 0036) and no new destination is reviewable or reachable (ADR 0047).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from .campaign_models import (
    DOCKER_CPU_RUNTIME,
    SALAD_PROVIDER,
    SALAD_RUNTIME,
    CampaignSpecV2,
    SaladAdapterRequest,
    SaladStageSpec,
    is_adapter_request,
    stage_provider,
)
from .salad_campaign_profiles import SaladCampaignProfile, SaladCampaignProfiles
from .salad_workload_contract import (
    GPU_RUNTIME_MANIFEST_SCHEMA,
    GPU_RUNTIME_MIN_SECONDS,
    IMAGE_ENTRIES,
    MAX_TOTAL_OUTPUT_BYTES,
    OUTPUT_NAME_RE,
    WorkloadContractV1,
)
from .snapshot_composition import composed_inventory
from .store import Conflict
from .workspace import inventory_digest

MAX_SALAD_SECONDS = 3600
MAX_SALAD_OUTPUTS = 16
MAX_TEMPLATE_TOKENS = 32
B64_CHUNK = 250
LAUNCHER = IMAGE_ENTRIES[0]


# What the adapter needs from the database to size and digest the stage's input: the (relative
# path, sha256, size) of every file a pinned ``snapshot`` or ``artifact`` input contributes. A
# Conflict names the input that cannot be resolved. None means pure mode (tests, offline checks).
InputEntries = Callable[[Any], "list[tuple[str, str, int]]"]
MIB = 1 << 20


def default_program(stage: Any) -> dict[str, Any]:
    """The generic program template of an adapted stage when the profile supplies none.

    Neutral, bounded text that names only the stage; nothing experiment-specific."""
    text = f"Run the pinned CPU stage {stage.key!r} unchanged on a Salad CPU container."
    names = (
        "objective hypothesis success_gate failure_gate stop_gate target_domain provenance_policy "
        "dataset_recommendation representation_recommendation split_recommendation "
        "metric_recommendation uncertainty"
    ).split()
    return {
        "outcome": "adapted_stage_run",
        "brief": {name: text for name in names},
        "source": {
            "uri": "urn:tessera:workload:contract-v1",
            "quoted_passage": "Reviewed generic workload contract.",
            "content_digest": "d" * 64,
            "locator": "workload-contract.json",
        },
        "gate": {
            "category": "experimental_ground_truth",
            "metric": "stage_complete",
            "unit": "fraction",
            "procedure": "Platform-derived fraction of the stage's declared outputs produced.",
            "comparison": "eq",
            "threshold": 1.0,
            "statistic": "mean",
            "minimum_replicates": 2,
            "minimum_samples": 1,
        },
    }


def input_plan(
    stage: Any, profile: SaladCampaignProfile, entries: InputEntries | None
) -> tuple[str, int]:
    """(snapshot digest, byte cap) of the one snapshot the stage's inputs become on Salad.

    One pinned snapshot is itself. Several snapshot and pinned-artifact inputs are the snapshot the
    runner composes before staging, whose digest is a pure function of the sources' file digests, so
    it is known now and the runner's own composition must reproduce it exactly (a changed source
    fails closed). An input produced by another stage of the same campaign has no digest yet."""
    adapter = profile.stage_adapter
    sources = list(stage.inputs)
    other = [s.name for s in sources if s.kind not in ("snapshot", "artifact")]
    if other:
        raise _refuse(
            stage,
            f"input {other[0]!r} is another stage's output, whose digest is not known when the "
            "spec is submitted; pin it as an artifact of a finished campaign or as a snapshot",
        )
    single = len(sources) == 1 and sources[0].kind == "snapshot"
    if entries is None:
        if not single or adapter.max_input_bytes is None:
            raise _refuse(
                stage, "the input cannot be sized here (needs the platform's recorded inputs)"
            )
        return sources[0].digest, adapter.max_input_bytes
    rows: list[tuple[str, str, int, str]] = []
    total = 0
    for source in sources:
        for path, sha, size in entries(source):
            total += size
            rows.append(
                (path if single else f"{source.name}/{path}", sha, size, "imported_unverified")
            )
    digest = sources[0].digest if single else inventory_digest(composed_inventory(rows))
    disk = profile.disk_mb * MIB
    if total > disk:
        raise _refuse(
            stage,
            f"the inputs are {total} bytes, over profile {profile.name!r} disk_mb "
            f"{profile.disk_mb} ({disk} bytes)",
        )
    if adapter.max_input_bytes is not None and total > adapter.max_input_bytes:
        raise _refuse(
            stage,
            f"the inputs are {total} bytes, over the profile's stage_adapter max_input_bytes "
            f"{adapter.max_input_bytes}",
        )
    return digest, max(1, total)


def _refuse(stage: Any, reason: str) -> Conflict:
    return Conflict(f"Stage {stage.key!r}: Salad stage adapter: {reason}.")


def _manifest(argv: list[str]) -> bytes:
    manifest = {
        "schema": GPU_RUNTIME_MANIFEST_SCHEMA,
        "fetches": [{"kind": "entry", "source": "image", "path": LAUNCHER, "argv": argv}],
    }
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def derive_candidate_config(
    stage: Any, profile: SaladCampaignProfile, entries: InputEntries | None = None
) -> dict[str, Any]:
    """The full ``SaladStageSpec`` (as JSON) that runs ``stage`` on ``profile``, or Conflict.

    ``stage`` is a ``StageV2`` without its adapter candidates."""
    adapter = profile.stage_adapter
    if profile.gpu or adapter is None:
        raise _refuse(
            stage,
            f"profile {profile.name!r} is not a CPU-only profile with a stage_adapter section "
            "(the operator has not opened it to adapted stages)",
        )
    if stage.runtime != DOCKER_CPU_RUNTIME:
        raise _refuse(stage, f"only a {DOCKER_CPU_RUNTIME} stage can be adapted")
    if not profile.image_allowed(stage.image):
        raise _refuse(
            stage,
            f"image {stage.image!r} is not allowlisted: add "
            f'"sha256:{stage.image.split("@sha256:", 1)[-1]}" to allowed_image_digests of profile '
            f"{profile.name!r}",
        )
    seconds = stage.max_duration_seconds
    if seconds > MAX_SALAD_SECONDS:
        raise _refuse(
            stage,
            f"max_duration_seconds {seconds} exceeds the Salad limit of {MAX_SALAD_SECONDS}",
        )
    if seconds < GPU_RUNTIME_MIN_SECONDS:
        raise _refuse(
            stage,
            f"max_duration_seconds {seconds} is below the Salad runtime minimum of "
            f"{GPU_RUNTIME_MIN_SECONDS}",
        )
    for field, value in (
        ("env", stage.env),
        ("egress", stage.egress),
        ("scratch_bytes", stage.scratch_bytes),
        ("retry.transient_exit_codes", stage.retry.transient_exit_codes),
    ):
        if value:
            raise _refuse(stage, f"{field} cannot be carried to Salad")
    snapshot_digest, input_bytes = input_plan(stage, profile, entries)
    outputs = list(stage.outputs)
    if len(outputs) > MAX_SALAD_OUTPUTS:
        raise _refuse(stage, f"more than {MAX_SALAD_OUTPUTS} outputs")
    bad = [o.path for o in outputs if not OUTPUT_NAME_RE.fullmatch(o.path)]
    if bad:
        raise _refuse(
            stage, f"output path {bad[0]!r} is not a flat Salad output name (a-z, 0-9, . _ -)"
        )
    if sum(o.max_bytes for o in outputs) > MAX_TOTAL_OUTPUT_BYTES:
        raise _refuse(stage, f"outputs exceed {MAX_TOTAL_OUTPUT_BYTES} bytes in total")
    raw = _manifest(list(stage.argv))
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    refs = [f"b64:{encoded[i : i + B64_CHUNK]}" for i in range(0, len(encoded), B64_CHUNK)]
    total = sum(o.max_bytes for o in outputs)
    template = [
        "v1",
        "{operation}",
        "{epoch}",
        "{custody_host}",
        profile.watchdog_profile,
        str(seconds),
        str(total),
        str(len(outputs)),
        hashlib.sha256(raw).hexdigest(),
        f"input:{snapshot_digest}:{input_bytes}",
        *refs,
    ]
    if len(template) > MAX_TEMPLATE_TOKENS:
        raise _refuse(stage, "the argv is too large to carry in a Salad workload argv")
    try:
        contract = WorkloadContractV1.model_validate(
            {
                "contract": "tessera.salad.workload.v1",
                "runtime": "gpu_runtime_v1",
                "argv_template": template,
                "env": {},
                "workload_seconds": seconds,
                "outputs": [{"name": o.path, "max_bytes": o.max_bytes} for o in outputs],
                "egress": {"kind": "provider_broad", "fetches": []},
                "custody_host": profile.custody_host,
                "input": {
                    "kind": "staged_snapshot",
                    "snapshot_digest": snapshot_digest,
                    "max_bytes": input_bytes,
                },
            }
        )
    except ValidationError as error:
        first = error.errors()[0] if error.errors() else {}
        raise _refuse(
            stage, f"the derived workload is invalid: {str(first.get('msg', ''))[:200]}"
        ) from None
    config = SaladStageSpec(
        profile=profile.name,
        worker_principal_id=profile.worker_principal_id,
        workload=contract,
        program=adapter.program or default_program(stage),
        grant_ttl_seconds=adapter.grant_ttl_seconds,
        gpu=False,
        adapted=True,
    )
    return config.model_dump(mode="json")


def candidate_estimate_cents(stage: Any, profile: SaladCampaignProfile) -> int:
    """The whole stage duration at the profile's CPU rate, rounded up to a cent (at least 1)."""
    worst = profile.estimated_cents(stage.max_duration_seconds)
    cents = int(worst)
    return max(1, cents if cents == worst else cents + 1)


def expand_spec(
    spec: Any,
    profiles: SaladCampaignProfiles | None,
    entries: InputEntries | None = None,
    tenant: tuple[str, str] | None = None,
) -> Any:
    """``spec`` with every adapter request replaced by its derived candidate (else ``spec``)."""
    if not isinstance(spec, CampaignSpecV2):
        return spec
    for own in spec.stages:
        # The request is a candidate form: a stage's own provider_config must be explicit.
        if is_adapter_request(own.provider_config):
            raise _refuse(own, "the adapter is only for an alternate_providers candidate")
    data = spec.model_dump(mode="json")
    changed = False
    for stage_data, stage in zip(data["stages"], spec.stages, strict=True):
        found = [
            (index, item)
            for index, item in enumerate(stage.alternate_providers or ())
            if is_adapter_request(item.provider_config)
        ]
        if not found:
            continue
        changed = True
        kept = [
            item.model_dump(mode="json")
            for item in stage.alternate_providers
            if not is_adapter_request(item.provider_config)
        ]
        plain = dict(stage_data)
        if kept:
            plain["alternate_providers"] = kept
        else:
            plain.pop("alternate_providers", None)
        # Validated as a stage of its own: the base stage must already be well formed.
        base = type(stage).model_validate(plain)
        for index, item in found:
            if item.provider != SALAD_PROVIDER or item.runtime != SALAD_RUNTIME:
                raise _refuse(stage, "only a salad candidate takes the adapter")
            try:
                request = SaladAdapterRequest.model_validate(item.provider_config)
            except ValidationError:
                raise _refuse(stage, "the adapt request must be exactly {profile: NAME}") from None
            if profiles is None or not profiles.profiles:
                raise _refuse(stage, "no Salad profile is configured")
            profile = profiles.get(request.adapt.profile)
            if profile is not None and tenant is not None:
                # A profile of another organization or project is not visible: refuse before
                # anything about it (resources, allowlist, rates) can shape a message.
                if (profile.org_id, profile.project_id) != tenant:
                    profile = None
            if profile is None:
                raise _refuse(stage, f"unknown profile {request.adapt.profile!r}")
            config = derive_candidate_config(base, profile, entries)
            estimate = (
                item.max_exposure_cents
                if item.max_exposure_cents is not None
                else candidate_estimate_cents(base, profile)
            )
            if estimate > stage.max_exposure_cents:
                raise _refuse(
                    stage,
                    f"the stage's max_exposure_cents {stage.max_exposure_cents} is below the "
                    f"Salad estimate {estimate} cents for its full duration; the per-run cap "
                    "is never raised for a candidate",
                )
            entry = stage_data["alternate_providers"][index]
            entry["provider_config"] = config
            entry["max_exposure_cents"] = estimate
    if not changed:
        return spec
    return type(spec).model_validate(data)


def expand_with_registered(
    spec: Any, entries: InputEntries | None = None, tenant: tuple[str, str] | None = None
) -> Any:
    """``expand_spec`` with the profiles of the registered Salad provider (None when absent)."""
    try:
        provider = stage_provider(SALAD_PROVIDER)
    except KeyError:
        provider = None
    return expand_spec(spec, getattr(provider, "profiles", None), entries, tenant)
