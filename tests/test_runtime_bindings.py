"""Actual temporary control auth/SQLite/API; owner-private native facts are synthetic."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_agent_authority import enroll, issue, post, prepared  # noqa: F401

from tessera_control.runtime_bindings import NativeCheckWindow, RuntimeBindingStore
from tessera_control.store import Conflict, Forbidden, Store, _connect, canonical_digest


class SyntheticOwnerAdapter:
    """Isolated upstream fixture, not a production adapter or provider guard proof."""

    stale = False
    mismatch = False
    revoke_during_check = False

    def verify_enrollment(self, connection, actor, request, scope, pins):
        assert actor["role"] == "human"

    def verify_allocation(self, connection, actor, snapshot, request):
        assert actor["credential_id"] != snapshot["scope"]["credential"]

    def verify_claim(self, connection, actor, operation, request):
        now = datetime.now(UTC)
        if self.revoke_during_check:
            scope = operation["binding"]["scope"]
            connection.execute(
                "INSERT INTO runtime_binding_revocations VALUES (?,?,?,?,?,?)",
                (
                    "synthetic-revoke",
                    "financial",
                    request["financial_ref"],
                    actor["org_id"],
                    scope["issuer"],
                    now.isoformat(),
                ),
            )
        window = NativeCheckWindow(
            local_check_ref=request["local_check_ref"],
            quota_check_ref=request["quota_check_ref"],
            operation_ref=operation["operation_ref"],
            actor_credential_id=actor["credential_id"],
            nonce=operation["nonce"],
            scope_digest=canonical_digest(operation["binding"]["scope"]),
            pins_digest=canonical_digest(request["pins"]),
            request_ref=request["request_ref"],
            final_fingerprint=request["final_fingerprint"],
            order_revision=request["order_revision"],
            sequence=request["sequence"],
            checked_at=now,
            snapshot_checked_at=now,
            valid_until=now + timedelta(seconds=10),
        )
        if self.stale:
            window = replace(
                window,
                checked_at=now - timedelta(minutes=1),
                snapshot_checked_at=now - timedelta(minutes=1),
            )
        if self.mismatch:
            window = replace(window, nonce="0" * 64)
        return window


class SafeFixture(dict):
    def __repr__(self):
        return "<temporary authenticated runtime fixture; credentials redacted>"


@pytest.fixture
def runtime(prepared, request):  # noqa: F811
    state, client, owner, agent, project, bounds, worker = prepared
    adapter = SyntheticOwnerAdapter()
    client.app.state.runtime_bindings.adapter = adapter
    parent = issue(prepared)
    publisher, publisher_record = enroll(client, owner, project)
    subject = Store(state).authenticate(agent["Authorization"].removeprefix("Bearer "))
    context_body = {
        "credential_id": subject["credential_id"],
        "destination": bounds["destination"],
        "title": "Bounded runtime context",
        "body": "Public fixture",
        "classification": "project",
        "disclosure_approved": True,
    }
    context = post(client, owner, f"/projects/{project}/agent-contexts", context_body)
    expiry = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    policy_body = {
        "version": "tessera-runtime-admission-v2",
        "assurance": "local-enforcement-not-provider-billing",
        "max_financial_age_seconds": 600,
        "max_evidence_age_seconds": 30,
        "provider_unknown": "explicitly-approved",
        "max_operation_age_seconds": 30,
        "allowed_actions": ["runtime:prepare", "runtime:invoke"],
        "allowed_native_phases": ["native-http-fetch", "native-ws-handshake", "native-ws-frame"],
        "model": bounds["model"],
        "max_native_operations": getattr(request, "param", 1),
        "max_exposure_cents": 10 * getattr(request, "param", 1),
        "exposure_per_operation_cents": 10,
        "expires_at": expiry,
    }
    policy = post(client, owner, f"/projects/{project}/runtime-policies", policy_body)
    pins = {
        n: "a" * 64
        for n in (
            "build",
            "sdk",
            "harness_schema",
            "effective_config",
            "loaded_skill",
            "typed_catalog",
            "tool_mapping",
            "binding",
            "terminal_implementation",
        )
    }
    pins["instance"] = "fixture-instance"
    enrollment_body = {
        "policy_ref": policy["policy_ref"],
        "expected_policy_revision": 1,
        "subject_credential_id": subject["credential_id"],
        "publisher_credential_id": publisher_record["credential_id"],
        "publisher_audience": "control",
        "publisher_ref": "b" * 64,
        "opaque_owner_ref": "c" * 64,
        "session_ref": "d" * 64,
        "destination": bounds["destination"],
        "role": "planner",
        "context_ref": context["id"],
        "context_digest": canonical_digest(context_body),
        "parent_agent_authority_grant_id": parent["id"],
        "expected_parent_revision": 1,
        "expected_parent_epoch": 1,
        "pins": pins,
        "expires_at": expiry,
    }
    enrollment = post(client, owner, f"/projects/{project}/runtime-enrollments", enrollment_body)
    financial_body = {
        "enrollment_ref": enrollment["enrollment_ref"],
        "expected_enrollment_revision": 1,
        "policy_ref": policy["policy_ref"],
        "expected_policy_revision": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "valid_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "purchased_credit_balance": 0,
        "auto_top_up_off": True,
        "automatic_reload_off": True,
    }
    financial = post(
        client, owner, f"/projects/{project}/runtime-financial-assertions", financial_body
    )
    resolve = {
        "enrollment_ref": enrollment["enrollment_ref"],
        "expected_enrollment_revision": 1,
        "financial_ref": financial["attestation_ref"],
        "expected_grant_revision": 1,
        "expected_grant_epoch": 1,
    }
    allocation = {
        **resolve,
        "action": "runtime:invoke",
        "phase": "terminal-http",
        "native_phase": "native-http-fetch",
        "pins": pins,
        "request_ref": "e" * 64,
        "final_fingerprint": "f" * 64,
        "order_revision": 1,
        "sequence": 1,
    }
    return SafeFixture(
        state=state,
        client=client,
        owner=owner,
        agent=agent,
        publisher=publisher,
        project=project,
        policy=policy,
        policy_body=policy_body,
        enrollment=enrollment,
        enrollment_body=enrollment_body,
        financial=financial,
        financial_body=financial_body,
        resolve=resolve,
        allocation=allocation,
        adapter=adapter,
        parent=parent,
        worker=worker,
    )


def allocate(r):
    return post(r["client"], r["publisher"], "/runtime-operations", r["allocation"])


def test_colocated_continuation_last_unit_enters_once_and_crash_never_retries(runtime):
    r = runtime
    op = allocate(r)
    b = claim_body(r, op)
    receipt = post(
        r["client"], r["publisher"], f"/runtime-operations/{op['operation_ref']}/claim", b
    )
    actor = Store(r["state"]).authenticate(r["publisher"]["Authorization"][7:])
    s = r["client"].app.state.runtime_bindings
    calls = []

    def native():
        calls.append("entered")
        raise RuntimeError("synthetic lost native reply")

    with pytest.raises(RuntimeError):
        s.guarded_native_continuation(actor, op["operation_ref"], receipt["claim_ref"], b, native)
    with pytest.raises(Conflict):
        s.guarded_native_continuation(actor, op["operation_ref"], receipt["claim_ref"], b, native)
    assert calls == ["entered"]
    assert s.get_operation(actor, op["operation_ref"])["state"] == "claimed"


def test_colocated_continuation_rechecks_private_revocation_before_native(runtime):
    r = runtime
    op = allocate(r)
    b = claim_body(r, op)
    receipt = post(
        r["client"], r["publisher"], f"/runtime-operations/{op['operation_ref']}/claim", b
    )
    actor = Store(r["state"]).authenticate(r["publisher"]["Authorization"][7:])
    r["adapter"].revoke_during_check = True
    calls = []
    with pytest.raises(Forbidden):
        r["client"].app.state.runtime_bindings.guarded_native_continuation(
            actor, op["operation_ref"], receipt["claim_ref"], b, lambda: calls.append("entered")
        )
    assert calls == []


def test_operation_expiry_before_claim_does_not_reserve(runtime, monkeypatch):
    import tessera_control.runtime_bindings as bindings

    r = runtime
    op = allocate(r)
    future = datetime.now(UTC) + timedelta(seconds=31)

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(bindings, "datetime", FutureClock)
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        claim_body(r, op),
        expected=403,
    )


def claim_body(r, op):
    return {
        "expected_operation_revision": 1,
        "nonce": op["nonce"],
        "ticket": op["ticket"],
        **{
            k: r["allocation"][k]
            for k in (
                "expected_grant_revision",
                "expected_grant_epoch",
                "expected_enrollment_revision",
                "financial_ref",
                "pins",
                "request_ref",
                "final_fingerprint",
                "order_revision",
                "sequence",
            )
        },
        "local_check_ref": "1" * 64,
        "quota_check_ref": "2" * 64,
    }


def fresh_ws_frame(r, request_ref="9" * 64, sequence=2):
    return {
        **r["allocation"],
        "request_ref": request_ref,
        "sequence": sequence,
        "phase": "terminal-ws",
        "native_phase": "native-ws-frame",
    }


def claim_and_native_return(r, allocation, effects):
    op = post(r["client"], r["publisher"], "/runtime-operations", allocation)
    b = claim_body({**r, "allocation": allocation}, op)
    claim = post(r["client"], r["publisher"], f"/runtime-operations/{op['operation_ref']}/claim", b)
    actor = Store(r["state"]).authenticate(r["publisher"]["Authorization"][7:])
    r["client"].app.state.runtime_bindings.guarded_native_continuation(
        actor,
        op["operation_ref"],
        claim["claim_ref"],
        b,
        lambda: effects.append(allocation["native_phase"]),
    )
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/settle",
        {
            "claim_ref": claim["claim_ref"],
            "expected_claim_revision": 2,
            "disposition": "native-returned",
        },
        expected=200,
    )
    return op, claim


@pytest.mark.parametrize("runtime", [2], indirect=True)
def test_returned_handshake_advances_to_fresh_frame_with_cumulative_last_unit(runtime):
    r = runtime
    effects = []
    handshake = {**r["allocation"], "phase": "terminal-ws", "native_phase": "native-ws-handshake"}
    first, claim1 = claim_and_native_return(r, handshake, effects)
    r["client"].app.state.runtime_bindings = RuntimeBindingStore(r["state"], r["adapter"])
    second, claim2 = claim_and_native_return(r, fresh_ws_frame(r), effects)
    assert effects == ["native-ws-handshake", "native-ws-frame"]
    assert first["nonce"] != second["nonce"] and first["ticket"] != second["ticket"]
    assert claim1["scope"]["grant"] == claim2["scope"]["grant"]
    assert claim2["remaining_native_operations"] == claim2["remaining_exposure_cents"] == 0
    third = post(r["client"], r["publisher"], "/runtime-operations", fresh_ws_frame(r, "8" * 64, 3))
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{third['operation_ref']}/claim",
        claim_body({**r, "allocation": fresh_ws_frame(r, "8" * 64, 3)}, third),
        expected=403,
    )
    c = _connect(Store(r["state"]).database_path)
    try:
        assert c.execute(
            "SELECT reserved_units,reserved_cents FROM runtime_authority_grants"
        ).fetchone()[:] == (2, 20)
        assert (
            c.execute(
                "SELECT reserved_cents FROM agent_authority_grants WHERE id=?", (r["parent"]["id"],)
            ).fetchone()[0]
            == 20
        )
        for table in (
            "runtime_native_claims",
            "runtime_native_entries",
            "runtime_native_settlements",
        ):
            assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 2
    finally:
        c.close()


@pytest.mark.parametrize("runtime", [2], indirect=True)
@pytest.mark.parametrize("state", ["claimed", "uncertain", "crashed-entry"])
def test_nonreturned_root_never_advances_across_restart(runtime, state):
    r = runtime
    op = allocate(r)
    b = claim_body(r, op)
    claim = post(r["client"], r["publisher"], f"/runtime-operations/{op['operation_ref']}/claim", b)
    if state == "uncertain":
        post(
            r["client"],
            r["publisher"],
            f"/runtime-operations/{op['operation_ref']}/settle",
            {
                "claim_ref": claim["claim_ref"],
                "expected_claim_revision": 2,
                "disposition": "uncertain",
            },
            expected=200,
        )
    if state == "crashed-entry":
        actor = Store(r["state"]).authenticate(r["publisher"]["Authorization"][7:])

        def lost_reply():
            raise RuntimeError("synthetic native reply lost")

        with pytest.raises(RuntimeError):
            r["client"].app.state.runtime_bindings.guarded_native_continuation(
                actor,
                op["operation_ref"],
                claim["claim_ref"],
                b,
                lost_reply,
            )
    r["client"].app.state.runtime_bindings = RuntimeBindingStore(r["state"], r["adapter"])
    post(r["client"], r["publisher"], "/runtime-operations", fresh_ws_frame(r), expected=409)
    snapshot = post(
        r["client"], r["publisher"], "/runtime-bindings/resolve", r["resolve"], expected=200
    )
    assert (
        snapshot["remaining_native_operations"] == 1 and snapshot["remaining_exposure_cents"] == 10
    )


@pytest.mark.parametrize("runtime", [2], indirect=True)
def test_concurrent_next_claim_moves_exact_predecessor_once(runtime):
    r = runtime
    effects = []
    first, _ = claim_and_native_return(r, r["allocation"], effects)
    allocations = [fresh_ws_frame(r, "8" * 64, 2), fresh_ws_frame(r, "9" * 64, 3)]
    operations = [post(r["client"], r["publisher"], "/runtime-operations", a) for a in allocations]
    actor = Store(r["state"]).authenticate(r["publisher"]["Authorization"][7:])

    def claim(i):
        try:
            return RuntimeBindingStore(r["state"], r["adapter"]).claim_operation(
                actor,
                operations[i]["operation_ref"],
                "next-claim-" + str(i),
                claim_body({**r, "allocation": allocations[i]}, operations[i]),
            )
        except Conflict:
            return 409, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sorted(result[0] for result in results) == [201, 409]
    winner = next(i for i, result in enumerate(results) if result[0] == 201)
    loser = 1 - winner
    receipt = results[winner][1]
    r["client"].app.state.runtime_bindings.guarded_native_continuation(
        actor,
        operations[winner]["operation_ref"],
        receipt["claim_ref"],
        claim_body({**r, "allocation": allocations[winner]}, operations[winner]),
        lambda: effects.append("next-frame"),
    )
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{operations[winner]['operation_ref']}/settle",
        {
            "claim_ref": receipt["claim_ref"],
            "expected_claim_revision": 2,
            "disposition": "native-returned",
        },
        expected=200,
    )
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{operations[loser]['operation_ref']}/claim",
        claim_body({**r, "allocation": allocations[loser]}, operations[loser]),
        expected=409,
    )
    assert first["operation_ref"] != operations[winner]["operation_ref"]
    assert effects == ["native-http-fetch", "next-frame"]


@pytest.mark.parametrize("runtime", [2], indirect=True)
def test_returned_projection_without_immutable_settlement_is_not_authority(runtime):
    r = runtime
    op = allocate(r)
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        claim_body(r, op),
    )
    c = _connect(Store(r["state"]).database_path)
    try:
        # Test corruption of a mutable projection, never weaken historical triggers.
        c.execute("UPDATE runtime_native_root_fences SET state='native-returned'")
        c.commit()
    finally:
        c.close()
    post(r["client"], r["publisher"], "/runtime-operations", fresh_ws_frame(r), expected=409)


def test_real_issue_resolve_last_unit_claim_readback_restart_settle(runtime):
    r = runtime
    snapshot = post(
        r["client"], r["publisher"], "/runtime-bindings/resolve", r["resolve"], expected=200
    )
    assert (
        snapshot["scope"]["principal"]
        != Store(r["state"]).authenticate(r["publisher"]["Authorization"][7:])["id"]
    )
    assert snapshot["provider_entitlement"] == "unknown"
    op = allocate(r)
    payload = claim_body(r, op)
    path = f"/runtime-operations/{op['operation_ref']}/claim"
    receipt = post(r["client"], r["publisher"], path, payload, key="claim")
    assert receipt["remaining_native_operations"] == 0 and receipt["delivery"] == "first-claim"
    r["client"].app.state.runtime_bindings = RuntimeBindingStore(r["state"], r["adapter"])
    replay = post(r["client"], r["publisher"], path, payload, key="claim", expected=200)
    assert replay["delivery"] == "readback-only" and replay["claim_ref"] == receipt["claim_ref"]
    post(r["client"], r["publisher"], path, payload, expected=409)
    post(
        r["client"],
        r["publisher"],
        "/runtime-operations",
        {**r["allocation"], "request_ref": "9" * 64},
        expected=409,
    )
    settled = post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/settle",
        {
            "expected_claim_revision": 2,
            "claim_ref": receipt["claim_ref"],
            "disposition": "uncertain",
        },
        expected=200,
    )
    assert settled["state"] == "uncertain"
    after = post(
        r["client"], r["publisher"], "/runtime-bindings/resolve", r["resolve"], expected=200
    )
    assert after["remaining_native_operations"] == 0 and after["remaining_exposure_cents"] == 0


@pytest.mark.parametrize(
    "field", ["nonce", "ticket", "final_fingerprint", "request_ref", "sequence", "order_revision"]
)
def test_wrong_claim_pin_rolls_back(runtime, field):
    r = runtime
    op = allocate(r)
    b = claim_body(r, op)
    b[field] = 2 if field in {"sequence", "order_revision"} else "0" * 64
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        b,
        expected=403,
    )
    assert (
        r["client"]
        .get(f"/runtime-operations/{op['operation_ref']}", headers=r["publisher"])
        .json()["state"]
        == "allocated"
    )


@pytest.mark.parametrize("mode", ["stale", "mismatch", "revoke_during_check"])
def test_private_check_scope_freshness_and_current_revocation(runtime, mode):
    r = runtime
    op = allocate(r)
    setattr(r["adapter"], mode, True)
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        claim_body(r, op),
        expected=403,
    )
    c = _connect(Store(r["state"]).database_path)
    try:
        assert c.execute("SELECT count(*) FROM runtime_native_claims").fetchone()[0] == 0
        assert c.execute("SELECT reserved_units FROM runtime_authority_grants").fetchone()[0] == 0
    finally:
        c.close()


def test_concurrent_distinct_keys_cannot_claim_twice(runtime):
    r = runtime
    op = allocate(r)
    actor = Store(r["state"]).authenticate(r["publisher"]["Authorization"][7:])
    b = claim_body(r, op)

    def call(i):
        try:
            return RuntimeBindingStore(r["state"], r["adapter"]).claim_operation(
                actor, op["operation_ref"], str(i), b
            )[0]
        except Conflict:
            return 409

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(call, range(2))) == [201, 409]


@pytest.mark.parametrize("kind", ["policy", "enrollment", "financial"])
def test_revoke_fences_current_claim_and_immutable_finance(runtime, kind):
    r = runtime
    op = allocate(r)
    ref = r[kind][
        {"policy": "policy_ref", "enrollment": "enrollment_ref", "financial": "attestation_ref"}[
            kind
        ]
    ]
    path = {
        "policy": "runtime-policies",
        "enrollment": "runtime-enrollments",
        "financial": "runtime-financial-assertions",
    }[kind]
    post(r["client"], r["owner"], f"/{path}/{ref}/revoke", {"expected_revision": 1}, expected=200)
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        claim_body(r, op),
        expected=403,
    )
    c = _connect(Store(r["state"]).database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE runtime_financial_assertions SET created_at='changed'")
    finally:
        c.close()


def test_event_failure_rolls_back_claim_budget_and_idempotency(runtime, monkeypatch):
    r = runtime
    op = allocate(r)
    s = r["client"].app.state.runtime_bindings

    def fail(*args):
        raise RuntimeError("synthetic event failure")

    monkeypatch.setattr(s, "_event", fail)
    actor = Store(r["state"]).authenticate(r["publisher"]["Authorization"][7:])
    with pytest.raises(RuntimeError):
        s.claim_operation(actor, op["operation_ref"], "failed-event", claim_body(r, op))
    c = _connect(s.database_path)
    try:
        assert c.execute("SELECT count(*) FROM runtime_native_claims").fetchone()[0] == 0
        assert c.execute("SELECT reserved_units FROM runtime_authority_grants").fetchone()[0] == 0
        assert c.execute("SELECT state FROM runtime_native_operations").fetchone()[0] == "allocated"
    finally:
        c.close()
