"""Review follow-ups to the campaign prep work (ADR 0077): tenant-scoped profiles, standing
approvals that expand the adapter request, and trust hints from the refused class. Fakes only."""
# ruff: noqa: F811

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec,
    salad_stage,
)
from test_salad_stage_adapter import (  # noqa: F401
    NEAR,
    cpu_world,
    near,
    request,
    spec_of,
    stage,
)

from tessera_control.campaign_models import SALAD_RUNTIME, CampaignSpec, normalize, parse_spec
from tessera_control.campaign_providers import get_provider
from tessera_control.campaign_salad_adapter import expand_spec
from tessera_control.campaign_store import CampaignStore
from tessera_control.store import Conflict, _connect, canonical_digest

FOREIGN = dict(org_id="org_other", project_id="proj_other")
DETAILS = ("disk", "8192", "estimate", "max_input_bytes", "allowed_image_digests", "1" * 64)


def body_of(world, **over):
    return {"spec": spec_of(world, **over)}


def refusal_text(world, **over):
    env = world.env
    body = body_of(world, **over)
    with pytest.raises(Conflict) as refused:
        env.store.submit_campaign(env.owner, env.project["id"], "k", canonical_digest(body), body)
    return str(refused.value)


# -- 1. another tenant's profile is not visible ------------------------------------------------


def test_a_foreign_profile_is_refused_like_an_unknown_one_and_leaks_nothing(tmp_path, near):
    foreign = cpu_world(tmp_path, near, **FOREIGN)
    missing = refusal_text(foreign, alternate_providers=[request("nope")])
    for call in ("validate", "submit"):
        env = foreign.env
        body = body_of(foreign)
        with pytest.raises(Conflict) as refused:
            if call == "validate":
                env.store.validate_campaign_spec(env.owner, env.project["id"], body)
            else:
                env.store.submit_campaign(
                    env.owner, env.project["id"], "k", canonical_digest(body), body
                )
        text = str(refused.value)
        assert text == missing.replace("'nope'", "'cpu-main'")
        assert "unknown profile" in text and not any(d in text for d in DETAILS)


def test_the_own_tenant_profile_still_expands(tmp_path, near):
    world = cpu_world(tmp_path, near)
    env = world.env
    checked = env.store.validate_campaign_spec(env.owner, env.project["id"], body_of(world))
    assert checked["expanded_spec"]["stages"][0]["alternate_providers"][0]["max_exposure_cents"]


def test_pure_expansion_scopes_by_tenant_only_when_given_one(tmp_path, near):
    world = cpu_world(tmp_path, near, **FOREIGN)
    spec = parse_spec(spec_of(world))
    with pytest.raises(Conflict, match="unknown profile"):
        expand_spec(spec, world.profiles, None, ("org_mine", "proj_mine"))
    assert expand_spec(spec, world.profiles) is not spec  # no tenant: unchanged behaviour
    assert expand_spec(spec, world.profiles, None, ("org_other", "proj_other")) is not spec


# -- 2. standing approvals expand the request ---------------------------------------------------


def standing_body(world, spec_body, digest, **over):
    body = {
        "spec": spec_body,
        "expected_spec_digest": digest,
        "per_start_budget_cap_cents": 20,
        "max_starts": 1,
        "max_total_budget_cents": 20,
        "not_after": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    return {**body, **over}


def create_standing(world, body):
    env = world.env
    return env.store.create_standing_approval(
        env.human(), env.project["id"], "standing-1", canonical_digest(body), body
    )


def test_a_standing_approval_expands_the_adapter_request_and_stores_the_explicit_candidate(
    tmp_path, near
):
    world = cpu_world(tmp_path, near)
    env = world.env
    spec_body = spec_of(world)
    checked = env.store.validate_campaign_spec(env.owner, env.project["id"], {"spec": spec_body})
    status, created = create_standing(
        world, standing_body(world, spec_body, checked["spec_digest"])
    )
    assert status == 201
    stored = json.loads(
        env.query("SELECT spec_json FROM campaign_standing_approvals")[0]["spec_json"]
    )
    config = stored["stages"][0]["alternate_providers"][0]["provider_config"]
    assert config["adapted"] is True and "adapt" not in config
    assert created["spec"] == stored


def test_the_raw_spec_digest_is_a_clean_conflict_not_a_crash(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec_body = spec_of(world)
    raw = canonical_digest(parse_spec(spec_body).model_dump(mode="json"))
    with pytest.raises(Conflict, match="Spec digest does not match"):
        create_standing(world, standing_body(world, spec_body, raw))


def test_a_primary_slot_request_in_a_standing_approval_is_a_clean_conflict(tmp_path, near):
    world = cpu_world(tmp_path, near)
    own = {"provider": "salad", "runtime": SALAD_RUNTIME, "alternate_providers": None}
    own["provider_config"] = {"adapt": {"profile": "cpu-main"}}
    spec_body = spec_of(world, **own)
    raw = canonical_digest(parse_spec(spec_body).model_dump(mode="json"))
    with pytest.raises(Conflict, match="only for an alternate_providers candidate"):
        create_standing(world, standing_body(world, spec_body, raw))


# -- 3. the refused class drives the hint (admit_class itself: test_salad_prep_followups_trust) --


def stage_of(world):
    spec = salad_spec(world.env, [salad_stage(world.env, "g1")])
    return normalize(CampaignSpec.model_validate(spec)).stages[0]


def check(world):
    with _connect(world.env.state / "tessera.sqlite3") as connection:
        CampaignStore.check_stage_inputs(connection, stage_of(world), get_provider("salad"))


def test_a_derived_refusal_carries_a_merge_hint_and_an_unreadable_manifest_none(tmp_path):
    from campaign_fixture import record_snapshot_private

    world = SaladWorld(tmp_path)
    env = world.env
    record_snapshot_private(env.state, env.snapshot_id, "derived")
    with pytest.raises(Conflict) as derived:
        check(world)
    text = str(derived.value)
    assert "To admit it" in text and "merge" in text and "private_derived" in text
    assert world.worker["id"] in text and "all_registered_providers" not in text
    with _connect(env.state / "tessera.sqlite3") as db:
        db.execute("UPDATE source_snapshots SET manifest_json='{}'")
        db.commit()
    with pytest.raises(Conflict) as unreadable:
        check(world)
    assert "unreadable" in str(unreadable.value) and "To admit it" not in str(unreadable.value)
