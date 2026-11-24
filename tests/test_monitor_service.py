"""Opt-in execution-monitor listener in `serve` (ADR 0127). Localhost TLS only, fake workload."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from test_execution import _owner, _worker
from test_execution_monitor import KEYS, FakeAdapter, assignment

from tessera_control import cli
from tessera_control import monitor_service as service_module
from tessera_control.monitor_evidence import accept_ack
from tessera_control.monitor_service import (
    EnrollmentRefused,
    MonitorService,
    load_enrollments,
    make_authorizer,
)
from tessera_control.store import Store, _connect, canonical_digest, initialize_state
from tessera_monitor.channel import connect_tls
from tessera_monitor.journal import MonitorJournal
from tessera_monitor.protocol import Denied, Pulse, digest, open_envelope, seal
from tessera_monitor.runtime import Monitor

OTHER_KEYS = dict(authority_key=b"A" * 32, channel_key=b"C" * 32, evidence_key=b"E" * 32)


# -- localhost PKI -------------------------------------------------------------------------


def _private(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _pki_cryptography(root: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = dt.datetime.now(dt.UTC)

    def name(common):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

    def key_pem(key):
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def authority(common):
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(name(common))
            .issuer_name(name(common))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        return key, cert

    def leaf(ca_key, ca_cert, common, usage, stem):
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(name(common))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
        )
        if usage == ExtendedKeyUsageOID.SERVER_AUTH:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]
                ),
                critical=False,
            )
        cert = builder.sign(ca_key, hashes.SHA256())
        (root / f"{stem}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        _private(root / f"{stem}.key", key_pem(key))

    ca_key, ca_cert = authority("synthetic test CA")
    (root / "ca.pem").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    other_key, other_cert = authority("synthetic foreign CA")
    leaf(ca_key, ca_cert, "server", ExtendedKeyUsageOID.SERVER_AUTH, "server")
    leaf(ca_key, ca_cert, "monitor", ExtendedKeyUsageOID.CLIENT_AUTH, "client")
    leaf(ca_key, ca_cert, "unpinned", ExtendedKeyUsageOID.CLIENT_AUTH, "rogue")
    leaf(other_key, other_cert, "foreign", ExtendedKeyUsageOID.CLIENT_AUTH, "foreign")


def _pki_openssl(root: Path) -> None:
    def run(*args):
        subprocess.run(["openssl", *args], cwd=root, check=True, capture_output=True, timeout=60)

    ext = root / "ext.cnf"
    ext.write_text(
        "[server]\nsubjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
        "[client]\nextendedKeyUsage=clientAuth\n"
    )
    for ca in ("ca", "foreignca"):
        run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{ca}.key")
        run("req", "-x509", "-new", "-key", f"{ca}.key", "-subj", f"/CN={ca}", "-days", "1",
            "-out", f"{ca}.pem")  # fmt: skip
    for stem, ca, section in (
        ("server", "ca", "server"),
        ("client", "ca", "client"),
        ("rogue", "ca", "client"),
        ("foreign", "foreignca", "client"),
    ):
        run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{stem}.raw")
        run("pkcs8", "-topk8", "-nocrypt", "-in", f"{stem}.raw", "-out", f"{stem}.key")
        run("req", "-new", "-key", f"{stem}.key", "-subj", f"/CN={stem}", "-out", f"{stem}.csr")
        run("x509", "-req", "-in", f"{stem}.csr", "-CA", f"{ca}.pem", "-CAkey", f"{ca}.key",
            "-CAcreateserial", "-days", "1", "-extfile", "ext.cnf", "-extensions", section,
            "-out", f"{stem}.pem")  # fmt: skip
        (root / f"{stem}.key").chmod(0o600)


def _fingerprint(pem: Path) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem.read_text())).hexdigest()


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    root = tmp_path_factory.mktemp("pki")
    root.chmod(0o700)
    try:
        import cryptography  # noqa: F401

        _pki_cryptography(root)
    except ImportError:
        if shutil.which("openssl") is None:
            pytest.skip("neither the cryptography package nor the openssl CLI is available")
        _pki_openssl(root)
    return types.SimpleNamespace(
        root=root,
        ca=root / "ca.pem",
        server=_fingerprint(root / "server.pem"),
        client=_fingerprint(root / "client.pem"),
        rogue=_fingerprint(root / "rogue.pem"),
    )


# -- control store, assignment and enrollment -----------------------------------------------


@pytest.fixture
def control(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    owner = _owner(state)
    _, project = Store(state).create_project(
        owner, "project", canonical_digest({"name": "Pilot"}), {"name": "Pilot"}
    )
    worker = _worker(state, owner, project["id"], "worker-token")
    return types.SimpleNamespace(
        state=state,
        tenant=owner["org_id"],
        project=project["id"],
        credential=worker["credential_id"],
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def live_assignment(control, monitor="monitor-a", lifetime_ms=60000):
    now = time.time_ns() // 1_000_000
    base = assignment()
    return assignment(
        tenant=control.tenant,
        project=control.project,
        monitor=monitor,
        issued_ms=now,
        expires_ms=now + lifetime_ms,
        limits=base.limits.model_copy(update={"runtime_seconds": 60, "lease_seconds": 10}),
    )


class Enrollment:
    """Writes one owner-only enrollment file and the entries' key/assignment files."""

    def __init__(self, tmp_path, pki, control, port=None):
        self.dir = tmp_path / "enroll"
        self.dir.mkdir(mode=0o700)
        self.path = self.dir / "monitors.json"
        self.listener = {
            "bind_ip": "127.0.0.1",
            "port": port or _free_port(),
            "server_cert": str(pki.root / "server.pem"),
            "server_key": str(pki.root / "server.key"),
            "client_ca": str(pki.ca),
        }
        self.monitors: list[dict] = []
        self.control = control

    def add(self, a, keys=KEYS, pin=None, **changes):
        stem = self.dir / a.monitor
        stem.mkdir(mode=0o700, exist_ok=True)
        entry = {
            "monitor": a.monitor,
            "tenant": a.tenant,
            "project": a.project,
            "assignment": str(
                _private(
                    stem / "assignment.json",
                    seal(a, keys["authority_key"], b"assignment").model_dump_json().encode(),
                )
            ),
            **{name: str(_private(stem / name, value)) for name, value in keys.items()},
            "client_cert_sha256": pin or secrets.token_hex(32),
            "worker_credential_id": self.control.credential,
            "active": True,
        }
        entry.update(changes)
        self.monitors.append(entry)
        return entry

    def write(self, document=None):
        document = document or {
            "schema": "tessera.monitor-enrollments.v1",
            "listener": self.listener,
            "monitors": self.monitors,
        }
        staged = self.dir / "staged.json"
        _private(staged, json.dumps(document).encode())
        os.replace(staged, self.path)
        return self.path


def _refused(path):
    with pytest.raises(EnrollmentRefused) as caught:
        load_enrollments(path)
    message = str(caught.value)
    assert "/" not in message and "aaaa" not in message
    return message


# -- loader ---------------------------------------------------------------------------------


def test_a_valid_enrollment_loads(tmp_path, pki, control):
    e = Enrollment(tmp_path, pki, control)
    e.add(live_assignment(control), pin=pki.client)
    loaded = load_enrollments(e.write())
    assert list(loaded.monitors) == ["monitor-a"]
    assert loaded.monitors["monitor-a"].keys == KEYS


def test_loader_refuses_group_readable_symlinked_or_relative_files(tmp_path, pki, control):
    e = Enrollment(tmp_path, pki, control)
    e.add(live_assignment(control))
    path = e.write()
    path.chmod(0o640)
    _refused(path)
    path.chmod(0o600)
    link = e.dir / "link.json"
    link.symlink_to(path)
    _refused(link)
    _refused(Path("monitors.json"))
    _refused(e.dir / "missing.json")


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda e, d: d.update(schema="tessera.other.v1"), "document"),
        (lambda e, d: d.update(extra=True), "document"),
        (lambda e, d: d.update(monitors=[]), "document"),
        (lambda e, d: d["listener"].update(bind_ip="localhost"), "numeric bind_ip"),
        (lambda e, d: d["listener"].update(client_ca="ca.pem"), "numeric bind_ip"),
        (lambda e, d: d["monitors"][0].update(inline_key="a" * 64), "entry #1"),
        (lambda e, d: d["monitors"][0].update(active="yes"), "entry #1"),
        (lambda e, d: d["monitors"][0].update(client_cert_sha256="F" * 64), "entry #1"),
        (lambda e, d: d["monitors"][0].pop("worker_credential_id"), "entry #1"),
        (lambda e, d: d["monitors"][0].update(tenant="org_other"), "differs from the entry"),
        (lambda e, d: d["monitors"][0].update(monitor="monitor-z"), "differs from the entry"),
    ],
)
def test_loader_refuses_malformed_documents(tmp_path, pki, control, mutate, reason):
    e = Enrollment(tmp_path, pki, control)
    e.add(live_assignment(control))
    document = {"schema": "tessera.monitor-enrollments.v1", "listener": e.listener}
    document["monitors"] = e.monitors
    mutate(e, document)
    assert reason in _refused(e.write(document))


def test_loader_refuses_duplicate_ids_and_pins(tmp_path, pki, control):
    e = Enrollment(tmp_path, pki, control)
    e.add(live_assignment(control), pin=pki.client)
    e.monitors.append(dict(e.monitors[0], client_cert_sha256=pki.rogue))
    assert "enrolled twice" in _refused(e.write())
    e.monitors.pop()
    e.add(live_assignment(control, monitor="monitor-b"), keys=OTHER_KEYS, pin=pki.client)
    assert "pin is already enrolled" in _refused(e.write())


def test_loader_refuses_bad_keys(tmp_path, pki, control):
    e = Enrollment(tmp_path, pki, control)
    entry = e.add(live_assignment(control))
    channel = Path(entry["channel_key"])
    channel.chmod(0o644)
    assert "32-byte" in _refused(e.write())
    _private(channel, b"c" * 31)
    assert "32-byte" in _refused(e.write())
    _private(channel, KEYS["evidence_key"])
    assert "distinct" in _refused(e.write())
    _private(channel, KEYS["channel_key"])
    entry["channel_key"] = "relative/key"
    assert "32-byte" in _refused(e.write())
    entry["channel_key"] = str(channel)
    load_enrollments(e.write())
    # Another monitor reusing this monitor's channel key is refused; a shared issuer
    # (authority) key is allowed.
    shared = dict(OTHER_KEYS, authority_key=KEYS["authority_key"])
    e.add(live_assignment(control, monitor="monitor-b"), keys=shared)
    load_enrollments(e.write())
    e.add(
        live_assignment(control, monitor="monitor-c"),
        keys=dict(OTHER_KEYS, channel_key=KEYS["channel_key"], evidence_key=b"Z" * 32),
    )
    assert "shared with another key" in _refused(e.write())


def test_loader_refuses_an_assignment_under_another_authority(tmp_path, pki, control):
    e = Enrollment(tmp_path, pki, control)
    entry = e.add(live_assignment(control))
    a = live_assignment(control)
    _private(
        Path(entry["assignment"]),
        seal(a, b"x" * 32, b"assignment").model_dump_json().encode(),
    )
    assert "opens under its authority key" in _refused(e.write())


# -- serve wiring ---------------------------------------------------------------------------


def test_serve_without_the_flag_starts_no_listener(monkeypatch):
    from test_campaign_serve_wiring import _serve

    assert (
        cli.parser().parse_args(["serve", "--state-dir", "/x", "--port", "9"]).monitor_enrollments
        is None
    )

    def forbidden(*_a, **_k):
        raise AssertionError("monitor service constructed without opt-in")

    monkeypatch.setattr(service_module, "MonitorService", forbidden)
    monkeypatch.setattr(service_module, "load_enrollments", forbidden)
    code, _ = _serve(monkeypatch)
    assert code == 0


def test_serve_refuses_a_bad_enrollment_file_before_serving(monkeypatch, tmp_path, pki, control):
    from test_campaign_serve_wiring import _serve

    e = Enrollment(tmp_path, pki, control)
    e.add(live_assignment(control))
    path = e.write()
    path.chmod(0o644)
    code, _ = _serve(monkeypatch, "--monitor-enrollments", str(path))
    assert code == 2


def test_serve_starts_the_listener_with_the_app_and_stops_it_on_shutdown(
    monkeypatch, tmp_path, pki, control
):
    from fastapi.testclient import TestClient

    e = Enrollment(tmp_path, pki, control)
    e.add(live_assignment(control), pin=pki.client)
    path = e.write()
    port = e.listener["port"]
    seen = []

    def fake_run(app, host, port_):
        with TestClient(app) as client:
            assert client.get("/health/live").status_code == 200
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                seen.append("listening")
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        seen.append("closed")

    monkeypatch.setattr(cli.uvicorn, "run", lambda app, host, port: fake_run(app, host, port))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera",
            "serve",
            "--state-dir",
            str(control.state),
            "--port",
            "9",
            "--no-campaign-runner",
            "--campaign-stuck-scan-seconds",
            "0",
            "--monitor-enrollments",
            str(path),
        ],
    )
    assert cli.main() == 0
    assert seen == ["listening", "closed"]
    journal = control.state / "execution-monitors" / "monitor-a"
    assert journal.is_dir() and journal.stat().st_mode & 0o777 == 0o700


# -- end to end over localhost mutual TLS ---------------------------------------------------


def _dial(pki, port, stem="client"):
    return connect_tls(
        address="127.0.0.1",
        port=port,
        server_name="127.0.0.1",
        ca=str(pki.ca),
        certificate=str(pki.root / f"{stem}.pem"),
        private_key=str(pki.root / f"{stem}.key"),
        peer_sha256=pki.server,
    )


class Pulser:
    def __init__(self, a, keys=KEYS):
        self.a, self.keys, self.sequence = a, keys, 0

    def __call__(self, channel):
        self.sequence += 1
        pulse = Pulse(
            assignment=digest(self.a),
            nonce=self.a.nonce,
            monitor=self.a.monitor,
            sequence=self.sequence,
            challenge=secrets.token_hex(32),
            action="challenge",
            issued_ms=time.time_ns() // 1_000_000,
        )
        channel.send(seal(pulse, self.keys["channel_key"], b"challenge"))
        response = open_envelope(channel.receive(), Pulse, self.keys["channel_key"], b"response")
        assert response.model_copy(update={"action": "challenge"}) == pulse
        return response.action


@pytest.fixture
def running(tmp_path, pki, control):
    e = Enrollment(tmp_path, pki, control)
    a = live_assignment(control)
    e.add(a, pin=pki.client)
    e.write()
    service = MonitorService(e.path, control.state)
    service.start()
    yield types.SimpleNamespace(e=e, a=a, service=service, port=e.listener["port"])
    assert service.stop()


def _challenges(service, monitor="monitor-a"):
    with service._slots[monitor].journal.connect() as db:
        return db.execute("SELECT count(*) FROM monitor_challenges").fetchone()[0]


def test_pinned_monitor_gets_continue_and_its_evidence_is_acked_and_journaled(
    tmp_path, pki, running
):
    a, service = running.a, running.service
    document = seal(a, KEYS["authority_key"], b"assignment")
    monitor_journal = MonitorJournal(tmp_path / "monitor")
    channel = _dial(pki, running.port)
    try:
        pulse = Pulser(a)
        assert pulse(channel) == "continue"
        m = Monitor(
            document, identity=a.monitor, journal=monitor_journal, adapter=FakeAdapter(a), **KEYS
        )
        [armed] = monitor_journal.documents()
        channel.send(armed)
        accept_ack(monitor_journal, channel.receive(), a, KEYS["channel_key"])
        assert [r.kind for r in service._slots["monitor-a"].receiver.records()] == ["armed"]
        assert m.records[0].kind == "armed"
        status = service.status()["monitor-a"]
        assert status["activation"] == "blocked" and status["authentic"] is False
    finally:
        channel.close()
        monitor_journal.close()
    root = running.e.control.state / "execution-monitors"
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "monitor-a").stat().st_mode & 0o777 == 0o700


def _unread(channel):
    try:
        channel.receive()
    except Denied:
        return True
    except (OSError, ssl.SSLError):
        return True
    return False


def test_an_unpinned_or_foreign_client_is_dropped_with_nothing_journaled(pki, running):
    a, service = running.a, running.service
    for stem in ("rogue", "foreign"):
        try:
            channel = _dial(pki, running.port, stem)
        except Denied:
            continue  # the handshake itself refused the certificate
        try:
            pulse = Pulse(
                assignment=digest(a),
                nonce=a.nonce,
                monitor=a.monitor,
                sequence=1,
                challenge="9" * 64,
                action="challenge",
                issued_ms=time.time_ns() // 1_000_000,
            )
            try:
                channel.send(seal(pulse, KEYS["channel_key"], b"challenge"))
            except OSError:
                pass
            assert _unread(channel)
        finally:
            channel.close()
    assert _challenges(service) == 0
    assert service._slots["monitor-a"].receiver.records() == []


def test_a_second_connection_for_the_same_monitor_is_dropped(pki, running):
    first = _dial(pki, running.port)
    pulse = Pulser(running.a)
    try:
        assert pulse(first) == "continue"
        second = _dial(pki, running.port)
        try:
            assert _unread(second)
        finally:
            second.close()
        assert pulse(first) == "continue"
    finally:
        first.close()


def _set(e, **changes):
    e.monitors[0].update(changes)
    e.write()


def test_operator_enrollment_and_credential_revoke_the_next_response(pki, running, control):
    e = running.e
    channel = _dial(pki, running.port)
    pulse = Pulser(running.a)
    try:
        assert pulse(channel) == "continue"
        _set(e, active=False)
        assert pulse(channel) == "revoke"
        _set(e, active=True)
        assert pulse(channel) == "continue"  # re-read at each check, never cached
        saved = e.monitors.pop()
        e.write()
        assert pulse(channel) == "revoke"
        e.monitors.append(saved)
        e.write()
        assert pulse(channel) == "continue"
        Path(e.path).chmod(0o640)
        assert pulse(channel) == "revoke"  # an unreadable file is not authority
        Path(e.path).chmod(0o600)
        _set(e, client_cert_sha256=pki.rogue)
        assert pulse(channel) == "revoke"  # a changed entry needs a restart
        _set(e, client_cert_sha256=pki.client)
        assert pulse(channel) == "continue"
        with _connect(control.state / "tessera.sqlite3") as db:
            db.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ?",  # synthetic revoke
                ("2026-01-01T00:00:00Z", control.credential),
            )
        assert pulse(channel) == "revoke"
    finally:
        channel.close()
    assert running.service.status()["monitor-a"]["activation"] == "blocked"


def test_a_replaced_assignment_file_revokes(pki, running):
    e = running.e
    channel = _dial(pki, running.port)
    pulse = Pulser(running.a)
    try:
        assert pulse(channel) == "continue"
        other = running.a.model_copy(update={"operation": "operation-b"})
        _private(
            Path(e.monitors[0]["assignment"]),
            seal(other, KEYS["authority_key"], b"assignment").model_dump_json().encode(),
        )
        assert pulse(channel) == "revoke"
    finally:
        channel.close()


def test_expiry_is_revoke_at_the_authorizer_and_closes_the_wire(tmp_path, pki, control):
    e = Enrollment(tmp_path, pki, control)
    a = live_assignment(control, lifetime_ms=1500)
    e.add(a, pin=pki.client)
    e.write()
    enrolled = load_enrollments(e.path).monitors["monitor-a"]
    database = control.state / "tessera.sqlite3"
    assert make_authorizer(e.path, database, enrolled, now=lambda: a.expires_ms - 1)(a)
    assert not make_authorizer(e.path, database, enrolled, now=lambda: a.expires_ms)(a)
    assert not make_authorizer(e.path, database, enrolled)(a.model_copy(update={"nonce": "8" * 64}))
    service = MonitorService(e.path, control.state)
    service.start()
    try:
        channel = _dial(pki, e.listener["port"])
        try:
            pulse = Pulser(a)
            assert pulse(channel) == "continue"
            time.sleep(max(0.0, (a.expires_ms - time.time_ns() // 1_000_000) / 1000) + 0.05)
            # The receiver refuses a challenge after expiry outright: no continue is sent.
            with pytest.raises(Denied):
                pulse(channel)
        finally:
            channel.close()
    finally:
        assert service.stop()


def test_a_journal_holding_another_assignment_refuses_startup(tmp_path, pki, control):
    e = Enrollment(tmp_path, pki, control)
    e.add(live_assignment(control), pin=pki.client)
    e.write()
    first = MonitorService(e.path, control.state)
    first.start()
    first.stop()
    _private(
        Path(e.monitors[0]["assignment"]),
        seal(live_assignment(control), KEYS["authority_key"], b"assignment")
        .model_dump_json()
        .encode(),
    )
    with pytest.raises(service_module.MonitorServiceError, match="monitor-a"):
        MonitorService(e.path, control.state).start()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", e.listener["port"]), timeout=1).close()
