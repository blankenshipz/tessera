"""A Salad live check that exits with no receipt and no effect ends the attempt unclaimed.

Real stores and runner (``SaladWorld``) for the control side; the executor fakes for the
report. "No effect" is proven only from control's records: no create/start claim and no
route effect. Anything else keeps today's wait-then-unknown path.
"""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_salad_executor import RECEIPT, FakeControl, Harness, item

from tessera_control.campaign_salad_commands import LIVE_CHECK_NO_RECEIPT
from tessera_control.campaign_salad_executor import (
    RECEIPT_NAME,
    RouteExitedWithoutReceipt,
    RouteTimeout,
    StoreControl,
)
from tessera_control.store import Conflict, StoreError


def _money(world):
    campaign = world.row(
        "SELECT held_cents, settled_cents, budget_cap_cents FROM campaigns WHERE id=?",
        world.campaign_id,
    )
    limits = world.row("SELECT held_cents, settled_cents FROM execution_project_limits")
    return dict(campaign), dict(limits)


def _executing_without_receipt(world):
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    return row


def _report(world, row):
    # The real composition the executor uses.
    StoreControl(world.env.state, world.worker).record_no_receipt(
        row["attempt_id"], row["route_id"]
    )


def test_no_receipt_and_no_effect_ends_unclaimed_with_the_hold_released(tmp_path):
    world = SaladWorld(tmp_path)
    row = _executing_without_receipt(world)
    (campaign, limits) = _money(world)
    assert campaign["held_cents"] > 0 and campaign["settled_cents"] == 0
    assert limits["held_cents"] > 0
    _report(world, row)
    assert world.salad()["waiting_reason"] == LIVE_CHECK_NO_RECEIPT
    assert world.events("campaign.salad_no_receipt")[-1]["reason"] == LIVE_CHECK_NO_RECEIPT
    world.tick_until(lambda: world.status() in {"failed", "stopped"})
    attempt = world.attempt()
    assert attempt["status"] == "failed_transient"
    assert attempt["failure_class"] == "t1_salad_unclaimed"
    assert attempt["failure_detail"] == LIVE_CHECK_NO_RECEIPT
    assert attempt["settled_cents"] == 0
    assert world.phase() == "unclaimed"
    assert world.route_state()["desired"] == "stop"
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    assert world.events("execution.grant_reconciled_unclaimed")[-1]["proof"] == (
        "no_create_or_start_claim"
    )
    # Holds released exactly as on other unclaimed ends; nothing is charged or settled.
    (after, after_limits) = _money(world)
    assert after == {**campaign, "held_cents": 0}
    assert after_limits == {"held_cents": 0, "settled_cents": limits["settled_cents"]}


@pytest.mark.parametrize("effect", ["claim", "route_effect"])
def test_a_possible_effect_keeps_the_attempt_waiting(tmp_path, effect):
    world = SaladWorld(tmp_path)
    row = _executing_without_receipt(world)
    if effect == "claim":
        world.claim("create")
    else:
        state = world.route_state()
        state["effects"] = [*state["effects"], {"action": "create"}]
        state["revision"] += 1
        world._append("remote_execution.state", state, state["revision"])
    _report(world, row)
    money = _money(world)
    world.tick(6)
    attempt = world.attempt()
    assert world.phase() == "executing" and attempt["status"] == "running"
    assert attempt["settled_cents"] is None and attempt["failure_class"] is None
    assert world.route_state()["desired"] != "stop"
    assert _money(world) == money
    assert world.status() == "running"


def test_without_the_report_the_route_keeps_waiting(tmp_path):
    world = SaladWorld(tmp_path)
    _executing_without_receipt(world)
    money = _money(world)
    world.tick(4)
    assert world.phase() == "executing" and world.attempt()["status"] == "running"
    assert _money(world) == money


def test_the_report_is_refused_outside_executing_or_for_another_route(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    row = world.salad()
    control = StoreControl(world.env.state, world.worker)
    with pytest.raises(Conflict, match="no executing route"):
        control.record_no_receipt(row["attempt_id"], row["route_id"])
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    with pytest.raises(Conflict, match="did not start"):
        control.record_no_receipt(row["attempt_id"], "route_other")
    with pytest.raises(StoreError):
        world.env.store.record_salad_no_receipt(
            world.worker,
            row["attempt_id"],
            world.key("bad"),
            {"route_id": row["route_id"], "reason": "x"},
        )
    assert world.salad()["waiting_reason"] is None


# -- executor -------------------------------------------------------------------------------


class ReportingControl(FakeControl):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.report_error = None

    def record_no_receipt(self, attempt_id, route_id):
        self._c("record_no_receipt", attempt_id, route_id)
        if self.report_error:
            raise self.report_error


def test_the_executor_reports_a_live_check_that_left_no_receipt(tmp_path):
    h = Harness(tmp_path, ReportingControl(), receipt=None)
    h.control.items = [item("route_started")]
    actions = [a["action"] for a in h.executor.run_once()]
    assert actions[-2:] == ["no_receipt", "no_receipt_reported"]
    assert ("record_no_receipt", "att_1", "route_1") in h.control.calls
    assert "record_outcome" not in h.names()


def test_a_failed_report_is_resent_on_the_next_pass(tmp_path):
    control = ReportingControl()
    control.report_error = RuntimeError("control /private down")
    h = Harness(tmp_path, control, receipt=None)
    h.control.items = [item("route_started")]
    assert ("no_receipt_report_failed", "RuntimeError") in [
        (a["action"], a["detail"]) for a in h.executor.run_once()
    ]
    control.report_error = None
    h.control.items = [item("executing")]
    assert "no_receipt_reported" in [a["action"] for a in h.executor.run_once()]
    assert h.names().count("record_no_receipt") == 2
    # Reported once: later passes do not repeat it, and the route never re-runs.
    h.executor.run_once()
    assert h.names().count("record_no_receipt") == 2 and len(h.runs) == 1


def test_a_timeout_or_a_restart_never_reports_no_receipt(tmp_path):
    def runner(config_path, receipt_dir, timeout):
        raise RouteTimeout

    h = Harness(tmp_path, ReportingControl(), runner=runner)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    # After a restart nothing shows the live check exited on its own: no report while the
    # route has a create effect (a route with none is reported, ADR 0205).
    h2 = Harness(tmp_path / "b", ReportingControl())
    h2.control.state = dict(h2.control.state, effects=[{"id": "e1", "action": "create"}])
    h2.control.items = [item("executing")]
    h2.executor.run_once()
    assert "record_no_receipt" not in h.names() + h2.names()


def test_the_real_exception_path_reports_the_terminal_no_receipt_outcome(tmp_path):
    """``subprocess_route_runner`` raises instead of returning None: the executor must still
    queue and send the terminal report (and carry the status:reason note)."""

    def runner(config_path, receipt_dir, timeout):
        raise RouteExitedWithoutReceipt("inconclusive:source_pin_malformed")

    h = Harness(tmp_path, ReportingControl(), runner=runner)
    h.control.items = [item("route_started")]
    actions = [(a["action"], a["detail"]) for a in h.executor.run_once()]
    assert actions[-2:] == [
        ("no_receipt", "inconclusive:source_pin_malformed"),
        ("no_receipt_reported", ""),
    ]
    assert ("record_no_receipt", "att_1", "route_1") in h.control.calls
    assert "record_outcome" not in h.names()
    assert "att_1" not in h.executor.no_receipt  # delivered, so not re-sent


def test_a_failed_report_after_the_real_exception_is_resent_on_the_next_pass(tmp_path):
    def runner(config_path, receipt_dir, timeout):
        raise RouteExitedWithoutReceipt("exited_without_receipt")

    control = ReportingControl()
    control.report_error = RuntimeError("control down")
    h = Harness(tmp_path, control, runner=runner)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert "att_1" in h.executor.no_receipt
    control.report_error = None
    h.control.items = [item("executing")]
    h.executor.run_once()
    assert h.names().count("record_no_receipt") == 2
    assert "att_1" not in h.executor.no_receipt


def test_a_late_receipt_after_the_no_receipt_report_is_not_reported_again(tmp_path):
    def runner(config_path, receipt_dir, timeout):
        raise RouteExitedWithoutReceipt("exited_without_receipt")

    h = Harness(tmp_path, ReportingControl(), runner=runner)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert h.names().count("record_no_receipt") == 1
    # The receipt shows up on disk afterwards (a slow exit): the normal finish path reads it;
    # it never turns into a second no-receipt report and never crashes the pass.
    receipts = h.executor._route_dir("route_1") / "receipts"
    target = receipts / RECEIPT_NAME
    target.write_text(json.dumps(RECEIPT))
    target.chmod(0o600)
    h.control.items = [item("executing")]
    h.executor.run_once()
    assert h.names().count("record_no_receipt") == 1
    assert "record_outcome" in h.names()
