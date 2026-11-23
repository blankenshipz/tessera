"""Guard the migration registry: every version up to LATEST has exactly one migration.

Migrations are registered with ``MIGRATIONS.setdefault(version - 1, function)``. If two
migrations claim the same version, the first one registered wins and the other is silently
never run; if a version is missing, ``migrate_database`` has no way across the gap. Both fail
quietly in review, so this test makes them loud.
"""

from __future__ import annotations

from tessera_control.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS


def test_registered_migrations_are_contiguous_up_to_latest():
    keys = sorted(MIGRATIONS)
    missing = [v for v in range(min(keys), LATEST_SCHEMA_VERSION) if v not in MIGRATIONS]
    assert not missing, f"no migration registered from schema version(s) {missing}"
    assert max(keys) == LATEST_SCHEMA_VERSION - 1, (
        "the highest registered migration must lead to LATEST_SCHEMA_VERSION "
        f"(highest key {max(keys)}, latest {LATEST_SCHEMA_VERSION})"
    )


def test_each_migration_function_is_registered_once():
    # Not the tests-only placeholders tests/conftest.py registers for versions whose lanes have
    # not landed (they are one closure, by design, and never exist in the product code).
    names = [
        function.__qualname__
        for function in MIGRATIONS.values()
        if "_bridge_unlanded_migrations" not in function.__qualname__
    ]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"the same migration is registered for several versions: {duplicates}"
