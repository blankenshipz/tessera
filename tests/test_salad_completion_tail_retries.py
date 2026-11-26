"""Completion tail: a finished run (output retained, provider stopped) passes and records its
outputs despite transients in cancel, stop, stop confirm, custody seal, verification, guardian
final, import or exit report. Isolated fakes only: no live Salad, no network, no database."""

import hashlib
import json
import sqlite3

import pytest
from test_campaign_salad_executor import RECEIPT, FakeControl, Harness, item
from test_salad_live_check import LocalSession

from tessera_control.campaign_salad_executor import project_guardian_final
from tessera_control.salad_live_check import (
    PRECUSTODY_REALLOCATION,
    GuardianCancelUndelivered,
    _completion_stop_cause,
    _drive,
    _write_receipt,
)
from tessera_control.salad_safety_service import ServiceUnavailable
from tessera_control.salad_server import GuardianRefused, GuardianUnavailable

LONG_ESTIMATE = "0.5119111111111111111111111111"  # exact decimal accrual: 28 places


class IncidentSession(LocalSession):
    """A running group retains its output, then (after the stop) the provider confirms the stop
    while the guardian has not yet sealed custody, with scripted transient failures."""

    def __init__(
        self,
        *,
        unsealed_polls=3,
        stop_confirm_errors=(),
        verify_errors=(),
        cancel_errors=(),
        stop_errors=(),
        guardian_stop_cause=None,
        custody_work_begun=True,
        running_polls=2,
    ):
        super().__init__()
        self.now = 0.0
        self.proof.update(
            output_retained=False,
            stop_confirmed=False,
            writers_quiescent=False,
            provider_stop_confirmed=False,
            stop_cause=None,
            custody_work_begun=custody_work_begun,
        )
        self.unsealed_polls = unsealed_polls
        self.stop_confirm_errors = list(stop_confirm_errors)
        self.verify_errors = list(verify_errors)
        self.cancel_errors = list(cancel_errors)
        self.stop_errors = list(stop_errors)
        self.guardian_stop_cause = guardian_stop_cause
        self.running_polls = running_polls
        self.cancel_calls = 0
        self.evidence_calls = 0

    def stopping(self):
        return self.cancelled

    def dispatch(self, route, action):
        if action == "stop" and self.stop_errors:
            # The preflight read failed: nothing recorded, nothing sent.
            self.actions.append("stop_preflight_failed")
            raise self.stop_errors.pop(0)
        if action == "start":
            self.record["provider_status"] = "running"
        if action == "stop":
            self.record["provider_status"] = "stopped"
        if action == "observe" and "start" in self.actions and not self.stopping():
            self.running_polls -= 1
            if self.running_polls < 0:
                self.proof["output_retained"] = True
        return super().dispatch(route, action)

    def cancel(self, route):
        self.cancel_calls += 1
        if self.cancel_errors:
            error = self.cancel_errors.pop(0)
            self.cancelled = True  # control recorded it; only the guardian call failed
            raise error
        super().cancel(route)

    def evidence(self, route):
        self.evidence_calls += 1
        if not self.stopping():
            return dict(self.proof)
        if self.stop_confirm_errors:
            raise self.stop_confirm_errors.pop(0)
        if self.unsealed_polls > 0:
            self.unsealed_polls -= 1
            return dict(
                self.proof,
                provider_stop_confirmed=True,
                stop_confirmed=False,
                writers_quiescent=False,
                stop_cause=self.guardian_stop_cause,
            )
        if self.guardian_stop_cause == "allocation_changed":
            # Custody never seals after a pre-custody reallocation.
            return dict(
                self.proof,
                provider_stop_confirmed=True,
                stop_confirmed=False,
                writers_quiescent=False,
                stop_cause=self.guardian_stop_cause,
            )
        return dict(
            self.proof,
            provider_stop_confirmed=True,
            stop_confirmed=True,
            writers_quiescent=True,
            stop_cause=self.guardian_stop_cause,
        )

    def retained_bytes(self, route):
        if self.verify_errors:
            raise self.verify_errors.pop(0)
        return self.data


def drive(session, *, max_polls=10, max_seconds=100):
    def sleep(seconds):
        session.now += seconds

    return _drive(
        session,
        route_id="exact-route",
        source_revision="a" * 40,
        max_seconds=max_seconds,
        max_polls=max_polls,
        sleep=sleep,
        clock=lambda: session.now,
    )


def guardian_status(status="stopped"):
    return dict(
        status=status,
        reason="exact_terminal_and_sealed" if status == "stopped" else "",
        orphan=False,
        allocation_uncertain=False,
        escalation_pending=False,
        budget_fenced=False,
        rate_unknown=False,
        published_hourly_rate_usd="0.014",
        estimated_exposure_cents=LONG_ESTIMATE,
    )


class FinishControl(FakeControl):
    """Guardian final read through the real projection, with scripted failures."""

    def __init__(self, finals, *, upload_errors=(), exit_errors=()):
        super().__init__()
        self.finals = list(finals)
        self.upload_errors = list(upload_errors)
        self.exit_errors = list(exit_errors)

    def guardian_final(self, route_id):
        self._c("guardian_final", route_id)
        value = self.finals.pop(0) if len(self.finals) > 1 else self.finals[0]
        if isinstance(value, Exception):
            raise value
        return project_guardian_final(value)

    def upload_outputs(self, lease_id, epoch, operation_id, directory, files):
        if self.upload_errors:
            self._c("upload_failed")
            raise self.upload_errors.pop(0)
        return super().upload_outputs(lease_id, epoch, operation_id, directory, files)

    def report_exited(self, lease_id, epoch, operation_id, provider_id, observed_at):
        if self.exit_errors:
            self._c("report_exited_failed")
            raise self.exit_errors.pop(0)
        super().report_exited(lease_id, epoch, operation_id, provider_id, observed_at)


def run_until_recorded(h, receipt, passes=6):
    """First pass runs the route (child returns ``receipt``); later passes resume from the
    receipt on disk, as a restarted or next executor pass does."""
    h.receipt = receipt
    h.control.items = [item("route_started")]
    actions = h.executor.run_once()
    receipts = h.executor._route_dir("route_1") / "receipts"
    _write_receipt(receipts, receipt)
    h.control.items = [item("executing")]
    for _ in range(passes):
        if "record_outcome" in h.names():
            break
        actions += h.executor.run_once()
    return actions


# --- the 10:01 AM ET production shape ---------------------------------------------------------


def test_execution_complete_provider_stopped_custody_unsealed_retries_until_sealed_and_records_manifest(  # noqa: E501
    tmp_path,
):
    session = IncidentSession(
        unsealed_polls=3,
        stop_confirm_errors=[TimeoutError("bounded live operation expired")],
        verify_errors=[ServiceUnavailable("timeout")],
    )
    receipt = drive(session, max_polls=10, max_seconds=1000)

    # The live check: provider stopped first, custody sealed later; the run passed.
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["guardian_stop_cause"] is None
    assert receipt["provider_stop_confirmed"] is True
    assert receipt["stop_confirmed"] is True
    assert receipt["status"] == "passed"
    assert receipt["reason"] == "verified_journey"
    assert receipt["custody_seal_waits"] == 3
    # Every transient tail error is on the receipt, sanitized.
    steps = [(e["step"], e["exception"], e["transient"]) for e in receipt["tail_errors"]]
    assert steps == [
        ("stop_confirm", "TimeoutError", True),
        ("verify", "ServiceUnavailable", True),
    ]
    assert receipt["tail_error_count"] == 2
    assert "SECRET" not in json.dumps(receipt)

    # The executor: the guardian final is first unreadable, then not yet stopped, then a stopped
    # status whose exact-decimal estimate used to be dropped as unprojectable.
    # One executor pass reads it 1 + guardian_retries (3) times: not stopped on any of them.
    control = FinishControl(
        [TimeoutError("timed out")] + [guardian_status("armed")] * 4 + [guardian_status("stopped")]
    )
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, receipt)
    names = [a["action"] for a in actions]
    assert "finish_deferred" in names
    assert actions[-1] == {
        "action": "outcome_recorded",
        "attempt_id": "att_1",
        "detail": "manifest",
    }
    assert h.names().count("record_outcome") == 1
    payload = next(c for c in control.calls if c[0] == "record_outcome")[2]
    assert payload["manifest_id"] == "manifest_1"
    assert payload["live_receipt"]["status"] == "passed"
    assert payload["guardian_final"]["status"] == "stopped"
    # Rounded up, never down, to the twelve places control records.
    assert payload["guardian_final"]["estimated_exposure_cents"] == "0.511911111112"
    assert control.uploaded == {"output.json": b'{"result": 1}'}
    # Imported and uploaded once; the marker keeps a later pass from repeating it.
    assert h.names().count("upload") == 1


def test_true_precustody_reallocation_is_never_turned_into_a_pass():
    session = IncidentSession(
        unsealed_polls=0, guardian_stop_cause="allocation_changed", custody_work_begun=False
    )
    receipt = drive(session, max_polls=10, max_seconds=1000)
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == PRECUSTODY_REALLOCATION
    assert receipt["stop_confirmed"] is False
    assert "custody_seal_waits" not in receipt
    # The stop wait ended at the provider's confirmation: one observe after the stop.
    stop_at = session.actions.index("stop")
    assert session.actions[stop_at + 1 :] == ["observe"]


def test_completed_run_whose_custody_never_seals_ends_bounded_and_unsealed():
    session = IncidentSession(unsealed_polls=10_000)
    receipt = drive(session, max_polls=10, max_seconds=100)
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == "provider_stopped_custody_unsealed"
    assert 0 < receipt["custody_seal_waits"] <= 300 / 10 + 1
    assert session.now <= 100 + 300 + 30


# --- C3: stop, stop confirm and verification retry transients --------------------------------


def test_three_observe_timeouts_during_stop_confirm_do_not_fail_a_retained_run():
    session = IncidentSession(
        unsealed_polls=0,
        stop_confirm_errors=[
            TimeoutError("bounded live operation expired"),
            ServiceUnavailable("timeout"),
            sqlite3.OperationalError("database is locked"),
        ],
    )
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt["reason"]
    assert [e["step"] for e in receipt["tail_errors"]] == ["stop_confirm"] * 3


def test_completed_run_passes_when_one_post_stop_status_read_times_out():
    session = IncidentSession(unsealed_polls=0, verify_errors=[TimeoutError("expired")])
    receipt = drive(session)
    assert receipt["status"] == "passed"
    assert receipt["tail_errors"][0]["step"] == "verify"


def test_stop_preflight_read_timeout_is_retried_before_stop_effect():
    session = IncidentSession(unsealed_polls=0, stop_errors=[TimeoutError("read timed out")])
    receipt = drive(session)
    assert receipt["status"] == "passed"
    assert session.actions.count("stop_preflight_failed") == 1
    # The stop itself is sent once.
    assert [e["action"] for e in session.record["effects"]].count("stop") == 1
    assert receipt["tail_errors"][0]["step"] == "stop"


def test_an_integrity_mismatch_in_the_tail_is_not_retried():
    session = IncidentSession(unsealed_polls=0)
    session.proof["output_sha256"] = "0" * 64
    receipt = drive(session)
    assert receipt["status"] == "inconclusive"
    verify = [e for e in receipt["tail_errors"] if e["step"] == "verify"]
    assert len(verify) == 1 and verify[0]["message"] == "output integrity"
    assert verify[0]["transient"] is False


# --- H7: a completion cancel whose guardian call fails ---------------------------------------


def test_completion_cancel_guardian_unavailable_after_control_recorded_is_retried_and_run_passes():
    failure = GuardianCancelUndelivered("guardian cancel undelivered")
    failure.__cause__ = GuardianUnavailable()
    session = IncidentSession(unsealed_polls=0, cancel_errors=[failure])
    receipt = drive(session)
    assert receipt["cancel_recorded"] is True
    assert receipt["cancel_failure"] is None
    assert receipt["guardian_cancel_failure"]["guardian_refusal"] == "unavailable"
    assert session.cancel_calls == 2
    assert receipt["status"] == "passed"
    assert receipt["tail_errors"][0]["step"] == "guardian_cancel"


# --- C4: the status read after a refused heartbeat --------------------------------------------


class RefusedAfterRetention(IncidentSession):
    """The guardian retained the output during the sleep, stopped the group and refuses the next
    heartbeat; the status read that would show retention times out twice in a row."""

    def __init__(self):
        super().__init__(unsealed_polls=0, running_polls=10_000)
        self.status_timeouts = 2
        self.beats = 0

    def keepalive(self, route):
        self.keepalives += 1
        if "start" in self.actions and self.actions.count("observe") >= 3:
            self.proof["output_retained"] = True
            raise GuardianRefused("custody_unready")

    def evidence(self, route):
        if not self.stopping() and self.proof["output_retained"] and self.status_timeouts:
            self.status_timeouts -= 1
            raise ServiceUnavailable("timeout")
        return super().evidence(route)


def test_refused_heartbeat_after_output_retained_with_status_read_timeout_still_passes():
    session = RefusedAfterRetention()
    receipt = drive(session, max_polls=20, max_seconds=1000)
    assert receipt["completion_status_read"]["outcome"] == "retained"
    assert receipt["completion_status_read"]["attempts"] == 3
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["status"] == "passed"


# --- L11: retained exactly as the deadline passes ---------------------------------------------


class RetainedAtDeadline(IncidentSession):
    def __init__(self):
        super().__init__(unsealed_polls=0, running_polls=10_000)

    def keepalive(self, route):
        self.keepalives += 1
        if self.now >= 90:
            self.proof["output_retained"] = True


def test_output_retained_as_the_deadline_passes_completes():
    session = RetainedAtDeadline()
    receipt = drive(session, max_polls=10, max_seconds=100)
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["status"] == "passed"


# --- executor: H6 / H11 / guardian final projection -------------------------------------------


def test_guardian_final_with_exact_decimal_estimate_is_projected_not_dropped():
    final = project_guardian_final(guardian_status("stopped"))
    assert final is not None
    assert final["estimated_exposure_cents"] == "0.511911111112"
    assert final["published_hourly_rate_usd"] == "0.014"
    # A shape that is not a decimal is still refused, never guessed.
    assert project_guardian_final(dict(guardian_status(), estimated_exposure_cents="1e5")) is None


def test_finish_passed_receipt_transient_import_failure_defers_outcome_and_resume_uploads_manifest(
    tmp_path,
):
    control = FinishControl(
        [guardian_status("stopped")],
        upload_errors=[sqlite3.OperationalError("database is locked")],
        exit_errors=[TimeoutError("timed out")],
    )
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, dict(RECEIPT))
    names = [a["action"] for a in actions]
    assert names.count("finish_deferred") == 1
    assert ("import_failed", "OperationalError") in [(a["action"], a["detail"]) for a in actions]
    assert actions[-1]["detail"] == "manifest"
    payload = next(c for c in control.calls if c[0] == "record_outcome")[2]
    assert payload["manifest_id"] == "manifest_1"
    assert h.names().count("report_exited") == 1


def test_finish_gives_up_after_the_bound_and_records_what_it_has(tmp_path, monkeypatch):
    from tessera_control import campaign_salad_executor as module

    monkeypatch.setattr(module, "FINISH_MAX_DEFERRALS", 2)
    control = FinishControl([TimeoutError("timed out")])
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, dict(RECEIPT), passes=5)
    names = [a["action"] for a in actions]
    assert names.count("finish_deferred") == 2
    assert "finish_retries_exhausted" in names
    assert h.names().count("record_outcome") == 1


def test_finish_integrity_failure_is_recorded_not_deferred(tmp_path):
    control = FinishControl([guardian_status("stopped")])
    h = Harness(tmp_path, control)

    def bad_importer(route_id, destination, profile):
        (destination / "output.json").write_bytes(b"other")
        return [dict(name="output.json", sha256=hashlib.sha256(b"x").hexdigest(), bytes=1)]

    h.executor.importer = bad_importer
    h.receipt = dict(RECEIPT)
    h.control.items = [item("route_started")]
    actions = h.executor.run_once()
    assert "finish_deferred" not in [a["action"] for a in actions]
    assert actions[-1] == {
        "action": "outcome_recorded",
        "attempt_id": "att_1",
        "detail": "no_manifest",
    }


# --- M9: an unconfirmed stop of a created group ----------------------------------------------


class GroupStopControl(FinishControl):
    def __init__(self, stops):
        super().__init__([guardian_status("stopped")])
        self.stops = list(stops)
        self.state = dict(self.state, termination_confirmed=False, effects=[{"action": "create"}])

    def guardian_stop(self, route_id):
        self._c("guardian_stop", route_id)
        return self.stops.pop(0) if len(self.stops) > 1 else self.stops[0]

    def report_group_stopped(self, lease_id, epoch, operation_id, provider_id, observed_at):
        self._c("report_group_stopped", provider_id)


@pytest.mark.parametrize("reason", ["stop_unresolved", "custody_stop_or_reconnect_unresolved"])
def test_unconfirmed_stop_is_held_until_guardian_confirms_exact_group(tmp_path, reason):
    control = GroupStopControl(
        [
            dict(provider_id="group-1", status="armed"),
            dict(provider_id="group-1", status="stopped"),
        ]
    )
    h = Harness(tmp_path, control)
    h.executor._group_stop_next = _Never()
    receipt = {
        "schema": "tessera.salad.live-check.v1",
        "status": "inconclusive",
        "reason": reason,
        "stop_confirmed": False,
        "provider_stop_confirmed": False,
    }
    # The child ran and wrote this receipt (a created group, the stop never confirmed).
    receipts = h.executor._route_dir("route_1") / "receipts"
    receipts.mkdir(mode=0o700, parents=True)
    _write_receipt(receipts, receipt)
    h.control.items = [item("executing")]
    actions = []
    for _ in range(4):
        if "record_outcome" in h.names():
            break
        actions += h.executor.run_once()
    names = [a["action"] for a in actions]
    assert "finish_deferred" in names
    assert names.index("group_stop_reported") < names.index("outcome_recorded")
    assert h.names().count("record_outcome") == 1


class _Never(dict):
    """Group-stop recheck pacing disabled for the test (the guardian is read every pass)."""

    def get(self, key, default=None):
        return 0.0

    def __setitem__(self, key, value):
        pass


# --- M6: the child finishes its tail through a SIGTERM ---------------------------------------


def test_live_check_child_defers_sigterm_and_counts_it():
    import os
    import signal

    from tessera_control import salad_live_check as live

    previous = signal.getsignal(signal.SIGTERM)
    before = live._SIGTERMS["received"]
    try:
        live._defer_sigterm()
        os.kill(os.getpid(), signal.SIGTERM)
        assert live._SIGTERMS["received"] == before + 1
    finally:
        signal.signal(signal.SIGTERM, previous)
        live._SIGTERMS["received"] = before


# --- the 10:01 AM ET shape once the guardian records this driver's cancel as control_cancel -------


class OwnCancelSession(IncidentSession):
    """Like the real session: ``cancel`` returns True when this call recorded the cancel at
    control, False when the route already said stop (an operator or another party)."""

    def __init__(self, *, already_stopped=False, **kwargs):
        super().__init__(**kwargs)
        self.already_stopped = already_stopped

    def cancel(self, route):
        recorded_here = not (self.already_stopped or self.cancelled)
        super().cancel(route)
        return recorded_here


def test_completion_cancel_recorded_as_control_cancel_still_waits_for_the_seal_and_passes(
    tmp_path,
):
    # The driver's completion cancel reaches the guardian before the output is recorded as
    # retained, so the guardian's first stop cause is control_cancel; custody seals later.
    session = OwnCancelSession(
        unsealed_polls=3,
        guardian_stop_cause="control_cancel",
        stop_confirm_errors=[TimeoutError("bounded live operation expired")],
    )
    receipt = drive(session, max_polls=10, max_seconds=1000)

    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["cancel_recorded"] is True
    assert receipt["cancel_recorded_by_driver"] is True
    assert receipt["guardian_stop_cause"] == "control_cancel"
    assert receipt["custody_seal_waits"] == 3
    assert receipt["stop_confirmed"] is True
    assert receipt["status"] == "passed", receipt["reason"]
    assert receipt["reason"] == "verified_journey"
    assert receipt["reason"] != "provider_stopped_custody_unsealed"

    # The executor records the pass with its manifest.
    control = FinishControl([guardian_status("stopped")])
    h = Harness(tmp_path, control)
    actions = run_until_recorded(h, receipt)
    assert actions[-1] == {
        "action": "outcome_recorded",
        "attempt_id": "att_1",
        "detail": "manifest",
    }
    payload = next(c for c in control.calls if c[0] == "record_outcome")[2]
    assert payload["manifest_id"] == "manifest_1"
    assert payload["live_receipt"]["status"] == "passed"


def test_completion_cancel_whose_guardian_call_failed_after_recording_still_waits_for_the_seal():
    failure = GuardianCancelUndelivered(recorded_here=True)
    failure.__cause__ = GuardianUnavailable()
    session = OwnCancelSession(
        unsealed_polls=2, guardian_stop_cause="control_cancel", cancel_errors=[failure]
    )
    receipt = drive(session, max_polls=10, max_seconds=1000)
    assert receipt["cancel_recorded_by_driver"] is True
    assert receipt["custody_seal_waits"] == 2
    assert receipt["status"] == "passed", receipt["reason"]


def test_control_cancel_recorded_by_someone_else_does_not_get_the_seal_wait():
    # The route already said stop when the run completed (an operator or campaign cancel): the
    # guardian's control_cancel is not this driver's completion stop, so no seal wait.
    session = OwnCancelSession(
        already_stopped=True, unsealed_polls=3, guardian_stop_cause="control_cancel"
    )
    receipt = drive(session, max_polls=10, max_seconds=1000)
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["cancel_recorded_by_driver"] is False
    assert "custody_seal_waits" not in receipt
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == "provider_stopped_custody_unsealed"


def test_a_precustody_reallocation_still_refuses_with_the_drivers_own_cancel():
    session = OwnCancelSession(
        unsealed_polls=0, guardian_stop_cause="allocation_changed", custody_work_begun=False
    )
    receipt = drive(session, max_polls=10, max_seconds=1000)
    assert receipt["cancel_recorded_by_driver"] is True
    assert receipt["status"] == "inconclusive"
    assert receipt["reason"] == PRECUSTODY_REALLOCATION
    assert "custody_seal_waits" not in receipt


def test_completion_stop_cause_accepts_control_cancel_only_for_the_drivers_own_completion():
    own = {
        "guardian_stop_cause": "control_cancel",
        "cancel_cause": {"class": "execution_complete"},
        "cancel_recorded": True,
        "cancel_recorded_by_driver": True,
    }
    assert _completion_stop_cause(own)
    assert _completion_stop_cause(dict(own, guardian_stop_cause=None))
    assert _completion_stop_cause(dict(own, guardian_stop_cause="output_retained"))
    assert not _completion_stop_cause(dict(own, cancel_recorded_by_driver=False))
    assert not _completion_stop_cause(dict(own, cancel_recorded=False))
    assert not _completion_stop_cause(dict(own, cancel_cause={"class": "control_refused"}))
    assert not _completion_stop_cause(dict(own, cancel_cause=None))
    for cause in ("heartbeat_silence", "allocation_changed", "cost_cap", "deadline"):
        assert not _completion_stop_cause(dict(own, guardian_stop_cause=cause))
    # Before any cancel (the undecided-refusal check while running): only None/output_retained.
    assert not _completion_stop_cause({"guardian_stop_cause": "control_cancel"})
