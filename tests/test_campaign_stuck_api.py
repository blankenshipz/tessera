"""Stuck-campaign routes: scan, list, acknowledge and settings, with authority and tenancy."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.store import canonical_digest, utc_now

DESTINATION = "planner.local"


class World:
    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.project = self.env.project["id"]
        self.base = f"/projects/{self.project}/campaign-stuck"
        self.client = TestClient(create_app(self.env.state, campaign_runner_interval=None))
        self.issued: dict = {}
        self.n = 0
        campaign = self.env.submit()
        self.campaign = campaign["id"]
        self.sql("UPDATE campaigns SET status='blocked', stop_reason='budget_refused:s1'")

    def sql(self, statement, *args):
        with sqlite3.connect(self.env.state / "tessera.sqlite3") as db:
            return db.execute(statement, args).fetchall()

    def credential(self, name, **body):
        if name in self.issued:
            return self.issued[name]
        body = {
            "audience": "control",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            **body,
        }
        token = f"synthetic-{name}-token-" + "x" * 30
        self.env.plain.issue_credential(
            self.env.owner, f"cred-{name}", canonical_digest(body), body, token
        )
        self.issued[name] = {"Authorization": f"Bearer {token}", "X-Agent-Destination": DESTINATION}
        return self.issued[name]

    def human(self):
        return self.credential("human", kind="human_session")

    def agent(self, actions):
        return self.credential(
            "agent-" + "-".join(a.replace(":", "_") for a in actions),
            kind="agent",
            project_id=self.project,
            external_agent={"actions": actions, "destination": DESTINATION},
        )

    def other_org_human(self):
        from tessera_control.store import Store, _token_hash

        token = "other-org-owner-" + "z" * 40
        with sqlite3.connect(self.env.state / "tessera.sqlite3") as db:
            db.execute("INSERT INTO organizations VALUES (?,?,?)", ("org_b", "B", utc_now()))
            db.execute(
                "INSERT INTO principals VALUES (?,?,?,?,?,?)",
                ("owner_b", "org_b", "human", _token_hash(token), None, utc_now()),
            )
            db.execute(
                "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("cred_b", "owner_b", "local_owner", "control", None, None, None, None,
                 utc_now(), None),
            )  # fmt: skip
        owner_b = Store(self.env.state).authenticate(token)
        body = {
            "audience": "control",
            "kind": "human_session",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        }
        session = "other-org-session-" + "y" * 40
        self.env.plain.issue_credential(owner_b, "cred-b", canonical_digest(body), body, session)
        return {"Authorization": f"Bearer {session}", "X-Agent-Destination": DESTINATION}

    def key(self):
        self.n += 1
        return {"Idempotency-Key": f"k{self.n}"}

    def scan(self, headers=None, key=None, project=None):
        path = f"/projects/{project or self.project}/campaign-stuck/scan"
        return self.client.post(path, headers={**(headers or self.human()), **(key or self.key())})

    def listing(self, headers=None, **params):
        return self.client.get(self.base, params=params, headers=headers or self.human())

    def ack(self, fid, headers=None, note=None, key=None):
        return self.client.post(
            f"{self.base}/{fid}/acknowledge",
            json={} if note is None else {"note": note},
            headers={**(headers or self.human()), **(key or self.key())},
        )


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_human_scans_lists_and_acknowledges_with_receipts(world):
    scanned = world.scan()
    assert scanned.status_code == 200
    (found,) = scanned.json()["opened"]
    assert found["code"] == "campaign_blocked" and scanned.json()["open_count"] == 1
    listed = world.listing().json()
    assert listed["settings"] == {"unchanged_seconds": 3600, "lease_grace_seconds": 300}
    assert [f["id"] for f in listed["findings"]] == [found["id"]]
    acked = world.ack(found["id"], note="looking into it")
    assert acked.status_code == 200 and acked.json()["status"] == "acknowledged"
    assert acked.json()["note"] == "looking into it"
    assert world.listing().json()["findings"] == []  # open filter
    assert [f["id"] for f in world.listing(status="acknowledged").json()["findings"]] == [
        found["id"]
    ]
    kinds = [
        r[0]
        for r in world.sql("SELECT event_type FROM events WHERE event_type LIKE 'campaign.stuck%'")
    ]
    assert kinds == ["campaign.stuck_detected", "campaign.stuck_acknowledged"]


def test_a_scan_replays_with_its_key_and_changes_nothing_when_repeated(world):
    key = {"Idempotency-Key": "same"}
    first = world.scan(key=key)
    replay = world.scan(key=key)
    assert replay.json() == first.json()
    again = world.scan()
    assert again.json()["opened"] == [] and again.json()["open_count"] == 1
    assert (
        world.sql("SELECT COUNT(*) FROM events WHERE event_type='campaign.stuck_detected'")[0][0]
        == 1
    )


def test_an_agent_with_the_read_scope_scans_and_lists_but_cannot_acknowledge(world):
    reader = world.agent(["campaign:read"])
    fid = world.scan(reader).json()["opened"][0]["id"]
    assert world.listing(reader).status_code == 200
    assert world.ack(fid, reader).status_code == 403


def test_acknowledging_needs_the_cancel_scope_and_an_unscoped_agent_is_refused(world):
    fid = world.scan().json()["opened"][0]["id"]
    assert world.ack(fid, world.agent(["campaign:cancel"])).status_code == 200
    nobody = world.agent(["context:read"])
    assert world.scan(nobody).status_code == 403
    assert world.listing(nobody).status_code == 403


def test_acknowledging_an_unknown_or_resolved_finding_is_refused(world):
    assert world.ack("stuck_" + "0" * 32).status_code == 404
    fid = world.scan().json()["opened"][0]["id"]
    world.sql("UPDATE campaigns SET status='running', stop_reason=NULL, updated_at=?", utc_now())
    world.sql("UPDATE campaign_stages SET updated_at=?", utc_now())
    assert world.scan().json()["resolved"] == [fid]
    assert world.ack(fid).status_code == 409


def test_another_organization_cannot_see_scan_or_acknowledge(world):
    fid = world.scan().json()["opened"][0]["id"]
    theirs = world.other_org_human()
    assert world.listing(theirs).status_code == 404
    assert world.scan(theirs).status_code == 404
    assert world.ack(fid, theirs).status_code == 404


def test_settings_are_human_only_validated_and_change_what_is_found(world):
    path = f"{world.base}/settings"
    body = {"unchanged_seconds": 120, "lease_grace_seconds": 10}
    agent = world.agent(["campaign:cancel"])
    assert world.client.put(path, json=body, headers={**agent, **world.key()}).status_code == 403
    bad = {"unchanged_seconds": 5, "lease_grace_seconds": 10}
    assert (
        world.client.put(path, json=bad, headers={**world.human(), **world.key()}).status_code
        == 422
    )
    done = world.client.put(path, json=body, headers={**world.human(), **world.key()})
    assert done.status_code == 200 and done.json() == body
    assert world.listing().json()["settings"] == body
    assert (
        world.sql("SELECT COUNT(*) FROM events WHERE event_type='campaign.stuck_settings_set'")[0][
            0
        ]
        == 1
    )
    # The shorter age now flags a quiet running campaign.
    world.sql(
        "UPDATE campaigns SET status='running', stop_reason=NULL, updated_at='2020-01-01T00:00:00Z'"
    )
    world.sql("UPDATE campaign_stages SET updated_at='2020-01-01T00:00:00Z'")
    codes = [f["code"] for f in world.scan().json()["opened"]]
    assert "no_progress" in codes


def test_a_worker_credential_is_refused(world):
    worker = world.credential("worker", kind="worker", audience="worker", project_id=world.project)
    assert world.listing(worker).status_code in (401, 403)
