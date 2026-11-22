"""Git source reader for code review submit (ADR 0108): local bare repositories only, no network."""

import json
import os
import subprocess

import pytest

from tessera_control.code_review_git import (
    ChangedFile,
    GitRemotes,
    GitSourceRefused,
    load_git_remotes,
)

GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "LANG": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd, *args):
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    return result.stdout.decode().strip()


def write(work, files):
    for name, data in files.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def commit(work, message):
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", str(work))
    git(work, "remote", "add", "origin", str(bare))
    write(
        work,
        {
            "src/keep.py": b"keep = 1\n",
            "src/change.py": b"value = 1\n",
            "src/gone.py": b"gone = 1\n",
            "tests/test_change.py": b"def test_value():\n    assert True\n",
        },
    )
    root = commit(work, "base")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "feature/x")
    write(work, {"src/change.py": b"value = 2\n", "src/new.py": b"new = \x00\xff\n"})
    (work / "src" / "gone.py").unlink()
    head = commit(work, "feature")
    git(work, "push", "-q", "origin", "feature/x")
    return {
        "bare": bare,
        "work": work,
        "root": root,
        "head": head,
        "remotes": GitRemotes({"demo": bare}),
    }


def refused(code, fn, *args, **kwargs):
    with pytest.raises(GitSourceRefused) as info:
        fn(*args, **kwargs)
    assert info.value.code == code
    assert str(info.value) == code
    return info.value


def test_resolve_happy_path(world):
    res = world["remotes"].resolve("demo", "feature/x", world["head"])
    assert (res.repo, res.branch, res.head_sha, res.base_branch) == (
        "demo",
        "feature/x",
        world["head"],
        "main",
    )
    assert res.base_sha == world["root"]
    assert sorted(res.changed, key=lambda c: c.path) == [
        ChangedFile("src/change.py", "M"),
        ChangedFile("src/gone.py", "D"),
        ChangedFile("src/new.py", "A"),
    ]
    assert {d["status"] for d in res.changed_dicts()} == {"A", "M", "D"}


def test_resolve_uses_merge_base_when_base_advanced(world):
    work = world["work"]
    git(work, "checkout", "-q", "main")
    write(work, {"src/keep.py": b"keep = 2\n", "src/other.py": b"x\n"})
    commit(work, "base moves on")
    git(work, "push", "-q", "origin", "main")
    res = world["remotes"].resolve("demo", "feature/x", world["head"])
    assert res.base_sha == world["root"]
    assert {c.path for c in res.changed} == {"src/change.py", "src/gone.py", "src/new.py"}


def test_head_mismatch_branch_and_base_not_found(world):
    remotes = world["remotes"]
    refused("head_mismatch", remotes.resolve, "demo", "feature/x", world["root"])
    refused("head_mismatch", remotes.resolve, "demo", "feature/x", "a" * 40)
    refused("branch_not_found", remotes.resolve, "demo", "feature/none", world["head"])
    refused(
        "base_not_found", remotes.resolve, "demo", "feature/x", world["head"], base_branch="develop"
    )


def test_no_changes(world):
    work = world["work"]
    git(work, "checkout", "-q", "-b", "same", "main")
    git(work, "push", "-q", "origin", "same")
    refused("no_changes", world["remotes"].resolve, "demo", "same", world["root"])
    # A commit that changes nothing relative to the base tree.
    git(work, "commit", "-q", "--allow-empty", "-m", "empty")
    empty = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "same")
    refused("no_changes", world["remotes"].resolve, "demo", "same", empty)


def test_change_too_large(world):
    few = GitRemotes({"demo": world["bare"]}, max_changed_files=2)
    refused("change_too_large", few.resolve, "demo", "feature/x", world["head"])
    small = GitRemotes({"demo": world["bare"]}, max_total_bytes=20)
    refused("change_too_large", small.resolve, "demo", "feature/x", world["head"])
    refused(
        "change_too_large",
        small.files_at,
        "demo",
        world["root"],
        ["src/keep.py", "src/change.py", "tests/test_change.py"],
    )


def test_unsupported_entry_symlink(world):
    work = world["work"]
    git(work, "checkout", "-q", "feature/x")
    os.symlink("change.py", work / "src" / "link.py")
    head = commit(work, "symlink")
    git(work, "push", "-q", "origin", "feature/x")
    remotes = world["remotes"]
    refused("unsupported_entry", remotes.resolve, "demo", "feature/x", head)
    refused("unsupported_entry", remotes.files_at, "demo", head, ["src/link.py"])
    assert remotes.tree_has_files("demo", head, ["src/link.py", "src/change.py"]) == {
        "src/change.py"
    }


@pytest.mark.parametrize(
    "branch",
    [
        "-x",
        "--upload-pack=x",
        "a..b",
        "x@{1}",
        "@{-1}",
        "HEAD~1",
        "x^",
        "a b",
        "a:b",
        "x.lock",
        "x/",
        "/x",
        "a//b",
        "",
        "refs/heads/../x",
        "x\n",
        7,
        None,
        "a" * 201,
    ],
)
def test_branch_injection_refused(world, branch):
    remotes = world["remotes"]
    refused("invalid_branch", remotes.resolve, "demo", branch, world["head"])
    refused(
        "invalid_base_branch",
        remotes.resolve,
        "demo",
        "feature/x",
        world["head"],
        base_branch=branch,
    )


@pytest.mark.parametrize("repo", ["a/b", "../demo", "-demo", ".demo", "", "x" * 65, None])
def test_repo_name_refused(world, repo):
    refused("invalid_repo", world["remotes"].resolve, repo, "feature/x", world["head"])


def test_unknown_repo_and_bad_shas(world):
    remotes = world["remotes"]
    refused("repo_not_configured", remotes.resolve, "other", "feature/x", world["head"])
    refused("repo_not_configured", remotes.files_at, "other", world["head"], ["a"])
    for sha in ["HEAD", world["head"].upper(), world["head"][:39], "-" + "a" * 39, "0" * 40]:
        refused("invalid_head_sha", remotes.resolve, "demo", "feature/x", sha)
        refused("invalid_sha", remotes.files_at, "demo", sha, ["src/keep.py"])
        refused("invalid_sha", remotes.tree_has_files, "demo", sha, ["src/keep.py"])
    refused("commit_not_found", remotes.files_at, "demo", "b" * 40, ["src/keep.py"])


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../b", "a//b", "./a", "a/", "", "a\0b"])
def test_paths_validated(world, path):
    remotes = world["remotes"]
    refused("invalid_path", remotes.files_at, "demo", world["head"], [path])
    refused("invalid_path", remotes.tree_has_files, "demo", world["head"], [path])


def test_files_at_bytes_exact(world):
    remotes = world["remotes"]
    head = remotes.files_at(
        "demo", world["head"], ["src/change.py", "src/new.py", "src/gone.py", "-p", "*.py"]
    )
    assert head == {"src/change.py": b"value = 2\n", "src/new.py": b"new = \x00\xff\n"}
    base = remotes.files_at("demo", world["root"], ["src/change.py", "src/gone.py", "src/new.py"])
    assert base == {"src/change.py": b"value = 1\n", "src/gone.py": b"gone = 1\n"}
    assert remotes.files_at("demo", world["head"], []) == {}


def test_tree_has_files(world):
    remotes = world["remotes"]
    found = remotes.tree_has_files(
        "demo",
        world["head"],
        ["tests/test_change.py", "tests", "src", "tests/missing.py", "tests/*.py", "src/gone.py"],
    )
    assert found == {"tests/test_change.py"}
    assert remotes.tree_has_files("demo", world["head"], ["tests"]) == set()


def test_load_git_remotes(world, tmp_path):
    config = tmp_path / "remotes.json"
    config.write_text(json.dumps({"repos": {"demo": str(world["bare"])}}))
    remotes = load_git_remotes(config)
    assert remotes.repo_names == ("demo",)
    assert remotes.resolve("demo", "feature/x", world["head"]).base_sha == world["root"]
    for bad in [
        {"repos": {"a/b": "/x"}},
        {"repos": {"demo": "relative"}},
        {"repo": {}},
        {"repos": {"demo": "/x"}, "extra": 1},
        [],
    ]:
        config.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_git_remotes(config)
