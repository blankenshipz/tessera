"""A full project group list (100+ groups) is never paged: the alert is the closed note.

The documented list read has no pagination mechanism (ADR 0031), so absence cannot be
proven past 100 groups. The automatic release stays refused, and one closed
``project_group_list_full`` event tells the operator to reconcile the frame.
"""

import pytest
from test_salad_safety_service import LOST_CREATE_READ_SECONDS_FOR_TEST, _lost_create

from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS


def _kinds(s):
    return [r[0] for r in s.db.execute("SELECT kind FROM events")]


def _due(s, b, clock):
    clock[0] = s._row(b["reservation"])["deadline"] + LOST_CREATE_MARGIN_SECONDS + 1


def test_full_list_alerts_once_and_does_not_release(tmp_path):
    s, b, clock, provider = _lost_create(tmp_path)
    try:
        provider.list_read = "absent_partial"
        for _ in range(3):
            _due(s, b, clock)
            s.loop()
            clock[0] += LOST_CREATE_READ_SECONDS_FOR_TEST
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        assert _kinds(s).count("project_group_list_full") == 1
        detail = s.db.execute(
            "SELECT detail FROM events WHERE kind='project_group_list_full'"
        ).fetchone()[0]
        assert "100_plus" in detail
    finally:
        s.close()


@pytest.mark.parametrize("list_read", ["absent", "unavailable"])
def test_other_reads_record_no_alert(tmp_path, list_read):
    s, b, clock, provider = _lost_create(tmp_path)
    try:
        provider.list_read = list_read
        _due(s, b, clock)
        s.loop()
        assert "project_group_list_full" not in _kinds(s)
        released = s._row(b["reservation"])["lost_create_resolution"] is not None
        assert released == (list_read == "absent")
    finally:
        s.close()
