"""Guard: the fresh schema may only change together with a migration and version bump.

The backup/restore check compares a backup with the shapes of a fresh schema. If the
fresh schema is edited without a migration that converges older databases, upgraded
databases drift from fresh and can no longer pass that check.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.recovery import _table_shapes
from tessera_control.store import _create_schema

# APPEND-ONLY mapping of schema_version -> fingerprint of the fresh schema shapes.
# Never edit or delete an existing entry. When the fresh schema changes, add a
# migration, bump LATEST_SCHEMA_VERSION, and append the new version's fingerprint.
# A version bump that does not change fresh shapes repeats the previous fingerprint.
FRESH_SCHEMA_FINGERPRINTS: dict[int, str] = {
    78: "07ab61598b1041ef454a78e63da99e2bef135373304293aedc0dfe761a012a63",
    79: "07ab61598b1041ef454a78e63da99e2bef135373304293aedc0dfe761a012a63",
    80: "07ab61598b1041ef454a78e63da99e2bef135373304293aedc0dfe761a012a63",
    81: "5c732e73abdb4b11e331a3815bcb7bc9166521147b9bd764f76b9e3137796235",
    82: "1a91293d2834a440f2d8e12cf9477b243932d258c30ea41bfdee2899ad2d8b5c",
    83: "00e0b5db456f0d3154858f48e44d1007af3086809208a78b4e804df90f774e58",
    84: "df80364488b16eaea806dfa0628fbb040e15b93bc134a88a982043b48bcf0680",
    85: "58416c4abf15a137f53563b40499e1a71554adec447d321a150766d5e13a7a76",
}


def fresh_schema_fingerprint() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        _create_schema(connection)
        tables, triggers, objects = _table_shapes(connection)
    finally:
        connection.close()
    canonical = {
        "tables": {name: tables[name] for name in sorted(tables)},
        "triggers": triggers,
        "objects": sorted(objects),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(encoded.encode()).hexdigest()


def test_fresh_schema_fingerprint_matches_latest_version():
    actual = fresh_schema_fingerprint()
    expected = FRESH_SCHEMA_FINGERPRINTS.get(LATEST_SCHEMA_VERSION)
    assert expected is not None, (
        f"No fingerprint recorded for LATEST_SCHEMA_VERSION={LATEST_SCHEMA_VERSION}. "
        f"Append {LATEST_SCHEMA_VERSION}: {actual!r} to FRESH_SCHEMA_FINGERPRINTS."
    )
    assert actual == expected, (
        "The fresh schema (store._create_schema) changed without a new schema version. "
        "A change to the fresh schema without a new migration and version bump leaves "
        "upgraded databases unable to pass the backup check. Add a migration that "
        "converges old shapes to the new one, bump LATEST_SCHEMA_VERSION, and append "
        f"the new fingerprint {actual!r} for the new version (do not edit old entries)."
    )
