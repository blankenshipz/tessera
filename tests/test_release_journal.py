"""Real disposable materialized bytes; external quota effects are scripted."""

import errno
import json
import os

import pytest

from tessera_operator.bundle_export import directory_identity
from tessera_operator.maintenance_hold import MaintenanceHold, plan_key
from tessera_operator.release_journal import LIMIT, ReleaseJournal
from tessera_operator.schema_supervised import SupervisedDenied


@pytest.fixture
def journal(tmp_path):
    directory = tmp_path.resolve() / "hold"
    directory.mkdir(mode=0o700)
    return ReleaseJournal(directory, directory_identity(directory))


def tree(directory):
    return {
        p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in directory.iterdir()
        if p.is_file()
    }


def test_four_real_materialized_records_not_sparse_reservations(journal):
    before = sum(p.stat().st_blocks for p in journal.directory.iterdir())
    for name in ("prepared", "claim", "failure", "ack"):
        value = {"payload": "x" * (LIMIT - 128)}
        journal.write(name, value)
        assert journal.read(name) == value
    files = list(journal.directory.iterdir())
    assert len(files) == 5  # retained ack staging hardlink, not duplicate data
    logical = [p for p in files if not p.name.startswith(".")]
    assert sum(p.stat().st_size for p in logical) <= 4 * LIMIT
    assert (journal.directory / ".release.ack.staged").stat().st_ino == (
        journal.directory / "release.ack.json"
    ).stat().st_ino
    # Actual bytes written and physical blocks consumed, not sparse truncate or
    # diskfree interpreted as a reservation. Allocation is <~1MiB per test.
    assert sum(p.stat().st_blocks for p in files) > before
    for path in files:
        assert path.stat().st_blocks > 0
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        journal.write("claim", {})


def test_oversize_and_unknown_record_no_write(journal):
    with pytest.raises(SupervisedDenied, match="bound"):
        journal.write("prepared", {"x": "a" * LIMIT})
    with pytest.raises(SupervisedDenied):
        journal.write("../other", {})
    assert not list(journal.directory.iterdir())


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "directory"])
def test_alias_drift_denied(journal, alias):
    directory = journal.directory
    if alias == "directory":
        directory.rename(directory.with_name("prior"))
        directory.mkdir(mode=0o700)
        with pytest.raises(SupervisedDenied, match="drift"):
            journal.write("prepared", {})
        return
    journal.write("prepared", {})
    path = directory / "release.prepared.json"
    if alias == "hardlink":
        os.link(path, directory / "alias")
    else:
        path.rename(directory / "prior")
        path.symlink_to(directory / "prior")
    with pytest.raises(SupervisedDenied):
        journal.read("prepared")


class LocalHold(MaintenanceHold):
    """Only quota/prerequisite observations scripted; real writer/apply ordering."""

    def binding(self, phase, plan):
        if (self.directory / "release.claim.json").exists():
            raise SupervisedDenied("claimed; no replay")
        return dict(
            phase=phase,
            plan=plan,
            plan_sha256=plan_key(plan),
            command={},
            journal_identity=directory_identity(self.directory),
        )

    def match(self, *args):
        pass

    def quota(self):
        return {"metadata": {"uid": "quota", "resourceVersion": "2"}}

    def retained_custody(self, plan):
        pass


@pytest.fixture
def hold(journal):
    plan = dict(operation="test", target={"pvc_uid": "pvc"}, deadline=100)
    calls = []

    class Transport:
        def call(self, command):
            # Exact fallback and claim bytes must already be fsynced/readable.
            assert journal.read("claim")["plan"] == plan
            assert journal.read("failure")["state"] == "unknown_ack"
            calls.append(command)
            return {"metadata": {"uid": "quota", "resourceVersion": "2"}}

    owner = LocalHold(journal.directory, prerequisites={}, transport=Transport())
    (journal.directory / "hold.ack.json").write_text(json.dumps(dict(uid="quota")))
    owner.prepare("release", plan)
    return owner, plan, calls


def test_exact_hold_write_order_success_and_readonly_reconnect(hold, journal):
    owner, plan, calls = hold
    owner.apply("release", plan)
    before = tree(journal.directory)
    fresh = LocalHold(journal.directory, prerequisites={})
    result = fresh.reconcile("release")
    assert result["state"] == "observed_unconfirmed"
    assert result["ack_observed"] and not result["completion_confirmed"]
    with pytest.raises(SupervisedDenied, match="observed/unconfirmed"):
        fresh.released(plan)
    assert tree(journal.directory) == before
    assert len(calls) == 1
    with pytest.raises(SupervisedDenied, match="no replay"):
        owner.apply("release", plan)


@pytest.mark.parametrize("stage", ["claim", "failure", "ack"])
def test_real_writer_fsync_enospc_retains_unknown_no_replay(hold, journal, monkeypatch, stage):
    owner, plan, calls = hold
    real_fsync = os.fsync

    def enospc(fd):
        # Inject disk-full at fsync of the actual named record, after real bytes
        # have been written. No fake capacity success or erased partial files.
        path = journal.directory / (
            ".release.ack.staged" if stage == "ack" else "release." + stage + ".json"
        )
        if path.exists() and os.fstat(fd).st_ino == path.stat().st_ino:
            raise OSError(errno.ENOSPC, "injected disk full")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", enospc)
    with pytest.raises(OSError) as error:
        owner.apply("release", plan)
    assert error.value.errno == errno.ENOSPC
    assert len(calls) == (1 if stage == "ack" else 0)
    assert (journal.directory / "release.claim.json").exists()
    with pytest.raises(SupervisedDenied, match="no replay"):
        owner.apply("release", plan)
    # A fully written ack whose fsync failed must NOT be treated as durable.
    before = tree(journal.directory)
    fresh = LocalHold(journal.directory, prerequisites={})
    assert fresh.reconcile("release")["state"] == "unknown_ack"
    assert tree(journal.directory) == before


@pytest.mark.parametrize("fault", ["publish", "directory_fsync", "crash_after_publish"])
def test_ack_publication_and_directory_sync_uncertainty(hold, journal, monkeypatch, fault):
    owner, plan, calls = hold
    real_link, real_fsync = os.link, os.fsync

    def link(*args, **kwargs):
        if fault == "publish":
            raise OSError(errno.ENOSPC, "injected publication failure")
        return real_link(*args, **kwargs)

    def sync(fd):
        if (journal.directory / "release.ack.json").exists() and os.fstat(
            fd
        ).st_ino == journal.directory.stat().st_ino:
            if fault == "crash_after_publish":
                raise KeyboardInterrupt
            raise OSError(errno.ENOSPC, "injected directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises((OSError, KeyboardInterrupt)):
        owner.apply("release", plan)
    assert len(calls) == 1
    assert (journal.directory / ".release.ack.staged").exists()
    before = tree(journal.directory)
    fresh = LocalHold(journal.directory, prerequisites={})
    result = fresh.reconcile("release")
    assert result["state"] == ("unknown_ack" if fault == "publish" else "observed_unconfirmed")
    assert not result["completion_confirmed"]
    assert tree(journal.directory) == before
    with pytest.raises(SupervisedDenied, match="no replay"):
        fresh.apply("release", plan)


def test_partial_ack_write_not_published(hold, journal, monkeypatch):
    owner, plan, calls = hold
    original = os.fdopen

    class BrokenStream:
        def __init__(self, fd):
            self.stream = original(fd, "wb")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, raw):
            self.stream.write(raw[: len(raw) // 2])
            self.stream.flush()
            raise OSError(errno.ENOSPC, "injected partial write")

    def opened(fd, mode):
        staged = journal.directory / ".release.ack.staged"
        if staged.exists() and os.fstat(fd).st_ino == staged.stat().st_ino:
            return BrokenStream(fd)
        return original(fd, mode)

    monkeypatch.setattr(os, "fdopen", opened)
    with pytest.raises(OSError):
        owner.apply("release", plan)
    assert len(calls) == 1
    assert not (journal.directory / "release.ack.json").exists()
    before = tree(journal.directory)
    fresh = LocalHold(journal.directory, prerequisites={})
    assert fresh.reconcile("release")["state"] == "unknown_ack"
    assert tree(journal.directory) == before
    with pytest.raises(SupervisedDenied):
        fresh.apply("release", plan)


def test_crash_after_claim_before_effect_retains_unknown(hold, journal, monkeypatch):
    owner, plan, calls = hold
    real = ReleaseJournal.write

    def crash(self, suffix, value):
        if suffix == "failure":
            raise KeyboardInterrupt
        return real(self, suffix, value)

    monkeypatch.setattr(ReleaseJournal, "write", crash)
    with pytest.raises(KeyboardInterrupt):
        owner.apply("release", plan)
    assert not calls
    before = tree(journal.directory)
    assert owner.reconcile("release")["state"] == "unknown_ack"
    assert tree(journal.directory) == before
    with pytest.raises(SupervisedDenied):
        owner.apply("release", plan)
