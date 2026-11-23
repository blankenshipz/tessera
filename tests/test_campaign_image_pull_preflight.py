"""Image pull-size preflight: a per-provider ceiling, a recorded section, a strict refusal.

Fakes only: worker-grant providers with a fake size source; no registry, no provider, no money.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from campaign_runner_harness import dump
from pydantic import ValidationError
from test_campaign_models_v2 import _spec2
from test_campaign_placement import FAR, NEAR, _placed, _receipt, _stage, _world

from tessera_control import campaign_image_pull as pull
from tessera_control import campaign_placement as placement
from tessera_control import campaign_provider_salad as salad
from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_providers import (
    ProviderDescriptor,
    register_provider,
    unregister_provider,
    validate_descriptor,
)
from tessera_control.salad_campaign_profiles import SaladCampaignProfile

GIB = 1024**3
LIMIT = 4 * GIB


def _sized(size):
    """A worker provider class whose image size is whatever the test says (None: unknown)."""

    class _Sized(worker.WorkerGrantProvider):
        def image_pull_bytes(self, stage):
            return size

    return _Sized


@pytest.fixture
def made():
    ids: list[str] = []

    def make(provider_id, principal, *, limit=LIMIT, strict=False, size=None):
        descriptor = worker._descriptor(
            provider_id, principal, None, 1, max_image_pull_bytes=limit, strict_image_pull=strict
        )
        register_provider(_sized(size)(descriptor))
        ids.append(provider_id)

    yield make
    for provider_id in ids:
        unregister_provider(provider_id)


def _events(world, cid):
    return [e["payload"] for e in world.events_of(pull.EVENT) if e["payload"]["campaign_id"] == cid]


def _run(tmp_path, made, *, size, strict=False, limit=LIMIT, alternates=()):
    def providers(provider_id, principal):
        made(provider_id, principal, limit=limit, strict=strict, size=size)

    world = _world(tmp_path, providers)
    campaign = world.campaign(_spec2(_stage(world.env, "a", alternates=alternates)))
    world.tick_all(campaign["id"])
    return world, campaign["id"]


# -- defaults and validation ------------------------------------------------------------------


def test_defaults_are_off_for_worker_providers_and_four_gib_for_the_interruptible_one():
    assert worker._descriptor("x-cpu", "p").max_image_pull_bytes is None
    assert worker._descriptor("x-cpu", "p").strict_image_pull is False
    assert worker.LocalDockerConfig(worker_principal_id="p").max_image_pull_bytes is None
    assert salad.SaladRouteProvider.descriptor.max_image_pull_bytes == 4 * GIB
    assert pull.DEFAULT_INTERRUPTIBLE_MAX_IMAGE_PULL_BYTES == 4 * GIB
    assert SaladCampaignProfile.model_fields["max_image_pull_bytes"].default == 4 * GIB
    assert SaladCampaignProfile.model_fields["strict_image_pull"].default is False


@pytest.mark.parametrize("bad", [0, -1, 1.5, "4", True, 1 << 51])
def test_a_bad_ceiling_is_rejected_everywhere(bad):
    descriptor = worker._descriptor("x-cpu", "p")
    with pytest.raises(ValueError):
        validate_descriptor(replace(descriptor, max_image_pull_bytes=bad))
    with pytest.raises(ValidationError):
        worker.LocalDockerConfig.model_validate(
            {"worker_principal_id": "p", "max_image_pull_bytes": bad}
        )
    with pytest.raises(ValueError):
        pull.validate_limits(bad, False, "x")


def test_strict_needs_a_ceiling_and_a_boolean():
    descriptor = worker._descriptor("x-cpu", "p")
    validate_descriptor(replace(descriptor, max_image_pull_bytes=GIB, strict_image_pull=True))
    for bad in (
        replace(descriptor, strict_image_pull=True),
        replace(descriptor, strict_image_pull=1),
    ):
        with pytest.raises(ValueError):
            validate_descriptor(bad)
    with pytest.raises(ValidationError):
        worker.LocalDockerConfig.model_validate(
            {"worker_principal_id": "p", "strict_image_pull": True}
        )


def test_salad_profile_validation_and_size_lookup():
    from test_salad_campaign_profiles import profile_data

    base = profile_data()
    digest = base["allowed_image_digests"][0]
    ok = SaladCampaignProfile.model_validate({**base, "image_pull_sizes": {digest: 5 * GIB}})
    assert ok.image_pull_sizes[digest] == 5 * GIB
    assert SaladCampaignProfile.model_validate({**base, "max_image_pull_bytes": None})
    for bad in (
        {"max_image_pull_bytes": 0},
        {"max_image_pull_bytes": -5},
        {"strict_image_pull": 1},
        {"max_image_pull_bytes": None, "strict_image_pull": True},
        {"image_pull_sizes": {"sha256:" + "0" * 64: 1}},
    ):
        with pytest.raises(ValidationError):
            SaladCampaignProfile.model_validate({**base, **bad})


def test_the_descriptor_is_off_without_a_ceiling_and_nothing_is_evaluated():
    provider = worker.WorkerGrantProvider(worker._descriptor("x-cpu", "p"))
    assert pull.evaluate(provider, object()) is None


def test_a_registered_size_resolver_is_the_provider_neutral_source():
    provider = worker.WorkerGrantProvider(
        worker._descriptor("x-cpu", "p", max_image_pull_bytes=GIB)
    )
    assert pull.evaluate(provider, object())["reason"] == pull.SIZE_UNKNOWN
    resolver = lambda stage: 2 * GIB  # noqa: E731
    pull.register_size_resolver(resolver)
    try:
        section = pull.evaluate(provider, object())
    finally:
        pull.unregister_size_resolver(resolver)
    assert section == {
        "status": "over_limit",
        "reason": pull.OVER_LIMIT,
        "size_bytes": 2 * GIB,
        "limit_bytes": GIB,
        "strict": False,
    }


def test_the_refusal_reason_is_a_permanent_placement_reason():
    assert pull.REFUSED_REASON == "image_pull_refused"
    assert pull.REFUSED_REASON in placement.PERMANENT_REASONS
    assert pull.REFUSED_REASON not in placement.TRANSIENT_REASONS


# -- single-provider stage ----------------------------------------------------------------------


def test_over_the_limit_warns_and_is_recorded_in_the_status_and_the_receipt(tmp_path, made):
    world, cid = _run(tmp_path, made, size=5 * GIB)
    assert world.status(cid) == "succeeded", dump(world, cid)
    expected = {
        "provider_id": NEAR,
        "status": "over_limit",
        "reason": "image_pull_over_limit",
        "size_bytes": 5 * GIB,
        "limit_bytes": LIMIT,
        "strict": False,
    }
    [event] = _events(world, cid)
    assert {k: event[k] for k in expected} == expected
    [attempt] = _receipt(world, cid)["stages"][0]["attempts"]
    assert attempt[pull.SECTION] == expected
    assert isinstance(attempt[pull.SECTION]["size_bytes"], int)
    assert isinstance(attempt[pull.SECTION]["limit_bytes"], int)
    status = world.runner.store.get_campaign(world.env.owner, cid)
    [stage] = status["stages"]
    assert stage[pull.SECTION]["reason"] == "image_pull_over_limit"
    assert json.dumps(status)  # plain data


def test_under_the_limit_records_nothing_alarming(tmp_path, made):
    world, cid = _run(tmp_path, made, size=GIB)
    assert world.status(cid) == "succeeded", dump(world, cid)
    [attempt] = _receipt(world, cid)["stages"][0]["attempts"]
    section = attempt[pull.SECTION]
    assert section["status"] == "within_limit" and section["reason"] is None
    assert section["size_bytes"] == GIB


def test_an_unknown_size_is_recorded_with_its_reason_and_never_refused(tmp_path, made):
    world, cid = _run(tmp_path, made, size=None, strict=True)
    assert world.status(cid) == "succeeded", dump(world, cid)
    [event] = _events(world, cid)
    assert (event["status"], event["reason"], event["size_bytes"]) == (
        "unknown",
        "image_size_unknown",
        None,
    )
    assert event["limit_bytes"] == LIMIT


def test_no_ceiling_records_nothing(tmp_path, made):
    world, cid = _run(tmp_path, made, size=99 * GIB, limit=None)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert _events(world, cid) == []
    [attempt] = _receipt(world, cid)["stages"][0]["attempts"]
    assert pull.SECTION not in attempt


def test_strict_refuses_a_single_provider_stage_with_the_closed_reason(tmp_path, made):
    world, cid = _run(tmp_path, made, size=5 * GIB, strict=True)
    assert world.status(cid) == "failed", dump(world, cid)
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == "stage_failed:a:image_pull_refused"
    assert not world.attempts(cid)
    [event] = _events(world, cid)
    assert event["status"] == "refused" and event["reason"] == "image_pull_over_limit"


# -- placed stage -----------------------------------------------------------------------------


def test_strict_skips_a_candidate_permanently_and_the_other_one_runs(tmp_path, made):
    def providers(provider_id, principal):
        if provider_id == NEAR:
            made(provider_id, principal, strict=True, size=5 * GIB)
        else:
            made(provider_id, principal, limit=None)

    world = _world(tmp_path, providers)
    campaign = world.campaign(_spec2(_stage(world.env, "a")))
    cid = campaign["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    [placed] = _placed(world, cid)
    assert placed["provider_id"] == FAR
    assert placed["skipped"] == [
        {"provider_id": NEAR, "reason": "image_pull_refused", "detail": f"{5 * GIB} > {LIMIT}"}
    ]


def test_strict_with_every_candidate_over_the_limit_fails_the_stage(tmp_path, made):
    def providers(provider_id, principal):
        made(provider_id, principal, strict=True, size=5 * GIB)

    world = _world(tmp_path, providers)
    campaign = world.campaign(_spec2(_stage(world.env, "a")))
    cid = campaign["id"]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == "stage_failed:a:no_eligible_provider"
    assert not world.attempts(cid)


def test_a_placed_warning_is_recorded_for_the_chosen_provider(tmp_path, made):
    world, cid = _run(tmp_path, made, size=6 * GIB, alternates=(FAR,))
    assert world.status(cid) == "succeeded", dump(world, cid)
    [event] = _events(world, cid)
    assert event["provider_id"] == NEAR and event["reason"] == "image_pull_over_limit"
    [attempt] = _receipt(world, cid)["stages"][0]["attempts"]
    assert attempt[pull.SECTION]["size_bytes"] == 6 * GIB


def test_the_descriptor_type_stays_closed():
    assert "max_image_pull_bytes" in ProviderDescriptor.__dataclass_fields__


# -- trust and licence refusals come first on the single-provider path --------------------------


def _strict_far(made):
    """near-cpu has no ceiling; far-cpu is strict and its image is over the ceiling."""

    def providers(provider_id, principal):
        if provider_id == FAR:
            made(provider_id, principal, strict=True, size=5 * GIB)
        else:
            made(provider_id, principal, limit=None)

    return providers


def test_a_licence_refusal_is_reported_before_a_strict_pull_size_refusal(tmp_path, made):
    from test_license_placement_guard import MESSAGE, STAGE_REFUSED, _label, _policy

    world = _world(tmp_path, _strict_far(made))
    env = world.env
    _policy(env, world.far_worker)
    campaign = world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    cid = campaign["id"]
    _label(env, ["restricted"])
    _policy(env, world.far_worker, far_labels=["open"])
    world.tick_all(cid)
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == "stage_failed:a:input_not_admitted", dump(world, cid)
    assert not world.attempts(cid)
    [refused] = [
        e["payload"] for e in world.events_of(STAGE_REFUSED) if e["payload"]["campaign_id"] == cid
    ]
    assert refused["provider"] == FAR and MESSAGE in refused["reason"]
    assert [e["status"] for e in _events(world, cid)] == []  # no image-size refusal recorded


def test_a_trust_refusal_is_reported_before_a_strict_pull_size_refusal(tmp_path, made):
    from test_campaign_placement import _trust

    world = _world(tmp_path, _strict_far(made))
    env = world.env
    world.scenarios = [(0, {"data.json": '{"score": 1}'})]
    campaign = world.campaign(
        _spec2(
            _stage(env, "a", alternates=()),
            _stage(env, "b", provider=FAR, alternates=(), upstream="a"),
        )
    )
    cid = campaign["id"]
    # After approval the operator narrows far-cpu to public data: b's derived input may not go.
    _trust(env, world.far_worker, far=["public"])
    world.tick_all(cid)
    assert world.stages(cid)["b"] == "failed", dump(world, cid)
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == "stage_failed:b:input_not_admitted"
    assert [a["stage_key"] for a in world.attempts(cid)] == ["a"]
    assert [e for e in _events(world, cid) if e["stage_key"] == "b"] == []


# -- the pre-dispatch plan readout ----------------------------------------------------------------


def test_the_validate_plan_shows_each_candidates_preflight_before_dispatch(tmp_path, made):
    world = _world(tmp_path, _strict_far(made))
    env = world.env
    spec = _spec2(
        _stage(env, "a", alternates=(FAR,)),
        _stage(env, "b", provider=NEAR, alternates=()),
    )
    plan = env.store.validate_campaign_spec(env.owner, env.project["id"], {"spec": spec})
    stages = {item["key"]: item for item in plan["stages"]}
    assert stages["a"][pull.SECTION] == [
        {
            "provider_id": FAR,
            "status": "refused",
            "reason": "image_pull_over_limit",
            "size_bytes": 5 * GIB,
            "limit_bytes": LIMIT,
            "strict": True,
        }
    ]
    assert pull.SECTION not in stages["b"]  # no candidate with a ceiling: nothing to show
    assert json.dumps(plan)
    # A dry run records nothing.
    assert world.events_of(pull.EVENT) == []


def test_the_plan_reports_an_unknown_size_without_refusing(tmp_path, made):
    world = _world(tmp_path, lambda pid, principal: made(pid, principal, strict=True, size=None))
    env = world.env
    spec = _spec2(_stage(env, "a", alternates=()))
    plan = env.store.validate_campaign_spec(env.owner, env.project["id"], {"spec": spec})
    [section] = plan["stages"][0][pull.SECTION]
    assert (section["provider_id"], section["status"], section["reason"]) == (
        NEAR,
        "unknown",
        "image_size_unknown",
    )
