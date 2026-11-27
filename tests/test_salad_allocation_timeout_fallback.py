"""A CPU Salad group that never gets an instance falls back to the next CPU profile.

The one evidence-gated exception to the strict rule's "no move after a start effect": the
guardian's own allocation deadline fired, its stop was confirmed, its flags are clean, nothing
was billed and its estimate is exactly zero, control's own guardian read said ``stopped`` (the
attempt settled at its hold under ``guardian_bounded_hold``), the route was never reallocated and
the live receipt did not pass. Anything missing keeps today's infrastructure retry path.

Real stores and runner; the Salad executor, guardian and human are the doubles of the Salad
campaign fixture (no provider call, no network, no money). Nothing here proves provider behavior.
"""

from __future__ import annotations

import dataclasses

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec,
    salad_stage,
)
from test_campaign_class_fallback import _attempts, _prestart_failure, _stop_reason, _to_next_route
from test_campaign_salad_settlement import CLEAN
from test_salad_allocation_deadline import TIMEOUT_FINAL
from test_salad_cpu_profile_fallback_runner import COSTLY, RATE, _country, _cpu, _receipt
from test_salad_profile_strict_rule import _world as _gpu_world

from tessera_control import campaign_class_fallback as class_fallback
from tessera_control import campaign_salad_fallback as fallback
from tessera_control.campaign_models import normalize, parse_spec_json, stage_provider
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles

TIMEOUT = "infra_allocation_timeout"
REBOUND = {
    "count": 1,
    "events": [
        {
            "at": "2026-01-01T00:00:00Z",
            "from_allocation": "allocation-one",
            "to_allocation": "allocation-two",
            "reason": "prestart_reallocation",
        }
    ],
}


def _world(
    tmp_path,
    chain=("cpu-b", "cpu-c"),
    *,
    fallbacks=1,
    infra=0,
    max_attempts=1,
    over=None,
) -> SaladWorld:
    """A CPU stage on ``gpu-main`` (a CPU profile; the fixture's name), no spec alternates; the
    operator file chains ``chain`` after it."""
    over = over or {}
    world = SaladWorld(tmp_path, gpu_class=None, cpu_rate=RATE)
    env = world.env
    main = {"fallback_profiles": list(chain)} if chain else {}
    documents = [_cpu(env, "gpu-main", **main, **over.get("gpu-main", {}))] + [
        _cpu(env, name, **over.get(name, {})) for name in ("cpu-b", "cpu-c")
    ]
    world.profiles = SaladCampaignProfiles.model_validate(
        dict(schema="tessera.salad.campaign-profiles.v1", profiles=documents)
    )
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    stage = salad_stage(env, "g1")
    stage["salad"] = {**stage["salad"], "gpu": False}
    stage["retry"] = {
        **stage["retry"],
        "max_attempts": max_attempts,
        "infra_retries": infra,
        "max_class_fallbacks": fallbacks,
    }
    world.spec = salad_spec(env, [stage])
    return world


def _timeout(world, final=TIMEOUT_FINAL) -> None:
    """Executor double: the group was created and started, then the guardian cancelled it at
    the allocation deadline (no outputs). A final the settlement path cannot bound waits for a
    person's charge, which is recorded so the attempt concludes."""
    world.run_to_outcome(passed=False, outputs=False, guardian_final=final)
    first = len(_attempts(world))

    def concluded() -> bool:
        rows = _attempts(world)
        return rows[first - 1]["status"] not in {
            "admitted",
            "running",
            "exited",
            "collected",
            "resolved",
            "measured",
            "reconciled",
        } or (world.phase() == "awaiting_charge")

    world.tick_until(concluded)
    if world.phase() == "awaiting_charge":
        world.record_charge(1)
        world.tick_until(lambda: _attempts(world)[first - 1]["status"].startswith("failed"))


def _ended(world, number=1) -> dict:
    return dict(_attempts(world)[number - 1])


def _shape(row: dict) -> tuple:
    return tuple(
        row[name]
        for name in (
            "status",
            "failure_class",
            "failure_detail",
            "settled_cents",
            "settlement_basis",
        )
    )


def _today(tmp_path, final=TIMEOUT_FINAL, **kw) -> tuple:
    """Today's behaviour for the same timeout: the same stage with no fallback chain."""
    root = tmp_path / "today"
    root.mkdir(mode=0o700)
    # Registered last, so the provider registry now serves this world (the caller's is done).
    world = _world(root, chain=(), **kw)
    world.to_route_started()
    _timeout(world, final)
    world.tick(3)
    return _shape(_ended(world)), len(_attempts(world)), world.status()


# -- (1) the move ----------


def test_a_proven_no_instance_timeout_moves_to_the_next_cpu_profile(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    assert _country(world) == "US"
    _timeout(world)
    world.tick_until(lambda: len(_attempts(world)) == 2)
    first = _ended(world)
    # The attempt's own record is today's class; nothing was ever placed, so it settles at the
    # guardian's accrued estimate (zero here), still a provisional bounded hold, not its hold.
    assert first["status"] == "failed_transient" and first["failure_class"] == TIMEOUT
    assert first["settlement_basis"] == "guardian_bounded_hold"
    assert first["settled_cents"] == 0 < first["hold_cents"]
    assert "class fallback 1 of 1" in first["failure_detail"]
    [switch] = world.events(fallback.SWITCH_EVENT)
    assert switch["reason"] == fallback.REASON_ALLOCATION_TIMEOUT
    assert switch["source"] == fallback.SOURCE_OPERATOR
    assert (switch["from_profile"], switch["to_profile"]) == ("gpu-main", "cpu-b")
    assert (switch["from_attempt"], switch["to_attempt"]) == (1, 2)
    assert switch["started"] is True and switch["spent"] is False
    assert switch["settled_cents"] == first["settled_cents"]
    assert switch["settlement_basis"] == "guardian_bounded_hold"
    assert switch["estimated_exposure_cents"] == "0"
    evidence = switch["evidence"]
    assert evidence["guardian_status"] == "stopped" and evidence["guardian_verdict_digest"]
    assert evidence["allocation_timeout"] == {"elapsed_seconds": 125, "deadline_seconds": 120}
    assert world.events(fallback.STOP_EVENT) == []
    assert world.events(class_fallback.EVENT) == []  # not a pre-start, zero-settled move
    _to_next_route(world)
    assert _country(world) == "CA"  # the next attempt is placed on the fallback profile
    entry = _receipt(world)["stages"][0]
    assert [a["salad_profile"] for a in entry["attempts"]] == ["gpu-main", "cpu-b"]
    [recorded] = entry["salad_profiles"]["switches"]
    assert recorded["reason"] == "allocation_timeout" and recorded["started"] is True
    assert recorded["settlement_basis"] == "guardian_bounded_hold"
    assert recorded["estimated_exposure_cents"] == "0"


# -- (2) any missing proof: today's behaviour ----------


@pytest.mark.parametrize(
    "final",
    [
        dict(TIMEOUT_FINAL, estimated_exposure_cents="1"),
        dict(TIMEOUT_FINAL, estimated_exposure_cents="0.01"),
        dict(TIMEOUT_FINAL, orphan=True),
        dict(TIMEOUT_FINAL, allocation_uncertain=True),
        dict(TIMEOUT_FINAL, escalation_pending=True),
        dict(TIMEOUT_FINAL, budget_fenced=True),
        dict(TIMEOUT_FINAL, rate_unknown=True),
        dict(TIMEOUT_FINAL, status="unknown"),
        dict(TIMEOUT_FINAL, actual_billed_cost_cents=0),
        dict(CLEAN, estimated_exposure_cents="0"),  # no allocation_timeout
        dict(TIMEOUT_FINAL, prestart_rebinds=REBOUND),  # allocated once, then re-bound
    ],
    ids=[
        "estimate_1",
        "estimate_fraction",
        "orphan",
        "allocation_uncertain",
        "escalation_pending",
        "budget_fenced",
        "rate_unknown",
        "not_stopped",
        "actual_billed",
        "no_timeout",
        "prestart_rebinds",
    ],
)
def test_any_missing_proof_keeps_todays_path_exactly(tmp_path, final):
    world = _world(tmp_path, infra=1)
    world.to_route_started()
    _timeout(world, final)
    world.tick(3)
    assert world.events(fallback.SWITCH_EVENT) == []
    assert world.events(fallback.STOP_EVENT) == []
    assert (_shape(_ended(world)), len(_attempts(world)), world.status()) == _today(
        tmp_path, final, infra=1
    )


def test_the_provider_proof_refuses_each_missing_record(tmp_path):
    """The evidence read itself, on a concluded timeout of a stage with no chain."""
    world = _world(tmp_path, chain=(), infra=0)
    world.to_route_started()
    _timeout(world)
    world.tick(2)
    provider = stage_provider("salad")
    spec = normalize(parse_spec_json(world.row("SELECT spec_json FROM campaigns")["spec_json"]))
    stage = spec.stage("g1")
    attempt = _ended(world)
    with world.env.store.runner_connection() as connection:
        assert provider.allocation_timeout_evidence(connection, attempt, stage) is not None
        for name, value in (
            ("salad_verdict", "ok"),
            ("settlement_basis", "human_charge"),
            ("settled_cents", None),
        ):
            assert (
                provider.allocation_timeout_evidence(connection, {**attempt, name: value}, stage)
                is None
            )
        gpu = dataclasses.replace(stage, provider_config={**stage.provider_config, "gpu": True})
        assert provider.allocation_timeout_evidence(connection, attempt, gpu) is None
    # A reallocated route is never proof.
    world.terminate_route(reallocated=True)
    with world.env.store.runner_connection() as connection:
        assert provider.allocation_timeout_evidence(connection, attempt, stage) is None


# -- (3) a fallback over the approved hold ----------


def test_a_fallback_over_the_hold_is_never_moved_to_and_today_applies(tmp_path):
    over = {"cpu-b": {"cpu_rate": COSTLY}, "cpu-c": {"cpu_rate": COSTLY}}
    world = _world(tmp_path, infra=1, over=over)
    world.to_route_started()
    _timeout(world)
    world.tick(3)
    assert world.events(fallback.SWITCH_EVENT) == []
    # Unlike a pre-start move, nothing is held: the infrastructure retry path runs unchanged.
    assert world.events(fallback.STOP_EVENT) == []
    assert (_shape(_ended(world)), len(_attempts(world)), world.status()) == _today(
        tmp_path, infra=1
    )


def test_an_uncovered_fallback_is_skipped_for_the_next_covered_one(tmp_path):
    world = _world(tmp_path, over={"cpu-b": {"cpu_rate": COSTLY}})
    world.to_route_started()
    _timeout(world)
    world.tick_until(lambda: len(_attempts(world)) == 2)
    [switch] = world.events(fallback.SWITCH_EVENT)
    assert switch["to_profile"] == "cpu-c"
    assert switch["skipped"] == [{"profile": "cpu-b", "detail": "fallback_over_hold"}]
    _to_next_route(world)
    assert _country(world) == "DE"


# -- (4) no bound or no chain ----------


@pytest.mark.parametrize("fallbacks, chain", [(0, ("cpu-b",)), (1, ())])
def test_without_a_bound_or_a_chain_nothing_changes(tmp_path, fallbacks, chain):
    world = _world(tmp_path, chain, fallbacks=fallbacks, infra=1)
    world.to_route_started()
    _timeout(world)
    world.tick(3)
    assert world.events(fallback.SWITCH_EVENT) == []
    assert (_shape(_ended(world)), len(_attempts(world)), world.status()) == _today(
        tmp_path, fallbacks=fallbacks, infra=1
    )


# -- (5) GPU ----------


def test_a_gpu_stage_keeps_the_strict_rule(tmp_path):
    world = _gpu_world(tmp_path)
    world.to_route_started()
    _timeout(world)
    world.tick(3)
    assert world.events(fallback.SWITCH_EVENT) == []
    first = _ended(world)
    assert first["failure_class"] == TIMEOUT
    assert "moved to the next profile" not in (first["failure_detail"] or "")


# -- (6) idempotent ----------


def test_a_replayed_conclusion_or_a_restart_records_one_switch(tmp_path):
    world = _world(tmp_path)
    world.to_route_started()
    _timeout(world)
    world.tick_until(lambda: len(_attempts(world)) == 2)
    runner = world.runner
    ctx = runner._load(world.campaign_id)
    first = _ended(world)
    # A replay of the conclusion (as if the attempt were still concluding) adds nothing.
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='reconciled' WHERE id=?", first["id"]
    )
    assert runner._allocation_timeout_fallback(ctx, "g1", first, None) is True
    with world.env.store.runner_connection() as connection:
        again = fallback.allocation_timeout_move(
            runner, ctx, "g1", ctx.spec.stage("g1"), _ended(world), connection
        )
    assert again == fallback.MOVED
    assert len(world.events(fallback.SWITCH_EVENT)) == 1
    assert _ended(world)["status"] == "failed_transient"
    world.tick(5)
    assert len(world.events(fallback.SWITCH_EVENT)) == 1


# -- the move is not an infrastructure retry (ADR 0191 section 1 item 9) ----------


def test_with_no_infrastructure_retries_a_proven_timeout_still_moves_and_is_bounded(tmp_path):
    """infra_retries 0: today a timeout fails the stage (stage_infra_retries_exhausted). A proven
    no-instance timeout instead moves along the chain, limited only by max_class_fallbacks, and
    the next timeout with no move left then fails exactly as today."""
    world = _world(tmp_path, infra=0, fallbacks=1)
    world.to_route_started()
    _timeout(world)
    _to_next_route(world)
    assert _country(world) == "CA"
    assert len(world.events(fallback.SWITCH_EVENT)) == 1
    assert world.events("campaign.stage_infra_retries_exhausted") == []
    assert world.status() == "running"
    _timeout(world)
    world.tick_until(lambda: world.status() == "failed")
    rows = _attempts(world)
    assert len(rows) == 2 and rows[1]["status"] == "failed_permanent"
    assert len(world.events(fallback.SWITCH_EVENT)) == 1  # no second move: bound spent
    assert world.events("campaign.stage_infra_retries_exhausted") != []


def test_with_no_infrastructure_retries_and_no_bound_the_first_timeout_fails_as_today(tmp_path):
    world = _world(tmp_path, infra=0, fallbacks=0)
    world.to_route_started()
    _timeout(world)
    world.tick_until(lambda: world.status() == "failed")
    rows = _attempts(world)
    assert len(rows) == 1 and rows[0]["status"] == "failed_permanent"
    assert world.events(fallback.SWITCH_EVENT) == []
    assert world.events("campaign.stage_infra_retries_exhausted") != []


# -- (7) the worst case ----------


def test_timeout_fallback_timeout_is_bounded_by_the_approved_worst_case(tmp_path):
    world = _world(tmp_path, infra=1, fallbacks=1)
    world.to_route_started()
    _timeout(world)
    _to_next_route(world)
    assert _country(world) == "CA"
    _timeout(world)
    world.tick_until(lambda: world.status() == "failed")
    rows = _attempts(world)
    retry = normalize(parse_spec_json(world.row("SELECT spec_json FROM campaigns")["spec_json"]))
    assert len(rows) <= retry.stage("g1").retry.worst_case_attempts
    assert len(rows) == 2  # the second timeout found no move and no infrastructure retry left
    assert [r["failure_class"] for r in rows] == [TIMEOUT, TIMEOUT]
    assert rows[1]["status"] == "failed_permanent"
    assert "no infrastructure retry left" in rows[1]["failure_detail"]
    assert len(world.events(fallback.SWITCH_EVENT)) == 1


def test_a_timeout_move_shares_the_bound_with_pre_start_moves(tmp_path):
    world = _world(tmp_path, fallbacks=1)
    world.to_route_started()
    _timeout(world)
    _to_next_route(world)
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: world.status() == "failed")
    assert len(_attempts(world)) == 2
    assert len(world.events(fallback.SWITCH_EVENT)) == 1
    assert _stop_reason(world) == f"stage_failed:g1:{class_fallback.EXHAUSTED}"


def test_a_pre_start_move_uses_the_bound_before_a_timeout(tmp_path):
    world = _world(tmp_path, fallbacks=1, infra=0)
    world.to_route_started()
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    _to_next_route(world)
    _timeout(world)
    world.tick_until(lambda: world.status() == "failed")
    assert len(_attempts(world)) == 2
    assert [s["reason"] for s in world.events(fallback.SWITCH_EVENT)] == ["prestart_no_spend"]
