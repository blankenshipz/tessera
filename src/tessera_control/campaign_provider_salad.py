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
from .salad_campaign_profiles import (
    PROFILES_SCHEMA,
    SaladCampaignProfile,
    SaladCampaignProfiles,
    stage_has_nonpublic_input,
)
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
UNCLAIMABLE_PHASES = frozenset(
    {"executing", "outcome_recorded", "awaiting_charge", "settled", "unclaimed"}
)


class CredentialBlocked(Conflict):
    """A 409 for a stage whose provider credential cannot be used (placement skips such a
    candidate as ``credential_unavailable``; submit and approval show the message)."""

    def __init__(self, message: str, state: dict[str, Any]) -> None:
        super().__init__(message)
        self.credential_state = state


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

    def _check_credential(self, profile: Any) -> None:
        """Refuse up front when the profile's credential is expired, revoked or pinned at a
        version that is not the enrolled one (metadata only; unknown or warning never refuses)."""
        state_of = getattr(self, "credential_state", None)
        state = state_of(profile) if state_of is not None else None
        if credential_blocked(state):
            raise CredentialBlocked(credential_message(state), state)

    def check_stage(self, connection: sqlite3.Connection, project_id: str, stage: Any) -> None:
        """Operator profile ceilings for one Salad stage; every refusal is a 409.

        Input admission (data class and trust) is the core's ``check_stage_inputs``."""
        profiles = self.profiles
        if profiles is None or not profiles.profiles:
            raise Conflict(SALAD_REFUSAL)
        config = _salad_config(stage)
        profile = profiles.get(config.profile)
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
        # Every attempt holds its own exposure, an enabled infrastructure retry (ADR 0058)
        # included: the operator's billable per-task ceiling counts them all.
        attempts = stage.retry.max_attempts + stage.retry.infra_retries
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
        detail = f"salad:{attempt.get('failure_detail') or 'not_passed'}"[:500]
        return Observation(
            "failed_permanent",
            failure_class="salad_not_passed",
            detail=detail,
            evidence_kind="salad_live_receipt",
            evidence_digest=evidence,
            manifest_id=attempt.get("manifest_id"),
        )

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
            for name in (
                "live_receipt_digest",
                "manifest_id",
                "salad_evidence_digest",
                "charged_cents",
            )
        ):
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

    def placement_capability(self, stage: Any) -> PlacementCapability:
        """Rented capacity; CPU or GPU as the stage's own Salad block says (ADR 0088)."""
        return PlacementCapability("remote", "gpu" if salad_config(stage).gpu else "cpu")

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
            rebinds = json.loads(row["guardian_final_json"]).get("prestart_rebinds")
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
