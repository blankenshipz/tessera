"""First-call readiness check (no model call, no key value ever read).

The Anthropic profile and credential files are the synthetic ones of the credential-reference
tests. The API key file holds a clearly synthetic marker, and the tests prove the checker never
opens it and never prints it.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_reasoning_credential_references import (  # noqa: F401 - anthropic is a fixture
    API_KEY,
    REF_LABEL,
    anthropic,
    write_private,
)

from tessera_agent import first_call_readiness as fcr
from tessera_control.model_spend import create_model_spend_schema

PROJECT = "proj1"
KEY_FILE = "anthropic-api-key"
EXPECTED = (
    "ready except key: install the API key with tessera-service-credential install, "
    "then start the service"
)


@pytest.fixture
def world(anthropic, tmp_path):  # noqa: F811 - imported fixture
    root, profile, env = anthropic
    profile = {**profile, "model_spend": {"project_id": PROJECT}}
    write_private(root / "profile", json.dumps(profile))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    control = tmp_path / "control"
    control.mkdir()
    database = sqlite3.connect(control / "tessera.sqlite3")
    create_model_spend_schema(database)
    database.execute(
        "INSERT INTO model_spend_limits VALUES ('org1', ?, 'anthropic', 20000000, 90000000, 1, "
        "'p', 'now')",
        (PROJECT,),
    )
    database.commit()
    database.close()
    return {
        "root": root,
        "profile": profile,
        "state": state,
        "control": control,
        "environment": {"PATH": "/usr/bin"},
        "args": {
            "profile_path": root / "profile",
            "credentials_directory": root,
            "state_dir": state,
            "environment": {"PATH": "/usr/bin"},
            "control_state_dir": control,
            "control_url": "http://control.invalid",
            "opener": lambda method, url, timeout: 200 if url.endswith("/ready") else 401,
        },
    }


def run(world, **override):
    return fcr.run_checks(**{**world["args"], **override})


def status(report, name):
    return next(c for c in report["checks"] if c["name"] == name)["status"]


def test_everything_in_place_is_ready(world):
    report = run(world)
    assert report["overall"] == "ready", report["checks"]
    assert [c["name"] for c in report["checks"]] == [
        "environment",
        "profile",
        "credential_reference",
        "api_key_file",
        "egress_allowlist",
        "receipts_store",
        "control_slices",
        "spend_caps",
    ]
    assert all(c["status"] == "ok" for c in report["checks"])
    assert report["key"] == {"present": True, "size_ok": True}


def test_only_the_key_file_missing_is_ready_except_key(world):
    (world["root"] / KEY_FILE).unlink()
    report = run(world)
    assert report["overall"] == "ready_except_key"
    assert status(report, "api_key_file") == "missing"
    assert report["key"] == {"present": False, "size_ok": None}
    assert report["summary"] == EXPECTED
    assert (
        "tessera-service-credential install"
        in next(c for c in report["checks"] if c["name"] == "api_key_file")["fix"]
    )


def test_a_missing_key_plus_any_other_gap_is_not_ready(world):
    (world["root"] / KEY_FILE).unlink()
    (world["root"] / "model-credential").unlink()
    report = run(world)
    assert report["overall"] == "not_ready"
    assert status(report, "credential_reference") == "missing"


def test_the_key_file_is_never_opened_or_printed(world, monkeypatch, capsys):
    key_path = str(world["root"] / KEY_FILE)

    def guard(real):
        def guarded(first, *args, **kwargs):
            assert key_path not in str(first), "the key file must never be opened"
            return real(first, *args, **kwargs)

        return guarded

    def guard_path(real):
        def guarded(self, *args, **kwargs):
            assert key_path not in str(self), "the key file must never be opened"
            return real(self, *args, **kwargs)

        return guarded

    # Every way the standard library can read a file: builtins.open, io.open, os.open and the
    # pathlib readers. Only lstat is allowed on the key file.
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(os, "open", guard(os.open))
    for name in ("read_bytes", "read_text", "open"):
        monkeypatch.setattr(Path, name, guard_path(getattr(Path, name)))
    report = run(world)
    monkeypatch.undo()
    assert report["overall"] == "ready"
    assert API_KEY not in json.dumps(report) and API_KEY[:8] not in json.dumps(report)
    code = fcr.main(
        [
            "check",
            "--profile",
            str(world["root"] / "profile"),
            "--credentials-directory",
            str(world["root"]),
            "--state-dir",
            str(world["state"]),
        ]
    )
    out = capsys.readouterr()
    assert code in {0, 10, 1, 11}
    assert API_KEY not in out.out + out.err


def test_a_forbidden_environment_variable_fails_even_when_empty(world):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"):
        report = run(world, environment={name: ""})
        assert report["overall"] == "not_ready" and status(report, "environment") == "fail"
        assert name in next(c for c in report["checks"] if c["name"] == "environment")["detail"]


def test_an_invalid_profile_fails_and_dependent_checks_are_unchecked(world):
    broken = {k: v for k, v in world["profile"].items() if k != "limits"}
    write_private(world["root"] / "profile", json.dumps(broken))
    report = run(world)
    assert status(report, "profile") == "fail"
    assert status(report, "credential_reference") == "unchecked"
    assert status(report, "spend_caps") == "unchecked"
    assert report["overall"] == "not_ready"


def test_a_missing_profile_fails(world):
    (world["root"] / "profile").unlink()
    assert status(run(world), "profile") == "fail"


def test_the_expensive_tier_needs_its_approval_id(world):
    fable = {**world["profile"], "model": {"providerID": "anthropic", "id": "claude-fable-x"}}
    write_private(world["root"] / "profile", json.dumps(fable))
    report = run(world)
    assert status(report, "profile") == "fail"
    write_private(
        world["root"] / "profile", json.dumps({**fable, "fable_approval_id": "approval1"})
    )
    assert status(run(world), "profile") == "ok"


def test_a_reference_file_that_differs_from_the_profile_fails(world):
    write_private(world["root"] / "model-credential", "anthropic-api-key://owner1/other\n")
    report = run(world)
    assert status(report, "credential_reference") == "fail"
    assert report["overall"] == "not_ready"
    assert REF_LABEL not in json.dumps(report)


def test_a_credentials_directory_that_is_not_private_fails(world):
    world["root"].chmod(0o777)
    report = run(world)
    assert status(report, "credential_reference") == "fail"
    assert status(report, "api_key_file") == "unchecked"


@pytest.mark.parametrize(
    ("mode", "size", "expected"),
    [(0o640, 20, False), (0o600, 600, False), (0o600, 513, True), (0o600, 512, True)],
)
def test_an_unsafe_key_file_fails_by_stat_alone(world, mode, size, expected):
    path = world["root"] / KEY_FILE
    path.write_bytes(b"k" * size)
    path.chmod(mode)
    report = run(world)
    ok = status(report, "api_key_file") == "ok"
    assert ok is (mode == 0o600 and expected)
    assert report["key"]["present"] is True
    assert (report["overall"] == "ready") is ok


def test_an_empty_key_file_fails(world):
    path = world["root"] / KEY_FILE
    path.write_bytes(b"")
    path.chmod(0o600)
    assert status(run(world), "api_key_file") == "fail"


def test_a_symlinked_key_file_fails(world, tmp_path):
    real = tmp_path / "elsewhere"
    real.write_bytes(b"synthetic")
    real.chmod(0o600)
    (world["root"] / KEY_FILE).unlink()
    (world["root"] / KEY_FILE).symlink_to(real)
    assert status(run(world), "api_key_file") == "fail"


def test_a_widened_egress_allowlist_fails_naming_the_extra_host(world, monkeypatch):
    monkeypatch.setattr(
        fcr,
        "model_runtime_allowlist",
        lambda: frozenset({("api.anthropic.com", 443), ("evil.example", 443)}),
    )
    report = run(world)
    assert status(report, "egress_allowlist") == "fail"
    assert (
        "evil.example:443"
        in next(c for c in report["checks"] if c["name"] == "egress_allowlist")["detail"]
    )


def test_an_egress_check_that_allows_other_hosts_fails(world, monkeypatch):
    monkeypatch.setattr(fcr, "model_runtime_egress_allowed", lambda *a: True)
    assert status(run(world), "egress_allowlist") == "fail"


def test_the_receipts_store_must_exist_be_private_and_writable(world, tmp_path):
    assert status(run(world, state_dir=tmp_path / "nope"), "receipts_store") == "fail"
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    assert status(run(world, state_dir=shared), "receipts_store") == "fail"
    a_file = tmp_path / "file"
    a_file.write_text("x")
    assert status(run(world, state_dir=a_file), "receipts_store") == "fail"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_read_only_state_directory_fails(world):
    world["state"].chmod(0o500)
    try:
        assert status(run(world), "receipts_store") == "fail"
    finally:
        world["state"].chmod(0o700)


def test_the_probe_leaves_nothing_behind_and_never_creates_the_journal(world):
    run(world)
    assert list(world["state"].iterdir()) == []


@pytest.mark.parametrize(
    ("ready", "route", "expected"),
    [(200, 401, "ok"), (200, 403, "ok"), (200, 405, "ok"), (200, 404, "fail"), (503, 401, "fail")],
)
def test_control_slices_distinguish_a_missing_ledger_route(world, ready, route, expected):
    opener = lambda method, url, timeout: ready if url.endswith("/ready") else route  # noqa: E731
    assert status(run(world, opener=opener), "control_slices") == expected


def test_control_slices_fail_when_unreachable_or_misconfigured(world):
    def down(method, url, timeout):
        raise OSError("refused")

    assert status(run(world, opener=down), "control_slices") == "fail"
    assert status(run(world, control_url="ftp://x"), "control_slices") == "fail"
    assert status(run(world, control_url="http://user:pw@host"), "control_slices") == "fail"
    assert status(run(world, control_url=None), "control_slices") == "unchecked"


def test_the_control_probe_sends_no_credential(world):
    seen = []

    def opener(method, url, timeout):
        seen.append((method, url, timeout))
        return 200 if url.endswith("/ready") else 401

    run(world, opener=opener)
    assert seen == [
        ("GET", "http://control.invalid/health/ready", 3),
        ("GET", "http://control.invalid/model-spend/limits", 3),
    ]


def _limits(world, rows):
    database = sqlite3.connect(world["control"] / "tessera.sqlite3")
    database.execute("DELETE FROM model_spend_limits")
    for provider, per_call, daily in rows:
        database.execute(
            "INSERT INTO model_spend_limits VALUES ('org1', ?, ?, ?, ?, 1, 'p', 'now')",
            (PROJECT, provider, per_call, daily),
        )
    database.commit()
    database.close()


def test_the_default_limit_row_covers_the_provider(world):
    _limits(world, [("*", 10_000_000, 50_000_000)])
    assert status(run(world), "spend_caps") == "ok"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [("anthropic", 0, 0)],
        [("anthropic", 5_000_000, 1_000_000)],
        [("anthropic", 9_999_999, 50_000_000)],  # below the profile's 10-cent per-call bound
    ],
)
def test_a_missing_or_unusable_limit_fails_before_the_first_reservation(world, rows):
    _limits(world, rows)
    report = run(world)
    assert status(report, "spend_caps") == "fail"
    assert report["overall"] == "not_ready"


def test_a_database_older_than_the_ledger_fails(world):
    database = sqlite3.connect(world["control"] / "tessera.sqlite3")
    database.execute("DROP TABLE model_spend_limits")
    database.commit()
    database.close()
    assert status(run(world), "spend_caps") == "fail"


def test_a_missing_control_database_fails_and_nothing_is_created(world, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert status(run(world, control_state_dir=empty), "spend_caps") == "fail"
    assert list(empty.iterdir()) == []


def test_the_control_database_is_opened_read_only(world):
    path = world["control"] / "tessera.sqlite3"
    before = path.read_bytes()
    run(world)
    assert path.read_bytes() == before


def test_without_a_project_the_cap_check_is_unchecked_not_failed(world):
    without = {k: v for k, v in world["profile"].items() if k != "model_spend"}
    write_private(world["root"] / "profile", json.dumps(without))
    report = run(world)
    assert status(report, "spend_caps") == "unchecked" and report["overall"] == "not_fully_checked"
    assert report["unchecked"] == ["spend_caps"]
    assert "spend_caps" in report["summary"] and "not a pass" in report["summary"]
    assert status(run(world, project_id=PROJECT), "spend_caps") == "ok"


def test_no_control_inputs_leave_those_checks_unchecked(world):
    report = run(world, control_state_dir=None, control_url=None)
    assert report["unchecked"] == ["control_slices", "spend_caps"]
    # Nothing failed, but an unverified spend limit is what makes the first call refuse after the
    # key is installed: this is never ready, and the summary does not say every check passed.
    assert report["overall"] == "not_fully_checked"
    assert "every check passed" not in report["summary"]
    assert "control_slices, spend_caps" in report["summary"]


def test_unchecked_checks_block_ready_except_key_too(world):
    (world["root"] / KEY_FILE).unlink()  # only the key is missing, and two checks could not run
    report = run(world, control_state_dir=None, control_url=None)
    assert report["overall"] == "not_fully_checked" and report["overall"] != "ready_except_key"
    assert status(report, "api_key_file") == "missing"
    assert report["unchecked"] == ["control_slices", "spend_caps"]
    # With the control inputs given, the same deployment is ready except the key.
    assert run(world)["overall"] == "ready_except_key"


def test_a_failure_is_reported_before_an_unverified_check(world):
    (world["root"] / "profile").unlink()
    report = run(world, control_state_dir=None, control_url=None)
    assert report["overall"] == "not_ready"


def _argv(world, *extra):
    return [
        "check",
        "--profile",
        str(world["root"] / "profile"),
        "--credentials-directory",
        str(world["root"]),
        "--state-dir",
        str(world["state"]),
        *extra,
    ]


def test_exit_codes_and_the_final_text(world, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Both control inputs are needed to be ready; the probe is stubbed (no network in tests).
    monkeypatch.setattr(fcr, "_default_opener", world["args"]["opener"])
    extra = [
        "--control-state-dir",
        str(world["control"]),
        "--control-url",
        "http://control.invalid",
    ]
    assert fcr.main(_argv(world, *extra)) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["overall"] == "ready" and out.err.startswith("ready:")
    (world["root"] / KEY_FILE).unlink()
    assert fcr.main(_argv(world, *extra)) == 10
    assert capsys.readouterr().err.strip() == EXPECTED
    (world["root"] / "profile").unlink()
    assert fcr.main(_argv(world, *extra)) == 1
    assert capsys.readouterr().err.startswith("not ready")


def test_unchecked_checks_exit_non_zero_with_their_own_status(world, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = fcr.main(_argv(world))  # no --control-url and no --control-state-dir
    out = capsys.readouterr()
    assert code == fcr.EXIT_NOT_FULLY_CHECKED == 11
    assert code not in {
        fcr.EXIT_READY,
        fcr.EXIT_READY_EXCEPT_KEY,
        fcr.EXIT_NOT_READY,
        fcr.EXIT_USAGE,
    }
    assert json.loads(out.out)["overall"] == "not_fully_checked"
    assert out.err.startswith("not fully checked") and "every check passed" not in out.err
    (world["root"] / KEY_FILE).unlink()
    assert fcr.main(_argv(world)) == 11  # still not ready-except-key (10) while unverified
    capsys.readouterr()


def test_the_cli_environment_is_the_process_environment(world, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert fcr.main(_argv(world)) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_a_usage_error_exits_2(capsys):
    with pytest.raises(SystemExit) as stop:
        fcr.main(["check"])
    assert stop.value.code == 2
    with pytest.raises(SystemExit) as stop:
        fcr.main([])
    assert stop.value.code == 2
