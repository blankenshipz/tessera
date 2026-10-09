"""Opt-in real local A2A boundary with isolated synthetic provisioning.

Uses existing test helpers to provision a synthetic worker/resource ledger (no
workload runs). Actual authority issue/delegate/revoke methods, an HTTP control
process and two independent official-SDK peer processes are exercised. Requires
the checkout's tests, not an installed production dependency on test helpers.
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
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fixture():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from check_a2a_peers import denied, run, send, stop
    from test_execution import _grant_payload, _setup
    from test_goal_to_decision import _command, _design

    from tessera_control.agent_authority import AgentAuthorityStore
    from tessera_control.experiments import ExperimentStore
    from tessera_control.store import Store, _connect, canonical_digest

    approved_tmp = Path("/private/tmp/opencode")
    with tempfile.TemporaryDirectory(
        prefix="tessera-a2a-", dir=approved_tmp if approved_tmp.is_dir() else None
    ) as tmp:
        root = Path(tmp)
        state, execution, owner, worker, project, task = _setup(root)
        store = Store(state)
        with _connect(execution.database_path) as db:
            snapshot_id = db.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
        payload = _grant_payload(task, worker, {"id": snapshot_id})
        _, execution_grant = execution.issue_execution_grant(
            owner, project["id"], "fixture-execution", canonical_digest(payload), payload
        )
        experiment = _command(
            ExperimentStore(state).create_experiment,
            owner,
            project["id"],
            payload=_design(grant=execution_grant),
        )
        variant = experiment["design"]["variants"][0]
        binding = {
            "variant_id": variant["id"],
            "expected_variant_revision": variant["revision"],
            "expected_design_revision": experiment["current_design_revision"],
            "execution_grant_id": execution_grant["id"],
            "expected_execution_grant_revision": execution_grant["revision"],
            "expected_execution_epoch": execution_grant["epoch"],
            "task_id": task["id"],
            "expected_task_revision": task["revision"],
            "plan_digest": execution_grant["plan_digest"],
            "target_digest": "b" * 64,
        }
        authority, actors, env_names = AgentAuthorityStore(state), [], []
        try:
            for name in ("A", "B"):
                token = secrets.token_urlsafe(32)
                env = "TESSERA_A2A_FIXTURE_CONTROL_" + name
                os.environ[env] = token
                env_names.append(env)
                body = {
                    "kind": "agent",
                    "audience": "control",
                    "project_id": project["id"],
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "external_agent": {
                        "actions": ["authority:consume"],
                        "destination": "a2a:local-reference",
                    },
                }
                store.issue_credential(owner, "agent-" + name, canonical_digest(body), body, token)
                actor = store.authenticate(token)
                actor["agent_destination"] = "a2a:local-reference"
                actors.append(actor)
            grants, parents = [], []
            for i, actor in enumerate(actors):
                bounds = {
                    "subject_credential_id": actor["credential_id"],
                    "destination": "a2a:local-reference",
                    "model": "reference-only",
                    "purpose": "Synthetic local protocol proof; no workload dispatch",
                    "actions": ["attempt:status", "authority:delegate"],
                    "bindings": [binding],
                    "budget_cents": 0,
                    "max_duration_seconds": 60,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                    "delegation_depth": 1,
                }
                _, parent = authority.issue_grant(
                    owner, project["id"], "root-" + str(i), {"bounds": bounds}
                )
                parents.append(parent)
                child_bounds = {
                    **bounds,
                    "subject_credential_id": actors[1 - i]["credential_id"],
                    "actions": ["attempt:status"],
                    "delegation_depth": 0,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=25)).isoformat(),
                }
                _, child = authority.delegate_grant(
                    actor,
                    parent["id"],
                    "delegate-" + str(i),
                    {
                        "expected_revision": 1,
                        "expected_epoch": 1,
                        "bounds": child_bounds,
                    },
                )
                grants.append(child)
            control_port, paths = free_port(), []
            for i, name in enumerate(("A", "B")):
                peer_token = secrets.token_urlsafe(32)
                env = "TESSERA_A2A_FIXTURE_INCOMING_" + name
                os.environ[env] = peer_token
                env_names.append(env)
                actor, sender, grant = actors[i], actors[1 - i], grants[1 - i]
                config = {
                    "public_url": "http://127.0.0.1:" + str(free_port()) + "/",
                    "control_url": "http://127.0.0.1:" + str(control_port),
                    "database": str(root / ("peer-" + name + ".sqlite")),
                    "agent_id": actor["id"],
                    "credential_id": actor["credential_id"],
                    "destination": "a2a:local-reference",
                    "model": "reference-only",
                    "control_token_env": "TESSERA_A2A_FIXTURE_CONTROL_" + name,
                    "incoming_test_token_env": env,
                    "identities": {
                        hashlib.sha256(peer_token.encode()).hexdigest(): {
                            "tenant": sender["org_id"],
                            "agent_id": sender["id"],
                            "project_id": project["id"],
                        }
                    },
                    "test_envelope": {
                        "operation": "submit",
                        "reference": "context-synthetic-local",
                        "authority": {
                            "grant_id": grant["id"],
                            "revision": grant["revision"],
                            "epoch": grant["epoch"],
                            "binding": binding,
                        },
                    },
                }
                path = root / ("peer-" + name + ".json")
                path.write_text(json.dumps(config))
                path.chmod(0o600)
                paths.append(path)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import uvicorn,sys;from pathlib import Path;"
                    "from tessera_control.api import create_app;"
                    'uvicorn.run(create_app(Path(sys.argv[1])),host="127.0.0.1",port=int(sys.argv[2]),access_log=False)',
                    str(state),
                    str(control_port),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for attempt in range(50):
                    try:
                        with httpx.Client(trust_env=False) as readiness:
                            readiness.get("http://127.0.0.1:" + str(control_port) + "/", timeout=5)
                        break
                    except httpx.ConnectError:
                        if process.poll() is not None or attempt == 49:
                            raise
                        time.sleep(0.1)

                async def revoke_check(configs, clients, tasks):
                    authority.revoke_grant(
                        owner,
                        parents[1]["id"],
                        "revoke-parent",
                        {
                            "expected_revision": 1,
                            "expected_epoch": 1,
                        },
                    )
                    await denied(send(clients[0][1], configs[0]["test_envelope"], tasks[0][3]))
                    _, short = authority.delegate_grant(
                        actors[0],
                        parents[0]["id"],
                        "short-lived",
                        {
                            "expected_revision": 1,
                            "expected_epoch": 1,
                            "bounds": {
                                **grants[0]["bounds"],
                                "expires_at": (
                                    datetime.now(UTC) + timedelta(seconds=3)
                                ).isoformat(),
                            },
                        },
                    )
                    expired_envelope = {
                        **configs[1]["test_envelope"],
                        "authority": {
                            "grant_id": short["id"],
                            "revision": 1,
                            "epoch": 1,
                            "binding": binding,
                        },
                    }
                    message_id = secrets.token_hex(16)
                    await send(clients[1][1], expired_envelope, message_id)
                    await asyncio.sleep(3.1)
                    await denied(send(clients[1][1], expired_envelope, message_id))

                result = asyncio.run(run(paths, revoke_check))
                result["checks"].append("ancestor-revocation-replay-denial")
                result["checks"].append("real-time-expiry-replay-denial")
                result["authority_provisioning"] = (
                    "synthetic setup records; real issue/delegate/revoke and HTTP checks; no stub"
                )
                return result
            finally:
                stop(process)
        finally:
            for env in env_names:
                os.environ.pop(env, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        print(
            json.dumps(
                {
                    "status": "not-run",
                    "effects": "opt-in temporary local control and two SDK peers only",
                }
            )
        )
        return
    print(json.dumps(fixture(), sort_keys=True))


if __name__ == "__main__":
    main()
