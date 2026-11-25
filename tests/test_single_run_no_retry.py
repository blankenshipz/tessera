"""A single run has no automatic retry: an operation that already has a route never gets a second.

README (Limits and safety): "Single runs outside campaigns have no automatic retry or scheduling."
``RemoteExecutionStore.propose`` refuses a second route for an operation that already has one
(a recorded ``remote_execution.state`` event, or a ``kubernetes_dispatches`` row): it never
retargets or retries. The refusal must write nothing and leave the first route as it was."""

from __future__ import annotations

import sqlite3

import pytest
from test_remote_execution_routing import setup_route

from tessera_control.store import Conflict

REFUSAL = "Operation already has a route; never retarget or retry it."


def _snapshot(path) -> dict[str, list]:
    """Every table's full content (the refusal must change none of it)."""
    with sqlite3.connect(path) as db:
        names = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        return {name: db.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall() for name in names}


def _second_route(h, key):
    task_id = h.route["task_id"]
    return h.store.propose(h.journey.actor, task_id, key, h.body)


SALAD_GUARD = "Salad group already reserved; one immutable group per attempt."


@pytest.mark.parametrize(
    ("target", "start", "expected"),
    [
        # A proposal only: the recorded remote_execution.state event is what refuses.
        ("kubernetes", False, REFUSAL),
        # Started: the kubernetes_dispatches row exists as well and refuses.
        ("kubernetes", True, REFUSAL),
        # Salad's own duplicate-group guard answers first for the same situation.
        ("salad_container", False, SALAD_GUARD),
    ],
)
def test_a_second_route_for_the_same_operation_is_refused_and_writes_nothing(
    tmp_path, target, start, expected
):
    h = setup_route(tmp_path, target, approve=start, start=start)
    first = h.store.get(h.journey.actor, h.route["id"])
    before = _snapshot(h.store.database_path)
    with pytest.raises(Conflict) as refused:
        _second_route(h, "route-again")
    assert expected in str(refused.value)
    assert _snapshot(h.store.database_path) == before, "the refusal wrote something"
    assert h.store.get(h.journey.actor, h.route["id"]) == first, "the first route changed"


def test_the_refusal_is_not_an_idempotent_replay_that_creates_a_route(tmp_path):
    """A different key is a new request, not a replay: still refused. The same key replays the
    first answer without a second route."""
    h = setup_route(tmp_path, "kubernetes", approve=True, start=True)
    count = _snapshot(h.store.database_path)["events"]
    for key in ("again-1", "again-2"):
        with pytest.raises(Conflict):
            _second_route(h, key)
    assert _snapshot(h.store.database_path)["events"] == count
