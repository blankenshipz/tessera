"""Operator backup size bound: default 1 GiB, raised only explicitly, named in the error."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tessera_control import recovery
from tessera_control.recovery import backup_byte_limit, backup_state, restore_state
from tessera_control.store import StoreError, initialize_state


def _small_default(monkeypatch) -> int:
    """Pretend the default is tiny so a fresh state exceeds it."""
    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 64 * 1024)
    monkeypatch.setattr(recovery, "MAX_CONFIGURABLE_BACKUP_BYTES", 1 << 30)
    return 64 * 1024


def test_default_bound_refuses_with_the_setting_named_and_a_raised_bound_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    small = _small_default(monkeypatch)
    assert (state / "tessera.sqlite3").stat().st_size > small
    with pytest.raises(StoreError, match="--max-backup-bytes") as refused:
        backup_state(state, tmp_path / "refused")
    assert str(small) in str(refused.value)

    manifest = backup_state(state, tmp_path / "backup", max_bytes=64 << 20)
    assert manifest["size"] > small
    # The bound is per call: the next call without one is back at the default.
    with pytest.raises(StoreError, match="--max-backup-bytes"):
        backup_state(state, tmp_path / "again")

    with pytest.raises(StoreError, match="--max-backup-bytes"):
        restore_state(tmp_path / "backup", tmp_path / "restore-refused")
    database, _header = restore_state(
        tmp_path / "backup", tmp_path / "restored", max_bytes=64 << 20
    )
    assert database.exists()


@pytest.mark.parametrize("bad", [0, (1 << 30) - 1, (64 << 30) + 1, True, "2147483648"])
def test_bound_is_never_below_the_default_or_above_the_maximum(bad) -> None:
    with pytest.raises(StoreError, match="--max-backup-bytes"):
        backup_byte_limit(bad)
    assert backup_byte_limit(None) == 1 << 30
    assert backup_byte_limit(2 << 30) == 2 << 30


def test_cli_passes_the_bound(tmp_path: Path, monkeypatch, capsys) -> None:
    from tessera_control import cli

    state = tmp_path / "state"
    initialize_state(state)
    _small_default(monkeypatch)
    argv = ["tessera", "backup", "--state-dir", str(state), "--output", str(tmp_path / "b1")]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 1
    assert "--max-backup-bytes" in capsys.readouterr().err
    monkeypatch.setattr(
        sys, "argv", [*argv[:-1], str(tmp_path / "b2"), "--max-backup-bytes", str(64 << 20)]
    )
    assert cli.main() == 0


def test_every_deadline_scales_with_the_bound(tmp_path: Path, monkeypatch) -> None:
    """The copy deadline and the SQLite validation handler share one scaled bound."""
    state = tmp_path / "state"
    initialize_state(state)
    clock = [0.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock[0])
    original = recovery._validate_snapshot

    def slow_validation(path, start=None):
        clock[0] += recovery.MAX_BACKUP_SECONDS + 1  # past the default 30 s, inside 2 GiB's 60 s
        return original(path, start)

    monkeypatch.setattr(recovery, "_validate_snapshot", slow_validation)
    with pytest.raises(StoreError, match="time limit|validation failed"):
        backup_state(state, tmp_path / "default")
    clock[0] = 0.0
    assert backup_state(state, tmp_path / "raised", max_bytes=2 << 30)["size"] > 0
