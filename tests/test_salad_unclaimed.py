"""Zero-settlement reconcile of a Salad grant that provably claimed no provider effect."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from salad_campaign_fixture import SaladWorld

from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.salad_unclaimed import SaladUnclaimedStore
from tessera_control.store import Conflict, Forbidden, canonical_digest


def revoke(world, grant_id):
    grant = world.row("SELECT revision FROM execution_grants WHERE id=?", grant_id)
    ExecutionStore(world.env.state).revoke_execution_grant(
        world.human, grant_id, world.key("revoke"), "d", grant["revision"]
    )


def reconcile(world, grant_id, actor=None):
    revision = world.row("SELECT revision FROM execution_grants WHERE id=?", grant_id)["revision"]
    body = {"expected_revision": revision}
    return SaladUnclaimedStore(world.env.state).reconcile(
        actor or world.human, grant_id, world.key("unclaimed"), canonical_digest(body), body
    )


@pytest.fixture
def packaged(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    return world, world.attempt()["grant_id"]


def test_unclaimed_reconcile_settles_zero_and_releases_the_project_hold(packaged):
    world, grant_id = packaged
    held_before = world.row("SELECT held_cents FROM execution_project_limits")["held_cents"]
    revoke(world, grant_id)
    status, grant = reconcile(world, grant_id)
    assert status == 200 and grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    limits = world.row("SELECT held_cents, settled_cents FROM execution_project_limits")
    assert limits["held_cents"] == held_before - 20 and limits["settled_cents"] == 0
    (event,) = world.events("execution.grant_reconciled_unclaimed")
    assert event["proof"] == "no_create_or_start_claim" and event["settlement_cents"] == 0
    assert event["operation_id"] == world.salad()["operation_id"]


def test_a_live_grant_is_not_reconcilable_as_unclaimed(packaged):
    world, grant_id = packaged
    with pytest.raises(Conflict, match="cancelled"):
        reconcile(world, grant_id)


@pytest.mark.parametrize("action", ["create", "start"])
def test_a_create_or_start_claim_refuses_it(packaged, action):
    world, grant_id = packaged
    # No route exists yet, so the claim is written against the operation directly.
    row = world.salad()
    world._append(
        "salad_proxy.claim",
        dict(operation_id=row["operation_id"], action=action, route_id="route_x", nonce="n" * 8),
    )
    revoke(world, grant_id)
    with pytest.raises(Conflict, match="claimed"):
        reconcile(world, grant_id)


def test_a_stop_or_observe_claim_is_not_a_provider_mutation_claim(packaged):
    world, grant_id = packaged
    row = world.salad()
    world._append(
        "salad_proxy.claim",
        dict(operation_id=row["operation_id"], action="observe", route_id="route_x", nonce="o" * 8),
    )
    revoke(world, grant_id)
    assert reconcile(world, grant_id)[0] == 200


def test_only_a_human_may_reconcile_and_the_revision_must_be_current(packaged):
    world, grant_id = packaged
    revoke(world, grant_id)
    with pytest.raises(Forbidden):
        reconcile(world, grant_id, actor=world.worker)
    body = {"expected_revision": 1}
    with pytest.raises(Conflict, match="revision"):
        SaladUnclaimedStore(world.env.state).reconcile(
            world.human, grant_id, "stale", canonical_digest(body), body
        )


def test_a_running_route_must_be_cancelled_first(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    grant_id = world.attempt()["grant_id"]
    revoke(world, grant_id)
    # desired == run and no claim: the route could still be started, so it is refused.
    with pytest.raises(Conflict, match="route"):
        reconcile(world, grant_id)


def test_a_grant_without_a_packaged_operation_is_refused(tmp_path):
    world = SaladWorld(tmp_path)
    world.start()
    world.tick_until(
        lambda: world.row("SELECT 1 FROM campaign_salad_stages WHERE source_id IS NOT NULL")
    )
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    grant_id = world.attempt()["grant_id"]
    revoke(world, grant_id)
    with pytest.raises(Conflict, match="operation"):
        reconcile(world, grant_id)


def test_the_http_route_is_human_only_and_idempotent(packaged):
    world, grant_id = packaged
    revoke(world, grant_id)
    client = TestClient(create_app(world.env.state))
    owner = (world.env.state / "owner.header").read_text().strip().split(": ", 1)[1]
    revision = world.row("SELECT revision FROM execution_grants WHERE id=?", grant_id)["revision"]
    path = f"/execution-grants/{grant_id}/reconcile-unclaimed-salad"
    headers = {"Authorization": owner, "Idempotency-Key": "k1", "X-Tessera-Automation": "test"}
    unauthenticated = client.post(path, json={"expected_revision": revision})
    assert unauthenticated.status_code in {401, 403}
    first = client.post(path, json={"expected_revision": revision}, headers=headers)
    again = client.post(path, json={"expected_revision": revision}, headers=headers)
    assert first.status_code == 200 and again.json() == first.json()
    assert first.json()["status"] == "reconciled"
