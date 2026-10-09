"""Closed typed MCP workflow; authorization remains at the HTTP boundary."""

from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from mcp.types import ToolAnnotations
from pydantic import Field, field_validator

from tessera_control.experiments_models import CreateExperiment
from tessera_control.external_agent_models import AgentProvenance
from tessera_control.models import (
    NonBlank200,
    NonBlank4000,
    Sha256Digest,
    StrictDuration,
    StrictNonNegativeInt,
    StrictRequest,
    StrictRevision,
)
from tessera_control.research_models import CreateStudy
from tessera_mcp.client import ControlClient, segment


def notebook_path(
    project_id,
    *,
    export=False,
    after_sequence=0,
    after_event_cursor=0,
    limit=100,
    max_bytes=2097152,
):
    """Closed cursor parameters; neither arbitrary paths nor query forwarding."""
    values = (after_sequence, after_event_cursor, limit, max_bytes)
    if any(type(value) is not int for value in values):
        raise ValueError("Notebook bounds must be integers")
    if not (
        after_sequence >= 0
        and after_event_cursor >= 0
        and 1 <= limit <= 1000
        and 1024 <= max_bytes <= 2097152
    ):
        raise ValueError("Invalid notebook cursors or bounds")
    path = f"/projects/{segment(project_id)}/agent-notebook"
    if export:
        path += "/export"
    return (
        path
        + "?"
        + urlencode(
            {
                "after_sequence": after_sequence,
                "after_event_cursor": after_event_cursor,
                "limit": limit,
                "max_bytes": max_bytes,
            }
        )
    )


class ResourceBinding(StrictRequest):
    variant_id: NonBlank200
    expected_variant_revision: StrictRevision
    expected_design_revision: StrictRevision
    execution_grant_id: NonBlank200
    expected_execution_grant_revision: StrictRevision
    expected_execution_epoch: StrictRevision
    task_id: NonBlank200
    expected_task_revision: StrictRevision
    plan_digest: Sha256Digest
    target_digest: Sha256Digest


Action = Literal[
    "attempt:admit", "attempt:dispatch", "attempt:status", "execution:cancel", "authority:delegate"
]


class AuthorityBounds(StrictRequest):
    subject_credential_id: NonBlank200
    destination: NonBlank200
    model: NonBlank200
    purpose: NonBlank4000
    actions: Annotated[list[Action], Field(min_length=1, max_length=5)]
    bindings: Annotated[list[ResourceBinding], Field(min_length=1, max_length=64)]
    budget_cents: StrictNonNegativeInt
    max_duration_seconds: StrictDuration
    expires_at: datetime
    delegation_depth: Annotated[int, Field(strict=True, ge=0, le=8)] = 0

    @field_validator("bindings")
    @classmethod
    def unique_bindings(cls, value):
        if len({b.variant_id for b in value}) != len(value):
            raise ValueError("Authority variants must be unique")
        if len({b.execution_grant_id for b in value}) != len(value):
            raise ValueError("Authority execution grants must be unique")
        return value

    @field_validator("actions")
    @classmethod
    def unique_actions(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Authority actions must be unique")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Authority expiry requires a timezone")
        return value


class Admit(StrictRequest):
    action: Literal["attempt:admit"]
    variant_id: NonBlank200
    expected_variant_revision: StrictRevision
    expected_design_revision: StrictRevision
    execution_grant_id: NonBlank200


class Dispatch(StrictRequest):
    action: Literal["attempt:dispatch"]
    attempt_id: NonBlank200
    expected_revision: StrictRevision
    target_digest: Sha256Digest
    profile_version: Literal["development-minikube-opencode-v1"]


class AttemptStatus(StrictRequest):
    action: Literal["attempt:status"]
    attempt_id: NonBlank200


class Cancel(StrictRequest):
    action: Literal["execution:cancel"]
    execution_grant_id: NonBlank200
    expected_revision: StrictRevision


class Consume(StrictRequest):
    expected_revision: StrictRevision
    expected_epoch: StrictRevision
    model: NonBlank200
    command: Annotated[Admit | Dispatch | AttemptStatus | Cancel, Field(discriminator="action")]


class ProjectProposal(StrictRequest):
    kind: Literal["project"]
    name: NonBlank200
    purpose: NonBlank4000


class StudyProposal(StrictRequest):
    kind: Literal["study"]
    study: CreateStudy


class ExperimentProposal(StrictRequest):
    kind: Literal["experiment"]
    study_id: NonBlank200
    experiment: CreateExperiment


class ResearchProposal(StrictRequest):
    proposal: Annotated[
        ProjectProposal | StudyProposal | ExperimentProposal, Field(discriminator="kind")
    ]
    provenance: AgentProvenance


class Observation(StrictRequest):
    """Caller-attested observation, not human testimony or verified execution."""

    attempt_id: NonBlank200
    expected_attempt_revision: StrictRevision
    summary: NonBlank4000
    artifact_ids: Annotated[list[NonBlank200], Field(max_length=128)]
    provenance: AgentProvenance


class ReviewRequest(StrictRequest):
    subject_type: Literal["proposal", "experiment", "attempt", "observation"]
    subject_id: NonBlank200
    expected_revision: StrictRevision
    reason: NonBlank4000


def register_workflow(server, client: ControlClient):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    def project(collection):
        return f"/projects/{segment(client.config.project_id)}/{collection}"

    async def submit(collection, payload, key):
        return await client._request(
            "POST", project(collection), body=payload.model_dump(mode="json"), key=key
        )

    @server.tool(annotations=write)
    async def tessera_request_authority(
        bounds: AuthorityBounds, idempotency_key: str
    ) -> dict[str, Any]:
        """Request human-issued bounded authority. Never issue or approve a grant."""
        return await client._request(
            "POST",
            project("agent-authority-requests"),
            body={"bounds": bounds.model_dump(mode="json")},
            key=idempotency_key,
        )

    @server.tool(annotations=read)
    async def tessera_list_authority_requests(offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """Read only this agent's authority requests."""
        return await client._request(
            "GET", client._collection("agent-authority-requests", offset, limit)
        )

    @server.tool(annotations=read)
    async def tessera_authority_status(grant_id: str) -> dict[str, Any]:
        """Read grant epoch, revocation, expiry and remaining budget."""
        return await client._request("GET", f"/agent-authority-grants/{segment(grant_id)}")

    @server.tool(annotations=write)
    async def tessera_consume_grant(
        grant_id: str, payload: Consume, idempotency_key: str
    ) -> dict[str, Any]:
        """Admit, dispatch, status or cancel only a separately human-granted binding.

        Persist key and exact arguments before calling; reuse on reconnect. Ambiguous
        transport failure is not permission to mint a new key or broaden authority.
        Results describe control/execution state, never scientific validity.
        """
        return await client._request(
            "POST",
            f"/agent-authority-grants/{segment(grant_id)}/consume",
            body=payload.model_dump(mode="json"),
            key=idempotency_key,
        )

    @server.tool(annotations=write)
    async def tessera_propose_research(
        payload: ResearchProposal, idempotency_key: str
    ) -> dict[str, Any]:
        """Propose project, study or experiment for human review, not canonical creation."""
        return await submit("agent-workflow-proposals", payload, idempotency_key)

    @server.tool(annotations=write)
    async def tessera_record_observation(
        payload: Observation, idempotency_key: str
    ) -> dict[str, Any]:
        """Submit caller-attested observation, not human testimony or verification."""
        return await submit("agent-observations", payload, idempotency_key)

    @server.tool(annotations=write)
    async def tessera_request_review(
        payload: ReviewRequest, idempotency_key: str
    ) -> dict[str, Any]:
        """Ask a separately authenticated human to review; never accept a finding."""
        return await submit("agent-review-requests", payload, idempotency_key)

    @server.tool(annotations=read)
    async def tessera_notebook(
        offset: int = 0, limit: int = 20, after_event_cursor: int = 0
    ) -> dict[str, Any]:
        """Read redacted notebook. offset is a sequence cursor, not a row offset.

        Persist both next.after_sequence and next.after_event_cursor for continuation.
        Partial pages never establish completeness of unrecorded or off-platform work.
        """
        return await client._request(
            "GET",
            notebook_path(
                client.config.project_id,
                after_sequence=offset,
                after_event_cursor=after_event_cursor,
                limit=limit,
            ),
        )

    @server.tool(annotations=read)
    async def tessera_export_notebook(
        after_sequence: int = 0,
        after_event_cursor: int = 0,
        limit: int = 100,
        max_bytes: int = 2097152,
    ) -> dict[str, Any]:
        """Export scoped JSON pages using both next cursors; no filesystem writes/uploads."""
        return await client._request(
            "GET",
            notebook_path(
                client.config.project_id,
                export=True,
                after_sequence=after_sequence,
                after_event_cursor=after_event_cursor,
                limit=limit,
                max_bytes=max_bytes,
            ),
            max_bytes=max_bytes,
        )
