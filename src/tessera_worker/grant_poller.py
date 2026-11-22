"""Opt-in development poller: run each pending grant for one worker credential.

This is a supervisor-side loop around the one-shot ``DevelopmentWorkerService``.
It never creates authority: it only lists grants the control plane already issued
to this worker credential and drives each one through the existing service.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import scratch_limits as limits
from .control_channel import HttpControlChannel, classify_control_failure
from .cpu import CpuError, _docker
from .docker_network_pool import DEFAULT_PREFIX_LENGTH, DockerNetworkPool
from .service import (
    DevelopmentWorkerConfig,
    DevelopmentWorkerError,
    DevelopmentWorkerService,
    _check_container_user,
    network_pool_from_config,
    read_private_credential,
    safe_error_detail,
)
from .supervisor import SupervisorError

_GRANT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_MAX_CONSECUTIVE_FAILURES = 5
LIST_PATH = "/worker/execution-grants"


@dataclass(frozen=True)
class GrantPollerConfig:
    control_url: str
    credential_file: Path
    workspace_root: Path
    development_output_root: Path
    allowed_images: tuple[str, ...]
    egress_proxy_image: str | None = None
    poll_seconds: float = 2.0
    max_grants: int | None = None
    # Operator-owned container limits for every grant this poller runs (see DockerCpuAdapter).
    memory_bytes: int | None = None
    scratch_bytes: int | None = None
    # Operator-owned numeric "uid:gid" the stage containers run as (never from a plan or spec).
    # None: the poller's own uid:gid on native Linux, otherwise 65532:65532.
    container_user: str | None = None
    allow_root_container_user: bool = False
    # Operator-owned IPv4 block for the Docker networks Tessera creates (explicit
    # --subnet/--gateway), for hosts whose default address pools are exhausted. None keeps
    # Docker's default pools. The prefix length is the size of each per-run network.
    docker_network_pool: str | None = None
    docker_network_prefix_length: int = DEFAULT_PREFIX_LENGTH
    # Operator ceiling on one operation's output bytes (16 MiB .. 1 TiB); None keeps the
    # installation default. A plan asking for more is refused, never truncated.
    max_output_bytes: int | None = None
    # Operator ceiling on the scratch a plan may request (ADR 0059); None keeps the 64 MiB
    # default ceiling. Checked against the host's memory when above the historical bounds.
    max_scratch_bytes: int | None = None

    def __post_init__(self) -> None:
        if (
            not self.allowed_images
            or any(not isinstance(image, str) or not image for image in self.allowed_images)
            or self.poll_seconds <= 0
            or (self.max_grants is not None and self.max_grants < 1)
            or (
                self.memory_bytes is not None
                and (
                    type(self.memory_bytes) is not int
                    or not limits.MIN_MEMORY_BYTES <= self.memory_bytes <= limits.MAX_BYTES
                )
            )
            or (
                self.scratch_bytes is not None
                and (
                    type(self.scratch_bytes) is not int
                    or not limits.MIN_SCRATCH_BYTES <= self.scratch_bytes <= limits.MAX_BYTES
                )
            )
            or (
                self.max_scratch_bytes is not None
                and (
                    type(self.max_scratch_bytes) is not int
                    or not limits.MIN_SCRATCH_BYTES <= self.max_scratch_bytes <= limits.MAX_BYTES
                    or (self.scratch_bytes or 0) > self.max_scratch_bytes
                )
            )
            or (
                self.max_output_bytes is not None
                and (
                    type(self.max_output_bytes) is not int
                    or not 16 * 1024 * 1024 <= self.max_output_bytes <= 1024**4
                )
            )
        ):
            raise DevelopmentWorkerError("Grant poller configuration is invalid.")
        _check_container_user(self.container_user, self.allow_root_container_user)
        try:
            limits.check_host_capacity(self.memory_bytes, self.max_scratch_bytes)
        except ValueError as error:
            raise DevelopmentWorkerError(
                f"Grant poller configuration is invalid: {error}"
            ) from None
        network_pool_from_config(self.docker_network_pool, self.docker_network_prefix_length)


ServiceFactory = Callable[[DevelopmentWorkerConfig], Any]

CONFIG_SCHEMA = "tessera.worker.development-poll.v1"
_MAX_CONFIG_BYTES = 16384
_CONFIG_KEYS = {
    "schema",
    "control_url",
    "credential_file",
    "workspace_root",
    "development_output_root",
    "allowed_images",
    "egress_proxy_image",
    "poll_seconds",
    "max_grants",
    "memory_bytes",
    "scratch_bytes",
    "container_user",
    "allow_root_container_user",
    "docker_network_pool",
    "docker_network_prefix_length",
    "max_output_bytes",
    "max_scratch_bytes",
}


def load_poller_config(path: Path) -> GrantPollerConfig:
    """Read the poller config file (ADR 0038 loop contract).

    A closed JSON object holding paths and settings only: the bearer value stays in the
    ``credential_file`` it names, never in this file, argv, logs or health. Any problem
    raises ``DevelopmentWorkerError`` without echoing file content (exit 2 in the CLI).
    """
    refused = DevelopmentWorkerError("Grant poller config is unavailable or invalid.")
    path = Path(path)
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise refused
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise refused
        document = json.loads(raw)
    except (OSError, ValueError) as error:
        raise refused from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != CONFIG_SCHEMA
        or not set(document) <= _CONFIG_KEYS
    ):
        raise refused
    images = document.get("allowed_images")
    try:
        return GrantPollerConfig(
            control_url=str(document["control_url"]),
            credential_file=Path(document["credential_file"]),
            workspace_root=Path(document["workspace_root"]),
            development_output_root=Path(document["development_output_root"]),
            allowed_images=tuple(images) if isinstance(images, list) else (),
            egress_proxy_image=document.get("egress_proxy_image"),
            poll_seconds=float(document.get("poll_seconds", 2.0)),
            max_grants=document.get("max_grants"),
            memory_bytes=document.get("memory_bytes"),
            scratch_bytes=document.get("scratch_bytes"),
            container_user=document.get("container_user"),
            allow_root_container_user=document.get("allow_root_container_user", False),
            docker_network_pool=document.get("docker_network_pool"),
            docker_network_prefix_length=document.get(
                "docker_network_prefix_length", DEFAULT_PREFIX_LENGTH
            ),
            max_output_bytes=document.get("max_output_bytes"),
            max_scratch_bytes=document.get("max_scratch_bytes"),
        )
    except (KeyError, TypeError, ValueError, DevelopmentWorkerError) as error:
        raise refused from error


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise DevelopmentWorkerError("Grant poller directory is not private.")
    return path


class DevelopmentGrantPoller:
    def __init__(
        self,
        config: GrantPollerConfig,
        *,
        channel: Any | None = None,
        service_factory: ServiceFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        prune_networks: Callable[[DockerNetworkPool], list[str]] | None = None,
    ) -> None:
        self.config = config
        self.root = _private_dir(Path(config.workspace_root))
        self.output_root = _private_dir(Path(config.development_output_root))
        self.channel = channel or HttpControlChannel(
            config.control_url, read_private_credential(config.credential_file)
        )
        self.service_factory = service_factory or self._default_service
        self.sleep = sleep
        self.clock = clock
        # grant id -> clock time before which its undelivered observation is not retried
        # (ADR 0054). In memory only: the durable backoff lives in the runtime's outbox, so a
        # restarted poller starts here empty and the outbox still refuses an early resend.
        self._retry_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self.grants_run = 0
        self.errors = 0
        self.pruned_networks: list[str] = []
        self._network_pool = network_pool_from_config(
            config.docker_network_pool, config.docker_network_prefix_length
        )
        self._prune_networks = prune_networks
        self._pruned = self._network_pool is None

    def _default_service(self, config: DevelopmentWorkerConfig) -> Any:
        return DevelopmentWorkerService(config, channel=self.channel)

    def _grant_dir(self, grant_id: str) -> Path:
        if not _GRANT_ID.fullmatch(grant_id) or grant_id in {".", ".."}:
            raise DevelopmentWorkerError("Grant identifier is not a safe path component.")
        return self.root / "grants" / grant_id

    def _run_one(self, grant_id: str, image: str) -> dict[str, Any] | None:
        directory = self._grant_dir(grant_id)
        _private_dir(directory)
        workspace = _private_dir(directory / "workspace")
        outputs = _private_dir(self.output_root / "grants" / grant_id)
        service = self.service_factory(
            DevelopmentWorkerConfig(
                control_url=self.config.control_url,
                grant_id=grant_id,
                credential_file=self.config.credential_file,
                workspace_root=workspace,
                development_output_root=outputs,
                allowed_image=image,
                egress_proxy_image=self.config.egress_proxy_image,
                memory_bytes=self.config.memory_bytes,
                scratch_bytes=self.config.scratch_bytes,
                container_user=self.config.container_user,
                allow_root_container_user=self.config.allow_root_container_user,
                docker_network_pool=self.config.docker_network_pool,
                docker_network_prefix_length=self.config.docker_network_prefix_length,
                max_output_bytes=self.config.max_output_bytes,
                max_scratch_bytes=self.config.max_scratch_bytes,
                poll_seconds=self.config.poll_seconds,
            )
        )
        try:
            outcome = service.run()
        except (DevelopmentWorkerError, SupervisorError, OSError, ConnectionError) as error:
            # No result is recorded: a grant directory with a pinned-image marker is
            # resumed next cycle (the listing excludes a grant once it has a lease, so
            # the poller must remember its own).  The service replays its idempotent
            # lease/package requests or reconciles a persisted runtime descriptor; it
            # never re-leases.  A control outage (no HTTP status, 5xx, 408/425/429) is
            # not a verdict on the grant and does not count toward the give-up bound.
            transient = isinstance(error, ConnectionError) and (
                classify_control_failure(error) == "transient"
            )
            if not transient:
                self._failures[grant_id] = self._failures.get(grant_id, 0) + 1
            return {"state": "error", "detail": safe_error_detail(error, self.root)}
        if outcome.get("delivery_pending") is True:
            # An undelivered observation is not a verdict: record no result, so the grant is
            # recovered after the backoff and the same observation is delivered.
            self._retry_at[grant_id] = self.clock() + float(outcome["retry_after_seconds"])
            return outcome
        self._retry_at.pop(grant_id, None)
        self._write_result(directory, outcome)
        self._failures.pop(grant_id, None)
        return outcome

    @staticmethod
    def _write_result(directory: Path, outcome: dict[str, Any]) -> None:
        temporary = directory / "result.json.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as target:
            json.dump(outcome, target, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, directory / "result.json")

    def _recoverable(self) -> list[str]:
        base = self.root / "grants"
        if not base.is_dir():
            return []
        found = []
        for entry in sorted(base.iterdir()):
            if (
                _GRANT_ID.fullmatch(entry.name)
                and entry.is_dir()
                and not entry.is_symlink()
                and not (entry / "result.json").exists()
                and self._retry_at.get(entry.name, 0.0) <= self.clock()
                and (
                    # An admitted operation is always reconciled; a leased grant that
                    # never reached its descriptor (control outage) is resumed until
                    # control gives a permanent refusal enough times.
                    (entry / "workspace" / "runtime" / "runtime.json").exists()
                    or (
                        (entry / "image").is_file()
                        and self._failures.get(entry.name, 0) < _MAX_CONSECUTIVE_FAILURES
                    )
                )
            ):
                found.append(entry.name)
        return found

    @staticmethod
    def _remember_image(directory: Path, image: str) -> None:
        descriptor = os.open(directory / "image", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as target:
            target.write(image)

    def _images(self, grant_id: str) -> str | None:
        """The pinned image persisted for a recovery run (written at first start)."""
        try:
            return (self._grant_dir(grant_id) / "image").read_text().strip() or None
        except OSError:
            return None

    def run(self, max_cycles: int | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {"cycles": 0, "grants": [], "errors": 0}
        while max_cycles is None or report["cycles"] < max_cycles:
            report["cycles"] += 1
            self._cycle(report)
            if (
                self.config.max_grants is not None
                and len(report["grants"]) >= self.config.max_grants
            ):
                break
            if max_cycles is not None and report["cycles"] >= max_cycles:
                break
            self.sleep(self.config.poll_seconds)
        return report

    def step(self) -> list[dict[str, Any]]:
        """One poll cycle for the shared service loop: bounded log entries, no history.

        Counters (``grants_run``, ``errors``) are the only state kept across cycles.
        """
        self._prune_orphan_networks()
        report: dict[str, Any] = {"cycles": 0, "grants": [], "errors": 0}
        report["cycles"] += 1
        self._cycle(report)
        self.grants_run += len(report["grants"])
        self.errors += report["errors"]
        return [
            {"service": "development-poll", "grant_id": item["grant_id"], "grant_run": True}
            for item in report["grants"]
        ]

    def _prune_orphan_networks(self) -> None:
        """Startup reconciliation: remove Tessera-labelled orphan networks, once.

        Only with a configured pool; other software's networks are never considered.
        """
        if self._pruned or self._network_pool is None:
            return
        self._pruned = True
        try:
            prune = self._prune_networks or (lambda pool: pool.prune_orphans(_docker))
            self.pruned_networks = prune(self._network_pool)
        except CpuError:
            pass  # Best effort; the next poller start retries.

    def done(self) -> bool:
        """True once ``max_grants`` grants have been run."""
        limit = self.config.max_grants
        return limit is not None and self.grants_run >= limit

    def _record(
        self, report: dict[str, Any], grant_id: str, outcome: dict[str, Any] | None
    ) -> None:
        if outcome is not None and outcome.get("delivery_pending") is True:
            report.setdefault("pending", []).append(grant_id)  # neither a verdict nor an error
        elif outcome is not None and outcome.get("state") == "error":
            report["errors"] += 1
        else:
            report["grants"].append({"grant_id": grant_id, "outcome": outcome})

    def _room(self, report: dict[str, Any]) -> bool:
        limit = self.config.max_grants
        return limit is None or self.grants_run + len(report["grants"]) < limit

    def _cycle(self, report: dict[str, Any]) -> None:
        attempted: set[str] = set()
        for grant_id in self._recoverable():
            if not self._room(report):
                return
            attempted.add(grant_id)
            image = self._images(grant_id)
            if image is None or image not in self.config.allowed_images:
                continue
            self._record(report, grant_id, self._run_one(grant_id, image))
        try:
            listing = self.channel.get(LIST_PATH)
        except ConnectionError:
            report["errors"] += 1
            return
        grants = listing.get("grants") if isinstance(listing, dict) else None
        for grant in grants if isinstance(grants, list) else []:
            if not self._room(report):
                return
            grant_id, image = grant.get("id"), grant.get("pinned_image")
            if (
                not isinstance(grant_id, str)
                or grant_id in attempted
                or not _GRANT_ID.fullmatch(grant_id)
                or image not in self.config.allowed_images
                or (self.root / "grants" / grant_id / "result.json").exists()
                or self._retry_at.get(grant_id, 0.0) > self.clock()
                or self._failures.get(grant_id, 0) >= _MAX_CONSECUTIVE_FAILURES
            ):
                continue
            directory = _private_dir(self._grant_dir(grant_id))
            self._remember_image(directory, image)
            self._record(report, grant_id, self._run_one(grant_id, image))
