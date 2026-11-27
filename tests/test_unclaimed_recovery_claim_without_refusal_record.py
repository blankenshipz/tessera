"""Blocked-campaign recovery refuses a claim with no ``claim_refused`` record once a group
identity is recorded (ADR 0205 amendment, point 6).

An earlier build's trusted server recorded the ``salad_proxy.claim`` create effect but never a
``salad_proxy.claim_refused`` event, and the receipt is the bare provider ``admission_denied``
refusal. That shape alone is reconcilable (covered end to end in
``test_salad_admission_denied_not_started``). The same shape with a recorded group identity (a
provider or allocation id on the receipt or the route) is a claimed create: the owner's recovery
request stays ``refused`` / ``create_claimed`` for a blocked campaign, writes nothing, and the
attempt stays ``unknown``. Real stores and runner (``SaladWorld``); the executor and trusted
server are played by the test."""

from __future__ import annotations

from typing import Any

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control.campaign_salad import SaladStageDriver
from tessera_control.store import Conflict, canonical_digest

CREATE = "df9bee483b654978a3e55b90e793c381"
BARE_RECEIPT = {
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
# Each alone: a recorded group identity / allocation, so the create may have reached Salad.
GROUP_IDENTITY = {
    "receipt_provider_id": ({"provider_id": "a" * 32}, {}),
    "receipt_allocation_id": ({"allocation_id": "b" * 64}, {}),
    "route_allocation_id": ({}, {"allocation_id": "b" * 64}),
}


def _world(tmp_path) -> SaladWorld:
    return SaladWorld(
        tmp_path,
        stages=lambda env: [
            salad_stage(env, retry={"max_attempts": 1, "infra_retries": 1}),
        ],
    )


def _record_claim_without_refusal(
    world: SaladWorld, receipt: dict[str, Any], route: dict[str, Any]
) -> None:
    """The route as the earlier build left it: a claim, the receipt, and no ``claim_refused``."""
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    row = world.salad()
    state = world.route_state()
    world._append(
        "salad_proxy.claim",
        dict(
            nonce=CREATE,
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
    state = world.route_state()
    state["effects"] = [*state["effects"], {"id": CREATE, "action": "create"}]
    state["receipts"] = [*(state.get("receipts") or []), {**BARE_RECEIPT, **receipt}]
    state.update(desired="stop", state="stop_requested", cancellation="pending")
    state.update(route)
    state["revision"] += 1
    world._append("remote_execution.state", state, state["revision"])
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
    assert not world.events("salad_proxy.claim_refused")


def _block_as_old_build(world: SaladWorld, monkeypatch) -> dict[str, Any]:
    """Leave the attempt ``unknown`` and its campaign ``blocked`` with no automatic budget."""
    world.runner.unclaimed_autoreconcile_limit = 0
    with monkeypatch.context() as patch:
        patch.setattr(SaladStageDriver, "_never_sent", lambda *args: False)
        world.tick()
    world.tick(3)
    attempt = _first(world)
    assert attempt["status"] == "unknown" and attempt["settled_cents"] is None
    assert world.status() == "blocked"
    return attempt


def _first(world: SaladWorld):
    return world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=1",
        world.campaign_id,
    )


def _request(world: SaladWorld, *, dry_run: bool = False):
    body = {"reason": "claim without a refusal record", "dry_run": dry_run}
    return world.env.store.reconcile_unclaimed_attempts(
        world.human,
        world.campaign_id,
        world.key("unclaimed"),
        canonical_digest(body),
        body,
    )


@pytest.mark.parametrize("shape", sorted(GROUP_IDENTITY))
def test_blocked_claim_without_refusal_record_but_with_group_identity_stays_refused(
    tmp_path, monkeypatch, shape
):
    receipt, route = GROUP_IDENTITY[shape]
    world = _world(tmp_path)
    _record_claim_without_refusal(world, receipt, route)
    _block_as_old_build(world, monkeypatch)

    _, listed = _request(world, dry_run=True)
    (entry,) = listed["attempts"]
    assert entry["verdict"] == "refused" and entry["refusal"] == "create_claimed"
    with pytest.raises(Conflict, match="create_claimed"):
        _request(world)
    assert not world.events("campaign.unclaimed_reconcile_requested")

    world.tick(3)
    attempt = _first(world)
    assert attempt["status"] == "unknown" and attempt["settled_cents"] is None
    assert world.status() == "blocked"
    assert not world.events("execution.grant_reconciled_unclaimed")
    assert not world.events("campaign.attempt_unclaimed_reconciled")
