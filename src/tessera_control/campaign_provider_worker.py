"""Worker-grant compute provider: ``local-docker`` and operator-listed ``self-hosted`` instances.

Every instance is the same code (ADR 0040 section 2): the runner issues an ordinary
execution grant to the instance's worker principal, and ``tessera-worker development-poll``
leases and runs it (``docker-cpu-v1``). Nothing here starts a container or touches a
provider; observation, cancel and settlement read and drive the grant tables through the
runner facade. The built-in ``local-docker`` instance exists without a file. Trust is never
granted here: only the provider trust setting binds a worker principal to data classes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .artifact_limits import ABSOLUTE_MAX_ARTIFACT_BYTES, operator_limits
from .campaign_models import (
    BUILD_RUNTIME,
    DOCKER_CPU_RUNTIME,
    LOCAL_DOCKER_PROVIDER,
    NormalStage,
    build_policy,
    public_fetch_policy,
)
from .campaign_providers import (
    MAX_SCRATCH_BYTES,
    MAX_WORKER_SLOTS,
    ConfigSection,
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
from .campaign_steps import StepFailed
from .data_class import PUBLIC
from .egress_models import EgressPolicy
from .image_build_models import RegisteredRepository, repository_registered
from .models import ExecutionSpec
from .store import Conflict, Forbidden, Store

# Outputs above 16 MiB use chunked large-artifact custody; the operator limit applies.
MAX_OUTPUT_TOTAL_BYTES = operator_limits().max_artifact_bytes
SELF_HOSTED_FILE_SCHEMA = "tessera.campaign-providers.v1"
# ADR 0038 section 2: every delegable step except the Salad-only ones.
WORKER_STEPS = frozenset(
    {
        "create_task",
        "revise_plan",
        "approve_task",
        "compose_snapshot",
        "release_snapshot",
        "revoke_release",
        "create_experiment",
        "authorize_budget",
        "issue_grant",
        "admit_attempt",
        "revoke_grant",
        "reconcile_grant",
        "reconcile_unattempted",
        "record_disposition",
        "custody_release",
        "resolve_attempt",
        "link_artifacts",
        "derive_measurement",
        "bind_split",
        "record_gate_metric",
        "ingest_evidence_rows",
        "register_lineage_node",
        "record_lineage_edge",
    }
)
_RESERVED_IDS = frozenset({LOCAL_DOCKER_PROVIDER, "salad"})


class WorkerGrantProvider:
    """One worker-grant provider instance; ``descriptor`` carries its worker principal."""

    config_model = None

    def __init__(self, descriptor: ProviderDescriptor) -> None:
        self.descriptor = descriptor

    # -- submit / approve -------------------------------------------------------------

    def worker_for(self, stage: NormalStage) -> str | None:
        return stage.worker_principal_id or self.descriptor.worker_principal_id or None

    def validate_stage(self, stage: NormalStage) -> None:
        if stage.env is not None:
            raise ValueError(f"Stage {stage.key}: env is not supported for {DOCKER_CPU_RUNTIME}")

    # A worker-grant provider carries a stage's scratch request to its worker (ADR 0059).
    grants_scratch = True

    def check_stage(
        self, connection: sqlite3.Connection, project_id: str, stage: NormalStage
    ) -> None:
        ceiling = self.descriptor.max_scratch_bytes
        if stage.scratch_bytes is not None and stage.scratch_bytes > ceiling:
            raise Conflict(
                f"Stage {stage.key!r}: scratch_exceeds_operator_ceiling: scratch_bytes "
                f"{stage.scratch_bytes} is above the {ceiling} bytes provider "
                f"{self.descriptor.provider_id!r} is configured to grant; lower scratch_bytes "
                "or have the operator raise that provider's max_scratch_bytes (and the "
                "worker's own ceiling)."
            )
        build = build_policy(stage)
        if (
            build is not None
            and build["push"]
            and not repository_registered(build["repository"], self.descriptor.build_repositories)
        ):
            raise Conflict(
                f"Stage {stage.key!r}: build_repository_not_registered: repository "
                f"{build['repository']!r} is not a registry destination the operator registered "
                f"for provider {self.descriptor.provider_id!r} (build_repositories)."
            )
        worker_id = self.worker_for(stage)
        instance = self.descriptor.worker_principal_id
        if worker_id is None:
            raise Conflict(f"Provider {self.descriptor.provider_id} has no worker principal.")
        if instance and worker_id != instance:
            raise Conflict(f"Stage {stage.key}: worker is not provider's configured worker.")
        project = connection.execute(
            "SELECT org_id FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise Conflict("Campaign project was not found.")
        try:
            worker = Store._live_principal(
                connection, {"id": worker_id, "org_id": project["org_id"]}, audience="worker"
            )
        except Forbidden:
            raise Conflict("Campaign worker is not registered.") from None
        if (
            worker["kind"] != "worker"
            or worker["role"] != "agent"
            or worker["project_id"] != project_id
        ):
            raise Conflict("Campaign worker is not authorized for this project.")

    @staticmethod
    def _build_plan(stage: NormalStage) -> tuple[dict[str, Any], list[str], EgressPolicy]:
        """The image-build policy, the builder's derived argv and its egress (ADR 0123).

        Fails closed (ADR 0047: private data never gets egress): the build's only input must be
        its pinned public snapshot context, whatever else the spec validation already checked."""
        policy = build_policy(stage)
        if policy is None or stage.runtime != BUILD_RUNTIME:
            raise ValueError(f"Stage {stage.key}: {BUILD_RUNTIME} runs only a build stage")
        context = [i for i in stage.inputs if i.name == policy["context_input"]]
        if (
            len(stage.inputs) != 1
            or len(context) != 1
            or context[0].kind != "snapshot"
            or context[0].data_class != PUBLIC
        ):
            raise ValueError(
                f"Stage {stage.key}: an image build takes only its public snapshot build context"
            )
        if stage.egress is not None or stage.env is not None:
            raise ValueError(f"Stage {stage.key}: a build stage derives its own egress, no env")
        from tessera_worker.image_build import builder_argv

        egress = EgressPolicy(
            profile="allowlist-proxy-v1",
            destinations=[{"host": host, "port": 443} for host in policy["hosts"]],
            max_egress_bytes=policy["max_transfer_bytes"],
        )
        return policy, list(builder_argv(policy)), egress

    def execution_spec(self, stage: NormalStage, input_digest: str) -> dict[str, Any]:
        if stage.build is not None or stage.runtime == BUILD_RUNTIME:
            policy, argv, egress = self._build_plan(stage)
            if policy["push"] and not repository_registered(
                policy["repository"], self.descriptor.build_repositories
            ):
                raise ValueError(
                    f"Stage {stage.key}: build_repository_not_registered: "
                    f"{policy['repository']!r} is not a registered destination of provider "
                    f"{self.descriptor.provider_id!r}"
                )
            return ExecutionSpec(
                argv=argv,
                input_snapshot_digest=input_digest,
                pinned_skill_digests=[],
                output_paths=stage.output_paths,
                profile="local-cpu",
                egress=egress,
                network_enabled=False,
                max_output_bytes=stage.max_output_bytes,
                max_scratch_bytes=stage.scratch_bytes,
                build=policy,
            ).model_dump(mode="json")
        return ExecutionSpec(
            argv=list(stage.argv),
            input_snapshot_digest=input_digest,
            pinned_skill_digests=[],
            output_paths=stage.output_paths,
            profile="local-cpu",
            egress=stage.egress,
            network_enabled=False,
            max_output_bytes=stage.max_output_bytes,
            max_scratch_bytes=stage.scratch_bytes,
            # ADR 0097: the worker's supervisor fetches pinned public files (no egress for the
            # container); None for a stage without a public_artifact keeps the plan digest.
            public_fetch=public_fetch_policy(stage),
        ).model_dump(mode="json")

    # -- submit -----------------------------------------------------------------------

    def prepare(self, rt: RunnerServices, ctx: Any, key: str) -> PrepareResult:
        return "ready"

    def grant_request(self, rt: RunnerServices, ctx: Any, stage: NormalStage) -> GrantRequest:
        worker = self.worker_for(stage)
        if worker is None:
            raise StepFailed("release_snapshot", "step_refused", "provider has no worker principal")
        return GrantRequest(worker, stage.grant_ttl_seconds)

    def free_slots(self, rt: RunnerServices, ctx: Any, stage: NormalStage) -> int:
        """Slots left: ``worker_slots`` minus the worker's live grants (ADR 0048, ADR 0060).

        A grant that has expired does not occupy a slot; one that is unresolved and
        unexpired does, whatever campaign it came from, unless the worker reported that
        its launch was refused (ADR 0046): that grant is final and runs nothing."""
        worker = self.worker_for(stage)
        now = rt.parse_utc(rt.stamp())
        live = sum(
            rt.parse_utc(row["expires_at"]) > now
            for row in rt.rows(
                """SELECT g.expires_at FROM execution_grants g
                WHERE g.worker_principal_id=? AND g.status IN ('active','cancel_requested')
                AND NOT EXISTS (
                    SELECT 1 FROM events e WHERE e.project_id=g.project_id
                    AND e.event_type='execution.observation_reported'
                    AND json_extract(e.payload_json,'$.grant_id')=g.id
                    AND json_extract(e.payload_json,'$.launch_error') IS NOT NULL)""",
                (worker,),
            )
        )
        return max(self.descriptor.worker_slots - live, 0)

    def memory_report(
        self, rt: RunnerServices, ctx: Any, stage: NormalStage
    ) -> tuple[int | None, int | None] | None:
        """``(memory available now, container memory limit)`` as the worker last reported them
        in a fresh heartbeat (ADR 0067), or ``None`` when it reported neither. Control never
        infers a host's memory: a stale or silent worker has no report."""
        from .workers import HEARTBEAT_MAX_AGE

        worker = self.worker_for(stage)
        row = rt.one(
            """SELECT payload_json, created_at FROM events WHERE project_id=?
            AND event_type='worker.heartbeat' AND actor_principal_id=?
            ORDER BY cursor DESC LIMIT 1""",
            (ctx.campaign["project_id"], worker),
        )
        if row is None:
            return None
        if rt.parse_utc(rt.stamp()) - rt.parse_utc(row["created_at"]) > HEARTBEAT_MAX_AGE:
            return None
        body = json.loads(row["payload_json"])
        free, limit = body.get("memory_available_bytes"), body.get("container_memory_bytes")
        return (free, limit) if free is not None or limit is not None else None

    def slot_free(self, rt: RunnerServices, ctx: Any, stage: NormalStage) -> bool:
        """A slot is free while the worker holds fewer live grants than ``worker_slots``."""
        return self.free_slots(rt, ctx, stage) > 0

    def after_admit(self, rt: RunnerServices, ctx: Any, attempt: Mapping[str, Any]) -> None:
        return None

    # -- poll -------------------------------------------------------------------------

    def advance(self, rt: RunnerServices, ctx: Any, stage_row: Mapping, attempt: Mapping) -> bool:
        return rt.observe_grant(ctx, dict(stage_row), dict(attempt))

    def observe(self, rt: RunnerServices, attempt: Mapping[str, Any]) -> Observation:
        code = attempt["exit_code"]
        evidence = {
            "evidence_kind": "run_receipt",
            "evidence_digest": attempt["run_receipt_digest"],
            "manifest_id": attempt["manifest_id"],
            "exit_code": code,
        }
        if code is None:
            return Observation("unknown", "observed_unknown", "no exit code recorded")
        if code == 0:
            return Observation("succeeded", **evidence)
        detail = f"exit_code:{code}"
        if code in rt.stage_for(attempt).retry.transient_exit_codes:
            return Observation("failed_transient", "t2_declared_exit_code", detail, **evidence)
        return Observation("failed_permanent", "undeclared_exit_code", detail, **evidence)

    # -- cancel -----------------------------------------------------------------------

    def cancel(self, rt: RunnerServices, ctx: Any, key: str, attempt: Mapping) -> bool:
        """Drive one attempt's grant to reconciled; True when nothing is outstanding."""
        grant_id = attempt["grant_id"]
        seen = rt.observe_grant_state(attempt)
        grant = seen["grant"]
        if grant["status"] == "reconciled":
            cents = int(grant["settlement_cents"] or 0)
            with rt.connection() as connection:
                rt.settle_attempt(
                    connection,
                    attempt["id"],
                    cents,
                    "local_zero" if cents == 0 else "human_reconcile",
                )
            return True
        expired = rt.now() >= rt.parse_utc(grant["expires_at"])
        reconciliation = seen["reconciliation"]
        exited = bool(
            reconciliation
            and reconciliation["observed_state"] == "exited"
            and reconciliation["termination_confirmed"]
        )

        def reconcile() -> bool:
            settlement = self.settle(rt, attempt)
            result = rt.step(
                ctx,
                key,
                attempt["attempt"],
                "reconcile_grant",
                1,
                lambda: (
                    {"grant_id": grant_id},
                    {
                        "expected_revision": rt.one(
                            "SELECT revision FROM execution_grants WHERE id=?", (grant_id,)
                        )["revision"],
                        "settlement_cents": settlement.cents,
                        "provider_terminated": True,
                    },
                ),
            )
            with rt.connection() as connection:
                rt.settle_attempt(
                    connection,
                    attempt["id"],
                    int(result.response.get("settlement_cents") or 0),
                    settlement.basis,
                )
            return True

        if seen["operation_id"] is not None:
            if exited:
                return reconcile()
            if grant["status"] == "active":
                rt.revoke_grant(ctx, key, dict(attempt), grant)
                return False
            if expired:
                rt.set_attempt(
                    attempt["id"],
                    status="unknown",
                    failure_class="observed_unknown",
                    failure_detail="cancelled grant never confirmed termination",
                )
            return False
        if seen["leases"]:
            if expired:
                rt.set_attempt(
                    attempt["id"],
                    status="unknown",
                    failure_class="observed_unknown",
                    failure_detail="leased grant expired without an operation",
                )
            elif grant["status"] == "active":
                rt.revoke_grant(ctx, key, dict(attempt), grant)
            return False
        if expired:
            result = rt.step(
                ctx,
                key,
                attempt["attempt"],
                "reconcile_unattempted",
                1,
                lambda: ({"grant_id": grant_id}, {"expected_revision": grant["revision"]}),
            )
            with rt.connection() as connection:
                rt.settle_attempt(
                    connection,
                    attempt["id"],
                    int(result.response.get("settlement_cents") or 0),
                    "unclaimed_zero",
                )
            return True
        if grant["status"] == "active":
            rt.revoke_grant(ctx, key, dict(attempt), grant)
        return False

    # -- cost / settle ----------------------------------------------------------------

    def settle(self, rt: RunnerServices, attempt: Mapping[str, Any]) -> Settlement:
        """Local CPU costs nothing; a grant a human reconciled keeps the human's figure."""
        grant = rt.one("SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],))
        if grant is not None and grant["status"] == "reconciled":
            cents = int(grant["settlement_cents"] or 0)
            return Settlement(cents, "local_zero" if cents == 0 else "human_reconcile", True)
        return Settlement(0, "local_zero", True)

    def exposure_open(self, connection: sqlite3.Connection, campaign_id: str) -> bool:
        return False

    def after_tick(self, rt: RunnerServices, ctx: Any) -> None:
        return None

    # -- receipts ---------------------------------------------------------------------

    def receipt_fields(
        self, connection: sqlite3.Connection, attempt: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {}


# -- operator file ---------------------------------------------------------------------

ProviderIdStr = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
PrincipalStr = Annotated[str, StringConstraints(min_length=1, max_length=200)]
ScratchCeiling = Annotated[int, Field(strict=True, ge=1024 * 1024, le=MAX_SCRATCH_BYTES)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutputLimitsConfig(_Strict):
    max_files: Annotated[int, Field(strict=True, ge=1, le=128)] = 128
    max_total_bytes: Annotated[int, Field(strict=True, ge=1, le=ABSOLUTE_MAX_ARTIFACT_BYTES)] = (
        MAX_OUTPUT_TOTAL_BYTES
    )
    max_file_bytes: Annotated[int, Field(strict=True, ge=1, le=ABSOLUTE_MAX_ARTIFACT_BYTES)] = (
        MAX_OUTPUT_TOTAL_BYTES
    )


class SelfHostedInstance(_Strict):
    """A worker-grant provider on an operator-owned host; it claims no GPU."""

    id: ProviderIdStr
    worker_principal_id: PrincipalStr
    runtimes: Annotated[
        list[Literal["docker-cpu-v1", "docker-build-v1"]], Field(min_length=1, max_length=2)
    ] = [  # noqa: RUF012
        "docker-cpu-v1"
    ]
    output_limits: OutputLimitsConfig = OutputLimitsConfig()
    # Grants the instance's pollers can run at once; 1 unless several pollers share the
    # principal (ADR 0048).
    worker_slots: Annotated[int, Field(strict=True, ge=1, le=MAX_WORKER_SLOTS)] = 1
    max_scratch_bytes: ScratchCeiling = 64 * 1024 * 1024
    # ADR 0123: registries (``host[/path]``) a build stage on this instance may push to.
    build_repositories: Annotated[list[RegisteredRepository], Field(max_length=32)] = []  # noqa: RUF012


class LocalDockerConfig(_Strict):
    worker_principal_id: PrincipalStr
    worker_slots: Annotated[int, Field(strict=True, ge=1, le=MAX_WORKER_SLOTS)] = 1
    max_scratch_bytes: ScratchCeiling = 64 * 1024 * 1024
    build_repositories: Annotated[list[RegisteredRepository], Field(max_length=32)] = []  # noqa: RUF012


class ProvidersFile(_Strict):
    """The ``--campaign-providers`` operator file; secret-free, closed per section."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["tessera.campaign-providers.v1"] = Field(
        default=SELF_HOSTED_FILE_SCHEMA, alias="schema"
    )
    local_docker: LocalDockerConfig | None = None
    self_hosted: Annotated[list[SelfHostedInstance], Field(max_length=16)] = []  # noqa: RUF012
    # Read by the Salad provider module; opaque here.
    salad_profiles: Any = None
    # Directory of the Salad credential store, read for expiry metadata only (never a value).
    salad_credential_directory: str | None = None
    # Third-party vendor plugins: ``{entry point or module name: that plugin's config}``.
    # Read by ``campaign_providers.load_plugin``; opaque here.
    plugins: dict[str, Any] = {}  # noqa: RUF012
    # ADR 0070: operator placement preference by resource class. Validated strictly by
    # ``campaign_placement_preference`` when the file is loaded; opaque here.
    placement_preferences: Any = None


def config_sections() -> tuple[ConfigSection, ...]:
    """The ``local-docker`` and ``self_hosted`` entries of the operator file (ADR 0081)."""

    def validate(document: dict) -> None:
        parsed = ProvidersFile.model_validate(document)
        ids = [item.id for item in parsed.self_hosted]
        if len(set(ids)) != len(ids) or set(ids) & _RESERVED_IDS:
            raise ValueError("self_hosted ids must be unique and not local-docker or salad")

    def describe_instance(entry: dict) -> dict:
        item = SelfHostedInstance.model_validate(entry)
        return {
            "resource_class": "cpu",
            "runtimes": list(item.runtimes),
            "worker_slots": item.worker_slots,
            "max_scratch_bytes": item.max_scratch_bytes,
            "max_total_output_bytes": item.output_limits.max_total_bytes,
        }

    def describe_local(entry: dict) -> dict:
        item = LocalDockerConfig.model_validate(entry)
        return {
            "resource_class": "cpu",
            "worker_slots": item.worker_slots,
            "max_scratch_bytes": item.max_scratch_bytes,
        }

    return (
        ConfigSection(
            name="self-hosted",
            provider="self-hosted",
            path=("self_hosted",),
            key_field="id",
            model=SelfHostedInstance,
            validate=validate,
            describe=describe_instance,
            worker=lambda entry: entry.get("worker_principal_id"),
            provider_of=lambda entry: entry.get("id", "self-hosted"),
        ),
        ConfigSection(
            name="local-docker",
            provider=LOCAL_DOCKER_PROVIDER,
            path=("local_docker",),
            key_field=None,
            model=LocalDockerConfig,
            validate=validate,
            describe=describe_local,
            worker=lambda entry: entry.get("worker_principal_id"),
        ),
    )


def _descriptor(
    provider_id: str,
    principal: str,
    limits: OutputLimitsConfig | None = None,
    worker_slots: int = 1,
    max_scratch_bytes: int = 64 * 1024 * 1024,
    runtimes: frozenset[str] = frozenset({DOCKER_CPU_RUNTIME, BUILD_RUNTIME}),
    build_repositories: tuple[str, ...] = (),
) -> ProviderDescriptor:
    limits = limits or OutputLimitsConfig()
    return ProviderDescriptor(
        provider_id=provider_id,
        kind="worker_grant",
        runtimes=runtimes,
        target=None,
        billable=False,
        third_party=False,
        delegable_steps=WORKER_STEPS,
        output_limits=OutputLimits(limits.max_files, limits.max_total_bytes, limits.max_file_bytes),
        worker_principal_id=principal,
        worker_slots=worker_slots,
        max_scratch_bytes=max_scratch_bytes,
        build_repositories=build_repositories,
    )


_REGISTERED: set[str] = set()


def _replace(provider_id: str, instance: WorkerGrantProvider) -> None:
    if provider_id in _REGISTERED:
        unregister_provider(provider_id)
    register_provider(instance)
    _REGISTERED.add(provider_id)


def register_from_config(config: dict | None) -> None:
    """Register ``local-docker`` and every ``self_hosted`` instance of the operator file.

    Idempotent for the ids this module registered; raises ValueError for a malformed file
    or an id that another provider owns.
    """
    parsed = ProvidersFile.model_validate(config or {})
    principal = "" if parsed.local_docker is None else parsed.local_docker.worker_principal_id
    local_builds = (
        () if parsed.local_docker is None else tuple(parsed.local_docker.build_repositories)
    )
    local_slots = 1 if parsed.local_docker is None else parsed.local_docker.worker_slots
    local_scratch = (
        64 * 1024 * 1024 if parsed.local_docker is None else parsed.local_docker.max_scratch_bytes
    )
    seen: set[str] = set()
    for item in parsed.self_hosted:
        if item.id in _RESERVED_IDS or item.id in seen:
            raise ValueError(f"Provider id {item.id!r} is reserved or repeated")
        seen.add(item.id)
        try:
            get_provider(item.id)
        except KeyError:
            pass
        else:
            if item.id not in _REGISTERED:
                raise ValueError(f"Provider id {item.id!r} is already registered")
    _replace(
        LOCAL_DOCKER_PROVIDER,
        WorkerGrantProvider(
            _descriptor(
                LOCAL_DOCKER_PROVIDER,
                principal,
                None,
                local_slots,
                local_scratch,
                build_repositories=local_builds,
            )
        ),
    )
    for item in parsed.self_hosted:
        _replace(
            item.id,
            WorkerGrantProvider(
                _descriptor(
                    item.id,
                    item.worker_principal_id,
                    item.output_limits,
                    item.worker_slots,
                    item.max_scratch_bytes,
                    frozenset(item.runtimes),
                    tuple(item.build_repositories),
                )
            ),
        )


try:
    get_provider(LOCAL_DOCKER_PROVIDER)
except KeyError:
    register_from_config(None)
