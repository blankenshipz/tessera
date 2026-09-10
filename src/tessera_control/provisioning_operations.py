"""Human-approved, fake-only provisioning operation records for Phase 1.

The store is intentionally in-memory: it is a contract slice, not a durable
control-plane activation.  It never starts a live adapter and exposes explicit
replay/cancellation state for focused conformance tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from tessera_provisioner.contracts import (
    LostReplyError,
    ProvisioningContractError,
    ProvisioningPlan,
    digest,
)
from tessera_provisioner.fake_adapter import FakeEffect, FakeProvisioningOperator

from .environment_models import EnvironmentActor
from .environments import EnvironmentPlanner
from .store import Conflict, Forbidden, NotFound


@dataclass(frozen=True)
class ProvisioningOperation:
    id: str
    tenant_id: str
    plan: ProvisioningPlan
    revision: int
    desired_state: str
    observed_state: str
    approved_by: str | None = None

    @property
    def live_effects(self) -> bool:
        return False


class ProvisioningOperationStore:
    """Admits requests, but only a human can bind an immutable plan for fake apply."""

    def __init__(self, planner: EnvironmentPlanner, operator: FakeProvisioningOperator) -> None:
        self.planner = planner
        self.operator = operator
        self._operations: dict[str, ProvisioningOperation] = {}
        self._idempotency: dict[tuple[str, str, str], tuple[str, ProvisioningOperation]] = {}

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        return (now or datetime.now(UTC)).astimezone(UTC)

    @staticmethod
    def _operation_id(plan: ProvisioningPlan) -> str:
        return "envop_" + plan.digest[:32]

    def _replay(
        self, actor: EnvironmentActor, method: str, idempotency_key: str, request: object
    ) -> ProvisioningOperation | None:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ProvisioningContractError("idempotency_key must be a bounded non-empty string.")
        key = (actor.tenant_id, actor.id, f"{method}:{idempotency_key}")
        request_digest = digest(request)
        prior = self._idempotency.get(key)
        if prior is None:
            return None
        if prior[0] != request_digest:
            raise Conflict("Idempotency key was reused with different provisioning content.")
        return prior[1]

    def _remember(
        self,
        actor: EnvironmentActor,
        method: str,
        idempotency_key: str,
        request: object,
        operation: ProvisioningOperation,
    ) -> ProvisioningOperation:
        self._idempotency[(actor.tenant_id, actor.id, f"{method}:{idempotency_key}")] = (
            digest(request),
            operation,
        )
        return operation

    def _operation(self, actor: EnvironmentActor, operation_id: str) -> ProvisioningOperation:
        operation = self._operations.get(operation_id)
        if operation is None or operation.tenant_id != actor.tenant_id:
            raise NotFound("Provisioning operation was not found.")
        return operation

    def request_apply(
        self,
        actor: EnvironmentActor,
        plan: ProvisioningPlan,
        expected_plan_digest: str,
        idempotency_key: str,
    ) -> ProvisioningOperation:
        plan.assert_current()
        request = {"expected_plan_digest": expected_plan_digest, "plan_digest": plan.digest}
        replay = self._replay(actor, "request_apply", idempotency_key, request)
        if replay is not None:
            return replay
        if plan.tenant_id != actor.tenant_id or expected_plan_digest != plan.digest:
            raise Forbidden("Provisioning request does not match this tenant's exact plan digest.")
        # The actor only requests desired state.  This is not approval or adapter mutation.
        operation_id = self._operation_id(plan)
        operation = self._operations.get(operation_id)
        if operation is None:
            operation = ProvisioningOperation(
                id=operation_id,
                tenant_id=actor.tenant_id,
                plan=plan,
                revision=1,
                desired_state="apply_requested",
                observed_state="not_reconciled",
            )
            self._operations[operation_id] = operation
        elif operation.plan != plan:
            raise Conflict("Provisioning operation identity conflicts with its immutable plan.")
        return self._remember(actor, "request_apply", idempotency_key, request, operation)

    def approve(
        self,
        actor: EnvironmentActor,
        operation_id: str,
        expected_revision: int,
        expected_plan_digest: str,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningOperation:
        request = {
            "expected_plan_digest": expected_plan_digest,
            "expected_revision": expected_revision,
            "operation_id": operation_id,
        }
        replay = self._replay(actor, "approve", idempotency_key, request)
        if replay is not None:
            return replay
        if actor.role != "human":
            raise Forbidden("Only an eligible human may approve provisioning.")
        operation = self._operation(actor, operation_id)
        operation.plan.assert_current()
        if operation.revision != expected_revision or operation.plan.digest != expected_plan_digest:
            raise Conflict("Provisioning approval is fenced by revision and exact plan digest.")
        if operation.desired_state != "apply_requested":
            raise Conflict("Only an unapproved provisioning request may be approved.")
        target = self.planner.target(actor, operation.plan.target_id)
        if (
            target.revision != operation.plan.target_revision
            or target.identity_digest != operation.plan.target_identity_digest
        ):
            raise Conflict("Provisioning plan target revision is stale.")
        if operation.plan.is_expired(self._now(now)):
            raise Conflict("Provisioning plan has expired.")
        updated = replace(
            operation,
            revision=operation.revision + 1,
            desired_state="approved",
            approved_by=actor.id,
        )
        self._operations[operation.id] = updated
        return self._remember(actor, "approve", idempotency_key, request, updated)

    def request_cancel(
        self,
        actor: EnvironmentActor,
        operation_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> ProvisioningOperation:
        request = {"expected_revision": expected_revision, "operation_id": operation_id}
        replay = self._replay(actor, "request_cancel", idempotency_key, request)
        if replay is not None:
            return replay
        operation = self._operation(actor, operation_id)
        if operation.revision != expected_revision:
            raise Conflict("Provisioning cancellation is fenced by operation revision.")
        updated = (
            operation
            if operation.desired_state == "cancel_requested"
            else replace(
                operation, revision=operation.revision + 1, desired_state="cancel_requested"
            )
        )
        self._operations[operation.id] = updated
        return self._remember(actor, "request_cancel", idempotency_key, request, updated)

    def reconcile(
        self, actor: EnvironmentActor, operation_id: str, *, now: datetime | None = None
    ) -> tuple[ProvisioningOperation, tuple[FakeEffect, ...]]:
        """Run only the fake operator after every authority fence is checked again."""
        operation = self._operation(actor, operation_id)
        if actor.role == "agent":
            raise Forbidden("Agents cannot mutate provisioning through reconciliation.")
        if operation.desired_state == "reconciled":
            return operation, ()

        def allowed() -> bool:
            current = self._operations.get(operation_id)
            if (
                current is None
                or current.desired_state != "approved"
                or current.revision != operation.revision
            ):
                return False
            try:
                current.plan.assert_current()
                target = self.planner.target(actor, current.plan.target_id)
            except (NotFound, Forbidden, ProvisioningContractError):
                return False
            return (
                target.revision == current.plan.target_revision
                and target.identity_digest == current.plan.target_identity_digest
                and not current.plan.is_expired(self._now(now))
            )

        if not allowed():
            raise Conflict("Provisioning reconciliation is no longer authorized.")
        try:
            effects = self.operator.reconcile(operation.plan, allowed=allowed)
        except LostReplyError:
            unknown = replace(operation, observed_state="unknown")
            self._operations[operation.id] = unknown
            raise
        except ProvisioningContractError as error:
            if self._operations[operation.id].desired_state == "cancel_requested":
                return self._operations[operation.id], ()
            raise Conflict(
                "Provisioning reconciliation did not pass current authority fences."
            ) from error
        reconciled = replace(
            operation,
            revision=operation.revision + 1,
            desired_state="reconciled",
            observed_state="fake_reconciled",
        )
        self._operations[operation.id] = reconciled
        return reconciled, effects


__all__ = ["ProvisioningOperation", "ProvisioningOperationStore"]
