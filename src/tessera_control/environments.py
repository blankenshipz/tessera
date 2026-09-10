"""Target catalog and pure environment-plan request boundary for Phase 1."""

from __future__ import annotations

from tessera_provisioner.contracts import ProvisioningPlan

from .environment_models import (
    EnvironmentActor,
    EnvironmentPlanRequest,
    EnvironmentTarget,
    resolve_environment_plan,
)
from .store import Conflict, Forbidden, NotFound


class EnvironmentPlanner:
    """Keeps target selection tenant-scoped without performing any provision action."""

    def __init__(self) -> None:
        self._targets: dict[tuple[str, str], EnvironmentTarget] = {}

    def register_target(self, actor: EnvironmentActor, target: EnvironmentTarget) -> None:
        if actor.role != "human":
            raise Forbidden("Only a human may change an environment target.")
        if actor.tenant_id != target.tenant_id:
            raise Forbidden("Environment target tenant does not match the actor.")
        existing = self._targets.get((target.tenant_id, target.id))
        if existing is not None and target.revision <= existing.revision:
            raise Conflict("Environment target revision must increase.")
        self._targets[(target.tenant_id, target.id)] = target

    def target(self, actor: EnvironmentActor, target_id: str) -> EnvironmentTarget:
        target = self._targets.get((actor.tenant_id, target_id))
        if target is None:
            raise NotFound("Environment target was not found.")
        return target

    def resolve(self, actor: EnvironmentActor, request: EnvironmentPlanRequest) -> ProvisioningPlan:
        # Both humans and agents may request this pure calculation.  It persists
        # neither a decision nor an external operation.
        return resolve_environment_plan(self.target(actor, request.target_id), request)
