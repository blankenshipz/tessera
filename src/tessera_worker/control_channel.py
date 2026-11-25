"""Pinned, bounded HTTP transport for a worker's control-plane instance."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError, wait
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
# A routine observation can take longer than a few seconds on a busy control plane (a live run saw
# 7 s), so the default leaves room; an operator can raise it to the bound or lower it to 1 s.
DEFAULT_TIMEOUT_SECONDS = 15.0
MIN_OPERATOR_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 30.0
# A worker lease lasts 150 s (tessera_control.execution.LEASE_SECONDS, asserted by a test) and is
# renewed every 10 s by a single-threaded loop (ADR 0189). One request may take at most the 30 s
# request window ADR 0178 sized its retries for, less a renewal interval, so a single unanswered
# call never holds up the renewal loop for long; the longer lease only adds stall margin.
WORKER_LEASE_SECONDS = 150.0
RENEWAL_INTERVAL_SECONDS = 10.0
REQUEST_WINDOW_SECONDS = 30.0
MAX_OPERATOR_TIMEOUT_SECONDS = REQUEST_WINDOW_SECONDS - RENEWAL_INTERVAL_SECONDS
# After a request times out its thread may still be finishing (the socket timeout restarts on each
# read). The next call waits up to this long for it before refusing, instead of failing at once.
STRAGGLER_WAIT_SECONDS = 5.0


def validate_operator_timeout(
    value: object, renewal_interval_seconds: float = RENEWAL_INTERVAL_SECONDS
) -> float:
    """An operator-set control HTTP timeout in seconds, or ``ValueError``.

    It must fit the renewal timing: at most the 30 s request window minus the renewal interval,
    so a single unanswered request cannot stall the renewal loop (20 s with the default interval).
    """
    ceiling = min(MAX_OPERATOR_TIMEOUT_SECONDS, REQUEST_WINDOW_SECONDS - renewal_interval_seconds)
    if type(value) not in {int, float} or not MIN_OPERATOR_TIMEOUT_SECONDS <= value <= ceiling:
        raise ValueError(
            f"Control timeout must be between 1 and {ceiling:g} seconds "
            "(the 30 s request window minus the renewal interval)."
        )
    return float(value)


_MAX_BASE_URL_BYTES = 2048
_MAX_HEADER_VALUE_BYTES = 4096
_MAX_PATH_BYTES = 4096
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*\Z")

OpenRequest = Callable[[Request, float], Any]


def _invalid_json_constant(_: str) -> None:
    raise ValueError("non-standard JSON constant")


class ControlChannelError(ConnectionError):
    """A sanitized control transport failure, optionally with an HTTP status.

    ``authority_gone`` is True only when control answered 403 with one of its fixed
    detail strings in ``AUTHORITY_GONE_DETAILS``; the body itself is never kept.
    ``refusal_reason`` is control's machine-readable refusal code: set only when an error
    response is a JSON object whose ``reason`` is a short lowercase token
    (``REFUSAL_REASON_TOKEN``), else None. No other body field, and never the detail text, is
    kept. It is a claim by control; callers decide which codes they trust for which status.
    """

    def __init__(
        self,
        reason: str,
        status: int | None = None,
        *,
        authority_gone: bool = False,
        refusal_reason: str | None = None,
    ):
        self.reason = reason
        self.status = status
        self.authority_gone = authority_gone
        self.refusal_reason = (
            refusal_reason
            if isinstance(refusal_reason, str) and REFUSAL_REASON_TOKEN.fullmatch(refusal_reason)
            else None
        )
        detail = reason if status is None else f"{reason} (HTTP {status})"
        super().__init__(f"Control channel failed: {detail}")


_TRANSIENT_CLIENT_STATUSES = frozenset({408, 425, 429})
_AUTH_STATUSES = frozenset({401, 403})
_MAX_ERROR_BODY_BYTES = 2048

# Control's fixed 403 ``detail`` strings (tessera_control/execution.py and
# workers.py) that mean the worker's authority is gone and a new credential cannot
# restore it.  Matched exactly, never by substring.  Every other 403 detail, an
# unreadable body, or a 401 stays in the "auth" class (fail closed, no durable
# record).  Control answers 401 ``Unauthorized`` for every invalid, unknown,
# expired, revoked or wrong-audience worker token.
AUTHORITY_GONE_DETAILS = frozenset(
    {
        "Worker is not assigned to this execution grant.",
        "Worker is retired.",
    }
)


# Control's machine-readable refusal code (``{"detail": ..., "reason": code}``): a short
# lowercase token, never free text.
REFUSAL_REASON_TOKEN = re.compile(r"[a-z][a-z_]{0,40}")


def _error_body(error: HTTPError) -> dict[str, Any] | None:
    """A 4xx error body read once (bounded) as a JSON object, or None.

    Any failure to read or parse it yields None, so the body can only add a fact, never
    change the sanitized error the caller gets.
    """
    if not 400 <= error.code < 500:
        return None
    try:
        body = error.read(_MAX_ERROR_BODY_BYTES + 1)
        if not isinstance(body, bytes) or len(body) > _MAX_ERROR_BODY_BYTES:
            return None
        decoded = json.loads(body.decode("utf-8"), parse_constant=_invalid_json_constant)
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


def _authority_gone(code: int, body: dict[str, Any] | None) -> bool:
    """True when a 403 body is exactly one of control's authority-gone details."""
    detail = body.get("detail") if code == 403 and body is not None else None
    return isinstance(detail, str) and detail in AUTHORITY_GONE_DETAILS


def _refusal_reason(body: dict[str, Any] | None) -> str | None:
    """Control's refusal code when the body's ``reason`` is a short token, else None."""
    reason = body.get("reason") if body is not None else None
    return reason if isinstance(reason, str) and REFUSAL_REASON_TOKEN.fullmatch(reason) else None


def classify_control_failure(error: BaseException) -> str:
    """Classify a control failure as ``"permanent"``, ``"auth"`` or ``"transient"``.

    Auth: HTTP 401, or a 403 whose detail is not a known authority-gone string.
    The worker credential or registration was refused; an operator can fix it, so
    this is never a durable refusal, and retrying cannot help.
    Permanent: an HTTP 4xx other than 401/403/408/425/429, i.e. control understood
    and refused the request, or a 403 with an exact authority-gone detail (grant no
    longer assigned to this worker, worker retired), so replaying cannot succeed.
    Transient: everything else (no status such as connection errors/timeouts,
    5xx, 408/425/429, and any non-error status with a protocol fault).
    """
    status = getattr(error, "status", None) if isinstance(error, ControlChannelError) else None
    if (
        type(status) is int
        and status == 403
        and isinstance(error, ControlChannelError)
        and error.authority_gone
    ):
        return "permanent"
    if type(status) is int and status in _AUTH_STATUSES:
        return "auth"
    if type(status) is int and 400 <= status < 500 and status not in _TRANSIENT_CLIENT_STATUSES:
        return "permanent"
    return "transient"


class _RejectRedirects(HTTPRedirectHandler):
    """Never let urllib create a second request that could carry credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        raise ControlChannelError("redirect denied", code)


def _default_open(request: Request, timeout: float) -> Any:
    # An empty ProxyHandler disables proxy environment variables.  No cookie
    # processor is installed, and urllib's HTTPS handler keeps certificate
    # validation enabled by default.
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    return opener.open(request, timeout=timeout)


def _header_value(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_HEADER_VALUE_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{name} must be a bounded header value.")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be ASCII.") from error
    return value


def _close_quietly(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except BaseException:
            # Close errors must not replace a sanitized protocol result or leak
            # response content, URL userinfo, or worker credentials.
            pass


class HttpControlChannel:
    """One worker credential bound to one validated control-plane origin.

    The caller supplies an idempotency key and decides whether an action is safe
    to retry.  This transport makes exactly one HTTP request for each ``post``.
    """

    # The largest request body this channel sends; the uploader sizes large-artifact parts to it.
    max_request_bytes = MAX_REQUEST_BYTES

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: OpenRequest | None = None,
    ):
        self.origin = self._origin(base_url)
        self.bearer_token = _header_value(bearer_token, "Bearer token")
        if (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("Timeout must be between zero and 30 seconds.")
        self.timeout_seconds = float(timeout_seconds)
        self._open = opener or _default_open
        self._inflight: Future[dict[str, Any]] | None = None
        self._inflight_lock = threading.Lock()

    @staticmethod
    def _origin(base_url: str) -> str:
        if (
            not isinstance(base_url, str)
            or len(base_url.encode("utf-8")) > _MAX_BASE_URL_BYTES
            or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in base_url)
            or "\\" in base_url
        ):
            raise ValueError("Control base URL is invalid.")
        try:
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise ValueError("Control base URL is invalid.") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Control base URL must be an origin without credentials or a path.")
        if parsed.scheme == "http":
            loopback_host = hostname.lower()
            literal_host = f"[{loopback_host}]" if loopback_host == "::1" else loopback_host
            literal_authority = literal_host if port is None else f"{literal_host}:{port}"
            if (
                loopback_host not in {"127.0.0.1", "::1"}
                or parsed.netloc.lower() != literal_authority
            ):
                raise ValueError("HTTP control endpoints must use a literal loopback address.")
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _path(path: str) -> str:
        if (
            not isinstance(path, str)
            or len(path.encode("utf-8")) > _MAX_PATH_BYTES
            or not _SAFE_PATH.fullmatch(path)
            or "%" in path
            or any(segment in {".", ".."} for segment in path.split("/"))
        ):
            raise ValueError("Control request path is not a safe relative path.")
        return path

    @staticmethod
    def _body(body: dict[str, Any]) -> bytes:
        if not isinstance(body, dict):
            raise ValueError("Control request body must be a JSON object.")
        try:
            encoded = json.dumps(
                body, allow_nan=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("Control request body is not JSON serializable.") from error
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("Control request body exceeds its bound.")
        return encoded

    def _perform(self, request: Request, timeout_seconds: float) -> dict[str, Any]:
        try:
            response = self._open(request, timeout_seconds)
        except ControlChannelError:
            raise
        except HTTPError as error:
            body = _error_body(error)
            _close_quietly(error)
            raise ControlChannelError(
                "control rejected request",
                error.code,
                authority_gone=_authority_gone(error.code, body),
                refusal_reason=_refusal_reason(body),
            ) from None
        except (OSError, URLError, ValueError):
            raise ControlChannelError("control transport unavailable") from None
        try:
            status = response.getcode()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ControlChannelError(
                    "control rejected request", status if isinstance(status, int) else None
                )
            if content_type != "application/json":
                raise ControlChannelError("control response has an invalid content type", status)
            encoded = response.read(MAX_RESPONSE_BYTES + 1)
        except ControlChannelError:
            raise
        except (OSError, ValueError, AttributeError):
            raise ControlChannelError("control transport unavailable") from None
        finally:
            _close_quietly(response)
        if not isinstance(encoded, bytes) or len(encoded) > MAX_RESPONSE_BYTES:
            raise ControlChannelError("control response exceeds its bound", status)
        try:
            decoded = json.loads(encoded.decode("utf-8"), parse_constant=_invalid_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise ControlChannelError("control response is not valid JSON", status) from None
        if not isinstance(decoded, dict):
            raise ControlChannelError("control response must be a JSON object", status)
        return decoded

    def _run_request(
        self, future: Future[dict[str, Any]], request: Request, timeout_seconds: float
    ) -> None:
        try:
            future.set_result(self._perform(request, timeout_seconds))
        except BaseException as error:
            # The caller retrieves every worker failure through the Future, so a
            # daemon cannot write an unhandled exception containing transport data.
            future.set_exception(error)

    def _submit(self, request: Request, timeout_seconds: float) -> Future[dict[str, Any]]:
        """Start the request thread and return its future (see ``_submit_within``)."""
        return self._submit_within(request, timeout_seconds)[0]

    def _submit_within(
        self, request: Request, timeout_seconds: float
    ) -> tuple[Future[dict[str, Any]], float]:
        """Start the request thread; return its future and the time left to wait for it.

        A request that timed out may still be winding down; wait for it so the next call (a
        heartbeat, or the retry of a renewal that timed out) is not refused and a healthy run is
        not cancelled for it. The wait is bounded by ``STRAGGLER_WAIT_SECONDS`` and by this
        call's own timeout: it comes out of this call's own deadline, so a call with a short
        deadline (a cancellation path, a renewal retry capped by the lease time left) is never
        held past it, and there is still never more than one request thread (ADR 0178).
        """
        with self._inflight_lock:
            straggler = self._inflight if self._inflight and not self._inflight.done() else None
        waited = 0.0
        if straggler is not None:
            started = time.monotonic()
            wait([straggler], timeout=min(STRAGGLER_WAIT_SECONDS, timeout_seconds))
            waited = time.monotonic() - started
            timeout_seconds = max(0.001, timeout_seconds - waited)
        with self._inflight_lock:
            if self._inflight is not None and not self._inflight.done():
                raise ControlChannelError("control request is still in progress")
            future: Future[dict[str, Any]] = Future()
            self._inflight = future
            thread = threading.Thread(
                target=self._run_request,
                args=(future, request, timeout_seconds),
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError:
                self._inflight = None
                raise ControlChannelError("control transport unavailable") from None
            return future, timeout_seconds

    def get(self, path: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """GET one bounded JSON document without redirects, cookies, or retries."""
        if timeout_seconds is None:
            request_timeout = self.timeout_seconds
        elif (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= self.timeout_seconds
        ):
            raise ValueError("Request timeout must be positive and no longer than channel timeout.")
        else:
            request_timeout = float(timeout_seconds)
        request = Request(
            f"{self.origin}{self._path(path)}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.bearer_token}",
            },
            method="GET",
        )
        future, request_timeout = self._submit_within(request, request_timeout)
        try:
            return future.result(timeout=request_timeout)
        except TimeoutError:
            raise ControlChannelError("control response deadline exceeded") from None
        except ControlChannelError:
            raise
        except Exception:
            raise ControlChannelError("control transport unavailable") from None

    def post(
        self,
        path: str,
        body: dict[str, Any],
        key: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """POST one bounded JSON command without redirects, cookies, or retries."""
        if timeout_seconds is None:
            request_timeout = self.timeout_seconds
        elif (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= self.timeout_seconds
        ):
            raise ValueError("Request timeout must be positive and no longer than channel timeout.")
        else:
            request_timeout = float(timeout_seconds)
        request = Request(
            f"{self.origin}{self._path(path)}",
            data=self._body(body),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.bearer_token}",
                "Content-Type": "application/json",
                "Idempotency-Key": _header_value(key, "Idempotency key"),
            },
            method="POST",
        )
        future, request_timeout = self._submit_within(request, request_timeout)
        try:
            return future.result(timeout=request_timeout)
        except TimeoutError:
            raise ControlChannelError("control response deadline exceeded") from None
        except ControlChannelError:
            raise
        except Exception:
            raise ControlChannelError("control transport unavailable") from None
