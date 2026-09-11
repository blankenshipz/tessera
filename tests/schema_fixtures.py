"""Explicit historical-fixture cleanup; never discard populated experiment evidence."""

import sqlite3


def drop_empty_experiment_schema(connection: sqlite3.Connection) -> None:
    """Remove v25-v35 experiment tables before testing a pre-v25 migration.

    These fixtures initialize current state to seed older authority/evidence. Just
    relabeling its schema version leaves future tables that a historical migration
    correctly refuses to overwrite. Assert emptiness so cleanup cannot hide lost data.
    """
    tables = (
        "research_admitted_conclusion_evidence",
        "admitted_experiment_artifact_evidence_references",
        "research_admitted_attempt_measurements",
        "research_admitted_attempt_artifacts",
        "research_experiment_admitted_attempt_resolutions",
        "research_experiment_admitted_attempt_states",
        "research_experiment_admitted_attempts",
        "research_conclusion_amendments",
        "research_conclusion_lineage",
        "research_conclusion_evidence",
        "experiment_artifact_evidence_references",
        "research_attempt_measurements",
        "research_attempt_artifacts",
        "research_experiment_attempts",
        "research_conclusions",
        "research_experiment_variants",
        "research_experiment_designs",
        "research_experiments",
    )
    present = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema")}
    for table in tables:
        if table in present:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    for table in tables:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
