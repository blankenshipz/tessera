"""Bounded Codex App Server transport, without implicit execution authorization.

The supervisor must authorize and journal a turn before calling this adapter.
No harness event is a provider termination receipt or a scientific conclusion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CODEX_VERSION = "0.153.4"
MAX_MESSAGE_BYTES = 1024 * 1024
SAFE_METHODS = frozenset(
    {
        "initialize",
        "thread/start",
        "turn/start",
        "turn/steer",
        "turn/interrupt",
        "account/read",
        "account/rateLimits/read",
        "account/usage/read",
    }
)
APPROVAL_METHODS = frozenset(
    {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
)


class HarnessError(Exception):
    """The protocol cannot establish the requested effect."""


@dataclass(frozen=True)
class HarnessEvent:
    method: str
    params: dict[str, Any]


class AppServerTransport:
    """One private stdio connection, with bounded requests and event backpressure.

    Commands are configured by the trusted supervisor, never supplied by a model
    or HTTP caller. All server-initiated privileged requests are denied. A later
    decision bridge may replace denial only with a persisted scoped human decision.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float = 15,
        event_capacity: int = 256,
    ):
        if not command or timeout_seconds <= 0 or event_capacity < 1:
            raise ValueError("Invalid transport bounds.")
        self.command = tuple(command)
        self.cwd = cwd
        self.environment = dict(environment)
        self.timeout = timeout_seconds
        self.events: asyncio.Queue[HarnessEvent] = asyncio.Queue(maxsize=event_capacity)
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._sequence = 0
        self._write_lock = asyncio.Lock()
        self._failure: HarnessError | None = None
        self._closed = False

    async def start(self) -> dict[str, Any]:
        if self.process is not None or self._closed:
            raise HarnessError("Transport cannot be started twice.")
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Diagnostics can contain prompts/credentials. Do not persist them.
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_MESSAGE_BYTES,
        )
        self._reader = asyncio.create_task(self._read())
        try:
            result = await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "tessera", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": False},
                },
            )
            await self._send({"method": "initialized", "params": {}})
            return result
        except BaseException:
            await self.close()
            raise

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in SAFE_METHODS:
            raise HarnessError("Method is outside the supported harness contract.")
        if self._closed or self._failure or self.process is None:
            raise HarnessError("Harness connection is unavailable.")
        if len(self._pending) >= 32:
            raise HarnessError("Too many pending harness requests.")
        self._sequence += 1
        identifier = self._sequence
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        try:
            await self._send({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError as error:
            # A timed out submit is uncertain. Never resend it with a fresh id.
            await self.close()
            raise HarnessError("Harness reply timed out; effect is unknown.") from error
        finally:
            self._pending.pop(identifier, None)

    async def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(data) > MAX_MESSAGE_BYTES:
            raise HarnessError("Harness message exceeds the configured limit.")
        async with self._write_lock:
            if self.process is None or self.process.stdin is None:
                raise HarnessError("Harness stdin is unavailable.")
            self.process.stdin.write(data)
            try:
                await asyncio.wait_for(self.process.stdin.drain(), timeout=self.timeout)
            except (BrokenPipeError, ConnectionResetError, TimeoutError) as error:
                raise HarnessError("Harness write failed; effect is unknown.") from error

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise HarnessError("Harness disconnected; effects require reconciliation.")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise HarnessError("Malformed harness message.")
                if "method" in message:
                    method = message["method"]
                    params = message.get("params", {})
                    if not isinstance(method, str) or not isinstance(params, dict):
                        raise HarnessError("Malformed harness notification.")
                    if "id" in message:
                        # Never grant session-wide or persistent authority.
                        if method in APPROVAL_METHODS:
                            await self._send(
                                {"id": message["id"], "result": {"decision": "decline"}}
                            )
                        else:
                            await self._send(
                                {
                                    "id": message["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "Unsupported privileged request.",
                                    },
                                }
                            )
                        event = HarnessEvent("tessera/approvalDenied", {"method": method})
                    else:
                        event = HarnessEvent(method, params)
                    try:
                        self.events.put_nowait(event)
                    except asyncio.QueueFull as error:
                        raise HarnessError(
                            "Harness event queue exhausted; reconciliation required."
                        ) from error
                else:
                    identifier = message.get("id")
                    if type(identifier) is not int or identifier not in self._pending:
                        raise HarnessError("Unmatched harness reply.")
                    future = self._pending[identifier]
                    if future.done():
                        raise HarnessError("Duplicate harness reply.")
                    if "error" in message:
                        # Avoid echoing server diagnostics which may contain secrets.
                        future.set_exception(HarnessError("Harness rejected the request."))
                    elif isinstance(message.get("result"), dict):
                        future.set_result(message["result"])
                    else:
                        raise HarnessError("Malformed harness result.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failure = (
                error
                if isinstance(error, HarnessError)
                else HarnessError("Invalid harness stream.")
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(self._failure)
            # A broken or saturated stream cannot safely continue unattended.
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.terminate()

    async def next_event(self) -> HarnessEvent:
        if self._failure and self.events.empty():
            raise self._failure
        try:
            return await asyncio.wait_for(self.events.get(), timeout=self.timeout)
        except TimeoutError as error:
            raise HarnessError("No harness event before the deadline.") from error

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process is not None:
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        self.process.kill()
                    await self.process.wait()
            if self._reader is not None:
                self._reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._reader
        for future in self._pending.values():
            if not future.done():
                future.cancel()


class CodexSession:
    """Stable protocol operations for a single supervisor-owned thread.

    Real dispatch must remain disabled until the supervisor proves all profile
    limits. In particular, read-only command sandboxing is not model-egress control.
    """

    def __init__(self, transport: AppServerTransport):
        self.transport = transport
        self.thread_id: str | None = None
        self.turn_id: str | None = None

    async def open_thread(self, cwd: Path) -> str:
        if self.thread_id is not None:
            raise HarnessError("Thread already selected.")
        result = await self.transport.request(
            "thread/start",
            {
                "cwd": str(cwd),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "ephemeral": True,
            },
        )
        self.thread_id = result["thread"]["id"]
        return self.thread_id

    async def start_turn(self, text: str) -> str:
        if not self.thread_id or self.turn_id:
            raise HarnessError("Thread unavailable or turn already active.")
        result = await self.transport.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": "never",
            },
        )
        self.turn_id = result["turn"]["id"]
        return self.turn_id

    async def steer(self, text: str) -> None:
        if not self.thread_id or not self.turn_id:
            raise HarnessError("No active turn to steer.")
        await self.transport.request(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": self.turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )

    async def interrupt(self) -> dict[str, Any]:
        if not self.thread_id or not self.turn_id:
            raise HarnessError("No active turn to interrupt.")
        await self.transport.request(
            "turn/interrupt",
            {
                "threadId": self.thread_id,
                "turnId": self.turn_id,
            },
        )
        # Keep the active turn until a matching terminal notification is observed.
        return {"interrupt_delivered": True, "provider_termination_confirmed": False}

    def observe(self, event: HarnessEvent) -> bool:
        if event.method != "turn/completed" or event.params.get("threadId") != self.thread_id:
            return False
        turn = event.params.get("turn", {})
        if not isinstance(turn, dict) or turn.get("id") != self.turn_id:
            return False
        if turn.get("status") not in {"completed", "interrupted", "failed"}:
            raise HarnessError("Unknown terminal turn status.")
        self.turn_id = None
        return True
