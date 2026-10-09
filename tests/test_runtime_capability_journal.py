"""Crash, concurrency and reconciliation failures using temporary offline state."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_runtime_capability_broker import make_broker as broker_factory_fixture
from test_runtime_capability_broker import run

from tessera_control.store import Conflict, Store, canonical_digest
from tessera_worker.capability_broker import CapabilityBroker
from tessera_worker.capability_effects import FakeCapabilityEffects
from tessera_worker.capability_journal import CapabilityJournal, JournalConflict

make_broker = broker_factory_fixture


def test_exclusive_journal_owner_and_cross_journal_claim(make_broker):
    h = make_broker()
    with pytest.raises(JournalConflict, match="supervised"):
        CapabilityJournal(h.journal.root)
    with pytest.raises(JournalConflict, match="attached"):
        CapabilityBroker(h.journal, authority=h.authority, identity=h.identity)
    run(h)
    other = CapabilityJournal(h.root / "other-journal")
    try:
        effects = FakeCapabilityEffects(h.content)
        broker = CapabilityBroker(
            other, authority=h.authority, identity=h.identity, effects=effects
        )
        assert broker.run(h.package)["state"] != "completed"
        assert not effects.calls
    finally:
        other.close()


def test_in_process_concurrency_cannot_double_effect(make_broker):
    h = make_broker()
    entered, release = Event(), Event()

    def wait(phase):
        if phase == "open":
            entered.set()
            assert release.wait(3)

    h.effects.before = wait
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(run, h)
        try:
            assert entered.wait(3)
            with pytest.raises(JournalConflict, match="concurrency"):
                run(h)
        finally:
            release.set()
        assert future.result()["state"] == "completed"
    assert h.effects.calls.count("open") == 1


def test_crash_recovery_unknown_never_reissues(make_broker):
    h = make_broker()

    def crash(phase):
        if phase == "read":
            raise KeyboardInterrupt()

    h.effects.before = crash
    with pytest.raises(KeyboardInterrupt):
        run(h)
    h.journal.close()
    reopened = CapabilityJournal(h.journal.root)
    try:
        effects = FakeCapabilityEffects(h.content)
        broker = CapabilityBroker(
            reopened, authority=h.authority, identity=h.identity, effects=effects
        )
        bundle, _ = reopened.bundle(h.operation["id"])
        assert reopened.retained_package(h.operation["id"]) == h.package
        assert bundle["state"] == "unknown"
        assert bundle["receipts"][-1]["reason"] == "crash"
        with pytest.raises(JournalConflict, match="replay"):
            broker.run(h.package)
        assert not effects.calls
    finally:
        reopened.close()


def test_receipt_failure_rolls_back_chunk_and_prevents_next_effect(make_broker, monkeypatch):
    h = make_broker()
    original = h.journal._append

    def fail(db, operation_id, receipt):
        original(db, operation_id, receipt)
        if receipt["state"] == "progress":
            raise sqlite3.OperationalError("injected write failure")

    monkeypatch.setattr(h.journal, "_append", fail)
    with pytest.raises(sqlite3.OperationalError):
        run(h)
    bundle, content = h.journal.bundle(h.operation["id"])
    assert bundle["state"] == "started" and content == b""
    assert h.effects.calls.count("read") == 1


def test_sink_failure_retains_bytes_and_idempotent_evidence_retry(make_broker, monkeypatch):
    h = make_broker()
    bundle = run(h)

    def fail(*args):
        raise sqlite3.OperationalError("audit unavailable")

    def sink(package, bundle, content):
        return h.store.reconcile(h.worker, package, bundle, content)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail))
        with pytest.raises(sqlite3.OperationalError):
            h.journal.reconcile(h.package, sink)
    assert h.journal.bundle(h.operation["id"])[0] == bundle
    with h.journal.connect() as db:
        assert db.execute("SELECT acknowledged FROM operations").fetchone()[0] is None
    # Object publication may precede failed authority commit. No-overwrite retry
    # reconciles that same object instead of repeating the download.
    calls = list(h.effects.calls)
    assert h.journal.reconcile(h.package, sink)["custody"] == "quarantined_unreleased"
    assert h.effects.calls == calls


def test_lost_sink_reply_retry_does_not_duplicate_audit(make_broker):
    h = make_broker()
    run(h)

    def lose(package, bundle, content):
        h.store.reconcile(h.worker, package, bundle, content)
        raise TimeoutError("lost reply")

    with pytest.raises(TimeoutError):
        h.journal.reconcile(h.package, lose)
    h.journal.reconcile(h.package, lambda p, b, c: h.store.reconcile(h.worker, p, b, c))
    with sqlite3.connect(h.store.database_path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type='capability.broker_journal_reconciled'"
            ).fetchone()[0]
            == 1
        )


def test_reconciliation_rejects_modified_chain_or_content(make_broker):
    h = make_broker()
    bundle = run(h)
    bad = json.loads(json.dumps(bundle))
    bad["receipts"][1]["state"] = "completed"
    with pytest.raises(ValueError):
        h.store.reconcile(h.worker, h.package, bad, h.content)
    with pytest.raises(Conflict):
        h.store.reconcile(h.worker, h.package, bundle, b"changed")


def test_journal_path_symlink_and_nonprivate_root_denied(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(JournalConflict):
        CapabilityJournal(linked)
    target.chmod(0o755)
    with pytest.raises(JournalConflict):
        CapabilityJournal(target)


def test_control_claim_fences_other_action_and_journal_until_reconciled(make_broker):
    def two_actions(d):
        d["actions"].append(d["actions"][0] | {"id": "second-action"})
        d["max_bytes"] *= 2
        d["max_seconds"] *= 2

    h = make_broker(change=two_actions)
    first = run(h)
    binding = h.package.decode().binding.model_dump(mode="json")
    binding["action"] = h.declared["actions"][1]
    _, op = h.store.admit(h.worker, h.policy["id"], "second", binding)
    second = h.store.issue(h.worker, op["id"])
    with pytest.raises(Conflict, match="unreconciled"):
        h.store.claim(h.worker, second, "a" * 64)
    assert first["state"] == "completed"
    h.journal.reconcile(h.package, lambda p, b, c: h.store.reconcile(h.worker, p, b, c))
    assert h.store.claim(h.worker, second, "a" * 64)["operation_id"] == op["id"]


def test_failed_bundle_revokes_remaining_policy_but_never_claims_termination(make_broker):
    h = make_broker()
    h.effects.content = b"bad"
    result = run(h)
    assert result["state"] == "failed"
    h.journal.reconcile(h.package, lambda p, b, c: h.store.reconcile(h.worker, p, b, c))
    assert h.store.get(h.owner, h.policy["id"])["status"] == "revoked"
    with sqlite3.connect(h.store.database_path) as db:
        payload = json.loads(
            db.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='capability.automatically_revoked' ORDER BY cursor DESC"
            ).fetchone()[0]
        )
        assert payload["termination_confirmed"] is False


def test_control_does_not_trust_self_consistent_mismatched_completed_receipt(make_broker):
    h = make_broker()
    bundle = run(h)
    bundle["receipts"][-1]["artifact_name"] = "unexpected"
    # Recomputing the chain cannot make an unexpected effect approved.
    previous = "0" * 64
    for receipt in bundle["receipts"]:
        receipt["previous_digest"] = previous
        previous = canonical_digest(receipt)
    result = h.store.reconcile(h.worker, h.package, bundle, h.content)
    assert result["assessment"] == "quarantined"
    assert "receipt_identity_or_limit_mismatch" in result["reasons"]
    assert h.store.get(h.owner, h.policy["id"])["status"] == "revoked"


def test_no_overwrite_of_conflicting_project_object(make_broker):
    from tessera_control.project_artifacts import (
        FilesystemProjectArtifactBackend,
        ProjectArtifactStorageError,
    )

    h = make_broker()
    run(h)
    backend = FilesystemProjectArtifactBackend(h.state / "project-artifacts")
    artifact_id = "broker-evidence-" + h.package.digest
    content = b"unrelated previously committed bytes"
    stage = backend.stage(
        h.worker["org_id"],
        h.grant["project_id"],
        artifact_id,
        hashlib.sha256(content).hexdigest(),
        content,
    )
    placement = backend.commit(stage)
    with pytest.raises(ProjectArtifactStorageError):
        h.journal.reconcile(h.package, lambda p, b, c: h.store.reconcile(h.worker, p, b, c))
    assert (
        backend.read(h.worker["org_id"], h.grant["project_id"], artifact_id, placement) == content
    )


def test_revoked_worker_cannot_use_historical_sink(make_broker):
    from tessera_control.store import Forbidden

    h = make_broker()
    run(h)
    with sqlite3.connect(h.store.database_path) as db:
        db.execute(
            "UPDATE credentials SET revoked_at='2026-01-01' WHERE principal_id=?", (h.worker["id"],)
        )
    with pytest.raises(Forbidden):
        h.journal.reconcile(h.package, lambda p, b, c: h.store.reconcile(h.worker, p, b, c))
    assert h.journal.retained_package(h.operation["id"]) == h.package
