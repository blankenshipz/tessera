"""Deterministic provider-disabled Astra coordinator.

The coordinator only calls the existing read-only facade.  It has no model,
provider, shell, tool, skill-loader, approval, grant, or dispatch integration.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .facade import AstraAgentFacade

_UNSUPPORTED = ("approval", "grant", "dispatch", "tool", "shell", "skill_loading")


class ContextReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["task", "experiment"]
    record_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)


class CoordinatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: tuple[str, ...] = Field(min_length=1, max_length=20)
    recap: str = Field(min_length=1, max_length=4_000)
    proposal: str = Field(min_length=1, max_length=4_000)
    proposal_type: Literal["human_review_needed"] = "human_review_needed"
    provider_disabled: Literal[True] = True
    unsupported_actions: tuple[
        Literal["approval", "grant", "dispatch", "tool", "shell", "skill_loading"], ...
    ] = _UNSUPPORTED


class StaleContextError(RuntimeError):
    """A record changed after its immutable context snapshot was selected."""


class ProviderDisabledCoordinator:
    """Assemble a bounded, honest status/proposal/recap without generation."""

    provider_enabled = False

    def __init__(self, facade: AstraAgentFacade) -> None:
        self._facade = facade

    def assemble(self, prompt: str, selected_context: list[ContextReference]) -> CoordinatorOutput:
        status: list[str] = []
        for reference in selected_context:
            if reference.kind == "task":
                task = self._facade.read_task(reference.record_id)
                if task.revision != reference.expected_revision:
                    raise StaleContextError("Selected task context is stale.")
                status.append(f"Task {task.id}: {task.status}, revision {task.revision}.")
            else:
                experiment = self._facade.read_experiment_status(reference.record_id)
                if experiment.current_design_revision != reference.expected_revision:
                    raise StaleContextError("Selected experiment context is stale.")
                status.append(
                    f"Experiment {experiment.experiment_id}: design revision "
                    f"{experiment.current_design_revision}, "
                    f"{experiment.operation_count} operations."
                )
        if not status:
            status.append("No selected control records were provided.")
        bounded_prompt = prompt.strip()[:4_000]
        return CoordinatorOutput(
            status=tuple(status),
            recap=f"Provider-disabled recap of the request: {bounded_prompt}",
            proposal=(
                "Human review is needed before any separate proposal is created. "
                "This record has no approval, grant, dispatch, tool, shell, "
                "or skill-loading effect."
            ),
            unsupported_actions=_UNSUPPORTED,
        )


class CoordinatorConsumerError(RuntimeError):
    """The ordinary coordinator could not complete a durable control transition."""


class _CoordinatorHttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _CoordinatorHttpClient(Protocol):
    def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> _CoordinatorHttpResponse: ...


class ProviderDisabledCoordinatorConsumer:
    """One ordinary, restart-safe provider-disabled coordinator consumer.

    The control plane owns queue selection, lease expiry, retry limits, and result
    idempotency.  This process holds no authority beyond its existing scoped agent
    credential and never invokes a provider, tool, shell, skill, approval, grant,
    or dispatch path.
    """

    provider_enabled = False

    def __init__(
        self,
        client: _CoordinatorHttpClient,
        token: SecretStr | str,
        facade: AstraAgentFacade,
    ) -> None:
        self._client = client
        self._token = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not isinstance(self._token, str) or not self._token:
            raise ValueError("Coordinator consumer requires an agent token.")
        self._coordinator = ProviderDisabledCoordinator(facade)

    def _post(self, path: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        response = self._client.post(
            path,
            headers={"Authorization": f"Bearer {self._token}", "Idempotency-Key": key},
            json=body,
        )
        if not 200 <= response.status_code < 300:
            raise CoordinatorConsumerError(
                f"Coordinator control command was rejected ({response.status_code})."
            )
        value = response.json()
        if not isinstance(value, dict):
            raise CoordinatorConsumerError("Coordinator control response is not an object.")
        return value

    def run_once(self) -> dict[str, Any]:
        """Heartbeat, claim exactly one next turn, and settle it deterministically."""
        self._post("/astra/consumer/heartbeat", {}, f"astra-consumer-heartbeat-{uuid.uuid4().hex}")
        claimed = self._post(
            "/astra/turns/next-claim", {}, f"astra-consumer-next-{uuid.uuid4().hex}"
        )
        turn = claimed.get("turn")
        if turn is None:
            return {"availability": "idle", "provider_disabled": True, "provider_enabled": False}
        if not isinstance(turn, dict) or not isinstance(claimed.get("prompt"), str):
            raise CoordinatorConsumerError("Coordinator claim response is invalid.")
        try:
            context = [
                ContextReference.model_validate(item) for item in claimed["selected_context"]
            ]
            output = self._coordinator.assemble(claimed["prompt"], context)
        except Exception as error:
            # The durable lease/retry budget remains authoritative.  A deterministic
            # failure is recorded once; a lost acknowledgement can replay this key.
            key = f"astra-consumer-fail-{turn['id']}-{turn['revision']}"
            self._post(
                f"/astra/turns/{turn['id']}/fail",
                {"expected_revision": turn["revision"], "error_code": "consumer_error"},
                key,
            )
            raise CoordinatorConsumerError("Coordinator assembly failed.") from error
        payload = {"expected_revision": turn["revision"], **output.model_dump(mode="json")}
        return self._post(
            f"/astra/turns/{turn['id']}/complete",
            payload,
            f"astra-consumer-complete-{turn['id']}-{turn['revision']}",
        )


class ExplainStatusTurnProtocol(Protocol):
    """Durable protocol boundary for the one provider-disabled status tool.

    Implementations persist intent before the facade read and never make a
    second invocation after an uncertain response.  A future provider-ready
    coordinator may implement this protocol, but it receives no broader tool
    capability from this module.
    """

    def claim(self, turn_id: str, expected_revision: int) -> dict[str, Any]: ...
    def begin_tool(
        self, turn_id: str, expected_revision: int, experiment_id: str
    ) -> dict[str, Any]: ...
    def record_result(
        self, turn_id: str, expected_revision: int, result: dict[str, Any]
    ) -> dict[str, Any]: ...
    def answer(self, turn_id: str, expected_revision: int) -> dict[str, Any]: ...
    def uncertain(self, turn_id: str, expected_revision: int) -> dict[str, Any]: ...


class _StatusHttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _StatusHttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str]) -> _StatusHttpResponse: ...

    def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> _StatusHttpResponse: ...


class ExplainStatusMediatorError(RuntimeError):
    """The mediator could not establish a safe control-plane transition."""


class ExplainStatusMediator:
    """HTTP implementation of the narrow, durable Explain-status protocol.

    It has no OpenCode, model, provider, harness, or generic tool surface.  A
    stable command key lets a *new explicit run* recover a received control
    command without issuing a second command.  It never retries a failed HTTP
    request itself.  Every successful mutation is followed by the durable turn
    read so a restart observes the current binding rather than an old
    idempotency response.
    """

    def __init__(self, client: _StatusHttpClient, token: SecretStr | str) -> None:
        self._client = client
        self._token = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not isinstance(self._token, str) or not self._token:
            raise ValueError("Explain-status mediation requires an agent token.")

    def claim(self, turn_id: str, expected_revision: int) -> dict[str, Any]:
        self._post(turn_id, "claim", expected_revision, {"expected_revision": expected_revision})
        return self._get(turn_id)

    def begin_tool(
        self, turn_id: str, expected_revision: int, experiment_id: str
    ) -> dict[str, Any]:
        self._post(
            turn_id,
            "tool-intent",
            expected_revision,
            {
                "expected_revision": expected_revision,
                "tool": "read_experiment_status",
                "arguments": {"identifier": experiment_id},
            },
        )
        return self._get(turn_id)

    def record_result(
        self, turn_id: str, expected_revision: int, result: dict[str, Any]
    ) -> dict[str, Any]:
        self._post(
            turn_id,
            "tool-result",
            expected_revision,
            {"expected_revision": expected_revision, "result": result},
        )
        return self._get(turn_id)

    def answer(self, turn_id: str, expected_revision: int) -> dict[str, Any]:
        self._post(turn_id, "answer", expected_revision, {"expected_revision": expected_revision})
        return self._get(turn_id)

    def uncertain(self, turn_id: str, expected_revision: int) -> dict[str, Any]:
        self._post(
            turn_id, "uncertain", expected_revision, {"expected_revision": expected_revision}
        )
        return self._get(turn_id)

    def _headers(self, key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    @staticmethod
    def _key(turn_id: str, action: str, revision: int) -> str:
        value = uuid.uuid5(
            uuid.NAMESPACE_URL, f"tessera:astra-status:{turn_id}:{action}:{revision}"
        )
        return f"astra-status-{value.hex}"

    def _post(self, turn_id: str, action: str, revision: int, body: dict[str, Any]) -> None:
        response = self._client.post(
            f"/astra/explain-status-turns/{turn_id}/{action}",
            headers=self._headers(self._key(turn_id, action, revision)),
            json=body,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise ExplainStatusMediatorError(
                f"Explain-status {action} was rejected ({response.status_code})."
            )

    def _get(self, turn_id: str) -> dict[str, Any]:
        response = self._client.get(
            f"/astra/explain-status-turns/{turn_id}", headers=self._headers()
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise ExplainStatusMediatorError(
                f"Explain-status state could not be read ({response.status_code})."
            )
        value = response.json()
        if not isinstance(value, dict):
            raise ExplainStatusMediatorError("Explain-status state is not an object.")
        return value


class ExplainStatusCoordinator:
    """Run exactly one mediated ``read_experiment_status`` call for one turn."""

    provider_enabled = False

    def __init__(self, facade: AstraAgentFacade, control: ExplainStatusTurnProtocol) -> None:
        self._facade = facade
        self._control = control

    def run(self, turn_id: str, expected_turn_revision: int) -> dict[str, Any]:
        claimed = self._control.claim(turn_id, expected_turn_revision)
        binding = claimed.get("binding")
        if not isinstance(binding, dict):
            return claimed
        if binding.get("state") == "tool_result":
            return self._control.answer(turn_id, binding["revision"])
        if binding.get("state") != "claimed":
            # An already-intended/uncertain/cancelled turn is intentionally not
            # resumed: another facade call could duplicate an unknown effect.
            return claimed
        binding_revision = binding["revision"]
        experiment_id = binding["experiment_id"]
        begun = self._control.begin_tool(turn_id, binding_revision, experiment_id)
        current = begun.get("binding")
        if not isinstance(current, dict):
            return begun
        if current.get("state") == "tool_result":
            return self._control.answer(turn_id, current["revision"])
        if current.get("state") != "tool_intent":
            return begun
        intent_revision = current["revision"]
        try:
            result = self._facade.call("read_experiment_status", {"identifier": experiment_id})
            recorded = self._control.record_result(
                turn_id, intent_revision, result.model_dump(mode="json")
            )
        except Exception:
            # The persisted intent is the duplicate-invocation fence.  If this
            # acknowledgement was itself lost, a later retry still cannot invoke.
            try:
                self._control.uncertain(turn_id, intent_revision)
            except Exception:
                pass
            raise
        result_binding = recorded.get("binding")
        if not isinstance(result_binding, dict) or result_binding.get("state") != "tool_result":
            return recorded
        return self._control.answer(turn_id, result_binding["revision"])
