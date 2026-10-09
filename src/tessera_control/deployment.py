"""Narrow configuration and probe state for one control-plane process."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sqlite3
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tessera_agent.coordinator import ProviderDisabledCoordinatorConsumer
from tessera_agent.facade import AgentCredential, AstraAgentFacade

from .migrations import LATEST_SCHEMA_VERSION
from .store import DATABASE_NAME

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "testserver")


@dataclass(frozen=True)
class DeploymentConfig:
    """Serving configuration deliberately separate from authority stored in SQLite."""

    trusted_hosts: tuple[str, ...]
    public_origin: str | None
    profile: str

    @classmethod
    def local(cls) -> DeploymentConfig:
        return cls(trusted_hosts=_LOCAL_HOSTS, public_origin=None, profile="local-loopback-v1")

    @classmethod
    def remote(cls, trusted_hosts: list[str], public_origin: str | None) -> DeploymentConfig:
        hosts = tuple(dict.fromkeys(host.lower() for host in trusted_hosts))
        if not hosts or any(not _valid_host(host) for host in hosts):
            raise ValueError("remote serving requires explicit concrete --trusted-host values")
        if public_origin is None:
            raise ValueError("remote serving requires an explicit HTTPS --public-origin")
        if any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in public_origin
        ):
            raise ValueError("--public-origin must not contain whitespace or control characters")
        parsed = urlsplit(public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or "?" in public_origin
            or "#" in public_origin
            or parsed.netloc.endswith(":")
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError(
                "--public-origin must be an HTTPS origin without credentials or a path"
            )
        origin_host = parsed.hostname.lower()
        if origin_host not in {_host_without_brackets(host) for host in hosts}:
            raise ValueError("--public-origin host must be listed in --trusted-host")
        return cls(
            trusted_hosts=hosts,
            public_origin=public_origin.rstrip("/"),
            profile="remote-lab-v1",
        )


def serving_config(
    bind_host: str, trusted_hosts: list[str] | None, public_origin: str | None
) -> DeploymentConfig:
    """Keep the established loopback invocation safe unless remote mode is explicit."""

    if bind_host in {"127.0.0.1", "::1"} and not trusted_hosts and public_origin is None:
        return DeploymentConfig.local()
    return DeploymentConfig.remote(trusted_hosts or [], public_origin)


def backend_status(state_dir: Path) -> dict[str, str | None]:
    """Read only the minimal durable state needed by an unauthenticated probe.

    A running process may encounter an on-disk schema rollback or a held restore
    after startup.  The API gate uses this same result before admitting requests.
    """

    database = state_dir / DATABASE_NAME
    try:
        state = state_dir.lstat()
        parent = state_dir.parent.lstat()
        if (
            not stat.S_ISDIR(state.st_mode)
            or stat.S_ISLNK(state.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
        ):
            raise ValueError("state directory is not safe")
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("state database is not a regular file")
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        try:
            schema = connection.execute(
                "SELECT schema_version FROM schema_meta WHERE singleton=1"
            ).fetchone()
            recovery = connection.execute(
                "SELECT state, incarnation FROM control_recovery WHERE singleton=1"
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return {"status": "unready", "reason": "state_unavailable", "backend_identity": None}
    if schema is None or schema[0] != LATEST_SCHEMA_VERSION:
        return {"status": "unready", "reason": "schema_mismatch", "backend_identity": None}
    if recovery is None or recovery[0] != "active":
        return {"status": "unready", "reason": "recovery_hold", "backend_identity": None}
    identity = hashlib.sha256(str(recovery[1]).encode("utf-8")).hexdigest()[:24]
    return {"status": "ready", "reason": None, "backend_identity": identity}


def _valid_host(host: str) -> bool:
    # Match Host allowlist syntax, not URLs, ports, wildcard or userinfo syntax.
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return False
        return "%" not in host
    if len(host) > 253:
        return False
    return all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is not None
        for label in host.split(".")
    )


def _host_without_brackets(host: str) -> str:
    return host.removeprefix("[").removesuffix("]")


"""Opt-in, provider-disabled coordinator process for a separately hosted control API.

Requires an existing project-scoped agent credential, a private TLS endpoint and
an operator-provisioned CA. Never creates authority or starts a provider.
"""


class Response:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> dict:
        return json.loads(self.body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # A redirect must never forward a scoped bearer to another endpoint.
        return None


class ControlClient:
    def __init__(self, origin: str, ca_file: Path, timeout: float = 5) -> None:
        parsed = urllib.parse.urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or origin.endswith(":")
        ):
            raise ValueError(
                "A concrete HTTPS control origin without path or credentials is required"
            )
        self.origin = origin
        self.context = ssl.create_default_context(cafile=str(ca_file))
        self.timeout = timeout
        # Do not allow ambient HTTP(S)_PROXY to redirect a bearer-bearing request.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self.context),
            _NoRedirect(),
        )

    def _request(
        self,
        method: str,
        path: str,
        headers: dict,
        body: dict | None = None,
        params: dict | None = None,
    ) -> Response:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Control path must be relative to the configured origin")
        url = self.origin + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                **headers,
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return Response(response.status, response.read(1_048_577))
        except urllib.error.HTTPError as error:
            return Response(error.code, error.read(1_048_577))

    def get(self, path: str, *, headers: dict, params: dict | None = None) -> Response:
        return self._request("GET", path, headers, params=params)

    def post(self, path: str, *, headers: dict, json: dict) -> Response:
        return self._request("POST", path, headers, json)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--max-cycles", type=int, default=0, help="0: run until stopped")
    args = parser.parse_args()
    if not 1 <= args.poll_seconds <= 60 or args.max_cycles < 0:
        parser.error("poll interval must be 1..60 and max cycles nonnegative")
    client = ControlClient(args.origin, args.ca_file)
    # Kubernetes projected Secret files are symlinks to read-only 0440 files.
    # This is a deliberate mounted-secret boundary, not an arbitrary host path.
    if args.credential_file.parent != Path("/run/secrets/tessera"):
        parser.error("credential must be mounted under /run/secrets/tessera")
    token = args.credential_file.read_bytes()[:4097]
    if len(token) > 4096 or not token or b"\n" in token.strip():
        parser.error("credential is missing or invalid")
    try:
        credential = token.decode("ascii").strip()
    except UnicodeDecodeError:
        parser.error("credential is invalid")
    facade = AstraAgentFacade(client, AgentCredential(token=credential, project_id=args.project_id))
    consumer = ProviderDisabledCoordinatorConsumer(client, credential, facade)
    for _ in range(args.max_cycles) if args.max_cycles else iter(int, 1):
        consumer.run_once()
        if args.max_cycles != 1:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
