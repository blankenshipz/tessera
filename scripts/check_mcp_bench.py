"""Opt-in installed stdio clients against an existing authorized HTTPS research bench.

Only the named synthetic project's retained 36/37-byte artifacts may be disclosed.
No control service, worker, fixture, provider inference, approval or execution is created.
Human credential references enroll short scoped agents, explicitly disclose released
bytes and promote one immutable proposal to draft only. Created agents are revoked.
Run using the isolated installed wheel's Python, never PYTHONPATH=src.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import ssl
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError


@asynccontextmanager
async def session(entrypoint, env):
    with open(os.devnull, "w") as errors:
        async with stdio_client(
            StdioServerParameters(command=str(entrypoint), env=env, cwd=entrypoint.parent),
            errlog=errors,
        ) as (read, write):
            async with ClientSession(read, write) as client:
                assert (await client.initialize()).protocolVersion == "2025-11-25"
                yield client


async def call(client, name, args=None, denied=False):
    result = await client.call_tool(name, args or {})
    assert bool(result.isError) == denied, f"Unexpected tool outcome: {name}"
    return result.structuredContent


async def denied_resource(client, uri):
    try:
        await client.read_resource(uri)
    except McpError:
        return
    raise AssertionError("Resource denial not enforced")


async def acceptance(args):
    refs = json.loads(args.references.read_text())
    owner = Path(refs["owner_credential_file"]).read_text().strip().split("Bearer ", 1)[-1]
    ca = refs["ca_file"]
    args.work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    token_file = args.work_dir / "agent-token"
    credential_ids = []
    destination = "test:installed-sdk-research-bench"
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("TESSERA_") and k != "PYTHONPATH"
    }
    env.update(
        TESSERA_CONTROL_URL=refs["origin"],
        TESSERA_CONTROL_CA_FILE=ca,
        TESSERA_AGENT_TOKEN_FILE=str(token_file),
        TESSERA_PROJECT_ID=args.project_id,
        TESSERA_AGENT_DESTINATION=destination,
    )
    receipt = {
        "mode": "live",
        "origin": refs["origin"],
        "project_id": args.project_id,
        "entrypoint": str(args.entrypoint),
        "checks": [],
        "created_credentials": credential_ids,
    }
    async with httpx.AsyncClient(
        base_url=refs["origin"],
        trust_env=False,
        timeout=30,
        verify=ssl.create_default_context(cafile=ca),
    ) as http:

        async def request(method, path, body=None, token=owner, expected=(200, 201), dest=None):
            headers = {"Authorization": "Bearer " + token, "Idempotency-Key": secrets.token_hex(16)}
            if dest:
                headers["X-Agent-Destination"] = dest
            response = await http.request(method, path, headers=headers, json=body)
            assert response.status_code in expected, f"HTTP {response.status_code} at {path}"
            return response.json()

        async def enroll(seconds=600):
            token = secrets.token_urlsafe(32)
            credential = await request(
                "POST",
                "/credentials",
                {
                    "token": token,
                    "kind": "agent",
                    "audience": "control",
                    "project_id": args.project_id,
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
                    "external_agent": {
                        "actions": ["context:read", "proposal:create", "proposal:read"],
                        "destination": destination,
                    },
                },
            )
            credential_ids.append(credential["credential_id"])
            token_file.write_text(token)
            token_file.chmod(0o600)
            return token, credential["credential_id"]

        async def js(client_env, document):
            path = args.work_dir / "js-arguments.json"
            path.write_text(json.dumps(document))
            result = await asyncio.to_thread(
                subprocess.run,
                ["node", str(Path(__file__).with_name("check_mcp_client.mjs")), str(path)],
                env={
                    **client_env,
                    "TESSERA_MCP_ENTRYPOINT": str(args.entrypoint),
                    "TESSERA_MCP_JS_SDK": str(args.js_sdk),
                },
                capture_output=True,
                timeout=90,
            )
            assert result.returncode == 0, "Installed TypeScript SDK client failed (logs withheld)"

        try:
            assert (await http.get("/health/ready")).status_code == 200
            token, credential_id = await enroll()
            manifests = (await request("GET", f"/projects/{args.project_id}/artifact-manifests"))[
                "artifact_manifests"
            ]
            contexts = []
            for manifest in manifests:
                if not any(a["size"] in (36, 37) for a in manifest["artifacts"]):
                    continue
                # Only already-human-released artifacts; never auto-release custody here.
                released = await request("GET", f"/artifact-manifests/{manifest['id']}/released")
                for artifact in released["manifest"]["artifacts"]:
                    if artifact["size"] not in (36, 37):
                        continue
                    context = await request(
                        "POST",
                        f"/projects/{args.project_id}/agent-artifact-contexts",
                        {
                            "credential_id": credential_id,
                            "destination": destination,
                            "title": "Authorized safe synthetic bench output",
                            "classification": "project",
                            "disclosure_approved": True,
                            "manifest_id": manifest["id"],
                            "artifact_id": artifact["id"],
                            "expected_manifest_digest": manifest["manifest_digest"],
                            "max_bytes": artifact["size"],
                        },
                    )
                    contexts.append(context)
            assert {c["size"] for c in contexts} == {36, 37}, (
                "Expected released baseline/intervention bytes absent"
            )
            plan = {
                "title": "Installed external-agent review request",
                "purpose": "Review synthetic bench output; do not execute",
                "budget_cents": 0,
                "max_duration_seconds": 30,
                "execution_spec": {
                    "argv": ["python", "-c", "print('proposal-only')"],
                    "input_snapshot_digest": args.input_digest,
                    "pinned_skill_digests": [],
                    "output_paths": [],
                    "profile": "local-cpu",
                    "network_enabled": False,
                    "max_output_bytes": 1024,
                },
                "client_name": "official-installed-sdk-acceptance",
                "model": "none-no-inference",
                "session_id": args.work_dir.name,
                "idempotency_key": secrets.token_hex(16),
            }
            (args.work_dir / "proposal-arguments.json").write_text(json.dumps(plan))
            async with session(args.entrypoint, env) as client:
                assert (await call(client, "tessera_status"))["project_id"] == args.project_id
                for context in contexts:
                    resource = await client.read_resource(f"tessera://contexts/{context['id']}")
                    record = json.loads(resource.contents[0].text)
                    content = base64.b64decode(record["content_base64"], validate=True)
                    assert len(content) == context["size"] <= 65536
                    assert hashlib.sha256(content).hexdigest() == context["sha256"]
                    assert (
                        await call(client, "tessera_read_context", {"context_id": context["id"]})
                    )["sha256"] == context["sha256"]
                    await js(
                        env,
                        {
                            "mode": "artifact-resource",
                            "context_id": context["id"],
                            "content_base64": record["content_base64"],
                        },
                    )
                proposal = await call(client, "tessera_propose_task_plan", plan)
                assert proposal["promotion"] is None and proposal["authority_effect"] == "none"
                await call(client, "tessera_approve", denied=True)
            async with session(args.entrypoint, env) as client:
                assert (await call(client, "tessera_propose_task_plan", plan))["id"] == proposal[
                    "id"
                ]
                await call(
                    client, "tessera_propose_task_plan", {**plan, "title": "conflict"}, denied=True
                )
                await js(
                    env,
                    {
                        "mode": "bench-proposal",
                        "context_id": contexts[0]["id"],
                        "content_base64": contexts[0]["content_base64"],
                        "proposal_args": plan,
                    },
                )
                wrong = {**env, "TESSERA_AGENT_DESTINATION": "test:wrong-recipient"}
                async with session(args.entrypoint, wrong) as denied:
                    await denied_resource(denied, f"tessera://contexts/{contexts[0]['id']}")
                await js(
                    wrong,
                    {"mode": "artifact-resource", "context_id": contexts[0]["id"], "denied": True},
                )
                promote = {"expected_revision": 1, "expected_digest": proposal["digest"]}
                await request(
                    "POST",
                    f"/agent-proposals/{proposal['id']}/promote",
                    promote,
                    token=token,
                    expected=(403,),
                    dest=destination,
                )
                promoted = await request(
                    "POST", f"/agent-proposals/{proposal['id']}/promote", promote
                )
                observed = await call(
                    client, "tessera_read_proposal", {"proposal_id": proposal["id"]}
                )
                assert observed["promotion"] and promoted
                task = await request("GET", f"/tasks/{observed['promotion']['task_id']}")
                assert task["status"] == "draft" and not task["execution_granted"]
                await request("POST", f"/credentials/{credential_id}/revoke", {})
                await call(client, "tessera_propose_task_plan", plan, denied=True)
                await denied_resource(client, f"tessera://contexts/{contexts[0]['id']}")
                await enroll(seconds=3)
                await call(client, "tessera_status")
                await asyncio.sleep(3.2)
                await call(client, "tessera_status", denied=True)
            receipt.update(
                contexts=[{k: c[k] for k in ["id", "size", "sha256"]} for c in contexts],
                proposal_id=proposal["id"],
                promoted_task_id=task["id"],
                checks=[
                    "installed-entrypoint-https-ca",
                    "two-sdk-retained-bytes",
                    "proposal-review-request",
                    "reconnect-replay-conflict",
                    "destination-denial",
                    "no-agent-promotion",
                    "human-draft-only",
                    "revocation-replay-read-denial",
                    "expiry",
                ],
            )
        finally:
            for credential_id in credential_ids:
                await request("POST", f"/credentials/{credential_id}/revoke", {})
            token_file.unlink(missing_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "Installed wheel stdio + deployed verified HTTPS: both SDKs, retained36/37 bytes, "
        "proposal/reconnect/conflict/destination/expiry/revocation/"
        "no-self-approval/draft-only passed"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ["references", "entrypoint", "js-sdk", "work-dir", "receipt"]:
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--input-digest", required=True)
    asyncio.run(acceptance(parser.parse_args()))


if __name__ == "__main__":
    main()
