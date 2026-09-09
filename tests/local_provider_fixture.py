"""Loopback-only HTTP fake used to test the local-provider-v1 adapter.

It is a deterministic test fixture, not a provider emulator or credential
service.  Lost replies apply the requested state transition before dropping the
local connection so tests must recover through the adapter's stable identities.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class LocalProviderFixture:
    fixture_token: str = "local-provider-fixture-test-token"
    lose_submit_reply: bool = False
    lose_observe_reply: bool = False
    lose_cancel_reply: bool = False
    accounting_unknown: bool = True
    submit_calls: list[str] = field(default_factory=list)
    observe_calls: list[str] = field(default_factory=list)
    cancel_calls: list[str] = field(default_factory=list)
    accounting_calls: list[str] = field(default_factory=list)
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    _server: ThreadingHTTPServer | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __enter__(self) -> LocalProviderFixture:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def _json_body(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 16 * 1024:
                        return None
                    decoded = json.loads(self.rfile.read(length))
                except (ValueError, json.JSONDecodeError):
                    return None
                return decoded if isinstance(decoded, dict) else None

            def _send(self, value: dict[str, Any]) -> None:
                encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _reject(self) -> None:
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _drop(self) -> None:
                self.close_connection = True

            def _handle(self, method: str) -> None:
                if self.headers.get("X-Local-Provider-Fixture") != fixture.fixture_token:
                    self._reject()
                    return
                if method == "POST" and self.path == "/v1/operations":
                    body = self._json_body()
                    key = self.headers.get("Idempotency-Key")
                    if (
                        body is None
                        or not isinstance(key, str)
                        or body.get("idempotency_key") != key
                        or not all(
                            isinstance(body.get(name), str)
                            for name in ("operation_id", "lookup_key")
                        )
                    ):
                        self._reject()
                        return
                    lookup_key = body["lookup_key"]
                    fixture.submit_calls.append(lookup_key)
                    with fixture._lock:
                        receipt = fixture.operations.get(lookup_key)
                        if receipt is None:
                            provider_id = "local-" + hashlib.sha256(key.encode()).hexdigest()[:48]
                            payload = {
                                "idempotency_key": key,
                                "lookup_key": lookup_key,
                                "operation_id": body["operation_id"],
                                "provider_id": provider_id,
                                "provider_schema": "local-provider-v1",
                            }
                            receipt = {
                                "provider_kind": "fake",
                                "provider_version": "local-provider-v1",
                                "operation_id": body["operation_id"],
                                "provider_id": provider_id,
                                "lookup_key": lookup_key,
                                "idempotency_key": key,
                                "receipt_payload": payload,
                                "state": "running",
                            }
                            fixture.operations[lookup_key] = receipt
                        elif (
                            receipt["operation_id"] != body["operation_id"]
                            or receipt["idempotency_key"] != key
                        ):
                            self._reject()
                            return
                    if fixture.lose_submit_reply:
                        self._drop()
                    else:
                        self._send(receipt)
                    return
                prefix = "/v1/operations/"
                if not self.path.startswith(prefix):
                    self._reject()
                    return
                suffix = self.path[len(prefix) :]
                if suffix.endswith("/cancel"):
                    lookup_key, action = suffix.removesuffix("/cancel"), "cancel"
                elif suffix.endswith("/accounting"):
                    lookup_key, action = suffix.removesuffix("/accounting"), "accounting"
                else:
                    lookup_key, action = suffix, "observe"
                with fixture._lock:
                    receipt = fixture.operations.get(lookup_key)
                if action == "observe" and method == "GET":
                    fixture.observe_calls.append(lookup_key)
                    if fixture.lose_observe_reply:
                        self._drop()
                    else:
                        self._send(fixture._observation(receipt))
                    return
                if action == "cancel" and method == "POST" and self._json_body() == {}:
                    fixture.cancel_calls.append(lookup_key)
                    if receipt is not None:
                        with fixture._lock:
                            receipt["state"] = "exited"
                    if fixture.lose_cancel_reply:
                        self._drop()
                    else:
                        self._send(fixture._observation(receipt))
                    return
                if action == "accounting" and method == "GET":
                    fixture.accounting_calls.append(lookup_key)
                    if receipt is None or fixture.accounting_unknown:
                        self._send({"exposure_state": "unknown", "provider_id": None})
                    else:
                        self._send(
                            {
                                "exposure_state": "reported",
                                "provider_id": receipt["provider_id"],
                                "usage": {"synthetic_units": 0},
                            }
                        )
                    return
                self._reject()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def endpoint(self) -> str:
        if self._server is None:
            raise RuntimeError("Local provider fixture is not running.")
        return f"http://127.0.0.1:{self._server.server_port}"

    @staticmethod
    def _observation(receipt: dict[str, Any] | None) -> dict[str, Any]:
        if receipt is None:
            return {"state": "unknown", "provider_id": None, "termination_confirmed": False}
        return {
            "state": receipt["state"],
            "provider_id": receipt["provider_id"],
            "receipt": {key: value for key, value in receipt.items() if key != "state"},
            "termination_confirmed": receipt["state"] == "exited",
        }

    def __exit__(self, *_: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
