"""Installed-service startup on an already-closed run needs no model credential.

Synthetic only: a private manager directory with a profile and no key or reference files, and a
journal row set to a closed state. Nothing is started or called.
"""

# ruff: noqa: F811
import json
import os

import pytest
from test_anthropic_tool_runtime import make_profile, private
from test_reasoning_credential_references import anthropic  # noqa: F401

from tessera_agent import service_startup
from tessera_agent.remote_reasoning_store import ReasoningStore
from tessera_agent.service_startup import main


def manager(tmp_path, *, profile=True):
    root = tmp_path / "manager"
    root.mkdir(mode=0o700)
    if profile:
        private(root / "profile", json.dumps(make_profile(tools=(), max_steps=1)))
    return root


def state_with(tmp_path, run_state):
    state = tmp_path / "state"
    store = ReasoningStore(state)
    try:
        store.bind("run1", {"b": 1}, 1.0)
        if run_state:
            store.state("run1", run_state, "r")
    finally:
        store.close()
    return state


@pytest.fixture
def no_exec(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "execve", lambda *a: calls.append(a))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return calls


@pytest.mark.parametrize(
    "run_state,code", [("completed", 0), ("failed", 2), ("stop_acknowledged", 2)]
)
def test_closed_run_exits_without_credentials(
    tmp_path, monkeypatch, capsys, no_exec, run_state, code
):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path)))
    state = state_with(tmp_path, run_state)
    assert main(["reasoning", "--state-dir", str(state)]) == code
    err = capsys.readouterr().err
    assert "already closed" in err and "no model credential needed" in err
    assert "blocked" not in err and no_exec == []


def test_open_run_without_credentials_stays_blocked_78(tmp_path, monkeypatch, capsys, no_exec):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path)))
    state = state_with(tmp_path, None)
    assert main(["reasoning", "--state-dir", str(state)]) == 78
    assert "startup blocked" in capsys.readouterr().err and no_exec == []


def test_unknown_run_or_no_journal_stays_blocked_78(tmp_path, monkeypatch, capsys, no_exec):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path)))
    state = tmp_path / "state"
    assert main(["reasoning", "--state-dir", str(state)]) == 78
    assert not state.exists()  # the closed-run check created nothing
    ReasoningStore(state).close()  # a journal without this run
    assert main(["reasoning", "--state-dir", str(state)]) == 78
    assert no_exec == []


def test_absent_manager_directory_or_profile_stays_blocked_78(
    tmp_path, monkeypatch, capsys, no_exec
):
    state = state_with(tmp_path, "completed")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert main(["reasoning", "--state-dir", str(state)]) == 78
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path, profile=False)))
    assert main(["reasoning", "--state-dir", str(state)]) == 78
    assert no_exec == []


def test_the_closed_run_check_is_reasoning_only(tmp_path, monkeypatch, no_exec):
    monkeypatch.setattr(
        service_startup, "closed_run_exit", lambda *a: pytest.fail("called for another mode")
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path)))
    assert main(["a2a", "--state-dir", str(tmp_path / "state")]) == 78
