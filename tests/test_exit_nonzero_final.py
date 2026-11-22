"""A confirmed non-infrastructure nonzero exit is a final failure carrying its exit code, with or
without outputs (ADR 0063). Fakes only: in-process control and a scripted worker."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import campaign_exited_resolve as cer
from tessera_control.execution_models import ReportExecutionObservation
from tessera_control.external_operations import confirmed_exit_code

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
OK = {"data.json": json.dumps({"score": 3})}


def _world(tmp_path, **stage_extra):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **stage_extra)]
    return world, world.campaign(spec)


def _classes(world, cid):
    return [(a["status"], a["failure_class"], a["exit_code"]) for a in world.attempts(cid)]


def _run_to_exit(world, cid):
    """Run the worker until its container exited and it stopped (no outputs, no receipt)."""
    for _ in range(40):
        world.runner.tick()
        world.poller.run(max_cycles=1)
        if world.worker_runs and world.attempts(cid)[-1]["status"] in {"admitted", "running"}:
            if _observed_exit(world, cid) is not None:
                return
    raise AssertionError("the worker never reported an exit\n" + dump(world, cid))


def _observed_exit(world, cid):
    grant = world.attempts(cid)[-1]["grant_id"]
    operation = world.row("SELECT operation_id FROM external_operations WHERE grant_id=?", grant)
    if operation is None:
        return None
    with_conn = world.env.query(
        "SELECT 1 FROM execution_observations WHERE operation_id=? AND observed_state='exited' "
        "AND termination_confirmed=1",
        operation["operation_id"],
    )
    return operation["operation_id"] if with_conn else None


def _later(world, minutes):
    world.runner.now = lambda: datetime.now(UTC) + timedelta(minutes=minutes)


def test_exit_one_with_no_outputs_is_a_final_failure_with_its_exit_code(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 3, "transient_exit_codes": [7]})
    cid = campaign["id"]
    world.scenarios = [(1, {})]  # the container fails and leaves nothing behind
    _run_to_exit(world, cid)
    assert not world.env.query("SELECT 1 FROM execution_run_receipts")  # no receipt will come
    _later(world, 10)  # past the grace for the receipt and manifest to arrive
    world.tick_all(cid, worker=False)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [("failed_permanent", "undeclared_exit_code", 1)]
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 1  # no retry
    assert world.row("SELECT status FROM execution_grants")["status"] == "reconciled"
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == "stage_failed:s1:undeclared_exit_code:exit_code=1"
    ledger = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", cid)
    assert (ledger["held_cents"], ledger["settled_cents"]) == (0, 0)


def test_the_receipt_and_outcome_show_the_exit_code(tmp_path):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    world.scenarios = [(1, {})]
    _run_to_exit(world, cid)
    _later(world, 10)
    world.tick_all(cid, worker=False)
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", cid)[
            "receipt_json"
        ]
    )
    (attempt,) = receipt["stages"][0]["attempts"]
    assert (attempt["status"], attempt["failure_class"], attempt["exit_code"]) == (
        "failed_permanent",
        "undeclared_exit_code",
        1,
    )
    assert "exit_code=1" in json.dumps(receipt)  # the outcome names the code too


def test_a_declared_transient_code_without_outputs_keeps_its_retry(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2, "transient_exit_codes": [7]})
    cid = campaign["id"]
    world.scenarios = [(7, {}), (0, OK)]
    _run_to_exit(world, cid)
    _later(world, 10)
    for _ in range(60):
        world.runner.tick()
        world.poller.run(max_cycles=1)
        if world.status(cid) in {"succeeded", "failed"}:
            break
    first = world.attempts(cid)[0]
    assert (first["status"], first["failure_class"], first["exit_code"]) == (
        "failed_transient",
        "t2_declared_exit_code",
        7,
    )
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 2


def test_an_infrastructure_exit_keeps_the_retried_path(tmp_path):
    """137 (killed by the engine or the out-of-memory killer) is not a task failure."""
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    world.scenarios = [(137, {}), (0, OK)]
    _run_to_exit(world, cid)
    _later(world, 10)
    for _ in range(60):
        world.runner.tick()
        world.poller.run(max_cycles=1)
        if len(world.attempts(cid)) > 1:
            break
    first = world.attempts(cid)[0]
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        cer.EXIT_CONFIRMED_UNRESOLVED,
    )
    assert "137" in first["failure_detail"]
    assert len(world.attempts(cid)) == 2


def test_a_zero_exit_without_a_receipt_is_still_never_a_pass(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    original = world.channel.post
    world.channel.post = lambda path, body, key: (
        (_ for _ in ()).throw(ConnectionError("lost"))
        if "run-receipt" in path or "artifact" in path
        else original(path, body, key)
    )
    world.scenarios = [(0, OK)]
    _run_to_exit(world, cid)
    _later(world, 10)
    world.runner.tick()
    first = world.attempts(cid)[0]
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        cer.EXIT_CONFIRMED_UNRESOLVED,
    )


def test_the_worker_reports_the_code_only_with_a_confirmed_exit(tmp_path):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    world.scenarios = [(1, {})]
    bodies = []
    original = world.channel.post

    def spy(path, body, key):
        if path == "/worker/observations":
            bodies.append(body)
        return original(path, body, key)

    world.channel.post = spy
    _run_to_exit(world, cid)
    assert bodies
    for body in bodies:
        confirmed = body["observed_state"] == "exited" and body["termination_confirmed"]
        assert ("exit_code" in body) == confirmed
        if confirmed:
            assert body["exit_code"] == 1
    operation = _observed_exit(world, cid)
    with world.runner.store.runner_connection() as connection:
        assert confirmed_exit_code(connection, operation) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"observed_state": "running"},
        {"observed_state": "exited", "termination_confirmed": False},
        {"observed_state": "unknown"},
    ],
)
def test_an_exit_code_is_refused_without_a_confirmed_exit(fields):
    base = {
        "lease_id": "lease",
        "expected_epoch": 1,
        "observed_at": "2026-01-01T00:00:00+00:00",
        "exit_code": 1,
    }
    with pytest.raises(ValueError):
        ReportExecutionObservation.model_validate(base | fields)
    ok = ReportExecutionObservation.model_validate(
        base | {"observed_state": "exited", "termination_confirmed": True}
    )
    assert ok.exit_code == 1


# -- composition with infrastructure retries (ADR 0058) ------------------------------------------


def test_with_infrastructure_retries_on_a_killed_exit_without_outputs_uses_their_bound(
    tmp_path, monkeypatch
):
    """A 137 the worker reported with no receipt follows the spec's infrastructure policy:
    retried under its own bound, never spending a scientific retry. (The test moves the
    runner's clock past a heartbeat's freshness, so the worker's memory report is fixed.)"""
    from tessera_control.campaign_provider_worker import WorkerGrantProvider

    monkeypatch.setattr(
        WorkerGrantProvider,
        "memory_report",
        lambda self, rt, ctx, stage: (8 * 1024**3, 256 * 1024**2),
    )
    world, campaign = _world(
        tmp_path, retry={"max_attempts": 1, "infra_retries": 1, "transient_exit_codes": [7]}
    )
    cid = campaign["id"]
    world.scenarios = [(137, {}), (137, {}), (0, {"data.json": json.dumps({"score": 3})})]
    _run_to_exit(world, cid)
    _later(world, 10)
    world.tick_all(cid)
    # One infrastructure retry was allowed: the second kill is final and says so.
    assert world.status(cid) == "failed", dump(world, cid)
    classes = [(s, c) for s, c, _ in _classes(world, cid)]
    assert classes == [
        ("failed_transient", "infra_container_killed"),
        ("failed_permanent", "undeclared_exit_code"),
    ]
    assert world.events_of("campaign.stage_infra_retries_exhausted")


def test_with_infrastructure_retries_off_a_killed_exit_keeps_the_historical_retry(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    world.scenarios = [(137, {}), (0, {"data.json": json.dumps({"score": 3})})]
    _run_to_exit(world, cid)
    _later(world, 10)
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert [(s, c) for s, c, _ in _classes(world, cid)][0] == (
        "failed_transient",
        cer.EXIT_CONFIRMED_UNRESOLVED,
    )
    assert not world.events_of("campaign.stage_infra_retry")  # off: nothing was enabled


def test_the_specs_own_infrastructure_codes_replace_the_default(tmp_path):
    world, campaign = _world(
        tmp_path, retry={"max_attempts": 1, "infra_retries": 1, "infra_exit_codes": [99]}
    )
    cid = campaign["id"]
    world.scenarios = [(137, {}), (0, {})]
    _run_to_exit(world, cid)
    _later(world, 10)
    world.tick_all(cid, worker=False)
    # 137 is not this spec's infrastructure code: with the loop on it is an ordinary final
    # failure, neither retried as a kill nor as the historical unresolved exit.
    assert [(s, c) for s, c, _ in _classes(world, cid)] == [
        ("failed_permanent", "undeclared_exit_code")
    ]
