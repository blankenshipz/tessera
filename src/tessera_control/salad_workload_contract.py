"""Reviewed generic GPU workload contract for Salad (ADR 0036 phase 1).

This is reviewed profile data, not an execution grant and not a template engine:
only the three named placeholders are ever substituted. Egress declarations are
recorded for review and capability matching; Salad's provider-wide egress is not
enforced by Tessera, so a route carrying this contract never claims network-none.
"""

import base64
import binascii
import hashlib
import json
import math
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from .artifact_limits import operator_limits
from .capability_models import Destination, Strict

CONTRACT_ID = "tessera.salad.workload.v1"
COMMAND = "/tessera/bin/run"
PLACEHOLDERS = ("{operation}", "{epoch}", "{custody_host}")
RESERVED_UPLOAD_GRACE_SECONDS = 120
# Output custody (ADR 0036 phase 3). Raw bytes per upload chunk and per import chunk.
OUTPUT_CHUNK_BYTES = 1 << 20
# The single rule for a declared output file name: the contract, custody and the
# importer use it; the standalone runtime keeps an equal copy (tests assert equality).
OUTPUT_NAME_PATTERN = r"[a-z0-9][a-z0-9._-]{0,63}"
OUTPUT_NAME_RE = re.compile(OUTPUT_NAME_PATTERN)
# Platform total across all declared outputs: SaladExecution and ExecutionSpec cap
# max_output_bytes at 16 MiB, so custody never reserves more than that per operation.
MAX_TOTAL_OUTPUT_BYTES = 16 * 1024 * 1024
# Shape the original single-object endpoint (/v1/workload/output) still accepts.
LEGACY_OUTPUT_NAME = "output.json"
LEGACY_OUTPUT_MAX_BYTES = 65536
_PRINTABLE = re.compile(r"^[\x20-\x7e]*$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_WORDS = (
    "KEY", "AUTH", "PASS", "PASSWD", "PRIVATE", "TOKEN", "SECRET", "CREDENTIAL",
)  # fmt: skip
_REFUSED_PREFIXES = ("LD_", "AWS_", "AZURE_")
_REFUSED_NAMES = {"PATH", "PYTHONPATH", "BASH_ENV", "ENV", "GOOGLE_APPLICATION_CREDENTIALS"}
_ALLOWED_PYTHON = {"PYTHONUNBUFFERED"}
_CREDENTIAL_PREFIXES = ("-----BEGIN", "sk-", "hf_", "ghp_", "AKIA")
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")
_HEX = re.compile(r"^[A-Fa-f0-9]+$")


def _entropy(text):
    counts = {c: text.count(c) for c in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def env_refusal(key, value):
    """Reason a reviewed env entry is refused, else None. Shape allowlist plus refusals."""
    if not _ENV_KEY.fullmatch(key):
        return "malformed name"
    if (
        key in _REFUSED_NAMES
        or key.startswith(_REFUSED_PREFIXES)
        or (key.startswith("PYTHON") and key not in _ALLOWED_PYTHON)
    ):
        return "process-control or cloud-credential name"
    if any(word in key for word in _SECRET_WORDS):
        return "secret-like name"
    if len(value) > 512 or not _PRINTABLE.fullmatch(value):
        return "value must be at most 512 printable characters"
    if value.startswith(_CREDENTIAL_PREFIXES):
        return "credential-like value"
    if _BASE64ISH.fullmatch(value) and not _HEX.fullmatch(value) and _entropy(value) >= 4.0:
        return "high-entropy token-like value"
    return None


_HOST = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")


class Closed(Strict):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


Token = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$")]


class OutputFile(Closed):
    name: Annotated[str, StringConstraints(pattern=f"^{OUTPUT_NAME_PATTERN}$")]
    max_bytes: int = Field(ge=1, le=67108864)


def fetch_destination(url: str) -> Destination:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or any(c.isspace() or not c.isprintable() for c in url)
    ):
        raise ValueError("fetch url must be an https URL with a host and no credentials")
    try:
        port = parts.port or 443
    except ValueError:
        raise ValueError("invalid fetch url port") from None
    if parts.netloc != parts.netloc.lower():
        raise ValueError("fetch url host must be lowercase")
    return Destination(host=parts.hostname, port=port)


class Fetch(Closed):
    url: Annotated[str, StringConstraints(max_length=512)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    max_bytes: int = Field(ge=1, le=8589934592)

    @field_validator("url")
    @classmethod
    def https_url(cls, value):
        fetch_destination(value)
        return value


# Declared fetches per contract. 128 rows leaves room for a pinned dependency set
# plus a model revision while the manifest (at most 256 KiB as a file in the input
# snapshot) stays small, distinct hosts stay at most 32, each fetch keeps its own
# max_bytes cap, and the selection bounds their sum by disk_mb.
MAX_FETCHES = 128


# Reviewed HTTP redirect targets (gpu_runtime_v1): a pinned fetch URL may answer
# with a redirect to a CDN whose signed URL expires and so cannot be pinned. The
# fetch's sha256 and max_bytes still bind the bytes; these exact hosts (HTTPS,
# port 443) are the only places a redirect may lead, and they join the capability
# network declaration reviewers approve.
MAX_REDIRECT_HOSTS = 16
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_REDIRECT_HOST = re.compile(rf"^(?=.{{1,253}}$){_DNS_LABEL}(?:\.{_DNS_LABEL})+$")


def redirect_host_refusal(host):
    """Reason ``host`` is not an exact lowercase DNS name, else None."""
    if not isinstance(host, str) or not _REDIRECT_HOST.fullmatch(host):
        return "must be an exact lowercase DNS name (no wildcard, port or scheme)"
    if any(label.isdigit() or label.startswith("0x") for label in host.split(".")):
        return "must be a DNS name: no all-digit or 0x label (no IP-like form)"
    return None


def redirect_hosts_digest(hosts):
    """sha256 of the canonical JSON list of redirect hosts; the runtime derives the same.

    The argv ``redirect-hosts:`` token carries it, binding the manifest's
    ``redirect_hosts`` (inline or carried in the input) to ``egress.redirect_hosts``.
    """
    raw = json.dumps(list(hosts), separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class Egress(Closed):
    kind: Literal["provider_broad"]
    fetches: Annotated[list[Fetch], Field(max_length=MAX_FETCHES)]
    # Optional, reviewed: sorted, unique exact DNS names a fetch may redirect to.
    # Empty (the default) means no redirect is followed, as before; it is then not
    # serialized, so existing contract and proposal digests are unchanged.
    redirect_hosts: Annotated[
        list[Annotated[str, StringConstraints(max_length=253)]],
        Field(max_length=MAX_REDIRECT_HOSTS),
    ] = []

    @model_serializer(mode="wrap")
    def stable_shape(self, handler):
        data = handler(self)
        if not self.redirect_hosts:
            data.pop("redirect_hosts", None)
        return data

    @field_validator("redirect_hosts")
    @classmethod
    def exact_redirect_hosts(cls, value):
        for host in value:
            reason = redirect_host_refusal(host)
            if reason:
                raise ValueError(f"redirect host {reason}")
        if value != sorted(set(value)):
            raise ValueError("redirect_hosts must be sorted and unique")
        return value

    @model_validator(mode="after")
    def bounded_destinations(self):
        if len(self.destinations()) > 32:
            raise ValueError("at most 32 distinct fetch and redirect destinations")
        return self

    def destinations(self):
        """Sorted distinct (host, port) pairs the workload may connect to for fetches.

        Each fetch URL's own host and port, plus every reviewed redirect host on
        443 (the runtime follows a redirect only over HTTPS on the default port).
        """
        found = {(d.host, d.port) for d in map(fetch_destination, (f.url for f in self.fetches))}
        return sorted(found | {(h, 443) for h in self.redirect_hosts})

    def declared_bytes(self):
        return sum(f.max_bytes for f in self.fetches)


def fetches_digest(rows):
    """sha256 of the sorted (url, sha256, max_bytes) rows; the runtime derives the same.

    Binds a manifest carried inside the input snapshot (whose rows the contract
    cannot read at validation time) to ``egress.fetches``: the runtime refuses a
    manifest whose rows hash differently.
    """
    canonical = sorted([r[0], r[1], r[2]] for r in rows)
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# The largest input a workload can declare: the operator artifact bound (ADR 0037 chunked
# snapshots; default 8 GiB, was a fixed 2 GiB).
MAX_INPUT_BYTES = operator_limits().max_artifact_bytes
# Guardian input replies (/v1/workload/input): the workload reads each stored 4 MiB
# chunk in ranges of at most INPUT_RANGE_BYTES raw, so every reply (a range, or the
# manifest with its chunk index) is at most INPUT_REPLY_BYTES encoded and fits the
# guardian's rate-derived chunk request budget (asserted in salad_safety_service).
INPUT_RANGE_BYTES = 1 << 20
INPUT_REPLY_BYTES = INPUT_RANGE_BYTES * 4 // 3 + 4096


class InputSnapshot(Closed):
    """A reviewed declaration that the workload expects a staged input snapshot.

    ``snapshot_digest`` must equal the route's ``manifests.data`` (enforced by the
    execution selection) and, for gpu_runtime_v1, the argv ``input:`` token; the
    runtime fetches and verifies against it. ``max_bytes`` is the reviewed cap on
    the snapshot's total size.
    """

    kind: Literal["staged_snapshot"]
    snapshot_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    max_bytes: int = Field(ge=1, le=MAX_INPUT_BYTES)


GPU_RUNTIME_V1 = "gpu_runtime_v1"
GPU_RUNTIME_MANIFEST_SCHEMA = "tessera.gpu-runtime.manifest.v1"
GPU_RUNTIME_MIN_SECONDS = 60
_HEX64 = re.compile(r"^[a-f0-9]{64}$")


_INPUT_TOKEN = re.compile(r"^input:([a-f0-9]{64}):([1-9][0-9]{0,9})$")
REDIRECT_TOKEN_PREFIX = "redirect-hosts:"
_REDIRECT_TOKEN = re.compile(r"^redirect-hosts:([a-f0-9]{64})$")
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_INPUT_MANIFEST_TOKEN = re.compile(
    rf"^input-manifest:([a-f0-9]{{64}}):({_SEGMENT}(?:/{_SEGMENT}){{0,7}})$"
)


def parse_gpu_runtime_manifest(raw, digest):
    """Decoded manifest bytes -> manifest dict, checked against MANIFEST_SHA256."""
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("gpu_runtime_v1 manifest sha256 does not match argv")
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise ValueError("gpu_runtime_v1 manifest is not valid JSON") from None
    rows = manifest.get("fetches") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != GPU_RUNTIME_MANIFEST_SCHEMA
        or not isinstance(rows, list)
        or not all(isinstance(r, dict) for r in rows)
    ):
        raise ValueError("gpu_runtime_v1 manifest malformed")
    return manifest


# An item served from the verified input snapshot instead of a URL fetch:
# ``{"kind": "entry"|"file", "source": "input", "path": RELATIVE}``. It carries no
# url, sha256 or max_bytes: the snapshot digest (``input.snapshot_digest``, bound to
# ``manifests.data``) already pins its bytes, so it is neither an egress fetch nor a
# capability network destination. The path is a subset of the input materialization
# rules (relative segments, no ``..``, at most 8 deep); the runtime opens it without
# following links and requires a regular file.
#
# Two levels of checks. The model validator (which also re-parses STORED proposals
# on the stop, heartbeat and reconcile paths) stays as lenient as before input
# items existed: it only excludes input items from the egress comparison and
# requires a declared input for them. The strict shape, path and exactly-one-entry
# rules (check_gpu_runtime_manifest_rules) run where a NEW route is staged, proposed,
# approved or admitted (setup, check_staged_input, check_workload_admission).
INPUT_SOURCE = "input"
# ADR 0076: an entry shipped in the (operator-pinned) image, not fetched and not in the input.
IMAGE_SOURCE = "image"
IMAGE_ENTRIES = ("stage_argv.py",)
IMAGE_ITEM_KEYS = frozenset({"kind", "source", "path", "argv"})
MAX_IMAGE_ARGV = 128
MAX_IMAGE_ARGV_TOKEN = 4000
INPUT_ITEM_KEYS = frozenset({"kind", "source", "path"})
INPUT_ITEM_KINDS = ("entry", "file")
URL_ITEM_KEYS = frozenset({"url", "sha256", "max_bytes", "kind", "dest"})
URL_ITEM_KINDS = ("wheel", "file", "entry")
_INPUT_ITEM_PATH = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT}){{0,7}}$")


def is_input_item(item):
    """A manifest item served from the staged input: ``source == "input"`` and no url."""
    return isinstance(item, dict) and item.get("source") == INPUT_SOURCE and "url" not in item


def is_image_item(item):
    """A manifest item resolved inside the image: ``source == "image"`` and no url (ADR 0076)."""
    return isinstance(item, dict) and item.get("source") == IMAGE_SOURCE and "url" not in item


def image_item_refusal(row):
    """Reason an image item is malformed, else None: exactly {kind, source, path, argv}."""
    argv = row.get("argv")
    if (
        set(row) != IMAGE_ITEM_KEYS
        or row.get("kind") != "entry"
        or row.get("path") not in IMAGE_ENTRIES
        or not isinstance(argv, list)
        or not 1 <= len(argv) <= MAX_IMAGE_ARGV
        or not all(
            isinstance(t, str) and 1 <= len(t) <= MAX_IMAGE_ARGV_TOKEN and "\x00" not in t
            for t in argv
        )
    ):
        return (
            "gpu_runtime_v1 image item must be exactly {kind: entry, source: image, path, argv} "
            f"with a shipped path ({', '.join(IMAGE_ENTRIES)}) and 1..{MAX_IMAGE_ARGV} argv strings"
        )
    return None


def check_gpu_runtime_manifest_rules(manifest, input_declared, redirect_hosts=()):
    """Strict new-route rules for a gpu_runtime_v1 manifest; return its input items.

    Not called by the model validator, so stored proposals keep parsing. The
    manifest has exactly the keys ``schema`` and ``fetches``, plus
    ``redirect_hosts`` exactly when the contract's ``egress.redirect_hosts`` (passed
    here) is non-empty, and then equal to it. Every
    URL item has exactly ``{url, sha256, max_bytes, kind, dest}`` (a stray key,
    such as ``source`` on a URL row, is refused); every input item is exactly
    ``{kind, source: "input", path}`` with a bounded relative path without ``..``
    (an entry ends in ``.py``), unique, and only with a declared input. The
    manifest has exactly one entry in total (URL-fetched or input-sourced).
    """
    expected_keys = {"schema", "fetches"} | ({"redirect_hosts"} if redirect_hosts else set())
    if set(manifest) != expected_keys or manifest.get("redirect_hosts", []) != list(redirect_hosts):
        raise ValueError(
            "gpu_runtime_v1 manifest keys must be schema and fetches, plus redirect_hosts "
            "equal to egress.redirect_hosts exactly when that is non-empty"
        )
    items = []
    for row in manifest["fetches"]:
        if is_image_item(row):
            reason = image_item_refusal(row)
            if reason:
                raise ValueError(reason)
            continue
        if not is_input_item(row):
            if (
                set(row) not in (URL_ITEM_KEYS, URL_ITEM_KEYS | {"cache"})
                or row["kind"] not in URL_ITEM_KINDS
            ):
                raise ValueError(
                    "gpu_runtime_v1 URL item must be exactly {url, sha256, max_bytes, kind, dest}"
                    " with an optional boolean cache"
                )
            if "cache" in row and (
                type(row["cache"]) is not bool or (row["cache"] and row["kind"] == "entry")
            ):
                raise ValueError(
                    "gpu_runtime_v1 cache is a boolean allowed only on file and wheel items"
                )
            continue
        path = row.get("path")
        if (
            set(row) != INPUT_ITEM_KEYS
            or row["kind"] not in INPUT_ITEM_KINDS
            or not isinstance(path, str)
            or not _INPUT_ITEM_PATH.fullmatch(path)
            or ".." in path.split("/")
        ):
            raise ValueError(
                "gpu_runtime_v1 input item must be exactly {kind, source: input, path} "
                "with a relative path (no url, sha256 or max_bytes)"
            )
        if row["kind"] == "entry" and not path.endswith(".py"):
            raise ValueError("gpu_runtime_v1 entry must end in .py")
        items.append(row)
    if items and not input_declared:
        raise ValueError("gpu_runtime_v1 input items need a declared input")
    if len({i["path"] for i in items}) != len(items):
        raise ValueError("gpu_runtime_v1 input item paths must be unique")
    if sum(1 for r in manifest["fetches"] if r.get("kind") == "entry") != 1:
        raise ValueError("gpu_runtime_v1 manifest needs exactly one entry")
    if any(is_image_item(r) for r in manifest["fetches"]) and len(manifest["fetches"]) != 1:
        raise ValueError("gpu_runtime_v1 image entry must be the manifest's only item")
    return items


def check_gpu_runtime_manifest(contract, raw, digest):
    """The manifest's URL rows must equal ``egress.fetches`` (model-validator level).

    Input items (``source == "input"``, no url) are not network fetches: they never
    count as egress rows (so they are absent from ``egress.fetches``,
    FETCHES_SHA256 and the capability network declaration), are bound by the
    snapshot digest instead, and need a declared input. Everything stricter is in
    check_gpu_runtime_manifest_rules, applied to new routes only.
    """
    manifest = parse_gpu_runtime_manifest(raw, digest)
    if contract.input is None and any(is_input_item(r) for r in manifest["fetches"]):
        raise ValueError("gpu_runtime_v1 input items need a declared input")
    # Sorted by repr: a malformed row (e.g. no url) is then a mismatch (ValueError),
    # never a TypeError from comparing None with str inside the model validator.
    got = sorted(
        ((r.get("url"), r.get("sha256"), r.get("max_bytes"))
         for r in manifest["fetches"] if not is_input_item(r) and not is_image_item(r)),
        key=repr,
    )  # fmt: skip
    want = sorted(((f.url, f.sha256, f.max_bytes) for f in contract.egress.fetches), key=repr)
    if got != want:
        raise ValueError("gpu_runtime_v1 manifest fetches must equal egress.fetches")
    # Only contracts that declare redirect hosts (a field newer than every stored
    # proposal) are compared here; the exact key rules are new-route checks.
    hosts = contract.egress.redirect_hosts
    if hosts and manifest.get("redirect_hosts") != list(hosts):
        raise ValueError("gpu_runtime_v1 manifest redirect_hosts must equal egress.redirect_hosts")
    return manifest


def gpu_runtime_inline_manifest(contract):
    """The decoded inline (``b64:``) manifest of a validated contract, else None."""
    if contract.runtime != GPU_RUNTIME_V1:
        return None
    _, refs = gpu_runtime_refs(contract)
    if not refs or not all(r.startswith("b64:") for r in refs):
        return None
    text = "".join(r[4:] for r in refs)
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    return parse_gpu_runtime_manifest(raw, contract.argv_template[8])


def check_gpu_runtime_inline_rules(contract):
    """New-route check of an inline manifest (with or without a declared input)."""
    manifest = gpu_runtime_inline_manifest(contract)
    if manifest is not None:
        check_gpu_runtime_manifest_rules(
            manifest, contract.input is not None, contract.egress.redirect_hosts
        )
    return manifest


def check_input_items_staged(manifest, staged_entries):
    """Every input-sourced item must be a file of the staged snapshot (its entries).

    Snapshot entries are regular files only (links and special files are never
    inventoried), so membership also means "a regular file, not a directory".
    """
    staged_paths = {e["relative_path"] for e in staged_entries}
    for item in manifest["fetches"]:
        if is_input_item(item) and item.get("path") not in staged_paths:
            raise ValueError(
                f"the manifest's input {item.get('kind')} {item.get('path')!r} is not a file "
                "of the staged input"
            )


def _gpu_runtime_tail(contract):
    """Split the argv tail into (input token or None, redirect token or None, refs)."""
    tail = list(contract.argv_template[9:])
    token = tail.pop(0) if tail and tail[0].startswith("input:") else None
    redirect = tail.pop(0) if tail and tail[0].startswith(REDIRECT_TOKEN_PREFIX) else None
    return token, redirect, tail


def gpu_runtime_refs(contract):
    """Split a gpu_runtime_v1 argv tail into (input token match or None, manifest refs)."""
    token, _, refs = _gpu_runtime_tail(contract)
    return (None if token is None else _INPUT_TOKEN.fullmatch(token)), refs


def gpu_runtime_input_manifest_path(contract):
    """Path of the manifest file inside the input snapshot, or None for inline refs."""
    _, refs = gpu_runtime_refs(contract)
    match = _INPUT_MANIFEST_TOKEN.fullmatch(refs[0]) if len(refs) == 1 else None
    return match.group(2) if match else None


def check_gpu_runtime_argv(contract):
    """Bind a gpu_runtime_v1 contract's argv layout and manifest to the contract.

    Layout (workloads/gpu_runtime_v1/README.md): ``v1 {operation} {epoch}
    {custody_host} WATCHDOG SECONDS OUTPUT_BYTES OUTPUT_FILES MANIFEST_SHA256
    [input:SNAPSHOT_SHA256:MAX_BYTES] [redirect-hosts:HOSTS_SHA256] REF...``. The
    literal numeric tokens must equal the contract's own fields. The ``input:``
    token is present exactly when the contract declares an input and must equal
    that declaration. The ``redirect-hosts:`` token is present exactly when
    ``egress.redirect_hosts`` is non-empty and must equal ``redirect_hosts_digest``
    of it; the runtime refuses a manifest whose ``redirect_hosts`` hash differently
    (and an image older than redirects refuses the token at argv parsing, before
    any fetch or claim). REF is either
    inline ``b64:`` tokens (the manifest must hash to MANIFEST_SHA256 and its
    rows must equal ``egress.fetches``) or, with a declared input, one
    ``input-manifest:FETCHES_SHA256:PATH`` token: the manifest is the file PATH
    inside the snapshot, and FETCHES_SHA256 must equal ``fetches_digest`` of
    ``egress.fetches`` (the runtime refuses a manifest whose rows hash
    differently). The ``file:`` ref form is refused: a contract pins bytes.

    A manifest item ``{"kind": "entry"|"file", "source": "input", "path": P}``
    is served from the verified input snapshot instead of a URL: it needs a
    declared input, is not an egress row (so it is in neither ``egress.fetches``,
    FETCHES_SHA256 nor the capability network) and is pinned by the snapshot
    digest; admission checks that P is a file of the staged snapshot.
    """
    t = contract.argv_template
    if len(t) < 10 or t[0] != "v1" or tuple(t[1:4]) != PLACEHOLDERS:
        raise ValueError("gpu_runtime_v1 argv layout mismatch")
    watchdog, seconds, out_bytes, out_files, digest = t[4:9]
    if not _HEX64.fullmatch(watchdog) or not _HEX64.fullmatch(digest):
        raise ValueError("gpu_runtime_v1 watchdog and manifest digest must be 64 hex")
    if contract.workload_seconds < GPU_RUNTIME_MIN_SECONDS:
        raise ValueError("gpu_runtime_v1 needs workload_seconds >= 60")
    if (
        seconds != str(contract.workload_seconds)
        or out_bytes != str(contract.output_bytes)
        or out_files != str(len(contract.outputs))
    ):
        raise ValueError("gpu_runtime_v1 literal argv tokens must equal the contract fields")
    token, refs = gpu_runtime_refs(contract)
    _, redirect, _ = _gpu_runtime_tail(contract)
    hosts = contract.egress.redirect_hosts
    if (redirect is None) != (not hosts) or (
        redirect is not None
        and (
            _REDIRECT_TOKEN.fullmatch(redirect) is None
            or redirect[len(REDIRECT_TOKEN_PREFIX) :] != redirect_hosts_digest(hosts)
        )
    ):
        raise ValueError(
            "gpu_runtime_v1 redirect-hosts token must be present exactly with "
            "egress.redirect_hosts and equal its digest"
        )
    declared = contract.input
    if t[9:] and t[9].startswith("input:") and token is None:
        raise ValueError("gpu_runtime_v1 input token malformed")
    if (token is None) != (declared is None) or (
        token is not None
        and (token.group(1), int(token.group(2))) != (declared.snapshot_digest, declared.max_bytes)
    ):
        raise ValueError("gpu_runtime_v1 input token must equal the declared input")
    if len(refs) == 1 and refs[0].startswith("input-manifest:"):
        match = _INPUT_MANIFEST_TOKEN.fullmatch(refs[0])
        if declared is None or match is None or ".." in match.group(2).split("/"):
            raise ValueError("gpu_runtime_v1 input-manifest ref needs a declared input")
        if match.group(1) != fetches_digest(
            (f.url, f.sha256, f.max_bytes) for f in contract.egress.fetches
        ):
            raise ValueError("gpu_runtime_v1 input-manifest fetches digest must bind egress")
        return
    if not refs or not all(re.fullmatch(r"b64:[A-Za-z0-9_-]{1,250}", r) for r in refs):
        raise ValueError("gpu_runtime_v1 contracts require inline b64: manifest tokens")
    text = "".join(r[4:] for r in refs)
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("gpu_runtime_v1 manifest is not valid base64url JSON") from None
    check_gpu_runtime_manifest(contract, raw, digest)


class WorkloadContractV1(Closed):
    contract: Literal["tessera.salad.workload.v1"]
    # Optional declaration that argv_template uses the gpu_runtime_v1 layout.
    runtime: Literal["gpu_runtime_v1"] | None = None
    argv_template: Annotated[list[Token], Field(min_length=1, max_length=32)]
    env: dict[str, str]
    workload_seconds: int = Field(ge=1, le=3600)
    outputs: Annotated[list[OutputFile], Field(min_length=1, max_length=16)]
    egress: Egress
    custody_host: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    # Optional reviewed declaration that a staged input snapshot is expected.
    input: InputSnapshot | None = None

    @model_serializer(mode="wrap")
    def stable_shape(self, handler):
        # Contracts without an input keep their pre-existing serialized bytes, so
        # retained proposal and code digests are unchanged.
        data = handler(self)
        if self.input is None:
            data.pop("input", None)
        return data

    @field_validator("argv_template")
    @classmethod
    def only_named_placeholders(cls, value):
        for token in value:
            if ("{" in token or "}" in token) and token not in PLACEHOLDERS:
                raise ValueError("only {operation}, {epoch}, {custody_host} placeholders")
        return value

    @field_validator("env")
    @classmethod
    def non_secret_env(cls, value):
        if len(value) > 32:
            raise ValueError("at most 32 environment variables")
        for key, item in value.items():
            reason = env_refusal(key, item)
            if reason:
                raise ValueError(f"environment entry refused: {reason}")
        return value

    @field_validator("custody_host")
    @classmethod
    def exact_host(cls, value):
        if not _HOST.fullmatch(value) or value.startswith((".", "-")) or value.endswith((".", "-")):
            raise ValueError("custody host must be an exact lowercase hostname")
        return value

    @model_validator(mode="after")
    def unique_outputs(self):
        if len({o.name for o in self.outputs}) != len(self.outputs):
            raise ValueError("duplicate output names")
        return self

    @model_validator(mode="after")
    def runtime_binding(self):
        if self.runtime == GPU_RUNTIME_V1:
            check_gpu_runtime_argv(self)
        elif self.egress.redirect_hosts:
            # Only gpu_runtime_v1 binds and enforces the redirect set; a newer
            # field than every stored proposal, so refusing it here is safe.
            raise ValueError("egress.redirect_hosts requires runtime gpu_runtime_v1")
        return self

    @property
    def output_bytes(self):
        return sum(o.max_bytes for o in self.outputs)

    def derived_argv(self, operation_id, epoch):
        """Command plus the template with exactly the three placeholders resolved."""
        values = {
            "{operation}": operation_id,
            "{epoch}": str(epoch),
            "{custody_host}": self.custody_host,
        }
        return [COMMAND, *(values.get(token, token) for token in self.argv_template)]

    def capability_network(self):
        """Capability-declaration `network` shape implied by the fetch declaration."""
        return [{"host": h, "port": p} for h, p in self.egress.destinations()]

    def outputs_supported(self):
        """Custody can hold these outputs: at most MAX_TOTAL_OUTPUT_BYTES in total."""
        return self.output_bytes <= MAX_TOTAL_OUTPUT_BYTES

    def legacy_single_output(self):
        """Exactly one output.json of at most 64 KiB: the original single-object endpoint."""
        return (
            len(self.outputs) == 1
            and self.outputs[0].name == LEGACY_OUTPUT_NAME
            and self.outputs[0].max_bytes <= LEGACY_OUTPUT_MAX_BYTES
        )

    def output_plan(self):
        """Declared outputs, in contract order, as custody and the runtime see them."""
        return [dict(name=o.name, max_bytes=o.max_bytes) for o in self.outputs]


def output_chunk_count(size):
    """Chunks a file of ``size`` bytes uploads as; an empty file is one empty chunk."""
    return max(1, -(-size // OUTPUT_CHUNK_BYTES))


def capability_network_matches(execution, declaration):
    """Salad capability network rule: empty for smoke/exact, fetch hosts for workload_v1."""
    network = declaration["network"]
    if execution.get("argv_binding") != "workload_v1":
        return not network
    contract = WorkloadContractV1.model_validate(execution["workload"])
    return sorted((d["host"], d["port"]) for d in network) == contract.egress.destinations()
