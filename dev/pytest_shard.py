"""Run disjoint file-based pytest shards without collecting the entire suite."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


def shard_files(root: Path, index: int, count: int) -> list[Path]:
    if count < 1 or not 0 <= index < count:
        raise ValueError("shard index must be in [0, count)")
    return [
        path
        for path in sorted(root.rglob("test_*.py"))
        if int.from_bytes(
            hashlib.sha256(path.relative_to(root).as_posix().encode()).digest()[:8], "big"
        )
        % count
        == index
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--list", action="store_true")
    args, pytest_args = parser.parse_known_args()
    try:
        files = shard_files(Path("tests"), args.index, args.count)
    except ValueError as error:
        parser.error(str(error))
    if not files:
        parser.error("empty shard; refusing pytest's implicit full-suite fallback")
    if args.list:
        print("\n".join(map(str, files)))
        return 0
    return subprocess.call([sys.executable, "-m", "pytest", *map(str, files), *pytest_args])


if __name__ == "__main__":
    raise SystemExit(main())
