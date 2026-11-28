"""Generic reviewed GPU runtime (ADR 0036 phase 5); stdlib only.

Fetches only what a hash-pinned manifest lists, verifies every byte before use,
installs verified wheels offline, runs the single verified entry under a wall
clock, and retains every declared output with the guardian (IMDS JWT, begin-once
before the entry, the guardian's declared output plan, then chunked upload with
per-file sha256). With a declared input snapshot it pulls the snapshot from the
guardian after begin-once and verifies every chunk, file and the snapshot digest
against the reviewed argv before the entry runs. Failures are detail-free.

Argv (all tokens ``[A-Za-z0-9_.:/=-]``-class, see parse_argv)::

    v1 OPERATION EPOCH CUSTODY_HOST WATCHDOG WORKLOAD_SECONDS MAX_OUTPUT_BYTES
       MAX_OUTPUT_FILES MANIFEST_SHA256 [input:SNAPSHOT_SHA256:MAX_BYTES]
       [redirect-hosts:HOSTS_SHA256] MANIFEST_REF...

With a ``redirect-hosts:`` token the manifest must carry ``redirect_hosts``
hashing to it; a fetch may then follow at most MAX_REDIRECTS HTTPS redirects to
exactly those hosts (port 443). Without it no redirect is followed.

MANIFEST_REF is one or more ``b64:<base64url>`` tokens whose payloads are
concatenated and decoded (unpadded base64url), or, with an input token, exactly
one ``input-manifest:FETCHES_SHA256:PATH`` token naming the manifest file inside
the verified input snapshot. A ``file:/abs/path`` form exists only behind
parse_argv(allow_file=True) for tests; reviewed contracts refuse it.

A manifest item may instead be served from the verified input snapshot
(``{"kind": "entry"|"file", "source": "input", "path": RELATIVE}``): it is not
fetched, the snapshot digest pins its bytes, and the entry then runs from the
read-only input tree.
"""

import base64
import binascii
import contextlib
import fcntl
import hashlib
import http.client
import inspect
import json
import os
import re
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

WORK_ROOT = "/tessera/work"
MANIFEST_SCHEMA = "tessera.gpu-runtime.manifest.v1"
STATUS_SCHEMA = "tessera.gpu-runtime-status.v1"
MAX_MANIFEST_BYTES = 65536  # inline (argv) manifest
MAX_FILE_MANIFEST_BYTES = 262144  # manifest carried as a file in the input snapshot
MAX_FETCHES = 128  # equals the contract's egress.fetches cap
MAX_TOTAL_FETCH_BYTES = 1 << 40  # sum of per-URL caps; the RemoteResources disk ceiling
MAX_TOTAL_OUTPUT_BYTES = 16 * 1024 * 1024  # guardian custody total per operation
MAX_OUTPUT_FILES = 16
OUTPUT_CHUNK_BYTES = 1 << 20  # must equal the guardian's plan
CHUNK_ATTEMPTS = 3  # per chunk; a chunk is idempotent at the guardian
UPLOAD_RETRY_BUDGET = 8  # extra attempts across the whole upload
OUTPUT_JSON = "output.json"
_OUTPUT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MIN_CLAIM_SECONDS = 60  # never spend the single claim with less than this left
UPLOAD_GRACE_SECONDS = 100  # inside the guardian's 120 s billed upload grace
# Reserved declared output names (ADR for runtime failure custody); equal to the contract's.
CHECKPOINT_NAME = "checkpoint.bin"  # the entry writes it atomically: temp, fsync, rename
DIAGNOSTIC_NAME = "runtime-diagnostic.log"  # header line plus the tail of entry output
DIAGNOSTIC_SCHEMA = "tessera.gpu-runtime-diagnostic.v1"
DIAGNOSTIC_TAIL_BYTES = 64 * 1024
MAX_CHECKPOINT_UPLOADS = 10  # custody's superseding-upload allowance; one is the final upload
CHECKPOINT_UPLOAD_INTERVAL_SECONDS = 120
ENTRY_POLL_SECONDS = 5
ENTRY_TERM_GRACE_SECONDS = 20  # SIGTERM this long before the entry's hard SIGKILL
SOFT_DEADLINE_ENV = "TESSERA_ENTRY_SOFT_DEADLINE_SECONDS"  # seconds from entry start to SIGTERM
HARD_DEADLINE_ENV = "TESSERA_ENTRY_HARD_DEADLINE_SECONDS"  # seconds from entry start to SIGKILL
LINGER_SECONDS = 120  # after the last chunk or a failure report: wait for the guardian's stop
FAILED_PATH = "/v1/workload/failed"
FAILURE_REASONS = (
    "entry", "entry_timeout", "output", "output_plan", "custody", "deadline", "input", "fetch",
    "install", "manifest", "internal",
)  # fmt: skip
MONOTONIC = time.monotonic  # hooks, so a test can run the linger on a fake clock
SLEEP = time.sleep
_STOP = threading.Event()  # set by SIGTERM: the guardian (or Salad) is ending the container
KINDS = ("wheel", "file", "entry")
INPUT_SOURCE = "input"
INPUT_ITEM_KEYS = {"kind", "source", "path"}
INPUT_ITEM_KINDS = ("entry", "file")
# An entry shipped in this image (ADR 0076): ``{"kind":"entry","source":"image","path":NAME,
# "argv":[...]}``. NAME must be one of IMAGE_ENTRIES; the file lives under IMAGE_ENTRY_DIR.
URL_ITEM_KEYS = {"url", "sha256", "max_bytes", "kind", "dest"}
# Provider-side cache (an OPTIONAL persistent directory the provider mounts into the container).
CACHE_DIR_ENV = "TESSERA_FETCH_CACHE_DIR"
CACHE_MAX_BYTES_ENV = "TESSERA_FETCH_CACHE_MAX_BYTES"
_HEX64 = re.compile(r"[a-f0-9]{64}")
IMAGE_SOURCE = "image"
IMAGE_ENTRIES = ("stage_argv.py",)
IMAGE_ENTRY_DIR = "/tessera/lib/entries"
IMAGE_ITEM_KEYS = {"kind", "source", "path", "argv"}
MAX_IMAGE_ARGV = 128
MAX_IMAGE_ARGV_TOKEN = 4000
_KIND_DIR = {"wheel": "wheels", "file": "files", "entry": "entry"}
_DEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){0,7}")
_CHUNK = 1 << 16
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_INPUT_TOKEN = re.compile(r"input:([a-f0-9]{64}):([1-9][0-9]{0,9})")
_INPUT_MANIFEST_TOKEN = re.compile(
    rf"input-manifest:([a-f0-9]{{64}}):({_SEGMENT}(?:/{_SEGMENT}){{0,7}})"
)

# Staged input snapshot (ADR 0036 phase 2, ADR 0037). These mirror
# tessera_worker.chunked_inputs exactly; tests check the two agree case by case.
INPUT_DIR = "input"  # <work>/input, read-only once verified
INPUT_CHUNK_BYTES = 4 * 1024 * 1024
MAX_INPUT_BYTES = 8 * 1024 * 1024 * 1024  # the default operator artifact bound
MAX_INPUT_FILES = 10000
MAX_INPUT_MANIFEST_BYTES = 4 * 1024 * 1024
# Every guardian input reply (the manifest, or one range of a stored chunk) is at most
# INPUT_REPLY_BYTES so it fits the guardian's rate-derived request budget; a stored
# 4 MiB chunk is read in ranges of at most INPUT_RANGE_BYTES and verified whole.
INPUT_RANGE_BYTES = 1 << 20
INPUT_REPLY_BYTES = INPUT_RANGE_BYTES * 4 // 3 + 4096
INPUT_CHUNK_ATTEMPTS = 3  # transport failures only; equals the guardian's per-item bound
INPUT_RETRY_BUDGET = 8  # extra attempts across the whole pull (guardian allows 12)
_HEX64 = re.compile(r"[a-f0-9]{64}")
# Reviewed redirects (equal to salad_workload_contract's rules; tests assert it).
MAX_REDIRECTS = 3
MAX_REDIRECT_HOSTS = 16
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_REDIRECT_TOKEN = re.compile(r"redirect-hosts:([a-f0-9]{64})")
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_REDIRECT_HOST = re.compile(rf"(?=.{{1,253}}$){_DNS_LABEL}(?:\.{_DNS_LABEL})+")
_LOCATION = re.compile(r"[\x21-\x7e]{1,2048}")
_INDEX_KEYS = {"seq", "relative_path", "offset", "size", "sha256"}
_INPUT_RANGE_KEYS = {"schema", "snapshot_id", "seq", "offset", "length", "content_base64"}


# The custody phase a failure happened in; the status line carries only these tokens.
STEPS = frozenset({"claim", "manifest", "chunk", "output_plan", "upload"})


@contextlib.contextmanager
def _step(name):
    """Tag an exception leaving this block with the phase it came from (innermost wins)."""
    try:
        yield
    except BaseException as exc:
        if name in STEPS and getattr(exc, "step", None) is None:
            try:
                exc.step = name
            except Exception:  # pragma: no cover - an exception that refuses attributes
                pass
        raise


class Failure(Exception):
    """Intentionally detail-free; ``code`` is a closed, non-sensitive token."""

    def __init__(self, code="failed"):
        super().__init__(code)
        self.code = code
        self.exit_code = None  # the entry's exit status when the entry itself failed
        self.partial = False  # declared outputs that existed were uploaded, marked partial
        self.outputs = None  # what that partial upload retained
        self.step = None  # the custody phase it failed in (one of STEPS), when it is one
        self.reported = False  # the guardian recorded the failure and will stop the container


def failure_reason(code):
    """Map a detail-free failure code onto the closed set the guardian accepts."""
    if code in FAILURE_REASONS:
        return code
    for prefix in ("input", "manifest", "fetch"):
        if isinstance(code, str) and code.startswith(prefix + "_"):
            return prefix
    if code in {"redirect", "size"}:
        return "fetch"
    if code == "argv":
        return "manifest"
    return "internal"


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


# --------------------------------------------------------------------- argv


def parse_argv(argv, allow_file=False):
    if not 10 <= len(argv) <= 32 or any(
        not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:/=-]{1,256}", v) for v in argv
    ):
        raise Failure("argv")
    if argv[0] != "v1":
        raise Failure("argv")
    operation, epoch, host, watchdog, seconds, out_bytes, out_files, manifest_sha = argv[1:9]
    ref = argv[9:]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", operation):
        raise Failure("argv")
    if not re.fullmatch(r"[1-9][0-9]{0,8}", epoch):
        raise Failure("argv")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", host) or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split(".")
    ):
        raise Failure("argv")
    if not re.fullmatch(r"[a-f0-9]{64}", watchdog) or not re.fullmatch(
        r"[a-f0-9]{64}", manifest_sha
    ):
        raise Failure("argv")
    if any(not re.fullmatch(r"[1-9][0-9]{0,7}", v) for v in (seconds, out_bytes, out_files)):
        raise Failure("argv")
    seconds, out_bytes, out_files = int(seconds), int(out_bytes), int(out_files)
    if not (
        MIN_CLAIM_SECONDS <= seconds <= 3600
        and 1 <= out_bytes <= MAX_TOTAL_OUTPUT_BYTES
        and 1 <= out_files <= MAX_OUTPUT_FILES
    ):
        raise Failure("argv")
    expected_input = None
    if ref and ref[0].startswith("input:"):
        match = _INPUT_TOKEN.fullmatch(ref[0])
        if match is None or not 1 <= int(match.group(2)) <= MAX_INPUT_BYTES:
            raise Failure("argv")
        expected_input = {"digest": match.group(1), "max_bytes": int(match.group(2))}
        ref = ref[1:]
    redirect_hosts_sha256 = None
    if ref and ref[0].startswith("redirect-hosts:"):
        match = _REDIRECT_TOKEN.fullmatch(ref[0])
        if match is None:
            raise Failure("argv")
        redirect_hosts_sha256 = match.group(1)
        ref = ref[1:]
    manifest_in_input = None
    if allow_file and len(ref) == 1 and ref[0].startswith("file:"):  # test-only
        if not re.fullmatch(r"file:/[A-Za-z0-9_./-]{1,200}", ref[0]) or ".." in ref[0]:
            raise Failure("argv")
    elif len(ref) == 1 and ref[0].startswith("input-manifest:"):
        match = _INPUT_MANIFEST_TOKEN.fullmatch(ref[0])
        if expected_input is None or match is None or ".." in match.group(2).split("/"):
            raise Failure("argv")
        manifest_in_input = {"fetches_sha256": match.group(1), "path": match.group(2)}
    elif not ref or any(not re.fullmatch(r"b64:[A-Za-z0-9_-]{1,250}", t) for t in ref):
        raise Failure("argv")
    return {
        "operation": operation,
        "epoch": epoch,
        "destination": host,
        "watchdog": watchdog,
        "max_seconds": seconds,
        "max_output_bytes": out_bytes,
        "max_output_files": out_files,
        "manifest_sha256": manifest_sha,
        "manifest_ref": ref,
        "input": expected_input,
        "manifest_in_input": manifest_in_input,
        "redirect_hosts_sha256": redirect_hosts_sha256,
    }


def binding_of(request):
    """The exact guardian binding (matches the reviewed profile's derived request)."""
    keys = (
        "operation",
        "epoch",
        "destination",
        "watchdog",
        "max_seconds",
        "max_output_bytes",
        "max_output_files",
    )
    return {k: request[k] for k in keys}


# ----------------------------------------------------------------- manifest


def load_manifest(request):
    """Return the verified, validated argv manifest. Digest is checked BEFORE parsing."""
    if request.get("manifest_in_input") is not None:
        raise Failure("manifest")  # read from the verified input after begin-once instead
    ref = request["manifest_ref"]
    try:
        if ref[0].startswith("file:"):
            with open(ref[0][5:], "rb") as handle:
                raw = handle.read(MAX_MANIFEST_BYTES + 1)
        else:
            text = "".join(t[4:] for t in ref)
            raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (OSError, binascii.Error, ValueError):
        raise Failure("manifest") from None
    manifest = parse_manifest(raw, request["manifest_sha256"], MAX_MANIFEST_BYTES)
    bind_redirect_hosts(manifest, request)
    return manifest


def parse_manifest(raw, digest, cap):
    if not raw or len(raw) > cap:
        raise Failure("manifest")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise Failure("manifest_digest")
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise Failure("manifest") from None
    return validate_manifest(manifest)


def redirect_hosts_digest(hosts):
    """Matches salad_workload_contract.redirect_hosts_digest: canonical JSON list."""
    return hashlib.sha256(json.dumps(list(hosts), separators=(",", ":")).encode()).hexdigest()


def bind_redirect_hosts(manifest, request):
    """The manifest's ``redirect_hosts`` must hash to the reviewed argv token.

    No token means the manifest must not name redirect hosts (no redirects).
    """
    hosts = manifest.get("redirect_hosts")
    token = request.get("redirect_hosts_sha256")
    if token is None:
        if hosts is not None:
            raise Failure("manifest")
        return
    if hosts is None or redirect_hosts_digest(hosts) != token:
        raise Failure("manifest_redirects")


def redirect_hosts_of(manifest):
    return frozenset(manifest.get("redirect_hosts", ()))


def _redirect_host_ok(host):
    """Exact DNS name; no label is all digits or starts with 0x (no IP-like form)."""
    return (
        isinstance(host, str)
        and _REDIRECT_HOST.fullmatch(host) is not None
        and not any(label.isdigit() or label.startswith("0x") for label in host.split("."))
    )


def fetches_digest(rows):
    """Matches salad_workload_contract.fetches_digest: sorted (url, sha256, max_bytes)."""
    return hashlib.sha256(encode(sorted([r[0], r[1], r[2]] for r in rows))).hexdigest()


def load_input_manifest(request, input_root):
    """The manifest file inside the verified input snapshot, bound twice by argv.

    Its sha256 is checked BEFORE parsing (exactly as the inline form), and its
    (url, sha256, max_bytes) rows must hash to the reviewed FETCHES_SHA256, which
    contract validation derived from ``egress.fetches``.
    """
    spec = request["manifest_in_input"]
    try:
        with _open_input_file(input_root, spec["path"]) as handle:
            raw = handle.read(MAX_FILE_MANIFEST_BYTES + 1)
    except Failure:
        raise Failure("manifest") from None
    except OSError:
        raise Failure("manifest") from None
    manifest = parse_manifest(raw, request["manifest_sha256"], MAX_FILE_MANIFEST_BYTES)
    rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in url_items(manifest)]
    if fetches_digest(rows) != spec["fetches_sha256"]:
        raise Failure("manifest_fetches")
    bind_redirect_hosts(manifest, request)
    return manifest


def is_image_item(item):
    """Resolved inside this image (``source`` is ``image``), not fetched."""
    return isinstance(item, dict) and item.get("source") == IMAGE_SOURCE


def is_input_item(item):
    """Served from the verified input snapshot (has ``source``), not fetched."""
    return isinstance(item, dict) and "source" in item and not is_image_item(item)


def url_items(manifest):
    return [
        item for item in manifest["fetches"] if not is_input_item(item) and not is_image_item(item)
    ]


def image_items(manifest):
    return [item for item in manifest["fetches"] if is_image_item(item)]


def _validate_image_item(item):
    """Exactly ``{kind: entry, source: image, path, argv}`` with a shipped path."""
    argv = item.get("argv")
    if (
        set(item) != IMAGE_ITEM_KEYS
        or item["kind"] != "entry"
        or item["path"] not in IMAGE_ENTRIES
        or not isinstance(argv, list)
        or not 1 <= len(argv) <= MAX_IMAGE_ARGV
        or not all(
            isinstance(t, str) and 1 <= len(t) <= MAX_IMAGE_ARGV_TOKEN and "\x00" not in t
            for t in argv
        )
    ):
        raise Failure("manifest")


def input_items(manifest):
    return [item for item in manifest["fetches"] if is_input_item(item)]


def _validate_input_item(item):
    """``{kind, source: input, path}`` only: no url/sha256/max_bytes, a bounded relative path."""
    path = item.get("path")
    if (
        set(item) != INPUT_ITEM_KEYS
        or item["source"] != INPUT_SOURCE
        or item["kind"] not in INPUT_ITEM_KINDS
        or not isinstance(path, str)
        or not _DEST.fullmatch(path)
        or ".." in path.split("/")
    ):
        raise Failure("manifest")
    _input_path(path)
    return item["kind"], path


def validate_manifest(manifest):
    if (
        not isinstance(manifest, dict)
        or set(manifest) - {"redirect_hosts"} != {"schema", "fetches"}
        or manifest["schema"] != MANIFEST_SCHEMA
        or not isinstance(manifest["fetches"], list)
        or not 1 <= len(manifest["fetches"]) <= MAX_FETCHES
    ):
        raise Failure("manifest")
    if "redirect_hosts" in manifest:
        hosts = manifest["redirect_hosts"]
        if (
            not isinstance(hosts, list)
            or not 1 <= len(hosts) <= MAX_REDIRECT_HOSTS
            or not all(_redirect_host_ok(h) for h in hosts)
            or hosts != sorted(set(hosts))
        ):
            raise Failure("manifest")
    seen, entries, total = set(), 0, 0
    for item in manifest["fetches"]:
        if is_image_item(item):
            # Beside it only pinned public ``file`` items (ADR 0097): the launcher runs the argv,
            # and nothing else (no wheel, URL entry or input item) is staged.
            _validate_image_item(item)
            entries += 1
            if any(
                not is_image_item(other) and (is_input_item(other) or other.get("kind") != "file")
                for other in manifest["fetches"]
                if isinstance(other, dict)
            ) or not all(isinstance(other, dict) for other in manifest["fetches"]):
                raise Failure("manifest")
            continue
        if is_input_item(item):
            try:
                kind, path = _validate_input_item(item)
            except Failure:
                raise Failure("manifest") from None
            if kind == "entry":
                entries += 1
                if not path.endswith(".py"):
                    raise Failure("manifest")
            if (INPUT_SOURCE, path) in seen:
                raise Failure("manifest")
            seen.add((INPUT_SOURCE, path))
            continue
        if not isinstance(item, dict) or set(item) not in (
            URL_ITEM_KEYS,
            URL_ITEM_KEYS | {"cache"},
        ):
            raise Failure("manifest")
        url, digest, cap, kind, dest = (
            item[k] for k in ("url", "sha256", "max_bytes", "kind", "dest")
        )
        if (
            not isinstance(url, str)
            or not re.fullmatch(
                r"https://[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[\x21-\x7e]{0,500}", url
            )
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or type(cap) is not int
            or not 1 <= cap <= 8589934592
            or kind not in KINDS
            or not isinstance(dest, str)
            or not _DEST.fullmatch(dest)
            or ".." in dest.split("/")
        ):
            raise Failure("manifest")
        if kind == "wheel" and ("/" in dest or not dest.endswith(".whl")):
            raise Failure("manifest")
        if "cache" in item and (
            type(item["cache"]) is not bool or (item["cache"] and kind == "entry")
        ):
            # The provider-side cache holds verified public data and wheels, never an entry.
            raise Failure("manifest")
        if kind == "entry":
            entries += 1
            if not dest.endswith(".py"):
                raise Failure("manifest")
        key = (kind, dest)
        if key in seen:
            raise Failure("manifest")
        seen.add(key)
        total += cap
    if entries != 1 or total > MAX_TOTAL_FETCH_BYTES:
        raise Failure("manifest")
    return manifest


# -------------------------------------------------------------------- fetch


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def open_url(url, timeout):
    """One HTTPS GET: never follows a redirect, no proxy, no credentials. Patched in tests.

    A redirect status is returned (not raised) so open_following can check its
    Location; every other HTTP error raises. Each call builds a fresh request
    with no headers, so nothing (no Authorization, no cookie) crosses a hop.
    """
    if not url.startswith("https://"):
        raise Failure("fetch")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    try:
        return opener.open(urllib.request.Request(url, method="GET"), timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code in REDIRECT_STATUSES:
            return error
        raise


def redirect_target(current, location, allowed):
    """The absolute URL a redirect leads to, or Failure("redirect").

    ``location`` is resolved against ``current`` (so a relative Location gets the
    same check), and must be HTTPS to a host exactly in ``allowed`` on the
    default port, without credentials. The fragment is dropped (never sent).
    """
    if not isinstance(location, str) or not _LOCATION.fullmatch(location):
        raise Failure("redirect")
    try:
        parts = urllib.parse.urlsplit(urllib.parse.urljoin(current, location))
        port = parts.port
    except ValueError:
        raise Failure("redirect") from None
    host = parts.hostname
    if (
        parts.scheme != "https"
        or host not in allowed
        or parts.netloc not in (host, f"{host}:443")
        or port not in (None, 443)
    ):
        raise Failure("redirect")
    return urllib.parse.urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _location(response):
    headers = getattr(response, "headers", None) or {}
    get_all = getattr(headers, "get_all", None)
    values = get_all("Location") if get_all else [headers.get("Location")]
    values = [v for v in values or [] if v is not None]
    return values[0] if len(values) == 1 else None


def open_following(url, deadline, opener, allowed):
    """GET ``url`` through ``opener`` (one hop per call), following reviewed redirects.

    At most MAX_REDIRECTS redirects, each to an HTTPS URL on a host in
    ``allowed`` (empty: none followed). Each hop's socket timeout is at most
    the remaining wall clock divided by the hops that may still follow, and the
    deadline is checked again when a hop returns. Returns the final
    non-redirect response.
    """
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Failure("deadline")
        hops_left = MAX_REDIRECTS + 1 - hop if allowed else 1
        response = opener(current, min(30, remaining / hops_left))
        if time.monotonic() >= deadline:
            response.close()
            raise Failure("deadline")
        if getattr(response, "status", 200) not in REDIRECT_STATUSES:
            return response
        try:
            location = _location(response)
        finally:
            response.close()
        if hop == MAX_REDIRECTS or not allowed:
            raise Failure("redirect")
        current = redirect_target(current, location, allowed)
    raise Failure("redirect")  # unreachable: the last hop returns or raises


def fetch_one(item, dest_path, deadline, opener=None, redirect_hosts=frozenset()):
    """Stream to a .part file with a hard size cap, verify sha256, then rename.

    ``opener`` performs one request per call; redirects are followed only to
    ``redirect_hosts`` (see open_following). The cap and sha256 apply to the
    final response's bytes, wherever they came from.
    """
    opener = opener or open_url
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Failure("deadline")
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    part = dest_path + ".part"
    digest, total = hashlib.sha256(), 0
    started = time.monotonic()
    try:
        response = open_following(item["url"], deadline, opener, redirect_hosts)
        try:
            if getattr(response, "status", 200) != 200:
                raise Failure("fetch")
            declared = response.headers.get("Content-Length") if response.headers else None
            if declared is not None and declared.isdigit() and int(declared) > item["max_bytes"]:
                raise Failure("size")
            with open(part, "wb") as out:
                while True:
                    if time.monotonic() >= deadline:
                        raise Failure("deadline")
                    chunk = response.read(min(_CHUNK, item["max_bytes"] + 1 - total))
                    # The whole body, including its final read, ends within the run
                    # deadline; one blocking read is bounded by the hop's socket
                    # timeout (at most the remaining time) and the process alarm.
                    if time.monotonic() >= deadline:
                        raise Failure("deadline")
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > item["max_bytes"]:
                        raise Failure("size")
                    digest.update(chunk)
                    out.write(chunk)
        finally:
            response.close()
        if digest.hexdigest() != item["sha256"]:
            raise Failure("sha256")
        os.chmod(part, 0o444)
        os.replace(part, dest_path)
    except Failure:
        _unlink(part)
        raise
    except Exception:
        _unlink(part)
        raise Failure("fetch") from None
    seconds = round(time.monotonic() - started, 3)
    return {"url": item["url"], "sha256": item["sha256"], "bytes": total, "seconds": seconds}


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# A part file younger than this is never reclaimed: its writer may not have locked it yet.
STALE_PART_MIN_AGE_SECONDS = 60
_PART_NAME = re.compile(r"\.[a-f0-9]{64}\.part")


class _FetchCache:
    """A digest-keyed cache of verified public bytes on provider-persistent storage.

    The key is the item's pinned sha256 (never a manifest-chosen name). Every reuse streams the
    entry through sha256 while copying it (copies, never links), so nothing unverified is ever
    used: a mismatch or wrong size deletes the entry and the item is fetched normally. The
    directory is outside the work tree, so cached bytes can never become an output or a staged
    input. Storing is best effort and bounded by the quota; a failure never fails the run.
    """

    def __init__(self, directory, quota):
        self.directory, self.quota = directory, quota

    def _entry(self, digest):
        return os.path.join(self.directory, digest)

    def hit(self, item, target, deadline):
        """Copy a verified entry to ``target`` (0444) and return the row, else None.

        An entry is deleted only on a confirmed fingerprint mismatch: it was streamed to its end
        within this pin's size bound and its digest differs. Every other failure (I/O error, a
        size above this pin's bound, a non-regular or hard-linked entry, a size that changed under
        the reader) is a miss for this call and leaves the entry in place.
        """
        entry = self._entry(item["sha256"])
        part = target + ".part"
        started = time.monotonic()
        try:
            info = os.lstat(entry)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or info.st_size > item["max_bytes"]
            ):
                if (
                    stat.S_ISFIFO(info.st_mode)
                    or stat.S_ISCHR(info.st_mode)
                    or (stat.S_ISBLK(info.st_mode) or stat.S_ISSOCK(info.st_mode))
                ):
                    self._discard(entry, info)  # a special file is never a cache entry
                return None
            # O_NONBLOCK: an entry swapped for a FIFO after the lstat must not hang the open;
            # the descriptor is checked below and then switched back to blocking reads.
            fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return None
        digest, total = hashlib.sha256(), 0
        mismatch = False
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            with os.fdopen(fd, "rb") as source, open(part, "wb") as out:
                opened = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_size != info.st_size
                    or (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev)
                ):
                    _unlink(part)
                    return None
                os.set_blocking(source.fileno(), True)
                while True:
                    if time.monotonic() >= deadline:
                        raise Failure("deadline")
                    chunk = source.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > item["max_bytes"]:
                        _unlink(part)
                        return None  # above this pin's bound: not a fingerprint verdict
                    digest.update(chunk)
                    out.write(chunk)
                mismatch = digest.hexdigest() != item["sha256"]
        except Failure:
            _unlink(part)
            raise
        except OSError:
            _unlink(part)
            return None
        if mismatch:
            _unlink(part)
            self._discard(entry, opened)
            return None
        try:
            os.chmod(part, 0o444)
            os.replace(part, target)
        except OSError:
            _unlink(part)
            return None
        try:
            os.utime(entry)  # recency for eviction
        except OSError:
            pass
        seconds = round(time.monotonic() - started, 3)
        return {"url": item["url"], "sha256": item["sha256"], "bytes": total, "seconds": seconds}

    @staticmethod
    def _discard(entry, opened=None):
        """Delete a confirmed-mismatching entry, unless it was replaced since it was opened."""
        if opened is not None:
            try:
                now = os.lstat(entry)
            except OSError:
                return
            if (now.st_ino, now.st_dev) != (opened.st_ino, opened.st_dev):
                return
        _unlink(entry)

    def _judge_part(self, name):
        """Classify one ``.<sha256>.part`` file: reclaim it when stale, else report it.

        Returns None when the file is gone, symlink/dir or was reclaimed (not counted), else the
        size of a live or unjudgeable partial (counted, never evicted). A writer holds an
        exclusive flock on its part file until it renames or removes it; the kernel drops the
        lock when the writer dies, so a lockable old file is an abandoned one.
        """
        path = os.path.join(self.directory, name)
        try:
            info = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None  # never follow or delete anything that is not a regular file
        if (
            info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or time.time() - info.st_mtime < STALE_PART_MIN_AGE_SECONDS
        ):
            return info.st_size
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return info.st_size
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return info.st_size  # a writer holds the lock: live
            opened = os.fstat(fd)
            if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
                return info.st_size
            _unlink(path)  # while holding the lock
            return None
        except OSError:
            return info.st_size
        finally:
            os.close(fd)

    def store(self, item, source_path, size):
        """Best effort: keep a freshly verified file when the quota allows; never raise."""
        try:
            if self.quota <= 0 or size > self.quota:
                return
            own_part = "." + item["sha256"] + ".part"
            entries, live = [], 0
            for name in os.listdir(self.directory):
                if _PART_NAME.fullmatch(name):
                    kept = self._judge_part(name)
                    if name == own_part and kept is not None:
                        return  # another process is storing this digest
                    live += kept or 0
                    continue
                if not _HEX64.fullmatch(name) or name == item["sha256"]:
                    continue
                info = os.lstat(os.path.join(self.directory, name))
                if stat.S_ISREG(info.st_mode):
                    entries.append((info.st_mtime, name, info.st_size))
            entries.sort()
            used = live + sum(e[2] for e in entries)
            while entries and used + size > self.quota:
                _mtime, name, length = entries.pop(0)
                _unlink(os.path.join(self.directory, name))
                used -= length
            if used + size > self.quota:
                return
            temp = os.path.join(self.directory, own_part)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    fd = None
                    _unlink(temp)
                    return
                with open(source_path, "rb") as source, os.fdopen(fd, "wb") as out:
                    fd = None
                    shutil.copyfileobj(source, out, _CHUNK)
                    out.flush()
                    os.fsync(out.fileno())
                    os.replace(temp, self._entry(item["sha256"]))  # lock held until close
            except BaseException:
                if fd is not None:
                    os.close(fd)
                _unlink(temp)
                raise
        except Exception:
            return


def _open_cache(work):
    """The usable cache, or None (``unavailable``): an absolute, real (no symlink in the path)
    directory owned by this uid with mode 0700 that neither contains nor lies inside the work
    tree. The directory comes only from this process's environment."""
    path = os.environ.get(CACHE_DIR_ENV, "")
    try:
        if not path or not os.path.isabs(path) or path != os.path.normpath(path):
            return None
        current = "/"
        for part in path.split("/")[1:]:
            current = os.path.join(current, part)
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                return None
        info = os.lstat(path)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            return None
        real, root = os.path.realpath(path), os.path.realpath(work)
        if real == root or real.startswith(root + os.sep) or root.startswith(real + os.sep):
            return None
        raw = os.environ.get(CACHE_MAX_BYTES_ENV, "0")
        quota = int(raw) if raw.isdigit() and len(raw) <= 15 else 0
    except (OSError, ValueError):
        return None
    return _FetchCache(path, quota)


def fetch_all(manifest, work, deadline, opener=None):
    """Download+verify every URL item. Returns (paths-by-kind, fetched record list).

    Input-sourced items are not fetched; resolve_input_items checks them later. Each row carries
    ``cache``: ``off`` (item not flagged), ``unavailable`` (flagged, no usable cache directory),
    ``hit`` (served from the verified cache) or ``miss`` (fetched, then stored when allowed).
    """
    paths = {k: [] for k in KINDS}
    fetched = []
    hosts = redirect_hosts_of(manifest)  # bound to the argv token when loaded
    cache = False  # not yet looked at
    for item in url_items(manifest):
        target = os.path.join(work, _KIND_DIR[item["kind"]], item["dest"])
        if item.get("cache") is not True:
            row = fetch_one(item, target, deadline, opener, hosts) | {"cache": "off"}
        else:
            if cache is False:
                cache = _open_cache(work)
            row = None if cache is None else cache.hit(item, target, deadline)
            if row is not None:
                row = row | {"cache": "hit"}
            else:
                row = fetch_one(item, target, deadline, opener, hosts)
                if cache is None:
                    row = row | {"cache": "unavailable"}
                else:
                    cache.store(item, target, row["bytes"])
                    row = row | {"cache": "miss"}
        fetched.append(row)
        paths[item["kind"]].append(target)
    return paths, fetched


def install_wheels(wheels, work, deadline, runner=subprocess.run):
    """Offline install of ONLY the verified wheel files into <work>/site."""
    site = os.path.join(work, "site")
    if not wheels:
        return site
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Failure("deadline")
    command = [
        sys.executable,
        "-I",
        "-m",
        "pip",
        "install",
        "--isolated",
        "--no-index",
        "--no-deps",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--no-compile",
        "--target",
        site,
        "--find-links",
        os.path.join(work, _KIND_DIR["wheel"]),
        *wheels,
    ]
    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
            check=False,
            env=_clean_env(work),
        )
    except Exception:
        raise Failure("install") from None
    if result.returncode != 0:
        raise Failure("install")
    return site


def _clean_env(work):
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": work,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
    }


def _entry_env(work, site):
    """Reviewed contract env passes through; interpreter/pip/XDG state does not."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PIP_", "XDG_"))}
    env.update(HOME=work, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=site, PYTHONUNBUFFERED="1")
    return env


def _remove_tree(path):
    """Remove a tree that may hold read-only (0555/0444) input; never follows symlinks."""
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        for root, dirs, _ in os.walk(path, followlinks=False):
            for name in [root, *(os.path.join(root, d) for d in dirs)]:
                try:
                    if not os.path.islink(name):
                        os.chmod(name, 0o700)
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)
    else:
        _unlink(path)


def prepare_work(work):
    """Start from an empty work dir (plus a fresh out dir); nothing survives a restart."""
    os.makedirs(work, exist_ok=True)
    for name in os.listdir(work):
        _remove_tree(os.path.join(work, name))
    out_dir = os.path.join(work, "out")
    os.makedirs(out_dir)
    return out_dir


# -------------------------------------------------------------------- input


def _input_path(value):
    """Mirror of tessera_worker.inputs._path: a bounded relative POSIX file path."""
    try:
        if isinstance(value, str):
            value.encode()  # lone surrogates are malformed, as for the worker
    except UnicodeError:
        raise Failure("input") from None
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 4096
        or len(value.split("/")) > 64
        or "\\" in value
        or "\0" in value
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} or len(part.encode()) > 255 for part in value.split("/"))
    ):
        raise Failure("input")
    return value


def input_chunk_plan(entries):
    """(seq, relative_path, offset, size) per fixed INPUT_CHUNK_BYTES chunk, manifest order."""
    plan = []
    for entry in entries:
        size = entry["size"]
        for offset in range(0, size, INPUT_CHUNK_BYTES):
            plan.append(
                (len(plan), entry["relative_path"], offset, min(INPUT_CHUNK_BYTES, size - offset))
            )
    return plan


def verify_input_manifest(snapshot, digest):
    """Mirror of chunked_inputs._verify_manifest: re-derive the digest, bound every entry."""
    manifest = snapshot["manifest"]
    if not isinstance(manifest, dict):
        raise Failure("input")
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if (
        len(raw) > MAX_INPUT_MANIFEST_BYTES
        or hashlib.sha256(raw).hexdigest() != digest
        or snapshot["digest"] != digest
        or set(manifest) != {"root", "entries", "exclusions", "total_bytes"}
        or not isinstance(manifest["root"], str)
        or not isinstance(manifest["exclusions"], list)
        or len(manifest["exclusions"]) > 10000
        or type(manifest["total_bytes"]) is not int
        or not 0 <= manifest["total_bytes"] <= MAX_INPUT_BYTES
    ):
        raise Failure("input_digest")
    for exclusion in manifest["exclusions"]:
        if (
            not isinstance(exclusion, dict)
            or set(exclusion) != {"relative_path", "reason"}
            or not all(isinstance(v, str) for v in exclusion.values())
        ):
            raise Failure("input")
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_INPUT_FILES:
        raise Failure("input")
    expected, total = {}, 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise Failure("input")
        path = _input_path(entry.get("relative_path"))
        if (
            set(entry) != {"relative_path", "size", "sha256", "provenance", "classification"}
            or path in expected
            or type(entry["size"]) is not int
            or not 0 <= entry["size"] <= MAX_INPUT_BYTES
            or not isinstance(entry["sha256"], str)
            or not _HEX64.fullmatch(entry["sha256"])
            or entry["provenance"] != "imported_unverified"
            or entry["classification"] != "unclassified"
        ):
            raise Failure("input")
        expected[path] = entry
        total += entry["size"]
    if total != manifest["total_bytes"]:
        raise Failure("input")
    aliases = {}
    for path in expected:
        parts = path.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            alias = unicodedata.normalize("NFC", prefix).casefold()
            if alias in aliases and aliases[alias] != prefix:
                raise Failure("input")
            aliases[alias] = prefix
        for length in range(1, len(parts)):
            if "/".join(parts[:length]) in expected:
                raise Failure("input")  # a file path collides with a directory
    return expected


def verify_input_index(snapshot):
    """Mirror of chunked_inputs._verify_index: the index is exactly the manifest's plan."""
    index = snapshot["chunks"]
    plan = input_chunk_plan(snapshot["manifest"]["entries"])
    if not isinstance(index, list) or len(index) != len(plan):
        raise Failure("input")
    for item, (seq, path, offset, size) in zip(index, plan, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != _INDEX_KEYS
            or type(item["seq"]) is not int
            or item["seq"] != seq
            or item["relative_path"] != path
            or type(item["offset"]) is not int
            or item["offset"] != offset
            or type(item["size"]) is not int
            or item["size"] != size
            or not isinstance(item["sha256"], str)
            or not _HEX64.fullmatch(item["sha256"])
        ):
            raise Failure("input")
    return index


def decode_input_range(snapshot, item, offset, length, reply):
    """One range reply: exact fields for the requested (seq, offset, length), canonical base64."""
    if (
        not isinstance(reply, dict)
        or set(reply) != _INPUT_RANGE_KEYS
        or reply["schema"] != "tessera.input-range.v1"
        or reply["snapshot_id"] != snapshot["id"]
        or any(
            type(reply[k]) is not int or reply[k] != v
            for k, v in (("seq", item["seq"]), ("offset", offset), ("length", length))
        )
    ):
        raise Failure("input_chunk")
    encoded = reply["content_base64"]
    if not isinstance(encoded, str) or len(encoded) != ((length + 2) // 3) * 4:
        raise Failure("input_chunk")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise Failure("input_chunk") from None
    if len(content) != length or base64.b64encode(content).decode("ascii") != encoded:
        raise Failure("input_chunk")
    return content


def verify_input_chunk(item, content):
    """The reassembled stored chunk must match its index entry (size and sha256)."""
    if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
        raise Failure("input_chunk")
    return content


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _walk_dirs(root_fd, parts, create):
    """Open (optionally creating 0700) each directory below root_fd without following links."""
    fd = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_input_file(root, relative):
    """Read-only regular file inside the verified input tree (no symlink anywhere)."""
    parts = _input_path(relative).split("/")
    root_fd = os.open(root, _DIR_FLAGS)
    try:
        parent = _walk_dirs(root_fd, parts[:-1], create=False)
    finally:
        os.close(root_fd)
    try:
        fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Failure("input")
    return os.fdopen(fd, "rb")


def resolve_input_items(manifest, input_root):
    """Every input-sourced item must be a regular file of the verified tree.

    Opened without following any link (``_open_input_file``). Returns the
    absolute path of the input-sourced entry, or None when the entry is fetched.
    The tree was written by fetch_input from verified chunks only and is
    read-only; the entry runs from it in place.
    """
    entry = None
    for item in input_items(manifest):
        if input_root is None:
            raise Failure("manifest")
        try:
            with _open_input_file(input_root, item["path"]):
                pass
        except (Failure, OSError):
            raise Failure("input_item") from None
        if item["kind"] == "entry":
            entry = os.path.join(input_root, *item["path"].split("/"))
    return entry


def fetch_input(custody, expected, work, deadline):
    """Pull, verify and materialize the declared input snapshot read-only at <work>/input.

    The expected digest and byte cap come from the reviewed argv, never from the
    guardian. The manifest digest is re-derived and the chunk index must be exactly
    the manifest's plan. Each stored chunk is read in ranges of at most
    INPUT_RANGE_BYTES (exact range fields, canonical base64), reassembled and
    checked against its index size and sha256 before it is written; then every
    file's size and sha256 and the total. Transport failures are retried
    within INPUT_CHUNK_ATTEMPTS per range and INPUT_RETRY_BUDGET overall; a
    verification mismatch is never retried. Any failure removes the partial tree.
    """
    spare = [INPUT_RETRY_BUDGET]

    def call(**body):
        for attempt in range(INPUT_CHUNK_ATTEMPTS):
            if time.monotonic() >= deadline:
                raise Failure("deadline")
            try:
                return custody.input_request(body)
            except Failure:
                if attempt + 1 == INPUT_CHUNK_ATTEMPTS or spare[0] == 0:
                    raise Failure("input_fetch") from None
                spare[0] -= 1
        raise Failure("input_fetch")  # pragma: no cover

    with _step("manifest"):
        reply = call(request="manifest")
        if (
            set(reply) != {"schema", "snapshot_id", "digest", "manifest", "delivery", "chunks"}
            or reply["schema"] != "tessera.input-manifest.v1"
            or reply["delivery"] != "chunked"
            or not isinstance(reply["snapshot_id"], str)
            or not reply["snapshot_id"]
        ):
            raise Failure("input")
        if reply["digest"] != expected["digest"]:
            raise Failure("input_digest")
        snapshot = {k: reply[k] for k in ("digest", "manifest", "delivery", "chunks")}
        snapshot["id"] = reply["snapshot_id"]
        files = verify_input_manifest(snapshot, expected["digest"])
        index = verify_input_index(snapshot)
        if snapshot["manifest"]["total_bytes"] > expected["max_bytes"]:
            raise Failure("input_size")
    root = os.path.join(work, INPUT_DIR)
    os.mkdir(root, 0o700)  # never pre-existing: prepare_work emptied <work>
    directories = {root}
    try:
        root_fd = os.open(root, _DIR_FLAGS)
        try:
            by_path = {}
            for item in index:
                by_path.setdefault(item["relative_path"], []).append(item)
            total = 0
            for path, entry in files.items():
                parts = path.split("/")
                parent = _walk_dirs(root_fd, parts[:-1], create=True)
                for length in range(1, len(parts)):
                    directories.add(os.path.join(root, *parts[:length]))
                try:
                    fd = os.open(
                        parts[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=parent,
                    )
                finally:
                    os.close(parent)
                digest, written = hashlib.sha256(), 0
                with os.fdopen(fd, "wb") as out:
                    for item in by_path.get(path, []):
                        parts = []
                        with _step("chunk"):
                            for offset in range(0, item["size"], INPUT_RANGE_BYTES):
                                length = min(INPUT_RANGE_BYTES, item["size"] - offset)
                                reply = call(
                                    request="chunk", seq=item["seq"], offset=offset, length=length
                                )
                                parts.append(
                                    decode_input_range(snapshot, item, offset, length, reply)
                                )
                            # Nothing is written until the whole stored chunk verifies.
                            content = verify_input_chunk(item, b"".join(parts))
                        written += len(content)
                        total += len(content)
                        if written > entry["size"] or total > expected["max_bytes"]:
                            raise Failure("input_size")
                        digest.update(content)
                        out.write(content)
                    if written != entry["size"] or digest.hexdigest() != entry["sha256"]:
                        raise Failure("input_file")
                    out.flush()
                    os.fchmod(out.fileno(), 0o444)
            if total != snapshot["manifest"]["total_bytes"]:
                raise Failure("input")
        finally:
            os.close(root_fd)
        for path in sorted(directories, key=len, reverse=True):
            os.chmod(path, 0o555)
    except Failure:
        _remove_tree(root)
        raise
    except BaseException:
        _remove_tree(root)
        raise
    return root


# ------------------------------------------------------------------- custody


class HTTPSCustody:
    """Guardian protocol of the reviewed smoke recipe (IMDS JWT, begin-once, output).

    Mirrors workloads/salad_gpu_smoke/smoke.py: no unauthenticated fallback,
    redirects, management key, or env lookup. Binding comes from argv.
    """

    def __init__(self, binding, deadline):
        self.binding = binding
        self.deadline = deadline
        self.context = ssl.create_default_context()
        self.token, self.token_expires = self.identity()

    def identity(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Failure("custody")
        conn = http.client.HTTPConnection("169.254.169.254", 80, timeout=min(5, remaining))
        try:
            conn.request("GET", "/v1/token", headers={"Metadata": "true"})
            response = conn.getresponse()
            if response.status != 200:
                raise Failure("custody")
            raw = response.read(4097)
            if len(raw) > 4096 or time.monotonic() >= self.deadline:
                raise Failure("custody")
            body = json.loads(raw)
            token = body.get("jwt") if isinstance(body, dict) else None
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 1000
                or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token)
            ):
                raise Failure("custody")
            payload = token.split(".")[1]
            claims = json.loads(
                base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
            )
            expiry = claims.get("exp") if isinstance(claims, dict) else None
            # Unverified expiry is ONLY a refresh hint. Server verifies all claims.
            if type(expiry) is not int or expiry <= time.time():
                raise Failure("custody")
            return token, expiry
        except Exception:
            raise Failure("custody") from None
        finally:
            conn.close()

    def post(self, path, body, cap=4096, timeout=10):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Failure("custody")
        conn = http.client.HTTPSConnection(
            self.binding["destination"], 443, timeout=min(timeout, remaining), context=self.context
        )
        try:
            conn.request(
                "POST",
                path,
                body=encode(body),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.token,
                },
            )
            response = conn.getresponse()
            if response.status != 200:
                raise Failure("custody")
            raw = response.read(cap + 1)
            if len(raw) > cap or time.monotonic() >= self.deadline:
                raise Failure("custody")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise Failure("custody")
            return result
        except Exception:
            raise Failure("custody") from None
        finally:
            conn.close()

    def begin_once(self):
        result = self.post("/v1/workload/begin-once", {"binding": self.binding})
        return result == {
            "schema": "tessera.begin-once.v1",
            "granted": True,
            "operation": self.binding["operation"],
            "epoch": self.binding["epoch"],
        }

    def retain(self, output, digest):
        # At most one proactive refresh, solely for upload, never claim/compute retry.
        if time.time() >= self.token_expires - 15:
            self.token, self.token_expires = self.identity()
        result = self.post(
            "/v1/workload/output",
            {"binding": self.binding, "sha256": digest, "output": json.loads(output)},
        )
        return result == {
            "schema": "tessera.retained.v1",
            "sha256": digest,
            "operation": self.binding["operation"],
            "epoch": self.binding["epoch"],
            "retained": True,
        }

    def _refresh(self):
        if time.time() >= self.token_expires - 15:
            self.token, self.token_expires = self.identity()

    def input_request(self, request):
        """One read of the staged input from the guardian (manifest or one chunk).

        Same IMDS identity and binding as begin-once; the ticket stays in the
        guardian. Replies are bounded like tessera_worker.provider_inputs.
        """
        self._refresh()
        return self.post(
            "/v1/workload/input",
            {"binding": self.binding, **request},
            cap=INPUT_REPLY_BYTES,
            timeout=30,
        )

    def report_failed(self, reason, exit_code):
        """Closed failure report: binding, a closed reason token, the child's exit code.

        The guardian records the stop cause ``workload_failed:<reason>`` and stops the
        group at once. Nothing else travels: no message, path, traceback or output."""
        self._refresh()
        result = self.post(
            FAILED_PATH,
            {"binding": self.binding, "reason": reason, "exit_code": exit_code},
        )
        return result == {
            "schema": "tessera.workload-failed.v1",
            "operation": self.binding["operation"],
            "epoch": self.binding["epoch"],
            "recorded": True,
        }

    def output_plan(self):
        """The guardian's declared outputs for this operation (after begin-once)."""
        result = self.post("/v1/workload/outputs", {"binding": self.binding})
        if (
            set(result) != {"schema", "operation", "epoch", "chunk_bytes", "outputs"}
            or result["schema"] != "tessera.output-plan.v1"
            or result["operation"] != self.binding["operation"]
            or result["epoch"] != self.binding["epoch"]
        ):
            raise Failure("custody")
        return result

    def put_chunk(self, name, size, digest, seq, data):
        """One idempotent chunk; returns (file_complete, outputs_complete)."""
        self._refresh()
        result = self.post(
            "/v1/workload/output-chunk",
            {
                "binding": self.binding,
                "name": name,
                "sha256": digest,
                "bytes": size,
                "seq": seq,
                "chunk_sha256": hashlib.sha256(data).hexdigest(),
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        )
        if (
            set(result)
            != {
                "schema",
                "operation",
                "epoch",
                "name",
                "seq",
                "stored",
                "file_complete",
                "outputs_complete",
            }  # fmt: skip
            or result["schema"] != "tessera.output-chunk.v1"
            or result["operation"] != self.binding["operation"]
            or result["epoch"] != self.binding["epoch"]
            or result["name"] != name
            or result["seq"] != seq
            or result["stored"] is not True
            or type(result["file_complete"]) is not bool
            or type(result["outputs_complete"]) is not bool
        ):
            raise Failure("custody")
        return result["file_complete"], result["outputs_complete"]


# ---------------------------------------------------------------- entry run


class EntrySupervisor:
    """Bounded diagnostics and periodic checkpoint custody while the entry runs.

    Keeps only the last ``tail_limit`` bytes of the entry's combined stdout/stderr. The
    optional ``upload`` callable (checkpoint custody) runs from the wait loop, at most
    once per ``interval`` seconds; a failed upload is never fatal to the entry."""

    def __init__(self, upload=None, tail_limit=DIAGNOSTIC_TAIL_BYTES, interval=None):
        self.tail_limit = tail_limit
        self.upload = upload
        self.interval = CHECKPOINT_UPLOAD_INTERVAL_SECONDS if interval is None else interval
        self.clock = MONOTONIC
        self._tail = bytearray()
        self._lock = threading.Lock()
        self._reader = None
        self._last = None
        self.truncated = False
        self.terminated = False  # the soft deadline was reached and SIGTERM was sent

    def attach(self, stream):
        if stream is None:
            return
        self._reader = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._reader.start()

    def _pump(self, stream):
        try:
            while True:
                data = os.read(stream.fileno(), 65536)
                if not data:
                    return
                self.feed(data)
        except (OSError, ValueError):
            return

    def feed(self, data):
        with self._lock:
            self._tail += data
            if len(self._tail) > self.tail_limit:
                self.truncated = True
                del self._tail[: len(self._tail) - self.tail_limit]

    def finish(self):
        if self._reader is not None:
            self._reader.join(timeout=2)

    def tail(self):
        with self._lock:
            return bytes(self._tail), self.truncated

    def tick(self):
        if self.upload is None:
            return
        now = self.clock()
        if self._last is None:
            self._last = now
        if now - self._last < self.interval:
            return
        self._last = now
        try:
            self.upload()
        except Exception:
            pass  # the final upload (or the next interval) retries; never fatal


def run_entry(
    entry, work, site, out_dir, deadline, runner=subprocess.Popen, args=None, supervisor=None
):
    """Run the verified entry: ``python entry WORK_DIR OUT_DIR`` with a hard wall clock.

    An image entry (ADR 0076) also gets ``-- ARGV...``, the manifest's reviewed argv.
    With a supervisor the entry's stdout/stderr tail is captured (stdin stays closed),
    the entry sees its deadlines in ``TESSERA_ENTRY_SOFT_DEADLINE_SECONDS`` and
    ``TESSERA_ENTRY_HARD_DEADLINE_SECONDS`` and gets SIGTERM at the soft one (a chance
    to write a final checkpoint) before SIGKILL at the hard one. A failure carries the
    entry's exit status in ``exit_code``."""
    started = MONOTONIC()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Failure("deadline")
    env = _entry_env(work, site)
    soft = (
        remaining - ENTRY_TERM_GRACE_SECONDS
        if remaining >= 2 * ENTRY_TERM_GRACE_SECONDS
        else remaining / 2
    )
    if supervisor is not None:
        env[SOFT_DEADLINE_ENV] = str(int(soft))
        env[HARD_DEADLINE_ENV] = str(int(remaining))
    try:
        proc = runner(
            [sys.executable, "-B", entry, work, out_dir, *(["--", *args] if args else [])],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if supervisor is None else subprocess.PIPE,
            stderr=subprocess.DEVNULL if supervisor is None else subprocess.STDOUT,
            env=env,
            cwd=work,
            start_new_session=True,
        )
    except Exception:
        raise Failure("entry") from None
    if supervisor is None:
        try:
            code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise Failure("entry_timeout") from None
        except BaseException:
            _kill(proc)
            raise
        if code != 0:
            error = Failure("entry")
            error.exit_code = code
            raise error
        return
    supervisor.attach(getattr(proc, "stdout", None))
    soft_at, hard_at = started + soft, started + remaining
    try:
        while True:
            now = MONOTONIC()
            if now >= hard_at:
                break
            if not supervisor.terminated and now >= soft_at:
                supervisor.terminated = True
                _signal_group(proc, signal.SIGTERM)
            wait = hard_at - now if supervisor.terminated else min(hard_at, soft_at) - now
            try:
                code = proc.wait(timeout=max(0.01, min(ENTRY_POLL_SECONDS, wait)))
            except subprocess.TimeoutExpired:
                supervisor.tick()
                continue
            _signal_group(proc, signal.SIGKILL)  # nothing the entry started outlives it
            supervisor.finish()
            if supervisor.terminated:
                error = Failure("entry_timeout")
                error.exit_code = code
                raise error
            if code != 0:
                error = Failure("entry")
                error.exit_code = code
                raise error
            return
    except Failure:
        raise
    except BaseException:
        _kill(proc)
        supervisor.finish()
        raise
    _kill(proc)
    supervisor.finish()
    error = Failure("entry_timeout")
    error.exit_code = proc.returncode if isinstance(getattr(proc, "returncode", None), int) else -9
    raise error


def _signal_group(proc, signum):
    try:
        os.killpg(proc.pid, signum)
    except (OSError, AttributeError):
        pass


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def validate_plan(plan, request):
    """The guardian's declared outputs must agree with the reviewed argv literals."""
    outputs = plan.get("outputs")
    if (
        plan.get("chunk_bytes") != OUTPUT_CHUNK_BYTES
        or not isinstance(outputs, list)
        or len(outputs) != request["max_output_files"]
    ):
        raise Failure("output_plan")
    names, total = set(), 0
    for item in outputs:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "max_bytes"}
            or not isinstance(item["name"], str)
            or not _OUTPUT_NAME.fullmatch(item["name"])
            or item["name"] in names
            or type(item["max_bytes"]) is not int
            or not 1 <= item["max_bytes"] <= MAX_TOTAL_OUTPUT_BYTES
        ):
            raise Failure("output_plan")
        names.add(item["name"])
        total += item["max_bytes"]
    if total != request["max_output_bytes"]:
        raise Failure("output_plan")
    return [(item["name"], item["max_bytes"]) for item in outputs]


def _open_output(out_dir, name):
    """A declared output must be a regular file directly in out_dir (no symlinks)."""
    try:
        fd = os.open(os.path.join(out_dir, name), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Failure("output") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Failure("output")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def read_output(out_dir, fetched, max_bytes):
    """output.json must be one JSON object; the runtime adds the `fetched` list."""
    try:
        with _open_output(out_dir, OUTPUT_JSON) as handle:
            raw = handle.read(max_bytes + 1)
        value = json.loads(raw)
    except Failure:
        raise
    except (OSError, ValueError):
        raise Failure("output") from None
    if len(raw) > max_bytes or not isinstance(value, dict) or "fetched" in value:
        raise Failure("output")
    value["fetched"] = fetched
    try:
        output = encode(value)
    except (TypeError, ValueError):
        raise Failure("output") from None
    if not output or len(output) > max_bytes:
        raise Failure("output")
    return output


def _stage_file(out_dir, target, name, cap):
    """Copy one declared regular file from out_dir into the staging area, capped."""
    digest, size = hashlib.sha256(), 0
    with open(target, "xb") as out, _open_output(out_dir, name) as source:
        while True:
            chunk = source.read(min(_CHUNK, cap + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > cap:
                raise Failure("output")
            digest.update(chunk)
            out.write(chunk)
    os.chmod(target, 0o400)
    return size, digest.hexdigest()


def _stage_bytes(target, data):
    with open(target, "xb") as out:
        out.write(data)
    os.chmod(target, 0o400)
    return len(data), hashlib.sha256(data).hexdigest()


def diagnostic_bytes(tail, truncated, status, exit_code, cap):
    """One JSON header line plus the tail of the entry's output, at most ``cap`` bytes.

    The tail is cut from the front to fit. No environment, path or credential of the
    runtime is added; the tail is whatever the entry itself printed."""
    header = (
        encode(
            {
                "schema": DIAGNOSTIC_SCHEMA,
                "status": status,
                "exit_code": exit_code,
                "truncated": bool(truncated),
            }
        )
        + b"\n"
    )
    room = max(0, cap - len(header))
    if len(tail) > room:
        header = header.replace(b'"truncated":false', b'"truncated":true')
        room = max(0, cap - len(header))
        tail = tail[len(tail) - room :] if room else b""
    return (header + tail)[:cap]


def stage_outputs(out_dir, stage_dir, plan, fetched, partial=None, diagnostic=None, skipped=None):
    """Snapshot every declared output into stage_dir before any byte is uploaded.

    Complete mode (``partial`` is None): all-or-nothing. A missing, non-regular or
    oversize declared file fails before the first chunk. Partial mode (the entry failed
    or was cut off; ``partial`` = {"reason", "exit_code"}): a declared file that is
    missing, not a regular file or over its cap is skipped and named in ``skipped``;
    every per-file cap still holds. Undeclared files in out_dir are never uploaded.
    output.json, when declared, gets the runtime's ``fetched`` record, and in partial
    mode ``partial: true`` with the reason, exit code and skipped names. The reserved
    diagnostic output, when declared, is built from ``diagnostic`` =
    (tail, truncated, status, exit_code) instead of read from out_dir. Returns
    ``[(name, staged_path, size, sha256)]`` in plan order.
    """
    os.makedirs(stage_dir, mode=0o700)
    staged = {}
    skipped = [] if skipped is None else skipped
    for name, cap in plan:
        if name == OUTPUT_JSON:
            continue
        target = os.path.join(stage_dir, name)
        if name == DIAGNOSTIC_NAME and diagnostic is not None:
            tail, truncated, status, exit_code = diagnostic
            data = diagnostic_bytes(tail, truncated, status, exit_code, cap)
            staged[name] = (name, target, *_stage_bytes(target, data))
            continue
        try:
            staged[name] = (name, target, *_stage_file(out_dir, target, name, cap))
        except Failure:
            if partial is None:
                raise
            if os.path.lexists(target):
                os.unlink(target)
            skipped.append(name)
    for name, cap in plan:
        if name != OUTPUT_JSON:
            continue
        target = os.path.join(stage_dir, name)
        if partial is None:
            data = read_output(out_dir, fetched, cap)
        else:
            try:
                value = json.loads(read_output(out_dir, fetched, cap))
            except Failure:
                value = {"fetched": fetched}
            value.update(
                partial=True,
                partial_reason=partial["reason"],
                partial_exit_code=partial["exit_code"],
                partial_skipped=sorted(skipped),
            )
            data = encode(value)
            if len(data) > cap:
                data = encode({"partial": True})
        staged[name] = (name, target, *_stage_bytes(target, data))
    # The diagnostic goes last: its upload is what completes the declared outputs, so
    # every other file (a final checkpoint included) is in custody before the stop starts.
    ordered = [name for name, _ in plan if name != DIAGNOSTIC_NAME and name in staged]
    ordered += [DIAGNOSTIC_NAME] if DIAGNOSTIC_NAME in staged else []
    return [staged[name] for name in ordered]


def snapshot_checkpoint(out_dir, stage_dir, cap, serial):
    """Copy the latest complete checkpoint aside; None when there is none to upload.

    Only the exact declared name is ever opened (a regular file, no symlink), so a
    temp file the entry is still writing is never read; the entry's atomic rename is
    what publishes a checkpoint. An oversize checkpoint is not uploaded."""
    os.makedirs(stage_dir, mode=0o700, exist_ok=True)
    target = os.path.join(stage_dir, f"{serial}.{CHECKPOINT_NAME}")
    try:
        size, digest = _stage_file(out_dir, target, CHECKPOINT_NAME, cap)
    except (Failure, OSError):
        if os.path.lexists(target):
            os.unlink(target)
        return None
    return CHECKPOINT_NAME, target, size, digest


def _put_file(custody, name, path, size, digest, spare):
    """Upload one staged file in idempotent chunks; returns (spare, complete flag)."""
    chunks = max(1, -(-size // OUTPUT_CHUNK_BYTES))
    file_complete = complete = False
    with open(path, "rb") as handle:
        for seq in range(chunks):
            data = handle.read(OUTPUT_CHUNK_BYTES)
            for attempt in range(CHUNK_ATTEMPTS):
                try:
                    file_complete, complete = custody.put_chunk(name, size, digest, seq, data)
                    break
                except Failure:
                    if attempt + 1 == CHUNK_ATTEMPTS or spare == 0:
                        raise
                    spare -= 1
    if not file_complete:
        raise Failure("custody")
    return spare, complete


def upload_outputs(custody, staged, require_complete=True):
    """Chunked, idempotent upload of every staged output, with bounded retries.

    One chunk is in memory at a time. Each chunk is retried at most
    CHUNK_ATTEMPTS times and UPLOAD_RETRY_BUDGET extra attempts in total; the
    guardian independently bounds chunk calls. Success requires every file to be
    reported complete and, unless ``require_complete`` is False (a partial upload,
    where declared files may be absent), the final chunk to report all declared
    outputs complete.
    """
    spare = UPLOAD_RETRY_BUDGET
    complete = False
    with _step("upload"):
        for name, path, size, digest in staged:
            spare, complete = _put_file(custody, name, path, size, digest, spare)
        if require_complete and not complete:
            raise Failure("custody")
    return [{"name": n, "sha256": d, "bytes": z} for n, _, z, d in staged]


def _accepts(function, name):
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters  # only a runner that names it; a bare **kwargs one is left alone


def _report(custody, exc):
    """Best-effort closed failure report; at most two attempts, never raises."""
    report = getattr(custody, "report_failed", None)
    if report is None or getattr(exc, "report_attempted", False):
        return
    exc.report_attempted = True
    code = exc.code if isinstance(exc, Failure) else "internal"
    exit_code = getattr(exc, "exit_code", None)
    for _ in range(2):
        try:
            if report(failure_reason(code), exit_code) is True:
                exc.reported = True
                return
        except BaseException:
            pass


def execute(
    request,
    deadline,
    work=WORK_ROOT,
    custody_factory=HTTPSCustody,
    opener=None,
    installer=install_wheels,
    entry_runner=run_entry,
):
    from_input = request.get("manifest_in_input") is not None
    expected_input = request.get("input")
    if from_input and expected_input is None:
        raise Failure("argv")
    manifest = None if from_input else load_manifest(request)
    if manifest is not None and input_items(manifest) and expected_input is None:
        raise Failure("manifest")  # an input-sourced item needs a declared input
    work = os.path.realpath(work)
    out_dir = prepare_work(work)
    paths = site = fetched = None
    if manifest is not None:
        # Inline manifest: everything is fetched and verified before the claim.
        paths, fetched = fetch_all(manifest, work, deadline, opener)
        site = installer(paths["wheel"], work, deadline)
    # An uncertain/lost/rejected claim is terminal. Never retry or release a claim.
    if deadline - time.monotonic() < MIN_CLAIM_SECONDS:
        raise Failure("deadline")
    custody = custody_factory(binding_of(request), deadline + UPLOAD_GRACE_SECONDS)
    with _step("claim"):
        if custody.begin_once() is not True:
            raise Failure("custody")
    # The claim is spent: from here a failure is reported to the guardian, which stops
    # the group at once, so the exit is never taken for a provider move.
    try:
        return _run_claimed(
            request, deadline, work, out_dir, custody, opener, installer, entry_runner,
            manifest, expected_input, from_input, paths, site, fetched,
        )  # fmt: skip
    except BaseException as exc:
        _report(custody, exc)
        raise


def _run_claimed(
    request, deadline, work, out_dir, custody, opener, installer, entry_runner,
    manifest, expected_input, from_input, paths, site, fetched,
):  # fmt: skip
    with _step("output_plan"):
        plan = validate_plan(custody.output_plan(), request)
    names = {name for name, _ in plan}
    caps = dict(plan)
    ckpt_dir = os.path.join(work, "ckpt")
    state = {"digest": None, "uploads": 0, "serial": 0}

    def upload_checkpoint():
        """One superseding upload of the latest complete checkpoint (never fatal)."""
        if state["uploads"] >= MAX_CHECKPOINT_UPLOADS - 2:
            return
        state["serial"] += 1
        found = snapshot_checkpoint(out_dir, ckpt_dir, caps[CHECKPOINT_NAME], state["serial"])
        if found is None:
            return
        name, path, size, digest = found
        try:
            if digest != state["digest"]:
                _put_file(custody, name, path, size, digest, 0)
                state["digest"] = digest
                state["uploads"] += 1
        finally:
            os.unlink(path)

    periodic = (
        upload_checkpoint
        if CHECKPOINT_NAME in names and DIAGNOSTIC_NAME in names and len(names) > 1
        else None
    )
    supervisor = EntrySupervisor(upload=periodic)
    input_root = None
    if expected_input is not None:
        # Only a reviewed input declaration fetches; none declared means none fetched.
        input_root = fetch_input(custody, expected_input, work, deadline)
        if from_input:
            manifest = load_input_manifest(request, input_root)
            paths, fetched = fetch_all(manifest, work, deadline, opener)
            site = installer(paths["wheel"], work, deadline)
    image = image_items(manifest)
    extra = {"supervisor": supervisor} if _accepts(entry_runner, "supervisor") else {}
    failure = None
    try:
        if image:
            entry = os.path.join(IMAGE_ENTRY_DIR, image[0]["path"])
            entry_runner(entry, work, site, out_dir, deadline, args=list(image[0]["argv"]), **extra)
        else:
            entry = resolve_input_items(manifest, input_root) or paths["entry"][0]
            entry_runner(entry, work, site, out_dir, deadline, **extra)
    except Failure as exc:
        if exc.code not in {"entry", "entry_timeout"}:
            raise
        failure = exc  # the entry ran and failed: salvage what it declared
    skipped = []
    tail, truncated = supervisor.tail()
    status = "complete" if failure is None else "partial"
    exit_code = None if failure is None else failure.exit_code
    staged = stage_outputs(
        out_dir,
        os.path.join(work, "staged"),
        plan,
        fetched,
        partial=None if failure is None else {"reason": failure.code, "exit_code": exit_code},
        diagnostic=(tail, truncated, status, 0 if failure is None else exit_code),
        skipped=skipped,
    )
    if failure is None:
        return upload_outputs(custody, staged)
    # Upload every file but the last, report, then upload the last. The last file is the
    # one that would complete the declared outputs and start a stop with cause
    # ``output_retained``; reporting first makes the guardian's first stop decision (and
    # so the verdict) ``workload_failed:<reason>``. The guardian still takes the last
    # file while it stops, within the upload grace. A custody error here never replaces
    # the entry's own failure as the reason.
    outputs = []
    try:
        outputs += upload_outputs(custody, staged[:-1], require_complete=False)
    except Failure:
        pass
    _report(custody, failure)
    if failure.reported:
        # Only under a recorded failure cause: without it, the last file would complete
        # the outputs and a lost report would let the failed run verify as a pass.
        try:
            outputs += upload_outputs(custody, staged[-1:], require_complete=False)
        except Failure:
            pass
    failure.partial, failure.outputs = bool(outputs), outputs
    raise failure


def linger(seconds, clock=None, sleep=None, stopped=None):
    """Stay alive until the guardian's stop ends the container, or ``seconds`` pass.

    A one-shot container that exits is replaced by Salad, and the replacement would be
    taken for an unapproved second allocation. Returns True when SIGTERM ended it."""
    clock, sleep = clock or MONOTONIC, sleep or SLEEP
    stopped = _STOP if stopped is None else stopped
    end = clock() + seconds
    while not stopped.is_set():
        left = end - clock()
        if left <= 0:
            return False
        sleep(min(1.0, left))
    return True


def main(argv):
    armed = False
    work = WORK_ROOT
    stay = False
    try:
        request = parse_argv(argv)

        def expired(signum, frame):
            raise Failure("deadline")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, request["max_seconds"] + UPLOAD_GRACE_SECONDS)
        armed = True
        deadline = time.monotonic() + request["max_seconds"]
        outputs = execute(request, deadline, work)
        print(encode({"schema": STATUS_SCHEMA, "status": "retained", "outputs": outputs}).decode())
        code, stay = 0, True
    except BaseException as exc:
        reason = exc.code if isinstance(exc, Failure) else "failed"
        line = {"schema": STATUS_SCHEMA, "status": "failed", "reason": reason}
        step = getattr(exc, "step", None)
        if step in STEPS:
            line["step"] = step
        exit_code = getattr(exc, "exit_code", None)
        if exit_code is not None:
            line["exit_code"] = exit_code
        if getattr(exc, "partial", False):
            line.update(partial=True, outputs=exc.outputs)
        print(encode(line).decode())
        code, stay = 1, bool(getattr(exc, "reported", False))
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
        for name in ("out", "staged", "ckpt"):
            shutil.rmtree(os.path.join(work, name), ignore_errors=True)
        _remove_tree(os.path.join(work, INPUT_DIR))
    if stay:
        # Never exit on our own after the last chunk or a failure report: the guardian
        # stops the group (its stop can take minutes), SIGTERM ends us, or the bound does.
        try:
            signal.signal(signal.SIGTERM, lambda signum, frame: _STOP.set())
        except (ValueError, OSError):
            pass
        linger(LINGER_SECONDS)
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
