"""A non-transient store exception fails a forward step's intent instead of replaying forever.

Contention or I/O errors (and any error of a reduce-only step) leave the intent ``pending`` for
replay; any other exception raised by a forward step's store call is deterministic, so the intent
is recorded ``failed`` with the exception class and the step surfaces as ``StepFailed``.
Real stores and executor; the slot's kwargs are captured from a real runner pass.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest
from campaign_runner_harness import World, stage_spec
from test_campaign_runner import METRIC, _spec

from tessera_control import campaign_steps
from tessera_control.campaign_steps import (
    StepExecutor,
    StepFailed,
    StepTransient,
    is_transient_error,
)


def _capture(world: World, monkeypatch, target: str) -> dict:
    """Tick the real runner until it first reaches ``target``; return that slot's kwargs."""
    seen: dict = {}
    original = StepExecutor.run

    def spy(self, **kwargs):
        if kwargs["step"] == target and not seen:
            seen.update(kwargs)
            raise StepTransient("captured")
        return original(self, **kwargs)

    monkeypatch.setattr(StepExecutor, "run", spy)
    for _ in range(8):
        world.runner.tick()
        if seen:
            break
    monkeypatch.setattr(StepExecutor, "run", original)
    assert seen, f"{target} was never reached"
    return seen


def _world(tmp_path, monkeypatch, target: str) -> tuple[World, str, dict]:
    world = World(tmp_path)
    spec = _spec(world, [stage_spec(world.env, "s1", metrics=METRIC)])
    cid = world.campaign(spec)["id"]
    return world, cid, _capture(world, monkeypatch, target)


def _slot(world: World, cid: str, step: str) -> list:
    return world.env.query(
        "SELECT * FROM campaign_step_intents WHERE campaign_id=? AND step=?", cid, step
    )


def _raising(monkeypatch, error: Exception) -> list[int]:
    """Make every prepared store call raise ``error``; the list counts the calls."""
    real_prepare = campaign_steps.prepare
    calls: list[int] = []

    def patched(*args, **kw):
        prepared = real_prepare(*args, **kw)

        def call(principal, key):
            calls.append(1)
            raise error

        return dataclasses.replace(prepared, call=call)

    monkeypatch.setattr(campaign_steps, "prepare", patched)
    return calls


@pytest.mark.parametrize("error_type", [ValueError, KeyError])
def test_nontransient_error_fails_the_forward_intent(tmp_path, monkeypatch, error_type):
    world, cid, kwargs = _world(tmp_path, monkeypatch, "issue_grant")
    failed_before = len(world.events_of("campaign.step_failed"))
    calls = _raising(monkeypatch, error_type("boom"))

    with pytest.raises(StepFailed) as raised:
        world.runner.steps.run(**kwargs)
    assert raised.value.error_class == error_type.__name__
    assert raised.value.step == "issue_grant"
    assert raised.value.detail == "store call raised"
    (intent,) = _slot(world, cid, "issue_grant")
    assert intent["status"] == "failed"
    assert intent["error_class"] == error_type.__name__
    assert len(world.events_of("campaign.step_failed")) == failed_before + 1
    assert len(calls) == 1

    # The recorded failure is permanent for the slot: a re-run reports it without calling.
    with pytest.raises(StepFailed) as again:
        world.runner.steps.run(**kwargs)
    assert again.value.error_class == error_type.__name__
    assert again.value.detail == "recorded"
    assert len(calls) == 1
    assert len(world.events_of("campaign.step_failed")) == failed_before + 1


def test_busy_database_stays_transient_and_pending(tmp_path, monkeypatch):
    world, cid, kwargs = _world(tmp_path, monkeypatch, "issue_grant")
    failed_before = len(world.events_of("campaign.step_failed"))
    _raising(monkeypatch, sqlite3.OperationalError("database is locked"))

    with pytest.raises(StepTransient) as raised:
        world.runner.steps.run(**kwargs)
    assert str(raised.value) == "issue_grant: OperationalError"
    assert raised.value.deterministic is False
    (intent,) = _slot(world, cid, "issue_grant")
    assert intent["status"] == "pending" and intent["error_class"] is None
    assert len(world.events_of("campaign.step_failed")) == failed_before


def test_effect_committed_before_the_error_returns_the_recorded_result(tmp_path, monkeypatch):
    world, cid, kwargs = _world(tmp_path, monkeypatch, "issue_grant")
    real_prepare = campaign_steps.prepare

    def patched(*args, **kw):
        prepared = real_prepare(*args, **kw)

        def call(principal, key):
            status, body = prepared.call(principal, key)
            assert status < 300
            raise ValueError("after commit")

        return dataclasses.replace(prepared, call=call)

    monkeypatch.setattr(campaign_steps, "prepare", patched)
    result = world.runner.steps.run(**kwargs)
    assert result.status < 300
    (intent,) = _slot(world, cid, "issue_grant")
    assert intent["status"] == "done" and intent["error_class"] is None


def test_reduce_only_step_stays_transient_for_any_error(tmp_path, monkeypatch):
    world, cid, kwargs = _world(tmp_path, monkeypatch, "issue_grant")
    _raising(monkeypatch, ValueError("boom"))
    reduce_kwargs = {
        **kwargs,
        "step": "revoke_grant",
        "attempt": 1,
        "build": lambda: ({"grant_id": "grant_fake"}, {"expected_revision": 1}),
    }
    failed_before = len(world.events_of("campaign.step_failed"))
    with pytest.raises(StepTransient) as raised:
        world.runner.steps.run(**reduce_kwargs)
    # Replayed, but marked so the runner's breaker counts it.
    assert raised.value.deterministic is True
    assert raised.value.error_class == "ValueError"
    (intent,) = _slot(world, cid, "revoke_grant")
    assert intent["status"] == "pending" and intent["error_class"] is None
    assert len(world.events_of("campaign.step_failed")) == failed_before


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (sqlite3.OperationalError("database is locked"), True),
        (sqlite3.OperationalError("database table is locked"), True),
        (sqlite3.OperationalError("database is busy"), True),
        (sqlite3.OperationalError("disk I/O error"), True),
        (sqlite3.OperationalError("no such table: nope"), False),
        (TimeoutError("slow"), True),
        (ConnectionError("reset"), True),
        (OSError("disk"), True),
        (ValueError("bad"), False),
        (KeyError("missing"), False),
    ],
)
def test_is_transient_error_table(error, expected):
    assert is_transient_error(error) is expected
