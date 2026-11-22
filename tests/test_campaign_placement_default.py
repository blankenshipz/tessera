# ruff: noqa: F811
"""The built-in default placement policy (ADR 0088). Fakes only: no provider, no network, no money.

Unit tests build ``NormalStage`` objects against two registered worker-grant providers and the
process-wide ``salad`` provider; the end-to-end tests reuse ``SaladWorld`` from the Salad
placement tests (a ``near-cpu`` own provider with a Salad CPU alternate).
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from salad_campaign_fixture import (  # noqa: F401
    IMAGE,
    SaladWorld,
    contract_data,
    program_template,
    salad_provider_reset,
    salad_spec_v2,
)
from test_campaign_models_v2 import _spec2, _stage2
from test_campaign_placement_salad import (  # noqa: F401
    NEAR,
    _placed,
    _stage,
    near,
)

from tessera_control import campaign_placement_preference as preference
from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_models import (
    SALAD_RUNTIME,
    NormalStage,
    normalize,
    parse_spec,
    spec_digest,
    stage_on_candidate,
)
from tessera_control.campaign_providers import (
    PlacementCapability,
    get_provider,
    placement_capability_of,
    register_provider,
    unregister_provider,
)
from tessera_control.egress_models import EgressPolicy

FAR = "far-cpu"
GIB = 1024**3
HEAVY_MEMORY = 1536 * 1024**2
CPU_RATE = {"vcpu_hour_usd": "0.04", "memory_gb_hour_usd": "0.01"}
EGRESS = {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": "downloads.example.org", "port": 443}],
    "max_egress_bytes": 1000,
    "sources": [
        {
            "host": "downloads.example.org",
            "license": "Apache-2.0",
            "source": "https://downloads.example.org/model",
        }
    ],
}


@pytest.fixture(autouse=True)
def _no_preference():
    preference.configure(None, ())
    yield
    preference.configure(None, ())


@pytest.fixture
def local_providers():
    for provider_id in (NEAR, FAR):
        register_provider(
            worker.WorkerGrantProvider(worker._descriptor(provider_id, "p1", None, 1))
        )
    yield
    for provider_id in (NEAR, FAR):
        unregister_provider(provider_id)


def _salad_alt(gpu: bool) -> dict:
    return {
        "provider": "salad",
        "runtime": SALAD_RUNTIME,
        "max_exposure_cents": 20,
        "provider_config": {
            "profile": "cpu-main" if not gpu else "gpu-main",
            "worker_principal_id": "w-1",
            "workload": contract_data(),
            "program": program_template(),
            "gpu": gpu,
        },
    }


def _normal(*, gpu: bool = False, order=("salad", FAR), **extra) -> NormalStage:
    """near-cpu first, then the alternates in ``order``; any ``extra`` is a stage field."""
    alts = {"salad": _salad_alt(gpu), FAR: {"provider": FAR, "runtime": "docker-cpu-v1"}}
    stage = _stage2(
        "a",
        provider=NEAR,
        image=IMAGE,
        argv=["/tessera/bin/run", *contract_data()["argv_template"]],
        outputs=[{"name": "output", "path": "output.json", "max_bytes": 4096}],
        max_exposure_cents=20,
        alternate_providers=[alts[item] for item in order],
        **extra,
    )
    return normalize(parse_spec(_spec2(stage, budget_cap_cents=50))).stages[0]


# -- 1. classes and signals ------------------------------------------------------------------------


def test_a_plain_stage_is_light_with_no_signals(local_providers):
    assert preference.stage_class(_normal()) == ("light", [])


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        (HEAVY_MEMORY - 1, "light"),
        (HEAVY_MEMORY, "heavy"),
        (2 * GIB, "heavy"),
    ],
)
def test_the_declared_memory_threshold_is_one_and_a_half_gib(local_providers, memory, expected):
    cls, signals = preference.stage_class(_normal(memory_bytes=memory))
    assert cls == expected
    assert signals == (["declared_memory"] if expected == "heavy" else [])


@pytest.mark.parametrize(
    ("seconds", "expected"), [(1799, "light"), (1800, "heavy"), (3600, "heavy")]
)
def test_the_long_expected_runtime_threshold_is_1800_seconds(local_providers, seconds, expected):
    stage = _normal(max_duration_seconds=3600, expected_duration_seconds=seconds)
    cls, signals = preference.stage_class(stage)
    assert cls == expected
    assert signals == (["long_expected_runtime"] if expected == "heavy" else [])


def test_a_big_duration_cap_without_an_estimate_is_light(local_providers):
    stage = _normal(max_duration_seconds=3600)
    assert stage.expected_duration_seconds is None
    assert preference.stage_class(stage) == ("light", [])


@pytest.mark.parametrize("tag", ["heavy", "training"])
def test_a_heavy_or_training_tag_makes_a_stage_heavy(local_providers, tag):
    assert preference.stage_class(_normal(placement_tags=[tag])) == ("heavy", [f"tag:{tag}"])


def test_signals_accumulate_in_a_fixed_order(local_providers):
    stage = _normal(
        memory_bytes=2 * GIB,
        max_duration_seconds=3600,
        expected_duration_seconds=3600,
        placement_tags=["training", "heavy"],
    )
    assert preference.stage_class(stage) == (
        "heavy",
        ["declared_memory", "long_expected_runtime", "tag:heavy", "tag:training"],
    )


def test_a_gpu_tag_makes_a_gpu_stage_and_keeps_heavy_signals(local_providers):
    assert preference.stage_class(_normal(placement_tags=["gpu"])) == ("gpu", ["tag:gpu"])
    stage = _normal(placement_tags=["gpu", "heavy"], memory_bytes=2 * GIB)
    assert preference.stage_class(stage) == ("gpu", ["tag:gpu", "declared_memory", "tag:heavy"])


def test_egress_beats_heavy_and_gpu(local_providers):
    # Egress plus a Salad alternate cannot be submitted (egress is local-CPU only), so the egress
    # declaration is set on the normalized stage directly.
    stage = dataclasses.replace(
        _normal(placement_tags=["heavy", "gpu"], memory_bytes=2 * GIB),
        egress=EgressPolicy.model_validate(EGRESS),
    )
    cls, signals = preference.stage_class(stage)
    assert cls == "egress_bound"
    assert signals == ["egress", "declared_memory", "tag:heavy"]


# -- 2. candidate kinds ----------------------------------------------------------------------------


def test_candidate_kinds(local_providers):
    cpu, gpu = _normal(gpu=False), _normal(gpu=True)
    assert preference.candidate_kind(cpu, NEAR) == "local"
    assert preference.candidate_kind(cpu, "salad") == "remote_cpu"
    assert preference.candidate_kind(gpu, "salad") == "remote_gpu"
    assert preference.candidate_kind(cpu, "nowhere") == "unknown"
    broken = dataclasses.replace(
        cpu,
        alternates=tuple(
            dataclasses.replace(item, provider_config={}) if item.provider == "salad" else item
            for item in cpu.alternates
        ),
    )
    assert preference.candidate_kind(broken, "salad") == "other"


# -- 3. ordering -----------------------------------------------------------------------------------


def test_heavy_tries_a_remote_cpu_candidate_first_then_the_spec_order(local_providers):
    order, record = preference.preferred_order(_normal(memory_bytes=2 * GIB))
    assert order == ("salad", NEAR, FAR)
    assert record["source"] == "default_policy" and record["class"] == "heavy"
    assert record["spec_order"] == [NEAR, "salad", FAR] and record["order"] == list(order)
    assert record["reordered"] is True
    assert record["kinds"] == {NEAR: "local", "salad": "remote_cpu", FAR: "local"}
    assert record["signals"] == ["declared_memory"]
    assert "note" not in record


def test_heavy_does_not_put_a_remote_gpu_candidate_first(local_providers):
    stage = _normal(gpu=True, memory_bytes=2 * GIB)
    order, record = preference.preferred_order(stage)
    assert order == (NEAR, "salad", FAR) and record["reordered"] is False
    assert "remote_cpu" in record["note"]


def test_gpu_tag_tries_a_remote_gpu_first_but_not_a_remote_cpu(local_providers):
    order, record = preference.preferred_order(_normal(gpu=True, placement_tags=["gpu"]))
    assert order == ("salad", NEAR, FAR) and record["class"] == "gpu" and record["reordered"]
    order, record = preference.preferred_order(_normal(gpu=False, placement_tags=["gpu"]))
    assert order == (NEAR, "salad", FAR) and record["reordered"] is False


@pytest.mark.parametrize("order", [("salad", FAR), (FAR, "salad")])
def test_a_light_stage_keeps_the_order_its_spec_wrote(local_providers, order):
    # No signal: even a remote candidate listed first is not reordered, and nothing is recorded.
    stage = _normal(order=order)
    assert preference.stage_class(stage) == ("light", [])
    assert preference.preferred_order(stage) == ((NEAR, *order), None)
    assert preference.default_order(stage) == (stage.candidate_ids, None)


def test_egress_bound_keeps_salad_behind_the_local_candidates(local_providers):
    stage = dataclasses.replace(
        _normal(order=("salad", FAR), memory_bytes=2 * GIB),
        egress=EgressPolicy.model_validate(EGRESS),
    )
    order, record = preference.preferred_order(stage)
    assert order == (NEAR, FAR, "salad") and record["class"] == "egress_bound"


def test_reroute_only_targets_stay_last(local_providers):
    stage = _normal(memory_bytes=2 * GIB)
    retry = stage.retry.model_copy(update={"reroute_providers": ["elsewhere-cpu"]})
    stage = dataclasses.replace(stage, retry=retry)
    order, _ = preference.preferred_order(stage)
    assert order == ("salad", NEAR, FAR, "elsewhere-cpu")


def test_a_stage_without_alternates_is_untouched(local_providers):
    stage = normalize(parse_spec(_spec2(_stage2("a", provider=NEAR, memory_bytes=4 * GIB)))).stages[
        0
    ]
    assert stage.alternates == ()
    assert preference.preferred_order(stage) == ((NEAR,), None)


# -- 4. operator config ----------------------------------------------------------------------------


def test_the_operator_can_switch_the_default_off(local_providers):
    stage = _normal(memory_bytes=2 * GIB)
    preference.configure({"default_policy": False}, ())
    assert preference.default_enabled() is False
    assert preference.preferred_order(stage) == ((NEAR, "salad", FAR), None)
    view = preference.status_view([stage])["default_policy"]
    assert view["enabled"] is False and view["stages"] == []


def _est(seconds):
    return {"max_duration_seconds": 3600, "expected_duration_seconds": seconds}


def test_the_operator_can_move_the_thresholds(local_providers):
    preference.configure({"heavy_memory_bytes": 4 * GIB, "long_runtime_seconds": 3600}, ())
    assert preference.stage_class(_normal(memory_bytes=2 * GIB))[0] == "light"
    assert preference.stage_class(_normal(memory_bytes=4 * GIB))[0] == "heavy"
    assert preference.stage_class(_normal(**_est(3000)))[0] == "light"
    assert preference.stage_class(_normal(**_est(3600)))[0] == "heavy"
    preference.configure({"heavy_memory_bytes": 1024 * 1024}, ())
    assert preference.stage_class(_normal(memory_bytes=1024 * 1024))[0] == "heavy"


@pytest.mark.parametrize(
    "bad",
    [
        {"heavy_memory_bytes": 1},
        {"long_runtime_seconds": 0},
        {"default_policy": "maybe"},
        {"unknown": 1},
    ],
)
def test_malformed_default_settings_are_refused_and_install_nothing(bad):
    with pytest.raises(ValueError):
        preference.configure(bad, ())
    assert preference.active() is None


def test_a_matching_operator_rule_wins_over_the_default(local_providers):
    stage = _normal(memory_bytes=2 * GIB, placement_tags=["heavy"])
    preference.configure(
        {"rules": [{"name": "keep", "when": {"any_tags": ["heavy"]}, "prefer": [FAR, NEAR]}]},
        (NEAR, FAR),
    )
    order, record = preference.preferred_order(stage)
    assert order == (FAR, NEAR, "salad")
    assert record["rule"] == "keep" and "source" not in record and "class" not in record


def test_a_stage_no_rule_matches_falls_to_the_default(local_providers):
    preference.configure(
        {"rules": [{"name": "keep", "when": {"any_tags": ["other"]}, "prefer": [FAR]}]},
        (NEAR, FAR),
    )
    order, record = preference.preferred_order(_normal(memory_bytes=2 * GIB))
    assert order[0] == "salad" and record["source"] == "default_policy"


# -- 5. status view --------------------------------------------------------------------------------


def test_status_view_reports_the_default_policy_block(local_providers):
    heavy = _normal(memory_bytes=2 * GIB)
    light = _normal(order=(FAR, "salad"))  # no signal: listed, never reordered
    bare = normalize(parse_spec(_spec2(_stage2("b", provider=NEAR)))).stages[0]
    view = preference.status_view([heavy, light, bare])
    assert view["configured"] is False and view["rules"] == [] and view["stages"] == []
    block = view["default_policy"]
    assert block["enabled"] is True
    assert block["heavy_memory_bytes"] == HEAVY_MEMORY and block["long_runtime_seconds"] == 1800
    # The stage without alternates is not listed; each other stage is, applying only if reordered.
    assert block["stages"] == [
        {
            "stage_key": "a",
            "class": "heavy",
            "signals": ["declared_memory"],
            "order": ["salad", NEAR, FAR],
            "applies": True,
        },
        {
            "stage_key": "a",
            "class": "light",
            "signals": [],
            "order": [NEAR, FAR, "salad"],
            "applies": False,
        },
    ]


def test_status_view_reflects_operator_thresholds(local_providers):
    preference.configure({"heavy_memory_bytes": 4 * GIB, "long_runtime_seconds": 99}, ())
    block = preference.status_view([])["default_policy"]
    assert block["heavy_memory_bytes"] == 4 * GIB and block["long_runtime_seconds"] == 99


# -- 6. the spec digest ----------------------------------------------------------------------------


def test_memory_bytes_is_absent_from_the_spec_unless_set_and_keeps_digests(local_providers):
    plain = _spec2(_stage2("a"))
    assert "memory_bytes" not in parse_spec(plain).model_dump(mode="json")["stages"][0]
    explicit = {**plain, "stages": [{**plain["stages"][0], "memory_bytes": None}]}
    assert spec_digest(parse_spec(explicit)) == spec_digest(parse_spec(plain))
    sized = {**plain, "stages": [{**plain["stages"][0], "memory_bytes": 2 * GIB}]}
    assert spec_digest(parse_spec(sized)) != spec_digest(parse_spec(plain))
    assert normalize(parse_spec(sized)).stages[0].memory_bytes == 2 * GIB
    assert normalize(parse_spec(plain)).stages[0].memory_bytes is None
    for bad in (0, 1024 * 1024 - 1, 2049 * GIB, "2g", 1.5 * GIB, True):
        with pytest.raises(ValueError):
            parse_spec({**plain, "stages": [{**plain["stages"][0], "memory_bytes": bad}]})


# -- 7. end to end through the real runner ---------------------------------------------------------


def _cpu_world(tmp_path, near, extra, slots=1, alt=None):
    """near-cpu (own provider, ``slots`` free) with a Salad CPU alternate on a CPU profile."""
    world = SaladWorld(tmp_path, gpu_class=None, cpu_rate=CPU_RATE)
    world.near_principal = near(world, slots)
    stage = _stage(world.env)
    stage["alternate_providers"][0] = {
        **stage["alternate_providers"][0],
        "provider_config": {**stage["alternate_providers"][0]["provider_config"], "gpu": False},
        **(alt or {}),
    }
    world.spec = salad_spec_v2(world.env, [{**stage, **extra}])
    return world


def _receipt_entry(world):
    import json

    receipt = json.loads(
        world.row(
            "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", world.campaign_id
        )["receipt_json"]
    )
    return receipt["stages"][0]["attempts"][0]


def test_a_heavy_stage_goes_to_salad_by_default_even_with_a_free_local_slot(tmp_path, near):
    world = _cpu_world(tmp_path, near, {"memory_bytes": 2 * GIB})
    world.to_route_started()
    (placed,) = _placed(world)
    assert placed["provider_id"] == "salad" and placed["candidates"] == ["salad", NEAR]
    assert placed["skipped"] == []
    record = placed["preference"]
    assert record["source"] == "default_policy" and record["class"] == "heavy"
    assert record["signals"] == ["declared_memory"]  # the 3000 s cap is not an estimate
    assert record["spec_order"] == [NEAR, "salad"] and record["order"] == ["salad", NEAR]
    assert record["kinds"] == {NEAR: "local", "salad": "remote_cpu"}
    assert record["reordered"] is True and record["chosen"] == "salad"
    assert record["fell_back"] == []
    assert world.attempt()["provider_id"] == "salad"
    world.run_to_outcome()
    world.tick_until_phase("awaiting_charge")
    world.record_charge(13)
    world.tick_until(lambda: world.status() == "succeeded")
    entry = _receipt_entry(world)
    assert entry["provider_id"] == "salad"
    assert entry["placement"]["preference"] == record


def test_a_salad_candidate_that_is_skipped_falls_back_to_the_local_one_and_says_why(tmp_path, near):
    from tessera_control.store import canonical_digest

    world = _cpu_world(tmp_path, near, {"placement_tags": ["heavy"]})
    world.start()
    revision = world.row("SELECT revision FROM campaigns WHERE id=?", world.campaign_id)
    body = {
        "expected_revision": revision["revision"],
        "stage_key": "g1",
        "exclude": ["salad"],
        "reason": "keep this stage off billable compute",
    }
    status, _ = world.env.store.steer_campaign_placement(
        world.human, world.campaign_id, "k-steer", canonical_digest(body), body
    )
    assert status == 200
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR
    record = placed["preference"]
    assert record["source"] == "default_policy" and record["class"] == "heavy"
    assert record["order"] == ["salad", NEAR] and record["chosen"] == NEAR
    (fell,) = record["fell_back"]
    assert fell["provider_id"] == "salad" and fell["reason"] == "excluded_by_steering"
    assert "detail" in fell
    assert world.row("SELECT 1 FROM campaign_salad_stages") is None  # never routed or held
    assert world.attempt()["provider_id"] == NEAR


def test_a_light_stage_stays_on_the_local_provider(tmp_path, near):
    # Short, untagged, no declared memory: light. (Egress-bound is covered by the unit tests only:
    # a stage that declares egress cannot also list a Salad alternate at submit.)
    world = _cpu_world(tmp_path, near, {"max_duration_seconds": 60})
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and placed["candidates"] == [NEAR, "salad"]
    assert placed["not_evaluated"] == ["salad"]
    # Already in light order: nothing to record.
    assert "preference" not in placed or placed["preference"] is None
    assert world.row("SELECT 1 FROM campaign_salad_stages") is None


def _skip_world(tmp_path, near, extra, alt=None):
    return _cpu_world(tmp_path, near, {"memory_bytes": 2 * GIB, **extra}, alt=alt)


def _assert_fell_back(world, reason):
    """The heavy stage tried Salad first, skipped it for ``reason`` and ran on near-cpu; the same
    skip is in the decision's ``skipped`` list and in ``preference.fell_back``."""
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and placed["candidates"] == ["salad", NEAR]
    (skip,) = placed["skipped"]
    assert skip["provider_id"] == "salad" and skip["reason"] == reason
    record = placed["preference"]
    assert record["source"] == "default_policy" and record["class"] == "heavy"
    assert record["order"] == ["salad", NEAR] and record["chosen"] == NEAR
    assert record["fell_back"] == [skip]
    assert world.attempt()["provider_id"] == NEAR
    assert world.row("SELECT 1 FROM campaign_salad_stages") is None  # never routed or held


def test_heavy_falls_back_to_local_when_salad_is_over_the_run_cap(tmp_path, near, monkeypatch):
    # Submit refuses a candidate whose estimate exceeds the stage's own cap, so the condition
    # (candidate hold > the stage's max_exposure_cents) is reached by making the Salad candidate
    # run one cent above it when the real runner decides.
    from tessera_control import campaign_placement as placement

    real = placement.stage_on_candidate

    def over_cap(base, provider_id):
        stage = real(base, provider_id)
        if provider_id == "salad":
            return dataclasses.replace(stage, max_exposure_cents=base.max_exposure_cents + 1)
        return stage

    monkeypatch.setattr(placement, "stage_on_candidate", over_cap)
    world = _skip_world(tmp_path, near, {})
    world.start()
    _assert_fell_back(world, "over_run_cap")
    assert _placed(world)[0]["skipped"][0]["detail"] == "21 > 20"


def test_heavy_with_salad_unregistered_after_submit_is_not_tried_first(tmp_path, near):
    # An unregistered candidate has kind "unknown": the default policy cannot prefer it, so the
    # stage simply runs on near-cpu with no skip. ("unregistered" as a recorded skip is covered
    # for operator rules in test_campaign_placement_preference.py.)
    world = _skip_world(tmp_path, near, {})
    world.start()
    unregister_provider("salad")  # the operator restarted without it (autouse fixture restores)
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and placed["skipped"] == []
    record = placed["preference"]
    assert record["kinds"] == {NEAR: "local", "salad": "unknown"}
    assert record["reordered"] is False and "remote_cpu" in record["note"]
    assert record["fell_back"] == [] and record["chosen"] == NEAR
    assert record["unavailable"] == ["salad"]  # the receipt names the destination it could not rank


def test_heavy_falls_back_to_local_when_salad_is_no_longer_trusted(tmp_path, near, monkeypatch):
    # Salad only accepts public snapshot input, which every trust setting admits, and it refuses
    # anything else itself first (as ``not_enrolled``), so a real trust withdrawal cannot reach
    # the ``untrusted`` check for it. The trust check is therefore made to refuse the Salad
    # principal for the data class when the real runner decides (the Conflict, the skip and the
    # fallback are the real ones).
    from tessera_control import provider_trust
    from tessera_control.data_class import DataClassRefused

    world = _skip_world(tmp_path, near, {})
    world.start()
    real = provider_trust.provider_admits

    def withdrawn(connection, provider_id, *args, **kwargs):
        if provider_id == "salad":
            raise DataClassRefused("the snapshot data is recorded public, trust withdrawn")
        return real(connection, provider_id, *args, **kwargs)

    monkeypatch.setattr(provider_trust, "provider_admits", withdrawn)
    _assert_fell_back(world, "untrusted")
    assert "trust withdrawn" in _placed(world)[0]["skipped"][0]["detail"]


def test_heavy_falls_back_to_local_when_salad_is_over_the_campaign_budget(
    tmp_path, near, monkeypatch
):
    # In this fixture near-cpu reserves the stage's own 20 cent exposure too, so a reduced budget
    # skips both candidates (a recorded budget refusal). To show the fallback itself, near-cpu is
    # made to hold nothing when the real runner decides, as an unbilled worker-grant stage would.
    from tessera_control import campaign_placement as placement
    from tessera_control.store import canonical_digest

    real = placement.stage_on_candidate

    def unbilled_local(base, provider_id):
        stage = real(base, provider_id)
        return dataclasses.replace(stage, max_exposure_cents=0) if provider_id == NEAR else stage

    monkeypatch.setattr(placement, "stage_on_candidate", unbilled_local)
    world = _skip_world(tmp_path, near, {})
    world.start()
    revision = world.row("SELECT revision FROM campaigns WHERE id=?", world.campaign_id)
    body = {
        "expected_revision": revision["revision"],
        "budget_cap_cents": 10,  # below Salad's 20 cent hold
        "reason": "tighten",
    }
    status, _ = world.env.store.reduce_campaign_budget(
        world.human, world.campaign_id, "k-reduce", canonical_digest(body), body
    )
    assert status == 200
    _assert_fell_back(world, "over_campaign_budget")
    assert _placed(world)[0]["skipped"][0]["detail"] == "0 + 20 > 10"


# -- 8. egress and a Salad alternate are refused at submit -----------------------------------------


def test_a_stage_that_declares_egress_and_lists_a_salad_alternate_is_refused_at_submit(
    tmp_path, near
):
    world = _cpu_world(tmp_path, near, {})
    stage = world.spec["stages"][0]
    world.spec = salad_spec_v2(world.env, [{**stage, "egress": EGRESS}])
    with pytest.raises(Exception, match="provider-wide egress") as refused:
        world.env.submit(spec=world.spec)
    assert getattr(refused.value, "status", 409) in (400, 409, 422)
    assert world.row("SELECT 1 FROM campaigns") is None
    assert world.row("SELECT 1 FROM campaign_stages") is None


# -- 9. the policy names no vendor -----------------------------------------------------------------


def _listing(provider_id) -> NormalStage:
    """A heavy near-cpu stage that lists ``provider_id`` as its alternate."""
    alternates = [{"provider": provider_id, "runtime": "docker-cpu-v1"}]
    stage = _stage2("a", provider=NEAR, memory_bytes=2 * GIB, alternate_providers=alternates)
    return normalize(parse_spec(_spec2(stage))).stages[0]


class _Rented(worker.WorkerGrantProvider):
    """A vendor-neutral remote CPU provider: the worker-grant mechanics under a custom kind."""

    def placement_capability(self, stage):
        return PlacementCapability("remote", "cpu")


@pytest.fixture
def rented():
    descriptor = dataclasses.replace(
        worker._descriptor("rent-cpu", "p1", None, 1), kind="route_other", target="t-1"
    )
    register_provider(_Rented(descriptor))
    yield "rent-cpu"
    unregister_provider("rent-cpu")


def test_a_non_salad_remote_provider_is_routed_by_the_default_policy(local_providers, rented):
    alternates = [{"provider": rented, "runtime": "docker-cpu-v1"}]
    stage = normalize(
        parse_spec(
            _spec2(
                _stage2("a", provider=NEAR, memory_bytes=2 * GIB, alternate_providers=alternates)
            )
        )
    ).stages[0]
    assert preference.candidate_kind(stage, rented) == "remote_cpu"
    order, record = preference.preferred_order(stage)
    assert order == (rented, NEAR) and record["reordered"] is True
    assert record["kinds"] == {NEAR: "local", rented: "remote_cpu"}
    # An egress-bound stage keeps it behind the local candidate.
    bound = dataclasses.replace(stage, egress=EgressPolicy.model_validate(EGRESS))
    assert preference.preferred_order(bound)[0] == (NEAR, rented)


def test_placement_capability_of_reports_each_provider_kind(local_providers, rented):
    cpu, gpu = _normal(gpu=False), _normal(gpu=True)
    assert placement_capability_of(get_provider(NEAR), cpu) == PlacementCapability("local", "cpu")
    salad = get_provider("salad")
    on_cpu, on_gpu = stage_on_candidate(cpu, "salad"), stage_on_candidate(gpu, "salad")
    assert placement_capability_of(salad, on_cpu) == PlacementCapability("remote", "cpu")
    assert placement_capability_of(salad, on_gpu) == PlacementCapability("remote", "gpu")

    class _Plain:  # no method, not a worker-grant kind: placement leaves it where it is
        descriptor = dataclasses.replace(
            get_provider(NEAR).descriptor, kind="other_kind", target="t-1"
        )

    class _Broken(_Plain):
        def placement_capability(self, stage):
            raise RuntimeError("unreadable stage config")

    assert placement_capability_of(_Plain(), cpu) is None
    assert placement_capability_of(_Broken(), cpu) is None
    assert placement_capability_of(get_provider(rented), cpu) == PlacementCapability(
        "remote", "cpu"
    )


def test_a_provider_without_a_capability_is_kind_other(local_providers):
    class _Plain(worker.WorkerGrantProvider):
        pass

    descriptor = dataclasses.replace(
        worker._descriptor("plain-x", "p1", None, 1), kind="other_kind", target="t-1"
    )
    register_provider(_Plain(descriptor))
    try:
        assert preference.candidate_kind(_listing("plain-x"), "plain-x") == "other"
    finally:
        unregister_provider("plain-x")


def test_the_placement_preference_module_names_no_vendor():
    path = Path(preference.__file__)
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if "salad" in a.name.lower()]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or "", *(a.name for a in node.names)]
            found += [n for n in names if "salad" in n.lower()]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and "salad" in node.value.lower():
                found.append(node.value)
    assert found == []


# -- 10. the expected_duration_seconds stage field -------------------------------------------------


def test_expected_duration_seconds_is_absent_unless_set_and_keeps_digests():
    plain = _spec2(_stage2("a", max_duration_seconds=3600))

    def with_estimate(value):
        return {**plain, "stages": [{**plain["stages"][0], "expected_duration_seconds": value}]}

    assert "expected_duration_seconds" not in parse_spec(plain).model_dump(mode="json")["stages"][0]
    assert spec_digest(parse_spec(with_estimate(None))) == spec_digest(parse_spec(plain))
    assert spec_digest(parse_spec(with_estimate(1800))) != spec_digest(parse_spec(plain))
    assert normalize(parse_spec(with_estimate(1800))).stages[0].expected_duration_seconds == 1800
    assert normalize(parse_spec(plain)).stages[0].expected_duration_seconds is None


@pytest.mark.parametrize("bad", [0, -1, 3601, 604_801, "1h", 1.5, True])
def test_expected_duration_seconds_above_the_cap_or_malformed_is_refused(bad):
    stage = _stage2("a", max_duration_seconds=3600, expected_duration_seconds=bad)
    with pytest.raises(ValueError):
        parse_spec(_spec2(stage))
