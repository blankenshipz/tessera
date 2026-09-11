"""Opt-in, provider-disabled compatibility probe for a pinned OpenCode binary.

This is deliberately separate from the harness adapter.  It starts one configured
local binary only, and reports unavailable rather than substituting the synthetic
adapter when that binary or its requested version is absent.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

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

    def events(self, after: str | None = None) -> tuple[list[dict[str, str]], str | None]:
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
            return events, events[-1].get("id")
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
        deadline = time.monotonic() + _CLEANUP_TIMEOUT
        while self._group_is_alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def close(self) -> bool:
        if self.process is None:
            return True
        if self._group_is_alive():
            os.killpg(self._process_group_id, signal.SIGTERM)
        try:
            self.process.wait(timeout=_CLEANUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False
        if not self._wait_for_group_exit():
            os.killpg(self._process_group_id, signal.SIGKILL)
            if not self._wait_for_group_exit():
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
    cleanup = False
    result: FixtureResult | None = None
    try:
        temporary = tempfile.TemporaryDirectory(prefix="tessera-opencode-fixture-")
        try:
            root_text = temporary.name
            root = Path(root_text)
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
            document = client.json("GET", "/doc")
            info = document.get("info")
            schema_matches = (
                hashlib.sha256(_canonical_json(document)).hexdigest() == settings.schema_sha256
            )
            version_matches = isinstance(info, dict) and info.get("version") == settings.version
            if not version_matches or not schema_matches:
                raise FixtureProtocolError(
                    "OpenCode /doc schema digest does not match its exact pin"
                )
            session = client.json("POST", "/session", {"title": "tessera-compatibility-fixture"})
            session_id = session.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise FixtureProtocolError("OpenCode session response lacked an id")
            message_outcome = "accepted_no_provider_no_tools"
            message_path = f"/session/{quote(session_id, safe='')}/message"
            message_status, message_headers, message_body = client.request(
                "POST",
                message_path,
                {
                    "noReply": True,
                    "parts": [
                        {
                            "type": "text",
                            "text": "Fixture context only; no model, provider, or tool use.",
                        }
                    ],
                    "tools": {},
                },
            )
            if not 200 <= message_status < 300:
                if _is_provider_disabled_response(message_status, message_headers, message_body):
                    message_outcome = "provider_disabled"
                else:
                    raise FixtureProtocolError(
                        f"OpenCode rejected POST {message_path} (HTTP {message_status})"
                    )
            first_events, cursor = client.events()
            second_events, _ = client.events(cursor)
            client.acknowledge("POST", f"/session/{quote(session_id, safe='')}/abort", {})
            result = FixtureResult(
                "passed",
                "real pinned OpenCode server exercised with providers and tools disabled",
                settings.version,
                settings.schema_sha256,
                health_checked=True,
                auth_checked=True,
                session_created=True,
                message=message_outcome,
                sse_events=len(first_events) + len(second_events),
                reconnected=True,
                abort_acknowledged=True,
            )
        finally:
            if server is not None:
                try:
                    cleanup = server.close()
                except (OSError, subprocess.SubprocessError):
                    cleanup = False
            temporary.cleanup()
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
