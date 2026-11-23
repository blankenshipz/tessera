"""Provider-side fetch cache of the gpu_runtime_v1 runtime (digest-keyed, verified on every reuse).

Offline: fake openers, temporary directories. The cache only helps where the provider mounts
persistent storage; here a private temporary directory stands in for it.
"""

import hashlib
import importlib.util
import os
import stat
from pathlib import Path

import pytest

from tessera_control.salad_workload_contract import check_gpu_runtime_manifest_rules

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "gpu_runtime_cache", ROOT / "workloads/gpu_runtime_v1/runtime.py"
)
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

DATA = b"public-bytes" * 10
OTHER = b"another-public-file" * 7
URL, URL2 = "https://d.example.com/data.bin", "https://d.example.com/other.bin"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def item(data=DATA, url=URL, dest="data.bin", kind="file", cache=True, cap=1000):
    row = {"url": url, "sha256": sha(data), "max_bytes": cap, "kind": kind, "dest": dest}
    if cache is not None:
        row["cache"] = cache
    return row


def manifest(*rows):
    entry = {
        "url": "https://d.example.com/e.py",
        "sha256": sha(b"x=1\n"),
        "max_bytes": 100,
        "kind": "entry",
        "dest": "e.py",
    }
    return {"schema": rt.MANIFEST_SCHEMA, "fetches": [*rows, entry]}


BODIES = {URL: DATA, URL2: OTHER, "https://d.example.com/e.py": b"x=1\n"}


class Opener:
    def __init__(self, bodies=None):
        self.bodies, self.calls = dict(bodies or BODIES), []

    def __call__(self, url, timeout):
        self.calls.append(url)

        class Resp:
            status, headers = 200, {}

            def __init__(self, data):
                import io

                self.data = io.BytesIO(data)

            def read(self, n=-1):
                return self.data.read(n)

            def close(self):
                pass

        return Resp(self.bodies[url])


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    path = tmp_path / "persist"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    monkeypatch.setenv(rt.CACHE_DIR_ENV, str(path))
    monkeypatch.setenv(rt.CACHE_MAX_BYTES_ENV, "100000")
    return path


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return str(path)


def run(work, rows, opener=None):
    opener = opener or Opener()
    paths, fetched = rt.fetch_all(manifest(*rows), work, rt.time.monotonic() + 60, opener)
    return paths, fetched, opener


def data_row(fetched):
    return next(r for r in fetched if r["url"] == URL)


def test_miss_stores_then_hit_is_served_without_the_opener(cache_dir, work, tmp_path):
    _, first, opener = run(work, [item()])
    assert data_row(first)["cache"] == "miss" and URL in opener.calls
    assert (cache_dir / sha(DATA)).read_bytes() == DATA
    assert stat.S_IMODE((cache_dir / sha(DATA)).stat().st_mode) == 0o600
    second_work = tmp_path / "work2"
    second_work.mkdir()
    paths, second, opener2 = run(str(second_work), [item()])
    row = data_row(second)
    assert row["cache"] == "hit" and row["bytes"] == len(DATA) and row["sha256"] == sha(DATA)
    assert URL not in opener2.calls  # served from the cache, only the entry was fetched
    target = Path(second_work, "files", "data.bin")
    assert target.read_bytes() == DATA and stat.S_IMODE(target.stat().st_mode) == 0o444
    assert target.stat().st_nlink == 1 and (cache_dir / sha(DATA)).stat().st_nlink == 1  # a copy


def test_every_reuse_is_rehashed_and_a_corrupt_entry_is_discarded_and_refetched(
    cache_dir, work, tmp_path
):
    run(work, [item()])
    (cache_dir / sha(DATA)).write_bytes(b"x" * len(DATA))  # same size, wrong bytes
    w2 = tmp_path / "w2"
    w2.mkdir()
    _, fetched, opener = run(str(w2), [item()])
    assert URL in opener.calls and data_row(fetched)["cache"] == "miss"
    assert Path(w2, "files", "data.bin").read_bytes() == DATA
    assert (cache_dir / sha(DATA)).read_bytes() == DATA  # replaced by the verified refetch


def test_entry_above_this_pins_cap_is_a_miss_and_the_refetch_replaces_it(cache_dir, work):
    (cache_dir / sha(DATA)).write_bytes(DATA + b"extra")
    (cache_dir / sha(DATA)).chmod(0o600)
    _, fetched, opener = run(work, [item(cap=len(DATA) + 2)])
    assert data_row(fetched)["cache"] == "miss" and URL in opener.calls


def test_reuse_hashes_the_whole_entry_each_time(cache_dir, work, tmp_path, monkeypatch):
    run(work, [item()])
    seen = []
    real = rt.hashlib.sha256

    def counting(*args, **kwargs):
        seen.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(rt.hashlib, "sha256", counting)
    for n in range(2):
        w = tmp_path / f"r{n}"
        w.mkdir()
        run(str(w), [item()])
    assert len(seen) >= 2  # one re-hash per reuse (plus the entry's own fetch hash)


def test_symlinked_entry_and_symlinked_directory_are_not_used(cache_dir, work, tmp_path):
    secret = tmp_path / "elsewhere"
    secret.write_bytes(DATA)
    (cache_dir / sha(DATA)).symlink_to(secret)
    _, fetched, opener = run(work, [item()])
    assert URL in opener.calls and data_row(fetched)["cache"] == "miss"
    link = tmp_path / "link"
    link.symlink_to(cache_dir)
    os.environ[rt.CACHE_DIR_ENV] = str(link)
    w = tmp_path / "w3"
    w.mkdir()
    _, fetched, _ = run(str(w), [item()])
    assert data_row(fetched)["cache"] == "unavailable"


def test_directory_with_wrong_mode_or_inside_the_work_tree_is_unavailable(
    cache_dir, work, tmp_path, monkeypatch
):
    cache_dir.chmod(0o755)
    _, fetched, _ = run(work, [item()])
    assert data_row(fetched)["cache"] == "unavailable"
    cache_dir.chmod(0o700)
    inside = Path(work) / "cache"
    inside.mkdir(mode=0o700)
    monkeypatch.setenv(rt.CACHE_DIR_ENV, str(inside))
    assert data_row(run(work, [item()])[1])["cache"] == "unavailable"
    outer = tmp_path / "outer"
    (outer / "work").mkdir(parents=True)
    outer.chmod(0o700)
    monkeypatch.setenv(rt.CACHE_DIR_ENV, str(outer))
    assert data_row(run(str(outer / "work"), [item()])[1])["cache"] == "unavailable"
    monkeypatch.setenv(rt.CACHE_DIR_ENV, "relative/dir")
    assert data_row(run(work, [item()])[1])["cache"] == "unavailable"
    monkeypatch.delenv(rt.CACHE_DIR_ENV)
    assert data_row(run(work, [item()])[1])["cache"] == "unavailable"


def test_wrong_owner_is_unavailable(cache_dir, work, monkeypatch):
    other = os.geteuid() + 1
    monkeypatch.setattr(rt.os, "geteuid", lambda: other)
    assert data_row(run(work, [item()])[1])["cache"] == "unavailable"


def test_quota_zero_never_stores_but_lookups_still_work(cache_dir, work, tmp_path, monkeypatch):
    monkeypatch.setenv(rt.CACHE_MAX_BYTES_ENV, "0")
    assert data_row(run(work, [item()])[1])["cache"] == "miss"
    assert list(cache_dir.iterdir()) == []
    (cache_dir / sha(DATA)).write_bytes(DATA)
    (cache_dir / sha(DATA)).chmod(0o600)
    w = tmp_path / "w2"
    w.mkdir()
    assert data_row(run(str(w), [item()])[1])["cache"] == "hit"


def test_eviction_removes_the_oldest_first_and_oversize_is_never_stored(
    cache_dir, work, tmp_path, monkeypatch
):
    monkeypatch.setenv(rt.CACHE_MAX_BYTES_ENV, str(len(DATA) + len(OTHER)))
    old = b"o" * 20
    (cache_dir / sha(old)).write_bytes(old)
    os.utime(cache_dir / sha(old), (1, 1))
    run(work, [item(), item(OTHER, URL2, "other.bin")])
    names = {p.name for p in cache_dir.iterdir()}
    assert names == {sha(DATA), sha(OTHER)}  # the oldest was evicted to fit both
    monkeypatch.setenv(rt.CACHE_MAX_BYTES_ENV, "5")
    w = tmp_path / "w5"
    w.mkdir()
    third = b"z" * 50
    bodies = {**BODIES, "https://d.example.com/z": third}
    run(str(w), [item(third, "https://d.example.com/z", "z.bin")], Opener(bodies))
    assert sha(third) not in {p.name for p in cache_dir.iterdir()}


def test_a_failed_store_never_fails_the_run(cache_dir, work, monkeypatch):
    monkeypatch.setattr(rt.shutil, "copyfileobj", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert data_row(run(work, [item()])[1])["cache"] == "miss"
    assert not any(p.name.startswith(".") for p in cache_dir.iterdir())


def test_unflagged_items_are_off_and_never_cached(cache_dir, work):
    _, fetched, _ = run(work, [item(cache=None), item(OTHER, URL2, "other.bin", cache=False)])
    assert {r["cache"] for r in fetched} == {"off"}
    assert list(cache_dir.iterdir()) == []


def test_cached_bytes_are_never_staged_as_outputs(cache_dir, work, tmp_path):
    run(work, [item()])
    out = Path(work) / "out"
    out.mkdir(exist_ok=True)
    (out / "output.json").write_text("{}")
    plan = [("output.json", 100)]
    staged = rt.stage_outputs(str(out), str(tmp_path / "staged"), plan, [])
    assert [item[0] for item in staged] == ["output.json"]
    assert not Path(tmp_path / "staged", sha(DATA)).exists()


def test_the_manifest_accepts_cache_on_file_and_wheel_only():
    rt.validate_manifest(manifest(item(), item(OTHER, URL2, "w-1-py3-none-any.whl", "wheel")))
    for bad in (
        item(kind="entry", dest="x.py"),
        {**item(), "cache": "yes"},
        {**item(), "extra": 1},
    ):
        with pytest.raises(rt.Failure):
            rt.validate_manifest(manifest(bad))


def test_the_control_side_rules_agree_and_an_older_key_set_is_refused():
    check_gpu_runtime_manifest_rules(manifest(item()), input_declared=False)
    with pytest.raises(ValueError, match="cache"):
        check_gpu_runtime_manifest_rules(
            manifest(item(kind="entry", dest="x.py")), input_declared=False
        )
    with pytest.raises(ValueError):
        check_gpu_runtime_manifest_rules(manifest({**item(), "cache": "yes"}), input_declared=False)
    with pytest.raises(ValueError):
        check_gpu_runtime_manifest_rules(manifest({**item(), "stray": 1}), input_declared=False)
