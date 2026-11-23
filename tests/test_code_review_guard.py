"""Secret and data guard of the code review git reader (ADR 0108): synthetic bare repositories,
no network. A refusal carries a code and a rule label, never a path or file content."""

import json

import pytest
from test_code_review_git import commit, git, refused, write

from tessera_control.code_review_git import GitRemotes, load_git_remotes

SECRET_BODY = b"CANARY-SECRET-BODY-7731\n"


def make_repo(tmp_path, files):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", str(work))
    git(work, "remote", "add", "origin", str(bare))
    write(work, {"src/keep.py": b"keep = 1\n"})
    commit(work, "base")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "feature/x")
    write(work, files)
    head = commit(work, "feature")
    git(work, "push", "-q", "origin", "feature/x")
    return bare, head


DENIED = [
    ".env",
    "config/.env.production",
    "deploy/server.pem",
    "keys/service.key",
    "home/id_rsa",
    "home/id_ed25519.pub",
    "certs/store.p12",
    "certs/store.pfx",
    "app/credentials.json",
    "app/secrets.yaml",
    ".aws/config",
    "x/.ssh/known_hosts",
    ".gnupg/pubring.kbx",
    "state/app.sqlite3",
    "state/app.db",
    "data/train.csv",
    "pkg/datasets/rows.txt",
]


@pytest.mark.parametrize("path", DENIED)
def test_denied_path_classes_refused_without_content(tmp_path, path):
    bare, head = make_repo(tmp_path, {path: SECRET_BODY, "src/ok.py": b"ok = 1\n"})
    remotes = GitRemotes({"demo": bare})
    err = refused("path_denied", remotes.resolve, "demo", "feature/x", head)
    assert err.rule
    assert SECRET_BODY.decode().strip() not in repr(err) + str(err) + err.rule
    assert path not in repr(err) + str(err)


def test_deleted_secret_path_is_refused_too(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", str(work))
    git(work, "remote", "add", "origin", str(bare))
    write(work, {"src/keep.py": b"keep = 1\n", ".env": SECRET_BODY})
    commit(work, "base")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "feature/x")
    (work / ".env").unlink()
    head = commit(work, "drop")
    git(work, "push", "-q", "origin", "feature/x")
    refused("path_denied", GitRemotes({"demo": bare}).resolve, "demo", "feature/x", head)


def test_normal_source_diff_accepted(tmp_path):
    files = {
        "src/app.py": b"def f():\n    return 'key'\n",
        "src/environment.py": b"env = 1\n",
        "docs/data_model.md": b"notes\n",
        "tests/test_app.py": b"def test_f():\n    assert True\n",
    }
    bare, head = make_repo(tmp_path, files)
    remotes = GitRemotes({"demo": bare})
    resolution = remotes.resolve("demo", "feature/x", head)
    got = remotes.files_at("demo", head, [c.path for c in resolution.changed])
    assert got == files


@pytest.mark.parametrize(
    "content",
    [
        b"x = 1\n-----BEGIN RSA PRIVATE KEY-----\nabc\n",
        b"-----BEGIN PRIVATE KEY-----\nabc\n",
        b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
        b'KEY = "sk-ant-' + b"api03-" + b"A" * 30 + b'"\n',
        b"aws = 'AKIAABCDEFGHIJKLMNOP'\n",
        b"t = 'ghp_" + b"a" * 36 + b"'\n",
    ],
)
def test_secret_content_refused_without_content(tmp_path, content):
    bare, head = make_repo(tmp_path, {"src/leaky.py": content})
    remotes = GitRemotes({"demo": bare})
    resolution = remotes.resolve("demo", "feature/x", head)
    err = refused("content_denied", remotes.files_at, "demo", head, ["src/leaky.py"])
    assert err.rule and b"BEGIN" not in err.rule.encode() and "sk-ant" not in repr(err)
    assert resolution.changed


def test_public_key_and_prose_not_content_denied(tmp_path):
    files = {"src/a.py": b"# a PRIVATE KEY is never committed\nx = 'sk-ant'\n"}
    bare, head = make_repo(tmp_path, files)
    remotes = GitRemotes({"demo": bare})
    assert remotes.files_at("demo", head, ["src/a.py"]) == files


def test_operator_deny_and_allow_paths(tmp_path):
    files = {"corpus/rows.txt": b"r\n", "data/schema.py": b"schema = 1\n"}
    bare, head = make_repo(tmp_path, files)
    plain = GitRemotes({"demo": bare})
    refused("path_denied", plain.resolve, "demo", "feature/x", head)
    allowed = GitRemotes({"demo": bare}, allow_paths=["data/schema.py"], deny_paths=["corpus"])
    err = refused("path_denied", allowed.resolve, "demo", "feature/x", head)
    assert err.rule == "operator_deny_paths"
    only_allow = GitRemotes({"demo": bare}, allow_paths=["data/*.py"])
    assert only_allow.files_at("demo", head, ["data/schema.py", "corpus/rows.txt"])
    # An allow glob never exempts the content check or an operator deny.
    both = GitRemotes({"demo": bare}, allow_paths=["corpus/*"], deny_paths=["corpus/*"])
    refused("path_denied", both.files_at, "demo", head, ["corpus/rows.txt"])


def test_operator_lists_loaded_and_validated(tmp_path):
    bare, head = make_repo(tmp_path, {"corpus/rows.txt": b"r\n"})
    config = tmp_path / "remotes.json"
    config.write_text(json.dumps({"repos": {"demo": str(bare)}, "deny_paths": ["corpus"]}))
    remotes = load_git_remotes(config)
    refused("path_denied", remotes.resolve, "demo", "feature/x", head)
    for bad in [
        {"repos": {"demo": str(bare)}, "deny_paths": "corpus"},
        {"repos": {"demo": str(bare)}, "allow_paths": ["/abs"]},
        {"repos": {"demo": str(bare)}, "deny_paths": [""]},
        {"repos": {"demo": str(bare)}, "deny_paths": ["../x"]},
        {"repos": {"demo": str(bare)}, "deny_paths": [1]},
        {"deny_paths": []},
    ]:
        config.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_git_remotes(config)
