"""Real two-process SDK/reference peer + real loopback collector; no inference."""

import asyncio
import hashlib
import json
import secrets
import socket
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from test_a2a_provenance_delivery import delivered_setup  # noqa: F401
from test_agent_authority import enroll, post, prepared  # noqa: F401

pytest.importorskip("a2a", reason="optional official A2A SDK required")

from a2a.types import a2a_pb2 as pb  # noqa: E402

from tessera_control.a2a_store import A2AStore  # noqa: E402
from tessera_control.store import Store  # noqa: E402


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_two_independent_sdk_peers_reference_outbox_cli_and_causal_notebook(request, monkeypatch):
    setup = request.getfixturevalue("delivered_setup")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
    from check_a2a_peers import connect, denied, send

    human, control, project = setup["owner"], setup["setup_client"], setup["project"]
    second, _ = enroll(
        control, human, project, ["authority:consume", "provenance:append", "notebook:read"]
    )
    tokens = [setup["token"], second["Authorization"].removeprefix("Bearer ")]
    actors = [Store(setup["state"]).authenticate(token) for token in tokens]
    grants = []
    for i, actor in enumerate(actors):
        bounds = {
            **deepcopy(setup["grant"]["bounds"]),
            "subject_credential_id": actor["credential_id"],
            "actions": ["attempt:status", "authority:delegate"],
            "delegation_depth": 1,
        }
        parent = post(
            control, human, f"/projects/{project}/agent-authority-grants", {"bounds": bounds}
        )
        recipient = actors[1 - i]
        child = {
            **bounds,
            "subject_credential_id": recipient["credential_id"],
            "actions": ["attempt:status"],
            "delegation_depth": 0,
            "budget_cents": 0,
        }
        grants.append(
            post(
                control,
                {"Authorization": "Bearer " + tokens[i], "X-Agent-Destination": "openai:personal"},
                f"/agent-authority-grants/{parent['id']}/delegate",
                {"expected_revision": 1, "expected_epoch": 1, "bounds": child},
            )
        )
    paths, configs, processes = [], [], []
    try:
        for i, actor in enumerate(actors):
            token_env, incoming_env = (
                "TESSERA_REFERENCE_TEST_TOKEN_" + str(i),
                "TESSERA_REFERENCE_TEST_INCOMING_" + str(i),
            )
            incoming = secrets.token_urlsafe(32)
            monkeypatch.setenv(token_env, tokens[i])
            monkeypatch.setenv(incoming_env, incoming)
            sender = actors[1 - i]
            config = {
                "agent_id": actor["id"],
                "credential_id": actor["credential_id"],
                "model": "model-1",
                "destination": "openai:personal",
                "control_url": setup["origin"],
                "public_url": "http://127.0.0.1:" + str(port()) + "/",
                "control_token_env": token_env,
                "database": str(setup["tmp_path"] / ("reference-peer-" + str(i) + ".sqlite")),
                "incoming_test_token_env": incoming_env,
                "identities": {
                    hashlib.sha256(incoming.encode()).hexdigest(): {
                        "tenant": sender["org_id"],
                        "agent_id": sender["id"],
                        "project_id": project,
                    }
                },
            }
            path = setup["tmp_path"] / ("reference-peer-" + str(i) + ".json")
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            configs.append(config)
            paths.append(path)
            argv = [
                sys.executable,
                "-m",
                "tessera_control.a2a_peer",
                "--config",
                str(path),
                "--port",
                str(config["public_url"].split(":")[-1].rstrip("/")),
            ]
            if i == 1:
                argv.append("--deliver-provenance")
            processes.append(
                subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            )

        async def exchange():
            clients, tasks = [], []
            try:
                for config in configs:
                    clients.append(await connect(config))
                for i, (_, client, _) in enumerate(clients):
                    grant = grants[1 - i]
                    envelope = {
                        "operation": "submit",
                        "reference": "opaque_input_reference",
                        "authority": {
                            "grant_id": grant["id"],
                            "revision": grant["revision"],
                            "epoch": grant["epoch"],
                            "binding": grant["bounds"]["bindings"][0],
                        },
                    }
                    message_id = "sdk_input_" + str(i)
                    task = await send(client, envelope, message_id)
                    result = await send(
                        client,
                        {**envelope, "operation": "result", "reference": "opaque_result_reference"},
                        "sdk_result_" + str(i),
                        task.id,
                    )
                    assert result.artifacts and result.status.state == pb.TASK_STATE_COMPLETED
                    assert (await send(client, envelope, message_id)).id == task.id
                    await denied(send(client, {**envelope, "reference": "changed"}, message_id))
                    pending = await send(client, envelope, "sdk_cancel_input_" + str(i))
                    assert (
                        await client.cancel_task(pb.CancelTaskRequest(id=pending.id))
                    ).status.state == pb.TASK_STATE_CANCELED
                    assert (
                        await client.cancel_task(pb.CancelTaskRequest(id=pending.id))
                    ).status.state == pb.TASK_STATE_CANCELED
                    tasks.append((task.id, pending.id, envelope))
                return tasks
            finally:
                for http, _, _ in clients:
                    await http.aclose()

        try:
            tasks = asyncio.run(exchange())
        except httpx.ConnectError:
            failed = next((process for process in processes if process.poll() is not None), None)
            if failed is not None:
                diagnostic = failed.stderr.read().splitlines()
                raise AssertionError(
                    diagnostic[-1] if diagnostic else "peer startup failed"
                ) from None
            raise

        def drain(i, task_id):
            sender = actors[1 - i]
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tessera_control.a2a_peer",
                    "drain-outbox",
                    "--config",
                    str(paths[i]),
                    "--task-id",
                    task_id,
                    "--sender-id",
                    sender["id"],
                    "--tenant",
                    sender["org_id"],
                    "--project-id",
                    project,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode in {0, 2}
            assert all(token not in result.stdout for token in tokens)
            return json.loads(result.stdout)

        record_ids = set()
        delivery_summaries = []
        for i, (completed, canceled, _) in enumerate(tasks):
            delivery_summaries.extend((drain(i, completed), drain(i, canceled)))
            store = A2AStore(Path(configs[i]["database"]))
            sender = actors[1 - i]
            for task_id in (completed, canceled):
                items = store.provenance_outbox(sender["org_id"], sender["id"], task_id)
                record_ids.update(item["envelope"]["record"]["record_id"] for item in items)
            drain(i, completed)  # exact replay: notebook must not gain duplicates
        with httpx.Client(trust_env=False, timeout=5) as client:
            notebook = client.get(
                setup["origin"] + f"/projects/{project}/agent-notebook/export",
                headers=setup["critic"],
            )
        assert notebook.status_code == 200
        records = [r for r in notebook.json()["provenance"] if r["record_id"] in record_ids]
        assert len(records) == 14
        for i, (completed, canceled, _) in enumerate(tasks):
            relevant = [r for r in records if r["remote"]["task_id"] in {completed, canceled}]
            assert len(relevant) == 7
            assert all(
                r["actor_principal_id"] == actors[i]["id"]
                and r["credential_id"] == actors[i]["credential_id"]
                for r in relevant
            )
            assert all(
                r["model_id"] is None and r["remote"]["peer_id"] == actors[1 - i]["id"]
                for r in relevant
            )
            result_message = next(r for r in relevant if r["tool_id"] == "a2a.reference.result")
            assert result_message["causation_id"] == "sdk_input_" + str(i)
            artifact_record = next(
                r for r in relevant if r["tool_id"] == "a2a.reference.result-reference"
            )
            assert artifact_record["causation_id"] == "sdk_result_" + str(i)
            assert any(r["kind"] == "permission" and r["outcome"] == "canceled" for r in relevant)
        with sqlite3.connect(setup["state"] / "tessera.sqlite3") as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM agent_provenance WHERE record_id LIKE 'peer_provenance_%'"
                ).fetchone()[0]
                == 14
            )

        async def cancel_after_revoke():
            http, client, _ = await connect(configs[0])
            try:
                task = await send(client, tasks[0][2], "sdk_before_revoke")
                grant = grants[1]
                post(
                    control,
                    human,
                    f"/agent-authority-grants/{grant['id']}/revoke",
                    {"expected_revision": 1, "expected_epoch": 1},
                    expected=200,
                )
                await client.cancel_task(pb.CancelTaskRequest(id=task.id))
                return task.id
            finally:
                await http.aclose()

        revoked_task = asyncio.run(cancel_after_revoke())
        rejected = drain(0, revoked_task)
        assert rejected["pending"] == 3 and rejected["acknowledged"] == 0
        store = A2AStore(Path(configs[0]["database"]))
        frozen = store.provenance_outbox(actors[1]["org_id"], actors[1]["id"], revoked_task)
        assert all(item["last_delivery"] == "rejected" for item in frozen)
        assert all(item["envelope"]["grant_proof"]["expected_epoch"] == 1 for item in frozen)
        assert [summary["acknowledged"] for summary in delivery_summaries] == [4, 3, 4, 3]
        assert all(summary["outbox_fully_acknowledged"] for summary in delivery_summaries)
    finally:
        for process in processes:
            stop(process)
