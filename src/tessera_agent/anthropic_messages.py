"""Direct Anthropic Messages API reasoning adapter behind an enforced egress allowlist.

The API key is read from ``$CREDENTIALS_DIRECTORY/anthropic-api-key`` at call time, kept only
in a local variable and sent only as the ``x-api-key`` header to the allowlisted model-runtime
host.  Errors carry sanitized codes only; upstream message text, request ids and completion text
never reach exceptions, observations or reprs.  Completion text goes only to an injected
``output_sink(row, text)``.

Tool use (ADR 0083) is deny by default: only tools named by exact profile allow rules are
offered to the model or executed through an injected tool bridge.  Each model turn and each
tool_use block is receipted by digest through injected callbacks with one status model:
an executed tool gets a ``started`` intent row before the bridge call that is completed
exactly once afterwards (``succeeded``, ``error`` or ``uncertain``); a tool that is not
allowed is ``denied``; a fence trip before a call ``refused`` the remaining blocks of the
turn with the fence code; tools requested on the last allowed turn are ``refused`` with
``turn_limit_reached`` and never executed.  Tool inputs and results live only in the
in-memory conversation (and a parked snapshot).

A loop parked by a definite rejection after an accepted turn is handed to an injected
``on_park(row, snapshot)`` callback as a JSON-serialisable snapshot (the conversation, counts,
cumulative usage and the offered tools' digest; never the API key or headers) so a supervisor
can persist it and a new adapter can ``restore()`` it after a restart.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import ssl
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .mcp_tool_bridge import catalog_sha256
from .model_destinations import API_KEY_SHAPE, model_runtime_egress_allowed
from .remote_reasoning_store import private_file

API_URL = "https://api.anthropic.com/v1/messages"
COUNT_URL = "https://api.anthropic.com/v1/messages/count_tokens"
API_VERSION = "2023-06-01"
PROVIDER = "anthropic"
CREDENTIAL_FILE = "anthropic-api-key"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_OUTPUT_TOKENS_PER_CALL = 32768
MAX_COST_CENTS_PER_CALL = 5000
MAX_DAILY_COST_CENTS = 100000
MAX_BACKOFF_SECONDS = 24 * 3600
# count_tokens is free and fast: a short fixed timeout.  The billed /v1/messages call is
# streamed (ADR 0083): the gap between bytes is bounded by an idle timeout and the whole call
# by a total cap that scales with the per-call output cap (see stream_timeout).  A call that
# idles, breaks or overruns ends uncertain with a reserved worst-case estimate on the daily
# ledger.  An injected transport without ``post_stream`` (test fakes) gets the non-streaming
# call, whose single read timeout is messages_timeout.
COUNT_TIMEOUT_SECONDS = 30.0
MESSAGES_TIMEOUT_BASE_SECONDS = 60.0
MESSAGES_TIMEOUT_SECONDS_PER_OUTPUT_TOKEN = 0.04
MESSAGES_TIMEOUT_MAX_SECONDS = 600.0
STREAM_TIMEOUT_BASE_SECONDS = 120.0
STREAM_TIMEOUT_SECONDS_PER_OUTPUT_TOKEN = 0.1
STREAM_TIMEOUT_MAX_SECONDS = 3600.0
# Optional operator override for the billed call (``limits.request_timeout_seconds``).
REQUEST_TIMEOUT_MIN_SECONDS = 30
REQUEST_TIMEOUT_MAX_SECONDS = 3600
# Optional idle bound between stream bytes (``limits.stream_idle_timeout_seconds``).
STREAM_IDLE_TIMEOUT_SECONDS = 60
STREAM_IDLE_TIMEOUT_MIN_SECONDS = 10
STREAM_IDLE_TIMEOUT_MAX_SECONDS = 300
# A streamed body carries per-event framing, so its bound is larger than MAX_RESPONSE_BYTES.
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_STREAM_LINE_BYTES = 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
# Error event types and the HTTP status each stands for, so a stream that is refused before
# message_start maps exactly like the same refusal returned as a status.  Unknown -> 500.
_STREAM_ERROR_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "billing_error": 402,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "overloaded_error": 529,
}
LIMIT_KEYS = (
    "max_output_tokens_per_call",
    "max_input_tokens_per_call",
    "max_cost_cents_per_call",
    "daily_token_cap",
    "daily_cost_cap_cents",
)
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")
MAX_STEPS = 16
MAX_TOOL_CALLS = 64
MAX_TOOL_RESULT_BYTES = 65536
TOOL_LIMIT_KEYS = ("max_tool_calls", "max_tool_result_bytes")
DENY_ALL = {"action": "*", "resource": "*", "effect": "deny"}
TOOL_NOT_PERMITTED = "tool not permitted"
_TOOL_ACTION = re.compile(r"tessera_[A-Za-z0-9_]+\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_\-]{1,128}\Z")
_TOOL_CODE = re.compile(r"tool_[a-z_]{1,48}\Z")
_FENCE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_STOP_REASON = re.compile(r"[a-z_]{1,32}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_KEY = API_KEY_SHAPE
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9.\-]{0,127}\Z")
SNAPSHOT_VERSION = 1
SNAPSHOT_KEYS = frozenset(
    {
        "version",
        "run_id",
        "turn",
        "messages",
        "tools_sha256",
        "tool_calls_used",
        "turns_used",
        "model_id",
        "cumulative",
    }
)
USAGE_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")
TURN_LIMIT_REACHED = "turn_limit_reached"


class EgressRefused(Exception):
    """The transport refused a destination or redirect; ``str`` is a code only."""


class TransportFailed(Exception):
    """A sanitized transport failure (for example the response bound); a code only."""


class _ToolUncertain(Exception):
    """The bridge call failed or answered malformed: the tool may have run."""


class CapRefused(Exception):
    """A preflight cap would be exceeded; nothing was sent to /v1/messages."""

    def __init__(self, code: str = "model_call_cap_exceeded"):
        super().__init__(code)


def messages_timeout(max_output_tokens_per_call: int) -> float:
    """Read timeout for the billed non-streaming call: ``min(600, 60 + 0.04 * max_output)``."""
    return min(
        MESSAGES_TIMEOUT_MAX_SECONDS,
        MESSAGES_TIMEOUT_BASE_SECONDS
        + MESSAGES_TIMEOUT_SECONDS_PER_OUTPUT_TOKEN * max_output_tokens_per_call,
    )


def stream_timeout(max_output_tokens_per_call: int) -> float:
    """Total cap for the streamed billed call: ``min(3600, 120 + 0.1 * max_output)``."""
    return min(
        STREAM_TIMEOUT_MAX_SECONDS,
        STREAM_TIMEOUT_BASE_SECONDS
        + STREAM_TIMEOUT_SECONDS_PER_OUTPUT_TOKEN * max_output_tokens_per_call,
    )


def _blocked(code: str, *, sent: bool | None, retry_after: float | None = None, condition=False):
    """Build the runtime's sanitized exception lazily (the runtime imports this module).

    ``request_sent`` is False when the model request was definitely not accepted (refused
    before sending, or a definite upstream rejection) and None when the outcome is unknown.
    """
    from .remote_reasoning_runtime import ModelConditionBlocked, RuntimeBlocked

    error = ModelConditionBlocked(code, retry_after) if condition else RuntimeBlocked(code)
    error.request_sent = sent
    error.resumable = False
    return error


def _billed(code: str, billed_call: dict):
    """An uncertain outcome for a billed or possibly billed call.

    ``billed_call`` (numbers, codes and a validated model id only) lets the supervisor append
    the call's receipt row and charge the daily ledger before halting: actual usage when the
    response reported it, otherwise the reserved preflight worst case.
    """
    error = _blocked(code, sent=None)
    error.billed_call = billed_call
    return error


class _RefuseRedirects(HTTPRedirectHandler):
    # Run before urllib's default redirect handling so no second request is built.
    handler_order = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise EgressRefused("model_egress_refused")


def build_default_opener():
    """Env proxies ignored, redirects refused, verified TLS with the default context."""
    return build_opener(
        ProxyHandler({}),
        _RefuseRedirects(),
        HTTPSHandler(context=ssl.create_default_context()),
    )


def _stream_invalid() -> TransportFailed:
    return TransportFailed("model_stream_invalid")


def _reassemble_stream(events) -> tuple[int, dict]:
    """Rebuild the non-streaming Messages body from parsed server-sent event payloads.

    Returns ``(200, body)`` at ``message_stop``.  An ``error`` event before ``message_start``
    is a refusal: ``(status, {"type": "error", "error": {"type": kind}})`` with the status that
    error type stands for (upstream message text is dropped).  Anything else -- an error after
    ``message_start``, a malformed or out-of-order event, or the end of the stream without
    ``message_stop`` -- raises TransportFailed: the call may have been billed and its outcome
    is unknown.  Partial usage seen on the way is deliberately discarded with the body.
    """
    message: dict | None = None
    partial_inputs: dict[int, str] = {}
    for event in events:
        kind = event.get("type")
        if kind == "ping":
            continue
        if kind == "error":
            if message is not None:
                raise TransportFailed("model_stream_incomplete")
            error = event.get("error")
            error_type = error.get("type") if isinstance(error, dict) else None
            if error_type not in _STREAM_ERROR_STATUS:
                error_type = "api_error"
            return _STREAM_ERROR_STATUS[error_type], {
                "type": "error",
                "error": {"type": error_type},
            }
        if kind == "message_start":
            start = event.get("message")
            if message is not None or not isinstance(start, dict):
                raise _stream_invalid()
            message = copy.deepcopy(start)
            content = message.get("content")
            message["content"] = content if isinstance(content, list) else []
            continue
        if message is None:
            raise _stream_invalid()
        content = message["content"]
        if kind == "content_block_start":
            index, block = event.get("index"), event.get("content_block")
            if type(index) is not int or index != len(content) or not isinstance(block, dict):
                raise _stream_invalid()
            content.append(copy.deepcopy(block))
            if "input" in block:
                partial_inputs[index] = ""
        elif kind in {"content_block_delta", "content_block_stop"}:
            index = event.get("index")
            if type(index) is not int or not 0 <= index < len(content):
                raise _stream_invalid()
            block = content[index]
            if kind == "content_block_stop":
                if partial_inputs.get(index):
                    try:
                        block["input"] = json.loads(partial_inputs[index])
                    except ValueError:
                        raise _stream_invalid() from None
                partial_inputs.pop(index, None)
                continue
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise _stream_invalid()
            delta_type = delta.get("type")
            field = {
                "text_delta": "text",
                "thinking_delta": "thinking",
                "signature_delta": "signature",
            }.get(delta_type)
            if field is not None:
                piece = delta.get(field)
                if not isinstance(piece, str) or not isinstance(block.get(field, ""), str):
                    raise _stream_invalid()
                block[field] = block.get(field, "") + piece
            elif delta_type == "input_json_delta":
                piece = delta.get("partial_json")
                if not isinstance(piece, str) or index not in partial_inputs:
                    raise _stream_invalid()
                partial_inputs[index] += piece
            elif delta_type == "citations_delta":
                citations = block.setdefault("citations", [])
                if not isinstance(citations, list):
                    raise _stream_invalid()
                citations.append(copy.deepcopy(delta.get("citation")))
            # Unknown delta types are skipped, as the API documents for new event kinds.
        elif kind == "message_delta":
            delta, usage = event.get("delta"), event.get("usage")
            if isinstance(delta, dict):
                for name in ("stop_reason", "stop_sequence"):
                    if name in delta:
                        message[name] = delta[name]
            if isinstance(usage, dict):
                before = message.get("usage")
                message["usage"] = (dict(before) if isinstance(before, dict) else {}) | usage
        elif kind == "message_stop":
            if partial_inputs:
                raise _stream_invalid()
            return 200, message
        # Other event types are skipped.
    raise TransportFailed("model_stream_incomplete")


class AllowlistedTransport:
    """POST JSON only to a model-runtime allowlisted ``https`` host on port 443."""

    def __init__(
        self,
        provider: str,
        *,
        opener=None,
        timeout=COUNT_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.provider = provider
        self.timeout = timeout
        self._opener = opener if opener is not None else build_default_opener()
        self._monotonic = monotonic

    def __repr__(self) -> str:
        return f"AllowlistedTransport(provider={self.provider!r})"

    def _check(self, url: object) -> str:
        if not isinstance(url, str) or not url.isascii() or any(c <= " " for c in url):
            raise EgressRefused("model_egress_refused")
        try:
            parts = urlsplit(url)
            port = parts.port if parts.port is not None else 443
        except ValueError:
            raise EgressRefused("model_egress_refused") from None
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or "@" in parts.netloc
            or parts.fragment
            or not host
            or host.endswith(".")
            or parts.netloc.lower() not in {host, f"{host}:443"}
            or port != 443
            or not model_runtime_egress_allowed(self.provider, host, port)
        ):
            raise EgressRefused("model_egress_refused")
        return url

    def _open(self, url, headers, body, timeout):
        """Check the destination, then send one POST; an HTTP error is returned as a response."""
        url = self._check(url)
        data = json.dumps(body, separators=(",", ":")).encode()
        request = Request(url, data=data, method="POST")
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            return self._opener.open(request, timeout=timeout)
        except HTTPError as error:
            return error

    @staticmethod
    def _head(response) -> tuple[int, dict]:
        status = getattr(response, "status", None)
        if status is None:
            status = getattr(response, "code", None)
        if not isinstance(status, int):
            raise TransportFailed("model_response_invalid")
        if 300 <= status < 400:
            raise EgressRefused("model_egress_refused")
        raw_headers = getattr(response, "headers", None)
        found = {}
        if raw_headers is not None:
            found = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        return status, found

    @staticmethod
    def _json(response) -> dict:
        raw = response.read(MAX_RESPONSE_BYTES + 1) or b""
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportFailed("model_response_bound")
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _close(response) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                pass

    def post_json(self, url, headers, body, *, timeout=None) -> tuple[int, dict, dict]:
        """POST once; ``timeout`` (seconds) overrides the transport default for this request."""
        response = self._open(url, headers, body, self.timeout if timeout is None else timeout)
        try:
            status, found = self._head(response)
            return status, found, self._json(response)
        finally:
            self._close(response)

    def post_stream(
        self, url, headers, body, *, idle_timeout: float, total_timeout: float
    ) -> tuple[int, dict, dict]:
        """POST once with ``"stream": true`` and reassemble the server-sent events.

        Same egress checks as post_json (allowlisted https host:443 checked before connecting,
        no environment proxies, redirects refused).  ``idle_timeout`` is the socket timeout,
        so it bounds the connect, the wait for headers and every gap between bytes;
        ``total_timeout`` is checked between reads, so the call ends within
        ``total_timeout + idle_timeout``.  Returns ``(status, headers, body)`` like post_json:
        a non-200 returns its bounded JSON error body, a 200 returns the message reassembled
        into the non-streaming body shape (or an ``error`` event before ``message_start`` as
        that error's status, see _reassemble_stream).  Raises TransportFailed with a code only
        when the stream breaks, idles, overruns, exceeds MAX_STREAM_BYTES, is malformed or
        ends without ``message_stop``; nothing it read is logged or kept.
        """
        deadline = self._monotonic() + total_timeout
        response = self._open(
            url, headers, dict(body, stream=True), min(idle_timeout, total_timeout)
        )
        try:
            status, found = self._head(response)
            if status != 200:
                return (status, found, self._json(response))
            status, message = _reassemble_stream(self._events(response, deadline))
            return status, found, message
        finally:
            self._close(response)

    def _events(self, response, deadline: float):
        """Yield each SSE event's JSON payload; codes-only TransportFailed on any problem."""
        read = getattr(response, "read1", None) or response.read
        pending, data, total = b"", [], 0
        while True:
            if self._monotonic() > deadline:
                raise TransportFailed("model_stream_timeout")
            try:
                chunk = read(STREAM_CHUNK_BYTES)
            except TimeoutError:
                raise TransportFailed("model_stream_idle") from None
            except Exception:  # noqa: BLE001 - sanitized; never echo upstream detail
                raise TransportFailed("model_stream_interrupted") from None
            if not chunk:
                return  # EOF; an unterminated event is discarded, as SSE specifies.
            total += len(chunk)
            if total > MAX_STREAM_BYTES:
                raise TransportFailed("model_response_bound")
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            if len(pending) > MAX_STREAM_LINE_BYTES:
                raise TransportFailed("model_response_bound")
            for line in lines:
                line = line.removesuffix(b"\r")
                if not line:
                    if data:
                        try:
                            payload = json.loads(b"\n".join(data))
                        except (ValueError, UnicodeDecodeError):
                            raise _stream_invalid() from None
                        if not isinstance(payload, dict):
                            raise _stream_invalid()
                        data = []
                        yield payload
                elif line.startswith(b"data:"):
                    data.append(line[5:].removeprefix(b" "))
                # ``event:``, ``id:``, ``retry:`` and ``:`` comments are not needed: every
                # Anthropic payload names its own ``type``.


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _price_table(prices) -> dict:
    if not isinstance(prices, dict) or set(prices) != set(PRICE_KEYS):
        raise ValueError("anthropic_pricing_invalid")
    for key in PRICE_KEYS:
        value = prices[key]
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError("anthropic_pricing_invalid")
    return prices


def validate_anthropic_profile(profile: dict) -> None:
    """Static config checks only; messages are codes, never values."""
    model = profile.get("model")
    if (
        not isinstance(model, dict)
        or model.get("providerID") != PROVIDER
        or not isinstance(model.get("id"), str)
        or not _MODEL_ID.match(model["id"])
    ):
        raise ValueError("anthropic_model_invalid")
    limits = profile.get("limits")
    if not isinstance(limits, dict) or not all(_positive_int(limits.get(k)) for k in LIMIT_KEYS):
        raise ValueError("anthropic_limits_invalid")
    if (
        limits["max_output_tokens_per_call"] > MAX_OUTPUT_TOKENS_PER_CALL
        or limits["max_cost_cents_per_call"] > MAX_COST_CENTS_PER_CALL
        or limits["daily_cost_cap_cents"] > MAX_DAILY_COST_CENTS
    ):
        raise ValueError("anthropic_limits_invalid")
    if "request_timeout_seconds" in limits:
        timeout = limits["request_timeout_seconds"]
        if (
            type(timeout) is not int
            or not REQUEST_TIMEOUT_MIN_SECONDS <= timeout <= REQUEST_TIMEOUT_MAX_SECONDS
        ):
            raise ValueError("anthropic_limits_invalid")
    if "control_ca_file" in profile:
        # Optional PEM bundle for the control URL; passed to the MCP server child only
        # as TESSERA_CONTROL_CA_FILE. Absolute, normalised, no control characters.
        ca = profile["control_ca_file"]
        if (
            not isinstance(ca, str)
            or not ca.startswith("/")
            or ca == "/"
            or len(ca) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in ca)
            or ".." in ca.split("/")
            or "//" in ca
        ):
            raise ValueError("anthropic_control_ca_file_invalid")
    if "stream_idle_timeout_seconds" in limits:
        idle = limits["stream_idle_timeout_seconds"]
        if (
            type(idle) is not int
            or not STREAM_IDLE_TIMEOUT_MIN_SECONDS <= idle <= STREAM_IDLE_TIMEOUT_MAX_SECONDS
        ):
            raise ValueError("anthropic_limits_invalid")
    prices = _price_table(profile.get("pricing_cents_per_mtok"))
    if "substitution_pricing_cents_per_mtok" in profile:
        substitution = _price_table(profile["substitution_pricing_cents_per_mtok"])
        if any(substitution[key] < prices[key] for key in PRICE_KEYS):
            raise ValueError("anthropic_pricing_invalid")
    allowed = allowed_tools(profile)
    steps = profile.get("max_steps")
    if (steps is not None or allowed) and not (_positive_int(steps) and steps <= MAX_STEPS):
        raise ValueError("anthropic_limits_invalid")
    if not allowed:
        # No tools: no catalog pin and no tool limits may be configured.
        if "tool_catalog_sha256" in profile:
            raise ValueError("anthropic_tool_catalog_invalid")
        if any(key in limits for key in TOOL_LIMIT_KEYS):
            raise ValueError("anthropic_limits_invalid")
        return
    pin = profile.get("tool_catalog_sha256")
    if not isinstance(pin, str) or not _SHA256.match(pin):
        raise ValueError("anthropic_tool_catalog_invalid")
    if not all(_positive_int(limits.get(key)) for key in TOOL_LIMIT_KEYS) or (
        limits["max_tool_calls"] > MAX_TOOL_CALLS
        or limits["max_tool_result_bytes"] > MAX_TOOL_RESULT_BYTES
    ):
        raise ValueError("anthropic_limits_invalid")


def allowed_tools(profile: dict) -> frozenset[str]:
    """Exact Tessera MCP tool names the profile allows; deny-all first, nothing else."""
    rules = profile.get("permissions")
    if not isinstance(rules, list) or not rules or rules[0] != DENY_ALL:
        raise ValueError("anthropic_permissions_invalid")
    names = []
    for rule in rules[1:]:
        if (
            not isinstance(rule, dict)
            or set(rule) != {"action", "resource", "effect"}
            or rule["effect"] != "allow"
            or rule["resource"] != "*"
            or not isinstance(rule["action"], str)
            or not _TOOL_ACTION.match(rule["action"])
            or rule["action"] in names
        ):
            raise ValueError("anthropic_permissions_invalid")
        names.append(rule["action"])
    return frozenset(names)


def _digest_json(value) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode()
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


class AnthropicMessagesReasoning:
    """A bounded synchronous Messages tool loop per prompt; status() reports totals in memory."""

    def __init__(
        self,
        profile: dict,
        *,
        credentials_dir: Path,
        transport=None,
        clock=time.time,
        budget_check: Callable[[int, float], object] | None = None,
        output_sink: Callable[[dict, str], object] | None = None,
        tool_bridge=None,
        on_model_call: Callable[[dict, dict], object] | None = None,
        on_tool_call: Callable[[dict, dict], object] | None = None,
        between_turns: Callable[[dict], object] | None = None,
        on_park: Callable[[dict, dict], object] | None = None,
        on_unpark: Callable[[dict], object] | None = None,
        on_tool_intent: Callable[[dict, dict], object] | None = None,
        on_tool_result: Callable[[object, dict], object] | None = None,
        before_tool: Callable[[dict], object] | None = None,
        on_model_reserve: Callable[[dict, dict], object] | None = None,
        on_model_release: Callable[[dict], object] | None = None,
    ):
        validate_anthropic_profile(profile)
        self.allowed = allowed_tools(profile)
        self.max_steps = profile.get("max_steps", 1)
        self.profile = profile
        self.model_id = profile["model"]["id"]
        self.limits = dict(profile["limits"])
        self.prices = dict(profile["pricing_cents_per_mtok"])
        substitution = profile.get("substitution_pricing_cents_per_mtok")
        self.substitution_prices = dict(substitution) if substitution is not None else None
        self.credentials_dir = Path(credentials_dir)
        self.transport = transport if transport is not None else AllowlistedTransport(PROVIDER)
        self.clock = clock
        self.budget_check = budget_check
        self.output_sink = output_sink
        self.tool_bridge = tool_bridge
        self.on_model_call = on_model_call
        self.on_tool_call = on_tool_call
        self.between_turns = between_turns
        self.on_park = on_park
        self.on_unpark = on_unpark
        # Intent receipts: on_tool_intent(row, intent) -> handle runs before the bridge is
        # called; on_tool_result(handle, completion) completes that row exactly once.
        self.on_tool_intent = on_tool_intent
        self.on_tool_result = on_tool_result
        # Kill switch before every tool call (stop, grant, duration, budget, daily cap).
        self.before_tool = before_tool
        # Billed-call reservations (ADR 0083 §5): on_model_reserve(row, reservation) durably
        # reserves the worst case before every billed request; the response settles it
        # (on_model_call, or the billed_call of a halting error); on_model_release(row)
        # voids it after a definite rejection that sent nothing billable.
        self.on_model_reserve = on_model_reserve
        self.on_model_release = on_model_release
        self._observations: dict[str, dict] = {}
        # Rows whose usage became unknown (an uncertain billed call); status() refuses them.
        self._unknown: set[str] = set()
        # In-memory conversations parked by a definite rejection on a later turn.
        self._pending: dict[str, dict] = {}

    def __repr__(self) -> str:
        return f"AnthropicMessagesReasoning(model={self.model_id!r})"

    # -- credential -------------------------------------------------------------------------
    def _api_key(self) -> str:
        try:
            raw = private_file(
                self.credentials_dir / CREDENTIAL_FILE, credentials_dir=self.credentials_dir
            ).rstrip(b"\r\n")
        except (OSError, ValueError):
            raise _blocked("model_credential_unavailable", sent=False) from None
        if not _KEY.fullmatch(raw):
            raise _blocked("model_credential_unavailable", sent=False)
        return raw.decode("ascii")

    def _headers(self, key: str) -> dict:
        return {
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    # -- harness interface --------------------------------------------------------------------
    def verify(self) -> bool:
        try:
            validate_anthropic_profile(self.profile)
        except ValueError:
            raise _blocked("anthropic_profile_invalid", sent=False) from None
        self._api_key()  # Presence/shape only; discarded immediately.
        return True

    def create(self, row: dict) -> None:
        return None  # Local session; nothing to create upstream.

    def interrupt(self, row: dict) -> None:
        return None  # Calls are synchronous; nothing to interrupt.

    def _retry_after(self, headers: dict) -> float | None:
        value = headers.get("retry-after", "")
        if re.fullmatch(r"[0-9]{1,6}(\.[0-9]{1,3})?", value):
            seconds = float(value)
        else:
            seconds = None
            for name, reset in headers.items():
                if not (name.startswith("anthropic-ratelimit-") and name.endswith("-reset")):
                    continue
                try:
                    when = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when.tzinfo is None:
                    continue
                wait = when.timestamp() - self.clock()
                seconds = wait if seconds is None else max(seconds, wait)
        if seconds is not None and math.isfinite(seconds) and 0 < seconds <= MAX_BACKOFF_SECONDS:
            return seconds
        return None

    def _rejection(self, status: int, headers: dict, body: dict, reservation=None):
        """Map a non-200. ``reservation`` is set only for the billed /v1/messages call.

        429/529/402/billing_error are definite rejections and 4xx request errors are not
        billed; any other status (for example 5xx) leaves the outcome unknown, so the billed
        call carries the reserved worst case and is receipted like a timeout.
        """
        error = body.get("error") if isinstance(body, dict) else None
        kind = error.get("type") if isinstance(error, dict) else None
        if kind == "billing_error" or status == 402:
            return _blocked(
                "model_quota_exhausted",
                sent=False,
                retry_after=self._retry_after(headers),
                condition=True,
            )
        if status in {429, 529} or kind in {"rate_limit_error", "overloaded_error"}:
            return _blocked(
                "model_rate_limited",
                sent=False,
                retry_after=self._retry_after(headers),
                condition=True,
            )
        if reservation is not None and not 400 <= status < 500:
            return _billed("model_call_failed", reservation)
        return _blocked("model_call_failed", sent=None)

    def _post(self, url: str, key: str, body: dict, *, timeout: float, reservation=None):
        """``reservation`` is set for the billed call: a failure after sending reserves it.

        The billed call is streamed whenever the transport offers ``post_stream`` (the
        default AllowlistedTransport does), bounded by the stream idle timeout and the total
        cap; ``timeout`` is then unused.  Otherwise it is one non-streaming POST.
        """
        stream = getattr(self.transport, "post_stream", None)
        try:
            if reservation is not None and callable(stream):
                return stream(
                    url,
                    self._headers(key),
                    dict(body, stream=True),
                    idle_timeout=float(
                        self.limits.get("stream_idle_timeout_seconds", STREAM_IDLE_TIMEOUT_SECONDS)
                    ),
                    total_timeout=self._stream_timeout(),
                )
            return self.transport.post_json(url, self._headers(key), body, timeout=timeout)
        except EgressRefused:
            raise _blocked("model_egress_refused", sent=False) from None
        except Exception:  # noqa: BLE001 - sanitized; never echo upstream detail
            if reservation is not None:
                raise _billed("model_call_uncertain", reservation) from None
            # The free preflight failed; no billed request was attempted.
            raise _blocked("model_call_failed", sent=False) from None

    def _cents(self, usage: dict, prices: dict | None = None) -> float:
        prices = self.prices if prices is None else prices
        total = (
            usage["input"] * prices["input"]
            + usage["output"] * prices["output"]
            + usage["cache_read"] * prices["cache_read"]
            + usage["cache_write"] * prices["cache_write"]
        )
        return total / 1e6

    def _preflight(self, input_tokens: int) -> tuple[int, float]:
        max_output = self.limits["max_output_tokens_per_call"]
        worst_tokens = input_tokens + max_output
        worst_cents = (
            input_tokens * self.prices["input"] + max_output * self.prices["output"]
        ) / 1e6
        if input_tokens > self.limits["max_input_tokens_per_call"]:
            raise CapRefused()
        if worst_cents > self.limits["max_cost_cents_per_call"]:
            raise CapRefused()
        if self.budget_check is not None:
            try:
                allowed = self.budget_check(worst_tokens, worst_cents)
            except Exception as error:  # noqa: BLE001
                from .remote_reasoning_runtime import RuntimeBlocked

                if isinstance(error, RuntimeBlocked):
                    error.request_sent = False
                    raise
                raise CapRefused() from None
            if allowed is False:
                raise CapRefused()
        return worst_tokens, worst_cents

    def _messages_timeout(self) -> float:
        """The operator's ``request_timeout_seconds`` if set, else the derived default."""
        configured = self.limits.get("request_timeout_seconds")
        if configured is not None:
            return float(configured)
        return messages_timeout(self.limits["max_output_tokens_per_call"])

    def _stream_timeout(self) -> float:
        """Total cap for the streamed call: ``request_timeout_seconds`` or stream_timeout."""
        configured = self.limits.get("request_timeout_seconds")
        if configured is not None:
            return float(configured)
        return stream_timeout(self.limits["max_output_tokens_per_call"])

    def _reservation(self, input_tokens: int, worst_cents: float, outcome_class: str) -> dict:
        """Usage unknown: the preflight worst case (counted input + output cap), an estimate."""
        usage = {
            "input": input_tokens,
            "output": self.limits["max_output_tokens_per_call"],
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
        }
        return {
            "outcome_class": outcome_class,
            "cost_basis": "reserved_worst_case_estimate",
            "usage": usage,
            "tokens": sum(usage.values()),
            "cost": worst_cents,
            "reported_model": None,
            "cost_cents_estimate_max": worst_cents,
        }

    def _actual(self, call: dict, worst_cents: float, outcome_class: str, reported: object) -> dict:
        """Usage known from the response: actual tokens, cost from the pinned prices."""
        usage = dict(call) | {"reasoning": 0}
        return {
            "outcome_class": outcome_class,
            "cost_basis": "computed_from_pinned_prices",
            "usage": usage,
            "tokens": sum(usage.values()),
            "cost": self._cents(call),
            "reported_model": PROVIDER + "/" + reported
            if isinstance(reported, str) and _MODEL_ID.match(reported)
            else None,
            "cost_cents_estimate_max": worst_cents,
        }

    def _substituted(self, call: dict, reservation: dict, reported: object) -> dict:
        """Billed under another model: our pinned prices may understate its rates.

        Charge the worst case: ``max(actual at pinned prices, reserved preflight worst
        case)`` for cost and ``max(actual, reserved)`` for tokens.  ``usage`` stays the
        reported actual token breakdown.  If the operator pinned
        ``substitution_pricing_cents_per_mtok`` (each price at least the pinned one), the
        actual is priced at that table instead.
        """
        actual = self._actual(
            call, reservation["cost_cents_estimate_max"], "model_substituted", reported
        )
        return actual | {
            "cost_basis": "worst_case_for_substituted_model",
            "tokens": max(actual["tokens"], reservation["tokens"]),
            "cost": max(
                self._cents(call, self.substitution_prices),
                reservation["cost"],
            )
            if self.substitution_prices is not None
            else max(actual["cost"], reservation["cost"]),
        }

    @staticmethod
    def _usage(body: dict) -> dict | None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None
        call = {
            "input": _count(usage.get("input_tokens")),
            "output": _count(usage.get("output_tokens")),
            "cache_read": _count(usage.get("cache_read_input_tokens") or 0),
            "cache_write": _count(usage.get("cache_creation_input_tokens") or 0),
        }
        return None if any(v is None for v in call.values()) else call

    def _model_matches(self, reported: object) -> bool:
        return isinstance(reported, str) and (
            reported == self.model_id
            or re.fullmatch(re.escape(self.model_id) + r"-[0-9]{8}", reported) is not None
        )

    # -- tool loop ------------------------------------------------------------------------------
    def _offered_tools(self) -> list[dict]:
        if not self.allowed:
            return []
        if self.tool_bridge is None:
            raise _blocked("tool_bridge_unavailable", sent=False)
        try:
            listed = self.tool_bridge.tools()
        except Exception as error:  # noqa: BLE001 - sanitized; codes only
            code = str(error) if _TOOL_CODE.match(str(error)) else "tool_bridge_unavailable"
            raise _blocked(code, sent=False) from None
        if not isinstance(listed, list):
            raise _blocked("tool_bridge_unavailable", sent=False)
        tools = []
        for tool in listed:
            if not (
                isinstance(tool, dict)
                and set(tool) == {"name", "description", "input_schema"}
                and isinstance(tool["name"], str)
                and isinstance(tool["description"], str)
                and isinstance(tool["input_schema"], dict)
            ):
                raise _blocked("tool_catalog_changed", sent=False)
            if tool["name"] in self.allowed:  # Deny by default, even if the bridge over-lists.
                tools.append(copy.deepcopy(tool))
        if {tool["name"] for tool in tools} != self.allowed or len(tools) != len(self.allowed):
            raise _blocked("tool_catalog_changed", sent=False)
        return sorted(tools, key=lambda tool: tool["name"])

    def prompt(self, row: dict, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise _blocked("prompt_required_or_changed", sent=False)
        if row["id"] in self._pending:
            # A parked conversation continues only through resume(); never replay turn 1.
            raise _blocked("model_resume_required", sent=None)
        if hasattr(self, "prompt_fence"):
            self.prompt_fence(row, text)
        state = {
            "messages": [{"role": "user", "content": text}],
            "tools": self._offered_tools(),
            "turn": 1,
            "tool_calls": 0,
        }
        self._loop(row, state)

    def resume(self, row: dict) -> None:
        """Continue a loop parked by a definite rejection, at the rejected turn."""
        state = self._pending.pop(row["id"], None)
        if state is None:
            raise _blocked("model_resume_unavailable", sent=None)
        try:
            self._loop(row, state)
        finally:
            if row["id"] not in self._pending:
                self._unpark(row)  # Completed or halted: the persisted state is obsolete.

    def _unpark(self, row: dict) -> None:
        if self.on_unpark is None:
            return
        try:
            self.on_unpark(row)
        except Exception:  # noqa: BLE001 - best effort; must not mask the loop's own outcome
            pass

    def _snapshot(self, row: dict, state: dict) -> dict:
        """The JSON-serialisable paused loop (schema version 1); no key, no headers."""
        observed = self._observations[row["id"]]
        snapshot = {
            "version": SNAPSHOT_VERSION,
            "run_id": row["id"],
            "turn": state["turn"],
            "messages": copy.deepcopy(state["messages"]),
            "tools_sha256": catalog_sha256(state["tools"]),
            "tool_calls_used": state["tool_calls"],
            "turns_used": observed["turns"],
            "model_id": self.model_id,
            "cumulative": {
                "tokens": observed["tokens"],
                "cost": observed["cost"],
                "usage": dict(observed["usage"]),
            },
        }
        # Proves serialisability (no NaN, no foreign objects) before anyone stores it.
        json.dumps(snapshot, allow_nan=False)
        return snapshot

    def _park(self, row: dict, state: dict, error):
        """A definite rejection after an accepted turn: keep the conversation for resume()."""
        if self.on_park is not None:
            try:
                self.on_park(row, self._snapshot(row, state))
            except Exception:  # noqa: BLE001 - sanitized; the loop is dropped
                return _blocked("model_park_unrecorded", sent=None)
        error.request_sent = True
        error.resumable = True
        self._pending[row["id"]] = state
        return error

    def restore(self, row: dict, state: dict) -> None:
        """Re-arm a parked loop from an on_park snapshot so resume(row) continues at its turn.

        Validation is strict; any mismatch refuses with ``model_resume_state_invalid`` and
        nothing is sent.  The offered tools are re-listed and must match the parked digest.
        """
        run_id = row["id"]
        if run_id in self._pending or run_id in self._unknown:
            raise _blocked("model_resume_state_invalid", sent=None)
        tools = self._offered_tools()
        if not self._snapshot_valid(row, state, tools):
            raise _blocked("model_resume_state_invalid", sent=None)
        cumulative = state["cumulative"]
        self._observations[run_id] = {
            "tokens": cumulative["tokens"],
            "cost": cumulative["cost"],
            "outcome": None,
            "usage": dict(cumulative["usage"]),
            # Not part of the snapshot; the next accepted turn reports both again.
            "reported_model": None,
            "rate_limit": None,
            "cost_cents_estimate_max": None,
            "turns": state["turns_used"],
            "tool_calls": state["tool_calls_used"],
        }
        self._pending[run_id] = {
            "messages": copy.deepcopy(state["messages"]),
            "tools": tools,
            "turn": state["turn"],
            "tool_calls": state["tool_calls_used"],
        }

    def _snapshot_valid(self, row: dict, state: object, tools: list[dict]) -> bool:
        if not isinstance(state, dict) or set(state) != SNAPSHOT_KEYS:
            return False
        turn, used, calls = state["turn"], state["turns_used"], state["tool_calls_used"]
        if (
            type(state["version"]) is not int
            or state["version"] != SNAPSHOT_VERSION
            or state["run_id"] != row["id"]
            or state["model_id"] != self.model_id
            or not isinstance(state["tools_sha256"], str)
            or not _SHA256.match(state["tools_sha256"])
            or state["tools_sha256"] != catalog_sha256(tools)
            or type(turn) is not int
            or not 2 <= turn <= self.max_steps
            or type(used) is not int
            or used != turn - 1
            or type(calls) is not int
            or not 0 <= calls <= self.limits.get("max_tool_calls", 0)
        ):
            return False
        return self._messages_valid(state["messages"], turn, calls) and self._cumulative_valid(
            state["cumulative"]
        )

    def _messages_valid(self, messages: object, turn: int, calls: int) -> bool:
        # The first prompt, then one assistant turn and one tool_result turn per accepted turn.
        if not isinstance(messages, list) or len(messages) != 2 * (turn - 1) + 1:
            return False
        results, requested = 0, []
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                return False
            role, content = message["role"], message["content"]
            if role != ("user" if index % 2 == 0 else "assistant"):
                return False
            if index == 0:
                if not isinstance(content, str) or not content:
                    return False
                continue
            if not isinstance(content, list) or not content:
                return False
            if not all(isinstance(block, dict) for block in content):
                return False
            if role == "assistant":
                requested = [b.get("id") for b in content if b.get("type") == "tool_use"]
                continue
            # Each tool_result turn answers exactly the previous turn's tool_use ids, in order.
            if [block.get("tool_use_id") for block in content] != requested or not all(
                block.get("type") == "tool_result"
                and isinstance(block.get("tool_use_id"), str)
                and isinstance(block.get("content"), str)
                and type(block.get("is_error")) is bool
                for block in content
            ):
                return False
            results += len(content)
        return results == calls

    def _cumulative_valid(self, cumulative: object) -> bool:
        if not isinstance(cumulative, dict) or set(cumulative) != {"tokens", "cost", "usage"}:
            return False
        usage, tokens, cost = cumulative["usage"], cumulative["tokens"], cumulative["cost"]
        if (
            not isinstance(usage, dict)
            or set(usage) != set(USAGE_KEYS)
            or not all(_count(usage[key]) is not None for key in USAGE_KEYS)
            or type(tokens) is not int
            or tokens != sum(usage.values())
            or type(cost) not in {int, float}
            or not math.isfinite(cost)
            or cost < 0
        ):
            return False
        # The cost must be what the configured prices give for the recorded usage.
        expected = self._cents({key: usage[key] for key in PRICE_KEYS})
        return math.isclose(cost, expected, rel_tol=1e-9, abs_tol=1e-9)

    def _halt(self, error: BaseException | None, code: str):
        """A non-resumable stop after an accepted turn; the conversation is dropped."""
        from .remote_reasoning_runtime import RuntimeBlocked

        if not isinstance(error, RuntimeBlocked):
            error = _blocked(code, sent=True)
        error.request_sent = True
        error.resumable = False
        return error

    def _uncertain(self, row: dict, code: str):
        self._unknown.add(row["id"])
        return _blocked(code, sent=None)

    def _loop(self, row: dict, state: dict) -> None:
        from .remote_reasoning_runtime import RuntimeBlocked

        while state["turn"] <= self.max_steps:
            turn = state["turn"]
            if turn > 1 and self.between_turns is not None:
                try:
                    self.between_turns(row)  # Kill switch: stop, grant, duration, budget.
                except Exception as error:  # noqa: BLE001
                    raise self._halt(error, "invalid_authority_or_usage") from None
            try:
                body, worst_cents, input_tokens = self._turn(row, state)
            except RuntimeBlocked as error:
                if error.request_sent is False and turn > 1:
                    raise self._park(row, state, error) from None
                raise
            final = self._account(row, state, body, worst_cents, input_tokens)
            if final is not None:
                self._observations[row["id"]]["outcome"] = "succeeded"
                if self.output_sink is not None:
                    try:
                        self.output_sink(row, final)
                    except Exception:  # noqa: BLE001
                        if self.on_model_call is not None:
                            # This turn's actual usage is already receipted by
                            # on_model_call; a second row would double count it.
                            raise _blocked("model_output_unrecorded", sent=None) from None
                        raise self._billed_turn(
                            row,
                            state,
                            "model_output_unrecorded",
                            self._actual(
                                self._usage(body), worst_cents, "output_unrecorded", body["model"]
                            ),
                            body,
                        ) from None
                return
            if turn == self.max_steps:
                # No turn left to send tool results to: refuse the tools (turn_limit_reached).
                self._skip_tools(row, state, body["content"])
                break
            results = self._run_tools(row, state, body["content"])
            state["messages"].append({"role": "assistant", "content": body["content"]})
            state["messages"].append({"role": "user", "content": results})
            state["turn"] = turn + 1
        observed = self._observations[row["id"]]
        observed["outcome"] = "failed"
        observed["reason"] = "turn_limit_reached"

    @staticmethod
    def _stop_reason(body: dict) -> str | None:
        stop_reason = body.get("stop_reason") if isinstance(body, dict) else None
        if isinstance(stop_reason, str) and _STOP_REASON.match(stop_reason):
            return stop_reason
        return None

    def _billed_turn(self, row: dict, state: dict, code: str, billed_call: dict, body=None):
        """This turn ends the run with a billed or possibly billed call (ADR 0083 §5).

        ``billed_call`` is this turn's receipt for the supervisor: actual usage when the
        response reported it, otherwise the reserved preflight worst case for this turn. It
        carries the turn number (and the stop reason when a response arrived); the turn was
        not passed to ``on_model_call``, so the row is written exactly once. Never replayed.
        """
        self._unknown.add(row["id"])
        billed_call = dict(billed_call) | {"turn": state["turn"]}
        stop_reason = self._stop_reason(body) if body is not None else None
        if stop_reason is not None:
            billed_call["stop_reason"] = stop_reason
        return _billed(code, billed_call)

    def _reserve(self, row: dict, state: dict, reservation: dict) -> None:
        """Durably reserve this turn's worst case before the billed request; else not sent."""
        if self.on_model_reserve is None:
            return
        try:
            self.on_model_reserve(row, dict(reservation) | {"turn": state["turn"]})
        except CapRefused as refused:
            # The authoritative (atomic) cap check refused the reservation: nothing sent.
            code = refused.args[0] if refused.args else None
            if code not in {"model_call_cap_exceeded", "daily_cap_exceeded"}:
                code = "model_call_cap_exceeded"
            raise _blocked(code, sent=False) from None
        except Exception:  # noqa: BLE001 - sanitized; the request is never sent unreserved
            raise _blocked("model_call_unrecorded", sent=False) from None

    def _release(self, row: dict) -> None:
        """Void the reservation of a request that definitely billed nothing.

        A failed release leaves the reservation in place, so the call is receipted as its
        worst case and the run halts uncertain (never resumed against a stale ledger).
        """
        if self.on_model_release is None:
            return
        try:
            self.on_model_release(row)
        except Exception:  # noqa: BLE001
            raise self._uncertain(row, "model_call_unrecorded") from None

    def _turn(self, row: dict, state: dict) -> tuple[dict, float, int]:
        """Preflight and one billed call; returns the accepted, model-checked response body."""
        key = self._api_key()
        request = {"model": self.model_id, "messages": state["messages"]}
        if state["tools"]:
            request["tools"] = state["tools"]
        status, headers, body = self._post(COUNT_URL, key, request, timeout=COUNT_TIMEOUT_SECONDS)
        if status != 200:
            raise self._rejection(status, headers, body)
        input_tokens = _count(body.get("input_tokens"))
        if input_tokens is None:
            raise _blocked("model_call_failed", sent=False)
        try:
            _, worst_cents = self._preflight(input_tokens)
        except CapRefused:
            raise _blocked("model_call_cap_exceeded", sent=False) from None
        request = {
            "model": self.model_id,
            "max_tokens": self.limits["max_output_tokens_per_call"],
            "messages": state["messages"],
        }
        if state["tools"]:
            request["tools"] = state["tools"]
        # This turn's worst case: reserved durably before sending, and kept as the receipt
        # if the outcome becomes unknown.
        reservation = self._reservation(input_tokens, worst_cents, "uncertain")
        self._reserve(row, state, reservation)
        try:
            status, headers, body = self._post(
                API_URL, key, request, timeout=self._messages_timeout(), reservation=reservation
            )
        except Exception as error:
            if not isinstance(getattr(error, "billed_call", None), dict):
                self._release(row)  # Egress refused before sending: nothing billable.
            if getattr(error, "request_sent", None) is None:
                self._unknown.add(row["id"])
                if isinstance(getattr(error, "billed_call", None), dict):
                    error.billed_call["turn"] = state["turn"]
            raise
        del key
        if status != 200:
            error = self._rejection(status, headers, body, reservation)
            if not isinstance(getattr(error, "billed_call", None), dict):
                # 429/529/402/billing_error or a 4xx request error: not billed.
                self._release(row)
            if error.request_sent is None:
                self._unknown.add(row["id"])
                if isinstance(getattr(error, "billed_call", None), dict):
                    error.billed_call["turn"] = state["turn"]
            raise error
        call = self._usage(body) if isinstance(body, dict) else None
        content = body.get("content") if isinstance(body, dict) else None
        reported = body.get("model") if isinstance(body, dict) else None
        if call is None or not isinstance(content, list) or not isinstance(reported, str):
            # Not a usable message (empty, unparseable, or missing content/usage/model):
            # accepted but unaccountable, so the outcome and usage are unknown.  This is
            # checked first: an unusable body is never evidence of a substitution.
            raise self._billed_turn(row, state, "model_call_uncertain", reservation)
        if not self._model_matches(reported):
            # Billed under another model: receipt the worst case and halt.
            raise self._billed_turn(
                row,
                state,
                "model_substituted",
                self._substituted(call, reservation, reported),
                body,
            )
        return body, worst_cents, input_tokens

    def _account(
        self, row: dict, state: dict, body: dict, worst_cents: float, input_tokens: int
    ) -> str | None:
        """Account one accepted turn; receipt it; return final text or None to continue."""
        call = self._usage(body)
        content = body.get("content")
        if call is None or not isinstance(content, list):
            # Accepted but unaccountable: the outcome and usage are unknown; reserve.
            raise self._billed_turn(
                row,
                state,
                "model_call_uncertain",
                self._reservation(input_tokens, worst_cents, "uncertain"),
            )
        stop_reason = self._stop_reason(body)
        tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        previous = self._observations.get(row["id"])
        total = dict(previous["usage"]) if previous else dict.fromkeys(call, 0) | {"reasoning": 0}
        for name, value in call.items():
            total[name] += value
        cost = self._cents(call)
        self._observations[row["id"]] = {
            "tokens": sum(total.values()),
            "cost": (previous["cost"] if previous else 0.0) + cost,
            "outcome": None,
            "usage": total,
            "reported_model": PROVIDER + "/" + body["model"],
            "rate_limit": None,
            "cost_cents_estimate_max": worst_cents,
            "turns": (previous["turns"] if previous else 0) + 1,
            "tool_calls": previous["tool_calls"] if previous else 0,
        }
        if self.on_model_call is not None:
            receipt = {
                "turn": state["turn"],
                "usage": call | {"reasoning": 0},
                "total": sum(call.values()),
                "cost": cost,
                "cost_cents_estimate_max": worst_cents,
                "reported_model": PROVIDER + "/" + body["model"],
                "stop_reason": stop_reason,
                "tool_use_count": len(tool_uses),
                # Tools requested on the last allowed turn are never executed.
                "unrun_tool_count": len(tool_uses)
                if stop_reason == "tool_use" and state["turn"] == self.max_steps
                else 0,
            }
            try:
                self.on_model_call(row, receipt)
            except Exception:  # noqa: BLE001
                raise self._uncertain(row, "model_call_unrecorded") from None
        if stop_reason != "tool_use":
            return "".join(
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
        if not tool_uses or not all(
            isinstance(b.get("id"), str)
            and isinstance(b.get("name"), str)
            and isinstance(b.get("input"), dict)
            for b in tool_uses
        ):
            raise self._halt(None, "model_call_failed")
        return None

    @staticmethod
    def _fence_code(error: BaseException) -> str:
        from .remote_reasoning_runtime import RuntimeBlocked

        code = str(error) if isinstance(error, RuntimeBlocked) else ""
        return code if _FENCE_CODE.match(code) else "invalid_authority_or_usage"

    def _receipt(self, row: dict, receipt: dict):
        """A terminal row for a tool that was never executed, or (without intent hooks) any."""
        if self.on_tool_call is None:
            return
        try:
            self.on_tool_call(row, receipt)
        except Exception:  # noqa: BLE001
            raise _blocked("tool_call_unrecorded", sent=None) from None

    def _refuse_rest(self, row: dict, state: dict, blocks: list, start: int, code: str):
        """Receipt this block and every later block of the turn as refused (never executed)."""
        offered = {tool["name"] for tool in state["tools"]}
        for index in range(start, len(blocks)):
            name, input_sha256 = blocks[index]
            self._receipt(
                row,
                {
                    "turn": state["turn"],
                    "index": index,
                    "name": name if _TOOL_NAME.match(name) else None,
                    "input_sha256": input_sha256,
                    "result_sha256": None,
                    "is_error": None,
                    "truncated": None,
                    "allowed": name in self.allowed and name in offered,
                    "status": "refused",
                    "skip_reason": code,
                },
            )

    def _run_tools(self, row: dict, state: dict, content: list) -> list[dict]:
        cap = self.limits.get("max_tool_calls", 0)
        result_limit = self.limits.get("max_tool_result_bytes", MAX_TOOL_RESULT_BYTES)
        offered = {tool["name"] for tool in state["tools"]}
        results = []
        tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        try:
            blocks = [(b["name"], _digest_json(b["input"])) for b in tool_uses]
        except ValueError:
            raise self._halt(None, "model_call_failed") from None
        for index, block in enumerate(tool_uses):
            name, arguments = block["name"], block["input"]
            input_sha256 = blocks[index][1]
            allowed = name in self.allowed and name in offered
            if allowed and self.before_tool is not None:
                try:
                    self.before_tool(row)  # Kill switch before every executed tool call.
                except Exception as error:  # noqa: BLE001
                    code = self._fence_code(error)
                    self._refuse_rest(row, state, blocks, index, code)
                    raise self._halt(error, code) from None
            if state["tool_calls"] + 1 > cap:
                self._refuse_rest(row, state, blocks, index, "tool_call_cap_exceeded")
                raise self._halt(None, "tool_call_cap_exceeded")
            state["tool_calls"] += 1
            self._observations[row["id"]]["tool_calls"] += 1
            position = {
                "turn": state["turn"],
                "index": index,
                "name": name if _TOOL_NAME.match(name) else None,
                "input_sha256": input_sha256,
            }
            if not allowed:
                text, is_error = TOOL_NOT_PERMITTED, True  # Never executed.
                self._receipt(
                    row,
                    position
                    | {
                        "result_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "is_error": True,
                        "truncated": False,
                        "allowed": False,
                        "status": "denied",
                        "skip_reason": None,
                    },
                )
            else:
                text, is_error = self._execute(row, position, name, arguments, result_limit)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": text,
                    "is_error": is_error,
                }
            )
        return results

    def _skip_tools(self, row: dict, state: dict, content: list) -> None:
        """The last allowed turn's tool_use blocks: refused (turn_limit_reached), never run."""
        tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        try:
            blocks = [(b["name"], _digest_json(b["input"])) for b in tool_uses]
        except ValueError:
            raise self._halt(None, "model_call_failed") from None
        self._refuse_rest(row, state, blocks, 0, TURN_LIMIT_REACHED)

    def _execute(self, row: dict, position: dict, name: str, arguments: dict, limit: int):
        """Intent row, bridge call, completion: an executed tool is always on receipts."""
        handle = None
        if self.on_tool_intent is not None:
            if self.on_tool_result is None:
                raise _blocked("tool_call_unrecorded", sent=None)
            try:
                handle = self.on_tool_intent(row, position | {"allowed": True})
            except Exception:  # noqa: BLE001
                raise _blocked("tool_call_unrecorded", sent=None) from None
        try:
            text, is_error, truncated = self._call_tool(name, arguments)
        except _ToolUncertain:
            # The call may have reached the control plane: receipt it, halt, never retry.
            self._complete(
                row,
                handle,
                position,
                {"status": "uncertain", "result_sha256": None, "is_error": None},
                truncated=None,
            )
            raise _blocked("tool_call_uncertain", sent=None) from None
        text, cut = _truncate(text, limit)
        self._complete(
            row,
            handle,
            position,
            {
                "status": "error" if is_error else "succeeded",
                "result_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "is_error": is_error,
            },
            truncated=truncated or cut,
        )
        return text, is_error

    def _complete(self, row: dict, handle, position: dict, outcome: dict, *, truncated):
        completion = outcome | {"truncated": truncated}
        if self.on_tool_intent is None:
            # No intent hooks: one terminal row after the call, as before.
            self._receipt(row, position | completion | {"allowed": True, "skip_reason": None})
            return
        try:
            self.on_tool_result(handle, completion)
        except Exception:  # noqa: BLE001 - the started row stays; a restart marks it uncertain
            raise _blocked("tool_call_unrecorded", sent=None) from None

    def _call_tool(self, name: str, arguments: dict) -> tuple[str, bool, bool]:
        try:
            found = self.tool_bridge.call(name, copy.deepcopy(arguments))
        except Exception:  # noqa: BLE001 - sanitized; any failure leaves the outcome unknown
            raise _ToolUncertain from None
        if (
            not isinstance(found, tuple)
            or len(found) != 2
            or not isinstance(found[0], str)
            or type(found[1]) is not bool
        ):
            raise _ToolUncertain
        # The bridge flags its own truncation on an attribute reset by every call.
        truncated = getattr(self.tool_bridge, "last_truncated", False) is True
        return found[0], found[1], truncated

    def status(self, row: dict) -> dict:
        found = self._observations.get(row["id"])
        if found is None or row["id"] in self._unknown:
            # No in-memory observation (for example after a restart) or an uncertain call.
            raise _blocked("usage_unknown", sent=None)
        return copy.deepcopy(found)
