"""Owner decline, one-step interactive review, and the v58 decline migration."""

import argparse
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import post
from test_agent_authority import prepared as prepared  # noqa: F401  (fixture)

from tessera_control import agent_research_review as review_module
from tessera_control.api import create_app
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.store import Store

STUDY = {
    "proposal": {
        "kind": "study",
        "study": {"purpose": "draft", "protocol": "pending", "mode": "exploratory"},
    },
    "provenance": {"client": "test", "model": "attested", "session_id": "review"},
}


def propose(client, agent, project):
    return post(client, agent, f"/projects/{project}/agent-workflow-proposals", STUDY)


def body(record, **extra):
    return {"expected_revision": 1, "expected_digest": record["digest"], **extra}


def decline(client, actor, record, expected=201, key=None, rationale="not now"):
    return post(
        client,
        actor,
        f"/agent-research-proposals/{record['id']}/decline",
        body(record, rationale=rationale),
        key=key,
        expected=expected,
    )


def promote(client, actor, record, expected=201):
    return post(
        client,
        actor,
        f"/agent-research-proposals/{record['id']}/promote",
        body(record),
        expected=expected,
    )


def test_decline_visible_to_owner_and_agent_and_blocks_promote(prepared):
    state, client, owner, agent, project, _, _ = prepared
    record = propose(client, agent, project)
    result = decline(client, owner, record, key="decline-1")
    assert result["declined"] is True and result["credential_kind"] == "local_owner"
    assert decline(client, owner, record, expected=201, key="decline-1") == result
    for actor in (owner, agent):
        read = client.get(f"/agent-research-proposals/{record['id']}", headers=actor).json()
        assert read["decline"]["rationale"] == "not now"
        assert read["promotion"] is None
    promote(client, owner, record, expected=409)
    # A fresh key with the same decline returns the same durable record.
    again = decline(client, owner, record, expected=200)
    assert again == result
    decline(client, owner, record, expected=409, rationale="different")


def test_decline_after_promote_and_digest_mismatch(prepared):
    state, client, owner, agent, project, _, _ = prepared
    record = propose(client, agent, project)
    bad = {**body(record, rationale="x"), "expected_digest": "0" * 64}
    post(client, owner, f"/agent-research-proposals/{record['id']}/decline", bad, expected=409)
    promote(client, owner, record)
    decline(client, owner, record, expected=409)


def test_agent_cannot_decide(prepared):
    state, client, owner, agent, project, _, _ = prepared
    record = propose(client, agent, project)
    decline(client, agent, record, expected=403)
    promote(client, agent, record, expected=403)
    read = client.get(f"/agent-research-proposals/{record['id']}", headers=agent).json()
    assert read["promotion"] is None and "decline" not in read


def test_decline_is_event_backed_and_survives_new_app(prepared):
    state, client, owner, agent, project, _, _ = prepared
    record = propose(client, agent, project)
    decline(client, owner, record)
    with sqlite3.connect(Store(state).database_path) as db:
        assert db.execute(
            "SELECT count(*) FROM events WHERE event_type='agent_research.declined'"
        ).fetchone() == (1,)
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )
    fresh = TestClient(create_app(state))
    read = fresh.get(f"/agent-research-proposals/{record['id']}", headers=agent).json()
    assert read["decline"]["rationale"] == "not now"


# --- interactive CLI -------------------------------------------------------------


def transport_for(client, owner):
    def call(method, path, payload=None, key=None):
        headers = {**owner, **({"Idempotency-Key": key} if key else {})}
        response = client.request(method, path, headers=headers, json=payload)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    return call


def run(transport, proposal_id, answers, tty=True):
    lines, prompts = [], []
    supplied = iter(answers)

    def ask(prompt):
        prompts.append(prompt)
        return next(supplied)

    code = review_module.review(
        transport,
        proposal_id,
        input_fn=ask,
        isatty=lambda: tty,
        out=lines.append,
        key_fn=lambda: os.urandom(8).hex(),
    )
    return code, "\n".join(lines), prompts


def state_of(client, owner, record):
    return client.get(f"/agent-research-proposals/{record['id']}", headers=owner).json()


def test_review_approve_requires_digest_prefix(prepared):
    state, client, owner, agent, project, _, _ = prepared
    record = propose(client, agent, project)
    transport = transport_for(client, owner)
    code, output, _ = run(transport, record["id"], ["approve", "deadbeef"])
    assert code == 1 and state_of(client, owner, record)["promotion"] is None
    assert "untrusted" in output and record["digest"] in output
    code, output, _ = run(transport, record["id"], ["approve", record["digest"][:8]])
    assert code == 0 and "Approved" in output
    assert state_of(client, owner, record)["promotion"]["resource"]["id"] in output


def test_review_decline_skip_nontty_and_decided(prepared):
    state, client, owner, agent, project, _, _ = prepared
    transport = transport_for(client, owner)
    record = propose(client, agent, project)
    code, _, _ = run(transport, record["id"], ["skip"])
    assert code == 0 and state_of(client, owner, record)["promotion"] is None
    code, _, prompts = run(transport, record["id"], [], tty=False)
    assert code == 2 and prompts == []
    assert "decline" not in state_of(client, owner, record)
    code, _, _ = run(transport, record["id"], ["decline", "  "])
    assert code == 1 and "decline" not in state_of(client, owner, record)
    code, _, _ = run(transport, record["id"], ["decline", "out of scope"])
    assert code == 0
    assert state_of(client, owner, record)["decline"]["rationale"] == "out of scope"
    code, output, prompts = run(transport, record["id"], [])
    assert code == 0 and prompts == [] and "DECLINED" in output


def test_agent_credential_cannot_review(prepared):
    state, client, owner, agent, project, _, _ = prepared
    record = propose(client, agent, project)
    transport = transport_for(client, agent)
    code, _, _ = run(transport, record["id"], ["decline", "mine"])
    assert code == 1 and "decline" not in state_of(client, owner, record)


def test_cli_origin_and_header_checks(tmp_path):
    for bad in (
        "http://example.com:1",
        "https://127.0.0.1:1",
        "http://127.0.0.1",
        "http://127.0.0.1:1/x",
    ):
        with pytest.raises(review_module.ReviewError):
            review_module.control_origin(bad)
    assert review_module.control_origin("http://[::1]:9") == "http://[::1]:9"
    header = tmp_path / "owner.header"
    header.write_text("Authorization: Bearer secret\n")
    header.chmod(0o644)
    with pytest.raises(review_module.ReviewError):
        review_module.read_owner_header(header)
    header.chmod(0o600)
    assert review_module.read_owner_header(header) == "Bearer secret"


def test_run_agent_proposal_non_tty_exit_2(prepared, tmp_path, monkeypatch):
    state, client, owner, agent, project, _, _ = prepared
    record = propose(client, agent, project)
    header = tmp_path / "owner.header"
    header.write_text("Authorization: " + owner["Authorization"] + "\n")
    header.chmod(0o600)
    args = argparse.Namespace(
        proposal_action="review",
        proposal_id=record["id"],
        owner_header=header,
        control_url="http://127.0.0.1:1",
    )
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    code = review_module.run_agent_proposal(
        args, transport_factory=lambda origin, auth: transport_for(client, owner)
    )
    assert code == 2 and "decline" not in state_of(client, owner, record)
