"""A human cancel of a campaign with a Salad stage is processed by runner ticks without opening
a second runner transaction inside another one, with or without an in-flight attempt.

``runner_connection()`` always begins IMMEDIATE, and SQLite reserved locks are per connection,
so a nested one on the same thread waits on itself until ``busy_timeout`` and the runner defers
the tick. A reentrancy detector refuses the nested entry at once instead of waiting.

Synthetic fixtures only: the real stores and runner, a test-driven executor; no provider call.
"""

from __future__ import annotations

import threading
import traceback
from contextlib import contextmanager

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control.campaign_store import CampaignStore


class NestedRunnerTransaction(AssertionError):
    pass


@pytest.fixture
def nested(monkeypatch):
    """Every nested ``runner_connection()`` entry on one thread, refused before BEGIN IMMEDIATE."""
    original = CampaignStore.runner_connection
    depth = threading.local()
    seen: list[str] = []

    @contextmanager
    def guarded(self):
        if getattr(depth, "value", 0):
            seen.append("".join(traceback.format_stack(limit=8)))
            raise NestedRunnerTransaction("runner_connection() opened inside another one")
        depth.value = 1
        try:
            with original(self) as connection:
                yield connection
        finally:
            depth.value = 0

    monkeypatch.setattr(CampaignStore, "runner_connection", guarded)
    return seen


class Actions(list):
    """Every action of every tick report of a world's runner."""

    def watch(self, world, monkeypatch):
        original = world.runner.tick

        def tick():
            report = original()
            self.extend(report.actions)
            return report

        monkeypatch.setattr(world.runner, "tick", tick)


@pytest.fixture
def actions():
    return Actions()


def _faults(actions):
    return [a for a in actions if a.startswith(("deferred:", "error:"))]


def test_cancel_of_an_in_flight_attempt_opens_no_nested_runner_transaction(
    tmp_path, monkeypatch, nested, actions
):
    world = SaladWorld(tmp_path)
    actions.watch(world, monkeypatch)
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], "x1")
    world.claim("create")
    world.claim("start")
    world.cancel()
    world.tick_until(lambda: world.route_state()["desired"] == "stop")
    world.tick(4)
    world.terminate_route()
    world.observe_exit()
    world.tick(3)
    world.record_charge(6)
    world.tick_until(lambda: world.status() == "cancelled")
    assert nested == [], nested[0] if nested else ""
    assert _faults(actions) == []


def test_cancel_before_any_claim_opens_no_nested_runner_transaction(
    tmp_path, monkeypatch, nested, actions
):
    world = SaladWorld(tmp_path)
    actions.watch(world, monkeypatch)
    world.to_awaiting_review()
    world.cancel()
    world.tick_until(lambda: world.status() == "cancelled")
    assert nested == [], nested[0] if nested else ""
    assert _faults(actions) == []
