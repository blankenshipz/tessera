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
    python -m tessera_control.campaign_salad_executor --config FILE --check-config

Exit 0 is a clean stop, 1 a runtime failure (restart it), 2 a refusal of the operator's own
configuration (restarting cannot help; ``tessera_control.exit_codes``). ``--check-config`` runs the
configuration checks only: no lock, no control database, no credential read, nothing started.
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
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
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

from . import exit_codes, maintenance_lock
from .campaign_infra_retry import SAFETY_STOP_CAUSES
from .campaign_salad_lease_cap import needed_seconds
from .campaign_salad_lease_lapse import window_shortfall
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
from .salad_exit_watch import nonzero_exit_codes
from .salad_precreate import launch_effects, sent_launch_effects
from .salad_termination import provider_termination
from .salad_workload_contract import WorkloadContractV1
from .store import canonical_digest

CONFIG_SCHEMA = "tessera.salad.campaign-executor.v1"
RECEIPT_NAME = "salad-live-receipt.json"
LIVE_RECEIPT_SCHEMA = "tessera.salad.live-check.v1"
# The child's completion tail (cancel, stop, custody seal, verification) retries transients
# inside bounded windows (salad_live_check.CUSTODY_SEAL_SECONDS, VERIFY_RETRY_SECONDS); this
# grace past the route window covers them. The group is stopped while the tail runs.
SUBPROCESS_GRACE_SECONDS = 900
# The most an operator may configure for ``termination_confirm_seconds``: the runner bounds its
# wait for a lease-renewing executor by it (it cannot read the executor's own configuration).
TERMINATION_CONFIRM_MAX_SECONDS = 3600
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
    termination_confirm_seconds: Annotated[int, Field(ge=0, le=TERMINATION_CONFIRM_MAX_SECONDS)] = (
        600
    )
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


class ExecutorConfigRefused(ValueError):
    """The operator's configuration (config file, profiles, worker credential) is refused.

    Raised only by the configuration checks, never by effectful setup, so ``main`` can tell a
    refusal (exit ``CONFIG_REFUSED``, not restarted) from a runtime fault (exit ``RUNTIME_FAILURE``)
    by where the error came from and not by its type."""


def source_pin_is_stale(config: ExecutorConfig) -> bool:
    """True when the well-formed pin is not the installed source's (deploy-upgrade re-pins it)."""
    from .salad_live_check import installed_source_pin

    commit = config.source_revision.split("+sha256:")[0]
    return config.source_revision != installed_source_pin(commit)


def load_config(path: Path | str, *, allow_stale_pin: bool = False) -> ExecutorConfig:
    """Closed, owner-only (0600) absolute config file; every referenced path is checked.

    ``source_revision`` must be the pin ``salad_live_check.run`` accepts for the source
    installed now; a missing, malformed or stale pin is refused here with its own message.
    ``allow_stale_pin`` (``--check-config`` only) accepts a well-formed pin of another source:
    ``deploy-check`` runs under the new release before ``deploy-upgrade`` re-pins it."""
    from .salad_live_check import _private_bytes, _private_path
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
    if not allow_stale_pin and source_pin_is_stale(config):
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
    def record_route_lost(
        self, attempt_id: str, route_id: str, guardian_final: dict[str, Any]
    ) -> None: ...
    def record_refusal(self, attempt_id: str, code: str, step: str) -> None: ...
    def guardian_final(self, route_id: str) -> dict[str, Any] | None: ...
    def record_custody_evidence(self, attempt_id: str, route_id: str) -> None: ...


RouteRunner = Callable[[Path, Path, int], "dict[str, Any] | None"]
# ``(route id, destination, the profile the attempt's stage names)``.
Importer = Callable[..., "list[dict[str, Any]]"]


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


# The sanitized error fields the live check puts on its report or receipt (salad_live_check.
# _error_detail) that the executor repeats in its journal, each re-checked against a closed shape.
_ERROR_FIELD_SHAPES = {
    "exception": re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}"),
    "cause": re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}"),
    "detail": re.compile(r"[a-z][a-z0-9_]{0,63}"),
    "site": re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\.[A-Za-z_<>][A-Za-z0-9_<>]{0,63}"),
    "step": re.compile(r"[a-z][a-z0-9_]{0,31}"),
    "message": re.compile(r"[A-Za-z][A-Za-z0-9 ,.'():;_/-]{0,159}"),
}


def live_check_error_summary(error: Any) -> str | None:
    """``key=value`` pairs of a live-check error detail, each value re-checked; else None.

    The child already sanitized these (no secrets, paths or bodies); anything that does not
    fit its closed shape here is dropped rather than echoed."""
    if type(error) is not dict:
        return None
    parts = []
    for key, shape in _ERROR_FIELD_SHAPES.items():
        value = error.get(key)
        if type(value) is str and shape.fullmatch(value) and "://" not in value:
            parts.append(f"{key}={value!r}" if key == "message" else f"{key}={value}")
    return " ".join(parts) or None


def live_check_report_error(stdout: bytes | None) -> str | None:
    """The sanitized ``error`` of the live check's last JSON report line (no receipt case)."""
    for line in reversed((stdout or b"")[-_REPORT_SCAN_BYTES:].splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        return live_check_error_summary(report.get("error")) if type(report) is dict else None
    return None


_STDERR_CLASS = re.compile(r"(?:[a-z_][a-z0-9_]*\.)*([A-Z][A-Za-z0-9_]{0,63})(?::.*)?")
LIVE_CHECK_STDERR_UNCLASSIFIED = "stderr_unclassified"


def live_check_stderr_class(stderr: bytes | None) -> str | None:
    """The exception class name the live check's stderr ended with, else a fixed code.

    None for empty stderr. Only a class-name token is ever taken (the last line shaped like
    ``module.ClassName: ...`` or ``ClassName``); its message, other lines and any value are
    dropped, so nothing the child printed is echoed."""
    tail = (stderr or b"")[-_REPORT_SCAN_BYTES:]
    if not tail.strip():
        return None
    for raw in reversed(tail.splitlines()):
        line = raw.decode("ascii", "replace").strip()
        if not line:
            continue
        match = _STDERR_CLASS.fullmatch(line)
        if match:
            return match.group(1)
        break
    return LIVE_CHECK_STDERR_UNCLASSIFIED


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
    stderr_class = live_check_stderr_class(completed.stderr)
    if stderr_class is not None:
        # The child's stderr is otherwise discarded; keep only its closed class so a failed
        # cancel or an unexpected exit is diagnosable without echoing any text.
        _LOG.warning("live check stderr: %s", stderr_class)
    receipt = read_receipt(receipt_dir)
    if receipt is None:
        detail = live_check_exit_detail(completed.stdout)
        error = live_check_report_error(completed.stdout)
        _LOG.warning(
            "live check exited without a receipt: %s%s", detail, f" ({error})" if error else ""
        )
        raise RouteExitedWithoutReceipt(detail)
    # Errors the child held on a running route, and the error behind its own cancel, are on
    # the receipt (sanitized); repeat them here so the journal is never blind to them.
    held = live_check_error_summary(receipt.get("last_held_error"))
    if held is not None:
        _LOG.warning(
            "live check held %s error(s) on a running route; last: %s",
            receipt.get("running_errors_held")
            if type(receipt.get("running_errors_held")) is int
            else "?",
            held,
        )
    cause = receipt.get("cancel_cause")
    ended = live_check_error_summary(cause.get("error")) if type(cause) is dict else None
    if ended is not None:
        _LOG.warning(
            "live check cancel cause %s: %s",
            cause.get("class") if _REPORT_TOKEN.fullmatch(str(cause.get("class"))) else "?",
            ended,
        )
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
# Campaign statuses under which a listed ``route_started`` attempt may be launched. Control
# also lists a blocked or paused campaign's started routes, for keepalive only (v0.104.0);
# an item without the field (an older control) is treated as launchable, as before.
LAUNCH_CAMPAIGN_STATUSES = frozenset({"running", "stopping", "cancel_requested"})
# Campaign statuses under which a listed ``awaiting_lease`` attempt may be leased (ADR 0208
# addendum). ``stopping`` lets outstanding attempts finish (ADR 0038), so its grants are
# leased; a blocked, paused, cancel-requested or finished campaign's are not, and a
# steering-paused one (``steering_paused``) neither: the runner gives such grants back.
LEASE_CAMPAIGN_STATUSES = frozenset({"running", "stopping"})


def lease_held(item: Mapping[str, Any]) -> str | None:
    """Why an ``awaiting_lease`` item must not be leased now, or None. An item without
    ``campaign_status`` (an older control) is leased, as before."""
    status = item.get("campaign_status", "running")
    if status not in LEASE_CAMPAIGN_STATUSES:
        return str(status)[:40]
    if item.get("steering_paused"):
        return "paused"
    return None


# Consecutive failed provider reads after which the termination wait gives up (ADR 0149).
MAX_TERMINATION_OBSERVE_FAILURES = 3


GROUP_STOP_MARKER = "group-stop.json"
# A receipt whose reduce-only cancel was never recorded at control is held (the attempt stays
# executing) until the guardian confirms the exact group stopped and that stop is reported, for
# at most this long; then the outcome is recorded with the failure visible.
CANCEL_UNRECORDED_HOLD_SECONDS = 600.0
GROUP_STOP_MAX_FAILURES = 5
GROUP_STOP_RECHECK_SECONDS = 30.0
# ADR 0212 addendum: a resumed ``executing`` attempt whose live check is gone with no receipt on
# disk is reported to control with the guardian's terminal final (read-only status), once per
# route (a private marker in the route directory). The final must read terminal on two reads at
# least ROUTE_LOST_SETTLE_SECONDS apart, with no receipt on disk at the second, so a live check
# still writing its receipt is never reported lost. Failed reports are bounded per process.
ROUTE_LOST_MARKER = "route-lost.json"
ROUTE_LOST_SETTLE_SECONDS = 120.0
ROUTE_LOST_MAX_FAILURES = 5
# The executor's step breaker (ADR 0212 addendum): an attempt whose pre-launch step (grant poll,
# lease, package) fails this many passes in a row is no longer retried by this executor and is
# reported to control as a typed refusal (``step_breaker_open``), so control unwinds and retries
# it at once. The streak survives a restart (a private file in the work directory), so a crash
# loop on one attempt still trips it.
STEP_BREAKER_THRESHOLD = 5
STEP_BREAKER_FILE = "executor-breaker.json"
_PRE_LAUNCH_STEPS = frozenset({"grant_poll", "lease", "package"})
# A passed receipt whose idempotent finish steps (import, upload, exit report, guardian final)
# failed transiently is not recorded yet: the attempt stays ``executing`` and ``_resume`` retries
# the steps from the receipt on disk, at most FINISH_MAX_DEFERRALS times and within
# FINISH_RETRY_SECONDS of the first deferral (per process), then records with what it has.
FINISH_MARKER = "finish.json"
FINISH_MAX_DEFERRALS = 30
FINISH_RETRY_SECONDS = 1800.0
# Receipt reasons of a run whose exact stop could not be confirmed in the child (M9).
STOP_UNRESOLVED_REASONS = frozenset({"stop_unresolved", "custody_stop_or_reconnect_unresolved"})


def _finish_transient(error: BaseException) -> bool:
    """A failed finish step worth retrying: a timeout, lock, connection or transient guardian or
    provider read. Integrity, binding and refusals are not (they are recorded as before)."""
    import sqlite3

    from .salad_live_check import _tail_transient

    if isinstance(error, (TimeoutError, ConnectionError, sqlite3.OperationalError)):
        return True
    try:
        return _tail_transient(error)
    except Exception:  # noqa: BLE001 - an unclassifiable error is not retried
        return False


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


# While a route's child runs, the executor's own pass is blocked (serial mode): a keepalive
# thread sends the registry heartbeat and renews the leases the pass keeps alive, well inside
# the store's 150 s lease and its 60 s registry liveness bound.
KEEPALIVE_SECONDS = 20
# Phases whose lease a pass keeps alive besides the running route (``_keepalive``/``_resume``).
KEPT_ALIVE_PHASES = LEASE_HELD_PHASES | {"route_started", "executing"}


def _renewal_refused(error: BaseException) -> bool:
    """A renewal control refused (the lease is fenced, foreign or gone): final for this process.
    Anything else (a held write lock, a timeout, a connection error, an unclassified failure) is
    a transient failure: counted and retried at the next renewal. A lease that really lapses
    behind repeated transient failures is refused by control at the next renewal that gets
    through, so retrying never extends authority past its bounds."""
    from .store import Conflict, Forbidden, NotFound

    return isinstance(error, (Conflict, Forbidden, NotFound))


# How long the pass waits for the keepalive thread to stop after a child returns. A beat stuck
# in a lock wait finishes on its own; the pass goes on without it (and records that).
KEEPALIVE_JOIN_SECONDS = 5.0
# Consecutive non-refusal renewal failures of one lease after which the executor stops retrying
# it and fences the attempt as a refusal does (a bug or a dead control would otherwise retry
# every interval until the lease lapses). Counted per lease, reset by any successful renewal.
MAX_CONSECUTIVE_RENEWAL_FAILURES = 5
TRANSIENT_CAP = "TransientRenewalCapExceeded"
# The executor defers a launch whose lease would no longer cover its route window this much
# earlier than control requeues it, so control never unwinds a route the executor just launched.
EXECUTOR_WINDOW_MARGIN_SECONDS = 120
KEEPALIVE_MARKER = "keepalive.json"


class _Keepalive:
    """Registry heartbeat and lease renewal on its own thread while a route's child runs.

    It holds a private copy of the leases to renew and writes only its own record, under its
    lock; the pass reads a snapshot of that record after ``stop`` and applies it
    (``self.fenced``) on its own thread. A refused renewal is recorded once and never retried
    past the fence; a transient failure is counted and retried at the next interval.

    ``own`` is the lease of the route this keepalive runs beside (its child, or its finish). The
    child renews it while it polls, but not in its tail (stop confirmation, custody seal,
    verification) and nobody does while the executor finishes the route (termination confirm,
    guardian final, import, upload), so it is renewed here too, every interval. A refusal of it
    is recorded (``own_refused``) and stops its renewal, but never fences the route here: the
    child and the finish decide from control's own answers, as before. Every renewal stays
    bounded by the lease's hard expiry, its epoch and an active grant."""

    def __init__(
        self,
        control: Any,
        leases: Mapping[str, tuple[str, int]],
        interval: float,
        own: tuple[str, tuple[str, int]] | None = None,
    ) -> None:
        self.control, self.interval = control, interval
        self.leases = dict(leases)
        self.own = own
        self.own_refused: str | None = None
        self.refused: dict[str, str] = {}
        self.transient: dict[str, int] = {}
        self.last_transient: dict[str, str] = {}
        self._streak: dict[str, int] = {}
        self.beats = 0
        self.heartbeat_failures: list[str] = []
        self.join_timed_out = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="salad-keepalive", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.beat()

    def beat(self) -> None:
        try:
            self.control.heartbeat()
            with self._lock:
                self.beats += 1
        except Exception as error:  # noqa: BLE001 - recorded, retried next interval
            with self._lock:
                self.heartbeat_failures.append(type(error).__name__)
        for attempt, lease in list(self.leases.items()):
            if self._stop.is_set():
                return  # stopping: the pass renews what is left itself
            try:
                self.control.renew(*lease)
                with self._lock:
                    self._streak.pop(attempt, None)
            except Exception as error:  # noqa: BLE001 - classified below
                with self._lock:
                    if _renewal_refused(error):
                        self.refused[attempt] = type(error).__name__
                        del self.leases[attempt]
                    else:
                        self.transient[attempt] = self.transient.get(attempt, 0) + 1
                        self.last_transient[attempt] = type(error).__name__
                        streak = self._streak[attempt] = self._streak.get(attempt, 0) + 1
                        if streak >= MAX_CONSECUTIVE_RENEWAL_FAILURES:
                            self.refused[attempt] = TRANSIENT_CAP
                            del self.leases[attempt]
                            _LOG.error(
                                "lease renewal for attempt %s failed %d times in a row "
                                "(last: %s); no longer renewing it",
                                attempt,
                                streak,
                                type(error).__name__,
                            )
        own = self.own
        if own is None or self._stop.is_set():
            return
        try:
            self.control.renew(*own[1])
            with self._lock:
                self._streak.pop(own[0], None)
        except Exception as error:  # noqa: BLE001 - classified below
            with self._lock:
                if _renewal_refused(error):
                    self.own_refused = type(error).__name__
                    self.own = None
                else:
                    self.transient[own[0]] = self.transient.get(own[0], 0) + 1
                    self.last_transient[own[0]] = type(error).__name__
                    streak = self._streak[own[0]] = self._streak.get(own[0], 0) + 1
                    if streak >= MAX_CONSECUTIVE_RENEWAL_FAILURES:
                        self.own_refused, self.own = TRANSIENT_CAP, None

    def snapshot(self) -> dict[str, Any]:
        """A consistent copy of the record (the thread may still be finishing a beat)."""
        with self._lock:
            return {
                "refused": dict(self.refused),
                "own_refused": self.own_refused,
                "transient": dict(self.transient),
                "last_transient": dict(self.last_transient),
                "beats": self.beats,
                "heartbeat_failures": list(self.heartbeat_failures),
                "join_timed_out": self.join_timed_out,
            }

    def __enter__(self) -> _Keepalive:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self.thread.join(KEEPALIVE_JOIN_SECONDS)
        if self.thread.is_alive():
            # The beat still in flight finishes on its own thread after the pass read its
            # snapshot: a refusal it meets then is not applied by this pass. Nothing is lost:
            # the next pass renews that lease itself (``_keepalive``) and fences on the refusal.
            with self._lock:
                self.join_timed_out = True


def keepalive_counts(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The receipt's ``executor_keepalive`` block: counts only (closed names, no identifiers
    beyond the attempts' own), or None when the keepalive saw nothing to report."""
    transient = sum(record["transient"].values())
    failures = record["heartbeat_failures"]
    own = 1 if record.get("own_refused") else 0
    if not (transient or failures or record["refused"] or own or record["join_timed_out"]):
        return None
    return {
        "beats": record["beats"],
        "heartbeat_failures": len(failures),
        "renewal_transient_failures": transient,
        "renewals_refused": len(record["refused"]) + own,
        "join_timed_out": record["join_timed_out"],
    }


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
        keepalive_seconds: float = KEEPALIVE_SECONDS,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sleep = sleep
        self.keepalive_seconds = keepalive_seconds
        # Wall-clock UTC, the clock control judges lease bounds on (never the monotonic one).
        self.wall_clock = wall_clock or (lambda: datetime.now(UTC))
        # This pass's listed work (the keepalive thread renews its held leases while a child
        # runs); read and written by the pass's own thread only.
        self._pass_items: list[dict[str, Any]] = []
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
        # Consecutive transient renewal failures per listed, unfenced attempt (pass keepalive).
        self._renewal_streak: dict[str, int] = {}
        self._step = "grant_poll"  # the control call in flight, named on a recorded refusal
        # Attempts whose live check exited on its own with no receipt, until control has the
        # report (a failed report is re-sent on the next pass of this process only).
        self.no_receipt: set[str] = set()
        # Executing attempts resumed with no receipt and no launch effect, reported once.
        self._no_receipt_resumed: set[str] = set()
        # Whether this pass already sent its pre-lease registry heartbeat.
        self._heartbeat_sent = False
        # Whether a lease keepalive in this pass already heartbeated the registration.
        self._renewal_heartbeat = False
        # ADR 0160: failed group-stop reports per attempt; bounded, in memory only.
        self._group_stop_failures: dict[str, int] = {}
        self._group_stop_given_up: set[str] = set()
        self._group_stop_next: dict[str, float] = {}
        self._cancel_unrecorded_since: dict[str, float] = {}
        # Deferred finishes per attempt: (first deferral clock, deferrals); in memory only.
        self._finish_deferred: dict[str, tuple[float, int]] = {}
        self._final_retryable: dict[str, bool] = {}
        # Attempts whose finish already ran the full provider termination wait in this process:
        # a deferred finish re-reads the route on later passes instead of waiting again.
        self._termination_waited: set[str] = set()
        # This pass's notes only (``run_once`` returns them; the service loop journals them).
        self.log: list[dict[str, Any]] = []
        # Concurrent mode only (``max_concurrent_paid_routes`` above 1): the routes whose child
        # runs now, by attempt. Every field of the executor is read and written by the pass's own
        # thread only; a route's thread runs ``route_runner`` alone and writes only its record.
        self._in_flight: dict[str, _RouteRun] = {}
        self._watched: str | None = None  # the one in-flight attempt the stage watcher follows
        self._concurrency_held = False  # a concurrent route ended without a receipt: drain first
        self._executing_elsewhere: set[str] = set()
        self._pass_frame_limit: Any = _UNREAD
        # Attempts whose last control call was refused, by step: if control then stops listing
        # one, that is journaled once instead of the executor going quiet (v0.104.0).
        self._refused: dict[str, str] = {}
        # Late-importable attempts with no receipt on disk, noted once per process.
        self._late_import_noted: set[str] = set()
        # ADR 0212 addendum: per attempt, the first time the guardian's final read terminal with
        # no receipt on disk, the next time the guardian may be read, and failed reports.
        self._route_lost_seen: dict[str, float] = {}
        self._route_lost_next: dict[str, float] = {}
        self._route_lost_failures: dict[str, int] = {}
        # Typed refusals already delivered to control (``record_refusal``), by attempt.
        self._refusal_reported: dict[str, str] = {}
        # The step breaker's persisted streaks: attempt -> {"step", "count"}.
        self._breaker_path = self.work_directory / STEP_BREAKER_FILE
        self._streaks: dict[str, dict[str, Any]] = self._load_streaks()
        # Health counters for the service health file (``health``): never reset by a pass.
        self.counters: dict[str, Any] = {
            "work_unavailable_total": 0,
            "work_unavailable_consecutive": 0,
            "last_work_error_class": None,
            "item_errors_total": 0,
            "item_errors_last_pass": 0,
            "last_item_error_class": None,
            "refusals_reported_total": 0,
            "refusal_report_failures_total": 0,
            "routes_lost_reported_total": 0,
        }

    # -- one pass ---------------------------------------------------------------------------

    @property
    def concurrent(self) -> bool:
        return self.config.max_concurrent_paid_routes > 1

    def run_once(self) -> list[dict[str, Any]]:
        # Only this pass's notes are kept: a long-running service never accumulates history.
        self.log = []
        self._heartbeat_sent = False
        self._renewal_heartbeat = False
        concurrent = self.concurrent
        if concurrent:
            self._harvest()
        self.counters["item_errors_last_pass"] = 0
        try:
            items = self.control.work()
        except Exception as error:
            # Counted for the health file (a supervisor sees it), never fatal to the loop.
            self.counters["work_unavailable_total"] += 1
            self.counters["work_unavailable_consecutive"] += 1
            self.counters["last_work_error_class"] = type(error).__name__
            self._note("work_unavailable", None, type(error).__name__)
            self._heartbeat_once(None)
            return list(self.log)
        self.counters["work_unavailable_consecutive"] = 0
        try:
            self._pass(items, concurrent)
        finally:
            # Liveness never rides on leases: a healthy executor heartbeats its registration
            # every pass, also when it holds no lease, only fenced ones or is idle (a pass that
            # already heartbeated for a lease or a renewal sends no second one).
            if not self._renewal_heartbeat:
                self._heartbeat_once(None)
        return list(self.log)

    def _pass(self, items: list[dict[str, Any]], concurrent: bool) -> None:
        if concurrent:
            self._pass_frame_limit = _UNREAD
            self._executing_elsewhere = {
                entry["attempt_id"]
                for entry in items
                if entry.get("phase") == "executing" and entry["attempt_id"] not in self._in_flight
            }
        pending: set[str] | None = None
        self._pass_items = items
        listed = {item["attempt_id"] for item in items}
        for attempt in sorted(set(self._refused) - listed - set(self._in_flight)):
            # Control refused this attempt's last call and no longer lists it: say so once,
            # rather than go quiet (v0.104.0).
            self._note("work_withdrawn_after_refusal", attempt, self._refused.pop(attempt))
        for attempt in set(self._renewal_streak) - listed:
            # An attempt control no longer lists has ended: its renewal streak goes with it.
            del self._renewal_streak[attempt]
        if set(self._streaks) - listed - set(self._in_flight):
            # An attempt control no longer lists (unwound, refused, finished) ends its streak.
            for attempt in set(self._streaks) - listed - set(self._in_flight):
                del self._streaks[attempt]
            self._save_streaks()
        self._prune_ended(listed)
        for item in items:
            attempt = item["attempt_id"]
            if concurrent and attempt in self._in_flight:
                # Its child owns the route (and renews the lease and registry heartbeat) until
                # a cancel fences the lease's epoch or the child returns, exactly as in the
                # serial executor; it is finished by ``_harvest`` only, never resumed. The pass
                # also renews its lease (the child sends none in its tail), as the serial
                # child's keepalive does.
                self._renew_running(attempt)
                continue
            if item["phase"] == "awaiting_lease" and self._breaker_open(item):
                continue
            try:
                self._step = "grant_poll"
                if item["phase"] == "awaiting_lease":
                    held = lease_held(item) if attempt not in self._leases else None
                    if held is not None:
                        # Skipped, never failed: no lease is taken, the grant stays for the
                        # runner to give back or for the campaign to run once it may.
                        self._note("lease_held", attempt, held)
                        continue
                    if pending is None:
                        pending = self.control.pending_grants()
                    self._lease_and_package(item, pending)
                elif item["phase"] == "route_started":
                    self._step = "renew"
                    self._keepalive(item)
                    if item.get("campaign_status", "running") not in LAUNCH_CAMPAIGN_STATUSES:
                        # A blocked or paused campaign: kept alive, never launched.
                        self._note("launch_held", attempt, str(item["campaign_status"])[:40])
                        continue
                    self._step = "execute"
                    if concurrent:
                        self._launch(item)
                    else:
                        self._execute(item)
                elif item["phase"] == "executing":
                    self._step = "execute"
                    self._resume(item)
                elif item["phase"] == "unknown":
                    self._step = "execute"
                    self._late_import(item)
                elif item["phase"] in LEASE_HELD_PHASES:
                    self._step = "renew"
                    self._keepalive(item)
                self._refused.pop(attempt, None)
                if attempt in self._streaks:
                    del self._streaks[attempt]
                    self._save_streaks()
            except Exception as error:
                # Class name only: messages can carry identifiers from provider paths.
                self._note("error", attempt, type(error).__name__)
                self.counters["item_errors_total"] += 1
                self.counters["item_errors_last_pass"] += 1
                self.counters["last_item_error_class"] = type(error).__name__
                self._refused[attempt] = self._step
                self._record_rejection(attempt, error)
                self._count_failure(attempt, self._step)

    def _prune_ended(self, listed: set[str]) -> None:
        """Drop per-attempt finish state of attempts that have definitely ended: control no
        longer lists them, no child runs them and no finish of theirs is deferred.

        Only state that never guards a forward step is dropped here. ``_leases`` (an
        ``awaiting_lease`` attempt of a blocked campaign is unlisted until it resumes, and its
        cached lease is the only one it has), ``fenced`` (never launched or renewed past a
        refusal) and ``_no_receipt_resumed`` (a no-receipt marker is reported once) are kept."""
        keep = listed | set(self._in_flight) | set(self._finish_deferred)
        for attempt in set(self._final_retryable) - keep:
            del self._final_retryable[attempt]
        self._termination_waited &= keep
        # A no-receipt report is only ever re-sent for a listed ``executing`` attempt.
        self.no_receipt &= keep

    def _record_rejection(self, attempt_id: str, error: BaseException) -> None:
        """Make a control refusal visible on the stage (a short code); never alters the pass."""
        record = getattr(self.control, "record_rejection", None)
        if record is None:
            return
        try:
            record(attempt_id, self._step, error)
        except Exception as failure:  # noqa: BLE001 - recording must not break the pass
            self._note("rejection_unrecorded", attempt_id, type(failure).__name__)

    # -- refusals, the step breaker and health (ADR 0212 addendum) -------------------------

    def _report_refusal(self, attempt_id: str, code: str, step: str) -> bool:
        """Tell control this executor will not run the attempt (a closed code), once per
        attempt and code per process; control then unwinds it at once instead of waiting out
        the grant. True when control has it. A failure is noted and counted, never fatal."""
        if self._refusal_reported.get(attempt_id) == code:
            return True
        record = getattr(self.control, "record_refusal", None)
        if record is None:
            return False
        try:
            record(attempt_id, code, step)
        except Exception as error:  # noqa: BLE001 - recorded, retried on a later pass
            self.counters["refusal_report_failures_total"] += 1
            self._note("refusal_report_failed", attempt_id, type(error).__name__)
            return False
        self._refusal_reported[attempt_id] = code
        self.counters["refusals_reported_total"] += 1
        self._note("refusal_reported", attempt_id, code)
        return True

    def _load_streaks(self) -> dict[str, dict[str, Any]]:
        """The persisted step streaks; an unreadable file starts empty (noted on first save)."""
        try:
            document = _read_private_json(self._breaker_path)
        except Exception:  # noqa: BLE001 - a bad file never stops the executor
            document = None
        streaks = (document or {}).get("streaks") if type(document) is dict else None
        if type(streaks) is not dict:
            return {}
        return {
            key: {"step": str(value.get("step"))[:20], "count": value["count"]}
            for key, value in streaks.items()
            if type(key) is str
            and _IDENT.fullmatch(key)
            and type(value) is dict
            and type(value.get("count")) is int
            and value["count"] > 0
        }

    def _save_streaks(self) -> None:
        try:
            _write_private_json(self._breaker_path, {"streaks": self._streaks})
        except Exception as error:  # noqa: BLE001 - held in memory; the next save retries
            self._note("breaker_unsaved", None, type(error).__name__)

    def _count_failure(self, attempt_id: str, step: str) -> None:
        """One more consecutive failure of ``attempt_id`` (persisted); a different step starts
        the streak again."""
        streak = self._streaks.get(attempt_id)
        if streak is None or streak.get("step") != step:
            streak = {"step": step, "count": 0}
        streak["count"] += 1
        self._streaks[attempt_id] = streak
        self._save_streaks()

    def _breaker_open(self, item: dict[str, Any]) -> bool:
        """True when the attempt's pre-launch step failed STEP_BREAKER_THRESHOLD passes in a
        row: it is not tried again here, and control is told (``step_breaker_open``)."""
        attempt = item["attempt_id"]
        streak = self._streaks.get(attempt)
        if (
            streak is None
            or streak["count"] < STEP_BREAKER_THRESHOLD
            or streak["step"] not in _PRE_LAUNCH_STEPS
        ):
            return False
        self._note("breaker_open", attempt, f"{streak['step']}:{streak['count']}")
        self._report_refusal(attempt, "step_breaker_open", streak["step"])
        return True

    def health(self) -> dict[str, Any]:
        """Counters for the service health file (``run_service``'s ``health_extra``)."""
        open_steps = [
            value
            for value in self._streaks.values()
            if value["count"] >= STEP_BREAKER_THRESHOLD and value["step"] in _PRE_LAUNCH_STEPS
        ]
        return {
            **self.counters,
            "breaker_open_attempts": len(open_steps),
            "longest_failure_streak": max(
                (value["count"] for value in self._streaks.values()), default=0
            ),
        }

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
            self._report_refusal(attempt, "different_profile", "grant_poll")
            return
        plan = self.control.plan(grant)
        if not 1 <= plan["budget_cents"] <= profile.max_task_cents:
            self._note("refused_budget", attempt, "plan_budget_above_profile")
            self._report_refusal(attempt, "plan_budget_above_profile", "grant_poll")
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
        # The listed phase is stale from here: the attempt now holds a lease, so any keepalive
        # later in this pass (a serial child's, a finish's) must renew it.
        item["phase"] = "leased"
        self._note("packaged", attempt, package["operation_id"])

    def _heartbeat_before_lease(self, attempt_id: str) -> None:
        """At most one registry heartbeat per pass, sent right before asking for a lease.

        The store gives lease authority only to a worker seen within its heartbeat window, so
        the heartbeat goes out before the lease request. A failure is noted (class name only)
        and is not fatal: the lease request still goes out and the store decides.
        """
        self._heartbeat_once(attempt_id)

    def _heartbeat_once(self, attempt_id: str | None) -> None:
        """The pass's registry heartbeat, unless one was already sent in this pass."""
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
            # Only a heartbeat that went through spares the pass-end fallback (F-5).
            self._renewal_heartbeat = True
        except Exception as error:  # noqa: BLE001 - liveness is retried next pass, never a fence
            self._note("heartbeat_failed", attempt, type(error).__name__)
        try:
            self.control.renew(*lease)
            self._renewal_streak.pop(attempt, None)
        except Exception as error:
            if not _renewal_refused(error):
                # A held write lock or a timeout: retried next pass, up to the cap.
                streak = self._renewal_streak[attempt] = self._renewal_streak.get(attempt, 0) + 1
                self._note("lease_renewal_transient", attempt, type(error).__name__)
                if streak < MAX_CONSECUTIVE_RENEWAL_FAILURES:
                    return
                _LOG.error(
                    "lease renewal for attempt %s failed %d passes in a row (last: %s); "
                    "no longer renewing it",
                    attempt,
                    streak,
                    type(error).__name__,
                )
                self.fenced[attempt] = TRANSIENT_CAP
                self._renewal_streak.pop(attempt, None)  # fenced: never renewed again
                self._note("lease_renewal_refused", attempt, TRANSIENT_CAP)
                return
            self._renewal_streak.pop(attempt, None)
            self.fenced[attempt] = type(error).__name__
            self._note("lease_renewal_refused", attempt, type(error).__name__)

    def _renew_running(self, attempt: str) -> None:
        """Renew an in-flight route's own lease from the pass (concurrent mode). A refusal is
        noted, never a fence here: the child and the finish decide from control's answers."""
        lease = self._leases.get(attempt)
        if lease is None or attempt in self.fenced:
            return
        try:
            self.control.renew(*lease)
        except Exception as error:  # noqa: BLE001 - noted; the child keeps its own renewals
            name = type(error).__name__
            if _renewal_refused(error):
                self._note("running_lease_renewal_refused", attempt, name)
            else:
                self._note("lease_renewal_transient", attempt, name)

    def _child_keepalive(self, running: str) -> _Keepalive:
        """The keepalive for one serial child or one finish: every other listed attempt whose
        lease this pass keeps alive (not fenced; a lease taken earlier in this pass included),
        the running route's own lease (``_Keepalive.own``), and the registry heartbeat. Another
        concurrent route's child renews its own lease and is left to it."""
        leases: dict[str, tuple[str, int]] = {}
        found = None if running in self.fenced else self._leases.get(running)
        own = None if found is None else (running, found)
        for item in self._pass_items:
            attempt = item["attempt_id"]
            if attempt == running or attempt in self.fenced or attempt in self._in_flight:
                continue
            if item.get("phase") not in KEPT_ALIVE_PHASES and not (
                # A lease acquired in this pass whose packaging then failed.
                item.get("phase") == "awaiting_lease" and attempt in self._leases
            ):
                continue
            try:
                lease = self._lease_for(item)
            except Exception as error:  # noqa: BLE001 - that attempt is renewed next pass
                self._note("keepalive_lease_unread", attempt, type(error).__name__)
                continue
            if lease is not None:
                leases[attempt] = lease
        return _Keepalive(self.control, leases, self.keepalive_seconds, own)

    @contextmanager
    def _kept_alive(self, running: str):
        """Run the body (a route's finish) with the same keepalive as a serial child: the pass
        thread may block for minutes there (termination confirmation up to
        ``termination_confirm_seconds``, guardian-final retries, import and upload)."""
        keepalive = self._child_keepalive(running)
        try:
            with keepalive:
                yield
        finally:
            self._apply_keepalive(keepalive)

    def _apply_keepalive(self, keepalive: _Keepalive) -> dict[str, Any]:
        """Record, on the pass's thread, what the stopped keepalive thread saw; returns the
        snapshot it applied."""
        record = keepalive.snapshot()
        if record["own_refused"]:
            self._note("running_lease_renewal_refused", None, record["own_refused"])
        for attempt, name in sorted(record["refused"].items()):
            if attempt not in self.fenced:
                self.fenced[attempt] = name
                self._note("lease_renewal_refused", attempt, name)
        for attempt, count in sorted(record["transient"].items()):
            self._note(
                "lease_renewal_transient", attempt, f"{record['last_transient'][attempt]} x{count}"
            )
        if record["heartbeat_failures"]:
            self._note(
                "heartbeat_failed",
                None,
                f"{record['heartbeat_failures'][-1]} x{len(record['heartbeat_failures'])}",
            )
        if record["join_timed_out"]:
            self._note("keepalive_join_timeout", None)
        return record

    def _save_keepalive(self, route_id: str, record: Mapping[str, Any]) -> None:
        """Keep the child's keepalive counts beside its receipt, so every finish of it (now or
        resumed from disk) records the same receipt."""
        counts = keepalive_counts(record)
        if counts is None:
            self._clear_keepalive(route_id)  # never leave an earlier run's counts behind
            return
        try:
            _write_private_json(self._route_dir(route_id) / KEEPALIVE_MARKER, counts)
        except Exception as error:  # noqa: BLE001 - counts are diagnostics only
            self._note("keepalive_counts_unsaved", None, type(error).__name__)

    def _clear_keepalive(self, route_id: str) -> None:
        try:
            (self._route_dir(route_id) / KEEPALIVE_MARKER).unlink(missing_ok=True)
        except Exception as error:  # noqa: BLE001 - diagnostics only
            self._note("keepalive_counts_unsaved", None, type(error).__name__)

    def _with_keepalive(self, route_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        """The receipt with the executor's keepalive counts for its run, when there were any."""
        counts = _read_private_json(self._route_dir(route_id) / KEEPALIVE_MARKER)
        if not counts or "executor_keepalive" in receipt:
            return receipt
        return {**receipt, "executor_keepalive": counts}

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
        keepalive = self._child_keepalive(attempt)
        self._clear_keepalive(item["route_id"])  # a marker here is an earlier run's, not this one's
        try:
            with watch.ticking() if signals is not None else nullcontext(), keepalive:
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
            self._save_keepalive(item["route_id"], self._apply_keepalive(keepalive))
        if receipt is None:
            self._note("no_receipt", attempt)
            self.no_receipt.add(attempt)
            self._report_no_receipt(item)
            return
        with self._kept_alive(attempt):
            self._finish(item, self._with_keepalive(item["route_id"], receipt))

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
        # The run the lease must still cover: the route window, never more than a lease is
        # issued for (the same measure control requeues on; ``campaign_salad_lease_cap``).
        need = needed_seconds(derived.resources.max_seconds, plan.get("max_duration_seconds"))
        if not self._lease_covers_window(item, need):
            return None
        if not self._guardian_has_room(item, attempt):
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

    def _lease_covers_window(self, item: dict[str, Any], window: int) -> bool:
        """False (deferred, nothing marked) when the attempt's lease can no longer cover the
        route window after waiting before the route (``window_shortfall``, with
        ``EXECUTOR_WINDOW_MARGIN_SECONDS`` so the executor defers before control requeues). The
        attempt stays ``route_started``; control unwinds it at 0 and requeues it with a fresh
        lease. An unreadable lease defers too; a control port without the read checks nothing."""
        read = getattr(self.control, "lease_bounds", None)
        attempt = item["attempt_id"]
        lease = self._lease_for(item)
        if read is None or lease is None:
            return True
        try:
            issued, hard = read(lease[0])
            short = window_shortfall(
                issued,
                hard,
                self.wall_clock(),
                window,
                margin=EXECUTOR_WINDOW_MARGIN_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - unreadable: defer, never launch
            self._note("route_deferred", attempt, f"lease_unreadable:{type(error).__name__}")
            return False
        if short is not None:
            self._note("route_deferred", attempt, f"lease_window_short:{short}s")
            return False
        return True

    def _guardian_has_room(self, item: dict[str, Any], attempt: str) -> bool:
        """False (deferred, nothing marked) unless the guardian's own read-only capacity answer
        shows an open-frame slot: ``max_open_billable_frames`` is checked before the attempt is
        committed, so a full fence (an orphan frame still closing) is a wait, never a refused
        reserve that spends a retry. Unreadable defers too. A control port that cannot ask
        (no guardian composed) checks nothing, as before."""
        read = getattr(self.control, "guardian_capacity", None)
        if read is None:
            return True
        try:
            answer = read(item["route_id"])
        except Exception as error:  # noqa: BLE001 - unreadable guardian: defer, never launch
            self._note(
                "route_deferred", attempt, f"guardian_capacity_unreadable:{type(error).__name__}"
            )
            return False
        if answer is None:
            return True
        count, limit = answer
        if count >= limit:
            self._note("route_deferred", attempt, f"guardian_frame_limit:{count}/{limit}")
            return False
        return True

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
                with self._kept_alive(attempt):
                    self._complete(run)
            except Exception as error:
                self._note("error", attempt, type(error).__name__)
                self._refused[attempt] = self._step
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
        self._no_receipt_resumed.add(attempt)  # delivered: a later resume never repeats it
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
            with self._kept_alive(item["attempt_id"]):
                self._finish(item, self._with_keepalive(item["route_id"], receipt))
            return
        if item["attempt_id"] in self.no_receipt:
            self._report_no_receipt(item)
        elif item["attempt_id"] not in self._no_receipt_resumed and self._never_launched(item):
            # Marked executing, but no receipt on disk, no child of this process (a resumed
            # attempt has none) and no create or start effect on the route: report the
            # idempotent no-receipt marker so the runner's unclaimed unwind ends it without a
            # person (M8, ADR 0205). Once per process; the runner's own proof decides.
            self._no_receipt_resumed.add(item["attempt_id"])
            self._note("no_receipt", item["attempt_id"], "resumed_without_launch_effect")
            self._report_no_receipt(item)
        self._report_group_stop(item)
        self._report_route_lost(item)

    def _late_import(self, item: dict[str, Any]) -> None:
        """A late-importable ``unknown`` attempt (the runner called it silent): finish it only
        from a receipt already on disk, by the ordinary finish (v0.104.0). Nothing is launched,
        no lease is renewed and nothing else is reported; control re-verifies the outcome."""
        attempt = item["attempt_id"]
        receipts = self._route_dir(item["route_id"]) / "receipts"
        receipt = read_receipt(receipts) if receipts.is_dir() else None
        if receipt is None:
            if attempt not in self._late_import_noted:
                self._late_import_noted.add(attempt)
                self._note("late_import_no_receipt", attempt)
            self._report_route_lost(item)
            return
        self._note("late_import", attempt)
        self._finish(item, self._with_keepalive(item["route_id"], receipt))

    def _report_route_lost(self, item: dict[str, Any]) -> None:
        """ADR 0212 addendum: the route's live check is gone (no child of this process runs it)
        and no receipt is on disk; once the guardian's read-only status for the route is
        terminal (``stopped`` or ``reconciled``) on two reads ROUTE_LOST_SETTLE_SECONDS apart,
        report it with that final (``record_route_lost``). Once per route: a private marker
        fixes the final so a retry replays the same request. Nothing is launched, nothing about
        effects or spend is asserted, and control alone decides from its own records."""
        attempt, route_id = item["attempt_id"], item["route_id"]
        report = getattr(self.control, "record_route_lost", None)
        if report is None or self._route_lost_failures.get(attempt, 0) >= ROUTE_LOST_MAX_FAILURES:
            return
        directory = self._route_dir(route_id)
        if not directory.is_dir():
            return  # never launched here: this executor cannot speak for the route
        marker = directory / ROUTE_LOST_MARKER
        recorded = _read_private_json(marker)
        if recorded is not None and recorded.get("reported") is True:
            return
        if recorded is None or type(recorded.get("guardian_final")) is not dict:
            now = self.clock()
            if now < self._route_lost_next.get(attempt, 0.0):
                return
            self._route_lost_next[attempt] = now + GROUP_STOP_RECHECK_SECONDS
            try:
                final = self.control.guardian_final(route_id)
            except Exception as error:  # noqa: BLE001 - an unreadable guardian holds
                self._note("route_lost_held", attempt, type(error).__name__)
                return
            status = final.get("status") if type(final) is dict else None
            if status not in ("stopped", "reconciled"):
                self._route_lost_seen.pop(attempt, None)
                self._note("route_lost_held", attempt, f"guardian_{status}"[:40])
                return
            first = self._route_lost_seen.setdefault(attempt, now)
            if now - first < ROUTE_LOST_SETTLE_SECONDS:
                self._note("route_lost_held", attempt, "settling")
                return
            receipts = directory / "receipts"
            if receipts.is_dir() and read_receipt(receipts) is not None:
                return  # a receipt arrived after all: the ordinary finish takes it next pass
            recorded = {"guardian_final": final, "reported": False}
            _write_private_json(marker, recorded)
        try:
            report(attempt, route_id, recorded["guardian_final"])
        except Exception as error:  # noqa: BLE001 - recorded and bounded, never fatal
            self._route_lost_failures[attempt] = self._route_lost_failures.get(attempt, 0) + 1
            self._note("route_lost_report_failed", attempt, type(error).__name__)
            return
        recorded["reported"] = True
        _write_private_json(marker, recorded)
        self._route_lost_failures.pop(attempt, None)
        self.counters["routes_lost_reported_total"] += 1
        self._note("route_lost_reported", attempt, str(recorded["guardian_final"]["status"]))

    def _never_launched(self, item: dict[str, Any]) -> bool:
        """True when the route has no create or start effect and names no provider group (a
        fresh read; fails closed)."""
        try:
            state = self.control.route(item["route_id"])
        except Exception as error:  # noqa: BLE001 - unreadable: assume an effect may exist
            self._note("route_unreadable", item["attempt_id"], type(error).__name__)
            return False
        return type(state) is dict and not state.get("provider_id") and not launch_effects(state)

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

    def _cancel_unrecorded_held(self, item: dict[str, Any], receipt: dict[str, Any]) -> bool:
        """True while a receipt whose cancel control never recorded must wait for the guardian.

        The live check's reduce-only cancel failed (control write lock, refusal), so the route
        may still say ``desired`` run with the group up. The failure is noted every time; the
        guardian's confirmed stop of the exact group is reported as termination evidence
        (ADR 0160) before any outcome, so the attempt never ends ``unknown`` behind a group that
        may still run. Holding ends once that is reported, the provider's own read confirmed the
        stop, the report was given up, or after CANCEL_UNRECORDED_HOLD_SECONDS."""
        attempt = item["attempt_id"]
        failure = receipt.get("cancel_failure")
        exception = failure.get("exception") if type(failure) is dict else None
        self._note("cancel_unrecorded", attempt, str(exception or "")[:64])
        if receipt.get("provider_stop_confirmed") is True or self._never_launched(item):
            # A route with no create or start effect has no group to wait for (M10): the
            # outcome is recorded now and the runner unwinds it on control's own records.
            self._cancel_unrecorded_since.pop(attempt, None)
            return False
        try:
            self._report_group_stop(item)
        except Exception as error:  # noqa: BLE001 - a failed check holds, bounded below
            self._note("group_stop_held", attempt, type(error).__name__)
        marker = _read_private_json(self._route_dir(item["route_id"]) / GROUP_STOP_MARKER)
        reported = marker is not None and marker.get("reported") is True
        since = self._cancel_unrecorded_since.setdefault(attempt, self.clock())
        if (
            reported
            or attempt in self._group_stop_given_up
            or self.clock() - since >= CANCEL_UNRECORDED_HOLD_SECONDS
        ):
            self._cancel_unrecorded_since.pop(attempt, None)
            return False
        self._note("cancel_unrecorded_held", attempt)
        return True

    def _finish(self, item: dict[str, Any], receipt: dict[str, Any]) -> None:
        attempt, route_id = item["attempt_id"], item["route_id"]
        if receipt.get("cancel_recorded") is False and self._cancel_unrecorded_held(item, receipt):
            return
        # A passed receipt is never recorded behind a transient finish failure (H6, H11): the
        # failed steps are retried from the receipt on disk (``_resume``), bounded.
        passed = receipt.get("status") == "passed"
        deferred: list[str] = []
        state = self.control.route(route_id)
        provider_id = state.get("provider_id")
        lease = self._lease_for(item)
        waiting = provider_termination(state)
        if (
            waiting.status == "unconfirmed"
            and waiting.reason != "no_stop_requested"
            and attempt not in self._termination_waited
        ):
            # The full wait runs once per attempt in this process; a deferred finish's later
            # passes use the route just read (its confirmation, if any, came in since).
            self._termination_waited.add(attempt)
            state = self._confirm_termination(attempt, route_id, self._profile_for(item))
        if not passed and self._unconfirmed_stop_held(item, receipt, state):
            return
        guardian_final: dict[str, Any] | None = None
        if not passed:
            # A failed run's final is read before any import, so a deferred pass repeats no
            # import or upload. The guardian records the container-log tail of a non-zero exit
            # on a later loop pass than the exit itself (salad_exit_watch): while the final names
            # that exit and has no tail yet, the outcome waits (bounded by ``_defer_finish``,
            # the hold stays held), then is recorded with what the guardian has.
            guardian_final = self._read_guardian_final(attempt, route_id)
            if _container_log_pending(guardian_final) and self._defer_finish(
                attempt, ["guardian_final"]
            ):
                return
        marker_path = self._route_dir(route_id) / FINISH_MARKER
        progress = (_read_private_json(marker_path) or {}) if passed else {}
        manifest_id = (
            progress.get("manifest_id") if type(progress.get("manifest_id")) is str else None
        )
        # An attempt the provider moved after custody began has no diagnostic and maybe no other
        # declared file. It gets a partial manifest of what reached custody (each file
        # hash-verified); it never counts as a pass.
        partial = _interrupted_after_custody(receipt)
        if partial and lease is not None:
            # Control admits a partial manifest only against its own record of the guardian's
            # custody status: write it first; without it nothing partial is imported.
            try:
                self.control.record_custody_evidence(attempt, route_id)
            except Exception as error:
                self._note("custody_evidence_failed", attempt, type(error).__name__)
                partial = False
        if (
            manifest_id is None
            and (receipt.get("stop_confirmed") is True or partial)
            and lease is not None
        ):
            try:
                manifest_id = self._import_and_upload(item, state, lease, partial=partial)
                if passed:
                    progress["manifest_id"] = manifest_id
                    self._save_progress(marker_path, progress)
            except Exception as error:
                self._note("import_failed", attempt, type(error).__name__)
                if passed and _finish_transient(error):
                    deferred.append("import")
        if (
            lease is not None
            and provider_id
            and state.get("termination_confirmed") is True
            and not state.get("reallocated")
            and progress.get("exited_reported") is not True
        ):
            observed_at = _last_provider_observation(state)
            if observed_at is not None:
                try:
                    self.control.report_exited(
                        lease[0], lease[1], item["operation_id"], provider_id, observed_at
                    )
                    if passed:
                        progress["exited_reported"] = True
                        self._save_progress(marker_path, progress)
                except Exception as error:
                    self._note("observation_failed", attempt, type(error).__name__)
                    if passed and _finish_transient(error):
                        deferred.append("exit_report")
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
        if passed:
            guardian_final = self._read_guardian_final(attempt, route_id)
        if passed and self._final_retryable.pop(attempt, False):
            # The read failed, or the guardian has not reached ``stopped`` yet: read it again
            # on a later pass rather than record a stale snapshot (H11). No guardian, or a status
            # that cannot be projected, is not retried.
            deferred.append("guardian_final")
        if deferred and self._defer_finish(attempt, deferred):
            return
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
        self._finish_deferred.pop(attempt, None)
        self._termination_waited.discard(attempt)
        self._final_retryable.pop(attempt, None)
        self._note("outcome_recorded", attempt, "manifest" if manifest_id else "no_manifest")

    @staticmethod
    def _save_progress(path: Path, progress: dict[str, Any]) -> None:
        """Keep which idempotent finish steps already succeeded (best effort: every step is
        idempotent at control, the marker only saves repeating a full import)."""
        try:
            if path.parent.is_dir():
                _write_private_json(path, progress)
        except Exception:  # noqa: BLE001 - a lost marker only repeats an idempotent step
            pass

    def _defer_finish(self, attempt: str, steps: list[str]) -> bool:
        """True when the outcome waits for another pass (bounded); False once the bound ran out,
        then the outcome is recorded with what the finish has, and that is noted."""
        now = self.clock()
        first, count = self._finish_deferred.get(attempt, (now, 0))
        count += 1
        detail = ",".join(steps)
        if count > FINISH_MAX_DEFERRALS or now - first >= FINISH_RETRY_SECONDS:
            self._finish_deferred.pop(attempt, None)
            self._note("finish_retries_exhausted", attempt, detail)
            return False
        self._finish_deferred[attempt] = (first, count)
        self._note("finish_deferred", attempt, detail)
        return True

    def _unconfirmed_stop_held(
        self, item: dict[str, Any], receipt: dict[str, Any], state: dict[str, Any]
    ) -> bool:
        """True while a non-passed receipt whose exact stop the child could not confirm (M9: a
        group created, maybe never started, and one stop read failed) waits for the guardian's
        confirmed stop of the exact group, reported as termination evidence (ADR 0160).

        Bounded like a deferred finish; never for a stop the receipt or control already
        confirmed, nor for a route with no create effect (there is no group to wait for)."""
        attempt = item["attempt_id"]
        if (
            receipt.get("reason") not in STOP_UNRESOLVED_REASONS
            or receipt.get("stop_confirmed") is True
            or receipt.get("provider_stop_confirmed") is True
            or state.get("termination_confirmed") is True
            or provider_termination(state).confirmed
            or not any(effect.get("action") == "create" for effect in state.get("effects", []))
            or (not state.get("provider_id") and not sent_launch_effects(state))
        ):
            # The last: no group identity was ever recorded and every create/start was refused
            # at the trusted server's use-time admission (ADR 0205 amendment), so there is no
            # group for the guardian to confirm stopped; the runner's own proof decides.
            return False
        try:
            self._report_group_stop(item)
        except Exception as error:  # noqa: BLE001 - a failed check holds, bounded below
            self._note("group_stop_held", attempt, type(error).__name__)
        marker = _read_private_json(self._route_dir(item["route_id"]) / GROUP_STOP_MARKER)
        if (marker is not None and marker.get("reported") is True) or (
            attempt in self._group_stop_given_up
        ):
            return False
        return self._defer_finish(attempt, ["group_stop"])

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
        failed = False
        for index in range(1 + self.config.guardian_retries):
            failed = raised = False
            try:
                final = self.control.guardian_final(route_id)
            except Exception as error:
                final, reason, raised = None, type(error).__name__, True
                failed = _finish_transient(error)
            if final is not None and final.get("status") == "stopped":
                return final
            if final is None and not raised:
                # No guardian is configured (or nothing projectable): re-reading cannot help.
                break
            if index < self.config.guardian_retries:
                self.sleep(self.config.guardian_retry_seconds)
        # Worth another read later: the read failed transiently, or the final is not
        # ``stopped`` yet.
        self._final_retryable[attempt] = failed or final is not None
        if final is None:
            self._note("guardian_final_unavailable", attempt, reason)
        else:
            self._note("guardian_not_stopped", attempt, str(final.get("status"))[:40])
        return final

    def _import_and_upload(self, item, state, lease, partial=False) -> str:
        profile = self._profile_for(item)
        if profile is None:
            raise ValueError("the attempt's profile is not served here")
        directory = self._route_dir(item["route_id"]) / "outputs"
        if partial:
            files = self.importer(item["route_id"], directory, profile, partial=True)
        else:
            files = self.importer(item["route_id"], directory, profile)
        for entry in files:
            data = (directory / entry["name"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["bytes"]:
                raise ValueError("imported file differs from its manifest entry")
        if partial:
            manifest = self.control.upload_outputs(
                lease[0], lease[1], item["operation_id"], directory, files, partial=True
            )
        else:
            manifest = self.control.upload_outputs(
                lease[0], lease[1], item["operation_id"], directory, files
            )
        return manifest["id"]


def _container_log_pending(final: Mapping[str, Any] | None) -> bool:
    """True when the guardian's ``stopped`` final names a workload non-zero exit but carries no
    container-log tail (the guardian's one log read has not been recorded yet)."""
    return (
        final is not None
        and final.get("status") == "stopped"
        and bool(nonzero_exit_codes(final.get("start_events")))
        and final.get("container_log") is None
    )


def _interrupted_after_custody(receipt: Mapping[str, Any]) -> bool:
    """The live receipt's own word that the guardian stopped on an allocation change after
    custody work began, with the provider's stop confirmed (the evidence the campaign driver
    classifies as a retryable interruption)."""
    return (
        receipt.get("guardian_stop_cause") == "allocation_changed"
        and receipt.get("custody_work_begun") is True
        and receipt.get("provider_stop_confirmed") is True
    )


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
        guardian_open_frames: Callable[[dict[str, Any]], tuple[int, int]] | None = None,
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
        self.guardian_open_frames = guardian_open_frames
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

    def upload_outputs(self, lease_id, epoch, operation_id, directory, files, partial=False):
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
        if partial:
            payload["partial"] = True
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

    def guardian_capacity(self, route_id):
        """``(open frames, max_open_billable_frames)`` from the guardian's read-only answer, or
        None when no guardian client is composed. A failed or malformed read raises."""
        if self.guardian_open_frames is None:
            return None
        return self.guardian_open_frames(self.routes.get(self.worker, route_id))

    def lease_bounds(self, lease_id):
        """``(issued_at, hard_expires_at)`` of this worker's own lease (a read)."""
        from .store import _parse_utc

        rows = self._read(
            "SELECT issued_at, hard_expires_at FROM execution_leases "
            "WHERE id = ? AND worker_principal_id = ?",
            (lease_id, self.worker["id"]),
        )
        if not rows:
            raise LookupError("lease unavailable")
        return _parse_utc(rows[0]["issued_at"]), _parse_utc(rows[0]["hard_expires_at"])

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

    def record_route_lost(self, attempt_id, route_id, guardian_final):
        """ADR 0212 addendum: the live check is gone with no receipt; the guardian's terminal
        final for the route, as the executor read it."""
        self.campaigns.record_salad_route_lost(
            self.worker,
            attempt_id,
            _key("route-lost", attempt_id),
            {"route_id": route_id, "guardian_final": guardian_final},
        )

    def record_refusal(self, attempt_id, code, step):
        """ADR 0212 addendum: this executor will not run the attempt (a closed code)."""
        self.campaigns.record_salad_refusal(
            self.worker,
            attempt_id,
            _key(f"refusal-{code}", attempt_id),
            {"code": code, "step": step},
        )

    def record_custody_evidence(self, attempt_id, route_id):
        """Record the guardian's own custody status for the route (read over the authenticated
        admin channel) before a partial manifest is uploaded. Raises when it cannot be read."""
        if self.guardian_status is None:
            raise ValueError("no guardian status client")
        status = self.guardian_status(self.routes.get(self.worker, route_id))
        began, cause = status.get("custody_work_begun"), status.get("stop_cause")
        if type(began) is not bool or (cause is not None and type(cause) is not str):
            raise ValueError("guardian status lacks custody evidence")
        self.campaigns.record_salad_custody_evidence(
            self.worker,
            attempt_id,
            _key("custody-evidence", attempt_id),
            {"route_id": route_id, "custody_work_begun": began, "stop_cause": cause},
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


# Decimal places the recorded guardian final keeps (control's ``_CentsText`` shape).
GUARDIAN_TEXT_PLACES = 12
_LONG_DECIMAL = re.compile(r"[0-9]{1,12}\.[0-9]{13,}")


def _cents_text(value: Any) -> str | None:
    """``_text``, with a longer decimal fraction rounded UP to GUARDIAN_TEXT_PLACES places.

    The guardian accrues with exact decimal division (rate x seconds x 100 / 3600), so its
    estimate often carries 28 significant digits; recorded as is, it fails control's 12-place
    shape and the whole final was dropped as unprojectable. Rounding up never under-states a
    cost or a rate; anything else is passed through for validation unchanged."""
    text = _text(value)
    if text is None or not _LONG_DECIMAL.fullmatch(text):
        return text
    try:
        number = Decimal(text).quantize(Decimal(1).scaleb(-GUARDIAN_TEXT_PLACES), ROUND_CEILING)
    except InvalidOperation:
        return text
    return format(number, "f")


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
        published_hourly_rate_usd=_cents_text(status.get("published_hourly_rate_usd")),
        estimated_exposure_cents=_cents_text(status.get("estimated_exposure_cents")),
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
        if cut.get("custody_work_begun") is False:
            # Additive: the workload never began custody work before the route limit cut it.
            projected["route_deadline"]["custody_work_begun"] = False
    if (
        status.get("never_allocated") is True
        and status.get("status") in {"stopped", "reconciled"}
        and status.get("stop_cause") in {None, "deadline"}
        and "allocation_timeout" not in projected
        and "route_deadline" not in projected
    ):
        # Additive (ADR 0225): the guardian's own word that no machine was ever placed, seen
        # running or served by custody before the frame closed.
        projected["never_allocated"] = True
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
    from .salad_guardian import RECONCILE_PROVIDER

    if (
        status.get("status") == "reconciled"
        and status.get("reason") == RECONCILE_PROVIDER
        and status.get("allocation_uncertain") is True
        and status.get("output_retained") is True
    ):
        # Additive: the run retained its output first, then the provider's allocation changed
        # while the group was stopping, so the guardian could only close the frame by the
        # provider-confirmed reconcile (custody unsealed, outputs never accepted).
        projected["retained_before_move"] = True
    if status.get("stop_cause") in SAFETY_STOP_CAUSES:
        # Additive: the guardian's first stop cause, only when the Salad safety plane stopped the
        # route for its own health (closed table); control classifies from this record alone.
        projected["safety_stop_cause"] = status["stop_cause"]
    from .salad_container_log import closed_container_log

    container_log = closed_container_log(status.get("container_log"))
    if container_log is not None:
        projected["container_log"] = container_log
    try:
        _GuardianFinal.model_validate(projected)
    except ValidationError:
        return None  # an unrecordable status must not lose the outcome
    return projected


def trusted_importer(config: ExecutorConfig, worker, state_dir):
    """Importer over a TrustedSaladServer using the independent custody HTTPS client."""

    def import_route(
        route_id: str, destination: Path, profile: SaladCampaignProfile, partial: bool = False
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
            if partial:
                reply = server.import_retained(route_id, destination=str(destination), partial=True)
            else:
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


def check_profiles(config: ExecutorConfig) -> list[Any]:
    """The pure part of ``build_executor``: the named profiles exist and match the custody name.

    Reads the profiles file only; shared with ``--check-config``. Refuses with
    ``ExecutorConfigRefused``."""
    try:
        loaded = load_profiles(config.profiles_file)
    except ValueError as error:
        raise ExecutorConfigRefused(str(error)) from None
    profiles = [loaded.get(name) for name in config.profile_names]
    if any(profile is None for profile in profiles):
        raise ExecutorConfigRefused("Unknown Salad campaign profile.")
    # The executor may only hand workloads a custody name it owns: the name it was configured to
    # advertise (``workload_host``, else ``host``) must be the profile's ``custody_host``. The
    # admin ``host`` it connects to may differ.
    if any(config.custody.workload_name() != profile.custody_host for profile in profiles):
        raise ExecutorConfigRefused("Custody host differs from the profile.")
    return profiles


def _authenticate_executor_worker(config: ExecutorConfig, state_dir: Path) -> dict[str, Any]:
    from .salad_live_check import _private_bytes
    from .store import Forbidden, Store

    try:
        token = _private_bytes(config.worker_credential_file, 4096).decode("ascii").strip()
    except ValueError:  # an empty, oversized or non-ASCII credential file: the operator's file
        raise ExecutorConfigRefused("Worker credential file is unusable.") from None
    try:
        return Store(state_dir).authenticate_worker(token)
    except Forbidden:
        # The database answered and does not accept this credential (unknown, revoked, expired or
        # not a worker credential): the operator's file, which a restart does not change.
        raise ExecutorConfigRefused("Worker credential is not accepted.") from None


def build_executor(config: ExecutorConfig) -> SaladCampaignExecutor:
    profiles = check_profiles(config)
    state_dir = Path(config.state_dir)
    worker = _authenticate_executor_worker(config, state_dir)

    def guardian_status(state):
        from .salad_server import SaladSafetyHTTPSClient

        return SaladSafetyHTTPSClient(**config.custody.client_fields()).status(state)

    def guardian_open_frames(state):
        from .salad_server import SaladSafetyHTTPSClient

        return SaladSafetyHTTPSClient(**config.custody.client_fields()).open_frames(state)

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
        StoreControl(state_dir, worker, guardian_status, guardian_open_frames),
        worker_principal_id=worker["id"],
        importer=trusted_importer(config, worker, state_dir),
        stage_watch=stage_watch,
        observer=trusted_observer(config, worker, state_dir),
    )


def _print_closed(payload: dict[str, str]) -> None:
    print(json.dumps(payload))


def _check_config(options) -> int:
    """``--check-config``: the configuration checks of a real start and nothing effectful.

    No maintenance lock, no control database, no credential read, nothing started."""
    try:
        config = load_config(options.config, allow_stale_pin=True)
        notes = []
        if source_pin_is_stale(config):
            notes.append("source_revision is stale for this release; deploy-upgrade re-pins it")
        check_profiles(config)
    except ValueError as error:
        print(f"salad executor refused: {error}", file=sys.stderr)
        _print_closed({"status": "refused", "reason": "executor_config_refused"})
        return exit_codes.CONFIG_REFUSED
    except Exception as error:
        _print_closed(
            {
                "status": "unavailable",
                "reason": "executor_check_failed",
                "error_class": type(error).__name__,
            }
        )
        return exit_codes.RUNTIME_FAILURE
    print(
        json.dumps(
            {
                "schema": exit_codes.CONFIG_CHECK_SCHEMA,
                "program": "salad-executor",
                "status": "ok",
                "widths": {"max_concurrent_paid_routes": config.max_concurrent_paid_routes},
                "notes": notes,
            }
        )
    )
    return exit_codes.OK


def main(argv=None) -> int:
    from tessera_worker.service_loop import StopFlag, run_service

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--health-file", type=Path, default=None)
    parser.add_argument("--check-config", action="store_true")
    options = parser.parse_args(argv)
    if options.check_config:
        return _check_config(options)
    # Refusal of the operator's configuration (exit 2, not restarted) is decided by which step
    # raised, not by the exception type: ``load_config`` refuses with ``ValueError`` and
    # ``build_executor``'s configuration checks with ``ExecutorConfigRefused``; every other fault
    # in the effectful steps below exits 1 (restarted).
    try:
        config = load_config(options.config)
    except ValueError as error:
        print(f"salad executor refused: {error}", file=sys.stderr)
        _print_closed({"status": "refused", "reason": "executor_config_refused"})
        return exit_codes.CONFIG_REFUSED
    except Exception as error:
        return _setup_failed(error)
    try:
        # Before anything opens the control database: `tessera db compact` refuses while this
        # shared lock is held, and a running compaction makes this start refuse (exit 1).
        maintenance_lock.hold_shared_for_process(config.state_dir)
        executor = build_executor(config)
    except maintenance_lock.MaintenanceBusy as error:
        print(f"salad executor refused: {error}", file=sys.stderr)
        return exit_codes.RUNTIME_FAILURE
    except ExecutorConfigRefused as error:
        print(f"salad executor refused: {error}", file=sys.stderr)
        _print_closed({"status": "refused", "reason": "executor_config_refused"})
        return exit_codes.CONFIG_REFUSED
    except Exception as error:
        return _setup_failed(error)
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
        health_extra=executor.health,
    )


def _setup_failed(error: BaseException) -> int:
    """A setup fault that is not a refusal of the operator's configuration: restart it."""
    _print_closed(
        {
            "status": "unavailable",
            "reason": "executor_setup_failed",
            "error_class": type(error).__name__,
        }
    )
    return exit_codes.RUNTIME_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
