"""A guardian outage during the pre-start ticket mint is transient infrastructure.

The live receipt names a failed ticket mint or input delivery check whose cause was
``GuardianUnavailable`` and the route carries no ``start`` effect: the attempt is retried within
``retry.infra_retries`` (class ``infra_salad_guardian_unavailable``), never a scientific retry and
never ``failed_permanent`` for this alone. Any other cause keeps the permanent behaviour.
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_campaign_class_fallback import _attempts, _route_effects, _stop_reason, _world

from tessera_control.campaign_infra_retry import INFRA_SALAD_GUARDIAN_UNAVAILABLE, is_infra_class
from tessera_control.campaign_models import FAILURE_CLASSES
from tessera_control.campaign_report import _FAILURES


def _failed_receipt(world, *, code, exception, effects=("create",), extra=None) -> None:
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    _route_effects(world, *effects)
    world.terminate_route()
    world.observe_exit()
    receipt = {
        "schema": "tessera.salad.live-check.v1",
        "status": "failed",
        "reason": "execution_or_output_unresolved",
        "stop_confirmed": True,
        "prestart_failure": {"class": code, "exception": exception},
        "create_rejection": None,
        **(extra or {}),
    }
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(route_id=row["route_id"], live_receipt=receipt),
    )


def _settle(world) -> None:
    """The failed attempt awaits its recorded charge exactly as any failed receipt does."""
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)


def _world_with(tmp_path, infra_retries):
    return _world(
        tmp_path, alternates=(), retry={"infra_retries": infra_retries, "max_class_fallbacks": 0}
    )


def test_the_class_is_registered_as_infrastructure():
    assert is_infra_class(INFRA_SALAD_GUARDIAN_UNAVAILABLE)
    assert INFRA_SALAD_GUARDIAN_UNAVAILABLE in FAILURE_CLASSES
    assert INFRA_SALAD_GUARDIAN_UNAVAILABLE in _FAILURES


@pytest.mark.parametrize("code", ["ticket_mint_failed", "input_delivery_unconfirmed"])
def test_a_guardian_outage_before_start_is_retried_as_infrastructure(tmp_path, code):
    world = _world_with(tmp_path, 1)
    world.to_route_started()
    _failed_receipt(
        world,
        code=code,
        exception="GuardianUnavailable",
        extra={"prestart_delivery_retries": [{"attempt": 1}]},
    )
    _settle(world)
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    first = _attempts(world)[0]
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == INFRA_SALAD_GUARDIAN_UNAVAILABLE
    assert "retry 1 of 1" in first["failure_detail"]
    assert world.status() != "failed"


def test_with_the_infra_budget_spent_the_stage_fails_under_the_transient_named_class(tmp_path):
    world = _world_with(tmp_path, 0)
    world.to_route_started()
    _failed_receipt(world, code="ticket_mint_failed", exception="GuardianUnavailable")
    _settle(world)
    world.tick_until(lambda: world.status() == "failed")
    attempt = _attempts(world)[0]
    assert attempt["failure_class"] == INFRA_SALAD_GUARDIAN_UNAVAILABLE
    assert attempt["failure_class"] not in {"step_refused", "salad_not_passed"}
    assert "no infrastructure retry left" in attempt["failure_detail"]
    assert world.row("SELECT 1 FROM campaign_stage_attempts WHERE attempt=2") is None
    assert _stop_reason(world).endswith(INFRA_SALAD_GUARDIAN_UNAVAILABLE)


def test_a_second_outage_past_the_budget_fails_the_stage_under_the_infra_class(tmp_path):
    world = _world_with(tmp_path, 1)
    world.to_route_started()
    _failed_receipt(world, code="ticket_mint_failed", exception="GuardianUnavailable")
    _settle(world)
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    from test_campaign_class_fallback import _to_next_route

    _to_next_route(world)
    _failed_receipt(world, code="ticket_mint_failed", exception="GuardianUnavailable")
    _settle(world)
    world.tick_until(lambda: world.status() == "failed")
    second = _attempts(world)[1]
    assert second["failure_class"] == INFRA_SALAD_GUARDIAN_UNAVAILABLE
    assert second["status"] == "failed_permanent"
    assert _stop_reason(world) == f"stage_failed:g1:{INFRA_SALAD_GUARDIAN_UNAVAILABLE}"


@pytest.mark.parametrize(
    "exception,effects",
    [("Conflict", ("create",)), ("GuardianUnavailable", ("create", "start"))],
)
def test_other_causes_or_a_dispatched_start_keep_the_permanent_behaviour(
    tmp_path, exception, effects
):
    world = _world_with(tmp_path, 1)
    world.to_route_started()
    _failed_receipt(world, code="ticket_mint_failed", exception=exception, effects=effects)
    _settle(world)
    world.tick_until(lambda: world.status() == "failed")
    attempt = _attempts(world)[0]
    assert attempt["status"] == "failed_permanent"
    assert attempt["failure_class"] != INFRA_SALAD_GUARDIAN_UNAVAILABLE
