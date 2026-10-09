"""Fixed Engine endpoint; no ambient Docker context, CLI, auth discovery or exec API."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl
from urllib.parse import quote, urlencode, urlsplit


class DockerTransportUnknown(Exception):
    """Closed diagnostic: caller must reconcile, never log raw Engine errors."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class FixedDockerTransport:
    def __init__(self, endpoint: str, *, tls: ssl.SSLContext | None = None):
        parsed = urlsplit(endpoint)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("fixed credential-free Docker endpoint required")
        if parsed.scheme == "unix":
            if parsed.netloc or not parsed.path.startswith("/") or "\0" in parsed.path:
                raise ValueError("absolute worker-local socket reference required")
        elif parsed.scheme == "https":
            if not parsed.hostname or parsed.path not in {"", "/"} or tls is None:
                raise ValueError("verified fixed TLS Docker endpoint required")
            if tls.verify_mode != ssl.CERT_REQUIRED or not tls.check_hostname:
                raise ValueError("verified TLS is mandatory")
        else:
            raise ValueError("ambient/plaintext Docker endpoints are unsupported")
        self.endpoint, self.parsed, self.tls = endpoint, parsed, tls
        self.endpoint_digest = hashlib.sha256(endpoint.encode()).hexdigest()

    def _call(self, method, path, *, body=None, limit=65536, timeout_seconds=2):
        timeout = min(10.0, max(0.001, timeout_seconds))
        conn = (
            _UnixConnection(self.parsed.path, timeout)
            if self.parsed.scheme == "unix"
            else http.client.HTTPSConnection(
                self.parsed.hostname, self.parsed.port or 443, context=self.tls, timeout=timeout
            )
        )
        try:
            encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode()
            if encoded is not None and len(encoded) > 65536:
                raise DockerTransportUnknown()
            conn.request(method, "/v1.47" + path, encoded, {"Content-Type": "application/json"})
            response = conn.getresponse()
            data = response.read(limit + 1)
            if len(data) > limit:
                raise DockerTransportUnknown()
            if response.status == 404:
                return None
            if response.status not in {200, 201, 204, 304}:
                raise DockerTransportUnknown()
            return data
        except (OSError, ValueError, http.client.HTTPException):
            raise DockerTransportUnknown() from None
        finally:
            conn.close()

    def _json(self, method, path, **kwargs):
        data = self._call(method, path, **kwargs)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise DockerTransportUnknown() from None

    @staticmethod
    def _id(value):
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("exact Docker container ID required")
        return value

    def info(self, **kwargs):
        return self._json("GET", "/info", **kwargs)

    def image(self, reference, **kwargs):
        if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}", reference):
            raise ValueError("immutable image reference required")
        return self._json("GET", "/images/" + quote(reference, safe="") + "/json", **kwargs)

    def create(self, name, config, **kwargs):
        if not re.fullmatch(r"tessera-docker-[a-f0-9]{32}", name):
            raise ValueError("owned container name required")
        return self._json(
            "POST", "/containers/create?" + urlencode({"name": name}), body=config, **kwargs
        )

    def inspect(self, identity, **kwargs):
        if not re.fullmatch(r"(?:[a-f0-9]{64}|tessera-docker-[a-f0-9]{32})", identity):
            raise ValueError("exact owned container selector required")
        return self._json("GET", "/containers/" + quote(identity, safe="") + "/json", **kwargs)

    def start(self, identity, **kwargs):
        return self._call("POST", "/containers/" + self._id(identity) + "/start", **kwargs)

    def signal(self, identity, signal, **kwargs):
        if signal not in {"SIGTERM", "SIGKILL"}:
            raise ValueError("unsupported owned stop signal")
        return self._call(
            "POST",
            "/containers/" + self._id(identity) + "/kill?" + urlencode({"signal": signal}),
            **kwargs,
        )

    def archive(self, identity, path, *, limit, **kwargs):
        if path not in {"/scratch/tessera-result.json", "/outputs"}:
            raise ValueError("fixed output paths only")
        return self._call(
            "GET",
            "/containers/" + self._id(identity) + "/archive?" + urlencode({"path": path}),
            limit=limit,
            **kwargs,
        )
