from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import tessera_worker.opencode_compatibility_fixture as compatibility_fixture
from tessera_worker.opencode_compatibility_fixture import (
    FixtureProtocolError,
    FixtureResult,
    FixtureSettings,
    FixtureUnavailable,
    _is_provider_disabled_response,
    _LoopbackClient,
    _safe_environment,
    _ServerProcess,
    fixture_exit_code,
    run_fixture,
)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class ServerFixture:
    """OpenCode-shaped transport fixture; it is not compatibility evidence."""

    def __init__(self) -> None:
        self.document = {"openapi": "3.1.0", "info": {"title": "OpenCode", "version": "1.2.3"}}
        self.requests: list[tuple[str, str, str | None]] = []
        self.drop_event_once = False
        self.message_response: tuple[int, object | None] = (204, None)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_: object) -> None:
                pass

            def authorized(self) -> bool:
                return self.headers.get("Authorization", "").startswith("Basic ")

            def send_json(self, status: int, value: object) -> None:
                body = canonical(value)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                fixture.requests.append(
                    (self.command, self.path, self.headers.get("Last-Event-ID"))
                )
                if not self.authorized():
                    self.send_json(401, {"error": "unauthorized"})
                elif self.path == "/global/health":
                    self.send_json(200, {"healthy": True, "version": "1.2.3"})
                elif self.path == "/doc":
                    self.send_json(200, fixture.document)
                elif self.path == "/event":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if fixture.drop_event_once:
                        fixture.drop_event_once = False
                        return
                    event_id = "2" if self.headers.get("Last-Event-ID") else "1"
                    self.wfile.write(
                        f"id: {event_id}\nevent: server.connected\ndata: {{}}\n\n".encode()
                    )
                else:
                    self.send_json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                fixture.requests.append(
                    (self.command, self.path, self.headers.get("Last-Event-ID"))
                )
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                if not self.authorized():
                    self.send_json(401, {"error": "unauthorized"})
                elif self.path == "/session":
                    self.send_json(200, {"id": "real-shaped-session"})
                elif self.path.endswith("/message"):
                    status, response = fixture.message_response
                    if response is None:
                        self.send_response(status)
                        self.end_headers()
                    else:
                        self.send_json(status, response)
                elif self.path.endswith("/abort"):
                    self.send_json(200, True)
                else:
                    self.send_json(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


@contextmanager
def server_fixture() -> Iterator[ServerFixture]:
    fixture = ServerFixture()
    fixture.start()
    try:
        yield fixture
    finally:
        fixture.close()


def test_missing_or_invalid_opt_in_is_an_honest_skip_configuration(tmp_path: Path) -> None:
    with pytest.raises(FixtureUnavailable, match="not explicitly requested"):
        FixtureSettings.from_environment({})
    with pytest.raises(FixtureUnavailable, match="regular file"):
        FixtureSettings.from_environment(
            {
                "TESSERA_OPENCODE_EXECUTABLE": str(tmp_path / "missing"),
                "TESSERA_OPENCODE_VERSION": "1.2.3",
                "TESSERA_OPENCODE_SCHEMA_SHA256": "a" * 64,
            }
        )


def test_loopback_transport_covers_auth_schema_session_message_sse_reconnect_and_abort() -> None:
    with server_fixture() as fixture:
        client = _LoopbackClient(fixture.server.server_port, "fixture", "password")
        assert client.request("GET", "/global/health", authenticated=False)[0] == 401
        assert client.json("GET", "/global/health")["version"] == "1.2.3"
        document = client.json("GET", "/doc")
        assert hashlib.sha256(canonical(document)).hexdigest()
        session = client.json("POST", "/session", {"title": "fixture"})
        client.acknowledge(
            "POST", f"/session/{session['id']}/message", {"noReply": True, "tools": {}}
        )
        first, cursor = client.events()
        second, _ = client.events(cursor)
        client.acknowledge("POST", f"/session/{session['id']}/abort", {})
        assert [first[0]["id"], second[0]["id"]] == ["1", "2"]
        assert [item[2] for item in fixture.requests if item[1] == "/event"] == [None, "1"]


def test_lost_sse_response_is_reported_and_deliberate_reconnect_is_one_new_request() -> None:
    with server_fixture() as fixture:
        client = _LoopbackClient(fixture.server.server_port, "fixture", "password")
        fixture.drop_event_once = True
        with pytest.raises(FixtureProtocolError, match="ended before an event"):
            client.events()
        assert client.events()[0][0]["id"] == "1"
        assert len([item for item in fixture.requests if item[1] == "/event"]) == 2


def test_full_probe_against_a_local_http_fixture_is_not_real_opencode_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with server_fixture() as fixture:
        executable = tmp_path / "opencode-version"
        executable.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then printf \'opencode 1.2.3\\n\'; fi\n',
            encoding="utf-8",
        )
        executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        environments: list[dict[str, str]] = []

        class LocalHttpServerProcess:
            def __init__(
                self,
                settings: FixtureSettings,
                workspace: Path,
                environment: dict[str, str],
                port: int,
            ) -> None:
                assert settings.executable == executable
                assert workspace.name == "workspace"
                assert port == fixture.server.server_port
                environments.append(environment)
                self.process = None
                self.starts = 0

            def start(self) -> None:
                self.starts += 1

            def close(self) -> bool:
                return True

        digest = hashlib.sha256(canonical(fixture.document)).hexdigest()
        monkeypatch.setattr(
            "tessera_worker.opencode_compatibility_fixture._ServerProcess", LocalHttpServerProcess
        )
        monkeypatch.setattr(
            "tessera_worker.opencode_compatibility_fixture._reserve_port",
            lambda: fixture.server.server_port,
        )

        result = run_fixture(FixtureSettings(executable, "1.2.3", digest))

    assert result.status == "passed"
    assert result.message == "accepted_no_provider_no_tools"
    assert result.sse_events == 2
    assert result.abort_acknowledged is True
    assert result.cleanup_confirmed is True
    assert environments and "HTTPS_PROXY" not in environments[0]
    assert [item[1] for item in fixture.requests] == [
        "/global/health",
        "/global/health",
        "/doc",
        "/session",
        "/session/real-shaped-session/message",
        "/event",
        "/event",
        "/session/real-shaped-session/abort",
    ]


def test_safe_server_environment_drops_provider_proxy_and_opencode_state(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    environment = _safe_environment(tmp_path / "home", config, "user", "password")
    assert "ANTHROPIC_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["OPENCODE_SERVER_PASSWORD"] == "password"
    assert environment["NO_PROXY"] == "*"


def test_version_mismatch_skips_without_starting_a_server(tmp_path: Path) -> None:
    executable = tmp_path / "opencode-version"
    executable.write_text("#!/bin/sh\nprintf 'opencode 9.9.9\\n'\n", encoding="utf-8")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    result = run_fixture(FixtureSettings(executable, "1.2.3", "a" * 64))
    assert result.status == "unavailable"
    assert result.cleanup_confirmed is False


def test_server_process_is_terminated_without_a_duplicate_start(tmp_path: Path) -> None:
    executable = tmp_path / "sleeping-opencode"
    executable.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    settings = FixtureSettings(executable, "1.2.3", "a" * 64)
    server = _ServerProcess(settings, tmp_path, {"PATH": os.defpath}, 9999)
    server.start()
    with pytest.raises(FixtureProtocolError, match="duplicate"):
        server.start()
    assert server.close() is True
    assert server.process is not None and server.process.poll() is not None


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": {"code": "provider_disabled", "enabled_providers": []}}, False),
        (401, {"error": {"code": "provider_disabled", "enabled_providers": []}}, False),
        (403, {"error": {"code": "provider_disabled", "enabled_providers": []}}, False),
        (404, {"error": {"code": "provider_disabled", "enabled_providers": []}}, False),
        (422, {"error": {"code": "request_validation", "enabled_providers": []}}, False),
        (422, {"error": {"code": "provider_disabled", "enabled_providers": []}}, True),
    ],
)
def test_only_the_exact_provider_disabled_response_is_classified_as_disabled(
    status: int, body: object, expected: bool
) -> None:
    assert (
        _is_provider_disabled_response(
            status, {"content-type": "application/json"}, canonical(body)
        )
        is expected
    )


@pytest.mark.parametrize(
    ("status", "body", "expected_status", "expected_message"),
    [
        (400, {"error": {"code": "provider_disabled", "enabled_providers": []}}, "failed", None),
        (401, {"error": {"code": "provider_disabled", "enabled_providers": []}}, "failed", None),
        (403, {"error": {"code": "provider_disabled", "enabled_providers": []}}, "failed", None),
        (404, {"error": {"code": "provider_disabled", "enabled_providers": []}}, "failed", None),
        (422, {"error": {"code": "request_validation", "enabled_providers": []}}, "failed", None),
        (
            422,
            {"error": {"code": "provider_disabled", "enabled_providers": []}},
            "passed",
            "provider_disabled",
        ),
    ],
)
def test_message_error_statuses_are_not_inferred_as_provider_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
    body: object,
    expected_status: str,
    expected_message: str | None,
) -> None:
    with server_fixture() as fixture:
        fixture.message_response = (status, body)
        executable = tmp_path / "opencode-version"
        executable.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then printf \'opencode 1.2.3\\n\'; fi\n',
            encoding="utf-8",
        )
        executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        class LocalHttpServerProcess:
            def __init__(self, *_: object) -> None:
                self.process = None

            def start(self) -> None:
                pass

            def close(self) -> bool:
                return True

        monkeypatch.setattr(
            "tessera_worker.opencode_compatibility_fixture._ServerProcess", LocalHttpServerProcess
        )
        monkeypatch.setattr(
            "tessera_worker.opencode_compatibility_fixture._reserve_port",
            lambda: fixture.server.server_port,
        )

        result = run_fixture(
            FixtureSettings(
                executable, "1.2.3", hashlib.sha256(canonical(fixture.document)).hexdigest()
            )
        )

    assert result.status == expected_status
    assert result.message == expected_message


def test_cleanup_failure_changes_an_otherwise_passing_probe_to_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with server_fixture() as fixture:
        executable = tmp_path / "opencode-version"
        executable.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then printf \'opencode 1.2.3\\n\'; fi\n',
            encoding="utf-8",
        )
        executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        class FailedCleanupServerProcess:
            def __init__(self, *_: object) -> None:
                self.process = None

            def start(self) -> None:
                pass

            def close(self) -> bool:
                return False

        monkeypatch.setattr(
            "tessera_worker.opencode_compatibility_fixture._ServerProcess",
            FailedCleanupServerProcess,
        )
        monkeypatch.setattr(
            "tessera_worker.opencode_compatibility_fixture._reserve_port",
            lambda: fixture.server.server_port,
        )

        result = run_fixture(
            FixtureSettings(
                executable, "1.2.3", hashlib.sha256(canonical(fixture.document)).hexdigest()
            )
        )

    assert result.status == "failed"
    assert result.cleanup_confirmed is False
    assert "cleanup was not confirmed" in result.reason
    assert fixture_exit_code(result) != 0


def test_server_process_cleans_up_its_process_group_descendants(tmp_path: Path) -> None:
    executable = tmp_path / "forking-opencode"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then printf \'opencode 1.2.3\\n\'; exit 0; fi\n'
        "sleep 30 &\n"
        "wait\n",
        encoding="utf-8",
    )
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    server = _ServerProcess(
        FixtureSettings(executable, "1.2.3", "a" * 64), tmp_path, {"PATH": os.defpath}, 9999
    )
    server.start()
    assert server.process is not None
    process_group_id = server.process.pid

    assert server.close() is True
    with pytest.raises(ProcessLookupError):
        os.killpg(process_group_id, 0)


@pytest.mark.parametrize(
    ("status", "exit_status"),
    [("passed", 0), ("failed", 1), ("skipped", 2), ("unavailable", 3)],
)
def test_cli_reports_distinct_statuses_and_never_reserves_zero_for_nonpass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    exit_status: int,
) -> None:
    result = FixtureResult(status, f"{status} fixture result")  # type: ignore[arg-type]
    monkeypatch.setattr(
        compatibility_fixture.FixtureSettings,
        "from_environment",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(compatibility_fixture, "run_fixture", lambda _settings: result)

    assert compatibility_fixture.main() == exit_status
    assert json.loads(capsys.readouterr().out)["status"] == status
