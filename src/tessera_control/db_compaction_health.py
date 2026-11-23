"""Read-only advice on whether ``tessera db compact`` would reclaim meaningful space.

SQLite keeps deleted pages on a freelist instead of returning them to the filesystem. The
database is recommended for compaction only when BOTH module-level thresholds are met:

* ``FREELIST_MIN_BYTES``: at least 64 MiB of free pages, and
* ``FREELIST_MIN_FRACTION``: free pages are at least 25% of the whole database file.

The thresholds are constants, not environment variables, so alerts are reproducible.
"""

from __future__ import annotations

import sqlite3
from typing import Any

FREELIST_MIN_BYTES = 64 * 1024 * 1024
FREELIST_MIN_FRACTION = 0.25
COMPACT_COMMAND = "tessera db compact"


def recommendation(connection: sqlite3.Connection) -> dict[str, Any]:
    """Freelist statistics and a compaction recommendation. Never raises."""
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        database_bytes = page_size * page_count
        reclaimable_bytes = page_size * freelist_pages
        fraction = reclaimable_bytes / database_bytes if database_bytes else 0.0
        return {
            "page_size": page_size,
            "page_count": page_count,
            "freelist_pages": freelist_pages,
            "database_bytes": database_bytes,
            "reclaimable_bytes": reclaimable_bytes,
            "reclaimable_fraction": round(fraction, 4),
            "recommended": reclaimable_bytes >= FREELIST_MIN_BYTES
            and fraction >= FREELIST_MIN_FRACTION,
            "command": COMPACT_COMMAND,
        }
    except Exception:
        return {"recommended": False, "error": "unavailable"}
