# ruff: noqa: F811
"""Operator placement preference by resource class (ADR 0070). Fakes only.

Uses the two-provider world of ``test_campaign_placement``: ``near-cpu`` (the project host)
and ``far-cpu`` (another registered CPU provider, standing in for a preferred heavy-stage
provider such as a Salad CPU profile). No network, no provider, no money.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import dump
from test_campaign_models_v2 import _spec2
from test_campaign_placement import (
    FAR,
    GHOST,
    NEAR,
    _placed,
    _receipt,
    _stage,
    _trust,
    _world,
    providers,  # noqa: F401 - pytest fixture
)

from tessera_control import campaign_placement_preference as preference
from tessera_control.campaign_models import normalize, parse_spec, spec_digest
from tessera_control.campaign_providers import configure_providers, provider_ids
from tessera_control.store import utc_now

HEAVY_SECONDS = 600
METRICS = [
    {"name": "score", "path": "data.json", "json_pointer": "/score"},
    {"name": "stopped", "path": "data.json", "json_pointer": "/stopped"},
]


@pytest.fixture(autouse=True)
def _no_preference():
    from tessera_control.campaign_providers import unregister_provider

    preference.configure(None, ())
    yield
    preference.configure(None, ())
    unregister_provider(GHOST)  # ``_world(ghost=True)`` registers it outside the fixture


def _rules(*prefer: str, **when) -> dict:
    when = when or {"min_duration_seconds": HEAVY_SECONDS, "any_tags": ["heavy"]}
    return {"rules": [{"name": "heavy", "when": when, "prefer": list(prefer)}]}


def _prefer(*prefer: str, **when) -> None:
    preference.configure(_rules(*prefer, **when), provider_ids())


def _heavy(env, key, **extra):
    return _stage(env, key, max_duration_seconds=HEAVY_SECONDS, **extra)


# -- 1. absent config ---------------------------------------------------------------------------


def test_without_a_preference_a_heavy_stage_keeps_the_spec_order_and_records_nothing_new(
    tmp_path, providers
):
    world = _world(tmp_path, providers)
    campaign = world.campaign(_spec2(_heavy(world.env, "a", placement_tags=["heavy"])))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR and placed["candidates"] == [NEAR, FAR]
    # No operator rule, no Salad candidate: the default policy (ADR 0088) still records its class
    # and that the spec's order stood; nothing is reordered.
    record = placed["preference"]
    assert record["source"] == "default_policy" and record["class"] == "heavy"
    assert record["reordered"] is False and record["order"] == [NEAR, FAR]
    assert record["chosen"] == NEAR and record["fell_back"] == []
    entry = _receipt(world, campaign["id"])["stages"][0]["attempts"][0]
    assert entry["placement"]["preference"]["class"] == "heavy"


def test_placement_tags_are_absent_from_the_spec_unless_set_and_keep_digests():
    from test_campaign_models_v2 import _stage2

    plain = _spec2(_stage2("a"))
    assert "placement_tags" not in parse_spec(plain).model_dump(mode="json")["stages"][0]
    explicit = {**plain, "stages": [{**plain["stages"][0], "placement_tags": None}]}
    assert spec_digest(parse_spec(explicit)) == spec_digest(parse_spec(plain))
    tagged = {**plain, "stages": [{**plain["stages"][0], "placement_tags": ["heavy"]}]}
    assert spec_digest(parse_spec(tagged)) != spec_digest(parse_spec(plain))
    assert normalize(parse_spec(tagged)).stages[0].placement_tags == ("heavy",)
    for bad in (["heavy", "heavy"], ["Heavy"], [], ["a"] * 9):
        with pytest.raises(ValueError):
            parse_spec({**plain, "stages": [{**plain["stages"][0], "placement_tags": bad}]})


# -- 2. a heavy stage prefers the configured provider ---------------------------------------------


@pytest.mark.parametrize(
    ("extra", "matched"),
    [
        ({"max_duration_seconds": HEAVY_SECONDS}, ["min_duration_seconds"]),
        ({"placement_tags": ["heavy"]}, ["any_tags"]),
    ],
)
def test_a_heavy_stage_is_placed_on_the_preferred_candidate_and_the_rule_is_recorded(
    tmp_path, providers, extra, matched
):
    world = _world(tmp_path, providers)
    spec = _spec2(_stage(world.env, "a", **extra))
    digest = spec_digest(parse_spec(spec))
    _prefer(FAR, NEAR)
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == FAR and placed["candidates"] == [FAR, NEAR]
    assert placed["skipped"] == [] and placed["not_evaluated"] == [NEAR]
    assert placed["preference"] == {
        "rule": "heavy",
        "matched": matched,
        "prefer": [FAR, NEAR],
        "spec_order": [NEAR, FAR],
        "order": [FAR, NEAR],
        "reordered": True,
    }
    entry = _receipt(world, campaign["id"])["stages"][0]["attempts"][0]
    assert entry["provider_id"] == FAR
    assert entry["placement"]["preference"]["rule"] == "heavy"
    # The operator rule never touches the approved spec.
    stored = world.row("SELECT spec_digest FROM campaigns WHERE id=?", campaign["id"])
    assert stored["spec_digest"] == digest


def test_a_light_stage_keeps_the_spec_order(tmp_path, providers):
    world = _world(tmp_path, providers)
    _prefer(FAR, NEAR)
    campaign = world.campaign(_spec2(_stage(world.env, "a", placement_tags=["light"])))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR and placed["candidates"] == [NEAR, FAR]
    assert "preference" not in placed


def test_the_scratch_criterion_and_first_matching_rule(tmp_path, providers):
    world = _world(tmp_path, providers)
    preference.configure(
        {
            "rules": [
                {"name": "big-disk", "when": {"min_scratch_bytes": 1 << 30}, "prefer": [FAR]},
                {"name": "slow", "when": {"min_duration_seconds": 60}, "prefer": [GHOST]},
            ]
        },
        [NEAR, FAR, GHOST],
    )
    base = normalize(parse_spec(_spec2(_stage(world.env, "a", alternates=(FAR,))))).stages[0]
    big = normalize(
        parse_spec(_spec2(_stage(world.env, "a", alternates=(FAR,), scratch_bytes=1 << 30)))
    ).stages[0]
    order, record = preference.preferred_order(big)
    assert order == (FAR, NEAR) and record["rule"] == "big-disk"
    assert record["matched"] == ["min_scratch_bytes"]
    # The second rule matches the 60 s stage but names a provider the stage does not list: the
    # order is the spec's, and the rule is still recorded as applied (not reordered).
    order, record = preference.preferred_order(base)
    assert order == (NEAR, FAR)
    assert record["rule"] == "slow" and record["reordered"] is False


# -- 3. fallback in preference order, every check still applied -----------------------------------


def test_a_busy_preferred_provider_falls_back_to_the_next(tmp_path, providers):
    world = _world(tmp_path, providers)
    world.scenarios = [(0, {"data.json": '{"score": 1}'}), (0, {"data.json": '{"score": 2}'})]
    _prefer(FAR, NEAR)
    campaign = world.campaign(_spec2(_heavy(world.env, "a"), _heavy(world.env, "b")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    by_stage = {p["stage_key"]: p for p in _placed(world, campaign["id"])}
    assert by_stage["a"]["provider_id"] == FAR
    assert by_stage["b"]["provider_id"] == NEAR
    assert [(s["provider_id"], s["reason"]) for s in by_stage["b"]["skipped"]] == [
        (FAR, "no_free_slot")
    ]


def test_an_untrusted_preferred_provider_falls_back(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    world.scenarios = [(0, {"data.json": '{"score": 1}'}), (0, {"data.json": '{"score": 2}'})]
    _prefer(FAR, NEAR)
    campaign = world.campaign(_spec2(_stage(env, "a"), _heavy(env, "b", upstream="a")))
    _trust(env, world.far_worker, far=["public"])  # b's derived input may not go to far-cpu
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    placed = {p["stage_key"]: p for p in _placed(world, campaign["id"])}
    assert placed["b"]["provider_id"] == NEAR
    assert [s["reason"] for s in placed["b"]["skipped"]] == ["untrusted"]
    assert placed["b"]["preference"]["order"] == [FAR, NEAR]


def test_an_unregistered_preferred_provider_falls_back(tmp_path, providers):
    from tessera_control.campaign_providers import unregister_provider

    world = _world(tmp_path, providers)
    _prefer(FAR, NEAR)
    campaign = world.campaign(_spec2(_heavy(world.env, "a")))
    unregister_provider(FAR)  # the operator restarted without it
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR
    assert [(s["provider_id"], s["reason"]) for s in placed["skipped"]] == [(FAR, "unregistered")]


def test_a_preferred_provider_whose_worker_is_no_longer_enrolled_falls_back(tmp_path, providers):
    world = _world(tmp_path, providers)
    _prefer(FAR, NEAR)
    campaign = world.campaign(_spec2(_heavy(world.env, "a")))
    world.env.execute(
        "UPDATE principals SET revoked_at=? WHERE id=?", utc_now(), world.far_worker["id"]
    )
    world.tick_until(lambda: bool(_placed(world, campaign["id"])), worker=False)
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR
    assert [(s["provider_id"], s["reason"]) for s in placed["skipped"]] == [(FAR, "not_enrolled")]


def test_a_preferred_provider_over_the_remaining_budget_falls_back_in_preference_order(
    tmp_path, providers
):
    world = _world(tmp_path, providers, ghost=True)
    env = world.env
    pricey = {"provider": FAR, "runtime": "docker-cpu-v1", "max_exposure_cents": 40}
    cheap = {"provider": GHOST, "runtime": "docker-cpu-v1", "max_exposure_cents": 10}
    spec = _spec2(
        _stage(env, "a", alternates=(), exposure=30),
        _heavy(env, "b", alternates=(pricey, cheap), exposure=40),
        budget_cap_cents=60,
    )
    env.execute("UPDATE execution_project_limits SET monetary_limit_cents=1000")
    _prefer(FAR, GHOST)
    campaign = world.campaign(spec)
    world.tick_until(lambda: len(world.attempts(campaign["id"])) == 2, worker=False)
    placed = {p["stage_key"]: p for p in _placed(world, campaign["id"])}
    # a holds 30 of 60: far-cpu's 40 does not fit while it is held, the next preferred one does.
    assert placed["b"]["candidates"] == [FAR, GHOST, NEAR]
    assert placed["b"]["provider_id"] == GHOST and placed["b"]["hold_cents"] == 10
    assert [(s["provider_id"], s["reason"]) for s in placed["b"]["skipped"]] == [
        (FAR, "budget_in_flight")
    ]


# -- 4. never a new destination ------------------------------------------------------------------


def test_a_preferred_provider_the_stage_does_not_list_is_never_used(tmp_path, providers):
    world = _world(tmp_path, providers, ghost=True)
    _prefer(GHOST, FAR)
    campaign = world.campaign(_spec2(_heavy(world.env, "a")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["candidates"] == [FAR, NEAR] and placed["provider_id"] == FAR
    assert {a["provider_id"] for a in world.attempts(campaign["id"])} == {FAR}


def test_a_stage_without_alternates_is_never_placed_by_a_preference(tmp_path, providers):
    world = _world(tmp_path, providers)
    _prefer(FAR, NEAR)
    campaign = world.campaign(_spec2(_heavy(world.env, "a", alternates=())))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert not _placed(world, campaign["id"])
    assert {a["provider_id"] for a in world.attempts(campaign["id"])} == {NEAR}


def test_reroute_only_targets_stay_last(tmp_path, providers):
    world = _world(tmp_path, providers, ghost=True)
    _prefer(GHOST, FAR)
    stage = normalize(
        parse_spec(
            _spec2(
                _heavy(
                    world.env,
                    "a",
                    retry={"max_attempts": 1, "infra_retries": 1, "reroute_providers": [GHOST]},
                )
            )
        )
    ).stages[0]
    order, record = preference.preferred_order(stage)
    assert order == (FAR, NEAR, GHOST) and record["order"] == [FAR, NEAR, GHOST]


# -- 5. ADR 0067 resource-limit retry -------------------------------------------------------------


def test_a_resource_limit_stop_on_the_preferred_provider_moves_to_the_next(
    tmp_path, providers, monkeypatch
):
    from tessera_control.campaign_provider_worker import WorkerGrantProvider

    world = _world(tmp_path, providers)
    monkeypatch.setattr(WorkerGrantProvider, "memory_report", lambda *_: None)
    world.scenarios = [
        (0, {"data.json": json.dumps({"score": 1, "stopped": 1})}),
        (0, {"data.json": json.dumps({"score": 2, "stopped": 0})}),
    ]
    _prefer(FAR, NEAR)
    stage = _heavy(
        world.env,
        "a",
        metrics=METRICS,
        retry={"max_attempts": 1, "infra_retries": 1, "resource_limit_metrics": ["stopped"]},
    )
    cid = world.campaign(_spec2(stage))["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert [(a["provider_id"], a["failure_class"]) for a in world.attempts(cid)] == [
        (FAR, "infra_resource_limit"),
        (NEAR, None),
    ]
    placed = {p["attempt"]: p for p in _placed(world, cid)}
    assert placed[1]["provider_id"] == FAR and placed[1]["preference"]["order"] == [FAR, NEAR]
    # The infrastructure retry tries the untried candidate first, in preference order, and the
    # one that hit the limit is not chosen again.
    assert placed[2]["provider_id"] == NEAR and placed[2]["candidates"] == [NEAR, FAR]
    assert placed[2]["not_evaluated"] == [FAR]
    assert placed[2]["preference"]["rule"] == "heavy"


def test_a_memory_kill_on_the_fallback_retries_on_the_preferred_provider(
    tmp_path, providers, monkeypatch
):
    """The preferred provider is busy, so ``b`` falls back to the host and is killed (137).
    Its infrastructure retry goes to the preferred provider, never back to the host whose
    free memory is not shown above the limit (ADR 0067), waiting for the slot if needed."""
    from tessera_control.campaign_provider_worker import WorkerGrantProvider

    world = _world(tmp_path, providers)
    monkeypatch.setattr(WorkerGrantProvider, "memory_report", lambda *_: None)
    # The host's poller runs first each cycle: its run is killed, far-cpu's succeed.
    world.scenarios = [
        (137, {"data.json": '{"score": 0}'}),
        (0, {"data.json": '{"score": 1}'}),
        (0, {"data.json": '{"score": 2}'}),
    ]
    _prefer(FAR, NEAR)
    retry = {"max_attempts": 1, "infra_retries": 1}
    campaign = world.campaign(
        _spec2(_heavy(world.env, "a", retry=retry), _heavy(world.env, "b", retry=retry))
    )
    cid = campaign["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    by_stage: dict[str, list] = {}
    for attempt in world.attempts(cid):
        by_stage.setdefault(attempt["stage_key"], []).append(
            (attempt["provider_id"], attempt["failure_class"])
        )
    assert by_stage["a"] == [(FAR, None)]
    assert by_stage["b"] == [(NEAR, "infra_container_killed"), (FAR, None)]
    retried = [p for p in _placed(world, cid) if p["stage_key"] == "b" and p["attempt"] == 2]
    assert retried[0]["provider_id"] == FAR and retried[0]["preference"]["rule"] == "heavy"


# -- 6. inspection ---------------------------------------------------------------------------------


def test_the_campaign_read_shows_the_rules_and_whether_they_apply(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _prefer(FAR, NEAR)
    campaign = world.campaign(
        _spec2(
            _heavy(env, "heavy"),
            _heavy(env, "single", alternates=()),
            _heavy(env, "first", provider=FAR, alternates=(NEAR,)),
            _stage(env, "light"),
        )
    )
    view = env.store.get_campaign(env.owner, campaign["id"])["placement_preference"]
    assert view["configured"] is True
    assert view["rules"] == [
        {
            "name": "heavy",
            "when": {"min_duration_seconds": HEAVY_SECONDS, "any_tags": ["heavy"]},
            "prefer": [FAR, NEAR],
        }
    ]
    assert view["stages"] == [
        {
            "stage_key": "heavy",
            "rule": "heavy",
            "matched": ["min_duration_seconds"],
            "preferred_listed": [FAR, NEAR],
            "applies": True,
        },
        {
            "stage_key": "single",
            "rule": "heavy",
            "matched": ["min_duration_seconds"],
            "preferred_listed": [NEAR],
            "applies": False,
        },
        {  # the preferred provider is already first: the rule changes nothing
            "stage_key": "first",
            "rule": "heavy",
            "matched": ["min_duration_seconds"],
            "preferred_listed": [FAR, NEAR],
            "applies": False,
        },
    ]
    preference.configure(None, ())
    cleared = env.store.get_campaign(env.owner, campaign["id"])["placement_preference"]
    assert cleared["configured"] is False and cleared["rules"] == [] and cleared["stages"] == []
    assert cleared["default_policy"]["enabled"] is True  # the built-in policy needs no rule


# -- 7. operator config validation ----------------------------------------------------------------


def _one(when, prefer=(FAR,), **extra):
    return {"rules": [{"name": "h", "when": when, "prefer": list(prefer), **extra}]}


@pytest.mark.parametrize(
    "section",
    [
        {"default_policy": "yes"},
        {"heavy_memory_bytes": 1},
        {"long_runtime_seconds": 0},
        {"unknown": 1},
        _one({}),
        _one({"min_duration_seconds": 0}),
        _one({"min_duration_seconds": 86_401}),
        _one({"min_duration_seconds": "600"}),
        _one({"min_scratch_bytes": 1}),
        _one({"any_tags": ["Heavy"]}),
        _one({"any_tags": ["x", "x"]}),
        _one({"min_memory_bytes": 1}),
        _one({"any_tags": ["x"]}, prefer=()),
        _one({"any_tags": ["x"]}, prefer=(FAR, FAR)),
        _one({"any_tags": ["x"]}, prefer=("Bad_Id",)),
        _one({"any_tags": ["x"]}, extra=1),
        {"rules": _one({"any_tags": ["x"]})["rules"] * 2},
        {"rules": [{**_one({"any_tags": ["x"]})["rules"][0], "name": f"r{i}"} for i in range(9)]},
        _one({"any_tags": ["x"]}, prefer=(NEAR, FAR, "a", "b", "c", "d")),
        {"rules": [{"name": "Bad Name", "when": {"any_tags": ["x"]}, "prefer": [FAR]}]},
        [],
    ],
)
def test_a_malformed_preference_is_refused_and_installs_nothing(section):
    with pytest.raises(ValueError):
        preference.configure(section, [NEAR, FAR, "a", "b", "c", "d"])
    assert preference.active() is None


def test_a_preference_naming_an_unregistered_provider_is_refused():
    with pytest.raises(ValueError, match="unregistered"):
        preference.configure(_rules("nowhere"), [NEAR, FAR])
    assert preference.active() is None


def test_the_providers_file_carries_the_section_and_absent_clears_it(monkeypatch):
    calls: list[str] = []
    configure_providers({"placement_preferences": _rules("local-docker")})
    try:
        assert preference.active() is not None
        assert preference.active().rules[0].prefer == ["local-docker"]
        configure_providers(None)
        assert preference.active() is None
        # A malformed section is refused before any provider is (re)registered.
        from tessera_control import campaign_providers as cp

        monkeypatch.setattr(cp, "load_builtin_providers", lambda *_: calls.append("loaded"))
        with pytest.raises(ValueError):
            configure_providers({"placement_preferences": {"rules": "nope"}})
        assert calls == [] and preference.active() is None
    finally:
        monkeypatch.undo()
        configure_providers(None)
