from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from test_artifact_manifests import _completed_outputs, _operation
from test_artifacts import _payload, _two_path_delivery
from test_credentials import headers, issue, owner_headers, token
from test_research import _source, _study, _variant

from tessera_control.api import create_app
from tessera_control.artifacts import ArtifactStore
from tessera_control.recovery import backup_state, restore_state
from tessera_control.research import ResearchStore
from tessera_control.store import Store, canonical_digest


def _rows(state: Path, tables: tuple[str, ...]) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def test_restore_preserves_artifact_execution_and_ledger_records(tmp_path: Path) -> None:
    state, worker, lease = _two_path_delivery(tmp_path)
    artifacts = ArtifactStore(state)
    complete = _payload(lease, b"complete artifact bytes")
    complete["relative_path"] = "first.txt"
    artifacts.upload_artifact(worker, lease["id"], "complete", canonical_digest(complete), complete)
    partial_bytes = b"partial upload bytes"
    begin = {
        "expected_epoch": lease["epoch"],
        "operation_id": _operation(lease),
        "relative_path": "second.txt",
        "sha256": hashlib.sha256(partial_bytes).hexdigest(),
        "size": len(partial_bytes),
    }
    _, partial = artifacts.begin_upload(
        worker, lease["id"], "partial", canonical_digest(begin), begin
    )
    chunk = {"offset": 0, "content_base64": base64.b64encode(partial_bytes[:7]).decode()}
    artifacts.append_chunk(
        worker, lease["id"], partial["id"], "partial-chunk", canonical_digest(chunk), chunk
    )
    owner = Store(state).authenticate(owner_headers(state)["Authorization"].removeprefix("Bearer "))
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        project_id = connection.execute(
            "SELECT project_id FROM execution_grants LIMIT 1"
        ).fetchone()[0]
        connection.execute("UPDATE execution_project_limits SET settled_cents=7")
        connection.commit()
    research = ResearchStore(state)
    study = _study(research, owner, project_id)
    source = _source(research, owner, study["id"])
    variant = _variant(research, owner, study["id"], "preserved variant")
    finding = {
        "statement": "Preserve this candidate.",
        "source_ids": [source["id"]],
        "variant_ids": [variant["id"]],
    }
    assert (
        research.create_finding(
            owner, study["id"], "recovery-finding", canonical_digest(finding), finding
        )[0]
        == 201
    )
    tables = (
        "artifacts",
        "artifact_uploads",
        "artifact_manifests",
        "source_snapshots",
        "snapshot_files",
        "snapshot_releases",
        "events",
        "idempotency",
        "execution_grants",
        "execution_leases",
        "execution_task_epochs",
        "execution_project_limits",
        "research_studies",
        "research_variants",
        "research_sources",
        "research_findings",
        "research_finding_sources",
        "research_finding_variants",
        "research_corrections",
        "tasks",
        "task_revisions",
        "task_approvals",
    )
    before = _rows(state, tables)
    backup = tmp_path / "backup"
    backup_state(state, backup)
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    assert _rows(restored, tables) == before


def test_restore_revokes_all_old_credentials_and_holds_fresh_owner(tmp_path: Path) -> None:
    state, worker, _lease, first, _second = _completed_outputs(tmp_path)
    client = TestClient(create_app(state))
    project_id = str(first["project_id"])
    human_value, agent_value = token("old-human"), token("old-agent")
    issue(
        client,
        state,
        value=human_value,
        kind="human_session",
        audience="control",
        project_id=project_id,
        key="old-human",
    )
    issue(
        client,
        state,
        value=agent_value,
        kind="agent",
        audience="control",
        project_id=project_id,
        key="old-agent",
    )
    old_owner = owner_headers(state)
    backup = tmp_path / "backup"
    backup_state(state, backup)
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    restored_client = TestClient(create_app(restored))
    old_headers = (old_owner, headers(human_value), headers(agent_value), headers("worker-token"))
    assert all(
        restored_client.get("/projects", headers=value).status_code == 401 for value in old_headers
    )
    assert worker["kind"] == "worker"
    assert restored_client.get("/projects", headers=owner_headers(restored)).status_code == 200
    assert (
        restored_client.post(
            "/projects",
            headers=owner_headers(restored) | {"Idempotency-Key": "held-empty"},
            json={"name": "held"},
        ).status_code
        == 403
    )


def test_recovery_count_discloses_truncation_for_scoped_actor(tmp_path: Path) -> None:
    state, _worker, _lease, first, _second = _completed_outputs(tmp_path)
    client = TestClient(create_app(state))
    scoped_value = token("scoped-recovery")
    issue(
        client,
        state,
        value=scoped_value,
        kind="human_session",
        audience="control",
        project_id=str(first["project_id"]),
        key="scoped-recovery",
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        grant = connection.execute("SELECT * FROM execution_grants LIMIT 1").fetchone()
        task = connection.execute("SELECT * FROM tasks WHERE id=?", (grant[2],)).fetchone()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(execution_grants)")]
        values = [grant[index] for index in range(len(columns))]
        id_index = columns.index("id")
        task_columns = [row[1] for row in connection.execute("PRAGMA table_info(tasks)")]
        task_values = [task[index] for index in range(len(task_columns))]
        task_id_index = task_columns.index("id")
        grant_task_index = columns.index("task_id")
        statement = (
            f"INSERT INTO execution_grants ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})"
        )
        task_statement = (
            f"INSERT INTO tasks ({','.join(task_columns)}) "
            f"VALUES ({','.join('?' for _ in task_columns)})"
        )
        for number in range(101):
            task_id = f"recovery_task_{number}"
            task_values[task_id_index] = task_id
            connection.execute(task_statement, task_values)
            values[id_index] = f"recovery_grant_{number}"
            values[grant_task_index] = task_id
            connection.execute(statement, values)
        connection.commit()
    response = client.get("/recovery", headers=headers(scoped_value))
    assert response.status_code == 200
    grants = response.json()["unresolved_grants"]
    assert grants["count"] == 102 and grants["listed_count"] == 100 and grants["truncated"] is True
