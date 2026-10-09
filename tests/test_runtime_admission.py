"""Actual local v2 verifier/SQLite; SYNTHETIC credentials/authority/issuer ports."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tessera_mcp.runtime_admission import (
    AdmissionDenied,
    AdmissionLedger,
    AdmissionPolicy,
    CurrentAuthority,
    CurrentLocalState,
    Evidence,
    Financial,
    LocalChecks,
    NativeTerminalPort,
    OperatorStatus,
    Pins,
    Quota,
    RuntimeAdmissionVerifier,
    Scope,
    TerminalExpectation,
    public_digest,
)


@pytest.fixture
def synthetic_v2(tmp_path):
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)

    def stamp(delta=0):
        return (now + timedelta(seconds=delta)).isoformat()

    policy = AdmissionPolicy(
        version="tessera-runtime-admission-v2",
        assurance="local-enforcement-not-provider-billing",
        admission_policy_ref="approved-policy",
        revision=6,
        max_financial_age_seconds=3600,
        max_evidence_age_seconds=30,
        provider_unknown="explicitly-approved",
        operator_issuer="SYNTHETIC-operator",
    )
    scope = Scope(
        issuer="SYNTHETIC-guard",
        publisher_ref="a" * 64,
        implementation="1" * 64,
        enrollment="approved-enrollment",
        principal="SYNTHETIC-agent",
        credential="SYNTHETIC-credential",
        opaque_owner_ref="2" * 64,
        project="project",
        destination="destination",
        session_ref="3" * 64,
        grant="grant",
        grant_revision=2,
        grant_epoch=4,
        policy_ref=policy.admission_policy_ref,
        policy_digest=policy.digest,
        actions_digest=public_digest(["dispatch", "session"]),
        role="planner",
        context_ref="approved-context",
        context_digest="b" * 64,
    )
    pins = Pins(
        instance="SYNTHETIC-instance",
        **{key: "4" * 64 for key in Pins.model_fields if key != "instance"},
    )
    financial = dict(
        evidence_class="authenticated-operator-attestation",
        attestation_ref="operator-assertion",
        issuer=policy.operator_issuer,
        opaque_owner_ref=scope.opaque_owner_ref,
        project=scope.project,
        destination=scope.destination,
        policy_revision=policy.revision,
        observed_at=stamp(-120),
        asserted_at=stamp(-60),
        valid_until=stamp(600),
        purchased_credit_balance=0,
        auto_top_up_off=True,
        automatic_reload_off=True,
    )
    local = {
        key: "passed"
        for key in (
            "oauth_method",
            "owner_account_match",
            "openai_only",
            "approved_provider_model",
            "chatgpt_endpoint",
            "api_key_overrides_absent",
            "custom_overrides_absent",
            "no_paid_fallback",
        )
    }
    local.update(
        evidence_class="completed-native-local-enforcement", provider="openai", model="model"
    )
    state = dict(
        authority_revoked=False,
        operator_revoked=False,
        budget=True,
        context=True,
        auth=True,
        kind="operator",
        installed=pins,
        late=False,
        effects=0,
        bound=0,
        current_scope=scope,
        operator_assertion=Financial(**financial),
        actions=("session", "dispatch"),
    )
    expected = TerminalExpectation(
        "session-preparation", "5" * 64, "6" * 64, 4, 2, "7" * 64, "8" * 64
    )
    receipt = dict(
        version=policy.version,
        assurance=policy.assurance,
        nonce="7" * 64,
        ticket="8" * 64,
        phase=expected.phase,
        observed_at=stamp(),
        valid_until=stamp(20),
        scope=scope.model_dump(),
        pins=pins.model_dump(),
        local=local,
        quota=dict(
            outcome="passed",
            snapshot_checked_at=stamp(-5),
            checked_at=stamp(-1),
            valid_until=stamp(25),
        ),
        financial=financial,
        provider_facts=dict(entitlement="unknown", billing_guarantee="unknown"),
        order_revision=4,
        sequence=2,
        request_ref=expected.request_ref,
        final_fingerprint=expected.fingerprint,
    )
    ledger = AdmissionLedger(tmp_path / "private-ledger")
    state.update(
        native_local=LocalChecks(**local),
        native_quota=Quota(**receipt["quota"]),
        native_expected=expected,
    )

    def current_local(operation, expected):
        return CurrentLocalState(
            state["native_local"], state["native_quota"], now, state["native_expected"]
        )

    def authorize(operation):
        return CurrentAuthority(
            state["current_scope"],
            state.get("authority_expiry", now + timedelta(seconds=60)),
            operation,
            state["authority_revoked"],
            state["budget"],
            state["context"],
            state["actions"],
        )

    def operator(_):
        return OperatorStatus(state["operator_assertion"], state["kind"], state["operator_revoked"])

    def authenticate(raw, _):
        return hashlib.sha256(raw).hexdigest() if state["auth"] else "wrong"

    def bind(verifier):
        state["bound"] += 1
        state["terminal_verifier"] = verifier

        def tick(supervisor, run_id, prompt):
            raise AdmissionDenied("SYNTHETIC-native-no-dispatch")

        return tick

    def make():
        return RuntimeAdmissionVerifier(
            policy=policy,
            scope=scope,
            pins=pins,
            model="model",
            ledger=ledger,
            authorize=authorize,
            authenticate_operator=operator,
            authenticate_evidence=authenticate,
            current_local_checks=current_local,
            terminal_port=NativeTerminalPort(lambda: state["installed"], bind),
            clock=lambda: now,
        )

    rig = SimpleNamespace(
        now=now,
        stamp=stamp,
        policy=policy,
        scope=scope,
        pins=pins,
        receipt=receipt,
        expected=expected,
        state=state,
        ledger=ledger,
        make=make,
    )
    yield rig
    ledger.close()


def encoded(rig):
    return json.dumps(rig.receipt).encode()


def test_session_ready_is_not_dispatch_or_included_oauth(synthetic_v2):
    rig = synthetic_v2
    result = rig.make().verify(encoded(rig), operation="session", expected=rig.expected)
    assert result["state"] == "session-ready-no-dispatch"
    assert "included_oauth_verified" not in result and "complete" not in result
    assert rig.state["effects"] == 0


def test_terminal_current_context_requires_exact_final_phase(synthetic_v2):
    rig = synthetic_v2
    expected = replace(rig.expected, phase="terminal-http")
    rig.receipt["phase"] = expected.phase
    rig.state["native_expected"] = expected
    result = rig.make().verify_terminal(encoded(rig), expected)
    assert result["state"] == "terminal-validated"
    assert result["request_ref"] == expected.request_ref
    assert rig.state["effects"] == 0  # Validation itself never dispatches.


@pytest.mark.parametrize(
    "group,key,value",
    [
        (None, "version", "tessera-runtime-admission-v1"),
        (None, "assurance", "provider-billing-guaranteed"),
        (None, "complete", True),
        (None, "included_oauth_verified", True),
        (None, "phase", "post-model-request-hooks"),
        (None, "order_revision", 5),
        (None, "sequence", 3),
        (None, "request_ref", "a" * 64),
        (None, "final_fingerprint", "a" * 64),
        (None, "observed_at", "2026-10-07T12:00:01+00:00"),
        (None, "valid_until", "2026-10-07T12:00:00+00:00"),
        (None, "observed_at", "2026-10-07T11:59:20+00:00"),
        ("local", "oauth_method", "unknown"),
        ("local", "owner_account_match", "unknown"),
        ("local", "chatgpt_endpoint", "unknown"),
        ("local", "no_paid_fallback", "unknown"),
        ("local", "api_key_overrides_absent", "unknown"),
        ("local", "custom_overrides_absent", "unknown"),
        ("local", "provider", "other"),
        ("local", "model", "other"),
        ("scope", "principal", "foreign"),
        ("scope", "issuer", "foreign"),
        ("scope", "implementation", "a" * 64),
        ("scope", "grant_revision", 3),
        ("scope", "policy_digest", "a" * 64),
        ("scope", "session_ref", "a" * 64),
        ("pins", "build", "a" * 64),
        ("pins", "sdk", "a" * 64),
        ("pins", "instance", "other"),
        ("pins", "effective_config", "a" * 64),
        ("pins", "loaded_skill", "a" * 64),
        ("pins", "typed_catalog", "a" * 64),
        ("pins", "tool_mapping", "a" * 64),
        ("pins", "terminal_implementation", "a" * 64),
        ("quota", "outcome", "unknown"),
        ("quota", "snapshot_checked_at", "2026-10-07T11:59:00+00:00"),
        ("quota", "checked_at", "2026-10-07T12:00:01+00:00"),
        ("quota", "valid_until", "2026-10-07T12:00:31+00:00"),
        ("financial", "issuer", "SYNTHETIC-agent"),
        ("financial", "project", "foreign"),
        ("financial", "destination", "foreign"),
        ("financial", "opaque_owner_ref", "a" * 64),
        ("financial", "policy_revision", 5),
        ("financial", "observed_at", "2026-10-07"),
        ("financial", "observed_at", "2026-10-07T10:00:00+00:00"),
        ("financial", "asserted_at", "2026-10-07T12:00:01+00:00"),
        ("financial", "valid_until", "2026-10-07T12:00:00+00:00"),
        ("financial", "purchased_credit_balance", 1),
        ("financial", "purchased_credit_balance", False),
        ("financial", "auto_top_up_off", False),
        ("financial", "automatic_reload_off", 1),
        ("financial", "account_id", "FORBIDDEN-SYNTHETIC"),
        ("provider_facts", "entitlement", "measured"),
        ("provider_facts", "billing_guarantee", "included"),
    ],
)
def test_missing_stale_mismatch_partial_and_unknown_deny(synthetic_v2, group, key, value):
    rig = synthetic_v2
    (rig.receipt[group] if group else rig.receipt)[key] = value
    with pytest.raises(AdmissionDenied):
        rig.make().verify(encoded(rig), operation="session", expected=rig.expected)
    assert rig.state["effects"] == 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("authority_revoked", True),
        ("operator_revoked", True),
        ("budget", False),
        ("context", False),
        ("auth", False),
        ("kind", "agent"),
        ("actions", ()),
        ("installed", None),
    ],
)
def test_independent_current_ports_deny(synthetic_v2, key, value):
    rig = synthetic_v2
    rig.state[key] = value
    with pytest.raises(AdmissionDenied):
        rig.make().verify(encoded(rig), operation="session", expected=rig.expected)
    assert rig.state["effects"] == 0


@pytest.mark.parametrize("state", ["pending", "uncertain", "validated"])
def test_persistent_replay_denies_restart_and_new_nonce_same_ticket(synthetic_v2, tmp_path, state):
    rig = synthetic_v2
    rig.ledger.claim(rig.receipt["nonce"], rig.receipt["ticket"], "request-scope")
    rig.ledger.finish(rig.receipt["nonce"], state)
    rig.ledger.close()
    rig.ledger.db = __import__("sqlite3").connect(tmp_path / "private-ledger/admission-v2.sqlite3")
    with pytest.raises(AdmissionDenied, match="replayed"):
        rig.make().verify(encoded(rig), operation="session", expected=rig.expected)
    rig.receipt["nonce"] = "9" * 64
    with pytest.raises(AdmissionDenied, match="replayed"):
        rig.make().verify(encoded(rig), operation="session", expected=rig.expected)


def test_failure_uncertain_and_cannot_retry(synthetic_v2):
    rig = synthetic_v2
    verifier = rig.make()
    rig.state["auth"] = False
    with pytest.raises(AdmissionDenied):
        verifier.verify(encoded(rig), operation="session", expected=rig.expected)
    assert rig.ledger.db.execute("SELECT state FROM tickets").fetchone()[0] == "uncertain"
    rig.state["auth"] = True
    with pytest.raises(AdmissionDenied, match="replayed"):
        verifier.verify(encoded(rig), operation="session", expected=rig.expected)


def test_late_installed_order_or_revocation_change_denies(synthetic_v2):
    rig = synthetic_v2
    verifier = rig.make()

    def revoke(_):
        rig.state["authority_revoked"] = True
        return OperatorStatus(rig.state["operator_assertion"], "operator", False)

    verifier.authenticate_operator = revoke
    with pytest.raises(AdmissionDenied):
        verifier.verify(encoded(rig), operation="session", expected=rig.expected)


def test_missing_required_policy_age_is_not_defaulted(synthetic_v2):
    data = synthetic_v2.policy.model_dump()
    del data["max_financial_age_seconds"]
    with pytest.raises(ValidationError):
        AdmissionPolicy(**data)


def test_duplicate_json_or_missing_local_fact_is_not_admitted(synthetic_v2):
    rig = synthetic_v2
    raw = encoded(rig)
    with pytest.raises(AdmissionDenied):
        rig.make().verify(
            b'{"version":"legacy",' + raw[1:], operation="session", expected=rig.expected
        )
    del rig.receipt["local"]["owner_account_match"]
    with pytest.raises(AdmissionDenied):
        rig.make().verify(encoded(rig), operation="session", expected=rig.expected)


def test_partial_preparation_cannot_validate_terminal(synthetic_v2):
    rig = synthetic_v2
    with pytest.raises(AdmissionDenied):
        rig.make().verify_terminal(encoded(rig), replace(rig.expected, phase="terminal-ws"))


def test_evidence_closed_schema(synthetic_v2):
    assert Evidence.model_validate(synthetic_v2.receipt).version == synthetic_v2.policy.version
    assert Evidence.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("publisher_ref", "c" * 64),
        ("role", "critic"),
        ("project", "other"),
        ("destination", "other"),
        ("context_ref", "other"),
        ("context_digest", "c" * 64),
        ("credential", "other"),
        ("enrollment", "other"),
    ],
)
def test_same_authenticated_key_does_not_authorize_wrong_publisher_or_scope(
    synthetic_v2, key, value
):
    rig = synthetic_v2
    # Synthetic authentication still accepts bytes; exact independently expected pins deny.
    rig.receipt["scope"][key] = value
    with pytest.raises(AdmissionDenied):
        rig.make().verify(encoded(rig), operation="session", expected=rig.expected)
    assert rig.state["effects"] == 0


def next_challenge(rig):
    expected = replace(rig.expected, nonce="c" * 64, ticket="d" * 64, request_ref="e" * 64)
    rig.receipt.update(
        nonce=expected.nonce, ticket=expected.ticket, request_ref=expected.request_ref
    )
    rig.state["native_expected"] = expected
    return expected


@pytest.mark.parametrize(
    "change", ["quota", "financial", "principal", "grant", "expiry", "revoked", "budget"]
)
def test_cached_previous_session_admission_never_replaces_current_checks(synthetic_v2, change):
    rig = synthetic_v2
    verifier = rig.make()
    verifier.verify(encoded(rig), operation="session", expected=rig.expected)
    expected = next_challenge(rig)
    if change == "quota":
        rig.state["native_quota"] = rig.state["native_quota"].model_copy(
            update={"snapshot_checked_at": rig.stamp(-2)}
        )
    elif change == "financial":
        rig.state["operator_assertion"] = rig.state["operator_assertion"].model_copy(
            update={"asserted_at": rig.stamp(-20)}
        )
    elif change == "principal":
        rig.state["current_scope"] = rig.scope.model_copy(update={"principal": "other"})
    elif change == "grant":
        rig.state["current_scope"] = rig.scope.model_copy(update={"grant_revision": 3})
    elif change == "expiry":
        rig.state["authority_expiry"] = rig.now
    elif change == "budget":
        rig.state["budget"] = False
    else:
        rig.state["authority_revoked"] = True
    with pytest.raises(AdmissionDenied):
        verifier.verify(encoded(rig), operation="session", expected=expected)
    assert rig.state["effects"] == 0


@pytest.mark.parametrize("which", ["operator", "native", "installed", "clock"])
def test_final_reread_detects_attestation_quota_pins_and_expiry_toctou(synthetic_v2, which):
    rig = synthetic_v2
    verifier = rig.make()
    if which == "operator":
        calls = 0
        original = verifier.authenticate_operator

        def change(ref):
            nonlocal calls
            calls += 1
            if calls == 2:
                rig.state["operator_revoked"] = True
            return original(ref)

        verifier.authenticate_operator = change
    elif which == "native":
        calls = 0
        original = verifier.current_local_checks

        def change(operation, expected):
            nonlocal calls
            calls += 1
            if calls == 2:
                rig.state["native_expected"] = replace(expected, order_revision=99)
            return original(operation, expected)

        verifier.current_local_checks = change
    elif which == "installed":
        original = verifier.authenticate_operator

        def change(ref):
            rig.state["installed"] = None
            return original(ref)

        verifier.authenticate_operator = change
    else:
        calls = 0

        def advance():
            nonlocal calls
            calls += 1
            return rig.now if calls == 1 else rig.now + timedelta(seconds=31)

        verifier.clock = advance
    with pytest.raises(AdmissionDenied):
        verifier.verify(encoded(rig), operation="session", expected=rig.expected)
    assert rig.state["effects"] == 0


def test_new_nonce_ticket_cannot_replay_consumed_native_request_scope(synthetic_v2):
    rig = synthetic_v2
    verifier = rig.make()
    verifier.verify(encoded(rig), operation="session", expected=rig.expected)
    expected = replace(rig.expected, nonce="c" * 64, ticket="d" * 64)
    rig.receipt.update(nonce=expected.nonce, ticket=expected.ticket)
    rig.state["native_expected"] = expected
    with pytest.raises(AdmissionDenied, match="replayed"):
        verifier.verify(encoded(rig), operation="session", expected=expected)


@pytest.mark.parametrize("flag", ["authorized", "complete", "verified", "terminal_installed"])
def test_statefile_flags_cannot_become_capability_or_enrollment(synthetic_v2, tmp_path, flag):
    rig = synthetic_v2
    path = tmp_path / "untrusted-state.json"
    path.write_text(json.dumps({flag: True}))
    flags = json.loads(path.read_text())
    verifier = rig.make()
    verifier.authorize = lambda _: flags
    with pytest.raises(AdmissionDenied):
        verifier.verify(encoded(rig), operation="session", expected=rig.expected)
    assert rig.state["effects"] == 0


def test_missing_terminal_or_json_port_cannot_use_success_flag(synthetic_v2):
    rig = synthetic_v2
    rig.state["installed"] = {"verified": True, "complete": True}
    with pytest.raises(AdmissionDenied):
        rig.make()
    assert rig.state["effects"] == 0


def test_grant_revision_or_policy_refresh_cannot_revive_same_native_request(synthetic_v2):
    rig = synthetic_v2
    prior = rig.make()
    prior.verify(encoded(rig), operation="session", expected=rig.expected)
    scope = rig.scope.model_copy(update={"grant_revision": 3})
    rig.state["current_scope"] = scope
    expected = replace(rig.expected, nonce="c" * 64, ticket="d" * 64)
    rig.receipt.update(scope=scope.model_dump(), nonce=expected.nonce, ticket=expected.ticket)
    rig.state["native_expected"] = expected
    verifier = RuntimeAdmissionVerifier(
        policy=rig.policy,
        scope=scope,
        pins=rig.pins,
        model="model",
        ledger=rig.ledger,
        authorize=prior.authorize,
        authenticate_operator=prior.authenticate_operator,
        authenticate_evidence=prior.authenticate_evidence,
        current_local_checks=prior.current_local_checks,
        terminal_port=prior.terminal_port,
        clock=lambda: rig.now,
    )
    with pytest.raises(AdmissionDenied, match="replayed"):
        verifier.verify(encoded(rig), operation="session", expected=expected)


def test_untrusted_operator_or_native_success_dictionary_denies(synthetic_v2):
    rig = synthetic_v2
    verifier = rig.make()
    verifier.authenticate_operator = lambda _: {"authorized": True, "role": "human"}
    with pytest.raises(AdmissionDenied):
        verifier.verify(encoded(rig), operation="session", expected=rig.expected)
    expected = next_challenge(rig)
    verifier = rig.make()
    verifier.current_local_checks = lambda *args: {"verified": True, "complete": True}
    with pytest.raises(AdmissionDenied):
        verifier.verify(encoded(rig), operation="session", expected=expected)
    assert rig.state["effects"] == 0


@pytest.mark.parametrize("transport", ["custom-http-client", "native-redirect", "arbitrary-socket"])
def test_unsupported_transport_is_not_assumed_terminal_guarded(synthetic_v2, transport):
    rig = synthetic_v2
    expected = replace(rig.expected, phase=transport)
    with pytest.raises(AdmissionDenied, match="independent_native_context_invalid"):
        rig.make().verify_terminal(encoded(rig), expected)
    assert rig.state["effects"] == 0


@pytest.mark.parametrize("value", [True, {"complete": True}, None])
def test_native_binding_handshake_never_accepts_boolean_or_record_as_tick(synthetic_v2, value):
    rig = synthetic_v2
    verifier = rig.make()
    verifier.terminal_port = NativeTerminalPort(lambda: rig.pins, lambda _: value)
    with pytest.raises(AdmissionDenied, match="approved_terminal_tick_adapter_missing"):
        verifier.bind_native_tick()
    assert rig.state["effects"] == 0
