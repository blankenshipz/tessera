"""Delegated campaign steps (ADR 0038): intent first, then one ordinary store call.

Every step is an existing store command invoked with an in-process delegation marker.
``Store._mutate`` hands the marker to ``campaign_delegation.admit`` inside the command's
own transaction, which completes the intent in the same transaction as the effect. This
module builds each request with the same model, ``model_dump`` and digest expression the
HTTP routes use, writes the intent, makes the call, and reports the completed intent.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .artifact_custody import ArtifactCustodyReleaseRequest, ArtifactCustodyStore
from .artifact_dispositions import ArtifactDispositionRequest, ArtifactDispositionStore
from .artifact_lineage import ArtifactLineageStore, LineageEdgeRequest, LineageNodeRequest
from .campaign_delegation import (
    FORWARD_LEGAL_STATUSES,
    REDUCE_ONLY_STEPS,
    STEP_TABLE,
    delegated_principal,
    intent_key,
    write_intent,
)
from .campaign_store import CampaignStore
from .capabilities import CapabilityStore
from .capability_models import ApproveCapabilities, RequestCapabilities
from .execution import ExecutionStore
from .execution_models import (
    IssueExecutionGrant,
    ReconcileExecutionGrant,
    ReconcileExpiredUnattemptedGrant,
    RevokeExecutionGrant,
)
from .experiment_budget_models import AuthorizeExperimentBudget
from .experiments import ExperimentStore
from .experiments_models import (
    AdmitExperimentAttempt,
    CreateExperiment,
    DeriveOutputMeasurement,
    LinkAttemptArtifacts,
    ResolveAdmittedAttempt,
)
from .gate_evidence import GateEvidenceStore
from .gate_evidence_ingest import IngestEvidenceRows
from .gate_evidence_models import BindAttemptSplit, CreateSplitLock, RecordMetric
from .interaction import InteractionStore
from .interaction_models import CreateDecision, ResolveDecision
from .models import ApproveTask, CreateTask, ReviseTaskPlan
from .provider_fetch_record import RecordProviderFetch
from .remote_execution import RemoteExecutionStore
from .remote_execution_models import RemoteCommand, RemoteProposal, TargetPolicy
from .research import ResearchStore
from .research_models import AssessSource, CreateSource, CreateStudy, RegisterVariant
from .salad_unclaimed import ReconcileUnclaimedSalad, SaladUnclaimedStore
from .snapshot_composition import ComposeSnapshot, SnapshotCompositionStore
from .snapshot_releases import ReleaseSnapshot, RevokeSnapshotRelease, SnapshotReleaseStore
from .store import (
    Conflict,
    Forbidden,
    Store,
    StoreError,
    _parse_utc,
    canonical_digest,
    utc_now,
)

# One generation of approval is 1000 ordinals wide so that a re-issued intent (after a
# re-approval abandoned the pending one) gets a fresh idempotency key and slot.
GENERATION_WIDTH = 1000

STEP_MODELS: dict[str, type] = {
    "create_task": CreateTask,
    "revise_plan": ReviseTaskPlan,
    "approve_task": ApproveTask,
    "compose_snapshot": ComposeSnapshot,
    "release_snapshot": ReleaseSnapshot,
    "revoke_release": RevokeSnapshotRelease,
    "create_experiment": CreateExperiment,
    "authorize_budget": AuthorizeExperimentBudget,
    "issue_grant": IssueExecutionGrant,
    "admit_attempt": AdmitExperimentAttempt,
    "revoke_grant": RevokeExecutionGrant,
    "reconcile_grant": ReconcileExecutionGrant,
    "reconcile_unattempted": ReconcileExpiredUnattemptedGrant,
    "record_disposition": ArtifactDispositionRequest,
    "custody_release": ArtifactCustodyReleaseRequest,
    "resolve_attempt": ResolveAdmittedAttempt,
    "link_artifacts": LinkAttemptArtifacts,
    "derive_measurement": DeriveOutputMeasurement,
    "bind_split": BindAttemptSplit,
    "record_gate_metric": RecordMetric,
    "ingest_evidence_rows": IngestEvidenceRows,
    "record_provider_fetch": RecordProviderFetch,
    "register_lineage_node": LineageNodeRequest,
    "record_lineage_edge": LineageEdgeRequest,
    "create_study": CreateStudy,
    "create_study_source": CreateSource,
    "register_study_variant": RegisterVariant,
    "create_split_lock": CreateSplitLock,
    "propose_decision": CreateDecision,
    "assess_source": AssessSource,
    "resolve_decision": ResolveDecision,
    "request_capability": RequestCapabilities,
    "approve_capability": ApproveCapabilities,
    "propose_route": RemoteProposal,
    "approve_route": RemoteCommand,
    "start_route": RemoteCommand,
    "cancel_route": RemoteCommand,
    "reconcile_unclaimed_salad": ReconcileUnclaimedSalad,
}


class StepFailed(Exception):
    """A delegated step was refused or failed permanently (intent marked ``failed``)."""

    def __init__(self, step: str, error_class: str, detail: str) -> None:
        super().__init__(f"{step}: {error_class}: {detail}")
        self.step = step
        self.error_class = error_class
        self.detail = detail


class StepTransient(Exception):
    """The call may or may not have committed; the intent stays pending and is replayed."""


class DelegationLapsed(Exception):
    """The approver's authority is gone; the campaign pauses instead of failing the step."""


@dataclass(frozen=True)
class StepResult:
    status: int
    response: dict[str, Any]
    intent_id: str
    executed: bool  # False when a completed intent was found (replay of a done slot)


@dataclass(frozen=True)
class Prepared:
    step: str
    path: str
    resources: dict[str, str]
    payload: dict[str, Any]
    digest: str
    call: Callable[[dict[str, Any], str], tuple[int, dict[str, Any]]]


class Stores:
    """Lazily constructed store instances; each is a thin wrapper over one database."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._cache: dict[str, Any] = {}

    def _get(self, name: str, factory: Callable[[Path], Any]) -> Any:
        if name not in self._cache:
            self._cache[name] = factory(self.state_dir)
        return self._cache[name]

    @property
    def base(self) -> Store:
        return self._get("base", Store)

    @property
    def releases(self) -> SnapshotReleaseStore:
        return self._get("releases", SnapshotReleaseStore)

    @property
    def composition(self) -> SnapshotCompositionStore:
        return self._get("composition", SnapshotCompositionStore)

    @property
    def experiments(self) -> ExperimentStore:
        return self._get("experiments", ExperimentStore)

    @property
    def execution(self) -> ExecutionStore:
        return self._get("execution", ExecutionStore)

    @property
    def dispositions(self) -> ArtifactDispositionStore:
        return self._get("dispositions", ArtifactDispositionStore)

    @property
    def custody(self) -> ArtifactCustodyStore:
        return self._get("custody", ArtifactCustodyStore)

    @property
    def lineage(self) -> ArtifactLineageStore:
        return self._get("lineage", ArtifactLineageStore)

    @property
    def research(self) -> ResearchStore:
        return self._get("research", ResearchStore)

    @property
    def evidence(self) -> GateEvidenceStore:
        return self._get("evidence", GateEvidenceStore)

    @property
    def interactions(self) -> InteractionStore:
        return self._get("interactions", lambda _state_dir: InteractionStore(self.base))

    @property
    def capabilities(self) -> CapabilityStore:
        return self._get("capabilities", CapabilityStore)

    @property
    def unclaimed(self) -> SaladUnclaimedStore:
        return self._get("unclaimed", SaladUnclaimedStore)

    def routes(self, policy: TargetPolicy | None) -> RemoteExecutionStore:
        """Built per call: the allowlist is exactly the one operator-derived policy."""
        if policy is None:
            raise StepFailed("route", "step_refused", "no operator route policy is bound")
        return RemoteExecutionStore(self.state_dir, policies=(policy,))


def prepare(
    stores: Stores,
    step: str,
    ids: Mapping[str, str],
    body: Mapping[str, Any],
    route_policy: TargetPolicy | None = None,
) -> Prepared:
    """Validate ``body`` with the route's model and bind the exact call, path and digest."""
    model = STEP_MODELS[step]
    try:
        parsed = model.model_validate(dict(body))
    except ValidationError as error:
        raise StepFailed(
            step, "step_refused", f"invalid request body: {error.error_count()}"
        ) from None
    payload = parsed.model_dump(mode="json", exclude_none=step == "approve_task")
    path = STEP_TABLE[step][1].format(**ids)
    resources = dict(ids)
    digest = canonical_digest(payload)

    def call(principal: dict[str, Any], key: str) -> tuple[int, dict[str, Any]]:
        if step == "create_task":
            return stores.base.create_task(principal, ids["project_id"], key, digest, payload)
        if step == "revise_plan":
            return stores.base.revise_task_plan(principal, ids["task_id"], key, digest, payload)
        if step == "approve_task":
            return stores.base.approve_task(
                principal,
                ids["task_id"],
                key,
                digest,
                payload["expected_revision"],
                payload.get("expected_plan_digest"),
            )
        if step == "compose_snapshot":
            return stores.composition.compose(principal, ids["project_id"], key, digest, payload)
        if step == "release_snapshot":
            return stores.releases.release_snapshot(
                principal, ids["snapshot_id"], key, digest, payload
            )
        if step == "revoke_release":
            return stores.releases.revoke_snapshot_release(
                principal, ids["release_id"], key, digest, payload["expected_revision"]
            )
        if step == "create_experiment":
            return stores.experiments.create_experiment(
                principal, ids["project_id"], key, digest, payload
            )
        if step == "authorize_budget":
            # budget_command derives the digest itself, from the same model dump.
            return stores.experiments.budget_command(principal, ids["experiment_id"], key, payload)
        if step == "issue_grant":
            return stores.execution.issue_execution_grant(
                principal, ids["project_id"], key, digest, payload
            )
        if step == "admit_attempt":
            return stores.experiments.admit_attempt(
                principal, ids["variant_id"], key, digest, payload
            )
        if step == "revoke_grant":
            return stores.execution.revoke_execution_grant(
                principal, ids["grant_id"], key, digest, payload["expected_revision"]
            )
        if step == "reconcile_grant":
            return stores.execution.reconcile_execution_grant(
                principal, ids["grant_id"], key, digest, payload
            )
        if step == "reconcile_unattempted":
            return stores.execution.reconcile_expired_unattempted_grant(
                principal, ids["grant_id"], key, digest, payload
            )
        if step == "record_disposition":
            return stores.dispositions.record_disposition(
                principal, ids["manifest_id"], key, digest, payload
            )
        if step == "custody_release":
            return stores.custody.record_release(
                principal, ids["manifest_id"], key, digest, payload
            )
        if step == "resolve_attempt":
            return stores.experiments.resolve_admitted_attempt(
                principal, ids["attempt_id"], key, digest, payload
            )
        if step == "link_artifacts":
            return stores.experiments.link_attempt_artifacts(
                principal, ids["attempt_id"], key, digest, payload
            )
        if step == "derive_measurement":
            return stores.experiments.derive_output_measurement(
                principal, ids["attempt_id"], key, digest, payload
            )
        if step == "bind_split":
            return stores.evidence.bind_attempt_split(
                principal, ids["attempt_id"], key, digest, payload
            )
        if step == "record_gate_metric":
            return stores.evidence.record_metric(principal, ids["attempt_id"], key, digest, payload)
        if step == "ingest_evidence_rows":
            return stores.experiments.ingest_evidence_rows(
                principal, ids["attempt_id"], key, digest, payload
            )
        if step == "record_provider_fetch":
            return stores.experiments.record_provider_fetch(
                principal, ids["attempt_id"], key, digest, payload
            )
        if step == "register_lineage_node":
            # Lineage derives its digest from the same dump (no digest parameter).
            return stores.lineage.register_node(principal, ids["project_id"], key, payload)
        if step == "record_lineage_edge":
            return stores.lineage.record_edge(principal, ids["project_id"], key, payload)
        if step == "create_study":
            return stores.research.create_study(principal, ids["project_id"], key, digest, payload)
        if step == "create_study_source":
            return stores.research.create_source(principal, ids["study_id"], key, digest, payload)
        if step == "register_study_variant":
            return stores.research.register_variant(
                principal, ids["study_id"], key, digest, payload
            )
        if step == "create_split_lock":
            return stores.evidence.create_split_lock(
                principal, ids["project_id"], key, digest, payload
            )
        if step == "propose_decision":
            return stores.interactions.request_decision(
                principal,
                key,
                payload["project_id"],
                payload["kind"],
                payload["scope"],
                payload["expires_at"],
                priority=payload["priority"],
                task_id=payload["task_id"],
                expected_task_revision=payload["expected_task_revision"],
                reviewer_principal_id=payload["reviewer_principal_id"],
            )
        if step == "assess_source":
            return stores.research.assess_source(principal, ids["source_id"], key, digest, payload)
        if step == "resolve_decision":
            return stores.interactions.resolve_decision(
                principal,
                key,
                payload["decision_id"],
                payload["expected_revision"],
                payload["decision"],
                payload["rationale"],
            )
        if step == "request_capability":
            return stores.capabilities.request(principal, ids["task_id"], key, payload)
        if step == "approve_capability":
            return stores.capabilities.approve(principal, ids["capability_id"], key, payload)
        if step == "propose_route":
            return stores.routes(route_policy).propose(principal, ids["task_id"], key, payload)
        if step in {"approve_route", "start_route", "cancel_route"}:
            return stores.routes(route_policy).command(principal, ids["route_id"], key, payload)
        if step == "reconcile_unclaimed_salad":
            return stores.unclaimed.reconcile(principal, ids["grant_id"], key, digest, payload)
        raise StepFailed(step, "step_refused", "unknown step")

    if step == "record_disposition":
        resources["disposition"] = payload["disposition"]
    if step == "release_snapshot":
        resources["classification"] = payload["classification"]
    if step == "propose_decision":
        resources.update(
            project_id=payload["project_id"],
            study_id=payload["scope"]["study_id"],
            decision_action=payload["scope"]["action"],
        )
    if step in {"approve_route", "start_route", "cancel_route"}:
        resources["command"] = payload["command"]
    return Prepared(step, path, resources, payload, digest, call)


class StepExecutor:
    def __init__(self, store: CampaignStore, state_dir: Path) -> None:
        self.store = store
        self.stores = Stores(state_dir)

    @staticmethod
    def _slot(
        connection: sqlite3.Connection,
        campaign_id: str,
        stage_key: str | None,
        attempt: int | None,
        step: str,
        ordinal: int,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """SELECT * FROM campaign_step_intents WHERE campaign_id=? AND stage_key IS ?
            AND attempt IS ? AND step=? AND (ordinal % ?) = ? ORDER BY created_at, id""",
            (campaign_id, stage_key, attempt, step, GENERATION_WIDTH, ordinal),
        ).fetchall()

    def completed(
        self,
        connection: sqlite3.Connection,
        campaign_id: str,
        stage_key: str | None,
        attempt: int | None,
        step: str,
        ordinal: int,
    ) -> StepResult | None:
        for row in self._slot(connection, campaign_id, stage_key, attempt, step, ordinal):
            if row["status"] == "done":
                return StepResult(
                    row["response_status"], json.loads(row["response_json"]), row["id"], False
                )
        return None

    def failed(
        self,
        connection: sqlite3.Connection,
        campaign_id: str,
        stage_key: str | None,
        attempt: int | None,
        step: str,
        ordinal: int,
    ) -> sqlite3.Row | None:
        for row in self._slot(connection, campaign_id, stage_key, attempt, step, ordinal):
            if row["status"] == "failed":
                return row
        return None

    def run(
        self,
        *,
        campaign: Mapping[str, Any],
        approval: Mapping[str, Any],
        generation: int,
        stage_key: str | None,
        attempt: int | None,
        step: str,
        ordinal: int,
        build: Callable[[], tuple[dict[str, str], dict[str, Any]]],
        approver_is_live: Callable[[sqlite3.Connection, Mapping], bool],
        route_policy: TargetPolicy | None = None,
        before_call: Callable[[], None] | None = None,
        provider_id: str | None = None,
    ) -> StepResult:
        """Run one slot. A completed slot is returned without calling the store."""
        with self.store.runner_connection() as connection:
            done = self.completed(connection, campaign["id"], stage_key, attempt, step, ordinal)
            if done is not None:
                return done
            failed = self.failed(connection, campaign["id"], stage_key, attempt, step, ordinal)
            if failed is not None:
                raise StepFailed(step, failed["error_class"] or "step_refused", "recorded")
            if not approver_is_live(connection, approval):
                raise DelegationLapsed
        ids, body = build()
        prepared = prepare(self.stores, step, ids, body, route_policy)
        key = intent_key(
            campaign["id"], stage_key, attempt, step, ordinal + GENERATION_WIDTH * generation
        )
        with self.store.runner_connection() as connection:
            try:
                intent = write_intent(
                    connection,
                    campaign=campaign,
                    approval=approval,
                    stage_key=stage_key,
                    attempt=attempt,
                    step=step,
                    ordinal=ordinal + GENERATION_WIDTH * generation,
                    method=STEP_TABLE[step][0],
                    path=prepared.path,
                    request_digest=prepared.digest,
                    idempotency_key=key,
                    resources=(
                        prepared.resources
                        if provider_id is None
                        else {**prepared.resources, "provider_id": provider_id}
                    ),
                )
            except Conflict:
                # The recorded intent for this slot no longer matches the rebuilt request.
                self._mark_failed(
                    connection,
                    campaign,
                    approval,
                    stage_key,
                    attempt,
                    step,
                    ordinal + GENERATION_WIDTH * generation,
                    "step_refused",
                )
                intent = None
        if intent is None:
            raise StepFailed(step, "step_refused", "intent no longer matches request")
        principal = delegated_principal(approval, campaign, intent["id"])
        if before_call is not None:
            before_call()  # the runner lease is re-checked right before the write
        try:
            prepared.call(principal, key)
        except (Conflict, Forbidden, StoreError) as error:
            with self.store.runner_connection() as connection:
                if isinstance(error, Forbidden):
                    if not approver_is_live(connection, approval):
                        raise DelegationLapsed from None
                    if not self._authority_current(connection, campaign, approval):
                        # Cancel, expiry or supersession raced this step; it committed
                        # nothing, so it stays pending and is re-issued or abandoned.
                        raise StepTransient(f"{step}: authority changed") from None
                    if step not in REDUCE_ONLY_STEPS and not self._forward_legal(
                        connection, campaign
                    ):
                        # The campaign left a forward-legal state (blocked, cancel requested)
                        # while this forward step was prepared. The delegation deny is about
                        # the campaign, not the stage: the step committed nothing, so the
                        # intent stays pending and the attempt keeps its reservation and hold
                        # until a later pass may continue (ADR 0038 addendum, v0.104.0).
                        raise StepTransient(f"{step}: campaign not running") from None
                self._fail_intent(connection, campaign, intent["id"], type(error).__name__, step)
            raise StepFailed(step, type(error).__name__, str(error)[:300]) from None
        except Exception as error:
            raise StepTransient(f"{step}: {type(error).__name__}") from None
        with self.store.runner_connection() as connection:
            row = connection.execute(
                "SELECT * FROM campaign_step_intents WHERE id=?", (intent["id"],)
            ).fetchone()
        if row is None or row["status"] != "done":
            raise StepTransient(f"{step}: intent not completed")
        return StepResult(row["response_status"], json.loads(row["response_json"]), row["id"], True)

    @staticmethod
    def _authority_current(
        connection: sqlite3.Connection, campaign: Mapping, approval: Mapping
    ) -> bool:
        row = connection.execute(
            """SELECT c.status AS campaign_status, a.status AS approval_status, a.expires_at
            FROM campaigns c JOIN campaign_approvals a ON a.campaign_id=c.id
            WHERE c.id=? AND a.id=?""",
            (campaign["id"], approval["id"]),
        ).fetchone()
        if row is None or row["approval_status"] != "active":
            return False
        if row["campaign_status"] not in {"running", "stopping", "cancel_requested", "blocked"}:
            return False
        return _parse_utc(row["expires_at"]) > _parse_utc(utc_now())

    @staticmethod
    def _forward_legal(connection: sqlite3.Connection, campaign: Mapping) -> bool:
        """The campaign's status still admits forward steps (``running`` or ``stopping``;
        delegation decides the in-flight rule for ``stopping``)."""
        row = connection.execute(
            "SELECT status FROM campaigns WHERE id=?", (campaign["id"],)
        ).fetchone()
        return row is not None and row["status"] in FORWARD_LEGAL_STATUSES

    def _fail_intent(
        self,
        connection: sqlite3.Connection,
        campaign: Mapping,
        intent_id: str,
        error_class: str,
        step: str,
    ) -> None:
        updated = connection.execute(
            """UPDATE campaign_step_intents SET status='failed', error_class=?, completed_at=?
            WHERE id=? AND status='pending'""",
            (error_class, utc_now(), intent_id),
        )
        if updated.rowcount:
            self.store.record_runner_event(
                connection,
                campaign,
                "campaign.step_failed",
                {"intent_id": intent_id, "step": step, "error_class": error_class},
            )

    def _mark_failed(
        self, connection, campaign, approval, stage_key, attempt, step, ordinal, error_class
    ) -> None:
        row = connection.execute(
            """SELECT id FROM campaign_step_intents WHERE campaign_id=? AND approval_id=?
            AND stage_key IS ? AND attempt IS ? AND step=? AND ordinal=? AND status='pending'""",
            (campaign["id"], approval["id"], stage_key, attempt, step, ordinal),
        ).fetchone()
        if row is not None:
            self._fail_intent(connection, campaign, row["id"], error_class, step)
