"""`tessera init` refuses cleanly: a short message, exit 1, no traceback, no path, no leftovers."""

from __future__ import annotations

import os
import sqlite3

import pytest

from tessera_control import store
from tessera_control.cli import main


def _init(monkeypatch, capsys, target) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.argv", ["tessera", "init", "--state-dir", str(target)])
    code = main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _assert_clean(err: str, target) -> None:
    assert err.startswith("init failed:")
    assert "Traceback" not in err
    assert str(target) not in err and str(target.parent) not in err


def test_an_existing_state_directory_is_refused_without_a_traceback(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    assert _init(monkeypatch, capsys, state)[0] == 0
    code, out, err = _init(monkeypatch, capsys, state)
    assert code == 1 and out == ""
    _assert_clean(err, state)


def test_a_missing_parent_is_refused_without_a_traceback(tmp_path, monkeypatch, capsys):
    target = tmp_path / "nope" / "deeper" / "state"
    code, out, err = _init(monkeypatch, capsys, target)
    assert code == 1 and out == ""
    _assert_clean(err, target)
    assert "left in place" not in err
    assert not (tmp_path / "nope").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write into a read-only directory")
def test_an_unwritable_parent_is_refused_without_a_traceback(tmp_path, monkeypatch, capsys):
    parent = tmp_path / "readonly"
    parent.mkdir()
    parent.chmod(0o555)
    target = parent / "state"
    try:
        code, out, err = _init(monkeypatch, capsys, target)
    finally:
        parent.chmod(0o700)
    assert code == 1 and out == ""
    _assert_clean(err, target)
    assert "Permission denied" in err
    assert "left in place" not in err
    assert not target.exists()


def test_a_name_the_filesystem_rejects_is_refused_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    target = tmp_path / ("a" * 400)
    code, out, err = _init(monkeypatch, capsys, target)
    assert code == 1 and out == ""
    _assert_clean(err, target)


def test_a_failure_after_the_directory_exists_says_the_partial_directory_was_kept(
    tmp_path, monkeypatch, capsys
):
    def full_disk(connection):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_create_schema", full_disk)
    target = tmp_path / "state"
    code, out, err = _init(monkeypatch, capsys, target)
    assert code == 1 and out == ""
    _assert_clean(err, target)
    assert "No space left on device" in err
    # initialize_state keeps a failed bootstrap for diagnosis, and the message says so, because a
    # retry would otherwise only report that the directory already exists.
    assert target.exists()
    assert "left in place; remove it before retrying" in err


def test_a_sqlite_failure_is_refused_without_a_traceback(tmp_path, monkeypatch, capsys):
    def broken(path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(store, "_connect", broken)
    target = tmp_path / "state"
    code, out, err = _init(monkeypatch, capsys, target)
    assert code == 1 and out == ""
    _assert_clean(err, target)
    assert "OperationalError" in err
    assert "left in place; remove it before retrying" in err
