"""Opt-in monitor conformance. Default does not inspect Docker or create state."""

import argparse
import json
from pathlib import Path

from tessera_monitor.conformance import prerequisites, run_once


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-prerequisites", action="store_true")
    modes.add_argument("--run", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--allow-host-socket", action="store_true")
    args = parser.parse_args(argv)
    if args.check_prerequisites:
        result = prerequisites()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["state"] == "available" else 1
    if not args.run:
        print(json.dumps(dict(state="disabled", effects=0, activation="blocked")))
        return 0
    if not (args.image and args.evidence_dir and args.allow_host_socket):
        parser.error("--run requires --image, fresh --evidence-dir and --allow-host-socket")
    try:
        result = run_once(args.evidence_dir, args.image)
    except Exception:
        result = dict(state="unknown", activation="blocked", reason="retain_evidence_do_not_retry")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
