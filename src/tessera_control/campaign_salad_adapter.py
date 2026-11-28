"""Stage adapter: a local-docker CPU stage as a Salad placement candidate (ADR 0076).

The heavy-stage rule (ADR 0141, amendment 2026-11-23) also derives a GPU candidate for a
``gpu``-tagged stage on a GPU profile the operator opened with a price; an explicit
``{"adapt": ...}`` request still takes a CPU-only profile only.

A v2 stage lists ``{"provider": "salad", "runtime": "salad-workload-v1", "provider_config":
{"adapt": {"profile": NAME}}}`` (optionally with ``"alternate_profiles": [NAME, ...]``, ADR 0151)
among its ``alternate_providers``. ``expand_spec`` turns that request into the explicit ``salad``
block a hand-written candidate would carry, from the stage itself and the operator's profile,
before the spec is digested and stored. It is a pure function:
the same stage and profile give the same bytes, nothing is read from the network or the database,
and every refusal is a 409 that names the limit. The expanded candidate then passes the ordinary
submit, approval and placement checks of the Salad provider; this module relaxes none of them.

The only fetches the contract it builds declares are the stage's own ``public_artifact`` pins
(ADR 0097, amended 2026-11-23): one fetch row per pinned file (url, sha256, max_bytes), the pins'
redirect hosts, and one ``file`` item per pin beside the image entry in the inline manifest, so the
runtime downloads and verifies them before the claim. The reviewable destinations are therefore
exactly the pinned hosts and their redirect hosts; a stage with free-form ``egress`` is refused,
and a stage without pins declares no fetch and no redirect host (ADR 0036, ADR 0047).

A stage that reads another stage's output is accepted with a late-bound input: the contract
declares the input by a late-bind key and a cap from the upstream outputs' declared
``max_bytes``. Its bytes still arrive only as the staged input snapshot the runner composes, and
the Salad driver checks them against the resolved upstream records before anything is proposed.
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
    REDIRECT_TOKEN_PREFIX,
    WorkloadContractV1,
    redirect_hosts_digest,
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


# A stage-output input (``kind: stage``) is produced by another stage of the same campaign, so
# its bytes and the digest of the snapshot the runner composes are known only once that stage
# has succeeded. The contract then carries a late-bind key in place of the snapshot digest: a
# digest of the stage's custody input declarations (which upstream stage, which output paths),
# so the reviewed contract names exactly what will be bound. At stage start the Salad driver
# checks the resolved inputs and replaces the key with the composed snapshot's digest
# (``bind_workload``); the reviewed ``max_bytes`` cap stays. Upstream sizes are the upstream
# outputs' declared ``max_bytes``: an output without a declared bound is refused.
LATE_BIND_SCHEMA = "tessera.salad.late-bound-input.v1"
CUSTODY_KINDS = ("snapshot", "artifact", "artifact_folder", "stage")

# The (path, max_bytes) of each output a ``stage`` input names, from the upstream stage's
# declared outputs; None when an output cannot be found or carries no bound.
UpstreamOutputs = Callable[[Any], "list[tuple[str, int]] | None"]


def _custody(stage: Any) -> list[Any]:
    return [s for s in stage.inputs if getattr(s, "kind", None) != "public_artifact"]


def late_bind_key(stage: Any, upstream: UpstreamOutputs | None = None) -> str | None:
    """The late-bind key of a stage with a ``stage`` input (else None).

    Computed from the spec form (``outputs`` names resolved through ``upstream``) at submit
    and from the normal form (resolved ``paths``) at start; both give the same digest."""
    rows: list[list[Any]] = []
    late = False
    for source in _custody(stage):
        kind = getattr(source, "kind", None)
        if kind == "stage":
            late = True
            paths = list(getattr(source, "paths", None) or ())
            if not paths:
                found = None if upstream is None else upstream(source)
                if not found:
                    return None
                paths = [path for path, _size in found]
            rows.append([source.name, kind, source.stage, sorted(paths)])
        elif kind == "snapshot":
            rows.append([source.name, kind, source.digest, []])
        else:
            rows.append([source.name, str(kind), "", []])
    if not late:
        return None
    body = {"schema": LATE_BIND_SCHEMA, "inputs": sorted(rows)}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


# A re-run (ADR 0069/0209) replaces a ``stage`` input of a retried stage by a pin of the earlier
# campaign's recorded output (``artifact`` / ``artifact_folder``), but the stage keeps its
# reviewed adapted contract, which declared its input by the original late-bind key. The re-run
# plan rebinds that one key to the pinned-input key below (a digest of exactly what the stage now
# pins: names, producing campaign and stage, outputs, paths, sha256), and only when the contract
# declared exactly the original key. The runner still composes the snapshot and the driver still
# binds its digest at start, so the late-bound checks apply to a re-run unchanged.
PINNED_BIND_SCHEMA = "tessera.salad.pinned-late-bound-input.v1"


def _pinned_files(source: Any) -> list[list[str]]:
    rows = []
    for item in getattr(source, "files", None) or ():
        if isinstance(item, (tuple, list)):
            output, path, sha256 = item
        else:
            output, path, sha256 = item.output, item.path, item.sha256
        rows.append([str(output), str(path), str(sha256)])
    return sorted(rows)


def pinned_bind_key(stage: Any) -> str | None:
    """The key a re-run gives a stage whose ``stage`` inputs became pins (else None).

    Defined for a stage with at least one ``artifact`` / ``artifact_folder`` input; computed from
    the normal form, so the re-run plan and the runner derive the same digest. A ``stage`` input
    the re-run kept (its upstream runs again) is a row of its stage and resolved paths, as in
    ``late_bind_key``, so a mixed stage's key names every pin and every late-bound input in full
    (``late_bind_key`` records a pin only by its name and kind)."""
    rows: list[list[Any]] = []
    pinned = False
    for source in _custody(stage):
        kind = getattr(source, "kind", None)
        if kind == "stage":
            paths = list(getattr(source, "paths", None) or ())
            if not paths:
                return None
            rows.append([source.name, kind, source.stage, sorted(paths)])
        elif kind == "snapshot":
            rows.append([source.name, kind, source.digest, []])
        elif kind == "artifact":
            pinned = True
            rows.append(
                [
                    source.name,
                    kind,
                    f"{source.campaign_id}/{source.stage}",
                    [[source.output, source.sha256]],
                ]
            )
        elif kind == "artifact_folder":
            pinned = True
            rows.append(
                [
                    source.name,
                    kind,
                    f"{source.campaign_id}/{source.stage}",
                    _pinned_files(source),
                ]
            )
        else:
            rows.append([source.name, str(kind), "", []])
    if not pinned:
        return None
    body = {"schema": PINNED_BIND_SCHEMA, "inputs": sorted(rows)}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def rebound_key(stage: Any) -> str | None:
    """The key a re-run rebinds ``stage``'s contract to: ``pinned_bind_key`` once it pins an
    earlier output (alone or beside a ``stage`` input the re-run kept), else ``late_bind_key``."""
    return pinned_bind_key(stage) or late_bind_key(stage)


def is_late_bound(stage: Any, contract: Any) -> bool:
    """Whether ``contract`` declares the stage's input by its late-bind key (ADR 0076 note),
    or by the pinned-input key a re-run rebinds it to."""
    declared = getattr(contract, "input", None)
    if declared is None:
        return False
    keys = {late_bind_key(stage), pinned_bind_key(stage)} - {None}
    return declared.snapshot_digest in keys


def bind_workload(
    contract: WorkloadContractV1, snapshot_digest: str, max_bytes: int | None = None
) -> WorkloadContractV1:
    """``contract`` with its declared input digest (and the argv ``input:`` token) replaced by
    the composed snapshot's ``snapshot_digest``. The reviewed ``max_bytes`` cap is unchanged
    unless ``max_bytes`` is given (a checkpoint retry, ADR 0211), and the result is validated
    as a contract of its own."""
    declared = contract.input
    if declared is None:
        raise ValueError("the workload contract declares no input to bind")
    old = f"input:{declared.snapshot_digest}:{declared.max_bytes}"
    cap = declared.max_bytes if max_bytes is None else max_bytes
    data = contract.model_dump(mode="json")
    data["input"]["snapshot_digest"] = snapshot_digest
    data["input"]["max_bytes"] = cap
    data["argv_template"] = [
        f"input:{snapshot_digest}:{cap}" if token == old else token
        for token in contract.argv_template
    ]
    return WorkloadContractV1.model_validate(data)


def input_plan(
    stage: Any,
    profile: SaladCampaignProfile,
    entries: InputEntries | None,
    upstream: UpstreamOutputs | None = None,
) -> tuple[str, int] | None:
    """(snapshot digest, byte cap) of the one snapshot the stage's inputs become on Salad.

    One pinned snapshot is itself. Several snapshot and pinned-artifact inputs are the snapshot the
    runner composes before staging, whose digest is a pure function of the sources' file digests, so
    it is known now and the runner's own composition must reproduce it exactly (a changed source
    fails closed). An input produced by another stage of the same campaign (``kind: stage``) has
    no digest yet: the plan is then the late-bind key and a cap of the known sizes plus the
    upstream outputs' declared ``max_bytes`` (see ``late_bind_key``).

    A ``public_artifact`` input is fetched by the provider and is never part of the snapshot
    (ADR 0097): it is ignored here, and a stage with no other input has no snapshot (None)."""
    adapter = profile.stage_adapter
    sources = _custody(stage)
    if not sources:
        return None
    other = [s for s in sources if getattr(s, "kind", None) not in CUSTODY_KINDS]
    if other:
        raise _refuse(
            stage,
            f"input {other[0].name!r} of kind {getattr(other[0], 'kind', None)!r} cannot be "
            "carried to Salad",
        )
    late = [s for s in sources if s.kind == "stage"]
    single = len(sources) == 1 and sources[0].kind == "snapshot"
    if entries is None and not late:
        if not single or adapter.max_input_bytes is None:
            raise _refuse(
                stage, "the input cannot be sized here (needs the platform's recorded inputs)"
            )
        return sources[0].digest, adapter.max_input_bytes
    rows: list[tuple[str, str, int, str]] = []
    total = 0
    for source in sources:
        if source.kind == "stage":
            found = None if upstream is None else upstream(source)
            if not found or any(type(size) is not int or size < 1 for _path, size in found):
                raise _refuse(
                    stage,
                    f"input {source.name!r} reads outputs of stage {source.stage!r} that have no "
                    "declared max_bytes here, so its size cannot be bounded",
                )
            total += sum(size for _path, size in found)
            continue
        if entries is None:
            raise _refuse(
                stage, "the input cannot be sized here (needs the platform's recorded inputs)"
            )
        for path, sha, size in entries(source):
            total += size
            rows.append(
                (path if single else f"{source.name}/{path}", sha, size, "imported_unverified")
            )
    if late:
        digest = late_bind_key(stage, upstream)
        if digest is None:  # pragma: no cover - every stage input was bounded above
            raise _refuse(stage, "the stage-output inputs cannot be bound")
    else:
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


def upstream_outputs(spec: Any) -> UpstreamOutputs:
    """``UpstreamOutputs`` read from the declared outputs of ``spec``'s stages."""
    declared = {
        stage.key: {o.name: o for o in getattr(stage, "outputs", ()) or ()} for stage in spec.stages
    }

    def find(source: Any) -> list[tuple[str, int]] | None:
        outputs = declared.get(source.stage)
        if outputs is None:
            return None
        picked = [outputs.get(name) for name in getattr(source, "outputs", ()) or ()]
        if not picked or any(o is None or getattr(o, "max_bytes", None) is None for o in picked):
            return None
        return [(o.path, o.max_bytes) for o in picked]

    return find


def _refuse(stage: Any, reason: str) -> Conflict:
    return Conflict(f"Stage {stage.key!r}: Salad stage adapter: {reason}.")


def public_file_items(stage: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """The manifest ``file`` items and sorted redirect hosts of the stage's ``public_artifact``
    pins (ADR 0097): one item per pinned file, fetched to ``files/<input>/<name>`` (the launcher
    maps the local worker's ``/inputs-public/<input>/<name>`` there), flagged ``cache`` exactly
    when its source asks. Reads the spec form (``files``) and the normal form (``public_files``),
    so the adapter that builds and the provider that checks derive the same items."""
    items: list[dict[str, Any]] = []
    hosts: set[str] = set()
    for source in getattr(stage, "inputs", ()):
        if getattr(source, "kind", None) != "public_artifact":
            continue
        files = getattr(source, "public_files", None)
        if files is None or (not files and getattr(source, "files", None)):
            files = [(f.name, f.url, f.sha256, f.max_bytes) for f in source.files]
        hosts |= set(source.redirect_hosts)
        for name, url, sha256, max_bytes in files:
            item = {
                "url": url,
                "sha256": sha256,
                "max_bytes": max_bytes,
                "kind": "file",
                "dest": f"{source.name}/{name}",
            }
            if getattr(source, "cache", False):
                item["cache"] = True
            items.append(item)
    return items, sorted(hosts)


def adapted_manifest(argv: list[str], files: list[dict[str, Any]], hosts: list[str]) -> dict:
    """The inline manifest of an adapted stage: the image entry, then the pinned files."""
    manifest: dict[str, Any] = {
        "schema": GPU_RUNTIME_MANIFEST_SCHEMA,
        "fetches": [
            {"kind": "entry", "source": "image", "path": LAUNCHER, "argv": argv},
            *files,
        ],
    }
    if hosts:
        manifest["redirect_hosts"] = list(hosts)
    return manifest


def _manifest(argv: list[str], files: list[dict[str, Any]], hosts: list[str]) -> bytes:
    manifest = adapted_manifest(argv, files, hosts)
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def derive_candidate_config(
    stage: Any,
    profile: SaladCampaignProfile,
    entries: InputEntries | None = None,
    upstream: UpstreamOutputs | None = None,
    *,
    gpu: bool = False,
) -> dict[str, Any]:
    """The full ``SaladStageSpec`` (as JSON) that runs ``stage`` on ``profile``, or Conflict.

    ``stage`` is a ``StageV2`` without its adapter candidates. ``gpu`` asks for a GPU candidate
    (ADR 0141 amendment 2026-11-23: only the heavy-stage rule does, for a ``gpu``-tagged stage):
    the profile must then be a GPU profile the operator opened to adapted stages with a GPU price,
    and otherwise a CPU-only one, so neither kind is ever offered for the other."""
    adapter = profile.stage_adapter
    if gpu and (not profile.gpu or adapter is None or adapter.gpu_hour_usd is None):
        raise _refuse(
            stage,
            f"profile {profile.name!r} is not a GPU profile with a stage_adapter section that "
            "names gpu_hour_usd (the operator has not opened it to adapted GPU stages)",
        )
    if not gpu and (profile.gpu or adapter is None):
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
    plan = input_plan(stage, profile, entries, upstream)
    files, redirect_hosts = public_file_items(stage)
    fetched = sum(item["max_bytes"] for item in files)
    disk = profile.disk_mb * MIB
    if fetched and fetched + (0 if plan is None else plan[1]) > disk:
        raise _refuse(
            stage,
            f"the pinned public files ({fetched} bytes) and inputs exceed profile "
            f"{profile.name!r} disk_mb {profile.disk_mb} ({disk} bytes)",
        )
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
    raw = _manifest(list(stage.argv), files, redirect_hosts)
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
        *([] if plan is None else [f"input:{plan[0]}:{plan[1]}"]),
        *(
            [f"{REDIRECT_TOKEN_PREFIX}{redirect_hosts_digest(redirect_hosts)}"]
            if redirect_hosts
            else []
        ),
        *refs,
    ]
    if len(template) > MAX_TEMPLATE_TOKENS:
        raise _refuse(
            stage,
            "the argv is too large to carry in a Salad workload argv (long pinned public_artifact "
            "URLs, many pinned files or a long stage argv are the usual cause)",
        )
    try:
        contract = WorkloadContractV1.model_validate(
            {
                "contract": "tessera.salad.workload.v1",
                "runtime": "gpu_runtime_v1",
                "argv_template": template,
                "env": {},
                "workload_seconds": seconds,
                "outputs": [{"name": o.path, "max_bytes": o.max_bytes} for o in outputs],
                "egress": {
                    "kind": "provider_broad",
                    "fetches": [
                        {key: item[key] for key in ("url", "sha256", "max_bytes")} for item in files
                    ],
                    **({"redirect_hosts": redirect_hosts} if redirect_hosts else {}),
                },
                "custody_host": profile.custody_host,
                **(
                    {}
                    if plan is None
                    else {
                        "input": {
                            "kind": "staged_snapshot",
                            "snapshot_digest": plan[0],
                            "max_bytes": plan[1],
                        }
                    }
                ),
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
        gpu=gpu,
        adapted=True,
    )
    return config.model_dump(mode="json")


def candidate_estimate_cents(stage: Any, profile: SaladCampaignProfile) -> int:
    """The whole stage duration at the profile's CPU rate (or, on a GPU profile, the operator's
    ``stage_adapter.gpu_hour_usd``), rounded up to a cent (at least 1)."""
    worst = profile.adapter_estimated_cents(stage.max_duration_seconds)
    cents = int(worst)
    return max(1, cents if cents == worst else cents + 1)


def _visible(
    profiles: SaladCampaignProfiles, name: str, tenant: tuple[str, str] | None
) -> SaladCampaignProfile | None:
    """The named profile, or None when it is unknown or (with ``tenant``) of another
    organization or project: such a profile is not visible, so nothing about it (resources,
    allowlist, rates) can shape a message."""
    profile = profiles.get(name)
    if (
        profile is not None
        and tenant is not None
        and (profile.org_id, profile.project_id) != tenant
    ):
        return None
    return profile


def expand_spec(
    spec: Any,
    profiles: SaladCampaignProfiles | None,
    entries: InputEntries | None = None,
    *,
    tenant: tuple[str, str] | None,
) -> Any:
    """``spec`` with every adapter request replaced by its derived candidate (else ``spec``).

    ``tenant`` is required: a caller that has an organization passes ``(org_id, project_id)``, and
    a profile of any other organization or project is then treated as unknown. ``None`` is for
    offline validation only (no organization exists to check) and must be written out, so a new
    caller cannot skip the check by forgetting the argument."""
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
                raise _refuse(
                    stage,
                    "the adapt request must be exactly {profile: NAME} with optional distinct "
                    "alternate_profiles (at most 4, none equal to profile)",
                ) from None
            if profiles is None or not profiles.profiles:
                raise _refuse(stage, "no Salad profile is configured")
            profile = _visible(profiles, request.adapt.profile, tenant)
            if profile is None:
                raise _refuse(stage, f"unknown profile {request.adapt.profile!r}")
            config = derive_candidate_config(base, profile, entries, upstream_outputs(spec))
            estimates = [candidate_estimate_cents(base, profile)]
            # ADR 0151: each alternate must derive exactly the same workload (only the profile
            # name may differ), so ``stage_on_profile`` is an exact substitution at run time.
            for name in request.adapt.alternate_profiles or ():
                other = _visible(profiles, name, tenant)
                if other is None:
                    raise _refuse(stage, f"unknown alternate profile {name!r}")
                derived = derive_candidate_config(base, other, entries, upstream_outputs(spec))
                if derived | {"profile": profile.name} != config:
                    raise _refuse(
                        stage,
                        f"alternate profile {name!r} derives a different Salad workload than "
                        f"profile {profile.name!r} (worker, custody host, watchdog, program or "
                        "grant lifetime differ); list only profiles that run the same workload",
                    )
                estimates.append(candidate_estimate_cents(base, other))
            if request.adapt.alternate_profiles:
                config["alternate_profiles"] = list(request.adapt.alternate_profiles)
            # The candidate's hold covers its costliest listed profile (a single profile keeps
            # its own estimate, so an existing expansion is byte-identical).
            estimate = (
                item.max_exposure_cents if item.max_exposure_cents is not None else max(estimates)
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
    spec: Any, entries: InputEntries | None = None, *, tenant: tuple[str, str] | None
) -> Any:
    """``expand_spec`` with the profiles of the registered Salad provider (None when absent).

    ``tenant`` is required, as for ``expand_spec``."""
    try:
        provider = stage_provider(SALAD_PROVIDER)
    except KeyError:
        provider = None
    return expand_spec(spec, getattr(provider, "profiles", None), entries, tenant=tenant)
