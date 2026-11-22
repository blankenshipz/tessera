"""Infrastructure retries of a campaign stage (ADR 0058). Fakes only; no provider calls."""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_models import CampaignSpec, RetryPolicy, spec_digest
from tessera_control.store import StoreError

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
OK = {"data.json": json.dumps({"score": 3})}
KILLED = 137


def _world(tmp_path, **retry) -> tuple[World, dict]:
    world = World(tmp_path)
    spec = world.env.spec()
    # Infrastructure retries are off unless the spec enables them; these tests enable two.
    extra = {"retry": {"max_attempts": 1, "infra_retries": 2, **retry}}
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **extra)]
    return world, world.campaign(spec)


def _classes(world, cid):
    return [(a["status"], a["failure_class"]) for a in world.attempts(cid)]


def test_a_killed_container_is_retried_by_the_runner_and_the_retry_recorded(tmp_path):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert _classes(world, cid) == [
        ("failed_transient", "infra_container_killed"),
        ("succeeded", None),
    ]
    one, two = world.attempts(cid)
    assert "exit_code:137" in one["failure_detail"] and "retry 1 of 2" in one["failure_detail"]
    assert one["grant_id"] != two["grant_id"]
    # Each attempt has its own hold and settlement, so the campaign budget was charged per
    # attempt and holds nothing now.
    assert one["settled_cents"] == 0 and two["settled_cents"] == 0
    ledger = world.row("SELECT held_cents FROM campaigns WHERE id=?", cid)
    assert ledger["held_cents"] == 0
    (event,) = world.events_of("campaign.stage_infra_retry")
    assert event["payload"]["reason"] == "infra_container_killed"
    assert event["payload"]["infra_retries_used"] == 1
    assert event["payload"]["infra_retries_max"] == 2 and event["payload"]["rerouted"] is False
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    entries = {a["attempt"]: a for stage in receipt["stages"] for a in stage["attempts"]}
    assert entries[1]["failure_class"] == "infra_container_killed"
    assert "retry 1 of 2" in entries[1]["infrastructure_retry"]["reason"]
    assert entries[1]["run_receipt_digest"] and entries[2]["run_receipt_digest"]  # own receipts


def test_the_infrastructure_bound_is_enforced_and_recorded(tmp_path):
    world, campaign = _world(tmp_path, infra_retries=1)
    cid = campaign["id"]
    world.scenarios = [(KILLED, OK), (KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [
        ("failed_transient", "infra_container_killed"),
        ("failed_permanent", "undeclared_exit_code"),
    ]
    assert world.events_of("campaign.stage_infra_retries_exhausted")
    assert len(world.worker_runs) == 2  # never a third run


def test_a_task_failure_is_never_an_infrastructure_retry(tmp_path):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    world.scenarios = [(1, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [("failed_permanent", "undeclared_exit_code")]
    assert not world.events_of("campaign.stage_infra_retry")
    assert len(world.worker_runs) == 1


def test_infra_retries_zero_turns_the_loop_off(tmp_path):
    world, campaign = _world(tmp_path, infra_retries=0)
    cid = campaign["id"]
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [("failed_permanent", "undeclared_exit_code")]


def test_an_infrastructure_retry_does_not_use_a_scientific_retry(tmp_path):
    world, campaign = _world(tmp_path, max_attempts=2, transient_exit_codes=[7], infra_retries=1)
    cid = campaign["id"]
    # infra kill, then a declared-transient failure, then success: the kill used no
    # scientific attempt, so the transient failure still had one retry left.
    world.scenarios = [(KILLED, OK), (7, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert [c for _, c in _classes(world, cid)] == [
        "infra_container_killed",
        "t2_declared_exit_code",
        None,
    ]


def test_a_declared_transient_code_wins_over_an_infrastructure_code(tmp_path):
    world, campaign = _world(tmp_path, max_attempts=2, transient_exit_codes=[137], infra_retries=2)
    cid = campaign["id"]
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert _classes(world, cid)[0] == ("failed_transient", "t2_declared_exit_code")
    assert not world.events_of("campaign.stage_infra_retry")


def test_a_stopped_campaign_does_not_retry(tmp_path):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_until(lambda: len(world.worker_runs) == 1)
    world.cancel(campaign)
    world.tick_all(cid)
    assert world.status(cid) == "cancelled", dump(world, cid)
    assert len(world.worker_runs) == 1  # the cancel ended every retry


def test_each_infrastructure_retry_is_charged_against_the_budget_cap(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec(budget_cap_cents=7)
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, max_exposure_cents=6)]
    cid = world.campaign(spec)["id"]
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    # The first attempt settles at zero (local compute), so the cap holds one hold at a
    # time; a cap below one hold refuses the retry instead of exceeding it.
    assert world.status(cid) in {"succeeded", "failed", "stopped"}, dump(world, cid)
    cap = world.row("SELECT budget_cap_cents, held_cents FROM campaigns WHERE id=?", cid)
    assert cap["held_cents"] == 0
    refused = [a for a in world.attempts(cid) if a["status"] == "refused_budget"]
    assert all(a["failure_class"] in {"budget_refused", "exposure_unknown"} for a in refused)


# -- the spec ------------------------------------------------------------------------------


def test_a_spec_without_the_new_fields_keeps_its_digest():
    policy = RetryPolicy()
    assert policy.infra_retries == 0 and policy.infra_exit_codes == [137]
    dumped = policy.model_dump(mode="json")
    assert dumped == {"max_attempts": 1, "transient_exit_codes": []}
    explicit = RetryPolicy(infra_retries=1, reroute_providers=["self-hosted"])
    assert explicit.model_dump(mode="json")["infra_retries"] == 1
    assert explicit.model_dump(mode="json")["reroute_providers"] == ["self-hosted"]
    assert spec_digest is not None and CampaignSpec is not None


@pytest.mark.parametrize(
    "bad",
    [
        {"infra_retries": 4},
        {"infra_retries": -1},
        {"infra_exit_codes": []},
        {"infra_exit_codes": [0]},
    ],
)
def test_the_infrastructure_policy_is_bounded(bad):
    with pytest.raises(ValueError):
        RetryPolicy(**bad)


# -- rerouting to another registered provider --------------------------------------------------


@pytest.fixture
def lab_box(tmp_path):
    """A second registered worker-grant provider for the same worker principal."""
    from tessera_control.campaign_provider_worker import register_from_config
    from tessera_control.campaign_providers import unregister_provider

    holder: dict = {}

    def make(world):
        register_from_config(
            {"self_hosted": [{"id": "lab-box", "worker_principal_id": world.env.worker["id"]}]}
        )
        holder["on"] = True

    yield make
    if holder:
        unregister_provider("lab-box")


def test_an_infrastructure_retry_may_move_to_a_registered_named_provider(tmp_path, lab_box):
    world = World(tmp_path)
    lab_box(world)  # public inputs are admitted to any registered provider
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(
            world.env,
            "s1",
            metrics=METRIC,
            retry={"max_attempts": 1, "infra_retries": 2, "reroute_providers": ["lab-box"]},
        )
    ]
    cid = world.campaign(spec)["id"]
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    first, second = world.attempts(cid)
    assert (first["provider_id"], second["provider_id"]) == ("local-docker", "lab-box")
    assert first["failure_class"] == "infra_container_killed"
    (event,) = world.events_of("campaign.stage_infra_retry")
    assert event["payload"]["provider"] == "lab-box" and event["payload"]["rerouted"] is True
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    entries = {a["attempt"]: a for stage in receipt["stages"] for a in stage["attempts"]}
    assert (entries[1]["provider_id"], entries[2]["provider_id"]) == ("local-docker", "lab-box")


def test_a_reroute_must_be_registered_and_trusted_at_submit(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(
            world.env, "s1", metrics=METRIC, retry={"reroute_providers": ["nowhere-registered"]}
        )
    ]
    from tessera_control.store import Conflict

    with pytest.raises(Conflict, match="not registered"):
        world.env.submit(spec=spec)


def test_a_reroute_naming_the_stages_own_provider_is_refused(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "s1", metrics=METRIC, retry={"reroute_providers": ["local-docker"]})
    ]
    with pytest.raises(StoreError, match="reroute_providers"):
        world.env.submit(spec=spec)


# -- status ------------------------------------------------------------------------------------


def test_the_retry_state_is_readable_and_stopping_is_cancelling(tmp_path):
    from tessera_control.agent_campaign_reads import AgentCampaignReads

    world, campaign = _world(tmp_path, infra_retries=2)
    cid = campaign["id"]
    world.scenarios = [(KILLED, OK), (KILLED, OK), (0, OK)]
    world.tick_until(
        lambda: any(a["failure_class"] == "infra_container_killed" for a in world.attempts(cid))
    )
    reads = AgentCampaignReads(world.env.state)
    view = reads.campaign_retries(world.env.human(), cid)
    (stage,) = view["stages"]
    assert stage["infra_retries_max"] == 2 and stage["infra_retries_used"] >= 1
    assert stage["infra_retries_remaining"] == 2 - stage["infra_retries_used"]
    assert stage["infra_exit_codes"] == [137] and stage["reroute_providers"] == []
    assert stage["infra_failures"][0]["failure_class"] == "infra_container_killed"
    assert "tessera_cancel_campaign" in view["stop"]
    world.cancel(campaign)
    # The retry's grant was never leased, so (as for any cancel before a lease) it is
    # reconciled once it expires.
    world.env.execute("UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z'")
    world.tick_all(cid, worker=False)
    assert world.status(cid) == "cancelled", dump(world, cid)
    final = reads.campaign_retries(world.env.human(), cid)
    assert final["status"] == "cancelled"


# -- review fixes: off by default, counted, confirmed-exit path, reservation-time checks -----


def test_infrastructure_retries_are_off_unless_the_spec_enables_them(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]  # no retry field at all
    cid = world.campaign(spec)["id"]
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [("failed_permanent", "undeclared_exit_code")]
    assert len(world.worker_runs) == 1
    assert not world.events_of("campaign.stage_infra_retry")
    # Off is not "exhausted": nothing was enabled, so nothing is reported spent.
    assert not world.events_of("campaign.stage_infra_retries_exhausted")


def test_enabling_infrastructure_retries_changes_the_approved_digest(tmp_path):
    world = World(tmp_path)

    def digest(**retry):
        spec = world.env.spec()
        extra = {"retry": retry} if retry else {}
        spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **extra)]
        return spec_digest(CampaignSpec.model_validate(spec))

    assert digest() == digest(max_attempts=1) == digest(infra_retries=0)
    assert digest(infra_retries=2) != digest()
    assert digest(infra_retries=2) != digest(infra_retries=1)


def test_the_worst_case_exposure_counts_enabled_infrastructure_retries(tmp_path):
    world = World(tmp_path)

    def spec(exposure, **retry):
        body = world.env.spec(budget_cap_cents=5000)
        body["stages"] = [
            stage_spec(world.env, "s1", metrics=METRIC, max_exposure_cents=exposure, retry=retry)
        ]
        return body

    CampaignSpec.model_validate(spec(1000, max_attempts=1))  # 1 x 1000
    CampaignSpec.model_validate(spec(1250, max_attempts=1, infra_retries=3))  # 4 x 1250 = 5000
    with pytest.raises(ValueError, match=r"infra_retries"):
        CampaignSpec.model_validate(spec(2000, max_attempts=1, infra_retries=3))  # 8000
    CampaignSpec.model_validate(spec(5000, max_attempts=1))  # unchanged for existing specs


def test_the_stage_experiment_budget_ceiling_covers_every_attempt_it_may_make(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec(budget_cap_cents=5000)
    spec["stages"] = [
        stage_spec(
            world.env,
            "s1",
            metrics=METRIC,
            max_exposure_cents=1000,
            retry={"max_attempts": 1, "infra_retries": 3},
        )
    ]
    cid = world.campaign(spec)["id"]
    world.tick_until(lambda: world.env.query("SELECT 1 FROM experiment_budgets"), worker=False)
    ceiling = world.row("SELECT ceiling_cents FROM experiment_budgets")["ceiling_cents"]
    assert ceiling == 1000 * (1 + 3)
    assert cid


def test_a_rerouted_attempt_drops_the_stage_provider_settings(tmp_path):
    import dataclasses

    from tessera_control.campaign_models import normalize
    from tessera_control.campaign_runner import CampaignRunner

    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    stage = normalize(CampaignSpec.model_validate(spec)).stage("s1")
    configured = dataclasses.replace(
        stage, provider_config={"region": "elsewhere"}, worker_principal_id="worker_original"
    )
    moved = CampaignRunner._on_provider(configured, "lab-box")
    assert moved.provider == "lab-box"
    assert moved.provider_config == {} and moved.worker_principal_id is None
    assert CampaignRunner._on_provider(configured, configured.provider) is configured


def test_a_killed_exit_resolved_on_the_confirmed_exit_is_retried_too(tmp_path):
    """The ADR 0053 path (a report that never arrived, the exit recorded by control) goes
    through the same infrastructure classification as the normal observation path."""
    from test_campaign_exited_resolve import _admit, _drop, _insert_run_receipt

    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    world.scenarios = [(KILLED, OK), (0, OK)]
    _admit(world, cid)
    original = _drop(world, "run-receipt", "manifest", "artifact")
    world.poller.run(max_cycles=1)
    world.channel.post = original
    (attempt,) = world.attempts(cid)
    # Control has the confirmed exit; the receipt control holds says 137.
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='unknown' WHERE id=?", attempt["id"]
    )
    _insert_run_receipt(world, attempt, KILLED)
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    first, second = world.attempts(cid)
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        "infra_container_killed",
    )
    assert "resolved on the worker's confirmed exit" in first["failure_detail"]
    assert "retry 1 of 2" in first["failure_detail"]
    assert second["status"] == "succeeded"
    (event,) = world.events_of("campaign.stage_infra_retry")
    assert event["payload"]["reason"] == "infra_container_killed"


def test_a_killed_exit_resolved_on_the_confirmed_exit_respects_the_bound(tmp_path):
    from test_campaign_exited_resolve import _admit, _drop, _insert_run_receipt

    world, campaign = _world(tmp_path, infra_retries=0)
    cid = campaign["id"]
    _admit(world, cid)
    _drop(world, "run-receipt", "manifest", "artifact")
    world.poller.run(max_cycles=1)
    (attempt,) = world.attempts(cid)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='unknown' WHERE id=?", attempt["id"]
    )
    _insert_run_receipt(world, attempt, KILLED)
    world.tick_all(cid)
    assert _classes(world, cid) == [("failed_permanent", "undeclared_exit_code")]


def test_the_reroute_checks_are_repeated_when_the_retry_is_reserved(tmp_path, lab_box, monkeypatch):
    """A provider that passed at approval but no longer passes its own stage check is not
    used: the retry falls back to the stage's own provider."""
    from tessera_control.campaign_provider_worker import WorkerGrantProvider
    from tessera_control.store import Conflict

    world = World(tmp_path)
    lab_box(world)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(
            world.env,
            "s1",
            metrics=METRIC,
            retry={"max_attempts": 1, "infra_retries": 2, "reroute_providers": ["lab-box"]},
        )
    ]
    cid = world.campaign(spec)["id"]  # approved while lab-box passes every check
    original = WorkerGrantProvider.check_stage

    def refuse_lab_box(self, connection, project_id, stage):
        if stage.provider == "lab-box":
            raise Conflict("synthetic: the provider no longer passes its own check")
        return original(self, connection, project_id, stage)

    monkeypatch.setattr(WorkerGrantProvider, "check_stage", refuse_lab_box)
    world.scenarios = [(KILLED, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    first, second = world.attempts(cid)
    assert (first["provider_id"], second["provider_id"]) == ("local-docker", "local-docker")
    (event,) = world.events_of("campaign.stage_infra_retry")
    assert event["payload"]["provider"] == "local-docker" and event["payload"]["rerouted"] is False


# -- what an approver sees ---------------------------------------------------------------------


def test_the_approval_time_view_states_the_infrastructure_retry_policy(tmp_path, lab_box):
    world = World(tmp_path)
    lab_box(world)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "quiet", metrics=METRIC),  # no retry field: off
        stage_spec(
            world.env,
            "loud",
            metrics=METRIC,
            retry={"max_attempts": 2, "infra_retries": 3, "reroute_providers": ["lab-box"]},
        ),
    ]
    submitted = world.env.submit(spec=spec)
    expected = {
        "quiet": {
            "stage_key": "quiet",
            "infra_retries": 0,
            "infra_exit_codes": [137],
            "reroute_providers": [],
            "worst_case_attempts": 1,
        },
        "loud": {
            "stage_key": "loud",
            "infra_retries": 3,
            "infra_exit_codes": [137],
            "reroute_providers": ["lab-box"],
            "worst_case_attempts": 5,
        },
    }
    # The submit response (what the approver is shown) and every later read say so, even
    # for a stage whose stored spec omits the defaults.
    assert {p["stage_key"]: p for p in submitted["infra_retry_policy"]} == expected
    assert "infra_retries" not in json.dumps(submitted["spec"]["stages"][0]["retry"])
    read = world.env.store.get_campaign(world.env.human(), submitted["id"])
    assert {p["stage_key"]: p for p in read["infra_retry_policy"]} == expected
