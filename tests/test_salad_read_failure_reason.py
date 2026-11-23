"""A failed provider read records its closed transport reason and the offending field NAME.

Fake transports and responses only: no network, credentials or provider calls. Values the
provider echoes (and the organization/project/group names) must never reach a diagnostic.
"""

import copy
import json

import pytest
from test_salad_http import (
    GPU,
    Factory,
    gpu_class,
    group,
    run,
    selection,
)
from test_salad_live_check import LocalSession, drive
from test_salad_safety_service import service_case  # noqa: F401  (fixture)

from tessera_control.salad_http import SaladHTTPTransport
from tessera_control.salad_live_check import PRESTART_STEPS
from tessera_control.salad_safety_service import ProviderUnknown
from tessera_control.salad_secrets import SecretBuffer

LEAK = "echoed-value-that-must-not-leak"


def read_group_with(mutate):
    data = group()
    mutate(data)
    return run(Factory((200, data)))


def _set(path, value):
    def apply(data):
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return apply


@pytest.mark.parametrize(
    "mutate,field",
    [
        (
            _set(["container", "resources", "gpu_classes"], [LEAK]),
            "container.resources.gpu_classes",
        ),
        (_set(["container", "resources", "memory"], 4096), "container.resources.memory"),
        (
            _set(["container", "resources", "storage_amount"], 1),
            "container.resources.storage_amount",
        ),
        (_set(["container", "image"], LEAK), "container.image"),
        (
            _set(["container", "environment_variables"], {"X": LEAK}),
            "container.environment_variables",
        ),
        (_set(["priority"], LEAK), "priority"),
        (_set(["replicas"], 3), "replicas"),
        (_set(["organization_name"], LEAK), "organization_name"),
        (_set(["project_name"], LEAK), "project_name"),
        (_set(["name"], LEAK), "name"),
        (_set(["readiness_probe"], {"x": LEAK}), "readiness_probe"),
        (_set(["container", "logging"], {"x": LEAK}), "container.logging"),
    ],
)
def test_identity_mismatch_names_the_field_never_the_value(mutate, field):
    result = read_group_with(mutate)
    assert (result.status, result.reason) == ("unknown", "identity_mismatch")
    assert result.field == field
    dumped = result.model_dump_json()
    assert LEAK not in dumped
    for private in ("acme", "research"):
        assert private not in dumped


def test_an_unlisted_status_is_malformed_response_naming_current_state_status():
    result = read_group_with(lambda d: d["current_state"].__setitem__("status", LEAK))
    assert (result.reason, result.field) == ("malformed_response", "current_state.status")
    assert LEAK not in result.model_dump_json()


def test_a_missing_reviewed_key_names_the_key():
    result = read_group_with(lambda d: d.pop("current_state"))
    assert (result.reason, result.field) == ("malformed_response", "current_state")


@pytest.mark.parametrize(
    "status,detail",
    [(503, "non_2xx_5xx"), (500, "non_2xx_5xx"), (429, "non_2xx_4xx"), (403, "non_2xx_4xx")],
)
def test_non_2xx_records_only_the_status_class(status, detail):
    result = run(Factory((status, {"detail": LEAK})))
    assert (result.reason, result.detail, result.field) == ("provider_unavailable", detail, None)
    assert LEAK not in result.model_dump_json()


def test_reset_and_timeout_are_distinguished():
    reset = run(Factory(ConnectionResetError(LEAK)))
    assert (reset.reason, reset.detail) == ("provider_unavailable", "reset")
    slow = run(Factory(TimeoutError(LEAK)))
    assert (slow.reason, slow.detail) == ("provider_unavailable", "timeout")
    assert LEAK not in reset.model_dump_json() + slow.model_dump_json()


def test_a_non_json_body_is_malformed_response():
    result = run(Factory((200, b"<html>" + LEAK.encode())))
    assert result.reason == "malformed_response"
    assert LEAK not in result.model_dump_json()


def test_request_kind_names_the_failing_request_without_the_path():
    assert read_group_with(_set(["priority"], LEAK)).request_kind == "read_group"
    assert run(Factory((503, {}))).request_kind == "read_group"
    assert run(Factory((200, group()), (503, {})), "read_instance").request_kind == "read_instance"
    s = selection()

    def preflight(*responses):
        secret = SecretBuffer(bytearray(b"isolated-secret-marker"))
        with secret:
            return SaladHTTPTransport(s, connection_factory=Factory(*responses)).preflight(
                s, secret
            )

    assert preflight((503, {})).request_kind == "list"
    assert preflight((200, {"items": []}), (503, {})).request_kind == "quotas"
    quota = {
        "container_groups_quotas": dict(container_replicas_quota=10, container_replicas_used=1)
    }
    assert preflight((200, {"items": []}), (200, quota), (503, {})).request_kind == "gpu_classes"
    assert (
        preflight(
            (200, {"items": []}), (200, quota), (200, {"items": [gpu_class()]}), (503, {})
        ).request_kind
        == "availability"
    )
    dumped = preflight((503, {})).model_dump_json()
    assert "containers" not in dumped and "acme" not in dumped


def test_the_existing_group_preflight_failure_carries_the_field():
    data = group()
    data["container"]["resources"]["gpu_classes"] = [LEAK]
    s = selection()
    secret = SecretBuffer(bytearray(b"isolated-secret-marker"))
    with secret:
        result = SaladHTTPTransport(
            s, connection_factory=Factory((200, {"items": [data]}))
        ).preflight(s, secret)
    assert (result.status, result.reason) == ("unknown", "identity_mismatch")
    assert result.field == "container.resources.gpu_classes"
    assert result.request_kind == "list"
    assert LEAK not in result.model_dump_json()
    assert GPU not in result.model_dump_json()


def test_outward_reason_vocabulary_is_unchanged_and_fields_stay_closed():
    from pydantic import ValidationError

    from tessera_control.salad_proxy_contract import ProxyResult

    ProxyResult(status="unknown", reason="identity_mismatch", field="container.image")
    with pytest.raises(ValidationError):
        ProxyResult(status="unknown", reason="identity_mismatch", field="has a value 123")
    with pytest.raises(ValidationError):
        ProxyResult(status="unknown", reason="provider_unavailable", detail="Not Closed")


# --- safety service: provider_read_failed keeps provider_unknown, adds closed diagnostics ----


def _failing_reads(service, result_factory):
    original = service.transport_factory

    def factory(selection):
        inner = original(selection)

        class Wrapped:
            def __getattr__(self, name):
                return getattr(inner, name)

            def send(self, action, selection, provider_id, secret):
                if action in {"read_group", "read_instance"}:
                    return result_factory()
                return inner.send(action, selection, provider_id, secret)

        return Wrapped()

    service.transport_factory = factory


def _result(reason, detail=None, field=None, kind="read_instance"):
    return lambda: type(
        "Result",
        (),
        dict(
            status="unknown",
            observation=None,
            reason=reason,
            detail=detail,
            field=field,
            request_kind=kind,
        ),
    )()


def _status(service, body):
    return service.admin(dict(operation="status", reservation=body["reservation"]))


@pytest.mark.parametrize(
    "reason,detail,field",
    [
        ("identity_mismatch", None, "container.resources.gpu_classes"),
        ("malformed_response", None, "current_state.status"),
        ("provider_unavailable", "non_2xx_5xx", None),
        ("provider_unavailable", "reset", None),
    ],
)
def test_provider_read_failed_records_closed_reason_and_field(service_case, reason, detail, field):  # noqa: F811
    s, b, *_ = service_case
    s.retry_sleep = lambda _: None
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    _failing_reads(s, _result(reason, detail, field))
    service_case[4][0] += 5
    s.loop()
    status = _status(s, b)
    assert status["last_provider_read_failure"] == "provider_unknown"  # outward code unchanged
    assert status["last_provider_read_failure_reason"] == reason
    assert status["last_provider_read_failure_detail"] == detail
    assert status["last_provider_read_failure_field"] == field
    assert status["last_provider_read_failure_request_kind"] == "read_instance"
    assert status["stop_cause"] == "provider_unavailable"  # retry/stop behavior untouched
    events = s.db.execute(
        "SELECT kind, detail FROM events WHERE reservation=? AND kind LIKE 'provider_read_failed%'",
        (b["reservation"],),
    ).fetchall()
    assert json.dumps([list(e) for e in events]).count("provider_unknown") >= 1
    assert LEAK not in json.dumps([list(e) for e in events])


def test_a_timeout_records_the_timeout_reason(service_case):  # noqa: F811
    s, b, request, token, clock, provider, *_ = service_case
    s.retry_sleep = lambda _: None
    s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    provider.read_error = TimeoutError("service_deadline")
    provider.read_failures = 0
    send = s._send

    def always_timeout(row, action, *args, **kwargs):
        if action in {"read_group", "read_instance"}:
            raise TimeoutError("service_deadline")
        return send(row, action, *args, **kwargs)

    s._send = always_timeout
    clock[0] += 5
    s.loop()
    status = _status(s, b)
    assert status["last_provider_read_failure"] == "timeout"
    assert status["last_provider_read_failure_reason"] == "timeout"
    assert status["last_provider_read_failure_detail"] == "timeout"
    assert status["last_provider_read_failure_field"] is None


def test_provider_unknown_text_stays_the_closed_code():
    error = ProviderUnknown("identity_mismatch", None, "priority")
    assert str(error) == "provider_unknown" and isinstance(error, ValueError)


# --- start preflight refusal --------------------------------------------------------------


def test_start_preflight_refusal_carries_the_transport_diagnostics():
    from tessera_control import salad_server

    refusal = salad_server.ProviderReadUnavailable("Fresh provider preflight unavailable.")
    assert (refusal.failure, refusal.detail, refusal.field) == (None, None, None)


# --- live receipt ---------------------------------------------------------------------------


def test_receipt_records_prestart_step_timestamps():
    session = LocalSession()
    session.expects_input = True
    session.record["provider_id"] = "synthetic-group"
    ticks = iter(f"2030-01-01T00:00:{n:02d}Z" for n in range(60))
    receipt = _drive_with_wall_clock(session, lambda: next(ticks))
    stamps = receipt["prestart_timestamps"]
    assert tuple(stamps) == PRESTART_STEPS
    assert all(stamps[name] for name in PRESTART_STEPS)
    assert [stamps[n] for n in PRESTART_STEPS] == sorted(stamps[n] for n in PRESTART_STEPS)


def _drive_with_wall_clock(session, wall_clock):
    from tessera_control.salad_live_check import _drive

    return _drive(
        session,
        route_id="exact-route",
        source_revision="a" * 40,
        max_seconds=10,
        max_polls=2,
        sleep=lambda _: None,
        clock=lambda: 0,
        wall_clock=wall_clock,
    )


def test_steps_that_did_not_run_stay_none():
    session = LocalSession()
    session.fail = "create"
    stamps = drive(session)["prestart_timestamps"]
    assert stamps["create"] is None and stamps["start"] is None


def test_a_refused_start_preflight_puts_reason_and_field_on_the_receipt():
    from tessera_control.salad_server import ProviderReadUnavailable

    session = LocalSession()
    original = session.dispatch

    def dispatch(route, action):
        if action == "start":
            refusal = ProviderReadUnavailable(LEAK)
            refusal.failure = "identity_mismatch"
            refusal.field = "container.resources.gpu_classes"
            refusal.request_kind = "list"
            raise refusal
        return original(route, action)

    session.dispatch = dispatch
    receipt = drive(session)
    failure = receipt["prestart_failure"]
    assert failure["class"] == "start_failed"
    assert failure["exception"] == "ProviderReadUnavailable"
    assert failure["provider_read"] == {
        "reason": "identity_mismatch",
        "detail": None,
        "field": "container.resources.gpu_classes",
        "request_kind": "list",
    }
    assert receipt["prestart_timestamps"]["start_preflight"] is not None
    assert receipt["prestart_timestamps"]["start"] is None
    assert LEAK not in json.dumps(receipt)


def test_guardian_diagnostics_reach_the_receipt_and_values_are_not_echoed():
    session = LocalSession()
    session.proof = copy.deepcopy(session.proof)
    session.proof.update(
        last_provider_read_failure="provider_unknown",
        last_provider_read_failure_reason="identity_mismatch",
        last_provider_read_failure_detail=None,
        last_provider_read_failure_field="container.resources.gpu_classes",
        last_provider_read_failure_request_kind="list",
    )
    receipt = drive(session)
    assert receipt["guardian_last_provider_read_failure_reason"] == "identity_mismatch"
    assert receipt["guardian_last_provider_read_failure_detail"] is None
    assert receipt["guardian_last_provider_read_failure_field"] == "container.resources.gpu_classes"
    assert receipt["guardian_last_provider_read_failure_request_kind"] == "list"
