"""Run the whole default suite as concurrent file shards (no pytest-xdist needed).

Each shard is one `dev/pytest_shard.py` process with its own basetemp and log, so the
shards stay disjoint and a failure in one never hides another. Exit status is non-zero
if any shard fails. Example: `.venv/bin/python dev/pytest_parallel.py --jobs 8 -x`.

Each run's basetemps (`.pytest-tmp/parallel<N>-<run id>`) are removed when every shard is green
and kept, with their paths printed, when any shard fails. `--keep-basetemp` (or
`TESSERA_KEEP_BASETEMP=1`) keeps them either way.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Loaded by path: the cleanup helper is standard-library only, so this runner still works
# without the package's dependencies and never picks up another installed copy.
_SOURCE = Path(__file__).resolve().parents[1] / "src" / "tessera_control" / "scratch_cleanup.py"
_spec = importlib.util.spec_from_file_location("_tessera_scratch_cleanup", _SOURCE)
scratch_cleanup = importlib.util.module_from_spec(_spec)
sys.modules["_tessera_scratch_cleanup"] = scratch_cleanup
_spec.loader.exec_module(scratch_cleanup)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument(
        "--keep-basetemp",
        action="store_true",
        help="keep this run's pytest base directories even when every shard passes",
    )
    args, pytest_args = parser.parse_known_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    log_dir = args.log_dir or Path(tempfile.mkdtemp(prefix="tessera-pytest-"))
    log_dir.mkdir(parents=True, exist_ok=True)
    scratch = scratch_cleanup.Scratch(
        Path("."), keep=scratch_cleanup.keep_requested(args.keep_basetemp)
    )
    running = []
    for index in range(args.jobs):
        log = (log_dir / f"shard{index}.log").open("w")
        command = [
            sys.executable,
            "dev/pytest_shard.py",
            "--index",
            str(index),
            "--count",
            str(args.jobs),
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(scratch.path(f"parallel{index}")),
            *pytest_args,
        ]
        running.append((index, log, subprocess.Popen(command, stdout=log, stderr=log)))
    failed = []
    for index, log, process in running:
        code = process.wait()
        log.close()
        lines = (log_dir / f"shard{index}.log").read_text().strip().splitlines()
        print(f"shard {index}: exit {code}: {lines[-1] if lines else '(no output)'}")
        if code != 0:
            failed.append(index)
    print(f"logs: {log_dir}")
    scratch.finish(green=not failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
