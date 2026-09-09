"""Bounded client for the injected ``local-provider-v1`` HTTP fake.

This adapter only permits a literal loopback endpoint supplied by a test or
development fixture.  It has no provider discovery, credential loading,
fallback, retry loop, cloud route, or billing path.  Callers retain an uncertain
effect after a transport failure and reconcile using the stable lookup key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .provider import (
    ProviderAccountingObservation,
    ProviderError,
    ProviderObservation,
    ProviderReceipt,
)

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 5.0
_MAX_ENDPOINT_BYTES = 2048
_MAX_TOKEN_BYTES = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


class LocalProviderHttpError(ProviderError):
    """Sanitized local-provider protocol or transport failure."""


class LocalProviderResponse(Protocol):
    headers: Any

    def getcode(self) -> int: ...

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


OpenRequest = Callable[[Request, float], LocalProviderResponse]


class _RejectRedirects(HTTPRedirectHandler):
    handler_order = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        raise LocalProviderHttpError("local provider redirect denied")


def _default_open(request: Request, timeout: float) -> LocalProviderResponse:
    return build_opener(ProxyHandler({}), _RejectRedirects()).open(request, timeout=timeout)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _invalid_json_constant(_: str) -> None:
    raise ValueError("non-standard JSON constant")


def _close_quietly(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except BaseException:
            pass


class LocalProviderV1:
    """Version-pinned submit/observe/cancel/accounting fake-provider adapter."""

    kind = "fake"
    version = "local-provider-v1"

    def __init__(
        self,
        endpoint: str,
        fixture_token: str,
        *,
        timeout_seconds: float = 1.0,
        opener: OpenRequest | None = None,
    ) -> None:
        self.origin = self._origin(endpoint)
        self._fixture_token = self._token(fixture_token)
        if (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("Local provider timeout must be between zero and five seconds.")
        self.timeout_seconds = float(timeout_seconds)
        self._open = opener or _default_open

    @staticmethod
    def _origin(endpoint: str) -> str:
        if (
            not isinstance(endpoint, str)
            or len(endpoint.encode()) > _MAX_ENDPOINT_BYTES
            or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in endpoint)
            or "\\" in endpoint
        ):
            raise ValueError("Local provider endpoint is invalid.")
        try:
            parsed = urlsplit(endpoint)
            hostname, port = parsed.hostname, parsed.port
        except ValueError as error:
            raise ValueError("Local provider endpoint is invalid.") from error
        host = hostname.lower() if hostname else None
        literal_host = f"[{host}]" if host == "::1" else host
        authority = literal_host if port is None else f"{literal_host}:{port}"
        if (
            parsed.scheme != "http"
            or host not in {"127.0.0.1", "::1"}
            or parsed.netloc.lower() != authority
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Local provider endpoint must be a literal loopback HTTP origin.")
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _token(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode()) > _MAX_TOKEN_BYTES
            or not value.isascii()
            or any(ord(char) < 0x21 or ord(char) == 0x7F for char in value)
        ):
            raise ValueError("Local provider fixture token is invalid.")
        return value

    @staticmethod
    def _identifier(value: object, label: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{label} must be a bounded identifier.")
        return value

    def submit(self, operation_id: str, lookup_key: str, idempotency_key: str) -> ProviderReceipt:
        operation_id = self._identifier(operation_id, "operation_id")
        lookup_key = self._identifier(lookup_key, "lookup_key")
        idempotency_key = self._identifier(idempotency_key, "idempotency_key")
        response = self._request(
            "POST",
            "/v1/operations",
            {
                "operation_id": operation_id,
                "lookup_key": lookup_key,
                "idempotency_key": idempotency_key,
            },
            idempotency_key,
        )
        return self._receipt(response, operation_id, lookup_key, idempotency_key)

    def observe(self, lookup_key: str, provider_id: str | None = None) -> ProviderObservation:
        return self._observation("GET", lookup_key, provider_id)

    def cancel(self, lookup_key: str, provider_id: str | None = None) -> ProviderObservation:
        return self._observation("POST", lookup_key, provider_id)

    def accounting(
        self, lookup_key: str, provider_id: str | None = None
    ) -> ProviderAccountingObservation:
        lookup_key = self._identifier(lookup_key, "lookup_key")
        if provider_id is not None:
            self._identifier(provider_id, "provider_id")
        response = self._request("GET", f"/v1/operations/{lookup_key}/accounting", None, None)
        try:
            exposure_state = response["exposure_state"]
            observed_provider_id = response.get("provider_id")
            usage = response.get("usage")
        except (AttributeError, KeyError) as error:
            raise LocalProviderHttpError(
                "local provider accounting response is malformed"
            ) from error
        if exposure_state not in {"unknown", "reported"} or (
            observed_provider_id is not None and not isinstance(observed_provider_id, str)
        ):
            raise LocalProviderHttpError("local provider accounting response is malformed")
        if provider_id is not None and observed_provider_id not in {None, provider_id}:
            return ProviderAccountingObservation("unknown", None)
        if exposure_state == "unknown":
            return ProviderAccountingObservation("unknown", observed_provider_id)
        if not isinstance(usage, dict):
            raise LocalProviderHttpError("local provider accounting response is malformed")
        return ProviderAccountingObservation("reported", observed_provider_id, usage)

    def _observation(
        self, method: str, lookup_key: str, provider_id: str | None
    ) -> ProviderObservation:
        lookup_key = self._identifier(lookup_key, "lookup_key")
        if provider_id is not None:
            self._identifier(provider_id, "provider_id")
        suffix = "/cancel" if method == "POST" else ""
        response = self._request(method, f"/v1/operations/{lookup_key}{suffix}", {}, None)
        try:
            state = response["state"]
            observed_provider_id = response.get("provider_id")
            raw_receipt = response.get("receipt")
            confirmed = response["termination_confirmed"]
        except (AttributeError, KeyError) as error:
            raise LocalProviderHttpError(
                "local provider observation response is malformed"
            ) from error
        if state not in {"unknown", "running", "exited"} or type(confirmed) is not bool:
            raise LocalProviderHttpError("local provider observation response is malformed")
        if state == "unknown":
            return ProviderObservation("unknown", None)
        if not isinstance(observed_provider_id, str) or not isinstance(raw_receipt, dict):
            raise LocalProviderHttpError("local provider observation response is malformed")
        receipt = self._receipt(raw_receipt, None, lookup_key, None)
        if receipt.provider_id != observed_provider_id or (
            provider_id and provider_id != observed_provider_id
        ):
            return ProviderObservation("unknown", None)
        return ProviderObservation(state, observed_provider_id, receipt, confirmed)

    def _receipt(
        self,
        response: dict[str, Any],
        operation_id: str | None,
        lookup_key: str,
        idempotency_key: str | None,
    ) -> ProviderReceipt:
        try:
            payload = response["receipt_payload"]
            receipt = ProviderReceipt(
                response["provider_kind"],
                response["provider_version"],
                response["operation_id"],
                response["provider_id"],
                response["lookup_key"],
                payload,
                response["idempotency_key"],
            )
        except (AttributeError, KeyError, TypeError) as error:
            raise LocalProviderHttpError("local provider receipt response is malformed") from error
        if (
            receipt.provider_kind != self.kind
            or receipt.provider_version != self.version
            or not all(
                isinstance(value, str) and _IDENTIFIER.fullmatch(value)
                for value in (
                    receipt.operation_id,
                    receipt.provider_id,
                    receipt.lookup_key,
                    receipt.idempotency_key,
                )
            )
            or not isinstance(receipt.payload, dict)
            or receipt.lookup_key != lookup_key
            or (operation_id is not None and receipt.operation_id != operation_id)
            or (idempotency_key is not None and receipt.idempotency_key != idempotency_key)
        ):
            raise LocalProviderHttpError("local provider receipt response is malformed")
        # Bind the provider's evidence payload to the immutable identity returned
        # by its envelope.  ProviderDispatch persists this exact payload and the
        # control plane rejects a later receipt with a different digest.
        if (
            receipt.payload.get("provider_schema") != self.version
            or receipt.payload.get("operation_id") != receipt.operation_id
            or receipt.payload.get("provider_id") != receipt.provider_id
            or receipt.payload.get("lookup_key") != receipt.lookup_key
            or receipt.payload.get("idempotency_key") != receipt.idempotency_key
        ):
            raise LocalProviderHttpError("local provider receipt identity is malformed")
        return receipt

    def _request(
        self, method: str, path: str, body: dict[str, object] | None, key: str | None
    ) -> dict[str, Any]:
        data = None if body is None else _canonical_json(body)
        if data is not None and len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Local provider request exceeds its bound.")
        headers = {"Accept": "application/json", "X-Local-Provider-Fixture": self._fixture_token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        request = Request(f"{self.origin}{path}", data=data, headers=headers, method=method)
        try:
            response = self._open(request, self.timeout_seconds)
        except LocalProviderHttpError:
            raise
        except HTTPError as error:
            _close_quietly(error)
            raise LocalProviderHttpError("local provider rejected request") from None
        except (OSError, URLError, ValueError):
            raise LocalProviderHttpError("local provider transport unavailable") from None
        try:
            status = response.getcode()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            encoded = response.read(MAX_RESPONSE_BYTES + 1)
            if (
                not isinstance(status, int)
                or not 200 <= status < 300
                or content_type != "application/json"
            ):
                raise LocalProviderHttpError("local provider rejected request")
            if not isinstance(encoded, bytes) or len(encoded) > MAX_RESPONSE_BYTES:
                raise LocalProviderHttpError("local provider response exceeds its bound")
            decoded = json.loads(encoded.decode("utf-8"), parse_constant=_invalid_json_constant)
            if not isinstance(decoded, dict):
                raise LocalProviderHttpError("local provider response is malformed")
            return decoded
        except LocalProviderHttpError:
            raise
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            raise LocalProviderHttpError("local provider response is malformed") from None
        finally:
            _close_quietly(response)


__all__ = ["LocalProviderHttpError", "LocalProviderV1"]
