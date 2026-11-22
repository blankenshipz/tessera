# ruff: noqa: F811
"""Heavy-stage placement guard (ADR 0107). Fakes only: no provider call, no network, no money.

The end-to-end tests reuse the default-placement world: a ``near-cpu`` worker-grant provider
(local) as the stage's own provider with a Salad CPU alternate, and a Salad campaign profile
registered for the project by ``SaladWorld``.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest
from salad_campaign_fixture import salad_provider_reset, salad_spec_v2  # noqa: F401
from test_campaign_placement_default import (  # noqa: F401
    FAR,
    GIB,
    _cpu_world,
    _normal,
    local_providers,
)
from test_campaign_placement_salad import NEAR, _placed, _stage, near  # noqa: F401

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_placement as placement
from tessera_control import campaign_placement_preference as preference
from tessera_control.campaign_provider_salad import SaladRouteProvider, register_provider_instance
from tessera_control.store import Conflict

HEAVY = {"placement_tags": ["heavy"]}


@pytest.fixture(autouse=True)
def _no_preference():
    preference.configure(None, ())
    yield
    preference.configure(None, ())


def _salad_not_enrolled(monkeypatch):
    def refuse(self, connection, project_id, stage):
        raise Conflict("Salad stage names an unknown operator profile.")

    monkeypatch.setattr(SaladRouteProvider, "check_stage", refuse)


def _fallbacks(world):
    return world.events(guard.EVENT)


def _receipt_entry(world):
    from tessera_control.campaign_receipts import build_receipt

    with world.env.store.runner_connection() as connection:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, "running")
    return receipt["stages"][0]["attempts"][0]


# -- definitions -------------------------------------------------------------------------------


def test_a_light_stage_is_not_heavy(local_providers):
    assert guard.heavy(_normal()) == (False, [])


def test_heavy_keeps_the_adr_0088_signals_and_adds_pretraining(local_providers):
    assert guard.heavy(_normal(placement_tags=["heavy"])) == (True, ["tag:heavy"])
    assert guard.heavy(_normal(memory_bytes=2 * GIB)) == (True, ["declared_memory"])
    stage = _normal(placement_tags=["pretraining"])
    # The ADR 0088 class is unchanged; only the guard reads the extra tag.
    assert preference.stage_class(stage) == ("light", [])
    assert guard.heavy(stage) == (True, ["tag:pretraining"])


def test_a_gpu_tag_alone_is_not_a_heavy_signal(local_providers):
    assert guard.heavy(_normal(placement_tags=["gpu"])) == (False, [])


def test_large_outputs_from_the_declared_output_limits(local_providers):
    stage = _normal()
    assert guard.heavy(dataclasses.replace(stage, max_output_bytes=2 * GIB)) == (
        True,
        ["large_outputs"],
    )
    under = dataclasses.replace(stage, max_output_bytes=preference.heavy_bytes_threshold() - 1)
    assert guard.heavy(under) == (False, [])


def _io_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE source_snapshots (id, project_id, manifest_json)")
    connection.execute(
        """CREATE TABLE campaign_stage_io (org_id, project_id, campaign_id, stage_key,
        direction, name, sha256, size_bytes)"""
    )
    return connection


def test_large_inputs_sum_the_sizes_the_store_records_for_this_project(local_providers):
    connection = _io_db()
    half = GIB
    manifest = json.dumps({"entries": [{"size": half // 2}, {"size": half // 2}]})
    connection.execute("INSERT INTO source_snapshots VALUES ('s1','p1',?)", (manifest,))
    # The same snapshot id under another project is never read.
    connection.execute(
        "INSERT INTO source_snapshots VALUES ('s1','p2',?)",
        (json.dumps({"entries": [{"size": 100 * GIB}]}),),
    )
    connection.execute(
        "INSERT INTO campaign_stage_io VALUES ('o1','p1','c0','prep','output','data',?,?)",
        ("a" * 64, half),
    )
    campaign = {"id": "c1", "org_id": "o1", "project_id": "p1"}
    from tessera_control.campaign_models import NormalInput

    base = _normal()
    snap = NormalInput("snapshot", "src", snapshot_id="s1")
    art = NormalInput(
        "artifact", "data", campaign_id="c0", stage="prep", output="data", sha256="a" * 64
    )
    unknown = NormalInput("snapshot", "gone", snapshot_id="nope")
    only_snapshot = dataclasses.replace(base, inputs=(snap, unknown))
    assert guard.input_bytes(connection, campaign, only_snapshot) == half
    assert guard.heavy(only_snapshot, connection, campaign) == (False, [])
    both = dataclasses.replace(base, inputs=(snap, art, unknown))
    assert guard.input_bytes(connection, campaign, both) == 2 * half
    assert guard.heavy(both, connection, campaign) == (True, ["large_inputs"])
    # Another org's artifact row with the same coordinates is not counted.
    other = {"id": "c1", "org_id": "o2", "project_id": "p1"}
    assert guard.input_bytes(connection, other, both) == half


def test_the_operator_setting_is_validated_and_defaults_to_warn():
    assert preference.heavy_local_fallback() == "warn"
    preference.configure({"heavy_local_fallback": "refuse"}, ())
    assert preference.heavy_local_fallback() == "refuse"
    preference.configure({"heavy_local_fallback": "warn"}, ())
    assert preference.heavy_local_fallback() == "warn"
    for bad in ("block", True, None, 1):
        with pytest.raises(ValueError):
            preference.configure({"heavy_local_fallback": bad}, ())
        assert preference.active() is None  # a malformed section installs nothing


def test_reasons_for_two_local_candidates_are_not_listed(local_providers):
    stage = _normal(order=(FAR,), placement_tags=["heavy"])
    kinds = {pid: preference.candidate_kind(stage, pid) for pid in stage.candidate_ids}
    assert kinds == {NEAR: "local", FAR: "local"}
    view = guard.Guard(("tag:heavy",), "warn", kinds)
    assert guard.reasons(view, stage, NEAR, (), None, None) == (["not_listed"], [])


# -- recording, end to end ---------------------------------------------------------------------


def test_salad_not_enrolled_falls_back_to_local_with_a_warning(tmp_path, near, monkeypatch):
    world = _cpu_world(tmp_path, near, HEAVY)
    world.start()
    _salad_not_enrolled(monkeypatch)
    world.tick_until(lambda: world.attempt() is not None)
    world.tick(3)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and placed["candidates"] == ["salad", NEAR]
    expected = {
        "warning": True,
        "signals": ["tag:heavy"],
        "reasons": ["no_fitting_profile"],
        "detail": [{"provider_id": "salad", "reason": "not_enrolled"}],
        "mode": "warn",
    }
    assert placed["heavy_guard"] == expected
    (event,) = _fallbacks(world)  # once, however many ticks
    assert event == {
        "campaign_id": world.campaign_id,
        "stage_key": "g1",
        "attempt": 1,
        "provider_id": NEAR,
        "mode": "warn",
        "signals": ["tag:heavy"],
        "reasons": ["no_fitting_profile"],
        "detail": [{"provider_id": "salad", "reason": "not_enrolled"}],
    }
    assert world.attempt()["provider_id"] == NEAR
    entry = _receipt_entry(world)
    assert entry["placement"]["heavy_guard"] == expected
    assert entry["placement"]["provider_id"] == NEAR


def test_a_credential_skip_is_recognised(tmp_path, near, monkeypatch):
    real = placement._check

    def expired(rt, ctx, base, stage, exposure_open):
        if stage.provider == "salad":
            return placement.Skip("salad", "credential_expired", "renew the credential")
        return real(rt, ctx, base, stage, exposure_open)

    monkeypatch.setattr(placement, "_check", expired)
    world = _cpu_world(tmp_path, near, HEAVY)
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR
    assert placed["heavy_guard"]["reasons"] == ["credential_unavailable"]
    assert placed["heavy_guard"]["detail"] == [
        {"provider_id": "salad", "reason": "credential_expired"}
    ]


def test_an_operator_rule_keeping_heavy_work_local_is_an_override(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    preference.configure(
        {"rules": [{"name": "keep-local", "when": {"any_tags": ["heavy"]}, "prefer": [NEAR]}]},
        [NEAR, "salad"],
    )
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and placed["not_evaluated"] == ["salad"]
    assert placed["heavy_guard"]["reasons"] == ["operator_override"]
    assert placed["heavy_guard"]["detail"] == []
    assert len(_fallbacks(world)) == 1


def test_a_heavy_stage_without_a_remote_candidate_is_not_listed(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    single = {k: v for k, v in world.spec["stages"][0].items() if k != "alternate_providers"}
    world.spec = salad_spec_v2(world.env, [single])
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    world.tick(3)
    assert not _placed(world)  # one provider: never placed, as before
    (event,) = _fallbacks(world)
    assert event["provider_id"] == NEAR and event["attempt"] == 1
    assert event["reasons"] == ["not_listed"] and event["signals"] == ["tag:heavy"]
    assert event["detail"] == []
    entry = _receipt_entry(world)
    assert entry["placement"] == {
        "heavy_guard": {
            "warning": True,
            "signals": ["tag:heavy"],
            "reasons": ["not_listed"],
            "detail": [],
            "mode": "warn",
        }
    }


def test_refuse_mode_waits_then_resumes_on_salad(tmp_path, near, monkeypatch):
    world = _cpu_world(tmp_path, near, HEAVY)
    preference.configure({"heavy_local_fallback": "refuse"}, [NEAR, "salad"])
    world.start()
    _salad_not_enrolled(monkeypatch)
    world.tick(5)
    assert world.attempt() is None and not _placed(world) and not _fallbacks(world)
    (waiting,) = world.events(placement.WAITING_EVENT)
    assert [(s["provider_id"], s["reason"], s["detail"]) for s in waiting["skipped"]] == [
        ("salad", "not_enrolled", "Salad stage names an unknown operator profile."),
        (NEAR, "heavy_local_refused", "no_fitting_profile"),
    ]
    assert waiting["heavy_guard"] == {
        "warning": False,
        "refused": True,
        "signals": ["tag:heavy"],
        "reasons": ["no_fitting_profile"],
        "detail": [{"provider_id": "salad", "reason": "not_enrolled"}],
        "mode": "refuse",
    }
    held = world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)
    assert held["held_cents"] == 0
    # The profile fits again: the stage is placed on Salad with no warning.
    monkeypatch.undo()
    # Placed on Salad (its scaffolding then runs before any attempt is reserved there).
    world.tick_until(lambda: bool(_placed(world)))
    (placed,) = _placed(world)
    assert placed["provider_id"] == "salad" and "heavy_guard" not in placed
    assert not _fallbacks(world)


def test_refuse_mode_waits_for_a_one_provider_heavy_stage(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    single = {k: v for k, v in world.spec["stages"][0].items() if k != "alternate_providers"}
    world.spec = salad_spec_v2(world.env, [single])
    preference.configure({"heavy_local_fallback": "refuse"}, [NEAR, "salad"])
    world.start()
    world.tick(5)
    assert world.attempt() is None and not _fallbacks(world)
    (waiting,) = world.events(placement.WAITING_EVENT)
    assert waiting["skipped"] == [
        {"provider_id": NEAR, "reason": "heavy_local_refused", "detail": "not_listed"}
    ]
    assert waiting["heavy_guard"]["reasons"] == ["not_listed"]


# -- no change for light stages or projects without a profile --------------------------------------


def test_a_light_stage_placement_is_unchanged(tmp_path, near):
    world = _cpu_world(tmp_path, near, {"max_duration_seconds": 60})
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and "heavy_guard" not in placed
    assert not _fallbacks(world)
    assert "heavy_guard" not in _receipt_entry(world)["placement"]


def test_without_a_salad_profile_a_heavy_fallback_records_nothing_new(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    world.start()
    register_provider_instance(None)  # the operator restarted without Salad profiles
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR
    assert placed["skipped"][0]["reason"] == "not_enrolled"
    assert "heavy_guard" not in placed and not _fallbacks(world)
    assert set(placed) == {
        "campaign_id",
        "stage_key",
        "attempt",
        "candidates",
        "provider_id",
        "hold_cents",
        "skipped",
        "not_evaluated",
        "steering",
        "budget_refusal",
        "preference",
    }
