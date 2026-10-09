"""Trusted remote-plane dispatcher. No desktop process, shell or credential resolver.

Construction is server-side dependency injection, not a workload/plugin API.
All shipped adapters are offline. Live transports cannot be enabled by a request.
"""

from dataclasses import dataclass
from typing import Protocol

from tessera_control.remote_execution_models import (
    ExecutionSelection,
    ProviderReadback,
    RemoteExecutionPackage,
    RemoteProposal,
    TargetPolicy,
)
from tessera_control.store import Conflict, canonical_digest, utc_now

from .execution_contracts import PreflightRequest
from .kubernetes_execution import KubernetesExecutionProtocolAdapter


@dataclass(frozen=True)
class RoutePreflight:
    admissible: bool
    selection_digest: str
    mode: str = "offline"
    reason: str = "synthetic_only_live_activation_blocked"


class RemoteAdapter(Protocol):
    def preflight(self, proposal: RemoteProposal) -> RoutePreflight: ...

    def perform(self, action: str, proposal: RemoteProposal, state: dict) -> ProviderReadback: ...


class RemoteDispatcher:
    def __init__(self, store, worker, adapters: dict[str, RemoteAdapter]):
        self.store, self.worker, self.adapters = store, worker, dict(adapters)

    def dispatch(self, route_id, action):
        state = self.store.get(self.worker, route_id)
        proposal = RemoteProposal.model_validate(state["proposal"])
        package = RemoteExecutionPackage.model_validate(state["routing_package"])
        if (
            package.proposal != proposal
            or package.org_id != self.worker["org_id"]
            or package.project_id != state["project_id"]
            or package.task_id != state["task_id"]
            or canonical_digest(state["routing_package"]) != state["proposal_digest"]
        ):
            raise Conflict("Remote routing package integrity mismatch.")
        adapter = self.adapters.get(proposal.selection.target_digest)
        if adapter is None:
            raise Conflict("Remote provider disabled or target adapter unavailable.")
        preflight = adapter.preflight(proposal)
        if (
            not preflight.admissible
            or preflight.mode != "offline"
            or preflight.selection_digest
            != canonical_digest(proposal.selection.model_dump(mode="json"))
        ):
            raise Conflict("Target-specific preflight blocked remote execution.")
        intent = self.store.begin(
            self.worker, route_id, action, preflight_digest=canonical_digest(vars(preflight))
        )
        effect_id = intent["effects"][-1]["id"]
        # The committed intent survives process death, provider timeout or lost reply.
        # Never retry this call; subsequent reconciliation invokes observe only.
        try:
            fresh = self.store.get(self.worker, route_id)
            if fresh["effects"][-1]["id"] != effect_id or (
                action in {"create", "start"}
                and (
                    fresh["authority_status"] != "current_offline_only" or fresh["desired"] != "run"
                )
            ):
                raise Conflict("Remote effect fenced after intent; reconcile only.")
            readback = adapter.perform(action, proposal, intent)
            readback = ProviderReadback.model_validate_json(readback.model_dump_json())
        except Exception:
            # Provider exceptions may contain keys/URLs/headers; never retain their text.
            readback = ProviderReadback(state="unknown", observed_at=utc_now())
        return self.store.record(self.worker, route_id, effect_id, readback.model_dump(mode="json"))


class FakeRemoteAdapter:
    """Strict in-memory provider; an explicitly synthetic, non-executing test double."""

    def __init__(self, policy: TargetPolicy):
        self.selection = ExecutionSelection.model_validate_json(policy.selection.model_dump_json())
        self.calls = []
        self.readbacks = []
        self.state = "unknown"
        self.provider_id = None
        self.allocation_id = None

    def preflight(self, proposal):
        exact = proposal.selection == self.selection and self.selection.target != "salad_inference"
        return RoutePreflight(exact, canonical_digest(self.selection.model_dump(mode="json")))

    def perform(self, action, proposal, state):
        if not self.preflight(proposal).admissible:
            raise Conflict("Fake target mismatch.")
        self.calls.append(action)
        if self.readbacks:
            result = self.readbacks.pop(0)
            if isinstance(result, Exception):
                raise result
            return ProviderReadback.model_validate(result | {"observed_at": utc_now()})
        if action == "create":
            self.provider_id = "fake-" + proposal.operation_id
            self.state = "stopped"
        elif action == "start":
            self.state, self.allocation_id = "running", "allocation-1"
        elif action == "stop":
            # Salad's 202 is only an acknowledgement. A later GET must confirm stop.
            return ProviderReadback(
                provider_id=self.provider_id,
                allocation_id=self.allocation_id,
                state="unknown",
                observed_at=utc_now(),
                response_status=202,
            )
        elif action == "delete":
            self.state = "deleted"
        return ProviderReadback(
            provider_id=self.provider_id,
            allocation_id=self.allocation_id,
            state=self.state,
            observed_at=utc_now(),
            response_status=200,
            termination_confirmed=self.state in {"stopped", "deleted"},
        )


class KubernetesRouteAdapter:
    """Offline integration with existing admitted Job runner and collection handoff.

    This does not bypass capability RunnerPlan/CollectorActivationGate: those gates
    are still required for isolated capability execution and still reject live clients.
    The ordinary Kubernetes adapter's fake-client evidence is not a runtime witness.
    """

    def __init__(self, adapter: KubernetesExecutionProtocolAdapter, package, policy: TargetPolicy):
        self.adapter, self.package, self.policy = adapter, package, policy
        self.envelope = package.execution_envelope(adapter.target)

    def preflight(self, proposal):
        selection = proposal.selection
        exact = (
            selection == self.policy.selection
            and selection.target == "kubernetes"
            and selection.target_digest == self.adapter.target.identity_digest
            and proposal.package_digest == self.package.package_digest
            and proposal.operation_id == self.package.operation_id
            and proposal.lease_id == self.envelope.lease_id
            and proposal.epoch == self.envelope.epoch
        )
        admissible = (
            exact
            and self.adapter.preflight(
                PreflightRequest(self.envelope.target, self.envelope)
            ).admissible
        )
        return RoutePreflight(admissible, canonical_digest(selection.model_dump(mode="json")))

    def perform(self, action, proposal, state):
        if action == "create":
            observed = self.adapter.submit(self.envelope, timeout_seconds=2)
        elif action == "observe":
            observed = self.adapter.reconcile(self.envelope, timeout_seconds=2)
        elif action in {"stop", "delete"}:
            stopped = self.adapter.cancel(self.envelope, timeout_seconds=2)
            return ProviderReadback(
                provider_id=stopped.handle.provider_id,
                state="stopped" if stopped.termination_confirmed else "unknown",
                termination_confirmed=stopped.termination_confirmed,
                observed_at=utc_now(),
            )
        else:
            raise Conflict("Kubernetes submit includes start; no separate restart allowed.")
        evidence = []
        if observed.state == "succeeded":
            handoff = self.adapter.collect(self.envelope)
            # Metadata-only handoff, not fabricated artifact bytes or custody.
            evidence.append(
                dict(
                    kind="artifact",
                    sha256=canonical_digest(
                        [handoff.handle.provider_id, list(handoff.output_paths)]
                    ),
                )
            )
        return ProviderReadback(
            provider_id=observed.handle.provider_id,
            state=observed.state if observed.state != "pending" else "unknown",
            observed_at=observed.observed_at.isoformat(),
            evidence=evidence,
        )
