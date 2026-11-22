"""Opt-in bounded journey driver; default CLI performs no reads or network calls.

Deployment composition is supplied by the trusted server, not by client booleans.
No direct provider mutation, credential discovery, retries or paid-effect defaults.
"""

import argparse
import hashlib
import io
import json
import os
import re
import signal
import stat
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Route window from create through output and stop.
# GPU node allocation plus a multi-GB CUDA image pull exceeds 5 minutes. Polls stay
# <=10s apart for guardian heartbeats, so the poll cap scales with the window.
# 45 min unbilled provisioning + up to 60 min billed workload + upload grace.
# How long a group that reads stopped may take to read settled (no change in flight, no instance)
# before start is attempted anyway and the transport's own exact read decides.
PRESTART_SETTLE_SECONDS = 45
MAX_ROUTE_SECONDS = 7200
MAX_POLLS = 720


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
        or set(value) != keys
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
    return value, policy, hashlib.sha256(raw).hexdigest()


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

    def state(self, route):
        if route != self.config["route_id"]:
            raise ValueError("exact route required")
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
        if action in {"create", "start"}:
            # Registry liveness before use-time authority: a long-lived executor's last
            # sighting may be older than the 60 s disconnect bound (keepalive starts later).
            self._registry_heartbeat()
            state = self.server.store.preflight_authority(
                self.server.worker, route, RemoteProposal.model_validate(state["proposal"]), action
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
        with _wall_bound(45):
            return self.dispatcher.dispatch(route, action)

    def keepalive(self, route):
        """Heartbeat the worker registry, renew the exact soft lease, heartbeat the guardian.

        Both go through existing authenticated paths: renewal is bounded by the
        lease's hard expiry and epoch, and the heartbeat re-runs use-time authority
        (cancelling on failure). Neither extends the guardian's run-wide deadline.
        """
        import uuid

        from .store import canonical_digest

        state = self.state(route)
        proposal = state["proposal"]
        # Registry liveness first: a worker unseen for 60s is "disconnected" and
        # can no longer renew lease authority.
        self._registry_heartbeat()
        payload = {"lease_id": proposal["lease_id"], "expected_epoch": proposal["epoch"]}
        with _wall_bound(5):
            self.server.store.renew_execution_lease(
                self.server.worker,
                proposal["lease_id"],
                "salad-live-renew-" + uuid.uuid4().hex,
                canonical_digest(payload),
                proposal["epoch"],
            )
        with _wall_bound(15):
            self.server.heartbeat(route)

    def _registry_heartbeat(self):
        import uuid

        from .store import canonical_digest
        from .workers import WorkerRegistryStore

        with _wall_bound(5):
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
        with _wall_bound(30):
            self.server.deliver_inputs(route)
        held = self._exchange(self._body(route, "status")).get("input_delivery")
        if (
            type(held) is not dict
            or held.get("ticket_held") is not True
            or held.get("snapshot_digest") != declared.snapshot_digest
            or held.get("snapshot_digest") != state["proposal"]["selection"]["manifests"]["data"]
        ):
            raise ValueError("guardian input delivery unconfirmed")

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
        state = self.state(route)
        if state["desired"] != "stop":
            # The executor's own worker credential (reduce-only, own route): no human
            # session credential exists for this path.
            self.server.store.worker_cancel(
                self.server.worker,
                route,
                "salad-live-cancel-" + route,
                expected_revision=state["revision"],
                proposal_digest=state["proposal_digest"],
            )
        with _wall_bound(15):
            self.server.safety.cancel(self.state(route))

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
            stop_cause=_cause(status.get("stop_cause")),
            last_workload_refusal=_cause(status.get("last_workload_refusal")),
            last_rate_read_failure=_cause(status.get("last_rate_read_failure")),
            last_provider_read_failure=_cause(status.get("last_provider_read_failure")),
        )
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


def source_digest():
    """Observed installed Python package-input inventory, not an inferred git state."""
    root = Path(__file__).parent.parent
    digest = hashlib.sha256()
    for package in ("tessera_control", "tessera_worker"):
        for path in sorted((root / package).rglob("*.py")):
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise ValueError("source inventory unsafe")
            data = path.read_bytes()
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


INVALID_CODE = "invalid_code"


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
# The server maps every pre-start provider state (pending, deploying, allocating,
# downloading, creating) to "unknown", so only "unknown" reaches here. It also covers
# provider failures and reallocation; those see the slower observe cadence, which is safe
# because the guardian, not the driver, drives stops and retention.
PRESTART_PROVIDER_STATUSES = frozenset({"unknown"})
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
# authority) are terminal as before.
# A transient refusal is retried at the next interval only while the next beat can
# still land inside the silence bound with a margin, and at most
# MAX_TRANSIENT_GUARDIAN_REFUSALS times in a row.
GUARDIAN_SILENCE_SECONDS = 30
GUARDIAN_SILENCE_MARGIN_SECONDS = 5
MAX_TRANSIENT_GUARDIAN_REFUSALS = 3
TRANSIENT_GUARDIAN_CODES = frozenset({"guardian_loop_unready", "workload_verifier_unready"})
# A read-only observe whose fresh provider read timed out changes nothing, and the
# guardian keeps its own heartbeat and stop authority meanwhile, so it is skipped and
# retried at the next interval, at most this many times in a row.
MAX_TRANSIENT_OBSERVE_FAILURES = 3


def _guardian_code(error):
    from .salad_server import guardian_refusal_code

    return guardian_refusal_code(error)


def _drive(session, *, route_id, source_revision, max_seconds, max_polls, sleep, clock):
    """Internal orchestration seam; isolated fake sessions prove only local behavior.

    Session.dispatch MUST be the existing RemoteDispatcher.dispatch. Evidence and
    reopen MUST come from authenticated durable external service composition.
    The driver never manufactures admissions, resets claims or reissues launch.
    """
    receipt = {
        "schema": "tessera.salad.live-check.v1",
        "source_revision": source_revision,
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
        # Latest closed code with which the guardian refused a workload call, if any
        # (e.g. a container that called begin-once before its instance could be bound).
        # ADVISORY: recorded before the call's token binds, so it is a diagnostic hint only.
        "guardian_last_workload_refusal": None,
        # Closed reason the guardian's last failed rate / group read gave up with
        # (timeout, provider_unknown, rate_stale, ...), if any.
        "guardian_last_rate_read_failure": None,
        "guardian_last_provider_read_failure": None,
    }
    deadline = clock() + max_seconds
    interval = min(POLL_INTERVAL_CAP_SECONDS, max(1, max_seconds / max_polls))
    launched = False
    execution_complete = False

    def dispatch(action):
        receipt["dispatch_calls"] += 1
        return session.dispatch(route_id, action)

    beats = {"ok_at": None, "transient": 0}
    observes = {"failed": 0}
    evidence = {"output_retained": False}

    def observe():
        """dispatch("observe"), or None after a bounded transient provider read failure."""
        from .salad_server import ProviderReadUnavailable

        try:
            observed = dispatch("observe")
        except ProviderReadUnavailable:
            if observes["failed"] >= MAX_TRANSIENT_OBSERVE_FAILURES - 1:
                raise
            observes["failed"] += 1
            receipt["observe_transient_failures"] += 1
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
            if code in TRANSIENT_GUARDIAN_CODES:
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
            # Once the approved output is retained the guardian stops the
            # group and closes writers, so it refuses further heartbeats.
            # That is completion; any other refusal is still a failure.
            if not session.evidence(route_id)["output_retained"]:
                if code is not None:
                    receipt["guardian_refusal"] = {"code": code, "cause": cause}
                raise
            return
        beats["ok_at"], beats["transient"] = started, 0

    try:
        # This is a read of the durable record, not a new admission. A rerun or
        # reconnect with existing effects never reaches create/start.
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
        if not prior_launches:
            launched = True  # lost create reply still requires exact stop/reconcile
            created = dispatch("create")
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
            polls = 0
            stopped_since = None
            settle_started = clock()
            for _ in range(max_polls):
                heartbeat()
                state = observe() or state
                polls += 1
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
            if state["provider_status"] != "stopped":
                raise ValueError("create unresolved")
            if receipt["input_expected"]:
                # The group exists but is stopped (unbilled). Delivery must be confirmed
                # before start; a failure here never starts it and falls through to the
                # exact cancel/stop below.
                session.deliver_inputs(route_id)
                receipt["input_delivered"] = True
            dispatch("start")
        for _ in range(max_polls):
            if clock() >= deadline:
                break
            if launched:
                heartbeat()
            observed = observe()
            if observed is None:
                sleep(min(interval, max(0, deadline - clock())))
                continue
            state = observed
            evidence = session.evidence(route_id)
            receipt["financial"] = _financial(evidence.get("financial", {}))
            receipt["guardian_rate_read_retries"] = evidence.get("rate_read_retries")
            receipt["guardian_provider_read_retries"] = evidence.get("provider_read_retries")
            receipt["guardian_stop_cause"] = evidence.get("stop_cause")
            receipt["guardian_last_workload_refusal"] = evidence.get("last_workload_refusal")
            receipt["guardian_last_rate_read_failure"] = evidence.get("last_rate_read_failure")
            receipt["guardian_last_provider_read_failure"] = evidence.get(
                "last_provider_read_failure"
            )
            if evidence["output_retained"]:
                break
            # Spread polls over the approved window; stay well inside the guardian's
            # 30s heartbeat staleness bound so a live run is not stopped for silence.
            sleep(min(interval, max(0, deadline - clock())))
            if state["provider_status"] in PRESTART_PROVIDER_STATUSES:
                # Stretch only the observe cadence: keep heartbeating at the normal
                # interval until the backed-off observe is due.
                waited = interval
                while waited < PRESTART_POLL_INTERVAL_SECONDS and clock() < deadline:
                    if launched:
                        heartbeat()
                    step = min(interval, PRESTART_POLL_INTERVAL_SECONDS - waited)
                    sleep(min(step, max(0, deadline - clock())))
                    waited += step
        else:
            raise ValueError("poll bound")
        if clock() >= deadline or not evidence["output_retained"]:
            raise ValueError("output unavailable")
        execution_complete = True
    except Exception:
        receipt["reason"] = "execution_or_output_unresolved"
    finally:
        # Even a failed dispatch/preflight or existing uncertain group gets one
        # exact stop attempt. Never delete, choose a new target, or retry stop.
        try:
            session.cancel(route_id)
            receipt["cancel_recorded"] = True
        except Exception:
            receipt["cancel_recorded"] = False
        # Completion, failure and cancel alike: drop the guardian's ticket and revoke it at
        # control. Best effort; the ticket's own expiry (at most an hour) bounds a failure.
        try:
            receipt["input_revoked"] = bool(session.revoke_inputs(route_id))
        except Exception:
            receipt["input_revoked"] = False
        try:
            existing = session.state(route_id)
            if not any(effect["action"] == "stop" for effect in existing["effects"]):
                dispatch("stop")
            # One exact stop/reconciliation may extend past the scheduling deadline.
            stop_deadline = max(deadline, clock() + STOP_CONFIRM_SECONDS)
            for _ in range(max_polls):
                state = dispatch("observe")
                evidence = session.evidence(route_id)
                receipt["financial"] = _financial(evidence.get("financial", {}))
                receipt["guardian_rate_read_retries"] = evidence.get("rate_read_retries")
                receipt["guardian_provider_read_retries"] = evidence.get("provider_read_retries")
                receipt["guardian_stop_cause"] = evidence.get("stop_cause")
                receipt["guardian_last_workload_refusal"] = evidence.get("last_workload_refusal")
                receipt["guardian_last_rate_read_failure"] = evidence.get("last_rate_read_failure")
                receipt["guardian_last_provider_read_failure"] = evidence.get(
                    "last_provider_read_failure"
                )
                if evidence.get("provider_stop_confirmed"):
                    receipt["provider_stop_confirmed"] = True
                if evidence["stop_confirmed"]:
                    receipt["stop_confirmed"] = True
                    break
                if evidence.get("provider_stop_confirmed"):
                    # Spend closed but custody can never seal: waiting cannot help.
                    receipt["reason"] = "provider_stopped_custody_unsealed"
                    break
                if clock() >= stop_deadline:
                    break
                sleep(min(interval, max(0, stop_deadline - clock())))
        except Exception:
            receipt["reason"] = "stop_unresolved"
    try:
        if not execution_complete or not receipt["stop_confirmed"]:
            raise ValueError("stop unresolved")
        evidence = session.evidence(route_id)
        receipt["financial"] = _financial(evidence.get("financial", {}))
        receipt["guardian_rate_read_retries"] = evidence.get("rate_read_retries")
        receipt["guardian_provider_read_retries"] = evidence.get("provider_read_retries")
        receipt["guardian_stop_cause"] = evidence.get("stop_cause")
        receipt["guardian_last_workload_refusal"] = evidence.get("last_workload_refusal")
        receipt["guardian_last_rate_read_failure"] = evidence.get("last_rate_read_failure")
        receipt["guardian_last_provider_read_failure"] = evidence.get("last_provider_read_failure")
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
            if before["effects"] != after["effects"] or after["proposal_digest"] != original_digest:
                raise ValueError("reconnect identity")
            again = (
                reopened.retained_files(route_id) if multi else reopened.retained_bytes(route_id)
            )
            if again != retained:
                raise ValueError("reconnect custody")
        finally:
            if reopened is not session and type(reopened) is SaladLiveSession:
                reopened.close()
        identity = evidence["provider_id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,200}", identity):
            raise ValueError("provider identity")
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
        elif receipt["reason"] != "provider_stopped_custody_unsealed":
            receipt["reason"] = "custody_stop_or_reconnect_unresolved"
    return receipt


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
    if not re.fullmatch(r"[0-9a-f]{40}\+sha256:[0-9a-f]{64}", source_revision) or not (
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
    )
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
            except Exception:
                report = {
                    "status": "inconclusive",
                    "reason": "secure_setup_or_execution_unresolved",
                }
            finally:
                if session is not None:
                    session.close()
    print(json.dumps(report, sort_keys=True))
    return (0 if report.get("status") == "passed" else 2) if options.run else 0


if __name__ == "__main__":
    raise SystemExit(main())
