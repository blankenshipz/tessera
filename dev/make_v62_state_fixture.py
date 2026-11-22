"""Regenerate tests/fixtures/state_v62.sql: a real schema-v62 control database.

Run with the v62 source tree (a `git archive` of a v62 commit) first on the path, so the
schema and rows come from v62 code, not from today's code with tables dropped:

    PYTHONPATH=V62/src:V62/tests python dev/make_v62_state_fixture.py OUT.sql

It creates a fresh state, stages two chunked snapshots through v62's own stores (one
without a data-class declaration, one declared public) and writes an SQL dump.
Synthetic data only; the owner token never leaves the temporary directory.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

from test_artifact_custody import _released_manifest
from test_data_class import PUBLIC, _chunked, _labelled

from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.snapshot_chunks import ChunkedSnapshotStore


def _label_owner_as_automation() -> None:
    """As v62's tests/conftest.py does: owner calls carry an automation label."""
    from tessera_control.approval_trust import AUTOMATION_KEY
    from tessera_control.store import Store

    original = Store.authenticate

    def authenticate(self, token):
        actor = original(self, token)
        if actor.get("kind") == "local_owner" and not actor.get(AUTOMATION_KEY):
            actor[AUTOMATION_KEY] = "fixture"
        return actor

    Store.authenticate = authenticate


def main(out: str) -> None:
    assert LATEST_SCHEMA_VERSION == 62, "run with the v62 source tree first on PYTHONPATH"
    _label_owner_as_automation()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o700)
        state, owner, _worker, manifest, *_ = _released_manifest(root)
        store = ChunkedSnapshotStore(state)
        project = manifest["project_id"]
        _chunked(store, _labelled(owner), project, {"m.py": b"m = 1\n"}, "pre1")
        _chunked(store, _labelled(owner), project, {"rows.csv": b"1\n"}, "pre2", begin=PUBLIC)
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            dump = "\n".join(db.iterdump()) + "\n"
    # No machine-specific temporary path in the committed fixture.
    Path(out).write_text(dump.replace(str(root), "/fixture"))


if __name__ == "__main__":
    main(sys.argv[1])
