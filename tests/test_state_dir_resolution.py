"""Resolving the control state directory for owner-run commands: sources, disagreement, refusals."""

from __future__ import annotations

import json
import os
import sys

import pytest

from tessera_control import state_dir_resolution as sdr
from tessera_control.deploy_upgrade_plan import PLAN_SCHEMA
from tessera_control.store import initialize_state


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    initialize_state(path)
    return path


def plan_file(tmp_path, state_dir):
    document = {
        "schema": PLAN_SCHEMA,
        "state_dir": str(state_dir),
        "backup_dir": str(tmp_path / "backups"),
        "receipt_dir": str(tmp_path / "receipts"),
        "services": [
            {
                "name": "api",
                "restart_argv": ["restart-api"],
                "health": {"kind": "http", "url": "http://127.0.0.1:8080/health"},
            }
        ],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path


def test_an_explicit_directory_resolves(state):
    assert sdr.resolve_state_dir(state, environ={}) == state.resolve()


def test_the_environment_variable_resolves_when_nothing_explicit_is_given(state):
    assert sdr.resolve_state_dir(environ={sdr.ENV_VAR: str(state)}) == state.resolve()


def test_the_deploy_plan_resolves(tmp_path, state):
    assert sdr.resolve_state_dir(plan=plan_file(tmp_path, state), environ={}) == state.resolve()


def test_sources_that_agree_are_accepted_and_ones_that_disagree_are_refused(tmp_path, state):
    other = tmp_path / "other"
    initialize_state(other)
    plan = plan_file(tmp_path, state)
    agreed = sdr.resolve_state_dir(state, plan=plan, environ={sdr.ENV_VAR: str(state)})
    assert agreed == state.resolve()
    with pytest.raises(sdr.StateDirRefused, match="disagree") as caught:
        sdr.resolve_state_dir(state, plan=plan, environ={sdr.ENV_VAR: str(other)})
    # It names the sources, never a path.
    assert str(state) not in str(caught.value) and str(other) not in str(caught.value)


def test_no_source_is_a_refusal_that_says_how_to_give_one():
    with pytest.raises(sdr.StateDirRefused, match="--state-dir.*TESSERA_CONTROL_STATE_DIR.*--plan"):
        sdr.resolve_state_dir(environ={})


def test_a_directory_without_the_control_database_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(sdr.StateDirRefused, match="no database found"):
        sdr.resolve_state_dir(empty, environ={})
    with pytest.raises(sdr.StateDirRefused, match="no database found"):
        sdr.resolve_state_dir(tmp_path / "missing", environ={})


def test_a_loose_or_bad_plan_is_refused_without_echoing_it(tmp_path, state):
    plan = plan_file(tmp_path, state)
    plan.chmod(0o644)
    with pytest.raises(sdr.StateDirRefused, match="--plan is unusable"):
        sdr.resolve_state_dir(plan=plan, environ={})
    plan.chmod(0o600)
    plan.write_text("{not json")
    with pytest.raises(sdr.StateDirRefused, match="--plan is unusable"):
        sdr.resolve_state_dir(plan=plan, environ={})


def test_the_database_name_matches_the_store():
    from tessera_control.store import DATABASE_NAME

    assert sdr.DATABASE_NAME == DATABASE_NAME


def test_the_real_environment_is_read_by_default(monkeypatch, state):
    monkeypatch.setenv(sdr.ENV_VAR, str(state))
    assert sdr.resolve_state_dir() == state.resolve()
    monkeypatch.delenv(sdr.ENV_VAR)
    with pytest.raises(sdr.StateDirRefused):
        sdr.resolve_state_dir()
    assert os.environ.get(sdr.ENV_VAR) is None


def _project(state):
    from tessera_control.store import Store, canonical_digest

    store = Store(state)
    owner = store.authenticate((state / "owner.header").read_text().strip().split("Bearer ")[1])
    _, project = store.create_project(owner, "p", canonical_digest({"name": "P"}), {"name": "P"})
    return project["id"]


def _issue(monkeypatch, capsys, state_args, tmp_path, project, name="creds"):
    from tessera_control import cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera", "dev", "smoke-credentials", "issue", *state_args,
            "--project-id", project, "--destination", "planner.local",
            "--out-dir", str(tmp_path / name),
        ],
    )  # fmt: skip
    code = cli.main()
    seen = capsys.readouterr()
    return code, seen.out, seen.err


def test_the_smoke_credentials_command_resolves_the_directory_from_the_environment(
    tmp_path, state, monkeypatch, capsys
):
    project = _project(state)
    monkeypatch.setenv(sdr.ENV_VAR, str(state))
    code, out, err = _issue(monkeypatch, capsys, [], tmp_path, project)
    assert code == 0, err
    assert out.split()[0].startswith("smokegrant_")


def test_the_smoke_credentials_command_resolves_the_directory_from_the_deploy_plan(
    tmp_path, state, monkeypatch, capsys
):
    project = _project(state)
    monkeypatch.delenv(sdr.ENV_VAR, raising=False)
    plan = plan_file(tmp_path, state)
    code, out, err = _issue(monkeypatch, capsys, ["--plan", str(plan)], tmp_path, project)
    assert code == 0, err


def test_the_smoke_credentials_command_refuses_without_a_directory_or_on_disagreement(
    tmp_path, state, monkeypatch, capsys
):
    project = _project(state)
    monkeypatch.delenv(sdr.ENV_VAR, raising=False)
    code, out, err = _issue(monkeypatch, capsys, [], tmp_path, project, "none")
    assert code == 1 and "No state directory given" in err and not (tmp_path / "none").exists()
    other = tmp_path / "other"
    initialize_state(other)
    monkeypatch.setenv(sdr.ENV_VAR, str(other))
    code, out, err = _issue(
        monkeypatch, capsys, ["--state-dir", str(state)], tmp_path, project, "d"
    )
    assert code == 1 and "disagree" in err and not (tmp_path / "d").exists()
