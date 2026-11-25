"""Reasoning start-up order (ADR 0170 with the closed-run exit), and two review follow-ups.

A closed run exits before the admission marker is judged, so an expired marker cannot turn a
finished run into a failed unit; an open run with an expired or malformed marker still refuses
with 78. Synthetic only: private manager directories and a one-row journal; nothing is started.
"""

# ruff: noqa: F811
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_reasoning_startup_refusal import marker
from test_service_startup_closed_run import manager, no_exec, state_with  # noqa: F401

from tessera_agent import first_call_readiness as fcr
from tessera_agent import readiness_in_unit, service_startup
from tessera_control.agent_authority import AgentAuthorityStore

EXPIRED = lambda: marker(datetime.now(UTC) - timedelta(days=1))  # noqa: E731
VALID = lambda: marker(datetime.now(UTC) + timedelta(days=2))  # noqa: E731


def with_marker(root: Path, content: bytes) -> None:
    path = root / "admission"
    path.write_bytes(content)
    path.chmod(0o600)


@pytest.mark.parametrize(
    ("run_state", "code"), [("completed", 0), ("failed", 2), ("stop_acknowledged", 2)]
)
def test_closed_run_with_an_expired_marker_exits_before_admission(
    tmp_path, monkeypatch, capsys, no_exec, run_state, code
):
    root = manager(tmp_path)
    with_marker(root, EXPIRED())
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(root))
    state = state_with(tmp_path, run_state)
    assert service_startup.main(["reasoning", "--state-dir", str(state)]) == code
    err = capsys.readouterr().err
    assert "already closed" in err and "admission_expired" not in err and no_exec == []


@pytest.mark.parametrize(
    ("content", "reason"),
    [(EXPIRED(), "admission_expired"), (b'{"kind": "x"}', "admission_marker_malformed")],
)
@pytest.mark.parametrize("run_state", [None, "running"])
def test_open_run_with_a_bad_marker_still_refuses_78(
    tmp_path, monkeypatch, capsys, no_exec, content, reason, run_state
):
    root = manager(tmp_path)
    with_marker(root, content)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(root))
    state = state_with(tmp_path, run_state)
    assert service_startup.main(["reasoning", "--state-dir", str(state)]) == 78
    assert reason in capsys.readouterr().err and no_exec == []


def test_open_run_with_a_valid_marker_starts(tmp_path, monkeypatch, no_exec):
    root = manager(tmp_path)
    with_marker(root, VALID())
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(root))
    monkeypatch.setattr(
        service_startup, "launch_spec", lambda mode, state, env: (["python", "-m", "x"], {})
    )
    state = state_with(tmp_path, "running")
    service_startup.main(["reasoning", "--state-dir", str(state)])
    assert len(no_exec) == 1  # execve was reached: the run starts


def test_a_run_with_no_journal_and_an_expired_marker_still_refuses(
    tmp_path, monkeypatch, capsys, no_exec
):
    root = manager(tmp_path)
    with_marker(root, EXPIRED())
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(root))
    assert service_startup.main(["reasoning", "--state-dir", str(tmp_path / "none")]) == 78
    assert "admission_expired" in capsys.readouterr().err and no_exec == []


# -- first_call_readiness --in-unit forwards an explicit --admitted-path -------------------------


def test_in_unit_forwards_the_explicit_admitted_path_and_defaults_to_the_units(monkeypatch):
    seen = []

    def fake(unit_path, **kwargs):
        seen.append(kwargs)
        return 0, "{}"

    monkeypatch.setattr(readiness_in_unit, "run_in_unit", fake)
    fcr.main(["check", "--in-unit", "--admitted-path", "/etc/x/explicit.admitted"])
    fcr.main(["check", "--in-unit"])
    assert seen[0]["admitted_path"] == Path("/etc/x/explicit.admitted")
    assert seen[1]["admitted_path"] is None  # run_in_unit then uses the unit's own condition path


# -- the two binding rules cannot drift ----------------------------------------------------------


def _db(rows):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE execution_grants (id TEXT, project_id TEXT, status TEXT, revision INT, "
        "epoch INT, expires_at TEXT)"
    )
    connection.executemany("INSERT INTO execution_grants VALUES (?,?,?,?,?,?)", rows)
    return connection


def _bounds(*grant_ids, revision=1, epoch=1):
    return {
        "bindings": [
            {
                "execution_grant_id": grant_id,
                "expected_execution_grant_revision": revision,
                "expected_execution_epoch": epoch,
            }
            for grant_id in grant_ids
        ]
    }


def test_binding_rules_agree_on_every_same_project_case():
    soon = (datetime.now(UTC) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    connection = _db(
        [
            ("ok", "p1", "active", 1, 1, soon),
            ("ok2", "p1", "active", 1, 1, soon),
            ("revoked", "p1", "revoked", 1, 1, soon),
            ("moved", "p1", "active", 2, 1, soon),
            ("epoch", "p1", "active", 1, 2, soon),
            ("expired", "p1", "active", 1, 1, past),
            ("garbled", "p1", "active", 1, 1, "not-a-time"),
        ]
    )
    cases = [
        (_bounds("ok"), True),
        (_bounds("ok", "ok2"), True),
        (_bounds("ok", "revoked"), False),
        (_bounds("moved"), False),
        (_bounds("epoch"), False),
        (_bounds("expired"), False),
        (_bounds("garbled"), False),
        (_bounds("missing"), False),
        ({"bindings": []}, False),
        ({}, False),
        ({"bindings": "x"}, False),
        ({"bindings": [{}]}, False),
    ]
    now = datetime.now(UTC)
    for bounds, expected in cases:
        control = AgentAuthorityStore._bindings_active(connection, {"project_id": "p1"}, bounds)
        readiness = fcr._execution_bindings_active(connection, bounds, now)
        assert control is readiness is expected, bounds


def test_control_is_stricter_across_projects_by_design():
    soon = (datetime.now(UTC) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    connection = _db([("other", "p2", "active", 1, 1, soon)])
    bounds = _bounds("other")
    assert AgentAuthorityStore._bindings_active(connection, {"project_id": "p1"}, bounds) is False
    assert fcr._execution_bindings_active(connection, bounds, datetime.now(UTC)) is True
