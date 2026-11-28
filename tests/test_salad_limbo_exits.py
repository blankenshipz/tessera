"""ADR 0216: a charge wait has a deadline, a person may reconcile an unknown Salad attempt as
retryable infrastructure at the guardian's bound, zero-spend auto-reconcile limits scale with the
campaign, and a cancel finishes past an unknown attempt at its hold.

Fakes only (the Salad campaign world, its guardian reader and executor doubles; no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from salad_campaign_fixture import HOLD, SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control import campaign_runner
from tessera_control.campaign_infra_retry import (
    INFRA_RECONCILED_RETRYABLE,
    RETRYABLE_RECONCILE_EVENT,
    is_infra_class,
)
from tessera_control.campaign_models import FAILURE_CLASSES
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.campaign_salad import ROUTE_UNCONFIRMED
from tessera_control.salad_guardian import RECONCILE_OPERATOR
from tessera_control.salad_guardian_bound import (
    CHARGE_DEADLINE_SECONDS,
    CHARGE_DEADLINE_SETTLED_EVENT,
    CHARGE_OVERDUE,
    CHARGE_OVERDUE_RESUMED_EVENT,
)
from tessera_control.salad_retryable_reconcile import SaladRetryableReconcileStore
from tessera_control.store import Conflict, Forbidden, StoreError, canonical_digest

STAMP = datetime.now(UTC)
# A closed but not clean guardian final: one failed rate refresh fenced the frame.
FENCED = dict(
    status="stopped",
    reason="rate_unknown",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=True,
    rate_unknown=True,
    published_hourly_rate_usd=None,
    estimated_exposure_cents=None,
    actual_billed_cost_cents=None,
    observed_at=STAMP.isoformat(),
)
# A route that never confirmed termination, whose guardian final the ADR 0212 rule does not take
# (an operator attested the reconcile), with no output kept.
ATTESTED = dict(
    FENCED,
    status="reconciled",
    reason=RECONCILE_OPERATOR,
    budget_fenced=False,
    rate_unknown=False,
    allocation_uncertain=True,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.2",
)
STOPPED = dict(ATTESTED, status="stopped", reason="cost_cap", allocation_uncertain=False)
RECEIPT = {
    "schema": "tessera.salad.live-check.v1",
    "status": "inconclusive",
    "reason": "execution_or_output_unresolved",
    "stop_confirmed": False,
    "custody_work_begun": True,
}


def _later(world: SaladWorld, seconds: int) -> None:
    world.runner.now = lambda: datetime.now(UTC) + timedelta(seconds=seconds)


def _first(world: SaladWorld):
    return world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")


def _attempts(world: SaladWorld) -> list:
    return world.env.query("SELECT * FROM campaign_stage_attempts ORDER BY attempt")


# -- A3: the charge deadline ------------------------------------------------------------------


def _awaiting_charge(tmp_path, final: dict | None) -> SaladWorld:
    world = SaladWorld(tmp_path)
    world.to_route_started()
    world.run_to_outcome(guardian_final=final)
    world.tick_until_phase("awaiting_charge")
    return world


def test_inside_the_deadline_the_charge_wait_is_unchanged(tmp_path):
    world = _awaiting_charge(tmp_path, FENCED)
    _later(world, CHARGE_DEADLINE_SECONDS - 60)
    world.tick(3)
    assert world.status() == "running" and world.phase() == "awaiting_charge"
    assert world.events(CHARGE_DEADLINE_SETTLED_EVENT) == []
    assert world.events("campaign.blocked") == []


def test_past_the_deadline_the_guardians_bound_settles_it(tmp_path):
    world = _awaiting_charge(tmp_path, FENCED)
    _later(world, CHARGE_DEADLINE_SECONDS + 60)
    world.tick_until(lambda: world.status() == "succeeded")
    first = _first(world)
    # The whole hold, provisional: a later charge confirms it or adds the excess.
    assert (first["settled_cents"], first["settlement_basis"]) == (HOLD, "guardian_bounded_hold")
    (event,) = world.events(CHARGE_DEADLINE_SETTLED_EVENT)
    assert event["settlement_cents"] == HOLD and event["settlement_is_estimate"] is True
    assert world.events("campaign.blocked") == []
    world.record_charge(13)
    world.runner.tick()
    assert world.salad()["settlement_confirmed_at"] is not None


@pytest.mark.parametrize(
    "final",
    [
        pytest.param(None, id="no_guardian_final"),
        pytest.param(dict(FENCED, status="unknown", reason="running"), id="frame_open"),
        pytest.param(dict(FENCED, orphan=True), id="orphan"),
    ],
)
def test_past_the_deadline_without_a_bound_it_blocks_once_and_a_charge_resumes_it(tmp_path, final):
    world = _awaiting_charge(tmp_path, final)
    _later(world, CHARGE_DEADLINE_SECONDS + 60)
    world.tick_until(lambda: world.status() == "blocked")
    world.tick(3)
    (blocked,) = world.events("campaign.blocked")
    assert blocked["reason"] == "charge_overdue" and blocked["attempt"] == 1
    attempt = _first(world)
    assert (attempt["status"], attempt["failure_detail"]) == ("unknown", CHARGE_OVERDUE)
    assert attempt["settled_cents"] is None
    assert world.salad()["waiting_reason"] == CHARGE_OVERDUE
    # Recording the charge is enough: the blocked pass re-reads it and the stage resumes.
    world.record_charge(13)
    world.tick_until(lambda: world.status() == "succeeded")
    assert world.events(CHARGE_OVERDUE_RESUMED_EVENT)[0]["basis"] == "human_charge"
    first = _first(world)
    assert (first["settled_cents"], first["settlement_basis"]) == (13, "human_charge")
    assert first["status"] == "succeeded"


# -- A5: a person's retryable-infrastructure reconcile ----------------------------------------


def _held_unknown(tmp_path, final: dict, *, infra: int = 1, upload: bool = False) -> SaladWorld:
    stage = lambda env: salad_stage(env, retry={"max_attempts": 1, "infra_retries": infra})  # noqa: E731
    world = SaladWorld(tmp_path, stages=lambda env: [stage(env)])
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    if upload:
        world.upload_manifest()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=RECEIPT,
            provider_id="provider-group-1",
            guardian_final=final,
        ),
    )
    world.tick_until(lambda: world.status() == "blocked")
    assert _first(world)["status"] == "unknown"
    assert world.salad(attempt=1)["waiting_reason"] == ROUTE_UNCONFIRMED
    return world


def _reconcile_retryable(world: SaladWorld, body: dict | None = None, actor=None):
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", _first(world)["grant_id"])
    body = body or {"expected_revision": grant["revision"]}
    return SaladRetryableReconcileStore(world.env.state).reconcile_retryable(
        actor or world.human, grant["id"], world.key("retryable"), canonical_digest(body), body
    )


def test_the_class_is_infrastructure_known_to_the_runner():
    assert INFRA_RECONCILED_RETRYABLE in FAILURE_CLASSES
    assert INFRA_RECONCILED_RETRYABLE in campaign_runner._FAILURE_CLASSES
    assert is_infra_class(INFRA_RECONCILED_RETRYABLE)


@pytest.mark.parametrize("final", [ATTESTED, STOPPED], ids=["attested", "stopped"])
def test_a_retryable_reconcile_settles_at_the_hold_and_retries_the_stage(tmp_path, final):
    world = _held_unknown(tmp_path, final)
    status, grant = _reconcile_retryable(world)
    assert status == 200 and grant["settlement_cents"] == HOLD
    (event,) = world.events(RETRYABLE_RECONCILE_EVENT)
    assert event["settlement_supplied_by_caller"] is False and event["output_kept"] is False
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        INFRA_RECONCILED_RETRYABLE,
    )
    assert (first["settled_cents"], first["settlement_basis"]) == (HOLD, "human_reconcile")
    (retried,) = world.events("campaign.attempt_reconciled_retryable")
    assert retried["retried"] is True and retried["consumed_scientific_retry"] is False
    assert world.status() == "running"


def test_with_no_infra_retry_left_only_the_stage_fails(tmp_path):
    world = _held_unknown(tmp_path, STOPPED, infra=0)
    assert _reconcile_retryable(world)[0] == 200
    world.tick_until(lambda: world.status() == "failed")
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_permanent",
        INFRA_RECONCILED_RETRYABLE,
    )
    assert first["settled_cents"] == HOLD and len(_attempts(world)) == 1


def test_the_caller_can_never_name_the_amount(tmp_path):
    world = _held_unknown(tmp_path, STOPPED)
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", _first(world)["grant_id"])
    with pytest.raises(StoreError):
        _reconcile_retryable(world, {"expected_revision": grant["revision"], "settlement_cents": 1})


@pytest.mark.parametrize(
    "final",
    [
        pytest.param(dict(STOPPED, status="unknown", reason="running"), id="frame_open"),
        pytest.param(dict(STOPPED, escalation_pending=True), id="escalation"),
        pytest.param(dict(STOPPED, estimated_exposure_cents="9999"), id="estimate_above_hold"),
        pytest.param(dict(STOPPED, retained_before_move=True), id="output_retained"),
    ],
)
def test_a_retryable_reconcile_is_refused_short_of_the_bound(tmp_path, final):
    world = _held_unknown(tmp_path, final)
    with pytest.raises(Conflict):
        _reconcile_retryable(world)
    assert world.events(RETRYABLE_RECONCILE_EVENT) == []
    world.tick(2)
    assert world.status() == "blocked" and _first(world)["status"] == "unknown"


def test_a_collected_output_refuses_a_retryable_reconcile(tmp_path):
    world = _held_unknown(tmp_path, STOPPED, upload=True)
    with pytest.raises(Conflict):
        _reconcile_retryable(world)


def test_only_a_person_may_reconcile_as_retryable(tmp_path):
    world = _held_unknown(tmp_path, STOPPED)
    with pytest.raises(Forbidden):
        _reconcile_retryable(world, actor=world.worker)


def test_the_cli_posts_only_the_revision(tmp_path):
    import os

    from tessera_control import cli

    calls = []

    class Transport:
        def __init__(self, origin, header):
            pass

        def __call__(self, method, path, body, key):
            calls.append((method, path, body, key))
            return 200, {"status": "reconciled"}

    header = tmp_path / "owner"
    header.write_text("Authorization: Bearer test-token")
    os.chmod(header, 0o600)
    arguments = cli.parser().parse_args(
        [
            "campaign",
            "reconcile-retryable",
            "--grant-id",
            "grant/1",
            "--expected-revision",
            "3",
            "--owner-header",
            str(header),
            "--idempotency-key",
            "k-1",
        ]
    )
    assert cli.run_campaign(arguments, transport_factory=Transport) == 0
    assert calls == [
        (
            "POST",
            "/execution-grants/grant%2F1/reconcile-retryable-salad",
            {"expected_revision": 3},
            "k-1",
        )
    ]


# -- A6: zero-spend auto-reconcile limits scale with the campaign ------------------------------


def _limit(monkeypatch, *, per_stage: int, stages: int, used: int, stage_used: int):
    runner = CampaignRunner.__new__(CampaignRunner)
    runner.unclaimed_autoreconcile_limit = per_stage
    counts = {None: used, "s1": stage_used}
    monkeypatch.setattr(
        campaign_runner,
        "automatic_count",
        lambda connection, campaign, stage_key=None: counts[stage_key],
    )
    ctx = SimpleNamespace(spec=SimpleNamespace(stages=[object()] * stages), campaign={})
    return runner._unclaimed_limit(None, ctx, "s1")


def test_the_campaign_bound_scales_with_its_stages(monkeypatch):
    # The live shape: 21 stages, three lease lapses on different stages.
    assert _limit(monkeypatch, per_stage=2, stages=21, used=3, stage_used=0) == (None, 3, 42)


def test_one_stage_keeps_its_own_bound(monkeypatch):
    reason, used, limit = _limit(monkeypatch, per_stage=2, stages=21, used=3, stage_used=2)
    assert reason is not None and "by this stage" in reason and (used, limit) == (3, 42)


def test_a_one_stage_campaign_keeps_the_old_bound_and_zero_still_disables(monkeypatch):
    reason, _, limit = _limit(monkeypatch, per_stage=2, stages=1, used=2, stage_used=2)
    assert (reason, limit) == ("not auto-reconciled: 2 of 2 automatic reconciles used", 2)
    reason, _, _ = _limit(monkeypatch, per_stage=0, stages=21, used=0, stage_used=0)
    assert reason == "not auto-reconciled: automatic unclaimed reconcile is disabled"


# -- A7: a cancel finishes past an unknown attempt at its hold ---------------------------------


def test_a_cancel_finishes_past_an_unknown_attempt_at_its_hold(tmp_path):
    world = _held_unknown(tmp_path, STOPPED)
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    attempt = _first(world)
    # Reduce-only: nothing settled or released; the grant is revoked and the route stopped.
    assert attempt["status"] == "unknown" and attempt["settled_cents"] is None
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "cancel_requested"
    assert world.route_state()["desired"] == "stop"
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["held_cents"] == HOLD and campaign["settled_cents"] == 0
    (held,) = world.events("campaign.cancel_held_unknown")
    assert held["hold_cents"] == HOLD and held["settled"] is False
    # A person can still reconcile the grant afterwards.
    assert _reconcile_retryable(world)[0] == 200


def test_a_cancel_settles_an_unknown_attempt_a_person_already_reconciled(tmp_path):
    world = _held_unknown(tmp_path, STOPPED)
    world.cancel()
    assert _reconcile_retryable(world)[0] == 200
    world.tick_until(lambda: world.status() == "cancelled")
    attempt = _first(world)
    assert (attempt["status"], attempt["failure_class"]) == (
        "failed_permanent",
        INFRA_RECONCILED_RETRYABLE,
    )
    assert attempt["settled_cents"] == HOLD and len(_attempts(world)) == 1
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["held_cents"] == 0 and campaign["settled_cents"] == HOLD
