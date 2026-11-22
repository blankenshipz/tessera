"""A stage that stops on a resource limit is retried as an infrastructure failure (ADR 0067).

Fakes only; no provider calls. The limit is reported by a stage metric the spec declares
(``retry.resource_limit_metrics``); a kill (exit 137) takes the same path (ADR 0058).
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_infra_retry import resource_limit_stop
from tessera_control.campaign_models import CampaignSpec, RetryPolicy
from tessera_control.campaign_provider_worker import WorkerGrantProvider, register_from_config
from tessera_control.campaign_providers import unregister_provider
from tessera_control.store import StoreError

MIB = 1024 * 1024
METRICS = [
    {"name": "score", "path": "data.json", "json_pointer": "/score"},
    {"name": "stopped", "path": "data.json", "json_pointer": "/stopped"},
]


def _out(score=1, stopped=0):
    return {"data.json": json.dumps({"score": score, "stopped": stopped})}


@pytest.fixture(autouse=True)
def _lab_box():
    yield
    unregister_provider("lab-box")


def _world(tmp_path, reroute=False, **retry):
    """One stage on local-docker; ``reroute`` names a second approved provider it may move to."""
    world = World(tmp_path)
    if reroute:
        register_from_config(
            {"self_hosted": [{"id": "lab-box", "worker_principal_id": world.env.worker["id"]}]}
        )
        retry["reroute_providers"] = ["lab-box"]
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(
            world.env,
            "s1",
            metrics=METRICS,
            retry={"max_attempts": 1, "resource_limit_metrics": ["stopped"], **retry},
        )
    ]
    return world, world.campaign(spec)["id"]


def _classes(world, cid):
    return [(a["status"], a["failure_class"]) for a in world.attempts(cid)]


# -- classification ----------------------------------------------------------------------------


def test_a_time_budget_stop_is_retried_and_the_reason_is_on_the_receipt(tmp_path):
    world, cid = _world(tmp_path, reroute=True, infra_retries=2)
    world.scenarios = [(0, _out(1, 1)), (0, _out(2, 0))]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert _classes(world, cid) == [
        ("failed_transient", "infra_resource_limit"),
        ("succeeded", None),
    ]
    first = world.attempts(cid)[0]
    assert "stopped=1" in first["failure_detail"] and "retry 1 of 2" in first["failure_detail"]
    (event,) = world.events_of("campaign.stage_infra_retry")
    assert event["payload"]["reason"] == "infra_resource_limit"
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    entries = {a["attempt"]: a for stage in receipt["stages"] for a in stage["attempts"]}
    assert entries[1]["failure_class"] == "infra_resource_limit"
    assert "stopped=1" in entries[1]["infrastructure_retry"]["reason"]
    assert entries[2]["failure_class"] is None


def test_a_zero_or_unlisted_metric_is_not_a_stop(tmp_path):
    world, cid = _world(tmp_path, infra_retries=2)
    world.scenarios = [(0, _out(1, 0))]
    world.tick_all(cid)
    assert _classes(world, cid) == [("succeeded", None)]
    assert not world.events_of("campaign.stage_infra_retry")


def test_a_stop_without_infrastructure_retries_enabled_is_left_as_it_was(tmp_path):
    world, cid = _world(tmp_path)  # infra_retries is off by default (ADR 0058)
    world.scenarios = [(0, _out(1, 1))]
    world.tick_all(cid)
    assert _classes(world, cid) == [("succeeded", None)]
    assert not world.events_of("campaign.stage_infra_retries_exhausted")


def test_the_bound_holds_and_the_exhaustion_names_the_metric(tmp_path):
    world, cid = _world(tmp_path, reroute=True, infra_retries=1)
    world.scenarios = [(0, _out(1, 1)), (0, _out(2, 1)), (0, _out(3, 0))]
    world.tick_all(cid)
    assert _classes(world, cid) == [
        ("failed_transient", "infra_resource_limit"),
        ("succeeded", None),  # the bound is spent: the stage is left as before, never looped
    ]
    assert len(world.worker_runs) == 2
    (event,) = world.events_of("campaign.stage_infra_retries_exhausted")
    assert event["payload"]["resource_limit_metric"] == "stopped"


def test_a_killed_container_still_takes_the_same_path(tmp_path):
    world, cid = _world(tmp_path, infra_retries=1)
    world.scenarios = [(137, _out(1, 0)), (0, _out(2, 0))]
    world.tick_all(cid)
    assert [c for _, c in _classes(world, cid)] == ["infra_container_killed", None]


def test_the_metric_reader_counts_only_a_recorded_number_above_zero():
    rows = json.dumps(
        [
            {"name": "a", "value": 0},
            {"name": "b", "value": None},
            {"name": "c", "value": True},
            {"name": "d", "value": "3"},
            {"name": "e", "value": 2.5},
        ]
    )
    assert resource_limit_stop(["a", "b", "c", "d"], rows) is None
    assert resource_limit_stop(["a", "e"], rows) == ("e", 2.5)
    assert resource_limit_stop([], rows) is None and resource_limit_stop(["e"], None) is None


# -- the spec ---------------------------------------------------------------------------------


def test_the_field_is_part_of_the_digest_only_when_set():
    assert RetryPolicy().model_dump(mode="json") == {"max_attempts": 1, "transient_exit_codes": []}
    assert RetryPolicy(resource_limit_metrics=["stopped"]).model_dump(mode="json")[
        "resource_limit_metrics"
    ] == ["stopped"]
    with pytest.raises(ValueError):
        RetryPolicy(resource_limit_metrics=["a", "b", "c", "d", "e"])


def test_a_declared_metric_must_be_one_of_the_stages(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(
            world.env,
            "s1",
            metrics=METRICS,
            retry={"max_attempts": 1, "resource_limit_metrics": ["nowhere"]},
        )
    ]
    with pytest.raises(ValueError, match="resource_limit_metrics"):
        CampaignSpec.model_validate(spec)
    with pytest.raises(StoreError, match="resource_limit_metrics"):
        world.env.submit(spec=spec)


# -- where a stage with no other place retries ---------------------------------------------------


def _report(monkeypatch, value):
    monkeypatch.setattr(WorkerGrantProvider, "memory_report", lambda self, rt, ctx, stage: value)


@pytest.mark.parametrize(
    "report",
    [(10 * MIB, 256 * MIB), (8192 * MIB, 256 * MIB), None],
    ids=["too_little", "plenty", "no_report"],
)
def test_a_time_budget_stop_is_never_retried_in_place_whatever_the_free_memory(
    tmp_path, monkeypatch, report
):
    """Free memory does not make a stage fit its time; the spec has no raised budget."""
    world, cid = _world(tmp_path, infra_retries=2)
    _report(monkeypatch, report)
    world.scenarios = [(0, _out(1, 1)), (0, _out(2, 0))]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [("failed_transient", "infra_resource_limit")]
    assert len(world.worker_runs) == 1  # never repeated where it hit the limit
    (event,) = world.events_of("campaign.stage_resource_limit_no_provider")
    assert event["payload"]["reason"] == "infra_resource_limit"
    assert event["payload"]["blocked_by"] == "time_budget_not_raisable"
    assert (
        "resource_limit_no_provider"
        in (world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"])
    )


def test_a_single_provider_stage_killed_with_137_retries_in_place_as_adr_0058_did(
    tmp_path, monkeypatch
):
    world, cid = _world(tmp_path, infra_retries=2)
    _report(monkeypatch, None)  # a worker that reports no memory does not change this
    world.scenarios = [(137, _out(1, 0)), (0, _out(2, 0))]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert [c for _, c in _classes(world, cid)] == ["infra_container_killed", None]
    first, second = world.attempts(cid)
    assert first["provider_id"] == second["provider_id"]
    # Still bounded by infra_retries: a second kill with one retry allowed ends the stage.
    (tmp_path / "b").mkdir()
    again, cid2 = _world(tmp_path / "b", infra_retries=1)
    again.scenarios = [(137, _out(1, 0)), (137, _out(1, 0)), (0, _out(2, 0))]
    again.tick_all(cid2)
    assert len(again.attempts(cid2)) == 2 and again.status(cid2) == "failed"


# -- the next candidate -------------------------------------------------------------------------


def test_a_retry_goes_to_the_next_placement_candidate_not_back_to_the_provider_that_hit_the_limit(
    tmp_path, monkeypatch
):
    from test_campaign_models_v2 import _spec2
    from test_campaign_placement import (
        FAR,
        NEAR,
        _placed,
        _stage,
        providers,  # noqa: F401
    )
    from test_campaign_placement import _world as placement_world

    registered: list[str] = []
    from tessera_control import campaign_provider_worker as worker
    from tessera_control.campaign_providers import register_provider, unregister_provider

    def make(provider_id, principal, slots=1):
        register_provider(
            WorkerGrantProvider(worker._descriptor(provider_id, principal, None, slots))
        )
        registered.append(provider_id)

    try:
        world = placement_world(tmp_path, make)
        original = WorkerGrantProvider.memory_report

        def report(self, rt, ctx, stage):
            if stage.provider == NEAR:  # near-cpu shows too little free memory
                return (10 * MIB, 256 * MIB)
            return original(self, rt, ctx, stage)

        monkeypatch.setattr(WorkerGrantProvider, "memory_report", report)
        env = world.env
        world.scenarios = [(0, _out(1, 1)), (0, _out(2, 0))]
        stage = _stage(
            env,
            "a",
            metrics=METRICS,
            retry={"max_attempts": 1, "infra_retries": 1, "resource_limit_metrics": ["stopped"]},
        )
        cid = world.campaign(_spec2(stage))["id"]
        world.tick_all(cid)
        assert world.status(cid) == "succeeded", dump(world, cid)
        attempts = world.attempts(cid)
        assert [(a["provider_id"], a["failure_class"]) for a in attempts] == [
            (NEAR, "infra_resource_limit"),
            (FAR, None),
        ]
        placed = {p["attempt"]: p for p in _placed(world, cid)}
        assert placed[2]["provider_id"] == FAR
        # The candidate that hit the limit is ordered after the untried one (ADR 0060 5a), so it
        # is not even evaluated while far-cpu can take the retry.
        assert placed[2]["not_evaluated"] == [NEAR]
    finally:
        for provider_id in registered:
            unregister_provider(provider_id)


def test_a_reroute_provider_takes_the_retry_after_a_stop(tmp_path):
    from tessera_control.campaign_provider_worker import register_from_config
    from tessera_control.campaign_providers import unregister_provider

    world = World(tmp_path)
    register_from_config(
        {"self_hosted": [{"id": "lab-box", "worker_principal_id": world.env.worker["id"]}]}
    )
    try:
        spec = world.env.spec()
        spec["stages"] = [
            stage_spec(
                world.env,
                "s1",
                metrics=METRICS,
                retry={
                    "max_attempts": 1,
                    "infra_retries": 1,
                    "resource_limit_metrics": ["stopped"],
                    "reroute_providers": ["lab-box"],
                },
            )
        ]
        cid = world.campaign(spec)["id"]
        world.scenarios = [(0, _out(1, 1)), (0, _out(2, 0))]
        world.tick_all(cid)
        assert world.status(cid) == "succeeded", dump(world, cid)
        first, second = world.attempts(cid)
        assert (first["provider_id"], second["provider_id"]) == ("local-docker", "lab-box")
        assert first["failure_class"] == "infra_resource_limit"
    finally:
        unregister_provider("lab-box")


# -- the memory report -------------------------------------------------------------------------


def test_a_pending_heartbeat_replays_the_body_it_first_sent(tmp_path):
    import sqlite3

    from tessera_worker.service import HeartbeatIntentJournal

    journal = HeartbeatIntentJournal(tmp_path)
    first = journal.intent({"memory_available_bytes": 5 * MIB})
    again = journal.intent({"memory_available_bytes": 9 * MIB})  # a restart: same key, same body
    assert again == first and first[1] == {"memory_available_bytes": 5 * MIB}
    journal.complete()
    assert journal.intent({"memory_available_bytes": 9 * MIB})[1] == {
        "memory_available_bytes": 9 * MIB
    }
    # A journal written before the body existed gains the column in place.
    old = tmp_path / "old"
    old.mkdir()
    with sqlite3.connect(old / "heartbeats.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE heartbeat_intents (sequence INTEGER PRIMARY KEY, key TEXT NOT NULL, "
            "completed INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO heartbeat_intents VALUES (1, 'pending-key', 0)")
    assert HeartbeatIntentJournal(old).intent({"x": 1}) == ("pending-key", {})


def test_the_heartbeat_carries_only_figures_the_host_can_read(tmp_path):
    from tessera_worker import scratch_limits

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 16777216 kB\nMemAvailable:    4194304 kB\n")
    assert scratch_limits.host_memory_available_bytes(meminfo) == 4 * 1024 * MIB
    assert scratch_limits.host_memory_available_bytes(tmp_path / "missing") is None
    meminfo.write_text("MemTotal: 1 kB\n")
    assert scratch_limits.host_memory_available_bytes(meminfo) is None


def test_control_takes_the_memory_figures_in_a_heartbeat_and_bounds_them(tmp_path):
    from tessera_control.worker_models import WorkerHeartbeat

    assert WorkerHeartbeat().model_dump(exclude_none=True) == {}  # an old worker's beat
    beat = WorkerHeartbeat(memory_available_bytes=3 * MIB, container_memory_bytes=256 * MIB)
    assert beat.memory_available_bytes == 3 * MIB
    for bad in ({"memory_available_bytes": -1}, {"container_memory_bytes": 1}, {"x": 1}):
        with pytest.raises(ValueError):
            WorkerHeartbeat(**bad)


def test_a_heartbeat_reaches_the_event_the_provider_reads(tmp_path):
    world, cid = _world(tmp_path, infra_retries=1)
    world.scenarios = [(0, _out(1, 0))]
    world.tick_all(cid)
    beats = [
        e for e in world.events_of("worker.heartbeat") if "container_memory_bytes" in e["payload"]
    ]
    assert beats and beats[-1]["payload"]["container_memory_bytes"] == 256 * MIB
    assert beats[-1]["payload"]["approved_metadata_only"] is True


# -- every candidate skipped ---------------------------------------------------------------------


def test_when_every_placement_candidate_is_skipped_the_stage_ends_on_the_resource_limit(
    tmp_path,
):
    from test_campaign_models_v2 import _spec2
    from test_campaign_placement import (
        FAR,
        NEAR,
        _stage,
    )
    from test_campaign_placement import _world as placement_world

    from tessera_control import campaign_provider_worker as worker
    from tessera_control.campaign_providers import register_provider, unregister_provider

    registered: list[str] = []

    def make(provider_id, principal, slots=1):
        register_provider(
            WorkerGrantProvider(worker._descriptor(provider_id, principal, None, slots))
        )
        registered.append(provider_id)

    try:
        world = placement_world(tmp_path, make)
        world.scenarios = [(0, _out(1, 1)), (0, _out(2, 0))]
        stage = _stage(
            world.env,
            "a",
            metrics=METRICS,
            retry={"max_attempts": 1, "infra_retries": 2, "resource_limit_metrics": ["stopped"]},
        )
        cid = world.campaign(_spec2(stage))["id"]
        unregister_provider(FAR)  # approved, but the only other candidate can no longer take it
        registered.remove(FAR)
        world.tick_all(cid)
        assert world.status(cid) == "failed", dump(world, cid)
        assert [a["provider_id"] for a in world.attempts(cid)] == [NEAR]
        (event,) = world.events_of("campaign.stage_resource_limit_no_provider")
        assert event["payload"]["reason"] == "infra_resource_limit"
        reasons = {s["provider_id"]: s["reason"] for s in event["payload"]["skipped"]}
        assert reasons == {NEAR: "resource_limit_last_attempt", FAR: "unregistered"}
        stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
        assert "resource_limit_no_provider" in stop
        receipt = json.loads(
            world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"]
        )
        assert "resource_limit_no_provider" in receipt["stop_reason"]
    finally:
        for provider_id in registered:
            unregister_provider(provider_id)


# -- an older control plane ----------------------------------------------------------------------


def test_a_heartbeat_an_older_control_plane_rejects_is_sent_again_without_memory(tmp_path):
    from types import SimpleNamespace

    from tessera_worker.control_channel import ControlChannelError
    from tessera_worker.service import DevelopmentWorkerService, HeartbeatIntentJournal

    posts: list[tuple[dict, str]] = []

    class Watch:
        def post(self, path, body, key):
            posts.append((body, key))
            if body:  # an older WorkerHeartbeat forbids extra fields
                raise ControlChannelError("extra fields not permitted", status=422)

    me = SimpleNamespace(
        heartbeats=HeartbeatIntentJournal(tmp_path),
        _watch=Watch(),
        _memory_unsupported=False,
        _memory_report=lambda: {"container_memory_bytes": 256 * MIB},
    )
    DevelopmentWorkerService._heartbeat(me)
    assert [b for b, _ in posts] == [{"container_memory_bytes": 256 * MIB}, {}]
    assert posts[1][1] == posts[0][1] + "-plain"
    assert me._memory_unsupported and me.heartbeats.intent({})[1] == {}  # beat completed
    DevelopmentWorkerService._heartbeat(me)  # later beats carry no memory fields at all
    assert posts[-1][0] == {}


# -- the memory report, read directly --------------------------------------------------------------


def test_the_memory_report_is_the_fresh_heartbeat_and_nothing_else():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from tessera_control import campaign_provider_worker as worker
    from tessera_control.workers import HEARTBEAT_MAX_AGE

    class Provider(WorkerGrantProvider):
        def worker_for(self, stage):
            return "w1"

    now = datetime(2030, 1, 1, tzinfo=UTC)
    rows: list = []

    def one(sql, params):
        assert params[1] == "w1"  # the stage's worker principal, never another's
        return rows[0] if rows else None

    rt = SimpleNamespace(one=one, stamp=lambda: now.isoformat(), parse_utc=datetime.fromisoformat)
    ctx = SimpleNamespace(campaign={"project_id": "p"})
    provider = Provider(worker._descriptor("p1", "w1", None, 1))

    def beat(age, **body):
        rows[:] = [{"payload_json": json.dumps(body), "created_at": (now - age).isoformat()}]

    assert provider.memory_report(rt, ctx, None) is None  # silent: never inferred
    beat(timedelta(0), memory_available_bytes=5, container_memory_bytes=7)
    assert provider.memory_report(rt, ctx, None) == (5, 7)
    beat(HEARTBEAT_MAX_AGE - timedelta(seconds=1), container_memory_bytes=7)
    assert provider.memory_report(rt, ctx, None) == (None, 7)
    beat(
        HEARTBEAT_MAX_AGE + timedelta(seconds=1), memory_available_bytes=5, container_memory_bytes=7
    )
    assert provider.memory_report(rt, ctx, None) is None  # stale
    beat(timedelta(0), approved_metadata_only=True)  # an older worker's beat carries neither
    assert provider.memory_report(rt, ctx, None) is None


# -- a metric means one thing --------------------------------------------------------------------


def test_a_resource_limit_metric_may_not_also_be_a_stop_rule(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(
            world.env,
            "s1",
            metrics=METRICS,
            retry={"max_attempts": 1, "resource_limit_metrics": ["stopped"]},
            stop_when=[{"metric": "stopped", "op": ">", "value": 0}],
        )
    ]
    with pytest.raises(ValueError, match="stop_when"):
        CampaignSpec.model_validate(spec)
    with pytest.raises(StoreError, match="stop_when"):
        world.env.submit(spec=spec)


# -- the same provider again, with room ----------------------------------------------------------


def _killed_twice(tmp_path, monkeypatch, report):
    """A kill on local-docker, then on its reroute provider; the reroute list is then spent."""
    world, cid = _world(tmp_path, reroute=True, infra_retries=2)
    _report(monkeypatch, report)
    world.scenarios = [(137, _out(1, 0)), (137, _out(1, 0)), (0, _out(2, 0))]
    world.tick_all(cid)
    return world, cid


def test_a_kill_returns_to_the_first_provider_when_its_fresh_free_memory_is_above_the_limit(
    tmp_path, monkeypatch
):
    world, cid = _killed_twice(tmp_path, monkeypatch, (8192 * MIB, 256 * MIB))
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert [a["provider_id"] for a in world.attempts(cid)] == [
        "local-docker",
        "lab-box",
        "local-docker",
    ]


@pytest.mark.parametrize(
    "report", [(10 * MIB, 256 * MIB), None], ids=["too_little", "stale_or_silent"]
)
def test_a_stale_or_silent_worker_or_too_little_memory_refuses_the_return(
    tmp_path, monkeypatch, report
):
    world, cid = _killed_twice(tmp_path, monkeypatch, report)
    assert world.status(cid) == "failed", dump(world, cid)
    assert len(world.attempts(cid)) == 2
    (event,) = world.events_of("campaign.stage_resource_limit_no_provider")
    skipped = {s["provider_id"]: s for s in event["payload"]["skipped"]}
    assert set(skipped) == {"local-docker", "lab-box"}
    assert {s["reason"] for s in skipped.values()} == {"resource_limit_last_attempt"}
    assert all("free memory not shown above its limit" in s["detail"] for s in skipped.values())


def test_each_provider_is_judged_against_the_limit_its_own_attempt_ran_with(tmp_path, monkeypatch):
    """Both attempts ran with 256 MiB (their receipts); the worker's own container figure
    (1 GiB here) is only the fallback when no receipt says, so 512 MiB free is room."""
    world, cid = _world(tmp_path, reroute=True, infra_retries=2)
    monkeypatch.setattr(
        WorkerGrantProvider, "memory_report", lambda self, rt, ctx, stage: (512 * MIB, 1024 * MIB)
    )
    world.scenarios = [(137, _out(1, 0)), (137, _out(1, 0)), (0, _out(2, 0))]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert len(world.attempts(cid)) == 3


# -- budget, 137 and lineage ---------------------------------------------------------------------


def test_a_retry_after_a_resource_limit_kill_is_refused_when_the_budget_is_spent(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec(budget_cap_cents=10)
    spec["stages"] = [
        stage_spec(
            world.env,
            "s1",
            metrics=METRICS,
            max_exposure_cents=6,
            retry={"max_attempts": 1, "infra_retries": 2},
        )
    ]
    cid = world.campaign(spec)["id"]
    world.scenarios = [(137, _out(1, 0)), (0, _out(2, 0))]
    world.tick_until(lambda: bool(world.attempts(cid)) and world.attempts(cid)[0]["grant_id"])
    # The cap drops below one hold while attempt 1 runs: its hold stands, a retry cannot.
    world.env.execute("UPDATE campaigns SET budget_cap_cents=5 WHERE id=?", cid)
    world.tick_all(cid)
    assert world.status(cid) == "stopped", dump(world, cid)
    assert [(a["status"], a["failure_class"]) for a in world.attempts(cid)] == [
        ("failed_transient", "infra_container_killed"),
        ("refused_budget", "budget_refused"),
    ]
    assert len(world.worker_runs) == 1  # never run past the cap
    assert "budget" in world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]


def test_a_sigkill_137_without_an_oom_flag_is_an_infrastructure_retry_per_adr_0058(tmp_path):
    """A real pretrain killed by SIGKILL exits 137 with no out-of-memory marker: the exit code
    is all control has, so a single-provider stage retries it in place, bounded."""
    world, cid = _world(tmp_path, infra_retries=1)
    world.scenarios = [(137, _out(1, 0)), (0, _out(2, 0))]  # SIGKILL: exit 137, no OOM marker
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    first, second = world.attempts(cid)
    assert (first["status"], first["failure_class"], first["exit_code"]) == (
        "failed_transient",
        "infra_container_killed",
        137,
    )
    assert second["failure_class"] is None and second["provider_id"] == first["provider_id"]
    (event,) = world.events_of("campaign.stage_infra_retry")
    assert event["payload"]["reason"] == "infra_container_killed"


def test_a_reclassified_stop_registers_no_lineage_and_a_downstream_stage_sees_only_the_retry(
    tmp_path,
):
    import hashlib

    world = World(tmp_path)
    register_from_config(
        {"self_hosted": [{"id": "lab-box", "worker_principal_id": world.env.worker["id"]}]}
    )
    spec = world.env.spec()
    first = stage_spec(
        world.env,
        "s1",
        metrics=METRICS,
        retry={
            "max_attempts": 1,
            "infra_retries": 1,
            "resource_limit_metrics": ["stopped"],
            "reroute_providers": ["lab-box"],
        },
    )
    second = stage_spec(
        world.env,
        "s2",
        inputs=[
            first["inputs"][0],
            {"kind": "stage", "name": "up", "stage": "s1", "paths": ["data.json"]},
        ],
        metrics=METRICS,
    )
    spec["stages"] = [first, second]
    cid = world.campaign(spec)["id"]
    stopped, kept = _out(1, 1), _out(2, 0)
    world.scenarios = [(0, stopped), (0, kept), (0, _out(3, 0))]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    s1 = [a for a in world.attempts(cid) if a["stage_key"] == "s1"]
    assert [a["failure_class"] for a in s1] == ["infra_resource_limit", None]
    digest = lambda out: hashlib.sha256(out["data.json"].encode()).hexdigest()  # noqa: E731
    nodes = {r["sha256"] for r in world.env.query("SELECT sha256 FROM artifact_lineage_nodes")}
    assert digest(kept) in nodes  # the retry's output is what the lineage records
    assert digest(stopped) not in nodes  # the stopped attempt registered none
    # The downstream stage took the retry's artifact, not the stopped attempt's.
    assert len([a for a in world.attempts(cid) if a["stage_key"] == "s2"]) == 1


def test_a_budget_or_policy_skip_is_the_stage_failure_before_the_resource_limit():
    from tessera_control.campaign_placement import Decision, Skip, failure_reason

    def reason(*skips):
        return failure_reason(Decision(("a", "b"), None, tuple(Skip(p, r) for p, r in skips)))

    last = ("a", "resource_limit_last_attempt")
    assert reason(last, ("b", "unregistered")) == "resource_limit_no_provider"
    assert reason(last, ("b", "untrusted")) == "input_not_admitted"
    assert reason(last, ("b", "over_run_cap")) == "no_eligible_provider"
    assert reason(last, ("b", "plan_mismatch")) == "no_eligible_provider"
