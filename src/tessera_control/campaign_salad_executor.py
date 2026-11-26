"""Operator-run executor for Salad campaign stages, GPU and CPU (ADR 0038 amendment).

Runs on the trusted Linux server with the trusted-server worker credential. It leases and
packages admitted Salad grants, keeps leases alive, runs each started route through the
unchanged ``salad_live_check --run`` subprocess, imports sealed custody, uploads the verified
files through the worker artifact/manifest stores, reports the provider-confirmed exit and
records its outcome. It never approves, starts, settles, resolves a decision or records a
charge, and it never calls create/start outside the live driver.

One executor serves every operator profile its config names (``profile`` or ``profiles``,
ADR 0070) for its worker principal, so a CPU profile (ADR 0062) placed beside a GPU one is
leased and run by the same process; work for a profile it does not name is refused.

Routes run one at a time unless ``max_concurrent_paid_routes`` (1 to 4, default 1) is raised;
then each child waits on its own thread and a further route starts only while the guardian's
own ``max_open_billable_frames`` admits it (docs/campaigns.md, Salad GPU stage executor).

    python -m tessera_control.campaign_salad_executor --config FILE [--once] [--health-file PATH]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from . import maintenance_lock
from .remote_execution_models import SALAD_PROVISIONING_ALLOWANCE_SECONDS
from .salad_campaign_profiles import (
    DATA_POLICIES,
    DATA_POLICY_PUBLIC,
    DATA_POLICY_TRUST,
    SaladCampaignProfile,
    derive_selection,
    load_profiles,
    proxy_policy,
    route_window_source,
)
from .salad_events import closed_record
from .salad_termination import provider_termination
from .salad_workload_contract import WorkloadContractV1
from .store import canonical_digest

CONFIG_SCHEMA = "tessera.salad.campaign-executor.v1"
RECEIPT_NAME = "salad-live-receipt.json"
LIVE_RECEIPT_SCHEMA = "tessera.salad.live-check.v1"
SUBPROCESS_GRACE_SECONDS = 300
KEEPALIVE_PHASES = ("leased", "route_started", "executing")
_IDENT = re.compile(r"[a-zA-Z0-9_-]{1,200}\Z")
_Path = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
_ProfileName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
SOURCE_PIN_ERROR = (
    "source_revision must be '<40 lowercase hex commit>+sha256:<64 lowercase hex installed "
    "source digest>' (refresh it with: tessera deploy-check --apply refresh_source_pin)"
)
_CONFIG_ERROR = (
    f"Executor config must be an owner-only absolute {CONFIG_SCHEMA} JSON file "
    "whose referenced files and directories are private."
)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CustodyConfig(_Closed):
    host: Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")]
    port: Annotated[int, Field(ge=443, le=443)]
    token_file: _Path
    ca_file: _Path
    certificate_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    # The name workloads are told to reach custody by (a profile's ``custody_host``), when it
    # differs from ``host``, the admin endpoint this executor itself connects to and pins. Absent:
    # ``host`` is both. It is only compared with the profile; it is never connected to or recorded.
    workload_host: (
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")]
        | None
    ) = None

    def client_fields(self) -> dict[str, Any]:
        """The fields of the custody HTTPS client and the closed live-check config."""
        return self.model_dump(mode="json", exclude={"workload_host"})

    def workload_name(self) -> str:
        return self.workload_host or self.host


class StageWatcherBlock(_Closed):
    """Optional tuning of the default-on watcher (ADR 0111, amended by ADR 0125).

    Without this block the executor watches every route with the default kill-switch marker
    (``DEFAULT_KILL_SWITCH_NAME`` in the executor state directory; present means engaged). Opt out
    only with ``{"enabled": false}``. A block that omits ``enabled`` (for example one that only
    tunes ``silent_seconds``) stays enabled, as on the poller. Bounds are
    ``tessera_worker.stage_watch_config.StageWatchConfig``'s (checked here); an omitted bound
    takes its default, an omitted ``kill_switch_path`` the default marker.
    """

    enabled: bool = True
    kill_switch_path: _Path | None = None
    silent_seconds: int | None = None
    heartbeat_seconds: int | None = None
    max_reports: int | None = None

    def watch_config_fields(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in self.model_dump().items()
            if value is not None or name == "kill_switch_path"
        }

    @model_validator(mode="after")
    def _same_bounds_as_the_worker(self) -> StageWatcherBlock:
        # Configuration only: this module imports no watcher code.
        from tessera_worker.stage_watch_config import StageWatchConfig

        StageWatchConfig(**self.watch_config_fields())
        return self


DEFAULT_KILL_SWITCH_NAME = "stage-watch.kill"


def effective_stage_watcher(
    block: StageWatcherBlock | None, state_dir: Path | str
) -> StageWatcherBlock | None:
    """The watcher block this executor runs: default-on, explicit opt-out only. Never raises.

    The default marker lives in the executor's own private state directory and is never
    created here: its presence engages the kill switch (no decision requests or model calls;
    observing and reporting continue, ADR 0087 and 0125). A block that is enabled but names no
    marker takes the default one, as the worker does.
    """
    try:
        if block is not None and not block.enabled:
            return None
        if block is not None and block.kill_switch_path is not None:
            return block
        marker = str(Path(state_dir) / DEFAULT_KILL_SWITCH_NAME)
        fields = {} if block is None else block.model_dump()
        fields.update(enabled=True, kill_switch_path=marker)
        return StageWatcherBlock(**fields)
    except Exception:
        print("salad executor: stage watcher default unusable; no watcher", file=sys.stderr)
        return None


class ExecutorConfig(_Closed):
    schema_id: Annotated[str, Field(alias="schema")]
    state_dir: _Path
    profiles_file: _Path
    # Exactly one of ``profile`` (one name) and ``profiles`` (up to eight distinct names).
    profile: _ProfileName | None = None
    profiles: Annotated[list[_ProfileName], Field(min_length=1, max_length=8)] | None = None
    worker_credential_file: _Path
    credential_directory: _Path
    guardian_socket: _Path
    guardian_uid: Annotated[int, Field(ge=0, le=2**31 - 1)]
    custody: CustodyConfig
    work_directory: _Path
    # Exactly the pin ``salad_live_check.run`` requires; refused here so a bad one stops the
    # executor at start instead of every route at the live-check start.
    source_revision: str
    poll_seconds: Annotated[int, Field(ge=2, le=30)]
    # The guardian may not have reached ``stopped`` right after the exit: re-read a few
    # times, a short fixed delay apart, before the runner falls back to a human charge.
    guardian_retries: Annotated[int, Field(ge=0, le=10)] = 3
    guardian_retry_seconds: Annotated[int, Field(ge=0, le=30)] = 2
    # ADR 0149: after a reallocated route stops, read the provider again (one route observe per
    # poll) until it confirms the group terminated, for at most this long. 0 disables the wait;
    # the attempt is then left held as termination-unconfirmed at once.
    termination_confirm_seconds: Annotated[int, Field(ge=0, le=3600)] = 600
    termination_poll_seconds: Annotated[int, Field(ge=5, le=60)] = 15
    stage_watcher: StageWatcherBlock | None = None
    # How many paid routes (live-check children) this executor drives at once. The default 1
    # is the serial executor: each route's child runs to completion inside the pass. Above 1, a
    # further route starts only while the guardian's own status names a ``max_open_billable_frames``
    # that admits it (the executor never plans past the guardian's setting); otherwise it waits.
    max_concurrent_paid_routes: Annotated[int, Field(ge=1, le=4)] = 1

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    @field_validator("source_revision")
    @classmethod
    def _live_check_source_pin(cls, value: str) -> str:
        from .salad_live_check import is_source_pin

        if not is_source_pin(value):
            raise ValueError(SOURCE_PIN_ERROR)
        return value

    @model_validator(mode="after")
    def _one_profile_field(self) -> ExecutorConfig:
        if (self.profile is None) == (self.profiles is None):
            raise ValueError("name profile or profiles, not both")
        if self.profiles is not None and len(set(self.profiles)) != len(self.profiles):
            raise ValueError("profiles must be distinct")
        return self

    @property
    def profile_names(self) -> tuple[str, ...]:
        return (self.profile,) if self.profile is not None else tuple(self.profiles or ())


def load_config(path: Path | str) -> ExecutorConfig:
    """Closed, owner-only (0600) absolute config file; every referenced path is checked.

    ``source_revision`` must be the pin ``salad_live_check.run`` accepts for the source
    installed now; a missing, malformed or stale pin is refused here with its own message."""
    from .salad_live_check import _private_bytes, _private_path, installed_source_pin
    from .salad_workload_jwt import strict_json

    try:
        config = ExecutorConfig.model_validate(strict_json(_private_bytes(path, 65536)))
        if config.schema_id != CONFIG_SCHEMA:
            raise ValueError("schema")
        for directory in (config.state_dir, config.credential_directory, config.work_directory):
            os.close(_private_path(directory, directory=True))
        for file in (
            config.worker_credential_file,
            config.custody.token_file,
            config.custody.ca_file,
            config.profiles_file,
        ):
            os.close(_private_path(file))
        socket_path = Path(config.guardian_socket)
        if not socket_path.is_absolute() or ".." in socket_path.parts:
            raise ValueError("guardian socket")
    except ValidationError as error:
        if any(item["loc"] == ("source_revision",) for item in error.errors()):
            raise ValueError(SOURCE_PIN_ERROR) from None
        raise ValueError(_CONFIG_ERROR) from None
    except (OSError, ValueError):
        raise ValueError(_CONFIG_ERROR) from None
    # The live check refuses a pin whose digest is not the installed source's.
    commit = config.source_revision.split("+sha256:")[0]
    if config.source_revision != installed_source_pin(commit):
        raise ValueError("source_revision does not pin the installed source; " + SOURCE_PIN_ERROR)
    return config


class ControlPort(Protocol):
    """Everything the executor does to control, as the worker. Tests inject a fake."""

    def work(self) -> list[dict[str, Any]]: ...
    def pending_grants(self) -> set[str]: ...
    def plan(self, grant_id: str) -> dict[str, int]: ...
    def acquire_lease(self, grant_id: str, key: str) -> dict[str, Any]: ...
    def package(
        self, lease_id: str, epoch: int, admitted_attempt_id: str, key: str
    ) -> dict[str, Any]: ...
    def record_package(self, attempt_id: str, payload: dict[str, Any]) -> None: ...
    def operation(self, operation_id: str) -> dict[str, Any] | None: ...
    def heartbeat(self) -> None: ...
    def record_rejection(self, attempt_id: str, step: str, error: BaseException) -> None: ...
    def renew(self, lease_id: str, epoch: int) -> None: ...
    def route(self, route_id: str) -> dict[str, Any]: ...
    def mark_executing(self, attempt_id: str) -> None: ...
    def upload_outputs(
        self, lease_id: str, epoch: int, operation_id: str, directory: Path, files: list[dict]
    ) -> dict[str, Any]: ...
    def report_exited(
        self,
        lease_id: str,
        epoch: int,
        operation_id: str,
        provider_id: str,
        observed_at: str,
    ) -> None: ...
    def record_outcome(self, attempt_id: str, payload: dict[str, Any]) -> None: ...
    def record_no_receipt(self, attempt_id: str, route_id: str) -> None: ...
    def guardian_final(self, route_id: str) -> dict[str, Any] | None: ...


RouteRunner = Callable[[Path, Path, int], "dict[str, Any] | None"]
# ``(route id, destination, the profile the attempt's stage names)``.
Importer = Callable[[str, Path, SaladCampaignProfile], "list[dict[str, Any]]"]


class RouteTimeout(Exception):
    """The live-check subprocess outlived its bound; no outcome is recorded."""


class RouteExitedWithoutReceipt(Exception):
    """The live check exited without a receipt; ``detail`` is a bounded closed-form reason."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


LIVE_CHECK_NO_RECEIPT = "live_check_exited_without_receipt"
_REPORT_TOKEN = re.compile(r"[a-z0-9_]{1,64}")
_REPORT_SCAN_BYTES = 65536
_LOG = logging.getLogger(__name__)


def live_check_exit_detail(stdout: bytes | None) -> str:
    """``status:reason`` from the live check's last JSON report line, else a fixed code.

    Only two short lowercase tokens are ever taken from the output; anything else (other
    fields, other lines, stderr, odd characters, oversized output) is dropped.
    """
    for line in reversed((stdout or b"")[-_REPORT_SCAN_BYTES:].splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        if type(report) is not dict:
            continue
        status, reason = report.get("status"), report.get("reason")
        if (
            type(status) is str
            and type(reason) is str
            and _REPORT_TOKEN.fullmatch(status)
            and _REPORT_TOKEN.fullmatch(reason)
        ):
            return f"{status}:{reason}"
        break
    return LIVE_CHECK_NO_RECEIPT


def subprocess_route_runner(config_path: Path, receipt_dir: Path, timeout: int):
    """Run the unchanged live driver in a fresh interpreter; return its receipt, if any."""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "tessera_control.salad_live_check",
                "--run",
                "--server-config",
                str(config_path),
                "--receipt-directory",
                str(receipt_dir),
            ],
            timeout=timeout,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RouteTimeout from None
    receipt = read_receipt(receipt_dir)
    if receipt is None:
        detail = live_check_exit_detail(completed.stdout)
        _LOG.warning("live check exited without a receipt: %s", detail)
        raise RouteExitedWithoutReceipt(detail)
    return receipt


def read_receipt(receipt_dir: Path) -> dict[str, Any] | None:
    from .salad_live_check import _private_bytes

    try:
        value = json.loads(_private_bytes(Path(receipt_dir) / RECEIPT_NAME, 65536))
    except (OSError, ValueError):
        return None
    if type(value) is not dict or value.get("schema") != LIVE_RECEIPT_SCHEMA:
        return None
    return value


# Phases after packaging and before the route starts: the executor holds the lease while the
# runner gathers reviews and the route approval, so each pass keeps it alive (``_keepalive``).
# ``route_started`` is kept alive too, before it runs; ``executing`` goes to ``_resume``.
LEASE_HELD_PHASES = frozenset(
    {"leased", "awaiting_review", "reviewed", "capability_ready", "route_proposed"}
)


# Consecutive failed provider reads after which the termination wait gives up (ADR 0149).
MAX_TERMINATION_OBSERVE_FAILURES = 3


GROUP_STOP_MARKER = "group-stop.json"
GROUP_STOP_MAX_FAILURES = 5
GROUP_STOP_RECHECK_SECONDS = 30.0


def _same_group(left: Any, right: Any) -> bool:
    """Exact group identity; a UUID matches in any of its spellings, anything else verbatim."""
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except ValueError:
        return left == right


def group_stop_hold_reason(stop: Any, provider_id: str) -> str | None:
    """None when the guardian's status proves the exact group stopped, else why it holds."""
    from .salad_guardian import RECONCILE_PROVIDER

    if type(stop) is not dict:
        return "guardian_status_unavailable"
    if not stop.get("provider_id"):
        return "guardian_group_unrecorded"
    if not _same_group(stop["provider_id"], provider_id):
        return "guardian_group_mismatch"
    if stop.get("status") == "stopped":
        return None
    if stop.get("status") == "reconciled" and stop.get("reason") == RECONCILE_PROVIDER:
        return None
    return "guardian_not_stopped"


def _read_private_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if type(value) is dict else None


def _write_private_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(document, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


_UNREAD = object()


class _RouteRun:
    """One in-flight route (concurrent mode). Its thread calls only ``route_runner`` and
    writes only ``receipt``/``error``; the pass reads them after ``join``."""

    def __init__(self, item: dict[str, Any], config_path: Path, signals: Path | None) -> None:
        self.item, self.config_path, self.signals = item, config_path, signals
        self.stack = ExitStack()
        self.thread: threading.Thread | None = None
        self.receipt: dict[str, Any] | None = None
        self.error: BaseException | None = None

    def run(self, runner: RouteRunner, config_path: Path, receipts: Path, timeout: int) -> None:
        try:
            self.receipt = runner(config_path, receipts, timeout)
        except BaseException as error:  # noqa: BLE001 - handed to the pass unchanged
            self.error = error


def _key(step: str, attempt_id: str) -> str:
    return f"salad-exec-{step}-{hashlib.sha256(attempt_id.encode()).hexdigest()[:40]}"


class SaladCampaignExecutor:
    def __init__(
        self,
        config: ExecutorConfig,
        profile: SaladCampaignProfile | Sequence[SaladCampaignProfile],
        control: ControlPort,
        *,
        worker_principal_id: str,
        route_runner: RouteRunner = subprocess_route_runner,
        importer: Importer,
        sleep: Callable[[float], None] = time.sleep,
        stage_watch: Any = None,
        clock: Callable[[], float] = time.monotonic,
        observer: Callable[[str, SaladCampaignProfile], None] | None = None,
    ) -> None:
        self.sleep = sleep
        self.clock = clock
        # ADR 0149: one provider read of a route's group (the trusted composition in
        # ``build_executor``); without it the control port's ``observe_termination`` is used.
        self.observer = observer
        # Optional ``SaladRouteWatch`` (descriptive reports only; None by default).
        self.stage_watch = stage_watch
        chosen = (profile,) if isinstance(profile, SaladCampaignProfile) else tuple(profile)
        if not chosen or len({item.name for item in chosen}) != len(chosen):
            raise ValueError("The executor needs distinct profiles.")
        if any(item.worker_principal_id != worker_principal_id for item in chosen):
            raise ValueError("The profile belongs to a different worker principal.")
        # Every profile this executor serves, by name; ``profile`` is the first (one-profile
        # configurations read it as before).
        self.profiles: Mapping[str, SaladCampaignProfile] = {item.name: item for item in chosen}
        self.profile = chosen[0]
        self.config, self.control = config, control
        self.route_runner, self.importer = route_runner, importer
        self.work_directory = Path(config.work_directory)
        self._leases: dict[str, tuple[str, int]] = {}
        # Attempts whose lease renewal was refused: never retried past the fence.
        self.fenced: dict[str, str] = {}
        self._step = "grant_poll"  # the control call in flight, named on a recorded refusal
        # Attempts whose live check exited on its own with no receipt, until control has the
        # report (a failed report is re-sent on the next pass of this process only).
        self.no_receipt: set[str] = set()
        # Whether this pass already sent its pre-lease registry heartbeat.
        self._heartbeat_sent = False
        # ADR 0160: failed group-stop reports per attempt; bounded, in memory only.
        self._group_stop_failures: dict[str, int] = {}
        self._group_stop_given_up: set[str] = set()
        self._group_stop_next: dict[str, float] = {}
        self.log: list[dict[str, Any]] = []
        # Concurrent mode only (``max_concurrent_paid_routes`` above 1): the routes whose child
        # runs now, by attempt. Every field of the executor is read and written by the pass's own
        # thread only; a route's thread runs ``route_runner`` alone and writes only its record.
        self._in_flight: dict[str, _RouteRun] = {}
        self._watched: str | None = None  # the one in-flight attempt the stage watcher follows
        self._concurrency_held = False  # a concurrent route ended without a receipt: drain first
        self._executing_elsewhere: set[str] = set()
        self._pass_frame_limit: Any = _UNREAD

    # -- one pass ---------------------------------------------------------------------------

    @property
    def concurrent(self) -> bool:
        return self.config.max_concurrent_paid_routes > 1

    def run_once(self) -> list[dict[str, Any]]:
        start = len(self.log)
        self._heartbeat_sent = False
        concurrent = self.concurrent
        if concurrent:
            self._harvest()
        try:
            items = self.control.work()
        except Exception as error:
            self._note("work_unavailable", None, type(error).__name__)
            return self.log[start:]
        if concurrent:
            self._pass_frame_limit = _UNREAD
            self._executing_elsewhere = {
                entry["attempt_id"]
                for entry in items
                if entry.get("phase") == "executing" and entry["attempt_id"] not in self._in_flight
            }
        pending: set[str] | None = None
        for item in items:
            attempt = item["attempt_id"]
            if concurrent and attempt in self._in_flight:
                # Its child owns the route (and renews the lease) until it returns, exactly as
                # in the serial executor; it is finished by ``_harvest`` only, never resumed.
                continue
            try:
                self._step = "grant_poll"
                if item["phase"] == "awaiting_lease":
                    if pending is None:
                        pending = self.control.pending_grants()
                    self._lease_and_package(item, pending)
                elif item["phase"] == "route_started":
                    self._step = "renew"
                    self._keepalive(item)
                    self._step = "execute"
                    if concurrent:
                        self._launch(item)
                    else:
                        self._execute(item)
                elif item["phase"] == "executing":
                    self._step = "execute"
                    self._resume(item)
                elif item["phase"] in LEASE_HELD_PHASES:
                    self._step = "renew"
                    self._keepalive(item)
            except Exception as error:
                # Class name only: messages can carry identifiers from provider paths.
                self._note("error", attempt, type(error).__name__)
                self._record_rejection(attempt, error)
        return self.log[start:]

    def _record_rejection(self, attempt_id: str, error: BaseException) -> None:
        """Make a control refusal visible on the stage (a short code); never alters the pass."""
        record = getattr(self.control, "record_rejection", None)
        if record is None:
            return
        try:
            record(attempt_id, self._step, error)
        except Exception as failure:  # noqa: BLE001 - recording must not break the pass
            self._note("rejection_unrecorded", attempt_id, type(failure).__name__)

    def _note(self, action: str, attempt_id: str | None, detail: str = "") -> None:
        self.log.append({"action": action, "attempt_id": attempt_id, "detail": detail})

    def _profile_for(self, item: dict[str, Any]) -> SaladCampaignProfile | None:
        """The served profile the attempt's stage names, or None (never another one)."""
        return self.profiles.get(item.get("profile") or "")

    # -- steps 2 and 3: lease, package, keepalive -------------------------------------------

    def _lease_and_package(self, item: dict[str, Any], pending: set[str]) -> None:
        attempt, grant = item["attempt_id"], item["grant_id"]
        if grant not in pending and attempt not in self._leases:
            self._note("not_pending", attempt)
            return
        profile = self._profile_for(item)
        if profile is None:
            self._note("refused_profile", attempt, "different_profile")
            return
        plan = self.control.plan(grant)
        if not 1 <= plan["budget_cents"] <= profile.max_task_cents:
            self._note("refused_budget", attempt, "plan_budget_above_profile")
            return
        if attempt not in self._leases:
            self._step = "lease"
            self._heartbeat_before_lease(attempt)
            lease = self.control.acquire_lease(grant, _key("lease", attempt))
            self._leases[attempt] = (lease["id"], lease["epoch"])
        lease_id, epoch = self._leases[attempt]
        self._step = "package"
        response = self.control.package(
            lease_id, epoch, item["admitted_attempt_id"], _key("package", attempt)
        )
        package = response["package"]
        self._step = "record_package"
        self.control.record_package(
            attempt,
            dict(
                operation_id=package["operation_id"],
                lease_id=lease_id,
                epoch=epoch,
                package_digest=response["package_digest"],
            ),
        )
        self._note("packaged", attempt, package["operation_id"])

    def _heartbeat_before_lease(self, attempt_id: str) -> None:
        """At most one registry heartbeat per pass, sent only right before asking for a lease.

        The store gives lease authority only to a worker seen within its heartbeat window, and
        ``_keepalive`` heartbeats only for attempts already leased, so a fresh or long-idle
        executor would otherwise be refused every pass. A failure is noted (class name only) and
        is not fatal: the lease request still goes out and the store decides.
        """
        if self._heartbeat_sent:
            return
        self._heartbeat_sent = True
        try:
            self.control.heartbeat()
        except Exception as error:
            self._note("heartbeat_failed", attempt_id, type(error).__name__)

    def _lease_for(self, item: dict[str, Any]) -> tuple[str, int] | None:
        attempt = item["attempt_id"]
        if attempt not in self._leases and item.get("operation_id"):
            operation = self.control.operation(item["operation_id"])
            if operation is not None:
                self._leases[attempt] = (operation["lease_id"], operation["epoch"])
        return self._leases.get(attempt)

    def _keepalive(self, item: dict[str, Any]) -> None:
        """Registry heartbeat and lease renewal; a refusal is recorded and never retried."""
        attempt = item["attempt_id"]
        if attempt in self.fenced:
            return
        lease = self._lease_for(item)
        if lease is None:
            return
        try:
            self.control.heartbeat()
            self.control.renew(*lease)
        except Exception as error:
            self.fenced[attempt] = type(error).__name__
            self._note("lease_renewal_refused", attempt, type(error).__name__)

    # -- steps 4 to 6: run a started route --------------------------------------------------

    def _route_dir(self, route_id: str) -> Path:
        if not _IDENT.fullmatch(route_id):
            raise ValueError("exact route id required")
        return self.work_directory / route_id

    def _verified_selection(self, item: dict[str, Any], state: dict[str, Any]):
        """Re-derive the exact selection from the operator profile; refuse on any mismatch."""
        profile = self._profile_for(item)
        if profile is None:
            return None
        proposal = state["proposal"]
        selection = proposal["selection"]
        execution = selection["salad"]["execution"]
        if (
            execution.get("argv_binding") != "workload_v1"
            or (state["org_id"], state["project_id"]) != (profile.org_id, profile.project_id)
            or proposal["operation_id"] != item["operation_id"]
            or not state.get("approved_by")
            or state["desired"] == "stop"
        ):
            return None
        data_policy = execution.get("data_policy", DATA_POLICY_PUBLIC)
        if data_policy not in DATA_POLICIES:
            return None
        # Derived from the admission evidence the control plane recorded, not from the
        # route's own literal: input that was not admitted as public-only never runs under
        # the public literal (ADR 0040 section 4).
        admission = state.get("input_admission")
        if (
            isinstance(admission, dict)
            and admission.get("admission_basis") != "public_only"
            and data_policy != DATA_POLICY_TRUST
        ):
            return None
        plan = self.control.plan(item["grant_id"])
        contract = WorkloadContractV1.model_validate(execution["workload"])
        derived = derive_selection(
            profile,
            contract,
            operation_id=proposal["operation_id"],
            epoch=proposal["epoch"],
            snapshot_digest=selection["manifests"]["data"],
            image=selection["manifests"]["image"],
            max_cost_cents=execution["max_cost_cents"],
            plan_max_seconds=plan["max_duration_seconds"],
            # The runner derived the same value from the stage's inputs; the control plane
            # admitted the route under the provider trust setting (ADR 0040 §4).
            data_policy=data_policy,
        )
        if derived.model_dump(mode="json") != selection:
            return None
        if not 1 <= plan["budget_cents"] <= profile.max_task_cents:
            return None
        return derived, plan, profile

    def _write_live_config(
        self,
        route_id,
        state,
        derived,
        plan,
        directory: Path,
        profile: SaladCampaignProfile,
        watch_signals_file=None,
    ) -> Path:
        from .salad_live_check import MAX_POLLS

        policy = proxy_policy(profile, derived)
        policy_json = policy.model_dump(mode="json")
        policy_json["management"] = policy.management.model_dump(mode="json")
        max_seconds = derived.resources.max_seconds
        config = dict(
            schema="tessera.salad.live-config.v1",
            state_dir=self.config.state_dir,
            route_id=route_id,
            task_id=state["task_id"],
            org_id=state["org_id"],
            project_id=state["project_id"],
            proposal_digest=state["proposal_digest"],
            policy=policy_json,
            worker_credential_file=self.config.worker_credential_file,
            credential_directory=self.config.credential_directory,
            guardian_socket=self.config.guardian_socket,
            guardian_uid=self.config.guardian_uid,
            custody=self.config.custody.client_fields(),
            approved_max_cost_cents=plan["budget_cents"],
            max_seconds=max_seconds,
            max_polls=min(MAX_POLLS, max(30, -(-max_seconds // 5))),
            source_revision=self.config.source_revision,
        )
        config["route_window"] = dict(
            seconds=max_seconds,
            provisioning_allowance_seconds=SALAD_PROVISIONING_ALLOWANCE_SECONDS,
            source=route_window_source(profile, max_seconds),
        )
        if watch_signals_file is not None:
            config["watch_signals_file"] = str(watch_signals_file)
        path = directory / "live-config.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(config, sort_keys=True).encode())
        return path

    def _execute(self, item: dict[str, Any]) -> None:
        prepared = self._prepare_route(item, self.stage_watch)
        if prepared is None:
            return
        attempt = item["attempt_id"]
        config_path, receipts, timeout, signals = prepared
        watch = self.stage_watch
        try:
            with watch.ticking() if signals is not None else nullcontext():
                receipt = self.route_runner(config_path, receipts, timeout)
        except RouteTimeout:
            self._note("route_timeout", attempt)
            return
        except RouteExitedWithoutReceipt as error:
            # The live check exited on its own with no receipt (never after a timeout): the same
            # terminal report as a runner that returns None, carrying the status:reason note.
            self._note("no_receipt", attempt, error.detail)
            self.no_receipt.add(attempt)
            self._report_no_receipt(item)
            return
        finally:
            config_path.unlink(missing_ok=True)
            if signals is not None:
                watch.end(item["operation_id"])
        if receipt is None:
            self._note("no_receipt", attempt)
            self.no_receipt.add(attempt)
            self._report_no_receipt(item)
            return
        self._finish(item, receipt)

    def _prepare_route(
        self,
        item: dict[str, Any],
        watch: Any,
        admit: Callable[[str], bool] | None = None,
    ) -> tuple[Path, Path, int, Path | None] | None:
        """Every check before a route's child starts, then mark it executing and lay out its
        private directory and live config. None when it must not (or may not yet) run.

        ``admit`` (concurrent mode) is asked last before ``mark_executing``: a False answer
        leaves the attempt untouched (``route_started``, no directory) to be asked again."""
        attempt, route_id = item["attempt_id"], item["route_id"]
        if attempt in self.fenced:
            self._note("fenced_no_run", attempt, self.fenced[attempt])
            return None
        state = self.control.route(route_id)
        if any(effect["action"] in {"create", "start"} for effect in state["effects"]):
            # A route with any effect is never re-run; the runner's silent-executor rule applies.
            self._note("refused_route_has_effects", attempt)
            return None
        verified = self._verified_selection(item, state)
        if verified is None:
            self._note("refused_selection_mismatch", attempt)
            return None
        derived, plan, profile = verified
        directory = self._route_dir(route_id)
        if directory.exists():
            # A directory means a prior pass already launched (or tried to): never re-run.
            self._note("refused_route_already_attempted", attempt)
            return None
        if admit is not None and not admit(attempt):
            return None
        try:
            self.control.mark_executing(attempt)
        except Exception as error:
            self._note("mark_executing_refused", attempt, type(error).__name__)
            return None
        directory.mkdir(mode=0o700)
        receipts = directory / "receipts"
        receipts.mkdir(mode=0o700)
        (directory / "outputs").mkdir(mode=0o700)
        signals = None if watch is None else self._watch_begin(watch, item, derived)
        config_path = self._write_live_config(
            route_id, state, derived, plan, directory, profile, signals
        )
        timeout = derived.resources.max_seconds + SUBPROCESS_GRACE_SECONDS
        return config_path, receipts, timeout, signals

    # -- concurrent mode (``max_concurrent_paid_routes`` above 1) ---------------------------

    def _launch(self, item: dict[str, Any]) -> None:
        """Start one route's child on its own thread and return; ``_harvest`` finishes it.

        The checks, ``mark_executing``, the private directory and the live config are exactly
        the serial executor's (``_prepare_route``); only the wait for the child moves off the
        pass. The stage watcher follows one in-flight route at a time (it is built for one)."""
        attempt = item["attempt_id"]
        watch = self.stage_watch if self._watched is None else None
        prepared = self._prepare_route(item, watch, self._admit)
        if prepared is None:
            return
        config_path, receipts, timeout, signals = prepared
        run = _RouteRun(item, config_path, signals)
        if signals is not None:
            self._watched = attempt
            run.stack.enter_context(self.stage_watch.ticking())
        self._in_flight[attempt] = run
        run.thread = threading.Thread(
            target=run.run,
            args=(self.route_runner, config_path, receipts, timeout),
            name="salad-route",
        )
        try:
            run.thread.start()
        except Exception as error:  # noqa: BLE001 - finished below as the runner's failure
            run.thread, run.error = None, error
        self._note("route_launched", attempt)
        if run.thread is None:
            self._harvest()

    def _admit(self, attempt: str) -> bool:
        """Whether one more route may start now; a False answer only defers it (no failure).

        With nothing in flight a route always starts, as in the serial executor. A further one
        needs every bound: the configured count (routes left ``executing`` by an earlier process
        count too, their frames may still bill), no recent receiptless end, and the guardian's
        own ``max_open_billable_frames`` read from an in-flight route's status admitting it. An
        unreadable or refused guardian read (``single_operation_orphan_exposure`` included)
        waits; the attempt is not marked executing, so nothing is spent or retried past it."""
        if not self._in_flight:
            return True
        occupied = len(self._in_flight) + len(self._executing_elsewhere)
        if self._concurrency_held:
            reason = "draining_after_no_receipt"
        elif occupied >= self.config.max_concurrent_paid_routes:
            reason = "executor_route_limit"
        else:
            limit = self._guardian_frame_limit()
            if limit is None:
                reason = "guardian_limit_unknown"
            elif occupied + 1 > limit:
                reason = "guardian_frame_limit"
            else:
                return True
        self._note("route_deferred", attempt, reason)
        return False

    def _guardian_frame_limit(self) -> int | None:
        """The guardian's ``max_open_billable_frames``, read at most once per pass."""
        if self._pass_frame_limit is not _UNREAD:
            return self._pass_frame_limit
        self._pass_frame_limit = None
        read = getattr(self.control, "guardian_frame_limit", None)
        if read is None:
            return None
        for attempt in sorted(self._in_flight):
            try:
                value = read(self._in_flight[attempt].item["route_id"])
            except Exception:  # noqa: BLE001 - an unreadable guardian defers, never fails
                continue
            if type(value) is int and value >= 1:
                self._pass_frame_limit = value
                break
        return self._pass_frame_limit

    def _harvest(self) -> None:
        """Finish every in-flight route whose child returned, in attempt order."""
        for attempt in sorted(self._in_flight):
            run = self._in_flight[attempt]
            if run.thread is not None and run.thread.is_alive():
                continue
            if run.thread is not None:
                run.thread.join()
            del self._in_flight[attempt]
            try:
                self._step = "execute"
                self._complete(run)
            except Exception as error:
                self._note("error", attempt, type(error).__name__)
                self._record_rejection(attempt, error)
        if not self._in_flight:
            self._concurrency_held = False

    def _complete(self, run: _RouteRun) -> None:
        """The serial executor's handling of the child's result, unchanged."""
        item, attempt = run.item, run.item["attempt_id"]
        if run.receipt is None:
            # Any receiptless end (a guardian fence refusal inside the child included) stops
            # further concurrent starts until every in-flight route is finished.
            self._concurrency_held = True
        try:
            run.stack.close()
            if run.error is not None:
                raise run.error
            receipt = run.receipt
        except RouteTimeout:
            self._note("route_timeout", attempt)
            return
        except RouteExitedWithoutReceipt as error:
            self._note("no_receipt", attempt, error.detail)
            self.no_receipt.add(attempt)
            self._report_no_receipt(item)
            return
        finally:
            run.config_path.unlink(missing_ok=True)
            if run.signals is not None:
                self._watched = None
                self.stage_watch.end(item["operation_id"])
        if receipt is None:
            self._note("no_receipt", attempt)
            self.no_receipt.add(attempt)
            self._report_no_receipt(item)
            return
        self._finish(item, receipt)

    def _report_no_receipt(self, item: dict[str, Any]) -> None:
        """Tell control the live check exited without a receipt (no effect is asserted)."""
        attempt = item["attempt_id"]
        try:
            self.control.record_no_receipt(attempt, item["route_id"])
        except Exception as error:
            self._note("no_receipt_report_failed", attempt, type(error).__name__)
            return
        self.no_receipt.discard(attempt)
        self._note("no_receipt_reported", attempt)

    def _watch_begin(self, watch: Any, item: dict[str, Any], derived: Any) -> Path | None:
        """Start the descriptive watch for this route; None (no watch) on any failure."""
        try:
            lease = self._lease_for(item)
            if lease is None:
                return None
            return watch.begin(
                operation_id=item["operation_id"],
                lease_id=lease[0],
                epoch=lease[1],
                stage_key=item["stage_key"],
                deadline_seconds=derived.resources.max_seconds,
            )
        except Exception:
            return None

    def _resume(self, item: dict[str, Any]) -> None:
        """An `executing` attempt after a restart: finish only from a receipt on disk.

        With no receipt, the guardian's confirmed stop of the exact group is delivered as
        termination evidence (ADR 0160); nothing else is recorded and no outcome is invented."""
        self._keepalive(item)
        receipts = self._route_dir(item["route_id"]) / "receipts"
        receipt = read_receipt(receipts) if receipts.is_dir() else None
        if receipt is not None:
            self._finish(item, receipt)
            return
        if item["attempt_id"] in self.no_receipt:
            self._report_no_receipt(item)
        self._report_group_stop(item)

    def _report_group_stop(self, item: dict[str, Any]) -> None:
        """Report the guardian's confirmed stop of the route's exact group (ADR 0160).

        Only when the guardian's own status names the same provider group control recorded on
        the route, and says ``stopped`` (exact terminal, custody sealed) or ``reconciled`` by the
        provider's fresh exact read of the group stopped. Anything else holds and is logged with
        a reason. The observation is sent once, as the worker that held the lease, with a time
        fixed in a private marker so a retry replays the same request."""
        attempt, route_id = item["attempt_id"], item["route_id"]
        read = getattr(self.control, "guardian_stop", None)
        report = getattr(self.control, "report_group_stopped", None)
        if read is None or report is None or attempt in self._group_stop_given_up:
            return
        directory = self._route_dir(route_id)
        if not directory.is_dir():
            return  # never launched here: there is no group to speak for
        marker = directory / GROUP_STOP_MARKER
        recorded = _read_private_json(marker)
        if recorded is not None and recorded.get("reported") is True:
            return
        lease = self._lease_for(item)
        if lease is None:
            self._note("group_stop_held", attempt, "lease_unknown")
            return
        if recorded is None:
            now = time.monotonic()
            if now < self._group_stop_next.get(attempt, 0.0):
                return  # looked recently; the guardian is read at most every recheck interval
            self._group_stop_next[attempt] = now + GROUP_STOP_RECHECK_SECONDS
            provider_id = self.control.route(route_id).get("provider_id")
            if not provider_id:
                self._note("group_stop_held", attempt, "group_identity_unrecorded")
                return
            try:
                stop = read(route_id)
            except Exception as error:  # noqa: BLE001 - an unreadable guardian holds
                self._note("group_stop_held", attempt, type(error).__name__)
                return
            reason = group_stop_hold_reason(stop, provider_id)
            if reason is not None:
                self._note("group_stop_held", attempt, reason)
                return
            recorded = {
                "provider_id": provider_id,
                "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "guardian_status": stop["status"],
                "guardian_reason": str(stop.get("reason") or "")[:80],
                "reported": False,
            }
            _write_private_json(marker, recorded)
        try:
            report(
                lease[0],
                lease[1],
                item["operation_id"],
                recorded["provider_id"],
                recorded["observed_at"],
            )
        except Exception as error:  # noqa: BLE001 - recorded and bounded, never fatal
            failures = self._group_stop_failures.get(attempt, 0) + 1
            self._group_stop_failures[attempt] = failures
            if failures >= GROUP_STOP_MAX_FAILURES:
                self._group_stop_given_up.add(attempt)
            self._note("group_stop_report_failed", attempt, type(error).__name__)
            return
        recorded["reported"] = True
        _write_private_json(marker, recorded)
        self._group_stop_failures.pop(attempt, None)
        self._note("group_stop_reported", attempt, recorded["guardian_status"])

    # -- step 5: custody import, upload, observation, outcome -------------------------------

    def _finish(self, item: dict[str, Any], receipt: dict[str, Any]) -> None:
        attempt, route_id = item["attempt_id"], item["route_id"]
        manifest_id = None
        provider_id = None
        state = self.control.route(route_id)
        provider_id = state.get("provider_id")
        lease = self._lease_for(item)
        waiting = provider_termination(state)
        if waiting.status == "unconfirmed" and waiting.reason != "no_stop_requested":
            state = self._confirm_termination(attempt, route_id, self._profile_for(item))
        if receipt.get("stop_confirmed") is True and lease is not None:
            try:
                manifest_id = self._import_and_upload(item, state, lease)
            except Exception as error:
                self._note("import_failed", attempt, type(error).__name__)
        if (
            lease is not None
            and provider_id
            and state.get("termination_confirmed") is True
            and not state.get("reallocated")
        ):
            observed_at = _last_provider_observation(state)
            if observed_at is not None:
                try:
                    self.control.report_exited(
                        lease[0], lease[1], item["operation_id"], provider_id, observed_at
                    )
                except Exception as error:
                    self._note("observation_failed", attempt, type(error).__name__)
        reading = provider_termination(state)
        if lease is not None and provider_id and reading.confirmed and reading.observed_at:
            # ADR 0149: the provider's own read confirmed the reallocated group stopped. The
            # exited observation is reported once (fixed key) at that read's time; control
            # re-checks the route's provider readbacks itself before it settles anything.
            try:
                self.control.report_exited(
                    lease[0], lease[1], item["operation_id"], provider_id, reading.observed_at
                )
                self._note("termination_confirmed_reported", attempt)
            except Exception as error:
                self._note("observation_failed", attempt, type(error).__name__)
        elif reading.status == "unconfirmed":
            self._note("termination_unconfirmed", attempt, reading.code)
        financial = receipt.get("financial")
        guardian_final = self._read_guardian_final(attempt, route_id)
        self.control.record_outcome(
            attempt,
            dict(
                route_id=route_id,
                live_receipt=receipt,
                manifest_id=manifest_id,
                provider_id=provider_id,
                estimated_exposure=financial if isinstance(financial, dict) else None,
                guardian_final=guardian_final,
            ),
        )
        self._note("outcome_recorded", attempt, "manifest" if manifest_id else "no_manifest")

    def _confirm_termination(
        self, attempt: str, route_id: str, profile: SaladCampaignProfile | None
    ) -> dict[str, Any]:
        """Poll the provider (one route observe per poll) until it confirms the reallocated
        group terminated or ``termination_confirm_seconds`` pass; return the latest route.

        Only the provider readbacks recorded on the route count (``provider_termination``); the
        guardian's status is never read here. A refused observe (for example the route's effect
        budget is spent) ends the wait at once: nothing is retried past a refusal."""
        deadline = self.clock() + self.config.termination_confirm_seconds
        polls = 0
        state = self.control.route(route_id)
        observe = self.observer or getattr(self.control, "observe_termination", None)
        if profile is None or observe is None:
            self._note("termination_wait_refused", attempt, "observer_unavailable")
            return state
        failures = 0
        while self.clock() < deadline:
            polls += 1
            try:
                observe(route_id, profile)
                failures = 0
            except Exception as error:
                failures += 1
                self._note("termination_observe_failed", attempt, type(error).__name__)
                # A refusal (fenced route, spent effect budget) is final; a failed provider
                # read is retried at most MAX_TERMINATION_OBSERVE_FAILURES times in a row.
                if (
                    type(error).__name__ in {"Conflict", "Forbidden", "NotFound", "LookupError"}
                    or failures >= MAX_TERMINATION_OBSERVE_FAILURES
                ):
                    break
            state = self.control.route(route_id)
            if provider_termination(state).confirmed:
                self._note("termination_confirmed", attempt, f"polls={polls}")
                return state
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            self.sleep(min(self.config.termination_poll_seconds, remaining))
        self._note("termination_wait_ended", attempt, f"polls={polls}")
        return state

    def _read_guardian_final(self, attempt: str, route_id: str) -> dict[str, Any] | None:
        """The guardian's final status, re-read while it is missing or not yet ``stopped``.

        None (the runner then waits for a human charge) is logged with the error class, so
        a miswired status client is visible rather than silently disabling the bound."""
        final: dict[str, Any] | None = None
        reason = "unprojectable"
        for index in range(1 + self.config.guardian_retries):
            failed = False
            try:
                final = self.control.guardian_final(route_id)
            except Exception as error:
                final, reason, failed = None, type(error).__name__, True
            if final is not None and final.get("status") == "stopped":
                return final
            if final is None and not failed:
                # No guardian is configured (or nothing projectable): re-reading cannot help.
                break
            if index < self.config.guardian_retries:
                self.sleep(self.config.guardian_retry_seconds)
        if final is None:
            self._note("guardian_final_unavailable", attempt, reason)
        else:
            self._note("guardian_not_stopped", attempt, str(final.get("status"))[:40])
        return final

    def _import_and_upload(self, item, state, lease) -> str:
        profile = self._profile_for(item)
        if profile is None:
            raise ValueError("the attempt's profile is not served here")
        directory = self._route_dir(item["route_id"]) / "outputs"
        files = self.importer(item["route_id"], directory, profile)
        for entry in files:
            data = (directory / entry["name"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["bytes"]:
                raise ValueError("imported file differs from its manifest entry")
        manifest = self.control.upload_outputs(
            lease[0], lease[1], item["operation_id"], directory, files
        )
        return manifest["id"]


def _last_provider_observation(state: dict[str, Any]) -> str | None:
    provider = [r for r in state.get("receipts", []) if r.get("provenance") == "provider"]
    return provider[-1].get("observed_at") if provider else None


# -- real composition (trusted server only; not exercised by default tests) ------------------


class StoreControl:
    """ControlPort over the control stores, acting as the worker. No HTTP, no new authority."""

    def __init__(
        self,
        state_dir: Path | str,
        worker: dict[str, Any],
        guardian_status: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        from .campaign_store import CampaignStore
        from .execution import ExecutionStore
        from .project_artifact_store import ProjectArtifactStore
        from .remote_execution import RemoteExecutionStore
        from .runtime_packages import RuntimePackageStore
        from .worker_grants import WorkerGrantStore
        from .workers import WorkerRegistryStore

        self.state_dir, self.worker = Path(state_dir), worker
        self.guardian_status = guardian_status
        self.campaigns = CampaignStore(self.state_dir)
        self.grants = WorkerGrantStore(self.state_dir)
        self.executions = ExecutionStore(self.state_dir)
        self.packages = RuntimePackageStore(self.state_dir)
        self.registry = WorkerRegistryStore(self.state_dir)
        self.routes = RemoteExecutionStore(self.state_dir)
        # The worker-facing artifact store with project placement (a subclass of the manifest
        # store): outputs are committed under the project's storage binding before the manifest,
        # exactly as a worker-grant host does, so a project whose earlier stage ran on such a
        # host (and so has a binding) still accepts the Salad stage's manifest.
        self.artifacts = ProjectArtifactStore(self.state_dir)

    def _read(self, sql: str, args: tuple) -> list[Any]:
        from .store import _connect

        connection = _connect(self.executions.database_path)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def work(self):
        return self.campaigns.list_salad_work(self.worker)

    def pending_grants(self):
        return {g["id"] for g in self.grants.list_pending(self.worker)["grants"]}

    def plan(self, grant_id):
        rows = self._read(
            """SELECT r.budget_cents, r.max_duration_seconds FROM execution_grants g
            JOIN projects p ON p.id = g.project_id
            JOIN task_revisions r ON r.task_id = g.task_id AND r.plan_digest = g.plan_digest
            AND r.revision < g.task_revision
            WHERE g.id = ? AND g.worker_principal_id = ? AND p.org_id = ?
            ORDER BY r.revision DESC LIMIT 1""",
            (grant_id, self.worker["id"], self.worker["org_id"]),
        )
        if not rows:
            raise LookupError("plan unavailable")
        return {k: rows[0][k] for k in ("budget_cents", "max_duration_seconds")}

    def acquire_lease(self, grant_id, key):
        return self.executions.acquire_execution_lease(
            self.worker, grant_id, key, canonical_digest({"grant_id": grant_id})
        )[1]

    def package(self, lease_id, epoch, admitted_attempt_id, key):
        payload = dict(
            expected_epoch=epoch,
            admitted_attempt_id=admitted_attempt_id,
            execution_adapter="kubernetes-job",
        )
        return self.packages.execution_package(
            self.worker,
            lease_id,
            key,
            canonical_digest(payload),
            epoch,
            False,
            None,
            admitted_attempt_id,
            False,
            "kubernetes-job",
        )[1]

    def record_package(self, attempt_id, payload):
        self.campaigns.record_salad_package(
            self.worker, attempt_id, _key("record-package", attempt_id), payload
        )

    def operation(self, operation_id):
        rows = self._read(
            "SELECT lease_id, epoch, grant_id FROM external_operations "
            "WHERE operation_id = ? AND org_id = ? AND worker_principal_id = ?",
            (operation_id, self.worker["org_id"], self.worker["id"]),
        )
        return dict(rows[0]) if rows else None

    def heartbeat(self):
        import uuid

        self.registry.heartbeat(
            self.worker, "salad-exec-worker-" + uuid.uuid4().hex, canonical_digest({}), {}
        )

    def record_rejection(self, attempt_id, step, error):
        from .stage_rejections import record

        record(self.campaigns, self.worker, attempt_id, step, error)

    def renew(self, lease_id, epoch):
        import uuid

        payload = {"lease_id": lease_id, "expected_epoch": epoch}
        self.executions.renew_execution_lease(
            self.worker,
            lease_id,
            "salad-exec-renew-" + uuid.uuid4().hex,
            canonical_digest(payload),
            epoch,
        )

    def route(self, route_id):
        return self.routes.get(self.worker, route_id)

    def mark_executing(self, attempt_id):
        self.campaigns.mark_salad_executing(
            self.worker, attempt_id, _key("mark-executing", attempt_id)
        )

    def upload_outputs(self, lease_id, epoch, operation_id, directory, files):
        entries = []
        for entry in files:
            data = (Path(directory) / entry["name"]).read_bytes()
            payload = dict(
                expected_epoch=epoch,
                operation_id=operation_id,
                relative_path=entry["name"],
                sha256=entry["sha256"],
                content_base64=base64.b64encode(data).decode("ascii"),
            )
            key = _key("upload-" + entry["name"], operation_id)
            _, uploaded = self.artifacts.upload_artifact(
                self.worker, lease_id, key, canonical_digest(payload), payload
            )
            entries.append(
                dict(
                    id=uploaded["id"],
                    relative_path=entry["name"],
                    sha256=entry["sha256"],
                    size=len(data),
                )
            )
        placement = dict(
            expected_epoch=epoch,
            operation_id=operation_id,
            artifacts=[{name: item[name] for name in ("id", "sha256", "size")} for item in entries],
        )
        self.artifacts.commit_project_artifacts(
            self.worker,
            lease_id,
            _key("placement", operation_id),
            canonical_digest(placement),
            placement,
        )
        payload = dict(expected_epoch=epoch, operation_id=operation_id, artifacts=entries)
        return self.artifacts.create_manifest(
            self.worker,
            lease_id,
            _key("manifest", operation_id),
            canonical_digest(payload),
            payload,
        )[1]

    def report_exited(self, lease_id, epoch, operation_id, provider_id, observed_at):
        payload = dict(
            expected_epoch=epoch,
            operation_id=operation_id,
            observed_state="exited",
            observed_at=observed_at,
            provider_id=provider_id,
            termination_confirmed=True,
        )
        self.executions.report_execution_observation(
            self.worker,
            lease_id,
            _key("observe-exited", operation_id),
            canonical_digest(payload),
            payload,
        )

    def report_group_stopped(self, lease_id, epoch, operation_id, provider_id, observed_at):
        """The guardian's confirmed stop of the exact group, as termination evidence (ADR 0160)."""
        payload = dict(
            expected_epoch=epoch,
            operation_id=operation_id,
            observed_state="exited",
            observed_at=observed_at,
            provider_id=provider_id,
            termination_confirmed=True,
            termination_evidence="provider_group_stopped",
        )
        self.executions.report_execution_observation(
            self.worker,
            lease_id,
            _key("observe-group-stopped", operation_id),
            canonical_digest(payload),
            payload,
        )

    def guardian_stop(self, route_id):
        """The guardian's verdict and the group it is about, or None with no guardian."""
        if self.guardian_status is None:
            return None
        status = self.guardian_status(self.routes.get(self.worker, route_id))
        if type(status) is not dict:
            return None
        return {key: status.get(key) for key in ("status", "reason", "provider_id")}

    def guardian_frame_limit(self, route_id):
        """The guardian's own ``max_open_billable_frames`` from the route's status, else None.

        Read only to bound concurrent starts (never to start or settle anything); a route the
        guardian holds no reservation for, or any other unusable status, gives None."""
        if self.guardian_status is None:
            return None
        status = self.guardian_status(self.routes.get(self.worker, route_id))
        value = status.get("max_open_billable_frames") if type(status) is dict else None
        return value if type(value) is int and value >= 1 else None

    def record_outcome(self, attempt_id, payload):
        self.campaigns.record_salad_outcome(
            self.worker, attempt_id, _key("outcome", attempt_id), payload
        )

    def record_no_receipt(self, attempt_id, route_id):
        self.campaigns.record_salad_no_receipt(
            self.worker, attempt_id, _key("no-receipt", attempt_id), {"route_id": route_id}
        )

    def guardian_final(self, route_id):
        """The final guardian status projection, or None when it cannot be recorded.

        A failed read raises; the executor logs the error class and may retry."""
        if self.guardian_status is None:
            return None
        return project_guardian_final(self.guardian_status(self.routes.get(self.worker, route_id)))


_GUARDIAN_FLAGS = (
    "orphan",
    "allocation_uncertain",
    "escalation_pending",
    "budget_fenced",
    "rate_unknown",
)


def _text(value: Any) -> str | None:
    """A decimal-text field: strings, integers and finite JSON floats (``3.5`` -> ``"3.5"``)."""
    if type(value) in (str, int):
        return str(value)
    if type(value) is float and math.isfinite(value):
        return format(Decimal(repr(value)), "f")
    return None


def _project_rebinds(value: Any) -> dict[str, Any] | None:
    """The guardian's pre-start re-binds (count and the last three), or None when it has none
    or the shape is not the expected one (then nothing is recorded, never a guess)."""
    if type(value) is not dict or type(value.get("count")) is not int or value["count"] < 1:
        return None
    events = []
    for item in (value.get("events") or [])[-3:]:
        if type(item) is not dict or type(item.get("at")) not in {int, float}:
            return None
        events.append(
            {
                "at": datetime.fromtimestamp(item["at"], UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "from_allocation": str(item.get("from") or "")[:96],
                "to_allocation": str(item.get("to") or "")[:96],
                "reason": "prestart_reallocation",
            }
        )
    return {"count": min(value["count"], 10), "events": events}


def project_guardian_final(
    status: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Only the keys the control plane records; anything missing leaves the flag unclean."""
    if type(status) is not dict:
        return None
    from .campaign_store import _GuardianFinal

    billed = status.get("actual_billed_cost_cents")
    projected = dict(
        status=status.get("status"),
        reason=str(status.get("reason") or "")[:80],
        **{flag: status.get(flag) is not False for flag in _GUARDIAN_FLAGS},
        published_hourly_rate_usd=_text(status.get("published_hourly_rate_usd")),
        estimated_exposure_cents=_text(status.get("estimated_exposure_cents")),
        actual_billed_cost_cents=billed if type(billed) is int and billed >= 0 else None,
        observed_at=(now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    timeout = status.get("allocation_timeout")
    # The guardian writes the evidence only for a group that never showed an instance: its own
    # deadline (``allocation_timeout``) or the route limit reached in that state (``deadline``).
    if (
        status.get("stop_cause") in {"allocation_timeout", "deadline"}
        and type(timeout) is dict
        and type(timeout.get("elapsed_seconds")) is int
        and type(timeout.get("deadline_seconds")) is int
    ):
        projected["allocation_timeout"] = {
            "elapsed_seconds": timeout.get("elapsed_seconds"),
            "deadline_seconds": timeout.get("deadline_seconds"),
        }
    cut = status.get("route_deadline")
    if (
        status.get("stop_cause") == "deadline"
        and type(cut) is dict
        and type(cut.get("running_seconds")) is int
        and cut["running_seconds"] >= 0
        and cut.get("limit") in {"running_cap", "route_deadline"}
        and "allocation_timeout" not in projected
    ):
        projected["route_deadline"] = {
            "running_seconds": cut["running_seconds"],
            "limit": cut["limit"],
        }
    rebinds = _project_rebinds(status.get("prestart_rebinds"))
    if rebinds is not None:
        projected["prestart_rebinds"] = rebinds
    failure = status.get("start_failure")
    if (
        status.get("stop_cause") == "start_failure_suspected"
        and type(failure) is dict
        and type(failure.get("machines")) is int
    ):
        projected["start_failure"] = {"machines": failure["machines"]}
    events = closed_record(status.get("start_events"))
    if events is not None:
        projected["start_events"] = events
    try:
        _GuardianFinal.model_validate(projected)
    except ValidationError:
        return None  # an unrecordable status must not lose the outcome
    return projected


def trusted_importer(config: ExecutorConfig, worker, state_dir):
    """Importer over a TrustedSaladServer using the independent custody HTTPS client."""

    def import_route(
        route_id: str, destination: Path, profile: SaladCampaignProfile
    ) -> list[dict[str, Any]]:
        from .remote_execution import RemoteExecutionStore
        from .salad_live_check import _private_bytes
        from .salad_secret_files import PrivateFileSecretResolver
        from .salad_server import SaladSafetyHTTPSClient, TrustedSaladServer

        route = RemoteExecutionStore(state_dir).get(worker, route_id)
        selection = route["proposal"]["selection"]
        from .remote_execution_models import ExecutionSelection

        policy = proxy_policy(profile, ExecutionSelection.model_validate(selection))
        _private_bytes(config.worker_credential_file, 4096)
        resolver = PrivateFileSecretResolver(config.credential_directory)
        try:
            server = TrustedSaladServer(
                state_dir=state_dir,
                worker=worker,
                policy=policy,
                resolver=resolver,
                safety=SaladSafetyHTTPSClient(**config.custody.client_fields()),
            )
            reply = server.import_retained(route_id, destination=str(destination))
        finally:
            resolver.close()
        return reply["files"]

    return import_route


def trusted_observer(config: ExecutorConfig, worker, state_dir):
    """One provider read of a route's group through the trusted server (ADR 0149).

    The same composition as the live driver's ``observe``: a route effect is begun, the
    credentialed transport reads the exact group and the readback is recorded on the route by
    the claim-checked intake. No create, start, stop or delete is ever sent from here."""

    def observe(route_id: str, profile: SaladCampaignProfile) -> None:
        from tessera_worker.remote_dispatch import RemoteDispatcher
        from tessera_worker.salad_remote import SaladTrustedAdapter

        from .remote_execution import RemoteExecutionStore
        from .remote_execution_models import ExecutionSelection
        from .salad_live_check import _private_bytes
        from .salad_secret_files import PrivateFileSecretResolver
        from .salad_server import SaladSafetyHTTPSClient, TrustedSaladServer

        route = RemoteExecutionStore(state_dir).get(worker, route_id)
        selection = ExecutionSelection.model_validate(route["proposal"]["selection"])
        policy = proxy_policy(profile, selection)
        _private_bytes(config.worker_credential_file, 4096)
        resolver = PrivateFileSecretResolver(config.credential_directory)
        try:
            server = TrustedSaladServer(
                state_dir=state_dir,
                worker=worker,
                policy=policy,
                resolver=resolver,
                safety=SaladSafetyHTTPSClient(**config.custody.client_fields()),
            )
            adapter = SaladTrustedAdapter(server, route_id)
            RemoteDispatcher(
                server.store, worker, {policy.target.selection.target_digest: adapter}
            ).dispatch(route_id, "observe")
        finally:
            resolver.close()

    return observe


def build_executor(config: ExecutorConfig) -> SaladCampaignExecutor:
    from .salad_live_check import _private_bytes
    from .store import Store

    loaded = load_profiles(config.profiles_file)
    profiles = [loaded.get(name) for name in config.profile_names]
    if any(profile is None for profile in profiles):
        raise ValueError("Unknown Salad campaign profile.")
    # The executor may only hand workloads a custody name it owns: the name it was configured to
    # advertise (``workload_host``, else ``host``) must be the profile's ``custody_host``. The
    # admin ``host`` it connects to may differ.
    if any(config.custody.workload_name() != profile.custody_host for profile in profiles):
        raise ValueError("Custody host differs from the profile.")
    state_dir = Path(config.state_dir)
    worker = Store(state_dir).authenticate_worker(
        _private_bytes(config.worker_credential_file, 4096).decode("ascii").strip()
    )

    def guardian_status(state):
        from .salad_server import SaladSafetyHTTPSClient

        return SaladSafetyHTTPSClient(**config.custody.client_fields()).status(state)

    stage_watch = None
    watcher_block = effective_stage_watcher(config.stage_watcher, state_dir)
    if watcher_block is not None:
        from .stage_watch_channel import build_salad_route_watch

        stage_watch = build_salad_route_watch(
            watcher_block,
            state_dir=state_dir,
            worker=worker,
            poll_seconds=config.poll_seconds,
        )
        if stage_watch is None:
            print("salad executor: stage watcher not started", file=sys.stderr)
    return SaladCampaignExecutor(
        config,
        profiles,
        StoreControl(state_dir, worker, guardian_status),
        worker_principal_id=worker["id"],
        importer=trusted_importer(config, worker, state_dir),
        stage_watch=stage_watch,
        observer=trusted_observer(config, worker, state_dir),
    )


def main(argv=None) -> int:
    from tessera_worker.service_loop import StopFlag, run_service

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--health-file", type=Path, default=None)
    options = parser.parse_args(argv)
    try:
        config = load_config(options.config)
        # Before anything opens the control database: `tessera db compact` refuses while this
        # shared lock is held, and a running compaction makes this start refuse (exit 1).
        maintenance_lock.hold_shared_for_process(config.state_dir)
        executor = build_executor(config)
    except maintenance_lock.MaintenanceBusy as error:
        print(f"salad executor refused: {error}", file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "refused", "reason": "executor_setup_unavailable"}))
        return 2
    stop = StopFlag()
    stop.install()
    return run_service(
        step=executor.run_once,
        interval=config.poll_seconds,
        once=options.once,
        max_cycles=options.max_cycles,
        health_file=options.health_file,
        name="campaign-salad-executor",
        stop=stop,
    )


if __name__ == "__main__":
    raise SystemExit(main())
