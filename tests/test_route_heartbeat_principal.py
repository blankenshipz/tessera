"""The principal the Salad executor heartbeats as is the grant principal the route view reads."""

import sqlite3
from datetime import UTC, datetime, timedelta

from test_campaign_route_live_path import _insert
from test_execution import _owner, _worker

from tessera_control.campaign_route_live_path import campaign_live_path
from tessera_control.campaign_salad_executor import StoreControl
from tessera_control.store import Store, canonical_digest, initialize_state

ROUTE = "route_hb"


def _setup(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    owner = _owner(state)
    _, project = Store(state).create_project(
        owner, "project", canonical_digest({"name": "P"}), {"name": "P"}
    )
    executor = _worker(state, owner, project["id"], "executor-token")
    other = _worker(state, owner, project["id"], "other-token")
    # Plain connection (foreign keys off), as the route-view tests seed their fixtures.
    db = _executor(state)
    with db:
        _insert(
            db,
            "campaigns",
            id="c",
            org_id=owner["org_id"],
            spec_digest="a" * 64,
            status="running",
            budget_cap_cents=100,
            revision=1,
        )
        _insert(
            db,
            "campaign_stage_attempts",
            id="a",
            campaign_id="c",
            stage_key="train",
            attempt=1,
            status="running",
            hold_cents=0,
            revision=1,
            grant_id="g",
        )
        # The grant records the principal the executor authenticates as.
        _insert(
            db,
            "execution_grants",
            id="g",
            worker_principal_id=executor["id"],
            currency="USD",
            status="active",
        )
        _insert(
            db,
            "campaign_salad_attempts",
            attempt_id="a",
            phase="executing",
            lease_id=None,
            route_id=ROUTE,
            revision=1,
            updated_at="x",
        )
        _insert(db, "campaign_salad_stages", campaign_id="c", stage_key="train", profile="p")
        # Both workers were last seen long ago.
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        db.execute("UPDATE worker_registry SET last_seen_at = ?, heartbeat_at = ?", (old, old))
    db.close()
    return state, owner, executor, other


def _executor(state):
    db = sqlite3.connect(state / "tessera.sqlite3")
    db.row_factory = sqlite3.Row
    return db


def _freshness(state, owner):
    db = _executor(state)
    try:
        return campaign_live_path(db, owner["org_id"], ROUTE)["executor"]
    finally:
        db.close()


def test_executor_heartbeat_principal_is_the_grant_principal_the_view_reads(tmp_path):
    state, owner, executor, other = _setup(tmp_path)
    assert _freshness(state, owner)["freshness"] == "stale"
    # A heartbeat under a different worker principal never freshens this grant's executor.
    StoreControl(state, other).heartbeat()
    assert _freshness(state, owner)["freshness"] == "stale"
    # The executor's own authenticated worker (what build_executor passes) does.
    StoreControl(state, executor).heartbeat()
    view = _freshness(state, owner)
    assert view["registered"] is True and view["freshness"] == "fresh"


def test_executor_work_selects_only_attempts_whose_grant_principal_is_its_own(tmp_path):
    state, _, executor, other = _setup(tmp_path)
    assert [w["attempt_id"] for w in StoreControl(state, executor).work()] == ["a"]
    assert StoreControl(state, other).work() == []


def test_unregistered_grant_principal_fails_closed(tmp_path):
    state, owner, executor, _ = _setup(tmp_path)
    db = _executor(state)
    with db:
        db.execute("UPDATE execution_grants SET worker_principal_id = 'unknown' WHERE id = 'g'")
    db.close()
    StoreControl(state, executor).heartbeat()
    assert _freshness(state, owner) == {"registered": False}
