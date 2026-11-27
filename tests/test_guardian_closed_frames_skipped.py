"""A guardian pass is not O(history): closed frames are not re-accounted or re-walked (fakes).

A bound frame the guardian stopped (or reconciled) is final. Its estimate is frozen and it
accrues nothing more, so a pass neither re-writes its row nor walks its close duties again once
one pass of this process has closed it. Open frames are still accounted every pass, and the
aggregate total-cap sum still counts the stopped frame's frozen estimate unchanged.
"""

from __future__ import annotations

from decimal import Decimal

from test_salad_safety_concurrent_frames import (
    GroupRateProvider,
    _bind,
    _reserve,
    _service,
    _status,
)


def _stopped_and_open(tmp_path):
    provider = GroupRateProvider()
    service, clock, _ = _service(tmp_path, provider, max_open_billable_frames=2, total_cap_cents=40)
    a = _reserve(service, clock, "a" * 32, seconds=300, workload_seconds=60)
    b = _reserve(service, clock, "b" * 32, seconds=300, workload_seconds=60)
    _bind(service, a)
    provider.state = "running"
    for _ in range(3):
        clock[0] += 5
        service.loop()
        for body in (a, b):
            service.admin(dict(operation="heartbeat", reservation=body["reservation"]))
    service.admin(dict(operation="cancel", reservation=a["reservation"]))
    provider.state = "stopped"
    clock[0] += 5
    service.loop()
    service.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    assert _status(service, a)["status"] == "stopped"
    return service, clock, a, b


def _spy_frames(service):
    seen = []
    real = service._loop_frame

    def frame(reservation, now):
        seen.append(reservation)
        return real(reservation, now)

    service._loop_frame = frame
    return seen


def _spy_writes(service):
    """Count UPDATEs that touch the reservations table, by reservation argument."""
    written = []
    real = service.db

    class Recording:
        def __getattr__(self, name):
            return getattr(real, name)

        def __enter__(self):
            return real.__enter__()

        def __exit__(self, *exc):
            return real.__exit__(*exc)

        def execute(self, sql, args=()):
            if sql.lstrip().startswith("UPDATE reservations SET accrued_cents"):
                written.append(args[-1])
            return real.execute(sql, args)

    service.db = Recording()
    return written


def test_a_closed_frame_is_not_rewritten_or_walked_each_pass(tmp_path):
    service, clock, a, b = _stopped_and_open(tmp_path)
    try:
        # One more pass closes A for this process (its close duties run once).
        clock[0] += 5
        service.loop()
        service.admin(dict(operation="heartbeat", reservation=b["reservation"]))
        assert a["reservation"] in service._closed_frames
        frozen = service._row(a["reservation"])
        committed = service._committed(account=False)

        seen, written = _spy_frames(service), _spy_writes(service)
        for _ in range(5):
            clock[0] += 5
            service.loop()
            service.admin(dict(operation="heartbeat", reservation=b["reservation"]))
        # A is neither walked nor re-written (also not by the aggregate cap's accounting).
        assert a["reservation"] not in seen
        assert a["reservation"] not in written
        assert service._row(a["reservation"]) == frozen
        # B, still open, is walked and accounted every pass.
        assert seen.count(b["reservation"]) == 5
        assert written.count(b["reservation"]) >= 5
        assert service._row(b["reservation"])["accounted_at"] == clock[0]
        # The aggregate counts A's frozen estimate and B's hold, exactly as before.
        hold_b = Decimal(10)
        expected = Decimal(frozen["accrued_cents"]) + max(
            Decimal(service._row(b["reservation"])["accrued_cents"]), hold_b
        )
        assert service._committed(account=False) == committed == expected
        assert service._committed() == expected
        assert Decimal(_status(service, b)["total_committed_cents"]) == expected
    finally:
        service.close()


def test_a_restarted_process_walks_a_closed_frame_once_then_skips_it(tmp_path):
    service, clock, a, b = _stopped_and_open(tmp_path)
    try:
        service._closed_frames.clear()  # what a fresh process starts with
        seen = _spy_frames(service)
        for _ in range(3):
            clock[0] += 5
            service.loop()
            service.admin(dict(operation="heartbeat", reservation=b["reservation"]))
        assert seen.count(a["reservation"]) == 1
        assert seen.count(b["reservation"]) == 3
    finally:
        service.close()


def test_an_open_frame_is_still_accounted_and_rewritten(tmp_path):
    provider = GroupRateProvider()
    service, clock, _ = _service(tmp_path, provider, max_open_billable_frames=2, total_cap_cents=40)
    try:
        a = _reserve(service, clock, "a" * 32, seconds=300, workload_seconds=60)
        _bind(service, a)
        provider.state = "running"
        accrued = []
        for _ in range(3):
            clock[0] += 5
            service.loop()
            service.admin(dict(operation="heartbeat", reservation=a["reservation"]))
            accrued.append(Decimal(service._row(a["reservation"])["accrued_cents"]))
        assert accrued[0] < accrued[1] < accrued[2]
        assert service._row(a["reservation"])["accounted_at"] == clock[0]
        assert not service._closed_frames
    finally:
        service.close()
