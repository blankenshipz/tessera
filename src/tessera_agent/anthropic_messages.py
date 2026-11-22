"""Direct Anthropic Messages API reasoning adapter behind an enforced egress allowlist.

The API key is read from ``$CREDENTIALS_DIRECTORY/anthropic-api-key`` at call time, kept only
in a local variable and sent only as the ``x-api-key`` header to the allowlisted model-runtime
host.  Errors carry sanitized codes only; upstream message text, request ids and completion text
never reach exceptions, observations or reprs.  Completion text goes only to an injected
``output_sink(row, text)``.
"""

from __future__ import annotations

import copy
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

from .model_destinations import model_runtime_egress_allowed
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
# count_tokens is free and fast: a short fixed timeout.  The billed non-streaming
# /v1/messages call returns nothing until generation ends, so its timeout scales with the
# per-call output cap (see messages_timeout); a longer call ends uncertain with a reserved
# worst-case estimate on the daily ledger (ADR 0083).
COUNT_TIMEOUT_SECONDS = 30.0
MESSAGES_TIMEOUT_BASE_SECONDS = 60.0
MESSAGES_TIMEOUT_SECONDS_PER_OUTPUT_TOKEN = 0.04
MESSAGES_TIMEOUT_MAX_SECONDS = 600.0
# Optional operator override for the billed call (``limits.request_timeout_seconds``).
REQUEST_TIMEOUT_MIN_SECONDS = 30
REQUEST_TIMEOUT_MAX_SECONDS = 600
LIMIT_KEYS = (
    "max_output_tokens_per_call",
    "max_input_tokens_per_call",
    "max_cost_cents_per_call",
    "daily_token_cap",
    "daily_cost_cap_cents",
)
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")
_KEY = re.compile(rb"[\x21-\x7e]{1,512}")
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9.\-]{0,127}\Z")


class EgressRefused(Exception):
    """The transport refused a destination or redirect; ``str`` is a code only."""


class TransportFailed(Exception):
    """A sanitized transport failure (for example the response bound); a code only."""


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


def _blocked(code: str, *, sent: bool | None, retry_after: float | None = None, condition=False):
    """Build the runtime's sanitized exception lazily (the runtime imports this module).

    ``request_sent`` is False when the model request was definitely not accepted (refused
    before sending, or a definite upstream rejection) and None when the outcome is unknown.
    """
    from .remote_reasoning_runtime import ModelConditionBlocked, RuntimeBlocked

    error = ModelConditionBlocked(code, retry_after) if condition else RuntimeBlocked(code)
    error.request_sent = sent
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


class AllowlistedTransport:
    """POST JSON only to a model-runtime allowlisted ``https`` host on port 443."""

    def __init__(self, provider: str, *, opener=None, timeout=COUNT_TIMEOUT_SECONDS):
        self.provider = provider
        self.timeout = timeout
        self._opener = opener if opener is not None else build_default_opener()

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

    def post_json(self, url, headers, body, *, timeout=None) -> tuple[int, dict, dict]:
        """POST once; ``timeout`` (seconds) overrides the transport default for this request."""
        url = self._check(url)
        data = json.dumps(body, separators=(",", ":")).encode()
        request = Request(url, data=data, method="POST")
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            response = self._opener.open(
                request, timeout=self.timeout if timeout is None else timeout
            )
        except HTTPError as error:
            response = error
        try:
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
            raw = response.read(MAX_RESPONSE_BYTES + 1) or b""
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TransportFailed("model_response_bound")
            try:
                parsed = json.loads(raw) if raw else {}
            except (ValueError, UnicodeDecodeError):
                parsed = {}
            return status, found, parsed if isinstance(parsed, dict) else {}
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - cleanup must not mask the result
                    pass


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


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
    prices = profile.get("pricing_cents_per_mtok")
    if not isinstance(prices, dict) or set(prices) != set(PRICE_KEYS):
        raise ValueError("anthropic_pricing_invalid")
    for key in PRICE_KEYS:
        value = prices[key]
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError("anthropic_pricing_invalid")
    if profile.get("permissions") != [{"action": "*", "resource": "*", "effect": "deny"}]:
        raise ValueError("anthropic_permissions_invalid")


class AnthropicMessagesReasoning:
    """One synchronous Messages call per prompt; status() reports the in-memory observation."""

    def __init__(
        self,
        profile: dict,
        *,
        credentials_dir: Path,
        transport=None,
        clock=time.time,
        budget_check: Callable[[int, float], object] | None = None,
        output_sink: Callable[[dict, str], object] | None = None,
    ):
        validate_anthropic_profile(profile)
        self.profile = profile
        self.model_id = profile["model"]["id"]
        self.limits = dict(profile["limits"])
        self.prices = dict(profile["pricing_cents_per_mtok"])
        self.credentials_dir = Path(credentials_dir)
        self.transport = transport if transport is not None else AllowlistedTransport(PROVIDER)
        self.clock = clock
        self.budget_check = budget_check
        self.output_sink = output_sink
        self._observations: dict[str, dict] = {}

    def __repr__(self) -> str:
        return f"AnthropicMessagesReasoning(model={self.model_id!r})"

    # -- credential -------------------------------------------------------------------------
    def _api_key(self) -> str:
        try:
            raw = private_file(self.credentials_dir / CREDENTIAL_FILE).rstrip(b"\r\n")
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
        """``reservation`` is set for the billed call: a failure after sending reserves it."""
        try:
            return self.transport.post_json(url, self._headers(key), body, timeout=timeout)
        except EgressRefused:
            raise _blocked("model_egress_refused", sent=False) from None
        except Exception:  # noqa: BLE001 - sanitized; never echo upstream detail
            if reservation is not None:
                raise _billed("model_call_uncertain", reservation) from None
            # The free preflight failed; no billed request was attempted.
            raise _blocked("model_call_failed", sent=False) from None

    def _cents(self, usage: dict) -> float:
        total = (
            usage["input"] * self.prices["input"]
            + usage["output"] * self.prices["output"]
            + usage["cache_read"] * self.prices["cache_read"]
            + usage["cache_write"] * self.prices["cache_write"]
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

    def prompt(self, row: dict, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise _blocked("prompt_required_or_changed", sent=False)
        if hasattr(self, "prompt_fence"):
            self.prompt_fence(row, text)
        key = self._api_key()
        messages = [{"role": "user", "content": text}]
        status, headers, body = self._post(
            COUNT_URL,
            key,
            {"model": self.model_id, "messages": messages},
            timeout=COUNT_TIMEOUT_SECONDS,
        )
        if status != 200:
            raise self._rejection(status, headers, body)
        input_tokens = _count(body.get("input_tokens"))
        if input_tokens is None:
            raise _blocked("model_call_failed", sent=False)
        try:
            _, worst_cents = self._preflight(input_tokens)
        except CapRefused:
            raise _blocked("model_call_cap_exceeded", sent=False) from None
        reservation = self._reservation(input_tokens, worst_cents, "uncertain")
        status, headers, body = self._post(
            API_URL,
            key,
            {
                "model": self.model_id,
                "max_tokens": self.limits["max_output_tokens_per_call"],
                "messages": messages,
            },
            timeout=self._messages_timeout(),
            reservation=reservation,
        )
        del key
        if status != 200:
            raise self._rejection(status, headers, body, reservation)
        call = self._usage(body)
        if not self._model_matches(body.get("model")):
            # Billed under another model: receipt the actual usage (or reserve) and halt.
            raise _billed(
                "model_substituted",
                self._actual(call, worst_cents, "model_substituted", body.get("model"))
                if call is not None
                else self._reservation(input_tokens, worst_cents, "model_substituted"),
            )
        content = body.get("content")
        if call is None or not isinstance(content, list):
            # Accepted but unaccountable: the outcome and usage are unknown.
            raise _billed("model_call_uncertain", reservation)
        completion = "".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        previous = self._observations.get(row["id"])
        total = dict(previous["usage"]) if previous else dict.fromkeys(call, 0) | {"reasoning": 0}
        for name, value in call.items():
            total[name] += value
        cost = (previous["cost"] if previous else 0.0) + self._cents(call)
        self._observations[row["id"]] = {
            "tokens": sum(total.values()),
            "cost": cost,
            "outcome": "succeeded",
            "usage": total,
            "reported_model": PROVIDER + "/" + body["model"],
            "rate_limit": None,
            "cost_cents_estimate_max": worst_cents,
        }
        if self.output_sink is not None:
            try:
                self.output_sink(row, completion)
            except Exception:  # noqa: BLE001
                raise _billed(
                    "model_output_unrecorded",
                    self._actual(call, worst_cents, "output_unrecorded", body["model"]),
                ) from None

    def status(self, row: dict) -> dict:
        found = self._observations.get(row["id"])
        if found is None:
            # No in-memory observation (for example after a restart): usage is unknown.
            raise _blocked("usage_unknown", sent=None)
        return copy.deepcopy(found)
