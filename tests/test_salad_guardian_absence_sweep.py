"""The guardian's lost-create absence reads never starve its IPC, at a restart or later.

Every loop pass is also when reserve, bind and ticket IPC is answered, so the provider reads of
the late-group watch and of lost-create re-resolution are spread out:

* at most ``LOST_CREATE_READS_PER_PASS`` absence reads per pass, on every pass (the periodic
  watch as well as a restart's backlog);
* a frame first seen by a new process (a restart) gets its first watch read spread over one
  watch interval, not all at once in the first pass;
* when the guardian's own credential is not ready, no read is made, so an already resolved
  (``lost_create_absent``) frame is not voided; a read that was made and did not prove absence
  still voids, as before.

Real guardian service and store on a fake provider; no network.
"""

from __future__ import annotations

import pytest
from test_salad_safety_service import _absence_reads, _kinds, _resolved

from tessera_control.salad_safety_service import (
    LOST_CREATE_READ_SECONDS,
    LOST_CREATE_WATCH_INTERVAL_SECONDS,
)


def _restart(s):
    """What a new guardian process knows: nothing in memory about read schedules."""
    s.next_absence.clear()


def test_a_restart_spreads_the_first_watch_read_over_one_interval(tmp_path):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        reservation = b["reservation"]
        _restart(s)
        now = clock[0]
        due = s._watch_due(reservation, now)
        assert now <= due < now + LOST_CREATE_WATCH_INTERVAL_SECONDS
        # Stable: the same frame gets the same offset on every restart.
        _restart(s)
        assert s._watch_due(reservation, now) == due
        _restart(s)
        reads = _absence_reads(provider)
        s.loop()  # the new process first sees the frame: its first read is scheduled
        clock[0] += LOST_CREATE_WATCH_INTERVAL_SECONDS
        s.loop()
        s.loop()
        assert _absence_reads(provider) <= reads + 2
        assert _absence_reads(provider) >= reads + 1
        # A definite absence keeps the resolution (idempotent, nothing re-voided).
        assert s._row(reservation)["lost_create_resolution"] is not None
        assert "lost_create_resolution_voided" not in _kinds(s)
    finally:
        s.close()


def test_the_periodic_watch_makes_one_read_per_interval_and_never_voids_on_absence(tmp_path):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        reads = _absence_reads(provider)
        for _ in range(5):
            clock[0] += LOST_CREATE_WATCH_INTERVAL_SECONDS + 1
            s.loop()
            s.loop()  # a second pass inside the interval reads nothing more
        assert _absence_reads(provider) == reads + 5
        assert s._row(b["reservation"])["lost_create_resolution"] is not None
        assert "lost_create_resolution_voided" not in _kinds(s)
    finally:
        s.close()


def test_a_pass_spends_at_most_one_absence_read(tmp_path):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        clock[0] += LOST_CREATE_WATCH_INTERVAL_SECONDS + 1
        s._absence_reads_left = 0  # the slot already went to another frame this pass
        frame_reads = _absence_reads(provider)
        s._loop_frame(b["reservation"], clock[0])
        assert _absence_reads(provider) == frame_reads
        # The next pass has a slot again and reads it.
        s.loop()
        assert _absence_reads(provider) == frame_reads + 1
    finally:
        s.close()


def test_an_unready_credential_skips_the_watch_read_without_voiding(tmp_path, monkeypatch):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        reads = _absence_reads(provider)

        def unready():
            raise ValueError("credential_unavailable")

        monkeypatch.setattr(s, "_credential", unready)
        clock[0] += LOST_CREATE_WATCH_INTERVAL_SECONDS + 1
        s.loop()
        assert _absence_reads(provider) == reads
        assert s._row(b["reservation"])["lost_create_resolution"] is not None
        assert "lost_create_resolution_voided" not in _kinds(s)
        # Retried soon, not a whole interval later; once ready the read is made.
        assert s.next_absence[b["reservation"]] == clock[0] + LOST_CREATE_READ_SECONDS
        monkeypatch.undo()
        clock[0] += LOST_CREATE_READ_SECONDS
        s.loop()
        assert _absence_reads(provider) == reads + 1
        assert s._row(b["reservation"])["lost_create_resolution"] is not None
    finally:
        s.close()


@pytest.mark.parametrize("mode", ["unknown", "raise"])
def test_a_read_that_was_made_and_did_not_prove_absence_still_voids(tmp_path, mode):
    s, b, clock, provider = _resolved(tmp_path)
    try:
        provider.absence = mode
        clock[0] += LOST_CREATE_WATCH_INTERVAL_SECONDS + 1
        s.loop()
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        assert _kinds(s)[-1] == "lost_create_resolution_voided"
    finally:
        s.close()
