"""A bound group's definitive 404 on the GROUP resource is the closed reason ``group_gone``.

Fake connections only: no network, credentials or provider calls. The pre-allocation 404 on
the instances sub-resource stays "no instance yet", and a 404 before the group was ever seen
(no provider id) or with an unrecognised body stays the ordinary failed read. Nothing but
closed tokens may reach a result, event or status (no body, URL, organization or project).
"""

import json

import pytest
from test_salad_http import GROUP_404, ID, Factory, group, run
from test_salad_safety_service import service_case  # noqa: F401  (fixture)

from tessera_control.salad_http import SaladHTTPTransport

PRIVATE = ("acme", "research", "traceId", "Not Found", "/api/public", "rfc9110")


def assert_closed(text):
    for value in PRIVATE:
        assert value not in text


@pytest.mark.parametrize("operation", ["read_group", "read_instance"])
def test_a_group_404_after_the_group_was_seen_is_group_gone(operation):
    f = Factory((404, GROUP_404))
    result = run(f, operation, ID)
    assert (result.status, result.reason) == ("unknown", "group_gone")
    assert (result.detail, result.field, result.request_kind) == (None, None, "read_group")
    assert result.observation is None
    assert len(f.calls) == 1 and f.closed == 1  # no instances request, no retry inside
    assert_closed(result.model_dump_json())


def test_the_instances_404_on_an_existing_unstarted_group_stays_empty():
    f = Factory((200, group()), (404, GROUP_404))
    result = run(f, "read_instance", ID)
    assert (result.status, result.reason) == ("observed", "observed")
    assert result.observation["instances"] == []
    assert [c[1].endswith("/instances") for c in f.calls] == [False, True]


def test_the_instances_404_for_a_running_group_is_not_group_gone():
    running = group()
    running["current_state"]["status"] = "running"
    result = run(Factory((200, running), (404, GROUP_404)), "read_instance", ID)
    assert result.status == "unknown" and result.reason == "provider_unavailable"
    assert result.request_kind == "read_instance"


def test_a_group_404_before_the_group_was_seen_is_not_group_gone():
    result = run(Factory((404, GROUP_404)), "read_group", None)
    assert (result.reason, result.detail) == ("provider_unavailable", "non_2xx_4xx")


@pytest.mark.parametrize(
    "body",
    [
        {"secret": "provider internals"},
        {"traceId": "x"},
        {"title": "Not Found"},
        {"title": "Not Found", "status": "404"},
        {"title": "Forbidden", "status": 404},
        b"<html><body>404 Not Found</body></html>",
        b"",
    ],
)
def test_an_unrecognised_404_body_is_the_ordinary_failed_read(body):
    result = run(Factory((404, body)), "read_group", ID)
    assert (result.reason, result.detail) == ("provider_unavailable", "non_2xx_4xx")
    assert result.request_kind == "read_group"
    assert "internals" not in result.model_dump_json()


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_other_statuses_are_never_group_gone(status):
    result = run(Factory((status, GROUP_404)), "read_group", ID)
    assert result.reason == "provider_unavailable"


def test_a_bound_read_that_returns_200_still_checks_identity():
    data = group()
    data["name"] = "someone-elses-group"
    result = run(Factory((200, data)), "read_group", ID)
    assert result.reason == "identity_mismatch"


def test_the_service_stops_on_group_gone_without_burning_read_retries(service_case):  # noqa: F811
    s, b, *_ = service_case
    s.retry_sleep = lambda _: None
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    f = Factory(*[(404, GROUP_404)] * 40)
    s.transport_factory = lambda selection: SaladHTTPTransport(selection, connection_factory=f)
    with pytest.raises(ValueError, match="provider_unknown"):
        s._provider(s._row(b["reservation"]))
    assert len(f.calls) == 1  # deterministic: not retried
    assert s._row(b["reservation"])["provider_read_retries"] == 0

    service_case[4][0] += 5
    s.loop()
    status = s.admin(dict(operation="status", reservation=b["reservation"]))
    assert status["last_provider_read_failure"] == "provider_unknown"
    assert status["last_provider_read_failure_reason"] == "group_gone"
    assert status["last_provider_read_failure_request_kind"] == "read_group"
    assert status["last_provider_read_failure_detail"] is None
    assert (status["status"], status["reason"]) == ("unknown", "provider_unavailable")
    assert len(f.calls) <= 8  # a handful of reads in one pass, not the retry window
    events = s.db.execute(
        "SELECT kind, detail FROM events WHERE reservation=? AND kind LIKE 'provider_read_failed%'",
        (b["reservation"],),
    ).fetchall()
    assert any("group_gone" in (e[1] or "") for e in events)
    assert_closed(json.dumps([list(e) for e in events]) + json.dumps(status, default=str))
