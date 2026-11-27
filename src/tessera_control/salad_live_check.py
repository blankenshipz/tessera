"""Opt-in bounded journey driver; default CLI performs no reads or network calls.

Deployment composition is supplied by the trusted server, not by client booleans.
No direct provider mutation, credential discovery, retries or paid-effect defaults.
"""

import argparse
import hashlib
import io
import json
import math
import os
import re
import signal
import stat
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .salad_events import closed_record
from .salad_guardian import START_FAILURE_REASON
from .salad_running_latch import RunningLatch
from .salad_workload_contract import WORKLOAD_FAILED_CAUSE_PREFIX, WORKLOAD_FAILURE_REASONS

# Route window from create through output and stop.
# GPU node allocation plus a multi-GB CUDA image pull exceeds 5 minutes. Polls stay
# <=10s apart for guardian heartbeats, so the poll cap scales with the window.
# 45 min unbilled provisioning + up to 60 min billed workload + upload grace.
# How long a group that reads stopped may take to read settled (no change in flight, no instance)
# before start is attempted anyway and the transport's own exact read decides.
PRESTART_SETTLE_SECONDS = 45
# The guardian stopped on a reallocation before any custody work: the provider moved an instance
# that had produced and received nothing, so a fresh attempt may retry it (ADR 0179).
PRECUSTODY_REALLOCATION = "provider_reallocated_before_custody"
MAX_ROUTE_SECONDS = 7200
MAX_POLLS = 720


# Closed set of pre-start failure classes recorded on the receipt (field `prestart_failure`).
# Each names the step that failed before dispatch("start"); the receipt status and reason are
# unchanged by it. `prestart_unclassified` is the named fallback for a failure at a step that
# cannot be attributed more precisely; it is never silently merged into another class.
PRESTART_FAILURE_CLASSES = frozenset(
    {
        "route_state_unreadable",  # reading the durable route / input declaration
        "create_failed",  # dispatch("create") raised
        "group_unsettled",  # pre-start settle wait: observe/heartbeat raised or group not stopped
        "ticket_mint_failed",  # the server could not mint or hand the input ticket to the guardian
        "input_delivery_unconfirmed",  # guardian status did not confirm the held ticket/digest
        "start_failed",  # dispatch("start") raised
        "wall_bound_exceeded",  # a bounded live operation timed out before start
        "prestart_unclassified",  # any other failure that carries no closed class
    }
)


PRESTART_STEPS = (
    "create",
    "bound",
    "settle",
    "deliver_inputs",
    "start_preflight",
    "start",
)


def _wall_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class PrestartStepError(Exception):
    """Carries a closed failure class out of a session step; never carries message text."""

    def __init__(self, failure_class):
        super().__init__(failure_class)
        self.failure_class = failure_class


def _exception_type_name(error):
    """The exception's class name only (bounded, identifier-shaped); never its text."""
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) else "Exception"


def _closed_token(value):
    if value is None:
        return None
    return (
        value
        if type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
        else "invalid_code"
    )


# Closed names of the in-process control (Store / API) calls a live-check refusal is attributed
# to (receipt ``control_refusal.call``). The session tags the error with the innermost one.
CONTROL_CALLS = frozenset(
    {
        "Store.get",
        "Store.preflight_authority",
        "Store.renew_execution_lease",
        "WorkerRegistryStore.heartbeat",
        "TrustedSaladServer.heartbeat",
        "RemoteDispatcher.dispatch",
        "Store.execution_authority",
    }
)


@contextmanager
def _calling(name):
    """Tag an error raised inside with the control call it came from (innermost tag wins)."""
    try:
        yield
    except Exception as error:
        if getattr(error, "live_check_call", None) is None:
            try:
                error.live_check_call = name
            except Exception:
                pass
        raise


# Closed reasons a refused pre-start dispatch/observe (a store Conflict) carries on the receipt.
CONFLICT_REASONS = frozenset({"route_revision_changed", "provider_read_unavailable", "conflict"})
# Only these are retried (once): the route moved under the read; the next read sees the new state.
TRANSIENT_CONFLICT_REASONS = frozenset({"route_revision_changed"})


def conflict_reason(error):
    """Closed reason of a store Conflict, or None for any other error (never its text)."""
    from .remote_execution import RouteRevisionChanged
    from .salad_server import ProviderReadUnavailable
    from .store import Conflict

    if not isinstance(error, Conflict):
        return None
    if isinstance(error, RouteRevisionChanged):
        return "route_revision_changed"
    if isinstance(error, ProviderReadUnavailable):
        return "provider_read_unavailable"
    return "conflict"


def _prestart_failure(error, default):
    if isinstance(error, TimeoutError):
        failure_class = "wall_bound_exceeded"
    elif isinstance(error, PrestartStepError) and error.failure_class in PRESTART_FAILURE_CLASSES:
        failure_class = error.failure_class
    else:
        failure_class = default if default in PRESTART_FAILURE_CLASSES else "prestart_unclassified"
    cause = error.__cause__ if isinstance(error, PrestartStepError) else None
    result = {
        "class": failure_class,
        "exception": _exception_type_name(cause if cause is not None else error),
    }
    source = cause if cause is not None else error
    reason = conflict_reason(source)
    if reason is not None:
        result["reason"] = reason
    failure = getattr(source, "failure", None)
    if failure is not None:
        # A refused provider read (start preflight): the transport's closed diagnostics.
        result["provider_read"] = {
            "reason": _closed_token(failure),
            "detail": _closed_token(getattr(source, "detail", None)),
            "field": _field(getattr(source, "field", None)),
            "request_kind": _closed_token(getattr(source, "request_kind", None)),
        }
    return result


@contextmanager
def _wall_bound(seconds):
    # Standalone trusted Linux CLI, not an API thread or an existing timer owner.
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getitimer(signal.ITIMER_REAL)[0]
    ):
        raise ValueError("isolated main-thread live driver required")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(*_):
        raise TimeoutError("bounded live operation expired")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _private_path(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("explicit absolute private path required")
    # Descriptor walking rejects symlink ancestors as well as the final entry.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
        result = os.open(path.name, flags, dir_fd=fd)
        info = os.fstat(result)
        if (
            info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)
        ):
            os.close(result)
            raise ValueError("private path unavailable")
        return result
    finally:
        os.close(fd)


def _private_bytes(path, cap):
    fd = _private_path(path)
    try:
        if not 0 < os.fstat(fd).st_size <= cap:
            raise ValueError("private file bound")
        data = os.read(fd, cap + 1)
        if not 0 < len(data) <= cap:
            raise ValueError("private file bound")
        return data
    finally:
        os.close(fd)


def _config(path):
    from .salad_proxy_contract import ProxyPolicy
    from .salad_workload_jwt import strict_json

    raw = _private_bytes(path, 65536)
    value = strict_json(raw)
    keys = {
        "schema",
        "state_dir",
        "route_id",
        "task_id",
        "org_id",
        "project_id",
        "proposal_digest",
        "policy",
        "worker_credential_file",
        "credential_directory",
        "guardian_socket",
        "guardian_uid",
        "custody",
        "approved_max_cost_cents",
        "max_seconds",
        "max_polls",
        "source_revision",
    }
    if (
        type(value) is not dict
        or set(value) - {WATCH_SIGNALS_KEY, ROUTE_WINDOW_KEY} != keys
        or value["schema"] != "tessera.salad.live-config.v1"
    ):
        raise ValueError("closed server config required")
    for key in ("route_id", "task_id", "org_id", "project_id"):
        if type(value[key]) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", value[key]):
            raise ValueError("exact identifiers required")
    if not re.fullmatch(r"[a-f0-9]{64}", value["proposal_digest"]):
        raise ValueError("exact route digest required")
    for key, lower, upper in (
        ("approved_max_cost_cents", 1, 10000),
        ("max_seconds", 1, MAX_ROUTE_SECONDS),
        ("max_polls", 1, MAX_POLLS),
        ("guardian_uid", 0, 2**31 - 1),
    ):
        if type(value[key]) is not int or not lower <= value[key] <= upper:
            raise ValueError("bounded server config required")
    if not re.fullmatch(r"[a-f0-9]{40}(?:\+sha256:[a-f0-9]{64})?", value["source_revision"]):
        raise ValueError("source revision required")
    policy = ProxyPolicy.model_validate(value["policy"])
    if (policy.org_id, policy.project_id) != (value["org_id"], value["project_id"]):
        raise ValueError("exact server scope required")
    custody = value["custody"]
    if type(custody) is not dict or set(custody) != {
        "host",
        "port",
        "token_file",
        "ca_file",
        "certificate_sha256",
    }:
        raise ValueError("closed custody config required")
    if (
        type(custody["host"]) is not str
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", custody["host"])
        or type(custody["port"]) is not int
        or custody["port"] != 443
        or not re.fullmatch(r"[a-f0-9]{64}", custody["certificate_sha256"])
    ):
        raise ValueError("fixed authenticated custody target required")
    for key in ("state_dir", "credential_directory"):
        os.close(_private_path(value[key], directory=True))
    for path_value in (
        value["worker_credential_file"],
        custody["token_file"],
        custody["ca_file"],
        str(Path(value["state_dir"]) / "tessera.sqlite3"),
    ):
        os.close(_private_path(path_value))
    socket_path = Path(value["guardian_socket"])
    if not socket_path.is_absolute() or ".." in socket_path.parts:
        raise ValueError("explicit guardian socket required")
    if ROUTE_WINDOW_KEY in value:
        window = value[ROUTE_WINDOW_KEY]
        if (
            type(window) is not dict
            or set(window) != {"seconds", "provisioning_allowance_seconds", "source"}
            or type(window["seconds"]) is not int
            or type(window["provisioning_allowance_seconds"]) is not int
            or window["source"] not in {"profile_route_max_seconds", "plan_bound"}
        ):
            raise ValueError("closed route window required")
    if WATCH_SIGNALS_KEY in value:
        _watch_signals_path(value[WATCH_SIGNALS_KEY])
    return value, policy, hashlib.sha256(raw).hexdigest()


# Optional, off when absent (ADR 0111): where the stage watcher reads this route's
# non-secret signal snapshot. The file need not exist yet; its directory must be private.
WATCH_SIGNALS_KEY = "watch_signals_file"
# Optional closed record of the approved route window (seconds, provisioning allowance, source),
# shown on the receipt so a reviewer reads the window the run really had.
ROUTE_WINDOW_KEY = "route_window"


def _watch_signals_path(value):
    if (
        type(value) is not str
        or not 0 < len(value) <= 4096
        or "\x00" in value
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.json", Path(value).name)
    ):
        raise ValueError("private watch signals file required")
    path = Path(value)
    os.close(_private_path(path.parent, directory=True))
    return path


def _observe_watch(path, *, state, evidence, polls):
    """Write one stage-watch signal snapshot after a successful poll; never raises.

    Read-only and spend-neutral: it only maps values this loop already holds
    (``signals_from_poll``: no identifier, name, secret or money) and writes one private
    file. Any failure is swallowed so it can never change the run or its receipt.
    """
    if path is None:
        return
    try:
        from datetime import UTC, datetime

        from tessera_worker.stage_watcher.signals import write_signal_snapshot

        from .salad_watch_signals import signals_from_poll

        now = datetime.now(UTC)
        signals = signals_from_poll(
            provider_status=state.get("provider_status"),
            termination_confirmed=state.get("termination_confirmed") is True,
            stop_confirmed=evidence.get("stop_confirmed") is True,
            provider_stop_confirmed=evidence.get("provider_stop_confirmed") is True,
            poll_count=polls,
            now=now,
        )
        write_signal_snapshot(path, signals, clock=lambda: now)
    except Exception:
        pass


class SaladLiveSession:
    """Concrete secure-file composition; no request fields grant execution authority."""

    def __init__(self, config_file):
        from tessera_worker.remote_dispatch import RemoteDispatcher
        from tessera_worker.salad_remote import SaladTrustedAdapter

        from .salad_safety_service import SafetyHTTPSClient
        from .salad_secret_files import PrivateFileSecretResolver
        from .salad_server import SaladSafetyClient, TrustedSaladServer
        from .store import Store

        self.config_file = Path(config_file)
        self._seen_output = None
        # The route's fixed proposal, read once by ``execution_authority`` (bounded).
        self._authority_proposal = None
        try:
            self.config, policy, self.config_digest = _config(self.config_file)
            auth = Store(Path(self.config["state_dir"]))
            worker = auth.authenticate_worker(
                _private_bytes(self.config["worker_credential_file"], 4096).decode("ascii").strip()
            )
            self.resolver = PrivateFileSecretResolver(self.config["credential_directory"])
        except Exception:
            raise ValueError("private authenticated config unavailable") from None
        try:
            self.server = TrustedSaladServer(
                state_dir=Path(self.config["state_dir"]),
                worker=worker,
                policy=policy,
                resolver=self.resolver,
                safety=SaladSafetyClient(
                    self.config["guardian_socket"], service_uid=self.config["guardian_uid"]
                ),
            )
            adapter = SaladTrustedAdapter(self.server, self.config["route_id"])
            self.dispatcher = RemoteDispatcher(
                self.server.store, worker, {policy.target.selection.target_digest: adapter}
            )
            self.custody = SafetyHTTPSClient(**self.config["custody"])
            self.state(self.config["route_id"])
        except Exception:
            self.resolver.close()
            raise ValueError("trusted session composition unavailable") from None

    def close(self):
        self.resolver.close()

    def control_readiness(self):
        """Control's own readiness probe (the HTTP API gate's ``backend_status``): ``"ready"``
        or its closed unready reason (``state_unavailable``, ``schema_mismatch``,
        ``recovery_hold``). Read only; asked only after control refused a call."""
        from .deployment import backend_status

        status = backend_status(Path(self.config["state_dir"]))
        return "ready" if status["status"] == "ready" else status["reason"]

    def state(self, route):
        if route != self.config["route_id"]:
            raise ValueError("exact route required")
        with _calling("Store.get"):
            state = self.server.store.get(self.server.worker, route)
        if (
            (state["org_id"], state["project_id"], state["task_id"], state["proposal_digest"])
            != tuple(self.config[k] for k in ("org_id", "project_id", "task_id", "proposal_digest"))
            or not self.server.store._bound(state)
            or not state["approved_by"]
        ):
            raise ValueError("preexisting approved exact route required")
        return state

    def _body(self, route, operation):
        from .store import canonical_digest

        state = self.state(route)
        return dict(
            operation=operation, reservation=canonical_digest([route, state["proposal_digest"]])
        )

    def dispatch(self, route, action):
        from .remote_execution_models import RemoteProposal
        from .store import _connect

        state = self.state(route)
        if action == "stop":
            from .salad_precreate import CreateNotAttempted, launch_effects

            if not launch_effects(state):
                # No create effect: no group was ever asked for, so there is nothing to stop
                # (a stop preflight could only fail on the missing identity). The cancel has
                # already fenced the guardian's reservation (ADR 0205, L5).
                raise CreateNotAttempted()
        if action in {"create", "start"}:
            # Registry liveness before use-time authority: a long-lived executor's last
            # sighting may be older than the 60 s disconnect bound (keepalive starts later).
            self._registry_heartbeat()
            with _calling("Store.preflight_authority"):
                state = self.server.store.preflight_authority(
                    self.server.worker,
                    route,
                    RemoteProposal.model_validate(state["proposal"]),
                    action,
                )
            execution = state["proposal"]["selection"]["salad"]["execution"]
            with _connect(self.server.store.database_path) as db:
                live = self.server.store._live_principal(db, self.server.worker, audience="worker")
                _, package = self.server.store._operation(db, live, state["proposal"])
            plan = package["plan"]
            if (
                not 0
                < execution["max_cost_cents"]
                <= plan["budget_cents"]
                <= self.config["approved_max_cost_cents"]
                or state["proposal"]["selection"]["resources"]["max_seconds"]
                > self.config["max_seconds"]
            ):
                raise ValueError("approved financial runtime scope exceeded")
            # Fresh published rate + conservative enforced reservation ledger,
            # not an invented billing endpoint or a claim of known actual cost.
            with _wall_bound(15):
                self.server.safety.reserve(state)
            status = self._exchange(self._body(route, "status"))
            financial = _financial(status)
            rate = financial["published_hourly_rate_usd"]
            exposure = financial["estimated_exposure_cents"]
            if (
                rate is None
                or exposure is None
                or status.get("rate_unknown") is not False
                or status.get("budget_fenced") is not False
                or status.get("escalation_pending") is not False
                or status.get("orphan") is True
                or status.get("allocation_uncertain") is True
                or Decimal(exposure)
                + Decimal(rate)
                * Decimal(str(max(0, state["guardian_deadline"] - time.time())))
                * 100
                / 3600
                > execution["max_cost_cents"]
            ):
                raise ValueError("unknown exposure blocks new work")
        # An observe is a read: its tighter bound keeps the next heartbeat inside the
        # guardian's silence bound (a timeout is a skippable read stall in the driver).
        with _calling("RemoteDispatcher.dispatch"):
            with _wall_bound(
                OBSERVE_WALL_SECONDS if action == "observe" else DISPATCH_WALL_SECONDS
            ):
                return self.dispatcher.dispatch(route, action)

    def keepalive(self, route):
        """Heartbeat the guardian first, then the worker registry, then renew the soft lease.

        Heartbeat independence (ADR 0075 amendment): the beat never waits behind a control
        write lock. The trusted server re-runs use-time authority as a read (no write lock)
        and, if that read stalls on a lock, beats on the last verified authority while its
        soft lease is unexpired; a refusal blocks the beat and is raised for the driver. The
        registry and lease writes come after the beat, each with its own bound, and every
        connection opened here waits at most KEEPALIVE_LOCK_WAIT_SECONDS for a lock (a Python
        timer cannot interrupt a SQLite lock wait). Neither write extends the guardian's
        run-wide deadline; renewal stays bounded by the lease's hard expiry and epoch.
        """
        import uuid

        from .salad_server import AuthorityStalled
        from .store import bounded_lock_wait, canonical_digest

        beat_error, delivered, stalled, refused = None, False, None, None
        with bounded_lock_wait(KEEPALIVE_LOCK_WAIT_SECONDS):
            try:
                with _calling("TrustedSaladServer.heartbeat"), _wall_bound(15):
                    self.server.heartbeat(route)
                delivered = True
            except Exception as error:
                if isinstance(error, AuthorityStalled):
                    delivered = error.beat_delivered
                    beat_error = error
                elif _is_stall(error):
                    beat_error = error
                else:
                    # A refusal (authority or guardian) is raised for the driver to classify
                    # once the liveness writes below were attempted: a refusal the driver holds
                    # must not also let the registry and the lease go stale.
                    refused = error
            if delivered:
                self._note_beat(route, last_verified=beat_error is not None)
            try:
                # Registry liveness: a worker unseen for 60 s is "disconnected" and can no
                # longer renew lease authority.
                self._registry_heartbeat()
            except Exception as error:
                if not _is_stall(error) and refused is None:
                    raise
                stalled = error
            try:
                proposal = self.state(route)["proposal"]
                payload = {"lease_id": proposal["lease_id"], "expected_epoch": proposal["epoch"]}
                # Its own bound: a slow registry write never skips the renewal.
                with _calling("Store.renew_execution_lease"), _wall_bound(5):
                    self.server.store.renew_execution_lease(
                        self.server.worker,
                        proposal["lease_id"],
                        "salad-live-renew-" + uuid.uuid4().hex,
                        canonical_digest(payload),
                        proposal["epoch"],
                    )
            except Exception as error:
                if not _is_stall(error) and refused is None:
                    raise
                # Both writes are idempotent (fresh keys): a stall, including a wall-bound
                # timeout that surfaced after the write returned, only defers them a pass.
                stalled = stalled or error
        if refused is not None:
            raise refused  # the beat's refusal wins over anything the writes raised
        if delivered and (stalled is not None or beat_error is not None):
            raise ControlStall(beat_delivered=True) from (stalled or beat_error)
        if beat_error is not None:
            if stalled is None:
                raise beat_error
            raise ControlStall(beat_delivered=False) from stalled
        if stalled is not None:
            raise ControlStall(beat_delivered=True) from stalled

    def execution_authority(self, route):
        """Control's own record of whether it still wants this route run (a read, no write).

        Returns ``{"grant_status", "operation_desired", "epoch_current"}`` read in one snapshot
        from the route's exact operation, its execution grant and its lease: a grant that is no
        longer ``active`` (a cancel sets ``cancel_requested`` and bumps the epoch), an operation
        whose desired state is no longer ``admitted``, or a grant/lease epoch other than the
        proposal's (fenced) is control's decision to stop the route. Any failure (a lock, a
        bounded call that ran out, a refused or missing read) raises: it is never a cancel."""
        from contextlib import closing

        from .store import _connect, _read_snapshot, bounded_lock_wait

        store = self.server.store
        with (
            bounded_lock_wait(KEEPALIVE_LOCK_WAIT_SECONDS),
            _calling("Store.execution_authority"),
            _wall_bound(5),
        ):
            # The route's proposal is fixed (its digest is pinned by the config), so it is read
            # once, and that read sits inside the same lock and wall bounds as the authority
            # read: a held write lock can never stretch it past the guardian's beat bound.
            proposal = getattr(self, "_authority_proposal", None)
            if proposal is None:
                proposal = self.state(route)["proposal"]
                self._authority_proposal = proposal
            with closing(_connect(store.database_path)) as db, _read_snapshot(db):
                return self._authority_facts(store, db, proposal)

    def _authority_facts(self, store, db, proposal):
        """``execution_authority``'s one-snapshot read of the operation, grant and lease."""
        live = store._live_principal(db, self.server.worker, audience="worker")
        op, _ = store._operation(db, live, proposal)
        grant = db.execute(
            "SELECT status, epoch FROM execution_grants WHERE id=?", (op["grant_id"],)
        ).fetchone()
        lease = db.execute(
            "SELECT epoch FROM execution_leases WHERE id=?", (op["lease_id"],)
        ).fetchone()
        if grant is None or lease is None:
            raise ValueError("execution authority unreadable")
        return {
            "grant_status": grant["status"],
            "operation_desired": op["desired_state"],
            "epoch_current": grant["epoch"] == proposal["epoch"] == lease["epoch"],
        }

    def _note_beat(self, route, *, last_verified):
        """Record a beat the guardian took: the monotonic gap since the previous one."""
        now = time.monotonic()
        record = self.__dict__.setdefault("_beats", {}).setdefault(
            route,
            dict(
                delivered=0,
                on_last_verified_authority=0,
                max_gap_seconds=None,
                last_gap_seconds=None,
                at=None,
            ),
        )
        if record["at"] is not None:
            gap = round(max(0.0, now - record["at"]), 1)
            record["last_gap_seconds"] = gap
            record["max_gap_seconds"] = max(gap, record["max_gap_seconds"] or 0.0)
        record["at"] = now
        record["delivered"] += 1
        if last_verified:
            record["on_last_verified_authority"] += 1

    def beat_record(self, route):
        """Receipt ``guardian_beats``: beats the guardian took for this route and the largest
        and latest gap between two of them (seconds), or None when none was delivered."""
        record = self.__dict__.get("_beats", {}).get(route)
        if record is None:
            return None
        return {key: value for key, value in record.items() if key != "at"}

    def _registry_heartbeat(self):
        import uuid

        from .store import canonical_digest
        from .workers import WorkerRegistryStore

        with _calling("WorkerRegistryStore.heartbeat"), _wall_bound(5):
            WorkerRegistryStore(Path(self.config["state_dir"])).heartbeat(
                self.server.worker,
                "salad-live-worker-" + uuid.uuid4().hex,
                canonical_digest({}),
                {},
            )

    def input_expected(self, route):
        """True when the approved route's reviewed contract declares a staged input."""
        from .salad_server import route_input

        return route_input(self.state(route)["proposal"]) is not None

    def input_data_class(self, route):
        """Data class of the non-code files the route's grant delivers (ADR 0039)."""
        return self._input_classes(route)["input_data_class"]

    def input_code(self, route):
        """Approved private code the route's grant delivers (ADR 0039, amendment 1)."""
        classes = self._input_classes(route)
        return {key: classes[key] for key in ("input_code", "input_code_files")}

    def _input_classes(self, route):
        from .code_approval import operation_input_classes
        from .store import _connect

        operation = self.state(route)["proposal"]["operation_id"]
        connection = _connect(self.server.store.database_path)
        try:
            return operation_input_classes(connection, operation)
        finally:
            connection.close()

    def deliver_inputs(self, route):
        """Mint the input ticket for the guardian only, then confirm the guardian holds it.

        Called before start; any failure refuses the start. The ticket never leaves the
        trusted server and guardian; the confirmation reads metadata only.
        """
        from .salad_server import route_input

        state = self.state(route)
        declared = route_input(state["proposal"])
        if declared is None:
            raise ValueError("route declares no input")
        try:
            with _wall_bound(30):
                self.server.deliver_inputs(route)
        except TimeoutError:
            raise
        except Exception as error:
            raise PrestartStepError("ticket_mint_failed") from error
        try:
            held = self._exchange(self._body(route, "status")).get("input_delivery")
        except TimeoutError:
            raise
        except Exception as error:
            raise PrestartStepError("input_delivery_unconfirmed") from error
        if (
            type(held) is not dict
            or held.get("ticket_held") is not True
            or held.get("snapshot_digest") != declared.snapshot_digest
            or held.get("snapshot_digest") != state["proposal"]["selection"]["manifests"]["data"]
        ):
            raise PrestartStepError("input_delivery_unconfirmed")

    def revoke_inputs(self, route):
        """Drop at the guardian and revoke at control; a no-op for routes without input."""
        with _wall_bound(15):
            return self.server.revoke_inputs(route)

    def _exchange(self, body, **kwargs):
        with _wall_bound(10):
            return self.custody.exchange(body, **kwargs)

    def _multi_output(self, state):
        """Declared outputs {name: max_bytes} when they are not the original single shape."""
        from .salad_output_import import declared_outputs
        from .salad_workload_contract import LEGACY_OUTPUT_MAX_BYTES, LEGACY_OUTPUT_NAME

        declared = declared_outputs(state["proposal"]["selection"]["salad"]["execution"])
        if declared is None or (
            set(declared) == {LEGACY_OUTPUT_NAME}
            and declared[LEGACY_OUTPUT_NAME] <= LEGACY_OUTPUT_MAX_BYTES
        ):
            return None
        return declared

    def cancel(self, route):
        """Record the reduce-only cancel at control, then deliver the guardian's cancel.

        Returns True when this call recorded the cancel (the route did not already say stop),
        False when control had recorded it before (an operator, a campaign or an earlier try).
        ``GuardianCancelUndelivered.recorded_here`` carries the same fact."""
        state = self.state(route)
        unrecorded = None
        recorded_here = False
        if state["desired"] != "stop":
            # The executor's own worker credential (reduce-only, own route): no human
            # session credential exists for this path.
            try:
                self.server.store.worker_cancel(
                    self.server.worker,
                    route,
                    "salad-live-cancel-" + route,
                    expected_revision=state["revision"],
                    proposal_digest=state["proposal_digest"],
                )
                recorded_here = True
            except Exception as error:
                unrecorded = error
        if unrecorded is None:
            from .salad_server import GuardianRefused

            try:
                with _wall_bound(15):
                    self.server.safety.cancel(self.state(route))
            except GuardianRefused as error:
                # Control recorded the cancel and the guardian holds no reservation for this
                # route (its reserve was refused or never reached): there is nothing for it to
                # fence, so the cancel is complete, not unrecorded. Checked first, so a
                # never-created route is not retried as an undelivered guardian cancel.
                if error.code != "reservation_missing":
                    raise GuardianCancelUndelivered(recorded_here=recorded_here) from error
            except Exception as error:
                # Control recorded the cancel; only the guardian delivery failed (retried by
                # the caller: the route already says stop, so a retry sends only this call).
                raise GuardianCancelUndelivered(recorded_here=recorded_here) from error
            return recorded_here
        # Control did not record the cancel (e.g. a write lock). The guardian's own cancel
        # (reduce-only: it fences the reservation and stops the exact group) still goes out,
        # so no group is left running on a refused control write; the failure stays visible.
        sent = False
        try:
            with _wall_bound(15):
                self.server.safety.cancel(state)
            sent = True
        except Exception:
            pass
        raise CancelUnrecorded(guardian_cancel_sent=sent) from unrecorded

    def evidence(self, route):
        state = self.state(route)
        status = self._exchange(self._body(route, "status"))
        stopped = (
            status.get("status") == "stopped"
            and status.get("reason") == "exact_terminal_and_sealed"
            and status.get("orphan") is False
            and status.get("allocation_uncertain") is False
            and state["provider_status"] == "stopped"
            and state["termination_confirmed"]
        )
        # The provider's exact read confirmed the group stopped, but the guardian
        # could not seal custody (allocation changed): spend is closed, custody is
        # not, so this never counts as stop_confirmed or permits acceptance.
        provider_stopped = stopped or (
            status.get("status") == "reconciled"
            and status.get("reason") == "provider_confirmed_exact_group_stopped"
            and status.get("orphan") is False
            and state["provider_status"] == "stopped"
        )
        manifest = status.get("manifest")
        proof = dict(
            output_retained=False,
            stop_confirmed=stopped,
            provider_stop_confirmed=provider_stopped,
            writers_quiescent=False,
            output_byte_cap=state["proposal"]["selection"]["salad"]["execution"][
                "max_output_bytes"
            ],
            output_sha256=None,
            provider_id=state["provider_id"],
            financial=_financial(status),
            rate_read_retries=_count(status.get("rate_read_retries")),
            provider_read_retries=_count(status.get("provider_read_retries")),
            stop_cause=_stop_cause(status.get("stop_cause")),
            allocation_timeout=_allocation_timeout(status.get("allocation_timeout")),
            custody_work_begun=(
                status.get("custody_work_begun")
                if type(status.get("custody_work_begun")) is bool
                else None
            ),
            last_workload_refusal=_cause(status.get("last_workload_refusal")),
            last_rate_read_failure=_cause(status.get("last_rate_read_failure")),
            last_provider_read_failure=_cause(status.get("last_provider_read_failure")),
            prestart_read_retries=_prestart_read_retries(status.get("prestart_read_retries")),
            last_provider_read_failure_reason=_cause(
                status.get("last_provider_read_failure_reason")
            ),
            last_provider_read_failure_detail=_cause(
                status.get("last_provider_read_failure_detail")
            ),
            last_provider_read_failure_field=_field(status.get("last_provider_read_failure_field")),
            last_provider_read_failure_request_kind=_cause(
                status.get("last_provider_read_failure_request_kind")
            ),
        )
        start_failure = _start_failure(status.get("start_failure"))
        if start_failure is not None:
            proof["start_failure"] = start_failure
        start_events = closed_record(status.get("start_events"))
        if start_events is not None:
            proof["start_events"] = start_events
        declared = self._multi_output(state)
        if declared is not None:
            return self._multi_evidence(route, state, status, stopped, manifest, declared, proof)
        metadata = status.get("retained_output")
        if metadata is not None:
            expected_binding = self._output_binding(state)
            if (
                type(metadata) is not dict
                or set(metadata) != {"binding", "name", "retained", "sha256", "bytes", "quarantine"}
                or metadata["binding"] != expected_binding
                or metadata["name"] != "output.json"
                or metadata["quarantine"] is not True
                or type(metadata["retained"]) is not bool
            ):
                raise ValueError("exact retained metadata required")
            if metadata["retained"]:
                checksum, size = metadata["sha256"], metadata["bytes"]
                if (
                    type(checksum) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", checksum)
                    or type(size) is not int
                    or not 1 <= size <= proof["output_byte_cap"]
                ):
                    raise ValueError("retained metadata integrity or cap")
                identity = (checksum, size)
                if self._seen_output is not None and self._seen_output != identity:
                    raise ValueError("retained output changed")
                self._seen_output = identity
                proof.update(output_retained=True, output_sha256=checksum)
            elif metadata["sha256"] is not None or metadata["bytes"] is not None:
                raise ValueError("unretained metadata must not claim bytes")
        if stopped and type(manifest) is dict:
            data, checksum = self._import(route, manifest)
            if self._seen_output is not None and self._seen_output != (checksum, len(data)):
                raise ValueError("sealed output differs from observed retention")
            proof.update(output_retained=True, writers_quiescent=True, output_sha256=checksum)
        # No import before stopped: the service's import closes writer authority.
        return proof

    def _multi_evidence(self, route, state, status, stopped, manifest, declared, proof):
        """Declared multi-file outputs: retained only when every declared file is."""
        progress = status.get("retained_outputs")
        proof["outputs"] = None
        if progress is not None:
            if (
                type(progress) is not dict
                or set(progress) != {"binding", "declared", "files", "complete", "quarantine"}
                or progress["binding"] != self._output_binding(state)
                or progress["declared"] != sorted(declared)
                or progress["quarantine"] is not True
                or type(progress["complete"]) is not bool
                or type(progress["files"]) is not list
            ):
                raise ValueError("exact retained outputs metadata required")
            files = progress["files"]
            for entry in files:
                if (
                    type(entry) is not dict
                    or set(entry) != {"name", "sha256", "bytes"}
                    or entry["name"] not in declared
                    or type(entry["sha256"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                    or type(entry["bytes"]) is not int
                    or not 0 <= entry["bytes"] <= declared[entry["name"]]
                ):
                    raise ValueError("retained outputs metadata integrity or cap")
            if progress["complete"] != ([f["name"] for f in files] == sorted(declared)):
                raise ValueError("retained outputs metadata completeness")
            if progress["complete"]:
                identity = json.dumps(files, sort_keys=True)
                if self._seen_output is not None and self._seen_output != identity:
                    raise ValueError("retained output changed")
                self._seen_output = identity
                proof.update(output_retained=True, outputs=files)
        if stopped and type(manifest) is dict:
            files = self._import_files(route, manifest)
            identity = json.dumps(files, sort_keys=True)
            if self._seen_output is not None and self._seen_output != identity:
                raise ValueError("sealed output differs from observed retention")
            proof.update(output_retained=True, writers_quiescent=True, outputs=files)
        return proof

    def _import_files(self, route, expected_manifest=None):
        """Chunked import of every declared file; per-file sha256 recomputed here."""
        from .salad_output_import import (
            IMPORT_CHUNK_REPLY_BYTES,
            declared_outputs,
            stream_files,
            verify_manifest,
        )
        from .salad_workload_contract import OUTPUT_CHUNK_BYTES

        state = self.state(route)
        execution = state["proposal"]["selection"]["salad"]["execution"]
        reply = self._exchange(self._body(route, "import"))
        if (
            type(reply) is not dict
            or set(reply) != {"manifest", "chunk_bytes"}
            or reply["chunk_bytes"] != OUTPUT_CHUNK_BYTES
        ):
            raise ValueError("retained response unavailable")
        if expected_manifest is not None and reply["manifest"] != expected_manifest:
            raise ValueError("retained manifest changed")
        files = verify_manifest(
            reply["manifest"],
            binding=self._output_binding(state),
            declared=declared_outputs(execution),
            max_total=execution["max_output_bytes"],
        )

        def fetch(name, seq):
            return self._exchange(
                dict(self._body(route, "import_chunk"), name=name, seq=seq),
                cap=IMPORT_CHUNK_REPLY_BYTES,
            )

        return stream_files(fetch, files)

    def retained_files(self, route):
        proof = self.evidence(route)
        if not proof["stop_confirmed"] or not proof["writers_quiescent"]:
            raise ValueError("independent confirmed stop and sealed custody required")
        return self._import_files(route)

    @staticmethod
    def _output_binding(state):
        proposal = state["proposal"]
        return dict(
            tenant=state["org_id"],
            project=state["project_id"],
            operation=proposal["operation_id"],
            epoch=proposal["epoch"],
            artifact=proposal["selection"]["salad"]["execution"]["artifact_destination"],
            kind="output",
        )

    def _import(self, route, expected_manifest=None):
        from .salad_safety_service import encoded
        from .store import canonical_digest

        state = self.state(route)
        reply = self._exchange(self._body(route, "import"))
        if type(reply) is dict and set(reply) == {"manifest", "chunk_bytes"}:
            # A single output.json retained through chunked custody.
            return self._import_single_chunked(route, state, reply, expected_manifest)
        if type(reply) is not dict or set(reply) != {"manifest", "output"}:
            raise ValueError("retained response unavailable")
        manifest = reply["manifest"]
        if expected_manifest is not None and manifest != expected_manifest:
            raise ValueError("retained manifest changed")
        if not all(manifest.get(k) is True for k in ("writers_closed", "quarantine", "complete")):
            raise ValueError("sealed quarantine required")
        if (
            canonical_digest({k: v for k, v in manifest.items() if k != "digest"})
            != manifest["digest"]
        ):
            raise ValueError("manifest integrity")
        inventories = manifest["inventories"]
        if len(inventories) != 1:
            raise ValueError("one exact inventory required")
        inventory = inventories[0]
        binding = inventory["binding"]
        proposal = state["proposal"]
        if binding != self._output_binding(state) or not all(
            inventory.get(k) is True for k in ("writers_closed", "quarantine", "complete")
        ):
            raise ValueError("exact custody binding required")
        if (
            canonical_digest({k: v for k, v in inventory.items() if k != "digest"})
            != inventory["digest"]
        ):
            raise ValueError("inventory integrity")
        data = encoded(reply["output"])
        checksum = hashlib.sha256(data).hexdigest()
        if inventory["files"] != [dict(name="output.json", sha256=checksum, bytes=len(data))]:
            raise ValueError("retained bytes integrity")
        if len(data) > proposal["selection"]["salad"]["execution"]["max_output_bytes"]:
            raise ValueError("retained byte cap")
        return data, checksum

    def _import_single_chunked(self, route, state, reply, expected_manifest):
        from .salad_output_import import (
            IMPORT_CHUNK_REPLY_BYTES,
            declared_outputs,
            stream_files,
            verify_manifest,
        )
        from .salad_workload_contract import OUTPUT_CHUNK_BYTES

        execution = state["proposal"]["selection"]["salad"]["execution"]
        if reply["chunk_bytes"] != OUTPUT_CHUNK_BYTES:
            raise ValueError("retained response unavailable")
        if expected_manifest is not None and reply["manifest"] != expected_manifest:
            raise ValueError("retained manifest changed")
        files = verify_manifest(
            reply["manifest"],
            binding=self._output_binding(state),
            declared=declared_outputs(execution),
            max_total=execution["max_output_bytes"],
        )
        buffer = io.BytesIO()

        def fetch(name, seq):
            return self._exchange(
                dict(self._body(route, "import_chunk"), name=name, seq=seq),
                cap=IMPORT_CHUNK_REPLY_BYTES,
            )

        stream_files(fetch, files, lambda _: buffer)
        data = buffer.getvalue()
        return data, hashlib.sha256(data).hexdigest()

    def retained_bytes(self, route):
        proof = self.evidence(route)
        if not proof["stop_confirmed"] or not proof["writers_quiescent"]:
            raise ValueError("independent confirmed stop and sealed custody required")
        return self._import(route)[0]

    def reopen(self):
        if (
            hashlib.sha256(_private_bytes(self.config_file, 65536)).hexdigest()
            != self.config_digest
        ):
            raise ValueError("server config changed")
        return SaladLiveSession(self.config_file)


def source_digest(root=None):
    """Observed installed Python package-input inventory, not an inferred git state.

    ``root`` defaults to the directory this package is installed in; an empty inventory
    cannot be measured and is refused."""
    root = Path(__file__).parent.parent if root is None else Path(root)
    digest = hashlib.sha256()
    count = 0
    for package in ("tessera_control", "tessera_worker"):
        for path in sorted((root / package).rglob("*.py")):
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise ValueError("source inventory unsafe")
            data = path.read_bytes()
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(hashlib.sha256(data).digest())
            count += 1
    if not count:
        raise ValueError("source inventory empty")
    return digest.hexdigest()


# The one shape ``run`` accepts for ``source_revision``: the git commit, then the observed
# installed source digest. The executor config loader refuses anything else with this check.
SOURCE_PIN_PATTERN = re.compile(r"[0-9a-f]{40}\+sha256:[0-9a-f]{64}")
SOURCE_PIN_SHAPE = "<40 lowercase hex commit>+sha256:<64 lowercase hex installed source digest>"


def is_source_pin(value):
    """True when ``value`` has the exact shape ``run`` requires (the digest is not compared)."""
    return type(value) is str and SOURCE_PIN_PATTERN.fullmatch(value) is not None


def installed_source_pin(commit, root=None):
    """``commit`` pinned to the installed source as measured now: the value ``run`` accepts.

    Refuses a commit that is not 40 lowercase hex and a source tree that cannot be measured
    (missing, empty, unreadable or holding a symlink)."""
    if type(commit) is not str or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source commit must be 40 lowercase hex characters")
    try:
        digest = source_digest(root)
    except (OSError, ValueError):
        raise ValueError("the installed source cannot be measured") from None
    return f"{commit}+sha256:{digest}"


INVALID_CODE = "invalid_code"


def _allocation_timeout(value):
    """The guardian's allocation-timeout record (seconds waited, deadline) when well formed."""
    if (
        type(value) is dict
        and type(value.get("elapsed_seconds")) is int
        and type(value.get("deadline_seconds")) is int
        and 0 <= value["elapsed_seconds"] <= 86400
        and 0 < value["deadline_seconds"] <= 86400
    ):
        return {
            "elapsed_seconds": value["elapsed_seconds"],
            "deadline_seconds": value["deadline_seconds"],
        }
    return None


def _final_reason(reason):
    """A receipt reason later steps must not overwrite: the guardian's own recorded end."""
    from .salad_precreate import CREATE_NOT_ATTEMPTED

    return (
        reason in GUARDIAN_REASONS
        or reason == CREATE_NOT_ATTEMPTED  # nothing was sent: no later stop step can change it
        or str(reason).startswith(WORKLOAD_FAILED_CAUSE_PREFIX)
    )


def _stop_cause(value):
    """The guardian's stop cause: a closed token, or ``workload_failed:<closed reason>``
    when the workload reported its own failure (never an echoed free-form string)."""
    prefix = WORKLOAD_FAILED_CAUSE_PREFIX
    if type(value) is str and value.startswith(prefix):
        return value if value[len(prefix) :] in WORKLOAD_FAILURE_REASONS else "invalid_code"
    return _cause(value)


def _start_failure(value):
    """The guardian's start-failure record (distinct machines seen before custody)."""
    if type(value) is dict and type(value.get("machines")) is int and 0 <= value["machines"] <= 100:
        return {"machines": value["machines"]}
    return None


def _note_start_evidence(receipt, evidence):
    """Copy the start-failure record and the one events read into the receipt: closed tokens
    only, and only when present, so every other receipt keeps its shape."""
    for key in ("start_failure", "start_events"):
        if evidence.get(key) is not None:
            receipt["guardian_" + key] = evidence[key]


def _cause(value):
    """The guardian's recorded cause if it is a closed token; absent stays None.

    A present value outside the closed token shape is never echoed and never silently
    dropped: it is reported as the explicit ``invalid_code`` marker.
    """
    if value is None:
        return None
    if type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        return value
    return INVALID_CODE


def _prestart_read_retries(value):
    """Closed summary of the guardian's pre-start read retries, or None when there were none.

    Counts, times and closed tokens only (reason and mismatching field NAME), never a body."""
    if type(value) is not dict:
        return None
    count = _count(value.get("count"))
    if not count:
        return None

    def at(key):
        v = value.get(key)
        return round(v, 3) if type(v) in {int, float} and math.isfinite(v) else None

    return dict(
        count=count,
        first_at=at("first_at"),
        last_at=at("last_at"),
        last_reason=_cause(value.get("last_reason")),
        last_field=_cause(value.get("last_field")),
        deadline_seconds=at("deadline_seconds"),
    )


def _field(value):
    """A reported field NAME (letters, dots, dashes, underscores; never a value or digits)."""
    if value is None:
        return None
    if type(value) is str and re.fullmatch(r"[a-z][a-z_.\-]{0,47}", value):
        return value
    return INVALID_CODE


def _count(value):
    """A small non-negative integer from the guardian, else None (never trusted further)."""
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _financial(status):
    """Closed sanitized estimate projection. Actual invoice cost remains unknown."""
    result = dict(
        published_hourly_rate_usd=None,
        estimated_exposure_cents=None,
        actual_billed_cost_cents=None,
        financial_hard_cap=False,
    )
    for key in ("published_hourly_rate_usd", "estimated_exposure_cents"):
        value = status.get(key)
        if (
            type(value) is not str
            or len(value) > 40
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)
        ):
            continue
        try:
            number = Decimal(value)
            if number.is_finite() and (number > 0 if key.startswith("published") else number >= 0):
                result[key] = format(number, "f")
        except InvalidOperation:
            pass
    return result


def readiness():
    """Static repository readiness, not a probe or account health assertion."""
    return {
        "schema": "tessera.salad.readiness.v1",
        "evidence": "local_static_only",
        "network_calls": 0,
        "credential_reads": 0,
        "effects": 0,
        "live_ready": False,
        "missing": [
            "approved_exact_task_route_account_project_gpu_and_immutable_image",
            "trusted_linux_server_secure_file_composition_and_worker_identity",
            "private_versioned_management_credential_reference",
            "independently_running_authenticated_guardian_and_https_custody",
            "approved_financial_exposure_and_current_known_usage",
            "bounded_live_execution_authorization",
            "deployed_reconnect_and_retained_bytes_evidence_interface",
        ],
    }


def _write_receipt(directory, receipt, *, name="salad-live-receipt.json"):
    # Caller supplies an already-private receipt directory, never a workload path.
    parent = _private_path(directory, directory=True)
    try:
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


POLL_INTERVAL_CAP_SECONDS = 10
STOP_CONFIRM_SECONDS = 120
# While the provider has not started an instance (allocating/downloading/creating, or a
# group still pending/deploying; the route records those as "unknown"), observe slowly so a
# long cold image pull cannot exhaust the route's effect budget. Heartbeats/keepalive stay
# on the normal cadence; only the observe sleep stretches.
PRESTART_POLL_INTERVAL_SECONDS = 30
# The server reports a group Salad is still pulling for or deploying as "pending" or
# "deploying" and maps the instance-level pre-start states (allocating, downloading,
# creating) and every failed read or reallocation to "unknown". An affirmative observation of
# these sees the slower observe cadence, which is safe because the guardian, not the driver,
# drives stops and retention; once the running latch engaged (start recorded or running seen),
# a failed, blocked or unknown read never does (salad_running_latch).
# Receipt reasons the guardian's own stop decides; later steps never overwrite them.
GUARDIAN_REASONS = ("allocation_timeout", START_FAILURE_REASON)
PRESTART_PROVIDER_STATUSES = frozenset({"unknown", "pending", "deploying"})
# Guardian heartbeat refusals (docs/salad-safety-service.md, "Loop freshness and
# admin refusal codes").
# A running guardian stops a run on its own after GUARDIAN_SILENCE_SECONDS without a
# recorded heartbeat (GuardianAssignment.heartbeat_seconds). Transient refusals are
# only those a live guardian *returned*, so that stop is still in force while the driver
# skips one beat:
#   * guardian_loop_unready: its loop pass was not fresh when the request was accepted.
#   * workload_verifier_unready: the JWKS cache is stale; the loop refreshes it.
# "unavailable" (no answer: socket missing/unsafe, connect, timeout, cut-off reply) is
# terminal: a dead guardian enforces nothing on silence, so the driver must stop.
# Everything else is terminal and cancels at once, including stop_only,
# exposure_fenced, budget_binding_expired, stop_credential_window_insufficient,
# custody_unready, guardian_unready, budget_*, internal and any unknown code.
# rate_unknown is terminal here: on the heartbeat path the guardian raises it only
# after fencing the reservation (recovery=1, provider cancel), so it is never "not
# fenced". Non-guardian keepalive failures (registry, lease renewal, use-time
# authority) are terminal as before, except a control stall (a write lock, a bounded local
# call that ran out): see RUNNING_PROVIDER_STATUS below.
# A transient refusal is retried at the next interval only while the next beat can
# still land inside the silence bound with a margin, and at most
# MAX_TRANSIENT_GUARDIAN_REFUSALS times in a row.
GUARDIAN_SILENCE_SECONDS = 30
# The keepalive's control connections give up on another writer's lock after this long, so a
# held write lock costs each keepalive a few seconds, never the guardian beat (store
# bounded_lock_wait). The writes are retried at the next interval.
KEEPALIVE_LOCK_WAIT_SECONDS = 2
GUARDIAN_SILENCE_MARGIN_SECONDS = 5
MAX_TRANSIENT_GUARDIAN_REFUSALS = 3
TRANSIENT_GUARDIAN_CODES = frozenset({"guardian_loop_unready", "workload_verifier_unready"})
# A read-only observe whose fresh provider read timed out changes nothing, and the
# guardian keeps its own heartbeat and stop authority meanwhile, so it is skipped and
# retried at the next interval, at most this many times in a row.
MAX_TRANSIENT_OBSERVE_FAILURES = 3
# Attempts at the start preflight's fresh provider read (the start POST is sent once, only
# after a read observed the group) and the pauses between them.
START_PREFLIGHT_ATTEMPTS = 4
START_PREFLIGHT_BACKOFF_SECONDS = (2, 4, 8)
# One pre-start settle read (an exact observe of the new group) is retried while it fails
# transiently, inside a time window measured from its first attempt. Salad's first GET of a
# freshly created group can lag the create by longer than one 2 s read deadline, so a fixed
# handful of attempts gave up on a group that became readable moments later. The window covers
# about ten 2 s reads plus pauses, then fails closed; it is a post-create budget only and does
# not loosen any other read. Pauses grow to a cap (the last entry repeats); the attempt count is
# a second hard bound should a clock or read return instantly. Only a transient failure is
# retried: a timeout, or a failed fresh provider read that is not deterministic and carries a
# 5xx or no HTTP status class. A route conflict, a 3xx/4xx refusal, a deterministic read failure
# or any other error fails at once. Every retry also stops at the run deadline, and the guardian
# is heartbeat between attempts (each pause stays well inside the guardian silence bound).
SETTLE_READ_WINDOW_SECONDS = 60
SETTLE_READ_BACKOFF_SECONDS = (2, 4, 5)
SETTLE_READ_ATTEMPTS = 20
# Closed vocabulary of the receipt's ``prestart_settle_read`` block.
SETTLE_READ_CALLS = frozenset({"observe", "keepalive"})
SETTLE_READ_REASONS = frozenset({"read_unavailable", "conflict_state", "timeout", "other"})
HTTP_STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx", "none"})


def _settle_read_failure(error):
    """(reason, http_status_class, transient) of a failed settle step; closed values only.

    Reads only the error's class and the transport's closed ``detail`` token; never its
    message text, body, URL or headers."""
    from .salad_guardian import is_deterministic_read_failure
    from .salad_server import ProviderReadUnavailable
    from .store import Conflict

    status_class = "none"
    if isinstance(error, ProviderReadUnavailable):
        detail = getattr(error, "detail", None)
        match = re.fullmatch(r"non_2xx_([2-5])xx", detail) if type(detail) is str else None
        if match:
            status_class = match.group(1) + "xx"
        reason = "timeout" if detail == "timeout" else "read_unavailable"
        transient = status_class in {"5xx", "none"} and not is_deterministic_read_failure(error)
    elif isinstance(error, TimeoutError):
        reason, transient = "timeout", True
    elif isinstance(error, Conflict):
        reason, transient = "conflict_state", False
    else:
        reason, transient = "other", False
    return reason, status_class, transient


def _guardian_code(error):
    from .salad_server import guardian_refusal_code

    return guardian_refusal_code(error)


# --- a still-running route is not cancelled for a stall ----------------------------------------
# The provider status a post-start observe reports for a group whose instance is running.
RUNNING_PROVIDER_STATUS = "running"
# While the last post-start observe read the group running, a control stall (a write lock on
# the control database, a bounded local call that ran out) or a timed-out read is not a reason
# for this driver to stop it: the guardian keeps its own deadline, budget and silence authority,
# and stops the group itself. The driver skips the beat or read and tries again at the next
# interval, inside the run deadline and poll bound. That holds past the guardian's silence
# bound too: a keepalive whose control write (or guardian beat) stalled on a running route with
# no guardian stop cause is a control stall, never a silence_bound cancel. Outside "running"
# (pre-start, unknown) or once the guardian recorded a stop cause the earlier bounds apply.
MAX_TRANSIENT_CONTROL_STALLS = 3
# Bounded local wall time of one observe dispatch, so a slow read cannot keep the next
# guardian heartbeat past its silence bound (other dispatches keep the 45 s bound).
OBSERVE_WALL_SECONDS = 25
DISPATCH_WALL_SECONDS = 45
# A session.cancel that fails on a control stall is retried this many times in all, with a short
# pause; the guardian cancel is sent even when the control write fails (SaladLiveSession.cancel).
CANCEL_ATTEMPTS = 3
CANCEL_RETRY_SECONDS = 2
# Receipt reason when the reduce-only cancel was never recorded at control: visible, never
# folded into custody_stop_or_reconnect_unresolved.
CANCEL_UNRECORDED_REASON = "cancel_unrecorded"
# Receipt reason prefix when this driver cancelled a route whose last post-start observe read
# "running": the suffix is the closed cancel class (never a provider move or preemption).
RUNNING_CANCEL_PREFIX = "running_route_cancelled_"
# Closed classes of the receipt's ``cancel_cause`` (why this driver sent its cancel).
CANCEL_CAUSE_CLASSES = frozenset(
    {
        "execution_complete",  # output retained (or the guardian's own recorded end)
        "guardian_stop_cause",  # the guardian recorded why it stopped the group
        "deadline",  # the run deadline passed
        "budget",  # a budget / rate refusal from the guardian
        "guardian_refusal",  # any other terminal guardian heartbeat refusal
        "silence_bound",  # no heartbeat reached the guardian within its silence bound
        "guardian_unavailable",  # the guardian did not answer
        "control_stall",  # control writes stalled past their bound before the route ran
        "observe_timeout",  # the observe read failed past its bound
        "control_refused",  # control refused (use-time authority, lease, route revision)
        # control's own record says it decided to stop the route: its execution grant is no
        # longer active (cancel_requested), its operation no longer admitted, or its grant /
        # lease epoch moved past the proposal's (fenced); see SaladLiveSession.execution_authority
        "control_cancel_requested",
        "prestart",  # a pre-start step failed (see prestart_failure)
        "poll_bound",  # the poll bound ran out with no output
        "output_unavailable",  # the loop ended with no retained output
        "error",  # anything else (see cancel_cause["error"] for the sanitized detail)
    }
)


class ControlCancelRequested(Exception):
    """Control's own authoritative record says it decided to stop this route (a grant that is
    no longer active, an operation no longer admitted, or a fenced epoch). A genuine terminal
    cause even on a running route: the driver ends its loop, cancels and stops the exact group."""

    def __init__(self):
        super().__init__("control cancel requested")


class ControlStall(Exception):
    """A keepalive's control write (worker registry, lease renewal) stalled.

    ``beat_delivered`` says whether the guardian heartbeat was still sent after it."""

    def __init__(self, beat_delivered):
        super().__init__("control stall")
        self.beat_delivered = beat_delivered


class GuardianCancelUndelivered(Exception):
    """Control recorded the reduce-only cancel, but the guardian's own cancel call failed.

    The cancel is recorded (the route's desired state is stop); only the guardian delivery is
    retried. The exact stop dispatch and the guardian's sealed stop still decide the run.
    ``recorded_here``: this call (not an earlier one or another party) recorded the cancel."""

    def __init__(self, message="guardian cancel undelivered", *, recorded_here=False):
        super().__init__(message)
        self.recorded_here = recorded_here


# --- the completion tail: stop, stop confirm, custody seal, verification -----------------------
# Once the output is retained the money is spent and sealed custody is immutable, so every step
# of the tail is an idempotent read or a reduce-only write: a transient failure in it is retried
# within bounded windows and recorded on the receipt (``tail_errors``), never a failed run.
# A completed run whose stop the provider already confirmed waits up to CUSTODY_SEAL_SECONDS
# (from the start of the stop wait) for the guardian to seal custody: the provider stops first,
# custody seals after. The group is stopped meanwhile, so the wait accrues nothing.
CUSTODY_SEAL_SECONDS = 300
# Transient read failures in a row tolerated while confirming the stop of a completed run (the
# window above still bounds them); other runs keep MAX_TRANSIENT_OBSERVE_FAILURES.
TAIL_READ_FAILURES = 12
# The stop preflight (a fresh exact read) is retried while no stop effect exists; the stop POST
# itself is still sent at most once.
STOP_DISPATCH_ATTEMPTS = 4
# Attempts at the verification (evidence, import, reconnect) of a confirmed, sealed stop, inside
# VERIFY_RETRY_SECONDS from the first.
VERIFY_ATTEMPTS = 6
VERIFY_RETRY_SECONDS = 120
TAIL_BACKOFF_SECONDS = (2, 4, 8, 15)
# Receipt ``tail_errors`` keeps at most this many (the latest); ``tail_error_count`` counts all.
TAIL_ERRORS_CAP = 20
TAIL_STEPS = frozenset(
    {"cancel", "guardian_cancel", "stop", "stop_confirm", "verify", "final_evidence"}
)
# Waiting states and guardian/custody answers that a later read can change. Integrity, binding,
# identity and pin mismatches are never in it: they stay terminal.
TAIL_WAIT_MESSAGES = frozenset(
    {
        "custody unresolved",
        "independent confirmed stop and sealed custody required",
        "independent service unavailable",
        "retained response unavailable",
        "service_unavailable",
    }
)
# After a refused heartbeat, the guardian status read that decides completion (output retained)
# is tried this many times, a short pause apart. If every try stalls, the refusal is undecided:
# skipped to the next pass (whose evidence read decides) at most MAX_UNDECIDED_REFUSALS times in
# a row. The guardian keeps its own stop authority meanwhile.
COMPLETION_STATUS_READ_ATTEMPTS = 3
COMPLETION_STATUS_READ_PAUSE_SECONDS = 1
MAX_UNDECIDED_REFUSALS = 3
# The guardian's stop cause when it stopped the group because the approved output was retained.
OUTPUT_RETAINED_STOP_CAUSE = "output_retained"
# The guardian's stop cause when control asked it to stop (ADR 0075). The guardian records it as
# the first cause of any cancel, including this driver's own completion cancel sent before the
# output was retained.
CONTROL_CANCEL_STOP_CAUSE = "control_cancel"


def _completion_stop_cause(receipt):
    """True when the guardian's recorded stop cause is compatible with a completed run whose
    custody may still seal: none yet, ``output_retained``, or ``control_cancel`` when that cancel
    is this driver's own completion stop (``cancel_cause.class`` exactly ``execution_complete``
    and this driver recorded the cancel at control). An operator, campaign or other cancel
    recorded as ``control_cancel`` never qualifies, nor does any other guardian stop."""
    cause = receipt.get("guardian_stop_cause")
    if cause in {None, OUTPUT_RETAINED_STOP_CAUSE}:
        return True
    cancel = receipt.get("cancel_cause")
    return (
        cause == CONTROL_CANCEL_STOP_CAUSE
        and type(cancel) is dict
        and cancel.get("class") == "execution_complete"
        and receipt.get("cancel_recorded") is True
        and receipt.get("cancel_recorded_by_driver") is True
    )


def _tail_transient(error):
    """True when a completion-tail error may be retried: a read stall (timeout, lock, transient
    provider or guardian read), a route-revision race, a guardian code that is not its decision,
    or a custody answer that is not final yet. Integrity and binding failures never are."""
    root = error
    if isinstance(error, (CancelUnrecorded, GuardianCancelUndelivered)) and error.__cause__:
        root = error.__cause__
    try:
        if _is_read_stall(root) or conflict_reason(root) in TRANSIENT_CONFLICT_REASONS:
            return True
        code = _guardian_code(root)
        if code is not None:
            return code in HELD_GUARDIAN_CODES
    except Exception:
        return False
    args = getattr(root, "args", ())
    return (
        type(root) is ValueError
        and len(args) == 1
        and type(args[0]) is str
        and args[0] in TAIL_WAIT_MESSAGES
    )


class CancelUnrecorded(Exception):
    """The reduce-only cancel was not recorded at control; ``guardian_cancel_sent`` says
    whether the guardian's own cancel (which stops the provider group) still went out."""

    def __init__(self, guardian_cancel_sent):
        super().__init__("cancel unrecorded")
        self.guardian_cancel_sent = guardian_cancel_sent


def _is_stall(error):
    """A control write lock, a local bounded call that ran out, or a control stall."""
    import sqlite3

    return isinstance(error, (sqlite3.OperationalError, TimeoutError, ControlStall))


def _is_read_stall(error):
    """A stall, or a fresh provider read that failed transiently (not deterministically)."""
    from .salad_guardian import is_deterministic_read_failure
    from .salad_safety_service import TRANSIENT_SERVICE_DETAILS, ServiceUnavailable
    from .salad_server import ProviderReadUnavailable

    if isinstance(error, ProviderReadUnavailable):
        return not is_deterministic_read_failure(error)
    if isinstance(error, ServiceUnavailable):
        # The guardian status read (``SaladLiveSession.evidence``) timed out or was cut off:
        # a read that changes nothing, retried like a timed-out provider read.
        return error.detail in TRANSIENT_SERVICE_DETAILS
    return _is_stall(error)


def _is_read_failure(error):
    """A failed read the post-start loop may skip (bounded unless the route is running)."""
    from .salad_safety_service import ServiceUnavailable
    from .salad_server import ProviderReadUnavailable

    return isinstance(error, (ProviderReadUnavailable, ServiceUnavailable)) or _is_stall(error)


# --- a control refusal while running: verified revocation, or a stall ----------------------------
# While the last post-start observe read the group running, a Forbidden (or an HTTP 401/403)
# cancels the route only when it is a verified revocation: control is ready and the refusal's
# detail is exactly one of control's authority-gone details (``AUTHORITY_GONE_DETAILS`` in
# tessera_worker.control_channel; for an HTTP refusal, the channel's own exact check). Any
# other Forbidden (a generic "Unauthorized", an empty or unreadable detail, "Authenticated
# exact Salad route required.") or any Forbidden while control's readiness probe fails is a
# transient control stall: it takes the stall path above (skip to the next interval; a
# keepalive that kept the beat from the guardian is tolerated past the silence bound only while
# control is unready and the guardian has recorded no stop cause, else inside that bound).
# The guardian keeps its own deadline, budget, silence and stop authority meanwhile.
# Every refusal (Forbidden, 401/403, a store Conflict) leaves bounded evidence on the receipt:
# the driver step, the control call, the HTTP status when there was one and the detail.
REFUSAL_STEPS = frozenset({"keepalive", "observe", "evidence", "run"})
REFUSAL_KINDS = frozenset({"forbidden", "conflict"})
UNRECOGNIZED_DETAIL = "unrecognized_detail"
# Plain sentence text only (a "/" only between words, as in "lease/epoch"): no URL, path,
# query, header or token-shaped run survives.
_REFUSAL_DETAIL_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9 ,.'():;_/-]{0,159}")
_UNSAFE_DETAIL = re.compile(r"[A-Za-z0-9_-]{24,}|(?<![A-Za-z0-9])/|/(?![A-Za-z0-9])|:/")


def _refusal_detail(value):
    """A refusal's detail if it is bounded plain text, else UNRECOGNIZED_DETAIL (None if none)."""
    from tessera_worker.control_channel import AUTHORITY_GONE_DETAILS

    if value is None or value == "":
        return None
    if type(value) is not str:
        return UNRECOGNIZED_DETAIL
    if value in AUTHORITY_GONE_DETAILS:
        return value
    if not _REFUSAL_DETAIL_SHAPE.fullmatch(value) or _UNSAFE_DETAIL.search(value):
        return UNRECOGNIZED_DETAIL
    return value


def _refusal(error):
    """(kind, http_status, detail, verified) of a control refusal, or None for anything else.

    ``verified`` is True only for an exact authority-gone detail (in-process) or a channel
    error that control's 403 body verified as authority gone (HTTP). Never a substring match."""
    from tessera_worker.control_channel import AUTHORITY_GONE_DETAILS, ControlChannelError

    from .salad_server import ProviderReadUnavailable
    from .store import Conflict, Forbidden

    if isinstance(error, Forbidden):
        detail = error.args[0] if len(error.args) == 1 else None
        verified = type(detail) is str and detail in AUTHORITY_GONE_DETAILS
        return "forbidden", None, _refusal_detail(detail), verified
    if isinstance(error, Conflict) and not isinstance(error, ProviderReadUnavailable):
        detail = error.args[0] if len(error.args) == 1 else None
        return "conflict", None, _refusal_detail(detail), False
    if isinstance(error, ControlChannelError) and error.status in {401, 403, 409}:
        kind = "conflict" if error.status == 409 else "forbidden"
        verified = error.status == 403 and error.authority_gone is True
        return kind, error.status, _refusal_detail(error.refusal_reason), verified
    return None


# --- a running route is never cancelled on an unexpected or transient error -------------------
# While the last post-start observe read the group running and the guardian has recorded no stop
# cause, only a genuine terminal cause ends the run and sends this driver's cancel: the run
# deadline, a budget refusal, control's own recorded cancel (its grant no longer active or its
# epoch fenced: ``control_check``), an explicit operator cancel or revocation (a verified
# authority-gone refusal from a ready control, or a control Conflict other than a route-revision
# race), the guardian's own decision (its recorded stop cause, or a terminal refusal code it
# returned) or a verified execution_complete. Anything else raised inside the post-start loop
# (a ValueError, a malformed or partial provider or guardian reply, a read timeout, a control
# stall, an OperationalError, any new class) is recorded on the receipt (``running_errors_held`` and
# ``last_held_error``, sanitized) and the pass is retried at the next interval; the guardian keeps
# its own deadline, budget and silence authority meanwhile and stops the group itself.
# Guardian heartbeat codes that are not a guardian decision: it did not answer, its loop or
# verifier was not fresh, or its own request ran out of time (e.g. while it waited on a slow
# provider read). Every other code is the guardian's refusal and stays terminal.
HELD_GUARDIAN_CODES = TRANSIENT_GUARDIAN_CODES | frozenset(
    {"unavailable", "internal", "service_deadline", "admin_deadline_exceeded"}
)
# Closed steps of the post-start loop a held error is attributed to.
HELD_ERROR_STEPS = frozenset({"keepalive", "observe", "evidence", "record"})
ERROR_MESSAGE_CAP = 160
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_ERROR_SITE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\.[A-Za-z_<>][A-Za-z0-9_<>]{0,63}")
# Words that never appear in a recorded message, whatever its shape.
_SECRET_WORDS = re.compile(r"(?i)bearer|token|secret|passw|api.?key|authoriz|cookie|credential")


def _error_message(error):
    """The error's message if it is short plain text, else UNRECOGNIZED_DETAIL (None if empty).

    Only a single string argument is read. A closed token (``service_unavailable``) or a plain
    sentence (``exact retained metadata required``) survives; a path, URL, query, long token-
    shaped run, secret-looking word or anything over ERROR_MESSAGE_CAP characters does not."""
    args = getattr(error, "args", ())
    if not args:
        return None
    if len(args) != 1 or type(args[0]) is not str:
        return UNRECOGNIZED_DETAIL
    text = args[0].strip()
    if not text:
        return None
    if (
        len(text) > ERROR_MESSAGE_CAP
        or not _REFUSAL_DETAIL_SHAPE.fullmatch(text)
        or _UNSAFE_DETAIL.search(text)
        or _SECRET_WORDS.search(text)
    ):
        return UNRECOGNIZED_DETAIL
    return text


def _error_site(error):
    """``module.function`` of the innermost frame that raised (no path, no line), or None."""
    tb = getattr(error, "__traceback__", None)
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    site = Path(code.co_filename).stem + "." + code.co_name
    return site if _ERROR_SITE.fullmatch(site) else None


def _error_detail(error):
    """Sanitized, bounded diagnosis of an error for the receipt; never secrets, paths or bodies.

    ``exception``: class name; ``message``: _error_message; ``cause``: the class name of the
    error it was raised from (also a suppressed ``from None`` context, e.g. the socket timeout
    behind ``ValueError("service_unavailable")``); ``detail``: the error's own closed ``detail``
    token when it carries one (a transport's ``timeout``, ``non_200``...); ``site``: the raising
    ``module.function``; ``call``: the tagged control call (CONTROL_CALLS) or None."""
    if error is None:
        return None
    try:
        inner = error.__cause__ if error.__cause__ is not None else error.__context__
        call = getattr(error, "live_check_call", None)
        return {
            "exception": _exception_type_name(error),
            "message": _error_message(error),
            "cause": None if inner is None else _exception_type_name(inner),
            "detail": _closed_token(getattr(error, "detail", None)),
            "site": _error_site(error),
            "call": call if call in CONTROL_CALLS else None,
        }
    except Exception:
        return {"exception": _exception_type_name(error), "message": UNRECOGNIZED_DETAIL}


def _budget_code(code):
    return type(code) is str and (code.startswith("budget_") or code == "rate_unknown")


def _drive(
    session,
    *,
    route_id,
    source_revision,
    max_seconds,
    max_polls,
    sleep,
    clock,
    watch_signals_file=None,
    wall_clock=_wall_now,
    route_window=None,
):
    """Internal orchestration seam; isolated fake sessions prove only local behavior.

    Session.dispatch MUST be the existing RemoteDispatcher.dispatch. Evidence and
    reopen MUST come from authenticated durable external service composition.
    The driver never manufactures admissions, resets claims or reissues launch.
    """
    receipt = {
        "schema": "tessera.salad.live-check.v1",
        "source_revision": source_revision,
        # The window the run had (None when the caller did not name it), and who guards
        # completeness: a trainer that stops itself and exits 0 passes at platform level.
        "route_window": route_window,
        "completeness_guard": "experiment",
        "status": "inconclusive",
        "reason": "journey_incomplete",
        "stop_confirmed": False,
        "provider_stop_confirmed": False,
        "reconnected": False,
        "dispatch_calls": 0,
        "cost_cents": None,
        "usage_status": "unknown",
        "node_writers": "unknown",
        "financial": _financial({}),
        "input_expected": None,
        "input_delivered": False,
        "input_revoked": None,
        # The reduce-only cancel is sent with the executor's own worker credential
        # (ADR 0075); True once the route state recorded it, False if it was refused.
        "cancel_authority": "executor_worker",
        "cancel_recorded": None,
        # Why this driver sent its cancel: {"class": one of CANCEL_CAUSE_CLASSES, "exception":
        # the ending error's class name or None, "guardian_refusal": closed code or None,
        # "last_provider_status": the last post-start observe's status or None,
        # "heartbeat_silent_seconds": seconds since the last heartbeat the guardian took,
        # "error": the ending error's sanitized, bounded detail (_error_detail) or None}.
        # Added only when present: ``running_errors_held`` / ``last_held_error`` (errors held
        # on a running route, see HELD_GUARDIAN_CODES) and ``last_skipped_read``.
        "cancel_cause": None,
        # ``cancel_cause["refusal"]``: the control refusal behind the cancel (see
        # ``control_refusal`` below) or None.
        # Added only when control refused a call (absent otherwise): ``control_refusal`` is the
        # latest refusal {"kind": forbidden|conflict, "step": keepalive|observe|evidence|run,
        # "call": the Store/API call (CONTROL_CALLS) or None, "http_status": int or None,
        # "detail": control's bounded plain-text detail or "unrecognized_detail" or None,
        # "verified_authority_gone": bool, "control_readiness": "ready", a closed unready
        # reason or None when not probed, "skipped": True when it was skipped as a stall};
        # ``control_refusals_skipped`` counts refusals skipped as a stall while running.
        # Set only when the cancel failed: {"exception", "attempts", "guardian_cancel_sent"}.
        "cancel_failure": None,
        # Keepalives whose control writes stalled (write lock, bounded call ran out); the
        # guardian heartbeat is still sent when it can be.
        "control_stalls": 0,
        # The provider status of the last post-start observe (closed token), or None. Once the
        # running latch engaged, only an affirmative observation (the readback's proxy_reason
        # "observed") updates it: a failed, blocked or unknown read leaves it unchanged.
        # Added only once the latch engaged: ``running_latch`` {"state": started|seen_running|
        # cleared, "cleared_by": provider_<stopped|succeeded|failed|deleted>|reallocated|None}
        # (salad_running_latch); ``running_classifier_errors_held`` counts errors held because
        # hold()'s own classification raised.
        "last_provider_status": None,
        # ADR 0039: the delivered input's recorded class (None when nothing is
        # delivered or the session cannot read it); outputs are always private_derived.
        "input_data_class": None,
        # Amendment 1: "private_code_approved" with each approved file's sha256, or
        # "none"; None when no input is expected, "unknown" when one is expected but the
        # session cannot read it. input_data_class is "none_all_approved_code" when every
        # delivered file is approved code.
        "input_code": None,
        "input_code_files": None,
        "output_data_class": "private_derived",
        # The provider's bounded summary when it cleanly rejected the create (400/422).
        "create_rejection": None,
        # Closed guardian refusal code (and cause) when a refusal ended the run.
        "guardian_refusal": None,
        "guardian_transient_refusals": 0,
        # Read-only observes skipped after a provider read timeout (bounded in a row).
        "observe_transient_failures": 0,
        # Published-rate and group/instance reads the guardian had to retry (bounded,
        # fail-closed), and why the guardian itself first decided to stop, if it did.
        "guardian_rate_read_retries": None,
        "guardian_provider_read_retries": None,
        "guardian_stop_cause": None,
        # Whether custody work (begin-once, an input served, an output requested or written)
        # had begun when the guardian stopped: False only on the guardian's own word, None when
        # unknown. With a reallocation stop it classifies the run as retryable (ADR 0179).
        "custody_work_begun": None,
        # Latest closed code with which the guardian refused a workload call, if any
        # (e.g. a container that called begin-once before its instance could be bound).
        # ADVISORY: recorded before the call's token binds, so it is a diagnostic hint only.
        "guardian_last_workload_refusal": None,
        # Closed reason the guardian's last failed rate / group read gave up with
        # (timeout, provider_unknown, rate_stale, ...), if any.
        "guardian_last_rate_read_failure": None,
        "guardian_last_provider_read_failure": None,
        # The transport's closed diagnostics for that failure (the outward code above stays
        # provider_unknown): reason (e.g. identity_mismatch, malformed_response,
        # provider_unavailable), detail (non_2xx_<class>xx, reset, timeout) and the NAME of the
        # mismatching field. Never values, bodies or organization/project paths.
        "guardian_last_provider_read_failure_reason": None,
        "guardian_last_provider_read_failure_detail": None,
        "guardian_last_provider_read_failure_field": None,
        # Which provider request failed: list, quotas, gpu_classes, availability, read_group,
        # read_instance, create or other.
        "guardian_last_provider_read_failure_request_kind": None,
        # UTC time each pre-start step finished (create, bound, settle, deliver_inputs,
        # start_preflight, start); a step that did not run stays None. ``start_preflight``
        # is the moment the start dispatch began, not finished (its fresh provider read runs
        # inside it). ``create`` is stamped even when the create was rejected, and ``settle``
        # means the settle wait ended, whether or not the group read stopped.
        "prestart_timestamps": dict.fromkeys(PRESTART_STEPS),
        # Closed class of the pre-start step that failed ({"class", "exception"}: the
        # exception's type name only, never its text), or None. Diagnostic; it does not
        # change status or reason.
        "prestart_failure": None,
        # Failed fresh reads of the start preflight that were retried before start (the
        # start itself is sent once); and the guardian's pre-start read retries (closed
        # summary, no values): see salad_guardian.PRESTART_READ_RETRY_PREFIX.
        "start_preflight_retries": 0,
        "guardian_prestart_read_retries": None,
        # Settle-step observes retried once after a transient Conflict (route revision race).
        "settle_conflict_retries": 0,
        # Added only when a pre-start settle read or heartbeat failed (absent otherwise, so
        # receipts without such a failure keep their digests): ``prestart_settle_read`` is
        # {"call": observe|keepalive, "reason": read_unavailable|conflict_state|timeout|
        # other, "http_status_class": 2xx|3xx|4xx|5xx|none, "attempts": n, "outcome":
        # recovered|failed, "elapsed_seconds": s since the first attempt} for the latest such
        # failure. Closed values only.
    }
    step = {"name": None}

    def stamp(name):
        # Diagnostic only: a failing wall clock never changes the journey.
        try:
            receipt["prestart_timestamps"][name] = wall_clock()
        except Exception:
            pass

    deadline = clock() + max_seconds
    interval = min(POLL_INTERVAL_CAP_SECONDS, max(1, max_seconds / max_polls))
    launched = False
    execution_complete = False

    def dispatch(action):
        receipt["dispatch_calls"] += 1
        return session.dispatch(route_id, action)

    def note_tail_error(name, error, attempt=None):
        """Record one completion-tail error on the receipt, sanitized and bounded: ``tail_errors``
        (the latest TAIL_ERRORS_CAP) and ``tail_error_count`` (all). Absent until one happens, so
        receipts without a tail error keep their shape. Never raises."""
        try:
            entry = dict(
                _error_detail(error) or {},
                step=name if name in TAIL_STEPS else "tail",
                attempt=attempt if type(attempt) is int else None,
                transient=_tail_transient(error),
            )
        except Exception:
            entry = {"exception": _exception_type_name(error), "step": "tail"}
        try:
            errors = receipt.setdefault("tail_errors", [])
            errors.append(entry)
            del errors[:-TAIL_ERRORS_CAP]
            receipt["tail_error_count"] = receipt.get("tail_error_count", 0) + 1
        except Exception:
            pass

    def tail_retry(name, call, *, attempts, until):
        """``call()``, retried while it fails transiently (``_tail_transient``), at most
        ``attempts`` times and never past ``until``; every failure is recorded on the receipt.
        The last failure, or any non-transient one, propagates unchanged."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return call()
            except Exception as error:
                note_tail_error(name, error, attempt)
                if attempt >= attempts or not _tail_transient(error) or clock() >= until:
                    raise
                pause = TAIL_BACKOFF_SECONDS[min(attempt - 1, len(TAIL_BACKOFF_SECONDS) - 1)]
                sleep(min(pause, max(0, until - clock())))

    def dispatch_start():
        """dispatch("start"), retrying a failed fresh read of the start preflight.

        The start preflight is a read: when it fails nothing was sent or claimed, so a
        transient failure (timeout, provider_unknown) is retried a few times within the
        run bound, keeping the guardian heartbeat alive between attempts. A deterministic
        failure (identity mismatch, group gone) is not retried; the last failure
        propagates unchanged. The one start POST itself is never repeated."""
        from .salad_guardian import is_deterministic_read_failure
        from .salad_server import ProviderReadUnavailable

        for attempt in range(START_PREFLIGHT_ATTEMPTS):
            try:
                return dispatch("start")
            except ProviderReadUnavailable as error:
                if (
                    attempt == START_PREFLIGHT_ATTEMPTS - 1
                    or is_deterministic_read_failure(error)
                    or clock() >= deadline
                ):
                    raise
                receipt["start_preflight_retries"] += 1
                heartbeat()
                sleep(min(START_PREFLIGHT_BACKOFF_SECONDS[attempt], max(0, deadline - clock())))

    beats = {"ok_at": None, "transient": 0, "stalls": 0, "undecided": 0}
    observes = {"failed": 0}
    # The class a bounded helper gave up with (heartbeat, observe), read by the cancel cause.
    ended = {"class": None}

    # Running-route latch (salad_running_latch): engaged once the start effect is recorded or
    # an observe reads running; only an affirmative non-running observation, a reallocation or a
    # guardian stop cause clears it. A failed, blocked or unknown read never lifts it.
    latch = RunningLatch()

    def running():
        """True while the running latch is engaged. Callers that end protection on a guardian
        stop cause (``hold``, the heartbeat stall hold) also check that cause themselves."""
        return latch.engaged()

    def note_latch():
        block = latch.receipt()
        if block is not None:
            receipt["running_latch"] = block

    failure = {"error": None}

    def readiness():
        """Control's readiness token, or None when the session has no probe; a probe that
        cannot answer reads as ``state_unavailable`` (unready)."""
        probe = getattr(session, "control_readiness", None)
        if not callable(probe):
            return None
        try:
            return _closed_token(probe()) or "state_unavailable"
        except Exception:
            return "state_unavailable"

    def note_refusal(error, step):
        """Bounded evidence of a control refusal (also kept as the receipt's latest), or None."""
        if error is None:
            return None
        cached = getattr(error, "live_check_refusal", None)
        if isinstance(cached, dict):
            return cached
        try:
            refusal = _refusal(error)
            if refusal is None:
                return None
            kind, http_status, detail, verified = refusal
            call = getattr(error, "live_check_call", None)
            evidence = {
                "kind": kind,
                "step": step if step in REFUSAL_STEPS else "run",
                "call": call if call in CONTROL_CALLS else None,
                "http_status": http_status,
                "detail": detail,
                "verified_authority_gone": verified,
                "control_readiness": readiness() if kind == "forbidden" else None,
                "skipped": False,
            }
        except Exception:
            return None
        try:
            error.live_check_refusal = evidence
        except Exception:
            pass
        receipt["control_refusal"] = evidence
        receipt.setdefault("control_refusals_skipped", 0)
        return evidence

    def control_check():
        """Raise ControlCancelRequested when control's own authoritative record (a fresh read of
        the route's grant, operation and lease; ``SaladLiveSession.execution_authority``) says
        it decided to stop the route. Asked once per poll interval, after the heartbeat, so a
        cancel ends the run even when nothing else refuses. A read that fails is never a cancel:
        it is counted (``control_authority_read_failures``, ``last_control_authority_read_failure``,
        sanitized) and the existing stall / hold handling applies. A session without the read
        (isolated fakes) is not asked. If the guardian already retained the output, the pass
        goes on so the run completes (the next evidence read decides) instead of cancelling."""
        reader = getattr(session, "execution_authority", None)
        if not callable(reader):
            return
        try:
            facts = reader(route_id)
            status, desired, current = (
                facts["grant_status"],
                facts["operation_desired"],
                facts["epoch_current"],
            )
            if type(status) is not str or type(desired) is not str or type(current) is not bool:
                raise ValueError("execution authority malformed")
        except Exception as error:
            try:
                receipt["control_authority_read_failures"] = (
                    receipt.get("control_authority_read_failures", 0) + 1
                )
                receipt["last_control_authority_read_failure"] = _error_detail(error)
            except Exception:
                pass
            return
        if status == "active" and desired == "admitted" and current:
            return
        receipt["control_cancel"] = {
            "grant_status": _closed_token(status),
            "operation_desired": _closed_token(desired),
            "epoch_current": current,
        }
        try:
            retained = session.evidence(route_id)["output_retained"] is True
        except Exception:
            retained = False  # unread: control's decision stands (fail closed)
        receipt["control_cancel"]["output_retained"] = retained
        if not retained:
            raise ControlCancelRequested()

    def beat_then_check(launched):
        """The guardian beat (once launched), then control's authority check, so a held write
        lock on the authority read never pushes the beat past its staleness bound. When the
        beat itself fails, control's record is still read first: a cancel explains that
        refusal and ends the run as control's cancel; otherwise the beat's failure stands."""
        if launched:
            try:
                heartbeat()
            except Exception:
                control_check()
                raise
        control_check()

    def refusal_stall(evidence):
        """True when a refusal on a running route is a transient control stall, not a cancel:
        a Forbidden/401/403 without a verified revocation, or any while control is unready."""
        return (
            evidence is not None
            and evidence["kind"] == "forbidden"
            and running()
            and (
                not evidence["verified_authority_gone"]
                or evidence["control_readiness"] not in {None, "ready"}
            )
        )

    def skip_refusal(evidence):
        evidence["skipped"] = True
        receipt["control_refusals_skipped"] += 1

    def cancel_cause(error):
        """The closed reason this driver sends its cancel (receipt ``cancel_cause``)."""
        from .salad_server import ProviderReadUnavailable
        from .store import Conflict, Forbidden

        refusal = receipt["guardian_refusal"]
        code = refusal["code"] if refusal else (None if error is None else _guardian_code(error))
        if execution_complete or _final_reason(receipt["reason"]):
            kind = "execution_complete" if execution_complete else "guardian_stop_cause"
        elif receipt["guardian_stop_cause"] not in {None, "control_cancel"}:
            # ``control_cancel`` is a stop control asked for, not the guardian's own decision.
            kind = "guardian_stop_cause"
        elif clock() >= deadline:
            kind = "deadline"
        elif _budget_code(code):
            kind = "budget"
        elif isinstance(error, ControlCancelRequested):
            kind = "control_cancel_requested"
        elif ended["class"] is not None:
            kind = ended["class"]
        elif code == "unavailable":
            kind = "guardian_unavailable"
        elif code is not None:
            kind = "guardian_refusal"
        elif step["name"] is not None or receipt["create_rejection"] is not None:
            kind = "prestart"
        elif (
            isinstance(error, (Conflict, Forbidden))
            and not isinstance(error, ProviderReadUnavailable)
        ) or _refusal(error) is not None:
            kind = "control_refused"
        else:
            kind = "error"
        try:
            silent = None if beats["ok_at"] is None else round(max(0, clock() - beats["ok_at"]), 1)
        except Exception:
            silent = None
        return {
            "class": kind,
            "exception": None if error is None else _exception_type_name(error),
            "guardian_refusal": _closed_token(code),
            "last_provider_status": receipt["last_provider_status"],
            "heartbeat_silent_seconds": silent,
            # Which call control refused, its HTTP status and detail (bounded, never secret).
            "refusal": note_refusal(error, "run"),
            # The ending error, sanitized and bounded (_error_detail): message, cause class,
            # closed detail and raising site, so the cancel is diagnosable from the receipt.
            "error": _error_detail(error),
        }

    def terminal(error, refusal):
        """True when ``error`` is a genuine terminal cause even on a running route: a guardian
        refusal that is its own decision (budget included), or an explicit control decision
        (a verified revocation from a ready control, a Conflict other than a revision race)."""
        if isinstance(error, ControlCancelRequested):
            return True  # control's own recorded decision (control_check), never a stall
        code = _guardian_code(error)
        if code is not None:
            return code not in HELD_GUARDIAN_CODES
        if refusal is None:
            return False
        if refusal["kind"] == "conflict":
            return conflict_reason(error) not in TRANSIENT_CONFLICT_REASONS
        ready = refusal["control_readiness"] in {None, "ready"}
        return refusal["verified_authority_gone"] and ready

    def hold(error, where):
        """True when an error in the post-start loop leaves a running route running.

        Only while the last observe read running, the guardian has recorded no stop cause, the
        deadline has not passed and the error is not a genuine terminal cause (``terminal``).
        The error is recorded sanitized; the caller retries at the next interval."""
        if not isinstance(error, Exception):
            return False
        try:
            if not running() or receipt["guardian_stop_cause"] is not None or clock() >= deadline:
                return False
            refusal = note_refusal(error, where if where in REFUSAL_STEPS else "run")
            if terminal(error, refusal):
                return False
            if refusal is not None and not refusal["skipped"]:
                skip_refusal(refusal)
            detail = _error_detail(error)
            code = _guardian_code(error)
        except Exception as internal:
            # The classification itself failed: fail toward hold on a latched route with no
            # guardian stop cause (the guardian keeps its own deadline, budget and silence
            # authority, and the loop still ends at the run deadline), else end as before.
            return hold_unclassified(error, where, internal)
        # A held beat or read is not the ending cause: no bound a helper gave up with, and no
        # guardian refusal, may linger into a later cancel's class.
        ended["class"] = None
        if code is not None:
            receipt["guardian_refusal"] = None
        receipt["running_errors_held"] = receipt.get("running_errors_held", 0) + 1
        receipt["last_held_error"] = dict(
            detail,
            step=where if where in HELD_ERROR_STEPS else "record",
            guardian_refusal=_closed_token(code),
            heartbeat_silent_seconds=(
                None if beats["ok_at"] is None else round(max(0, clock() - beats["ok_at"]), 1)
            ),
        )
        return True

    def hold_unclassified(error, where, internal):
        """hold() when its own classification raised: True (held, minimally recorded) on a
        latched route with no guardian stop cause; never raises."""
        try:
            if not latch.engaged() or receipt.get("guardian_stop_cause") is not None:
                return False
        except Exception:
            return False
        try:
            ended["class"] = None
            receipt["running_errors_held"] = receipt.get("running_errors_held", 0) + 1
            receipt["running_classifier_errors_held"] = (
                receipt.get("running_classifier_errors_held", 0) + 1
            )
            receipt["last_held_error"] = {
                "exception": _exception_type_name(error),
                "message": UNRECOGNIZED_DETAIL,
                "step": where if where in HELD_ERROR_STEPS else "record",
                "classifier_error": _exception_type_name(internal),
            }
        except Exception:
            pass
        return True

    evidence = {"output_retained": False}
    # Successful observe-and-evidence passes; read only by the optional watch snapshot.
    watch = {"polls": 0}

    def watch_poll(polled, proof):
        watch["polls"] += 1
        _observe_watch(watch_signals_file, state=polled, evidence=proof, polls=watch["polls"])

    def skip_read(error, step):
        """True when a failed post-start read may be skipped to the next interval.

        A provider read failure or a stall is skipped at most MAX_TRANSIENT_OBSERVE_FAILURES - 1
        times in a row; while the route last read running, a transient one (timeout, control
        stall, non-deterministic read failure, an unverified refusal) is skipped without that
        bound: the guardian, not this driver, decides to stop a running group. Anything else
        that reaches the post-start loop on a running route is held there (``hold``)."""
        evidence = note_refusal(error, step)
        if refusal_stall(evidence):
            skip_refusal(evidence)
            note_skipped(error, step)
            return True
        if not _is_read_failure(error):
            return False
        if not (running() and _is_read_stall(error)):
            if observes["failed"] >= MAX_TRANSIENT_OBSERVE_FAILURES - 1:
                ended["class"] = "observe_timeout"
                return False
        observes["failed"] += 1
        receipt["observe_transient_failures"] += 1
        note_skipped(error, step)
        return True

    def note_skipped(error, step):
        """The latest skipped read's sanitized detail (``last_skipped_read``; absent until one)."""
        detail = _error_detail(error)
        if detail is not None:
            receipt["last_skipped_read"] = dict(detail, step=step)

    def observe():
        """dispatch("observe"), or None after a skippable read failure (skip_read)."""
        try:
            observed = dispatch("observe")
        except Exception as error:
            if not skip_read(error, "observe"):
                raise
            return None
        observes["failed"] = 0
        return observed

    def heartbeat():
        started = clock()
        if beats["ok_at"] is None:
            beats["ok_at"] = started  # silence is measured from the first attempt
        try:
            session.keepalive(route_id)
        except Exception as error:
            code = _guardian_code(error)
            cause = "terminal_code"
            refused = note_refusal(error, "keepalive") if code is None else None
            refused_stall = refusal_stall(refused)
            if code is None and (_is_stall(error) or refused_stall):
                # A control stall (write lock, bounded local call ran out, or an unverified
                # refusal while running). When the guardian still took its beat, only the lease
                # renewal was skipped: tolerated while the route runs (the next beat re-runs
                # authority), else a few times in a row. Without a delivered beat it is
                # tolerated only inside the silence bound, except on a route last read
                # running with no guardian stop cause: a control stall is never grounds for
                # this driver to stop that route, however long it lasts. The guardian keeps
                # its own silence, deadline and budget authority and stops the group itself;
                # the run deadline still ends this loop. The beat is retried next interval.
                if not refused_stall:
                    receipt["control_stalls"] += 1
                beats["stalls"] += 1
                delivered = isinstance(error, ControlStall) and error.beat_delivered
                if delivered:
                    beats["ok_at"], beats["transient"] = started, 0
                silent = clock() - beats["ok_at"] + interval
                quiet = silent <= GUARDIAN_SILENCE_SECONDS - GUARDIAN_SILENCE_MARGIN_SECONDS
                # Held: a lock / bounded-call stall, or a refusal while control is unready (an
                # unverified refusal from a ready control keeps the silence bound).
                held = (
                    running()
                    and receipt["guardian_stop_cause"] is None
                    and (not refused_stall or refused["control_readiness"] not in {None, "ready"})
                )
                if held or (
                    (delivered or quiet)
                    and (running() or beats["stalls"] <= MAX_TRANSIENT_CONTROL_STALLS)
                ):
                    if refused_stall:
                        skip_refusal(refused)
                    return
                ended["class"] = "control_stall" if delivered or quiet else "silence_bound"
            elif code in TRANSIENT_GUARDIAN_CODES:
                # The next beat starts about one interval from now; it must still
                # reach the guardian well inside its silence bound.
                silent = clock() - beats["ok_at"] + interval
                if (
                    beats["transient"] < MAX_TRANSIENT_GUARDIAN_REFUSALS
                    and silent <= GUARDIAN_SILENCE_SECONDS - GUARDIAN_SILENCE_MARGIN_SECONDS
                ):
                    beats["transient"] += 1
                    receipt["guardian_transient_refusals"] += 1
                    return
                cause = "silence_bound"
                ended["class"] = "silence_bound"
            # Once the approved output is retained the guardian stops the
            # group and closes writers, so it refuses further heartbeats.
            # That is completion; any other refusal is still a failure.
            # The guardian is busy stopping and sealing exactly then, so a stalled status read is
            # retried a few times; if every try stalls the refusal is undecided (see
            # MAX_UNDECIDED_REFUSALS): the next pass's evidence read decides. A failed read never
            # counts as "retained". What was read is kept as ``completion_status_read``.
            retained, read_error, tries = None, None, 0
            for tries in range(1, COMPLETION_STATUS_READ_ATTEMPTS + 1):
                try:
                    retained = session.evidence(route_id)["output_retained"] is True
                    break
                except Exception as status_error:
                    read_error = status_error
                    if not _is_read_stall(status_error):
                        retained = False
                        break
                    if tries < COMPLETION_STATUS_READ_ATTEMPTS:
                        sleep(COMPLETION_STATUS_READ_PAUSE_SECONDS)
            receipt["completion_status_read"] = {
                "attempts": tries,
                "outcome": {True: "retained", False: "not_retained", None: "unavailable"}[retained],
                "error": _error_detail(read_error),
            }
            if (
                retained is None
                and beats["undecided"] < MAX_UNDECIDED_REFUSALS
                and code is not None
                and not _budget_code(code)
                and _completion_stop_cause(receipt)
                and clock() < deadline
            ):
                beats["undecided"] += 1
                receipt["undecided_refusals"] = receipt.get("undecided_refusals", 0) + 1
                return
            if not retained:
                if hold(error, "keepalive"):
                    # Running, no guardian stop cause, not a genuine terminal cause: the beat
                    # is retried next interval and this pass still observes the group.
                    return
                if code is not None:
                    receipt["guardian_refusal"] = {"code": code, "cause": cause}
                raise
            return
        beats["ok_at"], beats["transient"], beats["stalls"] = started, 0, 0
        beats["undecided"] = 0

    def note_settle(call, error, attempts, elapsed=0.0):
        reason, status_class, transient = _settle_read_failure(error)
        receipt["prestart_settle_read"] = {
            "call": call,
            "reason": reason,
            "http_status_class": status_class,
            "attempts": attempts,
            "outcome": "failed",
            "elapsed_seconds": elapsed,
        }
        return transient

    def settle_beat():
        """heartbeat() during the settle wait; a failure that escapes it is recorded."""
        try:
            heartbeat()
        except Exception as error:
            note_settle("keepalive", error, 1)
            raise

    def settle_read():
        """One exact observe of the new group during the settle wait.

        A transient failure is retried inside SETTLE_READ_WINDOW_SECONDS (and
        SETTLE_READ_ATTEMPTS), never past the run deadline, heartbeating the guardian in
        between; anything else, or the last failure, propagates unchanged (the run then fails
        closed as group_unsettled and the group is never treated as settled). Each failure
        leaves its closed reason, attempts and elapsed seconds on the receipt; a read that
        succeeded after a retry is marked recovered."""
        attempts = 0
        started = clock()
        while True:
            attempts += 1
            try:
                observed = dispatch("observe")
            except Exception as error:
                elapsed = round(clock() - started, 1)
                transient = note_settle("observe", error, attempts, elapsed)
                pause = SETTLE_READ_BACKOFF_SECONDS[
                    min(attempts - 1, len(SETTLE_READ_BACKOFF_SECONDS) - 1)
                ]
                if (
                    not transient
                    or attempts >= SETTLE_READ_ATTEMPTS
                    or clock() >= deadline
                    or clock() - started + pause >= SETTLE_READ_WINDOW_SECONDS
                ):
                    raise
                settle_beat()
                sleep(min(pause, max(0, deadline - clock())))
                continue
            if attempts > 1:
                receipt["prestart_settle_read"].update(
                    attempts=attempts,
                    outcome="recovered",
                    elapsed_seconds=round(clock() - started, 1),
                )
            return observed

    from . import salad_precreate as _precreate

    try:
        # This is a read of the durable record, not a new admission. A rerun or
        # reconnect with existing effects never reaches create/start.
        step["name"] = "route_state_unreadable"
        state = session.state(route_id)
        original_digest = state["proposal_digest"]
        receipt["input_expected"] = session.input_expected(route_id) is True
        reader = getattr(session, "input_data_class", None)
        if receipt["input_expected"] and callable(reader):
            receipt["input_data_class"] = reader(route_id)
        code_reader = getattr(session, "input_code", None)
        if receipt["input_expected"] and callable(code_reader):
            code = code_reader(route_id)
            receipt["input_code"] = code["input_code"]
            receipt["input_code_files"] = code["input_code_files"]
        elif receipt["input_expected"]:
            receipt["input_code"] = "unknown"
        prior_launches = [e for e in state["effects"] if e["action"] in {"create", "start"}]
        latch.note_effects(state)  # a reconnect to a started route is protected from the start
        note_latch()
        # The state read succeeded. A reconnect or rerun skips create/start, so any later
        # failure is post-start and must not carry a pre-start class.
        step["name"] = None
        if not prior_launches:
            launched = True  # lost create reply still requires exact stop/reconcile
            step["name"] = "create_failed"
            # A failed step before the create intent is retried (bounded) only while a fresh
            # route read shows no create effect; once one exists nothing is retried (ADR 0205).
            created = _precreate.dispatch_create(
                dispatch,
                lambda: session.state(route_id),
                clock=clock,
                sleep=sleep,
                deadline=deadline,
                receipt=receipt,
            )
            stamp("create")  # stamped even if the create is rejected just below
            receipts = created.get("receipts") if isinstance(created, dict) else None
            created = receipts[-1] if isinstance(receipts, list) and receipts else {}
            rejected = isinstance(created, dict) and (
                created.get("proxy_reason") in {"provider_rejected_request", "request_not_sent"}
            )
            if rejected:
                # Report why, then fall through: session.cancel below fences the route
                # (the guardian already set stop-only recovery on the report) and the
                # guardian releases the reservation only on its own absence reads.
                receipt["create_rejection"] = created.get("provider_rejection")
                raise ValueError("create rejected")
            # A new group settles from pending to stopped after create (observed live), and
            # can read stopped while a change is still in flight; poll the exact group within
            # the same bounds until it reads stopped AND settled (no change in flight, no
            # instance: the guardian's terminal reading) before start. A group that reads
            # stopped but never reports a settled shape is handed to start after a bounded
            # wait, where the transport's own exact read decides.
            step["name"] = "group_unsettled"
            polls = 0
            stopped_since = None
            settle_started = clock()
            settle_retried = False
            for _ in range(max_polls):
                # The guardian beat first: the authority read never delays it.
                settle_beat()
                control_check()
                try:
                    state = settle_read()
                except Exception as error:
                    if settle_retried or conflict_reason(error) not in TRANSIENT_CONFLICT_REASONS:
                        raise
                    # Known transient: the route advanced under the read. Observe is a read;
                    # retry it once. Anything else (or a second conflict) fails closed.
                    settle_retried = True
                    receipt["settle_conflict_retries"] += 1
                    sleep(min(1, max(0, deadline - clock())))
                    continue
                polls += 1
                if state.get("provider_id") and receipt["prestart_timestamps"]["bound"] is None:
                    stamp("bound")
                if state["provider_status"] == "stopped":
                    stopped_since = clock() if stopped_since is None else stopped_since
                    # The guardian's state always carries termination_confirmed (true only for
                    # a stopped group with no change in flight and no instance); a session
                    # that does not report it gives no settle signal to wait for.
                    if (
                        state.get("termination_confirmed", True) is True
                        or clock() - stopped_since >= PRESTART_SETTLE_SECONDS
                    ):
                        break
                if clock() >= deadline:
                    break
                sleep(min(interval, max(0, deadline - clock())))
            receipt["prestart_settle"] = {
                "polls": polls,
                "provider_status": state["provider_status"],
                "settled": state.get("termination_confirmed", True) is True,
                # What the wait cost: a group that never reports settled (counts Salad did not
                # send) spends the whole bound before every start; this makes that visible.
                "waited_seconds": round(clock() - settle_started, 1),
            }
            stamp("settle")  # the settle wait ended; the stopped check follows
            if state["provider_status"] != "stopped":
                raise ValueError("create unresolved")
            if receipt["input_expected"]:
                # The group exists but is stopped (unbilled). Delivery must be confirmed
                # before start; a failure here never starts it and falls through to the
                # exact cancel/stop below.
                step["name"] = "prestart_unclassified"
                session.deliver_inputs(route_id)
                receipt["input_delivered"] = True
                stamp("deliver_inputs")
            step["name"] = "start_failed"
            stamp("start_preflight")  # the start dispatch begins with its fresh provider read
            dispatch_start()
            latch.note_start()  # the start effect is recorded: protect provisioning as well
            note_latch()
            stamp("start")
            step["name"] = None
        for _ in range(max_polls):
            if clock() >= deadline:
                break
            # A pass on a running route that raises anything but a genuine terminal cause is
            # held (``hold``): recorded sanitized, retried next interval, never a cancel.
            where = {"step": "keepalive"}
            try:
                beat_then_check(launched)
                began = clock()
                where["step"] = "observe"
                observed = observe()
                if observed is None:
                    sleep(min(interval, max(0, deadline - clock())))
                    continue
                state = observed
                # A latched route records only an affirmative observation's status: a failed,
                # blocked or unknown read never overwrites it (nor lifts the latch). A partial
                # reply that names no status never turns a running route into an unknown one.
                record_status = latch.note_observe(state)
                note_latch()
                if record_status and (state.get("provider_status") is not None or not running()):
                    receipt["last_provider_status"] = _closed_token(state.get("provider_status"))
                where["step"] = "evidence"
                try:
                    polled = session.evidence(route_id)
                except Exception as error:
                    # The guardian status read is a read too: a stall skips this pass like a
                    # failed observe (a dead guardian still ends the run at the next heartbeat).
                    if not skip_read(error, "evidence"):
                        raise
                    sleep(min(interval, max(0, deadline - clock())))
                    continue
                where["step"] = "record"
                evidence = polled
                watch_poll(state, evidence)
                receipt["financial"] = _financial(evidence.get("financial", {}))
                receipt["guardian_rate_read_retries"] = evidence.get("rate_read_retries")
                receipt["guardian_provider_read_retries"] = evidence.get("provider_read_retries")
                receipt["guardian_prestart_read_retries"] = evidence.get("prestart_read_retries")
                receipt["guardian_stop_cause"] = evidence.get("stop_cause")
                _note_start_evidence(receipt, evidence)
                receipt["custody_work_begun"] = evidence.get("custody_work_begun")
                receipt["guardian_last_workload_refusal"] = evidence.get("last_workload_refusal")
                receipt["guardian_last_rate_read_failure"] = evidence.get("last_rate_read_failure")
                receipt["guardian_last_provider_read_failure"] = evidence.get(
                    "last_provider_read_failure"
                )
                receipt["guardian_last_provider_read_failure_reason"] = evidence.get(
                    "last_provider_read_failure_reason"
                )
                receipt["guardian_last_provider_read_failure_detail"] = evidence.get(
                    "last_provider_read_failure_detail"
                )
                receipt["guardian_last_provider_read_failure_field"] = evidence.get(
                    "last_provider_read_failure_field"
                )
                receipt["guardian_last_provider_read_failure_request_kind"] = evidence.get(
                    "last_provider_read_failure_request_kind"
                )
                if evidence.get("stop_cause") == "allocation_timeout" or (
                    evidence.get("stop_cause") == "deadline"
                    and _allocation_timeout(evidence.get("allocation_timeout")) is not None
                ):
                    # The guardian cancelled the group: no instance was ever allocated in time
                    # (its own deadline or provisioning bound, or the route limit reached a group
                    # that never ran). Nothing was placed, so there is no output to import: stop
                    # polling; the reason is kept below.
                    receipt["reason"] = "allocation_timeout"
                    receipt["allocation_timeout"] = evidence.get("allocation_timeout")
                    break
                if str(evidence.get("stop_cause") or "").startswith(WORKLOAD_FAILED_CAUSE_PREFIX):
                    # The workload reported its own failure and the guardian stopped the group:
                    # the run failed for that reason (never a provider move). Stop polling.
                    receipt["reason"] = evidence["stop_cause"]
                    break
                if evidence.get("stop_cause") == START_FAILURE_REASON:
                    # The guardian saw the container fail to start on several machines before any
                    # custody began: stop polling; the reason is kept below.
                    receipt["reason"] = START_FAILURE_REASON
                    break
                if evidence["output_retained"]:
                    break
                # Spread polls over the approved window; stay well inside the guardian's
                # 30s heartbeat staleness bound so a live run is not stopped for silence. A slow
                # observe or evidence read spends part of the interval, so only the rest is slept.
                sleep(min(max(0, interval - (clock() - began)), max(0, deadline - clock())))
                if state["provider_status"] in PRESTART_PROVIDER_STATUSES and latch.slow_cadence(
                    state, PRESTART_PROVIDER_STATUSES
                ):
                    # Stretch only the observe cadence: keep heartbeating at the normal
                    # interval until the backed-off observe is due.
                    waited = interval
                    where["step"] = "keepalive"
                    while waited < PRESTART_POLL_INTERVAL_SECONDS and clock() < deadline:
                        beat_then_check(launched)
                        slice_seconds = min(interval, PRESTART_POLL_INTERVAL_SECONDS - waited)
                        sleep(min(slice_seconds, max(0, deadline - clock())))
                        waited += slice_seconds
            except Exception as error:
                if not hold(error, where["step"]):
                    raise
                sleep(min(interval, max(0, deadline - clock())))
                continue
        else:
            ended["class"] = "poll_bound"
            raise ValueError("poll bound")
        if (
            not evidence["output_retained"]
            and clock() >= deadline
            and not _final_reason(receipt["reason"])
        ):
            # The loop ends at the deadline before its next read: an output the guardian
            # retained in that last interval is still the run's output (one bounded read).
            try:
                last = session.evidence(route_id)
                if last.get("output_retained") is True:
                    evidence = last
            except Exception as error:
                note_tail_error("final_evidence", error)
        # Retained output completes the run, even as the deadline passes.
        if not evidence["output_retained"]:
            ended["class"] = ended["class"] or "output_unavailable"
            raise ValueError("output unavailable")
        execution_complete = True
    except Exception as error:
        failure["error"] = error
        if not _final_reason(receipt["reason"]):
            receipt["reason"] = "execution_or_output_unresolved"
        if step["name"] is not None and receipt["create_rejection"] is None:
            receipt["prestart_failure"] = _prestart_failure(error, step["name"])
    finally:
        # Even a failed dispatch/preflight or existing uncertain group gets one
        # exact stop attempt. Never delete, choose a new target, or retry stop.
        # Why it is sent is recorded first, as a closed class (never exception text).
        receipt["cancel_cause"] = cancel_cause(failure["error"])
        # True once one of this driver's own cancel calls recorded the cancel at control (the
        # route did not already say stop): only then is a guardian ``control_cancel`` stop cause
        # this driver's own (see _completion_stop_cause).
        receipt["cancel_recorded_by_driver"] = False
        for attempt in range(1, CANCEL_ATTEMPTS + 1):
            try:
                if session.cancel(route_id) is True:
                    receipt["cancel_recorded_by_driver"] = True
                receipt["cancel_recorded"] = True
                receipt["cancel_failure"] = None
                break
            except GuardianCancelUndelivered as error:
                # Control recorded the cancel; only the guardian's own cancel call failed. The
                # cancel is recorded (a pass is still possible), the delivery failure is kept on
                # the receipt and retried while it is transient. The exact stop below and the
                # guardian's sealed stop still decide the run.
                receipt["cancel_recorded"] = True
                if getattr(error, "recorded_here", False) is True:
                    receipt["cancel_recorded_by_driver"] = True
                receipt["cancel_failure"] = None
                root = error.__cause__ if error.__cause__ is not None else error
                receipt["guardian_cancel_failure"] = {
                    "exception": _exception_type_name(root),
                    "attempts": attempt,
                    "guardian_refusal": _closed_token(_guardian_code(root)),
                }
                note_tail_error("guardian_cancel", error, attempt)
                if attempt == CANCEL_ATTEMPTS or not _tail_transient(error):
                    break
                sleep(CANCEL_RETRY_SECONDS)
            except Exception as error:
                receipt["cancel_recorded"] = False
                root = error
                if isinstance(error, CancelUnrecorded) and error.__cause__ is not None:
                    root = error.__cause__
                receipt["cancel_failure"] = {
                    "exception": _exception_type_name(root),
                    "attempts": attempt,
                    "guardian_cancel_sent": getattr(error, "guardian_cancel_sent", None),
                }
                note_tail_error("cancel", error, attempt)
                # Only a control stall or a route-revision race is retried (the same idempotent
                # cancel); a refusal is final. The failure stays on the receipt and in its
                # reason either way.
                if attempt == CANCEL_ATTEMPTS or not (
                    _is_stall(root) or conflict_reason(root) in TRANSIENT_CONFLICT_REASONS
                ):
                    break
                sleep(CANCEL_RETRY_SECONDS)
        if receipt["cancel_recorded"] is False and not _final_reason(receipt["reason"]):
            receipt["reason"] = CANCEL_UNRECORDED_REASON
        # Completion, failure and cancel alike: drop the guardian's ticket and revoke it at
        # control. Best effort; the ticket's own expiry (at most an hour) bounds a failure.
        try:
            receipt["input_revoked"] = bool(session.revoke_inputs(route_id))
        except Exception:
            receipt["input_revoked"] = False
        try:
            # A completed run waits longer for custody to seal (CUSTODY_SEAL_SECONDS); the
            # group is stopped meanwhile. One exact stop/reconciliation may extend past the
            # scheduling deadline.
            stop_deadline = max(
                deadline,
                clock() + (CUSTODY_SEAL_SECONDS if execution_complete else STOP_CONFIRM_SECONDS),
            )

            def send_stop():
                # Re-read before every try: once a stop effect exists (its intent is committed
                # before the provider call) it is never sent again; only its preflight read is
                # retried while it fails transiently.
                existing = session.state(route_id)
                if not any(effect["action"] == "stop" for effect in existing["effects"]):
                    dispatch("stop")

            tail_retry("stop", send_stop, attempts=STOP_DISPATCH_ATTEMPTS, until=stop_deadline)
            stop_read_failures = 0
            read_failure_bound = (
                TAIL_READ_FAILURES if execution_complete else MAX_TRANSIENT_OBSERVE_FAILURES
            )
            # Seal waits (below) have their own bound and do not spend the stop poll bound.
            seal_waits_left = math.ceil(CUSTODY_SEAL_SECONDS / interval)
            stop_polls = 0
            while stop_polls < max_polls:
                stop_polls += 1
                try:
                    state = dispatch("observe")
                    evidence = session.evidence(route_id)
                except Exception as error:
                    # A stalled read while confirming the stop is retried a bounded number
                    # of times in a row; anything else (or the last) leaves it unresolved.
                    note_tail_error("stop_confirm", error, stop_read_failures + 1)
                    stop_read_failures += 1
                    transient = _is_read_stall(error) or (
                        execution_complete and _tail_transient(error)
                    )
                    if (
                        not transient
                        or stop_read_failures >= read_failure_bound
                        or clock() >= stop_deadline
                    ):
                        raise
                    sleep(min(interval, max(0, stop_deadline - clock())))
                    continue
                stop_read_failures = 0
                watch_poll(state, evidence)
                receipt["financial"] = _financial(evidence.get("financial", {}))
                receipt["guardian_rate_read_retries"] = evidence.get("rate_read_retries")
                receipt["guardian_provider_read_retries"] = evidence.get("provider_read_retries")
                receipt["guardian_prestart_read_retries"] = evidence.get("prestart_read_retries")
                receipt["guardian_stop_cause"] = evidence.get("stop_cause")
                _note_start_evidence(receipt, evidence)
                receipt["custody_work_begun"] = evidence.get("custody_work_begun")
                receipt["guardian_last_workload_refusal"] = evidence.get("last_workload_refusal")
                receipt["guardian_last_rate_read_failure"] = evidence.get("last_rate_read_failure")
                receipt["guardian_last_provider_read_failure"] = evidence.get(
                    "last_provider_read_failure"
                )
                receipt["guardian_last_provider_read_failure_reason"] = evidence.get(
                    "last_provider_read_failure_reason"
                )
                receipt["guardian_last_provider_read_failure_detail"] = evidence.get(
                    "last_provider_read_failure_detail"
                )
                receipt["guardian_last_provider_read_failure_field"] = evidence.get(
                    "last_provider_read_failure_field"
                )
                receipt["guardian_last_provider_read_failure_request_kind"] = evidence.get(
                    "last_provider_read_failure_request_kind"
                )
                if evidence.get("provider_stop_confirmed"):
                    receipt["provider_stop_confirmed"] = True
                if evidence["stop_confirmed"]:
                    receipt["stop_confirmed"] = True
                    break
                if evidence.get("provider_stop_confirmed"):
                    precustody = (
                        receipt["guardian_stop_cause"] == "allocation_changed"
                        and receipt["custody_work_begun"] is False
                    )
                    if (
                        execution_complete
                        and not precustody
                        and _completion_stop_cause(receipt)
                        and clock() < stop_deadline
                        and seal_waits_left > 0
                    ):
                        seal_waits_left -= 1
                        stop_polls -= 1
                        # A finished run: the provider stops first, then the guardian seals
                        # the retained output. Spend is closed, so keep reading (bounded by
                        # CUSTODY_SEAL_SECONDS) until custody seals; never fail it early.
                        receipt["custody_seal_waits"] = receipt.get("custody_seal_waits", 0) + 1
                        sleep(min(interval, max(0, stop_deadline - clock())))
                        continue
                    # Spend closed but custody cannot seal (an allocation change or another
                    # guardian stop, or a completed run past its seal window): stop waiting.
                    if not _final_reason(receipt["reason"]) and (
                        precustody or receipt["reason"] != CANCEL_UNRECORDED_REASON
                    ):
                        receipt["reason"] = (
                            PRECUSTODY_REALLOCATION
                            if precustody
                            else "provider_stopped_custody_unsealed"
                        )
                    break
                if clock() >= stop_deadline:
                    break
                sleep(min(interval, max(0, stop_deadline - clock())))
        except Exception:
            if not _final_reason(receipt["reason"]) and (
                receipt["reason"] != CANCEL_UNRECORDED_REASON
            ):
                receipt["reason"] = "stop_unresolved"
    try:
        # A cancel control never recorded is not a settled stop, whatever else was seen.
        if (
            not execution_complete
            or not receipt["stop_confirmed"]
            or receipt["cancel_recorded"] is not True
        ):
            raise ValueError("stop unresolved")

        def verify():
            """One full verification of the confirmed, sealed stop (evidence, import, reconnect).

            Every read is idempotent (sealed custody is immutable after stop), so a transient
            failure is retried as a whole by ``tail_retry``; an integrity, binding or identity
            mismatch is not."""
            digest = None
            evidence = session.evidence(route_id)
            receipt["financial"] = _financial(evidence.get("financial", {}))
            receipt["guardian_rate_read_retries"] = evidence.get("rate_read_retries")
            receipt["guardian_provider_read_retries"] = evidence.get("provider_read_retries")
            receipt["guardian_prestart_read_retries"] = evidence.get("prestart_read_retries")
            receipt["guardian_stop_cause"] = evidence.get("stop_cause")
            _note_start_evidence(receipt, evidence)
            receipt["custody_work_begun"] = evidence.get("custody_work_begun")
            receipt["guardian_last_workload_refusal"] = evidence.get("last_workload_refusal")
            receipt["guardian_last_rate_read_failure"] = evidence.get("last_rate_read_failure")
            receipt["guardian_last_provider_read_failure"] = evidence.get(
                "last_provider_read_failure"
            )
            receipt["guardian_last_provider_read_failure_reason"] = evidence.get(
                "last_provider_read_failure_reason"
            )
            receipt["guardian_last_provider_read_failure_detail"] = evidence.get(
                "last_provider_read_failure_detail"
            )
            receipt["guardian_last_provider_read_failure_field"] = evidence.get(
                "last_provider_read_failure_field"
            )
            receipt["guardian_last_provider_read_failure_request_kind"] = evidence.get(
                "last_provider_read_failure_request_kind"
            )
            if not evidence["output_retained"] or not evidence["writers_quiescent"]:
                raise ValueError("custody unresolved")
            multi = evidence.get("outputs") is not None
            if multi:
                # Declared multi-file outputs: per-file sha256 recomputed from import.
                retained = session.retained_files(route_id)
                if retained != evidence["outputs"]:
                    raise ValueError("output integrity")
                if sum(f["bytes"] for f in retained) > evidence["output_byte_cap"]:
                    raise ValueError("output bound")
            else:
                retained = session.retained_bytes(route_id)
                if not 1 <= len(retained) <= evidence["output_byte_cap"]:
                    raise ValueError("output bound")
                digest = hashlib.sha256(retained).hexdigest()
                if digest != evidence["output_sha256"]:
                    raise ValueError("output integrity")
            before = session.state(route_id)
            reopened = session.reopen()
            try:
                after = reopened.state(route_id)
                if (
                    before["effects"] != after["effects"]
                    or after["proposal_digest"] != original_digest
                ):
                    raise ValueError("reconnect identity")
                again = (
                    reopened.retained_files(route_id)
                    if multi
                    else reopened.retained_bytes(route_id)
                )
                if again != retained:
                    raise ValueError("reconnect custody")
            finally:
                if reopened is not session and type(reopened) is SaladLiveSession:
                    reopened.close()
            identity = evidence["provider_id"]
            if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,200}", identity):
                raise ValueError("provider identity")
            return evidence, retained, multi, digest, identity

        evidence, retained, multi, digest, identity = tail_retry(
            "verify", verify, attempts=VERIFY_ATTEMPTS, until=clock() + VERIFY_RETRY_SECONDS
        )
        receipt.update(
            status="passed",
            reason="verified_journey",
            reconnected=True,
            provider_id=identity,
            new_launch_attempted=launched,
        )
        if multi:
            receipt.update(outputs=retained, output_bytes=sum(f["bytes"] for f in retained))
        else:
            receipt.update(output_sha256=digest, output_bytes=len(retained))
    except Exception:
        if receipt["create_rejection"] is not None:
            # Status 0: no HTTP exchange happened, the create was never sent.
            receipt["reason"] = (
                "create_not_sent"
                if receipt["create_rejection"].get("status") == 0
                else "provider_rejected_request"
            )
        elif _final_reason(receipt["reason"]) or receipt["reason"] in {
            PRECUSTODY_REALLOCATION,
            CANCEL_UNRECORDED_REASON,
        }:
            pass
        elif _running_cancel(receipt):
            # This driver cancelled a route whose last post-start observe read running, and the
            # guardian recorded no stop of its own: say why, never as a provider move.
            receipt["reason"] = RUNNING_CANCEL_PREFIX + receipt["cancel_cause"]["class"]
        elif receipt["reason"] != "provider_stopped_custody_unsealed":
            receipt["reason"] = "custody_stop_or_reconnect_unresolved"
    return receipt


def _running_cancel(receipt):
    """True when the driver's own cancel ended a route whose last observe read running, for a
    reason other than completion, the guardian's recorded stop, the deadline or the budget."""
    cause = receipt.get("cancel_cause")
    latch = receipt.get("running_latch")
    return (
        type(cause) is dict
        and (
            cause.get("last_provider_status") == RUNNING_PROVIDER_STATUS
            or (type(latch) is dict and latch.get("state") == "seen_running")
        )
        and cause.get("class")
        not in {"execution_complete", "guardian_stop_cause", "deadline", "budget"}
        # The guardian records this driver's own cancel as ``control_cancel`` (ADR 0075): that is
        # not a stop of the guardian's own.
        and receipt.get("guardian_stop_cause") in {None, "control_cancel"}
    )


# SIGTERMs the child received while it ran (see ``_defer_sigterm``).
_SIGTERMS = {"received": 0}


def _defer_sigterm():
    """Keep a SIGTERM from killing the live check mid-run (an executor stop or restart sends
    one): the run and its completion tail finish and the receipt is written, so a restarted
    executor finishes the attempt from it. The count is kept on the receipt
    (``sigterm_deferred``). The executor's own timeout (SIGKILL) and the guardian's silence,
    deadline and budget authority still bound the run."""

    def deferred(*_):
        _SIGTERMS["received"] += 1

    try:
        signal.signal(signal.SIGTERM, deferred)
    except (ValueError, OSError):
        pass


def run(session, *, route_id, source_revision, receipt_directory, max_seconds=300, max_polls=30):
    """Callable real driver for an administrator-composed, preapproved session.

    Session must bind state/dispatch/cancel to the same concrete trusted server,
    and external evidence/retained_bytes/reopen to its independent service. A
    The CLI composes that evidence seam from an explicit private server config;
    --run alone is not authorization. This function does not enroll credentials
    or approve.
    """
    from tessera_worker.remote_dispatch import RemoteDispatcher

    from .salad_server import TrustedSaladServer

    # Test/client callbacks cannot be promoted to authenticated live evidence.
    if type(session) is not SaladLiveSession:
        raise ValueError("concrete authenticated live session required")
    if (
        type(session.server) is not TrustedSaladServer
        or type(session.dispatcher) is not RemoteDispatcher
    ):
        raise ValueError("trusted server session required")
    if session.dispatcher.store is not session.server.store or (
        session.dispatch.__self__ is not session
        or session.dispatcher.worker != session.server.worker
    ):
        raise ValueError("exact dispatcher binding required")
    if not is_source_pin(source_revision) or not (
        type(max_seconds) is int
        and 1 <= max_seconds <= MAX_ROUTE_SECONDS
        and type(max_polls) is int
        and 1 <= max_polls <= MAX_POLLS
    ):
        raise ValueError("bounded source-pinned run required")
    if source_revision != session.config["source_revision"] or (
        source_revision.split("+sha256:")[1] != source_digest()
    ):
        raise ValueError("source inventory changed")
    directory_fd = _private_path(receipt_directory, directory=True)
    try:
        if os.listdir(directory_fd):
            raise ValueError("new empty private receipt directory required")
    finally:
        os.close(directory_fd)
    _write_receipt(
        receipt_directory,
        {
            "schema": "tessera.salad.live-check.v1",
            "source_revision": source_revision,
            "status": "in_progress",
            "reason": "run_started_not_acceptance",
        },
        name="salad-live-started.json",
    )
    receipt = _drive(
        session,
        route_id=route_id,
        source_revision=source_revision,
        max_seconds=max_seconds,
        max_polls=max_polls,
        sleep=time.sleep,
        clock=time.monotonic,
        watch_signals_file=session.config.get(WATCH_SIGNALS_KEY),
        route_window=session.config.get(ROUTE_WINDOW_KEY),
    )
    try:
        # Beats the guardian took and the largest gap between two of them (ADR 0075).
        receipt["guardian_beats"] = session.beat_record(route_id)
    except Exception:
        receipt["guardian_beats"] = None
    if _SIGTERMS["received"]:
        receipt["sigterm_deferred"] = _SIGTERMS["received"]
    _write_receipt(receipt_directory, receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--receipt-directory", type=Path)
    options = parser.parse_args(argv)
    report = readiness()
    if options.run:
        if not options.server_config or not options.receipt_directory:
            report["reason"] = "explicit_private_server_config_and_receipt_directory_required"
        else:
            session = None
            _defer_sigterm()
            try:
                session = SaladLiveSession(options.server_config)
                report = run(
                    session,
                    route_id=session.config["route_id"],
                    source_revision=session.config["source_revision"],
                    receipt_directory=options.receipt_directory,
                    max_seconds=session.config["max_seconds"],
                    max_polls=session.config["max_polls"],
                )
            except Exception as error:
                # No receipt may exist: the report still says what failed, sanitized and
                # bounded (_error_detail), so the executor's journal is never blind.
                report = {
                    "status": "inconclusive",
                    "reason": "secure_setup_or_execution_unresolved",
                    "error": _error_detail(error),
                }
            finally:
                if session is not None:
                    session.close()
    print(json.dumps(report, sort_keys=True))
    return (0 if report.get("status") == "passed" else 2) if options.run else 0


if __name__ == "__main__":
    raise SystemExit(main())
