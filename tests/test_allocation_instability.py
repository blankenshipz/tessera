# ruff: noqa: F811
"""Allocation instability per GPU class and the next attempt's fallback (ADR 0148).

Fakes only: no Salad call, no network, no money. Route states are the control plane's own
``remote_execution.state`` events written as a test double; the end-to-end tests reuse
``SaladWorld`` with a stage whose GPU tag puts its Salad GPU candidate first and a CPU host
(``near-cpu``) as the approved alternate.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from salad_campaign_fixture import (  # noqa: F401
    GPU,
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
)
from test_campaign_placement_salad import NEAR, _placed, _stage, near  # noqa: F401

from tessera_control import campaign_allocation_instability as instability
from tessera_control import campaign_models
from tessera_control import campaign_placement_preference as preference

OTHER = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _preferences():
    preference.configure(None, ())
    yield
    preference.configure(None, ())


def _on(**over):
    preference.configure({"allocation_instability": over}, ())


def _state(
    route_id="route_a",
    *,
    gpu_class=GPU,
    after=55,
    reallocated=True,
    org_id="org",
    project_id="proj",
    start=T0,
):
    """A route that ran at ``start`` and showed a new allocation ``after`` seconds later."""
    receipts = [
        {"state": "running", "observed_at": _iso(start), "allocation_id": "alloc-1"},
    ]
    if reallocated:
        receipts.append(
            {
                "state": "unknown",
                "observed_at": _iso(start + timedelta(seconds=after)),
                "allocation_id": "alloc-2",
                "proxy_reason": "reallocation_uncertain",
            }
        )
    return {
        "id": route_id,
        "org_id": org_id,
        "project_id": project_id,
        "proposal": {"selection": {"resources": {"gpu_class": gpu_class}}},
        "reallocated": reallocated,
        "receipts": receipts,
        "effects": [{"action": "create", "requested_at": _iso(start - timedelta(seconds=5))}],
    }


# -- 1. configuration ------------------------------------------------------------------------------


def test_the_setting_is_off_by_default_and_with_a_section_that_omits_it():
    assert instability.setting() is None
    preference.configure({"default_policy": True}, ())
    assert instability.setting() is None


def test_the_setting_has_conservative_defaults():
    _on()
    config = instability.setting()
    assert (config.window_seconds, config.threshold, config.lookback_seconds) == (900, 2, 86_400)


@pytest.mark.parametrize(
    "bad",
    [
        {"window_seconds": 59},
        {"window_seconds": 6 * 3600 + 1},
        {"threshold": 0},
        {"threshold": 11},
        {"lookback_seconds": 3599},
        {"lookback_seconds": 7 * 86_400 + 1},
        {"threshold": "2"},
        {"threshold": True},
        {"unknown": 1},
    ],
)
def test_malformed_settings_are_refused_and_install_nothing(bad):
    with pytest.raises((ValidationError, ValueError)):
        preference.configure({"allocation_instability": bad}, ())
    assert preference.active() is None and instability.setting() is None


# -- 2. observation of one route -------------------------------------------------------------------


def test_a_reallocation_within_the_window_is_unstable_for_its_class():
    seen = instability.observe(_state(after=55), None, 900)
    assert seen["unstable"] is True and seen["timing"] == "within"
    assert seen["gpu_class"] == GPU and seen["elapsed_seconds"] == 55
    assert seen["source"] == instability.SOURCE_ROUTE


def test_a_reallocation_outside_the_window_is_recorded_but_not_unstable():
    seen = instability.observe(_state(after=2000), None, 900)
    assert seen["unstable"] is False and seen["timing"] == "outside"
    assert seen["elapsed_seconds"] == 2000


def test_a_route_whose_allocation_never_changed_has_no_observation():
    assert instability.observe(_state(reallocated=False), None, 900) is None


def test_a_reallocation_with_no_start_time_is_not_counted():
    state = _state()
    state["receipts"][0]["state"] = "unknown"
    state["effects"] = []
    seen = instability.observe(state, None, 900)
    assert seen["unstable"] is False and seen["timing"] == "unknown"


def test_the_guardian_allocation_change_counts_by_its_upper_bound_time():
    state = _state(reallocated=False)
    final = {"allocation_uncertain": True, "observed_at": _iso(T0 + timedelta(seconds=120))}
    seen = instability.observe(state, final, 900)
    assert seen["source"] == instability.SOURCE_GUARDIAN and seen["unstable"] is True
    late = {"allocation_uncertain": True, "observed_at": _iso(T0 + timedelta(seconds=5000))}
    assert instability.observe(state, late, 900)["unstable"] is False


# -- 3. counting per class, tenant-scoped ----------------------------------------------------------


def _events_db(states, created=T0):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT,
        project_id TEXT, event_type TEXT, payload_json TEXT, created_at TEXT)"""
    )
    connection.execute(
        "CREATE TABLE campaign_salad_attempts (route_id TEXT, guardian_final_json TEXT)"
    )
    for state in states:
        connection.execute(
            "INSERT INTO events (org_id, project_id, event_type, payload_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                state["org_id"],
                state["project_id"],
                "remote_execution.state",
                json.dumps(state),
                _iso(created),
            ),
        )
    return connection


def test_counts_are_per_class_per_route_and_only_for_this_org_and_project():
    states = [
        _state("r1"),
        _state("r1"),  # a later save of the same route counts once
        _state("r2"),
        _state("r3", gpu_class=OTHER),
        _state("r4", after=4000),  # outside the window
        _state("r5", project_id="elsewhere"),
        _state("r6", org_id="other-org"),
    ]
    connection = _events_db(states)
    config = instability.AllocationInstability()
    counts = instability.class_counts(connection, "org", "proj", config, now=T0)
    assert counts == {GPU: 2, OTHER: 1}


def test_routes_older_than_the_lookback_do_not_count():
    connection = _events_db([_state("r1"), _state("r2")], created=T0 - timedelta(days=2))
    config = instability.AllocationInstability()
    assert instability.class_counts(connection, "org", "proj", config, now=T0) == {}


# -- 4. reordering (unit) ------------------------------------------------------------------------


class _Rt:
    @contextmanager
    def connection(self):
        yield None


def _unit(monkeypatch, *, classes, counts, costs=None, third_party=(), cap=20, placement=None):
    """A stage whose candidates have the given classes, unstable counts and holds."""
    ids = tuple(classes)
    costs = costs or {}
    base = SimpleNamespace(
        key="g1",
        provider=ids[0],
        max_exposure_cents=cap,
        placement_ids=tuple(placement if placement is not None else ids),
    )
    monkeypatch.setattr(
        campaign_models,
        "stage_on_candidate",
        lambda stage, pid: SimpleNamespace(provider=pid, max_exposure_cents=costs.get(pid, cap)),
    )
    monkeypatch.setattr(
        campaign_models,
        "stage_provider",
        lambda pid: SimpleNamespace(descriptor=SimpleNamespace(third_party=pid in third_party)),
    )
    monkeypatch.setattr(instability, "candidate_class", lambda stage: classes[stage.provider])
    monkeypatch.setattr(instability, "class_counts", lambda *a, **k: dict(counts))
    ctx = SimpleNamespace(campaign={"org_id": "org", "project_id": "proj"}, approval=None)
    return base, ids, ctx


def test_off_means_the_order_is_unchanged_and_nothing_is_recorded(monkeypatch):
    base, ids, ctx = _unit(monkeypatch, classes={"salad": GPU, NEAR: None}, counts={GPU: 9})
    assert instability.reorder(_Rt(), ctx, base, ids, {}) == (ids, None)


def test_below_the_threshold_nothing_moves_and_the_count_is_recorded(monkeypatch):
    _on(threshold=2)
    base, ids, ctx = _unit(monkeypatch, classes={"salad": GPU, NEAR: None}, counts={GPU: 1})
    order, record = instability.reorder(_Rt(), ctx, base, ids, {})
    assert order == ids
    assert record["outcome"] is None and record["counts"] == {GPU: 1}
    assert instability.with_outcome(record, "salad") == record


def test_at_the_threshold_an_eligible_other_class_goes_first(monkeypatch):
    _on(threshold=2)
    classes = {"gpu-a": GPU, "gpu-b": OTHER, NEAR: None}
    base, ids, ctx = _unit(monkeypatch, classes=classes, counts={GPU: 2})
    order, record = instability.reorder(_Rt(), ctx, base, ids, {})
    assert order == ("gpu-b", NEAR, "gpu-a")
    assert record["eligible"] == ["gpu-b", NEAR] and record["refused"] == []
    done = instability.with_outcome(record, "gpu-b")
    assert done["outcome"] == instability.FALLBACK
    assert (done["from_class"], done["to_class"]) == (GPU, OTHER)
    assert done["from_provider"] == "gpu-a" and done["chosen"] == "gpu-b"


def test_a_candidate_over_the_per_run_cap_is_not_a_fallback(monkeypatch):
    _on(threshold=1)
    base, ids, ctx = _unit(
        monkeypatch, classes={"gpu-a": GPU, "gpu-b": OTHER}, counts={GPU: 1}, costs={"gpu-b": 21}
    )
    order, record = instability.reorder(_Rt(), ctx, base, ids, {})
    assert order == ids
    assert record["refused"] == [{"provider_id": "gpu-b", "reason": "over_run_cap"}]
    done = instability.with_outcome(record, "gpu-a")
    assert done["outcome"] == instability.NO_ALTERNATE and done["to_class"] == GPU


def test_a_third_party_candidate_without_its_preapproval_is_refused_not_switched(monkeypatch):
    _on(threshold=1)
    base, ids, ctx = _unit(
        monkeypatch,
        classes={"gpu-a": GPU, "gpu-b": OTHER},
        counts={GPU: 1},
        third_party={"gpu-a", "gpu-b"},
    )
    order, record = instability.reorder(_Rt(), ctx, base, ids, {})
    assert order == ids  # nothing re-bound: the unstable candidate keeps its place
    assert record["refused"] == [{"provider_id": "gpu-b", "reason": "needs_approval"}]
    assert record["outcome"] == instability.NO_ALTERNATE


def test_a_candidate_on_another_unstable_class_steered_out_or_reroute_only_is_refused(
    monkeypatch,
):
    _on(threshold=1)
    classes = {"gpu-a": GPU, "gpu-b": OTHER, "far": None, "reroute": None}
    base, ids, ctx = _unit(
        monkeypatch,
        classes=classes,
        counts={GPU: 1, OTHER: 1},
        placement=("gpu-a", "gpu-b", "far"),
    )
    order, record = instability.reorder(_Rt(), ctx, base, ids, {"far": object()})
    assert order == ids
    assert {r["provider_id"]: r["reason"] for r in record["refused"]} == {
        "gpu-b": "same_class_unstable",
        "far": "excluded",
        "reroute": "not_a_placement_candidate",
    }
    assert instability.with_outcome(record, None)["outcome"] == instability.NO_ALTERNATE


def test_a_stable_head_is_never_reordered(monkeypatch):
    _on(threshold=1)
    base, ids, ctx = _unit(monkeypatch, classes={NEAR: None, "salad": GPU}, counts={GPU: 5})
    order, record = instability.reorder(_Rt(), ctx, base, ids, {})
    assert order == ids and record["outcome"] is None


# -- 5. end to end through the real runner ---------------------------------------------------------


def _world(tmp_path, near):
    """near-cpu (free slot) with a Salad GPU alternate; the GPU tag puts Salad first."""
    world = SaladWorld(tmp_path)
    near(world, 1)
    world.spec = salad_spec_v2(world.env, [{**_stage(world.env), "placement_tags": ["gpu"]}])
    return world


def _unstable(world, count, *, after=55, gpu_class=GPU):
    campaign = world.row("SELECT org_id, project_id FROM campaigns WHERE id=?", world.campaign_id)
    now = datetime.now(UTC) - timedelta(minutes=10)
    for index in range(count):
        state = _state(
            f"route_prior_{index}",
            gpu_class=gpu_class,
            after=after,
            org_id=campaign["org_id"],
            project_id=campaign["project_id"],
            start=now,
        )
        world._append("remote_execution.state", state)


def test_below_the_threshold_the_stage_still_goes_to_its_gpu_class(tmp_path, near):
    _on(threshold=2)
    world = _world(tmp_path, near)
    world.start()
    _unstable(world, 1)
    world.tick_until(lambda: bool(_placed(world)))  # Salad waits for its review
    (placed,) = _placed(world)
    assert placed["provider_id"] == "salad"
    record = placed["allocation_instability"]
    assert record["outcome"] is None and record["counts"] == {GPU: 1}


def test_at_the_threshold_the_next_attempt_falls_back_and_says_why(tmp_path, near):
    _on(threshold=2)
    world = _world(tmp_path, near)
    world.start()
    _unstable(world, 2)
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and placed["candidates"] == [NEAR, "salad"]
    record = placed["allocation_instability"]
    assert record["outcome"] == instability.FALLBACK
    assert record["from_provider"] == "salad" and record["from_class"] == GPU
    assert record["chosen"] == NEAR and record["to_class"] is None
    assert record["counts"] == {GPU: 2} and record["threshold"] == 2
    assert world.attempt()["provider_id"] == NEAR
    assert world.row("SELECT 1 FROM campaign_salad_stages") is None  # never routed or held


def test_reallocations_outside_the_window_do_not_move_the_stage(tmp_path, near):
    _on(threshold=1, window_seconds=600)
    world = _world(tmp_path, near)
    world.start()
    _unstable(world, 3, after=1200)
    world.tick_until(lambda: bool(_placed(world)))  # Salad waits for its review
    (placed,) = _placed(world)
    assert placed["provider_id"] == "salad" and "allocation_instability" not in placed


def test_the_trust_guard_still_decides_for_the_fallback_candidate(tmp_path, near, monkeypatch):
    # The fallback candidate is refused by the existing data-class trust check (ADR 0040): the
    # instability fallback is skipped with that reason and the attempt stays on the approved
    # Salad class, recorded as having no stable alternate.
    from tessera_control import provider_trust
    from tessera_control.data_class import DataClassRefused

    _on(threshold=1)
    world = _world(tmp_path, near)
    world.start()
    _unstable(world, 1)
    real = provider_trust.provider_admits

    def refused(connection, provider_id, *args, **kwargs):
        if provider_id == NEAR:
            raise DataClassRefused("restricted input may not go to this provider")
        return real(connection, provider_id, *args, **kwargs)

    monkeypatch.setattr(provider_trust, "provider_admits", refused)
    world.tick_until(lambda: bool(_placed(world)))  # Salad waits for its review
    (placed,) = _placed(world)
    assert placed["candidates"] == [NEAR, "salad"] and placed["provider_id"] == "salad"
    (skip,) = placed["skipped"]
    assert skip["provider_id"] == NEAR and skip["reason"] == "untrusted"
    record = placed["allocation_instability"]
    assert record["outcome"] == instability.NO_ALTERNATE and record["chosen"] == "salad"


def test_an_attempt_in_flight_is_never_moved(tmp_path, near):
    _on(threshold=1)
    world = _world(tmp_path, near)
    world.to_route_started()
    (before,) = _placed(world)
    attempt = world.attempt()
    assert before["provider_id"] == "salad" and attempt["provider_id"] == "salad"
    _unstable(world, 3)
    world.tick(5)
    assert len(_placed(world)) == 1  # no new placement for the attempt in flight
    after = world.attempt()
    assert (after["id"], after["provider_id"]) == (attempt["id"], "salad")
    stage = world.row(
        "SELECT provider_id FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
        world.campaign_id,
    )
    assert stage["provider_id"] == "salad"


def test_the_attempt_receipt_records_its_own_reallocation_within_the_window(tmp_path, near):
    _on(threshold=2, window_seconds=600)
    world = _world(tmp_path, near)
    world.to_route_started()
    attempt = dict(world.attempt())

    def entry():
        with world.env.store.runner_connection() as connection:
            return instability.attempt_entry(connection, attempt)

    assert entry() is None  # allocation unchanged so far
    state = world.route_state()
    start = datetime.now(UTC) - timedelta(minutes=5)
    state.update(
        _state(state["id"], gpu_class=GPU, after=55, start=start)
        | {k: state[k] for k in ("org_id", "project_id", "proposal", "effects")}
    )
    state["revision"] += 1
    world._append("remote_execution.state", state, state["revision"])
    seen = entry()
    assert seen["unstable"] is True and seen["gpu_class"] == GPU
    assert seen["route_id"] == state["id"] and seen["window_seconds"] == 600
    # Moved outside the window: recorded, not unstable.
    state["receipts"][1]["observed_at"] = _iso(start + timedelta(seconds=900))
    state["revision"] += 1
    world._append("remote_execution.state", state, state["revision"])
    seen = entry()
    assert seen["unstable"] is False and seen["timing"] == "outside"
    preference.configure(None, ())
    assert entry() is None  # off: nothing on the receipt
