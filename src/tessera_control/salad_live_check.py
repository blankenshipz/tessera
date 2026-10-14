"""Opt-in bounded journey driver; default CLI performs no reads or network calls.

Deployment composition is supplied by the trusted server, not by client booleans.
No direct provider mutation, credential discovery, retries or paid-effect defaults.
"""

import argparse
import hashlib
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
        "cancel_credential_file",
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
        ("max_seconds", 1, 300),
        ("max_polls", 1, 30),
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
        value["cancel_credential_file"],
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
            self.cancel_actor = auth.authenticate(
                _private_bytes(self.config["cancel_credential_file"], 4096).decode("ascii").strip()
            )
            if self.cancel_actor["role"] != "human":
                raise ValueError("existing human cancellation credential required")
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
            with _wall_bound(3):
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
        with _wall_bound(20):
            return self.dispatcher.dispatch(route, action)

    def _exchange(self, body):
        with _wall_bound(10):
            return self.custody.exchange(body)

    def cancel(self, route):
        state = self.state(route)
        if state["desired"] != "stop":
            self.server.store.command(
                self.cancel_actor,
                route,
                "salad-live-cancel-" + route,
                dict(
                    command="cancel",
                    expected_revision=state["revision"],
                    proposal_digest=state["proposal_digest"],
                ),
            )
        with _wall_bound(3):
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
        manifest = status.get("manifest")
        proof = dict(
            output_retained=False,
            stop_confirmed=stopped,
            writers_quiescent=False,
            output_byte_cap=state["proposal"]["selection"]["salad"]["execution"][
                "max_output_bytes"
            ],
            output_sha256=None,
            provider_id=state["provider_id"],
            financial=_financial(status),
        )
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
        "reconnected": False,
        "dispatch_calls": 0,
        "cost_cents": None,
        "usage_status": "unknown",
        "node_writers": "unknown",
        "financial": _financial({}),
    }
    deadline = clock() + max_seconds
    launched = False
    execution_complete = False

    def dispatch(action):
        receipt["dispatch_calls"] += 1
        return session.dispatch(route_id, action)

    try:
        # This is a read of the durable record, not a new admission. A rerun or
        # reconnect with existing effects never reaches create/start.
        state = session.state(route_id)
        original_digest = state["proposal_digest"]
        prior_launches = [e for e in state["effects"] if e["action"] in {"create", "start"}]
        if not prior_launches:
            launched = True  # lost create reply still requires exact stop/reconcile
            dispatch("create")
            state = dispatch("observe")
            if state["provider_status"] != "stopped":
                raise ValueError("create unresolved")
            dispatch("start")
        for _ in range(max_polls):
            if clock() >= deadline:
                break
            state = dispatch("observe")
            evidence = session.evidence(route_id)
            receipt["financial"] = _financial(evidence.get("financial", {}))
            if evidence["output_retained"]:
                break
            sleep(min(1, max(0, deadline - clock())))
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
        except Exception:
            pass
        try:
            existing = session.state(route_id)
            if not any(effect["action"] == "stop" for effect in existing["effects"]):
                dispatch("stop")
            for _ in range(max_polls):
                state = dispatch("observe")
                evidence = session.evidence(route_id)
                receipt["financial"] = _financial(evidence.get("financial", {}))
                if evidence["stop_confirmed"]:
                    receipt["stop_confirmed"] = True
                    break
                if clock() >= deadline:
                    break
                sleep(min(1, max(0, deadline - clock())))
        except Exception:
            receipt["reason"] = "stop_unresolved"
    try:
        if not execution_complete or not receipt["stop_confirmed"]:
            raise ValueError("stop unresolved")
        evidence = session.evidence(route_id)
        receipt["financial"] = _financial(evidence.get("financial", {}))
        data = session.retained_bytes(route_id)
        if not evidence["output_retained"] or not evidence["writers_quiescent"]:
            raise ValueError("custody unresolved")
        if not 1 <= len(data) <= evidence["output_byte_cap"]:
            raise ValueError("output bound")
        digest = hashlib.sha256(data).hexdigest()
        if digest != evidence["output_sha256"]:
            raise ValueError("output integrity")
        before = session.state(route_id)
        reopened = session.reopen()
        try:
            after = reopened.state(route_id)
            if before["effects"] != after["effects"] or after["proposal_digest"] != original_digest:
                raise ValueError("reconnect identity")
            if reopened.retained_bytes(route_id) != data:
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
            output_sha256=digest,
            output_bytes=len(data),
            new_launch_attempted=launched,
        )
    except Exception:
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
        and 1 <= max_seconds <= 300
        and type(max_polls) is int
        and 1 <= max_polls <= 30
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
