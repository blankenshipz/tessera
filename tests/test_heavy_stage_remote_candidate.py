# ruff: noqa: F811
"""Heavy stages get the operator's remote candidate at submit (ADR 0141).

Fakes only: the Salad executor, guardian and human are ``SaladWorld`` test doubles, the CPU host
is a worker-grant provider with a fixed number of free slots, and no Salad call is made.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import salad_provider_reset, salad_spec, salad_stage  # noqa: F401
from test_salad_stage_adapter import NEAR, cpu_world, near, spec_of  # noqa: F401

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_heavy_remote as heavy_remote
from tessera_control import campaign_placement_preference as preference
from tessera_control import program_budget
from tessera_control.campaign_models import HEAVY_REMOTE_ADDED, parse_spec
from tessera_control.store import canonical_digest

GIB = 1024**3
HEAVY = {"alternate_providers": None, "memory_bytes": 2 * GIB}
SETTING = {"provider": "salad", "max_exposure_cents": 10}


@pytest.fixture(autouse=True)
def _reset():
    preference.configure(None, ())
    program_budget.reset()
    yield
    preference.configure(None, ())
    program_budget.reset()


def _configure(**over):
    preference.configure({"heavy_remote_candidate": {**SETTING, **over}}, [NEAR, "salad"])


def _stored(world) -> dict:
    return json.loads(world.env.query("SELECT spec_json FROM campaigns")[0]["spec_json"])


def _submit(world, **over) -> dict:
    world.spec = spec_of(world, **over)
    submitted = world.env.submit(spec=world.spec)
    return _stored(world)["stages"][0] | {"_digest": submitted["spec_digest"]}


def _placed(world):
    return world.events("campaign.stage_placed")


def test_a_heavy_local_only_stage_lists_salad_first_and_is_placed_there(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near, slots=1)  # the local host is idle
    world.spec = spec_of(world, **HEAVY)
    world.start()
    stage = _stored(world)["stages"][0]
    first = stage["alternate_providers"][0]
    assert first["provider"] == "salad" and first["provider_config"]["adapted"] is True
    assert first["provider_config"]["profile"] == "cpu-main"
    assert stage["heavy_remote_candidate"] == {
        "added": True,
        "reason": HEAVY_REMOTE_ADDED,
        "provider": "salad",
        "signals": ["declared_memory"],
        "profile": "cpu-main",
        "max_exposure_cents": first["max_exposure_cents"],
    }
    # Within every cap: the operator's, the stage's per-run cap and the campaign's budget.
    assert 0 < first["max_exposure_cents"] <= min(10, stage["max_exposure_cents"], 50)
    # The digest a person approved covers the added candidate and its record.
    campaign = world.row("SELECT spec_digest FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["spec_digest"] == canonical_digest(_stored(world))
    world.tick_until(lambda: bool(_placed(world)))
    (placed,) = _placed(world)
    assert placed["provider_id"] == "salad" and placed["candidates"][0] == "salad"
    assert not world.events(guard.EVENT)  # not a local fallback
    from tessera_control.campaign_receipts import build_receipt

    with world.env.store.runner_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, row, "running")
    assert receipt["stages"][0]["heavy_remote_candidate"]["reason"] == HEAVY_REMOTE_ADDED


def test_validate_shows_the_same_digest_as_submit(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    env = world.env
    body = {"spec": spec_of(world, **HEAVY)}
    checked = env.store.validate_campaign_spec(env.owner, env.project["id"], body)
    record = checked["expanded_spec"]["stages"][0]["heavy_remote_candidate"]
    assert record["added"] is True
    assert env.submit(spec=spec_of(world, **HEAVY))["spec_digest"] == checked["spec_digest"]


def test_a_light_stage_is_unchanged(tmp_path, near):
    world = cpu_world(tmp_path, near)
    plain = parse_spec(spec_of(world, alternate_providers=None))
    _configure()
    stage = _submit(world, alternate_providers=None)
    assert "alternate_providers" not in stage and "heavy_remote_candidate" not in stage
    assert stage["_digest"] == canonical_digest(plain.model_dump(mode="json"))


def test_a_stage_already_listing_salad_is_unchanged(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    stage = _submit(world, memory_bytes=2 * GIB)  # the spec's own adapter request
    assert [item["provider"] for item in stage["alternate_providers"]] == ["salad"]
    assert "heavy_remote_candidate" not in stage


def test_without_the_setting_nothing_is_added_and_the_local_fallback_is_warned(tmp_path, near):
    world = cpu_world(tmp_path, near, slots=1)
    world.spec = spec_of(world, **HEAVY)
    world.start()
    stage = _stored(world)["stages"][0]
    assert "alternate_providers" not in stage and "heavy_remote_candidate" not in stage
    world.tick_until(lambda: world.attempt() is not None)
    assert world.attempt()["provider_id"] == NEAR
    (event,) = world.events(guard.EVENT)
    assert event["reasons"] == ["not_listed"] and event["signals"] == ["declared_memory"]


@pytest.mark.parametrize(
    ("setting", "stage_over", "reason"),
    [
        ({"max_exposure_cents": 1}, {}, "over_operator_cap"),
        ({}, {"max_exposure_cents": 1}, "over_stage_cap"),
    ],
)
def test_a_candidate_over_a_cap_is_not_added_and_says_why(
    tmp_path, near, setting, stage_over, reason
):
    _configure(**setting)
    world = cpu_world(tmp_path, near)
    stage = _submit(world, **HEAVY, **stage_over)
    assert "alternate_providers" not in stage
    record = stage["heavy_remote_candidate"]
    assert record["added"] is False and record["reason"] == reason
    assert record["profile"] == "cpu-main" and "estimate" in record["detail"]


def test_a_candidate_a_program_budget_cannot_admit_is_not_added(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    program_budget.configure(
        program_budget.ProgramBudgetConfig.model_validate(
            {
                "schema": program_budget.PROGRAM_BUDGET_SCHEMA,
                "programs": [{"id": "tiny", "cap_cents": 1}],
            }
        )
    )
    stage = _submit(world, **HEAVY)
    assert "alternate_providers" not in stage
    record = stage["heavy_remote_candidate"]
    assert record["reason"] == "program_budget"
    assert record["detail"].startswith("program_budget_exhausted:tiny")


def test_a_gpu_heavy_stage_gets_no_cpu_candidate(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    stage = _submit(world, **HEAVY, placement_tags=["gpu"])
    assert "alternate_providers" not in stage
    record = stage["heavy_remote_candidate"]
    # Only a CPU profile is registered: a GPU stage is never offered it (ADR 0141 amendment).
    assert record["added"] is False and record["reason"] == "no_gpu_profile"


def test_no_fitting_profile_is_recorded(tmp_path, near):
    _configure(profile="nope")
    world = cpu_world(tmp_path, near)
    record = _submit(world, **HEAVY)["heavy_remote_candidate"]
    assert record["reason"] == "no_fitting_profile"


def test_a_stage_the_adapter_refuses_is_recorded_and_still_submitted(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    # Over the Salad duration limit: the adapter refuses, and submit still succeeds locally.
    stage = _submit(world, **HEAVY, max_duration_seconds=3601, grant_ttl_seconds=4300)
    assert "alternate_providers" not in stage
    record = stage["heavy_remote_candidate"]
    assert record["reason"] == "profile_unfit" and "3600" in record["detail"]


def test_a_submitted_record_is_discarded_and_recomputed(tmp_path, near):
    world = cpu_world(tmp_path, near)
    forged = {
        "added": True,
        "reason": HEAVY_REMOTE_ADDED,
        "provider": "salad",
        "signals": ["declared_memory"],
        "profile": "cpu-main",
        "max_exposure_cents": 2,
    }
    # Without the setting: the forged record and the alternate it claims are both dropped.
    stage = _submit(world, memory_bytes=2 * GIB, heavy_remote_candidate=forged)
    assert "heavy_remote_candidate" not in stage and "alternate_providers" not in stage


def test_the_setting_is_strict():
    for bad in (
        {"provider": "local-docker", "max_exposure_cents": 1},
        {"provider": "salad", "max_exposure_cents": 0},
        {"provider": "salad", "max_exposure_cents": 1, "extra": 1},
        {"provider": "salad"},
    ):
        with pytest.raises(ValueError):
            preference.configure({"heavy_remote_candidate": bad}, ())
        assert preference.active() is None


def test_a_v1_spec_is_left_unchanged(tmp_path):
    from salad_campaign_fixture import Env

    _configure()
    env = Env(tmp_path)
    spec = parse_spec(salad_spec(env, [salad_stage(env)]))
    assert spec.version == 1
    assert heavy_remote.apply(None, None, {"org_id": "o"}, "p", spec, None) is spec


def test_an_approved_submission_binds_the_added_candidate(tmp_path, near):
    from tessera_control.campaign_submissions import CampaignSubmissionStore

    _configure()
    world = cpu_world(tmp_path, near)
    env = world.env
    store = CampaignSubmissionStore(env.state)
    raw = spec_of(world, **HEAVY)
    checked = env.store.validate_campaign_spec(env.owner, env.project["id"], {"spec": raw})
    body = {
        "kind": "submit",
        "spec": raw,
        "expected_spec_digest": checked["spec_digest"],
        "per_run_budget_cap_cents": 20,
        "not_after": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    status, record = store.create_submission_approval(
        env.human(), env.project["id"], "create-1", canonical_digest(body), body
    )
    assert status == 201
    stage = record["spec"]["stages"][0]
    assert stage["alternate_providers"][0]["provider"] == "salad"
    assert stage["heavy_remote_candidate"]["added"] is True


def _own_entry(world, **config_over) -> dict:
    """An author-listed Salad alternate (the adapter's expansion of the spec's own request)."""
    from test_salad_stage_adapter import expanded

    entry = expanded(world).stages[0].alternate_providers[0].model_dump(mode="json")
    entry["provider_config"] = {**entry["provider_config"], **config_over}
    return entry


def _forged(**over) -> dict:
    return {
        "added": True,
        "reason": HEAVY_REMOTE_ADDED,
        "provider": "salad",
        "signals": ["declared_memory"],
        "profile": "cpu-main",
        "max_exposure_cents": 2,
    } | over


def test_a_forged_record_does_not_remove_an_author_listed_candidate_on_a_light_stage(
    tmp_path, near
):
    _configure()
    world = cpu_world(tmp_path, near)
    own = _own_entry(world)
    stage = _submit(
        world, alternate_providers=[own], heavy_remote_candidate=_forged(profile="cpu-forged")
    )
    assert [item["provider"] for item in stage["alternate_providers"]] == ["salad"]
    assert stage["alternate_providers"][0]["provider_config"]["profile"] == "cpu-main"
    assert "heavy_remote_candidate" not in stage


def test_a_forged_record_leaves_an_author_listed_candidate_on_a_heavy_stage(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    own = _own_entry(world)
    stage = _submit(
        world,
        alternate_providers=[own],
        memory_bytes=2 * GIB,
        heavy_remote_candidate=_forged(profile="cpu-forged"),
    )
    # The stage lists a remote candidate, so the rule adds none and records nothing.
    assert len(stage["alternate_providers"]) == 1
    assert stage["alternate_providers"][0]["provider_config"]["profile"] == "cpu-main"
    assert "heavy_remote_candidate" not in stage


def test_a_forged_record_removes_only_a_first_alternate_of_the_derived_shape(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    derived = _own_entry(world)  # adapted, profile cpu-main: what the adapter derives
    stage = _submit(
        world,
        alternate_providers=[derived],
        memory_bytes=2 * GIB,
        heavy_remote_candidate=_forged(max_exposure_cents=derived["max_exposure_cents"]),
    )
    # Dropped with the record, then recomputed by the rule: Tessera's own record stands.
    assert [item["provider"] for item in stage["alternate_providers"]] == ["salad"]
    assert stage["heavy_remote_candidate"]["added"] is True
    # Provider candidates are distinct, so a record can never account for more than one removal.
