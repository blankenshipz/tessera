"""Frozen image-build policy, the builder's result row, and the worker's report (ADR 0123).

Shared by the control plane and the worker supervisor, so it imports nothing from either. Every
field here is validated strictly: the supervisor re-checks what control froze, and control
re-checks what the worker reports.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# The closed set of reasons a build is reported as failed. Free text never reaches control.
FAILURE_CODES = frozenset(
    {
        "build_exit_nonzero",  # the builder container exited non-zero
        "result_missing",  # no /outputs/image-build.json
        "result_unsafe",  # not a regular file, or a link
        "result_too_large",
        "result_invalid",  # not JSON, or not the strict result schema
        "result_mismatch",  # well formed, but contradicts the frozen policy
        "policy_invalid",  # the frozen policy failed the supervisor's own strict re-check
    }
)
RESULT_SCHEMA = "tessera.image-build.v1"
RESULT_PATH = "image-build.json"
MAX_RESULT_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 1024**4

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Repository = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/[a-z0-9]+(?:[._/-][a-z0-9]+)*$",
        max_length=255,
    ),
]
# An operator-registered push destination: ``host[:port]`` or ``host[:port]/path`` (lowercase).
RegisteredRepository = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._/-][a-z0-9]+)*)?$",
        max_length=255,
    ),
]


def repository_registered(repository: str, registered: Any) -> bool:
    """True when ``repository`` is a registered destination or a path under one (ADR 0123).

    A registered entry matches on whole path segments: ``reg.example/team`` covers
    ``reg.example/team`` and ``reg.example/team/app`` but not ``reg.example/team2/app``. An empty
    or absent registry matches nothing, so a build push is refused until the operator names one."""
    if not isinstance(repository, str) or not isinstance(registered, (list, tuple, frozenset, set)):
        return False
    return any(
        isinstance(entry, str)
        and entry != ""
        and (repository == entry or repository.startswith(entry.rstrip("/") + "/"))
        for entry in registered
    )


BaseImage = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}$", max_length=330
    ),
]
InputName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
DockerfilePath = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,198}$", max_length=200)
]
Host = Annotated[str, StringConstraints(min_length=1, max_length=253)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuildPolicyModel(_Strict):
    """The ``build_policy`` dict a build stage freezes into its execution spec."""

    builder_image: Annotated[str, StringConstraints(max_length=330)]
    context_input: InputName
    dockerfile: DockerfilePath
    base_images: Annotated[list[BaseImage], Field(min_length=1, max_length=8)]
    repository: Repository
    registry_host: Host
    hosts: Annotated[list[Host], Field(min_length=1, max_length=16)]
    push: Annotated[bool, Field(strict=True)]
    max_transfer_bytes: Annotated[int, Field(strict=True, ge=1, le=32 * 1024**3)]

    @model_validator(mode="after")
    def _consistent(self) -> BuildPolicyModel:
        if ".." in self.dockerfile.split("/") or "//" in self.dockerfile:
            raise ValueError("the Dockerfile path stays inside the build context")
        if self.repository.split("/", 1)[0].partition(":")[0] != self.registry_host:
            raise ValueError("registry_host must be the repository's host")
        if self.registry_host not in self.hosts:
            raise ValueError("the registry host is one of the allowed hosts")
        return self


class ImageBuildRow(_Strict):
    """The closed result row: what control records for one built image."""

    image: Annotated[str, StringConstraints(max_length=300)]
    digest: Digest
    size_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_IMAGE_BYTES)]
    pushed: Annotated[bool, Field(strict=True)]
    base_images: Annotated[list[BaseImage], Field(min_length=1, max_length=8)]
    seconds: Annotated[float, Field(strict=True, ge=0, le=86400)] | None = None

    @model_validator(mode="after")
    def _image_matches_digest(self) -> ImageBuildRow:
        if not self.image.endswith(f"@sha256:{self.digest}"):
            raise ValueError("the image reference names its own digest")
        return self


class ReportImageBuild(_Strict):
    lease_id: Identifier
    expected_epoch: Annotated[int, Field(strict=True, ge=1)]
    result: ImageBuildRow | None = None
    failed: str | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> ReportImageBuild:
        if (self.result is None) == (self.failed is None):
            raise ValueError("report exactly one of result or failed")
        if self.failed is not None and self.failed not in FAILURE_CODES:
            raise ValueError("failed must be a closed failure code")
        return self


def check_row_against_policy(policy: dict[str, Any], row: dict[str, Any]) -> bool:
    """True when ``row`` is consistent with the frozen build policy."""
    repository = policy["repository"]
    return (
        row["image"] == f"{repository}@sha256:{row['digest']}"
        and row["image"].split("/", 1)[0].partition(":")[0] == policy["registry_host"]
        and row["pushed"] is policy["push"]
        and row["base_images"] == policy["base_images"]
        and row["size_bytes"] <= policy["max_transfer_bytes"]
    )
