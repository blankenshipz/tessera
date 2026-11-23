"""Abandoned ``.<sha256>.part`` files of the fetch cache are reclaimed, counted and never raced.

Offline: temporary directories and real child processes. A writer holds an exclusive flock on its
part file; the kernel drops it when the process dies, so a lockable old part file is abandoned.
"""

import hashlib
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "gpu_runtime_cache_partfile", ROOT / "workloads/gpu_runtime_v1/runtime.py"
)
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

DATA = b"public-bytes" * 10
OTHER = b"another-public-file" * 7


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pin(data=DATA):
    return {"url": "https://d.example.com/x.bin", "sha256": sha(data), "max_bytes": 1000}


def part_path(directory, data=DATA):
    return os.path.join(directory, "." + sha(data) + ".part")


CRASH = """
import fcntl, os, signal, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.write(fd, b"x" * int(sys.argv[2]))
os.kill(os.getpid(), signal.SIGKILL)
"""

HOLD = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.write(fd, b"x" * int(sys.argv[2]))
os.write(1, b"r")
time.sleep(120)
"""


def crash(path, nbytes=5):
    proc = subprocess.run([sys.executable, "-c", CRASH, str(path), str(nbytes)], timeout=30)
    assert proc.returncode == -9


def age(path, seconds=120):
    old = time.time() - seconds
    os.utime(path, (old, old), follow_symlinks=False)


@pytest.fixture
def directory(tmp_path):
    d = tmp_path / "persist"
    d.mkdir(mode=0o700)
    return str(d)


@pytest.fixture
def source(tmp_path):
    def make(data=DATA):
        p = tmp_path / ("src-" + sha(data)[:8])
        p.write_bytes(data)
        return str(p)

    return make


@pytest.fixture
def holder():
    procs = []

    def start(path, nbytes=5):
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLD, str(path), str(nbytes)], stdout=subprocess.PIPE
        )
        procs.append(proc)
        assert proc.stdout.read(1) == b"r"
        return proc

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()
        proc.stdout.close()


def parts(directory):
    return [n for n in os.listdir(directory) if n.endswith(".part")]


def test_killed_mid_write_part_is_reclaimed_and_digest_cached(directory, source):
    cache = rt._FetchCache(directory, 10000)
    crash(part_path(directory))
    age(part_path(directory))
    cache.store(pin(), source(), len(DATA))
    assert Path(directory, sha(DATA)).read_bytes() == DATA
    assert parts(directory) == []


def test_young_leftover_is_not_reclaimed(directory, source):
    cache = rt._FetchCache(directory, 10000)
    crash(part_path(directory))
    assert rt.STALE_PART_MIN_AGE_SECONDS == 60
    age(part_path(directory), rt.STALE_PART_MIN_AGE_SECONDS - 30)
    cache.store(pin(), source(), len(DATA))
    assert not os.path.exists(os.path.join(directory, sha(DATA)))
    assert os.path.exists(part_path(directory))


def test_live_locked_part_is_never_removed_and_counts_to_quota(directory, source, holder):
    other = part_path(directory, OTHER)
    holder(other, 50)
    age(other)
    cache = rt._FetchCache(directory, 50 + len(DATA) - 1)
    cache.store(pin(), source(), len(DATA))
    assert os.path.exists(other)
    assert not os.path.exists(os.path.join(directory, sha(DATA)))
    roomy = rt._FetchCache(directory, 50 + len(DATA))
    roomy.store(pin(), source(), len(DATA))
    assert Path(directory, sha(DATA)).read_bytes() == DATA
    assert os.path.exists(other)


def test_live_own_digest_part_returns_without_storing(directory, source, holder):
    own = part_path(directory)
    holder(own, 5)
    age(own)
    rt._FetchCache(directory, 10000).store(pin(), source(), len(DATA))
    assert not os.path.exists(os.path.join(directory, sha(DATA)))
    assert os.path.getsize(own) == 5


def test_stale_other_digest_part_is_reclaimed_and_not_counted(directory, source):
    other = part_path(directory, OTHER)
    crash(other, 500)
    age(other)
    rt._FetchCache(directory, len(DATA) + 10).store(pin(), source(), len(DATA))
    assert not os.path.exists(other)
    assert Path(directory, sha(DATA)).read_bytes() == DATA


def test_unsafe_candidates_are_left_untouched(directory, source, tmp_path, monkeypatch):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    sym = part_path(directory, b"s")
    os.symlink(victim, sym)
    folder = part_path(directory, b"d")
    os.mkdir(folder)
    linked = part_path(directory, b"l")
    Path(linked).write_bytes(b"x")
    os.link(linked, str(tmp_path / "second-name"))
    foreign = part_path(directory, b"f")
    Path(foreign).write_bytes(b"x")
    for p in (linked, foreign):
        age(p)
    age(sym)
    real = os.geteuid()
    monkeypatch.setattr(rt.os, "geteuid", lambda: real + 1)
    rt._FetchCache(directory, 10000).store(pin(), source(), len(DATA))
    assert os.path.islink(sym) and victim.read_bytes() == b"keep"
    assert os.path.isdir(folder)
    assert os.path.exists(linked) and os.path.exists(foreign)
    assert Path(directory, sha(DATA)).read_bytes() == DATA


def test_hard_linked_and_foreign_owner_checked_independently(directory, source, tmp_path):
    linked = part_path(directory, b"l")
    Path(linked).write_bytes(b"x")
    os.link(linked, str(tmp_path / "second-name"))
    age(linked)
    rt._FetchCache(directory, 10000).store(pin(), source(), len(DATA))
    assert os.path.exists(linked)


def test_repeated_crashes_stay_within_quota(directory, source):
    quota = 2000
    cache = rt._FetchCache(directory, quota)
    for i in range(8):
        data = b"payload-%d-" % i * 20
        crash(part_path(directory, data), 300)
        age(part_path(directory, data))
        cache.store(pin(data), source(data), len(data))
        total = sum(os.lstat(os.path.join(directory, n)).st_size for n in os.listdir(directory))
        assert total <= quota
        assert parts(directory) == []


def test_store_never_raises(directory, source, monkeypatch):
    cache = rt._FetchCache(directory + "-missing", 10000)
    cache.store(pin(), source(), len(DATA))
    cache = rt._FetchCache(directory, 10000)

    def boom(_path):
        raise OSError("listdir failed")

    monkeypatch.setattr(rt.os, "listdir", boom)
    cache.store(pin(), source(), len(DATA))
