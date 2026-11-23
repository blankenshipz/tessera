"""Owner-signed approval delegations (ADR 0122): the record, its triggers, routes and schema.

Synthetic fixtures only; the in-process control store and the real app.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control import approval_delegations as ad
from tessera_control import program_budget
from tessera_control.api import create_app
from tessera_control.approval_trust import AUTOMATION_KEY
from tessera_control.migrations import (
    APPROVAL_DELEGATION_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    migrate_database,
)
from tessera_control.recovery import _table_shapes
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    _token_hash,
    canonical_digest,
    initialize_state,
    validate_database,
)

RULE = {
    "arm_a_variant_id": "va",
    "arm_b_variant_id": "vb",
    "metric": "score",
    "split_lock_id": "lock1",
    "partition": "test",
    "statistic": "mean",
    "comparison": "ge",
    "threshold": 0.5,
    "minimum_pairs": 5,
    "allowed_outcomes": ["pass", "fail"],
}
READ = "campaign:read"


def terms(**overrides: Any) -> dict[str, Any]:
    body = {
        "budget_ceiling_cents": 100,
        "per_run_cap_cents": 20,
        "allowed_providers": ["local-docker"],
        "max_approvals": 5,
        "gate_rules": [{"name": "primary", "bound_outcome": RULE}],
    }
    body.update(overrides)
    return body


def later(days: float = 2) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


class Del:
    def __init__(self, tmp_path) -> None:
        self.env = Env(tmp_path)
        self.store = ad.ApprovalDelegationCommands(self.env.state)
        self.counter = 0

    def key(self, prefix: str = "k") -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    @property
    def project(self) -> str:
        return self.env.project["id"]

    def body(self, **overrides: Any) -> dict[str, Any]:
        body = {"terms": terms(), "expires_at": later()}
        body.update(overrides)
        return body

    def create(self, actor=None, key=None, **overrides: Any) -> dict[str, Any]:
        body = self.body(**overrides)
        status, result = self.store.create_approval_delegation(
            actor or self.env.human(),
            self.project,
            key or self.key("create"),
            canonical_digest(body),
            body,
        )
        assert status == 201
        return result

    def revoke(self, record, actor=None, revision=None, key=None):
        body = {"expected_revision": revision or record["revision"]}
        return self.store.revoke_approval_delegation(
            actor or self.env.human(),
            record["id"],
            key or self.key("revoke"),
            canonical_digest(body),
            body,
        )

    def reader(self) -> dict[str, Any]:
        return self.env.agent([READ], name="reader")

    def row(self, ident: str) -> dict[str, Any]:
        return dict(self.env.query("SELECT * FROM approval_delegations WHERE id=?", ident)[0])

    def other_tenant(self) -> dict[str, str]:
        value = "synthetic-other-tenant-token-" + "x" * 30
        with sqlite3.connect(self.env.state / "tessera.sqlite3") as db:
            db.execute(
                "INSERT INTO organizations (id, name, created_at) VALUES "
                "('org_other','Other','2026-01-01T00:00:00Z')"
            )
            db.execute(
                "INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at) "
                "VALUES ('other_human','org_other','human',?,NULL,'2026-01-01T00:00:00Z')",
                (_token_hash(value),),
            )
            db.execute(
                "INSERT INTO credentials (id, principal_id, kind, audience, project_id, "
                "parent_id, expires_at, revoked_at, created_at, created_by_principal_id) "
                "VALUES ('cred_other','other_human','local_owner','control',NULL,NULL,NULL,"
                "NULL,'2026-01-01T00:00:00Z','other_human')"
            )
        return {"Authorization": f"Bearer {value}"}

    def approve_under(self, record) -> dict[str, Any]:
        """Synthetic fixture only: approve a campaign and stamp the delegation onto its row."""
        campaign, approval = self.env.approved()
        self.env.execute(
            "UPDATE campaign_approvals SET delegation_id=? WHERE id=?",
            record["id"],
            approval["id"],
        )
        return campaign


@pytest.fixture
def dele(tmp_path):
    program_budget.reset()
    yield Del(tmp_path)
    program_budget.reset()


# -- creation ------------------------------------------------------------------------------


def test_human_creates_active_delegation_with_provenance(dele):
    record = dele.create()
    assert record["id"].startswith("deleg_")
    assert record["status"] == "active" and record["state"] == "active"
    assert record["revision"] == 1 and record["revoked_at"] is None
    assert record["decision_class"] == "human_session"
    assert record["created_by_principal_id"] == dele.env.human()["id"]
    assert record["terms"]["budget_ceiling_cents"] == 100
    assert record["terms_digest"] == canonical_digest(record["terms"])
    assert dele.row(record["id"])["org_id"] == dele.env.human()["org_id"]


@pytest.mark.parametrize("who", ["scoped_agent", "plain_agent", "worker", "bare_owner"])
def test_only_a_human_creates(dele, who):
    actor = {
        "scoped_agent": lambda: dele.env.agent([READ, "campaign:submit"], name="scoped"),
        "plain_agent": lambda: dele.env.agent(None, name="plain"),
        "worker": lambda: dele.env.worker,
        "bare_owner": lambda: {k: v for k, v in dele.env.owner.items() if k != AUTOMATION_KEY},
    }[who]()
    body = dele.body()
    with pytest.raises(Forbidden):
        dele.store.create_approval_delegation(
            actor, dele.project, "k", canonical_digest(body), body
        )
    assert dele.env.query("SELECT count(*) AS n FROM approval_delegations")[0]["n"] == 0


def test_another_tenant_cannot_create_or_read(dele):
    record = dele.create()
    client = TestClient(create_app(dele.env.state))
    other = dele.other_tenant()
    body = dele.body()
    denied = client.post(
        f"/projects/{dele.project}/approval-delegations",
        json=body,
        headers={**other, "Idempotency-Key": "o1"},
    )
    assert denied.status_code == 404
    assert client.get(f"/approval-delegations/{record['id']}", headers=other).status_code == 404
    assert (
        client.get(f"/approval-delegations/{record['id']}/decisions", headers=other).status_code
        == 404
    )
    revoke = client.post(
        f"/approval-delegations/{record['id']}/revoke",
        json={"expected_revision": 1},
        headers={**other, "Idempotency-Key": "o2"},
    )
    assert revoke.status_code == 404
    assert dele.row(record["id"])["status"] == "active"


def test_load_delegation_hides_another_organization(dele):
    record = dele.create()
    with closing(_connect(dele.env.state / "tessera.sqlite3")) as connection:
        assert ad.load_delegation(connection, record["id"], record["org_id"])["id"] == record["id"]
        with pytest.raises(NotFound):
            ad.load_delegation(connection, record["id"], "org_other")
        with pytest.raises(NotFound):
            ad.load_delegation(connection, "deleg_missing", record["org_id"])


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"terms": terms(per_run_cap_cents=101)}, "per_run_cap_cents"),
        ({"terms": terms(allowed_providers=["local-docker", "local-docker"])}, "unique"),
        ({"terms": terms(allowed_providers=["no-such-provider"])}, "unregistered"),
        ({"terms": terms(program_id="no-program")}, "program"),
        ({"terms": terms(unknown_field=1)}, "unknown_field"),
        ({"expires_at": later(-1)}, "expires_at"),
        ({"expires_at": later(31)}, "expires_at"),
        ({"expires_at": "2030-01-01T00:00:00"}, "expires_at"),
        ({"expires_at": "tomorrow"}, "expires_at"),
    ],
)
def test_terms_and_expiry_validation(dele, overrides, match):
    body = dele.body(**overrides)
    with pytest.raises(StoreError, match=match):
        dele.store.create_approval_delegation(
            dele.env.human(), dele.project, "k", canonical_digest(body), body
        )
    assert dele.env.query("SELECT count(*) AS n FROM approval_delegations")[0]["n"] == 0


def test_expiry_at_the_limit_is_accepted(dele):
    record = dele.create(expires_at=later(29.9))
    assert record["state"] == "active"


def test_program_must_be_configured_and_cover_the_project(dele):
    body = dele.body(terms=terms(program_id="prog-a"))
    with pytest.raises(StoreError, match="program"):  # no program budget configured
        dele.store.create_approval_delegation(
            dele.env.human(), dele.project, "k1", canonical_digest(body), body
        )
    config = program_budget.ProgramBudgetConfig.model_validate(
        {
            "schema": program_budget.PROGRAM_BUDGET_SCHEMA,
            "programs": [
                {"id": "prog-a", "cap_cents": 500, "project_ids": [dele.project]},
                {"id": "prog-b", "cap_cents": 500, "project_ids": ["project_elsewhere"]},
            ],
        }
    )
    program_budget.configure(config)
    record = dele.create(terms=terms(program_id="prog-a"))
    assert record["terms"]["program_id"] == "prog-a"
    body = dele.body(terms=terms(program_id="prog-b"))
    with pytest.raises(StoreError, match="cover"):
        dele.store.create_approval_delegation(
            dele.env.human(), dele.project, "k2", canonical_digest(body), body
        )
    program_budget.mark_broken()
    with pytest.raises(StoreError, match="program"):
        dele.store.create_approval_delegation(
            dele.env.human(),
            dele.project,
            "k3",
            canonical_digest(dele.body(terms=terms(program_id="prog-a"))),
            dele.body(terms=terms(program_id="prog-a")),
        )


def test_create_is_idempotent_and_key_reuse_conflicts(dele):
    body = dele.body()
    args = (dele.env.human(), dele.project, "same-key", canonical_digest(body), body)
    first = dele.store.create_approval_delegation(*args)
    again = dele.store.create_approval_delegation(*args)
    assert first == again
    assert dele.env.query("SELECT count(*) AS n FROM approval_delegations")[0]["n"] == 1
    other = dele.body(expires_at=later(3))
    with pytest.raises(Conflict):
        dele.store.create_approval_delegation(
            dele.env.human(), dele.project, "same-key", canonical_digest(other), other
        )


def test_created_event_carries_no_rule_body(dele):
    record = dele.create()
    (event,) = dele.env.events("campaign.delegation_created")
    payload = event["payload"]
    assert payload["delegation_id"] == record["id"]
    assert payload["terms_digest"] == record["terms_digest"]
    assert payload["expires_at"] == record["expires_at"]
    assert payload["allowed_providers"] == ["local-docker"]
    assert payload["budget_ceiling_cents"] == 100 and payload["per_run_cap_cents"] == 20
    text = event["payload_json"]
    for leaked in ("gate_rules", "bound_outcome", "split_lock_id", "lock1", "threshold"):
        assert leaked not in text


# -- revoke --------------------------------------------------------------------------------


def test_human_revokes_and_state_becomes_revoked(dele):
    record = dele.create()
    status, revoked = dele.revoke(record)
    assert status == 200
    assert revoked["status"] == "revoked" and revoked["state"] == "revoked"
    assert revoked["revision"] == 2 and revoked["revoked_at"]
    assert revoked["revoked_by_principal_id"] == dele.env.human()["id"]
    (event,) = dele.env.events("campaign.delegation_revoked")
    assert event["payload"]["delegation_id"] == record["id"]
    assert "terms_json" not in event["payload_json"]
    with pytest.raises(Conflict, match="already revoked"):
        dele.revoke(record, revision=2)


def test_revoke_is_human_only(dele):
    record = dele.create()
    for actor in (
        dele.env.agent(["campaign:cancel", READ], name="canceller"),
        dele.env.worker,
    ):
        with pytest.raises(Forbidden):
            dele.revoke(record, actor=actor)
    assert dele.row(record["id"])["status"] == "active"


def test_revoke_stale_revision_conflicts_and_replay_is_stable(dele):
    record = dele.create()
    with pytest.raises(Conflict, match="revision"):
        dele.revoke(record, revision=7)
    body = {"expected_revision": 1}
    args = (dele.env.human(), record["id"], "rv", canonical_digest(body), body)
    first = dele.store.revoke_approval_delegation(*args)
    assert dele.store.revoke_approval_delegation(*args) == first
    assert len(dele.env.events("campaign.delegation_revoked")) == 1


# -- state, usage, reads ---------------------------------------------------------------------


def test_derived_state_expired_and_revoked_beats_expired(dele):
    record = dele.create()
    row = dele.row(record["id"])
    now = datetime.now(UTC)
    assert ad.delegation_state(row, now) == "active"
    assert ad.delegation_state(row, now + timedelta(days=3)) == "expired"
    assert ad.delegation_state({**row, "status": "revoked"}, now + timedelta(days=3)) == "revoked"
    assert ad.parse_terms(row).per_run_cap_cents == 20


def test_expired_state_is_visible_in_reads(dele):
    record = dele.create()
    # Synthetic fixture only: age the record by rewriting the immutable expiry directly.
    with sqlite3.connect(dele.env.state / "tessera.sqlite3") as db:
        db.execute("DROP TRIGGER approval_delegations_terms_fixed")
        db.execute(
            "UPDATE approval_delegations SET expires_at='2020-01-01T00:00:00Z' WHERE id=?",
            (record["id"],),
        )
    got = dele.store.get_approval_delegation(dele.reader(), record["id"])
    assert got["state"] == "expired" and got["status"] == "active"


def test_usage_counts_cancelled_campaigns_by_what_they_spent(dele):
    record = dele.create()
    with closing(_connect(dele.env.state / "tessera.sqlite3")) as connection:
        assert ad.committed_cents(connection, record["id"]) == 0
        assert ad.approval_count(connection, record["id"]) == 0
    first = dele.approve_under(record)
    second = dele.approve_under(record)
    dele.env.approved()  # an ordinary approval is not counted
    with closing(_connect(dele.env.state / "tessera.sqlite3")) as connection:
        assert ad.committed_cents(connection, record["id"]) == 20  # two 10-cent campaigns
        assert ad.approval_count(connection, record["id"]) == 2
        connection.execute(
            "UPDATE campaigns SET status='cancelled', settled_cents=4 WHERE id=?", (second["id"],)
        )
        # a cancelled campaign counts what it spent (never its cap, never nothing)
        assert ad.committed_cents(connection, record["id"]) == 10 + 4
        assert ad.approval_count(connection, record["id"]) == 2  # cancelled still counted
    got = dele.store.get_approval_delegation(dele.reader(), record["id"])
    assert got["usage"] == {"committed_cents": 14, "approvals": 2}
    assert first["id"] != second["id"]


def decide(dele, record, campaign, **kwargs):
    defaults = {
        "kind": "gate",
        "outcome": "escalated",
        "reason": "rule_not_met",
        "rule_name": "primary",
        "evaluation": {"observed": 0.25, "threshold": 0.5},
        "now": "2026-06-01T00:00:00Z",
    }
    defaults.update(kwargs)
    with closing(_connect(dele.env.state / "tessera.sqlite3")) as connection:
        campaign_row = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign["id"],)
        ).fetchone()
        delegation = ad.load_delegation(connection, record["id"], record["org_id"])
        ident = ad.record_decision(
            connection,
            delegation=delegation,
            campaign=campaign_row,
            actor=dele.env.human(),
            **defaults,
        )
        connection.commit()
        return ident


def test_record_decision_and_decision_reads_paginate(dele):
    record = dele.create()
    campaign = dele.approve_under(record)
    ids = [decide(dele, record, campaign, now=f"2026-06-01T00:00:0{n}Z") for n in range(5)]
    assert all(i.startswith("ddec_") for i in ids)
    reader = dele.reader()
    page = dele.store.list_delegated_decisions(reader, record["id"], 1, 2)["decisions"]
    assert [d["id"] for d in page] == ids[1:3]
    assert page[0]["evaluation"] == {"observed": 0.25, "threshold": 0.5}
    assert page[0]["kind"] == "gate" and page[0]["outcome"] == "escalated"
    assert page[0]["campaign_spec_digest"] == campaign["spec_digest"]
    assert len(dele.store.list_delegated_decisions(reader, record["id"])["decisions"]) == 5
    got = dele.store.get_approval_delegation(reader, record["id"])
    assert [d["id"] for d in got["decisions"]] == ids[::-1]
    assert "terms_json" not in got and got["terms"]["gate_rules"][0]["name"] == "primary"


def test_read_bounds_and_scopes(dele):
    record = dele.create()
    reader = dele.reader()
    for offset, limit in ((-1, 10), (0, 0), (0, 201), (2**63, 10)):
        with pytest.raises(StoreError, match="page"):
            dele.store.list_approval_delegations(reader, dele.project, None, offset, limit)
        with pytest.raises(StoreError, match="page"):
            dele.store.list_delegated_decisions(reader, record["id"], offset, limit)
    with pytest.raises(StoreError, match="status"):
        dele.store.list_approval_delegations(reader, dele.project, "bogus")
    no_read = dele.env.agent(["campaign:cancel"], name="noread")
    with pytest.raises(Forbidden):
        dele.store.get_approval_delegation(no_read, record["id"])
    with pytest.raises(Forbidden):
        dele.store.get_approval_delegation(dele.env.worker, record["id"])
    dele.revoke(record)
    second = dele.create()
    assert [
        r["id"]
        for r in dele.store.list_approval_delegations(reader, dele.project)["approval_delegations"]
    ] == sorted([record["id"], second["id"]], key=lambda i: (dele.row(i)["created_at"], i))
    only = dele.store.list_approval_delegations(reader, dele.project, "revoked")
    assert [r["id"] for r in only["approval_delegations"]] == [record["id"]]
    assert dele.store.list_approval_delegations(reader, dele.project, None, 1, 1)[
        "approval_delegations"
    ][0]["state"] in {"active", "revoked"}


def test_routes_round_trip(dele):
    dele.env.human()
    dele.env.agent([READ], name="reader")
    client = TestClient(create_app(dele.env.state))
    human = {"Authorization": "Bearer synthetic-human-token-" + "x" * 30}
    agent = {
        "Authorization": "Bearer synthetic-reader-token-" + "x" * 30,
        "X-Agent-Destination": "planner.local",
    }
    base = f"/projects/{dele.project}/approval-delegations"
    body = dele.body()
    denied = client.post(base, json=body, headers={**agent, "Idempotency-Key": "a1"})
    assert denied.status_code == 403
    invalid = client.post(
        base,
        json={**body, "terms": terms(per_run_cap_cents=500)},
        headers={**human, "Idempotency-Key": "a2"},
    )
    assert invalid.status_code == 422
    created = client.post(base, json=body, headers={**human, "Idempotency-Key": "a3"})
    assert created.status_code == 201, created.text
    record = created.json()
    listed = client.get(base + "?status=active&limit=1", headers=agent)
    assert [r["id"] for r in listed.json()["approval_delegations"]] == [record["id"]]
    assert client.get(base + "?limit=0", headers=agent).status_code == 422
    detail = client.get(f"/approval-delegations/{record['id']}", headers=agent).json()
    assert detail["usage"] == {"committed_cents": 0, "approvals": 0} and detail["decisions"] == []
    decisions = client.get(f"/approval-delegations/{record['id']}/decisions", headers=agent)
    assert decisions.json() == {"decisions": []}
    assert (
        client.get(
            f"/approval-delegations/{record['id']}/decisions?limit=201", headers=agent
        ).status_code
        == 422
    )
    revoke_path = f"/approval-delegations/{record['id']}/revoke"
    refused = client.post(
        revoke_path, json={"expected_revision": 1}, headers={**agent, "Idempotency-Key": "r1"}
    )
    assert refused.status_code == 403
    stale = client.post(
        revoke_path, json={"expected_revision": 9}, headers={**human, "Idempotency-Key": "r2"}
    )
    assert stale.status_code == 409
    done = client.post(
        revoke_path, json={"expected_revision": 1}, headers={**human, "Idempotency-Key": "r3"}
    )
    assert done.status_code == 200 and done.json()["state"] == "revoked"
    replay = client.post(
        revoke_path, json={"expected_revision": 1}, headers={**human, "Idempotency-Key": "r3"}
    )
    assert replay.status_code == 200 and replay.json() == done.json()


# -- triggers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "assignment",
    [
        "terms_json='{}'",
        "terms_digest='" + "0" * 64 + "'",
        "expires_at='2099-01-01T00:00:00Z'",
        "project_id='other'",
        "org_id='other'",
        "created_by_principal_id='other'",
        "decision_class='x'",
        "created_at='2000-01-01T00:00:00Z'",
        "id='deleg_other'",
        "status='expired'",
        "revision=0",
    ],
)
def test_immutable_columns_refuse_update(dele, assignment):
    record = dele.create()
    with sqlite3.connect(dele.env.state / "tessera.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(f"UPDATE approval_delegations SET {assignment} WHERE id=?", (record["id"],))
    assert dele.row(record["id"])["status"] == "active"


def test_status_only_moves_active_to_revoked_and_rows_are_kept(dele):
    record = dele.create()
    path = dele.env.state / "tessera.sqlite3"
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM approval_delegations WHERE id=?", (record["id"],))
        db.execute(
            "UPDATE approval_delegations SET status='revoked', revision=2, "
            "revoked_at='2026-01-01T00:00:00Z' WHERE id=?",
            (record["id"],),
        )
        with pytest.raises(sqlite3.DatabaseError):  # never back to active
            db.execute(
                "UPDATE approval_delegations SET status='active', revoked_at=NULL WHERE id=?",
                (record["id"],),
            )
        with pytest.raises(sqlite3.DatabaseError):  # the revocation stamp is fixed
            db.execute(
                "UPDATE approval_delegations SET revoked_at='2027-01-01T00:00:00Z' WHERE id=?",
                (record["id"],),
            )
    assert dele.row(record["id"])["status"] == "revoked"


def test_decisions_are_append_only_and_checked(dele):
    record = dele.create()
    campaign = dele.approve_under(record)
    ident = decide(dele, record, campaign)
    with sqlite3.connect(dele.env.state / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE delegated_decisions SET reason='x' WHERE id=?", (ident,))
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM delegated_decisions WHERE id=?", (ident,))
    with pytest.raises(sqlite3.IntegrityError):
        decide(dele, record, campaign, outcome="maybe")
    with pytest.raises(sqlite3.IntegrityError):
        decide(dele, record, campaign, kind="other")
    assert dele.env.query("SELECT count(*) AS n FROM delegated_decisions")[0]["n"] == 1


# -- schema convergence --------------------------------------------------------------------

NEW_TABLES = ("approval_delegations", "delegated_decisions")
MINE = ("approval_delegations_", "delegated_decisions_")


def _objects(path, tables) -> dict[str, str]:
    marks = ",".join("?" for _ in tables)
    with sqlite3.connect(path) as db:
        return dict(
            db.execute(
                f"SELECT name, sql FROM sqlite_master WHERE tbl_name IN ({marks})", tables
            ).fetchall()
        )


def _columns(path, table) -> list[str]:
    with sqlite3.connect(path) as db:
        return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def test_version_is_registered_as_a_step():
    assert APPROVAL_DELEGATION_SCHEMA_VERSION == 86 <= LATEST_SCHEMA_VERSION
    assert MIGRATIONS[85].__name__ == "migrate_approval_delegation_schema"


def test_the_two_new_columns_are_last_and_nullable(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    path = state / "tessera.sqlite3"
    for table in ("campaign_approvals", "campaign_gate_decisions"):
        assert "delegation_id" in _columns(path, table)
    with sqlite3.connect(path) as db:
        for table in ("campaign_approvals", "campaign_gate_decisions"):
            info = {r[1]: r for r in db.execute(f"PRAGMA table_info({table})")}
            assert info["delegation_id"][2] == "TEXT" and info["delegation_id"][3] == 0


def test_previous_version_migrates_to_the_fresh_schema(tmp_path):
    fresh_dir, migrated_dir = tmp_path / "fresh", tmp_path / "migrated"
    initialize_state(fresh_dir)
    initialize_state(migrated_dir)
    fresh_path = fresh_dir / "tessera.sqlite3"
    path = migrated_dir / "tessera.sqlite3"
    tables = (*NEW_TABLES, "campaign_approvals", "campaign_gate_decisions")
    fresh = _objects(fresh_path, tables)
    assert {"approval_delegations_terms_fixed", "delegated_decisions_no_update"} <= set(fresh)
    with sqlite3.connect(path) as db:
        for name, sql in fresh.items():
            if sql and sql.startswith("CREATE TRIGGER") and name.startswith(MINE):
                db.execute(f"DROP TRIGGER {name}")
        db.execute("DROP TABLE delegated_decisions")
        db.execute("DROP TABLE approval_delegations")
        db.execute("ALTER TABLE campaign_approvals DROP COLUMN delegation_id")
        db.execute("ALTER TABLE campaign_gate_decisions DROP COLUMN delegation_id")
        db.execute("UPDATE operator_backup_health SET last_success_at='t',last_backup_id='b'")
        db.execute(
            "UPDATE schema_meta SET schema_version=?", (APPROVAL_DELEGATION_SCHEMA_VERSION - 1,)
        )
    with pytest.raises(ValueError, match="migrate"):
        validate_database(path)
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION  # no-op
    validate_database(path)
    migrated = _objects(path, tables)
    assert set(migrated) == set(fresh)
    for name in (
        *NEW_TABLES,
        *(n for n, s in fresh.items() if s and s.startswith("CREATE TRIGGER")),
    ):
        assert migrated[name] == fresh[name]
    for table in tables:
        assert sorted(_columns(path, table)) == sorted(_columns(fresh_path, table))
    # The recovery validator's table shapes agree, and a migrated backup-health row is reset.
    with sqlite3.connect(path) as a, sqlite3.connect(fresh_path) as b:
        assert _table_shapes(a)[0] == _table_shapes(b)[0]
        assert a.execute("SELECT last_success_at FROM operator_backup_health").fetchone() == (None,)
    # The trigger behaviour survives the migration.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO delegated_decisions (id, delegation_id, org_id, project_id, campaign_id,"
            " kind, outcome, reason, rule_name, evaluation_json, campaign_spec_digest,"
            " actor_principal_id, actor_kind, created_at) VALUES ('ddec_1','d','o','p','c',"
            "'gate','go',NULL,NULL,'{}','x','a','human','t')"
        )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM delegated_decisions")
