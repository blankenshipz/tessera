"""Explicit, scope-limited Kubernetes authentication; no discovery, exec plugins or retries."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

import yaml
from pydantic import Field, model_validator

from tessera_control.capability_models import Digest, Identifier
from tessera_control.store import canonical_digest

from .capability_activation import Frozen, Name, Principal
from .capability_effects import BackendBlocked


class CollectionError(BackendBlocked):
    """Only fixed classifications cross the boundary, never upstream exception text."""

    def __init__(self, category):
        if category not in {
            "auth-denied",
            "timeout",
            "malformed-response",
            "stale",
            "contradictory",
            "unavailable",
        }:
            category = "unavailable"
        self.category = category
        super().__init__("Kubernetes evidence: " + category)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid(_):
        raise ValueError

    result = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    if type(result) is not dict:
        raise ValueError
    return result


class ClientBinding(Frozen):
    mode: Literal["kubeconfig", "service-account"]
    context: Name
    server: str = Field(max_length=256)
    ca_sha256: Digest
    cluster_uid: Identifier  # pinned kube-system Namespace UID; Kubernetes has no cluster UID API
    namespace: Name
    namespace_uid: Identifier
    org_id: Identifier
    project_id: Identifier
    plan_digest: Digest
    identity: Principal
    pv_name: Name
    timeout_seconds: int = Field(ge=1, le=5)
    max_seconds: int = Field(ge=1, le=120)
    max_requests: int = Field(ge=1, le=256)
    max_response_bytes: int = Field(ge=1024, le=2 * 1024 * 1024)
    retries: Literal[0] = 0

    @model_validator(mode="after")
    def target(self):
        p = urlsplit(self.server)
        if p.scheme != "https" or p.path or p.query or p.fragment or p.username or p.password:
            raise ValueError("explicit HTTPS API server required")
        ipaddress.ip_address(p.hostname)  # numeric pin; no DNS/ambient resolver
        if not p.port or self.server != f"https://{p.netloc}":
            raise ValueError("explicit API port required")
        if self.mode == "service-account" and not self.identity.startswith(
            f"system:serviceaccount:{self.namespace}:"
        ):
            raise ValueError("explicit in-cluster service-account binding required")
        return self

    @property
    def cluster_digest(self):
        return canonical_digest([self.server, self.ca_sha256, self.cluster_uid])


@dataclass(frozen=True, repr=False)
class Credentials:
    token: str
    ca_pem: bytes

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9._~-]{1,16384}", self.token):
            raise CollectionError("auth-denied")
        if not 1 <= len(self.ca_pem) <= 128 * 1024:
            raise CollectionError("auth-denied")


def private_read(path, limit):
    # Explicit references only. No default path, environment, home expansion or plugin.
    import os
    import stat

    path = Path(path)
    if not path.is_absolute():
        raise CollectionError("auth-denied")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError
        return value
    finally:
        os.close(fd)


class _KubeconfigLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("credential aliases are unsupported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("ambiguous credential configuration")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_credentials(binding, *, kubeconfig=None, token_file=None, ca_file=None):
    try:
        binding = ClientBinding.model_validate_json(binding.model_dump_json())
        if binding.mode == "kubeconfig":
            if kubeconfig is None or token_file is not None or ca_file is not None:
                raise ValueError
            config = yaml.load(private_read(kubeconfig, 256 * 1024), Loader=_KubeconfigLoader)

            # Never consult current-context; selection must be unique and explicit.
            def select(items, name, field):
                values = [item[field] for item in items if item["name"] == name]
                if len(values) != 1:
                    raise ValueError
                return values[0]

            context = select(config["contexts"], binding.context, "context")
            if (
                set(context) != {"cluster", "user", "namespace"}
                or context["namespace"] != binding.namespace
            ):
                raise ValueError
            cluster = select(config["clusters"], context["cluster"], "cluster")
            user = select(config["users"], context["user"], "user")
            if set(cluster) != {"server", "certificate-authority-data"} or set(user) != {"token"}:
                raise ValueError
            if cluster["server"] != binding.server:
                raise ValueError
            credentials = Credentials(
                user["token"],
                base64.b64decode(cluster["certificate-authority-data"], validate=True),
            )
        else:
            if kubeconfig is not None or token_file is None or ca_file is None:
                raise ValueError
            credentials = Credentials(
                private_read(token_file, 16384).decode().strip(), private_read(ca_file, 128 * 1024)
            )
        if hashlib.sha256(credentials.ca_pem).hexdigest() != binding.ca_sha256:
            raise ValueError
        return credentials
    except Exception:
        raise CollectionError("auth-denied") from None


@dataclass(frozen=True)
class APIRequest:
    method: Literal["GET", "POST", "DELETE"]
    path: str
    body: bytes = b""


@dataclass(frozen=True, repr=False)
class APIResponse:
    status: int
    body: bytes
    audit_id: str


class KubernetesTransport(Protocol):
    def send(self, request: APIRequest, *, deadline: float, limit: int) -> APIResponse: ...


class HTTPSKubernetesTransport:
    """One numeric-IP TLS socket per call; bounded headers and Content-Length body.

    Chunking/redirects/compression are rejected, not retried. Every read resets its
    timeout to the remaining *total* deadline. No proxy or ambient CA store.
    """

    def __init__(self, binding, credentials, *, monotonic=time.monotonic):
        binding = ClientBinding.model_validate_json(binding.model_dump_json())
        self.binding, self._credentials, self.clock = binding, credentials, monotonic
        if hashlib.sha256(credentials.ca_pem).hexdigest() != binding.ca_sha256:
            raise CollectionError("auth-denied")
        try:
            self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self._tls.minimum_version = ssl.TLSVersion.TLSv1_2
            self._tls.load_verify_locations(cadata=credentials.ca_pem.decode("ascii"))
        except Exception:
            raise CollectionError("auth-denied") from None

    def send(self, request, *, deadline, limit):
        sock = None
        try:
            p = urlsplit(self.binding.server)
            family = (
                socket.AF_INET6 if ipaddress.ip_address(p.hostname).version == 6 else socket.AF_INET
            )
            sock = socket.socket(family, socket.SOCK_STREAM)

            def timeout():
                remaining = min(self.binding.timeout_seconds, deadline - self.clock())
                if remaining <= 0:
                    raise CollectionError("timeout")
                sock.settimeout(remaining)

            timeout()
            sock.connect((p.hostname, p.port))
            timeout()
            sock = self._tls.wrap_socket(sock, server_hostname=p.hostname)
            pin = getattr(self, "peer_certificate_sha256", None)
            if (
                pin is not None
                and hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest() != pin
            ):
                raise CollectionError("auth-denied")
            if ipaddress.ip_address(sock.getpeername()[0]) != ipaddress.ip_address(p.hostname):
                raise CollectionError("auth-denied")
            timeout()
            header = (
                f"{request.method} {request.path} HTTP/1.1\r\nHost: {p.netloc}\r\n"
                f"Authorization: Bearer {self._credentials.token}\r\n"
                "Accept: application/json\r\nAccept-Encoding: identity\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n"
                f"Content-Length: {len(request.body)}\r\n\r\n"
            ).encode("ascii")
            sock.sendall(header + request.body)
            data = b""
            while b"\r\n\r\n" not in data:
                timeout()
                part = sock.recv(1)  # header cap cannot be bypassed by long lines
                if not part or len(data) >= 16384:
                    raise ValueError
                data += part
            lines = data[:-4].split(b"\r\n")
            version, status, _ = lines[0].split(b" ", 2)
            if version not in {b"HTTP/1.1", b"HTTP/1.0"} or not re.fullmatch(
                b"[1-5][0-9]{2}", status
            ):
                raise ValueError
            headers = {}
            for line in lines[1:]:
                name, value = line.split(b":", 1)
                name = name.lower()
                if name in headers or not re.fullmatch(b"[a-z-]+", name):
                    raise ValueError
                headers[name] = value.strip()
            if (
                b"transfer-encoding" in headers
                or b"location" in headers
                or headers.get(b"content-encoding", b"identity") != b"identity"
                or headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json"
            ):
                raise ValueError
            if not re.fullmatch(b"[0-9]{1,9}", headers[b"content-length"]):
                raise ValueError
            length = int(headers[b"content-length"])
            if not 0 <= length <= limit:
                raise ValueError
            body = b""
            while len(body) < length:
                timeout()
                part = sock.recv(min(65536, length - len(body)))
                if not part:
                    raise ValueError
                body += part
            timeout()
            normalized = encoded(decode(body))
            if self._credentials.token.encode() in normalized or self._credentials.ca_pem in body:
                raise CollectionError("malformed-response")
            return APIResponse(int(status), body, headers[b"audit-id"].decode("ascii"))
        except CollectionError:
            raise
        except TimeoutError:
            raise CollectionError("timeout") from None
        except ssl.SSLError:
            raise CollectionError("auth-denied") from None
        except OSError:
            raise CollectionError("unavailable") from None
        except Exception:
            raise CollectionError("malformed-response") from None
        finally:
            if sock is not None:
                sock.close()


class AuthenticatedKubernetesClient:
    """Supervisor creates an exact request catalogue; callers cannot pass URLs/bodies.

    A fake transport is explicit test injection, never configurable from CLI JSON.
    """

    def __init__(self, *, binding, transport, plan, monotonic=time.monotonic):
        from .capability_collector import catalogue
        from .capability_runner_contract import RunnerPlan

        self.binding = ClientBinding.model_validate_json(binding.model_dump_json())
        plan = RunnerPlan.model_validate_json(plan.model_dump_json())
        if isinstance(transport, HTTPSKubernetesTransport) and transport.binding != self.binding:
            raise CollectionError("contradictory")
        self.transport, self.clock = transport, monotonic
        self._requests = catalogue(plan, self.binding)
        self._count = 0
        self._deadline = self.clock() + binding.max_seconds
        self._audit_ids = set()

    def request(self, key, *, deadline):
        try:
            request = self._requests[key]
            self._count += 1
            end = min(deadline, self._deadline, self.clock() + self.binding.timeout_seconds)
            if self._count > self.binding.max_requests:
                raise CollectionError("unavailable")
            if end <= self.clock():
                raise CollectionError("timeout")
            reply = self.transport.send(
                request, deadline=end, limit=self.binding.max_response_bytes
            )
            if self.clock() >= end:
                raise CollectionError("timeout")
            if len(reply.body) > self.binding.max_response_bytes:
                raise CollectionError("malformed-response")
            if reply.status == 401:
                raise CollectionError("auth-denied")
            if reply.status == 404 or reply.status >= 500:
                raise CollectionError("unavailable")
            if reply.status not in {200, 201, 403, 422}:
                raise CollectionError("malformed-response")
            if (
                not re.fullmatch(r"[A-Za-z0-9-]{1,100}", reply.audit_id)
                or reply.audit_id in self._audit_ids
            ):
                raise CollectionError("stale")
            self._audit_ids.add(reply.audit_id)
            body = decode(reply.body)
            # Never retain credentials even if an upstream admission error echoes one.
            serialized = encoded(body).lower()
            if len(serialized) > self.binding.max_response_bytes:
                raise CollectionError("malformed-response")
            if any(
                word in serialized
                for word in (b"bearer ", b"-----begin", b'"token":', b'"client-key-data":')
            ):
                raise CollectionError("malformed-response")
            return {
                "status": reply.status,
                "body": body,
                "audit_id": reply.audit_id,
                "request": {
                    "method": request.method,
                    "path": request.path,
                    "body": decode(request.body) if request.body else {},
                },
                "identity": self.binding.identity,
            }
        except CollectionError:
            raise
        except Exception:
            raise CollectionError("malformed-response") from None
