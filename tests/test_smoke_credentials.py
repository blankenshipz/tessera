"""Owner-run smoke credentials: a $0 local-docker agent credential and its approver.

Everything here is enforced by the control plane (store entry points and the HTTP dependency);
the smoke script is not involved. Synthetic data only: no provider, no network, no money."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from test_campaign_models_v2 import _spec2, _stage2

from tessera_control import campaign_provider_worker as worker
from tessera_control import cli, smoke_credentials
from tessera_control.api import create_app
from tessera_control.campaign_models import normalize, parse_spec
from tessera_control.campaign_providers import register_provider
from tessera_control.campaign_receipts import build_receipt
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _connect,
    canonical_digest,
    initialize_state,
)

DEST = "smoke.local"
ACTIONS = ["budget:read", "campaign:cancel", "campaign:read", "campaign:submit", "snapshot:read"]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def zero(env, **overrides):
    spec = env.spec(budget_cap_cents=0, **overrides)
    for stage in spec["stages"]:
        stage["max_exposure_cents"] = 0
    return spec


def rows(env, sql, *args):
    return [dict(row) for row in env.query(sql, *args)]


class World:
    def __init__(self, env):
        self.env = env
        self.grant = smoke_credentials.issue(env.state, env.project["id"], DEST)
        self.agent = {
            **env.plain.authenticate(self.grant["agent_token"]),
            "agent_destination": DEST,
        }
        self.approver = env.plain.authenticate(self.grant["approver_token"])

    def submit(self, spec=None):
        return self.env.submit(self.agent, spec or zero(self.env))

    def refused_submit(self, spec, match=smoke_credentials.REFUSAL):
        body = {"spec": spec}
        with pytest.raises(Forbidden, match=match):
            self.env.store.submit_campaign(
                self.agent, self.env.project["id"], "k-refused", canonical_digest(body), body
            )
        assert (
            rows(
                self.env,
                "SELECT id FROM campaigns WHERE submitted_credential_id=?",
                self.agent["credential_id"],
            )
            == []
        )

    def approve_body(self, campaign):
        body = {
            "expected_revision": campaign["revision"],
            "expected_spec_digest": campaign["spec_digest"],
        }
        return body


@pytest.fixture
def world(env):
    return World(env)


# -- issue ------------------------------------------------------------------------------------


def test_issue_creates_exactly_two_credentials_and_one_grant(env, world):
    grant = world.grant
    credentials = rows(
        env, "SELECT * FROM credentials WHERE created_by_principal_id=?", env.owner["id"]
    )
    smoke = [
        c
        for c in credentials
        if c["id"] in {grant["agent_credential_id"], grant["approver_credential_id"]}
    ]
    assert {c["kind"] for c in smoke} == {"agent", "human_session"}
    assert all(
        c["project_id"] == env.project["id"]
        and c["expires_at"] == grant["expires_at"]
        and c["audience"] == "control"
        and c["revoked_at"] is None
        for c in smoke
    )
    # the worker and nothing else was created besides these two (plus the fixture's own)
    scope = rows(
        env,
        "SELECT * FROM external_agent_scopes WHERE credential_id=?",
        grant["agent_credential_id"],
    )[0]
    assert sorted(json.loads(scope["actions_json"])) == ACTIONS and scope["destination"] == DEST
    assert (
        rows(
            env,
            "SELECT * FROM external_agent_scopes WHERE credential_id=?",
            grant["approver_credential_id"],
        )
        == []
    )
    (row,) = rows(env, "SELECT * FROM smoke_credential_grants")
    assert row["id"] == grant["grant_id"] and row["max_budget_cents"] == 0
    assert row["providers_json"] == '["local-docker"]' and row["revoked_at"] is None
    assert row["agent_credential_id"] == grant["agent_credential_id"]
    assert row["approver_credential_id"] == grant["approver_credential_id"]
    (event,) = rows(
        env, "SELECT * FROM operator_audit_events WHERE event_type=?", "smoke_credentials.issued"
    )
    assert json.loads(event["payload_json"])["grant_id"] == grant["grant_id"]
    # tokens are returned only: hashes in the database, never the token itself
    dump = (env.state / "tessera.sqlite3").read_bytes().decode("latin-1")
    assert grant["agent_token"] not in dump and grant["approver_token"] not in dump
    assert grant["agent_token"] not in json.dumps(rows(env, "SELECT * FROM principals"))


def test_expiry_is_short_and_the_credentials_die_with_it(env):
    now = datetime.now(UTC)
    short = smoke_credentials.issue(env.state, env.project["id"], DEST, ttl_minutes=1)
    assert (
        now + timedelta(seconds=30)
        < datetime.fromisoformat(short["expires_at"])
        <= (now + timedelta(minutes=1, seconds=5))
    )
    old = smoke_credentials.issue(
        env.state, env.project["id"], DEST, now=now - timedelta(hours=2), ttl_minutes=10
    )
    for token in (old["agent_token"], old["approver_token"]):
        with pytest.raises(Forbidden):
            env.plain.authenticate(token)


@pytest.mark.parametrize("ttl", [0, 61, -5, True, 1.5])
def test_ttl_out_of_range_is_refused_and_writes_nothing(env, ttl):
    with pytest.raises(Conflict):
        smoke_credentials.issue(env.state, env.project["id"], DEST, ttl_minutes=ttl)
    assert rows(env, "SELECT id FROM smoke_credential_grants") == []


def test_unknown_project_and_blank_destination_are_refused(env):
    with pytest.raises(NotFound):
        smoke_credentials.issue(env.state, "project_missing", DEST)
    for destination in ("", "   "):
        with pytest.raises(Conflict):
            smoke_credentials.issue(env.state, env.project["id"], destination)
    assert rows(env, "SELECT id FROM smoke_credential_grants") == []


def test_only_the_owner_header_can_issue(env, tmp_path):
    header = env.state / "owner.header"
    header.chmod(0o644)
    with pytest.raises(Forbidden):
        smoke_credentials.issue(env.state, env.project["id"], DEST)
    header.chmod(0o600)


def test_the_grant_row_can_only_be_revoked(env, world):
    path = env.state / "tessera.sqlite3"
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM smoke_credential_grants")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE smoke_credential_grants SET expires_at='2999-01-01T00:00:00Z'")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE smoke_credential_grants SET max_budget_cents=1")


# -- agent credential enforcement -------------------------------------------------------------


def test_the_agent_submits_a_local_docker_zero_budget_spec(world):
    campaign = world.submit()
    assert campaign["status"] == "submitted" and campaign["budget_cap_cents"] == 0
    body = {"spec": zero(world.env)}
    checked = world.env.store.validate_campaign_spec(world.agent, world.env.project["id"], body)
    assert checked["valid"] is True


def test_a_nonzero_budget_cap_is_refused(world):
    world.refused_submit(
        world.env.spec(budget_cap_cents=1, **{}) | {"stages": zero(world.env)["stages"]}
    )


def test_a_nonzero_stage_exposure_is_refused(world):
    # exposure above a zero cap is invalid anyway; with a cap it is the grant that refuses
    spec = zero(world.env)
    spec["budget_cap_cents"] = 5
    spec["stages"][1]["max_exposure_cents"] = 1
    world.refused_submit(spec)


def test_a_non_local_provider_is_refused(world):
    register_provider(worker.WorkerGrantProvider(worker._descriptor("other-cpu", "p1", None, 1)))
    spec = _spec2(_stage2("a", provider="other-cpu", max_exposure_cents=0), budget_cap_cents=0)
    world.refused_submit(spec)
    # and the same spec on the local provider is fine up to the spec's own references
    assert smoke_credentials.spec_is_smoke_safe(
        normalize(parse_spec(_spec2(_stage2("a", max_exposure_cents=0), budget_cap_cents=0)))
    )
    assert not smoke_credentials.spec_is_smoke_safe(normalize(parse_spec(spec)))


def test_validate_applies_the_same_limits(world):
    spec = world.env.spec(budget_cap_cents=10)
    with pytest.raises(Forbidden, match=smoke_credentials.REFUSAL):
        world.env.store.validate_campaign_spec(world.agent, world.env.project["id"], {"spec": spec})


def test_other_credentials_are_not_limited(env, world):
    campaign = env.submit(env.owner, env.spec())  # a normal spec by the owner is untouched
    assert campaign["budget_cap_cents"] == 10


# -- approver ---------------------------------------------------------------------------------


def test_the_approver_approves_the_paired_agents_campaign_and_it_is_recorded(env, world):
    campaign = world.submit()
    result = env.approve(campaign, world.approver)
    assert result["approval"]["decision_class"] == "human_session"
    (event,) = rows(env, "SELECT * FROM events WHERE event_type='campaign.approved'")
    assert json.loads(event["payload_json"])["smoke_grant_id"] == world.grant["grant_id"]
    from tessera_control.store import _connect

    connection = _connect(env.state / "tessera.sqlite3")
    try:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()
        receipt, _digest = build_receipt(connection, row, "cancelled")
    finally:
        connection.close()
    assert receipt["approval"]["smoke_grant_id"] == world.grant["grant_id"]


def test_an_ordinary_approval_has_no_smoke_grant(env):
    campaign = env.approve(env.submit())
    (event,) = rows(env, "SELECT * FROM events WHERE event_type='campaign.approved'")
    assert "smoke_grant_id" not in json.loads(event["payload_json"])
    assert campaign["approval"]["decision_class"] == "human_session"


def _approve(env, actor, campaign):
    body = {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    }
    return env.store.approve_campaign(
        actor, campaign["id"], "k-approve", canonical_digest(body), body
    )


def test_the_approver_refuses_campaigns_it_is_not_paired_with(env, world):
    by_owner = env.submit(env.owner, zero(env))
    other_agent = env.agent(["campaign:submit", "campaign:read"], name="other")
    by_other = env.submit(other_agent, zero(env))
    for campaign in (by_owner, by_other):
        with pytest.raises(Forbidden, match=smoke_credentials.REFUSAL):
            _approve(env, world.approver, campaign)
    assert rows(env, "SELECT id FROM campaign_approvals") == []


def test_the_approver_cannot_approve_after_revoke_or_expiry(env, world):
    campaign = world.submit()
    smoke_credentials.revoke(env.state, world.grant["grant_id"])
    with pytest.raises(Forbidden):
        _approve(env, world.approver, campaign)
    second = World(env)
    campaign = second.submit()
    path = env.state / "tessera.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE credentials SET expires_at='2020-01-01T00:00:00Z' WHERE id=?",
            (second.grant["approver_credential_id"],),
        )
    with pytest.raises(Forbidden):
        _approve(env, second.approver, campaign)
    assert rows(env, "SELECT id FROM campaign_approvals") == []


def test_the_approver_can_do_nothing_but_approve(env, world):
    campaign = world.submit()
    me = world.approver
    body = {
        "kind": "human_session",
        "audience": "control",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    attempts = [
        lambda: env.plain.issue_credential(me, "k1", canonical_digest(body), body, "t" * 40),
        lambda: env.plain.create_project(me, "k2", canonical_digest({"name": "x"}), {"name": "x"}),
        lambda: env.store.cancel_campaign(
            me, campaign["id"], "k3", canonical_digest({}), {"expected_revision": 1, "reason": "x"}
        ),
        lambda: env.store.submit_campaign(
            me, env.project["id"], "k4", canonical_digest({"spec": zero(env)}), {"spec": zero(env)}
        ),
        lambda: env.store.revoke_credential(me, world.grant["agent_credential_id"], "k5", "d" * 64),
    ]
    for attempt in attempts:
        with pytest.raises(Forbidden, match=smoke_credentials.ROUTE_REFUSAL):
            attempt()
    # the one thing it may do still works afterwards
    assert env.approve(campaign, me)["status"] == "running"


def test_http_the_approver_is_limited_to_reads_and_approve(env, world):
    client = TestClient(create_app(env.state))
    campaign = world.submit()
    auth = {"Authorization": "Bearer " + world.grant["approver_token"]}
    path = f"/campaigns/{campaign['id']}"
    assert client.get(path, headers=auth).status_code == 200
    denied = client.post(
        f"/projects/{env.project['id']}/campaigns",
        json={"spec": zero(env)},
        headers={**auth, "Idempotency-Key": "k"},
    )
    assert denied.status_code == 403
    for sub in ("cancel", "pause"):
        assert (
            client.post(
                f"{path}/{sub}",
                json={"expected_revision": 1, "reason": "x"},
                headers={**auth, "Idempotency-Key": "k-" + sub},
            ).status_code
            == 403
        )
    approve = client.post(
        f"{path}/approve",
        json={
            "expected_revision": campaign["revision"],
            "expected_spec_digest": campaign["spec_digest"],
        },
        headers={**auth, "Idempotency-Key": "k-approve"},
    )
    assert approve.status_code == 200, approve.text


# -- revoke -----------------------------------------------------------------------------------


def test_revoke_ends_both_credentials_and_is_idempotent(env, world):
    first = smoke_credentials.revoke(env.state, world.grant["grant_id"])
    second = smoke_credentials.revoke(env.state, world.grant["grant_id"])
    assert first == second and first["revoked_at"]
    for token in (world.grant["agent_token"], world.grant["approver_token"]):
        with pytest.raises(Forbidden):
            env.plain.authenticate(token)
    assert (
        len(
            rows(
                env,
                "SELECT * FROM operator_audit_events WHERE event_type=?",
                "smoke_credentials.revoked",
            )
        )
        == 1
    )
    (row,) = smoke_credentials.list_grants(env.state)
    assert row["state"] == "revoked" and "token" not in json.dumps(row)
    with pytest.raises(NotFound):
        smoke_credentials.revoke(env.state, "smokegrant_missing")


# -- CLI --------------------------------------------------------------------------------------


def _main(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["tessera", *argv])
    code = cli.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    initialize_state(state)
    owner = Store(state).authenticate(
        (state / "owner.header").read_text().strip().split("Bearer ")[1]
    )
    store = Store(state)
    _, project = store.create_project(owner, "p", canonical_digest({"name": "P"}), {"name": "P"})
    out = tmp_path / "creds"
    common = ["--state-dir", str(state)]
    code, stdout, err = _main(
        monkeypatch,
        capsys,
        "dev",
        "smoke-credentials",
        "issue",
        *common,
        "--project-id",
        project["id"],
        "--destination",
        DEST,
        "--out-dir",
        str(out),
    )
    assert code == 0, err
    lines = stdout.split()
    grant_id = lines[0]
    assert grant_id.startswith("smokegrant_") and len(lines) == 4
    agent_token = (out / "agent.token").read_text().strip()
    approver_token = (out / "approver.token").read_text().strip()
    assert agent_token not in stdout and approver_token not in stdout
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for name in ("agent.token", "approver.token", "smoke-grant.json"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    description = json.loads((out / "smoke-grant.json").read_text())
    assert description["schema"] == "tessera.smoke-credentials.v1"
    assert description["grant_id"] == grant_id and description["control_url"] is None
    assert description["providers"] == ["local-docker"] and description["max_budget_cents"] == 0
    assert description["destination"] == DEST and description["project_id"] == project["id"]
    assert agent_token not in json.dumps(description) and approver_token not in json.dumps(
        description
    )
    assert description["snapshot_id"].startswith("snapshot_")
    assert len(description["snapshot_digest"]) == 64
    with _connect(state / "tessera.sqlite3") as connection:
        assert [r["id"] for r in connection.execute("SELECT id FROM source_snapshots")] == [
            description["snapshot_id"]
        ]
    assert store.authenticate(agent_token)["kind"] == "agent"
    assert store.authenticate(approver_token)["kind"] == "human_session"
    # never overwritten
    code, _out, err = _main(
        monkeypatch,
        capsys,
        "dev",
        "smoke-credentials",
        "issue",
        *common,
        "--project-id",
        project["id"],
        "--destination",
        DEST,
        "--out-dir",
        str(out),
    )
    assert code == 1 and "already" in err
    assert (out / "agent.token").read_text().strip() == agent_token
    # refusals: ttl, project
    for extra, project_id in ((["--ttl-minutes", "61"], project["id"]), ([], "project_nope")):
        code, _out, err = _main(
            monkeypatch,
            capsys,
            "dev",
            "smoke-credentials",
            "issue",
            *common,
            "--project-id",
            project_id,
            "--destination",
            DEST,
            "--out-dir",
            str(tmp_path / "other"),
            *extra,
        )
        assert code == 1 and not (tmp_path / "other").exists()
    code, stdout, _err = _main(monkeypatch, capsys, "dev", "smoke-credentials", "list", *common)
    listing = json.loads(stdout)["grants"]
    assert code == 0 and [g["state"] for g in listing] == ["active"]
    code, stdout, _err = _main(
        monkeypatch, capsys, "dev", "smoke-credentials", "revoke", *common, "--grant-id", grant_id
    )
    assert code == 0 and json.loads(stdout)["grant_id"] == grant_id
    with pytest.raises(Forbidden):
        store.authenticate(agent_token)
    code, stdout, _err = _main(monkeypatch, capsys, "dev", "smoke-credentials", "list", *common)
    assert json.loads(stdout)["grants"][0]["state"] == "revoked"


def test_cli_has_no_provider_or_budget_flags():
    parser = cli.parser()
    for flag in ("--provider", "--max-budget-cents", "--budget"):
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "dev",
                    "smoke-credentials",
                    "issue",
                    "--state-dir",
                    "x",
                    "--project-id",
                    "p",
                    "--destination",
                    "d",
                    "--out-dir",
                    "o",
                    flag,
                    "1",
                ]
            )
    assert os.name  # keep the import used
