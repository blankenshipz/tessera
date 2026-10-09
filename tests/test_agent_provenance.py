"""Local SQLite acceptance, not live agent/provider execution evidence."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tessera_control.agent_provenance import (
    create_agent_provenance_schema,
    project_agent_notebook,
    project_external_agent_notebook,
    record_agent_provenance,
)
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    canonical_digest,
    initialize_state,
)


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    store = Store(state)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    actor = store.authenticate(token)
    body = {"name": "Provenance"}
    _, project = store.create_project(actor, "project", canonical_digest(body), body)
    conn = sqlite3.connect(store.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    create_agent_provenance_schema(conn)
    conn.commit()
    yield conn, actor, project["id"], store.database_path
    conn.close()


def record(record_id="provenance_1", **kwargs):
    return {
        "record_id": record_id,
        "kind": "tool",
        "outcome": "succeeded",
        "correlation_id": "command_1",
        "tool_id": "research.attempt.admit",
        "references": {"attempt_id": "attempt_1", "grant_id": "grant_1"},
        **kwargs,
    }


def test_atomic_schema_and_record_restart_idempotency(setup):
    conn, actor, project, path = setup
    conn.execute("BEGIN IMMEDIATE")
    create_agent_provenance_schema(conn)
    assert conn.in_transaction
    first = record_agent_provenance(conn, actor, project, record())
    assert conn.in_transaction
    assert record_agent_provenance(conn, actor, project, record()) == first
    with pytest.raises(Conflict):
        record_agent_provenance(conn, actor, project, record(outcome="failed"))
    conn.commit()
    with sqlite3.connect(path) as resumed:
        resumed.row_factory = sqlite3.Row
        resumed.execute("BEGIN")
        assert record_agent_provenance(resumed, actor, project, record()) == first
        second = record_agent_provenance(resumed, actor, project, record("provenance_2"))
        assert second["sequence"] > first["sequence"]
    conn.execute("BEGIN IMMEDIATE")
    record_agent_provenance(conn, actor, project, record("rolled_back"))
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM agent_provenance").fetchone()[0] == 2


def test_transaction_required_and_immutable(setup):
    conn, actor, project, _ = setup
    with pytest.raises(RuntimeError):
        record_agent_provenance(conn, actor, project, record())
    with pytest.raises(RuntimeError):
        project_agent_notebook(conn, actor, project)
    conn.execute("BEGIN IMMEDIATE")
    record_agent_provenance(conn, actor, project, record())
    for sql in (
        "DELETE FROM agent_provenance",
        "UPDATE agent_provenance SET recorded_at='changed'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", "private prompt"),
        ("chain_of_thought", "private reasoning"),
        ("secret", "not-a-real-secret"),
        ("tool_output", "private output"),
        ("tool_id", "https://host/path?token=unsafe"),
        ("model_id", "Bearer unsafe"),
        ("content_digest", "private content"),
        ("error_code", "arbitrary exception detail"),
        ("references", {"message_id": "private message content"}),
        ("references", {"token": "unsafe"}),
    ],
)
def test_reject_content_and_unknown_fields(setup, field, value):
    conn, actor, project, _ = setup
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises((ValueError, ValidationError)):
        record_agent_provenance(conn, actor, project, record(**{field: value}))
    assert conn.execute("SELECT COUNT(*) FROM agent_provenance").fetchone()[0] == 0


def test_project_and_tenant_fences_and_revoked_identity(setup):
    conn, actor, project, _ = setup
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(Forbidden):
        record_agent_provenance(conn, {**actor, "org_id": "other_org"}, project, record())
    with pytest.raises(NotFound):
        record_agent_provenance(conn, actor, "other_project", record())
    with pytest.raises(NotFound):
        project_agent_notebook(conn, actor, "other_project")
    conn.execute(
        "UPDATE principals SET revoked_at='2000-01-01T00:00:00Z' WHERE id=?", (actor["id"],)
    )
    with pytest.raises(Forbidden):
        project_agent_notebook(conn, actor, project)


def test_enrolled_agent_can_record_but_not_read_human_notebook(setup):
    conn, owner, project, path = setup
    store = Store(path.parent)
    body = {
        "kind": "agent",
        "audience": "control",
        "project_id": project,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    token = "synthetic-isolated-test-agent-token"
    store.issue_credential(owner, "agent", canonical_digest(body), body, token)
    agent = store.authenticate(token)
    other_body = {"name": "Other"}
    _, other = store.create_project(owner, "other", canonical_digest(other_body), other_body)
    conn.execute("BEGIN IMMEDIATE")
    result = record_agent_provenance(conn, agent, project, record())
    assert result["actor_principal_id"] == agent["id"]
    assert result["credential_id"] == agent["credential_id"]
    with pytest.raises(Forbidden):
        project_agent_notebook(conn, agent, project)
    with pytest.raises(NotFound):
        record_agent_provenance(conn, agent, other["id"], record("cross_project"))
    with pytest.raises(Conflict):
        record_agent_provenance(conn, owner, project, record())
    conn.commit()
    conn.execute("BEGIN")
    assert len(project_agent_notebook(conn, owner, other["id"])["provenance"]) == 0
    assert len(project_agent_notebook(conn, owner, project)["provenance"]) == 1


def test_mixed_outcomes_event_join_and_completeness(setup):
    conn, actor, project, _ = setup
    conn.execute("BEGIN IMMEDIATE")
    outcomes = ["started", "succeeded", "failed", "denied", "unknown", "canceled"]
    kinds = ["agent", "model", "tool", "permission", "message", "delegation"]
    for index, (outcome, kind) in enumerate(zip(outcomes, kinds, strict=True)):
        record_agent_provenance(
            conn,
            actor,
            project,
            record(
                f"record_{index}",
                outcome=outcome,
                kind=kind,
                model_id="openai.gpt-model",
                causation_id=f"record_{max(index - 1, 0)}",
                content_digest="sha256:" + "a" * 64,
            ),
        )
    for kind in ("research.registered", "agent_authority.consumed", "task.created"):
        Store._event(
            conn,
            actor,
            project,
            None,
            kind,
            1,
            {
                "study_id": "study_1",
                "consumption_id": "consumption_1",
                "prompt": "PRIVATE CONTENT",
                "message_id": "PRIVATE MESSAGE CONTENT",
            },
        )
    full = project_agent_notebook(conn, actor, project)
    assert full["watermark"]["sequence"] == 6
    assert {item["outcome"] for item in full["provenance"]} == set(outcomes)
    assert {event["event_type"] for event in full["control_events"]} >= {
        "research.registered",
        "agent_authority.consumed",
        "task.created",
    }
    assert "PRIVATE" not in json.dumps(full)
    assert full["provenance"][0]["model_attribution"] == "self_reported"
    assert full["completeness"] == "complete_recorded_project_metadata_snapshot"
    assert full["scientific_validity"] == "not_assessed"
    page = project_agent_notebook(conn, actor, project, limit=2)
    assert page["has_more"] and page["completeness"] == "partial_page"
    sequences = [item["sequence"] for item in page["provenance"]]
    while page["has_more"]:
        page = project_agent_notebook(conn, actor, project, limit=2, **page["next"])
        sequences.extend(item["sequence"] for item in page["provenance"])
    assert sequences == list(range(1, 7))
    assert page["completeness"] == "partial_page"
    with pytest.raises(Conflict, match="byte cap"):
        project_agent_notebook(conn, actor, project, max_bytes=1024)


def _external_agent(setup, actions=("notebook:read",)):
    conn, owner, project, path = setup
    store = Store(path.parent)
    body = {
        "kind": "agent",
        "audience": "control",
        "project_id": project,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    token = "synthetic-external-notebook-test-token"
    store.issue_credential(owner, "external-notebook", canonical_digest(body), body, token)
    agent = store.authenticate(token)
    # Seed the reserved additive enrollment action in the isolated DB. The backend
    # owns its HTTP/enrollment Literal integration; this is not a live enrollment.
    if actions is not None:
        conn.execute(
            "INSERT INTO external_agent_scopes (credential_id,actions_json,destination) "
            "VALUES (?,?,?)",
            (agent["credential_id"], json.dumps(actions), "planner.local"),
        )
        conn.commit()
    return {**agent, "agent_destination": "planner.local"}


@pytest.mark.parametrize("actions", [None, ["context:read"], ["authority:consume"]])
def test_external_notebook_requires_explicit_enrollment(setup, actions):
    conn, _, project, _ = setup
    agent = _external_agent(setup, actions)
    conn.execute("BEGIN")
    with pytest.raises(Forbidden, match="explicit external-agent scope"):
        project_external_agent_notebook(conn, agent, project)


@pytest.mark.parametrize("destination", [None, "other.local", ""])
def test_external_notebook_destination_fence(setup, destination):
    conn, _, project, _ = setup
    agent = _external_agent(setup)
    conn.execute("BEGIN")
    with pytest.raises(Forbidden, match="destination"):
        project_external_agent_notebook(conn, {**agent, "agent_destination": destination}, project)


@pytest.mark.parametrize("fence", ["expired", "revoked", "parent_revoked", "foreign_org"])
def test_external_notebook_live_credential_fences(setup, fence):
    conn, owner, project, _ = setup
    agent = _external_agent(setup)
    conn.execute("BEGIN IMMEDIATE")
    if fence == "expired":
        conn.execute(
            "UPDATE credentials SET expires_at=? WHERE id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                agent["credential_id"],
            ),
        )
    elif fence == "revoked":
        conn.execute(
            "UPDATE credentials SET revoked_at=? WHERE id=?",
            (
                datetime.now(UTC).isoformat(),
                agent["credential_id"],
            ),
        )
    elif fence == "parent_revoked":
        conn.execute(
            "UPDATE credentials SET revoked_at=? WHERE id=?",
            (
                datetime.now(UTC).isoformat(),
                owner["credential_id"],
            ),
        )
    else:
        agent = {**agent, "org_id": "foreign_org"}
    with pytest.raises(Forbidden):
        project_external_agent_notebook(conn, agent, project)


def test_external_notebook_identity_and_project_fences(setup):
    conn, owner, project, path = setup
    agent = _external_agent(setup)
    store = Store(path.parent)
    body = {"name": "Other project"}
    _, other = store.create_project(owner, "other-notebook", canonical_digest(body), body)
    conn.execute("BEGIN")
    with pytest.raises(Forbidden):
        project_external_agent_notebook(conn, agent, other["id"])
    with pytest.raises(Forbidden):
        project_external_agent_notebook(
            conn, {**owner, "agent_destination": "planner.local"}, project
        )
    # Even an explicitly authorized external agent cannot enter the human path.
    with pytest.raises(Forbidden):
        project_agent_notebook(conn, agent, project)


def test_external_notebook_projection_pagination_redaction_caps_and_restart(setup):
    conn, owner, project, path = setup
    agent = _external_agent(setup)
    conn.execute("BEGIN IMMEDIATE")
    for index in range(4):
        record_agent_provenance(conn, owner, project, record(f"external_record_{index}"))
        Store._event(
            conn,
            owner,
            project,
            None,
            "agent_authority.consumed",
            1,
            {
                "consumption_id": f"consumption_{index}",
                "prompt": "PRIVATE CONTENT",
            },
        )
    conn.commit()
    with pytest.raises(RuntimeError):
        project_external_agent_notebook(conn, agent, project)
    with sqlite3.connect(path) as resumed:
        resumed.row_factory = sqlite3.Row
        resumed.execute("BEGIN")
        full = project_external_agent_notebook(resumed, agent, project)
        human = project_agent_notebook(resumed, owner, project)
        assert full == human
        assert "PRIVATE" not in json.dumps(full)
        assert full["completeness"] == "complete_recorded_project_metadata_snapshot"
        page = project_external_agent_notebook(resumed, agent, project, limit=1)
        sequences = [row["sequence"] for row in page["provenance"]]
        cursors = [row["cursor"] for row in page["control_events"]]
        while page["has_more"]:
            page = project_external_agent_notebook(resumed, agent, project, limit=1, **page["next"])
            assert page["watermark"] == full["watermark"]
            sequences.extend(row["sequence"] for row in page["provenance"])
            cursors.extend(row["cursor"] for row in page["control_events"])
        assert sequences == [row["sequence"] for row in full["provenance"]]
        assert cursors == [row["cursor"] for row in full["control_events"]]
        assert page["completeness"] == "partial_page"
        with pytest.raises(Conflict, match="byte cap"):
            project_external_agent_notebook(resumed, agent, project, max_bytes=1024)
        for kwargs in ({"limit": 0}, {"after_sequence": -1}, {"max_bytes": 0}):
            with pytest.raises(ValueError):
                project_external_agent_notebook(resumed, agent, project, **kwargs)
