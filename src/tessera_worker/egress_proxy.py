"""Allowlist CONNECT proxy for governed sandbox egress (ADR 0034).

Standard library only and self-contained: this file is also mounted read-only
into the proxy container and run as ``python egress_proxy.py ...``.

Only ``CONNECT host:port`` to an exact allowlisted destination is tunnelled. The
proxy resolves the name itself, refuses it if ANY resolved address is not
globally routable, and connects to the address it checked. TLS is not
intercepted. Every decision is appended to a JSONL journal.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import signal
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

MAX_HEADER_BYTES = 8 * 1024
HEADER_TIMEOUT_SECONDS = 10.0
IDLE_TIMEOUT_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 10.0
CHUNK = 16 * 1024
MAX_SUMMARY_DESTINATIONS = 256

Resolver = Callable[[str, int], Awaitable[list[str]]]
PublicPredicate = Callable[[str], bool]

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


def is_public_address(value: str) -> bool:
    """True only for globally routable addresses, including embedded IPv4 forms."""
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_address(str(address.ipv4_mapped))
        if address.sixtofour is not None:
            return is_public_address(str(address.sixtofour)) and address.is_global
        if address.teredo is not None or address in _NAT64_LOCAL:
            return False  # Embedded or locally-scoped translation ranges are refused.
        if address in _NAT64:
            return is_public_address(str(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)))
    return address.is_global


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


class EgressJournal:
    """Append-only JSONL, flushed per record. Path ``-`` writes to stdout."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._stdout = str(path) == "-"
        self._file = sys.stdout if self._stdout else open(self.path, "a", encoding="utf-8")  # noqa: SIM115

    def record(
        self,
        host: str,
        port: int,
        decision: str,
        reason: str,
        bytes_up: int = 0,
        bytes_down: int = 0,
        duration_ms: int = 0,
    ) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": host[:255],
            "port": port,
            "decision": decision,
            "reason": reason,
            "bytes_up": bytes_up,
            "bytes_down": bytes_down,
            "duration_ms": duration_ms,
        }
        self._file.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._stdout:
            self._file.flush()
        else:
            self._file.close()


class EgressProxy:
    def __init__(
        self,
        destinations: list[tuple[str, int]],
        max_egress_bytes: int,
        journal: EgressJournal,
        *,
        resolver: Resolver = default_resolver,
        is_public: PublicPredicate = is_public_address,
        header_timeout: float = HEADER_TIMEOUT_SECONDS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self.allowed = {(host.lower(), int(port)) for host, port in destinations}
        self.max_egress_bytes = max_egress_bytes
        self.journal = journal
        self.resolver = resolver
        self.is_public = is_public
        self.header_timeout = header_timeout
        self.idle_timeout = idle_timeout
        self.total_bytes = 0
        self.cap_hit = False

    async def start(self, host: str, port: int) -> asyncio.base_events.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEADER_BYTES)

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: str) -> None:
        try:
            writer.write(
                f"HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode()
            )
            await writer.drain()
        except OSError:
            pass

    async def _read_head(self, reader: asyncio.StreamReader) -> list[str] | None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.header_timeout)
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ConnectionError,
        ):
            return None
        if len(raw) > MAX_HEADER_BYTES:
            return None
        try:
            return raw.decode("ascii").split("\r\n")
        except UnicodeDecodeError:
            return None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        host, port = "", 0
        try:
            lines = await self._read_head(reader)
            if lines is None:
                self.journal.record("", 0, "deny", "bad_request")
                await self._reply(writer, "400 Bad Request")
                return
            parts = lines[0].split(" ")
            # HTTP/1.0 too: Python <=3.11's http.client (urllib) sends its tunnel request as
            # "CONNECT host:port HTTP/1.0"; the method and target rules are the same.
            if len(parts) != 3 or parts[0] != "CONNECT" or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}:
                self.journal.record(parts[1][:255] if len(parts) > 1 else "", 0, "deny", "method")
                await self._reply(writer, "405 Method Not Allowed")
                return
            target = parts[1]
            name, separator, port_text = target.rpartition(":")
            if (
                not separator
                or not name
                or not port_text.isdigit()
                or not 1 <= int(port_text) <= 65535
                or any(character in name for character in "[]/@ ")
            ):
                self.journal.record(target[:255], 0, "deny", "bad_target")
                await self._reply(writer, "400 Bad Request")
                return
            host, port = name.lower().rstrip("."), int(port_text)
            if (host, port) not in self.allowed:
                self.journal.record(host, port, "deny", "not_allowlisted")
                await self._reply(writer, "403 Forbidden")
                return
            if self.cap_hit or self.total_bytes >= self.max_egress_bytes:
                self.cap_hit = True
                self.journal.record(host, port, "deny", "cap")
                await self._reply(writer, "403 Forbidden")
                return
            try:
                addresses = await asyncio.wait_for(
                    self.resolver(host, port), CONNECT_TIMEOUT_SECONDS
                )
            except (OSError, TimeoutError):
                self.journal.record(host, port, "deny", "resolve_failed")
                await self._reply(writer, "502 Bad Gateway")
                return
            if not addresses or not all(self.is_public(address) for address in addresses):
                self.journal.record(host, port, "deny", "non_public_address")
                await self._reply(writer, "403 Forbidden")
                return
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(addresses[0], port), CONNECT_TIMEOUT_SECONDS
                )
            except (OSError, TimeoutError):
                self.journal.record(host, port, "deny", "connect_failed")
                await self._reply(writer, "502 Bad Gateway")
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            counts = {"up": 0, "down": 0}
            reason = await self._tunnel(reader, writer, upstream_reader, upstream_writer, counts)
            self.journal.record(
                host,
                port,
                "allow",
                reason,
                counts["up"],
                counts["down"],
                int((time.monotonic() - started) * 1000),
            )
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def _pipe(
        self,
        source: asyncio.StreamReader,
        sink: asyncio.StreamWriter,
        counts: dict[str, int],
        key: str,
        activity: dict[str, float] | None = None,
    ) -> str:
        # ``activity`` is shared by both directions of one tunnel: a tunnel is idle only when
        # NEITHER direction moved a byte for ``idle_timeout``. A download whose request side is
        # quiet (an HTTPS GET of a large file) must not be cut while bytes are still arriving.
        activity = activity if activity is not None else {"last": time.monotonic()}
        while True:
            try:
                data = await asyncio.wait_for(source.read(CHUNK), self.idle_timeout)
            except TimeoutError:
                if time.monotonic() - activity["last"] < self.idle_timeout:
                    continue
                return "idle"
            except (ConnectionError, OSError):
                return "closed"
            if not data:
                return "closed"
            activity["last"] = time.monotonic()
            remaining = self.max_egress_bytes - self.total_bytes
            if len(data) > remaining:
                self.cap_hit = True
                if remaining > 0:
                    data = data[:remaining]
                    self.total_bytes += remaining
                    counts[key] += remaining
                    try:
                        sink.write(data)
                        await sink.drain()
                    except (ConnectionError, OSError):
                        pass
                return "cap"
            self.total_bytes += len(data)
            counts[key] += len(data)
            try:
                sink.write(data)
                await sink.drain()
            except (ConnectionError, OSError):
                return "closed"

    async def _tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        counts: dict[str, int],
    ) -> str:
        activity = {"last": time.monotonic()}
        tasks = [
            asyncio.ensure_future(self._pipe(reader, upstream_writer, counts, "up", activity)),
            asyncio.ensure_future(self._pipe(upstream_reader, writer, counts, "down", activity)),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            reasons = [task.result() for task in done]
            if self.cap_hit and "cap" not in reasons:
                reasons.append("cap")
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in tasks:
                task.cancel()
            try:
                upstream_writer.close()
            except OSError:
                pass
        for preferred in ("cap", "idle"):
            if preferred in reasons:
                return preferred
        return "closed"


def journal_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def journal_bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def summarize_journal(path: Path | str) -> dict[str, Any]:
    """Aggregate a journal into the control plane's bounded summary shape."""
    return summarize_journal_bytes(Path(path).read_bytes())


def summarize_journal_bytes(data: bytes) -> dict[str, Any]:
    """Aggregate JSONL journal bytes (file or pod log); malformed lines are skipped."""
    allowed = denied = up = down = 0
    groups: dict[tuple[str, int, str], list[int]] = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            host, port, decision = entry["host"], entry["port"], entry["decision"]
            sent, received = int(entry["bytes_up"]), int(entry["bytes_down"])
        except (KeyError, TypeError, ValueError):
            continue
        if decision not in {"allow", "deny"}:
            continue
        if decision == "allow":
            allowed += 1
        else:
            denied += 1
        up += sent
        down += received
        group = groups.setdefault((host, port, decision), [0, 0, 0])
        group[0] += 1
        group[1] += sent
        group[2] += received
    ordered = sorted(groups.items(), key=lambda item: (-item[1][0], item[0]))
    return {
        "connections_allowed": allowed,
        "connections_denied": denied,
        "bytes_up": up,
        "bytes_down": down,
        "destinations": [
            {
                "host": host,
                "port": port,
                "decision": decision,
                "connections": values[0],
                "bytes_up": values[1],
                "bytes_down": values[2],
            }
            for (host, port, decision), values in ordered[:MAX_SUMMARY_DESTINATIONS]
        ],
    }


def parse_config(config: dict[str, Any]) -> tuple[list[tuple[str, int]], int]:
    destinations = [(item["host"], int(item["port"])) for item in config["destinations"]]
    return destinations, int(config["max_egress_bytes"])


def load_config(path: Path | str) -> tuple[list[tuple[str, int]], int]:
    return parse_config(json.loads(Path(path).read_text(encoding="utf-8")))


def load_config_b64(value: str) -> tuple[list[tuple[str, int]], int]:
    return parse_config(json.loads(base64.b64decode(value, validate=True).decode("utf-8")))


async def _serve(
    config: str | None, config_b64: str | None, journal_path: str, listen: str
) -> None:
    destinations, cap = (
        load_config_b64(config_b64) if config_b64 is not None else load_config(config)
    )
    host, _, port = listen.rpartition(":")
    journal = EgressJournal(journal_path)
    proxy = EgressProxy(destinations, cap, journal)
    server = await proxy.start(host or "0.0.0.0", int(port))  # noqa: S104
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, name), stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    async with server:
        await stop.wait()
    journal.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="egress_proxy")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config")
    source.add_argument("--config-b64", dest="config_b64")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--listen", default="0.0.0.0:3128")  # noqa: S104
    arguments = parser.parse_args()
    try:
        asyncio.run(
            _serve(arguments.config, arguments.config_b64, arguments.journal, arguments.listen)
        )
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
