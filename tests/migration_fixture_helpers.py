"""Explicit empty v55 removal for disposable pre-v55 test databases only.

Never use on application state. No version changes, commits, authority deletion,
or migration inference: callers retain their original historical reconstruction.
"""

import sqlite3

# Children precede their referenced parents (including the root-fence ledger).
RUNTIME_V55_TABLES = (
    "runtime_native_entries",
    "runtime_native_settlements",
    "runtime_native_claims",
    "runtime_native_root_fences",
    "runtime_native_operations",
    "runtime_authority_grants",
    "runtime_binding_revocations",
    "runtime_financial_assertions",
    "runtime_enrollments",
    "runtime_policy_profiles",
)
IMMUTABLE_RUNTIME_V55_TABLES = (
    "runtime_policy_profiles",
    "runtime_enrollments",
    "runtime_financial_assertions",
    "runtime_binding_revocations",
    "runtime_native_claims",
    "runtime_native_settlements",
    "runtime_native_entries",
)


def assert_empty_runtime_v55(connection: sqlite3.Connection) -> None:
    """All tables must exist and be empty; partial or populated states fail closed."""
    for table in RUNTIME_V55_TABLES:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, (
            f"Refusing to remove populated runtime authority/history: {table}"
        )


def strip_empty_runtime_v55(connection: sqlite3.Connection) -> None:
    """Remove the exact empty additions, in the caller's transaction, without commit."""
    # Validate every table BEFORE any DDL, even if a later table holds authority.
    assert_empty_runtime_v55(connection)
    for table in IMMUTABLE_RUNTIME_V55_TABLES:
        for action in ("update", "delete"):
            connection.execute(f"DROP TRIGGER {table}_no_{action}")
    for table in RUNTIME_V55_TABLES:
        connection.execute(f"DROP TABLE {table}")
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name=? OR tbl_name=?", (table, table)
            ).fetchone()
            is None
        )
