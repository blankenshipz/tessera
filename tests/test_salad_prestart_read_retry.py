"""Pre-start provider read failures do not end a run (live check start dispatch, transport).

Fakes only: the start preflight's fresh read is retried boundedly while it fails transiently,
never when it is deterministic; a bound, never-started group with no instance resource yet reads
as "no instance yet", not as a failed read."""

from test_salad_http import Factory, group, run
from test_salad_live_check import LocalSession, drive_clocked

from tessera_control.salad_server import ProviderReadUnavailable


class FlakyStartPreflight(LocalSession):
    def __init__(self, failures, *, reason=None, field=None):
        super().__init__()
        self.now = 0.0
        self.failures, self.reason, self.field = failures, reason, field
        self.start_attempts = 0
        self.beats = 0

    def keepalive(self, route):
        self.beats += 1

    def dispatch(self, route, action):
        if action == "start":
            self.start_attempts += 1
            if self.failures:
                self.failures -= 1
                error = ProviderReadUnavailable("Fresh provider preflight unavailable.")
                error.reason, error.field = self.reason, self.field
                raise error
        return super().dispatch(route, action)


def test_start_preflight_read_failing_then_succeeding_still_starts_smoke_10():
    session = FlakyStartPreflight(2)
    receipt = drive_clocked(session)
    assert receipt["status"] == "passed", receipt
    assert session.start_attempts == 3 and session.actions.count("start") == 1
    assert receipt["start_preflight_retries"] == 2 and receipt["prestart_failure"] is None
    assert session.beats >= 2  # the guardian heartbeat stays alive between attempts


def test_start_preflight_failures_past_the_bound_still_fail_without_a_start():
    session = FlakyStartPreflight(99, reason="provider_unavailable")
    receipt = drive_clocked(session)
    assert receipt["status"] == "inconclusive"
    assert session.start_attempts == 4 and "start" not in session.actions
    assert receipt["prestart_failure"]["exception"] == "ProviderReadUnavailable"
    assert session.cancelled


def test_deterministic_start_preflight_failure_is_not_retried():
    session = FlakyStartPreflight(99, reason="identity_mismatch", field="gpu_classes")
    receipt = drive_clocked(session)
    assert receipt["status"] == "inconclusive"
    assert session.start_attempts == 1 and receipt["start_preflight_retries"] == 0


def test_receipt_carries_the_guardian_prestart_read_retries_closed_summary():
    session = LocalSession()
    session.proof["prestart_read_retries"] = dict(
        count=2, first_at=1.0, last_at=6.0, last_reason="timeout", last_field=None,
        deadline_seconds=60.0,
    )  # fmt: skip
    from test_salad_live_check import drive

    receipt = drive(session)
    assert receipt["guardian_prestart_read_retries"]["count"] == 2
    assert receipt["guardian_prestart_read_retries"]["last_reason"] == "timeout"


def test_bound_unstarted_group_with_no_instance_resource_reads_as_no_instance_yet():
    # Smoke 9: the guardian reads instances once an id is bound; the group never started.
    f = Factory((200, group()), (404, {"title": "Not Found", "status": 404}))
    result = run(f, "read_instance")
    assert result.status == "observed" and result.observation["instances"] == []


def test_a_404_instance_read_of_a_running_group_is_still_a_failed_read():
    running = group()
    running["current_state"]["status"] = "running"
    running["current_state"]["instance_status_counts"]["running_count"] = 1
    f = Factory((200, running), (404, {"title": "Not Found", "status": 404}))
    assert run(f, "read_instance").status == "unknown"


def test_a_404_instance_read_without_a_problem_details_body_is_a_failed_read():
    for body in (b"<html>404</html>", [], {"title": "Forbidden", "status": 404}, {"status": "404"}):
        f = Factory((200, group()), (404, body))
        assert run(f, "read_instance").status == "unknown"


def test_other_instance_read_errors_are_still_failures():
    f = Factory((200, group()), (500, {"title": "x"}))
    assert run(f, "read_instance").status == "unknown"
