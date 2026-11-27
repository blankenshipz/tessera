"""A delegation Forbidden caused by the campaign's status is transient for a forward step.

When the campaign is no longer ``running`` the delegated call is refused because of the
campaign, not the stage: the executor raises ``StepTransient`` and the intent stays ``pending``
(ADR 0038 addendum). Genuine refusals while running and reduce-only steps keep their old
behavior. Real stores and executor; the slot's kwargs are captured from a real runner pass.
"""

from __future__ import annotations

import dataclasses

import pytest
from campaign_runner_harness import World, stage_spec
from test_campaign_runner import METRIC, _spec

from tessera_control import campaign_steps
from tessera_control.campaign_steps import StepExecutor, StepFailed, StepTransient
from tessera_control.store import Forbidden


def _capture(world: World, monkeypatch, cid: str, target: str) -> dict:
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
    return world, cid, _capture(world, monkeypatch, cid, target)


def _slot(world: World, cid: str, step: str) -> list:
    return world.env.query(
        "SELECT * FROM campaign_step_intents WHERE campaign_id=? AND step=?", cid, step
    )


def _set(world: World, cid: str, status: str) -> None:
    world.env.execute("UPDATE campaigns SET status=? WHERE id=?", status, cid)


@pytest.mark.parametrize("step", ["release_snapshot", "issue_grant"])
@pytest.mark.parametrize("status", ["blocked", "cancel_requested"])
def test_forward_step_forbidden_by_campaign_status_is_transient_and_replays(
    tmp_path, monkeypatch, step, status
):
    world, cid, kwargs = _world(tmp_path, monkeypatch, step)
    _set(world, cid, status)
    failed_before = len(world.events_of("campaign.step_failed"))
    with pytest.raises(StepTransient):
        world.runner.steps.run(**kwargs)
    (intent,) = _slot(world, cid, step)
    assert intent["status"] == "pending" and intent["error_class"] is None
    assert len(world.events_of("campaign.step_failed")) == failed_before
    key = intent["idempotency_key"]

    _set(world, cid, "running")
    result = world.runner.steps.run(**kwargs)
    assert result.executed and result.status < 300
    (again,) = _slot(world, cid, step)
    assert again["id"] == intent["id"] and again["idempotency_key"] == key
    assert again["status"] == "done"
    assert len(world.events_of("campaign.step_failed")) == failed_before


def test_genuine_refusal_while_running_still_fails_the_intent(tmp_path, monkeypatch):
    world, cid, kwargs = _world(tmp_path, monkeypatch, "release_snapshot")
    real_prepare = campaign_steps.prepare

    def refusing(*args, **kw):
        prepared = real_prepare(*args, **kw)

        def call(principal, key):
            raise Forbidden("wrong resource")

        return dataclasses.replace(prepared, call=call)

    monkeypatch.setattr(campaign_steps, "prepare", refusing)
    with pytest.raises(StepFailed) as raised:
        world.runner.steps.run(**kwargs)
    assert raised.value.error_class == "Forbidden"
    (intent,) = _slot(world, cid, "release_snapshot")
    assert intent["status"] == "failed" and intent["error_class"] == "Forbidden"
    assert len(world.events_of("campaign.step_failed")) == 1
    # The recorded failure is permanent for the slot: a re-run reports it without calling.
    with pytest.raises(StepFailed):
        world.runner.steps.run(**kwargs)


def test_reduce_only_forbidden_under_blocked_keeps_the_old_failure(tmp_path, monkeypatch):
    world, cid, kwargs = _world(tmp_path, monkeypatch, "release_snapshot")
    _set(world, cid, "blocked")
    real_prepare = campaign_steps.prepare

    def refusing(*args, **kw):
        prepared = real_prepare(*args, **kw)

        def call(principal, key):
            raise Forbidden("refused")

        return dataclasses.replace(prepared, call=call)

    # Reuse the captured slot's wiring but run a reduce-only step (a fake grant revoke).
    monkeypatch.setattr(campaign_steps, "prepare", refusing)
    reduce_kwargs = {
        **kwargs,
        "step": "revoke_grant",
        "attempt": 1,
        "build": lambda: ({"grant_id": "grant_fake"}, {"expected_revision": 1}),
    }
    with pytest.raises(StepFailed) as raised:
        world.runner.steps.run(**reduce_kwargs)
    assert raised.value.error_class == "Forbidden"
    (intent,) = _slot(world, cid, "revoke_grant")
    assert intent["status"] == "failed"
