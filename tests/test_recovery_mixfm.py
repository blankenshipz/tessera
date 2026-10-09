"""Disposable, provider-free v48 recovery drill; synthetic metadata, not science."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_credentials import headers, issue, owner_headers, token
from test_dataset_ingestion import _snapshot_payload
from test_program_evaluations import prepared
from test_program_interactions import ProgramJourney
from test_recovery_cli import run_cli
from test_recovery_records import _rows
from test_recovery_safety import _refresh_manifest

from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import (
    SCHEMA_VERSION,
    StoreError,
    initialize_state,
    migrate_state,
    utc_now,
)

DATASET_TABLES = (
    "dataset_snapshots",
    "dataset_snapshot_sources",
    "dataset_ingestion_proposals",
)
HISTORY_TABLES = DATASET_TABLES + (
    "research_studies",
    "research_sources",
    "research_variants",
    "research_findings",
    "decision_requests",
    "events",
    "idempotency",
)


def _v47(state: Path) -> None:
    """Remove only v48 additions from disposable, empty current-schema state."""
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        from migration_fixture_helpers import strip_empty_runtime_v55

        strip_empty_runtime_v55(connection)
        for table in reversed(DATASET_TABLES):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=47")
        connection.execute(
            "UPDATE operator_backup_health SET last_success_at=?,last_backup_id='pre-v48'",
            (utc_now(),),
        )


def _health(state: Path) -> dict:
    response = TestClient(create_app(state)).get("/operator/health", headers=owner_headers(state))
    assert response.status_code == 200
    return response.json()["backups"]


@pytest.mark.parametrize("prior_failure", [False, True])
def test_v48_migration_requires_new_backup_without_clearing_hold(
    tmp_path: Path, prior_failure: bool
) -> None:
    state = tmp_path / "source"
    initialize_state(state)
    _v47(state)
    if prior_failure:
        with sqlite3.connect(state / "tessera.sqlite3") as connection:
            connection.execute(
                "UPDATE operator_backup_health SET last_failure_at=?,"
                "last_failure_code='backup_failed'",
                (utc_now(),),
            )
    assert migrate_state(state) == SCHEMA_VERSION
    health = _health(state)
    assert health["freshness"] == "unknown"
    assert health["last_success_at"] is None and health["last_backup_id"] is None
    assert health["status"] == ("failed" if prior_failure else "unknown")
    assert health["last_failure_code"] == ("backup_failed" if prior_failure else None)
    assert _rows(state, DATASET_TABLES) == dict.fromkeys(DATASET_TABLES, [])
    backup = tmp_path / "backup"
    manifest = backup_state(state, backup)
    assert _health(state)["last_backup_id"] == manifest["backup_id"]
    assert _health(state)["freshness"] == "fresh"
    assert migrate_state(state) == SCHEMA_VERSION
    assert _health(state)["last_backup_id"] == manifest["backup_id"]

    held = tmp_path / "held"
    restore_state(backup, held)
    _v47(held)
    before = _rows(
        held, ("control_recovery", "principals", "credentials", "operator_backup_health")
    )
    assert migrate_state(held) == SCHEMA_VERSION
    assert _rows(held, tuple(before)) == before
    client = TestClient(create_app(held))
    assert client.get("/recovery", headers=owner_headers(held)).json()["recovery_hold"] is True
    assert (
        client.post(
            "/projects",
            headers=owner_headers(held) | {"Idempotency-Key": "no-promotion"},
            json={"name": "forbidden"},
        ).status_code
        == 403
    )


def _populated(tmp_path: Path) -> tuple[ProgramJourney, dict, dict]:
    j = ProgramJourney(tmp_path)
    _v47(j.state)
    assert migrate_state(j.state) == SCHEMA_VERSION
    j.client = TestClient(create_app(j.state))
    _finding, _protocol, evaluation = prepared(j)
    review = j.propose(j.scope("evaluation", evaluation=evaluation))
    j.resolve(review)
    source = {**j.source, "revision": 2}  # prepared() records the human assessment
    payload = _snapshot_payload(source)
    snapshot = j.post(
        f"/studies/{j.study['id']}/dataset-snapshots", payload, key="dataset-snapshot"
    )
    proposal = j.post(
        f"/studies/{j.study['id']}/dataset-ingestion-proposals",
        {
            "snapshot_id": snapshot["id"],
            "expected_snapshot_revision": snapshot["revision"],
            "intended_use": "Synthetic recovery drill; no dataset download or execution",
        },
    )
    j.post(
        f"/dataset-ingestion-proposals/{proposal['id']}/review",
        {
            "expected_revision": proposal["revision"],
            "decision": "approve",
            "rationale": "Metadata declaration only; no validated dataset bytes",
        },
    )
    return j, payload, proposal


def test_v48_mixfm_cli_backup_restore_drill(tmp_path: Path, monkeypatch) -> None:
    j, payload, proposal = _populated(tmp_path)
    old_headers = [owner_headers(j.state), headers(j.agent.removeprefix("Bearer "))]
    for kind, audience in (("human_session", "control"), ("worker", "worker")):
        value = token(f"old-{kind}")
        issue(
            j.client,
            j.state,
            value=value,
            kind=kind,
            audience=audience,
            project_id=j.project["id"],
            key=kind,
        )
        old_headers.append(headers(value))
    paths = (
        f"/studies/{j.study['id']}/export",
        f"/studies/{j.study['id']}/dataset-snapshots",
        f"/interaction/studies/{j.study['id']}/program-context",
    )
    expected = {}
    for path in paths:
        response = j.client.get(path, headers=owner_headers(j.state))
        assert response.status_code == 200, response.text
        expected[path] = response.json()
    assert expected[paths[0]]["completeness"] == "complete_in_scope_study_ledger"
    assert expected[paths[1]]["dataset_snapshots"][0]["content_validation"] == "not_performed"
    assert expected[paths[2]]["execution_granted"] is False
    evaluation = next(
        row for row in expected[paths[2]]["reviews"] if row["scope"]["action"] == "evaluation"
    )
    assert evaluation["status"] == "approved"
    assert len(evaluation["evaluation_report"]["gates"]) == 11
    before = _rows(j.state, HISTORY_TABLES)
    old_credentials = _rows(j.state, ("credentials",))["credentials"]
    assert all(before.values()), "Every compared history table must contain fixture records"
    backup = tmp_path / "backup"
    status, stdout, stderr = run_cli(
        monkeypatch, "backup", "--state-dir", str(j.state), "--output", str(backup)
    )
    assert status == 0 and stderr == ""
    manifest = json.loads((backup / "manifest.json").read_text())
    assert stdout.strip() == manifest["backup_id"]
    assert manifest["schema_version"] == SCHEMA_VERSION
    with sqlite3.connect(j.state / "tessera.sqlite3") as connection:
        watermark = connection.execute("SELECT MAX(cursor) FROM events").fetchone()[0]
    assert manifest["event_watermark"] == watermark
    # Deliberately advance the source: restoration must preserve the snapshot,
    # not imply the original writer was fenced or that later history was copied.
    j.post("/projects", {"name": "Not in the backup"})
    restored = tmp_path / "restored"
    status, stdout, stderr = run_cli(
        monkeypatch, "restore", "--backup", str(backup), "--state-dir", str(restored)
    )
    assert status == 0 and stderr == ""
    assert stdout.splitlines() == [
        str(restored / "tessera.sqlite3"),
        str(restored / "owner.header"),
    ]
    assert _rows(restored, HISTORY_TABLES) == before
    with sqlite3.connect(restored / "tessera.sqlite3") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for credential in old_credentials:
            assert (
                connection.execute(
                    "SELECT revoked_at FROM credentials WHERE id=?", (credential[0],)
                ).fetchone()[0]
                is not None
            )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM credentials WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 1
        )

    database = restored / "tessera.sqlite3"
    held_bytes = database.read_bytes()
    for _restart in range(2):
        client = TestClient(create_app(restored))
        fresh = owner_headers(restored)
        for path, content in expected.items():
            response = client.get(path, headers=fresh)
            assert response.status_code == 200
            assert response.json() == content
            for old in old_headers:
                assert client.get(path, headers=old).status_code == 401
        recovery = client.get("/recovery", headers=fresh).json()
        assert recovery["state"] == "held_restore" and recovery["recovery_hold"] is True
        assert recovery["promotion"]["supported"] is False
        assert recovery["original_branch_fenced"] is False
        assert recovery["unknown_post_backup_activity"] is True
        assert recovery["source_backup"]["event_watermark"] == watermark
        assert recovery["source_backup"]["digest"] == manifest["sha256"]
        # Existing idempotency key and fresh key must both fail, not replay success.
        for key in ("dataset-snapshot", "new-snapshot"):
            response = client.post(paths[1], json=payload, headers=fresh | {"Idempotency-Key": key})
            assert response.status_code == 403
        response = client.post(
            f"/dataset-ingestion-proposals/{proposal['id']}/review",
            headers=fresh | {"Idempotency-Key": "held-review"},
            json={"expected_revision": 2, "decision": "reject", "rationale": "Must remain held"},
        )
        assert response.status_code == 403
        j.client, j.owner = client, fresh["Authorization"]
        j.propose(j.scope("go_no_go"), status=403)
        assert database.read_bytes() == held_bytes
    assert migrate_state(restored) == SCHEMA_VERSION
    assert database.read_bytes() == held_bytes
    held_backup = tmp_path / "held-backup"
    backup_state(restored, held_backup)
    assert database.read_bytes() == held_bytes
    second = tmp_path / "second-held"
    restore_state(held_backup, second)
    second_client = TestClient(create_app(second))
    assert second_client.get("/projects", headers=owner_headers(restored)).status_code == 401
    assert (
        second_client.post(
            "/projects",
            headers=owner_headers(second) | {"Idempotency-Key": "still-held"},
            json={"name": "forbidden"},
        ).status_code
        == 403
    )
    assert _rows(second, HISTORY_TABLES) == before


@pytest.mark.parametrize("damage", ["bytes", "foreign_key", "schema", "watermark", "old_schema"])
def test_v48_restore_rejects_damaged_dataset_backup(tmp_path: Path, damage: str) -> None:
    j, _payload, _proposal = _populated(tmp_path)
    backup = tmp_path / "backup"
    backup_state(j.state, backup)
    snapshot = backup / "snapshot.sqlite3"
    if damage in {"bytes", "foreign_key", "schema"}:
        with sqlite3.connect(snapshot) as connection:
            if damage == "schema":
                connection.execute("DROP TABLE dataset_snapshot_sources")
            elif damage == "foreign_key":
                connection.execute("UPDATE dataset_snapshot_sources SET source_id='missing'")
            else:
                connection.execute("UPDATE dataset_snapshots SET name='tampered'")
        if damage != "bytes":
            _refresh_manifest(backup)  # Hash alone must not bypass structural integrity.
    else:
        path = backup / "manifest.json"
        manifest = json.loads(path.read_text())
        if damage == "watermark":
            manifest["event_watermark"] += 1
        else:
            manifest["schema_version"] = 47
        path.write_text(json.dumps(manifest))
    restored = tmp_path / "rejected"
    with pytest.raises(StoreError):
        restore_state(backup, restored)
    assert not (restored / "tessera.sqlite3").exists()
    assert not (restored / "owner.header").exists()
