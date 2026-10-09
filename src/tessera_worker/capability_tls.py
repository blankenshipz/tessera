"""Anonymous pinned-IP HTTPS GET primitive for a future isolated runner.

No requests/urllib proxy handling, ambient CA lookup, redirect, DNS lookup, client
certificate, credential discovery, decompression, shell or retry. Not wired into
the broker: running it on the host is not an acceptable sandbox implementation.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
import time
from datetime import UTC, datetime

from tessera_control.store import canonical_digest

from .capability_activation import PinnedDestination, destination_host, stamp
from .capability_effects import CHUNK_BYTES, DownloadResponse


class DownloadDenied(ValueError):
    def __init__(self):
        super().__init__("pinned HTTPS transfer rejected")


class PinnedHTTPSDownload:
    """One connection, one GET, bounded wire headers/body and a total deadline.

    Fresh authenticated DNS observations are supplied with the immutable pin set;
    DNS is deliberately not re-resolved between checking and connecting. The caller
    must independently validate their provenance. OS calls use the remaining total
    deadline, not a fresh timeout for every byte. An external watchdog is still
    required for hard preemption, including kernel/SSL implementation failures.
    """

    def __init__(
        self,
        destination,
        *,
        ca_pem,
        expected_size,
        expected_sha256,
        content_type,
        max_seconds,
        monotonic=time.monotonic,
        now=None,
    ):
        # Revalidate serialized input, even if somebody used model_construct/copy.
        try:
            self.destination = PinnedDestination.model_validate_json(destination.model_dump_json())
        except Exception:
            raise DownloadDenied from None
        if (
            type(expected_size) is not int
            or not 0 <= expected_size <= 16 * 1024 * 1024
            or type(max_seconds) is not int
            or not 1 <= max_seconds <= 120
            or not isinstance(ca_pem, str)
            or len(ca_pem) > 128 * 1024
            or len(expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha256)
        ):
            raise DownloadDenied
        self.expected_size, self.expected_sha256 = expected_size, expected_sha256
        self.content_type, self.max_seconds = content_type, max_seconds
        self.clock, self.now = monotonic, now or (lambda: datetime.now(UTC))
        # SSLContext, not create_default_context: no SSL_CERT_FILE/SSL_CERT_DIR or
        # system trust-store fallback. Only the explicitly supplied trust anchors.
        try:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.context.verify_mode = ssl.CERT_REQUIRED
            self.context.check_hostname = True
            self.context.load_verify_locations(cadata=ca_pem)
        except Exception:
            raise DownloadDenied from None
        self._socket = None
        self._opened = False
        self._closed = False
        self._buffer = b""
        self._offset = 0
        self._hash = hashlib.sha256()
        self.verified = False

    def _timeout(self, timeout):
        remaining = min(timeout, self._deadline - self.clock())
        if remaining <= 0:
            raise DownloadDenied
        self._socket.settimeout(remaining)

    def _receive(self, count, timeout):
        self._timeout(timeout)
        result = self._socket.recv(count)
        # Reject a late reply even if a fake or broken socket ignored its timeout.
        if self.clock() >= self._deadline or len(result) > count:
            raise DownloadDenied
        return result

    def open(self, *, timeout):
        if self._opened or self._closed:
            raise DownloadDenied
        self._opened = True  # Even a failed connection cannot be retried.
        self._deadline = self.clock() + self.max_seconds
        host, port, path = destination_host(self.destination)
        try:
            age = (self.now() - stamp(self.destination.dns_observed_at)).total_seconds()
            if not 0 <= age <= 5 or timeout <= 0:
                raise DownloadDenied
            address = self.destination.addresses[0]  # no fallback/retry
            family = (
                socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
            )
            raw = socket.socket(family, socket.SOCK_STREAM)
            self._socket = raw
            self._timeout(timeout)
            # A numeric address passed to this AF-specific socket performs no DNS.
            raw.connect((address, port))
            self._timeout(timeout)
            self._socket = self.context.wrap_socket(raw, server_hostname=host)
            self._timeout(timeout)
            peer = str(ipaddress.ip_address(self._socket.getpeername()[0]))
            der = self._socket.getpeercert(binary_form=True)
            cert = self._socket.getpeercert()
            if (
                peer != address
                or hashlib.sha256(der).hexdigest() != self.destination.certificate_sha256
                or canonical_digest(cert.get("issuer", ())) != self.destination.issuer_sha256
                or ("DNS", host) not in cert.get("subjectAltName", ())
            ):
                raise DownloadDenied
            # SSL has already validated the chain, validity and SNI hostname. The
            # exact SAN check additionally disallows CN fallback/wildcard pins.
            authority = host if port == 443 else f"{host}:{port}"
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {authority}\r\n"
                "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            self._timeout(timeout)
            self._socket.sendall(request)
            header = b""
            while b"\r\n\r\n" not in header:
                part = self._receive(min(4096, 16385 - len(header)), timeout)
                if not part or len(header) + len(part) > 16384:
                    raise DownloadDenied
                header += part
            head, self._buffer = header.split(b"\r\n\r\n", 1)
            lines = head.split(b"\r\n")
            if len(lines) > 65 or lines[0] not in {b"HTTP/1.1 200 OK", b"HTTP/1.0 200 OK"}:
                raise DownloadDenied
            fields = {}
            for line in lines[1:]:
                name, value = line.split(b":", 1)
                if (
                    not name
                    or any(c not in b"abcdefghijklmnopqrstuvwxyz-" for c in name.lower())
                    or name.lower() in fields
                    or any(c < 32 or c > 126 for c in value)
                ):
                    raise DownloadDenied
                fields[name.lower()] = value.strip()
            if (
                b"transfer-encoding" in fields
                or b"location" in fields
                or fields.get(b"content-encoding", b"identity") != b"identity"
                or fields.get(b"content-length") != str(self.expected_size).encode()
                or fields.get(b"content-type") != self.content_type.encode("ascii")
                or len(self._buffer) > self.expected_size
            ):
                raise DownloadDenied
            return DownloadResponse(
                self.destination.url,
                self.destination.addresses,
                200,
                self.content_type,
                self.expected_size,
            )
        except Exception:
            self.close()
            raise DownloadDenied from None

    def read(self, offset, *, limit, timeout):
        try:
            if (
                not self._opened
                or self._closed
                or offset != self._offset
                or type(limit) is not int
                or not 1 <= limit <= CHUNK_BYTES
            ):
                raise DownloadDenied
            self._timeout(timeout)
            if self._buffer:
                chunk, self._buffer = self._buffer[:limit], self._buffer[limit:]
            else:
                chunk = self._receive(min(limit, self.expected_size - self._offset + 1), timeout)
            if self._offset + len(chunk) > self.expected_size:
                raise DownloadDenied
            self._offset += len(chunk)
            self._hash.update(chunk)
            if not chunk:
                if (
                    self._offset != self.expected_size
                    or self._hash.hexdigest() != self.expected_sha256
                ):
                    raise DownloadDenied
                self.verified = True
                self.close()
            return chunk
        except Exception:
            self.close()
            raise DownloadDenied from None

    def close(self):
        self._closed = True
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
