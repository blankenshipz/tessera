"""Confirmed-stop workspace handoff API tests; no provider work is dispatched."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.migrations import MIGRATIONS
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Store, initialize_state, migrate_state
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
    assert migrate_state(state) == 22
    assert migrate_state(state) == 22
