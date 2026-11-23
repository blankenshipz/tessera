"""The per-frame ``max_open_billable_frames`` fence (ADR 0041 section 7.2, question 4)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec
from test_salad_safety_service import Provider, make_service, reservation


def _service(tmp_path, **kwargs):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    service = make_service(tmp_path, lambda: clock[0], key, Provider(), **kwargs)
    service.resolver.expires_at = 100 + 86400
    return service, clock


def _second(service, clock):
    service.loop()
    return reservation(now=clock[0], operation="b" * 32)


def _fence_events(service):
    rows = service.db.execute(
        "SELECT open_frames, frame_limit FROM fence_refusals ORDER BY sequence"
    ).fetchall()
    return [{"open": opened, "limit": limit} for opened, limit in rows]


@pytest.mark.parametrize("value", [0, 2, -1, True, "1", 1.0, None])
def test_the_guardian_refuses_to_start_with_any_limit_other_than_one(tmp_path, value):
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError, match="invalid_max_open_billable_frames"):
        make_service(
            tmp_path,
            lambda: 100.0,
            ec.generate_private_key(ec.SECP256R1()),
            Provider(),
            max_open_billable_frames=value,
        )


def test_the_default_is_one_and_status_reports_it(tmp_path):
    service, _clock = _service(tmp_path)
    try:
        first = reservation()
        service.loop()
        service.admin(first)
        status = service.admin(dict(operation="status", reservation=first["reservation"]))
        assert status["max_open_billable_frames"] == 1
    finally:
        service.close()


def test_a_second_open_frame_is_refused_and_the_refusal_is_recorded(tmp_path):
    service, clock = _service(tmp_path)
    try:
        first = reservation()
        service.loop()
        service.admin(first)  # an ambiguous create (no provider id) still counts as open
        assert _fence_events(service) == []
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            service.admin(_second(service, clock))
        assert _fence_events(service) == [{"open": 1, "limit": 1}]
        # The refused frame left no reservation behind.
        assert service.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
    finally:
        service.close()


def test_a_reconciled_frame_frees_the_slot_with_no_time_advance(tmp_path):
    service, clock = _service(tmp_path)
    try:
        first = reservation()
        service.loop()
        service.admin(first)
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            service.admin(_second(service, clock))
        # Proven absent (a lost create resolved as absent) reads ``reconciled``.
        with service.db:
            service.db.execute(
                "UPDATE reservations SET recovery=1, lost_create_resolution=? WHERE reservation=?",
                (json.dumps({"kind": "automatic", "resolved_at": 100.0}), first["reservation"]),
            )
        assert service._status(service._row(first["reservation"]))["status"] == "reconciled"
        second = _second(service, clock)
        service.admin(second)
        assert service._row(second["reservation"]) is not None
        assert len(_fence_events(service)) == 1  # only the earlier refusal
        assert service.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        service.close()


def test_the_total_cap_refusal_is_not_recorded_as_a_fence_refusal(tmp_path):
    service, clock = _service(tmp_path, total_cap_cents=1)
    try:
        with pytest.raises(ValueError, match="total_budget_exhausted"):
            service.loop()
            service.admin(reservation())
        assert _fence_events(service) == []
    finally:
        service.close()
