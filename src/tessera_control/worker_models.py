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
    """A liveness beat. The memory figures are optional and, when sent, are what the worker
    host reports about itself (ADR 0067): memory available now, and the memory limit its
    stage containers run with. They feed placement and retry decisions and nothing else."""

    memory_available_bytes: Annotated[int, Field(strict=True, ge=0, le=2**50)] | None = None
    container_memory_bytes: Annotated[int, Field(strict=True, ge=1_048_576, le=2**50)] | None = None
