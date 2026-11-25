"""Indexes that keep a run's per-request lookups bounded as the history grows (ADR 0190).

Every observation post, output upload, manifest and run receipt runs inside control's write
lock. Before these indexes, several of their lookups read a whole table that only grows: the
event log (a lookup by event type and a payload field), every worker observation (a lookup by
lease) and every chunked upload ever begun (a lookup of open uploads). The queries themselves
are unchanged; an index never changes what a query returns, only how it is found.

Idempotent: used for a fresh database and by the schema 92 migration.
"""

from __future__ import annotations

import sqlite3

OUTPUT_LOOKUP_INDEXES = (
    # Lookups by event type, newest or oldest first: a requested grant cancellation, the
    # lease's acquisition trust fields, a recorded run receipt, the egress journal, image build
    # and public fetch reports, a confirmed exit's code.
    "CREATE INDEX IF NOT EXISTS events_by_type_cursor ON events(event_type, cursor)",
    # A lease's observations in recorded (rowid) order: the latest observation and provider,
    # and the late-output check of an exit inside the grant.
    "CREATE INDEX IF NOT EXISTS execution_observations_by_lease "
    "ON execution_observations(lease_id)",
    # A lease's confirmed exits (normally one): when the run's exit was recorded.
    "CREATE INDEX IF NOT EXISTS execution_observations_confirmed_exit "
    "ON execution_observations(lease_id, epoch, recorded_at) "
    "WHERE observed_state='exited' AND termination_confirmed=1",
    # Open chunked uploads (the output budget's reservations), not every upload ever begun.
    "CREATE INDEX IF NOT EXISTS snapshot_chunk_uploads_by_status "
    "ON snapshot_chunk_uploads(status, expires_at)",
)


def create_output_lookup_indexes(connection: sqlite3.Connection) -> None:
    for statement in OUTPUT_LOOKUP_INDEXES:
        connection.execute(statement)
