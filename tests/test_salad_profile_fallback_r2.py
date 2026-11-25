"""ADR 0151 / ADR 0169 follow-ups: one transaction for a refused move, the profile record of a
stage that also lists alternate providers, and a person's reconcile of a held attempt.

Fakes only: the Salad executor, guardian, human and the CPU worker host are the test doubles of
the Salad campaign fixture and the Salad placement tests (no provider call, no network, no
money). Nothing here proves provider behavior.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
    salad_stage_v2,
)
from test_campaign_class_fallback import _attempts, _prestart_failure
from test_campaign_placement_salad import NEAR, near  # noqa: F401  (fixture)
from test_salad_profile_fallback import (
    _first_attempt_started,
    _profiles,
    _ran_unstable,
    _receipt,
    _world,
)
from test_salad_termination_confirm import (
    _ran_and_reallocated,
    _read,
    _record_outcome,
    _route_event,
)

from tessera_control import campaign_salad_fallback as fallback
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.store import canonical_digest


def _stage_status(world: SaladWorld) -> str:
    return world.row(
        "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
        world.campaign_id,
    )["status"]


# -- F1: the stop event and the stage's hold commit together ----------


def test_a_failure_between_the_stop_event_and_the_hold_leaves_neither(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _first_attempt_started(world)
    _ran_unstable(world, within=False)
    world.tick_until_phase("awaiting_charge")

    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("injected between the stop event and the stage hold")

    monkeypatch.setattr(world.runner, "_mark_stage_unknown", boom)
    world.record_charge(5)
    world.tick_until(lambda: len(calls) >= 2)
    # The injected failure rolled back the whole transaction: no stop event, no hold.
    assert world.events(fallback.STOP_EVENT) == []
    assert world.events("campaign.blocked") == []
    assert _stage_status(world) != "blocked_unknown" and world.status() == "running"
    assert world.attempt()["status"] == "failed_permanent"

    monkeypatch.undo()
    world.tick_until(lambda: world.status() == "blocked")
    [stop] = world.events(fallback.STOP_EVENT)
    assert stop["outcome"] == fallback.POST_START_REFUSED and stop["attempt"] == 1
    assert _stage_status(world) == "blocked_unknown"
    assert world.events("campaign.blocked")[-1]["reason"] == fallback.POST_START_REFUSED
    world.tick(3)
    assert len(world.events(fallback.STOP_EVENT)) == 1


# -- F3: a Salad stage with alternate profiles and an alternate provider ----------


def _two_candidate_world(tmp_path, near) -> SaladWorld:  # noqa: F811
    """A Salad stage (``gpu-main`` then ``gpu-alt1``, ``gpu-alt2``) that may also run on a CPU
    worker host (``near-cpu``). The CPU host has a free slot, but Salad is the stage's own
    provider and so is tried first."""
    world = SaladWorld(tmp_path)
    near(world, 1)
    world.profiles = _profiles(world.env)
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    stage = salad_stage_v2(world.env, "g1")
    stage["provider_config"] = {
        **stage["provider_config"],
        "alternate_profiles": ["gpu-alt1", "gpu-alt2"],
    }
    stage["retry"] = {**stage["retry"], "max_class_fallbacks": 2}
    stage["grant_ttl_seconds"] = stage["max_duration_seconds"] + 600
    stage["alternate_providers"] = [
        {"provider": NEAR, "runtime": "docker-cpu-v1", "provider_config": {}}
    ]
    world.spec = salad_spec_v2(world.env, [stage])
    return world


def _exclude(world: SaladWorld, provider_id: str) -> None:
    revision = world.row("SELECT revision FROM campaigns WHERE id=?", world.campaign_id)
    body = {
        "expected_revision": revision["revision"],
        "stage_key": "g1",
        "exclude": [provider_id],
        "reason": "keep the next attempt off this candidate",
    }
    status, _ = world.env.store.steer_campaign_placement(
        world.human, world.campaign_id, "k-steer-" + provider_id, canonical_digest(body), body
    )
    assert status == 200


def test_a_switch_whose_next_attempt_lands_elsewhere_labels_only_salad_attempts(
    tmp_path,
    near,  # noqa: F811
):
    world = _two_candidate_world(tmp_path, near)
    world.to_route_started()
    assert world.attempt()["provider_id"] == "salad"
    # The next placement may not use Salad (steering stands in for any placement skip or
    # ADR 0148 reorder): the switch recorded for attempt 2 names Salad's next profile.
    _exclude(world, "salad")
    _prestart_failure(world)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(0)
    world.tick_until(lambda: bool(world.events("campaign.stage_waiting_placement")))
    world.tick(2)
    assert len(_attempts(world)) == 1
    # Attempt 2 placed on the CPU host. The fixture's CPU host cannot take over a stage whose
    # task plan Salad prepared (``plan_mismatch``), so the rows that placement writes for it are
    # recorded directly: this test is about what the profile record says, not placement.
    stamp = datetime.now(UTC).isoformat()
    world.env.execute(
        """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status,
        hold_cents, settled_cents, failure_class, revision, created_at, updated_at,
        provider_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        "campaign_attempt_on_cpu",
        world.campaign_id,
        "g1",
        2,
        "failed_permanent",
        0,
        0,
        "task_failed",
        1,
        stamp,
        stamp,
        NEAR,
    )
    world.env.execute(
        "UPDATE campaign_stages SET provider_id=? WHERE campaign_id=? AND stage_key='g1'",
        NEAR,
        world.campaign_id,
    )

    first, second = _attempts(world)
    assert first["provider_id"] == "salad" and second["provider_id"] == NEAR
    [switch] = world.events(fallback.SWITCH_EVENT)
    assert switch["provider_id"] == "salad" and switch["to_profile"] == "gpu-alt1"
    assert (switch["from_attempt"], switch["to_attempt"]) == (1, 2)

    campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))
    from tessera_control.campaign_delegation import _normal_spec

    spec_stage = _normal_spec(campaign).stage("g1")
    with world.env.store.runner_connection() as connection:
        # The attempt that ran on the CPU host has no Salad profile; the one on Salad keeps its.
        assert fallback.attempt_profile(connection, campaign, spec_stage, dict(first)) == (
            "gpu-main"
        )
        assert fallback.attempt_profile(connection, campaign, spec_stage, dict(second)) is None
        # The next attempt back on Salad runs on (and would be labelled) the switched profile.
        later = dict(second) | {"attempt": 3, "provider_id": "salad"}
        assert fallback.attempt_profile(connection, campaign, spec_stage, later) == "gpu-alt1"
        assert fallback.candidate_profile(connection, campaign, "g1", spec_stage) == "gpu-alt1"

    # Stage status names no Salad profile for the attempt that is running on the CPU host.
    from tessera_control.agent_stage_status import salad_profile_views

    with world.env.store.runner_connection() as connection:
        [view] = salad_profile_views(connection, campaign).values()
    assert view["latest_attempt_profile"] is None
    assert [s["to_profile"] for s in view["switches"]] == ["gpu-alt1"]

    # The receipt keeps the switch history although the stage is now on the CPU host, and
    # labels only the attempt that ran on Salad.
    entry = _receipt(world)["stages"][0]
    assert entry["provider_id"] == NEAR
    [recorded] = entry["salad_profiles"]["switches"]
    assert recorded["to_profile"] == "gpu-alt1" and recorded["to_attempt"] == 2
    assert entry["salad_profiles"]["order"] == ["gpu-main", "gpu-alt1", "gpu-alt2"]
    by_number = {a["attempt"]: a for a in entry["attempts"]}
    assert by_number[1]["salad_profile"] == "gpu-main"
    assert "salad_profile" not in by_number[2]


# -- the review's limit: a person's reconcile of a held attempt never switches ----------


def _confirmed_late(world: SaladWorld) -> None:
    """A provider read of the stopped group arrives after the person already reconciled."""
    state = world.route_state()
    stop = next(e for e in state["effects"] if e["action"] == "stop")
    late = datetime.fromisoformat(stop["requested_at"]) + timedelta(seconds=30)
    observe = dict(id=uuid.uuid4().hex, action="observe", requested_at=late.isoformat())
    state["effects"].append(observe)
    read = _read(observe, 0, stopped=True)
    read["observed_at"] = (late + timedelta(milliseconds=1)).isoformat()
    state["receipts"].append(read)
    state["revision"] += 1
    _route_event(world, state)


@pytest.mark.parametrize("cents", [0, 5])
def test_a_reconciled_held_attempt_never_switches_even_when_confirmed_later(tmp_path, cents):
    world = _world(tmp_path)
    _first_attempt_started(world)
    # The route ran, was reallocated and its stop is not confirmed: the attempt is held.
    _ran_and_reallocated(world, [dict(stopped=False, allocation="alloc-b")])
    _record_outcome(world)
    world.tick_until(lambda: world.status() == "blocked")
    assert world.attempt()["status"] == "unknown"
    # A person reconciles the grant and records the charge.
    world.env.execute(
        "UPDATE execution_grants SET status='reconciled', settlement_cents=? WHERE id=?",
        cents,
        world.attempt()["grant_id"],
    )
    world.record_charge(cents)
    world.tick_until(lambda: world.attempt()["status"] == "failed_permanent")
    # The reconcile records no verdict: it can never read as a confirmed ``failed`` run.
    assert world.attempt()["salad_verdict"] is None
    _confirmed_late(world)
    world.tick(4)

    assert world.events(fallback.SWITCH_EVENT) == []
    assert world.attempt()["salad_verdict"] is None
    assert len(_attempts(world)) == 1
    assert _attempts(world)[0]["failure_class"] != "prestart_no_spend"
    assert world.status() != "running"
    with world.env.store.runner_connection() as connection:
        campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))
        assert fallback.unstable_reason(connection, campaign, dict(world.attempt())) is None


def test_preapproval_profile_reads_ignore_another_tenants_approve_event(tmp_path):
    """``covered_provider`` and ``recorded_profiles`` read the approve event of THIS campaign's
    organization and project only; a same-id event elsewhere is never consulted."""
    from tessera_control import campaign_preapproval as pre

    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    entry = {"id": "preapr_scoped", "provider_id": "other-provider", "profiles": [{"k": "v"}]}
    event = {"campaign_id": world.campaign_id, "preapprovals": [entry]}
    given = {"campaign_id": world.campaign_id, "id": "preapr_scoped"}
    # The foreign-tenant copy is ignored ...
    _insert_event(world, "org_elsewhere", "proj_elsewhere", event)
    with world.env.store.runner_connection() as connection:
        assert pre.covered_provider(connection, given, "stage-provider") == "stage-provider"
        assert pre.recorded_profiles(connection, given) is None
    # ... and this campaign's own event is read.
    _insert_event(world, campaign["org_id"], campaign["project_id"], event)
    with world.env.store.runner_connection() as connection:
        assert pre.covered_provider(connection, given, "stage-provider") == "other-provider"
        assert pre.recorded_profiles(connection, given) == [{"k": "v"}]


def _insert_event(world, org_id, project_id, payload):
    """Append a ``campaign.approved`` row for any tenant, copying the shape of the last event."""
    import json
    import sqlite3

    with sqlite3.connect(world.env.state / "tessera.sqlite3") as connection:  # FK off here
        connection.row_factory = sqlite3.Row
        columns = [r[1] for r in connection.execute("PRAGMA table_info(events)")]
        row = connection.execute("SELECT * FROM events ORDER BY cursor DESC LIMIT 1").fetchone()
        values = {
            "org_id": org_id,
            "project_id": project_id,
            "event_type": "campaign.approved",
            "payload_json": json.dumps(payload),
        }
        merged = {c: values.get(c, row[c]) for c in columns if c != "cursor"}
        connection.execute(
            f"INSERT INTO events ({','.join(merged)}) VALUES ({','.join('?' for _ in merged)})",  # noqa: S608
            tuple(merged.values()),
        )
