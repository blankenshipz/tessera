"""ADR 0158: a container the stage watcher saw exit, whose executor never reported, is closed.

Fakes only: in-process control and the scripted worker. The executor's exit observation, run
receipt, manifest and artifacts are lost; only its earlier ``running`` observation and the
watcher's ``failed`` report (with or without the closed ``exit``) reach control.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import campaign_exit_without_report as ewr
from tessera_control.stage_watch_models import ReportStageWatch
from tessera_worker.stage_watcher.contract import StageContract
from tessera_worker.stage_watcher.killswitch import KillSwitch
from tessera_worker.stage_watcher.outbox import WatchOutbox
from tessera_worker.stage_watcher.probe import ProbeSample
from tessera_worker.stage_watcher.report import BODY_KEYS, WatchExit, WatchReport
from tessera_worker.stage_watcher.watcher import StageWatcher

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
CONTRACT = "e" * 64
CLOSED = ewr.EXITED_WITHOUT_REPORT
GRACE = ewr.EXIT_REPORT_GRACE_SECONDS


def _world(tmp_path, **stage_extra):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **stage_extra)]
    return world, world.campaign(spec)["id"]


def _killed_unreported(world, cid):
    """The container ran and exited; every report after the first ``running`` observation
    (the exit observation, the run receipt, the manifest, the artifacts) is lost."""
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )
    original = world.channel.post
    world.lost = []  # the observations that never reached control, to re-post later

    def cut(path, body, key):
        if "observations" in path and body.get("observed_state") != "running":
            world.lost.append((path, body, key))
            raise ConnectionError("synthetic report loss")
        if any(n in path for n in ("run-receipt", "manifest", "artifact")):
            raise ConnectionError("synthetic report loss")
        return original(path, body, key)

    world.channel.post = cut
    world.poller.run(max_cycles=1)
    world.runner.tick()  # the runner sees the ``running`` observation and binds the operation
    attempt = world.attempts(cid)[-1]
    assert attempt["status"] in ("admitted", "running"), dump(world, cid)
    assert attempt["operation_id"] is not None, dump(world, cid)
    return original, attempt


def _operation(world, grant_id):
    return world.row("SELECT * FROM external_operations WHERE grant_id=?", grant_id)


def _watch(world, post, attempt, *, kind="failed", exit=None, seq=1, at=None):
    """Post one watch report through the real control route; return its event row."""
    operation = _operation(world, attempt["grant_id"])
    lease = world.row("SELECT * FROM execution_leases WHERE id=?", operation["lease_id"])
    body = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "kind": kind,
        "observed_at": (at or datetime.now(UTC)).isoformat(),
        "seq": seq,
        "counters": {"elapsed_s": 12, "silent_s": 3},
        "kill_switch_engaged": False,
        "contract_digest": CONTRACT,
    }
    if exit is not None:
        body["exit"] = exit
    op = operation["operation_id"]
    post(f"/worker/operations/{op}/watch-reports", body, f"watch-{op}-{seq}")
    return world.row(
        "SELECT * FROM events WHERE event_type='execution.watch_reported' ORDER BY cursor DESC"
    )


def _at(world, event, seconds):
    recorded = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
    world.runner.now = lambda: recorded + timedelta(seconds=seconds)


def _attempt(world, attempt_id):
    return world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt_id)


def _grant(world, grant_id):
    return world.row("SELECT * FROM execution_grants WHERE id=?", grant_id)


def _closed_events(world):
    return [
        e
        for e in world.events_of("campaign.attempt_exit_resolved")
        if e["payload"].get("closed_reason") == CLOSED
    ]


def _assess(world, attempt_id, event, seconds, **kwargs):
    recorded = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
    with world.runner.store.runner_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        return ewr.assess(
            connection,
            row,
            now=recorded + timedelta(seconds=seconds),
            free_worker_grant=kwargs.pop("free", True),
            **kwargs,
        )


KILLED = {"exit_code": 137, "oom_killed": False}


# -- the window and the close --------------------------------------------------------------


def test_the_window_boundary_then_one_close_that_frees_the_slot_and_shows_on_the_receipt(
    tmp_path,
):
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    _at(world, event, GRACE - 1)
    world.runner.tick()
    assert _attempt(world, attempt["id"])["settled_cents"] is None
    assert _grant(world, attempt["grant_id"])["status"] == "active"
    _at(world, event, GRACE)
    world.runner.tick()
    done = _attempt(world, attempt["id"])
    assert (done["status"], done["failure_class"]) == ("failed_transient", CLOSED), dump(world, cid)
    assert (done["settled_cents"], done["settlement_basis"]) == (0, "local_zero")
    assert done["exit_code"] is None  # the watcher's code is evidence, not the attempt's code
    assert "killed" in done["failure_detail"] and "exit_code:137" in done["failure_detail"]
    grant = _grant(world, attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    (closed,) = _closed_events(world)
    payload = closed["payload"]
    assert payload["exit_class"] == "killed" and payload["exit_code"] == 137
    assert payload["watch_event_cursor"] == event["cursor"]
    assert payload["is_evidence"] is False and payload["automatic"] is True
    (reconciled,) = world.events_of("execution.exited_grant_reconciled")
    assert reconciled["payload"]["watch_event_cursor"] == event["cursor"]
    assert reconciled["payload"]["observation_cursor"] is None
    for _ in range(6):
        world.runner.tick()
        if world.status(cid) in {"failed", "stopped", "succeeded"}:
            break
    assert world.status(cid) == "failed", dump(world, cid)
    stored = world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", cid)
    entries = [
        a for stage in json.loads(stored["receipt_json"])["stages"] for a in stage["attempts"]
    ]
    resolved = entries[0]["exit_resolved"]
    assert resolved["closed_reason"] == CLOSED and resolved["exit_class"] == "killed"
    assert resolved["watch_event_cursor"] == event["cursor"] and resolved["is_evidence"] is False


def test_two_sweeps_and_a_stale_row_have_one_effect(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    _at(world, event, GRACE + 1)
    ctx = world.runner._load(cid)
    assert world.runner._close_unreported_exit(ctx, dict(attempt)) is True
    before = (
        dict(_grant(world, attempt["grant_id"])),
        dict(world.row("SELECT * FROM execution_project_limits")),
    )
    assert world.runner._close_unreported_exit(ctx, dict(attempt)) is False  # stale row
    world.runner._close_unreported_exits(ctx)
    world.runner._resolve_exited_attempts(ctx)  # ADR 0053 finds nothing left to resolve
    after = (
        dict(_grant(world, attempt["grant_id"])),
        dict(world.row("SELECT * FROM execution_project_limits")),
    )
    assert before == after and len(_closed_events(world)) == 1
    assert len(world.events_of("campaign.attempt_exit_resolved")) == 1


def test_a_second_closer_that_wins_first_rolls_this_close_back(tmp_path, monkeypatch):
    """A human reconcile (or ADR 0048/0053) moving the grant's revision between this runner's
    assessment and its release raises Conflict: nothing of this close is committed."""
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    _at(world, event, GRACE + 1)
    from tessera_control import campaign_runner as runner_module

    real = runner_module.release_grant

    def racing(connection, grant, **kwargs):
        connection.execute(
            "UPDATE execution_grants SET revision=revision+1 WHERE id=?", (grant["id"],)
        )
        return real(connection, grant, **kwargs)

    monkeypatch.setattr(runner_module, "release_grant", racing)
    world.runner._close_unreported_exits(world.runner._load(cid))
    row = _attempt(world, attempt["id"])
    assert row["settled_cents"] is None and row["status"] in ("admitted", "running")
    assert _grant(world, attempt["grant_id"])["status"] == "active" and not _closed_events(world)


def test_a_runner_that_lost_its_lease_closes_nothing(tmp_path):
    from tessera_control.campaign_runner import CampaignRunner

    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    _at(world, event, GRACE + 1)
    other = CampaignRunner(world.env.state)
    other.now = world.runner.now
    try:
        closed = other._close_unreported_exit(other._load(cid), dict(attempt))
    except Exception as error:  # noqa: BLE001 - the lease check refuses; nothing changes
        assert "lease" in type(error).__name__.lower()
        closed = False
    assert closed is False and _grant(world, attempt["grant_id"])["status"] == "active"


# -- what never closes -------------------------------------------------------------------------


def test_a_failed_report_without_exit_never_closes(tmp_path):
    """unknown/absent for 3 ticks, a passed deadline and exited_unknown: no ``exit`` field."""
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt)  # failed, no exit
    assert "exit" not in json.loads(event["payload_json"])
    assert _assess(world, attempt["id"], event, ewr.MAX_EXIT_REPORT_GRACE_SECONDS * 3) is None
    _at(world, event, ewr.MAX_EXIT_REPORT_GRACE_SECONDS + 60)
    world.runner.tick()
    row = _attempt(world, attempt["id"])
    assert row["failure_class"] != CLOSED and row["settled_cents"] is None
    assert not _closed_events(world)


def test_a_clean_zero_exit_a_billable_provider_or_another_lease_never_closes(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    aid = attempt["id"]
    assert _assess(world, aid, event, GRACE) is not None
    assert _assess(world, aid, event, GRACE, free=False) is None
    payload = json.loads(event["payload_json"])

    def rewrite(**changes):
        world.env.execute(
            "UPDATE events SET payload_json=? WHERE cursor=?",
            json.dumps({**payload, **changes}),
            event["cursor"],
        )

    rewrite(exit={"exit_code": 0, "oom_killed": False})
    assert _assess(world, aid, event, GRACE) is None
    rewrite(lease_id="another-lease")
    assert _assess(world, aid, event, GRACE) is None
    rewrite(epoch=payload["epoch"] + 1)
    assert _assess(world, aid, event, GRACE) is None
    rewrite(kind="stuck")
    assert _assess(world, aid, event, GRACE) is None
    rewrite(exit={"exit_code": "137", "oom_killed": False})
    assert _assess(world, aid, event, GRACE) is None
    rewrite()
    assert _assess(world, aid, event, GRACE) is not None


def test_a_live_lease_blocks_the_close(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    far = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    world.env.execute(
        "UPDATE execution_leases SET expires_at=? WHERE grant_id=?", far, attempt["grant_id"]
    )
    _at(world, event, GRACE + 60)
    world.runner.tick()
    assert _attempt(world, attempt["id"])["settled_cents"] is None and not _closed_events(world)
    assert _assess(world, attempt["id"], event, GRACE + 60) is None


def test_a_later_running_observation_or_a_confirmed_exit_blocks_the_close(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    aid = attempt["id"]
    assert _assess(world, aid, event, GRACE) is not None
    operation = _operation(world, attempt["grant_id"])["operation_id"]
    obs = world.row(
        "SELECT * FROM execution_observations WHERE operation_id=? ORDER BY rowid DESC", operation
    )
    assert obs is not None and obs["observed_state"] == "running"
    cols = [c for c in obs.keys() if c != "id"]

    def insert(identifier, **changes):
        world.env.execute(
            f"INSERT INTO execution_observations (id, {','.join(cols)}) "
            f"VALUES (?, {','.join('?' for _ in cols)})",
            identifier,
            *[changes.get(c, obs[c]) for c in cols],
        )

    later = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    insert("obs-later-running", observed_at=later, recorded_at=later)
    assert _assess(world, aid, event, GRACE) is None
    world.env.execute("DELETE FROM execution_observations WHERE id='obs-later-running'")
    assert _assess(world, aid, event, GRACE) is not None
    # A confirmed exit on record is ADR 0053's case; this path never acts on it.
    insert("obs-confirmed", observed_state="exited", termination_confirmed=1)
    assert _assess(world, aid, event, GRACE) is None


def test_an_upload_still_arriving_holds_the_close(tmp_path, monkeypatch):
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    calls = []

    def arriving(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(ewr, "uploads_arriving", arriving)
    assert _assess(world, attempt["id"], event, GRACE) is None and calls
    monkeypatch.undo()
    assert _assess(world, attempt["id"], event, GRACE) is not None


# -- late reports after the close ------------------------------------------------------------


def test_a_late_observation_receipt_or_watch_report_is_refused_and_nothing_settles_twice(
    tmp_path,
):
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    _at(world, event, GRACE)
    world.runner.tick()
    assert _attempt(world, attempt["id"])["failure_class"] == CLOSED
    ledger = dict(world.row("SELECT * FROM execution_project_limits"))
    grant = dict(_grant(world, attempt["grant_id"]))
    observations = len(world.env.query("SELECT id FROM execution_observations"))
    # The worker's confirmed exit observation, lost before, is re-posted under its own key:
    # control refuses it because the grant is already reconciled.
    exited = [
        (path, body, key)
        for path, body, key in world.lost
        if body.get("observed_state") == "exited" and body.get("termination_confirmed")
    ]
    assert exited, world.lost
    for path, body, key in exited:
        with pytest.raises(ConnectionError, match="409"):
            post(path, body, key)
    assert len(world.env.query("SELECT id FROM execution_observations")) == observations
    # A late watch report is refused as well (grant_reconciled).
    with pytest.raises(ConnectionError, match="409"):
        _watch(world, post, attempt, exit=KILLED, seq=2)
    # The worker comes back: its outbox redelivers whatever it still holds.
    world.channel.post = post
    world.poller.run(max_cycles=2)
    row = _attempt(world, attempt["id"])
    assert (row["status"], row["failure_class"], row["settled_cents"]) == (
        "failed_transient",
        CLOSED,
        0,
    )
    assert dict(world.row("SELECT * FROM execution_project_limits")) == ledger
    assert dict(_grant(world, attempt["grant_id"])) == grant
    world.runner.tick()
    assert len(_closed_events(world)) == 1
    assert len(world.events_of("execution.exited_grant_reconciled")) == 1
    assert world.status(cid) != "succeeded"


# -- classes and policy -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "oom", "klass"),
    [
        (137, False, "killed"),
        (143, False, "killed"),
        (137, True, "oom"),
        (1, True, "oom"),
        (1, False, "error"),
        (3, False, "error"),
        (0, False, None),
    ],
)
def test_exit_class_mapping(code, oom, klass):
    assert ewr.exit_class(code, oom) == klass


def test_status_follows_the_retry_policy_for_each_class(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 2, "transient_exit_codes": [3]})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    payload = json.loads(event["payload_json"])
    expected = {
        (137, False): ("failed_transient", "killed"),
        (143, False): ("failed_transient", "killed"),
        (137, True): ("failed_transient", "oom"),
        (3, False): ("failed_transient", "error"),  # a declared transient code
        (1, False): ("failed_transient", "error"),  # undeclared: still never permanent
        (2, True): ("failed_transient", "oom"),
    }
    for (code, oom), (status, klass) in expected.items():
        world.env.execute(
            "UPDATE events SET payload_json=? WHERE cursor=?",
            json.dumps({**payload, "exit": {"exit_code": code, "oom_killed": oom}}),
            event["cursor"],
        )
        verdict = _assess(world, attempt["id"], event, GRACE, transient_codes=frozenset({3}))
        assert (verdict.status, verdict.exit_class, verdict.failure_class) == (
            status,
            klass,
            CLOSED,
        )
        assert verdict.evidence["watch_event_cursor"] == event["cursor"]
        assert verdict.evidence["declared_transient"] is (code == 3)


def _close_one(world, cid, exit):
    """Lose one attempt's reports, post the watcher's exit, and let the runner close it."""
    world.runner.now = lambda: datetime.now(UTC)
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=exit)
    _at(world, event, GRACE)
    world.runner.tick()
    world.channel.post = post
    row = _attempt(world, attempt["id"])
    assert (row["status"], row["failure_class"]) == ("failed_transient", CLOSED), dump(world, cid)
    return row


def test_an_undeclared_code_is_retried_and_stops_at_the_bound(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 2})
    error = {"exit_code": 1, "oom_killed": False}
    _close_one(world, cid, error)
    world.tick_until(lambda: len(world.attempts(cid)) == 2, worker=False)
    _close_one(world, cid, error)
    for _ in range(6):
        world.runner.tick()
        if world.status(cid) in {"failed", "stopped", "succeeded"}:
            break
    assert world.status(cid) == "failed", dump(world, cid)
    attempts = world.attempts(cid)
    assert len(attempts) == 2
    assert all((a["status"], a["failure_class"]) == ("failed_transient", CLOSED) for a in attempts)
    assert all(e["payload"]["exit_class"] == "error" for e in _closed_events(world))
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == f"stage_failed:s1:{CLOSED}_exhausted", stop


def test_a_killed_close_is_retried_under_the_stage_policy(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 2})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    _at(world, event, GRACE)
    world.channel.post = post  # the retry's reports flow normally
    world.tick_until(lambda: len(world.attempts(cid)) == 2)
    first, second = world.attempts(cid)
    assert first["failure_class"] == CLOSED and second["attempt"] == 2


def test_with_infra_retries_on_a_kill_spends_the_infra_bound(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 1, "infra_retries": 1})
    post, attempt = _killed_unreported(world, cid)
    event = _watch(world, post, attempt, exit=KILLED)
    _at(world, event, GRACE)
    world.runner._close_unreported_exits(world.runner._load(cid))
    row = _attempt(world, attempt["id"])
    assert (row["status"], row["failure_class"]) == ("failed_transient", "infra_container_killed")
    assert CLOSED in row["failure_detail"]
    (closed,) = _closed_events(world)
    assert closed["payload"]["failure_class"] == "infra_container_killed"


def test_the_grace_is_bounded():
    assert ewr.grace_seconds(300) == 300 and ewr.grace_seconds(900) == 900
    for bad in (ewr.MIN_EXIT_REPORT_GRACE_SECONDS - 1, 901, True, 300.0):
        with pytest.raises(ValueError):
            ewr.grace_seconds(bad)


# -- the report, with and without the exit field ---------------------------------------------


def _report(**overrides):
    fields = {
        "operation_id": "a" * 32,
        "lease_id": "lease-1",
        "epoch": 1,
        "kind": "failed",
        "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "seq": 0,
        "counters": {"elapsed_s": 1},
        "kill_switch_engaged": False,
        "contract_digest": CONTRACT,
    } | overrides
    return WatchReport(**fields)


def test_a_report_round_trips_with_and_without_the_exit_field():
    plain = _report()
    assert set(plain.to_body()) == BODY_KEYS
    assert WatchReport.from_body(plain.operation_id, plain.to_body()) == plain
    with_exit = _report(exit=WatchExit(137, False))
    body = with_exit.to_body()
    assert set(body) == BODY_KEYS | {"exit"} and body["exit"] == KILLED
    assert WatchReport.from_body(with_exit.operation_id, body) == with_exit
    ReportStageWatch.model_validate(plain.to_body())
    assert ReportStageWatch.model_validate(body).exit.exit_code == 137
    for bad in (
        {**body, "exit": {"exit_code": 137}},
        {**body, "exit": {"exit_code": 137, "oom_killed": False, "signal": 9}},
        {**body, "exit": {"exit_code": 256, "oom_killed": False}},
        {**body, "extra": 1},
    ):
        with pytest.raises(ValueError):
            WatchReport.from_body(with_exit.operation_id, bad)
        with pytest.raises(ValueError):
            ReportStageWatch.model_validate(bad)
    with pytest.raises(ValueError):
        _report(kind="stuck", exit=WatchExit(137, False))
    with pytest.raises(ValueError):
        ReportStageWatch.model_validate({**body, "kind": "stuck"})
    with pytest.raises(ValueError):
        WatchExit(-1, False)


def test_control_refuses_an_exit_on_a_routine_report_and_records_it_only_on_failed(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 1})
    post, attempt = _killed_unreported(world, cid)
    with pytest.raises(ConnectionError, match="422"):
        _watch(world, post, attempt, kind="heartbeat", exit=KILLED)
    plain = _watch(world, post, attempt, kind="heartbeat", seq=1)
    assert "exit" not in json.loads(plain["payload_json"])
    failed = _watch(world, post, attempt, exit=KILLED, seq=2)
    assert json.loads(failed["payload_json"])["exit"] == KILLED


class _Probe:
    def __init__(self, samples):
        self.samples = list(samples)

    def sample(self, now):
        return self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]


class _Sink:
    def __init__(self):
        self.reports = []

    def deliver(self, report):
        self.reports.append(report)
        return True


def _watcher(tmp_path, samples, **contract):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    fields = {
        "operation_id": "b" * 32,
        "lease_id": "lease-1",
        "epoch": 1,
        "stage_key": "train",
        "provider": "local",
        "deadline_seconds": 3600,
        "silent_seconds": 300,
        "heartbeat_seconds": 60,
        "max_reports": 100,
        "kill_switch_path": None,
    } | contract
    stage = StageContract(**fields)
    sink = _Sink()
    watcher = StageWatcher(
        stage,
        _Probe(samples),
        sink,
        KillSwitch(None),
        lambda: now[0],
        WatchOutbox(tmp_path / "outbox.sqlite3"),
        sleep=lambda _: None,
    )

    def tick(seconds=10):
        now[0] += timedelta(seconds=seconds)
        watcher.tick()

    return watcher, sink, tick, now


def _sample(state, now=datetime(2026, 1, 1, tzinfo=UTC), **kwargs):
    return ProbeSample(state, now, **kwargs)


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ([_sample("exited", exit_code=137)], {"exit_code": 137, "oom_killed": False}),
        ([_sample("exited", exit_code=1, oom_killed=True)], {"exit_code": 1, "oom_killed": True}),
        ([_sample("exited", exit_code=None, oom_killed=True)], None),  # no code: no exit
        ([_sample("unknown")], None),
        ([_sample("absent")], None),
    ],
)
def test_the_watcher_sets_exit_only_for_a_definitive_exit(tmp_path, samples, expected):
    watcher, sink, tick, _ = _watcher(tmp_path, samples)
    for _ in range(4):
        tick()
        if watcher.done:
            break
    (failed,) = [r for r in sink.reports if r.kind == "failed"]
    assert (None if failed.exit is None else failed.exit.to_body()) == expected
    # The report survives the durable outbox round trip with the same exit.
    (recorded,) = [r for r in watcher._outbox.recorded() if r.kind == "failed"]
    assert recorded.exit == failed.exit


def test_a_deadline_failure_carries_no_exit(tmp_path):
    watcher, sink, tick, _ = _watcher(
        tmp_path,
        [_sample("running", output_bytes=1)],
        deadline_seconds=60,
        silent_seconds=60,
        heartbeat_seconds=60,
    )
    for _ in range(10):
        tick(30)
        if watcher.done:
            break
    (failed,) = [r for r in sink.reports if r.kind == "failed"]
    assert failed.exit is None
