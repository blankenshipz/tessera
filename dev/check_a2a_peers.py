"""Opt-in two-process official SDK exchange against real delegated control grants.

No inference, grant creation or external deployment. Configs and envelopes must
reference existing authorized grants and environment-held peer/agent credentials.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from google.protobuf.json_format import ParseDict


def launch(path, config):
    url = urlsplit(config["public_url"])
    if url.hostname != "127.0.0.1" or not url.port:
        raise ValueError("harness launches only explicitly configured loopback peers")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tessera_control.a2a_peer",
            "--config",
            str(path),
            "--port",
            str(url.port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def connect(config):
    http = httpx.AsyncClient(
        headers={"Authorization": "Bearer " + os.environ[config["incoming_test_token_env"]]},
        timeout=10,
        follow_redirects=False,
        trust_env=False,
    )
    try:
        for attempt in range(50):
            try:
                readiness = await http.get(
                    config["public_url"].rstrip("/") + "/.well-known/agent-card.json"
                )
                readiness.raise_for_status()
                card = await A2ACardResolver(http, config["public_url"]).get_agent_card()
                if card.name != config["agent_id"]:
                    raise ValueError("port is owned by a different peer")
                return (
                    http,
                    ClientFactory(
                        ClientConfig(
                            streaming=False,
                            httpx_client=http,
                            supported_protocol_bindings=["JSONRPC"],
                        )
                    ).create(card),
                    card,
                )
            except httpx.ConnectError:
                if attempt == 49:
                    raise
                await asyncio.sleep(0.1)
    except BaseException:
        await http.aclose()
        raise


async def send(client, envelope, message_id=None, task_id=""):
    request = ParseDict(
        {
            "message": {
                "messageId": message_id or str(uuid.uuid4()),
                "role": "ROLE_USER",
                "taskId": task_id,
                "parts": [{"data": envelope}],
            }
        },
        pb.SendMessageRequest(),
    )
    responses = [response async for response in client.send_message(request)]
    assert len(responses) == 1 and responses[0].HasField("task")
    return responses[0].task


async def denied(awaitable):
    try:
        await awaitable
    except Exception as exc:
        # Require a protocol denial, not connectivity/timeout failure.
        from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

        if not isinstance(exc, (InvalidParamsError, TaskNotFoundError)):
            raise
        return
    raise AssertionError("expected protocol denial")


async def run(paths, authority_fence_check=None):
    configs = [json.loads(path.read_text()) for path in paths]
    processes, clients = [], []
    try:
        for path, config in zip(paths, configs, strict=True):
            processes.append(launch(path, config))
        for config in configs:
            clients.append(await connect(config))
        tasks = []
        for config, (http, client, card) in zip(configs, clients, strict=True):
            assert card.supported_interfaces[0].protocol_version == "1.0"
            envelope = config["test_envelope"]
            message_id = str(uuid.uuid4())
            task = await send(client, envelope, message_id)
            assert task.status.state == pb.TASK_STATE_INPUT_REQUIRED
            replay = await send(client, envelope, message_id)
            assert replay.id == task.id
            changed = {**envelope, "reference": "different-reference"}
            await denied(send(client, changed, message_id))
            denied_authority = json.loads(json.dumps(envelope))
            denied_authority["authority"]["epoch"] += 1
            await denied(send(client, denied_authority))
            fetched = await client.get_task(pb.GetTaskRequest(id=task.id, history_length=0))
            assert fetched.id == task.id and not fetched.history
            result = await send(
                client,
                {**envelope, "operation": "result", "reference": "artifact-local-protocol-proof"},
                task_id=task.id,
            )
            assert result.status.state == pb.TASK_STATE_COMPLETED and result.artifacts
            pending = await send(client, envelope)
            tasks.append((task, pending, envelope, message_id))
            unauthorized = await http.post(
                config["public_url"], headers={"Authorization": "Bearer invalid"}, json={}
            )
            assert unauthorized.status_code == 401
        # A task from one private ledger must not be observable on the other.
        await denied(clients[1][1].get_task(pb.GetTaskRequest(id=tasks[0][0].id)))
        for process in processes:
            stop(process)
        for http, _, _ in clients:
            await http.aclose()
        clients.clear()
        processes = [launch(path, config) for path, config in zip(paths, configs, strict=True)]
        for config in configs:
            clients.append(await connect(config))
        for (_, client, _), (completed, pending, envelope, message_id) in zip(
            clients, tasks, strict=True
        ):
            assert (
                await client.get_task(pb.GetTaskRequest(id=completed.id))
            ).status.state == pb.TASK_STATE_COMPLETED
            assert (await send(client, envelope, message_id)).id == completed.id
            canceled = await client.cancel_task(pb.CancelTaskRequest(id=pending.id))
            assert canceled.status.state == pb.TASK_STATE_CANCELED
            assert (
                await client.cancel_task(pb.CancelTaskRequest(id=pending.id))
            ).status.state == pb.TASK_STATE_CANCELED
            await denied(send(client, {**envelope, "operation": "result"}, task_id=pending.id))
        if authority_fence_check:
            await authority_fence_check(configs, clients, tasks)
        return {
            "sdk": "a2a-sdk==1.2.1",
            "protocol": "1.0.0",
            "binding": "JSONRPC",
            "peer_processes": 2,
            "checks": [
                "discovery",
                "send/get/result",
                "restart/replay",
                "cancel/late-result-denial",
                "stale-authority-denial",
                "authentication",
                "cross-peer-task-denial",
            ],
            "reasoning": "not exercised",
            "independent_host": False,
        }
    finally:
        for http, _, _ in clients:
            await http.aclose()
        for process in processes:
            stop(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--peer-a-config", type=Path)
    parser.add_argument("--peer-b-config", type=Path)
    args = parser.parse_args()
    if not args.run:
        print(
            json.dumps(
                {
                    "status": "not-run",
                    "required": (
                        "two private peer configs, real delegated grants, "
                        "environment credential references"
                    ),
                }
            )
        )
        return
    if not args.peer_a_config or not args.peer_b_config:
        parser.error("--run requires both peer configs")
    print(json.dumps(asyncio.run(run([args.peer_a_config, args.peer_b_config])), sort_keys=True))


if __name__ == "__main__":
    main()
