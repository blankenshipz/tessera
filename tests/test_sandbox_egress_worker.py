from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tessera_worker.cpu import CpuError, CpuObservation, CpuOperation, DockerCpuAdapter
from tessera_worker.egress_proxy import (
    EgressJournal,
    EgressProxy,
    is_public_address,
    journal_sha256,
    summarize_journal,
)
from tessera_worker.runtime import CpuRuntime, ObservationDeliveryPending
from tessera_worker.supervisor import DriverResult, Lease, SupervisorError

IMAGE = "python@sha256:" + "a" * 64
PROXY_IMAGE = "python@sha256:" + "d" * 64
OP = "b" * 32
POLICY = {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": "api.example.org", "port": 443}],
    "max_egress_bytes": 1000,
}


def op(**changes) -> CpuOperation:
    values = {
        "operation_id": OP,
        "image": IMAGE,
        "argv": ("python", "-c", "print(2)"),
        "plan_digest": "c" * 64,
        "max_duration_seconds": 10,
    }
    return CpuOperation(**(values | changes))


def egress_op() -> CpuOperation:
    return op(network_profile="allowlist-proxy-v1", egress=POLICY)


class Docker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.existing: set[str] = set()

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        a = list(argv)
        if a[:2] == ["network", "inspect"] or a[:3] == ["inspect", "--type", "container"]:
            if a[-1] in self.existing:
                return subprocess.CompletedProcess(a, 0, "[]", "")
            return subprocess.CompletedProcess(a, 1, "", "No such object")
        if a[:2] == ["network", "create"] or (a[0] == "create" and "--name" in a):
            self.existing.add(a[-1] if a[0] == "network" else a[a.index("--name") + 1])
        return subprocess.CompletedProcess(a, 0, "ok", "")


def adapter(tmp_path: Path, docker: Docker, proxy_image: str | None = PROXY_IMAGE):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    return DockerCpuAdapter(
        frozenset({IMAGE}), docker, input_root=root, egress_proxy_image=proxy_image
    )


def test_none_profile_argv_unchanged(tmp_path):
    args = adapter(tmp_path, Docker()).create_arguments(op())
    assert args[args.index("--network") + 1] == "none"
    assert "--env" not in args


def test_allowlist_argv_uses_internal_network_and_proxy_env(tmp_path):
    a = adapter(tmp_path, Docker())
    args = a.create_arguments(egress_op())
    assert args[args.index("--network") + 1] == f"tessera-egress-{OP}"
    assert "HTTPS_PROXY=http://egress-proxy:3128" in args
    assert "HTTP_PROXY=http://egress-proxy:3128" in args
    assert "NO_PROXY=" in args
    baseline = a.create_arguments(op())
    stripped = [x for x in args if x not in {"--env"} and "PROXY=" not in x]
    assert stripped == [f"tessera-egress-{OP}" if x == "none" else x for x in baseline]


def test_missing_proxy_image_refuses(tmp_path):
    docker = Docker()
    a = adapter(tmp_path, docker, proxy_image=None)
    with pytest.raises(CpuError):
        a.create_arguments(egress_op())
    with pytest.raises(CpuError):
        a.prepare(egress_op())
    assert not any("create" in c for c in docker.calls)
    with pytest.raises(ValueError):
        DockerCpuAdapter(frozenset({IMAGE}), Docker(), egress_proxy_image="python:latest")


def test_setup_and_teardown_commands(tmp_path):
    docker = Docker()
    a = adapter(tmp_path, docker)
    a.prepare(egress_op())
    flat = [" ".join(c) for c in docker.calls]
    assert any(
        c.startswith("network create --internal") and f"tessera-egress-{OP}" in c for c in flat
    )
    proxy = next(c for c in docker.calls if c[0] == "create" and "tessera.role=egress-proxy" in c)
    for flag in ("--read-only", "--cap-drop", "--network-alias", "--pids-limit", "--memory"):
        assert flag in proxy
    assert proxy[proxy.index("--network") + 1] == f"tessera-egress-{OP}"
    assert proxy[proxy.index("--network-alias") + 1] == "egress-proxy"
    assert proxy[proxy.index("--user") + 1] == "65532:65532"
    assert "no-new-privileges=true" in proxy
    mounts = [m for m in proxy if m.startswith("type=bind")]
    assert sum(m.endswith(",readonly") for m in mounts) == 2 and len(mounts) == 3
    assert ["network", "connect", "bridge", f"tessera-egress-proxy-{OP}"] in docker.calls
    assert docker.calls.index(["start", f"tessera-egress-proxy-{OP}"]) < docker.calls.index(
        ["network", "connect", "bridge", f"tessera-egress-proxy-{OP}"]
    )
    workload = next(c for c in docker.calls if c[0] == "create" and "tessera-op-" + OP in c)
    assert workload[workload.index("--network") + 1] == f"tessera-egress-{OP}"
    config = json.loads((tmp_path / "root" / f"tessera-egress-{OP}" / "config.json").read_text())
    assert config["max_egress_bytes"] == 1000
    docker.calls.clear()
    a.teardown_egress(egress_op())
    a.teardown_egress(egress_op())
    assert ["rm", "--force", f"tessera-egress-proxy-{OP}"] in docker.calls
    assert ["network", "rm", f"tessera-egress-{OP}"] in docker.calls
    assert (tmp_path / "root" / f"tessera-egress-{OP}" / "journal" / "journal.jsonl").exists()


def test_none_operation_issues_no_egress_commands(tmp_path):
    docker = Docker()
    adapter(tmp_path, docker).prepare(op())
    assert not any(c[0] == "network" for c in docker.calls)


# --- runtime validation ---------------------------------------------------------------


def _validate(package_extra, spec_extra):
    package = {"plan": {"execution_spec": dict(spec_extra)}} | package_extra
    CpuRuntime._validate_network_profile(package, package["plan"]["execution_spec"])


@pytest.mark.parametrize(
    "package,spec,ok",
    [
        ({}, {}, True),
        ({"network_profile": "none"}, {}, True),
        ({"network_profile": "allowlist-proxy-v1", "egress": POLICY}, {"egress": POLICY}, True),
        ({}, {"egress": POLICY}, False),
        ({"network_profile": "none", "egress": POLICY}, {"egress": POLICY}, False),
        ({"network_profile": "none"}, {"egress": POLICY}, False),
        ({"network_profile": "allowlist-proxy-v1"}, {"egress": POLICY}, False),
        ({"network_profile": "allowlist-proxy-v1", "egress": POLICY}, {}, False),
        (
            {"network_profile": "allowlist-proxy-v1", "egress": POLICY | {"max_egress_bytes": 5}},
            {"egress": POLICY},
            False,
        ),
        ({"network_profile": "bridge"}, {}, False),
        (
            {"network_profile": "allowlist-proxy-v1", "egress": {"profile": "x"}},
            {"egress": {"profile": "x"}},
            False,
        ),
    ],
)
def test_runtime_profile_validation(package, spec, ok):
    if ok:
        _validate(package, spec)
    else:
        with pytest.raises(SupervisorError):
            _validate(package, spec)


# --- journal post -----------------------------------------------------------------------


class EgressAdapter:
    def __init__(self, journal: Path) -> None:
        self.journal = journal
        self.torn_down = 0

    def teardown_egress(self, operation):
        self.torn_down += 1

    def egress_summary(self, operation):
        return journal_sha256(self.journal), summarize_journal(self.journal)


class Channel:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict, str]] = []
        self.fail_journal = 0

    def post(self, path, body, key):
        if path.endswith("/egress-journal") and self.fail_journal:
            self.fail_journal -= 1
            raise ConnectionError("lost")
        self.posts.append((path, body, key))
        return {}


def test_journal_posted_once_with_replayable_key(tmp_path):
    journal = tmp_path / "j.jsonl"
    log = EgressJournal(journal)
    log.record("api.example.org", 443, "allow", "closed", 10, 20, 5)
    log.record("evil.example", 443, "deny", "not_allowlisted")
    log.close()
    channel = Channel()
    channel.fail_journal = 1
    runtime = CpuRuntime(
        tmp_path / "rt", _private(tmp_path / "in"), channel, EgressAdapter(journal), worker_id="w"
    )
    runtime.operation = egress_op()
    runtime.lease = Lease("lease-1", OP, "c" * 64, 3, _future(), 1, "p")
    observation = CpuObservation("exited", "cid", 0, True)
    # The failed post is kept in the durable outbox and retried with the same key (ADR 0054);
    # a restarted worker waits out the persisted backoff, which is simulated as elapsed here.
    with pytest.raises(ObservationDeliveryPending):
        runtime._report_egress_journal()
    with runtime._observation_connection() as connection:
        connection.execute("UPDATE reports SET next_attempt_at='2000-01-01T00:00:00+00:00'")
    runtime._report_egress_journal()
    runtime._report_egress_journal()  # already complete: no second post
    journal_posts = [p for p in channel.posts if p[0].endswith("/egress-journal")]
    assert len(journal_posts) == 1
    path, body, key = journal_posts[0]
    assert path == f"/worker/operations/{OP}/egress-journal"
    assert body["lease_id"] == "lease-1" and body["expected_epoch"] == 3
    assert body["journal_sha256"] == journal_sha256(journal)
    assert body["summary"]["connections_allowed"] == 1
    assert body["summary"]["connections_denied"] == 1
    assert observation.termination_confirmed
    runtime.close()


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def _future():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(hours=1)


def test_report_posts_journal_only_for_terminal_egress(tmp_path):
    journal = tmp_path / "j.jsonl"
    EgressJournal(journal).close()
    channel = Channel()
    runtime = CpuRuntime(
        tmp_path / "rt", _private(tmp_path / "in"), channel, EgressAdapter(journal), worker_id="w"
    )
    runtime.operation = egress_op()
    runtime.lease = Lease("lease-1", OP, "c" * 64, 3, _future(), 1, "p")
    running = CpuObservation("running", "cid")
    runtime.report(DriverResult("running", running))
    assert not any(p[0].endswith("/egress-journal") for p in channel.posts)
    runtime.report(DriverResult("terminal", CpuObservation("exited", "cid", 0, True)))
    assert sum(p[0].endswith("/egress-journal") for p in channel.posts) == 1
    runtime.close()


# --- proxy ------------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


async def _echo_server():
    async def handle(reader, writer):
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def _connect(proxy_port: int, request: bytes):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(request)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    return head, reader, writer


def _proxy(tmp_path, upstream_port=0, cap=1000, host="api.example.org", **kwargs):
    journal = EgressJournal(tmp_path / "journal.jsonl")

    async def resolver(h, p):
        return ["127.0.0.1"]

    proxy = EgressProxy(
        [(host, 443)], cap, journal, resolver=resolver, idle_timeout=2, header_timeout=1, **kwargs
    )
    return proxy, journal


def test_allowed_connect_tunnels_bytes_and_journals(tmp_path):
    async def scenario():
        echo = await _echo_server()
        eport = echo.sockets[0].getsockname()[1]

        async def opener_resolver(h, p):
            return ["127.0.0.1"]

        journal = EgressJournal(tmp_path / "journal.jsonl")
        proxy = EgressProxy(
            [("api.example.org", eport)],
            1000,
            journal,
            resolver=opener_resolver,
            is_public=lambda a: True,
            idle_timeout=2,
        )
        server = await proxy.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        head, reader, writer = await _connect(
            port, f"CONNECT API.example.org:{eport} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        )
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"hello")
        await writer.drain()
        assert await reader.readexactly(5) == b"hello"
        writer.close()
        await asyncio.sleep(0.2)
        server.close()
        echo.close()
        journal.close()
        return eport

    eport = _run(scenario())
    entries = [json.loads(x) for x in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert entries[0]["decision"] == "allow" and entries[0]["port"] == eport
    assert entries[0]["bytes_up"] == 5 and entries[0]["bytes_down"] == 5
    assert set(entries[0]) == {
        "ts",
        "host",
        "port",
        "decision",
        "reason",
        "bytes_up",
        "bytes_down",
        "duration_ms",
    }
    summary = summarize_journal(tmp_path / "journal.jsonl")
    assert summary["connections_allowed"] == 1 and summary["bytes_up"] == 5
    assert len(journal_sha256(tmp_path / "journal.jsonl")) == 64


def _deny_scenario(tmp_path, request: bytes, **proxy_kwargs):
    async def scenario():
        proxy, journal = _proxy(tmp_path, **proxy_kwargs)
        server = await proxy.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        head, _, writer = await _connect(port, request)
        writer.close()
        server.close()
        journal.close()
        return head

    return _run(scenario())


def _entries(tmp_path):
    return [json.loads(x) for x in (tmp_path / "journal.jsonl").read_text().splitlines()]


def test_non_allowlisted_host_and_port_forbidden(tmp_path):
    head = _deny_scenario(tmp_path, b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403")
    e = _entries(tmp_path)[0]
    assert (e["decision"], e["reason"]) == ("deny", "not_allowlisted")


def test_wrong_port_forbidden(tmp_path):
    head = _deny_scenario(tmp_path, b"CONNECT api.example.org:80 HTTP/1.1\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403")


def test_non_connect_method_is_405(tmp_path):
    head = _deny_scenario(tmp_path, b"GET http://api.example.org/ HTTP/1.1\r\nHost: x\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 405")
    assert _entries(tmp_path)[0]["reason"] == "method"


def test_private_resolution_refused_with_real_predicate(tmp_path):
    head = _deny_scenario(tmp_path, b"CONNECT api.example.org:443 HTTP/1.1\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403")
    assert _entries(tmp_path)[0]["reason"] == "non_public_address"


def test_any_non_public_address_refuses_mixed_resolution(tmp_path):
    async def scenario():
        journal = EgressJournal(tmp_path / "journal.jsonl")

        async def resolver(h, p):
            return ["8.8.8.8", "10.0.0.5"]

        proxy = EgressProxy([("api.example.org", 443)], 100, journal, resolver=resolver)
        server = await proxy.start("127.0.0.1", 0)
        head, _, writer = await _connect(
            server.sockets[0].getsockname()[1], b"CONNECT api.example.org:443 HTTP/1.1\r\n\r\n"
        )
        writer.close()
        server.close()
        journal.close()
        return head

    assert _run(scenario()).startswith(b"HTTP/1.1 403")


def test_cap_exceeded_closes_tunnel(tmp_path):
    async def scenario():
        echo = await _echo_server()
        eport = echo.sockets[0].getsockname()[1]
        journal = EgressJournal(tmp_path / "journal.jsonl")

        async def resolver(h, p):
            return ["127.0.0.1"]

        proxy = EgressProxy(
            [("api.example.org", eport)], 10, journal, resolver=resolver, is_public=lambda a: True
        )
        server = await proxy.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        head, reader, writer = await _connect(
            port, f"CONNECT api.example.org:{eport} HTTP/1.1\r\n\r\n".encode()
        )
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"x" * 100)
        await writer.drain()
        received = await asyncio.wait_for(reader.read(), 5)  # EOF: tunnel closed
        assert len(received) <= 10
        writer.close()
        # a new connection after the cap is refused
        head2, _, writer2 = await _connect(
            port, f"CONNECT api.example.org:{eport} HTTP/1.1\r\n\r\n".encode()
        )
        writer2.close()
        await asyncio.sleep(0.2)
        server.close()
        echo.close()
        journal.close()
        return head2

    assert _run(scenario()).startswith(b"HTTP/1.1 403")
    entries = _entries(tmp_path)
    assert entries[0]["reason"] == "cap" and entries[0]["bytes_up"] <= 10
    assert entries[1]["reason"] == "cap" and entries[1]["decision"] == "deny"


def test_oversized_header_rejected(tmp_path):
    head = _deny_scenario(
        tmp_path, b"CONNECT api.example.org:443 HTTP/1.1\r\nX: " + b"a" * 20000 + b"\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 400") or head == b""


def test_public_predicate():
    assert is_public_address("8.8.8.8")
    for bad in (
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "::1",
        "::ffff:10.0.0.1",
        "2002:0a00:0001::1",
        "64:ff9b::a00:1",
        "fe80::1",
        "garbage",
    ):
        assert not is_public_address(bad)


def test_python_http_client_tunnel_is_accepted(tmp_path):
    """urllib/http.client send "CONNECT host:port HTTP/1.0"; the allowlist still applies."""
    import http.client

    async def scenario():
        echo = await _echo_server()
        eport = echo.sockets[0].getsockname()[1]

        async def resolver(h, p):
            return ["127.0.0.1"]

        journal = EgressJournal(tmp_path / "journal.jsonl")
        proxy = EgressProxy(
            [("api.example.org", eport)],
            1000,
            journal,
            resolver=resolver,
            is_public=lambda a: True,
            idle_timeout=2,
        )
        server = await proxy.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        def tunnel(host):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.set_tunnel(host, eport)
            try:
                connection.connect()  # sends the CONNECT line and reads the proxy's status
                return "open"
            except OSError as error:
                return str(error)
            finally:
                connection.close()

        allowed = await asyncio.to_thread(tunnel, "api.example.org")
        refused = await asyncio.to_thread(tunnel, "evil.example")
        await asyncio.sleep(0.2)
        server.close()
        echo.close()
        journal.close()
        return allowed, refused

    allowed, refused = _run(scenario())
    assert allowed == "open"
    assert "403" in refused


@pytest.mark.parametrize("version", [b"HTTP/2", b"HTTP/0.9", b"http/1.1"])
def test_other_connect_versions_are_still_405(tmp_path, version):
    head = _deny_scenario(tmp_path, b"CONNECT api.example.org:443 " + version + b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 405")


def test_raw_http10_connect_allowed_tunnels_and_journals(tmp_path):
    """Version-independent: the exact HTTP/1.0 line, without relying on the local http.client."""

    async def scenario():
        echo = await _echo_server()
        eport = echo.sockets[0].getsockname()[1]

        async def resolver(h, p):
            return ["127.0.0.1"]

        journal = EgressJournal(tmp_path / "journal.jsonl")
        proxy = EgressProxy(
            [("api.example.org", eport)],
            1000,
            journal,
            resolver=resolver,
            is_public=lambda a: True,
            idle_timeout=2,
        )
        server = await proxy.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        head, reader, writer = await _connect(
            port, f"CONNECT api.example.org:{eport} HTTP/1.0\r\n\r\n".encode()
        )
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"ping")
        await writer.drain()
        assert await reader.readexactly(4) == b"ping"
        writer.close()
        await asyncio.sleep(0.2)
        server.close()
        echo.close()
        journal.close()
        return eport

    eport = _run(scenario())
    entries = _entries(tmp_path)
    assert [(e["host"], e["port"], e["decision"]) for e in entries] == [
        ("api.example.org", eport, "allow")
    ]


def test_raw_http10_connect_not_allowlisted_is_403_and_journalled(tmp_path):
    head = _deny_scenario(tmp_path, b"CONNECT evil.example:443 HTTP/1.0\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403")
    assert [(e["host"], e["decision"], e["reason"]) for e in _entries(tmp_path)] == [
        ("evil.example", "deny", "not_allowlisted")
    ]


def test_raw_http10_connect_to_a_non_public_address_is_403(tmp_path):
    head = _deny_scenario(tmp_path, b"CONNECT api.example.org:443 HTTP/1.0\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403")
    assert [(e["decision"], e["reason"]) for e in _entries(tmp_path)] == [
        ("deny", "non_public_address")
    ]


async def _drip_server(chunks: int, every: float):
    """Upstream that reads the request once, then sends one byte every ``every`` seconds."""

    async def handle(reader, writer):
        await reader.read(1024)
        for _ in range(chunks):
            await asyncio.sleep(every)
            writer.write(b"x")
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def _idle_scenario(tmp_path, chunks: int):
    async def scenario():
        drip = await _drip_server(chunks, 0.1)
        dport = drip.sockets[0].getsockname()[1]

        async def resolver(h, p):
            return ["127.0.0.1"]

        journal = EgressJournal(tmp_path / "journal.jsonl")
        proxy = EgressProxy(
            [("api.example.org", dport)],
            1000,
            journal,
            resolver=resolver,
            is_public=lambda a: True,
            idle_timeout=0.35,
        )
        server = await proxy.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        head, reader, writer = await _connect(
            port, f"CONNECT api.example.org:{dport} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        )
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"GET")  # then the client side stays silent, like a large HTTPS download
        await writer.drain()
        received = b""
        while chunk := await reader.read(64):
            received += chunk
        writer.close()
        await asyncio.sleep(0.2)
        server.close()
        drip.close()
        journal.close()
        return received

    received = _run(scenario())
    entries = [json.loads(x) for x in (tmp_path / "journal.jsonl").read_text().splitlines()]
    return received, entries[0]


def test_quiet_request_side_does_not_cut_a_flowing_download(tmp_path):
    # 12 bytes over ~1.2 s, far past the 0.35 s idle timeout of the silent client->upstream side.
    received, entry = _idle_scenario(tmp_path, 12)
    assert received == b"x" * 12
    assert entry["reason"] == "closed" and entry["bytes_down"] == 12


def test_tunnel_with_no_traffic_either_way_still_closes_idle(tmp_path):
    async def scenario():
        async def silent(reader, writer):
            await asyncio.sleep(5)
            writer.close()

        upstream = await asyncio.start_server(silent, "127.0.0.1", 0)
        uport = upstream.sockets[0].getsockname()[1]

        async def resolver(h, p):
            return ["127.0.0.1"]

        journal = EgressJournal(tmp_path / "journal.jsonl")
        proxy = EgressProxy(
            [("api.example.org", uport)],
            1000,
            journal,
            resolver=resolver,
            is_public=lambda a: True,
            idle_timeout=0.3,
        )
        server = await proxy.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        head, reader, writer = await _connect(
            port, f"CONNECT api.example.org:{uport} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        )
        assert head.startswith(b"HTTP/1.1 200")
        assert await asyncio.wait_for(reader.read(64), 3) == b""
        writer.close()
        await asyncio.sleep(0.1)
        server.close()
        upstream.close()
        journal.close()

    _run(scenario())
    entry = json.loads((tmp_path / "journal.jsonl").read_text().splitlines()[0])
    assert entry["reason"] == "idle"
