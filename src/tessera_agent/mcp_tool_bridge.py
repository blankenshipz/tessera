"""Synchronous bridge from the Anthropic Messages tool loop to Tessera's own MCP server.

The bridge launches ``python -m tessera_mcp.server`` over stdio exactly as the OpenCode
harness binding does: the same module, the same runtime references and the agent token
passed by file path only. The child environment holds ONLY those references (plus
``PATH`` and ``PYTHONNOUSERSITE=1``); no model-provider credential or other parent
variable is inherited. The control API re-checks the credential's scopes on every call.

Deny by default: only names in ``allowed`` are offered to the model or executed. A call
to any other name returns an error result without contacting the server. The bridge never
logs tool arguments, results or the token, and every failure is a short code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import anyio
from mcp import ClientSession, types
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage

_ALLOWED_ENV_KEYS = (
    "TESSERA_CONTROL_URL",
    "TESSERA_AGENT_TOKEN_FILE",
    "TESSERA_PROJECT_ID",
    "TESSERA_AGENT_DESTINATION",
    "TESSERA_CONTROL_CA_FILE",
    "PATH",
    "PYTHONNOUSERSITE",
)
_MAX_LINE_BYTES = 16 * 1024 * 1024
_MAX_LIST_PAGES = 64
_TERMINATE_GRACE = 2.0
_NOT_PERMITTED = "tool not permitted"
_TRUNCATED_MARKER = "\n[truncated]"
# JSON-RPC error codes that mean "no definite answer", not "the server refused".
_UNCERTAIN_CODES = frozenset({types.CONNECTION_CLOSED, -32001, 408})


class ToolBridgeBlocked(Exception):
    """A bridge failure whose ``str()`` is only a stable code, never content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


def _tool_def(tool: dict) -> dict:
    if not isinstance(tool, dict):
        raise ToolBridgeBlocked("tool_catalog_invalid")
    name, description, schema = (
        tool.get("name"),
        tool.get("description"),
        tool.get("input_schema"),
    )
    if not isinstance(name, str) or not name or not isinstance(description, str):
        raise ToolBridgeBlocked("tool_catalog_invalid")
    if not isinstance(schema, dict):
        raise ToolBridgeBlocked("tool_catalog_invalid")
    return {"name": name, "description": description, "input_schema": schema}


def catalog_sha256(tools: list[dict]) -> str:
    """Pin the offered tool list: canonical JSON of ``{name, description, input_schema}``.

    Sorted by name so server listing order does not matter; other keys are ignored.
    Duplicate names or malformed entries block with ``tool_catalog_invalid``.
    """
    if not isinstance(tools, list):
        raise ToolBridgeBlocked("tool_catalog_invalid")
    defs = sorted((_tool_def(tool) for tool in tools), key=lambda tool: tool["name"])
    if len({tool["name"] for tool in defs}) != len(defs):
        raise ToolBridgeBlocked("tool_catalog_invalid")
    canonical = json.dumps(defs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class McpToolBridge:
    """Sync facade over one ``mcp`` client session running on a private event-loop thread."""

    def __init__(
        self,
        *,
        allowed: frozenset[str],
        control_url: str,
        project_id: str,
        destination: str,
        token_file: Path,
        ca_file: str | None = None,
        python: str = sys.executable,
        timeout: float = 30.0,
        max_result_bytes: int = 65536,
        server_command=None,
        startup_timeout: float = 30.0,
    ):
        if not isinstance(allowed, frozenset) or not all(
            isinstance(name, str) and name for name in allowed
        ):
            raise ValueError("allowed must be a frozenset of tool names")
        if isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int):
            raise ValueError("max_result_bytes must be an int")
        if max_result_bytes < 1 or not timeout > 0 or not startup_timeout > 0:
            raise ValueError("limits must be positive")
        self.allowed = allowed
        self._control_url = control_url
        self._project_id = project_id
        self._destination = destination
        self._token_file = Path(token_file)
        self._ca_file = ca_file
        self._python = python
        self._timeout = float(timeout)
        self._startup_timeout = float(startup_timeout)
        self._max_result_bytes = max_result_bytes
        self._server_command = list(server_command) if server_command is not None else None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._session: ClientSession | None = None
        self._process = None
        self._catalog: list[dict] | None = None
        self._closed = False
        self.last_truncated = False

    # --- launch -------------------------------------------------------------------------

    def command(self) -> list[str]:
        """The server argv: the same module the OpenCode harness binding launches."""
        if self._server_command is not None:
            return list(self._server_command)
        return [self._python, "-m", "tessera_mcp.server"]

    def environment(self) -> dict[str, str]:
        """The complete child environment: runtime references only, token by path."""
        env = {
            "TESSERA_CONTROL_URL": self._control_url,
            "TESSERA_AGENT_TOKEN_FILE": str(self._token_file),
            "TESSERA_PROJECT_ID": self._project_id,
            "TESSERA_AGENT_DESTINATION": self._destination,
            "PATH": os.environ.get("PATH") or os.defpath,
            "PYTHONNOUSERSITE": "1",
        }
        if self._ca_file is not None:
            env["TESSERA_CONTROL_CA_FILE"] = str(self._ca_file)
        return env

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                raise ToolBridgeBlocked("tool_bridge_already_started")
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name="mcp-tool-bridge", daemon=True
            )
            self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._open(), self._loop)
        try:
            future.result(timeout=self._startup_timeout)
        except BaseException:
            future.cancel()
            self.close()
            raise ToolBridgeBlocked("tool_server_unavailable") from None

    async def _open(self) -> None:
        self._stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        self._runner = asyncio.get_running_loop().create_task(self._run(ready))
        await ready

    async def _run(self, ready: asyncio.Future) -> None:
        try:
            async with self._stdio() as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    catalog = await self._list_tools(session)
                    self._session = session
                    self._catalog = catalog
                    if not ready.done():
                        ready.set_result(None)
                    await self._stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(ToolBridgeBlocked("tool_server_unavailable"))
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            self._session = None
            if not ready.done():
                ready.set_exception(ToolBridgeBlocked("tool_server_unavailable"))

    @staticmethod
    async def _list_tools(session: ClientSession) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(_MAX_LIST_PAGES):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await session.list_tools(params=params)
            for tool in result.tools:
                tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": dict(tool.inputSchema or {"type": "object"}),
                    }
                )
            cursor = result.nextCursor
            if not cursor:
                return tools
        raise ToolBridgeBlocked("tool_catalog_invalid")

    @asynccontextmanager
    async def _stdio(self):
        """Minimal stdio transport with an exact environment.

        ``mcp.client.stdio.stdio_client`` always merges HOME/USER/SHELL/TERM into the child
        environment; the bridge must pass only the listed references, so it spawns the
        process itself. Server stderr is discarded so nothing it prints reaches host logs.
        """
        process = await anyio.open_process(
            self.command(),
            env=self.environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            # A fixed root working directory: ``python -m`` puts the cwd first on
            # sys.path, so an inherited directory could shadow tessera_mcp.
            cwd="/",
        )
        self._process = process
        read_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_reader = anyio.create_memory_object_stream(0)

        async def stdout_reader():
            buffer = b""
            try:
                async with read_writer:
                    async for chunk in process.stdout:
                        buffer += chunk
                        if len(buffer) > _MAX_LINE_BYTES:
                            return
                        *lines, buffer = buffer.split(b"\n")
                        for line in lines:
                            if not line.strip():
                                continue
                            try:
                                message = types.JSONRPCMessage.model_validate_json(line)
                            except Exception:
                                continue
                            await read_writer.send(SessionMessage(message))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass

        async def stdin_writer():
            try:
                async with write_reader:
                    async for session_message in write_reader:
                        data = session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        )
                        await process.stdin.send((data + "\n").encode("utf-8"))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(stdout_reader)
                group.start_soon(stdin_writer)
                try:
                    yield read_stream, write_stream
                finally:
                    group.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await self._reap(process)
                for stream in (read_stream, write_stream, read_writer, write_reader):
                    await stream.aclose()

    @staticmethod
    async def _reap(process) -> None:
        with suppress(Exception):
            await process.stdin.aclose()
        with anyio.move_on_after(_TERMINATE_GRACE):
            await process.wait()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            with anyio.move_on_after(_TERMINATE_GRACE):
                await process.wait()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    # --- tools ----------------------------------------------------------------------------

    def tools(self) -> list[dict]:
        """Only allowed tools, as Anthropic tool definitions, sorted by name."""
        catalog = self._catalog
        if catalog is None:
            raise ToolBridgeBlocked("tool_bridge_not_started")
        by_name: dict[str, dict] = {}
        for tool in catalog:
            if tool["name"] in by_name:
                raise ToolBridgeBlocked("tool_catalog_changed")
            by_name[tool["name"]] = tool
        if not self.allowed <= by_name.keys():
            raise ToolBridgeBlocked("tool_catalog_changed")
        return [
            {
                "name": name,
                "description": by_name[name]["description"],
                "input_schema": json.loads(json.dumps(by_name[name]["input_schema"])),
            }
            for name in sorted(self.allowed)
        ]

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Run one allowed tool. Returns ``(result_text, is_error)``.

        A name outside ``allowed`` returns ``("tool not permitted", True)`` without
        contacting the server. ``last_truncated`` reports whether the text was cut to
        ``max_result_bytes``. A timeout or transport failure leaves the outcome unknown
        and raises ``ToolBridgeBlocked("tool_call_uncertain")``.
        """
        self.last_truncated = False
        if not isinstance(name, str) or name not in self.allowed:
            return _NOT_PERMITTED, True
        if not isinstance(arguments, dict):
            return "invalid tool arguments", True
        loop, session = self._loop, self._session
        if loop is None or session is None or self._closed:
            raise ToolBridgeBlocked("tool_call_uncertain")
        future = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(session.call_tool(name, arguments), self._timeout), loop
        )
        try:
            result = future.result(timeout=self._timeout + 1.0)
        except McpError as exc:
            if exc.error.code in _UNCERTAIN_CODES:
                raise ToolBridgeBlocked("tool_call_uncertain") from None
            return self._bound("tool call rejected by server"), True
        except BaseException:
            future.cancel()
            raise ToolBridgeBlocked("tool_call_uncertain") from None
        parts = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append("[non-text content omitted]")
        return self._bound("\n".join(parts)), bool(result.isError)

    def _bound(self, text: str) -> str:
        data = text.encode("utf-8")
        limit = self._max_result_bytes
        if len(data) <= limit:
            return text
        self.last_truncated = True
        marker = _TRUNCATED_MARKER.encode("utf-8")
        if limit > len(marker):
            return data[: limit - len(marker)].decode("utf-8", "ignore") + _TRUNCATED_MARKER
        return data[:limit].decode("utf-8", "ignore")

    # --- shutdown -------------------------------------------------------------------------

    def close(self) -> None:
        """Stop the session, reap the child (stdin close, then TERM, then KILL), end the loop."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        runner = self._runner

        async def shutdown():
            if self._stop is not None:
                self._stop.set()
            if runner is not None:
                with suppress(BaseException):
                    await asyncio.wait_for(runner, timeout=3 * _TERMINATE_GRACE + 2.0)

        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
            with suppress(BaseException):
                future.result(timeout=3 * _TERMINATE_GRACE + 5.0)
        process = self._process
        if process is not None and process.returncode is None:
            with suppress(Exception):
                os.killpg(process.pid, 9)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
        if not thread.is_alive():
            loop.close()
        self._session = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
