"""Operator wiring of the code review git remotes (ADR 0108): create_app loads the config, the
default stays off, a malformed file fails startup, and the secret guard reaches the API."""

import json
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient
from test_code_review_git import commit, git, write
from test_code_review_jobs import CRPolicy
from test_code_review_submit import repo, submit_body  # noqa: F401
from test_review_jobs import world  # noqa: F401

from tessera_control import cli
from tessera_control.api import create_app


def _client_for(w, remotes_file=None):
    app = create_app(w.state, code_review_git_remotes=remotes_file)
    app.state.review_policy = CRPolicy()
    return app, TestClient(app)


def _post(client, w, body):
    return client.post(
        f"/projects/{w.project}/code-reviews",
        headers={**w.submitter, "Idempotency-Key": "k-wire"},
        json=body,
    )


def test_default_off_refuses_git_remote_not_configured(world, repo):  # noqa: F811
    app, client = _client_for(world)
    assert app.state.code_review_git is None
    r = _post(client, world, submit_body(repo))
    assert r.status_code == 409 and r.json()["reason"] == "git_remote_not_configured"


def test_create_app_loads_config_and_submit_succeeds(world, repo, tmp_path):  # noqa: F811
    config = tmp_path / "remotes.json"
    config.write_text(json.dumps({"repos": {"demo": str(repo["bare"])}}))
    app, client = _client_for(world, config)
    assert app.state.code_review_git.repo_names == ("demo",)
    r = _post(client, world, submit_body(repo))
    assert r.status_code == 201, r.text


def test_create_app_fails_closed_on_malformed_config(world, tmp_path):  # noqa: F811
    config = tmp_path / "remotes.json"
    config.write_text("{not json")
    with pytest.raises(ValueError):
        create_app(world.state, code_review_git_remotes=config)
    with pytest.raises(OSError):
        create_app(world.state, code_review_git_remotes=tmp_path / "missing.json")


def test_serve_flag_and_env_fail_closed_with_clear_error(world, tmp_path, monkeypatch, capsys):  # noqa: F811
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    base = ["tessera", "serve", "--state-dir", str(world.state), "--port", "8099"]
    monkeypatch.setattr(sys, "argv", [*base, "--code-review-git-remotes", str(bad)])
    code = cli.main()
    assert code == 2
    assert "code review git remotes" in capsys.readouterr().err
    monkeypatch.setenv("TESSERA_CODE_REVIEW_GIT_REMOTES", str(bad))
    monkeypatch.setattr(sys, "argv", base)
    code = cli.main()
    assert code == 2


def test_submit_refuses_secret_path_before_any_snapshot(world, tmp_path):  # noqa: F811
    bare = tmp_path / "leaky.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    work = tmp_path / "leakywork"
    git(tmp_path, "init", "-q", str(work))
    git(work, "remote", "add", "origin", str(bare))
    write(work, {"src/a.py": b"a = 1\n", "tests/test_a.py": b"def test_a():\n    pass\n"})
    commit(work, "base")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "feature/x")
    write(work, {".env": b"TOKEN=CANARY-9921\n", "src/a.py": b"a = 2\n"})
    head = commit(work, "leak")
    git(work, "push", "-q", "origin", "feature/x")
    config = tmp_path / "remotes.json"
    config.write_text(json.dumps({"repos": {"demo": str(bare)}}))
    _, client = _client_for(world, config)
    body = {
        "repo": "demo", "branch": "feature/x", "head_sha": head, "tier": "sonnet",
        "budget_cents": 100, "focused_tests": ["tests/test_a.py"],
    }  # fmt: skip
    r = _post(client, world, body)
    assert r.status_code == 409 and r.json()["reason"] == "path_denied"
    assert "CANARY" not in r.text and ".env" not in r.text
    with sqlite3.connect(world.db) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM review_jobs WHERE kind='code_review'").fetchone()[0]
            == 0
        )
