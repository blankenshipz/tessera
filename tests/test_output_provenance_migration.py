"""Explicit schema-52 upgrades of genuine pre-output-binding schema-51 shapes."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import assert_empty_runtime_v55, strip_runtime_v55
from test_agent_provenance_outputs import joined
from test_agent_provenance_outputs import output as output

from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import SCHEMA_VERSION, initialize_state, migrate_state, utc_now


def old51(state):
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        strip_runtime_v55(db)
        db.execute("DROP TABLE agent_output_provider_bindings")
        db.execute("UPDATE schema_meta SET schema_version=51")
        db.execute(
            "UPDATE operator_backup_health SET last_success_at=?,last_backup_id='v51',"
            "last_failure_at=?,last_failure_code='backup_failed'",
            (utc_now(), utc_now()),
        )


def rows(state, tables):
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        return {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}


@pytest.mark.parametrize("held", [False, True])
def test_explicit51_to52_preserves_forensics_and_only_invalidates_active_success(tmp_path, held):
    state = tmp_path / "source"
    initialize_state(state)
    assert SCHEMA_VERSION == 56
    assert TestClient(create_app(state)).get("/health/ready").status_code == 200
    assert rows(state, ["agent_output_provider_bindings"]) == {"agent_output_provider_bindings": []}
    if held:
        backup = tmp_path / "backup"
        backup_state(state, backup)
        state = tmp_path / "held"
        restore_state(backup, state)
    old51(state)
    history = [
        "control_recovery",
        "principals",
        "credentials",
        "events",
        "idempotency",
        "agent_provenance",
    ]
    before = rows(state, history)
    before_health = rows(state, ["operator_backup_health"])
    assert TestClient(create_app(state)).get("/health/ready").status_code == 503
    # App construction/readiness must not silently create the new table.
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='agent_output_provider_bindings'"
            ).fetchone()
            is None
        )
    assert migrate_state(state) == SCHEMA_VERSION
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert_empty_runtime_v55(db)
    assert rows(state, history) == before
    assert rows(state, ["agent_output_provider_bindings"]) == {"agent_output_provider_bindings": []}
    if held:
        assert rows(state, ["operator_backup_health"]) == before_health
        owner = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
        client = TestClient(create_app(state))
        assert (
            client.get("/recovery", headers={"Authorization": "Bearer " + owner}).json()[
                "recovery_hold"
            ]
            is True
        )
        assert (
            client.post(
                "/projects",
                json={"name": "forbidden"},
                headers={"Authorization": "Bearer " + owner, "Idempotency-Key": "held-write"},
            ).status_code
            == 403
        )
    else:
        assert TestClient(create_app(state)).get("/health/ready").status_code == 200
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.row_factory = sqlite3.Row
            health = db.execute("SELECT * FROM operator_backup_health").fetchone()
            assert health["last_success_at"] is None and health["last_backup_id"] is None
            assert health["last_failure_at"] and health["last_failure_code"] == "backup_failed"
    after = rows(state, history + ["operator_backup_health"])
    assert migrate_state(state) == SCHEMA_VERSION
    assert rows(state, history + ["operator_backup_health"]) == after


def test_actual_old51_manifest_history_exports_after_explicit_upgrade_without_uid_invention(output):
    state = output["state"]
    old51(state)
    history = [
        "artifact_manifests",
        "external_operations",
        "research_experiment_admitted_attempts",
        "events",
        "agent_provenance",
        "idempotency",
    ]
    before = rows(state, history)
    assert TestClient(create_app(state)).get("/health/ready").status_code == 503
    assert migrate_state(state) == SCHEMA_VERSION
    assert rows(state, history) == before
    result = joined(output)
    assert result["output_manifest_id"] == output["manifest"]["id"]
    assert result["operation_id"] == output["manifest"]["operation_id"]
    assert result["attempt_id"] == output["attempt"]["id"]
    assert result["late"] and result["status"] == "quarantined"
    assert result["control_outcome"] == "cancelled"
    assert (
        result["provider_job_uid"] is None
        and result["provider_identity_status"] == "job_uid_unknown"
    )
    assert {a["sha256"] for a in result["artifacts"]} == {
        output["first"]["sha256"],
        output["second"]["sha256"],
    }
    owner = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    exported = TestClient(create_app(state)).get(
        f"/experiments/{output['experiment']['id']}/export",
        headers={"Authorization": "Bearer " + owner},
    )
    assert exported.status_code == 200
    assert any(
        e.get("output", {}).get("output_manifest_id") == output["manifest"]["id"]
        for e in exported.json()["agent_notebook"]["control_events"]
    )
    assert rows(state, ["agent_output_provider_bindings"]) == {"agent_output_provider_bindings": []}
