"""The local fetch cache serves the bytes it verified and discards only on a real mismatch."""

from __future__ import annotations

import errno
import hashlib
import os
import stat

import pytest

from tessera_worker import public_fetch as pf

GOOD = b"good bytes " * 100
SHA = hashlib.sha256(GOOD).hexdigest()


def _setup(tmp_path, data=GOOD, max_bytes=10_000):
    cache, dest = tmp_path / "cache", tmp_path / "dest"
    cache.mkdir()
    dest.mkdir()
    (cache / SHA).write_bytes(data)
    pin = {"name": "f.bin", "sha256": SHA, "max_bytes": max_bytes, "url": "https://x.example/f"}
    return cache, dest, pin, dest / "f.bin"


def _clean(dest):
    assert sorted(p.name for p in dest.iterdir()) in ([], ["f.bin"])


def test_hit_returns_size_copy_0444_no_temp(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    assert pf._from_cache(cache, pin, final) == len(GOOD)
    assert final.read_bytes() == GOOD
    assert stat.S_IMODE(final.stat().st_mode) == 0o444
    assert final.stat().st_ino != (cache / SHA).stat().st_ino
    assert [p.name for p in dest.iterdir()] == ["f.bin"]


def test_miss_returns_none(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    (cache / SHA).unlink()
    assert pf._from_cache(cache, pin, final) is None
    assert list(dest.iterdir()) == []


def test_corrupt_entry_removed(tmp_path):
    cache, dest, pin, final = _setup(tmp_path, data=b"different")
    assert pf._from_cache(cache, pin, final) is None
    assert not (cache / SHA).exists()
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize("err", [errno.EMFILE, errno.EACCES, errno.EIO])
def test_io_error_on_open_keeps_entry(tmp_path, monkeypatch, err):
    cache, dest, pin, final = _setup(tmp_path)
    real = os.open

    def boom(path, *a, **k):
        if str(path).startswith(str(cache)):
            raise OSError(err, "boom")
        return real(path, *a, **k)

    monkeypatch.setattr(pf.os, "open", boom)
    assert pf._from_cache(cache, pin, final) is None
    monkeypatch.undo()
    assert (cache / SHA).read_bytes() == GOOD
    assert list(dest.iterdir()) == []


def test_permission_error_on_read_keeps_entry(tmp_path, monkeypatch):
    cache, dest, pin, final = _setup(tmp_path)
    real_fdopen = os.fdopen

    class Bad:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

        def fileno(self):
            return self.f.fileno()

        def read(self, n):
            raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(
        pf.os,
        "fdopen",
        lambda fd, mode="r", *a, **k: (
            Bad(real_fdopen(fd, mode, *a, **k)) if mode == "rb" else real_fdopen(fd, mode, *a, **k)
        ),
    )
    assert pf._from_cache(cache, pin, final) is None
    monkeypatch.undo()
    assert (cache / SHA).read_bytes() == GOOD
    assert list(dest.iterdir()) == []


def test_symlinked_entry_is_miss_and_kept(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    real = tmp_path / "real"
    (cache / SHA).rename(real)
    (cache / SHA).symlink_to(real)
    assert pf._from_cache(cache, pin, final) is None
    assert (cache / SHA).is_symlink() and real.read_bytes() == GOOD
    assert list(dest.iterdir()) == []


def test_non_regular_entry_is_miss_and_kept(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    (cache / SHA).unlink()
    (cache / SHA).mkdir()
    assert pf._from_cache(cache, pin, final) is None
    assert (cache / SHA).is_dir()


def _within(seconds, call, fifo=None):
    """Run ``call`` in a thread; fail (instead of hanging the suite) when it does not return."""
    import threading

    box = {}
    thread = threading.Thread(target=lambda: box.update(value=call()), daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive() and fifo is not None:
        # Let a regressed (blocking) open finish, so the failure is reported instead of a hang.
        release = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        thread.join(5)
        os.close(release)
    assert not thread.is_alive(), "the cache lookup hung on a non-regular entry"
    return box["value"]


def test_fifo_entry_is_a_miss_without_hanging_and_is_removed(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    (cache / SHA).unlink()
    os.mkfifo(cache / SHA)
    assert _within(5, lambda: pf._from_cache(cache, pin, final), cache / SHA) is None
    assert not (cache / SHA).exists()  # a special file is never a cache entry
    assert list(dest.iterdir()) == []


def test_a_fifo_with_a_writer_open_is_still_a_miss(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    (cache / SHA).unlink()
    os.mkfifo(cache / SHA)
    writer = os.open(cache / SHA, os.O_RDWR)
    try:
        assert _within(5, lambda: pf._from_cache(cache, pin, final)) is None
    finally:
        os.close(writer)
    assert list(dest.iterdir()) == []


def test_entry_owned_by_another_user_is_a_miss_and_kept(tmp_path, monkeypatch):
    cache, dest, pin, final = _setup(tmp_path)
    me = os.geteuid()
    monkeypatch.setattr(pf.os, "geteuid", lambda: me + 1)
    assert pf._from_cache(cache, pin, final) is None
    assert (cache / SHA).read_bytes() == GOOD
    assert list(dest.iterdir()) == []


def test_a_regular_entry_is_still_served_after_the_guard(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    assert _within(5, lambda: pf._from_cache(cache, pin, final)) == len(GOOD)
    assert final.read_bytes() == GOOD


def test_smaller_max_bytes_is_miss_and_keeps_entry(tmp_path):
    cache, dest, pin, final = _setup(tmp_path, max_bytes=len(GOOD) - 1)
    assert pf._from_cache(cache, pin, final) is None
    assert (cache / SHA).read_bytes() == GOOD
    assert list(dest.iterdir()) == []
    pin["max_bytes"] = len(GOOD)
    assert pf._from_cache(cache, pin, final) == len(GOOD)


def test_growth_past_cap_mid_pass_is_miss_keeps_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "_CHUNK", 64)
    cache, dest, pin, final = _setup(tmp_path, max_bytes=len(GOOD))
    entry = cache / SHA
    real_fdopen, state = os.fdopen, {"n": 0}

    class Grow:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

        def fileno(self):
            return self.f.fileno()

        def read(self, n):
            state["n"] += 1
            if state["n"] == 2:
                with open(entry, "ab") as w:  # entry grows while it is being served
                    w.write(b"x" * 5000)
            return self.f.read(n)

    monkeypatch.setattr(
        pf.os,
        "fdopen",
        lambda fd, mode="r", *a, **k: (
            Grow(real_fdopen(fd, mode, *a, **k)) if mode == "rb" else real_fdopen(fd, mode, *a, **k)
        ),
    )
    assert pf._from_cache(cache, pin, final) is None
    assert not final.exists()
    assert list(dest.iterdir()) == []
    assert entry.exists()


def test_mutated_mid_pass_never_serves_other_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "_CHUNK", 64)
    cache, dest, pin, final = _setup(tmp_path)
    entry = cache / SHA
    real_fdopen, state = os.fdopen, {"n": 0}

    class Mutate:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

        def fileno(self):
            return self.f.fileno()

        def read(self, n):
            state["n"] += 1
            if (
                state["n"] == 1
            ):  # between open and first read: same size, different bytes, written in place
                with open(entry, "r+b") as w:
                    w.seek(200)
                    w.write(b"EVIL")
            return self.f.read(n)

    monkeypatch.setattr(
        pf.os,
        "fdopen",
        lambda fd, mode="r", *a, **k: (
            Mutate(real_fdopen(fd, mode, *a, **k))
            if mode == "rb"
            else real_fdopen(fd, mode, *a, **k)
        ),
    )
    assert pf._from_cache(cache, pin, final) is None
    assert not final.exists()
    assert list(dest.iterdir()) == []
    assert not entry.exists()  # whole file read and its digest differs: confirmed mismatch


def test_whole_flow_cache_hit_row(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    rows = pf.fetch_public_files(
        [pin],
        dest=dest,
        cache_dir=cache,
        opener=lambda *a, **k: pytest.fail("network used"),
    )
    assert rows[0]["cached"] is True and rows[0]["bytes"] == len(GOOD)
    assert hashlib.sha256(final.read_bytes()).hexdigest() == SHA


def test_hard_linked_entry_is_miss_and_kept(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    os.link(cache / SHA, tmp_path / "other")
    assert pf._from_cache(cache, pin, final) is None
    assert (cache / SHA).read_bytes() == GOOD
    assert list(dest.iterdir()) == []


def test_hard_linked_entry_is_refetched(tmp_path):
    cache, dest, pin, final = _setup(tmp_path)
    os.link(cache / SHA, tmp_path / "other")
    rows = pf.fetch_public_files(
        [pin], dest=dest, cache_dir=cache, opener=lambda url: (200, {}, iter([GOOD]))
    )
    assert rows[0]["cached"] is False
    assert final.read_bytes() == GOOD


def test_copy_failure_on_hit_refetches_and_reports_miss(tmp_path, monkeypatch):
    cache, dest, pin, final = _setup(tmp_path)
    real = pf.os.replace

    def boom(src, dst, *a, **k):
        if str(src).endswith(".cachetmp"):
            raise OSError(errno.ENOSPC, "full")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(pf.os, "replace", boom)
    rows = pf.fetch_public_files(
        [pin], dest=dest, cache_dir=cache, opener=lambda url: (200, {}, iter([GOOD]))
    )
    assert rows[0]["cached"] is False
    assert final.read_bytes() == GOOD
