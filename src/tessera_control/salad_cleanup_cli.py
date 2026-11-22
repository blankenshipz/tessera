"""Dry-run report of stopped Salad groups Tessera could clean up.

Reads Tessera's own route records from a state directory opened read-only. It makes
no provider call, reads no credential and changes nothing. It is a local operator
tool and reads every organization in the state directory. There is deliberately no
apply option: applying (deleting) is not available in this milestone, because live
group deletion has no activation gate.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from .salad_group_cleanup import (
    DEFAULT_MIN_AGE_SECONDS,
    build_plan,
    read_route_snapshots,
)
from .store import DATABASE_NAME

EXIT_USAGE = 64


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _age(seconds):
    if seconds is None:
        return "unknown"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d{hours}h{minutes}m" if days else f"{hours}h{minutes}m"


def render(plan):
    lines = []
    eligible = [d for d in plan["decisions"] if d["decision"] == "eligible"]
    skipped = [d for d in plan["decisions"] if d["decision"] != "eligible"]
    lines.append(f"Eligible stopped groups (min age {plan['min_age_seconds']}s): {len(eligible)}")
    for d in eligible:
        lines.append(f"  {str(d['operation_id'])[:8]}  {d['group']}  age {_age(d['age_seconds'])}")
    lines.append(f"Skipped routes: {len(skipped)}")
    for d in skipped:
        lines.append(f"  {str(d['operation_id'])[:8]}  {d['group']}  {d['reason']}")
    counts = plan["counts"]
    lines.append(
        f"Counts: considered={counts['considered']} eligible={counts['eligible']} "
        f"skipped={counts['skipped']}"
    )
    lines.append(plan["statement"])
    lines.append(plan["liability"])
    return "\n".join(lines)


def main(argv=None):
    parser = _Parser(prog="tessera-salad-cleanup", description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--min-age-seconds", type=int, default=DEFAULT_MIN_AGE_SECONDS)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.min_age_seconds < 0:
        parser.error("--min-age-seconds must be non-negative")
    database = args.state_dir / DATABASE_NAME
    if not args.state_dir.is_dir() or not database.is_file():
        parser.error("state directory has no Tessera database (it is never created)")
    try:
        snapshots = read_route_snapshots(database)
    except Exception:
        parser.error("state database could not be read read-only")
    plan = build_plan(snapshots, now=time.time(), min_age_seconds=args.min_age_seconds)
    print(json.dumps(plan, sort_keys=True, indent=2) if args.as_json else render(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
