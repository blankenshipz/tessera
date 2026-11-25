"""Frozen public-fetch plan and the worker's fetch report (ADR 0097).

The plan travels in the digest-bound execution spec of a local-cpu stage and is what the
worker supervisor may download before the container starts. It is imported by ``models`` so it
must not import it.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
InputName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
FileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Host = Annotated[str, StringConstraints(min_length=1, max_length=253)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
MAX_FILE_BYTES = 8 * 1024**3
MAX_FILES = 32


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD = re.compile(
    r"^(?:[a-z][a-z0-9-]*[a-z0-9]|[a-z])$"
)  # starts with a letter: never numeric or 0x..
# Names that only ever resolve inside a network (the single-label and resolver-search forms too).
_INTERNAL_SUFFIXES = (
    "localhost",
    "local",
    "internal",
    "localdomain",
    "lan",
    "intranet",
    "corp",
    "home",
    "private",
    "home.arpa",
)


def canonical_public_host(host: str | None) -> str | None:
    """The canonical form of a public fully qualified DNS name, else ``None`` (fail closed).

    One trailing dot is stripped (the same name to a resolver), the name is lowercased and
    IDNA-encoded, and the result must be at least two ordinary DNS labels whose last label starts
    with a letter. That last rule refuses every IP literal in every spelling a resolver or URL
    parser accepts (dotted quad, hex ``0x7f.0.0.1``, octal, a single integer, short forms such as
    ``127.1``) without trying to parse them, and IPv6 (including IPv4-mapped) never matches a DNS
    label. Internal names (``localhost``, ``*.local``, ``*.internal``, single labels, ...) are
    refused.
    """
    if not host:
        return None
    if host.endswith("."):
        host = host[:-1]
    if not host or host.endswith(".") or host.startswith("."):
        return None
    try:
        host = host.lower().encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253:
        return None
    if not all(_LABEL.fullmatch(label) for label in labels) or not _TLD.fullmatch(labels[-1]):
        return None
    if any(host == suffix or host.endswith("." + suffix) for suffix in _INTERNAL_SUFFIXES):
        return None
    return host


def public_fetch_host(url: str) -> str | None:
    """The canonical host of an acceptable pinned-download URL, else ``None`` (fail closed)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or host is None
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.query
        or parts.fragment
        or parts.netloc != parts.netloc.lower()  # urlsplit lowercases hostname itself
        or not parts.path.startswith("/")
    ):
        return None
    return canonical_public_host(host)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicFetchFile(_Strict):
    input: InputName
    name: FileName
    url: Annotated[str, StringConstraints(min_length=9, max_length=2048, pattern=r"^https://")]
    sha256: Sha256
    max_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_FILE_BYTES)]


class PublicFetchPlan(_Strict):
    """What the executing worker may fetch: pinned files and the only hosts it may contact."""

    hosts: Annotated[list[Host], Field(min_length=1, max_length=40)]
    files: Annotated[list[PublicFetchFile], Field(min_length=1, max_length=MAX_FILES)]
    max_bytes_total: Annotated[int, Field(strict=True, ge=1)]
    cache: bool

    @model_validator(mode="after")
    def _consistent(self) -> PublicFetchPlan:
        keys = [(f.input, f.name) for f in self.files]
        if len(set(keys)) != len(keys):
            raise ValueError("public fetch file names must be unique within an input")
        if self.max_bytes_total != sum(f.max_bytes for f in self.files):
            raise ValueError("public fetch byte total must equal the sum of its files")
        if len(set(self.hosts)) != len(self.hosts):
            raise ValueError("public fetch hosts must be unique")
        # The same host rules the spec layer applies (ADR 0097): a plan sent through any other
        # door can never name an internal host or a URL with a query, and every pinned file's
        # host is one of the plan's hosts.
        for host in self.hosts:
            if canonical_public_host(host) != host:
                raise ValueError("public fetch hosts are canonical public fully qualified hosts")
        allowed = set(self.hosts)
        for item in self.files:
            host = public_fetch_host(item.url)
            if host is None:
                raise ValueError(
                    "a public file source is an https URL on a public fully qualified host "
                    "(no credentials, no port, no query or fragment)"
                )
            if host not in allowed:
                raise ValueError("a public file's host must be one of the plan's hosts")
        return self


class PublicFetchRow(_Strict):
    input: InputName
    name: FileName
    source_host: Host
    sha256: Sha256
    bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_FILE_BYTES)]
    seconds: Annotated[float, Field(ge=0, le=7 * 86400)]
    # ``cached`` is the hit/miss boolean every row has always carried. ``cache`` is the optional
    # tri-state outcome (absent when the spec never asked for the cache); ``unavailable`` means
    # the runtime had no usable storage, so there is no boolean to report.
    cached: Annotated[bool, Field(strict=True)] | None = None
    cache: Literal["hit", "miss", "unavailable"] | None = None

    @model_validator(mode="after")
    def _cache_consistent(self) -> PublicFetchRow:
        if self.cache is None:
            if self.cached is None:
                raise ValueError("a public fetch row reports cached or cache")
        elif self.cache == "unavailable":
            if self.cached is not None:
                raise ValueError("an unavailable cache has no hit or miss boolean")
        elif self.cached is not None and self.cached != (self.cache == "hit"):
            raise ValueError("cached must agree with the cache outcome")
        return self


class ReportPublicFetch(_Strict):
    lease_id: Identifier
    expected_epoch: Annotated[int, Field(strict=True, ge=1)]
    files: Annotated[list[PublicFetchRow], Field(min_length=1, max_length=MAX_FILES)]
