"""Trusted server boundary with a permanently disabled live entry point.

No HTTP client is shipped. Only the explicitly synthetic subclass can resolve the
fixed fake marker and exercise a closed in-memory transport. Never deploy the fake
subclass as live authority; fake receipts remain synthetic in the control ledger.
"""

import hashlib
import json
import re
import time

from .remote_execution_models import ProviderReadback
from .salad_proxy_contract import ProviderCall, ProxyPolicy, ProxyRequest, ProxyResult
from .salad_secrets import FakeSecretResolver
from .store import canonical_digest, utc_now

ACTION = {"create_stopped": "create", "start": "start", "stop": "stop", "delete": "delete"}


class SaladProxy:
    def __init__(self, *, resolver=None, transport=None):
        self._resolver, self._transport = resolver, transport

    def request(self, request):
        # This gate precedes parsing, resolver metadata/values and all network paths.
        return ProxyResult(status="blocked", reason="salad_live_transport_disabled")


class FakeSaladTransport:
    """Closed simulator, not an injectable live HTTP adapter or provider witness."""

    def __init__(self):
        self.calls = []
        self.responses = []
        self.state = "unknown"
        self.provider_id = None
        self.allocation_id = None
        self.available = True
        self.quota_ok = True

    def preflight(self, selection, route_digest):
        return dict(
            selection_digest=canonical_digest(selection.model_dump(mode="json")),
            route_digest=route_digest,
            observed_at=utc_now(),
            available=self.available,
            quota_ok=self.quota_ok,
            group_absent=self.provider_id is None,
            provider_id=self.provider_id,
            state=self.state,
        )

    def send(self, call, secret, *, timeout_seconds):
        assert timeout_seconds == 2
        self.calls.append(call)  # No secret/headers/body in diagnostic records.
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        status = 200
        if call.operation == "create_stopped":
            self.provider_id, self.state = call.provider_id, "stopped"
        elif call.operation == "start":
            self.state, self.allocation_id = "running", "allocation-1"
        elif call.operation in {"stop", "delete"}:
            status = 202  # An acknowledgement changes no observed state.
        return dict(
            status=status,
            organization=call.organization,
            project=call.project,
            group=call.group,
            provider_id=call.provider_id,
            allocation_id=self.allocation_id,
            state=self.state,
            selection_digest=call.selection_digest,
            logs=[],
            events=[],
        )


class FakeSaladProxy(SaladProxy):
    def __init__(self, *, store, worker, policy, resolver, transport, now=time.time):
        if type(resolver) is not FakeSecretResolver or type(transport) is not FakeSaladTransport:
            raise ValueError("only closed synthetic resolver and transport are permitted")
        super().__init__(resolver=resolver, transport=transport)
        # Revalidate snapshots; frozen Pydantic objects can still contain mutable containers.
        self._policy = ProxyPolicy.model_validate_json(
            json.dumps(
                policy.model_dump(mode="json") | {"management": policy.management.model_dump()}
            )
        )
        self._store, self._worker, self._now = store, dict(worker), now

    def preflight(self, proposal, route_id):
        state = self._store.get(self._worker, route_id)
        if proposal.model_dump(mode="json") != state["proposal"] or (
            proposal.selection != self._policy.target.selection
        ):
            raise ValueError("Salad target mismatch")
        return self._transport.preflight(proposal.selection, state["proposal_digest"])

    def request_fake(self, request, *, preflight):
        # The inherited .request remains blocked, including on this test composition.
        try:
            request = ProxyRequest.model_validate_json(request.model_dump_json())
            state = self._store.get(self._worker, request.route_id)
            if (
                not state["effects"]
                or ACTION.get(request.operation, "observe") != state["effects"][-1]["action"]
            ):
                raise ValueError("operation mismatch")
            state = self._store.claim_salad_proxy(
                self._worker,
                request.route_id,
                request.effect_id,
                self._policy,
                preflight,
            )
        except Exception:
            return ProxyResult(status="blocked", reason="admission_denied")
        credential = self._policy.management
        secret = None
        try:
            self._credential_current(credential)
            secret = self._resolver.resolve(credential.reference, credential.version)
        except Exception:
            return ProxyResult(status="blocked", reason="credential_unavailable")
        with secret:
            # Resolution is a separate trust boundary and may stall. Recheck
            # authority and freshness after it, without minting/replaying a claim.
            try:
                state = self._store.claim_salad_proxy(
                    self._worker,
                    request.route_id,
                    request.effect_id,
                    self._policy,
                    preflight,
                    recheck=True,
                )
            except Exception:
                return ProxyResult(status="blocked", reason="admission_denied")
            try:
                self._credential_current(credential)
            except Exception:
                return ProxyResult(status="blocked", reason="credential_unavailable")
            proposal = state["proposal"]
            profile = proposal["selection"]["salad"]
            call = ProviderCall(
                operation=request.operation,
                organization=profile["organization"],
                project=profile["project"],
                group=profile["group"],
                provider_id=state["provider_id"] or proposal["operation_id"],
                selection_digest=canonical_digest(proposal["selection"]),
                nonce=request.effect_id,
                spec_digest=canonical_digest(profile["execution"]),
            )
            try:
                response = self._transport.send(call, secret, timeout_seconds=2)
            except Exception:
                return self._unknown("provider_unavailable")
            return self._normalize(call, response, secret, state)

    def _credential_current(self, credential):
        metadata = self._resolver.metadata(credential.reference)
        now = self._now()
        if (
            metadata.version != credential.version
            or metadata.revoked
            or metadata.purpose != "salad_management"
            or not 0 <= now - metadata.checked_at <= 5
            or metadata.expires_at <= now + 2
        ):
            raise ValueError("credential unavailable")

    @staticmethod
    def _unknown(reason, *, status="unknown", response_status=None):
        return ProxyResult(
            status=status,
            reason=reason,
            observation=ProviderReadback(
                state="unknown",
                observed_at=utc_now(),
                response_status=response_status,
            ).model_dump(mode="json"),
        )

    def _normalize(self, call, response, secret, state):
        try:
            raw = json.dumps(response, allow_nan=False).encode()
            if len(raw) > 65536 or secret.contains_in(raw):
                raise ValueError("invalid response")
            if type(response) is not dict or set(response) != {
                "status",
                "organization",
                "project",
                "group",
                "provider_id",
                "allocation_id",
                "state",
                "selection_digest",
                "logs",
                "events",
            }:
                raise ValueError("invalid response")
            status = response["status"]
            if type(status) is not int:
                raise ValueError("invalid response")
            if 300 <= status < 400:
                return self._unknown("redirect_denied")
            if status not in {200, 201, 202}:
                return self._unknown("provider_unavailable")
            for key in ("organization", "project", "group", "provider_id", "selection_digest"):
                if response[key] != getattr(call, key):
                    return self._unknown("identity_mismatch")
            if status == 202:
                return self._unknown("accepted_pending", status="pending", response_status=202)
            if response["state"] not in {
                "stopped",
                "running",
                "succeeded",
                "failed",
                "deleted",
                "unknown",
            }:
                raise ValueError("invalid state")
            if call.operation == "create_stopped" and response["state"] != "stopped":
                return self._unknown("identity_mismatch")
            allocation = response["allocation_id"]
            # Closed simulator allocation vocabulary, not arbitrary provider text.
            # A live UUID/generation wire contract needs separate conformance.
            if allocation is not None and (
                type(allocation) is not str
                or re.fullmatch(r"allocation-[1-9][0-9]{0,8}", allocation) is None
            ):
                raise ValueError("invalid allocation")
            evidence = []
            for name, kind in (("logs", "log"), ("events", "event")):
                values = response[name]
                if (
                    type(values) is not list
                    or len(values) > 16
                    or any(type(v) is not str or len(v) > 1024 for v in values)
                ):
                    raise ValueError("unbounded evidence")
                # Raw provider text is untrusted and never leaves proxy custody.
                # Hashes are references only, not retained artifact/byte custody.
                evidence.extend(
                    dict(kind=kind, sha256=hashlib.sha256(v.encode()).hexdigest()) for v in values
                )
            reallocated = bool(
                state["allocation_id"] and response["allocation_id"] != state["allocation_id"]
            )
            group_observation = call.operation in {"create_stopped", "read_group"}
            observed_state = (
                response["state"] if group_observation or call.operation == "start" else "unknown"
            )
            observed = ProviderReadback(
                provider_id=call.provider_id,
                allocation_id=response["allocation_id"],
                state="unknown" if reallocated else observed_state,
                observed_at=utc_now(),
                response_status=status,
                evidence=evidence,
                termination_confirmed=(
                    group_observation
                    and not reallocated
                    and response["state"] in {"stopped", "deleted"}
                ),
            )
            return ProxyResult(
                status="unknown" if reallocated else "observed",
                reason="reallocation_uncertain" if reallocated else "observed",
                observation=observed.model_dump(mode="json"),
            )
        except Exception:
            return self._unknown("malformed_response")
