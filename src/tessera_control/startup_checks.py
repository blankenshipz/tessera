"""Run each service's own ``--check-config`` under this release before a deploy stops anything.

``deploy-check`` (and ``deploy-upgrade``'s preflight, which shares its checks) used to re-check the
operator files with their own, weaker readers: a providers file that ``tessera serve`` would refuse
passed the preflight. Here the check is the startup path itself. Each service is started in
``--check-config`` mode with its real arguments, under the interpreter running this command (run
the deploy check from the release you are about to install), and a fresh process so nothing it
registers leaks into this one. ``--check-config`` parses and validates exactly what startup does
and exits without binding, without opening the control database for writing and without contacting
anything (ADR 0214).

The exit code is the contract (``exit_codes``): 0 startup would go on, 2 startup would refuse the
configuration, anything else the check itself failed. A successful check prints one
``tessera.config-check.v1`` JSON line naming the route widths it read, so the widths are compared
from the same parsed values the services start with: control's ``salad_max_concurrent_routes``
must not exceed the executors' ``max_concurrent_paid_routes``, which must not exceed the
guardians' ``max_open_billable_frames``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import exit_codes
from .deploy_check import Finding

AREA = "startup"
# Program name -> the module run with ``python -m`` and the arguments placed before the operator's.
PROGRAMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "serve": ("tessera_control.cli", ("serve", "--check-config")),
    "salad-executor": ("tessera_control.campaign_salad_executor", ("--check-config",)),
    "salad-guardian": ("tessera_control.salad_safety_service", ("--check-config",)),
}
# The width each program reports, in the order they must not decrease.
WIDTH_KEYS = {
    "serve": "salad_max_concurrent_routes",
    "salad-executor": "max_concurrent_paid_routes",
    "salad-guardian": "max_open_billable_frames",
}
DEFAULT_TIMEOUT_SECONDS = 60
MAX_MESSAGE_CHARS = 400
MAX_ARGS = 64

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class StartupCheck:
    """One service's startup arguments, as its unit passes them (without ``--check-config``)."""

    program: str
    args: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.program not in PROGRAMS:
            raise ValueError(f"unknown startup check program: {self.program}")
        if len(self.args) > MAX_ARGS or any("\0" in item for item in self.args):
            raise ValueError("startup check arguments are malformed")
        if "--check-config" in self.args:
            raise ValueError("give the service's own arguments; --check-config is added")


def command(check: StartupCheck, python: str | None = None) -> list[str]:
    module, prefix = PROGRAMS[check.program]
    return [python or sys.executable, "-m", module, *prefix, *check.args]


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:MAX_MESSAGE_CHARS]
    return ""


def _report(stdout: str) -> dict[str, Any] | None:
    """The last ``tessera.config-check.v1`` line on stdout, if any."""
    for line in reversed((stdout or "").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("schema") == exit_codes.CONFIG_CHECK_SCHEMA:
            return value
    return None


def run_check(
    check: StartupCheck,
    label: str,
    *,
    python: str | None = None,
    runner: Runner = subprocess.run,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[Finding, dict[str, Any] | None]:
    """Run one ``--check-config``; the finding and, when it passed, its parsed report."""
    check_id = f"startup.{check.program}.{label}"
    detail: dict[str, Any] = {"program": check.program}
    try:
        result = runner(
            command(check, python),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Finding(
            check_id, AREA, "fail", f"{check.program} --check-config timed out.", detail
        ), None
    except OSError:
        return Finding(
            check_id, AREA, "fail", f"{check.program} --check-config could not be started.", detail
        ), None
    detail["exit_code"] = result.returncode
    if result.returncode == exit_codes.CONFIG_REFUSED:
        message = _first_line(result.stderr) or _first_line(result.stdout) or "no message"
        return Finding(
            check_id,
            AREA,
            "fail",
            f"{check.program} startup would refuse its configuration: {message}",
            detail,
        ), None
    report = _report(result.stdout) if result.returncode == exit_codes.OK else None
    if report is None or report.get("status") != "ok" or report.get("program") != check.program:
        message = _first_line(result.stderr)
        return Finding(
            check_id,
            AREA,
            "fail",
            f"{check.program} --check-config did not complete"
            + (f": {message}" if message else "."),
            detail,
        ), None
    widths = report.get("widths") if isinstance(report.get("widths"), dict) else {}
    notes = [str(n)[:MAX_MESSAGE_CHARS] for n in report.get("notes") or [] if isinstance(n, str)]
    detail.update({"widths": widths, "notes": notes})
    summary = f"{check.program} startup accepts its configuration under this release."
    if notes:
        summary += " Notes: " + "; ".join(notes) + "."
    return Finding(check_id, AREA, "ok", summary, detail), report


_UNSET = object()
_NOT_AN_INTEGER = object()


def _width(report: dict[str, Any], program: str) -> object:
    """The reported width, ``_UNSET`` when absent or null, ``_NOT_AN_INTEGER`` otherwise.

    A boolean is not an integer width here: ``True`` never counts as 1."""
    widths = report.get("widths")
    value = widths.get(WIDTH_KEYS[program]) if isinstance(widths, dict) else None
    if value is None:
        return _UNSET
    return value if type(value) is int else _NOT_AN_INTEGER


def width_finding(reports: Sequence[tuple[str, dict[str, Any]]]) -> Finding | None:
    """control ≤ executors ≤ guardians, from the widths the passing checks reported.

    With several executors or guardians their widths are summed: control's width bounds every
    Salad grant at once, and each executor (and guardian) admits its own routes (frames).
    Every level that is set is compared, so an executor wider than its guardians fails even
    when control's width is unset; a reported width that is not an integer fails. Only then
    does an unset level (control unbounded, or a service that reported no width) warn."""
    present = {program for program, _ in reports}
    if len(present & set(WIDTH_KEYS)) < 2:
        return None
    check_id = "startup.route_width"
    levels: dict[str, int | None] = {}
    invalid: list[str] = []
    for program in WIDTH_KEYS:
        values = [_width(report, program) for name, report in reports if name == program]
        if not values:
            continue
        if any(v is _NOT_AN_INTEGER for v in values):
            invalid.append(WIDTH_KEYS[program])
            levels[program] = None
        elif any(v is _UNSET for v in values):
            levels[program] = None
        else:
            levels[program] = sum(values)  # type: ignore[arg-type]
    detail: dict[str, Any] = {WIDTH_KEYS[program]: value for program, value in levels.items()}
    if invalid:
        detail["not_an_integer"] = invalid
        return Finding(
            check_id,
            AREA,
            "fail",
            "A reported route width is not an integer: " + ", ".join(invalid) + ".",
            detail,
        )
    ordered = [(program, value) for program, value in levels.items() if value is not None]
    for i, (lower_name, lower) in enumerate(ordered):
        for upper_name, upper in ordered[i + 1 :]:
            if lower > upper:  # type: ignore[operator]
                return Finding(
                    check_id,
                    AREA,
                    "fail",
                    f"Route widths disagree: {WIDTH_KEYS[lower_name]} ({lower}) is larger than "
                    f"{WIDTH_KEYS[upper_name]} ({upper}). Set control ≤ executor ≤ guardian.",
                    detail,
                )
    if levels.get("serve", 0) is None:
        return Finding(
            check_id,
            AREA,
            "warn",
            "Control's salad_max_concurrent_routes is unset (unbounded): control may grant more "
            "Salad stages than the executor runs at once.",
            detail,
        )
    unset = [WIDTH_KEYS[program] for program, value in levels.items() if value is None]
    if unset:
        return Finding(
            check_id,
            AREA,
            "warn",
            "No route width was reported for " + ", ".join(unset) + "; it was not compared.",
            detail,
        )
    return Finding(
        check_id, AREA, "ok", "Route widths agree (control ≤ executor ≤ guardian).", detail
    )


def check_all(
    checks: Sequence[StartupCheck],
    *,
    python: str | None = None,
    runner: Runner = subprocess.run,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[Finding]:
    """Every startup check in order, then the width cross-check when two levels passed."""
    findings: list[Finding] = []
    reports: list[tuple[str, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    for check in checks:
        counts[check.program] = counts.get(check.program, 0) + 1
        finding, report = run_check(
            check, str(counts[check.program]), python=python, runner=runner, timeout=timeout
        )
        findings.append(finding)
        if report is not None:
            reports.append((check.program, report))
    width = width_finding(reports)
    if width is not None:
        findings.append(width)
    return findings
