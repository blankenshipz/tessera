"""Real loopback collector HTTP/SQLite; synthetic setup/reasoning, no inference."""

import hashlib
import json
import secrets
import socket
import sqlite3
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import uvicorn
from test_agent_authority import enroll, issue, post, prepared  # noqa: F401
from test_remote_reasoning_runtime import Harness

from tessera_agent.a2a_reasoning_bridge import A2AReasoningBridge, drain_outbox, main
from tessera_agent.provenance_client import DeliveryResult, ProvenanceClient
from tessera_agent.remote_reasoning_runtime import JsonEndpoint, RuntimeBlocked
from tessera_agent.remote_reasoning_store import ReasoningStore
from tessera_control.a2a_store import A2AStore, PeerConflict
from tessera_control.api import create_app
from tessera_control.store import Store


@pytest.fixture
def delivered_setup(request, tmp_path):
    initial = request.getfixturevalue("prepared")
    state, setup_client, owner, sender, project, initial_bounds, _ = initial
    critic, credential = enroll(
        setup_client,
        owner,
        project,
        [
            "context:read",
            "authority:consume",
            "notebook:read",
            "provenance:append",
        ],
    )
    parent_bounds = {
        **deepcopy(initial_bounds),
        "actions": ["attempt:status", "authority:delegate"],
        "delegation_depth": 1,
    }
    parent = issue(initial, parent_bounds)
    child_bounds = {
        **deepcopy(parent_bounds),
        "subject_credential_id": credential["credential_id"],
        "actions": ["attempt:status"],
        "budget_cents": 0,
        "delegation_depth": 0,
    }
    exchange = post(
        setup_client,
        sender,
        f"/agent-authority-grants/{parent['id']}/delegate",
        {
            "expected_revision": parent["revision"],
            "expected_epoch": parent["epoch"],
            "bounds": child_bounds,
        },
    )
    reasoning_bounds = {
        **deepcopy(parent_bounds),
        "subject_credential_id": credential["credential_id"],
        "actions": ["attempt:status"],
        "delegation_depth": 0,
    }
    grant = issue(initial, reasoning_bounds)
    prompt = b"Disclosed synthetic critic delivery fixture; no actual model run"
    context = post(
        setup_client,
        owner,
        f"/projects/{project}/agent-contexts",
        {
            "credential_id": credential["credential_id"],
            "destination": "openai:personal",
            "title": "disclosed synthetic critic",
            "body": prompt.decode(),
            "classification": "project",
            "disclosure_approved": True,
        },
    )
    sender_actor = Store(state).authenticate(sender["Authorization"].removeprefix("Bearer "))
    peer = A2AStore(tmp_path / "peer.sqlite")
    task = peer.send(
        credential["org_id"],
        sender_actor["id"],
        "peer_input_message",
        {
            "operation": "submit",
            "reference": context["id"],
            "authority": {
                "grant_id": exchange["id"],
                "revision": exchange["revision"],
                "epoch": exchange["epoch"],
                "binding": child_bounds["bindings"][0],
            },
        },
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    origin = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(create_app(state), log_level="critical", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    runtime = ReasoningStore(tmp_path / "runtime")
    try:
        for _ in range(100):
            if server.started:
                break
            if not thread.is_alive():
                raise AssertionError("owned collector failed to start")
            time.sleep(0.01)
        assert server.started
        config = {
            "credential_id": credential["credential_id"],
            "destination": "openai:personal",
            "model": "model-1",
            "control_url": origin,
            "database": str(peer.path),
        }
        profile = {
            "run_id": "critic_delivery_run",
            "agent": "critic",
            "grant_id": grant["id"],
            "subject_credential_id": credential["credential_id"],
            "destination": "openai:personal",
            "revision": grant["revision"],
            "epoch": grant["epoch"],
            "model": {"providerID": "openai", "id": "model-1"},
            "account_mode": "included-chatgpt-oauth",
            "max_steps": 3,
            "max_tokens": 100,
            "max_duration_seconds": 60,
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
            "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
            "control_url": origin,
            "agent_token_env": "TESSERA_TEST_CRITIC_DELIVERY_TOKEN",
            "harness_token_env": "TESSERA_TEST_UNAVAILABLE_HARNESS_TOKEN",
        }
        raw = json.dumps(profile).encode()
        mapping = {
            "task_id": task["id"],
            "tenant": credential["org_id"],
            "sender_id": sender_actor["id"],
            "project_id": project,
            "context_id": context["id"],
            "profile_file": str(tmp_path / "profile.json"),
            "prompt_file": str(tmp_path / "prompt.txt"),
            "profile_sha256": hashlib.sha256(raw).hexdigest(),
        }
        harness, exchange_state = Harness(), {"active": True}

        async def synthetic_exchange_verifier(identity, fence):
            if not exchange_state["active"]:
                raise RuntimeBlocked("synthetic_exchange_denial")

        token = critic["Authorization"].removeprefix("Bearer ")
        bridge = A2AReasoningBridge(
            peer,
            runtime,
            config,
            mapping,
            raw,
            prompt,
            JsonEndpoint(origin, token, config["destination"]),
            harness,
            synthetic_exchange_verifier,
        )
        with ProvenanceClient(origin, token, config["destination"]) as delivery:
            yield {
                "peer": peer,
                "mapping": mapping,
                "bridge": bridge,
                "harness": harness,
                "delivery": delivery,
                "state": state,
                "project": project,
                "critic": critic,
                "setup_client": setup_client,
                "owner": owner,
                "grant": grant,
                "origin": origin,
                "token": token,
                "config": config,
                "raw": raw,
                "exchange_state": exchange_state,
                "tmp_path": tmp_path,
            }
    finally:
        runtime.close()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def notebook(setup):
    with httpx.Client(trust_env=False, timeout=5) as client:
        response = client.get(
            setup["origin"] + f"/projects/{setup['project']}/agent-notebook/export",
            headers=setup["critic"],
        )
    assert response.status_code == 200
    return response.json()["provenance"]


def test_real_http_delivery_and_joined_model_session_delegation_notebook(delivered_setup):
    setup = delivered_setup
    setup["harness"].observation["outcome"] = "succeeded"
    result = setup["bridge"].tick()
    assert result["state"] == "completed", setup["bridge"].runtime.get("critic_delivery_run")[
        "reason"
    ]
    result = drain_outbox(setup["peer"], setup["mapping"], setup["delivery"])
    assert result == {
        "total": 4,
        "acknowledged": 4,
        "pending": 0,
        "outbox_fully_acknowledged": True,
    }
    records = notebook(setup)
    relevant = [r for r in records if r["remote"]["task_id"] == setup["mapping"]["task_id"]]
    assert len(relevant) == 4
    assert {r["kind"] for r in relevant} == {"model", "agent", "delegation"}
    assert any(r["outcome"] == "succeeded" and r["model_id"] == "model-1" for r in relevant)
    assert any(
        r["correlation_id"].startswith("ses_") and r["causation_id"] == "critic_delivery_run"
        for r in relevant
    )
    assert all(r["references"]["context_id"] == setup["mapping"]["context_id"] for r in relevant)
    before = {r["record_id"]: r["sequence"] for r in relevant}
    assert drain_outbox(setup["peer"], setup["mapping"], setup["delivery"]) == result
    assert {
        r["record_id"]: r["sequence"] for r in notebook(setup) if r["record_id"] in before
    } == before


def test_real_notebook_permission_denial_and_unknown_outcome_are_not_relabelled(delivered_setup):
    setup = delivered_setup
    setup["exchange_state"]["active"] = False
    assert setup["bridge"].tick()["state"] == "stop_acknowledged"
    assert not setup["harness"].calls
    assert drain_outbox(setup["peer"], setup["mapping"], setup["delivery"])["pending"] == 0
    relevant = [r for r in notebook(setup) if r["remote"]["task_id"] == setup["mapping"]["task_id"]]
    assert any(r["kind"] == "permission" and r["outcome"] == "denied" for r in relevant)
    assert any(r["kind"] == "model" and r["outcome"] == "unknown" for r in relevant)


def test_lost_acknowledgement_real_commit_replay_has_no_duplicate(delivered_setup):
    setup = delivered_setup
    setup["harness"].observation["outcome"] = "succeeded"
    setup["bridge"].tick()

    class LostAck:
        def append(self, *args, **kwargs):
            receipt = setup["delivery"].append(*args, **kwargs)
            assert receipt.status == "accepted"
            return DeliveryResult("unknown", "transport_failure", receipt.record_id)

    assert drain_outbox(setup["peer"], setup["mapping"], LostAck())["pending"] == 4
    with sqlite3.connect(setup["state"] / "tessera.sqlite3") as db:
        before = db.execute(
            "SELECT count(*) FROM agent_provenance WHERE record_id LIKE 'a2a_provenance_%'"
        ).fetchone()[0]
    assert before == 4
    reopened = A2AStore(setup["peer"].path)
    assert drain_outbox(reopened, setup["mapping"], setup["delivery"])["pending"] == 0
    with sqlite3.connect(setup["state"] / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM agent_provenance WHERE record_id LIKE 'a2a_provenance_%'"
            ).fetchone()[0]
            == before
        )
    items = reopened.provenance_outbox(
        setup["mapping"]["tenant"], setup["mapping"]["sender_id"], setup["mapping"]["task_id"]
    )
    assert all(item["delivery_attempts"] == 2 and item["receipt_sequence"] for item in items)


def test_revoke_collector_denial_preserves_pending_original_outcomes_and_pins(delivered_setup):
    setup = delivered_setup
    setup["harness"].fail = "prompt"
    assert setup["bridge"].tick()["state"] == "stop_acknowledged"
    scope = (setup["mapping"]["tenant"], setup["mapping"]["sender_id"], setup["mapping"]["task_id"])
    before = {
        r["envelope"]["record"]["record_id"]: r["envelope"]
        for r in setup["peer"].provenance_outbox(*scope)
    }
    post(
        setup["setup_client"],
        setup["owner"],
        f"/agent-authority-grants/{setup['grant']['id']}/revoke",
        {"expected_revision": 1, "expected_epoch": 1},
        expected=200,
    )
    result = drain_outbox(setup["peer"], setup["mapping"], setup["delivery"])
    assert result["pending"] == 3 and result["acknowledged"] == 1
    after = setup["peer"].provenance_outbox(*scope)
    assert {r["envelope"]["record"]["record_id"]: r["envelope"] for r in after} == before
    assert any(
        r["delivery_code"] == "collector_rejected" and r["last_delivery"] == "rejected"
        for r in after
    )
    assert any(
        r["envelope"]["record"]["outcome"] == "unknown"
        for r in after
        if r["delivery_state"] == "pending"
    )


def test_cli_drain_does_not_require_harness_or_prompt_or_runtime_state(
    delivered_setup, monkeypatch, capsys
):
    setup = delivered_setup
    setup["harness"].observation["outcome"] = "succeeded"
    setup["bridge"].tick()
    root = setup["tmp_path"]
    paths = {
        "profile.json": setup["raw"],
        "mapping.json": json.dumps(setup["mapping"]).encode(),
        "peer.json": json.dumps(setup["config"]).encode(),
    }
    for name, content in paths.items():
        path = root / name
        path.write_bytes(content)
        path.chmod(0o600)
    monkeypatch.setenv("TESSERA_TEST_CRITIC_DELIVERY_TOKEN", setup["token"])
    monkeypatch.delenv("TESSERA_TEST_UNAVAILABLE_HARNESS_TOKEN", raising=False)
    assert (
        main(
            [
                "drain-outbox",
                "--mapping",
                str(root / "mapping.json"),
                "--peer-config",
                str(root / "peer.json"),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert setup["token"] not in output
    assert json.loads(output)["outbox_fully_acknowledged"] is True
    assert not (root / "prompt.txt").exists()


def test_delivery_body_mismatch_never_marks_acknowledged(delivered_setup):
    setup = delivered_setup
    setup["bridge"].tick()
    scope = (setup["mapping"]["tenant"], setup["mapping"]["sender_id"], setup["mapping"]["task_id"])
    envelope = setup["peer"].provenance_outbox(*scope)[0]["envelope"]
    payload = {key: deepcopy(envelope[key]) for key in ("record", "grant_proof")}
    payload["record"]["outcome"] = "succeeded"
    with pytest.raises(PeerConflict):
        setup["peer"].provenance_delivery(
            *scope,
            payload,
            DeliveryResult(
                "accepted", "collector_acknowledged", payload["record"]["record_id"], 201, 1
            ),
        )
    assert setup["peer"].provenance_summary(*scope)["acknowledged"] == 0


def test_expired_collector_credential_retains_pending_and_original_unknown(delivered_setup):
    setup = delivered_setup
    setup["harness"].fail = "prompt"
    setup["bridge"].tick()
    token = secrets.token_urlsafe(32)
    post(
        setup["setup_client"],
        setup["owner"],
        "/credentials",
        {
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": setup["project"],
            "expires_at": (datetime.now(UTC) + timedelta(seconds=2)).isoformat(),
            "external_agent": {"destination": "openai:personal", "actions": ["provenance:append"]},
        },
    )
    time.sleep(2.1)
    scope = (setup["mapping"]["tenant"], setup["mapping"]["sender_id"], setup["mapping"]["task_id"])
    before = [item["envelope"] for item in setup["peer"].provenance_outbox(*scope)]
    with ProvenanceClient(setup["origin"], token, "openai:personal") as expired:
        result = drain_outbox(setup["peer"], setup["mapping"], expired)
    assert result["acknowledged"] == 0 and result["pending"] == len(before)
    after = setup["peer"].provenance_outbox(*scope)
    assert [item["envelope"] for item in after] == before
    assert all(item["last_delivery"] == "rejected" for item in after)
    with setup["peer"].connect() as db:
        assert {r[0] for r in db.execute("SELECT status_code FROM peer_provenance_delivery")} == {
            401
        }


def test_bounded_pass_reports_remaining_not_complete(delivered_setup):
    setup = delivered_setup
    setup["harness"].observation["outcome"] = "succeeded"
    setup["bridge"].tick()
    result = drain_outbox(setup["peer"], setup["mapping"], setup["delivery"], limit=2)
    assert result["acknowledged"] == 2 and result["pending"] == 2
    assert result["outbox_fully_acknowledged"] is False
    assert drain_outbox(setup["peer"], setup["mapping"], setup["delivery"], limit=2)["pending"] == 0


def test_invalid_acknowledgement_stays_pending(delivered_setup):
    setup = delivered_setup
    setup["bridge"].tick()
    scope = (setup["mapping"]["tenant"], setup["mapping"]["sender_id"], setup["mapping"]["task_id"])
    envelope = setup["peer"].provenance_outbox(*scope)[0]["envelope"]
    payload = {key: envelope[key] for key in ("record", "grant_proof")}
    setup["peer"].provenance_delivery(
        *scope,
        payload,
        DeliveryResult("accepted", "collector_acknowledged", "wrong-record", 201, 1),
    )
    item = setup["peer"].provenance_outbox(*scope)[0]
    assert item["delivery_state"] == "pending" and item["last_delivery"] == "unknown"
    assert item["delivery_code"] == "invalid_acknowledgement" and item["receipt_sequence"] is None
