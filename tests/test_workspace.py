import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

import tessera_control.workspace as workspace
from tessera_control.workspace import (
    WorkspaceError,
    capture_workspace,
    inventory_digest,
    inventory_workspace,
    snapshot_skill,
)


def test_inventory_is_deterministic_and_excludes_links_secrets_and_dirs(tmp_path: Path):
    (tmp_path / "z.bin").write_bytes(b"\x00\xff")
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / ".env.local").write_text("secret")
    (tmp_path / "owner.header").write_text("secret")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "a.txt")
    first = inventory_workspace(tmp_path.absolute())
    second = inventory_workspace(tmp_path.absolute())
    assert first == second
    assert [entry.relative_path for entry in first.entries] == ["a.txt", "z.bin"]
    assert {item.reason for item in first.exclusions} == {
        "credential_path",
        "ignored_directory",
        "symlink",
    }
    assert first.entries[0].provenance == "imported_unverified"
    assert first.entries[0].classification == "unclassified"


def test_inventory_requires_real_absolute_root_and_honors_bounds(tmp_path: Path):
    (tmp_path / "x").write_bytes(b"1234")
    with pytest.raises(WorkspaceError, match="absolute"):
        inventory_workspace("relative")
    link = tmp_path / "root-link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="real directory"):
        inventory_workspace(link.absolute())
    with pytest.raises(WorkspaceError, match="byte limit"):
        inventory_workspace(tmp_path.absolute(), max_file_bytes=3)
    with pytest.raises(WorkspaceError, match="file count"):
        inventory_workspace(tmp_path.absolute(), max_files=0)


def test_capture_freezes_single_read_bytes_and_canonical_inventory_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "input.txt"
    source.write_bytes(b"before")
    (tmp_path / ".env.local").write_text("secret")
    original_read = workspace._read_bytes_at

    def read_then_mutate(directory_fd: int, name: str, limit: int, relative_path: str) -> bytes:
        payload = original_read(directory_fd, name, limit, relative_path)
        if relative_path == "input.txt":
            source.write_bytes(b"after")
        return payload

    monkeypatch.setattr(workspace, "_read_bytes_at", read_then_mutate)
    captured = capture_workspace(tmp_path.absolute())
    assert captured.files == (("input.txt", b"before"),)
    entry = captured.inventory.entries[0]
    assert entry.sha256 == hashlib.sha256(captured.files[0][1]).hexdigest()
    expected = hashlib.sha256(
        json.dumps(asdict(captured.inventory), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert inventory_digest(captured.inventory) == expected
    assert captured.inventory.exclusions == inventory_workspace(tmp_path.absolute()).exclusions


def test_capture_is_deterministic_and_honors_capture_bounds(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.txt").write_text("two")
    one = capture_workspace(tmp_path.absolute())
    two = capture_workspace(tmp_path.absolute())
    assert one == two
    assert inventory_digest(one.inventory) == inventory_digest(two.inventory)
    with pytest.raises(WorkspaceError, match="file exceeds byte limit"):
        capture_workspace(tmp_path.absolute(), max_file_bytes=2)
    with pytest.raises(WorkspaceError, match="total byte limit"):
        capture_workspace(tmp_path.absolute(), max_total_bytes=5)
    with pytest.raises(WorkspaceError, match="file count"):
        capture_workspace(tmp_path.absolute(), max_files=1)


def test_skill_validation_snapshot_and_qualified_names(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text(
        "---\nname: sample\ndescription: |\n  Do a thing.\n---\n\nInstructions.\n"
    )
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "guide.txt").write_text("guide")
    one = snapshot_skill(tmp_path.absolute(), qualified_name="team/sample")
    two = snapshot_skill(tmp_path.absolute(), qualified_name="team/sample")
    assert one == two
    assert one.name == "team/sample"
    assert {item[0] for item in one.manifest} == {"SKILL.md", "references/guide.txt"}
    with pytest.raises(WorkspaceError, match="qualified"):
        snapshot_skill(tmp_path.absolute(), qualified_name="sample")


@pytest.mark.parametrize(
    "frontmatter",
    ["name: only-name", "description: only-description", "name: [broken"],
)
def test_skill_rejects_malformed_frontmatter(tmp_path: Path, frontmatter: str):
    (tmp_path / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n")
    with pytest.raises(WorkspaceError):
        snapshot_skill(tmp_path.absolute())


def test_skill_rejects_symlink_and_does_not_execute_scripts(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text("---\nname: safe\ndescription: safe\n---\n")
    (tmp_path / "run.py").write_text("raise RuntimeError('executed')")
    outside = tmp_path.parent / "outside-skill-secret"
    outside.write_text("secret")
    (tmp_path / "ref").symlink_to(outside)
    with pytest.raises(WorkspaceError, match="symlink"):
        snapshot_skill(tmp_path.absolute())


def test_roots_with_symlinked_ancestors_are_rejected(tmp_path: Path):
    actual = tmp_path / "actual"
    skill = actual / "skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: safe\ndescription: safe\n---\n")
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="real directory"):
        inventory_workspace((alias / "skill").absolute())
    with pytest.raises(WorkspaceError, match="real directory"):
        snapshot_skill((alias / "skill").absolute())


def test_inventory_detects_queued_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    child = tmp_path / "child"
    child.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must not be inventoried")
    original_scandir = workspace.os.scandir
    swapped = False

    class ScandirThatSwapsAfterRead:
        def __init__(self, scanned):
            self.scanned = scanned

        def __enter__(self):
            return self.scanned

        def __exit__(self, *args):
            nonlocal swapped
            self.scanned.close()
            if not swapped:
                child.rmdir()
                child.symlink_to(outside, target_is_directory=True)
                swapped = True

    def scandir_and_swap(directory_fd: int):
        return ScandirThatSwapsAfterRead(original_scandir(directory_fd))

    monkeypatch.setattr(workspace.os, "scandir", scandir_and_swap)
    with pytest.raises(WorkspaceError, match="directory changed"):
        inventory_workspace(tmp_path.absolute())


def test_inventory_caps_exclusions_and_all_visited_nodes(tmp_path: Path):
    (tmp_path / ".env.a").write_text("secret")
    (tmp_path / ".env.b").write_text("secret")
    with pytest.raises(WorkspaceError, match="exclusion limit"):
        inventory_workspace(tmp_path.absolute(), max_exclusions=1)
    with pytest.raises(WorkspaceError, match="node limit"):
        inventory_workspace(tmp_path.absolute(), max_nodes=1)


def test_inventory_stops_scanning_after_remaining_node_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for index in range(3):
        (tmp_path / f"file-{index}").write_text("safe")
    original_scandir = workspace.os.scandir
    yielded = 0

    class CountingScandir:
        def __init__(self, scanned):
            self.scanned = scanned

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.scanned.close()

        def __iter__(self):
            nonlocal yielded
            for item in self.scanned:
                yielded += 1
                if yielded > 2:
                    raise AssertionError("scandir consumed beyond its node budget")
                yield item

    def counting_scandir(directory_fd: int):
        return CountingScandir(original_scandir(directory_fd))

    monkeypatch.setattr(workspace.os, "scandir", counting_scandir)
    with pytest.raises(WorkspaceError, match="node limit"):
        inventory_workspace(tmp_path.absolute(), max_nodes=1)
    assert yielded == 2


def test_missing_no_follow_support_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "file.txt").write_text("safe")
    monkeypatch.delattr(workspace.os, "O_NOFOLLOW", raising=False)
    with pytest.raises(WorkspaceError, match="O_NOFOLLOW"):
        inventory_workspace(tmp_path.absolute())


def test_missing_nonblocking_read_support_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "file.txt").write_text("safe")
    monkeypatch.delattr(workspace.os, "O_NONBLOCK", raising=False)
    with pytest.raises(WorkspaceError, match="O_NONBLOCK"):
        inventory_workspace(tmp_path.absolute())
    with pytest.raises(WorkspaceError, match="O_NONBLOCK"):
        capture_workspace(tmp_path.absolute())
    (tmp_path / "SKILL.md").write_text("---\nname: safe\ndescription: safe\n---\n")
    with pytest.raises(WorkspaceError, match="O_NONBLOCK"):
        snapshot_skill(tmp_path.absolute())


@pytest.mark.skipif(
    not hasattr(os, "O_NONBLOCK"), reason="platform does not support nonblocking opens"
)
def test_inventory_rejects_regular_file_replaced_with_fifo_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "file.txt"
    target.write_text("safe")
    original_stat = workspace.os.stat
    swapped = False

    def stat_then_replace(path, *args, **kwargs):
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        if path == "file.txt" and not swapped:
            target.unlink()
            os.mkfifo(target)
            swapped = True
        return result

    monkeypatch.setattr(workspace.os, "stat", stat_then_replace)
    with pytest.raises(WorkspaceError, match="nonregular file"):
        inventory_workspace(tmp_path.absolute())


def test_skill_manifest_uses_the_same_skill_bytes_as_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    alpha = "---\nname: alpha\ndescription: alpha description\n---\n"
    beta = "---\nname: beta\ndescription: beta description\n---\n"
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(alpha)
    original_open = workspace.os.open
    skill_opens = 0

    def swap_before_second_skill_open(path, flags, *args, **kwargs):
        nonlocal skill_opens
        if path == "SKILL.md":
            skill_opens += 1
            if skill_opens == 2:
                skill_file.write_text(beta)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace.os, "open", swap_before_second_skill_open)
    snapshot = snapshot_skill(tmp_path.absolute())
    assert skill_opens == 1
    assert snapshot.name == "alpha"
    assert snapshot.manifest == (
        ("SKILL.md", hashlib.sha256(alpha.encode()).hexdigest(), len(alpha)),
    )


def test_skill_yaml_recursion_error_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "SKILL.md").write_text("---\nname: safe\ndescription: safe\n---\n")

    def recurse(_: str):
        raise RecursionError("untrusted secret text")

    monkeypatch.setattr(workspace.yaml, "safe_load", recurse)
    with pytest.raises(WorkspaceError, match="invalid SKILL.md YAML") as error:
        snapshot_skill(tmp_path.absolute())
    assert "untrusted secret text" not in str(error.value)


def test_skill_caps_manifest_and_rejects_credential_paths(tmp_path: Path):
    skill = "---\nname: safe\ndescription: safe\n---\n"
    (tmp_path / "SKILL.md").write_text(skill)
    (tmp_path / "reference.txt").write_text("reference")
    with pytest.raises(WorkspaceError, match="file count"):
        snapshot_skill(tmp_path.absolute(), max_files=1)
    with pytest.raises(WorkspaceError, match="total byte"):
        snapshot_skill(tmp_path.absolute(), max_total_bytes=len(skill))
    (tmp_path / ".env").write_text("secret")
    with pytest.raises(WorkspaceError, match="credential path"):
        snapshot_skill(tmp_path.absolute())


def test_literal_dot_components_are_rejected_before_path_normalization(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text("---\nname: safe\ndescription: safe\n---\n")
    path_with_parent = f"{tmp_path.parent}/../{tmp_path.parent.name}/{tmp_path.name}"
    with pytest.raises(WorkspaceError, match="unsafe path component"):
        inventory_workspace(path_with_parent)
    with pytest.raises(WorkspaceError, match="unsafe path component"):
        snapshot_skill(path_with_parent)
