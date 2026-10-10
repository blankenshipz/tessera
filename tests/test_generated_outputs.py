"""Synthetic public output; real authenticated HTTP/SQLite, no model inference."""

import base64
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import assert_empty_runtime_v55, enroll, post, strip_runtime_v55

from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import initialize_state, migrate_state


@pytest.fixture
def output_setup(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    owner = {"Authorization": (state / "owner.header").read_text().split(": ", 1)[1].strip()}
    project = post(client, owner, "/projects", {"name": "generated"})["id"]
    sender, credential = enroll(client, owner, project, ["context:read", "provenance:append"])
    receiver, recipient = enroll(client, owner, project, ["context:read"])
    context = post(
        client,
        owner,
        f"/projects/{project}/agent-contexts",
        {
            "credential_id": credential["credential_id"],
            "destination": "openai:personal",
            "title": "curated public",
            "body": "public source context",
            "classification": "public",
            "disclosure_approved": True,
        },
    )
    pins = {
        "model_id": "included-model",
        "run_id": "run-one",
        "session_id": "session-one",
        "harness_instance_id": "harness-one",
        "context_id": context["id"],
        "context_digest": "a" * 64,
        "skill_digest": "b" * 64,
        "config_digest": "c" * 64,
        "task_id": None,
        "operation_id": None,
    }
    bounds = {
        "action": "generated-output:register",
        "output_kind": "critic_review",
        "credential_id": credential["credential_id"],
        "destination": "openai:personal",
        "pins": pins,
        "public_output_admitted": True,
        "max_outputs": 1,
        "max_total_bytes": 1024,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    grant = post(client, owner, f"/projects/{project}/generated-output-grants", bounds)
    content = b"Public critic recommendation; human review required."
    body = {
        "grant_id": grant["id"],
        "expected_revision": 1,
        "pins": pins,
        "source_event_id": "message-one",
        "source_sequence": 1,
        "output_kind": "critic_review",
        "visibility": "public",
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }
    return state, client, owner, project, sender, receiver, recipient, grant, body


def test_register_read_disclose_replay_revoke_without_execution_identity(output_setup):
    state, client, owner, project, sender, receiver, recipient, grant, body = output_setup
    path = f"/projects/{project}/generated-outputs"
    output = post(client, sender, path, body, key="stable-public")
    assert post(client, sender, path, body, key="stable-public") == output
    assert output["scientific_validity"] == "not_assessed" and not output["execution_established"]
    assert "content_base64" not in output
    assert client.get(f"/generated-outputs/{output['id']}", headers=receiver).status_code == 403
    disclosure = post(
        client,
        owner,
        f"/generated-outputs/{output['id']}/disclosures",
        {
            "credential_id": recipient["credential_id"],
            "destination": "openai:personal",
            "disclosure_approved": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    route = f"/generated-outputs/{output['id']}?disclosure_id={disclosure['id']}"
    result = client.get(route, headers=receiver)
    assert result.status_code == 200 and result.json()["content_base64"] == body["content_base64"]
    metadata = client.get(
        f"/generated-outputs/{output['id']}/metadata?disclosure_id={disclosure['id']}",
        headers=receiver,
    )
    assert metadata.status_code == 200 and "content_base64" not in metadata.json()
    assert client.get(route, headers=sender).status_code == 403
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 0
        event = db.execute(
            "SELECT payload_json FROM events WHERE event_type='generated_output.registered'"
        ).fetchone()[0]
        assert body["content_base64"] not in event and "content_base64" not in event
        assert db.execute(
            "SELECT used_outputs,used_bytes FROM generated_output_grants"
        ).fetchone() == (1, result.json()["size"])
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM generated_public_outputs")
    post(
        client,
        owner,
        f"/generated-output-disclosures/{disclosure['id']}/revoke",
        {"expected_revision": 1},
        expected=200,
    )
    assert client.get(route, headers=receiver).status_code == 403
    post(
        client,
        owner,
        f"/generated-output-grants/{grant['id']}/revoke",
        {"expected_revision": 1},
        expected=200,
    )
    post(client, sender, path, body, key="stable-public", expected=409)
    assert client.get(f"/generated-outputs/{output['id']}", headers=owner).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        "model",
        "context",
        "revision",
        "hash",
        "hidden",
        "destination",
        "identity",
        "human",
        "expired",
        "tenant",
    ],
)
def test_registration_fails_closed_atomically(output_setup, change):
    state, client, owner, project, sender, receiver, recipient, grant, body = output_setup
    body = deepcopy(body)
    auth = sender
    expected = 403
    if change == "model":
        body["pins"]["model_id"] = "wrong"
    if change == "context":
        body["pins"]["context_digest"] = "f" * 64
    if change == "revision":
        body["expected_revision"] = 2
    if change == "hash":
        body["sha256"] = "0" * 64
        expected = 400
    if change == "hidden":
        body["visibility"] = "hidden_reasoning"
        expected = 422
    if change == "destination":
        auth = sender | {"X-Agent-Destination": "other"}
    if change == "identity":
        auth = receiver
    if change == "human":
        auth = owner
    if change == "expired":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            bounds = json.loads(
                db.execute("SELECT bounds_json FROM generated_output_grants").fetchone()[0]
            )
            bounds["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            db.execute("UPDATE generated_output_grants SET bounds_json=?", (json.dumps(bounds),))
        expected = 409
    if change == "tenant":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute("INSERT INTO organizations VALUES ('other','other','now')")
            db.execute("UPDATE projects SET org_id='other'")
        expected = 401
    post(client, auth, f"/projects/{project}/generated-outputs", body, expected=expected)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM generated_public_outputs").fetchone()[0] == 0
        assert db.execute(
            "SELECT used_outputs,used_bytes FROM generated_output_grants"
        ).fetchone() == (0, 0)


def test_concurrent_output_budget_and_restart_replay(output_setup):
    state, client, owner, project, sender, receiver, recipient, grant, body = output_setup
    path = f"/projects/{project}/generated-outputs"

    def send(key):
        return client.post(path, json=body, headers=sender | {"Idempotency-Key": key})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, ["first", "second"]))
    assert sorted(r.status_code for r in results) == [201, 409]
    winner = next(i for i, r in enumerate(results) if r.status_code == 201)
    fresh = TestClient(create_app(state))
    replay = fresh.post(
        path, json=body, headers=sender | {"Idempotency-Key": ["first", "second"][winner]}
    )
    assert replay.status_code == 201 and replay.json() == results[winner].json()


@pytest.mark.parametrize("held", [False, True])
def test_old52_requires_explicit53_migration(output_setup, held):
    state, client, owner, project, sender, receiver, recipient, grant, body = output_setup
    if held:
        backup = state.parent / "backup"
        backup_state(state, backup)
        restored = state.parent / "held"
        restore_state(backup, restored)
        state = restored
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        strip_runtime_v55(db)
        for table in (
            "generated_output_disclosures",
            "generated_public_outputs",
            "generated_output_grants",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("UPDATE schema_meta SET schema_version=52")
        tables = ("control_recovery", "credentials", "principals", "events", "idempotency")
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        health = db.execute("SELECT * FROM operator_backup_health").fetchall()
    assert TestClient(create_app(state)).get("/health/ready").status_code == 503
    assert migrate_state(state) == 56
    if not held:
        assert TestClient(create_app(state)).get("/health/ready").status_code == 200
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM generated_public_outputs").fetchone()[0] == 0
        assert_empty_runtime_v55(db)
        assert all(
            db.execute(f"SELECT * FROM {table}").fetchall() == before[table] for table in tables
        )
        if held:
            assert db.execute("SELECT * FROM operator_backup_health").fetchall() == health


@pytest.mark.parametrize("fence", ["budget", "credential", "issuer", "source_identity"])
def test_output_additional_lifetime_and_source_fences(output_setup, fence):
    state, client, owner, project, sender, receiver, recipient, grant, body = output_setup
    path = f"/projects/{project}/generated-outputs"
    if fence == "source_identity":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            bounds = json.loads(
                db.execute("SELECT bounds_json FROM generated_output_grants").fetchone()[0]
            )
            bounds["max_outputs"] = 2
            db.execute("UPDATE generated_output_grants SET bounds_json=?", (json.dumps(bounds),))
        post(client, sender, path, body)
        body = deepcopy(body)
        content = b"Different public output under reused source identity"
        body.update(
            sha256=hashlib.sha256(content).hexdigest(),
            content_base64=base64.b64encode(content).decode(),
        )
        post(client, sender, path, body, expected=409)
    else:
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            if fence == "budget":
                bounds = json.loads(
                    db.execute("SELECT bounds_json FROM generated_output_grants").fetchone()[0]
                )
                bounds["max_total_bytes"] = 1
                db.execute(
                    "UPDATE generated_output_grants SET bounds_json=?", (json.dumps(bounds),)
                )
            elif fence == "issuer":
                db.execute("UPDATE principals SET revoked_at='now' WHERE role='human'")
            else:
                db.execute("UPDATE credentials SET revoked_at='now' WHERE kind='agent'")
        post(client, sender, path, body, expected=409 if fence == "budget" else 401)
