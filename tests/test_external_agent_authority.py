"""Executable local authority tests; no provider inference or mocked API calls."""

import base64
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import assert_empty_runtime_v55, strip_runtime_v55
from test_artifact_custody import _released_manifest

from tessera_control.api import create_app
from tessera_control.models import CreateCredential
from tessera_control.store import Store, canonical_digest, initialize_state, migrate_state


def _enroll(client, owner, project_id, actions=None, destination="openai:personal"):
    token = secrets.token_urlsafe(32)
    body = {
        "token": token,
        "kind": "agent",
        "audience": "control",
        "project_id": project_id,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "external_agent": {
            "actions": actions or ["context:read", "proposal:create", "proposal:read"],
            "destination": destination,
        },
    }
    response = client.post(
        "/credentials", headers={**owner, "Idempotency-Key": secrets.token_hex(8)}, json=body
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {token}", "X-Agent-Destination": destination}, response.json()


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    client = TestClient(create_app(state))
    project = client.post(
        "/projects", headers={**owner, "Idempotency-Key": "project"}, json={"name": "Research"}
    ).json()
    agent, credential = _enroll(client, owner, project["id"])
    return state, client, owner, project["id"], agent, credential


def _plan():
    return {
        "proposal_type": "task_plan",
        "plan": {
            "title": "compare",
            "purpose": "bounded comparison",
            "budget_cents": 0,
            "max_duration_seconds": 60,
            "execution_spec": {
                "argv": ["python", "main.py"],
                "input_snapshot_digest": "a" * 64,
                "pinned_skill_digests": [],
                "output_paths": ["result.txt"],
                "profile": "local-cpu",
                "network_enabled": False,
                "max_output_bytes": 1024,
            },
        },
        "provenance": {"client": "stdio", "model": "caller-model", "session_id": "session-1"},
    }


def test_proposal_immutable_replay_and_separate_human_promotion(setup):
    state, client, owner, project, agent, _ = setup
    path = f"/projects/{project}/agent-proposals"
    headers = {**agent, "Idempotency-Key": "proposal"}
    response = client.post(path, headers=headers, json=_plan())
    assert response.status_code == 201, response.text
    proposal = response.json()
    assert proposal["authority_effect"] == "none"
    assert proposal["provenance_kind"] == "external_agent_caller_attested"
    assert "provider_disabled" not in proposal
    assert client.post(path, headers=headers, json=_plan()).json() == proposal
    changed = _plan()
    changed["plan"]["title"] = "replacement"
    assert client.post(path, headers=headers, json=changed).status_code == 409
    assert client.get(f"/agent-proposals/{proposal['id']}", headers=agent).status_code == 200
    promote_path = f"/agent-proposals/{proposal['id']}/promote"
    body = {"expected_revision": 1, "expected_digest": proposal["digest"]}
    assert (
        client.post(
            promote_path, headers={**agent, "Idempotency-Key": "self"}, json=body
        ).status_code
        == 403
    )
    assert (
        client.post(
            promote_path,
            headers={**owner, "Idempotency-Key": "stale"},
            json={**body, "expected_revision": 2},
        ).status_code
        == 409
    )
    promoted = client.post(promote_path, headers={**owner, "Idempotency-Key": "promote"}, json=body)
    assert promoted.status_code == 201, promoted.text
    assert (
        client.post(promote_path, headers={**owner, "Idempotency-Key": "promote"}, json=body).json()
        == promoted.json()
    )
    assert (
        client.post(promote_path, headers={**owner, "Idempotency-Key": "again"}, json=body).json()
        == promoted.json()
    )
    task = client.get(f"/tasks/{promoted.json()['task_id']}", headers=owner).json()
    assert task["status"] == "draft"
    assert task["execution_granted"] is False
    plan = Store(state).get_current_task_plan(
        Store(state).authenticate(owner["Authorization"].split()[1]),
        task["id"],
        task["current_plan_digest"],
    )
    assert plan["execution_spec"] == _plan()["plan"]["execution_spec"] | {"docker": None}
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE external_agent_proposals SET digest='changed'")
        assert db.execute("SELECT COUNT(*) FROM external_agent_promotions").fetchone()[0] == 1


def test_scope_destination_generic_authority_and_revocation(setup):
    state, client, owner, project, agent, credential = setup
    assert client.get("/external-agent/status", headers=agent).status_code == 200
    assert (
        client.get(
            "/external-agent/status", headers={"Authorization": agent["Authorization"]}
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/external-agent/status", headers={**agent, "X-Agent-Destination": "other"}
        ).status_code
        == 403
    )
    for path in (
        "/projects",
        "/credentials",
        "/operator/health",
        f"/projects/{project}/tasks",
        "/recovery",
    ):
        assert client.get(path, headers=agent).status_code == 403, path
    assert (
        client.post(
            f"/projects/{project}/tasks",
            headers={**agent, "Idempotency-Key": "task"},
            json={k: v for k, v in _plan()["plan"].items() if k != "execution_spec"},
        ).status_code
        == 403
    )
    readonly, _ = _enroll(client, owner, project, ["proposal:read"])
    path = f"/projects/{project}/agent-proposals"
    assert (
        client.post(path, headers={**readonly, "Idempotency-Key": "deny"}, json=_plan()).status_code
        == 403
    )
    proposal = client.post(path, headers={**agent, "Idempotency-Key": "one"}, json=_plan()).json()
    assert client.get(f"/agent-proposals/{proposal['id']}", headers=readonly).status_code == 404
    client.post(
        f"/credentials/{credential['credential_id']}/revoke",
        headers={**owner, "Idempotency-Key": "revoke"},
    )
    assert client.get("/external-agent/status", headers=agent).status_code == 401
    assert (
        client.post(path, headers={**agent, "Idempotency-Key": "one"}, json=_plan()).status_code
        == 401
    )


def test_curated_context_exact_recipient_and_expiry(setup):
    state, client, owner, project, agent, credential = setup
    body = {
        "credential_id": credential["credential_id"],
        "destination": "openai:personal",
        "title": "selected",
        "body": "untrusted paper text",
        "classification": "project",
        "disclosure_approved": True,
    }
    path = f"/projects/{project}/agent-contexts"
    assert (
        client.post(path, headers={**agent, "Idempotency-Key": "self"}, json=body).status_code
        == 403
    )
    assert (
        client.post(
            path,
            headers={**owner, "Idempotency-Key": "deny"},
            json={**body, "disclosure_approved": False},
        ).status_code
        == 422
    )
    response = client.post(path, headers={**owner, "Idempotency-Key": "release"}, json=body)
    assert response.status_code == 201, response.text
    context = response.json()
    assert (
        client.get(f"/agent-contexts/{context['id']}", headers=agent).json()["body"] == body["body"]
    )
    other, _ = _enroll(client, owner, project)
    assert client.get(f"/agent-contexts/{context['id']}", headers=other).status_code == 404
    assert client.get(path, headers=other).json() == {"contexts": []}
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE credentials SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), credential["credential_id"]),
        )
    assert client.get(f"/agent-contexts/{context['id']}", headers=agent).status_code == 401
    assert (
        client.post(path, headers={**owner, "Idempotency-Key": "release"}, json=body).status_code
        == 403
    )


def test_retained_artifact_disclosure_is_additional_and_revocable(tmp_path):
    state, human, _, manifest, first, _, custody = _released_manifest(tmp_path)
    token = (state / "owner.header").read_text().split("Bearer ")[1].strip()
    owner = {"Authorization": f"Bearer {token}"}
    client = TestClient(create_app(state))
    agent, credential = _enroll(client, owner, manifest["project_id"])
    # Existing human custody alone provides no external-agent access.
    assert client.get(
        f"/artifact-manifests/{manifest['id']}/custody", headers=agent
    ).status_code in (403, 404)
    assert client.get(
        f"/projects/{manifest['project_id']}/agent-contexts", headers=agent
    ).json() == {"contexts": []}
    body = {
        "credential_id": credential["credential_id"],
        "destination": "openai:personal",
        "title": "output",
        "classification": "project",
        "disclosure_approved": True,
        "manifest_id": manifest["id"],
        "artifact_id": first["id"],
        "expected_manifest_digest": manifest["manifest_digest"],
        "max_bytes": 64,
    }
    path = f"/projects/{manifest['project_id']}/agent-artifact-contexts"
    assert (
        client.post(
            path, headers={**owner, "Idempotency-Key": "small"}, json={**body, "max_bytes": 1}
        ).status_code
        == 409
    )
    result = client.post(path, headers={**owner, "Idempotency-Key": "delegate"}, json=body)
    assert result.status_code == 201, result.text
    context_path = f"/agent-contexts/{result.json()['id']}"
    read = client.get(context_path, headers=agent)
    assert read.status_code == 200, read.text
    assert base64.b64decode(read.json()["content_base64"]) == b"direct"
    assert read.json()["sha256"] == first["sha256"]
    assert (
        client.get(context_path, headers={**agent, "X-Agent-Destination": "other"}).status_code
        == 403
    )
    # Reads recheck actual placement and retention, not cached released bytes.
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE project_artifact_retention SET state='tombstoned' WHERE artifact_id=?",
            (first["id"],),
        )
    assert client.get(context_path, headers=agent).status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE project_artifact_retention SET state='active' WHERE artifact_id=?",
            (first["id"],),
        )
        db.execute(
            "UPDATE project_artifact_placements SET sha256=? WHERE artifact_id=?",
            ("f" * 64, first["id"]),
        )
    assert client.get(context_path, headers=agent).status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE project_artifact_placements SET sha256=? WHERE artifact_id=?",
            (first["sha256"], first["id"]),
        )
    revoke = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 1,
        "action": "revoke",
        "rationale": "stop disclosure",
    }
    custody.record_release(human, manifest["id"], "revoke", canonical_digest(revoke), revoke)
    assert client.get(context_path, headers=agent).status_code == 403
    # Withdrawal must not poison discovery of unrelated readable context.
    plain = client.post(
        f"/projects/{manifest['project_id']}/agent-contexts",
        headers={**owner, "Idempotency-Key": "still-readable"},
        json={
            "credential_id": credential["credential_id"],
            "destination": "openai:personal",
            "title": "safe",
            "body": "selected public note",
            "classification": "project",
            "disclosure_approved": True,
        },
    )
    assert plain.status_code == 201, plain.text
    listed = client.get(f"/projects/{manifest['project_id']}/agent-contexts", headers=agent)
    assert listed.status_code == 200, listed.text
    assert [record["id"] for record in listed.json()["contexts"]] == [plain.json()["id"]]
    assert (
        client.post(path, headers={**owner, "Idempotency-Key": "delegate"}, json=body).status_code
        == 403
    )
    rerelease = {**revoke, "expected_revision": 2, "action": "release"}
    custody.record_release(
        human, manifest["id"], "rerelease", canonical_digest(rerelease), rerelease
    )
    assert client.get(context_path, headers=agent).status_code == 403


def test_schema_rejects_agent_privilege_and_fake_provenance(setup):
    _, client, owner, project, agent, _ = setup
    with pytest.raises(ValueError):
        CreateCredential.model_validate(
            {
                "token": secrets.token_urlsafe(32),
                "kind": "agent",
                "audience": "control",
                "project_id": project,
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
                "external_agent": {"actions": ["approve"], "destination": "here"},
            }
        )
    plan = _plan()
    plan["provenance"]["provider_disabled"] = True
    assert (
        client.post(
            f"/projects/{project}/agent-proposals",
            headers={**agent, "Idempotency-Key": "fake"},
            json=plan,
        ).status_code
        == 422
    )


def test_same_worker_renewal_preserves_identity_and_never_revives(setup):
    state, client, owner, project, agent, _ = setup
    token = secrets.token_urlsafe(32)
    body = {
        "token": token,
        "kind": "worker",
        "audience": "worker",
        "project_id": project,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    credential = client.post(
        "/credentials", headers={**owner, "Idempotency-Key": "worker"}, json=body
    ).json()
    before = Store(state).authenticate_worker(token)
    path = f"/credentials/{credential['credential_id']}/renew"
    renewal = {"expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    assert (
        client.post(path, headers={**agent, "Idempotency-Key": "self"}, json=renewal).status_code
        == 403
    )
    response = client.post(path, headers={**owner, "Idempotency-Key": "renew"}, json=renewal)
    assert response.status_code == 200, response.text
    after = Store(state).authenticate_worker(token)
    assert before["id"] == after["id"]
    assert before["credential_id"] == after["credential_id"]
    assert after["expires_at"] > before["expires_at"]
    assert (
        client.post(path, headers={**owner, "Idempotency-Key": "renew"}, json=renewal).json()
        == response.json()
    )
    client.post(
        f"/credentials/{credential['credential_id']}/revoke",
        headers={**owner, "Idempotency-Key": "revoke"},
    )
    assert (
        client.post(path, headers={**owner, "Idempotency-Key": "renew"}, json=renewal).status_code
        == 403
    )


def test_cross_project_tenant_parent_revocation_and_atomic_denial(setup):
    state, client, owner, project, agent, credential = setup
    other = client.post(
        "/projects", headers={**owner, "Idempotency-Key": "other"}, json={"name": "Other"}
    ).json()["id"]
    path = f"/projects/{project}/agent-proposals"
    response = client.post(path, headers={**agent, "Idempotency-Key": "original"}, json=_plan())
    proposal = response.json()
    assert response.status_code == 201
    assert (
        client.post(
            f"/projects/{other}/agent-proposals",
            headers={**agent, "Idempotency-Key": "wrong-project"},
            json=_plan(),
        ).status_code
        == 404
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        # Denied commands leave no proposal, idempotency record or success event.
        assert db.execute("SELECT COUNT(*) FROM external_agent_proposals").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM idempotency WHERE idempotency_key='wrong-project'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='external_agent.proposal_created'"
            ).fetchone()[0]
            == 1
        )
        db.execute(
            "INSERT INTO organizations VALUES ('org_foreign','foreign',?)",
            (datetime.now(UTC).isoformat(),),
        )
        db.execute(
            "INSERT INTO projects VALUES ('project_foreign','org_foreign','foreign',?)",
            (datetime.now(UTC).isoformat(),),
        )
    assert client.get("/projects/project_foreign/agent-proposals", headers=owner).status_code == 404
    assert (
        client.post(
            "/projects/project_foreign/agent-proposals",
            headers={**owner, "Idempotency-Key": "foreign"},
            json=_plan(),
        ).status_code
        == 403
    )
    # Human cannot promote a proposal hidden by its current tenant identity.
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE projects SET org_id='org_foreign' WHERE id=?", (project,))
    assert client.get(f"/agent-proposals/{proposal['id']}", headers=owner).status_code == 404
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE projects SET org_id=? WHERE id=?", (credential["org_id"], project))
        db.execute(
            "UPDATE credentials SET revoked_at=? WHERE id=?",
            (datetime.now(UTC).isoformat(), credential["parent_id"]),
        )
    assert client.get(f"/agent-proposals/{proposal['id']}", headers=agent).status_code == 401
    assert (
        client.post(
            path, headers={**agent, "Idempotency-Key": "original"}, json=_plan()
        ).status_code
        == 401
    )


def test_v49_migration_creates_empty_authority_without_inference(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        strip_runtime_v55(db)
        for table in (
            "external_agent_contexts",
            "external_agent_promotions",
            "external_agent_proposals",
            "external_agent_scopes",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("UPDATE schema_meta SET schema_version=49")
    assert migrate_state(state) == 55
    assert migrate_state(state) == 55
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert_empty_runtime_v55(db)
        for table in (
            "external_agent_contexts",
            "external_agent_promotions",
            "external_agent_proposals",
            "external_agent_scopes",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 1


def test_worker_expiry_cannot_be_revived_by_human_renewal(setup):
    state, client, owner, project, _, _ = setup
    token = secrets.token_urlsafe(32)
    body = {
        "token": token,
        "kind": "worker",
        "audience": "worker",
        "project_id": project,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    credential = client.post(
        "/credentials", headers={**owner, "Idempotency-Key": "worker"}, json=body
    ).json()
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE credentials SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), credential["credential_id"]),
        )
    renewal = {"expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    assert (
        client.post(
            f"/credentials/{credential['credential_id']}/renew",
            headers={**owner, "Idempotency-Key": "revive"},
            json=renewal,
        ).status_code
        == 403
    )
