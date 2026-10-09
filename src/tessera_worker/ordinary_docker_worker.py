"""Ordinary experiment-worker CLI with fixed authenticated native HTTP binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .ordinary_docker_contract import DockerTarget
from .ordinary_docker_service import DockerServiceError, DockerWorkerConfig


def _credential(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise DockerServiceError("private owned credential reference required")
        value = os.read(fd, 4097)
        if len(value) > 4096:
            raise DockerServiceError("bounded credential required")
        text = value.decode("ascii").strip()
        for prefix in ("Authorization: Bearer ", "Bearer "):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        if not text or any(ord(c) <= 32 or ord(c) >= 127 for c in text):
            raise DockerServiceError("invalid credential reference")
        return text
    finally:
        os.close(fd)


def run_worker(config, args):
    from urllib.request import HTTPSHandler, ProxyHandler, build_opener

    from .control_channel import _RejectRedirects
    from .ordinary_docker_backend import HttpDockerWorkerBackend
    from .ordinary_docker_execution import OrdinaryDockerExecutionAdapter
    from .ordinary_docker_http import NativeDockerHttpClient
    from .ordinary_docker_service import OrdinaryDockerWorkerService
    from .ordinary_docker_transport import FixedDockerTransport

    tls = ssl.create_default_context(cafile=str(config.control_ca_file))
    opener = build_opener(ProxyHandler({}), _RejectRedirects(), HTTPSHandler(context=tls))
    client = NativeDockerHttpClient(
        config.control_url,
        _credential(config.credential_file),
        opener=lambda req, timeout: opener.open(req, timeout=timeout),
    )
    transport = FixedDockerTransport(
        args.docker_endpoint,
        tls=(
            ssl.create_default_context(cafile=args.docker_ca_file)
            if args.docker_endpoint.startswith("https:")
            else None
        ),
    )
    service = OrdinaryDockerWorkerService(
        config,
        None,
        adapter_factory=lambda claim, path, authority: OrdinaryDockerExecutionAdapter(
            path, config.target, transport, current_authority=authority
        ),
    )
    try:
        service.backend = HttpDockerWorkerBackend(
            client, config.workspace / "ordinary-docker-http.sqlite3", config.worker_principal_id
        )
        result = service.run(install_signal_handlers=True)
        return {"status": result.status, "termination_confirmed": result.termination_confirmed}, (
            0 if result.status == "stopped" and result.termination_confirmed else 2
        )
    finally:
        service.close()


def request_stop(workspace: Path):
    """Local operator stop intent, not a claim of container termination."""
    if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
        raise DockerServiceError("existing private worker workspace required")
    info = workspace.stat()
    path = workspace / "ordinary-docker-service.sqlite3"
    if (
        info.st_mode & 0o077
        or info.st_uid != os.getuid()
        or path.is_symlink()
        or not path.is_file()
    ):
        raise DockerServiceError("existing private worker journal required")
    info = path.stat()
    if info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise DockerServiceError("private worker journal required")
    with sqlite3.connect(path, timeout=2) as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('stop_requested','true')")
    return {"status": "stop_requested", "termination_confirmed": False}


def _configuration(args):
    endpoint = urlsplit(args.docker_endpoint)
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise DockerServiceError("fixed Docker endpoint required")
    if endpoint.scheme == "unix":
        if endpoint.netloc or not endpoint.path.startswith("/"):
            raise DockerServiceError("absolute fixed socket reference required")
    elif endpoint.scheme == "https":
        if not endpoint.hostname or endpoint.path not in {"", "/"}:
            raise DockerServiceError("fixed Docker TLS endpoint required")
    else:
        raise DockerServiceError("plaintext/ambient Docker endpoint refused")
    target = DockerTarget(
        daemon_id=args.docker_daemon_id,
        image=args.image,
        endpoint_digest=hashlib.sha256(args.docker_endpoint.encode()).hexdigest(),
    )
    return DockerWorkerConfig(
        args.worker_principal_id,
        args.control_url,
        Path(args.credential_file),
        Path(args.control_ca_file),
        Path(args.workspace),
        target,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "run"):
        command = subcommands.add_parser(action)
        for name in (
            "workspace",
            "worker-principal-id",
            "control-url",
            "credential-file",
            "control-ca-file",
            "docker-endpoint",
            "docker-daemon-id",
            "image",
        ):
            command.add_argument("--" + name, required=True)
        command.add_argument("--docker-ca-file")
    command = subcommands.add_parser("request-stop")
    command.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "request-stop":
            result, code = request_stop(args.workspace), 0
        else:
            config = _configuration(args)  # check opens no auth files.
            if args.action == "check":
                result, code = (
                    {
                        "status": "configuration_shape_valid",
                        "execution_started": False,
                        "http_binding": "native_schema54",
                    },
                    0,
                )
            else:
                if args.docker_endpoint.startswith("https:") and not args.docker_ca_file:
                    raise DockerServiceError("verified Docker CA reference required")
                result, code = run_worker(config, args)
    except (OSError, ValueError, sqlite3.Error):
        result, code = (
            {
                "status": "blocked",
                "reason": "worker_configuration_or_journal_invalid",
                "execution_started": False,
            },
            2,
        )
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
