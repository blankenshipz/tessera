"""Scoped agent reads of snapshots, outputs, attempt logs, budget and campaign receipts."""

from __future__ import annotations

import json

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers

from tessera_control import agent_campaign_reads
from tessera_control.api import create_app

DEST = {"X-Agent-Destination": "planner.local"}
ALL = ["campaign:read", "snapshot:read", "artifact:read", "campaign:logs", "budget:read"]


def _token(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    org = env.query("SELECT org_id FROM projects WHERE id=?", env.project["id"])[0][0]
    env.execute(
        "INSERT INTO campaign_stage_io (id, org_id, project_id, campaign_id, stage_key, "
        "direction, name, kind, media_type, sha256, size_bytes, data_class, created_at) "
        "VALUES ('io1', ?, ?, ?, 's1', 'output', 'data.json', 'file', 'application/json', ?, "
        "7, 'private_derived', '2026-01-01T00:00:00Z')",
        org,
        env.project["id"],
        campaign["id"],
        "d" * 64,
    )
    env.execute(
        "INSERT INTO campaign_stage_io (id, org_id, project_id, campaign_id, stage_key, "
        "direction, name, kind, sha256, size_bytes, data_class, created_at) "
        "VALUES ('io2', ?, ?, ?, 's1', 'input', 'code', 'snapshot', ?, 1, 'public', "
        "'2026-01-01T00:00:00Z')",
        org,
        env.project["id"],
        campaign["id"],
        "e" * 64,
    )
    env.agent(ALL, "full")
    env.agent(["context:read"], "plain")
    env.agent(["campaign:read"], "reader")
    client = TestClient(create_app(env.state))
    return env, client, campaign


def _get(client, path, name, destination=DEST):
    return client.get(path, headers={**_token(name), **destination})


def test_snapshot_catalog_is_scoped_and_metadata_only(world):
    env, client, _ = world
    path = f"/projects/{env.project['id']}/agent-snapshots"
    body = _get(client, path, "full").json()
    ids = [s["id"] for s in body["snapshots"]]
    assert env.snapshot_id in ids
    row = next(s for s in body["snapshots"] if s["id"] == env.snapshot_id)
    assert row["digest"] == env.snapshot_digest
    assert row["data_class"]
    assert not any(isinstance(v, str) and v.startswith("/") for v in row.values())
    assert set(row) == {
        "id",
        "digest",
        "created_at",
        "total_bytes",
        "file_count",
        "data_class",
    }
    assert _get(client, path, "reader").status_code == 403
    assert _get(client, path, "plain").status_code == 403
    assert _get(client, path + "?limit=0", "full").status_code == 422


def test_snapshot_detail_lists_files_and_is_metadata_only(world):
    env, client, _ = world
    path = f"/projects/{env.project['id']}/agent-snapshots/{env.snapshot_id}"
    response = _get(client, path, "full")
    assert response.status_code == 200
    body = response.json()
    manifest = json.loads(
        env.query("SELECT manifest_json FROM source_snapshots WHERE id=?", env.snapshot_id)[0][0]
    )
    assert body["id"] == env.snapshot_id and body["digest"] == env.snapshot_digest
    assert body["file_count"] == len(manifest["entries"]) > 0
    assert body["total_bytes"] == manifest["total_bytes"]
    assert body["truncated"] is False and body["data_class"]
    assert isinstance(body["license_labels"], list)
    assert [(f["relative_path"], f["size"], f["sha256"]) for f in body["files"]] == [
        (e["relative_path"], e["size"], e["sha256"]) for e in manifest["entries"]
    ]
    assert all(
        set(f) == {"relative_path", "size", "sha256", "classification"} for f in body["files"]
    )
    assert "root" not in body and "provenance" not in body and "manifest" not in body
    assert "bytes are never returned" in body["boundary"]
    serialized = json.dumps(body)
    assert manifest["root"] not in serialized
    assert not any(f["relative_path"].startswith("/") for f in body["files"])
    assert not any(isinstance(v, str) and v.startswith("/") for v in body.values())


def test_snapshot_detail_is_scoped_and_tenant_checked(world):
    env, client, _ = world
    path = f"/projects/{env.project['id']}/agent-snapshots/{env.snapshot_id}"
    assert _get(client, path, "reader").status_code == 403
    assert _get(client, path, "plain").status_code == 403
    other_project = f"/projects/proj_other/agent-snapshots/{env.snapshot_id}"
    assert _get(client, other_project, "full").status_code == 404
    missing = f"/projects/{env.project['id']}/agent-snapshots/snap_missing"
    assert _get(client, missing, "full").status_code == 404
    outsider = _other_tenant_headers(env.state)
    assert client.get(path, headers={**outsider, **DEST}).status_code in (403, 404)


def test_snapshot_detail_says_when_the_file_list_is_cut(world, monkeypatch):
    env, client, _ = world
    manifest = json.loads(
        env.query("SELECT manifest_json FROM source_snapshots WHERE id=?", env.snapshot_id)[0][0]
    )
    monkeypatch.setattr(agent_campaign_reads, "_ROW_CAP", 1)
    path = f"/projects/{env.project['id']}/agent-snapshots/{env.snapshot_id}"
    body = _get(client, path, "full").json()
    assert len(body["files"]) == 1
    assert body["file_count"] == len(manifest["entries"])
    assert body["truncated"] is (len(manifest["entries"]) > 1)


def test_destination_header_is_required_for_external_agents(world):
    env, client, campaign = world
    for path in (f"/campaigns/{campaign['id']}", f"/campaigns/{campaign['id']}/logs"):
        assert _get(client, path, "full", destination={}).status_code == 403
        assert _get(client, path, "full", {"X-Agent-Destination": "elsewhere"}).status_code == 403


def test_outputs_logs_budget_need_their_own_scope(world):
    env, client, campaign = world
    base = f"/campaigns/{campaign['id']}"
    outputs = _get(client, base + "/outputs", "full").json()["outputs"]
    assert [o["name"] for o in outputs] == ["data.json"]  # inputs are never listed
    assert outputs[0]["sha256"] == "d" * 64 and "path" not in outputs[0]
    logs = _get(client, base + "/logs", "full").json()
    assert [s["stage_key"] for s in logs["stages"]] == ["s1", "s2"]
    assert logs["attempts"] == [] and "stdout" in logs["boundary"]
    budget = _get(client, base + "/budget", "full").json()
    assert budget["budget_cap_cents"] == 10 and budget["settled_cents"] == 0
    for suffix in ("outputs", "logs", "budget"):
        assert _get(client, f"{base}/{suffix}", "reader").status_code == 403
        assert _get(client, f"{base}/{suffix}", "plain").status_code == 403


def test_single_scope_does_not_unlock_the_others(world):
    env, client, campaign = world
    env.agent(["artifact:read"], "artifacts")
    base = f"/campaigns/{campaign['id']}"
    assert _get(client, base + "/outputs", "artifacts").status_code == 200
    assert _get(client, base + "/logs", "artifacts").status_code == 403
    assert _get(client, base + "/budget", "artifacts").status_code == 403
    assert _get(client, base, "artifacts").status_code == 403


def _other_tenant_agent(state, actions):
    """An enrolled agent (scopes + destination) that belongs to ANOTHER organization."""
    import json
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from tessera_control.store import _token_hash

    token = "synthetic-other-org-agent-token-" + "x" * 30
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "INSERT OR IGNORE INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
            ("org_other_agent", "Other tenant", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO projects (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            ("project_other_agent", "org_other_agent", "other", "2026-01-01T00:00:00Z"),
        )
        # The other org's own owner root, then its agent issued under that root (the same
        # structure the real issuing path writes).
        connection.execute(
            "INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at) "
            "VALUES ('other_owner', 'org_other_agent', 'human', ?, NULL, ?)",
            (_token_hash("synthetic-other-org-owner-token-" + "x" * 30), "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO credentials (id, principal_id, kind, audience, project_id, parent_id, "
            "expires_at, revoked_at, created_at, created_by_principal_id) "
            "VALUES ('credential_other_owner', 'other_owner', 'local_owner', 'control', NULL, "
            "NULL, NULL, NULL, ?, 'other_owner')",
            ("2026-01-01T00:00:00Z",),
        )
        connection.execute(
            "INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at) "
            "VALUES (?, ?, 'agent', ?, NULL, ?)",
            ("other_agent", "org_other_agent", _token_hash(token), "2026-01-01T00:00:00Z"),
        )
        expires = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        connection.execute(
            "INSERT INTO credentials (id, principal_id, kind, audience, project_id, parent_id, "
            "expires_at, revoked_at, created_at, created_by_principal_id) "
            "VALUES ('credential_other_agent', 'other_agent', 'agent', 'control', "
            "'project_other_agent', 'credential_other_owner', ?, NULL, ?, 'other_owner')",
            (expires, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        connection.execute(
            "INSERT INTO external_agent_scopes VALUES ('credential_other_agent', ?, ?)",
            (json.dumps(actions), "planner.local"),
        )
        connection.commit()
    return {"Authorization": f"Bearer {token}", **DEST}


def test_an_agent_of_another_org_gets_a_strict_404_identical_to_a_missing_id(world):
    env, client, campaign = world
    other = _other_tenant_agent(env.state, ALL)
    # Sanity: the same credential shape works for the owning org's agent.
    assert _get(client, f"/campaigns/{campaign['id']}/outputs", "full").status_code == 200
    for suffix in (
        "",
        "/outputs",
        "/logs",
        "/budget",
        "/grants",
        "/retries",
        "/holdout",
        "/receipt",
    ):
        real = client.get(f"/campaigns/{campaign['id']}{suffix}", headers=other)
        missing = client.get(f"/campaigns/campaign_missing{suffix}", headers=other)
        assert real.status_code == 404, (suffix, real.status_code)
        assert (real.status_code, real.json()) == (missing.status_code, missing.json()), suffix
    project = env.project["id"]
    for path in (
        "agent-snapshots",
        "campaigns",
        "holdout-claims",
        "agent-spend",
        "agent-standing-status",
    ):
        real = client.get(f"/projects/{project}/{path}", headers=other)
        missing = client.get(f"/projects/project_missing/{path}", headers=other)
        assert real.status_code == 404, (path, real.status_code)
        assert (real.status_code, real.json()) == (missing.status_code, missing.json()), path


def test_no_cross_project_or_cross_tenant_reads(world):
    env, client, campaign = world
    assert _get(client, "/projects/other-project/agent-snapshots", "full").status_code == 404
    assert _get(client, "/campaigns/missing/outputs", "full").status_code == 404
    other = _other_tenant_headers(env.state)
    for suffix in ("outputs", "logs", "budget", "receipt"):
        assert client.get(f"/campaigns/{campaign['id']}/{suffix}", headers=other).status_code == 404


def test_receipt_and_list_are_readable_with_campaign_read(world):
    env, client, campaign = world
    listing = _get(client, f"/projects/{env.project['id']}/campaigns", "reader").json()
    assert [c["id"] for c in listing["campaigns"]] == [campaign["id"]]
    assert "spec_json" not in listing["campaigns"][0]
    # Not terminal yet: the receipt does not exist and the route says so.
    assert _get(client, f"/campaigns/{campaign['id']}/receipt", "reader").status_code == 404
    assert _get(client, f"/campaigns/{campaign['id']}/receipt", "plain").status_code == 403


def test_human_reads_without_enrolled_scopes(world):
    env, client, campaign = world
    human = {"Authorization": (env.state / "owner.header").read_text().strip().split(": ", 1)[1]}
    for suffix in ("outputs", "logs", "budget"):
        assert client.get(f"/campaigns/{campaign['id']}/{suffix}", headers=human).status_code == 200
    assert (
        client.get(f"/projects/{env.project['id']}/agent-snapshots", headers=human).status_code
        == 200
    )


def test_budget_reports_the_campaign_ledger_and_logs_show_attempts(world):
    env, client, campaign = world
    for ident, key, attempt, status, hold, settled in (
        ("a1", "s1", 1, "succeeded", 3, 2),
        ("a2", "s2", 1, "reserved", 4, None),
    ):
        env.execute(
            "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
            "hold_cents, settled_cents, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            ident,
            campaign["id"],
            key,
            attempt,
            status,
            hold,
            settled,
        )
    env.execute(
        "UPDATE campaigns SET held_cents = 4, settled_cents = 2 WHERE id = ?", campaign["id"]
    )
    base = f"/campaigns/{campaign['id']}"
    budget = _get(client, base + "/budget", "full").json()
    assert budget["budget_cap_cents"] == 10
    assert (budget["held_cents"], budget["settled_cents"], budget["remaining_cents"]) == (4, 2, 4)
    logs = _get(client, base + "/logs", "full").json()
    assert [(a["stage_key"], a["attempt"], a["status"]) for a in logs["attempts"]] == [
        ("s1", 1, "succeeded"),
        ("s2", 1, "reserved"),
    ]
    assert logs["truncated"] is False
    assert _get(client, base + "/outputs", "full").json()["truncated"] is False


def test_enrolled_agent_submit_needs_the_destination_header(world):
    env, client, _ = world
    env.agent(["campaign:submit", "campaign:read"], "submitter")
    path = f"/projects/{env.project['id']}/campaigns"
    body = {"spec": env.spec()}
    headers = {**_token("submitter"), "Idempotency-Key": "agent-submit-1"}
    refused = client.post(path, json=body, headers=headers)
    assert refused.status_code == 403
    accepted = client.post(path, json=body, headers={**headers, **DEST})
    assert accepted.status_code == 201, accepted.text


def test_standing_status_is_agent_readable_without_the_template_or_principals(world):
    env, client, _ = world
    path = f"/projects/{env.project['id']}/agent-standing-status"
    body = _get(client, path, "reader").json()
    assert body["standing_approvals"] == []
    assert "cannot create" in body["boundary"]
    assert _get(client, path, "plain").status_code == 403
    # The full human routes stay human-only for the same credential.
    full = _get(client, f"/projects/{env.project['id']}/standing-approvals", "reader")
    assert full.status_code == 403


def test_standing_status_subqueries_match_the_schema(world):
    env, _, _ = world
    env.query(
        "SELECT start_index, campaign_id, kind, trigger_campaign_id, retry_number, "
        "budget_cap_cents, created_at FROM campaign_standing_starts WHERE standing_approval_id=?",
        "none",
    )
    env.query(
        "SELECT trigger_campaign_id, kind, retry_number, status, start_index, reason, "
        "created_at, resolved_at FROM campaign_standing_triggers "
        "WHERE target_standing_approval_id=?",
        "none",
    )
