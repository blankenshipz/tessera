"""Regression coverage for the native development fixture cleanup."""

from __future__ import annotations

from pathlib import Path


def test_fixture_cleanup_removes_read_only_staged_input(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    from check_development_e2e import _remove_fixture_root

    staged = tmp_path / "fixture"
    input_directory = staged / "workspace" / "inputs"
    input_directory.mkdir(parents=True)
    (input_directory / "input.txt").write_text("synthetic input")
    (input_directory / "input.txt").chmod(0o444)
    input_directory.chmod(0o555)

    _remove_fixture_root(staged)

    assert not staged.exists()
