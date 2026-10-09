"""Init-container copy with exact hashes; projected symlinks never followed.

Kubernetes projected volumes use symlinks by design. Open the pinned ..data
directory once, then read fixed regular part files using no-follow directory FDs.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from tessera_control.dataset_snapshots import _read_file, load_dataset_snapshot
from tessera_worker.dataset_inputs import validate_descriptor


def materialize(source, destination, descriptor, operation_id):
    validate_descriptor(descriptor, operation_id)
    # Resolve only Kubernetes' fixed projection link; chunks are all immutable.
    target = os.readlink(source / "..data")
    if "/" in target or target in {".", ".."} or not target.startswith(".."):
        raise ValueError("Invalid Kubernetes projection")
    root = source / target
    files = {"dataset.csv": bytearray(), "dataset-manifest.json": bytearray()}
    for part in descriptor["parts"]:
        content = _read_file(root, part["key"], part["size"])
        if len(content) != part["size"] or hashlib.sha256(content).hexdigest() != part["sha256"]:
            raise ValueError("Chunk changed or incomplete")
        files[part["file"]].extend(content)
    for name, content in files.items():
        fd = os.open(
            destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444
        )
        with os.fdopen(fd, "wb") as output:
            output.write(content)
    load_dataset_snapshot(destination, expected_manifest_digest=descriptor["manifest_digest"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    materialize(
        Path("/dataset-parts"), Path("/dataset"), json.loads(args.descriptor), args.operation_id
    )
