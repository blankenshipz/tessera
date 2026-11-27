"""A create the trusted server's own use-time recheck refused after its claim was never sent.

A provider-provenance receipt is accepted only for an effect with a committed
``salad_proxy.claim``, so the field's ``admission_denied`` receipt always follows a claim. The
server now records ``salad_proxy.claim_refused`` when its recheck (which precedes the one
provider call) refuses the claim, and that record voids the claim (ADR 0205 amendment); with no
record only the bare pre-call receipt with no group identity, allocation or exchange field does
(q-2704 follow-up).
Real trusted server, store and HTTP transport double (``test_salad_server.fixture``)."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest
from test_remote_execution_routing import dispatch
from test_salad_server import fixture, preflight, wire

from tessera_control.salad_precreate import (
    EXCHANGE_FIELDS,
    effect_unsent,
    sent_launch_effects,
    unrecorded_refusal_unsent,
)
from tessera_control.salad_unclaimed import create_claimed
from tessera_control.store import Conflict, Forbidden


def _events(h, event_type):
    with sqlite3.connect(h.store.database_path) as db:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload_json FROM events WHERE event_type=? ORDER BY cursor", (event_type,)
            )
        ]


def _claimed(h, operation_id):
    with sqlite3.connect(h.store.database_path) as db:
        db.row_factory = sqlite3.Row
        return create_claimed(db, h.worker["org_id"], operation_id)


def _fence_on_second_resolve(h, monkeypatch, fence):
    """The capability or budget is fenced while the secret resolves: the recheck refuses."""
    resolve = h.server.resolver.resolve
    count = 0

    def resolve_then_fence(reference, version):
        nonlocal count
        count += 1
        secret = resolve(reference, version)
        if count == 2:
            with sqlite3.connect(h.store.database_path) as db:
                db.execute(fence)
        return secret

    monkeypatch.setattr(h.server.resolver, "resolve", resolve_then_fence)


@pytest.mark.parametrize(
    "fence",
    [
        "UPDATE capability_policies SET status='revoked'",
        "UPDATE experiment_budgets SET state='fenced'",
    ],
    ids=["capability_revoked", "budget_fenced"],
)
def test_a_recheck_refusal_records_the_claim_refused_and_the_create_is_unsent(
    tmp_path, monkeypatch, fence
):
    h = fixture(tmp_path, monkeypatch)
    _fence_on_second_resolve(h, monkeypatch, fence)
    factory = wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    # The field shape: one create effect, one provider receipt for it, admission_denied.
    receipt = state["receipts"][-1]
    assert receipt["effect_id"] == state["effects"][-1]["id"]
    assert receipt["provenance"] == "provider"
    assert receipt["proxy_reason"] == "admission_denied"
    assert receipt["provider_id"] is None and receipt["allocation_id"] is None
    assert receipt["state"] == "unknown" and receipt["termination_confirmed"] is False
    assert not any(m == "POST" and p.endswith("/containers") for m, p, _, _ in factory.calls)
    operation_id = state["proposal"]["operation_id"]
    (claim,) = _events(h, "salad_proxy.claim")
    (refused,) = _events(h, "salad_proxy.claim_refused")
    assert refused == {
        "nonce": claim["nonce"],
        "route_id": state["id"],
        "operation_id": operation_id,
        "action": "create",
        "stage": "use_time_recheck",
        "reason": "admission_denied",
        "provider_call_made": False,
    }
    assert effect_unsent(state, state["effects"][-1]) and not sent_launch_effects(state)
    assert _claimed(h, operation_id) is False


def test_a_failure_after_the_recheck_records_nothing_and_the_claim_stands(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    resolve = h.server.resolver.resolve
    count = 0

    def resolve_then_revoke(reference, version):
        nonlocal count
        count += 1
        secret = resolve(reference, version)
        if count == 2:
            h.server.resolver.revoke(reference)
        return secret

    monkeypatch.setattr(h.server.resolver, "resolve", resolve_then_revoke)
    wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    assert state["receipts"][-1]["proxy_reason"] == "credential_unavailable"
    assert _events(h, "salad_proxy.claim") and not _events(h, "salad_proxy.claim_refused")
    assert _claimed(h, state["proposal"]["operation_id"]) is True


def test_a_failed_refusal_write_leaves_the_bare_pre_call_receipt_as_the_proof(
    tmp_path, monkeypatch
):
    """With no record (a failed write here; v0.102.0 never wrote one) the real server's pre-call
    refusal receipt is itself the proof: no group identity, allocation or exchange field
    (q-2704 follow-up)."""
    h = fixture(tmp_path, monkeypatch)
    _fence_on_second_resolve(h, monkeypatch, "UPDATE capability_policies SET status='revoked'")

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(h.store, "record_claim_refused", refuse)
    factory = wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    receipt = state["receipts"][-1]
    assert receipt["proxy_reason"] == "admission_denied"
    assert all(receipt.get(name) is None for name in EXCHANGE_FIELDS)
    assert not receipt.get("evidence")
    assert not any(m == "POST" and p.endswith("/containers") for m, p, _, _ in factory.calls)
    assert not _events(h, "salad_proxy.claim_refused")
    assert _events(h, "salad_proxy.claim")
    assert unrecorded_refusal_unsent(state, state["effects"][-1])
    assert _claimed(h, state["proposal"]["operation_id"]) is False
    assert h.server.input_log[-1]["event"] == "claim_refusal_unrecorded"


def test_the_transports_admission_denied_after_a_create_post_stays_claimed(tmp_path, monkeypatch):
    """A 201 that did not read settled is refused ``admission_denied`` after Salad created the
    group, with no provider id on the receipt; ``proxy_request_kind`` keeps it claimed."""
    h = fixture(tmp_path, monkeypatch)
    running = copy.deepcopy(h.provider_group)
    running["current_state"]["status"] = "running"
    running["pending_change"] = True
    factory = wire(h, *preflight(h, absent=True), (201, running))
    state = dispatch(h, "create")
    receipt = state["receipts"][-1]
    assert receipt["proxy_reason"] == "admission_denied"
    assert receipt["provider_id"] is None and receipt["allocation_id"] is None
    assert receipt["proxy_request_kind"] == "create"
    assert any(m == "POST" and p.endswith("/containers") for m, p, _, _ in factory.calls)
    assert not _events(h, "salad_proxy.claim_refused")
    assert not unrecorded_refusal_unsent(state, state["effects"][-1])
    assert _claimed(h, state["proposal"]["operation_id"]) is True


def test_the_refusal_cannot_be_recorded_after_a_receipt_or_without_a_claim(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    _fence_on_second_resolve(h, monkeypatch, "UPDATE capability_policies SET status='revoked'")
    wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    effect = state["effects"][-1]["id"]
    # The effect has its receipt now: no later record can void its claim.
    with pytest.raises(Forbidden):
        h.store.record_claim_refused(h.worker, state["id"], effect)
    with sqlite3.connect(h.store.database_path) as db:
        db.execute("DELETE FROM events WHERE event_type='salad_proxy.claim_refused'")
        db.execute("DELETE FROM events WHERE event_type='salad_proxy.claim'")
    assert _claimed(h, state["proposal"]["operation_id"]) is False
    with pytest.raises(Forbidden):
        h.store.record_claim_refused(h.worker, state["id"], effect)


def test_a_refusal_needs_the_committed_claim_of_that_effect(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    calls = []
    original = h.store.claim_proxy

    def claim_then_check_refusal(worker, ident, effect_id, policy, preflight, *, recheck=False):
        if not recheck:
            # Before any claim the server's write is refused: there is nothing to void.
            with pytest.raises(Conflict):
                h.store.record_claim_refused(worker, ident, effect_id)
        calls.append(recheck)
        return original(worker, ident, effect_id, policy, preflight, recheck=recheck)

    monkeypatch.setattr(h.store, "claim_proxy", claim_then_check_refusal)
    wire(h, *preflight(h, absent=True), (500, {}))
    dispatch(h, "create")
    assert calls[:1] == [False]
    assert not _events(h, "salad_proxy.claim_refused")
