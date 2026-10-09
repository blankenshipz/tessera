"""CLI output is evidence, not acceptance or authority."""

import argparse
import os
import signal
from pathlib import Path

from tessera_control.dataset_snapshots import canonical_bytes

from .workload import VARIANTS, run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--variant", required=True, choices=VARIANTS)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        result = run(
            args.snapshot_root, args.manifest_digest, args.variant, cancelled=lambda: stopped
        )
        exit_code = 0
    except Exception as error:
        result = {
            "version": "tessera-tabular-result-v1",
            "status": "cancelled" if isinstance(error, InterruptedError) else "inconclusive",
            "failure_category": type(error).__name__,
            "variant": args.variant,
            "dataset_manifest_digest": args.manifest_digest,
            "scientific_validation": "not-established",
            "disposition": "quarantine-human-review-required",
        }
        exit_code = 2
    encoded = canonical_bytes(result)
    if len(encoded) > 1024 * 1024:
        raise ValueError("Output bound exceeded")
    # Supervisor supplies a fresh owned output directory. Never overwrite evidence.
    fd = os.open(
        args.output_root / "result.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "wb") as output:
        output.write(encoded)
    print(result["status"], args.variant, flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
