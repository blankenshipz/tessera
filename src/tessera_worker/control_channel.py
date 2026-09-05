"""Pinned, bounded HTTP transport for a worker's control-plane instance."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 30.0
_MAX_BASE_URL_BYTES = 2048
_MAX_HEADER_VALUE_BYTES = 4096
_MAX_PATH_BYTES = 4096
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*\Z")

OpenRequest = Callable[[Request, float], Any]


def _invalid_json_constant(_: str) -> None:
    raise ValueError("non-standard JSON constant")


class ControlChannelError(ConnectionError):
    """A sanitized control transport failure, optionally with an HTTP status."""

    def __init__(self, reason: str, status: int | None = None):
        self.reason = reason
        self.status = status
        detail = reason if status is None else f"{reason} (HTTP {status})"
        super().__init__(f"Control channel failed: {detail}")


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

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout_seconds: float = 5.0,
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

    def _perform(self, request: Request) -> dict[str, Any]:
        try:
            response = self._open(request, self.timeout_seconds)
        except ControlChannelError:
            raise
        except HTTPError as error:
            _close_quietly(error)
            raise ControlChannelError("control rejected request", error.code) from None
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

    def _run_request(self, future: Future[dict[str, Any]], request: Request) -> None:
        try:
            future.set_result(self._perform(request))
        except BaseException as error:
            # The caller retrieves every worker failure through the Future, so a
            # daemon cannot write an unhandled exception containing transport data.
            future.set_exception(error)

    def _submit(self, request: Request) -> Future[dict[str, Any]]:
        with self._inflight_lock:
            if self._inflight is not None and not self._inflight.done():
                raise ControlChannelError("control request is still in progress")
            future: Future[dict[str, Any]] = Future()
            self._inflight = future
            thread = threading.Thread(target=self._run_request, args=(future, request), daemon=True)
            try:
                thread.start()
            except RuntimeError:
                self._inflight = None
                raise ControlChannelError("control transport unavailable") from None
            return future

    def post(self, path: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        """POST one bounded JSON command without redirects, cookies, or retries."""
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
        future = self._submit(request)
        try:
            return future.result(timeout=self.timeout_seconds)
        except TimeoutError:
            raise ControlChannelError("control response deadline exceeded") from None
        except ControlChannelError:
            raise
        except Exception:
            raise ControlChannelError("control transport unavailable") from None
