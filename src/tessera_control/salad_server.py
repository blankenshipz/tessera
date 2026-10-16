"""Explicit trusted-server Salad composition, never constructed from a request.

This module does not activate the default service or SaladProxy.request. It needs
an administrator-owned secret backend and an independently supervised Linux Unix
socket guardian/custody service. An absent/unready service fences new work.
"""

import hashlib
import json
import os
import socket
import stat
import struct
import time

from .remote_execution import RemoteExecutionStore
from .remote_execution_models import ProviderReadback, derived_salad_argv
from .salad_http import SaladHTTPTransport
from .salad_proxy import ACTION
from .salad_proxy_contract import ProxyPolicy, ProxyRequest, ProxyResult
from .salad_secret_files import PrivateFileSecretResolver
from .store import Conflict, Forbidden, _connect, _parse_utc, canonical_digest, utc_now


class SaladSafetyClient:
    """Fixed local IPC, with native peer identity rather than conformance flags.

    The independent service owns durable reservations, deadlines, stop-only
    recovery and custody. It must implement reserve/ready/bind/cancel. Responses
    cannot supply execution credentials, provider identity or new authority.
    """

    def __init__(self, path, *, service_uid):
        self.path, self.service_uid = os.fspath(path), service_uid

    def _call(self, body):
        # SO_PEERCRED is deliberately required: this deployment seam is Linux.
        if not hasattr(socket, "SO_PEERCRED"):
            raise ValueError("independent Linux guardian required")
        parent = os.stat(os.path.dirname(self.path), follow_symlinks=False)
        before = os.stat(self.path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != self.service_uid
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISSOCK(before.st_mode)
            or before.st_uid != self.service_uid
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise ValueError("independent service unavailable")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            deadline = time.monotonic() + 2
            client.settimeout(2)
            client.connect(self.path)
            pid, uid, _ = struct.unpack(
                "3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            after = os.stat(self.path, follow_symlinks=False)
            if (
                uid != self.service_uid
                or pid == os.getpid()
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise ValueError("independent service identity mismatch")
            client.sendall(json.dumps(body, allow_nan=False).encode() + b"\n")
            data = bytearray()
            while not data.endswith(b"\n"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("independent service unavailable")
                client.settimeout(remaining)
                part = client.recv(4096)
                if not part or len(data) + len(part) > 16384:
                    raise ValueError("independent service unavailable")
                data.extend(part)
            reply = json.loads(data)
            return reply

    def _exchange(self, body):
        reply = self._call(body)
        if type(reply) is not dict or set(reply) != {"reservation", "state", "observed_at"}:
            raise ValueError("independent service unavailable")
        if (
            reply["reservation"] != body["reservation"]
            or reply["state"] != "ready"
            or type(reply["observed_at"]) not in {int, float}
            or not 0 <= time.time() - reply["observed_at"] <= 2
        ):
            raise ValueError("independent service not ready")

    @staticmethod
    def _reservation(state):
        return canonical_digest([state["id"], state["proposal_digest"]])

    def reserve(self, state):
        self._exchange(
            dict(
                operation="reserve",
                reservation=self._reservation(state),
                route_id=state["id"],
                org_id=state["org_id"],
                project_id=state["project_id"],
                proposal=state["proposal"],
                routing_package=state["routing_package"],
                runtime_package=state["runtime_package"],
                route_digest=state["proposal_digest"],
                deadline=state["guardian_deadline"],
            )
        )

    def ready(self, state):
        self._exchange(dict(operation="ready", reservation=self._reservation(state)))

    def heartbeat(self, state):
        self._exchange(dict(operation="heartbeat", reservation=self._reservation(state)))

    def bind(self, state, observation):
        self._exchange(
            dict(
                operation="bind",
                reservation=self._reservation(state),
                provider_id=observation.provider_id,
                allocation_id=observation.allocation_id,
            )
        )

    def cancel(self, state):
        self._exchange(dict(operation="cancel", reservation=self._reservation(state)))

    def status(self, state):
        result = self._call(dict(operation="status", reservation=self._reservation(state)))
        if type(result) is not dict or result.get("status") not in {
            "reserved",
            "armed",
            "stopped",
            "unknown",
        }:
            raise ValueError("independent service unavailable")
        return result

    def import_retained(self, state):
        result = self._call(dict(operation="import", reservation=self._reservation(state)))
        if type(result) is not dict or set(result) != {"manifest", "output"}:
            raise ValueError("invalid retained output")
        manifest = result["manifest"]
        if (
            type(manifest) is not dict
            or manifest.get("writers_closed") is not True
            or manifest.get("quarantine") is not True
        ):
            raise ValueError("invalid retained output")
        unsigned = {k: v for k, v in manifest.items() if k != "digest"}
        if canonical_digest(unsigned) != manifest.get("digest"):
            raise ValueError("invalid retained output")
        data = json.dumps(
            result["output"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        profile = state["proposal"]["selection"]["salad"]["execution"]
        if len(data) > profile["max_output_bytes"]:
            raise ValueError("invalid retained output")
        inventories = manifest.get("inventories")
        if type(inventories) is not list or len(inventories) != 1:
            raise ValueError("invalid retained output")
        inventory = inventories[0]
        expected = dict(
            tenant=state["org_id"],
            project=state["project_id"],
            operation=state["proposal"]["operation_id"],
            epoch=state["proposal"]["epoch"],
            artifact=profile["artifact_destination"],
            kind="output",
        )
        if (
            inventory.get("binding") != expected
            or inventory.get("files")
            != [dict(name="output.json", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))]
            or inventory.get("writers_closed") is not True
            or canonical_digest({k: v for k, v in inventory.items() if k != "digest"})
            != inventory.get("digest")
        ):
            raise ValueError("invalid retained output")
        return result


class SaladSafetyHTTPSClient(SaladSafetyClient):
    """Pinned authenticated TLS to an independently supervised safety service.

    References are administrator-configured files, never raw keys or request
    headers. Readiness is queried from the service's enforcing loop/custody state.
    Host/process independence remains a deployment acceptance prerequisite.
    """

    def __init__(self, host, port, *, token_file, ca_file, certificate_sha256):
        from .salad_safety_service import SafetyHTTPSClient

        if (
            type(host) is not str
            or not host
            or any(c in host for c in "/:@\\\r\n")
            or type(port) is not int
            or not 1 <= port <= 65535
            or type(certificate_sha256) is not str
            or len(certificate_sha256) != 64
            or any(c not in "0123456789abcdef" for c in certificate_sha256)
        ):
            raise ValueError("pinned safety service configuration required")
        self._client = SafetyHTTPSClient(
            host,
            port,
            token_file=token_file,
            ca_file=ca_file,
            certificate_sha256=certificate_sha256,
        )

    def _call(self, body):
        return self._client.exchange(body)


class TrustedSaladExecutionStore(RemoteExecutionStore):
    """Server-private store; provider intake is bound to the concrete composition."""

    def _bound(self, state):
        server = getattr(self, "_salad_server", None)
        return type(server) is TrustedSaladServer and (
            state["org_id"],
            state["project_id"],
            state["proposal"]["selection"],
        ) == (
            server.policy.org_id,
            server.policy.project_id,
            server.policy.target.selection.model_dump(mode="json"),
        )

    def _provider_receipt_allowed(self, db, worker, state, effect_id):
        allowed = (
            self._bound(state)
            and db.execute(
                "SELECT 1 FROM events WHERE org_id=? AND event_type='salad_proxy.claim' "
                "AND json_extract(payload_json,'$.nonce')=? "
                "AND json_extract(payload_json,'$.route_id')=?",
                (worker["org_id"], effect_id, state["id"]),
            ).fetchone()
            is not None
        )
        if allowed:
            state.update(
                provider_enabled=True,
                live_activation="trusted_server_composed_readiness_required",
                evidence="provider",
            )
        return allowed

    def get(self, actor, ident):
        state = super().get(actor, ident)
        if self._bound(state):
            state["live_activation"] = "trusted_server_composed_readiness_required"
            state["evidence"] = (
                "provider"
                if any(r["provenance"] == "provider" for r in state["receipts"])
                else "unobserved"
            )
            state["proxy_safety"]["transport"] = "trusted_server"
            state["proxy_safety"]["blocked_reasons"] = ["independent_readiness_required"]
            if state["authority_status"] == "current_offline_only":
                state["authority_status"] = "current_trusted_server_only"
            state["go_no_go"] = "independent_readiness_required"
        return state

    def preflight_authority(self, worker, ident, proposal, action):
        with _connect(self.database_path) as db:
            live = self._live_principal(db, worker, audience="worker")
            state = self._load(db, live, ident)
            op, package = self._operation(db, live, state["proposal"])
            # Ephemeral server-only snapshot from retained admission; never an
            # HTTP request package, persisted overlay mutation, or activation flag.
            state["runtime_package"] = package
            if (
                live["id"] != op["worker_principal_id"]
                or proposal.model_dump(mode="json") != state["proposal"]
                or not self._bound(state)
                or not state["approved_by"]
            ):
                raise Forbidden("Authenticated exact Salad route required.")
            if action in {"create", "start"}:
                self._current(db, live, state)
                if state["desired"] != "run" or state["reallocated"]:
                    raise Conflict("Salad execution fenced.")
                execution = state["proposal"]["selection"]["salad"]["execution"]
                try:
                    derived_salad_argv(
                        execution,
                        package["plan"]["execution_spec"]["argv"],
                        proposal.operation_id,
                        proposal.epoch,
                        proposal.selection.resources.max_seconds,
                    )
                except ValueError:
                    raise Conflict("Salad argv differs from admitted package.") from None
                capability = db.execute(
                    "SELECT * FROM capability_policies WHERE org_id=? AND project_id=? "
                    "AND task_id=? AND declaration_digest=? AND status='approved'",
                    (
                        live["org_id"],
                        state["project_id"],
                        state["task_id"],
                        execution["capability_digest"],
                    ),
                ).fetchone()
                if capability is None:
                    raise Conflict("Approved Salad capability policy required.")
                self._current_policy(db, live, capability)
                declaration = json.loads(capability["declaration_json"])
                if (
                    declaration["image_digest"] != proposal.selection.manifests.image.split("@")[1]
                    or declaration["actions"]
                    or declaration["credential_refs"]
                    or declaration["network"]
                ):
                    raise Conflict("Salad runtime capabilities unsupported.")
                lease = db.execute(
                    "SELECT expires_at,hard_expires_at FROM execution_leases WHERE id=?",
                    (op["lease_id"],),
                ).fetchone()
                state["guardian_deadline"] = min(
                    _parse_utc(lease["expires_at"]).timestamp(),
                    _parse_utc(lease["hard_expires_at"]).timestamp(),
                    time.time() + proposal.selection.resources.max_seconds,
                )
            return state


class TrustedSaladServer:
    def __init__(self, *, state_dir, worker, policy, resolver, safety):
        if type(resolver) is not PrivateFileSecretResolver or type(safety) not in {
            SaladSafetyClient,
            SaladSafetyHTTPSClient,
        }:
            raise ValueError("concrete private backend and independent service required")
        self.policy = ProxyPolicy.model_validate_json(
            json.dumps(
                policy.model_dump(mode="json") | {"management": policy.management.model_dump()}
            )
        )
        self.worker, self.resolver, self.safety = dict(worker), resolver, safety
        self.store = TrustedSaladExecutionStore(state_dir, policies=[self.policy.target])
        self.store._salad_server = self
        self.transport = SaladHTTPTransport(self.policy.target.selection)

    def _credential(self):
        credential = self.policy.management
        metadata = self.resolver.metadata(credential.reference)
        now = time.time()
        if (
            metadata.version != credential.version
            or metadata.revoked
            or metadata.purpose != "salad_management"
            or not 0 <= now - metadata.checked_at <= 5
            or metadata.expires_at <= now + 2
        ):
            raise ValueError("credential unavailable")

    def preflight(self, proposal, route_id, action):
        state = self.store.preflight_authority(self.worker, route_id, proposal, action)
        self._credential()
        with self.resolver.resolve(
            self.policy.management.reference, self.policy.management.version
        ) as secret:
            # Resolution may stall or revoke authority. Recheck before any API read.
            state = self.store.preflight_authority(self.worker, route_id, proposal, action)
            self._credential()
            if action in {"create", "start"}:
                if action == "create":
                    # Repeated reservation must preserve its first deadline and
                    # cannot revive a stopped/recovered/expired assignment.
                    self.safety.reserve(state)
                else:
                    self.safety.ready(state)
                state = self.store.preflight_authority(self.worker, route_id, proposal, action)
                self._credential()
                result = self.transport.preflight(proposal.selection, secret)
            elif action == "stop":
                # A narrowly bound stop must survive mutable configuration drift.
                # Keep credential/authority checks and fresh scope/name/UUID proof;
                # this observation says nothing about execution or termination.
                result = self.transport.inspect_stop(
                    proposal.selection, state["provider_id"], secret
                )
            else:
                # Observe/delete keep full reviewed-profile validation.
                result = self.transport.send(
                    "read_group", proposal.selection, state["provider_id"], secret
                )
        if result.status != "observed" or result.observation is None:
            raise Conflict("Fresh provider preflight unavailable.")
        observation = result.observation
        preflight = dict(
            observation,
            route_digest=state["proposal_digest"],
            available=observation.get("available", False),
            quota_ok=observation.get("quota_ok", False),
            group_absent=observation.get("group_absent", False),
            state=observation.get("state", "unknown"),
            provider_id=observation.get("provider_id"),
        )
        return preflight

    def request(self, request, *, preflight):
        try:
            request = ProxyRequest.model_validate_json(request.model_dump_json())
            state = self.store.get(self.worker, request.route_id)
            if ACTION.get(request.operation, "observe") != state["effects"][-1]["action"]:
                raise ValueError("operation mismatch")
            state = self.store.claim_salad_proxy(
                self.worker, request.route_id, request.effect_id, self.policy, preflight
            )
        except Exception:
            return ProxyResult(status="blocked", reason="admission_denied")
        failure_reason = "credential_unavailable"
        try:
            self._credential()
            with self.resolver.resolve(
                self.policy.management.reference, self.policy.management.version
            ) as secret:
                failure_reason = "admission_denied"
                state = self.store.claim_salad_proxy(
                    self.worker,
                    request.route_id,
                    request.effect_id,
                    self.policy,
                    preflight,
                    recheck=True,
                )
                failure_reason = "credential_unavailable"
                self._credential()
                failure_reason = "provider_unavailable"
                if request.operation in {"create_stopped", "start"}:
                    self.safety.ready(state)
                elif request.operation in {"stop", "delete"}:
                    # Guardian/custody outage must not block the exact provider stop.
                    try:
                        self.safety.cancel(state)
                    except Exception:
                        pass
                # IPC can stall just like secret resolution. It cannot extend
                # target freshness, credential expiry or current execution authority.
                failure_reason = "admission_denied"
                state = self.store.claim_salad_proxy(
                    self.worker,
                    request.route_id,
                    request.effect_id,
                    self.policy,
                    preflight,
                    recheck=True,
                )
                failure_reason = "credential_unavailable"
                self._credential()
                failure_reason = "provider_unavailable"
                result = self.transport.send(
                    request.operation, self.policy.target.selection, state["provider_id"], secret
                )
                failure_reason = "malformed_response"
                observation = self.readback(result, state)
                if (
                    observation.provider_id
                    and (
                        state["provider_id"] is None
                        or (
                            state["allocation_id"] is None
                            and observation.allocation_id is not None
                            and state["desired"] == "run"
                        )
                    )
                    and request.operation
                    in {
                        "create_stopped",
                        "read_group",
                        "read_instance",
                    }
                ):
                    try:
                        self.safety.bind(state, observation)
                    except Exception:
                        # Durable reservation still enforces reserved-name recovery.
                        # Never retry create/start or turn binding failure into success.
                        return ProxyResult(status="unknown", reason="provider_unavailable")
                return result
        except Exception:
            return ProxyResult(status="unknown", reason=failure_reason)

    @staticmethod
    def readback(result, state):
        observation = result.observation or {}
        instances = observation.get("instances", [])
        allocation = None
        if len(instances) == 1:
            instance = instances[0]
            # State and group configuration version are observations, not a new
            # allocation. Match guardian's immutable instance+machine identity.
            allocation = canonical_digest(
                {
                    "instance_id": instance["instance_id"],
                    "machine_id": instance["machine_id"],
                }
            )
        reallocated = bool(
            state["allocation_id"] and allocation and state["allocation_id"] != allocation
        )
        provider_state = observation.get("state", "unknown")
        if provider_state not in {
            "stopped",
            "running",
            "succeeded",
            "failed",
            "deleted",
            "unknown",
        }:
            provider_state = "unknown"
        counts = observation.get("instance_status_counts")
        terminal = (
            provider_state == "stopped"
            and type(counts) is dict
            and set(counts)
            == {"allocating_count", "creating_count", "running_count", "stopping_count"}
            and all(type(v) is int and v == 0 for v in counts.values())
            and not instances
            and observation.get("pending_change") is False
            and observation.get("response_status") != 202
        )
        return ProviderReadback(
            provider_id=observation.get("provider_id"),
            allocation_id=allocation,
            state="unknown" if reallocated else provider_state,
            observed_at=observation.get("observed_at", utc_now()),
            response_status=observation.get("response_status"),
            termination_confirmed=bool(terminal) and not reallocated,
            provenance="provider",
            proxy_reason="reallocation_uncertain" if reallocated else result.reason,
        )

    def dispatcher(self, route_id):
        from tessera_worker.remote_dispatch import RemoteDispatcher
        from tessera_worker.salad_remote import SaladTrustedAdapter

        return RemoteDispatcher(
            self.store,
            self.worker,
            {self.policy.target.selection.target_digest: SaladTrustedAdapter(self, route_id)},
        )

    def heartbeat(self, route_id):
        """Trusted control-owner polling; never extends a lease or guardian deadline."""
        from .remote_execution_models import RemoteProposal

        state = self.store.get(self.worker, route_id)
        proposal = RemoteProposal.model_validate(state["proposal"])
        try:
            state = self.store.preflight_authority(self.worker, route_id, proposal, "start")
        except (Conflict, Forbidden):
            self.safety.cancel(state)
            raise
        if not any(effect["action"] == "create" for effect in state["effects"]):
            raise Conflict("Guardian heartbeat requires existing create intent.")
        self.safety.heartbeat(state)

    def import_retained(self, route_id):
        state = self.store.get(self.worker, route_id)
        if (
            not self.store._bound(state)
            or not state["termination_confirmed"]
            or state["reallocated"]
        ):
            raise Conflict("Exact terminal Salad route required for retained import.")
        return self.safety.import_retained(state)
