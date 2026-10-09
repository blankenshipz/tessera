"""Caller-attested collaboration; no scientific approval or execution receipts."""

from typing import Annotated, Literal

from pydantic import Field

from .experiments_models import CreateExperiment
from .external_agent_models import AgentProvenance
from .models import NonBlank200, NonBlank4000, StrictRequest, StrictRevision
from .research_models import CreateStudy


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


class AgentObservation(StrictRequest):
    attempt_id: NonBlank200
    expected_attempt_revision: StrictRevision
    summary: NonBlank4000
    artifact_ids: Annotated[list[NonBlank200], Field(max_length=128)]
    provenance: AgentProvenance


class AgentReviewRequest(StrictRequest):
    subject_type: Literal["proposal", "experiment", "attempt", "observation"]
    subject_id: NonBlank200
    expected_revision: StrictRevision
    reason: NonBlank4000
