"""Schema v64 -> v65: provider trust policies, stage I/O index, provider ids (ADR 0040)."""

from __future__ import annotations

import json
import re
import sqlite3

import pytest
from campaign_fixture import Env

from tessera_control.campaign_schema import V65_COLUMNS
from tessera_control.migrations import LATEST_SCHEMA_VERSION, migrate_database
from tessera_control.store import _connect, validate_database


def _downgrade_to_v64(path) -> None:
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DROP TABLE campaign_stage_io")
        raw.execute("DROP TABLE provider_trust_activations")
        raw.execute("DROP TABLE provider_trust_policies")
        for table, column in V65_COLUMNS:
            raw.execute(f"ALTER TABLE {table} DROP COLUMN {column.split()[0]}")
        raw.execute("UPDATE schema_meta SET schema_version=64")
        raw.execute("UPDATE operator_backup_health SET last_success_at='2026-01-01T00:00:00Z'")
    finally:
        raw.close()


def _shape(path) -> dict:
    connection = sqlite3.connect(path)
    try:
        return {
            name: sql
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_schema WHERE name LIKE 'campaign_stage%' "
                "OR name LIKE 'provider_trust%' OR name='campaign_approvals'"
            )
        }
    finally:
        connection.close()


def _checks(sql: str) -> set[str]:
    """Every ``CHECK(...)`` clause of a CREATE statement, whitespace-free."""
    found = set()
    for match in re.finditer(r"CHECK\s*\(", sql or ""):
        depth, index = 1, match.end()
        while depth and index < len(sql):
            depth += {"(": 1, ")": -1}.get(sql[index], 0)
            index += 1
        found.add("".join(sql[match.start() : index].split()))
    return found


def _convergence(path) -> dict:
    """Per object: its column set (name, type, not null, default) and its CHECK clauses."""
    connection = sqlite3.connect(path)
    try:
        shape = {}
        for name, kind, sql in connection.execute(
            "SELECT name, type, sql FROM sqlite_schema WHERE name LIKE 'campaign_stage%' "
            "OR name LIKE 'provider_trust%' OR name='campaign_approvals' "
            "OR name='campaign_salad_attempts'"
        ):
            columns = (
                set()
                if kind != "table"
                else {
                    (c[1], c[2], c[3], c[4])
                    for c in connection.execute(f"PRAGMA table_info({name})")
                }
            )
            shape[name] = (columns, _checks(sql) if kind == "table" else " ".join(sql.split()))
        return shape
    finally:
        connection.close()


def test_v64_migrates_with_backfill_and_converges(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    # Rows for stage s2 are rewritten as a Salad v1 stage in the stored spec.
    spec = json.loads(env.query("SELECT spec_json FROM campaigns")[0]["spec_json"])
    spec["stages"][1]["backend"] = "salad-gpu-v1"
    env.execute("UPDATE campaigns SET spec_json=?", json.dumps(spec))
    path = env.state / "tessera.sqlite3"
    fresh_shape = _shape(path)
    fresh_convergence = _convergence(path)
    _downgrade_to_v64(path)
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION == 71
    validate_database(path)
    connection = _connect(path)
    try:
        rows = dict(
            connection.execute(
                "SELECT stage_key, provider_id FROM campaign_stages WHERE campaign_id=?",
                (campaign["id"],),
            ).fetchall()
        )
        assert rows == {"s1": "local-docker", "s2": "salad"}
        assert (
            connection.execute("SELECT last_success_at FROM operator_backup_health").fetchone()[0]
            is None
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute(
            "INSERT INTO provider_trust_policies VALUES ('d', '{}', 'builtin', 'now')"
        )
        for sql in (
            "UPDATE provider_trust_policies SET source='file'",
            "DELETE FROM provider_trust_policies",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql)
    finally:
        connection.close()
    assert migrate_database(path, _connect) == 71  # a second run is a no-op
    assert _shape(path).keys() == fresh_shape.keys()
    # The migrated database has the same columns and CHECK clauses as a fresh one.
    migrated = _convergence(path)
    for name, (columns, checks) in fresh_convergence.items():
        assert migrated[name][0] == columns, (name, columns ^ migrated[name][0])
        assert migrated[name][1] == checks, (name, checks ^ migrated[name][1])
    assert migrated.keys() == fresh_convergence.keys()


def test_stage_io_is_append_only_and_checks_direction(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    project = env.project["id"]
    org = env.query("SELECT org_id FROM projects WHERE id=?", project)[0][0]
    connection = _connect(env.state / "tessera.sqlite3")
    try:

        def insert(direction: str) -> None:
            connection.execute(
                "INSERT INTO campaign_stage_io (id, org_id, project_id, campaign_id, stage_key,"
                " direction, name, kind, sha256, size_bytes, data_class, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"io-{direction}", org, project, campaign["id"], "s1", direction,
                    "n", "file", "a" * 64, 1, "private_derived", "now",
                ),
            )  # fmt: skip

        insert("output")
        with pytest.raises(sqlite3.IntegrityError):
            insert("sideways")
        for sql in ("UPDATE campaign_stage_io SET name='x'", "DELETE FROM campaign_stage_io"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(sql)
    finally:
        connection.close()


def test_a_snapshot_purge_is_never_blocked_by_the_io_index(tmp_path):
    """The index only names the snapshot (sha256 is the evidence): no foreign key to it."""
    env = Env(tmp_path)
    campaign = env.submit()
    project = env.project["id"]
    org = env.query("SELECT org_id FROM projects WHERE id=?", project)[0][0]
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        targets = {r[2] for r in connection.execute("PRAGMA foreign_key_list(campaign_stage_io)")}
        assert "source_snapshots" not in targets
        # A reference to a snapshot that no longer exists is accepted with enforcement on.
        connection.execute(
            "INSERT INTO campaign_stage_io (id, org_id, project_id, campaign_id, stage_key,"
            " direction, name, kind, sha256, size_bytes, snapshot_id, data_class, created_at)"
            " VALUES ('io-gone',?,?,?,'s1','input','m','file',?,1,'snap-purged','public','now')",
            (org, project, campaign["id"], "b" * 64),
        )
    finally:
        connection.close()
