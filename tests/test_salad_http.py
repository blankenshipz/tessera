"""Isolated official-wire tests: no HTTP server, credentials, or provider calls."""

import copy
import json
import time

import pytest

from tessera_control.remote_execution_models import ExecutionSelection
from tessera_control.salad_http import SaladHTTPTransport
from tessera_control.salad_secrets import SecretBuffer

GPU = "550e8400-e29b-41d4-a716-446655440000"
GROUP = "ab3a4591-efc3-46c0-b06a-3d820c0ec100"
ID = GROUP.replace("-", "")
RESERVED_GROUP = "attempt-" + "a" * 32
MARKER = b"isolated-secret-marker"


def selection():
    return ExecutionSelection.model_validate(
        dict(
            target="salad_container",
            target_digest="a" * 64,
            resources=dict(
                cpu_millicores=2000,
                memory_mb=2048,
                disk_mb=2048,
                gpu_count=1,
                gpu_class=GPU,
                max_seconds=30,
            ),
            manifests=dict(
                image="public/workload@sha256:" + "a" * 64,
                **{k: "a" * 64 for k in ("code", "data", "model", "environment")},
            ),
            salad=dict(
                organization="acme",
                project="research",
                group=RESERVED_GROUP,
                country="US",
                provider_trust="broad_egress_ephemeral_reallocation_credit_not_hard_cap",
                execution=dict(
                    platform="linux/amd64",
                    command="/tessera/bin/run",
                    argv=["gpu"],
                    env={"PYTHONUNBUFFERED": "1"},
                    max_output_bytes=1024,
                    max_output_files=1,
                    capability_digest="a" * 64,
                    network_policy="provider_broad_egress_explicitly_accepted",
                    data_policy="public_released_only",
                    artifact_destination="artifact-ref:output",
                    checkpoint_destination="checkpoint-ref:output",
                    monitor_profile="a" * 64,
                    watchdog_profile="a" * 64,
                    max_cost_cents=10,
                    credit_exposure="unknown_blocks_new_work_credit_not_hard_cap",
                ),
            ),
        )
    )


def gpu_class():
    return dict(
        id=GPU,
        name="Synthetic GPU",
        gpu_count=1,
        min_vcpu=1,
        max_vcpu=8,
        min_ram=1024,
        max_ram=32768,
        min_storage=1073741824,
        max_storage=268435456000,
        prices=[dict(priority="high", price="0.123456789")],
    )


def group():
    return dict(
        id=GROUP,
        name=RESERVED_GROUP,
        organization_name="acme",
        project_name="research",
        autostart_policy=False,
        replicas=1,
        restart_policy="never",
        country_codes=["us"],
        priority="high",
        pending_change=False,
        version=1,
        **{"scheduled-scaling-enabled": False, "scaling-actions": []},
        container=dict(
            image="public/workload@sha256:" + "a" * 64,
            command=["/tessera/bin/run", "gpu"],
            environment_variables={"PYTHONUNBUFFERED": "1"},
            resources=dict(cpu=2, memory=2048, storage_amount=2147483648, gpu_classes=[GPU]),
        ),
        current_state=dict(
            status="stopped",
            description="UNTRUSTED NOT RETURNED",
            instance_status_counts={
                k + "_count": 0 for k in ("allocating", "creating", "running", "stopping")
            },
        ),
    )


class Response:
    def __init__(self, status, data, headers=None):
        self.status = status
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.offset = 0
        self.headers = headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        chunk = self.raw[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class Factory:
    def __init__(self, *responses, connect_error=None):
        self.responses = list(responses)
        self.calls = []
        self.closed = 0
        self.timeouts = []
        self.connect_error = connect_error

    def __call__(self, host, *, port, timeout):
        # Reads get the 2 s total deadline; only the create POST gets 12 s.
        assert (host, port) == ("api.salad.com", 443) and timeout in (2, 12)
        self.timeouts.append(timeout)
        factory = self

        class Connection:
            sock = None

            def connect(self):
                if factory.connect_error is not None:
                    raise factory.connect_error

            def request(self, method, path, *, body, headers):
                factory.calls.append((method, path, body, headers))

            def getresponse(self):
                response = factory.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return Response(*response)

            def close(self):
                factory.closed += 1

        return Connection()


def run(factory, operation="read_group", provider_id=ID):
    s = selection()
    secret = SecretBuffer(bytearray(MARKER))
    with secret:
        result = SaladHTTPTransport(s, connection_factory=factory).send(
            operation, s, provider_id, secret
        )
    assert not any(secret._buffer)
    assert MARKER.decode() not in result.model_dump_json()
    return result


def test_create_exact_wire_and_actual_identity(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    f = Factory((201, group()))
    result = run(f, "create_stopped", None)
    assert result.status == "observed"
    assert result.observation["provider_id"] == ID
    method, path, body, headers = f.calls[0]
    assert method == "POST"
    assert path == "/api/public/organizations/acme/projects/research/containers"
    wire = json.loads(body)
    assert wire["autostart_policy"] is False
    assert wire["replicas"] == 1 and wire["restart_policy"] == "never"
    assert wire["country_codes"] == ["us"]
    assert wire["container"]["resources"] == group()["container"]["resources"]
    assert wire["container"]["command"] == ["/tessera/bin/run", "gpu"]
    assert set(headers) == {"Salad-Api-Key", "Accept", "Content-Type"}
    assert f.closed == 1
    assert "UNTRUSTED" not in result.model_dump_json()


@pytest.mark.parametrize("operation", ["start", "stop", "delete"])
def test_ack_is_pending_after_exact_identity_read(operation):
    f = Factory((200, group()), (202, b""))
    result = run(f, operation)
    assert result.status == "pending" and result.reason == "accepted_pending"
    assert result.observation["state"] == "unknown"
    assert not {"terminated", "no_writers", "allocation_terminated"} & result.observation.keys()
    assert f.calls[0][0] == "GET" and len(f.calls) == 2
    assert f.calls[1][0] == ("DELETE" if operation == "delete" else "POST")


def test_preflight_real_nested_quota_and_filtered_availability():
    f = Factory(
        (200, {"items": []}),
        (
            200,
            {
                "container_groups_quotas": {
                    "container_replicas_quota": 5,
                    "container_replicas_used": 4,
                }
            },
        ),
        (200, {"items": [gpu_class()]}),
        (200, {"available_gpu_high": 1, "on_call_gpu": 999}),
    )
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert result.status == "observed"
    assert result.observation["project_exists"] and result.observation["group_absent"]
    assert result.observation["quota_ok"] and result.observation["available"]
    assert f.calls[-1][1].endswith("/availability/sce-gpu-availability")
    assert json.loads(f.calls[-1][2]) == dict(
        group()["container"]["resources"], country_codes=["us"]
    )


@pytest.mark.parametrize(
    "status,data,reason",
    [
        (302, b"", "redirect_denied"),
        (200, b"{", "malformed_response"),
        (200, b"x" * 65537, "malformed_response"),
        (200, {"message": MARKER.decode()}, "malformed_response"),
        (404, {"secret": "provider internals"}, "provider_unavailable"),
        (429, {"message": "retry"}, "provider_unavailable"),
        (200, {"id": "invented"}, "malformed_response"),
    ],
)
def test_no_redirect_retry_raw_output_or_missing_group_termination(status, data, reason):
    f = Factory((status, data))
    result = run(f)
    assert result.status == "unknown" and result.reason == reason
    assert result.observation is None
    assert len(f.calls) == 1 and f.closed == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", GPU),
        ("organization_name", "foreign"),
        ("project_name", "foreign"),
        ("name", "foreign"),
        ("replicas", 2),
        ("country_codes", ["ca"]),
        ("autostart_policy", True),
        ("restart_policy", "always"),
        ("priority", "low"),
        ("pending_change", "false"),
        ("queue_connection", {}),
        ("scheduled-scaling-enabled", True),
    ],
)
def test_rev001_stop_survives_mutable_drift_but_start_stays_fenced(field, value):
    data = group()
    data[field] = value
    f = Factory((200, data))
    assert run(f, "start").reason == "identity_mismatch"
    f = Factory((200, data), (202, b""))
    result = run(f, "stop")
    if field in {"id", "organization_name", "project_name", "name"}:
        assert result.reason == "identity_mismatch" and len(f.calls) == 1
    else:
        assert result.status == "pending" and len(f.calls) == 2


def test_pin_rejects_client_selection_override_before_network():
    s = selection()
    f = Factory()
    transport = SaladHTTPTransport(s, connection_factory=f)
    raw = s.model_dump()
    raw["salad"]["group"] = "attempt-" + "b" * 32
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = transport.send("start", ExecutionSelection.model_validate(raw), ID, secret)
    assert result.reason == "identity_mismatch" and not f.calls


def test_lost_create_is_not_retried_reconcile_by_reserved_name():
    f = Factory(TimeoutError(MARKER.decode()), (200, group()))
    s = selection()
    transport = SaladHTTPTransport(s, connection_factory=f)
    with SecretBuffer(bytearray(MARKER)) as secret:
        lost = transport.send("create_stopped", s, None, secret)
        found = transport.send("read_group", s, None, secret)
    assert lost.reason == "provider_unavailable" and lost.observation is None
    assert found.observation["provider_id"] == ID
    assert [c[0] for c in f.calls] == ["POST", "GET"]


def test_a_create_that_never_left_the_process_is_reported_not_sent():
    """A connect failure (or any local error before request bytes are written) cannot
    create a group later: it is a clean "not sent", unlike a lost reply after sending."""
    f = Factory(connect_error=OSError("Name or service not known"))
    result = run(f, "create_stopped", None)
    assert result.status == "unknown" and result.reason == "request_not_sent"
    assert result.observation["provider_rejection"]["status"] == 0
    assert f.calls == []  # nothing was requested


def test_a_local_error_before_the_post_is_reported_not_sent(monkeypatch):
    def broken(self, *args, **kwargs):
        raise TypeError("unexpected keyword argument")

    monkeypatch.setattr(SaladHTTPTransport, "_request", broken)
    result = run(Factory(), "create_stopped", None)
    assert result.reason == "request_not_sent"


def test_a_failure_after_the_post_was_sent_stays_uncertain():
    f = Factory(ConnectionResetError("reset"))
    result = run(f, "create_stopped", None)
    assert result.reason == "provider_unavailable" and result.observation is None
    assert [c[0] for c in f.calls] == ["POST"]


def test_a_read_failure_is_never_reported_as_a_create_not_sent():
    f = Factory(connect_error=OSError("down"))
    result = run(f, "read_group")
    assert result.reason == "provider_unavailable"


def test_instance_collection_preserves_actual_identity_without_death_inference():
    instance = dict(
        id=GPU,
        machine_id=GROUP,
        state="running",
        version=1,
        update_time="2026-01-01T00:00:00Z",
        ssh_ip="private",
        logs="private",
    )
    f = Factory((200, group()), (200, {"instances": [instance]}))
    result = run(f, "read_instance")
    assert result.observation["instances"] == [
        dict(instance_id=GPU.replace("-", ""), machine_id=ID, state="running", version=1)
    ]
    f = Factory((200, group()), (200, {"instances": []}))
    result = run(f, "read_instance")
    assert result.observation["instances"] == []
    assert "terminated" not in result.model_dump_json()


@pytest.mark.parametrize(
    "change",
    [dict(cpu_millicores=1500), dict(gpu_class="rtx4090"), dict(memory_mb=512), dict(disk_mb=512)],
)
def test_unrepresentable_resources_block_without_rounding(change):
    raw = selection().model_dump()
    raw["resources"].update(change)
    with pytest.raises(ValueError):
        SaladHTTPTransport(ExecutionSelection.model_validate(raw), connection_factory=Factory())


def test_secret_header_injection_and_zeroized_buffer_block_before_network():
    s = selection()
    f = Factory()
    transport = SaladHTTPTransport(s, connection_factory=f)
    for value in [bytearray(b"bad\r\nheader"), bytearray(b"\0\0")]:
        with SecretBuffer(value) as secret:
            assert transport.send("read_group", s, ID, secret).reason == "credential_unavailable"
    assert not f.calls


def test_mutating_original_nested_selection_cannot_widen_pin():
    s = selection()
    transport = SaladHTTPTransport(s, connection_factory=Factory())
    s.salad.execution.argv.append("other")
    with SecretBuffer(bytearray(MARKER)) as secret:
        assert transport.send("read_group", s, ID, secret).reason == "identity_mismatch"


def test_malformed_counts_or_bool_resources_not_accepted():
    data = copy.deepcopy(group())
    data["current_state"]["instance_status_counts"]["running_count"] = True
    assert run(Factory((200, data))).reason == "malformed_response"
    data = group()
    data["container"]["resources"]["cpu"] = True
    assert run(Factory((200, data))).reason == "identity_mismatch"


@pytest.mark.parametrize("raw", [b'{"id":1,"id":2}', b'{"id":NaN}', b"[]"])
def test_ambiguous_or_nonobject_json_rejected(raw):
    assert run(Factory((200, raw))).reason == "malformed_response"


def test_response_logging_configuration_is_not_admitted():
    data = group()
    data["container"]["logging"] = {"http": {"host": "unreviewed.invalid"}}
    assert run(Factory((200, data)), "start").reason == "identity_mismatch"


def test_missing_environment_observation_cannot_validate_even_empty_approved_env():
    s = selection()
    raw = s.model_dump(mode="json")
    raw["salad"]["execution"]["env"] = {}
    s = ExecutionSelection.model_validate(raw)
    observed = group()
    observed["container"].pop("environment_variables")
    transport = SaladHTTPTransport(s, connection_factory=Factory((200, observed)))
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = transport.send("start", s, ID, secret)
    assert result.status == "unknown"
    assert result.reason == "identity_mismatch"


def test_full_group_page_does_not_prove_reserved_name_absence():
    f = Factory((200, {"items": [{"name": "other"}] * 100}))
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert result.reason == "malformed_response" and len(f.calls) == 1


def test_preflight_does_not_count_on_call_or_other_priority_capacity():
    f = Factory(
        (200, {"items": []}),
        (
            200,
            {
                "container_groups_quotas": {
                    "container_replicas_quota": 2,
                    "container_replicas_used": 2,
                }
            },
        ),
        (200, {"items": [gpu_class()]}),
        (200, {"available_gpu_high": 0, "available_gpu_low": 9, "on_call_gpu": 99}),
    )
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert result.status == "observed"
    assert not result.observation["available"] and not result.observation["quota_ok"]


@pytest.mark.parametrize(
    "name",
    [
        "tessera-unique",
        "attempt-unique",
        "attempt-" + "a" * 31,
        "attempt-" + "a" * 33,
        "attempt-" + "g" * 32,
        "other-" + "a" * 32,
    ],
)
def test_only_existing_exact_attempt_reservation_is_accepted(name):
    raw = selection().model_dump()
    raw["salad"]["group"] = name
    with pytest.raises(ValueError, match="reserved Salad names required"):
        SaladHTTPTransport(ExecutionSelection.model_validate(raw), connection_factory=Factory())


def test_existing_admission_minimum_resources_convert_without_unit_drift():
    raw = selection().model_dump()
    raw["resources"].update(cpu_millicores=1000, memory_mb=1024, disk_mb=1024)
    s = ExecutionSelection.model_validate(raw)
    data = group()
    data["container"]["resources"].update(cpu=1, memory=1024, storage_amount=1073741824)
    f = Factory((201, data))
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).send("create_stopped", s, None, secret)
    assert result.status == "observed"
    wire = json.loads(f.calls[0][2])
    assert wire["name"] == RESERVED_GROUP
    assert wire["container"]["resources"] == data["container"]["resources"]
    assert result.observation["provider_id"] == ID


@pytest.mark.parametrize(
    "items,maximum,used,allowed",
    [
        ([group()], 1, 1, True),
        ([], 1, 1, False),
        ([group()], 1, 2, False),
        ([group()], 0, 0, False),
        # Live (2026-11-18): stopped groups are not counted; starting needs room.
        ([group()], 1, 0, True),
        ([group()], 10, 0, True),
    ],
)
def test_existing_stopped_replica_quota_counted_or_room_available(items, maximum, used, allowed):
    f = Factory(
        (200, {"items": items}),
        (
            200,
            {
                "container_groups_quotas": {
                    "container_replicas_quota": maximum,
                    "container_replicas_used": used,
                }
            },
        ),
        (200, {"items": [gpu_class()]}),
        (200, {"available_gpu_high": 1}),
    )
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert result.status == "observed"
    assert result.observation["quota_ok"] is allowed


@pytest.mark.parametrize(
    "quota",
    [
        {},
        {"container_replicas_quota": 1},
        {"container_replicas_quota": 1, "container_replicas_used": True},
        {"container_replicas_quota": 1, "container_replicas_used": -1},
    ],
)
def test_existing_group_unknown_or_malformed_quota_fails_closed(quota):
    f = Factory((200, {"items": [group()]}), (200, {"container_groups_quotas": quota}))
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert result.status == "unknown" and result.reason == "malformed_response"
    assert result.observation is None


def test_existing_mismatched_group_cannot_gain_quota_exception():
    data = group()
    data["replicas"] = 2
    f = Factory((200, {"items": [data]}))
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert result.reason == "identity_mismatch" and len(f.calls) == 1


def test_preflight_freshness_starts_before_first_of_four_requests(monkeypatch):
    times = iter(["2026-10-11T12:00:00Z", "2026-10-11T12:00:06Z"])
    monkeypatch.setattr("tessera_control.salad_http.utc_now", lambda: next(times))
    f = Factory(
        (200, {"items": [group()]}),
        (
            200,
            {
                "container_groups_quotas": {
                    "container_replicas_quota": 1,
                    "container_replicas_used": 1,
                }
            },
        ),
        (200, {"items": [gpu_class()]}),
        (200, {"available_gpu_high": 1}),
    )
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert result.observation["observed_at"] == "2026-10-11T12:00:00Z"
    assert len(f.calls) == 4


@pytest.mark.parametrize(
    "state,pending_change,active_count",
    [
        ("running", False, 0),
        ("unknown", False, 0),
        ("pending", False, 0),
        ("deploying", False, 0),
        ("stopped", True, 0),
        ("stopped", False, 1),
    ],
)
def test_start_never_posts_when_group_changes_after_stopped_preflight(
    state, pending_change, active_count
):
    changed = group()
    changed["current_state"]["status"] = state
    changed["pending_change"] = pending_change
    changed["current_state"]["instance_status_counts"]["running_count"] = active_count
    f = Factory(
        (200, {"items": [group()]}),
        (
            200,
            {
                "container_groups_quotas": {
                    "container_replicas_quota": 1,
                    "container_replicas_used": 1,
                }
            },
        ),
        (200, {"items": [gpu_class()]}),
        (200, {"available_gpu_high": 1}),
        (200, changed),
    )
    s = selection()
    transport = SaladHTTPTransport(s, connection_factory=f)
    with SecretBuffer(bytearray(MARKER)) as secret:
        preflight = transport.preflight(s, secret)
        assert preflight.status == "observed" and preflight.observation["state"] == "stopped"
        result = transport.send("start", s, ID, secret)
    assert result.status == "unknown"
    assert result.reason == ("malformed_response" if state == "unknown" else "admission_denied")
    assert len(f.calls) == 5
    assert [call[0] for call in f.calls] == ["GET", "GET", "GET", "POST", "GET"]
    assert not any(call[1].endswith("/start") for call in f.calls)


@pytest.mark.parametrize(
    "state,pending_change,active_count",
    [
        ("running", False, 0),
        ("unknown", False, 0),
        ("pending", False, 0),
        ("stopped", True, 0),
        ("stopped", False, 1),
    ],
)
def test_create_stopped_201_requires_stopped_no_pending_change_and_zero_instances(
    state, pending_change, active_count
):
    data = group()
    data["current_state"]["status"] = state
    data["pending_change"] = pending_change
    data["current_state"]["instance_status_counts"]["allocating_count"] = active_count
    f = Factory((201, data))
    result = run(f, "create_stopped", None)
    assert result.status == "unknown"
    assert result.reason == ("malformed_response" if state == "unknown" else "admission_denied")
    assert len(f.calls) == 1  # Never retry creation after an uncertain acknowledgement.


def test_stop_is_not_blocked_by_running_state_or_pending_change():
    data = group()
    data["current_state"]["status"] = "running"
    data["current_state"]["instance_status_counts"]["running_count"] = 1
    data["pending_change"] = True
    f = Factory((200, data), (202, b""))
    assert run(f, "stop").status == "pending"
    assert len(f.calls) == 2 and f.calls[-1][1].endswith("/stop")


def read_gpu(data):
    s = selection()
    f = Factory((200, data))
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).gpu_class(s, secret)
    assert not any(secret._buffer)
    assert MARKER.decode() not in result.model_dump_json()
    assert len(f.calls) == 1 and f.calls[0][:2] == (
        "GET",
        "/api/public/organizations/acme/gpu-classes",
    )
    return result


def test_gpu_read_sanitized_published_decimal_rate_not_invoice(monkeypatch):
    monkeypatch.setenv("SALAD_GPU_PRICE", "0.0001")
    data = gpu_class()
    data["name"] = "UNTRUSTED-NAME"
    data["diagnostic"] = "DO NOT RETURN"
    result = read_gpu({"items": [data]})
    assert result.status == "observed"
    assert result.observation["gpu_class_id"] == GPU
    assert result.observation["hourly_price_usd"] == "0.123456789"
    assert result.observation["price_basis"] == "published_gpu_hourly_rate_not_actual_cost"
    assert result.observation["observed_at"]
    assert not {"cost_cents", "usage", "name", "diagnostic"} & result.observation.keys()


@pytest.mark.parametrize(
    "field", ["min_vcpu", "max_vcpu", "min_ram", "max_ram", "min_storage", "max_storage"]
)
def test_gpu_schema_partial_bounds_are_unknown_not_implicitly_compatible(field):
    data = gpu_class()
    del data[field]
    result = read_gpu({"items": [data]})
    assert result.status == "unknown" and result.reason == "malformed_response"


def test_gpu_class_without_published_bounds_defers_to_provider_validation():
    # Live public gpu-classes shape (2026-11-18): id/name/type/demand/prices only.
    data = dict(
        id=GPU,
        name="RTX 4060 (8 GB)",
        gpu_class_type="community",
        is_high_demand=False,
        prices=[dict(priority="high", price="0.090"), dict(priority="batch", price="0.05")],
    )
    result = read_gpu({"items": [data]})
    assert result.status == "observed"
    assert result.observation["hourly_price_usd"] == "0.090"
    assert result.observation["resource_bounds"] == "not_published"


def test_gpu_class_with_full_bounds_reports_enforced():
    assert read_gpu({"items": [gpu_class()]}).observation["resource_bounds"] == "enforced"


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_vcpu", 3),
        ("max_vcpu", 1),
        ("min_ram", 2049),
        ("max_ram", 2047),
        ("min_storage", 2147483649),
        ("max_storage", 2147483647),
        ("gpu_count", 2),
    ],
)
def test_gpu_live_bounds_enforce_exact_cpu_mb_and_byte_units(field, value):
    data = gpu_class()
    data[field] = value
    assert read_gpu({"items": [data]}).reason == "admission_denied"


def test_gpu_bounds_are_inclusive_and_storage_accepts_int64():
    data = gpu_class()
    data.update(
        min_vcpu=2,
        max_vcpu=2,
        min_ram=2048,
        max_ram=2048,
        min_storage=2147483648,
        max_storage=2147483648,
    )
    assert read_gpu({"items": [data]}).status == "observed"
    data["max_storage"] = 268435456000
    assert read_gpu({"items": [data]}).status == "observed"


def test_gpu_count_optional_but_if_present_requires_one():
    data = gpu_class()
    del data["gpu_count"]
    assert read_gpu({"items": [data]}).status == "observed"
    data["gpu_count"] = True
    assert read_gpu({"items": [data]}).reason == "malformed_response"


@pytest.mark.parametrize("items", [[], [gpu_class(), gpu_class()], [dict(gpu_class(), id=GROUP)]])
def test_gpu_missing_or_duplicate_selected_class_fails_closed(items):
    assert read_gpu({"items": items}).reason == "identity_mismatch"


@pytest.mark.parametrize(
    "prices", [[], [dict(priority="low", price="0.1")], [dict(priority="high", price="0.1")] * 2]
)
def test_gpu_requires_exactly_one_high_priority_price(prices):
    data = gpu_class()
    data["prices"] = prices
    assert read_gpu({"items": [data]}).reason == "malformed_response"


@pytest.mark.parametrize(
    "price",
    [
        "NaN",
        "Infinity",
        "-0.1",
        "+0.1",
        "1e-2",
        " 0.1",
        "0.1 ",
        ".1",
        "01.1",
        "0",
        "0.000",
        "9" * 21,
        0.1,
        None,
        MARKER.decode(),
    ],
)
def test_gpu_price_strict_positive_finite_decimal_string_only(price):
    data = gpu_class()
    data["prices"][0]["price"] = price
    result = read_gpu({"items": [data]})
    assert result.status == "unknown" and result.reason == "malformed_response"


def test_gpu_resource_bounds_reject_boolean_negative_or_reversed():
    for changes in [
        dict(min_ram=True),
        dict(min_ram=-1),
        dict(min_ram=4096, max_ram=2048),
        dict(max_storage=2**63),
    ]:
        data = dict(gpu_class(), **changes)
        assert read_gpu({"items": [data]}).reason == "malformed_response"


@pytest.mark.parametrize("change", [dict(max_storage=2147483647), dict(prices=[]), dict(id=GROUP)])
def test_preflight_invalid_current_gpu_stops_before_availability_or_mutation(change):
    data = dict(gpu_class(), **change)
    f = Factory(
        (200, {"items": []}),
        (
            200,
            {
                "container_groups_quotas": {
                    "container_replicas_quota": 1,
                    "container_replicas_used": 0,
                }
            },
        ),
        (200, {"items": [data]}),
    )
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert result.status == "unknown"
    assert len(f.calls) == 3 and all(call[0] == "GET" for call in f.calls)


# Observed live: identical for a missing group and a missing project, no detail.
GROUP_404 = {
    "type": "https://tools.ietf.org/html/rfc9110#section-15.5.5",
    "title": "Not Found",
    "status": 404,
    "traceId": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-00",
}


def absence(*responses):
    f = Factory(*responses)
    s = selection()
    secret = SecretBuffer(bytearray(MARKER))
    with secret:
        result = SaladHTTPTransport(s, connection_factory=f).read_absence(s, secret)
    assert MARKER.decode() not in result.model_dump_json()
    return result, f


def evidence(result):
    return {k: result.observation[k] for k in ("group_not_found", "list_read")}


def test_lost_create_absence_needs_definite_404_then_reads_the_project_list():
    name = selection().salad.group
    result, f = absence((404, GROUP_404), (200, {"items": [{"name": "other"}]}))
    assert result.status == "observed"
    assert evidence(result) == dict(group_not_found=True, list_read="absent")
    assert [c[0] for c in f.calls] == ["GET", "GET"]
    assert f.calls[0][1].endswith("/" + name)
    assert f.calls[1][1] == f.calls[0][1].rsplit("/", 1)[0]
    listed, _ = absence((404, GROUP_404), (200, {"items": [{"name": name}]}))
    assert evidence(listed) == dict(group_not_found=True, list_read="present")
    # A reached, well-formed page of 100+ items without the name is only partial.
    full, _ = absence((404, GROUP_404), (200, {"items": [{"name": "x"}] * 100}))
    assert evidence(full) == dict(group_not_found=True, list_read="absent_partial")
    # A failed or malformed list read cannot prove absence.
    for reply in [
        (500, {}),
        TimeoutError(),
        (404, GROUP_404),
        (200, {"items": [{}]}),
        (200, {"items": None}),
    ]:
        unknown, _ = absence((404, GROUP_404), reply)
        assert evidence(unknown) == dict(group_not_found=True, list_read="unavailable")


@pytest.mark.parametrize("key", ["next", "next_page", "page", "continuationToken", "cursor"])
def test_paginated_group_list_does_not_prove_absence(key):
    page = {"items": [{"name": "other"}], key: "opaque"}
    result, _ = absence((404, GROUP_404), (200, page))
    assert evidence(result) == dict(group_not_found=True, list_read="unavailable")
    # A page that already shows the name still proves presence.
    page["items"].append({"name": selection().salad.group})
    result, _ = absence((404, GROUP_404), (200, page))
    assert evidence(result)["list_read"] == "present"


@pytest.mark.parametrize(
    "reply",
    [
        (500, {}),
        (503, {}),
        (401, {}),
        (403, {}),
        (429, {}),
        (302, {}),
        TimeoutError(MARKER.decode()),
        ConnectionResetError(),
    ],
)
def test_lost_create_absence_never_from_transport_error_timeout_or_5xx(reply):
    result, f = absence(reply)
    assert result.status == "unknown" and result.observation is None
    assert len(f.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        # Not ProblemDetails for this 404: conflicting status or title.
        {"title": "Not Found", "status": 500},
        {"title": "Not Found", "status": "404"},
        {"title": "Forbidden", "status": 404},
        # Gateway or load-balancer 404s: HTML, empty, plain text, JSON non-object.
        b"<html><body><h1>404 Not Found</h1></body></html>",
        b"",
        b"Not Found",
        b'["container group not found"]',
    ],
)
def test_unknown_404_body_is_unknown_not_absence(body):
    result, f = absence((404, body))
    assert result.status == "unknown" and result.observation is None
    assert len(f.calls) == 1


def test_project_not_found_is_caught_by_the_list_read_not_the_body():
    # A missing project answers the exact-name read with the same body as a missing
    # group, so only the failing list read (404 on the project) keeps it unproven.
    result, f = absence((404, GROUP_404), (404, GROUP_404))
    assert evidence(result) == dict(group_not_found=True, list_read="unavailable")
    assert len(f.calls) == 2
    # Minimal ProblemDetails (no detail, title or status) is still accepted by shape.
    minimal, _ = absence((404, {"traceId": "x"}), (200, {"items": []}))
    assert evidence(minimal) == dict(group_not_found=True, list_read="absent")


def test_404_must_be_fully_read_against_content_length():
    raw = json.dumps(GROUP_404).encode()
    short, _ = absence((404, raw, {"Content-Length": str(len(raw) + 7)}))
    assert short.status == "unknown" and short.observation is None
    bogus, _ = absence((404, raw, {"Content-Length": "abc"}))
    assert bogus.status == "unknown"
    exact, _ = absence((404, raw, {"Content-Length": str(len(raw))}), (200, {"items": []}))
    assert evidence(exact) == dict(group_not_found=True, list_read="absent")


def test_lost_create_absence_present_group_is_not_absent():
    for reply in [(200, group()), (202, b"")]:
        result, f = absence(reply)
        assert evidence(result) == dict(group_not_found=False, list_read="present")
        assert len(f.calls) == 1


def test_404_stays_a_failure_outside_the_absence_read():
    result = run(Factory((404, GROUP_404)), "read_group", None)
    assert result.status == "unknown" and result.reason == "provider_unavailable"


# ---- create rejection classification ------------------------------------------------------

REJECTED_400 = {
    "type": "https://tools.ietf.org/html/rfc9110#section-15.5.1",
    "title": "One or more validation errors occurred.",
    "status": 400,
    "detail": "storage_amount must be a whole number of GiB",
    "errors": {"container.resources.storage_amount": ["must be a multiple of 1073741824"]},
    "traceId": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-00",
}


PROBLEM = {"Content-Type": "application/problem+json; charset=utf-8"}


@pytest.mark.parametrize("status", [400, 422])
def test_clean_create_rejection_is_classified_with_a_bounded_summary(status):
    raw = json.dumps(dict(REJECTED_400, status=status)).encode()
    result = run(
        Factory((status, raw, dict(PROBLEM, **{"Content-Length": str(len(raw))}))),
        "create_stopped",
        None,
    )
    assert result.status == "unknown" and result.reason == "provider_rejected_request"
    rejection = result.observation["provider_rejection"]
    assert rejection == dict(
        status=status,
        title="One or more validation errors occurred.",
        detail="storage_amount must be a whole number of GiB",
        error_fields=["container.resources.storage_amount"],
    )
    # Values of ``errors`` are never kept; the summary is bounded and single-line.
    assert "multiple of" not in result.model_dump_json()
    long = run(
        Factory((400, dict(REJECTED_400, detail="x\n" * 500), PROBLEM)), "create_stopped", None
    )
    detail = long.observation["provider_rejection"]["detail"]
    assert len(detail) <= 200 and "\n" not in detail
    # type, title and status are required; detail is not. Plain application/json is fine.
    minimal = {k: REJECTED_400[k] for k in ("type", "title", "status")}
    result = run(
        Factory((400, minimal, {"Content-Type": "application/json"})), "create_stopped", None
    )
    assert result.observation["provider_rejection"]["detail"] is None


# Real Salad 400 bodies (read-only log-query endpoint; trace ids shortened).
LIVE_400_CODE = {
    "type": "invalid_query_syntax",
    "title": "Bad Request",
    "status": 400,
    "detail": "The query could not be parsed.",
    "instance": "31893354",
    "traceId": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-00",
}
LIVE_400_FIELD = dict(LIVE_400_CODE, detail="Invalid field name 'x'")
LIVE_400_VALIDATION = {
    "errors": {"Query": ["The Query field is required."]},
    "type": "https://tools.ietf.org/html/rfc9110#section-15.5.1",
    "title": "One or more validation errors occurred.",
    "status": 400,
    "traceId": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-00",
}


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            LIVE_400_CODE,
            dict(title="Bad Request", detail="The query could not be parsed.", error_fields=[]),
        ),
        (
            LIVE_400_FIELD,
            dict(title="Bad Request", detail="Invalid field name 'x'", error_fields=[]),
        ),
        (
            LIVE_400_VALIDATION,
            dict(
                title="One or more validation errors occurred.", detail=None, error_fields=["Query"]
            ),
        ),
    ],
)
def test_live_salad_400_shapes_are_rejections(body, expected):
    result = run(Factory((400, body, PROBLEM)), "create_stopped", None)
    assert result.reason == "provider_rejected_request"
    assert result.observation["provider_rejection"] == dict(status=400, **expected)
    assert "The Query field is required." not in result.model_dump_json()


def without(key):
    return {k: v for k, v in REJECTED_400.items() if k != key}


@pytest.mark.parametrize(
    "reply",
    [
        # Statuses that never mean a clean rejection of this create.
        (409, dict(REJECTED_400, status=409)),  # may mean the group exists
        (404, GROUP_404),
        (401, dict(REJECTED_400, status=401)),
        (403, dict(REJECTED_400, status=403)),
        (429, dict(REJECTED_400, status=429)),
        (500, dict(REJECTED_400, status=500)),
        (503, {}),
        (302, {}),
        TimeoutError(),
        ConnectionResetError(),
        # 400/422 without a ProblemDetails body, even when labelled JSON.
        (400, b"<html><body>Bad Request</body></html>", PROBLEM),
        (400, b"", PROBLEM),
        (400, b'["bad request"]', PROBLEM),
        (400, dict(REJECTED_400, status=422), PROBLEM),
        (422, dict(REJECTED_400, status="422"), PROBLEM),
        (400, dict(REJECTED_400, title=7), PROBLEM),
        (400, without("title"), PROBLEM),
        (400, without("type"), PROBLEM),
        (400, without("status"), PROBLEM),
        (400, dict(REJECTED_400, detail=["list"]), PROBLEM),
        (400, dict(REJECTED_400, errors="flat"), PROBLEM),
        (400, dict(REJECTED_400, errors=["a list"]), PROBLEM),
        (400, dict(REJECTED_400, type=""), PROBLEM),
        (400, dict(LIVE_400_CODE, instance=31893354), PROBLEM),
        (400, dict(LIVE_400_CODE, traceId=None), PROBLEM),
        (400, json.dumps(REJECTED_400).encode(), dict(PROBLEM, **{"Content-Length": "9999"})),
        # A generic gateway JSON error is not a rejection of this create.
        (400, {"message": "Bad Request", "status": 400}, {"Content-Type": "application/json"}),
        (400, {"error": "bad_request", "code": 400}, {"Content-Type": "application/json"}),
        # A ProblemDetails body without a JSON media type, or with none at all.
        (400, REJECTED_400, {"Content-Type": "text/html"}),
        (400, REJECTED_400),
    ],
)
def test_only_a_clean_problem_details_400_or_422_is_a_rejection(reply):
    result = run(Factory(reply), "create_stopped", None)
    assert result.status == "unknown"
    assert result.reason != "provider_rejected_request"
    assert result.observation is None


def test_rejection_is_only_classified_for_the_create():
    result = run(Factory((400, REJECTED_400, PROBLEM)), "read_group")
    assert result.reason == "provider_unavailable" and result.observation is None


def test_rejection_summary_containing_the_credential_is_unknown():
    body = dict(REJECTED_400, detail="echo " + MARKER.decode())
    result = run(Factory((400, body, PROBLEM)), "create_stopped", None)
    assert result.reason != "provider_rejected_request" and result.observation is None


@pytest.mark.parametrize(
    "change",
    [
        dict(disk_mb=30000),  # not a whole GiB
        dict(disk_mb=257024),  # 251 GiB
        dict(memory_mb=61441),
        dict(cpu_millicores=17000),
    ],
)
def test_transport_refuses_an_out_of_limit_shape_before_the_create_post(change):
    raw = selection().model_dump()
    raw["resources"].update(change)
    s = ExecutionSelection.model_validate(raw)
    f = Factory((201, group()))
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).send("create_stopped", s, None, secret)
    assert result.status == "unknown" and result.reason == "admission_denied"
    assert not f.calls


@pytest.mark.parametrize(
    "change,message",
    [
        (dict(disk_mb=30000), "whole number of GiB"),
        (dict(disk_mb=257024), "1..250 GiB"),  # 251 GiB
        (dict(memory_mb=61441), "1024..61440 MB"),
        (dict(cpu_millicores=17000), "1..16 vCPUs"),
        (dict(cpu_millicores=1500), "whole number of vCPUs"),
        (dict(gpu_class="rtx4090"), "UUID"),
    ],
)
def test_admission_refuses_out_of_limit_shapes_but_stored_proposals_still_parse(change, message):
    from tessera_control.remote_execution import _admit_workload
    from tessera_control.salad_workload_admission import check_workload_admission
    from tessera_control.store import Conflict

    raw = selection().model_dump(mode="json")
    raw["resources"].update(change)
    # A stored proposal outside the limits keeps parsing (status, stop, reconcile).
    stored = ExecutionSelection.model_validate_json(json.dumps(raw))
    assert stored.resources.model_dump(mode="json") == raw["resources"]
    with pytest.raises(ValueError, match=message):
        check_workload_admission(None, "project", raw)
    with pytest.raises(Conflict, match="Salad resources refused"):
        _admit_workload(None, "project", raw)
    # Starting an existing stopped group never applies the create-shape limits.
    check_workload_admission(None, "project", raw, create_shape=False)
    _admit_workload(None, "project", raw, create_shape=False)


def test_admission_accepts_documented_limits_and_cpu_only_shapes():
    from tessera_control.salad_workload_admission import check_workload_admission

    raw = selection().model_dump(mode="json")
    for edge in (
        dict(disk_mb=1024, memory_mb=1024, cpu_millicores=1000),
        dict(disk_mb=256000, memory_mb=61440, cpu_millicores=16000),
        dict(gpu_class=None, gpu_count=0),
    ):
        check_workload_admission(None, "project", dict(raw, resources=raw["resources"] | edge))


# ---- create deadline ------------------------------------------------------------------------


class SlowFactory(Factory):
    """Advances a fake monotonic clock by ``delay`` while the response is awaited."""

    def __init__(self, clock, delay, *responses):
        super().__init__(*responses)
        self.clock, self.delay = clock, delay

    def __call__(self, host, *, port, timeout):
        connection = super().__call__(host, port=port, timeout=timeout)
        original, factory = connection.getresponse, self

        def getresponse():
            factory.clock[0] += factory.delay
            return original()

        connection.getresponse = getresponse
        return connection


def test_slow_create_within_its_total_deadline_succeeds_but_reads_stay_short(monkeypatch):
    import tessera_control.salad_http as wire

    clock = [1000.0]
    monkeypatch.setattr(wire.time, "monotonic", lambda: clock[0])
    f = SlowFactory(clock, 8, (201, group()))
    result = run(f, "create_stopped", None)
    assert result.status == "observed" and f.timeouts == [wire.CREATE_DEADLINE_SECONDS]
    # Past the create deadline the same reply is unknown.
    f = SlowFactory(clock, wire.CREATE_DEADLINE_SECONDS + 1, (201, group()))
    assert run(f, "create_stopped", None).reason == "provider_unavailable"
    # An 8 s read is far past its 2 s deadline.
    f = SlowFactory(clock, 8, (200, group()))
    assert run(f, "read_group").reason == "provider_unavailable"
    assert f.timeouts == [wire.READ_DEADLINE_SECONDS]


def test_bounded_connection_bounds_dns_and_connect(monkeypatch):
    import threading

    import tessera_control.salad_http as wire

    release = threading.Event()

    def stuck_resolver(*args, **kwargs):
        release.wait(5)
        return [(None, None, None, "", ("192.0.2.1", 443))]

    monkeypatch.setattr(wire.socket, "getaddrinfo", stuck_resolver)
    monkeypatch.setattr(
        wire.socket, "create_connection", lambda *a, **k: pytest.fail("connect after DNS timeout")
    )
    connection = wire.BoundedHTTPSConnection(wire.HOST, port=443, timeout=0.2)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        connection.connect()
    assert time.monotonic() - started < 1
    release.set()
    # Connect gets only the time left, never a fresh full timeout.
    seen = []

    def connect(address, timeout):
        seen.append((address, timeout))
        raise TimeoutError("synthetic connect timeout")

    monkeypatch.setattr(
        wire.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, "", ("192.0.2.1", 443))]
    )
    monkeypatch.setattr(wire.socket, "create_connection", connect)
    connection = wire.BoundedHTTPSConnection(wire.HOST, port=443, timeout=12)
    with pytest.raises(TimeoutError):
        connection.connect()
    assert seen[0][0] == ("192.0.2.1", 443) and 0 < seen[0][1] <= 12
    # An exhausted deadline refuses before any DNS or connect.
    monkeypatch.setattr(wire.socket, "getaddrinfo", lambda *a, **k: pytest.fail("resolved"))
    connection = wire.BoundedHTTPSConnection(wire.HOST, port=443, timeout=12)
    connection._deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        connection.connect()
