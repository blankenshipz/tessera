"""Opt-in genuine reasoning check; never part of default pytest/CI.

Requires secure runtime profile and explicit included-OAuth execution admission.
The receipt contains only state/usage, not provider messages or hidden reasoning.
"""

import argparse
import json
from pathlib import Path

from tessera_agent.remote_reasoning_runtime import main as runtime_main
from tessera_agent.remote_reasoning_store import ReasoningStore, private_file


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--execute-included-oauth", action="store_true", required=True)
    args = parser.parse_args(argv)
    profile = json.loads(private_file(args.profile))
    # This check deliberately limits one run, with no automatic retry/replacement.
    if (
        profile["max_steps"] > 3
        or profile["max_duration_seconds"] > 120
        or profile["max_tokens"] > 8192
    ):
        parser.error("Live check requires <=3 steps, <=120 seconds and <=8192 observed tokens.")
    previous = ReasoningStore(args.state_dir)
    try:
        try:
            row = previous.get(profile["run_id"])
        except ValueError:
            row = None
        if row and row["state"] != "running":
            parser.error(
                "A terminal/prior run is not fresh live evidence; reconcile it separately."
            )
    finally:
        previous.close()
    result = runtime_main(
        [
            "run",
            "--profile",
            str(args.profile),
            "--state-dir",
            str(args.state_dir),
            "--prompt-file",
            str(args.prompt_file),
        ]
    )
    store = ReasoningStore(args.state_dir)
    try:
        row = store.get(profile["run_id"])
        passed = result == 0 and row["tokens"] > 0 and row["cost"] == 0
        print(
            json.dumps(
                {
                    "check": "remote-runtime",
                    "mode": "live",
                    "passed": passed,
                    "state": row["state"],
                    "observed_tokens": row["tokens"],
                    "observed_cost_usd": row["cost"],
                    "independent_host_verified": False,
                    "provider_termination_verified": False,
                }
            )
        )
        return 0 if passed else 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
