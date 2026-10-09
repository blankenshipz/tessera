"""Public setup records; none of these JSON records is a runtime capability."""

from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from tessera_control.runtime_binding_models import (
    Digest,
    IssueRuntimeEnrollment,
    ResolveRuntimeBinding,
    RuntimeBindingDTO,
    RuntimePins,
    RuntimePolicyRecord,
)


class OwnerBinding(RuntimeBindingDTO):
    adapter_file: str
    adapter_sha256: Digest
    resource_reference_file: str
    signed_manifest_file: str
    approved_manifest_sha256: Digest
    approval_reference: str


class SetupProfile(RuntimeBindingDTO):
    version: Literal["tessera-runtime-setup-v1"]
    origin: str
    audience: Literal["control", "worker"]
    destination: str
    project_id: str
    human_credential_file: str
    subject_credential_file: str
    publisher_credential_file: str
    ca_file: str
    owner: OwnerBinding
    policy: RuntimePolicyRecord | None = None
    enrollment_request: IssueRuntimeEnrollment | None = None
    resolve_request: ResolveRuntimeBinding | None = None


class InstalledReadback(RuntimeBindingDTO):
    """Owner-measured public projection, not an approval Boolean."""

    manifest_sha256: Digest
    signature_reference: str = Field(min_length=1, max_length=160)
    approval_reference: str = Field(min_length=1, max_length=160)
    process_link_reference: str = Field(min_length=1, max_length=160)
    publisher_ref: Digest
    opaque_owner_ref: Digest
    session_ref: Digest
    subject_credential_id: str = Field(min_length=1, max_length=160)
    publisher_credential_id: str = Field(min_length=1, max_length=160)
    pins: RuntimePins


@dataclass(frozen=True)
class InstalledOwnerCapability:
    """Only a trusted pinned executable adapter may return this capability.

    assert_current must recheck signed manifest approval, loaded process and private
    resource linkage. A caller JSON record or signature status cannot supply it.
    """

    readback: InstalledReadback
    assert_current: object
