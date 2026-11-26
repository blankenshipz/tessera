"""Never-ready fast release and pre-create heartbeat refresh in the Salad safety service.

A reservation that never answered ``ready`` cannot have sent a create, so once it is in recovery
the guardian releases it on its own absence reads after a short settle instead of waiting out
the route deadline plus the lost-create margin. Fake provider only; no network."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec
from test_salad_safety_service import Provider, make_service, reservation

from tessera_control.salad_safety_service import (
    ABSENT_REASONS,
    LOST_CREATE_MARGIN_SECONDS,
    NEVER_READY_SETTLE_SECONDS,
    REASON_NEVER_READY_ABSENT,
)


def _service(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    provider.group_missing = True
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    s.resolver.expires_at = 100 + 86400
    return s, clock, provider


def _cancelled(tmp_path):
    """Reserve at 100 and cancel without ever calling ``ready``: recovery, never bound."""
    s, clock, provider = _service(tmp_path)
    b = reservation()
    s.loop()
    s.admin(b)
    s.admin(dict(operation="cancel", reservation=b["reservation"]))
    s.loop()
    row = s._row(b["reservation"])
    assert row["recovery"] == 1 and row["provider_id"] is None and row["last_ready_at"] is None
    return s, b, clock, provider


def _second(s, clock):
    s.loop()
    return reservation(now=clock[0], operation="b" * 32)


def _reads(provider):
    return provider.calls.count("read_absence")


def _kinds(s):
    return [r[0] for r in s.db.execute("SELECT kind FROM events ORDER BY sequence")]


def test_cancelled_never_ready_reservation_releases_on_own_absence_reads_before_margin(tmp_path):
    s, b, clock, provider = _cancelled(tmp_path)
    try:
        heartbeat = s._row(b["reservation"])["heartbeat"]
        clock[0] = heartbeat + NEVER_READY_SETTLE_SECONDS - 1
        s.loop()
        assert _reads(provider) == 0
        pending = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert pending["status"] == "reserved"
        assert pending["never_ready_eligible_at"] == heartbeat + NEVER_READY_SETTLE_SECONDS
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_second(s, clock))
        clock[0] = heartbeat + NEVER_READY_SETTLE_SECONDS
        assert clock[0] < b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        assert _reads(provider) == 1
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "reconciled" and status["reason"] == "never_ready_absent"
        assert status["reason"] == REASON_NEVER_READY_ABSENT
        assert REASON_NEVER_READY_ABSENT in ABSENT_REASONS
        record = status["lost_create"]
        assert record["kind"] == "never_ready"
        assert record["name_read"] == "not_found" and record["list_read"] == "absent"
        assert record["margin_seconds"] == NEVER_READY_SETTLE_SECONDS
        assert s._row(b["reservation"])["lost_create_resolution"] is not None
        assert "never_ready_absent_reconciled" in _kinds(s)
        clock[0] += 1
        s.loop()
        s.admin(_second(s, clock))
    finally:
        s.close()


def test_ready_reservation_is_not_released_before_the_margin(tmp_path):
    s, clock, provider = _service(tmp_path)
    try:
        b = reservation()
        s.loop()
        s.admin(b)
        s.admin(dict(operation="ready", reservation=b["reservation"]))
        s.admin(dict(operation="cancel", reservation=b["reservation"]))
        assert s._row(b["reservation"])["recovery"] == 1
        clock[0] = 100 + NEVER_READY_SETTLE_SECONDS + 5
        assert clock[0] < b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        assert _reads(provider) == 0
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        assert "never_ready_eligible_at" not in s._status(s._row(b["reservation"]))
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_second(s, clock))
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        assert s._status(s._row(b["reservation"]))["reason"] == "lost_create_absent"
    finally:
        s.close()


def test_nothing_is_read_or_released_before_the_settle(tmp_path):
    s, b, clock, provider = _cancelled(tmp_path)
    try:
        heartbeat = s._row(b["reservation"])["heartbeat"]
        for at in (heartbeat, heartbeat + NEVER_READY_SETTLE_SECONDS - 1):
            clock[0] = at
            s.loop()
            assert _reads(provider) == 0
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        with pytest.raises(ValueError, match="never_ready_settle_pending"):
            s._resolve_lost_create(s._row(b["reservation"]), never_ready=True)
        assert _reads(provider) == 0
    finally:
        s.close()


@pytest.mark.parametrize(
    "absence,list_read",
    [
        ("unknown", "absent"),
        ("raise", "absent"),
        ("present", "absent"),
        ("not_found", "unavailable"),
        ("not_found", "absent_partial"),
        ("not_found", "present"),
    ],
)
def test_unconfirmed_absence_never_releases_a_never_ready_reservation(tmp_path, absence, list_read):
    s, b, clock, provider = _cancelled(tmp_path)
    provider.absence, provider.list_read = absence, list_read
    try:
        clock[0] = s._row(b["reservation"])["heartbeat"] + NEVER_READY_SETTLE_SECONDS + 1
        s.loop()
        assert _reads(provider) == 1
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_second(s, clock))
        assert "never_ready_absent_reconciled" not in _kinds(s)
    finally:
        s.close()


def test_a_voided_fast_release_is_not_released_by_the_never_ready_path(tmp_path):
    s, b, clock, provider = _cancelled(tmp_path)
    try:
        # A group was seen under the name by the late-group watch: the fast paths never apply.
        with s.db:
            s.db.execute("UPDATE reservations SET create_rejection_voided=1")
        clock[0] = s._row(b["reservation"])["heartbeat"] + NEVER_READY_SETTLE_SECONDS + 5
        s.loop()
        assert _reads(provider) == 0
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        with pytest.raises(ValueError, match="never_ready_settle_pending"):
            s._resolve_lost_create(s._row(b["reservation"]), never_ready=True)
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_second(s, clock))
    finally:
        s.close()


def test_ready_is_refused_once_recovery_is_set(tmp_path):
    s, b, clock, provider = _cancelled(tmp_path)
    try:
        with pytest.raises(ValueError):
            s.admin(dict(operation="ready", reservation=b["reservation"]))
        assert s._row(b["reservation"])["last_ready_at"] is None
    finally:
        s.close()


def test_repeated_reserve_before_ready_refreshes_the_heartbeat(tmp_path):
    s, clock, provider = _service(tmp_path)
    try:
        b = reservation()
        s.loop()
        s.admin(b)
        assert s._row(b["reservation"])["heartbeat"] == 100.0
        clock[0] = 110.0
        s.loop()
        s.admin(b)
        assert s._row(b["reservation"])["heartbeat"] == 110.0
        clock[0] = 120.0
        s.loop()
        s.admin(b)
        row = s._row(b["reservation"])
        assert row["heartbeat"] == 120.0 and row["recovery"] == 0
    finally:
        s.close()


def test_repeated_reserve_after_ready_does_not_refresh_the_heartbeat(tmp_path):
    s, clock, provider = _service(tmp_path)
    try:
        b = reservation()
        s.loop()
        s.admin(b)
        clock[0] = 105.0
        s.loop()
        s.admin(dict(operation="ready", reservation=b["reservation"]))
        before = s._row(b["reservation"])["heartbeat"]
        clock[0] = 115.0
        s.loop()
        s.admin(b)
        row = s._row(b["reservation"])
        assert row["heartbeat"] == before and row["last_ready_at"] == 105.0
    finally:
        s.close()


def test_repeated_reserve_in_recovery_does_not_refresh_the_heartbeat(tmp_path):
    s, b, clock, provider = _cancelled(tmp_path)
    try:
        before = s._row(b["reservation"])["heartbeat"]
        clock[0] = before + 10
        s.loop()
        try:
            s.admin(b)
        except ValueError:
            pass  # a refusal is as good: the point is the unchanged heartbeat
        row = s._row(b["reservation"])
        assert row["heartbeat"] == before and row["recovery"] == 1
        assert json.dumps(row["lost_create_resolution"]) == "null"
    finally:
        s.close()
