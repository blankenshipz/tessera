"""Opt-in source RC gate: real MCP stdio + independent SDK peers, synthetic outputs.

Run with the pinned optional SDKs installed:
PYTHONPATH=src python dev/check_protocol_compatibility.py --receipt /PRIVATE/receipt.json
Never uses provider inference or active services. Final RC requires installed-wheel rerun.
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "dev"))

# The launcher must not import Tessera source via fixture imports before the
# installed candidate preflight. Pytest imports this file as its own module when
# collecting the gate, after candidate re-exec (or in explicit source mode).
if __name__ != "__main__":
    from test_a2a_research_generated_backend import (  # noqa: F401
        backend,
        capture_registered_source,
    )
    from test_agent_authority import post, prepared  # noqa: F401


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def base_env():
    env = {
        **{key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ},
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
    }
    if os.environ.get("TESSERA_COMPAT_INSTALLED_INFO"):
        env.pop("PYTHONPATH")
        env.update(PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
    return env


def service_command(name):
    info = os.environ.get("TESSERA_COMPAT_INSTALLED_INFO")
    if info:
        return [str(Path(json.loads(info)["bin_dir"]) / name)]
    return [
        sys.executable,
        "-m",
        {"tessera-mcp": "tessera_mcp.server", "tessera-a2a-peer": "tessera_control.a2a_peer"}[name],
    ]


def test_actual_local_protocol_compatibility(request, tmp_path, monkeypatch):
    from a2a.types import a2a_pb2 as pb
    from check_a2a_peers import connect, denied, send, stop
    from google.protobuf.json_format import MessageToDict
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from tessera_control.a2a_peer import drain_peer_outbox

    installed = None
    if os.environ.get("TESSERA_COMPAT_INSTALLED_INFO"):
        from protocol_installation import current_installation

        installed = current_installation(json.loads(os.environ["TESSERA_COMPAT_INSTALLED_INFO"]))
    b = request.getfixturevalue("backend")
    capture_registered_source(b, tmp_path / "author-capture")
    assert version("mcp") == "1.26.0" and version("a2a-sdk") == "1.2.1"
    stages, exits, processes = [], [], []
    peer_tokens = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    child_env = base_env()
    configs, paths = [], []
    for i, actor in enumerate(b["actors"]):
        config = deepcopy(b["config"])
        config.update(
            agent_id=actor["id"],
            credential_id=actor["credential_id"],
            database=str(tmp_path / f"peer-{i}.sqlite"),
            public_url=f"http://127.0.0.1:{available_port()}/",
            control_token_env=f"TESSERA_COMPAT_CONTROL_{i}",
            incoming_test_token_env=f"TESSERA_COMPAT_INCOMING_{i}",
        )
        config["identities"] = {
            hashlib.sha256(token.encode()).hexdigest(): {
                "tenant": principal["org_id"],
                "agent_id": principal["id"],
                "project_id": b["project"],
            }
            for token, principal in zip(peer_tokens, reversed(b["actors"]), strict=True)
        }
        token = b["critic" if i == 0 else "planner"]["Authorization"].removeprefix("Bearer ")
        child_env[config["control_token_env"]] = token
        child_env[config["incoming_test_token_env"]] = peer_tokens[i]
        monkeypatch.setenv(config["control_token_env"], token)
        monkeypatch.setenv(config["incoming_test_token_env"], peer_tokens[i])
        path = tmp_path / f"peer-{i}.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        configs.append(config)
        paths.append(path)

    def launch(i):
        process = subprocess.Popen(
            [
                *service_command("tessera-a2a-peer"),
                "--config",
                str(paths[i]),
                "--port",
                configs[i]["public_url"].split(":")[-1].rstrip("/"),
            ],
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    reverse_bounds = {
        **deepcopy(b["child"]["bounds"]),
        "subject_credential_id": b["actors"][1]["credential_id"],
        "actions": ["attempt:status", "authority:delegate"],
        "delegation_depth": 1,
    }
    parent = post(
        b["setup"],
        b["owner"],
        f"/projects/{b['project']}/agent-authority-grants",
        {"bounds": reverse_bounds},
    )
    reverse = post(
        b["setup"],
        b["planner"],
        f"/agent-authority-grants/{parent['id']}/delegate",
        {
            "expected_revision": 1,
            "expected_epoch": 1,
            "bounds": {
                **reverse_bounds,
                "subject_credential_id": b["actors"][0]["credential_id"],
                "actions": ["attempt:status"],
                "delegation_depth": 0,
            },
        },
    )

    def authority(grant):
        return {
            "grant_id": grant["id"],
            "revision": 1,
            "epoch": 1,
            "binding": grant["bounds"]["bindings"][0],
        }

    async def run():
        resources = []
        try:
            a_http, a, _ = await connect(configs[0])
            resources.append(a_http)
            b_http, peer, _ = await connect(configs[1])
            resources.append(b_http)
            stages.append("two_independent_peer_discovery")
            reference = {
                "operation": "submit",
                "reference": "synthetic-planner-request",
                "authority": authority(reverse),
            }
            reverse_task = await send(a, reference, "reverse-reference")
            await denied(peer.get_task(pb.GetTaskRequest(id=reverse_task.id)))
            stages.append("reverse_narrow_delegation_cross_ledger_denial")
            envelope = {
                "operation": "submit",
                "reference": b["pins"]["output_id"],
                "authority": authority(b["child"]),
                "generated": b["pins"],
            }
            task = await send(peer, envelope, "critic-submit")
            result = await send(peer, {**envelope, "operation": "result"}, "critic-result", task.id)
            assert result.status.state == pb.TASK_STATE_INPUT_REQUIRED
            assert (await send(peer, envelope, "critic-submit")).id == task.id
            await denied(
                send(
                    peer,
                    {**envelope, "generated": {**b["pins"], "session_id": "mismatch"}},
                    "bad-pins",
                )
            )
            stages.append("registered_typed_capture_result_replay_pin_denial")
            await b_http.aclose()
            resources.remove(b_http)
            stop(processes[-1])
            exits.append(processes[-1].returncode)
            launch(1)
            b_http, peer, _ = await connect(configs[1])
            resources.append(b_http)
            assert (await peer.get_task(pb.GetTaskRequest(id=task.id))).id == task.id
            assert (
                await send(peer, {**envelope, "operation": "result"}, "critic-result", task.id)
            ).id == task.id
            planner_config = {
                **configs[1],
                "incoming_test_token_env": configs[0]["incoming_test_token_env"],
            }
            planner_http, planner, _ = await connect(planner_config)
            resources.append(planner_http)
            ack = await send(planner, {**envelope, "operation": "consume"}, "planner-ack", task.id)
            assert ack.status.state == pb.TASK_STATE_COMPLETED
            assert (
                await send(planner, {**envelope, "operation": "consume"}, "planner-ack", task.id)
            ).id == task.id
            stages.append("active_peer_restart_same_task_explicit_planner_ack")
            pending = await send(peer, envelope, "cancel-submit")
            await send(peer, {**envelope, "operation": "result"}, "cancel-result", pending.id)
            assert (
                await peer.cancel_task(pb.CancelTaskRequest(id=pending.id))
            ).status.state == pb.TASK_STATE_CANCELED
            await denied(
                send(planner, {**envelope, "operation": "consume"}, "canceled-ack", pending.id)
            )
            stages.append("generated_pending_cancel_fences_ack")
            scope = {
                "tenant": b["actors"][0]["org_id"],
                "agent_id": b["actors"][0]["id"],
                "project_id": b["project"],
            }
            assert (await asyncio.to_thread(drain_peer_outbox, configs[1], scope, task.id))[
                "acknowledged"
            ] == 5
            stages.append("five_exact_collector_transport_receipts")
            return task.id, MessageToDict(ack)["metadata"]["planner_consumption"]["planner_id"]
        finally:
            for resource in resources:
                await resource.aclose()

    token_file = tmp_path / "mcp-planner.token"
    token_file.write_text(b["planner"]["Authorization"].removeprefix("Bearer "))
    token_file.chmod(0o600)
    mcp_env = {
        **base_env(),
        "TESSERA_CONTROL_URL": b["origin"],
        "TESSERA_PROJECT_ID": b["project"],
        "TESSERA_AGENT_DESTINATION": "openai:personal",
        "TESSERA_AGENT_TOKEN_FILE": str(token_file),
    }

    async def mcp_run():
        catalogs = []
        negotiated = []
        for _ in range(2):
            async with stdio_client(
                StdioServerParameters(
                    command=service_command("tessera-mcp")[0],
                    args=service_command("tessera-mcp")[1:],
                    env=mcp_env,
                )
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    initialization = await session.initialize()
                    negotiated.append(initialization.protocolVersion)
                    catalog = sorted(t.name for t in (await session.list_tools()).tools)
                    assert not any("approve" in name or "issue_grant" in name for name in catalog)
                    catalogs.append(catalog)
                    for tool, args in [
                        ("tessera_status", {}),
                        ("tessera_authority_status", {"grant_id": b["child"]["id"]}),
                        (
                            "tessera_generated_output_metadata",
                            {
                                "output_id": b["pins"]["output_id"],
                                "disclosure_id": b["pins"]["disclosure_id"],
                            },
                        ),
                    ]:
                        result = await session.call_tool(tool, arguments=args)
                        assert not result.isError, tool
                    invalid = await session.call_tool(
                        "tessera_generated_output_metadata",
                        arguments={
                            "output_id": b["pins"]["output_id"],
                            "disclosure_id": "not-disclosed",
                        },
                    )
                    assert invalid.isError
        assert catalogs[0] == catalogs[1] and negotiated == ["2025-11-25", "2025-11-25"]
        stages.append("actual_mcp_stdio_catalog_typed_calls_reconnect_disclosure_denial")
        critic_token = tmp_path / "mcp-critic.token"
        critic_token.write_text(b["critic"]["Authorization"].removeprefix("Bearer "))
        critic_token.chmod(0o600)
        critic_env = {**mcp_env, "TESSERA_AGENT_TOKEN_FILE": str(critic_token)}
        for _ in range(2):
            async with stdio_client(
                StdioServerParameters(
                    command=service_command("tessera-mcp")[0],
                    args=service_command("tessera-mcp")[1:],
                    env=critic_env,
                )
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    replay = await session.call_tool(
                        "tessera_register_generated_output",
                        arguments={
                            "payload": b["registration"],
                            "idempotency_key": "fixture-output-stable",
                        },
                    )
                    assert not replay.isError
                    result = replay.structuredContent or json.loads(replay.content[0].text)
                    assert result["id"] == b["output"]["id"]
        stages.append("actual_mcp_typed_registration_write_reconnect_exact_replay")
        return catalogs[0], negotiated[0]

    try:
        launch(0)
        launch(1)
        task_id, planner_id = asyncio.run(asyncio.wait_for(run(), timeout=90))
        catalog, protocol = asyncio.run(asyncio.wait_for(mcp_run(), timeout=60))
    finally:
        for process in processes:
            stop(process)
        exits = [p.returncode for p in processes]
        assert all(code is not None for code in exits)
    source_files = [
        "dev/check_protocol_compatibility.py",
        "src/tessera_control/a2a_peer.py",
        "src/tessera_control/a2a_store.py",
        "src/tessera_mcp/server.py",
        "src/tessera_mcp/generated_outputs.py",
        "src/tessera_agent/research_consumer.py",
    ]
    receipt = {
        "schema": "tessera-source-protocol-compatibility-v1",
        "status": "passed",
        "mode": "local executable",
        "synthetic": [
            "human fixture identities/resources",
            "public critic bytes/model/source metadata",
        ],
        "actual": [
            "loopback backend HTTP/SQLite",
            "independent A2A process ledgers/SDKJSONRPC",
            "MCP stdio subprocess reconnect",
            "typed collector receipts",
        ],
        "stages": stages,
        "a2a_task_id": task_id,
        "planner_id": planner_id,
        "peer_native_exit_codes": exits,
        "mcp_protocol": protocol,
        "mcp_catalog": catalog,
        "sdk_versions": {name: version(name) for name in ("mcp", "a2a-sdk")},
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_files
        },
        "limitations": [
            "source-tree module entrypoints, not final wheel",
            "no real critic inference/public visibility",
            "no offhost/laptop-off",
            "no experiment dispatch/running-job termination",
            "MCP native exit not exposed by SDK stdio context",
        ],
    }
    target = os.environ.get("TESSERA_COMPAT_RECEIPT")
    if installed:
        receipt["schema"] = "tessera-installed-protocol-compatibility-v1"
        receipt["installed_artifact"] = current_installation(installed)
        receipt["limitations"][0] = (
            "installed protocol gate only; full RC/live journey acceptance separate"
        )
    if target:
        Path(target).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--installed-python", type=Path)
    parser.add_argument("--installed-bin-dir", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--wheel-sha256")
    args = parser.parse_args()
    if any((args.installed_python, args.installed_bin_dir, args.wheel, args.wheel_sha256)):
        if not all(
            (
                args.installed_python,
                args.installed_bin_dir,
                args.wheel,
                args.wheel_sha256,
                args.receipt,
            )
        ):
            parser.error(
                "Installed mode requires both candidate paths, wheel, independent SHA256 "
                "and a distinct receipt"
            )
        if (
            args.receipt.resolve()
            == Path("/private/tmp/opencode/tessera-source-protocol-compatibility.json").resolve()
        ):
            parser.error(
                "Preserve the prior source receipt; installed mode needs a separate receipt path"
            )
        from protocol_installation import check_paths, inspect_installation

        info = check_paths(
            args.installed_python, args.installed_bin_dir, args.wheel, args.wheel_sha256
        )
        env = base_env()
        env.pop("PYTHONPATH", None)
        checked = inspect_installation(info, env)
        env["TESSERA_COMPAT_INSTALLED_INFO"] = json.dumps(checked)
        env["TESSERA_COMPAT_RECEIPT"] = str(args.receipt.absolute())
        raise SystemExit(
            subprocess.run(
                [info["python"], "-I", str(Path(__file__).resolve())], env=env
            ).returncode
        )
    if args.receipt:
        os.environ["TESSERA_COMPAT_RECEIPT"] = str(args.receipt)
    raise SystemExit(pytest.main([__file__, "-q", "-s", "--tb=short"]))
