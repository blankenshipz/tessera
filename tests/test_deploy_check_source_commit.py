"""``deploy-check --apply refresh_source_pin`` holds ``--source-commit`` to the checkout head.

Only the pin's digest is measured from the install; the commit is operator-asserted. With a
repository checkout in the working directory a commit that is not its head is refused before any
write; without one the commit is accepted as given.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from test_deploy_config import executor_document, write

from tessera_control import salad_live_check as live
from tessera_control.cli import main

STALE = "a" * 40


def _config(tmp_path):
    base = tmp_path / "executor"
    base.mkdir(mode=0o700)
    return write(base, dict(executor_document(base), source_revision=STALE))


def _checkout(root):
    """A git checkout (tests/ and src/ present); returns its head commit."""
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x = 1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], cwd=root, check=True)
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _run(monkeypatch, path, receipts, commit):
    argv = ["tessera", "deploy-check", "--format", "json", "--executor-config", str(path)]
    argv += ["--apply", "refresh_source_pin", "--receipt-dir", str(receipts)]
    monkeypatch.setattr("sys.argv", [*argv, "--source-commit", commit])
    return main()


@pytest.fixture
def receipts(tmp_path):
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700)
    return directory


def test_a_commit_that_is_the_checkout_head_is_accepted(tmp_path, receipts, monkeypatch, capsys):
    path = _config(tmp_path)
    site = tmp_path / "site"
    head = _checkout(site)
    monkeypatch.chdir(site)
    _run(monkeypatch, path, receipts, head)
    report = json.loads(capsys.readouterr().out)
    assert [entry["fix"] for entry in report["applied"]] == ["refresh_source_pin"]
    assert json.loads(path.read_text())["source_revision"] == live.installed_source_pin(head)


def test_a_commit_that_is_not_the_checkout_head_is_refused_before_any_write(
    tmp_path, receipts, monkeypatch, capsys
):
    path = _config(tmp_path)
    before = path.read_bytes()
    site = tmp_path / "site"
    head = _checkout(site)
    monkeypatch.chdir(site)
    assert _run(monkeypatch, path, receipts, "b" * 40) == 2
    error = capsys.readouterr().err
    assert "--source-commit" in error and head in error
    assert path.read_bytes() == before and list(receipts.iterdir()) == []


def test_without_a_checkout_the_commit_is_accepted_as_given(
    tmp_path, receipts, monkeypatch, capsys
):
    path = _config(tmp_path)
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    _run(monkeypatch, path, receipts, "b" * 40)
    report = json.loads(capsys.readouterr().out)
    assert [entry["fix"] for entry in report["applied"]] == ["refresh_source_pin"]
    assert json.loads(path.read_text())["source_revision"] == live.installed_source_pin("b" * 40)
