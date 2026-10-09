"""Narrow Salad fake adapter and trusted proxy seam; zero live transport implementation."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from pydantic import StringConstraints

from tessera_control.remote_execution_models import (
    ExecutionSelection,
    Frozen,
    ProviderReadback,
    RemoteProposal,
)
from tessera_control.store import Conflict, _parse_utc, utc_now

from .remote_dispatch import FakeRemoteAdapter, RoutePreflight


class SaladProxyConfig(Frozen):
    # Server-owned reference only. Neither endpoint nor reference enters a workload/package.
    proxy_reference: Annotated[str, StringConstraints(pattern=r"^proxy-ref:[a-z0-9-]{1,63}$")]
    credential_reference: Annotated[
        str, StringConstraints(pattern=r"^credential-ref:[a-z0-9-]{1,63}$")
    ]
    live_enabled: Literal[False] = False
    conformance: Literal["not_validated"] = "not_validated"


class SaladProxyTransport(Protocol):
    """Implement on a trusted server, never in desktop/agent/workload.

    Read-only preflight must compare organization/project/group absence or exact
    stopped-group image/resources/country/replicas. Mutation is one bounded call,
    with no automatic retries. Observe must pin group and allocation identities.
    A future implementation must independently validate custody and stop evidence.
    """

    def preflight(self, proposal: RemoteProposal, config: SaladProxyConfig) -> RoutePreflight: ...

    def readback(self, proposal: RemoteProposal, config: SaladProxyConfig) -> ProviderReadback: ...

    def mutate(
        self,
        action: str,
        proposal: RemoteProposal,
        config: SaladProxyConfig,
        *,
        timeout_seconds: int,
    ) -> ProviderReadback: ...


class SaladGroupReadback(Frozen):
    """Read-only exact group specification, separate from provider status/202 replies."""

    selection: ExecutionSelection
    provider_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9-]{1,200}$")]
    state: Literal["stopped", "running", "unknown"]
    observed_at: str

    def require_stopped(self, proposal: RemoteProposal, provider_id: str):
        snapshot = SaladGroupReadback.model_validate_json(self.model_dump_json())
        age = (datetime.now(UTC) - _parse_utc(snapshot.observed_at)).total_seconds()
        if (
            snapshot.selection != proposal.selection
            or snapshot.selection.target != "salad_container"
            or snapshot.provider_id != provider_id
            or snapshot.state != "stopped"
            or not 0 <= age <= 5
        ):
            raise Conflict("Fresh exact stopped Salad group readback required.")
        return snapshot


class SaladOfflineAdapter(FakeRemoteAdapter):
    def __init__(self, policy):
        if policy.selection.target != "salad_container" or policy.selection.salad is None:
            raise Conflict("Exact single-container Salad policy required.")
        super().__init__(policy)
        self.group_selection = None

    def perform(self, action, proposal, state):
        if action == "start":
            # Synthetic GET/readback immediately before the one permitted start.
            SaladGroupReadback(
                selection=self.group_selection,
                provider_id=self.provider_id,
                state=self.state,
                observed_at=utc_now(),
            ).require_stopped(proposal, state["provider_id"])
        result = super().perform(action, proposal, state)
        if action == "create" and result.state == "stopped":
            self.group_selection = self.selection
        return result


class SaladProxyAdapter:
    """Disabled even when configured. Merely injecting transport is not conformance."""

    def __init__(self, config: SaladProxyConfig, transport: SaladProxyTransport):
        self._config, self._transport = config, transport

    def preflight(self, proposal):
        from tessera_control.salad_proxy import SaladProxy

        blocked = SaladProxy(transport=self._transport).request(None)
        return RoutePreflight(False, proposal.selection.target_digest, reason=blocked.reason)

    def perform(self, action, proposal, state):
        raise Conflict("Salad live effects disabled; proxy conformance and activation required.")


class SaladProxyFakeAdapter:
    """Server-composed admission exercise; never a workload or live transport."""

    def __init__(self, proxy, route_id):
        from tessera_control.salad_proxy import FakeSaladProxy

        if type(proxy) is not FakeSaladProxy:
            raise ValueError("closed fake proxy required")
        self._proxy, self._route_id = proxy, route_id
        self._preflight = None

    def preflight(self, proposal):
        from tessera_control.store import canonical_digest

        self._preflight = self._proxy.preflight(proposal, self._route_id)
        return RoutePreflight(
            True,  # Use-time admission applies quota/availability only to new work, not stops.
            canonical_digest(proposal.selection.model_dump(mode="json")),
        )

    def perform(self, action, proposal, state):
        from tessera_control.salad_proxy_contract import ProxyRequest

        if state["id"] != self._route_id:
            raise Conflict("Salad route mismatch.")
        result = self._proxy.request_fake(
            ProxyRequest(
                route_id=self._route_id,
                effect_id=state["effects"][-1]["id"],
                operation={"create": "create_stopped", "observe": "read_group"}.get(action, action),
            ),
            preflight=self._preflight,
        )
        if result.observation is None:
            return ProviderReadback(
                state="unknown", observed_at=utc_now(), proxy_reason=result.reason
            )
        return ProviderReadback.model_validate(result.observation | {"proxy_reason": result.reason})
