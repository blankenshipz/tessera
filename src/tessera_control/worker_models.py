"""Strict worker lifecycle command bodies."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Metadata = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RegisterWorker(WorkerRequest):
    worker_principal_id: Identifier
    approved_build_identifier: Metadata
    approved_build_digest: Digest
    trust_class: Metadata
    region: Metadata


class WorkerLifecycle(WorkerRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class WorkerHeartbeat(WorkerRequest):
    pass
