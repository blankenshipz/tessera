"""Opt-in real Electron/local-service UI journey; no provider execution.

Only the credential picker is substituted. Documentation records are explicitly
synthetic human observations, never execution receipts. Output is private.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from check_control_runtime import serve
from check_desktop_control import prepare_experiment

from tessera_control.store import Store, _connect, canonical_digest, initialize_state


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="tessera-workspace-") as directory:
        state = Path(directory) / "control"
        initialize_state(state)
        store = Store(state)
        owner = store.authenticate((state / "owner.header").read_text().split("Bearer ")[1].strip())
        body = {"name": "UI test · research workspace"}
        _, project = store.create_project(owner, "ui-project", canonical_digest(body), body)
        experiment = prepare_experiment(state, owner, project["id"])
        with serve(state) as endpoint:
            fixture = Path(directory) / "fixture.json"
            fixture.write_text(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "header": str(state / "owner.header"),
                        "projectId": project["id"],
                        "experimentId": experiment["experiment"]["id"],
                        "output": str(output),
                    }
                )
            )
            environment = dict(os.environ)
            environment.pop("ELECTRON_RUN_AS_NODE", None)
            subprocess.run(
                [
                    str(repository / "desktop/node_modules/.bin/electron"),
                    str(repository / "desktop/test/electron-workspace-smoke.cjs"),
                    str(fixture),
                ],
                env=environment,
                check=True,
                timeout=90,
            )
            projects = store.list_projects(owner, 0, 100)
            created = [item for item in projects if item["name"] == "UI test · record-only journey"]
            assert len(created) == 1
            with _connect(store.database_path) as connection:
                assert (
                    connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0
                )
                assert (
                    connection.execute("SELECT count(*) FROM execution_leases").fetchone()[0] == 0
                )
            # The renderer checks one durable draft after reload and reconnect.
            print("Local service verified: one UI-created project; zero grants and leases.")


if __name__ == "__main__":
    main()
