"""The ``salad`` compute provider: a ``salad_route`` plugin over ``SaladStageDriver`` (ADR 0040).

The runner core calls only the ``ComputeProvider`` protocol. This module adapts that
protocol to the Salad stage driver (``campaign_salad``), which keeps every Salad-specific
phase: the stage study scaffold, per-attempt reviews, capability and route steps, outcome
verification, unwind, and settlement. Provider-facing effects stay with the operator-run
executor (``campaign_salad_executor``); nothing here calls Salad. The operator's Salad
profiles come from the ``--campaign-providers`` file (key ``salad_profiles``); without
them the provider is registered but refuses every Salad stage ("specified, not enabled").
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from . import provider_trust
from .campaign_delegation import STEP_TABLE
from .campaign_image_pull import DEFAULT_INTERRUPTIBLE_MAX_IMAGE_PULL_BYTES
from .campaign_infra_retry import (
    GUARDIAN_UNAVAILABLE_PRESTART_CLASSES,
    INFRA_SALAD_GUARDIAN_UNAVAILABLE,
    INFRA_SALAD_PROVISIONING_OVERRUN,
    INFRA_SALAD_REALLOCATED,
    INFRA_SALAD_SAFETY_STOP,
    REALLOCATED_PRECUSTODY,
    SAFETY_STOP_CAUSES,
    SALAD_INTERRUPTED,
    provisioning_overrun,
)
from .campaign_models import SALAD_BACKEND, SALAD_RUNTIME, SALAD_TARGET, SaladStageSpec
from .campaign_providers import (
    ComputeProvider,
    ConfigSection,
    CreateClaim,
    GrantRequest,
    Observation,
    OutputLimits,
    PlacementCapability,
    PrepareResult,
    ProviderDescriptor,
    RunnerServices,
    Settlement,
    get_provider,
    register_provider,
    replace_provider,
)
from .campaign_salad import (
    LATE_BOUND_EVENT,
    SaladStageDriver,
    credential_blocked,
    credential_message,
    salad_config,
)
from .campaign_salad_adapter import CUSTODY_KINDS, bind_workload, is_late_bound
from .campaign_store import SALAD_REFUSAL
from .models import ExecutionSpec
from .remote_execution_models import salad_route_window_refusal
from .salad_alternate_profiles import operator_chain, profile_order
from .salad_campaign_profiles import (
    PROFILES_SCHEMA,
    SaladCampaignProfile,
    SaladCampaignProfiles,
    stage_has_nonpublic_input,
)
from .salad_exit_watch import FAILURE_CLASS as EXIT_FAILURE_CLASS
from .salad_exit_watch import container_exit_receipt
from .salad_exit_watch import failure_detail as exit_failure_detail
from .salad_live_check import PRESTART_FAILURE_CLASSES
from .salad_unclaimed import CLAIMS, PROOF, unclaimed_proof
from .salad_workload_contract import COMMAND as SALAD_COMMAND
from .salad_workload_contract import (
    GPU_RUNTIME_V1,
    check_gpu_runtime_inline_rules,
    gpu_runtime_inline_manifest,
    is_image_item,
)
from .store import Conflict, Store

PROVIDER_ID = "salad"
RUNTIME = SALAD_RUNTIME
# Salad's custody total (``MAX_TOTAL_OUTPUT_BYTES``) and per-stage file cap.
OUTPUT_LIMITS = OutputLimits(
    max_files=16, max_total_bytes=16 * 1024 * 1024, max_file_bytes=16 * 1024 * 1024
)
# The step set is ADR 0038's table: Salad adds the study, decision, capability and route
# steps to the core ones. The table is closed; a plugin never adds a step.
# Salad stages carry their own bound program gate; the generic split-binding, metric and
# evidence-row steps (ADR 0049, ADR 0052) are not delegated to them. ``record_provider_fetch``
# (ADR 0097) is: control reads a Salad stage's fetch rows from its retained output.json.
DELEGABLE_STEPS = frozenset(STEP_TABLE) - {
    "bind_split",
    "record_gate_metric",
    "ingest_evidence_rows",
}


def _salad_config(stage: Any) -> SaladStageSpec | None:
    """The stage's Salad fields, or None when the stage carries none."""
    found = getattr(stage, "salad", None) or getattr(stage, "provider_config", None)
    return salad_config(stage) if found else None


PUBLIC_ARTIFACT_MISMATCH = "public_artifact_contract_mismatch"
PUBLIC_ARTIFACT_DELIVERY = "public_artifact_input_delivery"
PUBLIC_ARTIFACT_CACHE = "public_artifact_cache_mismatch"
PUBLIC_ARTIFACT_CACHE_INLINE = "public_artifact_cache_needs_inline_manifest"


def public_pins(stage: Any) -> tuple[list[tuple[str, str, int]], list[str]] | None:
    """The stage's ``public_artifact`` pins as ``(sorted (url, sha256, max_bytes) rows, sorted
    redirect hosts)`` (ADR 0097), or None when it has none. Reads both the normalized input
    (``public_files``) and the spec source (``files``)."""
    rows: list[tuple[str, str, int]] = []
    hosts: set[str] = set()
    found = False
    for source in getattr(stage, "inputs", ()):
        if getattr(source, "kind", None) != "public_artifact":
            continue
        found = True
        files = getattr(source, "public_files", None)
        if files is None or (not files and getattr(source, "files", None)):
            files = [(f.name, f.url, f.sha256, f.max_bytes) for f in source.files]
        rows += [(url, sha, size) for _name, url, sha, size in files]
        hosts |= set(source.redirect_hosts)
    return (sorted(rows), sorted(hosts)) if found else None


def public_cache_flags(stage: Any) -> dict[tuple[str, str, int], bool]:
    """Whether the approved spec asks the provider to cache each pinned file (ADR 0097), by
    ``(url, sha256, max_bytes)``. The flag is per source and applies to every file of it."""
    flags: dict[tuple[str, str, int], bool] = {}
    for source in getattr(stage, "inputs", ()):
        if getattr(source, "kind", None) != "public_artifact":
            continue
        files = getattr(source, "public_files", None)
        if files is None or (not files and getattr(source, "files", None)):
            files = [(f.name, f.url, f.sha256, f.max_bytes) for f in source.files]
        for _name, url, sha, size in files:
            flags[(url, sha, size)] = bool(getattr(source, "cache", False))
    return flags


def _cache_refusal(stage: Any, config: SaladStageSpec) -> str | None:
    """The provider-side cache is something a person approves with the spec and sees in the
    reviewed manifest: the manifest's per-file ``cache`` flag must equal the spec source's, in
    both directions. A manifest read from the input cannot be inspected at plan time, so a spec
    that asks for caching needs an inline manifest."""
    wanted = public_cache_flags(stage)
    workload = config.workload
    if getattr(workload, "runtime", None) != GPU_RUNTIME_V1:
        return PUBLIC_ARTIFACT_CACHE_INLINE if any(wanted.values()) else None
    try:
        manifest = gpu_runtime_inline_manifest(workload)
    except ValueError:
        manifest = None
    if manifest is None:
        return PUBLIC_ARTIFACT_CACHE_INLINE if any(wanted.values()) else None
    seen: set[tuple[str, str, int]] = set()
    for item in manifest["fetches"]:
        if not isinstance(item, dict) or "url" not in item:
            continue
        key = (item.get("url"), item.get("sha256"), item.get("max_bytes"))
        flag = item.get("cache", False)
        if type(flag) is not bool or flag != wanted.get(key, False):
            return PUBLIC_ARTIFACT_CACHE
        seen.add(key)
    if any(flag and key not in seen for key, flag in wanted.items()):
        return PUBLIC_ARTIFACT_CACHE  # a cached pin the reviewed manifest does not carry
    return None


def public_artifact_refusal(stage: Any, config: SaladStageSpec) -> str | None:
    """Closed reason when the workload contract does not carry exactly the spec's pinned
    public files and redirect hosts, else None (ADR 0097). The contract's fetch rows are what
    the in-container runtime may download, so a reviewer-approved spec can fetch nothing else."""
    pins = public_pins(stage)
    if pins is None:
        return None
    egress = config.workload.egress
    contract_rows = sorted((f.url, f.sha256, f.max_bytes) for f in egress.fetches)
    if contract_rows != pins[0] or sorted(egress.redirect_hosts) != pins[1]:
        return PUBLIC_ARTIFACT_MISMATCH
    sources = [getattr(s, "kind", None) for s in stage.inputs]
    if config.workload.input is not None and not set(sources) & set(CUSTODY_KINDS):
        return PUBLIC_ARTIFACT_DELIVERY  # a snapshot the contract expects is never delivered
    if stage_has_nonpublic_input(stage) and config.workload.runtime == GPU_RUNTIME_V1:
        # Private data is pulled only after begin-once; a manifest read from that input would
        # fetch after the private snapshot is materialized. Only an inline manifest fetches first.
        if gpu_runtime_inline_manifest(config.workload) is None:
            return PUBLIC_ARTIFACT_MISMATCH
    return _cache_refusal(stage, config)


def _hosts_declared(config: SaladStageSpec) -> bool:
    egress = config.workload.egress
    return bool(egress.fetches or egress.redirect_hosts)


# A Salad attempt in one of these phases ran, or may have run, past the provider create: it
# is never reconciled automatically as unclaimed (ADR 0157).
def _receipt_sent_nothing(row: Mapping[str, Any]) -> bool:
    """True when the attempt's live receipt names no provider resource and did not pass.

    Such a receipt is an honest report, not evidence of a provider effect (ADR 0205): whether
    a create or start was sent is then decided only by control's own claim and route records
    (``unclaimed_proof``), exactly as for an attempt with no receipt."""
    try:
        receipt = json.loads(row["live_receipt_json"])
    except (TypeError, ValueError):
        return False
    return (
        type(receipt) is dict
        and receipt.get("status") != "passed"
        and not receipt.get("provider_id")
        and receipt.get("create_rejection") is None
    )


UNCLAIMABLE_PHASES = frozenset(
    {"executing", "outcome_recorded", "awaiting_charge", "settled", "unclaimed"}
)


class CredentialBlocked(Conflict):
    """A 409 for a stage whose provider credential cannot be used (placement skips such a
    candidate as ``credential_unavailable``; submit and approval show the message)."""

    def __init__(self, message: str, state: dict[str, Any]) -> None:
        super().__init__(message)
        self.credential_state = state


def _check_fallbacks_reach_profiles(stage: Any, order: tuple[str, ...]) -> None:
    """A move to the next listed profile happens only before any possible start and is bounded
    by ``retry.max_class_fallbacks`` (ADR 0060 addendum, strict rule): it never spends a
    scientific attempt. A stage that lists alternates with that bound at 0 could never reach
    them, so it is refused at submit instead of approving profiles nothing can use."""
    retry = getattr(stage, "retry", None)
    if len(order) > 1 and retry is not None and retry.max_class_fallbacks < 1:
        raise Conflict(
            f"Salad stage lists {len(order) - 1} alternate profile(s) but "
            "retry.max_class_fallbacks is 0: set it to at least 1 (a move to the next profile "
            "happens only after an attempt that provably never started and spent nothing)."
        )


class SaladRouteProvider:
    descriptor = ProviderDescriptor(
        provider_id=PROVIDER_ID,
        kind="salad_route",
        runtimes=frozenset({RUNTIME}),
        target=SALAD_TARGET,
        billable=True,
        third_party=True,
        delegable_steps=DELEGABLE_STEPS,
        output_limits=OUTPUT_LIMITS,
        worker_principal_id="",
        # Interruptible capacity: a conservative default; a campaign profile may change it.
        max_image_pull_bytes=DEFAULT_INTERRUPTIBLE_MAX_IMAGE_PULL_BYTES,
    )
    config_model = SaladStageSpec

    def __init__(
        self,
        profiles: SaladCampaignProfiles | None = None,
        credential_directory: str | None = None,
        guardian_reader: Any = None,
        manifest_size_reader: Callable[[str], int | None] | None = None,
    ) -> None:
        self.profiles = profiles
        self.credential_directory = credential_directory
        # Reads the image's total compressed layer size from its registry manifest. None until
        # the operator wires one; the receipt then records ``size_unavailable``. The runner waits
        # at most ``image_provenance.SIZE_READ_TIMEOUT_SECONDS`` for it and then records
        # ``size_unavailable``; a reader that calls a registry should also set its own timeout.
        self.manifest_size_reader = manifest_size_reader
        # Control's own read of the guardian's per-frame record (ADR 0041); None means no
        # Salad frame can settle on a guardian verdict.
        self.guardian_reader = guardian_reader

    def image_pull_size_bytes(self, stage: Any) -> int | None:
        """Total compressed layer bytes of the stage image, or None when not readable."""
        if self.manifest_size_reader is None:
            return None
        return self.manifest_size_reader(stage.image)

    # -- credential expiry (metadata only; never a value) -------------------------------

    def credential_state(self, profile: Any, now: float | None = None) -> dict[str, Any]:
        """Expiry state of the profile's pinned management credential. Never raises."""
        import time

        from . import credential_health
        from .salad_secret_files import PrivateFileSecretResolver

        when = time.time() if now is None else now
        metadata = None
        # Names only the class of cause, never a path or a value.
        reason = "no credential directory is configured for this service"
        if self.credential_directory:
            reason = "the credential store could not be read by this service"
            resolver = None
            try:
                resolver = PrivateFileSecretResolver(self.credential_directory)
                metadata = resolver.read_metadata(profile.credential_reference)
            except FileNotFoundError:
                metadata = None
                reason = "the credential directory or file does not exist"
            except PermissionError:
                metadata = None
                reason = "permission denied reading the credential store"
            except Exception:  # noqa: BLE001 - unreadable store is "unknown", not a crash
                metadata = None
            finally:
                if resolver is not None:
                    resolver.close()
        state = credential_health.evaluate(
            metadata, when, pinned_version=profile.credential_version
        )
        if metadata is None:
            state["reason"] = reason
        return {
            "provider_id": PROVIDER_ID,
            "profile": profile.name,
            "reference": profile.credential_reference,
            **state,
        }

    def _profile_of(self, stage: Any) -> Any:
        config = _salad_config(stage)
        if config is None or self.profiles is None:
            return None
        return self.profiles.get(config.profile)

    def image_pull_limits(self, stage: Any) -> tuple[int | None, bool]:
        """The stage's profile ceiling on the image's compressed pull size (default 4 GiB)."""
        profile = self._profile_of(stage)
        if profile is None:
            return self.descriptor.max_image_pull_bytes, False
        return profile.max_image_pull_bytes, profile.strict_image_pull

    def image_pull_bytes(self, stage: Any) -> int | None:
        """The operator-recorded pull size of the stage image's digest, if any."""
        profile = self._profile_of(stage)
        image = getattr(stage, "image", None)
        if profile is None or not isinstance(image, str) or "@" not in image:
            return None
        return profile.image_pull_sizes.get(image.split("@", 1)[1])

    def credential_health(self, now: float | None = None) -> list[dict[str, Any]]:
        profiles = self.profiles.profiles if self.profiles is not None else []
        return [self.credential_state(profile, now) for profile in profiles]

    def driver(self, rt: RunnerServices) -> SaladStageDriver:
        """One driver per runner services object. It lives in the runner's own
        ``provider_state``, so it is evicted with the runner (a provider-held map would
        keep both alive: the driver references its runner)."""
        driver = rt.provider_state.get(PROVIDER_ID)
        if (
            driver is None
            or driver._profiles is not self.profiles
            or driver._guardian_reader is not self.guardian_reader
        ):
            driver = rt.provider_state[PROVIDER_ID] = SaladStageDriver(
                rt, self.profiles, self.guardian_reader
            )
        return driver

    # -- submit / approve -----------------------------------------------------------------

    def validate_stage(self, stage: Any) -> None:
        """Pure; ``salad`` fields iff the backend is Salad, and the workload contract
        owns argv, outputs and bytes. A stage with a non-public input may not declare
        fetch or redirect hosts (ADR 0040 §4); input classes are decided statically."""
        label = f"Stage {stage.key}"
        config = _salad_config(stage)
        if config is None:
            raise ValueError(f"{label}: salad is required for, and only for, {SALAD_BACKEND}")
        workload = config.workload
        if getattr(stage, "env", None) is not None:
            raise ValueError(f"{label}: env comes from the workload contract, not the stage")
        if getattr(stage, "egress", None) is not None:
            raise ValueError(f"{label}: the workload contract declares provider-wide egress")
        if not stage.inputs:
            raise ValueError(f"{label}: a Salad stage needs at least one input")
        if getattr(stage, "scratch_bytes", None) is not None:
            # Salad grants no scratch request (ADR 0059): its disk is the profile's. Refused
            # rather than ignored, so a placed stage never runs without the scratch it asked for.
            raise ValueError(f"{label}: scratch_bytes is not granted by Salad")
        if stage.retry.transient_exit_codes:
            raise ValueError(f"{label}: Salad stages have no exit code to classify as transient")
        if stage.max_exposure_cents < 1:
            raise ValueError(f"{label}: a Salad stage needs max_exposure_cents >= 1")
        # The pins first (ADR 0097): a fetch row that is not a pin is refused with its closed
        # reason, adapted or not.
        refusal = public_artifact_refusal(stage, config)
        if refusal is not None:
            raise ValueError(f"{label}: {refusal}")
        if config.adapted:
            self._validate_adapted(label, stage, config)
        elif list(stage.argv) != [SALAD_COMMAND, *workload.argv_template]:
            raise ValueError(f"{label}: argv must equal the workload contract's argv")
        if sorted(stage.output_paths) != sorted(o.name for o in workload.outputs):
            raise ValueError(f"{label}: output_paths must equal the contract's declared outputs")
        if stage.max_output_bytes < workload.output_bytes:
            raise ValueError(f"{label}: max_output_bytes is below the contract's output bytes")
        if stage.max_duration_seconds < workload.workload_seconds:
            raise ValueError(
                f"{label}: max_duration_seconds is below the contract's workload_seconds"
            )
        if not workload.outputs_supported():
            raise ValueError(f"{label}: declared outputs exceed the custody total")
        # The Kubernetes-job package refuses longer plans or argv; the lease also ends at
        # this duration, so it bounds the time humans have to resolve the attempt's reviews.
        if stage.max_duration_seconds > 3600 or (not config.adapted and len(stage.argv) > 32):
            raise ValueError(f"{label}: max_duration_seconds <= 3600 and at most 32 argv items")
        if (
            public_pins(stage) is None
            and stage_has_nonpublic_input(stage)
            and _hosts_declared(config)
        ):
            raise ValueError(
                f"{label}: a stage with non-public input cannot declare workload fetch or "
                "redirect hosts"
            )

    @staticmethod
    def _validate_adapted(label: str, stage: Any, config: SaladStageSpec) -> None:
        """ADR 0076: the contract is the gpu_runtime_v1 launcher carrying this stage's argv.

        The stage's argv is the local-docker form; what runs on Salad is the contract's argv, whose
        manifest holds exactly the image entry with the stage's argv, so a person reviews one
        and the other cannot differ. Beside it the manifest holds exactly one ``file`` item per
        ``public_artifact`` pin of the stage (ADR 0097), and the contract's fetch rows and redirect
        hosts are exactly the pins: no other fetch, redirect host or env is declared. It may be a
        GPU candidate (ADR 0141 amendment 2026-11-23): ``check_stage`` still requires a GPU profile
        for it and a CPU-only one otherwise."""
        from .campaign_salad_adapter import adapted_manifest, public_file_items

        workload = config.workload
        try:
            if workload.runtime != GPU_RUNTIME_V1:
                raise ValueError("the workload must use the gpu_runtime_v1 layout")
            manifest = check_gpu_runtime_inline_rules(workload)
            items = None if manifest is None else manifest["fetches"]
            if not items or not is_image_item(items[0]) or items[0]["argv"] != list(stage.argv):
                raise ValueError("the manifest must be the stage's argv as its one image entry")
            files, hosts = public_file_items(stage)
            if manifest != adapted_manifest(list(stage.argv), files, hosts):
                raise ValueError(
                    "the manifest must be the image entry plus exactly the stage's pinned files"
                )
            rows = sorted((f.url, f.sha256, f.max_bytes) for f in workload.egress.fetches)
            pinned = sorted((f["url"], f["sha256"], f["max_bytes"]) for f in files)
            if rows != pinned or list(workload.egress.redirect_hosts) != hosts or workload.env:
                raise ValueError(
                    "an adapted workload declares no fetch or redirect host beyond the stage's "
                    "pins, and no env"
                )
        except ValueError as error:
            raise ValueError(f"{label}: adapted stage: {error}") from None

    def runtime_capabilities(self, stage: Any) -> frozenset[str] | None:
        """What the stage's runtime image can do, from the operator profile's
        ``image_capabilities`` for that allowlisted digest (metadata only). None, unknown, when
        Salad is not enabled, the profile is unknown or the operator declared nothing for the
        image: placement then refuses a pinned-fetch stage here (fail closed)."""
        profiles = self.profiles
        config = _salad_config(stage)
        if profiles is None or config is None:
            return None
        profile = profiles.get(config.profile)
        return None if profile is None else profile.image_runtime_capabilities(stage.image)

    def _check_credential(self, profile: Any) -> None:
        """Refuse up front when the profile's credential is expired, revoked or pinned at a
        version that is not the enrolled one (metadata only; unknown or warning never refuses)."""
        state_of = getattr(self, "credential_state", None)
        state = state_of(profile) if state_of is not None else None
        if credential_blocked(state):
            raise CredentialBlocked(credential_message(state), state)

    def check_stage(self, connection: sqlite3.Connection, project_id: str, stage: Any) -> None:
        """Operator profile ceilings for one Salad stage; every refusal is a 409.

        Input admission (data class and trust) is the core's ``check_stage_inputs``; it is
        provider-level (the Salad provider's trust setting and the stage's worker), so it holds
        for every profile of the stage alike.

        ADR 0151: each of the stage's ``alternate_profiles`` passes exactly the checks of its
        own profile (registered, this organization and project, the stage's worker, GPU/CPU
        kind, credential, image allowlist, per-run and per-task ceilings, CPU worst case) with
        the stage's unchanged ``max_exposure_cents``; a refusal names the alternate."""
        profiles = self.profiles
        if profiles is None or not profiles.profiles:
            raise Conflict(SALAD_REFUSAL)
        config = _salad_config(stage)
        self._check_profile(connection, project_id, stage, config, config.profile)
        order = profile_order(config)
        _check_fallbacks_reach_profiles(stage, order)
        for name in order[1:]:
            try:
                self._check_profile(connection, project_id, stage, config, name)
            except CredentialBlocked:
                raise  # its message already names the profile
            except Conflict as error:
                raise Conflict(f"Salad stage alternate profile {name!r}: {error}") from None

    def _check_profile(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        stage: Any,
        config: SaladStageSpec,
        name: str,
    ) -> None:
        """``check_stage`` for one listed profile of the stage."""
        profile = self.profiles.get(name)
        if profile is None:
            raise Conflict("Salad stage names an unknown operator profile.")
        project = connection.execute(
            "SELECT org_id FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if (
            project is None
            or profile.org_id != project["org_id"]
            or profile.project_id != project_id
        ):
            raise Conflict("Salad profile does not belong to this organization and project.")
        if config.worker_principal_id != profile.worker_principal_id:
            raise Conflict("Salad stage worker is not the profile's worker.")
        # Placement (ADR 0062): a stage that declares no GPU runs only on a CPU-only profile,
        # and a GPU stage never on one, so neither wastes a GPU nor fails at the provider.
        if config.gpu != profile.gpu:
            raise Conflict(
                "Salad stage needs a GPU profile."
                if config.gpu
                else "Salad stage declares no GPU and needs a CPU-only profile."
            )
        self._check_worker(connection, project["org_id"], project_id, config.worker_principal_id)
        self._check_credential(profile)
        # The route window must cover provisioning plus the declared run; never shortened.
        refusal = salad_route_window_refusal(
            config.workload.workload_seconds,
            profile.route_max_seconds,
            f"profile {profile.name!r} route_max_seconds",
        )
        if refusal is not None:
            raise Conflict(f"Salad stage refused: {refusal}.")
        refusal = public_artifact_refusal(stage, config)
        if refusal is not None:
            raise Conflict(f"Salad stage refused: {refusal}.")
        if (
            public_pins(stage) is None
            and _hosts_declared(config)
            and any(
                provider_trust.admission_basis(connection, source.snapshot_id) != "public_only"
                for source in stage.inputs
                if source.kind == "snapshot"
            )
        ):
            raise Conflict(
                "Salad stage input is not public (or carries approved code) and cannot "
                "declare workload fetch or redirect hosts."
            )
        if not profile.image_allowed(stage.image):
            raise Conflict(
                "Salad stage image digest is not allowlisted by the operator profile: add "
                f'"sha256:{stage.image.split("@sha256:", 1)[-1]}" to allowed_image_digests of '
                f"profile {profile.name!r}."
            )
        if stage.max_exposure_cents > profile.max_run_cents:
            raise Conflict("Salad stage exposure exceeds the profile's per-run ceiling.")
        if not profile.gpu:
            # A CPU profile has static operator rates, so the worst-case cost of the stage's
            # whole duration is known now; the per-run cap is enforced at admission, not only
            # by the guardian while it runs.
            worst = profile.estimated_cents(stage.max_duration_seconds)
            if worst > stage.max_exposure_cents:
                raise Conflict(
                    "Salad stage's estimated CPU cost for its full duration exceeds its "
                    "max_exposure_cents."
                )
        # Every attempt holds its own exposure, an enabled infrastructure retry (ADR 0058), a
        # pre-custody reallocation retry (ADR 0179) or class fallback (ADR 0060 addendum)
        # included: the per-task ceiling counts them all.
        attempts = stage.retry.hold_attempts
        if stage.max_exposure_cents * attempts > profile.max_task_cents:
            raise Conflict("Salad stage worst-case hold exceeds the profile's per-task ceiling.")

    @staticmethod
    def _check_worker(
        connection: sqlite3.Connection, org_id: str, project_id: str, worker_id: str
    ) -> None:
        from .store import Forbidden

        try:
            worker = Store._live_principal(
                connection, {"id": worker_id, "org_id": org_id}, audience="worker"
            )
        except Forbidden:
            raise Conflict("Campaign worker is not registered.") from None
        if (
            worker["kind"] != "worker"
            or worker["role"] != "agent"
            or worker["project_id"] != project_id
        ):
            raise Conflict("Campaign worker is not authorized for this project.")

    def execution_spec(self, stage: Any, input_digest: str) -> dict[str, Any]:
        """The task plan's execution spec: the base plan contract only, because the
        workload contract declares Salad's provider-wide egress."""
        config = _salad_config(stage)
        argv = list(stage.argv)
        if config is not None and config.adapted:
            workload = config.workload
            if is_late_bound(stage, workload):
                # The stage-output inputs are composed now: the plan carries the bound argv,
                # the same one the route's contract carries (``bind_workload``).
                workload = bind_workload(workload, input_digest)
            argv = [SALAD_COMMAND, *workload.argv_template]
        return ExecutionSpec(
            argv=argv,
            input_snapshot_digest=input_digest,
            pinned_skill_digests=[],
            output_paths=list(stage.output_paths),
            profile="kubernetes-opencode",
            network_enabled=False,
            max_output_bytes=stage.max_output_bytes,
        ).model_dump(mode="json")

    # -- submit -----------------------------------------------------------------------------

    def prepare(self, rt: RunnerServices, ctx: Any, key: str) -> PrepareResult:
        return self.driver(rt).scaffold(ctx, key)  # type: ignore[return-value]

    def grant_request(self, rt: RunnerServices, ctx: Any, stage: Any) -> GrantRequest:
        config = _salad_config(stage)
        approval = getattr(ctx, "approval", None)
        return GrantRequest(
            worker_principal_id=config.worker_principal_id,
            ttl_seconds=config.grant_ttl_seconds,
            expires_not_after=approval["expires_at"] if approval else None,
        )

    def after_admit(self, rt: RunnerServices, ctx: Any, attempt: Mapping[str, Any]) -> None:
        self.driver(rt).admitted(attempt["id"])

    # -- poll -------------------------------------------------------------------------------

    def experiment_ready(
        self, rt: RunnerServices, ctx: Any, stage_row: Mapping, stage: Any
    ) -> bool:
        """Study variant and split lock for the stage experiment; False after a step ran."""
        return self.driver(rt).experiment_ready(ctx, dict(stage_row), stage)

    def advance(self, rt: RunnerServices, ctx: Any, stage_row: Mapping, attempt: Mapping) -> bool:
        driver = self.driver(rt)
        spec_stage = driver._spec(ctx, stage_row["stage_key"])
        return self._translate(
            lambda: driver.advance(ctx, dict(stage_row), spec_stage, dict(attempt))
        )

    def observe(self, rt: RunnerServices, attempt: Mapping[str, Any]) -> Observation:
        """A Salad attempt has no exit code; its verdict is the recorded evidence verdict."""
        verdict = attempt.get("salad_verdict")
        evidence = attempt.get("salad_evidence_digest")
        if verdict == "ok":
            return Observation(
                "succeeded",
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        if verdict is None:
            return Observation("running")
        start_failure = self._guardian_record(rt, attempt, "start_failure")
        if type(start_failure) is dict and type(start_failure.get("machines")) is int:
            # ADR 0193 (amended): the guardian saw the container fail to start on several
            # distinct machines before any custody began. The image or its command is at fault,
            # so another machine, another class or another attempt of the same digest would
            # fail the same way: permanent, and never a class-fallback trigger (the fallback
            # reads only ``prestart_failure`` and the allocation timeout, neither of which this
            # carries).
            codes = (self._guardian_record(rt, attempt, "start_events") or {}).get("exit_codes")
            exit_text = (
                f", exit_code={codes[0]}"
                if type(codes) is list and codes and type(codes[0]) is int
                else ""
            )
            events = self._guardian_record(rt, attempt, "start_events") or {}
            if "instance_exited" not in (events.get("events") or []):
                # Salad also moves a healthy instance between nodes while it allocates, pulls
                # or creates, so three machines alone do not prove the start failed. Without a
                # recorded ``instance_exited`` (no read, an empty or rate-limited one) the
                # attempt is not a permanent failure of the digest: a fresh attempt may retry,
                # bounded by ``retry.max_reallocation_retries``.
                return Observation(
                    "failed_transient",
                    failure_class=REALLOCATED_PRECUSTODY,
                    detail=(
                        f"{REALLOCATED_PRECUSTODY}: start_failure_suspected_unconfirmed: "
                        f"{start_failure['machines']} distinct machines, no recorded "
                        "instance_exited"
                    )[:500],
                    evidence_kind="salad_live_receipt",
                    evidence_digest=evidence,
                    manifest_id=attempt.get("manifest_id"),
                )
            return Observation(
                "failed_permanent",
                failure_class="start_failure_suspected",
                detail=(
                    "start_failure_suspected: the container failed to start on "
                    f"{start_failure['machines']} distinct machines before its first call-in"
                    f"{exit_text}"
                )[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        timeout = self._allocation_timeout(rt, attempt)
        # Only the guardian's own recorded timeout counts. A reason string the executor
        # reported (the live receipt's detail) is not evidence of a placement failure.
        if timeout is not None:
            # The guardian cancelled the group because nothing was ever allocated: a placement
            # failure (the runner bounds and reroutes it like an infrastructure one), never the
            # stage's own logic failing.
            return Observation(
                "failed_transient",
                failure_class="infra_allocation_timeout",
                detail=(
                    "allocation_timeout: no instance allocated within the allocation deadline"
                    if timeout["deadline_seconds"] is None
                    else f"allocation_timeout: no instance allocated within "
                    f"{timeout['deadline_seconds']}s (elapsed {timeout['elapsed_seconds']}s)"
                )[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        exited = exit_failure_detail(self._guardian_record(rt, attempt, "start_events"))
        if exited is not None:
            # The recorded system events name a non-zero process exit (not a kill or a platform
            # stop): the workload's own entry failure. A replacement machine would fail the same
            # way, so this is permanent, never ``salad_interrupted`` (which spends an infra
            # retry) or a pre-custody reallocation, whatever stop cause the guardian recorded.
            # Checked before the route-deadline records (q-2711 x ADR 0206): a run that exited
            # non-zero and also reached its route limit is never a provisioning-overrun retry.
            return Observation(
                "failed_permanent",
                failure_class=EXIT_FAILURE_CLASS,
                detail=exited,
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        deadline = self._route_deadline(rt, attempt)
        if provisioning_overrun(deadline):
            # The machine was placed but the workload never got going before the route limit
            # (no custody work, at most a minute seen running): a slow pull or start, retried
            # as infrastructure, never the stage's own failure.
            return Observation(
                "failed_transient",
                failure_class=INFRA_SALAD_PROVISIONING_OVERRUN,
                detail=(
                    f"{INFRA_SALAD_PROVISIONING_OVERRUN}: the route deadline came after "
                    f"{deadline['running_seconds']}s running with no custody work begun"
                )[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        if self._guardian_unavailable_prestart(rt, attempt):
            # The guardian's IPC was down while the server minted or checked the input ticket,
            # before any start effect: an outage, not the stage's logic. Retried as
            # infrastructure within ``retry.infra_retries``.
            return Observation(
                "failed_transient",
                failure_class=INFRA_SALAD_GUARDIAN_UNAVAILABLE,
                detail=(
                    f"{INFRA_SALAD_GUARDIAN_UNAVAILABLE}: the guardian was unavailable before "
                    "the group was started"
                ),
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        if deadline is not None:
            # The guardian stopped a run after an instance was seen because the route window
            # ran out: not a workload bug, and not retried here (retry policy is separate).
            return Observation(
                "failed_permanent",
                failure_class="route_deadline_reached",
                detail=(
                    f"route_deadline_reached: stopped at the route deadline after "
                    f"{deadline['running_seconds']}s running"
                )[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        failure_detail = str(attempt.get("failure_detail") or "")
        # The class is durable: before the runner records its observation the detail carries it;
        # afterwards the stored failure_class (and the ``salad:``-prefixed detail) do, so a
        # repeated observe of the same attempt gives the same answer.
        failure_detail = failure_detail.removeprefix("salad:")
        stored_class = attempt.get("failure_class")
        if stored_class == REALLOCATED_PRECUSTODY or failure_detail.startswith(
            REALLOCATED_PRECUSTODY
        ):
            # ADR 0179: moved by the provider before any custody work; a fresh attempt may retry.
            return Observation(
                "failed_transient",
                failure_class=REALLOCATED_PRECUSTODY,
                detail=f"salad:{failure_detail}"[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        if stored_class == SALAD_INTERRUPTED or failure_detail.startswith(SALAD_INTERRUPTED):
            # Moved after custody began, stop confirmed by the provider's own read: a fresh
            # attempt (new operation, own claim and hold) may retry within ``infra_retries``.
            return Observation(
                "failed_transient",
                failure_class=SALAD_INTERRUPTED,
                detail=f"salad:{failure_detail}"[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        if stored_class == INFRA_SALAD_REALLOCATED or failure_detail.startswith(
            INFRA_SALAD_REALLOCATED
        ):
            # The provider moved the instance (route flag, provider read and exited worker all
            # confirmed) without the guardian's custody word: infrastructure, retried within
            # ``retry.infra_retries`` by the runner.
            return Observation(
                "failed_transient",
                failure_class=INFRA_SALAD_REALLOCATED,
                detail=f"salad:{failure_detail}"[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        if self._guardian_record(rt, attempt, "retained_before_move") is True:
            # The run retained its output first; then the provider changed the allocation while
            # the group was stopping, so the guardian could only close the frame by the
            # provider-confirmed reconcile and never sealed custody (it cannot confirm a writer
            # died after a move). The outputs are not accepted; the provider's own move is
            # infrastructure, retried within ``retry.infra_retries``. Selected only by the
            # guardian's recorded final, never by the receipt's reason.
            return Observation(
                "failed_transient",
                failure_class=INFRA_SALAD_REALLOCATED,
                detail=(
                    f"{INFRA_SALAD_REALLOCATED}: the allocation changed while the group was "
                    "stopping after its output was retained; the provider confirmed the exact "
                    "group stopped; custody unsealed, outputs not accepted"
                )[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        safety_cause = self._guardian_record(rt, attempt, "safety_stop_cause")
        if safety_cause in SAFETY_STOP_CAUSES:
            # The safety plane stopped the route for its own health (silence, a restart, an
            # unreadable provider or rate, an uncertain clock, a failed loop pass). Only the
            # guardian's first stop cause on the executor's recorded final selects this, never
            # a receipt reason: infrastructure, retried within ``retry.infra_retries``.
            return Observation(
                "failed_transient",
                failure_class=INFRA_SALAD_SAFETY_STOP,
                detail=(
                    f"{INFRA_SALAD_SAFETY_STOP}: the Salad safety plane stopped the route "
                    f"({safety_cause}); not evidence about the stage"
                )[:500],
                evidence_kind="salad_live_receipt",
                evidence_digest=evidence,
                manifest_id=attempt.get("manifest_id"),
            )
        detail = f"salad:{failure_detail or 'not_passed'}"[:500]
        return Observation(
            "failed_permanent",
            failure_class="salad_not_passed",
            detail=detail,
            evidence_kind="salad_live_receipt",
            evidence_digest=evidence,
            manifest_id=attempt.get("manifest_id"),
        )

    def _guardian_unavailable_prestart(
        self, rt: RunnerServices, attempt: Mapping[str, Any]
    ) -> bool:
        """True only for a failed receipt that proves no start was dispatched (``_prestart_record``)
        and whose pre-start failure is a ticket mint or input delivery check that raised
        ``GuardianUnavailable``. Anything else, unreadable or older is False."""
        if rt is None or attempt.get("id") is None or attempt.get("campaign_id") is None:
            return False
        try:
            receipt = self._prestart_record(rt._one, attempt)
        except Exception:  # noqa: BLE001 - unreadable evidence is not proof
            return False
        record = None if receipt is None else receipt.get("prestart_failure")
        return (
            type(record) is dict
            and record.get("class") in GUARDIAN_UNAVAILABLE_PRESTART_CLASSES
            and record.get("exception") == "GuardianUnavailable"
            and receipt.get("create_rejection") is None
        )

    @staticmethod
    def _route_deadline(rt: RunnerServices, attempt: Mapping[str, Any]) -> dict[str, Any] | None:
        """The guardian's recorded route-deadline cut after an instance was seen, or None."""
        if rt is None or attempt.get("id") is None:
            return None
        row = rt._one(
            "SELECT guardian_final_json FROM campaign_salad_attempts WHERE attempt_id=?",
            (attempt["id"],),
        )
        if row is None or row["guardian_final_json"] is None:
            return None
        try:
            found = json.loads(row["guardian_final_json"]).get("route_deadline")
        except ValueError:
            return None
        if (
            type(found) is dict
            and type(found.get("running_seconds")) is int
            and found.get("limit") == "route_deadline"
        ):
            return found  # a declared-run cap overrun is the workload's, not the route's
        return None

    @staticmethod
    def _guardian_record(rt: RunnerServices, attempt: Mapping[str, Any], key: str) -> Any:
        """One key of the executor's recorded guardian final for this attempt, or None."""
        if rt is None or attempt.get("id") is None:
            return None
        row = rt._one(
            "SELECT guardian_final_json FROM campaign_salad_attempts WHERE attempt_id=?",
            (attempt["id"],),
        )
        if row is None or row["guardian_final_json"] is None:
            return None
        try:
            return json.loads(row["guardian_final_json"]).get(key)
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _allocation_timeout(
        rt: RunnerServices, attempt: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The guardian's recorded allocation timeout for this attempt, or None."""
        if rt is None or attempt.get("id") is None:
            return None  # nothing to look up: the verdict alone decides, as before
        row = rt._one(
            "SELECT guardian_final_json FROM campaign_salad_attempts WHERE attempt_id=?",
            (attempt["id"],),
        )
        if row is None or row["guardian_final_json"] is None:
            return None
        try:
            found = json.loads(row["guardian_final_json"]).get("allocation_timeout")
        except ValueError:
            return None
        if (
            type(found) is dict
            and type(found.get("elapsed_seconds")) is int
            and type(found.get("deadline_seconds")) is int
        ):
            return found
        return None

    # -- cancel -----------------------------------------------------------------------------

    def cancel(self, rt: RunnerServices, ctx: Any, key: str, attempt: Mapping) -> bool:
        return self._translate(lambda: self.driver(rt).unwind(ctx, key, dict(attempt)))

    # -- cost / settle ----------------------------------------------------------------------

    def settle(self, rt: RunnerServices, attempt: Mapping[str, Any]) -> Settlement:
        """Never zero without an unclaimed proof; waits for a charge otherwise.

        Basis values are ADR 0038's: ``unclaimed_zero``, ``human_reconcile``,
        ``human_charge`` and ``guardian_bounded_hold`` (provisional, so not final)."""
        row = dict(attempt)
        cents, basis = self._translate(lambda: self.driver(rt).settlement_for(None, row))
        return Settlement(cents=cents, basis=basis, final=basis != "guardian_bounded_hold")

    def exposure_open(self, connection: sqlite3.Connection, campaign_id: str) -> bool:
        return SaladStageDriver.exposure_open(None, connection, campaign_id)  # type: ignore[arg-type]

    def after_tick(self, rt: RunnerServices, ctx: Any) -> None:
        """Late charges are added once by CAS; bounded-hold settlements get confirmed."""
        if ctx is None:
            self.sweep(rt)
            return
        self._translate(lambda: self.driver(rt).late_charges(ctx))

    def evidence_pass(self, rt: RunnerServices, ctx: Any) -> None:
        """Evidence-only Salad work: verify recorded (or late-imported) outcomes and cancel the
        route of an attempt already unknown that was provably never sent (v0.104.0)."""
        self._translate(lambda: self.driver(rt).evidence_pass(ctx))

    def on_settled(
        self, connection: sqlite3.Connection, attempt_id: str, cents: int, stamp: str
    ) -> None:
        """Runs in the settle transaction: record the charge and close the Salad phase.

        An attempt proven unclaimed keeps that phase; any other becomes ``settled``."""
        connection.execute(
            """UPDATE campaign_salad_attempts SET charged_cents=?,
            phase=CASE WHEN phase='unclaimed' THEN phase ELSE 'settled' END,
            revision=revision+1, updated_at=? WHERE attempt_id=?""",
            (cents, stamp, attempt_id),
        )

    # -- unclaimed attempts (ADR 0157) -------------------------------------------------------

    def create_claim_state(
        self, connection: sqlite3.Connection, attempt: Mapping[str, Any]
    ) -> CreateClaim:
        """Whether a Salad create or start was claimed for the attempt's grant; a pure read.

        ``none`` rests on the same proof the operator's reconcile-unclaimed route checks
        (``salad_unclaimed.unclaimed_proof``) plus this attempt's own Salad record showing no
        provider id, receipt, manifest, evidence or charge and no phase past the route start."""
        row = connection.execute(
            "SELECT * FROM campaign_salad_attempts WHERE attempt_id=?", (attempt["id"],)
        ).fetchone()
        if row is None:
            return CreateClaim("unknown", "no_salad_record")
        if row["provider_id"] is not None:
            return CreateClaim("claimed", "provider_reference")
        if any(
            row[name] is not None
            for name in ("manifest_id", "salad_evidence_digest", "charged_cents")
        ) or (row["live_receipt_digest"] is not None and not _receipt_sent_nothing(row)):
            return CreateClaim("unknown", "salad_record_has_evidence")
        if row["phase"] in UNCLAIMABLE_PHASES:
            return CreateClaim("unknown", "salad_phase")
        org = connection.execute(
            "SELECT org_id FROM campaigns WHERE id=?", (attempt["campaign_id"],)
        ).fetchone()
        if org is None or attempt["grant_id"] is None:
            return CreateClaim("unknown", "no_grant")
        refusal, proof = unclaimed_proof(connection, org["org_id"], attempt["grant_id"])
        if refusal is not None:
            return CreateClaim("claimed" if refusal in CLAIMS else "unknown", refusal)
        if row["operation_id"] not in (None, proof["operation_id"]):
            return CreateClaim("unknown", "operation_mismatch")
        return CreateClaim(
            "none",
            "no_create_or_start_claim",
            proof=PROOF,
            evidence={**proof, "phase": row["phase"]},
        )

    def mark_unclaimed(self, connection: sqlite3.Connection, attempt_id: str, stamp: str) -> None:
        """The Salad record keeps the unclaimed phase through the zero settlement."""
        connection.execute(
            """UPDATE campaign_salad_attempts SET phase='unclaimed', revision=revision+1,
            updated_at=? WHERE attempt_id=?""",
            (stamp, attempt_id),
        )

    # -- class fallback (ADR 0060 addendum) ------------------------------------------------

    def fallback_classes(self, stage: Any) -> int:
        """The stage's profile and its ``alternate_profiles`` (ADR 0151), then the operator
        ``fallback_profiles`` chain of those profiles (none unless the operator file names
        one): one class each. Moves stay bounded by ``retry.max_class_fallbacks``."""
        config = _salad_config(stage)
        if config is None:
            return 1
        return len(profile_order(config)) + len(operator_chain(config, self.profiles))

    def operator_fallback_refusal(
        self, connection: sqlite3.Connection, project_id: str, stage: Any
    ) -> str | None:
        """A closed code when ``stage`` (the approved stage on an operator fallback profile,
        ``stage_on_operator_profile``) may not run there, else None. Read-only; fails closed.

        The cost bound first: the stage's approved per-run hold (``max_exposure_cents``) must be
        within the profile's ``max_run_cents`` (``over_run_cap``); for a CPU profile the operator
        rate over the stage's whole ``max_duration_seconds`` (the CPU worst case ``check_stage``
        computes) must be within that hold, and the hold of every attempt the approved worst case
        allows (``retry.hold_attempts``) within the profile's ``max_task_cents``
        (``fallback_over_hold``). Then every other check an approved profile passes at submit
        (``_check_profile``: tenant, worker, kind, credential, input trust and public-artifact
        guards, image allowlist): any refusal is ``fallback_refused``."""
        config = _salad_config(stage)
        profiles = self.profiles
        profile = None if config is None or profiles is None else profiles.get(config.profile)
        if profile is None:
            return "profile_unavailable"
        try:
            hold = int(stage.max_exposure_cents)
            if hold > profile.max_run_cents:
                return "over_run_cap"
            if config.gpu or profile.gpu:
                return "fallback_refused"  # operator fallbacks are CPU-only (static rates)
            if profile.estimated_cents(int(stage.max_duration_seconds)) > hold:
                return "fallback_over_hold"
            if hold * int(stage.retry.hold_attempts) > profile.max_task_cents:
                return "fallback_over_hold"
            self._check_profile(connection, project_id, stage, config, config.profile)
        except Conflict:
            return "fallback_refused"
        except Exception:  # noqa: BLE001 - anything unreadable is not covered
            return "fallback_refused"
        return None

    def prestart_failure(
        self, connection: sqlite3.Connection, attempt: Mapping[str, Any]
    ) -> str | None:
        """A closed reason when this attempt provably never started, else None.

        Proof, all from control's own records: the attempt's route (read within the campaign's
        own organization and project) carries no ``start`` effect (an effect is committed as
        an intent before the provider is called, so none means the start call never happened)
        and was never reallocated, and the executor's live receipt did not pass and names the
        pre-start step that failed (or the provider's create rejection)."""
        receipt = self._prestart_record(
            lambda sql, params: connection.execute(sql, params).fetchone(), attempt
        )
        if receipt is None:
            return None
        if receipt.get("create_rejection") is not None:
            return "create_rejected"
        failure = receipt.get("prestart_failure")
        code = failure.get("class") if isinstance(failure, dict) else None
        return code if code in PRESTART_FAILURE_CLASSES else None

    @staticmethod
    def _prestart_record(fetch: Any, attempt: Mapping[str, Any]) -> dict[str, Any] | None:
        """The attempt's failed live receipt when control's own records prove it never started
        (the route, read within the campaign's organization and project, carries no ``start``
        effect and was never reallocated), else None. ``fetch(sql, params)`` returns one row."""
        row = fetch(
            "SELECT route_id, live_receipt_json FROM campaign_salad_attempts WHERE attempt_id=?",
            (attempt["id"],),
        )
        if row is None or row["route_id"] is None or row["live_receipt_json"] is None:
            return None
        found = fetch(
            """SELECT e.payload_json FROM events e JOIN campaigns c ON c.id=?
            WHERE e.event_type='remote_execution.state' AND e.org_id=c.org_id
            AND e.project_id=c.project_id AND json_extract(e.payload_json,'$.id')=?
            ORDER BY e.cursor DESC LIMIT 1""",
            (attempt["campaign_id"], row["route_id"]),
        )
        if found is None:
            return None
        state = json.loads(found["payload_json"])
        effects = state.get("effects")
        if (
            not isinstance(effects, list)
            or state.get("reallocated")
            or any(not isinstance(e, dict) or e.get("action") == "start" for e in effects)
        ):
            return None
        receipt = json.loads(row["live_receipt_json"])
        if not isinstance(receipt, dict) or receipt.get("status") == "passed":
            return None
        return receipt

    def cpu_profile(self, stage: Any) -> bool:
        """True only when ``stage``'s Salad block and its operator profile are both CPU (no
        GPU class); anything unreadable is False. Read-only."""
        try:
            config = _salad_config(stage)
            profile = (
                None
                if config is None or self.profiles is None
                else (self.profiles.get(config.profile))
            )
            return profile is not None and not config.gpu and not profile.gpu
        except Exception:  # noqa: BLE001 - unreadable is not a CPU profile
            return False

    def allocation_timeout_evidence(
        self, connection: sqlite3.Connection, attempt: Mapping[str, Any], stage: Any
    ) -> dict[str, Any] | None:
        """Closed evidence that this attempt's started group never got an instance and accrued
        nothing, else None. Read-only; every missing, malformed or ambiguous record is None.

        ``stage`` is the approved stage on the profile the attempt ran on. All of, from
        control's own records (the one exception to ADR 0169 point 3 reads only these):

        - the stage runs on a CPU profile (``cpu_profile``; GPU stages keep the strict rule);
        - the attempt's verdict is ``failed`` and it settled under ``guardian_bounded_hold``,
          which the settlement path records only after control's own guardian read said
          ``stopped`` (``SaladStageDriver._read_verdict``), and its frame carries that read's
          verdict digest;
        - the executor's recorded guardian final validates (``_GuardianFinal``), carries the
          guardian's own ``allocation_timeout`` (its deadline fired: by the guardian's rules no
          instance, allocation or running time was ever seen), status ``stopped``, no orphan,
          uncertain allocation, pending escalation, budget fence or unknown rate, no billed
          cost, no pre-start re-bind (an allocation once existed) and an estimate that parses
          to exactly zero;
        - the attempt's route (read within the campaign's own organization and project) was
          never reallocated, and the executor's live receipt did not pass."""
        from decimal import Decimal, InvalidOperation

        from pydantic import ValidationError

        from . import execution_frames as frames
        from .campaign_salad_commands import _GuardianFinal

        if not self.cpu_profile(stage):
            return None
        if attempt.get("salad_verdict") != "failed":
            return None
        if attempt.get("settlement_basis") != "guardian_bounded_hold":
            return None
        if type(attempt.get("settled_cents")) is not int:
            return None
        row = connection.execute(
            """SELECT sa.route_id, sa.live_receipt_json, sa.guardian_final_json, c.org_id,
            c.project_id FROM campaign_salad_attempts sa
            JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
            JOIN campaigns c ON c.id = a.campaign_id
            WHERE sa.attempt_id=? AND a.campaign_id=?""",
            (attempt["id"], attempt["campaign_id"]),
        ).fetchone()
        if (
            row is None
            or row["route_id"] is None
            or row["live_receipt_json"] is None
            or row["guardian_final_json"] is None
        ):
            return None
        try:
            final = _GuardianFinal.model_validate_json(row["guardian_final_json"])
            estimate = Decimal(final.estimated_exposure_cents or "")
        except (ValidationError, ValueError, InvalidOperation):
            return None
        timeout = final.allocation_timeout
        if (
            timeout is None
            or final.status != "stopped"
            or final.orphan
            or final.allocation_uncertain
            or final.escalation_pending
            or final.budget_fenced
            or final.rate_unknown
            or final.actual_billed_cost_cents is not None
            # A group the guardian re-bound to a new allocation was allocated once: never "never
            # allocated", whatever else its final says.
            or final.prestart_rebinds is not None
            or not estimate.is_finite()
            or estimate != 0
        ):
            return None
        frame = frames.frame_for_attempt(
            connection, attempt["id"], frames.Tenant(row["org_id"], row["project_id"])
        )
        if frame is None or frame.get("guardian_verdict_digest") is None:
            return None
        found = connection.execute(
            """SELECT payload_json FROM events WHERE event_type='remote_execution.state'
            AND org_id=? AND project_id=? AND json_extract(payload_json,'$.id')=?
            ORDER BY cursor DESC LIMIT 1""",
            (row["org_id"], row["project_id"], row["route_id"]),
        ).fetchone()
        if found is None:
            return None
        try:
            state = json.loads(found["payload_json"])
            receipt = json.loads(row["live_receipt_json"])
        except ValueError:
            return None
        if not isinstance(state, dict) or state.get("reallocated", False) is not False:
            return None
        if not isinstance(receipt, dict) or receipt.get("status") in (None, "passed"):
            return None
        return {
            "route_id": row["route_id"],
            "guardian_status": final.status,
            "guardian_verdict_digest": frame["guardian_verdict_digest"],
            "allocation_timeout": {
                "elapsed_seconds": timeout.elapsed_seconds,
                "deadline_seconds": timeout.deadline_seconds,
            },
        }

    def placement_capability(self, stage: Any) -> PlacementCapability:
        """Rented capacity; CPU or GPU as the stage's own Salad block says (ADR 0088)."""
        return PlacementCapability("remote", "gpu" if salad_config(stage).gpu else "cpu")

    def allocation_class(self, stage: Any) -> str | None:
        """The GPU class this stage's operator profile requests (ADR 0148), or None for a CPU
        profile, an unknown profile or no profiles. Read-only; never raises."""
        if self.profiles is None:
            return None
        try:
            profile = self.profiles.get(salad_config(stage).profile)
        except Exception:  # noqa: BLE001 - a malformed stage block has no class
            return None
        return None if profile is None else profile.gpu_class

    def sweep(self, rt: RunnerServices) -> None:
        """Confirm bounded-hold settlements of terminal campaigns (events only)."""
        self.driver(rt).sweep_unconfirmed()

    # -- receipts ---------------------------------------------------------------------------

    def receipt_fields(
        self, connection: sqlite3.Connection, attempt: Mapping[str, Any]
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT phase, route_id, guardian_final_json FROM campaign_salad_attempts "
            "WHERE attempt_id=?",
            (attempt["id"],),
        ).fetchone()
        estimate = None
        if row is not None and row["guardian_final_json"] is not None:
            final = json.loads(row["guardian_final_json"])
            # The recorded guardian estimate, labelled as not an invoice.
            estimate = {
                "estimated_exposure_cents": final.get("estimated_exposure_cents"),
                "published_hourly_rate_usd": final.get("published_hourly_rate_usd"),
                "label": "estimate_not_invoice",
            }
        fields = {
            "salad_verdict": attempt["salad_verdict"],
            "salad_evidence_digest": attempt["salad_evidence_digest"],
            "salad_phase": row["phase"] if row else None,
            "salad_route_id": row["route_id"] if row else None,
            "guardian_estimate": estimate,
        }
        if row is not None and row["guardian_final_json"] is not None:
            timeout = final.get("allocation_timeout")
            if timeout:
                # Reason, how long the guardian waited, and (guardian_estimate above) the spend
                # so far; absent unless the guardian cancelled for lack of an allocation.
                fields["allocation_timeout"] = {"reason": "allocation_timeout", **timeout}
            for key in ("start_failure", "start_events"):
                if final.get(key):
                    # Closed tokens only (event names, exit codes, machine count).
                    fields["guardian_" + key] = final[key]
            exited = container_exit_receipt(final.get("start_events"))
            if exited is not None:
                fields["guardian_container_exit"] = exited
            rebinds = final.get("prestart_rebinds")
            if rebinds:
                # Recorded by the guardian: an allocation change before anything ran, re-bound.
                fields["guardian_prestart_rebinds"] = rebinds
        termination = self._termination(connection, attempt, row)
        if termination is not None:
            # ADR 0149: only on a reallocated route, so every other receipt keeps its shape.
            fields["salad_termination"] = termination
        bound = self._late_bound_inputs(connection, attempt)
        if bound is not None:
            fields["late_bound_inputs"] = bound
        return fields

    @staticmethod
    def _termination(
        connection: sqlite3.Connection, attempt: Mapping[str, Any], row: Any
    ) -> dict[str, Any] | None:
        """Control's own reading of the route's provider readbacks, tenant-scoped."""
        from .salad_termination import provider_termination

        if row is None or row["route_id"] is None:
            return None
        found = connection.execute(
            """SELECT e.payload_json FROM events e JOIN campaigns c ON c.id=?
            WHERE e.event_type='remote_execution.state' AND e.org_id=c.org_id
            AND e.project_id=c.project_id AND json_extract(e.payload_json,'$.id')=?
            ORDER BY e.cursor DESC LIMIT 1""",
            (attempt.get("campaign_id"), row["route_id"]),
        ).fetchone()
        if found is None:
            return None
        reading = provider_termination(json.loads(found["payload_json"]))
        if reading.status == "not_applicable":
            return None
        view = reading.as_dict()
        view["hold_released"] = attempt.get("settled_cents") is not None
        return view

    @staticmethod
    def _late_bound_inputs(
        connection: sqlite3.Connection, attempt: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The stage-output inputs the driver resolved and checked for this attempt's stage
        (source stage, output name, sha256, bytes), with the composed snapshot digest the
        route named; None for a stage without late-bound inputs. Read from the recorded event,
        scoped to the campaign's project."""
        campaign = connection.execute(
            "SELECT project_id FROM campaigns WHERE id=?", (attempt["campaign_id"],)
        ).fetchone()
        if campaign is None:
            return None
        found = connection.execute(
            """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
            AND json_extract(payload_json, '$.campaign_id') = ?
            AND json_extract(payload_json, '$.stage_key') = ?
            ORDER BY cursor DESC LIMIT 1""",
            (
                campaign["project_id"],
                LATE_BOUND_EVENT,
                attempt["campaign_id"],
                attempt["stage_key"],
            ),
        ).fetchone()
        if found is None:
            return None
        payload = json.loads(found["payload_json"])
        return {
            "snapshot_digest": payload.get("snapshot_digest"),
            "snapshot_bytes": payload.get("snapshot_bytes"),
            "max_bytes": payload.get("max_bytes"),
            "inputs": [
                {
                    name: item.get(name)
                    for name in ("name", "source_stage", "output", "digest", "bytes", "data_class")
                }
                for item in payload.get("inputs", [])
            ],
        }

    @staticmethod
    def _translate(call):
        """The runner's wait signal is the protocol's ``Waiting`` (one type), so nothing
        needs translating; the hook stays for the driver call sites."""
        return call()


_REGISTERED: list[bool] = []


def register_provider_instance(
    profiles: SaladCampaignProfiles | None = None,
    credential_directory: str | None = None,
    guardian_reader: Any = None,
) -> ComputeProvider:
    """Register (or replace, when this module registered it before) the ``salad`` provider."""
    candidate = SaladRouteProvider(profiles, credential_directory, guardian_reader)
    instance = replace_provider(candidate) if _REGISTERED else register_provider(candidate)
    if not _REGISTERED:
        _REGISTERED.append(True)
    return instance


def register_from_config(config: dict | None) -> None:
    """Register ``salad`` from the operator's ``--campaign-providers`` document.

    ``salad_profiles`` holds a ``tessera.salad.campaign-profiles.v1`` document. Without
    it the provider is registered and refuses every Salad stage as not enabled. Calling
    it again replaces the instance; the id may not belong to another provider.
    """
    profiles = None
    if config and config.get("salad_profiles") is not None:
        profiles = SaladCampaignProfiles.model_validate(config["salad_profiles"])
    if not _REGISTERED:
        try:
            get_provider(PROVIDER_ID)
        except KeyError:
            pass
        else:
            raise ValueError(f"Provider id {PROVIDER_ID!r} is already registered")
    directory = (config or {}).get("salad_credential_directory")
    if directory is not None and (not isinstance(directory, str) or not directory.startswith("/")):
        raise ValueError("salad_credential_directory must be an absolute path")
    reader = None
    guardian_read = (config or {}).get("salad_guardian_read")
    if guardian_read is not None:
        if not isinstance(guardian_read, str) or not guardian_read.startswith("/"):
            raise ValueError("salad_guardian_read must be an absolute path")
        from .guardian_record_reader import build_https_reader

        reader = build_https_reader(guardian_read)
    register_provider_instance(profiles, directory, reader)


def config_sections() -> tuple[ConfigSection, ...]:
    """Salad campaign profiles in the operator file (ADR 0081)."""

    def validate(document: dict) -> None:
        if document.get("salad_profiles") is not None:
            SaladCampaignProfiles.model_validate(document["salad_profiles"])

    def describe(entry: dict) -> dict:
        profile = SaladCampaignProfile.model_validate(entry)
        return {
            "resource_class": "gpu" if profile.gpu else "cpu",
            "gpu_class": profile.gpu_class,
            "cpu_millicores": profile.cpu_millicores,
            "memory_mb": profile.memory_mb,
            "disk_mb": profile.disk_mb,
            "route_max_seconds": profile.route_max_seconds,
            "max_run_cents": profile.max_run_cents,
            "max_task_cents": profile.max_task_cents,
            "allowed_image_digests": list(profile.allowed_image_digests),
            "max_image_pull_bytes": profile.max_image_pull_bytes,
            "strict_image_pull": profile.strict_image_pull,
            "credential_reference": profile.credential_reference,
            "custody_host": profile.custody_host,
            "stage_adapter": getattr(profile, "stage_adapter", None) is not None,
        }

    return (
        ConfigSection(
            name="salad-profile",
            provider=PROVIDER_ID,
            path=("salad_profiles", "profiles"),
            key_field="name",
            model=SaladCampaignProfile,
            validate=validate,
            describe=describe,
            worker=lambda entry: entry.get("worker_principal_id"),
            container=lambda: {"schema": PROFILES_SCHEMA, "profiles": []},
            scope="organization",
        ),
    )


try:
    get_provider(PROVIDER_ID)
except KeyError:
    register_from_config(None)
