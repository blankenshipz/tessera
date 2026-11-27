"""The keepalive stops retrying a renewal that keeps failing, and never reads a stale marker.

* A lease whose renewals fail MAX_CONSECUTIVE_RENEWAL_FAILURES times in a row with a
  non-refusal error is fenced like a refusal (child thread and pass keepalive), logged loudly;
  a success in between resets the count.
* ``keepalive.json`` of an earlier run in a reused route directory is cleared when the next
  run saw nothing to report, so its counts never reach a later receipt.

Fakes only: no subprocess, no network, no live Salad.
"""

from __future__ import annotations

import logging

from test_campaign_salad_executor import RECEIPT, Harness, item

from tessera_control import campaign_salad_executor as executor_module
from tessera_control.campaign_salad_executor import (
    KEEPALIVE_MARKER,
    MAX_CONSECUTIVE_RENEWAL_FAILURES,
    TRANSIENT_CAP,
    _Keepalive,
)

CAP = MAX_CONSECUTIVE_RENEWAL_FAILURES


class Always:
    def __init__(self, error=None, ok_after=None):
        self.error, self.ok_after, self.calls = error or ValueError("bug"), ok_after, 0

    def heartbeat(self):
        return None

    def renew(self, lease_id, epoch):
        self.calls += 1
        if self.ok_after is None or self.calls not in self.ok_after:
            raise self.error


def test_child_keepalive_fences_after_the_cap_and_logs(caplog):
    control = Always()
    keepalive = _Keepalive(control, {"att": ("lease", 1)}, 60)
    with caplog.at_level(logging.ERROR):
        for _ in range(CAP + 3):
            keepalive.beat()
    record = keepalive.snapshot()
    assert record["refused"] == {"att": TRANSIENT_CAP}
    assert control.calls == CAP  # never retried past the fence
    assert "att" not in keepalive.leases
    assert any("no longer renewing" in r.getMessage() for r in caplog.records)


def test_a_success_resets_the_consecutive_count():
    control = Always(ok_after={CAP - 1})
    keepalive = _Keepalive(control, {"att": ("lease", 1)}, 60)
    for _ in range(CAP + CAP - 2):
        keepalive.beat()
    assert keepalive.snapshot()["refused"] == {}
    assert "att" in keepalive.leases


def test_pass_keepalive_fences_after_the_cap(tmp_path, caplog):
    h = Harness(tmp_path)
    h.control.renew_error = ValueError("bug")
    h.control.items = [item("awaiting_review")]
    with caplog.at_level(logging.ERROR):
        for _ in range(CAP - 1):
            h.executor.run_once()
        assert "att_1" not in h.executor.fenced
        h.executor.run_once()
    assert h.executor.fenced["att_1"] == TRANSIENT_CAP
    assert any("no longer renewing" in r.getMessage() for r in caplog.records)
    renews = h.names().count("renew")
    h.executor.run_once()
    assert h.names().count("renew") == renews


def test_a_stale_marker_is_cleared_when_the_next_run_reports_nothing(tmp_path):
    h = Harness(tmp_path)
    route_dir = h.executor._route_dir("route_1")
    route_dir.mkdir(parents=True, exist_ok=True)
    executor_module._write_private_json(route_dir / KEEPALIVE_MARKER, {"beats": 9})
    quiet = {
        "refused": {}, "transient": {}, "last_transient": {}, "beats": 3,
        "heartbeat_failures": [], "join_timed_out": False,
    }
    h.executor._save_keepalive("route_1", quiet)
    assert not (route_dir / KEEPALIVE_MARKER).exists()
    assert "executor_keepalive" not in h.executor._with_keepalive("route_1", dict(RECEIPT))
