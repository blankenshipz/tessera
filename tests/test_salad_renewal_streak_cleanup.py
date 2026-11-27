"""The pass keepalive's per-attempt renewal streak does not outlive the attempt.

An attempt that ends (control stops listing it) or is fenced (by a refusal or the transient
cap) is never renewed again, so its streak entry is dropped. Fakes only.
"""

from __future__ import annotations

from test_campaign_salad_executor import Harness, item

from tessera_control.campaign_salad_executor import MAX_CONSECUTIVE_RENEWAL_FAILURES
from tessera_control.store import Conflict


def test_an_attempt_that_is_no_longer_listed_drops_its_streak(tmp_path):
    h = Harness(tmp_path)
    h.control.renew_error = TimeoutError()
    h.control.items = [item("awaiting_review")]
    h.executor.run_once()
    assert h.executor._renewal_streak == {"att_1": 1}
    h.control.items = []  # the attempt ended without another successful renewal
    h.executor.run_once()
    assert h.executor._renewal_streak == {}


def test_a_fenced_attempt_drops_its_streak(tmp_path):
    h = Harness(tmp_path)
    h.control.renew_error = TimeoutError()
    h.control.items = [item("awaiting_review")]
    h.executor.run_once()
    h.control.renew_error = Conflict("Execution lease is no longer current.")
    h.executor.run_once()
    assert h.executor.fenced["att_1"] == "Conflict"
    assert "att_1" not in h.executor._renewal_streak


def test_the_transient_cap_fence_drops_its_streak(tmp_path):
    h = Harness(tmp_path)
    h.control.renew_error = TimeoutError()
    h.control.items = [item("awaiting_review")]
    for _ in range(MAX_CONSECUTIVE_RENEWAL_FAILURES):
        h.executor.run_once()
    assert "att_1" in h.executor.fenced
    assert h.executor._renewal_streak == {}
