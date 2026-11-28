"""deploy-check startup checks: exit-code contract and route-width cross-check (ADR 0214)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tessera_control import exit_codes, startup_checks
from tessera_control.startup_checks import StartupCheck


def _ok(program: str, **widths) -> str:
    return json.dumps(
        {
            "schema": exit_codes.CONFIG_CHECK_SCHEMA,
            "program": program,
            "status": "ok",
            "widths": widths,
            "notes": [],
        }
    )


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        code, stdout, stderr = self.results.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


def test_command_runs_the_module_with_check_config_under_this_interpreter():
    check = StartupCheck("serve", ("--state-dir", "/srv/state", "--port", "8000"))
    assert startup_checks.command(check) == [
        sys.executable,
        "-m",
        "tessera_control.cli",
        "serve",
        "--check-config",
        "--state-dir",
        "/srv/state",
        "--port",
        "8000",
    ]
    assert startup_checks.command(StartupCheck("salad-guardian", ("--run",)), "/py")[:4] == [
        "/py",
        "-m",
        "tessera_control.salad_safety_service",
        "--check-config",
    ]


@pytest.mark.parametrize(
    "program,args", [("nope", ()), ("serve", ("--check-config",)), ("serve", ("a\0b",))]
)
def test_malformed_checks_refuse(program, args):
    with pytest.raises(ValueError):
        StartupCheck(program, args)


def test_exit_2_is_a_config_refusal_with_the_startup_message():
    runner = FakeRunner([(2, "", "serve configuration error: bad providers\nmore\n")])
    [finding] = startup_checks.check_all([StartupCheck("serve", ())], runner=runner)
    assert finding.status == "fail"
    assert finding.check_id == "startup.serve.1"
    assert "would refuse its configuration: serve configuration error: bad providers" in (
        finding.summary
    )
    assert finding.detail["exit_code"] == 2
    assert runner.calls[0][1]["timeout"] == startup_checks.DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "code,stdout",
    [(1, ""), (0, "not json"), (0, _ok("salad-executor")), (78, _ok("serve"))],
)
def test_anything_but_a_matching_ok_report_fails(code, stdout):
    runner = FakeRunner([(code, stdout, "")])
    [finding] = startup_checks.check_all([StartupCheck("serve", ())], runner=runner)
    assert finding.status == "fail"
    assert "did not complete" in finding.summary


def test_timeout_and_unstartable_fail():
    def timeout(argv, **options):
        raise subprocess.TimeoutExpired(argv, 1)

    def missing(argv, **options):
        raise FileNotFoundError(argv[0])

    for runner in (timeout, missing):
        [finding] = startup_checks.check_all([StartupCheck("serve", ())], runner=runner)
        assert finding.status == "fail"


def _three(control, executor, guardian):
    return FakeRunner(
        [
            (0, _ok("serve", salad_max_concurrent_routes=control), ""),
            (0, _ok("salad-executor", max_concurrent_paid_routes=executor), ""),
            (0, _ok("salad-guardian", max_open_billable_frames=guardian), ""),
        ]
    )


CHECKS = [
    StartupCheck("serve", ()),
    StartupCheck("salad-executor", ()),
    StartupCheck("salad-guardian", ()),
]


@pytest.mark.parametrize(
    "control,executor,guardian,status",
    [
        (2, 2, 2, "ok"),
        (1, 2, 4, "ok"),
        (3, 2, 2, "fail"),
        (2, 2, 1, "fail"),
        (None, 2, 2, "warn"),
        # Control unset still compares the levels that are set: an executor wider than its
        # guardian is refused, and so is a control wider than the guardian.
        (None, 4, 1, "fail"),
        (None, 2, 1, "fail"),
        (3, None, 2, "fail"),
        (2, None, 2, "warn"),
    ],
)
def test_route_width_cross_check(control, executor, guardian, status):
    findings = startup_checks.check_all(CHECKS, runner=_three(control, executor, guardian))
    assert [f.status for f in findings[:3]] == ["ok", "ok", "ok"]
    width = findings[3]
    assert width.check_id == "startup.route_width"
    assert width.status == status
    if status == "fail":
        assert "Route widths disagree" in width.summary


@pytest.mark.parametrize(
    "control,executor,guardian,key",
    [
        (True, 2, 2, "salad_max_concurrent_routes"),
        (2, "2", 2, "max_concurrent_paid_routes"),
        (None, 2, 2.0, "max_open_billable_frames"),
        (1, False, 4, "max_concurrent_paid_routes"),
    ],
)
def test_a_width_that_is_not_an_integer_fails(control, executor, guardian, key):
    # A report can only carry one if a service let it through; a bool never counts as 1.
    findings = startup_checks.check_all(CHECKS, runner=_three(control, executor, guardian))
    width = findings[3]
    assert width.status == "fail"
    assert width.detail["not_an_integer"] == [key]
    assert "not an integer" in width.summary


def test_no_cross_check_with_one_level_or_a_failed_level():
    runner = FakeRunner([(0, _ok("serve", salad_max_concurrent_routes=2), "")])
    findings = startup_checks.check_all([StartupCheck("serve", ())], runner=runner)
    assert [f.check_id for f in findings] == ["startup.serve.1"]
    runner = FakeRunner(
        [
            (0, _ok("serve", salad_max_concurrent_routes=4), ""),
            (2, "", "salad executor refused"),
        ]
    )
    findings = startup_checks.check_all(CHECKS[:2], runner=runner)
    assert [f.status for f in findings] == ["ok", "fail"]


def test_several_executors_sum_their_widths():
    runner = FakeRunner(
        [
            (0, _ok("serve", salad_max_concurrent_routes=3), ""),
            (0, _ok("salad-executor", max_concurrent_paid_routes=2), ""),
            (0, _ok("salad-executor", max_concurrent_paid_routes=1), ""),
        ]
    )
    checks = [CHECKS[0], CHECKS[1], StartupCheck("salad-executor", ("--config", "/b"))]
    findings = startup_checks.check_all(checks, runner=runner)
    assert [f.check_id for f in findings] == [
        "startup.serve.1",
        "startup.salad-executor.1",
        "startup.salad-executor.2",
        "startup.route_width",
    ]
    assert findings[-1].status == "ok"


def test_deploy_check_cli_parses_unit_argument_strings():
    from tessera_control import deploy_check_cli

    parser = __import__("argparse").ArgumentParser()
    deploy_check_cli.add_parser(parser.add_subparsers(dest="command"))
    arguments = parser.parse_args(
        [
            "deploy-check",
            "--serve-args",
            "--state-dir /srv/state --port 8000 --campaign-providers '/etc/t/p q.json'",
            "--executor-args",
            "--config /etc/t/executor.json",
            "--guardian-args",
            "--run --config /etc/t/guardian.json",
        ]
    )
    checks = deploy_check_cli.startup_checks_from(arguments)
    assert [c.program for c in checks] == ["serve", "salad-executor", "salad-guardian"]
    assert checks[0].args[-1] == "/etc/t/p q.json"
    arguments.serve_args = "--port 'unterminated"
    with pytest.raises(ValueError):
        deploy_check_cli.startup_checks_from(arguments)
