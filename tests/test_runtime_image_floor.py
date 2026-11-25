"""The runtime image floor of a pinned-fetch stage at placement. Fakes only.

A stage with a pinned ``public_artifact`` input (ADR 0097) hands its executor a pinned-fetch
manifest. Placement refuses a candidate whose executor image does not declare the
``pinned_fetch_manifest`` runtime capability, or whose capability cannot be determined, with the
closed reason ``runtime_image_below_floor`` on the placement record (and so on the receipt).
Stages without pins are unaffected. Two worker-grant providers stand in for the candidates; a
test double sets what each one's image declares. The Salad provider answers from its operator
profile's ``image_capabilities``. No network, no provider, no money.
"""

from __future__ import annotations

import pytest
import test_campaign_placement as placement_tests
from campaign_runner_harness import dump
from salad_campaign_fixture import profile_data
from test_campaign_models_v2 import _spec2
from test_campaign_placement import FAR, NEAR, WAITING, _placed, _stage, _world

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_models import NormalInput, normalize, parse_spec
from tessera_control.campaign_placement import (
    PERMANENT_REASONS,
    RUNTIME_FLOOR_REASON,
    required_runtime_capabilities,
    runtime_floor_skip,
)
from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.campaign_providers import (
    PINNED_FETCH_MANIFEST,
    register_provider,
    unregister_provider,
)
from tessera_control.campaign_receipts import build_receipt
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles
from tessera_control.store import _connect

URL = "https://models.example.com/weights/w.bin"
SHA = "c" * 64
DIGEST = "sha256:" + "1" * 64
UNSET = object()
providers = placement_tests.providers  # the two worker-grant candidates' fixture


class _Image(worker.WorkerGrantProvider):
    """A worker-grant provider whose executor image declares what the test sets."""

    def __init__(self, descriptor, declared):
        super().__init__(descriptor)
        self.declared = declared

    def runtime_capabilities(self, stage):
        return self.declared


class _Silent(worker.WorkerGrantProvider):
    """A provider with no runtime capability hook at all."""

    runtime_capabilities = None


def _near(world, declared=UNSET):
    """Replace near-cpu with one whose image declares ``declared`` (UNSET: no hook)."""
    unregister_provider(NEAR)
    descriptor = worker._descriptor(NEAR, world.env.worker["id"], None, 1)
    provider = _Silent(descriptor) if declared is UNSET else _Image(descriptor, declared)
    register_provider(provider)


def _public():
    return {
        "kind": "public_artifact",
        "name": "weights",
        "files": [{"name": "w.bin", "url": URL, "sha256": SHA, "max_bytes": 4096}],
    }


def _pinned(env, key="a", **extra):
    stage = _stage(env, key, **extra)
    stage["inputs"] = [*stage["inputs"], _public()]
    return stage


def _until_attempt(world, campaign_id):
    # Runner ticks only: a pinned stage's files are never fetched here (no worker poll).
    world.tick_until(lambda: bool(world.attempts(campaign_id)), worker=False)


def _receipt_entry(world, campaign_id):
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        receipt, _digest = build_receipt(connection, campaign, "running")
    finally:
        connection.close()
    (stage,) = receipt["stages"]
    (entry,) = stage["attempts"]
    return entry


# -- placement ---------------------------------------------------------------------------------


def test_a_pinned_fetch_stage_skips_a_below_floor_candidate_for_the_next_one(tmp_path, providers):
    world = _world(tmp_path, providers)
    _near(world, frozenset())  # near's image predates pinned-fetch manifests
    campaign = world.campaign(_spec2(_pinned(world.env, "a")))
    _until_attempt(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["candidates"] == [NEAR, FAR] and placed["provider_id"] == FAR
    (skip,) = placed["skipped"]
    assert skip["provider_id"] == NEAR and skip["reason"] == RUNTIME_FLOOR_REASON
    assert skip["detail"] == f"runtime image does not declare {PINNED_FETCH_MANIFEST}"
    assert [a["provider_id"] for a in world.attempts(campaign["id"])] == [FAR]
    # The receipt carries the placement and the closed reason.
    entry = _receipt_entry(world, campaign["id"])
    assert entry["provider_id"] == FAR
    assert entry["placement"]["provider_id"] == FAR
    assert entry["placement"]["skipped"] == [skip]


def test_a_pinned_fetch_stage_is_placed_on_an_at_floor_candidate(tmp_path, providers):
    world = _world(tmp_path, providers)
    _near(world, frozenset({PINNED_FETCH_MANIFEST}))
    campaign = world.campaign(_spec2(_pinned(world.env, "a")))
    _until_attempt(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR and placed["skipped"] == []
    assert placed["not_evaluated"] == [FAR]
    entry = _receipt_entry(world, campaign["id"])
    assert entry["placement"]["provider_id"] == NEAR and entry["placement"]["skipped"] == []


@pytest.mark.parametrize(
    "declared", [None, UNSET, "pinned_fetch_manifest"], ids=["none", "no_hook", "not_a_set"]
)
def test_unknown_image_capability_is_refused_with_the_same_reason(tmp_path, providers, declared):
    # None: the hook says it cannot tell; UNSET: no hook; a bare string is not a capability set.
    world = _world(tmp_path, providers)
    _near(world, declared)
    campaign = world.campaign(_spec2(_pinned(world.env, "a")))
    _until_attempt(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == FAR
    (skip,) = placed["skipped"]
    assert skip["provider_id"] == NEAR and skip["reason"] == RUNTIME_FLOOR_REASON
    assert "capability unknown" in skip["detail"]


def test_a_stage_without_pins_is_unaffected_by_a_below_floor_image(tmp_path, providers):
    world = _world(tmp_path, providers)
    _near(world, frozenset())
    campaign = world.campaign(_spec2(_stage(world.env, "a")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR and placed["candidates"] == [NEAR, FAR]
    assert placed["skipped"] == [] and placed["not_evaluated"] == [FAR]


def test_when_every_candidate_is_below_floor_the_stage_fails_closed(tmp_path, providers):
    world = _world(tmp_path, providers)
    _near(world, frozenset())
    campaign = world.campaign(_spec2(_pinned(world.env, "a", alternates=())))
    world.tick_all(campaign["id"], worker=False)
    assert world.stages(campaign["id"])["a"] == "failed"
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", campaign["id"])
    assert stop["stop_reason"] == f"stage_failed:a:{RUNTIME_FLOOR_REASON}"
    assert not world.attempts(campaign["id"])  # nothing prepared, held or sent
    (final,) = [e["payload"] for e in world.events_of(WAITING)]
    assert final["final"] is True
    assert [s["reason"] for s in final["skipped"]] == [RUNTIME_FLOOR_REASON]


def test_a_stage_without_pins_on_one_below_floor_provider_still_runs(tmp_path, providers):
    world = _world(tmp_path, providers)
    _near(world, frozenset())
    campaign = world.campaign(_spec2(_stage(world.env, "a", alternates=())))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert not _placed(world, campaign["id"]) and not world.events_of(WAITING)


# -- the generic rule --------------------------------------------------------------------------


def test_only_a_pinned_public_input_requires_the_capability():
    plain = NormalInput("snapshot", "code")
    pinned = NormalInput("public_artifact", "weights", public_files=(("w.bin", URL, SHA, 1),))

    class _Stage:
        provider = NEAR

        def __init__(self, *inputs):
            self.inputs = inputs

    assert required_runtime_capabilities(_Stage(plain)) == frozenset()
    assert required_runtime_capabilities(_Stage(plain, pinned)) == {PINNED_FETCH_MANIFEST}

    # Never asked about a stage that needs nothing, even with a raising hook.
    class _Raises:
        def runtime_capabilities(self, stage):
            raise RuntimeError("boom")

    assert runtime_floor_skip(_Raises(), _Stage(plain)) is None
    skip = runtime_floor_skip(_Raises(), _Stage(plain, pinned))
    assert skip.reason == RUNTIME_FLOOR_REASON and "unknown" in skip.detail
    assert RUNTIME_FLOOR_REASON in PERMANENT_REASONS


def test_the_built_in_worker_provider_declares_pinned_fetch_support():
    descriptor = worker._descriptor("w-cpu", "principal", None, 1)
    assert worker.WorkerGrantProvider(descriptor).runtime_capabilities(None) == {
        PINNED_FETCH_MANIFEST
    }


# -- the Salad answer comes from its operator profile -------------------------------------------


class _Env:
    owner = {"org_id": "org-1"}
    project = {"id": "project-1"}
    worker = {"id": "worker-1"}


def _salad(**over) -> SaladRouteProvider:
    profiles = SaladCampaignProfiles.model_validate(
        {
            "schema": "tessera.salad.campaign-profiles.v1",
            "profiles": [profile_data(_Env(), **over)],
        }
    )
    return SaladRouteProvider(profiles)


def _salad_stage(image=f"ghcr.io/example/runtime@{DIGEST}", profile="gpu-main", pins=True):
    from types import SimpleNamespace

    inputs = [NormalInput("snapshot", "code")]
    if pins:
        inputs.append(NormalInput("public_artifact", "w", public_files=(("w", URL, SHA, 1),)))
    return SimpleNamespace(
        key="g1",
        provider="salad",
        image=image,
        inputs=tuple(inputs),
        provider_config={"profile": profile},
        salad=None,
    )


@pytest.fixture
def salad_config(monkeypatch):
    from tessera_control import campaign_provider_salad as module

    monkeypatch.setattr(
        module,
        "_salad_config",
        lambda stage: type("C", (), {"profile": stage.provider_config["profile"]})(),
    )


def test_salad_reports_the_profiles_declared_image_capabilities(salad_config):
    declared = _salad(image_capabilities={DIGEST: [PINNED_FETCH_MANIFEST]})
    assert declared.runtime_capabilities(_salad_stage()) == {PINNED_FETCH_MANIFEST}
    assert runtime_floor_skip(declared, _salad_stage()) is None
    below = _salad(image_capabilities={DIGEST: []})
    assert below.runtime_capabilities(_salad_stage()) == frozenset()
    assert runtime_floor_skip(below, _salad_stage()).reason == RUNTIME_FLOOR_REASON


def test_salad_fails_closed_when_the_profile_declares_nothing(salad_config):
    legacy = _salad()  # an existing profile without the setting still loads
    assert legacy.runtime_capabilities(_salad_stage()) is None
    skip = runtime_floor_skip(legacy, _salad_stage())
    assert skip.reason == RUNTIME_FLOOR_REASON and "unknown" in skip.detail
    # A stage without pins on the same profile is never refused by the floor.
    assert runtime_floor_skip(legacy, _salad_stage(pins=False)) is None
    # Unknown profile, no profiles: unknown.
    assert _salad().runtime_capabilities(_salad_stage(profile="nope")) is None
    assert SaladRouteProvider(None).runtime_capabilities(_salad_stage()) is None


def test_a_salad_profile_declares_capabilities_only_for_allowlisted_images():
    with pytest.raises(ValueError, match="not in allowed_image_digests"):
        _salad(image_capabilities={"sha256:" + "2" * 64: [PINNED_FETCH_MANIFEST]})
    with pytest.raises(ValueError):
        _salad(image_capabilities={DIGEST: ["fetch_everything"]})


def test_the_spec_round_trip_keeps_public_inputs_for_the_rule(tmp_path, providers):
    # The normal form placement reads still carries the pinned input the rule keys on.
    world = _world(tmp_path, providers)
    normal = normalize(parse_spec(_spec2(_pinned(world.env, "a"))))
    assert required_runtime_capabilities(normal.stages[0]) == {PINNED_FETCH_MANIFEST}
