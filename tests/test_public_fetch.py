import hashlib
import json

import pytest

from tessera_worker import public_fetch as pf

DATA = b"weights-bytes" * 100
SHA = hashlib.sha256(DATA).hexdigest()
URL = "https://files.example.com/m.bin"


def pin(**kw):
    base = {"name": "m.bin", "url": URL, "sha256": SHA, "max_bytes": len(DATA)}
    return {**base, **kw}


def serve(routes, calls=None):
    """routes: url -> (status, headers, bytes)."""

    def opener(url):
        if calls is not None:
            calls.append(url)
        status, headers, body = routes[url]
        return status, headers, iter([body[i : i + 100] for i in range(0, len(body), 100)])

    return opener


def ok(data=DATA, headers=None):
    return {URL: (200, headers or {}, data)}


def code(excinfo):
    return excinfo.value.code


def test_success_with_timing(tmp_path):
    ticks = iter([10.0, 12.3456])
    rows = pf.fetch_public_files(
        [pin()], dest=tmp_path, opener=serve(ok()), clock=lambda: next(ticks)
    )
    assert rows == [
        {
            "name": "m.bin",
            "source_host": "files.example.com",
            "sha256": SHA,
            "bytes": len(DATA),
            "seconds": 2.346,
            "cached": False,
        }
    ]
    assert (tmp_path / "m.bin").read_bytes() == DATA
    assert (tmp_path / "m.bin").stat().st_mode & 0o777 == 0o444
    assert [p.name for p in tmp_path.iterdir()] == ["m.bin"]


def test_digest_mismatch_leaves_nothing(tmp_path):
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files([pin()], dest=tmp_path, opener=serve(ok(b"x" * len(DATA))))
    assert code(err) == "digest_mismatch"
    assert "secret" not in str(err.value) and "sig=" not in str(err.value)
    assert list(tmp_path.iterdir()) == []


def test_size_over_cap_mid_stream(tmp_path):
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files([pin(max_bytes=250)], dest=tmp_path, opener=serve(ok()))
    assert code(err) == "size_exceeded"
    assert list(tmp_path.iterdir()) == []


def test_content_length_over_cap(tmp_path):
    routes = ok(headers={"Content-Length": str(len(DATA) + 1)})
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files([pin()], dest=tmp_path, opener=serve(routes))
    assert code(err) == "size_exceeded"
    assert list(tmp_path.iterdir()) == []


def test_redirect_to_allowed_host(tmp_path):
    cdn = "https://cdn.example.net/blob"
    routes = {URL: (302, {"Location": cdn}, b""), cdn: (200, {}, DATA)}
    rows = pf.fetch_public_files(
        [pin()], dest=tmp_path, redirect_hosts=["cdn.example.net"], opener=serve(routes)
    )
    assert rows[0]["source_host"] == "files.example.com"


def test_redirect_to_disallowed_host(tmp_path):
    evil = "https://evil.example.org/blob"
    calls = []
    routes = {URL: (302, {"location": evil}, b""), evil: (200, {}, DATA)}
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files([pin()], dest=tmp_path, opener=serve(routes, calls))
    assert code(err) == "host_not_allowed" and calls == [URL]


def test_too_many_hops(tmp_path):
    hops = [URL] + [f"https://files.example.com/h{i}" for i in range(1, 6)]
    routes = {u: (302, {"Location": hops[i + 1]}, b"") for i, u in enumerate(hops[:-1])}
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files([pin()], dest=tmp_path, opener=serve(routes))
    assert code(err) == "too_many_redirects"


def test_exactly_three_hops_allowed(tmp_path):
    hops = [URL] + [f"https://files.example.com/h{i}" for i in range(1, 4)]
    routes = {u: (302, {"Location": hops[i + 1]}, b"") for i, u in enumerate(hops[:-1])}
    routes[hops[-1]] = (200, {}, DATA)
    assert pf.fetch_public_files([pin()], dest=tmp_path, opener=serve(routes))[0]["bytes"] == len(
        DATA
    )


def test_redirect_without_location_and_bad_status(tmp_path):
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files([pin()], dest=tmp_path, opener=serve({URL: (302, {}, b"")}))
    assert code(err) == "redirect_without_location"
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files([pin()], dest=tmp_path, opener=serve({URL: (404, {}, b"")}))
    assert code(err) == "bad_status"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://files.example.com/m.bin", "scheme_not_allowed"),
        ("https://files.example.com:8443/m.bin", "host_not_allowed"),
        ("https://user:pw@files.example.com/m.bin", "host_not_allowed"),
        ("https://user@files.example.com/m.bin", "host_not_allowed"),
        ("https://10.0.0.1/m.bin", "host_not_allowed"),
        ("https://[::1]/m.bin", "host_not_allowed"),
        ("https://2130706433/m.bin", "host_not_allowed"),
    ],
)
def test_refused_urls_never_open(tmp_path, url, expected):
    calls = []
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files([pin(url=url)], dest=tmp_path, opener=serve({}, calls))
    assert code(err) == expected and calls == []


def test_ip_literal_redirect_refused(tmp_path):
    routes = {URL: (302, {"Location": "https://192.168.1.1/x"}, b"")}
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files(
            [pin()], dest=tmp_path, redirect_hosts=["192.168.1.1"], opener=serve(routes)
        )
    assert code(err) == "host_not_allowed"


@pytest.mark.parametrize(
    "name", ["../x", "a/b", ".hidden", "", "a" * 129, "a b", "x\n", "..", "a\\b"]
)
def test_bad_names_refused(tmp_path, name):
    calls = []
    with pytest.raises(pf.PublicFetchError):
        pf.fetch_public_files([pin(name=name)], dest=tmp_path, opener=serve(ok(), calls))
    assert calls == []


def test_cache_hit_reverifies(tmp_path):
    cache, dest = tmp_path / "cache", tmp_path / "dest"
    pf.fetch_public_files([pin()], dest=dest, cache_dir=cache, opener=serve(ok()))
    assert (cache / SHA).read_bytes() == DATA
    dest2, calls = tmp_path / "dest2", []
    ticks = iter([1.0, 1.5])
    rows = pf.fetch_public_files(
        [pin()], dest=dest2, cache_dir=cache, opener=serve(ok(), calls), clock=lambda: next(ticks)
    )
    assert calls == [] and rows[0]["cached"] is True and rows[0]["seconds"] == 0.5
    assert (dest2 / "m.bin").read_bytes() == DATA


def test_corrupt_cache_entry_refetched(tmp_path):
    cache, dest = tmp_path / "cache", tmp_path / "dest"
    cache.mkdir()
    (cache / SHA).write_bytes(b"corrupt")
    calls = []
    rows = pf.fetch_public_files([pin()], dest=dest, cache_dir=cache, opener=serve(ok(), calls))
    assert calls == [URL] and rows[0]["cached"] is False
    assert (cache / SHA).read_bytes() == DATA


def test_cache_not_written_on_failed_verify(tmp_path):
    cache, dest = tmp_path / "cache", tmp_path / "dest"
    with pytest.raises(pf.PublicFetchError):
        pf.fetch_public_files(
            [pin()], dest=dest, cache_dir=cache, opener=serve(ok(b"y" * len(DATA)))
        )
    assert list(cache.iterdir()) == []


def test_load_pins(tmp_path):
    shape = {
        "files": {
            "b.bin": {"url": "https://h.example.com/b", "sha256": SHA, "max_bytes": 5},
            "a.bin": {"url": "https://h.example.com/a", "sha256": SHA, "max_bytes": 7},
        },
        "redirect_hosts": ["cdn.example.net"],
    }
    files, hosts = pf.load_pins(shape)
    assert [f["name"] for f in files] == ["a.bin", "b.bin"] and files[0]["max_bytes"] == 7
    assert hosts == ["cdn.example.net"]
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(shape))
    assert pf.load_pins(path) == (files, hosts)
    with pytest.raises(ValueError):
        pf.load_pins({"files": {"a": {"url": "u"}}})
