"""The Salad in-container public fetch stays proxy-free (ADR 0097; synthetic, no network).

``workloads/gpu_runtime_v1/runtime.py`` builds its opener with ``ProxyHandler({})``, so proxy
environment variables in the container never route a pinned fetch through another host. This
pins that, even with every proxy variable set, the opener ``open_url`` builds carries no proxy.
"""

import importlib.util
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY_VARIABLES = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def _runtime():
    path = ROOT / "workloads/gpu_runtime_v1/runtime.py"
    spec = importlib.util.spec_from_file_location("gpu_runtime_v1_runtime_no_proxy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Opened(Exception):
    pass


def test_open_url_opener_ignores_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, "http://proxy.example.com:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert urllib.request.getproxies()  # the environment would route a default opener
    rt = _runtime()
    passed: list[tuple] = []
    built: list[urllib.request.OpenerDirector] = []
    real_build_opener = urllib.request.build_opener

    def capture(*handlers):
        passed.append(handlers)
        opener = real_build_opener(*handlers)
        built.append(opener)

        def refuse(*_args, **_kwargs):
            raise _Opened  # never touch the network

        opener.open = refuse
        return opener

    monkeypatch.setattr(rt.urllib.request, "build_opener", capture)
    with pytest.raises(_Opened):
        rt.open_url("https://hub.example.com/org/repo/weights.bin", 5)

    assert len(passed) == 1
    proxy_handlers = [h for h in passed[0] if isinstance(h, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    # The explicit empty handler replaces build_opener's default environment ProxyHandler, so
    # the built opener holds no handler that opens through a proxy.
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in built[0].handlers)
    assert not any(hasattr(h, "proxy_open") for h in built[0].handlers)
