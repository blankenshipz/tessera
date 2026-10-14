"""Isolated official-wire tests: no HTTP server, credentials, or provider calls."""

import copy
import json

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
    def __init__(self, status, data):
        self.status = status
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.offset = 0

    def read1(self, size):
        chunk = self.raw[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class Factory:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = 0

    def __call__(self, host, *, port, timeout):
        assert (host, port, timeout) == ("api.salad.com", 443, 2)
        factory = self

        class Connection:
            sock = None

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
def test_stop_refuses_group_identity_or_selection_drift(field, value):
    data = group()
    data[field] = value
    f = Factory((200, data))
    assert run(f, "stop").reason == "identity_mismatch"
    assert len(f.calls) == 1


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
        ([group()], 1, 0, False),
    ],
)
def test_existing_stopped_replica_does_not_require_additional_quota(items, maximum, used, allowed):
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
def test_gpu_schema_optional_bounds_are_unknown_not_implicitly_compatible(field):
    data = gpu_class()
    del data[field]
    result = read_gpu({"items": [data]})
    assert result.status == "unknown" and result.reason == "malformed_response"


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
