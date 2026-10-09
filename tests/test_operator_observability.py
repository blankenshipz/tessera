"""Default, isolated checks for secret-free operator observability."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.observability import _held_sink_path
from tessera_control.recovery import DATABASE_NAME, backup_state, restore_state
from tessera_control.store import StoreError, initialize_state
from tessera_control.workers import WorkerRegistryStore


def _owner_headers(state: Path) -> dict[str, str]:
    name, value = state.joinpath("owner.header").read_text().strip().split(": ", 1)
    return {name: value}


def _state(tmp_path: Path) -> tuple[Path, TestClient]:
    state = tmp_path / "state"
    initialize_state(state)
    return state, TestClient(create_app(state))


def _project(client: TestClient, state: Path) -> dict[str, object]:
    response = client.post(
        "/projects",
        headers=_owner_headers(state) | {"Idempotency-Key": "project"},
        json={"name": "Operator projection"},
    )
    assert response.status_code == 201
    return response.json()


def test_credential_audit_and_denial_are_secret_free_and_event_lineage_is_visible(
    tmp_path: Path,
) -> None:
    state, client = _state(tmp_path)
    project = _project(client, state)
    token = "session_" + "x" * 48
    issue_response = client.post(
        "/credentials",
        headers=_owner_headers(state) | {"Idempotency-Key": "credential"},
        json={
            "token": token,
            "kind": "human_session",
            "audience": "control",
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert issue_response.status_code == 201
    revoked = client.post(
        f"/credentials/{issue_response.json()['credential_id']}/revoke",
        headers=_owner_headers(state) | {"Idempotency-Key": "revoke-session"},
    )
    assert revoked.status_code == 200
    agent_token = "agent_" + "z" * 48
    agent = client.post(
        "/credentials",
        headers=_owner_headers(state) | {"Idempotency-Key": "agent"},
        json={
            "token": agent_token,
            "kind": "agent",
            "audience": "control",
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert agent.status_code == 201
    denied = client.post(
        "/credentials",
        headers={"Authorization": f"Bearer {agent_token}", "Idempotency-Key": "denied"},
        json={
            "token": "forbidden_" + "y" * 48,
            "kind": "agent",
            "audience": "control",
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert denied.status_code == 403
    audit = client.get("/operator/audit", headers=_owner_headers(state)).json()["events"]
    assert [(event["event_type"], event["decision"]) for event in audit] == [
        ("credential.created", "allowed"),
        ("credential.revoked", "allowed"),
        ("credential.created", "allowed"),
        ("privileged.command_denied", "denied"),
    ]
    serialized = str(audit)
    assert token not in serialized
    assert agent_token not in serialized
    assert "token_hash" not in serialized
    assert all(event["correlation_id"] == event["causation_id"] for event in audit)
    labbook = client.get(
        f"/projects/{project['id']}/labbook", headers=_owner_headers(state)
    ).json()["events"]
    assert all(event["correlation_id"] == event["causation_id"] for event in labbook)
    assert (
        client.get(
            "/operator/health", headers={"Authorization": f"Bearer {agent_token}"}
        ).status_code
        == 403
    )


def test_health_projects_stale_workers_backlog_backup_failures_and_resource_budgets(
    tmp_path: Path,
) -> None:
    state, client = _state(tmp_path)
    project = _project(client, state)
    owner = _owner_headers(state)
    worker_token = "worker_" + "x" * 48
    issue_response = client.post(
        "/credentials",
        headers=owner | {"Idempotency-Key": "worker"},
        json={
            "token": worker_token,
            "kind": "worker",
            "audience": "worker",
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert issue_response.status_code == 201
    issued = issue_response.json()
    owner_actor = client.app.state.store.authenticate(
        owner["Authorization"].removeprefix("Bearer ")
    )
    WorkerRegistryStore(state).register_worker(
        owner_actor,
        str(project["id"]),
        "register",
        "register-digest",
        {
            "worker_principal_id": issued["principal_id"],
            "approved_build_identifier": "test-worker",
            "approved_build_digest": "a" * 64,
            "trust_class": "test",
            "region": "local",
        },
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE worker_registry SET last_seen_at='2020-01-01T00:00:00Z'")
        connection.execute(
            """INSERT INTO execution_project_limits
            (project_id,revision,currency,monetary_limit_cents,max_concurrency,held_cents,
             settled_cents,created_at,updated_at)
            VALUES (?,1,'USD',10,1,10,0,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')""",
            (project["id"],),
        )
        connection.execute(
            """INSERT INTO execution_recovery_reconciliations
            (id,org_id,project_id,task_id,grant_id,lease_id,epoch,operation_id,
             original_worker_principal_id,identity_loss_reason,state,revision,
             cancellation_requested,created_at,updated_at,closed_at,closed_by_principal_id)
            VALUES ('recovery_test','org_local',?,'task_missing','grant_missing','lease_missing',1,
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',?,'worker_retired','identity_lost',1,0,
            '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',NULL,NULL)""",
            (project["id"], issued["principal_id"]),
        )
        connection.commit()
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(StoreError):
        backup_state(state, existing)
    health = client.get("/operator/health", headers=owner).json()
    assert {alert["code"] for alert in health["alerts"]} >= {
        "worker_heartbeat_stale",
        "reconciliation_backlog",
        "backup_failed",
        "resource_budget_exhausted",
    }
    assert health["workers"]["stale_count"] == 1
    assert health["reconciliation"] == {
        "open_count": 1,
        "open_by_state": {"identity_lost": 1},
        "oldest_opened_at": "2026-01-01T00:00:00Z",
    }
    assert health["backups"]["status"] == "failed"
    assert health["resource_budgets"] == [
        {
            "project_id": project["id"],
            "currency": "USD",
            "monetary_limit_cents": 10,
            "held_cents": 10,
            "settled_cents": 0,
            "unattempted_reserved_cents": 0,
            "attempted_reserved_cents": 0,
            "unattributed_held_cents": 10,
            "remaining_cents": 0,
            "authority_admission": "blocked",
            "max_concurrency": 1,
            "active_or_cancelling_grants": 0,
            "status": "exhausted",
        }
    ]
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DELETE FROM execution_recovery_reconciliations WHERE id='recovery_test'"
        )
        connection.commit()
    backup = tmp_path / "backup"
    manifest = backup_state(state, backup)
    assert manifest["schema_version"] == LATEST_SCHEMA_VERSION
    healthy = client.get("/operator/health", headers=owner).json()["backups"]
    assert healthy["status"] == "healthy"
    assert healthy["freshness"] == "fresh"
    assert healthy["age_seconds"] is not None

    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE operator_backup_health SET last_success_at='2020-01-01T00:00:00Z'"
        )
        connection.commit()
    stale = client.get("/operator/health", headers=owner).json()
    assert stale["backups"]["status"] == stale["backups"]["freshness"] == "stale"
    assert stale["backups"]["age_seconds"] > stale["backups"]["stale_after_seconds"]
    assert {alert["code"] for alert in stale["alerts"]} >= {"backup_stale"}


def test_held_denials_and_backup_health_use_external_sink_without_changing_history(
    tmp_path: Path,
) -> None:
    state, client = _state(tmp_path)
    project = _project(client, state)
    issued = client.post(
        "/credentials",
        headers=_owner_headers(state) | {"Idempotency-Key": "credential-to-revoke"},
        json={
            "token": "session_" + "x" * 48,
            "kind": "human_session",
            "audience": "control",
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert issued.status_code == 201
    backup = tmp_path / "backup"
    backup_state(state, backup)
    held = tmp_path / "held"
    restore_state(backup, held)
    held_client = TestClient(create_app(held))
    owner = _owner_headers(held)
    before = (held / DATABASE_NAME).read_bytes()

    rejected = (
        (
            "/projects",
            {"Idempotency-Key": "held-project"},
            {"name": "must remain held"},
        ),
        (
            "/credentials",
            {"Idempotency-Key": "held-credential"},
            {
                "token": "forbidden_" + "y" * 48,
                "kind": "human_session",
                "audience": "control",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        ),
        (
            f"/credentials/{issued.json()['credential_id']}/revoke",
            {"Idempotency-Key": "held-privileged"},
            None,
        ),
    )
    for path, headers, payload in rejected:
        response = held_client.post(path, headers=owner | headers, json=payload)
        assert response.status_code == 403
        assert (held / DATABASE_NAME).read_bytes() == before

    audit = held_client.get("/operator/audit", headers=owner).json()["events"]
    denials = [event for event in audit if event["event_type"] == "privileged.command_denied"]
    assert [(event["payload"]["method"], event["payload"]["path"]) for event in denials] == [
        ("POST", path) for path, _headers, _payload in rejected
    ]
    assert all(event["storage"] == "external_held_operational_sink" for event in denials)
    assert all(event["correlation_id"] == event["causation_id"] for event in denials)

    held_backup = tmp_path / "held-backup"
    backup_state(held, held_backup)
    assert (held / DATABASE_NAME).read_bytes() == before
    healthy = held_client.get("/operator/health", headers=owner).json()["backups"]
    assert healthy["status"] == "healthy"
    assert healthy["storage"] == "external_held_operational_sink"

    existing_output = tmp_path / "existing-output"
    existing_output.mkdir()
    with pytest.raises(StoreError):
        backup_state(held, existing_output)
    assert (held / DATABASE_NAME).read_bytes() == before
    failed = held_client.get("/operator/health", headers=owner).json()["backups"]
    assert failed["status"] == "failed"
    assert failed["storage"] == "external_held_operational_sink"


@pytest.mark.parametrize("unsafe_kind", ("state_dir", "database"))
def test_unsafe_backup_source_does_not_follow_on_to_health_writes(
    tmp_path: Path, unsafe_kind: str
) -> None:
    state, _client = _state(tmp_path)
    database = state / DATABASE_NAME
    before = database.read_bytes()
    if unsafe_kind == "state_dir":
        source = tmp_path / "state-link"
        source.symlink_to(state, target_is_directory=True)
    else:
        target = tmp_path / "database-target.sqlite3"
        database.rename(target)
        database.symlink_to(target)
        source = state
        before = target.read_bytes()

    with pytest.raises((StoreError, OSError), match="unsafe|regular|symbolic"):
        backup_state(source, tmp_path / f"{unsafe_kind}-backup")

    checked = state / DATABASE_NAME if unsafe_kind == "state_dir" else target
    assert checked.read_bytes() == before
    assert not _held_sink_path(source).exists()


def test_opt_in_lab_hook_refuses_without_run_and_runbook_names_all_procedures() -> None:
    root = Path(__file__).parents[1]
    hook = root / "scripts/check_single_active_lab_recovery.py"
    result = subprocess.run(
        [sys.executable, str(hook)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "without --run" in result.stderr
    runbook = (root / "docs/single-active-lab-operations.md").read_text()
    for phrase in (
        "Process restart",
        "PVC loss",
        "Backup verification",
        "Writer fencing",
        "RPO and RTO",
    ):
        assert phrase in runbook
