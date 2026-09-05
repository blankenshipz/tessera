"""Opt-in native Electron/HTTP decision fixture using temporary synthetic authority.

Run with `.venv/bin/python scripts/check_desktop_control.py` after installing the
locked desktop dependencies. The file picker is stubbed only in the test harness;
production renderer, preload, IPC, authentication and control routes are exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from check_control_runtime import serve

from tessera_control.interaction import InteractionStore
from tessera_control.store import Store, _connect, canonical_digest, initialize_state


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="tessera-desktop-control-") as directory:
        root = Path(directory)
        state = root / "control"
        initialize_state(state)
        store = Store(state)
        owner = store.authenticate(
            (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
        )
        body = {"name": "Synthetic desktop decision fixture"}
        _, project = store.create_project(owner, "project", canonical_digest(body), body)
        interaction = InteractionStore(store)
        _, decision = interaction.request_decision(
            owner,
            "decision",
            project["id"],
            "direction",
            {"instruction": "Record synthetic platform acceptance only; do not launch work."},
            (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        )
        with serve(state) as endpoint:
            fixture = root / "fixture.json"
            fixture.write_text(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "header": str(state / "owner.header"),
                        "projectId": project["id"],
                        "decisionId": decision["id"],
                    }
                )
            )
            environment = dict(os.environ)
            environment.pop("ELECTRON_RUN_AS_NODE", None)
            result = subprocess.run(
                [
                    str(repository / "desktop/node_modules/.bin/electron"),
                    str(repository / "desktop/test/electron-control-smoke.cjs"),
                    str(fixture),
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(f"Desktop control fixture failed: {result.stderr[-2000:]}")
            resolved = interaction.list_decisions(owner, project["id"], status="approved")
            assert len(resolved) == 1 and resolved[0]["id"] == decision["id"]
            assert resolved[0]["rationale"] == "Synthetic UI acceptance fixture."
            assert resolved[0]["resolved_by_principal_id"] == owner["id"]
            with closing(_connect(store.database_path)) as connection:
                assert (
                    connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0
                )
                assert (
                    connection.execute("SELECT count(*) FROM execution_leases").fetchone()[0] == 0
                )
            print(json.dumps({"desktop_decision": "approved", "execution_started": False}))


if __name__ == "__main__":
    main()
