"""ADR 0204: a running frame tolerates a brief price-endpoint or provider-read outage.

A transient published-rate read failure on a running frame with a known last rate keeps the
last rate for RATE_STALE_WINDOW_SECONDS (60 s) of its observation, accruing and checking the
per-run cap at that rate; the frame is fenced when the window ends, a read shows the price
changed, or required exposure exceeds the cap. A frame that is already stopping is neither
refreshed nor fenced. The guardian retries transient group/instance reads after start for
POSTSTART_READ_WINDOW_SECONDS (60 s), which equals the unseen-run gap admission charges.
Deterministic failures, the route deadline and the per-run cap still stop at once. Fakes only.
"""

import hashlib
import time
from decimal import Decimal

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from test_salad_safety_service import (  # noqa: E402
    GROUP,
    Provider,
    claims,
    make_service,
    reservation,
    service_case,  # noqa: F401 (fixture)
    signed,
)

from tessera_control.salad_guardian import (  # noqa: E402
    POSTSTART_READ_RETRY_LIMIT,
    POSTSTART_READ_WINDOW_SECONDS,
)
from tessera_control.salad_safety_service import (  # noqa: E402
    RATE_STALE_WINDOW_SECONDS,
    UNSEEN_RUN_GAP_SECONDS,
    encoded,
    unseen_run_gap_seconds,
)

OUTAGE = 10**6  # fail every gpu_class (or group/instance) read until cleared


@pytest.fixture
def long_case(tmp_path):
    """Like ``service_case`` but with a 300 s route, so a 60 s outage fits inside it."""
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    service = make_service(tmp_path, lambda: clock[0], key, provider)
    service.retry_sleep = lambda _: None
    body = reservation(seconds=300)
    service.loop()
    service.admin(body)
    service.admin(
        dict(
            operation="bind", reservation=body["reservation"], provider_id=GROUP, allocation_id=None
        )
    )
    provider.state = "running"
    service.loop()
    request = service._request(body["proposal"]["selection"]["salad"]["execution"]["argv"])
    payload = claims()
    salad = body["proposal"]["selection"]["salad"]
    payload["salad_workload_name"] = f"{salad['organization']}-{salad['project']}-{salad['group']}"
    token = signed(key, payload)
    yield service, body, request, token, clock, provider
    service.close()


def _status(s, b):
    return s.admin(dict(operation="status", reservation=b["reservation"]))


def _row(s, b):
    return s._row(b["reservation"])


def _pass(s, b, clock, seconds=5):
    """One loop pass and one driver beat, ``seconds`` later. The heartbeat also reads the rate
    (tolerated or fenced like the loop); a refused heartbeat is returned, not raised."""
    clock[0] += seconds
    s.loop()
    try:
        s.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    except ValueError as error:
        return str(error)
    return None


def test_constants_tie_the_tolerance_to_the_charged_gap():
    assert UNSEEN_RUN_GAP_SECONDS == 60 == POSTSTART_READ_WINDOW_SECONDS
    assert RATE_STALE_WINDOW_SECONDS == POSTSTART_READ_WINDOW_SECONDS
    # One failure per 5 s poll over the window, plus the one that stops: bounded.
    assert POSTSTART_READ_RETRY_LIMIT == 13
    assert all(POSTSTART_READ_WINDOW_SECONDS <= unseen_run_gap_seconds(n) for n in range(1, 5))


def test_rate_read_failure_after_output_retained_does_not_fence_the_frame(service_case):  # noqa: F811
    s, b, request, token, clock, provider, *_ = service_case
    s.retry_sleep = lambda _: None
    assert s.workload("/v1/workload/begin-once", dict(binding=request), token)["granted"]
    output = {"schema": "isolated-output", "result": 123}
    digest = hashlib.sha256(encoded(output)).hexdigest()
    assert s.workload(
        "/v1/workload/output", dict(binding=request, output=output, sha256=digest), token
    )["retained"]
    s.loop()  # retention alone starts the stop
    operation = b["proposal"]["operation_id"]
    assert s.guardian.status(operation)["reason"] == "output_retained"
    reads = provider.calls.count("gpu_class")
    accrued = Decimal(_row(s, b)["accrued_cents"])
    provider.rate_failures = OUTAGE  # the price endpoint blips between retention and stop
    provider.rate_age = 6  # and a deterministic stale read would also have fenced
    clock[0] += 5
    s.loop()
    row = _row(s, b)
    assert not row["budget_fenced"] and not row["rate_unknown"] and not row["recovery"]
    assert provider.calls.count("gpu_class") == reads  # a stopping frame is not refreshed
    assert Decimal(row["accrued_cents"]) > accrued  # accrual at the last rate continues
    assert s.guardian.status(operation)["reason"] == "output_retained"  # not re-labelled
    provider.state = "stopped"
    clock[0] += 5
    s.loop()
    status = _status(s, b)
    assert status["status"] == "stopped" and status["retained_output"]["retained"]
    assert not status["budget_fenced"] and not status["rate_unknown"]
    assert s.admin(dict(operation="import", reservation=b["reservation"]))["output"] == output


def test_running_frame_survives_brief_rate_read_outage_and_fences_after_stale_window(long_case):
    s, b, request, token, clock, provider = long_case
    assert _pass(s, b, clock) is None
    last_good = _row(s, b)["rate_at"]
    rate = Decimal(_row(s, b)["rate"])
    provider.rate_failures = OUTAGE
    start_accrued, start = Decimal(_row(s, b)["accrued_cents"]), clock[0]
    while clock[0] + 5 - last_good < RATE_STALE_WINDOW_SECONDS:
        assert _pass(s, b, clock) is None  # heartbeat accepted while the rate is stale
        row = _row(s, b)
        assert not row["budget_fenced"] and not row["rate_unknown"] and not row["recovery"]
        assert s.guardian.status(b["proposal"]["operation_id"])["status"] == "armed"
    assert "stop" not in provider.calls
    row = _row(s, b)
    # Accrual continued at the last known rate for the whole stale stretch.
    expected = start_accrued + rate * Decimal(str(clock[0] - start)) * 100 / 3600
    assert abs(Decimal(row["accrued_cents"]) - expected) < Decimal("1e-15")
    assert row["rate"] == format(rate, "f") and row["rate_at"] == last_good
    assert _status(s, b)["last_rate_read_failure"] == "provider_unknown"
    # The window ends: the next failed read fences and stops, exactly as before ADR 0204.
    assert _pass(s, b, clock) in {"rate_unknown", "stop_only"}
    row = _row(s, b)
    assert row["budget_fenced"] and row["rate_unknown"] and row["recovery"]
    assert "stop" in provider.calls


def test_brief_rate_outage_then_recovery_keeps_running(long_case):
    s, b, request, token, clock, provider = long_case
    provider.rate_failures = OUTAGE
    for _ in range(6):  # 30 s of failed price reads
        assert _pass(s, b, clock) is None
    provider.rate_failures = 0
    assert _pass(s, b, clock) is None
    row = _row(s, b)
    assert not row["budget_fenced"] and not row["rate_unknown"] and not row["recovery"]
    assert 0 <= clock[0] - row["rate_at"] <= 5  # a fresh observation ended the window
    assert "stop" not in provider.calls


def test_price_change_seen_after_an_outage_fences(long_case):
    s, b, request, token, clock, provider = long_case
    provider.rate_failures = OUTAGE
    assert _pass(s, b, clock) is None
    provider.rate_failures, provider.rate = 0, "0.2"
    assert _pass(s, b, clock) in {"exposure_fenced", "stop_only"}
    row = _row(s, b)
    assert row["budget_fenced"] and row["rate_unknown"] and row["recovery"]


@pytest.mark.parametrize("failure", ["stale", "mismatch"])
def test_a_deterministic_rate_failure_on_a_running_frame_still_fences_at_once(long_case, failure):
    s, b, request, token, clock, provider = long_case
    if failure == "stale":
        provider.rate_age = 6
    else:
        provider.rate_error = ValueError("rate_observation_invalid")
    clock[0] += 5
    s.loop()
    row = _row(s, b)
    assert row["budget_fenced"] and row["rate_unknown"] and row["recovery"]


def test_a_rate_outage_before_running_still_fences(tmp_path):
    """Pre-start (no billing yet, no last running rate) keeps the fail-closed rule."""
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    s.retry_sleep = lambda _: None
    try:
        body = reservation(seconds=300)
        s.loop()
        s.admin(body)
        s.admin(
            dict(
                operation="bind",
                reservation=body["reservation"],
                provider_id=GROUP,
                allocation_id=None,
            )
        )
        provider.state, provider.instance_state = "deploying", "creating"
        provider.rate_failures = OUTAGE
        clock[0] += 5
        s.loop()
        row = _row(s, body)
        assert row["first_running_at"] is None
        assert row["budget_fenced"] and row["rate_unknown"]
    finally:
        s.close()


def test_per_run_cap_still_fences_during_the_stale_window(long_case):
    s, b, request, token, clock, provider = long_case
    provider.rate_failures = OUTAGE
    assert _pass(s, b, clock) is None
    # Accrual has nearly reached the per-run cap: required exposure at the last known rate
    # over the remaining window no longer fits, so the frame is fenced inside the window.
    with s.db:
        s.db.execute(
            "UPDATE reservations SET accrued_cents=? WHERE reservation=?",
            (format(Decimal(_row(s, b)["cost_cap"]) - Decimal("0.01"), "f"), b["reservation"]),
        )
    assert _pass(s, b, clock) in {"exposure_fenced", "stop_only"}
    row = _row(s, b)
    assert row["budget_fenced"] and row["recovery"]
    assert "stop" in provider.calls


def test_route_deadline_still_stops_during_the_stale_window(service_case):  # noqa: F811
    s, b, request, token, clock, provider, *_ = service_case
    s.retry_sleep = lambda _: None
    provider.rate_failures = OUTAGE
    deadline = s._stop_deadline(_row(s, b))
    assert deadline - _row(s, b)["rate_at"] < RATE_STALE_WINDOW_SECONDS
    while clock[0] < deadline:
        _pass(s, b, clock)
    row = _row(s, b)
    assert row["recovery"] and "stop" in provider.calls


def test_running_group_survives_60_second_provider_read_outage_within_charged_gap(long_case):
    s, b, request, token, clock, provider = long_case
    operation = b["proposal"]["operation_id"]
    assert _pass(s, b, clock) is None
    provider.read_failures = OUTAGE  # every group/instance read fails (a Salad brown-out)
    first = None
    while True:
        refused = _pass(s, b, clock)
        status = s.guardian.status(operation)
        if first is None and status.get("poststart_read_retries"):
            first = status["poststart_read_retries"]["first_at"]
        if status["stop_cause"] is not None:
            break
        assert refused is None
        assert first is None or clock[0] - first < POSTSTART_READ_WINDOW_SECONDS + 15
        assert "stop" not in provider.calls
    assert first is not None
    # The stop came only once the 60 s window was spent, never after it plus one poll backoff,
    # and never later than the unseen-run gap admission charged on top of the running window.
    stopped_at = clock[0]
    assert POSTSTART_READ_WINDOW_SECONDS <= stopped_at - first <= UNSEEN_RUN_GAP_SECONDS + 15
    assert status["stop_cause"] == "provider_unavailable" and "stop" in provider.calls
    assert status["poststart_read_retries"]["count"] < POSTSTART_READ_RETRY_LIMIT


def test_provider_read_outage_shorter_than_the_window_keeps_running(long_case):
    s, b, request, token, clock, provider = long_case
    operation = b["proposal"]["operation_id"]
    assert _pass(s, b, clock) is None
    provider.read_failures = OUTAGE
    started = time.monotonic()
    for _ in range(10):  # 50 s of failed group reads, polled at the guardian's backoff
        assert _pass(s, b, clock) is None
    provider.read_failures = 0
    for _ in range(4):
        assert _pass(s, b, clock) is None
    status = s.guardian.status(operation)
    assert status["status"] == "armed" and status["stop_cause"] is None
    assert status["poststart_read_retries"]["count"] >= 4
    assert "stop" not in provider.calls
    assert time.monotonic() - started < 30
