from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tessera_control.environment_models import (
    EnvironmentActor,
    EnvironmentPlanRequest,
    EnvironmentTarget,
)
from tessera_control.environments import EnvironmentPlanner
from tessera_control.provisioning_operations import ProvisioningOperationStore
from tessera_control.store import Conflict, Forbidden, NotFound
from tessera_provisioner.contracts import (
    LostReplyError,
    ProvisioningContractError,
    ResourceIntent,
)
from tessera_provisioner.fake_adapter import FakeProvisioningAdapter, FakeProvisioningOperator

NOW = datetime(2026, 9, 9, tzinfo=UTC)
EXPIRES = "2026-09-09T01:00:00Z"


def _actors() -> tuple[EnvironmentActor, EnvironmentActor, EnvironmentActor]:
    return (
        EnvironmentActor("human-one", "tenant-one", "human"),
        EnvironmentActor("agent-one", "tenant-one", "agent"),
        EnvironmentActor("human-two", "tenant-two", "human"),
    )


def _target(revision: int = 1) -> EnvironmentTarget:
    return EnvironmentTarget("target-one", "tenant-one", revision, "a" * 64)


def _request(*intents: ResourceIntent, expires_at: str = EXPIRES) -> EnvironmentPlanRequest:
    return EnvironmentPlanRequest("target-one", 1, expires_at, intents)


def _setup() -> tuple[
    EnvironmentPlanner,
    ProvisioningOperationStore,
    FakeProvisioningAdapter,
    EnvironmentActor,
    EnvironmentActor,
    EnvironmentActor,
]:
    human, agent, foreign = _actors()
    planner = EnvironmentPlanner()
    planner.register_target(human, _target())
    adapter = FakeProvisioningAdapter()
    operations = ProvisioningOperationStore(planner, FakeProvisioningOperator(adapter))
    return planner, operations, adapter, human, agent, foreign


def test_resolution_is_deterministic_for_one_explicit_target_and_omits_namespace_pvc() -> None:
    planner, _, _, human, agent, _ = _setup()
    alpha = ResourceIntent("clusterpolicy", "alpha", {"mode": "strict"})
    beta = ResourceIntent("clusterpolicy", "beta", {"mode": "strict"})

    first = planner.resolve(agent, _request(beta, alpha))
    second = planner.resolve(human, _request(alpha, beta))

    assert first == second
    assert first.target_id == "target-one"
    assert first.target_revision == 1
    assert first.namespace is None
    assert first.persistent_volume_claim is None
    assert [intent.identity for intent in first.resource_intents] == [
        "clusterpolicy/alpha",
        "clusterpolicy/beta",
    ]
    with pytest.raises(ProvisioningContractError, match="omit namespace"):
        EnvironmentPlanRequest("target-one", 1, EXPIRES, namespace="unsafe")  # type: ignore[arg-type]
    with pytest.raises(ProvisioningContractError, match="cannot provision namespaces"):
        ResourceIntent("namespace", "unsafe", {})


def test_agent_can_request_but_only_human_can_approve_or_reconcile() -> None:
    planner, operations, adapter, human, agent, _ = _setup()
    plan = planner.resolve(agent, _request(ResourceIntent("clusterpolicy", "one", {})))
    requested = operations.request_apply(agent, plan, plan.digest, "agent-request")

    with pytest.raises(Forbidden, match="human may approve"):
        operations.approve(agent, requested.id, 1, plan.digest, "agent-approve", now=NOW)
    with pytest.raises(Forbidden, match="Agents cannot mutate"):
        operations.reconcile(agent, requested.id, now=NOW)

    approved = operations.approve(human, requested.id, 1, plan.digest, "human-approve", now=NOW)
    reconciled, effects = operations.reconcile(human, approved.id, now=NOW)
    replayed, replay_effects = operations.reconcile(human, approved.id, now=NOW)

    assert reconciled.desired_state == "reconciled"
    assert replayed == reconciled
    assert replay_effects == ()
    assert reconciled.live_effects is False
    assert len(effects) == len(adapter.effects) == 1
    assert adapter.live_effects is False


def test_tenant_target_revision_and_expiry_fences_hold_before_fake_effects() -> None:
    planner, operations, adapter, human, agent, foreign = _setup()
    plan = planner.resolve(agent, _request(ResourceIntent("clusterpolicy", "one", {})))
    operation = operations.request_apply(agent, plan, plan.digest, "request")

    with pytest.raises(NotFound):
        operations.approve(foreign, operation.id, 1, plan.digest, "foreign", now=NOW)
    planner.register_target(human, _target(revision=2))
    with pytest.raises(Conflict, match="target revision"):
        operations.approve(human, operation.id, 1, plan.digest, "stale-target", now=NOW)
    assert not adapter.effects

    fresh_planner, fresh_operations, fresh_adapter, fresh_human, fresh_agent, _ = _setup()
    expired = fresh_planner.resolve(
        fresh_agent,
        _request(ResourceIntent("clusterpolicy", "one", {}), expires_at="2026-09-09T00:00:00Z"),
    )
    expired_operation = fresh_operations.request_apply(
        fresh_agent, expired, expired.digest, "expired-request"
    )
    with pytest.raises(Conflict, match="expired"):
        fresh_operations.approve(
            fresh_human, expired_operation.id, 1, expired.digest, "expired-approve", now=NOW
        )
    assert not fresh_adapter.effects


def test_lost_reply_replays_by_stable_identity_without_duplicate_effects() -> None:
    planner, operations, adapter, human, agent, _ = _setup()
    plan = planner.resolve(
        agent,
        _request(
            ResourceIntent("clusterpolicy", "one", {}),
            ResourceIntent("clusterpolicy", "two", {}),
        ),
    )
    first = operations.request_apply(agent, plan, plan.digest, "request")
    assert operations.request_apply(agent, plan, plan.digest, "request") == first
    approved = operations.approve(human, first.id, 1, plan.digest, "approve", now=NOW)
    adapter.lose_next_reply = True

    with pytest.raises(LostReplyError, match="reply was lost"):
        operations.reconcile(human, approved.id, now=NOW)
    assert len(adapter.effects) == 2

    reconciled, replay_effects = operations.reconcile(human, approved.id, now=NOW)
    assert reconciled.desired_state == "reconciled"
    assert len(replay_effects) == 2
    assert len(adapter.effects) == 2


def test_cancel_wins_before_fake_reconciliation_and_handles_the_race() -> None:
    planner, operations, adapter, human, agent, _ = _setup()
    plan = planner.resolve(agent, _request(ResourceIntent("clusterpolicy", "one", {})))
    requested = operations.request_apply(agent, plan, plan.digest, "request")
    approved = operations.approve(human, requested.id, 1, plan.digest, "approve", now=NOW)

    def cancel_at_reconcile_boundary() -> None:
        adapter.before_each_intent = None
        operations.request_cancel(agent, approved.id, approved.revision, "cancel-race")

    adapter.before_each_intent = cancel_at_reconcile_boundary
    cancelled, effects = operations.reconcile(human, approved.id, now=NOW)

    assert cancelled.desired_state == "cancel_requested"
    assert effects == ()
    assert not adapter.effects
