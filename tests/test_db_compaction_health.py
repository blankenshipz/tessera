"""Compaction recommendation, its health projection, and serve's maintenance lock."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_control import cli, db_compaction_health, maintenance_lock
from tessera_control.api import create_app
from tessera_control.db_compaction_health import recommendation
from tessera_control.maintenance_lock import MaintenanceBusy
from tessera_control.store import initialize_state


def _bloated(path: Path, *, keep: int = 0) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE blob (id INTEGER PRIMARY KEY, data BLOB)")
    connection.executemany(
        "INSERT INTO blob (data) VALUES (?)", [(b"x" * 4000,) for _ in range(1000)]
    )
    connection.execute("DELETE FROM blob WHERE id > ?", (keep,))
    connection.commit()
    return connection


def test_clean_database_has_no_freelist(tmp_path: Path) -> None:
    connection = _bloated(tmp_path / "a.db", keep=1000)
    result = recommendation(connection)
    assert result["freelist_pages"] == 0
    assert result["reclaimable_bytes"] == 0
    assert result["recommended"] is False
    assert result["command"] == "tessera db compact"
    assert result["database_bytes"] == result["page_size"] * result["page_count"]


def test_bloated_database_reports_freelist_but_small_stays_quiet(tmp_path: Path) -> None:
    connection = _bloated(tmp_path / "a.db")
    result = recommendation(connection)
    assert result["freelist_pages"] > 500
    assert result["reclaimable_fraction"] > 0.9
    assert result["reclaimable_bytes"] == result["freelist_pages"] * result["page_size"]
    assert result["recommended"] is False  # ~4 MB: under the byte threshold


def test_both_thresholds_are_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _bloated(tmp_path / "a.db")
    monkeypatch.setattr(db_compaction_health, "FREELIST_MIN_BYTES", 1024)
    assert recommendation(connection)["recommended"] is True
    monkeypatch.setattr(db_compaction_health, "FREELIST_MIN_FRACTION", 1.1)
    assert recommendation(connection)["recommended"] is False
    monkeypatch.setattr(db_compaction_health, "FREELIST_MIN_FRACTION", 0.25)
    monkeypatch.setattr(db_compaction_health, "FREELIST_MIN_BYTES", 10**9)
    assert recommendation(connection)["recommended"] is False
    assert db_compaction_health.FREELIST_MIN_FRACTION == 0.25


def test_default_thresholds_documented_values() -> None:
    assert db_compaction_health.FREELIST_MIN_BYTES == 64 * 1024 * 1024
    assert db_compaction_health.FREELIST_MIN_FRACTION == 0.25


def test_never_raises(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "a.db")
    connection.close()
    assert recommendation(connection) == {"recommended": False, "error": "unavailable"}
    assert recommendation(None) == {"recommended": False, "error": "unavailable"}  # type: ignore[arg-type]
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    assert recommendation(sqlite3.connect(garbage))["recommended"] is False


def _health(tmp_path: Path) -> dict:
    tmp_path.mkdir()
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    name, value = state.joinpath("owner.header").read_text().strip().split(": ", 1)
    response = client.get("/operator/health", headers={name: value})
    assert response.status_code == 200
    return response.json()


def test_health_always_has_database_key_and_alert_only_when_recommended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _health(tmp_path / "one")
    assert body["database"]["recommended"] is False
    assert "freelist_pages" in body["database"]
    assert "db_compaction_recommended" not in {a["code"] for a in body["alerts"]}

    monkeypatch.setattr(db_compaction_health, "FREELIST_MIN_BYTES", 0)
    monkeypatch.setattr(db_compaction_health, "FREELIST_MIN_FRACTION", 0.0)
    body = _health(tmp_path / "two")
    assert body["database"]["recommended"] is True
    assert {"code": "db_compaction_recommended", "severity": "warning"} in body["alerts"]


def _serve(monkeypatch: pytest.MonkeyPatch, state: Path, run) -> int:
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(
        sys, "argv", ["tessera", "serve", "--port", "8765", "--state-dir", str(state)]
    )
    return cli.main()


def test_serve_holds_shared_lock_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    seen: list[str] = []

    def run(app, **_):
        with pytest.raises(MaintenanceBusy):
            with maintenance_lock.exclusive(state):
                pass
        seen.append("busy")

    assert _serve(monkeypatch, state, run) == 0
    assert seen == ["busy"]
    with maintenance_lock.exclusive(state):  # released after serve returns
        pass


def test_serve_refuses_while_exclusive_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    with maintenance_lock.exclusive(state):
        code = _serve(monkeypatch, state, lambda *a, **k: pytest.fail("served during compaction"))
    assert code == 1
    assert "serve failed:" in capsys.readouterr().err
