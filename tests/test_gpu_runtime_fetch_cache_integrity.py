"""A cache entry is deleted only on a confirmed fingerprint mismatch (ADR 0116).

Offline: temporary directories and monkeypatched OS calls. Every other failure is a miss that
leaves the entry in place.
"""

import errno
import hashlib
import importlib.util
import os
import stat
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "gpu_runtime_cache_integrity", ROOT / "workloads/gpu_runtime_v1/runtime.py"
)
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

DATA = b"public-bytes" * 10
DIGEST = hashlib.sha256(DATA).hexdigest()


def pin(cap=1000):
    return {"url": "https://d.example.com/data.bin", "sha256": DIGEST, "max_bytes": cap}


@pytest.fixture
def cache(tmp_path):
    directory = tmp_path / "persist"
    directory.mkdir(mode=0o700)
    entry = directory / DIGEST
    entry.write_bytes(DATA)
    entry.chmod(0o600)
    return rt._FetchCache(str(directory), 100000), entry


@pytest.fixture
def target(tmp_path):
    return str(tmp_path / "work" / "files" / "data.bin")


def hit(cache_obj, target, cap=1000):
    return cache_obj.hit(pin(cap), target, time.monotonic() + 60)


def assert_served(cache_obj, target):
    row = hit(cache_obj, target)
    assert row is not None and row["bytes"] == len(DATA) and row["sha256"] == DIGEST
    assert Path(target).read_bytes() == DATA
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o444


def assert_no_part(target):
    assert not os.path.exists(target + ".part")


@pytest.mark.parametrize("code", [errno.EMFILE, errno.EACCES, errno.EIO])
def test_oserror_at_lstat_is_a_miss_and_keeps_the_entry(cache, target, monkeypatch, code):
    c, entry = cache
    real = os.lstat

    def boom(path, *a, **k):
        if str(path) == str(entry):
            raise OSError(code, "injected")
        return real(path, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(rt.os, "lstat", boom)
        assert hit(c, target) is None
    assert entry.read_bytes() == DATA
    assert_no_part(target)
    assert_served(c, target)


@pytest.mark.parametrize("code", [errno.EMFILE, errno.EACCES, errno.EIO])
def test_oserror_at_open_is_a_miss_and_keeps_the_entry(cache, target, monkeypatch, code):
    c, entry = cache
    real = os.open

    def boom(path, *a, **k):
        if str(path) == str(entry):
            raise OSError(code, "injected")
        return real(path, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(rt.os, "open", boom)
        assert hit(c, target) is None
    assert entry.read_bytes() == DATA
    assert_no_part(target)
    assert_served(c, target)


@pytest.mark.parametrize("code", [errno.EMFILE, errno.EACCES, errno.EIO])
def test_oserror_mid_read_is_a_miss_and_keeps_the_entry(cache, target, monkeypatch, code):
    c, entry = cache
    real = rt.os.fdopen

    class Failing:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, n=-1):
            raise OSError(code, "injected")

    with monkeypatch.context() as m:
        m.setattr(rt.os, "fdopen", lambda fd, mode="r", *a, **k: Failing(real(fd, mode, *a, **k)))
        assert hit(c, target) is None
    assert entry.read_bytes() == DATA
    assert_no_part(target)
    assert_served(c, target)


def test_smaller_max_bytes_is_a_miss_and_keeps_the_entry(cache, target):
    c, entry = cache
    assert hit(c, target, cap=len(DATA) - 1) is None
    assert entry.read_bytes() == DATA
    assert_no_part(target)
    assert_served(c, target)  # the original bound still hits


def test_size_changed_under_the_reader_is_a_miss(cache, target, monkeypatch):
    c, entry = cache
    real = os.lstat

    def understate(path, *a, **k):  # lstat reports a size the open file does not have
        info = real(path, *a, **k)
        if str(path) == str(entry):
            return os.stat_result(
                (
                    info.st_mode,
                    info.st_ino,
                    info.st_dev,
                    info.st_nlink,
                    info.st_uid,
                    info.st_gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
        return info

    with monkeypatch.context() as m:
        m.setattr(rt.os, "lstat", understate)
        assert hit(c, target, cap=len(DATA) - 1) is None
    assert entry.read_bytes() == DATA
    assert_no_part(target)


def test_hard_linked_entry_is_a_miss_and_not_deleted(cache, target, tmp_path):
    c, entry = cache
    link = tmp_path / "other-name"
    os.link(entry, link)
    assert hit(c, target) is None
    assert entry.read_bytes() == DATA and link.read_bytes() == DATA
    assert_no_part(target)
    link.unlink()
    assert_served(c, target)


def test_symlinked_entry_is_a_miss_and_not_deleted(cache, target, tmp_path):
    c, entry = cache
    real = tmp_path / "elsewhere"
    real.write_bytes(DATA)
    entry.unlink()
    entry.symlink_to(real)
    assert hit(c, target) is None
    assert entry.is_symlink() and real.read_bytes() == DATA
    assert_no_part(target)


def test_directory_entry_is_a_miss_and_not_deleted(cache, target):
    c, entry = cache
    entry.unlink()
    entry.mkdir()
    assert hit(c, target) is None
    assert entry.is_dir()
    assert_no_part(target)


@pytest.mark.parametrize("bad", [b"x" * len(DATA), b"short", DATA + b"extra"])
def test_confirmed_mismatch_is_deleted(cache, target, bad):
    c, entry = cache
    entry.write_bytes(bad)
    assert hit(c, target) is None
    assert not entry.exists()
    assert_no_part(target)
    assert not os.path.exists(target)


def test_missing_entry_is_a_plain_miss(cache, target):
    c, entry = cache
    entry.unlink()
    assert hit(c, target) is None
    assert_no_part(target)


def test_deadline_still_raises_and_keeps_entry_and_no_part(cache, target):
    c, entry = cache
    with pytest.raises(rt.Failure):
        c.hit(pin(), target, time.monotonic() - 1)
    assert entry.read_bytes() == DATA
    assert_no_part(target)


def test_discard_skips_an_entry_replaced_since_it_was_opened(cache):
    c, entry = cache
    opened = os.stat(entry)
    replacement = entry.with_name("new")
    replacement.write_bytes(DATA)
    os.replace(replacement, entry)
    c._discard(str(entry), opened)
    assert entry.read_bytes() == DATA
