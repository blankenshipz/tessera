"""Explicit opt-in service; no install, credential discovery or daemon-host fallback."""

import argparse
import json
import os
import stat
from pathlib import Path

from pydantic import Field

from tessera_worker.local_docker_contract import FixturePlan, LocalFixtureConfig
from tessera_worker.local_docker_engine import DockerCLI, LocalDockerEngine

from .channel import connect_tls, upload
from .docker import DockerAdapter
from .journal import MonitorJournal
from .protocol import Assignment, Denied, Digest, Envelope, Frozen, Name, open_envelope
from .runtime import Monitor


class TLSConfig(Frozen):
    address: str = Field(max_length=64)
    port: int = Field(ge=1, le=65535)
    server_name: str = Field(max_length=253)
    ca: str = Field(max_length=1024)
    certificate: str = Field(max_length=1024)
    private_key: str = Field(max_length=1024)
    peer_sha256: Digest


class Config(Frozen):
    enabled: bool = False
    identity: Name
    journal: str = Field(max_length=1024)
    assignment_file: str = Field(max_length=1024)
    authority_key_file: str = Field(max_length=1024)
    channel_key_file: str = Field(max_length=1024)
    evidence_key_file: str = Field(max_length=1024)
    allow_host_socket: bool = False
    fixture: FixturePlan
    tls: TLSConfig


def private_read(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise Denied("private runtime file required")
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise Denied("runtime file bound")
        return data
    finally:
        os.close(fd)


def adapter_for(config):
    engine = LocalDockerEngine(
        DockerCLI(LocalFixtureConfig(enabled=True), Path(config.journal) / "docker-config")
    )
    return DockerAdapter(engine, config.fixture, allow_host_socket=config.allow_host_socket)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--recover", action="store_true", help="historical stop-only; never restart")
    modes.add_argument("--reconcile", action="store_true", help="upload only; no Docker effects")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if not (args.run or args.recover or args.reconcile):
        print(json.dumps(dict(state="disabled", effects=0, activation="blocked")))
        return 0
    try:
        if args.config is None:
            raise Denied("explicit configuration required")
        config = Config.model_validate_json(private_read(args.config, 65536))
        if not config.enabled:
            raise Denied("explicit opt-in required")
        document = Envelope.model_validate_json(private_read(config.assignment_file, 65536))
        keys = {
            name: private_read(getattr(config, name + "_file"), 32)
            for name in ("authority_key", "channel_key", "evidence_key")
        }
        if any(len(k) != 32 for k in keys.values()) or len(set(keys.values())) != 3:
            raise Denied("distinct 32-byte runtime keys required")
        a = open_envelope(document, Assignment, keys["authority_key"], b"assignment")
        if a.monitor != config.identity:
            raise Denied("monitor identity mismatch")
        if args.run:
            from .daemon import run_guarded

            run_guarded(config, document, keys)
        else:
            journal = MonitorJournal(config.journal)
            try:
                if journal.assignment(keys["authority_key"]) != a:
                    raise Denied("historical assignment mismatch")
                if args.recover:
                    Monitor(
                        document,
                        identity=config.identity,
                        journal=journal,
                        adapter=adapter_for(config),
                        recover=True,
                        **keys,
                    )
                else:
                    channel = connect_tls(**config.tls.model_dump())
                    try:
                        upload(journal, channel, a, keys["channel_key"])
                    finally:
                        channel.close()
            finally:
                journal.close()
        print(json.dumps(dict(state="retained", activation="blocked")))
        return 0
    except Exception:
        print(
            json.dumps(
                dict(
                    state="unknown",
                    activation="blocked",
                    reason="monitor_prerequisite_or_evidence_unavailable",
                )
            )
        )
        return 1
