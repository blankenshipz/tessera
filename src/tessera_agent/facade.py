"""HTTP-only, allowlisted reads for a provider-disabled fake agent.

This module intentionally has no control-store imports, runtime skill loader, or
generic HTTP request method.  The authenticated control API remains the tenant
and project authorization boundary; this facade adds a narrower tool boundary.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

_MAX_PAGE_SIZE = 100
_MAX_CONTROL_LIST_SIZE = 1_000
_MAX_EXPERIMENT_EXPORT_BYTES = 65_536
_IDENTIFIER_MAX_LENGTH = 200
_OPERATION_ID_LENGTH = 32


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _HttpClient(Protocol):
    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, object] | None = None
    ) -> _HttpResponse: ...


class FacadeError(RuntimeError):
    """A safe, non-authoritative facade error."""


class CredentialError(FacadeError):
    """The supplied credential is not the expected scoped control agent credential."""


class ControlPlaneError(FacadeError):
    """The authenticated control API rejected or could not satisfy a read."""


class ProtocolError(FacadeError):
    """The control API returned data outside this facade's typed contract."""


class UnknownToolError(FacadeError):
    """The requested tool is not in the agent allowlist."""


class ForbiddenToolError(FacadeError):
    """A consequential action is explicitly unavailable to the fake agent."""


class ToolInputError(FacadeError):
    """Tool arguments were not a small, strict input object."""


class AgentCredential(_StrictModel):
    """A short-lived existing control API agent credential and its fixed project."""

    token: SecretStr
    project_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_LENGTH)


class _IdentifierArguments(_StrictModel):
    identifier: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_LENGTH)


class _ThreadArguments(_StrictModel):
    thread_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_LENGTH)
    offset: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=50, ge=1, le=_MAX_PAGE_SIZE)


class _OperationArguments(_StrictModel):
    experiment_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_LENGTH)
    operation_id: str = Field(pattern=rf"^[0-9a-f]{{{_OPERATION_ID_LENGTH}}}$")


class AgentIdentity(_StrictModel):
    credential_id: str
    principal_id: str
    org_id: str
    role: Literal["agent"]
    kind: Literal["agent"]
    audience: Literal["control"]
    project_id: str
    expires_at: str
    revoked_at: None
    parent_id: str


class TaskStatus(_StrictModel):
    id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_LENGTH)
    project_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_LENGTH)
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=4000)
    budget_cents: int
    max_duration_seconds: int
    status: str
    revision: int
    plan_digest: str | None


class ExecutionPlan(_StrictModel):
    argv: list[str] = Field(min_length=1, max_length=128)
    input_snapshot_digest: str
    pinned_skill_digests: list[str] = Field(max_length=128)
    output_paths: list[str] = Field(max_length=128)
    profile: Literal["local-cpu", "kubernetes-opencode"]
    network_enabled: Literal[False]
    max_output_bytes: int


class TaskPlan(_StrictModel):
    task_id: str
    project_id: str
    revision: int
    title: str
    purpose: str
    budget_cents: int
    max_duration_seconds: int
    plan_digest: str
    execution_spec: ExecutionPlan | None


class OperationStatus(_StrictModel):
    operation_id: str
    project_id: str
    task_id: str
    desired_state: Literal["admitted", "cancel_requested"]
    observed_state: Literal["unknown", "running", "exited"]
    provider_id: str | None


class ExperimentStatus(_StrictModel):
    experiment_id: str
    project_id: str
    current_design_revision: int
    operation_count: int
    operations: tuple[OperationStatus, ...] = Field(max_length=_MAX_PAGE_SIZE)
    operations_truncated: bool
    scientific_validity: Literal["not_assessed"] = "not_assessed"


class ThreadMessage(_StrictModel):
    id: str
    thread_id: str
    sender_principal_id: str
    intent: Literal["question", "direction", "status", "evidence"]
    body: str = Field(min_length=1, max_length=8000)
    reply_to: str | None
    created_at: str


class ThreadContext(_StrictModel):
    thread_id: str
    messages: tuple[ThreadMessage, ...]
    offset: int
    limit: int


class SkillReleaseStatus(_StrictModel):
    id: str
    digest: str
    status: Literal["active", "revoked"]
    expires_at: str
    revision: int


class SkillMetadata(_StrictModel):
    skill_id: str
    project_id: str
    name: str
    description: str
    digest: str
    provenance: str
    classification: str
    releases: tuple[SkillReleaseStatus, ...]
    metadata_only: Literal[True] = True
    materialized: Literal[False] = False
    loaded: Literal[False] = False
    runtime_skill_loading: Literal["disabled"] = "disabled"


_Response = (
    TaskStatus | TaskPlan | ExperimentStatus | OperationStatus | ThreadContext | SkillMetadata
)
_Arguments = _IdentifierArguments | _ThreadArguments | _OperationArguments
_T = TypeVar("_T", bound=BaseModel)


class AstraAgentFacade:
    """One project-scoped, read-only allowlist for the Astra fake agent.

    ``client`` must be an HTTP client (for example ``httpx.Client`` or
    FastAPI's test client). It is deliberately used only through ``GET``.
    """

    allowed_tools = frozenset(
        {
            "read_task",
            "read_plan",
            "read_experiment_status",
            "read_operation_status",
            "read_thread_context",
            "read_skill_metadata",
        }
    )
    forbidden_tools = frozenset(
        {
            "approve",
            "grant",
            "dispatch",
            "provision",
            "secret",
            "shell",
            "accept_result",
            "result_acceptance",
            "publish",
            "publication",
            "load_skill",
        }
    )
    provider_enabled = False

    def __init__(self, client: _HttpClient, credential: AgentCredential) -> None:
        self._client = client
        self._credential = credential
        self._identity_verified = False

    def call(self, tool: str, arguments: dict[str, object]) -> _Response:
        """Invoke exactly one allowlisted read with strict bounded arguments."""
        if tool in self.forbidden_tools:
            raise ForbiddenToolError(
                f"Tool {tool!r} is unavailable to the provider-disabled agent."
            )
        model_type: type[_Arguments]
        if tool in {"read_task", "read_plan", "read_experiment_status", "read_skill_metadata"}:
            model_type = _IdentifierArguments
        elif tool == "read_operation_status":
            model_type = _OperationArguments
        elif tool == "read_thread_context":
            model_type = _ThreadArguments
        else:
            raise UnknownToolError(f"Tool {tool!r} is not allowlisted.")
        try:
            parsed = model_type.model_validate(arguments)
        except ValidationError as error:
            raise ToolInputError("Tool arguments are invalid.") from error
        if tool == "read_task":
            return self.read_task(parsed.identifier)  # type: ignore[union-attr]
        if tool == "read_plan":
            return self.read_plan(parsed.identifier)  # type: ignore[union-attr]
        if tool == "read_experiment_status":
            return self.read_experiment_status(parsed.identifier)  # type: ignore[union-attr]
        if tool == "read_operation_status":
            return self.read_operation_status(parsed.experiment_id, parsed.operation_id)  # type: ignore[union-attr]
        if tool == "read_thread_context":
            return self.read_thread_context(parsed.thread_id, parsed.offset, parsed.limit)  # type: ignore[union-attr]
        return self.read_skill_metadata(parsed.identifier)  # type: ignore[union-attr]

    def read_task(self, task_id: str) -> TaskStatus:
        task_id = self._identifier(task_id)
        return self._task_status(self._get(f"/tasks/{task_id}"))

    def read_plan(self, task_id: str) -> TaskPlan:
        task = self.read_task(task_id)
        if task.plan_digest is None:
            raise ControlPlaneError("Task has no current plan.")
        value = self._get(f"/tasks/{task.id}/plans/{task.plan_digest}")
        plan = self._model(TaskPlan, self._select(value, TaskPlan))
        self._require_project(plan.project_id)
        return plan

    def read_experiment_status(self, experiment_id: str) -> ExperimentStatus:
        experiment_id = self._identifier(experiment_id)
        export = self._experiment_export(experiment_id)
        experiment = self._mapping(export, "experiment")
        project_id = self._string(experiment, "project_id")
        self._require_project(project_id)
        operations = tuple(
            self._operation_status(item) for item in self._items(export, "operations")
        )
        visible = operations[:_MAX_PAGE_SIZE]
        return ExperimentStatus(
            experiment_id=self._string(experiment, "id"),
            project_id=project_id,
            current_design_revision=self._integer(experiment, "current_design_revision"),
            operation_count=len(operations),
            operations=visible,
            operations_truncated=len(operations) > len(visible),
        )

    def read_operation_status(self, experiment_id: str, operation_id: str) -> OperationStatus:
        args = _OperationArguments(experiment_id=experiment_id, operation_id=operation_id)
        operation_id = args.operation_id
        export = self._experiment_export(args.experiment_id)
        experiment = self._mapping(export, "experiment")
        self._require_project(self._string(experiment, "project_id"))
        for raw_operation in self._items(export, "operations"):
            operation = self._operation_status(raw_operation)
            if operation.operation_id == operation_id:
                return operation
        raise ControlPlaneError("Operation was not found in the specified experiment.")

    def read_thread_context(
        self, thread_id: str, offset: int = 0, limit: int = 50
    ) -> ThreadContext:
        args = _ThreadArguments(thread_id=thread_id, offset=offset, limit=limit)
        value = self._get(
            f"/interaction/threads/{args.thread_id}/messages",
            {"offset": args.offset, "limit": args.limit},
        )
        messages = tuple(
            self._model(ThreadMessage, self._select(item, ThreadMessage))
            for item in self._items(value, "messages")
        )
        if len(messages) > args.limit:
            raise ProtocolError("Control API exceeded the requested thread context bound.")
        return ThreadContext(
            thread_id=args.thread_id, messages=messages, offset=args.offset, limit=args.limit
        )

    def read_skill_metadata(self, skill_id: str) -> SkillMetadata:
        skill_id = self._identifier(skill_id)
        skill = self._get(f"/skills/{skill_id}")
        project_id = self._string(skill, "project_id")
        self._require_project(project_id)
        releases_response = self._get(f"/skills/{skill_id}/releases", {"limit": _MAX_PAGE_SIZE})
        releases = tuple(
            self._model(SkillReleaseStatus, self._select(item, SkillReleaseStatus))
            for item in self._items(releases_response, "releases")
        )
        return SkillMetadata(
            skill_id=self._string(skill, "id"),
            project_id=project_id,
            name=self._string(skill, "name"),
            description=self._string(skill, "description"),
            digest=self._string(skill, "digest"),
            provenance=self._string(skill, "provenance"),
            classification=self._string(skill, "classification"),
            releases=releases,
        )

    def _experiment_export(self, experiment_id: str) -> dict[str, Any]:
        return self._get(
            f"/experiments/{experiment_id}/export", {"max_bytes": _MAX_EXPERIMENT_EXPORT_BYTES}
        )

    def _get(self, path: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        self._verify_identity()
        return self._raw_get(path, params)

    def _verify_identity(self) -> None:
        if self._identity_verified:
            return
        identity = self._model(AgentIdentity, self._raw_get("/me"))
        if identity.project_id != self._credential.project_id:
            raise CredentialError("Credential project scope does not match the facade project.")
        self._identity_verified = True

    def _raw_get(self, path: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        response = self._client.get(
            path,
            headers={"Authorization": f"Bearer {self._credential.token.get_secret_value()}"},
            params=params,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise ControlPlaneError(f"Control API read was rejected ({response.status_code}).")
        value = response.json()
        if not isinstance(value, dict):
            raise ProtocolError("Control API response must be an object.")
        return value

    def _task_status(self, value: dict[str, Any]) -> TaskStatus:
        task = self._model(TaskStatus, self._select(value, TaskStatus))
        self._require_project(task.project_id)
        return task

    def _operation_status(self, value: object) -> OperationStatus:
        if not isinstance(value, dict):
            raise ProtocolError("Operation status must be an object.")
        operation = self._model(OperationStatus, self._select(value, OperationStatus))
        self._require_project(operation.project_id)
        return operation

    def _require_project(self, project_id: str) -> None:
        if project_id != self._credential.project_id:
            raise ProtocolError("Control API response escaped the credential project scope.")

    @staticmethod
    def _identifier(value: str) -> str:
        return _IdentifierArguments(identifier=value).identifier

    @staticmethod
    def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
        item = value.get(key)
        if not isinstance(item, dict):
            raise ProtocolError(f"Control API response has no valid {key!r} object.")
        return item

    @staticmethod
    def _items(value: dict[str, Any], key: str) -> list[object]:
        items = value.get(key)
        if not isinstance(items, list) or len(items) > _MAX_CONTROL_LIST_SIZE:
            raise ProtocolError(f"Control API response has invalid or unbounded {key!r} items.")
        return items

    @staticmethod
    def _string(value: dict[str, Any], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str):
            raise ProtocolError(f"Control API response has no valid {key!r} string.")
        return item

    @staticmethod
    def _integer(value: dict[str, Any], key: str) -> int:
        item = value.get(key)
        if type(item) is not int:
            raise ProtocolError(f"Control API response has no valid {key!r} integer.")
        return item

    @staticmethod
    def _select(value: dict[str, Any], model: type[_T]) -> dict[str, Any]:
        selected = {name: value[name] for name in model.model_fields if name in value}
        return selected

    @staticmethod
    def _model(model: type[_T], value: dict[str, Any]) -> _T:
        try:
            return model.model_validate(value)
        except ValidationError as error:
            raise ProtocolError("Control API response is outside the facade contract.") from error


BoundedAgentFacade = AstraAgentFacade
