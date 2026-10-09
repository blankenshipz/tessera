"""Historical resource export permission never implies project provenance disclosure."""

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import post
from test_goal_to_decision import _design

from tessera_control.agent_provenance import record_agent_provenance
from tessera_control.api import create_app
from tessera_control.store import Store, _connect, initialize_state


@pytest.mark.parametrize("kind", ["experiment", "study"])
def test_legacy_scoped_export_does_not_disclose_project_notebook(tmp_path, kind):
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    project = post(client, owner, "/projects", {"name": "privacy"})
    token = secrets.token_urlsafe(32)
    post(
        client,
        owner,
        "/credentials",
        {
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    agent = {"Authorization": "Bearer " + token}
    if kind == "experiment":
        resource = post(client, owner, f"/projects/{project['id']}/experiments", _design())
        path = f"/experiments/{resource['id']}/export"
    else:
        resource = post(
            client,
            owner,
            f"/projects/{project['id']}/studies",
            {
                "purpose": "read resource",
                "protocol": "bounded",
                "mode": "exploratory",
            },
        )
        path = f"/studies/{resource['id']}/export"
    human = Store(state).authenticate(owner["Authorization"].split()[1])
    conn = _connect(state / "tessera.sqlite3")
    try:
        conn.execute("BEGIN IMMEDIATE")
        record_agent_provenance(
            conn,
            human,
            project["id"],
            {
                "record_id": "private_project_marker",
                "kind": "model",
                "outcome": "unknown",
                "correlation_id": "private_project_run",
                "model_id": "private_model_identity",
            },
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    human_export = client.get(path, headers=owner)
    assert human_export.status_code == 200, human_export.text
    assert "private_project_marker" in human_export.text
    exported = client.get(path, headers=agent)
    assert exported.status_code == 200, exported.text
    assert exported.json()["agent_notebook"] == {
        "availability": "unavailable",
        "reason": "separate_project_notebook_authority_required",
        "completeness": "not_disclosed",
    }
    assert "private_project_marker" not in exported.text
    assert "private_model_identity" not in exported.text
    assert client.get(f"/projects/{project['id']}/agent-notebook", headers=agent).status_code == 403
