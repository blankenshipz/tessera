"""deploy-upgrade refuses to restart while a Salad attempt is executing (v0.104.0).

A restart loses a running route's child, so the attempt would end ``unknown``; the operator may
override with ``--allow-executing-salad``. Fakes only: no service is touched."""

from __future__ import annotations

import json
import sqlite3
from argparse import ArgumentParser

import pytest
from test_deploy_upgrade import Fakes, make_plan, run

from tessera_control import deploy_upgrade
from tessera_control.cli import main
from tessera_control.deploy_upgrade_cli import add_parser
from tessera_control.store import DATABASE_NAME


def add_attempt(state, phase, attempt_id="att_1", route_id="route_1"):
    with sqlite3.connect(state / DATABASE_NAME) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO campaign_salad_attempts(attempt_id, phase, route_id, revision, "
            "updated_at) VALUES (?, ?, ?, 1, '2026-01-01T00:00:00Z')",
            (attempt_id, phase, route_id),
        )


def blocking(document):
    return [entry for entry in document["preflight"]["blocking"] if entry.startswith("salad.")]


def test_an_executing_attempt_blocks_a_dry_run(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    add_attempt(state, "executing")
    fakes = Fakes()
    document = run(plan, fakes, execute=False)
    assert document["result"] != "ok"
    assert len(blocking(document)) == 1
    assert blocking(document)[0].startswith("salad.executing (1 ")
    assert document["salad_executing"] == 1
    assert fakes.calls == []


def test_an_executing_attempt_stops_execute_before_any_stop_or_restart(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    add_attempt(state, "executing")
    fakes = Fakes()
    document = run(plan, fakes, execute=True)
    assert document["result"] == "failed" and document["failed_step"] == "preflight"
    assert blocking(document)
    assert fakes.calls == []  # nothing stopped, nothing restarted
    assert document["salad_executing"] == 1


def test_the_override_makes_the_guard_a_warning_and_the_upgrade_runs(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    add_attempt(state, "executing")
    fakes = Fakes()
    document = run(plan, fakes, execute=True, allow_executing_salad=True)
    assert document["result"] == "ok", document["steps"]
    assert blocking(document) == []
    assert any("Salad attempt(s) executing" in w for w in document["warnings"])
    assert document["salad_executing"] == 1
    assert ["restart-control"] in fakes.calls and ["stop-executor"] in fakes.calls


def test_the_override_also_applies_to_a_dry_run(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    add_attempt(state, "executing")
    document = run(plan, Fakes(), execute=False, allow_executing_salad=True)
    assert document["result"] == "ok" and blocking(document) == []
    assert any("executing" in w for w in document["warnings"])


def test_a_route_started_attempt_does_not_block(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    add_attempt(state, "route_started")
    fakes = Fakes()
    document = run(plan, fakes, execute=True)
    assert document["result"] == "ok", document["steps"]
    assert blocking(document) == []
    assert document["salad_executing"] == 0
    assert not any("executing" in w for w in document.get("warnings", []))
    assert ["restart-control"] in fakes.calls


def test_the_count_is_exact_and_zero_without_rows_or_a_database(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    assert deploy_upgrade.executing_salad_attempts(state) == 0
    add_attempt(state, "executing", "att_1", "route_1")
    add_attempt(state, "executing", "att_2", "route_2")
    add_attempt(state, "unknown", "att_3", "route_3")
    assert deploy_upgrade.executing_salad_attempts(state) == 2
    assert deploy_upgrade.executing_salad_attempts(tmp_path / "missing") == 0


def test_the_command_refuses_then_accepts_the_flag(tmp_path, capsys, monkeypatch):
    _plan, path, state = make_plan(tmp_path, with_config=False)
    add_attempt(state, "executing")
    base = ["tessera", "deploy-upgrade", "--plan", str(path), "--format", "json"]
    monkeypatch.setattr("sys.argv", base)
    assert main() != 0
    assert "salad.executing" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", [*base, "--allow-executing-salad"])
    assert main() == 0
    document = json.loads(capsys.readouterr().out)
    assert document["salad_executing"] == 1 and document["warnings"]


@pytest.mark.parametrize("flags,expected", [([], False), (["--allow-executing-salad"], True)])
def test_the_flag_parses_and_defaults_off(flags, expected):
    parser = ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    arguments = parser.parse_args(["deploy-upgrade", "--plan", "/x", *flags])
    assert arguments.allow_executing_salad is expected
