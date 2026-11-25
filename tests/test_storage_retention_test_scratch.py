"""Test scratch (``.pytest-tmp``) in the storage retention policy: planned, kept, removed safely."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tessera_control import storage_retention
from tessera_control.storage_retention_models import POLICY_FILE, RetentionPolicy
from tessera_control.storage_retention_status import retention_status
from tessera_control.storage_retention_test_scratch import (
    ScratchError,
    default_root,
    delete_test_scratch,
    plan_test_scratch,
)

HOUR = 3600


def _age(path: Path, seconds: float) -> None:
    """Make every file and directory of the tree look ``seconds`` old."""
    moment = time.time() - seconds
    for directory, names, files in os.walk(path, followlinks=False):
        for name in [*names, *files]:
            full = os.path.join(directory, name)
            if not os.path.islink(full):
                os.utime(full, (moment, moment))
        os.utime(directory, (moment, moment))


def _basetemp(root: Path, name: str, *, age: float, size: int = 100) -> Path:
    path = root / name
    (path / "sub").mkdir(parents=True)
    (path / "sub" / "data.bin").write_bytes(b"x" * size)
    (path / "top.txt").write_text("t")
    _age(path, age)
    return path


@pytest.fixture
def world(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    root = tmp_path / "repo" / ".pytest-tmp"
    root.mkdir(parents=True)

    def policy(**extra):
        document = {"reclaim_test_scratch": True, "test_scratch_root": str(root), **extra}
        (state / POLICY_FILE).write_text(json.dumps(document))
        return RetentionPolicy.model_validate(document)

    return state, root, policy


def _plan(policy):
    return plan_test_scratch(policy, now=datetime.now(UTC))


def test_an_old_basetemp_is_planned_for_deletion_and_a_recent_one_is_kept(world):
    _, root, policy = world
    _basetemp(root, "old-run", age=48 * HOUR, size=1000)
    _basetemp(root, "recent-run", age=600)
    _basetemp(root, "middle-run", age=5 * HOUR)
    found, skipped = _plan(policy())
    by_ref = {c.ref: c for c in found}
    assert skipped == []
    assert (by_ref["old-run"].decision, by_ref["old-run"].reasons) == ("delete", ["max_age"])
    assert by_ref["old-run"].size_bytes == 1001  # data.bin + top.txt
    assert (by_ref["recent-run"].decision, by_ref["recent-run"].reasons) == (
        "keep",
        ["recent_activity"],
    )
    assert (by_ref["middle-run"].decision, by_ref["middle-run"].reasons) == (
        "keep",
        ["within_max_age"],
    )
    assert all(c.kind == "test_scratch" and c.path == c.ref for c in found)


def test_the_max_age_is_configurable(world):
    _, root, policy = world
    _basetemp(root, "five-hours", age=5 * HOUR)
    [kept] = _plan(policy())[0]
    assert kept.decision == "keep"
    [chosen] = _plan(policy(test_scratch_max_age_hours=4))[0]
    assert chosen.decision == "delete"


def test_the_class_is_opt_in_and_an_absent_root_is_a_no_op(world, tmp_path):
    _, root, policy = world
    _basetemp(root, "old-run", age=48 * HOUR)
    assert plan_test_scratch(RetentionPolicy(), now=datetime.now(UTC)) == ([], [])
    absent = RetentionPolicy(
        reclaim_test_scratch=True, test_scratch_root=str(tmp_path / "none" / ".pytest-tmp")
    )
    assert plan_test_scratch(absent, now=datetime.now(UTC)) == ([], [])


def test_apply_removes_the_old_basetemp_and_keeps_the_recent_one(world):
    state, root, policy = world
    policy()
    old = _basetemp(root, "old-run", age=48 * HOUR, size=500)
    recent = _basetemp(root, "recent-run", age=600)
    plan = storage_retention.run(state, mode="dry_run", trigger="manual")
    assert [c["ref"] for c in plan["plan"]["candidates"] if c["decision"] == "delete"] == [
        "old-run"
    ]
    receipt = storage_retention.run(
        state, mode="apply", trigger="manual", expect_plan_digest=plan["plan_digest"]
    )
    assert receipt["status"] == "complete" and receipt["failed"] == []
    assert [d["ref"] for d in receipt["deleted"]] == ["old-run"]
    assert receipt["bytes_freed"] == 501
    assert not old.exists() and recent.exists() and root.exists()


def test_a_changed_tree_makes_the_plan_stale_and_nothing_is_deleted(world):
    state, root, policy = world
    policy()
    old = _basetemp(root, "old-run", age=48 * HOUR)
    plan = storage_retention.run(state, mode="dry_run", trigger="manual")
    (old / "late.txt").write_text("more")  # the tree changed after the plan was listed
    _age(old, 48 * HOUR)
    with pytest.raises(storage_retention.RetentionError, match="plan changed"):
        storage_retention.run(
            state, mode="apply", trigger="manual", expect_plan_digest=plan["plan_digest"]
        )
    assert old.exists()


def test_a_basetemp_touched_after_the_plan_is_kept_at_apply(world):
    _, root, policy = world
    candidate = _plan(policy())[0]
    assert candidate == []
    old = _basetemp(root, "old-run", age=48 * HOUR)
    [candidate] = _plan(policy())[0]
    (old / "now.txt").write_text("a run started using it")  # fresh mtime
    with pytest.raises(ScratchError, match="changed recently"):
        delete_test_scratch(root, candidate)
    assert old.exists()


def test_read_only_directories_are_made_writable_and_removed(world):
    _, root, policy = world
    old = _basetemp(root, "old-run", age=48 * HOUR)
    (old / "sub").chmod(0o500)
    (old / "sub" / "data.bin").chmod(0o400)
    _age(old, 48 * HOUR)
    [candidate] = _plan(policy())[0]
    delete_test_scratch(root, candidate)
    assert not old.exists()


def test_a_symlink_child_is_refused_and_never_followed(world, tmp_path):
    _, root, policy = world
    outside = tmp_path / "outside"
    (outside / "precious").mkdir(parents=True)
    (outside / "precious" / "keep.txt").write_text("do not delete")
    _age(outside, 99 * HOUR)
    (root / "link").symlink_to(outside, target_is_directory=True)
    found, skipped = _plan(policy())
    assert found == []
    assert skipped == [
        {
            "kind": "test_scratch",
            "ref": "link",
            "reason": "not a plain directory inside .pytest-tmp",
        }
    ]
    from tessera_control.storage_retention_models import Candidate

    forged = Candidate("test_scratch", "link", "link", 1, "0" * 64, "x", "delete", ["max_age"])
    with pytest.raises(ScratchError, match="not a plain directory"):
        delete_test_scratch(root, forged)
    assert (outside / "precious" / "keep.txt").read_text() == "do not delete"


def test_an_escaping_or_root_candidate_is_refused(world, tmp_path):
    _, root, _ = world
    victim = tmp_path / "repo" / "victim"
    victim.mkdir()
    (victim / "file").write_text("x")
    from tessera_control.storage_retention_models import Candidate

    for ref in ("..", ".", "../victim", "sub/../../victim", ""):
        forged = Candidate("test_scratch", ref, ref, 1, "0" * 64, "x", "delete", ["max_age"])
        with pytest.raises(ScratchError):
            delete_test_scratch(root, forged)
    assert victim.exists() and root.exists()


def test_a_root_not_named_pytest_tmp_is_refused(world, tmp_path):
    _, _, _ = world
    other = tmp_path / "somewhere-else"
    other.mkdir()
    _basetemp(other, "old-run", age=48 * HOUR)
    found, skipped = plan_test_scratch(
        RetentionPolicy(reclaim_test_scratch=True, test_scratch_root=str(other)),
        now=datetime.now(UTC),
    )
    assert found == [] and skipped[0]["reason"].startswith("the test scratch root is not")
    from tessera_control.storage_retention_models import Candidate

    forged = Candidate("test_scratch", "old-run", "old-run", 1, "0" * 64, "x", "delete", [])
    with pytest.raises(ScratchError, match="not a plain .pytest-tmp"):
        delete_test_scratch(other, forged)
    assert (other / "old-run").exists()


def test_a_removal_failure_is_recorded_and_the_rest_still_run(world):
    state, root, policy = world
    policy()
    _basetemp(root, "a-old", age=48 * HOUR)
    _basetemp(root, "b-old", age=48 * HOUR)
    plan = storage_retention.run(state, mode="dry_run", trigger="manual")

    def flaky(scratch_root, candidate):
        if candidate.ref == "a-old":
            raise ScratchError("the scratch directory could not be removed")
        return delete_test_scratch(scratch_root, candidate)

    receipt = storage_retention.run(
        state,
        mode="apply",
        trigger="manual",
        expect_plan_digest=plan["plan_digest"],
        delete_scratch=flaky,
    )
    assert receipt["status"] == "failed"
    assert [f["ref"] for f in receipt["failed"]] == ["a-old"]
    assert receipt["failed"][0]["reason"] == "the scratch directory could not be removed"
    assert [d["ref"] for d in receipt["deleted"]] == ["b-old"]
    assert (root / "a-old").exists() and not (root / "b-old").exists()


def test_the_class_shows_in_the_plan_receipt_and_the_status(world):
    state, root, policy = world
    policy(test_scratch_max_age_hours=12)
    _basetemp(root, "old-run", age=48 * HOUR, size=300)
    receipt = storage_retention.run(state, mode="dry_run", trigger="manual")
    kinds = {c["kind"] for c in receipt["plan"]["candidates"]}
    assert kinds == {"test_scratch"}
    status = retention_status(state)
    assert status["test_scratch"] == {"enabled": True, "max_age_hours": 12}
    assert status["pending_dry_run"]["deletions_planned"] == 1
    assert status["pending_dry_run"]["bytes_reclaimable"] == 301


def test_the_default_root_is_the_repository_pytest_tmp():
    assert default_root().name == ".pytest-tmp"
    assert (default_root().parent / "src" / "tessera_control").is_dir()
