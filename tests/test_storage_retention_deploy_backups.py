"""Retention manages the backups `tessera deploy-upgrade` takes: the pre-migration snapshot, its
config copies, and a validated bundle, verified in place (no staged copy of a large backup)."""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_storage_retention_backups import NOW, make_bundle, plan, policy

from tessera_control import deploy_upgrade, recovery
from tessera_control import storage_retention_backups as sb
from tessera_control.store import initialize_state


def make_snapshot(root: Path, age_days: float, *, schema: int = 70, configs: bool = True) -> Path:
    """A real pre-migration snapshot made by the deploy-upgrade command's own code."""
    scratch = Path(tempfile.mkdtemp(prefix="tessera-deploy-state-"))
    initialize_state(scratch / "state")
    database = scratch / "state" / "tessera.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("UPDATE schema_meta SET schema_version=?", (schema,))
    connection.commit()
    connection.close()
    when = NOW - timedelta(days=age_days)
    name = "backup-" + when.strftime("%Y%m%dT%H%M%SZ")
    deploy_upgrade.take_backup(scratch / "state", root / name)
    if configs:
        companion = root / (name + "-configs")
        companion.mkdir(mode=0o700)
        (companion / "config-0.json").write_text("{}")
    shutil.rmtree(scratch)
    return root / name


def deleted(result):
    return [c.path for c in result if c.decision == "delete"]


def test_a_pre_migration_snapshot_is_recognised_with_its_time_and_config_copies(tmp_path):
    snapshot = make_snapshot(tmp_path, 3)
    entries, skipped = sb.list_backups(tmp_path)
    assert skipped == []  # neither "no manifest" nor the config copies directory
    (entry,) = entries
    assert entry.path == snapshot.name and entry.ref == entry.sha256[:32]
    assert entry.created_at == (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_bytes = (snapshot / "snapshot.sqlite3").stat().st_size
    assert entry.size_bytes > snapshot_bytes  # the manifest and the config copies are counted


def test_mixed_formats_are_pruned_together_and_the_config_copies_go_with_a_snapshot(tmp_path):
    old = make_snapshot(tmp_path, 90)
    make_bundle(tmp_path, 1, 60)  # a validated bundle
    newest = make_snapshot(tmp_path, 1)
    result = plan(tmp_path, policy(backup_keep_last=2, backup_max_age_days=30))
    assert deleted(result) == [old.name]
    assert [c.path for c in result if c.decision == "keep"][0] == newest.name
    entry = next(c for c in result if c.path == old.name)
    receipt = sb.delete_backup(tmp_path, entry)
    assert receipt["sha256"] == entry.sha256 and receipt["size_bytes"] == entry.size_bytes
    assert not old.exists() and not (tmp_path / (old.name + "-configs")).exists()
    assert newest.exists() and (tmp_path / (newest.name + "-configs")).exists()


def test_keep_last_never_deletes_the_last_restorable_backup(tmp_path):
    """The newest snapshot is corrupt, so the older restorable one is the only restorable
    backup: it stays, however old it is and whatever the disk reading says."""
    only = make_snapshot(tmp_path, 400)
    broken = make_snapshot(tmp_path, 1)
    with (broken / "snapshot.sqlite3").open("r+b") as handle:  # same size, flipped byte
        handle.seek(4096)
        byte = handle.read(1)
        handle.seek(4096)
        handle.write(bytes([byte[0] ^ 0xFF]))
    result = plan(
        tmp_path, policy(backup_keep_last=1, backup_max_age_days=1, disk_free_min_bytes=10**15), 0
    )
    assert deleted(result) == []
    entries, skipped = sb.list_backups(tmp_path)
    assert [e.path for e in entries] == [only.name]
    assert [s["path"] for s in skipped] == [broken.name]
    assert "snapshot does not match its manifest" in skipped[0]["reason"]


def test_keep_last_two_keeps_the_two_newest_restorable_across_formats(tmp_path):
    a = make_snapshot(tmp_path, 100)
    make_bundle(tmp_path, 1, 80)
    b = make_snapshot(tmp_path, 60)
    broken = make_snapshot(tmp_path, 1)
    (broken / "snapshot.sqlite3").write_bytes(b"not a database")
    result = plan(tmp_path, policy(backup_keep_last=2, backup_max_age_days=30))
    kept = [c.path for c in result if c.decision == "keep"]
    assert kept == [b.name, "backup-1"]  # the broken newest does not use up the floor
    assert deleted(result) == [a.name]


def test_a_snapshot_directory_with_extra_files_or_the_wrong_schema_is_skipped(tmp_path):
    extra = make_snapshot(tmp_path, 5)
    (extra / "notes.txt").write_text("x")
    wrong = make_snapshot(tmp_path, 4, configs=False)
    connection = sqlite3.connect(wrong / "snapshot.sqlite3")
    connection.execute("UPDATE schema_meta SET schema_version=71")
    connection.commit()
    connection.close()
    entries, skipped = sb.list_backups(tmp_path)
    assert entries == [] and {s["path"] for s in skipped} == {extra.name, wrong.name}


def test_a_bundle_is_verified_in_place_without_staging_a_copy(tmp_path, monkeypatch):
    """Restore stages a full copy in a temporary directory; a retention check of a multi-gigabyte
    backup must not (the staged copy filled the disk and raised OSError). With no usable
    temporary directory the check still passes."""
    bundle = make_bundle(tmp_path, 1, 1)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "no-such-temp-dir"))
    assert recovery.verify_restorable(bundle)["backup_id"] == f"{1:032x}"
    (entry,), skipped = sb.list_backups(tmp_path)
    assert skipped == [] and entry.path == bundle.name


def test_an_in_place_check_still_rejects_what_restore_rejects(tmp_path):
    bundle = make_bundle(tmp_path, 1, 1)
    (bundle / "extra.bin").write_bytes(b"x")
    with pytest.raises(recovery.StoreError):
        recovery.verify_restorable(bundle)
    other = make_bundle(tmp_path, 2, 1)
    with (other / "snapshot.sqlite3").open("r+b") as handle:
        handle.seek(8192)
        handle.write(b"\xff")
    with pytest.raises(recovery.StoreError):
        recovery.verify_restorable(other)


def test_an_os_error_reason_names_the_errno_and_never_a_path():
    error = OSError(errno.ENOSPC, "No space left on device", "/private/tmp/x")
    reason = sb._why(error)
    assert reason == "OSError ENOSPC" and "/private" not in reason
    assert sb._why(ValueError("secret detail")) == "ValueError"


def test_a_stray_config_copies_directory_with_no_backup_is_reported(tmp_path):
    (tmp_path / "backup-20260101T000000Z-configs").mkdir()
    entries, skipped = sb.list_backups(tmp_path)
    assert entries == [] and [s["reason"] for s in skipped] == ["no manifest"]


def test_only_the_snapshot_and_its_manifest_are_in_a_fresh_deploy_snapshot(tmp_path):
    snapshot = make_snapshot(tmp_path, 2, configs=False)
    assert sorted(os.listdir(snapshot)) == ["snapshot-manifest.json", "snapshot.sqlite3"]
    assert datetime.now(UTC) > NOW - timedelta(days=3)  # sanity: the stamp is in the past


# --- review follow-ups -------------------------------------------------------------------------


def make_wal_snapshot(root: Path, age_days: float) -> Path:
    """A deploy snapshot taken from a database in WAL mode (it keeps the WAL header)."""
    scratch = Path(tempfile.mkdtemp(prefix="tessera-deploy-state-"))
    initialize_state(scratch / "state")
    database = scratch / "state" / "tessera.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("UPDATE schema_meta SET schema_version=70")
    connection.commit()
    connection.close()
    name = "backup-" + (NOW - timedelta(days=age_days)).strftime("%Y%m%dT%H%M%SZ")
    deploy_upgrade.take_backup(scratch / "state", root / name)
    shutil.rmtree(scratch)
    return root / name


def test_checking_a_wal_mode_snapshot_writes_nothing_into_the_backup(tmp_path):
    snapshot = make_wal_snapshot(tmp_path, 3)
    before = sorted(os.listdir(snapshot))
    assert before == ["snapshot-manifest.json", "snapshot.sqlite3"]
    for _ in range(3):  # a second pass used to find -wal/-shm files and reject it
        entries, skipped = sb.list_backups(tmp_path)
        assert [e.path for e in entries] == [snapshot.name] and skipped == []
        assert sorted(os.listdir(snapshot)) == before
    assert sb.verify_all(tmp_path)["restorable"] == 1


def test_a_snapshot_schema_newer_than_this_release_is_not_restorable(tmp_path):
    make_snapshot(tmp_path, 3, schema=10_000, configs=False)
    entries, skipped = sb.list_backups(tmp_path)
    assert entries == [] and "newer than this release" in skipped[0]["reason"]


def test_a_config_directory_with_unexpected_contents_is_not_removed_with_the_backup(tmp_path):
    snapshot = make_snapshot(tmp_path, 90)
    make_snapshot(tmp_path, 1)
    companion = tmp_path / (snapshot.name + "-configs")
    (companion / "notes.txt").write_text("something an operator kept here")
    entries, skipped = sb.list_backups(tmp_path)
    assert [s["path"] for s in skipped] == [companion.name]  # reported, not hidden
    old = next(e for e in entries if e.path == snapshot.name)
    sb.delete_backup(tmp_path, old)
    assert not snapshot.exists() and companion.exists()  # never touched


def test_a_bundle_named_like_a_config_directory_is_not_hidden_or_removed(tmp_path):
    make_bundle(tmp_path, 1, 1)
    bundle = make_bundle(tmp_path, 2, 2)
    renamed = bundle.rename(tmp_path / "backup-20260101T000000Z-configs")
    make_snapshot_dir = make_snapshot(tmp_path, 400, configs=False)
    sibling = tmp_path / "backup-20260101T000000Z"
    sibling.mkdir()
    shutil.copy(make_snapshot_dir / "snapshot.sqlite3", sibling / "snapshot.sqlite3")
    shutil.copy(make_snapshot_dir / "snapshot-manifest.json", sibling / "snapshot-manifest.json")
    entries, _ = sb.list_backups(tmp_path)
    assert renamed.name in {e.path for e in entries}  # a real bundle, listed and countable
    snap = next(e for e in entries if e.path == sibling.name)
    sb.delete_backup(tmp_path, snap)
    assert renamed.exists()  # its name does not make it the snapshot's config copies


def test_a_symlinked_config_directory_is_not_followed(tmp_path):
    snapshot = make_snapshot(tmp_path, 90, configs=False)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / "config-0.json").write_text("{}")
    (tmp_path / (snapshot.name + "-configs")).symlink_to(outside, target_is_directory=True)
    make_snapshot(tmp_path, 1)
    entries, skipped = sb.list_backups(tmp_path)
    assert any(s["reason"] == "symlink" for s in skipped)
    sb.delete_backup(tmp_path, next(e for e in entries if e.path == snapshot.name))
    assert (outside / "config-0.json").exists()
    shutil.rmtree(outside)


def test_a_failed_config_removal_is_reported_honestly_after_the_backup_is_gone(
    tmp_path, monkeypatch
):
    snapshot = make_snapshot(tmp_path, 90)
    make_snapshot(tmp_path, 1)
    entries, _ = sb.list_backups(tmp_path)
    old = next(e for e in entries if e.path == snapshot.name)
    real = shutil.rmtree

    def flaky(path, *args, **kwargs):
        if str(path).endswith("-configs"):
            raise OSError(errno.EACCES, "denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(sb.shutil, "rmtree", flaky)
    result = sb.delete_backup(tmp_path, old)
    assert not snapshot.exists()  # the backup is gone
    assert result["companion"] == "not removed: PermissionError EACCES"
    assert result["size_bytes"] < old.size_bytes  # only what was actually freed
    assert (tmp_path / (snapshot.name + "-configs")).exists()
    assert "/" not in result["companion"]
