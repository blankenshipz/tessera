"""A never-ready frame frees its per-run hold in the total at resolution; others keep it.

A reservation that never answered ``ready`` cannot have sent a create, so its hold is released
at once (as for ``create_rejected``). An ``automatic`` resolution may follow a sent create and
keeps the hold for the 24 h late-group watch. Fake provider only; no network."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")
from test_salad_never_ready_release import _cancelled, _second
from test_salad_safety_service import GROUP, _kinds, _lost_create, _next_reservation

from tessera_control.salad_safety_service import (
    LOST_CREATE_MARGIN_SECONDS,
    LOST_CREATE_WATCH_SECONDS,
    NEVER_READY_SETTLE_SECONDS,
)


def _hold(b):
    return b["proposal"]["selection"]["salad"]["execution"]["max_cost_cents"]


def test_never_ready_resolution_frees_its_hold_at_once(tmp_path):
    s, b, clock, provider = _cancelled(tmp_path)
    try:
        clock[0] = s._row(b["reservation"])["heartbeat"] + NEVER_READY_SETTLE_SECONDS
        assert s._committed() == _hold(b)
        s.loop()
        assert s._row(b["reservation"])["lost_create_resolution"] is not None
        assert s._committed() == 0
        clock[0] += 1
        s.loop()
        s.admin(_second(s, clock))
    finally:
        s.close()


def test_automatic_resolution_keeps_its_hold_for_the_watch(tmp_path):
    s, b, clock, provider = _lost_create(tmp_path)
    try:
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        record = s._status(s._row(b["reservation"]))["lost_create"]
        assert record["kind"] == "automatic"
        assert s._committed() == _hold(b)
        clock[0] = record["resolved_at"] + LOST_CREATE_WATCH_SECONDS - 1
        assert s._committed(account=False) == _hold(b)
        clock[0] = record["resolved_at"] + LOST_CREATE_WATCH_SECONDS
        assert s._committed(account=False) == 0
    finally:
        s.close()


def test_late_group_under_a_never_ready_name_still_reopens_and_blocks(tmp_path):
    s, b, clock, provider = _cancelled(tmp_path)
    try:
        clock[0] = s._row(b["reservation"])["heartbeat"] + NEVER_READY_SETTLE_SECONDS
        s.loop()
        assert s._committed() == 0
        provider.absence = "present"
        provider.group_missing = False
        provider.state = "running"
        clock[0] += 61
        s.loop()
        row = s._row(b["reservation"])
        assert row["lost_create_resolution"] is None and row["provider_id"] == GROUP
        assert "lost_create_late_group_observed" in _kinds(s)
        assert s._committed() == _hold(b)
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
    finally:
        s.close()
