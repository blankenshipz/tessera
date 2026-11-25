"""The installed unit's ExecStop is idempotent and needs no model credential.

Synthetic only: a private manager directory with a profile and no key, reference, token or
admission files, and a journal row in a chosen state. Nothing is started or called.
"""

# ruff: noqa: F811
import os
from datetime import UTC, datetime, timedelta

import pytest
from test_reasoning_credential_references import anthropic  # noqa: F401
from test_reasoning_startup_refusal import marker
from test_service_startup_closed_run import manager, no_exec, state_with  # noqa: F401

from tessera_agent.remote_reasoning_store import ReasoningStore
from tessera_agent.service_startup import main


def stop(state):
    return main(["reasoning-stop", "--state-dir", str(state)])


def stop_requested(state):
    store = ReasoningStore(state)
    try:
        return store.get("run1")["stop_requested"]
    finally:
        store.close()


@pytest.mark.parametrize("run_state", ["completed", "failed", "stop_acknowledged"])
def test_stop_after_closed_run_exits_0_without_credentials(
    tmp_path, monkeypatch, capsys, run_state
):
    root = manager(tmp_path)
    assert sorted(p.name for p in root.iterdir()) == ["profile"]
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(root))
    state = state_with(tmp_path, run_state)
    assert stop(state) == 0
    err = capsys.readouterr().err
    assert f"already closed (state {run_state})" in err and "blocked" not in err
    assert err.count("\n") == 1
    assert stop_requested(state) == 0  # nothing written for a closed run
    assert stop(state) == 0  # idempotent


def test_stop_after_closed_run_needs_no_manager_directory(tmp_path, monkeypatch, capsys):
    state = state_with(tmp_path, "completed")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert stop(state) == 0
    assert "no open run" in capsys.readouterr().err


def test_stop_without_journal_exits_0_and_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    state = tmp_path / "state"
    assert stop(state) == 0
    assert not state.exists()
    assert "no run journal" in capsys.readouterr().err


def test_stop_after_main_process_exited_writes_nothing(tmp_path, monkeypatch, capsys):
    # systemd runs ExecStop after the main process exits on its own (also before a crash
    # restart, which must resume the open run): EXIT_CODE is set and MAINPID is not.
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path)))
    monkeypatch.setenv("EXIT_CODE", "exited")
    monkeypatch.delenv("MAINPID", raising=False)
    state = state_with(tmp_path, None)
    assert stop(state) == 0
    assert "supervisor not running" in capsys.readouterr().err
    assert stop_requested(state) == 0


def test_stop_of_open_run_requests_stop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path)))
    monkeypatch.setenv("MAINPID", "4242")
    state = state_with(tmp_path, None)
    assert stop(state) == 0
    assert "stop requested for run run1" in capsys.readouterr().err
    assert stop_requested(state) == 1
    assert stop(state) == 0  # a second stop stays successful


def test_stop_of_open_run_without_run_identity_fails_closed(tmp_path, monkeypatch, capsys):
    state = state_with(tmp_path, None)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path, profile=False)))
    assert stop(state) == 78
    assert "startup blocked" in capsys.readouterr().err
    assert stop_requested(state) == 0


def test_start_of_open_run_without_credential_still_exits_78(
    tmp_path, monkeypatch, capsys, no_exec
):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(manager(tmp_path)))
    state = state_with(tmp_path, None)
    assert main(["reasoning", "--state-dir", str(state)]) == 78
    assert "startup blocked" in capsys.readouterr().err and no_exec == []


def test_start_of_open_run_with_expired_marker_still_exits_78(
    anthropic, tmp_path, monkeypatch, capsys, no_exec
):
    root, _, env = anthropic
    (root / "admission").write_bytes(marker(datetime.now(UTC) - timedelta(days=1)))
    (root / "admission").chmod(0o600)
    monkeypatch.setattr(os, "environ", dict(env))
    state = state_with(tmp_path, None)
    assert main(["reasoning", "--state-dir", str(state)]) == 78
    assert "admission_expired" in capsys.readouterr().err and no_exec == []
