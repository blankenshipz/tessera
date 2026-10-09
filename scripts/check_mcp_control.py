"""Opt-in real subprocess stdio / real loopback HTTP control acceptance; no inference.

Run with the installed mcp extra, or PYTHONPATH=src in an isolated SDK environment.
Creates and deletes its own private state and credentials; never touches existing state.
Optional --js-sdk points to an isolated @modelcontextprotocol/sdk@1.32.0 directory.
This verifies local process integration, not desktop hosts or off-machine deployment.
"""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tessera_control.store import initialize_state
from tessera_mcp.harness_binding import PERMITTED_TOOLS


@asynccontextmanager
async def session(env):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "tessera_mcp.server"], env=env
    )
    with open(os.devnull, "w") as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as client:
                initialization = await client.initialize()
                assert initialization.protocolVersion == "2025-11-25"
                yield client


async def call(client, name, args=None, *, denied=False):
    result = await client.call_tool(name, args or {})
    assert bool(result.isError) == denied, f"Unexpected MCP result for {name}"
    if not denied:
        return result.structuredContent
    return result


async def acceptance(root, js_sdk):
    _, owner_file = initialize_state(root / "state")
    owner = owner_file.read_text().strip().split("Bearer ", 1)[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    # Import factory through a private generated module; no credentials in process arguments.
    launcher = root / "control.py"
    launcher.write_text(
        "from pathlib import Path\nfrom tessera_control.api import create_app\n"
        f"app = create_app(Path({str(root / 'state')!r}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), str(Path(__file__).resolve().parents[1] / "src"), env.get("PYTHONPATH", "")]
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "control:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--log-level",
            "critical",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient(base_url=endpoint, trust_env=False, timeout=10) as http:
            for _ in range(100):
                try:
                    if (await http.get("/health/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Real control process failed startup")
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Real control process did not become ready")

            async def request(method, path, body=None, token=owner, expected=(200, 201), dest=None):
                headers = {
                    "Authorization": "Bearer " + token,
                    "Idempotency-Key": secrets.token_hex(16),
                }
                if dest:
                    headers["X-Agent-Destination"] = dest
                result = await http.request(method, path, json=body, headers=headers)
                assert result.status_code in expected, (
                    f"Unexpected HTTP {result.status_code}: {path}"
                )
                return result.json()

            project = await request("POST", "/projects", {"name": "MCP acceptance"})
            project_id = project["id"]
            destination = "test:local-acceptance"

            async def enroll(seconds=600, actions=None):
                token = secrets.token_urlsafe(32)
                credential = await request(
                    "POST",
                    "/credentials",
                    {
                        "token": token,
                        "kind": "agent",
                        "audience": "control",
                        "project_id": project_id,
                        "expires_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
                        "external_agent": {
                            "destination": destination,
                            "actions": actions
                            or ["context:read", "proposal:create", "proposal:read"],
                        },
                    },
                )
                return token, credential["credential_id"]

            token, credential_id = await enroll()
            token_file = root / "agent-token"
            token_file.write_text(token)
            token_file.chmod(0o600)
            env.update(
                TESSERA_CONTROL_URL=endpoint,
                TESSERA_AGENT_TOKEN_FILE=str(token_file),
                TESSERA_PROJECT_ID=project_id,
                TESSERA_AGENT_DESTINATION=destination,
            )
            context = await request(
                "POST",
                f"/projects/{project_id}/agent-contexts",
                {
                    "credential_id": credential_id,
                    "destination": destination,
                    "title": "Public synthetic acceptance objective",
                    "body": "Propose, never execute.",
                    "classification": "public",
                    "disclosure_approved": True,
                },
            )
            args = {
                "title": "Bounded acceptance proposal",
                "purpose": "Test real MCP integration",
                "budget_cents": 0,
                "max_duration_seconds": 30,
                "execution_spec": {
                    "argv": ["python", "-c", "print('acceptance')"],
                    "input_snapshot_digest": "0" * 64,
                    "pinned_skill_digests": [],
                    "output_paths": [],
                    "profile": "local-cpu",
                    "network_enabled": False,
                    "max_output_bytes": 1024,
                },
                "client_name": "official-sdk-acceptance",
                "model": "none-no-inference",
                "session_id": "local-acceptance",
                "idempotency_key": "persisted-before-submission",
            }
            async with session(env) as client:
                assert sorted(tool.name for tool in (await client.list_tools()).tools) == sorted(
                    PERMITTED_TOOLS
                )
                assert (await call(client, "tessera_status"))["project_id"] == project_id
                assert (await call(client, "tessera_read_context", {"context_id": context["id"]}))[
                    "body"
                ]
                resource = await client.read_resource(f"tessera://contexts/{context['id']}")
                assert resource.contents
                proposal = await call(client, "tessera_propose_task_plan", args)
                assert proposal["authority_effect"] == "none" and proposal["promotion"] is None
                await call(client, "tessera_approve", denied=True)
            async with session(env) as client:
                replay = await call(client, "tessera_propose_task_plan", args)
                assert replay["id"] == proposal["id"]
                await call(
                    client, "tessera_propose_task_plan", {**args, "title": "changed"}, denied=True
                )
                assert len((await call(client, "tessera_list_proposals"))["proposals"]) == 1

            # Exercise the standalone full-workflow client on the genuine protocol
            # boundary available here; this is not authority/provider acceptance.
            scenario = root / "reconnect-scenario.json"
            scenario.write_text(
                json.dumps(
                    [
                        {"tool": "tessera_propose_task_plan", "arguments": args},
                        {"tool": "tessera_propose_task_plan", "arguments": args, "same_as": 0},
                        {"tool": "tessera_approve", "denied": True},
                    ]
                )
            )
            checked = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("check_mcp_full_workflow.py")),
                "--scenario",
                str(scenario),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, _ = await asyncio.wait_for(checked.communicate(), timeout=90)
            assert checked.returncode == 0, "Standalone real SDK workflow harness failed"
            assert len(json.loads(output)["receipts"]) == 3

            promote = {"expected_revision": 1, "expected_digest": proposal["digest"]}
            await request(
                "POST",
                f"/agent-proposals/{proposal['id']}/promote",
                promote,
                token=token,
                dest=destination,
                expected=(403,),
            )
            await request("GET", f"/projects/{project_id}", token=token, expected=(403,))
            wrong = {**env, "TESSERA_AGENT_DESTINATION": "test:wrong-recipient"}
            async with session(wrong) as client:
                await call(
                    client, "tessera_read_context", {"context_id": context["id"]}, denied=True
                )

            if js_sdk:
                arguments_file = root / "proposal-arguments.json"
                arguments_file.write_text(json.dumps(args))
                js_env = {
                    **env,
                    "TESSERA_MCP_JS_SDK": str(js_sdk),
                    "TESSERA_MCP_PYTHON": sys.executable,
                }
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "node",
                        str(Path(__file__).with_name("check_mcp_client.mjs")),
                        str(arguments_file),
                    ],
                    env=js_env,
                    capture_output=True,
                    timeout=60,
                )
                assert completed.returncode == 0, (
                    "Official JS SDK acceptance failed (logs withheld)"
                )
                print("Official TypeScript SDK 1.32.0: tools/resources/proposal replay passed")

            promoted = await request("POST", f"/agent-proposals/{proposal['id']}/promote", promote)
            tasks = await request("GET", f"/projects/{project_id}/tasks")
            assert len(tasks["tasks"]) == 1 and tasks["tasks"][0]["status"] == "draft"
            assert promoted
            async with session(env) as client:
                assert (
                    await call(client, "tessera_read_proposal", {"proposal_id": proposal["id"]})
                )["promotion"]
                await request("POST", f"/credentials/{credential_id}/revoke", {})
                await call(client, "tessera_propose_task_plan", args, denied=True)

                expiring, _ = await enroll(seconds=2)
                token_file.write_text(expiring)
                await call(client, "tessera_status")
                await asyncio.sleep(2.2)
                await call(client, "tessera_status", denied=True)
                fresh, _ = await enroll(actions=["proposal:read"])
                token_file.write_text(fresh)
                await call(client, "tessera_status")
                await call(client, "tessera_propose_task_plan", args, denied=True)
                await call(
                    client, "tessera_read_context", {"context_id": context["id"]}, denied=True
                )
            print(
                "Official Python ClientSession: real stdio/control API proposal, reconnect, "
                "conflict, destination/scope denial, human-only promotion, revocation, expiry "
                "and fresh-credential replacement passed; no execution or inference"
            )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--js-sdk", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix="tessera-mcp-", dir="/private/tmp/opencode"
    ) as directory:
        asyncio.run(acceptance(Path(directory), args.js_sdk))


if __name__ == "__main__":
    main()
