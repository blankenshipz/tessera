"""Ordinary app enrollment/intake/export seam, not duplicate collector unit coverage."""

import sqlite3
from copy import deepcopy

import pytest
from test_agent_authority import (  # noqa: F401
    assert_empty_runtime_v55,
    enroll,
    issue,
    post,
    prepared,
    strip_runtime_v55,
)

from tessera_control.api import create_app
from tessera_control.store import initialize_state, migrate_state


@pytest.fixture
def collector(request):
    setup = request.getfixturevalue("prepared")
    state, client, owner, _, project, bounds, _ = setup
    scopes = [
        "context:read",
        "proposal:create",
        "proposal:read",
        "authority:request",
        "authority:consume",
        "observation:create",
        "review:request",
        "notebook:read",
        "provenance:append",
    ]
    agent, credential = enroll(client, owner, project, scopes)
    bounds = deepcopy(bounds)
    bounds["subject_credential_id"] = credential["credential_id"]
    grant = issue(setup, bounds)
    body = {
        "record": {
            "record_id": "runtime_record_1",
            "kind": "model",
            "outcome": "unknown",
            "correlation_id": "runtime_run_1",
            "model_id": "model-1",
            "error_code": "outcome_unknown",
            "references": {"grant_id": grant["id"], "task_id": bounds["bindings"][0]["task_id"]},
            "remote": {"task_id": "remote_task_1", "peer_id": "critic_peer_1"},
        },
        "grant_proof": {
            "grant_id": grant["id"],
            "expected_revision": grant["revision"],
            "expected_epoch": grant["epoch"],
        },
    }
    return state, client, owner, agent, credential, project, grant, body


def test_registered_collector_enrollment_append_replay_and_notebook_export(collector):
    state, client, _, agent, _, project, grant, body = collector
    assert client.get("/health/ready").status_code == 200
    path = f"/projects/{project}/agent-provenance"
    recorded = post(client, agent, path, body, key="outbox-record")
    assert recorded["record_id"] == "runtime_record_1" and recorded["outcome"] == "unknown"
    assert recorded["grant_revision"] == grant["revision"]
    assert recorded["remote"]["destination_digest"].startswith("sha256:")
    assert post(client, agent, path, body, key="outbox-record") == recorded
    changed = deepcopy(body)
    changed["record"]["outcome"] = "succeeded"
    post(client, agent, path, changed, key="outbox-record", expected=409)
    for route in ("agent-notebook", "agent-notebook/export"):
        response = client.get(f"/projects/{project}/{route}", headers=agent)
        assert response.status_code == 200, response.text
        match = next(
            r for r in response.json()["provenance"] if r["record_id"] == recorded["record_id"]
        )
        assert match["references"]["grant_id"] == grant["id"] and match["outcome"] == "unknown"
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM agent_provenance WHERE record_id='runtime_record_1'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='agent_provenance.recorded'"
            ).fetchone()[0]
            == 1
        )


def test_collector_no_scope_or_destination_fallback_and_no_human_proxy(collector):
    _, client, owner, agent, _, project, _, body = collector
    path = f"/projects/{project}/agent-provenance"
    post(client, {**agent, "X-Agent-Destination": "other"}, path, body, expected=403)
    legacy, _ = enroll(client, owner, project, ["context:read", "notebook:read"])
    post(client, legacy, path, body, expected=403)
    post(client, owner, path, body, expected=403)
    assert client.get(f"/projects/{project}/agent-notebook", headers=legacy).status_code == 200


def test_collector_cross_project_subject_stale_grant_and_revoked_credential(collector):
    _, client, owner, agent, credential, project, grant, body = collector
    other = post(client, owner, "/projects", {"name": "separate"})
    peer, _ = enroll(client, owner, other["id"], ["provenance:append", "notebook:read"])
    path = f"/projects/{project}/agent-provenance"
    post(client, peer, path, body, expected=403)
    post(client, agent, f"/projects/{other['id']}/agent-provenance", body, expected=403)
    same_project_peer, _ = enroll(client, owner, project, ["provenance:append", "notebook:read"])
    post(client, same_project_peer, path, body, expected=403)
    post(
        client,
        owner,
        f"/agent-authority-grants/{grant['id']}/revoke",
        {"expected_revision": 1, "expected_epoch": 1},
        expected=200,
    )
    post(client, agent, path, body, expected=409)
    denied = deepcopy(body)
    denied["record"].update(
        record_id="runtime_record_denied", outcome="denied", error_code="revoked"
    )
    denied["grant_proof"].update(expected_revision=2, expected_epoch=2)
    assert post(client, agent, path, denied)["outcome"] == "denied"
    client.post(
        f"/credentials/{credential['credential_id']}/revoke",
        headers={**owner, "Idempotency-Key": "revoke-collector"},
    )
    post(client, agent, path, denied, expected=401)
    assert client.get(f"/projects/{project}/agent-notebook", headers=agent).status_code == 401


def test_collector_schema51_readiness_and_existing50_explicit_migration(tmp_path):
    from fastapi.testclient import TestClient

    state = tmp_path / "state"
    initialize_state(state)
    assert TestClient(create_app(state)).get("/health/ready").status_code == 200
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        strip_runtime_v55(db)
        db.execute("UPDATE schema_meta SET schema_version=50")
    old = TestClient(create_app(state))
    assert old.get("/health/ready").status_code == 503
    assert migrate_state(state) == 55
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert_empty_runtime_v55(db)
    assert TestClient(create_app(state)).get("/health/ready").status_code == 200
