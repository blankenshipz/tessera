from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tessera_control.execution import ExecutionStore
from tessera_control.execution_models import IssueExecutionGrant, ReportExecutionObservation
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _token_hash,
    canonical_digest,
    initialize_state,
)
from tessera_control.workspace import capture_workspace, inventory_digest


def _owner(state: Path) -> dict[str, object]:
    header = (state / "owner.header").read_text().strip()
    return Store(state).authenticate(header.split(": ", 1)[1].removeprefix("Bearer "))


def _worker(
    state: Path, owner: dict[str, object], project_id: str, token: str
) -> dict[str, object]:
    now = datetime.now(UTC)
    expires = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    created = now.isoformat().replace("+00:00", "Z")
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)
            VALUES (?, ?, 'agent', ?, NULL, ?)""",
            (f"worker-{token}", owner["org_id"], _token_hash(token), created),
        )
        connection.execute(
            """INSERT INTO credentials
            (id, principal_id, kind, audience, project_id, parent_id, expires_at,
             revoked_at, created_at, created_by_principal_id)
            VALUES (?, ?, 'worker', 'worker', ?, ?, ?, NULL, ?, ?)""",
            (
                f"credential-{token}",
                f"worker-{token}",
                project_id,
                owner["credential_id"],
                expires,
                created,
                owner["id"],
            ),
        )
        connection.execute(
            """INSERT INTO worker_registry
            (id, org_id, project_id, worker_principal_id, credential_id, approved_build_identifier,
             approved_build_digest, trust_class, region, lifecycle_state, revision, registered_at,
             heartbeat_at, last_seen_at, retired_at, retired_by_principal_id)
            VALUES (?, ?, ?, ?, ?, 'synthetic-test-worker', ?, 'test', 'local', 'ready', 1,
                    ?, ?, ?, NULL, NULL)""",
            (
                f"registered-{token}",
                owner["org_id"],
                project_id,
                f"worker-{token}",
                f"credential-{token}",
                "a" * 64,
                created,
                created,
                created,
            ),
        )
    return Store(state).authenticate_worker(token)


def _snapshot_release(
    state: Path, owner: dict[str, object], project_id: str, worker_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    root = state.parent / "workspace"
    root.mkdir()
    (root / "input.txt").write_bytes(b"immutable input")
    capture = capture_workspace(root.absolute())
    digest = inventory_digest(capture.inventory)
    snapshots = SnapshotStore(state)
    _, snapshot = snapshots.adopt_snapshot(owner, project_id, "snapshot", digest, capture)
    payload = {
        "expected_digest": digest,
        "worker_principal_id": worker_id,
        "classification": "public",
        "permits_worker_transfer": True,
        "rationale": "Local CPU pilot input release.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    _, release = SnapshotReleaseStore(state).release_snapshot(
        owner, snapshot["id"], "release", canonical_digest(payload), payload
    )
    return snapshot, release


def _task(
    state: Path, owner: dict[str, object], project_id: str, snapshot_digest: str, name: str
) -> dict[str, object]:
    store = Store(state)
    _, task = store.create_task(
        owner,
        project_id,
        f"task-{name}",
        canonical_digest({"name": name}),
        {
            "title": name,
            "purpose": "Exercise durable local execution authority.",
            "budget_cents": 100,
            "max_duration_seconds": 60,
        },
    )
    revision = {
        "expected_revision": task["revision"],
        "title": name,
        "purpose": "Exercise durable local execution authority.",
        "budget_cents": 100,
        "max_duration_seconds": 60,
        "execution_spec": {
            "argv": ["python", "-c", "print('pilot')"],
            "input_snapshot_digest": snapshot_digest,
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }
    _, revised = store.revise_task_plan(
        owner, task["id"], f"revise-{name}", canonical_digest(revision), revision
    )
    _, approved = store.approve_task(
        owner,
        revised["id"],
        f"approve-{name}",
        canonical_digest({"expected_revision": revised["revision"]}),
        revised["revision"],
        revised["current_plan_digest"],
    )
    return approved


def _grant_payload(
    task: dict[str, object], worker: dict[str, object], snapshot: dict[str, object]
) -> dict[str, object]:
    return {
        "task_id": task["id"],
        "expected_task_revision": task["revision"],
        "expected_plan_digest": task["current_plan_digest"],
        "worker_principal_id": worker["id"],
        "snapshot_id": snapshot["id"],
        "pinned_image": "python@sha256:" + "a" * 64,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        "max_exposure_cents": 60,
        "currency": "USD",
        "acknowledges_unmeasured_infrastructure": True,
    }


def _setup(
    tmp_path: Path,
) -> tuple[
    Path,
    ExecutionStore,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    state = tmp_path / "state"
    initialize_state(state)
    owner = _owner(state)
    store = Store(state)
    _, project = store.create_project(
        owner, "project", canonical_digest({"name": "Pilot"}), {"name": "Pilot"}
    )
    worker = _worker(state, owner, project["id"], "worker-token")
    snapshot, _ = _snapshot_release(state, owner, project["id"], worker["id"])
    task = _task(state, owner, project["id"], snapshot["digest"], "First")
    execution = ExecutionStore(state)
    limits = {
        "expected_revision": 0,
        "currency": "USD",
        "monetary_limit_cents": 100,
        "max_concurrency": 1,
    }
    assert (
        execution.configure_project_execution(
            owner, project["id"], "limits", canonical_digest(limits), limits
        )[0]
        == 201
    )
    return state, execution, owner, worker, project, task


def test_grant_reservation_replay_and_concurrency_limit(tmp_path: Path) -> None:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    limits = {
        "expected_revision": 1,
        "currency": "USD",
        "monetary_limit_cents": 200,
        "max_concurrency": 1,
    }
    assert (
        execution.configure_project_execution(
            owner, project["id"], "raise-limit", canonical_digest(limits), limits
        )[0]
        == 200
    )
    payload = _grant_payload(task, worker, {"id": "missing"})
    # Use the released snapshot recorded by the task's approved execution spec.
    with _connect(execution.database_path) as connection:
        snapshot_id, snapshot_digest = connection.execute(
            """SELECT r.snapshot_id, s.digest FROM snapshot_releases r
            JOIN source_snapshots s ON s.id = r.snapshot_id"""
        ).fetchone()
    payload["snapshot_id"] = snapshot_id
    first = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    replay = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    assert first == replay and first[0] == 201
    assert first[1]["execution_dispatched"] is False
    assert execution.get_project_execution(owner, project["id"])["held_cents"] == 60
    second = _task(
        state,
        owner,
        project["id"],
        snapshot_digest,
        "Second",
    )
    second_payload = _grant_payload(second, worker, {"id": snapshot_id})
    with pytest.raises(Conflict, match="concurrency"):
        execution.issue_execution_grant(
            owner, project["id"], "second", canonical_digest(second_payload), second_payload
        )


def test_worker_lease_fencing_revocation_and_scope(tmp_path: Path) -> None:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "acquire", "acquire")
    _, renewed = execution.renew_execution_lease(
        worker, lease["id"], "renew", "renew", lease["epoch"]
    )
    assert renewed["epoch"] == lease["epoch"]
    with pytest.raises(Conflict, match="current"):
        execution.renew_execution_lease(worker, lease["id"], "stale", "stale", lease["epoch"] + 1)
    with pytest.raises(Conflict, match="already has a lease"):
        execution.acquire_execution_lease(worker, grant["id"], "again", "again")

    _, other_project = Store(state).create_project(
        owner, "other", canonical_digest({"name": "Other"}), {"name": "Other"}
    )
    other_worker = _worker(state, owner, other_project["id"], "other-worker-token")
    with pytest.raises(NotFound):
        execution.acquire_execution_lease(other_worker, grant["id"], "foreign", "foreign")
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE credentials SET revoked_at = ? WHERE id = ?", ("x", worker["credential_id"])
        )
    with pytest.raises(Forbidden):
        execution.renew_execution_lease(worker, lease["id"], "revoked", "revoked", renewed["epoch"])


def test_cancel_observation_reconciliation_and_real_overage(tmp_path: Path) -> None:
    _, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "acquire", "acquire")
    execution.renew_execution_lease(worker, lease["id"], "renew", "renew", lease["epoch"])
    unknown = {
        "expected_epoch": lease["epoch"],
        "observed_state": "unknown",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "local-container",
        "termination_confirmed": False,
    }
    execution.report_execution_observation(
        worker, lease["id"], "unknown-before-revoke", canonical_digest(unknown), unknown
    )
    with pytest.raises(Conflict, match="out of order"):
        execution.report_execution_observation(
            worker,
            lease["id"],
            "out-of-order",
            "out-of-order",
            unknown | {"observed_at": lease["issued_at"]},
        )
    with pytest.raises(Conflict, match="Provider identity"):
        execution.report_execution_observation(
            worker,
            lease["id"],
            "changed-provider",
            "changed-provider",
            unknown | {"provider_id": "other-container"},
        )
    _, cancelled = execution.revoke_execution_grant(owner, grant["id"], "cancel", "cancel", 1)
    with pytest.raises(Conflict, match="Cached|not active"):
        execution.acquire_execution_lease(worker, grant["id"], "acquire", "acquire")
    with pytest.raises(Conflict, match="Cached|not active"):
        execution.renew_execution_lease(worker, lease["id"], "renew", "renew", lease["epoch"])
    with pytest.raises(Conflict, match="termination"):
        execution.reconcile_execution_grant(
            owner,
            grant["id"],
            "reconcile-unknown",
            "reconcile-unknown",
            {
                "expected_revision": cancelled["revision"],
                "settlement_cents": 0,
                "provider_terminated": True,
            },
        )
    exited = unknown | {"observed_state": "exited", "termination_confirmed": True}
    execution.report_execution_observation(
        worker, lease["id"], "exited", canonical_digest(exited), exited
    )
    with pytest.raises(Conflict, match="termination"):
        execution.report_execution_observation(
            worker,
            lease["id"],
            "later-unknown",
            canonical_digest(unknown),
            unknown,
        )
    _, observation = execution.report_execution_observation(
        worker,
        lease["id"],
        "exited-final",
        "exited-final",
        exited | {"observed_at": (datetime.now(UTC) + timedelta(seconds=2)).isoformat()},
    )
    assert observation["desired_state"] == "cancel_requested"
    _, reconciled = execution.reconcile_execution_grant(
        owner,
        grant["id"],
        "reconcile",
        "reconcile",
        {
            "expected_revision": cancelled["revision"],
            "settlement_cents": 120,
            "provider_terminated": True,
        },
    )
    assert reconciled["status"] == "reconciled"
    limits = execution.get_project_execution(owner, project["id"])
    assert limits["held_cents"] == 0 and limits["settled_cents"] == 120
    with pytest.raises(Conflict, match="budget|limit"):
        execution.issue_execution_grant(
            owner, project["id"], "blocked", canonical_digest(payload), payload
        )


def test_cancellation_telemetry_is_derived_fenced_and_does_not_clear_unknown(
    tmp_path: Path,
) -> None:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    assert grant["cancellation"] == {"state": "not_requested"}
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    unknown = {
        "expected_epoch": lease["epoch"],
        "observed_state": "unknown",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "synthetic-local",
        "termination_confirmed": False,
    }
    _, reported = execution.report_execution_observation(
        worker, lease["id"], "unknown", canonical_digest(unknown), unknown
    )
    assert reported["cancellation"]["state"] == "not_requested"
    _, requested = execution.revoke_execution_grant(owner, grant["id"], "cancel", "cancel", 1)
    assert requested["cancellation"]["state"] == "unresolved"
    repeated = execution.revoke_execution_grant(
        owner, grant["id"], "cancel-again", "cancel-again", requested["revision"]
    )
    assert repeated[1]["cancellation"]["state"] == "unresolved"
    with pytest.raises(Conflict, match="epoch"):
        execution.report_execution_observation(
            worker, lease["id"], "stale", "stale", unknown | {"expected_epoch": lease["epoch"] + 1}
        )
    same_project_wrong_worker = _worker(state, owner, project["id"], "wrong-worker-token")
    with pytest.raises(NotFound):
        execution.report_execution_observation(
            same_project_wrong_worker, lease["id"], "wrong-worker", "wrong-worker", unknown
        )
    exited = unknown | {
        "observed_state": "exited",
        "termination_confirmed": True,
        "observed_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
    }
    _, confirmed = execution.report_execution_observation(
        worker, lease["id"], "exited", canonical_digest(exited), exited
    )
    assert confirmed["cancellation"]["state"] == "confirmed_termination"
    assert (
        execution.get_execution_grant(owner, grant["id"])["cancellation"]["state"]
        == "confirmed_termination"
    )
    with _connect(execution.database_path) as connection:
        event = connection.execute(
            "SELECT payload_json FROM events WHERE "
            "event_type='execution.observation_reported' ORDER BY cursor DESC LIMIT 1"
        ).fetchone()[0]
    assert json.loads(event)["cancellation"]["state"] == "confirmed_termination"
    _, reconciled = execution.reconcile_execution_grant(
        owner,
        grant["id"],
        "reconcile",
        "reconcile",
        {
            "expected_revision": requested["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
    )
    assert reconciled["cancellation"]["state"] == "confirmed_termination"


def test_unleased_cancel_reconciles_zero_and_event_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})

    def fail_event(*_: object) -> None:
        raise sqlite3.OperationalError("injected event failure")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail_event))
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            execution.issue_execution_grant(
                owner, project["id"], "rollback", canonical_digest(payload), payload
            )
    assert execution.get_project_execution(owner, project["id"])["held_cents"] == 0
    with _connect(execution.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%execution-grants'"
            ).fetchone()[0]
            == 0
        )
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    _, cancelled = execution.revoke_execution_grant(owner, grant["id"], "cancel", "cancel", 1)
    _, reconciled = execution.reconcile_execution_grant(
        owner,
        grant["id"],
        "reconcile",
        "reconcile",
        {
            "expected_revision": cancelled["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
    )
    assert reconciled["status"] == "reconciled"


def test_expired_grant_keeps_held_exposure_and_cannot_lease(tmp_path: Path) -> None:
    _, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    with _connect(execution.database_path) as connection:
        connection.execute(
            "UPDATE execution_grants SET expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), grant["id"]),
        )
    with pytest.raises(Conflict, match="expired"):
        execution.acquire_execution_lease(worker, grant["id"], "expired", "expired")
    assert execution.get_project_execution(owner, project["id"])["held_cents"] == 60


def test_execution_models_accept_json_times_and_reject_unsafe_images() -> None:
    payload = {
        "task_id": "task",
        "expected_task_revision": 1,
        "expected_plan_digest": "a" * 64,
        "worker_principal_id": "worker",
        "snapshot_id": "snapshot",
        "pinned_image": "registry.example:5000/local/python@sha256:" + "b" * 64,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "max_exposure_cents": 1,
        "currency": "USD",
        "acknowledges_unmeasured_infrastructure": True,
    }
    assert IssueExecutionGrant.model_validate(payload).expires_at.tzinfo is not None
    with pytest.raises(ValidationError):
        IssueExecutionGrant.model_validate(
            payload | {"pinned_image": "../python@sha256:" + "b" * 64}
        )
    with pytest.raises(ValidationError):
        ReportExecutionObservation.model_validate(
            {
                "lease_id": "lease",
                "expected_epoch": 1,
                "observed_state": "running",
                "observed_at": 1,
                "termination_confirmed": False,
            }
        )


def test_service_rejects_unacknowledged_unmeasured_exposure(tmp_path: Path) -> None:
    _, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    payload["acknowledges_unmeasured_infrastructure"] = False
    with pytest.raises(StoreError, match="Invalid execution"):
        execution.issue_execution_grant(
            owner, project["id"], "unacknowledged", canonical_digest(payload), payload
        )


def test_prior_task_settlement_survives_plan_revision_budget_checks(tmp_path: Path) -> None:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    widened_limits = {
        "expected_revision": 1,
        "currency": "USD",
        "monetary_limit_cents": 200,
        "max_concurrency": 1,
    }
    execution.configure_project_execution(
        owner, project["id"], "widen-project", canonical_digest(widened_limits), widened_limits
    )
    with _connect(execution.database_path) as connection:
        snapshot_id, snapshot_digest = connection.execute(
            """SELECT r.snapshot_id, s.digest FROM snapshot_releases r
            JOIN source_snapshots s ON s.id = r.snapshot_id"""
        ).fetchone()
    first_payload = _grant_payload(task, worker, {"id": snapshot_id})
    _, first_grant = execution.issue_execution_grant(
        owner, project["id"], "first", canonical_digest(first_payload), first_payload
    )
    _, lease = execution.acquire_execution_lease(worker, first_grant["id"], "lease", "lease")
    exit_report = {
        "expected_epoch": lease["epoch"],
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "local-container",
        "termination_confirmed": True,
    }
    execution.report_execution_observation(
        worker, lease["id"], "exited", canonical_digest(exit_report), exit_report
    )
    execution.reconcile_execution_grant(
        owner,
        first_grant["id"],
        "settle",
        "settle",
        {"expected_revision": 1, "settlement_cents": 60, "provider_terminated": True},
    )
    with pytest.raises(Conflict, match="prior exposure"):
        execution.issue_execution_grant(
            owner, project["id"], "same-cap", canonical_digest(first_payload), first_payload
        )

    revised_plan = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": 120,
        "max_duration_seconds": task["max_duration_seconds"],
        "execution_spec": {
            "argv": ["python", "-c", "print('pilot')"],
            "input_snapshot_digest": snapshot_digest,
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }
    _, revised = Store(state).revise_task_plan(
        owner,
        task["id"],
        "raise-task-cap",
        canonical_digest(revised_plan),
        revised_plan,
    )
    _, approved = Store(state).approve_task(
        owner,
        task["id"],
        "approve-raised-cap",
        canonical_digest({"expected_revision": revised["revision"]}),
        revised["revision"],
        revised["current_plan_digest"],
    )
    raised_payload = _grant_payload(approved, worker, {"id": snapshot_id})
    assert (
        execution.issue_execution_grant(
            owner, project["id"], "raised-cap", canonical_digest(raised_payload), raised_payload
        )[0]
        == 201
    )


def test_zero_exposure_authority_freezes_project_currency(tmp_path: Path) -> None:
    _, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id}) | {"max_exposure_cents": 0}
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "zero", canonical_digest(payload), payload
    )
    changed_currency = {
        "expected_revision": 1,
        "currency": "EUR",
        "monetary_limit_cents": 100,
        "max_concurrency": 1,
    }
    with pytest.raises(Conflict, match="currency"):
        execution.configure_project_execution(
            owner,
            project["id"],
            "change-currency",
            canonical_digest(changed_currency),
            changed_currency,
        )
    with _connect(execution.database_path) as connection:
        connection.execute(
            "UPDATE execution_project_limits SET currency = 'EUR' WHERE project_id = ?",
            (project["id"],),
        )
    with pytest.raises(Conflict, match="currency"):
        execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
