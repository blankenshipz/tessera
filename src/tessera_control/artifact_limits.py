"""Operator-configurable size limits for large content-addressed artifacts.

Artifacts at or below ``LEGACY_INLINE_BYTES`` keep the original single-BLOB quarantine path
byte for byte. Larger artifacts (for example model weights) are stored as fixed-size chunks
and verified with one sha256 over the whole artifact, streamed with bounded memory.

The defaults suit multi-gigabyte files on an ordinary install. They are ceilings on what an
approved plan may ask for, not allocations. Operators change them with the environment
variables below, set for the control service before it starts (they are read once, on first
use, by ``operator_limits``);
a malformed value, or one outside the allowed range, stops startup with a clear error.
This module is stdlib-only so workers can import it.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Mapping
from dataclasses import dataclass

MiB = 1024 * 1024
GiB = 1024 * MiB

LEGACY_INLINE_BYTES = 16 * MiB
CHUNK_BYTES = 4 * MiB
DEFAULT_MAX_ARTIFACT_BYTES = 8 * GiB
DEFAULT_MAX_PROJECT_BYTES = 32 * GiB
ABSOLUTE_MAX_ARTIFACT_BYTES = 1024 * GiB
# The longest an exited run's outputs may still arrive after control recorded its exit: the hold
# of an exited attempt past its grant's expiry (ADR 0064) and the limit of the not-late rule for
# its artifacts and manifest (ADR 0086).
MAX_EXTENDED_HOLD_SECONDS = 2 * 3600
# How long after control recorded a run's confirmed exit its outputs may still register (begin an
# upload) and ride the hold or the not-late rule (ADRs 0064, 0086). A worker that reports its exit
# and then keeps computing gains at most this much before it must register.
OUTPUT_REGISTRATION_GRACE_SECONDS = 120

# The control service refuses a request body above this (a reverse proxy in front may bound it
# lower: set this to the smaller of the two). It also decides how large a part of a large-artifact
# chunk a worker is told to send, so an upload never depends on a body the path would refuse.
DEFAULT_MAX_REQUEST_BYTES = 24 * MiB
MIN_MAX_REQUEST_BYTES = 512 * 1024
ABSOLUTE_MAX_REQUEST_BYTES = 256 * MiB
# JSON envelope, field names and headroom reserved around the base64 of one upload part.
REQUEST_ENVELOPE_BYTES = 16 * 1024
PART_GRANULE_BYTES = 64 * 1024
MIN_PART_BYTES = PART_GRANULE_BYTES

ENV_MAX_ARTIFACT = "TESSERA_MAX_ARTIFACT_BYTES"
ENV_MAX_REQUEST = "TESSERA_MAX_REQUEST_BYTES"
ENV_MAX_PROJECT = "TESSERA_MAX_PROJECT_ARTIFACT_BYTES"
ENV_ORPHAN_SECONDS = "TESSERA_ORPHAN_ARTIFACT_SECONDS"
DEFAULT_ORPHAN_SECONDS = 7 * 24 * 3600
MIN_ORPHAN_SECONDS = 3600
MAX_ORPHAN_SECONDS = 365 * 24 * 3600


def part_bytes_for(max_request_bytes: int) -> int:
    """Raw bytes of one upload part whose base64 and envelope fit a request of this size.

    A multiple of 64 KiB, at least 64 KiB and at most one storage chunk (4 MiB). With the 24 MiB
    default a whole chunk fits, so nothing changes; behind a 1 MiB limit it is 704 KiB."""
    usable = (max_request_bytes - REQUEST_ENVELOPE_BYTES) * 3 // 4
    return max(MIN_PART_BYTES, min(CHUNK_BYTES, usable // PART_GRANULE_BYTES * PART_GRANULE_BYTES))


def request_bytes_for_part(part_bytes: int) -> int:
    """The request size a part of this many raw bytes needs (base64 plus the envelope)."""
    return 4 * ((part_bytes + 2) // 3) + REQUEST_ENVELOPE_BYTES


class ArtifactLimitError(ValueError):
    """A configured artifact limit is malformed or out of range."""


@dataclass(frozen=True)
class ArtifactLimits:
    # Largest single output file, and largest chunked snapshot, this installation accepts.
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES
    # Chunked bytes (large artifacts plus chunked snapshots) one project may hold.
    max_project_bytes: int = DEFAULT_MAX_PROJECT_BYTES
    # A completed large artifact that no manifest lists is reclaimed after this many seconds
    # (default 7 days; 1 hour to 365 days).
    orphan_artifact_seconds: int = DEFAULT_ORPHAN_SECONDS
    # Largest HTTP request body control accepts (see DEFAULT_MAX_REQUEST_BYTES).
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES

    def __post_init__(self) -> None:
        for value in (
            self.max_artifact_bytes,
            self.max_project_bytes,
            self.orphan_artifact_seconds,
            self.max_request_bytes,
        ):
            if type(value) is not int:
                raise ArtifactLimitError("Artifact limits must be integers.")
        if not LEGACY_INLINE_BYTES <= self.max_artifact_bytes <= ABSOLUTE_MAX_ARTIFACT_BYTES:
            raise ArtifactLimitError("The per-artifact limit must lie between 16 MiB and 1 TiB.")
        if not MIN_MAX_REQUEST_BYTES <= self.max_request_bytes <= ABSOLUTE_MAX_REQUEST_BYTES:
            raise ArtifactLimitError("The request size limit must lie between 512 KiB and 256 MiB.")
        if not MIN_ORPHAN_SECONDS <= self.orphan_artifact_seconds <= MAX_ORPHAN_SECONDS:
            raise ArtifactLimitError("The orphan age must lie between 1 hour and 365 days.")
        if (
            not self.max_artifact_bytes
            <= self.max_project_bytes
            <= 64 * ABSOLUTE_MAX_ARTIFACT_BYTES
        ):
            raise ArtifactLimitError(
                "The per-project limit must be at least the per-artifact limit."
            )


def _read(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ArtifactLimitError(f"{name} must be a whole number of bytes.") from error
    return value


def artifact_limits(environ: Mapping[str, str] | None = None) -> ArtifactLimits:
    """The effective limits: defaults, overridden by the operator's environment."""
    env = os.environ if environ is None else environ
    artifact = _read(env, ENV_MAX_ARTIFACT, DEFAULT_MAX_ARTIFACT_BYTES)
    project = _read(env, ENV_MAX_PROJECT, max(DEFAULT_MAX_PROJECT_BYTES, artifact))
    orphan = _read(env, ENV_ORPHAN_SECONDS, DEFAULT_ORPHAN_SECONDS)
    request = _read(env, ENV_MAX_REQUEST, DEFAULT_MAX_REQUEST_BYTES)
    return ArtifactLimits(artifact, project, orphan, request)


@functools.lru_cache(maxsize=1)
def operator_limits() -> ArtifactLimits:
    """The process-wide operator limits, read from the environment once.

    Plan submission, package build, upload, manifest, the chunk store and the worker call
    this, so they cannot disagree within a process. A few module-level constants (the
    chunked-snapshot bounds, the provider output total and the Salad input ceiling) are
    snapshots of it taken at import, because they bound pydantic fields; they follow the
    same environment but are not refreshed by ``cache_clear()``. Tests that change the
    environment call ``operator_limits.cache_clear()`` and patch those constants directly.
    """
    return artifact_limits()
