# ruff: noqa: F811
"""Class fallback (ADR 0060 addendum) on a heavy stage (ADR 0107 and its 2026-11-23b amendment).

A heavy stage whose remote class failed before start with nothing spent moves to the remote's
next class, never to a local candidate the heavy guard would defer; once every remote class is
spent the stage fails closed with ``class_fallback_exhausted``, or reaches a local candidate the
spec lists only as the guard allows (recorded ``remote_skipped`` in warn mode, refused in refuse
mode). Real stores and placement; the Salad provider's class hooks are doubles offering two
classes. No provider call, no network, no money.
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_campaign_placement_default import _cpu_world
from test_campaign_placement_salad import NEAR, near  # noqa: F401

from tessera_control import campaign_class_fallback as class_fallback
from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_placement as placement
from tessera_control import campaign_placement_preference as preference
from tessera_control import program_budget
from tessera_control.campaign_provider_salad import SaladRouteProvider

HEAVY = {"placement_tags": ["heavy"]}
FAILED = {"provider_id": "salad", "failure_class": class_fallback.PRESTART_NO_SPEND}


@pytest.fixture(autouse=True)
def _reset():
    preference.configure(None, ())
    program_budget.reset()
    yield
    preference.configure(None, ())
    program_budget.reset()


@pytest.fixture
def two_classes(monkeypatch):
    """The Salad candidate offers two classes; each check on the next class is recorded (the
    class a candidate runs on comes from its recorded switch events: 1 = its next class)."""
    seen: list[int] = []

    def on_current_class(rt, ctx, key, stage):
        seen.append(1)
        return stage

    monkeypatch.setattr(SaladRouteProvider, "fallback_classes", lambda self, stage: 2)
    monkeypatch.setattr(class_fallback, "on_current_class", on_current_class)
    return seen


def _decide(world, attempts):
    ctx = world.runner._load(world.campaign_id)
    base = ctx.spec.stage("g1")
    return placement.decide(world.runner, ctx, base, lambda connection: False, attempts=attempts)


def _world(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)  # salad first by default, near-cpu has a slot
    world.start()
    return world


def test_a_heavy_stage_moves_to_the_next_remote_class_not_to_local(tmp_path, near, two_classes):
    world = _world(tmp_path, near)
    decision = _decide(world, [FAILED])
    assert decision.chosen == "salad" and decision.class_index == 1
    assert two_classes == [1]  # checked (and run) on the next class
    assert decision.candidates == ("salad", NEAR) and decision.skipped == ()
    assert decision.not_evaluated == (NEAR,)
    assert decision.heavy_guard is None  # no local fallback happened
    assert decision.payload("g1", 2)["class_index"] == 1


def test_the_next_class_is_kept_ahead_of_local_in_refuse_and_warn_modes(
    tmp_path, near, two_classes
):
    world = _world(tmp_path, near)
    for mode in ("refuse", "warn"):
        preference.configure({"heavy_local_fallback": mode}, [NEAR, "salad"])
        decision = _decide(world, [FAILED])
        assert (decision.chosen, decision.class_index) == ("salad", 1)


def test_every_remote_class_spent_fails_closed_when_local_is_not_allowed(
    tmp_path, near, two_classes, monkeypatch
):
    world = _world(tmp_path, near)
    # An operator pinned the stage to the remote: the local candidate may not serve it.
    monkeypatch.setattr(placement, "steering_for", lambda *a: {"pin": "salad", "exclude": []})
    decision = _decide(world, [FAILED, FAILED])
    assert decision.chosen is None and not decision.waiting
    assert [(s.provider_id, s.reason) for s in decision.skipped] == [
        ("salad", class_fallback.SKIP_REASON),
        (NEAR, "not_pinned"),
    ]
    assert placement.failure_reason(decision) == class_fallback.EXHAUSTED
    assert two_classes == []  # an exhausted candidate is never checked on another class


def test_every_remote_class_spent_refuses_local_in_refuse_mode(tmp_path, near, two_classes):
    world = _world(tmp_path, near)
    preference.configure({"heavy_local_fallback": "refuse"}, [NEAR, "salad"])
    decision = _decide(world, [FAILED, FAILED])
    assert decision.chosen is None
    assert [(s.provider_id, s.reason) for s in decision.skipped] == [
        ("salad", class_fallback.SKIP_REASON),
        (NEAR, guard.REFUSED_REASON),
    ]
    assert decision.heavy_guard["refused"] is True
    assert decision.heavy_guard["detail"] == [
        {"provider_id": "salad", "reason": class_fallback.SKIP_REASON}
    ]


def test_a_listed_local_after_every_remote_class_is_an_explained_recorded_fallback(
    tmp_path, near, two_classes
):
    # Warn mode (the default): the spec lists the local candidate and the remote was skipped
    # for a recorded reason, so the guard's rules admit it and say why on the placement.
    world = _world(tmp_path, near)
    decision = _decide(world, [FAILED, FAILED])
    assert decision.chosen == NEAR and decision.class_index == 0
    assert [(s.provider_id, s.reason) for s in decision.skipped] == [
        ("salad", class_fallback.SKIP_REASON)
    ]
    block = decision.heavy_guard
    assert block["warning"] is True and block["reasons"] == ["remote_skipped"]
    assert block["detail"] == [{"provider_id": "salad", "reason": class_fallback.SKIP_REASON}]
    assert "unexplained_local" not in block
