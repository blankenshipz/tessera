# ruff: noqa: E501
"""Narrow, pinned HTTP/SSE client for one configured OpenCode server.

This is deliberately not an OpenCode SDK.  It inspects the pinned upstream
document and issues only documented session/context/abort/event calls used by
the Tessera harness boundary.  It has no provider, shell, MCP, cookie, proxy,
or redirect surface.
"""

# fmt: off

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_SSE_BYTES = 64 * 1024
MAX_SSE_EVENTS = 64
MAX_SSE_FIELD_BYTES = 4 * 1024
MAX_TIMEOUT_SECONDS = 30.0
_READ_CHUNK_BYTES = 4 * 1024
_MAX_ENDPOINT_BYTES = 2048
_MAX_HEADER_BYTES = 4096
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class OpenCodeHttpError(ConnectionError):
    """Sanitized endpoint, transport, or pinned-schema failure."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        self.reason = reason
        self.status = status
        detail = reason if status is None else f"{reason} (HTTP {status})"
        super().__init__(f"OpenCode harness endpoint failed: {detail}")


class OpenCodeHttpResponse(Protocol):
    headers: Any

    def getcode(self) -> int: ...

    def read(self, amount: int = -1) -> bytes: ...

    def readline(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


OpenRequest = Callable[[Request, float], OpenCodeHttpResponse]


class _RejectRedirects(HTTPRedirectHandler):
    # ``build_opener`` may retain urllib's default redirect handler for a
    # subclass.  Run before it so no redirect can construct a second request.
    handler_order = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        raise OpenCodeHttpError("redirect denied", code)


def _default_open(request: Request, timeout: float) -> OpenCodeHttpResponse:
    # Disable environment proxies.  urllib has no CookieJar unless one is added;
    # keeping this opener local also prevents cookie state between calls.
    return build_opener(ProxyHandler({}), _RejectRedirects()).open(request, timeout=timeout)


def _close_quietly(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except BaseException:
            pass


def _json_constant(_: str) -> None:
    raise ValueError("non-standard JSON constant")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class OpenCodeServerPin:
    """Release/build and API-document facts for one selected server.

    Release identity and the ``/doc`` API schema identity are deliberately
    separate.  Omitted optional facts mark a legacy synthetic fixture, not a
    v1.18.30 compatibility assertion.
    """

    release_version: str
    schema_sha256: str
    api_schema_version: str | None = None
    release_commit: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.release_version, str) or not _VERSION.fullmatch(self.release_version):
            raise ValueError("OpenCode release_version must be an exact semantic version.")
        if not isinstance(self.schema_sha256, str) or not _SHA256.fullmatch(self.schema_sha256):
            raise ValueError("OpenCode schema_sha256 must be a lowercase SHA-256 digest.")
        if self.api_schema_version is not None and (
            not isinstance(self.api_schema_version, str) or not self.api_schema_version
        ):
            raise ValueError("OpenCode api_schema_version must be a bounded non-empty string.")
        if self.release_commit is not None and (
            not isinstance(self.release_commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}", self.release_commit)
        ):
            raise ValueError("OpenCode release_commit must be a lowercase 40-character commit.")

    @property
    def server_version(self) -> str:
        """Compatibility spelling for old synthetic callers."""
        return self.release_version


@dataclass(frozen=True)
class OpenCodeAdapterProfile:
    """Adapter behavior, separate from upstream and launcher identity."""

    profile_id: str
    event_contract: Literal["legacy_numeric_replay", "opaque_reconnect"]

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not _IDENTIFIER.fullmatch(self.profile_id):
            raise ValueError("OpenCode adapter profile must be a bounded identifier.")


LEGACY_SYNTHETIC_PROFILE = OpenCodeAdapterProfile("legacy-synthetic-v1", "legacy_numeric_replay")
OPENCODE_V1_18_30_CONTEXT_PROFILE = OpenCodeAdapterProfile(
    "opencode-v1.18.30-context-only", "opaque_reconnect"
)


@dataclass(frozen=True)
class OpenCodeHarnessInstancePin:
    """Approved identity and immutable configuration for one harness instance.

    These are trusted launcher/enrollment facts, not claims made by ``/doc``.
    The immutable OpenAPI digest separately binds the selected upstream
    protocol.  Never let an upstream document self-attest a Tessera instance.
    """

    harness_instance_id: str
    server_identity: str
    server_incarnation: str
    build_sha256: str
    config_sha256: str
    workspace_scope: str

    def __post_init__(self) -> None:
        for field in (
            "harness_instance_id",
            "server_identity",
            "server_incarnation",
            "workspace_scope",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{field} must be a bounded identifier.")
        for field in ("build_sha256", "config_sha256"):
            value = getattr(self, field)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"{field} must be a lowercase SHA-256 digest.")


@dataclass(frozen=True)
class OpenCodeSseEvent:
    """One bounded event from the schema-pinned SSE route."""

    upstream_event_id: str | None
    event_name: str
    data: dict[str, object]

    @property
    def cursor(self) -> int:
        """Legacy fixture-only numeric cursor compatibility."""
        if self.upstream_event_id is None or not self.upstream_event_id.isdecimal():
            raise ValueError("OpenCode upstream event ID is opaque, not a replay cursor.")
        return int(self.upstream_event_id)

    def fingerprint(self) -> str:
        return hashlib.sha256(
            _canonical_json({"id": self.upstream_event_id, "event": self.event_name, "data": self.data})
        ).hexdigest()


@dataclass(frozen=True)
class OpenCodeCapabilities:
    """Capabilities explicitly proved by the selected pinned OpenAPI document."""

    session_create: bool
    context_append: bool
    assistant_turn: bool
    abort: bool
    events: bool
    replay: bool


_AuthMode = Literal["basic", "bearer", "none"]


class OpenCodeHttpClient:
    """Authenticated fixed-route OpenCode server client.

    The supplied authorization value authenticates only to the configured
    OpenCode endpoint.  It is never serialized, passed as a message part, or
    exposed through a provider-facing API.
    """

    def __init__(
        self,
        endpoint: str,
        authorization: str,
        pin: OpenCodeServerPin,
        harness_instance: OpenCodeHarnessInstancePin,
        *,
        auth_mode: _AuthMode = "bearer",
        adapter_profile: OpenCodeAdapterProfile = LEGACY_SYNTHETIC_PROFILE,
        timeout_seconds: float = 5.0,
        opener: OpenRequest | None = None,
    ) -> None:
        self.origin = self._origin(endpoint)
        self.auth_mode = self._auth_mode(auth_mode)
        self._authorization = self._authorization_value(authorization, self.auth_mode)
        if not isinstance(pin, OpenCodeServerPin):
            raise ValueError("OpenCode endpoint requires an explicit server/schema pin.")
        self.pin = pin
        if not isinstance(harness_instance, OpenCodeHarnessInstancePin):
            raise ValueError("OpenCode endpoint requires an approved harness-instance pin.")
        self.harness_instance = harness_instance
        if not isinstance(adapter_profile, OpenCodeAdapterProfile):
            raise ValueError("OpenCode endpoint requires an explicit adapter profile.")
        if adapter_profile == OPENCODE_V1_18_30_CONTEXT_PROFILE and (
            pin.release_version != "1.18.30"
            or pin.release_commit != "3104c1428ec91f809e5ab86631300de41eb6952e"
        ):
            raise ValueError("The v1.18.30 adapter profile requires its exact inspected release/build pin.")
        self.adapter_profile = adapter_profile
        if (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("OpenCode timeout must be between zero and 30 seconds.")
        self.timeout_seconds = float(timeout_seconds)
        self._open = opener or _default_open
        self._verified = False
        self._routes: dict[str, str] = {}
        self.capabilities: OpenCodeCapabilities | None = None

    @staticmethod
    def _origin(endpoint: str) -> str:
        if (
            not isinstance(endpoint, str)
            or len(endpoint.encode("utf-8")) > _MAX_ENDPOINT_BYTES
            or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in endpoint)
            or "\\" in endpoint
        ):
            raise ValueError("OpenCode endpoint is invalid.")
        try:
            parsed = urlsplit(endpoint)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise ValueError("OpenCode endpoint is invalid.") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("OpenCode endpoint must be an origin without credentials or a path.")
        if parsed.scheme == "http":
            host = hostname.lower()
            literal_host = f"[{host}]" if host == "::1" else host
            literal_authority = literal_host if port is None else f"{literal_host}:{port}"
            if host not in {"127.0.0.1", "::1"} or parsed.netloc.lower() != literal_authority:
                raise ValueError("OpenCode HTTP endpoints must use a literal loopback address.")
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _auth_mode(value: object) -> _AuthMode:
        if value not in {"basic", "bearer", "none"}:
            raise ValueError("OpenCode auth mode must be basic, bearer, or explicit none.")
        return value

    @staticmethod
    def _authorization_value(value: str, mode: _AuthMode) -> str:
        if mode == "none":
            if value != "":
                raise ValueError("OpenCode unauthenticated mode requires an empty authorization value.")
            return ""
        prefix = "Basic " if mode == "basic" else "Bearer "
        if (
            not isinstance(value, str)
            or not value.startswith(prefix)
            or len(value.encode("utf-8")) > _MAX_HEADER_BYTES
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        ):
            raise ValueError(f"OpenCode endpoint requires a bounded {prefix[:-1]} authorization value.")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("OpenCode authorization must be ASCII.") from error
        if not value[len(prefix) :]:
            raise ValueError(f"OpenCode endpoint requires a bounded {prefix[:-1]} authorization value.")
        return value

    def verify_contract(self) -> None:
        """Verify the exact OpenAPI document before any session mutation."""
        document = self._json("GET", "/doc", None, expect_json=True)
        digest = hashlib.sha256(_canonical_json(document)).hexdigest()
        try:
            version = document["info"]["version"]
        except (KeyError, TypeError) as error:
            raise OpenCodeHttpError("OpenCode schema lacks an exact server version") from error
        expected_schema_version = self.pin.api_schema_version or self.pin.release_version
        if version != expected_schema_version or digest != self.pin.schema_sha256:
            raise OpenCodeHttpError("OpenCode server version or schema pin is incompatible")
        try:
            routes, capabilities = self._document_contract(document)
        except ValueError as error:
            raise OpenCodeHttpError("OpenCode document lacks the selected protocol contract") from error
        self._routes = routes
        self.capabilities = capabilities
        self._verified = True

    def create_session(self, operation_id: str) -> str:
        self._require_verified()
        self._identifier(operation_id, "operation_id")
        response = self._json(
            "POST",
            self._route("session_create"),
            {"title": f"tessera-{operation_id}"},
            expect_json=True,
            operation_id=operation_id,
        )
        try:
            session_id = response["id"]
        except (KeyError, TypeError) as error:
            raise OpenCodeHttpError("OpenCode create response is malformed") from error
        try:
            self._identifier(session_id, "session_id")
        except ValueError as error:
            raise OpenCodeHttpError("OpenCode create response is malformed") from error
        assert isinstance(session_id, str)
        return session_id

    def send_message(self, session_id: str, message: str, operation_id: str) -> None:
        self._require_verified()
        self._identifier(session_id, "session_id")
        self._identifier(operation_id, "operation_id")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("OpenCode message must be non-empty text.")
        self._require_capability("assistant_turn")
        self._json(
            "POST",
            self._session_route("assistant_turn", session_id),
            {"parts": [{"type": "text", "text": message}]},
            expect_json=False,
            operation_id=operation_id,
        )

    def send_context_only_message(self, session_id: str, message: str, delivery_id: str) -> dict[str, object]:
        """Send one human context message and require an upstream readback identity.

        This deliberately has no model, provider, tool, MCP, or permission route.
        A server that cannot return a message identity is uncertain rather than
        treated as an exactly-once delivery.
        """
        self._require_verified()
        self._identifier(session_id, "session_id")
        self._identifier(delivery_id, "delivery_id")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("OpenCode message must be non-empty text.")
        self._require_capability("context_append")
        parts: list[dict[str, str]] = [{"type": "text", "text": message}]
        response = self._json(
            "POST", self._session_route("context_append", session_id), {"noReply": True, "parts": parts},
            expect_json=True, operation_id=delivery_id,
        )
        info, returned_parts = response.get("info"), response.get("parts")
        if not isinstance(info, dict) or not isinstance(returned_parts, list) or len(returned_parts) != 1:
            raise OpenCodeHttpError("OpenCode context readback lacks the exact submitted content")
        message_id, returned = info.get("id"), returned_parts[0]
        if (
            info.get("sessionID") != session_id
            or info.get("role") not in {None, "user"}
            or not isinstance(message_id, str)
            or not isinstance(returned, dict)
            or returned.get("type") != "text"
            or returned.get("text") != message
            or (returned.get("sessionID") is not None and returned.get("sessionID") != session_id)
            or (returned.get("messageID") is not None and returned.get("messageID") != message_id)
        ):
            raise OpenCodeHttpError("OpenCode context readback lacks the designated message identity")
        self._identifier(message_id, "upstream_message_id")
        return response

    def abort(self, session_id: str, operation_id: str) -> None:
        self._require_verified()
        self._identifier(session_id, "session_id")
        self._identifier(operation_id, "operation_id")
        self._require_capability("abort")
        response = self._json_value(
            "POST", self._session_route("abort", session_id), {}, operation_id=operation_id
        )
        if response not in ({}, True):
            raise OpenCodeHttpError("OpenCode abort acknowledgement is not accepted")

    def events(self, after_cursor: int | None = None) -> list[OpenCodeSseEvent]:
        """Read one bounded SSE response, closing it before a reconnect attempt."""
        self._require_verified()
        self._require_capability("events")
        if after_cursor is not None and (type(after_cursor) is not int or after_cursor < 0):
            raise ValueError("OpenCode replay cursor must be a non-negative integer.")
        if after_cursor is not None and self.adapter_profile.event_contract != "legacy_numeric_replay":
            raise OpenCodeHttpError("OpenCode profile does not prove upstream event replay")
        request = self._request(
            "GET",
            self._route("events"),
            None,
            {
                "Accept": "text/event-stream",
                **(
                    {"Last-Event-ID": str(after_cursor)}
                    if after_cursor is not None
                    and self.adapter_profile.event_contract == "legacy_numeric_replay"
                    else {}
                ),
            },
        )
        deadline = time.monotonic() + self.timeout_seconds
        response = self._open_response(request, deadline)
        try:
            status = response.getcode()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise OpenCodeHttpError("OpenCode rejected event stream", status)
            if content_type != "text/event-stream":
                raise OpenCodeHttpError("OpenCode event stream has an invalid content type", status)
            # A response with a declared length is a finite replay response and
            # can be consumed completely.  An ordinary SSE response has no such
            # boundary: return after a complete event rather than waiting for an
            # EOF that a healthy long-lived stream will not send.
            content_length = response.headers.get("Content-Length")
            return self._parse_sse(
                response, deadline=deadline, return_after_event=not bool(content_length)
            )
        except OpenCodeHttpError:
            raise
        except (OSError, ValueError, AttributeError):
            raise OpenCodeHttpError("OpenCode event stream is unavailable") from None
        finally:
            _close_quietly(response)

    def _json(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None,
        *,
        expect_json: bool,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        request = self._request(method, path, body, {}, operation_id)
        deadline = time.monotonic() + self.timeout_seconds
        response = self._open_response(request, deadline)
        try:
            status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise OpenCodeHttpError("OpenCode rejected request", status)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if expect_json and content_type != "application/json":
                raise OpenCodeHttpError("OpenCode response has an invalid content type", status)
            encoded = self._read_bounded_response(response, MAX_RESPONSE_BYTES, deadline)
            if not expect_json:
                if encoded:
                    # Reject malformed non-empty acknowledgements.
                    self._decode_json(encoded, status)
                return {}
            return self._decode_json(encoded, status)
        except OpenCodeHttpError:
            raise
        except (OSError, ValueError, AttributeError):
            raise OpenCodeHttpError("OpenCode transport unavailable") from None
        finally:
            _close_quietly(response)

    def _json_value(
        self, method: str, path: str, body: dict[str, object] | None, *, operation_id: str | None = None
    ) -> object:
        """Read the two real abort acknowledgement forms: empty or JSON true."""
        request = self._request(method, path, body, {}, operation_id)
        deadline = time.monotonic() + self.timeout_seconds
        response = self._open_response(request, deadline)
        try:
            status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise OpenCodeHttpError("OpenCode rejected request", status)
            encoded = self._read_bounded_response(response, MAX_RESPONSE_BYTES, deadline)
            if not encoded:
                return {}
            return json.loads(encoded.decode("utf-8"), parse_constant=_json_constant)
        except OpenCodeHttpError:
            raise
        except (OSError, ValueError, AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            raise OpenCodeHttpError("OpenCode transport unavailable") from None
        finally:
            _close_quietly(response)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None,
        extra_headers: Mapping[str, str],
        operation_id: str | None = None,
    ) -> Request:
        data = None if body is None else _canonical_json(body)
        if data is not None and len(data) > MAX_REQUEST_BYTES:
            raise ValueError("OpenCode request exceeds its bound.")
        headers = {"Accept": "application/json"}
        if self.auth_mode != "none":
            headers["Authorization"] = self._authorization
        if data is not None:
            headers["Content-Type"] = "application/json"
        # ``operation_id`` is local journaling metadata.  Do not invent an
        # upstream header that a selected OpenCode contract did not specify.
        headers.update(extra_headers)
        return Request(f"{self.origin}{path}", data=data, headers=headers, method=method)

    def _open_response(self, request: Request, deadline: float) -> OpenCodeHttpResponse:
        """Open within the request's absolute deadline.

        An injected or platform transport can ignore its supplied socket timeout.
        Keep that blocking call outside the request state and close a late response
        before it can be reused.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenCodeHttpError("OpenCode request deadline exceeded")
        result: list[OpenCodeHttpResponse] = []
        failure: list[BaseException] = []
        expired = threading.Event()

        def open_request() -> None:
            try:
                response = self._open(request, remaining)
                if expired.is_set():
                    _close_quietly(response)
                else:
                    result.append(response)
            except BaseException as error:
                failure.append(error)

        opener = threading.Thread(target=open_request, daemon=True)
        opener.start()
        opener.join(remaining)
        if opener.is_alive() or time.monotonic() > deadline:
            expired.set()
            if result:
                _close_quietly(result[0])
            raise OpenCodeHttpError("OpenCode request deadline exceeded")
        if failure:
            error = failure[0]
            if isinstance(error, OpenCodeHttpError):
                raise error
            if isinstance(error, HTTPError):
                _close_quietly(error)
                raise OpenCodeHttpError("OpenCode rejected request", error.code) from None
            if isinstance(error, OSError | URLError | ValueError):
                raise OpenCodeHttpError("OpenCode transport unavailable") from None
            raise OpenCodeHttpError("OpenCode transport unavailable") from None
        if not result:
            raise OpenCodeHttpError("OpenCode transport unavailable")
        return result[0]

    @staticmethod
    def _decode_json(encoded: bytes, status: int) -> dict[str, object]:
        try:
            decoded = json.loads(encoded.decode("utf-8"), parse_constant=_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise OpenCodeHttpError("OpenCode response is not valid JSON", status) from None
        if not isinstance(decoded, dict):
            raise OpenCodeHttpError("OpenCode response must be a JSON object", status)
        return decoded

    def _parse_sse(
        self, response: OpenCodeHttpResponse, *, deadline: float, return_after_event: bool
    ) -> list[OpenCodeSseEvent]:
        total = 0
        fields: dict[str, list[str]] = {}
        events: list[OpenCodeSseEvent] = []
        while True:
            if time.monotonic() > deadline:
                raise OpenCodeHttpError("OpenCode event stream deadline exceeded")
            line = self._readline_before_deadline(response, deadline)
            if not isinstance(line, bytes):
                raise OpenCodeHttpError("OpenCode event stream is malformed")
            total += len(line)
            if total > MAX_SSE_BYTES:
                raise OpenCodeHttpError("OpenCode event stream exceeds its bound")
            if not line:
                break
            if len(line) > MAX_SSE_FIELD_BYTES:
                raise OpenCodeHttpError("OpenCode event field exceeds its bound")
            if line in {b"\n", b"\r\n"}:
                if fields:
                    events.append(self._sse_event(fields))
                    if len(events) > MAX_SSE_EVENTS:
                        raise OpenCodeHttpError("OpenCode event stream has too many events")
                    fields = {}
                    if return_after_event:
                        return events
                continue
            if line.startswith(b":"):
                continue
            try:
                name, value = line.rstrip(b"\r\n").decode("utf-8").split(":", 1)
            except (UnicodeDecodeError, ValueError):
                raise OpenCodeHttpError("OpenCode event stream is malformed") from None
            if name not in {"id", "event", "data"}:
                raise OpenCodeHttpError("OpenCode event stream has an unsupported field")
            value = value[1:] if value.startswith(" ") else value
            fields.setdefault(name, []).append(value)
            if sum(len(part.encode("utf-8")) for part in fields[name]) > MAX_SSE_FIELD_BYTES:
                raise OpenCodeHttpError("OpenCode event field exceeds its bound")
        if fields:
            events.append(self._sse_event(fields))
        return events

    @staticmethod
    def _read_bounded_response(
        response: OpenCodeHttpResponse, maximum: int, deadline: float
    ) -> bytes:
        """Read a finite JSON response in bounded chunks under one deadline."""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = OpenCodeHttpClient._read_before_deadline(
                response, min(_READ_CHUNK_BYTES, maximum + 1 - total), deadline
            )
            if not isinstance(chunk, bytes):
                raise OpenCodeHttpError("OpenCode response is malformed")
            total += len(chunk)
            if total > maximum:
                raise OpenCodeHttpError("OpenCode response exceeds its bound")
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    @staticmethod
    def _readline_before_deadline(response: OpenCodeHttpResponse, deadline: float) -> bytes:
        """Read one SSE line without allowing slow bytes to extend the deadline.

        Socket read timeouts commonly reset whenever another byte arrives, so a
        peer can otherwise keep ``readline`` alive indefinitely with a slow
        drip.  Keep the blocking read isolated and close the response when the
        whole-stream deadline expires.  The thread only reads the response and
        is bounded by that close; no parsed state can be changed after expiry.
        """
        return OpenCodeHttpClient._read_before_deadline(
            response, MAX_SSE_FIELD_BYTES + 1, deadline, line=True
        )

    @staticmethod
    def _read_before_deadline(
        response: OpenCodeHttpResponse, amount: int, deadline: float, *, line: bool = False
    ) -> bytes:
        deadline_reason = (
            "OpenCode event stream deadline exceeded"
            if line
            else "OpenCode response deadline exceeded"
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenCodeHttpError(deadline_reason)
        result: list[bytes] = []
        failure: list[Exception] = []

        def read() -> None:
            try:
                reader = response.readline if line else response.read
                result.append(reader(amount))
            except Exception as error:  # The caller sanitizes transport failures.
                failure.append(error)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(remaining)
        if reader.is_alive():
            _close_quietly(response)
            raise OpenCodeHttpError(deadline_reason)
        if time.monotonic() > deadline:
            raise OpenCodeHttpError(deadline_reason)
        if failure:
            raise failure[0]
        if not result:
            raise OpenCodeHttpError("OpenCode event stream is malformed")
        return result[0]

    def _sse_event(self, fields: Mapping[str, list[str]]) -> OpenCodeSseEvent:
        try:
            event_name = "".join(fields["event"])
            data = json.loads("\n".join(fields["data"]), parse_constant=_json_constant)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, RecursionError):
            raise OpenCodeHttpError("OpenCode event is malformed") from None
        upstream_event_id = "".join(fields.get("id", [])) or None
        if upstream_event_id is not None and (
            len(upstream_event_id.encode("utf-8")) > 512
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in upstream_event_id)
        ):
            raise OpenCodeHttpError("OpenCode event id is malformed")
        if self.adapter_profile.event_contract == "legacy_numeric_replay" and (
            upstream_event_id is None or not upstream_event_id.isdecimal()
        ):
            raise OpenCodeHttpError("OpenCode legacy event cursor is malformed")
        if (
            not _IDENTIFIER.fullmatch(event_name)
            or not isinstance(data, dict)
        ):
            raise OpenCodeHttpError("OpenCode event is malformed")
        return OpenCodeSseEvent(upstream_event_id, event_name, data)

    def _require_verified(self) -> None:
        if not self._verified:
            raise OpenCodeHttpError("OpenCode server/schema pin has not been verified")

    def _route(self, capability: str) -> str:
        try:
            return self._routes[capability]
        except KeyError as error:
            raise OpenCodeHttpError("OpenCode capability is not available") from error

    def _session_route(self, capability: str, session_id: str) -> str:
        route = self._route(capability)
        return re.sub(r"\{[^{}]+\}", session_id, route)

    def _require_capability(self, capability: str) -> None:
        if not self._verified or self.capabilities is None or not getattr(self.capabilities, capability):
            raise OpenCodeHttpError(f"OpenCode {capability.replace('_', ' ')} capability is unavailable")

    def _document_contract(
        self, document: dict[str, object]
    ) -> tuple[dict[str, str], OpenCodeCapabilities]:
        if not self._auth_is_proved(document):
            raise ValueError("selected authentication mode is not proved by /doc")
        paths = document.get("paths")
        if not isinstance(paths, dict):
            raise ValueError("paths missing")

        def post_path(pattern: str) -> str | None:
            for path, operation in paths.items():
                if isinstance(path, str) and re.fullmatch(pattern, path) and isinstance(operation, dict) and isinstance(operation.get("post"), dict):
                    return path
            return None

        session = post_path(r"/session")
        message = post_path(r"/session/\{[^{}]+\}/message")
        abort = post_path(r"/session/\{[^{}]+\}/abort")
        events = "/event" if isinstance(paths.get("/event"), dict) and isinstance(paths["/event"].get("get"), dict) else None
        if session is None or message is None:
            raise ValueError("session/context route missing")
        message_operation = paths[message]["post"]
        assert isinstance(message_operation, dict)
        context_append = self._schema_has_properties(document, message_operation, {"noReply", "parts"})
        replay = (
            self.adapter_profile.event_contract == "legacy_numeric_replay"
            and events is not None
            and self._operation_has_parameter(paths["/event"]["get"], "Last-Event-ID")
        )
        routes = {"session_create": session, "assistant_turn": message}
        if context_append:
            routes["context_append"] = message
        if abort is not None:
            routes["abort"] = abort
        if events is not None:
            routes["events"] = events
        return routes, OpenCodeCapabilities(
            session_create=True, context_append=context_append, assistant_turn=True,
            abort=abort is not None, events=events is not None, replay=replay,
        )

    def _auth_is_proved(self, document: dict[str, object]) -> bool:
        if self.auth_mode == "none":
            return document.get("security") in (None, [])
        components = document.get("components")
        schemes = components.get("securitySchemes") if isinstance(components, dict) else None
        if not isinstance(schemes, dict):
            return False
        expected = "basic" if self.auth_mode == "basic" else "bearer"
        names = [
            name for name, definition in schemes.items()
            if isinstance(name, str) and isinstance(definition, dict)
            and definition.get("type") == "http" and str(definition.get("scheme", "")).lower() == expected
        ]
        security = document.get("security")
        return isinstance(security, list) and any(
            isinstance(requirement, dict) and any(name in requirement for name in names)
            for requirement in security
        )

    @staticmethod
    def _operation_has_parameter(operation: object, name: str) -> bool:
        return isinstance(operation, dict) and any(
            isinstance(parameter, dict) and parameter.get("name") == name and parameter.get("in") == "header"
            for parameter in operation.get("parameters", []) if isinstance(operation.get("parameters", []), list)
        )

    @staticmethod
    def _schema_has_properties(document: dict[str, object], operation: dict[str, object], required: set[str]) -> bool:
        try:
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
        except (KeyError, TypeError):
            return False
        if not isinstance(schema, dict):
            return False
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            components = document.get("components")
            schemas = components.get("schemas") if isinstance(components, dict) else None
            schema = schemas.get(reference.rsplit("/", 1)[1]) if isinstance(schemas, dict) else None
        return isinstance(schema, dict) and required <= set(schema.get("properties", {}))

    @staticmethod
    def _identifier(value: object, field: str) -> None:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{field} must be a bounded identifier.")
