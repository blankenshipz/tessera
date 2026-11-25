"""A reserve must not cost more than one pass over the reservations (guardian admin budget).

``_open_billable_frames`` used to build the whole status of every other reservation, and a status
sums the total committed spend over all reservations: quadratic in the reservation count, past the
unix admin call's budget on a long-lived guardian. It now reads only each frame's verdict."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec
from test_salad_safety_service import GROUP, Provider, make_service, reservation

CAP = 10_000


def _service(tmp_path, **kwargs):
    tmp_path.chmod(0o700)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = Provider()
    service = make_service(tmp_path, lambda: clock[0], key, provider, **kwargs)
    service.resolver.expires_at = 100 + 86400
    return service, clock, provider


def _reserve(service, clock, index):
    body = reservation(now=clock[0], operation=f"{index:032x}")
    service.loop()
    service.admin(body)
    return body


def _close_as_absent(service, body, kind="automatic"):
    with service.db:
        service.db.execute(
            "UPDATE reservations SET recovery=1, lost_create_resolution=? WHERE reservation=?",
            (json.dumps({"kind": kind, "resolved_at": 100.0}), body["reservation"]),
        )


def _old_count(service, operation):
    """The implementation this replaces: the full status of every other reservation."""
    others = service.db.execute(
        "SELECT reservation FROM reservations WHERE operation!=?", (operation,)
    ).fetchall()
    return sum(
        1
        for (other,) in others
        if service._status(service._row(other))["status"] not in {"stopped", "reconciled"}
    )


def _many_settled(service, clock, count):
    bodies = []
    for index in range(count):
        body = _reserve(service, clock, index + 1)
        _close_as_absent(service, body)
        bodies.append(body)
    return bodies


def test_open_billable_frames_matches_status_for_every_row_kind(tmp_path):
    service, clock, _provider = _service(tmp_path, total_cap_cents=CAP)
    try:
        bodies = []
        for index in range(7):
            body = _reserve(service, clock, index + 1)
            bodies.append(body)
            if index < 6:
                # one frame open at a time: close the earlier ones so the next reserve is admitted
                _close_as_absent(service, body, kind="automatic")
        kinds = {
            0: ("automatic", True),
            1: ("create_rejected", True),
            2: ("operator", True),
        }
        for index, (kind, _closed) in kinds.items():
            _close_as_absent(service, bodies[index], kind=kind)
        with service.db:
            # recovery without an allocation and no resolution: still open (row 3); a reserved,
            # never-ready frame (row 4) and the frame bound below (row 5): open
            service.db.execute(
                "UPDATE reservations SET lost_create_resolution=NULL, recovery=1 "
                "WHERE reservation=?",
                (bodies[3]["reservation"],),
            )
            for index in (4, 5):
                service.db.execute(
                    "UPDATE reservations SET lost_create_resolution=NULL, recovery=0 "
                    "WHERE reservation=?",
                    (bodies[index]["reservation"],),
                )
        # row 5 is bound to a provider group; the guardian's answer for it is scripted below
        with service.db:
            service.db.execute(
                "UPDATE reservations SET provider_id=? WHERE reservation=?",
                (GROUP, bodies[5]["reservation"]),
            )
        op5 = service._row(bodies[5]["reservation"])["operation"]
        op6 = service._row(bodies[6]["reservation"])["operation"]
        forced = {}

        def status(operation):
            return dict(
                status=forced.get(operation, "armed"),
                reason="scripted",
                orphan=False,
                allocation_uncertain=False,
                manifest=None,
                cost="unknown",
                pending_exposure=True,
                node_writers="unknown",
                quarantine=True,
            )

        service.guardian.status = status
        for state in ("armed", "stopped", "reconciled", "unknown"):
            forced[op5] = state
            for row in service.db.execute("SELECT reservation FROM reservations").fetchall():
                service._account(service._row(row[0]))
                verdict = service._frame_verdict(service._row(row[0]))
                assert verdict == service._status(service._row(row[0]))["status"]
            operation = op6
            assert service._open_billable_frames(operation) == _old_count(service, operation)
        # the count really distinguishes the kinds: closed-absent rows are not open
        forced[op5] = "reconciled"
        assert service._open_billable_frames(op6) == 2  # rows 3 and 4
        forced[op5] = "armed"
        assert service._open_billable_frames(op6) == 3
    finally:
        service.close()


def test_open_billable_frames_is_linear(tmp_path, monkeypatch):
    service, clock, _provider = _service(tmp_path, total_cap_cents=CAP)
    try:
        bodies = _many_settled(service, clock, 40)
        accounted = []
        committed = []
        real_account, real_committed = service._account, service._committed
        monkeypatch.setattr(
            service, "_account", lambda row: (accounted.append(1), real_account(row))[1]
        )
        monkeypatch.setattr(
            service, "_committed", lambda **kw: (committed.append(1), real_committed(**kw))[1]
        )
        assert service._open_billable_frames("f" * 32) == 0
        assert len(accounted) <= 2 * len(bodies)
        assert committed == []  # the total committed spend is never summed per frame
    finally:
        service.close()


def test_reserve_with_many_settled_reservations_stays_linear(tmp_path, monkeypatch):
    service, clock, _provider = _service(tmp_path, total_cap_cents=CAP)
    try:
        count = 80
        _many_settled(service, clock, count)
        accounted = []
        real_account = service._account
        monkeypatch.setattr(
            service, "_account", lambda row: (accounted.append(1), real_account(row))[1]
        )
        service.loop()
        accounted.clear()
        service.admin(reservation(now=clock[0], operation="e" * 32))
        # One accounting pass for the fence, one for the cap: far below the old count*(count+1).
        assert len(accounted) <= 6 * count
        assert len(accounted) < count * count // 4
    finally:
        service.close()


def test_the_fence_still_refuses_one_open_frame_and_a_new_reserve(tmp_path):
    service, clock, _provider = _service(tmp_path, total_cap_cents=CAP)
    try:
        _many_settled(service, clock, 5)
        _reserve(service, clock, 100)  # open: never ready, never closed
        service.loop()
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            service.admin(reservation(now=clock[0], operation="c" * 32))
    finally:
        service.close()
