from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera_control.recovery import backup_state
from tessera_control.storage_retention_backups import (
    BackupRetentionError,
    delete_backup,
    list_backups,
    select_backups,
)
from tessera_control.storage_retention_models import RetentionPolicy
from tessera_control.store import initialize_state

NOW = datetime(2026, 6, 1, tzinfo=UTC)


_TEMPLATE: list[Path] = []


def _template() -> Path:
    """One real `tessera backup` bundle, made once (the only kind restore accepts)."""
    if not _TEMPLATE:
        scratch = Path(tempfile.mkdtemp(prefix="tessera-retention-template-"))
        initialize_state(scratch / "state")
        backup_state(scratch / "state", scratch / "bundle")
        _TEMPLATE.append(scratch / "bundle")
    return _TEMPLATE[0]


def make_bundle(root: Path, n: int, age_days: float, size: int | None = None) -> Path:
    """A restorable bundle with its own id and age (the manifest id and time are free)."""
    bundle = root / f"backup-{n}"
    shutil.copytree(_template(), bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["backup_id"] = f"{n:032x}"
    manifest["created_at"] = (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def policy(**knobs: int) -> RetentionPolicy:
    return RetentionPolicy(**knobs)


def plan(root: Path, pol: RetentionPolicy, free: int = 10**12):
    entries, _ = list_backups(root)
    return select_backups(entries, pol, now=NOW, free_bytes=free)


def deleted(result) -> list[str]:
    return [c.path for c in result if c.decision == "delete"]


def test_list_reports_relative_path_and_size(tmp_path: Path) -> None:
    make_bundle(tmp_path, 1, 1)
    entries, skipped = list_backups(tmp_path)
    assert skipped == []
    (entry,) = entries
    assert entry.path == "backup-1" and entry.kind == "backup" and entry.decision == "keep"
    assert entry.size_bytes > 0 and entry.ref == f"{1:032x}"


def test_a_bundle_restore_would_reject_is_never_a_candidate(tmp_path: Path) -> None:
    """The newest bundle has a stray file and the manifest no longer matches its layout:
    restore rejects it, so it must neither count toward keep-last nor protect itself while
    the older, restorable bundle is deleted."""
    make_bundle(tmp_path, 1, 90)
    make_bundle(tmp_path, 2, 60)
    broken = make_bundle(tmp_path, 3, 1)
    (broken / "extra.bin").write_bytes(b"x")
    entries, skipped = list_backups(tmp_path)
    assert [c.path for c in entries] == ["backup-2", "backup-1"] or sorted(
        c.path for c in entries
    ) == ["backup-1", "backup-2"]
    assert [s["path"] for s in skipped] == ["backup-3"]
    assert "restore would reject" in skipped[0]["reason"]
    result = plan(tmp_path, policy(backup_keep_last=1, backup_max_age_days=30))
    assert deleted(result) == ["backup-1"]  # backup-2 (newest restorable) survives
    assert [c.path for c in result if c.decision == "keep"] == ["backup-2"]


def test_the_last_restorable_backup_is_never_selected(tmp_path: Path) -> None:
    only = make_bundle(tmp_path, 1, 900)
    broken = make_bundle(tmp_path, 2, 1)
    (broken / "snapshot.sqlite3").write_bytes(b"not a database")
    result = plan(tmp_path, policy(backup_max_age_days=1, disk_free_min_bytes=10**15), free=0)
    assert deleted(result) == [] and (only / "manifest.json").exists()


def test_keep_last_floor(tmp_path: Path) -> None:
    for n, age in enumerate([50, 40, 30, 20, 10], start=1):
        make_bundle(tmp_path, n, age)
    result = plan(tmp_path, policy(backup_keep_last=3, backup_max_age_days=5))
    assert [c.path for c in result][:3] == ["backup-5", "backup-4", "backup-3"]
    assert deleted(result) == ["backup-2", "backup-1"]
    assert all(c.reasons == ["keep_last"] for c in result[:3])
    assert all(c.reasons == ["max_age"] for c in result if c.decision == "delete")


def test_newest_always_kept_when_everything_is_old(tmp_path: Path) -> None:
    make_bundle(tmp_path, 1, 400)
    make_bundle(tmp_path, 2, 300)
    result = plan(tmp_path, policy(backup_max_age_days=7))
    assert deleted(result) == ["backup-1"]
    assert result[0].path == "backup-2" and result[0].decision == "keep"


def test_max_age(tmp_path: Path) -> None:
    make_bundle(tmp_path, 1, 40)
    make_bundle(tmp_path, 2, 20)
    make_bundle(tmp_path, 3, 1)
    assert deleted(plan(tmp_path, policy(backup_max_age_days=30))) == ["backup-1"]


def test_disk_pressure_oldest_first_stops_at_threshold(tmp_path: Path) -> None:
    for n, age in enumerate([40, 30, 20, 10, 1], start=1):
        make_bundle(tmp_path, n, age)
    entries, _ = list_backups(tmp_path)
    size = entries[0].size_bytes
    threshold = 10 * size
    pol = policy(backup_keep_last=2, disk_free_min_bytes=threshold)
    result = select_backups(entries, pol, now=NOW, free_bytes=lambda: threshold - 2 * size)
    assert sorted(deleted(result)) == ["backup-1", "backup-2"]
    assert [c.path for c in result if c.decision == "delete"] == ["backup-2", "backup-1"]
    assert all(c.reasons == ["disk_pressure"] for c in result if c.decision == "delete")
    # Not enough to free everything: the floor still holds.
    result = select_backups(entries, pol, now=NOW, free_bytes=0)
    assert sorted(deleted(result)) == ["backup-1", "backup-2", "backup-3"]
    assert {c.path for c in result[:2]} == {"backup-5", "backup-4"}
    assert all(c.decision == "keep" for c in result[:2])
    # Free space is fine: nothing selected.
    assert deleted(select_backups(entries, pol, now=NOW, free_bytes=10**9)) == []


def test_disk_pressure_selects_minimum(tmp_path: Path) -> None:
    for n, age in enumerate([40, 30, 20], start=1):
        make_bundle(tmp_path, n, age)
    entries, _ = list_backups(tmp_path)
    size = entries[0].size_bytes
    result = select_backups(
        entries, policy(disk_free_min_bytes=10 * size), now=NOW, free_bytes=9 * size
    )
    assert deleted(result) == ["backup-1"]


def test_no_knobs_selects_nothing(tmp_path: Path) -> None:
    for n, age in enumerate([400, 300, 200], start=1):
        make_bundle(tmp_path, n, age)
    assert deleted(plan(tmp_path, policy(), free=0)) == []


def test_symlink_incomplete_and_unparsable_skipped(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    make_bundle(root, 1, 1)
    outside = tmp_path / "outside"
    outside.mkdir()
    make_bundle(outside, 9, 1)
    (root / "link").symlink_to(outside / "backup-9", target_is_directory=True)
    (root / "note.txt").write_text("hi")
    (root / "empty").mkdir()
    bad = make_bundle(root, 2, 2)
    (bad / "manifest.json").write_text("{nope")
    trunc = make_bundle(root, 3, 3)
    (trunc / "snapshot.sqlite3").write_bytes(b"short")
    missing = make_bundle(root, 4, 4)
    (missing / "snapshot.sqlite3").unlink()
    entries, skipped = list_backups(root)
    assert [c.path for c in entries] == ["backup-1"]
    assert {s["path"] for s in skipped} == {"link", "empty", "backup-2", "backup-3", "backup-4"}
    assert all(s["reason"] for s in skipped)


def test_delete_removes_bundle_and_returns_receipt(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, 1, 5)
    (entry,), _ = list_backups(tmp_path)
    receipt = delete_backup(tmp_path, entry)
    assert receipt == {
        "ref": entry.ref,
        "path": "backup-1",
        "size_bytes": entry.size_bytes,
        "sha256": entry.sha256,
    }
    assert not bundle.exists()


def test_delete_refuses_changed_snapshot(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, 1, 5)
    (entry,), _ = list_backups(tmp_path)
    (bundle / "snapshot.sqlite3").write_bytes(b"\xff" * 100)  # same size, new digest
    with pytest.raises(BackupRetentionError):
        delete_backup(tmp_path, entry)
    assert bundle.exists()


def test_delete_refuses_changed_manifest(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, 1, 5)
    (entry,), _ = list_backups(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["backup_id"] = "f" * 32
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupRetentionError):
        delete_backup(tmp_path, entry)
    assert bundle.exists()


def test_delete_refuses_symlinked_child(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    real = make_bundle(tmp_path, 1, 5)
    (entry,), _ = list_backups(tmp_path)
    (root / "backup-1").symlink_to(real, target_is_directory=True)
    with pytest.raises(BackupRetentionError):
        delete_backup(root, entry)
    assert (real / "snapshot.sqlite3").exists()


def test_delete_refuses_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    make_bundle(tmp_path, 1, 5)
    (entry,), _ = list_backups(tmp_path)
    from dataclasses import replace

    with pytest.raises(BackupRetentionError):
        delete_backup(root, replace(entry, path="../backup-1"))
    assert (tmp_path / "backup-1").exists()


def test_a_bundle_that_rots_in_place_is_never_trusted_from_an_earlier_pass(tmp_path: Path) -> None:
    """Corruption that keeps a file's size and modification time must not hide behind an
    earlier successful verification: the newest bundle is re-hashed on every plan, so the
    older restorable one is never deleted for it."""
    import os

    make_bundle(tmp_path, 1, 90)
    newest = make_bundle(tmp_path, 2, 60)
    pol = policy(backup_keep_last=1, backup_max_age_days=30)
    assert deleted(plan(tmp_path, pol)) == ["backup-1"]  # both verify; the older one is due
    snapshot = newest / "snapshot.sqlite3"
    before = snapshot.stat()
    data = bytearray(snapshot.read_bytes())
    data[len(data) // 2] ^= 0xFF  # same size
    snapshot.write_bytes(bytes(data))
    os.utime(snapshot, ns=(before.st_atime_ns, before.st_mtime_ns))  # same modification time
    after = snapshot.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    result = plan(tmp_path, pol)
    assert deleted(result) == []  # the only restorable bundle is the older one, and it stays
    entries, skipped = list_backups(tmp_path)
    assert [c.path for c in entries] == ["backup-1"]
    assert [s["path"] for s in skipped] == ["backup-2"]


def test_only_bundles_whose_verdict_matters_are_hashed(tmp_path: Path, monkeypatch) -> None:
    from tessera_control import recovery
    from tessera_control.storage_retention_backups import plan_backups

    for n, age in enumerate([90, 60, 20, 10, 1], start=1):
        make_bundle(tmp_path, n, age)
    calls: list[str] = []
    real = recovery.verify_restorable
    monkeypatch.setattr(
        recovery, "verify_restorable", lambda path: (calls.append(path.name), real(path))[1]
    )
    pol = policy(backup_keep_last=1, backup_max_age_days=30)
    result, skipped = plan_backups(tmp_path, pol, now=NOW, free_bytes=10**12)
    assert deleted(result) == ["backup-2", "backup-1"] and skipped == []
    # the newest (kept floor) and the two selected for deletion: not the two young ones
    assert sorted(calls) == ["backup-1", "backup-2", "backup-5"]
    calls.clear()
    plan_backups(tmp_path, policy(), now=NOW, free_bytes=10**12)  # no knob: only the floor
    assert calls == ["backup-5"]


def test_an_unrestorable_deletion_candidate_is_reported_and_kept(tmp_path: Path) -> None:
    from tessera_control.storage_retention_backups import plan_backups

    make_bundle(tmp_path, 1, 90)
    rotten = make_bundle(tmp_path, 2, 60)
    make_bundle(tmp_path, 3, 1)
    (rotten / "extra.bin").write_bytes(b"x")
    result, skipped = plan_backups(
        tmp_path, policy(backup_keep_last=1, backup_max_age_days=30), now=NOW, free_bytes=10**12
    )
    assert deleted(result) == ["backup-1"]
    assert [s["path"] for s in skipped] == ["backup-2"]


def test_verification_is_tracked_by_directory_not_by_backup_id(tmp_path: Path) -> None:
    """A copied bundle shares its backup id with the original: a corrupt copy selected for
    deletion must still be checked itself, and must not take the good copy out of the pool."""
    from tessera_control.storage_retention_backups import plan_backups

    make_bundle(tmp_path, 1, 90)
    copy = tmp_path / "backup-1-copy"
    shutil.copytree(tmp_path / "backup-1", copy)
    (copy / "snapshot.sqlite3").write_bytes(b"rotted copy")
    make_bundle(tmp_path, 3, 1)
    result, skipped = plan_backups(
        tmp_path, policy(backup_keep_last=1, backup_max_age_days=30), now=NOW, free_bytes=10**12
    )
    assert deleted(result) == ["backup-1"]  # the good original, verified itself
    assert [s["path"] for s in skipped] == ["backup-1-copy"]  # the rotted copy, reported
