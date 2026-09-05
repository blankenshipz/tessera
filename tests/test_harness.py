from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tessera_worker.harness import AppServerTransport, CodexSession, HarnessError, HarnessEvent

FAKE_SERVER = """
import json, sys
from pathlib import Path
log = Path(sys.argv[1])
mode = sys.argv[2]
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    with log.open("a") as out:
        out.write(json.dumps(message) + "\\n")
    method = message.get("method")
    if method is None or "id" not in message:
        continue
    if mode == "silent":
        continue
    if mode == "malformed":
        print("not-json", flush=True)
        continue
    result = {}
    if method == "initialize":
        result = {"userAgent": "fixture/0.153.4"}
    elif method == "thread/start":
        result = {"thread": {"id": "thread-1"}}
    elif method == "turn/start":
        result = {"turn": {"id": "turn-1"}}
        if mode == "approval":
            send({"id": "approve-1", "method": "item/commandExecution/requestApproval",
                  "params": {"command": "do not run"}})
    elif method == "turn/interrupt":
        send({"method": "turn/completed", "params": {"threadId": "thread-1",
              "turn": {"id": "turn-1", "status": "interrupted"}}})
    send({"id": message["id"], "result": result})
    if mode == "flood" and method == "initialize":
        for n in range(10):
            send({"method": "item/delta", "params": {"n": n}})
"""


def transport(tmp_path: Path, mode: str = "normal", **kwargs: object) -> AppServerTransport:
    script = tmp_path / "server.py"
    script.write_text(FAKE_SERVER)
    return AppServerTransport(
        [sys.executable, str(script), str(tmp_path / "wire.jsonl"), mode],
        cwd=tmp_path,
        environment={},
        **kwargs,
    )


def test_structured_turn_steer_and_interrupt_do_not_claim_provider_stop(tmp_path: Path) -> None:
    async def run() -> None:
        rpc = transport(tmp_path)
        await rpc.start()
        try:
            session = CodexSession(rpc)
            assert await session.open_thread(tmp_path) == "thread-1"
            assert await session.start_turn("Synthetic fixture") == "turn-1"
            with pytest.raises(HarnessError, match="already active"):
                await session.start_turn("Duplicate")
            await session.steer("Synthetic steer")
            result = await session.interrupt()
            assert result == {"interrupt_delivered": True, "provider_termination_confirmed": False}
            assert session.turn_id == "turn-1"
            assert session.observe(await rpc.next_event())
            assert session.turn_id is None
        finally:
            await rpc.close()
        assert rpc.process is not None and rpc.process.returncode is not None

    asyncio.run(run())
    messages = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert messages[0]["params"]["capabilities"] == {"experimentalApi": False}
    thread = next(m for m in messages if m.get("method") == "thread/start")
    assert thread["params"]["sandbox"] == "read-only"
    assert thread["params"]["approvalPolicy"] == "never"
    steer = next(m for m in messages if m.get("method") == "turn/steer")
    assert steer["params"]["expectedTurnId"] == "turn-1"


def test_privileged_server_requests_are_denied(tmp_path: Path) -> None:
    async def run() -> None:
        rpc = transport(tmp_path, "approval")
        await rpc.start()
        try:
            session = CodexSession(rpc)
            await session.open_thread(tmp_path)
            await session.start_turn("Fixture")
            event = await rpc.next_event()
            assert event.method == "tessera/approvalDenied"
            # An acknowledged read ensures the fake server consumed our denial.
            await rpc.request("account/read", {})
            with pytest.raises(HarnessError, match="outside"):
                await rpc.request("command/exec", {"command": ["echo", "unsafe"]})
        finally:
            await rpc.close()

    asyncio.run(run())
    messages = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert next(m for m in messages if m.get("id") == "approve-1")["result"] == {
        "decision": "decline"
    }


@pytest.mark.parametrize("mode", ["silent", "malformed"])
def test_protocol_failure_closes_process_without_retry(tmp_path: Path, mode: str) -> None:
    async def run() -> None:
        rpc = transport(tmp_path, mode, timeout_seconds=0.5)
        with pytest.raises(HarnessError):
            await rpc.start()
        assert rpc.process is not None and rpc.process.returncode is not None

    asyncio.run(run())
    assert len((tmp_path / "wire.jsonl").read_text().splitlines()) == 1


def test_event_saturation_stops_unattended_harness(tmp_path: Path) -> None:
    async def run() -> None:
        rpc = transport(tmp_path, "flood", event_capacity=1)
        try:
            with pytest.raises(HarnessError):
                await rpc.start()
                await rpc.request("account/read", {})
        finally:
            await rpc.close()
        assert rpc.process is not None and rpc.process.returncode is not None

    asyncio.run(run())


def test_stale_terminal_event_cannot_clear_active_turn(tmp_path: Path) -> None:
    session = CodexSession(transport(tmp_path))
    session.thread_id = "current-thread"
    session.turn_id = "current-turn"
    assert not session.observe(
        HarnessEvent(
            "turn/completed",
            {
                "threadId": "other-thread",
                "turn": {"id": "current-turn", "status": "completed"},
            },
        )
    )
    assert session.turn_id == "current-turn"
