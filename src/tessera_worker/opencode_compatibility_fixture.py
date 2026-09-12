"""Opt-in, provider-disabled compatibility probe for a pinned OpenCode binary.

This is deliberately separate from the harness adapter.  It starts one configured
local binary only, and reports unavailable rather than substituting the synthetic
adapter when that binary or its requested version is absent.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .opencode_harness import TesseraBinding
from .opencode_http import OpenCodeHarnessInstancePin, OpenCodeHttpClient, OpenCodeServerPin
from .opencode_live_adapter import LiveOpenCodeHarnessAdapter

_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BODY_BYTES = 64 * 1024
_MAX_SSE_BYTES = 32 * 1024
_REQUEST_TIMEOUT = 3.0
_SSE_TIMEOUT = 5.0
_START_TIMEOUT = 12.0
_CLEANUP_TIMEOUT = 5.0


class FixtureUnavailable(Exception):
    """The explicitly opt-in fixture cannot safely run on this node."""


class FixtureSkipped(FixtureUnavailable):
    """The fixture was not explicitly requested on this node."""


class FixtureProtocolError(RuntimeError):
    """The selected OpenCode server did not meet the narrow fixture contract."""


@dataclass(frozen=True)
class FixtureSettings:
    executable: Path
    version: str
    schema_sha256: str

    @classmethod
    def from_environment(cls, environment: dict[str, str] | None = None) -> FixtureSettings:
        values = os.environ if environment is None else environment
        executable = values.get("TESSERA_OPENCODE_EXECUTABLE")
        version = values.get("TESSERA_OPENCODE_VERSION")
        schema = values.get("TESSERA_OPENCODE_SCHEMA_SHA256")
        if not executable and not version and not schema:
            raise FixtureSkipped("fixture was not explicitly requested")
        if not executable or not version or not schema:
            raise FixtureUnavailable(
                "all of TESSERA_OPENCODE_EXECUTABLE, TESSERA_OPENCODE_VERSION, and "
                "TESSERA_OPENCODE_SCHEMA_SHA256 are required"
            )
        path = Path(executable)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or not os.access(path, os.X_OK)
        ):
            raise FixtureUnavailable(
                "configured OpenCode executable is not an executable regular file"
            )
        if not _VERSION.fullmatch(version):
            raise FixtureUnavailable("configured OpenCode version is not an exact semantic version")
        if not _SHA256.fullmatch(schema):
            raise FixtureUnavailable("configured OpenCode schema digest is invalid")
        return cls(path, version, schema)


@dataclass(frozen=True)
class FixtureResult:
    status: Literal["skipped", "unavailable", "passed", "failed"]
    reason: str
    version: str | None = None
    schema_sha256: str | None = None
    health_checked: bool = False
    auth_checked: bool = False
    session_created: bool = False
    message: str | None = None
    sse_events: int = 0
    reconnected: bool = False
    abort_acknowledged: bool = False
    cleanup_confirmed: bool = False
    transport_checked: bool = False
    reconnect_checked: bool = False
    cursor_replay_checked: bool = False
    session_attribution_checked: bool = False
    context_readback_checked: bool = False
    activity_checked: bool = False
    retained_temp_state: str | None = None

    def json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def fixture_exit_code(result: FixtureResult) -> int:
    """Reserve zero for a fully verified, cleaned-up fixture run."""
    return {"passed": 0, "failed": 1, "skipped": 2, "unavailable": 3}[result.status]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise FixtureProtocolError("fixture directory is not private")


def _safe_environment(home: Path, config: Path, username: str, password: str) -> dict[str, str]:
    # Do not inherit provider credentials, proxy settings, OpenCode account state,
    # or arbitrary configuration from the invoking shell.
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "LANG": "C",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    environment.update(
        {
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "OPENCODE_CONFIG": str(config),
            "OPENCODE_CONFIG_DIR": str(home / "config-dir"),
            "OPENCODE_CONFIG_CONTENT": config.read_text(encoding="utf-8"),
            "OPENCODE_SERVER_USERNAME": username,
            "OPENCODE_SERVER_PASSWORD": password,
        }
    )
    return environment


def _fixture_config() -> dict[str, object]:
    return {
        "autoupdate": False,
        "enabled_providers": [],
        "formatter": False,
        "lsp": False,
        "mcp": {},
        "permission": {"*": "deny"},
        "plugin": [],
        "share": "disabled",
        "snapshot": False,
        "tools": {"bash": False, "edit": False, "webfetch": False, "websearch": False},
    }


def _is_provider_disabled_response(status: int, headers: dict[str, str], body: bytes) -> bool:
    """Recognize only the fixture's explicit no-provider response contract.

    A generic client, route, authorization, or request-schema error is not
    evidence that providers were disabled.  The exact 422 envelope is deliberately
    narrow so upstream response drift fails this compatibility fixture.
    """
    if (
        status != 422
        or headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
    ):
        return False
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return decoded == {"error": {"code": "provider_disabled", "enabled_providers": []}}


def _event_session_id(event: dict[str, str]) -> str | None:
    """Return only the typed session attribution used by the pinned probe."""
    try:
        envelope = json.loads(event["data"])
        session_id = envelope["properties"]["sessionID"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    return session_id if isinstance(session_id, str) and session_id else None


def _forbidden_activity(value: object) -> bool:
    """Reject observed provider/inference/tool/plugin/MCP activity, never infer absence.

    A context-only HTTP response cannot prove that an opaque server never made a
    network call.  It *can* prove that it returned an assistant/tool-shaped part
    or emitted a forbidden activity event, which makes provider-disabled mode a
    failure rather than a green claim.
    """
    if isinstance(value, str):
        return any(
            token in value.lower()
            for token in ("provider", "inference", "tool", "plugin", "mcp", "assistant")
        )
    if isinstance(value, list):
        return any(_forbidden_activity(item) for item in value)
    if isinstance(value, dict):
        return any(
            _forbidden_activity(key) or _forbidden_activity(item) for key, item in value.items()
        )
    return False


def _launcher_pin() -> OpenCodeHarnessInstancePin:
    """Fixture-local trusted launcher binding; intentionally absent from ``/doc``."""
    return OpenCodeHarnessInstancePin(
        harness_instance_id="fixture-launcher",
        server_identity="fixture-server",
        server_incarnation="fixture-incarnation",
        build_sha256="f" * 64,
        config_sha256="e" * 64,
        workspace_scope="fixture-workspace",
    )


class _LoopbackClient:
    """Small basic-auth client with no proxy, redirect, cookie, or DNS surface."""

    def __init__(self, port: int, username: str, password: str) -> None:
        self.port = port
        encoded = base64.b64encode(f"{username}:{password}".encode("ascii")).decode("ascii")
        self.authorization = f"Basic {encoded}"

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        authenticated: bool = True,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        encoded = None if body is None else _canonical_json(body)
        if encoded is not None and len(encoded) > _MAX_BODY_BYTES:
            raise FixtureProtocolError("fixture request exceeds its bound")
        request_headers = {"Accept": "application/json"}
        if authenticated:
            request_headers["Authorization"] = self.authorization
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=_REQUEST_TIMEOUT)
        try:
            connection.request(method, path, body=encoded, headers=request_headers)
            response = connection.getresponse()
            data = response.read(_MAX_BODY_BYTES + 1)
            if len(data) > _MAX_BODY_BYTES:
                raise FixtureProtocolError("fixture response exceeds its bound")
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, data
        except (OSError, http.client.HTTPException) as error:
            raise FixtureProtocolError("loopback OpenCode request failed") from error
        finally:
            connection.close()

    def json(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        status, headers, data = self.request(method, path, body)
        if not 200 <= status < 300:
            raise FixtureProtocolError(f"OpenCode rejected {method} {path} (HTTP {status})")
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise FixtureProtocolError("OpenCode response did not use JSON")
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FixtureProtocolError("OpenCode response was not JSON") from error
        if not isinstance(decoded, dict):
            raise FixtureProtocolError("OpenCode response was not a JSON object")
        return decoded

    def acknowledge(self, method: str, path: str, body: dict[str, object] | None = None) -> None:
        status, _, _ = self.request(method, path, body)
        if not 200 <= status < 300:
            raise FixtureProtocolError(f"OpenCode rejected {method} {path} (HTTP {status})")

    def events(self, after: str | None = None) -> tuple[list[dict[str, str]], str]:
        headers = {"Accept": "text/event-stream"}
        if after is not None:
            headers["Last-Event-ID"] = after
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=_REQUEST_TIMEOUT)
        try:
            connection.request(
                "GET", "/event", headers={**headers, "Authorization": self.authorization}
            )
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "").split(";", 1)[0]
            if response.status != 200 or content_type != "text/event-stream":
                raise FixtureProtocolError("OpenCode event endpoint is incompatible")
            total = 0
            deadline = time.monotonic() + _SSE_TIMEOUT
            fields: dict[str, list[str]] = {}
            events: list[dict[str, str]] = []
            while len(events) < 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FixtureProtocolError(
                        "OpenCode event stream exceeded the fixture deadline"
                    )
                try:
                    stream_socket = connection.sock
                    if stream_socket is None:
                        stream_socket = getattr(getattr(response.fp, "raw", None), "_sock", None)
                    if stream_socket is not None:
                        stream_socket.settimeout(min(_REQUEST_TIMEOUT, remaining))
                    line = response.readline(_MAX_SSE_BYTES + 1)
                except (OSError, http.client.HTTPException) as error:
                    raise FixtureProtocolError("OpenCode event stream was lost") from error
                total += len(line)
                if total > _MAX_SSE_BYTES or len(line) > _MAX_SSE_BYTES:
                    raise FixtureProtocolError("OpenCode event stream exceeds its bound")
                if not line:
                    break
                if line in {b"\n", b"\r\n"}:
                    if fields:
                        events.append({key: "\n".join(value) for key, value in fields.items()})
                    fields = {}
                    continue
                if line.startswith(b":"):
                    continue
                try:
                    name, value = line.rstrip(b"\r\n").decode("utf-8").split(":", 1)
                except (UnicodeDecodeError, ValueError) as error:
                    raise FixtureProtocolError("OpenCode event stream was malformed") from error
                if name not in {"id", "event", "data"}:
                    raise FixtureProtocolError("OpenCode event stream used an unsupported field")
                fields.setdefault(name, []).append(value.lstrip(" "))
            if not events:
                raise FixtureProtocolError("OpenCode event stream ended before an event")
            if any(not isinstance(event.get("id"), str) or not event["id"] for event in events):
                raise FixtureProtocolError("OpenCode event stream included an id-less event")
            cursor = events[-1]["id"]
            if len(cursor.encode("utf-8")) > 512:
                raise FixtureProtocolError("OpenCode event stream lacked a bounded replay cursor")
            return events, cursor
        finally:
            connection.close()


class _ServerProcess:
    def __init__(
        self, settings: FixtureSettings, workspace: Path, environment: dict[str, str], port: int
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._environment = environment
        self._port = port
        self.process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None
        self.starts = 0

    def start(self) -> None:
        if self.process is not None or self.starts:
            raise FixtureProtocolError("fixture refuses to create a duplicate OpenCode process")
        self.starts = 1
        self.process = subprocess.Popen(
            [
                str(self._settings.executable),
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(self._port),
            ],
            cwd=self._workspace,
            env=self._environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._process_group_id = self.process.pid

    def _group_is_alive(self) -> bool:
        if self._process_group_id is None:
            return False
        try:
            os.killpg(self._process_group_id, 0)
        except ProcessLookupError:
            return False
        return True

    def _wait_for_group_exit(self) -> bool:
        return self._wait_for_group_exit_before(time.monotonic() + _CLEANUP_TIMEOUT)

    def _wait_for_group_exit_before(self, deadline: float) -> bool:
        while self._group_is_alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def close(self) -> bool:
        if self.process is None:
            return True
        deadline = time.monotonic() + _CLEANUP_TIMEOUT
        term_deadline = deadline - (_CLEANUP_TIMEOUT / 2)
        if self._group_is_alive():
            try:
                os.killpg(self._process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=max(0, term_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        if self._group_is_alive():
            try:
                os.killpg(self._process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not self._wait_for_group_exit_before(deadline):
            return False
        return self.process.poll() is not None and not self._group_is_alive()


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _observed_version(settings: FixtureSettings) -> None:
    try:
        completed = subprocess.run(
            [str(settings.executable), "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env={"PATH": os.defpath, "HOME": tempfile.gettempdir(), "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FixtureUnavailable(
            "configured OpenCode executable could not report its version"
        ) from error
    text = completed.stdout.decode("utf-8", "replace").strip()
    candidates = re.findall(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", text)
    if completed.returncode != 0 or candidates != [settings.version]:
        raise FixtureUnavailable(
            "configured OpenCode executable version does not match its exact pin"
        )


def run_fixture(settings: FixtureSettings) -> FixtureResult:
    """Run exactly one real server probe, retaining unavailable as an honest result."""
    try:
        _observed_version(settings)
    except FixtureUnavailable as error:
        return FixtureResult(
            "unavailable",
            str(error),
            version=settings.version,
            schema_sha256=settings.schema_sha256,
        )
    server: _ServerProcess | None = None
    root: Path | None = None
    cleanup = False
    retained_temp_state: str | None = None
    result: FixtureResult | None = None
    try:
        try:
            root = Path(tempfile.mkdtemp(prefix="tessera-opencode-fixture-"))
            home, workspace = root / "home", root / "workspace"
            for directory in (
                home,
                workspace,
                home / "config-dir",
                home / "config",
                home / "data",
                home / "cache",
            ):
                _private_directory(directory)
            config = root / "opencode.json"
            config.write_bytes(_canonical_json(_fixture_config()))
            os.chmod(config, 0o600)
            username, password = "tessera-fixture", secrets.token_urlsafe(32)
            port = _reserve_port()
            environment = _safe_environment(home, config, username, password)
            server = _ServerProcess(settings, workspace, environment, port)
            server.start()
            client = _LoopbackClient(port, username, password)
            deadline = time.monotonic() + _START_TIMEOUT
            while True:
                try:
                    health = client.json("GET", "/global/health")
                    break
                except FixtureProtocolError:
                    if server.process is not None and server.process.poll() is not None:
                        raise FixtureProtocolError(
                            "OpenCode exited before becoming healthy"
                        ) from None
                    if time.monotonic() >= deadline:
                        raise FixtureProtocolError(
                            "OpenCode did not become healthy before the fixture deadline"
                        ) from None
                    time.sleep(0.1)
            unauthenticated, _, _ = client.request("GET", "/global/health", authenticated=False)
            if unauthenticated != 401:
                raise FixtureProtocolError(
                    "OpenCode did not enforce fixture-only HTTP basic authentication"
                )
            if health.get("healthy") is not True or health.get("version") != settings.version:
                raise FixtureProtocolError("OpenCode health version does not match its exact pin")
            upstream = OpenCodeHttpClient(
                f"http://127.0.0.1:{port}",
                client.authorization,
                OpenCodeServerPin(settings.version, settings.schema_sha256),
                _launcher_pin(),
                auth_mode="basic",
                timeout_seconds=_REQUEST_TIMEOUT,
            )
            try:
                upstream.verify_contract()
            except Exception as error:
                raise FixtureProtocolError(
                    "OpenCode /doc does not prove the selected real protocol"
                ) from error
            if upstream.capabilities is None or not upstream.capabilities.context_append:
                raise FixtureProtocolError("OpenCode /doc does not prove no-reply context append")
            session_id = upstream.create_session("compatibility")
            message_outcome = "context_appended_no_reply"
            journal = root / "journal"
            try:
                with LiveOpenCodeHarnessAdapter(
                    upstream,
                    journal,
                    authority_is_current=lambda *_: True,
                    context_only_delivery=True,
                    provider_disabled=True,
                ) as adapter:
                    binding = TesseraBinding(
                        "fixture-org",
                        "fixture-project",
                        "fixture-task",
                        "compatibility",
                        0,
                        "fixture-launcher",
                        "fixture-attempt",
                        "fixture-operation",
                        "fixture-attempt-authority",
                        session_id,
                    )
                    adapter.deliver_context_only(
                        binding, "fixture-delivery", "Fixture context only.", "d" * 64
                    )
            except Exception as error:
                raise FixtureProtocolError(
                    "OpenCode context delivery was not acknowledged"
                ) from error
            first_events = upstream.events(None)
            if _forbidden_activity([event.data for event in first_events]):
                raise FixtureProtocolError(
                    "OpenCode emitted provider, inference, tool, plugin, or MCP activity"
                )
            cursor = first_events[-1].cursor if first_events else None
            if cursor is None:
                raise FixtureProtocolError("OpenCode event stream ended before an event")
            if upstream.capabilities.replay is not True:
                raise FixtureProtocolError("OpenCode /doc does not prove event replay")
            second_events = upstream.events(cursor)
            if _forbidden_activity([event.data for event in second_events]):
                raise FixtureProtocolError(
                    "OpenCode emitted provider, inference, tool, plugin, or MCP activity"
                )
            replay_cursor = second_events[-1].cursor if second_events else cursor
            if replay_cursor == cursor or any(event.cursor == cursor for event in second_events):
                raise FixtureProtocolError("OpenCode did not replay beyond the supplied cursor")
            if not any(
                isinstance(event.data.get("properties"), dict)
                and event.data["properties"].get("sessionID") == session_id
                for event in (*first_events, *second_events)
            ):
                raise FixtureProtocolError("OpenCode events lacked created-session attribution")
            upstream.abort(session_id, "compatibility")
            result = FixtureResult(
                "passed",
                "real pinned OpenCode server exercised in provider-disabled configuration; "
                "no forbidden activity observed",
                settings.version,
                settings.schema_sha256,
                health_checked=True,
                auth_checked=True,
                session_created=True,
                message=message_outcome,
                sse_events=len(first_events) + len(second_events),
                reconnected=True,
                abort_acknowledged=True,
                transport_checked=True,
                reconnect_checked=True,
                cursor_replay_checked=True,
                session_attribution_checked=True,
                context_readback_checked=True,
                activity_checked=True,
            )
        finally:
            if server is not None:
                try:
                    cleanup = server.close()
                except (OSError, subprocess.SubprocessError):
                    cleanup = False
            if root is not None and (server is None or cleanup):
                shutil.rmtree(root)
            elif root is not None:
                retained_temp_state = str(root)
    except (FixtureProtocolError, OSError) as error:
        result = FixtureResult(
            "failed",
            str(error),
            settings.version,
            settings.schema_sha256,
        )
    if result is None:
        raise FixtureProtocolError("fixture did not produce a result")
    if server is not None and not cleanup:
        cleanup_reason = "OpenCode process-group cleanup was not confirmed"
        result = FixtureResult(
            "failed",
            cleanup_reason if result.status == "passed" else f"{result.reason}; {cleanup_reason}",
            result.version,
            result.schema_sha256,
            health_checked=result.health_checked,
            auth_checked=result.auth_checked,
            session_created=result.session_created,
            message=result.message,
            sse_events=result.sse_events,
            reconnected=result.reconnected,
            abort_acknowledged=result.abort_acknowledged,
            transport_checked=result.transport_checked,
            reconnect_checked=result.reconnect_checked,
            cursor_replay_checked=result.cursor_replay_checked,
            session_attribution_checked=result.session_attribution_checked,
            context_readback_checked=result.context_readback_checked,
            activity_checked=result.activity_checked,
            retained_temp_state=retained_temp_state,
        )
    return FixtureResult(
        result.status,
        result.reason,
        result.version,
        result.schema_sha256,
        health_checked=result.health_checked,
        auth_checked=result.auth_checked,
        session_created=result.session_created,
        message=result.message,
        sse_events=result.sse_events,
        reconnected=result.reconnected,
        abort_acknowledged=result.abort_acknowledged,
        cleanup_confirmed=cleanup,
        transport_checked=result.transport_checked,
        reconnect_checked=result.reconnect_checked,
        cursor_replay_checked=result.cursor_replay_checked,
        session_attribution_checked=result.session_attribution_checked,
        context_readback_checked=result.context_readback_checked,
        activity_checked=result.activity_checked,
        retained_temp_state=retained_temp_state,
    )


def main() -> int:
    try:
        settings = FixtureSettings.from_environment()
    except FixtureSkipped as error:
        print(FixtureResult("skipped", str(error)).json())
        return fixture_exit_code(FixtureResult("skipped", str(error)))
    except FixtureUnavailable as error:
        print(FixtureResult("unavailable", str(error)).json())
        return fixture_exit_code(FixtureResult("unavailable", str(error)))
    result = run_fixture(settings)
    print(result.json())
    return fixture_exit_code(result)


if __name__ == "__main__":  # pragma: no cover - exercised by the script entry point
    raise SystemExit(main())
