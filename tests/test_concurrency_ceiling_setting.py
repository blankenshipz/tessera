"""The operator ceiling on a project's execution concurrency (ADR 0171); synthetic, no provider."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient

from tessera_control import campaign_placement
from tessera_control.api import create_app
from tessera_control.concurrency_ceiling import (
    DEFAULT_PROJECT_CONCURRENCY_CEILING,
    ENV_PROJECT_CONCURRENCY_CEILING,
    HARD_PROJECT_CONCURRENCY_CAP,
    ConcurrencyCeiling,
    ConcurrencyCeilingError,
    concurrency_ceiling,
    operator_concurrency_ceiling,
)
from tessera_control.execution import PROJECT_LIMITS_TABLE_SQL
from tessera_control.execution_models import ConfigureProjectExecution
from tessera_control.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    PROJECT_CONCURRENCY_SCHEMA_VERSION,
)
from tessera_control.store import initialize_state, migrate_state

OLD_LIMITS_TABLE = """CREATE TABLE execution_project_limits (
        project_id TEXT PRIMARY KEY REFERENCES projects(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        currency TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
        monetary_limit_cents INTEGER NOT NULL CHECK (monetary_limit_cents >= 0),
        max_concurrency INTEGER NOT NULL CHECK (max_concurrency BETWEEN 1 AND 8),
        held_cents INTEGER NOT NULL DEFAULT 0 CHECK (held_cents >= 0),
        settled_cents INTEGER NOT NULL DEFAULT 0 CHECK (settled_cents >= 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""


@pytest.fixture(autouse=True)
def _fresh_ceiling(monkeypatch):
    monkeypatch.delenv(ENV_PROJECT_CONCURRENCY_CEILING, raising=False)
    operator_concurrency_ceiling.cache_clear()
    yield
    operator_concurrency_ceiling.cache_clear()


def _set_ceiling(monkeypatch, value: str) -> None:
    monkeypatch.setenv(ENV_PROJECT_CONCURRENCY_CEILING, value)
    operator_concurrency_ceiling.cache_clear()


class World:
    def __init__(self, tmp_path):
        self.state = tmp_path / "state"
        initialize_state(self.state)
        self.owner = {
            "Authorization": (self.state / "owner.header").read_text().strip().split(": ", 1)[1]
        }
        self.client = TestClient(create_app(self.state))
        self.sequence = count()
        self.project = self.call("POST", "/projects", {"name": "Synthetic ceiling"}).json()

    def call(self, method, path, body=None, headers=None):
        return self.client.request(
            method,
            path,
            json=body,
            headers=(headers or self.owner) | {"Idempotency-Key": f"ccs-{next(self.sequence)}"},
        )

    def limits(self, max_concurrency, expected_revision=0, headers=None, **extra):
        return self.call(
            "PUT",
            f"/projects/{self.project['id']}/execution-limits",
            {
                "expected_revision": expected_revision,
                "currency": "USD",
                "monetary_limit_cents": 100,
                "max_concurrency": max_concurrency,
                **extra,
            },
            headers=headers,
        )

    def events(self):
        with sqlite3.connect(self.state / "tessera.sqlite3") as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload_json FROM events "
                    "WHERE event_type='execution.limits_configured' ORDER BY cursor"
                )
            ]


def test_default_is_eight_and_unconfigured():
    ceiling = concurrency_ceiling({})
    assert ceiling == ConcurrencyCeiling(8, configured=False)
    assert DEFAULT_PROJECT_CONCURRENCY_CEILING == 8
    assert ceiling.evidence() == {}
    assert concurrency_ceiling({ENV_PROJECT_CONCURRENCY_CEILING: ""}).value == 8
    assert operator_concurrency_ceiling().value == 8


def test_default_admits_eight_refuses_nine_and_records_no_new_field(tmp_path):
    world = World(tmp_path)
    refused = world.limits(9)
    assert refused.status_code == 422
    assert "ceiling of 8" in refused.json()["detail"]
    accepted = world.limits(8)
    assert accepted.status_code == 201, accepted.text
    # Response and event bytes are the pre-ceiling shape: no new key when the setting is unset.
    assert accepted.json() == {
        "project_id": world.project["id"],
        "revision": 1,
        "currency": "USD",
        "monetary_limit_cents": 100,
        "max_concurrency": 8,
        "held_cents": 0,
        "settled_cents": 0,
        "created_at": accepted.json()["created_at"],
        "updated_at": accepted.json()["updated_at"],
        "operational_only": True,
    }
    assert world.events() == [
        {
            "currency": "USD",
            "monetary_limit_cents": 100,
            "max_concurrency": 8,
            "operational_only": True,
        }
    ]
    # Above the hard cap the request is invalid as before (request validation, 422).
    assert world.limits(HARD_PROJECT_CONCURRENCY_CAP + 1, 1).json() == {
        "detail": "Invalid request."
    }


def test_operator_raised_ceiling_admits_more_and_is_recorded(tmp_path, monkeypatch):
    _set_ceiling(monkeypatch, "12")
    world = World(tmp_path)
    accepted = world.limits(12)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["max_concurrency"] == 12
    assert "concurrency_ceiling" not in accepted.json()
    refused = world.limits(13, 1)
    assert refused.status_code == 422
    assert "ceiling of 12" in refused.json()["detail"]
    assert world.events() == [
        {
            "currency": "USD",
            "monetary_limit_cents": 100,
            "max_concurrency": 12,
            "operational_only": True,
            "concurrency_ceiling": 12,
        }
    ]
    with sqlite3.connect(world.state / "tessera.sqlite3") as db:
        assert db.execute("SELECT max_concurrency FROM execution_project_limits").fetchone() == (
            12,
        )


def test_explicit_eight_is_recorded_as_configured(tmp_path, monkeypatch):
    _set_ceiling(monkeypatch, "8")
    world = World(tmp_path)
    assert world.limits(8).status_code == 201
    assert world.events()[0]["concurrency_ceiling"] == 8


@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "65", "1000000", "abc", "8.0", "1e1", " 9", "9 ", "+9", "٩", "0x10"],
)
def test_invalid_operator_values_fail_closed(raw, monkeypatch, tmp_path):
    with pytest.raises(ConcurrencyCeilingError, match=ENV_PROJECT_CONCURRENCY_CEILING):
        concurrency_ceiling({ENV_PROJECT_CONCURRENCY_CEILING: raw})
    _set_ceiling(monkeypatch, raw)
    initialize_state(tmp_path / "state")
    # The service refuses to start rather than falling back to any value.
    with pytest.raises(ConcurrencyCeilingError):
        create_app(tmp_path / "state")


def test_bounds_are_inclusive():
    assert concurrency_ceiling({ENV_PROJECT_CONCURRENCY_CEILING: "1"}).value == 1
    assert concurrency_ceiling({ENV_PROJECT_CONCURRENCY_CEILING: "64"}).value == 64
    with pytest.raises(ConcurrencyCeilingError):
        ConcurrencyCeiling(True)  # a bool is not a whole number


def test_requests_cannot_raise_the_ceiling(tmp_path):
    world = World(tmp_path)
    assert set(ConfigureProjectExecution.model_fields) == {
        "expected_revision",
        "currency",
        "monetary_limit_cents",
        "max_concurrency",
    }
    # An extra field naming the ceiling is not accepted.
    smuggled = world.limits(12, concurrency_ceiling=64)
    assert smuggled.status_code == 422
    assert world.limits(12).status_code == 422
    # An agent credential cannot set project limits at all, even within the ceiling.
    token = "synthetic-agent-ceiling-" + "x" * 40
    credential = world.call(
        "POST",
        "/credentials",
        {
            "kind": "agent",
            "audience": "control",
            "token": token,
            "project_id": world.project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert credential.status_code == 201, credential.text
    agent = {"Authorization": f"Bearer {token}"}
    assert world.limits(4, headers=agent).status_code == 403
    assert world.events() == []
    assert operator_concurrency_ceiling().value == 8


def _limits_db(stored: int, unresolved: int) -> sqlite3.Connection:
    # Only the two tables the admission check reads, with the production limits table.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    db.execute(PROJECT_LIMITS_TABLE_SQL)
    db.execute("CREATE TABLE execution_grants (id TEXT PRIMARY KEY, project_id TEXT, status TEXT)")
    db.execute("INSERT INTO projects VALUES ('p')")
    db.execute(
        "INSERT INTO execution_project_limits VALUES ('p',1,'USD',100,?,0,0,'t','t')", (stored,)
    )
    db.executemany(
        "INSERT INTO execution_grants VALUES (?,'p','active')",
        [(f"g{index}",) for index in range(unresolved)],
    )
    return db


def test_lowered_ceiling_caps_admission_of_a_higher_stored_limit(monkeypatch):
    _set_ceiling(monkeypatch, "12")
    db = _limits_db(stored=12, unresolved=8)
    try:
        assert campaign_placement.project_limits_allow(db, "p", 0) is True
        _set_ceiling(monkeypatch, "8")
        assert campaign_placement.project_limits_allow(db, "p", 0) is False
        monkeypatch.delenv(ENV_PROJECT_CONCURRENCY_CEILING)
        operator_concurrency_ceiling.cache_clear()
        assert campaign_placement.project_limits_allow(db, "p", 0) is False
    finally:
        db.close()


def test_schema_check_is_the_hard_cap(tmp_path):
    initialize_state(tmp_path / "state")
    with sqlite3.connect(tmp_path / "state" / "tessera.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO execution_project_limits VALUES ('a',1,'USD',1,64,0,0,'t','t')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO execution_project_limits VALUES ('b',1,'USD',1,65,0,0,'t','t')")
        sql = db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='execution_project_limits'"
        ).fetchone()[0]
    assert sql == PROJECT_LIMITS_TABLE_SQL


def test_migration_rebuilds_old_check_and_keeps_rows(tmp_path):
    assert PROJECT_CONCURRENCY_SCHEMA_VERSION == 89 <= LATEST_SCHEMA_VERSION
    assert MIGRATIONS[88].__name__ == "migrate_project_concurrency_schema"
    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    world = World(tmp_path)
    assert world.limits(8).status_code == 201
    state = world.state
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        (row,) = db.execute("SELECT * FROM execution_project_limits").fetchall()
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TABLE execution_project_limits")
        db.execute(OLD_LIMITS_TABLE)
        db.execute("INSERT INTO execution_project_limits VALUES (?,?,?,?,?,?,?,?,?)", row)
        db.execute("UPDATE schema_meta SET schema_version=88")
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION  # a second run is a no-op
    query = "SELECT sql FROM sqlite_schema WHERE name='execution_project_limits'"
    with (
        sqlite3.connect(state / "tessera.sqlite3") as db,
        sqlite3.connect(fresh / "tessera.sqlite3") as ref,
    ):
        assert db.execute(query).fetchone() == ref.execute(query).fetchone()
        assert db.execute("SELECT * FROM execution_project_limits").fetchall() == [row]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
