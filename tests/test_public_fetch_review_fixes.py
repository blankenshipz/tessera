"""ADR 0097 review fixes: plan/worker host rules, and a cache that keeps one link per file."""

from __future__ import annotations

import hashlib
import json

import pytest

from tessera_control.public_fetch_models import PublicFetchPlan, public_fetch_host
from tessera_worker import public_fetch as pf

DATA = b"cached weights" * 50
SHA = hashlib.sha256(DATA).hexdigest()


def plan(**overrides):
    file = {
        "input": "model",
        "name": "m.bin",
        "url": "https://files.example.com/m.bin",
        "sha256": SHA,
        "max_bytes": 1000,
    }
    base = {
        "hosts": ["files.example.com"],
        "files": [{**file, **overrides.pop("file", {})}],
        "max_bytes_total": 1000,
        "cache": False,
    }
    return {**base, **overrides}


def test_a_well_formed_plan_is_accepted():
    assert PublicFetchPlan.model_validate(plan()).hosts == ["files.example.com"]


@pytest.mark.parametrize(
    "bad",
    [
        {"file": {"url": "https://localhost/x"}, "hosts": ["localhost"]},
        {
            "file": {"url": "https://metadata.google.internal/x"},
            "hosts": ["metadata.google.internal"],
        },
        {"file": {"url": "https://files.example.com/m.bin?token=1"}},
        {"file": {"url": "https://files.example.com/m.bin#frag"}},
        {"file": {"url": "https://user:pw@files.example.com/m.bin"}},
        {"file": {"url": "https://files.example.com:8443/m.bin"}},
        {"file": {"url": "https://10.0.0.1/m.bin"}, "hosts": ["10.0.0.1"]},
        {"hosts": ["files.example.com", "localhost"]},
        {"file": {"url": "https://other.example.com/m.bin"}},  # host not in the plan's hosts
    ],
)
def test_a_plan_naming_an_internal_host_or_a_query_is_refused(bad):
    with pytest.raises(ValueError):
        PublicFetchPlan.model_validate(plan(**bad))


def test_the_shared_host_rule_is_the_one_the_spec_layer_uses():
    from tessera_control.campaign_models import public_fetch_host as spec_rule

    assert spec_rule is public_fetch_host


def opener_for(routes):
    def opener(url):
        status, headers, body = routes[url]
        return status, headers, iter([body])

    return opener


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/x",
        "https://metadata.google.internal/x",
        "https://printer.local/x",
        "https://files.example.com/m.bin?token=1",
    ],
)
def test_the_worker_refuses_internal_hosts_and_a_pinned_query(tmp_path, url):
    pin = {"name": "m.bin", "url": url, "sha256": SHA, "max_bytes": 1000}
    host = url.split("/")[2]
    called = []
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files(
            [pin],
            dest=tmp_path,
            redirect_hosts=(host,),
            opener=lambda u: called.append(u) or (200, {}, iter([DATA])),
        )
    assert err.value.code == "host_not_allowed" and not called
    assert list(tmp_path.iterdir()) == []


def test_a_redirect_target_may_carry_a_signed_query_but_not_an_internal_host(tmp_path):
    pinned = "https://files.example.com/m.bin"
    signed = "https://cdn.example.com/blob?sig=abc"
    routes = {pinned: (302, {"Location": signed}, b""), signed: (200, {}, DATA)}
    pin = {"name": "m.bin", "url": pinned, "sha256": SHA, "max_bytes": 1000}
    rows = pf.fetch_public_files(
        [pin], dest=tmp_path, redirect_hosts=("cdn.example.com",), opener=opener_for(routes)
    )
    assert rows[0]["bytes"] == len(DATA)
    internal = "https://169.254.169.254/latest"
    routes = {pinned: (302, {"Location": internal}, b"")}
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files(
            [pin],
            dest=tmp_path / "b",
            redirect_hosts=("169.254.169.254",),
            opener=opener_for(routes),
        )
    assert err.value.code == "host_not_allowed"


def test_a_cache_keeps_one_link_per_file_so_the_mount_check_accepts_it(tmp_path):
    cache = tmp_path / "cache"
    pin = {
        "name": "m.bin",
        "url": "https://files.example.com/m.bin",
        "sha256": SHA,
        "max_bytes": 1000,
    }
    routes = {pin["url"]: (200, {}, DATA)}
    first, second = tmp_path / "one", tmp_path / "two"
    pf.fetch_public_files([pin], dest=first, cache_dir=cache, opener=opener_for(routes))
    assert (first / "m.bin").stat().st_nlink == 1  # the cache write did not link the mounted file
    assert (cache / SHA).stat().st_nlink == 1
    rows = pf.fetch_public_files([pin], dest=second, cache_dir=cache, opener=opener_for({}))
    assert rows[0]["cached"] is True
    assert (second / "m.bin").stat().st_nlink == 1  # a hit is a copy, not a second link
    assert (second / "m.bin").read_bytes() == DATA
    assert json.dumps(rows)  # rows stay plain data


# --- round 3: every spelling of an internal name or an IP address, in every path ---------------

BAD_HOSTS = [
    "localhost.",
    "metadata.google.internal.",
    "foo.local.",
    "foo.localhost",
    "printer.lan",
    "intranet",
    "files.corp",
    "0x7f.0.0.1",  # hex dotted
    "0X7F.0.0.1",
    "0177.0.0.1",  # octal
    "127.1",  # short form
    "127.0.1",
    "2130706433",  # single integer
    "0x7f000001",
    "127.0.0.1",
    "169.254.169.254",
    "10.0.0.1",
    "8.8.8.8",  # a public IP literal is still never a pin source
    "[::1]",
    "[::ffff:127.0.0.1]",
    "[::ffff:7f00:1]",
    "[fe80::1]",
    "files.example.com..",
    ".files.example.com",
    "a..example.com",
    "-bad.example.com",
]


def _url(host):
    return f"https://{host}/m.bin"


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_the_shared_rule_refuses_every_spelling(host):
    assert public_fetch_host(_url(host)) is None


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_a_plan_refuses_every_spelling(host):
    bare = host.strip("[]")
    for hosts, url in (
        (["files.example.com", bare], "https://files.example.com/m.bin"),
        ([bare], _url(host)),
    ):
        with pytest.raises(ValueError):
            PublicFetchPlan.model_validate(plan(hosts=hosts, file={"url": url}))


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_the_worker_refuses_every_spelling_as_a_pin_and_as_a_redirect(tmp_path, host):
    called = []

    def opener(url):
        called.append(url)
        return 200, {}, iter([DATA])

    pin = {"name": "m.bin", "url": _url(host), "sha256": SHA, "max_bytes": 1000}
    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files(
            [pin], dest=tmp_path / "a", redirect_hosts=(host.strip("[]"),), opener=opener
        )
    assert err.value.code == "host_not_allowed" and not called
    good = {
        "name": "m.bin",
        "url": "https://files.example.com/m.bin",
        "sha256": SHA,
        "max_bytes": 1000,
    }
    routes = {good["url"]: (302, {"Location": _url(host)}, b"")}

    def redirecting(url):
        called.append(url)
        status, headers, body = routes[url]
        return status, headers, iter([body])

    with pytest.raises(pf.PublicFetchError) as err:
        pf.fetch_public_files(
            [good], dest=tmp_path / "b", redirect_hosts=(host.strip("[]"),), opener=redirecting
        )
    assert err.value.code == "host_not_allowed"
    assert called == [good["url"]]  # the redirect was never followed


def test_a_trailing_dot_or_case_or_idna_normalizes_to_one_allowed_host(tmp_path):
    from tessera_control.public_fetch_models import canonical_public_host

    assert canonical_public_host("Files.Example.com.") == "files.example.com"
    assert canonical_public_host("bücher.example.de") == "xn--bcher-kva.example.de"
    pin = {
        "name": "m.bin",
        "url": "https://files.example.com./m.bin",
        "sha256": SHA,
        "max_bytes": 1000,
    }
    routes = {pin["url"]: (200, {}, DATA)}
    rows = pf.fetch_public_files([pin], dest=tmp_path, opener=opener_for(routes))
    assert rows[0]["source_host"] == "files.example.com" and rows[0]["bytes"] == len(DATA)
