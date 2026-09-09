"""Opt-in loopback control plus native development-worker Docker fixture.

Run only after pulling the pinned local image.  This fixture creates synthetic
human authority in temporary state, retains outputs only in quarantine, and
does not claim provider, cost, or scientific success.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

from check_control_runtime import IMAGE, docker, prepare, serve

from tessera_worker.provider import FakeProvider
from tessera_worker.service import DevelopmentWorkerConfig, DevelopmentWorkerService

OUTPUT_PROBE = """
from pathlib import Path
Path('/outputs/result.txt').write_text('development fixture output')
"""


def _remove_fixture_root(root: Path) -> None:
    """Remove only the fixture's own read-only staged snapshot after success."""

    root = root.resolve()

    def make_writable_and_retry(action, path, error):
        path = Path(path)
        try:
            path.relative_to(root)
        except ValueError:
            # Never make a path outside this fixture writable while handling a
            # cleanup error (including the parent of the fixture root itself).
            raise OSError(f"Refusing cleanup chmod outside fixture root: {path}") from error
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                action(path)
                return
            mode = stat.S_IRWXU if stat.S_ISDIR(info.st_mode) else stat.S_IRUSR | stat.S_IWUSR
            os.chmod(path, mode)
            parent = path.parent
            if parent != root:
                parent.relative_to(root)
                os.chmod(parent, stat.S_IRWXU)
            action(path)
        except OSError:
            # shutil.rmtree will report this as unresolved; do not broaden the
            # retry to any directory outside the temporary fixture.
            raise

    shutil.rmtree(root, onerror=make_writable_and_retry)


def main() -> None:
    docker("image", "inspect", IMAGE)
    root = Path(tempfile.mkdtemp(prefix="tessera-development-e2e-")).resolve()
    receipt = None
    try:
        state, _, _, _, token, grant = prepare(
            root, OUTPUT_PROBE, output_paths=["result.txt"], max_output_bytes=1024
        )
        credential = root / "worker.credential"
        credential.write_text(token)
        credential.chmod(0o600)
        worker_root = root / "worker"
        outputs = root / "development-outputs"
        worker_root.mkdir(mode=0o700)
        outputs.mkdir(mode=0o700)
        with serve(state) as origin:
            result = DevelopmentWorkerService(
                DevelopmentWorkerConfig(
                    origin,
                    grant["id"],
                    credential,
                    worker_root,
                    outputs,
                    IMAGE,
                    poll_seconds=0.1,
                    max_ticks=100,
                ),
                provider=FakeProvider(),
            ).run()
        if result.get("state") != "exited" or result.get("quarantined") is not True:
            raise RuntimeError("Development worker did not reach confirmed quarantined exit.")
        receipt = result["receipt"]
        if (
            receipt.get("status") != "quarantined"
            or receipt.get("classification") != "unclassified"
        ):
            raise RuntimeError("Development worker output left quarantine.")
        if list(outputs.glob("development-only-cpu-outputs/workspaces/*")):
            raise RuntimeError("Development workspace was not reclaimed after quarantine transfer.")
        with sqlite3.connect(state / "tessera.sqlite3") as connection:
            operations = connection.execute(
                "SELECT operation_id, observed_state FROM external_operations"
            ).fetchall()
            receipts = connection.execute(
                "SELECT operation_id, provider_kind, observed_state "
                "FROM external_operation_receipts"
            ).fetchall()
        if operations != [(receipt["operation_id"], "exited")]:
            raise RuntimeError(
                "Development fixture did not retain one resolved operation identity."
            )
        if receipts != [(receipt["operation_id"], "fake", "exited")]:
            raise RuntimeError("Development fixture did not retain fake-provider receipt evidence.")
        print(
            json.dumps(
                {
                    "fixture": "development-e2e",
                    "operation_id": receipt["operation_id"],
                    "quarantined": True,
                },
                sort_keys=True,
            )
        )
    finally:
        if receipt is not None:
            docker("rm", "--force", f"tessera-op-{receipt['operation_id']}")
            try:
                _remove_fixture_root(root)
            except OSError as error:
                print(
                    f"Development fixture cleanup unresolved at {root}: {error}.", file=sys.stderr
                )
        else:
            print(f"Development fixture cleanup unresolved at {root}.", file=sys.stderr)


if __name__ == "__main__":
    main()
