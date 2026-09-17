"""Narrow configuration and probe state for one control-plane process."""

from __future__ import annotations

import hashlib
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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
        parsed = urlsplit(public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
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
    return bool(host) and host != "*" and "*" not in host and "://" not in host and "/" not in host


def _host_without_brackets(host: str) -> str:
    return host.removeprefix("[").removesuffix("]")
