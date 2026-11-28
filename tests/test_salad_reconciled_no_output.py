"""ADR 0212: a guardian-reconciled, output-less claimed attempt resolves as retryable infra.

Fakes only (the Salad campaign world, its guardian reader and executor doubles; no network).
The field shape, on synthetic ids: a claimed create whose custody had begun (inputs served and
one output requested, which control sees as ``custody_work_begun``), whose route never confirmed
termination by its own provider receipts, and whose guardian final reads ``reconciled`` by the
safety service's provider-confirmed close with ``allocation_uncertain`` set and no output kept.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control import campaign_report, campaign_runner, campaign_salad
from tessera_control.campaign_infra_retry import (
    INFRA_SALAD_RECONCILED_NO_OUTPUT,
    PLACEMENT_RETRY_CLASSES,
    RECONCILED_NO_OUTPUT_REASON,
    is_infra_class,
)
from tessera_control.campaign_models import FAILURE_CLASSES
from tessera_control.campaign_salad import ROUTE_UNCONFIRMED
from tessera_control.salad_guardian import RECONCILE_OPERATOR, RECONCILE_PROVIDER
from tessera_control.store import canonical_digest

STAMP = datetime.now(UTC)
FINAL = dict(
    status="reconciled",
    reason=RECONCILE_PROVIDER,
    orphan=False,
    allocation_uncertain=True,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.2",
    actual_billed_cost_cents=None,
    observed_at=STAMP.isoformat(),
)
# The executor's receipt of a run whose output never came back after the allocation changed.
RECEIPT = {
    "schema": "tessera.salad.live-check.v1",
    "status": "inconclusive",
    "reason": "execution_or_output_unresolved",
    "stop_confirmed": False,
    "custody_work_begun": True,
}


def _world(tmp_path, *, infra: int = 1) -> SaladWorld:
    stage = lambda env: salad_stage(env, retry={"max_attempts": 1, "infra_retries": infra})  # noqa: E731
    world = SaladWorld(tmp_path, stages=lambda env: [stage(env)])
    world.to_route_started()
    return world


def _ran_uncertain(
    world: SaladWorld,
    final: dict | None = None,
    *,
    receipt: dict | None = None,
    upload: bool = False,
) -> None:
    """Executor double: create and start claimed, custody begun, no route termination, the
    guardian's reconciled final recorded (and optionally an uploaded output)."""
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
            live_receipt=receipt or RECEIPT,
            provider_id="provider-group-1",
            guardian_final=FINAL if final is None else final,
        ),
    )


def _provider_read(world: SaladWorld, *, state: str, observed: datetime) -> None:
    """A provider readback on the route (not a termination confirmation)."""
    route = world.route_state()
    route["receipts"] = [
        *route.get("receipts", []),
        {
            "provenance": "provider",
            "state": state,
            "provider_id": "provider-group-1",
            "allocation_id": "allocation-2" if state == "running" else None,
            "observed_at": observed.isoformat(),
        },
    ]
    route["revision"] += 1
    world._append("remote_execution.state", route, route["revision"])


def _attempts(world: SaladWorld) -> list:
    return world.env.query("SELECT * FROM campaign_stage_attempts ORDER BY attempt")


def _first(world: SaladWorld):
    return world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")


def _retried_at_estimate(world: SaladWorld):
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        INFRA_SALAD_RECONCILED_NO_OUTPUT,
    )
    assert RECONCILED_NO_OUTPUT_REASON in first["failure_detail"]
    assert first["salad_verdict"] == "failed" and first["manifest_id"] is None
    # The guardian's estimate (3.2 cents) rounded up to the next cent; provisional basis.
    assert (first["settled_cents"], first["settlement_basis"]) == (4, "guardian_bounded_hold")
    assert _attempts(world)[1]["attempt"] == 2
    return first


def _held_unknown(world: SaladWorld) -> None:
    world.tick_until(lambda: world.status() == "blocked")
    world.tick(2)
    attempt = _first(world)
    assert (attempt["status"], attempt["failure_class"]) == ("unknown", "observed_unknown")
    assert attempt["settled_cents"] is None
    salad = world.salad(attempt=1)
    assert (salad["phase"], salad["waiting_reason"]) == ("unknown", ROUTE_UNCONFIRMED)
    assert len(_attempts(world)) == 1
    assert world.status() == "blocked"
    assert world.events("campaign.salad_reconciled_no_output") == []


# -- the class ------------------------------------------------------------------------------


def test_the_class_is_infrastructure_known_to_the_runner_the_report_and_the_retry():
    assert INFRA_SALAD_RECONCILED_NO_OUTPUT in FAILURE_CLASSES
    assert INFRA_SALAD_RECONCILED_NO_OUTPUT in campaign_runner._FAILURE_CLASSES
    assert INFRA_SALAD_RECONCILED_NO_OUTPUT in campaign_report._FAILURES
    assert INFRA_SALAD_RECONCILED_NO_OUTPUT in PLACEMENT_RETRY_CLASSES
    assert is_infra_class(INFRA_SALAD_RECONCILED_NO_OUTPUT)


def test_a_reconciled_output_less_attempt_retries_and_settles_at_the_estimate(tmp_path):
    world = _world(tmp_path)
    _ran_uncertain(world)
    _retried_at_estimate(world)
    assert world.status() == "running"
    # Never blocked, never a person's settlement.
    assert world.events("campaign.blocked") == []


def test_an_older_live_readback_is_superseded_by_the_guardians_close(tmp_path):
    world = _world(tmp_path)
    _provider_read(world, state="running", observed=STAMP - timedelta(minutes=5))
    _ran_uncertain(world)
    _retried_at_estimate(world)


def test_with_no_infra_retry_left_it_fails_under_its_own_class(tmp_path):
    world = _world(tmp_path, infra=0)
    _ran_uncertain(world)
    world.tick_until(lambda: world.status() == "failed")
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_RECONCILED_NO_OUTPUT,
    )
    assert (first["settled_cents"], first["settlement_basis"]) == (4, "guardian_bounded_hold")
    assert len(_attempts(world)) == 1


# -- fail closed: today's unknown and block stand ----------------------------------------------


@pytest.mark.parametrize(
    "final",
    [
        pytest.param(dict(FINAL, reason=RECONCILE_OPERATOR), id="operator_attestation"),
        pytest.param(dict(FINAL, status="unknown", reason="running"), id="frame_not_closed"),
        pytest.param(dict(FINAL, status="armed", reason="armed"), id="frame_armed"),
        pytest.param(dict(FINAL, estimated_exposure_cents=None), id="no_cost_estimate"),
        pytest.param(dict(FINAL, estimated_exposure_cents="9999"), id="estimate_above_hold"),
        pytest.param(dict(FINAL, allocation_uncertain=False), id="allocation_certain"),
        pytest.param(dict(FINAL, orphan=True), id="orphan"),
        pytest.param(dict(FINAL, actual_billed_cost_cents=5), id="billed_cost"),
        pytest.param(dict(FINAL, retained_before_move=True), id="output_retained_by_guardian"),
    ],
)
def test_a_guardian_final_short_of_the_rule_keeps_the_attempt_unknown(tmp_path, final):
    world = _world(tmp_path)
    _ran_uncertain(world, final)
    _held_unknown(world)


def test_no_guardian_final_keeps_the_attempt_unknown(tmp_path):
    world = _world(tmp_path)
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(route_id=row["route_id"], live_receipt=RECEIPT, provider_id="provider-group-1"),
    )
    _held_unknown(world)


def test_an_output_named_on_the_receipt_keeps_the_attempt_unknown(tmp_path):
    world = _world(tmp_path)
    output = {"name": "output.json", "sha256": "a" * 64, "bytes": 3}
    _ran_uncertain(world, receipt=dict(RECEIPT, outputs=[output]))
    _held_unknown(world)


def test_an_output_retained_at_the_cancel_keeps_the_attempt_unknown(tmp_path):
    world = _world(tmp_path)
    _ran_uncertain(world, receipt=dict(RECEIPT, control_cancel={"output_retained": True}))
    _held_unknown(world)


def test_an_uploaded_output_keeps_the_attempt_unknown(tmp_path):
    world = _world(tmp_path)
    _ran_uncertain(world, upload=True)
    _held_unknown(world)


def test_a_provider_read_listing_a_live_instance_keeps_the_attempt_unknown(tmp_path):
    world = _world(tmp_path)
    _provider_read(world, state="running", observed=STAMP + timedelta(minutes=1))
    _ran_uncertain(world)
    _held_unknown(world)


# -- an already-blocked campaign resumes on the next pass --------------------------------------


def _blocked_before_the_rule(world: SaladWorld, monkeypatch) -> None:
    """The state the runner left before ADR 0212: unknown attempt, stage blocked, campaign
    blocked. The rule is switched off only while it gets there."""
    with monkeypatch.context() as patch:
        patch.setattr(campaign_salad, "reconciled_no_output_refusal", lambda *a: "pre_rule")
        _held_unknown(world)
    stage = world.row("SELECT status FROM campaign_stages WHERE campaign_id=?", world.campaign_id)
    assert stage["status"] == "blocked_unknown"


def test_a_campaign_blocked_on_such_an_attempt_resumes_without_a_person(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _ran_uncertain(world)
    _blocked_before_the_rule(world, monkeypatch)

    world.tick_until(lambda: world.status() == "running")
    (event,) = world.events("campaign.salad_reconciled_no_output")
    assert event["reason"] == RECONCILED_NO_OUTPUT_REASON
    assert event["failure_class"] == INFRA_SALAD_RECONCILED_NO_OUTPUT
    assert event["attempt"] == 1
    assert world.events("campaign.unblocked") != []
    first = _retried_at_estimate(world)
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", first["grant_id"])
    assert grant["settlement_cents"] != 0
    assert world.status() == "running"


def test_a_blocked_attempt_that_still_fails_the_rule_stays_blocked(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _ran_uncertain(world, dict(FINAL, reason=RECONCILE_OPERATOR))
    _blocked_before_the_rule(world, monkeypatch)
    world.tick(4)
    assert world.status() == "blocked"
    assert _first(world)["status"] == "unknown"
    assert world.events("campaign.salad_reconciled_no_output") == []


# -- the human grant reconcile is unchanged --------------------------------------------------


def test_the_human_grant_reconcile_still_settles_a_held_attempt(tmp_path):
    from tessera_control.execution import ExecutionStore

    world = _world(tmp_path)
    _ran_uncertain(world, dict(FINAL, reason=RECONCILE_OPERATOR))
    _held_unknown(world)
    world.observe_exit()
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", _first(world)["grant_id"])
    body = {
        "expected_revision": grant["revision"],
        "settlement_cents": 11,
        "provider_terminated": True,
    }
    status, _ = ExecutionStore(world.env.state).reconcile_execution_grant(
        world.human, grant["id"], "human-reconcile", canonical_digest(body), body
    )
    assert status == 200
    world.tick_until(lambda: _first(world)["settled_cents"] is not None)
    first = _first(world)
    assert first["status"] == "failed_permanent"
    assert first["settled_cents"] == 11
    assert world.events("campaign.salad_reconciled_no_output") == []
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert "observed_unknown" in (stop["stop_reason"] or "")


def test_the_evidence_digest_binds_the_guardian_final(tmp_path):
    world = _world(tmp_path)
    _ran_uncertain(world)
    _retried_at_estimate(world)
    first = _first(world)
    salad = world.salad(attempt=1)
    assert first["salad_evidence_digest"] == salad["salad_evidence_digest"]
    assert json.loads(salad["guardian_final_json"])["reason"] == RECONCILE_PROVIDER


def test_without_a_worker_exit_the_grant_is_reconciled_on_the_guardians_close(tmp_path):
    world = _world(tmp_path)
    _ran_uncertain(world)
    first = _retried_at_estimate(world)
    (event,) = world.events("execution.reconciled_no_output_grant_reconciled")
    assert event["grant_id"] == first["grant_id"]
    assert (event["settlement_cents"], event["settlement_is_estimate"]) == (4, True)
    assert event["termination_evidence"] == "guardian_provider_confirmed_close"
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", first["grant_id"])
    assert (grant["status"], grant["settlement_cents"]) == ("reconciled", 4)


def test_with_a_worker_confirmed_exit_the_ordinary_reconcile_step_settles_it(tmp_path):
    world = _world(tmp_path)
    _ran_uncertain(world)
    world.observe_exit()
    first = _retried_at_estimate(world)
    assert world.events("execution.reconciled_no_output_grant_reconciled") == []
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", first["grant_id"])
    assert (grant["status"], grant["settlement_cents"]) == ("reconciled", 4)


# -- round 2: a refusal never locks the attempt out; a stale guardian final is refreshed ------

HELD = campaign_salad.RECONCILED_NO_OUTPUT_HELD_EVENT


def _held(world: SaladWorld, refusal: str) -> list:
    return [e for e in world.events(HELD) if e["refusal"] == refusal]


def _still_keyed(world: SaladWorld) -> None:
    salad = world.salad(attempt=1)
    assert (salad["phase"], salad["waiting_reason"]) == ("unknown", ROUTE_UNCONFIRMED)
    assert world.status() == "blocked"


def test_a_live_readback_then_a_clean_one_resolves_on_the_next_pass(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _ran_uncertain(world)
    _blocked_before_the_rule(world, monkeypatch)
    _provider_read(world, state="running", observed=STAMP + timedelta(minutes=1))
    world.tick(4)
    _still_keyed(world)
    assert len(_held(world, "provider_lists_live_instance")) == 1
    _provider_read(world, state="stopped", observed=STAMP + timedelta(minutes=2))
    world.tick_until(lambda: world.status() == "running")
    _retried_at_estimate(world)


def test_a_missing_route_state_then_present_resolves(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _ran_uncertain(world)
    _blocked_before_the_rule(world, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(campaign_salad.SaladStageDriver, "route_state", lambda self, route_id: None)
        world.tick(4)
        _still_keyed(world)
    assert len(_held(world, "route_state_missing")) == 1
    world.tick_until(lambda: world.status() == "running")
    _retried_at_estimate(world)


def test_a_temporary_refusal_at_reconcile_keeps_the_reason_and_resolves_later(
    tmp_path, monkeypatch
):
    """The lockout: the reconcile step's re-check refuses once (a provider read still listing an
    instance); the attempt must stay keyed for the blocked re-read and resolve after it clears."""
    world = _world(tmp_path)
    _ran_uncertain(world)
    real = campaign_salad.reconciled_no_output_refusal
    refuse = {"on": False}
    monkeypatch.setattr(
        campaign_salad,
        "reconciled_no_output_refusal",
        lambda *a: "provider_lists_live_instance" if refuse["on"] else real(*a),
    )
    world.tick_until(lambda: _first(world)["status"] in ("resolved", "measured"))
    refuse["on"] = True
    world.tick_until(lambda: world.status() == "blocked")
    world.tick(3)
    _still_keyed(world)
    assert _first(world)["failure_detail"] == ROUTE_UNCONFIRMED
    assert len(_held(world, "reconcile_provider_lists_live_instance")) == 1
    refuse["on"] = False
    world.tick_until(lambda: world.status() == "running")
    _retried_at_estimate(world)


def _stale_world(tmp_path, monkeypatch) -> SaladWorld:
    monkeypatch.setattr(campaign_salad, "GUARDIAN_FINAL_REFRESH_SECONDS", 0)
    world = _world(tmp_path)
    _ran_uncertain(world, dict(FINAL, status="armed", reason="armed"))
    _held_unknown(world)
    return world


def test_a_stale_guardian_final_is_refreshed_once_the_guardian_closes(tmp_path, monkeypatch):
    world = _stale_world(tmp_path, monkeypatch)
    reads: list[str] = []
    world.guardian.read_final = lambda reservation: (
        reads.append(reservation) or dict(FINAL, status="armed", reason="armed")
    )
    world.tick(3)
    _still_keyed(world)
    assert reads and world.events(campaign_salad.GUARDIAN_FINAL_REFRESHED_EVENT) == []
    closed = dict(FINAL, observed_at=datetime.now(UTC).isoformat())
    world.guardian.read_final = lambda reservation: dict(closed)
    world.tick_until(lambda: world.status() == "running")
    (refreshed,) = world.events(campaign_salad.GUARDIAN_FINAL_REFRESHED_EVENT)
    assert (refreshed["previous_status"], refreshed["status"]) == ("armed", "reconciled")
    first = _retried_at_estimate(world)
    stored = json.loads(world.salad(attempt=1)["guardian_final_json"])
    assert (stored["status"], stored["reason"]) == ("reconciled", RECONCILE_PROVIDER)
    assert first["failure_class"] == INFRA_SALAD_RECONCILED_NO_OUTPUT


def test_an_unreadable_guardian_holds_with_one_event(tmp_path, monkeypatch):
    world = _stale_world(tmp_path, monkeypatch)
    world.guardian.read_final = lambda reservation: None
    world.tick(6)
    _still_keyed(world)
    assert len(_held(world, "guardian_final_unreadable")) == 1
    assert world.events(campaign_salad.GUARDIAN_FINAL_REFRESHED_EVENT) == []


def test_a_terminal_stored_final_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign_salad, "GUARDIAN_FINAL_REFRESH_SECONDS", 0)
    world = _world(tmp_path)
    _ran_uncertain(world, dict(FINAL, reason=RECONCILE_OPERATOR))
    _held_unknown(world)
    reads: list[str] = []
    world.guardian.read_final = lambda reservation: reads.append(reservation) or dict(FINAL)
    world.tick(4)
    _still_keyed(world)
    assert reads == []
    stored = json.loads(world.salad(attempt=1)["guardian_final_json"])
    assert stored["reason"] == RECONCILE_OPERATOR


def test_a_retained_output_stays_held_across_many_passes_with_one_event(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _ran_uncertain(world, receipt=dict(RECEIPT, control_cancel={"output_retained": True}))
    _blocked_before_the_rule(world, monkeypatch)
    world.tick(10)
    _still_keyed(world)
    assert len(_held(world, "output_retained")) == 1
    assert world.events("campaign.salad_reconciled_no_output") == []
    assert _first(world)["status"] == "unknown" and _first(world)["settled_cents"] is None


def test_an_unexpected_error_in_the_re_read_is_recorded_once_and_holds(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _ran_uncertain(world)
    _blocked_before_the_rule(world, monkeypatch)

    def boom(*args):
        raise RuntimeError("synthetic")

    with monkeypatch.context() as patch:
        patch.setattr(campaign_salad.SaladStageDriver, "_resume_reconciled_no_output", boom)
        world.tick(5)
        _still_keyed(world)
    (error,) = world.events(campaign_salad.RECONCILED_NO_OUTPUT_ERROR_EVENT)
    assert error["error_class"] == "RuntimeError"
    world.tick_until(lambda: world.status() == "running")
    _retried_at_estimate(world)


def test_the_https_reader_projects_the_final_and_fails_closed():
    from tessera_control.guardian_record_reader import HttpsGuardianRecordReader

    class Client:
        reply: object = None

        def _call(self, body):
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    client = Client()
    reader = HttpsGuardianRecordReader(client)
    client.reply = dict(FINAL, allocation_uncertain=True, stop_cause=None)
    final = reader.read_final("r" * 64)
    assert (final["status"], final["reason"], final["allocation_uncertain"]) == (
        "reconciled",
        RECONCILE_PROVIDER,
        True,
    )
    for bad in (RuntimeError("down"), None, {"status": "bogus"}, "text"):
        client.reply = bad
        assert reader.read_final("r" * 64) is None
