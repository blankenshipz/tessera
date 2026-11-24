"""Verified download of large public files on the executing provider.

Each file is fetched from its pinned https URL with a manual, host-checked redirect chain,
hashed while streaming, and accepted only when its sha256 equals the pin and its size stays
within the pinned cap. A failed file leaves nothing in the destination. Only the returned
receipt rows (source host, digest, bytes, timing) are meant to travel back to the control plane.

Error messages carry a closed code and a host, never a URL query string or credential.

Proxies: environment proxy variables (HTTPS_PROXY, ALL_PROXY, ...) are always ignored. A proxy is
used only when the worker operator configures one explicitly (``proxy``, from the worker's
``--public-fetch-proxy`` / ``public_fetch_proxy`` setting) and its host is on this fetch's egress
allowlist (the pinned hosts plus the redirect hosts); otherwise the fetch is refused before any
request (``proxy_invalid`` for a malformed value, ``proxy_not_allowed`` for a host off the list).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

from tessera_control.public_fetch_models import canonical_public_host

CODES = frozenset(
    {
        "host_not_allowed",
        "scheme_not_allowed",
        "too_many_redirects",
        "bad_status",
        "redirect_without_location",
        "size_exceeded",
        "size_mismatch",
        "digest_mismatch",
        "io_error",
        "proxy_invalid",
        "proxy_not_allowed",
    }
)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REDIRECTS = (301, 302, 303, 307, 308)
_CHUNK = 1 << 20

Opener = Callable[[str], tuple[int, dict[str, str], Iterator[bytes]]]


class PublicFetchError(ValueError):
    """A refused or failed fetch. ``code`` is one of CODES; the message is code and host only."""

    def __init__(self, code: str, host: str | None = None):
        assert code in CODES
        self.code = code
        self.host = host
        super().__init__(f"{code}: {host}" if host else code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # redirects are followed manually, host by host


def parse_proxy(value: Any) -> tuple[str, str]:
    """Validate an operator proxy setting: ``(canonical host, "host:port")``.

    Only ``http://host[:port]`` (an https request tunnels through it with CONNECT, so TLS stays
    end to end): a public fully qualified host under the same rule as the pins, no credentials,
    path, query or fragment. Anything else raises ``proxy_invalid`` without echoing the value.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise PublicFetchError("proxy_invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise PublicFetchError("proxy_invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise PublicFetchError("proxy_invalid")
    canonical = canonical_public_host(host)
    if canonical is None:
        raise PublicFetchError("proxy_invalid")
    return canonical, f"{canonical}:{port or 80}"


class _ExplicitProxy(urllib.request.BaseHandler):
    """Tunnel every https request through one operator proxy (CONNECT). Unlike ProxyHandler it
    reads no environment (not even ``no_proxy``), so the configured proxy is the only route."""

    handler_order = 100  # before the https handler, as ProxyHandler

    def __init__(self, hostport: str):
        self.hostport = hostport

    def https_open(self, request: urllib.request.Request) -> None:
        request.set_proxy(self.hostport, "http")
        return None  # the https handler opens the tunnelled connection


def http_open(url: str, *, proxy: str | None = None) -> tuple[int, dict[str, str], Iterator[bytes]]:
    """One GET without automatic redirects: (status, headers, iterator of chunks).

    Environment proxies are never read (an empty ProxyHandler replaces the default one); ``proxy``
    is the ``host:port`` of the explicit operator proxy, already checked by ``fetch_public_files``.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "tessera-public-fetch/1"})
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({}), _NoRedirect()]
    if proxy is not None:
        handlers.append(_ExplicitProxy(proxy))
    try:
        response = urllib.request.build_opener(*handlers).open(request, timeout=120)
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), iter(())
    return response.status, dict(response.headers), iter(lambda: response.read(_CHUNK), b"")


def _check_url(url: str, allowed: frozenset[str], *, pinned: bool = False) -> str:
    """Scheme, host and port rules for one hop. A pinned URL (the first hop) also may not carry
    a query or fragment; a redirect target may (signed content-network URLs do)."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise PublicFetchError("host_not_allowed") from None
    if parsed.scheme != "https":
        raise PublicFetchError("scheme_not_allowed", host)
    if not host or parsed.username is not None or parsed.password is not None:
        raise PublicFetchError("host_not_allowed", host)
    canonical = canonical_public_host(
        host
    )  # trailing dot, case, IDNA; refuses IP forms, internal names
    if port not in (None, 443) or canonical is None:
        raise PublicFetchError("host_not_allowed", host)
    if canonical not in allowed or (pinned and (parsed.query or parsed.fragment)):
        raise PublicFetchError("host_not_allowed", host)
    return canonical


def _open_chain(url: str, allowed: frozenset[str], opener: Opener, max_hops: int):
    for hop in range(max_hops + 1):
        host = _check_url(url, allowed, pinned=hop == 0)
        status, headers, body = opener(url)
        if status in _REDIRECTS:
            location = {k.lower(): v for k, v in headers.items()}.get("location")
            if not location:
                raise PublicFetchError("redirect_without_location", host)
            url = urllib.parse.urljoin(url, location)
            continue
        if status != 200:
            raise PublicFetchError("bad_status", host)
        return host, headers, body
    raise PublicFetchError("too_many_redirects", urllib.parse.urlsplit(url).hostname)


def _validate_pin(pin: dict[str, Any]) -> None:
    name = pin.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or ".." in name:
        raise PublicFetchError("io_error")
    if not isinstance(pin.get("sha256"), str) or not _SHA_RE.fullmatch(pin["sha256"]):
        raise PublicFetchError("digest_mismatch")
    max_bytes = pin.get("max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise PublicFetchError("size_exceeded")
    if not isinstance(pin.get("url"), str):
        raise PublicFetchError("host_not_allowed")


def _discard_special(entry: Path, opened: os.stat_result) -> None:
    """Remove ``entry`` only while it is still the very object that was opened."""
    try:
        now = entry.lstat()
        if (now.st_ino, now.st_dev) == (opened.st_ino, opened.st_dev):
            entry.unlink(missing_ok=True)
    except OSError:
        pass


def _from_cache(cache: Path, pin: dict[str, Any], final: Path) -> int | None:
    """Copy a verified cache entry to ``final``; return bytes served, or None for a miss.

    The entry is read once: the same bytes are hashed and written to a temp file, which only
    replaces ``final`` after the digest equals the pin. The entry is deleted only when the whole
    file was read and its sha256 differs; an I/O error, a symlinked, hard-linked or directory
    entry, an entry owned by another user or a size above this pin's cap is a miss that leaves the
    entry in place.

    The open never waits: ``O_NONBLOCK`` means a FIFO or device planted at the entry path cannot
    hang the stage. The opened descriptor must be a regular file owned by this user, else it is a
    miss; a FIFO, device or socket (never a file or directory) is also removed, by the identity of
    the object that was opened. ``O_NONBLOCK`` is cleared before any read.
    """
    entry, tmp = cache / pin["sha256"], final.with_name(final.name + ".cachetmp")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(entry, flags)
    except OSError:
        return None
    digest, size, mismatch, opened = hashlib.sha256(), 0, False, None
    try:
        with os.fdopen(fd, "rb") as src:
            opened = os.fstat(src.fileno())
            if not stat.S_ISREG(opened.st_mode):
                if (
                    stat.S_ISFIFO(opened.st_mode)
                    or stat.S_ISCHR(opened.st_mode)
                    or (stat.S_ISBLK(opened.st_mode) or stat.S_ISSOCK(opened.st_mode))
                ):
                    _discard_special(entry, opened)
                return None
            if (
                opened.st_nlink != 1  # a hard-linked entry is not trusted
                or opened.st_uid != os.geteuid()
                or opened.st_size > pin["max_bytes"]
            ):
                return None
            os.set_blocking(src.fileno(), True)
            tmp.unlink(missing_ok=True)
            out_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(out_fd, "wb") as out:
                while chunk := src.read(_CHUNK):
                    size += len(chunk)
                    if size > pin["max_bytes"]:
                        tmp.unlink(missing_ok=True)
                        return None
                    digest.update(chunk)
                    out.write(chunk)
            mismatch = digest.hexdigest() != pin["sha256"]
            if not mismatch:
                os.chmod(tmp, 0o444)
                os.replace(tmp, final)  # a copy, never a link: the mount accepts one link only
                return size
    except OSError:
        tmp.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        return None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.unlink(missing_ok=True)
    if mismatch and opened is not None:  # confirmed fingerprint mismatch: drop the same file
        try:
            now = entry.lstat()
            if (now.st_ino, now.st_dev) == (opened.st_ino, opened.st_dev):
                entry.unlink(missing_ok=True)
        except OSError:
            pass
    return None


def _download(
    pin: dict[str, Any], allowed, opener: Opener, max_hops: int, part: Path
) -> tuple[str, str, int]:
    host, headers, body = _open_chain(pin["url"], allowed, opener, max_hops)
    declared = {k.lower(): v for k, v in headers.items()}.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > pin["max_bytes"]:
        raise PublicFetchError("size_exceeded", host)
    digest, size = hashlib.sha256(), 0
    try:
        with part.open("wb") as out:
            for chunk in body:
                size += len(chunk)
                if size > pin["max_bytes"]:
                    raise PublicFetchError("size_exceeded", host)
                digest.update(chunk)
                out.write(chunk)
    except OSError:
        raise PublicFetchError("io_error", host) from None
    if digest.hexdigest() != pin["sha256"]:
        raise PublicFetchError("digest_mismatch", host)
    return host, digest.hexdigest(), size


def fetch_public_files(
    files: list[dict[str, Any]],
    *,
    dest: str | Path,
    redirect_hosts: Iterable[str] = (),
    cache_dir: str | Path | None = None,
    opener: Opener | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_hops: int = 3,
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch and verify every pinned file into ``dest``; return one receipt row per file.

    ``proxy`` is the operator's explicit proxy (None: direct, environment proxies ignored); it
    is refused unless its host is on the allowlist. ``opener`` replaces the network (tests)."""
    for pin in files:
        _validate_pin(pin)
    named = [urllib.parse.urlsplit(pin["url"]).hostname for pin in files] + list(redirect_hosts)
    allowed = frozenset(h for h in map(canonical_public_host, named) if h is not None)
    proxy_hostport = None
    if proxy is not None:
        proxy_host, proxy_hostport = parse_proxy(proxy)
        if proxy_host not in allowed:
            raise PublicFetchError("proxy_not_allowed", proxy_host)
    if opener is None:
        opener = partial(http_open, proxy=proxy_hostport)
    dest_dir = Path(dest)
    cache = Path(cache_dir) if cache_dir is not None else None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if cache is not None:
            cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise PublicFetchError("io_error") from None
    rows: list[dict[str, Any]] = []
    for pin in files:
        final, part = dest_dir / pin["name"], dest_dir / (pin["name"] + ".part")
        pinned_host = canonical_public_host(urllib.parse.urlsplit(pin["url"]).hostname) or ""
        started = clock()
        _check_url(pin["url"], allowed, pinned=True)  # also before a cache hit: pins are rules too
        cached_size = _from_cache(cache, pin, final) if cache is not None else None
        if cached_size is not None:
            size, cached = cached_size, True
        else:
            cached = False
            try:
                _, _, size = _download(pin, allowed, opener, max_hops, part)
                final.unlink(missing_ok=True)
                os.chmod(part, 0o444)
                os.replace(part, final)
                if cache is not None:  # written only after verification
                    _store_cache(cache, pin["sha256"], final)
            except BaseException:
                part.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
                raise
        rows.append(
            {
                "name": pin["name"],
                "source_host": pinned_host,
                "sha256": pin["sha256"],
                "bytes": size,
                "seconds": round(clock() - started, 3),
                "cached": cached,
            }
        )
    return rows


def _store_cache(cache: Path, digest: str, source: Path) -> None:
    entry, tmp = cache / digest, cache / f".{digest}.tmp"
    try:
        tmp.unlink(missing_ok=True)
        shutil.copyfile(source, tmp)  # a copy, never a link: the mounted file keeps one link
        os.replace(tmp, entry)
    except OSError:
        tmp.unlink(missing_ok=True)  # a cache failure never fails a verified fetch


def load_pins(source: str | Path | dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Read pins (file or dict): {files: {name: {url, sha256, max_bytes}}, redirect_hosts: []}."""
    data = source if isinstance(source, dict) else json.loads(Path(source).read_text())
    named = data.get("files") if isinstance(data, dict) else None
    if not isinstance(named, dict) or not named:
        raise ValueError("pins need a non-empty files mapping")
    files = []
    for name, pin in sorted(named.items()):
        if not isinstance(pin, dict) or {"url", "sha256", "max_bytes"} - set(pin):
            raise ValueError(f"pin {name!r} needs url, sha256 and max_bytes")
        files.append(
            {
                "name": name,
                "url": pin["url"],
                "sha256": pin["sha256"],
                "max_bytes": pin["max_bytes"],
            }
        )
    hosts = data.get("redirect_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ValueError("redirect_hosts must be a list of host names")
    return files, list(hosts)
