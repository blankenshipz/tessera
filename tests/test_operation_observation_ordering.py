"""Historical timestamp encodings must not release uncertain execution exposure."""

import sqlite3

import pytest

from tessera_control.external_operations import current_operation_reconciliation


@pytest.mark.parametrize("source", ["worker", "recovery_observer"])
@pytest.mark.parametrize(
    "earlier,later,contradiction",
    [
        ("2026-10-09T03:00:00Z", "2026-10-09T03:00:00.500000Z", "later_adverse_observation"),
        ("2026-10-09T03:00:00Z", "2026-10-09T03:00:00.000001Z", "later_adverse_observation"),
        ("2026-10-09T04:00:00+01:00", "2026-10-09T03:00:00.500000Z", "later_adverse_observation"),
        ("2026-10-09T03:00:00Z", "2026-10-09T03:00:00.000000+00:00", "same_time_contradiction"),
        ("2026-10-09T04:00:00+01:00", "2026-10-09T03:00:00Z", "same_time_contradiction"),
    ],
)
def test_existing_history_orders_instants_and_retains_equal_time_conflicts(
    source, earlier, later, contradiction
):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """CREATE TABLE execution_observations (
            id TEXT, operation_id TEXT, observed_state TEXT, observed_at TEXT,
            termination_confirmed INTEGER);
            CREATE TABLE external_operation_receipts (
            operation_id TEXT, observed_state TEXT, observed_at TEXT,
            termination_confirmed INTEGER, observation_sequence INTEGER);
            CREATE TABLE execution_recovery_reconciliations (id TEXT, operation_id TEXT);
            CREATE TABLE execution_recovery_observations (
            id TEXT, recovery_id TEXT, observed_state TEXT, observed_at TEXT,
            termination_confirmed INTEGER);
            INSERT INTO execution_recovery_reconciliations VALUES ('recovery','operation');"""
        )
        table, binding = (
            ("execution_observations", "operation")
            if source == "worker"
            else ("execution_recovery_observations", "recovery")
        )
        connection.executemany(
            f"INSERT INTO {table} VALUES (?,?,?,?,?)",
            [("exit", binding, "exited", earlier, 1), ("adverse", binding, "running", later, 0)],
        )
        current = current_operation_reconciliation(connection, "operation")
        assert current["observed_state"] == "running"
        assert current["termination_confirmed"] is False
        assert current["contradiction_state"] == contradiction
        if contradiction == "same_time_contradiction":
            assert len(current["sources"]) == 2
