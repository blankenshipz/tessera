"""Confirmed-stop workspace handoff API tests; no provider work is dispatched."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient
from test_input_delivery import _delivery

from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.handoffs import WorkspaceHandoffStore
from tessera_control.migrations import MIGRATIONS
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import (
    Conflict,
    Store,
    _connect,
    canonical_digest,
    initialize_state,
    migrate_state,
)
from tessera_control.workspace import capture_workspace, inventory_digest


def test_confirmed_stop_fences_worker_then_records_human_diff_and_handback(tmp_path):
    state, source = tmp_path / "state", tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("baseline")
    (source / "delete.txt").write_text("delete")
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    client, sequence = TestClient(create_app(state)), count()

    def call(method, path, body, expected, headers=None, key=None):
        response = client.request(
            method,
            path,
            json=body,
            headers=(headers or owner) | {"Idempotency-Key": key or f"handoff-{next(sequence)}"},
        )
        assert response.status_code == expected, response.text
        return response.json()

    project = call("POST", "/projects", {"name": "Handoff fixture"}, 201)
    snapshots = SnapshotStore(state)
    actor = snapshots.authenticate(owner["Authorization"].removeprefix("Bearer "))

    def adopt(key):
        capture = capture_workspace(source)
        _, snapshot = snapshots.adopt_snapshot(
            actor, project["id"], key, inventory_digest(capture.inventory), capture
        )
        return snapshot

    baseline = adopt("baseline")
    worker_token = "handoff-worker-" + "x" * 48
    worker = {"Authorization": f"Bearer {worker_token}"}
    credential = call(
        "POST",
        "/credentials",
        {
            "kind": "worker",
            "audience": "worker",
            "token": worker_token,
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        201,
    )
    other_worker = {"Authorization": "Bearer other-handoff-worker-" + "y" * 48}
    call(
        "POST",
        "/credentials",
        {
            "kind": "worker",
            "audience": "worker",
            "token": other_worker["Authorization"].removeprefix("Bearer "),
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        201,
    )
    call(
        "POST",
        f"/projects/{project['id']}/workers",
        {
            "worker_principal_id": credential["principal_id"],
            "approved_build_identifier": "handoff-worker",
            "approved_build_digest": "a" * 64,
            "trust_class": "test",
            "region": "local",
        },
        201,
    )
    task = call(
        "POST",
        f"/projects/{project['id']}/tasks",
        {
            "title": "Human change fixture",
            "purpose": "No provider work",
            "budget_cents": 10,
            "max_duration_seconds": 120,
        },
        201,
    )
    plan = call(
        "PUT",
        f"/tasks/{task['id']}/plan",
        {
            "expected_revision": task["revision"],
            "title": task["title"],
            "purpose": task["purpose"],
            "budget_cents": 10,
            "max_duration_seconds": 120,
            "execution_spec": {
                "argv": ["python", "-c", "print('fixture')"],
                "input_snapshot_digest": baseline["digest"],
                "pinned_skill_digests": [],
                "output_paths": [],
                "profile": "local-cpu",
                "network_enabled": False,
                "max_output_bytes": 1024,
            },
        },
        200,
    )
    approved = call(
        "POST",
        f"/tasks/{task['id']}/approve",
        {"expected_revision": plan["revision"], "expected_plan_digest": plan["plan_digest"]},
        200,
    )
    call(
        "PUT",
        f"/projects/{project['id']}/execution-limits",
        {
            "expected_revision": 0,
            "currency": "USD",
            "monetary_limit_cents": 10,
            "max_concurrency": 1,
        },
        201,
    )
    call(
        "POST",
        f"/snapshots/{baseline['id']}/releases",
        {
            "expected_digest": baseline["digest"],
            "worker_principal_id": credential["principal_id"],
            "classification": "public",
            "permits_worker_transfer": True,
            "rationale": "Synthetic only",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        },
        201,
    )
    grant_body = {
        "task_id": task["id"],
        "expected_task_revision": approved["revision"],
        "expected_plan_digest": approved["plan_digest"],
        "worker_principal_id": credential["principal_id"],
        "snapshot_id": baseline["id"],
        "pinned_image": "python@sha256:" + "a" * 64,
        "currency": "USD",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "max_exposure_cents": 10,
        "acknowledges_unmeasured_infrastructure": True,
    }
    grant = call("POST", f"/projects/{project['id']}/execution-grants", grant_body, 201)
    lease = call("POST", "/worker/leases", {"grant_id": grant["id"]}, 201, worker)
    handoff = call(
        "POST",
        f"/projects/{project['id']}/workspace-handoffs",
        {
            "task_id": task["id"],
            "grant_id": grant["id"],
            "expected_task_revision": approved["revision"],
            "expected_grant_revision": grant["revision"],
            "baseline_snapshot_id": baseline["id"],
            "expected_baseline_digest": baseline["digest"],
            "workspace_label": "selected fixture root",
        },
        201,
        key="handoff-once",
    )
    assert handoff["status"] == "stop_requested" and handoff["human_owner"] is False
    assert (
        call(
            "POST",
            f"/projects/{project['id']}/workspace-handoffs",
            {
                "task_id": task["id"],
                "grant_id": grant["id"],
                "expected_task_revision": approved["revision"],
                "expected_grant_revision": grant["revision"],
                "baseline_snapshot_id": baseline["id"],
                "expected_baseline_digest": baseline["digest"],
                "workspace_label": "selected fixture root",
            },
            201,
            key="handoff-once",
        )
        == handoff
    )
    call(
        "POST",
        "/worker/leases/renew",
        {"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
        409,
        worker,
    )
    observation = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "observed_state": "unknown",
        "termination_confirmed": False,
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "synthetic-provider",
    }
    call("POST", "/worker/observations", observation, 404, other_worker)
    call(
        "POST",
        "/worker/observations",
        observation | {"expected_epoch": lease["epoch"] + 1},
        409,
        worker,
    )
    call("POST", "/worker/observations", observation, 201, worker)
    call(
        "POST",
        f"/workspace-handoffs/{handoff['id']}/confirm-stop",
        {"expected_revision": handoff["revision"]},
        409,
    )
    exited = observation | {
        "observed_state": "exited",
        "termination_confirmed": True,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    call("POST", "/worker/observations", exited | {"provider_id": "other-provider"}, 409, worker)
    call("POST", "/worker/observations", exited, 201, worker)
    confirmed = call(
        "POST",
        f"/workspace-handoffs/{handoff['id']}/confirm-stop",
        {"expected_revision": handoff["revision"]},
        200,
    )
    assert confirmed["status"] == "stopped_confirmed" and confirmed["human_owner"] is True
    call("POST", "/worker/observations", exited, 409, worker)
    call(
        "POST",
        f"/workspace-handoffs/{handoff['id']}/handback",
        {
            "expected_revision": confirmed["revision"],
            "expected_baseline_digest": "0" * 64,
            "result_snapshot_id": baseline["id"],
            "expected_result_digest": baseline["digest"],
        },
        409,
    )
    (source / "keep.txt").write_text("human edit")
    (source / "delete.txt").unlink()
    (source / "added.txt").write_text("new")
    (source / ".env").write_text("excluded")
    result = adopt("result")
    handed_back = call(
        "POST",
        f"/workspace-handoffs/{handoff['id']}/handback",
        {
            "expected_revision": confirmed["revision"],
            "expected_baseline_digest": baseline["digest"],
            "result_snapshot_id": result["id"],
            "expected_result_digest": result["digest"],
        },
        200,
    )
    assert handed_back["changes"] == {
        "added": ["added.txt"],
        "changed": ["keep.txt"],
        "deleted": ["delete.txt"],
    }
    assert handed_back["exclusions"] == [{"relative_path": ".env", "reason": "credential_path"}]
    assert handed_back["context_epoch"] > handoff["stop_epoch"]
    restarted = TestClient(create_app(state))
    listed = restarted.get(f"/projects/{project['id']}/workspace-handoffs", headers=owner).json()
    assert listed["workspace_handoffs"] == [handed_back]


def _journaled_handoff(tmp_path):
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    package = RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "package",
        canonical_digest({"expected_epoch": lease["epoch"]}),
        lease["epoch"],
    )[1]
    payload = {
        "task_id": grant["task_id"],
        "grant_id": grant["id"],
        "expected_task_revision": grant["task_revision"],
        "expected_grant_revision": grant["revision"],
        "baseline_snapshot_id": grant["snapshot_id"],
        "expected_baseline_digest": grant["snapshot_digest"],
        "workspace_label": "journaled fixture root",
    }
    handoffs = WorkspaceHandoffStore(state)
    with pytest.raises(Conflict, match="expected revision"):
        handoffs.request_handoff(
            owner,
            grant["project_id"],
            "handoff-stale",
            canonical_digest(payload | {"expected_grant_revision": grant["revision"] + 1}),
            payload | {"expected_grant_revision": grant["revision"] + 1},
        )
    _, handoff = handoffs.request_handoff(
        owner, grant["project_id"], "handoff", canonical_digest(payload), payload
    )
    return state, execution, owner, worker, grant, lease, package["operation"], handoff, payload


@pytest.mark.parametrize("reconcile_first", [False, True])
def test_journaled_handoff_fences_cancellation_and_orders_confirmation(tmp_path, reconcile_first):
    state, execution, owner, worker, grant, lease, operation, handoff, payload = _journaled_handoff(
        tmp_path
    )
    assert handoff["human_owner"] is False
    assert (
        WorkspaceHandoffStore(state).request_handoff(
            owner, grant["project_id"], "handoff", canonical_digest(payload), payload
        )[1]
        == handoff
    )
    with _connect(state / "tessera.sqlite3") as connection:
        cancelled = connection.execute(
            "SELECT status, revision, epoch FROM execution_grants WHERE id=?", (grant["id"],)
        ).fetchone()
        journal = connection.execute(
            "SELECT desired_state FROM external_operations WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()
        events = [
            row[0]
            for row in connection.execute(
                """SELECT event_type FROM events WHERE task_id=?
                AND event_type IN ('execution.grant_cancel_requested',
                'workspace.handoff_stop_requested') ORDER BY cursor""",
                (grant["task_id"],),
            )
        ]
    assert cancelled["status"] == "cancel_requested"
    assert cancelled["epoch"] == handoff["stop_epoch"]
    assert journal["desired_state"] == "cancel_requested"
    assert events == ["execution.grant_cancel_requested", "workspace.handoff_stop_requested"]
    with pytest.raises(Conflict, match="not active"):
        execution.renew_execution_lease(worker, lease["id"], "renew", "renew", lease["epoch"])
    with pytest.raises(Conflict, match="not active"):
        RuntimePackageStore(state).execution_package(
            worker,
            lease["id"],
            "package-after-handoff",
            canonical_digest({"expected_epoch": lease["epoch"]}),
            lease["epoch"],
        )

    def report(key, observed_state, confirmed, seconds):
        body = {
            "expected_epoch": lease["epoch"],
            "operation_id": operation["operation_id"],
            "observed_state": observed_state,
            "termination_confirmed": confirmed,
            "observed_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
            "provider_id": "handoff-fake-provider",
        }
        return execution.report_execution_observation(
            worker, lease["id"], key, canonical_digest(body), body
        )

    report("unknown", "unknown", False, 0)
    with pytest.raises(Conflict, match="matching worker-confirmed"):
        WorkspaceHandoffStore(state).confirm_stop(
            owner, handoff["id"], "confirm-unknown", "confirm-unknown", handoff["revision"]
        )
    assert WorkspaceHandoffStore(state).get_handoff(owner, handoff["id"])["human_owner"] is False
    report("exited", "exited", True, 1)
    report("unknown-after-exit", "unknown", False, 2)
    with pytest.raises(Conflict, match="matching worker-confirmed"):
        WorkspaceHandoffStore(state).confirm_stop(
            owner,
            handoff["id"],
            "confirm-after-unknown",
            "confirm-after-unknown",
            handoff["revision"],
        )
    report("exited-final", "exited", True, 3)
    with pytest.raises(Conflict, match="no longer awaiting"):
        WorkspaceHandoffStore(state).confirm_stop(
            owner, handoff["id"], "confirm-stale", "confirm-stale", handoff["revision"] + 1
        )

    reconciliation = {
        "expected_revision": cancelled["revision"],
        "settlement_cents": 0,
        "provider_terminated": True,
    }
    if reconcile_first:
        execution.reconcile_execution_grant(
            owner, grant["id"], "reconcile", canonical_digest(reconciliation), reconciliation
        )
        replacement = {
            "task_id": grant["task_id"],
            "expected_task_revision": grant["task_revision"],
            "expected_plan_digest": grant["plan_digest"],
            "worker_principal_id": worker["id"],
            "snapshot_id": grant["snapshot_id"],
            "pinned_image": grant["pinned_image"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "max_exposure_cents": grant["max_exposure_cents"],
            "currency": grant["currency"],
            "acknowledges_unmeasured_infrastructure": True,
        }
        with pytest.raises(Conflict, match="open workspace handoff"):
            execution.issue_execution_grant(
                owner,
                grant["project_id"],
                "replacement-before-confirm",
                canonical_digest(replacement),
                replacement,
            )
    # A fresh store models process restart; the durable handoff remains confirmable
    # after reconciliation because its final attributed exit is retained.
    confirmed = WorkspaceHandoffStore(state).confirm_stop(
        owner, handoff["id"], "confirm", "confirm", handoff["revision"]
    )[1]
    assert confirmed["human_owner"] is True
    if not reconcile_first:
        execution.reconcile_execution_grant(
            owner, grant["id"], "reconcile", canonical_digest(reconciliation), reconciliation
        )
    assert ExecutionStore(state).get_execution_grant(owner, grant["id"])["status"] == "reconciled"


def test_open_handoff_survives_restart_and_repeatable_migration(tmp_path):
    state, _, owner, _, grant, _, operation, handoff, _ = _journaled_handoff(tmp_path)
    assert migrate_state(state) == 25
    assert migrate_state(state) == 25
    restarted = WorkspaceHandoffStore(state).get_handoff(owner, handoff["id"])
    assert restarted["status"] == "stop_requested" and restarted["human_owner"] is False
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT desired_state FROM external_operations WHERE operation_id=?",
                (operation["operation_id"],),
            ).fetchone()[0]
            == "cancel_requested"
        )
        assert (
            connection.execute(
                "SELECT status FROM execution_grants WHERE id=?", (grant["id"],)
            ).fetchone()[0]
            == "cancel_requested"
        )


def test_handoff_cross_tenant_is_not_disclosed(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    client = TestClient(create_app(state))
    project = client.post(
        "/projects", json={"name": "private"}, headers=owner | {"Idempotency-Key": "project"}
    ).json()
    response = client.get(
        f"/projects/{project['id']}/workspace-handoffs",
        headers={"Authorization": "Bearer bad"},
    )
    assert response.status_code == 401


def test_schema17_handoff_migration_is_explicit_transactional_and_repeatable(tmp_path, monkeypatch):
    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE workspace_handoffs")
        connection.execute("UPDATE schema_meta SET schema_version=16")

    with pytest.raises(ValueError, match="explicit"):
        Store(state)

    def fail(connection):
        from tessera_control.handoffs import create_handoff_schema

        create_handoff_schema(connection)
        raise RuntimeError("injected workspace handoff migration fault")

    monkeypatch.setitem(MIGRATIONS, 16, fail)
    with pytest.raises(RuntimeError, match="injected workspace handoff"):
        migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 16
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_handoffs'"
            ).fetchone()
            is None
        )
    monkeypatch.undo()
    assert migrate_state(state) == 25
    assert migrate_state(state) == 25
