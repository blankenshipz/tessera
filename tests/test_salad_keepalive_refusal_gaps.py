"""Keepalive refusal gaps: NotFound fencing, a real lapsed-lease refusal, a cancelled heartbeat.

* A renewal refused with ``NotFound`` fences the attempt exactly like ``Conflict`` and
  ``Forbidden`` (child keepalive thread and pass keepalive) and is never retried.
* A lapsed execution lease renewed through the real ``ExecutionStore.renew_execution_lease``
  path the executor's ``StoreControl.renew`` uses raises ``Conflict``, which the executor
  classifies as a refusal (fence), not a transient failure.
* The trusted server's heartbeat on a route whose grant control cancelled is refused (its
  authority read comes before the guardian beat), so neither the guardian beat nor the lease
  is refreshed.

Temporary databases and fakes only: no network, no provider.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_salad_executor import Harness, item
from test_salad_live_check_cancel_requested import (  # noqa: F401
    _cancel_grant,
    real_route,  # noqa: F811
)

from tessera_control.campaign_salad_executor import (
    StoreControl,
    _Keepalive,
    _renewal_refused,
)
from tessera_control.store import Conflict, Forbidden, NotFound


class Refusing:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def heartbeat(self):
        return None

    def renew(self, lease_id, epoch):
        self.calls += 1
        raise self.error


# --- (1) NotFound fences, never retried ------------------------------------------------------


def test_notfound_is_a_refusal_like_conflict_and_forbidden():
    for error in (Conflict("c"), Forbidden("f"), NotFound("n")):
        assert _renewal_refused(error)
    assert not _renewal_refused(sqlite3.OperationalError("database is locked"))


def test_child_keepalive_fences_on_notfound_and_never_retries():
    control = Refusing(NotFound("Execution lease was not found."))
    keepalive = _Keepalive(control, {"att": ("lease", 1)}, 60)
    for _ in range(4):
        keepalive.beat()
    record = keepalive.snapshot()
    assert record["refused"] == {"att": "NotFound"}
    assert record["transient"] == {}
    assert "att" not in keepalive.leases
    assert control.calls == 1


def test_child_keepalive_records_a_notfound_on_its_own_lease_without_retrying():
    control = Refusing(NotFound("Execution lease was not found."))
    keepalive = _Keepalive(control, {}, 60, own=("run", ("lease", 1)))
    for _ in range(3):
        keepalive.beat()
    record = keepalive.snapshot()
    assert record["own_refused"] == "NotFound" and record["transient"] == {}
    assert control.calls == 1


def test_pass_keepalive_fences_on_notfound_and_never_retries(tmp_path):
    h = Harness(tmp_path)
    h.control.renew_error = NotFound("Execution lease was not found.")
    h.control.items = [item("awaiting_review")]
    h.executor.run_once()
    assert h.executor.fenced["att_1"] == "NotFound"
    renews = h.names().count("renew")
    assert renews == 1
    h.executor.run_once()
    h.executor.run_once()
    assert h.names().count("renew") == renews


# --- (2) a real lapsed lease is a refusal ----------------------------------------------------


def _lapse(world, lease_id):
    with closing(sqlite3.connect(world.env.state / "tessera.sqlite3")) as db, db:
        db.execute(
            "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (lease_id,),
        )


def test_a_lapsed_real_lease_is_refused_with_conflict_and_fenced(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    lease = world.lease
    control = StoreControl(world.env.state, world.worker)
    control.renew(lease["id"], lease["epoch"])  # a live lease renews through the real path
    _lapse(world, lease["id"])
    with pytest.raises(Conflict) as refused:
        control.renew(lease["id"], lease["epoch"])
    assert _renewal_refused(refused.value)  # a refusal, not a transient failure

    class Real:
        heartbeat = staticmethod(lambda: None)
        renew = staticmethod(control.renew)

    keepalive = _Keepalive(Real(), {"att": (lease["id"], lease["epoch"])}, 60)
    keepalive.beat()
    record = keepalive.snapshot()
    assert record["refused"] == {"att": "Conflict"}
    assert record["transient"] == {} and "att" not in keepalive.leases


# --- (3) a heartbeat on a cancelled route is refused, not refreshed --------------------------


def _expires(database, route_lease):
    with closing(sqlite3.connect(database)) as db:
        return db.execute(
            "SELECT expires_at, hard_expires_at FROM execution_leases WHERE id=?", (route_lease,)
        ).fetchone()


def test_a_heartbeat_on_a_cancelled_route_is_refused_and_refreshes_nothing(real_route):  # noqa: F811
    h, route, database, session = real_route
    registry_beats = []
    session._registry_heartbeat = lambda: registry_beats.append(1)
    session.keepalive(route)  # active: one guardian beat and one registry beat
    assert [b["operation"] for b in h.safety_calls].count("heartbeat") >= 1
    assert registry_beats == [1]
    lease_id = h.body["lease_id"]
    lease_before = _expires(database, lease_id)
    _cancel_grant(h, route, database)
    beats_before, calls_before = len(registry_beats), len(h.safety_calls)
    for _ in range(2):
        with pytest.raises(Conflict):
            h.server.heartbeat(route)
    # The trusted server refuses before the guardian beat: nothing is sent, and the earlier
    # verification no longer lets a stalled read beat on it.
    assert h.safety_calls[calls_before:] == []
    assert route not in getattr(h.server, "_verified_authority", {})
    for _ in range(2):
        with pytest.raises(Conflict):
            session.keepalive(route)
    # Actual behaviour of the live check's keepalive: the refusal is raised only after the
    # worker-registry beat is still attempted (a held refusal must not let the registry go
    # stale), so that one liveness write per call goes out. The guardian gets no beat and the
    # lease is not renewed (control refuses it: the grant is no longer active).
    assert h.safety_calls[calls_before:] == []
    assert len(registry_beats) == beats_before + 2
    assert _expires(database, lease_id) == lease_before
