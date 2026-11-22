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
from collections.abc import Mapping
from typing import Any

from . import provider_trust
from .campaign_delegation import STEP_TABLE
from .campaign_models import SALAD_BACKEND, SALAD_RUNTIME, SALAD_TARGET, SaladStageSpec
from .campaign_providers import (
    ComputeProvider,
    GrantRequest,
    Observation,
    OutputLimits,
    PrepareResult,
    ProviderDescriptor,
    RunnerServices,
    Settlement,
    get_provider,
    register_provider,
    unregister_provider,
)
from .campaign_salad import SaladStageDriver, salad_config
from .campaign_store import SALAD_REFUSAL
from .models import ExecutionSpec
from .salad_campaign_profiles import (
    SaladCampaignProfiles,
    stage_has_nonpublic_input,
)
from .salad_workload_contract import COMMAND as SALAD_COMMAND
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
# evidence-row steps (ADR 0049, ADR 0052) are not delegated to them.
DELEGABLE_STEPS = frozenset(STEP_TABLE) - {
    "bind_split",
    "record_gate_metric",
    "ingest_evidence_rows",
}


def _salad_config(stage: Any) -> SaladStageSpec | None:
    """The stage's Salad fields, or None when the stage carries none."""
    found = getattr(stage, "salad", None) or getattr(stage, "provider_config", None)
    return salad_config(stage) if found else None


def _hosts_declared(config: SaladStageSpec) -> bool:
    egress = config.workload.egress
    return bool(egress.fetches or egress.redirect_hosts)


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
    )
    config_model = SaladStageSpec

    def __init__(self, profiles: SaladCampaignProfiles | None = None) -> None:
        self.profiles = profiles

    def driver(self, rt: RunnerServices) -> SaladStageDriver:
        """One driver per runner services object. It lives in the runner's own
        ``provider_state``, so it is evicted with the runner (a provider-held map would
        keep both alive: the driver references its runner)."""
        driver = rt.provider_state.get(PROVIDER_ID)
        if driver is None or driver._profiles is not self.profiles:
            driver = rt.provider_state[PROVIDER_ID] = SaladStageDriver(rt, self.profiles)
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
        if stage.retry.transient_exit_codes:
            raise ValueError(f"{label}: Salad stages have no exit code to classify as transient")
        if stage.max_exposure_cents < 1:
            raise ValueError(f"{label}: a Salad stage needs max_exposure_cents >= 1")
        if list(stage.argv) != [SALAD_COMMAND, *workload.argv_template]:
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
        if stage.max_duration_seconds > 3600 or len(stage.argv) > 32:
            raise ValueError(f"{label}: max_duration_seconds <= 3600 and at most 32 argv items")
        if stage_has_nonpublic_input(stage) and _hosts_declared(config):
            raise ValueError(
                f"{label}: a stage with non-public input cannot declare workload fetch or "
                "redirect hosts"
            )

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
        self._check_worker(connection, project["org_id"], project_id, config.worker_principal_id)
        if _hosts_declared(config) and any(
            provider_trust.admission_basis(connection, source.snapshot_id) != "public_only"
            for source in stage.inputs
            if source.kind == "snapshot"
        ):
            raise Conflict(
                "Salad stage input is not public (or carries approved code) and cannot "
                "declare workload fetch or redirect hosts."
            )
        if not profile.image_allowed(stage.image):
            raise Conflict("Salad stage image digest is not allowlisted by the operator profile.")
        if stage.max_exposure_cents > profile.max_run_cents:
            raise Conflict("Salad stage exposure exceeds the profile's per-run ceiling.")
        if stage.max_exposure_cents * stage.retry.max_attempts > profile.max_task_cents:
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
        return ExecutionSpec(
            argv=list(stage.argv),
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
        return {
            "salad_verdict": attempt["salad_verdict"],
            "salad_evidence_digest": attempt["salad_evidence_digest"],
            "salad_phase": row["phase"] if row else None,
            "salad_route_id": row["route_id"] if row else None,
            "guardian_estimate": estimate,
        }

    @staticmethod
    def _translate(call):
        """The runner's wait signal is the protocol's ``Waiting`` (one type), so nothing
        needs translating; the hook stays for the driver call sites."""
        return call()


_REGISTERED: list[bool] = []


def register_provider_instance(
    profiles: SaladCampaignProfiles | None = None,
) -> ComputeProvider:
    """Register (or replace, when this module registered it before) the ``salad`` provider."""
    if _REGISTERED:
        unregister_provider(PROVIDER_ID)
    instance = register_provider(SaladRouteProvider(profiles))
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
    register_provider_instance(profiles)


try:
    get_provider(PROVIDER_ID)
except KeyError:
    register_from_config(None)
