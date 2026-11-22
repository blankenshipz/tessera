"""A database created before the settlement amendment converges (idempotent upgrade)."""

from __future__ import annotations

import sqlite3

from campaign_fixture import Env

from tessera_control.campaign_schema import add_v65_columns


def _columns(path, table):
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def test_a_database_without_the_settlement_columns_gains_them_once(tmp_path):
    env = Env(tmp_path)
    path = env.state / "tessera.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("ALTER TABLE campaign_stage_attempts DROP COLUMN settlement_basis")
        raw.execute("ALTER TABLE campaign_salad_attempts DROP COLUMN guardian_final_json")
        raw.execute("ALTER TABLE campaign_salad_attempts DROP COLUMN settlement_confirmed_at")
        assert "settlement_basis" not in _columns(path, "campaign_stage_attempts")
        add_v65_columns(raw)
        add_v65_columns(raw)  # idempotent
    finally:
        raw.close()
    assert "settlement_basis" in _columns(path, "campaign_stage_attempts")
    assert {"guardian_final_json", "settlement_confirmed_at"} <= _columns(
        path, "campaign_salad_attempts"
    )
    connection = sqlite3.connect(path)
    try:
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='campaign_stage_attempts'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert "guardian_bounded_hold" in sql  # the closed basis list survives the upgrade


def test_the_budget_entry_index_exists_and_is_restored_idempotently(tmp_path):
    env = Env(tmp_path)
    path = env.state / "tessera.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    try:

        def indexed():
            return {r[1] for r in raw.execute("PRAGMA index_list(experiment_budget_entries)")}

        assert "experiment_budget_entries_by_attempt" in indexed()
        raw.execute("DROP INDEX experiment_budget_entries_by_attempt")
        add_v65_columns(raw)
        add_v65_columns(raw)
        assert "experiment_budget_entries_by_attempt" in indexed()
        columns = [
            r[2] for r in raw.execute("PRAGMA index_info(experiment_budget_entries_by_attempt)")
        ]
        assert columns == ["experiment_id", "admitted_attempt_id", "category"]
    finally:
        raw.close()
