"""Read-only Linux inspection for an already-provisioned persistent output slot.

This does not start Docker, write a probe file, or provide exhaustion/restart
evidence. It only prints the existing contract when host inspection succeeds.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tessera_worker.persistent_output_probe import inspect_persistent_output_storage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, help="existing dedicated Linux output mount")
    arguments = parser.parse_args()
    contract = inspect_persistent_output_storage(arguments.path)
    if contract is None:
        print("Persistent output storage is not admissible from read-only host inspection.")
        return 1
    print(json.dumps(contract, sort_keys=True))
    print("Host facts passed; Docker exhaustion and restart evidence remains required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
