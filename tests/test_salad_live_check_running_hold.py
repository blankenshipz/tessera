"""A running route is never cancelled on an unexpected or transient error; every error the live
check holds, skips or ends on is on the receipt, sanitized.

While the last post-start observe read running and the guardian recorded no stop cause, only
the deadline, a budget refusal, an explicit operator cancel or revocation, the guardian's own
decision (stop cause or terminal refusal) or a verified execution_complete ends the run.

Isolated orchestration only (fake sessions, a fake HTTPS connection): never a provider.
"""

import hashlib
import json
import logging
import sqlite3
import subprocess
from contextlib import nullcontext

import pytest
from test_salad_live_check_running_cancel import (
    SECRET,
    DeployingSession,
    RunningSession,
    StoppedSession,
    cancelled_before_output,
    drive,
)

from tessera_control import campaign_salad_executor, salad_live_check, salad_safety_service
from tessera_control.campaign_salad_executor import (
    RouteExitedWithoutReceipt,
    live_check_error_summary,
    live_check_report_error,
    subprocess_route_runner,
)
from tessera_control.remote_execution import RouteRevisionChanged
from tessera_control.salad_live_check import (
    CANCEL_CAUSE_CLASSES,
    RUNNING_CANCEL_PREFIX,
    UNRECOGNIZED_DETAIL,
    SaladLiveSession,
    _error_detail,
    _is_read_stall,
)
from tessera_control.salad_safety_service import (
    SERVICE_UNAVAILABLE_DETAILS,
    SafetyHTTPSClient,
    ServiceUnavailable,
)
from tessera_control.salad_server import GuardianRefused, GuardianUnavailable
from tessera_control.store import Conflict, Forbidden

RETIRED = "Worker is retired."
CERT = b"pinned-service-certificate"


# --- the exact Seed 17 raise site: the guardian status read timed out -------------------------


class _Sock:
    def getpeercert(self, binary_form=False):
        return CERT


def fake_connection(failure):
    """An HTTPSConnection whose response read fails with ``failure`` (or answers ``failure``
    when it is an (HTTP status, body) pair)."""

    class Connection:
        def __init__(self, host, port, timeout=None, context=None):
            self.sock = None

        def connect(self):
            self.sock = _Sock()

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            if isinstance(failure, BaseException):
                raise failure
            status, body = failure

            class Response:
                def read(self, cap):
                    return body

            response = Response()
            response.status = status
            return response

        def close(self):
            pass

    return Connection


def https_client(monkeypatch, failure):
    monkeypatch.setattr(salad_safety_service, "private_read", lambda path, cap: b"token")
    monkeypatch.setattr(
        salad_safety_service.http.client, "HTTPSConnection", fake_connection(failure)
    )
    client = object.__new__(SafetyHTTPSClient)
    client.host, client.port, client.token_file = "guardian.invalid", 8443, "unused"
    client.context, client.pin = None, hashlib.sha256(CERT).hexdigest()
    return client


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (TimeoutError("The read operation timed out"), "timeout"),
        (TimeoutError("bounded live operation expired"), "timeout"),
        (ConnectionResetError(104, "Connection reset by peer"), "reset"),
        (ConnectionRefusedError(111, "refused"), "connect"),
        ((503, b"{}"), "non_200"),
        ((200, b"{not json"), "malformed_response"),
        ((200, b"x" * 200000), "oversize"),
    ],
)
def test_the_admin_exchange_keeps_its_error_but_names_a_closed_detail(monkeypatch, failure, detail):
    client = https_client(monkeypatch, failure)
    with pytest.raises(ValueError, match="^service_unavailable$") as raised:
        client.exchange({"operation": "status"})
    assert type(raised.value) is ServiceUnavailable
    assert raised.value.detail == detail
    assert raised.value.detail in SERVICE_UNAVAILABLE_DETAILS
    assert raised.value.__cause__ is None
    if isinstance(failure, BaseException):
        # Raised ``from None``: the transport error stays out of any traceback text, but the
        # live check still names its class (``cause``) from the suppressed context.
        assert raised.value.__suppress_context__
        assert type(raised.value.__context__) is type(failure)


def test_a_pin_mismatch_is_not_a_transient_read(monkeypatch):
    client = https_client(monkeypatch, (200, b"{}"))
    client.pin = "0" * 64
    with pytest.raises(ServiceUnavailable) as raised:
        client.exchange({})
    assert raised.value.detail == "identity_mismatch"
    assert not _is_read_stall(raised.value)
    assert _is_read_stall(ServiceUnavailable("timeout"))
    assert not _is_read_stall(ServiceUnavailable("non_200"))


def concrete_evidence(monkeypatch, failure):
    """The real SaladLiveSession.evidence over the real admin client, failing like Seed 17."""
    monkeypatch.setattr(salad_live_check, "_wall_bound", lambda seconds: nullcontext())
    live = object.__new__(SaladLiveSession)
    live.custody = https_client(monkeypatch, failure)
    live.state = lambda route: {"proposal_digest": "p" * 64}
    return live


class StatusReadSession(RunningSession):
    """A running route whose guardian status reads (``evidence``) fail on the listed calls."""

    def __init__(self, failing, read, **kwargs):
        super().__init__(**kwargs)
        self.failing, self.read, self.evidence_calls = set(failing), read, 0

    def evidence(self, route):
        self.evidence_calls += 1
        if self.evidence_calls in self.failing:
            return self.read(route)
        return super().evidence(route)


def test_seed_17_a_timed_out_guardian_status_read_never_cancels_a_running_route(monkeypatch):
    # The incident: the group reads running, the guardian has no stop cause, and its admin
    # status read times out (it was itself waiting on a provider read_instance timeout). The
    # real client turns that into ValueError("service_unavailable"), which used to fall
    # through to cancel_cause class "error" / exception "ValueError" and cancel the run.
    live = concrete_evidence(monkeypatch, TimeoutError("The read operation timed out"))
    session = StatusReadSession({2, 3}, live.evidence, retain_after=3)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["observe_transient_failures"] == 2
    assert receipt["last_skipped_read"] == {
        "step": "evidence",
        "exception": "ServiceUnavailable",
        "message": "service_unavailable",
        "cause": "TimeoutError",
        "detail": "timeout",
        "site": "salad_safety_service.exchange",
        "call": None,
    }
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)


def test_seed_17_status_read_timeouts_that_outlast_the_run_end_only_at_the_deadline(monkeypatch):
    live = concrete_evidence(monkeypatch, TimeoutError("timed out"))
    session = StatusReadSession(range(2, 10_000), live.evidence)
    receipt = drive(session, max_polls=40, max_seconds=100)
    cause = receipt["cancel_cause"]
    assert cause["class"] == "deadline", cause
    assert cause["last_provider_status"] == "running"
    assert receipt["last_skipped_read"]["detail"] == "timeout"
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)


def test_status_read_timeouts_before_running_are_skipped_like_other_read_stalls(monkeypatch):
    # Before running the timed-out status read is a read stall like a locked control read
    # (skipped while observes succeed), no longer an immediate "error" cancel; the run still
    # ends at its deadline and the receipt names the transport detail.
    live = concrete_evidence(monkeypatch, TimeoutError("timed out"))
    session = DeployingSession()
    session.evidence = live.evidence
    receipt = drive(session, max_polls=40, max_seconds=100)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "deadline"
    assert receipt["last_provider_status"] == "deploying"
    assert receipt["last_skipped_read"]["detail"] == "timeout"
    assert "running_errors_held" not in receipt


def test_status_read_timeouts_with_failing_observes_keep_the_bound():
    # The bound applies once the running latch is cleared (an affirmative stopped read).
    session = StoppedSession(observe_errors=[None] + [ServiceUnavailable("timeout")] * 5)
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "observe_timeout"
    assert receipt["cancel_cause"]["error"]["detail"] == "timeout"


# --- any other unexpected error on a running route is held, with a sanitized message ---------


def raising(error):
    def read(route):
        raise error

    return read


@pytest.mark.parametrize(
    "error",
    [
        ValueError("exact retained metadata required"),  # a partial guardian status reply
        ValueError("something nobody expected"),
        ValueError(),
        ServiceUnavailable("non_200"),
        GuardianRefused("internal"),
        sqlite3.OperationalError("database is locked"),
        KeyError("output_retained"),
        type("NovelError", (Exception,), {})("brand new failure"),
    ],
    ids=lambda e: type(e).__name__ + ":" + (e.args[0] if e.args else ""),
)
def test_an_unexpected_evidence_error_on_a_running_route_is_held(error):
    session = StatusReadSession({2, 3}, raising(error), retain_after=3)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    detail = receipt.get("last_held_error") or receipt.get("last_skipped_read")
    assert detail["step"] == "evidence"
    assert detail["exception"] == type(error).__name__


def test_a_held_value_error_records_its_message_class_and_site():
    session = StatusReadSession({2}, raising(ValueError("something nobody expected")))
    session.retain_after = 3
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["running_errors_held"] == 1
    held = receipt["last_held_error"]
    assert held["exception"] == "ValueError"
    assert held["message"] == "something nobody expected"
    assert held["step"] == "evidence"
    assert held["site"] == "test_salad_live_check_running_hold.read"
    assert held["heartbeat_silent_seconds"] is not None


class PartialSession(RunningSession):
    """Running; the listed post-start observes return a partial or malformed reply."""

    def __init__(self, replies, **kwargs):
        super().__init__(**kwargs)
        self.replies = dict(replies)
        self.observes = 0

    def dispatch(self, route, action):
        state = super().dispatch(route, action)
        if action == "observe" and "start" in self.actions and not self.cancelled:
            self.observes += 1
            if self.observes in self.replies:
                return self.replies[self.observes]
        return state


@pytest.mark.parametrize(
    ("reply", "held"),
    [({}, 2), ({"provider_status": None}, 0), (["running"], 2), ("running", 2)],
)
def test_a_partial_or_malformed_provider_reply_on_a_running_route_is_held(reply, held):
    # A reply that names no status never makes a running route read unknown.
    session = PartialSession({2: reply, 3: reply}, retain_after=4)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["last_provider_status"] == "running"
    assert receipt.get("running_errors_held", 0) == held


def test_a_partial_guardian_status_reply_on_a_running_route_is_held():
    session = StatusReadSession({2, 3}, lambda route: {"financial": {}}, retain_after=3)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["last_held_error"]["step"] == "record"
    assert receipt["last_held_error"]["exception"] == "KeyError"


@pytest.mark.parametrize(
    "error",
    [
        GuardianUnavailable,
        lambda: GuardianRefused("service_deadline"),
        lambda: GuardianRefused("internal"),
        lambda: ValueError("unexpected keepalive failure"),
        lambda: RuntimeError("anything"),
    ],
)
def test_keepalive_errors_past_the_silence_bound_on_a_running_route_are_held(error):
    session = RunningSession(retain_after=12, beat_errors=[None] + [error() for _ in range(10)])
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["running_errors_held"] >= 1
    assert receipt["last_held_error"]["step"] == "keepalive"
    assert receipt["guardian_refusal"] is None
    assert receipt["cancel_cause"]["class"] == "execution_complete"


def test_a_route_revision_race_while_running_is_held():
    race = RouteRevisionChanged("Route revision changed.")
    session = RunningSession(retain_after=2, observe_errors=[None, race, race])
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["last_held_error"]["exception"] == "RouteRevisionChanged"


def test_errors_that_outlast_the_run_end_at_the_deadline_with_the_held_detail():
    session = StatusReadSession(range(2, 10_000), raising(ValueError("still broken")))
    receipt = drive(session, max_polls=40, max_seconds=100)
    cause = receipt["cancel_cause"]
    assert cause["class"] == "deadline"
    # The poll bound and the deadline coincide (interval = window / polls); the deadline names it.
    assert cause["error"]["message"] in {"output unavailable", "poll bound"}
    assert receipt["last_held_error"]["message"] == "still broken"
    assert receipt["running_errors_held"] >= 8
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)


# --- genuine terminal causes still end the run ------------------------------------------------


def test_the_deadline_still_ends_a_clean_run():
    receipt = drive(RunningSession(), max_polls=40, max_seconds=50)
    assert receipt["cancel_cause"]["class"] == "deadline"


@pytest.mark.parametrize("code", ["budget_exhausted", "budget_binding_expired", "rate_unknown"])
def test_a_budget_refusal_still_cancels(code):
    session = RunningSession(beat_errors=[None, None, GuardianRefused(code)])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "budget"
    assert receipt["cancel_cause"]["error"]["exception"] == "GuardianRefused"
    assert "running_errors_held" not in receipt


def test_a_guardian_terminal_refusal_still_cancels():
    session = RunningSession(beat_errors=[None, None, GuardianRefused("stop_only")])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "guardian_refusal"


@pytest.mark.parametrize(
    "error",
    [lambda: Forbidden(RETIRED), lambda: Conflict("Remote execution lease/epoch is fenced.")],
)
def test_an_operator_cancel_or_revocation_still_cancels(error):
    session = RunningSession(observe_errors=[None, error()])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "control_refused"
    assert "running_errors_held" not in receipt


def test_a_guardian_stop_cause_lifts_the_hold():
    session = StatusReadSession(
        range(3, 10_000), raising(ValueError("x")), stop_cause="missed_poll"
    )
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["guardian_stop_cause"] == "missed_poll"
    assert receipt["cancel_cause"]["class"] == "guardian_stop_cause"
    assert receipt["cancel_cause"]["error"]["message"] == "x"
    assert "running_errors_held" not in receipt


def test_execution_complete_still_completes_through_held_errors():
    session = StatusReadSession({2}, raising(ValueError("x")), retain_after=2)
    receipt = drive(session)
    assert receipt["status"] == "passed"
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["cancel_cause"]["error"] is None


def test_an_unexpected_error_after_an_affirmative_stop_still_cancels_with_its_detail():
    session = StoppedSession(observe_errors=[None, ValueError("not yet running")])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["cancel_cause"]["class"] == "error"
    assert receipt["cancel_cause"]["error"]["message"] == "not yet running"
    assert receipt["cancel_cause"]["class"] in CANCEL_CAUSE_CLASSES


# --- the recorded detail is sanitized and bounded ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        SECRET,
        "x" * 400,
        "Bearer abc",
        "token abc",
        "see https://example.invalid/a?b=c",
        "open /srv/private/state/file failed",
        "a" * 30,
    ],
)
def test_an_error_message_never_carries_secret_path_or_unbounded_text(text):
    detail = _error_detail(ValueError(text))
    assert detail["message"] == UNRECOGNIZED_DETAIL
    assert text not in json.dumps(detail)


def test_an_error_message_keeps_short_plain_text_and_drops_non_text_args():
    assert _error_detail(ValueError("poll bound"))["message"] == "poll bound"
    assert _error_detail(ValueError())["message"] is None
    assert _error_detail(OSError(2, "No such file"))["message"] == UNRECOGNIZED_DETAIL
    assert _error_detail(None) is None


def test_a_held_secret_never_reaches_the_receipt():
    session = StatusReadSession({2}, raising(RuntimeError(SECRET)), retain_after=3)
    receipt = drive(session)
    assert receipt["status"] == "passed"
    assert receipt["last_held_error"]["message"] == UNRECOGNIZED_DETAIL
    assert SECRET not in json.dumps(receipt)


# --- the executor's journal is never blind ----------------------------------------------------


def test_error_summary_rechecks_every_field():
    summary = live_check_error_summary(
        {
            "exception": "ValueError",
            "message": "service_unavailable",
            "cause": "TimeoutError",
            "detail": "timeout",
            "site": "salad_safety_service.exchange",
            "step": "evidence",
        }
    )
    assert summary == (
        "exception=ValueError cause=TimeoutError detail=timeout "
        "site=salad_safety_service.exchange step=evidence message='service_unavailable'"
    )
    assert live_check_error_summary({"message": SECRET, "site": "/etc/passwd"}) is None
    assert live_check_error_summary("ValueError") is None


def completed(stdout=b""):
    return subprocess.CompletedProcess(args=[], returncode=2, stdout=stdout, stderr=b"")


def test_a_child_without_a_receipt_logs_its_sanitized_error(monkeypatch, tmp_path, caplog):
    report = {
        "status": "inconclusive",
        "reason": "secure_setup_or_execution_unresolved",
        "error": {"exception": "ValueError", "message": "trusted session composition unavailable"},
    }
    stdout = json.dumps(report).encode() + b"\n"
    assert live_check_report_error(stdout) == (
        "exception=ValueError message='trusted session composition unavailable'"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed(stdout))
    monkeypatch.setattr(campaign_salad_executor, "read_receipt", lambda directory: None)
    with caplog.at_level(logging.WARNING), pytest.raises(RouteExitedWithoutReceipt):
        subprocess_route_runner(tmp_path / "c.json", tmp_path, 5)
    assert "trusted session composition unavailable" in caplog.text


def test_a_receipt_with_held_errors_is_logged(monkeypatch, tmp_path, caplog):
    receipt = {
        "running_errors_held": 2,
        "last_held_error": {"exception": "ValueError", "message": "x", "step": "evidence"},
        "cancel_cause": {"class": "execution_complete", "error": None},
    }
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed())
    monkeypatch.setattr(campaign_salad_executor, "read_receipt", lambda directory: receipt)
    with caplog.at_level(logging.WARNING):
        assert subprocess_route_runner(tmp_path / "c.json", tmp_path, 5) is receipt
    assert "held 2 error(s)" in caplog.text and "step=evidence" in caplog.text
