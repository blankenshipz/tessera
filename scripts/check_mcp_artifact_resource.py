"""Focused real HTTP/stdio artifact-disclosure boundary acceptance, no provider calls.

Bootstrap uses existing synthetic worker-history fixture to place actual b'direct'
bytes in real private storage/custody. Reads, human disclosure, destination denial
and custody revocation then use real control HTTP and official Python/JS SDK clients.
This does NOT verify worker execution or ordinary-service artifact collection.
Requires source checkout + test dependencies, and isolated JS SDK1.32.0 reference.
"""

import argparse
import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from check_mcp_control import call, session
from mcp.shared.exceptions import McpError


async def acceptance(root, js_sdk):
    checkout = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(checkout / "tests"))
    from test_artifact_custody import _released_manifest

    state, _, _, manifest, artifact, _, _ = _released_manifest(root)
    owner = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    launcher = root / "control.py"
    launcher.write_text(
        "from pathlib import Path\nfrom tessera_control.api import create_app\n"
        f"app = create_app(Path({str(state)!r}))\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(checkout / "src")])}
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
        async with httpx.AsyncClient(base_url=endpoint, trust_env=False, timeout=20) as http:
            for _ in range(100):
                try:
                    if (await http.get("/health/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Real control startup failed")
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Real control readiness timed out")

            async def human_post(path, body):
                response = await http.post(
                    path,
                    json=body,
                    headers={
                        "Authorization": "Bearer " + owner,
                        "Idempotency-Key": secrets.token_hex(16),
                    },
                )
                assert response.status_code in (200, 201), f"HTTP {response.status_code}"
                return response.json()

            token = secrets.token_urlsafe(32)
            destination = "test:artifact-recipient"
            credential = await human_post(
                "/credentials",
                {
                    "token": token,
                    "kind": "agent",
                    "audience": "control",
                    "project_id": manifest["project_id"],
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                    "external_agent": {"actions": ["context:read"], "destination": destination},
                },
            )
            token_file = root / "agent-token"
            token_file.write_text(token)
            token_file.chmod(0o600)
            env.update(
                TESSERA_CONTROL_URL=endpoint,
                TESSERA_AGENT_TOKEN_FILE=str(token_file),
                TESSERA_PROJECT_ID=manifest["project_id"],
                TESSERA_AGENT_DESTINATION=destination,
            )
            context = await human_post(
                f"/projects/{manifest['project_id']}/agent-artifact-contexts",
                {
                    "credential_id": credential["credential_id"],
                    "destination": destination,
                    "title": "Explicit retained bytes disclosure",
                    "classification": "project",
                    "disclosure_approved": True,
                    "manifest_id": manifest["id"],
                    "artifact_id": artifact["id"],
                    "expected_manifest_digest": manifest["manifest_digest"],
                    "max_bytes": 65536,
                },
            )
            expected = base64.b64encode(b"direct").decode()
            uri = f"tessera://contexts/{context['id']}"

            async def javascript(client_env, denied=False):
                args_file = root / "resource-arguments.json"
                args_file.write_text(
                    json.dumps(
                        {
                            "mode": "artifact-resource",
                            "context_id": context["id"],
                            "content_base64": expected,
                            "denied": denied,
                        }
                    )
                )
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["node", str(checkout / "scripts/check_mcp_client.mjs"), str(args_file)],
                    env={
                        **client_env,
                        "TESSERA_MCP_JS_SDK": str(js_sdk),
                        "TESSERA_MCP_PYTHON": sys.executable,
                    },
                    capture_output=True,
                    timeout=60,
                )
                assert result.returncode == 0, (
                    "Official JS artifact boundary failed (logs withheld)"
                )

            async with session(env) as client:
                read = await client.read_resource(uri)
                serialized = read.contents[0].text
                assert len(serialized.encode()) <= 128 * 1024
                record = json.loads(serialized)
                assert (
                    record["content_base64"] == expected and record["sha256"] == artifact["sha256"]
                )
                assert (await call(client, "tessera_read_context", {"context_id": context["id"]}))[
                    "size"
                ] == 6
                await javascript(env)
                wrong = {**env, "TESSERA_AGENT_DESTINATION": "test:wrong-recipient"}
                async with session(wrong) as other:
                    try:
                        await other.read_resource(uri)
                    except McpError:
                        pass
                    else:
                        raise AssertionError("Wrong destination resource unexpectedly disclosed")
                    await call(
                        other, "tessera_read_context", {"context_id": context["id"]}, denied=True
                    )
                await javascript(wrong, denied=True)
                await human_post(
                    f"/artifact-manifests/{manifest['id']}/custody-releases",
                    {
                        "expected_manifest_digest": manifest["manifest_digest"],
                        "expected_revision": 1,
                        "action": "revoke",
                        "rationale": "End bounded acceptance disclosure",
                    },
                )
                try:
                    await client.read_resource(uri)
                except McpError:
                    pass
                else:
                    raise AssertionError("Revoked custody resource unexpectedly disclosed")
                await javascript(env, denied=True)
            print(
                "Official Python + TypeScript SDKs: bounded retained-byte context resource/tool, "
                "wrong-destination denial and custody-revoked resource denial passed. "
                "Synthetic worker-history bootstrap; no worker execution/inference claimed."
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
    parser.add_argument("--js-sdk", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix="tessera-mcp-artifact-", dir="/private/tmp/opencode"
    ) as directory:
        asyncio.run(acceptance(Path(directory), args.js_sdk))


if __name__ == "__main__":
    main()
