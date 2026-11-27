"""A Salad create refused at the trusted server's use-time admission is not started.

Replays the field shape (ADR 0205 amendment): one create effect whose only receipt is the
provider-provenance ``admission_denied`` refusal (no provider id, allocation, request id,
response status or cost), a committed ``salad_proxy.claim`` for it (a provider receipt is only
accepted for a claimed effect) and a live receipt that never confirmed a stop. With the
server's ``salad_proxy.claim_refused`` record the runner unwinds the attempt at zero with no
person. Without it (a build before v0.103.0 never wrote one, q-2704 follow-up) the same bare
receipt with no group identity or allocation still reads never sent; any sign of a provider call
(a provider or allocation id, request id, response status, transport diagnostic, charge,
evidence or a start) keeps the attempt held. Real stores and runner (``SaladWorld``); the
executor and trusted server are played by the test."""

from __future__ import annotations

from typing import Any

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control.campaign_salad import SaladStageDriver
from tessera_control.store import Conflict, Forbidden, canonical_digest

CREATE = "df9bee483b654978a3e55b90e793c381"
FIELD_RECEIPT = {
    "effect_id": CREATE,
    "provenance": "provider",
    "proxy_reason": "admission_denied",
    "state": "unknown",
    "provider_id": None,
    "allocation_id": None,
    "termination_confirmed": False,
    "request_id": None,
    "response_status": None,
    "proxy_request_kind": None,
    "cost_cents": None,
    "evidence": [],
}
LIVE_RECEIPT = {
    "schema": "tessera.salad.live-check.v1",
    "status": "inconclusive",
    "reason": "custody_stop_or_reconnect_unresolved",
    "stop_confirmed": False,
    "outputs": [],
}


def _world(tmp_path, **retry: Any) -> SaladWorld:
    return SaladWorld(
        tmp_path,
        stages=lambda env: [
            salad_stage(env, retry={"max_attempts": 1, "infra_retries": 1, **retry})
        ],
    )


def _claim(world: SaladWorld, nonce: str = CREATE) -> None:
    row = world.salad()
    state = world.route_state()
    world._append(
        "salad_proxy.claim",
        dict(
            nonce=nonce,
            route_id=state["id"],
            route_digest=state["proposal_digest"],
            operation_id=row["operation_id"],
            attempt_id=state["proposal"]["attempt_id"],
            lease_id=row["lease_id"],
            epoch=row["epoch"],
            package_digest=row["package_digest"],
            action="create",
            preflight_digest="f" * 64,
        ),
    )


def _claim_refused(world: SaladWorld, nonce: str = CREATE) -> None:
    world._append(
        "salad_proxy.claim_refused",
        dict(
            nonce=nonce,
            route_id=world.route_state()["id"],
            operation_id=world.salad()["operation_id"],
            action="create",
            stage="use_time_recheck",
            reason="admission_denied",
            provider_call_made=False,
        ),
    )


# Receipts and route fields of a create that did reach Salad (or may have), each alone. The first
# is the transport's own ``admission_denied`` after a create POST answered 201 not settled.
REACHED_SALAD = {
    "create_post_refused": ({"proxy_request_kind": "create", "response_status": 201}, {}),
    "request_kind": ({"proxy_request_kind": "create"}, {}),
    "response_status": ({"response_status": 201}, {}),
    "request_id": ({"request_id": "req_0123456789"}, {}),
    "receipt_provider_id": ({"provider_id": "a" * 32}, {}),
    "receipt_allocation_id": ({"allocation_id": "b" * 64}, {}),
    "route_allocation_id": ({}, {"allocation_id": "b" * 64}),
    "cost": ({"cost_cents": 1}, {}),
    "evidence": ({"evidence": [{"kind": "provider_event", "digest": "c" * 64}]}, {}),
    "transport_detail": ({"proxy_detail": "timeout"}, {}),
}


def _field_route(
    world: SaladWorld,
    receipt: dict[str, Any] = FIELD_RECEIPT,
    route: dict[str, Any] | None = None,
    effects: tuple[dict[str, Any], ...] = (),
) -> None:
    """The route after the refused create and the live check's cancel: desired stop, no group."""
    state = world.route_state()
    state["effects"] = [*state["effects"], {"id": CREATE, "action": "create"}, *effects]
    state["receipts"] = [*(state.get("receipts") or []), dict(receipt)]
    state.update(desired="stop", state="stop_requested", cancellation="pending")
    state.update(route or {})
    state["revision"] += 1
    world._append("remote_execution.state", state, state["revision"])


def _outcome(world: SaladWorld) -> None:
    """What the executor records after its finish: no manifest, no provider id."""
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=LIVE_RECEIPT,
            manifest_id=None,
            provider_id=None,
            estimated_exposure={"estimated_exposure_cents": 0},
        ),
    )


def _executing(world: SaladWorld) -> None:
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))


def _held(world: SaladWorld) -> int:
    return world.row("SELECT held_cents FROM execution_project_limits")["held_cents"]


def _first(world: SaladWorld):
    return world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=1",
        world.campaign_id,
    )


def _blocked_by_old_build(world: SaladWorld, monkeypatch) -> None:
    """The attempt as an earlier build left it: ``unknown``, its campaign ``blocked``."""
    world.runner.unclaimed_autoreconcile_limit = 0
    with monkeypatch.context() as patch:
        patch.setattr(SaladStageDriver, "_never_sent", lambda *args: False)
        world.tick()
    attempt = _first(world)
    assert attempt["status"] == "unknown"
    assert attempt["failure_class"] == "observed_unknown"
    assert attempt["failure_detail"] == "route termination is not confirmed by provider receipts"
    world.tick(2)
    assert world.status() == "blocked"


def _request(world: SaladWorld, actor=None, *, dry_run: bool = False):
    body = {"reason": "field admission_denied create", "dry_run": dry_run}
    return world.env.store.reconcile_unclaimed_attempts(
        actor or world.human,
        world.campaign_id,
        world.key("unclaimed"),
        canonical_digest(body),
        body,
    )


# -- forward path ------------------------------------------------------------------------------


def test_a_recheck_refused_create_ends_not_started_at_zero_and_the_stage_retries(tmp_path):
    world = _world(tmp_path)
    _executing(world)
    held = _held(world)
    assert held > 0
    _claim(world)
    _claim_refused(world)
    _field_route(world)
    _outcome(world)
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    attempt = _first(world)
    assert attempt["status"] == "failed_transient"
    # An infrastructure retry, never a scientific one (max_attempts is 1).
    assert attempt["failure_class"] == "infra_salad_create_not_sent"
    assert attempt["failure_detail"] == (
        "live_check_create_not_sent; infrastructure failure, retry 1 of 1 allowed"
    )
    assert attempt["settled_cents"] == 0
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    assert world.events("execution.grant_reconciled_unclaimed")[-1]["proof"] == (
        "no_create_or_start_claim"
    )
    assert world.status() == "running"
    assert not world.events("campaign.blocked")
    assert _held(world) <= held  # the first hold is released; a retry holds only its own


def test_with_no_infrastructure_retry_the_unwind_keeps_the_existing_class(tmp_path):
    world = _world(tmp_path, infra_retries=0)
    _executing(world)
    _claim(world)
    _claim_refused(world)
    _field_route(world)
    _outcome(world)
    world.tick_until(lambda: world.status() in {"failed", "stopped"})
    attempt = _first(world)
    assert attempt["status"] == "failed_transient"
    assert attempt["failure_class"] == "t1_salad_unclaimed"
    assert attempt["failure_detail"] == "live_check_create_not_sent"
    assert attempt["settled_cents"] == 0
    assert not world.events("campaign.blocked")


def test_the_field_shape_from_an_earlier_build_with_no_refusal_record_unwinds_at_zero(tmp_path):
    """v0.102.0 refused the claim at its use-time recheck but wrote no ``claim_refused``: the
    bare pre-call receipt with no group identity or allocation is the proof (q-2704 follow-up)."""
    world = _world(tmp_path)
    _executing(world)
    held = _held(world)
    _claim(world)
    _field_route(world)
    _outcome(world)
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    attempt = _first(world)
    assert attempt["status"] == "failed_transient"
    assert attempt["failure_class"] == "infra_salad_create_not_sent"
    assert attempt["settled_cents"] == 0
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    event = world.events("execution.grant_reconciled_unclaimed")[-1]
    assert event["provider_termination_inferred"] is False and event["settlement_cents"] == 0
    assert world.status() == "running"
    assert not world.events("campaign.blocked")
    assert _held(world) <= held


@pytest.mark.parametrize("shape", sorted(REACHED_SALAD))
def test_a_claim_with_no_refusal_record_that_may_have_reached_salad_stays_held(tmp_path, shape):
    """Without the server's record, any sign of a provider call keeps the claim (fail closed)."""
    receipt, route = REACHED_SALAD[shape]
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    _field_route(world, {**FIELD_RECEIPT, **receipt}, route)
    _outcome(world)
    held = _held(world)
    world.tick(6)
    attempt = _first(world)
    assert attempt["status"] == "unknown" and attempt["settled_cents"] is None
    assert world.status() == "blocked"
    assert _held(world) == held
    assert not world.events("execution.grant_reconciled_unclaimed")


def test_a_claim_with_no_refusal_record_and_a_start_effect_stays_held(tmp_path):
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    _field_route(world, effects=({"id": "e" * 32, "action": "start"},))
    _outcome(world)
    world.tick(6)
    assert _first(world)["status"] == "unknown"
    assert not world.events("execution.grant_reconciled_unclaimed")


def test_a_refusal_record_of_another_nonce_does_not_void_the_claim(tmp_path):
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    _claim_refused(world, nonce="0" * 32)
    # A receipt the record-less reading cannot clear, so only the record could.
    _field_route(world, {**FIELD_RECEIPT, "proxy_request_kind": "create"})
    _outcome(world)
    world.tick(6)
    assert _first(world)["status"] == "unknown"
    assert not world.events("execution.grant_reconciled_unclaimed")


def test_a_create_whose_receipt_is_not_the_admission_refusal_stays_held(tmp_path):
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    _claim_refused(world)
    _field_route(world, {**FIELD_RECEIPT, "proxy_reason": "provider_unavailable"})
    _outcome(world)
    world.tick(6)
    assert _first(world)["status"] == "unknown"
    assert not world.events("execution.grant_reconciled_unclaimed")


def test_a_running_route_is_untouched(tmp_path):
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    state = world.route_state()
    state["effects"] = [*state["effects"], {"id": CREATE, "action": "create"}]
    state["receipts"] = [
        *(state.get("receipts") or []),
        {**FIELD_RECEIPT, "proxy_reason": "observed", "provider_id": "a" * 32, "state": "running"},
    ]
    state.update(provider_id="a" * 32, state="running")
    state["revision"] += 1
    world._append("remote_execution.state", state, state["revision"])
    held = _held(world)
    world.tick(4)
    attempt = _first(world)
    assert attempt["status"] == "running" and attempt["settled_cents"] is None
    assert world.phase() == "executing"
    assert _held(world) == held
    try:
        _request(world)
    except Conflict as error:
        assert "no unknown attempt" in str(error)
    else:  # pragma: no cover - the request must refuse
        raise AssertionError("a running attempt was offered for recovery")


# -- already-blocked recovery ------------------------------------------------------------------


def test_an_already_blocked_campaign_recovers_at_zero_on_the_owners_request(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    _claim_refused(world)
    _field_route(world)
    _outcome(world)
    _blocked_by_old_build(world, monkeypatch)
    held = _held(world)
    # The automatic limit is spent: the runner alone leaves it blocked.
    world.tick(3)
    assert world.status() == "blocked" and _first(world)["status"] == "unknown"
    status, listed = _request(world, dry_run=True)
    assert status == 200 and listed["dry_run"] is True
    (entry,) = listed["attempts"]
    assert entry["verdict"] == "reconcilable" and entry["proof"] == "no_create_or_start_claim"
    assert not world.events("campaign.unclaimed_reconcile_requested")
    status, response = _request(world)
    assert status == 200 and response["requested"] == [entry["campaign_attempt_id"]]
    (asked,) = world.events("campaign.unclaimed_reconcile_requested")
    assert asked["evidence_digest"] == entry["evidence_digest"]
    assert asked["requested_by_principal_id"] == world.human["id"]
    # A second request before the runner acts writes nothing new.
    status, again = _request(world)
    assert again["requested"] == [] and again["attempts"][0]["verdict"] == "requested"
    assert len(world.events("campaign.unclaimed_reconcile_requested")) == 1
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    attempt = _first(world)
    assert attempt["status"] == "failed_transient" and attempt["failure_class"] == "not_started"
    assert attempt["settled_cents"] == 0
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    reconciled = world.events("campaign.attempt_unclaimed_reconciled")[-1]
    assert reconciled["automatic"] is False
    assert reconciled["requested_by_principal_id"] == world.human["id"]
    assert reconciled["consumed_scientific_retry"] is False
    assert reconciled["released_hold_cents"] == grant["max_exposure_cents"]
    grant_event = world.events("execution.grant_reconciled_unclaimed")[-1]
    assert grant_event["provider_termination_inferred"] is False
    assert world.status() == "running"
    assert world.events("campaign.unblocked")
    assert _held(world) <= held


def test_an_earlier_build_attempt_with_no_refusal_record_recovers_on_the_owners_request(
    tmp_path, monkeypatch
):
    """The live v0.102.0 grant: a committed claim, no ``claim_refused``, the bare receipt."""
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    _field_route(world)
    _outcome(world)
    _blocked_by_old_build(world, monkeypatch)
    assert not world.events("salad_proxy.claim_refused")
    world.tick(3)
    assert world.status() == "blocked" and _first(world)["status"] == "unknown"
    _, listed = _request(world, dry_run=True)
    (entry,) = listed["attempts"]
    assert entry["verdict"] == "reconcilable" and entry["proof"] == "no_create_or_start_claim"
    status, response = _request(world)
    assert status == 200 and response["requested"] == [entry["campaign_attempt_id"]]
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    attempt = _first(world)
    assert attempt["status"] == "failed_transient" and attempt["failure_class"] == "not_started"
    assert attempt["settled_cents"] == 0
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    reconciled = world.events("campaign.attempt_unclaimed_reconciled")[-1]
    assert reconciled["automatic"] is False
    assert reconciled["consumed_scientific_retry"] is False
    assert (
        world.events("execution.grant_reconciled_unclaimed")[-1]["provider_termination_inferred"]
        is False
    )
    assert world.status() == "running"
    assert world.events("campaign.unblocked")


@pytest.mark.parametrize(
    "shape", ["create_post_refused", "receipt_provider_id", "route_allocation_id", "request_id"]
)
def test_the_recovery_refuses_a_claimed_create_and_writes_nothing(tmp_path, monkeypatch, shape):
    """A claimed create that reached (or may have reached) Salad stays ``create_claimed``."""
    receipt, route = REACHED_SALAD[shape]
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    _field_route(world, {**FIELD_RECEIPT, **receipt}, route)
    _outcome(world)
    world.tick(3)
    assert world.status() == "blocked"
    status, listed = _request(world, dry_run=True)
    (entry,) = listed["attempts"]
    assert entry["verdict"] == "refused" and entry["refusal"] == "create_claimed"
    try:
        _request(world)
    except Conflict as error:
        assert "create_claimed" in str(error)
    else:  # pragma: no cover - the request must refuse
        raise AssertionError("a claimed create was offered for recovery")
    assert not world.events("campaign.unclaimed_reconcile_requested")
    world.tick(3)
    assert world.status() == "blocked" and _first(world)["settled_cents"] is None


def test_the_recovery_needs_the_owners_credential(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _executing(world)
    _claim(world)
    _claim_refused(world)
    _field_route(world)
    _outcome(world)
    _blocked_by_old_build(world, monkeypatch)
    try:
        _request(world, world.worker)
    except Forbidden:
        pass
    else:  # pragma: no cover - the request must refuse
        raise AssertionError("a worker asked for an unclaimed recovery")
    assert not world.events("campaign.unclaimed_reconcile_requested")
