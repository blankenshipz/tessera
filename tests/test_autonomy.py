"""Synthetic-only autonomy acceptance; all state and effects are disposable."""

import copy
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_credentials import headers, issue, owner_headers, token
from test_runtime_capability_broker import make_broker as broker_fixture

from tessera_control.api import create_app
from tessera_control.autonomy import AutonomyStore
from tessera_control.autonomy_models import AutonomyPlan
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import Conflict, Forbidden, NotFound, Store, StoreError
from tessera_worker.capability_broker import EffectDenied


@pytest.fixture
def make_broker(tmp_path):
    yield from broker_fixture.__wrapped__(tmp_path)


def plan_for(h, **changes):
    plan = dict(
        task_plan_revision=h.policy["plan_revision"],
        task_plan_digest=h.policy["plan_digest"],
        capability=dict(
            policy_id=h.policy["id"], declaration_digest=h.policy["declaration_digest"]
        ),
        profile="public-prep",
        objective="Prepare declared fixture",
        environment_plan="Pinned disposable image; no host access",
        acquisitions=[
            dict(
                action_id=h.action["id"],
                kind=h.action["kind"],
                purpose="Offline preparation",
                provenance="Synthetic fixture, not verified public data",
                access="public",
            )
        ],
        success_criteria="Matching synthetic receipt",
        stop_conditions="Any uncertainty or budget exhaustion",
        uncertainty="Live isolation not established",
        public_auto=True,
        tools=["context_read", "capability_prepare"],
    )
    plan.update(changes)
    return AutonomyPlan.model_validate(plan).model_dump(mode="json")


def setup_session(h, *, approve=True, **changes):
    store = AutonomyStore(h.state)
    plan = plan_for(h, **changes)
    _, session = store.create(h.owner, h.grant["task_id"], "session", plan)
    if approve:
        session = command(store, h.owner, session, "approve", plan_digest=session["plan_digest"])
    return store, session


def command(store, actor, session, choice, **extra):
    return store.command(
        actor,
        session["id"],
        choice + str(session["revision"]),
        dict(expected_revision=session["revision"], command=choice, **extra),
    )[1]


def start(store, h, session):
    return store.start(
        h.worker,
        session["id"],
        "start",
        dict(
            expected_revision=session["revision"],
            operation_id=h.operation["id"],
            package_digest=h.package.digest,
        ),
    )[1]


def update_body(session):
    action = session["actions"][-1]
    return dict(
        expected_revision=session["revision"],
        operation_id=action["operation_id"],
        nonce=action["nonce"],
    )


def finish(store, h, session):
    bundle = h.broker.run(h.package, lock=h.lock)
    h.journal.reconcile(h.package, lambda p, b, c: h.store.reconcile(h.worker, p, b, c))
    final = store.update_action(
        h.worker, session["id"], "receipt", update_body(session), receipt=True
    )[1]
    return bundle, final


@pytest.mark.parametrize("kind", ["data", "model", "package"])
def test_allowed_autonomous_action_receipt_audit_and_idempotency(make_broker, kind):
    h = make_broker(kind)
    store, session = setup_session(h)
    running = start(store, h, session)
    assert start(store, h, session) == running  # Historical response, not new authority.
    bundle, final = finish(store, h, running)
    assert bundle["state"] == "completed" and final["state"] == "ready"
    receipt = final["actions"][0]["receipt"]
    assert receipt["evidence"] == "synthetic" and receipt["cost"] is None
    assert receipt["artifact_sha256"] == h.action["sha256"]
    assert receipt["stdout_sha256"] is receipt["stderr_sha256"] is None
    assert receipt["termination_confirmed"] and receipt["scientific_validity"] == "not_assessed"
    assert final["actions"][0]["decision"] == "auto_approved"
    assert (
        store.update_action(h.worker, running["id"], "receipt", update_body(running), receipt=True)[
            1
        ]
        == final
    )
    assert AutonomyStore(h.state).get(h.owner, final["id"])["actions"] == final["actions"]
    with sqlite3.connect(store.database_path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE event_type='autonomy.action_bound'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT count(*) FROM events WHERE event_type='autonomy.state'").fetchone()[
                0
            ]
            == 4
        )
    prior = list(h.effects.calls)
    with pytest.raises(EffectDenied):
        h.broker.run(h.package)
    assert h.effects.calls == prior


def test_checkpoint_resume_requires_human_and_exact_action(make_broker):
    h = make_broker()
    store, session = setup_session(h, public_auto=False)
    with pytest.raises(Conflict, match="checkpoint"):
        start(store, h, session)
    checkpoint = dict(
        reason="consequential",
        question="Prepare this exact public artifact?",
        action_id=h.action["id"],
    )
    waiting = command(store, h.owner, session, "checkpoint", checkpoint=checkpoint)
    ready = command(store, h.owner, waiting, "resume")
    assert ready["deadline"] == session["deadline"]
    running = start(store, h, ready)
    assert running["actions"][0]["decision"] == "human_checkpoint"
    assert finish(store, h, running)[1]["state"] == "ready"


@pytest.mark.parametrize(
    "escalation",
    [
        "new_domain",
        "credentials",
        "private_source",
        "paid_source",
        "persistent_write",
        "external_effect",
        "scientific_execution",
        "publication_decision",
    ],
)
def test_escalation_never_becomes_effect_authority(make_broker, escalation):
    h = make_broker()
    capability = plan_for(h)["capability"] | {"escalation": [escalation]}
    store, session = setup_session(h, capability=capability)
    with pytest.raises(Forbidden, match="escalation"):
        start(store, h, session)
    assert escalation in store.get(h.owner, session["id"])["blocked_reasons"]
    assert not h.effects.calls


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE capability_policies SET status='revoked'",
        "UPDATE tasks SET revision=revision+1",
        "UPDATE execution_grants SET epoch=epoch+1",
        "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'",
        "UPDATE worker_registry SET lifecycle_state='retired'",
    ],
)
def test_stale_authority_blocks_start_and_use(make_broker, sql):
    h = make_broker()
    store, session = setup_session(h)
    with sqlite3.connect(store.database_path) as db:
        db.execute(sql)
    with pytest.raises((Conflict, Forbidden)):
        start(store, h, session)
    assert not h.effects.calls


def test_cancel_between_chunks_fences_existing_broker(make_broker):
    h = make_broker(content=b"a" * 100000)
    store, session = setup_session(h)
    running = start(store, h, session)

    def cancel(phase):
        if phase == "read":
            command(store, h.owner, running, "cancel")

    h.effects.before = cancel
    bundle = h.broker.run(h.package)
    assert bundle["state"] == "failed" and bundle["size"] == 65536
    state = store.get(h.owner, session["id"])
    assert state["state"] == "unknown" and state["cancel_requested"]
    with pytest.raises(Conflict):
        command(store, h.owner, state, "resume")


def test_lost_reply_unknown_receipt_never_resumes(make_broker):
    h = make_broker()
    store, session = setup_session(h)
    running = start(store, h, session)
    assert h.broker.run(h.package)["state"] == "completed"
    # Caller lost the reply. Later confirmed journal evidence does not erase that state.
    unknown = command(store, h.owner, running, "unknown")
    h.journal.reconcile(h.package, lambda p, b, c: h.store.reconcile(h.worker, p, b, c))
    final = store.update_action(
        h.worker, session["id"], "late", update_body(unknown), receipt=True
    )[1]
    assert final["state"] == "unknown" and final["actions"][0]["receipt"]["termination_confirmed"]
    with pytest.raises(Conflict):
        command(store, h.owner, final, "resume")
    with pytest.raises(Conflict):
        start(store, h, final)


def test_lease_expiry_restart_and_heartbeat_do_not_reauthorize(make_broker, monkeypatch):
    import tessera_control.autonomy as module

    h = make_broker()
    store, session = setup_session(h)
    running = start(store, h, session)
    renewed = store.update_action(h.worker, session["id"], "heartbeat", update_body(running))[1]
    future = datetime.now(UTC) + timedelta(seconds=6)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(module, "datetime", Clock)
    assert AutonomyStore(h.state).get(h.owner, session["id"])["state"] == "unknown"
    with pytest.raises(Conflict):
        store.update_action(h.worker, session["id"], "late", update_body(renewed))
    with pytest.raises(EffectDenied):
        h.broker.run(h.package)
    assert not h.effects.calls


def test_revision_expansion_and_suspend_preserve_deadline(make_broker):
    h = make_broker()
    store, session = setup_session(h)
    suspended = command(store, h.owner, session, "suspend")
    resumed = command(store, h.owner, suspended, "resume")
    assert resumed["deadline"] == session["deadline"]
    expanded = copy.deepcopy(resumed["plan"])
    expanded["capability"]["escalation"] = ["new_domain"]
    revised = command(store, h.owner, resumed, "revise", plan=expanded)
    assert revised["approved_by"] is None and revised["plan_digest"] != session["plan_digest"]
    with pytest.raises(Conflict):
        start(store, h, revised)
    with pytest.raises(Conflict):
        command(store, h.owner, revised, "approve", plan_digest=session["plan_digest"])
    approved = command(store, h.owner, revised, "approve", plan_digest=revised["plan_digest"])
    assert approved["deadline"] == session["deadline"]
    with pytest.raises(Forbidden):
        start(store, h, approved)


@pytest.mark.parametrize(
    "change",
    [
        {"workspace": "/host"},
        {"workspace": "/workspace/../control"},
        {"tools": ["shell"]},
        {"environment": {"TOKEN": "fixture-secret"}},
        {"credentials": "fixture-secret"},
        {"mounts": ["/var/run/docker.sock"]},
        {"limits": {"max_seconds": 901}},
        {"limits": {"spend_cents": 1}},
        {"argv": ["sh", "-c", "escape"]},
    ],
)
def test_secret_host_path_and_effect_fields_rejected(make_broker, change):
    h = make_broker()
    with pytest.raises(ValidationError):
        plan_for(h, **change)
    assert not h.effects.calls


def test_no_caller_receipts_wrong_package_and_audit_rollback(make_broker, monkeypatch):
    h = make_broker()
    store, session = setup_session(h)
    with pytest.raises(Conflict):
        store.start(
            h.worker,
            session["id"],
            "wrong",
            dict(expected_revision=2, operation_id=h.operation["id"], package_digest="0" * 64),
        )
    original = store._event

    def fail(*args, **kwargs):
        if args[4] == "autonomy.state":
            raise RuntimeError("injected audit failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        start(store, h, session)
    assert store.get(h.owner, session["id"])["state"] == "ready"
    with sqlite3.connect(store.database_path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE event_type='autonomy.action_bound'"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(store, "_event", original)
    running = start(store, h, session)
    with pytest.raises(Conflict, match="journal first"):
        store.update_action(
            h.worker, session["id"], "fake-receipt", update_body(running), receipt=True
        )
    with pytest.raises(StoreError):
        store.update_action(
            h.worker,
            session["id"],
            "forged",
            update_body(running) | {"success": True},
            receipt=True,
        )


def test_api_agent_can_propose_not_approve_and_tenant_access(make_broker):
    h = make_broker()
    client = TestClient(create_app(h.state))
    value = token("autonomy-agent")
    issue(
        client,
        h.state,
        value=value,
        kind="agent",
        audience="control",
        project_id=h.grant["project_id"],
        key="agent",
    )
    agent = headers(value)
    response = client.post(
        f"/tasks/{h.grant['task_id']}/autonomy-sessions",
        headers=agent | {"Idempotency-Key": "create"},
        json=plan_for(h),
    )
    assert response.status_code == 201, response.text
    session = response.json()
    review = dict(expected_revision=1, command="approve", plan_digest=session["plan_digest"])
    assert (
        client.post(
            f"/autonomy-sessions/{session['id']}/commands",
            headers=agent | {"Idempotency-Key": "approve"},
            json=review,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/autonomy-sessions/{session['id']}/commands",
            headers=owner_headers(h.state) | {"Idempotency-Key": "approve"},
            json=review,
        ).status_code
        == 200
    )
    projection = client.get(f"/autonomy-sessions/{session['id']}", headers=agent).json()
    assert projection["execution_enabled"] is projection["provider_enabled"] is False
    checkpoint = client.post(
        f"/autonomy-sessions/{session['id']}/commands",
        headers=agent | {"Idempotency-Key": "checkpoint"},
        json=dict(
            expected_revision=2,
            command="checkpoint",
            checkpoint=dict(reason="blocked", question="May I continue?"),
        ),
    )
    assert checkpoint.status_code == 200
    assert (
        client.post(
            f"/autonomy-sessions/{session['id']}/commands",
            headers=agent | {"Idempotency-Key": "resume"},
            json=dict(expected_revision=3, command="resume"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/worker/autonomy-sessions/{session['id']}/actions",
            headers=agent | {"Idempotency-Key": "start"},
            json=dict(
                expected_revision=2, operation_id=h.operation["id"], package_digest=h.package.digest
            ),
        ).status_code
        == 401
    )
    store = AutonomyStore(h.state)
    with sqlite3.connect(store.database_path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(NotFound):
            store._load(db, {**h.owner, "org_id": "another-org"}, session["id"])
        with pytest.raises(NotFound):
            store._load(db, {**h.owner, "project_id": "another-project"}, session["id"])


def test_provider_disabled_default_and_held_recovery(make_broker, tmp_path):
    h = make_broker()
    store, session = setup_session(
        h, profile="observation-only", public_auto=False, tools=["context_read"]
    )
    with pytest.raises(Conflict):
        start(store, h, session)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_state(h.state, backup)
    restore_state(backup, restored)
    owner = Store(restored).authenticate(
        owner_headers(restored)["Authorization"].removeprefix("Bearer ")
    )
    held = AutonomyStore(restored)
    projection = held.get(owner, session["id"])
    assert projection["plan_digest"] == session["plan_digest"] and projection["actions"] == []
    assert "authority_not_current" in projection["blocked_reasons"]
    with pytest.raises(Forbidden, match="held"):
        command(held, owner, session, "cancel")
    assert not h.effects.calls


@pytest.mark.parametrize("profile", ["public-prep", "experiment-ready"])
def test_new_session_cannot_rebind_operation_or_resume_inflight(make_broker, profile):
    h = make_broker()
    store, first = setup_session(h, profile=profile)
    running = start(store, h, first)
    _, second = store.create(h.owner, h.grant["task_id"], "second", plan_for(h))
    second = command(store, h.owner, second, "approve", plan_digest=second["plan_digest"])
    with pytest.raises(Conflict, match="already bound"):
        start(store, h, second)
    with pytest.raises(Conflict, match="In-flight"):
        command(store, h.owner, running, "resume")
    with pytest.raises(Conflict, match="identity"):
        store.update_action(
            h.worker, first["id"], "wrong-nonce", update_body(running) | {"nonce": "0" * 64}
        )
    with pytest.raises(Conflict, match="different request"):
        store.start(
            h.worker,
            first["id"],
            "start",
            dict(expected_revision=2, operation_id=h.operation["id"], package_digest="0" * 64),
        )


@pytest.mark.parametrize("access", ["unknown", "private", "paid"])
def test_source_classification_cannot_auto_approve(make_broker, access):
    h = make_broker()
    acquisitions = plan_for(h)["acquisitions"]
    acquisitions[0]["access"] = access
    store, session = setup_session(h, acquisitions=acquisitions)
    with pytest.raises(Forbidden):
        start(store, h, session)


def test_caps_default_deny_and_revoked_running_projection(make_broker):
    h = make_broker()
    minimal = plan_for(h)
    for field in ("profile", "public_auto", "tools"):
        minimal.pop(field)
    defaults = AutonomyPlan.model_validate(minimal)
    assert defaults.profile == "observation-only" and not defaults.public_auto
    store, session = setup_session(h, limits={"storage_bytes": 1, "output_bytes": 1})
    with pytest.raises(Forbidden):
        start(store, h, session)
    revised = command(store, h.owner, session, "revise", plan=plan_for(h))
    approved = command(store, h.owner, revised, "approve", plan_digest=revised["plan_digest"])
    running = start(store, h, approved)
    h.store.revoke(h.owner, h.policy["id"], "revoke", {"expected_revision": 2})
    assert store.get(h.owner, running["id"])["state"] == "unknown"
    with pytest.raises(EffectDenied):
        h.broker.run(h.package)
    assert not h.effects.calls
