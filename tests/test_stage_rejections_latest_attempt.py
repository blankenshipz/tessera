"""attempt_for resolves one attempt deterministically and only for the owning worker."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from campaign_fixture import Env
from test_stage_rejections_routes import _grant, _lease

from tessera_control.stage_rejections import attempt_for


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        _grant(db, env, "grant_latest", env.worker["id"])
        _lease(db, "grant_latest", env.worker["id"], "lease_latest")
    return env, campaign, env.store


def _attempt(db, campaign, attempt_id, number, grant_id):
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, grant_id, revision, created_at, updated_at) "
        "VALUES (?, ?, 's1', ?, 'reserved', 4, ?, 1, ?, ?)",
        (attempt_id, campaign["id"], number, grant_id, now, now),
    )


def test_resolves_by_grant_and_by_lease_for_the_owner_only(world):
    env, campaign, store = world
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        _attempt(db, campaign, "att_new", 2, "grant_latest")
    assert attempt_for(store, env.worker, "grant_latest", None) == "att_new"
    assert attempt_for(store, env.worker, None, "lease_latest") == "att_new"
    assert attempt_for(store, {"id": "someone_else"}, "grant_latest", None) is None
    assert attempt_for(store, {"id": "someone_else"}, None, "lease_latest") is None


def test_schema_allows_one_attempt_per_grant_so_the_choice_is_never_ambiguous(world):
    env, campaign, _store = world
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        _attempt(db, campaign, "att_old", 1, "grant_latest")
        with pytest.raises(sqlite3.IntegrityError):
            _attempt(db, campaign, "att_new", 2, "grant_latest")
