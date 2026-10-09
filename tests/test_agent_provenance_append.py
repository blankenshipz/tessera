"""Real local HTTP/SQLite with isolated synthetic enrolled records, not live peers."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tessera_control.agent_authority import create_agent_authority_schema
from tessera_control.agent_provenance import create_agent_provenance_schema
from tessera_control.agent_provenance_api import router
from tessera_control.agent_provenance_store import AgentProvenanceStore
from tessera_control.api import create_app
from tessera_control.store import Store, canonical_digest, initialize_state


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    store = Store(state)
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    owner = store.authenticate(owner_token)
    projects = []
    for name in ("Here", "Elsewhere"):
        body = {"name": name}
        _, project = store.create_project(owner, name, canonical_digest(body), body)
        projects.append(project["id"])
    token = "synthetic-provenance-append-test-token"
    body = {
        "kind": "agent",
        "audience": "control",
        "project_id": projects[0],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    store.issue_credential(owner, "agent", canonical_digest(body), body, token)
    agent = store.authenticate(token)
    conn = sqlite3.connect(store.database_path)
    conn.row_factory = sqlite3.Row
    create_agent_authority_schema(conn)
    create_agent_provenance_schema(conn)
    conn.execute(
        "INSERT INTO external_agent_scopes VALUES (?,?,?)",
        (
            agent["credential_id"],
            '["provenance:append"]',
            "peer.local",
        ),
    )
    for project, context in zip(projects, ("context_here", "context_foreign"), strict=True):
        conn.execute(
            "INSERT INTO external_agent_contexts VALUES (?,?,?,?,?,?,?)",
            (
                context,
                project,
                agent["credential_id"],
                "peer.local",
                "{}",
                owner["id"],
                datetime.now(UTC).isoformat(),
            ),
        )
    conn.execute(
        """INSERT INTO agent_authority_grants
        (id,org_id,project_id,subject_credential_id,issuer_id,bounds_json,status,
        revision,epoch,reserved_cents,created_at,updated_at)
        VALUES (?,?,?,?,?,?,'revoked',2,2,0,?,?)""",
        (
            "grant_here",
            owner["org_id"],
            projects[0],
            agent["credential_id"],
            owner["id"],
            '{"destination":"peer.local"}',
            datetime.now(UTC).isoformat(),
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
    app = create_app(state)
    app.state.agent_provenance = AgentProvenanceStore(state)
    app.include_router(router)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Destination": "peer.local",
        "Idempotency-Key": "append_1",
    }
    with TestClient(app) as client:
        yield client, conn, projects, agent, owner_token, headers
    conn.close()


def body(**changes):
    return {
        "record": {
            "record_id": "remote_record_1",
            "kind": "message",
            "outcome": "unknown",
            "correlation_id": "remote_task_1",
            "model_id": "self.reported-model",
            "references": {"context_id": "context_here"},
            "remote": {
                "task_id": "peer_task_1",
                "message_id": "peer_message_1",
                "peer_id": "peer_1",
            },
            **changes,
        }
    }


def post(setup, payload=None, headers=None, project=None):
    client, _, projects, _, _, standard_headers = setup
    return client.post(
        f"/projects/{project or projects[0]}/agent-provenance",
        json=payload or body(),
        headers=headers or standard_headers,
    )


def test_append_atomic_replay_and_notebook_link(setup):
    _, conn, _, _, _, headers = setup
    first = post(setup)
    assert first.status_code == 201, first.text
    assert first.json()["model_attribution"] == "self_reported"
    assert first.json()["remote"]["destination_digest"].startswith("sha256:")
    assert post(setup).json() == first.json()
    assert post(setup, body(outcome="failed")).status_code == 409
    assert post(setup, headers={**headers, "Idempotency-Key": "another_key"}).json() == first.json()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM agent_provenance WHERE record_id='remote_record_1'"
        ).fetchone()[0]
        == 1
    )
    event = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='agent_provenance.recorded'"
    ).fetchall()
    assert len(event) == 1
    assert json.loads(event[0][0])["provenance_record_id"] == first.json()["record_id"]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE path LIKE '%/agent-provenance'"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize(
    "refs",
    [
        {},
        {"context_id": "context_foreign"},
        {"task_id": "missing_task"},
        {"artifact_id": "foreign_artifact"},
        {"attempt_id": "foreign_attempt"},
        {"message_id": "peer_message_1"},
        {"grant_id": "grant_here"},
    ],
)
def test_unbound_or_foreign_references_denied_atomically(setup, refs):
    assert post(setup, body(references=refs)).status_code == 403
    conn = setup[1]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM agent_provenance WHERE record_id='remote_record_1'"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE path LIKE '%/agent-provenance'"
        ).fetchone()[0]
        == 0
    )


def test_revoked_grant_is_honest_denial_not_authority(setup):
    payload = body(
        kind="permission",
        outcome="denied",
        error_code="revoked",
        references={"grant_id": "grant_here"},
    )
    payload["grant_proof"] = {"grant_id": "grant_here", "expected_revision": 2, "expected_epoch": 2}
    result = post(setup, payload)
    assert result.status_code == 201
    assert (result.json()["grant_revision"], result.json()["grant_epoch"]) == (2, 2)
    conn = setup[1]
    assert (
        conn.execute("SELECT status FROM agent_authority_grants WHERE id='grant_here'").fetchone()[
            0
        ]
        == "revoked"
    )
    assert conn.execute("SELECT COUNT(*) FROM agent_authority_consumptions").fetchone()[0] == 0
    payload["grant_proof"]["expected_epoch"] = 1
    assert (
        post(setup, payload, headers={**setup[-1], "Idempotency-Key": "stale"}).status_code == 409
    )


def test_sqlite_event_failure_rolls_back_record_and_idempotency(setup):
    conn = setup[1]
    conn.execute("""CREATE TRIGGER reject_provenance_event BEFORE INSERT ON events
        WHEN NEW.event_type='agent_provenance.recorded' BEGIN
        SELECT RAISE(ABORT, 'isolated atomicity fixture'); END""")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="atomicity fixture"):
        post(setup)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM agent_provenance WHERE record_id='remote_record_1'"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE path LIKE '%/agent-provenance'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "change", ["destination", "project", "expired", "revoked", "owner", "scope"]
)
def test_live_identity_scope_and_replay_fences(setup, change):
    _, conn, projects, agent, owner_token, headers = setup
    assert post(setup).status_code == 201
    if change == "destination":
        headers = {**headers, "X-Agent-Destination": "other.local"}
    elif change == "project":
        assert post(setup, project=projects[1]).status_code == 403
        return
    elif change == "owner":
        headers = {**headers, "Authorization": f"Bearer {owner_token}"}
    elif change == "scope":
        # Immutable enrollment cannot be changed in production; removing/replacing
        # its row here is only a negative corrupted-state fixture.
        conn.execute("DROP TRIGGER external_agent_scopes_no_update")
        conn.execute("UPDATE external_agent_scopes SET actions_json='[\"context:read\"]'")
        conn.commit()
    else:
        field = "expires_at" if change == "expired" else "revoked_at"
        conn.execute(
            f"UPDATE credentials SET {field}=? WHERE id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                agent["credential_id"],
            ),
        )
        conn.commit()
    assert post(setup, headers=headers).status_code in {401, 403}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM agent_provenance WHERE record_id='remote_record_1'"
        ).fetchone()[0]
        == 1
    )


def test_strict_redacted_validation_and_body_limit(setup):
    for payload in (
        body(prompt="PRIVATE CONTENT"),
        body(outcome="approved"),
        body(tool_id="human.approve"),
        body(remote={"message_id": "PRIVATE CONTENT"}),
    ):
        response = post(setup, payload)
        assert response.status_code in {403, 422}
        assert "PRIVATE CONTENT" not in response.text
    client, conn, projects, _, _, headers = setup
    response = client.post(
        f"/projects/{projects[0]}/agent-provenance",
        content=b" " * 16385,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert (
        post(
            setup,
            headers={key: value for key, value in headers.items() if key != "Idempotency-Key"},
        ).status_code
        == 400
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM agent_provenance WHERE record_id='remote_record_1'"
        ).fetchone()[0]
        == 0
    )
