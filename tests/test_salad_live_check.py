"""Isolated orchestration only: never GPU/provider/deployed-service evidence."""

import hashlib
import json
from unittest.mock import patch

import pytest

from tessera_control.salad_live_check import (
    SaladLiveSession,
    _config,
    _drive,
    _private_bytes,
    _write_receipt,
    main,
    readiness,
    run,
    source_digest,
)


class LocalSession:
    def __init__(self, *, prior=False):
        self.actions = []
        self.data = b'{"local":"synthetic"}'
        self.record = {
            "proposal_digest": "same-route",
            "provider_status": "stopped",
            "effects": [{"action": "create"}, {"action": "start"}] if prior else [],
        }
        self.proof = {
            "output_retained": True,
            "stop_confirmed": True,
            "writers_quiescent": True,
            "output_byte_cap": 65536,
            "output_sha256": hashlib.sha256(self.data).hexdigest(),
            "provider_id": "synthetic-group",
        }
        self.fail = None
        self.cancelled = False
        self.corrupt_reconnect = False
        self.keepalives = 0
        self.settling = 0
        self.refuse_keepalive = False
        self.expects_input = False
        self.deliver_error = None
        self.revoke_error = None
        self.input_calls = []

    def state(self, route):
        return json.loads(json.dumps(self.record))

    def dispatch(self, route, action):
        self.actions.append(action)
        if self.fail == action:
            raise RuntimeError("SECRET must not appear")
        self.record["effects"].append({"action": action})
        if action == "observe" and self.settling:
            self.settling -= 1
            return dict(self.state(route), provider_status="unknown")
        return self.state(route)

    def keepalive(self, route):
        self.keepalives += 1
        if self.refuse_keepalive and "start" in self.actions:
            raise RuntimeError("guardian refuses heartbeat")

    def cancel(self, route):
        self.cancelled = True
        self.input_calls.append("cancel")

    def input_expected(self, route):
        return self.expects_input

    def deliver_inputs(self, route):
        self.input_calls.append("deliver")
        if self.deliver_error:
            raise self.deliver_error

    def revoke_inputs(self, route):
        self.input_calls.append("revoke")
        if self.revoke_error:
            raise self.revoke_error
        return self.expects_input

    def evidence(self, route):
        return self.proof

    def retained_bytes(self, route):
        return self.data

    def reopen(self):
        if self.corrupt_reconnect:
            other = LocalSession()
            other.record["proposal_digest"] = "changed"
            return other
        return self


def drive(session, *, max_polls=2):
    return _drive(
        session,
        route_id="exact-route",
        source_revision="a" * 40,
        max_seconds=10,
        max_polls=max_polls,
        sleep=lambda _: None,
        clock=lambda: 0,
    )


def test_default_and_run_cli_never_read_files_or_network(capsys):
    with (
        patch("builtins.open", side_effect=AssertionError("file read")),
        patch("socket.socket", side_effect=AssertionError("network")),
    ):
        assert main([]) == 0
        default = json.loads(capsys.readouterr().out)
        assert default == readiness()
        assert main(["--run"]) == 2
        opted = json.loads(capsys.readouterr().out)
    assert opted["effects"] == opted["network_calls"] == opted["credential_reads"] == 0
    assert not opted["live_ready"]
    assert opted["reason"] == "explicit_private_server_config_and_receipt_directory_required"


def test_config_paths_without_run_are_not_opened_or_probed(capsys):
    with (
        patch("builtins.open", side_effect=AssertionError("file read")),
        patch("socket.socket", side_effect=AssertionError("network")),
    ):
        assert (
            main(
                [
                    "--server-config",
                    "/nonexistent/private.json",
                    "--receipt-directory",
                    "/nonexistent/receipts",
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
    assert result == readiness()
    assert not result["live_ready"]


def test_local_orchestration_stop_bytes_and_reconnect():
    session = LocalSession()
    receipt = drive(session)
    assert receipt["status"] == "passed"
    assert receipt["stop_confirmed"] and receipt["reconnected"]
    assert session.actions == ["create", "observe", "start", "observe", "stop", "observe"]
    assert session.cancelled
    assert receipt["output_sha256"] == hashlib.sha256(session.data).hexdigest()
    assert "local" not in json.dumps(receipt)


def test_existing_claims_never_launch_again():
    session = LocalSession(prior=True)
    assert drive(session)["status"] == "passed"
    assert "create" not in session.actions and "start" not in session.actions


def test_existing_stop_claim_reconciles_without_reissuing_stop():
    session = LocalSession(prior=True)
    session.record["effects"].append({"action": "stop"})
    assert drive(session)["status"] == "passed"
    assert set(session.actions) == {"observe"}


def test_normal_output_while_running_triggers_stop_then_byte_verification_and_reconnect():
    class RunningOutputSession(LocalSession):
        def __init__(self):
            super().__init__()
            self.proof.update(stop_confirmed=False, writers_quiescent=False)
            self.states = []

        def dispatch(self, route, action):
            if action == "start":
                self.record["provider_status"] = "running"
            elif action == "stop":
                self.record["provider_status"] = "stopped"
                self.proof.update(stop_confirmed=True, writers_quiescent=True)
            return super().dispatch(route, action)

        def evidence(self, route):
            self.states.append((self.record["provider_status"], self.cancelled))
            return self.proof

        def retained_bytes(self, route):
            assert self.proof["stop_confirmed"] and self.proof["writers_quiescent"]
            return super().retained_bytes(route)

    session = RunningOutputSession()
    receipt = drive(session)
    assert receipt["status"] == "passed"
    assert session.states[0] == ("running", False)
    assert session.states[1] == ("stopped", True)
    assert session.actions == ["create", "observe", "start", "observe", "stop", "observe"]
    assert (
        receipt["reconnected"]
        and receipt["output_sha256"] == hashlib.sha256(session.data).hexdigest()
    )


@pytest.mark.parametrize("action", ["create", "start", "observe", "stop"])
def test_failure_still_exact_stop_without_retry_or_secret(action):
    session = LocalSession()
    session.fail = action
    receipt = drive(session)
    assert receipt["status"] == "inconclusive"
    assert session.cancelled and session.actions.count("stop") == 1
    assert session.actions.count("create") <= 1 and session.actions.count("start") <= 1
    assert "SECRET" not in json.dumps(receipt)


@pytest.mark.parametrize("field", ["stop_confirmed", "writers_quiescent", "output_retained"])
def test_unknown_or_unretained_is_never_passed(field):
    session = LocalSession()
    session.proof[field] = False
    assert drive(session)["status"] == "inconclusive"
    assert session.actions.count("stop") == 1
    assert len(session.actions) <= 8


@pytest.mark.parametrize("failure", ["digest", "size", "identity", "reconnect"])
def test_evidence_integrity_failure(failure):
    session = LocalSession()
    if failure == "digest":
        session.proof["output_sha256"] = "0" * 64
    elif failure == "size":
        session.proof["output_byte_cap"] = 1
    elif failure == "identity":
        session.proof["provider_id"] = "raw response\nSECRET"
    else:
        session.corrupt_reconnect = True
    receipt = drive(session)
    assert receipt["status"] == "inconclusive"
    assert "SECRET" not in json.dumps(receipt)


def test_fake_session_cannot_enter_public_live_driver(tmp_path):
    session = LocalSession()
    session.server = object()
    session.dispatcher = object()
    with pytest.raises(ValueError, match="session"):
        run(session, route_id="exact", source_revision="a" * 40, receipt_directory=tmp_path)
    assert session.actions == []


def test_receipt_private_exclusive_and_no_symlink(tmp_path):
    tmp_path.chmod(0o700)
    _write_receipt(tmp_path, {"status": "inconclusive"})
    target = tmp_path / "salad-live-receipt.json"
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        _write_receipt(tmp_path, {})
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    (other / target.name).symlink_to(target)
    with pytest.raises(FileExistsError):
        _write_receipt(other, {})
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        _write_receipt(tmp_path, {})


def test_explicit_invalid_run_has_closed_error_and_no_network(capsys):
    with patch("socket.socket", side_effect=AssertionError("network")):
        assert (
            main(
                ["--run", "--server-config", "/nonexistent", "--receipt-directory", "/nonexistent"]
            )
            == 2
        )
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report.pop("error") == {
        "exception": "ValueError",
        "message": "private authenticated config unavailable",
        "cause": "FileNotFoundError",
        "detail": None,
        "site": "salad_live_check.__init__",
        "call": None,
    }
    assert report == {
        "status": "inconclusive",
        "reason": "secure_setup_or_execution_unresolved",
    }
    assert "/nonexistent" not in out


def test_private_loader_rejects_permissions_symlink_and_hardlink(tmp_path):
    tmp_path = tmp_path.resolve()
    path = tmp_path / "reference"
    path.write_bytes(b"isolated-test-value")
    path.chmod(0o600)
    assert _private_bytes(path, 64) == b"isolated-test-value"
    path.chmod(0o644)
    with pytest.raises(ValueError):
        _private_bytes(path, 64)
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        _private_bytes(alias, 64)
    alias.unlink()
    import os

    os.link(path, alias)
    with pytest.raises(ValueError):
        _private_bytes(path, 64)


def test_config_rejects_raw_secret_or_unknown_fields(tmp_path):
    path = tmp_path.resolve() / "config.json"
    path.write_text('{"schema":"tessera.salad.live-config.v1","raw_token":"do-not-echo"}')
    path.chmod(0o600)
    with pytest.raises(ValueError, match="closed server config"):
        _config(path)


def concrete_session(tmp_path, monkeypatch):
    """Real composition/authority store with explicitly synthetic IPC/HTTPS only."""
    import test_salad_server

    from tessera_control.salad_safety_service import SafetyHTTPSClient
    from tessera_control.salad_server import SaladSafetyClient

    tmp_path = tmp_path.resolve()
    original = test_salad_server.profile
    monkeypatch.setattr(test_salad_server, "profile", lambda: original() | {"max_cost_cents": 1})
    h = test_salad_server.fixture(tmp_path, monkeypatch)
    files = {}
    for name, data in (
        ("worker", b"worker-token"),
        ("admin", b"synthetic-admin-token"),
        ("ca", b"synthetic-ca-not-a-real-certificate"),
    ):
        path = tmp_path / name
        path.write_bytes(data)
        path.chmod(0o600)
        files[name] = str(path)
    policy = h.server.policy.model_dump(mode="json")
    policy["management"] = h.server.policy.management.model_dump(mode="json")
    config = dict(
        schema="tessera.salad.live-config.v1",
        state_dir=str(h.journey.state),
        route_id=h.route["id"],
        task_id=h.route["task_id"],
        org_id=h.route["org_id"],
        project_id=h.route["project_id"],
        proposal_digest=h.route["proposal_digest"],
        policy=policy,
        worker_credential_file=files["worker"],
        credential_directory=str(tmp_path / "private-credentials"),
        guardian_socket=str(tmp_path / "guardian.sock"),
        guardian_uid=__import__("os").geteuid(),
        custody=dict(
            host="custody.example",
            port=443,
            token_file=files["admin"],
            ca_file=files["ca"],
            certificate_sha256="a" * 64,
        ),
        approved_max_cost_cents=10000,
        max_seconds=60,
        max_polls=2,
        source_revision="a" * 40 + "+sha256:" + source_digest(),
    )
    path = tmp_path / "live-config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setattr(SafetyHTTPSClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(SaladSafetyClient, "_exchange", lambda self, body: None)
    session = SaladLiveSession(path)
    return session, h, path


def test_concrete_private_composition_reconnect_no_dispatch(tmp_path, monkeypatch):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    other = session.reopen()
    assert other.state(h.route["id"])["effects"] == []
    assert other.server is not session.server
    assert other.dispatcher.store is other.server.store
    other.close()
    session.close()
    h.server.resolver.close()


def test_concrete_unknown_exposure_blocks_actual_dispatch(tmp_path, monkeypatch):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session.custody, "exchange", lambda body: dict(cost="unknown", pending_exposure=True)
    )
    calls = []
    monkeypatch.setattr(session.dispatcher, "dispatch", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="unknown exposure"):
        session.dispatch(h.route["id"], "create")
    assert calls == []
    assert session.state(h.route["id"])["effects"] == []
    session.close()
    h.server.resolver.close()


def test_concrete_cancel_uses_worker_credential_and_is_repeatable(tmp_path, monkeypatch):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    assert not hasattr(session, "cancel_actor")
    session.cancel(h.route["id"])
    state = session.state(h.route["id"])
    assert state["desired"] == "stop"
    provenance = state["stop_provenance"]
    assert provenance["credential_kind"] == "worker"
    assert provenance["cancel_authority"] == "executor_worker"
    assert provenance["decision_class"] == "other_credential"
    session.cancel(h.route["id"])
    assert session.state(h.route["id"])["revision"] == state["revision"]
    session.close()
    h.server.resolver.close()


def test_worker_cancel_is_scoped_to_the_routes_own_worker(tmp_path, monkeypatch):
    from tessera_control.store import Forbidden

    session, h, _ = concrete_session(tmp_path, monkeypatch)
    route = session.state(h.route["id"])
    store = session.server.store
    kwargs = dict(expected_revision=route["revision"], proposal_digest=route["proposal_digest"])
    # A human/owner credential is not a worker credential: refused on the worker path.
    with pytest.raises(Forbidden):
        store.worker_cancel(
            store.authenticate(h.journey.owner.removeprefix("Bearer ")),
            h.route["id"],
            "k1",
            **kwargs,
        )
    # A different worker principal of the same org cannot cancel this route.
    # (a second, real, authenticating worker of the same org and project)
    from test_execution import _worker

    owner_actor = store.authenticate(h.journey.owner.removeprefix("Bearer "))
    _worker(h.journey.state, owner_actor, h.route["project_id"], "other-worker-token")
    other = store.authenticate_worker("other-worker-token")
    assert other["id"] != session.server.worker["id"]
    with pytest.raises(Forbidden, match="own dispatcher worker"):
        store.worker_cancel(other, h.route["id"], "k2", **kwargs)
    assert session.state(h.route["id"])["desired"] != "stop"
    # A stale revision or digest is a conflict, never a blind stop.
    from tessera_control.store import Conflict

    with pytest.raises(Conflict):
        store.worker_cancel(
            session.server.worker, h.route["id"], "k3", **dict(kwargs, expected_revision=999)
        )
    with pytest.raises(Conflict):
        store.worker_cancel(
            session.server.worker, h.route["id"], "k4", **dict(kwargs, proposal_digest="0" * 64)
        )
    session.close()
    h.server.resolver.close()


def test_live_config_refuses_a_cancel_credential_file(tmp_path, monkeypatch):
    session, h, path = concrete_session(tmp_path, monkeypatch)
    session.close()
    h.server.resolver.close()
    document = json.loads(path.read_text())
    document["cancel_credential_file"] = str(path)
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="closed server config"):
        _config(path)


def test_concrete_reopen_rejects_changed_config(tmp_path, monkeypatch):
    session, h, path = concrete_session(tmp_path, monkeypatch)
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="config changed"):
        session.reopen()
    session.close()
    h.server.resolver.close()


@pytest.mark.parametrize("tamper", [None, "bytes", "binding", "manifest"])
def test_concrete_import_verifies_bound_manifest_and_quarantined_bytes(
    tmp_path, monkeypatch, tamper
):
    from tessera_control.salad_safety_service import encoded
    from tessera_control.store import canonical_digest

    session, h, _ = concrete_session(tmp_path, monkeypatch)
    state = session.state(h.route["id"])
    proposal = state["proposal"]
    output = {"synthetic": "local isolated evidence"}
    data = encoded(output)
    binding = dict(
        tenant=state["org_id"],
        project=state["project_id"],
        operation=proposal["operation_id"],
        epoch=proposal["epoch"],
        artifact=proposal["selection"]["salad"]["execution"]["artifact_destination"],
        kind="output",
    )
    if tamper == "binding":
        binding["epoch"] += 1
    inventory = dict(
        binding=binding,
        files=[dict(name="output.json", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))],
        writers_closed=True,
        complete=True,
        quarantine=True,
    )
    inventory["digest"] = canonical_digest(inventory)
    manifest = dict(inventories=[inventory], writers_closed=True, complete=True, quarantine=True)
    manifest["digest"] = canonical_digest(manifest)
    reply = dict(manifest=manifest, output=output)
    if tamper == "bytes":
        reply["output"] = {"synthetic": "changed bytes"}
    if tamper == "manifest":
        manifest["digest"] = "0" * 64
    monkeypatch.setattr(session.custody, "exchange", lambda body: reply)
    if tamper:
        with pytest.raises(ValueError):
            session._import(h.route["id"])
    else:
        assert session._import(h.route["id"])[0] == data
    session.close()
    h.server.resolver.close()


def test_concrete_status_never_seals_active_writers(tmp_path, monkeypatch):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    calls = []

    def exchange(body):
        calls.append(body["operation"])
        return dict(status="armed", orphan=False, allocation_uncertain=False, manifest=None)

    monkeypatch.setattr(session.custody, "exchange", exchange)
    assert session.evidence(h.route["id"])["output_retained"] is False
    assert calls == ["status"]
    session.close()
    h.server.resolver.close()


@pytest.mark.parametrize("case", ["valid-estimate", "unknown-rate", "fenced", "over-cap"])
def test_concrete_estimated_exposure_not_fake_invoice_admission(tmp_path, monkeypatch, case):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    status = dict(
        published_hourly_rate_usd="0.1",
        estimated_exposure_cents="0",
        actual_billed_cost_cents=None,
        exposure_unknown=True,
        exposure_pending=True,
        rate_unknown=False,
        budget_fenced=False,
        escalation_pending=False,
    )
    if case == "unknown-rate":
        status["rate_unknown"] = True
    elif case == "fenced":
        status["budget_fenced"] = True
    elif case == "over-cap":
        status["estimated_exposure_cents"] = "2"
    monkeypatch.setattr(session.custody, "exchange", lambda body: status)
    calls = []
    monkeypatch.setattr(session.dispatcher, "dispatch", lambda *args: calls.append(args))
    if case == "valid-estimate":
        session.dispatch(h.route["id"], "create")
        assert calls == [(h.route["id"], "create")]
    else:
        with pytest.raises(ValueError, match="unknown exposure"):
            session.dispatch(h.route["id"], "create")
        assert calls == []
    session.close()
    h.server.resolver.close()


@pytest.mark.parametrize(
    "case", ["retained", "wrong-binding", "wrong-name", "bad-hash", "over-cap", "not-quarantined"]
)
def test_concrete_pre_stop_retention_metadata_is_exact_and_never_imports(
    tmp_path, monkeypatch, case
):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    state = session.state(h.route["id"])
    metadata = dict(
        binding=session._output_binding(state),
        name="output.json",
        retained=True,
        sha256="a" * 64,
        bytes=32,
        quarantine=True,
    )
    if case == "wrong-binding":
        metadata["binding"]["epoch"] += 1
    elif case == "wrong-name":
        metadata["name"] = "other.json"
    elif case == "bad-hash":
        metadata["sha256"] = "raw untrusted response"
    elif case == "over-cap":
        metadata["bytes"] = 2**30
    elif case == "not-quarantined":
        metadata["quarantine"] = False
    calls = []

    def exchange(body):
        calls.append(body["operation"])
        return dict(
            status="armed",
            orphan=False,
            allocation_uncertain=False,
            manifest=None,
            retained_output=metadata,
        )

    monkeypatch.setattr(session.custody, "exchange", exchange)
    if case == "retained":
        proof = session.evidence(h.route["id"])
        assert proof["output_retained"] is True
        assert proof["stop_confirmed"] is False and proof["writers_quiescent"] is False
        assert proof["output_sha256"] == "a" * 64
        with pytest.raises(ValueError, match="confirmed stop"):
            session.retained_bytes(h.route["id"])
    else:
        with pytest.raises(ValueError, match="metadata"):
            session.evidence(h.route["id"])
    assert set(calls) == {"status"}
    session.close()
    h.server.resolver.close()


def test_launched_run_keeps_lease_and_guardian_alive_each_poll():
    session = LocalSession()
    session.proof["output_retained"] = False
    receipt = drive(session, max_polls=3)
    # Never retained output: the settle read and every post-launch observation
    # first renew liveness (1 before start + 3 polls).
    assert receipt["status"] == "inconclusive"
    assert session.actions[:2] == ["create", "observe"]
    assert session.keepalives == 4


def test_reconnect_without_launch_never_sends_keepalive():
    session = LocalSession(prior=True)
    drive(session)
    assert session.keepalives == 0


def test_concrete_keepalive_renews_exact_lease_and_requires_create(tmp_path, monkeypatch):
    import sqlite3

    session, h, _ = concrete_session(tmp_path, monkeypatch)
    route = h.route["id"]
    lease = session.state(route)["proposal"]["lease_id"]

    def expiry():
        with sqlite3.connect(session.server.store.database_path) as db:
            return db.execute(
                "SELECT expires_at, renewed_at FROM execution_leases WHERE id=?", (lease,)
            ).fetchone()

    def last_seen():
        with sqlite3.connect(session.server.store.database_path) as db:
            return db.execute(
                "SELECT last_seen_at FROM worker_registry WHERE worker_principal_id=?",
                (session.server.worker["id"],),
            ).fetchone()

    before, seen_before = expiry(), last_seen()
    # Renewal is real; a guardian heartbeat without an existing create intent is refused.
    with pytest.raises(Exception, match="create intent"):
        session.keepalive(route)
    after = expiry()
    assert after[1] is not None and after != before
    assert last_seen() is not None and last_seen() != seen_before
    assert session.state(route)["effects"] == []
    session.close()
    h.server.resolver.close()


def test_create_waits_for_group_to_settle_before_start():
    session = LocalSession()
    session.settling = 2
    receipt = drive(session, max_polls=5)
    assert session.actions[:5] == ["create", "observe", "observe", "observe", "start"]
    assert receipt["status"] == "passed"


def test_group_that_never_settles_is_stopped_not_started():
    session = LocalSession()
    session.settling = 99
    receipt = drive(session, max_polls=3)
    assert "start" not in session.actions and "stop" in session.actions
    assert receipt["status"] == "inconclusive"


def test_heartbeat_refused_after_retained_output_is_completion_not_failure():
    session = LocalSession()
    session.refuse_keepalive = True  # guardian already stopped on retained output
    receipt = drive(session)
    assert receipt["status"] == "passed" and receipt["stop_confirmed"]


def test_heartbeat_refused_without_output_still_fails():
    session = LocalSession()
    session.refuse_keepalive = True
    session.proof["output_retained"] = False
    receipt = drive(session)
    assert receipt["status"] == "inconclusive"
    assert "stop" in session.actions


# --- ADR 0036 phase 3: declared multi-file outputs ------------------------------------------


class MultiSession(LocalSession):
    def __init__(self):
        super().__init__()
        self.files = [
            dict(name="output.json", sha256="1" * 64, bytes=10),
            dict(name="weights.bin", sha256="2" * 64, bytes=2000),
        ]
        self.proof = dict(self.proof, output_sha256=None, outputs=self.files)
        self.reconnect_files = self.files

    def retained_bytes(self, route):
        raise AssertionError("multi-file custody is never read as one blob")

    def retained_files(self, route):
        return [dict(f) for f in self.files]

    def reopen(self):
        other = MultiSession()
        other.record = self.record
        other.files = self.reconnect_files
        return other


def test_multi_output_receipt_carries_every_verified_per_file_digest():
    session = MultiSession()
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["outputs"] == session.files
    assert receipt["output_bytes"] == 2010
    assert "output_sha256" not in receipt


@pytest.mark.parametrize("tamper", ["import", "reconnect", "cap"])
def test_multi_output_digest_or_reconnect_mismatch_never_passes(tamper):
    session = MultiSession()
    changed = [dict(session.files[0]), dict(session.files[1], sha256="3" * 64)]
    if tamper == "import":
        session.retained_files = lambda route: changed
    elif tamper == "reconnect":
        session.reconnect_files = changed
    else:
        session.proof["output_byte_cap"] = 2009
    receipt = drive(session)
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == "custody_stop_or_reconnect_unresolved"


@pytest.mark.parametrize("tamper", [None, "chunk", "progress", "missing"])
def test_concrete_multi_output_evidence_and_chunked_import(tmp_path, monkeypatch, tamper):
    import base64

    import tessera_control.salad_output_import as output_import
    from tessera_control.store import canonical_digest

    session, h, _ = concrete_session(tmp_path, monkeypatch)
    state = session.state(h.route["id"])
    declared = {"output.json": 64, "weights.bin": 600}  # the fixture profile caps 1024 total
    monkeypatch.setattr(session, "_multi_output", lambda state: declared)
    monkeypatch.setattr(output_import, "declared_outputs", lambda execution: declared)
    data = {"output.json": b'{"result":1}', "weights.bin": bytes(range(256)) * 2}
    files = [
        dict(name=n, sha256=hashlib.sha256(d).hexdigest(), bytes=len(d))
        for n, d in sorted(data.items())
    ]
    binding = session._output_binding(state)
    sealed = dict(writers_closed=True, complete=True, quarantine=True)
    inventory = dict(binding=binding, files=files, **sealed)
    inventory["digest"] = canonical_digest(inventory)
    manifest = dict(inventories=[inventory], **sealed)
    manifest["digest"] = canonical_digest(manifest)
    progress = dict(
        binding=binding, declared=sorted(declared), files=files, complete=True, quarantine=True
    )
    if tamper == "progress":
        progress["files"] = [dict(files[0], bytes=65)]
    if tamper == "missing":
        progress["files"] = files[:1]
    calls = []

    def exchange(body, cap=131072):
        calls.append(body["operation"])
        if body["operation"] == "status":
            return dict(
                status="stopped",
                reason="exact_terminal_and_sealed",
                orphan=False,
                allocation_uncertain=False,
                manifest=manifest,
                retained_output=None,
                retained_outputs=progress,
            )
        if body["operation"] == "import":
            return dict(manifest=manifest, chunk_bytes=1 << 20)
        assert body["operation"] == "import_chunk" and body["seq"] == 0 and cap > 1 << 20
        chunk = data[body["name"]]
        if tamper == "chunk" and body["name"] == "weights.bin":
            chunk = chunk[:-1] + b"x"
        return dict(
            name=body["name"],
            seq=0,
            sha256=hashlib.sha256(chunk).hexdigest(),
            content_base64=base64.b64encode(chunk).decode(),
        )

    monkeypatch.setattr(session.custody, "exchange", exchange)
    record = session.server.store.get(session.server.worker, h.route["id"])
    monkeypatch.setattr(
        session,
        "state",
        lambda route: dict(record, provider_status="stopped", termination_confirmed=True),
    )
    try:
        if tamper is None:
            proof = session.evidence(h.route["id"])
            assert proof["output_retained"] and proof["writers_quiescent"]
            assert proof["outputs"] == files and proof["stop_confirmed"]
            assert session.retained_files(h.route["id"]) == files
            # evidence imports once; retained_files re-runs evidence and imports again.
            assert calls.count("import_chunk") == 6
        elif tamper == "missing":
            with pytest.raises(ValueError, match="completeness"):
                session.evidence(h.route["id"])
        else:
            with pytest.raises(ValueError):
                session.evidence(h.route["id"])
    finally:
        session.close()
        h.server.resolver.close()


def test_rejected_create_is_reported_never_started_and_still_stopped():
    rejection = dict(
        status=400,
        title="One or more validation errors occurred.",
        detail="storage_amount must be a whole number of GiB",
        error_fields=["container.resources.storage_amount"],
    )

    class Rejecting(LocalSession):
        def dispatch(self, route, action):
            state = super().dispatch(route, action)
            if action == "create":
                state["receipts"] = [
                    dict(proxy_reason="provider_rejected_request", provider_rejection=rejection)
                ]
            return state

    session = Rejecting()
    receipt = drive(session)
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == "provider_rejected_request"
    assert receipt["create_rejection"] == rejection
    assert "start" not in session.actions and session.actions.count("create") == 1
    assert session.cancelled and session.actions.count("stop") == 1
    # A normal journey carries no rejection.
    assert drive(LocalSession())["create_rejection"] is None


def test_prestart_observes_back_off_but_heartbeats_keep_cadence_then_normal_when_running():
    class PullSession(LocalSession):
        def __init__(self):
            super().__init__()
            self.now = 0.0
            self.observes = []
            self.beats = []
            self.pull_until = 3

        def dispatch(self, route, action):
            if action == "start":
                self.record["provider_status"] = "unknown"
            if action == "observe" and "start" in self.actions:
                if len(self.observes) >= self.pull_until:
                    self.record["provider_status"] = "running"
                self.observes.append((self.now, self.record["provider_status"]))
            if action == "observe" and "start" in self.actions and len(self.observes) > 6:
                self.proof["output_retained"] = True
            return super().dispatch(route, action)

        def keepalive(self, route):
            self.beats.append(self.now)

    session = PullSession()
    session.proof["output_retained"] = False

    def sleep(seconds):
        session.now += seconds

    _drive(
        session,
        route_id="exact-route",
        source_revision="a" * 40,
        max_seconds=3500,
        max_polls=350,
        sleep=sleep,
        clock=lambda: session.now,
    )
    times = [t for t, _ in session.observes]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    states = [s for _, s in session.observes]
    assert gaps[:3] == [30, 30, 30]  # pre-start: backed off
    assert states[3] == "running" and gaps[3:6] == [10, 10, 10]  # running: normal
    start = times[0]
    beats = [b for b in session.beats if b >= start]
    assert max(b - a for a, b in zip(beats, beats[1:], strict=False)) <= 10
    # 45 minutes of pre-start at 30 s is 90 observes: well under the derived budget.
    from tessera_control.remote_execution import effect_budget

    assert 2700 // 30 + 4 < effect_budget(3500)


# --- Guardian refusal classification in the keepalive ---------------------------------------


class RefusingSession(LocalSession):
    """Keepalive refuses with the scripted guardian errors once the group is started."""

    def __init__(self, refusals, *, retain_after=None, beat_seconds=0):
        super().__init__()
        self.refusals = list(refusals)
        self.now = 0.0
        self.beat_seconds = beat_seconds
        self.retain_after = retain_after
        self.proof["output_retained"] = False

    def dispatch(self, route, action):
        if action == "observe" and "start" in self.actions and self.retain_after is not None:
            self.retain_after -= 1
            if self.retain_after < 0:
                self.proof["output_retained"] = True
        return super().dispatch(route, action)

    def cancel(self, route):
        self.cancelled_at = self.now
        super().cancel(route)

    def keepalive(self, route):
        self.keepalives += 1
        self.now += self.beat_seconds
        if "start" in self.actions and self.refusals:
            error = self.refusals.pop(0)
            if error is not None:
                raise error


def drive_clocked(session, *, max_polls=10, max_seconds=100):
    def sleep(seconds):
        session.now += seconds

    return _drive(
        session,
        route_id="exact-route",
        source_revision="a" * 40,
        max_seconds=max_seconds,
        max_polls=max_polls,
        sleep=sleep,
        clock=lambda: session.now,
    )


def test_transient_guardian_refusal_is_retried_at_next_interval():
    from tessera_control.salad_server import GuardianRefused

    session = RefusingSession(
        [
            GuardianRefused("guardian_loop_unready"),
            None,
            GuardianRefused("workload_verifier_unready"),
            None,
        ],
        retain_after=5,
    )
    receipt = drive_clocked(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["guardian_transient_refusals"] == 2
    assert receipt["guardian_refusal"] is None
    assert session.actions.count("observe") >= 6 and not session.refusals


@pytest.mark.parametrize(
    "code",
    [
        "stop_only",
        "exposure_fenced",
        "rate_unknown",
        "budget_binding_expired",
        "stop_credential_window_insufficient",
        "custody_unready",
        "guardian_unready",
        "budget_insufficient",
        "internal",
        "something_new",
    ],
)
def test_terminal_guardian_refusal_cancels_at_once(code):
    from tessera_control.salad_server import GuardianRefused

    session = RefusingSession([GuardianRefused(code)], retain_after=5)
    receipt = drive_clocked(session)
    assert receipt["status"] == "inconclusive"
    assert receipt["guardian_refusal"] == {"code": code, "cause": "terminal_code"}
    assert receipt["guardian_transient_refusals"] == 0
    assert session.cancelled and "stop" in session.actions
    # Cancelled on the first refused beat: nothing is observed before the stop path.
    assert session.actions[session.actions.index("start") + 1] == "stop"


def test_transient_refusals_cancel_before_guardian_silence():
    from tessera_control.salad_live_check import (
        GUARDIAN_SILENCE_MARGIN_SECONDS,
        GUARDIAN_SILENCE_SECONDS,
    )
    from tessera_control.salad_server import GuardianRefused

    session = RefusingSession([GuardianRefused("guardian_loop_unready")] * 10, retain_after=50)
    receipt = drive_clocked(session, max_polls=20, max_seconds=200)  # 10 s interval
    assert receipt["status"] == "inconclusive"
    assert receipt["guardian_refusal"] == {
        "code": "guardian_loop_unready",
        "cause": "silence_bound",
    }
    assert session.cancelled and "stop" in session.actions
    # Last good beat at t=0, 10 s interval: refusals at t=0 and t=10 are skipped (the
    # next beat still lands by t=20 <= 30 - 5); at t=20 the next beat would be at 30 > 25,
    # so the run is cancelled while the guardian has not yet gone silent.
    assert receipt["guardian_transient_refusals"] == 2
    assert session.cancelled_at < GUARDIAN_SILENCE_SECONDS
    assert GUARDIAN_SILENCE_SECONDS - GUARDIAN_SILENCE_MARGIN_SECONDS == 25


def test_slow_transient_refusal_is_not_tolerated_near_silence():
    from tessera_control.salad_server import GuardianRefused

    # Each beat takes 6 s: the refused beat ends 22 s after the last good one started,
    # and the next would land at 32 > 30 - 5, so it is cancelled now.
    session = RefusingSession(
        [None, GuardianRefused("guardian_loop_unready")], retain_after=50, beat_seconds=6
    )
    receipt = drive_clocked(session, max_polls=20, max_seconds=200)
    assert receipt["guardian_refusal"] == {
        "code": "guardian_loop_unready",
        "cause": "silence_bound",
    }
    assert receipt["guardian_transient_refusals"] == 0


def test_unanswered_guardian_is_terminal():
    from tessera_control.salad_server import GuardianUnavailable

    # No answer may mean a dead guardian, which enforces nothing on silence.
    session = RefusingSession([GuardianUnavailable()], retain_after=5)
    receipt = drive_clocked(session)
    assert receipt["guardian_refusal"] == {"code": "unavailable", "cause": "terminal_code"}
    assert receipt["guardian_transient_refusals"] == 0
    assert session.cancelled and session.actions[session.actions.index("start") + 1] == "stop"


@pytest.mark.parametrize("error", ["forbidden", "conflict"])
def test_revoked_authority_cancels_at_once(error):
    from tessera_control.store import Conflict, Forbidden

    session = RefusingSession(
        [Forbidden("revoked") if error == "forbidden" else Conflict("revoked")], retain_after=5
    )
    receipt = drive_clocked(session)
    assert receipt["guardian_refusal"] is None and receipt["guardian_transient_refusals"] == 0
    assert session.cancelled and session.actions[session.actions.index("start") + 1] == "stop"


def test_guardian_silence_matches_assignment_default():
    from tessera_control.salad_guardian import GuardianAssignment
    from tessera_control.salad_live_check import GUARDIAN_SILENCE_SECONDS

    assert GuardianAssignment.__dataclass_fields__["heartbeat_seconds"].default == (
        GUARDIAN_SILENCE_SECONDS
    )


@pytest.mark.parametrize("provider_stopped", [True, False])
def test_provider_confirmed_stop_ends_the_stop_wait_without_claiming_custody(provider_stopped):
    """A stop_only refusal: the provider stopped the group at once, but custody can never
    seal (allocation changed). The driver must not wait out the whole window."""
    from tessera_control.salad_server import GuardianRefused

    session = RefusingSession([GuardianRefused("stop_only")])
    session.proof.update(stop_confirmed=False, writers_quiescent=False)
    session.proof["provider_stop_confirmed"] = provider_stopped
    receipt = drive_clocked(session, max_polls=50, max_seconds=1000)
    assert receipt["status"] == "inconclusive"
    assert receipt["stop_confirmed"] is False
    assert receipt["guardian_refusal"] == {"cause": "terminal_code", "code": "stop_only"}
    stop_at = session.actions.index("stop")
    if provider_stopped:
        assert receipt["provider_stop_confirmed"] is True
        assert receipt["reason"] == "provider_stopped_custody_unsealed"
        assert session.actions[stop_at + 1 :] == ["observe"]
        assert session.now < 100
    else:
        assert receipt["provider_stop_confirmed"] is False
        assert receipt["reason"] != "provider_stopped_custody_unsealed"
        assert session.actions[stop_at + 1 :].count("observe") > 5


@pytest.mark.parametrize(
    "status,reason,orphan,provider_status,expected",
    [
        ("reconciled", "provider_confirmed_exact_group_stopped", False, "stopped", True),
        # An operator attestation is not a provider read seen by this driver.
        ("reconciled", "operator_reconciled_exact_group_stopped", False, "stopped", False),
        ("reconciled", "provider_confirmed_exact_group_stopped", True, "stopped", False),
        ("reconciled", "provider_confirmed_exact_group_stopped", False, "unknown", False),
        ("unknown", "provider_unknown", True, "stopped", False),
    ],
)
def test_concrete_provider_stop_evidence_is_exact_and_never_stop_confirmed(
    tmp_path, monkeypatch, status, reason, orphan, provider_status, expected
):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    real = session.state(h.route["id"])
    monkeypatch.setattr(session, "state", lambda route: dict(real, provider_status=provider_status))
    monkeypatch.setattr(
        session.custody,
        "exchange",
        lambda body: dict(
            status=status,
            reason=reason,
            orphan=orphan,
            allocation_uncertain=True,
            manifest=None,
            retained_output=None,
        ),
    )
    proof = session.evidence(h.route["id"])
    assert proof["provider_stop_confirmed"] is expected
    assert proof["stop_confirmed"] is False and proof["writers_quiescent"] is False
    session.close()
    h.server.resolver.close()


def test_create_refreshes_worker_liveness_before_use_time_authority(tmp_path, monkeypatch):
    """A long-lived executor's last sighting can be older than the 60 s disconnect bound
    when the route launches; create must heartbeat the registry before preflight."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    session, h, _ = concrete_session(tmp_path, monkeypatch)
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(session.server.store.database_path) as db:
        changed = db.execute(
            "UPDATE worker_registry SET last_seen_at=?, heartbeat_at=? WHERE worker_principal_id=?",
            (stale, stale, session.server.worker["id"]),
        ).rowcount
    assert changed == 1
    status = dict(
        published_hourly_rate_usd="0.1",
        estimated_exposure_cents="0",
        actual_billed_cost_cents=None,
        exposure_unknown=True,
        exposure_pending=True,
        rate_unknown=False,
        budget_fenced=False,
        escalation_pending=False,
    )
    monkeypatch.setattr(session.custody, "exchange", lambda body: status)
    calls = []
    monkeypatch.setattr(session.dispatcher, "dispatch", lambda *args: calls.append(args))
    session.dispatch(h.route["id"], "create")
    assert calls == [(h.route["id"], "create")]
    with sqlite3.connect(session.server.store.database_path) as db:
        seen = db.execute(
            "SELECT last_seen_at FROM worker_registry WHERE worker_principal_id=?",
            (session.server.worker["id"],),
        ).fetchone()[0]
    assert seen > stale
    session.close()
    h.server.resolver.close()


def test_receipt_records_guardian_rate_read_retries():
    session = LocalSession()
    session.proof["rate_read_retries"] = 2
    receipt = drive(session)
    assert receipt["status"] == "passed"
    assert receipt["guardian_rate_read_retries"] == 2


@pytest.mark.parametrize("value,expected", [(3, 3), (0, 0), (-1, None), ("2", None), (True, None)])
def test_rate_read_retry_count_is_projected_strictly(value, expected):
    from tessera_control.salad_live_check import _count

    assert _count(value) == expected


class FlakyObserveSession(LocalSession):
    """Observe's fresh provider read times out for the next ``failures`` calls after start."""

    def __init__(self, failures, error=None):
        super().__init__()
        self.failures, self.error = failures, error

    def dispatch(self, route, action):
        if action == "observe" and "start" in self.actions and self.failures:
            self.failures -= 1
            self.actions.append(action)
            from tessera_control.salad_server import ProviderReadUnavailable

            raise self.error or ProviderReadUnavailable("Fresh provider preflight unavailable.")
        return super().dispatch(route, action)


@pytest.mark.parametrize("failures", [1, 2])
def test_a_transient_observe_read_failure_is_skipped_not_fatal(failures):
    session = FlakyObserveSession(failures)
    receipt = drive(session, max_polls=10)
    assert receipt["status"] == "passed", receipt
    assert receipt["observe_transient_failures"] == failures
    assert session.actions.count("stop") == 1


def test_consecutive_observe_read_failures_past_the_bound_still_fail():
    session = FlakyObserveSession(3)
    receipt = drive(session, max_polls=10)
    assert receipt["status"] == "inconclusive"
    assert receipt["observe_transient_failures"] == 2
    assert session.cancelled and session.actions.count("stop") == 1


def test_other_observe_errors_stay_fatal():
    session = FlakyObserveSession(1, error=RuntimeError("SECRET must not appear"))
    receipt = drive(session, max_polls=10)
    assert receipt["status"] == "inconclusive"
    assert receipt["observe_transient_failures"] == 0
    assert "SECRET" not in json.dumps(receipt)


def test_receipt_records_guardian_stop_cause_and_provider_read_retries():
    session = LocalSession()
    session.proof.update(stop_cause="provider_unavailable", provider_read_retries=2)
    receipt = drive(session)
    assert receipt["guardian_stop_cause"] == "provider_unavailable"
    assert receipt["guardian_provider_read_retries"] == 2


@pytest.mark.parametrize(
    "value,expected",
    [
        ("missed_poll", "missed_poll"),
        (None, None),
        ("Bad Cause", "invalid_code"),
        ("x" * 80, "invalid_code"),
        (3, "invalid_code"),
    ],
)
def test_stop_cause_is_projected_strictly(value, expected):
    from tessera_control.salad_live_check import _cause

    assert _cause(value) == expected


def test_receipt_records_the_last_guardian_workload_refusal():
    session = LocalSession()
    session.proof["last_workload_refusal"] = "workload_rejected"
    receipt = drive(session)
    assert receipt["guardian_last_workload_refusal"] == "workload_rejected"


def test_receipt_records_the_last_rate_and_group_read_failure_reasons():
    session = LocalSession()
    session.proof.update(last_rate_read_failure="timeout", last_provider_read_failure="rate_stale")
    receipt = drive(session)
    assert receipt["guardian_last_rate_read_failure"] == "timeout"
    assert receipt["guardian_last_provider_read_failure"] == "rate_stale"


def test_a_create_that_was_never_sent_is_reported_as_such_and_still_stopped():
    not_sent = dict(status=0, title="The create request was not sent", detail=None,
                    error_fields=[])  # fmt: skip

    class NotSent(LocalSession):
        def dispatch(self, route, action):
            state = super().dispatch(route, action)
            if action == "create":
                state["receipts"] = [
                    {"proxy_reason": "request_not_sent", "provider_rejection": not_sent}
                ]
            return state

    session = NotSent()
    receipt = drive(session)
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == "create_not_sent"
    assert receipt["create_rejection"] == not_sent
    assert "start" not in session.actions
    assert session.actions[: session.actions.index("stop")] == ["create"]
    assert session.cancelled and session.actions.count("stop") == 1


def test_a_receipt_cannot_mix_a_status_0_rejection_with_another_reason():
    from pydantic import ValidationError

    from tessera_control.remote_execution_models import ProviderReadback

    base = dict(state="unknown", observed_at="2026-11-20T00:00:00Z")
    zero = dict(status=0, title=None, detail=None, error_fields=[])
    ProviderReadback(proxy_reason="request_not_sent", provider_rejection=zero, **base)
    with pytest.raises(ValidationError):
        ProviderReadback(proxy_reason="provider_rejected_request", provider_rejection=zero, **base)
    with pytest.raises(ValidationError):
        ProviderReadback(proxy_reason="request_not_sent",
                         provider_rejection=dict(zero, status=400), **base)  # fmt: skip


def test_receipt_records_cancel_authority_and_outcome():
    session = LocalSession()
    receipt = drive(session)
    assert receipt["cancel_authority"] == "executor_worker"
    assert receipt["cancel_recorded"] is True


def test_receipt_records_a_failed_cancel():
    class Refusing(LocalSession):
        def cancel(self, route):
            raise ValueError("refused")

    receipt = drive(Refusing())
    assert receipt["cancel_authority"] == "executor_worker"
    assert receipt["cancel_recorded"] is False


# --- the group must read settled, not only stopped, before start (ADR 0071) ----------------


class UnsettledSession(LocalSession):
    """Reads stopped but not terminal (a change in flight) for the first observations."""

    def __init__(self, unsettled: int):
        super().__init__()
        self.unsettled = unsettled
        self.now = 0.0

    def dispatch(self, route, action):
        state = super().dispatch(route, action)
        if action == "observe":
            state["termination_confirmed"] = self.unsettled <= 0
            self.unsettled -= 1
        return state


def test_start_waits_until_the_group_reads_settled_not_just_stopped():
    session = UnsettledSession(unsettled=3)
    receipt = drive(session, max_polls=8)
    first = session.actions.index("start")
    assert session.actions[:first].count("observe") == 4  # three unsettled reads, one settled
    assert receipt["prestart_settle"] == {
        "polls": 4,
        "provider_status": "stopped",
        "settled": True,
        "waited_seconds": 0.0,
    }
    assert receipt["status"] == "passed"


def test_the_settle_block_is_a_closed_set_of_plain_values():
    receipt = drive_clocked(UnsettledSession(unsettled=10_000), max_polls=40, max_seconds=400)
    block = receipt["prestart_settle"]
    assert set(block) == {"polls", "provider_status", "settled", "waited_seconds"}
    assert type(block["polls"]) is int and type(block["settled"]) is bool
    assert type(block["waited_seconds"]) is float and block["waited_seconds"] >= 0
    assert block["provider_status"] in {"stopped", "running", "pending", "unknown"}


def test_a_group_that_stays_stopped_but_unsettled_is_handed_to_start_after_the_bound():
    session = UnsettledSession(unsettled=10_000)
    receipt = drive_clocked(session, max_polls=40, max_seconds=400)
    assert "start" in session.actions  # the transport's own exact read decides
    assert receipt["prestart_settle"]["settled"] is False
    assert receipt["prestart_settle"]["provider_status"] == "stopped"
    assert session.now >= 45  # it waited the bounded settle window first
    assert receipt["prestart_settle"]["waited_seconds"] >= 45  # and the receipt says what it cost
