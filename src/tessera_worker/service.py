"""Explicit, one-shot development worker runner.

This is deliberately a supervisor-side command, not a scheduler.  It consumes
one already-issued grant and one already-registered worker credential; it never
creates human authority or interprets an exit as a research result.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from tessera_control.image_build_models import RegisteredRepository

from . import scratch_limits
from .control_channel import (
    DEFAULT_TIMEOUT_SECONDS,
    ControlChannelError,
    HttpControlChannel,
    classify_control_failure,
    validate_operator_timeout,
)
from .cpu import (
    DEVELOPMENT_OUTPUT_PROFILE,
    DockerCpuAdapter,
    default_container_user,
    validate_container_user,
)
from .development_runtime import DevelopmentCpuRuntime, DevelopmentOutputProfile
from .docker_network_pool import DEFAULT_PREFIX_LENGTH, DockerNetworkPool
from .outputs import OutputCollectionError, worker_output_limit
from .private_auth_file import AuthFileRefused, check_private_auth_file
from .provider import Provider
from .public_fetch import PublicFetchError, parse_proxy
from .runtime import (
    AUTHORITY_MAX_AGE_SECONDS,
    LEASE_SAFETY_MARGIN_SECONDS,
    MAX_DESCRIPTOR_BYTES,
    ObservationDeliveryPending,
    ObservationRefused,
    is_transient_control_failure,
    lease_request_seconds,
)
from .supervisor import DriverResult, SupervisorError

_ABSOLUTE_PATH = re.compile(r"(?<![\w.])/[^\s'\"<>:;,)\]]+")
_DETAIL_LIMIT = 200
RUN_RECEIPT_ATTEMPTS = 3
_RUN_RECEIPT_RETRY_SECONDS = 1.0
# A renewal (heartbeat plus lease renewal) that fails transiently while the lease has time left
# is retried after this backoff (doubling, capped), until the lease's own expiry less
# LEASE_SAFETY_MARGIN_SECONDS. Only a refusal or that deadline stops the stage.
_RENEWAL_RETRY_BASE_SECONDS = 1.0
_RENEWAL_RETRY_CAP_SECONDS = 4.0
# Each renewal request (the heartbeat and the renewal separately) may use at most this share of
# the lease time left before the deadline, so this many attempts fit in it and one hung request
# never uses up the whole window (ADR 0178). Never below the minimum request time.
_RENEWAL_ATTEMPTS_PER_WINDOW = 3


@dataclass
class _RenewalRetries:
    """What the worker records about renewals that did not get through (grant result)."""

    failed: int = 0  # every failed renewal attempt in this run
    consecutive: int = 0  # failed attempts since the last confirmed renewal
    recovered: int = 0  # stalls that a later retry got through
    last_failure: str | None = None
    retry_at: float | None = None

    def summary(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "failed_attempts": self.failed,
            "recovered_stalls": self.recovered,
            "unconfirmed_attempts": self.consecutive,
        }
        if self.last_failure is not None:
            record["last_failure"] = self.last_failure
        return record


def safe_error_detail(error: BaseException, *private_paths: Path | str) -> str:
    """Bounded operator diagnostic: class name plus message, never host paths."""
    text = str(error)
    for private in private_paths:
        if str(private) not in {"", "/"}:
            text = text.replace(str(private), "<path>")
    text = _ABSOLUTE_PATH.sub("<path>", text)
    text = " ".join(text.split())
    detail = f"{type(error).__name__}: {text}" if text else type(error).__name__
    return detail[:_DETAIL_LIMIT]


class _CredentialWatch:
    """Channel proxy that remembers the first credential rejection, then re-raises.

    The runtime and supervisor deliberately swallow some control failures (for
    example a package refresh) and cancel instead.  Remembering the rejection here
    lets the service mark the final result without changing that fail-closed path.
    """

    def __init__(self, channel: Any) -> None:
        self._channel = channel
        self.rejected_status: int | None = None

    @property
    def max_request_bytes(self) -> int | None:
        """The wrapped channel's request bound: large-artifact uploads size their parts to it."""
        return getattr(self._channel, "max_request_bytes", None)

    @property
    def timeout_seconds(self) -> float | None:
        """The wrapped channel's request timeout, when it can cap a single call below it."""
        if not _accepts_timeout(self._channel):
            return None
        value = getattr(self._channel, "timeout_seconds", None)
        return value if type(value) in {int, float} else None

    def post(self, path: str, body: Any, key: str, *, timeout_seconds: float | None = None) -> Any:
        try:
            if timeout_seconds is None:
                return self._channel.post(path, body, key)
            return self._channel.post(path, body, key, timeout_seconds=timeout_seconds)
        except ControlChannelError as error:
            if self.rejected_status is None and classify_control_failure(error) == "auth":
                self.rejected_status = error.status
            raise


def _accepts_timeout(channel: Any) -> bool:
    try:
        return "timeout_seconds" in inspect.signature(channel.post).parameters
    except (AttributeError, TypeError, ValueError):
        return False


class DevelopmentWorkerError(SupervisorError):
    """The opt-in development worker cannot safely advance its one operation."""


def _check_container_user(user: object, allow_root: object) -> None:
    """Shared config validation for the operator-owned container user."""
    if type(allow_root) is not bool:
        raise DevelopmentWorkerError("Container user override must be a boolean.")
    if user is None:
        return
    try:
        validate_container_user(user, allow_root=allow_root)
    except ValueError as error:
        raise DevelopmentWorkerError("Container user configuration is invalid.") from error


def network_pool_from_config(cidr: Any, prefix_length: Any) -> DockerNetworkPool | None:
    """The operator's Docker network pool, or None when unset; invalid values are refused."""
    if cidr is None:
        return None
    try:
        return DockerNetworkPool(cidr, prefix_length)
    except ValueError as error:
        raise DevelopmentWorkerError("Docker network pool configuration is invalid.") from error


@dataclass(frozen=True)
class DevelopmentWorkerConfig:
    """Local-only configuration; credentials are always read from a private file."""

    control_url: str
    grant_id: str
    credential_file: Path
    workspace_root: Path
    development_output_root: Path
    allowed_image: str
    egress_proxy_image: str | None = None
    memory_bytes: int | None = None
    scratch_bytes: int | None = None
    max_scratch_bytes: int | None = None
    container_user: str | None = None
    allow_root_container_user: bool = False
    # HTTP timeout for every control request (seconds, 1 to 120; default 15). A slow answer is a
    # transient failure that is retried, never a reason to stop a healthy run (ADR 0054).
    control_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # Operator-owned IPv4 block (and per-network prefix length) for the Docker networks
    # Tessera creates; None keeps Docker's default address pools.
    docker_network_pool: str | None = None
    docker_network_prefix_length: int = DEFAULT_PREFIX_LENGTH
    # Disk reservation capacity; None means the worker output ceiling below.
    output_capacity_bytes: int | None = None
    # Operator ceiling on one operation's output bytes (16 MiB .. 1 TiB); None keeps the
    # installation default (tessera_control.artifact_limits).
    max_output_bytes: int | None = None
    # ADR 0097: private supervisor custody for fetched public inputs (shared by the adapter, which
    # mounts them read-only, and the runtime, which fetches), and the optional download cache.
    # None: a package carrying public_fetch is refused. Operator-owned; never from a plan.
    public_root: Path | None = None
    public_cache_dir: Path | None = None
    # ADR 0123: an image build may push only to ``build_repositories`` (``host[/path]``), using
    # the operator's registry auth file, which is mounted read-only into a pushing build and
    # never read here. Both unset (the default) refuses every pushing build; setting one
    # without the other is a configuration error. Never from a plan or control.
    registry_auth_file: Path | None = None
    build_repositories: tuple[str, ...] = ()
    # Optional explicit proxy (http://host:port) for that fetch; used only when its host is on the
    # stage's egress allowlist, else the fetch fails closed. None: direct, environment ignored.
    public_fetch_proxy: str | None = None
    output_capacity_inodes: int = 256
    poll_seconds: float = 1.0
    max_ticks: int = 90_000
    renew_every_ticks: int = 15
    # Control leases last 30 s (tessera_control.execution.LEASE_SECONDS) and only a renewal
    # extends them. Renew on elapsed time as well as tick count, so a longer poll interval or
    # slow control round trips can never let a running operation's lease lapse.
    renew_after_seconds: float = 10.0
    # How long a running operation's tick reuses its last authority read (the lease's package)
    # before posting again; every renewal forces a fresh read. 0 re-reads on every tick (the
    # old behaviour, two control writes per tick). At most 20 s, inside the 30 s lease.
    authority_reread_seconds: float = AUTHORITY_MAX_AGE_SECONDS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.grant_id, str)
            or not self.grant_id
            or len(self.grant_id) > 200
            or self.poll_seconds <= 0
            or self.max_ticks < 0
            or self.renew_every_ticks < 1
            or not 0 < self.renew_after_seconds <= 20
            or type(self.authority_reread_seconds) not in {int, float}
            or not 0 <= self.authority_reread_seconds <= 20
            or (self.output_capacity_bytes is not None and self.output_capacity_bytes < 1)
            or (
                self.max_output_bytes is not None
                and (
                    type(self.max_output_bytes) is not int
                    or not 16 * 1024 * 1024 <= self.max_output_bytes <= 1024**4
                )
            )
            or self.output_capacity_inodes < 1
        ):
            raise DevelopmentWorkerError("Development worker configuration is invalid.")
        _check_container_user(self.container_user, self.allow_root_container_user)
        try:
            validate_operator_timeout(self.control_timeout_seconds, self.renew_after_seconds)
        except ValueError as error:
            raise DevelopmentWorkerError("Control timeout configuration is invalid.") from error
        network_pool_from_config(self.docker_network_pool, self.docker_network_prefix_length)
        check_build_registry_shape(self.registry_auth_file, self.build_repositories)


_REPOSITORY_ADAPTER = TypeAdapter(RegisteredRepository)
MAX_BUILD_REPOSITORIES = 32


def check_build_registry_shape(auth_file: Any, repositories: Any) -> None:
    """Refuse an inconsistent or malformed registry setting (no filesystem access)."""
    invalid = DevelopmentWorkerError("Image build registry configuration is invalid.")
    if (auth_file is None) != (not repositories):
        raise invalid  # an auth file needs registered repositories, and the reverse
    if auth_file is None:
        return
    if (
        not isinstance(auth_file, (str, Path))
        or not Path(auth_file).is_absolute()
        or not isinstance(repositories, (tuple, list))
        or len(repositories) > MAX_BUILD_REPOSITORIES
        or len(set(repositories)) != len(repositories)
    ):
        raise invalid
    try:
        for repository in repositories:
            _REPOSITORY_ADAPTER.validate_python(repository, strict=True)
    except ValueError as error:
        raise invalid from error


def private_registry_auth_file(path: Path) -> Path:
    """The operator's registry auth file, checked private without being read.

    A regular file (no symlink) owned by the worker user with no group or world access; the
    mode is never repaired silently. Its content is never read, logged or reported here."""
    try:
        return check_private_auth_file(path)
    except AuthFileRefused as error:
        raise DevelopmentWorkerError(
            "Registry auth file must be a private regular file (no symlink, no group or world "
            f"access) owned by the worker user ({error.code})."
        ) from error


def read_private_credential(path: Path) -> str:
    """Read a bounded bearer value without following links or disclosing it."""

    path = Path(path)
    try:
        info = path.lstat()
        private = (
            not stat.S_ISLNK(info.st_mode)
            and stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
        )
        if not private:
            raise DevelopmentWorkerError("Worker credential file is unavailable or not private.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            encoded = source.read(4097)
    except OSError as error:
        raise DevelopmentWorkerError(
            "Worker credential file is unavailable or not private."
        ) from error
    if len(encoded) > 4096:
        raise DevelopmentWorkerError("Worker credential file exceeds its bound.")
    try:
        token = encoded.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise DevelopmentWorkerError("Worker credential file is invalid.") from error
    if not token or any(character.isspace() for character in token):
        raise DevelopmentWorkerError("Worker credential file is invalid.")
    return token


def _private_directory(path: Path) -> Path:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise DevelopmentWorkerError("Development worker directory is unavailable.") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DevelopmentWorkerError("Development worker directory must be private 0700 custody.")
    return path.resolve(strict=True)


def operator_private_directory(path: Path) -> Path:
    """Create (0700) or validate an operator directory for public-fetch custody (ADR 0097).

    Refused when it is a symlink, not a directory, owned by someone else, or accessible to
    group or world; the mode is never repaired silently."""
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as error:
        raise DevelopmentWorkerError("Public fetch directory is unavailable.") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DevelopmentWorkerError(
            "Public fetch directory must be a private 0700 directory (no symlink, no group or "
            "world access) owned by the worker user."
        )
    return path.resolve(strict=True)


class RenewalIntentJournal:
    """Persist one renewal key before its request, so a lost reply is replayed."""

    def __init__(self, root: Path) -> None:
        self.path = root / "renewals.sqlite3"
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS renewal_intents (
                lease_id TEXT NOT NULL, epoch INTEGER NOT NULL, sequence INTEGER NOT NULL,
                key TEXT NOT NULL,
                completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
                PRIMARY KEY (lease_id, epoch, sequence))"""
            )
        os.chmod(self.path, 0o600)

    def key(self, lease_id: str, epoch: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT key FROM renewal_intents WHERE lease_id=? AND epoch=? AND completed=0
                ORDER BY sequence DESC LIMIT 1""",
                (lease_id, epoch),
            ).fetchone()
            if row is not None:
                return row[0]
            sequence = connection.execute(
                "SELECT coalesce(max(sequence), 0) + 1 FROM renewal_intents "
                "WHERE lease_id=? AND epoch=?",
                (lease_id, epoch),
            ).fetchone()[0]
            key = (
                "development-renew-"
                + hashlib.sha256(f"v1:{lease_id}:{epoch}:{sequence}".encode()).hexdigest()
            )
            connection.execute(
                """INSERT INTO renewal_intents (lease_id, epoch, sequence, key, completed)
                VALUES (?, ?, ?, ?, 0)""",
                (lease_id, epoch, sequence, key),
            )
            return key

    def complete(self, lease_id: str, epoch: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE renewal_intents SET completed=1
                WHERE lease_id=? AND epoch=? AND completed=0""",
                (lease_id, epoch),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        return connection


class HeartbeatIntentJournal:
    """Persist heartbeat keys so retries are safe and successful beats are distinct.

    Keys are unique to this journal, not only to its sequence: the grant poller gives each
    grant a fresh workspace, and keys derived from the sequence alone repeated across grants
    under one worker credential, so control answered them as idempotent replays and the
    worker's liveness was never refreshed (new leases were then refused as disconnected).
    """

    def __init__(self, root: Path) -> None:
        self.path = root / "heartbeats.sqlite3"
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS heartbeat_intents (
                sequence INTEGER PRIMARY KEY, key TEXT NOT NULL,
                completed INTEGER NOT NULL CHECK (completed IN (0, 1)))"""
            )
            # The body of a pending beat is kept with its key, so a replay after a restart
            # sends exactly what the first try did (the memory figures change between beats).
            if "body" not in {
                row[1] for row in connection.execute("PRAGMA table_info(heartbeat_intents)")
            }:
                connection.execute("ALTER TABLE heartbeat_intents ADD COLUMN body TEXT")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS heartbeat_journal (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1), journal_id TEXT NOT NULL)"""
            )
            # Created once per journal file and kept, so a restart replays a pending key.
            connection.execute(
                "INSERT OR IGNORE INTO heartbeat_journal (singleton, journal_id) VALUES (1, ?)",
                (secrets.token_hex(16),),
            )
            self.journal_id = connection.execute(
                "SELECT journal_id FROM heartbeat_journal WHERE singleton = 1"
            ).fetchone()[0]
        os.chmod(self.path, 0o600)

    def key(self) -> str:
        return self.intent({})[0]

    def intent(self, body: dict[str, int]) -> tuple[str, dict[str, int]]:
        """The pending beat's key and body; a new beat records ``body`` with its key."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT key, body FROM heartbeat_intents WHERE completed=0 "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                return row[0], json.loads(row[1] or "{}")
            sequence = connection.execute(
                "SELECT coalesce(max(sequence), 0) + 1 FROM heartbeat_intents"
            ).fetchone()[0]
            key = (
                "development-heartbeat-"
                + hashlib.sha256(f"v2:{self.journal_id}:{sequence}".encode()).hexdigest()
            )
            connection.execute(
                "INSERT INTO heartbeat_intents (sequence, key, completed, body) "
                "VALUES (?, ?, 0, ?)",
                (sequence, key, json.dumps(body, sort_keys=True)),
            )
            return key, body

    def complete(self) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE heartbeat_intents SET completed=1 WHERE completed=0")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)


class ReceiptRefusalJournal:
    """Durable record that control permanently refused an operation's run receipt.

    A restart consults it before any receipt post, so a refusal is never replayed.
    """

    def __init__(self, root: Path) -> None:
        self.path = root / "receipt-refusals.sqlite3"
        try:
            with self._connect() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS receipt_refusals (
                    operation_id TEXT PRIMARY KEY, http_status INTEGER NOT NULL,
                    detail TEXT NOT NULL)"""
                )
        except sqlite3.Error:
            # An unreadable journal fails closed when it is read (detail()).
            pass
        else:
            os.chmod(self.path, 0o600)

    def record(self, operation_id: str, http_status: int, detail: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO receipt_refusals (operation_id, http_status, detail) "
                "VALUES (?, ?, ?)",
                (operation_id, http_status, detail[:_DETAIL_LIMIT]),
            )

    def detail(self, operation_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT detail FROM receipt_refusals WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return None if row is None else row[0]

    @contextlib.contextmanager
    def _connect(self):  # noqa: ANN202
        """Yield a connection inside a transaction; always closed afterwards."""
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            yield connection


class DevelopmentWorkerService:
    """Run exactly one existing grant with development-only output custody."""

    def __init__(
        self,
        config: DevelopmentWorkerConfig,
        *,
        channel: Any | None = None,
        adapter: Any | None = None,
        provider: Provider | None = None,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
        stage_watch: Any | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        # Wall-clock time, compared with the lease's own expiry (injectable for tests).
        self.wall_clock: Any = lambda: datetime.now(UTC)
        self._renewal_retries = _RenewalRetries()
        self.root = _private_directory(config.workspace_root)
        # ADR 0125: on by default. None means the default marker ``<workspace>/stage-watch.kill``
        # (never created); an explicit StageWatchConfig wins, a disabled one never imports the
        # stage watcher.
        from .stage_watch_config import resolve_stage_watch

        self.stage_watch = resolve_stage_watch(stage_watch, self.root)
        self.output_root = _private_directory(config.development_output_root)
        self.public_root = (
            operator_private_directory(config.public_root)
            if config.public_root is not None
            else None
        )
        self.public_cache_dir = (
            operator_private_directory(config.public_cache_dir)
            if config.public_cache_dir is not None
            else None
        )
        self.registry_auth_file = (
            private_registry_auth_file(config.registry_auth_file)
            if config.registry_auth_file is not None
            else None
        )
        if config.public_fetch_proxy is not None:
            try:
                parse_proxy(config.public_fetch_proxy)  # syntax now; the host check is per stage
            except PublicFetchError as error:  # code only; the value is never echoed
                raise DevelopmentWorkerError(
                    f"Development worker configuration is invalid: public_fetch_proxy {error.code}."
                ) from None
        self.token = read_private_credential(config.credential_file)
        self.channel = channel or HttpControlChannel(
            config.control_url, self.token, timeout_seconds=config.control_timeout_seconds
        )
        self._watch = _CredentialWatch(self.channel)
        self.adapter = adapter
        self.provider = provider
        self.sleep = sleep
        self.renewals = RenewalIntentJournal(self.root)
        self.heartbeats = HeartbeatIntentJournal(self.root)
        self._memory_unsupported = False  # an older control plane refused the memory fields
        self.receipt_refusals = ReceiptRefusalJournal(self.root)

    def run(self) -> dict[str, Any]:
        """Heartbeat, lease, execute, and quarantine only a single grant attempt.

        A control credential rejection (HTTP 401, or an unclassified 403) ends the
        run after the usual bounded cancellation and adds ``credential_rejected``
        and ``control_status`` to the non-terminal result; it never retries.
        """

        try:
            result = self._run()
        except ControlChannelError as error:
            if classify_control_failure(error) != "auth":
                raise
            return {
                "state": "unknown",
                "quarantined": False,
                "reason": "credential_rejected",
                "credential_rejected": True,
                "control_status": error.status,
            }
        status = self._watch.rejected_status
        if result.get("state") == "unknown" and result.get("terminal") is not True and status:
            result = {**result, "credential_rejected": True, "control_status": status}
        return result

    def _run(self) -> dict[str, Any]:
        inputs = self.root / "inputs"
        runtime_dir = self.root / "runtime"
        inputs.mkdir(mode=0o700, exist_ok=True)
        runtime_dir.mkdir(mode=0o700, exist_ok=True)
        output_base = self.output_root / "development-only-cpu-outputs"
        output_base.mkdir(mode=0o700, exist_ok=True)
        _private_directory(output_base)
        output_workspace_root = output_base / "workspaces"
        output_workspace_root.mkdir(mode=0o700, exist_ok=True)
        _private_directory(output_workspace_root)
        recovered = self._recovery_identity(runtime_dir)
        adapter = self.adapter or DockerCpuAdapter(
            frozenset({self.config.allowed_image}),
            input_root=inputs,
            development_output_root=output_workspace_root,
            egress_proxy_image=self.config.egress_proxy_image,
            memory_bytes=self.config.memory_bytes,
            scratch_bytes=self.config.scratch_bytes,
            max_scratch_bytes=self.config.max_scratch_bytes,
            container_user=self.config.container_user or default_container_user(),
            allow_root_container_user=self.config.allow_root_container_user,
            public_root=self.public_root,
            network_pool=network_pool_from_config(
                self.config.docker_network_pool, self.config.docker_network_prefix_length
            ),
        )
        try:
            worker_limit = worker_output_limit(self.config.max_output_bytes)
        except OutputCollectionError as error:
            raise DevelopmentWorkerError("Development worker configuration is invalid.") from error
        profile = DevelopmentOutputProfile(
            self.output_root,
            self.config.output_capacity_bytes or worker_limit,
            self.config.output_capacity_inodes,
            worker_max_output_bytes=self.config.max_output_bytes,
        )
        if recovered is not None:
            # A persisted runtime descriptor represents an admitted operation.
            # Reconcile it before any lease request: a restart must never turn an
            # unknown prior start into authority for a second start.
            lease_id, epoch, worker_id = recovered
            with DevelopmentCpuRuntime(
                runtime_dir,
                inputs,
                self._watch,
                adapter,
                worker_id=worker_id,
                development_profile=profile,
                provider=self.provider,
                public_root=self.public_root,
                public_cache_dir=self.public_cache_dir,
                registry_auth_file=self.registry_auth_file,
                build_repositories=self.config.build_repositories,
                public_fetch_proxy=self.config.public_fetch_proxy,
            ) as runtime:
                self._configure_runtime(runtime)
                self._attach_stage_watch(runtime, adapter)
                result = runtime.recover(lease_id, epoch)
                return self._drive(runtime, result)

        # Registration is an owner-only, prior human command.  A heartbeat is the
        # first worker mutation and deliberately precedes a new lease acquisition.
        self._heartbeat()
        lease = self._watch.post(
            "/worker/leases", {"grant_id": self.config.grant_id}, self._key("lease")
        )
        lease_id, epoch, worker_id = self._lease_identity(lease)
        with DevelopmentCpuRuntime(
            runtime_dir,
            inputs,
            self._watch,
            adapter,
            worker_id=worker_id,
            development_profile=profile,
            provider=self.provider,
            public_root=self.public_root,
            public_cache_dir=self.public_cache_dir,
            registry_auth_file=self.registry_auth_file,
            build_repositories=self.config.build_repositories,
            public_fetch_proxy=self.config.public_fetch_proxy,
        ) as runtime:
            self._configure_runtime(runtime)
            self._attach_stage_watch(runtime, adapter)
            result = runtime.start(lease_id, epoch)
            return self._drive(runtime, result)

    def _configure_runtime(self, runtime: DevelopmentCpuRuntime) -> None:
        runtime.authority_max_age_seconds = float(self.config.authority_reread_seconds)
        # This service retries a stalled renewal until the lease's own expiry less the margin,
        # so a transient failure of an authority read may leave the lease standing until then.
        runtime.transient_lease_grace = True
        # One view of the lease's time left: the renewal retry and the authority grace agree.
        runtime._lease_clock = self.wall_clock

    def _attach_stage_watch(self, runtime: DevelopmentCpuRuntime, adapter: Any) -> None:
        """Give the supervisor an observe-and-report stage watch runner, unless opted out.

        Local Docker only (no remote provider). The runner reports through the worker's own
        control channel (not the credential watch, so a refused watch report never marks this
        run's credential as rejected) and keeps its outbox under this worker's private root.
        It never raises and never changes an execution result.
        """
        if not getattr(self.stage_watch, "enabled", False):
            return
        if self.provider is not None:
            return
        from .stage_watch_runner import build_stage_watch_runner

        runtime.supervisor.watch_runner = build_stage_watch_runner(
            self.stage_watch, state_dir=self.root, channel=self.channel, adapter=adapter
        )

    def _drive(self, runtime: DevelopmentCpuRuntime, result: DriverResult) -> dict[str, Any]:
        outcome = self._drive_loop(runtime, result)
        retries = self._renewal_retries
        if retries.failed and isinstance(outcome, dict):
            # Every failed renewal attempt of this run is on the grant result, whether a later
            # retry got through or the lease ran out (an operator can see stalls that a
            # healthy stage survived, not only the ones that stopped it).
            outcome = {**outcome, "lease_renewal_retries": retries.summary()}
        return outcome

    def _drive_loop(self, runtime: DevelopmentCpuRuntime, result: DriverResult) -> dict[str, Any]:
        # None: renew on the first pass. start() leases afresh, but recover() resumes a lease
        # that may have only seconds left, and neither renews it.
        renewed_at: float | None = None
        for tick in range(self.config.max_ticks + 1):
            if result.phase == "terminal" and result.observation.termination_confirmed:
                if runtime.operation is not None:
                    refused = self._refusal_outcome(runtime.operation.operation_id)
                    if refused is not None:
                        # A permanent refusal is already durable: never repost.
                        return refused
                # collect reports a fresh confirmed exit before touching outputs.
                try:
                    observation = runtime.report(result)
                except Exception as error:
                    return self._report_failed(runtime, error)
                try:
                    receipt = runtime.collect(result, reported_observation=observation)
                except SupervisorError as error:
                    # A confirmed provider exit does not make missing, unsafe, or
                    # untransferable output safe to reclaim.  Keep its workspace.
                    runtime.development_outputs.mark_unknown(runtime.operation.operation_id)  # type: ignore[union-attr]
                    return {
                        "state": "unknown",
                        "quarantined": False,
                        "reason": "quarantine_failed",
                        "detail": safe_error_detail(error, self.root),
                    }
                run_receipt, failure = self._post_run_receipt(runtime, result)
                if failure is not None:
                    return failure
                runtime.development_outputs.cleanup(runtime.operation.operation_id)  # type: ignore[union-attr]
                outcome = {"state": "exited", "quarantined": True, "receipt": receipt}
                if run_receipt is not None:
                    outcome["run_receipt"] = {
                        "status": "recorded",
                        "receipt_digest": run_receipt.get("receipt_digest"),
                    }
                return outcome
            if result.phase in {"terminal", "denied", "uncertain"}:
                if runtime.operation is not None:
                    runtime.development_outputs.mark_unknown(runtime.operation.operation_id)
                if self._is_report_failure(result):
                    return self._cancel_unresolved(
                        runtime, "receipt_report_failure", report_exit=True
                    )
                try:
                    self._report(runtime, result)
                except Exception as error:
                    return self._report_failed(runtime, error)
                outcome = {"state": "unknown", "quarantined": False, "reason": result.reason}
                if result.detail is not None:
                    # Failed before any container existed: not started, no run cost.
                    outcome["detail"] = result.detail
                    outcome["container_created"] = False
                return outcome
            if self._is_report_failure(result):
                return self._cancel_unresolved(runtime, "receipt_report_failure", report_exit=True)
            try:
                self._report(runtime, result)
            except Exception as error:
                stopped = self._report_failed(runtime, error, running=True)
                if stopped is not None:
                    return stopped
                # The observation is queued in the durable outbox and the healthy container
                # keeps running; the next ticks retry it after its backoff (ADR 0054).
            if tick == self.config.max_ticks:
                # Do not abandon Docker merely because this foreground loop has
                # exhausted its own budget. Request cancellation and retain the
                # workspace for reconciliation rather than claiming it stopped.
                cancelled = runtime.cancel(reason="worker_tick_limit")
                if runtime.operation is not None:
                    runtime.development_outputs.mark_unknown(runtime.operation.operation_id)
                try:
                    self._report(runtime, cancelled)
                except Exception:
                    # The cancellation was already attempted.  Keep the durable
                    # report intent and workspace for a later reconciliation.
                    pass
                return {
                    "state": "unknown",
                    "quarantined": False,
                    "reason": "tick_limit_cancellation_requested",
                }
            now = self.clock()
            retries = self._renewal_retries
            if retries.retry_at is not None:
                due = now >= retries.retry_at
            else:
                due = (
                    renewed_at is None
                    or now - renewed_at >= self.config.renew_after_seconds
                    or bool(tick and tick % self.config.renew_every_ticks == 0)
                )
            if due:
                stopped = self._renew_or_retry(runtime)
                if stopped is not None:
                    return stopped
                if retries.retry_at is None:
                    # Measured before the request, so the interval never stretches.
                    renewed_at = now
            # Never sleep past the next renewal (or renewal retry): a long poll interval cannot
            # outlast the lease.
            if retries.retry_at is not None:
                until_due = retries.retry_at - self.clock()
            else:
                assert renewed_at is not None  # the first pass always renews
                until_due = renewed_at + self.config.renew_after_seconds - self.clock()
            self.sleep(max(0.0, min(self.config.poll_seconds, until_due)))
            result = runtime.tick()
        raise AssertionError("bounded worker loop did not return")

    def _refusal_outcome(self, operation_id: str) -> dict[str, Any] | None:
        try:
            detail = self.receipt_refusals.detail(operation_id)
        except (sqlite3.Error, OSError):
            return self._refusal_unrecorded(
                operation_id,
                "the receipt refusal journal could not be read; the run receipt was not reposted",
            )
        if detail is None:
            return None
        return {
            "state": "unknown",
            "quarantined": False,
            "reason": "run_receipt_refused",
            "detail": detail,
            "terminal": True,
            "operation_id": operation_id,
        }

    @staticmethod
    def _refusal_unrecorded(operation_id: str, detail: str) -> dict[str, Any]:
        return {
            "state": "unknown",
            "quarantined": False,
            "reason": "run_receipt_refusal_unrecorded",
            "detail": detail,
            "terminal": True,
            "operation_id": operation_id,
        }

    def _post_run_receipt(
        self, runtime: DevelopmentCpuRuntime, result: DriverResult
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Post the run receipt with bounded retry; return (receipt, failure outcome).

        Transient control failures retry with the same deterministic key.  A
        permanent refusal is recorded durably and is terminal.  Custody (workspace
        and uploaded output) is kept on every failure; nothing is claimed recorded.
        """
        for attempt in range(1, RUN_RECEIPT_ATTEMPTS + 1):
            try:
                return runtime.post_run_receipt(result), None
            except Exception as error:
                if not isinstance(error, OSError):  # ControlChannelError is a ConnectionError
                    return None, self._receipt_failed(safe_error_detail(error, self.root))
                kind = classify_control_failure(error)
                if isinstance(error, ControlChannelError) and kind == "auth":
                    # Fixable by the operator: no durable record, one attempt only.
                    return None, self._receipt_failed(
                        f"control rejected the worker credential (HTTP {error.status}); "
                        "renew or replace the credential, then restart to repost"
                    )
                if isinstance(error, ControlChannelError) and kind == "permanent":
                    operation_id = runtime.operation.operation_id  # type: ignore[union-attr]
                    detail = f"control refused the run receipt (HTTP {error.status})"
                    try:
                        self.receipt_refusals.record(operation_id, error.status, detail)  # type: ignore[arg-type]
                    except (sqlite3.Error, OSError):
                        return None, self._refusal_unrecorded(
                            operation_id,
                            f"control refused the run receipt (HTTP {error.status}); "
                            "the refusal could not be recorded durably, so a restart will repost",
                        )
                    return None, self._refusal_outcome(operation_id)
                if attempt == RUN_RECEIPT_ATTEMPTS:
                    detail = safe_error_detail(error, self.root)
                    return None, self._receipt_failed(
                        f"retries exhausted after {attempt} attempts: {detail}"[:_DETAIL_LIMIT]
                    )
                self.sleep(_RUN_RECEIPT_RETRY_SECONDS)
        raise AssertionError("bounded receipt retry did not return")

    @staticmethod
    def _receipt_failed(detail: str) -> dict[str, Any]:
        return {
            "state": "unknown",
            "quarantined": False,
            "reason": "run_receipt_failed",
            "detail": detail,
        }

    def _lease_seconds_left(self, runtime: DevelopmentCpuRuntime) -> float | None:
        """Seconds until the held lease expires, less the safety margin; None without a lease."""
        lease = getattr(runtime, "lease", None)
        expires_at = getattr(lease, "expires_at", None)
        if not isinstance(expires_at, datetime):
            return None
        left = (expires_at - self.wall_clock()).total_seconds()
        return left - LEASE_SAFETY_MARGIN_SECONDS

    def _renewal_request_seconds(self, runtime: DevelopmentCpuRuntime) -> float | None:
        """One renewal request's time limit, worked out again before each call.

        ``min(channel timeout, max(1 s, time left / 3))``, the time left being the lease's
        expiry less the safety margin: two or three attempts fit before the deadline, and a
        single hung request cannot use up the window. ``None`` (no per-call cap) when the
        channel has no timeout of its own or there is no lease.
        """
        channel_timeout = self._watch.timeout_seconds
        if channel_timeout is None:
            return None
        left = self._lease_seconds_left(runtime)
        if left is None:
            return None
        return lease_request_seconds(channel_timeout, left, attempts=_RENEWAL_ATTEMPTS_PER_WINDOW)

    def _renewal_limit(self, runtime: DevelopmentCpuRuntime) -> dict[str, float]:
        """The keyword that caps one renewal call (computed now), or none without a cap."""
        seconds = self._renewal_request_seconds(runtime)
        return {} if seconds is None else {"timeout_seconds": seconds}

    def _renew_or_retry(self, runtime: DevelopmentCpuRuntime) -> dict[str, Any] | None:
        """Heartbeat and renew once; None while the run goes on, else the stopped outcome.

        A transient failure (no answer in time, a dropped connection, a 5xx) while the lease
        has time left schedules a retry with backoff and the stage keeps running. A refusal
        (a 409 fence, a 404, an authority-gone 403), a rejected credential, or a lease whose
        expiry (less the safety margin) arrives before a renewal is confirmed stops it, and the
        result names the cause and the failed attempts before it.
        """
        retries = self._renewal_retries
        try:
            self._heartbeat(runtime)
            self._renew(runtime)
        except Exception as error:
            retries.failed += 1
            retries.consecutive += 1
            detail = safe_error_detail(error, self.root)
            retries.last_failure = detail
            if is_transient_control_failure(error):
                kind = "transient"
            elif isinstance(error, ControlChannelError):
                kind = classify_control_failure(error)
            else:
                kind = "local"
            if kind == "transient":
                left = self._lease_seconds_left(runtime)
                if left is not None and left > 0:
                    backoff = min(
                        _RENEWAL_RETRY_CAP_SECONDS,
                        _RENEWAL_RETRY_BASE_SECONDS * 2 ** (retries.consecutive - 1),
                    )
                    retries.retry_at = self.clock() + min(backoff, left)
                    return None
                cause = (
                    "the lease expired before a renewal was confirmed: "
                    f"{retries.consecutive} renewal attempt(s) failed, last: {detail}"
                )
            elif isinstance(error, ControlChannelError) and kind in {"permanent", "auth"}:
                cause = f"control refused the lease renewal (HTTP {error.status})"
                if retries.consecutive > 1:
                    cause += (
                        f" after {retries.consecutive - 1} unanswered renewal attempt(s) "
                        f"(the lease may have expired during the stall)"
                    )
            else:
                cause = f"the renewal could not be attempted: {detail}"
            retries.retry_at = None
            outcome = self._cancel_unresolved(
                runtime, "heartbeat_or_renewal_failure", report_exit=True
            )
            return {**outcome, "renewal_failure": cause[: _DETAIL_LIMIT * 2]}
        if retries.consecutive:
            retries.recovered += 1
        retries.consecutive = 0
        retries.retry_at = None
        return None

    def _renew(self, runtime: DevelopmentCpuRuntime) -> None:
        if runtime.lease is None:
            raise DevelopmentWorkerError("Development runtime has no lease to renew.")
        lease = runtime.lease
        key = self.renewals.key(lease.id, lease.epoch)  # durable before the HTTP mutation
        response = self._watch.post(
            "/worker/leases/renew",
            {"lease_id": lease.id, "expected_epoch": lease.epoch},
            key,
            **self._renewal_limit(runtime),
        )
        self.renewals.complete(lease.id, lease.epoch)
        apply = getattr(runtime, "apply_renewal", None)
        if apply is not None:
            # The extended expiry stands even if the package re-read below fails.
            apply(response)
        # Re-read frozen authority; renewal never changes operation identity. The runtime caps
        # this read by the lease time left too (ADR 0178).
        runtime._refresh_lease()

    def _heartbeat(self, runtime: DevelopmentCpuRuntime | None = None) -> None:
        key, body = self.heartbeats.intent(
            {} if self._memory_unsupported else self._memory_report()
        )  # durable before the mutation

        def limit(runtime: DevelopmentCpuRuntime | None) -> dict[str, float]:
            # Worked out again before each call, from the lease time left now.
            return {} if runtime is None else self._renewal_limit(runtime)

        try:
            self._watch.post("/worker/heartbeat", body, key, **limit(runtime))
        except ControlChannelError as error:
            # An older control plane refuses the memory fields (HTTP 400/422: the beat was
            # validated and rejected, never recorded). Liveness must not depend on them: send the
            # plain beat under its own key and stop reporting memory until restart (ADR 0067).
            if not body or error.status not in (400, 422):
                raise
            self._memory_unsupported = True
            self._watch.post("/worker/heartbeat", {}, key + "-plain", **limit(runtime))
        self.heartbeats.complete()

    def _memory_report(self) -> dict[str, int]:
        """What this host reports about its memory (ADR 0067): available now, and the limit its
        stage containers run with. Only figures it can read; none is a guess."""
        report: dict[str, int] = {}
        available = scratch_limits.host_memory_available_bytes()
        if available is not None:
            report["memory_available_bytes"] = available
        report["container_memory_bytes"] = (
            self.config.memory_bytes or scratch_limits.DEFAULT_MEMORY_BYTES
        )
        return report

    @staticmethod
    def _is_report_failure(result: DriverResult) -> bool:
        return result.reason is not None and result.reason.endswith("report_unconfirmed")

    @staticmethod
    def _cancel_unresolved(
        runtime: DevelopmentCpuRuntime, reason: str, *, report_exit: bool = False
    ) -> dict[str, Any]:
        """Cancellation must not depend on a control/reporting round trip succeeding.

        With ``report_exit`` (the run ended for a reason of the worker's own, such as a failed
        renewal, not because control refused an observation), a cancel the provider confirmed
        with an integer exit code is reported like any other confirmed exit, through the
        durable outbox (ADR 0054). Without it control would keep seeing ``running`` and could
        never resolve the attempt (ADR 0053). An undelivered report ends the run as
        ``report_pending`` without a recorded result, so the poller recovers the grant and
        delivers it. An uncertain or unconfirmed cancel, or one without an exit code, reports
        nothing new.
        """

        cancelled: DriverResult | None
        try:
            cancelled = runtime.cancel(reason=reason)
        except Exception:
            # Both the local supervisor journal and any provider-dispatch intent
            # remain durable; do not convert this uncertain state into success.
            cancelled = None
        if runtime.operation is not None:
            runtime.development_outputs.mark_unknown(runtime.operation.operation_id)
        outcome: dict[str, Any] = {"state": "unknown", "quarantined": False, "reason": reason}
        if (
            report_exit
            and cancelled is not None
            and cancelled.phase == "terminal"
            and cancelled.observation.state == "exited"
            and cancelled.observation.termination_confirmed
            and type(cancelled.observation.exit_code) is int
        ):
            try:
                runtime.report(cancelled)
            except ObservationDeliveryPending as error:
                # Keep the cause; delivery_pending is what makes the poller recover it.
                return {**DevelopmentWorkerService._pending_outcome(error), "reason": reason}
            except Exception:
                # Refused or aged out (journaled in the outbox, never reposted), or a
                # follow-up report failed after the exit was delivered: as before.
                pass
        return outcome

    @staticmethod
    def _pending_outcome(error: ObservationDeliveryPending) -> dict[str, Any]:
        pending: dict[str, Any] = {
            "state": "report_pending",
            "quarantined": False,
            "reason": "observation_delivery_pending",
            "delivery_pending": True,
            "retry_after_seconds": round(error.retry_after_seconds, 3),
        }
        if error.auth:
            # A rejected credential: the operator must renew it; the observation waits in
            # the outbox and is delivered after the restart (exit code 3, as before).
            pending["credential_rejected"] = True
            if type(error.status) is int:
                pending["control_status"] = error.status
        return pending

    def _report_failed(
        self, runtime: DevelopmentCpuRuntime, error: Exception, *, running: bool = False
    ) -> dict[str, Any] | None:
        """An observation was not delivered (ADR 0054).

        While the container is observed ``running``, a transient failure (a timeout, a
        connection error, a 5xx) does NOT stop it: the observation stays in the durable outbox,
        this returns ``None`` and the run goes on, retrying after the backoff. Authority is
        proven by the lease renewals, which still cancel on loss. A rejected credential, a
        permanent refusal, or any failure after the run has ended cancels the container as
        before; the observation stays in the outbox, and a retryable failure then ends this run
        as ``report_pending`` WITHOUT a recorded result, so the poller recovers the grant after
        the backoff and delivers the same observation, whatever state the container reached
        meanwhile. Only a permanent refusal, or a report that aged out undelivered, is a
        recorded unknown outcome.
        """
        if running and isinstance(error, ObservationDeliveryPending) and not error.auth:
            return None
        outcome = self._cancel_unresolved(
            runtime,
            "observation_refused"
            if isinstance(error, ObservationRefused)
            else "observation_report_failure",
        )
        if isinstance(error, ObservationDeliveryPending):
            return self._pending_outcome(error)
        return outcome

    @staticmethod
    def _report(runtime: DevelopmentCpuRuntime, result: DriverResult) -> None:
        runtime.report(result)

    @staticmethod
    def _lease_identity(lease: dict[str, Any]) -> tuple[str, int, str]:
        try:
            lease_id, epoch, worker_id = lease["id"], lease["epoch"], lease["worker_principal_id"]
        except (KeyError, TypeError) as error:
            raise DevelopmentWorkerError("Control returned an invalid lease.") from error
        if (
            not isinstance(lease_id, str)
            or type(epoch) is not int
            or not isinstance(worker_id, str)
        ):
            raise DevelopmentWorkerError("Control returned an invalid lease.")
        return lease_id, epoch, worker_id

    def _recovery_identity(self, runtime_dir: Path) -> tuple[str, int, str] | None:
        """Read only the identity needed to attach to a prior runtime descriptor.

        A malformed durable descriptor is a recovery failure, not a reason to
        acquire another lease.  ``DevelopmentCpuRuntime.recover`` revalidates the
        full descriptor and current control authority before any observation.
        """

        path = runtime_dir / "runtime.json"
        if not path.exists() and not path.is_symlink():
            return None
        try:
            descriptor_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor_fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError
                encoded = source.read(MAX_DESCRIPTOR_BYTES + 1)
            if len(encoded) > MAX_DESCRIPTOR_BYTES:
                raise ValueError
            descriptor = json.loads(encoded)
            package = descriptor["control"]["package"]
            lease_id, epoch, worker_id = (
                package["lease_id"],
                package["epoch"],
                package["worker_principal_id"],
            )
            if (
                not isinstance(lease_id, str)
                or not lease_id
                or len(lease_id) > 200
                or type(epoch) is not int
                or epoch < 1
                or not isinstance(worker_id, str)
                or not worker_id
                or len(worker_id) > 200
            ):
                raise ValueError
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise DevelopmentWorkerError(
                "Existing runtime descriptor cannot be recovered."
            ) from error
        return lease_id, epoch, worker_id

    def _key(self, action: str) -> str:
        return (
            "development-worker-"
            + hashlib.sha256(
                json.dumps([action, self.config.grant_id], separators=(",", ":")).encode()
            ).hexdigest()
        )


__all__ = [
    "DEVELOPMENT_OUTPUT_PROFILE",
    "DevelopmentWorkerConfig",
    "DevelopmentWorkerError",
    "DevelopmentWorkerService",
    "HeartbeatIntentJournal",
    "ReceiptRefusalJournal",
    "RenewalIntentJournal",
    "RUN_RECEIPT_ATTEMPTS",
    "read_private_credential",
]
