"""Alternate execution profiles of a Salad stage (ADR 0151): spec, submit checks, approval.

Fakes only: the Salad executor, guardian and human are the ``SaladWorld`` test doubles and no
Salad call is made.
"""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError
from salad_campaign_fixture import (  # noqa: F401
    IMAGE,
    SaladWorld,
    contract_data,
    profile_data,
    program_template,
    salad_provider_reset,
    salad_spec,
    salad_stage,
)
from test_salad_stage_adapter import (
    RATE,
    near,  # noqa: F401 - pytest fixture
    request,
)
from test_salad_stage_adapter import stage as adapter_stage

from tessera_control import campaign_preapproval as pre
from tessera_control.campaign_models import (
    SaladAdapterRequest,
    SaladStageSpec,
    StagePreapproval,
    normalize,
    parse_spec,
    parse_spec_json,
    spec_digest,
)
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.campaign_runner import _Ctx
from tessera_control.campaign_salad_adapter import expand_spec
from tessera_control.salad_alternate_profiles import (
    covered_profiles,
    next_profile,
    profile_order,
    stage_on_profile,
)
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles
from tessera_control.store import Conflict, canonical_digest

# -- fixed specs: an absent field keeps an old spec's digest byte-identical --------------------

SNAPSHOT = "snapshot_" + "0" * 32
WORKER = "principal_worker_fixed"
SALAD = dict(
    profile="gpu-main",
    worker_principal_id=WORKER,
    workload=contract_data(),
    program=program_template(),
)
INPUTS = [
    {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": SNAPSHOT,
        "digest": "e" * 64,
        "data_class": "public",
    }
]
FIXED_V1 = {
    "version": 1,
    "name": "fixed salad v1",
    "budget_cap_cents": 50,
    "deadline_seconds": 86400,
    "worker_principal_id": WORKER,
    "stages": [
        {
            "key": "g1",
            "backend": "salad-gpu-v1",
            "image": IMAGE,
            "argv": ["/tessera/bin/run", *contract_data()["argv_template"]],
            "output_paths": ["output.json"],
            "max_output_bytes": 4096,
            "max_duration_seconds": 3000,
            "max_exposure_cents": 20,
            "inputs": INPUTS,
            "salad": SALAD,
        }
    ],
}
FIXED_V2 = {
    "version": 2,
    "name": "fixed salad v2",
    "budget_cap_cents": 50,
    "deadline_seconds": 86400,
    "stages": [
        {
            "key": "g1",
            "provider": "salad",
            "runtime": "salad-workload-v1",
            "provider_config": SALAD,
            "image": IMAGE,
            "argv": ["/tessera/bin/run", *contract_data()["argv_template"]],
            "max_duration_seconds": 3000,
            "max_exposure_cents": 20,
            "inputs": INPUTS,
            "outputs": [{"name": "output", "path": "output.json", "max_bytes": 4096}],
        }
    ],
}
# Computed with the code before ADR 0151 (the field did not exist then).
FIXED_V1_DIGEST = "d46b75912764281125539d8a0a124accb8c8f3a2fb5cbcd3847b08472b56c80c"
FIXED_V2_DIGEST = "2901607bfef364c631058cf95eeb776f22da92287369edff8475d34f85ecaa71"


def test_an_old_spec_keeps_its_digest_byte_identical():
    assert spec_digest(parse_spec(FIXED_V1)) == FIXED_V1_DIGEST
    assert spec_digest(parse_spec(FIXED_V2)) == FIXED_V2_DIGEST
    # The field is omitted when absent, never serialized as null or [].
    dumped = json.dumps(parse_spec(FIXED_V2).model_dump(mode="json"))
    assert "alternate_profiles" not in dumped


def test_listing_alternates_changes_the_digest_and_is_bound_by_it():
    spec = copy.deepcopy(FIXED_V2)
    spec["stages"][0]["provider_config"]["alternate_profiles"] = ["gpu-alt"]
    other = copy.deepcopy(spec)
    other["stages"][0]["provider_config"]["alternate_profiles"] = ["gpu-third"]
    digest = spec_digest(parse_spec(spec))
    assert digest != FIXED_V2_DIGEST
    assert digest != spec_digest(parse_spec(other))


# -- schema ------------------------------------------------------------------------------------


def _config(**over):
    return {**SALAD, **over}


def test_schema_bounds_distinct_and_not_the_primary():
    assert SaladStageSpec.model_validate(_config()).alternate_profiles is None
    four = ["a1", "a2", "a3", "a4"]
    assert (
        SaladStageSpec.model_validate(_config(alternate_profiles=four)).alternate_profiles == four
    )
    for bad, match in (
        ([], "at least 1"),
        ([*four, "a5"], "at most 4"),
        (["a1", "a1"], "distinct"),
        (["a1", "gpu-main"], "must not repeat profile"),
        (["Not-A-Key"], "pattern"),
    ):
        with pytest.raises(ValidationError, match=match):
            SaladStageSpec.model_validate(_config(alternate_profiles=bad))


def test_the_adapter_request_carries_alternates_under_the_same_rules():
    ok = SaladAdapterRequest.model_validate(
        {"adapt": {"profile": "cpu-main", "alternate_profiles": ["cpu-alt"]}}
    )
    assert ok.adapt.alternate_profiles == ["cpu-alt"]
    assert SaladAdapterRequest.model_validate({"adapt": {"profile": "cpu-main"}})
    for bad in (["cpu-main"], ["x", "x"], []):
        with pytest.raises(ValidationError):
            SaladAdapterRequest.model_validate(
                {"adapt": {"profile": "cpu-main", "alternate_profiles": bad}}
            )


def test_profile_order_and_stage_on_profile():
    spec = copy.deepcopy(FIXED_V2)
    spec["stages"][0]["provider_config"]["alternate_profiles"] = ["gpu-alt", "gpu-third"]
    stage = normalize(parse_spec(spec)).stage("g1")
    assert covered_profiles(stage) == ("gpu-main", "gpu-alt", "gpu-third")
    config = SaladStageSpec.model_validate(stage.provider_config)
    assert profile_order(config) == covered_profiles(stage)
    assert next_profile(config, ["gpu-main"]) == "gpu-alt"
    assert next_profile(config, ["gpu-main", "gpu-alt", "gpu-third"]) is None
    assert stage_on_profile(stage, "gpu-main") is stage
    moved = stage_on_profile(stage, "gpu-third")
    moved_config = SaladStageSpec.model_validate(moved.provider_config)
    assert moved_config.profile == "gpu-third"
    # The same approved set, nothing else; everything but the profile is unchanged.
    assert set(covered_profiles(moved)) == set(covered_profiles(stage))
    assert moved_config.alternate_profiles == ["gpu-main", "gpu-alt"]
    assert moved_config.workload == config.workload and moved_config.program == config.program
    assert moved.max_exposure_cents == stage.max_exposure_cents
    assert moved.image == stage.image and moved.provider == stage.provider
    with pytest.raises(KeyError, match="no approved profile 'gpu-rogue'"):
        stage_on_profile(stage, "gpu-rogue")
    # A v1 stage (its ``salad`` block) too.
    v1 = copy.deepcopy(FIXED_V1)
    v1["stages"][0]["salad"]["alternate_profiles"] = ["gpu-alt"]
    spec_stage = parse_spec(v1).stages[0]
    assert stage_on_profile(spec_stage, "gpu-alt").salad.profile == "gpu-alt"
    with pytest.raises(KeyError):
        stage_on_profile(parse_spec(FIXED_V1).stages[0], "gpu-alt")


# -- submit ------------------------------------------------------------------------------------


def _multi(world: SaladWorld, *extra: dict) -> None:
    """Register the world's own profile plus ``extra`` profiles (each a profile_data override)."""
    own = world.profiles.profiles[0].model_dump(mode="json")
    docs = [own, *(profile_data(world.env, **{**over}) for over in extra)]
    world.profiles = SaladCampaignProfiles.model_validate(
        {"schema": "tessera.salad.campaign-profiles.v1", "profiles": docs}
    )
    register_provider_instance(world.profiles, guardian_reader=world.guardian)


def _alt(name: str, **over) -> dict:
    return {"name": name, **over}


def _with_alternates(env, names, **over):
    stage = salad_stage(env, max_duration_seconds=600, **over)
    stage["salad"] = {**stage["salad"], "alternate_profiles": list(names)}
    # A move to the next profile happens only before any possible start, bounded by
    # ``retry.max_class_fallbacks`` (ADR 0169): one move per listed alternate.
    if "retry" not in over:
        stage["retry"] = {**stage.get("retry", {}), "max_class_fallbacks": len(names)}
    return stage


def _submit(world, spec):
    world.counter += 1
    body = {"spec": spec}
    return world.env.store.submit_campaign(
        world.env.owner, world.env.project["id"], f"s{world.counter}", canonical_digest(body), body
    )


def _spec(world, names, **over):
    from salad_campaign_fixture import salad_spec

    return salad_spec(world.env, [_with_alternates(world.env, names, **over)], budget_cap_cents=200)


def test_registered_alternates_submit_and_the_stored_spec_lists_them(tmp_path):
    world = SaladWorld(tmp_path)
    _multi(world, _alt("gpu-alt", route_max_seconds=1600))
    status, result = _submit(world, _spec(world, ["gpu-alt"]))
    assert status == 201
    stored = world.row("SELECT spec_json FROM campaigns WHERE id=?", result["id"])["spec_json"]
    assert json.loads(stored)["stages"][0]["salad"]["alternate_profiles"] == ["gpu-alt"]


def test_alternates_need_the_prestart_move_bound(tmp_path):
    """A move to the next profile happens only before any possible start (ADR 0169) and is
    bounded by ``retry.max_class_fallbacks``; it never spends a scientific attempt. A stage that
    lists alternates with that bound at 0 could never reach them: refused at submit, however
    many scientific attempts it allows."""
    world = SaladWorld(tmp_path)
    _multi(
        world, _alt("gpu-alt", route_max_seconds=1600), _alt("gpu-third", route_max_seconds=1700)
    )
    for attempts in (1, 3):
        spec = _spec(world, ["gpu-alt", "gpu-third"], retry={"max_attempts": attempts})
        with pytest.raises(Conflict, match=r"retry\.max_class_fallbacks is 0"):
            _submit(world, spec)
    status, _ = _submit(
        world, _spec(world, ["gpu-alt", "gpu-third"], retry={"max_class_fallbacks": 1})
    )
    assert status == 201
    # A stage without alternates is untouched by the rule.
    plain = salad_stage(world.env, max_duration_seconds=600, retry={"max_attempts": 1})
    status, _ = _submit(world, salad_spec(world.env, [plain], budget_cap_cents=200))
    assert status == 201


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        (None, "alternate profile 'gpu-alt': Salad stage names an unknown operator profile"),
        (
            _alt("gpu-alt", gpu_class=None, cpu_rate=RATE, cpu_millicores=2000, memory_mb=2048),
            "alternate profile 'gpu-alt': Salad stage needs a GPU profile",
        ),
        (_alt("gpu-alt", max_run_cents=10), "alternate profile 'gpu-alt': .*per-run ceiling"),
        (
            _alt("gpu-alt", max_run_cents=20, max_task_cents=80),  # 20-cent hold x 4 attempts
            None,  # 1 + 2 reallocation retries (ADR 0179) + 1 class fallback: fits exactly
        ),
        (
            _alt("gpu-alt", allowed_image_digests=["sha256:" + "2" * 64]),
            "alternate profile 'gpu-alt': .*not allowlisted.*profile 'gpu-alt'",
        ),
        (
            _alt("gpu-alt", worker_principal_id="someone-else"),
            "alternate profile 'gpu-alt': .*profile's worker",
        ),
        (
            _alt("gpu-alt", project_id="another-project"),
            "alternate profile 'gpu-alt': .*organization and project",
        ),
    ],
)
def test_each_alternate_passes_the_checks_of_the_primary(tmp_path, extra, match):
    world = SaladWorld(tmp_path)
    if extra is not None:
        _multi(world, extra)
    if match is None:
        assert _submit(world, _spec(world, ["gpu-alt"]))[0] == 201
        return
    with pytest.raises(Conflict, match=match):
        _submit(world, _spec(world, ["gpu-alt"]))
    assert world.row("SELECT 1 FROM campaigns") is None


def test_a_cpu_stage_refuses_a_gpu_alternate_and_its_cpu_estimate_must_fit(tmp_path):
    cpu = dict(gpu_class=None, cpu_rate=RATE, cpu_millicores=2000, memory_mb=2048)
    world = SaladWorld(tmp_path, **cpu)
    _multi(
        world,
        _alt("gpu-alt"),
        _alt("cpu-dear", **{**cpu, "cpu_rate": dict(vcpu_hour_usd="9", memory_gb_hour_usd="9")}),
        _alt("cpu-alt", **cpu),
    )

    def cpu_spec(names):
        from salad_campaign_fixture import salad_spec

        stage = _with_alternates(world.env, names)
        stage["salad"]["gpu"] = False
        return salad_spec(world.env, [stage])

    with pytest.raises(Conflict, match="alternate profile 'gpu-alt': .*needs a CPU-only profile"):
        _submit(world, cpu_spec(["gpu-alt"]))
    with pytest.raises(Conflict, match="alternate profile 'cpu-dear': .*estimated CPU cost"):
        _submit(world, cpu_spec(["cpu-alt", "cpu-dear"]))
    assert _submit(world, cpu_spec(["cpu-alt"]))[0] == 201


# -- adapter -----------------------------------------------------------------------------------

CPU_ADAPTER = dict(
    name="cpu-main",
    gpu_class=None,
    cpu_rate=RATE,
    cpu_millicores=2000,
    memory_mb=2048,
    capability_environment_id="salad-cpu-campaign",
    max_run_cents=50,
    max_task_cents=100,
    stage_adapter={"program": program_template(), "max_input_bytes": 1 << 20},
)


def _adapter_profiles(world, **alt_over):
    return SaladCampaignProfiles.model_validate(
        {
            "schema": "tessera.salad.campaign-profiles.v1",
            "profiles": [
                profile_data(world.env, **CPU_ADAPTER),
                profile_data(world.env, **{**CPU_ADAPTER, "name": "cpu-alt", **alt_over}),
            ],
        }
    )


def _adapter_spec(world, alternates):
    from salad_campaign_fixture import salad_spec_v2

    item = request()
    item["provider_config"]["adapt"]["alternate_profiles"] = alternates
    return parse_spec(
        salad_spec_v2(world.env, [adapter_stage(world.env, alternate_providers=[item])])
    )


def test_an_adapter_request_with_alternates_expands_to_one_workload(tmp_path, near):  # noqa: F811
    world = SaladWorld(tmp_path, **CPU_ADAPTER)
    near(world, 0)
    dear = dict(vcpu_hour_usd="0.12", memory_gb_hour_usd="0.03")
    profiles = _adapter_profiles(world, cpu_rate=dear)
    expanded = expand_spec(_adapter_spec(world, ["cpu-alt"]), profiles, tenant=None)
    (item,) = expanded.stages[0].alternate_providers
    config = SaladStageSpec.model_validate(item.provider_config)
    assert config.profile == "cpu-main" and config.alternate_profiles == ["cpu-alt"]
    # 600 s at $0.30/h is 5 cents: the candidate's hold covers its costliest listed profile.
    assert item.max_exposure_cents == 5
    # The alternate derives exactly the same workload, so substitution is exact.
    placed = normalize(expanded).stages[0].alternates[0]
    assert placed.provider_config["alternate_profiles"] == ["cpu-alt"]
    # Without alternates the expansion is the pre-ADR-0151 bytes.
    single = expand_spec(_adapter_spec(world, None), profiles, tenant=None)
    plain = SaladStageSpec.model_validate(single.stages[0].alternate_providers[0].provider_config)
    assert plain.alternate_profiles is None
    assert single.stages[0].alternate_providers[0].max_exposure_cents == 2


def test_an_adapter_alternate_with_another_workload_or_unknown_is_refused(tmp_path, near):  # noqa: F811
    world = SaladWorld(tmp_path, **CPU_ADAPTER)
    near(world, 0)
    other = _adapter_profiles(world, custody_host="other-custody.example-tailnet.ts.net")
    with pytest.raises(Conflict, match="alternate profile 'cpu-alt' derives a different"):
        expand_spec(_adapter_spec(world, ["cpu-alt"]), other, tenant=None)
    with pytest.raises(Conflict, match="unknown alternate profile 'cpu-nope'"):
        expand_spec(_adapter_spec(world, ["cpu-nope"]), _adapter_profiles(world), tenant=None)
    # A profile of another project is not visible.
    foreign = _adapter_profiles(world, project_id="another-project")
    tenant = (world.env.owner["org_id"], world.env.project["id"])
    with pytest.raises(Conflict, match="unknown alternate profile 'cpu-alt'"):
        expand_spec(_adapter_spec(world, ["cpu-alt"]), foreign, tenant=tenant)


# -- approval: review pre-approval covers every listed profile, and only those ------------------


class AltWorld(SaladWorld):
    def __init__(self, tmp_path, names=("gpu-alt",)):
        super().__init__(
            tmp_path,
            stages=lambda env: [_with_alternates(env, names)],
            preapprove_reviews=True,
        )
        # A shorter route window gives the alternate its own plan digest.
        _multi(
            self,
            _alt("gpu-alt", route_max_seconds=1600, preapprove_reviews=True),
            _alt("gpu-third", route_max_seconds=1700, preapprove_reviews=True),
        )

    def submitted(self) -> dict:
        self.sub = self.env.submit(spec=self.spec)
        return self.sub

    def preview(self) -> dict:
        campaign = dict(self.env.query("SELECT * FROM campaigns WHERE id=?", self.sub["id"])[0])
        spec = normalize(parse_spec_json(campaign["spec_json"]))
        return {
            **self.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), "g1"),
            **pre.alternate_profile_digests(self.driver, campaign, spec, spec.stage("g1")),
            "max_attempt_cents": 50,
            "max_total_cents": 50,
        }

    def approve_with(self, entry):
        body = {
            "expected_revision": self.sub["revision"],
            "expected_spec_digest": self.sub["spec_digest"],
            "preapprovals": [entry],
        }
        return self.env.store.approve_campaign(
            self.human, self.sub["id"], self.key("approve"), canonical_digest(body), body
        )


def test_the_preview_names_each_alternate_and_one_entry_covers_them_all(tmp_path):
    world = AltWorld(tmp_path)
    world.submitted()
    entry = world.preview()
    assert [p["profile"] for p in entry["alternate_profiles"]] == ["gpu-alt"]
    alt_digest = entry["alternate_profiles"][0]["plan_digest"]
    assert alt_digest != entry["plan_digest"]  # the plan binds the profile's compute window
    StagePreapproval.model_validate(entry)
    status, result = world.approve_with(entry)
    assert status == 200
    world.campaign = result
    (declared,) = world.events("campaign.approved")[0]["preapprovals"]
    assert declared["profiles"] == [
        {"profile": "gpu-main", "plan_digest": entry["plan_digest"]},
        {"profile": "gpu-alt", "plan_digest": alt_digest},
    ]
    # The stage's own profile still resolves every review without a human.
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")
    assert world.events("campaign.preapproval_declined") == []
    uses = world.env.query("SELECT kind FROM campaign_review_preapproval_uses")
    assert sorted(row["kind"] for row in uses) == ["plan", "source_assessment", "source_use"]
    # An attempt recorded on the alternate is compared with the alternate's own digest.
    attempt_id = world.row("SELECT id FROM campaign_stage_attempts")["id"]
    world.env.execute("UPDATE campaign_salad_stages SET profile='gpu-alt'")
    with world.env.store.runner_connection() as connection:
        row = dict(world.row("SELECT * FROM campaign_review_preapprovals"))
        campaign = dict(world.row("SELECT * FROM campaigns"))
        bound = pre._profile_plan_digest(connection, campaign, row, "g1", "salad", attempt_id)
    assert bound == alt_digest


def test_an_entry_that_leaves_an_alternate_uncovered_is_refused_naming_it(tmp_path):
    world = AltWorld(tmp_path, names=("gpu-alt", "gpu-third"))
    world.submitted()
    entry = world.preview()
    entry["alternate_profiles"] = entry["alternate_profiles"][:1]
    with pytest.raises(Conflict, match="does not cover its alternate profile 'gpu-third'"):
        world.approve_with(entry)
    entry.pop("alternate_profiles")
    with pytest.raises(Conflict, match="does not cover its alternate profile 'gpu-alt'"):
        world.approve_with(entry)
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.row("SELECT 1 FROM campaign_approvals") is None


def test_a_profile_not_in_the_approved_spec_can_never_be_bound(tmp_path):
    world = AltWorld(tmp_path)
    world.submitted()
    entry = world.preview()
    # gpu-third is a registered operator profile, but the spec does not list it for the stage.
    rogue = {"profile": "gpu-third", "plan_digest": "3" * 64}
    with pytest.raises(Conflict, match="names profile 'gpu-third', which the approved spec"):
        world.approve_with({**entry, "alternate_profiles": [*entry["alternate_profiles"], rogue]})
    own = {"profile": "gpu-main", "plan_digest": entry["plan_digest"]}
    with pytest.raises(Conflict, match="is the stage's own profile"):
        world.approve_with({**entry, "alternate_profiles": [*entry["alternate_profiles"], own]})
    with pytest.raises(ValidationError):
        StagePreapproval.model_validate(
            {**entry, "alternate_profiles": [entry["alternate_profiles"][0]] * 2}
        )
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None


def test_an_attempt_on_an_uncovered_profile_waits_for_a_person(tmp_path):
    world = AltWorld(tmp_path)
    world.submitted()
    status, result = world.approve_with(world.preview())
    assert status == 200
    world.campaign = result
    world.tick_until_phase("awaiting_lease")
    # The execution plane recorded a profile the approval does not cover.
    world.env.execute("UPDATE campaign_salad_stages SET profile='gpu-third'")
    world.lease_and_package()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    declined = world.events("campaign.preapproval_declined")
    assert {d["kind"] for d in declined if d["reason"] == "wrong_profile"} == {
        "plan",
        "source_use",
    }
    salad = world.salad()
    plan = world.row("SELECT status FROM decision_requests WHERE id=?", salad["plan_decision_id"])
    assert plan["status"] == "pending"


def test_a_single_profile_stage_keeps_the_pre_adr_0151_entry(tmp_path):
    world = SaladWorld(
        tmp_path,
        stages=lambda env: [salad_stage(env, max_duration_seconds=600)],
        preapprove_reviews=True,
    )
    submitted = world.env.submit(spec=world.spec)
    campaign = dict(world.env.query("SELECT * FROM campaigns WHERE id=?", submitted["id"])[0])
    spec = normalize(parse_spec_json(campaign["spec_json"]))
    assert pre.alternate_profile_digests(world.driver, campaign, spec, spec.stage("g1")) == {}
    entry = {
        **world.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), "g1"),
        "max_attempt_cents": 50,
        "max_total_cents": 50,
    }
    with pytest.raises(Conflict, match="names profile 'gpu-alt', which the approved spec"):
        body = {
            "expected_revision": submitted["revision"],
            "expected_spec_digest": submitted["spec_digest"],
            "preapprovals": [
                {**entry, "alternate_profiles": [{"profile": "gpu-alt", "plan_digest": "4" * 64}]}
            ],
        }
        world.env.store.approve_campaign(
            world.human, submitted["id"], "k-alt", canonical_digest(body), body
        )
    body = {
        "expected_revision": submitted["revision"],
        "expected_spec_digest": submitted["spec_digest"],
        "preapprovals": [entry],
    }
    status, _ = world.env.store.approve_campaign(
        world.human, submitted["id"], "k-own", canonical_digest(body), body
    )
    assert status == 200
    (declared,) = world.events("campaign.approved")[0]["preapprovals"]
    assert "profiles" not in declared
