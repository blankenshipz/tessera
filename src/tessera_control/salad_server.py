"""Explicit trusted-server Salad composition, never constructed from a request.

This module does not activate the default service or SaladProxy.request. It needs
an administrator-owned secret backend and an independently supervised Linux Unix
socket guardian/custody service. An absent/unready service fences new work.
"""

import hashlib
import io
import json
import os
import re
import socket
import stat
import struct
import time
import uuid

from .execution_targets import require_admission_hook
from .provider_inputs import ProviderInputStore
from .remote_execution import RemoteExecutionStore
from .remote_execution_models import ProviderReadback, derived_salad_argv
from .salad_http import NOT_SENT_SUMMARY, SaladHTTPTransport
from .salad_proxy import ACTION
from .salad_proxy_contract import ProxyPolicy, ProxyRequest, ProxyResult
from .salad_safety_service import DISPATCH_IPC_SECONDS
from .salad_secret_files import PrivateFileSecretResolver
from .salad_workload_contract import capability_network_matches
from .salad_workload_jwt import strict_json
from .store import Conflict, Forbidden, NotFound, _connect, _parse_utc, canonical_digest, utc_now

IPC_TIMEOUT_SECONDS = 12
# The guardian's create-rejected settle bound counts this IPC wait (see there).
assert IPC_TIMEOUT_SECONDS <= DISPATCH_IPC_SECONDS
# Reissue (which revokes the old ticket) once the remaining lifetime falls under
# min(600 s, max(60 s, a quarter of the ticket's issued lifetime)).
INPUT_TICKET_REFRESH_SECONDS = 600
INPUT_TICKET_MIN_REFRESH_SECONDS = 60
# Pending control revokes: bounded table, exponential backoff, bounded attempts.
MAX_REVOKE_PENDING = 64
REVOKE_BACKOFF_SECONDS = 5
REVOKE_BACKOFF_MAX_SECONDS = 300
MAX_REVOKE_ATTEMPTS = 12


def input_refresh_threshold(lifetime):
    return min(INPUT_TICKET_REFRESH_SECONDS, max(INPUT_TICKET_MIN_REFRESH_SECONDS, lifetime * 0.25))


def route_input(proposal):
    """The reviewed input declaration of a workload_v1 route, else None.

    ``proposal`` is a RemoteProposal or its JSON dict. Smoke/exact routes carry no
    contract and return None (their legacy delivery path is unchanged).
    """
    from .remote_execution_models import RemoteProposal

    proposal = RemoteProposal.model_validate(proposal)
    execution = proposal.selection.salad.execution if proposal.selection.salad else None
    if execution is None or execution.argv_binding != "workload_v1":
        return None
    return execution.workload.input


GUARDIAN_REFUSAL_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Client-side meanings a guardian reply can never claim ("unavailable" means no answer).
RESERVED_REFUSAL_CODES = frozenset({"unavailable"})


# A create the provider cleanly rejected, or one that provably never left this process:
# both are reported to the guardian, which releases only on its own absence reads.
CREATE_RELEASE_REASONS = frozenset({"provider_rejected_request", "request_not_sent"})
# A committed create/start claim this server's own use-time recheck refused before any provider
# call is recorded as ``salad_proxy.claim_refused`` (ADR 0205 amendment); only these actions.
CLAIM_REFUSAL_ACTIONS = frozenset({"create", "start"})
CLAIM_REFUSAL_STAGE = "use_time_recheck"


class ProviderReadUnavailable(Conflict):
    """A fresh provider read timed out or failed; nothing was changed (still a 409).

    ``failure`` (alias ``reason``) is the transport's closed reason (e.g. identity_mismatch,
    provider_unavailable) and ``detail`` / ``field`` its closed diagnostics (an HTTP status
    class such as non_2xx_5xx, reset or timeout; the NAME of the mismatching field). Never a
    value, body or path.
    """

    failure = None
    reason = None
    detail = None
    field = None
    request_kind = None


def _failed_read_diagnostics(source, state, reallocated=False):
    """The closed ``detail`` / ``field`` / ``request_kind`` of a failed read, keyed on the
    readback's final state: only an unknown one that is not a reallocation carries them. Tokens,
    never a value or a path."""
    if state != "unknown" or reallocated:
        return {}
    return dict(
        proxy_detail=getattr(source, "detail", None),
        proxy_field=getattr(source, "field", None),
        proxy_request_kind=getattr(source, "request_kind", None),
    )


class GuardianRefused(ValueError):
    """The guardian answered and refused; ``code`` is its closed refusal code.

    Codes come only from the trusted unix admin socket and are bounded identifiers
    (anything else, or a reply without a code, is "internal").
    """

    def __init__(self, code):
        if (
            type(code) is not str
            or not GUARDIAN_REFUSAL_CODE.fullmatch(code)
            or code in RESERVED_REFUSAL_CODES
        ):
            code = "internal"
        self.code = code
        super().__init__("independent service refused: " + code)


class GuardianUnavailable(ValueError):
    """No guardian answer: socket missing/unsafe, connect, timeout or a cut-off reply."""

    def __init__(self):
        super().__init__("independent service unavailable")


class AuthorityStalled(TimeoutError):
    """The heartbeat's use-time authority read stalled on a control lock (it did not refuse).

    ``beat_delivered`` says whether the guardian beat still went out on the last verified
    authority (only while the soft lease that verification saw is unexpired). A stall, never a
    refusal: the live driver handles it like any other control stall."""

    def __init__(self, beat_delivered):
        super().__init__("control authority read stalled")
        self.beat_delivered = beat_delivered


def _lock_stall(error):
    """A SQLite busy/locked wait that ran out (not a disk, schema or programming error)."""
    import sqlite3

    return isinstance(error, sqlite3.OperationalError) and (
        getattr(error, "sqlite_errorcode", None) in {5, 6}  # SQLITE_BUSY, SQLITE_LOCKED
        or "locked" in str(error)
        or "busy" in str(error)
    )


def guardian_refusal_code(error):
    """The guardian refusal code carried by ``error``, "unavailable", or None."""
    if isinstance(error, GuardianRefused):
        return error.code
    if isinstance(error, GuardianUnavailable):
        return "unavailable"
    return None


class SaladSafetyClient:
    """Fixed local IPC, with native peer identity rather than conformance flags.

    The independent service owns durable reservations, deadlines, stop-only
    recovery and custody. It must implement reserve/ready/bind/cancel. Responses
    cannot supply execution credentials, provider identity or new authority.
    """

    def __init__(self, path, *, service_uid):
        self.path, self.service_uid = os.fspath(path), service_uid

    def _call(self, body, cap=16384):
        # SO_PEERCRED is deliberately required: this deployment seam is Linux.
        if not hasattr(socket, "SO_PEERCRED"):
            raise ValueError("independent Linux guardian required")
        try:
            parent = os.stat(os.path.dirname(self.path), follow_symlinks=False)
            before = os.stat(self.path, follow_symlinks=False)
        except OSError:
            raise GuardianUnavailable() from None
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != self.service_uid
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISSOCK(before.st_mode)
            or before.st_uid != self.service_uid
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise GuardianUnavailable()
        try:
            reply = self._socket_call(body, before, cap)
        except OSError:
            # Connect refused, reset or timed out: no answer, not a refusal.
            raise GuardianUnavailable() from None
        if type(reply) is dict and reply.get("error") == "request_rejected":
            code = reply.get("code") if set(reply) == {"error", "code"} else None
            raise GuardianRefused(code)
        return reply

    def _socket_call(self, body, before, cap):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            # Guardian operations may make bounded provider reads (budget 10s);
            # wait slightly longer so a slow success is not abandoned mid-reply.
            deadline = time.monotonic() + IPC_TIMEOUT_SECONDS
            client.settimeout(IPC_TIMEOUT_SECONDS)
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
                    raise GuardianUnavailable()
                client.settimeout(remaining)
                part = client.recv(65536)
                if not part or len(data) + len(part) > cap:
                    raise GuardianUnavailable()
                data.extend(part)
            return json.loads(data)

    def _exchange(self, body):
        self.sent_at = time.time()
        reply = self._call(body)
        if type(reply) is not dict or set(reply) != {"reservation", "state", "observed_at"}:
            raise ValueError("independent service unavailable")
        if body.get("operation") == "input_ticket" and body.get("input_ticket") is None:
            if reply["reservation"] != body["reservation"] or reply["state"] != "ready":
                raise ValueError("independent service not ready")
            return
        if (
            reply["reservation"] != body["reservation"]
            or reply["state"] != "ready"
            or type(reply["observed_at"]) not in {int, float}
            # observed_at is the guardian loop tick that admitted this request:
            # it must be live within 2s before sending, and not in the future.
            or not self.sent_at - 2 <= reply["observed_at"] <= time.time()
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
                budget_binding=state["budget_binding"],
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

    def input_ticket(self, state, ticket):
        """Hand the guardian a provider input ticket (or None to drop it) over the admin channel.

        This is the only path a ticket takes toward Salad; it never enters the workload profile,
        env or argv.  A drop is always honoured by the guardian without a loop-liveness check,
        so its reply may carry ``observed_at`` None or stale; only the echo is verified."""
        self._exchange(
            dict(
                operation="input_ticket",
                reservation=self._reservation(state),
                input_ticket=None
                if ticket is None
                else {
                    key: ticket[key]
                    for key in ("token", "expires_at", "snapshot_id", "snapshot_digest")
                },
            )
        )

    def create_rejected(self, state, rejection):
        """Report a create the provider cleanly rejected (400/422 ProblemDetails).

        Only a fence and a request: the guardian sets stop-only recovery at once and
        later releases the reservation solely on its own exact-name 404 and complete
        project-list reads. The bounded summary is diagnostics, never evidence."""
        result = self._call(
            dict(
                operation="create_rejected",
                reservation=self._reservation(state),
                rejection=rejection,
            )
        )
        if type(result) is not dict or result.get("status") not in {"reserved", "reconciled"}:
            raise ValueError("independent service unavailable")
        return result

    def status(self, state):
        result = self._call(dict(operation="status", reservation=self._reservation(state)))
        if type(result) is not dict or result.get("status") not in {
            "reserved",
            "armed",
            "stopped",
            "reconciled",
            "unknown",
        }:
            raise ValueError("independent service unavailable")
        return result

    def import_retained(self, state, destination=None, partial=False):
        """Import sealed custody after confirmed stop and verify it against the profile.

        ``partial`` (an attempt that ended interrupted or retryable) accepts a sealed manifest
        that lacks some declared files, such as the diagnostic; every listed file is still
        hash-verified. It needs the chunked shape: the single-object reply stays complete-only.

        Return shapes (callers rely on ``files`` for chunked imports):

        * the fixed smoke, or a single output.json retained through the original
          endpoint: ``{"manifest", "output"}`` (no ``files``), as before;
        * a single output.json (at most LEGACY_OUTPUT_MAX_BYTES) uploaded in chunks:
          ``{"manifest", "files", "output"}``; ``files`` lists the verified
          ``{name, sha256, bytes}`` entry and ``output`` is its strictly parsed JSON
          (NaN, Infinity and duplicate keys are refused);
        * any other declared workload_v1 outputs: ``{"manifest", "files"}``. Every file is pulled
          chunk by chunk, its sha256 recomputed by control and, with ``destination``
          (an existing owner-only directory), written there atomically per file.
        """
        from .salad_output_import import (
            IMPORT_CHUNK_REPLY_BYTES,
            OutputImportError,
            declared_outputs,
            expected_binding,
            stream_files,
            verify_manifest,
            write_verified_files,
        )
        from .salad_workload_contract import (
            LEGACY_OUTPUT_MAX_BYTES,
            LEGACY_OUTPUT_NAME,
            OUTPUT_CHUNK_BYTES,
        )

        reservation = self._reservation(state)
        result = self._call(dict(operation="import", reservation=reservation))
        profile = state["proposal"]["selection"]["salad"]["execution"]
        declared = declared_outputs(profile)
        if type(result) is dict and set(result) == {"manifest", "chunk_bytes"}:
            if result["chunk_bytes"] != OUTPUT_CHUNK_BYTES:
                raise ValueError("invalid retained output")
            try:
                files = verify_manifest(
                    result["manifest"],
                    binding=expected_binding(state),
                    declared=declared,
                    max_total=profile["max_output_bytes"],
                    partial=partial,
                )

                def fetch(name, seq):
                    return self._call(
                        dict(operation="import_chunk", reservation=reservation, name=name, seq=seq),
                        cap=IMPORT_CHUNK_REPLY_BYTES,
                    )

                single = (
                    len(files) == 1
                    and files[0]["name"] == LEGACY_OUTPUT_NAME
                    and files[0]["bytes"] <= LEGACY_OUTPUT_MAX_BYTES
                )
                if destination is None:
                    buffer = io.BytesIO()
                    verified = stream_files(fetch, files, (lambda _: buffer) if single else None)
                else:
                    verified = write_verified_files(fetch, files, destination)
                    buffer = None
            except OutputImportError:
                raise ValueError("invalid retained output") from None
            reply = dict(manifest=result["manifest"], files=verified)
            if single:
                try:
                    if buffer is None:
                        with open(os.path.join(destination, LEGACY_OUTPUT_NAME), "rb") as saved:
                            raw = saved.read(LEGACY_OUTPUT_MAX_BYTES + 1)
                    else:
                        raw = buffer.getvalue()
                    reply["output"] = strict_json(raw)
                except (OSError, ValueError):
                    raise ValueError("invalid retained output") from None
            return reply
        if type(result) is not dict or set(result) != {"manifest", "output"}:
            raise ValueError("invalid retained output")
        if declared is not None and (
            set(declared) != {LEGACY_OUTPUT_NAME}
            or declared[LEGACY_OUTPUT_NAME] > LEGACY_OUTPUT_MAX_BYTES
        ):
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
        if len(data) > profile["max_output_bytes"]:
            raise ValueError("invalid retained output")
        inventories = manifest.get("inventories")
        if type(inventories) is not list or len(inventories) != 1:
            raise ValueError("invalid retained output")
        inventory = inventories[0]
        if (
            inventory.get("binding") != expected_binding(state)
            or inventory.get("files")
            != [
                dict(
                    name=LEGACY_OUTPUT_NAME,
                    sha256=hashlib.sha256(data).hexdigest(),
                    bytes=len(data),
                )
            ]
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

    def _call(self, body, cap=131072):
        return self._client.exchange(body, cap=max(cap, 131072))


class TrustedSaladExecutionStore(RemoteExecutionStore):
    """Server-private store; provider intake is bound to the concrete composition."""

    def renew_execution_lease(self, *args, **kwargs):
        # The trusted live check's own worker renews its exact soft lease even after renewals
        # stalled past it (same epoch, inside the hard expiry, grant still active): a control
        # lock must not turn into a lease that can never be renewed (ADR 0075 amendment). The
        # HTTP renewal route keeps refusing a lapsed lease.
        kwargs.setdefault("renew_lapsed", True)
        return super().renew_execution_lease(*args, **kwargs)

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

    def record_claim_refused(self, worker, ident, effect_id):
        """Record that this server's own use-time recheck refused a committed create/start claim.

        Server-only (no public route): the trusted server calls it from ``request`` when a
        ``claim_proxy(recheck=True)`` raised, which every path to the provider call passes
        first, so no provider call was made for this one-use nonce in that request, and no other
        request can make one (``claim_proxy`` refuses a consumed nonce and any effect with a
        receipt). Accepted only for the route's newest create/start effect, while it has no
        receipt, with its committed claim, from the operation's own worker on a store composed
        with the trusted server. Idempotent per nonce (ADR 0205 amendment)."""

        def command(db, live):
            state = self._load(db, live, ident)
            op, _ = self._operation(db, live, state["proposal"])
            effect = state["effects"][-1] if state["effects"] else None
            if (
                not self._bound(state)
                or op["worker_principal_id"] != live["id"]
                or effect is None
                or effect["id"] != effect_id
                or effect["action"] not in CLAIM_REFUSAL_ACTIONS
                or any(r["effect_id"] == effect_id for r in state["receipts"])
            ):
                raise Forbidden("Salad claim refusal binding mismatch.")
            claim = db.execute(
                "SELECT payload_json FROM events WHERE org_id=? AND event_type='salad_proxy.claim' "
                "AND json_extract(payload_json,'$.nonce')=? "
                "AND json_extract(payload_json,'$.route_id')=?",
                (live["org_id"], effect_id, ident),
            ).fetchone()
            if claim is None:
                raise Conflict("Salad proxy has no committed claim to refuse.")
            seen = db.execute(
                "SELECT payload_json FROM events WHERE org_id=? "
                "AND event_type='salad_proxy.claim_refused' "
                "AND json_extract(payload_json,'$.nonce')=?",
                (live["org_id"], effect_id),
            ).fetchone()
            if seen is not None:
                return 200, json.loads(seen[0])
            claimed = json.loads(claim[0])
            payload = dict(
                nonce=effect_id,
                route_id=ident,
                operation_id=claimed["operation_id"],
                action=claimed["action"],
                stage=CLAIM_REFUSAL_STAGE,
                reason="admission_denied",
                provider_call_made=False,
            )
            self._event(
                db,
                live,
                state["project_id"],
                state["task_id"],
                "salad_proxy.claim_refused",
                1,
                payload,
            )
            return 201, payload

        return self._mutate(
            worker,
            uuid.uuid4().hex,
            "POST",
            f"/internal/salad/{ident}/claim-refused",
            canonical_digest([ident, effect_id]),
            command,
            audience="worker",
        )[1]

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

    def preflight_authority(self, worker, ident, proposal, action, *, admit=True):
        with _connect(self.database_path) as db:
            # The heartbeat (admit=False) only reads: a deferred read transaction keeps one
            # snapshot without taking the write lock, so another control writer holding it
            # (a runner tick, a campaign cancel) cannot keep the guardian beat waiting. A
            # use-time admission before create/start keeps the write lock it always had.
            db.execute("BEGIN IMMEDIATE" if admit else "BEGIN")
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
                launched = any(e["action"] == "start" for e in state["effects"])
                # A launched route's heartbeat: a soft lease that lapsed because renewals
                # stalled is not a revocation (the guardian's 30 s silence bound is its
                # liveness); everything else in use-time authority still refuses.
                self._current(db, live, state, soft_lease_lapse_ok=launched and not admit)
                if admit and not launched:
                    from .remote_execution import _admit_workload

                    # Re-checked at use time, before any reservation or begin-once. Never
                    # on the heartbeat of a launched route: a bound tightened later (or a
                    # purged snapshot row) must not cancel a job that is already running.
                    _admit_workload(
                        db,
                        state["project_id"],
                        state["proposal"]["selection"],
                        op["worker_principal_id"],
                        # Create-shape limits only before a create; never strand a
                        # created, stopped group at start.
                        create_shape=action == "create",
                    )
                # Salad-only path: a missing hook fails closed, never skips the budget.
                try:
                    hook = require_admission_hook(state["proposal"]["selection"]["target"])
                except ValueError:
                    raise Conflict("Salad target has no admission hook; refusing.") from None
                state["budget_binding"] = hook(db, live, state["proposal"], op)
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
                    or not capability_network_matches(execution, declaration)
                ):
                    raise Conflict("Salad runtime capabilities unsupported.")
                lease = db.execute(
                    "SELECT expires_at, hard_expires_at FROM execution_leases WHERE id=?",
                    (op["lease_id"],),
                ).fetchone()
                # The soft lease verified by this read (bounded by the hard expiry): how long
                # the trusted server may still beat on this verification if a later
                # authority read stalls (never extended by a beat).
                state["verified_lease_expires_at"] = min(
                    _parse_utc(lease["expires_at"]).timestamp(),
                    _parse_utc(lease["hard_expires_at"]).timestamp(),
                )
                # The renewable soft lease (30s) is a liveness signal enforced by the
                # guardian's own 30s heartbeat staleness stop; the run-wide deadline is
                # bounded by the lease's hard expiry, never the soft renewal window.
                state["guardian_deadline"] = min(
                    _parse_utc(state["budget_binding"]["expires_at"]).timestamp(),
                    _parse_utc(lease["hard_expires_at"]).timestamp(),
                    time.time() + proposal.selection.resources.max_seconds,
                )
            return state


# Exact provider reads a route's observe sends through ``TrustedSaladServer.request``.
READ_OPERATIONS = frozenset({"read_group", "read_instance"})


def _reraise_read_timeout(error, request):
    """Re-raise a ``TimeoutError`` (a caller's local bound, e.g. the live check's wall bound,
    ran out) out of a read's blanket handler, so the caller's stall path sees a timeout instead
    of a recorded "blocked"/"unknown" status. Create, start, stop and delete keep their
    closed results unchanged."""
    if isinstance(error, TimeoutError) and getattr(request, "operation", None) in READ_OPERATIONS:
        raise error


class TrustedSaladServer:
    def __init__(self, *, state_dir, worker, policy, resolver, safety, inputs=None):
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
        self.inputs = inputs or ProviderInputStore(state_dir)
        # route_id -> {"expires_at": epoch seconds, "pending": ticket awaiting guardian}.
        # Held in memory only; the trusted server is the sole issuer.
        self._input_state = {}
        # Routes whose control-side revoke failed: no re-minting; the revoke is retried.
        self._revoke_pending: dict[str, dict] = {}
        # route_id -> dict(attempts, next_at, dropped): backoff and guardian-drop bookkeeping.
        self._revoke_meta: dict[str, dict] = {}
        self.input_log = []
        # route_id -> {"state", "lease_expires_at"}: the last use-time authority a heartbeat
        # verified. In memory only; used solely to keep beating through a stalled (never a
        # refused) authority read until that verification's soft lease expires.
        self._verified_authority: dict[str, dict] = {}

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
            refusal = ProviderReadUnavailable("Fresh provider preflight unavailable.")
            refusal.failure = result.reason
            refusal.reason = result.reason
            refusal.detail = result.detail
            refusal.field = result.field
            refusal.request_kind = result.request_kind
            raise refusal
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
            state = self.store.claim_proxy(
                self.worker, request.route_id, request.effect_id, self.policy, preflight
            )
        except Exception as error:
            _reraise_read_timeout(error, request)
            return ProxyResult(status="blocked", reason="admission_denied")
        failure_reason = "credential_unavailable"
        # True only while a use-time recheck of the committed claim runs; both rechecks precede
        # the one provider call, so a failure while it is set made no provider call.
        rechecking = False
        # True only from a successful create ``ready`` until ``transport.send`` is called: a
        # failure while it is set provably sent no create, so it is reported as unsent.
        unsent_after_ready = False
        try:
            self._credential()
            with self.resolver.resolve(
                self.policy.management.reference, self.policy.management.version
            ) as secret:
                failure_reason = "admission_denied"
                rechecking = True
                state = self.store.claim_proxy(
                    self.worker,
                    request.route_id,
                    request.effect_id,
                    self.policy,
                    preflight,
                    recheck=True,
                )
                rechecking = False
                failure_reason = "credential_unavailable"
                self._credential()
                failure_reason = "provider_unavailable"
                if request.operation in {"create_stopped", "start"}:
                    self.safety.ready(state)
                    unsent_after_ready = request.operation == "create_stopped"
                elif request.operation in {"stop", "delete"}:
                    # Guardian/custody outage must not block the exact provider stop.
                    try:
                        self.safety.cancel(state)
                    except Exception:
                        pass
                    self._revoke_inputs(state)
                # IPC can stall just like secret resolution. It cannot extend
                # target freshness, credential expiry or current execution authority.
                failure_reason = "admission_denied"
                rechecking = True
                state = self.store.claim_proxy(
                    self.worker,
                    request.route_id,
                    request.effect_id,
                    self.policy,
                    preflight,
                    recheck=True,
                )
                rechecking = False
                failure_reason = "credential_unavailable"
                self._credential()
                failure_reason = "provider_unavailable"
                unsent_after_ready = False
                result = self.transport.send(
                    request.operation, self.policy.target.selection, state["provider_id"], secret
                )
                if (
                    request.operation == "create_stopped"
                    and result.reason in CREATE_RELEASE_REASONS
                ):
                    # Fence now and ask for an audited release; the guardian re-reads
                    # absence itself. A failed report leaves the deadline+margin path.
                    try:
                        self.safety.create_rejected(state, result.observation["provider_rejection"])
                    except Exception:
                        pass
                    return result
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
                    except Exception as error:
                        _reraise_read_timeout(error, request)
                        # Durable reservation still enforces reserved-name recovery.
                        # Never retry create/start or turn binding failure into success.
                        return ProxyResult(status="unknown", reason="provider_unavailable")
                return result
        except Exception as error:
            _reraise_read_timeout(error, request)
            if unsent_after_ready:
                self._report_unsent_create(state)
            if rechecking and request.operation in {"create_stopped", "start"}:
                self._record_claim_refused(request)
            return ProxyResult(status="unknown", reason=failure_reason)

    def _report_unsent_create(self, state):
        """Best effort: tell the guardian a ready-admitted create was never sent.

        Called only for a failure after the create's ``ready`` succeeded and before
        ``transport.send`` was called (a use-time recheck, the credential check), so no
        provider call was made. The report carries the transport's own ``request_not_sent``
        summary; the guardian fences at once and releases the reservation only on its own
        exact-name 404 and complete project-list reads after its settle. A failed report
        leaves the deadline+margin path."""
        try:
            self.safety.create_rejected(state, dict(NOT_SENT_SUMMARY))
        except Exception:  # noqa: BLE001 - never turns the original failure into anything else
            pass

    def _record_claim_refused(self, request):
        """Best effort: a failed write leaves the claim standing (fail closed, ADR 0205)."""
        try:
            self.store.record_claim_refused(self.worker, request.route_id, request.effect_id)
        except Exception as error:  # noqa: BLE001 - never turns a refusal into anything else
            self.input_log.append(
                dict(
                    route_id=request.route_id,
                    event="claim_refusal_unrecorded",
                    error=type(error).__name__,
                )
            )

    # ---- input delivery (ADR 0036 phase 2) -------------------------------------------------
    def _push_input_ticket(self, route_id, state, ticket):
        entry = self._input_state.setdefault(route_id, {})
        entry["pending"] = ticket
        self.safety.input_ticket(state, ticket)
        entry["pending"] = None
        entry["expires_at"] = _parse_utc(ticket["expires_at"]).timestamp()
        entry["lifetime"] = max(0.0, entry["expires_at"] - time.time())

    def deliver_inputs(self, route_id):
        """Mint a ticket for the route's operation and give it to the guardian only.

        Reissue revokes any earlier ticket for the operation at control. The workload can never
        trigger this; only the trusted server's own control flow calls it."""
        from .remote_execution_models import RemoteProposal

        if route_id in self._revoke_pending:
            raise Conflict("Input ticket revocation is pending for this route.")
        state = self.store.get(self.worker, route_id)
        if not self.store._bound(state):
            raise Conflict("Exact Salad route required for input delivery.")
        proposal = RemoteProposal.model_validate(state["proposal"])
        execution = proposal.selection.salad.execution
        if (
            execution is not None
            and execution.argv_binding == "workload_v1"
            and execution.workload.input is None
        ):
            # No reviewed input declaration: nothing is minted for this workload.
            raise Conflict("Route declares no input snapshot.")
        ticket = self.inputs.issue_ticket(
            self.worker,
            proposal.operation_id,
            {"lease_id": proposal.lease_id, "expected_epoch": proposal.epoch},
        )
        self._push_input_ticket(route_id, state, ticket)
        self.input_log.append(dict(route_id=route_id, event="issued", at=utc_now()))

    def refresh_inputs(self, route_id, *, now=None):
        """Reissue when the held ticket has under ten minutes left; retry a failed hand-off.

        Returns True when the guardian was given a new ticket.  A route that never had inputs
        delivered is never given one here."""
        if route_id in self._revoke_pending:
            # A failed revoke blocks re-minting; retry it (subject to backoff).
            self.retry_pending_revokes(now=now)
            return False
        entry = self._input_state.get(route_id)
        if entry is None:
            return False
        now = time.time() if now is None else now
        pending = entry.get("pending")
        if pending is not None:
            # A minted-but-undelivered ticket is retried only while it is still useful;
            # an expired or nearly expired one is discarded and a fresh one is minted.
            remaining = _parse_utc(pending["expires_at"]).timestamp() - now
            if remaining >= INPUT_TICKET_MIN_REFRESH_SECONDS:
                state = self.store.get(self.worker, route_id)
                self._push_input_ticket(route_id, state, pending)
                return True
            entry["pending"] = None
        elif entry.get("expires_at", 0) - now >= input_refresh_threshold(
            entry.get("lifetime", 0.0)
        ):
            return False
        self.deliver_inputs(route_id)
        self.input_log.append(dict(route_id=route_id, event="refreshed", at=utc_now()))
        return True

    def _drop_guardian_ticket(self, state):
        """Best effort guardian-side drop; never raises and never blocks the caller."""
        try:
            self.safety.input_ticket(state, None)
            return True
        except Exception as error:
            record = dict(route_id=state["id"], event="drop_failed", at=utc_now())
            if guardian_refusal_code(error) is not None:
                record["code"] = guardian_refusal_code(error)
            self.input_log.append(record)
            return False

    def _give_up_revoke(self, route_id, reason):
        self._revoke_pending.pop(route_id, None)
        self._revoke_meta.pop(route_id, None)
        self.input_log.append(dict(route_id=route_id, event="revoke_abandoned", reason=reason))

    def revoke_inputs(self, route_id):
        """Drop and revoke the route's input tickets now (completion, cancel or failure).

        Unlike the internal path, this also revokes at control when this process holds no
        input state for the route (e.g. after a reconnect) but the route's contract declares
        an input, so a ticket minted by an earlier process cannot outlive the run.  A route
        without an input declaration and without held state is left untouched."""
        state = self.store.get(self.worker, route_id)
        held = route_id in self._input_state or route_id in self._revoke_pending
        if not held and route_input(state["proposal"]) is None:
            return False
        self._revoke_inputs(state, force=True)
        return True

    def _revoke_inputs(self, state, force=False):
        """Best effort: drop at the guardian, revoke at control, forget; outcome is recorded.

        The guardian-side drop is attempted first and its failure is swallowed (the guardian's
        own cancel/stop paths also drop).  A failed control revoke leaves the ticket live at
        control until it is retried (bounded table, backoff and attempts; retried from
        ``heartbeat`` and ``refresh_inputs``) or its TTL lapses.  Pending revokes are held in
        memory only: persisting them would need a new durable table in the provider-input
        store, so after a server restart the ticket TTL (the sole bound, tickets are not
        re-minted without ``deliver_inputs``) limits the exposure."""
        route_id = state["id"]
        if (
            self._input_state.pop(route_id, None) is None
            and route_id not in self._revoke_pending
            and not force
        ):
            return
        meta = self._revoke_meta.setdefault(route_id, dict(attempts=0, next_at=0.0, dropped=False))
        if not meta["dropped"]:
            meta["dropped"] = self._drop_guardian_ticket(state)
        proposal = state["proposal"]
        record = dict(route_id=route_id, event="revoke", at=utc_now())
        meta["attempts"] += 1
        try:
            result = self.inputs.revoke_tickets(
                self.worker,
                proposal["operation_id"],
                {"lease_id": proposal["lease_id"], "expected_epoch": proposal["epoch"]},
            )
            record["revoked"] = result["revoked"]
            self._revoke_pending.pop(route_id, None)
            self._revoke_meta.pop(route_id, None)
        except Exception:
            record["revoked"] = None
            record["error"] = True
            if meta["attempts"] >= MAX_REVOKE_ATTEMPTS:
                self._give_up_revoke(route_id, "attempts")
            else:
                delay = min(
                    REVOKE_BACKOFF_MAX_SECONDS, REVOKE_BACKOFF_SECONDS * 2 ** (meta["attempts"] - 1)
                )
                meta["next_at"] = time.time() + delay
                self._revoke_pending[route_id] = state
                while len(self._revoke_pending) > MAX_REVOKE_PENDING:
                    oldest = next(iter(self._revoke_pending))
                    self._give_up_revoke(oldest, "capacity")
        self.input_log.append(record)

    def retry_pending_revokes(self, *, now=None):
        """Retry failed control revokes that are due; clear routes that no longer exist."""
        now = time.time() if now is None else now
        for route_id in list(self._revoke_pending):
            if self._revoke_meta.get(route_id, {}).get("next_at", 0.0) > now:
                continue
            try:
                state = self.store.get(self.worker, route_id)
            except NotFound:
                self._give_up_revoke(route_id, "route_gone")
                continue
            except Exception:
                state = self._revoke_pending[route_id]
            self._revoke_inputs(state)

    @staticmethod
    def readback(result, state):
        observation = result.observation or {}
        instances = observation.get("instances", [])
        allocation = None
        # Only a started instance identifies an allocation; Salad may try other
        # nodes while allocating/downloading/creating.
        if len(instances) == 1 and (
            instances[0].get("state") in {"running", "stopping"} or state["allocation_id"]
        ):
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
            "pending",
            "deploying",
            "unknown",
        }:
            provider_state = "unknown"
        if provider_state in {"pending", "deploying"} and not observation.get("provider_id"):
            provider_state = "unknown"  # an observation of a group names that group
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
            # Counts Salad did not send (a CPU-only group may omit them) are read as no live
            # instance for start admission, but never as evidence that a stop took effect.
            and observation.get("instance_counts_complete", True) is True
        )
        rejection = (
            observation.get("provider_rejection")
            if result.reason in CREATE_RELEASE_REASONS
            else None
        )
        return ProviderReadback(
            provider_rejection=rejection,
            provider_id=observation.get("provider_id"),
            allocation_id=allocation,
            state="unknown" if reallocated else provider_state,
            observed_at=observation.get("observed_at", utc_now()),
            response_status=observation.get("response_status"),
            termination_confirmed=bool(terminal) and not reallocated,
            provenance="provider",
            proxy_reason="reallocation_uncertain" if reallocated else result.reason,
            **_failed_read_diagnostics(
                result, "unknown" if reallocated else provider_state, reallocated
            ),
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
        """Trusted control-owner polling; never extends a lease or guardian deadline.

        Heartbeat independence (ADR 0075 amendment): the guardian beat goes out before any
        control write. Use-time authority is a read (no write lock), so another control writer
        holding the lock cannot keep the beat waiting. If that read itself stalls on a lock (it
        did not refuse), the beat is sent on the last verified authority while the soft lease
        seen by that verification is unexpired, and ``AuthorityStalled`` reports it. A refusal
        (Conflict/Forbidden) blocks the beat and is raised for the live driver to classify: on a
        launched route this server never cancels the guardian itself (a verified revocation is
        cancelled by the driver; anything else is held there). Revoke retries and ticket upkeep
        (control writes) come after the beat and never break it.
        """
        try:
            self._beat_route(route_id)
        finally:
            try:
                # Periodic retry of failed revokes for every route, not just this one; after
                # the beat (or its refusal), never in front of it.
                self.retry_pending_revokes()
            except Exception:
                self.input_log.append(dict(route_id=route_id, event="revoke_retry_failed"))

    def _beat_route(self, route_id):
        from .remote_execution_models import RemoteProposal

        verified = getattr(self, "_verified_authority", {}).get(route_id)
        state = None
        try:
            state = self.store.get(self.worker, route_id)
            proposal = RemoteProposal.model_validate(state["proposal"])
            state = self.store.preflight_authority(
                self.worker, route_id, proposal, "start", admit=False
            )
        except (Conflict, Forbidden):
            # A refusal ends beating on any earlier verification.
            getattr(self, "_verified_authority", {}).pop(route_id, None)
            launched = state is not None and any(
                effect["action"] == "start" for effect in state["effects"]
            )
            if not launched:
                self._authority_refused(route_id, state)
            raise
        except Exception as error:
            if not _lock_stall(error):
                # Not a lock stall (not found, invalid state, ...): never beat on the earlier
                # verification after a read that failed for a reason of its own.
                getattr(self, "_verified_authority", {}).pop(route_id, None)
                raise
            if verified is None or time.time() >= verified["lease_expires_at"]:
                raise AuthorityStalled(beat_delivered=False) from error
            self._beat(route_id, verified["state"])
            raise AuthorityStalled(beat_delivered=True) from error
        if not any(effect["action"] == "create" for effect in state["effects"]):
            raise Conflict("Guardian heartbeat requires existing create intent.")
        self._beat(route_id, state)
        expires = state.get("verified_lease_expires_at")
        if expires is not None:
            self.__dict__.setdefault("_verified_authority", {})[route_id] = dict(
                state=state, lease_expires_at=expires
            )
        try:
            self.refresh_inputs(route_id)
        except Exception as error:
            # Never let ticket upkeep break the liveness heartbeat; the failure is recorded.
            record = dict(route_id=route_id, event="refresh_failed", at=utc_now())
            if guardian_refusal_code(error) is not None:
                record["code"] = guardian_refusal_code(error)
            self.input_log.append(record)

    def _beat(self, route_id, state):
        try:
            self.safety.heartbeat(state)
        except Exception as error:
            # The guardian's closed refusal code (or "unavailable") lets the driver tell a
            # transient refusal from a terminal one; nothing else of the error is kept.
            # Only this call's errors are refusals; authority failures are not.
            code = guardian_refusal_code(error)
            if code is not None:
                self.input_log.append(
                    dict(route_id=route_id, event="heartbeat_refused", code=code, at=utc_now())
                )
            raise

    def _authority_refused(self, route_id, state):
        """Before launch only: best-effort guardian cancel and input revoke on a refusal.

        Revoked authority is terminal there. Best-effort cancel/revoke must never replace it: a
        guardian error here (unavailable, a refusal code, a timeout) would otherwise reach the
        driver as if the heartbeat itself had been refused."""
        if state is None:
            return
        try:
            self.safety.cancel(state)
        except Exception as error:
            record = dict(route_id=route_id, event="authority_cancel_failed", at=utc_now())
            if guardian_refusal_code(error) is not None:
                record["code"] = guardian_refusal_code(error)
            self.input_log.append(record)
        try:
            self._revoke_inputs(state)
        except Exception:
            self.input_log.append(
                dict(route_id=route_id, event="authority_revoke_failed", at=utc_now())
            )

    def cleanup_settled(self, cleanup=None, *, port=None, max_groups=1):
        """Opt-in hook: audited cleanup of settled (stopped, zero-instance) groups.

        Off by default and never called automatically. Live deletion has no activation
        gate in this milestone, so this hook refuses unless THIS server's own transport
        is marked non-live too: against a real server (real HTTP transport) it always
        refuses, because recording a delete through a fake port on routes of a real
        service would fake provider status. The caller must also pass an explicit
        non-live ``port``; the cleanup must belong to this server's worker and store;
        nothing runs inside a store transaction. The ``live is False`` marks guard
        against accidental live use; they are not a security boundary.
        """
        if cleanup is None:
            return dict(enabled=False, reason="cleanup_not_configured")
        from .salad_group_cleanup import ProviderError

        if getattr(self.transport, "live", True) is not False:
            raise ProviderError("live group deletion has no activation gate in this milestone")
        if cleanup.store is not self.store or cleanup.worker != self.worker:
            raise Conflict("Cleanup must use this server's route store and worker.")
        if port is None:
            raise ValueError("an explicit non-live provider port is required")
        cleanup.require_non_live(port)
        cleanup._require_no_transaction()
        return cleanup.apply(max_groups=max_groups, port=port, accept=self.store._bound)

    def import_retained(self, route_id, destination=None, partial=False):
        state = self.store.get(self.worker, route_id)
        # A partial import is only for an attempt the provider moved after custody began: the
        # route is flagged reallocated (and must be, for a partial import), and the provider's
        # own read still has to confirm the stop. A complete import needs an unmoved route.
        if (
            not self.store._bound(state)
            or not state["termination_confirmed"]
            or bool(state["reallocated"]) != bool(partial)
        ):
            raise Conflict("Exact terminal Salad route required for retained import.")
        # The job is complete: revoke the input ticket at control and clear this route's
        # input state so refresh_inputs can never re-mint after completion.
        self._revoke_inputs(state)
        extra = {"partial": True} if partial else {}
        if destination is None:
            return self.safety.import_retained(state, **extra)
        return self.safety.import_retained(state, destination=destination, **extra)
