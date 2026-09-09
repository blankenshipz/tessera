"""Strict metadata-only credential-access lease commands."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class CredentialAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OpaqueSecretRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]
ScopeValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Revision = Annotated[int, Field(strict=True, ge=1)]


class CreateCredentialAccessLease(CredentialAccessRequest):
    execution_lease_id: Identifier
    expected_epoch: Revision
    secret_ref: OpaqueSecretRef
    audience: Identifier
    purpose: Identifier
    scope: Annotated[list[ScopeValue], Field(min_length=1, max_length=32)]
    expires_at: datetime

    @field_validator("scope")
    @classmethod
    def scope_is_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("scope entries must be unique")
        return value

    @field_validator("expires_at", mode="before")
    @classmethod
    def expiry_has_offset(cls, value: object) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("expires_at requires ISO-8601 time") from error
        if not isinstance(value, datetime):
            raise ValueError("expires_at requires ISO-8601 time")
        if value.tzinfo is None:
            raise ValueError("expires_at requires an offset")
        return value


class RenewCredentialAccessLease(CredentialAccessRequest):
    expected_revision: Revision
    expires_at: datetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def expiry_has_offset(cls, value: object) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("expires_at requires ISO-8601 time") from error
        if not isinstance(value, datetime):
            raise ValueError("expires_at requires ISO-8601 time")
        if value.tzinfo is None:
            raise ValueError("expires_at requires an offset")
        return value


class RevokeCredentialAccessLease(CredentialAccessRequest):
    expected_revision: Revision
