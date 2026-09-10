"""Deterministic in-memory reconciliation with deliberately zero live effects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .contracts import (
    LostReplyError,
    ProvisioningContractError,
    ProvisioningPlan,
    ResourceIntent,
    digest,
)


@dataclass(frozen=True)
class FakeEffect:
    resource_key: str
    resource_identity: str
    plan_digest: str


class FakeProvisioningAdapter:
    """A test seam that records one effect for each stable resource identity.

    It has no client configuration, subprocess, network call, credentials, or
    Kubernetes dependency.  A simulated lost reply occurs after the fake resource
    is recorded so a retry must discover it instead of making a duplicate effect.
    """

    live_effects = False

    def __init__(self) -> None:
        self.resources: dict[str, ResourceIntent] = {}
        self.effects: list[FakeEffect] = []
        self.lose_next_reply = False
        self.before_each_intent: Callable[[], None] | None = None

    @staticmethod
    def resource_key(plan: ProvisioningPlan, intent: ResourceIntent) -> str:
        return digest({"intent": intent.payload(), "plan_digest": plan.digest})

    def reconcile(
        self, plan: ProvisioningPlan, *, allowed: Callable[[], bool] | None = None
    ) -> tuple[FakeEffect, ...]:
        plan.assert_current()
        observed: list[FakeEffect] = []
        for intent in plan.resource_intents:
            if allowed is not None and not allowed():
                raise ProvisioningContractError(
                    "Provisioning operation is no longer allowed to apply."
                )
            if self.before_each_intent is not None:
                self.before_each_intent()
            if allowed is not None and not allowed():
                raise ProvisioningContractError(
                    "Provisioning operation was cancelled before reconciliation."
                )
            key = self.resource_key(plan, intent)
            existing = self.resources.get(key)
            if existing is None:
                self.resources[key] = intent
                effect = FakeEffect(key, intent.identity, plan.digest)
                self.effects.append(effect)
            elif existing != intent:
                raise ProvisioningContractError(
                    "Fake resource identity conflicts with its durable intent."
                )
            else:
                effect = FakeEffect(key, intent.identity, plan.digest)
            observed.append(effect)
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise LostReplyError(
                "Fake adapter reply was lost; reconcile by stable resource identity."
            )
        return tuple(observed)


class FakeProvisioningOperator:
    """The only Phase 1 operator: a deterministic wrapper around the fake adapter."""

    live_effects = False

    def __init__(self, adapter: FakeProvisioningAdapter | None = None) -> None:
        self.adapter = adapter or FakeProvisioningAdapter()

    def reconcile(
        self, plan: ProvisioningPlan, *, allowed: Callable[[], bool] | None = None
    ) -> tuple[FakeEffect, ...]:
        return self.adapter.reconcile(plan, allowed=allowed)


__all__ = ["FakeEffect", "FakeProvisioningAdapter", "FakeProvisioningOperator"]
