"""Grant lifetime, slot and release rules (ADR 0048). Fakes only; no provider calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_runner import CampaignRunner
from tessera_control.store import Conflict

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
PAST = "2000-01-01T00:00:00Z"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# -- 1. admission ------------------------------------------------------------------------


def test_admission_refuses_a_grant_shorter_than_the_stage_duration(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "s1", metrics=METRIC, max_duration_seconds=900, grant_ttl_seconds=900)
    ]
    with pytest.raises(Conflict) as refused:
        world.env.submit(spec=spec)
    message = str(refused.value)
    assert "grant_shorter_than_stage_duration" in message
    assert "grant_ttl_seconds 900" in message and "max_duration_seconds 900" in message
    assert "1200s" in message  # duration plus the settle margin
    assert world.row("SELECT count(*) AS n FROM campaigns")["n"] == 0


def test_admission_accepts_a_grant_that_outlives_the_stage_by_the_margin(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "s1", metrics=METRIC, max_duration_seconds=600, grant_ttl_seconds=900)
    ]
    assert world.env.submit(spec=spec)["id"]  # 600 + 300 == 900 is exactly enough


# -- 2. the lifetime starts when a slot is free ---------------------------------------------


def test_a_grant_waits_for_a_free_worker_slot_and_its_lifetime_starts_then(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "a", metrics=METRIC),
        stage_spec(world.env, "b", metrics=METRIC),
    ]
    # The project allows several grants at once; the one worker still runs one at a time.
    world.env.execute("UPDATE execution_project_limits SET max_concurrency=4")
    cid = world.campaign(spec)["id"]
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, worker=False
    )
    for _ in range(6):
        world.runner.tick()
    # The worker's one slot is held by the first grant: the second is not issued, however
    # many ticks pass, and the wait is recorded once.
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, dump(world, cid)
    (waiting,) = world.events_of("campaign.stage_waiting_slot")
    assert waiting["payload"]["stage_key"] in {"a", "b"}
    assert not world.events_of("campaign.stage_waiting_limits")
    first = world.row("SELECT * FROM execution_grants")
    # Time passes while the first run holds the slot; the second grant's lifetime still
    # starts only when it is issued.
    later = datetime.now(UTC) + timedelta(minutes=10)
    world.runner = CampaignRunner(world.env.state, now=lambda: later)
    world.env.execute("UPDATE campaign_runner_lease SET expires_at=?", PAST)
    world.poller.run(max_cycles=1)  # the first run finishes
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 2, worker=False
    )
    second = world.row("SELECT * FROM execution_grants WHERE id!=?", first["id"])
    lifetime = _parse(second["expires_at"]) - later
    assert timedelta(minutes=14) <= lifetime <= timedelta(minutes=15, seconds=5), lifetime


def test_no_wait_event_when_the_slot_is_free(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s", metrics=METRIC)]
    cid = world.campaign(spec)["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert not world.events_of("campaign.stage_waiting_slot")


# -- 3. an expired grant with a confirmed exit releases its slot ------------------------------


def _finished_campaign(tmp_path, **stage_extra) -> tuple[World, dict, dict]:
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **stage_extra)]
    cid = world.campaign(spec)["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    (attempt,) = world.attempts(cid)
    return (
        world,
        world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"]),
        attempt,
    )


def _unresolve(world, grant, *, expires=PAST, settled=0):
    """Put a finished grant back to unresolved, as if nothing had reconciled it."""
    world.env.execute(
        """UPDATE execution_grants SET status='active', reconciled_at=NULL,
        reconciled_by_principal_id=NULL, settlement_cents=NULL, expires_at=? WHERE id=?""",
        expires,
        grant["id"],
    )
    world.env.execute(
        "UPDATE execution_project_limits SET held_cents=?, settled_cents=0",
        grant["max_exposure_cents"],
    )


def _sweep(world):
    world.env.execute("UPDATE campaign_runner_lease SET expires_at=?", PAST)
    world.runner = CampaignRunner(world.env.state)
    world.runner.tick()


def test_an_expired_grant_with_a_confirmed_exit_is_released_automatically(tmp_path):
    world, grant, _ = _finished_campaign(tmp_path)
    _unresolve(world, grant)
    _sweep(world)
    row = world.row("SELECT * FROM execution_grants WHERE id=?", grant["id"])
    assert row["status"] == "reconciled" and row["settlement_cents"] == 0
    assert row["revision"] > grant["revision"]
    limits = world.row("SELECT held_cents, settled_cents FROM execution_project_limits")
    assert (limits["held_cents"], limits["settled_cents"]) == (0, 0)
    (event,) = world.events_of("execution.expired_terminal_grant_reconciled")
    assert event["payload"]["grant_id"] == grant["id"]
    assert event["payload"]["basis"] == "expired_with_confirmed_exit"
    assert event["payload"]["provider_termination_inferred"] is False
    assert event["payload"]["automatic"] is True


def test_a_grant_that_has_not_expired_is_left_alone(tmp_path):
    world, grant, _ = _finished_campaign(tmp_path)
    _unresolve(world, grant, expires="2999-01-01T00:00:00Z")
    _sweep(world)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", grant["id"])["status"]
        == "active"
    )


def test_an_unconfirmed_or_contradicted_exit_is_never_released(tmp_path):
    world, grant, _ = _finished_campaign(tmp_path)
    _unresolve(world, grant)
    lease = world.row("SELECT * FROM execution_leases WHERE grant_id=?", grant["id"])
    later = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    world.env.execute(
        """INSERT INTO execution_observations (id, grant_id, lease_id, worker_principal_id,
        epoch, operation_id, desired_state, observed_state, observed_at, provider_id,
        termination_confirmed, recorded_at) VALUES ('observation_late', ?, ?, ?, ?,
        (SELECT operation_id FROM external_operations WHERE grant_id=?), 'admitted',
        'running', ?, NULL, 0, ?)""",
        grant["id"],
        lease["id"],
        lease["worker_principal_id"],
        lease["epoch"],
        grant["id"],
        later,
        later,
    )
    _sweep(world)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", grant["id"])["status"]
        == "active"
    )
    assert not world.events_of("execution.expired_terminal_grant_reconciled")


def test_a_grant_with_no_operation_is_never_released(tmp_path):
    world, grant, _ = _finished_campaign(tmp_path)
    _unresolve(world, grant)
    world.env.execute("DROP TRIGGER IF EXISTS execution_observations_no_delete")
    world.env.execute("DELETE FROM execution_observations")
    _sweep(world)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", grant["id"])["status"]
        == "active"
    )


def test_a_grant_a_campaign_attempt_still_owns_is_not_released_from_under_it(tmp_path):
    world, grant, attempt = _finished_campaign(tmp_path)
    _unresolve(world, grant)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='collected' WHERE id=?", attempt["id"]
    )
    _sweep(world)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", grant["id"])["status"]
        == "active"
    )


def test_a_nonzero_grant_settles_only_at_the_recorded_cost(tmp_path):
    world, grant, attempt = _finished_campaign(tmp_path, max_exposure_cents=5)
    assert grant["max_exposure_cents"] == 5
    _unresolve(world, grant)
    # No recorded cost: nothing is guessed and the grant stays unresolved.
    world.env.execute(
        "UPDATE campaign_stage_attempts SET settled_cents=NULL WHERE id=?", attempt["id"]
    )
    _sweep(world)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", grant["id"])["status"]
        == "active"
    )
    # A recorded cost within the hold is settled exactly, and the hold is released.
    world.env.execute(
        "UPDATE campaign_stage_attempts SET settled_cents=2 WHERE id=?", attempt["id"]
    )
    _sweep(world)
    row = world.row("SELECT * FROM execution_grants WHERE id=?", grant["id"])
    assert row["status"] == "reconciled" and row["settlement_cents"] == 2
    limits = world.row("SELECT held_cents, settled_cents FROM execution_project_limits")
    assert (limits["held_cents"], limits["settled_cents"]) == (0, 2)


def test_a_recorded_cost_above_the_hold_is_not_settled(tmp_path):
    world, grant, attempt = _finished_campaign(tmp_path, max_exposure_cents=5)
    _unresolve(world, grant)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET settled_cents=9 WHERE id=?", attempt["id"]
    )
    _sweep(world)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", grant["id"])["status"]
        == "active"
    )


# -- slots: more than one, and a refused launch frees its own -----------------------------------


class _Rt:
    """The two runner services ``slot_free`` reads."""

    def __init__(self, rows, now):
        self._rows, self._now = rows, now

    def rows(self, sql, params=()):
        return self._rows

    def stamp(self):
        return self._now

    @staticmethod
    def parse_utc(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _provider(slots):
    from tessera_control.campaign_provider_worker import WorkerGrantProvider, _descriptor

    return WorkerGrantProvider(_descriptor("self-hosted", "worker_x", None, slots))


def test_worker_slots_bound_how_many_live_grants_a_worker_may_hold():
    live = {"expires_at": "2999-01-01T00:00:00Z"}
    rt = _Rt([live, live], "2026-01-01T00:00:00Z")
    assert not _provider(2).slot_free(rt, None, _Stage())
    assert _provider(3).slot_free(rt, None, _Stage())
    rt = _Rt([live, {"expires_at": "2000-01-01T00:00:00Z"}], "2026-01-01T00:00:00Z")
    assert _provider(1).slot_free(
        _Rt([{"expires_at": "2000-01-01T00:00:00Z"}], rt._now), None, _Stage()
    )
    assert not _provider(1).slot_free(rt, None, _Stage())  # the expired one occupies nothing


class _Stage:
    worker_principal_id = "worker_x"


def test_worker_slots_come_from_the_operator_file_and_are_bounded():
    from pydantic import ValidationError

    from tessera_control.campaign_provider_worker import ProvidersFile, register_from_config
    from tessera_control.campaign_providers import get_provider

    parsed = ProvidersFile.model_validate(
        {"self_hosted": [{"id": "lab-box", "worker_principal_id": "w", "worker_slots": 3}]}
    )
    assert parsed.self_hosted[0].worker_slots == 3
    assert ProvidersFile.model_validate({}).local_docker is None
    for bad in (0, 9, True, "2"):
        with pytest.raises(ValidationError):
            ProvidersFile.model_validate(
                {
                    "self_hosted": [
                        {"id": "lab-box", "worker_principal_id": "w", "worker_slots": bad}
                    ]
                }
            )
    register_from_config({"self_hosted": [{"id": "lab-box", "worker_principal_id": "w"}]})
    assert get_provider("lab-box").descriptor.worker_slots == 1
    register_from_config(
        {
            "local_docker": {"worker_principal_id": "w", "worker_slots": 2},
            "self_hosted": [{"id": "lab-box", "worker_principal_id": "w", "worker_slots": 4}],
        }
    )
    assert get_provider("lab-box").descriptor.worker_slots == 4
    assert get_provider("local-docker").descriptor.worker_slots == 2
    register_from_config(None)  # leave the registry as other tests expect it


def test_a_grant_whose_launch_was_refused_frees_its_slot_at_once(tmp_path, monkeypatch):
    from campaign_runner_harness import ScriptedAdapter

    from tessera_worker.cpu import CpuLaunchError

    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "a", metrics=METRIC),
        stage_spec(world.env, "b", metrics=METRIC),
    ]
    world.env.execute("UPDATE execution_project_limits SET max_concurrency=4")
    cid = world.campaign(spec)["id"]
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, worker=False
    )
    for _ in range(3):
        world.runner.tick()
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, dump(world, cid)

    def refuse(self, operation):
        raise CpuLaunchError("refused", "network_address_pool_exhausted")

    monkeypatch.setattr(ScriptedAdapter, "prepare", refuse)
    world.poller.run(max_cycles=1)  # leased, refused, reported; the grant is still unexpired
    first = world.row("SELECT * FROM execution_grants ORDER BY created_at LIMIT 1")
    assert first["status"] == "active"
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 2, worker=False
    )


# -- review follow-ups: replay, release expiry, scan window, attribution ----------------------


def test_a_replayed_grant_issue_does_not_wait_for_a_slot(tmp_path, monkeypatch):
    from tessera_control.campaign_provider_worker import WorkerGrantProvider

    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s", metrics=METRIC)]
    cid = world.campaign(spec)["id"]
    monkeypatch.setattr(WorkerGrantProvider, "slot_free", lambda *args: False)
    world.tick_until(lambda: world.events_of("campaign.stage_waiting_slot"), worker=False)
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 0
    # An issue_grant intent already exists (a replay after a crash): the slot is not asked
    # again, so the replay is never held back by a slot its own grant may hold.
    (attempt,) = world.attempts(cid)
    world.env.execute(
        """INSERT INTO campaign_step_intents (id, campaign_id, approval_id, stage_key, attempt,
        step, ordinal, principal_id, method, path, request_digest, idempotency_key,
        resources_json, status, created_at)
        SELECT 'intent_replay', campaign_id, approval_id, stage_key, attempt, 'issue_grant', 0,
        principal_id, method, path, request_digest, 'replay-key', resources_json, 'pending',
        created_at FROM campaign_step_intents WHERE step='release_snapshot'"""
    )
    waits = len(world.events_of("campaign.stage_waiting_slot"))
    world.env.execute("DELETE FROM events WHERE event_type='campaign.stage_waiting_slot'")
    for _ in range(2):
        world.runner.tick()
    assert waits == 1
    assert not world.events_of("campaign.stage_waiting_slot"), dump(world, cid)
    assert attempt["grant_id"] is None


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_release_that_would_cut_the_grant_short_is_renewed_before_issue(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "a", metrics=METRIC),
        stage_spec(world.env, "b", metrics=METRIC),
    ]
    world.env.execute("UPDATE execution_project_limits SET max_concurrency=4")
    cid = world.campaign(spec)["id"]
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, worker=False
    )
    for _ in range(4):
        world.runner.tick()
    world.poller.run(max_cycles=1)  # the first run finishes
    old = world.row("SELECT * FROM snapshot_releases WHERE status='active'")
    # The long wait left the release expiring inside the next grant's lifetime.
    soon = _iso(datetime.now(UTC) + timedelta(minutes=5))
    world.env.execute("UPDATE snapshot_releases SET expires_at=? WHERE id=?", soon, old["id"])
    later = datetime.now(UTC) + timedelta(minutes=10)
    world.runner = CampaignRunner(world.env.state, now=lambda: later)
    world.env.execute("UPDATE campaign_runner_lease SET expires_at=?", PAST)
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 2, worker=False
    )
    second = world.row("SELECT * FROM execution_grants ORDER BY created_at DESC, id DESC LIMIT 1")
    assert second["snapshot_release_id"] != old["id"], dump(world, cid)
    assert (
        world.row("SELECT status FROM snapshot_releases WHERE id=?", old["id"])["status"]
        == "revoked"
    )
    lifetime = _parse(second["expires_at"]) - later
    assert lifetime >= timedelta(minutes=14), lifetime  # not cut by the old release
    # The campaign still finishes and cleans up every release it made.
    world.runner = CampaignRunner(world.env.state)
    world.env.execute("UPDATE campaign_runner_lease SET expires_at=?", PAST)
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert not world.env.query("SELECT 1 FROM snapshot_releases WHERE status='active'")


def _two_finished_grants(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "a", metrics=METRIC),
        stage_spec(world.env, "b", metrics=METRIC),
    ]
    cid = world.campaign(spec)["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    grants = world.env.query("SELECT * FROM execution_grants ORDER BY created_at, id")
    assert len(grants) == 2
    return world, grants


def test_grants_that_never_qualify_cannot_starve_the_scan(tmp_path, monkeypatch):
    from tessera_control import grant_release

    world, (first, second) = _two_finished_grants(tmp_path)
    monkeypatch.setattr(grant_release, "SCAN_WINDOW", 1)
    for grant, expires in ((first, "2000-01-01T00:00:00Z"), (second, "2000-01-02T00:00:00Z")):
        world.env.execute(
            """UPDATE execution_grants SET status='active', reconciled_at=NULL,
            reconciled_by_principal_id=NULL, settlement_cents=NULL, expires_at=? WHERE id=?""",
            expires,
            grant["id"],
        )
    # The earlier grant waits on a person: a hold with no recorded cost.
    world.env.execute("UPDATE execution_grants SET max_exposure_cents=5 WHERE id=?", first["id"])
    world.env.execute(
        "UPDATE campaign_stage_attempts SET settled_cents=NULL WHERE grant_id=?", first["id"]
    )
    world.env.execute("UPDATE execution_project_limits SET held_cents=5, settled_cents=0")
    now = datetime.now(UTC)
    with world.runner.store.runner_connection() as connection:
        released, cursor = grant_release.release_expired_terminal_grants(connection, now=now)
        assert released == [] and cursor is not None  # examined the stuck one only
        released, cursor = grant_release.release_expired_terminal_grants(
            connection, now=now, after=cursor
        )
        assert [item["grant_id"] for item in released] == [second["id"]]
        released, cursor = grant_release.release_expired_terminal_grants(
            connection, now=now, after=cursor
        )
        assert released == [] and cursor is None  # an empty window: the scan wraps around
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", first["id"])["status"]
        == "active"
    )


def test_an_automatic_release_is_credited_to_the_campaign_actor_not_the_grant_creator(tmp_path):
    world, grant, attempt = _finished_campaign(tmp_path)
    _unresolve(world, grant)
    approver = world.row(
        "SELECT approving_principal_id FROM campaign_approvals ORDER BY approved_at DESC"
    )["approving_principal_id"]
    world.env.execute(
        "UPDATE execution_grants SET created_by_principal_id=? WHERE id=?",
        world.row("SELECT id FROM principals WHERE id!=? LIMIT 1", approver)["id"],
        grant["id"],
    )
    _sweep(world)
    row = world.row("SELECT * FROM execution_grants WHERE id=?", grant["id"])
    assert row["status"] == "reconciled"
    assert row["reconciled_by_principal_id"] == approver != row["created_by_principal_id"]
    (event,) = world.events_of("execution.expired_terminal_grant_reconciled")
    assert event["payload"]["credited_to"] == "campaign_runner"
    assert event["payload"]["campaign_id"] == attempt["campaign_id"]
    actor = world.row(
        "SELECT actor_principal_id FROM events WHERE event_type=?",
        "execution.expired_terminal_grant_reconciled",
    )
    assert actor["actor_principal_id"] == approver


def test_a_grant_no_campaign_issued_is_never_released_automatically(tmp_path):
    world, grant, attempt = _finished_campaign(tmp_path)
    _unresolve(world, grant)
    world.env.execute("UPDATE campaign_stage_attempts SET grant_id=NULL WHERE id=?", attempt["id"])
    _sweep(world)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", grant["id"])["status"]
        == "active"
    )


def test_a_revoked_short_release_is_refused_not_renewed_forever(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "a", metrics=METRIC),
        stage_spec(world.env, "b", metrics=METRIC),
    ]
    world.env.execute("UPDATE execution_project_limits SET max_concurrency=4")
    cid = world.campaign(spec)["id"]
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, worker=False
    )
    for _ in range(4):
        world.runner.tick()
    world.poller.run(max_cycles=1)  # the first run finishes
    # A person revokes the release, which also ends soon: it can only be refused.
    soon = _iso(datetime.now(UTC) + timedelta(minutes=5))
    world.env.execute(
        "UPDATE snapshot_releases SET status='revoked', revoked_at=?, expires_at=?", soon, soon
    )
    later = datetime.now(UTC) + timedelta(minutes=10)
    world.runner = CampaignRunner(world.env.state, now=lambda: later)
    world.env.execute("UPDATE campaign_runner_lease SET expires_at=?", PAST)
    for _ in range(30):
        world.runner.tick()
    waiting = [a for a in world.attempts(cid) if a["status"] == "reserved"]
    failed = [a for a in world.attempts(cid) if a["status"] == "failed_permanent"]
    assert not waiting, dump(world, cid)  # nothing is left spinning
    assert failed and failed[0]["failure_class"] == "step_refused"
    assert "no longer active" in failed[0]["failure_detail"]
    assert failed[0]["revision"] < 40  # and it did not churn while deciding


def test_waiting_on_a_shared_release_is_recorded(tmp_path, monkeypatch):
    from tessera_control.campaign_provider_worker import WorkerGrantProvider

    monkeypatch.setattr(WorkerGrantProvider, "slot_free", lambda *args: True)
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "a", metrics=METRIC),
        stage_spec(world.env, "b", metrics=METRIC),
    ]
    world.env.execute("UPDATE execution_project_limits SET max_concurrency=4")
    cid = world.campaign(spec)["id"]
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, worker=False
    )
    # The live grant uses the release; the waiting stage's release would end too soon.
    soon = _iso(datetime.now(UTC) + timedelta(minutes=5))
    world.env.execute("UPDATE snapshot_releases SET expires_at=? WHERE status='active'", soon)
    for _ in range(6):
        world.runner.tick()
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, dump(world, cid)
    (event,) = world.events_of("campaign.stage_waiting_release")
    assert event["payload"]["stage_key"] in {"a", "b"}


def test_a_store_refusal_of_a_replayed_grant_issue_is_recorded(tmp_path, monkeypatch):
    from tessera_control.execution import ExecutionStore

    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s", metrics=METRIC, max_exposure_cents=5)]
    cid = world.campaign(spec)["id"]
    original = ExecutionStore.issue_execution_grant

    def crash(self, *args, **kwargs):
        raise RuntimeError("synthetic crash before the grant committed")

    monkeypatch.setattr(ExecutionStore, "issue_execution_grant", crash)
    world.tick_until(
        lambda: world.env.query(
            "SELECT 1 FROM campaign_step_intents WHERE step='issue_grant' AND status='pending'"
        ),
        worker=False,
    )
    monkeypatch.setattr(ExecutionStore, "issue_execution_grant", original)
    # Meanwhile the project's limit no longer covers the hold; the replay skips the
    # runner's own check (the intent exists) and the store refuses it.
    world.env.execute("UPDATE execution_project_limits SET monetary_limit_cents=0")
    for _ in range(4):
        world.runner.tick()
    (attempt,) = world.attempts(cid)
    assert attempt["status"] == "failed_permanent", dump(world, cid)
    assert attempt["failure_class"] == "step_refused"
    assert "project execution limit" in attempt["failure_detail"]
    (intent,) = world.env.query("SELECT * FROM campaign_step_intents WHERE step='issue_grant'")
    assert intent["status"] == "failed" and intent["error_class"] == "Conflict"
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 0
    assert world.row("SELECT held_cents FROM campaigns WHERE id=?", cid)["held_cents"] == 0


def test_a_revoked_release_that_is_still_long_enough_ends_as_a_recorded_refusal(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "a", metrics=METRIC),
        stage_spec(world.env, "b", metrics=METRIC),
    ]
    world.env.execute("UPDATE execution_project_limits SET max_concurrency=4")
    cid = world.campaign(spec)["id"]
    world.tick_until(
        lambda: world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1, worker=False
    )
    for _ in range(4):
        world.runner.tick()
    world.poller.run(max_cycles=1)  # the first run finishes
    # A person revokes the release; it would still outlive the next grant.
    world.env.execute("UPDATE snapshot_releases SET status='revoked', revoked_at=?", PAST)
    for _ in range(30):
        world.runner.tick()
    attempts = world.attempts(cid)
    assert not [a for a in attempts if a["status"] == "reserved"], dump(world, cid)
    failed = [a for a in attempts if a["status"] == "failed_permanent"]
    assert failed and failed[0]["failure_class"] == "step_refused", dump(world, cid)
    assert failed[0]["failure_detail"]  # the store's reason is recorded
    assert failed[0]["revision"] < 40
    refused = world.env.query(
        "SELECT * FROM campaign_step_intents WHERE step='issue_grant' AND status='failed'"
    )
    assert refused and refused[0]["error_class"]
