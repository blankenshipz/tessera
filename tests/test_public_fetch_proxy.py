"""ADR 0097: the worker's public fetch ignores environment proxies; only an explicit operator
proxy whose host is on the stage's egress allowlist is used. No test opens a network socket."""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import urllib.request
from pathlib import Path

import pytest
from test_supervisor import FakeAdapter

from tessera_worker import public_fetch as pf
from tessera_worker.cli import parser
from tessera_worker.grant_poller import GrantPollerConfig, load_poller_config
from tessera_worker.runtime import CpuRuntime
from tessera_worker.service import DevelopmentWorkerError
from tessera_worker.supervisor import SupervisorError

DATA = b"public weights" * 10
SHA = hashlib.sha256(DATA).hexdigest()
URL = "https://files.example.com/m.bin"
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


class _Response(io.BytesIO):
    """The parts of an http.client response that urllib's processors and http_open read."""

    def __init__(self, data: bytes = DATA) -> None:
        super().__init__(data)
        self.status = self.code = 200
        self.msg = "OK"
        self.headers = email.message.Message()

    def info(self):
        return self.headers


def pin(**kw):
    return {"name": "m.bin", "url": URL, "sha256": SHA, "max_bytes": len(DATA), **kw}


@pytest.fixture
def env_proxies(monkeypatch):
    for name in PROXY_VARS:
        monkeypatch.setenv(name, "http://env-proxy.example.net:3128")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def wire(monkeypatch):
    """Replace the https handler's network step: record where each request would connect."""
    seen: list[dict[str, str | None]] = []

    def https_open(self, request):
        seen.append({"connect": request.host, "tunnel": request._tunnel_host})
        return _Response()

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    return seen


def test_environment_proxies_are_ignored_by_default(env_proxies, wire, tmp_path):
    rows = pf.fetch_public_files([pin()], dest=tmp_path)
    assert rows[0]["sha256"] == SHA and rows[0]["source_host"] == "files.example.com"
    # Direct connection to the pinned host: no proxy, no tunnel, despite every proxy variable.
    assert wire == [{"connect": "files.example.com", "tunnel": None}]


def test_default_opener_has_no_environment_proxy_handler(env_proxies, monkeypatch):
    passed, built = [], []
    real = urllib.request.build_opener

    def capture(*handlers):
        passed.extend(handlers)
        opener = real(*handlers)
        built.append(opener)
        opener.open = lambda request, timeout=None: _Response(b"")
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", capture)
    pf.http_open(URL)
    # The empty ProxyHandler replaces build_opener's default one (which reads the environment);
    # with no *_open methods it is never registered, so no handler can route via a proxy.
    assert [h.proxies for h in passed if isinstance(h, urllib.request.ProxyHandler)] == [{}]
    (opener,) = built
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)
    assert not any(isinstance(h, pf._ExplicitProxy) for h in opener.handlers)


def test_explicit_proxy_on_the_allowlist_is_used(env_proxies, wire, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")  # urllib's ProxyHandler would bypass on this; ours does not
    rows = pf.fetch_public_files(
        [pin()],
        dest=tmp_path,
        redirect_hosts=["proxy.example.net"],
        proxy="http://Proxy.Example.NET.:3128",
    )
    assert rows[0]["bytes"] == len(DATA)
    # Connects to the operator proxy and tunnels (CONNECT) to the pinned host, not env-proxy.
    assert wire == [{"connect": "proxy.example.net:3128", "tunnel": "files.example.com"}]
    assert (tmp_path / "m.bin").read_bytes() == DATA


def test_explicit_proxy_on_a_pinned_host_defaults_to_port_80(wire, tmp_path):
    pf.fetch_public_files([pin()], dest=tmp_path, proxy="http://files.example.com/")
    assert wire == [{"connect": "files.example.com:80", "tunnel": "files.example.com"}]


def test_explicit_proxy_off_the_allowlist_is_refused_before_any_request(wire, tmp_path):
    with pytest.raises(pf.PublicFetchError) as excinfo:
        pf.fetch_public_files(
            [pin()],
            dest=tmp_path,
            redirect_hosts=["cdn.example.net"],
            proxy="http://proxy.example.net:3128",
        )
    assert excinfo.value.code == "proxy_not_allowed"
    assert str(excinfo.value) == "proxy_not_allowed: proxy.example.net"
    assert wire == [] and not any(tmp_path.iterdir())


@pytest.mark.parametrize(
    "value",
    [
        "",
        " http://proxy.example.net:3128",
        "proxy.example.net:3128",
        "https://proxy.example.net:3128",
        "socks5://proxy.example.net:1080",
        "http://user:secret@proxy.example.net:3128",
        "http://proxy.example.net:3128/path",
        "http://proxy.example.net:3128?x=1",
        "http://proxy.example.net:3128#f",
        "http://proxy.example.net:0",
        "http://proxy.example.net:99999",
        "http://proxy.example.net:abc",
        "http://127.0.0.1:3128",
        "http://[::1]:3128",
        "http://localhost:3128",
        "http://proxy.internal:3128",
        "http://:3128",
        3128,
    ],
)
def test_malformed_proxy_is_refused(value, wire, tmp_path):
    with pytest.raises(pf.PublicFetchError) as excinfo:
        pf.fetch_public_files(
            [pin()], dest=tmp_path, redirect_hosts=["proxy.example.net"], proxy=value
        )
    assert excinfo.value.code == "proxy_invalid"
    assert str(excinfo.value) == "proxy_invalid"  # the value (maybe a credential) is not echoed
    assert wire == []


def test_runtime_passes_the_operator_proxy_and_fails_closed(tmp_path):
    inputs, public = tmp_path / "inputs", tmp_path / "public"
    inputs.mkdir(mode=0o700)
    public.mkdir(mode=0o700)
    calls = []

    def fetcher(files, **kwargs):
        calls.append(kwargs.get("proxy"))
        return pf.fetch_public_files(files, opener=lambda url: (200, {}, iter([DATA])), **kwargs)

    plan = {
        "hosts": ["files.example.com"],
        "files": [{"input": "weights", **pin()}],
        "max_bytes_total": len(DATA),
        "cache": False,
    }

    def runtime(proxy):
        return CpuRuntime(
            tmp_path / f"runtime-{len(calls)}",
            inputs,
            None,
            FakeAdapter(),
            worker_id="worker",
            public_root=public,
            public_fetch_proxy=proxy,
            public_fetcher=fetcher,
        )

    runtime(None)._fetch_public("op-a", plan)
    runtime("http://files.example.com:3128")._fetch_public("op-b", plan)
    with pytest.raises(SupervisorError, match="Public fetch failed: proxy_not_allowed"):
        runtime("http://proxy.example.net:3128")._fetch_public("op-c", plan)
    assert calls == [None, "http://files.example.com:3128", "http://proxy.example.net:3128"]
    assert not (public / "tessera-public-op-c").exists()


def test_operator_setting_cli_and_config(tmp_path):
    for command in (
        [
            "development-run",
            "--control-url=http://127.0.0.1:1",
            "--grant-id=g",
            "--credential-file=c",
            "--workspace-root=w",
            "--development-output-root=o",
            "--allowed-image=i",
        ],
        ["development-poll", "--config=x"],
    ):
        arguments = parser().parse_args([*command, "--public-fetch-proxy=http://p.example.net:1"])
        assert arguments.public_fetch_proxy == "http://p.example.net:1"
        assert parser().parse_args(command).public_fetch_proxy is None

    document = {
        "schema": "tessera.worker.development-poll.v1",
        "control_url": "http://127.0.0.1:1",
        "credential_file": str(tmp_path / "cred"),
        "workspace_root": str(tmp_path / "w"),
        "development_output_root": str(tmp_path / "o"),
        "allowed_images": ["img@sha256:" + "a" * 64],
        "public_fetch_proxy": "http://proxy.example.net:3128",
    }
    config = tmp_path / "poll.json"
    config.write_text(json.dumps(document))
    config.chmod(0o600)
    assert load_poller_config(config).public_fetch_proxy == "http://proxy.example.net:3128"
    config.write_text(json.dumps(document | {"public_fetch_proxy": "http://u:pw@p.example.net"}))
    with pytest.raises(DevelopmentWorkerError) as excinfo:
        load_poller_config(config)
    assert "pw" not in str(excinfo.value)
    with pytest.raises(DevelopmentWorkerError, match="public_fetch_proxy proxy_invalid"):
        GrantPollerConfig(
            control_url="http://127.0.0.1:1",
            credential_file=Path("c"),
            workspace_root=Path("w"),
            development_output_root=Path("o"),
            allowed_images=("img",),
            public_fetch_proxy="http://127.0.0.1:3128",
        )
