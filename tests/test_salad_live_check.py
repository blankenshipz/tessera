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

    def state(self, route):
        return json.loads(json.dumps(self.record))

    def dispatch(self, route, action):
        self.actions.append(action)
        if self.fail == action:
            raise RuntimeError("SECRET must not appear")
        self.record["effects"].append({"action": action})
        return self.state(route)

    def cancel(self, route):
        self.cancelled = True

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
    assert json.loads(capsys.readouterr().out) == {
        "status": "inconclusive",
        "reason": "secure_setup_or_execution_unresolved",
    }


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
        ("cancel", h.journey.owner.removeprefix("Bearer ").encode()),
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
        cancel_credential_file=files["cancel"],
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


def test_concrete_cancel_preserves_human_authority_and_is_repeatable(tmp_path, monkeypatch):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    session.cancel(h.route["id"])
    state = session.state(h.route["id"])
    assert state["desired"] == "stop"
    session.cancel(h.route["id"])
    assert session.state(h.route["id"])["revision"] == state["revision"]
    session.close()
    h.server.resolver.close()


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
