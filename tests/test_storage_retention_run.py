from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_storage_retention_backups import NOW, make_bundle

from tessera_control import cli, storage_retention
from tessera_control.storage_retention_models import POLICY_FILE, RECEIPT_DIR


def setup(tmp_path: Path, **knobs):
    state = tmp_path / "state"
    root = tmp_path / "backups"
    state.mkdir()
    root.mkdir()
    for n, age in ((1, 90), (2, 60), (3, 1)):
        make_bundle(root, n, age)
    (state / POLICY_FILE).write_text(json.dumps({"backup_root": str(root), **knobs}))
    return state, root


def receipts(state: Path):
    return [json.loads(p.read_text()) for p in sorted((state / RECEIPT_DIR).glob("*.json"))]


def test_no_policy_selects_nothing_and_apply_is_refused(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    assert (
        storage_retention.run(state, mode="dry_run", trigger="manual")["plan"]["candidates"] == []
    )
    with pytest.raises(storage_retention.RetentionError):
        storage_retention.run(state, mode="apply", trigger="manual", expect_plan_digest="x")


def test_dry_run_deletes_nothing_and_leaves_a_receipt(tmp_path):
    state, root = setup(tmp_path, backup_max_age_days=30)
    receipt = storage_retention.run(state, mode="dry_run", trigger="manual", now=NOW)
    assert len(list(root.iterdir())) == 3 and receipt["deleted"] == []
    assert len(receipts(state)) == 1 and receipts(state)[0]["status"] == "complete"
    assert [c["ref"] for c in receipt["plan"]["candidates"] if c["decision"] == "delete"]


def test_apply_needs_the_listed_digest_then_receipts_size_and_digest(tmp_path):
    state, root = setup(tmp_path, backup_max_age_days=30)
    plan = storage_retention.run(state, mode="dry_run", trigger="manual", now=NOW)
    with pytest.raises(storage_retention.RetentionError):
        storage_retention.run(state, mode="apply", trigger="manual", now=NOW)  # no digest
    with pytest.raises(storage_retention.RetentionError):
        storage_retention.run(
            state, mode="apply", trigger="manual", expect_plan_digest="0" * 64, now=NOW
        )
    assert len(list(root.iterdir())) == 3  # refusals deleted nothing
    done = storage_retention.run(
        state, mode="apply", trigger="manual", expect_plan_digest=plan["plan_digest"], now=NOW
    )
    assert sorted(p.name for p in root.iterdir()) == ["backup-3"]
    assert done["status"] == "complete" and len(done["deleted"]) == 2
    assert all(d["size_bytes"] > 0 and len(d["sha256"]) == 64 for d in done["deleted"])
    assert done["bytes_freed"] == sum(d["size_bytes"] for d in done["deleted"])
    assert done["run_id"] in {r["run_id"] for r in receipts(state)}


def test_a_changed_plan_is_not_applied(tmp_path):
    state, root = setup(tmp_path, backup_max_age_days=30)
    plan = storage_retention.run(state, mode="dry_run", trigger="manual", now=NOW)
    make_bundle(root, 4, 100)  # a new old backup appears after the listing
    with pytest.raises(storage_retention.RetentionError):
        storage_retention.run(
            state, mode="apply", trigger="manual", expect_plan_digest=plan["plan_digest"], now=NOW
        )
    assert len(list(root.iterdir())) == 4


def test_a_refused_deletion_is_receipted_and_does_not_stop_the_rest(tmp_path):
    state, root = setup(tmp_path, backup_max_age_days=30)
    plan = storage_retention.run(state, mode="dry_run", trigger="manual", now=NOW)

    def flaky(root_, candidate):
        if candidate.ref.endswith("1"):
            raise OSError("boom")
        return {"size_bytes": candidate.size_bytes, "sha256": candidate.sha256}

    done = storage_retention.run(
        state,
        mode="apply",
        trigger="manual",
        expect_plan_digest=plan["plan_digest"],
        now=NOW,
        delete_backup=flaky,
    )
    assert done["status"] == "failed" and len(done["deleted"]) == 1
    assert done["failed"][0]["error"] == "OSError"


def test_timer_reports_but_only_applies_with_auto_apply(tmp_path):
    state, root = setup(tmp_path, backup_max_age_days=30)
    digest = storage_retention.tick(state, None)
    assert digest and len(list(root.iterdir())) == 3
    assert [r["mode"] for r in receipts(state)].count("dry_run") == 1
    assert storage_retention.tick(state, digest) == digest
    assert [r["mode"] for r in receipts(state)].count("dry_run") == 1  # not re-reported
    with pytest.raises(storage_retention.RetentionError):
        storage_retention.run(state, mode="apply", trigger="timer", expect_plan_digest=digest)
    (state / POLICY_FILE).write_text(
        json.dumps({"backup_root": str(root), "backup_max_age_days": 30, "auto_apply": True})
    )
    storage_retention.tick(state, digest)
    assert sorted(p.name for p in root.iterdir()) == ["backup-3"]
    assert receipts(state)[-1]["trigger"] == "timer"


def test_an_invalid_policy_is_refused_not_ignored(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    (state / POLICY_FILE).write_text('{"backup_keep_last": 0}')
    with pytest.raises(storage_retention.RetentionError):
        storage_retention.load_policy(state)


def test_cli_plan_then_apply(tmp_path, monkeypatch, capsys):
    state, root = setup(tmp_path, backup_max_age_days=1)
    monkeypatch.setattr("sys.argv", ["tessera", "retention", "plan", "--state-dir", str(state)])
    assert cli.main() == 0
    digest = json.loads(capsys.readouterr().out)["plan_digest"]
    monkeypatch.setattr("sys.argv", ["tessera", "retention", "apply", "--state-dir", str(state)])
    assert cli.main() == 1  # no digest
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "retention", "apply", "--state-dir", str(state), "--plan-digest", digest],
    )
    capsys.readouterr()
    assert cli.main() == 0
    assert sorted(p.name for p in root.iterdir()) == ["backup-3"]


def test_a_refused_apply_leaves_a_receipt_with_the_reason(tmp_path):
    state, root = setup(tmp_path, backup_max_age_days=30)
    with pytest.raises(storage_retention.RetentionError):
        storage_retention.run(
            state, mode="apply", trigger="manual", expect_plan_digest="0" * 64, now=NOW
        )
    (receipt,) = receipts(state)
    assert receipt["status"] == "refused" and "plan changed" in receipt["reason"]
    assert receipt["deleted"] == [] and len(list(root.iterdir())) == 3


def test_a_kept_bundle_that_rots_between_plan_and_delete_stops_the_apply(tmp_path, monkeypatch):
    state, root = setup(tmp_path, backup_max_age_days=30)
    plan = storage_retention.run(state, mode="dry_run", trigger="manual", now=NOW)
    real = storage_retention.build_plan

    def plan_then_rot(*args, **kwargs):
        result = real(*args, **kwargs)
        snapshot = root / "backup-3" / "snapshot.sqlite3"
        data = bytearray(snapshot.read_bytes())
        data[len(data) // 2] ^= 0xFF  # the newest bundle rots after it was verified
        snapshot.write_bytes(bytes(data))
        return result

    monkeypatch.setattr(storage_retention, "build_plan", plan_then_rot)
    with pytest.raises(storage_retention.RetentionError, match="no longer verifies"):
        storage_retention.run(
            state, mode="apply", trigger="manual", expect_plan_digest=plan["plan_digest"], now=NOW
        )
    assert len(list(root.iterdir())) == 3  # nothing was deleted
    (refused,) = [r for r in receipts(state) if r["mode"] == "apply"]
    assert refused["status"] == "refused" and "kept bundle no longer verifies" in refused["reason"]
    assert refused["deleted"] == []


def test_any_kept_bundle_that_rots_stops_the_apply_not_only_all_of_them(tmp_path, monkeypatch):
    state, root = setup(tmp_path, backup_max_age_days=30, backup_keep_last=2)
    for n, age in ((4, 90),):
        make_bundle(root, n, age)
    plan = storage_retention.run(state, mode="dry_run", trigger="manual", now=NOW)
    real = storage_retention.build_plan

    def plan_then_rot_one(*args, **kwargs):
        result = real(*args, **kwargs)
        snapshot = root / "backup-2" / "snapshot.sqlite3"  # one of the two kept; backup-3 is fine
        data = bytearray(snapshot.read_bytes())
        data[len(data) // 2] ^= 0xFF
        snapshot.write_bytes(bytes(data))
        return result

    monkeypatch.setattr(storage_retention, "build_plan", plan_then_rot_one)
    with pytest.raises(storage_retention.RetentionError, match="kept bundle no longer verifies"):
        storage_retention.run(
            state, mode="apply", trigger="manual", expect_plan_digest=plan["plan_digest"], now=NOW
        )
    assert len(list(root.iterdir())) == 4


def test_the_sweep_shows_a_rotted_young_bundle_that_a_plan_never_reads(tmp_path):
    from tessera_control.storage_retention_status import retention_status

    state, root = setup(tmp_path, backup_max_age_days=30)
    young = make_bundle(root, 5, 20)  # inside the window: a plan does not hash it
    (young / "snapshot.sqlite3").write_bytes(b"rotted")
    plan = storage_retention.run(state, mode="dry_run", trigger="manual", now=NOW)
    kinds = {c["path"]: c["reasons"] for c in plan["plan"]["candidates"]}
    assert kinds["backup-5"] == ["unverified"]  # labelled, not silently "fine"
    swept = storage_retention.verify(state)
    assert [u["path"] for u in swept["unrestorable"]] == ["backup-5"]
    status = retention_status(state)["backups"]
    assert status["unrestorable"] == 1 and status["restorable"] == 3
    # the timer runs the sweep when one is due, and not again before the interval
    for path in (state / RECEIPT_DIR).glob("*.json"):
        path.unlink()
    storage_retention.tick(state, None)
    assert [r["mode"] for r in receipts(state)].count("verify") == 1
    storage_retention.tick(state, None)
    assert [r["mode"] for r in receipts(state)].count("verify") == 1


def test_a_sweep_of_an_unreadable_backup_root_fails_loudly(tmp_path):
    from tessera_control.storage_retention_status import retention_status

    state, root = setup(tmp_path, backup_max_age_days=30)
    for child in list(root.iterdir()):
        shutil.rmtree(child)
    root.rmdir()  # the configured root is gone
    receipt = storage_retention.verify(state)
    assert receipt["status"] == "failed" and "unreadable" in receipt["error"]
    assert retention_status(state)["backups"]["last_sweep_status"] == "failed"


def test_a_failing_sweep_does_not_block_the_plan(tmp_path, monkeypatch):
    state, root = setup(tmp_path, backup_max_age_days=30)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage_retention, "verify", broken)
    digest = storage_retention.tick(state, None)
    assert digest  # the plan was still built and reported
    assert [r["mode"] for r in receipts(state)] == ["dry_run"]


def test_sweep_receipts_are_pruned_and_a_plan_flood_does_not_trigger_an_early_sweep(tmp_path):
    state, root = setup(tmp_path, backup_max_age_days=30)
    for _ in range(storage_retention.VERIFY_KEEP + 3):
        storage_retention.verify(state)
    sweeps = list((state / RECEIPT_DIR).glob("*.verify.json"))
    assert len(sweeps) == storage_retention.VERIFY_KEEP
    for _ in range(70):  # many newer plan receipts
        storage_retention.run(state, mode="dry_run", trigger="manual")
    policy = storage_retention.load_policy(state)
    assert storage_retention._verify_due(state, policy, datetime.now(UTC)) is False
