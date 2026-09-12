from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tessera_worker.opencode_harness import (
    AuthorityRejectedError,
    BoundedPlanMessage,
    OpenCodeHarnessError,
    TesseraBinding,
)
from tessera_worker.opencode_http import (
    MAX_SSE_FIELD_BYTES,
    OpenCodeHarnessInstancePin,
    OpenCodeHttpClient,
    OpenCodeHttpError,
    OpenCodeServerPin,
)
from tessera_worker.opencode_live_adapter import LiveOpenCodeHarnessAdapter


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class OpenCodeFixture:
    """In-process, authenticated OpenCode-shaped server with no provider route."""

    def __init__(self) -> None:
        self.document = {
            "openapi": "3.1.0",
            "info": {"title": "OpenCode", "version": "1.2.3"},
            "x-tessera-harness-instance": {
                "server_identity": "server-1",
                "server_incarnation": "incarnation-1",
                "build_sha256": "b" * 64,
                "config_sha256": "c" * 64,
                "workspace_scope": "workspace-1",
            },
        }
        self.requests: list[tuple[str, str, dict[str, str], object | None]] = []
        self.sessions: list[str] = []
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.aborts: list[str] = []
        self.event_batches: list[list[dict[str, object]]] = []
        self.drop_next: str | None = None
        self.redirect_doc = False
        self.provider_calls = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_: object) -> None:
                pass

            def _body(self) -> object | None:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length)) if length else None

            def _record(self, body: object | None) -> None:
                fixture.requests.append((self.command, self.path, dict(self.headers), body))

            def _authorized(self) -> bool:
                return self.headers.get("Authorization") == "Bearer fixture-token"

            def _json(self, status: int, body: object) -> None:
                encoded = _canonical(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _drop_if_configured(self) -> bool:
                if fixture.drop_next == self.path:
                    fixture.drop_next = None
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return True
                return False

            def do_GET(self) -> None:  # noqa: N802
                self._record(None)
                if self.path.startswith("/provider"):
                    fixture.provider_calls += 1
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.path == "/doc":
                    if fixture.redirect_doc:
                        self.send_response(302)
                        self.send_header("Location", "/doc")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        self.close_connection = True
                    else:
                        self._json(200, fixture.document)
                    return
                if self.path == "/event":
                    batch = fixture.event_batches.pop(0) if fixture.event_batches else []
                    encoded = b"".join(
                        (
                            f"id: {item['cursor']}\n"
                            "event: message\n"
                            f"data: {json.dumps(item['data'], separators=(',', ':'))}\n\n"
                        ).encode()
                        for item in batch
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                body = self._body()
                self._record(body)
                if self.path.startswith("/provider"):
                    fixture.provider_calls += 1
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.path == "/session":
                    session_id = f"session-{len(fixture.sessions) + 1}"
                    fixture.sessions.append(session_id)
                    if self._drop_if_configured():
                        return
                    self._json(200, {"id": session_id})
                    return
                if self.path.startswith("/session/") and self.path.endswith("/message"):
                    fixture.messages.append((self.path.split("/")[2], body))
                    if self._drop_if_configured():
                        return
                    if self.headers.get("X-Tessera-Operation-ID", "").startswith("delivery_"):
                        self._json(200, {"id": "upstream-message-1"})
                    else:
                        self._json(200, {})
                    return
                if self.path.startswith("/session/") and self.path.endswith("/abort"):
                    fixture.aborts.append(self.path.split("/")[2])
                    if self._drop_if_configured():
                        return
                    self.send_response(204)
                    self.end_headers()
                    return
                self._json(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def pin(self) -> OpenCodeServerPin:
        return OpenCodeServerPin("1.2.3", hashlib.sha256(_canonical(self.document)).hexdigest())

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


@contextmanager
def fixture() -> Iterator[OpenCodeFixture]:
    value = OpenCodeFixture()
    value.start()
    try:
        yield value
    finally:
        value.close()


def binding() -> TesseraBinding:
    return TesseraBinding(
        "org-1",
        "project-1",
        "task-1",
        "operation-1",
        4,
        "harness-1",
        "attempt-1",
        "operation-authority-1",
        "attempt-authority-1",
    )


def context() -> BoundedPlanMessage:
    return BoundedPlanMessage("plan-7", "Bounded, approved message")


def client(value: OpenCodeFixture) -> OpenCodeHttpClient:
    return OpenCodeHttpClient(
        value.endpoint,
        "Bearer fixture-token",
        value.pin,
        instance_pin(),
        timeout_seconds=2,
    )


def instance_pin(**changes: str) -> OpenCodeHarnessInstancePin:
    fields = {
        "harness_instance_id": "harness-1",
        "server_identity": "server-1",
        "server_incarnation": "incarnation-1",
        "build_sha256": "b" * 64,
        "config_sha256": "c" * 64,
        "workspace_scope": "workspace-1",
    }
    fields.update(changes)
    return OpenCodeHarnessInstancePin(**fields)


# fmt: off
def test_context_only_delivery_has_durable_identity_and_never_resends(tmp_path: Path) -> None:
    with fixture() as upstream:
        adapter = LiveOpenCodeHarnessAdapter(
            client(upstream), tmp_path / "journal", authority_is_current=lambda *_: True,
            context_only_delivery=True,
        )
        bound = binding().__class__(**(binding().__dict__ | {"session_id": "session-1"}))
        first = adapter.deliver_context_only(bound, "delivery_1", "Human context", "a" * 64)
        replay = adapter.deliver_context_only(bound, "delivery_1", "Human context", "a" * 64)
        assert first == replay and first[0] == "upstream-message-1"
        assert len(upstream.messages) == 1 and upstream.provider_calls == 0
        adapter.close()


def test_context_only_lost_ack_stays_uncertain_and_is_not_resent(tmp_path: Path) -> None:
    with fixture() as upstream:
        adapter = LiveOpenCodeHarnessAdapter(
            client(upstream), tmp_path / "journal", authority_is_current=lambda *_: True,
            context_only_delivery=True,
        )
        upstream.drop_next = "/session/session-1/message"
        bound = binding().__class__(**(binding().__dict__ | {"session_id": "session-1"}))
        with pytest.raises(OpenCodeHarnessError, match="uncertain"):
            adapter.deliver_context_only(bound, "delivery_2", "Human context", "b" * 64)
        with pytest.raises(OpenCodeHarnessError, match="uncertain"):
            adapter.deliver_context_only(bound, "delivery_2", "Human context", "b" * 64)
        assert len(upstream.messages) == 1 and upstream.provider_calls == 0
        adapter.close()


class _SseResponse:
    headers = {"Content-Type": "text/event-stream"}

    def getcode(self) -> int:
        return 200

    def read(self, amount: int = -1) -> bytes:  # noqa: ARG002
        return b""

    def close(self) -> None:
        pass


class _PersistentSseResponse(_SseResponse):
    def __init__(self) -> None:
        self.lines = [
            b"id: 0\n",
            b"event: message\n",
            b'data: {"type":"session.status","properties":{"sessionID":"session-1"}}\n',
            b"\n",
        ]
        self.closed = threading.Event()

    def readline(self, amount: int = -1) -> bytes:  # noqa: ARG002
        if self.lines:
            return self.lines.pop(0)
        self.closed.wait(1)
        return b""

    def close(self) -> None:
        self.closed.set()


class _SlowDripSseResponse(_SseResponse):
    def __init__(self) -> None:
        self.closed = threading.Event()

    def readline(self, amount: int = -1) -> bytes:  # noqa: ARG002
        # Model a peer that keeps a blocking line read alive with bytes but
        # never finishes a line.  Closing the response releases the reader.
        end = time.monotonic() + 0.8
        while time.monotonic() < end:
            if self.closed.wait(0.01):
                return b""
        return b": slow\n"

    def close(self) -> None:
        self.closed.set()


class _SlowDripJsonResponse(_SseResponse):
    headers = {"Content-Type": "application/json"}

    def __init__(self) -> None:
        self.closed = threading.Event()

    def read(self, amount: int = -1) -> bytes:  # noqa: ARG002
        # A transport can keep a finite response read pending with slow bytes;
        # the absolute request deadline, not byte progress, wins.
        end = time.monotonic() + 0.8
        while time.monotonic() < end:
            if self.closed.wait(0.01):
                return b""
        return b"{"

    def close(self) -> None:
        self.closed.set()


def _stream_client(response: _SseResponse, timeout_seconds: float = 0.1) -> OpenCodeHttpClient:
    value = OpenCodeHttpClient(
        "http://127.0.0.1:4096",
        "Bearer fixture-token",
        OpenCodeServerPin("1.2.3", "a" * 64),
        instance_pin(),
        timeout_seconds=timeout_seconds,
        opener=lambda _request, _timeout: response,
    )
    value._verified = True  # type: ignore[attr-defined]  # Unit transport fixture; no server call.
    return value


def adapter(
    value: OpenCodeFixture,
    journal: Path,
    *,
    http_client: OpenCodeHttpClient | None = None,
    **kwargs: object,
) -> LiveOpenCodeHarnessAdapter:
    return LiveOpenCodeHarnessAdapter(
        http_client or client(value),
        journal,
        authority_is_current=kwargs.pop("authority_is_current", lambda _b, _a: True),
    )


def live_event(
    cursor: int, session_id: str, kind: str = "session.status", **properties: object
) -> dict[str, object]:
    return {
        "cursor": cursor,
        "data": {"type": kind, "properties": {"sessionID": session_id, **properties}},
    }


def test_requires_authenticated_tls_or_literal_loopback_pinned_endpoint() -> None:
    pin = OpenCodeServerPin("1.2.3", "a" * 64)
    with pytest.raises(ValueError, match="Bearer"):
        OpenCodeHttpClient("http://127.0.0.1:4096", "", pin, instance_pin())
    with pytest.raises(ValueError, match="literal loopback"):
        OpenCodeHttpClient("http://localhost:4096", "Bearer x", pin, instance_pin())
    with pytest.raises(ValueError, match="literal loopback"):
        OpenCodeHttpClient("http://127.0.0.2:4096", "Bearer x", pin, instance_pin())
    assert (
        OpenCodeHttpClient("https://opencode.example", "Bearer x", pin, instance_pin()).origin
        == "https://opencode.example"
    )


def test_auth_exact_version_schema_pin_and_redirect_are_checked_before_session(
    tmp_path: Path,
) -> None:
    with fixture() as value:
        unauthenticated = OpenCodeHttpClient(
            value.endpoint, "Bearer wrong", value.pin, instance_pin()
        )
        with pytest.raises(OpenCodeHttpError, match="rejected"):
            unauthenticated.verify_contract()
        assert value.sessions == []

        wrong_pin = OpenCodeServerPin("1.2.4", value.pin.schema_sha256)
        with pytest.raises(OpenCodeHttpError, match="incompatible"):
            OpenCodeHttpClient(
                value.endpoint, "Bearer fixture-token", wrong_pin, instance_pin()
            ).verify_contract()
        assert value.sessions == []

        value.redirect_doc = True
        with pytest.raises(OpenCodeHttpError, match="redirect denied"):
            client(value).verify_contract()
        assert value.sessions == []
        assert all("Cookie" not in headers for _, _, headers, _ in value.requests)


def test_live_create_message_is_attributed_journaled_and_has_no_provider_surface(
    tmp_path: Path,
) -> None:
    with fixture() as value:
        with adapter(value, tmp_path / "journal") as live:
            bound = live.start(binding(), context())
            assert bound.session_id == "session-1"
            assert live.effect_outcome("operation-1", "create") == "acknowledged"
            assert live.effect_outcome("operation-1", "message") == "acknowledged"
        assert value.messages == [
            ("session-1", {"parts": [{"type": "text", "text": context().message}]})
        ]
        session_request = next(item for item in value.requests if item[1] == "/session")
        assert session_request[2]["X-Tessera-Operation-Id"] == "operation-1"
        assert value.provider_calls == 0
        assert not hasattr(client(value), "provider")


@pytest.mark.parametrize(
    "route,boundary", [("/session", "create"), ("/session/session-1/message", "message")]
)
def test_lost_create_or_message_reply_is_durable_uncertain_and_never_retried(
    tmp_path: Path, route: str, boundary: str
) -> None:
    with fixture() as value:
        value.drop_next = route
        live = adapter(value, tmp_path / "journal")
        with pytest.raises(OpenCodeHarnessError, match="uncertain"):
            live.start(binding(), context())
        assert live.effect_outcome("operation-1", boundary) == "uncertain"
        with pytest.raises(OpenCodeHarnessError, match="already has"):
            live.start(binding(), context())
        assert value.sessions == ["session-1"]
        assert len([item for item in value.requests if item[1] == "/session"]) == 1


def test_revocation_during_setup_blocks_message_after_durable_session(tmp_path: Path) -> None:
    with fixture() as value:
        seen: list[str] = []

        def authority(_binding: TesseraBinding, action: str) -> bool:
            seen.append(action)
            return action != "message"

        live = adapter(value, tmp_path / "journal", authority_is_current=authority)
        with pytest.raises(AuthorityRejectedError):
            live.start(binding(), context())
        assert seen == ["inspect", "create", "message"]
        assert live.active_binding("operation-1").session_id == "session-1"
        assert live.effect_outcome("operation-1", "message") == "blocked"
        assert value.messages == []


@pytest.mark.parametrize("boundary", ["events", "abort"])
def test_revocation_during_contract_recheck_blocks_privileged_call(
    tmp_path: Path, boundary: str
) -> None:
    with fixture() as value:
        counts = {"events": 0, "abort": 0}

        def authority(_binding: TesseraBinding, action: str) -> bool:
            if action == boundary:
                counts[action] += 1
                return counts[action] == 1
            return True

        live = adapter(value, tmp_path / "journal", authority_is_current=authority)
        live.start(binding(), context())
        if boundary == "events":
            with pytest.raises(AuthorityRejectedError):
                live.poll("operation-1")
            assert not any(item[1] == "/event" for item in value.requests)
        else:
            status = live.cancel("operation-1")
            assert status.requested is True and status.unresolved is True
            assert live.effect_outcome("operation-1", "abort") == "blocked"
            assert value.aborts == []


def test_duplicate_upstream_session_cannot_bind_two_operations(tmp_path: Path) -> None:
    with fixture() as value:
        live = adapter(value, tmp_path / "journal")
        first = live.start(binding(), context())
        assert first.session_id == "session-1"
        value.sessions.clear()
        second = TesseraBinding(
            "org-1",
            "project-1",
            "task-2",
            "operation-2",
            5,
            "harness-1",
            "attempt-2",
            "operation-authority-2",
            "attempt-authority-2",
        )
        with pytest.raises(OpenCodeHarnessError, match="session identity conflicts"):
            live.start(second, BoundedPlanMessage("plan-8", "Another bounded message"))
        assert live.effect_outcome("operation-2", "create") == "uncertain"
        assert live.active_binding("operation-2").session_id is None
        assert len(value.messages) == 1


def test_sse_reconnect_preserves_gap_dedup_cursor_and_attribution_after_restart(
    tmp_path: Path,
) -> None:
    with fixture() as value:
        journal = tmp_path / "journal"
        live = adapter(value, journal)
        bound = live.start(binding(), context())
        assert bound.session_id is not None
        value.event_batches = [
            [live_event(2, bound.session_id, state="idle")],
            [
                live_event(0, bound.session_id, state="running"),
                live_event(1, bound.session_id, state="running"),
                live_event(2, bound.session_id, state="idle"),
            ],
        ]
        assert live.poll("operation-1") == []
        assert [item.cursor for item in live.reconnect("operation-1")] == [0, 1, 2]
        live.acknowledge_through("operation-1", 2)
        live.close()

        restarted = adapter(value, journal)
        assert restarted.active_binding("operation-1") == bound
        value.event_batches = [[live_event(3, bound.session_id, state="idle")]]
        assert [item.cursor for item in restarted.reconnect("operation-1")] == [3]
        event_requests = [item for item in value.requests if item[1] == "/event"]
        assert [item[2].get("Last-Event-Id") for item in event_requests] == [None, None, "2"]


def test_interleaved_known_sessions_route_from_one_durable_stream_without_gaps(
    tmp_path: Path,
) -> None:
    with fixture() as value:
        live = adapter(value, tmp_path / "journal")
        first = live.start(binding(), context())
        second_binding = TesseraBinding(
            "org-1",
            "project-1",
            "task-2",
            "operation-2",
            5,
            "harness-1",
            "attempt-2",
            "operation-authority-2",
            "attempt-authority-2",
        )
        second = live.start(second_binding, BoundedPlanMessage("plan-8", "Second message"))
        assert first.session_id is not None and second.session_id is not None
        value.event_batches = [
            [
                live_event(0, first.session_id, state="running"),
                live_event(1, second.session_id, state="running"),
                live_event(2, first.session_id, state="idle"),
            ]
        ]

        assert [event.cursor for event in live.poll("operation-1")] == [0, 2]
        live.acknowledge_through("operation-1", 2)
        assert [event.cursor for event in live.poll("operation-2")] == [1]
        live.acknowledge_through("operation-2", 1)
        value.event_batches = [[live_event(3, first.session_id, state="idle")]]
        assert [event.cursor for event in live.reconnect("operation-1")] == [3]
        event_requests = [item for item in value.requests if item[1] == "/event"]
        assert [item[2].get("Last-Event-Id") for item in event_requests] == [None, "2"]


@pytest.mark.parametrize(
    "changed_client",
    [
        lambda value: OpenCodeHttpClient(
            "http://127.0.0.1:4097", "Bearer fixture-token", value.pin, instance_pin()
        ),
        lambda value: OpenCodeHttpClient(
            value.endpoint,
            "Bearer fixture-token",
            OpenCodeServerPin("1.2.3", "a" * 64),
            instance_pin(),
        ),
        lambda value: OpenCodeHttpClient(
            value.endpoint,
            "Bearer fixture-token",
            value.pin,
            instance_pin(config_sha256="d" * 64),
        ),
        lambda value: OpenCodeHttpClient(
            value.endpoint,
            "Bearer fixture-token",
            value.pin,
            instance_pin(server_identity="server-2"),
        ),
        lambda value: OpenCodeHttpClient(
            value.endpoint,
            "Bearer fixture-token",
            value.pin,
            instance_pin(server_incarnation="incarnation-2"),
        ),
        lambda value: OpenCodeHttpClient(
            value.endpoint,
            "Bearer fixture-token",
            value.pin,
            instance_pin(build_sha256="d" * 64),
        ),
        lambda value: OpenCodeHttpClient(
            value.endpoint,
            "Bearer fixture-token",
            value.pin,
            instance_pin(workspace_scope="workspace-2"),
        ),
    ],
)
def test_restarted_journal_rejects_changed_harness_binding_before_poll_or_mutation(
    tmp_path: Path, changed_client: object
) -> None:
    with fixture() as value:
        journal = tmp_path / "journal"
        live = adapter(value, journal)
        live.start(binding(), context())
        live.close()
        request_count = len(value.requests)
        assert callable(changed_client)
        restarted = adapter(value, journal, http_client=changed_client(value))

        with pytest.raises(OpenCodeHarnessError, match="instance changed"):
            restarted.poll("operation-1")
        with pytest.raises(OpenCodeHarnessError, match="instance changed"):
            restarted.acknowledge_through("operation-1", 0)
        with pytest.raises(OpenCodeHarnessError, match="instance changed"):
            restarted.cancel("operation-1")

        assert len(value.requests) == request_count
        assert restarted.effect_outcome("operation-1", "abort") == "none"


def test_restarted_journal_rechecks_server_incarnation_before_opening_event_stream(
    tmp_path: Path,
) -> None:
    with fixture() as value:
        journal = tmp_path / "journal"
        original_client = client(value)
        live = adapter(value, journal, http_client=original_client)
        live.start(binding(), context())
        live.close()
        value.document["x-tessera-harness-instance"]["server_incarnation"] = "incarnation-2"  # type: ignore[index]
        restarted = adapter(value, journal, http_client=original_client)
        event_requests_before = len([item for item in value.requests if item[1] == "/event"])

        with pytest.raises(OpenCodeHarnessError, match="contract verification failed"):
            restarted.poll("operation-1")

        event_requests_after = len([item for item in value.requests if item[1] == "/event"])
        assert event_requests_after == event_requests_before


def test_persistent_sse_returns_a_complete_event_without_waiting_for_eof() -> None:
    response = _PersistentSseResponse()
    events = _stream_client(response).events(None)

    assert [event.cursor for event in events] == [0]
    assert response.closed.is_set()


def test_slow_drip_cannot_extend_the_sse_deadline() -> None:
    response = _SlowDripSseResponse()
    started = time.monotonic()

    with pytest.raises(OpenCodeHttpError, match="deadline exceeded"):
        _stream_client(response, timeout_seconds=0.05).events(None)

    assert time.monotonic() - started < 0.45
    assert response.closed.is_set()


def test_slow_drip_cannot_extend_the_json_response_deadline() -> None:
    response = _SlowDripJsonResponse()
    http = _stream_client(response, timeout_seconds=0.05)
    started = time.monotonic()

    with pytest.raises(OpenCodeHttpError, match="response deadline exceeded"):
        http._json("GET", "/doc", None, expect_json=True)  # noqa: SLF001 - transport seam.

    assert time.monotonic() - started < 0.45
    assert response.closed.is_set()


def test_unacknowledged_events_redeliver_after_restart_before_cursor_progress(
    tmp_path: Path,
) -> None:
    with fixture() as value:
        journal = tmp_path / "journal"
        live = adapter(value, journal)
        bound = live.start(binding(), context())
        assert bound.session_id is not None
        value.event_batches = [[live_event(0, bound.session_id, state="running")]]
        assert [item.cursor for item in live.poll("operation-1")] == [0]
        live.close()

        restarted = adapter(value, journal)
        assert [item.cursor for item in restarted.reconnect("operation-1")] == [0]
        restarted.acknowledge_through("operation-1", 0)
        value.event_batches = [[live_event(1, bound.session_id, state="idle")]]
        assert [item.cursor for item in restarted.reconnect("operation-1")] == [1]
        event_requests = [item for item in value.requests if item[1] == "/event"]
        assert [item[2].get("Last-Event-Id") for item in event_requests] == [None, "0"]


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        (lambda session_id: live_event(0, "foreign-session"), "unknown or foreign session"),
        (lambda session_id: live_event(0, session_id, "provider.auth"), "unknown event kind"),
    ],
)
def test_foreign_or_unknown_cursor_cannot_advance_durable_replay(
    tmp_path: Path,
    first: object,
    reason: str,
) -> None:
    with fixture() as value:
        live = adapter(value, tmp_path / "journal")
        bound = live.start(binding(), context())
        assert bound.session_id is not None
        assert callable(first)
        value.event_batches = [
            [
                first(bound.session_id),
                live_event(1, bound.session_id, state="running"),
            ]
        ]
        assert live.poll("operation-1") == []
        assert live.quarantine[-1].reason == reason
        with pytest.raises(OpenCodeHarnessError, match="contiguous"):
            live.acknowledge_through("operation-1", 1)

        value.event_batches = [[live_event(2, bound.session_id, state="idle")]]
        assert live.reconnect("operation-1") == []
        event_requests = [item for item in value.requests if item[1] == "/event"]
        assert [item[2].get("Last-Event-Id") for item in event_requests] == [None, None]


def test_unknown_or_oversized_sse_events_are_quarantined_without_provider_effect(
    tmp_path: Path,
) -> None:
    with fixture() as value:
        live = adapter(value, tmp_path / "journal")
        bound = live.start(binding(), context())
        assert bound.session_id is not None
        value.event_batches = [
            [
                live_event(0, bound.session_id, "provider.auth", state="prompt"),
                live_event(1, bound.session_id, note="x" * 513),
            ]
        ]
        assert live.poll("operation-1") == []
        assert [item.reason for item in live.quarantine] == [
            "unknown event kind",
            "unsafe or malformed event",
        ]
        assert value.provider_calls == 0


def test_sse_field_bound_and_lost_abort_are_bounded_and_unresolved(tmp_path: Path) -> None:
    with fixture() as value:
        live = adapter(value, tmp_path / "journal")
        bound = live.start(binding(), context())
        assert bound.session_id is not None
        value.event_batches = [[live_event(0, bound.session_id, note="x" * MAX_SSE_FIELD_BYTES)]]
        with pytest.raises(OpenCodeHarnessError, match="unavailable or unsafe"):
            live.poll("operation-1")

        value.drop_next = "/session/session-1/abort"
        status = live.cancel("operation-1")
        assert status.requested is True
        assert status.abort_acknowledged is False
        assert status.unresolved is True
        assert live.effect_outcome("operation-1", "abort") == "uncertain"
        assert value.provider_calls == 0
