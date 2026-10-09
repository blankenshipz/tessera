"""Opt-in full real SDK/stdio/control journey with synthetic execution resources.

Owns only temporary loopback control state/process and private generated credentials.
Human issuance uses ordinary HTTP; snapshot adoption uses ordinary local CLI.
Worker HTTP prepares a package but never executes it or contacts any cluster/provider.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from check_mcp_control import call, session

from tessera_control.store import initialize_state


def expiry(seconds=600):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


async def acceptance(root):
    state = root / "state"
    _, owner_file = initialize_state(state)
    owner = owner_file.read_text().strip().split("Bearer ", 1)[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    (root / "control.py").write_text(
        "from pathlib import Path\nfrom tessera_control.api import create_app\n"
        f"app = create_app(Path({str(state)!r}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(root),
            str(Path(__file__).resolve().parents[1] / "src"),
            env.get("PYTHONPATH", ""),
        ]
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
    receipts = []
    try:
        async with httpx.AsyncClient(base_url=endpoint, trust_env=False, timeout=20) as http:
            for _ in range(100):
                try:
                    if (await http.get("/health/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Owned control failed startup")
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Owned control readiness failed")

            async def request(method, path, body=None, token=owner, expected=(200, 201), dest=None):
                headers = {
                    "Authorization": "Bearer " + token,
                    "Idempotency-Key": secrets.token_hex(16),
                }
                if dest:
                    headers["X-Agent-Destination"] = dest
                response = await http.request(method, path, json=body, headers=headers)
                assert response.status_code in expected, f"HTTP {response.status_code} {path}"
                return response.json()

            async def tool(name, arguments=None, denied=False):
                # Every tool call crosses a newly initialized actual subprocess session.
                async with session(env) as client:
                    result = await call(client, name, arguments, denied=denied)
                action = (arguments or {}).get("payload", {}).get("command", {}).get("action")
                receipts.append(
                    {
                        "tool": name,
                        "expected": "denied" if denied else "passed",
                        "reconnected": True,
                        "action": action,
                    }
                )
                return result

            project = await request("POST", "/projects", {"name": "MCP synthetic execution bench"})
            pid = project["id"]
            destination = "test:mcp-full-workflow"
            agent_token = secrets.token_urlsafe(32)
            credential = await request(
                "POST",
                "/credentials",
                {
                    "token": agent_token,
                    "kind": "agent",
                    "audience": "control",
                    "project_id": pid,
                    "expires_at": expiry(3600),
                    "external_agent": {
                        "destination": destination,
                        "actions": [
                            "context:read",
                            "proposal:create",
                            "proposal:read",
                            "authority:request",
                            "authority:consume",
                            "observation:create",
                            "review:request",
                            "notebook:read",
                        ],
                    },
                },
            )
            token_file = root / "agent-token"
            token_file.write_text(agent_token)
            token_file.chmod(0o600)
            env.update(
                TESSERA_CONTROL_URL=endpoint,
                TESSERA_AGENT_TOKEN_FILE=str(token_file),
                TESSERA_PROJECT_ID=pid,
                TESSERA_AGENT_DESTINATION=destination,
            )
            context = await request(
                "POST",
                f"/projects/{pid}/agent-contexts",
                {
                    "credential_id": credential["credential_id"],
                    "destination": destination,
                    "title": "Synthetic package preparation only",
                    "body": "No cluster execution.",
                    "classification": "public",
                    "disclosure_approved": True,
                },
            )
            await tool("tessera_status")
            await tool("tessera_list_contexts")
            assert (await tool("tessera_read_context", {"context_id": context["id"]}))["body"]
            provenance = {
                "client": "official-sdk",
                "model": "none-no-inference",
                "session_id": "isolated-full-workflow",
            }
            proposal = await tool(
                "tessera_propose_research",
                {
                    "payload": {
                        "proposal": {
                            "kind": "study",
                            "study": {
                                "purpose": "Synthetic execution workflow",
                                "protocol": "No provider effects",
                                "mode": "exploratory",
                            },
                        },
                        "provenance": provenance,
                    },
                    "idempotency_key": "persisted-study-proposal",
                },
            )
            assert proposal["authority_effect"] == "none"
            promoted = await request(
                "POST",
                f"/agent-research-proposals/{proposal['id']}/promote",
                {"expected_revision": 1, "expected_digest": proposal["digest"]},
            )
            study_id = promoted["resource"]["id"]
            await request(
                "POST",
                f"/agent-research-proposals/{proposal['id']}/promote",
                {"expected_revision": 1, "expected_digest": proposal["digest"]},
                token=agent_token,
                dest=destination,
                expected=(403,),
            )
            worker_token = secrets.token_urlsafe(32)
            worker = await request(
                "POST",
                "/credentials",
                {
                    "token": worker_token,
                    "kind": "worker",
                    "audience": "worker",
                    "project_id": pid,
                    "expires_at": expiry(3600),
                },
            )
            await request(
                "POST",
                f"/projects/{pid}/workers",
                {
                    "worker_principal_id": worker["principal_id"],
                    "approved_build_identifier": "synthetic-no-execution",
                    "approved_build_digest": "a" * 64,
                    "trust_class": "test",
                    "region": "local",
                },
            )
            await request("POST", "/worker/heartbeat", {}, token=worker_token)
            workspace = root / "synthetic-input"
            workspace.mkdir()
            (workspace / "input.txt").write_text("Synthetic public input, never executed.")
            cli = [
                sys.executable,
                "-c",
                "from tessera_control.cli import main; raise SystemExit(main())",
                "workspace",
            ]
            inventory = subprocess.run(
                cli + ["inventory", "--root", str(workspace)],
                env=env,
                capture_output=True,
                timeout=20,
                check=True,
            )
            digest = json.loads(inventory.stdout)["digest"]
            adopted = subprocess.run(
                cli
                + [
                    "adopt",
                    "--root",
                    str(workspace),
                    "--state-dir",
                    str(state),
                    "--project-id",
                    pid,
                    "--expected-digest",
                    digest,
                    "--idempotency-key",
                    "synthetic-snapshot",
                ],
                env=env,
                capture_output=True,
                timeout=20,
                check=True,
            )
            snapshot = json.loads(adopted.stdout)
            await request(
                "POST",
                f"/snapshots/{snapshot['id']}/releases",
                {
                    "expected_digest": digest,
                    "worker_principal_id": worker["principal_id"],
                    "classification": "public",
                    "permits_worker_transfer": True,
                    "rationale": "Synthetic local protocol bench",
                    "expires_at": expiry(1800),
                },
            )
            task = await request(
                "POST",
                f"/projects/{pid}/tasks",
                {
                    "title": "Synthetic package",
                    "purpose": "Protocol acceptance only",
                    "budget_cents": 100,
                    "max_duration_seconds": 300,
                },
            )
            plan = {
                "expected_revision": task["revision"],
                "title": "Synthetic package",
                "purpose": "Protocol acceptance only",
                "budget_cents": 100,
                "max_duration_seconds": 300,
                "execution_spec": {
                    "argv": ["opencode", "serve"],
                    "input_snapshot_digest": digest,
                    "pinned_skill_digests": [],
                    "output_paths": ["result.txt"],
                    "profile": "kubernetes-opencode",
                    "network_enabled": False,
                    "max_output_bytes": 1024,
                },
            }
            task = await request("PUT", f"/tasks/{task['id']}/plan", plan)
            task = await request(
                "POST",
                f"/tasks/{task['id']}/approve",
                {
                    "expected_revision": task["revision"],
                    "expected_plan_digest": task["current_plan_digest"],
                },
            )
            await request(
                "PUT",
                f"/projects/{pid}/execution-limits",
                {
                    "expected_revision": 0,
                    "currency": "USD",
                    "monetary_limit_cents": 100,
                    "max_concurrency": 1,
                },
            )
            execution = await request(
                "POST",
                f"/projects/{pid}/execution-grants",
                {
                    "task_id": task["id"],
                    "expected_task_revision": task["revision"],
                    "expected_plan_digest": task["current_plan_digest"],
                    "worker_principal_id": worker["principal_id"],
                    "snapshot_id": snapshot["id"],
                    "pinned_image": "python@sha256:" + "a" * 64,
                    "expires_at": expiry(1200),
                    "max_exposure_cents": 100,
                    "currency": "USD",
                    "acknowledges_unmeasured_infrastructure": True,
                },
            )
            lease = await request(
                "POST", "/worker/leases", {"grant_id": execution["id"]}, token=worker_token
            )
            execution = await request("GET", f"/execution-grants/{execution['id']}")
            design = {
                "goal": "Synthetic comparison",
                "success_criterion": "Protocol calls only",
                "protocol": "Never claim scientific validity",
                "variants": [
                    {
                        "kind": kind,
                        "name": kind,
                        "method_digest": "a" * 64,
                        "input_digest": digest,
                        "metadata": {"synthetic": True},
                        "execution_task_id": task["id"],
                        "execution_plan_digest": task["current_plan_digest"],
                    }
                    for kind in ["baseline", "intervention"]
                ],
            }
            experiment = await request("POST", f"/projects/{pid}/experiments", design)
            variant = experiment["design"]["variants"][0]
            for research in [
                {"kind": "project", "name": "proposed", "purpose": "review only"},
                {"kind": "experiment", "study_id": study_id, "experiment": design},
            ]:
                assert (
                    await tool(
                        "tessera_propose_research",
                        {
                            "payload": {"proposal": research, "provenance": provenance},
                            "idempotency_key": "persisted-" + research["kind"],
                        },
                    )
                )["authority_effect"] == "none"
            bounds = {
                "subject_credential_id": credential["credential_id"],
                "destination": destination,
                "model": provenance["model"],
                "purpose": "Synthetic package protocol only",
                "actions": [
                    "attempt:admit",
                    "attempt:dispatch",
                    "attempt:status",
                    "execution:cancel",
                ],
                "bindings": [
                    {
                        "variant_id": variant["id"],
                        "expected_variant_revision": variant["revision"],
                        "expected_design_revision": experiment["current_design_revision"],
                        "execution_grant_id": execution["id"],
                        "expected_execution_grant_revision": execution["revision"],
                        "expected_execution_epoch": execution["epoch"],
                        "task_id": task["id"],
                        "expected_task_revision": task["revision"],
                        "plan_digest": task["current_plan_digest"],
                        "target_digest": "a" * 64,
                    }
                ],
                "budget_cents": 100,
                "max_duration_seconds": 300,
                "expires_at": expiry(1800),
                "delegation_depth": 0,
            }
            requested = await tool(
                "tessera_request_authority",
                {"bounds": bounds, "idempotency_key": "persisted-authority-request"},
            )
            await tool("tessera_list_authority_requests")
            authority = await request(
                "POST",
                f"/projects/{pid}/agent-authority-grants",
                {"bounds": bounds, "request_id": requested["id"]},
            )
            await request(
                "POST",
                f"/projects/{pid}/agent-authority-grants",
                {"bounds": bounds},
                token=agent_token,
                dest=destination,
                expected=(403,),
            )
            await tool("tessera_authority_status", {"grant_id": authority["id"]})
            fence = {
                "expected_revision": authority["revision"],
                "expected_epoch": authority["epoch"],
                "model": provenance["model"],
            }
            admit = {
                **fence,
                "command": {
                    "action": "attempt:admit",
                    "variant_id": variant["id"],
                    "expected_variant_revision": variant["revision"],
                    "expected_design_revision": experiment["current_design_revision"],
                    "execution_grant_id": execution["id"],
                },
            }

            async def consume(payload, key, denied=False):
                return await tool(
                    "tessera_consume_grant",
                    {"grant_id": authority["id"], "payload": payload, "idempotency_key": key},
                    denied=denied,
                )

            admitted = await consume(admit, "persisted-admit")
            assert await consume(admit, "persisted-admit") == admitted
            attempt = admitted["result"]
            await request(
                "POST",
                f"/worker/leases/{lease['id']}/package",
                {
                    "expected_epoch": lease["epoch"],
                    "admitted_attempt_id": attempt["id"],
                    "execution_adapter": "kubernetes-job",
                },
                token=worker_token,
            )
            status = {**fence, "command": {"action": "attempt:status", "attempt_id": attempt["id"]}}
            current = (await consume(status, "persisted-status"))["result"]
            dispatch = {
                **fence,
                "command": {
                    "action": "attempt:dispatch",
                    "attempt_id": attempt["id"],
                    "expected_revision": current["revision"],
                    "target_digest": "b" * 64,
                    "profile_version": "development-minikube-opencode-v1",
                },
            }
            await consume(dispatch, "denied-expanded-target", denied=True)
            dispatch["command"]["target_digest"] = "a" * 64
            dispatched = await consume(dispatch, "persisted-dispatch")
            assert await consume(dispatch, "persisted-dispatch") == dispatched
            current = (await consume(status, "persisted-status-after-dispatch"))["result"]
            observed = await tool(
                "tessera_record_observation",
                {
                    "payload": {
                        "attempt_id": attempt["id"],
                        "expected_attempt_revision": current["revision"],
                        "summary": "Synthetic package queued; never executed",
                        "artifact_ids": [],
                        "provenance": provenance,
                    },
                    "idempotency_key": "persisted-observation",
                },
            )
            assert observed["provenance_kind"] == "external_agent_caller_attested"
            await tool(
                "tessera_request_review",
                {
                    "payload": {
                        "subject_type": "observation",
                        "subject_id": observed["id"],
                        "expected_revision": 1,
                        "reason": "Separate human review",
                    },
                    "idempotency_key": "persisted-review",
                },
            )
            expiring_bounds = {
                **bounds,
                "actions": ["attempt:status"],
                "budget_cents": 0,
                "expires_at": expiry(3),
            }
            expiring_authority = await request(
                "POST", f"/projects/{pid}/agent-authority-grants", {"bounds": expiring_bounds}
            )
            await asyncio.sleep(3.2)
            await tool(
                "tessera_consume_grant",
                {
                    "grant_id": expiring_authority["id"],
                    "payload": status,
                    "idempotency_key": "denied-expired-authority",
                },
                denied=True,
            )
            cancel = {
                **fence,
                "command": {
                    "action": "execution:cancel",
                    "execution_grant_id": execution["id"],
                    "expected_revision": execution["revision"],
                },
            }
            cancelled = await consume(cancel, "persisted-cancel")
            assert cancelled["result"]["status"] == "cancel_requested"
            assert await consume(cancel, "persisted-cancel") == cancelled
            notebook = await tool("tessera_notebook", {"limit": 2})
            assert notebook["has_more"] and notebook["completeness"] == "partial_page"
            continued = await tool(
                "tessera_notebook",
                {
                    "offset": notebook["next"]["after_sequence"],
                    "after_event_cursor": notebook["next"]["after_event_cursor"],
                    "limit": 2,
                },
            )
            assert not (
                {e["cursor"] for e in notebook["control_events"]}
                & {e["cursor"] for e in continued["control_events"]}
            )
            exported = await tool("tessera_export_notebook")
            assert not exported["has_more"]
            assert exported["completeness"] == "complete_recorded_project_metadata_snapshot"
            assert exported["execution_verification"] == "not_established_by_provenance"
            assert exported["scientific_validity"] == "not_assessed"
            expected_events = {
                "agent_authority.requested",
                "agent_authority.issued",
                "agent_authority.consumed",
                "agent_research.proposal",
                "agent_research.observation",
                "agent_research.review_request",
            }
            assert expected_events <= {e["event_type"] for e in exported["control_events"]}
            assert "Synthetic package queued" not in json.dumps(exported)
            await tool("tessera_approve", denied=True)
            await tool(
                "tessera_consume_grant",
                {
                    "grant_id": authority["id"],
                    "idempotency_key": "denied-forward",
                    "payload": {**fence, "command": {"action": "forward", "path": "/credentials"}},
                },
                denied=True,
            )
            await request(
                "POST",
                f"/agent-authority-grants/{authority['id']}/revoke",
                {"expected_revision": authority["revision"], "expected_epoch": authority["epoch"]},
            )
            await consume(admit, "persisted-admit", denied=True)
            revoked_export = await tool("tessera_export_notebook")
            assert "agent_authority.revoked" in {
                e["event_type"] for e in revoked_export["control_events"]
            }
            # Credential expiry is real wall-clock fencing, not a database time rewrite.
            expiring_token = secrets.token_urlsafe(32)
            await request(
                "POST",
                "/credentials",
                {
                    "token": expiring_token,
                    "kind": "agent",
                    "audience": "control",
                    "project_id": pid,
                    "expires_at": expiry(2),
                    "external_agent": {"destination": destination, "actions": ["context:read"]},
                },
            )
            token_file.write_text(expiring_token)
            await asyncio.sleep(2.2)
            await tool("tessera_status", denied=True)
            print(
                json.dumps(
                    {
                        "protocol": "2025-11-25",
                        "transport": "stdio",
                        "receipts": receipts,
                        "execution_effects": "synthetic package; no worker execution or cluster",
                        "human_setup": "separate ordinary HTTP/CLI",
                        "inference_calls": 0,
                    }
                )
            )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    with tempfile.TemporaryDirectory(
        prefix="tessera-mcp-full-", dir="/private/tmp/opencode"
    ) as directory:
        asyncio.run(asyncio.wait_for(acceptance(Path(directory)), timeout=240))


if __name__ == "__main__":
    main()
