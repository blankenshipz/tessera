"""Real SDK routes via isolated TestClient; control/model metadata are synthetic."""

import json

import httpx
from starlette.testclient import TestClient
from test_a2a_generated_peer import sdk  # noqa: F401
from test_a2a_generated_store import pins
from test_a2a_peer import envelope

from tessera_agent.research_peer import ResearchPeer
from tessera_control.a2a_peer import create_peer


def test_actual_sdk_protocol_client_keeps_task_and_message_ids(request):
    state, config, authority, _ = request.getfixturevalue("sdk")
    generated = pins()
    wire = envelope()
    wire["authority"]["binding"]["task_id"] = "central-task"
    wire.update(reference=generated["artifact_id"], generated=generated)
    wire.pop("operation")
    with TestClient(create_peer(config, authority)) as sdk_client:

        def route(request):
            response = sdk_client.post(
                "/", headers=dict(request.headers), json=json.loads(request.content)
            )
            return httpx.Response(response.status_code, json=response.json())

        transport = httpx.MockTransport(route)
        critic = ResearchPeer("http://127.0.0.1:8771", "critic", transport=transport)
        planner = ResearchPeer("http://127.0.0.1:8771", "planner", transport=transport)
        task = critic.send("submit", wire, "stable-submit")
        result = critic.send("result", wire, "stable-result", task_id=task["id"])
        assert result["id"] == task["id"]
        assert planner.read(task["id"])["id"] == task["id"]
        ack = planner.send("consume", wire, "stable-ack", task_id=task["id"])
        assert ack == planner.send("consume", wire, "stable-ack", task_id=task["id"])
        assert ack["status"]["state"] == "TASK_STATE_COMPLETED"
        critic.close()
        planner.close()


def test_two_independent_loopback_sdk_servers_keep_distinct_durable_tasks(request, tmp_path):
    """Real HTTP/SDK/SQLite peers; fixture control registry/principals/model are synthetic."""
    import socket
    import threading
    import time

    import uvicorn

    _, config, authority, _ = request.getfixturevalue("sdk")
    generated, wire = pins(), envelope()
    wire["authority"]["binding"]["task_id"] = "central-task"
    wire.update(reference=generated["artifact_id"], generated=generated)
    wire.pop("operation")
    servers, clients, tasks = [], [], []
    try:
        for index in range(2):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            cfg = {
                **config,
                "public_url": origin + "/",
                "database": str(tmp_path / f"independent-{index}.sqlite"),
            }
            server = uvicorn.Server(
                uvicorn.Config(create_peer(cfg, authority), log_level="critical", lifespan="off")
            )
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            servers.append((server, thread, sock))
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            critic, planner = ResearchPeer(origin, "critic"), ResearchPeer(origin, "planner")
            clients.extend([critic, planner])
            task = critic.send("submit", wire, "stable-submit")
            critic.send("result", wire, "stable-result", task_id=task["id"])
            assert planner.read(task["id"])["id"] == task["id"]
            ack = planner.send("consume", wire, "stable-ack", task_id=task["id"])
            assert ack["status"]["state"] == "TASK_STATE_COMPLETED"
            tasks.append(task["id"])
        assert tasks[0] != tasks[1]
    finally:
        for client in clients:
            client.close()
        for server, thread, sock in servers:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
            assert not thread.is_alive()
