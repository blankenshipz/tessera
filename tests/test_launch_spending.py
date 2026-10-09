"""Reservation limits are durable admission controls, not provider billing caps."""

import pytest
from test_execution import _grant_payload, _setup, _task

from tessera_control.execution import ExecutionStore
from tessera_control.store import Conflict, _connect, canonical_digest


def test_denied_spending_and_limit_reduction_leave_ledger_unchanged(tmp_path):
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot = dict(connection.execute("SELECT id, digest FROM source_snapshots").fetchone())
    payload = _grant_payload(task, worker, snapshot)
    accepted = execution.issue_execution_grant(
        owner, project["id"], "launch-grant", canonical_digest(payload), payload
    )
    before = execution.get_project_execution(owner, project["id"])
    assert before["held_cents"] == 60

    # A second task fits its own budget, but not the remaining project budget.
    second = _task(state, owner, project["id"], snapshot["digest"], "Second launch task")
    with _connect(execution.database_path) as connection:
        events_before = connection.execute("SELECT count(*) FROM events").fetchone()[0]
    over_budget = _grant_payload(second, worker, snapshot)
    with pytest.raises(Conflict, match="project execution limit"):
        execution.issue_execution_grant(
            owner, project["id"], "denied-grant", canonical_digest(over_budget), over_budget
        )
    with pytest.raises(Conflict, match="currency"):
        wrong_currency = over_budget | {"currency": "EUR"}
        execution.issue_execution_grant(
            owner,
            project["id"],
            "denied-currency",
            canonical_digest(wrong_currency),
            wrong_currency,
        )
    reduction = {
        "expected_revision": before["revision"],
        "currency": "USD",
        "monetary_limit_cents": 59,
        "max_concurrency": 1,
    }
    with pytest.raises(Conflict, match="held and settled exposure"):
        execution.configure_project_execution(
            owner, project["id"], "denied-reduction", canonical_digest(reduction), reduction
        )
    assert execution.get_project_execution(owner, project["id"]) == before

    # Restart/replay cannot mint another reservation; rejected commands consumed
    # neither an idempotency slot nor a grant/event write.
    restarted = ExecutionStore(state)
    assert (
        restarted.issue_execution_grant(
            owner, project["id"], "launch-grant", canonical_digest(payload), payload
        )
        == accepted
    )
    assert restarted.get_project_execution(owner, project["id"]) == before
    with _connect(execution.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE idempotency_key LIKE 'denied-%'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == events_before
