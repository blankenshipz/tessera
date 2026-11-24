"""Scoped agent reads of snapshots, outputs, attempt logs, budget and campaign receipts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers

from tessera_control import agent_campaign_reads
from tessera_control.api import create_app
from tessera_control.store import _token_hash

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
    assert client.get(path, headers={**outsider, **DEST}).status_code == 404
    other_agent = _other_tenant_agent(env.state, ALL)
    assert client.get(path, headers=other_agent).status_code == 404


def test_snapshot_detail_says_when_the_file_list_is_cut(world, monkeypatch):
    env, client, _ = world
    manifest = json.loads(
        env.query("SELECT manifest_json FROM source_snapshots WHERE id=?", env.snapshot_id)[0][0]
    )
    path = f"/projects/{env.project['id']}/agent-snapshots/{env.snapshot_id}"
    # Give the snapshot more files than the cap so the truncated branch really runs.
    first = manifest["entries"][0]
    manifest["entries"] = [
        {**first, "relative_path": f"extra/file_{i}.txt"} for i in range(3)
    ] + manifest["entries"]
    env.execute(
        "UPDATE source_snapshots SET manifest_json=? WHERE id=?",
        json.dumps(manifest),
        env.snapshot_id,
    )
    total = len(manifest["entries"])
    assert total > 1
    uncut = _get(client, path, "full").json()
    assert uncut["truncated"] is False and len(uncut["files"]) == total
    cap = 1
    assert total > cap
    monkeypatch.setattr(agent_campaign_reads, "_ROW_CAP", cap)
    body = _get(client, path, "full").json()
    assert body["truncated"] is True
    assert len(body["files"]) == cap
    assert body["file_count"] == total


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
            "INSERT OR IGNORE INTO projects (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            ("project_other_agent", "org_other_agent", "other", "2026-01-01T00:00:00Z"),
        )
        # The other org's own owner root, then its agent issued under that root (the same
        # structure the real issuing path writes).
        connection.execute(
            "INSERT OR IGNORE INTO principals "
            "(id, org_id, role, token_hash, revoked_at, created_at) "
            "VALUES ('other_owner', 'org_other_agent', 'human', ?, NULL, ?)",
            (_token_hash("synthetic-other-org-owner-token-" + "x" * 30), "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT OR IGNORE INTO credentials "
            "(id, principal_id, kind, audience, project_id, parent_id, "
            "expires_at, revoked_at, created_at, created_by_principal_id) "
            "VALUES ('credential_other_owner', 'other_owner', 'local_owner', 'control', NULL, "
            "NULL, NULL, NULL, ?, 'other_owner')",
            ("2026-01-01T00:00:00Z",),
        )
        connection.execute(
            "INSERT OR IGNORE INTO principals "
            "(id, org_id, role, token_hash, revoked_at, created_at) "
            "VALUES (?, ?, 'agent', ?, NULL, ?)",
            ("other_agent", "org_other_agent", _token_hash(token), "2026-01-01T00:00:00Z"),
        )
        expires = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        connection.execute(
            "INSERT OR IGNORE INTO credentials "
            "(id, principal_id, kind, audience, project_id, parent_id, "
            "expires_at, revoked_at, created_at, created_by_principal_id) "
            "VALUES ('credential_other_agent', 'other_agent', 'agent', 'control', "
            "'project_other_agent', 'credential_other_owner', ?, NULL, ?, 'other_owner')",
            (expires, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        connection.execute(
            "INSERT OR IGNORE INTO external_agent_scopes VALUES ('credential_other_agent', ?, ?)",
            (json.dumps(actions), "planner.local"),
        )
        connection.commit()
    return {"Authorization": f"Bearer {token}", **DEST}


def _headers(actor):
    return {"Authorization": f"Bearer {actor['token']}", **DEST}


def test_an_agent_of_another_org_gets_a_strict_404_identical_to_a_missing_id(world):
    env, client, campaign = world
    other = _headers(env.other_org_agent(ALL))
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


PROJECT_PATHS = (
    "agent-snapshots",
    "campaigns",
    "holdout-claims",
    "agent-spend",
    "agent-standing-status",
)


def test_an_other_org_principal_with_no_project_binding_is_refused_by_the_org_filter_alone(world):
    """The credential has project_id NULL, so no cross-project check can refuse it: the
    organization filter in ``Store._project`` is the only thing between it and the project.
    Removing that filter makes this test fail."""
    env, client, _campaign = world
    actor = env.other_org_human()
    own_org = env.query("SELECT org_id FROM projects WHERE id=?", env.project["id"])[0][0]
    assert actor["project_id"] is None and actor["org_id"] != own_org
    other = {"Authorization": f"Bearer {actor['token']}"}
    project = env.project["id"]
    for path in PROJECT_PATHS:
        real = client.get(f"/projects/{project}/{path}", headers=other)
        missing = client.get(f"/projects/project_missing/{path}", headers=other)
        assert real.status_code == 404, (path, real.status_code)
        assert (real.status_code, real.json()) == (missing.status_code, missing.json()), path


def test_the_other_org_helpers_use_the_real_issuing_path_and_are_idempotent(world):
    env, _client, _campaign = world
    first, second = env.other_org_agent(ALL, "one"), env.other_org_agent(ALL, "two")
    human = env.other_org_human()
    assert len({first["id"], second["id"], human["id"]}) == 3
    assert first["org_id"] == second["org_id"] == human["org_id"]
    assert env.other_org_agent(ALL, "one") is first and env.other_org_human() is human
    kinds = {
        r["kind"]: r["project_id"]
        for r in env.query(
            "SELECT kind, project_id FROM credentials WHERE principal_id IN (?, ?, ?)",
            first["id"],
            second["id"],
            human["id"],
        )
    }
    assert kinds == {"agent": "project_other_org", "human_session": None}


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


# ---- GET /campaigns/{id}/stage-status: placement, waiting, lease and heartbeat codes ----


def _ts(seconds_ago=0, seconds_ahead=0):
    # Reference time is taken per call, not at import, so freshness thresholds hold in long runs.
    moment = datetime.now(UTC) - timedelta(seconds=seconds_ago) + timedelta(seconds=seconds_ahead)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw(env, sql, *args):
    """Seed rows directly (foreign keys off) the way the runner and workers would write them."""
    with sqlite3.connect(env.state / "tessera.sqlite3") as connection:
        connection.execute(sql, args)


def _event(env, campaign, event_type, payload, at):
    org = env.query("SELECT org_id FROM projects WHERE id=?", env.project["id"])[0][0]
    actor = env.query("SELECT id FROM principals WHERE role='human' LIMIT 1")[0][0]
    _raw(
        env,
        "INSERT INTO events (org_id, project_id, event_type, resource_revision, "
        "actor_principal_id, created_at, payload_json) VALUES (?, ?, ?, 1, ?, ?, ?)",
        org,
        env.project["id"],
        event_type,
        actor,
        at,
        json.dumps({"campaign_id": campaign["id"], **payload}),
    )


SECRET = "host.example.invalid 10.0.0.9 secret-token"


def _seed_stage_status(env, campaign, *, heartbeat_ago=5, lease_ahead=120):
    cid = campaign["id"]
    worker = "worker_principal_secret_id"
    _raw(
        env,
        "INSERT INTO worker_registry (id, org_id, project_id, worker_principal_id, credential_id, "
        "approved_build_identifier, approved_build_digest, trust_class, region, lifecycle_state, "
        "revision, registered_at, heartbeat_at, last_seen_at) VALUES ('wreg_secret', 'o', ?, ?, "
        "'credential_secret_id', 'b', 'd', 't', 'r', 'ready', 1, ?, ?, ?)",
        env.project["id"],
        worker,
        _ts(500),
        _ts(heartbeat_ago),
        _ts(heartbeat_ago),
    )
    _raw(
        env,
        "INSERT INTO execution_grants (id, project_id, task_id, task_revision, plan_digest, "
        "worker_principal_id, snapshot_id, snapshot_digest, snapshot_release_id, "
        "snapshot_release_expires_at, pinned_image, currency, max_exposure_cents, "
        "acknowledges_unmeasured_infrastructure, status, revision, epoch, expires_at, created_at, "
        "updated_at, created_by_principal_id, settlement_cents) VALUES ('grant1', ?, 't', 1, 'p', "
        "?, 'sn', 'd', 'r', ?, 'img', 'USD', 7, 1, 'active', 1, 3, ?, ?, ?, 'creator', NULL)",
        env.project["id"],
        worker,
        _ts(0, 900),
        _ts(0, 900),
        _ts(100),
        _ts(100),
    )
    _raw(
        env,
        "INSERT INTO execution_leases (id, grant_id, worker_principal_id, epoch, issued_at, "
        "expires_at, hard_expires_at, renewed_at) VALUES ('lease1', 'grant1', ?, 3, ?, ?, ?, ?)",
        worker,
        _ts(100),
        _ts(0, lease_ahead),
        _ts(0, 600),
        _ts(10),
    )
    for ident, attempt, status, failure in (
        ("at1", 1, "failed_transient", "infrastructure"),
        ("at2", 2, "failed_transient", "infrastructure"),
        ("at3", 3, "reserved", None),
    ):
        _raw(
            env,
            "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
            "hold_cents, settled_cents, grant_id, exit_code, failure_class, failure_detail, "
            "revision, created_at, updated_at) "
            "VALUES (?, ?, 's1', ?, ?, 4, 1, ?, ?, ?, ?, 1, ?, ?)",
            ident,
            cid,
            attempt,
            status,
            "grant1" if attempt == 3 else None,
            None if attempt == 3 else 1,
            failure,
            SECRET if failure else None,
            _ts(60),
            _ts(30),
        )
    _raw(
        env,
        "INSERT INTO campaign_salad_attempts (attempt_id, phase, waiting_reason, revision, "
        "updated_at) VALUES ('at3', 'awaiting_lease', ?, 1, ?)",
        "waiting on " + SECRET,
        _ts(30),
    )
    _raw(
        env,
        "INSERT INTO campaign_salad_stages (campaign_id, stage_key, profile, revision, "
        "updated_at) VALUES (?, 's1', 'gpu_small', 1, ?)",
        cid,
        _ts(30),
    )
    _event(
        env,
        campaign,
        "campaign.stage_placed",
        {
            "stage_key": "s1",
            "attempt": 3,
            "candidates": ["local-docker", "salad"],
            "provider_id": "local-docker",
            "hold_cents": 4,
            "skipped": [
                {"provider_id": "salad", "reason": "no_free_slot", "detail": SECRET},
                {"provider_id": "other", "reason": "free text " + SECRET, "detail": SECRET},
            ],
        },
        _ts(40),
    )
    for n, (kind, match) in enumerate(
        (("slot", 1), ("slot", 2), ("slot", 3), ("exposure", 1)), start=1
    ):
        _event(
            env,
            campaign,
            f"campaign.stage_waiting_{kind}",
            {"stage_key": "s1", "attempt": match},
            _ts(50 - n),
        )


def _stage(client, campaign, name="full"):
    response = _get(client, f"/campaigns/{campaign['id']}/stage-status", name)
    assert response.status_code == 200, response.text
    body = response.json()
    return body, next(s for s in body["stages"] if s["stage_key"] == "s1")


def test_stage_status_needs_campaign_logs_and_the_human_owner_reads(world):
    env, client, campaign = world
    path = f"/campaigns/{campaign['id']}/stage-status"
    assert _get(client, path, "reader").status_code == 403
    assert _get(client, path, "plain").status_code == 403
    assert _get(client, path, "full").status_code == 200
    env.agent(["campaign:logs"], "logsonly")
    assert _get(client, path, "logsonly").status_code == 200
    human = {"Authorization": (env.state / "owner.header").read_text().strip().split(": ", 1)[1]}
    assert client.get(path, headers=human).status_code == 200


def _other_tenant_agent(state, actions):
    """An enrolled agent (scopes + destination) that belongs to ANOTHER organization."""
    token = "synthetic-other-org-agent-token-" + "x" * 30
    created = _ts()
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "INSERT OR IGNORE INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
            ("org_other_agent", "Other tenant", created),
        )
        connection.execute(
            "INSERT INTO projects (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            ("project_other_agent", "org_other_agent", "other", created),
        )
        connection.execute(
            "INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at) "
            "VALUES ('other_owner', 'org_other_agent', 'human', ?, NULL, ?)",
            (_token_hash("synthetic-other-org-owner-token-" + "x" * 30), created),
        )
        connection.execute(
            "INSERT INTO credentials (id, principal_id, kind, audience, project_id, parent_id, "
            "expires_at, revoked_at, created_at, created_by_principal_id) "
            "VALUES ('credential_other_owner', 'other_owner', 'local_owner', 'control', NULL, "
            "NULL, NULL, NULL, ?, 'other_owner')",
            (created,),
        )
        connection.execute(
            "INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at) "
            "VALUES ('other_agent', 'org_other_agent', 'agent', ?, NULL, ?)",
            (_token_hash(token), created),
        )
        connection.execute(
            "INSERT INTO credentials (id, principal_id, kind, audience, project_id, parent_id, "
            "expires_at, revoked_at, created_at, created_by_principal_id) "
            "VALUES ('credential_other_agent', 'other_agent', 'agent', 'control', "
            "'project_other_agent', 'credential_other_owner', ?, NULL, ?, 'other_owner')",
            (_ts(0, 7200), created),
        )
        connection.execute(
            "INSERT INTO external_agent_scopes VALUES ('credential_other_agent', ?, ?)",
            (json.dumps(actions), "planner.local"),
        )
    return {"Authorization": f"Bearer {token}", **DEST}


def test_stage_status_is_a_strict_404_for_an_agent_of_another_org(world):
    env, client, campaign = world
    other = _other_tenant_agent(env.state, ALL)
    real = client.get(f"/campaigns/{campaign['id']}/stage-status", headers=other)
    missing = client.get("/campaigns/campaign_missing/stage-status", headers=other)
    assert real.status_code == 404
    assert (real.status_code, real.json()) == (missing.status_code, missing.json())
    human = _other_tenant_headers(env.state)
    real = client.get(f"/campaigns/{campaign['id']}/stage-status", headers=human)
    missing = client.get("/campaigns/campaign_missing/stage-status", headers=human)
    assert real.status_code == 404
    assert (real.status_code, real.json()) == (missing.status_code, missing.json())


def test_stage_status_reports_placement_waiting_lease_and_fresh_heartbeat(world):
    env, client, campaign = world
    _seed_stage_status(env, campaign)
    body, stage = _stage(client, campaign)
    assert body["campaign_id"] == campaign["id"]
    assert body["paused"] is False
    assert body["heartbeat_max_age_seconds"] == 60
    assert stage["profile"] == "gpu_small"
    placement = stage["placement"]
    assert (placement["attempt"], placement["chosen_provider"]) == (3, "local-docker")
    assert placement["candidates"] == ["local-docker", "salad"]
    assert placement["skipped"] == [
        {"provider_id": "salad", "reason": "no_free_slot"},
        {"provider_id": "other", "reason": "detail_withheld"},
    ]
    waiting = {w["reason"]: w["count"] for w in stage["waiting"]}
    assert waiting == {"slot": 3, "exposure": 1}
    assert stage["failure_counts"] == {"infrastructure": 2}
    latest = stage["latest_attempt"]
    assert (latest["attempt"], latest["status"], latest["phase"]) == (
        3,
        "reserved",
        "awaiting_lease",
    )
    assert (latest["hold_cents"], latest["settled_cents"]) == (4, 1)
    assert latest["waiting_reason"] == "detail_withheld"
    assert 25 <= latest["age_seconds"] < 120
    assert latest["grant"]["status"] == "active"
    assert latest["grant"]["max_exposure_cents"] == 7
    assert latest["grant"]["settlement_cents"] is None
    assert latest["lease"]["held"] is True and latest["lease"]["expired"] is False
    assert latest["lease"]["epoch"] == 3
    assert 0 < latest["lease"]["expires_in_seconds"] <= 120
    assert latest["executor"]["registered"] is True
    assert latest["executor"]["lifecycle_state"] == "ready"
    assert latest["executor"]["heartbeat_stale"] is False


def test_stage_status_marks_an_expired_lease_and_stale_heartbeat(world):
    env, client, campaign = world
    _seed_stage_status(env, campaign, heartbeat_ago=600, lease_ahead=-30)
    _, stage = _stage(client, campaign)
    latest = stage["latest_attempt"]
    assert latest["lease"]["held"] is False and latest["lease"]["expired"] is True
    assert latest["executor"]["heartbeat_stale"] is True
    assert latest["executor"]["heartbeat_age_seconds"] >= 590


def test_stage_status_does_not_call_a_lease_held_once_its_grant_is_reconciled(world):
    env, client, campaign = world
    _seed_stage_status(env, campaign)
    _, stage = _stage(client, campaign)
    assert stage["latest_attempt"]["lease"]["held"] is True
    _raw(env, "UPDATE execution_grants SET status='reconciled' WHERE id='grant1'")
    _, stage = _stage(client, campaign)
    lease = stage["latest_attempt"]["lease"]
    assert lease["held"] is False and lease["expired"] is False


def test_stage_status_never_returns_free_text_or_identifiers(world):
    env, client, campaign = world
    _seed_stage_status(env, campaign)
    body, stage = _stage(client, campaign)
    text = json.dumps(body)
    for forbidden in (
        "host.example.invalid",
        "10.0.0.9",
        "secret-token",
        "failure_detail",
        "worker_principal_secret_id",
        "credential_secret_id",
        "wreg_secret",
        "worker_principal_id",
        "credential_id",
        "grant1",
        "lease1",
    ):
        assert forbidden not in text, forbidden
    assert stage["latest_attempt"]["waiting_reason"] == "detail_withheld"


def test_stage_status_with_no_rows_is_empty_not_an_error(world):
    _, client, campaign = world
    body, stage = _stage(client, campaign)
    assert stage["placement"] is None and stage["waiting"] == []
    assert stage["latest_attempt"] is None and stage["failure_counts"] == {}


def test_stage_status_shows_the_latest_rejection_and_code_counts_without_free_text(world):
    from tessera_control.stage_rejections import record
    from tessera_control.store import Conflict, Forbidden

    env, client, campaign = world
    _seed_stage_status(env, campaign)
    _, stage = _stage(client, campaign)
    assert stage["rejections"] == {"latest": None, "counts": {}}
    worker = {"id": "worker_principal_secret_id", "org_id": env.worker["org_id"]}
    _raw(
        env,
        "UPDATE campaigns SET org_id=? WHERE id=?",
        worker["org_id"],
        campaign["id"],
    )
    assert record(
        env.store, worker, "at3", "lease", Conflict("Worker heartbeat is stale " + SECRET)
    )
    assert record(env.store, worker, "at3", "package", Forbidden(SECRET))
    assert not record(env.store, worker, "at3", "package", Forbidden(SECRET))
    body, stage = _stage(client, campaign)
    rejections = stage["rejections"]
    assert rejections["counts"] == {"stale_heartbeat": 1, "refused_forbidden": 1}
    latest = rejections["latest"]
    assert (latest["attempt"], latest["step"], latest["code"]) == (
        3,
        "package",
        "refused_forbidden",
    )
    assert 0 <= latest["age_seconds"] < 60
    text = json.dumps(body)
    for forbidden in ("host.example.invalid", "10.0.0.9", "secret-token"):
        assert forbidden not in text
