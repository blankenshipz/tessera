"""Opt-in platform validation runner (not collected by pytest).

    .venv/bin/python scripts/platform_validation/run.py [--only NAME] [--keep]

Discovers check_*.py beside this file and runs each in a fresh Context (own temp state dir
and real `tessera serve` process), printing JSON lines then a table. A check marked
`OPT_IN = True` (the campaign check, which needs real Docker) runs only with `--only` naming
it or with `--include-opt-in`.
Exit 1 on any FAIL. GAP is reported honestly and does not fail the run.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from platform_validation.common import Context, Result, scrub  # noqa: E402


def _opt_in(path: Path) -> bool:
    """A check declares ``OPT_IN = True`` at module level; read without importing it."""
    return any(line.strip() == "OPT_IN = True" for line in path.read_text().splitlines())


def discover(only: str | None, include_opt_in: bool = False) -> list[Path]:
    paths = sorted(HERE.glob("check_*.py"))
    if only:
        return [p for p in paths if only in (p.stem, p.stem.removeprefix("check_"))]
    # Opt-in checks (real Docker campaigns) run only when named or with --include-opt-in.
    return [p for p in paths if include_opt_in or not _opt_in(p)]


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"platform_validation.{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_check(path: Path) -> list[Result]:
    name = path.stem.removeprefix("check_")
    ctx = Context.create()
    try:
        module = load(path)
        capability = getattr(module, "CAPABILITY", name)
        try:
            return list(module.run(ctx))
        except Exception as error:  # noqa: BLE001 - a crash is a FAIL, not a runner crash
            last = traceback.extract_tb(error.__traceback__)[-1]
            return [
                Result(
                    capability,
                    "check-crashed",
                    "FAIL",
                    f"{type(error).__name__}: {error} ({Path(last.filename).name}:{last.lineno})",
                )
            ]
    except Exception as error:  # noqa: BLE001
        return [Result(name, "check-load", "FAIL", f"{type(error).__name__}: {error}")]
    finally:
        ctx.cleanup()


def main() -> int:
    argp = argparse.ArgumentParser(description=__doc__)
    argp.add_argument("--only", help="check name, e.g. restart or check_restart")
    argp.add_argument("--include-opt-in", action="store_true", help="also run checks marked OPT_IN")
    arguments = argp.parse_args()
    paths = discover(arguments.only, arguments.include_opt_in)
    if not paths:
        print(f"no check matches {arguments.only!r}", file=sys.stderr)
        return 2
    results: list[Result] = []
    for path in paths:
        results.extend(run_check(path))
    for result in results:
        print(json.dumps(result.to_dict(), sort_keys=True))
    width = max(len(r.capability) for r in results) if results else 10
    print()
    for r in results:
        print(f"{r.status:<4}  {r.capability:<{width}}  {r.case}  -- {scrub(r.evidence, 110)}")
    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "PASS-WEAK", "FAIL", "GAP")}
    print(
        f"\nPASS={counts['PASS']} PASS-WEAK={counts['PASS-WEAK']} "
        f"FAIL={counts['FAIL']} GAP={counts['GAP']}"
    )
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
