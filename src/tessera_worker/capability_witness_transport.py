"""One-shot authenticated RPC to separately deployed, pinned witness daemons.

There is no imported-report API. Daemons must execute the fresh challenge, not
accept observations from a broker. Runtime references configure credentials, not
claimed outcomes. The existing numeric-IP TLS transport disables proxies/retries.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from .capability_activation import canonical, stamp
from .capability_kube_client import APIRequest, CollectionError, decode, encoded
from .capability_witness import (
    KINDS,
    Challenge,
    challenge_for,
    open_report,
    require,
    safe_payload,
    validate_report,
)

REQUEST_DOMAIN = b"tessera-witness-challenge-v1\0"


def partition(requests):
    result = {kind: {} for kind in KINDS}
    for key, value in requests.items():
        if key.startswith("network-"):
            kind = "cni"
        elif key.startswith(("enforcement-", "output-", "ephemeral-")) or key == "readonly-nonroot":
            kind = "watchdog" if key == "enforcement-time" else "quota"
        elif key.startswith("watchdog-"):
            kind = "watchdog"
        else:
            kind = "admission"
        result[kind][key] = value
    return result


class WitnessRPC:
    def __init__(self, *, pin, transport, request_key, report_key, monotonic=time.monotonic):
        if (
            type(request_key) is not bytes
            or type(report_key) is not bytes
            or min(len(request_key), len(report_key)) < 32
            or request_key == report_key
        ):
            raise CollectionError("auth-denied")
        self.pin, self.transport = pin, transport
        self._request_key, self._report_key = request_key, report_key
        self.clock = monotonic

    def observe(self, request, *, deadline):
        payload = canonical(request)
        body = encoded(
            {
                "request": decode(payload),
                "signature": hmac.new(
                    self._request_key, REQUEST_DOMAIN + payload, "sha256"
                ).hexdigest(),
            }
        )
        if self.clock() >= deadline:
            raise CollectionError("timeout")
        reply = self.transport.send(
            APIRequest("POST", "/v1/witness/" + request.kind, body),
            deadline=deadline,
            limit=2 * 1024 * 1024,
        )
        if self.clock() >= deadline:
            raise CollectionError("timeout")
        if reply.status != 200 or len(reply.body) > 2 * 1024 * 1024:
            raise CollectionError("unavailable")
        try:
            envelope = decode(reply.body)
            safe_payload(envelope, secrets=(self._request_key, self._report_key))
            return open_report(envelope, self._report_key), envelope
        except Exception:
            raise CollectionError("malformed-response") from None


def install_witness_tables(journal):
    with journal.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS witness_attempts (
                nonce TEXT PRIMARY KEY, operation TEXT UNIQUE NOT NULL,
                package TEXT UNIQUE NOT NULL, plan TEXT UNIQUE NOT NULL,
                challenge TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS witness_reports (
                nonce TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                payload BLOB NOT NULL, PRIMARY KEY(nonce,kind));
        """)
        for table in ("witness_attempts", "witness_reports"):
            for action in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'retained witness evidence'); END"
                )


class IndependentRuntimeWitnesses:
    """Exactly four distinct observer identities; all reports must pass together.

    Requests are parallel so the independent lease-expiry experiment can fit the
    existing five-second collection window. Daemons must enforce their own hard
    bounds. A hung/missing daemon cannot become a pass or an automatic retry.
    """

    def __init__(self, *, channels, journal, now=None, monotonic=time.monotonic):
        require(set(channels) == set(KINDS))
        require(all(type(c) is WitnessRPC and c.pin.kind == k for k, c in channels.items()))
        require(len({c.pin.identity for c in channels.values()}) == 4)
        # No single runtime signing/request key may impersonate another witness.
        keys = [key for c in channels.values() for key in (c._request_key, c._report_key)]
        require(len(set(keys)) == 8)
        self.channels, self.journal = dict(channels), journal
        self.now, self.clock = now or (lambda: datetime.now(UTC)), monotonic
        self.records = {}
        install_witness_tables(journal)

    def observe(self, plan, requests, *, deadline):
        m, started = plan.activation, self.now()
        challenge = secrets.token_hex(32)
        self.records = {}
        try:
            with self.journal.connect() as db:
                db.execute(
                    "INSERT INTO witness_attempts VALUES (?,?,?,?,?)",
                    (
                        m.nonce,
                        m.operation_id,
                        m.package_digest,
                        plan.digest,
                        challenge,
                    ),
                )
        except Exception:
            raise CollectionError("stale") from None
        require(
            all(
                c.pin.identity
                not in {
                    m.dispatcher_identity,
                    f"system:serviceaccount:{m.namespace}:{m.service_account}",
                }
                and c.pin.namespace != m.namespace
                for c in self.channels.values()
            )
        )
        remaining = min(5, deadline - self.clock(), (stamp(m.expires_at) - started).total_seconds())
        if remaining <= 0:
            raise CollectionError("stale")
        end = (started + timedelta(seconds=remaining)).isoformat()
        challenges = {
            kind: challenge_for(
                plan,
                kind,
                challenge=challenge,
                started_at=started.isoformat(),
                deadline=end,
                requests=inputs,
            )
            for kind, inputs in partition(requests).items()
        }
        self._challenges = challenges
        result, failures = {}, []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="runtime-witness") as pool:
            jobs = {
                kind: pool.submit(c.observe, challenges[kind], deadline=deadline)
                for kind, c in self.channels.items()
            }
            for kind, job in jobs.items():
                try:
                    report, envelope = job.result(timeout=max(0.001, deadline - self.clock()))
                    # Retain authenticated failed/unknown reports too. Unsafe payloads
                    # never reach this point. A crash retains the burned attempt.
                    with self.journal.connect() as db:
                        db.execute(
                            "INSERT INTO witness_reports VALUES (?,?,?,?)",
                            (
                                m.nonce,
                                kind,
                                envelope["digest"],
                                encoded(envelope),
                            ),
                        )
                    self.records[kind] = envelope
                    validate_report(
                        report, challenges[kind], self.channels[kind].pin, plan, now=self.now()
                    )
                    require(not (set(result) & set(report.probes)))
                    result.update(report.probes)
                except Exception:
                    failures.append(kind)
        if failures or self.clock() >= deadline:
            raise CollectionError("unavailable")
        require(set(result) == set(requests))
        return result

    def revalidate(self, plan, records, *, now):
        require(set(records) == set(KINDS) and hasattr(self, "_challenges"))
        for kind, envelope in records.items():
            channel = self.channels[kind]
            report = open_report(envelope, channel._report_key)
            require(report.request.plan_digest == plan.digest)
            validate_report(report, self._challenges[kind], channel.pin, plan, now=now)

    def require_api_agreement(self, objects):
        """Correlate separate channels; neither may override contradictory evidence."""
        policy = next(o for o in objects if o["kind"] == "NetworkPolicy")
        cni = self.records["cni"]["payload"]["observations"]
        require(
            (cni["policy_uid"], cni["policy_resource_version"])
            == (policy["metadata"]["uid"], policy["metadata"]["resourceVersion"])
        )
        admission = self.records["admission"]["payload"]["observations"]
        for key, kind in (
            ("policies", "ValidatingAdmissionPolicy"),
            ("bindings", "ValidatingAdmissionPolicyBinding"),
        ):
            require(admission[key] == [o for o in objects if o["kind"] == kind])


class WitnessDaemon:
    """Authenticated daemon-side execution core, owned by an EXTERNAL supervisor.

    Each daemon is deployed separately from workload/broker with its own clock,
    private journal and fixed sensor adapter. `execute` is deployment code, never
    a callable, command, path or observation supplied over the wire. It must run
    bounded disposable fixtures and gather node-side observations. Missing sensor
    adapters return unknown; this core does not fabricate them from API objects.
    """

    def __init__(
        self,
        *,
        pin,
        request_key,
        report_key,
        journal,
        approved_plan,
        harmless_images,
        execute,
        authority,
        now=None,
        monotonic=time.monotonic,
    ):
        from .capability_witness import WitnessPin

        self.pin = WitnessPin.model_validate_json(pin.model_dump_json())
        # Deployment-owned reviewed fixture image allowlist, never an assertion
        # in a submitted report. Admission's actual Pod must not run an installer
        # capable of external downloads just to test policy/defaulting.
        require(
            type(harmless_images) is frozenset and approved_plan.activation.image in harmless_images
        )
        require(
            type(request_key) is bytes
            and type(report_key) is bytes
            and min(len(request_key), len(report_key)) >= 32
            and request_key != report_key
        )
        self._request_key, self._report_key = request_key, report_key
        self.journal, self.plan, self.execute, self.authority = (
            journal,
            approved_plan,
            execute,
            authority,
        )
        self.now, self.clock = now or (lambda: datetime.now(UTC)), monotonic
        install_witness_tables(journal)

    def handle(self, body):
        from .capability_witness import WitnessReport, seal_report

        try:
            require(type(body) is bytes and len(body) <= 2 * 1024 * 1024)
            message = decode(body)
            require(set(message) == {"request", "signature"})
            payload = safe_payload(
                message["request"], secrets=(self._request_key, self._report_key)
            )
            require(
                hmac.compare_digest(
                    message["signature"],
                    hmac.new(self._request_key, REQUEST_DOMAIN + payload, "sha256").hexdigest(),
                )
            )
            request = Challenge.model_validate_json(payload)
            require(request.kind == self.pin.kind)
            # Reconstruct all binding fields, rather than trusting just the digest.
            from .capability_inspection import probe_requests

            probes = {k: v for k, v in probe_requests(self.plan).items() if "dryRun" not in v}
            expected = challenge_for(
                self.plan,
                self.pin.kind,
                challenge=request.challenge,
                started_at=request.started_at,
                deadline=request.deadline,
                requests=partition(probes)[self.pin.kind],
            )
            require(request == expected)
            require(
                stamp(request.started_at)
                <= self.now()
                < stamp(request.deadline)
                <= stamp(self.plan.activation.expires_at)
            )
            require(0 < (stamp(request.deadline) - stamp(request.started_at)).total_seconds() <= 5)
            with self.journal.connect() as db:
                db.execute(
                    "INSERT INTO witness_attempts VALUES (?,?,?,?,?)",
                    (
                        request.run_nonce,
                        request.operation_id,
                        request.package_digest,
                        request.plan_digest,
                        request.challenge,
                    ),
                )
            # Burn before revocation check; a previously signed request is not authority.
            require(self.authority(self.plan) is True)
            start, clock = self.now(), self.clock()
            deadline = clock + (stamp(request.deadline) - start).total_seconds()
            data = self.execute(request, deadline=deadline)
            require(self.authority(self.plan) is True)
            report = WitnessReport(
                source="independent-daemon",
                request=request,
                request_digest=hashlib.sha256(canonical(request)).hexdigest(),
                witness=self.pin,
                started_at=start.isoformat(),
                completed_at=self.now().isoformat(),
                monotonic_elapsed_ms=int((self.clock() - clock) * 1000),
                **data,
            )
            if report.state == "observed":
                validate_report(report, request, self.pin, self.plan, now=self.now())
            envelope = seal_report(report, self._report_key)
            with self.journal.connect() as db:
                db.execute(
                    "INSERT INTO witness_reports VALUES (?,?,?,?)",
                    (
                        request.run_nonce,
                        request.kind,
                        envelope["digest"],
                        encoded(envelope),
                    ),
                )
            return encoded(envelope)
        except Exception:
            # No upstream text, request bodies or credential bytes in diagnostics.
            raise CollectionError("unavailable") from None


def cleanup_owned(resource, request, *, read, delete, observer_pin=None):
    """One exact read/delete/read, UID+resourceVersion preconditions, zero retry.

    `read`/`delete` are fixed authenticated node/API adapters, not workload hooks.
    The owner resource came from this attempt's create response; never enumerate
    or delete by label, namespace, prefix or a report's claimed resource list.
    """
    require(resource.run_nonce == request.run_nonce)
    scopes = {(request.namespace, request.namespace_uid)}
    if observer_pin is not None:
        require(observer_pin.kind == request.kind)
        scopes.add((observer_pin.namespace, observer_pin.namespace_uid))
    require((resource.namespace, resource.namespace_uid) in scopes)
    got = read(resource)
    meta = got["metadata"]
    require(
        (meta["uid"], meta["resourceVersion"], meta["namespace"], meta["name"])
        == (resource.uid, resource.resource_version, resource.namespace, resource.name)
    )
    require(meta["annotations"]["tessera.io/nonce"] == request.run_nonce)
    delete(resource, {"uid": resource.uid, "resourceVersion": resource.resource_version})
    # A delete reply is not proof of cleanup/termination.
    require(read(resource) == {"kind": "Status", "code": 404, "reason": "NotFound"})


def serve_witness(*, address, tls_context, bearer, daemon, client_certificate_sha256=None):
    """Explicit external-process entrypoint; no listener is started on import.

    The node's supervisor supplies a server-only TLS context with its pinned
    certificate, an existing runtime bearer and a WitnessDaemon. No files, keys,
    credentials, adapters or effects are discovered from the environment. Run
    under an independent service manager with CPU/memory/time limits. Never call
    from the broker process as an alleged independent witness.
    """
    import ipaddress
    import ssl
    import uuid
    from http.server import BaseHTTPRequestHandler, HTTPServer

    ipaddress.ip_address(address[0])
    require(type(address[1]) is int and 1 <= address[1] <= 65535)
    require(tls_context.protocol == ssl.PROTOCOL_TLS_SERVER)
    require(tls_context.minimum_version >= ssl.TLSVersion.TLSv1_2)
    # Target-specific services require mutual TLS in addition to HMAC/bearer.
    # Legacy synthetic/protocol deployments retain their existing contract.
    if daemon.pin.target_digest is not None:
        require(tls_context.verify_mode == ssl.CERT_REQUIRED)
        require(type(client_certificate_sha256) is str and len(client_certificate_sha256) == 64)
    require(type(bearer) is str and len(bearer) >= 32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass  # Never log URLs, Authorization, request bodies or exceptions.

        def do_POST(self):
            status, body = 503, b'{"state":"unknown"}'
            try:
                if client_certificate_sha256 is not None:
                    require(
                        hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
                        == client_certificate_sha256
                    )
                require(self.path == "/v1/witness/" + daemon.pin.kind)
                auth = self.headers.get_all("Authorization")
                require(auth is not None and len(auth) == 1)
                require(hmac.compare_digest(auth[0], "Bearer " + bearer))
                require(self.headers.get_all("Content-Type") == ["application/json"])
                require(not self.headers.get_all("Transfer-Encoding"))
                lengths = self.headers.get_all("Content-Length")
                require(lengths is not None and len(lengths) == 1 and lengths[0].isdigit())
                length = int(lengths[0])
                require(0 < length <= 2 * 1024 * 1024)
                deadline, data = time.monotonic() + 5, bytearray()
                while len(data) < length:
                    remaining = deadline - time.monotonic()
                    require(remaining > 0)
                    self.connection.settimeout(remaining)
                    part = self.rfile.read1(min(65536, length - len(data)))
                    require(bool(part))
                    data.extend(part)
                body = daemon.handle(bytes(data))
                safe_payload(decode(body), secrets=(bearer.encode(),))
                status = 200
            except Exception:
                body = b'{"state":"unknown"}'
            self.close_connection = True
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Audit-Id", uuid.uuid4().hex)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # Lost reply stays consumed in daemon custody; no retry.

    class Server(HTTPServer):
        def get_request(self):
            raw, peer = super().get_request()
            raw.settimeout(5)
            try:
                return tls_context.wrap_socket(raw, server_side=True), peer
            except Exception:
                raw.close()
                raise

    with Server(address, Handler) as server:
        server.serve_forever(poll_interval=0.1)
