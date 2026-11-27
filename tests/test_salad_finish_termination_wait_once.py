"""A deferred Salad finish runs the provider termination wait once per attempt (fakes only).

A finish whose reallocated route is not yet confirmed terminated polls the provider, bounded by
``termination_confirm_seconds``. When that finish then defers (here: the guardian has not
reached ``stopped``), the later passes re-read the route instead of blocking the pass thread for
another full wait and spending more observes from the route's effect budget. What counts as
confirmed termination, and how it is reported, is unchanged.
"""

from __future__ import annotations

import json

from test_campaign_salad_executor import FINAL, RECEIPT, WORKER, Harness, item
from test_salad_termination_confirm import PollingControl, _at, _effect, _read

from tessera_control import campaign_salad_executor as module

# A passed receipt whose child could not confirm the stop itself: nothing is imported, the
# outcome waits for the guardian's final, and the route's termination is read from control.
PASSED_UNCONFIRMED = dict(RECEIPT, stop_confirmed=False)


def _harness(tmp_path, control, *, seconds=600):
    def runner(config_path, receipt_dir, timeout):
        control.ran()
        return dict(PASSED_UNCONFIRMED)

    h = Harness(tmp_path, control, receipt=dict(PASSED_UNCONFIRMED), runner=runner)
    clock = {"now": 0.0}
    h.executor.config = h.executor.config.model_copy(
        update={"termination_confirm_seconds": seconds, "termination_poll_seconds": 15}
    )
    h.executor.clock = lambda: clock["now"]

    def sleep(value):
        h.sleeps.append(value)
        clock["now"] += value

    h.executor.sleep = sleep
    h.clock = clock
    return h


def _receipt_on_disk(tmp_path):
    receipts = tmp_path / "work" / "route_1" / "receipts"
    receipts.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = receipts / module.RECEIPT_NAME
    target.write_text(json.dumps(PASSED_UNCONFIRMED))
    target.chmod(0o600)


def _guardian(control, finals):
    def read(route_id):
        control._c("guardian_final", route_id)
        return finals[0]

    control.guardian_final = read


def test_a_deferred_finish_waits_once_then_re_reads_the_route(tmp_path):
    control = PollingControl([])
    h = _harness(tmp_path, control)
    finals = [dict(FINAL, status="armed")]
    _guardian(control, finals)

    control.items = [item("route_started")]
    first = h.executor.run_once()
    waited = control.observes
    # One full, bounded wait: 600 s at 15 s polls (never past the deadline).
    assert waited == 40 and sum(s for s in h.sleeps if s == 15) == 600
    assert ("finish_deferred", "guardian_final") in [(a["action"], a["detail"]) for a in first]
    assert ("termination_wait_ended", f"polls={waited}") in [
        (a["action"], a["detail"]) for a in first
    ]
    assert "record_outcome" not in h.names()

    # The next passes resume the executing attempt from its receipt on disk: the finish defers
    # again (the guardian is still armed) but never repeats the wait or loops on observe.
    _receipt_on_disk(tmp_path)
    control.items = [item("executing")]
    before = h.clock["now"]
    for _ in range(3):
        actions = h.executor.run_once()
        assert ("finish_deferred", "guardian_final") in [
            (a["action"], a["detail"]) for a in actions
        ]
        assert not any(a["action"].startswith("termination_wait") for a in actions)
    assert control.observes == waited
    # Only the bounded guardian re-reads slept; no termination poll ran.
    config = h.executor.config
    assert h.clock["now"] - before == 3 * config.guardian_retry_seconds * config.guardian_retries
    assert "record_outcome" not in h.names() and "report_exited" not in h.names()

    # The provider's own read of the stopped group reaches the route later (another observe
    # path): the finish reports it exactly as before and records the outcome.
    observe = _effect("observe", 900)
    control.state["effects"].append(observe)
    control.state["receipts"].append(_read(observe, 901, stopped=True))
    finals[0] = dict(FINAL)
    actions = h.executor.run_once()
    assert control.observes == waited
    exits = [c for c in control.calls if c[0] == "report_exited"]
    operation = item("executing")["operation_id"]
    assert exits == [("report_exited", "lease_1", 3, operation, "provider-group-1", _at(901))]
    assert ("termination_confirmed_reported", "") in [(a["action"], a["detail"]) for a in actions]
    assert h.names().count("record_outcome") == 1
    outcome = [c for c in control.calls if c[0] == "record_outcome"][0][2]
    assert outcome["guardian_final"] == FINAL
    # The attempt's finish state is gone with its outcome.
    assert "att_1" not in h.executor._termination_waited
    assert "att_1" not in h.executor._finish_deferred


def test_a_confirmation_during_the_one_wait_is_recorded_as_before(tmp_path):
    running = dict(stopped=False, allocation="alloc-b")
    control = PollingControl([running, dict(stopped=True)])
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    actions = h.executor.run_once()
    assert control.observes == 2
    assert ("termination_confirmed", "polls=2") in [(a["action"], a["detail"]) for a in actions]
    assert [c[0] for c in control.calls].count("report_exited") == 1
    assert h.names().count("record_outcome") == 1


def test_a_new_process_may_wait_once_more(tmp_path):
    """The marker is in memory only: after a restart one more bounded wait is acceptable."""
    control = PollingControl([])
    h = _harness(tmp_path, control, seconds=60)
    _guardian(control, [dict(FINAL, status="armed")])
    control.items = [item("route_started")]
    h.executor.run_once()
    assert control.observes == 4
    _receipt_on_disk(tmp_path)
    control.items = [item("executing")]
    h.executor.run_once()
    assert control.observes == 4

    old = h.executor
    restarted = module.SaladCampaignExecutor(
        old.config,
        old.profile,
        control,
        worker_principal_id=WORKER,
        route_runner=old.route_runner,
        importer=old.importer,
        sleep=old.sleep,
        clock=old.clock,
    )
    restarted.run_once()
    assert control.observes == 8
    restarted.run_once()
    assert control.observes == 8
